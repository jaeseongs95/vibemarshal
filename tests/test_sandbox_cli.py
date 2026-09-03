from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.windows_sandbox import SandboxFingerprint, SandboxHostContext
from flowmarshal_gate0a.probe import (
    run_probe,
    setup_permission_sandbox,
    verify_permission,
)


def complete_fingerprint() -> SandboxFingerprint:
    return SandboxFingerprint(
        accounts=(),
        accounts_error=None,
        setup_marker={"exists": True, "error": None},
        secrets_directory={"exists": True, "error": None},
    )


def clean_vm_fingerprint() -> SandboxFingerprint:
    return SandboxFingerprint(
        accounts=(
            {"Name": "CodexSandboxOffline", "Missing": True},
            {"Name": "CodexSandboxOnline", "Missing": True},
        ),
        accounts_error=None,
        setup_marker={"exists": False, "error": None},
        secrets_directory={"exists": False, "error": None},
    )


class SandboxCliSafetyTests(unittest.TestCase):
    def test_host_setup_is_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = SandboxHostContext(
                root,
                root.parent / ".codex",
                root / "codex.exe",
                "tester",
            )
            args = argparse.Namespace(
                project_root=str(root),
                runtime="system",
                mode="elevated",
                timeout=1,
                environment="host",
                authoritative_codex_home=None,
            )
            with patch(
                "flowmarshal_gate0a.probe.resolve_host_context",
                return_value=context,
            ), patch("flowmarshal_gate0a.probe.make_client") as make_client:
                exit_code = setup_permission_sandbox(args)
            self.assertEqual(3, exit_code)
            make_client.assert_not_called()
            self.assertFalse((root / "spikes").exists())

    def test_vm_setup_rejects_second_authoritative_home_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = (
                root
                / "spikes"
                / "gate0a"
                / "artifacts"
                / "vm-harness"
                / "suites"
                / "suite-1"
                / "scenarios"
                / "cross-version"
                / "scenario.json"
            )
            claim.parent.mkdir(parents=True)
            claim.write_text(
                json.dumps({"authoritative_codex_home": str(root / "first-home")}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                project_root=str(root),
                runtime="system",
                mode="elevated",
                timeout=1,
                environment="disposable-vm",
                authoritative_codex_home=root / "second-home",
                vm_suite_id="suite-1",
                vm_scenario="cross-version",
                vm_attempt_id="second-home",
                simulate_setup_failure=False,
            )
            with patch.dict(
                os.environ,
                {"FLOWMARSHAL_DISPOSABLE_WINDOWS_VM": "1"},
            ), patch("flowmarshal_gate0a.probe.make_client") as make_client:
                exit_code = setup_permission_sandbox(args)
            self.assertEqual(2, exit_code)
            make_client.assert_not_called()
            guard = claim.parent / "guards" / "second-home.json"
            self.assertTrue(guard.exists())
            evidence = json.loads(guard.read_text(encoding="utf-8"))
            self.assertFalse(evidence["runtime_started"])
            self.assertFalse(evidence["setup_api_called"])

    def test_vm_flags_on_physical_host_do_not_start_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(
                project_root=str(root),
                runtime="system",
                mode="elevated",
                timeout=1,
                environment="disposable-vm",
                authoritative_codex_home=root / "vm-home",
                vm_suite_id="suite-1",
                vm_scenario="current-first",
                vm_attempt_id="current-first-01",
                simulate_setup_failure=False,
            )
            with patch.dict(
                os.environ,
                {"FLOWMARSHAL_DISPOSABLE_WINDOWS_VM": "1"},
            ), patch(
                "flowmarshal_gate0a.probe.capture_sandbox_fingerprint",
                return_value=clean_vm_fingerprint(),
            ), patch(
                "flowmarshal_gate0a.probe.capture_windows_vm_identity",
                return_value={
                    "recognized_virtual_machine": False,
                    "manufacturer": "ASUS",
                    "model": "System Product Name",
                    "hypervisor_present": True,
                    "error": None,
                },
            ), patch("flowmarshal_gate0a.probe.make_client") as make_client:
                exit_code = setup_permission_sandbox(args)
            self.assertEqual(2, exit_code)
            make_client.assert_not_called()
            contract = (
                root
                / "spikes"
                / "gate0a"
                / "artifacts"
                / "vm-harness"
                / "suites"
                / "suite-1"
                / "scenarios"
                / "current-first"
                / "scenario.json"
            )
            self.assertFalse(contract.exists())

    def test_permission_preflight_failure_creates_no_canary_or_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = SandboxHostContext(
                root,
                root.parent / ".codex",
                root / "codex.exe",
                "tester",
            )
            args = argparse.Namespace(
                project_root=str(root),
                runtime="system",
                authoritative_codex_home=None,
                recheck_id=None,
                surface_thread_id=None,
                record_only=False,
            )
            with patch(
                "flowmarshal_gate0a.probe.resolve_host_context",
                return_value=context,
            ), patch(
                "flowmarshal_gate0a.probe.capture_sandbox_fingerprint",
                return_value=complete_fingerprint(),
            ), patch(
                "flowmarshal_gate0a.probe.query_windows_sandbox_status",
                return_value={"status": "SETUP_REQUIRED", "reason_code": "SANDBOX_NOT_READY"},
            ), patch("flowmarshal_gate0a.probe.run_permission_recheck") as recheck:
                exit_code = verify_permission(args)
            self.assertEqual(3, exit_code)
            recheck.assert_not_called()
            self.assertFalse((root / "spikes").exists())

    def test_gate_run_preflight_failure_starts_no_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = SandboxHostContext(
                root,
                root.parent / ".codex",
                root / "codex.exe",
                "tester",
            )
            args = argparse.Namespace(
                project_root=str(root),
                runtime="system",
                authoritative_codex_home=None,
                run_id=None,
                turn_timeout=1,
            )
            with patch(
                "flowmarshal_gate0a.probe.resolve_host_context",
                return_value=context,
            ), patch(
                "flowmarshal_gate0a.probe.capture_sandbox_fingerprint",
                return_value=complete_fingerprint(),
            ), patch(
                "flowmarshal_gate0a.probe.query_windows_sandbox_status",
                return_value={"status": "SETUP_REQUIRED", "reason_code": "SANDBOX_NOT_READY"},
            ), patch("flowmarshal_gate0a.probe.probe_runtime") as runtime:
                exit_code = run_probe(args)
            self.assertEqual(3, exit_code)
            runtime.assert_not_called()
            self.assertFalse((root / "spikes").exists())


if __name__ == "__main__":
    unittest.main()
