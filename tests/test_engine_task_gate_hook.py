from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine.domain import RunOnceAction
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]


class RecordingGate:
    def __init__(self, *, execution: str | None = None, completion: str | None = None) -> None:
        self.execution = execution
        self.completion = completion
        self.calls: list[tuple[str, str]] = []

    def before_execution(self, task):
        self.calls.append(("before_execution", task["id"]))
        return self.execution

    def before_completion(self, task):
        self.calls.append(("before_completion", task["id"]))
        return self.completion


class TaskGateHookTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        workspace, _ = _copy_fixture(ROOT, base)
        self.inventory = qualification_inventory()
        self.prepared = _prepare(workspace=workspace, state_root=base / "state", inventory=self.inventory,
                                 roles=default_role_configuration(ROOT))
        self.runtime = FakeCodexRuntime(self.inventory)

    def dispatcher(self, gate: RecordingGate) -> EngineDispatcher:
        return EngineDispatcher(self.prepared.service, self.runtime, task_gate=gate)

    def task_status(self) -> str:
        with self.prepared.service.ledger.read() as connection:
            return connection.execute("SELECT status FROM task_contracts WHERE id = ?",
                                      (self.prepared.task_id,)).fetchone()[0]

    def run_worker(self, dispatcher: EngineDispatcher) -> None:
        project_id = self.prepared.project_id
        self.assertEqual(RunOnceAction.MATERIALIZED,
                         dispatcher.run_once(project_id, proposal=self.prepared.proposal).action)
        dispatched = dispatcher.run_once(project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        with self.prepared.service.ledger.read() as connection:
            thread_id = connection.execute("SELECT thread_id FROM runtime_jobs WHERE attempt_id = ?",
                                           (dispatched.attempt_id,)).fetchone()
        thread = thread_id[0] if thread_id and thread_id[0] else next(iter(self.runtime.threads))
        (self.prepared.workspace / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n", encoding="utf-8")
        self.runtime.complete(thread, response="worker completed")
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(project_id).action)
        self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(project_id).action)

    def test_gate_blocks_before_any_attempt_is_reserved(self) -> None:
        gate = RecordingGate(execution="governance plan was not accepted")
        dispatcher = self.dispatcher(gate)
        project_id = self.prepared.project_id
        dispatcher.run_once(project_id, proposal=self.prepared.proposal)
        outcome = dispatcher.run_once(project_id)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertEqual("governance plan was not accepted", outcome.detail)
        self.assertEqual([("before_execution", self.prepared.task_id)], gate.calls)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
        self.assertEqual(0, self.runtime.create_calls)
        self.assertEqual("materialized", self.task_status())

    def test_gate_blocks_completion_before_state_reobservation(self) -> None:
        gate = RecordingGate(completion="scope verdict NEEDS_APPROVAL")
        dispatcher = self.dispatcher(gate)
        self.run_worker(dispatcher)
        with patch.object(self.prepared.service, "reobserve_project",
                          wraps=self.prepared.service.reobserve_project) as reobserve:
            outcome = dispatcher.run_once(self.prepared.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("GOVERNANCE_GATE_FAILED", outcome.blocker_code)
        reobserve.assert_not_called()
        self.assertEqual("blocked", self.task_status())
        self.assertEqual(["before_execution", "before_completion"], [name for name, _ in gate.calls])

    def test_passing_gate_leaves_completion_to_core(self) -> None:
        gate = RecordingGate()
        dispatcher = self.dispatcher(gate)
        self.run_worker(dispatcher)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(self.prepared.project_id).action)
        self.assertEqual("completed", self.task_status())
        self.assertEqual([("before_execution", self.prepared.task_id), ("before_completion", self.prepared.task_id)],
                         gate.calls)


if __name__ == "__main__":
    unittest.main()
