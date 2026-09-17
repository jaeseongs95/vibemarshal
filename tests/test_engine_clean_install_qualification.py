"""FM-11 clean 설치(E2E-18) harness와 판정기의 fail-closed 계약을 검사한다.

여기서는 실제 provider를 호출하지 않는다. 실제 candidate wheel 설치는 FM-12가
수행하며, 이 테스트는 harness가 만든 typed evidence와 최종 재검증이 tamper,
미실행, 경로 이탈, 잘못된 wheel·계약 변경을 release PASS로 만들지 않는지만
직접 확인한다.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine import e2e_qualification
from flowmarshal.engine.e2e_qualification import dry_run_project_e2e_pre_provider
from flowmarshal.engine.eval_cli import _run, build_parser
from flowmarshal.engine.clean_install_qualification import (
    CLEAN_INSTALL_CELL_ID,
    CLEAN_INSTALL_EVIDENCE_KINDS,
    CLEAN_INSTALL_ORDER_SEED,
    CLEAN_INSTALL_RESPONSIBILITY_ID,
    CLEAN_INSTALL_STAGES,
    CleanInstallError,
    CleanInstallReport,
    CleanInstallStageObservation,
    clean_install_contract_digest,
    clean_install_fixture_digest,
    clean_install_suite,
    run_clean_install,
    verify_clean_install_report,
)
from flowmarshal.engine.qualification import (
    QualificationRunError,
    qualification_suite_manifest,
    source_manifest_digest,
)
from flowmarshal.engine.qualification_manifest import (
    CandidateWheelBinding,
    EvidenceProvenance,
    QualificationCellOutcome,
    QualificationCellStatus,
    QualificationFailureClass,
    build_qualification_evidence_record,
    write_qualification_reproduction_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
BINDING_DIGEST = "sha256:" + "b" * 64
DISTRIBUTION_NAME = "flowmarshal-engine"
DISTRIBUTION_VERSION = "0.2.0a1"


def _write_bundle(destination: Path) -> str:
    """실제 동결 bundle과 같은 형식의 최소 재현 bundle을 만든다."""

    source_root = destination.parent / "bundle-source"
    source_root.mkdir(parents=True, exist_ok=True)
    evaluator = source_root / "evaluator.py"
    evaluator.write_text("value = 1\n", encoding="utf-8")
    manifest = write_qualification_reproduction_bundle(
        source_root=source_root,
        destination=destination,
        suite=qualification_suite_manifest(ROOT),
        source_files={"evaluator.py": sha256_bytes(evaluator.read_bytes())},
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
    return manifest.bundle_digest


class CleanInstallQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.run_root = self.base / "run"
        self.run_root.mkdir()
        self.bundle_digest = _write_bundle(self.run_root / "reproduction-bundle")
        self.wheel = self.run_root / "candidate-wheel" / "candidate.whl"
        self.wheel.parent.mkdir(parents=True, exist_ok=True)
        self.wheel.write_bytes(b"candidate-wheel-bytes")
        self.wheel_digest = sha256_bytes(self.wheel.read_bytes())
        for name in ("install-log.txt", "import-origin.json", "cli-output.json",
                     "candidate-probe.json", "pre-provider-dry-run.json"):
            (self.run_root / name).write_text("{}\n", encoding="utf-8")
        self.fixture_digest = clean_install_fixture_digest(
            candidate_wheel_digest=self.wheel_digest,
            freeze_bundle_digest=self.bundle_digest,
        )
        self.suite = clean_install_suite(ROOT)
        self.contract_digest = clean_install_contract_digest(
            suite_manifest_digest=self.suite.manifest_digest,
            source_manifest_digest_value=source_manifest_digest(ROOT),
            fixture_digest=self.fixture_digest,
            candidate_wheel_binding_digest=BINDING_DIGEST,
            distribution_name=DISTRIBUTION_NAME,
            distribution_version=DISTRIBUTION_VERSION,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _records(self, **changes):
        artifacts = {
            "wheel": self.wheel,
            "install_log": self.run_root / "install-log.txt",
            "import_origin": self.run_root / "import-origin.json",
            "cli_output": self.run_root / "cli-output.json",
            "bundle": self.run_root / "reproduction-bundle" / "qualification-freeze.json",
            "candidate_probe": self.run_root / "candidate-probe.json",
            "pre_provider_dry_run": self.run_root / "pre-provider-dry-run.json",
        }
        arguments = {
            "cell_id": CLEAN_INSTALL_CELL_ID,
            "evaluation_contract_digest": self.contract_digest,
            "fixture_digest": self.fixture_digest,
            "order_seed": CLEAN_INSTALL_ORDER_SEED,
            "freeze_bundle_digest": self.bundle_digest,
            "candidate_wheel_digest": self.wheel_digest,
            "candidate_wheel_binding_digest": BINDING_DIGEST,
            "candidate_distribution_name": DISTRIBUTION_NAME,
            "candidate_distribution_version": DISTRIBUTION_VERSION,
        } | changes
        return tuple(
            build_qualification_evidence_record(
                run_root=self.run_root, path=artifacts[kind], kind=kind, **arguments
            )
            for kind in CLEAN_INSTALL_EVIDENCE_KINDS
        )

    def _report(self, **changes) -> CleanInstallReport:
        records = changes.pop("records", None) or self._records()
        status = changes.pop("status", QualificationCellStatus.PASSED)
        passed = changes.pop("passed", status is QualificationCellStatus.PASSED)
        outcome = changes.pop("outcome", None) or QualificationCellOutcome(
            cell_id=CLEAN_INSTALL_CELL_ID,
            evaluation_contract_digest=self.contract_digest,
            responsibility_ids=(CLEAN_INSTALL_RESPONSIBILITY_ID,),
            status=status,
            provenance=(EvidenceProvenance.LIVE,),
            pipeline_stages=CLEAN_INSTALL_STAGES,
            evidence_kinds=tuple(
                self.suite.e2e_responsibilities[0].required_evidence_kinds
            ),
            evidence_refs=tuple(item.relative_path for item in records),
            evidence_records=records,
            failure_class=(
                None
                if status is QualificationCellStatus.PASSED
                else (
                    QualificationFailureClass.PRODUCT
                    if status is QualificationCellStatus.FAILED
                    else None
                )
            ),
            failure_code=(
                None
                if status is not QualificationCellStatus.FAILED
                else "CLEAN_INSTALL_FAILED"
            ),
        )
        body = {
            "passed": passed,
            "suite_manifest_digest": self.suite.manifest_digest,
            "source_manifest_digest": source_manifest_digest(ROOT),
            "evaluation_contract_digest": self.contract_digest,
            "fixture_digest": self.fixture_digest,
            "freeze_bundle_digest": self.bundle_digest,
            "reproduction_bundle_source": str(self.run_root / "reproduction-bundle"),
            "candidate_wheel_path": str(self.wheel),
            "candidate_wheel_digest": self.wheel_digest,
            "candidate_wheel_binding_digest": BINDING_DIGEST,
            "candidate_distribution_name": DISTRIBUTION_NAME,
            "candidate_distribution_version": DISTRIBUTION_VERSION,
            "candidate_python": str(self.run_root / "candidate-venv" / "python"),
            "observations": (
                CleanInstallStageObservation(
                    stage=stage, command=("noop",), returncode=0, passed=True
                )
                for stage in CLEAN_INSTALL_STAGES
            ),
            "outcome": outcome,
        } | changes
        return CleanInstallReport.model_validate(
            {key: value for key, value in body.items()}
        )

    def test_clean_install_suite_keeps_approved_thresholds_and_single_responsibility(
        self,
    ) -> None:
        suite = clean_install_suite(ROOT)
        full = qualification_suite_manifest(ROOT)
        self.assertEqual(suite.role_gate, full.role_gate)
        self.assertEqual(suite.planning_gate, full.planning_gate)
        self.assertEqual(
            (CLEAN_INSTALL_RESPONSIBILITY_ID,),
            tuple(item.responsibility_id for item in suite.e2e_responsibilities),
        )
        requirement = suite.e2e_responsibilities[0]
        self.assertFalse(requirement.raw_request_pipeline_required)
        self.assertLessEqual(
            set(requirement.required_evidence_kinds), set(CLEAN_INSTALL_EVIDENCE_KINDS)
        )

    def test_complete_typed_evidence_passes_final_scope_verification(self) -> None:
        verification = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=self._report()
        )
        self.assertEqual((), verification.errors)
        self.assertTrue(verification.valid)
        self.assertEqual(self.fixture_digest, verification.recalculated_fixture_digest)
        self.assertEqual(self.contract_digest, verification.recalculated_contract_digest)

    def test_tampered_and_deleted_evidence_cannot_pass(self) -> None:
        report = self._report()
        (self.run_root / "cli-output.json").write_text("{\"x\": 1}\n", encoding="utf-8")
        tampered = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=report
        )
        self.assertFalse(tampered.valid)
        self.assertTrue(
            any(item.startswith("EVIDENCE_DIGEST_MISMATCH") for item in tampered.errors),
            tampered.errors,
        )
        (self.run_root / "import-origin.json").unlink()
        removed = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=report
        )
        self.assertFalse(removed.valid)
        self.assertTrue(
            any(item.startswith("EVIDENCE_FILE_MISSING") for item in removed.errors),
            removed.errors,
        )

    def test_evidence_outside_run_root_is_rejected_at_creation(self) -> None:
        outside = self.base / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(Exception):
            build_qualification_evidence_record(
                run_root=self.run_root,
                path=outside,
                kind="cli_output",
                cell_id=CLEAN_INSTALL_CELL_ID,
                evaluation_contract_digest=self.contract_digest,
                fixture_digest=self.fixture_digest,
                order_seed=CLEAN_INSTALL_ORDER_SEED,
            )

    def test_changed_wheel_or_bundle_invalidates_the_cell_contract(self) -> None:
        report = self._report()
        self.wheel.write_bytes(b"other-wheel-bytes")
        changed = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=report
        )
        self.assertFalse(changed.valid)
        self.assertIn("CLEAN_INSTALL_CANDIDATE_WHEEL_DIGEST_MISMATCH", changed.errors)
        self.assertIn("CLEAN_INSTALL_FIXTURE_DIGEST_MISMATCH", changed.errors)
        self.assertIn("CLEAN_INSTALL_CONTRACT_DIGEST_MISMATCH", changed.errors)
        self.assertNotEqual(self.contract_digest, changed.recalculated_contract_digest)
        freeze = self.run_root / "reproduction-bundle" / "payload" / "evaluator.py"
        freeze.write_text("value = 2\n", encoding="utf-8")
        broken = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=report
        )
        self.assertIn("CLEAN_INSTALL_FREEZE_BUNDLE_INVALID", broken.errors)

    def test_wrong_wheel_binding_in_evidence_is_rejected(self) -> None:
        report = self._report(
            records=self._records(candidate_wheel_binding_digest="sha256:" + "9" * 64)
        )
        verification = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=report
        )
        self.assertFalse(verification.valid)
        self.assertTrue(
            any(
                item.startswith("EVIDENCE_WHEEL_BINDING_MISMATCH")
                for item in verification.errors
            ),
            verification.errors,
        )

    def test_legacy_string_refs_without_typed_records_cannot_pass(self) -> None:
        outcome = QualificationCellOutcome(
            cell_id=CLEAN_INSTALL_CELL_ID,
            evaluation_contract_digest=self.contract_digest,
            responsibility_ids=(CLEAN_INSTALL_RESPONSIBILITY_ID,),
            status=QualificationCellStatus.PASSED,
            provenance=(EvidenceProvenance.LIVE,),
            pipeline_stages=CLEAN_INSTALL_STAGES,
            evidence_kinds=("wheel", "install_log", "import_origin", "cli_output", "bundle"),
            evidence_refs=("install-log.txt", "cli-output.json"),
        )
        verification = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=self._report(outcome=outcome)
        )
        self.assertFalse(verification.valid)
        self.assertTrue(
            any(
                item.startswith("LEGACY_EVIDENCE_UNVERIFIED")
                for item in verification.errors
            ),
            verification.errors,
        )
        self.assertIn(
            "CLEAN_INSTALL_PASS_WITHOUT_RESPONSIBILITY_EVIDENCE", verification.errors
        )

    def test_not_run_and_failed_cells_are_not_conflated_or_promoted(self) -> None:
        not_run = verify_clean_install_report(
            root=ROOT,
            run_root=self.run_root,
            report=self._report(status=QualificationCellStatus.NOT_RUN, passed=False),
        )
        self.assertFalse(not_run.valid)
        self.assertTrue(
            any(item.startswith("NOT_RUN:") for item in not_run.errors), not_run.errors
        )
        failed = verify_clean_install_report(
            root=ROOT,
            run_root=self.run_root,
            report=self._report(status=QualificationCellStatus.FAILED, passed=False),
        )
        self.assertFalse(failed.valid)
        self.assertTrue(
            any(item.startswith("PRODUCT_FAILURE:") for item in failed.errors),
            failed.errors,
        )
        lying = verify_clean_install_report(
            root=ROOT,
            run_root=self.run_root,
            report=self._report(status=QualificationCellStatus.FAILED, passed=True),
        )
        self.assertFalse(lying.valid)
        self.assertIn("CLEAN_INSTALL_STATUS_MISMATCH", lying.errors)

    def test_source_diagnostic_contract_change_invalidates_reuse(self) -> None:
        """다른 evaluation 계약의 cell을 이 cell의 PASS 근거로 재사용하지 않는다."""

        other = clean_install_contract_digest(
            suite_manifest_digest=self.suite.manifest_digest,
            source_manifest_digest_value=source_manifest_digest(ROOT),
            fixture_digest=self.fixture_digest,
            candidate_wheel_binding_digest=BINDING_DIGEST,
            distribution_name=DISTRIBUTION_NAME,
            distribution_version="9.9.9",
        )
        self.assertNotEqual(self.contract_digest, other)
        report = self._report(evaluation_contract_digest=other)
        verification = verify_clean_install_report(
            root=ROOT, run_root=self.run_root, report=report
        )
        self.assertFalse(verification.valid)
        self.assertIn("CLEAN_INSTALL_CONTRACT_DIGEST_MISMATCH", verification.errors)
        self.assertTrue(
            any(item.startswith("CELL_CONTRACT_MISMATCH") for item in verification.errors),
            verification.errors,
        )

    def test_harness_rejects_relative_inputs_and_non_empty_run_root(self) -> None:
        with self.assertRaises(CleanInstallError):
            run_clean_install(
                root=ROOT,
                run_root=self.base / "new",
                candidate_wheel=Path("candidate.whl"),
                reproduction_bundle=self.run_root / "reproduction-bundle",
            )
        with self.assertRaises(CleanInstallError):
            run_clean_install(
                root=ROOT,
                run_root=self.run_root,
                candidate_wheel=self.wheel,
                reproduction_bundle=self.run_root / "reproduction-bundle",
            )

    def test_failed_install_keeps_typed_evidence_and_never_reports_pass(self) -> None:
        """실제 빈 venv를 만들고 설치를 실패시켜 fail-closed 경로를 관측한다.

        ``--no-index``로 의존성 해결을 막아 네트워크 없이도 결정적으로 실패한다.
        """

        wheel = self.base / "not-a-real.whl"
        wheel.write_bytes(b"not a wheel")
        destination = self.base / "failed-run"
        run_root, report = run_clean_install(
            root=ROOT,
            run_root=destination,
            candidate_wheel=wheel,
            reproduction_bundle=self.run_root / "reproduction-bundle",
            base_python=sys.executable,
            pip_install_arguments=("--no-index",),
        )
        self.assertFalse(report.passed)
        self.assertIs(report.outcome.status, QualificationCellStatus.FAILED)
        self.assertIsNotNone(report.outcome.failure_class)
        self.assertEqual(
            set(CLEAN_INSTALL_EVIDENCE_KINDS),
            {item.kind for item in report.outcome.evidence_records},
        )
        for record in report.outcome.evidence_records:
            self.assertTrue((run_root / record.relative_path).is_file())
            self.assertEqual(
                record.sha256,
                sha256_bytes((run_root / record.relative_path).read_bytes()),
            )
        saved = json.loads(
            (run_root / "clean-install-report.json").read_text(encoding="utf-8")
        )
        self.assertFalse(saved["passed"])
        verification = verify_clean_install_report(
            root=ROOT, run_root=run_root, report=report
        )
        self.assertFalse(verification.valid)
        shutil.rmtree(run_root, ignore_errors=True)


class ProjectE2EPreProviderDryRunTests(unittest.TestCase):
    """dry invocation이 실제 결속을 쓰면서 provider 전에 멈추는지 검사한다."""

    def _binding(self) -> CandidateWheelBinding:
        files = {"flowmarshal/__init__.py": "sha256:" + "1" * 64}
        digest = sha256_digest(files)
        return CandidateWheelBinding(
            wheel_path=str(ROOT / "dist" / "candidate.whl"),
            wheel_digest="sha256:" + "2" * 64,
            distribution_name=DISTRIBUTION_NAME,
            distribution_version=DISTRIBUTION_VERSION,
            distribution_root=str(ROOT / "site-packages"),
            import_root=str(ROOT / "site-packages" / "flowmarshal"),
            package_file_digests=files,
            wheel_package_digest=digest,
            installed_package_digest=digest,
        )

    def test_dry_run_requires_the_same_candidate_wheel_binding(self) -> None:
        with self.assertRaisesRegex(QualificationRunError, "CANDIDATE_WHEEL_REQUIRED"):
            dry_run_project_e2e_pre_provider(root=ROOT, candidate_wheel=None)
        with self.assertRaises(QualificationRunError):
            dry_run_project_e2e_pre_provider(root=ROOT, candidate_wheel="candidate.whl")

    def test_dry_run_reports_fake_boundary_without_provider_contact(self) -> None:
        binding = self._binding()
        with patch.object(
            e2e_qualification, "verify_candidate_wheel_installation", return_value=binding
        ):
            with patch.object(
                e2e_qualification, "CodexAppServerRuntime"
            ) as runtime:
                document = dry_run_project_e2e_pre_provider(
                    root=ROOT, candidate_wheel=ROOT / "dist" / "candidate.whl"
                )
                runtime.assert_not_called()
        self.assertEqual("pre_provider_dry_run", document["mode"])
        self.assertIs(False, document["release_pass"])
        self.assertEqual(EvidenceProvenance.FAKE.value, document["provenance"])
        self.assertEqual(binding.binding_digest, document["candidate_wheel_binding_digest"])
        self.assertEqual(list(e2e_qualification.E2E_SCENARIOS), document["scenarios"])
        self.assertIn("provider_model_list", document["stages_not_run"])
        self.assertIn("deterministic_preflight", document["stages_not_run"])

    def test_cli_rejects_dry_run_outside_project_e2e(self) -> None:
        parser = build_parser()
        arguments = parser.parse_args(
            [
                "run",
                "--scope",
                "deterministic",
                "--project-root",
                str(ROOT),
                "--pre-provider-dry-run",
            ]
        )
        with self.assertRaisesRegex(QualificationRunError, "project-e2e"):
            _run(arguments)

    def test_cli_clean_install_command_requires_wheel_and_bundle(self) -> None:
        parser = build_parser()
        arguments = parser.parse_args(
            [
                "clean-install",
                "--project-root",
                str(ROOT),
                "--candidate-wheel",
                str(ROOT / "dist" / "candidate.whl"),
                "--reproduction-bundle",
                str(ROOT / "dist" / "bundle"),
            ]
        )
        self.assertEqual("clean-install", arguments.command)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["clean-install", "--project-root", str(ROOT)])


if __name__ == "__main__":
    unittest.main()
