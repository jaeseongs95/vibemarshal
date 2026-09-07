import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.budget import BudgetManager, BudgetedRoleRunner, GoalBudgetPolicy
from flowmarshal.engine.domain import GoalPreparationBinding, GoalReviewRatingsBinding
from flowmarshal.engine.evaluation_budget import (
    EvaluationPolicies,
    initialize_cell_budget,
    register_and_attach_goal,
)
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.qualification import (
    PlanningScenario,
    _bind_planning_runner_goal,
    _planning_cell,
    default_role_configuration,
)
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import (
    RoleCallReceipt,
    RoleCallResult,
    StructuredRoleError,
    make_role_request,
    strict_json_output_schema,
)
from tests.engine_helpers import goal, profile, skeleton, state


class _ProviderOnlyRunner:
    """Provider 결과만 모사하고 실제 BudgetedRoleRunner를 통과시킨다."""

    max_schema_recovery_attempts = 0

    def __init__(self) -> None:
        self.receipts = []
        self.call_number = 0

    def run(self, request, *, validator=None):
        self.call_number += 1
        schema_failed = request.role == "high_risk_reviewer"
        receipt = RoleCallReceipt(
            call_id=f"provider-{self.call_number}",
            role=request.role,
            status="schema_failed" if schema_failed else "succeeded",
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id=f"thread-{self.call_number}",
            turn_ids=(f"turn-{self.call_number}",),
            input_digest=request.request_digest,
            output_digest=None if schema_failed else sha256_digest({"role": request.role}),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            input_tokens=5,
            cached_input_tokens=0,
            output_tokens=2,
            reasoning_tokens=0,
            usage_available=True,
            latency_ms=1,
            recorded_at=datetime.now(timezone.utc),
        )
        self.receipts.append(receipt)
        if schema_failed:
            raise StructuredRoleError("schema failed", receipt=receipt)
        return RoleCallResult(payload={"role": request.role}, receipt=receipt)


class PlanningBudgetBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.project_id = "project_" + "9" * 32
        self.profile = profile(self.project_id)
        self.policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=1_000, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, role: str, *, candidate=None):
        payload = {"role": role}
        if role == "skeleton_reviewer":
            if candidate is None:
                raise AssertionError("skeleton_reviewer 요청에는 실제 Skeleton이 필요합니다.")
            payload = {
                "evidence_catalog": {
                    "artifact:skeleton": candidate.model_dump(mode="json"),
                },
            }
        return make_role_request(
            role=role,
            instructions=f"{role} request",
            payload=payload,
            output_schema={"type": "object", "properties": {}},
            model="test-model",
            effort="medium",
            inventory_digest=sha256_digest("inventory"),
            cwd=str(self.workspace),
        )

    @staticmethod
    def planning_inventory(roles) -> ModelInventory:
        grouped: dict[str, set[str]] = {}
        for key in (
            "normalizer", "skeleton_generator", "plan_expander", "general_reviewer",
            "critical_reviewer", "executor", "validator",
        ):
            binding = roles.binding_for(key)
            grouped.setdefault(binding.model, set()).add(binding.effort)
        return ModelInventory(
            executable_digest="sha256:" + "0" * 64,
            runtime_capabilities=RUNTIME_CAPABILITIES,
            source="planning-budget-binding-test",
            models=tuple(
                ModelCapability(model=model, supported_efforts=tuple(sorted(efforts)))
                for model, efforts in sorted(grouped.items())
            ),
        )

    def test_post_goal_planning_calls_use_registered_goal_for_call_and_usage(self) -> None:
        service, manager = initialize_cell_budget(
            state_root=Path(self.temporary.name) / "state",
            workspace=self.workspace,
            project_id=self.project_id,
            profile=self.profile,
            policies=self.policies,
        )
        provider = _ProviderOnlyRunner()
        prepared_goal = goal(self.project_id, self.profile.definition_digest)
        runner = BudgetedRoleRunner(
            provider,
            service,
            project_id=self.project_id,
            goal_id=prepared_goal.goal_id,
            goal_digest=None,
        )

        # Goal 준비 두 호출은 아직 digest 없이 기록되고 attach가 원장을 결속한다.
        runner.run(self.request("goal_normalizer"))
        runner.run(self.request("goal_reviewer"))
        register_and_attach_goal(service, manager, prepared_goal)

        # _planning_cell의 등록 직후 결속과 같은 전환이다.
        _bind_planning_runner_goal(runner, prepared_goal)
        candidate = skeleton(
            prepared_goal,
            state(
                self.project_id,
                prepared_goal.definition_digest,
                sha256_digest("planning-budget-binding-map"),
            ),
        )
        runner.run(self.request("skeleton_generator"))
        runner.run(self.request("skeleton_reviewer", candidate=candidate))
        runner.run(self.request("plan_expander"))
        with self.assertRaisesRegex(StructuredRoleError, "schema failed"):
            runner.run(self.request("high_risk_reviewer"))

        with service.ledger.read() as connection:
            calls = connection.execute(
                "SELECT role,goal_id,goal_contract_digest,status,usage_id FROM provider_calls "
                "ORDER BY created_at,id"
            ).fetchall()
            usages = connection.execute(
                "SELECT goal_contract_digest,payload_json FROM budget_usage "
                "ORDER BY recorded_at,id"
            ).fetchall()

        self.assertEqual(6, len(calls))
        self.assertEqual(6, len(usages))
        self.assertEqual(
            {"goal_normalizer", "goal_reviewer", "skeleton_generator", "skeleton_reviewer", "plan_expander", "high_risk_reviewer"},
            {row["role"] for row in calls},
        )
        self.assertTrue(all(row["goal_id"] == prepared_goal.goal_id for row in calls))
        self.assertTrue(all(row["goal_contract_digest"] == prepared_goal.definition_digest for row in calls))
        self.assertTrue(all(row["usage_id"] is not None for row in calls))
        self.assertEqual("settled", next(
            row["status"] for row in calls if row["role"] == "high_risk_reviewer"
        ))
        self.assertTrue(all(row["goal_contract_digest"] == prepared_goal.definition_digest for row in usages))
        usage_documents = [json.loads(row["payload_json"]) for row in usages]
        schema_usage = next(row for row in usage_documents if row["call_status"] == "schema_failed")
        self.assertTrue(schema_usage["usage_available"])
        summary = BudgetManager(service).status(self.project_id, goal_id=prepared_goal.goal_id)
        self.assertIsNone(summary.error_code)
        self.assertEqual(42, summary.measured_token_subtotal)
        usage = EngineApplication(service).usage_summary(
            self.project_id, goal_id=prepared_goal.goal_id
        )
        self.assertIsNone(usage.error_code)
        self.assertEqual((), usage.incomplete_reasons)
        self.assertEqual(6, usage.logical_call_count)
        self.assertEqual(6, usage.provider_call_count)
        self.assertEqual(42, usage.input_tokens.total + usage.output_tokens.total)
        self.assertTrue(service.ledger.verify_history(self.project_id))

    def test_planning_cell_binds_runner_before_first_skeleton_call(self) -> None:
        """_planning_cell의 실제 callsite가 Goal 이후 호출에 digest를 전달한다."""

        roles = default_role_configuration(Path(__file__).resolve().parents[1])
        inventory = self.planning_inventory(roles)
        provider = _ProviderOnlyRunner()
        captured = {}

        def runner_factory(_runtime, service, *, project_id, goal_id, goal_digest, **_kwargs):
            runner = BudgetedRoleRunner(
                provider, service, project_id=project_id, goal_id=goal_id, goal_digest=goal_digest
            )
            captured["runner"] = runner
            captured["service"] = service
            return runner

        def prepared_goal(pipeline, *, project_id, profile, goal_id, **_kwargs):
            pipeline.normalizer.runner.run(self.request("goal_normalizer"))
            pipeline.reviewer.runner.run(self.request("goal_reviewer"))
            contract = goal(project_id, profile.definition_digest).model_copy(update={
                "goal_id": goal_id,
                "preparation_binding": GoalPreparationBinding(
                    normalization_proposal_digest=sha256_digest("proposal"),
                    reviewer_submission_digest=sha256_digest("review"),
                    reviewer_role="goal_reviewer",
                    ratings=GoalReviewRatingsBinding(
                        goal_fit=4, grounding=4, engineering=4, verification=4, execution_safety=4,
                    ),
                ),
            })
            return SimpleNamespace(
                goal_contract=contract,
                proposal=SimpleNamespace(unresolved_questions=()),
                model_dump=lambda **_ignored: {"test_double": True},
            )

        class StopAfterSkeleton:
            def __init__(self, runner, *, model, effort, inventory_digest, cwd, **_kwargs):
                self.runner = runner
                self.model = model
                self.effort = effort
                self.inventory_digest = inventory_digest
                self.cwd = cwd

            def generate(self, **_kwargs):
                self.runner.run(make_role_request(
                    role="skeleton_generator", instructions="post-goal skeleton", payload={},
                    output_schema={"type": "object", "properties": {}}, model=self.model,
                    effort=self.effort, inventory_digest=self.inventory_digest, cwd=str(self.cwd),
                ))
                raise RuntimeError("STOP_AFTER_POST_GOAL_SKELETON")

        scenario = PlanningScenario(
            scenario_id="S01-single-bugfix", path_kind="single_path", source_request="계획을 만든다.",
            expected_disposition="selected", candidate_count=1,
        )
        fixture_root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "engine" / "live-smoke-project"
        with patch("flowmarshal.engine.qualification.budgeted_role_runner", side_effect=runner_factory), patch(
            "flowmarshal.engine.qualification.GoalPreparationPipeline.prepare", new=prepared_goal
        ), patch("flowmarshal.engine.qualification.AssignmentResolver.resolve_contract", return_value=None), patch(
            "flowmarshal.engine.qualification.SkeletonGeneratorAdapter", StopAfterSkeleton
        ), self.assertRaisesRegex(RuntimeError, "STOP_AFTER_POST_GOAL_SKELETON"):
            _planning_cell(
                scenario=scenario, seed=17, fixture_root=fixture_root, runtime=object(),
                inventory=inventory, roles=roles, work_root=Path(self.temporary.name) / "cell",
                evaluation_policies=self.policies,
            )

        service = captured["service"]
        bound_goal = captured["runner"].goal_digest
        with service.ledger.read() as connection:
            calls = connection.execute(
                "SELECT goal_contract_digest FROM provider_calls ORDER BY created_at,id"
            ).fetchall()
        self.assertEqual(3, len(calls))
        self.assertIsNotNone(bound_goal)
        self.assertTrue(all(row["goal_contract_digest"] == bound_goal for row in calls))
        usage = EngineApplication(service).usage_summary(
            captured["runner"].project_id, goal_id=captured["runner"].goal_id
        )
        self.assertIsNone(usage.error_code)
        self.assertEqual((), usage.incomplete_reasons)
        self.assertEqual(3, usage.logical_call_count)


if __name__ == "__main__":
    unittest.main()
