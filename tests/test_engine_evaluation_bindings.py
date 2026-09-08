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
    PlanExpanderAdapter, PlanReviewerAdapter, PlanExpansionEnvelope, PlanExpansionEnvelopeV2,
    PlanReviewEnvelope, PlanReviewEnvelopeV2, SkeletonBatchDraft,
    SkeletonCandidateDraft, SkeletonGeneratorAdapter, SkeletonRefinementDraft,
    SkeletonReviewerAdapter,
    PLAN_EXPANSION_INSPECTION_INSTRUCTIONS, READ_ONLY_REPORTING_INSTRUCTIONS,
    PlanRefinementDraft,
)
from flowmarshal.engine.planning import PlanningSearchOutcome
from flowmarshal.engine.plan_review_adjudication import PlanReviewAdjudicationDraft
from flowmarshal.engine.plan_inspection_provider import (
    PLAN_INSPECTION_PROVIDER_V1, PLAN_INSPECTION_PROVIDER_V2,
)
from flowmarshal.engine.evaluation import CheckpointContractError, ImmutableCheckpointStore
from flowmarshal.engine.qualification import (
    PlanningPartialFeasibleObservation, PlanningScenarioCatalog, _planning_contract,
    _role_progress, default_role_configuration,
)
from flowmarshal.engine.roles import strict_json_output_schema
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from tests.test_engine_qualification import qualification_inventory

ROOT = Path(__file__).resolve().parents[1]
POLICIES = EvaluationPolicies(
    budget=GoalBudgetPolicy(total_tokens=100_000, call_reservation_tokens=1_000),
    role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
)


