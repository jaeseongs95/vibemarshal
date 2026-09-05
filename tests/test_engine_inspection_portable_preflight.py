"""독립 inspection preflight의 입력 결속과 portable 요약 경계 회귀."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from scripts.diagnostics import r_s06_10


class PortablePreflightTests(unittest.TestCase):
    @staticmethod
    def _roles() -> MagicMock:
        roles = MagicMock()
        roles.selection_reason = "caller_provided_explicit_role_configuration"
        roles.binding = {"format": "fixture-role-input", "digest": "sha256:" + "a" * 64}
        return roles

    @staticmethod
    def _package_binding(package: Path) -> dict:
        return {"package": str(package.resolve()), "manifest_digest": "sha256:" + "b" * 64}

    def test_preflight_binds_explicit_detached_workspace_package_binary_and_rejects_byte_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run"
            package = root / "fixture-package"
            package.mkdir()
            executable = root / "codex.exe"
            executable.write_bytes(b"initial executable")
            workspace = {"root": "D:/fixed/detached", "head": "deadbeef", "schema_version": "inspection-workspace-binding-v1"}
            package_binding = self._package_binding(package)

            with patch("scripts.diagnostics.inspection_workspace.capture_workspace_binding", return_value=workspace), patch(
                "scripts.diagnostics.inspection_inputs.verify_fixture_package", return_value=package_binding
            ):
                binding = r_s06_10.portable_preflight(
                    run, fixture_package=package, codex_bin=executable,
                    role_configuration=self._roles(), execution_mode="development-diagnostic",
                )

            stored = json.loads((run / "workspace-preflight.json").read_text(encoding="utf-8"))
            self.assertEqual(workspace, binding["workspace_binding"])
            self.assertEqual(package_binding, binding["fixture_package_binding"])
            self.assertEqual(sha256_bytes(executable.read_bytes()), binding["codex_bin_digest"])
            self.assertEqual(sha256_digest(binding), stored["binding_digest"])

            with patch("scripts.diagnostics.inspection_workspace.verify_workspace_binding") as verify_workspace, patch(
                "scripts.diagnostics.inspection_inputs.verify_fixture_package", return_value=package_binding
            ):
                r_s06_10.verify_portable_inputs(binding)
                verify_workspace.assert_called_once_with(r_s06_10.ROOT, workspace)

            executable.write_bytes(b"mutated executable")
            with patch("scripts.diagnostics.inspection_workspace.verify_workspace_binding"), patch(
                "scripts.diagnostics.inspection_inputs.verify_fixture_package", return_value=package_binding
            ), self.assertRaisesRegex(RuntimeError, "CODEX_EXECUTABLE_CHANGED"):
                r_s06_10.verify_portable_inputs(binding)

    def test_preflight_rejects_non_explicit_or_relative_inputs_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package"
            package.mkdir()
            executable = root / "codex.exe"
            executable.write_bytes(b"bin")
            roles = self._roles()
            roles.selection_reason = "implicit_default"
            with self.assertRaisesRegex(RuntimeError, "PORTABLE_ROLE_CONFIGURATION_MUST_BE_EXPLICIT"):
                r_s06_10.portable_preflight(
                    root / "run", fixture_package=package, codex_bin=executable,
                    role_configuration=roles, execution_mode="qualification",
                )
            self.assertFalse((root / "run" / "workspace-preflight.json").exists())

            with self.assertRaisesRegex(RuntimeError, "PORTABLE_INPUT_PATH_MUST_BE_ABSOLUTE"):
                r_s06_10.portable_preflight(
                    root / "relative-run", fixture_package=Path("relative-package"), codex_bin=executable,
                    role_configuration=self._roles(), execution_mode="qualification",
                )

    def test_instruction_binding_uses_current_probe_sources_without_reading_old_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run"
            instruction = root / "AGENTS.md"
            instruction.write_text("# probe instruction\n", encoding="utf-8")
            probe = root / "probe-receipt.json"
            probe.write_text(json.dumps({"payload": {"instructionSources": [str(instruction)]}}), encoding="utf-8")

            with patch.object(r_s06_10, "OLD", root / "missing-old"):
                binding = r_s06_10.instruction_binding(
                    run, observed_sources=[str(instruction)], probe_receipt=probe,
                )

            self.assertEqual("current_ephemeral_thread_probe", binding["source_observation"])
            self.assertEqual(str(instruction.resolve()), binding["sources"][0]["path"])
            self.assertEqual(sha256_bytes(instruction.read_bytes()), binding["sources"][0]["content_digest"])
            self.assertEqual(instruction.read_bytes(), (run / "instruction-sources/00-AGENTS.md").read_bytes())

    def test_unreceived_instruction_probe_blocks_prepare_before_any_new_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            intent = run / "runtime-preflight/prepare/instructions-probe.intent.json"
            r_s06_10.write_new(intent, {"ephemeral": True})
            with patch.object(r_s06_10, "CapturingRuntime") as runtime, self.assertRaisesRegex(
                RuntimeError, "INCOMPLETE_PROVIDER_INTENT_EXISTS"
            ):
                r_s06_10.prepare(run, self._roles())
            runtime.assert_not_called()
            self.assertEqual([intent], r_s06_10.incomplete_provider_intents(run))

    def test_cli_restricts_portable_options_to_preflight_prepare_and_requires_the_triplet(self):
        with self.assertRaises(SystemExit):
            r_s06_10.parse_arguments(["preflight", "--run-root", "D:/runs/inspection-a"])
        with self.assertRaises(SystemExit):
            r_s06_10.parse_arguments([
                "run", "--run-root", "D:/runs/inspection-a", "--fixture-package", "D:/fixtures/a",
                "--codex-bin", "D:/bin/codex.exe", "--role-config", "D:/roles.json",
            ])
        parsed = r_s06_10.parse_arguments([
            "preflight", "--run-root", "D:/runs/inspection-a", "--fixture-package", "D:/fixtures/a",
            "--codex-bin", "D:/bin/codex.exe", "--role-config", "D:/roles.json",
            "--execution-mode", "development-diagnostic",
        ])
        self.assertEqual("preflight", parsed.mode)
        self.assertEqual("development-diagnostic", parsed.execution_mode)

    def _portable_lock(self, run: Path, workspace_files: dict[str, str]) -> dict:
        order = list(r_s06_10.STATIC_CASES)
        return {
            "lock_digest": "sha256:" + "1" * 64,
            "source_manifest_digest": "sha256:" + "2" * 64,
            "original_files": {}, "execution_mode": "development-diagnostic",
            "call_order": order, "maximum_logical_calls": len(order), "maximum_provider_turns": len(order),
            "workspace_binding": {"portable": True},
            "fixture_package_binding": {"package": "D:/fixture", "manifest_digest": "sha256:" + "3" * 64},
            "codex_bin": "D:/bin/codex.exe", "codex_bin_digest": "sha256:" + "4" * 64,
            "workspace_files": workspace_files,
        }

    def test_portable_summary_avoids_old_history_and_fails_on_its_own_workspace_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for mutated in (False, True):
                with self.subTest(mutated=mutated):
                    run = root / ("mutated" if mutated else "clean")
                    workspace = run / "workspace"
                    workspace.mkdir(parents=True)
                    target = workspace / "app.py"
                    target.write_text("VALUE = 1\n", encoding="utf-8")
                    expected_files = r_s06_10.files(workspace)
                    if mutated:
                        target.write_text("VALUE = 2\n", encoding="utf-8")
                    lock = self._portable_lock(run, expected_files)
                    write = r_s06_10.write_new
                    write(run / "preflight.json", lock)
                    write(run / "instruction-binding.json", {"sources": []})

                    with patch("scripts.diagnostics.r_s06_10.source_manifest_digest", return_value="sha256:" + "2" * 64), patch(
                        "scripts.diagnostics.r_s06_10.verify_portable_inputs"
                    ), patch("scripts.diagnostics.r_s06_10.preserved_files", side_effect=AssertionError("OLD history read")), patch.object(
                        r_s06_10, "OLD", root / "must-not-be-read"
                    ):
                        summary = r_s06_10.summarize(run, "PASS")

                    self.assertEqual("PASS" if not mutated else "FAIL", summary["status"])
                    self.assertEqual(not mutated, summary["checks"]["workspace_unchanged"])
                    self.assertTrue(summary["checks"]["isolated_inputs_unchanged"])


if __name__ == "__main__":
    unittest.main()
