from __future__ import annotations

import copy
import hmac
from pathlib import Path
from typing import Any, Callable, Generic, Mapping, TypeVar

from pydantic import BaseModel, Field, model_validator

from ..canonical import sha256_digest
from ..core.domain import PlanDraft
from .domain import CandidateReadiness, RequestSpec
from .r31_domain import (
    ApproachBrief,
    CandidateEnvelope,
    CandidateObservationStatus,
    CandidateStatus,
    ConfidenceLevel,
    MissionSelectionReceipt,
    MissionPrimary,
    MissionResolutionHint,
    MissionReviewEvidence,
    ModelCallReceipt,
    ModelCallStatus,
    PlanVerdict,
    PlanQualityReport,
    PlanningMissionDefinition,
    PlanningRole,
    PlanningRunReceipt,
    ProjectStateSnapshot,
    ProjectProfileRevision,
    R31Model,
    RequirementAnalysisContext,
    RequirementExtractionDraft,
    RequirementExtractionReceipt,
    RequirementIntentReview,
    RequirementReviewEvidence,
    R31_SCORE_POLICY_ID,
    R31_SCORE_POLICY_VERSION,
    IntentReviewVerdict,
    RiskTag,
)
from .r31_intent import (
    PlanningMissionResolver,
    ProfileInspectionReceipt,
    RequestSpecAssemblyContext,
    RequirementAnalysisAssembly,
    RequirementAnalyzer,
)
from .r31_models import (
    CodexStructuredRoleRunner,
    ResolvedPlanningModel,
    StructuredRoleRequest,
    StructuredRoleError,
    strict_json_output_schema,
)
from .r31_pipeline import (
    CandidateReviewUnavailable,
    RequiredPlanningRoleUnavailable,
    RoleOutput,
)
from .validator import DeterministicPlanValidator


class ApproachBatch(R31Model):
    approaches: tuple[ApproachBrief, ...] = Field(min_length=1, max_length=3)


class CandidateBatch(R31Model):
    candidates: tuple[CandidateEnvelope, ...] = Field(min_length=1, max_length=2)


class CandidateWalkthroughReview(R31Model):
    candidate_id: str = Field(min_length=1, max_length=200)
    status: CandidateStatus
    quality_report: PlanQualityReport


class CandidateWalkthroughBatch(R31Model):
    reviews: tuple[CandidateWalkthroughReview, ...] = Field(min_length=1, max_length=2)


class CandidateReviewResult(R31Model):
    """후보 식별자와 생성 rationale을 보지 않는 reviewer의 출력 계약."""

    status: CandidateStatus
    quality_report: PlanQualityReport


def _bind_unique_trace_offsets(raw_request: str, draft: dict[str, Any]) -> None:
    traces = draft.get("traces")
    if not isinstance(traces, list):
        raise ValueError("requirement traces는 JSON array여야 합니다.")
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        excerpt = trace.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt:
            continue
        offsets: list[int] = []
        start = 0
        while True:
            observed = raw_request.find(excerpt, start)
            if observed < 0:
                break
            offsets.append(observed)
            start = observed + 1
        if len(offsets) != 1:
            raise ValueError(
                "raw request trace excerpt는 원문에서 정확히 한 번 나타나야 "
                f"합니다: {excerpt!r}, matches={len(offsets)}"
            )
        trace["start_offset"] = offsets[0]
        trace["end_offset"] = offsets[0] + len(excerpt)


def _bind_candidate_computed_fields(
    candidate: dict[str, Any],
    planning_run: PlanningRunReceipt,
) -> None:
    """모델 의미 출력에 런타임 소유 식별자와 digest를 결속한다."""

    plan_payload = candidate.get("plan")
    contract_payload = candidate.get("contract")
    if not isinstance(plan_payload, dict) or not isinstance(contract_payload, dict):
        raise ValueError("candidate plan과 contract는 JSON object여야 합니다.")
    request = planning_run.planning_input.request_spec
    plan_payload["project_id"] = request.project_id
    plan_payload["request_spec_digest"] = request.canonical_digest
    for work_item in plan_payload.get("work_items", []):
        if isinstance(work_item, dict):
            # WorkItem 모델 배정은 후속 Assigner의 책임이다.
            work_item["assignment"] = None
    plan = PlanDraft.model_validate(plan_payload)
    contract_payload["plan_digest"] = plan.canonical_digest
    specification_digests = {
        (work.client_ref, validation.criterion_id): sha256_digest(
            validation.specification
        )
        for work in plan.work_items
        for validation in work.validations
    }
    for binding in contract_payload.get("criterion_bindings", []):
        if not isinstance(binding, dict):
            continue
        key = (binding.get("work_item_ref"), binding.get("criterion_id"))
        if key in specification_digests:
            binding["specification_digest"] = specification_digests[key]


def _validate_candidate_structure(
    candidate: CandidateEnvelope,
    planning_run: PlanningRunReceipt,
) -> CandidateEnvelope:
    report = DeterministicPlanValidator().validate(
        planning_run.planning_input.request_spec,
        candidate.plan,
    )
    if report.readiness is not CandidateReadiness.READY_FOR_ASSIGNMENT:
        diagnostics = "; ".join(
            f"{issue.code.value}: {issue.message}" for issue in report.issues
        )
        raise ValueError(
            "candidate가 deterministic PlanValidator를 통과하지 못했습니다: "
            + diagnostics
        )
    return candidate