class EvaluationBindingTests(unittest.TestCase):
    def test_writer_catalog_binding_does_not_inject_v1_citation_rules_into_v2(self):
        from flowmarshal.engine.planner_roles import RuleBasedTaskAssigner
        from tests.engine_helpers import assignment, skeleton
        from tests.test_engine_plan_inspection import inputs

        class Captured(Exception):
            pass

        class Capture:
            def run(self, request, **kwargs):
                self.request = request
                raise Captured()

        _, goal, snapshot, project_map = inputs("clean")
        models = assignment()
        capture = Capture()
        with self.assertRaises(Captured):
            PlanExpanderAdapter(
                capture,
                RuleBasedTaskAssigner(models, models, models),
                model="validator",
                effort="high",
                inventory_digest="sha256:" + "1" * 64,
                cwd=ROOT,
                inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2,
            ).expand(
                candidate=skeleton(goal, snapshot), goal=goal, state=snapshot,
                project_map=project_map,
            )

        self.assertEqual(
            "payload.skeleton",
            capture.request.payload["inspection_source_catalog"]["artifact:skeleton"],
        )
        self.assertNotIn(PLAN_EXPANSION_INSPECTION_INSTRUCTIONS, capture.request.instructions)
        self.assertEqual(
            strict_json_output_schema(PlanExpansionEnvelopeV2.model_json_schema()),
            strict_json_output_schema(capture.request.output_schema),
        )
        self.assertNotIn("InspectionCitation", capture.request.output_schema.get("$defs", {}))

    def test_v2_pipeline_contract_is_opt_in_and_cannot_reuse_v1_checkpoint(self):
        catalog = PlanningScenarioCatalog.model_validate_json(
            (ROOT / "tests/fixtures/engine/planning-scenarios.json").read_text(encoding="utf-8")
        )
        args = (ROOT, catalog, qualification_inventory(), default_role_configuration(ROOT), POLICIES)
        v1 = _planning_contract(*args)
        explicit_v1 = _planning_contract(
            *args, inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V1
        )
        v2 = _planning_contract(
            *args, inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2
        )
        self.assertEqual(v1.contract_digest, explicit_v1.contract_digest)
        self.assertNotEqual(v1.contract_digest, v2.contract_digest)
        self.assertNotEqual(v1.prompt_digest, v2.prompt_digest)
        self.assertNotEqual(v1.output_schema_digest, v2.output_schema_digest)
        v2_schemas = {
            model.__name__: strict_json_output_schema(model.model_json_schema())
            for model in (
                GoalNormalizationProposal, SkeletonBatchDraft, SkeletonCandidateDraft,
                SkeletonRefinementDraft,
                PlanExpansionEnvelopeV2, PlanReviewEnvelopeV2, ReviewDraft,
                PlanningPartialFeasibleObservation,
                PlanRefinementDraft, PlanningSearchOutcome, PlanReviewAdjudicationDraft,
            )
        }
        self.assertEqual(sha256_digest(v2_schemas), v2.output_schema_digest)
        with tempfile.TemporaryDirectory() as temp:
            store = ImmutableCheckpointStore(Path(temp), v1)
            store.initialize()
            with self.assertRaises(CheckpointContractError):
                ImmutableCheckpointStore(Path(temp), v2).initialize()

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
        contract = _planning_contract(ROOT, catalog, qualification_inventory(), default_role_configuration(ROOT), POLICIES)
        prompts = {role.__name__: inspect.getsource(role) for role in (
            GoalNormalizerAdapter, GoalReviewerAdapter, SkeletonGeneratorAdapter,
            SkeletonReviewerAdapter, PlanExpanderAdapter, PlanReviewerAdapter)}
        from flowmarshal.engine import goal as goal_roles, planner_roles
        prompts["shared_instructions"] = {
            name: value for module in (goal_roles, planner_roles) for name, value in vars(module).items()
            if name.endswith("_INSTRUCTIONS") and isinstance(value, str)
        }
        prompts["inspection_input_projection"] = inspect.getsource(planner_roles.inspection_source_catalog)
        prompts["inspection_source_verification"] = inspect.getsource(planner_roles.inspection_file_content)
        schemas = {model.__name__: strict_json_output_schema(model.model_json_schema()) for model in (
            GoalNormalizationProposal, SkeletonBatchDraft, SkeletonCandidateDraft,
            SkeletonRefinementDraft, PlanExpansionEnvelope, PlanReviewEnvelope, ReviewDraft,
            PlanningPartialFeasibleObservation, PlanRefinementDraft, PlanningSearchOutcome)}
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
        review_schema = strict_json_output_schema(capture.request.output_schema)
        review_catalog = capture.request.payload["inspection_source_catalog"]
        self.assertEqual(
            sorted(review_catalog),
            review_schema["$defs"]["InspectionCitation"]["properties"]["source_ref"]["enum"],
        )
        models = assignment()
        with self.assertRaises(Captured):
            PlanExpanderAdapter(capture, RuleBasedTaskAssigner(models, models, models), **options).expand(
                candidate=skeleton(goal, snapshot), goal=goal, state=snapshot, project_map=project_map)
        expansion_schema = strict_json_output_schema(capture.request.output_schema)
        expansion_catalog = capture.request.payload["inspection_source_catalog"]
        self.assertEqual("payload.skeleton", expansion_catalog["artifact:skeleton"])
        self.assertEqual("output.plan", expansion_catalog["artifact:plan_draft"])
        self.assertNotIn("artifact:plan_contract", expansion_catalog)
        self.assertEqual(
            sorted(expansion_catalog),
            expansion_schema["$defs"]["InspectionCitation"]["properties"]["source_ref"]["enum"],
        )
        self.assertEqual(["plan", "inspection"], list(expansion_schema["properties"]))
        expansion_inspection = expansion_schema["$defs"]["PlanExpansionInspection"]
        self.assertEqual(0, expansion_inspection["properties"]["finding_links"]["maxItems"])
        self.assertIn("작성자(writer) 모드", capture.request.instructions)
        self.assertIn("plan 전체를 먼저 확정", capture.request.instructions)
        self.assertIn("claim:// 같은 별도 ID 체계를 만들지 않는다", capture.request.instructions)
        self.assertIn("/dependencies/...", PLAN_EXPANSION_INSPECTION_INSTRUCTIONS)
        for entry in project_map.entries:
            if entry.kind.value in {"reference", "instruction"}:
                ref = f"project:{entry.entry_id}"
                projected = capture.request.payload["inspection_source_catalog"][ref]
                self.assertEqual(review_catalog[ref], projected)
                self.assertEqual(ref, projected["source_ref"])
                self.assertEqual("/content", projected["selector"])
                self.assertEqual(entry.content_digest, projected["content_digest"])
                self.assertTrue(projected["content"])
        changed = dict(prompts)
        changed["GoalNormalizerAdapter"] += "\n실제 지침 변경"
        self.assertNotEqual(sha256_digest(changed), contract.prompt_digest)
        for name in prompts["shared_instructions"]:
            module = "goal" if name == "GOAL_INTERPRETATION_INSTRUCTIONS" else "planner_roles"
            with self.subTest(shared_instruction=name), patch(f"flowmarshal.engine.{module}.{name}", "변경된 지침"):
                self.assertNotEqual(_planning_contract(ROOT, catalog, qualification_inventory(),
                                                     default_role_configuration(ROOT), POLICIES).prompt_digest,
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
