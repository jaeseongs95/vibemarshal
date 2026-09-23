"""일반 사용자 workflow와 원장 조회를 연결하는 Engine application facade."""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .capabilities import (
    CoreActionAuthority,
    CoreCapabilityError,
    GoalAuthorizationCapability,
    GoalAuthorizationTarget,
    require_host_execution,
)
from .domain import (
    AttemptRecord,
    BudgetStage,
    BudgetUsageRecord,
    ContextSourceRegistrationKind,
    EngineModel,
    FailureClass,
    GoalAuthorization,
    GoalContractRevision,
    GoalOperatingPolicy,
    GoalVerdict,
    ExecutionSpecProposal,
    ModelAssignmentContract,
    ModelFallback,
    MutationPolicy,
    PlanContractRevision,
    RecoveryAssessment,
    RepairAction,
    RevisionStatus,
    RoleAssignmentPolicy,
    RunOnceAction,
    RunOnceResult,
    RuntimeJob,
    RuntimeJobStatus,
    RuntimeIntentRecord,
    RuntimeReceipt,
    ValidationExecutionStep,
    new_id,
)
from .models import EngineRoleConfiguration, ModelInventory
from .goal import GoalPreparationOutcome
from .planning import PlanningSearchOutcome
from .recovery import (
    EvidenceFirstFailureClassifier,
    FailureDiagnosis,
    FailureSignal,
    recovery_limit_decision,
    recovery_route,
    terminal_failure_diagnosis,
)
from .read_models import (
    AttemptDetail,
    DuplicateLogicalCall,
    EntityRef,
    ExecutionObservationSummary,
    FinalReport,
    GovernancePluginTaskObservation,
    HistoryCursor,
    IntentDetail,
    ModelBindingItem,
    ModelBindingStatus,
    ModelObservationItem,
    ProviderCallExpectation,
    ProviderReceiptUsage,
    ReadPresentation,
    ReadOnlyReportVerification,
    RecoveryClassification,
    RecoveryCodeObservation,
    RecoveryExplanation,
    RecoveryLimitStatus,
    RecoveryNextAction,
    RecoveryScope,
    RecoveryStatus,
    TaskValidationRecovery,
    UsageBreakdown,
    UsageIncompleteReason,
    UsageMetric,
    UsageReconciliationPointer,
    UsageSummary,
)
from .roles import (
    CodexStructuredRoleRunner,
    RoleCallReceipt,
    RoleCallRequest,
    StructuredRolePort,
    strict_json_output_schema,
)
from .service import EngineService
from .validation_execution import GoalValidationRetryRequest
from .model_observation import authoritative_receipt_model_observation


class EnginePreparationResult(EngineModel):
    """실제 정규화·독립 review·Planning을 거친 승인 전 결과."""

    status: str
    project_id: str
    goal_preparation: GoalPreparationOutcome
    planning: PlanningSearchOutcome | None = None
    planning_search_id: str | None = None


class EngineAuthorizationResult(EngineModel):
    project_id: str
    authorization: GoalAuthorization
    activation_id: str


class EngineApplicationError(RuntimeError):
    """조회 인자가 원장에 없거나 결속이 깨졌을 때의 읽기 오류."""


UsageReadRecord = BudgetUsageRecord | ProviderReceiptUsage


def _metric(records: Iterable[UsageReadRecord], field: str) -> UsageMetric:
    # provider token usage와 elapsed latency는 독립 관측값이다. token usage가
    # unavailable이어도 terminal receipt의 latency_ms=0 또는 양수는 실측으로 보존한다.
    values = [
        getattr(item, field)
        if field == "latency_ms" or item.usage_available
        else None
        for item in records
    ]
    known = [value for value in values if value is not None]
    incomplete = len(values) - len(known)
    subtotal = sum(known)
    return UsageMetric(
        known_subtotal=subtotal,
        total=subtotal if incomplete == 0 else None,
        incomplete_call_count=incomplete,
    )


def _add_incomplete(metric: UsageMetric, count: int) -> UsageMetric:
    if count == 0:
        return metric
    return UsageMetric(
        known_subtotal=metric.known_subtotal,
        total=None,
        incomplete_call_count=metric.incomplete_call_count + count,
    )


def _breakdown(
    dimension: str,
    records: tuple[UsageReadRecord, ...],
    incomplete: dict[str, int] | None = None,
    *,
    latency_records: tuple[UsageReadRecord, ...] | None = None,
    latency_incomplete: dict[str, int] | None = None,
) -> tuple[UsageBreakdown, ...]:
    grouped: dict[str, list[UsageReadRecord]] = defaultdict(list)
    for item in records:
        grouped[item.stage.value if dimension == "stage" else item.role].append(item)
    incomplete = incomplete or {}
    latency_grouped: dict[str, list[UsageReadRecord]] = defaultdict(list)
    for item in (records if latency_records is None else latency_records):
        latency_grouped[item.stage.value if dimension == "stage" else item.role].append(item)
    latency_incomplete = latency_incomplete or {}
    result = []
    for value in sorted(set(grouped) | set(incomplete) | set(latency_grouped) | set(latency_incomplete)):
        items = grouped[value]
        latency_items = latency_grouped[value]
        missing = incomplete.get(value, 0)
        result.append(UsageBreakdown(
            dimension=dimension, value=value, logical_call_count=len(items),
            input_tokens=_add_incomplete(_metric(items, "input_tokens"), missing),
            cached_input_tokens=_add_incomplete(_metric(items, "cached_input_tokens"), missing),
            output_tokens=_add_incomplete(_metric(items, "output_tokens"), missing),
            reasoning_tokens=_add_incomplete(_metric(items, "reasoning_tokens"), missing),
            latency_ms=_add_incomplete(
                _metric(latency_items, "latency_ms"), latency_incomplete.get(value, 0)
            ),
        ))
    return tuple(result)


def _deduplicate_usage(
    records: Iterable[BudgetUsageRecord],
) -> tuple[tuple[BudgetUsageRecord, ...], tuple[DuplicateLogicalCall, ...], tuple[DuplicateLogicalCall, ...]]:
    """같은 논리 호출의 동일 receipt만 하나로 보며, 충돌은 합산하지 않는다."""
    grouped: dict[str, list[BudgetUsageRecord]] = defaultdict(list)
    for item in records:
        grouped[item.logical_call_ref].append(item)
    selected: list[BudgetUsageRecord] = []
    deduplicated: list[DuplicateLogicalCall] = []
    conflicts: list[DuplicateLogicalCall] = []
    for call_ref, items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: (item.recorded_at, item.usage_id))
        receipts = tuple(sorted({item.runner_receipt_digest for item in ordered}))
        if len(receipts) > 1:
            conflicts.append(DuplicateLogicalCall(
                logical_call_ref=call_ref, status="conflict",
                usage_ids=tuple(item.usage_id for item in ordered), receipt_digests=receipts,
                stages=tuple(sorted({item.stage.value for item in ordered})),
                roles=tuple(sorted({item.role for item in ordered})),
            ))
            continue
        selected.append(ordered[0])
        if len(ordered) > 1:
            deduplicated.append(DuplicateLogicalCall(
                logical_call_ref=call_ref, status="deduplicated",
                usage_ids=tuple(item.usage_id for item in ordered), receipt_digests=receipts,
                stages=tuple(sorted({item.stage.value for item in ordered})),
                roles=tuple(sorted({item.role for item in ordered})),
            ))
    return tuple(selected), tuple(deduplicated), tuple(conflicts)


def _deduplicate_provider_receipts(
    records: Iterable[ProviderReceiptUsage],
) -> tuple[
    tuple[ProviderReceiptUsage, ...],
    tuple[DuplicateLogicalCall, ...],
    tuple[DuplicateLogicalCall, ...],
]:
    grouped: dict[str, list[ProviderReceiptUsage]] = defaultdict(list)
    for item in records:
        grouped[item.logical_call_ref].append(item)
    selected: list[ProviderReceiptUsage] = []
    deduplicated: list[DuplicateLogicalCall] = []
    conflicts: list[DuplicateLogicalCall] = []
    for call_ref, items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: (item.recorded_at, item.provider_call_id))
        receipts = tuple(sorted({item.runner_receipt_digest for item in ordered}))
        duplicate = DuplicateLogicalCall(
            logical_call_ref=call_ref,
            status="conflict" if len(receipts) > 1 else "deduplicated",
            provider_call_ids=tuple(item.provider_call_id for item in ordered),
            receipt_digests=receipts,
            stages=tuple(sorted({item.stage.value for item in ordered})),
            roles=tuple(sorted({item.role for item in ordered})),
        )
        if len(receipts) > 1:
            conflicts.append(duplicate)
            continue
        selected.append(ordered[0])
        if len(ordered) > 1:
            deduplicated.append(duplicate)
    return tuple(selected), tuple(deduplicated), tuple(conflicts)


