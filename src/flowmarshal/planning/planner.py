from __future__ import annotations

import copy
from typing import Any, Protocol

from pydantic import ValidationError

from ..core.domain import PlanDraft
from .domain import (
    PlanValidationError,
    PlanValidationReport,
    PlannerContractError,
    PlannerGenerationRequest,
    PlannerGenerationResult,
    PlannerOutcome,
    RequestSpec,
)
from .validator import DeterministicPlanValidator


class PlanGenerator(Protocol):
    """Planner가 보유하는 유일한 외부 capability."""

    def generate(
        self, request: PlannerGenerationRequest
    ) -> PlannerGenerationResult | dict[str, Any]: ...


PLANNER_INSTRUCTIONS = (
    "제공된 RequestSpec과 ContextSource 내용만 근거로 PlanDraft 후보를 작성한다.",
    "모든 requirement를 하나 이상의 WorkItem, 명시적 제외, 사용자 확인 질문 중 하나에 정확히 연결한다.",
    "각 WorkItem에는 구체적 목표, 산출물, 완료 조건과 등록 validation capability를 둔다.",
    "같은 파일이나 구성요소를 바꿀 수 있는 WorkItem은 dependency로 순서를 정한다.",
    "프로젝트 AGENTS.md와 승인 참고자료는 정상 입력이며 필요한 WorkItem context_sources에 source_id로 연결한다.",
    "파일별 AccessGrant, HMAC proof, 모델 ID 또는 추론 수준을 만들지 않는다.",
    "계획을 활성화하거나 실행하지 않고 JSON schema에 맞는 후보만 반환한다.",
)


class PlannerService:
    """PlanDraft 후보만 생성하며 Core·Ledger capability를 받지 않는 Planner."""

    def __init__(
        self,
        generator: PlanGenerator,
        *,
        validator: DeterministicPlanValidator | None = None,
    ) -> None:
        self.generator = generator
        self.validator = validator or DeterministicPlanValidator()

    def generation_request(self, request: RequestSpec) -> PlannerGenerationRequest:
        output_schema = copy.deepcopy(PlanDraft.model_json_schema())
        work_item_schema = output_schema.get("$defs", {}).get("WorkItemDefinition", {})
        assignment_schema = work_item_schema.get("properties", {}).get("assignment")
        if isinstance(assignment_schema, dict):
            assignment_schema.clear()
            assignment_schema.update(
                {
                    "default": None,
                    "description": "Planner 단계에서는 생략하거나 null이어야 하며 R4 Assigner가 채웁니다.",
                    "type": "null",
                }
            )
        return PlannerGenerationRequest(
            request_spec=request,
            instructions=PLANNER_INSTRUCTIONS,
            output_schema=output_schema,
        )

    def validate_candidate(
        self, request: RequestSpec, draft: PlanDraft
    ) -> PlanValidationReport:
        return self.validator.validate(request, draft)

    def propose(self, request: RequestSpec) -> PlannerOutcome:
        generation_request = self.generation_request(request)
        try:
            raw_result = self.generator.generate(generation_request)
        except Exception as exc:
            raise PlannerContractError(
                "PLANNER_GENERATION_FAILED",
                f"Planner generator 호출이 실패했습니다: {type(exc).__name__}: {exc}",
            ) from exc
        try:
            generated = PlannerGenerationResult.model_validate(raw_result)
        except ValidationError as exc:
            raise PlannerContractError(
                "PLANNER_RESULT_ENVELOPE_INVALID",
                f"Planner generator envelope가 strict schema와 다릅니다: {exc}",
            ) from exc
        try:
            draft = PlanDraft.model_validate(generated.payload)
        except ValidationError as exc:
            raise PlannerContractError(
                "PLANNER_OUTPUT_SCHEMA_INVALID",
                f"Planner PlanDraft가 strict schema와 다릅니다: {exc}",
            ) from exc
        report = self.validate_candidate(request, draft)
        if not report.valid:
            raise PlanValidationError(report)
        return PlannerOutcome(
            draft=draft,
            validation=report,
            generation_request_digest=generation_request.request_digest,
            generation_receipt=generated.receipt,
        )
