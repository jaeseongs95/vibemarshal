"""역할 호출의 지연된 terminal usage를 원본을 보존한 채 추가 결속한다."""
from __future__ import annotations

import json
from typing import Any

from ..canonical import canonical_json, sha256_digest
from .domain import BudgetStage, BudgetUsageRecord, new_id, utc_now
from .roles import RoleCallReceipt


_TERMINAL_STATUSES = {"completed", "success", "succeeded", "failed", "interrupted", "cancelled"}
_USAGE_KEYS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")


def _blocked(code: str, detail: str) -> Exception:
    # budget.py가 이 모듈을 delegate로 호출하므로 import 순환을 호출 시점까지 늦춘다.
    from .budget import BudgetBlocked
    return BudgetBlocked(code, detail)


def _observation_document(observation: Any) -> dict[str, Any]:
    if hasattr(observation, "model_dump"):
        return observation.model_dump(mode="json")
    return {
        "thread_id": observation.thread_id,
        "turn_id": observation.turn_id,
        "active": observation.active,
        "terminal_status": observation.terminal_status,
        "final_response": observation.final_response,
        "payload": observation.payload,
    }


def _usage_values(payload: dict[str, Any]) -> tuple[bool, tuple[int | None, ...]]:
    """명시 turn scope의 원시 provider usage만 실측으로 인정한다.

    역할 receipt에는 최초 thread가 비어 있었다는 create receipt가 저장되지 않으므로,
    이 최소 경로에서는 thread aggregate를 소급 귀속하지 않는다.
    """
    if payload.get("usage_scope") != "turn" or not isinstance(payload.get("usage"), dict):
        return False, (None,) * len(_USAGE_KEYS)
    raw = payload["usage"]
    values = tuple(raw.get(key) for key in _USAGE_KEYS)
    known = all(type(value) is int and value >= 0 for value in values)
    known = known and values[1] <= values[0] and values[3] <= values[2]
    if known and "totalTokens" in raw:
        known = type(raw["totalTokens"]) is int and raw["totalTokens"] == values[0] + values[2]
    return (True, values) if known else (False, (None,) * len(_USAGE_KEYS))


