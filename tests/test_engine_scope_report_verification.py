from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.e2e_qualification import E2E_SCENARIOS
from flowmarshal.engine.evaluation import (
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationRunStatus,
    EvaluationScope,
    FixtureResult,
    ImmutableCheckpointStore,
    evaluate_role_fixtures,
)
from flowmarshal.engine.qualification import (
    ORDER_SEEDS,
    PlanningScenarioCatalog,
    ScopeQualificationReport,
    _combined_role_catalog,
    _deterministic_contract,
    _write_json,
    source_manifest_digest,
)
from flowmarshal.engine.scope_report_verification import verify_scope_report
from flowmarshal.engine.domain import utc_now
from flowmarshal.engine.role_observations import RoleCallReceipt


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


def _contract(*, scope: EvaluationScope, fixtures: tuple[str, ...], seeds: tuple[int, ...]) -> EvaluationContract:
    return EvaluationContract(
        model_lock_format="flowmarshal-model-lock-v2", scope=scope,
        fixture_digests=fixtures, scenario_set_digest=DIGEST, order_seeds=seeds,
        expected_cell_count=len(fixtures) * len(seeds), role_configuration_digest=DIGEST,
        source_manifest_digest=source_manifest_digest(ROOT), rules_digest=DIGEST,
        threshold_digest=DIGEST, taxonomy_digest=DIGEST, prompt_digest=DIGEST,
        output_schema_digest=DIGEST, model_lock_digest=DIGEST,
    )


def _store(root: Path, contract: EvaluationContract, cells: list[EvaluationCellCheckpoint]) -> None:
    store = ImmutableCheckpointStore(root, contract)
    store.initialize()
    for item in cells:
        store.put(item)
    store.set_state(EvaluationRunStatus.COMPLETED, updated_at=utc_now())
    _write_json(root / "evaluation-contract.json", contract)


def _role_receipt() -> dict:
    return RoleCallReceipt(
        call_id="call-1", role="reviewer", status="completed", model="fixture-model", effort="low",
        inventory_digest=DIGEST, permission_profile="danger-full-access", approval_policy="never",
        input_digest=DIGEST, output_schema_digest=DIGEST, latency_ms=0, recorded_at=utc_now(),
    ).model_dump(mode="json")


def _report(contract: EvaluationContract, *, passed: bool, metrics: dict, failures: tuple[str, ...]) -> ScopeQualificationReport:
    return ScopeQualificationReport(
        scope=contract.scope, contract_digest=contract.contract_digest,
        status=EvaluationRunStatus.COMPLETED, passed=passed, metrics=metrics,
        failures=failures, generated_at=utc_now(),
    )


