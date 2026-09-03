from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flowmarshal.windows_sandbox import (
    AUTHORITATIVE_HOME_INVALID,
    HOST_RUNTIME_NOT_SYSTEM,
    SandboxContractError,
    SandboxHostContext,
    artifact_safety_violations,
    capture_windows_vm_identity,
    manual_setup_command,
    query_windows_sandbox_status,
    resolve_host_context,
)


class HostContextTests(unittest.TestCase):
    def _layout(self, root: Path) -> tuple[Path, Path, Path]:
        project = root / "project"
        home = root / "user" / ".codex"
        codex = root / "bin" / "codex.exe"
        project.mkdir(parents=True)
        home.mkdir(parents=True)
        codex.parent.mkdir(parents=True)
        codex.write_bytes(b"test")
        return project, home, codex

    def test_nondefault_inherited_home_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, home, codex = self._layout(root)
            env = {
                "USERPROFILE": str(root / "user"),
                "CODEX_HOME": str(root / "other-home"),
                "FLOWMARSHAL_SYSTEM_CODEX": str(codex),
                "USERNAME": "tester",
            }
            with self.assertRaises(SandboxContractError) as raised:
                resolve_host_context(project_root=project, environ=env)
            self.assertEqual(AUTHORITATIVE_HOME_INVALID, raised.exception.reason_code)

    def test_project_internal_home_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, _home, codex = self._layout(root)
            internal = project / "artifacts" / "permission-home"
            internal.mkdir(parents=True)
            env = {
                "USERPROFILE": str(root / "user"),
                "FLOWMARSHAL_SYSTEM_CODEX": str(codex),
                "USERNAME": "tester",
            }
            with self.assertRaises(SandboxContractError) as raised:
                resolve_host_context(
                    project_root=project,
                    authoritative_home=internal,
                    environ=env,
                )
            self.assertEqual(AUTHORITATIVE_HOME_INVALID, raised.exception.reason_code)

    def test_requested_binary_must_match_system_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, home, codex = self._layout(root)
            other = root / "bin" / "old-codex.exe"
            other.write_bytes(b"old")
            env = {
                "USERPROFILE": str(root / "user"),
                "FLOWMARSHAL_SYSTEM_CODEX": str(codex),
                "USERNAME": "tester",
            }
            with patch(
                "flowmarshal.windows_sandbox.tempfile.gettempdir",
                return_value="Z:/unrelated-temp",
            ), self.assertRaises(SandboxContractError) as raised:
                resolve_host_context(
                    project_root=project,
                    authoritative_home=home,
                    requested_codex=other,
                    environ=env,
                )
            self.assertEqual(HOST_RUNTIME_NOT_SYSTEM, raised.exception.reason_code)

    def test_manual_setup_command_uses_authoritative_home(self) -> None:
        context = SandboxHostContext(
            Path("D:/work"),
            Path("C:/Users/test/.codex"),
            Path("C:/Program Files/Codex/codex.exe"),
            "test",
        )
        command = manual_setup_command(context)
        self.assertIn("--user 'test'", command)
        self.assertIn("--codex-home 'C:\\Users\\test\\.codex'", command)


class StatusQueryTests(unittest.TestCase):
    def test_readiness_query_never_calls_setup_start(self) -> None:
        context = SandboxHostContext(
            Path.cwd(),
            Path.cwd(),
            Path("C:/codex.exe"),
            "tester",
        )
        client = MagicMock()
        client._request_raw.return_value = {"status": "ready"}
        with patch("openai_codex.client.CodexClient", return_value=client):
            result = query_windows_sandbox_status(context, cwd=Path.cwd())
        self.assertEqual("READY", result["status"])
        methods = [call.args[0] for call in client._request_raw.call_args_list]
        self.assertEqual(["windowsSandbox/readiness"], methods)
        self.assertNotIn("windowsSandbox/setupStart", methods)


class VmIdentityTests(unittest.TestCase):
    def test_recognizes_hyper_v_guest_without_collecting_unique_id(self) -> None:
        completed = MagicMock(
            returncode=0,
            stdout='{"Manufacturer":"Microsoft Corporation","Model":"Virtual Machine","HypervisorPresent":true}',
            stderr="",
        )
        with patch("flowmarshal.windows_sandbox.os.name", "nt"), patch(
            "flowmarshal.windows_sandbox.subprocess.run",
            return_value=completed,
        ):
            result = capture_windows_vm_identity()
        self.assertTrue(result["recognized_virtual_machine"])
        self.assertNotIn("serial", {key.casefold() for key in result})

    def test_physical_machine_is_not_accepted_as_vm(self) -> None:
        completed = MagicMock(
            returncode=0,
            stdout='{"Manufacturer":"ASUS","Model":"System Product Name","HypervisorPresent":true}',
            stderr="",
        )
        with patch("flowmarshal.windows_sandbox.os.name", "nt"), patch(
            "flowmarshal.windows_sandbox.subprocess.run",
            return_value=completed,
        ):
            result = capture_windows_vm_identity()
        self.assertFalse(result["recognized_virtual_machine"])


class ArtifactSafetyTests(unittest.TestCase):
    def test_rejects_codex_secrets_auth_and_state_databases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "permission-home" / ".sandbox-secrets").mkdir(parents=True)
            (root / "auth.json").write_text("{}", encoding="utf-8")
            (root / "permission-home" / "state_5.sqlite").write_bytes(b"")
            kinds = {
                item["kind"] for item in artifact_safety_violations(root)
            }
            self.assertEqual(
                {"sandbox_secrets", "auth_file", "codex_state_database"},
                kinds,
            )


if __name__ == "__main__":
    unittest.main()
