from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import ValidationError

from .r31_domain import (
    ApproachBrief,
    R31_SCORE_POLICY_ID,
    R31_SCORE_POLICY_VERSION,
    CandidateEnvelope,
    CandidateStatus,
    ConfidenceLevel,
    FindingSeverity,
    GateDiagnostic,
    GateFinding,
    GateName,
    ModelCallReceipt,
    PlanningRole,
    PlanningRunReceipt,
    PlanningRunStatus,
    PlanningSearchOutcome,
    PlanQualityReport,
    PlanVerdict,
    SearchOutcomeStatus,
    SelectionReceipt,
    SelectionSource,
    SessionHint,
    SessionStrategy,
    MissionPrimary,
)
from .r31_search import DeterministicPlanningSearch, deduplicate_candidates
from .r31_store import PlanningArtifactRepository, PlanningRunService
from .domain import CandidateReadiness, PlanValidationReport
from .validator import DeterministicPlanValidator


T = TypeVar("T")


@dataclass(frozen=True)
class RoleOutput(Generic[T]):
    value: T
    receipts: tuple[ModelCallReceipt, ...] = ()


class ApproachGenerator(Protocol):
    def generate(
        self,
        planning_run: PlanningRunReceipt,
        *,
        limit: int,
    ) -> RoleOutput[tuple[ApproachBrief, ...]]: ...


class CandidateExpander(Protocol):
    def expand(
        self,
        planning_run: PlanningRunReceipt,
        approach: ApproachBrief,
    ) -> RoleOutput[CandidateEnvelope]: ...


class HardGateReviewer(Protocol):
    def review(
        self,
        planning_run: PlanningRunReceipt,
        candidate: CandidateEnvelope,
    ) -> RoleOutput[CandidateEnvelope]: ...


class CandidateRefiner(Protocol):
    def refine(
        self,
        planning_run: PlanningRunReceipt,
        candidate: CandidateEnvelope,
    ) -> RoleOutput[CandidateEnvelope]: ...


class TopKWalkthroughReviewer(Protocol):
    def walkthrough(
        self,
        planning_run: PlanningRunReceipt,
        candidates: tuple[CandidateEnvelope, ...],
    ) -> RoleOutput[tuple[CandidateEnvelope, ...]]: ...


class PlanningPipelineError(RuntimeError):
    pass


