from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from flowmarshal.engine.budget import BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.domain import ThreadBinding
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import GoalAuthorizationRequired
from tests import test_engine_goal_authorization as authorization_tests
from tests.test_engine_ledger_service import EngineServiceFixture


class EngineAuthorizationFreshnessTests(EngineServiceFixture):
    internal_revision_evaluation = authorization_tests.EngineGoalAuthorizationTests.internal_revision_evaluation
    register_internal_revision = authorization_tests.EngineGoalAuthorizationTests.register_internal_revision

    def authorize(self):
        return self.service.authorize_goal(project_id=self.project_id, source="user")

    def configure_budget(self, total_tokens: int, *, goal_override: bool = False) -> None:
        BudgetManager(self.service).configure(
            self.project_id,
            GoalBudgetPolicy(
                total_tokens=total_tokens,
                call_reservation_tokens=1_000,
            ),
            goal_id=self.goal.goal_id if goal_override else "",
        )

    def test_new_goal_override_cannot_expand_approved_project_budget(self) -> None:
        self.configure_budget(100_000)
        self.authorize()
        self.configure_budget(200_000, goal_override=True)

        with self.assertRaises(GoalAuthorizationRequired) as captured:
            self.service.activate_authorized_plan(
                plan_revision_id=self.plan.plan_revision_id
            )

        self.assertTrue(
            any(
                change["field"] == f"budget.effective.{self.goal.goal_id}"
                and change["approved"]["total_tokens"] == 100_000
                and change["requested"]["total_tokens"] == 200_000
                for change in captured.exception.changes
            )
        )

    def test_new_goal_override_may_lower_approved_project_budget(self) -> None:
        self.configure_budget(200_000)
        authorization = self.authorize()
        self.configure_budget(100_000, goal_override=True)

        self.service.activate_authorized_plan(
            plan_revision_id=self.plan.plan_revision_id
        )

        with self.ledger.read() as connection:
            activation = connection.execute(
                "SELECT authorization_id FROM plan_activations "
                "WHERE plan_revision_id = ?",
                (self.plan.plan_revision_id,),
            ).fetchone()
        self.assertEqual(authorization.authorization_id, activation["authorization_id"])

    def test_policy_expansion_after_runtime_intent_blocks_before_provider_effect(self) -> None:
        self.configure_budget(100_000)
        self.authorize()
        self.service.activate_authorized_plan(
            plan_revision_id=self.plan.plan_revision_id
        )
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        runtime = FakeCodexRuntime(self.inventory)
        expanded = False

        def expand_after_intent(point: str) -> None:
            nonlocal expanded
            if point == "after_thread_intent" and not expanded:
                expanded = True
                self.configure_budget(200_000, goal_override=True)

        dispatcher = EngineDispatcher(
            self.service,
            runtime,
            fault_hook=expand_after_intent,
        )

        blocked = dispatcher.run_once(self.project_id)

        self.assertTrue(expanded)
        self.assertEqual("blocked", blocked.action.value)
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code)
        self.assertEqual(0, runtime.create_calls)
        with self.ledger.read() as connection:
            prepared = connection.execute(
                "SELECT id, status FROM runtime_intents "
                "WHERE attempt_id = ? AND kind = 'create_thread'",
                (attempt.attempt_id,),
            ).fetchone()
        self.assertEqual("prepared", prepared["status"])

        self.authorize()
        resumed = dispatcher.run_once(self.project_id)

        self.assertEqual("dispatched", resumed.action.value)
        self.assertEqual(1, runtime.create_calls)
        with self.ledger.read() as connection:
            replayed = connection.execute(
                "SELECT id, status FROM runtime_intents "
                "WHERE attempt_id = ? AND kind = 'create_thread'",
                (attempt.attempt_id,),
            ).fetchall()
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM runtime_receipts WHERE intent_id = ?",
                (prepared["id"],),
            ).fetchone()[0]
        self.assertEqual([(prepared["id"], "received")], [tuple(row) for row in replayed])
        self.assertEqual(1, receipt_count)

    def test_policy_expansion_blocks_deterministic_validation_command(self) -> None:
        self.configure_budget(100_000)
        self.authorize()
        self.service.activate_authorized_plan(
            plan_revision_id=self.plan.plan_revision_id
        )
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        runtime = FakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(self.service, runtime)
        dispatched = dispatcher.run_once(self.project_id)
        with self.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(
                connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?",
                    (dispatched.attempt_id,),
                ).fetchone()[0]
            )
        runtime.complete(binding.thread_id)
        dispatcher.run_once(self.project_id)
        self.configure_budget(200_000, goal_override=True)

        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"ok", b""),
        ) as command:
            blocked = dispatcher.run_once(self.project_id)

        self.assertEqual("blocked", blocked.action.value)
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code)
        self.assertEqual(0, command.call_count)

    def test_policy_expansion_blocks_execution_preparation_provider(self) -> None:
        self.configure_budget(100_000)
        self.authorize()
        self.service.activate_authorized_plan(
            plan_revision_id=self.plan.plan_revision_id
        )
        self.configure_budget(200_000, goal_override=True)
        provider = MagicMock()
        dispatcher = EngineDispatcher(
            self.service,
            FakeCodexRuntime(self.inventory),
            proposal_provider=provider,
        )

        blocked = dispatcher.run_once(self.project_id)

        self.assertEqual("blocked", blocked.action.value)
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code)
        provider.prepare_task.assert_not_called()

    def test_policy_expansion_blocks_independent_goal_test_command(self) -> None:
        self.configure_budget(100_000)
        integration = self.plan.definition.integration_validations[0].model_copy(
            update={"evidence_mode": "independent"}
        )
        revision, task = self.register_internal_revision(
            definition_updates={"integration_validations": (integration,)}
        )
        self.authorize()
        self.service.activate_authorized_plan(
            plan_revision_id=revision.plan_revision_id
        )
        self.plan, self.task = revision, task
        spec = self.spec(task=task)
        self.service.materialize_execution_spec(spec, inventory=self.inventory)
        runtime = FakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(self.service, runtime)
        dispatched = dispatcher.run_once(self.project_id)
        with self.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(
                connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?",
                    (dispatched.attempt_id,),
                ).fetchone()[0]
            )
        runtime.complete(binding.thread_id)
        dispatcher.run_once(self.project_id)
        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"ok", b""),
        ):
            self.assertEqual("validated", dispatcher.run_once(self.project_id).action.value)
        self.assertEqual("completed", dispatcher.run_once(self.project_id).action.value)
        goal_step = spec.definition.validation_steps[0].model_copy(
            update={"validation_id": integration.validation_id}
        )
        self.assertEqual(
            "materialized",
            dispatcher.run_once(
                self.project_id,
                goal_validation_step=goal_step,
            ).action.value,
        )
        self.configure_budget(200_000, goal_override=True)

        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"ok", b""),
        ) as command:
            blocked = dispatcher.run_once(self.project_id)

        self.assertEqual("blocked", blocked.action.value)
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code)
        self.assertEqual(0, command.call_count)


if __name__ == "__main__":
    unittest.main()
