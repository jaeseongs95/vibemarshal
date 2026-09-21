from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
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
from flowmarshal.engine.evaluation_budget import (
    load_evaluation_policies,
    metadata_with_policies,
)
from flowmarshal.engine.qualification import (
    ORDER_SEEDS,
    PlanningScenarioCatalog,
    ScopeQualificationReport,
    _combined_role_catalog,
    _deterministic_contract,
    _write_json,
    default_role_configuration,
    qualification_suite_manifest,
    source_manifest_digest,
)
from flowmarshal.engine.qualification_manifest import (
    CandidateWheelBinding,
    QualificationCellOutcome,
    QualificationCellStatus,
    build_qualification_evidence_record,
    write_qualification_reproduction_bundle,
)
from flowmarshal.engine.scope_report_verification import verify_scope_report
from flowmarshal.engine.domain import utc_now
from flowmarshal.engine.role_observations import RoleCallReceipt


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64
GOVERNANCE_PLUGIN_IDENTITY = "sha256:" + "9" * 64
RELEASE_FREEZE_DIGEST = "sha256:" + "8" * 64


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
    def _verify_typed_e2e(
        self,
        *,
        run: Path,
        report: ScopeQualificationReport,
        binding: CandidateWheelBinding,
        contract: EvaluationContract,
        freeze_identity: str = GOVERNANCE_PLUGIN_IDENTITY,
    ):
        release = SimpleNamespace(
            valid=True,
            mismatches=(),
            manifest=SimpleNamespace(
                freeze_digest=RELEASE_FREEZE_DIGEST,
                governance_plugin=SimpleNamespace(
                    e2e_identity_digest=freeze_identity
                ),
            ),
        )
        with patch(
            "flowmarshal.engine.scope_report_verification.verify_candidate_wheel_metadata",
            return_value=binding,
        ), patch(
            "flowmarshal.engine.scope_report_verification.source_manifest_digest",
            return_value=contract.source_manifest_digest,
        ), patch(
            "flowmarshal.engine.release_freeze.verify_release_freeze",
            return_value=release,
        ):
            return verify_scope_report(root=ROOT, run_root=run, report=report)

    def _typed_e2e_run(
        self, run: Path, contract: EvaluationContract
    ) -> tuple[ScopeQualificationReport, Path, CandidateWheelBinding]:
        suite = qualification_suite_manifest(ROOT)
        source_root = run / "bundle-source"
        source_root.mkdir()
        evaluator = source_root / "evaluator.py"
        evaluator.write_text("value = 1\n", encoding="utf-8")
        freeze = write_qualification_reproduction_bundle(
            source_root=source_root,
            destination=run / "reproduction-bundle",
            suite=suite,
            source_files={"evaluator.py": sha256_bytes(evaluator.read_bytes())},
            source_manifest_digest=DIGEST,
            fixture_documents={"fixtures": list(contract.fixture_digests)},
            prompt_documents={"prompt": contract.prompt_digest},
            schema_documents={"schema": contract.output_schema_digest},
            threshold_documents={"threshold": contract.threshold_digest},
            taxonomy_documents={"taxonomy": contract.taxonomy_digest},
            model_inventory_document={"digest": contract.model_lock_digest},
            model_lock_document={"digest": contract.model_lock_digest},
            evaluator_files=("evaluator.py",),
        )
        wheel_path = run / "candidate.whl"
        wheel_path.write_bytes(b"candidate-wheel")
        distribution_root = run / "site-packages"
        import_root = distribution_root / "flowmarshal"
        import_root.mkdir(parents=True)
        package_digests = {
            "flowmarshal/__init__.py": sha256_bytes(b"package")
        }
        binding = CandidateWheelBinding(
            wheel_path=str(wheel_path.resolve()),
            wheel_digest=sha256_bytes(wheel_path.read_bytes()),
            distribution_name="flowmarshal-engine",
            distribution_version="0.2.0a1",
            distribution_root=str(distribution_root.resolve()),
            import_root=str(import_root.resolve()),
            package_file_digests=package_digests,
            wheel_package_digest=sha256_digest(package_digests),
            installed_package_digest=sha256_digest(package_digests),
        )
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        metadata = metadata_with_policies({
            "scope": "project-e2e",
            "candidate_wheel_binding": binding.model_dump(mode="json"),
            "candidate_wheel_binding_digest": binding.binding_digest,
            "role_configuration": default_role_configuration(ROOT).model_dump(
                mode="json"
            ),
            "release_freeze_path": str((run / "release-freeze").resolve()),
            "release_freeze_digest": RELEASE_FREEZE_DIGEST,
            "governance_plugin_identity_digest": GOVERNANCE_PLUGIN_IDENTITY,
        }, policies)
        (run / "run-metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        mapping = {
            "normal-completion": "E2E-01",
            "stale-after-materialization": "E2E-11",
            "stored-turn-restart-resume": "E2E-07",
            "unknown-receipt-no-duplicate": "E2E-08",
            "forced-termination-no-duplicate": "E2E-09",
            "absolute-timeout-no-duplicate": "E2E-10",
            "cancel-active-job": "E2E-16",
            "partial-write-input-changed": "E2E-13",
            "in-flight-replan-protection": "E2E-17",
            "partial-write-resume": "E2E-12",
        }
        cells: list[EvaluationCellCheckpoint] = []
        first_evidence_path: Path | None = None
        for digest, scenario in zip(
            contract.fixture_digests, E2E_SCENARIOS, strict=True
        ):
            responsibility_id = mapping[scenario]
            requirement = next(
                item
                for item in suite.e2e_responsibilities
                if item.responsibility_id == responsibility_id
            )
            cell_root = run / "work" / scenario
            records = []
            for kind in requirement.required_evidence_kinds:
                evidence_path = cell_root / "evidence" / f"{kind}.json"
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_text(
                    json.dumps({"cell_id": scenario, "kind": kind}),
                    encoding="utf-8",
                )
                first_evidence_path = first_evidence_path or evidence_path
                records.append(
                    build_qualification_evidence_record(
                        run_root=run,
                        path=evidence_path,
                        kind=kind,
                        cell_id=scenario,
                        evaluation_contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                        order_seed=0,
                        freeze_bundle_digest=freeze.bundle_digest,
                        governance_plugin_identity_digest=(
                            GOVERNANCE_PLUGIN_IDENTITY
                        ),
                        candidate_wheel_digest=binding.wheel_digest,
                        candidate_wheel_binding_digest=binding.binding_digest,
                        candidate_distribution_name=binding.distribution_name,
                        candidate_distribution_version=binding.distribution_version,
                    )
                )
            provenance = requirement.required_provenance or (
                requirement.allowed_provenance[0],
            )
            outcome = QualificationCellOutcome(
                cell_id=scenario,
                evaluation_contract_digest=contract.contract_digest,
                responsibility_ids=(responsibility_id,),
                status=QualificationCellStatus.PASSED,
                provenance=provenance,
                pipeline_stages=requirement.required_pipeline_stages,
                evidence_kinds=requirement.required_evidence_kinds,
                evidence_refs=tuple(item.relative_path for item in records),
                evidence_records=tuple(records),
            )
            events = [{"scenario": scenario, "event": "stored"}]
            cells.append(
                EvaluationCellCheckpoint(
                    model_lock_format="flowmarshal-model-lock-v2",
                    contract_digest=contract.contract_digest,
                    fixture_digest=digest,
                    order_seed=0,
                    raw_structured_assessment={
                        "scenario": scenario,
                        "order_seed": 0,
                        "passed": True,
                        "thread_create_count": 1,
                        "qualification_outcome": outcome.model_dump(mode="json"),
                    },
                    runner_receipts=(
                        {"events": events, "events_digest": sha256_digest(events)},
                    ),
                )
            )
        _store(run, contract, cells)
        draft = self._verify_typed_e2e(
            run=run,
            report=_report(contract, passed=False, metrics={}, failures=()),
            binding=binding,
            contract=contract,
        )
        report = _report(
            contract,
            passed=draft.recalculated_passed,
            metrics=draft.recalculated_metrics,
            failures=draft.recalculated_failures,
        )
        baseline = self._verify_typed_e2e(
            run=run,
            report=report,
            binding=binding,
            contract=contract,
        )
        self.assertTrue(baseline.valid, baseline.errors)
        assert first_evidence_path is not None
        return report, first_evidence_path, binding

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

    def test_project_e2e_prebuilt_cells_cannot_replace_responsibility_gate(self) -> None:
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
        metrics = {
            "cell_count": len(cells),
            "passed_cell_count": len(cells),
            "actual_codex_cell_count": len(cells),
            "duplicate_effect_count": 0,
        }
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

    def test_project_e2e_final_verifier_rechecks_typed_evidence(self) -> None:
        fixtures = tuple(
            sha256_digest({"e2e": scenario}) for scenario in E2E_SCENARIOS
        )
        contract = _contract(
            scope=EvaluationScope.PROJECT_E2E, fixtures=fixtures, seeds=(0,)
        )
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            report, evidence_path, binding = self._typed_e2e_run(run, contract)
            original_evidence = evidence_path.read_bytes()

            invalid_wheel = verify_scope_report(
                root=ROOT, run_root=run, report=report
            )
            self.assertFalse(invalid_wheel.valid)
            self.assertIn(
                "SCOPE_REPORT_E2E_CANDIDATE_WHEEL_INVALID",
                invalid_wheel.errors,
            )

            evidence_path.write_text("tampered", encoding="utf-8")
            tampered = self._verify_typed_e2e(
                run=run,
                report=report,
                binding=binding,
                contract=contract,
            )
            self.assertFalse(tampered.valid)
            self.assertTrue(
                any(
                    "EVIDENCE_DIGEST_MISMATCH" in item
                    for item in tampered.recalculated_failures
                )
            )
            evidence_path.write_bytes(original_evidence)

            evidence_path.unlink()
            deleted = self._verify_typed_e2e(
                run=run,
                report=report,
                binding=binding,
                contract=contract,
            )
            self.assertFalse(deleted.valid)
            self.assertTrue(
                any(
                    "EVIDENCE_FILE_MISSING" in item
                    for item in deleted.recalculated_failures
                )
            )
            evidence_path.write_bytes(original_evidence)

            checkpoint_path = next((run / "cells" / "seed-0").glob("*.json"))
            checkpoint_text = checkpoint_path.read_text(encoding="utf-8")
            mutations = {
                "path-escape": (
                    "relative_path",
                    "../outside.json",
                    "RESPONSIBILITY_EVIDENCE_MISSING",
                ),
                "wrong-cell": (
                    "cell_id",
                    "other-cell",
                    "EVIDENCE_CELL_MISMATCH",
                ),
                "wrong-contract": (
                    "evaluation_contract_digest",
                    "sha256:" + "b" * 64,
                    "EVIDENCE_CONTRACT_MISMATCH",
                ),
                "wrong-fixture": (
                    "fixture_digest",
                    "sha256:" + "b" * 64,
                    "EVIDENCE_FIXTURE_MISMATCH",
                ),
                "wrong-seed": (
                    "order_seed",
                    1,
                    "EVIDENCE_ORDER_SEED_MISMATCH",
                ),
                "wrong-freeze": (
                    "freeze_bundle_digest",
                    "sha256:" + "b" * 64,
                    "EVIDENCE_FREEZE_MISMATCH",
                ),
                "wrong-governance-plugin": (
                    "governance_plugin_identity_digest",
                    "sha256:" + "b" * 64,
                    "EVIDENCE_GOVERNANCE_PLUGIN_MISMATCH",
                ),
                "wrong-kind": (
                    "kind",
                    "unrelated",
                    "REQUIRED_EVIDENCE_MISSING",
                ),
                "wrong-wheel": (
                    "candidate_wheel_digest",
                    "sha256:" + "b" * 64,
                    "EVIDENCE_WHEEL_MISMATCH",
                ),
                "wrong-distribution-version": (
                    "candidate_distribution_version",
                    "9.9.9",
                    "EVIDENCE_DISTRIBUTION_VERSION_MISMATCH",
                ),
            }
            for name, (field, value, expected) in mutations.items():
                with self.subTest(name=name):
                    payload = json.loads(checkpoint_text)
                    payload["raw_structured_assessment"]["qualification_outcome"][
                        "evidence_records"
                    ][0][field] = value
                    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
                    changed = self._verify_typed_e2e(
                        run=run,
                        report=report,
                        binding=binding,
                        contract=contract,
                    )
                    self.assertFalse(changed.valid)
                    joined = "\n".join(
                        (*changed.errors, *changed.recalculated_failures)
                    )
                    self.assertIn(expected, joined)
                    checkpoint_path.write_text(checkpoint_text, encoding="utf-8")

            freeze_drift = self._verify_typed_e2e(
                run=run,
                report=report,
                binding=binding,
                contract=contract,
                freeze_identity="sha256:" + "7" * 64,
            )
            self.assertFalse(freeze_drift.valid)
            self.assertIn(
                "SCOPE_REPORT_E2E_GOVERNANCE_PLUGIN_INVALID",
                freeze_drift.errors,
            )

            metadata_path = run / "run-metadata.json"
            original_metadata = metadata_path.read_text(encoding="utf-8")
            metadata = json.loads(original_metadata)
            metadata["governance_plugin_identity_digest"] = "sha256:" + "6" * 64
            metadata.pop("metadata_digest")
            metadata["metadata_digest"] = sha256_digest(metadata)
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            metadata_drift = self._verify_typed_e2e(
                run=run,
                report=report,
                binding=binding,
                contract=contract,
            )
            self.assertFalse(metadata_drift.valid)
            self.assertIn(
                "SCOPE_REPORT_E2E_GOVERNANCE_PLUGIN_INVALID",
                metadata_drift.errors,
            )
            metadata_path.write_text(original_metadata, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