class ReviewedRequirementAnalysis(R31Model):
    assembly: RequirementAnalysisAssembly
    intent_review: RequirementIntentReview
    review_evidence: RequirementReviewEvidence
    model_call_receipts: tuple[ModelCallReceipt, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def evidence_is_consistent(self) -> "ReviewedRequirementAnalysis":
        if self.review_evidence.intent_review != self.intent_review:
            raise ValueError("요구 분석 결과와 review evidence의 intent review가 다릅니다.")
        expected = (
            self.review_evidence.extractor_receipt,
            self.review_evidence.reviewer_receipt,
        )
        if self.model_call_receipts != expected:
            raise ValueError("요구 분석 model receipt와 review evidence가 다릅니다.")
        return self


class ReviewedMissionResolution(R31Model):
    mission_selection: MissionSelectionReceipt
    review_evidence: MissionReviewEvidence
    model_call_receipts: tuple[ModelCallReceipt, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def evidence_is_consistent(self) -> "ReviewedMissionResolution":
        if self.review_evidence.resolved_selection != self.mission_selection:
            raise ValueError("Mission 결과와 review evidence가 다릅니다.")
        expected = (
            self.review_evidence.proposer_receipt,
            self.review_evidence.reviewer_receipt,
        )
        if self.model_call_receipts != expected:
            raise ValueError("Mission model receipt와 review evidence가 다릅니다.")
        return self


class ModelCallReceiptValidationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        receipts: tuple[ModelCallReceipt, ...] = (),
    ) -> None:
        super().__init__(message)
        self.receipts = receipts


class RequirementIntentReviewRejected(RuntimeError):
    def __init__(
        self,
        review: RequirementIntentReview,
        *,
        receipts: tuple[ModelCallReceipt, ...],
    ) -> None:
        super().__init__("독립 intent reviewer가 요구 추출 결과를 통과시키지 않았습니다.")
        self.review = review
        self.receipts = receipts


TModel = TypeVar("TModel", bound=BaseModel)

_SUCCESSFUL_MODEL_CALL_STATUSES = frozenset(
    {ModelCallStatus.SUCCEEDED, ModelCallStatus.SCHEMA_RECOVERED}
)


def _validated_success_receipts(
    output: RoleOutput[Any],
    *,
    expected_role: PlanningRole,
) -> tuple[ModelCallReceipt, ...]:
    """typed 역할 출력이 실제 성공 model call에 결속됐는지 검증한다."""

    receipts = output.receipts
    if not receipts:
        raise ModelCallReceiptValidationError(
            f"{expected_role.value} 역할의 model call receipt가 없습니다."
        )
    expected_output_digest = sha256_digest(output.value)
    call_ids: set[str] = set()
    for receipt in receipts:
        if receipt.call_id in call_ids:
            raise ModelCallReceiptValidationError(
                f"{expected_role.value} 역할의 model call receipt가 중복됐습니다.",
                receipts=receipts,
            )
        call_ids.add(receipt.call_id)
        if receipt.role is not expected_role:
            raise ModelCallReceiptValidationError(
                f"{expected_role.value} 역할 receipt의 role이 일치하지 않습니다.",
                receipts=receipts,
            )
        if receipt.status not in _SUCCESSFUL_MODEL_CALL_STATUSES:
            raise ModelCallReceiptValidationError(
                f"{expected_role.value} 역할 receipt가 성공 상태가 아닙니다.",
                receipts=receipts,
            )
        if receipt.thread_id is None or not receipt.turn_ids:
            raise ModelCallReceiptValidationError(
                f"{expected_role.value} 역할 receipt에 thread/turn 결속이 없습니다.",
                receipts=receipts,
            )
        if receipt.output_digest != expected_output_digest:
            raise ModelCallReceiptValidationError(
                f"{expected_role.value} 역할 receipt의 typed output digest가 다릅니다.",
                receipts=receipts,
            )
    return receipts


def _require_independent_review_sessions(
    producer_receipts: tuple[ModelCallReceipt, ...],
    reviewer_receipts: tuple[ModelCallReceipt, ...],
) -> None:
    producer_threads = {receipt.thread_id for receipt in producer_receipts}
    reviewer_threads = {receipt.thread_id for receipt in reviewer_receipts}
    if producer_threads & reviewer_threads:
        raise ModelCallReceiptValidationError(
            "생성 역할과 intent reviewer는 서로 다른 thread에서 실행되어야 합니다.",
            receipts=(*producer_receipts, *reviewer_receipts),
        )


def _request_intent_context(request_spec: RequestSpec) -> dict[str, Any]:
    """Mission/intent 판단에 필요한 사용자 계약만 전달하고 파일 본문은 제외한다."""

    return {
        "project_id": request_spec.project_id,
        "project_description": request_spec.project_description,
        "user_request": request_spec.user_request,
        "request_summary": request_spec.request_summary,
        "requirements": [
            item.model_dump(mode="json") for item in request_spec.requirements
        ],
        "out_of_scope": list(request_spec.out_of_scope),
    }


def _profile_context(
    profile: ProjectProfileRevision,
    *,
    sections: tuple[str, ...],
) -> dict[str, Any]:
    definition = profile.definition
    allowed = {"architecture", "validation", "runtime", "risk", "compatibility"}
    if not set(sections).issubset(allowed):
        raise ValueError("알 수 없는 ProjectProfile section을 요청했습니다.")
    return {
        "profile_revision_id": profile.profile_revision_id,
        "definition_digest": profile.definition_digest,
        "product_goal": definition.product_goal,
        "lifecycle_stage": definition.lifecycle_stage.value,
        "criticality": definition.criticality.value,
        "compatibility_policy": definition.compatibility_policy.value,
        "default_risk_tolerance": definition.default_risk_tolerance.value,
        "sections": {
            name: getattr(definition, name).model_dump(mode="json")
            for name in sections
        },
    }


def _planning_context_for_generation(
    planning_run: PlanningRunReceipt,
) -> dict[str, Any]:
    planning_input = planning_run.planning_input
    mission = planning_input.mission_selection.mission
    assert mission is not None
    section_names = {"architecture", "validation"}
    if mission.mutation_policy.value != "read_only":
        section_names.add("runtime")
    if mission.risk_tags:
        section_names.add("risk")
    if (
        mission.behavior_preservation.value != "not_applicable"
        or RiskTag.EXISTING_BEHAVIOR in mission.risk_tags
        or RiskTag.PUBLIC_CONTRACT_CHANGE in mission.risk_tags
    ):
        section_names.add("compatibility")
    request = planning_input.request_spec.model_dump(mode="json")
    return {
        "planning_input_digest": planning_run.planning_input_digest,
        "request_spec": request,
        "mission": planning_input.mission_selection.model_dump(mode="json"),
        "effective_policy": planning_input.effective_policy.model_dump(mode="json"),
        "requirement_extraction": _requirement_semantic_context(
            planning_input.requirement_extraction
        ),
        "project_snapshot": _snapshot_semantic_context(
            planning_input.project_snapshot
        ),
        "project_profile": _profile_context(
            planning_input.profile_revision,
            sections=tuple(sorted(section_names)),
        ),
        "input_artifact_refs": [
            {"artifact_type": item.artifact_type, "digest": item.digest}
            for item in sorted(
                planning_input.input_artifact_refs,
                key=lambda item: (item.artifact_type, item.digest),
            )
        ],
    }


def _requirement_semantic_context(
    receipt: RequirementExtractionReceipt,
) -> dict[str, Any]:
    def ordered(items: tuple[Any, ...], identifier: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in sorted(items, key=lambda item: getattr(item, identifier))
        ]

    return {
        "semantic_digest": receipt.semantic_digest,
        "raw_request_digest": receipt.raw_request_digest,
        "request_spec_digest": receipt.request_spec_digest,
        "mission_resolution_digest": receipt.mission_resolution_digest,
        "profile_definition_digest": receipt.profile_definition_digest,
        "traces": ordered(receipt.traces, "trace_id"),
        "requirements": ordered(receipt.requirements, "requirement_id"),
        "selected_profile_sections": sorted(
            item.value for item in receipt.selected_profile_sections
        ),
        "assumptions": ordered(receipt.assumptions, "note_id"),
        "implementation_suggestions": ordered(
            receipt.implementation_suggestions,
            "note_id",
        ),
        "unresolved_items": sorted(receipt.unresolved_items),
    }


def _snapshot_semantic_context(snapshot: ProjectStateSnapshot) -> dict[str, Any]:
    entries = sorted(
        (entry.model_dump(mode="json") for entry in snapshot.entries),
        key=lambda item: (
            item["path"].casefold(),
            item["kind"],
            item["entry_id"],
        ),
    )
    return {
        "semantic_digest": snapshot.semantic_digest,
        "project_id": snapshot.project_id,
        "mission_resolution_digest": snapshot.mission_resolution_digest,
        "entries": entries,
        "unknowns": sorted(snapshot.unknowns),
    }


def _context_source_snapshot_bindings(
    request_spec: RequestSpec,
    snapshot: ProjectStateSnapshot,
) -> list[dict[str, Any]]:
    by_path = {
        str(Path(entry.path).resolve(strict=False)).casefold(): entry
        for entry in snapshot.entries
    }
    bindings: list[dict[str, Any]] = []
    for source in sorted(request_spec.context_sources, key=lambda item: item.source_id):
        entry = by_path.get(str(Path(source.path).resolve(strict=False)).casefold())
        if entry is None or entry.content_digest != source.content_digest:
            raise ValueError(
                f"ContextSource와 snapshot 결속을 찾을 수 없습니다: {source.source_id}"
            )
        bindings.append(
            {
                "source_id": source.source_id,
                "source_path": source.path,
                "content_digest": source.content_digest,
                "snapshot_entry_id": entry.entry_id,
                "required_for_all_work_items": source.required_for_all_work_items,
            }
        )
    return bindings


def _bind_review_derived_fields(
    review: dict[str, Any],
    candidate: CandidateEnvelope,
) -> None:
    """reviewer의 의미 평가에서 결정적으로 계산 가능한 집계값을 결속한다."""

    report = review.get("quality_report")
    if not isinstance(report, dict):
        raise ValueError("quality_report는 JSON object여야 합니다.")
    report["planning_input_digest"] = candidate.planning_input_digest
    report["plan_digest"] = candidate.plan.canonical_digest

    findings = report.get("gate_findings")
    if not isinstance(findings, list) or not findings:
        raise ValueError("quality_report에는 Hard Gate 판정이 필요합니다.")
    verdicts = [
        item.get("plan_verdict")
        for item in findings
        if isinstance(item, dict)
    ]
    if len(verdicts) != len(findings):
        raise ValueError("Hard Gate 판정은 JSON object여야 합니다.")
    if PlanVerdict.BLOCKED.value in verdicts:
        derived_verdict = PlanVerdict.BLOCKED
    elif PlanVerdict.FAIL.value in verdicts:
        derived_verdict = PlanVerdict.FAIL
    elif verdicts and set(verdicts) == {PlanVerdict.NOT_APPLICABLE.value}:
        derived_verdict = PlanVerdict.NOT_APPLICABLE
    else:
        derived_verdict = PlanVerdict.PASS
    report["plan_verdict"] = derived_verdict.value

    ratings = report.get("dimension_ratings")
    if derived_verdict is PlanVerdict.PASS and isinstance(ratings, dict):
        names_and_weights = (
            ("goal_fit_change_safety", 25),
            ("verification_evidence_strength", 25),
            ("execution_risk_control", 20),
            ("maintainability_reproducibility", 20),
            ("resource_efficiency", 10),
        )
        if all(
            isinstance(ratings.get(name), int)
            and not isinstance(ratings.get(name), bool)
            for name, _ in names_and_weights
        ):
            numerator = sum(
                weight * ratings[name] for name, weight in names_and_weights
            )
            report["fitness_score"] = (numerator + 2) // 4
    elif derived_verdict is not PlanVerdict.PASS:
        report["fitness_score"] = None

    work_item_quality = report.get("work_item_quality")
    if isinstance(work_item_quality, list) and work_item_quality:
        axes = (
            "self_containment",
            "functional_cohesion",
            "acceptance_validation",
            "interface_clarity",
            "failure_retry",
        )
        if all(
            isinstance(item, dict)
            and isinstance(item.get("work_item_ref"), str)
            and all(
                isinstance(item.get(axis), int)
                and not isinstance(item.get(axis), bool)
                for axis in axes
            )
            for item in work_item_quality
        ):
            minimum = min(
                min(item[axis] for axis in axes) for item in work_item_quality
            )
            weakest_ref = min(
                item["work_item_ref"]
                for item in work_item_quality
                if min(item[axis] for axis in axes) == minimum
            )
            report["weakest_work_item_ref"] = weakest_ref
            report["weakest_work_item_rating"] = minimum
    else:
        report["weakest_work_item_ref"] = None
        report["weakest_work_item_rating"] = None

    parsed_report = PlanQualityReport.model_validate(report)
    if any(
        finding.runtime_status.value != "not_run"
        for finding in parsed_report.gate_findings
    ):
        raise ValueError(
            "계획 단계 Hard Gate의 runtime_status는 모두 not_run이어야 합니다."
        )

    if derived_verdict is PlanVerdict.BLOCKED:
        derived_status = CandidateStatus.BLOCKED
    elif derived_verdict is PlanVerdict.FAIL:
        diagnostics = [
            diagnostic
            for finding in parsed_report.gate_findings
            for diagnostic in finding.diagnostics
        ]
        derived_status = (
            CandidateStatus.NEEDS_REVISION
            if diagnostics and all(item.remediable for item in diagnostics)
            else CandidateStatus.REJECTED
        )
    elif derived_verdict is PlanVerdict.PASS:
        expected_refs = {item.client_ref for item in candidate.plan.work_items}
        actual_refs = {
            item.work_item_ref for item in parsed_report.work_item_quality
        }
        if actual_refs != expected_refs:
            raise ValueError(
                "Hard Gate PASS에는 PlanDraft의 모든 WorkItem 품질 평점이 정확히 "
                "한 번씩 필요합니다."
            )
        zero_refs = sorted(
            item.work_item_ref
            for item in parsed_report.work_item_quality
            if item.weakest_rating == 0
        )
        if zero_refs:
            raise ValueError(
                "0점 WorkItem 품질 축은 관련 Gate의 remediable FAIL 진단으로 "
                "표현해야 합니다: " + ", ".join(zero_refs)
            )
        if (
            parsed_report.dimension_ratings is None
            or parsed_report.fitness_score is None
            or parsed_report.tie_break_evidence is None
        ):
            raise ValueError(
                "Hard Gate PASS에는 5개 score dimension, 결정적 fitness_score와 "
                "tie-break 근거가 필요합니다."
            )
        derived_status = CandidateStatus.ADMISSIBLE
    else:
        derived_status = CandidateStatus.REJECTED
    review["status"] = derived_status.value


class _StructuredAdapter(Generic[TModel]):
    def __init__(
        self,
        *,
        runner: CodexStructuredRoleRunner,
        model: ResolvedPlanningModel,
        cwd: str | Path,
        instructions: str,
        expected_roles: frozenset[PlanningRole],
        receipt_sink: Callable[[ModelCallReceipt], None] | None = None,
    ) -> None:
        if model.role not in expected_roles:
            expected = ", ".join(sorted(item.value for item in expected_roles))
            raise ValueError(f"adapter 역할은 {expected} 중 하나여야 합니다.")
        self._runner = runner
        self._model = model
        self._cwd = str(Path(cwd).resolve(strict=True))
        self._instructions = instructions
        self._receipt_sink = receipt_sink

    def _call(
        self,
        payload: dict[str, Any],
        result_type: type[TModel],
        *,
        authoritative_bindings: Mapping[str, Any] | None = None,
        authoritative_transform: Callable[[dict[str, Any]], None] | None = None,
        semantic_validator: Callable[[TModel], TModel] | None = None,
    ) -> RoleOutput[TModel]:
        bindings = dict(authoritative_bindings or {})
        unknown_bindings = set(bindings) - set(result_type.model_fields)
        if unknown_bindings:
            raise ValueError(
                "출력 binding이 결과 schema에 없는 필드를 참조합니다: "
                f"{sorted(unknown_bindings)}"
            )
        bound_payload = copy.deepcopy(payload)
        if bindings:
            # Digest·artifact binding은 모델의 판단 대상이 아니다. 요청 digest에
            # 기대값을 포함하고, typed validation 직전에 권위 입력값으로 조립한다.
            bound_payload["authoritative_output_bindings"] = copy.deepcopy(bindings)
        bound_request = StructuredRoleRequest(
            role=self._model.role,
            instructions=self._instructions,
            payload=bound_payload,
            output_schema=strict_json_output_schema(result_type.model_json_schema()),
            model_id=self._model.model_id,
            reasoning_effort=self._model.reasoning_effort,
            inventory_digest=self._model.inventory_digest,
            cwd=self._cwd,
        )
        try:
            def validate_and_bind(candidate: dict[str, Any]) -> TModel:
                candidate.update(copy.deepcopy(bindings))
                if authoritative_transform is not None:
                    authoritative_transform(candidate)
                parsed = result_type.model_validate(candidate)
                if semantic_validator is not None:
                    return semantic_validator(parsed)
                return parsed

            result = self._runner.run(bound_request, validator=validate_and_bind)
        except StructuredRoleError as exc:
            if self._receipt_sink is not None and exc.receipt is not None:
                self._receipt_sink(exc.receipt)
            raise
        expected_schema_digest = sha256_digest(bound_request.output_schema)
        receipt_matches_request = (
            result.receipt.role is bound_request.role
            and result.receipt.model_id == bound_request.model_id
            and result.receipt.reasoning_effort == bound_request.reasoning_effort
            and hmac.compare_digest(
                result.receipt.inventory_digest,
                bound_request.inventory_digest,
            )
            and hmac.compare_digest(
                result.receipt.input_digest,
                bound_request.request_digest,
            )
            and hmac.compare_digest(
                result.receipt.output_schema_digest,
                expected_schema_digest,
            )
        )
        if not receipt_matches_request:
            if self._receipt_sink is not None:
                self._receipt_sink(result.receipt)
            raise ModelCallReceiptValidationError(
                f"{self._model.role.value} 역할 receipt가 실제 structured request와 다릅니다.",
                receipts=(result.receipt,),
            )
        parsed = result_type.model_validate(result.payload)
        if result.receipt.status not in _SUCCESSFUL_MODEL_CALL_STATUSES:
            if self._receipt_sink is not None:
                self._receipt_sink(result.receipt)
            raise ModelCallReceiptValidationError(
                f"{self._model.role.value} adapter가 성공 출력과 실패 receipt를 함께 받았습니다.",
                receipts=(result.receipt,),
            )
        receipt = ModelCallReceipt.model_validate(
            {
                **result.receipt.model_dump(mode="python"),
                # JSON decoder가 생략된 default를 채울 수 있으므로 raw payload가 아니라
                # 실제 downstream에 전달하는 typed artifact에 receipt를 결속한다.
                "output_digest": sha256_digest(parsed),
            }
        )
        if self._receipt_sink is not None:
            self._receipt_sink(receipt)
        output = RoleOutput(parsed, (receipt,))
        _validated_success_receipts(output, expected_role=self._model.role)
        return output


class StructuredMissionProposer(_StructuredAdapter[MissionResolutionHint]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.PURPOSE_RESOLVER}), **kwargs)

    def propose(
        self,
        request_spec: RequestSpec,
        profile_revision: ProjectProfileRevision,
    ) -> RoleOutput[MissionResolutionHint]:
        return self._call(
            {
                "request_spec": request_spec.model_dump(mode="json"),
                "project_profile": profile_revision.model_dump(mode="json"),
                "authority_order": [
                    "explicit_request",
                    "user_selected_mission",
                    "project_profile",
                    "model_recommendation",
                ],
            },
            MissionResolutionHint,
        )


