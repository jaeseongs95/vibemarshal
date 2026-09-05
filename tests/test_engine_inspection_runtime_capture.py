from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.diagnostics.r_s06_10 import (
    CAPTURE_PHASES,
    CapturingRuntime,
    claim_runtime_phase,
    execute,
    write_new,
)


class InspectionRuntimeCaptureTests(unittest.TestCase):
    @staticmethod
    def _runtime(run: Path, phase: str, capture: Path) -> CapturingRuntime:
        runtime = object.__new__(CapturingRuntime)
        runtime.run = run
        runtime.phase = phase
        runtime.capture = capture
        runtime.indices = {}
        return runtime

    def test_prepare_and_run_keep_separate_inventory_raw_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            prepare_capture = claim_runtime_phase(run, "prepare")
            prepare = self._runtime(run, "prepare", prepare_capture)
            prepare.record("inventory", {"models": ["prepare-only"]})
            prepared = (prepare_capture / "inventory-01.json").read_bytes()

            run_capture = claim_runtime_phase(run, "run")
            runtime = self._runtime(run, "run", run_capture)
            runtime.record("inventory", {"models": ["run-only"]})

            self.assertEqual(prepared, (prepare_capture / "inventory-01.json").read_bytes())
            self.assertEqual(b'{"models":["run-only"]}\n', (run_capture / "inventory-01.json").read_bytes())
            self.assertEqual({"prepare", "run"}, {path.name for path in (run / "runtime-preflight").iterdir()})

    def test_call_capture_restores_phase_for_the_next_preflight_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            phase_capture = claim_runtime_phase(run, "run")
            runtime = self._runtime(run, "run", phase_capture)
            call_capture = run / "calls/01-compact_plan_reviewer"
            call_capture.mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                with runtime.call_capture(call_capture):
                    runtime.record("policy", {"scope": "call"})
                    raise RuntimeError("fixture failure")
            runtime.record("inventory", {"scope": "next case"})

            self.assertEqual(phase_capture, runtime.capture)
            self.assertTrue((call_capture / "policy-01.json").is_file())
            self.assertTrue((phase_capture / "inventory-01.json").is_file())
            self.assertFalse((call_capture / "inventory-01.json").exists())

    def test_same_phase_records_are_append_only_and_collisions_never_skip_raw_files(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            capture = claim_runtime_phase(run, "review-generated")
            runtime = self._runtime(run, "review-generated", capture)
            runtime.record("inventory", {"number": 1})
            runtime.record("inventory", {"number": 2})
            self.assertEqual(["inventory-01.json", "inventory-02.json"],
                             sorted(path.name for path in capture.glob("inventory-*.json")))

            original = (capture / "inventory-01.json").read_bytes()
            duplicate = self._runtime(run, "review-generated", capture)
            with self.assertRaises(FileExistsError):
                duplicate.record("inventory", {"number": "duplicate"})
            self.assertEqual({}, duplicate.indices)
            self.assertEqual(original, (capture / "inventory-01.json").read_bytes())

        for raw in (b"", b'{"truncated"'):
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as temp:
                    run = Path(temp)
                    capture = run / "runtime-preflight/run"
                    capture.mkdir(parents=True)
                    path = capture / "inventory-01.json"
                    path.write_bytes(raw)
                    runtime = self._runtime(run, "run", capture)
                    with self.assertRaises(FileExistsError):
                        runtime.record("inventory", {"must_not": "skip to 02"})
                    self.assertEqual({}, runtime.indices)
                    self.assertEqual(raw, path.read_bytes())
                    self.assertFalse((capture / "inventory-02.json").exists())

    def test_concurrent_phase_claim_has_one_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            barrier = threading.Barrier(2)
            outcomes = []
            outcome_lock = threading.Lock()

            def claim() -> None:
                barrier.wait()
                try:
                    capture = claim_runtime_phase(run, "run")
                    outcome = ("claimed", capture)
                except Exception as error:  # 경쟁 실행의 원래 오류를 검사한다.
                    outcome = ("failed", error)
                with outcome_lock:
                    outcomes.append(outcome)

            workers = [threading.Thread(target=claim) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            claimed = [value for kind, value in outcomes if kind == "claimed"]
            failed = [value for kind, value in outcomes if kind == "failed"]
            self.assertEqual(1, len(claimed))
            self.assertEqual(1, len(failed))
            self.assertIsInstance(failed[0], FileExistsError)
            self.assertEqual(b'{"phase":"run"}\n', (run / "runtime-preflight/run/phase-claim.json").read_bytes())
            self.assertEqual(("prepare", "run", "review-generated"), CAPTURE_PHASES)

    def test_reexecution_claim_or_incomplete_intent_stops_before_runtime(self):
        for artifact in ("execution-started.json", "calls/01-role/thread.intent.json", "calls/01-role/turn.intent.json"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as temp:
                run = Path(temp)
                write_new(run / artifact, {"fixture": artifact})
                with patch("scripts.diagnostics.r_s06_10.verify_lock", return_value={"lock_digest": "fixture"}), patch(
                    "scripts.diagnostics.r_s06_10.CapturingRuntime"
                ) as runtime:
                    expected = FileExistsError if artifact == "execution-started.json" else RuntimeError
                    with self.assertRaises(expected):
                        execute(run)
                runtime.assert_not_called()
                self.assertFalse((run / "summary.json").exists())
                if artifact != "execution-started.json":
                    self.assertFalse((run / "execution-started.json").exists())


if __name__ == "__main__":
    unittest.main()
