from __future__ import annotations

import unittest

from pydantic import ValidationError

from flowmarshal.canonical import sha256_digest
from flowmarshal.core.domain import (
    PlanDraft,
    ValidationDefinition,
    WorkItemDefinition,
)
from flowmarshal.planning.r31_domain import (
    R31_SCORE_POLICY_ID,
    R31_SCORE_POLICY_VERSION,
    ApproachBrief,
    CandidateEnvelope,
    CandidateStatus,
    ConfidenceLevel,
    CriterionValidationBinding,
    FailureRecoveryContract,
    FindingSeverity,
    GateDiagnostic,
    GateFinding,
    GateName,
    IntegrationValidationContract,
    MissionPrimary,
    PlanContractSidecar,
    PlanOutcomeContract,
    PlanQualityReport,
    PlanVerdict,
    ScoreDimension,
    ScoreDimensionEvidence,
    ScoreDimensionRatings,
    SearchOutcomeStatus,
    RuntimeStatus,
    SelectionSource,
    TieBreakEvidence,
    WorkItemQualityRating,
)
from flowmarshal.planning.r31_search import (
    BALANCED_MVP_WEIGHTS,
    DeterministicPlanningSearch,
    PlanningSearchError,
    SelectedPlanExporter,
    balanced_fitness_score,
    deduplicate_candidates,
    validate_scored_candidate,
)


PLANNING_DIGEST = sha256_digest({"planning": "fixture"})
MISSION_DIGEST = sha256_digest({"mission": "feature"})
PROJECT_ID = "project_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _plan(variant: str = "feature") -> PlanDraft:
    return PlanDraft(
        project_id=PROJECT_ID,
        request_summary="R3.1 결정적 검색 fixture",
        work_items=(
            WorkItemDefinition(
                client_ref="slice",
                title="검증 가능한 slice",
                objective="독립적으로 검증 가능한 기능 slice를 구현한다.",
                expected_changes=(f"src/{variant}.py",),
                deliverables=(f"src/{variant}.py",),
                acceptance_criteria=("fixture 검사가 정의된 결과를 확인한다.",),
                validations=(
                    ValidationDefinition(
                        criterion_id="slice.test",
                        check_type="command",
                        capability_id="python-tests",
                        specification={"command": "python -m unittest"},
                    ),
                ),
            ),
        ),
    )


def _contract(plan: PlanDraft) -> PlanContractSidecar:
    return PlanContractSidecar(
        plan_digest=plan.canonical_digest,
        plan_outcome=PlanOutcomeContract(
            observable_outcome="기능 slice가 독립 검사를 통과할 수 있다.",
            acceptance_criteria=("통합 fixture가 결과를 관찰한다.",),
        ),
        integration_validations=(
            IntegrationValidationContract(
                validation_id="integration.slice",
                work_item_refs=("slice",),
                check_type="command",
                specification={"command": "python -m unittest"},
                required_evidence=("exit_code",),
            ),
        ),
        criterion_bindings=(
            CriterionValidationBinding(
                work_item_ref="slice",
                criterion_id="slice.test",
                check_type="command",
                capability_id="python-tests",
                specification_digest=sha256_digest(
                    {"command": "python -m unittest"}
                ),
                required_evidence=("exit_code",),
            ),
        ),
        failure_recovery_contracts=(
            FailureRecoveryContract(
                work_item_ref="slice",
                failure_detection="검사 exit code와 artifact를 확인한다.",
                idempotency_strategy="같은 입력 digest에서는 같은 변경만 적용한다.",
                partial_execution_strategy="부분 변경을 검사한 뒤 동일 Attempt를 재개한다.",
                duplicate_dispatch_strategy="candidate와 Attempt binding으로 중복을 막는다.",
                retry_policy="동일 범위는 새 Attempt 한 번으로 재시도한다.",
                rollback_strategy="변경 전 snapshot으로 되돌릴 수 있다.",
            ),
        ),
    )


