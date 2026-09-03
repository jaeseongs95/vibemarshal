from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.gate0b.cli import _smoke


class Gate0BCliTests(unittest.TestCase):
    def test_real_smoke_preflight_failure_creates_no_workspace_or_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "smoke"
            args = argparse.Namespace(
                root=root,
                control=Path(temporary) / "control" / "approval-capability",
                codex_bin=None,
                authoritative_codex_home=None,
                timeout=1,
            )
            with patch(
                "flowmarshal.gate0b.cli.resolve_host_context",
                return_value=SimpleNamespace(
                    project_root=Path(temporary),
                    authoritative_home=Path(temporary),
                    system_codex=Path(temporary) / "codex.exe",
                ),
            ), patch(
                "flowmarshal.gate0b.cli.query_windows_sandbox_status",
                return_value={
                    "status": "SETUP_REQUIRED",
                    "reason_code": "SANDBOX_NOT_READY",
                },
            ), patch(
                "flowmarshal.gate0b.cli.CodexAppServerRuntime"
            ) as runtime, patch("builtins.print"):
                exit_code = _smoke(args, real=True)

            results = tuple((root / "real").glob("*/smoke-result.json"))
            self.assertEqual(3, exit_code)
            self.assertEqual(1, len(results))
            self.assertFalse((results[0].parent / "workspace").exists())
            runtime.assert_not_called()

    def test_smoke_records_runtime_initialization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "smoke"
            args = argparse.Namespace(
                root=root,
                control=Path(temporary) / "control" / "approval-capability",
                codex_bin=None,
                timeout=1,
            )
            with patch(
                "flowmarshal.gate0b.cli.CodexAppServerRuntime",
                side_effect=RuntimeError("합성 초기화 실패"),
            ), patch(
                "flowmarshal.gate0b.cli.resolve_host_context",
                return_value=SimpleNamespace(
                    project_root=Path(temporary),
                    authoritative_home=Path(temporary),
                    system_codex=Path(temporary) / "codex.exe",
                ),
            ), patch(
                "flowmarshal.gate0b.cli.query_windows_sandbox_status",
                return_value={"status": "READY", "reason_code": None},
            ), patch("builtins.print"):
                exit_code = _smoke(args, real=True)

            results = tuple((root / "real").glob("*/smoke-result.json"))
            self.assertEqual(2, exit_code)
            self.assertEqual(1, len(results))
            document = json.loads(results[0].read_text(encoding="utf-8"))
            self.assertEqual("failed", document["outcome"]["status"])
            self.assertEqual(
                "SMOKE_SETUP_OR_EXECUTION_ERROR",
                document["outcome"]["reason_code"],
            )
            self.assertEqual("RuntimeError", document["error"]["type"])


if __name__ == "__main__":
    unittest.main()
