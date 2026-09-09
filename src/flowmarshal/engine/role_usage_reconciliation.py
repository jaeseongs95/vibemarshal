"""역할 호출의 지연된 runtime 관측을 원본 receipt와 분리해 보존한다."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..canonical import canonical_json, sha256_digest
from .domain import PROVIDER_TERMINAL_STATUSES, BudgetStage, BudgetUsageRecord, new_id
from .roles import RoleCallReceipt
from .model_observation import authoritative_receipt_model_observation


# 기존 diagnostic import 계약을 유지하되 단일 공통 정의를 참조한다.
TERMINAL_STATUSES = PROVIDER_TERMINAL_STATUSES
_USAGE_KEYS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")


def _blocked(code: str, detail: str) -> Exception:
    from .budget import BudgetBlocked
    return BudgetBlocked(code, detail)


def observation_document(observation: Any) -> dict[str, Any]:
    if hasattr(observation, "model_dump"):
        return observation.model_dump(mode="json")
    return {
        "thread_id": observation.thread_id, "turn_id": observation.turn_id,
        "active": observation.active, "terminal_status": observation.terminal_status,
        "final_response": observation.final_response, "payload": observation.payload,
    }


def _usage_source(payload: dict[str, Any], receipt: RoleCallReceipt | None) -> dict[str, Any] | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    # 역할 호출 전체를 하나의 logical call로 정산하므로 recovery나 다중 turn의
    # 마지막 turn usage만으로 전체 비용을 확정하지 않는다.
    if receipt is not None and (len(receipt.turn_ids) != 1 or receipt.schema_recovery_attempts != 0):
        return None
    if payload.get("usage_scope") == "turn":
        return usage
    if payload.get("usage_scope") != "thread" or receipt is None:
        return None
    proofs = payload.get("role_call_proofs")
    if not isinstance(proofs, dict) or payload.get("role_call_proofs_digest") != sha256_digest(proofs):
        return None
    created = proofs.get("thread_creation_receipt")
    started = proofs.get("turn_start_receipt")
    if not isinstance(created, dict) or not isinstance(started, dict):
        return None
    thread_id, turn_id = receipt.thread_id, receipt.turn_ids[0]
    created_binding, created_payload = created.get("binding"), created.get("payload")
    started_binding, started_payload = started.get("binding"), started.get("payload")
    created_thread = created_payload.get("thread") if isinstance(created_payload, dict) else None
    if not (
        thread_id is not None
        and created.get("operation_id") == thread_id
        and isinstance(created_binding, dict)
        and created_binding.get("thread_id") == thread_id
        and created_binding.get("turn_id") is None
        and isinstance(created_thread, dict)
        and created_thread.get("id") == thread_id
        and created_thread.get("turns") == []
        and started.get("operation_id") == turn_id
        and isinstance(started_binding, dict)
        and (started_binding.get("thread_id"), started_binding.get("turn_id")) == (thread_id, turn_id)
        and isinstance(started_payload, dict)
        and (started_payload.get("thread_id"), started_payload.get("turn_id")) == (thread_id, turn_id)
        and started_payload.get("first_empty_thread") is True
    ):
        return None
    return usage.get("total") if isinstance(usage.get("total"), dict) else None


def observation_usage_values(
    payload: dict[str, Any], receipt: RoleCallReceipt | None = None,
) -> tuple[bool, tuple[int | None, ...]]:
    """turn usage 또는 원시 receipt로 증명된 빈 thread 첫 turn usage만 인정한다."""
    raw = _usage_source(payload, receipt)
    if raw is None:
        return False, (None,) * len(_USAGE_KEYS)
    values = tuple(
        value if type(value) is int and value >= 0 else None
        for value in (raw.get(key) for key in _USAGE_KEYS)
    )
    if values[0] is not None and values[1] is not None and values[1] > values[0]:
        values = (values[0], None, values[2], values[3])
    if values[2] is not None and values[3] is not None and values[3] > values[2]:
        values = (values[0], values[1], values[2], None)
    if (
        values[0] is not None and values[2] is not None and "totalTokens" in raw
        and not (
            type(raw["totalTokens"]) is int
            and raw["totalTokens"] == values[0] + values[2]
        )
    ):
        return False, (None,) * len(_USAGE_KEYS)
    return any(value is not None for value in values), values


def runtime_observation_events(tx: Any, call_id: str) -> tuple[dict[str, Any], ...]:
    """새 History 관측만 순서대로 읽는다. 구 reconciliation row는 별도로 유지된다."""
    rows = tx.all(
        "SELECT payload_json,created_at FROM history_events "
        "WHERE entity_type='provider_call' AND entity_id=? AND event_type='budget.call_observed' "
        "ORDER BY sequence", (call_id,),
    )
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("observation_kind") != "runtime_observation":
            continue
        document = payload.get("observation")
        if not isinstance(document, dict) or payload.get("observation_digest") != sha256_digest(document):
            raise _blocked("BUDGET_OBSERVATION_HISTORY_INVALID", "runtime 관측 원문과 digest가 다릅니다.")
        events.append({**payload, "history_created_at": row["created_at"]})
    return tuple(events)


def latest_runtime_observation(tx: Any, call_id: str) -> dict[str, Any] | None:
    events = runtime_observation_events(tx, call_id)
    return None if not events else events[-1]


def _validate_call_binding(tx: Any, call: Any, receipt: RoleCallReceipt, document: dict[str, Any]) -> None:
    from .roles import RoleCallRequest, strict_json_output_schema

    try:
        request_document = json.loads(call["request_json"])
    except json.JSONDecodeError as error:
        raise _blocked("BUDGET_REQUEST_BINDING_MISMATCH", "예약 요청 원문을 읽을 수 없습니다.") from error
    if sha256_digest(request_document) != call["request_digest"] or receipt.role != call["role"]:
        raise _blocked("BUDGET_RECEIPT_BINDING_MISMATCH", "예약 요청과 원본 역할 receipt가 다릅니다.")
    if "output_schema" in request_document and "instructions" in request_document:
        try:
            request = RoleCallRequest.model_validate(request_document)
        except Exception as error:
            raise _blocked("BUDGET_REQUEST_BINDING_MISMATCH", "예약 역할 요청을 검증할 수 없습니다.") from error
        request_observation_digest = getattr(request, "observation_policy_digest", None)
        receipt_observation_digest = getattr(receipt, "observation_policy_digest", None)
        if (
            receipt.input_digest != request.request_digest
            or (receipt.role, receipt.model, receipt.effort, receipt.inventory_digest)
            != (request.role, request.model, request.effort, request.inventory_digest)
            or receipt.output_schema_digest != sha256_digest(strict_json_output_schema(request.output_schema))
            or getattr(receipt, "timeout_policy_digest", None)
            != getattr(request, "timeout_policy_digest", None)
            or receipt_observation_digest != request_observation_digest
        ):
            raise _blocked("BUDGET_RECEIPT_BINDING_MISMATCH", "예약 역할 요청과 원본 receipt 결속이 다릅니다.")
    if call["goal_contract_digest"] is not None and tx.maybe_one(
        "SELECT id FROM goal_revisions WHERE project_id=? AND goal_id=? AND definition_digest=?",
        (call["project_id"], call["goal_id"], call["goal_contract_digest"]),
    ) is None:
        raise _blocked("BUDGET_RECONCILIATION_GOAL_BINDING_MISMATCH", "호출의 Goal 계보가 원장과 다릅니다.")
    if (not receipt.turn_ids or document.get("thread_id") != receipt.thread_id
            or document.get("turn_id") != receipt.turn_ids[-1]):
        raise _blocked("BUDGET_OBSERVATION_BINDING_MISMATCH", "원본 역할 호출과 thread/turn이 다릅니다.")


def usage_from_observation(
    *, call: Any, receipt: RoleCallReceipt, event: dict[str, Any], goal_digest: str
) -> BudgetUsageRecord:
    document = event["observation"]
    terminal = (
        not document.get("active", True)
        and document.get("terminal_status") in PROVIDER_TERMINAL_STATUSES
    )
    available, values = observation_usage_values(document.get("payload") or {}, receipt)
    available = terminal and available
    if not available:
        values = (None,) * len(_USAGE_KEYS)
    complete = all(value is not None for value in values)
    duration = (document.get("payload") or {}).get("duration_ms")
    latency = duration if type(duration) is int and duration >= 0 and terminal else None
    observed_scope = (document.get("payload") or {}).get("usage_scope")
    provenance = {}
    if receipt.binding_provenance_version == "2.0":
        observed_model, observed_effort = authoritative_receipt_model_observation(
            observed_model=receipt.observed_model,
            observed_effort=receipt.observed_effort,
            binding_provenance=receipt.binding_provenance,
        )
        provenance = {
            "binding_provenance_version": "2.0",
            "requested_model": receipt.requested_model,
            "requested_effort": receipt.requested_effort,
            "observed_model": observed_model,
            "observed_effort": observed_effort,
            "provider_inventory_digest": receipt.provider_inventory_digest,
            "adapter_capability_digest": receipt.adapter_capability_digest,
            "binding_provenance": {
                **(receipt.binding_provenance or {}),
                "observed": (
                    (receipt.binding_provenance or {}).get("observed")
                    if observed_model is not None and observed_effort is not None
                    else None
                ),
            },
        }
    return BudgetUsageRecord(
        usage_id=new_id("usage"), project_id=call["project_id"], goal_contract_digest=goal_digest,
        stage=BudgetStage(call["stage"]), logical_call_ref=receipt.call_id, role=receipt.role,
        call_status=(document.get("terminal_status") if terminal else "active"), model=receipt.model,
        effort=receipt.effort, permission_profile=receipt.permission_profile,
        approval_policy=receipt.approval_policy, thread_id=receipt.thread_id, turn_ids=receipt.turn_ids,
        input_digest=receipt.input_digest,
        output_digest=(sha256_digest(document["final_response"])
                       if document.get("final_response") is not None else receipt.output_digest),
        output_schema_digest=receipt.output_schema_digest,
        runner_receipt_digest=event["observation_digest"], input_tokens=values[0],
        cached_input_tokens=values[1], output_tokens=values[2], reasoning_tokens=values[3],
        latency_ms=latency, usage_available=available,
        usage_scope=observed_scope if available else "unavailable",
        usage_source="runtime.read.reconciliation",
        attribution_basis=("first_empty_thread" if available and observed_scope == "thread"
                           else "provider_turn" if available else "unavailable"),
        unavailable_reason=None if complete else (
            "PROVIDER_USAGE_PARTIAL" if available else
            "PROVIDER_USAGE_UNAVAILABLE_OR_UNATTRIBUTABLE" if terminal
            else "PROVIDER_TERMINAL_UNOBSERVED"
        ), provider_observation=document, retry_count=receipt.schema_recovery_attempts,
        recorded_at=datetime.fromisoformat(event["history_created_at"]),
        **provenance,
    )


def reconcile_role_usage_terminal(service: Any, call_id: str, observation: Any) -> BudgetUsageRecord | None:
    """pre/post-Goal 역할 호출의 재관측을 History에 먼저 기록하고 가능한 경우 정산한다."""
    document = observation_document(observation)
    observation_digest = sha256_digest(document)
    with service.ledger.transaction() as tx:
        call = tx.one("SELECT * FROM provider_calls WHERE id=?", (call_id,))
        if call["attempt_id"] is not None:
            raise _blocked("BUDGET_ROLE_RECONCILIATION_ATTEMPT_NOT_SUPPORTED", "역할 호출(attempt_id 없음)만 재관측할 수 있습니다.")
        if call["receipt_json"] is None:
            raise _blocked("BUDGET_ROLE_RECEIPT_REQUIRED", "thread/turn이 결속된 원본 receipt가 필요합니다.")
        receipt = RoleCallReceipt.model_validate_json(call["receipt_json"])
        _validate_call_binding(tx, call, receipt, document)

        events = runtime_observation_events(tx, call_id)
        same = next((item for item in events if item["observation_digest"] == observation_digest), None)
        if same is not None:
            if call["usage_id"] is None:
                return None
            row = tx.one("SELECT payload_json FROM budget_usage WHERE id=?", (call["usage_id"],))
            usage = BudgetUsageRecord.model_validate_json(row["payload_json"])
            if usage.runner_receipt_digest == observation_digest:
                return usage
            reconciliation = tx.maybe_one(
                "SELECT effective_usage_id FROM usage_reconciliations WHERE call_id=? AND observation_digest=?",
                (call_id, observation_digest),
            )
            if reconciliation is None:
                return None
            row = tx.one("SELECT payload_json FROM budget_usage WHERE id=?", (reconciliation["effective_usage_id"],))
            return BudgetUsageRecord.model_validate_json(row["payload_json"])

        terminal = (
            not document.get("active", True)
            and document.get("terminal_status") in PROVIDER_TERMINAL_STATUSES
        )
        available, values = observation_usage_values(document.get("payload") or {}, receipt)
        available = terminal and available
        if not available:
            values = (None,) * len(_USAGE_KEYS)
        complete = all(value is not None for value in values)
        complete_total = values[0] is not None and values[2] is not None
        prior_event_values: tuple[int | None, ...] | None = None
        for prior_event in reversed(events):
            if prior_event.get("usage_available") is not True:
                continue
            prior_available, candidate = observation_usage_values(
                (prior_event["observation"].get("payload") or {}), receipt,
            )
            if prior_available:
                prior_event_values = candidate
                break
        if prior_event_values is not None:
            for name, old, new in zip(_USAGE_KEYS, prior_event_values, values):
                if old is not None and new is not None and old != new:
                    raise _blocked(
                        "BUDGET_RECONCILIATION_CONFLICT",
                        f"기존 {name}={old}와 late observation {new}가 충돌합니다.",
                    )
        if not terminal and any(item.get("terminal_observed") is True for item in events):
            raise _blocked("BUDGET_OBSERVATION_REGRESSION", "이미 확인한 terminal 상태를 active 관측으로 되돌릴 수 없습니다.")

        actual = values[0] + values[2] if complete_total else None
        event_payload = {
            "observation_kind": "runtime_observation", "observation": document,
            "observation_digest": observation_digest,
            "original_receipt_digest": sha256_digest(json.loads(call["receipt_json"])),
            "terminal_observed": terminal, "usage_available": available,
            "usage_complete": complete, "actual_tokens": actual,
        }
        tx.history(call["project_id"], "budget.call_observed", "provider_call", call_id, event_payload)
        from .budget import _insert_usage_observation
        _insert_usage_observation(
            tx, call=call, source="runtime_observation", raw_document=document,
            known=available, values=values,
            usage_scope=(document.get("payload") or {}).get("usage_scope", "unavailable"),
            attribution_basis=(
                "first_empty_thread" if available and (document.get("payload") or {}).get("usage_scope") == "thread"
                else "provider_turn" if available else "unavailable"
            ),
            unavailable_reason=None if complete else (
                "PROVIDER_USAGE_PARTIAL" if available else
                "PROVIDER_USAGE_UNAVAILABLE_OR_UNATTRIBUTABLE" if terminal
                else "PROVIDER_TERMINAL_UNOBSERVED"
            ),
            late=True,
        )
        status = "settled" if complete_total else "usage_unknown" if terminal else "reserved"
        completed_at = call["completed_at"] or (tx.now if terminal else None)
        execution_status = call["execution_status"] if call["execution_status"] == "terminal" else (
            "terminal" if terminal else call["execution_status"]
        )
        effect_status = (
            "none"
            if terminal and call["attempt_id"] is None and call["role"] != "worker"
            else call["effect_status"]
        )
        result_status = call["result_status"]
        if terminal and result_status in {"pending", "unknown"}:
            result_status = "unknown"
        if call["goal_contract_digest"] is None:
            tx.connection.execute(
                "UPDATE provider_calls SET execution_status=?,effect_status=?,result_status=?,"
                "status=?,actual_tokens=?,completed_at=? WHERE id=?",
                (execution_status, effect_status, result_status, status, actual, completed_at, call_id),
            )
            if complete_total:
                tx.history(call["project_id"], "budget.call_settled", "provider_call", call_id,
                           {"observation_digest": observation_digest, "actual_tokens": actual,
                            "usage_available": True, "settlement_source": "runtime_observation"})
            return None

        if call["usage_id"] is None:
            raise _blocked("BUDGET_RECONCILIATION_USAGE_BINDING_MISMATCH", "Goal-bound 호출의 원본 usage가 없습니다.")
        prior = tx.one("SELECT payload_json FROM budget_usage WHERE id=?", (call["usage_id"],))
        prior_usage = BudgetUsageRecord.model_validate_json(prior["payload_json"])
        if prior_usage.project_id != call["project_id"] or prior_usage.logical_call_ref != receipt.call_id:
            raise _blocked("BUDGET_RECONCILIATION_USAGE_BINDING_MISMATCH", "원본 usage와 역할 receipt의 호출 결속이 다릅니다.")
        prior_values = (
            prior_usage.input_tokens, prior_usage.cached_input_tokens,
            prior_usage.output_tokens, prior_usage.reasoning_tokens,
        )
        for name, old, new in zip(_USAGE_KEYS, prior_values, values):
            if old is not None and new is not None and old != new:
                raise _blocked(
                    "BUDGET_RECONCILIATION_CONFLICT",
                    f"기존 {name}={old}와 late observation {new}가 충돌합니다.",
                )
        merged_values = tuple(
            old if old is not None else new for old, new in zip(prior_values, values)
        )
        merged_available = any(value is not None for value in merged_values)
        merged_complete = all(value is not None for value in merged_values)
        merged_complete_total = merged_values[0] is not None and merged_values[2] is not None
        merged_actual = (
            merged_values[0] + merged_values[2] if merged_complete_total else None
        )
        if call["actual_tokens"] is not None and merged_actual != call["actual_tokens"]:
            raise _blocked(
                "BUDGET_RECONCILIATION_CONFLICT",
                "late observation은 기존 actual_tokens를 변경할 수 없습니다.",
            )
        usage = usage_from_observation(call=call, receipt=receipt,
            event={**event_payload, "history_created_at": tx.now}, goal_digest=call["goal_contract_digest"])
        usage = usage.model_copy(update={
            "input_tokens": merged_values[0],
            "cached_input_tokens": merged_values[1],
            "output_tokens": merged_values[2],
            "reasoning_tokens": merged_values[3],
            "usage_available": merged_available,
            "unavailable_reason": (
                None if merged_complete else "PROVIDER_USAGE_PARTIAL"
                if merged_available else usage.unavailable_reason
            ),
        })
        reconciliation_id = new_id("usage_reconciliation")
        tx.connection.execute(
            "INSERT INTO budget_usage (id,project_id,goal_contract_digest,stage,logical_call_ref,payload_json,recorded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (usage.usage_id, usage.project_id, usage.goal_contract_digest, usage.stage.value,
             usage.logical_call_ref, canonical_json(usage), usage.recorded_at.isoformat()),
        )
        original_receipt = json.loads(call["receipt_json"])
        tx.connection.execute(
            "INSERT INTO usage_reconciliations (id,project_id,call_id,prior_usage_id,effective_usage_id,"
            "original_receipt_json,original_receipt_digest,observation_json,observation_digest,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (reconciliation_id, call["project_id"], call_id, prior_usage.usage_id, usage.usage_id,
             canonical_json(original_receipt), sha256_digest(original_receipt), canonical_json(document),
             observation_digest, tx.now),
        )
        preserved_status = call["status"] if call["status"] == "settled" else (
            "settled" if merged_complete_total else status
        )
        preserved_actual = call["actual_tokens"] if call["actual_tokens"] is not None else merged_actual
        tx.connection.execute(
            "UPDATE provider_calls SET execution_status=?,effect_status=?,result_status=?,"
            "status=?,actual_tokens=?,usage_id=?,completed_at=? WHERE id=?",
            (execution_status, effect_status, result_status, preserved_status, preserved_actual,
             usage.usage_id, completed_at, call_id),
        )
        tx.history(call["project_id"], "budget.role_usage_reconciled", "provider_call", call_id,
                   {"reconciliation_id": reconciliation_id, "prior_usage_id": prior_usage.usage_id,
                    "effective_usage_id": usage.usage_id, "observation_digest": observation_digest,
                    "actual_tokens": preserved_actual, "usage_available": merged_available,
                    "usage_complete": merged_complete, "original_receipt_preserved": True,
                    "late_component_completion": True})
        if merged_complete_total and call["status"] != "settled":
            tx.history(call["project_id"], "budget.call_settled", "provider_call", call_id,
                       {"observation_digest": observation_digest, "actual_tokens": preserved_actual,
                        "usage_available": True, "settlement_source": "runtime_observation"})
        return usage
