"""VM Core의 완료 provider turn을 뒤따르는 AGS 호출에 결속해 서명한다.

서명 키는 Engine을 띄우는 신뢰 경계가 객체로 주입한다. CLI, 환경 변수,
workspace 파일이나 MCP 인자는 키를 만들거나 선택할 수 없다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ..canonical import sha256_digest
from .model_observation import authoritative_receipt_model_observation


class ProducerUnavailable(ValueError):
    """출처 또는 현재 Core 결속을 증명하지 못해 receipt를 발급하지 않는다."""


_HOST_ID = "flowmarshal-engine"
_DOMAIN = "vm-provider-terminal-to-governance"
_TERMINAL_STATUSES = frozenset({"succeeded", "completed"})
_SAFE_INTEGER = 2**53 - 1


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProducerUnavailable(f"VM_PRODUCER_{name.upper()}_MISSING")
    return value


def _time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as error:
        raise ProducerUnavailable("VM_PRODUCER_TIME_INVALID") from error
    if result.tzinfo is None:
        raise ProducerUnavailable("VM_PRODUCER_TIME_INVALID")
    return result.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    """AGS canonicalJson과 같은 허용 JSON subset. 미지원 숫자·값은 닫는다."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _SAFE_INTEGER:
        return str(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProducerUnavailable("VM_PRODUCER_STRING_INVALID") from error
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_canonical(key) + ":" + _canonical(value[key]) for key in keys) + "}"
    raise ProducerUnavailable("VM_PRODUCER_CANONICAL_INPUT_UNSUPPORTED")


def _terminal(fields: dict[str, Any]) -> dict[str, Any]:
    if fields["status"] not in _TERMINAL_STATUSES:
        raise ProducerUnavailable("VM_PRODUCER_TERMINAL_INCOMPLETE")
    for name in ("eventId", "callId", "threadId", "turnId", "model", "effort", "provenance"):
        _required(fields.get(name), name)
    observed_at = _required(fields.get("observedAt"), "observedAt")
    _time(observed_at)
    return {**fields, "digest": sha256_digest(fields)}


def _steward_terminal(connection: Any, project_id: str, call_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT u.id,u.payload_json,c.receipt_json FROM budget_usage u "
        "JOIN provider_calls c ON c.usage_id=u.id "
        "WHERE u.project_id=? AND u.stage='validation' AND u.logical_call_ref=?",
        (project_id, call_id),
    ).fetchall()
    if len(rows) != 1:
        raise ProducerUnavailable("VM_PRODUCER_STEWARD_EVENT_MISSING")
    row = rows[0]
    usage, receipt = json.loads(row["payload_json"]), json.loads(row["receipt_json"])
    model, effort = authoritative_receipt_model_observation(
        observed_model=usage.get("observed_model"),
        observed_effort=usage.get("observed_effort"),
        binding_provenance=usage.get("binding_provenance"),
    )
    turns = usage.get("turn_ids")
    if (
        model is None or effort is None or not isinstance(turns, list) or len(turns) != 1
        or usage.get("call_status") != "succeeded"
        or usage.get("runner_receipt_digest") != sha256_digest(receipt)
        or receipt.get("call_id") != call_id or receipt.get("turn_ids") != turns
        or receipt.get("thread_id") != usage.get("thread_id")
        or receipt.get("status") != "succeeded"
        or receipt.get("observed_model") != model
        or receipt.get("observed_effort") != effort
        or (receipt.get("binding_provenance") or {}).get("observed")
           != usage["binding_provenance"]["observed"]
    ):
        raise ProducerUnavailable("VM_PRODUCER_STEWARD_EVENT_MISMATCH")
    return _terminal({
        "eventId": row["id"], "callId": call_id, "threadId": usage["thread_id"],
        "turnId": turns[0], "status": "succeeded", "observedAt": usage["recorded_at"],
        "model": model, "effort": effort, "provenance": usage["binding_provenance"]["observed"],
    })


