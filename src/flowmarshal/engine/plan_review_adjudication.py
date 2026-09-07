from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ..canonical import sha256_digest
from .domain import (
    EngineModel,
    ReviewFinding,
    ReviewRatings,
    ReviewerSubmission,
    validate_reviewer_submission_evidence,
)
from .planning_feedback import PlanRefinementProposal
from .role_observations import RoleCallReceipt
from .validation_obligations import EXPLICIT_VALIDATION_OBLIGATION_INSTRUCTIONS


_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


class PlanReviewAdjudicationError(ValueError):
    pass


class PlanReviewFindingDisposition(EngineModel):
    original_finding_digest: str = Field(pattern=_DIGEST_PATTERN)
    disposition: Literal["upheld", "withdrawn"]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_is_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("재심 disposition evidence ref가 중복됐습니다.")
        return value


class PlanReviewAdjudicationDraft(EngineModel):
    dispositions: tuple[PlanReviewFindingDisposition, ...] = Field(min_length=1)
    additional_findings: tuple[ReviewFinding, ...] = ()
    ratings: ReviewRatings | None = None

    @model_validator(mode="after")
    def disposition_and_rating_shape_is_valid(self) -> "PlanReviewAdjudicationDraft":
        digests = tuple(item.original_finding_digest for item in self.dispositions)
        if len(digests) != len(set(digests)):
            raise ValueError("재심 disposition이 같은 원 finding을 중복 판정했습니다.")
        final_has_findings = (
            any(item.disposition == "upheld" for item in self.dispositions)
            or bool(self.additional_findings)
        )
        if final_has_findings == (self.ratings is not None):
            raise ValueError("재심 최종 finding이 없을 때만 ratings가 필요합니다.")
        return self


