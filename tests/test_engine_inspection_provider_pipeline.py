from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark import _require_matching_full_planning_scope_report
from flowmarshal.engine.benchmark_observation import (
    BenchmarkObservationError,
    observe_benchmark_execution_checkpoint,
)
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.eval_cli import _benchmark, _bound_scope_reports, _resume, build_parser
from flowmarshal.engine.evaluation import (
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationRunStatus,
    EvaluationScope,
)
from flowmarshal.engine.evaluation_budget import EvaluationPolicies, metadata_with_policies
from flowmarshal.engine.plan_inspection_provider import (
    PLAN_INSPECTION_PROVIDER_V1,
    PLAN_INSPECTION_PROVIDER_V2,
)
from flowmarshal.engine.qualification import QualificationRunError, ScopeQualificationReport, source_manifest_digest
from flowmarshal.engine.role_execution import RoleTimeoutPolicy


ROOT = Path(__file__).resolve().parents[1]
POLICIES = EvaluationPolicies(
    budget=GoalBudgetPolicy(total_tokens=1_000_000, call_reservation_tokens=100_000),
    role_timeouts=RoleTimeoutPolicy(),
)


def _digest(char: str) -> str:
    return "sha256:" + char * 64


def _contract(scope: EvaluationScope) -> EvaluationContract:
    return EvaluationContract(
        model_lock_format="flowmarshal-model-lock-v2",
        scope=scope,
        fixture_digests=(_digest("1"),),
        scenario_set_digest=_digest("2"),
        order_seeds=(1,),
        expected_cell_count=1,
        role_configuration_digest=_digest("3"),
        source_manifest_digest=source_manifest_digest(ROOT),
        rules_digest=_digest("4"),
        threshold_digest=_digest("5"),
        taxonomy_digest=_digest("6"),
        prompt_digest=_digest("7"),
        output_schema_digest=_digest("8"),
        model_lock_digest=_digest("9"),
    )


def _report(contract: EvaluationContract) -> ScopeQualificationReport:
    return ScopeQualificationReport(
        scope=contract.scope,
        contract_digest=contract.contract_digest,
        status=EvaluationRunStatus.COMPLETED,
        passed=True,
        metrics={"cell_count": 18, "failure_count": 0},
        failures=(),
        generated_at="2026-09-07T00:00:00Z",
    )


