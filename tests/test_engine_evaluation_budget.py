from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetBlocked
from flowmarshal.engine.evaluation_budget import (
    EvaluationPolicies,
    budgeted_role_runner,
    initialize_cell_budget,
    load_evaluation_policies,
    metadata_with_policies,
    policies_from_metadata,
    policy_contract_fragment,
    register_and_attach_goal,
    verify_metadata_digest,
    write_immutable_run_metadata,
)
from flowmarshal.engine.runtime import CodexProjectBinding
from flowmarshal.engine.role_execution import (
    RoleTimeoutOverride,
    RoleTimeoutPolicy,
    use_role_timeout_policy,
)
from flowmarshal.engine.roles import RoleCallReceipt, make_role_request
from flowmarshal.engine.budget import GoalBudgetPolicy
from tests.engine_helpers import goal, profile


def _digest(value: str) -> str:
    return sha256_digest(value)


class _NoProviderRuntime:
    requires_budget_policy = True

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str):
        self.calls += 1
        raise AssertionError(f"예산 정책 누락 시 provider {name}를 호출하면 안 됩니다.")


class EvaluationBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.project_id = "project_" + "1" * 32
        self.policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(
                default_timeout_seconds=30,
                overrides=(RoleTimeoutOverride(
                    role="goal_reviewer", timeout_seconds=45,
                    replaces_timeout_seconds=900, reason="qualification test",
                ),),
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _profile(self):
        return profile(self.project_id)

    def _request(self):
        return make_role_request(
            role="goal_reviewer", instructions="검토", payload={},
            output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
            inventory_digest=_digest("inventory"), cwd=str(self.root),
        )

    def test_user_policies_bind_metadata_timeout_and_immutable_contract(self) -> None:
        with use_role_timeout_policy(self.policies.role_timeouts):
            request = self._request()
        self.assertEqual(45, request.timeout_seconds)
        self.assertEqual(self.policies.role_timeouts.policy_digest, request.timeout_policy_digest)
        self.assertEqual(
            {
                "evaluation_policy_digest": self.policies.policy_digest,
                "budget_policy_digest": sha256_digest(self.policies.budget),
                "role_timeout_policy_digest": self.policies.role_timeouts.policy_digest,
                "max_schema_recovery_attempts": 0,
                "ephemeral_threads": False,
            },
            policy_contract_fragment(self.policies),
        )

        metadata = metadata_with_policies({"scope": "role-fixture"}, self.policies)
        verify_metadata_digest(metadata)
        self.assertEqual(self.policies, policies_from_metadata(metadata))
        tampered = json.loads(json.dumps(metadata))
        tampered["evaluation_policies"]["budget"]["total_tokens"] = 101
        with self.assertRaisesRegex(ValueError, "EVALUATION_POLICY_METADATA_DIGEST_MISMATCH"):
            policies_from_metadata(tampered)
        tampered = dict(metadata, scope="project-e2e")
        with self.assertRaisesRegex(ValueError, "EVALUATION_RUN_METADATA_DIGEST_MISMATCH"):
            verify_metadata_digest(tampered)

        legacy_policies = {
            "budget": self.policies.budget.model_dump(mode="json"),
            "role_timeouts": self.policies.role_timeouts.model_dump(mode="json"),
            "max_provider_calls": 14,
            "wall_timeout_seconds": 28_800,
        }
        self.assertNotIn("codex_project", self.policies.model_dump(mode="json", exclude_none=True))
        self.assertEqual(sha256_digest(legacy_policies), self.policies.policy_digest)
        self.assertEqual(legacy_policies, metadata["evaluation_policies"])

        destination = Path(self.temp.name) / "run-metadata.json"
        write_immutable_run_metadata(destination, {"scope": "role-fixture"}, self.policies)
        write_immutable_run_metadata(destination, {"scope": "role-fixture"}, self.policies)
        with self.assertRaisesRegex(ValueError, "EVALUATION_RUN_METADATA_BINDING_MISMATCH"):
            write_immutable_run_metadata(destination, {"scope": "project-e2e"}, self.policies)

    def test_codex_project_policy_is_loaded_bound_and_tamper_detected(self) -> None:
        budget_path = Path(self.temp.name) / "budget.json"
        timeout_path = Path(self.temp.name) / "timeouts.json"
        binding_path = Path(self.temp.name) / "codex-project.json"
        budget_path.write_text(self.policies.budget.model_dump_json(), encoding="utf-8")
        timeout_path.write_text(self.policies.role_timeouts.model_dump_json(), encoding="utf-8")
        binding = CodexProjectBinding(
            project_id="codex-project-1",
            expected_root=str(self.root),
            expected_name="Qualification project",
        )
        binding_path.write_text(binding.model_dump_json(), encoding="utf-8")

        policies = load_evaluation_policies(
            budget_policy_path=budget_path,
            role_timeout_policy_path=timeout_path,
            codex_project_binding_path=binding_path,
        )
        metadata = metadata_with_policies({"scope": "role-fixture"}, policies)

        self.assertEqual(binding, policies.codex_project)
        self.assertEqual(binding.model_dump(mode="json"), metadata["evaluation_policies"]["codex_project"])
        self.assertEqual(binding.model_dump(mode="json"), policy_contract_fragment(policies)["codex_project"])
        self.assertEqual(policies, policies_from_metadata(metadata))
        tampered = json.loads(json.dumps(metadata))
        tampered["evaluation_policies"]["codex_project"]["project_id"] = "other-project"
        with self.assertRaisesRegex(ValueError, "EVALUATION_POLICY_METADATA_DIGEST_MISMATCH"):
            policies_from_metadata(tampered)
        destination = Path(self.temp.name) / "bound-run-metadata.json"
        write_immutable_run_metadata(destination, {"scope": "role-fixture"}, policies)
        changed_policies = policies.model_copy(update={
            "codex_project": binding.model_copy(update={"project_id": "other-project"}),
        })
        with self.assertRaisesRegex(ValueError, "EVALUATION_RUN_METADATA_BINDING_MISMATCH"):
            write_immutable_run_metadata(destination, {"scope": "role-fixture"}, changed_policies)

    def test_new_cell_attaches_goal_and_settles_role_call_in_its_ledger(self) -> None:
        service, manager = initialize_cell_budget(
            state_root=Path(self.temp.name) / "state", workspace=self.root,
            project_id=self.project_id, profile=self._profile(), policies=self.policies,
        )
        fixture_goal = goal(self.project_id, self._profile().definition_digest)
        register_and_attach_goal(service, manager, fixture_goal)
        call_id = manager.reserve(
            project_id=self.project_id, goal_id=fixture_goal.goal_id,
            goal_digest=fixture_goal.definition_digest, call_key="qualification-review",
            role="goal_reviewer", request={"fixture": "one"},
        )
        receipt = RoleCallReceipt(
            call_id="qualification-review", role="goal_reviewer", status="succeeded",
            model="test-model", effort="medium", inventory_digest=_digest("inventory"),
            permission_profile=":danger-full-access", approval_policy="never",
            input_digest=_digest("input"), output_digest=_digest("output"),
            output_schema_digest=_digest("schema"), input_tokens=5, cached_input_tokens=0,
            output_tokens=2, reasoning_tokens=0, usage_available=True, latency_ms=0,
            recorded_at=fixture_goal.created_at,
        )
        manager.settle(call_id, receipt)
        with service.ledger.read() as connection:
            call = connection.execute(
                "SELECT goal_id,goal_contract_digest,status,actual_tokens,usage_id FROM provider_calls WHERE id=?",
                (call_id,),
            ).fetchone()
        assert call is not None
        self.assertEqual(
            (fixture_goal.goal_id, fixture_goal.definition_digest, "settled", 7),
            (call["goal_id"], call["goal_contract_digest"], call["status"], call["actual_tokens"]),
        )
        self.assertIsNotNone(call["usage_id"])

    def test_resume_rejects_changed_policy_or_goal_binding(self) -> None:
        state_root = Path(self.temp.name) / "resume-state"
        original_profile = self._profile()
        service, manager = initialize_cell_budget(
            state_root=state_root, workspace=self.root, project_id=self.project_id,
            profile=original_profile, policies=self.policies,
        )
        original_goal = goal(self.project_id, original_profile.definition_digest)
        register_and_attach_goal(service, manager, original_goal)
        changed_definition = original_goal.definition.model_copy(update={"observable_outcome": "다른 resume Goal"})
        changed_goal = original_goal.model_copy(update={
            "definition": changed_definition, "definition_digest": changed_definition.definition_digest,
        })
        with self.assertRaisesRegex(ValueError, "EVALUATION_GOAL_RESUME_BINDING_MISMATCH"):
            register_and_attach_goal(service, manager, changed_goal)
        changed_policies = self.policies.model_copy(update={
            "budget": GoalBudgetPolicy(total_tokens=101, call_reservation_tokens=10),
        })
        with self.assertRaisesRegex(ValueError, "EVALUATION_CELL_BUDGET_BINDING_MISMATCH"):
            initialize_cell_budget(
                state_root=state_root, workspace=self.root, project_id=self.project_id,
                profile=original_profile, policies=changed_policies,
            )

    def test_missing_policy_blocks_before_live_provider_call(self) -> None:
        # 새 cell helper를 우회해 정책이 없는 원장을 의도적으로 만든다.
        from flowmarshal.engine.ledger import SQLiteEngineLedger
        from flowmarshal.engine.service import EngineService

        service = EngineService(SQLiteEngineLedger(Path(self.temp.name) / "no-policy.sqlite3"))
        service.initialize()
        service.create_project(name="정책 없음", root=self.root, project_id=self.project_id)
        current_profile = self._profile()
        service.register_profile(current_profile)
        current_goal = goal(self.project_id, current_profile.definition_digest)
        service.register_goal(current_goal)
        runtime = _NoProviderRuntime()
        runner = budgeted_role_runner(
            runtime, service, project_id=self.project_id, goal_id=current_goal.goal_id,
            goal_digest=current_goal.definition_digest,
        )
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_POLICY_REQUIRED"):
            runner.run(self._request())
        self.assertEqual(0, runtime.calls)
        with service.ledger.read() as connection:
            calls = connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0]
        self.assertEqual(0, calls)


if __name__ == "__main__":
    unittest.main()
