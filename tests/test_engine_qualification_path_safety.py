from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine.qualification import (
    QualificationRunError,
    _OPERATION_TRACE_FILENAME_BUDGET,
    _WINDOWS_LEGACY_MAX_PATH,
    _role_cell_state_root,
)


class RoleFixturePathSafetyTests(unittest.TestCase):
    def test_compact_layout_prevents_fm13_operation_trace_path_regression(self) -> None:
        run_root = Path(
            "D:/codex/fm-inspection-runtime/performance-release-floor-20260907/"
            "redesign-1.0/FM-13/role-fixture-retry"
        )
        previous = (
            run_root
            / "work"
            / "seed-17"
            / ("d" * 64)
            / "budget-state"
            / "artifacts"
            / "operation-traces"
            / "model_call_9b8d70b95963491e8d51436879ff7842.operation-trace.jsonl"
        )

        with patch.object(sys, "platform", "win32"):
            state_root = _role_cell_state_root(
                run_root,
                order_seed=17,
                catalog_index=0,
            )

        projected = (
            state_root
            / "artifacts"
            / "operation-traces"
            / ("x" * _OPERATION_TRACE_FILENAME_BUDGET)
        ).resolve()
        self.assertGreater(len(str(previous.resolve())), _WINDOWS_LEGACY_MAX_PATH)
        self.assertLessEqual(len(str(projected)), _WINDOWS_LEGACY_MAX_PATH)
        self.assertEqual(Path("w/s17/c00"), state_root.relative_to(run_root))

    def test_overlong_windows_run_root_fails_before_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / ("x" * 180)
            with patch.object(sys, "platform", "win32"):
                with self.assertRaisesRegex(
                    QualificationRunError,
                    "EVALUATION_ARTIFACT_PATH_TOO_LONG",
                ):
                    _role_cell_state_root(
                        run_root,
                        order_seed=17,
                        catalog_index=0,
                    )
            self.assertFalse(run_root.exists())


if __name__ == "__main__":
    unittest.main()
