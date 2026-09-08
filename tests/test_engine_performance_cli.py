from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.benchmark import MEASUREMENT_RULES
from flowmarshal.engine.eval_cli import (
    _cutover,
    _observe_benchmark_lifecycle,
    main,
)
from flowmarshal.engine.evaluation import EvaluationContract, EvaluationScope
from flowmarshal.engine.evaluation_budget import (
    EvaluationPolicies,
    metadata_with_policies,
)
from flowmarshal.engine.performance import (
    PerformanceQualificationReport,
    PerformanceThresholdPolicy,
)
from flowmarshal.engine.performance_assessment import (
    load_bound_performance_policy,
    performance_threshold_digest,
)
from flowmarshal.engine.qualification import QualificationRunError
from flowmarshal.engine.role_execution import RoleTimeoutPolicy


ROOT = Path(__file__).resolve().parents[1]


def _digest(character: str) -> str:
    return "sha256:" + character * 64


class PerformanceCliTests(unittest.TestCase):
    def setUp(self):
        self.policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )
        self.policy = PerformanceThresholdPolicy.load(
            ROOT / "config" / "pre-1.0-performance-thresholds.json"
        )

    def _report(self, *, stage: str = "final", release_floor_passed: bool = True):
        """CLI 분기 전용 완결 모양의 report; 실제 performance 산출물을 주장하지 않는다."""
        return PerformanceQualificationReport.model_construct(
            schema_version="4.0",
            assessment_stage=stage,
            contract_digest=_digest("0"),
            source_manifest_digest=_digest("1"),
            scenario_set_digest=_digest("2"),
            expected_manifest_digest=_digest("3"),
            threshold_policy=self.policy,
            performance_threshold_policy_digest=self.policy.policy_digest,
            expected_cell_count=36,
            observed_cell_count=36,
            expected_pair_count=18,
            observed_pair_count=18,
            expected_selected_pair_count=12,
            observed_selected_pair_count=12,
            expected_blocked_pair_count=6,
            observed_blocked_pair_count=6,
            cell_digests=(),
            safety_observation_digests=(),
            source_evidence_digests=(),
            scope_results=(),
            pair_results=(),
            overall_mean_reduction=0.10,
            multi_path_median_reduction=0.30,
            worst_single_path_regression=0.05,
            worst_cell_token_regression=0.05,
            time_to_first_feasible_median_improvement=0.20,
            all_pair_disposition_median_improvement=0.20,
            unexecuted_detail_ratio=0.10,
            discarded_candidate_output_ratio=0.25,
            manifest_complete=True,
            functional_safety_passed=True,
            minimum_performance_floor_passed=release_floor_passed,
            release_floor_passed=release_floor_passed,
            optimization_targets_passed=False,
            optimization_followups_required=True,
            optimization_misses=("overall_mean_reduction",),
            planning_assessment_passed=release_floor_passed,
            cutover_eligible=False,
            failures=(),
            not_observed=(),
        )

    @staticmethod
    def _arguments(root: Path, run_root: Path, report_path: Path | None = None):
        return SimpleNamespace(
            project_root=str(root),
            run_root=str(run_root),
            scope_report=[],
            benchmark_report=None if report_path is None else str(report_path),
            output=None,
        )

    def _metadata(self, root: Path, run_root: Path):
        run_root.mkdir(parents=True)
        return {
            "scope": "benchmark",
            "project_root": str(root),
            "inspection_provider_contract": "plan-inspection-v1",
            "performance_threshold_policy": self.policy.model_dump(mode="json"),
            "performance_threshold_policy_digest": self.policy.policy_digest,
        }

    def _write_cutover_assessment(
        self, root: Path, run_root: Path, report: PerformanceQualificationReport,
    ) -> tuple[Path, dict[str, object]]:
        metadata = self._metadata(root, run_root)
        (run_root / "run-metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        bundle: dict[str, object] = {
            "format": "flowmarshal-performance-assessment-v1",
            "assessment_stage": "final",
            "source_run_root": str(run_root),
            "report_digest": sha256_digest(report),
        }
        directory = run_root / "lifecycle-assessments" / sha256_digest(bundle)[7:]
        directory.mkdir(parents=True)
        report_path = directory / "performance-qualification-report.json"
        report_path.write_text(json.dumps(report.model_dump(mode="json")), encoding="utf-8")
        (directory / "assessment.json").write_text(json.dumps(bundle), encoding="utf-8")
        return report_path, bundle

    def test_v4_observe_returns_zero_for_release_floor_pass_even_when_optimization_follows_up(self):
        report = self._report(release_floor_passed=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            run_root = Path(temporary) / "run"
            metadata = self._metadata(root, run_root)
            (run_root / "run-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            destination = run_root / "lifecycle-assessments" / "mock"
            calls: list[str] = []
            with patch("flowmarshal.engine.eval_cli._bound_scope_reports", return_value=()), patch(
                "flowmarshal.engine.performance_assessment.build_performance_assessment",
                side_effect=lambda **_kwargs: (calls.append("build") or (report, {"report": "mock"})),
            ), patch(
                "flowmarshal.engine.performance_assessment.write_performance_assessment",
                side_effect=lambda *_args: destination,
            ), patch(
                "flowmarshal.engine.runtime.CodexAppServerRuntime",
                side_effect=AssertionError("재관측 CLI가 provider를 호출하면 안 됩니다."),
            ), contextlib.redirect_stdout(io.StringIO()) as output:
                result = _observe_benchmark_lifecycle(self._arguments(root, run_root))
        self.assertEqual(0, result)
        self.assertEqual(["build"], calls)
        emitted = json.loads(output.getvalue())
        self.assertTrue(emitted["release_floor_passed"])
        self.assertFalse(emitted["optimization_targets_passed"])
        self.assertTrue(emitted["optimization_followups_required"])

    def test_cutover_rejects_v3_and_nonfinal_v4_before_recalculation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            run_root = Path(temporary) / "run"
            v3 = Path(temporary) / "v3.json"
            v3.write_text(json.dumps({"schema_version": "3.0"}), encoding="utf-8")
            with self.assertRaisesRegex(QualificationRunError, "PERFORMANCE_V4_REPORT_REQUIRED"):
                _cutover(self._arguments(root, run_root, v3))

            planning = self._report(stage="planning", release_floor_passed=False)
            v4 = Path(temporary) / "v4.json"
            v4.write_text(json.dumps({"schema_version": "4.0"}), encoding="utf-8")
            with patch.object(
                PerformanceQualificationReport,
                "model_validate_json",
                return_value=planning,
            ), self.assertRaisesRegex(QualificationRunError, "PERFORMANCE_FINAL_ASSESSMENT_REQUIRED"):
                _cutover(self._arguments(root, run_root, v4))

    def test_cutover_raw_recalculation_mismatch_returns_exit_two(self):
        report = self._report()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            run_root = Path(temporary) / "run"
            report_path, bundle = self._write_cutover_assessment(root, run_root, report)
            changed = dict(bundle, changed=True)
            with patch("flowmarshal.engine.eval_cli._bound_scope_reports", return_value=()), patch.object(
                PerformanceQualificationReport,
                "model_validate_json",
                return_value=report,
            ), patch(
                "flowmarshal.engine.performance_assessment.build_performance_assessment",
                return_value=(report, changed),
            ), contextlib.redirect_stdout(io.StringIO()) as output:
                result = main([
                    "cutover", "--project-root", str(root), "--scope-report", str(report_path),
                    "--benchmark-report", str(report_path),
                ])
        self.assertEqual(2, result)
        self.assertIn("PERFORMANCE_REPORT_RECALCULATION_MISMATCH", output.getvalue())

    def test_policy_metadata_roundtrip_and_digest_preserving_tamper_are_rejected(self):
        contract = EvaluationContract(
            model_lock_format="flowmarshal-model-lock-v2",
            scope=EvaluationScope.FULL_PLANNING_PIPELINE,
            fixture_digests=(_digest("4"),),
            scenario_set_digest=_digest("5"),
            order_seeds=(1,),
            expected_cell_count=1,
            role_configuration_digest=_digest("6"),
            source_manifest_digest=_digest("7"),
            rules_digest=sha256_digest(MEASUREMENT_RULES),
            threshold_digest=performance_threshold_digest(self.policies, self.policy),
            taxonomy_digest=_digest("9"),
            prompt_digest=_digest("a"),
            output_schema_digest=_digest("b"),
            model_lock_digest=_digest("c"),
        )
        metadata = metadata_with_policies({
            "performance_threshold_policy": self.policy.model_dump(mode="json"),
            "performance_threshold_policy_digest": self.policy.policy_digest,
        }, self.policies)
        self.assertEqual(self.policy, load_bound_performance_policy(metadata, contract, ROOT))
        tampered = dict(metadata)
        policy_body = dict(tampered["performance_threshold_policy"])
        policy_body["minimum_overall_mean_reduction"] = "0.99"
        tampered["performance_threshold_policy"] = policy_body
        body = dict(tampered)
        body.pop("metadata_digest")
        tampered["metadata_digest"] = sha256_digest(body)
        with self.assertRaises(ValueError):
            load_bound_performance_policy(tampered, contract, ROOT)


if __name__ == "__main__":
    unittest.main()
