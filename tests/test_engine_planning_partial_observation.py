import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.domain import GoalPreparationBinding, GoalReviewRatingsBinding, new_id
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.qualification import (
    PlanningScenario,
    PlanningScenarioCatalog,
    PlanningPartialFeasibleObservation,
    _planning_cell,
    default_role_configuration,
    run_full_planning_pipeline,
)
from flowmarshal.engine.planning import (
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import RoleCallReceipt, StructuredRoleError
from flowmarshal.engine.runtime import FakeCodexRuntime
from tests.engine_helpers import clean_review, goal, inventory as helper_inventory, plan, skeleton


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "engine" / "live-smoke-project"
ACTIVATION_ONE = "sha256:" + "1" * 64
ACTIVATION_TWO = "sha256:" + "2" * 64


def _inventory(roles) -> ModelInventory:
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
        source="partial-feasible-observation-test",
        models=tuple(
            ModelCapability(model=model, supported_efforts=tuple(sorted(efforts)))
            for model, efforts in sorted(grouped.items())
        ),
    )


def _schema_failed_receipt() -> RoleCallReceipt:
    return RoleCallReceipt(
        call_id="schema-call",
        role="plan_expander",
        status="schema_failed",
        model="test-model",
        effort="high",
        inventory_digest="sha256:" + "3" * 64,
        permission_profile=":danger-full-access",
        approval_policy="never",
        thread_id="thread-schema",
        turn_ids=("turn-schema",),
        input_digest="sha256:" + "4" * 64,
        output_digest=None,
        output_schema_digest="sha256:" + "5" * 64,
        input_tokens=11,
        cached_input_tokens=0,
        output_tokens=7,
        reasoning_tokens=0,
        usage_available=True,
        latency_ms=9,
        recorded_at=datetime.now(timezone.utc),
    )


class _FakeRuntimeContext:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.runtime = FakeCodexRuntime(_inventory(default_role_configuration(ROOT)))

    def __enter__(self):
        return self.runtime

    def __exit__(self, *args) -> None:
        self.runtime.close()


class PlanningPartialObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.roles = default_role_configuration(ROOT)
        self.inventory = _inventory(self.roles)
        self.policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=1_000, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )
        self.scenario = PlanningScenario(
            scenario_id="S01-single-bugfix",
            path_kind="single_path",
            source_request="계획을 만든다.",
            expected_disposition="selected",
            candidate_count=1,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_planning_cell_reports_only_first_admissible_observation_before_schema_failure(self) -> None:
        """실제 cell callback은 첫 admissible Plan만 caller collector에 보낸다."""
        observed: list[PlanningPartialFeasibleObservation] = []

        def prepared_goal(_pipeline, *, project_id, profile, goal_id, **_kwargs):
            contract = goal(project_id, profile.definition_digest).model_copy(update={
                "goal_id": goal_id,
                "preparation_binding": GoalPreparationBinding(
                    normalization_proposal_digest=sha256_digest("proposal"),
                    reviewer_submission_digest=sha256_digest("review"),
                    reviewer_role="goal_reviewer",
                    ratings=GoalReviewRatingsBinding(
                        goal_fit=4, grounding=4, engineering=4,
                        verification=4, execution_safety=4,
                    ),
                ),
            })
            return SimpleNamespace(
                goal_contract=contract,
                proposal=SimpleNamespace(unresolved_questions=()),
                model_dump=lambda **_ignored: {"prepared": True},
            )

        def schema_failure(*, feasible_observer, **_kwargs):
            feasible_observer(SimpleNamespace(plan=SimpleNamespace(activation_digest=ACTIVATION_ONE)))
            feasible_observer(SimpleNamespace(plan=SimpleNamespace(activation_digest=ACTIVATION_TWO)))
            raise StructuredRoleError("schema failed after feasible", receipt=_schema_failed_receipt())

        with (
            patch("flowmarshal.engine.qualification.GoalPreparationPipeline.prepare", new=prepared_goal),
            patch("flowmarshal.engine.qualification.SkeletonFirstPlanner.search", side_effect=schema_failure),
            self.assertRaisesRegex(StructuredRoleError, "schema failed after feasible"),
        ):
            _planning_cell(
                scenario=self.scenario,
                seed=17,
                fixture_root=FIXTURE_ROOT,
                runtime=FakeCodexRuntime(self.inventory),
                inventory=self.inventory,
                roles=self.roles,
                work_root=Path(self.temporary.name) / "cell",
                partial_feasible_observer=observed.append,
            )

        self.assertEqual(1, len(observed))
        self.assertTrue(observed[0].observed)
        self.assertGreaterEqual(observed[0].latency_ms or 0, 1)
        self.assertEqual(ACTIVATION_ONE, observed[0].plan_activation_digest)

    def _run_schema_failure(self, *, observe: bool):
        catalog = PlanningScenarioCatalog(scenarios=(self.scenario,))
        receipt = _schema_failed_receipt()
        captured = []

        def failing_cell(*, partial_feasible_observer=None, **kwargs):
            captured.append(kwargs)
            if observe:
                assert partial_feasible_observer is not None
                partial_feasible_observer(PlanningPartialFeasibleObservation(
                    observed=True, latency_ms=37, plan_activation_digest=ACTIVATION_ONE,
                ))
            raise StructuredRoleError("plan schema failed", receipt=receipt)

        destination = Path(self.temporary.name) / ("observed" if observe else "unobserved")
        with (
            patch("flowmarshal.engine.qualification._preflight", return_value=()),
            patch("flowmarshal.engine.qualification.CodexAppServerRuntime", _FakeRuntimeContext),
            patch("flowmarshal.engine.qualification.PlanningScenarioCatalog.load", return_value=catalog),
            patch("flowmarshal.engine.qualification.ORDER_SEEDS", (17,)),
            patch("flowmarshal.engine.qualification._planning_cell", side_effect=failing_cell),
        ):
            path, report = run_full_planning_pipeline(
                root=ROOT,
                run_root=destination,
                role_configuration=self.roles,
                evaluation_policies=self.policies,
            )
        self.assertEqual(destination.resolve(), path)
        self.assertFalse(report.passed)
        self.assertEqual(1, len(captured))
        checkpoint = next((path / "cells").rglob("*.json"))
        return receipt, __import__("json").loads(checkpoint.read_text(encoding="utf-8"))

    def test_pipeline_preserves_observed_feasible_in_strict_schema_failure_checkpoint(self) -> None:
        receipt, checkpoint = self._run_schema_failure(observe=True)
        cell = checkpoint["raw_structured_assessment"]
        self.assertFalse(cell["passed"])
        self.assertFalse(cell["schema_valid"])
        self.assertFalse(cell["selected"])
        self.assertNotIn("planning_outcome", cell)
        self.assertNotIn("selected_activation_digest", cell)
        self.assertEqual(
            {"observed": True, "latency_ms": 37, "plan_activation_digest": ACTIVATION_ONE},
            cell["partial_feasible_observation"],
        )
        self.assertEqual([receipt.model_dump(mode="json")], checkpoint["runner_receipts"])

    def test_pipeline_does_not_infer_feasible_observation_before_schema_failure(self) -> None:
        _receipt, checkpoint = self._run_schema_failure(observe=False)
        cell = checkpoint["raw_structured_assessment"]
        self.assertFalse(cell["passed"])
        self.assertFalse(cell["schema_valid"])
        self.assertFalse(cell["selected"])
        self.assertEqual(
            {"observed": False, "latency_ms": None, "plan_activation_digest": None},
            cell["partial_feasible_observation"],
        )

    def _run_actual_search_schema_failure(self, *, first_plan_feasible: bool, use_v2: bool = False):
        """실제 search가 첫 Plan 관측 뒤 다음 expander 오류까지 진행하게 한다."""
        scenario = self.scenario.model_copy(update={"candidate_count": 2})
        catalog = PlanningScenarioCatalog(scenarios=(scenario,))
        receipt = _schema_failed_receipt()
        first_activation_digests: list[str] = []

        def prepared_goal(_pipeline, *, project_id, profile, goal_id, **_kwargs):
            contract = goal(project_id, profile.definition_digest).model_copy(update={
                "goal_id": goal_id,
                "preparation_binding": GoalPreparationBinding(
                    normalization_proposal_digest=sha256_digest("proposal"),
                    reviewer_submission_digest=sha256_digest("review"),
                    reviewer_role="goal_reviewer",
                    ratings=GoalReviewRatingsBinding(
                        goal_fit=4, grounding=4, engineering=4,
                        verification=4, execution_safety=4,
                    ),
                ),
            })
            return SimpleNamespace(
                goal_contract=contract,
                proposal=SimpleNamespace(unresolved_questions=()),
                model_dump=lambda **_ignored: {"prepared": True},
            )

        class Generator:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def generate(self, *, goal, state, candidate_count, **_kwargs):
                first = skeleton(goal, state)
                second = first.model_copy(update={
                    "candidate_id": new_id("candidate"),
                    "approach": first.approach.model_copy(
                        update={"strategy_family": "alternate"}
                    ),
                })
                return (first, second)[:candidate_count]

            def refine(self, **_kwargs):
                raise AssertionError("admissible 후보에서는 skeleton refine을 호출하면 안 됩니다.")

        class SkeletonReviewer:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def review(self, *, candidate, goal, state, project_map):
                return clean_review(
                    sha256_digest(candidate),
                    role="compact-skeleton-reviewer",
                    evidence_catalog=skeleton_review_evidence_catalog(
                        candidate, goal, state, project_map
                    ),
                )

        class Expander:
            def __init__(self, *_args, **_kwargs) -> None:
                self.calls = 0
                self.runner = _args[0]

            def expand(self, *, candidate, goal, state, project_map, **_kwargs):
                self.calls += 1
                if not first_plan_feasible or self.calls == 2:
                    if use_v2:
                        self.runner.receipts.append(receipt)
                    raise StructuredRoleError(
                        "second expander schema failed", receipt=receipt,
                        settled_provider_call_id="provider_call_test" if use_v2 else None,
                    )
                expanded = plan(
                    goal.definition.project_id, goal, state, project_map.revision_digest,
                    candidate, helper_inventory(),
                )[0]
                if use_v2:
                    definition = expanded.definition.model_copy(update={"planning_budget": _kwargs["planning_budget"]})
                    expanded = expanded.model_copy(update={"definition": definition,
                                                           "definition_digest": definition.definition_digest})
                first_activation_digests.append(expanded.activation_digest)
                return expanded

        class PlanReviewer:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def review(self, *, plan, goal, state, project_map, **_kwargs):
                return clean_review(
                    plan.activation_digest,
                    role="compact-plan-reviewer",
                    evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
                )

        destination = Path(self.temporary.name) / (
            "actual-feasible" if first_plan_feasible else "actual-before-feasible"
        )
        with (
            patch("flowmarshal.engine.qualification._preflight", return_value=()),
            patch("flowmarshal.engine.qualification.CodexAppServerRuntime", _FakeRuntimeContext),
            patch("flowmarshal.engine.qualification.PlanningScenarioCatalog.load", return_value=catalog),
            patch("flowmarshal.engine.qualification.ORDER_SEEDS", (17,)),
            patch("flowmarshal.engine.qualification.GoalPreparationPipeline.prepare", new=prepared_goal),
            patch("flowmarshal.engine.qualification.SkeletonGeneratorAdapter", Generator),
            patch("flowmarshal.engine.qualification.SkeletonReviewerAdapter", SkeletonReviewer),
            patch("flowmarshal.engine.qualification.PlanExpanderAdapter", Expander),
            patch("flowmarshal.engine.qualification.PlanReviewerAdapter", PlanReviewer),
        ):
            path, report = run_full_planning_pipeline(
                root=ROOT,
                run_root=destination,
                role_configuration=self.roles,
                evaluation_policies=self.policies,
                inspection_provider_contract="plan-inspection-v2" if use_v2 else "plan-inspection-v1",
            )
        self.assertFalse(report.passed)
        checkpoint = next((path / "cells").rglob("*.json"))
        return (
            receipt,
            first_activation_digests,
            __import__("json").loads(checkpoint.read_text(encoding="utf-8")),
        )

    def test_v2_keeps_selected_plan_but_records_schema_failure_as_qualification_fail(self):
        """검색 복구가 실제 checkpoint의 schema/전체 FAIL을 성공으로 바꾸지 않는다."""
        receipt, first_activation_digests, checkpoint = self._run_actual_search_schema_failure(
            first_plan_feasible=True, use_v2=True,
        )
        cell = checkpoint["raw_structured_assessment"]
        self.assertTrue(cell["selected"])
        self.assertFalse(cell["schema_valid"])
        self.assertFalse(cell["passed"])
        self.assertEqual(first_activation_digests[0], cell["selected_activation_digest"])
        self.assertGreaterEqual(cell["latency_ms_to_first_feasible"], 1)
        failures = cell["planning_outcome"]["candidate_schema_failures"]
        self.assertEqual(1, len(failures))
        self.assertEqual(receipt.model_dump(mode="json"), failures[0]["receipt"])
        self.assertEqual(receipt.model_dump(mode="json"), checkpoint["runner_receipts"][0])

    def test_actual_search_keeps_first_feasible_before_second_expander_schema_failure(self) -> None:
        """실제 planner가 첫 admissible Plan 뒤의 schema 실패를 strict checkpoint로 전달한다."""
        receipt, first_activation_digests, checkpoint = self._run_actual_search_schema_failure(
            first_plan_feasible=True
        )
        cell = checkpoint["raw_structured_assessment"]
        observation = cell["partial_feasible_observation"]
        self.assertTrue(observation["observed"])
        self.assertGreaterEqual(observation["latency_ms"], 1)
        self.assertEqual(first_activation_digests, [observation["plan_activation_digest"]])
        self.assertFalse(cell["passed"])
        self.assertFalse(cell["schema_valid"])
        self.assertFalse(cell["selected"])
        self.assertNotIn("planning_outcome", cell)
        self.assertEqual([receipt.model_dump(mode="json")], checkpoint["runner_receipts"])

    def test_actual_search_does_not_create_observation_when_first_expander_schema_fails(self) -> None:
        """실제 planner가 feasible 전 실패하면 과거 시각이나 후보로 관측을 채우지 않는다."""
        _receipt, first_activation_digests, checkpoint = self._run_actual_search_schema_failure(
            first_plan_feasible=False
        )
        self.assertEqual([], first_activation_digests)
        cell = checkpoint["raw_structured_assessment"]
        self.assertEqual(
            {"observed": False, "latency_ms": None, "plan_activation_digest": None},
            cell["partial_feasible_observation"],
        )


if __name__ == "__main__":
    unittest.main()
