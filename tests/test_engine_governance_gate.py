"""필수 governance gate(`flowmarshal.engine.governance_gate`) 검사.

가짜 플러그인·steward로 gate 논리(스냅샷 기준선, 사용자 변경 겹침, 원장 재진입, 판정 보류, provider 정지,
제품 경로 필수화)를 결정적으로 확인한다. 개발 플러그인 worktree와 node가 있으면 실제 플러그인 스크립트·MCP로
같은 Task 3개 Goal을 한 번 더 확인한다. 모델·Codex는 호출하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.engine import governance_gate
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.domain import RunOnceAction
from flowmarshal.engine.governance_gate import (
    DEFAULT_PIN, GovernancePlugin, GovernanceRejected, GovernanceTaskGate, GovernanceTimeout, GovernanceUnavailable,
    McpStdioClient, MissingGovernanceGate, RoleSteward, snapshot_worktree,
)
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from tests.fixtures.engine.governance import multitask
from tests.test_engine_qualification import qualification_inventory

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = Path(os.environ.get("FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT", "D:/claude/mcp변경/ags-engine-host"))


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def init_repository(workspace: Path) -> None:
    """.gitignore 없이 fixture를 커밋한다. validation 산출물(__pycache__)은 gate 스냅샷이 스스로 뺀다."""
    for args in (("init", "-q"), ("add", "-A"),
                 ("-c", "user.name=fm", "-c", "user.email=fm@local", "commit", "-qm", "fixture")):
        git(workspace, *args)


def replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


EDITS = {
    "task_fix_add": lambda root: replace(root / "app.py", "left - right", "left + right"),
    "task_fix_shout": lambda root: replace(root / "text.py", "text.lower()", "text.upper()"),
    "task_add_total": lambda root: (root / "app.py").write_text(
        (root / "app.py").read_text(encoding="utf-8")
        + "\n\ndef total(values: list[int]) -> int:\n    result = 0\n    for value in values:\n"
          "        result = add(result, value)\n    return result\n", encoding="utf-8"),
}


def ls_tree(root: Path, commit: str) -> dict[str, str]:
    lines = git(root, "ls-tree", "-r", commit).splitlines()
    return {line.split("\t", 1)[1]: line.split()[2] for line in lines}


class FakePlugin:
    """플러그인 MCP·스크립트 흉내. 범위 확인은 commit 모드 규칙(모든 entry가 clean)대로 분류한다."""

    STAGES = (("change-scope-baseline-capture", "general"), ("minimal-implementation", "general"),
              ("change-scope-assurance", "general"), ("acceptance-evidence-validation", "deep"))

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.scripts: list[tuple[str, dict]] = []
        self.revisions: dict[str, int] = {}
        self.failures: dict[str, Exception] = {}

    def count(self, tool: str) -> int:
        return sum(1 for name, _, _ in self.calls if name == tool)

    def call(self, tool, arguments, observation):
        self.calls.append((tool, arguments, observation))
        if tool in self.failures:
            raise self.failures.pop(tool)
        serial = len(self.calls)
        if tool == "plan_workflow":
            data = {"executionMode": "orchestrated", "stages": [
                {"stageId": f"stage-{index}", "requiredCapability": capability,
                 "executionRequirement": {"minimumModelClass": model_class}}
                for index, (capability, model_class) in enumerate(self.STAGES, 1)]}
        elif tool == "open_convergence_root":
            data = {"rootId": f"root-{serial}", "revision": 0}
        elif tool == "claim_workflow_attempt":
            data = {"leaseId": f"lease-{serial}", "rootRevision": 1}
        elif tool == "start_guarded_workflow":
            data = {"runId": f"run-{serial}", "revision": 0}
            self.revisions[data["runId"]] = 0
        elif tool == "record_stage_result":
            if arguments["expectedRevision"] != self.revisions[arguments["runId"]]:
                return {"ok": False, "data": None, "error": {"code": "STALE_REVISION", "message": "stale"}}
            self.revisions[arguments["runId"]] += 1
            data = {"revision": self.revisions[arguments["runId"]]}
        elif tool == "finalize_workflow":
            data = {"state": "passed"}
        elif tool == "abort_workflow":
            data = {"state": "aborted"}
        else:
            raise AssertionError(tool)
        return {"ok": True, "data": data, "error": None}

    def script(self, name, request_path):
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        self.scripts.append((name, request))
        if name.endswith("cli.mjs"):
            passed = all(item["direction"] == "supports" for item in request["evidence"])
            return {"verdict": "PASS" if passed else "FAIL", "criteria": []}
        root = Path(request["repositoryRoot"])
        if name.endswith("capture-workspace-baseline.mjs"):
            entries = [{"path": path, "status": "clean", "sha256": blob}
                       for path, blob in sorted(ls_tree(root, request["commit"]).items())]
            return {"comparisonTarget": request["comparisonTarget"], "targetRef": request["commit"],
                    "entries": entries, "manifestSha256": hashlib.sha256(json.dumps(entries).encode()).hexdigest()}
        if name.endswith("compare-change-scope.mjs"):
            before = {entry["path"]: entry["sha256"] for entry in request["baseline"]["entries"]}
            after = ls_tree(root, request["commit"])
            included = set(request["taskEnvelope"]["scope"]["included"])
            changes = [{"path": path, "classification": "in-scope" if path in included else "unplanned"}
                       for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)]
            findings = [f"{item['classification']}:{item['path']}" for item in changes
                        if item["classification"] != "in-scope"]
            return {"verdict": "NEEDS_APPROVAL" if findings else "PASS", "findings": findings, "changes": changes,
                    "summary": {"changes": len(changes)},
                    "currentDigest": hashlib.sha256(request["commit"].encode()).hexdigest()}
        raise AssertionError(name)

    def close(self):
        return None


class ScriptedSteward:
    """stage 하한을 만족하는 Claude 모델로 수행했다고 관측되는 steward."""

    def __init__(self, reject: tuple[str, ...] = ()) -> None:
        self.reject = reject
        self.stages: list[tuple[str, dict]] = []

    def ensure_supported(self) -> None:
        return None

    def review(self, task, stage, requirement, brief):
        self.stages.append((stage, brief))
        deep = (requirement or {}).get("minimumModelClass") == "deep"
        return {"decision": {"accept": stage not in self.reject, "rationale": "scripted"},
                "observation": {"model": "claude-opus-5" if deep else "claude-sonnet-5", "effort": "high",
                                "source": "claude_session_transcript",
                                "actorId": f"flowmarshal-engine:steward:{stage}"},
                "failure": None, "callId": f"call-{len(self.stages)}"}


def worker_observed(attempt_id):
    return {"model": "claude-sonnet-5", "effort": "high", "source": "claude_session_transcript",
            "actorId": f"flowmarshal-engine:worker:{attempt_id}"}


class MultitaskGateHarness(unittest.TestCase):
    """Task 3개 합성 Goal을 gate와 함께 run_once로 진행한다. Worker 변경은 dispatch 뒤 테스트가 쓴다."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.inventory = qualification_inventory()
        workspace = multitask.copy_fixture(self.base)
        init_repository(workspace)
        self.prepared = multitask.prepare(workspace=workspace, state_root=self.base / "state",
                                          inventory=self.inventory, roles=default_role_configuration(ROOT))
        self.runtime = FakeCodexRuntime(self.inventory)
        self.plugin = FakePlugin()
        self.steward = ScriptedSteward()
        self.worker_payload: dict | None = None

    def gate(self, plugin=None, steward=None) -> GovernanceTaskGate:
        return GovernanceTaskGate(self.prepared.service, plugin=plugin or self.plugin, steward=steward or self.steward,
                                  state_dir=self.base / "governance", observe_worker=worker_observed)

    def dispatcher(self, gate) -> EngineDispatcher:
        return EngineDispatcher(self.prepared.service, self.runtime, task_gate=gate,
                                proposal_provider=multitask.MultitaskProposals(self.prepared))

    def drive(self, gate, extra_edits=None, *, stop=None, dispatcher=None):
        """Goal verdict·차단·stop 조건까지 run_once를 돌린다."""
        dispatcher = dispatcher or self.dispatcher(gate)
        refs = {task_id: ref for ref, task_id in self.prepared.task_ids.items()}
        goal = multitask.goal_step(self.prepared)
        for _ in range(80):
            outcome = dispatcher.run_once(self.prepared.project_id, goal_validation_step=goal)
            if outcome.action is RunOnceAction.DISPATCHED and outcome.attempt_id is not None:
                ref = refs[outcome.task_id]
                EDITS[ref](self.prepared.workspace)
                if extra_edits and ref in extra_edits:
                    extra_edits[ref](self.prepared.workspace)
                thread = list(self.runtime.threads)[-1]
                self.runtime.complete(thread, response="worker completed")
                if self.worker_payload is not None:
                    self.runtime.threads[thread].provider_payload = dict(self.worker_payload)
            if stop is not None and stop(outcome):
                return outcome
            if outcome.action is RunOnceAction.BLOCKED or outcome.goal_verdict_id is not None:
                return outcome
        self.fail("run_once가 80회 안에 끝나지 않았다")

    def statuses(self) -> dict[str, str]:
        with self.prepared.service.ledger.read() as connection:
            return {ref: connection.execute("SELECT status FROM task_contracts WHERE id = ?", (task_id,)).fetchone()[0]
                    for ref, task_id in self.prepared.task_ids.items()}

    def history(self, event_type: str) -> list[dict]:
        with self.prepared.service.ledger.read() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id = ? AND event_type = ? ORDER BY sequence",
                (self.prepared.project_id, event_type))]

    def gate_steps(self, step: str) -> list[dict]:
        return [item["result"] for item in self.history("operation.completed")
                if item["kind"] == "governance_gate" and item["result"]["step"] == step]