def summarize_usage_records(
    *,
    project_id: str,
    goal_id: str,
    goal_revision_digests: Iterable[str],
    records: Iterable[BudgetUsageRecord],
    presentation: ReadPresentation,
    expected_logical_call_refs: Iterable[str] = (),
    provider_call_expectations: Iterable[ProviderCallExpectation] = (),
    provider_receipt_usage: Iterable[ProviderReceiptUsage] = (),
    goal_revision_unavailable_reason: str | None = None,
    superseded_usage_ids: Iterable[str] = (),
    reconciliations: Iterable[UsageReconciliationPointer] = (),
) -> UsageSummary:
    """DB 밖에서 확보한 동일 Goal usage에도 쓸 수 있는 순수 집계 함수다."""
    superseded = tuple(sorted(set(superseded_usage_ids)))
    superseded_set = set(superseded)
    scoped = tuple(item for item in records if item.usage_id not in superseded_set)
    deduplicated_selected, usage_deduplicated, usage_conflicts = _deduplicate_usage(scoped)
    projected = tuple(provider_receipt_usage)
    projected_selected, projected_deduplicated, projected_conflicts = (
        _deduplicate_provider_receipts(projected)
    )
    deduplicated = (*usage_deduplicated, *projected_deduplicated)
    conflicts = (*usage_conflicts, *projected_conflicts)
    projected_provider_ids = {item.provider_call_id for item in projected}
    if len(projected_provider_ids) != len(projected):
        raise ValueError("provider receipt projection의 provider call ID가 중복됐습니다.")
    if {item.logical_call_ref for item in scoped} & {item.logical_call_ref for item in projected}:
        raise ValueError("Goal-bound usage와 provider receipt projection을 중복 합산할 수 없습니다.")
    expected = tuple(sorted(set(expected_logical_call_refs)))
    seen = {item.logical_call_ref for item in scoped} | {
        item.logical_call_ref for item in projected
    }
    provider_calls = tuple(provider_call_expectations)
    usage_ids = {item.usage_id for item in scoped}
    provider_incomplete = tuple(
        item for item in provider_calls
        if item.provider_call_id not in projected_provider_ids
        and (
            item.execution_status != "terminal"
            or item.usage_id is None
            or item.usage_id not in usage_ids
        )
    )
    # reserved/usage_unknown call에 연결된 usage는 최종 관측 전 값이다. 합산 대상에서
    # 먼저 제외한 뒤 하나의 미확인 호출로 표시해 같은 call을 두 번 세지 않는다.
    usage_by_id = {item.usage_id: item for item in scoped}
    incomplete_usage_ids = {item.usage_id for item in provider_incomplete if item.usage_id is not None}
    # provider usage_id가 같은 logical receipt의 deduplicated 아닌 사본을 가리킬 수
    # 있다. 결제 완결성은 usage row ID가 아니라 그 논리 호출 전체에 적용한다.
    incomplete_logical_call_refs = {
        usage_by_id[usage_id].logical_call_ref
        for usage_id in incomplete_usage_ids
        if usage_id in usage_by_id
    }
    selected_usage = tuple(
        item for item in deduplicated_selected
        if item.logical_call_ref not in incomplete_logical_call_refs
    )
    selected: tuple[UsageReadRecord, ...] = (*selected_usage, *projected_selected)
    # token settlement은 아직 불완전해도 receipt에서 얻은 latency 관측은 합산할 수
    # 있다. receipt conflict는 deduplicated_selected에서 제외돼 합산하지 않는다.
    latency_records: tuple[UsageReadRecord, ...] = (
        *deduplicated_selected,
        *projected_selected,
    )
    missing_expected = tuple(ref for ref in expected if ref not in seen)
    reasons: list[UsageIncompleteReason] = [
        UsageIncompleteReason(
            code="EXPECTED_LOGICAL_CALL_MISSING", call_ref=ref,
            detail="예상 논리 호출에 결속된 usage 레코드가 없습니다.",
        )
        for ref in missing_expected
    ]
    reasons.extend(
        UsageIncompleteReason(
            code=(item.incomplete_reason_code or (
                "PROVIDER_CALL_NOT_SETTLED"
                if item.execution_status != "terminal"
                else "PROVIDER_CALL_USAGE_MISSING"
            )),
            call_ref=item.call_key, stage=item.stage, role=item.role,
            detail=(item.incomplete_reason_detail or (
                "provider 실행·효과가 terminal이 아니므로 usage와 별개로 먼저 관측해야 합니다."
                if item.execution_status != "terminal"
                else "provider 실행 상태와 별개로 결속된 UsageObservation/usage 레코드가 없습니다."
            )),
        )
        for item in provider_incomplete
    )
    reasons.extend(
        UsageIncompleteReason(
            code="LOGICAL_CALL_RECEIPT_CONFLICT", call_ref=item.logical_call_ref,
            stage=item.stages[0] if len(item.stages) == 1 else None,
            role=item.roles[0] if len(item.roles) == 1 else None,
            detail="같은 논리 호출에 서로 다른 receipt가 있어 어느 값을 합산할지 결정할 수 없습니다.",
        )
        for item in conflicts
    )
    # selected의 미확인 record는 token _metric이 해당 record를 이미 불완전 호출로
    # 센다. 사유만 추가하고 아래의 원장 없는 호출 수에는 다시 더하지 않는다.
    # provider 미완결·receipt conflict는 token metric을 불완전하게 만들지만,
    # latency는 별도 receipt 관측이 있으면 알려진 소계를 보존한다.
    synthetic_reason_count = len(reasons)
    # provider 미완결 usage도 원시 unavailable/latency 사유는 표시한다. 다만
    # synthetic count에는 넣지 않아 provider 미완결 한 건을 두 번 세지 않는다.
    direct_reason_records = latency_records
    for item in direct_reason_records:
        if not item.usage_available:
            reasons.append(UsageIncompleteReason(
                code="USAGE_UNAVAILABLE", call_ref=item.logical_call_ref,
                stage=item.stage.value, role=item.role,
                detail=item.unavailable_reason or "provider가 이 논리 호출의 사용량을 제공하지 않았습니다.",
            ))
        elif any(
            value is None
            for value in (
                item.input_tokens,
                item.cached_input_tokens,
                item.output_tokens,
                item.reasoning_tokens,
            )
        ):
            reasons.append(UsageIncompleteReason(
                code="USAGE_PARTIAL", call_ref=item.logical_call_ref,
                stage=item.stage.value, role=item.role,
                detail=item.unavailable_reason or "provider usage의 일부 구성요소가 제공되지 않았습니다.",
            ))
        if item.latency_ms is None:
            reasons.append(UsageIncompleteReason(
                code="LATENCY_UNAVAILABLE", call_ref=item.logical_call_ref,
                stage=item.stage.value, role=item.role,
                detail="이 논리 호출의 지연 시간이 관측되지 않았습니다.",
            ))
    unobserved_provider_calls = tuple(item.call_key for item in provider_incomplete)
    incomplete_by_stage: dict[str, int] = defaultdict(int)
    incomplete_by_role: dict[str, int] = defaultdict(int)
    for reason in reasons[:synthetic_reason_count]:
        if reason.stage is not None:
            incomplete_by_stage[reason.stage] += 1
        if reason.role is not None:
            incomplete_by_role[reason.role] += 1
    latency_incomplete_by_stage: dict[str, int] = defaultdict(int)
    latency_incomplete_by_role: dict[str, int] = defaultdict(int)
    latency_synthetic_count = 0

    def mark_latency_missing(*, stage: str | None = None, role: str | None = None) -> None:
        nonlocal latency_synthetic_count
        latency_synthetic_count += 1
        if stage is not None:
            latency_incomplete_by_stage[stage] += 1
        if role is not None:
            latency_incomplete_by_role[role] += 1

    # 예상 호출/receipt conflict에는 사용할 latency receipt가 없고, provider call에
    # usage record 자체가 없을 때만 provider 상태가 latency total도 불완전하게 만든다.
    for _ref in missing_expected:
        mark_latency_missing()
    for conflict in conflicts:
        mark_latency_missing(
            stage=conflict.stages[0] if len(conflict.stages) == 1 else None,
            role=conflict.roles[0] if len(conflict.roles) == 1 else None,
        )
    latency_logical_call_refs = {item.logical_call_ref for item in latency_records}
    for provider_call in provider_incomplete:
        provider_usage = (
            None if provider_call.usage_id is None
            else usage_by_id.get(provider_call.usage_id)
        )
        if (
            provider_usage is None
            or provider_usage.logical_call_ref not in latency_logical_call_refs
        ):
            mark_latency_missing(stage=provider_call.stage, role=provider_call.role)
    return UsageSummary(
        project_id=project_id, goal_id=goal_id,
        goal_revision_digests=tuple(sorted(set(goal_revision_digests))),
        goal_revision_unavailable_reason=goal_revision_unavailable_reason,
        logical_call_count=len(selected), expected_logical_call_refs=expected,
        provider_call_count=len(provider_calls) if provider_calls else None,
        missing_expected_logical_call_refs=missing_expected,
        conflicts=conflicts, deduplicated=deduplicated,
        incomplete_reasons=tuple(reasons),
        provider_call_expectations=provider_calls,
        provider_calls_without_usage=unobserved_provider_calls,
        superseded_usage_ids=superseded,
        reconciliations=tuple(sorted(reconciliations, key=lambda item: item.reconciliation_id)),
        input_tokens=_add_incomplete(_metric(selected, "input_tokens"), synthetic_reason_count),
        cached_input_tokens=_add_incomplete(_metric(selected, "cached_input_tokens"), synthetic_reason_count),
        output_tokens=_add_incomplete(_metric(selected, "output_tokens"), synthetic_reason_count),
        reasoning_tokens=_add_incomplete(_metric(selected, "reasoning_tokens"), synthetic_reason_count),
        latency_ms=_add_incomplete(
            _metric(latency_records, "latency_ms"), latency_synthetic_count
        ),
        by_stage=_breakdown(
            "stage", selected, incomplete_by_stage,
            latency_records=latency_records,
            latency_incomplete=latency_incomplete_by_stage,
        ),
        by_role=_breakdown(
            "role", selected, incomplete_by_role,
            latency_records=latency_records,
            latency_incomplete=latency_incomplete_by_role,
        ),
        usage_records=selected_usage,
        provider_receipt_usage=projected,
        entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
        error_code=("USAGE_RECEIPT_CONFLICT" if conflicts else
                    "USAGE_INCOMPLETE" if reasons else presentation.error_code),
        next_action=(
            "충돌한 논리 호출의 receipt를 원장에서 대조하십시오."
            if conflicts else "예상 호출과 provider receipt를 원장에서 대조하십시오."
            if reasons else
            "Goal 준비 실패를 검토하십시오. Goal revision이나 BudgetUsageRecord를 임의로 만들지 마십시오."
            if goal_revision_unavailable_reason else presentation.next_action
        ),
    )


def _provider_receipt_projection(
    row: Any,
    history_events: tuple[Any, ...],
) -> tuple[ProviderReceiptUsage | None, tuple[str, str] | None]:
    """Goal 생성 전 호출의 원 receipt와 최신 runtime 관측을 읽기 전용으로 투영한다."""
    if row["goal_contract_digest"] is not None or row["usage_id"] is not None:
        return None, (
            "PROVIDER_GOAL_USAGE_BINDING_INCOMPLETE",
            "Goal digest와 usage ID가 함께 완성되지 않은 provider call입니다.",
        )
    if row["receipt_json"] is None:
        return None, ("PROVIDER_RECEIPT_MISSING", "provider call의 원문 receipt가 없습니다.")
    try:
        request_document = json.loads(row["request_json"])
        request = RoleCallRequest.model_validate(request_document)
        receipt_document = json.loads(row["receipt_json"])
        receipt = RoleCallReceipt.model_validate(receipt_document)
        if (
            row["request_digest"] != sha256_digest(request_document)
            or receipt.input_digest != request.request_digest
            or (row["role"], receipt.role) != (request.role, request.role)
            or (receipt.model, receipt.effort) != (request.model, request.effort)
            or receipt.inventory_digest != request.inventory_digest
            or receipt.output_schema_digest
            != sha256_digest(strict_json_output_schema(request.output_schema))
            or getattr(receipt, "timeout_policy_digest", None)
            != getattr(request, "timeout_policy_digest", None)
            or getattr(receipt, "observation_policy_digest", None)
            != getattr(request, "observation_policy_digest", None)
        ):
            raise ValueError("request/receipt digest 또는 역할·모델 결속이 다릅니다.")
        reserved_events = [
            event for event in history_events if event["event_type"] == "budget.call_reserved"
        ]
        if len(reserved_events) != 1:
            return None, (
                "PROVIDER_CALL_HISTORY_INCOMPLETE",
                "provider call의 예약 History가 정확히 한 건 필요합니다.",
            )
        reserved = json.loads(reserved_events[0]["payload_json"])
        if (
            reserved.get("goal_id") != row["goal_id"]
            or reserved.get("call_key") != row["call_key"]
            or reserved.get("role") != row["role"]
        ):
            return None, (
                "PROVIDER_CALL_HISTORY_BINDING_INVALID",
                "provider call의 예약 History가 현재 요청과 다릅니다.",
            )
        receipt_history_documents = []
        for event in history_events:
            payload = json.loads(event["payload_json"])
            if (event["event_type"] == "budget.call_observed"
                    and payload.get("observation_kind") == "role_receipt"
                    and payload.get("receipt") is not None):
                receipt_history_documents.append(payload["receipt"])
            elif event["event_type"] == "budget.call_settled" and payload.get("receipt") is not None:
                # schema revision 3의 기존 History는 unknown 관측도 call_settled로 기록했다.
                receipt_history_documents.append(payload["receipt"])
        if not receipt_history_documents:
            return None, ("PROVIDER_CALL_HISTORY_INCOMPLETE",
                          "원본 role receipt 관측 History가 없습니다.")
        def normalized_unavailable_receipt(value: Any) -> RoleCallReceipt:
            item = RoleCallReceipt.model_validate(value)
            counts = (item.input_tokens, item.cached_input_tokens,
                      item.output_tokens, item.reasoning_tokens)
            if not item.usage_available and all(count in (None, 0) for count in counts):
                item = item.model_copy(update={
                    "input_tokens": None, "cached_input_tokens": None,
                    "output_tokens": None, "reasoning_tokens": None,
                })
            return item

        normalized_receipt = normalized_unavailable_receipt(receipt)
        if any(canonical_json(normalized_unavailable_receipt(item))
               != canonical_json(normalized_receipt) for item in receipt_history_documents):
            return None, ("PROVIDER_CALL_HISTORY_BINDING_INVALID",
                          "원본 role receipt History가 현재 receipt와 다릅니다.")
        from .role_usage_reconciliation import TERMINAL_STATUSES, observation_usage_values
        runtime_events = []
        for event in history_events:
            if event["event_type"] != "budget.call_observed":
                continue
            payload = json.loads(event["payload_json"])
            if payload.get("observation_kind") != "runtime_observation":
                continue
            document = payload.get("observation")
            if (not isinstance(document, dict)
                    or payload.get("observation_digest") != sha256_digest(document)
                    or payload.get("original_receipt_digest") != sha256_digest(json.loads(row["receipt_json"]))
                    or document.get("thread_id") != receipt.thread_id
                    or not receipt.turn_ids or document.get("turn_id") != receipt.turn_ids[-1]):
                raise ValueError("runtime 관측의 원문/digest/thread/turn 결속이 다릅니다.")
            runtime_events.append((event, payload, document))

        projection_source = "provider_receipt"
        runtime_digest = None
        usage_scope = "unspecified"
        attribution_basis = None
        call_status = receipt.status
        runner_digest = sha256_digest(json.loads(row["receipt_json"]))
        recorded_at = receipt.recorded_at
        latency = receipt.latency_ms
        if runtime_events:
            event, payload, document = runtime_events[-1]
            terminal = not document.get("active", True) and document.get("terminal_status") in TERMINAL_STATUSES
            available, values = observation_usage_values(document.get("payload") or {}, receipt)
            available = terminal and available
            if not available:
                values = (None, None, None, None)
            complete = all(value is not None for value in values)
            complete_total = values[0] is not None and values[2] is not None
            actual = values[0] + values[2] if complete_total else None
            expected_status = "settled" if complete_total else "usage_unknown" if terminal else "reserved"
            duration = (document.get("payload") or {}).get("duration_ms")
            latency = duration if type(duration) is int and duration >= 0 and terminal else None
            projection_source = "runtime_observation"
            runtime_digest = payload["observation_digest"]
            runner_digest = runtime_digest
            recorded_at = event["created_at"]
            call_status = document.get("terminal_status") if terminal else "active"
            unavailable_reason = (None if complete else "PROVIDER_USAGE_PARTIAL" if available else
                "PROVIDER_USAGE_UNAVAILABLE_OR_UNATTRIBUTABLE" if terminal else
                "PROVIDER_TERMINAL_UNOBSERVED")
            usage_scope = document.get("payload", {}).get("usage_scope") if available else "unavailable"
            attribution_basis = ("first_empty_thread" if available and usage_scope == "thread"
                                 else "provider_turn" if available else "unavailable")
        else:
            terminal = receipt.status in {"succeeded", "failed", "schema_failed", "input_contract_failed"} or (
                receipt.terminal_observation_digest is not None
                and receipt.terminal_status_after_interrupt in TERMINAL_STATUSES
            )
            if not terminal:
                return None, ("PROVIDER_TERMINAL_RECEIPT_MISSING",
                              "provider receipt에서 terminal 상태를 확인할 수 없습니다.")
            available = receipt.usage_available and receipt.schema_recovery_attempts == 0
            values = (receipt.input_tokens, receipt.cached_input_tokens,
                      receipt.output_tokens, receipt.reasoning_tokens) if available else (None, None, None, None)
            complete = all(value is not None for value in values)
            complete_total = values[0] is not None and values[2] is not None
            actual = values[0] + values[2] if complete_total else None
            expected_status = "settled" if complete_total else "usage_unknown"
            unavailable_reason = (None if complete else "PROVIDER_USAGE_PARTIAL" if available
                                  else "PROVIDER_USAGE_UNAVAILABLE_OR_RECOVERY_UNATTRIBUTABLE")
        if row["status"] != expected_status or row["actual_tokens"] != actual:
            return None, ("PROVIDER_RECEIPT_SETTLEMENT_CONFLICT",
                          "provider call 상태·actual_tokens가 최신 유효 관측과 다릅니다.")
        observed_model, observed_effort = authoritative_receipt_model_observation(
            observed_model=receipt.observed_model,
            observed_effort=receipt.observed_effort,
            binding_provenance=receipt.binding_provenance,
        )
        return ProviderReceiptUsage(
            projection_source=projection_source, runtime_observation_digest=runtime_digest,
            usage_scope=usage_scope, attribution_basis=attribution_basis,
            provider_call_id=row["id"], project_id=row["project_id"], goal_id=row["goal_id"],
            stage=BudgetStage(row["stage"]), logical_call_ref=receipt.call_id, role=receipt.role,
            call_status=call_status, model=receipt.model, effort=receipt.effort,
            requested_model=receipt.requested_model or receipt.model,
            requested_effort=receipt.requested_effort or receipt.effort,
            observed_model=observed_model,
            observed_effort=observed_effort,
            provider_inventory_digest=(
                receipt.provider_inventory_digest
                or (receipt.observed_binding.inventory.provider_inventory_digest
                    if receipt.observed_binding is not None else None)
            ),
            adapter_capability_digest=(
                receipt.adapter_capability_digest
                or (receipt.observed_binding.inventory.adapter_capability_digest
                    if receipt.observed_binding is not None else None)
            ),
            binding_provenance={
                "requested": "role_request",
                # 늦은 회계 관측은 model provenance를 승격하지 않는다.
                "observed": (
                    (receipt.binding_provenance or {}).get("observed")
                    if observed_model is not None and observed_effort is not None
                    else None
                ),
                "provider_inventory": (
                    receipt.observed_binding.inventory.inventory_provenance
                    if receipt.observed_binding is not None else None
                ),
                "adapter_capability": "local_operational_binding" if receipt.observed_binding is not None else None,
            },
            runner_receipt_digest=runner_digest, input_tokens=values[0], cached_input_tokens=values[1],
            output_tokens=values[2], reasoning_tokens=values[3], latency_ms=latency,
            usage_available=available, unavailable_reason=unavailable_reason, recorded_at=recorded_at,
        ), None
    except Exception as error:
        return None, (
            "PROVIDER_RECEIPT_BINDING_INVALID",
            f"provider request/receipt를 typed usage로 투영할 수 없습니다: {error}"[:1000],
        )


