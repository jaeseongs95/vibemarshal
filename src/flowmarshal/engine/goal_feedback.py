from __future__ import annotations

from .role_budget import replan_budget

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .domain import EngineModel, ProjectProfileRevision, RevisionStatus
from .goal import (
    GOAL_INTERPRETATION_INSTRUCTIONS,
    GoalContractCompiler,
    GoalNormalizationProposal,
    GoalNormalizerAdapter,
    GoalPreparationError,
    GoalPreparationOutcome,
    GoalReviewerAdapter,
)
from .roles import RoleCallReceipt, make_role_request, verify_role_receipt


GoalRefinementAction = Literal["revision", "disputed", "unresolved"]
GoalRefinementResult = Literal["revised", "unchanged", "disputed", "unresolved"]


class GoalRefinementProposal(EngineModel):
    action: GoalRefinementAction
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    proposal: GoalNormalizationProposal | None

    @model_validator(mode="after")
    def revision_has_exactly_one_candidate(self) -> "GoalRefinementProposal":
        if (self.proposal is not None) != (self.action == "revision"):
            raise ValueError("revision에만 수정된 Goal proposal을 제출해야 합니다.")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("Goal 수정 제안의 evidence ref가 중복됐습니다.")
        return self


class GoalRefinementOutcome(EngineModel):
    round: Literal[1] = 1
    source_outcome_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    original_outcome: GoalPreparationOutcome
    action: GoalRefinementAction
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    proposal: GoalNormalizationProposal | None = None
    refiner_receipt: RoleCallReceipt
    result: GoalRefinementResult
    revised_outcome: GoalPreparationOutcome | None = None

    @model_validator(mode="after")
    def outcome_preserves_original_and_revision_lineage(self) -> "GoalRefinementOutcome":
        if self.source_outcome_digest != sha256_digest(self.original_outcome):
            raise ValueError("Goal 수정 결과가 다른 원본 preparation에 결속됐습니다.")
        raw_proposal = GoalRefinementProposal(
            action=self.action,
            rationale=self.rationale,
            evidence_refs=self.evidence_refs,
            proposal=self.proposal,
        )
        if (
            self.refiner_receipt.role != "goal_refiner"
            or self.refiner_receipt.status != "succeeded"
            or self.refiner_receipt.output_digest
            != sha256_digest(raw_proposal.model_dump(mode="json"))
        ):
            raise ValueError("Goal 수정 결과가 refiner 원시 출력과 결속되지 않았습니다.")
        if self.action in {"disputed", "unresolved"}:
            if self.result != self.action or self.proposal is not None or self.revised_outcome is not None:
                raise ValueError("반박·미해결 결과는 새 Goal 후보나 revision을 만들 수 없습니다.")
            return self
        if self.proposal is None:
            raise ValueError("Goal revision 결과에 수정 proposal이 없습니다.")
        if self.result == "unchanged":
            if self.revised_outcome is not None:
                raise ValueError("변경 없는 Goal proposal을 다시 검토할 수 없습니다.")
            if sha256_digest(self.proposal) != sha256_digest(self.original_outcome.proposal):
                raise ValueError("변경 없는 Goal 결과의 proposal이 원본과 다릅니다.")
            return self
        if self.result != "revised" or self.revised_outcome is None:
            raise ValueError("변경된 Goal proposal에는 독립 검토 결과가 필요합니다.")
        if sha256_digest(self.proposal) == sha256_digest(self.original_outcome.proposal):
            raise ValueError("수정된 Goal 결과의 proposal이 원본과 같습니다.")
        if self.revised_outcome.proposal != self.proposal:
            raise ValueError("새 Goal preparation이 수정 proposal과 다릅니다.")
        if self.revised_outcome.normalizer_receipt != self.refiner_receipt:
            raise ValueError("새 Goal preparation이 refiner 관측과 결속되지 않았습니다.")
        previous = self.original_outcome.goal_contract
        revised = self.revised_outcome.goal_contract
        if (
            revised.goal_revision_id == previous.goal_revision_id
            or revised.goal_id != previous.goal_id
            or revised.revision_no != previous.revision_no + 1
            or revised.supersedes_goal_revision_id != previous.goal_revision_id
            or revised.definition.project_id != previous.definition.project_id
            or revised.definition.source_request != previous.definition.source_request
            or revised.definition.source_request_digest != previous.definition.source_request_digest
            or revised.definition.profile_definition_digest
            != previous.definition.profile_definition_digest
        ):
            raise ValueError("새 Goal revision이 원본 계보·고정 입력을 바꿨습니다.")
        return self