class SnapshotTests(unittest.TestCase):
    def test_snapshot_commit_keeps_user_git_state_and_captures_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tracked.txt").write_text("one\n", encoding="utf-8")
            (root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
            init_repository(root)
            (root / "tracked.txt").write_text("two\n", encoding="utf-8")
            (root / "staged.txt").write_text("staged\n", encoding="utf-8")
            git(root, "add", "staged.txt")
            (root / "untracked.txt").write_text("new\n", encoding="utf-8")
            (root / "ignored.log").write_text("log\n", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "x.pyc").write_bytes(b"\0")
            before = (git(root, "rev-parse", "HEAD"), git(root, "symbolic-ref", "HEAD"),
                      git(root, "ls-files", "--stage"), git(root, "status", "--porcelain", "--untracked-files=all"))
            files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}

            first = snapshot_worktree(root, ref="refs/flowmarshal/governance/test/c0", message="C0")
            second = snapshot_worktree(root, ref="refs/flowmarshal/governance/test/c0", message="C0")

            after = (git(root, "rev-parse", "HEAD"), git(root, "symbolic-ref", "HEAD"),
                     git(root, "ls-files", "--stage"), git(root, "status", "--porcelain", "--untracked-files=all"))
            self.assertEqual(before, after)
            self.assertEqual(files, {path: path.read_bytes() for path in root.rglob("*")
                                     if path.is_file() and ".git" not in path.parts})
            self.assertEqual(first["commit"], second["commit"])
            self.assertEqual(first["commit"], git(root, "rev-parse", "refs/flowmarshal/governance/test/c0"))
            self.assertEqual(before[0], first["head"])
            tree = ls_tree(root, first["commit"])
            self.assertEqual({".gitignore", "tracked.txt", "staged.txt", "untracked.txt"}, set(tree))
            self.assertEqual("two", git(root, "show", f"{first['commit']}:tracked.txt"))


