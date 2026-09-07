from __future__ import annotations

import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    AttemptKind,
    CandidateDecision,
    CandidateStatus,
    FindingSeverity,
    GateName,
    GoalContractRevision,
    GoalOperatingPolicy,
    GoalCoverage,
    PlanContractDefinition,
    PlanContractRevision,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    ReviewFinding,
    ReviewerSubmission,
    RevisionStatus,
    RuntimeIntentKind,
    TaskContract,
    TaskSkeleton,
    ThreadBinding,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.service import EngineServiceError, GoalAuthorizationRequired

from tests.engine_helpers import clean_review
from tests.test_engine_ledger_service import EngineServiceFixture


class EngineGoalAuthorizationTests(EngineServiceFixture):
    """Goal 단위 인가와 내부 Plan 활성화 경계를 검증한다."""

    def authorize(self):
        return self.service.authorize_goal(project_id=self.project_id, source="user")

    def internal_revision_evaluation(
        self,
        *,
        definition_updates: dict[str, object] | None = None,
        review_findings: tuple[ReviewFinding, ...] = (),
        project_map=None,
    ) -> tuple[PlanContractRevision, TaskContract, ExpandedPlanEvaluation]:
        """같은 Goal·Skeleton 안에서 Task 식별자만 새 revision으로 만든다."""

        next_task = self.task.model_copy(update={"task_id": new_id("task")})
        updates: dict[str, object] = {
            "base_state_snapshot_digest": self.state.snapshot_digest,
            "project_map_digest": self.map.revision_digest,
            "tasks": (next_task,),
            "goal_coverage": tuple(
                coverage.model_copy(update={"task_ids": (next_task.task_id,)})
                for coverage in self.plan.definition.goal_coverage
            ),
        }
        updates.update(definition_updates or {})
        definition = self.plan.definition.model_copy(update=updates)
        with self.ledger.read() as connection:
            latest = connection.execute(
                "SELECT id, revision_no FROM plan_revisions WHERE plan_id = ? "
                "ORDER BY revision_no DESC LIMIT 1",
                (self.plan.plan_id,),
            ).fetchone()
        assert latest is not None
        revision = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=self.plan.plan_id,
            revision_no=latest["revision_no"] + 1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=latest["id"],
            created_at=utc_now(),
        )
        catalog = plan_review_evidence_catalog(
            revision, self.goal, self.state, project_map or self.map
        )
        review = (
            ReviewerSubmission(
                reviewer_role="compact_plan_reviewer",
                candidate_digest=revision.activation_digest,
                findings=review_findings,
                evidence_catalog_digest=sha256_digest(catalog),
            )
            if review_findings
            else clean_review(
                revision.activation_digest,
                role="compact_plan_reviewer",
                evidence_catalog=catalog,
            )
        )
        decision = derive_candidate_decision(
            candidate_digest=revision.activation_digest,
            findings=review.findings,
            ratings=review.ratings if not review.findings else None,
        )
        evaluation = ExpandedPlanEvaluation(
            plan=revision,
            semantic_submissions=(review,),
            decision=decision,
        )
        return revision, next_task, evaluation

    def register_internal_revision(
        self,
        *,
        definition_updates: dict[str, object] | None = None,
        review_findings: tuple[ReviewFinding, ...] = (),
        project_map=None,
    ) -> tuple[PlanContractRevision, TaskContract]:
        revision, task, evaluation = self.internal_revision_evaluation(
            definition_updates=definition_updates,
            review_findings=review_findings,
            project_map=project_map,
        )
        self.service.register_plan_evaluation(evaluation)
        return revision, task

    def replacement_state(self) -> dict[str, object]:
        with self.ledger.read() as connection:
            project = connection.execute(
                "SELECT active_plan_revision_id, run_state FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()
            plans = connection.execute(
                "SELECT id, status FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
                (self.project_id,),
            ).fetchall()
            tasks = connection.execute(
                "SELECT id, plan_revision_id, status FROM task_contracts "
                "WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT id, plan_revision_id, task_id, status, binding_json FROM attempts "
                "WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
            activations = connection.execute(
                "SELECT plan_revision_id, activation_digest, authorization_id FROM plan_activations "
                "WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
            intents = connection.execute(
                "SELECT i.id, i.attempt_id, i.status, i.request_digest FROM runtime_intents i "
                "JOIN attempts a ON a.id = i.attempt_id WHERE a.project_id = ? ORDER BY i.rowid",
                (self.project_id,),
            ).fetchall()
            receipts = connection.execute(
                "SELECT r.id, r.intent_id, r.provider_operation_id, r.response_digest, r.binding_json "
                "FROM runtime_receipts r JOIN runtime_intents i ON i.id = r.intent_id "
                "JOIN attempts a ON a.id = i.attempt_id WHERE a.project_id = ? ORDER BY r.rowid",
                (self.project_id,),
            ).fetchall()
        return {
            "project": tuple(project),
            "plans": tuple(tuple(row) for row in plans),
            "tasks": tuple(tuple(row) for row in tasks),
            "attempts": tuple(tuple(row) for row in attempts),
            "activations": tuple(tuple(row) for row in activations),
            "intents": tuple(tuple(row) for row in intents),
            "receipts": tuple(tuple(row) for row in receipts),
        }

    def test_authorize_goal_binds_active_boundary_without_plan_identity(self) -> None:
        authorization = self.authorize()

        self.assertEqual(self.project_id, authorization.project_id)
        self.assertEqual(str(self.root.resolve()), authorization.project_root)
        self.assertEqual(self.goal.goal_id, authorization.goal_id)
        self.assertEqual(self.goal.goal_revision_id, authorization.goal_revision_id)
        self.assertEqual(self.goal.definition_digest, authorization.goal_contract_digest)
        self.assertEqual(self.profile.definition_digest, authorization.profile_definition_digest)
        self.assertEqual(self.goal.definition.effect_policy, authorization.effect_policy)
        self.assertEqual(2, authorization.operating_policy.max_same_failure_replans)
        self.assertEqual(5, authorization.operating_policy.max_goal_replans)
        self.assertTrue(authorization.operating_policy.requires_new_evidence)
        self.assertEqual("existing_binding_first", authorization.operating_policy.resume_strategy)
        self.assertTrue(all(isinstance(item, str) for item in authorization.budget_policies))
        self.assertFalse(
            {"plan_id", "plan_revision_id", "activation_digest"}
            & set(authorization.model_dump(mode="json"))
        )

    def test_activation_requires_goal_authorization_on_auto_and_legacy_routes(self) -> None:
        before = self.replacement_state()

        with self.assertRaises(GoalAuthorizationRequired) as automatic:
            self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.assertTrue(automatic.exception.changes)
        self.assertTrue(all(isinstance(change, dict) for change in automatic.exception.changes))

        with self.assertRaises(GoalAuthorizationRequired) as legacy:
            self.service.activate_plan(
                plan_revision_id=self.plan.plan_revision_id,
                activation_digest=self.plan.activation_digest,
                source="strict",
            )
        self.assertTrue(legacy.exception.changes)
        self.assertEqual(before, self.replacement_state())

    def test_authorized_goal_allows_internal_plan_revision_without_reauthorization(self) -> None:
        authorization = self.authorize()
        first_activation_id = self.service.activate_authorized_plan(
            plan_revision_id=self.plan.plan_revision_id
        )
        revision, next_task, evaluation = self.internal_revision_evaluation()

        activation_id = self.service.register_authorized_plan_revision(evaluation)

        self.assertNotEqual(first_activation_id, activation_id)
        with self.ledger.read() as connection:
            authorization_id = connection.execute(
                "SELECT id FROM goal_authorizations WHERE project_id = ? "
                "ORDER BY revision_no DESC LIMIT 1",
                (self.project_id,),
            ).fetchone()[0]
        self.assertEqual(authorization.authorization_id, authorization_id)
        self.assertEqual(revision.plan_revision_id, self.replacement_state()["project"][0])
        self.assertEqual((next_task.task_id,), self.service.list_ready_tasks(self.project_id))

    def test_goal_revision_change_requires_a_new_authorization_with_evidence(self) -> None:
        self.authorize()
        definition = self.goal.definition.model_copy(
            update={"observable_outcome": "변경된 사용자의 목표를 검증한다."}
        )
        revised_goal = GoalContractRevision(
            goal_revision_id=new_id("goal_revision"),
            goal_id=self.goal.goal_id,
            revision_no=2,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_goal_revision_id=self.goal.goal_revision_id,
            created_at=utc_now(),
        )
        self.service.register_goal(revised_goal)

        with self.assertRaises(GoalAuthorizationRequired) as captured:
            self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)

        self.assertTrue(any(
            change["boundary"] == "goal" and change["field"] == "goal_contract_digest"
            for change in captured.exception.changes
        ))

    def test_effect_policy_expansion_requires_explicit_new_goal_authorization(self) -> None:
        self.authorize()
        definition = self.goal.definition.model_copy(update={"effect_policy": self.goal.definition.effect_policy.model_copy(
            update={"allowed_external_effects": ("외부 시스템 배포",)})})
        revised = GoalContractRevision(goal_revision_id=new_id("goal_revision"), goal_id=self.goal.goal_id,
            revision_no=2, definition=definition, definition_digest=definition.definition_digest,
            status=RevisionStatus.READY, supersedes_goal_revision_id=self.goal.goal_revision_id, created_at=utc_now())
        self.service.register_goal(revised)
        with self.assertRaises(GoalAuthorizationRequired) as captured:
            self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.assertTrue(any(change["boundary"] == "effect" for change in captured.exception.changes))
        authorization = self.authorize()
        self.assertEqual(("외부 시스템 배포",), authorization.effect_policy.allowed_external_effects)
        # 새 Goal 승인은 그 Goal과 결속되지 않은 옛 Plan을 소급 승인하지 않는다.
        with self.assertRaisesRegex(EngineServiceError, "active GoalContract"):
            self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)

    def test_project_map_root_change_requires_a_new_authorization_with_evidence(self) -> None:
        self.authorize()
        relocated_root = self.root / "relocated"
        relocated_root.mkdir()
        relocated_map = self.map.model_copy(update={
            "project_map_revision_id": new_id("project_map_revision"),
            "revision_no": 2,
            "root": str(relocated_root.resolve()),
            "created_at": utc_now(),
        })
        self.service.record_project_map(relocated_map)
        revision, _ = self.register_internal_revision(
            definition_updates={"project_map_digest": relocated_map.revision_digest},
            project_map=relocated_map,
        )

        with self.assertRaises(GoalAuthorizationRequired) as captured:
            self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)

        self.assertTrue(any(
            change["boundary"] == "project" and change["field"] == "project_map.root"
            for change in captured.exception.changes
        ))

    def test_planning_budget_expansion_requires_authorization_but_reduction_does_not(self) -> None:
        self.authorize()
        expanded = self.plan.definition.planning_budget.model_copy(
            update={"max_logical_role_calls": 15}
        )
        expanded_revision, _ = self.register_internal_revision(
            definition_updates={"planning_budget": expanded}
        )
        with self.assertRaises(GoalAuthorizationRequired) as captured:
            self.service.activate_authorized_plan(plan_revision_id=expanded_revision.plan_revision_id)
        self.assertTrue(any(
            change["boundary"] == "policy"
            and change["field"] == "planning_budget.max_logical_role_calls"
            for change in captured.exception.changes
        ))

        self.authorize()
        reduced = self.plan.definition.planning_budget.model_copy(
            update={"max_logical_role_calls": 13}
        )
        reduced_revision, _ = self.register_internal_revision(
            definition_updates={"planning_budget": reduced}
        )
        self.service.activate_authorized_plan(plan_revision_id=reduced_revision.plan_revision_id)
        self.assertEqual(reduced_revision.plan_revision_id, self.replacement_state()["project"][0])

    def test_rejected_scope_review_cannot_be_activated_after_authorization(self) -> None:
        self.authorize()
        revision, _ = self.register_internal_revision(review_findings=(
            ReviewFinding(
                finding_code="SCOPE_BOUNDARY_UNPROVEN",
                gate=GateName.GOAL,
                severity=FindingSeverity.WARNING,
                summary="제출된 근거만으로 Task 범위가 승인 경계를 지킨다고 판단할 수 없다.",
                evidence_refs=("artifact:plan_contract",),
                remediable=False,
            ),
        ))

        with self.assertRaisesRegex(EngineServiceError, "admissible이며 ready"):
            self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)

    def test_execution_spec_revision_uses_existing_goal_authorization(self) -> None:
        self.authorize()
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        initial = self.spec()
        self.service.materialize_execution_spec(initial, inventory=self.inventory)
        revised = self.spec(
            revision_no=2,
            supersedes=initial.execution_spec_revision_id,
        )
        self.service.materialize_execution_spec(revised, inventory=self.inventory)

        with self.ledger.read() as connection:
            authorizations = connection.execute(
                "SELECT COUNT(*) FROM goal_authorizations WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()[0]
            current_spec = connection.execute(
                "SELECT id FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                (self.task.task_id,),
            ).fetchone()[0]
        self.assertEqual(1, authorizations)
        self.assertEqual(revised.execution_spec_revision_id, current_spec)

    def test_authorized_task_split_with_review_activates_without_new_authorization(self) -> None:
        authorization = self.authorize()
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        task_a = self.task.model_copy(update={
            "task_id": new_id("task"),
            "task_ref": "task_a",
            "produces": ("result:a",),
            "validations": (
                self.task.validations[0].model_copy(update={"validation_id": "validation_a"}),
            ),
        })
        task_b = self.task.model_copy(update={
            "task_id": new_id("task"),
            "task_ref": "task_b",
            "produces": ("result:b",),
            "validations": (
                self.task.validations[0].model_copy(update={"validation_id": "validation_b"}),
            ),
        })
        split_skeleton = PlanSkeletonCandidate(
            candidate_id=new_id("candidate"),
            goal_contract_digest=self.goal.definition_digest,
            state_signature=self.state.semantic_digest,
            approach=self.skeleton.approach,
            tasks=(
                TaskSkeleton(
                    task_ref=task_a.task_ref,
                    kind=task_a.kind,
                    objective=task_a.objective,
                    contributes_to=task_a.goal_criterion_refs,
                    produces=task_a.produces,
                    consumes=task_a.consumes,
                ),
                TaskSkeleton(
                    task_ref=task_b.task_ref,
                    kind=task_b.kind,
                    objective=task_b.objective,
                    contributes_to=task_b.goal_criterion_refs,
                    produces=task_b.produces,
                    consumes=task_b.consumes,
                ),
            ),
            goal_coverage=(GoalCoverage(criterion_id="ac_one", task_refs=("task_a", "task_b")),),
            estimated_change_cost=2,
            estimated_context_tokens=200,
        )
        split_skeleton_digest = sha256_digest(split_skeleton)
        self.service.record_skeleton_evaluation(
            CandidateEvaluation(
                candidate=split_skeleton,
                semantic_submission=clean_review(
                    split_skeleton_digest,
                    role="skeleton_reviewer",
                    evidence_catalog=skeleton_review_evidence_catalog(
                        split_skeleton, self.goal, self.state, self.map
                    ),
                ),
                decision=CandidateDecision(
                    candidate_digest=split_skeleton_digest,
                    status=CandidateStatus.ADMISSIBLE,
                    fitness_score=100,
                    weakest_dimension="engineering",
                ),
            )
        )
        definition = PlanContractDefinition(
            project_id=self.project_id,
            goal_contract_digest=self.goal.definition_digest,
            base_state_snapshot_digest=self.state.snapshot_digest,
            project_map_digest=self.map.revision_digest,
            source_skeleton_digest=split_skeleton_digest,
            tasks=(task_a, task_b),
            goal_coverage=(
                PlanGoalCoverage(
                    criterion_id="ac_one",
                    task_ids=(task_a.task_id, task_b.task_id),
                    validation_ids=("validation_a", "validation_b", "validation_goal"),
                ),
            ),
            integration_validations=self.plan.definition.integration_validations,
            model_inventory_digest=self.inventory.inventory_digest,
        )
        split_plan = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=self.plan.plan_id,
            revision_no=2,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=self.plan.plan_revision_id,
            created_at=utc_now(),
        )
        evaluation = ExpandedPlanEvaluation(
            plan=split_plan,
            semantic_submissions=(
                clean_review(
                    split_plan.activation_digest,
                    role="compact_plan_reviewer",
                    evidence_catalog=plan_review_evidence_catalog(
                        split_plan, self.goal, self.state, self.map
                    ),
                ),
            ),
            decision=CandidateDecision(
                candidate_digest=split_plan.activation_digest,
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )

        self.service.register_authorized_plan_revision(evaluation)

        self.assertEqual({task_a.task_id, task_b.task_id}, set(self.service.list_ready_tasks(self.project_id)))
        with self.ledger.read() as connection:
            authorization_id = connection.execute(
                "SELECT authorization_id FROM plan_activations WHERE plan_revision_id = ?",
                (split_plan.plan_revision_id,),
            ).fetchone()[0]
        self.assertEqual(authorization.authorization_id, authorization_id)

    def test_model_copy_bypass_of_operating_policy_is_rejected(self) -> None:
        invalid = GoalOperatingPolicy().model_copy(
            update={"max_same_failure_replans": 11}
        )

        with self.assertRaises(ValueError):
            self.service.authorize_goal(
                project_id=self.project_id,
                source="user",
                operating_policy=invalid,
            )

    def test_in_flight_attempt_delays_authorized_plan_replacement_without_mutation(self) -> None:
        self.authorize()
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        revision, _ = self.register_internal_revision()
        before = self.replacement_state()

        with self.assertRaisesRegex(EngineServiceError, "PLAN_REPLACEMENT_IN_FLIGHT"):
            self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)

        after = self.replacement_state()
        self.assertEqual(before, after)
        self.assertIn((attempt.attempt_id, self.plan.plan_revision_id, self.task.task_id, "reserved", None),
                      after["attempts"])

    def test_running_attempt_with_actual_binding_delays_replacement_and_preserves_receipt(self) -> None:
        self.authorize()
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        intent = self.service.prepare_runtime_intent(
            attempt_id=attempt.attempt_id,
            kind=RuntimeIntentKind.CREATE_THREAD,
            idempotency_key="running-binding-intent-0001",
            request={"task_id": self.task.task_id},
        )
        binding = ThreadBinding(
            thread_id="thread-authorized-replacement",
            turn_id="turn-authorized-replacement",
            bound_at=utc_now(),
        )
        receipt = self.service.record_runtime_receipt(
            intent_id=intent.intent_id,
            provider_operation_id="operation-authorized-replacement",
            response={"thread_id": binding.thread_id},
            binding=binding,
        )
        revision, _ = self.register_internal_revision()
        before = self.replacement_state()

        with self.assertRaisesRegex(EngineServiceError, "PLAN_REPLACEMENT_IN_FLIGHT"):
            self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)

        after = self.replacement_state()
        self.assertEqual(before, after)
        self.assertTrue(any(
            row[0] == receipt.receipt_id and row[1] == intent.intent_id and row[4] is not None
            for row in after["receipts"]
        ))

    def test_unknown_runtime_intent_delays_replacement_without_mutating_ledger(self) -> None:
        self.authorize()
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        intent = self.service.prepare_runtime_intent(
            attempt_id=attempt.attempt_id,
            kind=RuntimeIntentKind.CREATE_THREAD,
            idempotency_key="unknown-intent-replacement-0001",
            request={"task_id": self.task.task_id},
        )
        self.assertEqual((intent.intent_id,), self.service.recover_inspect(self.project_id))
        revision, _ = self.register_internal_revision()
        before = self.replacement_state()

        with self.assertRaisesRegex(EngineServiceError, "PLAN_REPLACEMENT_IN_FLIGHT"):
            self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)

        after = self.replacement_state()
        self.assertEqual(before, after)
        self.assertTrue(any(row[0] == intent.intent_id and row[2] == "unknown" for row in after["intents"]))
        self.assertTrue(any(row[0] == attempt.attempt_id and row[3] == "unknown" for row in after["attempts"]))

    def test_validating_task_delays_replacement_without_mutating_ledger(self) -> None:
        self.authorize()
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        revision, _ = self.register_internal_revision()
        before = self.replacement_state()

        with self.assertRaisesRegex(EngineServiceError, "PLAN_REPLACEMENT_IN_FLIGHT"):
            self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)

        after = self.replacement_state()
        self.assertEqual(before, after)
        self.assertTrue(any(
            row[0] == self.task.task_id and row[2] == "validating" for row in after["tasks"]
        ))


if __name__ == "__main__":
    unittest.main()