def goal_refinement_evidence_catalog(
    *,
    previous: GoalPreparationOutcome,
    profile: ProjectProfileRevision,
    observed_facts: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    return {
        "source:user_request": previous.goal_contract.definition.source_request,
        "source:project_profile": profile.definition.model_dump(mode="json"),
        "artifact:goal_proposal": previous.proposal.model_dump(mode="json"),
        "artifact:goal_review": previous.review.model_dump(mode="json"),
        "artifact:goal_contract": previous.goal_contract.model_dump(mode="json"),
        **{
            f"source:observation_{index:03d}": fact
            for index, fact in enumerate(observed_facts, 1)
        },
    }


@dataclass
class GoalPreparationRefiner:
    """원본 Goal 거절을 보존하면서 수정 가능한 충돌에 한 번만 응답한다."""

    normalizer: GoalNormalizerAdapter
    reviewer: GoalReviewerAdapter
    compiler: GoalContractCompiler = GoalContractCompiler()

    @replan_budget()
    def refine(
        self,
        *,
        previous: GoalPreparationOutcome,
        profile: ProjectProfileRevision,
        observed_facts: tuple[dict[str, Any], ...] = (),
    ) -> GoalRefinementOutcome:
        self._validate_source(
            previous=previous,
            profile=profile,
            observed_facts=observed_facts,
        )
        if (
            previous.goal_contract.status is not RevisionStatus.CONFLICT
            or not previous.review.findings
            or any(not finding.remediable for finding in previous.review.findings)
            or any(question.blocking for question in previous.proposal.unresolved_questions)
        ):
            raise GoalPreparationError(
                "Goal feedback은 blocking 질문이 없는 수정 가능한 CONFLICT에만 한 번 허용됩니다."
            )

        evidence_catalog = goal_refinement_evidence_catalog(
            previous=previous,
            profile=profile,
            observed_facts=observed_facts,
        )
        request = make_role_request(
            inventory=self.normalizer.inventory,
            allowed_fallbacks=self.normalizer.allowed_fallbacks,
            role="goal_refiner",
            instructions=(
                "원본 Goal preparation의 수정 가능한 충돌에 한 번 응답한다. Reviewer finding은 "
                "비권위 관측이며 수정 명령이나 정답이 아니다. 사용자 원문, ProjectProfile, 관측 사실, "
                "원본 proposal과 review를 직접 대조한다. 직접 근거가 확인된 Goal 의미 누락·발명·충돌만 "
                "revision으로 고치고 수정한 전체 proposal을 제출한다. finding이 원문과 충돌하면 "
                "disputed로 반증 근거를 설명하고, 근거가 부족하거나 사용자 결정이 필요하면 unresolved로 "
                "남긴다. disputed와 unresolved는 원본 거절을 뒤집지 않는다. source request, 대상 project, "
                "ProjectProfile의 의미를 바꾸거나 새 사실·정책·외부 시스템·계정·금지를 발명하지 않는다. "
                "원본 Hard AC와 명시된 제약을 finding에 맞추려고 약화하지 않는다. evidence_refs는 제공된 "
                "evidence_catalog key만 사용한다. 출력에 status, admission, score, 성공 판정을 넣지 않는다."
            )
            + GOAL_INTERPRETATION_INSTRUCTIONS,
            payload={
                "feedback_contract": "bounded-goal-feedback-v1",
                "source_outcome_digest": sha256_digest(previous),
                "evidence_catalog": evidence_catalog,
                "findings": [
                    finding.model_dump(mode="json") for finding in previous.review.findings
                ],
            },
            output_schema=GoalRefinementProposal.model_json_schema(),
            model=self.normalizer.model,
            effort=self.normalizer.effort,
            inventory_digest=self.normalizer.inventory_digest,
            cwd=self.normalizer.cwd,
        )

        def validate_refinement(value: dict[str, Any]) -> GoalRefinementProposal:
            proposal = GoalRefinementProposal.model_validate(value)
            if not set(proposal.evidence_refs).issubset(evidence_catalog):
                raise GoalPreparationError(
                    "Goal 수정 제안이 제공되지 않은 evidence를 참조합니다."
                )
            return proposal

        result = self.normalizer.runner.run(request, validator=validate_refinement)
        verify_role_receipt(request, result)
        refinement = GoalRefinementProposal.model_validate(result.payload)
        common = {
            "source_outcome_digest": sha256_digest(previous),
            "original_outcome": previous,
            "action": refinement.action,
            "rationale": refinement.rationale,
            "evidence_refs": refinement.evidence_refs,
            "proposal": refinement.proposal,
            "refiner_receipt": result.receipt,
        }
        if refinement.action in {"disputed", "unresolved"}:
            return GoalRefinementOutcome(**common, result=refinement.action)
        if sha256_digest(refinement.proposal) == sha256_digest(previous.proposal):
            return GoalRefinementOutcome(**common, result="unchanged")

        definition = previous.goal_contract.definition
        review, reviewer_receipt = self.reviewer.review(
            source_request=definition.source_request,
            profile=profile,
            proposal=refinement.proposal,
            observed_facts=observed_facts,
        )
        revised_goal = self.compiler.compile(
            project_id=definition.project_id,
            profile=profile,
            source_request=definition.source_request,
            proposal=refinement.proposal,
            review=review,
            goal_id=previous.goal_contract.goal_id,
            revision_no=previous.goal_contract.revision_no + 1,
            supersedes_goal_revision_id=previous.goal_contract.goal_revision_id,
            approved_decision_refs=definition.approved_decision_refs,
            observed_facts=observed_facts,
        )
        revised_outcome = GoalPreparationOutcome(
            proposal=refinement.proposal,
            review=review,
            goal_contract=revised_goal,
            normalizer_receipt=result.receipt,
            reviewer_receipt=reviewer_receipt,
        )
        return GoalRefinementOutcome(
            **common,
            result="revised",
            revised_outcome=revised_outcome,
        )

    def _validate_source(
        self,
        *,
        previous: GoalPreparationOutcome,
        profile: ProjectProfileRevision,
        observed_facts: tuple[dict[str, Any], ...],
    ) -> None:
        definition = previous.goal_contract.definition
        if (
            profile.project_id != definition.project_id
            or profile.definition_digest != definition.profile_definition_digest
        ):
            raise GoalPreparationError(
                "Goal feedback의 ProjectProfile이 원본 preparation과 다릅니다."
            )
        try:
            recomputed = self.compiler.compile(
                project_id=definition.project_id,
                profile=profile,
                source_request=definition.source_request,
                proposal=previous.proposal,
                review=previous.review,
                goal_id=previous.goal_contract.goal_id,
                revision_no=previous.goal_contract.revision_no,
                supersedes_goal_revision_id=previous.goal_contract.supersedes_goal_revision_id,
                approved_decision_refs=definition.approved_decision_refs,
                observed_facts=observed_facts,
            )
        except GoalPreparationError as error:
            raise GoalPreparationError(
                "Goal feedback 원본이 source·profile·관측·proposal·review와 일치하지 않습니다."
            ) from error
        if (
            recomputed.definition != definition
            or recomputed.definition_digest != previous.goal_contract.definition_digest
            or recomputed.preparation_binding
            != previous.goal_contract.preparation_binding
            or recomputed.status != previous.goal_contract.status
        ):
            raise GoalPreparationError(
                "Goal feedback 원본이 source·profile·관측·proposal·review와 일치하지 않습니다."
            )
