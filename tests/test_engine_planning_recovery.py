from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    CandidateStatus,
    FindingSeverity,
    GateName,
    PlanningBudgetPolicy,
    ReviewFinding,
    ReviewerSubmission,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from flowmarshal.engine.plan_review_adjudication import (
    PlanReviewAdjudication,
    PlanReviewFindingDisposition,
)
from flowmarshal.engine.planning import (
    PlanningSearchOutcome,
    SkeletonFirstPlanner,
    plan_review_evidence_catalog,
)
from flowmarshal.engine.planning_feedback import (
    PlanRefinementProposal,
    PlanRefinementProvenance,
)
from flowmarshal.engine.planning_recovery import PlanningRecoveryPolicy
from flowmarshal.engine.role_observations import RoleCallReceipt
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests.engine_helpers import goal, inventory, profile, project_map, state, skeleton
from tests.test_engine_planning_feedback import (
    CleanSkeletonReviewer,
    FeedbackExpander,
    FindingThenCleanPlanReviewer,
    FindingThenCleanSkeletonReviewer,
    Generator,
    _clean_ratings,
)


class SequencedFeedbackExpander(FeedbackExpander):
    def __init__(self, *args, actions, with_provenance=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.actions = list(actions)
        self.with_provenance = with_provenance

    def refine(self, *, evaluation, **kwargs):
        self.action = self.actions.pop(0)
        proposal = super().refine(evaluation=evaluation, **kwargs)
        if not self.with_provenance:
            return proposal
        request_digest = sha256_digest({"kind": "test-request", "index": len(self.refine_calls)})
        output_digest = sha256_digest(proposal)
        provenance = PlanRefinementProvenance(
            source_evaluation_digest=sha256_digest(evaluation),
            proposal_digest=sha256_digest(
                proposal.model_dump(mode="json", exclude={"provenance"})
            ),
            call_id=f"model_call_test_{len(self.refine_calls)}",
            request_digest=request_digest,
            output_digest=output_digest,
            receipt_digest=sha256_digest({"kind": "test-receipt", "index": len(self.refine_calls)}),
        )
        return PlanRefinementProposal(
            **proposal.model_dump(mode="python", exclude={"provenance"}),
            provenance=provenance,
        )


class AdjudicatingPlanReviewer(FindingThenCleanPlanReviewer):
    def __init__(self, *, additional_finding=False):
        super().__init__(always_finding=False)
        self.additional_finding = additional_finding
        self.adjudication_calls = 0

    def adjudicate(self, *, evaluation, proposal, goal, state, project_map, candidate=None):
        self.adjudication_calls += 1
        original = evaluation.semantic_submissions[0]
        catalog = plan_review_evidence_catalog(
            evaluation.plan, goal, state, project_map
        )
        original_finding = original.findings[0]
        additional = ()
        if self.additional_finding:
            additional = (
                ReviewFinding(
                    finding_code="ACTUAL_PLAN_SCOPE_DEFECT",
                    gate=GateName.VERIFICATION,
                    severity=FindingSeverity.ERROR,
                    summary="상세 Plan의 검사 범위를 수정해야 합니다.",
                    evidence_refs=("artifact:plan_contract", "source:goal"),
                    affected_task_refs=(evaluation.plan.definition.tasks[0].task_ref,),
                    remediable=True,
                ),
            )
        final_submission = ReviewerSubmission(
            reviewer_role=original.reviewer_role,
            candidate_digest=evaluation.plan.activation_digest,
            findings=additional,
            ratings=None if additional else _clean_ratings(),
            evidence_catalog_digest=sha256_digest(catalog),
        )
        request_digest = sha256_digest({
            "kind": "test-adjudication-request",
            "call": self.adjudication_calls,
            "source": sha256_digest(evaluation),
        })
        output_digest = sha256_digest({
            "kind": "test-adjudication-output",
            "call": self.adjudication_calls,
        })
        receipt = RoleCallReceipt(
            call_id=f"model_call_adjudication_{self.adjudication_calls}",
            role=original.reviewer_role,
            status="succeeded",
            model="reviewer",
            effort="high",
            inventory_digest="sha256:" + "0" * 64,
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id=f"thread_adjudication_{self.adjudication_calls}",
            turn_ids=(f"turn_adjudication_{self.adjudication_calls}",),
            input_digest=request_digest,
            output_digest=output_digest,
            output_schema_digest=sha256_digest({"schema": "adjudication"}),
            latency_ms=1,
            usage_available=True,
            recorded_at=utc_now(),
        )
        return PlanReviewAdjudication(
            source_evaluation_digest=sha256_digest(evaluation),
            dispute=proposal,
            original_submission=original,
            dispositions=(
                PlanReviewFindingDisposition(
                    original_finding_digest=sha256_digest(original_finding),
                    disposition="withdrawn",
                    rationale="AC 원문이 해당 필수 연결을 요구하지 않습니다.",
                    evidence_refs=("source:goal", "artifact:plan_contract"),
                ),
            ),
            submission=final_submission,
            request_digest=request_digest,
            output_digest=output_digest,
            receipt=receipt,
        )


class MultiGenerator:
    def __init__(self, candidates):
        self.candidates = tuple(candidates)

    def generate(self, *, candidate_count, **_):
        return self.candidates[:candidate_count]


class PerPlanRevisionReviewer(FindingThenCleanPlanReviewer):
    def review(self, *, plan, goal, state, project_map, **_):
        self.calls += 1
        catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        if plan.revision_no == 1:
            finding = ReviewFinding(
                finding_code=f"PLAN_REVISION_REQUIRED_{plan.plan_id[-8:].upper()}",
                gate=GateName.VERIFICATION,
                severity=FindingSeverity.ERROR,
                summary="초기 상세 Plan에 검사 계약 보완이 필요합니다.",
                evidence_refs=("artifact:plan_contract", "source:goal"),
                affected_task_refs=(plan.definition.tasks[0].task_ref,),
                remediable=True,
            )
            return ReviewerSubmission(
                reviewer_role="compact-plan-reviewer",
                candidate_digest=plan.activation_digest,
                findings=(finding,),
                evidence_catalog_digest=sha256_digest(catalog),
            )
        return ReviewerSubmission(
            reviewer_role="compact-plan-reviewer",
            candidate_digest=plan.activation_digest,
            ratings=_clean_ratings(),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class PlanningRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "AGENTS.md").write_text("지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "8" * 32
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.map = project_map(self.project_id, self.root)
        self.state = state(
            self.project_id, self.goal.definition_digest, self.map.revision_digest
        )
        self.inventory = inventory()
        self.candidate = skeleton(self.goal, self.state)
        self.recovery = PlanningRecoveryPolicy()

    def tearDown(self):
        self.temp.cleanup()

    def expander(self, **kwargs):
        return FeedbackExpander(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.inventory,
            **kwargs,
        )

    def planner(self, *, generator=None, skeleton_reviewer=None, expander=None, plan_reviewer=None):
        return SkeletonFirstPlanner(
            generator=generator or Generator(self.candidate),
            skeleton_reviewer=skeleton_reviewer or CleanSkeletonReviewer(),
            expander=expander or self.expander(),
            plan_reviewer=plan_reviewer or FindingThenCleanPlanReviewer(),
        )

    def persisted_fixture(self):
        ledger = SQLiteEngineLedger(self.root / "planning-recovery.sqlite3")
        service = EngineService(ledger)
        service.initialize()
        project_id = service.create_project(name="계획 복구", root=self.root)
        profile_revision = profile(project_id)
        service.register_profile(profile_revision)
        goal_revision = goal(project_id, profile_revision.definition_digest)
        service.register_goal(goal_revision)
        map_revision = project_map(project_id, self.root)
        service.record_project_map(map_revision)
        snapshot = state(
            project_id, goal_revision.definition_digest, map_revision.revision_digest
        )
        service.record_state_snapshot(snapshot)
        candidate = skeleton(goal_revision, snapshot)
        expander = FeedbackExpander(
            project_id,
            goal_revision,
            snapshot,
            map_revision.revision_digest,
            inventory(),
            action="disputed",
        )
        reviewer = AdjudicatingPlanReviewer()
        outcome = SkeletonFirstPlanner(
            generator=Generator(candidate),
            skeleton_reviewer=CleanSkeletonReviewer(),
            expander=expander,
            plan_reviewer=reviewer,
        ).search(
            goal=goal_revision,
            state=snapshot,
            project_map=map_revision,
            recovery_policy=PlanningRecoveryPolicy(),
        )
        return service, ledger, project_id, outcome

    def test_skeleton_and_detail_repairs_have_independent_single_slots(self):
        def refine_candidate(candidate):
            task = candidate.tasks[0].model_copy(update={
                "detail_requirements": candidate.tasks[0].detail_requirements
                + ("검사 책임을 상세 Plan에서 명시한다.",)
            })
            return candidate.model_copy(update={
                "candidate_id": new_id("candidate"),
                "tasks": (task,),
                "parent_candidate_id": candidate.candidate_id,
                "version": 2,
                "refinement_round": 1,
            })

        generator = Generator(self.candidate, refine_candidate=refine_candidate)
        expander = self.expander()
        reviewer = FindingThenCleanPlanReviewer()
        outcome = self.planner(
            generator=generator,
            skeleton_reviewer=FindingThenCleanSkeletonReviewer(),
            expander=expander,
            plan_reviewer=reviewer,
        ).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            recovery_policy=self.recovery,
        )

        self.assertEqual(1, generator.refine_calls)
        self.assertEqual(1, len(expander.refine_calls))
        self.assertEqual(8, outcome.logical_role_calls)
        self.assertEqual(3, outcome.candidate_versions)
        self.assertLessEqual(outcome.logical_role_calls, 14)
        self.assertLessEqual(outcome.candidate_versions, 5)
        self.assertEqual(CandidateStatus.ADMISSIBLE, outcome.plan_evaluations[-1].decision.status)
        self.assertEqual(outcome.plan_evaluations[-1].plan.activation_digest,
                         outcome.selected_activation_digest)

    def test_disputed_false_positive_is_withdrawn_without_new_plan_version(self):
        expander = self.expander(action="disputed")
        reviewer = AdjudicatingPlanReviewer()
        outcome = self.planner(expander=expander, plan_reviewer=reviewer).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            recovery_policy=self.recovery,
        )

        evaluation = outcome.plan_evaluations[0]
        self.assertEqual(6, outcome.logical_role_calls)
        self.assertEqual(1, outcome.candidate_versions)
        self.assertEqual(1, reviewer.calls)
        self.assertEqual(1, reviewer.adjudication_calls)
        self.assertEqual("disputed", outcome.plan_refinements[0].result)
        self.assertEqual(0, outcome.plan_refinements[0].source_review_round)
        self.assertTrue(evaluation.original_evaluation().semantic_submissions[0].findings)
        self.assertEqual((), evaluation.adjudication.submission.findings)
        self.assertEqual(evaluation.plan.activation_digest, outcome.selected_activation_digest)

    def test_adjudication_new_finding_uses_remaining_detail_slot_and_selects_revision(self):
        expander = SequencedFeedbackExpander(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.inventory,
            actions=("disputed", "detail_revision"),
            with_provenance=True,
        )
        reviewer = AdjudicatingPlanReviewer(additional_finding=True)
        outcome = self.planner(expander=expander, plan_reviewer=reviewer).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            recovery_policy=self.recovery,
        )

        self.assertEqual(8, outcome.logical_role_calls)
        self.assertEqual(2, outcome.candidate_versions)
        self.assertEqual((0, 1), tuple(
            attempt.source_review_round for attempt in outcome.plan_refinements
        ))
        self.assertEqual(("disputed", "evaluated"), tuple(
            attempt.result for attempt in outcome.plan_refinements
        ))
        self.assertEqual(
            sha256_digest(outcome.plan_evaluations[0]),
            outcome.plan_refinements[1].proposal.provenance.source_evaluation_digest,
        )
        self.assertEqual(1, reviewer.adjudication_calls)
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(CandidateStatus.ADMISSIBLE, outcome.plan_evaluations[-1].decision.status)
        self.assertEqual(outcome.plan_evaluations[-1].plan.activation_digest,
                         outcome.selected_activation_digest)

    def test_revised_plan_still_needs_revision_records_stop_without_more_calls(self):
        expander = self.expander()
        reviewer = FindingThenCleanPlanReviewer(always_finding=True)
        outcome = self.planner(expander=expander, plan_reviewer=reviewer).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            recovery_policy=self.recovery,
        )

        self.assertEqual(6, outcome.logical_role_calls)
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(1, len(expander.refine_calls))
        self.assertEqual(1, len(outcome.plan_refinement_stops))
        stop = outcome.plan_refinement_stops[0]
        self.assertEqual(outcome.plan_evaluations[-1].plan.activation_digest,
                         stop.source_plan_digest)
        self.assertEqual(0, stop.source_review_round)
        self.assertEqual("refinement_limit", stop.reason)
        self.assertIsNone(outcome.selected_activation_digest)

    def test_forged_adjudication_stop_call_and_version_counts_are_rejected(self):
        expander = self.expander(action="disputed")
        reviewer = AdjudicatingPlanReviewer()
        outcome = self.planner(expander=expander, plan_reviewer=reviewer).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            recovery_policy=self.recovery,
        )

        payload = outcome.model_dump(mode="json")
        payload["plan_evaluations"].append(payload["plan_evaluations"][0])
        payload["logical_role_calls"] += 1
        with self.assertRaisesRegex(ValueError, "재심 한도"):
            PlanningSearchOutcome.model_validate(payload)

        payload = outcome.model_dump(mode="json")
        payload["plan_evaluations"][0]["adjudication"]["source_evaluation_digest"] = (
            "sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(ValueError, "evaluation"):
            PlanningSearchOutcome.model_validate(payload)

        payload = outcome.model_dump(mode="json")
        payload["plan_refinement_stops"] = [{
            "source_plan_digest": outcome.plan_evaluations[0].plan.activation_digest,
            "source_review_round": 1,
            "reason": "refinement_limit",
        }]
        with self.assertRaisesRegex(ValueError, "수정 가능한 원본 Plan"):
            PlanningSearchOutcome.model_validate(payload)

        for field in ("logical_role_calls", "candidate_versions"):
            with self.subTest(field=field):
                payload = outcome.model_dump(mode="json")
                payload[field] += 1
                with self.assertRaisesRegex(ValueError, "call|candidate_versions"):
                    PlanningSearchOutcome.model_validate(payload)

    def test_three_skeletons_two_detail_revisions_stop_at_global_limits(self):
        candidates = []
        for index, (cost, context) in enumerate(((1, 300), (2, 200), (3, 100)), 1):
            approach = self.candidate.approach.model_copy(update={
                "strategy_family": f"direct-{index}"
            })
            candidates.append(self.candidate.model_copy(update={
                "candidate_id": new_id("candidate"),
                "approach": approach,
                "estimated_change_cost": cost,
                "estimated_context_tokens": context,
            }))
        expander = self.expander()
        reviewer = PerPlanRevisionReviewer()
        policy = PlanningBudgetPolicy(
            max_logical_role_calls=12,
            max_initial_candidates=3,
            max_candidate_versions=5,
            max_shortlist=2,
        )
        outcome = self.planner(
            generator=MultiGenerator(candidates),
            expander=expander,
            plan_reviewer=reviewer,
        ).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            budget=policy,
            candidate_count=3,
            recovery_policy=self.recovery,
        )

        self.assertEqual(12, outcome.logical_role_calls)
        self.assertEqual(5, outcome.candidate_versions)
        self.assertEqual(3, len(outcome.skeleton_evaluations))
        self.assertEqual(2, len(outcome.plan_refinements))
        self.assertEqual(4, reviewer.calls)
        self.assertEqual(2, len(expander.expand_calls))
        self.assertEqual(2, len(expander.refine_calls))
        self.assertTrue(outcome.budget_exhausted)

    def test_service_preserves_original_and_adjudicated_reviews_with_original_decision(self):
        service, ledger, project_id, outcome = self.persisted_fixture()
        for evaluation in outcome.skeleton_evaluations:
            service.record_skeleton_evaluation(evaluation)
        for evaluation in outcome.plan_evaluations:
            service.register_plan_evaluation(evaluation)
        search_id = service.record_planning_search(outcome)

        evaluation = outcome.plan_evaluations[0]
        self.assertIsNotNone(evaluation.adjudication)
        with ledger.read() as connection:
            reviews = connection.execute(
                "SELECT payload_json FROM candidate_reviews "
                "WHERE project_id = ? AND artifact_kind = 'plan' AND artifact_digest = ?",
                (project_id, evaluation.plan.activation_digest),
            ).fetchall()
            event = connection.execute(
                "SELECT payload_json FROM history_events "
                "WHERE project_id = ? AND event_type = 'plan.review_adjudicated' "
                "AND entity_id = ?",
                (project_id, evaluation.plan.plan_revision_id),
            ).fetchone()
            search = connection.execute(
                "SELECT payload_json FROM history_events "
                "WHERE project_id = ? AND event_type = 'planning.search_recorded' "
                "AND entity_id = ?",
                (project_id, search_id),
            ).fetchone()
        stored_review_digests = {
            sha256_digest(json.loads(row["payload_json"])) for row in reviews
        }
        self.assertEqual({
            sha256_digest(evaluation.semantic_submissions[0]),
            sha256_digest(evaluation.adjudication.submission),
        }, stored_review_digests)
        payload = json.loads(event["payload_json"])
        self.assertEqual(
            evaluation.original_evaluation().decision.model_dump(mode="json"),
            payload["original_decision"],
        )
        self.assertEqual(
            evaluation.adjudication.model_dump(mode="json"), payload["adjudication"]
        )
        self.assertEqual(evaluation.decision.model_dump(mode="json"), payload["decision"])
        self.assertEqual(
            outcome.model_dump(mode="json"), json.loads(search["payload_json"])["outcome"]
        )
        self.assertTrue(ledger.verify_history(project_id))

    def test_service_rejects_forged_adjudication_reference_and_decision(self):
        service, _ledger, _project_id, outcome = self.persisted_fixture()
        for evaluation in outcome.skeleton_evaluations:
            service.record_skeleton_evaluation(evaluation)
        evaluation = outcome.plan_evaluations[0]
        adjudication = evaluation.adjudication
        self.assertIsNotNone(adjudication)

        forged_source = adjudication.model_copy(
            update={"source_evaluation_digest": "sha256:" + "f" * 64}
        )
        forged_ref = adjudication.model_copy(update={
            "dispute": adjudication.dispute.model_copy(
                update={"evidence_refs": ("source:unknown",)}
            )
        })
        forged_decision = evaluation.decision.model_copy(
            update={"fitness_score": 1}
        )
        cases = (
            evaluation.model_copy(update={"adjudication": forged_source}),
            evaluation.model_copy(update={"adjudication": forged_ref}),
            evaluation.model_copy(update={"decision": forged_decision}),
        )
        for forged in cases:
            with self.subTest(forged=forged):
                with self.assertRaises(EngineServiceError):
                    service.register_plan_evaluation(forged)


if __name__ == "__main__":
    unittest.main()