class PlanReviewAdjudication(EngineModel):
    source_evaluation_digest: str = Field(pattern=_DIGEST_PATTERN)
    dispute: PlanRefinementProposal
    original_submission: ReviewerSubmission
    dispositions: tuple[PlanReviewFindingDisposition, ...] = Field(min_length=1)
    submission: ReviewerSubmission
    request_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_digest: str = Field(pattern=_DIGEST_PATTERN)
    receipt: RoleCallReceipt

    @model_validator(mode="after")
    def receipt_observation_is_bound(self) -> "PlanReviewAdjudication":
        if self.dispute.action != "disputed":
            raise ValueError("명시적 disputed 제안만 재심할 수 있습니다.")
        if self.receipt.input_digest != self.request_digest:
            raise ValueError("재심 receipt가 요청 digest와 다릅니다.")
        if self.receipt.output_digest != self.output_digest:
            raise ValueError("재심 receipt가 출력 digest와 다릅니다.")
        if self.receipt.role != self.submission.reviewer_role:
            raise ValueError("재심 receipt role과 최종 submission role이 다릅니다.")
        if (
            self.receipt.status != "succeeded"
            or self.receipt.permission_profile != ":danger-full-access"
            or self.receipt.approval_policy != "never"
            or not self.receipt.thread_id
            or len(self.receipt.turn_ids) != 1
            or not self.receipt.turn_ids[0]
            or self.receipt.schema_recovery_attempts != 0
        ):
            raise ValueError("재심 receipt에 성공한 독립 turn·정책 결속이 없습니다.")
        self._validate_original_lineage(self.original_submission)
        return self

    def _validate_original_lineage(self, original_submission: ReviewerSubmission) -> None:
        if self.submission.candidate_digest != original_submission.candidate_digest:
            raise PlanReviewAdjudicationError("재심 submission이 다른 immutable Plan을 참조합니다.")
        if self.submission.reviewer_role != original_submission.reviewer_role:
            raise PlanReviewAdjudicationError("재심이 원검토와 다른 risk route role을 사용했습니다.")
        if self.submission.evidence_catalog_digest != original_submission.evidence_catalog_digest:
            raise PlanReviewAdjudicationError("재심 최종 evidence catalog digest가 원검토와 다릅니다.")
        original_by_digest = {
            sha256_digest(finding): finding for finding in original_submission.findings
        }
        if len(original_by_digest) != len(original_submission.findings):
            raise PlanReviewAdjudicationError("원검토 finding 객체가 중복됐습니다.")
        disposition_by_digest = {
            item.original_finding_digest: item for item in self.dispositions
        }
        if len(disposition_by_digest) != len(self.dispositions):
            raise PlanReviewAdjudicationError("재심 disposition이 중복됐습니다.")
        if disposition_by_digest.keys() != original_by_digest.keys():
            raise PlanReviewAdjudicationError(
                "재심은 모든 원 finding을 digest별 정확히 한 번 판정해야 합니다."
            )
        upheld = tuple(
            finding
            for digest, finding in original_by_digest.items()
            if disposition_by_digest[digest].disposition == "upheld"
        )
        original_codes = {item.finding_code for item in original_submission.findings}
        additional = tuple(
            finding for finding in self.submission.findings if finding not in upheld
        )
        if any(item.finding_code in original_codes for item in additional):
            raise PlanReviewAdjudicationError("withdrawn 원 finding을 additional finding으로 다시 넣을 수 없습니다.")
        if self.submission.findings != upheld + additional:
            raise PlanReviewAdjudicationError("재심 최종 finding 집합이 disposition과 다릅니다.")

    def validate_source(
        self,
        source_evaluation_digest: str | None = None,
        original_submission: ReviewerSubmission | None = None,
        evidence_catalog: dict[str, Any] | None = None,
        known_task_refs: set[str] | None = None,
        dispute_evidence_catalog: dict[str, Any] | None = None,
    ) -> "PlanReviewAdjudication":
        expected_source_digest = source_evaluation_digest or self.source_evaluation_digest
        source_submission = original_submission or self.original_submission
        if self.source_evaluation_digest != expected_source_digest:
            raise PlanReviewAdjudicationError("재심이 다른 원본 Plan evaluation에 결속됐습니다.")
        if (
            self.dispute.provenance is not None
            and self.dispute.provenance.source_evaluation_digest != expected_source_digest
        ):
            raise PlanReviewAdjudicationError("disputed 제안 provenance가 다른 evaluation을 참조합니다.")
        if source_submission != self.original_submission:
            raise PlanReviewAdjudicationError("재심에 제공한 원검토가 저장된 원검토와 다릅니다.")
        self._validate_original_lineage(source_submission)

        if evidence_catalog is None:
            return self
        expected_catalog_digest = sha256_digest(evidence_catalog)
        if (
            source_submission.evidence_catalog_digest != expected_catalog_digest
            or self.submission.evidence_catalog_digest != expected_catalog_digest
        ):
            raise PlanReviewAdjudicationError("재심의 원검토 또는 최종 evidence catalog digest가 다릅니다.")
        known_evidence = set(evidence_catalog)
        known_dispute_evidence = set(dispute_evidence_catalog or evidence_catalog)
        unknown_dispute = set(self.dispute.evidence_refs) - known_dispute_evidence
        if unknown_dispute:
            raise PlanReviewAdjudicationError(
                "재심 dispute가 제공되지 않은 evidence를 참조합니다: "
                f"{sorted(unknown_dispute)}"
            )
        for label, refs in (
            (f"disposition:{item.original_finding_digest}", item.evidence_refs)
            for item in self.dispositions
        ):
            unknown = set(refs) - known_evidence
            if unknown:
                raise PlanReviewAdjudicationError(
                    f"재심 {label}가 제공되지 않은 evidence를 참조합니다: {sorted(unknown)}"
                )

        validate_reviewer_submission_evidence(
            source_submission,
            evidence_catalog=evidence_catalog,
            known_task_refs=known_task_refs,
        )
        validate_reviewer_submission_evidence(
            self.submission,
            evidence_catalog=evidence_catalog,
            known_task_refs=known_task_refs,
        )
        return self


