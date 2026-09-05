"""development-diagnostic의 독립 사례 수집 경계 회귀."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from unittest.mock import MagicMock, patch

from flowmarshal.engine.roles import StructuredRoleError
from scripts.diagnostics.r_s06_10 import STATIC_CASES, execute, write_new
from tests import test_engine_inspection_summary as inspection_summary


class InspectionCollectAllTests(unittest.TestCase):
    """실제 summary artifact 판독을 재사용하되 provider 경계만 모의한다."""

    def setUp(self) -> None:
        self.artifacts = inspection_summary.InspectionSummaryTests(methodName="runTest")
        self.artifacts.setUp()
        self.request = self.artifacts.request

    def _lock(self, mode: str) -> dict:
        order = STATIC_CASES if mode == "development-diagnostic" else (*STATIC_CASES, "expansion", "expanded-review")
        return {
            "lock_digest": "sha256:" + "1" * 64,
            "source_manifest_digest": "sha256:" + "2" * 64,
            "original_files": {},
            "execution_mode": mode,
            "call_order": list(order),
            "maximum_logical_calls": len(order),
            "maximum_provider_turns": len(order),
            "operational_binding": self.request.operational_binding.model_dump(mode="json"),
            "codex_bin": "unused",
        }

    def _run(self, run: Path, lock: dict) -> None:
        write_new(run / "preflight.json", lock)
        write_new(run / "instruction-binding.json", {"sources": []})
        write_new(run / "roles.json", {})
        (run / "workspace").mkdir()
        for name in STATIC_CASES:
            write_new(run / "requests" / f"{name}.json", self.request)

    @contextmanager
    def _patches(self, run: Path, lock: dict, invoke):
        runtime = MagicMock()
        runtime_context = MagicMock()
        runtime_context.__enter__.return_value = runtime
        with ExitStack() as stack:
            stack.enter_context(patch.multiple(
                "scripts.diagnostics.r_s06_10",
                verify_lock=MagicMock(return_value=lock),
                CapturingRuntime=MagicMock(return_value=runtime_context),
                invoke=MagicMock(side_effect=invoke),
                case_expectation=MagicMock(return_value={}),
                assess_case_inspection_review=MagicMock(return_value={"passed": True}),
                source_manifest_digest=MagicMock(return_value="sha256:" + "2" * 64),
                preserved_files=MagicMock(return_value={}),
                files=MagicMock(return_value={}),
            ))
            stack.enter_context(patch(
                "scripts.diagnostics.r_s06_10.EngineRoleConfiguration.model_validate",
                return_value=MagicMock(),
            ))
            yield

    def _schema_failure(self, run: Path, names: list[str], *, omit_usage=False):
        def invoke(name, runner, *_args):
            names.append(name)
            capture, _ = self.artifacts._call(run, len(names), status="failure")
            if omit_usage:
                terminal = json.loads((capture / "terminal.json").read_text(encoding="utf-8"))
                terminal["payload"].pop("usage")
                (capture / "terminal.json").write_text(json.dumps(terminal), encoding="utf-8")
                failed = json.loads((capture / "failed.json").read_text(encoding="utf-8"))
                failed["receipts"][0]["usage_available"] = False
                (capture / "failed.json").write_text(json.dumps(failed), encoding="utf-8")
            raise StructuredRoleError("structured output이 유효하지 않습니다. schema recovery 0회")
        return invoke

    def _provider_terminal_failure(self, run: Path, names: list[str], *, terminal_status="failed", mutation=None):
        def invoke(name, _runner, *_args):
            names.append(name)
            capture, _ = self.artifacts._call(run, len(names), status="failure")
            terminal = json.loads((capture / "terminal.json").read_text(encoding="utf-8"))
            terminal["terminal_status"] = terminal_status
            (capture / "terminal.json").write_text(json.dumps(terminal), encoding="utf-8")
            failed = json.loads((capture / "failed.json").read_text(encoding="utf-8"))
            failed["receipts"][0]["status"] = "failed"
            (capture / "failed.json").write_text(json.dumps(failed), encoding="utf-8")
            if mutation is not None:
                mutation(capture)
            raise StructuredRoleError("role turn이 완료되지 않았습니다: terminal_status=failed")
        return invoke

    def test_diagnostic_collects_all_completed_schema_failures_and_usage_null_is_not_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            lock = self._lock("development-diagnostic")
            self._run(run, lock)
            names: list[str] = []
            with self._patches(run, lock, self._schema_failure(run, names, omit_usage=True)):
                self.assertIsNone(execute(run))

            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(list(STATIC_CASES), names)
            self.assertEqual(11, summary["logical_calls"])
            self.assertTrue(summary["collection_complete"])
            self.assertEqual("FAIL", summary["status"])
            self.assertEqual({"failure"}, {item["outcome"] for item in summary["call_artifacts"]})
            self.assertEqual(["FAIL"] * 11, [item["status"] for item in summary["case_results"]])
            self.assertTrue(all(item["failure_kind"] == "model_output" for item in summary["case_results"]))
            self.assertTrue(all(item["semantic_evaluated"] is False for item in summary["case_results"]))
            self.assertEqual(0, summary["outcomes"]["external_unknown"])
            self.assertIsNone(summary["usage"]["total_tokens"])

            with self.assertRaisesRegex(RuntimeError, "반복하지 않습니다"):
                execute(run)

    def test_qualification_stops_after_first_completed_schema_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            lock = self._lock("qualification")
            self._run(run, lock)
            names: list[str] = []
            with self._patches(run, lock, self._schema_failure(run, names)):
                self.assertIsNone(execute(run))

            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([STATIC_CASES[0]], names)
            self.assertFalse(summary["collection_complete"])
            self.assertEqual("FAIL", summary["case_results"][0]["status"])
            self.assertEqual("NOT_RUN", summary["case_results"][1]["status"])

    def test_verified_provider_terminal_failure_stops_diagnostic_before_next_case(self):
        for terminal_status in ("failed", "interrupted"):
            with self.subTest(terminal_status=terminal_status), tempfile.TemporaryDirectory() as temp:
                run = Path(temp)
                lock = self._lock("development-diagnostic")
                self._run(run, lock)
                names: list[str] = []
                with self._patches(run, lock, self._provider_terminal_failure(
                        run, names, terminal_status=terminal_status)):
                    self.assertIsNone(execute(run))

                summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual([STATIC_CASES[0]], names)
                self.assertEqual("provider_terminal_failed", summary["case_results"][0]["failure_kind"])
                self.assertFalse(summary["collection_complete"])
                call = summary["call_artifacts"][0]
                self.assertEqual("failure", call["outcome"])
                self.assertEqual("provider_terminal_failed", call["failure_kind"])
                self.assertTrue(call["common_binding"]["passed"])
                self.assertTrue(call["common_binding"]["checks"]["receipt_status"])

    def test_missing_active_or_mismatched_provider_terminal_is_unknown_and_stops(self):
        def missing_terminal(capture: Path) -> None:
            (capture / "terminal.json").unlink()

        def active_terminal(capture: Path) -> None:
            terminal = json.loads((capture / "terminal.json").read_text(encoding="utf-8"))
            terminal["active"] = True
            (capture / "terminal.json").write_text(json.dumps(terminal), encoding="utf-8")

        def mismatched_terminal(capture: Path) -> None:
            terminal = json.loads((capture / "terminal.json").read_text(encoding="utf-8"))
            terminal["payload"]["thread_id"] = "another-thread"
            (capture / "terminal.json").write_text(json.dumps(terminal), encoding="utf-8")

        for label, mutation in (("missing", missing_terminal), ("active", active_terminal),
                                ("binding_mismatch", mismatched_terminal)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                run = Path(temp)
                lock = self._lock("development-diagnostic")
                self._run(run, lock)
                names: list[str] = []
                with self._patches(run, lock, self._provider_terminal_failure(run, names, mutation=mutation)):
                    self.assertIsNone(execute(run))

                summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual([STATIC_CASES[0]], names)
                self.assertEqual("external_unknown", summary["case_results"][0]["failure_kind"])
                self.assertFalse(summary["collection_complete"])
                self.assertEqual("external_unknown", summary["call_artifacts"][0]["outcome"])

    def test_ambiguous_receipt_stops_diagnostic_before_next_case(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            lock = self._lock("development-diagnostic")
            self._run(run, lock)
            names: list[str] = []

            def ambiguous_failure(name, _runner, *_args):
                names.append(name)
                capture, _ = self.artifacts._call(run, 1, status="failure")
                failed = json.loads((capture / "failed.json").read_text(encoding="utf-8"))
                failed["receipts"].append(self.artifacts._receipt(
                    call_id="unattributed", thread_id="other-thread", turn_id="other-turn", status="schema_failed"
                ))
                (capture / "failed.json").write_text(json.dumps(failed), encoding="utf-8")
                raise StructuredRoleError("structured output이 유효하지 않습니다")

            with self._patches(run, lock, ambiguous_failure):
                self.assertIsNone(execute(run))

            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([STATIC_CASES[0]], names)
            self.assertIn("UNATTRIBUTED_RECEIPT", [item["code"] for item in summary["diagnostic_errors"]])
            self.assertEqual("external_unknown", summary["case_results"][0]["failure_kind"])
            self.assertFalse(summary["collection_complete"])

    def test_lock_failure_starts_no_provider_case(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            lock = self._lock("development-diagnostic")
            self._run(run, lock)
            invoke = MagicMock()
            with patch("scripts.diagnostics.r_s06_10.verify_lock", side_effect=RuntimeError("SOURCE_LOCK_CHANGED")), patch(
                "scripts.diagnostics.r_s06_10.invoke", invoke
            ):
                with self.assertRaisesRegex(RuntimeError, "SOURCE_LOCK_CHANGED"):
                    execute(run)
            invoke.assert_not_called()
            self.assertFalse((run / "execution-started.json").exists())

    def test_semantic_failure_keeps_collecting_later_independent_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            lock = self._lock("development-diagnostic")
            self._run(run, lock)
            names: list[str] = []

            def success(name, runner, *_args):
                names.append(name)
                self.artifacts._call(run, len(names), status="success")
                runner.last_result = SimpleNamespace(payload={})
                return {}

            def assessment(_envelope, _expectation, *, case_id, **_kwargs):
                return {"passed": case_id != STATIC_CASES[0]}

            with self._patches(run, lock, success), patch(
                "scripts.diagnostics.r_s06_10.PlanReviewEnvelope.model_validate", return_value=MagicMock()
            ), patch("scripts.diagnostics.r_s06_10.assess_case_inspection_review", side_effect=assessment):
                self.assertIsNone(execute(run))

            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(list(STATIC_CASES), names)
            self.assertTrue(summary["collection_complete"])
            self.assertEqual("semantic", summary["case_results"][0]["failure_kind"])
            self.assertTrue(summary["case_results"][0]["semantic_evaluated"])
            self.assertEqual("PASS", summary["case_results"][-1]["status"])

    def test_diagnostic_rejects_generated_review_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            lock = self._lock("development-diagnostic")
            self._run(run, lock)
            with patch("scripts.diagnostics.r_s06_10.verify_lock", return_value=lock), patch(
                "scripts.diagnostics.r_s06_10.invoke"
            ) as invoke:
                with self.assertRaisesRegex(RuntimeError, "GENERATED_REVIEW_NOT_IN_STATIC_DIAGNOSTIC"):
                    execute(run, generated=True)
            invoke.assert_not_called()
            self.assertFalse((run / "generated-review-started.json").exists())


if __name__ == "__main__":
    unittest.main()
