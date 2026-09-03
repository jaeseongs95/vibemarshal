from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes
from flowmarshal.planning.r31_domain import (
    CandidateEnvelope,
    CandidateStatus,
    ConfidenceLevel,
    FindingSeverity,
    GateDiagnostic,
    GateFinding,
    GateName,
    MissionPrimary,
    ModelCallReceipt,
    ModelCallStatus,
    PlanningRole,
    PlanQualityReport,
    PlanVerdict,
    PlanningRunStatus,
    RiskTag,
    SearchOutcomeStatus,
    SelectionSource,
)
from flowmarshal.planning.r31_pipeline import (
    CandidateReviewUnavailable,
    PlanningPipelineError,
    PlanningSearchPipeline,
    RequiredPlanningRoleUnavailable,
    RoleOutput,
)
from flowmarshal.planning.r31_prototype import (
    FixtureApproachGenerator,
    FixtureCandidateExpander,
    FixtureCandidateRefiner,
    FixtureHardGateReviewer,
    FixtureTopKWalkthrough,
)
from flowmarshal.planning.r31_search import SelectedPlanExporter
from flowmarshal.planning.r31_store import PlanningArtifactRepository, PlanningRunService
from tests.test_planner_r31_domain import NOW, planning_input


class PlannerR31PipelineTests(unittest.TestCase):
    def test_model_receipt_is_persisted_before_later_pipeline_failure(self) -> None:
        receipt = ModelCallReceipt(
            call_id="call_durable_before_failure",
            role=PlanningRole.CANDIDATE_GENERATOR,
            model_id="fixture-model",
            reasoning_effort="medium",
            inventory_digest=sha256_bytes(b"inventory"),
            input_digest=sha256_bytes(b"input"),
            output_schema_digest=sha256_bytes(b"schema"),
            output_digest=sha256_bytes(b"empty-approaches"),
            status=ModelCallStatus.SUCCEEDED,
            thread_id="thread-generator",
            turn_ids=("turn-generator",),
        )

        class EmptyGenerator:
            def generate(self, planning_run, *, limit):
                return RoleOutput((), (receipt,))

        class RecordingRepository(PlanningArtifactRepository):
            def __init__(self, artifact_root):
                super().__init__(artifact_root)
                self.events = []

            def save_model_call_receipt(self, run_id, model_receipt):
                self.events.append(("receipt", model_receipt.call_id))
                return super().save_model_call_receipt(run_id, model_receipt)

            def save_search_outcome(self, outcome):
                self.events.append(("outcome", outcome.outcome_digest))
                return super().save_search_outcome(outcome)

        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_receipt_durability",
            )
            frozen = runs.freeze(planning_input(), "pipeline-receipt-durability")
            artifacts = RecordingRepository(directory)
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=artifacts,
                approach_generator=EmptyGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=FixtureHardGateReviewer(),
            ).search(frozen)
            persisted = list(Path(directory).rglob("model-calls/*.json"))

        self.assertEqual(SearchOutcomeStatus.FAILED, outcome.status)
        self.assertEqual(1, len(persisted))
        self.assertEqual("receipt", artifacts.events[0][0])
        self.assertEqual("outcome", artifacts.events[1][0])

    def test_limited_search_refines_top_two_persists_and_exports_one_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_pipeline",
            )
            artifacts = PlanningArtifactRepository(directory)
            frozen = runs.freeze(planning_input(), "pipeline-happy-path")
            pipeline = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=artifacts,
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=FixtureHardGateReviewer(),
                candidate_refiner=FixtureCandidateRefiner(),
                top_k_walkthrough=FixtureTopKWalkthrough(),
            )

            outcome = pipeline.search(frozen)

            self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
            self.assertEqual(5, len(outcome.candidates))
            self.assertEqual(2, len(outcome.top_k_candidate_ids))
            self.assertEqual(
                SelectionSource.RECOMMENDED_DEFAULT,
                outcome.selection_receipt.selection_source,
            )
            self.assertEqual(
                PlanningRunStatus.READY_FOR_REVIEW,
                runs.get(frozen.run_id).status,
            )
            loaded = artifacts.load_search_outcome(frozen.run_id)
            self.assertEqual(outcome.outcome_digest, loaded.outcome_digest)
            exported = SelectedPlanExporter().export(
                outcome,
                outcome.selection_receipt.selection_digest,
            )
            self.assertEqual(
                outcome.selection_receipt.selected_candidate_id,
                next(
                    item.candidate_id
                    for item in outcome.candidates
                    if item.plan.canonical_digest == exported.plan_draft.canonical_digest
                    and item.candidate_id
                    == outcome.selection_receipt.selected_candidate_id
                ),
            )

    def test_remediable_initial_failures_use_one_refinement_round(self) -> None:
        class RemediableThenPassingReviewer(FixtureHardGateReviewer):
            def review(self, planning_run, candidate):
                if candidate.version > 1:
                    return super().review(planning_run, candidate)
                report = PlanQualityReport(
                    planning_input_digest=candidate.planning_input_digest,
                    plan_digest=candidate.plan.canonical_digest,
                    plan_verdict=PlanVerdict.FAIL,
                    gate_findings=tuple(
                        GateFinding(
                            gate=gate,
                            plan_verdict=(
                                PlanVerdict.FAIL
                                if gate is GateName.PLAN
                                else PlanVerdict.PASS
                            ),
                            summary="초기 후보의 수정 가능한 fixture 결함입니다.",
                            diagnostics=(
                                GateDiagnostic(
                                    finding_code="P01_FIXTURE_REMEDIABLE",
                                    severity=FindingSeverity.ERROR,
                                    message="한 번의 정제로 수정할 수 있습니다.",
                                    remediable=True,
                                ),
                            )
                            if gate is GateName.PLAN
                            else (),
                        )
                        for gate in GateName
                    ),
                    confidence=ConfidenceLevel.HIGH,
                )
                return RoleOutput(
                    CandidateEnvelope.model_validate(
                        {
                            **candidate.model_dump(mode="python"),
                            "status": CandidateStatus.NEEDS_REVISION,
                            "quality_report": report,
                        }
                    )
                )

        class RecordingRefiner(FixtureCandidateRefiner):
            def __init__(self):
                self.parents = []

            def refine(self, planning_run, candidate):
                self.parents.append(candidate.candidate_id)
                return super().refine(planning_run, candidate)

        refiner = RecordingRefiner()
        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_remediation",
            )
            frozen = runs.freeze(planning_input(), "pipeline-remediation")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=RemediableThenPassingReviewer(),
                candidate_refiner=refiner,
                top_k_walkthrough=FixtureTopKWalkthrough(),
            ).search(frozen)

        self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
        self.assertEqual(2, len(refiner.parents))
        self.assertEqual(5, len(outcome.candidates))
        self.assertTrue(
            any(item.version == 2 and item.status is CandidateStatus.SELECTED for item in outcome.candidates)
        )

    def test_one_candidate_failure_does_not_abort_other_branches(self) -> None:
        class PartiallyFailingExpander(FixtureCandidateExpander):
            def expand(self, planning_run, approach):
                if approach.strategy_family == "minimal-change":
                    raise ValueError("fixture branch failure")
                return super().expand(planning_run, approach)

        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_partial",
            )
            frozen = runs.freeze(planning_input(), "pipeline-partial")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=PartiallyFailingExpander(),
                hard_gate_reviewer=FixtureHardGateReviewer(),
            ).search(frozen)
            self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
            self.assertEqual(2, len(outcome.top_k_candidate_ids))
            self.assertTrue(any("후보 확장 실패" in item for item in outcome.failure_reasons))

    def test_one_candidate_review_failure_is_blocked_without_aborting_others(self) -> None:
        class PartiallyUnavailableReviewer(FixtureHardGateReviewer):
            def review(self, planning_run, candidate):
                if candidate.approach.strategy_family == "minimal-change":
                    raise CandidateReviewUnavailable("fixture reviewer timeout")
                return super().review(planning_run, candidate)

        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_partial_review",
            )
            frozen = runs.freeze(planning_input(), "pipeline-partial-review")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=PartiallyUnavailableReviewer(),
            ).search(frozen)
        self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
        blocked = [
            item for item in outcome.candidates if item.status is CandidateStatus.BLOCKED
        ]
        self.assertEqual(1, len(blocked))
        self.assertIsNone(blocked[0].quality_report.fitness_score)
        self.assertTrue(any("검토 불가" in item for item in outcome.failure_reasons))

    def test_refined_child_repeats_structural_validation_and_keeps_parent_fallback(self) -> None:
        class StructurallyInvalidRefiner(FixtureCandidateRefiner):
            def refine(self, planning_run, candidate):
                child = super().refine(planning_run, candidate).value
                plan = child.plan.model_copy(
                    update={"request_spec_digest": sha256_bytes(b"invalid-refined-plan")}
                )
                contract = child.contract.model_copy(
                    update={"plan_digest": plan.canonical_digest}
                )
                return RoleOutput(
                    CandidateEnvelope.model_validate(
                        {
                            **child.model_dump(mode="python"),
                            "plan": plan,
                            "contract": contract,
                        }
                    )
                )

        class CountingReviewer(FixtureHardGateReviewer):
            child_calls = 0

            def review(self, planning_run, candidate):
                if candidate.parent_candidate_id is not None:
                    self.child_calls += 1
                return super().review(planning_run, candidate)

        reviewer = CountingReviewer()
        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_refined_structural_failure",
            )
            frozen = runs.freeze(planning_input(), "pipeline-refined-structural-failure")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=reviewer,
                candidate_refiner=StructurallyInvalidRefiner(),
                top_k_walkthrough=FixtureTopKWalkthrough(),
            ).search(frozen)

        self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
        self.assertEqual(0, reviewer.child_calls)
        self.assertEqual(2, len(outcome.top_k_candidate_ids))
        self.assertTrue(
            all(
                next(item for item in outcome.candidates if item.candidate_id == candidate_id)
                .parent_candidate_id
                is None
                for candidate_id in outcome.top_k_candidate_ids
            )
        )
        invalid_children = [
            item for item in outcome.candidates if item.parent_candidate_id is not None
        ]
        self.assertEqual(2, len(invalid_children))
        self.assertTrue(
            all(item.status is CandidateStatus.NEEDS_REVISION for item in invalid_children)
        )

    def test_blocked_refined_child_does_not_remove_admissible_parent(self) -> None:
        class ChildUnavailableReviewer(FixtureHardGateReviewer):
            def review(self, planning_run, candidate):
                if candidate.parent_candidate_id is not None:
                    raise CandidateReviewUnavailable("fixture refined review timeout")
                return super().review(planning_run, candidate)

        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_refined_review_failure",
            )
            frozen = runs.freeze(planning_input(), "pipeline-refined-review-failure")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=ChildUnavailableReviewer(),
                candidate_refiner=FixtureCandidateRefiner(),
                top_k_walkthrough=FixtureTopKWalkthrough(),
            ).search(frozen)

        self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
        self.assertEqual(2, len(outcome.top_k_candidate_ids))
        self.assertTrue(
            all(
                next(item for item in outcome.candidates if item.candidate_id == candidate_id)
                .parent_candidate_id
                is None
                for candidate_id in outcome.top_k_candidate_ids
            )
        )
        blocked_children = [
            item
            for item in outcome.candidates
            if item.parent_candidate_id is not None
            and item.status is CandidateStatus.BLOCKED
        ]
        self.assertEqual(2, len(blocked_children))

    def test_cross_mission_refined_child_is_isolated_without_failing_run(self) -> None:
        class CrossMissionRefiner(FixtureCandidateRefiner):
            def refine(self, planning_run, candidate):
                child = super().refine(planning_run, candidate).value
                return RoleOutput(
                    child.model_copy(
                        update={
                            "mission_resolution_digest": sha256_bytes(
                                b"different-mission"
                            )
                        }
                    )
                )

        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_cross_mission_child",
            )
            frozen = runs.freeze(planning_input(), "pipeline-cross-mission-child")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=FixtureHardGateReviewer(),
                candidate_refiner=CrossMissionRefiner(),
                top_k_walkthrough=FixtureTopKWalkthrough(),
            ).search(frozen)

        self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
        self.assertEqual(2, len(outcome.top_k_candidate_ids))
        self.assertTrue(any("정제 실패" in item for item in outcome.failure_reasons))
        self.assertTrue(
            all(
                next(item for item in outcome.candidates if item.candidate_id == candidate_id)
                .parent_candidate_id
                is None
                for candidate_id in outcome.top_k_candidate_ids
            )
        )

    def test_refinement_cannot_branch_hop_or_remove_parent_risk_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_refinement_contract",
            )
            frozen = runs.freeze(planning_input(), "pipeline-refinement-contract")
            base_approach = FixtureApproachGenerator().generate(
                frozen,
                limit=1,
            ).value[0]
            risky_approach = base_approach.model_copy(
                update={"risk_tags": (RiskTag.SECURITY_SENSITIVE,)}
            )
            parent = FixtureCandidateExpander().expand(
                frozen,
                risky_approach,
            ).value
            child = FixtureCandidateRefiner().refine(frozen, parent).value
            invalid_children = {
                "approach signature": child.model_copy(
                    update={
                        "approach": child.approach.model_copy(
                            update={"change_shape": "부모와 다른 변경 전략"}
                        )
                    }
                ),
                "risk downgrade": child.model_copy(
                    update={
                        "approach": child.approach.model_copy(update={"risk_tags": ()})
                    }
                ),
            }

            for label, invalid_child in invalid_children.items():
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        PlanningPipelineError,
                        "approach|risk tag",
                    ):
                        PlanningSearchPipeline._validate_refinement(
                            frozen,
                            parent,
                            invalid_child,
                            reserved_candidate_ids=set(),
                        )

    def test_required_reviewer_unavailable_blocks_without_fallback(self) -> None:
        class UnavailableReviewer:
            def review(self, planning_run, candidate):
                raise RequiredPlanningRoleUnavailable("필수 강한 reviewer를 사용할 수 없습니다.")

        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_blocked",
            )
            frozen = runs.freeze(planning_input(), "pipeline-blocked")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=UnavailableReviewer(),
            ).search(frozen)
            self.assertEqual(SearchOutcomeStatus.BLOCKED, outcome.status)
            self.assertEqual(PlanningRunStatus.BLOCKED, runs.get(frozen.run_id).status)
            self.assertFalse(outcome.top_k_candidate_ids)

    def test_structural_failure_never_reaches_semantic_reviewer_or_scoring(self) -> None:
        class InvalidExpander(FixtureCandidateExpander):
            def expand(self, planning_run, approach):
                candidate = super().expand(planning_run, approach).value
                plan = candidate.plan.model_copy(
                    update={"request_spec_digest": sha256_bytes(b"wrong-request")}
                )
                contract = candidate.contract.model_copy(
                    update={"plan_digest": plan.canonical_digest}
                )
                invalid = CandidateEnvelope.model_validate(
                    {
                        **candidate.model_dump(mode="python"),
                        "plan": plan,
                        "contract": contract,
                    }
                )
                return RoleOutput(invalid)

        class CountingReviewer(FixtureHardGateReviewer):
            calls = 0

            def review(self, planning_run, candidate):
                self.calls += 1
                return super().review(planning_run, candidate)

        reviewer = CountingReviewer()
        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_structural_failure",
            )
            frozen = runs.freeze(planning_input(), "pipeline-structural-failure")
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=FixtureApproachGenerator(),
                candidate_expander=InvalidExpander(),
                hard_gate_reviewer=reviewer,
            ).search(frozen)
        self.assertEqual(SearchOutcomeStatus.NO_ADMISSIBLE, outcome.status)
        self.assertEqual(0, reviewer.calls)
        self.assertTrue(
            all(item.status is CandidateStatus.NEEDS_REVISION for item in outcome.candidates)
        )
        self.assertTrue(all(item.quality_report.fitness_score is None for item in outcome.candidates))

    def test_analysis_defaults_to_one_candidate(self) -> None:
        class RecordingGenerator(FixtureApproachGenerator):
            limit_seen = None

            def generate(self, planning_run, *, limit):
                self.limit_seen = limit
                return super().generate(planning_run, limit=limit)

        generator = RecordingGenerator()
        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_analysis",
            )
            frozen = runs.freeze(
                planning_input(primary=MissionPrimary.ANALYSIS_AUDIT),
                "pipeline-analysis",
            )
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=generator,
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=FixtureHardGateReviewer(),
            ).search(frozen)
            self.assertEqual(1, generator.limit_seen)
            self.assertEqual(SearchOutcomeStatus.READY_FOR_REVIEW, outcome.status)
            self.assertEqual(1, len(outcome.candidates))

    def test_bugfix_without_root_cause_artifact_generates_one_diagnostic_plan(self) -> None:
        class RecordingGenerator(FixtureApproachGenerator):
            limit_seen = None

            def generate(self, planning_run, *, limit):
                self.limit_seen = limit
                return super().generate(planning_run, limit=limit)

        generator = RecordingGenerator()
        with tempfile.TemporaryDirectory() as directory:
            runs = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_bugfix_unknown_cause",
            )
            frozen = runs.freeze(
                planning_input(primary=MissionPrimary.BUGFIX_STABILIZATION),
                "pipeline-bugfix-unknown-cause",
            )
            outcome = PlanningSearchPipeline(
                run_service=runs,
                artifact_repository=PlanningArtifactRepository(directory),
                approach_generator=generator,
                candidate_expander=FixtureCandidateExpander(),
                hard_gate_reviewer=FixtureHardGateReviewer(),
            ).search(frozen)
        self.assertEqual(1, generator.limit_seen)
        self.assertEqual("causal-diagnosis", outcome.candidates[0].approach.strategy_family)


if __name__ == "__main__":
    unittest.main()
