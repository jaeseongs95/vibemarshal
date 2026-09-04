from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.goal import GoalNormalizerAdapter, GoalReviewerAdapter, GoalNormalizationProposal, ReviewDraft
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter, PlanReviewerAdapter, PlanExpansionEnvelope, PlanReviewEnvelope, SkeletonBatchDraft,
    SkeletonCandidateDraft, SkeletonGeneratorAdapter, SkeletonReviewerAdapter,
    READ_ONLY_REPORTING_INSTRUCTIONS,
)
from flowmarshal.engine.qualification import (
    PlanningScenarioCatalog, _planning_contract, _role_progress, default_role_configuration,
)
from flowmarshal.engine.roles import strict_json_output_schema
from tests.test_engine_qualification import qualification_inventory

ROOT = Path(__file__).resolve().parents[1]


class EvaluationBindingTests(unittest.TestCase):
    def test_e2e_contract_locks_provider_schema_and_separate_goal_instructions(self):
        from flowmarshal.engine.e2e_qualification import _contract
        from flowmarshal.engine.execution import ExecutionPreparation, ProviderExecutionPreparation, GoalTestPreparation
        from flowmarshal.engine.runtime import EngineDispatcher
        args = (ROOT, qualification_inventory(), default_role_configuration(ROOT), "sha256:" + "1" * 64)
        contract = _contract(*args)
        schemas = {
            "preparation": strict_json_output_schema(ProviderExecutionPreparation.model_json_schema()),
            "goal": strict_json_output_schema(GoalTestPreparation.model_json_schema()),
            "semantic": EngineDispatcher._semantic_schema(),
        }
        self.assertEqual(sha256_digest(schemas), contract.output_schema_digest)
        schemas["preparation"] = strict_json_output_schema(ExecutionPreparation.model_json_schema())
        self.assertNotEqual(sha256_digest(schemas), contract.output_schema_digest)
        with patch("flowmarshal.engine.execution.GOAL_TEST_PREPARATION_INSTRUCTIONS", "변경된 Goal Test 지침"):
            self.assertNotEqual(_contract(*args).prompt_digest, contract.prompt_digest)

    def test_pipeline_contract_hashes_real_prompt_builders_and_schemas(self):
        catalog = PlanningScenarioCatalog.model_validate_json(
            (ROOT / "tests/fixtures/engine/planning-scenarios.json").read_text(encoding="utf-8"))
        contract = _planning_contract(ROOT, catalog, qualification_inventory(), default_role_configuration(ROOT))
        prompts = {role.__name__: inspect.getsource(role) for role in (
            GoalNormalizerAdapter, GoalReviewerAdapter, SkeletonGeneratorAdapter,
            SkeletonReviewerAdapter, PlanExpanderAdapter, PlanReviewerAdapter)}
        from flowmarshal.engine import goal as goal_roles, planner_roles
        prompts["shared_instructions"] = {
            name: value for module in (goal_roles, planner_roles) for name, value in vars(module).items()
            if name.endswith("_INSTRUCTIONS") and isinstance(value, str)
        }
        schemas = {model.__name__: strict_json_output_schema(model.model_json_schema()) for model in (
            GoalNormalizationProposal, SkeletonBatchDraft, SkeletonCandidateDraft, PlanExpansionEnvelope, PlanReviewEnvelope, ReviewDraft)}
        self.assertEqual(sha256_digest(prompts), contract.prompt_digest)
        self.assertEqual(sha256_digest(schemas), contract.output_schema_digest)
        from tests.engine_helpers import assignment, skeleton
        from tests.test_engine_plan_inspection import inputs
        from flowmarshal.engine.planner_roles import RuleBasedTaskAssigner
        plan, goal, snapshot, project_map = inputs("clean")

        class Captured(Exception):
            pass

        class Capture:
            def run(self, request, **kwargs):
                self.request = request
                raise Captured()

        capture = Capture()
        options = dict(model="validator", effort="high", inventory_digest="sha256:" + "1" * 64, cwd=ROOT)
        with self.assertRaises(Captured):
            PlanReviewerAdapter(capture, **options).review(plan=plan, goal=goal, state=snapshot,
                                                           project_map=project_map, risk_route="compact_plan_reviewer")
        self.assertEqual(schemas["PlanReviewEnvelope"], strict_json_output_schema(capture.request.output_schema))
        models = assignment()
        with self.assertRaises(Captured):
            PlanExpanderAdapter(capture, RuleBasedTaskAssigner(models, models, models), **options).expand(
                candidate=skeleton(goal, snapshot), goal=goal, state=snapshot, project_map=project_map)
        self.assertEqual(schemas["PlanExpansionEnvelope"], strict_json_output_schema(capture.request.output_schema))
        changed = dict(prompts)
        changed["GoalNormalizerAdapter"] += "\n실제 지침 변경"
        self.assertNotEqual(sha256_digest(changed), contract.prompt_digest)
        for name in prompts["shared_instructions"]:
            module = "goal" if name == "GOAL_INTERPRETATION_INSTRUCTIONS" else "planner_roles"
            with self.subTest(shared_instruction=name), patch(f"flowmarshal.engine.{module}.{name}", "변경된 지침"):
                self.assertNotEqual(_planning_contract(ROOT, catalog, qualification_inventory(),
                                                     default_role_configuration(ROOT)).prompt_digest,
                                    contract.prompt_digest)

    def test_partial_role_progress_is_not_a_completed_cell(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = _role_progress(root, scenario_id="synthetic", order_seed=17)
            progress({"event": "thread_created", "thread_id": "known-thread", "ephemeral": True})
            progress({"event": "turn_started", "thread_id": "known-thread", "turn_id": "known-turn"})
            events = [json.loads(path.read_text(encoding="utf-8")) for path in (root / "role-progress").glob("*.json")]
            self.assertEqual(2, len(events))
            self.assertFalse((root / "cells").exists())
            self.assertEqual({"synthetic"}, {event["scenario_id"] for event in events})


if __name__ == "__main__":
    unittest.main()