def _quality(
    plan: PlanDraft,
    *,
    ratings: ScoreDimensionRatings | None = None,
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    weakest: int = 2,
    mission_metrics: dict[str, int] | None = None,
    blocked: bool = False,
) -> PlanQualityReport:
    gate_findings: list[GateFinding] = []
    for gate in GateName:
        if blocked and gate is GateName.ENGINEERING:
            gate_findings.append(
                GateFinding(
                    gate=gate,
                    plan_verdict=PlanVerdict.BLOCKED,
                    summary="외부 보안 사실이 필요하다.",
                    diagnostics=(
                        GateDiagnostic(
                            finding_code="SECURITY_FACT_REQUIRED",
                            severity=FindingSeverity.ERROR,
                            message="사용자 또는 외부 권위의 보안 사실이 필요하다.",
                            remediable=False,
                        ),
                    ),
                )
            )
        else:
            gate_findings.append(
                GateFinding(
                    gate=gate,
                    plan_verdict=PlanVerdict.PASS,
                    summary=f"{gate.value} 계획 계약을 충족한다.",
                )
            )
    quality = WorkItemQualityRating(
        work_item_ref="slice",
        self_containment=weakest,
        functional_cohesion=2,
        acceptance_validation=2,
        interface_clarity=2,
        failure_retry=2,
    )
    if blocked:
        return PlanQualityReport(
            planning_input_digest=PLANNING_DIGEST,
            plan_digest=plan.canonical_digest,
            plan_verdict=PlanVerdict.BLOCKED,
            gate_findings=tuple(gate_findings),
            work_item_quality=(quality,),
            weakest_work_item_ref="slice",
            weakest_work_item_rating=weakest,
            confidence=confidence,
        )
    score_ratings = ratings or ScoreDimensionRatings(
        goal_fit_change_safety=4,
        verification_evidence_strength=4,
        execution_risk_control=4,
        maintainability_reproducibility=4,
        resource_efficiency=4,
    )
    return PlanQualityReport(
        planning_input_digest=PLANNING_DIGEST,
        plan_digest=plan.canonical_digest,
        plan_verdict=PlanVerdict.PASS,
        gate_findings=tuple(gate_findings),
        dimension_ratings=score_ratings,
        dimension_evidence=tuple(
            ScoreDimensionEvidence(
                dimension=dimension,
                rating=getattr(score_ratings, dimension.value),
                rationale=f"{dimension.value} fixture evidence",
                evidence_refs=(plan.canonical_digest,),
                sensitivity="fixture 계약 변경 시 재평가",
            )
            for dimension in ScoreDimension
        ),
        fitness_score=score_ratings.calculated_score,
        work_item_quality=(quality,),
        weakest_work_item_ref="slice",
        weakest_work_item_rating=weakest,
        confidence=confidence,
        tie_break_evidence=TieBreakEvidence(
            reversibility=3,
            public_contract_change=False,
            change_surface=2,
            cost=2,
            mission_metrics=mission_metrics or {},
        ),
    )


def _candidate(
    candidate_id: str,
    *,
    ratings: ScoreDimensionRatings | None = None,
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    weakest: int = 2,
    strategy: str = "adapter",
    mission: MissionPrimary = MissionPrimary.FEATURE_EXTENSION,
    mission_digest: str = MISSION_DIGEST,
    mission_metrics: dict[str, int] | None = None,
    status: CandidateStatus = CandidateStatus.ADMISSIBLE,
    blocked: bool = False,
    parent_candidate_id: str | None = None,
    version: int = 1,
    refinement_round: int = 0,
    plan_variant: str | None = None,
) -> CandidateEnvelope:
    plan = _plan(plan_variant or strategy)
    report = _quality(
        plan,
        ratings=ratings,
        confidence=confidence,
        weakest=weakest,
        mission_metrics=mission_metrics,
        blocked=blocked,
    )
    return CandidateEnvelope(
        candidate_id=candidate_id,
        parent_candidate_id=parent_candidate_id,
        version=version,
        refinement_round=refinement_round,
        status=status,
        planning_input_digest=PLANNING_DIGEST,
        mission_resolution_digest=mission_digest,
        mission_primary=mission,
        approach=ApproachBrief(
            approach_id=f"approach.{candidate_id}",
            mission_primary=mission,
            strategy_family=strategy,
            change_shape="additive boundary",
            compatibility="public contract 보존",
            rollout_recovery="adapter 제거로 rollback",
            rationale="요구사항을 작은 기능 slice로 전달한다.",
        ),
        plan=plan,
        contract=_contract(plan),
        quality_report=report,
        policy_id=R31_SCORE_POLICY_ID,
        policy_version=R31_SCORE_POLICY_VERSION,
    )


