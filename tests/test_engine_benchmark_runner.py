from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.benchmark import (
    BenchmarkLifecycleObservation,
    BenchmarkTaskLifecycleObservation,
    benchmark_cell,
    collect_lifecycle_observation,
    _legacy_hard_timeout_contract,
    _legacy_process,
    neutral_input,
    receipt_cost,
    run_benchmark,
)
from flowmarshal.engine.evaluation import EvaluationRunStatus
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.evaluation_budget import initialize_cell_budget, register_and_attach_goal
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.domain import BudgetStage
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.qualification import PlanningScenarioCatalog, QualificationRunError
from flowmarshal.engine.runtime import FakeCodexRuntime
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import RoleCallRequest
from flowmarshal.engine.smoke import run_synthetic_lifecycle
from tests.test_engine_qualification import qualification_inventory
from tests.engine_helpers import goal, profile


ROOT = Path(__file__).resolve().parents[1]
POLICIES = EvaluationPolicies(
    budget=GoalBudgetPolicy(total_tokens=100_000, call_reservation_tokens=1_000),
    role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
)


def runtime_inventory():
    return qualification_inventory().model_copy(update={
        "executable_digest": sha256_bytes(Path(sys.executable).read_bytes()),
    })


class ContextRuntime(FakeCodexRuntime):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class BenchmarkRunnerTests(unittest.TestCase):
    def test_legacy_parent_timeout_terminates_tree_and_writes_observation(self):
        timeout_contract = _legacy_hard_timeout_contract(POLICIES)

        class TimedOutProcess:
            pid = 4242
            returncode = None
            calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(["legacy"], timeout)
                return "", ""

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

        process = TimedOutProcess()
        popen_options = {}

        def start_process(*_args, **kwargs):
            popen_options.update(kwargs)
            return process

        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "legacy-state"
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            project_id = "project_" + "9" * 32
            budget_profile = profile(project_id)
            budget_goal = goal(project_id, budget_profile.definition_digest, read_only=True)
            service, manager = initialize_cell_budget(
                state_root=state_root / "engine-budget",
                workspace=workspace,
                project_id=project_id,
                profile=budget_profile,
                policies=POLICIES,
            )
            register_and_attach_goal(service, manager, budget_goal)
            request = RoleCallRequest(
                role="purpose_resolver", instructions="test", payload={}, output_schema={},
                model="test-model", effort="medium", inventory_digest="sha256:" + "1" * 64,
                cwd=str(workspace), timeout_seconds=30,
                timeout_policy_digest=POLICIES.role_timeouts.policy_digest,
            )
            manager.reserve(
                project_id=project_id, goal_id=budget_goal.goal_id,
                goal_digest=budget_goal.definition_digest, call_key="timeout-call",
                role=request.role, request=request.model_dump(mode="json"),
                stage=BudgetStage.GOAL_NORMALIZATION,
            )
            output = Path(temporary) / "raw.json"
            request_path = Path(temporary) / "request.json"
            request_path.write_text(json.dumps({"state_root": str(state_root)}), encoding="utf-8")
            with patch("flowmarshal.engine.benchmark.subprocess.Popen", side_effect=start_process), patch(
                "flowmarshal.engine.benchmark.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="terminated", stderr=""),
            ):
                with self.assertRaisesRegex(QualificationRunError, "LEGACY_PROCESS_TIMEOUT"):
                    _legacy_process(
                        ROOT,
                        ["--request-file", str(request_path), "--output", str(output)],
                        timeout_contract=timeout_contract,
                    )
            evidence = json.loads(
                output.with_name("raw-process-timeout.json").read_text(encoding="utf-8")
            )
        self.assertEqual(timeout_contract, evidence["timeout_contract"])
        self.assertTrue(evidence["termination_observed"])
        self.assertEqual("reserved", evidence["provider_calls_after_termination"][0]["status"])
        self.assertIn("creationflags", popen_options)
        self.assertIn("startupinfo", popen_options)

    def test_live_benchmark_rejects_legacy_cell_without_budget_evidence(self):
        cell_calls: list[str] = []

        def describe(_root, arguments, *, timeout_contract):
            self.assertEqual(POLICIES.role_timeouts.policy_digest,
                             timeout_contract["role_timeout_policy_digest"])
            output = Path(arguments[arguments.index("--output") + 1])
            if "--describe" not in arguments:
                output.write_text(json.dumps({
                    "disposition": "failed", "selected_candidate_id": None,
                    "candidate_records": [], "receipts": [],
                    "latency_ms_to_first_feasible": None,
                    "latency_ms_to_disposition": 1,
                    "neutral_input_digest": "sha256:" + "0" * 64,
                }), encoding="utf-8")
                return
            output.write_text(json.dumps({
                "prompt_digest": "sha256:" + "1" * 64,
                "schema_digest": "sha256:" + "2" * 64,
                "role_map": {},
            }), encoding="utf-8")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "flowmarshal.engine.benchmark._preflight", return_value=()
        ), patch(
            "flowmarshal.engine.benchmark.CodexAppServerRuntime",
            side_effect=lambda **_: ContextRuntime(runtime_inventory()),
        ), patch(
            "flowmarshal.engine.benchmark._resolve_codex_executable",
            return_value=Path(sys.executable),
        ), patch(
            "flowmarshal.engine.benchmark._legacy_process", side_effect=describe
        ), patch(
            "flowmarshal.engine.benchmark._planning_cell",
            side_effect=lambda **_: cell_calls.append("engine"),
        ), patch(
            "flowmarshal.engine.benchmark.random.Random.shuffle",
            side_effect=lambda _items: None,
        ):
            _root, report = run_benchmark(
                root=ROOT,
                run_root=Path(temporary),
                evaluation_policies=POLICIES,
            )
        self.assertEqual(EvaluationRunStatus.FAILED, report.status)
        self.assertEqual(0, report.completed_cell_count)
        self.assertEqual([], cell_calls)
        self.assertTrue(any("LEGACY_BUDGET_EVIDENCE_MISSING" in item for item in report.failures))

    def test_missing_usage_is_not_zero_and_cached_input_is_removed(self):
        with self.assertRaises(QualificationRunError):
            receipt_cost({"usage_available": False}, "skeleton_engine")
        with self.assertRaises(QualificationRunError):
            receipt_cost({"usage": []}, "r31_baseline")
        self.assertEqual((25, 12), receipt_cost({"usage": [
            {"name": "input_tokens", "value": 50}, {"name": "cached_input_tokens", "value": 25},
            {"name": "output_tokens", "value": 12},
        ]}, "r31_baseline"))

    def test_neutral_input_binds_actual_files_and_no_expected_answer(self):
        scenario = PlanningScenarioCatalog.load(ROOT / "tests/fixtures/engine/planning-scenarios.json").scenarios[4]
        value = neutral_input(ROOT, scenario)
        self.assertEqual(scenario.source_request, value["source_request"])
        self.assertIn("app.py", {item["path"] for item in value["files"]})
        self.assertNotIn("expected_disposition", value)
        self.assertNotIn("candidate_count", value)
        self.assertEqual(sha256_digest(value), sha256_digest(neutral_input(ROOT, scenario)))

    def test_planning_only_selected_cell_does_not_invent_lifecycle_counts(self):
        selected = "sha256:" + "7" * 64
        raw = {
            "receipts": [{
                "role": "plan_expander",
                "usage_available": True,
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 4,
            }],
            "selected": True,
            "passed": True,
            "blocking_questions": [],
            "selected_activation_digest": selected,
            "planning_outcome": {"plan_evaluations": [{"plan": {}}]},
            "neutral_input_digest": "sha256:" + "8" * 64,
            "latency_ms_to_first_feasible": 10,
            "latency_ms_to_disposition": 12,
        }
        scenario = SimpleNamespace(
            scenario_id="selected",
            scenario_digest="sha256:" + "9" * 64,
            path_kind="single_path",
            expected_disposition="selected",
        )
        with patch(
            "flowmarshal.engine.benchmark.candidate_output_costs",
            return_value=(4, 0),
        ):
            result = benchmark_cell(
                scenario, 17, "skeleton_engine", "sha256:" + "a" * 64, raw
            )
        self.assertIsNone(result.lifecycle_evidence_digest)
        self.assertIsNone(result.detailed_task_count)
        self.assertIsNone(result.unexecuted_detailed_task_count)
        self.assertEqual(4, result.candidate_output_tokens)

    def test_lifecycle_observation_rejects_missing_state_revision(self):
        task = BenchmarkTaskLifecycleObservation(
            task_id="task_" + "1" * 32,
            execution_spec_revision_id="execution_spec_" + "2" * 32,
            execution_spec_digest="sha256:" + "3" * 64,
            attempt_id="attempt_" + "4" * 32,
            runtime_receipt_digest="sha256:" + "5" * 64,
            validation_result_digests=("sha256:" + "6" * 64,),
        )
        values = {
            "project_id": "project_" + "7" * 32,
            "plan_revision_id": "plan_revision_" + "8" * 32,
            "plan_activation_digest": "sha256:" + "9" * 64,
            "model_lock_digest": "sha256:" + "a" * 64,
            "neutral_input_digest": "sha256:" + "b" * 64,
            "tasks": (task,),
            "integration_validation_result_digests": ("sha256:" + "c" * 64,),
            "state_before_digest": "sha256:" + "d" * 64,
            "state_after_digest": "sha256:" + "d" * 64,
            "state_reobservation_event_digest": "sha256:" + "e" * 64,
            "goal_verdict_digest": "sha256:" + "f" * 64,
            "history_head_digest": "sha256:" + "0" * 64,
        }
        with self.assertRaisesRegex(ValueError, "다른 snapshot revision"):
            BenchmarkLifecycleObservation(**values)

    def test_lifecycle_collector_requires_real_completed_ledger_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("합성 지침", encoding="utf-8")
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
            database = base / "state.sqlite3"
            artifacts = base / "artifacts"
            status = run_synthetic_lifecycle(
                project_root=project,
                database_path=database,
                artifact_root=artifacts,
            )
            project_id = status["project"]["id"]
            ledger = SQLiteEngineLedger(database, artifact_root=artifacts)
            with ledger.read() as connection:
                plan_json = connection.execute(
                    "SELECT p.payload_json FROM plan_revisions p JOIN plan_activations a "
                    "ON a.plan_revision_id = p.id WHERE a.project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            from flowmarshal.engine.domain import PlanContractRevision
            activation_digest = PlanContractRevision.model_validate_json(plan_json).activation_digest
            observation = collect_lifecycle_observation(
                ledger,
                project_id=project_id,
                plan_activation_digest=activation_digest,
                model_lock_digest="sha256:" + "a" * 64,
                neutral_input_digest="sha256:" + "b" * 64,
            )
        self.assertEqual("completed", observation.run_state)
        self.assertTrue(observation.history_valid)
        self.assertNotEqual(observation.state_before_digest, observation.state_after_digest)
        self.assertGreaterEqual(len(observation.tasks), 1)
        raw = {
            "receipts": [{
                "role": "plan_expander",
                "usage_available": True,
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 4,
            }],
            "selected": True,
            "passed": True,
            "blocking_questions": [],
            "selected_activation_digest": activation_digest,
            "planning_outcome": {"plan_evaluations": [{"plan": {}}]},
            "neutral_input_digest": "sha256:" + "b" * 64,
            "latency_ms_to_first_feasible": 10,
            "latency_ms_to_disposition": 12,
            "lifecycle_observation": observation.model_dump(mode="json"),
        }
        scenario = SimpleNamespace(
            scenario_id="lifecycle",
            scenario_digest="sha256:" + "9" * 64,
            path_kind="single_path",
            expected_disposition="selected",
        )
        with patch(
            "flowmarshal.engine.benchmark.candidate_output_costs",
            return_value=(4, 0),
        ):
            cell = benchmark_cell(
                scenario, 17, "skeleton_engine", "sha256:" + "a" * 64, raw
            )
        self.assertEqual(observation.observation_digest, cell.lifecycle_evidence_digest)
        self.assertEqual(len(observation.tasks), cell.detailed_task_count)
        self.assertEqual(0, cell.unexecuted_detailed_task_count)

    def test_completed_matrix_is_immutable_and_resume_does_not_call_models(self):
        calls = []
        safety_captures = []
        performance_assessments = []
        catalog = PlanningScenarioCatalog.load(ROOT / "tests/fixtures/engine/planning-scenarios.json")
        scenarios = {item.source_request: item for item in catalog.scenarios}

        def legacy(_root, arguments, *, timeout_contract):
            self.assertGreater(timeout_contract["timeout_seconds"], 0)
            output = Path(arguments[arguments.index("--output") + 1])
            if "--describe" in arguments:
                output.write_text(json.dumps({"prompt_digest": "sha256:" + "1" * 64,
                                              "schema_digest": "sha256:" + "2" * 64, "role_map": {}}), encoding="utf-8")
                return
            request = json.loads(Path(arguments[arguments.index("--request-file") + 1]).read_text(encoding="utf-8"))
            binding = request["budget_cell_binding"]
            self.assertEqual(sha256_digest(request["roles"]), binding["role_configuration_digest"])
            self.assertEqual(runtime_inventory().inventory_digest, binding["engine_inventory_digest"])
            self.assertEqual(request["legacy_inventory_digest"], binding["legacy_inventory_digest"])
            self.assertEqual(
                sha256_bytes(Path(request["codex_bin"]).read_bytes()),
                binding["codex_executable_digest"],
            )
            self.assertEqual(timeout_contract, binding["parent_hard_timeout_contract"])
            scenario = scenarios[request["neutral_input"]["source_request"]]
            calls.append((scenario.scenario_id, "legacy"))
            output.write_text(json.dumps({
                "disposition": "blocked" if scenario.expected_disposition == "blocked" else "failed",
                "selected_candidate_id": None, "candidate_records": [], "latency_ms_to_first_feasible": None,
                "latency_ms_to_disposition": 100, "neutral_input_digest": request["neutral_input_digest"],
                "receipts": [{"call_id": "fixture", "usage": [
                    {"name": "input_tokens", "value": 100}, {"name": "cached_input_tokens", "value": 0},
                    {"name": "output_tokens", "value": 10}]}],
            }), encoding="utf-8")

        def engine(**arguments):
            from flowmarshal.engine.domain import utc_now
            from flowmarshal.engine.roles import RoleCallReceipt
            scenario = arguments["scenario"]
            calls.append((scenario.scenario_id, "engine"))
            receipt = RoleCallReceipt(
                call_id="fixture", role="goal_normalizer", status="succeeded", model="fixture", effort="medium",
                inventory_digest="sha256:" + "1" * 64, permission_profile=":danger-full-access", approval_policy="never",
                input_digest="sha256:" + "2" * 64, output_schema_digest="sha256:" + "3" * 64,
                input_tokens=50, output_tokens=10, usage_available=True, latency_ms=50, recorded_at=utc_now(),
            )
            return {"selected": False, "passed": scenario.expected_disposition == "blocked",
                    "blocking_questions": ["추가 자료 필요"] if scenario.expected_disposition == "blocked" else [],
                    "latency_ms_to_first_feasible": None, "latency_ms_to_disposition": 50}, (receipt,)

        def capture_safety(*, work_root, implementation, raw, policies):
            # 이 테스트는 실제 provider/SQLite 관측이 없는 runner matrix mock이다.
            # collector 자체의 원장·trace 검증은 test_engine_benchmark_safety가 담당한다.
            self.assertIn(implementation, {"r31_baseline", "skeleton_engine"})
            self.assertIs(policies, POLICIES)
            self.assertIn("receipts", raw)
            safety_captures.append((work_root, implementation))
            return {"format": "mock-safety-checkpoint-v1", "implementation": implementation}

        def build_assessment(**arguments):
            from flowmarshal.engine.performance import (
                PerformanceQualificationReport,
                PerformanceThresholdPolicy,
            )

            self.assertEqual("planning", arguments["assessment_stage"])
            performance_assessments.append(arguments["run_root"])
            # matrix resume 불변성만 검증하는 mock이므로 안전성 통과를 위조하지 않는다.
            # 실제 collector의 6개 원장/trace 회귀는 별도 테스트에서 실행한다.
            policy = PerformanceThresholdPolicy.load(
                ROOT / "config" / "pre-1.0-performance-thresholds.json"
            )
            return PerformanceQualificationReport(
                schema_version="4.0",
                assessment_stage="planning",
                contract_digest="sha256:" + "0" * 64,
                source_manifest_digest="sha256:" + "1" * 64,
                scenario_set_digest="sha256:" + "2" * 64,
                expected_manifest_digest="sha256:" + "3" * 64,
                threshold_policy=policy,
                performance_threshold_policy_digest=policy.policy_digest,
                observed_cell_count=0,
                observed_pair_count=0,
                observed_selected_pair_count=0,
                observed_blocked_pair_count=0,
                cell_digests=(),
                safety_observation_digests=(),
                source_evidence_digests=(),
                scope_results=(),
                pair_results=(),
                overall_mean_reduction=None,
                multi_path_median_reduction=None,
                worst_single_path_regression=None,
                worst_cell_token_regression=None,
                time_to_first_feasible_median_improvement=None,
                all_pair_disposition_median_improvement=None,
                unexecuted_detail_ratio=None,
                discarded_candidate_output_ratio=None,
                manifest_complete=False,
                functional_safety_passed=False,
                minimum_performance_floor_passed=False,
                release_floor_passed=False,
                cutover_eligible=False,
                optimization_targets_passed=False,
                optimization_followups_required=False,
                optimization_misses=(),
                planning_assessment_passed=False,
                failures=(),
                not_observed=("PERFORMANCE_MOCK_NOT_OBSERVED",),
            ), {"format": "mock-performance-assessment-v1"}

        def write_assessment(run_root, _report, _bundle):
            return run_root / "performance-assessments" / "mock"

        with tempfile.TemporaryDirectory() as temporary, patch(
            "flowmarshal.engine.benchmark._preflight", return_value=()
        ), patch("flowmarshal.engine.benchmark.CodexAppServerRuntime", side_effect=lambda **_: ContextRuntime(runtime_inventory())), patch(
            "flowmarshal.engine.benchmark._resolve_codex_executable", return_value=Path(sys.executable)
        ), patch(
            "flowmarshal.engine.benchmark._legacy_process", side_effect=legacy
        ), patch("flowmarshal.engine.benchmark._planning_cell", side_effect=engine), patch(
            "flowmarshal.engine.benchmark._verify_legacy_budget_evidence"
        ), patch(
            "flowmarshal.engine.benchmark_safety.capture_planning_safety_checkpoint",
            side_effect=capture_safety,
        ), patch(
            "flowmarshal.engine.performance_assessment.build_performance_assessment",
            side_effect=build_assessment,
        ), patch(
            "flowmarshal.engine.performance_assessment.write_performance_assessment",
            side_effect=write_assessment,
        ):
            run_root, report = run_benchmark(
                root=ROOT,
                run_root=Path(temporary),
                evaluation_policies=POLICIES,
            )
            self.assertEqual(EvaluationRunStatus.COMPLETED, report.status, report.failures)
            self.assertEqual(36, report.completed_cell_count)
            self.assertFalse(report.passed)
            self.assertEqual(36, len(calls))
            self.assertEqual(36, len(safety_captures))
            self.assertIsNotNone(report.performance_qualification)
            self.assertFalse(report.performance_qualification.planning_assessment_passed)
            self.assertEqual(1, len(performance_assessments))
            checkpoints = {str(path): path.read_bytes() for path in run_root.glob("cells/**/*.json")}
            _, resumed = run_benchmark(
                root=ROOT,
                run_root=run_root,
                evaluation_policies=POLICIES,
            )
            self.assertEqual(36, resumed.completed_cell_count)
            self.assertEqual(36, len(calls))
            self.assertEqual(36, len(safety_captures))
            self.assertEqual(2, len(performance_assessments))
            self.assertEqual(checkpoints, {str(path): path.read_bytes() for path in run_root.glob("cells/**/*.json")})


if __name__ == "__main__":
    unittest.main()
