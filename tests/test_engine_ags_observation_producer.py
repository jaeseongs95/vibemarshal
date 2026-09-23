"""V02-c golden bytes와 VM producer/별도 Node verifier fixture의 결속 검사."""
from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.ags_observation_producer import AGSObservationProducer, ProducerUnavailable, _canonical
from flowmarshal.engine.governance_gate import (
    GATE_KIND, CoreOperations, GovernancePlugin, GovernanceTaskGate, _Run,
)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "engine"
FIXTURE = json.loads((FIXTURE_DIR / "vm_observation_producer_contract.json").read_text(encoding="utf-8"))
VERIFY_SCRIPT = FIXTURE_DIR / "vm_observation_verify.mjs"


class _Ledger:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @contextmanager
    def read(self):
        yield self.connection

    @contextmanager
    def transaction(self):
        class Tx:
            connection = self.connection

            def history(inner, project_id, event_type, entity_type, entity_id, payload):
                sequence = self.connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM history_events").fetchone()[0]
                self.connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
                    project_id, entity_type, entity_id, event_type, json.dumps(payload), sequence,
                ))

        try:
            yield Tx()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise


def _service(example: dict) -> tuple[SimpleNamespace, sqlite3.Connection, dict[str, str]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE projects (id TEXT, active_goal_revision_id TEXT, active_plan_revision_id TEXT);
        CREATE TABLE goal_revisions (id TEXT, revision_no INTEGER);
        CREATE TABLE task_contracts (id TEXT, project_id TEXT, plan_revision_id TEXT, status TEXT);
        CREATE TABLE execution_spec_revisions (id TEXT, task_id TEXT, revision_no INTEGER, is_current INTEGER);
        CREATE TABLE attempts (id TEXT, task_id TEXT, project_id TEXT, kind TEXT, status TEXT, attempt_no INTEGER);
        CREATE TABLE history_events (project_id TEXT, entity_type TEXT, entity_id TEXT, event_type TEXT,
                                     payload_json TEXT, sequence INTEGER);
        CREATE TABLE budget_usage (id TEXT, project_id TEXT, stage TEXT, logical_call_ref TEXT, payload_json TEXT);
        CREATE TABLE provider_calls (usage_id TEXT, receipt_json TEXT);
        CREATE TABLE runtime_jobs (id TEXT, attempt_id TEXT, thread_id TEXT, turn_id TEXT);
        CREATE TABLE runtime_job_observations (id TEXT, job_id TEXT, project_id TEXT, provider_terminal INTEGER,
                                               terminal_status TEXT, observed_at TEXT, payload_json TEXT);
    """)
    core = example["core"]
    connection.execute("INSERT INTO projects VALUES (?,?,?)", ("project-1", "goal-1", "plan-1"))
    connection.execute("INSERT INTO goal_revisions VALUES (?,?)", ("goal-1", core["goalRevision"]))
    connection.execute("INSERT INTO task_contracts VALUES (?,?,?,?)",
                       ("core-task-1", "project-1", "plan-1", "running"))
    connection.execute("INSERT INTO execution_spec_revisions VALUES (?,?,?,?)",
                       ("spec-1", "core-task-1", core["taskRevision"], 1))
    if example["kind"] == "steward":
        ref = {"kind": "steward", "callId": example["terminal"]["callId"]}
        receipt = {
            "call_id": ref["callId"], "thread_id": example["terminal"]["threadId"],
            "turn_ids": [example["terminal"]["turnId"]], "status": "succeeded",
            "observed_model": example["terminal"]["model"],
            "observed_effort": example["terminal"]["effort"],
            "binding_provenance": {"observed": example["terminal"]["provenance"]},
        }
        usage = {
            "call_status": "succeeded", "thread_id": receipt["thread_id"],
            "turn_ids": receipt["turn_ids"], "recorded_at": example["terminal"]["observedAt"],
            "observed_model": example["terminal"]["model"], "observed_effort": example["terminal"]["effort"],
            "binding_provenance": {"observed": example["terminal"]["provenance"]},
            "runner_receipt_digest": sha256_digest(receipt),
        }
        connection.execute("INSERT INTO budget_usage VALUES (?,?,?,?,?)",
                           (example["terminal"]["eventId"], "project-1", "validation",
                            ref["callId"], json.dumps(usage)))
        connection.execute("INSERT INTO provider_calls VALUES (?,?)",
                           (example["terminal"]["eventId"], json.dumps(receipt)))
    else:
        attempt_id = example["binding"]["attemptId"]
        ref = {"kind": "worker", "attemptId": attempt_id}
        connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)",
                           (attempt_id, "core-task-1", "project-1", "execution", "succeeded",
                            core["attemptOrdinal"]))
        original = {
            "model_observation_source": example["terminal"]["provenance"],
            "observed_model": example["terminal"]["model"],
            "observed_effort": example["terminal"]["effort"],
        }
        result = {
            "active": False, "terminal_status": "completed",
            "thread_id": example["terminal"]["threadId"], "turn_id": example["terminal"]["turnId"],
            "payload": original,
        }
        job_id = "fixture-job-1"
        connection.execute("INSERT INTO runtime_jobs VALUES (?,?,?,?)",
                           (job_id, attempt_id, result["thread_id"], result["turn_id"]))
        connection.execute("INSERT INTO runtime_job_observations VALUES (?,?,?,?,?,?,?)",
                           (example["terminal"]["eventId"], job_id, "project-1", 1, "completed",
                            example["terminal"]["observedAt"], json.dumps({"result": result})))
        observation = {
            "thread_id": result["thread_id"], "turn_id": result["turn_id"],
            "terminal_status": "completed", "payload": original,
        }
        usage = {
            "attempt_id": attempt_id, "thread_id": result["thread_id"],
            "turn_ids": [result["turn_id"]], "call_status": "completed",
            "runtime_receipt_id": example["terminal"]["callId"],
            "observed_model": example["terminal"]["model"], "observed_effort": example["terminal"]["effort"],
            "binding_provenance": {"observed": example["terminal"]["provenance"]},
            "provider_observation": observation, "runner_receipt_digest": sha256_digest(observation),
        }
        connection.execute("INSERT INTO budget_usage VALUES (?,?,?,?,?)",
                           ("usage-worker-1", "project-1", "execution", example["terminal"]["turnId"],
                            json.dumps(usage)))
    key = {"task_id": "core-task-1", "execution_spec_revision_id": "spec-1",
           "attempt_no": core["attemptOrdinal"] or 1}
    prepared = {"request": {
        "key": key, "goalRevisionId": "goal-1",
        "step": "plan" if example["kind"] == "steward" else "record:implementation",
        "tool": example["invocation"]["tool"],
        "arguments": example["invocation"]["input"], "observation": {"terminalRef": ref},
    }}
    connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)",
                       ("project-1", "core_operation", core["gateOperationKey"],
                        "operation.prepared", json.dumps(prepared), 5))
    if example["kind"] == "steward":
        review = {"kind": GATE_KIND, "result": {"key": key, "step": "steward:bootstrap",
                                             "data": {"callId": ref["callId"]}}}
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
            "project-1", "core_operation", "review-1", "operation.completed", json.dumps(review), 4,
        ))
    else:
        plan_request = {"request": {"key": key, "step": "plan", "arguments": {
            "taskEnvelope": {"taskId": example["binding"]["taskId"]}}}}
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
            "project-1", "core_operation", "plan-1", "operation.prepared", json.dumps(plan_request), 1,
        ))
        plan = {"kind": GATE_KIND, "result": {"key": key, "step": "plan", "data": {"raw": {}}}}
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
            "project-1", "core_operation", "plan-1", "operation.completed", json.dumps(plan), 2,
        ))
        started = {"kind": GATE_KIND, "result": {"key": key, "step": "start", "data": {"raw": {
            "content": [{"text": json.dumps({"ok": True, "data": {"runId": example["binding"]["runId"]}})}],
        }}}}
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
            "project-1", "core_operation", "start-1", "operation.completed", json.dumps(started), 3,
        ))
    connection.commit()
    return SimpleNamespace(ledger=_Ledger(connection)), connection, ref


def _producer(example: dict, key: Ed25519PrivateKey) -> AGSObservationProducer:
    return AGSObservationProducer(
        private_key=key, installation_id=example["producer"]["installationId"],
        key_id=example["producer"]["keyId"], instance_id=example["producer"]["instanceId"],
        session_id=example["binding"]["sessionId"],
        clock=lambda: datetime.fromisoformat(example["issuedAt"].replace("Z", "+00:00")),
        nonce=lambda: example["nonce"], invocation_id=lambda: example["binding"]["invocationId"],
    )


def _issue(example: dict, producer: AGSObservationProducer, service, ref) -> dict[str, str]:
    return producer.issue(
        service=service, project_id="project-1", task_id="core-task-1",
        envelope_task_id=example["binding"]["taskId"], run_id=example["binding"]["runId"],
        attempt_id=example["binding"]["attemptId"], stage=example["core"]["stage"],
        operation_id=example["core"]["gateOperationKey"], tool=example["invocation"]["tool"],
        arguments=example["invocation"]["input"], terminal_ref=ref,
    )


def _node(receipt: dict, example: dict, producer: AGSObservationProducer, *,
           expected: dict | None = None, trust: dict | None = None, seen: list[str] | None = None,
           received_at: str | None = None, claim_dir: str | None = None,
           wire_arguments: dict | None = None) -> dict:
    pin = {
        "publicKeySpki": base64.b64encode(producer.public_key_spki()).decode("ascii"),
        "installationId": example["producer"]["installationId"], "hostId": example["producer"]["hostId"],
    }
    payload = {
        "receipt": receipt, "arguments": wire_arguments or {
            **example["invocation"]["input"], "_hostAttestation": receipt,
        }, "expected": expected or {
            "binding": example["binding"], "hostId": "flowmarshal-engine",
            "tool": example["invocation"]["tool"], "inputDigest": example["invocation"]["inputDigest"],
        },
        "trust": trust or {example["producer"]["keyId"]: pin},
        "seen": seen or [], "receivedAt": received_at or example["issuedAt"],
        "claimDir": claim_dir,
    }
    result = subprocess.run(["node", str(VERIFY_SCRIPT)], input=json.dumps(payload), text=True,
                            capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


class AGSObservationProducerTests(unittest.TestCase):
    def test_gate_issues_after_prepared_intent_and_plugin_skips_caller_signer(self) -> None:
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        connection.execute("DELETE FROM history_events WHERE event_type='operation.prepared'")
        producer = _producer(example, Ed25519PrivateKey.generate())

        class Operations:
            def invoke(inner, *, project_id, kind, request, execute):
                self.assertEqual(GATE_KIND, kind)
                operation_id = CoreOperations._operation_id(project_id, kind, request)[1]
                connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
                    project_id, "core_operation", operation_id, "operation.prepared",
                    json.dumps({"request": request}), 1,
                ))
                connection.commit()
                return execute()

        class Plugin:
            observation = None

            def call(inner, tool, arguments, observation):
                inner.observation = observation
                return {"content": [{"type": "text", "text": json.dumps({
                    "ok": True, "data": {"executionMode": "orchestrated", "stages": []}, "error": None,
                })}]}

        plugin = Plugin()
        with tempfile.TemporaryDirectory() as folder:
            run = _Run(
                key={"task_id": "core-task-1", "execution_spec_revision_id": "spec-1", "attempt_no": 1},
                root=Path(folder), folder=Path(folder),
                envelope={"taskId": example["binding"]["taskId"]}, c0={},
                run_id="",  # 최초 plan 호출 전 steward turn
            )
            gate = GovernanceTaskGate(
                service, plugin=plugin, steward=object(), state_dir=Path(folder),
                operations=Operations(), producer=producer,
            )
            result = gate._mcp(
                {"id": "core-task-1", "project_id": "project-1"}, run,
                "plan", example["invocation"]["tool"], example["invocation"]["input"],
                {"terminalRef": ref, "stage": "bootstrap"}, replay=False,
            )
        self.assertEqual("orchestrated", result["executionMode"])
        self.assertTrue(_node(plugin.observation["_vmProducerReceipt"], example, producer)["accepted"])

        class Client:
            arguments = None

            def call(inner, tool, arguments):
                inner.arguments = arguments
                return {"ok": True}

        client = Client()
        with tempfile.TemporaryDirectory() as folder:
            adapter = GovernancePlugin(Path(folder), Path(folder) / "state")
            adapter._client = lambda: client
            adapter.call(example["invocation"]["tool"], example["invocation"]["input"], plugin.observation)
        self.assertEqual(plugin.observation["_vmProducerReceipt"], client.arguments["_hostAttestation"])
        self.assertTrue(_node(plugin.observation["_vmProducerReceipt"], example, producer,
                              wire_arguments=client.arguments)["accepted"])
        self.assertEqual("tool_input_mismatch", _node(
            plugin.observation["_vmProducerReceipt"], example, producer,
            wire_arguments={**client.arguments, "tampered": True})["reason"])
        self.assertEqual("attestation_mismatch", _node(
            plugin.observation["_vmProducerReceipt"], example, producer,
            wire_arguments=example["invocation"]["input"])["reason"])

    def test_steward_and_worker_signed_bytes_match_frozen_golden_and_node_verifies(self) -> None:
        self.assertEqual(2, len(FIXTURE["validCases"]))
        goldens = {item["caseId"]: item for item in FIXTURE["bodyGolden"]}
        for example in FIXTURE["validCases"]:
            with self.subTest(example=example["id"]):
                service, connection, ref = _service(example)
                self.addCleanup(connection.close)
                producer = _producer(example, Ed25519PrivateKey.generate())
                receipt = _issue(example, producer, service, ref)
                golden = goldens[example["id"]]
                self.assertEqual(golden["bodyBase64url"], receipt["body"])
                self.assertEqual(golden["canonicalJson"],
                                 base64.urlsafe_b64decode(receipt["body"] + "==").decode("utf-8"))
                verified = _node(receipt, example, producer)
                self.assertTrue(verified["accepted"], verified)
                self.assertEqual(golden["expectedObservation"], verified["observation"])

    def test_signed_cross_binding_and_other_host_are_rejected_in_a_separate_process(self) -> None:
        example = FIXTURE["validCases"][1]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        producer = _producer(example, Ed25519PrivateKey.generate())
        receipt = _issue(example, producer, service, ref)
        base = json.loads(base64.urlsafe_b64decode(receipt["body"] + "=="))
        for field in FIXTURE["bindingFields"]:
            with self.subTest(field=field):
                body = json.loads(json.dumps(base))
                body["binding"][field] = f"other-{field}"
                bytes_ = _canonical(body).encode("utf-8")
                mutated = {"body": base64.urlsafe_b64encode(bytes_).rstrip(b"=").decode(),
                           "signature": base64.urlsafe_b64encode(producer._key.sign(bytes_)).rstrip(b"=").decode(),
                           "keyId": producer.key_id}
                result = _node(mutated, example, producer)
                self.assertFalse(result["accepted"], result)
                self.assertEqual("host_mismatch" if field == "hostId" else "binding_mismatch",
                                 result["reason"])
        for field, replacement in (("tool", "plan_workflow"), ("inputDigest", "sha256:" + "0" * 64)):
            with self.subTest(field=field):
                body = json.loads(json.dumps(base))
                body["invocation"][field] = replacement
                bytes_ = _canonical(body).encode("utf-8")
                mutated = {"body": base64.urlsafe_b64encode(bytes_).rstrip(b"=").decode(),
                           "signature": base64.urlsafe_b64encode(producer._key.sign(bytes_)).rstrip(b"=").decode(),
                           "keyId": producer.key_id}
                self.assertEqual("tool_input_mismatch", _node(mutated, example, producer)["reason"])
        other = json.loads(json.dumps(base))
        other["producer"]["hostId"] = other["binding"]["hostId"] = "other-host"
        bytes_ = _canonical(other).encode("utf-8")
        other_key = Ed25519PrivateKey.generate()
        other_producer = _producer(example, other_key)
        other_receipt = {"body": base64.urlsafe_b64encode(bytes_).rstrip(b"=").decode(),
                         "signature": base64.urlsafe_b64encode(other_key.sign(bytes_)).rstrip(b"=").decode(),
                         "keyId": producer.key_id}
        pin = {"publicKeySpki": base64.b64encode(other_producer.public_key_spki()).decode(),
               "installationId": example["producer"]["installationId"], "hostId": "other-host"}
        result = _node(other_receipt, example, other_producer, trust={producer.key_id: pin})
        self.assertEqual("host_mismatch", result["reason"])

    def test_new_governance_call_has_distinct_invocation_nonce_and_input(self) -> None:
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        producer = AGSObservationProducer(
            private_key=Ed25519PrivateKey.generate(),
            installation_id=example["producer"]["installationId"],
            key_id=example["producer"]["keyId"],
            instance_id=example["producer"]["instanceId"],
            session_id=example["binding"]["sessionId"],
            clock=lambda: datetime.fromisoformat(example["issuedAt"].replace("Z", "+00:00")),
        )
        first = _issue(example, producer, service, ref)
        second_input = {**example["invocation"]["input"], "second": True}
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
            "project-1", "core_operation", "second-operation", "operation.prepared",
            json.dumps({"request": {"key": {"task_id": "core-task-1", "execution_spec_revision_id": "spec-1", "attempt_no": 1},
                                    "goalRevisionId": "goal-1",
                                    "step": "plan",
                                    "tool": example["invocation"]["tool"],
                                    "arguments": second_input,
                                    "observation": {"terminalRef": ref}}}), 2,
        ))
        connection.commit()
        second = producer.issue(
            service=service, project_id="project-1", task_id="core-task-1",
            envelope_task_id=example["binding"]["taskId"], run_id=None, attempt_id=None,
            stage="bootstrap", operation_id="second-operation", tool=example["invocation"]["tool"],
            arguments=second_input, terminal_ref=ref,
        )
        one = json.loads(base64.urlsafe_b64decode(first["body"] + "=="))
        two = json.loads(base64.urlsafe_b64decode(second["body"] + "=="))
        self.assertNotEqual(one["binding"]["invocationId"], two["binding"]["invocationId"])
        self.assertNotEqual(one["nonce"], two["nonce"])
        self.assertNotEqual(one["invocation"]["inputDigest"], two["invocation"]["inputDigest"])
        with self.assertRaisesRegex(ProducerUnavailable, "OPERATION_MISMATCH"):
            producer.issue(
                service=service, project_id="project-1", task_id="core-task-1",
                envelope_task_id=example["binding"]["taskId"], run_id=None, attempt_id=None,
                stage="bootstrap", operation_id="second-operation", tool="other-tool",
                arguments=second_input, terminal_ref=ref,
            )

    def test_current_revision_and_stored_task_run_steward_lineage_are_required(self) -> None:
        for example in FIXTURE["validCases"]:
            with self.subTest(example=example["id"], mismatch="spec"):
                service, connection, ref = _service(example)
                producer = _producer(example, Ed25519PrivateKey.generate())
                connection.execute("UPDATE execution_spec_revisions SET is_current=0")
                connection.execute("INSERT INTO execution_spec_revisions VALUES (?,?,?,?)",
                                   ("spec-2", "core-task-1", example["core"]["taskRevision"] + 1, 1))
                connection.commit()
                with self.assertRaisesRegex(ProducerUnavailable, "OPERATION_MISMATCH"):
                    _issue(example, producer, service, ref)
                connection.close()
            with self.subTest(example=example["id"], mismatch="goal"):
                service, connection, ref = _service(example)
                producer = _producer(example, Ed25519PrivateKey.generate())
                connection.execute("INSERT INTO goal_revisions VALUES (?,?)",
                                   ("goal-2", example["core"]["goalRevision"] + 1))
                connection.execute("UPDATE projects SET active_goal_revision_id='goal-2'")
                connection.commit()
                with self.assertRaisesRegex(ProducerUnavailable, "OPERATION_MISMATCH"):
                    _issue(example, producer, service, ref)
                connection.close()
            service, connection, ref = _service(example)
            producer = _producer(example, Ed25519PrivateKey.generate())
            with self.subTest(example=example["id"], mismatch="task"):
                with self.assertRaisesRegex(ProducerUnavailable, "TASK_BINDING_MISMATCH"):
                    producer.issue(
                        service=service, project_id="project-1", task_id="core-task-1",
                        envelope_task_id="cross-task", run_id=example["binding"]["runId"],
                        attempt_id=example["binding"]["attemptId"], stage=example["core"]["stage"],
                        operation_id=example["core"]["gateOperationKey"],
                        tool=example["invocation"]["tool"], arguments=example["invocation"]["input"],
                        terminal_ref=ref,
                    )
            if example["kind"] == "worker":
                with self.subTest(example=example["id"], mismatch="run"):
                    with self.assertRaisesRegex(ProducerUnavailable, "RUN_BINDING_MISMATCH"):
                        producer.issue(
                            service=service, project_id="project-1", task_id="core-task-1",
                            envelope_task_id=example["binding"]["taskId"], run_id="other-run",
                            attempt_id=example["binding"]["attemptId"], stage=example["core"]["stage"],
                            operation_id=example["core"]["gateOperationKey"],
                            tool=example["invocation"]["tool"], arguments=example["invocation"]["input"],
                            terminal_ref=ref,
                        )
            else:
                with self.subTest(example=example["id"], mismatch="steward"):
                    connection.execute("UPDATE history_events SET payload_json=? WHERE entity_id='review-1'", (
                        json.dumps({"kind": GATE_KIND, "result": {
                            "key": {"task_id": "other-task", "execution_spec_revision_id": "spec-1", "attempt_no": 1},
                            "step": "steward:bootstrap", "data": {"callId": ref["callId"]}}}),
                    ))
                    connection.commit()
                    with self.assertRaisesRegex(ProducerUnavailable, "STEWARD_BINDING_MISMATCH"):
                        _issue(example, producer, service, ref)
            connection.close()

    def test_issued_receipt_is_persisted_before_call_and_reused_after_restart(self) -> None:
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        key = Ed25519PrivateKey.generate()
        producer = _producer(example, key)
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "ledger.sqlite3"
            disk = sqlite3.connect(database)
            connection.backup(disk)
            disk.close()
            connection.close()
            for attempt in range(2):
                reopened = sqlite3.connect(database)
                reopened.row_factory = sqlite3.Row
                service = SimpleNamespace(ledger=_Ledger(reopened))
                restarted = producer if attempt == 0 else AGSObservationProducer(
                    private_key=key, installation_id=example["producer"]["installationId"],
                    key_id=example["producer"]["keyId"], instance_id=example["producer"]["instanceId"],
                    session_id=example["binding"]["sessionId"],
                    clock=lambda: datetime.fromisoformat(example["issuedAt"].replace("Z", "+00:00")),
                    nonce=lambda: "new-nonce", invocation_id=lambda: "new-invocation",
                )
                receipt = _issue(example, restarted, service, ref)
                self.assertEqual(1, reopened.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type='vm_observation.issued'"
                ).fetchone()[0])
                stored = json.loads(reopened.execute(
                    "SELECT payload_json FROM history_events WHERE event_type='vm_observation.issued'"
                ).fetchone()[0])
                self.assertEqual(receipt, stored["receipt"])
                if attempt == 0:
                    original = receipt
                else:
                    self.assertEqual(original, receipt)
                reopened.close()

    def test_only_explicit_no_effect_retry_gets_new_single_use_invocation(self) -> None:
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        now = [datetime.fromisoformat(example["issuedAt"].replace("Z", "+00:00"))]
        producer = AGSObservationProducer(
            private_key=Ed25519PrivateKey.generate(),
            installation_id=example["producer"]["installationId"],
            key_id=example["producer"]["keyId"], instance_id=example["producer"]["instanceId"],
            session_id=example["binding"]["sessionId"], clock=lambda: now[0],
        )
        first = _issue(example, producer, service, ref)
        first_body = json.loads(base64.urlsafe_b64decode(first["body"] + "=="))
        first_expected = {"binding": first_body["binding"], "hostId": "flowmarshal-engine",
                          "tool": example["invocation"]["tool"]}
        consumed = _node(first, example, producer, expected=first_expected)
        self.assertTrue(consumed["accepted"])
        now[0] += timedelta(minutes=2)
        self.assertEqual("expired", _node(first, example, producer, expected=first_expected,
                                           received_at=now[0].isoformat())["reason"])
        self.assertEqual(first, _issue(example, producer, service, ref))
        self.assertEqual(1, connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE event_type='vm_observation.issued'"
        ).fetchone()[0])
        operation_id = example["core"]["gateOperationKey"]
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
            "project-1", "core_operation", operation_id, "operation.no_effect", "{}", 7,
        ))
        # no_effect만으로는 새 발급하지 않는다. Core가 재시도 intent를 다시 기록해야 한다.
        self.assertEqual(first, _issue(example, producer, service, ref))
        original = json.loads(connection.execute(
            "SELECT payload_json FROM history_events WHERE event_type='operation.prepared' AND entity_id=?",
            (operation_id,),
        ).fetchone()[0])
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
            "project-1", "core_operation", operation_id, "operation.prepared", json.dumps(original), 8,
        ))
        connection.commit()
        second = _issue(example, producer, service, ref)
        second_body = json.loads(base64.urlsafe_b64decode(second["body"] + "=="))
        self.assertNotEqual(first_body["binding"]["invocationId"], second_body["binding"]["invocationId"])
        self.assertNotEqual(first_body["nonce"], second_body["nonce"])
        self.assertEqual(2, connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE event_type='vm_observation.issued'"
        ).fetchone()[0])
        expected = {"binding": second_body["binding"], "hostId": "flowmarshal-engine",
                    "tool": example["invocation"]["tool"]}
        self.assertTrue(_node(second, example, producer, expected=expected,
                              seen=[consumed["claim"]], received_at=second_body["issuedAt"])["accepted"])

    def test_expiry_replay_revocation_and_missing_provenance_fail_closed(self) -> None:
        example = FIXTURE["validCases"][1]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        producer = _producer(example, Ed25519PrivateKey.generate())
        receipt = _issue(example, producer, service, ref)
        claim = _node(receipt, example, producer)["claim"]
        self.assertEqual("already_consumed", _node(receipt, example, producer, seen=[claim])["reason"])
        with tempfile.TemporaryDirectory() as claim_dir:
            self.assertTrue(_node(receipt, example, producer, claim_dir=claim_dir)["accepted"])
            self.assertEqual("already_consumed",
                             _node(receipt, example, producer, claim_dir=claim_dir)["reason"])
        self.assertEqual("expired", _node(receipt, example, producer,
                                           received_at=example["expiresAt"])["reason"])
        self.assertEqual("expired", _node(receipt, example, producer,
                                           received_at="2026-09-23T00:00:00.000Z")["reason"])
        self.assertEqual("key_not_trusted", _node(receipt, example, producer,
                                                 trust={producer.key_id: {"revoked": True}})["reason"])
        usage = json.loads(connection.execute("SELECT payload_json FROM budget_usage").fetchone()[0])
        usage["binding_provenance"]["observed"] = "client_requested"
        connection.execute("UPDATE budget_usage SET payload_json=?", (json.dumps(usage),))
        connection.commit()
        with self.assertRaises(ProducerUnavailable):
            _issue(example, producer, service, ref)
        usage["binding_provenance"]["observed"] = example["terminal"]["provenance"]
        connection.execute("UPDATE budget_usage SET payload_json=?", (json.dumps(usage),))
        connection.execute("UPDATE runtime_job_observations SET terminal_status='active'")
        connection.commit()
        with self.assertRaises(ProducerUnavailable):
            _issue(example, producer, service, ref)
        connection.execute("UPDATE task_contracts SET status='blocked'")
        connection.commit()
        with self.assertRaisesRegex(ProducerUnavailable, "CORE_STALE"):
            _issue(example, producer, service, ref)

    def test_caller_json_cannot_become_a_signed_receipt(self) -> None:
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        producer = _producer(example, Ed25519PrivateKey.generate())
        receipt = _issue(example, producer, service, ref)
        forged = {**receipt, "signature": base64.urlsafe_b64encode(b"caller-json").decode().rstrip("=")}
        self.assertEqual("signature_invalid", _node(forged, example, producer)["reason"])
        connection.execute("UPDATE budget_usage SET logical_call_ref='other'")
        connection.commit()
        with self.assertRaises(ProducerUnavailable):
            _issue(example, producer, service, ref)


if __name__ == "__main__":
    unittest.main()