class PlannerR31SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.search = DeterministicPlanningSearch()

    def _search(
        self,
        candidates: tuple[CandidateEnvelope, ...],
        *,
        mission: MissionPrimary = MissionPrimary.FEATURE_EXTENSION,
        mission_digest: str = MISSION_DIGEST,
        selected: str | None = None,
    ):
        return self.search.search_candidates(
            run_id="run.search-fixture",
            planning_input_digest=PLANNING_DIGEST,
            mission_resolution_digest=mission_digest,
            mission_primary=mission,
            candidates=candidates,
            user_selected_candidate_id=selected,
        )

    def test_balanced_mvp_weights_and_integer_score_are_fixed(self) -> None:
        self.assertEqual(100, sum(BALANCED_MVP_WEIGHTS.values()))
        ratings = ScoreDimensionRatings(
            goal_fit_change_safety=3,
            verification_evidence_strength=4,
            execution_risk_control=2,
            maintainability_reproducibility=3,
            resource_efficiency=1,
        )
        # (75 + 100 + 40 + 60 + 10) / 4 = 71.25 -> 71
        self.assertEqual(71, balanced_fitness_score(ratings))
        self.assertEqual(ratings.calculated_score, balanced_fitness_score(ratings))

    def test_hard_gate_and_zero_work_item_axis_cannot_be_scored(self) -> None:
        plan = _plan()
        failed_findings = []
        for gate in GateName:
            if gate is GateName.INTENT:
                failed_findings.append(
                    GateFinding(
                        gate=gate,
                        plan_verdict=PlanVerdict.FAIL,
                        summary="명시 요구가 누락됐다.",
                        diagnostics=(
                            GateDiagnostic(
                                finding_code="INTENT_REQUIREMENT_MISSING",
                                severity=FindingSeverity.ERROR,
                                message="명시 요구를 계획이 추적하지 않는다.",
                                remediable=True,
                            ),
                        ),
                    )
                )
            else:
                failed_findings.append(
                    GateFinding(
                        gate=gate,
                        plan_verdict=PlanVerdict.PASS,
                        summary="계획 계약을 충족한다.",
                    )
                )
        ratings = ScoreDimensionRatings(
            goal_fit_change_safety=4,
            verification_evidence_strength=4,
            execution_risk_control=4,
            maintainability_reproducibility=4,
            resource_efficiency=4,
        )
        with self.assertRaises(ValidationError):
            PlanQualityReport(
                planning_input_digest=PLANNING_DIGEST,
                plan_digest=plan.canonical_digest,
                plan_verdict=PlanVerdict.FAIL,
                gate_findings=tuple(failed_findings),
                dimension_ratings=ratings,
                fitness_score=100,
                confidence=ConfidenceLevel.HIGH,
                tie_break_evidence=TieBreakEvidence(
                    reversibility=4,
                    public_contract_change=False,
                    change_surface=1,
                    cost=1,
                ),
            )

        valid_candidate = _candidate("candidate.valid")
        zero_axis_report = _quality(valid_candidate.plan, weakest=0)
        document = valid_candidate.model_dump(mode="python")
        document["quality_report"] = zero_axis_report
        with self.assertRaises(ValidationError):
            CandidateEnvelope.model_validate(document)

        premature = _candidate("candidate.premature-runtime")
        premature_report = premature.quality_report
        assert premature_report is not None
        findings = list(premature_report.gate_findings)
        findings[0] = findings[0].model_copy(update={"runtime_status": RuntimeStatus.PASS})
        changed_report = PlanQualityReport.model_validate(
            {**premature_report.model_dump(mode="python"), "gate_findings": findings}
        )
        changed_candidate = CandidateEnvelope.model_validate(
            {**premature.model_dump(mode="python"), "quality_report": changed_report}
        )
        with self.assertRaises(PlanningSearchError) as caught:
            validate_scored_candidate(changed_candidate)
        self.assertEqual("PREMATURE_RUNTIME_SUCCESS_CLAIM", caught.exception.code)

    def test_blocked_candidate_is_excluded_and_search_is_blocked(self) -> None:
        blocked = _candidate(
            "candidate.blocked",
            status=CandidateStatus.BLOCKED,
            blocked=True,
        )
        outcome = self._search((blocked,))
        self.assertEqual(SearchOutcomeStatus.BLOCKED, outcome.status)
        self.assertEqual((), outcome.top_k_candidate_ids)
        self.assertEqual(
            SelectionSource.NONE_NO_ADMISSIBLE,
            outcome.selection_receipt.selection_source,
        )
        self.assertIsNone(outcome.selection_receipt.selected_candidate_id)

    def test_near_scores_use_mission_tie_break_and_signature_dedupe(self) -> None:
        score_90 = ScoreDimensionRatings(
            goal_fit_change_safety=4,
            verification_evidence_strength=4,
            execution_risk_control=3,
            maintainability_reproducibility=4,
            resource_efficiency=2,
        )
        score_85 = ScoreDimensionRatings(
            goal_fit_change_safety=4,
            verification_evidence_strength=4,
            execution_risk_control=3,
            maintainability_reproducibility=3,
            resource_efficiency=2,
        )
        nominally_higher = _candidate(
            "candidate.nominally-higher",
            ratings=score_90,
            strategy="same-cluster",
            mission_metrics={"existing_behavior_compatibility": 1},
        )
        safer = _candidate(
            "candidate.safer",
            ratings=score_85,
            strategy="same-cluster",
            mission_metrics={"existing_behavior_compatibility": 4},
        )
        alternative = _candidate(
            "candidate.alternative",
            ratings=score_85,
            strategy="extension-point",
            mission_metrics={"existing_behavior_compatibility": 3},
        )
        outcome = self._search((nominally_higher, safer, alternative))

        self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
        self.assertEqual(
            ("candidate.safer", "candidate.alternative"),
            outcome.top_k_candidate_ids,
        )
        self.assertNotIn("candidate.nominally-higher", outcome.top_k_candidate_ids)
        self.assertEqual(
            "candidate.safer", outcome.selection_receipt.selected_candidate_id
        )
        self.assertEqual(
            SelectionSource.RECOMMENDED_DEFAULT,
            outcome.selection_receipt.selection_source,
        )

    def test_same_plan_and_contract_are_duplicates_even_if_strategy_names_differ(self) -> None:
        first = _candidate(
            "candidate.first-name",
            strategy="invented-name-one",
            plan_variant="same-artifact",
        )
        second = _candidate(
            "candidate.second-name",
            strategy="invented-name-two",
            plan_variant="same-artifact",
        )
        deduplicated = deduplicate_candidates((second, first))
        self.assertEqual(1, len(deduplicated))
        outcome = self._search((second, first))
        self.assertEqual(1, len(outcome.selection_receipt.pruned_duplicate_candidate_ids))

    def test_exact_ten_point_margin_beats_tie_break(self) -> None:
        perfect = _candidate(
            "candidate.perfect",
            strategy="adapter",
            mission_metrics={"existing_behavior_compatibility": 0},
        )
        score_90 = ScoreDimensionRatings(
            goal_fit_change_safety=4,
            verification_evidence_strength=4,
            execution_risk_control=3,
            maintainability_reproducibility=4,
            resource_efficiency=2,
        )
        mission_favorite = _candidate(
            "candidate.mission-favorite",
            ratings=score_90,
            strategy="extension-point",
            mission_metrics={"existing_behavior_compatibility": 4},
        )
        outcome = self._search((mission_favorite, perfect))
        self.assertEqual("candidate.perfect", outcome.top_k_candidate_ids[0])
        self.assertIn("10점", outcome.selection_receipt.tie_break_reasons[0])

    def test_cross_mission_and_candidate_budget_are_rejected(self) -> None:
        wrong_mission = _candidate(
            "candidate.audit",
            mission=MissionPrimary.ANALYSIS_AUDIT,
            mission_digest=sha256_digest({"mission": "audit"}),
        )
        with self.assertRaises(PlanningSearchError) as caught:
            self._search((wrong_mission,))
        self.assertEqual("CROSS_MISSION_RANKING_FORBIDDEN", caught.exception.code)

        too_many = tuple(
            _candidate(f"candidate.{index}", strategy=f"strategy-{index}")
            for index in range(4)
        )
        with self.assertRaises(PlanningSearchError) as caught:
            self._search(too_many)
        self.assertEqual("INITIAL_CANDIDATE_BUDGET_EXCEEDED", caught.exception.code)

    def test_low_confidence_waits_for_override_and_export_checks_digest(self) -> None:
        low = _candidate(
            "candidate.low-confidence",
            confidence=ConfidenceLevel.LOW,
            strategy="adapter",
        )
        alternative = _candidate(
            "candidate.alternative",
            confidence=ConfidenceLevel.MEDIUM,
            strategy="extension-point",
            mission_metrics={"existing_behavior_compatibility": 0},
        )
        # low 후보가 Mission tie-break에서 이기지만 자동 선택되지는 않는다.
        low_document = low.model_dump(mode="python")
        low_report = low.quality_report
        assert low_report is not None and low_report.tie_break_evidence is not None
        low_document["quality_report"] = PlanQualityReport.model_validate(
            {
                **low_report.model_dump(mode="python"),
                "tie_break_evidence": TieBreakEvidence(
                    reversibility=3,
                    public_contract_change=False,
                    change_surface=2,
                    cost=2,
                    mission_metrics={"existing_behavior_compatibility": 4},
                ),
            }
        )
        low = CandidateEnvelope.model_validate(low_document)
        outcome = self._search((low, alternative))
        self.assertEqual(
            SelectionSource.NONE_LOW_CONFIDENCE,
            outcome.selection_receipt.selection_source,
        )
        self.assertIsNone(outcome.selection_receipt.selected_candidate_id)
        with self.assertRaises(PlanningSearchError) as caught:
            SelectedPlanExporter().export(
                outcome, outcome.selection_receipt.selection_digest
            )
        self.assertEqual("SELECTION_REQUIRED", caught.exception.code)

        overridden = self.search.override_selection(
            outcome, "candidate.alternative"
        )
        self.assertEqual(
            SelectionSource.USER_OVERRIDE,
            overridden.selection_receipt.selection_source,
        )
        self.assertNotEqual(
            outcome.selection_receipt.selection_digest,
            overridden.selection_receipt.selection_digest,
        )
        self.assertEqual(
            outcome.selection_receipt.selection_digest,
            overridden.selection_receipt.supersedes_selection_digest,
        )
        with self.assertRaises(PlanningSearchError) as caught:
            SelectedPlanExporter().export(overridden, sha256_digest("stale"))
        self.assertEqual(
            "SELECTION_RECEIPT_DIGEST_MISMATCH", caught.exception.code
        )
        exported = SelectedPlanExporter().export(
            overridden, overridden.selection_receipt.selection_digest
        )
        selected_plan = next(
            candidate.plan
            for candidate in overridden.candidates
            if candidate.candidate_id == "candidate.alternative"
        )
        self.assertEqual(selected_plan.canonical_digest, exported.plan_draft.canonical_digest)
        self.assertEqual(
            overridden.selection_receipt.selection_digest,
            exported.selection_receipt_digest,
        )


if __name__ == "__main__":
    unittest.main()
