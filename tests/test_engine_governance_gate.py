"""필수 governance gate(`flowmarshal.engine.governance_gate`) 검사.

가짜 플러그인·steward로 gate 논리(스냅샷 기준선, 사용자 변경 겹침, 원장 재진입, 판정 보류, provider 정지,
제품 경로 필수화, 플러그인 계약 불일치 분류, manifest 조회와 preflight)를 결정적으로 확인한다.
FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT가 host-integration.json이 있는 plugin root를 가리키고 node가 있으면 실제 플러그인
스크립트·서명 CLI·MCP로 같은 Task 3개 Goal을 한 번 더 확인한다. 모델·Codex는 호출하지 않는다.
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
    CONSUMED_SURFACE, GovernanceContractMismatch, GovernancePlugin, GovernanceTaskGate, GovernanceTimeout,
    GovernanceUnavailable, McpStdioClient, MissingGovernanceGate, RoleSteward, load_model_classes, snapshot_worktree,
    user_change_overlap,
)
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from tests.fixtures.engine.governance import multitask
from tests.test_engine_qualification import qualification_inventory

ROOT = Path(__file__).resolve().parents[1]
# 실제 플러그인 검사는 host-integration.json이 있는 plugin root를 환경 변수로 받을 때만 실행한다. 고정 경로 default는 없다.
PLUGIN_ROOT = Path(os.environ["FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT"]) if os.environ.get(
    "FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT") else None


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
        self.responses: dict[str, object] = {}
        self.script_outputs: dict[str, object] = {}
        self.preflight_failure: Exception | None = None
        self.identity = {"summary": {"provenance": "local_derived", "closure_tree_digest": "sha256:" + "0" * 64},
                         "labels": [{"source": "plugin_manifest_file", "plugin": {"id": "fake", "version": "0"}}],
                         "files": {"fake.mjs": "0" * 64}}

    def preflight(self):
        if self.preflight_failure is not None:
            raise self.preflight_failure
        return json.loads(json.dumps(self.identity))

    def count(self, tool: str) -> int:
        return sum(1 for name, _, _ in self.calls if name == tool)

    def call(self, tool, arguments, observation):
        self.calls.append((tool, arguments, observation))
        if tool in self.failures:
            raise self.failures.pop(tool)
        if tool in self.responses:
            return self.responses.pop(tool)
        return {"content": [{"type": "text", "text": json.dumps(self.envelope(tool, arguments))}]}

    def envelope(self, tool, arguments):
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
        if name in self.script_outputs:
            return self.script_outputs.pop(name)
        if name == "acceptance-cli":
            passed = all(item["direction"] == "supports" for item in request["evidence"])
            return {"verdict": "PASS" if passed else "FAIL", "criteria": []}
        root = Path(request["repositoryRoot"])
        if name == "scope-baseline":
            entries = [{"path": path, "status": "clean", "sha256": blob}
                       for path, blob in sorted(ls_tree(root, request["commit"]).items())]
            return {"comparisonTarget": request["comparisonTarget"], "targetRef": request["commit"],
                    "entries": entries, "manifestSha256": hashlib.sha256(json.dumps(entries).encode()).hexdigest()}
        if name == "scope-compare":
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

    def gate(self, plugin=None, steward=None, **kwargs) -> GovernanceTaskGate:
        return GovernanceTaskGate(self.prepared.service, plugin=plugin or self.plugin, steward=steward or self.steward,
                                  state_dir=self.base / "governance", observe_worker=worker_observed, **kwargs)

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

    def run_state(self) -> str:
        with self.prepared.service.ledger.read() as connection:
            return connection.execute("SELECT run_state FROM projects WHERE id = ?",
                                      (self.prepared.project_id,)).fetchone()[0]

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
        captures = [request for name, request in self.plugin.scripts if name == "scope-baseline"]
        compares = [request for name, request in self.plugin.scripts if name == "scope-compare"]
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

    def repair_after_failed_validation(self, gate, before_retry=None) -> None:
        """add Task Worker가 잘못 고쳐 validation이 실패하면 실패 근거로 Task repair 재시도를 연다.

        validation 실패는 자동 분류되지 않으므로(TASK_VALIDATION_RECOVERY_REQUIRED) CLI `attempt retry`처럼
        RecoveryAssessment를 주어 retry_task를 부른다.
        """
        from flowmarshal.engine.domain import FailureClass, RecoveryAssessment, RepairAction, new_id

        wrong = {"task_fix_add": lambda root: replace(root / "app.py", "left + right", "left * right")}
        failed = self.drive(gate, wrong)
        self.assertEqual("TASK_VALIDATION_FAILED", failed.blocker_code, failed.detail)
        if before_retry is not None:
            before_retry(self.prepared.workspace)
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

    def task_row(self, ref: str):
        with self.prepared.service.ledger.read() as connection:
            return connection.execute("SELECT * FROM task_contracts WHERE id = ?",
                                      (self.prepared.task_ids[ref],)).fetchone()

    def test_task_repair_after_failed_validation_completes_the_goal_through_the_gate(self) -> None:
        gate = self.gate()
        self.repair_after_failed_validation(gate)
        task_id = self.prepared.task_ids["task_fix_add"]
        # Attempt 1이 바꾼 app.py로 입력이 stale해 같은 spec이 아니라 재관측 뒤 새 Execution Spec으로 준비한다.
        self.assertEqual("ready", self.statuses()["task_fix_add"])
        # 크기가 같은 편집이 같은 초에 겹치면 Python이 옛 .pyc를 써서 validation이 흔들리므로 크기를 바꿔 고친다.
        fix = {"task_fix_add": lambda root: replace(root / "app.py", "left * right", "(left + right)")}
        outcome = self.drive(gate, fix)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertIsNotNone(outcome.goal_verdict_id)
        self.assertEqual({"completed"}, set(self.statuses().values()))
        with self.prepared.service.ledger.read() as connection:
            specs = connection.execute("SELECT COUNT(*) FROM execution_spec_revisions WHERE task_id = ?",
                                       (task_id,)).fetchone()[0]
            attempts = [row["status"] for row in connection.execute(
                "SELECT status FROM attempts WHERE task_id = ? AND kind = 'execution' ORDER BY attempt_no", (task_id,))]
        self.assertEqual(2, specs)
        self.assertEqual(["succeeded", "succeeded"], attempts)
        # repair Attempt 2의 C0는 Attempt 1 Worker가 남긴 app.py를 담고, 그 변경은 사용자 변경이 아니다.
        snapshots = {item["key"]["attempt_no"]: item["data"] for item in self.gate_steps("snapshot_c0")
                     if item["key"]["task_id"] == task_id}
        self.assertIn("left * right", git(self.prepared.workspace, "show", f"{snapshots[2]['commit']}:app.py"))
        self.assertEqual([], self.history("task.governance_blocked"))
        self.assertTrue(self.history("task.retry_enabled")[-1]["execution_spec_refresh"])

    def test_task_repair_stops_when_the_user_edited_the_worker_output(self) -> None:
        from flowmarshal.engine.service import EngineServiceError

        gate = self.gate()
        with self.assertRaises(EngineServiceError) as raised:
            self.repair_after_failed_validation(
                gate, before_retry=lambda root: replace(root / "app.py", "left * right", "left * right  "))
        self.assertIn("REPAIR_INPUT_CHANGED", str(raised.exception))
        self.assertIn("app.py", str(raised.exception))
        # 사용자 편집은 재관측으로 흡수하지 않는다. Task는 실패 상태 그대로이고 새 spec을 준비하지 않는다.
        self.assertEqual("blocked", self.statuses()["task_fix_add"])
        self.assertEqual([], self.history("task.retry_enabled"))

    def test_task_repair_stops_when_the_user_deleted_the_worker_output(self) -> None:
        from flowmarshal.engine.service import EngineServiceError

        gate = self.gate()
        with self.assertRaises(EngineServiceError) as raised:
            self.repair_after_failed_validation(gate, before_retry=lambda root: (root / "app.py").unlink())
        # Worker 결과 기록이 내용을 남겼는데 파일이 없어졌으면 사용자 변경이다. 예외 문구가 아니라 같은 코드로 멈춘다.
        self.assertIn("REPAIR_INPUT_CHANGED", str(raised.exception))
        self.assertIn("app.py", str(raised.exception))
        self.assertEqual("blocked", self.statuses()["task_fix_add"])
        self.assertEqual([], self.history("task.retry_enabled"))

    def test_repair_reopens_when_the_worker_deleted_its_target_and_recorded_the_absence(self) -> None:
        from flowmarshal.engine.service import latest_write_observation

        # Worker가 쓰기 target을 지우고 성공 terminal로 끝나면 Attempt는 실패하고 after_digest null 관측이 남는다.
        recovered = self.drive(self.gate(), {"task_fix_add": lambda root: (root / "app.py").unlink()},
                               stop=lambda outcome: outcome.action is RunOnceAction.RECOVERED)
        self.assertEqual(RunOnceAction.RECOVERED, recovered.action, recovered.detail)
        with self.prepared.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT id, status FROM attempts WHERE task_id = ? AND kind = 'execution'",
                (self.prepared.task_ids["task_fix_add"],)).fetchone()
            observed = latest_write_observation(connection, attempt["id"], "app.py")
        self.assertEqual("failed", attempt["status"])
        # 기록된 부재(null)가 지금의 부재와 같으므로 Engine 변경이다. 예외 없이 새 Execution Spec 준비로 연다.
        self.assertEqual((True, None), observed)
        self.assertEqual("ready", self.statuses()["task_fix_add"])
        self.assertTrue(self.history("task.retry_enabled")[-1]["execution_spec_refresh"])

    def test_task_repair_keeps_the_recheck_error_for_an_unreadable_input(self) -> None:
        from unittest import mock

        from flowmarshal.engine.service import EngineServiceError

        # 있지만 읽을 수 없는 파일은 부재가 아니다. 사용자 변경으로 판정하지 않고 재확인 오류 그대로 멈춘다.
        patcher = mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied"))
        try:
            with self.assertRaises(EngineServiceError) as raised:
                self.repair_after_failed_validation(self.gate(), before_retry=lambda root: patcher.start())
        finally:
            patcher.stop()
        self.assertIn("재확인할 수 없습니다", str(raised.exception))
        self.assertNotIn("REPAIR_INPUT_CHANGED", str(raised.exception))
        self.assertEqual("blocked", self.statuses()["task_fix_add"])
        self.assertEqual([], self.history("task.retry_enabled"))

    def engine_write_decision(self, gate, task) -> list[str]:
        """이 Goal의 앞 dispatch 기록만으로 새 dispatch key의 app.py 사용자 변경 판정을 본다."""
        key = {"task_id": task["id"], "execution_spec_revision_id": "spec-next", "attempt_no": 99}
        dispatches = gate._goal_dispatches(task, key)
        return user_change_overlap(self.prepared.workspace, ["app.py"], dispatches[0]["data"],
                                   lambda path: gate._engine_output(dispatches, path))

    def test_user_edit_after_a_dispatch_without_attempt_blocks(self) -> None:
        # steward가 bootstrap에서 거절한 dispatch는 C0만 남기고 Worker를 실행하지 않는다.
        rejected = self.gate(steward=ScriptedSteward(reject=("bootstrap",)))
        self.assertIn("STEWARD_REJECTED", self.drive(rejected).detail)
        self.assertEqual(0, self.runtime.create_calls)
        replace(self.prepared.workspace / "app.py", "return", "return  ")
        self.assertEqual(["app.py"], self.engine_write_decision(rejected, self.task_row("task_fix_add")))

    def test_engine_write_stays_engine_after_a_later_dispatch_without_attempt(self) -> None:
        gate = self.gate()
        self.repair_after_failed_validation(gate)
        # Attempt 1이 app.py를 쓴 뒤 repair의 새 spec dispatch가 거절돼 Attempt 없이 C0만 남아도 Attempt 1 결과는 Engine 변경이다.
        rejected = self.gate(steward=ScriptedSteward(reject=("bootstrap",)))
        self.assertIn("STEWARD_REJECTED", self.drive(rejected).detail)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual([], self.engine_write_decision(rejected, self.task_row("task_fix_add")))
        replace(self.prepared.workspace / "app.py", "return", "return  ")
        self.assertEqual(["app.py"], self.engine_write_decision(rejected, self.task_row("task_fix_add")))

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


def mcp_result(envelope) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(envelope)}]}


class PluginContractTests(MultitaskGateHarness):
    """플러그인 계약·환경 불일치는 Task 상태와 원장 확정 결과를 건드리지 않고, 고치면 같은 키에서 이어 간다."""

    def gate_operations(self, event_type: str) -> list[dict]:
        return [item for item in self.history(event_type)
                if item.get("kind") == "governance_gate" or "GOVERNANCE_CONTRACT_MISMATCH" in item.get("detail", "")]

    def test_preflight_mismatch_before_dispatch_changes_nothing_and_unlocks_after_the_fix(self) -> None:
        self.plugin.preflight_failure = GovernanceContractMismatch("manifest_format", "v1", "v9")
        gate = self.gate()
        first = self.drive(gate)
        second = self.dispatcher(gate).run_once(self.prepared.project_id)
        for outcome in (first, second):
            self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", outcome.blocker_code)
            self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH: manifest_format: 기대 v1, 관측 v9", outcome.detail)
        self.assertEqual("materialized", self.statuses()["task_fix_add"])
        self.assertEqual([], self.gate_operations("operation.prepared"))
        self.assertEqual([], self.steward.stages)
        self.assertEqual("", git(self.prepared.workspace, "for-each-ref", "refs/flowmarshal"))
        self.assertEqual([{"phase": "before_execution", "detail": first.detail}],
                         self.history("task.governance_blocked"))
        self.plugin.preflight_failure = None
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)

    def test_preflight_mismatch_before_completion_keeps_the_task_validating(self) -> None:
        gate = self.gate()
        self.drive(gate, stop=lambda outcome: outcome.action is RunOnceAction.DISPATCHED)
        self.plugin.preflight_failure = GovernanceContractMismatch("closure_file", "plugin root 안의 파일", "gone.mjs")
        first = self.drive(gate)
        second = self.dispatcher(gate).run_once(self.prepared.project_id)
        for outcome in (first, second):
            self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", outcome.blocker_code)
        self.assertEqual("validating", self.statuses()["task_fix_add"])
        self.assertEqual([], self.history("task.validation_blocked"))
        self.assertEqual([{"phase": "before_completion", "detail": first.detail}],
                         self.history("task.governance_blocked"))
        self.plugin.preflight_failure = None
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(3, len(self.gate_steps("snapshot_c0")))

    def test_rejected_tool_call_is_not_stored_and_is_called_again_under_the_same_key(self) -> None:
        self.plugin.responses["open_convergence_root"] = mcp_result(
            {"ok": False, "data": None, "error": {"code": "BINDING_INVALID", "message": "below the floor"}})
        gate = self.gate()
        blocked = self.drive(gate)
        self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", blocked.blocker_code)
        self.assertIn("mcp_tool:open_convergence_root", blocked.detail)
        self.assertIn("BINDING_INVALID below the floor", blocked.detail)
        self.assertEqual([], self.gate_steps("root"))
        self.assertEqual(1, len(self.gate_operations("operation.no_effect")))
        self.assertEqual("materialized", self.statuses()["task_fix_add"])
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(4, self.plugin.count("open_convergence_root"))
        self.assertEqual(3, len(self.gate_steps("snapshot_c0")))
        self.assertEqual(["bootstrap"], [stage for stage, _ in self.steward.stages[:1]])
        self.assertEqual(1, sum(1 for stage, _ in self.steward.stages[:3] if stage == "bootstrap"))

    def test_malformed_effectful_response_is_stored_raw_and_never_requires_recovery(self) -> None:
        for response, fragment in (({"content": []}, "ok:true인 JSON envelope"),
                                   (mcp_result({"ok": True, "data": {"runId": 7, "revision": True}}), "'runId': 'int'")):
            with self.subTest(fragment=fragment):
                self.setUp()
                self.plugin.responses["start_guarded_workflow"] = response
                gate = self.gate()
                first = self.drive(gate)
                second = self.dispatcher(gate).run_once(self.prepared.project_id)
                for outcome in (first, second):
                    self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", outcome.blocker_code, outcome.detail)
                    self.assertIn("mcp_response:start_guarded_workflow", outcome.detail)
                    self.assertIn(fragment, outcome.detail)
                self.assertEqual(response, self.gate_steps("start")[0]["data"]["raw"])
                self.assertEqual(1, self.plugin.count("start_guarded_workflow"))
                self.assertNotEqual("recovery_required", self.run_state())
                self.assertEqual("materialized", self.statuses()["task_fix_add"])

    def test_malformed_plan_stage_requirement_is_a_mismatch_and_never_requires_recovery(self) -> None:
        # steward가 읽는 필드다. 검증 없이 넘기면 execute 안 예외가 되어 다음 tick이 recovery_required로 간다.
        for requirement in ("deep", {"minimumModelClass": "ultra"}):
            with self.subTest(requirement=requirement):
                self.setUp()
                data = self.plugin.envelope("plan_workflow", {})["data"]
                data["stages"][0]["executionRequirement"] = requirement
                self.plugin.responses["plan_workflow"] = mcp_result({"ok": True, "data": data, "error": None})
                gate = self.gate()
                first = self.drive(gate)
                second = self.dispatcher(gate).run_once(self.prepared.project_id)
                for outcome in (first, second):
                    self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", outcome.blocker_code, outcome.detail)
                    self.assertIn("mcp_response:plan_workflow:stage:executionRequirement", outcome.detail)
                self.assertEqual(["bootstrap"], [stage for stage, _ in self.steward.stages])
                self.assertNotEqual("recovery_required", self.run_state())
                self.assertEqual("materialized", self.statuses()["task_fix_add"])

    def test_malformed_script_output_has_no_effect_and_is_run_again(self) -> None:
        self.plugin.script_outputs["scope-baseline"] = {"manifestSha256": "0" * 64, "entries": "not-a-list"}
        gate = self.gate()
        blocked = self.drive(gate)
        self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", blocked.blocker_code)
        self.assertIn("script_output:scope-baseline", blocked.detail)
        self.assertNotEqual("recovery_required", self.run_state())
        self.assertEqual([], self.gate_steps("baseline"))
        self.plugin.script_outputs["scope-compare"] = {"verdict": "MAYBE", "summary": {}, "findings": [],
                                                       "currentDigest": "0" * 64}
        blocked = self.drive(gate)
        self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", blocked.blocker_code)
        self.assertIn("script_output:scope-compare:verdict", blocked.detail)
        self.assertEqual("validating", self.statuses()["task_fix_add"])
        self.assertNotEqual("recovery_required", self.run_state())
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)

    def test_plugin_identity_is_recorded_once_per_key_and_a_change_is_recorded_at_completion(self) -> None:
        gate = self.gate()
        self.drive(gate, stop=lambda outcome: outcome.action is RunOnceAction.DISPATCHED)
        recorded = self.gate_steps("plugin_identity")
        self.assertEqual(1, len(recorded))
        self.assertEqual(self.plugin.identity["summary"], recorded[0]["data"]["summary"])
        self.assertEqual("local_derived", recorded[0]["data"]["summary"]["provenance"])
        self.assertEqual(["plugin_manifest_file"], [label["source"] for label in recorded[0]["data"]["labels"]])
        self.assertEqual(self.plugin.identity["files"],
                         json.loads(Path(recorded[0]["data"]["files"]).read_text(encoding="utf-8")))
        before = dict(self.plugin.identity["summary"])
        self.plugin.identity["summary"]["closure_tree_digest"] = "sha256:" + "1" * 64
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        changed = self.gate_steps("plugin_identity_changed:before_completion")
        self.assertEqual(1, len(changed))
        self.assertEqual(before, changed[0]["data"]["stored"])
        self.assertEqual(self.plugin.identity["summary"], changed[0]["data"]["current"])
        self.assertEqual(3, len(self.gate_steps("plugin_identity")))


class RuntimeConformanceTests(MultitaskGateHarness):
    """제품 런타임은 처음 보는 plugin identity에서 적합성 검사를 한 번 실행하고 CoreOperations 기록으로 재생한다."""

    def conformance(self, verdict="PASS", failures=()):
        self.conformance_calls = 0
        pending = list(failures)

        def run():
            self.conformance_calls += 1
            if pending:
                raise pending.pop(0)
            return {"format": "flowmarshal-governance-conformance-v1", "provenance": "local_derived", "verdict": verdict,
                    "checks": [{"id": "plan", "status": verdict, "expected": "orchestrated", "observed": "direct"}]}

        return {"conformance": run, "conformance_check_set": "sha256:" + "1" * 64}

    def conformance_history(self, event_type):
        return [item for item in self.history(event_type) if item.get("kind") == "governance_conformance"
                or "conformance" in item.get("detail", "").lower()]

    def test_first_seen_identity_runs_once_and_is_replayed_for_later_tasks(self) -> None:
        gate = self.gate(**self.conformance())
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(1, self.conformance_calls)
        self.assertEqual(3, len(self.gate_steps("snapshot_c0")))
        completed = self.conformance_history("operation.completed")
        self.assertEqual(1, len(completed))
        self.assertEqual("local_derived", completed[0]["result"]["provenance"])
        # 새 gate(재시작)도 원장 기록으로 재생한다.
        restarted = self.gate(**self.conformance())
        restarted._conform({"project_id": self.prepared.project_id}, self.plugin.preflight())
        self.assertEqual(0, self.conformance_calls)

    def test_failed_conformance_is_replayed_as_a_mismatch_until_the_plugin_bytes_change(self) -> None:
        gate = self.gate(**self.conformance("FAIL"))
        first = self.drive(gate)
        second = self.dispatcher(gate).run_once(self.prepared.project_id)
        for outcome in (first, second):
            self.assertEqual("GOVERNANCE_CONTRACT_MISMATCH", outcome.blocker_code, outcome.detail)
            self.assertIn("GOVERNANCE_CONTRACT_MISMATCH: conformance:plan: 기대 orchestrated, 관측 direct", outcome.detail)
        self.assertEqual(1, self.conformance_calls)
        self.assertEqual("materialized", self.statuses()["task_fix_add"])
        self.assertEqual([], self.steward.stages)
        self.assertEqual([], self.gate_steps("plugin_identity"))
        self.assertEqual("", git(self.prepared.workspace, "for-each-ref", "refs/flowmarshal"))
        self.assertNotEqual("recovery_required", self.run_state())
        # 플러그인 bytes가 바뀌면 새 identity라 다시 검사한다.
        self.plugin.identity["summary"]["closure_tree_digest"] = "sha256:" + "2" * 64
        fixed = self.gate(**self.conformance())
        outcome = self.drive(fixed)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(1, self.conformance_calls)

    def test_environment_failure_is_retried_and_never_requires_recovery(self) -> None:
        gate = self.gate(**self.conformance(failures=[GovernanceUnavailable("GOVERNANCE_TIMEOUT: conformance")]))
        first = self.drive(gate)
        self.assertEqual("GOVERNANCE_GATE_PENDING", first.blocker_code, first.detail)
        self.assertEqual("materialized", self.statuses()["task_fix_add"])
        self.assertNotEqual("recovery_required", self.run_state())
        self.assertEqual(1, len(self.conformance_history("operation.no_effect")))
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(2, self.conformance_calls)

    def test_unexpected_conformance_error_is_retried_and_never_requires_recovery(self) -> None:
        """주입된 적합성 검사가 분류되지 않은 예외를 내도 gate가 효과 없는 실패로 바꾼다."""
        gate = self.gate(**self.conformance(failures=[OSError("적합성 검사가 예상 밖으로 죽었다")]))
        first = self.drive(gate)
        self.assertEqual("GOVERNANCE_GATE_PENDING", first.blocker_code, first.detail)
        self.assertIn("CONFORMANCE_ERROR", first.detail)
        self.assertEqual("materialized", self.statuses()["task_fix_add"])
        self.assertNotEqual("recovery_required", self.run_state())
        self.assertEqual(1, len(self.conformance_history("operation.no_effect")))
        outcome = self.drive(gate)
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertEqual(2, self.conformance_calls)

    def test_product_gate_always_binds_the_runtime_conformance_branch(self) -> None:
        from flowmarshal.engine.governance_conformance import CHECK_SET_DIGEST
        from flowmarshal.engine.governance_gate import GovernanceSettings

        settings = GovernanceSettings(self.base / "plugin-root", self.base / "governance-state")
        gate = settings.open_gate(self.prepared.service, runtime=self.runtime, roles=object(), runner=object())
        self.assertIsInstance(gate, GovernanceTaskGate)
        self.assertTrue(callable(gate.conformance))
        self.assertEqual(CHECK_SET_DIGEST, gate.conformance_check_set)
        with patch("flowmarshal.engine.governance_conformance.run_conformance", return_value={"verdict": "PASS"}) as run:
            self.assertEqual({"verdict": "PASS"}, gate.conformance())
            self.assertEqual({"verdict": "PASS"}, settings.check_conformance())
        self.assertEqual([((self.base / "plugin-root",), {})] * 2, [tuple(call) for call in run.call_args_list])


class PluginSurfaceTests(unittest.TestCase):
    """GovernancePlugin은 manifest로 진입점을 찾고 preflight로 실행 전에 계약·환경을 확인한다(node·플러그인 없이 검사)."""

    PATHS = {"mcp-server": "dist/server.mjs", "host-attestation-cli": "dist/sign.mjs",
             "scope-baseline": "moved/baseline.mjs", "scope-compare": "moved/compare.mjs",
             "acceptance-cli": "moved/cli.mjs"}

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "plugin"
        for relative in (*self.PATHS.values(), "data/schema.json"):
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / relative).write_text(relative, encoding="utf-8")
        self.manifest = {"format": governance_gate.MANIFEST_FORMAT, "plugin": {"id": "fake", "version": "9.9.9"},
                         "entryPoints": [{"id": entry_id, "path": path, "executionClosure": [path, "data/schema.json"]}
                                         for entry_id, path in self.PATHS.items()]}
        self.classes = self.base / "classes.json"
        self.write_classes({"observed-model": "deep"})
        self.node_version = b"v22.13.0\n"
        self.tools = list(CONSUMED_SURFACE["mcp_tools"])
        self.runs: list[tuple[list[str], dict]] = []
        self.clients: list[list[str]] = []
        test = self

        class StubClient:
            server_info = {"name": "stub-server", "version": "0"}

            def __init__(self, command, **kwargs):
                test.clients.append(command)

            def tool_names(self):
                if isinstance(test.tools, Exception):
                    raise test.tools
                return list(test.tools)

            def call(self, tool, arguments):
                return {"tool": tool, "arguments": arguments}

            def close(self):
                return None

        for name, value in (("McpStdioClient", StubClient), ("_run", self.fake_run)):
            patcher = patch.object(governance_gate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_run(self, command, **kwargs):
        self.runs.append((command, kwargs))
        output = self.node_version if command[1] == "--version" else json.dumps(
            {"token": "signed"} if command[1].endswith("sign.mjs") else {"ran": command[1:]}).encode("utf-8")
        return subprocess.CompletedProcess(command, 0, output, b"")

    def write_classes(self, classes, **extra) -> None:
        self.classes.write_text(json.dumps({"format": governance_gate.MODEL_CLASSES_FORMAT, "classes": classes,
                                            **extra}), encoding="utf-8")

    def plugin(self) -> GovernancePlugin:
        (self.root / governance_gate.MANIFEST).write_text(json.dumps(self.manifest), encoding="utf-8")
        return GovernancePlugin(self.root, self.base / "state", self.classes)

    def assert_mismatch(self, check: str) -> None:
        with self.assertRaises(GovernanceContractMismatch) as raised:
            self.plugin().preflight()
        self.assertTrue(str(raised.exception).startswith(f"GOVERNANCE_CONTRACT_MISMATCH: {check}: 기대 "), raised.exception)
        self.assertIn(", 관측 ", str(raised.exception))

    def test_entry_points_come_from_the_manifest_and_identity_is_derived_from_the_closure(self) -> None:
        plugin = self.plugin()
        identity = plugin.preflight()
        self.assertEqual([["node", str(self.root / "dist/server.mjs")]], self.clients)
        self.assertEqual(sorted([*self.PATHS.values(), "data/schema.json"]), sorted(identity["files"]))
        tree = "".join(f"{path}\0{hashlib.sha256(path.encode()).hexdigest()}\n" for path in sorted(identity["files"]))
        summary = identity["summary"]
        self.assertEqual("sha256:" + hashlib.sha256(tree.encode()).hexdigest(), summary["closure_tree_digest"])
        self.assertEqual("sha256:" + hashlib.sha256((self.root / governance_gate.MANIFEST).read_bytes()).hexdigest(),
                         summary["manifest_sha256"])
        self.assertEqual("sha256:" + hashlib.sha256(self.classes.read_bytes()).hexdigest(),
                         summary["model_class_table_digest"])
        self.assertEqual(("local_derived", 6, "v22.13.0"),
                         (summary["provenance"], summary["file_count"], summary["node_version"]))
        self.assertEqual([{"source": "plugin_manifest_file", "plugin": {"id": "fake", "version": "9.9.9"}},
                          {"source": "mcp_server_info", "serverInfo": {"name": "stub-server", "version": "0"}}],
                         identity["labels"])
        # version label은 판정에 쓰이지 않지만 manifest bytes라 manifest_sha256은 달라진다. tree digest는 closure만 따른다.
        self.manifest["plugin"]["version"] = "10.0.0"
        relabeled = self.plugin().preflight()["summary"]
        self.assertNotEqual(summary["manifest_sha256"], relabeled["manifest_sha256"])
        self.assertEqual(summary["closure_tree_digest"], relabeled["closure_tree_digest"])
        (self.root / "data/schema.json").write_text("changed", encoding="utf-8")
        changed = self.plugin().preflight()["summary"]
        self.assertNotEqual(summary["closure_tree_digest"], changed["closure_tree_digest"])

        request = self.base / "request.json"
        request.write_text("{}", encoding="utf-8")
        self.assertEqual({"ran": [str(self.root / "moved/baseline.mjs"), str(request)]},
                         plugin.script("scope-baseline", request))
        self.assertEqual({"ran": [str(self.root / "moved/cli.mjs"), "--input", str(request)]},
                         plugin.script("acceptance-cli", request))

    def test_signing_submits_the_host_and_the_class_from_the_injected_table(self) -> None:
        plugin = self.plugin()
        plugin.preflight()
        observation = {"model": "observed-model", "effort": "high", "actorId": "actor"}
        result = plugin.call("plan_workflow", {"a": 1}, observation)
        self.assertEqual({"a": 1, "_hostAttestation": "signed"}, result["arguments"])
        command, options = self.runs[-1]
        self.assertEqual(["node", str(self.root / "dist/sign.mjs")], command)
        self.assertEqual({"host": "flowmarshal-engine", "tool": "plan_workflow", "input": {"a": 1},
                          "model": "observed-model", "modelClass": "deep", "reasoningEffort": "high",
                          "actorId": "actor"}, json.loads(options["input"]))
        signed = len(self.runs)
        with self.assertRaises(GovernanceContractMismatch) as raised:
            plugin.call("plan_workflow", {}, {**observation, "model": "undeclared-model"})
        self.assertIn("GOVERNANCE_CONTRACT_MISMATCH: model_class:", str(raised.exception))
        self.assertIn("undeclared-model", str(raised.exception))
        self.assertEqual(signed, len(self.runs))

    def test_refused_or_malformed_signing_and_failed_scripts_are_effect_free_mismatches(self) -> None:
        plugin = self.plugin()
        plugin.preflight()
        observation = {"model": "observed-model", "effort": "high", "actorId": "actor"}
        request = self.base / "request.json"
        request.write_text("{}", encoding="utf-8")
        cases = (("host_attestation_output", 0, b'{"signed": "no token field"}', lambda: plugin.call("plan_workflow", {}, observation)),
                 ("host_attestation_output", 0, b"not json", lambda: plugin.call("plan_workflow", {}, observation)),
                 ("host_attestation:", 1, b"", lambda: plugin.call("plan_workflow", {}, observation)),
                 ("script_exit:scope-compare", 2, b"", lambda: plugin.script("scope-compare", request)))
        for check, code, output, act in cases:
            with self.subTest(check=check, output=output):
                with patch.object(governance_gate, "_run", lambda command, **kwargs: subprocess.CompletedProcess(
                        command, code, output, b"refused")):
                    with self.assertRaises(GovernanceContractMismatch) as raised:
                        act()
                self.assertIn(f"GOVERNANCE_CONTRACT_MISMATCH: {check}", str(raised.exception))
                # CoreOperations는 effects_started가 False인 예외만 no_effect로 남긴다.
                self.assertIs(False, raised.exception.effects_started)

    def test_preflight_names_the_failed_check(self) -> None:
        cases = {
            "manifest_format": lambda: self.manifest.update(format="agent-governance-suite.host-integration.v2"),
            "entry_point:scope-compare": lambda: self.manifest["entryPoints"].pop(3),
            "entry_point:mcp-server": lambda: self.manifest["entryPoints"][0].pop("executionClosure"),
            "entry_point:acceptance-cli": lambda: self.manifest["entryPoints"][4].update(id=["acceptance-cli"]),
            "entry_points": lambda: self.manifest.update(entryPoints=7),
            "closure_file": lambda: self.manifest["entryPoints"][1]["executionClosure"].append("missing.json"),
            "node_version": lambda: setattr(self, "node_version", b"v22.12.9\n"),
            "mcp_tools": lambda: self.tools.remove("finalize_workflow"),
            "mcp_server_start": lambda: setattr(self, "tools", GovernanceTimeout(
                "MCP_SERVER_EXITED: tools/list 응답 전에 서버가 끝났습니다.")),
            "model_class_table": lambda: self.write_classes({"observed-model": "huge"}),
        }
        for check, break_it in cases.items():
            with self.subTest(check=check):
                self.setUp()
                break_it()
                self.assert_mismatch(check)
        self.setUp()
        outside = self.base / "outside.json"
        outside.write_text("x", encoding="utf-8")
        for escaping in ("../outside.json", str(outside), ""):
            with self.subTest(path=escaping):
                self.manifest["entryPoints"][0]["executionClosure"][2:] = [escaping]
                self.assert_mismatch("closure_file")
        self.setUp()
        (self.root / governance_gate.MANIFEST).unlink(missing_ok=True)
        with self.assertRaises(GovernanceContractMismatch) as raised:
            GovernancePlugin(self.root, self.base / "state", self.classes).preflight()
        self.assertIn("GOVERNANCE_CONTRACT_MISMATCH: manifest:", str(raised.exception))

    def test_client_start_timeouts_stay_retryable_and_a_start_failure_is_a_mismatch(self) -> None:
        # initialize·tools/list의 timeout은 판정할 수 없는 상태다. 계약 불일치로 바꾸지 않고 그대로 다시 시도하게 둔다.
        for error in (GovernanceTimeout("GOVERNANCE_TIMEOUT: MCP tools/list: 120초"),
                      GovernanceUnavailable("GOVERNANCE_TIMEOUT: MCP initialize: 120초")):
            with self.subTest(error=str(error)):
                self.setUp()
                self.tools = error
                with self.assertRaises(type(error)) as raised:
                    self.plugin().preflight()
                self.assertIs(error, raised.exception)
        self.setUp()
        self.tools = GovernanceUnavailable("MCP_SERVER_START_FAILED: [WinError 2] node")
        self.assert_mismatch("mcp_server_start")

    def test_plan_stage_requirement_is_validated_from_the_consumed_surface_declaration(self) -> None:
        declared = CONSUMED_SURFACE["plan_stage_optional"]["executionRequirement"]
        self.assertEqual({None, *governance_gate.STEWARD_ROLE_BINDINGS}, set(declared["minimumModelClass"]))
        self.assertEqual(set(governance_gate.MODEL_CLASSES), set(governance_gate.STEWARD_ROLE_BINDINGS))
        stage = {"requiredCapability": "x", "stageId": "s", "executionRequirement": {"minimumModelClass": "deep"}}
        self.assertIs(stage, governance_gate._plan_stage(stage))
        # 검증은 선언을 읽는다. 선언이 바뀌면 판정과 소비 표면 digest가 함께 바뀐다.
        before = governance_gate.sha256_digest(CONSUMED_SURFACE)
        with patch.dict(declared, {"minimumModelClass": [None]}):
            self.assertNotEqual(before, governance_gate.sha256_digest(CONSUMED_SURFACE))
            with self.assertRaises(GovernanceContractMismatch):
                governance_gate._plan_stage(stage)

    def test_model_class_table_is_rejected_before_typed_use(self) -> None:
        self.assertEqual({"observed-model": "deep"}, load_model_classes(self.classes)[0])
        raw = {
            "duplicate model": '{"format": "%s", "classes": {"m": "deep", "m": "general"}}' % governance_gate.MODEL_CLASSES_FORMAT,
            "empty model": json.dumps({"format": governance_gate.MODEL_CLASSES_FORMAT, "classes": {" ": "deep"}}),
            "class outside the enum": json.dumps({"format": governance_gate.MODEL_CLASSES_FORMAT, "classes": {"m": None}}),
            "empty table": json.dumps({"format": governance_gate.MODEL_CLASSES_FORMAT, "classes": {}}),
            "unknown key": json.dumps({"format": governance_gate.MODEL_CLASSES_FORMAT, "classes": {"m": "deep"}, "x": 1}),
            "other format": json.dumps({"format": "other", "classes": {"m": "deep"}}),
            "not json": "{",
        }
        for name, text in raw.items():
            with self.subTest(name=name):
                self.classes.write_text(text, encoding="utf-8")
                with self.assertRaises(GovernanceContractMismatch) as raised:
                    load_model_classes(self.classes)
                self.assertIn("GOVERNANCE_CONTRACT_MISMATCH: model_class_table:", str(raised.exception))
        for path in (None, self.base / "absent.json"):
            with self.assertRaises(GovernanceContractMismatch):
                load_model_classes(path)

    def test_product_gate_does_not_pin_the_plugin(self) -> None:
        source = Path(governance_gate.__file__).read_text(encoding="utf-8")
        for name in ("PluginPin", "DEFAULT_PIN", "PLUGIN_PIN", "SERVER =", "SIGNER", "SCOPE_SCRIPTS", "ACCEPTANCE_CLI",
                     "engine-attestation", "server_sha256"):
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

class McpClientCleanupTests(unittest.TestCase):
    def test_close_finishes_cleanup_when_stdin_is_already_gone(self) -> None:
        """stdin.close()가 실패해도 프로세스·스레드 정리를 건너뛰지 않는다. 남은 프로세스가 앞 예외를 덮지 않게 한다."""
        done: list[str] = []

        def gone() -> None:
            raise OSError("stdin은 이미 끊겼다")

        stub = SimpleNamespace(
            process=SimpleNamespace(stdin=SimpleNamespace(close=gone),
                                    stdout=SimpleNamespace(close=lambda: done.append("stdout")),
                                    wait=lambda timeout: done.append("wait"),
                                    kill=lambda: done.append("kill")),
            _pump_thread=SimpleNamespace(join=lambda timeout: done.append("join")),
            _stderr=SimpleNamespace(close=lambda: done.append("stderr")))
        McpStdioClient.close(stub)
        self.assertEqual(["wait", "join", "stdout", "stderr"], done)


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


def plugin_unavailable() -> str | None:
    """실제 플러그인 검사를 건너뛰는 이유. 실행할 수 있으면 None이다."""
    if PLUGIN_ROOT is None:
        return "FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT가 없다"
    if not (PLUGIN_ROOT / governance_gate.MANIFEST).is_file():
        return f"{PLUGIN_ROOT}에 {governance_gate.MANIFEST}가 없다"
    return None if shutil.which("node") else "node가 없다"


@unittest.skipIf(plugin_unavailable(), f"실제 플러그인 검사 생략: {plugin_unavailable()}")
class RealPluginTests(MultitaskGateHarness):
    """환경 변수로 받은 plugin root의 실제 스크립트·서명 CLI·MCP 서버로 같은 합성 Goal을 확인한다(모델 호출 없음)."""

    def real_gate(self) -> GovernanceTaskGate:
        # ScriptedSteward와 worker_observed가 관측했다고 보고하는 모델의 class를 호출자 설정으로 준다.
        classes = self.base / "model-classes.json"
        classes.write_text(json.dumps({"format": governance_gate.MODEL_CLASSES_FORMAT, "classes": {
            "claude-sonnet-5": "general", "claude-opus-5": "deep"}}), encoding="utf-8")
        plugin = GovernancePlugin(PLUGIN_ROOT, self.base / "governance" / "plugin", classes)
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