class GovernanceGateTests(MultitaskGateHarness):
    def test_same_file_multitask_goal_completes_through_the_gate(self) -> None:
        outcome = self.drive(self.gate())
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertIsNotNone(outcome.goal_verdict_id)
        self.assertEqual({"completed"}, set(self.statuses().values()))
        captures = [request for name, request in self.plugin.scripts if name.endswith("capture-workspace-baseline.mjs")]
        compares = [request for name, request in self.plugin.scripts if name.endswith("compare-change-scope.mjs")]
        self.assertEqual(["commit"] * 6, [item["comparisonTarget"] for item in captures + compares])
        self.assertEqual(3, self.plugin.count("finalize_workflow"))
        baseline_questions = [brief["question"] for stage, brief in self.steward.stages if stage == "baseline"]
        self.assertEqual(3, len(baseline_questions))
        self.assertTrue(all("미커밋 변경이 없는가" not in question for question in baseline_questions))
        # total Task의 C0는 앞 Task가 커밋 없이 바꾼 app.py를 담는다.
        total_key = self.prepared.task_ids["task_add_total"]
        c0 = next(item for item in self.gate_steps("snapshot_c0") if item["key"]["task_id"] == total_key)["data"]
        self.assertIn("left + right", git(self.prepared.workspace, "show", f"{c0['commit']}:app.py"))
        self.assertEqual(git(self.prepared.workspace, "rev-parse", "HEAD"), c0["head"])

    def test_new_undeclared_file_still_blocks_completion(self) -> None:
        outcome = self.drive(self.gate(), {
            "task_fix_add": lambda root: (root / "notes.md").write_text("선언 밖 파일\n", encoding="utf-8")})
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("GOVERNANCE_GATE_FAILED", outcome.blocker_code)
        self.assertIn("unplanned:notes.md", outcome.detail)
        self.assertEqual("blocked", self.statuses()["task_fix_add"])
        self.assertEqual(1, self.plugin.count("abort_workflow"))

    def test_user_change_on_write_target_blocks_before_steward_and_worker(self) -> None:
        replace(self.prepared.workspace / "app.py", "return", "return  ")
        gate = self.gate()
        first = self.drive(gate)
        second = self.dispatcher(gate).run_once(self.prepared.project_id)
        for outcome in (first, second):
            self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
            self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
            self.assertIn("GOVERNANCE_USER_CHANGE_OVERLAP: 쓰기 target에", outcome.detail)
            self.assertIn("app.py", outcome.detail)
        self.assertEqual([], self.steward.stages)
        self.assertEqual([], self.plugin.calls)
        self.assertEqual(0, self.runtime.create_calls)
        self.assertEqual("materialized", self.statuses()["task_fix_add"])
        self.assertEqual(1, len(self.history("task.governance_blocked")))

    def test_user_change_outside_write_targets_does_not_block(self) -> None:
        (self.prepared.workspace / "scratch.txt").write_text("사용자 메모\n", encoding="utf-8")
        outcome = self.drive(self.gate())
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertIsNotNone(outcome.goal_verdict_id)

    def repair_after_failed_validation(self, gate) -> None:
        """add Task Worker가 잘못 고쳐 validation이 실패하면 실패 근거로 Task repair 재시도를 연다."""
        from flowmarshal.engine.domain import FailureClass, RecoveryAssessment, RepairAction, new_id

        wrong = {"task_fix_add": lambda root: replace(root / "app.py", "left + right", "left * right")}
        failed = self.drive(gate, wrong)
        self.assertEqual("TASK_VALIDATION_FAILED", failed.blocker_code, failed.detail)
        task_id = self.prepared.task_ids["task_fix_add"]
        with self.prepared.service.ledger.read() as connection:
            attempt = connection.execute("SELECT id FROM attempts WHERE task_id = ? AND kind = 'execution'",
                                         (task_id,)).fetchone()["id"]
            result = connection.execute("SELECT id, payload_json FROM validation_results WHERE task_id = ? "
                                        "AND status = 'fail'", (task_id,)).fetchone()
        self.prepared.service.retry_task(
            task_id=task_id, failed_validation_result_id=result["id"],
            recovery_assessment=RecoveryAssessment(
                assessment_id=new_id("recovery_assessment"), attempt_id=attempt,
                failure_class=FailureClass.IMPLEMENTATION, action=RepairAction.TASK_REPAIR,
                rationale="test_app 실패가 Worker 구현 결함을 보여 준다.",
                new_evidence_ids=tuple(json.loads(result["payload_json"])["evidence_ids"]),
                same_failure_replan_count=0, goal_replan_count=0))

    def test_task_repair_after_failed_validation_passes_the_gate(self) -> None:
        gate = self.gate()
        self.repair_after_failed_validation(gate)
        task_id = self.prepared.task_ids["task_fix_add"]
        with self.prepared.service.ledger.read() as connection:
            task = connection.execute("SELECT * FROM task_contracts WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual("materialized", task["status"])
        # Attempt 1의 Worker가 남긴 app.py 변경은 사용자 변경이 아니므로 repair Attempt 2가 gate를 지난다.
        # 같은 Execution Spec 재시도가 이후 STALE_EXECUTION_INPUT을 내는 것은 gate와 무관한 Core 규칙이다.
        self.assertIsNone(gate.before_execution(task))
        snapshots = {item["key"]["attempt_no"]: item["data"] for item in self.gate_steps("snapshot_c0")
                     if item["key"]["task_id"] == task_id}
        self.assertEqual([1, 2], sorted(snapshots))
        self.assertIn("left * right", git(self.prepared.workspace, "show", f"{snapshots[2]['commit']}:app.py"))
        self.assertEqual([], self.history("task.governance_blocked"))

    def test_user_change_made_before_the_goal_blocks_a_later_task_that_writes_it(self) -> None:
        # Goal 시작 전부터 있던 text.py 변경은 add Task를 막지 않고, text.py를 쓰는 shout Task 앞에서 막는다.
        replace(self.prepared.workspace / "text.py", "return", "return  ")
        outcome = self.drive(self.gate())
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("GOVERNANCE_USER_CHANGE_OVERLAP", outcome.detail)
        self.assertIn("text.py", outcome.detail)
        self.assertEqual({"task_fix_add": "completed", "task_fix_shout": "materialized", "task_add_total": "pending"},
                         self.statuses())

    def completed_then(self, gate, ref: str, action) -> None:
        """ref Task가 완료된 직후 action(root)을 실행한다."""
        dispatcher = self.dispatcher(gate)
        done = self.prepared.task_ids[ref]
        outcome = self.drive(gate, dispatcher=dispatcher, stop=lambda outcome: outcome.action is RunOnceAction.COMPLETED
                             and outcome.task_id == done)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        action(self.prepared.workspace)
        return dispatcher

    def test_user_edit_during_the_goal_to_a_later_write_target_blocks(self) -> None:
        gate = self.gate()
        dispatcher = self.completed_then(gate, "task_fix_add",
                                         lambda root: replace(root / "text.py", "return", "return  "))
        outcome = self.drive(gate, dispatcher=dispatcher)
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("GOVERNANCE_USER_CHANGE_OVERLAP", outcome.detail)
        self.assertIn("text.py", outcome.detail)

    def test_user_edit_to_a_file_the_engine_wrote_blocks_the_next_writer(self) -> None:
        gate = self.gate()
        dispatcher = self.completed_then(gate, "task_fix_shout",
                                         lambda root: replace(root / "app.py", "left + right", "right + left"))
        outcome = self.drive(gate, dispatcher=dispatcher)
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("GOVERNANCE_USER_CHANGE_OVERLAP", outcome.detail)
        self.assertIn("app.py", outcome.detail)
        self.assertEqual("materialized", self.statuses()["task_add_total"])

    def test_partial_write_of_a_failed_worker_is_not_a_user_change(self) -> None:
        gate = self.gate()
        dispatcher = self.dispatcher(gate)
        task_id = self.prepared.task_ids["task_fix_add"]
        for _ in range(10):
            outcome = dispatcher.run_once(self.prepared.project_id)
            if outcome.action is RunOnceAction.DISPATCHED and outcome.attempt_id is not None:
                break
        replace(self.prepared.workspace / "app.py", "left - right", "left + ")
        self.runtime.fail(list(self.runtime.threads)[-1], response="중간에 실패했다")
        dispatcher.run_once(self.prepared.project_id)
        with self.prepared.service.ledger.read() as connection:
            task = connection.execute("SELECT * FROM task_contracts WHERE id = ?", (task_id,)).fetchone()
        # Worker가 결과 기록 없이 실패했으므로 그 쓰기 target의 부분 변경은 Engine 변경으로 본다.
        self.assertIsNone(gate.before_execution(task))
        self.assertEqual([1, 2], sorted(item["key"]["attempt_no"] for item in self.gate_steps("snapshot_c0")
                                        if item["key"]["task_id"] == task_id))

    def test_gate_effects_are_recorded_before_and_after_in_core_history(self) -> None:
        self.drive(self.gate())
        prepared = [item for item in self.history("operation.prepared") if item["kind"] == "governance_gate"]
        completed = [item for item in self.history("operation.completed") if item["kind"] == "governance_gate"]
        self.assertEqual(len(prepared), len(completed))
        steps = {item["request"]["step"] for item in prepared}
        self.assertTrue({"snapshot_c0", "snapshot_c1", "steward:bootstrap", "steward:baseline", "steward:scope",
                         "steward:acceptance", "plan", "start", "baseline", "scope", "acceptance",
                         "record:baseline", "finalize"} <= steps)
        keys = {json.dumps(item["request"]["key"], sort_keys=True) for item in prepared}
        self.assertEqual(3, len(keys))
        self.assertTrue(all(json.loads(key)["attempt_no"] == 1 for key in keys))

    def test_restart_resumes_the_same_run_from_the_ledger(self) -> None:
        refs = {task_id: ref for ref, task_id in self.prepared.task_ids.items()}
        first = self.drive(self.gate(), stop=lambda outcome: outcome.action is RunOnceAction.DISPATCHED
                           and refs.get(outcome.task_id) == "task_fix_add" and outcome.attempt_id is not None)
        self.assertEqual(RunOnceAction.DISPATCHED, first.action)
        # 재시작: gate·dispatcher를 새로 만들고 메모리 상태 없이 이어 간다.
        outcome = self.drive(self.gate())
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(3, self.plugin.count("start_guarded_workflow"))
        self.assertEqual(3, len(self.gate_steps("snapshot_c0")))
        self.assertEqual(3, len([stage for stage, _ in self.steward.stages if stage == "bootstrap"]))

    def test_complete_task_failure_after_finalize_resumes_without_a_new_run(self) -> None:
        service = self.prepared.service
        original = service.complete_task
        failures = [RuntimeError("complete_task 일시 실패")]

        def flaky(task_id):
            if failures:
                raise failures.pop()
            return original(task_id)

        gate = self.gate()
        with patch.object(service, "complete_task", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self.drive(gate)
            outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(3, self.plugin.count("finalize_workflow"))
        self.assertEqual(3, self.plugin.count("start_guarded_workflow"))

    def test_reserve_failure_reuses_the_open_run(self) -> None:
        service = self.prepared.service
        original = service.reserve_attempt
        failures = [RuntimeError("reserve 일시 실패")]

        def flaky(**kwargs):
            if failures:
                raise failures.pop()
            return original(**kwargs)

        gate = self.gate()
        with patch.object(service, "reserve_attempt", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self.drive(gate)
            outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(3, self.plugin.count("start_guarded_workflow"))
        self.assertEqual(3, len(self.gate_steps("snapshot_c0")))

    def test_timed_out_effect_stops_as_external_unknown_without_rerun(self) -> None:
        self.plugin.failures["start_guarded_workflow"] = GovernanceTimeout("GOVERNANCE_TIMEOUT: MCP tools/call")
        gate = self.gate()
        first = self.drive(gate)
        self.assertEqual("GOVERNANCE_GATE_PENDING", first.blocker_code)
        second = self.dispatcher(gate).run_once(self.prepared.project_id)
        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", second.blocker_code)
        self.assertEqual(1, self.plugin.count("start_guarded_workflow"))
        self.assertEqual("materialized", self.statuses()["task_fix_add"])
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual("recovery_required", connection.execute(
                "SELECT run_state FROM projects WHERE id = ?", (self.prepared.project_id,)).fetchone()[0])

    def test_unavailable_script_is_retried_on_the_next_tick(self) -> None:
        original = self.plugin.script
        failures = [GovernanceUnavailable("GOVERNANCE_TIMEOUT: script")]

        def flaky(name, request_path):
            if failures:
                raise failures.pop()
            return original(name, request_path)

        self.plugin.script = flaky
        gate = self.gate()
        first = self.drive(gate)
        self.assertEqual("GOVERNANCE_GATE_PENDING", first.blocker_code)
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)

    def test_steward_rejection_is_replayed_not_rerolled(self) -> None:
        steward = ScriptedSteward(reject=("baseline",))
        gate = self.gate(steward=steward)
        first = self.drive(gate)
        second = self.dispatcher(gate).run_once(self.prepared.project_id)
        for outcome in (first, second):
            self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
            self.assertIn("STEWARD_REJECTED: baseline", outcome.detail)
        self.assertEqual(["bootstrap", "baseline"], [stage for stage, _ in steward.stages])
        self.assertEqual(1, self.plugin.count("abort_workflow"))
        self.assertEqual(1, len(self.history("task.governance_blocked")))
        self.assertEqual(0, self.runtime.create_calls)

    def test_missing_worker_observation_blocks_completion(self) -> None:
        gate = GovernanceTaskGate(self.prepared.service, plugin=self.plugin, steward=self.steward,
                                  state_dir=self.base / "governance", observe_worker=lambda attempt_id: None)
        outcome = self.drive(gate)
        self.assertEqual("GOVERNANCE_GATE_FAILED", outcome.blocker_code)
        self.assertIn("WORKER_OBSERVATION_UNAVAILABLE", outcome.detail)
        self.assertEqual(0, self.plugin.count("finalize_workflow"))

    def test_worker_observation_is_read_from_the_ledger_usage(self) -> None:
        def ledger_gate():
            return GovernanceTaskGate(self.prepared.service, plugin=self.plugin, steward=self.steward,
                                      state_dir=self.base / "governance")

        blocked = self.drive(ledger_gate())
        self.assertEqual("GOVERNANCE_GATE_FAILED", blocked.blocker_code)
        self.assertIn("WORKER_OBSERVATION_UNAVAILABLE", blocked.detail)

    def test_worker_provider_observation_reaches_the_implementation_stage(self) -> None:
        self.worker_payload = {"model_observation_source": "claude_session_transcript",
                               "observed_model": "claude-sonnet-5", "observed_effort": "high"}
        gate = GovernanceTaskGate(self.prepared.service, plugin=self.plugin, steward=self.steward,
                                  state_dir=self.base / "governance")
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        observations = [observation for tool, arguments, observation in self.plugin.calls
                        if tool == "record_stage_result" and arguments["stageId"] == "stage-2"]
        self.assertEqual(3, len(observations))
        self.assertTrue(all((item["model"], item["effort"], item["source"])
                            == ("claude-sonnet-5", "high", "claude_session_transcript") for item in observations))

    def test_missing_gate_refuses_execution_dispatch(self) -> None:
        outcome = self.drive(MissingGovernanceGate())
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("GOVERNANCE_GATE_REQUIRED", outcome.detail)
        self.assertEqual(0, self.runtime.create_calls)


class StewardTests(MultitaskGateHarness):
    def task_row(self):
        with self.prepared.service.ledger.read() as connection:
            return connection.execute("SELECT * FROM task_contracts WHERE id = ?",
                                      (self.prepared.task_ids["task_fix_add"],)).fetchone()

    def test_role_steward_uses_reviewer_bindings_records_usage_and_observation(self) -> None:
        roles = default_role_configuration(ROOT)

        class ObservedRunner(ScriptedStructuredRoleRunner):
            def run(self, request, *, validator=None):
                result = super().run(request, validator=validator)
                receipt = result.receipt.model_copy(update={
                    "observed_model": request.model, "observed_effort": request.effort,
                    "binding_provenance": {**result.receipt.binding_provenance,
                                           "observed": "claude_session_transcript"}})
                return result.model_copy(update={"receipt": receipt})

        runner = ObservedRunner({"governance_steward": [{"accept": True, "rationale": "근거 충족"}] * 2})
        steward = RoleSteward(self.prepared.service, runtime=self.runtime, roles=roles, runner=runner,
                              cwd=self.base / "steward-cwd")
        general = steward.review(self.task_row(), "baseline", {"minimumModelClass": "general"}, {"question": "q"})
        deep = steward.review(self.task_row(), "acceptance", {"minimumModelClass": "deep"}, {"question": "q"})
        self.assertEqual((roles.general_reviewer.model, roles.general_reviewer.effort),
                         (runner.calls[0].model, runner.calls[0].effort))
        self.assertEqual((roles.critical_reviewer.model, roles.critical_reviewer.effort),
                         (runner.calls[1].model, runner.calls[1].effort))
        self.assertEqual({"accept": True, "rationale": "근거 충족"}, general["decision"])
        self.assertEqual(("claude_session_transcript", roles.critical_reviewer.model),
                         (deep["observation"]["source"], deep["observation"]["model"]))
        with self.prepared.service.ledger.read() as connection:
            rows = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM budget_usage WHERE project_id = ? AND stage = 'validation'",
                (self.prepared.project_id,))]
        self.assertEqual(["governance_steward"] * 2, [row["role"] for row in rows])

    def test_steward_without_provider_observation_blocks_the_stage(self) -> None:
        runner = ScriptedStructuredRoleRunner({"governance_steward": [{"accept": True, "rationale": "ok"}] * 4})
        steward = RoleSteward(self.prepared.service, runtime=self.runtime, roles=default_role_configuration(ROOT),
                              runner=runner, cwd=self.base / "steward-cwd")
        steward.ensure_supported = lambda: None  # provider 판별과 무관하게 관측 부재 경로만 본다.
        outcome = self.drive(self.gate(steward=steward))
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("STEWARD_OBSERVATION_UNAVAILABLE: bootstrap", outcome.detail)
        self.assertEqual([], [call for call in self.plugin.calls if call[0] == "plan_workflow"])

    def test_codex_provider_stops_before_any_steward_call(self) -> None:
        runner = ScriptedStructuredRoleRunner({})
        steward = RoleSteward(self.prepared.service, runtime=self.runtime, roles=default_role_configuration(ROOT),
                              runner=runner, cwd=self.base / "steward-cwd")
        outcome = self.drive(self.gate(steward=steward))
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("GOVERNANCE_PROVIDER_UNSUPPORTED", outcome.detail)
        self.assertEqual([], runner.calls)
        self.assertEqual([], self.plugin.calls)
        self.assertEqual([], [item for item in self.history("operation.prepared") if item["kind"] == "governance_gate"])

    def test_configured_catalog_provider_is_supported(self) -> None:
        runtime = SimpleNamespace(list_models=lambda: SimpleNamespace(inventory_provenance="configured_catalog"))
        RoleSteward(self.prepared.service, runtime=runtime, roles=None, runner=None, cwd=self.base).ensure_supported()

    def test_product_gate_has_no_model_names(self) -> None:
        source = Path(governance_gate.__file__).read_text(encoding="utf-8")
        for name in ("claude-", "gpt-", "sonnet", "opus", "haiku"):
            self.assertNotIn(name, source)