class RequiredPlanningRoleUnavailable(PlanningPipelineError):
    """강한 reviewer 부재를 낮은 역할로 숨겨 fallback하지 않기 위한 신호."""

    def __init__(
        self,
        message: str,
        *,
        receipt: ModelCallReceipt | None = None,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt


class CandidateReviewUnavailable(PlanningPipelineError):
    """한 후보의 semantic review 실패를 다른 후보와 격리하기 위한 신호."""

    def __init__(
        self,
        message: str,
        *,
        receipt: ModelCallReceipt | None = None,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt


class PlanningSearchPipeline:
    """R3.1의 제한형 Generate → Gate → Top-K → Refine 파이프라인."""

    def __init__(
        self,
        *,
        run_service: PlanningRunService,
        artifact_repository: PlanningArtifactRepository,
        approach_generator: ApproachGenerator,
        candidate_expander: CandidateExpander,
        hard_gate_reviewer: HardGateReviewer,
        candidate_refiner: CandidateRefiner | None = None,
        top_k_walkthrough: TopKWalkthroughReviewer | None = None,
        search_engine: DeterministicPlanningSearch | None = None,
        structural_validator: DeterministicPlanValidator | None = None,
        allow_multiple_analysis_methods: bool = False,
    ) -> None:
        self._runs = run_service
        self._artifacts = artifact_repository
        self._generator = approach_generator
        self._expander = candidate_expander
        self._reviewer = hard_gate_reviewer
        self._refiner = candidate_refiner
        self._walkthrough = top_k_walkthrough
        self._search = search_engine or DeterministicPlanningSearch()
        self._structural_validator = structural_validator or DeterministicPlanValidator()
        self._allow_multiple_analysis_methods = allow_multiple_analysis_methods

    def search(self, frozen_run_input: PlanningRunReceipt) -> PlanningSearchOutcome:
        if frozen_run_input.status not in {
            PlanningRunStatus.FROZEN,
            PlanningRunStatus.SEARCHING,
        }:
            raise PlanningPipelineError("frozen 또는 복구 중인 searching PlanningRun만 검색할 수 있습니다.")
        searching = (
            self._runs.transition(
                frozen_run_input.run_id,
                PlanningRunStatus.SEARCHING,
            )
            if frozen_run_input.status is PlanningRunStatus.FROZEN
            else frozen_run_input
        )
        candidates: list[CandidateEnvelope] = []
        receipts: list[ModelCallReceipt] = []
        partial_failures: list[str] = []
        try:
            limit = self._initial_limit(searching)
            generated = self._generator.generate(searching, limit=limit)
            self._record_receipts(searching.run_id, generated.receipts, receipts)
            approaches = self._deduplicate_approaches(generated.value)
            if not approaches:
                return self._finish_failure(
                    searching,
                    candidates=(),
                    receipts=tuple(receipts),
                    reason="ApproachBrief가 생성되지 않았습니다.",
                )
            if len(approaches) > limit:
                raise PlanningPipelineError(
                    f"초기 후보 상한 {limit}개를 초과한 ApproachBrief가 생성됐습니다."
                )

            expanded: list[CandidateEnvelope] = []
            for approach in approaches:
                try:
                    result = self._expander.expand(searching, approach)
                    self._record_receipts(searching.run_id, result.receipts, receipts)
                    self._validate_initial_candidate(searching, approach, result.value)
                    expanded.append(result.value)
                except RequiredPlanningRoleUnavailable:
                    raise
                except Exception as exc:  # 한 branch의 실패는 다른 후보를 중단시키지 않는다.
                    self._capture_receipt(searching.run_id, exc, receipts)
                    partial_failures.append(
                        f"{approach.approach_id} 후보 확장 실패: {type(exc).__name__}"
                    )
            expanded = list(deduplicate_candidates(expanded))
            if not expanded:
                return self._finish_failure(
                    searching,
                    candidates=(),
                    receipts=tuple(receipts),
                    reason="모든 후보 확장이 실패했습니다.",
                    partial_failures=tuple(partial_failures),
                )

            structurally_ready: list[CandidateEnvelope] = []
            for candidate in expanded:
                report = self._structural_validator.validate(
                    searching.planning_input.request_spec,
                    candidate.plan,
                )
                if report.readiness is CandidateReadiness.READY_FOR_ASSIGNMENT:
                    structurally_ready.append(candidate)
                else:
                    rejected = self._structural_finding(candidate, report)
                    candidates.append(rejected)

            reviewed: list[CandidateEnvelope] = []
            for candidate in structurally_ready:
                try:
                    result = self._reviewer.review(searching, candidate)
                    self._record_receipts(searching.run_id, result.receipts, receipts)
                    self._validate_review(candidate, result.value)
                    reviewed.append(result.value)
                    candidates.append(result.value)
                except CandidateReviewUnavailable as exc:
                    self._capture_receipt(searching.run_id, exc, receipts)
                    blocked_candidate = self._review_unavailable_finding(
                        candidate,
                        message=str(exc),
                        receipt=exc.receipt,
                    )
                    reviewed.append(blocked_candidate)
                    candidates.append(blocked_candidate)
                    partial_failures.append(
                        f"{candidate.candidate_id} Hard Gate 검토 불가: {exc}"
                    )
                except RequiredPlanningRoleUnavailable:
                    raise
                except Exception as exc:
                    self._capture_receipt(searching.run_id, exc, receipts)
                    blocked_candidate = self._review_unavailable_finding(
                        candidate,
                        message=(
                            "필수 semantic Hard Gate 결과를 후보에 안전하게 결속하지 "
                            f"못했습니다: {type(exc).__name__}"
                        ),
                        receipt=getattr(exc, "receipt", None),
                    )
                    partial_failures.append(
                        f"{candidate.candidate_id} Hard Gate 검토 실패: {type(exc).__name__}"
                    )
                    reviewed.append(blocked_candidate)
                    candidates.append(blocked_candidate)

            remediation_targets = self._remediation_targets(reviewed)
            remediation_performed = bool(remediation_targets and self._refiner is not None)
            if remediation_performed:
                remediated = self._refine_candidates(
                    searching,
                    remediation_targets,
                    candidates=candidates,
                    receipts=receipts,
                    partial_failures=partial_failures,
                )
                reviewed.extend(remediated)
                candidates.extend(remediated)

            preliminary = self._search.search(searching, candidates)
            if (
                preliminary.status is SearchOutcomeStatus.READY_FOR_REVIEW
                and self._walkthrough is not None
            ):
                by_reviewed_id = {item.candidate_id: item for item in reviewed}
                walkthrough_input = tuple(
                    by_reviewed_id[item]
                    for item in preliminary.top_k_candidate_ids
                )
                walked = self._walkthrough.walkthrough(searching, walkthrough_input)
                self._record_receipts(searching.run_id, walked.receipts, receipts)
                self._validate_walkthrough(walkthrough_input, walked.value)
                replacements = {item.candidate_id: item for item in walked.value}
                reviewed = [replacements.get(item.candidate_id, item) for item in reviewed]
                candidates = [
                    replacements.get(item.candidate_id, item) for item in candidates
                ]
                preliminary = self._search.search(searching, candidates)
            if (
                preliminary.status is SearchOutcomeStatus.READY_FOR_REVIEW
                and self._refiner is not None
                and not remediation_performed
            ):
                by_id = {item.candidate_id: item for item in reviewed}
                refined = self._refine_candidates(
                    searching,
                    tuple(by_id[candidate_id] for candidate_id in preliminary.top_k_candidate_ids),
                    candidates=candidates,
                    receipts=receipts,
                    partial_failures=partial_failures,
                )
                candidates.extend(refined)
                outcome = self._search.search(searching, candidates)
            else:
                outcome = preliminary

            outcome = self._decorate_outcome(
                outcome,
                receipts=tuple(receipts),
                partial_failures=tuple(partial_failures),
            )
            return self._finish(searching, outcome)
        except RequiredPlanningRoleUnavailable as exc:
            if exc.receipt is not None:
                self._record_receipts(searching.run_id, (exc.receipt,), receipts)
            return self._finish_blocked(
                searching,
                candidates=tuple(candidates),
                receipts=tuple(receipts),
                reason=str(exc),
            )
        except Exception as exc:
            self._capture_receipt(searching.run_id, exc, receipts)
            return self._finish_failure(
                searching,
                candidates=tuple(candidates),
                receipts=tuple(receipts),
                reason=f"Planning search 실패: {type(exc).__name__}: {exc}",
                partial_failures=tuple(partial_failures),
            )

    def _record_receipts(
        self,
        run_id: str,
        new_receipts: tuple[ModelCallReceipt, ...],
        receipts: list[ModelCallReceipt],
    ) -> None:
        for receipt in new_receipts:
            if any(item.call_id == receipt.call_id for item in receipts):
                continue
            receipts.append(receipt)
            self._artifacts.save_model_call_receipt(run_id, receipt)

    def _capture_receipt(
        self,
        run_id: str,
        error: Exception,
        receipts: list[ModelCallReceipt],
    ) -> None:
        captured: list[ModelCallReceipt] = []
        receipt = getattr(error, "receipt", None)
        if isinstance(receipt, ModelCallReceipt):
            captured.append(receipt)
        many = getattr(error, "receipts", ())
        if isinstance(many, tuple):
            captured.extend(item for item in many if isinstance(item, ModelCallReceipt))
        self._record_receipts(run_id, tuple(captured), receipts)

    @staticmethod
    def _remediation_targets(
        reviewed: list[CandidateEnvelope],
    ) -> tuple[CandidateEnvelope, ...]:
        eligible: list[tuple[int, str, CandidateEnvelope]] = []
        for candidate in reviewed:
            report = candidate.quality_report
            if candidate.status is not CandidateStatus.NEEDS_REVISION or report is None:
                continue
            diagnostics = tuple(
                diagnostic
                for finding in report.gate_findings
                for diagnostic in finding.diagnostics
            )
            if diagnostics and all(item.remediable for item in diagnostics):
                eligible.append((len(diagnostics), candidate.candidate_id, candidate))
        return tuple(item[2] for item in sorted(eligible)[:2])

    def _refine_candidates(
        self,
        run: PlanningRunReceipt,
        parents: tuple[CandidateEnvelope, ...],
        *,
        candidates: list[CandidateEnvelope],
        receipts: list[ModelCallReceipt],
        partial_failures: list[str],
    ) -> list[CandidateEnvelope]:
        if self._refiner is None:
            return []
        refined: list[CandidateEnvelope] = []
        for parent in parents:
            try:
                result = self._refiner.refine(run, parent)
                self._record_receipts(run.run_id, result.receipts, receipts)
                child = result.value
                self._validate_refinement(
                    run,
                    parent,
                    child,
                    reserved_candidate_ids={
                        *(item.candidate_id for item in candidates),
                        *(item.candidate_id for item in refined),
                    },
                )
            except RequiredPlanningRoleUnavailable:
                raise
            except Exception as exc:
                self._capture_receipt(run.run_id, exc, receipts)
                partial_failures.append(
                    f"{parent.candidate_id} 정제 실패: {type(exc).__name__}"
                )
                continue

            structural_report = self._structural_validator.validate(
                run.planning_input.request_spec,
                child.plan,
            )
            if structural_report.readiness is not CandidateReadiness.READY_FOR_ASSIGNMENT:
                refined.append(self._structural_finding(child, structural_report))
                partial_failures.append(f"{child.candidate_id} 정제 후 구조 검사 미통과")
                continue

            try:
                reviewed_child = self._reviewer.review(run, child)
                self._record_receipts(
                    run.run_id,
                    reviewed_child.receipts,
                    receipts,
                )
                self._validate_review(child, reviewed_child.value)
                refined.append(reviewed_child.value)
            except CandidateReviewUnavailable as exc:
                self._capture_receipt(run.run_id, exc, receipts)
                refined.append(
                    self._review_unavailable_finding(
                        child,
                        message=str(exc),
                        receipt=exc.receipt,
                    )
                )
                partial_failures.append(
                    f"{child.candidate_id} 정제 후 Hard Gate 검토 불가: {exc}"
                )
            except RequiredPlanningRoleUnavailable:
                raise
            except Exception as exc:
                self._capture_receipt(run.run_id, exc, receipts)
                refined.append(
                    self._review_unavailable_finding(
                        child,
                        message=(
                            "정제 후보의 필수 semantic Hard Gate 결과를 안전하게 "
                            f"결속하지 못했습니다: {type(exc).__name__}"
                        ),
                        receipt=getattr(exc, "receipt", None),
                    )
                )
                partial_failures.append(
                    f"{child.candidate_id} 정제 후 Hard Gate 검토 실패: "
                    f"{type(exc).__name__}"
                )
        return refined

    def _initial_limit(self, run: PlanningRunReceipt) -> int:
        mission = run.planning_input.mission_selection.mission
        assert mission is not None
        if mission.primary is MissionPrimary.ANALYSIS_AUDIT:
            return 2 if self._allow_multiple_analysis_methods else 1
        if mission.primary is MissionPrimary.BUGFIX_STABILIZATION:
            has_root_cause_evidence = any(
                item.artifact_type in {"root_cause_evidence", "analysis_artifact"}
                for item in run.planning_input.input_artifact_refs
            )
            if not has_root_cause_evidence:
                return 1
        return 3

    @staticmethod
    def _deduplicate_approaches(
        approaches: tuple[ApproachBrief, ...],
    ) -> tuple[ApproachBrief, ...]:
        identifiers = [item.approach_id for item in approaches]
        if len(identifiers) != len(set(identifiers)):
            raise PlanningPipelineError("ApproachBrief ID가 중복됐습니다.")
        by_signature: dict[str, ApproachBrief] = {}
        for approach in sorted(approaches, key=lambda item: item.approach_id):
            by_signature.setdefault(approach.signature, approach)
        return tuple(by_signature.values())

    @staticmethod
    def _validate_initial_candidate(
        run: PlanningRunReceipt,
        approach: ApproachBrief,
        candidate: CandidateEnvelope,
    ) -> None:
        mission = run.planning_input.mission_selection.mission
        assert mission is not None
        if candidate.status is not CandidateStatus.GENERATED:
            raise PlanningPipelineError("초기 확장 후보는 generated 상태여야 합니다.")
        if candidate.version != 1 or candidate.parent_candidate_id is not None:
            raise PlanningPipelineError("초기 확장 후보의 lineage가 올바르지 않습니다.")
        if candidate.approach.signature != approach.signature:
            raise PlanningPipelineError("확장 후보가 요청한 ApproachBrief와 다릅니다.")
        if (
            candidate.planning_input_digest != run.planning_input_digest
            or candidate.mission_resolution_digest
            != run.planning_input.mission_selection.mission_resolution_digest
            or candidate.mission_primary is not mission.primary
        ):
            raise PlanningPipelineError("확장 후보가 frozen PlanningRun과 결속되지 않았습니다.")

    @staticmethod
    def _validate_review(before: CandidateEnvelope, after: CandidateEnvelope) -> None:
        if before.candidate_digest != after.candidate_digest:
            raise PlanningPipelineError("reviewer가 후보 정의를 변경했습니다.")
        if after.status is CandidateStatus.GENERATED:
            raise PlanningPipelineError("reviewer가 후보 판정을 제출하지 않았습니다.")

    @staticmethod
    def _structural_finding(
        candidate: CandidateEnvelope,
        report: PlanValidationReport,
    ) -> CandidateEnvelope:
        blocked = report.readiness is CandidateReadiness.NEEDS_USER_INPUT
        diagnostics = tuple(
            GateDiagnostic(
                finding_code=issue.code.value,
                severity=FindingSeverity.ERROR,
                message=issue.message,
                evidence_refs=(report.report_digest,),
                work_item_refs=issue.work_item_refs,
                remediable=not blocked,
            )
            for issue in report.issues
        )
        if blocked and not diagnostics:
            diagnostics = (
                GateDiagnostic(
                    finding_code="STRUCTURAL_USER_INPUT_REQUIRED",
                    severity=FindingSeverity.ERROR,
                    message="구조적으로 유효하지만 사용자 확인 전에는 후보를 진행할 수 없습니다.",
                    evidence_refs=(report.report_digest,),
                    remediable=False,
                ),
            )
        findings = tuple(
            GateFinding(
                gate=gate,
                plan_verdict=(
                    PlanVerdict.BLOCKED
                    if blocked and gate is GateName.PLAN
                    else PlanVerdict.FAIL
                    if not blocked and gate is GateName.PLAN
                    else PlanVerdict.PASS
                ),
                summary=(
                    "R3 deterministic 구조 검사에서 사용자 확인이 필요합니다."
                    if blocked and gate is GateName.PLAN
                    else "R3 deterministic 구조 검사에서 수정할 결함을 찾았습니다."
                    if not blocked and gate is GateName.PLAN
                    else "이 단계에서는 구조 계약만 검사했습니다."
                ),
                diagnostics=diagnostics if gate is GateName.PLAN else (),
            )
            for gate in GateName
        )
        quality = PlanQualityReport(
            planning_input_digest=candidate.planning_input_digest,
            plan_digest=candidate.plan.canonical_digest,
            plan_verdict=PlanVerdict.BLOCKED if blocked else PlanVerdict.FAIL,
            gate_findings=findings,
            confidence=ConfidenceLevel.HIGH,
        )
        return CandidateEnvelope.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "status": (
                    CandidateStatus.BLOCKED if blocked else CandidateStatus.NEEDS_REVISION
                ),
                "quality_report": quality,
            }
        )

    @staticmethod
    def _review_unavailable_finding(
        candidate: CandidateEnvelope,
        *,
        message: str,
        receipt: ModelCallReceipt | None,
    ) -> CandidateEnvelope:
        evidence_refs = (receipt.call_id,) if receipt is not None else ()
        findings = tuple(
            GateFinding(
                gate=gate,
                plan_verdict=PlanVerdict.BLOCKED,
                summary="필수 독립 semantic review 결과를 확보하지 못했습니다.",
                diagnostics=(
                    GateDiagnostic(
                        finding_code=f"{gate.value.upper()}_REVIEW_UNAVAILABLE",
                        severity=FindingSeverity.ERROR,
                        message=message,
                        evidence_refs=evidence_refs,
                        remediable=False,
                    ),
                ),
            )
            for gate in GateName
        )
        quality = PlanQualityReport(
            planning_input_digest=candidate.planning_input_digest,
            plan_digest=candidate.plan.canonical_digest,
            plan_verdict=PlanVerdict.BLOCKED,
            gate_findings=findings,
            confidence=ConfidenceLevel.LOW,
        )
        return CandidateEnvelope.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "status": CandidateStatus.BLOCKED,
                "quality_report": quality,
            }
        )

    @staticmethod
    def _validate_refinement(
        run: PlanningRunReceipt,
        parent: CandidateEnvelope,
        child: CandidateEnvelope,
        *,
        reserved_candidate_ids: set[str],
    ) -> None:
        if child.status is not CandidateStatus.GENERATED:
            raise PlanningPipelineError("정제 후보는 재검토 전 generated 상태여야 합니다.")
        if (
            child.parent_candidate_id != parent.candidate_id
            or child.version != parent.version + 1
            or child.refinement_round != 1
        ):
            raise PlanningPipelineError("정제 후보 lineage가 올바르지 않습니다.")
        if (
            child.approach.approach_id != parent.approach.approach_id
            or child.approach.signature != parent.approach.signature
        ):
            raise PlanningPipelineError(
                "정제 후보는 부모와 같은 구조화 approach 안에서만 수정할 수 있습니다."
            )
        if not set(parent.approach.risk_tags).issubset(child.approach.risk_tags):
            raise PlanningPipelineError(
                "정제 후보가 부모의 Gate 적용 risk tag를 삭제할 수 없습니다."
            )
        if child.planning_input_digest != run.planning_input_digest:
            raise PlanningPipelineError("정제 후보가 frozen PlanningRun과 다릅니다.")
        mission = run.planning_input.mission_selection.mission
        assert mission is not None
        if (
            child.mission_resolution_digest
            != run.planning_input.mission_selection.mission_resolution_digest
            or child.mission_primary is not mission.primary
        ):
            raise PlanningPipelineError("정제 후보가 frozen Mission과 다릅니다.")
        if (
            child.policy_id != R31_SCORE_POLICY_ID
            or child.policy_version != R31_SCORE_POLICY_VERSION
        ):
            raise PlanningPipelineError("정제 후보의 score policy가 올바르지 않습니다.")
        if child.candidate_id in reserved_candidate_ids:
            raise PlanningPipelineError("정제 후보 candidate_id가 이미 사용됐습니다.")

    @staticmethod
    def _validate_walkthrough(
        before: tuple[CandidateEnvelope, ...],
        after: tuple[CandidateEnvelope, ...],
    ) -> None:
        if {item.candidate_id for item in before} != {item.candidate_id for item in after}:
            raise PlanningPipelineError("Top-K walkthrough가 후보 집합을 변경했습니다.")
        before_digests = {item.candidate_id: item.candidate_digest for item in before}
        if any(
            before_digests[item.candidate_id] != item.candidate_digest
            for item in after
        ):
            raise PlanningPipelineError("Top-K walkthrough가 후보 정의를 변경했습니다.")
        if any(item.status is CandidateStatus.GENERATED for item in after):
            raise PlanningPipelineError("Top-K walkthrough가 Gate 판정을 제출하지 않았습니다.")

    @staticmethod
    def _decorate_outcome(
        outcome: PlanningSearchOutcome,
        *,
        receipts: tuple[ModelCallReceipt, ...],
        partial_failures: tuple[str, ...],
    ) -> PlanningSearchOutcome:
        hints = [
            SessionHint(
                role=PlanningRole.CANDIDATE_GENERATOR,
                strategy=SessionStrategy.REUSE,
                reusable_prefix_digest=outcome.planning_input_digest,
                independent_review_session=False,
                rationale="같은 Mission의 생성·정제는 frozen 공통 prefix를 재사용합니다.",
            )
        ]
        hints.extend(
            SessionHint(
                role=PlanningRole.HARD_GATE_REVIEWER,
                strategy=SessionStrategy.ISOLATE,
                candidate_id=candidate.candidate_id,
                independent_review_session=True,
                rationale="생성기의 숨은 문맥과 자기평가를 배제한 독립 검토입니다.",
            )
            for candidate in outcome.candidates
        )
        document = outcome.model_dump(mode="python")
        document["session_hints"] = tuple(hints)
        document["model_call_receipts"] = receipts
        document["failure_reasons"] = tuple(
            dict.fromkeys((*outcome.failure_reasons, *partial_failures))
        )
        return PlanningSearchOutcome.model_validate(document)

    def _finish(self, run: PlanningRunReceipt, outcome: PlanningSearchOutcome) -> PlanningSearchOutcome:
        self._artifacts.save_search_outcome(outcome)
        if outcome.status is SearchOutcomeStatus.READY_FOR_REVIEW:
            self._runs.transition(run.run_id, PlanningRunStatus.READY_FOR_REVIEW)
        elif outcome.status is SearchOutcomeStatus.BLOCKED:
            self._runs.transition(
                run.run_id,
                PlanningRunStatus.BLOCKED,
                reason="; ".join(outcome.failure_reasons),
            )
        else:
            self._runs.transition(
                run.run_id,
                PlanningRunStatus.FAILED,
                reason="; ".join(outcome.failure_reasons),
            )
        return outcome

    def _finish_failure(
        self,
        run: PlanningRunReceipt,
        *,
        candidates: tuple[CandidateEnvelope, ...],
        receipts: tuple[ModelCallReceipt, ...],
        reason: str,
        partial_failures: tuple[str, ...] = (),
    ) -> PlanningSearchOutcome:
        return self._finish_terminal(
            run,
            status=SearchOutcomeStatus.FAILED,
            candidates=candidates,
            receipts=receipts,
            reasons=(reason, *partial_failures),
        )

    def _finish_blocked(
        self,
        run: PlanningRunReceipt,
        *,
        candidates: tuple[CandidateEnvelope, ...],
        receipts: tuple[ModelCallReceipt, ...],
        reason: str,
    ) -> PlanningSearchOutcome:
        return self._finish_terminal(
            run,
            status=SearchOutcomeStatus.BLOCKED,
            candidates=candidates,
            receipts=receipts,
            reasons=(reason,),
        )

    def _finish_terminal(
        self,
        run: PlanningRunReceipt,
        *,
        status: SearchOutcomeStatus,
        candidates: tuple[CandidateEnvelope, ...],
        receipts: tuple[ModelCallReceipt, ...],
        reasons: tuple[str, ...],
    ) -> PlanningSearchOutcome:
        mission = run.planning_input.mission_selection.mission
        assert mission is not None
        unique_reasons = tuple(dict.fromkeys(reasons))
        receipt = SelectionReceipt(
            run_id=run.run_id,
            planning_input_digest=run.planning_input_digest,
            recommended_candidate_id=None,
            selected_candidate_id=None,
            selection_source=SelectionSource.NONE_NO_ADMISSIBLE,
            ranked_candidate_ids=(),
            alternative_candidate_ids=(),
            tie_break_reasons=(),
        )
        outcome = PlanningSearchOutcome(
            run_id=run.run_id,
            planning_input_digest=run.planning_input_digest,
            mission_primary=mission.primary,
            status=status,
            candidates=candidates,
            top_k_candidate_ids=(),
            selection_receipt=receipt,
            model_call_receipts=receipts,
            failure_reasons=unique_reasons,
        )
        return self._finish(run, outcome)


__all__ = [
    "ApproachGenerator",
    "CandidateReviewUnavailable",
    "CandidateExpander",
    "CandidateRefiner",
    "HardGateReviewer",
    "PlanningPipelineError",
    "PlanningSearchPipeline",
    "RequiredPlanningRoleUnavailable",
    "RoleOutput",
    "TopKWalkthroughReviewer",
]


# 권위 설계의 high-level port 이름. 결정적 ranker와 구분해 adapter orchestration,
# persistence, run 상태 전이를 포함한다.
PlanningSearchService = PlanningSearchPipeline
__all__.append("PlanningSearchService")
