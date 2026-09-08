from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .domain import EngineModel
from .role_observations import RoleCallReceipt, StructuredRoleError


class PlanningRecoveryPolicy(EngineModel):
    """기존 전체 예산 안에서 단계별 복구를 구분하는 명시적 개발 계약."""

    contract_version: Literal["planning-recovery-v2"] = "planning-recovery-v2"
    separate_stage_repairs: Literal[True] = True
    max_review_adjudications_per_candidate: Literal[1] = 1
    isolate_settled_candidate_schema_failures: Literal[True] = True


class CandidateSchemaFailure(EngineModel):
    """검색에서 격리한 한 호출의 실패. qualification 성공으로 변환하지 않는다."""

    source_skeleton_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_plan_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    operation: Literal[
        "expand", "review", "refine", "adjudicate", "skeleton_review",
        "initial_skeleton_review", "skeleton_refine",
    ]
    provider_call_id: str = Field(min_length=1)
    receipt: RoleCallReceipt

    @model_validator(mode="after")
    def terminal_usage_and_policy_are_known(self) -> "CandidateSchemaFailure":
        receipt = self.receipt
        initial_operation = self.operation in {"initial_skeleton_review", "skeleton_refine"}
        if initial_operation and self.source_plan_digest is not None:
            raise ValueError("초기 Skeleton 단계 실패에는 원본 Plan을 결속할 수 없습니다.")
        if not initial_operation and self.operation != "expand" and self.source_plan_digest is None:
            raise ValueError("상세 후보의 후속 단계 실패에는 원본 Plan이 필요합니다.")
        values = (receipt.input_tokens, receipt.cached_input_tokens,
                  receipt.output_tokens, receipt.reasoning_tokens)
        if (
            receipt.status != "schema_failed"
            or not receipt.usage_available
            or any(type(value) is not int or value < 0 for value in values)
            or receipt.cached_input_tokens > receipt.input_tokens
            or receipt.reasoning_tokens > receipt.output_tokens
            or receipt.permission_profile != ":danger-full-access"
            or receipt.approval_policy != "never"
            or not receipt.thread_id
            or len(receipt.turn_ids) != 1
            or not receipt.turn_ids[0]
            or receipt.schema_recovery_attempts != 0
        ):
            raise ValueError("후보 격리에는 정책·단일 종료 turn·사용량이 확인된 schema 실패가 필요합니다.")
        expected_roles = {
            "expand": {"plan_expander"}, "refine": {"plan_refiner"},
            "review": {"compact_plan_reviewer", "critical_effect_reviewer", "high_risk_reviewer", "external_effect_reviewer"},
            "adjudicate": {"compact_plan_reviewer", "critical_effect_reviewer", "high_risk_reviewer", "external_effect_reviewer"},
            "skeleton_review": {"skeleton_reviewer"},
            "initial_skeleton_review": {"skeleton_reviewer"},
            "skeleton_refine": {"skeleton_refiner"},
        }
        if receipt.role not in expected_roles[self.operation]:
            raise ValueError("격리한 실패의 역할이 실제 검색 단계와 다릅니다.")
        return self


def settled_candidate_schema_failure(
    error: StructuredRoleError, *, operation: str, source_skeleton_digest: str,
    source_plan_digest: str | None = None,
) -> CandidateSchemaFailure | None:
    """BudgetedRoleRunner가 정산을 마친 오류만 후보 경계에서 처리한다."""
    if (
        error.receipt is None
        or not error.settled_provider_call_id
        or not error.effects_started
        or not error.receipts
        or error.receipts[-1] != error.receipt
        or sum(item.call_id == error.receipt.call_id for item in error.receipts) != 1
    ):
        return None
    try:
        return CandidateSchemaFailure(
            operation=operation, source_skeleton_digest=source_skeleton_digest,
            source_plan_digest=source_plan_digest,
            provider_call_id=error.settled_provider_call_id, receipt=error.receipt,
        )
    except ValueError:
        return None