class ProductPathTests(MultitaskGateHarness):
    def test_application_without_governance_uses_a_blocking_gate(self) -> None:
        application = EngineApplication(self.prepared.service, runtime=self.runtime)
        gate = application.task_gate()
        self.assertIsInstance(gate, MissingGovernanceGate)
        self.assertIs(gate, application._dispatcher().task_gate)
        outcome = self.drive(gate)
        self.assertEqual("GOVERNANCE_GATE_BLOCKED", outcome.blocker_code)
        self.assertIn("GOVERNANCE_GATE_REQUIRED", outcome.detail)
        self.assertEqual(0, self.runtime.create_calls)

    def test_application_opens_the_configured_gate_once(self) -> None:
        opened = []
        real = self.gate()

        class Governance:
            def open_gate(inner, service, *, runtime, roles, runner):
                opened.append((service, runtime, roles, runner))
                return real

        application = EngineApplication(self.prepared.service, runtime=self.runtime, governance=Governance())
        self.assertIs(real, application._dispatcher().task_gate)
        self.assertIs(real, application.task_gate())
        self.assertEqual([(self.prepared.service, self.runtime, None, None)], opened)
        outcome = self.drive(application.task_gate())
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)


    def test_cli_validate_complete_is_refused(self) -> None:
        from flowmarshal.engine import cli
        from flowmarshal.engine.service import EngineServiceError

        with patch.object(cli, "_service", side_effect=AssertionError("원장을 열지 않는다")):
            with self.assertRaisesRegex(EngineServiceError, "GOVERNANCE_GATE_REQUIRED"):
                cli._cmd_validate_task(SimpleNamespace(complete=True, result_file="unused.json"))

    def test_cli_attempt_retry_leaves_dispatch_to_the_gated_run_once(self) -> None:
        from flowmarshal.engine import cli

        service = self.prepared.service
        emitted = []
        task_id = self.prepared.task_ids["task_fix_add"]
        with patch.object(cli, "_service", return_value=service),                 patch.object(service, "retry_task") as retry,                 patch.object(service, "reserve_attempt", side_effect=AssertionError("직접 예약하지 않는다")),                 patch.object(cli, "_emit", side_effect=emitted.append):
            cli._cmd_attempt_retry(SimpleNamespace(task_id=task_id, evidence_id=None, recovery_assessment_file=None,
                                                   failed_validation_result_id=None))
        retry.assert_called_once()
        self.assertEqual(task_id, emitted[0]["task_id"])
        self.assertIn("governance gate", emitted[0]["next"])

