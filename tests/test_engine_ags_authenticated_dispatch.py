"""VM Core 예약 ID와 실제 stdio tools/call ID의 결속 검사."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flowmarshal.engine.ags_invocation_transport import (
    AuthenticatedDispatchTransport, DispatchUnavailable,
)
from flowmarshal.engine.ags_observation_producer import AGSObservationProducer, ProducerUnavailable, _canonical
from flowmarshal.engine.governance_gate import GovernanceContractMismatch, GovernancePlugin, McpStdioClient
from test_engine_ags_observation_producer import FIXTURE, _Ledger, _service


SERVER = r'''
import base64, hashlib, json, secrets, sys
from datetime import datetime, timezone
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_der_public_key
epoch = secrets.token_urlsafe(32)
pending = {}
seen_nonces = set()
def decode(signed):
    raw = base64.urlsafe_b64decode(signed["body"] + "==")
    return raw, json.loads(raw)
def live(body):
    now = datetime.now(timezone.utc)
    issued = datetime.fromisoformat(body["issuedAt"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(body["expiresAt"].replace("Z", "+00:00"))
    return issued <= now < expires and (expires - issued).total_seconds() <= 60
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method, params, rid = request["method"], request.get("params", {}), request["id"]
    if method == "initialize":
        result = {"serverInfo": {"name": "test-ags"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "plan_workflow"}, {"name": "record_stage_result"}]}
    elif method == "vm/hello":
        result = {"serverEpoch": epoch}
    elif method == "vm/reserve_dispatch":
        registration = params["registration"]
        try:
            raw, body = decode(registration)
            pin = json.loads(Path("pin.json").read_text(encoding="utf-8"))
            key = load_der_public_key(base64.b64decode(pin["spki"]))
            key.verify(base64.urlsafe_b64decode(registration["signature"] + "=="), raw)
            valid = (registration["keyId"] == pin["keyId"]
                     and pin.get("revoked") is not True
                     and body["producer"]["keyId"] == pin["keyId"]
                     and body["producer"]["installationId"] == pin["installationId"]
                     and body["producer"]["hostId"] == pin["hostId"]
                     and body["domain"] == "ags-vm-dispatch-registration-v1"
                     and body["serverEpoch"] == epoch and body["nonce"] not in seen_nonces
                     and live(body))
        except Exception:
            valid = False
        if not valid:
            result = {"callId": "", "serverEpoch": epoch}
        else:
            seen_nonces.add(body["nonce"])
            call_id = "vm-call-" + secrets.token_hex(12)
            pending[call_id] = (registration, pin["spki"])
            result = {"callId": call_id, "serverEpoch": epoch}
    elif method == "tools/call":
        arguments = params["arguments"]
        receipt = arguments.get("_hostAttestation", {})
        try:
            raw, body = decode(receipt)
            registration, spki = pending[rid]
            load_der_public_key(base64.b64decode(spki)).verify(
                base64.urlsafe_b64decode(receipt["signature"] + "=="), raw)
            reg_raw, reg_body = decode(registration)
            accepted = (body["binding"]["invocationId"] == rid
                        and body["transport"]["serverEpoch"] == epoch
                        and body["transport"]["registrationDigest"] == "sha256:" + hashlib.sha256(reg_raw).hexdigest()
                        and body["terminal"] == reg_body["terminal"]
                        and body["invocation"]["inputDigest"] == reg_body["invocation"]["inputDigest"]
                        and body["invocation"]["tool"] == params["name"] and live(body))
        except Exception:
            accepted, registration = False, None
        result = {"accepted": accepted, "actualCallId": rid,
                  "registration": registration if accepted else None}
        if accepted:
            del pending[rid]
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid,
                          "error": {"code": -32601, "message": "unsupported"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
'''


def _decode(signed: dict[str, str]) -> dict:
    return json.loads(base64.urlsafe_b64decode(signed["body"] + "=="))


def _signed_registration(producer: AGSObservationProducer, receipt: dict[str, str],
                         epoch: str, nonce: str, **changes) -> dict[str, str]:
    body = _decode(receipt)
    registration = {"version": 1, "domain": "ags-vm-dispatch-registration-v1",
                    "serverEpoch": epoch, "nonce": nonce,
                    "issuedAt": body["issuedAt"], "expiresAt": body["expiresAt"],
                    "producer": body["producer"],
                    "binding": {k: v for k, v in body["binding"].items() if k != "invocationId"},
                    "terminal": body["terminal"], "core": body["core"],
                    "invocation": body["invocation"], **changes}
    raw = _canonical(registration).encode("utf-8")
    encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
    return {"body": encode(raw), "signature": encode(producer._key.sign(raw)),
            "keyId": producer.key_id}


class AuthenticatedDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        server = root / "server.py"
        server.write_text(SERVER, encoding="utf-8")
        self.client = McpStdioClient([sys.executable, str(server)], cwd=root,
                                    env=dict(os.environ), stderr_path=root / "stderr.log")
        self.addCleanup(self.client.close)
        self.transport = AuthenticatedDispatchTransport(self.client)

    def _issue(self, example: dict):
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        key = Ed25519PrivateKey.generate()
        producer = AGSObservationProducer(
            private_key=key, installation_id=example["producer"]["installationId"],
            key_id=example["producer"]["keyId"], instance_id=example["producer"]["instanceId"],
            session_id=example["binding"]["sessionId"],
            clock=lambda: datetime.now(timezone.utc),
        )
        (Path(self.folder.name) / "pin.json").write_text(json.dumps({
            "keyId": producer.key_id, "installationId": producer.installation_id,
            "hostId": "flowmarshal-engine",
            "spki": base64.b64encode(producer.public_key_spki()).decode("ascii"),
        }), encoding="utf-8")
        kwargs = dict(service=service, project_id="project-1", task_id="core-task-1",
                      envelope_task_id=example["binding"]["taskId"],
                      run_id=example["binding"]["runId"], attempt_id=example["binding"]["attemptId"],
                      stage=example["core"]["stage"], operation_id=example["core"]["gateOperationKey"],
                      tool=example["invocation"]["tool"], arguments=example["invocation"]["input"],
                      terminal_ref=ref, transport=self.transport)
        return producer, kwargs

    def test_actual_stdio_call_id_matches_signed_registration_and_receipt(self) -> None:
        for example in FIXTURE["validCases"]:
            with self.subTest(example=example["id"]):
                producer, kwargs = self._issue(example)
                receipt, ticket = producer.issue(**kwargs)
                result = self.transport.call(ticket, receipt, kwargs["tool"], kwargs["arguments"])
                self.assertTrue(result["accepted"])
                self.assertEqual(ticket.call_id, result["actualCallId"])
                body, registration = _decode(receipt), result["registration"]
                reg_body = _decode(registration)
                self.assertEqual(ticket.call_id, body["binding"]["invocationId"])
                self.assertEqual(body["binding"]["attemptId"], example["binding"]["attemptId"])
                self.assertEqual(reg_body["binding"], {k: v for k, v in body["binding"].items()
                                                       if k != "invocationId"})
                self.assertEqual(reg_body["terminal"], body["terminal"])
                self.assertEqual(reg_body["invocation"], body["invocation"])
                reg_bytes = base64.urlsafe_b64decode(registration["body"] + "==")
                self.assertEqual("sha256:" + hashlib.sha256(reg_bytes).hexdigest(),
                                 body["transport"]["registrationDigest"])
                for signed in (receipt, registration):
                    data = base64.urlsafe_b64decode(signed["body"] + "==")
                    signature = base64.urlsafe_b64decode(signed["signature"] + "==")
                    producer._key.public_key().verify(signature, data)
                with self.assertRaises(DispatchUnavailable):
                    self.transport.call(ticket, receipt, kwargs["tool"], kwargs["arguments"])

    def test_changed_tool_input_cannot_use_reserved_call(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        receipt, ticket = producer.issue(**kwargs)
        with self.assertRaises(DispatchUnavailable):
            self.transport.call(ticket, receipt, kwargs["tool"], {**kwargs["arguments"], "tampered": True})
        with self.assertRaises(DispatchUnavailable):
            self.transport.call(ticket, receipt, kwargs["tool"], kwargs["arguments"])

    def test_no_effect_requires_new_prepared_intent_before_reissue(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        receipt_a, ticket_a = producer.issue(**kwargs)
        connection = kwargs["service"].ledger.connection
        operation_id = kwargs["operation_id"]
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)",
                           ("project-1", "core_operation", operation_id, "operation.no_effect", "{}", 7))
        connection.commit()
        with self.assertRaises(ProducerUnavailable):
            producer.issue(**kwargs)
        original = connection.execute("SELECT payload_json FROM history_events "
                                      "WHERE event_type='operation.prepared' AND entity_id=?",
                                      (operation_id,)).fetchone()[0]
        connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)",
                           ("project-1", "core_operation", operation_id, "operation.prepared", original, 8))
        connection.commit()
        receipt_b, ticket_b = producer.issue(**kwargs)
        self.assertNotEqual(ticket_a.call_id, ticket_b.call_id)
        self.assertNotEqual(_decode(receipt_a)["nonce"], _decode(receipt_b)["nonce"])
        self.assertTrue(self.transport.call(ticket_b, receipt_b, kwargs["tool"], kwargs["arguments"])["accepted"])

    def test_registration_authentication_failure_does_not_issue_context(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        pin_path = Path(self.folder.name) / "pin.json"
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
        pin_path.write_text(json.dumps({**pin, "revoked": True}), encoding="utf-8")
        with self.assertRaises(DispatchUnavailable):
            producer.issue(**kwargs)
        self.assertEqual(0, kwargs["service"].ledger.connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE event_type='vm_observation.issued'"
        ).fetchone()[0])
        pin_path.write_text(json.dumps(pin), encoding="utf-8")
        receipt, ticket = producer.issue(**kwargs)
        self.assertTrue(self.transport.call(ticket, receipt, kwargs["tool"], kwargs["arguments"])["accepted"])

    def test_invalid_signature_epoch_and_ttl_reject_reservation(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        receipt, _ = producer.issue(**kwargs)
        valid = _signed_registration(producer, receipt, self.transport.epoch, "forged-signature")
        invalid = [
            {**valid, "signature": "AA"},
            _signed_registration(producer, receipt, "wrong-epoch", "wrong-epoch"),
            _signed_registration(producer, receipt, self.transport.epoch, "expired",
                                 expiresAt="2000-01-01T00:00:00.000Z"),
        ]
        for registration in invalid:
            with self.subTest(body=_decode(registration).get("nonce")):
                with self.assertRaises(DispatchUnavailable):
                    self.transport.reserve(registration)

    def test_same_input_other_call_id_rejects_a_receipt_and_preserves_a(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        receipt_a, ticket_a = producer.issue(**kwargs)
        signed_b = _signed_registration(producer, receipt_a, self.transport.epoch, "b-nonce")
        ticket_b = self.transport.reserve(signed_b)
        with self.assertRaises(DispatchUnavailable):
            self.transport.reserve(signed_b)
        wrong = self.client.call_reserved(ticket_b.call_id, kwargs["tool"],
                                          {**kwargs["arguments"], "_hostAttestation": receipt_a})
        self.assertFalse(wrong["accepted"])
        self.assertTrue(self.transport.call(ticket_a, receipt_a, kwargs["tool"], kwargs["arguments"])["accepted"])
        with self.assertRaises(ProducerUnavailable):
            producer.issue(**kwargs)  # 결과 미상인 동일 operation에는 새 예약을 발급하지 않는다.

    def test_baseline_requires_null_attempt_before_worker_attempt_exists(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        connection = kwargs["service"].ledger.connection
        prepared = json.loads(connection.execute(
            "SELECT payload_json FROM history_events WHERE event_type='operation.prepared'"
        ).fetchone()[0])
        prepared["request"]["step"] = "record:baseline"
        prepared["request"]["arguments"] = {"runId": "baseline-run"}
        connection.execute("UPDATE history_events SET payload_json=? WHERE event_type='operation.prepared'",
                           (json.dumps(prepared),))
        key = prepared["request"]["key"]
        plan_request = {"request": {"key": key, "step": "plan", "arguments": {
            "taskEnvelope": {"taskId": kwargs["envelope_task_id"]}}}}
        plan = {"kind": "governance_gate", "result": {"key": key, "step": "plan", "data": {}}}
        started = {"kind": "governance_gate", "result": {"key": key, "step": "start", "data": {
            "raw": {"content": [{"text": json.dumps({"data": {"runId": "baseline-run"}})}]}}}}
        review = {"kind": "governance_gate", "result": {"key": key, "step": "steward:baseline",
                                                       "data": {"callId": kwargs["terminal_ref"]["callId"]}}}
        for index, (entity, event, payload) in enumerate((
            ("plan", "operation.prepared", plan_request),
            ("plan", "operation.completed", plan),
            ("start", "operation.completed", started),
            ("review", "operation.completed", review),
        ), 10):
            connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)",
                               ("project-1", "core_operation", entity, event, json.dumps(payload), index))
        connection.commit()
        kwargs.update(run_id="baseline-run", attempt_id=None, stage="baseline",
                      tool="record_stage_result", arguments={"runId": "baseline-run"})
        connection.execute("UPDATE history_events SET payload_json=? WHERE event_type='operation.prepared' "
                           "AND entity_id=?", (json.dumps({"request": {**prepared["request"],
                               "tool": kwargs["tool"], "arguments": kwargs["arguments"]}}),
                               kwargs["operation_id"]))
        connection.commit()
        receipt, ticket = producer.issue(**kwargs)
        self.assertIsNone(_decode(receipt)["binding"]["attemptId"])
        self.assertTrue(self.transport.call(ticket, receipt, kwargs["tool"], kwargs["arguments"])["accepted"])
        connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)",
                           ("new-attempt", "core-task-1", "project-1", "execution", "succeeded", 1))
        connection.commit()
        with self.assertRaises(ProducerUnavailable):
            producer.issue(**kwargs)

    def test_stage_attempt_contract_fails_before_any_registration(self) -> None:
        for example, stage, attempt in (
            (FIXTURE["validCases"][0], "bootstrap", "forged-attempt"),
            (FIXTURE["validCases"][0], "baseline", "forged-attempt"),
            (FIXTURE["validCases"][1], "implementation", None),
            (FIXTURE["validCases"][1], "scope", None),
            (FIXTURE["validCases"][1], "acceptance", None),
        ):
            with self.subTest(stage=stage):
                producer, kwargs = self._issue(example)
                with self.assertRaisesRegex(ProducerUnavailable, "STAGE_ATTEMPT_MISMATCH"):
                    producer.issue(**{**kwargs, "stage": stage, "attempt_id": attempt})
                self.assertEqual(0, kwargs["service"].ledger.connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type='vm_observation.issued'"
                ).fetchone()[0])

    def test_restart_rejects_old_receipt_and_caller_receipt_fallback(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        receipt, ticket = producer.issue(**kwargs)
        root = Path(self.folder.name)
        replacement = McpStdioClient([sys.executable, str(root / "server.py")], cwd=root,
                                    env=dict(os.environ), stderr_path=root / "restart.stderr.log")
        self.addCleanup(replacement.close)
        fresh = AuthenticatedDispatchTransport(replacement)
        self.assertNotEqual(ticket.epoch, fresh.epoch)
        with self.assertRaises(DispatchUnavailable):
            fresh.call(ticket, receipt, kwargs["tool"], kwargs["arguments"])
        plugin = GovernancePlugin(root, root / "state")
        plugin._client = lambda: self.client
        with self.assertRaises(GovernanceContractMismatch):
            plugin.call(kwargs["tool"], kwargs["arguments"], {"_vmProducerReceipt": receipt})

    def test_reopened_core_ledger_cannot_reissue_unknown_dispatch(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        receipt, ticket = producer.issue(**kwargs)
        root = Path(self.folder.name)
        database = root / "restarted-ledger.sqlite3"
        disk = sqlite3.connect(database)
        kwargs["service"].ledger.connection.backup(disk)
        disk.close()
        reopened = sqlite3.connect(database)
        reopened.row_factory = sqlite3.Row
        self.addCleanup(reopened.close)
        restarted_service = SimpleNamespace(ledger=_Ledger(reopened))
        replacement = McpStdioClient([sys.executable, str(root / "server.py")], cwd=root,
                                    env=dict(os.environ), stderr_path=root / "reopened.stderr.log")
        self.addCleanup(replacement.close)
        fresh = AuthenticatedDispatchTransport(replacement)
        self.assertNotEqual(ticket.epoch, fresh.epoch)
        restarted_producer = AGSObservationProducer(
            private_key=producer._key, installation_id=producer.installation_id,
            key_id=producer.key_id, instance_id=producer.instance_id,
            session_id=producer.session_id)
        with self.assertRaisesRegex(ProducerUnavailable, "OUTCOME_UNKNOWN"):
            restarted_producer.issue(**{**kwargs, "service": restarted_service, "transport": fresh})
        self.assertEqual(1, reopened.execute(
            "SELECT COUNT(*) FROM history_events WHERE event_type='vm_observation.issued'"
        ).fetchone()[0])
        self.assertEqual(receipt, json.loads(reopened.execute(
            "SELECT payload_json FROM history_events WHERE event_type='vm_observation.issued'"
        ).fetchone()[0])["receipt"])

    def test_concurrent_plugin_access_reuses_one_transport(self) -> None:
        root = Path(self.folder.name)
        plugin = GovernancePlugin(root, root / "concurrent-state")
        plugin._client = lambda: self.client
        with ThreadPoolExecutor(max_workers=8) as pool:
            transports = list(pool.map(lambda _: plugin.authenticated_dispatch(), range(32)))
        self.assertTrue(all(item is transports[0] for item in transports))

    def test_concurrent_reserve_and_call_keep_each_signed_call_id(self) -> None:
        example = FIXTURE["validCases"][0]
        producer, kwargs = self._issue(example)
        first_receipt, first_ticket = producer.issue(**kwargs)
        registrations = [_signed_registration(producer, first_receipt, self.transport.epoch,
                                              f"parallel-{index}") for index in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(self.transport.reserve, registrations))
        self.assertEqual(8, len({ticket.call_id for ticket in tickets}))

        def dispatch(pair):
            registration, ticket = pair
            body = _decode(first_receipt)
            body["binding"]["invocationId"] = ticket.call_id
            reg_raw = base64.urlsafe_b64decode(registration["body"] + "==")
            body["transport"]["registrationDigest"] = "sha256:" + hashlib.sha256(reg_raw).hexdigest()
            body["nonce"] = "receipt-" + ticket.call_id
            raw = _canonical(body).encode("utf-8")
            encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
            receipt = {"body": encode(raw), "signature": encode(producer._key.sign(raw)),
                       "keyId": producer.key_id}
            return self.transport.call(ticket, receipt, kwargs["tool"], kwargs["arguments"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(dispatch, zip(registrations, tickets)))
        self.assertTrue(all(result["accepted"] for result in results))
        self.assertEqual({ticket.call_id for ticket in tickets},
                         {result["actualCallId"] for result in results})
        def replay_first(_):
            try:
                return self.transport.call(first_ticket, first_receipt,
                                           kwargs["tool"], kwargs["arguments"])["accepted"]
            except DispatchUnavailable:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([False, True], sorted(pool.map(replay_first, range(2))))

    def test_unsupported_control_rpc_fails_closed(self) -> None:
        class Unsupported:
            def _request(self, method, params):
                raise GovernanceContractMismatch("mcp_protocol:" + method, "result", "unsupported")

        transport = AuthenticatedDispatchTransport(Unsupported())
        with self.assertRaises(GovernanceContractMismatch):
            transport.reserve({"body": "x", "signature": "y", "keyId": "z"})


if __name__ == "__main__":
    unittest.main()