def _worker_terminal(connection: Any, project_id: str, attempt_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT o.id,o.payload_json,o.observed_at,o.terminal_status,j.thread_id,j.turn_id "
        "FROM runtime_job_observations o JOIN runtime_jobs j ON j.id=o.job_id "
        "WHERE o.project_id=? AND j.attempt_id=? AND o.provider_terminal=1 "
        "ORDER BY o.rowid DESC",
        (project_id, attempt_id),
    ).fetchall()
    usage_rows = connection.execute(
        "SELECT payload_json FROM budget_usage WHERE project_id=? AND stage='execution' "
        "AND json_extract(payload_json,'$.attempt_id')=? ORDER BY rowid DESC",
        (project_id, attempt_id),
    ).fetchall()
    if len(rows) != 1 or len(usage_rows) != 1:
        raise ProducerUnavailable("VM_PRODUCER_WORKER_EVENT_MISSING")
    row, usage = rows[0], json.loads(usage_rows[0]["payload_json"])
    result = json.loads(row["payload_json"]).get("result")
    stored_observation = usage.get("provider_observation")
    original_payload = result.get("payload") if isinstance(result, dict) else None
    stored_payload = (stored_observation.get("payload")
                      if isinstance(stored_observation, dict) else None)
    raw_core_payload = ({key: value for key, value in original_payload.items()
                         if key not in {"operation_trace", "operation_trace_ref", "operation_trace_digest"}}
                        if isinstance(original_payload, dict) else None)
    core_payload = ({key: value for key, value in stored_payload.items()
                     if key not in {"operation_trace", "operation_trace_ref", "operation_trace_digest"}}
                    if isinstance(stored_payload, dict) else None)
    model, effort = authoritative_receipt_model_observation(
        observed_model=usage.get("observed_model"),
        observed_effort=usage.get("observed_effort"),
        binding_provenance=usage.get("binding_provenance"),
    )
    if (
        not isinstance(result, dict) or not isinstance(stored_observation, dict)
        or model is None or effort is None
        or row["terminal_status"] != "completed"
        or result.get("active") is not False
        or result.get("terminal_status") != row["terminal_status"]
        or result.get("thread_id") != row["thread_id"]
        or result.get("turn_id") != row["turn_id"]
        or usage.get("thread_id") != row["thread_id"]
        or usage.get("turn_ids") != [row["turn_id"]]
        or usage.get("call_status") != row["terminal_status"]
        or stored_observation.get("thread_id") != row["thread_id"]
        or stored_observation.get("turn_id") != row["turn_id"]
        or stored_observation.get("terminal_status") != row["terminal_status"]
        or raw_core_payload is None or core_payload != raw_core_payload
        or usage.get("runner_receipt_digest") != sha256_digest(stored_observation)
        or original_payload.get("observed_model") != model
        or original_payload.get("observed_effort") != effort
        or original_payload.get("model_observation_source")
           != usage["binding_provenance"]["observed"]
        or not usage.get("runtime_receipt_id")
    ):
        raise ProducerUnavailable("VM_PRODUCER_WORKER_EVENT_MISMATCH")
    return _terminal({
        "eventId": row["id"], "callId": usage["runtime_receipt_id"],
        "threadId": row["thread_id"], "turnId": row["turn_id"],
        "status": row["terminal_status"], "observedAt": row["observed_at"],
        "model": model, "effort": effort, "provenance": usage["binding_provenance"]["observed"],
    })