class StructuredIntentReviewer(_StructuredAdapter[MissionResolutionHint]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.INTENT_REVIEWER}), **kwargs)

    def review(
        self,
        request_spec: RequestSpec,
        profile_revision: ProjectProfileRevision,
        proposal: MissionResolutionHint,
    ) -> RoleOutput[MissionResolutionHint]:
        return self._call(
            {
                "request": _request_intent_context(request_spec),
                "project_profile": _profile_context(
                    profile_revision,
                    sections=("risk", "compatibility"),
                ),
                "mission_proposal": proposal.model_dump(mode="json"),
                "review_contract": (
                    "누락·발명·권위 역전과 결과를 바꾸는 모호성만 판정하고 "
                    "생성기의 숨은 reasoning이나 자기 점수를 사용하지 않는다."
                ),
            },
            MissionResolutionHint,
        )


class StructuredRequirementExtractor(_StructuredAdapter[RequirementExtractionDraft]):
    """Luna 계열 purpose pass가 raw request에서 typed 요구 draft만 생성한다."""

    def __init__(
        self,
        *,
        analyzer: RequirementAnalyzer | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.PURPOSE_RESOLVER}), **kwargs)
        self._analyzer = analyzer or RequirementAnalyzer()

    def extract(
        self,
        context: RequirementAnalysisContext,
    ) -> RoleOutput[RequirementExtractionDraft]:
        return self._call(
            {
                "analysis_context": context.model_dump(mode="json"),
                "authority_rule": (
                    "필수 requirement/constraint는 raw trace, 선택된 profile source 또는 "
                    "snapshot entry 근거가 있어야 하며 가정·구현 제안으로 승격하지 않는다."
                ),
                "required_mission_projection": {
                    "observable_outcome": {
                        "kind": "observable_outcome",
                        "statement_must_equal_exactly": context.mission.observable_outcome,
                        "required_count": 1,
                        "mandatory": True,
                        "mission_grounded": True,
                    },
                    "forbidden_scope_exclusions_must_include_exactly": sorted(
                        context.mission.forbidden_scopes
                    ),
                    "snapshot_unknowns_must_remain_unresolved": sorted(
                        context.project_snapshot.unknowns
                    ),
                },
                "runtime_computed_output_fields": [
                    "traces[*].start_offset",
                    "traces[*].end_offset",
                ],
            },
            RequirementExtractionDraft,
            authoritative_bindings={
                "raw_request_digest": context.raw_request_digest,
                "mission_digest": context.mission.mission_digest,
                "profile_definition_digest": context.profile_definition_digest,
                "project_snapshot_digest": context.project_snapshot.snapshot_digest,
            },
            authoritative_transform=lambda draft: _bind_unique_trace_offsets(
                context.raw_request,
                draft,
            ),
            semantic_validator=lambda draft: self._analyzer.analyze(context, draft),
        )


