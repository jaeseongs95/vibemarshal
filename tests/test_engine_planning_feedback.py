from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    CandidateStatus,
    FindingSeverity,
    GateName,
    PlanContractRevision,
    PlanningBudgetPolicy,
    ReviewFinding,
    ReviewRatings,
    ReviewerSubmission,
    RevisionStatus,
    new_id,
    utc_now,
)
from flowmarshal.engine.planning import (
    PlanningSearchOutcome,
    SkeletonFirstPlanner,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.planning_feedback import PlanRefinementProposal
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.service import EngineService, EngineServiceError

from tests.engine_helpers import goal, inventory, plan, profile, project_map, state, skeleton


def _clean_ratings() -> ReviewRatings:
    return ReviewRatings(
        goal_fit=4,
        grounding=4,
        engineering=4,
        verification=4,
        execution_safety=4,
    )


class Generator:
    def __init__(self, candidate, *, refine_candidate=None):
        self.candidate = candidate
        self.refine_candidate = refine_candidate
        self.refine_calls = 0

    def generate(self, *, candidate_count, **_):
        return (self.candidate,)[:candidate_count]

    def refine(self, *, candidate, **_):
        self.refine_calls += 1
        if self.refine_candidate is None:
            raise AssertionError("예상하지 않은 Skeleton refinement입니다.")
        return self.refine_candidate(candidate)


class CleanSkeletonReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, *, candidate, goal, state, project_map):
        self.calls += 1
        catalog = skeleton_review_evidence_catalog(candidate, goal, state, project_map)
        return ReviewerSubmission(
            reviewer_role="compact-skeleton-reviewer",
            candidate_digest=sha256_digest(candidate),
            ratings=_clean_ratings(),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class FindingThenCleanSkeletonReviewer(CleanSkeletonReviewer):
    def review(self, *, candidate, goal, state, project_map):
        self.calls += 1
        catalog = skeleton_review_evidence_catalog(candidate, goal, state, project_map)
        if self.calls == 1:
            return ReviewerSubmission(
                reviewer_role="compact-skeleton-reviewer",
                candidate_digest=sha256_digest(candidate),
                findings=(
                    ReviewFinding(
                        finding_code="SKELETON_DETAIL_GAP",
                        gate=GateName.PLAN,
                        severity=FindingSeverity.ERROR,
                        summary="Skeleton의 상세화 책임이 부족합니다.",
                        evidence_refs=("artifact:skeleton",),
                        remediable=True,
                    ),
                ),
                evidence_catalog_digest=sha256_digest(catalog),
            )
        return ReviewerSubmission(
            reviewer_role="compact-skeleton-reviewer",
            candidate_digest=sha256_digest(candidate),
            ratings=_clean_ratings(),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class FindingThenCleanPlanReviewer:
    def __init__(self, *, always_finding: bool = False, remediable: bool = True):
        self.calls = 0
        self.always_finding = always_finding
        self.remediable = remediable

    def review(self, *, plan, goal, state, project_map, **_):
        self.calls += 1
        catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        if self.always_finding or self.calls == 1:
            return ReviewerSubmission(
                reviewer_role="compact-plan-reviewer",
                candidate_digest=plan.activation_digest,
                findings=(
                    ReviewFinding(
                        finding_code="MISSING_VALIDATION_DETAIL",
                        gate=GateName.VERIFICATION,
                        severity=FindingSeverity.ERROR,
                        summary="검사 계약의 직접 확인 절차가 부족합니다.",
                        evidence_refs=("artifact:plan_contract", "source:goal"),
                        affected_task_refs=(plan.definition.tasks[0].task_ref,),
                        remediable=self.remediable,
                    ),
                ),
                evidence_catalog_digest=sha256_digest(catalog),
            )
        return ReviewerSubmission(
            reviewer_role="compact-plan-reviewer",
            candidate_digest=plan.activation_digest,
            ratings=_clean_ratings(),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class FeedbackExpander:
    def __init__(
        self,
        project_id,
        goal_revision,
        snapshot,
        map_digest,
        model_inventory,
        *,
        action="detail_revision",
        semantic_change=True,
    ):
        self.project_id = project_id
        self.goal_revision = goal_revision
        self.snapshot = snapshot
        self.map_digest = map_digest
        self.model_inventory = model_inventory
        self.action = action
        self.semantic_change = semantic_change
        self.expand_calls = []
        self.refine_calls = []

    def expand(self, *, candidate, planning_budget=None, previous_plan=None, **_):
        self.expand_calls.append((candidate, previous_plan))
        policy = planning_budget or PlanningBudgetPolicy()
        if previous_plan is None:
            initial = plan(
                self.project_id,
                self.goal_revision,
                self.snapshot,
                self.map_digest,
                candidate,
                self.model_inventory,
            )[0]
            definition = initial.definition.model_copy(update={"planning_budget": policy})
            return initial.model_copy(
                update={
                    "definition": definition,
                    "definition_digest": definition.definition_digest,
                }
            )
        return self._revision(
            previous_plan,
            candidate,
            policy,
            semantic_change=self.semantic_change,
        )

    def refine(
        self,
        *,
        evaluation,
        candidate,
        planning_budget,
        allow_skeleton_revision,
        **_,
    ):
        self.refine_calls.append(
            {
                "evaluation": evaluation,
                "candidate": candidate,
                "allow_skeleton_revision": allow_skeleton_revision,
            }
        )
        if self.action == "detail_revision":
            revised = self._revision(
                evaluation.plan,
                candidate,
                planning_budget,
                semantic_change=self.semantic_change,
            )
            return PlanRefinementProposal(
                action="detail_revision",
                rationale="원문에 맞게 상세 validation 계약을 수정했습니다.",
                evidence_refs=("artifact:plan_contract", "source:goal"),
                plan=revised,
            )
        if self.action == "skeleton_revision":
            if not allow_skeleton_revision:
                return PlanRefinementProposal(
                    action="unresolved",
                    rationale="Skeleton을 수정하고 재평가할 호출 예산이 부족합니다.",
                    evidence_refs=("artifact:plan_contract",),
                )
            revised_task = candidate.tasks[0].model_copy(
                update={
                    "detail_requirements": candidate.tasks[0].detail_requirements
                    + ("검사 책임을 상세 Plan에서 명시한다.",)
                }
            )
            revised = candidate.model_copy(
                update={
                    "candidate_id": new_id("candidate"),
                    "tasks": (revised_task,),
                    "parent_candidate_id": candidate.candidate_id,
                    "version": candidate.version + 1,
                    "refinement_round": 1,
                }
            )
            return PlanRefinementProposal(
                action="skeleton_revision",
                rationale="Task 책임을 바꾸려면 Skeleton revision이 필요합니다.",
                evidence_refs=("artifact:plan_contract", "artifact:skeleton"),
                skeleton=revised,
            )
        return PlanRefinementProposal(
            action=self.action,
            rationale=(
                "finding이 Goal 원문과 충돌합니다."
                if self.action == "disputed"
                else "제공된 근거만으로 수정 방향을 결정할 수 없습니다."
            ),
            evidence_refs=("artifact:plan_contract", "source:goal"),
        )

    @staticmethod
    def _revision(previous, candidate, policy, *, semantic_change):
        previous_task = previous.definition.tasks[0]
        task_validation = previous_task.validations[0].model_copy(
            update={
                "validation_id": "validation_task_revised",
                "statement": (
                    previous_task.validations[0].statement + " 등록 절차를 직접 실행한다."
                    if semantic_change
                    else previous_task.validations[0].statement
                ),
            }
        )
        new_task_id = new_id("task")
        revised_task = previous_task.model_copy(
            update={
                "task_id": new_task_id,
                "validations": (task_validation,),
            }
        )
        previous_goal_validation = previous.definition.integration_validations[0]
        goal_validation = previous_goal_validation.model_copy(
            update={"validation_id": "validation_goal_revised"}
        )
        coverage = previous.definition.goal_coverage[0].model_copy(
            update={
                "task_ids": (new_task_id,),
                "validation_ids": (
                    task_validation.validation_id,
                    goal_validation.validation_id,
                ),
            }
        )
        definition = previous.definition.model_copy(
            update={
                "source_skeleton_digest": sha256_digest(candidate),
                "tasks": (revised_task,),
                "goal_coverage": (coverage,),
                "integration_validations": (goal_validation,),
                "planning_budget": policy,
            }
        )
        return PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=previous.plan_id,
            revision_no=previous.revision_no + 1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=previous.plan_revision_id,
            created_at=utc_now(),
        )


class PlanningFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "AGENTS.md").write_text("지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "3" * 32
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.map = project_map(self.project_id, self.root)
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.inventory = inventory()
        self.candidate = skeleton(self.goal, self.state)

    def tearDown(self):
        self.temp.cleanup()

    def _planner(self, expander, *, generator=None, skeleton_reviewer=None, plan_reviewer=None):
        return SkeletonFirstPlanner(
            generator=generator or Generator(self.candidate),
            skeleton_reviewer=skeleton_reviewer or CleanSkeletonReviewer(),
            expander=expander,
            plan_reviewer=plan_reviewer or FindingThenCleanPlanReviewer(),
        )

    def _expander(self, **kwargs):
        return FeedbackExpander(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.inventory,
            **kwargs,
        )

    def test_remediable_detail_failure_selects_independently_reviewed_revision(self):
        expander = self._expander()
        reviewer = FindingThenCleanPlanReviewer()
        outcome = self._planner(expander, plan_reviewer=reviewer).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )

        self.assertEqual(6, outcome.logical_role_calls)
        self.assertEqual(2, outcome.candidate_versions)
        self.assertEqual(2, len(outcome.plan_evaluations))
        self.assertEqual(CandidateStatus.NEEDS_REVISION, outcome.plan_evaluations[0].decision.status)
        self.assertEqual(CandidateStatus.ADMISSIBLE, outcome.plan_evaluations[1].decision.status)
        original = outcome.plan_evaluations[0].plan
        revised = outcome.plan_evaluations[1].plan
        self.assertEqual(original.plan_id, revised.plan_id)
        self.assertEqual(original.revision_no + 1, revised.revision_no)
        self.assertEqual(original.plan_revision_id, revised.supersedes_plan_revision_id)
        self.assertNotEqual(original.definition.tasks[0].task_id, revised.definition.tasks[0].task_id)
        self.assertEqual(original.definition.tasks[0].task_ref, revised.definition.tasks[0].task_ref)
        self.assertEqual(revised.activation_digest, outcome.selected_activation_digest)
        self.assertEqual("evaluated", outcome.plan_refinements[0].result)
        self.assertEqual(2, reviewer.calls)

    def test_disputed_finding_preserves_rejection_without_re_review(self):
        expander = self._expander(action="disputed")
        reviewer = FindingThenCleanPlanReviewer(always_finding=True)
        outcome = self._planner(expander, plan_reviewer=reviewer).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )

        self.assertEqual(5, outcome.logical_role_calls)
        self.assertEqual(1, outcome.candidate_versions)
        self.assertEqual(1, len(outcome.plan_evaluations))
        self.assertEqual(CandidateStatus.NEEDS_REVISION, outcome.plan_evaluations[0].decision.status)
        self.assertIsNone(outcome.selected_activation_digest)
        self.assertEqual("disputed", outcome.plan_refinements[0].result)
        self.assertEqual(1, reviewer.calls)
        self.assertEqual(1, len(expander.refine_calls))

    def test_detail_refinement_is_not_started_without_two_remaining_calls(self):
        expander = self._expander()
        outcome = self._planner(
            expander,
            plan_reviewer=FindingThenCleanPlanReviewer(always_finding=True),
        ).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            budget=PlanningBudgetPolicy(max_logical_role_calls=5),
        )

        self.assertEqual(4, outcome.logical_role_calls)
        self.assertEqual((), outcome.plan_refinements)
        self.assertEqual([], expander.refine_calls)
        self.assertEqual("insufficient_call_budget", outcome.plan_refinement_stops[0].reason)
        self.assertIsNone(outcome.selected_activation_digest)

    def test_skeleton_refinement_and_detail_refinement_share_root_limit(self):
        def refine_candidate(candidate):
            task = candidate.tasks[0].model_copy(
                update={
                    "detail_requirements": candidate.tasks[0].detail_requirements
                    + ("상세 검사 책임을 보존한다.",)
                }
            )
            return candidate.model_copy(
                update={
                    "candidate_id": new_id("candidate"),
                    "tasks": (task,),
                    "parent_candidate_id": candidate.candidate_id,
                    "version": candidate.version + 1,
                    "refinement_round": 1,
                }
            )

        generator = Generator(self.candidate, refine_candidate=refine_candidate)
        expander = self._expander()
        outcome = self._planner(
            expander,
            generator=generator,
            skeleton_reviewer=FindingThenCleanSkeletonReviewer(),
            plan_reviewer=FindingThenCleanPlanReviewer(always_finding=True),
        ).search(goal=self.goal, state=self.state, project_map=self.map)

        self.assertEqual(1, generator.refine_calls)
        self.assertEqual([], expander.refine_calls)
        self.assertEqual(2, outcome.candidate_versions)
        self.assertEqual((), outcome.plan_refinements)
        self.assertEqual("refinement_limit", outcome.plan_refinement_stops[0].reason)
        self.assertIsNone(outcome.selected_activation_digest)

    def test_id_only_detail_revision_is_recorded_as_unchanged(self):
        expander = self._expander(semantic_change=False)
        outcome = self._planner(
            expander,
            plan_reviewer=FindingThenCleanPlanReviewer(always_finding=True),
        ).search(goal=self.goal, state=self.state, project_map=self.map)

        self.assertEqual(5, outcome.logical_role_calls)
        self.assertEqual(2, outcome.candidate_versions)
        self.assertEqual(1, len(outcome.plan_evaluations))
        self.assertEqual("unchanged_candidate", outcome.plan_refinements[0].result)
        self.assertIsNone(outcome.selected_activation_digest)

    def test_nonremediable_plan_failure_is_not_sent_to_refiner(self):
        expander = self._expander()
        reviewer = FindingThenCleanPlanReviewer(always_finding=True, remediable=False)
        outcome = self._planner(expander, plan_reviewer=reviewer).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )

        self.assertEqual(CandidateStatus.BLOCKED, outcome.plan_evaluations[0].decision.status)
        self.assertEqual([], expander.refine_calls)
        self.assertEqual((), outcome.plan_refinements)
        self.assertEqual((), outcome.plan_refinement_stops)
        self.assertIsNone(outcome.selected_activation_digest)

    def test_serialized_outcome_cannot_forge_version_count_or_select_rejected_plan(self):
        outcome = self._planner(self._expander()).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )

        payload = outcome.model_dump(mode="json")
        payload["candidate_versions"] -= 1
        with self.assertRaisesRegex(ValueError, "candidate_versions"):
            PlanningSearchOutcome.model_validate(payload)

        payload = outcome.model_dump(mode="json")
        payload["selected_activation_digest"] = outcome.plan_evaluations[0].plan.activation_digest
        with self.assertRaisesRegex(ValueError, "selected Plan"):
            PlanningSearchOutcome.model_validate(payload)

        payload = outcome.model_dump(mode="json")
        payload["plan_refinement_stops"] = [{
            "source_plan_digest": outcome.selected_activation_digest,
            "reason": "refiner_unavailable",
        }]
        with self.assertRaisesRegex(ValueError, "수정 가능한 원본 Plan"):
            PlanningSearchOutcome.model_validate(payload)

    def test_skeleton_revision_uses_full_path_and_supersedes_original_plan(self):
        expander = self._expander(action="skeleton_revision")
        reviewer = FindingThenCleanPlanReviewer()
        skeleton_reviewer = CleanSkeletonReviewer()
        outcome = self._planner(
            expander,
            skeleton_reviewer=skeleton_reviewer,
            plan_reviewer=reviewer,
        ).search(goal=self.goal, state=self.state, project_map=self.map)

        self.assertEqual(8, outcome.logical_role_calls)
        self.assertEqual(2, outcome.candidate_versions)
        self.assertEqual(2, len(outcome.skeleton_evaluations))
        self.assertEqual(2, len(outcome.plan_evaluations))
        self.assertTrue(expander.refine_calls[0]["allow_skeleton_revision"])
        original = outcome.plan_evaluations[0].plan
        revised = outcome.plan_evaluations[1].plan
        self.assertIs(original, expander.expand_calls[1][1])
        self.assertEqual(original.plan_id, revised.plan_id)
        self.assertEqual(original.plan_revision_id, revised.supersedes_plan_revision_id)
        self.assertEqual(revised.activation_digest, outcome.selected_activation_digest)
        self.assertEqual("evaluated", outcome.plan_refinements[0].result)
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(2, skeleton_reviewer.calls)

    def test_converged_skeleton_repair_is_deduplicated_against_full_shortlist_history(self):
        first = self.candidate
        second = self.candidate.model_copy(update={
            "candidate_id": new_id("candidate"),
            "approach": self.candidate.approach.model_copy(
                update={"change_shape": self.candidate.approach.change_shape + " 대안"}
            ),
        })

        class MultiGenerator:
            def generate(self, *, candidate_count, **_):
                return (first, second)[:candidate_count]

            def refine(self, **_):
                raise AssertionError("예상하지 않은 초기 Skeleton refinement입니다.")

        class ConvergingExpander(FeedbackExpander):
            def refine(self, **kwargs):
                proposal = super().refine(**kwargs)
                return proposal.model_copy(update={
                    "skeleton": proposal.skeleton.model_copy(
                        update={"approach": first.approach}
                    )
                })

            def expand(self, **kwargs):
                revised = super().expand(**kwargs)
                if kwargs.get("previous_plan") is not None:
                    return revised.model_copy(update={"status": RevisionStatus.DRAFT})
                return revised

        expander = ConvergingExpander(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.inventory,
            action="skeleton_revision",
        )
        outcome = self._planner(
            expander,
            generator=MultiGenerator(),
            skeleton_reviewer=CleanSkeletonReviewer(),
            plan_reviewer=FindingThenCleanPlanReviewer(always_finding=True),
        ).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=2,
        )

        self.assertEqual(12, outcome.logical_role_calls)
        self.assertEqual(4, outcome.candidate_versions)
        self.assertEqual(4, len(outcome.skeleton_evaluations))
        self.assertEqual(3, len(outcome.shortlist_digests))
        self.assertEqual(2, len(outcome.plan_refinements))
        self.assertEqual(3, len(expander.expand_calls))
        self.assertIsNone(outcome.selected_activation_digest)

    def test_ledger_keeps_failed_revision_inactive_and_activates_only_repaired_digest(self):
        ledger_root = self.root / "engine-state"
        ledger = SQLiteEngineLedger(
            ledger_root / "flowmarshal-engine.sqlite3",
            artifact_root=self.root / "engine-artifacts",
        )
        service = EngineService(ledger)
        service.initialize()
        project_id = service.create_project(name="계획 피드백", root=self.root)
        profile_revision = profile(project_id)
        service.register_profile(profile_revision)
        goal_revision = goal(project_id, profile_revision.definition_digest)
        service.register_goal(goal_revision)
        project_map_revision = project_map(project_id, self.root)
        service.record_project_map(project_map_revision)
        snapshot = state(
            project_id,
            goal_revision.definition_digest,
            project_map_revision.revision_digest,
        )
        service.record_state_snapshot(snapshot)
        model_inventory = inventory()
        candidate = skeleton(goal_revision, snapshot)
        expander = FeedbackExpander(
            project_id,
            goal_revision,
            snapshot,
            project_map_revision.revision_digest,
            model_inventory,
        )
        outcome = SkeletonFirstPlanner(
            generator=Generator(candidate),
            skeleton_reviewer=CleanSkeletonReviewer(),
            expander=expander,
            plan_reviewer=FindingThenCleanPlanReviewer(),
        ).search(
            goal=goal_revision,
            state=snapshot,
            project_map=project_map_revision,
        )

        for evaluation in outcome.skeleton_evaluations:
            service.record_skeleton_evaluation(evaluation)
        for evaluation in outcome.plan_evaluations:
            service.register_plan_evaluation(evaluation)

        forged_plan_evaluations = list(outcome.plan_evaluations)
        original_evaluation = forged_plan_evaluations[0]
        forged_submission = original_evaluation.semantic_submissions[0].model_copy(
            update={"reviewer_role": "forged-reviewer"}
        )
        forged_plan_evaluations[0] = original_evaluation.model_copy(
            update={"semantic_submissions": (forged_submission,)}
        )
        forged_outcome = PlanningSearchOutcome.model_validate(
            outcome.model_copy(
                update={"plan_evaluations": tuple(forged_plan_evaluations)}
            ).model_dump(mode="json")
        )
        with self.assertRaisesRegex(EngineServiceError, "원장 후보·검토·판정"):
            service.record_planning_search(forged_outcome)

        search_id = service.record_planning_search(outcome)
        self.assertEqual(search_id, service.record_planning_search(outcome))
        with ledger.read() as connection:
            events = connection.execute(
                "SELECT payload_json FROM history_events WHERE event_type = 'planning.search_recorded'",
            ).fetchall()
        self.assertEqual(1, len(events))
        recorded = json.loads(events[0]["payload_json"])
        self.assertEqual(sha256_digest(outcome), recorded["outcome_digest"])
        self.assertEqual(outcome.model_dump(mode="json"), recorded["outcome"])
        self.assertTrue(ledger.verify_history(project_id))

        failed = outcome.plan_evaluations[0].plan
        repaired = outcome.plan_evaluations[1].plan
        with self.assertRaises(EngineServiceError):
            service.activate_plan(
                plan_revision_id=failed.plan_revision_id,
                activation_digest=failed.activation_digest,
                source="test",
            )
        with self.assertRaises(EngineServiceError):
            service.activate_plan(
                plan_revision_id=repaired.plan_revision_id,
                activation_digest=failed.activation_digest,
                source="test",
            )
        service.activate_plan(
            plan_revision_id=repaired.plan_revision_id,
            activation_digest=repaired.activation_digest,
            source="test",
        )


if __name__ == "__main__":
    unittest.main()