class AGSObservationProducer:
    """신뢰된 Engine bootstrap이 만든 signer. 미설정이면 gate는 기존 unsupported 경로다."""

    def __init__(
        self, *, private_key: Ed25519PrivateKey, installation_id: str, key_id: str,
        instance_id: str, session_id: str,
        clock: Callable[[], datetime] | None = None,
        nonce: Callable[[], str] | None = None,
        invocation_id: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ProducerUnavailable("VM_PRODUCER_KEY_UNAVAILABLE")
        self._key = private_key
        self.installation_id = _required(installation_id, "installationId")
        self.key_id = _required(key_id, "keyId")
        self.instance_id = _required(instance_id, "instanceId")
        self.session_id = _required(session_id, "sessionId")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._nonce = nonce or (lambda: secrets.token_urlsafe(24))
        self._invocation_id = invocation_id or (lambda: "vm-invocation-" + secrets.token_hex(16))

    def public_key_spki(self) -> bytes:
        """운영자 pin 절차에 전달할 공개키 DER. private key bytes는 내보내지 않는다."""
        return self._key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)

    def issue(
        self, *, service: Any, project_id: str, task_id: str, envelope_task_id: str,
        run_id: str | None, attempt_id: str | None, stage: str, operation_id: str,
        tool: str, arguments: dict[str, Any], terminal_ref: dict[str, str],
        transport: Any | None = None,
    ) -> Any:
        """Core prepared operation와 완료 terminal을 다시 읽은 직후 새 invocation을 서명한다."""
        if ((stage in {"bootstrap", "baseline"} and attempt_id is not None)
                or (stage in {"implementation", "scope", "acceptance"} and attempt_id is None)
                or stage not in {"bootstrap", "baseline", "implementation", "scope", "acceptance"}):
            raise ProducerUnavailable("VM_PRODUCER_STAGE_ATTEMPT_MISMATCH")
        with service.ledger.transaction() as tx:
            connection = tx.connection
            core = connection.execute(
                "SELECT g.id AS goal_revision_id,g.revision_no AS goal_revision,"
                "s.id AS spec_revision_id,s.revision_no AS task_revision,"
                "t.project_id,t.plan_revision_id,t.status,p.active_plan_revision_id "
                "FROM task_contracts t JOIN projects p ON p.id=t.project_id "
                "JOIN goal_revisions g ON g.id=p.active_goal_revision_id "
                "JOIN execution_spec_revisions s ON s.task_id=t.id AND s.is_current=1 "
                "WHERE t.id=?",
                (task_id,),
            ).fetchone()
            prepared = connection.execute(
                "SELECT payload_json,sequence FROM history_events WHERE project_id=? "
                "AND entity_type='core_operation' AND entity_id=? "
                "AND event_type='operation.prepared' ORDER BY sequence DESC LIMIT 1",
                (project_id, operation_id),
            ).fetchone()
            if (
                core is None or core["project_id"] != project_id
                or core["plan_revision_id"] != core["active_plan_revision_id"]
                or core["status"] not in {"ready", "materialized", "reserved", "running", "validating"}
                or prepared is None
            ):
                raise ProducerUnavailable("VM_PRODUCER_CORE_STALE")
            request = json.loads(prepared["payload_json"]).get("request", {})
            if (
                request.get("key", {}).get("task_id") != task_id
                or request["key"].get("execution_spec_revision_id") != core["spec_revision_id"]
                or request.get("goalRevisionId") != core["goal_revision_id"]
                or request.get("step") != ("plan" if stage == "bootstrap" else f"record:{stage}")
                or request.get("tool") != tool
                or request.get("arguments") != arguments
                or request.get("observation", {}).get("terminalRef") != terminal_ref
            ):
                raise ProducerUnavailable("VM_PRODUCER_OPERATION_MISMATCH")
            key = request["key"]
            lineage = connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id=? "
                "AND entity_type='core_operation' AND event_type='operation.completed' "
                "AND json_extract(payload_json,'$.kind')='governance_gate' ORDER BY sequence",
                (project_id,),
            ).fetchall()
            completed = [json.loads(row["payload_json"]).get("result", {}) for row in lineage]
            plan = [item for item in completed if item.get("step") == "plan" and item.get("key") == key]
            if stage == "bootstrap":
                planned_task_id = (arguments.get("taskEnvelope") or {}).get("taskId")
            else:
                if len(plan) != 1:
                    raise ProducerUnavailable("VM_PRODUCER_TASK_BINDING_MISMATCH")
                planned_task_id = None
                prior = connection.execute(
                    "SELECT payload_json FROM history_events WHERE project_id=? "
                    "AND entity_type='core_operation' AND event_type='operation.prepared' "
                    "AND json_extract(payload_json,'$.request.step')='plan' ORDER BY sequence DESC",
                    (project_id,),
                ).fetchall()
                for row in prior:
                    plan_request = json.loads(row["payload_json"]).get("request", {})
                    if plan_request.get("key") == key:
                        planned_task_id = ((plan_request.get("arguments") or {}).get("taskEnvelope") or {}).get("taskId")
                        break
            if planned_task_id != envelope_task_id:
                raise ProducerUnavailable("VM_PRODUCER_TASK_BINDING_MISMATCH")
            starts = [item for item in completed if item.get("step") == "start" and item.get("key") == key]
            if stage == "bootstrap":
                if run_id is not None or starts:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH")
            else:
                if len(starts) != 1:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH")
                raw = (starts[0].get("data") or {}).get("raw") or {}
                content = raw.get("content") if isinstance(raw, dict) else None
                try:
                    actual_run_id = json.loads(content[0]["text"])["data"]["runId"]
                except (IndexError, KeyError, TypeError, ValueError) as error:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH") from error
                if actual_run_id != run_id:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH")
            attempt_ordinal = None
            if attempt_id is not None:
                attempt = connection.execute(
                    "SELECT attempt_no,status FROM attempts WHERE id=? AND task_id=? "
                    "AND project_id=? AND kind='execution'",
                    (attempt_id, task_id, project_id),
                ).fetchone()
                latest = connection.execute(
                    "SELECT MAX(attempt_no) FROM attempts WHERE task_id=? AND kind='execution'",
                    (task_id,),
                ).fetchone()[0]
                if (attempt is None or attempt["status"] != "succeeded"
                        or attempt["attempt_no"] != latest
                        or request["key"].get("attempt_no") != attempt["attempt_no"]):
                    raise ProducerUnavailable("VM_PRODUCER_ATTEMPT_STALE")
                attempt_ordinal = attempt["attempt_no"]
            else:
                latest = connection.execute(
                    "SELECT COALESCE(MAX(attempt_no),0) FROM attempts WHERE task_id=? AND kind='execution'",
                    (task_id,),
                ).fetchone()[0]
                if key.get("attempt_no") != latest + 1:
                    raise ProducerUnavailable("VM_PRODUCER_ATTEMPT_STALE")
            if terminal_ref.get("kind") == "steward":
                reviews = [item for item in completed if item.get("step") == f"steward:{stage}"
                           and item.get("key") == key]
                if len(reviews) != 1 or (reviews[0].get("data") or {}).get("callId") != terminal_ref.get("callId"):
                    raise ProducerUnavailable("VM_PRODUCER_STEWARD_BINDING_MISMATCH")
                terminal = _steward_terminal(connection, project_id, _required(
                    terminal_ref.get("callId"), "callId"))
            elif terminal_ref.get("kind") == "worker" and attempt_id is not None:
                if terminal_ref.get("attemptId") != attempt_id:
                    raise ProducerUnavailable("VM_PRODUCER_TERMINAL_SOURCE_INVALID")
                terminal = _worker_terminal(connection, project_id, attempt_id)
            else:
                raise ProducerUnavailable("VM_PRODUCER_TERMINAL_SOURCE_INVALID")
            previous = connection.execute(
                "SELECT payload_json,sequence FROM history_events WHERE project_id=? "
                "AND entity_type='core_operation' AND entity_id=? "
                "AND event_type='vm_observation.issued' ORDER BY sequence DESC LIMIT 1",
                (project_id, operation_id),
            ).fetchone()
            if previous is not None:
                payload = json.loads(previous["payload_json"])
                if (payload.get("terminal") != terminal or payload.get("tool") != tool
                        or payload.get("argumentsDigest") != sha256_digest(arguments)):
                    raise ProducerUnavailable("VM_PRODUCER_ISSUED_BINDING_MISMATCH")
                no_effect = connection.execute(
                    "SELECT sequence FROM history_events WHERE project_id=? "
                    "AND entity_type='core_operation' AND entity_id=? "
                    "AND event_type='operation.no_effect' AND sequence>? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (project_id, operation_id, previous["sequence"]),
                ).fetchone()
                if no_effect is None or prepared["sequence"] <= no_effect["sequence"]:
                    if transport is not None:
                        raise ProducerUnavailable("VM_PRODUCER_OUTCOME_UNKNOWN")
                    return payload["receipt"]
            issued = self._clock()
            if issued.tzinfo is None or _time(terminal["observedAt"]) >= issued.astimezone(timezone.utc):
                raise ProducerUnavailable("VM_PRODUCER_CAUSAL_ORDER_INVALID")
            input_value = {key: value for key, value in arguments.items() if key != "_hostAttestation"}
            input_digest = "sha256:" + hashlib.sha256(_canonical(input_value).encode("utf-8")).hexdigest()
            timestamp = _utc(issued)
            encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
            producer = {"installationId": self.installation_id, "keyId": self.key_id,
                        "hostId": _HOST_ID, "instanceId": self.instance_id}
            binding = {"turnId": terminal["turnId"], "taskId": envelope_task_id,
                       "runId": run_id, "attemptId": attempt_id, "hostId": _HOST_ID,
                       "sessionId": self.session_id, "instanceId": self.instance_id}
            invocation = {"tool": _required(tool, "tool"), "inputDigest": input_digest,
                          "observedAt": timestamp}
            ticket = None
            transport_binding = None
            if transport is not None:
                registration_body = {
                    "version": 1, "domain": "ags-vm-dispatch-registration-v1",
                    "serverEpoch": transport.epoch, "nonce": _required(self._nonce(), "nonce"),
                    "issuedAt": timestamp, "expiresAt": _utc(issued + timedelta(seconds=60)),
                    "producer": producer, "binding": binding,
                    "terminal": terminal,
                    "core": {"goalRevision": core["goal_revision"], "taskRevision": core["task_revision"],
                             "attemptOrdinal": attempt_ordinal, "gateOperationKey": operation_id,
                             "stage": _required(stage, "stage")},
                    "invocation": invocation,
                }
                registration_data = _canonical(registration_body).encode("utf-8")
                ticket = transport.reserve({"body": encode(registration_data),
                                            "signature": encode(self._key.sign(registration_data)),
                                            "keyId": self.key_id})
                transport_binding = {"serverEpoch": ticket.epoch,
                                     "registrationDigest": "sha256:" + hashlib.sha256(registration_data).hexdigest()}
            body = {
                "version": 2 if ticket is not None else 1, "domain": _DOMAIN,
                "producer": producer,
                "binding": {"invocationId": ticket.call_id if ticket is not None else _required(
                    self._invocation_id(), "invocationId"), **binding},
                "terminal": terminal,
                "core": {"goalRevision": core["goal_revision"], "taskRevision": core["task_revision"],
                         "attemptOrdinal": attempt_ordinal, "gateOperationKey": operation_id,
                         "stage": _required(stage, "stage")},
                "invocation": invocation,
                "nonce": _required(self._nonce(), "nonce"), "issuedAt": timestamp,
                "expiresAt": _utc(issued + timedelta(seconds=60)),
            }
            if transport_binding is not None:
                body["transport"] = transport_binding
            data = _canonical(body).encode("utf-8")
            receipt = {"body": encode(data), "signature": encode(self._key.sign(data)), "keyId": self.key_id}
            tx.history(project_id, "vm_observation.issued", "core_operation", operation_id, {
                "receipt": receipt, "terminal": terminal, "tool": tool,
                "argumentsDigest": sha256_digest(arguments),
            })
            return (receipt, ticket) if ticket is not None else receipt
