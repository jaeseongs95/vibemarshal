from __future__ import annotations

import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.authorization import authorization_changes
from flowmarshal.engine.domain import (
    EffectContract, GoalContractRevision, MutationPolicy, RevisionStatus, TaskKind,
    derive_candidate_decision, new_id, utc_now,
)
from flowmarshal.engine.planning import (
    CandidateEvaluation, ExpandedPlanEvaluation, plan_review_evidence_catalog,
    skeleton_gate, skeleton_review_evidence_catalog,
)
from tests import engine_helpers as fixtures
from tests.test_engine_ledger_service import EngineServiceFixture


class EngineEffectAuthorizationTests(EngineServiceFixture):
    def revised_goal(self, effects, **updates):
        definition = self.goal.definition.model_copy(update={"effect_policy": effects, **updates})
        return GoalContractRevision(
            goal_revision_id=new_id("goal_revision"), goal_id=self.goal.goal_id,
            revision_no=self.goal.revision_no + 1, definition=definition,
            definition_digest=definition.definition_digest, status=RevisionStatus.READY,
            supersedes_goal_revision_id=self.goal.goal_revision_id, created_at=utc_now(),
        )

    def changes(self, approved_effects, requested_effects, *, plan=None, **updates):
        authorization = self.service.authorize_goal(project_id=self.project_id, source="test")
        approved_definition = self.goal.definition.model_copy(update={"effect_policy": approved_effects})
        authorization = authorization.model_copy(update={
            "effect_policy": approved_effects, "goal_contract_digest": approved_definition.definition_digest,
        })
        with self.ledger.read() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id=?", (self.project_id,)).fetchone()
        return authorization_changes(
            authorization, project=project, goal=self.revised_goal(requested_effects, **updates),
            profile_digest=self.profile.definition_digest, plan=plan or self.plan, budget_policies=(),
        )

    def test_effect_restrictions_and_relaxations_are_paired(self):
        base = self.goal.definition.effect_policy
        pairs = (
            (base.model_copy(update={"allowed_external_effects": ("publish",)}), base),
            (base, base.model_copy(update={"prohibited_effects": (*base.prohibited_effects, "delete-archive")})),
            (base.model_copy(update={"irreversible_effects_require_checkpoint": False}),
             base.model_copy(update={"irreversible_effects_require_checkpoint": True})),
        )
        for broad, narrow in pairs:
            with self.subTest(broad=broad, narrow=narrow):
                self.assertEqual((), self.changes(broad, narrow))
                denied = self.changes(narrow, broad)
                self.assertTrue(any(item["boundary"] == "effect" for item in denied))
                self.assertTrue(all(item["evidence_ref"] == self.plan.activation_digest for item in denied))

    def test_read_only_restriction_and_mutation_expansion_are_paired(self):
        base = self.goal.definition.effect_policy
        read_only = base.model_copy(update={"mutation_policy": MutationPolicy.READ_ONLY})
        for mutation in MutationPolicy:
            if mutation is MutationPolicy.READ_ONLY:
                continue
            with self.subTest(mutation=mutation):
                broad = base.model_copy(update={"mutation_policy": mutation})
                self.assertEqual((), self.changes(broad, read_only))
                denied = self.changes(read_only, broad)
                self.assertTrue(any(item["boundary"] == "effect" for item in denied))
                self.assertTrue(all(item["evidence_ref"] == self.plan.activation_digest for item in denied))
        changed_goal = self.changes(base, read_only, observable_outcome="새로운 사용자 목표")
        self.assertTrue(any(item["boundary"] == "goal" for item in changed_goal))

    def test_effect_restriction_does_not_hide_goal_or_acceptance_change(self):
        base = self.goal.definition.effect_policy
        narrow = base.model_copy(update={"prohibited_effects": (*base.prohibited_effects, "delete-archive")})
        for updates in (
            {"observable_outcome": "다른 사용자 목표"},
            {"non_goals": (*self.goal.definition.non_goals, "새로운 제외 범위")},
            {"hard_acceptance": (self.goal.definition.hard_acceptance[0].model_copy(
                update={"statement": "완화한 합격 조건"}), *self.goal.definition.hard_acceptance[1:])},
        ):
            with self.subTest(updates=updates):
                self.assertTrue(any(item["boundary"] == "goal" for item in self.changes(base, narrow, **updates)))

    def test_plan_effect_must_respect_current_restricted_goal(self):
        base = self.goal.definition.effect_policy
        effect = EffectContract(effect_id="publish", external=True, statement="publish")
        task = self.task.model_copy(update={"expected_effects": (effect,)})
        plan = self.plan.model_copy(update={"definition": self.plan.definition.model_copy(update={"tasks": (task,)})})
        broad = base.model_copy(update={"allowed_external_effects": ("publish",)})
        denied = self.changes(broad, base, plan=plan)
        self.assertTrue(any(item["boundary"] == "effect" and item["field"].startswith(task.task_ref) for item in denied))

    def test_reviewed_restricted_goal_plan_activates_with_original_authorization(self):
        self.activate_restricted_goal({
            "prohibited_effects": (*self.goal.definition.effect_policy.prohibited_effects, "delete-archive"),
        })

    def test_reviewed_read_only_goal_plan_activates_with_original_authorization(self):
        self.activate_restricted_goal({"mutation_policy": MutationPolicy.READ_ONLY}, task_kind=TaskKind.VALIDATE)

    def test_read_only_restriction_keeps_mutating_task_gate(self):
        effects = self.goal.definition.effect_policy.model_copy(update={"mutation_policy": MutationPolicy.READ_ONLY})
        goal = self.revised_goal(effects)
        state = fixtures.state(self.project_id, goal.definition_digest, self.map.revision_digest)
        candidate = fixtures.skeleton(goal, state)
        findings = skeleton_gate(candidate, goal=goal, state=state, project_map=self.map)
        self.assertTrue(any(item.finding_code == "READ_ONLY_MUTATION" for item in findings))

    def activate_restricted_goal(self, effect_updates, *, task_kind=TaskKind.CHANGE):
        authorization = self.service.authorize_goal(project_id=self.project_id, source="test")
        effects = self.goal.definition.effect_policy.model_copy(update=effect_updates)
        self.goal = self.revised_goal(effects)
        self.service.register_goal(self.goal)
        self.state = fixtures.state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.service.record_state_snapshot(self.state)
        candidate = fixtures.skeleton(self.goal, self.state)
        candidate = candidate.model_copy(update={"tasks": tuple(
            task.model_copy(update={"kind": task_kind}) for task in candidate.tasks
        )})
        review = fixtures.clean_review(sha256_digest(candidate), role="skeleton_reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(candidate, self.goal, self.state, self.map))
        self.service.record_skeleton_evaluation(CandidateEvaluation(candidate=candidate,
            semantic_submission=review, decision=derive_candidate_decision(
                candidate_digest=sha256_digest(candidate), findings=(), ratings=review.ratings)))
        plan, task, decision = fixtures.plan(self.project_id, self.goal, self.state, self.map.revision_digest,
                                             candidate, self.inventory)
        task = task.model_copy(update={"kind": task_kind})
        definition = plan.definition.model_copy(update={"tasks": (task,)})
        plan = plan.model_copy(update={"definition": definition, "definition_digest": definition.definition_digest})
        review = fixtures.clean_review(plan.activation_digest, role="compact_plan_reviewer",
            evidence_catalog=plan_review_evidence_catalog(plan, self.goal, self.state, self.map))
        decision = derive_candidate_decision(candidate_digest=plan.activation_digest, findings=(), ratings=review.ratings)
        self.service.register_authorized_plan_revision(ExpandedPlanEvaluation(
            plan=plan, semantic_submissions=(review,), decision=decision))
        with self.ledger.read() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM goal_authorizations").fetchone()[0])
            row = connection.execute("SELECT authorization_id FROM plan_activations").fetchone()
        self.assertEqual(authorization.authorization_id, row[0])
        self.assertEqual((task.task_id,), self.service.list_ready_tasks(self.project_id))


if __name__ == "__main__":
    unittest.main()