class TimeoutTests(unittest.TestCase):
    def test_mcp_client_times_out_on_a_silent_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server = Path(temp) / "silent.py"
            server.write_text("import sys, time\nsys.stdin.readline()\ntime.sleep(30)\n", encoding="utf-8")
            started = time.monotonic()
            with self.assertRaises(GovernanceTimeout) as raised:
                McpStdioClient([sys.executable, str(server)], cwd=Path(temp), env=dict(os.environ),
                               stderr_path=Path(temp) / "stderr.log", timeout=0.5)
            self.assertLess(time.monotonic() - started, 10)
            self.assertIn("GOVERNANCE_TIMEOUT: MCP initialize", str(raised.exception))

    def test_subprocess_calls_time_out_with_explicit_codes(self) -> None:
        sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.assertRaises(GovernanceTimeout):
            governance_gate._run(sleeper, timeout=0.5, code="X", effects_started=True)
        with self.assertRaises(GovernanceUnavailable) as raised:
            governance_gate._run(sleeper, timeout=0.5, code="GOVERNANCE_GIT", effects_started=False)
        self.assertIn("GOVERNANCE_TIMEOUT: GOVERNANCE_GIT", str(raised.exception))
        source = Path(governance_gate.__file__).read_text(encoding="utf-8")
        # 모든 git·node 호출은 timeout이 있는 _run을 지난다. MCP 서버만 McpStdioClient가 띄운다.
        self.assertEqual(1, source.count("subprocess.run("))
        self.assertEqual(1, source.count("subprocess.Popen("))


