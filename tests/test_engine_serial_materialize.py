"""같은 프로젝트의 직렬 materialize와 materialize 뒤 STALE 전환을 고정한다."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flowmarshal.engine.domain import RunOnceAction, ValidationExecutionStep
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from tests.fixtures.engine.governance import multitask
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]
IN_FLIGHT = {"materialized", "reserved", "running", "validating"}


def _replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


EDITS = {
    "task_fix_add": lambda root: _replace(root / "app.py", "left - right", "left + right"),
    "task_fix_shout": lambda root: _replace(root / "text.py", "text.lower()", "text.upper()"),
}


class SerialMaterializeTests(unittest.TestCase):
    """서로 독립인 ready Task 2개가 있어도 한 번에 하나만 준비되는지 확인한다.

    multitask fixture는 이 결함을 피하려고 control 의존성으로 한 줄 chain을 만든다.
    여기서는 fixture 파일을 고치지 않고 모듈 상수만 dependency 없는 2 Task로 바꿔
    실제 병렬 ready 상황을 만든다.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        steps = multitask.STEPS[:2]
        for name, value in (
            ("STEPS", steps),
            ("EDGES", ()),
            ("STATEMENTS", {
                step.criterion: multitask.STATEMENTS[step.criterion] for step in steps
            }),
        ):
            patcher = mock.patch.object(multitask, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.inventory = qualification_inventory()
        self.workspace = multitask.copy_fixture(base)
        self.prepared = multitask.prepare(
            workspace=self.workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=default_role_configuration(ROOT),
        )
        self.runtime = FakeCodexRuntime(self.inventory)
        self.dispatcher = EngineDispatcher(
            self.prepared.service,
            self.runtime,
            proposal_provider=multitask.MultitaskProposals(self.prepared),
        )
        self.refs = {task_id: ref for ref, task_id in self.prepared.task_ids.items()}

    def _goal_step(self) -> ValidationExecutionStep:
        return ValidationExecutionStep(
            validation_id="validation_goal", method="deterministic",
            argv=(sys.executable, "-m", "unittest", "test_app", "test_text"),
            working_directory=str(self.workspace), timeout_seconds=60,
            expected_exit_codes=(0,), required_evidence_kinds=("test",))

    def _statuses(self) -> dict[str, str]:
        with self.prepared.service.ledger.read() as connection:
            return {
                ref: connection.execute(
                    "SELECT status FROM task_contracts WHERE id = ?", (task_id,)
                ).fetchone()[0]
                for ref, task_id in self.prepared.task_ids.items()
            }

    def _spec_map_digests(self) -> dict[str, str]:
        with self.prepared.service.ledger.read() as connection:
            rows = {
                ref: connection.execute(
                    "SELECT payload_json FROM execution_spec_revisions "
                    "WHERE task_id = ? AND is_current = 1",
                    (task_id,),
                ).fetchone()[0]
                for ref, task_id in self.prepared.task_ids.items()
            }
        return {
            ref: json.loads(payload)["definition"]["project_map_digest"]
            for ref, payload in rows.items()
        }

    def _tick(self, *, work: bool = True):
        outcome = self.dispatcher.run_once(
            self.prepared.project_id, goal_validation_step=self._goal_step(),
        )
        if work and outcome.action is RunOnceAction.DISPATCHED and outcome.attempt_id is not None:
            EDITS[self.refs[outcome.task_id]](self.workspace)
            self.runtime.complete(list(self.runtime.threads)[-1], response="worker completed")
        return outcome

    def test_independent_ready_tasks_materialize_one_at_a_time(self) -> None:
        materialized_order: list[str] = []
        for _ in range(60):
            outcome = self._tick()
            # Goal Test binding의 MATERIALIZED에는 task_id가 없다.
            if outcome.action is RunOnceAction.MATERIALIZED and outcome.task_id is not None:
                materialized_order.append(self.refs[outcome.task_id])
            statuses = self._statuses()
            in_flight = [ref for ref, status in statuses.items() if status in IN_FLIGHT]
            self.assertLessEqual(len(in_flight), 1, statuses)
            if outcome.action is RunOnceAction.BLOCKED or outcome.goal_verdict_id is not None:
                break
        else:
            self.fail("run_once가 60회 안에 끝나지 않았다")
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(["task_fix_add", "task_fix_shout"], materialized_order)
        self.assertEqual({"completed"}, set(self._statuses().values()))

    def test_second_task_materializes_from_freshly_observed_inputs(self) -> None:
        for _ in range(60):
            outcome = self._tick()
            if outcome.action is RunOnceAction.BLOCKED or outcome.goal_verdict_id is not None:
                break
        else:
            self.fail("run_once가 60회 안에 끝나지 않았다")
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        digests = self._spec_map_digests()
        # 둘째 Task는 첫 Task의 쓰기를 재관측한 새 Project Map으로 준비된다.
        self.assertNotEqual(digests["task_fix_add"], digests["task_fix_shout"])

    def test_stale_input_after_materialize_blocks_without_raising(self) -> None:
        first = self._tick()
        self.assertEqual(RunOnceAction.MATERIALIZED, first.action, first.detail)
        # materialize 뒤 읽기 target을 바꾸면 immutable 입력이 stale해진다.
        _replace(self.workspace / "test_app.py", "add(2, 3)", "add(3, 2)")

        outcome = self._tick()

        self.assertEqual(RunOnceAction.BLOCKED, outcome.action, outcome.detail)
        self.assertEqual("STALE_EXECUTION_INPUT", outcome.blocker_code)
        self.assertIn("test_app.py", outcome.detail)
        self.assertEqual("materialized", self._statuses()["task_fix_add"])

    def test_deleted_input_after_materialize_blocks_without_raising(self) -> None:
        first = self._tick()
        self.assertEqual(RunOnceAction.MATERIALIZED, first.action, first.detail)
        # 없어진 입력도 부재로 바뀐 입력이다. 재확인 오류로 run_once 밖에 예외를 내지 않는다.
        (self.workspace / "test_app.py").unlink()

        outcome = self._tick()

        self.assertEqual(RunOnceAction.BLOCKED, outcome.action, outcome.detail)
        self.assertEqual("STALE_EXECUTION_INPUT", outcome.blocker_code)
        self.assertIn("test_app.py", outcome.detail)
        self.assertEqual("materialized", self._statuses()["task_fix_add"])

    def test_stale_input_during_gate_blocks_reserve_without_raising(self) -> None:
        first = self._tick()
        self.assertEqual(RunOnceAction.MATERIALIZED, first.action, first.detail)
        workspace = self.workspace

        class EditingGate:
            # dispatch 전 gate(steward)가 도는 사이 사용자가 읽기 target을 고친 경우다.
            def before_execution(self, task):
                _replace(workspace / "test_app.py", "add(2, 3)", "add(3, 2)")

            def before_completion(self, task):
                return None

        self.dispatcher.task_gate = EditingGate()

        outcome = self._tick()

        self.assertEqual(RunOnceAction.BLOCKED, outcome.action, outcome.detail)
        self.assertEqual("STALE_EXECUTION_INPUT", outcome.blocker_code)
        self.assertIn("test_app.py", outcome.detail)
        self.assertEqual("materialized", self._statuses()["task_fix_add"])
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
