from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark import neutral_input, receipt_cost, run_benchmark
from flowmarshal.engine.evaluation import EvaluationRunStatus
from flowmarshal.engine.qualification import PlanningScenarioCatalog, QualificationRunError
from flowmarshal.engine.runtime import FakeCodexRuntime
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]


class ContextRuntime(FakeCodexRuntime):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class BenchmarkRunnerTests(unittest.TestCase):
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

    def test_completed_matrix_is_immutable_and_resume_does_not_call_models(self):
        calls = []
        catalog = PlanningScenarioCatalog.load(ROOT / "tests/fixtures/engine/planning-scenarios.json")
        scenarios = {item.source_request: item for item in catalog.scenarios}

        def legacy(_root, arguments):
            output = Path(arguments[arguments.index("--output") + 1])
            if "--describe" in arguments:
                output.write_text(json.dumps({"prompt_digest": "sha256:" + "1" * 64,
                                              "schema_digest": "sha256:" + "2" * 64, "role_map": {}}), encoding="utf-8")
                return
            request = json.loads(Path(arguments[arguments.index("--request-file") + 1]).read_text(encoding="utf-8"))
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

        with tempfile.TemporaryDirectory() as temporary, patch(
            "flowmarshal.engine.benchmark._preflight", return_value=()
        ), patch("flowmarshal.engine.benchmark.CodexAppServerRuntime", side_effect=lambda **_: ContextRuntime(qualification_inventory())), patch(
            "flowmarshal.engine.benchmark._legacy_process", side_effect=legacy
        ), patch("flowmarshal.engine.benchmark._planning_cell", side_effect=engine):
            run_root, report = run_benchmark(root=ROOT, run_root=Path(temporary))
            self.assertEqual(EvaluationRunStatus.COMPLETED, report.status)
            self.assertEqual(36, report.completed_cell_count)
            self.assertFalse(report.passed)
            self.assertEqual(36, len(calls))
            checkpoints = {str(path): path.read_bytes() for path in run_root.glob("cells/**/*.json")}
            _, resumed = run_benchmark(root=ROOT, run_root=run_root)
            self.assertEqual(36, resumed.completed_cell_count)
            self.assertEqual(36, len(calls))
            self.assertEqual(checkpoints, {str(path): path.read_bytes() for path in run_root.glob("cells/**/*.json")})


if __name__ == "__main__":
    unittest.main()