def reconcile_role_usage_terminal(service: Any, call_id: str, observation: Any) -> BudgetUsageRecord:
    """기존 역할 timeout/unknown usage에 terminal 재관측을 append-only로 연결한다.

    이 helper만 같은 logical call의 서로 다른 usage receipt를 원장에 추가할 수 있다.
    Worker/Validator Attempt의 provider-turn proof 경로는 대상으로 삼지 않는다.
    """
    document = _observation_document(observation)
    observation_digest = sha256_digest(document)
    with service.ledger.transaction() as tx:
        call = tx.one("SELECT * FROM provider_calls WHERE id=?", (call_id,))
        if call["attempt_id"] is not None:
            raise _blocked("BUDGET_ROLE_RECONCILIATION_ATTEMPT_NOT_SUPPORTED", "역할 호출(attempt_id 없음)만 재관측할 수 있습니다.")
        if call["receipt_json"] is None or call["usage_id"] is None:
            raise _blocked("BUDGET_ROLE_RECEIPT_REQUIRED", "thread/turn과 기존 usage가 결속된 원본 receipt가 필요합니다.")
        receipt = RoleCallReceipt.model_validate_json(call["receipt_json"])
        if (not receipt.turn_ids or observation.thread_id != receipt.thread_id
                or observation.turn_id != receipt.turn_ids[-1]):
            raise _blocked("BUDGET_OBSERVATION_BINDING_MISMATCH", "원본 역할 호출과 thread/turn이 다릅니다.")
        if observation.active or observation.terminal_status not in _TERMINAL_STATUSES:
            raise _blocked("BUDGET_TERMINAL_UNOBSERVED", "종료 요청과 실제 종료 관측은 다릅니다.")

        same_observation = tx.maybe_one(
            "SELECT effective_usage_id FROM usage_reconciliations WHERE call_id=? AND observation_digest=?",
            (call_id, observation_digest),
        )
        if same_observation is not None:
            row = tx.one("SELECT payload_json FROM budget_usage WHERE id=?", (same_observation["effective_usage_id"],))
            return BudgetUsageRecord.model_validate_json(row["payload_json"])

        prior = tx.one("SELECT payload_json FROM budget_usage WHERE id=?", (call["usage_id"],))
        prior_usage = BudgetUsageRecord.model_validate_json(prior["payload_json"])
        if prior_usage.project_id != call["project_id"] or prior_usage.logical_call_ref != receipt.call_id:
            raise _blocked("BUDGET_RECONCILIATION_USAGE_BINDING_MISMATCH", "원본 usage와 역할 receipt의 호출 결속이 다릅니다.")
        if call["goal_contract_digest"] is None:
            raise _blocked("BUDGET_RECONCILIATION_GOAL_REQUIRED", "Goal revision에 귀속된 역할 호출만 재관측할 수 있습니다.")
        if prior_usage.usage_available:
            raise _blocked("BUDGET_RECONCILIATION_CONFLICT", "확정된 실측 usage를 다른 terminal 관측으로 바꿀 수 없습니다.")
        if call["status"] not in {"reserved", "usage_unknown"}:
            raise _blocked("BUDGET_RECONCILIATION_NOT_REQUIRED", "미확인 usage 호출만 재관측할 수 있습니다.")

        known, values = _usage_values(observation.payload)
        duration = observation.payload.get("duration_ms")
        latency = duration if type(duration) is int and duration >= 0 else None
        usage = BudgetUsageRecord(
            usage_id=new_id("usage"), project_id=call["project_id"],
            goal_contract_digest=call["goal_contract_digest"], stage=BudgetStage(call["stage"]),
            logical_call_ref=receipt.call_id, role=receipt.role, call_status=observation.terminal_status,
            model=receipt.model, effort=receipt.effort,
            permission_profile=receipt.permission_profile, approval_policy=receipt.approval_policy,
            thread_id=receipt.thread_id, turn_ids=receipt.turn_ids, input_digest=receipt.input_digest,
            output_digest=(sha256_digest(observation.final_response)
                           if observation.final_response is not None else receipt.output_digest),
            output_schema_digest=receipt.output_schema_digest, runner_receipt_digest=observation_digest,
            input_tokens=values[0], cached_input_tokens=values[1], output_tokens=values[2],
            reasoning_tokens=values[3], latency_ms=latency, usage_available=known,
            usage_scope="turn" if known else "unavailable",
            usage_source="runtime.read.reconciliation",
            attribution_basis="provider_turn" if known else "unavailable",
            unavailable_reason=None if known else "PROVIDER_USAGE_UNAVAILABLE_OR_UNATTRIBUTABLE",
            provider_observation=document, retry_count=receipt.schema_recovery_attempts,
            recorded_at=utc_now(),
        )
        actual = usage.input_tokens + usage.output_tokens if known else None
        reconciliation_id = new_id("usage_reconciliation")
        original_receipt = json.loads(call["receipt_json"])
        tx.connection.execute(
            "INSERT INTO budget_usage (id, project_id, goal_contract_digest, stage, logical_call_ref, payload_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (usage.usage_id, usage.project_id, usage.goal_contract_digest, usage.stage.value,
             usage.logical_call_ref, canonical_json(usage), usage.recorded_at.isoformat()),
        )
        tx.connection.execute(
            "INSERT INTO usage_reconciliations "
            "(id, project_id, call_id, prior_usage_id, effective_usage_id, original_receipt_json, "
            "original_receipt_digest, observation_json, observation_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (reconciliation_id, call["project_id"], call_id, prior_usage.usage_id, usage.usage_id,
             canonical_json(original_receipt), sha256_digest(original_receipt), canonical_json(document),
             observation_digest, tx.now),
        )
        tx.connection.execute(
            "UPDATE provider_calls SET status=?,actual_tokens=?,usage_id=?,completed_at=? WHERE id=?",
            ("settled" if known else "usage_unknown", actual, usage.usage_id, tx.now, call_id),
        )
        tx.history(call["project_id"], "budget.role_usage_reconciled", "provider_call", call_id,
                   {"reconciliation_id": reconciliation_id, "prior_usage_id": prior_usage.usage_id,
                    "effective_usage_id": usage.usage_id, "observation_digest": observation_digest,
                    "actual_tokens": actual, "usage_available": known,
                    "original_receipt_preserved": True})
        return usage