class ApplicationAuthority:
    """신뢰 host와 EngineApplication 사이의 process-local 승인 권위 경계.

    역할 scope 재진입을 막지만 hostile same-process 코드나 같은 OS 사용자를
    격리하는 보안 sandbox는 아니다.
    """

    def __init__(self, application: "EngineApplication") -> None:
        require_host_execution()
        self._application = application
        self._authorization_attempted = False
        application._install_core_action_authority()

    def authorization_target(
        self,
        project_id: str,
        *,
        operating_policy: GoalOperatingPolicy | None = None,
    ) -> GoalAuthorizationTarget:
        require_host_execution()
        return self._application.authorization_target(
            project_id,
            operating_policy=operating_policy,
        )

    def authorize(
        self,
        project_id: str,
        *,
        target: GoalAuthorizationTarget,
        source: str,
        operating_policy: GoalOperatingPolicy | None = None,
    ) -> EngineAuthorizationResult:
        require_host_execution()
        if self._authorization_attempted:
            raise CoreCapabilityError(
                "CORE_CAPABILITY_DENIED: Application 승인 요청은 이미 소모됐습니다."
            )
        self._authorization_attempted = True
        return self._application._authorize_with_application_authority(
            project_id,
            target=target,
            source=source,
            operating_policy=operating_policy,
        )


