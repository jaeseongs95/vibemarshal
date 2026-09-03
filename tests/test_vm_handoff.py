from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from flowmarshal.vm_handoff import (
    VM_HANDOFF_DESTINATION_EXISTS,
    VM_HANDOFF_TAMPERED,
    VmHandoffError,
    build_vm_bundle,
    export_vm_scenario,
    import_vm_suite,
    verify_scenario_export,
    verify_vm_bundle,
)
from flowmarshal.vm_sandbox_suite import VM_SCENARIOS, scenario_root, suite_root


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class VmHandoffBundleTests(unittest.TestCase):
    def _write_bundle_project(self, root: Path) -> None:
        for relative in (
            "README.md",
            "pyproject.toml",
            "requirements.lock",
            "docs/windows-sandbox-vm-revalidation.md",
            "docs/windows-vm-external-handoff.md",
            "src/flowmarshal/example.py",
            "scripts/windows-vm/example.ps1",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"payload:{relative}\n", encoding="utf-8")

    def test_bundle_contains_only_allowlisted_files_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            self._write_bundle_project(root)
            (root / "auth.json").write_text("secret", encoding="utf-8")
            output = Path(temporary) / "handoff.zip"

            built = build_vm_bundle(root, output)
            verified = verify_vm_bundle(output)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())

            self.assertEqual("READY_FOR_TRANSFER", built["status"])
            self.assertEqual("VALID", verified["status"])
            self.assertNotIn("auth.json", names)
            self.assertIn("bundle-manifest.json", names)
            self.assertTrue(output.with_name("handoff.zip.sha256").is_file())

    def test_bundle_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            self._write_bundle_project(root)
            output = Path(temporary) / "handoff.zip"
            build_vm_bundle(root, output)
            with zipfile.ZipFile(output, "a") as archive:
                archive.writestr("unexpected.txt", "tampered")

            with self.assertRaises(VmHandoffError) as raised:
                verify_vm_bundle(output)

            self.assertEqual(VM_HANDOFF_TAMPERED, raised.exception.reason_code)


class VmPowerShellCompatibilityTests(unittest.TestCase):
    def test_windows_vm_scripts_use_utf8_bom_for_windows_powershell_51(self) -> None:
        scripts = Path(__file__).resolve().parents[1] / "scripts" / "windows-vm"

        for script in scripts.glob("*.ps1"):
            with self.subTest(script=script.name):
                self.assertTrue(
                    script.read_bytes().startswith(b"\xef\xbb\xbf"),
                    f"{script.name} must use UTF-8 BOM for Windows PowerShell 5.1",
                )

    def test_scenario_runner_forces_python_utf8_output(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "windows-vm"
            / "Invoke-FlowMarshalVmScenario.ps1"
        ).read_text(encoding="utf-8-sig")

        self.assertIn('$env:PYTHONUTF8 = "1"', script)
        self.assertIn('$env:PYTHONIOENCODING = "utf-8"', script)
        self.assertIn('function Invoke-FlowMarshalPython', script)
        self.assertIn('$ErrorActionPreference = "Continue"', script)
        self.assertIn('$guardExitCode -ne 2', script)

    def test_guest_bootstrap_requires_codex_helper_binary(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "windows-vm"
            / "Bootstrap-FlowMarshalVmGuest.ps1"
        ).read_text(encoding="utf-8-sig")

        self.assertIn('"codex-runtime-*"', script)
        self.assertIn('"bin\\codex-code-mode-host.exe"', script)
        self.assertIn('"codex-resources\\codex-windows-sandbox-setup.exe"', script)
        self.assertIn('"codex-resources\\codex-command-runner.exe"', script)


class VmScenarioEvidenceTests(unittest.TestCase):
    def _write_guest_scenario(
        self,
        project: Path,
        suite_id: str,
        scenario: str,
    ) -> None:
        root = scenario_root(
            project / "spikes" / "gate0a" / "artifacts",
            suite_id,
            scenario,
        )
        write_json(
            root / "scenario.json",
            {
                "suite_id": suite_id,
                "scenario": scenario,
                "authoritative_codex_home": f"C:/FlowMarshal-VM/{scenario}-home",
            },
        )
        write_json(
            root / "attempts" / "attempt-01" / "setup-result.json",
            {
                "suite_id": suite_id,
                "scenario": scenario,
                "attempt_id": "attempt-01",
                "status": "completed",
            },
        )
        if scenario == "cross-version":
            write_json(
                root / "guards" / "second-home.json",
                {
                    "suite_id": suite_id,
                    "scenario": scenario,
                    "reason_code": "HOST_PROVISIONING_FORBIDDEN",
                },
            )

    def test_export_detects_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "guest"
            project.mkdir()
            evidence = base / "shared"
            self._write_guest_scenario(project, "suite-1", "current-first")
            export_vm_scenario(project, evidence, "suite-1", "current-first")
            exported = evidence / "suite-1" / "scenarios" / "current-first"
            attempt = exported / "attempts" / "attempt-01" / "setup-result.json"
            attempt.write_text("{}", encoding="utf-8")

            with self.assertRaises(VmHandoffError) as raised:
                verify_scenario_export(exported, "suite-1", "current-first")

            self.assertEqual(VM_HANDOFF_TAMPERED, raised.exception.reason_code)

    def test_three_snapshot_exports_import_once_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            evidence = base / "shared"
            suite_id = "suite-1"
            for scenario in VM_SCENARIOS:
                guest = base / f"guest-{scenario}"
                guest.mkdir()
                self._write_guest_scenario(guest, suite_id, scenario)
                result = export_vm_scenario(guest, evidence, suite_id, scenario)
                self.assertEqual("EXPORTED", result["status"])

            host = base / "host"
            host.mkdir()
            imported = import_vm_suite(host, evidence, suite_id)
            destination = suite_root(
                host / "spikes" / "gate0a" / "artifacts",
                suite_id,
            )

            self.assertEqual("IMPORTED", imported["status"])
            self.assertTrue((destination / "scenarios" / "current-first" / "scenario.json").is_file())
            self.assertFalse(any(path.name == "auth.json" for path in destination.rglob("*")))
            with self.assertRaises(VmHandoffError) as raised:
                import_vm_suite(host, evidence, suite_id)
            self.assertEqual(VM_HANDOFF_DESTINATION_EXISTS, raised.exception.reason_code)


if __name__ == "__main__":
    unittest.main()
