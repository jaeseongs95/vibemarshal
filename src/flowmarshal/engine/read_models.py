"""Engine 원장을 변경하지 않고 표시하는 typed read model."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .domain import (
    AttemptRecord,
    BudgetStage,
    BudgetUsageRecord,
    GoalContractRevision,
    GoalVerdict,
    PlanContractRevision,
    RecoveryAssessment,
    RuntimeIntentRecord,
    RuntimeReceipt,
    EngineModel,
)


class EntityRef(EngineModel):
    entity_type: str = Field(min_length=1, max_length=100)
    entity_id: str = Field(min_length=1, max_length=500)
    revision_no: int | None = Field(default=None, ge=1)
    digest: str | None = Field(default=None, max_length=200)


class HistoryCursor(EngineModel):
    """표시 결과를 만든 원장 끝 위치다. 재개나 상태 전이 권한은 없다."""

    project_id: str = Field(min_length=1, max_length=500)
    sequence: int = Field(ge=0)
    event_id: str | None = Field(default=None, max_length=500)
    event_hash: str | None = Field(default=None, max_length=200)
    created_at: str | None = Field(default=None, max_length=100)


class ReadPresentation(EngineModel):
    """Core 상태를 설명하는 비권위 UI 메타데이터."""

    entity_refs: tuple[EntityRef, ...]
    history_cursor: HistoryCursor
    error_code: str | None = Field(default=None, max_length=100)
    next_action: str | None = Field(default=None, max_length=1000)


class UsageMetric(EngineModel):
    """0은 실측값이고, total=None은 하나 이상이 미확인임을 뜻한다."""

    known_subtotal: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    incomplete_call_count: int = Field(ge=0)


class UsageBreakdown(EngineModel):
    dimension: Literal["stage", "role"]
    value: str = Field(min_length=1, max_length=200)
    logical_call_count: int = Field(ge=0)
    input_tokens: UsageMetric
    cached_input_tokens: UsageMetric
    output_tokens: UsageMetric
    reasoning_tokens: UsageMetric
    latency_ms: UsageMetric


class DuplicateLogicalCall(EngineModel):
    logical_call_ref: str = Field(min_length=1, max_length=300)
    status: Literal["deduplicated", "conflict"]
    usage_ids: tuple[str, ...] = ()
    provider_call_ids: tuple[str, ...] = ()
    receipt_digests: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


class UsageIncompleteReason(EngineModel):
    code: str = Field(min_length=1, max_length=100)
    call_ref: str = Field(min_length=1, max_length=300)
    stage: str | None = Field(default=None, max_length=100)
    role: str | None = Field(default=None, max_length=100)
    detail: str = Field(min_length=1, max_length=1000)


class ProviderCallExpectation(EngineModel):
    """예약 원장이 보이는 호출 기대값이다. actual/adjustment는 usage 합계가 아니다."""

    provider_call_id: str = Field(min_length=1, max_length=500)
    call_key: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=1, max_length=100)
    execution_status: str | None = Field(default=None, max_length=100)
    effect_status: str | None = Field(default=None, max_length=100)
    result_status: str | None = Field(default=None, max_length=100)
    role: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=100)
    usage_id: str | None = Field(default=None, max_length=500)
    incomplete_reason_code: str | None = Field(default=None, max_length=100)
    incomplete_reason_detail: str | None = Field(default=None, max_length=1000)


class ProviderReceiptUsage(EngineModel):
    """Goal revision 생성 전 provider 원장 receipt의 읽기 전용 usage 투영."""

    projection_source: Literal["provider_receipt", "runtime_observation"] = "provider_receipt"
    runtime_observation_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    usage_scope: Literal["unspecified", "turn", "thread", "unavailable"] = "unspecified"
    attribution_basis: Literal["provider_turn", "first_empty_thread", "unavailable"] | None = None
    provider_call_id: str = Field(min_length=1, max_length=500)
    usage_id: None = None
    project_id: str = Field(min_length=1, max_length=500)
    goal_id: str = Field(min_length=1, max_length=500)
    goal_contract_digest: None = None
    stage: BudgetStage
    logical_call_ref: str = Field(min_length=1, max_length=300)
    role: str = Field(min_length=1, max_length=100)
    call_status: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    binding_provenance_version: Literal["2.0"] = "2.0"
    requested_model: str = Field(min_length=1, max_length=200)
    requested_effort: str = Field(min_length=1, max_length=50)
    observed_model: str | None = Field(default=None, min_length=1, max_length=200)
    observed_effort: str | None = Field(default=None, min_length=1, max_length=50)
    provider_inventory_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    adapter_capability_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    binding_provenance: dict[str, str | None]
    runner_receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    usage_available: bool
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_usage(self) -> "ProviderReceiptUsage":
        counts = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
        )
        present = tuple(value is not None for value in counts)
        if (self.model, self.effort) != (self.requested_model, self.requested_effort):
            raise ValueError("legacy model/effort alias는 requested binding과 같아야 합니다.")
        if self.usage_available and not any(present):
            raise ValueError("관측된 provider receipt projection에는 token 필드가 필요합니다.")
        if not self.usage_available and any(value is not None for value in counts):
            raise ValueError("미확인 provider receipt projection에는 token 수를 넣지 않습니다.")
        if self.usage_available and all(present) and self.unavailable_reason is not None:
            raise ValueError("완전한 usage와 unavailable 이유를 함께 표시할 수 없습니다.")
        if self.usage_available and not all(present) and self.unavailable_reason is None:
            raise ValueError("부분 usage에는 누락 이유가 필요합니다.")
        if not self.usage_available and self.unavailable_reason is None:
            raise ValueError("미확인 usage에는 이유가 필요합니다.")
        if (
            self.cached_input_tokens is not None
            and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input token은 input token보다 클 수 없습니다.")
        if (
            self.reasoning_tokens is not None
            and self.output_tokens is not None
            and self.reasoning_tokens > self.output_tokens
        ):
            raise ValueError("reasoning token은 output token보다 클 수 없습니다.")
        return self


class UsageReconciliationPointer(EngineModel):
    """원본 usage를 보존한 terminal 재관측의 표시용 포인터다."""

    reconciliation_id: str = Field(min_length=1, max_length=500)
    call_id: str = Field(min_length=1, max_length=500)
    prior_usage_id: str = Field(min_length=1, max_length=500)
    effective_usage_id: str = Field(min_length=1, max_length=500)
    observation_digest: str = Field(min_length=1, max_length=200)


class UsageSummary(ReadPresentation):
    project_id: str = Field(min_length=1, max_length=500)
    goal_id: str = Field(min_length=1, max_length=500)
    goal_revision_digests: tuple[str, ...]
    goal_revision_unavailable_reason: str | None = Field(default=None, max_length=500)
    logical_call_count: int = Field(ge=0, description="소계 집계 대상으로 남은 usage record의 논리 호출 수")
    provider_call_count: int | None = Field(default=None, ge=0, description="제공된 provider 원장의 호출 수. 원장 입력이 없으면 null")
    expected_logical_call_refs: tuple[str, ...] = ()
    missing_expected_logical_call_refs: tuple[str, ...] = ()
    provider_call_expectations: tuple[ProviderCallExpectation, ...] = ()
    provider_calls_without_usage: tuple[str, ...] = ()
    superseded_usage_ids: tuple[str, ...] = ()
    reconciliations: tuple[UsageReconciliationPointer, ...] = ()
    conflicts: tuple[DuplicateLogicalCall, ...] = ()
    deduplicated: tuple[DuplicateLogicalCall, ...] = ()
    incomplete_reasons: tuple[UsageIncompleteReason, ...] = ()
    input_tokens: UsageMetric
    cached_input_tokens: UsageMetric
    output_tokens: UsageMetric
    reasoning_tokens: UsageMetric
    latency_ms: UsageMetric
    by_stage: tuple[UsageBreakdown, ...] = ()
    by_role: tuple[UsageBreakdown, ...] = ()
    usage_records: tuple[BudgetUsageRecord, ...] = ()
    provider_receipt_usage: tuple[ProviderReceiptUsage, ...] = ()


class ModelObservationItem(EngineModel):
    """요청 binding과 provider가 직접 반환한 관측값을 분리한 표시 항목."""

    logical_call_ref: str = Field(min_length=1, max_length=300)
    role: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=100)
    requested_model: str = Field(min_length=1, max_length=200)
    requested_effort: str = Field(min_length=1, max_length=50)
    observed_model: str | None = Field(default=None, min_length=1, max_length=200)
    observed_effort: str | None = Field(default=None, min_length=1, max_length=50)
    observed_source: str | None = Field(default=None, max_length=300)


class ExecutionObservationSummary(EngineModel):
    """사용자 보고에서 model, usage, 외부 effect 축을 섞지 않는 요약."""

    model_observations: tuple[ModelObservationItem, ...] = ()
    usage_status: Literal["not_observed", "complete", "partial", "missing"]
    usage_missing_components: tuple[str, ...] = ()
    usage_incomplete_reasons: tuple[UsageIncompleteReason, ...] = ()
    external_effect_status: Literal["none", "pending", "confirmed", "unknown"]
    external_effect_unknown: bool
    external_effect_refs: tuple[str, ...] = ()
    external_effect_reason: str = Field(min_length=1, max_length=1000)


class ReadOnlyReportVerification(EngineModel):
    """read_only 응답의 AC/evidence 완전성과 source 불변성을 분리한 검사 결과."""

    required_criterion_ids: tuple[str, ...]
    reported_criterion_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    missing_evidence_ids: tuple[str, ...] = ()
    criteria_complete: bool
    evidence_grounded: bool
    source_unchanged: bool
    baseline_project_map_semantic_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_project_map_semantic_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FinalReport(ReadPresentation):
    project_id: str = Field(min_length=1, max_length=500)
    goal: GoalContractRevision
    plan: PlanContractRevision
    verdict: GoalVerdict
    usage: UsageSummary
    execution_summary: ExecutionObservationSummary
    ledger_history_valid: bool
    read_only_verification: ReadOnlyReportVerification | None = None


class IntentDetail(EngineModel):
    intent: RuntimeIntentRecord
    receipt: RuntimeReceipt | None = None


class AttemptDetail(ReadPresentation):
    project_id: str = Field(min_length=1, max_length=500)
    plan_revision_id: str = Field(min_length=1, max_length=500)
    task_ref: str = Field(min_length=1, max_length=300)
    attempt: AttemptRecord
    intents: tuple[IntentDetail, ...] = ()


class TaskValidationRecovery(EngineModel):
    task_id: str
    attempt_id: str
    validation_id: str
    validation_result_id: str
    evidence_ids: tuple[str, ...]
    history_sequence: int = Field(ge=1)


class RecoveryStatus(ReadPresentation):
    project_id: str = Field(min_length=1, max_length=500)
    run_state: str = Field(min_length=1, max_length=100)
    recovery_reason: str | None = Field(default=None, max_length=2000)
    unresolved_intent_ids: tuple[str, ...] = ()
    assessments: tuple[RecoveryAssessment, ...] = ()
    task_validation_recovery: TaskValidationRecovery | None = None


class RecoveryCodeObservation(EngineModel):
    """복구 분류에 쓰인 code 하나와 그 출처 provenance."""

    values: tuple[str, ...] = ()
    provenance: Literal[
        "provider_observed", "local_derived", "model_reported", "client_requested"
    ]
    authoritative: bool
    note: str | None = Field(default=None, max_length=500)


class RecoveryClassification(EngineModel):
    """현재 미해결 실패의 원장 분류와 그 직접 근거."""

    task_id: str | None = Field(default=None, max_length=500)
    attempt_id: str | None = Field(default=None, max_length=500)
    validation_result_id: str | None = Field(default=None, max_length=500)
    failure_class: str | None = Field(default=None, max_length=100)
    basis: str = Field(min_length=1, max_length=100)
    rationale: str | None = Field(default=None, max_length=5000)
    transient: bool = False
    evidence_ids: tuple[str, ...] = ()
    codes: tuple[RecoveryCodeObservation, ...] = ()


class RecoveryScope(EngineModel):
    """복구 전후로 보존한 기록과 새 revision이 대체한 범위."""

    preserved_attempt_ids: tuple[str, ...] = ()
    preserved_evidence_ids: tuple[str, ...] = ()
    preserved_assessment_ids: tuple[str, ...] = ()
    preserved_receipt_intent_ids: tuple[str, ...] = ()
    superseded_plan_revision_ids: tuple[str, ...] = ()
    superseded_task_ids: tuple[str, ...] = ()
    active_plan_revision_id: str | None = Field(default=None, max_length=500)


class RecoveryLimitStatus(EngineModel):
    """원장에서 직접 센 복구 한도와 새 evidence 요구 상태."""

    assessment_id: str | None = Field(default=None, max_length=200)
    assessment_recorded: bool = False
    failure_class_retryable: bool = True
    task_recovery_count: int = Field(default=0, ge=0)
    max_task_recovery: int = Field(default=0, ge=0)
    same_failure_replan_count: int = Field(default=0, ge=0)
    max_same_failure_replans: int = Field(default=0, ge=0)
    goal_replan_count: int = Field(default=0, ge=0)
    max_goal_replans: int = Field(default=0, ge=0)
    requires_new_evidence: bool = True
    has_new_evidence: bool = True
    limit_code: str | None = Field(default=None, max_length=100)
    detail: str | None = Field(default=None, max_length=2000)


class RecoveryNextAction(EngineModel):
    """Core가 이어서 할 일 또는 사용자 판단이 필요한 이유."""

    mode: Literal["automatic", "user_decision", "observe_first", "none"]
    blocker_code: str | None = Field(default=None, max_length=100)
    suggested_repair_action: str | None = Field(default=None, max_length=100)
    checkpoint_required: bool = False
    detail: str = Field(min_length=1, max_length=2000)


class RecoveryExplanation(EngineModel):
    """분류 근거·보존/폐기 범위·다음 동작을 한 화면에 모은 설명."""

    state: Literal[
        "none", "automatic_pending", "user_decision_required",
        "observe_first_required", "recovered",
    ]
    classification: RecoveryClassification | None = None
    limits: RecoveryLimitStatus | None = None
    scope: RecoveryScope = RecoveryScope()
    assessments: tuple[RecoveryAssessment, ...] = ()
    next_action: RecoveryNextAction


class ModelBindingItem(EngineModel):
    task_id: str | None = None
    execution_spec_digest: str | None = None
    inventory_digest: str | None = None
    error_code: str | None = None
    role: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    supported: bool | None = None


class ModelBindingStatus(ReadPresentation):
    project_id: str = Field(min_length=1, max_length=500)
    plan_revision_id: str | None = Field(default=None, max_length=500)
    observed_inventory_digest: str | None = Field(default=None, max_length=200)
    bindings: tuple[ModelBindingItem, ...] = ()