class EngineApplication:
    """준비부터 최종 보고까지 Core 권위를 유지하는 단일 facade."""

    def __init__(
        self,
        service: EngineService,
        *,
        runtime: Any | None = None,
        role_configuration: EngineRoleConfiguration | None = None,
        structured_runner: StructuredRolePort | None = None,
        supervisor: Any | None = None,
        inspection_contract: str = "plan-inspection-v1",
        governance: Any | None = None,
    ) -> None:
        self.service = service
        self.runtime = runtime
        self.role_configuration = role_configuration
        self.inspection_contract = inspection_contract
        # 실행 Task의 필수 governance gate 설정(open_gate). 없으면 실행 Task를 dispatch하지 않는다.
        self.governance = governance
        self._task_gate: Any | None = None
        self._structured_runner = structured_runner
        self._core_action_authority: CoreActionAuthority | None = None
        if supervisor is not None:
            self.supervisor = supervisor
        elif runtime is not None:
            from .runtime import RuntimeJobSupervisor

            self.supervisor = RuntimeJobSupervisor(service, runtime)
        else:
            self.supervisor = None

    def _install_core_action_authority(self) -> None:
        """ApplicationAuthority 생성 시에만 Core issuer를 한 번 설치한다."""
        require_host_execution()
        if self._core_action_authority is not None:
            raise CoreCapabilityError(
                "CORE_CAPABILITY_ALREADY_BOUND: ApplicationAuthority가 이미 결속됐습니다."
            )
        authority = CoreActionAuthority()
        self.service._bind_action_authority(authority)
        self._core_action_authority = authority

    def _authorize_with_application_authority(
        self,
        project_id: str,
        *,
        target: GoalAuthorizationTarget,
        source: str,
        operating_policy: GoalOperatingPolicy | None = None,
    ) -> EngineAuthorizationResult:
        require_host_execution()
        authority = self._core_action_authority
        if authority is None:
            raise CoreCapabilityError(
                "CORE_CAPABILITY_REQUIRED: ApplicationAuthority가 필요합니다."
            )
        capability = authority.issue_goal_authorization(
            ledger_path=self.service.ledger.path,
            target=target,
        )
        return self.authorize(
            project_id,
            source=source,
            operating_policy=operating_policy,
            capability=capability,
            authorization_target=target,
            selected_plan_revision_id=target.plan_revision_id,
        )

    def _execution_components(self) -> tuple[Any, EngineRoleConfiguration, StructuredRolePort]:
        if self.runtime is None:
            raise EngineApplicationError("RUNTIME_REQUIRED")
        if self.role_configuration is None:
            raise EngineApplicationError("ROLE_CONFIGURATION_REQUIRED")
        runner = self._structured_runner
        if runner is None:
            runner = CodexStructuredRoleRunner(
                self.runtime,
                max_schema_recovery_attempts=0,
                ephemeral_threads=False,
            )
            self._structured_runner = runner
        return self.runtime, self.role_configuration, runner

    @staticmethod
    def _assignment(binding: Any, *, role: str) -> RoleAssignmentPolicy:
        return RoleAssignmentPolicy(
            role=role,
            preferred_model=binding.model,
            preferred_effort=binding.effort,
            allowed_fallbacks=tuple(
                ModelFallback(model=item.model, effort=item.effort)
                for item in binding.allowed_fallbacks
            ),
        )

    def prepare(
        self,
        project_id: str,
        *,
        source_request: str,
        candidate_count: int | None = None,
    ) -> EnginePreparationResult:
        """raw request를 실제 Goal 역할과 Planning 역할로 준비해 승인 후보를 만든다."""

        if not source_request.strip():
            raise EngineApplicationError("SOURCE_REQUEST_REQUIRED")
        return self._prepare_goal_revision(
            project_id,
            source_request=source_request,
            candidate_count=candidate_count,
            goal_id=new_id("goal"),
        )

    def revise(
        self,
        project_id: str,
        *,
        source_request: str,
        candidate_count: int | None = None,
    ) -> EnginePreparationResult:
        """최신 Goal의 다음 revision을 prepare와 같은 역할로 준비한다. Plan은 활성화하지 않는다."""

        if not source_request.strip():
            raise EngineApplicationError("SOURCE_REQUEST_REQUIRED")
        # active Plan 중 Goal revision은 아직 지원하지 않는다(미해결 gap).
        # register_goal이 교체를 막으므로 역할·runtime 호출과 원장 쓰기 전에 먼저 거절한다.
        active_plan_revision_id = self._project_row(project_id)["active_plan_revision_id"]
        if active_plan_revision_id is not None:
            raise EngineApplicationError(
                "GOAL_REVISION_ACTIVE_PLAN: active Plan "
                f"{active_plan_revision_id}이 있는 동안의 Goal revision은 아직 지원하지 않습니다."
            )
        latest = self.service.load_latest_goal(project_id)
        return self._prepare_goal_revision(
            project_id,
            source_request=source_request,
            candidate_count=candidate_count,
            goal_id=latest.goal_id,
            revision_no=latest.revision_no + 1,
            supersedes_goal_revision_id=latest.goal_revision_id,
        )

    def _prepare_goal_revision(
        self,
        project_id: str,
        *,
        source_request: str,
        candidate_count: int | None,
        goal_id: str,
        revision_no: int = 1,
        supersedes_goal_revision_id: str | None = None,
    ) -> EnginePreparationResult:
        """prepare·revise가 공유하는 Goal 준비·등록·Planning. 활성화는 authorize만 한다."""

        runtime, roles, base_runner = self._execution_components()
        inventory = runtime.list_models()
        roles.validate_inventory(inventory)
        profile = self.service.load_active_profile(project_id)

        from .budget import BudgetManager, BudgetedRoleRunner
        from .goal import GoalNormalizerAdapter, GoalPreparationPipeline, GoalReviewerAdapter

        goal_runner = BudgetedRoleRunner(
            base_runner,
            self.service,
            project_id=project_id,
            goal_id=goal_id,
        )
        root = Path(self.service.status(project_id)["project"]["root"])
        goal_preparation = GoalPreparationPipeline(
            GoalNormalizerAdapter(
                goal_runner,
                model=roles.normalizer.model,
                effort=roles.normalizer.effort,
                inventory_digest=inventory.inventory_digest,
                inventory=inventory,
                allowed_fallbacks=roles.normalizer.allowed_fallbacks,
                cwd=root,
            ),
            GoalReviewerAdapter(
                goal_runner,
                model=roles.critical_reviewer.model,
                effort=roles.critical_reviewer.effort,
                inventory_digest=inventory.inventory_digest,
                inventory=inventory,
                allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks,
                cwd=root,
            ),
        ).prepare(
            project_id=project_id,
            goal_id=goal_id,
            revision_no=revision_no,
            supersedes_goal_revision_id=supersedes_goal_revision_id,
            profile=profile,
            source_request=source_request,
            observed_facts=self.service.observe_goal_inputs(project_id, source_request),
        )
        goal = goal_preparation.goal_contract
        self.service.register_goal(goal, activate=goal.status is RevisionStatus.READY)
        BudgetManager(self.service).attach_goal(project_id, goal_id, goal.definition_digest)
        if goal.status is not RevisionStatus.READY:
            return EnginePreparationResult(
                status="needs_user_input",
                project_id=project_id,
                goal_preparation=goal_preparation,
            )

        active_goal = self.service.load_active_goal(project_id)
        project_map, state = self.service.reobserve_project(project_id)
        planning_runner = BudgetedRoleRunner(
            base_runner,
            self.service,
            project_id=project_id,
            goal_id=goal_id,
            goal_digest=goal.definition_digest,
        )
        assignment = ModelAssignmentContract(
            executor=self._assignment(roles.executor, role="executor"),
            validator=self._assignment(roles.validator, role="validator"),
            independence_required=True,
        )

        from .models import AssignmentResolver
        from .plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V2
        from .planner_roles import (
            PlanExpanderAdapter,
            PlanReviewerAdapter,
            RuleBasedTaskAssigner,
            SkeletonGeneratorAdapter,
            SkeletonReviewerAdapter,
        )
        from .planning import PlanningRecoveryPolicy, SkeletonFirstPlanner

        AssignmentResolver().resolve_contract(assignment, inventory)
        assigner = RuleBasedTaskAssigner(assignment, assignment, assignment)
        common = {
            "inventory_digest": inventory.inventory_digest,
            "inventory": inventory,
            "cwd": root,
        }
        planning = SkeletonFirstPlanner(
            SkeletonGeneratorAdapter(
                planning_runner,
                model=roles.skeleton_generator.model,
                effort=roles.skeleton_generator.effort,
                allowed_fallbacks=roles.skeleton_generator.allowed_fallbacks,
                **common,
            ),
            SkeletonReviewerAdapter(
                planning_runner,
                model=roles.general_reviewer.model,
                effort=roles.general_reviewer.effort,
                allowed_fallbacks=roles.general_reviewer.allowed_fallbacks,
                **common,
            ),
            PlanExpanderAdapter(
                planning_runner,
                assigner,
                model=roles.plan_expander.model,
                effort=roles.plan_expander.effort,
                allowed_fallbacks=roles.plan_expander.allowed_fallbacks,
                inspection_provider_contract=self.inspection_contract,
                **common,
            ),
            PlanReviewerAdapter(
                planning_runner,
                model=roles.general_reviewer.model,
                effort=roles.general_reviewer.effort,
                allowed_fallbacks=roles.general_reviewer.allowed_fallbacks,
                critical_model=roles.critical_reviewer.model,
                critical_effort=roles.critical_reviewer.effort,
                critical_allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks,
                inspection_provider_contract=self.inspection_contract,
                **common,
            ),
        ).search(
            goal=active_goal,
            state=state,
            project_map=project_map,
            candidate_count=candidate_count,
            recovery_policy=(
                PlanningRecoveryPolicy()
                if self.inspection_contract == PLAN_INSPECTION_PROVIDER_V2
                else None
            ),
        )
        for evaluation in planning.skeleton_evaluations:
            self.service.record_skeleton_evaluation(evaluation)
        for evaluation in planning.plan_evaluations:
            self.service.register_plan_evaluation(evaluation)
        search_id = self.service.record_planning_search(planning)
        return EnginePreparationResult(
            status=(
                "ready_for_authorization"
                if planning.selected_activation_digest is not None
                else "planning_blocked"
            ),
            project_id=project_id,
            goal_preparation=goal_preparation,
            planning=planning,
            planning_search_id=search_id,
        )

    def authorize(
        self,
        project_id: str,
        *,
        source: str,
        operating_policy: GoalOperatingPolicy | None = None,
        capability: GoalAuthorizationCapability | None = None,
        authorization_target: GoalAuthorizationTarget | None = None,
        selected_plan_revision_id: str | None = None,
    ) -> EngineAuthorizationResult:
        """사용자 승인 경계를 기록하고 Core 선택 Plan을 자동 활성화한다."""

        authorization, activation_id = self.service.authorize_goal_and_activate_plan(
            project_id=project_id,
            source=source,
            operating_policy=operating_policy,
            capability=capability,
            authorization_target=authorization_target,
            expected_plan_revision_id=selected_plan_revision_id,
        )
        return EngineAuthorizationResult(
            project_id=project_id,
            authorization=authorization,
            activation_id=activation_id,
        )

    def authorization_target(
        self,
        project_id: str,
        *,
        operating_policy: GoalOperatingPolicy | None = None,
    ) -> GoalAuthorizationTarget:
        """현재 사용자 승인 대상 전체를 상태 변경 없이 반환한다."""

        return self.service.goal_authorization_target(
            project_id=project_id,
            operating_policy=operating_policy,
        )

    def read_current_approved_role_slot_source(
        self, project_id: str, task_id: str, expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """VM Core의 현재 승인 snapshot을 trusted adapter에 제공한다."""
        return self.service.read_current_approved_role_slot_source(project_id, task_id, expected)

    def revoke_approved_role_slots(self, project_id: str, *, reason: str) -> None:
        self.service.revoke_approved_role_slots(project_id, reason=reason)

    def _dispatcher(self):
        if self.runtime is None:
            raise EngineApplicationError("RUNTIME_REQUIRED")
        from .runtime import EngineDispatcher

        provider = None
        recovery_provider = None
        if self.role_configuration is not None:
            _runtime, roles, runner = self._execution_components()
            from .execution import ExecutionProposalAdapter
            from .recovery_planning import RecoveryPlanProvider

            provider = ExecutionProposalAdapter(self.service, runner, roles)
            # 역할 설정이 있을 때만 실제 Plan subgraph 재계획·독립 검토를 연결한다.
            # 설정이 없으면 Core는 REPLAN_PROVIDER_REQUIRED로 명시 정지한다.
            recovery_provider = RecoveryPlanProvider(
                self.service,
                runner,
                roles,
                inspection_contract=self.inspection_contract,
            )
        return EngineDispatcher(
            self.service,
            self.runtime,
            proposal_provider=provider,
            supervisor=self.supervisor,
            recovery_provider=recovery_provider,
            task_gate=self.task_gate(),
        )

    def task_gate(self) -> Any:
        """활성화 뒤 실행 Task가 반드시 지나는 governance gate. 설정이 없으면 GOVERNANCE_GATE_REQUIRED로 막는다."""
        if self._task_gate is None:
            from .governance_gate import MissingGovernanceGate

            if self.governance is None:
                self._task_gate = MissingGovernanceGate()
            else:
                runner = None if self.role_configuration is None else self._execution_components()[2]
                self._task_gate = self.governance.open_gate(
                    self.service, runtime=self.runtime, roles=self.role_configuration, runner=runner
                )
        return self._task_gate

    def close_task_gate(self) -> None:
        if self._task_gate is not None:
            self._task_gate.close()
            self._task_gate = None

    def run_once(
        self,
        project_id: str,
        *,
        resume: bool = False,
        proposal: ExecutionSpecProposal | None = None,
        goal_validation_step: ValidationExecutionStep | None = None,
        goal_validation_retry: GoalValidationRetryRequest | None = None,
    ) -> RunOnceResult:
        """한 scheduler tick만 수행하며 중복 호출은 Core checkpoint로 수렴한다."""

        control = self.service.workflow_control_state(project_id)
        if control == "cancelled":
            return RunOnceResult(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                blocker_code="WORKFLOW_CANCELLED",
                detail="현재 Goal workflow가 사용자 요청으로 취소됐습니다.",
            )
        if control == "paused":
            if not resume:
                return RunOnceResult(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    blocker_code="WORKFLOW_PAUSED",
                    detail="현재 Goal workflow가 일시정지됐습니다. 명시적으로 resume하십시오.",
                )
            self.service.set_workflow_control(
                project_id,
                state="running",
                reason="명시적 run_once resume",
            )
        return self._dispatcher().run_once(
            project_id,
            proposal=proposal,
            goal_validation_step=goal_validation_step,
            goal_validation_retry=goal_validation_retry,
        )

    def observe(self, project_id: str) -> dict[str, Any]:
        """active job을 최대 한 번 관측하되 Core 완료 판정을 대신하지 않는다."""

        if self.supervisor is None:
            raise EngineApplicationError("RUNTIME_REQUIRED")
        job = self.service.active_runtime_job(project_id)
        cancelled = self.service.workflow_control_state(project_id) == "cancelled"
        if job is None and cancelled:
            job = self.service.unsettled_cancelled_runtime_job(project_id)
        if job is not None:
            if cancelled and job.status in {
                RuntimeJobStatus.CANCELLED,
                RuntimeJobStatus.PROVIDER_TERMINAL,
            }:
                if job.status is RuntimeJobStatus.CANCELLED:
                    job = self._dispatcher().finalize_cancelled_runtime_job(job.job_id)
                    if job.status is RuntimeJobStatus.CANCELLED:
                        try:
                            job = self.supervisor.observe_cancelled(job.job_id)
                        except Exception as error:
                            job = self._dispatcher().finalize_cancelled_runtime_job(
                                job.job_id,
                                observation_error=error,
                            )
                if job.status is RuntimeJobStatus.PROVIDER_TERMINAL:
                    job = self._dispatcher().finalize_cancelled_runtime_job(job.job_id)
            elif job.status is RuntimeJobStatus.COLLECTOR_LOST and job.thread_id is not None:
                job = self.supervisor.reattach(job.job_id)
            else:
                job = self.supervisor.tick(job.job_id)
        return {
            "project_id": project_id,
            "runtime_job": None if job is None else job.model_dump(mode="json"),
            "status": self.status(project_id),
        }

    def pause(self, project_id: str, *, reason: str = "사용자 요청") -> dict[str, Any]:
        """현재 Goal의 후속 tick을 멈추고 active provider job에는 bounded interrupt를 요청한다."""

        job = self.service.active_runtime_job(project_id)
        if job is not None and self.supervisor is None:
            raise EngineApplicationError("RUNTIME_REQUIRED")
        state = self.service.set_workflow_control(project_id, state="paused", reason=reason)
        if job is not None:
            job = self.supervisor.request_interrupt(
                job.job_id, reason="workflow_paused",
            )
        return {
            "project_id": project_id,
            "control_state": state,
            "runtime_job": None if job is None else job.model_dump(mode="json"),
        }

    def cancel(self, project_id: str, *, reason: str = "사용자 요청") -> dict[str, Any]:
        """현재 Goal을 취소하고 active job을 취소하되 terminal 관측을 발명하지 않는다."""

        job = self.service.active_runtime_job(project_id)
        if job is not None and self.supervisor is None:
            raise EngineApplicationError("RUNTIME_REQUIRED")
        state = self.service.set_workflow_control(project_id, state="cancelled", reason=reason)
        if job is not None:
            job = self.supervisor.cancel(job.job_id, reason=reason)
        return {
            "project_id": project_id,
            "control_state": state,
            "runtime_job": None if job is None else job.model_dump(mode="json"),
        }

    @staticmethod
    def _status_target_plan(connection: Any, project: dict[str, Any]) -> PlanContractRevision | None:
        plan_revision_id = project.get("active_plan_revision_id")
        row = None
        if plan_revision_id is not None:
            row = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id=? AND project_id=?",
                (plan_revision_id, project["id"]),
            ).fetchone()
        else:
            selection = connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id=? "
                "AND event_type='planning.search_recorded' ORDER BY sequence DESC LIMIT 1",
                (project["id"],),
            ).fetchone()
            selected_digest = None
            if selection is not None:
                try:
                    selected_digest = json.loads(selection["payload_json"])["outcome"].get(
                        "selected_activation_digest"
                    )
                except (KeyError, TypeError, ValueError):
                    selected_digest = None
            if selected_digest is not None:
                row = connection.execute(
                    "SELECT payload_json FROM plan_revisions WHERE project_id=? "
                    "AND activation_digest=? AND status='ready'",
                    (project["id"], selected_digest),
                ).fetchone()
            if row is None:
                ready = connection.execute(
                    "SELECT payload_json FROM plan_revisions WHERE project_id=? AND status='ready'",
                    (project["id"],),
                ).fetchall()
                row = ready[0] if len(ready) == 1 else None
        return None if row is None else PlanContractRevision.model_validate_json(row["payload_json"])

    def _execution_observation_summary(
        self,
        project_id: str,
        *,
        usage: UsageSummary | None,
        goal_id: str | None = None,
    ) -> ExecutionObservationSummary:
        records: tuple[UsageReadRecord, ...] = (
            ()
            if usage is None
            else (*usage.usage_records, *usage.provider_receipt_usage)
        )
        model_observations = []
        for item in records:
            provenance = getattr(item, "binding_provenance", None) or {}
            model_observations.append(
                ModelObservationItem(
                    logical_call_ref=item.logical_call_ref,
                    role=item.role,
                    stage=item.stage.value,
                    requested_model=getattr(item, "requested_model", None) or item.model,
                    requested_effort=getattr(item, "requested_effort", None) or item.effort,
                    observed_model=getattr(item, "observed_model", None),
                    observed_effort=getattr(item, "observed_effort", None),
                    observed_source=(
                        provenance.get("observed")
                        if isinstance(provenance, dict)
                        else None
                    ),
                )
            )

        metrics = (
            ()
            if usage is None
            else (
                ("input_tokens", usage.input_tokens),
                ("cached_input_tokens", usage.cached_input_tokens),
                ("output_tokens", usage.output_tokens),
                ("reasoning_tokens", usage.reasoning_tokens),
            )
        )
        missing_components = tuple(name for name, metric in metrics if metric.total is None)
        has_calls = bool(model_observations) or (
            usage is not None and usage.provider_call_count not in {None, 0}
        )
        has_observed_usage = any(item.usage_available for item in records)
        incomplete = (
            usage is not None
            and bool(
                missing_components
                or usage.incomplete_reasons
                or usage.provider_calls_without_usage
                or usage.missing_expected_logical_call_refs
            )
        )
        if not has_calls:
            usage_status = "not_observed"
        elif incomplete:
            usage_status = "partial" if has_observed_usage else "missing"
        else:
            usage_status = "complete"

        parameters: tuple[Any, ...] = (project_id,)
        goal_filter = ""
        if goal_id is not None:
            goal_filter = " AND goal_id=?"
            parameters = (project_id, goal_id)
        with self.service.ledger.read() as connection:
            effect_rows = connection.execute(
                "SELECT id,effect_status FROM provider_calls WHERE project_id=?"
                + goal_filter
                + " ORDER BY created_at,rowid",
                parameters,
            ).fetchall()
            unknown_attempts = connection.execute(
                "SELECT id FROM attempts WHERE project_id=? AND failure_class='external_unknown' "
                "AND plan_revision_id=(SELECT active_plan_revision_id FROM projects WHERE id=?) "
                "ORDER BY created_at,rowid",
                (project_id, project_id),
            ).fetchall()
            unknown_intents = connection.execute(
                "SELECT i.id FROM runtime_intents i JOIN attempts a ON a.id=i.attempt_id "
                "WHERE a.project_id=? AND a.plan_revision_id="
                "(SELECT active_plan_revision_id FROM projects WHERE id=?) "
                "AND i.status='unknown' ORDER BY i.prepared_at,i.rowid",
                (project_id, project_id),
            ).fetchall()
        unknown_refs = tuple(
            sorted(
                {
                    *(row["id"] for row in effect_rows if row["effect_status"] == "unknown"),
                    *(row["id"] for row in unknown_attempts),
                    *(row["id"] for row in unknown_intents),
                }
            )
        )
        pending_refs = tuple(
            row["id"] for row in effect_rows if row["effect_status"] == "pending"
        )
        confirmed_refs = tuple(
            row["id"] for row in effect_rows if row["effect_status"] == "terminal"
        )
        if unknown_refs:
            effect_status = "unknown"
            effect_refs = unknown_refs
            effect_reason = (
                "원장에 외부 효과 완료를 확정할 직접 관측이 없어 자동 재실행하지 않습니다."
            )
        elif pending_refs:
            effect_status = "pending"
            effect_refs = pending_refs
            effect_reason = "외부 효과 intent가 terminal confirmation을 기다리고 있습니다."
        elif confirmed_refs:
            effect_status = "confirmed"
            effect_refs = confirmed_refs
            effect_reason = "typed receipt와 대상 재관측에 결속된 외부 효과가 있습니다."
        else:
            effect_status = "none"
            effect_refs = ()
            effect_reason = "현재 Goal 범위에 미확정 외부 효과가 없습니다."
        return ExecutionObservationSummary(
            model_observations=tuple(model_observations),
            usage_status=usage_status,
            usage_missing_components=missing_components,
            usage_incomplete_reasons=() if usage is None else usage.incomplete_reasons,
            external_effect_status=effect_status,
            external_effect_unknown=effect_status == "unknown",
            external_effect_refs=effect_refs,
            external_effect_reason=effect_reason,
        )

    def _status_drill_down(
        self,
        project_id: str,
        *,
        snapshot: dict[str, Any],
        plan: PlanContractRevision | None,
    ) -> tuple[dict[str, Any], bool]:
        plan_revision_id = None if plan is None else plan.plan_revision_id
        target_task_ids = set() if plan is None else {
            item.task_id for item in plan.definition.tasks
        }
        target_task_statuses = tuple(
            task["status"] for task in snapshot["tasks"] if task["id"] in target_task_ids
        )
        execution_started = any(
            status in {"reserved", "running", "validating", "completed", "failed", "blocked"}
            for status in target_task_statuses
        )
        all_tasks_completed = bool(target_task_statuses) and all(
            status == "completed" for status in target_task_statuses
        )
        project_map = None
        validation_rows = ()
        evidence_rows = ()
        reuse_rows = ()
        with self.service.ledger.read() as connection:
            if plan is not None:
                row = connection.execute(
                    "SELECT payload_json FROM project_map_revisions WHERE project_id=? "
                    "AND revision_digest=?",
                    (project_id, plan.definition.project_map_digest),
                ).fetchone()
                if row is not None:
                    from .domain import ProjectMapRevision

                    project_map = ProjectMapRevision.model_validate_json(row["payload_json"])
                validation_rows = connection.execute(
                    "SELECT task_id,validation_id,status,payload_json,evaluated_at,rowid "
                    "FROM validation_results WHERE project_id=? AND plan_revision_id=? "
                    "ORDER BY evaluated_at,rowid",
                    (project_id, plan_revision_id),
                ).fetchall()
                task_ids = tuple(item.task_id for item in plan.definition.tasks)
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    reuse_rows = connection.execute(
                        f"SELECT task_id,source_task_id,evidence_ids_json FROM task_completion_reuse "
                        f"WHERE task_id IN ({placeholders})",
                        task_ids,
                    ).fetchall()
            if project_map is None:
                row = connection.execute(
                    "SELECT payload_json FROM project_map_revisions WHERE project_id=? "
                    "AND is_current=1",
                    (project_id,),
                ).fetchone()
                if row is not None:
                    from .domain import ProjectMapRevision

                    project_map = ProjectMapRevision.model_validate_json(row["payload_json"])
            elif all_tasks_completed:
                current_row = connection.execute(
                    "SELECT payload_json FROM project_map_revisions WHERE project_id=? "
                    "AND is_current=1",
                    (project_id,),
                ).fetchone()
                if current_row is not None:
                    from .domain import ProjectMapRevision

                    project_map = ProjectMapRevision.model_validate_json(
                        current_row["payload_json"]
                    )
            evidence_rows = connection.execute(
                "SELECT e.id,e.task_id,e.attempt_id,e.kind,e.source_ref,e.content_digest,"
                "t.plan_revision_id FROM evidence_records e LEFT JOIN task_contracts t "
                "ON t.id=e.task_id WHERE e.project_id=? ORDER BY e.observed_at,e.rowid",
                (project_id,),
            ).fetchall()

        files = []
        if project_map is not None:
            root = Path(project_map.root)
            for entry in project_map.entries:
                path = Path(entry.path)
                resolved = path if path.is_absolute() else root / path
                try:
                    observed_digest = sha256_bytes(resolved.read_bytes())
                except OSError:
                    observed_digest = None
                valid = observed_digest == entry.content_digest
                validity = (
                    "current"
                    if valid
                    else ("pending" if execution_started else "invalidated")
                )
                files.append(
                    {
                        "entry_id": entry.entry_id,
                        "path": entry.path,
                        "kind": entry.kind.value,
                        "record_status": "preserved",
                        "validity": validity,
                        "expected_digest": entry.content_digest,
                        "observed_digest": observed_digest,
                        "reason": (
                            "ProjectMap에 결속된 파일 digest와 현재 파일이 일치합니다."
                            if valid
                            else (
                                "실행이 시작된 뒤 파일이 바뀌어 validation 또는 State 재관측을 기다립니다."
                                if execution_started
                                else "ProjectMap에 결속된 파일이 없거나 현재 digest가 달라졌습니다."
                            )
                        ),
                    }
                )
        stale = any(item["validity"] == "invalidated" for item in files)

        reused_evidence_ids = {
            evidence_id
            for row in reuse_rows
            for evidence_id in json.loads(row["evidence_ids_json"])
        }
        tasks = []
        for task in snapshot["tasks"]:
            current = (
                task["id"] in target_task_ids
                and task["status"] != "superseded"
                and not stale
            )
            tasks.append(
                {
                    "task_id": task["id"],
                    "task_ref": task["task_ref"],
                    "plan_revision_id": task["plan_revision_id"],
                    "status": task["status"],
                    "record_status": "preserved",
                    "validity": "current" if current else "invalidated",
                    "reason": (
                        "현재 선택/활성 Plan과 파일 baseline에 결속된 Task입니다."
                        if current
                        else (
                            "현재 파일이 Plan baseline과 달라 Task 입력 결속이 무효화됐습니다."
                            if task["id"] in target_task_ids and stale
                            else "현재 선택/활성 Plan 밖의 Task 이력으로 보존됩니다."
                        )
                    ),
                }
            )

        latest_results: dict[tuple[str | None, str], Any] = {}
        for row in validation_rows:
            latest_results[(row["task_id"], row["validation_id"])] = row
        checks = []
        referenced_evidence_ids: set[str] = set(reused_evidence_ids)
        if plan is not None:
            contracts = [
                ("task", task.task_id, contract)
                for task in plan.definition.tasks
                for contract in task.validations
            ] + [
                ("goal", None, contract)
                for contract in plan.definition.integration_validations
            ]
            for scope, task_id, contract in contracts:
                row = latest_results.get((task_id, contract.validation_id))
                payload = None if row is None else json.loads(row["payload_json"])
                evidence_ids = tuple(() if payload is None else payload.get("evidence_ids", ()))
                referenced_evidence_ids.update(evidence_ids)
                checks.append(
                    {
                        "validation_id": contract.validation_id,
                        "scope": scope,
                        "task_id": task_id,
                        "method": contract.method,
                        "status": "not_run" if row is None else row["status"],
                        "record_status": "not_recorded" if row is None else "preserved",
                        "validity": (
                            "invalidated" if stale else ("pending" if row is None else "current")
                        ),
                        "evidence_ids": evidence_ids,
                        "reason": (
                            "현재 파일이 Plan baseline과 달라 검사 결속이 무효화됐습니다."
                            if stale
                            else (
                                "아직 Core에 검사 결과가 기록되지 않았습니다."
                                if row is None
                                else "현재 Plan에 결속된 최신 검사 결과입니다."
                            )
                        ),
                    }
                )

        evidence = []
        for row in evidence_rows:
            current_plan_evidence = (
                row["plan_revision_id"] == plan_revision_id
                or row["id"] in referenced_evidence_ids
            )
            valid = current_plan_evidence and not stale
            evidence.append(
                {
                    "evidence_id": row["id"],
                    "task_id": row["task_id"],
                    "attempt_id": row["attempt_id"],
                    "kind": row["kind"],
                    "source_ref": row["source_ref"],
                    "content_digest": row["content_digest"],
                    "record_status": "preserved",
                    "validity": "current" if valid else "invalidated",
                    "reason": (
                        "현재 Plan의 검사 또는 completion reuse에 결속된 evidence입니다."
                        if valid
                        else (
                            "현재 파일이 Plan baseline과 달라 evidence의 현재 적용이 무효화됐습니다."
                            if current_plan_evidence and stale
                            else "현재 선택/활성 Plan 밖의 historical evidence로 보존됩니다."
                        )
                    ),
                }
            )
        return {
            "tasks": tasks,
            "files": files,
            "checks": checks,
            "evidence": evidence,
        }, stale

    @staticmethod
    def _status_stage(
        *,
        project: dict[str, Any],
        control_state: str,
        active_job: RuntimeJob | None,
        plan: PlanContractRevision | None,
        task_counts: dict[str, int],
        stale: bool,
        external_unknown: bool,
        has_goal_verdict: bool,
    ) -> tuple[str, str, str]:
        if control_state == "cancelled":
            return "cancelled", "현재 Goal workflow가 사용자 요청으로 취소됐습니다.", "새 Goal을 prepare하십시오."
        if control_state == "paused":
            return "paused", "현재 Goal workflow의 후속 tick이 일시정지됐습니다.", "run-once --resume을 명시적으로 호출하십시오."
        if external_unknown or project.get("run_state") == "recovery_required":
            return (
                "external_effect_unknown",
                "완료가 확인되지 않은 외부 effect가 있어 새 실행을 시작할 수 없습니다.",
                "recover inspect로 기존 intent와 receipt를 먼저 대조하십시오.",
            )
        if stale:
            return (
                "stale_execution_input",
                "Plan에 결속된 파일 baseline과 현재 파일이 달라 실행 입력이 stale입니다.",
                "변경 파일을 검토하고 Execution Spec 또는 Plan revision을 다시 준비하십시오.",
            )
        if active_job is not None:
            stages = {
                "execution_spec_prepare": "preparing_execution",
                "worker_turn": "executing_task",
                "task_semantic_validate": "validating_task",
                "goal_test_prepare": "preparing_goal_validation",
                "goal_semantic_validate": "validating_goal",
                "recovery": "recovering",
                "replanning": "replanning",
            }
            return (
                stages.get(active_job.kind.value, "observing_runtime_job"),
                f"{active_job.kind.value} RuntimeJob이 {active_job.status.value} 상태입니다.",
                "observe로 같은 RuntimeJob을 먼저 관측하십시오.",
            )
        if project.get("run_state") == "completed" or has_goal_verdict:
            return "completed", "Core가 GoalVerdict를 기록해 workflow가 terminal 상태입니다.", "final-report를 확인하십시오."
        if project.get("active_goal_revision_id") is None:
            return "preparation_required", "활성 GoalContract가 아직 없습니다.", "raw request를 prepare하십시오."
        if project.get("active_plan_revision_id") is None:
            if plan is not None:
                return (
                    "authorization_required",
                    "검토를 통과한 선택 Plan이 있지만 GoalAuthorization이 아직 없습니다.",
                    "authorize에서 표시된 전체 target digest를 확인하십시오.",
                )
            return "planning_required", "활성 Goal에 실행 가능한 선택 Plan이 없습니다.", "prepare 결과의 질문·planning finding을 해소하십시오."
        if task_counts.get("failed", 0) or task_counts.get("blocked", 0):
            return "recovery_required", "현재 Plan의 Task가 실패 또는 차단 상태입니다.", "status 근거를 확인하고 허용된 recovery 경로를 선택하십시오."
        if task_counts.get("validating", 0):
            return "validating_task", "Worker 결과 뒤 Task validation이 남아 있습니다.", "run-once로 다음 validation 단계를 수행하십시오."
        if task_counts.get("materialized", 0):
            return "task_ready_to_dispatch", "한 Task의 Execution Spec이 current 입력에 결속됐습니다.", "run-once로 그 Task 하나를 dispatch하십시오."
        if task_counts.get("reserved", 0) or task_counts.get("running", 0):
            return "executing_task", "실행 Attempt가 진행 중이며 후속 관측이 필요합니다.", "run-once로 기존 Attempt를 먼저 관측하십시오."
        if task_counts.get("ready", 0):
            return "task_ready", "dependency를 만족한 Task 하나가 실행 준비 상태입니다.", "run-once로 Task 실행 명세 준비를 시작하십시오."
        if plan is not None and task_counts and task_counts.get("completed", 0) == sum(task_counts.values()):
            return "goal_validation_ready", "모든 Task가 완료되어 독립 Goal validation이 남아 있습니다.", "run-once로 Goal Test를 준비·실행하십시오."
        return "waiting", "Core 원장은 다음 결정적 전이를 기다리고 있습니다.", "status drill_down의 Task와 검사를 확인하십시오."

    _MODEL_REPORTED_NOTE = (
        "모델 자기보고 code는 진단 가설이며 자동 복구를 시작하는 근거가 아닙니다."
    )

    def _recovery_evidence_documents(
        self, attempt_id: str, *, evidence_ids: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
        """실패 evidence 원문을 읽기 전용으로 읽는다. 실행 경로와 같은 순서를 쓴다."""

        with self.service.ledger.read() as connection:
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                rows = connection.execute(
                    f"SELECT id,payload_json FROM evidence_records WHERE id IN ({placeholders}) "
                    "ORDER BY observed_at,rowid",
                    tuple(evidence_ids),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,payload_json FROM evidence_records WHERE attempt_id=? "
                    "ORDER BY observed_at,rowid",
                    (attempt_id,),
                ).fetchall()
        return (
            tuple(row["id"] for row in rows),
            tuple(json.loads(row["payload_json"]) for row in rows),
        )

    def _recovery_failure(
        self, project_id: str, *, plan_revision_id: str | None,
    ) -> tuple[dict[str, Any], FailureDiagnosis] | None:
        """현재 미해결 실패 하나와 그 원장 분류를 실행 경로와 같은 규칙으로 읽는다."""

        if plan_revision_id is None:
            return None
        classifier = EvidenceFirstFailureClassifier()
        with self.service.ledger.read() as connection:
            failed = connection.execute(
                "SELECT t.id AS task_id, a.id AS attempt_id, a.failure_class, a.failure_detail "
                "FROM task_contracts t JOIN attempts a ON a.id = ("
                "SELECT latest.id FROM attempts latest WHERE latest.task_id = t.id "
                "AND latest.kind = 'execution' "
                "ORDER BY latest.attempt_no DESC, latest.rowid DESC LIMIT 1"
                ") WHERE t.plan_revision_id = ? AND t.status IN ('failed','blocked') "
                "AND a.failure_class IS NOT NULL "
                "ORDER BY t.position LIMIT 1",
                (plan_revision_id,),
            ).fetchone()
        if failed is not None:
            failure_class = FailureClass(failed["failure_class"])
            evidence_ids, documents = self._recovery_evidence_documents(failed["attempt_id"])
            observed = terminal_failure_diagnosis(documents, classifier=classifier)
            matched = observed is not None and observed.failure_class is failure_class
            diagnosis = FailureDiagnosis(
                failure_class=failure_class,
                repair_action=self.service.repair_action_for(failure_class),
                provider_error_code=observed.provider_error_code if matched else None,
                local_engine_code=observed.local_engine_code if matched else None,
                evidence_ids=evidence_ids,
                rationale=failed["failure_detail"] or "원장에 기록된 Attempt 실패",
                source=(
                    "unclassified"
                    if failure_class is FailureClass.UNCLASSIFIED
                    else "direct_evidence"
                ),
                model_reported_codes=(
                    () if observed is None else observed.model_reported_codes
                ),
                transient=bool(matched and observed.transient),
            )
            return (
                {
                    "task_id": failed["task_id"],
                    "attempt_id": failed["attempt_id"],
                    "validation_result_id": None,
                },
                diagnosis,
            )
        validation_failure = self.service.task_validation_recovery_blocker(
            project_id, plan_revision_id=plan_revision_id
        )
        if validation_failure is None:
            return None
        evidence_ids, documents = self._recovery_evidence_documents(
            validation_failure["attempt_id"],
            evidence_ids=tuple(validation_failure["evidence_ids"]),
        )
        diagnosis = classifier.classify(FailureSignal(
            terminal_status="validation_failed",
            final_response=None,
            provider_payload={
                "validation_result_id": validation_failure["validation_result_id"]
            },
            evidence_ids=evidence_ids,
            evidence_documents=documents,
            failed_attempt_id=validation_failure["attempt_id"],
            failed_validation_id=validation_failure["validation_id"],
        ))
        return (
            {
                "task_id": validation_failure["task_id"],
                "attempt_id": validation_failure["attempt_id"],
                "validation_result_id": validation_failure["validation_result_id"],
            },
            diagnosis,
        )

    def _recovery_scope(self, project_id: str, *, active_plan_revision_id: str | None) -> RecoveryScope:
        """원장이 보존한 기록과 새 revision이 대체한 범위를 나눠 읽는다."""

        with self.service.ledger.read() as connection:
            attempts = connection.execute(
                "SELECT id FROM attempts WHERE project_id=? ORDER BY rowid", (project_id,)
            ).fetchall()
            evidence = connection.execute(
                "SELECT id FROM evidence_records WHERE project_id=? ORDER BY observed_at,rowid",
                (project_id,),
            ).fetchall()
            assessments = connection.execute(
                "SELECT id FROM recovery_assessments WHERE project_id=? ORDER BY created_at,rowid",
                (project_id,),
            ).fetchall()
            receipts = connection.execute(
                "SELECT i.id FROM runtime_intents i JOIN runtime_receipts r ON r.intent_id=i.id "
                "JOIN attempts a ON a.id=i.attempt_id WHERE a.project_id=? ORDER BY i.rowid",
                (project_id,),
            ).fetchall()
            superseded_plans = connection.execute(
                "SELECT id FROM plan_revisions WHERE project_id=? AND status='superseded' "
                "ORDER BY revision_no",
                (project_id,),
            ).fetchall()
            superseded_tasks = connection.execute(
                "SELECT id FROM task_contracts WHERE project_id=? AND status='superseded' "
                "ORDER BY rowid",
                (project_id,),
            ).fetchall()
        return RecoveryScope(
            preserved_attempt_ids=tuple(row["id"] for row in attempts),
            preserved_evidence_ids=tuple(row["id"] for row in evidence),
            preserved_assessment_ids=tuple(row["id"] for row in assessments),
            preserved_receipt_intent_ids=tuple(row["id"] for row in receipts),
            superseded_plan_revision_ids=tuple(row["id"] for row in superseded_plans),
            superseded_task_ids=tuple(row["id"] for row in superseded_tasks),
            active_plan_revision_id=active_plan_revision_id,
        )

    @staticmethod
    def _recovery_codes(diagnosis: FailureDiagnosis) -> tuple[RecoveryCodeObservation, ...]:
        codes: list[RecoveryCodeObservation] = []
        if diagnosis.provider_error_code is not None:
            codes.append(RecoveryCodeObservation(
                values=(diagnosis.provider_error_code,),
                provenance="provider_observed",
                authoritative=True,
                note="provider payload가 직접 반환한 error code입니다.",
            ))
        if diagnosis.local_engine_code is not None:
            codes.append(RecoveryCodeObservation(
                values=(diagnosis.local_engine_code,),
                provenance="local_derived",
                authoritative=True,
                note="로컬 Engine이 직접 관측해 만든 code입니다.",
            ))
        if diagnosis.model_reported_codes:
            codes.append(RecoveryCodeObservation(
                values=diagnosis.model_reported_codes,
                provenance="model_reported",
                authoritative=False,
                note=EngineApplication._MODEL_REPORTED_NOTE,
            ))
        return tuple(codes)

    def recovery_explanation(self, project_id: str) -> RecoveryExplanation:
        """분류 근거·보존/폐기 범위·다음 동작을 원장에서 읽기 전용으로 설명한다.

        실행 경로(`EngineDispatcher`)와 같은 `recovery_route`·`recovery_limit_decision`·
        `replan_candidate_blocker`를 사용하므로 표시된 다음 동작과 실제 다음 `run_once`
        결과가 갈라지지 않는다.
        """

        project = self._project_row(project_id)
        plan_revision_id = project["active_plan_revision_id"]
        scope = self._recovery_scope(project_id, active_plan_revision_id=plan_revision_id)
        with self.service.ledger.read() as connection:
            assessments = tuple(
                RecoveryAssessment.model_validate_json(row["payload_json"])
                for row in connection.execute(
                    "SELECT payload_json FROM recovery_assessments WHERE project_id=? "
                    "ORDER BY created_at,rowid",
                    (project_id,),
                )
            )
        failure = self._recovery_failure(project_id, plan_revision_id=plan_revision_id)
        if failure is None:
            recovered = bool(assessments) or bool(scope.superseded_plan_revision_ids)
            return RecoveryExplanation(
                state="recovered" if recovered else "none",
                scope=scope,
                assessments=assessments,
                next_action=RecoveryNextAction(
                    mode="none",
                    detail=(
                        "미해결 실패가 없습니다. 기존 Attempt·receipt·evidence는 보존되고 "
                        "대체된 Plan revision과 Task만 superseded로 남습니다."
                        if recovered
                        else "현재 자동 복구가 필요한 실패가 없습니다."
                    ),
                ),
            )
        target, diagnosis = failure
        route = recovery_route(
            diagnosis, validation_result_id=target["validation_result_id"]
        )
        limits = None
        limit_detail = None
        replan_blocker = None
        if route.mode == "automatic":
            from .runtime import replan_candidate_blocker

            with self.service.ledger.read() as connection:
                observation = recovery_limit_decision(
                    connection,
                    project_id=project_id,
                    task_id=target["task_id"],
                    attempt_id=target["attempt_id"],
                    diagnosis=diagnosis,
                )
            limits = RecoveryLimitStatus.model_validate(
                observation.model_dump(mode="json")
            )
            limit_detail = observation.detail
            # 소비된 재계획 후보의 차단 판정도 run_once와 같은 helper로 읽는다.
            replan_blocker = replan_candidate_blocker(
                self.service, project_id=project_id, assessment_id=observation.assessment_id,
            )
        classification = RecoveryClassification(
            task_id=target["task_id"],
            attempt_id=target["attempt_id"],
            validation_result_id=target["validation_result_id"],
            failure_class=(
                None if diagnosis.failure_class is None else diagnosis.failure_class.value
            ),
            basis=diagnosis.source,
            rationale=diagnosis.rationale,
            transient=diagnosis.transient,
            evidence_ids=diagnosis.evidence_ids,
            codes=self._recovery_codes(diagnosis),
        )
        if route.mode == "unrouted":
            next_action = RecoveryNextAction(
                mode="user_decision",
                blocker_code="TASK_VALIDATION_RECOVERY_REQUIRED",
                detail=(
                    "직접 근거로 원인을 분류하지 못했습니다. 실패 validation과 현재 Worker "
                    "evidence를 근거로 RecoveryAssessment를 명시해 재시도하십시오."
                ),
            )
            state = "user_decision_required"
        elif route.mode == "observe_first":
            next_action = RecoveryNextAction(
                mode="observe_first",
                blocker_code="EXTERNAL_EFFECT_UNKNOWN",
                suggested_repair_action=(
                    None if route.suggested_repair_action is None
                    else route.suggested_repair_action.value
                ),
                checkpoint_required=route.checkpoint_required,
                detail=route.detail,
            )
            state = "observe_first_required"
        elif route.mode == "user_decision":
            next_action = RecoveryNextAction(
                mode="user_decision",
                blocker_code=route.blocker_code,
                suggested_repair_action=(
                    None if route.suggested_repair_action is None
                    else route.suggested_repair_action.value
                ),
                checkpoint_required=route.checkpoint_required,
                detail=route.detail,
            )
            state = "user_decision_required"
        elif limits is not None and limits.limit_code is not None:
            next_action = RecoveryNextAction(
                mode="user_decision",
                blocker_code=limits.limit_code,
                suggested_repair_action=(
                    None if diagnosis.repair_action is None
                    else diagnosis.repair_action.value
                ),
                checkpoint_required=True,
                detail=limit_detail or "원장 복구 한도에 도달했습니다.",
            )
            state = "user_decision_required"
        elif replan_blocker is not None:
            next_action = RecoveryNextAction(
                mode="user_decision",
                blocker_code=replan_blocker[0],
                suggested_repair_action=RepairAction.SUBGRAPH_REPLAN.value,
                checkpoint_required=True,
                detail=replan_blocker[1][:2000],
            )
            state = "user_decision_required"
        else:
            next_action = RecoveryNextAction(
                mode="automatic",
                suggested_repair_action=(
                    None if diagnosis.repair_action is None
                    else diagnosis.repair_action.value
                ),
                detail=(
                    "run-once가 원장 한도 안에서 "
                    f"{'' if diagnosis.repair_action is None else diagnosis.repair_action.value}"
                    " 자동 복구를 이어서 수행합니다. 기존 Attempt·receipt·evidence는 보존됩니다."
                ),
            )
            state = "automatic_pending"
        return RecoveryExplanation(
            state=state,
            classification=classification,
            limits=limits,
            scope=scope,
            assessments=assessments,
            next_action=next_action,
        )

    def status(self, project_id: str) -> dict[str, Any]:
        """Core snapshot과 현재 단계·근거·다음 동작을 읽기 전용으로 설명한다."""

        snapshot = self.service.status(project_id)
        counts: dict[str, int] = {}
        for task in snapshot["tasks"]:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        project = snapshot["project"]
        control_state = self.service.workflow_control_state(project_id)
        active_job = self.service.active_runtime_job(project_id)
        with self.service.ledger.read() as connection:
            plan = self._status_target_plan(connection, project)
            goal_row = connection.execute(
                "SELECT goal_id FROM goal_revisions WHERE id=?",
                (project["active_goal_revision_id"],),
            ).fetchone() if project["active_goal_revision_id"] is not None else None
            verdict_row = connection.execute(
                "SELECT id FROM goal_verdicts WHERE project_id=? "
                "AND (? IS NULL OR plan_revision_id=?) ORDER BY evaluated_at DESC,rowid DESC LIMIT 1",
                (project_id, project["active_plan_revision_id"], project["active_plan_revision_id"]),
            ).fetchone()
        drill_down, stale = self._status_drill_down(
            project_id, snapshot=snapshot, plan=plan,
        )
        usage = None
        if goal_row is not None:
            try:
                usage = self.usage_summary(project_id, goal_id=goal_row["goal_id"])
            except EngineApplicationError as error:
                if str(error) != "GOAL_NOT_FOUND":
                    raise
        execution_summary = self._execution_observation_summary(
            project_id,
            usage=usage,
            goal_id=None if goal_row is None else goal_row["goal_id"],
        )
        stage, reason, next_action = self._status_stage(
            project=project,
            control_state=control_state,
            active_job=active_job,
            plan=plan,
            task_counts=counts,
            stale=stale,
            external_unknown=execution_summary.external_effect_unknown,
            has_goal_verdict=verdict_row is not None,
        )
        recovery = self.recovery_explanation(project_id)
        if stage == "recovery_required" and recovery.classification is not None:
            # 일반 문구 대신 원장 분류 근거와 실제 다음 동작을 그대로 노출한다.
            reason = (
                f"failure_class={recovery.classification.failure_class}; "
                f"근거={recovery.classification.basis}. "
                f"{recovery.classification.rationale or ''}"
            ).strip()
            next_action = recovery.next_action.detail
        return snapshot | {
            "current_stage": stage,
            "reason": reason,
            "next_action": next_action,
            "authorization_state": (
                "authorized"
                if project["active_plan_revision_id"] is not None
                else ("required" if plan is not None else "not_ready")
            ),
            "control_state": control_state,
            "active_runtime_job": (
                None if active_job is None else active_job.model_dump(mode="json")
            ),
            "task_status_counts": counts,
            "drill_down": drill_down,
            "execution_summary": execution_summary.model_dump(mode="json"),
            "recovery": recovery.model_dump(mode="json"),
        }

    def _project_row(self, project_id: str):
        with self.service.ledger.read() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise EngineApplicationError("PROJECT_NOT_FOUND")
        return row

    def _presentation(self, project_id: str, refs: Iterable[EntityRef]) -> ReadPresentation:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT sequence, id, event_hash, created_at FROM history_events "
                "WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return ReadPresentation(
            entity_refs=tuple(refs),
            history_cursor=HistoryCursor(
                project_id=project_id,
                sequence=0 if row is None else int(row["sequence"]),
                event_id=None if row is None else row["id"],
                event_hash=None if row is None else row["event_hash"],
                created_at=None if row is None else row["created_at"],
            ),
        )

    def _goal_rows(self, project_id: str, goal_id: str):
        with self.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT id, revision_no, definition_digest, payload_json FROM goal_revisions "
                "WHERE project_id = ? AND goal_id = ? ORDER BY revision_no, rowid",
                (project_id, goal_id),
            ).fetchall()
        if not rows:
            raise EngineApplicationError("GOAL_NOT_FOUND")
        return rows

    def usage_summary(
        self,
        project_id: str,
        *,
        goal_id: str | None = None,
        expected_logical_call_refs: Iterable[str] = (),
    ) -> UsageSummary:
        project = self._project_row(project_id)
        if goal_id is None:
            revision_id = project["active_goal_revision_id"]
            with self.service.ledger.read() as connection:
                if revision_id is not None:
                    row = connection.execute(
                        "SELECT goal_id FROM goal_revisions WHERE id = ?", (revision_id,)
                    ).fetchone()
                    if row is None:
                        raise EngineApplicationError("ACTIVE_GOAL_BINDING_BROKEN")
                    goal_id = row["goal_id"]
                else:
                    rows = connection.execute(
                        "SELECT DISTINCT goal_id FROM provider_calls "
                        "WHERE project_id=? AND status<>'released' ORDER BY goal_id",
                        (project_id,),
                    ).fetchall()
                    if not rows:
                        raise EngineApplicationError("ACTIVE_GOAL_NOT_FOUND")
                    if len(rows) != 1:
                        raise EngineApplicationError("GOAL_ID_REQUIRED")
                    goal_id = rows[0]["goal_id"]
        assert goal_id is not None
        with self.service.ledger.read() as connection:
            goals = connection.execute(
                "SELECT * FROM goal_revisions WHERE project_id=? AND goal_id=? "
                "ORDER BY revision_no, rowid",
                (project_id, goal_id),
            ).fetchall()
            provider_rows = connection.execute(
                "SELECT * FROM provider_calls WHERE project_id=? AND goal_id=? AND status<>'released' "
                "ORDER BY created_at, rowid",
                (project_id, goal_id),
            ).fetchall()
            provider_history_rows = connection.execute(
                "SELECT event_type, entity_id, payload_json, created_at FROM history_events "
                "WHERE project_id=? AND entity_type='provider_call' "
                "AND event_type IN ('budget.call_reserved','budget.call_observed','budget.call_settled') "
                "ORDER BY sequence",
                (project_id,),
            ).fetchall()
        if not goals and not provider_rows:
            raise EngineApplicationError("GOAL_NOT_FOUND")
        digests = tuple(row["definition_digest"] for row in goals)
        with self.service.ledger.read() as connection:
            if digests:
                placeholders = ", ".join("?" for _ in digests)
                usage_rows = connection.execute(
                    "SELECT payload_json FROM budget_usage WHERE project_id = ? "
                    f"AND goal_contract_digest IN ({placeholders}) ORDER BY recorded_at, rowid",
                    (project_id, *digests),
                ).fetchall()
            else:
                usage_rows = ()
            has_reconciliations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'usage_reconciliations'"
            ).fetchone() is not None
            reconciliation_rows = () if not has_reconciliations else connection.execute(
                "SELECT r.id, r.call_id, r.prior_usage_id, r.effective_usage_id, r.observation_digest "
                "FROM usage_reconciliations r JOIN provider_calls c ON c.id=r.call_id "
                "WHERE c.project_id=? AND c.goal_id=? ORDER BY r.created_at, r.rowid",
                (project_id, goal_id),
            ).fetchall()
        records = tuple(BudgetUsageRecord.model_validate_json(row["payload_json"]) for row in usage_rows)
        projected: list[ProviderReceiptUsage] = []
        projection_failures: dict[str, tuple[str, str]] = {}
        history_by_call: dict[str, list[Any]] = defaultdict(list)
        for event in provider_history_rows:
            history_by_call[event["entity_id"]].append(event)
        if not goals:
            for row in provider_rows:
                item, failure = _provider_receipt_projection(
                    row, tuple(history_by_call[row["id"]])
                )
                if item is not None:
                    projected.append(item)
                elif failure is not None:
                    projection_failures[row["id"]] = failure
        reconciliations = tuple(UsageReconciliationPointer(
            reconciliation_id=row["id"], call_id=row["call_id"], prior_usage_id=row["prior_usage_id"],
            effective_usage_id=row["effective_usage_id"], observation_digest=row["observation_digest"],
        ) for row in reconciliation_rows)
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             EntityRef(entity_type="goal", entity_id=goal_id),
             *(EntityRef(entity_type="goal_revision", entity_id=row["id"],
                          revision_no=row["revision_no"], digest=row["definition_digest"]) for row in goals),
             *(EntityRef(entity_type="provider_call", entity_id=item.provider_call_id)
               for item in projected),
             *(EntityRef(entity_type="usage_reconciliation", entity_id=item.reconciliation_id,
                         digest=item.observation_digest)
               for item in reconciliations)),
        )
        return summarize_usage_records(
            project_id=project_id, goal_id=goal_id, goal_revision_digests=digests,
            records=records, presentation=presentation,
            expected_logical_call_refs=expected_logical_call_refs,
            provider_call_expectations=tuple(ProviderCallExpectation(
                provider_call_id=row["id"], call_key=row["call_key"], status=row["status"], role=row["role"],
                execution_status=row["execution_status"], effect_status=row["effect_status"],
                result_status=row["result_status"],
                stage=row["stage"], usage_id=row["usage_id"],
                incomplete_reason_code=(
                    None if row["id"] not in projection_failures
                    else projection_failures[row["id"]][0]
                ),
                incomplete_reason_detail=(
                    None if row["id"] not in projection_failures
                    else projection_failures[row["id"]][1]
                ),
            ) for row in provider_rows),
            provider_receipt_usage=projected,
            goal_revision_unavailable_reason=(
                None if goals else "GOAL_REVISION_NOT_CREATED"
            ),
            superseded_usage_ids=(item.prior_usage_id for item in reconciliations),
            reconciliations=reconciliations,
        )

    def final_report(self, project_id: str, *, goal_verdict_id: str | None = None) -> FinalReport:
        self._project_row(project_id)
        query = (
            "SELECT id, plan_revision_id, goal_contract_digest, payload_json FROM goal_verdicts "
            "WHERE project_id = ? AND id = ?"
            if goal_verdict_id is not None else
            "SELECT id, plan_revision_id, goal_contract_digest, payload_json FROM goal_verdicts "
            "WHERE project_id = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1"
        )
        parameters = (project_id, goal_verdict_id) if goal_verdict_id is not None else (project_id,)
        with self.service.ledger.read() as connection:
            verdict_row = connection.execute(query, parameters).fetchone()
            if verdict_row is None:
                raise EngineApplicationError("GOAL_VERDICT_NOT_FOUND")
            plan_row = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ? AND project_id = ?",
                (verdict_row["plan_revision_id"], project_id),
            ).fetchone()
            goal_row = connection.execute(
                "SELECT id, payload_json FROM goal_revisions WHERE project_id = ? AND definition_digest = ?",
                (project_id, verdict_row["goal_contract_digest"]),
            ).fetchone()
        if plan_row is None:
            raise EngineApplicationError("VERDICT_PLAN_NOT_FOUND")
        if goal_row is None:
            raise EngineApplicationError("VERDICT_GOAL_REVISION_NOT_FOUND")
        plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
        verdict = GoalVerdict.model_validate_json(verdict_row["payload_json"])
        goal = GoalContractRevision.model_validate_json(goal_row["payload_json"])
        if plan.definition.goal_contract_digest != goal.definition_digest or verdict.goal_contract_digest != goal.definition_digest:
            raise EngineApplicationError("VERDICT_GOAL_BINDING_MISMATCH")
        usage = self.usage_summary(project_id, goal_id=goal.goal_id)
        execution_summary = self._execution_observation_summary(
            project_id,
            usage=usage,
            goal_id=goal.goal_id,
        )
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             EntityRef(entity_type="goal_verdict", entity_id=verdict.goal_verdict_id),
             EntityRef(entity_type="plan_revision", entity_id=plan.plan_revision_id,
                       revision_no=plan.revision_no, digest=plan.activation_digest),
             EntityRef(entity_type="goal_revision", entity_id=goal.goal_revision_id,
                       revision_no=goal.revision_no, digest=goal.definition_digest)),
        )
        read_only_verification = (
            self._read_only_report_verification(project_id, goal=goal, plan=plan, verdict=verdict)
            if goal.definition.effect_policy.mutation_policy is MutationPolicy.READ_ONLY
            else None
        )
        governance_plugin_tasks = self._governance_plugin_task_observations(project_id, plan)
        return FinalReport(
            project_id=project_id, goal=goal, plan=plan, verdict=verdict, usage=usage,
            execution_summary=execution_summary,
            ledger_history_valid=self.service.ledger.verify_history(project_id),
            governance_plugin_tasks=governance_plugin_tasks,
            entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
            read_only_verification=read_only_verification,
            error_code=(
                "READ_ONLY_REPORT_VERIFICATION_FAILED"
                if read_only_verification is not None and not (
                    read_only_verification.criteria_complete
                    and read_only_verification.evidence_grounded
                    and read_only_verification.source_unchanged
                )
                else usage.error_code
            ),
            next_action=(
                "read_only 응답의 AC/evidence 또는 source 불변성 검사를 확인하십시오."
                if read_only_verification is not None and not (
                    read_only_verification.criteria_complete
                    and read_only_verification.evidence_grounded
                    and read_only_verification.source_unchanged
                )
                else usage.next_action
            ),
        )

    def _governance_plugin_task_observations(
        self, project_id: str, plan: PlanContractRevision,
    ) -> tuple[GovernancePluginTaskObservation, ...]:
        """governance CoreOperations history를 Task별 최종 identity와 drift flag로 투영한다."""
        with self.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT p.sequence, p.payload_json AS prepared_json, c.payload_json AS completed_json "
                "FROM history_events p LEFT JOIN history_events c "
                "ON c.project_id = p.project_id AND c.entity_id = p.entity_id "
                "AND c.entity_type = 'core_operation' AND c.event_type = 'operation.completed' "
                "WHERE p.project_id = ? AND p.entity_type = 'core_operation' "
                "AND p.event_type = 'operation.prepared' ORDER BY p.sequence, c.sequence",
                (project_id,),
            ).fetchall()
        observed: dict[str, dict[str, Any]] = {}
        for row in rows:
            prepared = json.loads(row["prepared_json"])
            if prepared.get("kind") != "governance_gate" or row["completed_json"] is None:
                continue
            request = prepared.get("request", {})
            key = request.get("key", {})
            task_id = key.get("task_id")
            step = request.get("step")
            if not isinstance(task_id, str) or not isinstance(step, str):
                continue
            item = observed.setdefault(
                task_id,
                {
                    "closure_tree_digest": None,
                    "plugin_version_label": None,
                    "changed": False,
                    "changed_phases": [],
                },
            )
            if step.startswith("plugin_identity_changed:"):
                item["changed"] = True
                phase = step.partition(":")[2]
                if phase and phase not in item["changed_phases"]:
                    item["changed_phases"].append(phase)
            if step != "plugin_identity":
                continue
            completed = json.loads(row["completed_json"])
            result = completed.get("result", {}).get("data", {})
            summary = result.get("summary", {})
            item["closure_tree_digest"] = summary.get("closure_tree_digest")
            labels = result.get("labels", ())
            manifest = next((label.get("plugin") for label in labels
                             if label.get("source") == "plugin_manifest_file"), None)
            item["plugin_version_label"] = manifest.get("version") if isinstance(manifest, dict) else None
        return tuple(
            GovernancePluginTaskObservation(
                task_id=task.task_id,
                task_ref=task.task_ref,
                **(
                    observed.get(task.task_id, {})
                    | {
                        "changed_phases": tuple(
                            observed.get(task.task_id, {}).get("changed_phases", ())
                        )
                    }
                ),
            )
            for task in plan.definition.tasks
        )

    def _read_only_report_verification(
        self,
        project_id: str,
        *,
        goal: GoalContractRevision,
        plan: PlanContractRevision,
        verdict: GoalVerdict,
    ) -> ReadOnlyReportVerification:
        from .context import ProjectMapper, project_map_reobservation_scope

        with self.service.ledger.read() as connection:
            baseline_row = connection.execute(
                "SELECT payload_json FROM project_map_revisions "
                "WHERE project_id=? AND revision_digest=?",
                (project_id, plan.definition.project_map_digest),
            ).fetchone()
            project = connection.execute(
                "SELECT root FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if baseline_row is None or project is None:
            raise EngineApplicationError("READ_ONLY_BASELINE_NOT_FOUND")
        from .domain import ProjectMapRevision

        baseline = ProjectMapRevision.model_validate_json(baseline_row["payload_json"])
        sources = self.service.list_context_sources(project_id)
        observed = ProjectMapper().build(
            project_id=project_id,
            root=project["root"],
            revision_no=baseline.revision_no + 1,
            registered_references=(
                item.path
                for item in sources
                if item.kind is ContextSourceRegistrationKind.REFERENCE
            ),
            instruction_sources=(
                item.path
                for item in sources
                if item.kind is ContextSourceRegistrationKind.INSTRUCTION
            ),
            excluded_paths=(self.service.ledger.artifact_root.resolve(),),
            **project_map_reobservation_scope(baseline),
        )
        required = tuple(item.criterion_id for item in goal.definition.hard_acceptance)
        reported = tuple(item.criterion_id for item in verdict.criteria)
        evidence_ids = tuple(sorted({
            evidence_id
            for criterion in verdict.criteria
            for evidence_id in criterion.evidence_ids
        }))
        with self.service.ledger.read() as connection:
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                rows = connection.execute(
                    f"SELECT id FROM evidence_records WHERE project_id=? "
                    f"AND id IN ({placeholders})",
                    (project_id, *evidence_ids),
                ).fetchall()
            else:
                rows = ()
        found = {row["id"] for row in rows}
        missing = tuple(item for item in evidence_ids if item not in found)
        terminal_without_evidence = any(
            criterion.status.value in {"pass", "fail"} and not criterion.evidence_ids
            for criterion in verdict.criteria
        )
        return ReadOnlyReportVerification(
            required_criterion_ids=required,
            reported_criterion_ids=reported,
            evidence_ids=evidence_ids,
            missing_evidence_ids=missing,
            criteria_complete=(
                len(reported) == len(set(reported))
                and set(reported) == set(required)
            ),
            evidence_grounded=not missing and not terminal_without_evidence,
            source_unchanged=observed.semantic_digest == baseline.semantic_digest,
            baseline_project_map_semantic_digest=baseline.semantic_digest,
            observed_project_map_semantic_digest=observed.semantic_digest,
        )

    def attempt_detail(self, attempt_id: str) -> AttemptDetail:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT a.*, t.task_ref FROM attempts a JOIN task_contracts t ON t.id = a.task_id "
                "WHERE a.id = ?", (attempt_id,),
            ).fetchone()
            if row is None:
                raise EngineApplicationError("ATTEMPT_NOT_FOUND")
            intent_rows = connection.execute(
                "SELECT i.*, r.payload_json AS receipt_json FROM runtime_intents i "
                "LEFT JOIN runtime_receipts r ON r.intent_id = i.id "
                "WHERE i.attempt_id = ? ORDER BY i.prepared_at, i.rowid", (attempt_id,),
            ).fetchall()
        attempt = AttemptRecord.model_validate({
            "attempt_id": row["id"], "task_id": row["task_id"],
            "execution_spec_digest": row["execution_spec_digest"], "attempt_no": row["attempt_no"],
            "kind": row["kind"], "status": row["status"],
            "binding": None if row["binding_json"] is None else json.loads(row["binding_json"]),
            "failure_class": row["failure_class"], "failure_detail": row["failure_detail"],
            "started_at": row["started_at"], "ended_at": row["ended_at"],
        })
        intents = tuple(IntentDetail(
            intent=RuntimeIntentRecord.model_validate({
                "intent_id": item["id"], "attempt_id": item["attempt_id"], "kind": item["kind"],
                "idempotency_key": item["idempotency_key"], "request_digest": item["request_digest"],
                "status": item["status"], "prepared_at": item["prepared_at"],
            }),
            receipt=None if item["receipt_json"] is None else RuntimeReceipt.model_validate_json(item["receipt_json"]),
        ) for item in intent_rows)
        presentation = self._presentation(
            row["project_id"],
            (EntityRef(entity_type="project", entity_id=row["project_id"]),
             EntityRef(entity_type="plan_revision", entity_id=row["plan_revision_id"]),
             EntityRef(entity_type="attempt", entity_id=attempt_id,
                       digest=row["execution_spec_digest"])),
        )
        next_action = (
            "원장 receipt와 provider 상태를 먼저 관측하십시오."
            if any(item.intent.status.value in {"prepared", "unknown"} for item in intents) else None
        )
        return AttemptDetail(
            project_id=row["project_id"], plan_revision_id=row["plan_revision_id"], task_ref=row["task_ref"],
            attempt=attempt, intents=intents, entity_refs=presentation.entity_refs,
            history_cursor=presentation.history_cursor,
            error_code="EXTERNAL_EFFECT_UNKNOWN" if next_action else None, next_action=next_action,
        )

    def recovery_status(self, project_id: str) -> RecoveryStatus:
        project = self._project_row(project_id)
        active_refs = []
        with self.service.ledger.read() as connection:
            for entity_type, table, revision_id, digest_column in (
                ("goal_revision", "goal_revisions", project["active_goal_revision_id"], "definition_digest"),
                ("plan_revision", "plan_revisions", project["active_plan_revision_id"], "activation_digest"),
            ):
                if revision_id is None:
                    continue
                revision = connection.execute(
                    f"SELECT revision_no,{digest_column} AS digest FROM {table} WHERE id=? AND project_id=?",
                    (revision_id, project_id),
                ).fetchone()
                if revision is None:
                    raise EngineApplicationError("ACTIVE_REVISION_BINDING_BROKEN")
                active_refs.append(EntityRef(entity_type=entity_type, entity_id=revision_id,
                                             revision_no=revision["revision_no"], digest=revision["digest"]))
            intents = connection.execute(
                "SELECT i.id FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                "WHERE a.project_id = ? AND i.status IN ('prepared', 'unknown') ORDER BY i.prepared_at, i.rowid",
                (project_id,),
            ).fetchall()
            assessments = connection.execute(
                "SELECT payload_json FROM recovery_assessments WHERE project_id = ? ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
        unresolved = tuple(row["id"] for row in intents)
        validation_failure = self.service.task_validation_recovery_blocker(project_id)
        validation_recovery = (
            None if validation_failure is None
            else TaskValidationRecovery.model_validate(validation_failure)
        )
        validation_refs = () if validation_recovery is None else (
            EntityRef(entity_type="task", entity_id=validation_recovery.task_id),
            EntityRef(entity_type="attempt", entity_id=validation_recovery.attempt_id),
            EntityRef(entity_type="validation_result", entity_id=validation_recovery.validation_result_id),
            *(EntityRef(entity_type="evidence", entity_id=item) for item in validation_recovery.evidence_ids),
        )
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             *(EntityRef(entity_type="runtime_intent", entity_id=item) for item in unresolved),
             *active_refs,
             *validation_refs),
        )
        required = project["run_state"] == "recovery_required" or bool(unresolved)
        return RecoveryStatus(
            project_id=project_id, run_state=project["run_state"], recovery_reason=project["recovery_reason"],
            unresolved_intent_ids=unresolved,
            assessments=tuple(RecoveryAssessment.model_validate_json(row["payload_json"]) for row in assessments),
            task_validation_recovery=validation_recovery,
            entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
            error_code=("RECOVERY_REQUIRED" if required else
                        "TASK_VALIDATION_RECOVERY_REQUIRED" if validation_recovery else None),
            next_action=("기존 intent·receipt와 provider 상태를 재개 전에 대조하십시오." if required else
                         "실패 validation과 현재 Worker 근거로 원인을 분류한 RecoveryAssessment를 명시해 재시도하십시오."
                         if validation_recovery else None),
        )

    def model_binding_status(
        self, project_id: str, *, inventory: ModelInventory | None = None,
    ) -> ModelBindingStatus:
        project = self._project_row(project_id)
        plan_id = project["active_plan_revision_id"]
        if plan_id is None:
            presentation = self._presentation(project_id, (EntityRef(entity_type="project", entity_id=project_id),))
            return ModelBindingStatus(
                project_id=project_id, plan_revision_id=None,
                observed_inventory_digest=None if inventory is None else inventory.inventory_digest,
                bindings=(), entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
                error_code="ACTIVE_PLAN_NOT_FOUND", next_action="활성 PlanContract가 필요합니다.",
            )
        with self.service.ledger.read() as connection:
            row = connection.execute("SELECT payload_json FROM plan_revisions WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise EngineApplicationError("ACTIVE_PLAN_BINDING_BROKEN")
        plan = PlanContractRevision.model_validate_json(row["payload_json"])
        from .domain import TaskExecutionSpecRevision
        from .model_lock import verify_binding
        with self.service.ledger.read() as connection:
            specs = {row["task_id"]: TaskExecutionSpecRevision.model_validate_json(row["payload_json"])
                     for row in connection.execute("SELECT s.task_id,s.payload_json FROM execution_spec_revisions s "
                         "JOIN task_contracts t ON t.id=s.task_id WHERE t.plan_revision_id=? AND s.is_current=1", (plan_id,))}
        bindings = []
        for task in plan.definition.tasks:
            spec = specs.get(task.task_id)
            choices = ((task.assignment.executor, task.assignment.validator) if spec is None else
                       (spec.definition.executor, spec.definition.validator))
            for choice in choices:
                if choice is None:
                    continue
                model = choice.preferred_model if spec is None else choice.model
                effort = choice.preferred_effort if spec is None else choice.effort
                error = None
                supported = None if inventory is None else inventory.supports(model, effort)
                if inventory is not None and spec is not None:
                    try:
                        if choice.operational_binding is None:
                            raise ValueError("MODEL_BINDING_MISSING")
                        verify_binding(choice.operational_binding, inventory, role=choice.role, model=model, effort=effort)
                    except ValueError as failure:
                        error = str(failure).split(":", 1)[0]
                        supported = False
                bindings.append(ModelBindingItem(task_id=task.task_id, role=choice.role, model=model,
                    effort=effort, supported=supported, error_code=error,
                    execution_spec_digest=None if spec is None else spec.definition_digest,
                    inventory_digest=None if spec is None else choice.inventory_digest))
        bindings = tuple(bindings)
        unsupported = inventory is not None and any(item.supported is False for item in bindings)
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             EntityRef(entity_type="plan_revision", entity_id=plan_id,
                       revision_no=plan.revision_no, digest=plan.activation_digest)),
        )
        return ModelBindingStatus(
            project_id=project_id, plan_revision_id=plan_id,
            observed_inventory_digest=None if inventory is None else inventory.inventory_digest,
            bindings=bindings, entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
            error_code=("MODEL_INVENTORY_NOT_OBSERVED" if inventory is None else
                        "MODEL_BINDING_UNAVAILABLE" if unsupported else None),
            next_action=("model/list 관측 결과를 전달하십시오." if inventory is None else
                         "지원되는 model/effort로 새 binding을 준비하십시오." if unsupported else None),
        )