def plugin_available() -> bool:
    if shutil.which("node") is None or not (PLUGIN_ROOT / governance_gate.SERVER).is_file():
        return False
    try:
        DEFAULT_PIN.verify(PLUGIN_ROOT)
    except GovernanceRejected:
        return False
    return True


@unittest.skipUnless(plugin_available(), f"고정한 플러그인 worktree({PLUGIN_ROOT})나 node가 없다")
class RealPluginTests(MultitaskGateHarness):
    """개발 플러그인 worktree의 실제 스크립트·MCP 서버로 같은 합성 Goal을 확인한다(모델 호출 없음)."""

    def real_gate(self) -> GovernanceTaskGate:
        plugin = GovernancePlugin(PLUGIN_ROOT, self.base / "governance" / "plugin")
        gate = self.gate(plugin=plugin)
        self.addCleanup(gate.close)
        return gate

    def test_same_file_multitask_goal_completes_with_the_real_plugin(self) -> None:
        outcome = self.drive(self.real_gate())
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertIsNotNone(outcome.goal_verdict_id)
        self.assertEqual({"completed"}, set(self.statuses().values()))

    def test_new_undeclared_file_blocks_with_the_real_plugin(self) -> None:
        outcome = self.drive(self.real_gate(), {
            "task_fix_add": lambda root: (root / "notes.md").write_text("선언 밖 파일\n", encoding="utf-8")})
        self.assertEqual("GOVERNANCE_GATE_FAILED", outcome.blocker_code)
        self.assertIn("unplanned:notes.md", outcome.detail)


if __name__ == "__main__":
    unittest.main()
