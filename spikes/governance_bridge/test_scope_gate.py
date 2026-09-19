"""플러그인 없는 결정적 범위 gate(`scope_gate.py`)와 Task 3개 합성 Goal의 가짜 provider 검사.

실행: 저장소 루트에서
  FLOWMARSHAL_ENGINE_SOURCE_ROOT=. .venv/Scripts/python.exe -m unittest discover -s spikes/governance_bridge -p "test_*.py"
모델은 호출하지 않는다. Worker 변경은 테스트가 직접 쓴다.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.fixtures.engine.governance import multitask
from flowmarshal.engine.domain import RunOnceAction
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from scope_gate import WorkspaceScopeGate
from tests.test_engine_qualification import qualification_inventory

ROOT = Path(__file__).resolve().parents[2]


def replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


# Task별 Worker 변경. 모두 자기 쓰기 target만 바꾼다.
EDITS = {
    "task_fix_add": lambda root: replace(root / "app.py", "left - right", "left + right"),
    "task_fix_shout": lambda root: replace(root / "text.py", "text.lower()", "text.upper()"),
    "task_add_total": lambda root: (root / "app.py").write_text(
        (root / "app.py").read_text(encoding="utf-8")
        + "\n\ndef total(values: list[int]) -> int:\n    result = 0\n    for value in values:\n"
          "        result = add(result, value)\n    return result\n", encoding="utf-8"),
}


class MultitaskHarness(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.inventory = qualification_inventory()
        self.prepared = multitask.prepare(
            workspace=multitask.copy_fixture(self.base), state_root=self.base / "state",
            inventory=self.inventory, roles=default_role_configuration(ROOT))
        self.runtime = FakeCodexRuntime(self.inventory)

    def drive(self, task_gate=None, extra_edits=None):
        """Goal verdict나 차단까지 run_once를 돌린다. Worker 변경은 dispatch 뒤 테스트가 쓴다."""
        dispatcher = EngineDispatcher(self.prepared.service, self.runtime, task_gate=task_gate,
                                      proposal_provider=multitask.MultitaskProposals(self.prepared))
        refs = {task_id: ref for ref, task_id in self.prepared.task_ids.items()}
        goal = multitask.goal_step(self.prepared)
        for _ in range(60):
            outcome = dispatcher.run_once(self.prepared.project_id, goal_validation_step=goal)
            if outcome.action is RunOnceAction.DISPATCHED:
                ref = refs[outcome.task_id]
                EDITS[ref](self.prepared.workspace)
                if extra_edits and ref in extra_edits:
                    extra_edits[ref](self.prepared.workspace)
                self.runtime.complete(list(self.runtime.threads)[-1], response="worker completed")
            elif outcome.action is RunOnceAction.BLOCKED or outcome.goal_verdict_id is not None:
                return outcome
        self.fail("run_once가 60회 안에 끝나지 않았다")

    def statuses(self) -> dict[str, str]:
        with self.prepared.service.ledger.read() as connection:
            return {ref: connection.execute("SELECT status FROM task_contracts WHERE id = ?", (task_id,)).fetchone()[0]
                    for ref, task_id in self.prepared.task_ids.items()}


class WorkspaceScopeGateTests(MultitaskHarness):
    def test_goal_completes_when_every_task_stays_in_its_targets(self) -> None:
        outcome = self.drive(WorkspaceScopeGate(self.prepared.service))
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertIsNotNone(outcome.goal_verdict_id)
        self.assertEqual({"completed"}, set(self.statuses().values()))

    def test_new_undeclared_file_blocks_completion(self) -> None:
        outcome = self.drive(WorkspaceScopeGate(self.prepared.service), {
            "task_fix_add": lambda root: (root / "notes.md").write_text("선언 밖 파일\n", encoding="utf-8")})
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertIn("UNDECLARED_CHANGE", outcome.detail)
        self.assertIn("notes.md", outcome.detail)
        self.assertEqual("blocked", self.statuses()["task_fix_add"])

    def test_editing_another_tasks_file_blocks_completion(self) -> None:
        # text.py Task가 app.py(다른 Task의 쓰기 target, 이 Task에는 선언되지 않은 파일)를 바꾼다.
        extra = {"task_fix_shout": lambda root: replace(root / "app.py", "return left + right",
                                                        "return int(left + right)")}
        outcome = self.drive(WorkspaceScopeGate(self.prepared.service), extra)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertIn("UNDECLARED_CHANGE: 쓰기 target 밖 변경 app.py", outcome.detail)
        self.assertEqual("blocked", self.statuses()["task_fix_shout"])

    def test_same_change_completes_without_a_gate(self) -> None:
        # 대조군: gate가 없으면 같은 변경도 완료되고 다음 State 재관측이 새 기준으로 흡수한다.
        extra = {"task_fix_shout": lambda root: replace(root / "app.py", "return left + right",
                                                        "return int(left + right)")}
        outcome = self.drive(None, extra)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertIsNotNone(outcome.goal_verdict_id)


if __name__ == "__main__":
    unittest.main()