def adjudicate_plan_review(
    *,
    adapter: Any,
    evaluation: Any,
    proposal: PlanRefinementProposal,
    goal: Any,
    state: Any,
    project_map: Any,
    candidate: Any | None = None,
) -> PlanReviewAdjudication:
    """v2 disputed Plan review를 같은 route의 새 독립 역할 호출로 재심한다."""

    from .plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V2
    from .planning import plan_review_evidence_catalog, risk_route
    from .planner_roles import (
        PLAN_TASK_RESULT_BOUNDARY_INSTRUCTIONS,
        PLANNING_PROJECT_PATH_V2_INSTRUCTIONS,
        PLANNING_VALIDATION_BOUNDARY_V2_INSTRUCTIONS,
        PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS,
        inspection_source_catalog,
    )
    from .roles import make_role_request, verify_role_receipt

    if getattr(adapter, "inspection_provider_contract", None) != PLAN_INSPECTION_PROVIDER_V2:
        raise PlanReviewAdjudicationError("Plan review 재심은 v2 inspection 경로에서만 허용됩니다.")
    if proposal.action != "disputed":
        raise PlanReviewAdjudicationError("명시적 disputed 제안만 재심할 수 있습니다.")
    if evaluation.deterministic_findings:
        raise PlanReviewAdjudicationError("deterministic finding은 모델 재심으로 반박할 수 없습니다.")
    if len(evaluation.semantic_submissions) != 1:
        raise PlanReviewAdjudicationError("재심에는 정확히 하나의 원 semantic review가 필요합니다.")

    original_submission = evaluation.semantic_submissions[0]
    if not original_submission.findings:
        raise PlanReviewAdjudicationError("원 finding이 없는 Plan review는 재심할 수 없습니다.")
    plan = evaluation.plan
    route = risk_route(plan)
    if original_submission.reviewer_role != route:
        raise PlanReviewAdjudicationError("원검토 role이 현재 immutable Plan의 risk route와 다릅니다.")
    source_evaluation_digest = sha256_digest(evaluation)
    if (
        proposal.provenance is not None
        and proposal.provenance.source_evaluation_digest != source_evaluation_digest
    ):
        raise PlanReviewAdjudicationError("disputed 제안 provenance가 다른 evaluation을 참조합니다.")

    evidence_catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
    extra_evidence_catalog: dict[str, Any] = {}
    if candidate is not None:
        if sha256_digest(candidate) != plan.definition.source_skeleton_digest:
            raise PlanReviewAdjudicationError("재심에 제공한 Skeleton이 immutable Plan 원본과 다릅니다.")
        extra_evidence_catalog["artifact:skeleton"] = candidate.model_dump(mode="json")
    dispute_evidence_catalog = evidence_catalog | extra_evidence_catalog
    source_catalog = inspection_source_catalog(project_map, {
        key: f"payload.evidence_catalog.{key}" for key in evidence_catalog
    })
    known_task_refs = {task.task_ref for task in plan.definition.tasks}
    validate_reviewer_submission_evidence(
        original_submission,
        evidence_catalog=evidence_catalog,
        known_task_refs=known_task_refs,
    )
    unknown_dispute_refs = set(proposal.evidence_refs) - set(dispute_evidence_catalog)
    if unknown_dispute_refs:
        raise PlanReviewAdjudicationError(
            f"disputed 제안이 제공되지 않은 evidence를 참조합니다: {sorted(unknown_dispute_refs)}"
        )
    original_by_digest = {
        sha256_digest(finding): finding for finding in original_submission.findings
    }
    if len(original_by_digest) != len(original_submission.findings):
        raise PlanReviewAdjudicationError("원검토 finding 객체가 중복됐습니다.")

    use_critical = route != "compact_plan_reviewer"
    selected_model = (
        (adapter.critical_model or adapter.model) if use_critical else adapter.model
    )
    selected_effort = (
        (adapter.critical_effort or adapter.effort) if use_critical else adapter.effort
    )
    allowed_fallbacks = (
        adapter.critical_allowed_fallbacks if use_critical else adapter.allowed_fallbacks
    )
    request = make_role_request(
        inventory=adapter.inventory,
        allowed_fallbacks=allowed_fallbacks,
        role=route,
        instructions=(
            "원검토의 각 finding과 disputed 반박을 immutable Plan·Goal·직접 evidence로 독립 재심한다. "
            "모든 원 finding digest에 upheld 또는 withdrawn을 정확히 한 번 제출한다. 불확실하거나 반박 "
            "근거가 부족하면 원거절을 유지하도록 upheld한다. upheld finding은 원 객체를 수정·요약하지 "
            "않고 adapter가 그대로 보존한다. withdrawn finding을 additional_findings로 다시 넣지 않는다. "
            "새로 직접 확인한 별도 결함만 ReviewFinding으로 additional_findings에 제출한다. 다수결이나 "
            "모델 fallback, status·admissible·score 선언은 사용하지 않는다. dispositions의 evidence_refs와 "
            "additional finding의 evidence_refs는 payload.evidence_catalog의 key만 사용한다. 원 disputed "
            "제안의 evidence_refs만 payload.evidence_catalog와 payload.extra_evidence_catalog의 합집합에서 "
            "해석한다. inspection_source_catalog의 등록 자료 본문은 검사 수단·phase의 실제 능력 판단에 "
            "사용하되 project:<entry_id>를 출력 evidence_refs로 직접 제출하지 않는다. 최종 finding이 없을 "
            "때만 ratings 다섯 항목을 제출하고, 하나라도 남으면 ratings는 null이다. "
            + EXPLICIT_VALIDATION_OBLIGATION_INSTRUCTIONS
            + PLANNING_PROJECT_PATH_V2_INSTRUCTIONS
            + PLANNING_VALIDATION_BOUNDARY_V2_INSTRUCTIONS
            + PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS
            + PLAN_TASK_RESULT_BOUNDARY_INSTRUCTIONS
        ),
        payload={
            "source_evaluation_digest": source_evaluation_digest,
            "immutable_input_digests": {
                "plan_activation_digest": plan.activation_digest,
                "goal_definition_digest": goal.definition_digest,
                "state_snapshot_digest": state.snapshot_digest,
                "project_map_revision_digest": project_map.revision_digest,
                "source_skeleton_digest": plan.definition.source_skeleton_digest,
                "evidence_catalog_digest": sha256_digest(evidence_catalog),
                "inspection_source_catalog_digest": sha256_digest(source_catalog),
            },
            "original_submission": original_submission.model_dump(mode="json"),
            "original_submission_digest": sha256_digest(original_submission),
            "original_finding_digests": list(original_by_digest),
            "dispute": proposal.model_dump(mode="json"),
            "evidence_catalog": evidence_catalog,
            "extra_evidence_catalog": extra_evidence_catalog,
            "inspection_source_catalog": source_catalog,
        },
        output_schema=PlanReviewAdjudicationDraft.model_json_schema(),
        model=selected_model,
        effort=selected_effort,
        inventory_digest=adapter.inventory_digest,
        cwd=str(Path(adapter.cwd).resolve()),
    )

    accepted_draft: PlanReviewAdjudicationDraft | None = None
    accepted_submission: ReviewerSubmission | None = None

    def validate_adjudication(value: dict[str, Any]) -> PlanReviewAdjudicationDraft:
        nonlocal accepted_draft, accepted_submission
        draft = PlanReviewAdjudicationDraft.model_validate(value)
        disposition_by_digest = {
            item.original_finding_digest: item for item in draft.dispositions
        }
        if disposition_by_digest.keys() != original_by_digest.keys():
            raise PlanReviewAdjudicationError(
                "재심은 모든 원 finding을 digest별 정확히 한 번 판정해야 합니다."
            )
        original_codes = {item.finding_code for item in original_submission.findings}
        if any(item.finding_code in original_codes for item in draft.additional_findings):
            raise PlanReviewAdjudicationError(
                "withdrawn 원 finding을 additional finding으로 다시 넣을 수 없습니다."
            )
        unknown_disposition_refs = {
            ref
            for item in draft.dispositions
            for ref in item.evidence_refs
            if ref not in evidence_catalog
        }
        if unknown_disposition_refs:
            raise PlanReviewAdjudicationError(
                "재심 disposition이 제공되지 않은 evidence를 참조합니다: "
                f"{sorted(unknown_disposition_refs)}"
            )
        upheld = tuple(
            finding
            for digest, finding in original_by_digest.items()
            if disposition_by_digest[digest].disposition == "upheld"
        )
        submission = ReviewerSubmission(
            reviewer_role=route,
            candidate_digest=plan.activation_digest,
            findings=upheld + draft.additional_findings,
            ratings=draft.ratings,
            evidence_catalog_digest=sha256_digest(evidence_catalog),
        )
        validate_reviewer_submission_evidence(
            submission,
            evidence_catalog=evidence_catalog,
            known_task_refs=known_task_refs,
        )
        accepted_draft = draft
        accepted_submission = submission
        return draft

    result = adapter.runner.run(request, validator=validate_adjudication)
    verify_role_receipt(request, result)
    if accepted_draft is None or accepted_submission is None:
        raise PlanReviewAdjudicationError("재심 검증 결과가 보존되지 않았습니다.")
    adapter.receipts.append(result.receipt)
    adjudication = PlanReviewAdjudication(
        source_evaluation_digest=source_evaluation_digest,
        dispute=proposal,
        original_submission=original_submission,
        dispositions=accepted_draft.dispositions,
        submission=accepted_submission,
        request_digest=request.request_digest,
        output_digest=sha256_digest(result.payload),
        receipt=result.receipt,
    )
    return adjudication.validate_source(
        source_evaluation_digest,
        original_submission,
        evidence_catalog,
        known_task_refs,
        dispute_evidence_catalog,
    )
