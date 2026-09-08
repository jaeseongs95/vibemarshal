from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    CandidateDecision,
    CandidateStatus,
    DependencyType,
    FindingSeverity,
    GateName,
    PlanningBudgetPolicy,
    ReviewFinding,
    ReviewRatings,
    ReviewerSubmission,
    SkeletonDependency,
    TaskKind,
    TaskSkeleton,
    new_id,
)
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    PlanningSearchOutcome,
    SkeletonFirstPlanner,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
    skeleton_gate,
)

from tests.engine_helpers import goal, inventory, plan, profile, project_map, state, skeleton


class Generator:
    def __init__(self, candidates):
        self.candidates = tuple(candidates)
        self.requested = None

    def generate(self, *, candidate_count, **_):
        self.requested = candidate_count
        return self.candidates[:candidate_count]

    def refine(self, **_):
        raise AssertionError("이 테스트에서는 refine을 호출하면 안 됩니다.")


class CleanSkeletonReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, *, candidate, goal, state, project_map):
        self.calls += 1
        catalog = skeleton_review_evidence_catalog(candidate, goal, state, project_map)
        return ReviewerSubmission(
            reviewer_role="compact-skeleton-reviewer",
            candidate_digest=sha256_digest(candidate),
            ratings=ReviewRatings(
                goal_fit=4,
                grounding=4,
                engineering=4,
                verification=4,
                execution_safety=4,
            ),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class FindingReviewer:
    def review(self, *, candidate, goal, state, project_map):
        catalog = skeleton_review_evidence_catalog(candidate, goal, state, project_map)
        finding = ReviewFinding(
            finding_code="DUPLICATE_STRATEGY",
            gate=GateName.PLAN,
            severity=FindingSeverity.ERROR,
            summary="전략이 중복됐습니다.",
            evidence_refs=("artifact:skeleton",),
            remediable=True,
        )
        return ReviewerSubmission(
            reviewer_role="compact-skeleton-reviewer",
            candidate_digest=sha256_digest(candidate),
            findings=(finding,),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class Expander:
    def __init__(self, project_id, goal_revision, snapshot, map_digest, model_inventory):
        self.project_id = project_id
        self.goal_revision = goal_revision
        self.snapshot = snapshot
        self.map_digest = map_digest
        self.model_inventory = model_inventory
        self.calls = 0

    def expand(self, *, candidate, **_):
        self.calls += 1
        return plan(
            self.project_id,
            self.goal_revision,
            self.snapshot,
            self.map_digest,
            candidate,
            self.model_inventory,
        )[0]


class CleanPlanReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, *, plan, goal, state, project_map, **_):
        self.calls += 1
        catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        return ReviewerSubmission(
            reviewer_role="compact-plan-reviewer",
            candidate_digest=plan.activation_digest,
            ratings=ReviewRatings(
                goal_fit=4,
                grounding=4,
                engineering=4,
                verification=4,
                execution_safety=4,
            ),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class FixedPlanExpander:
    """현재 입력과 무관하게 이전에 만든 Plan을 반환하는 stale expander 모사체."""

    def __init__(self, stale_plan):
        self.stale_plan = stale_plan
        self.calls = 0

    def expand(self, **_):
        self.calls += 1
        return self.stale_plan


class EnginePlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "AGENTS.md").write_text("지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "2" * 32
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.map = project_map(self.project_id, self.root)
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.inventory = inventory()
        self.candidate = skeleton(self.goal, self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _planner(self, generator, reviewer=None):
        expander = Expander(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.inventory,
        )
        return (
            SkeletonFirstPlanner(
                generator=generator,
                skeleton_reviewer=reviewer or CleanSkeletonReviewer(),
                expander=expander,
                plan_reviewer=CleanPlanReviewer(),
            ),
            expander,
        )

    def test_clear_request_uses_one_candidate_and_selects_feasible_plan(self) -> None:
        generator = Generator((self.candidate,))
        planner, _ = self._planner(generator)
        outcome = planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )
        self.assertEqual(1, generator.requested)
        self.assertIsNotNone(outcome.selected_activation_digest)
        self.assertEqual(4, outcome.logical_role_calls)
        self.assertEqual(CandidateStatus.ADMISSIBLE, outcome.plan_evaluations[0].decision.status)

    def test_project_prefix_does_not_authorize_a_made_up_input(self) -> None:
        task = self.candidate.tasks[0].model_copy(update={"consumes": ("project:invented-path",)})
        candidate = self.candidate.model_copy(update={"tasks": (task,)})
        findings = skeleton_gate(candidate, goal=self.goal, state=self.state, project_map=self.map)
        self.assertIn("INPUT_REFERENCE_INVALID", {finding.finding_code for finding in findings})

    def test_skeleton_connectivity_uses_consumer_handoff_products(self) -> None:
        producer = self.candidate.tasks[0].model_copy(update={
            "produces": ("result:one", "result:producer_only"),
        })
        consumer = TaskSkeleton(
            task_ref="task_two",
            kind=TaskKind.VALIDATE,
            objective="전달된 결과를 검증한다.",
            contributes_to=("ac_one",),
            produces=("result:checked",),
            consumes=("result:one", "result:producer_only"),
        )
        dependency = SkeletonDependency(
            producer_task_ref="task_one",
            consumer_task_ref="task_two",
            dependency_type=DependencyType.DATA,
            produces=("result:one", "result:producer_only"),
            consumes=("result:one",),
        )
        candidate = self.candidate.model_copy(update={
            "tasks": (producer, consumer),
            "dependencies": (dependency,),
            "goal_coverage": (
                self.candidate.goal_coverage[0].model_copy(
                    update={"task_refs": ("task_one", "task_two")}
                ),
            ),
        })

        findings = skeleton_gate(
            candidate, goal=self.goal, state=self.state, project_map=self.map,
        )

        disconnected = [
            finding for finding in findings
            if finding.finding_code == "DISCONNECTED_CONSUME"
        ]
        self.assertEqual(1, len(disconnected))
        self.assertEqual(("task_two",), disconnected[0].affected_task_refs)

    def test_duplicate_skeletons_are_deduplicated_before_expansion(self) -> None:
        duplicate = self.candidate.model_copy(update={"candidate_id": new_id("candidate")})
        generator = Generator((self.candidate, duplicate))
        planner, expander = self._planner(generator)
        outcome = planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=2,
        )
        self.assertEqual(2, len(outcome.skeleton_evaluations))
        self.assertEqual(1, expander.calls)
        self.assertEqual(1, len(outcome.shortlist_digests))

    def test_cost_only_does_not_prune_a_semantically_distinct_candidate(self) -> None:
        """같은 graph 모양이어도 unknown은 전략의 trade-off이므로 비용으로 제거할 수 없다."""
        alternative = self.candidate.model_copy(update={
            "candidate_id": new_id("candidate"),
            "unknowns": ("호환성 검증이 추가로 필요하다.",),
            "estimated_change_cost": 99,
            "estimated_context_tokens": 9_999,
        })
        planner, expander = self._planner(Generator((self.candidate, alternative)))
        outcome = planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=2,
        )
        self.assertEqual(2, expander.calls)
        self.assertEqual(2, len(outcome.shortlist_digests))

    def test_hard_gate_failure_is_not_scored_or_reviewed(self) -> None:
        invalid = self.candidate.model_copy(
            update={"goal_contract_digest": "sha256:" + "9" * 64}
        )
        reviewer = CleanSkeletonReviewer()
        planner, _ = self._planner(Generator((invalid,)), reviewer)
        outcome = planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            budget=PlanningBudgetPolicy(max_refinement_per_candidate=0),
        )
        evaluation = outcome.skeleton_evaluations[0]
        self.assertEqual(0, reviewer.calls)
        self.assertIn(evaluation.decision.status, {CandidateStatus.NEEDS_REVISION, CandidateStatus.BLOCKED})
        self.assertIsNone(evaluation.decision.fitness_score)
        self.assertEqual(0, len(outcome.plan_evaluations))

    def test_stale_expanded_plan_is_rejected_before_plan_reviewer(self) -> None:
        """같은 후보는 정상 선택되지만 재관측 뒤의 과거 Plan은 결정적으로 거부한다."""
        baseline_plan = plan(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.candidate,
            self.inventory,
        )[0]
        normal_reviewer = CleanPlanReviewer()
        normal_planner = SkeletonFirstPlanner(
            generator=Generator((self.candidate,)),
            skeleton_reviewer=CleanSkeletonReviewer(),
            expander=FixedPlanExpander(baseline_plan),
            plan_reviewer=normal_reviewer,
        )
        normal = normal_planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )
        self.assertIsNotNone(normal.selected_activation_digest)
        self.assertEqual(1, normal_reviewer.calls)

        (self.root / "app.py").write_text("value = 2\n", encoding="utf-8")
        current_map = project_map(self.project_id, self.root)
        current_state = state(
            self.project_id,
            self.goal.definition_digest,
            current_map.revision_digest,
        )
        current_candidate = skeleton(self.goal, current_state)
        # 현재 Skeleton에는 결속됐지만 관찰 전 State/Project Map을 가진 과거 binding을
        # expander가 반환한 경우다. source skeleton까지 과거 것으로 반환하면 Outcome의
        # shortlist 불변조건에서 먼저 차단되어 Plan gate를 관찰할 수 없다.
        stale_plan = plan(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            current_candidate,
            self.inventory,
        )[0]
        stale_expander = FixedPlanExpander(stale_plan)
        stale_reviewer = CleanPlanReviewer()
        stale_planner = SkeletonFirstPlanner(
            generator=Generator((current_candidate,)),
            skeleton_reviewer=CleanSkeletonReviewer(),
            expander=stale_expander,
            plan_reviewer=stale_reviewer,
        )
        stale = stale_planner.search(
            goal=self.goal,
            state=current_state,
            project_map=current_map,
        )

        self.assertNotEqual(self.map.revision_digest, current_map.revision_digest)
        self.assertEqual(1, stale_expander.calls)
        self.assertEqual(0, stale_reviewer.calls)
        self.assertIsNone(stale.selected_activation_digest)
        evaluation = stale.plan_evaluations[0]
        self.assertEqual((), evaluation.semantic_submissions)
        finding_codes = {item.finding_code for item in evaluation.deterministic_findings}
        self.assertTrue(
            {"PLAN_MAP_BINDING_MISMATCH", "PLAN_STATE_BINDING_MISMATCH"}.issubset(
                finding_codes
            )
        )

    def test_reviewer_finding_cannot_be_admitted(self) -> None:
        planner, _ = self._planner(Generator((self.candidate,)), FindingReviewer())
        outcome = planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            budget=PlanningBudgetPolicy(max_refinement_per_candidate=0),
        )
        decision = outcome.skeleton_evaluations[0].decision
        self.assertEqual(CandidateStatus.NEEDS_REVISION, decision.status)
        self.assertIsNone(decision.fitness_score)
        self.assertIsNone(outcome.selected_activation_digest)

    def test_serialized_evaluation_cannot_override_core_decision(self) -> None:
        finding = ReviewFinding(
            finding_code="FABRICATED_GATE_FINDING",
            gate=GateName.PLAN,
            severity=FindingSeverity.ERROR,
            summary="직접 finding이 존재합니다.",
            evidence_refs=(sha256_digest(self.candidate),),
            remediable=True,
        )
        with self.assertRaisesRegex(ValueError, "Core 결정 규칙"):
            CandidateEvaluation(
                candidate=self.candidate,
                deterministic_findings=(finding,),
                decision=CandidateDecision(
                    candidate_digest=sha256_digest(self.candidate),
                    status=CandidateStatus.ADMISSIBLE,
                    fitness_score=100,
                    weakest_dimension="engineering",
                ),
            )

    def test_search_outcome_recomputes_selected_plan_and_call_count(self) -> None:
        planner, _ = self._planner(Generator((self.candidate,)))
        outcome = planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )
        payload = outcome.model_dump(mode="json")
        payload["selected_activation_digest"] = None
        with self.assertRaisesRegex(ValueError, "selected Plan"):
            PlanningSearchOutcome.model_validate(payload)
        payload = outcome.model_dump(mode="json")
        payload["logical_role_calls"] += 1
        with self.assertRaisesRegex(ValueError, "logical role call"):
            PlanningSearchOutcome.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
