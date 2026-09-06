"""Engine 원장을 변경하지 않고 표시하는 typed read model."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .domain import (
    AttemptRecord,
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
    role: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=100)
    usage_id: str | None = Field(default=None, max_length=500)


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


class FinalReport(ReadPresentation):
    project_id: str = Field(min_length=1, max_length=500)
    goal: GoalContractRevision
    plan: PlanContractRevision
    verdict: GoalVerdict
    usage: UsageSummary
    ledger_history_valid: bool


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