class InspectionProviderPipelineTests(unittest.TestCase):
    def test_full_planning_v1_report_is_rejected_for_v2_benchmark_contract(self) -> None:
        v1 = _contract(EvaluationScope.FULL_PLANNING_PIPELINE)
        v2 = v1.model_copy(update={"output_schema_digest": _digest("a")})
        with self.assertRaisesRegex(
            QualificationRunError, "BENCHMARK_FULL_PLANNING_INSPECTION_PROVIDER_MISMATCH"
        ):
            _require_matching_full_planning_scope_report((_report(v1),), v2)
        _require_matching_full_planning_scope_report((_report(v2),), v2)

    def test_cli_benchmark_binds_v2_and_rejects_v1_full_planning_metadata(self) -> None:
        planning_contract = _contract(EvaluationScope.FULL_PLANNING_PIPELINE)
        report = _report(planning_contract)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "qualification-report.json"
            (root / "evaluation-contract.json").write_text(
                planning_contract.model_dump_json(), encoding="utf-8", newline="\n"
            )
            report_path.write_text(report.model_dump_json(), encoding="utf-8", newline="\n")
            metadata = metadata_with_policies(
                {
                    "scope": "full-planning-pipeline",
                    "evaluation_contract_digest": planning_contract.contract_digest,
                    "inspection_provider_contract": PLAN_INSPECTION_PROVIDER_V1,
                },
                POLICIES,
            )
            (root / "run-metadata.json").write_text(
                json.dumps(metadata), encoding="utf-8", newline="\n"
            )
            with self.assertRaisesRegex(QualificationRunError, "inspection provider"):
                _bound_scope_reports(
                    [str(report_path)], ROOT,
                    inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2,
                )
            self.assertEqual(
                (report,),
                _bound_scope_reports(
                    [str(report_path)], ROOT,
                    inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V1,
                ),
            )

        arguments = argparse.Namespace(
            cells_file=None,
            project_root=str(ROOT),
            run_root=None,
            role_config=None,
            codex_bin=None,
            scope_report=[],
            inspection_contract=PLAN_INSPECTION_PROVIDER_V2,
            budget_policy="unused-budget.json",
            role_timeout_policy="unused-timeout.json",
            codex_project_binding=None,
        )
        completed = SimpleNamespace(
            report_digest=_digest("b"), model_dump=lambda **_: {"passed": True}, passed=True
        )
        with patch("flowmarshal.engine.eval_cli._evaluation_policies", return_value=POLICIES), patch(
            "flowmarshal.engine.eval_cli._roles", return_value=SimpleNamespace()
        ), patch("flowmarshal.engine.eval_cli._bound_scope_reports", return_value=()), patch(
            "flowmarshal.engine.benchmark.run_benchmark", return_value=(ROOT, completed)
        ) as benchmark:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, _benchmark(arguments))
        self.assertEqual(PLAN_INSPECTION_PROVIDER_V2, benchmark.call_args.kwargs["inspection_provider_contract"])

    def test_benchmark_resume_restores_metadata_provider_and_old_metadata_defaults_v1(self) -> None:
        contract = _contract(EvaluationScope.FULL_PLANNING_PIPELINE)
        completed = SimpleNamespace(
            report_digest=_digest("c"), model_dump=lambda **_: {"passed": True}, passed=True
        )
        for provider in (PLAN_INSPECTION_PROVIDER_V2, None):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                metadata_values = {
                    "scope": "benchmark",
                    "project_root": str(ROOT),
                    "evaluation_contract_digest": contract.contract_digest,
                    "role_configuration": {},
                    "scope_reports": [],
                }
                if provider is not None:
                    metadata_values["inspection_provider_contract"] = provider
                metadata = metadata_with_policies(metadata_values, POLICIES)
                (root / "run-metadata.json").write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
                (root / "evaluation-contract.json").write_text(contract.model_dump_json(), encoding="utf-8", newline="\n")
                with patch("flowmarshal.engine.eval_cli.EngineRoleConfiguration.model_validate", return_value=SimpleNamespace()), patch(
                    "flowmarshal.engine.benchmark.run_benchmark", return_value=(root, completed)
                ) as benchmark:
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(0, _resume(argparse.Namespace(run_root=str(root))))
                self.assertEqual(
                    provider or PLAN_INSPECTION_PROVIDER_V1,
                    benchmark.call_args.kwargs["inspection_provider_contract"],
                )

    def test_benchmark_parser_exposes_provider_and_observation_rejects_invalid_bound_value(self) -> None:
        arguments = build_parser().parse_args(
            ["benchmark", "--inspection-contract", PLAN_INSPECTION_PROVIDER_V2]
        )
        self.assertEqual(PLAN_INSPECTION_PROVIDER_V2, arguments.inspection_contract)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = _contract(EvaluationScope.FULL_PLANNING_PIPELINE)
            (root / "evaluation-contract.json").write_text(contract.model_dump_json(), encoding="utf-8", newline="\n")
            metadata = metadata_with_policies(
                {
                    "scope": "benchmark",
                    "evaluation_contract_digest": contract.contract_digest,
                    "inspection_provider_contract": "invalid-provider",
                },
                POLICIES,
            )
            (root / "run-metadata.json").write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
            checkpoint = EvaluationCellCheckpoint(
                model_lock_format="flowmarshal-model-lock-v2",
                contract_digest=contract.contract_digest,
                fixture_digest=_digest("1"),
                order_seed=1,
                raw_structured_assessment={},
                runner_receipts=({"receipt": "preserved"},),
            )
            with self.assertRaisesRegex(BenchmarkObservationError, "INSPECTION_PROVIDER_INVALID"):
                observe_benchmark_execution_checkpoint(checkpoint, run_root=root, contract=contract)


if __name__ == "__main__":
    unittest.main()
