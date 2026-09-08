from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.domain import new_id
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.roles import CodexStructuredRoleRunner
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.runtime import FakeCodexRuntime
from flowmarshal.engine.qualification import (
    QualificationRunError,
    _OPERATION_TRACE_FILENAME_BUDGET,
    _WINDOWS_LEGACY_MAX_PATH,
    _planning_cell_work_root,
    run_full_planning_pipeline,
)


class PlanningPathSafetyTests(unittest.TestCase):
    def test_compact_layout_keeps_operation_trace_inside_windows_path_budget(self) -> None:
        run_root = Path(
            "D:/codex/fm-inspection-runtime/performance-release-floor-20260907/"
            "redesign-1.0/REC-FM-13-a1101dd9eb77ace0-1/planning-attempt-3"
        )
        previous = (
            run_root
            / "work"
            / "seed-17"
            / "S01-single-bugfix"
            / "budget-state"
            / "artifacts"
            / "operation-traces"
            / "model_call_73327491e5f6448d8b2ec4d2a78e1899.operation-trace.jsonl"
        )

        with patch.object(sys, "platform", "win32"):
            work_root = _planning_cell_work_root(
                run_root,
                order_seed=17,
                catalog_index=0,
            )

        projected = (
            work_root
            / "budget-state"
            / "artifacts"
            / "operation-traces"
            / ("x" * _OPERATION_TRACE_FILENAME_BUDGET)
        ).resolve()
        self.assertGreater(len(str(previous.resolve())), _WINDOWS_LEGACY_MAX_PATH)
        self.assertLessEqual(len(str(projected)), _WINDOWS_LEGACY_MAX_PATH)
        self.assertEqual(Path("p/s17/c00"), work_root.relative_to(run_root))

    def test_trace_budget_matches_actual_runner_filename(self) -> None:
        from tests.test_engine_qualification import qualification_inventory

        runner = CodexStructuredRoleRunner(
            FakeCodexRuntime(qualification_inventory()), operation_trace_path=Path("traces"),
        )
        trace_path = runner._trace_path(new_id("model_call"))
        self.assertEqual(len(trace_path.name), _OPERATION_TRACE_FILENAME_BUDGET)

    def test_windows_path_limit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            short = Path(temporary) / "run"
            with patch.object(sys, "platform", "win32"):
                root = _planning_cell_work_root(short, order_seed=89, catalog_index=5)
                probe = (root / "budget-state/artifacts/operation-traces"
                         / ("x" * _OPERATION_TRACE_FILENAME_BUDGET)).resolve()
                at_limit = short.with_name(
                    short.name + "x" * (_WINDOWS_LEGACY_MAX_PATH - len(str(probe)))
                )
                _planning_cell_work_root(at_limit, order_seed=89, catalog_index=5)
                with self.assertRaisesRegex(QualificationRunError, "projected_length=260"):
                    _planning_cell_work_root(
                        at_limit.with_name(at_limit.name + "x"),
                        order_seed=89, catalog_index=5,
                    )
            self.assertFalse(at_limit.exists())

    def test_pipeline_rejects_overlong_root_before_artifact_initialization(self) -> None:
        from tests.test_engine_qualification import ROOT, qualification_inventory

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / ("x" * 180)
            with (
                patch("flowmarshal.engine.qualification._preflight", return_value=()),
                patch("flowmarshal.engine.qualification.CodexAppServerRuntime") as runtime,
                patch.object(sys, "platform", "win32"),
            ):
                runtime.return_value.__enter__.return_value.list_models.return_value = (
                    qualification_inventory()
                )
                with self.assertRaisesRegex(QualificationRunError, "EVALUATION_ARTIFACT_PATH_TOO_LONG"):
                    run_full_planning_pipeline(
                        root=ROOT, run_root=run_root,
                        evaluation_policies=EvaluationPolicies(
                            budget=GoalBudgetPolicy(
                                total_tokens=1_000_000, call_reservation_tokens=100_000,
                            ),
                            role_timeouts=RoleTimeoutPolicy(),
                        ),
                    )
                runtime.return_value.__enter__.return_value.create_thread.assert_not_called()
                runtime.return_value.__enter__.return_value.start_turn.assert_not_called()
            self.assertFalse(run_root.exists())

    def test_overlong_windows_run_root_fails_before_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / ("x" * 180)
            with patch.object(sys, "platform", "win32"):
                with self.assertRaisesRegex(
                    QualificationRunError,
                    "EVALUATION_ARTIFACT_PATH_TOO_LONG",
                ):
                    _planning_cell_work_root(
                        run_root,
                        order_seed=17,
                        catalog_index=0,
                    )
            self.assertFalse(run_root.exists())


if __name__ == "__main__":
    unittest.main()