class ScopeReportVerificationTests(unittest.TestCase):
    def test_deterministic_reaggregates_five_exit_and_freeze_observations(self) -> None:
        contract = _deterministic_contract(ROOT)
        names = ("compileall", "full-test-suite", "pip-check", "synthetic-lifecycle", "legacy-freeze-manifest")
        cells = []
        for digest, name in zip(contract.fixture_digests, names, strict=True):
            raw = {"name": name, "passed": True}
            if name == "legacy-freeze-manifest":
                raw["report"] = {"passed": True}
            else:
                raw["returncode"] = 0
            cells.append(EvaluationCellCheckpoint(
                model_lock_format="flowmarshal-model-lock-v2", contract_digest=contract.contract_digest,
                fixture_digest=digest, order_seed=0, raw_structured_assessment=raw,
                runner_receipts=({"runner": "local", "check": name, "completed": True},),
            ))
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            _store(run, contract, cells)
            report = _report(contract, passed=True, metrics={"check_count": 5, "failure_count": 0}, failures=())
            result = verify_scope_report(root=ROOT, run_root=run, report=report)
        self.assertTrue(result.valid, result.errors)

    def test_report_passed_value_cannot_hide_failed_deterministic_exit(self) -> None:
        contract = _deterministic_contract(ROOT)
        names = ("compileall", "full-test-suite", "pip-check", "synthetic-lifecycle", "legacy-freeze-manifest")
        cells = []
        for digest, name in zip(contract.fixture_digests, names, strict=True):
            body = {"name": name, "passed": True, "returncode": 0}
            if name == "pip-check":
                body.update({"passed": False, "returncode": 1})
            if name == "legacy-freeze-manifest":
                body.pop("returncode")
                body["report"] = {"passed": True}
            cells.append(EvaluationCellCheckpoint(
                model_lock_format="flowmarshal-model-lock-v2", contract_digest=contract.contract_digest,
                fixture_digest=digest, order_seed=0, raw_structured_assessment=body,
                runner_receipts=({"runner": "local", "check": name, "completed": True},),
            ))
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw); _store(run, contract, cells)
            forged = _report(contract, passed=True, metrics={"check_count": 5, "failure_count": 0}, failures=())
            result = verify_scope_report(root=ROOT, run_root=run, report=forged)
        self.assertFalse(result.valid)
        self.assertIn("SCOPE_REPORT_PASSED_MISMATCH", result.errors)
        self.assertIn("SCOPE_REPORT_FAILURES_MISMATCH", result.errors)

    def test_role_scope_recalculates_all_48_fixture_results(self) -> None:
        catalog = _combined_role_catalog(ROOT)
        contract = _contract(scope=EvaluationScope.ROLE_FIXTURE,
            fixtures=tuple(item.fixture_digest for item in catalog.fixtures), seeds=ORDER_SEEDS)
        cells = []
        for seed in ORDER_SEEDS:
            for fixture in catalog.fixtures:
                cells.append(EvaluationCellCheckpoint(
                    model_lock_format="flowmarshal-model-lock-v2", contract_digest=contract.contract_digest,
                    fixture_digest=fixture.fixture_digest, order_seed=seed,
                    raw_structured_assessment={"fixture_result": FixtureResult(
                        opaque_case_ref=fixture.opaque_case_ref, order_seed=seed, schema_valid=False,
                    ).model_dump(mode="json")}, runner_receipts=(_role_receipt(),),
                ))
        original = evaluate_role_fixtures(
            catalog=catalog,
            results=tuple(FixtureResult(opaque_case_ref=f.opaque_case_ref, order_seed=seed, schema_valid=False)
                          for seed in ORDER_SEEDS for f in catalog.fixtures),
            prompt_digest=contract.prompt_digest, output_schema_digest=contract.output_schema_digest,
            model_lock_digest=contract.model_lock_digest,
        )
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw); _store(run, contract, cells)
            report = _report(contract, passed=original.passed, metrics=original.metrics.model_dump(mode="json"), failures=original.failures)
            result = verify_scope_report(root=ROOT, run_root=run, report=report)
        self.assertTrue(result.valid, result.errors)
        self.assertFalse(result.recalculated_passed)
        self.assertEqual(48, result.recalculated_metrics["cell_count"])

    def test_planning_scope_reaggregates_eighteen_cells_and_requires_matrix(self) -> None:
        catalog = PlanningScenarioCatalog.load(ROOT / "tests" / "fixtures" / "engine" / "planning-scenarios.json")
        contract = _contract(scope=EvaluationScope.FULL_PLANNING_PIPELINE,
            fixtures=tuple(item.scenario_digest for item in catalog.scenarios), seeds=ORDER_SEEDS)
        cells = []
        for seed in ORDER_SEEDS:
            for scenario in catalog.scenarios:
                cells.append(EvaluationCellCheckpoint(
                    model_lock_format="flowmarshal-model-lock-v2", contract_digest=contract.contract_digest,
                    fixture_digest=scenario.scenario_digest, order_seed=seed,
                    raw_structured_assessment={"scenario_id": scenario.scenario_id, "scenario_digest": scenario.scenario_digest,
                        "order_seed": seed, "expected_disposition": scenario.expected_disposition, "passed": True,
                        "selected": scenario.expected_disposition == "selected", "schema_valid": True,
                        "logical_role_calls": 1, "candidate_versions": 1},
                    runner_receipts=(_role_receipt(),),
                ))
        metrics = {"cell_count": 18, "clean_selected_count": sum(1 for x in cells if x.raw_structured_assessment["selected"]),
            "adversarial_blocked_count": sum(1 for x in cells if x.raw_structured_assessment["expected_disposition"] == "blocked"),
            "max_logical_role_calls": 1, "max_candidate_versions": 1, "schema_failure_count": 0}
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw); _store(run, contract, cells)
            result = verify_scope_report(root=ROOT, run_root=run, report=_report(contract, passed=True, metrics=metrics, failures=()))
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(18, result.recalculated_metrics["cell_count"])

    def test_project_e2e_four_prebuilt_cells_cannot_replace_responsibility_gate(self) -> None:
        fixtures = tuple(sha256_digest({"e2e": scenario}) for scenario in E2E_SCENARIOS)
        contract = _contract(scope=EvaluationScope.PROJECT_E2E, fixtures=fixtures, seeds=(0,))
        cells = []
        for digest, scenario in zip(fixtures, E2E_SCENARIOS, strict=True):
            events = [{"scenario": scenario, "event": "stored"}]
            cells.append(EvaluationCellCheckpoint(
                model_lock_format="flowmarshal-model-lock-v2", contract_digest=contract.contract_digest,
                fixture_digest=digest, order_seed=0,
                raw_structured_assessment={"scenario": scenario, "order_seed": 0, "passed": True,
                    "thread_create_count": 1},
                runner_receipts=({"events": events, "events_digest": sha256_digest(events)},),
            ))
        metrics = {"cell_count": 4, "passed_cell_count": 4, "actual_codex_cell_count": 4, "duplicate_effect_count": 0}
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw); _store(run, contract, cells)
            report = _report(contract, passed=True, metrics=metrics, failures=())
            incomplete = verify_scope_report(root=ROOT, run_root=run, report=report)
            self.assertFalse(incomplete.valid)
            self.assertIn(
                "SCOPE_REPORT_E2E_RESPONSIBILITY_EVIDENCE_MISSING",
                incomplete.errors,
            )
            self.assertEqual(18, incomplete.recalculated_metrics["responsibility_count"])
            self.assertEqual(0, incomplete.recalculated_metrics["passed_responsibility_count"])
            cells_path = run / "cells" / "seed-0"
            target = next(cells_path.glob("*.json"))
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["runner_receipts"][0]["events_digest"] = "sha256:" + "0" * 64
            target.write_text(json.dumps(payload), encoding="utf-8")
            result = verify_scope_report(root=ROOT, run_root=run, report=report)
        self.assertFalse(result.valid)
        self.assertIn("SCOPE_REPORT_E2E_RECEIPT_BINDING_MISMATCH", result.errors)


if __name__ == "__main__":
    unittest.main()