class StructuredRequirementIntentReviewer(
    _StructuredAdapter[RequirementIntentReview]
):
    """생성 reasoning 없이 raw source와 extraction artifact만 독립 검토한다."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.INTENT_REVIEWER}), **kwargs)

    def review(
        self,
        context: RequirementAnalysisContext,
        draft: RequirementExtractionDraft,
    ) -> RoleOutput[RequirementIntentReview]:
        return self._call(
            {
                "analysis_context": context.model_dump(mode="json"),
                "extraction_artifact": draft.model_dump(mode="json"),
                "review_contract": [
                    "명시 필수 요구·제외 누락 검사",
                    "발명한 필수 요구와 잘못된 제외 검사",
                    "profile 우선순위 역전 검사",
                    "가정·구현 제안의 필수 요구 승격 검사",
                    "source span과 양방향 trace 검사",
                ],
            },
            RequirementIntentReview,
            authoritative_bindings={
                "analysis_context_digest": context.context_digest,
                "extraction_draft_digest": draft.draft_digest,
            },
        )


class MissionPlanningService:
    """Mission 제안·독립 review·결정적 우선순위 적용을 한 경계로 묶는다."""

    def __init__(
        self,
        *,
        resolver: PlanningMissionResolver,
        proposer: StructuredMissionProposer,
        reviewer: StructuredIntentReviewer,
    ) -> None:
        self._resolver = resolver
        self._proposer = proposer
        self._reviewer = reviewer

    def resolve(
        self,
        request_spec: RequestSpec,
        profile_revision: ProjectProfileRevision,
        user_selection: PlanningMissionDefinition | MissionPrimary | str | None = None,
        *,
        profile_inspection: ProfileInspectionReceipt | None = None,
    ) -> ReviewedMissionResolution:
        if request_spec.project_id != profile_revision.project_id:
            raise ValueError("RequestSpec과 ProjectProfile의 project_id가 다릅니다.")
        proposed = self._proposer.propose(request_spec, profile_revision)
        proposer_receipts = _validated_success_receipts(
            proposed,
            expected_role=PlanningRole.PURPOSE_RESOLVER,
        )
        reviewed = self._reviewer.review(
            request_spec,
            profile_revision,
            proposed.value,
        )
        reviewer_receipts = _validated_success_receipts(
            reviewed,
            expected_role=PlanningRole.INTENT_REVIEWER,
        )
        _require_independent_review_sessions(proposer_receipts, reviewer_receipts)
        if len(proposer_receipts) != 1 or len(reviewer_receipts) != 1:
            raise ModelCallReceiptValidationError(
                "Mission 역할마다 정확히 하나의 최종 model call receipt가 필요합니다.",
                receipts=(*proposer_receipts, *reviewer_receipts),
            )
        mission_selection = self._resolver.resolve(
            request_spec,
            profile_revision,
            user_selection,
            hint=reviewed.value,
            profile_inspection=profile_inspection,
        )
        evidence = MissionReviewEvidence(
            proposal=proposed.value,
            reviewed_proposal=reviewed.value,
            resolved_selection=mission_selection,
            proposer_receipt=proposer_receipts[0],
            reviewer_receipt=reviewer_receipts[0],
        )
        return ReviewedMissionResolution(
            mission_selection=mission_selection,
            review_evidence=evidence,
            model_call_receipts=(*proposer_receipts, *reviewer_receipts),
        )


class RequirementPlanningService:
    """요구 생성·결정적 검사·독립 review·R3 입력 조립을 한 경계로 묶는다."""

    def __init__(
        self,
        *,
        analyzer: RequirementAnalyzer,
        extractor: StructuredRequirementExtractor,
        reviewer: StructuredRequirementIntentReviewer,
    ) -> None:
        self._analyzer = analyzer
        self._extractor = extractor
        self._reviewer = reviewer

    def analyze_and_assemble(
        self,
        context: RequirementAnalysisContext,
        assembly_context: RequestSpecAssemblyContext,
    ) -> ReviewedRequirementAnalysis:
        generated = self._extractor.extract(context)
        extractor_receipts = _validated_success_receipts(
            generated,
            expected_role=PlanningRole.PURPOSE_RESOLVER,
        )
        draft = self._analyzer.analyze(context, generated.value)
        reviewed = self._reviewer.review(context, draft)
        reviewer_receipts = _validated_success_receipts(
            reviewed,
            expected_role=PlanningRole.INTENT_REVIEWER,
        )
        _require_independent_review_sessions(extractor_receipts, reviewer_receipts)
        if len(extractor_receipts) != 1 or len(reviewer_receipts) != 1:
            raise ModelCallReceiptValidationError(
                "요구 분석 역할마다 정확히 하나의 최종 model call receipt가 필요합니다.",
                receipts=(*extractor_receipts, *reviewer_receipts),
            )
        receipts = (*extractor_receipts, *reviewer_receipts)
        if reviewed.value.verdict is not IntentReviewVerdict.PASS:
            raise RequirementIntentReviewRejected(reviewed.value, receipts=receipts)
        assembly = self._analyzer.assemble(
            context,
            draft,
            assembly_context,
            intent_review=reviewed.value,
        )
        evidence = RequirementReviewEvidence(
            intent_review=reviewed.value,
            extractor_receipt=extractor_receipts[0],
            reviewer_receipt=reviewer_receipts[0],
        )
        return ReviewedRequirementAnalysis(
            assembly=assembly,
            intent_review=reviewed.value,
            review_evidence=evidence,
            model_call_receipts=receipts,
        )


class StructuredApproachGenerator(_StructuredAdapter[ApproachBatch]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.CANDIDATE_GENERATOR}), **kwargs)

    def generate(
        self,
        planning_run: PlanningRunReceipt,
        *,
        limit: int,
    ) -> RoleOutput[tuple[ApproachBrief, ...]]:
        mission = planning_run.planning_input.mission_selection.mission
        assert mission is not None
        root_cause_evidence_available = any(
            item.artifact_type in {"root_cause_evidence", "analysis_artifact"}
            for item in planning_run.planning_input.input_artifact_refs
        )
        result = self._call(
            {
                "planning_input": _planning_context_for_generation(planning_run),
                "candidate_limit": limit,
                "generation_mode": (
                    "diagnostic_work_item_only"
                    if mission.primary is MissionPrimary.BUGFIX_STABILIZATION
                    and not root_cause_evidence_available
                    else "implementation_plan"
                ),
                "required_difference_axes": [
                    "strategy_family",
                    "change_shape",
                    "compatibility",
                    "rollout_recovery",
                ],
                "risk_discovery_rule": (
                    "Mission risk tag를 보존하고, 실제 change surface가 immutable artifact "
                    "store·latest pointer·공유 기록을 다루면 shared_concurrency를 포함해 "
                    "구현에서 새로 드러난 risk tag를 추가한다."
                ),
            },
            ApproachBatch,
        )
        if len(result.value.approaches) > limit:
            raise ValueError("모델이 요청한 ApproachBrief 상한을 초과했습니다.")
        return RoleOutput(result.value.approaches, result.receipts)


class StructuredCandidateExpander(_StructuredAdapter[CandidateEnvelope]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.CANDIDATE_GENERATOR}), **kwargs)

    def expand(
        self,
        planning_run: PlanningRunReceipt,
        approach: ApproachBrief,
    ) -> RoleOutput[CandidateEnvelope]:
        mission = planning_run.planning_input.mission_selection.mission
        assert mission is not None
        request = planning_run.planning_input.request_spec

        return self._call(
            {
                "planning_input": _planning_context_for_generation(planning_run),
                "approach": approach.model_dump(mode="json"),
                "required_status": "generated",
                "score_forbidden": True,
                "plan_identifier_contract": {
                    "allowed_context_source_ids": sorted(
                        item.source_id for item in request.context_sources
                    ),
                    "required_context_source_ids_for_every_work_item": sorted(
                        item.source_id
                        for item in request.context_sources
                        if item.required_for_all_work_items
                    ),
                    "required_requirement_coverage_ids_exactly_once": sorted(
                        item.requirement_id for item in request.requirements
                    ),
                    "allowed_validation_capability_ids": sorted(
                        item.capability_id for item in request.available_validations
                    ),
                    "field_rules": [
                        "context_sources에는 allowed_context_source_ids만 넣는다. planning artifact나 WorkItem 참조는 넣지 않는다.",
                        "WorkItem 간 입력 관계는 dependencies와 sidecar dependency_contracts에 둔다.",
                        "requirement_coverage에는 required_requirement_coverage_ids_exactly_once만 사용한다.",
                        "모든 validation은 allowed_validation_capability_ids 중 하나에 결속한다.",
                    ],
                },
                "extraction_representation_contract": {
                    "coverage_ids": sorted(
                        item.requirement_id for item in request.requirements
                    ),
                    "excluded_outcomes": sorted(request.out_of_scope),
                    "observable_outcome": mission.observable_outcome,
                    "rule": (
                        "extraction의 exclusion·observable_outcome ID는 requirement_coverage에 "
                        "추가하지 않고 각각 plan_outcome.excluded_outcomes와 "
                        "plan_outcome.observable_outcome 및 integration validation으로 추적한다."
                    ),
                },
                "minimum_quality_obligations": [
                    "요청이 복수 후보 비교를 요구하면 최소 2개 비중복 approach signature와 전체 계획 그래프가 Hard Gate 비교에 들어간다는 완료조건·검증을 둔다.",
                    "각 WorkItem과 integration validation의 specification에 command 또는 절차, 입력, assertion, 기대 결과와 evidence 경로를 채운다.",
                    "각 acceptance criterion을 validation assertion과 기대 결과로 직접 확인한다.",
                    "existing_behavior 위험이면 기존 소비자·직렬화·상태 전이 회귀 범위와 기대값을 명시한다.",
                    "persistent artifact에는 atomic create 또는 CAS·lock 같은 경쟁 guard, 부분 저장 탐지, supersedes_selection_digest와 reconciliation을 failure/recovery 계약 및 동시성 검사에 명시한다.",
                    "등록 inventory로 구현 모듈·인터페이스·테스트 진입점을 특정하고 필요한 사전 probe를 WorkItem에 포함한다.",
                    "working_directory와 실행 명령은 frozen project_root에서 해석되는 상대 경로 또는 runtime resolver로 표현하고 사용자별 절대 경로를 만들지 않는다.",
                    "근거 없는 handoff를 만들지 말고 session hint가 후속 runtime 소유이면 WorkItem execution_requirements에서 그 사실을 명시한다.",
                    "계획 단계 validation은 not-run이며 성공했다고 주장하지 않는다.",
                ],
                "runtime_computed_output_fields": [
                    "plan.project_id",
                    "plan.request_spec_digest",
                    "plan.work_items[*].assignment",
                    "contract.plan_digest",
                    "contract.criterion_bindings[*].specification_digest",
                ],
            },
            CandidateEnvelope,
            authoritative_bindings={
                "parent_candidate_id": None,
                "version": 1,
                "refinement_round": 0,
                "status": CandidateStatus.GENERATED.value,
                "observation_status": CandidateObservationStatus.NOT_OBSERVED.value,
                "planning_input_digest": planning_run.planning_input_digest,
                "mission_resolution_digest": (
                    planning_run.planning_input.mission_selection.mission_resolution_digest
                ),
                "mission_primary": mission.primary.value,
                "approach": approach.model_dump(mode="json"),
                "quality_report": None,
                "policy_id": R31_SCORE_POLICY_ID,
                "policy_version": R31_SCORE_POLICY_VERSION,
            },
            authoritative_transform=lambda candidate: _bind_candidate_computed_fields(
                candidate,
                planning_run,
            ),
            semantic_validator=lambda candidate: _validate_candidate_structure(
                candidate,
                planning_run,
            ),
        )


class StructuredHardGateReviewer(_StructuredAdapter[CandidateReviewResult]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            expected_roles=frozenset(
                {PlanningRole.HARD_GATE_REVIEWER, PlanningRole.CRITICAL_REVIEWER}
            ),
            **kwargs,
        )

    def review(
        self,
        planning_run: PlanningRunReceipt,
        candidate: CandidateEnvelope,
    ) -> RoleOutput[CandidateEnvelope]:
        request = planning_run.planning_input.request_spec
        mission = planning_run.planning_input.mission_selection.mission
        assert mission is not None

        def bind_review_provenance(review: dict[str, Any]) -> None:
            _bind_review_derived_fields(review, candidate)

        def validate_review(review: CandidateReviewResult) -> CandidateReviewResult:
            CandidateEnvelope.model_validate(
                {
                    **candidate.model_dump(mode="python"),
                    "status": review.status,
                    "quality_report": review.quality_report,
                }
            )
            return review

        try:
            result = self._call(
                {
                    "frozen_contract": {
                        "request": _request_intent_context(
                            request
                        ),
                        "mission": planning_run.planning_input.mission_selection.model_dump(
                            mode="json"
                        ),
                        "requirements": _requirement_semantic_context(
                            planning_run.planning_input.requirement_extraction
                        ),
                        "project_snapshot": _snapshot_semantic_context(
                            planning_run.planning_input.project_snapshot
                        ),
                        "context_source_snapshot_bindings": (
                            _context_source_snapshot_bindings(
                                request,
                                planning_run.planning_input.project_snapshot,
                            )
                        ),
                        "effective_policy": planning_run.planning_input.effective_policy.model_dump(
                            mode="json"
                        ),
                        "profile": _profile_context(
                            planning_run.planning_input.profile_revision,
                            sections=(
                                "architecture",
                                "validation",
                                "runtime",
                                "risk",
                                "compatibility",
                            ),
                        ),
                        "representation_contract": {
                            "requirement_coverage_ids": sorted(
                                item.requirement_id for item in request.requirements
                            ),
                            "explicit_request_constraint_ids": sorted(
                                item.requirement_id
                                for item in planning_run.planning_input.requirement_extraction.requirements
                                if item.kind.value == "constraint"
                            ),
                            "excluded_outcomes": sorted(request.out_of_scope),
                            "observable_outcome": mission.observable_outcome,
                            "rule": (
                                "explicit_request_constraints에는 extraction kind=constraint만 "
                                "들어간다. kind=exclusion은 중복 constraint가 아니며 "
                                "RequestSpec.out_of_scope와 Mission/EffectivePolicy.forbidden_scopes로 "
                                "판정한다. exclusion·observable_outcome ID는 requirement coverage "
                                "대상이 아니며 각각 excluded_outcomes와 observable_outcome 및 "
                                "integration validation 경로로 판정한다."
                            ),
                        },
                        "planned_session_hints": [
                            {
                                "role": "candidate_generator",
                                "strategy": "reuse",
                                "reusable_prefix_digest": planning_run.planning_input_digest,
                            },
                            {
                                "role": self._model.role.value,
                                "strategy": "isolate",
                                "independent_review_session": True,
                            },
                        ],
                        "stage_owned_fields": {
                            "session_hints": (
                                "review 뒤 PlanningSearchPipeline._decorate_outcome이 결속한다. "
                                "candidate 내부에 없다는 이유만으로 X-06을 실패시키지 않는다."
                            )
                        },
                    },
                    # 후보 ID, ApproachBrief ID/rationale와 생성기 자기평가는 의도적으로 제외한다.
                    "anonymous_candidate": {
                        "planning_input_digest": candidate.planning_input_digest,
                        "mission_primary": candidate.mission_primary.value,
                        "approach_contract": {
                            "strategy_family": candidate.approach.strategy_family,
                            "change_shape": candidate.approach.change_shape,
                            "compatibility": candidate.approach.compatibility,
                            "rollout_recovery": candidate.approach.rollout_recovery,
                        },
                        "plan_digest": candidate.plan.canonical_digest,
                        "plan": candidate.plan.model_dump(mode="json"),
                        "contract": candidate.contract.model_dump(mode="json"),
                        "risk_tags": sorted(
                            {
                                *(tag.value for tag in candidate.approach.risk_tags),
                                *(
                                    tag.value
                                    for tag in (
                                        planning_run.planning_input.mission_selection.mission.risk_tags
                                        if planning_run.planning_input.mission_selection.mission
                                        is not None
                                        else ()
                                    )
                                ),
                            }
                        ),
                    },
                    "gate_order": [
                        "intent",
                        "plan",
                        "engineering",
                        "verification",
                        "execution",
                    ],
                    "runtime_evidence_rule": (
                        "계획 단계에서는 validation 성공을 주장하지 말고 정의 가능성만 판정한다."
                    ),
                    "runtime_computed_output_fields": [
                        "status",
                        "quality_report.planning_input_digest",
                        "quality_report.plan_digest",
                        "quality_report.plan_verdict",
                        "quality_report.fitness_score",
                        "quality_report.weakest_work_item_ref",
                        "quality_report.weakest_work_item_rating",
                    ],
                    "review_output_contract": [
                        "status는 Gate·진단·WorkItem 평점에서 runtime이 계산하므로 의미 판정은 quality_report에 완전하게 표현한다.",
                        "Hard Gate PASS이면 모든 WorkItem 평점을 정확히 한 번 제출하고 어떤 축도 0이 아니어야 한다.",
                        "WorkItem 축이 0이면 관련 Gate를 fail로 두고 remediable 진단에 WorkItem을 결속한다.",
                        "Hard Gate PASS 후보에만 다섯 score dimension/evidence와 tie-break 근거를 제출한다.",
                        "계획 단계의 모든 runtime_status는 not_run이다.",
                    ],
                },
                CandidateReviewResult,
                authoritative_transform=bind_review_provenance,
                semantic_validator=validate_review,
            )
            reviewed = CandidateEnvelope.model_validate(
                {
                    **candidate.model_dump(mode="python"),
                    "status": result.value.status,
                    "quality_report": result.value.quality_report,
                }
            )
            return RoleOutput(reviewed, result.receipts)
        except StructuredRoleError as exc:
            raise CandidateReviewUnavailable(
                "이 후보의 필수 semantic Hard Gate review가 실패했습니다.",
                receipt=exc.receipt,
            ) from exc


class StructuredCandidateRefiner(_StructuredAdapter[CandidateEnvelope]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.CANDIDATE_GENERATOR}), **kwargs)

    def refine(
        self,
        planning_run: PlanningRunReceipt,
        candidate: CandidateEnvelope,
    ) -> RoleOutput[CandidateEnvelope]:
        mission = planning_run.planning_input.mission_selection.mission
        assert mission is not None
        request = planning_run.planning_input.request_spec

        def bind_refined_fields(child: dict[str, Any]) -> None:
            approach_payload = child.get("approach")
            if not isinstance(approach_payload, dict):
                raise ValueError("refined candidate approach는 JSON object여야 합니다.")
            approach_payload.update(
                {
                    "approach_id": candidate.approach.approach_id,
                    "mission_primary": candidate.approach.mission_primary.value,
                    "strategy_family": candidate.approach.strategy_family,
                    "change_shape": candidate.approach.change_shape,
                    "compatibility": candidate.approach.compatibility,
                    "rollout_recovery": candidate.approach.rollout_recovery,
                }
            )
            observed_tags = approach_payload.get("risk_tags")
            if not isinstance(observed_tags, list):
                observed_tags = []
            approach_payload["risk_tags"] = sorted(
                {
                    *observed_tags,
                    *(tag.value for tag in candidate.approach.risk_tags),
                }
            )
            _bind_candidate_computed_fields(child, planning_run)

        return self._call(
            {
                "planning_input": _planning_context_for_generation(planning_run),
                "parent_candidate": candidate.model_dump(mode="json"),
                "required_lineage": {
                    "parent_candidate_id": candidate.candidate_id,
                    "version": candidate.version + 1,
                    "refinement_round": 1,
                    "status": "generated",
                },
                "rule": (
                    "부모의 approach ID·네 signature 축과 risk tag를 보존하고, 부모 "
                    "점수와 Gate 판정은 복사하지 않은 전체 재평가 가능 후보를 만든다."
                ),
                "plan_identifier_contract": {
                    "allowed_context_source_ids": sorted(
                        item.source_id for item in request.context_sources
                    ),
                    "required_context_source_ids_for_every_work_item": sorted(
                        item.source_id
                        for item in request.context_sources
                        if item.required_for_all_work_items
                    ),
                    "required_requirement_coverage_ids_exactly_once": sorted(
                        item.requirement_id for item in request.requirements
                    ),
                    "allowed_validation_capability_ids": sorted(
                        item.capability_id for item in request.available_validations
                    ),
                },
                "minimum_refinement_obligations": [
                    "부모 quality_report의 remediable finding을 구체적인 plan·sidecar delta로 해결한다.",
                    "validation specification에 실행 절차, assertion, 기대 결과와 evidence 경로를 유지한다.",
                    "exclusion·observable_outcome extraction ID를 requirement_coverage에 넣지 않는다.",
                    "부모 risk tag를 보존하고 finding에서 드러난 새 구현 위험 tag는 추가한다.",
                ],
                "runtime_computed_output_fields": [
                    "plan.project_id",
                    "plan.request_spec_digest",
                    "plan.work_items[*].assignment",
                    "contract.plan_digest",
                    "contract.criterion_bindings[*].specification_digest",
                ],
            },
            CandidateEnvelope,
            authoritative_bindings={
                "parent_candidate_id": candidate.candidate_id,
                "version": candidate.version + 1,
                "refinement_round": 1,
                "status": CandidateStatus.GENERATED.value,
                "observation_status": CandidateObservationStatus.NOT_OBSERVED.value,
                "planning_input_digest": planning_run.planning_input_digest,
                "mission_resolution_digest": (
                    planning_run.planning_input.mission_selection.mission_resolution_digest
                ),
                "mission_primary": mission.primary.value,
                "quality_report": None,
                "policy_id": R31_SCORE_POLICY_ID,
                "policy_version": R31_SCORE_POLICY_VERSION,
            },
            authoritative_transform=bind_refined_fields,
            semantic_validator=lambda child: _validate_candidate_structure(
                child,
                planning_run,
            ),
        )


class StructuredTopKWalkthrough(_StructuredAdapter[CandidateWalkthroughBatch]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(expected_roles=frozenset({PlanningRole.SCORER_SELECTOR}), **kwargs)

    def walkthrough(
        self,
        planning_run: PlanningRunReceipt,
        candidates: tuple[CandidateEnvelope, ...],
    ) -> RoleOutput[tuple[CandidateEnvelope, ...]]:
        # 입력 순서는 run마다 digest로 섞되 재현 가능하게 유지한다.
        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: sha256_digest(
                    {
                        "planning_input_digest": planning_run.planning_input_digest,
                        "candidate_id": item.candidate_id,
                    }
                ),
            )
        )
        by_id = {item.candidate_id: item for item in ordered}

        def bind_review_provenance(batch: dict[str, Any]) -> None:
            reviews = batch.get("reviews")
            if not isinstance(reviews, list):
                raise ValueError("walkthrough reviews는 JSON array여야 합니다.")
            for review in reviews:
                if not isinstance(review, dict):
                    continue
                source = by_id.get(review.get("candidate_id"))
                if source is not None:
                    _bind_review_derived_fields(review, source)

        def validate_walkthrough(
            batch: CandidateWalkthroughBatch,
        ) -> CandidateWalkthroughBatch:
            if {item.candidate_id for item in batch.reviews} != set(by_id):
                raise ValueError("walkthrough 결과의 candidate ID 집합이 입력과 다릅니다.")
            for review in batch.reviews:
                if review.status is CandidateStatus.GENERATED:
                    raise ValueError("walkthrough는 각 후보의 Gate 판정을 제출해야 합니다.")
                source = by_id[review.candidate_id]
                CandidateEnvelope.model_validate(
                    {
                        **source.model_dump(mode="python"),
                        "status": review.status,
                        "quality_report": review.quality_report,
                    }
                )
            return batch

        try:
            result = self._call(
                {
                    "planning_input_digest": planning_run.planning_input_digest,
                    "mission": planning_run.planning_input.mission_selection.model_dump(
                        mode="json"
                    ),
                    "candidates": [item.model_dump(mode="json") for item in ordered],
                    "walkthrough_contract": [
                        "dependency 순서와 produces/consumes 계약",
                        "migration 중간 호환·checkpoint·reconciliation",
                        "부분 실패·재실행·rollback",
                        "후속 작업이 앞선 작업을 갈아엎는지 여부",
                    ],
                    "rule": (
                        "후보를 합치거나 정의를 재출력하지 말고 입력 candidate_id별로 "
                        "status와 전체 quality_report만 갱신한다."
                    ),
                    "runtime_computed_output_fields": [
                        "reviews[*].status",
                        "reviews[*].quality_report.planning_input_digest",
                        "reviews[*].quality_report.plan_digest",
                        "reviews[*].quality_report.plan_verdict",
                        "reviews[*].quality_report.fitness_score",
                        "reviews[*].quality_report.weakest_work_item_ref",
                        "reviews[*].quality_report.weakest_work_item_rating",
                    ],
                },
                CandidateWalkthroughBatch,
                authoritative_transform=bind_review_provenance,
                semantic_validator=validate_walkthrough,
            )
        except StructuredRoleError as exc:
            raise RequiredPlanningRoleUnavailable(
                "필수 Top-K dependency/migration walkthrough가 실패했습니다.",
                receipt=exc.receipt,
            ) from exc
        reviews = {item.candidate_id: item for item in result.value.reviews}
        walked = tuple(
            CandidateEnvelope.model_validate(
                {
                    **candidate.model_dump(mode="python"),
                    "status": reviews[candidate.candidate_id].status,
                    "quality_report": reviews[candidate.candidate_id].quality_report,
                }
            )
            for candidate in ordered
        )
        return RoleOutput(walked, result.receipts)


_CRITICAL_RISK_TAGS = frozenset(
    {
        RiskTag.PUBLIC_CONTRACT_CHANGE,
        RiskTag.PERSISTENT_STATE_CHANGE,
        RiskTag.DESTRUCTIVE_EFFECT,
        RiskTag.EXTERNAL_EFFECT,
        RiskTag.SHARED_CONCURRENCY,
        RiskTag.SECURITY_SENSITIVE,
    }
)


class RiskRoutedHardGateReviewer:
    """위험 후보에는 필수 critical reviewer를 적용하고 조용히 fallback하지 않는다."""

    def __init__(
        self,
        *,
        general: StructuredHardGateReviewer,
        critical: StructuredHardGateReviewer | None,
    ) -> None:
        self._general = general
        self._critical = critical

    def review(
        self,
        planning_run: PlanningRunReceipt,
        candidate: CandidateEnvelope,
    ) -> RoleOutput[CandidateEnvelope]:
        mission = planning_run.planning_input.mission_selection.mission
        assert mission is not None
        needs_critical = bool(
            mission.primary is MissionPrimary.MIGRATION_MODERNIZATION
            or set(mission.risk_tags) & _CRITICAL_RISK_TAGS
            or set(candidate.approach.risk_tags) & _CRITICAL_RISK_TAGS
            or mission.confidence is ConfidenceLevel.LOW
        )
        if not needs_critical:
            return self._general.review(planning_run, candidate)
        if self._critical is None:
            raise RequiredPlanningRoleUnavailable(
                "migration·보안·데이터·동시성·외부 효과 또는 low-confidence 후보에 필요한 "
                "critical reviewer를 사용할 수 없습니다."
            )
        return self._critical.review(planning_run, candidate)


__all__ = [
    "ApproachBatch",
    "CandidateBatch",
    "CandidateReviewResult",
    "CandidateWalkthroughBatch",
    "CandidateWalkthroughReview",
    "MissionPlanningService",
    "ModelCallReceiptValidationError",
    "RequirementIntentReviewRejected",
    "RequirementPlanningService",
    "ReviewedMissionResolution",
    "ReviewedRequirementAnalysis",
    "RiskRoutedHardGateReviewer",
    "StructuredApproachGenerator",
    "StructuredCandidateExpander",
    "StructuredCandidateRefiner",
    "StructuredHardGateReviewer",
    "StructuredIntentReviewer",
    "StructuredMissionProposer",
    "StructuredRequirementExtractor",
    "StructuredRequirementIntentReviewer",
    "StructuredTopKWalkthrough",
]
