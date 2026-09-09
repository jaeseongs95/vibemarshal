from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.benchmark_safety import non_blocking_comparison_report
from flowmarshal.engine.qualification_manifest import (
    CandidateWheelBinding,
    EvidenceProvenance,
    QualificationCellOutcome,
    QualificationCellStatus,
    QualificationEvidenceRecord,
    QualificationFailureClass,
    QualificationManifestError,
    QualificationSuiteManifest,
    build_qualification_evidence_record,
    classify_qualification_failure,
    evaluate_qualification_responsibilities,
    verify_candidate_wheel_installation,
    verify_candidate_wheel_metadata,
    verify_qualification_reproduction_bundle,
    write_qualification_reproduction_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "sha256:" + "a" * 64
FIXTURE = "sha256:" + "f" * 64
WHEEL = "sha256:" + "e" * 64
WHEEL_BINDING = "sha256:" + "d" * 64
DISTRIBUTION_NAME = "flowmarshal-engine"
DISTRIBUTION_VERSION = "0.2.0a1"


class QualificationManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        self.suite = QualificationSuiteManifest.load(
            ROOT / "config" / "qualification-suite.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def outcome(self, responsibility_id: str, **changes) -> QualificationCellOutcome:
        requirement = next(
            item
            for item in self.suite.e2e_responsibilities
            if item.responsibility_id == responsibility_id
        )
        provenance = requirement.required_provenance or (
            requirement.allowed_provenance[0],
        )
        cell_id = "cell-" + responsibility_id.lower()
        evidence_records = []
        for kind in requirement.required_evidence_kinds:
            path = self.run_root / "evidence" / cell_id / f"{kind}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"cell_id": cell_id, "kind": kind}), encoding="utf-8"
            )
            evidence_records.append(
                build_qualification_evidence_record(
                    run_root=self.run_root,
                    path=path,
                    kind=kind,
                    cell_id=cell_id,
                    evaluation_contract_digest=CONTRACT,
                    fixture_digest=FIXTURE,
                    order_seed=0,
                    candidate_wheel_digest=WHEEL,
                    candidate_wheel_binding_digest=WHEEL_BINDING,
                    candidate_distribution_name=DISTRIBUTION_NAME,
                    candidate_distribution_version=DISTRIBUTION_VERSION,
                )
            )
        values = {
            "cell_id": cell_id,
            "evaluation_contract_digest": CONTRACT,
            "responsibility_ids": (responsibility_id,),
            "status": QualificationCellStatus.PASSED,
            "provenance": provenance,
            "pipeline_stages": requirement.required_pipeline_stages,
            "evidence_kinds": requirement.required_evidence_kinds,
            "evidence_refs": tuple(
                item.relative_path for item in evidence_records
            ),
            "evidence_records": tuple(evidence_records),
        }
        values.update(changes)
        return QualificationCellOutcome(**values)

    def evaluate(
        self, outcomes: tuple[QualificationCellOutcome, ...]
    ):
        return evaluate_qualification_responsibilities(
            self.suite,
            outcomes,
            evaluation_contract_digest=CONTRACT,
            run_root=self.run_root,
            expected_cell_bindings={
                item.cell_id: (FIXTURE, 0) for item in outcomes
            },
            expected_candidate_wheel_digest=WHEEL,
            expected_candidate_wheel_binding_digest=WHEEL_BINDING,
            expected_candidate_distribution_name=DISTRIBUTION_NAME,
            expected_candidate_distribution_version=DISTRIBUTION_VERSION,
        )

    def candidate_wheel_fixture(self):
        wheel = self.run_root / "flowmarshal_engine-0.2.0a1-py3-none-any.whl"
        package_files = {
            "flowmarshal/__init__.py": b"__version__ = '0.2.0a1'\n",
            "flowmarshal/module.py": b"VALUE = 1\n",
        }
        with zipfile.ZipFile(wheel, "w") as archive:
            for relative, payload in package_files.items():
                archive.writestr(relative, payload)
            archive.writestr(
                "flowmarshal_engine-0.2.0a1.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: flowmarshal-engine\nVersion: 0.2.0a1\n",
            )
        installed_root = self.run_root / "site-packages"
        for relative, payload in package_files.items():
            path = installed_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        class FakeDistribution:
            metadata = {"Name": "flowmarshal-engine"}
            version = "0.2.0a1"
            files = tuple(package_files)

            def __init__(self, *, editable: bool = False):
                self.editable = editable

            def locate_file(self, relative):
                return installed_root / relative

            def read_text(self, name):
                if name != "direct_url.json":
                    return None
                return json.dumps(
                    {
                        "url": wheel.as_uri(),
                        "dir_info": {"editable": self.editable},
                    }
                )

        return wheel, installed_root, FakeDistribution

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
        report = self.evaluate(outcomes)
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(18, report.passed_responsibility_count)

        bypassed = list(outcomes)
        bypassed[0] = self.outcome(
            "E2E-01", pipeline_stages=("plan_activation",)
        )
        report = self.evaluate(tuple(bypassed))
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
        report = self.evaluate(tuple(outcomes))
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
        report = self.evaluate(tuple(changed))
        self.assertFalse(report.passed)
        self.assertTrue(
            any(
                "REUSE_INVALIDATED_BY_CONTRACT_CHANGE" in item
                for item in report.failures
            )
        )

    def test_legacy_string_refs_cannot_create_new_release_pass(self) -> None:
        outcomes = tuple(
            self.outcome(item.responsibility_id)
            for item in self.suite.e2e_responsibilities
        )
        legacy = outcomes[0].model_copy(
            update={"evidence_records": (), "evidence_refs": ("E2E-01.json",)}
        )
        report = self.evaluate((legacy, *outcomes[1:]))
        self.assertFalse(report.passed)
        self.assertTrue(
            any("LEGACY_EVIDENCE_UNVERIFIED" in item for item in report.failures)
        )

    def test_project_e2e_pass_requires_verified_candidate_wheel_digest(self) -> None:
        outcomes = tuple(
            self.outcome(item.responsibility_id)
            for item in self.suite.e2e_responsibilities
        )
        report = evaluate_qualification_responsibilities(
            self.suite,
            outcomes,
            evaluation_contract_digest=CONTRACT,
            run_root=self.run_root,
            expected_cell_bindings={
                item.cell_id: (FIXTURE, 0) for item in outcomes
            },
        )
        self.assertFalse(report.passed)
        self.assertTrue(
            any("CANDIDATE_WHEEL_BINDING_MISSING" in item for item in report.failures)
        )

    def test_typed_evidence_rejects_tamper_wrong_binding_and_wrong_kind(self) -> None:
        baseline = tuple(
            self.outcome(item.responsibility_id)
            for item in self.suite.e2e_responsibilities
        )
        original = baseline[0]
        record = original.evidence_records[0]
        cases = {
            "tamper": (
                original,
                "EVIDENCE_DIGEST_MISMATCH",
            ),
            "wrong-cell": (
                original.model_copy(
                    update={
                        "evidence_records": (
                            record.model_copy(update={"cell_id": "other-cell"}),
                            *original.evidence_records[1:],
                        )
                    }
                ),
                "EVIDENCE_CELL_MISMATCH",
            ),
            "wrong-contract": (
                original.model_copy(
                    update={
                        "evidence_records": (
                            record.model_copy(
                                update={
                                    "evaluation_contract_digest": "sha256:" + "b" * 64
                                }
                            ),
                            *original.evidence_records[1:],
                        )
                    }
                ),
                "EVIDENCE_CONTRACT_MISMATCH",
            ),
            "wrong-fixture": (
                original.model_copy(
                    update={
                        "evidence_records": (
                            record.model_copy(
                                update={"fixture_digest": "sha256:" + "b" * 64}
                            ),
                            *original.evidence_records[1:],
                        )
                    }
                ),
                "EVIDENCE_FIXTURE_MISMATCH",
            ),
            "wrong-kind": (
                original.model_copy(
                    update={
                        "evidence_records": (
                            record.model_copy(update={"kind": "unrelated"}),
                            *original.evidence_records[1:],
                        )
                    }
                ),
                "REQUIRED_EVIDENCE_MISSING",
            ),
            "wrong-wheel": (
                original.model_copy(
                    update={
                        "evidence_records": (
                            record.model_copy(
                                update={
                                    "candidate_wheel_digest": "sha256:" + "b" * 64
                                }
                            ),
                            *original.evidence_records[1:],
                        )
                    }
                ),
                "EVIDENCE_WHEEL_MISMATCH",
            ),
            "wrong-wheel-binding": (
                original.model_copy(
                    update={
                        "evidence_records": (
                            record.model_copy(
                                update={
                                    "candidate_wheel_binding_digest": "sha256:" + "b" * 64
                                }
                            ),
                            *original.evidence_records[1:],
                        )
                    }
                ),
                "EVIDENCE_WHEEL_BINDING_MISMATCH",
            ),
        }
        for name, (changed, expected) in cases.items():
            with self.subTest(name=name):
                if name == "tamper":
                    target = self.run_root / record.relative_path
                    target.write_text("tampered", encoding="utf-8")
                report = self.evaluate((changed, *baseline[1:]))
                self.assertFalse(report.passed)
                self.assertTrue(
                    any(expected in item for item in report.failures), report.failures
                )
                if name == "tamper":
                    target.write_text(
                        json.dumps({"cell_id": original.cell_id, "kind": record.kind}),
                        encoding="utf-8",
                    )

    def test_evidence_builder_rejects_path_escape_and_missing_file(self) -> None:
        outside = self.run_root.parent / "outside-evidence.json"
        outside.write_text("outside", encoding="utf-8")
        try:
            with self.assertRaises(QualificationManifestError):
                build_qualification_evidence_record(
                    run_root=self.run_root,
                    path=outside,
                    kind="ledger",
                    cell_id="cell",
                    evaluation_contract_digest=CONTRACT,
                    fixture_digest=FIXTURE,
                    order_seed=0,
                )
            with self.assertRaises(QualificationManifestError):
                build_qualification_evidence_record(
                    run_root=self.run_root,
                    path=self.run_root / "missing.json",
                    kind="ledger",
                    cell_id="cell",
                    evaluation_contract_digest=CONTRACT,
                    fixture_digest=FIXTURE,
                    order_seed=0,
                )
            with self.assertRaises(ValueError):
                QualificationEvidenceRecord(
                    relative_path="../outside-evidence.json",
                    sha256="sha256:" + "0" * 64,
                    kind="ledger",
                    cell_id="cell",
                    evaluation_contract_digest=CONTRACT,
                    fixture_digest=FIXTURE,
                    order_seed=0,
                )
        finally:
            outside.unlink(missing_ok=True)

    def test_candidate_wheel_binds_noneditable_installed_package_bytes(self) -> None:
        wheel, installed_root, fake_distribution = self.candidate_wheel_fixture()
        binding = verify_candidate_wheel_installation(
            wheel,
            _distribution=fake_distribution(),
            _import_root=installed_root / "flowmarshal",
        )
        self.assertIsInstance(binding, CandidateWheelBinding)
        self.assertFalse(binding.editable)
        self.assertEqual("flowmarshal-engine", binding.distribution_name)
        self.assertEqual("0.2.0a1", binding.distribution_version)
        self.assertEqual(binding.wheel_package_digest, binding.installed_package_digest)
        self.assertEqual(2, len(binding.package_file_digests))

    def test_candidate_wheel_rejects_source_editable_missing_and_wrong_bytes(self) -> None:
        wheel, installed_root, fake_distribution = self.candidate_wheel_fixture()
        with self.assertRaisesRegex(
            QualificationManifestError, "ABSOLUTE_PATH_REQUIRED"
        ):
            verify_candidate_wheel_installation(Path(wheel.name))
        with self.assertRaisesRegex(QualificationManifestError, "NOT_FOUND"):
            verify_candidate_wheel_installation(self.run_root / "missing.whl")
        with self.assertRaisesRegex(QualificationManifestError, "EDITABLE"):
            verify_candidate_wheel_installation(
                wheel,
                _distribution=fake_distribution(editable=True),
                _import_root=installed_root / "flowmarshal",
            )
        wrong_distribution = fake_distribution()
        wrong_distribution.version = "9.9.9"
        with self.assertRaisesRegex(
            QualificationManifestError, "DISTRIBUTION_IDENTITY_MISMATCH"
        ):
            verify_candidate_wheel_installation(
                wheel,
                _distribution=wrong_distribution,
                _import_root=installed_root / "flowmarshal",
            )
        contaminated_distribution = fake_distribution()
        contaminated_distribution.files = (
            *contaminated_distribution.files,
            "flowmarshal/source_only.py",
        )
        with self.assertRaisesRegex(
            QualificationManifestError, "PACKAGE_FILESET_MISMATCH"
        ):
            verify_candidate_wheel_installation(
                wheel,
                _distribution=contaminated_distribution,
                _import_root=installed_root / "flowmarshal",
            )
        source_import = self.run_root / "source" / "flowmarshal"
        source_import.mkdir(parents=True)
        with self.assertRaisesRegex(
            QualificationManifestError, "IMPORT_OUTSIDE_DISTRIBUTION"
        ):
            verify_candidate_wheel_installation(
                wheel,
                _distribution=fake_distribution(),
                _import_root=source_import,
            )
        (installed_root / "flowmarshal" / "module.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            QualificationManifestError, "INSTALLED_FILE_DIGEST_MISMATCH"
        ):
            verify_candidate_wheel_installation(
                wheel,
                _distribution=fake_distribution(),
                _import_root=installed_root / "flowmarshal",
            )

    def test_candidate_wheel_metadata_rejects_changed_wheel(self) -> None:
        wheel, installed_root, fake_distribution = self.candidate_wheel_fixture()
        distribution = fake_distribution()
        binding = verify_candidate_wheel_installation(
            wheel,
            _distribution=distribution,
            _import_root=installed_root / "flowmarshal",
        )
        metadata = {
            "candidate_wheel_binding": binding.model_dump(mode="json"),
            "candidate_wheel_binding_digest": binding.binding_digest,
        }
        self.assertEqual(
            binding.binding_digest,
            CandidateWheelBinding.model_validate(
                metadata["candidate_wheel_binding"]
            ).binding_digest,
        )
        with wheel.open("ab") as stream:
            stream.write(b"changed-wheel-bytes")
        with patch(
            "flowmarshal.engine.qualification_manifest.verify_candidate_wheel_installation",
            side_effect=lambda path: verify_candidate_wheel_installation(
                path,
                _distribution=distribution,
                _import_root=installed_root / "flowmarshal",
            ),
        ):
            with self.assertRaisesRegex(
                QualificationManifestError, "BINDING_CHANGED"
            ):
                verify_candidate_wheel_metadata(metadata)

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
