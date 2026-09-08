from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.benchmark_safety import non_blocking_comparison_report
from flowmarshal.engine.qualification_manifest import (
    EvidenceProvenance,
    QualificationCellOutcome,
    QualificationCellStatus,
    QualificationFailureClass,
    QualificationManifestError,
    QualificationSuiteManifest,
    classify_qualification_failure,
    evaluate_qualification_responsibilities,
    verify_qualification_reproduction_bundle,
    write_qualification_reproduction_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "sha256:" + "a" * 64


class QualificationManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = QualificationSuiteManifest.load(
            ROOT / "config" / "qualification-suite.json"
        )

    def outcome(self, responsibility_id: str, **changes) -> QualificationCellOutcome:
        requirement = next(
            item
            for item in self.suite.e2e_responsibilities
            if item.responsibility_id == responsibility_id
        )
        provenance = requirement.required_provenance or (
            requirement.allowed_provenance[0],
        )
        values = {
            "cell_id": "cell-" + responsibility_id.lower(),
            "evaluation_contract_digest": CONTRACT,
            "responsibility_ids": (responsibility_id,),
            "status": QualificationCellStatus.PASSED,
            "provenance": provenance,
            "pipeline_stages": requirement.required_pipeline_stages,
            "evidence_kinds": requirement.required_evidence_kinds,
            "evidence_refs": ("evidence/" + responsibility_id + ".json",),
        }
        values.update(changes)
        return QualificationCellOutcome(**values)

    def test_approved_role_and_planning_thresholds_are_exact(self) -> None:
        self.assertEqual(48, self.suite.role_gate.expected_cell_count)
        self.assertEqual((17, 43, 89), self.suite.role_gate.order_seeds)
        self.assertEqual(0.90, self.suite.role_gate.required_finding_recall)
        self.assertEqual(0.85, self.suite.role_gate.finding_precision)
        self.assertEqual(18, self.suite.planning_gate.expected_cell_count)
        self.assertEqual(4, self.suite.planning_gate.selected_scenario_count)
        self.assertEqual(2, self.suite.planning_gate.blocking_question_scenario_count)
        self.assertEqual(14, self.suite.planning_gate.max_role_calls)
        self.assertEqual(5, self.suite.planning_gate.max_candidate_versions)
        self.assertFalse(self.suite.non_blocking_comparisons.release_blocking)
        self.assertEqual(18, len(self.suite.e2e_responsibilities))

        raw = self.suite.model_dump(mode="json")
        raw["role_gate"]["expected_cell_count"] = 47
        with self.assertRaisesRegex(ValueError, "역할 48회"):
            QualificationSuiteManifest.model_validate(raw)
        raw = self.suite.model_dump(mode="json")
        raw["planning_gate"]["max_role_calls"] = 15
        with self.assertRaisesRegex(ValueError, "Planning 18회"):
            QualificationSuiteManifest.model_validate(raw)

    def test_all_required_responsibilities_need_semantic_evidence(self) -> None:
        outcomes = tuple(
            self.outcome(item.responsibility_id)
            for item in self.suite.e2e_responsibilities
        )
        report = evaluate_qualification_responsibilities(
            self.suite, outcomes, evaluation_contract_digest=CONTRACT
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(18, report.passed_responsibility_count)

        bypassed = list(outcomes)
        bypassed[0] = self.outcome(
            "E2E-01", pipeline_stages=("plan_activation",)
        )
        report = evaluate_qualification_responsibilities(
            self.suite, tuple(bypassed), evaluation_contract_digest=CONTRACT
        )
        self.assertFalse(report.passed)
        self.assertIn("E2E-01", report.contract_failure_responsibility_ids)
        self.assertTrue(
            any("RAW_REQUEST_PIPELINE_BYPASSED" in item for item in report.failures)
        )

    def test_not_run_and_failure_classes_are_not_conflated(self) -> None:
        outcomes = [
            self.outcome(item.responsibility_id)
            for item in self.suite.e2e_responsibilities
        ]
        outcomes[1] = QualificationCellOutcome(
            cell_id="not-run",
            evaluation_contract_digest=CONTRACT,
            responsibility_ids=("E2E-02",),
            status=QualificationCellStatus.NOT_RUN,
            provenance=(EvidenceProvenance.LIVE,),
        )
        outcomes[2] = QualificationCellOutcome(
            cell_id="model-failure",
            evaluation_contract_digest=CONTRACT,
            responsibility_ids=("E2E-03",),
            status=QualificationCellStatus.FAILED,
            provenance=(EvidenceProvenance.LIVE,),
            failure_class=QualificationFailureClass.MODEL,
            failure_code="ROLE_SCHEMA_INVALID",
        )
        outcomes[3] = QualificationCellOutcome(
            cell_id="environment-failure",
            evaluation_contract_digest=CONTRACT,
            responsibility_ids=("E2E-04",),
            status=QualificationCellStatus.FAILED,
            provenance=(EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
            failure_class=QualificationFailureClass.ENVIRONMENT,
            failure_code="CODEX_UNAVAILABLE",
        )
        outcomes[4] = QualificationCellOutcome(
            cell_id="fixture-failure",
            evaluation_contract_digest=CONTRACT,
            responsibility_ids=("E2E-05",),
            status=QualificationCellStatus.FAILED,
            provenance=(EvidenceProvenance.LIVE,),
            failure_class=QualificationFailureClass.FIXTURE,
            failure_code="FIXTURE_DIGEST_MISMATCH",
        )
        report = evaluate_qualification_responsibilities(
            self.suite, tuple(outcomes), evaluation_contract_digest=CONTRACT
        )
        self.assertIn("E2E-02", report.not_run_responsibility_ids)
        self.assertIn("E2E-03", report.model_failure_responsibility_ids)
        self.assertIn("E2E-04", report.environment_failure_responsibility_ids)
        self.assertIn("E2E-05", report.fixture_failure_responsibility_ids)
        self.assertEqual(
            QualificationFailureClass.MODEL,
            classify_qualification_failure(RuntimeError("ROLE_SCHEMA_INVALID")),
        )
        self.assertEqual(
            QualificationFailureClass.ENVIRONMENT,
            classify_qualification_failure(RuntimeError("CODEX_UNAVAILABLE")),
        )
        self.assertEqual(
            QualificationFailureClass.FIXTURE,
            classify_qualification_failure(RuntimeError("FIXTURE_DIGEST_MISMATCH")),
        )

    def test_reused_evidence_is_invalidated_when_contract_changes(self) -> None:
        outcomes = tuple(
            self.outcome(item.responsibility_id)
            for item in self.suite.e2e_responsibilities
        )
        requirement = self.suite.e2e_responsibilities[13]
        changed = list(outcomes)
        changed[13] = self.outcome(
            requirement.responsibility_id,
            provenance=(EvidenceProvenance.REUSED,),
            reused_from_contract_digest="sha256:" + "b" * 64,
        )
        report = evaluate_qualification_responsibilities(
            self.suite, tuple(changed), evaluation_contract_digest=CONTRACT
        )
        self.assertFalse(report.passed)
        self.assertTrue(
            any(
                "REUSE_INVALIDATED_BY_CONTRACT_CHANGE" in item
                for item in report.failures
            )
        )

    def test_reproduction_bundle_detects_payload_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            source = root / "evaluator.py"
            source.write_text("value = 1\n", encoding="utf-8")
            digest = sha256_bytes(source.read_bytes())
            destination = Path(temp) / "bundle"
            manifest = write_qualification_reproduction_bundle(
                source_root=root,
                destination=destination,
                suite=self.suite,
                source_files={"evaluator.py": digest},
                source_manifest_digest="sha256:" + "c" * 64,
                fixture_documents={"fixture": {"digest": "one"}},
                prompt_documents={"prompt": "one"},
                schema_documents={"schema": {"type": "object"}},
                threshold_documents={"threshold": {"all": True}},
                taxonomy_documents={"taxonomy": ["failure"]},
                model_inventory_document={"models": ["model-a"]},
                model_lock_document={"selected": "model-a"},
                evaluator_files=("evaluator.py",),
            )
            self.assertEqual(
                manifest.bundle_digest,
                verify_qualification_reproduction_bundle(destination).bundle_digest,
            )
            self.assertTrue(
                (destination / "payload" / "inputs" / "model-inventory.json").is_file()
            )
            self.assertTrue(
                (destination / "payload" / "inputs" / "model-lock.json").is_file()
            )
            (destination / "payload" / "evaluator.py").write_text(
                "value = 2\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                QualificationManifestError, "payload digest"
            ):
                verify_qualification_reproduction_bundle(destination)

    def test_performance_comparison_is_explicitly_non_blocking(self) -> None:
        report = non_blocking_comparison_report(
            suite="performance36",
            observed_metrics={"pair_count": 0, "ratio": None},
            not_observed=("PAIR_NOT_RUN",),
        )
        self.assertFalse(report.complete)
        self.assertFalse(report.release_blocking)
        self.assertIsNone(report.observed_metrics["ratio"])


if __name__ == "__main__":
    unittest.main()
