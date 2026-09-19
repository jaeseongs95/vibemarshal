"""가짜 provider로 GovernanceTaskGate를 끝까지 돌리는 결정적 검사.

실행: 저장소 루트에서
  FLOWMARSHAL_ENGINE_SOURCE_ROOT=. .venv/Scripts/python.exe -m unittest discover -s spikes/governance_bridge -p "test_*.py"
플러그인 worktree(AGS_PLUGIN_ROOT, 기본 D:/claude/mcp변경/ags-engine-host)나 node가 없으면 건너뛴다.
모델은 호출하지 않는다. steward·Worker 관측은 스크립트 값이다.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from bridge import DEFAULT_PIN, GovernanceBridgeError, GovernanceTaskGate, Observation
from flowmarshal.engine.domain import RunOnceAction
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from tests.test_engine_qualification import qualification_inventory

ROOT = Path(__file__).resolve().parents[2]
FIXED_ADD = "def add(left: int, right: int) -> int:\n    return left + right\n"


class ScriptedSteward:
    """stage 하한을 만족하는 Claude 모델로 수행했다고 관측되는 steward. model로 강제할 수 있다."""

    def __init__(self, model: str | None = None, reject: tuple[str, ...] = ()) -> None:
        self.model = model
        self.reject = reject
        self.stages: list[str] = []

    def review(self, stage, requirement, brief):
        self.stages.append(stage)
        deep = (requirement or {}).get("minimumModelClass") == "deep"
        model = self.model or ("claude-opus-5" if deep else "claude-sonnet-5")
        return ({"accept": stage not in self.reject, "rationale": "scripted"},
                Observation(model, "high", f"flowmarshal-engine:steward:{stage}", "scripted"))


def worker_observed(attempt_id, thread_id):
    return Observation("claude-sonnet-5", "high", f"flowmarshal-engine:worker:{attempt_id}", "scripted")


@unittest.skipUnless(shutil.which("node") and (DEFAULT_PIN.root / "mcp-server/dist/server.mjs").is_file(),
                     "플러그인 worktree 또는 node가 없다")
class GovernanceBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        workspace, _ = _copy_fixture(ROOT, self.base)
        # 범위 확인은 git 저장소를 요구한다. 검증이 만드는 __pycache__는 범위 밖으로 둔다.
        (workspace / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(workspace), *args], check=True)
        self.inventory = qualification_inventory()
        self.prepared = _prepare(workspace=workspace, state_root=self.base / "state", inventory=self.inventory,
                                 roles=default_role_configuration(ROOT))
        self.runtime = FakeCodexRuntime(self.inventory)

    def gate(self, steward=None, observe_worker=worker_observed) -> GovernanceTaskGate:
        gate = GovernanceTaskGate(self.prepared.service, pin=DEFAULT_PIN, state_dir=self.base / "governance",
                                  steward=steward or ScriptedSteward(), observe_worker=observe_worker)
        self.addCleanup(gate.close)
        return gate

    def log(self) -> list[dict]:
        path = self.base / "governance" / "governance-log.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def task_status(self) -> str:
        with self.prepared.service.ledger.read() as connection:
            return connection.execute("SELECT status FROM task_contracts WHERE id = ?",
                                      (self.prepared.task_id,)).fetchone()[0]

    def run_to_validation(self, dispatcher, *, extra_edit=None):
        project_id = self.prepared.project_id
        self.assertEqual(RunOnceAction.MATERIALIZED,
                         dispatcher.run_once(project_id, proposal=self.prepared.proposal).action)
        dispatched = dispatcher.run_once(project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched.detail)
        (self.prepared.workspace / "app.py").write_text(FIXED_ADD, encoding="utf-8")
        if extra_edit is not None:
            extra_edit(self.prepared.workspace)
        self.runtime.complete(next(iter(self.runtime.threads)), response="worker completed")
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(project_id).action)
        self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(project_id).action)
        return dispatcher.run_once(project_id)

    def test_task_completes_only_after_governance_workflow_passes(self) -> None:
        steward = ScriptedSteward()
        dispatcher = EngineDispatcher(self.prepared.service, self.runtime, task_gate=self.gate(steward))
        outcome = self.run_to_validation(dispatcher)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual("completed", self.task_status())
        calls = [(event["tool"], event["ok"], event["attested"]) for event in self.log() if event["event"] == "mcp"]
        self.assertEqual([
            ("plan_workflow", True, True), ("open_convergence_root", True, False),
            ("claim_workflow_attempt", True, False), ("start_guarded_workflow", True, False),
            ("record_stage_result", True, True), ("record_stage_result", True, True),
            ("record_stage_result", True, True), ("record_stage_result", True, True),
            ("finalize_workflow", True, False)], calls)
        self.assertEqual("passed", [e for e in self.log() if e["event"] == "mcp"][-1]["state"])
        self.assertEqual(["bootstrap", "baseline", "scope", "acceptance"], steward.stages)
        acceptance = [e for e in self.log() if e.get("stageId") == "stage-04-acceptance-evidence-validation"][0]
        self.assertEqual("claude-opus-5", acceptance["observation"]["model"])

    @staticmethod
    def write_undeclared_file(workspace: Path) -> None:
        # Execution Spec이 선언하지 않은 파일. 선언된 읽기 target(test_app.py) 변경은 Engine이 digest로 이미 잡는다.
        (workspace / "notes.md").write_text("Worker가 남긴 선언 밖 파일\n", encoding="utf-8")

    def test_undeclared_file_change_blocks_completion(self) -> None:
        dispatcher = EngineDispatcher(self.prepared.service, self.runtime, task_gate=self.gate())
        outcome = self.run_to_validation(dispatcher, extra_edit=self.write_undeclared_file)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("GOVERNANCE_GATE_FAILED", outcome.blocker_code)
        self.assertIn("CHANGE_SCOPE_NEEDS_APPROVAL", outcome.detail)
        self.assertIn("unplanned:notes.md", outcome.detail)
        self.assertEqual("blocked", self.task_status())

    def test_same_change_completes_without_the_gate(self) -> None:
        # 현재 빈틈의 대조군: gate가 없으면 선언 밖 파일 변경도 Task 완료 뒤 새 기준으로 흡수된다.
        outcome = self.run_to_validation(EngineDispatcher(self.prepared.service, self.runtime),
                                         extra_edit=self.write_undeclared_file)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action)

    def test_observation_below_the_stage_floor_blocks_before_worker(self) -> None:
        dispatcher = EngineDispatcher(self.prepared.service, self.runtime,
                                      task_gate=self.gate(ScriptedSteward(model="claude-haiku-4-5-20251001")))
        project_id = self.prepared.project_id
        dispatcher.run_once(project_id, proposal=self.prepared.proposal)
        outcome = dispatcher.run_once(project_id)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("BINDING_INVALID", outcome.detail)
        self.assertEqual(0, self.runtime.create_calls)

    def test_baseline_failure_keeps_blocking_on_later_ticks(self) -> None:
        # 기준선 stage 실패 뒤 다음 tick에서 Worker가 dispatch되면 안 된다(감사 F1 회귀).
        dispatcher = EngineDispatcher(self.prepared.service, self.runtime,
                                      task_gate=self.gate(ScriptedSteward(reject=("baseline",))))
        project_id = self.prepared.project_id
        dispatcher.run_once(project_id, proposal=self.prepared.proposal)
        outcomes = [dispatcher.run_once(project_id) for _ in range(2)]
        self.assertEqual([RunOnceAction.BLOCKED] * 2, [outcome.action for outcome in outcomes])
        self.assertTrue(all("STEWARD_REJECTED: 변경 기준선" in outcome.detail for outcome in outcomes))
        self.assertEqual(0, self.runtime.create_calls)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
        aborts = [e for e in self.log() if e["event"] == "mcp" and e["tool"] == "abort_workflow"]
        self.assertEqual([(True, "blocked")], [(e["ok"], e["state"]) for e in aborts])

    def test_unobserved_worker_blocks_completion(self) -> None:
        dispatcher = EngineDispatcher(self.prepared.service, self.runtime,
                                      task_gate=self.gate(observe_worker=lambda attempt, thread: None))
        outcome = self.run_to_validation(dispatcher)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertIn("WORKER_OBSERVATION_UNAVAILABLE", outcome.detail)
        self.assertEqual("blocked", self.task_status())
        # 기준선만 기록되고 구현 stage는 token 없이 기록하지 않는다.
        records = [e["stageId"] for e in self.log() if e["event"] == "mcp" and e["tool"] == "record_stage_result"]
        self.assertEqual(["stage-01-change-scope-baseline-capture"], records)

    def test_plugin_pin_mismatch_refuses_to_start(self) -> None:
        wrong = dataclasses.replace(DEFAULT_PIN, server_sha256="0" * 64)
        with self.assertRaisesRegex(GovernanceBridgeError, "PLUGIN_PIN_MISMATCH"):
            GovernanceTaskGate(self.prepared.service, pin=wrong, state_dir=self.base / "governance",
                               steward=ScriptedSteward(), observe_worker=worker_observed)


if __name__ == "__main__":
    unittest.main()
