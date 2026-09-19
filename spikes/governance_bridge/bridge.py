"""FlowMarshal Task를 agent-governance-suite orchestrated workflow로 진행시키는 프로토타입 gate.

제품 wheel 밖의 spike다. ``EngineDispatcher(..., task_gate=GovernanceTaskGate(...))``로 끼운다.

- Worker dispatch 직전(before_execution): Task 계약에서 TaskEnvelope를 만들고 steward 검토 뒤
  plan_workflow → open_convergence_root → claim → start → 변경 기준선 stage까지 기록한다.
- Task 완료 직전(before_completion): 구현(Worker)·범위·수용 근거 stage를 기록하고 finalize한다.
  finalize가 passed가 아니면 차단 사유를 돌려준다. 완료 판정은 FlowMarshal Core가 한다.

MCP 서버는 flowmarshal-engine 호스트로 띄운다. plan·stage 호출마다 그 stage를 실제로 수행한
호출(steward 또는 Worker)의 관측 model·effort로 서명 CLI token을 받아 붙인다. 관측이 없으면
token을 만들지 않고 차단한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from flowmarshal.engine.domain import PlanContractRevision, TaskContract, TaskExecutionSpecRevision

SERVER = "mcp-server/dist/server.mjs"
SIGNER = "mcp-server/dist/engine-attestation.mjs"
SCOPE_SCRIPTS = "skills/change-scope-guardian/scripts"
ACCEPTANCE_CLI = "skills/acceptance-evidence-validator/scripts/cli.mjs"
SUPPORTED_CAPABILITIES = {
    "change-scope-baseline-capture", "minimal-implementation", "change-scope-assurance",
    "acceptance-evidence-validation",
}
SCOPE_STATES = {"PASS": "passed", "NEEDS_APPROVAL": "needs-approval", "BLOCKED": "blocked", "INCONCLUSIVE": "needs-input"}
ACCEPTANCE_STATES = {"PASS": "passed", "NEEDS_INPUT": "needs-input", "FAIL": "failed", "BLOCKED": "blocked"}
STATE_ERRORS = {("scope", "blocked"): "INVALID_INPUT", ("acceptance", "failed"): "GATE_FAILED",
                ("acceptance", "blocked"): "MISSING_EVIDENCE"}


class GovernanceBridgeError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@dataclass(frozen=True)
class PluginPin:
    """플러그인 worktree commit과 dist 번들 digest를 고정한다. 하나라도 다르면 시작하지 않는다."""

    root: Path
    commit: str
    server_sha256: str
    signer_sha256: str

    def verify(self) -> dict[str, str]:
        actual = {
            "commit": _git(self.root, "rev-parse", "HEAD").decode().strip(),
            "server_sha256": sha256_file(self.root / SERVER),
            "signer_sha256": sha256_file(self.root / SIGNER),
        }
        expected = {"commit": self.commit, "server_sha256": self.server_sha256, "signer_sha256": self.signer_sha256}
        if actual != expected:
            raise GovernanceBridgeError(f"PLUGIN_PIN_MISMATCH: expected {expected}, found {actual}")
        return {"root": str(self.root), **actual}


# W1 브랜치 claude/flowmarshal-engine-host의 검증·감사된 커밋. 플러그인 쪽이 바뀌면 다시 고정한다.
DEFAULT_PIN = PluginPin(
    root=Path(os.environ.get("AGS_PLUGIN_ROOT", "D:/claude/mcp변경/ags-engine-host")),
    commit="98db1302db8371883bc87c58017f7747bac2e77f",
    server_sha256="6e44ff66325df9f315675fe6e1faa8eb3591da1bc78b42aba289f3cb1704bca7",
    signer_sha256="d0232aa7e8780d7c8ccf611fa07b3c084760297253467353c24868054cc74592",
)


@dataclass(frozen=True)
class Observation:
    """stage를 실제로 수행한 호출의 관측. source는 관측원(예: Claude session transcript)이다."""

    model: str
    effort: str
    actor_id: str
    source: str


class Steward(Protocol):
    """governance stage의 의미 판단을 맡는 모델 역할. 결정과 그 호출의 관측을 돌려준다."""

    def review(self, stage: str, requirement: dict[str, Any] | None, brief: dict[str, Any]) -> tuple[dict[str, Any], Observation | None]: ...


class McpStdioClient:
    """플러그인 MCP 서버와 줄 단위 JSON-RPC로 통신한다."""

    def __init__(self, server: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(["node", str(server)], cwd=str(server.parents[2]), env=env, text=True,
                                        encoding="utf-8", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        self.sequence = 0
        self._request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "flowmarshal-governance-bridge", "version": "0.1.0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        self._send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        assert self.process.stdout is not None
        # ponytail: 응답을 동기로 기다린다. 서버가 멈추면 호출자도 멈추므로 제품화 때 timeout을 둔다.
        for line in self.process.stdout:
            message = json.loads(line)
            if message.get("id") == self.sequence:
                if "error" in message:
                    raise GovernanceBridgeError(f"MCP_PROTOCOL_ERROR: {message['error']}")
                return message["result"]
        raise GovernanceBridgeError("MCP_SERVER_EXITED")

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        return json.loads(result["content"][0]["text"])

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        self.process.wait(timeout=10)
        if self.process.stdout is not None:
            self.process.stdout.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GovernanceTaskGate:
    ENGINE_ACTOR = "flowmarshal-engine:governance-gate"

    def __init__(self, service: Any, *, pin: PluginPin, state_dir: Path, steward: Steward,
                 observe_worker: Callable[[str, str], Observation | None]) -> None:
        self.service = service
        self.plugin = pin.verify()
        self.root = pin.root
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        self.steward = steward
        self.observe_worker = observe_worker
        self.env = {**os.environ,
                    "AGENT_GOVERNANCE_HOST_ATTESTATION": "flowmarshal-engine",
                    "AGENT_GOVERNANCE_DB_PATH": str(state_dir / "workflows.sqlite3"),
                    "AGENT_GOVERNANCE_CONTINUITY_DB_PATH": str(state_dir / "continuity.sqlite3")}
        self.log_path = state_dir / "governance-log.jsonl"
        self.mcp = McpStdioClient(self.root / SERVER, self.env)
        self.runs: dict[str, dict[str, Any]] = {}
        self.blocked: dict[str, str] = {}
        self._log("plugin-pin", **self.plugin)

    def close(self) -> None:
        self.mcp.close()

    # ------------------------------------------------------------ 기록·호출
    def _log(self, event: str, **fields: Any) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": _now(), "event": event, **fields}, ensure_ascii=False) + "\n")

    def _attest(self, tool: str, arguments: dict[str, Any], observation: Observation) -> dict[str, Any]:
        request = {"tool": tool, "input": arguments, "model": observation.model,
                   "reasoningEffort": observation.effort, "actorId": observation.actor_id}
        signed = subprocess.run(["node", str(self.root / SIGNER)], input=json.dumps(request, ensure_ascii=False),
                                capture_output=True, text=True, encoding="utf-8", env=self.env)
        if signed.returncode != 0:
            raise GovernanceBridgeError(f"ATTESTATION_REFUSED: {signed.stderr.strip()}")
        return {**arguments, "_hostAttestation": json.loads(signed.stdout)["token"]}

    def _call(self, tool: str, arguments: dict[str, Any], observation: Observation | None = None) -> dict[str, Any]:
        sent = arguments if observation is None else self._attest(tool, arguments, observation)
        envelope = self.mcp.call(tool, sent)
        self._log("mcp", tool=tool, ok=envelope.get("ok"), error=envelope.get("error"),
                  attested=observation is not None,
                  observation=None if observation is None else observation.__dict__,
                  stageId=arguments.get("stageId"), state=(envelope.get("data") or {}).get("state"))
        if not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise GovernanceBridgeError(f"{tool}: {error.get('code')} {error.get('message')}")
        return envelope["data"]

    def _review(self, key: str, stage: str, requirement: dict[str, Any] | None, brief: dict[str, Any]) -> Observation:
        try:
            decision, observation = self.steward.review(key, requirement, brief)
        except Exception as error:  # steward 실패는 차단 사유로 돌려 Engine tick을 깨지 않는다.
            raise GovernanceBridgeError(f"STEWARD_FAILED: {stage}: {error}") from error
        self._log("steward", stage=key, decision=decision,
                  observation=None if observation is None else observation.__dict__)
        if observation is None:
            raise GovernanceBridgeError(f"STEWARD_OBSERVATION_UNAVAILABLE: {stage}")
        if not decision.get("accept"):
            raise GovernanceBridgeError(f"STEWARD_REJECTED: {stage}: {decision.get('rationale')}")
        return observation

    def _write(self, task_id: str, name: str, value: Any) -> Path:
        path = self.state_dir / task_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return path

    def _node(self, script: str, *args: str) -> dict[str, Any]:
        result = subprocess.run(["node", str(self.root / script), *args], capture_output=True,
                                text=True, encoding="utf-8")
        if result.returncode != 0:
            raise GovernanceBridgeError(f"SKILL_SCRIPT_FAILED: {script}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def _record(self, state: dict[str, Any], stage_key: str, stage_state: str, observation: Observation | None, *,
                output: dict[str, Any] | None = None, output_file: Path | None = None,
                artifacts: tuple[tuple[str, str, str], ...] = (), note: str = "") -> None:
        stage = state["stages"][stage_key]
        digest = None if output_file is None else sha256_file(output_file)
        # stateMapping이 오류를 요구하는 상태만 오류를 싣고, stage와 provider 결과에 같은 값을 둔다.
        code = STATE_ERRORS.get((stage_key, stage_state))
        error = None if code is None else {"code": code, "message": note, "details": None}
        arguments: dict[str, Any] = {
            "schemaVersion": "1.0.0", "runId": state["runId"], "stageId": stage["stageId"],
            "expectedRevision": state["revision"], "state": stage_state,
            "output": {"schemaVersion": "1.0.0", "kind": "output", "output": output, "error": error, "artifacts": [
                {"artifactId": artifact_id, "schemaId": schema_id, "locator": f"{output_file}{pointer}",
                 # v1.20.2: 범위 stage는 16진수만, 수용 근거 stage는 sha256: 접두사만 받는다.
                 "digest": f"{'sha256:' if stage_key == 'acceptance' else ''}{digest}",
                 "targetDigest": state["targetDigest"], "verified": True}
                for artifact_id, schema_id, pointer in artifacts]},
            # 통과 stage는 검증된 evidence가 하나 이상 있어야 한다. 산출물이 없으면 출력 파일 자체를 근거로 둔다.
            "evidence": [{"artifactId": artifact_id, "kind": "tool", "locator": f"{output_file}{pointer}",
                          "verified": True, "note": note} for artifact_id, _, pointer in artifacts or (("stage-output", "", ""),)],
            "findings": [], "blockers": [], "responseMode": "compact", "error": error,
        }
        if output_file is not None:
            arguments["outputFile"] = {"locator": str(output_file), "digest": f"sha256:{digest}"}
        state["revision"] = self._call("record_stage_result", arguments, observation)["revision"]

    # ------------------------------------------------------------ Task 계약 투영
    def _task_inputs(self, task: Any) -> tuple[TaskContract, TaskExecutionSpecRevision, PlanContractRevision, Path]:
        with self.service.ledger.read() as connection:
            spec = connection.execute("SELECT payload_json FROM execution_spec_revisions WHERE task_id = ? "
                                      "AND is_current = 1", (task["id"],)).fetchone()
            plan = connection.execute("SELECT payload_json FROM plan_revisions WHERE id = ?",
                                      (task["plan_revision_id"],)).fetchone()
            project = connection.execute("SELECT root FROM projects WHERE id = ?", (task["project_id"],)).fetchone()
        return (TaskContract.model_validate_json(task["payload_json"]),
                TaskExecutionSpecRevision.model_validate_json(spec["payload_json"]),
                PlanContractRevision.model_validate_json(plan["payload_json"]), Path(project["root"]))

    @staticmethod
    def _envelope(task_id: str, contract: TaskContract, spec: TaskExecutionSpecRevision,
                  plan: PlanContractRevision) -> dict[str, Any]:
        targets = spec.definition.resolved_targets
        writes = sorted({item.path for item in targets if item.access != "read"})
        reads = sorted({item.path for item in targets if item.access == "read"} - set(writes))
        prohibited = list(plan.definition.prohibited_effects) or ["쓰기 target 밖 변경"]
        return {
            "schemaVersion": "1.0.0", "taskId": f"fm-{task_id}", "objective": contract.objective,
            # 읽기 전용 target은 이 Task가 바꾸면 안 되는 경로다.
            "scope": {"included": writes, "excluded": reads},
            "acceptanceCriteria": [*contract.acceptance_criteria,
                                   *(f"{item.validation_id}: {item.statement}" for item in contract.validations)],
            "riskLevel": contract.risk_level.value,
            "workUnits": [{"id": "WU-1", "objective": contract.objective, "dependencies": [], "writeTargets": writes}],
            "requiredCapabilities": sorted(SUPPORTED_CAPABILITIES),
            "constraints": ["FlowMarshal Execution Spec의 쓰기 target만 바꾼다", *plan.definition.prohibited_effects],
            "authorization": {"allowedActions": ["Execution Spec의 쓰기 target 편집과 Task validation 실행"],
                              "prohibitedActions": prohibited, "approvalRequired": []},
            "decision": {"complexity": "simple", "hasConflicts": False},
            "orchestration": {"requested": True, "mcpAvailable": True},
        }

    # ------------------------------------------------------------ gate
    @staticmethod
    def _reason(error: Exception) -> str:
        return str(error) if isinstance(error, GovernanceBridgeError) else f"GATE_ERROR: {error!r}"

    def _abort(self, state: dict[str, Any]) -> None:
        try:
            self._call("abort_workflow", {"runId": state["runId"], "expectedRevision": state["revision"],
                                          "responseMode": "compact"})
        except Exception:
            pass

    def before_execution(self, task: Any) -> str | None:
        if task["id"] in self.runs:
            return None
        if task["id"] in self.blocked:
            # ponytail: 한 번 막힌 Task는 이 gate에서 다시 열지 않는다. 재시도는 새 계약·새 gate에서 한다.
            return self.blocked[task["id"]]
        try:
            # 기준선 stage까지 모두 기록된 뒤에만 run을 등록한다. 그 전 실패는 다음 tick에도 계속 막는다.
            self.runs[task["id"]] = self._open(task)
            return None
        except Exception as error:  # gate 실패는 차단 사유로 돌려 Engine tick을 깨지 않는다.
            self.blocked[task["id"]] = self._reason(error)
            self._log("blocked", phase="before_execution", taskId=task["id"], reason=self.blocked[task["id"]])
            return self.blocked[task["id"]]

    def before_completion(self, task: Any) -> str | None:
        state = self.runs.get(task["id"])
        if state is None:
            return "GOVERNANCE_RUN_MISSING: Worker dispatch 전에 governance workflow가 열리지 않았습니다."
        try:
            return self._close(task, state)
        except Exception as error:
            reason = self._reason(error)
            self._log("blocked", phase="before_completion", taskId=task["id"], reason=reason)
            self._abort(state)
            return reason

    def _open(self, task: Any) -> dict[str, Any]:
        contract, spec, plan, project_root = self._task_inputs(task)
        top = _git(project_root, "rev-parse", "--show-toplevel").decode().strip()
        if Path(top).resolve() != project_root.resolve():
            raise GovernanceBridgeError(f"GIT_REPOSITORY_REQUIRED: {project_root}는 git 저장소 루트가 아닙니다.")
        envelope = self._envelope(task["id"], contract, spec, plan)
        envelope_path = self._write(task["id"], "envelope.json", envelope)
        observation = self._review("bootstrap", "작업 계약", None, {
            "question": "이 TaskEnvelope가 FlowMarshal Task 계약의 목표·쓰기 범위·수용 기준을 빠짐없이 옮겼는가?",
            "taskEnvelope": envelope, "task": contract.model_dump(mode="json"),
            "resolvedTargets": [item.model_dump(mode="json") for item in spec.definition.resolved_targets]})
        workflow = self._call("plan_workflow", {"schemaVersion": "1.0.0", "taskEnvelope": envelope}, observation)
        stages = {stage["requiredCapability"]: stage for stage in workflow["stages"]}
        unsupported = sorted(set(stages) - SUPPORTED_CAPABILITIES)
        if workflow["executionMode"] != "orchestrated" or unsupported:
            raise GovernanceBridgeError(f"UNSUPPORTED_PLAN: mode={workflow['executionMode']} stages={unsupported}")
        tree_path = self.state_dir / task["id"] / "target-tree.txt"
        tree_path.write_bytes(_git(project_root, "ls-tree", "-r", "HEAD"))
        frame = {"schemaVersion": "1.0.0",
                 "workspace": {"workspaceId": f"flowmarshal-{task['project_id']}", "locator": str(project_root)},
                 "controlArtifacts": [{"artifactId": "task-envelope", "role": "pass-condition",
                                       "locator": str(envelope_path), "digest": f"sha256:{sha256_file(envelope_path)}"}],
                 "targetArtifacts": [{"artifactId": "project-worktree", "role": "target",
                                      "locator": f"{project_root} (git ls-tree -r HEAD)",
                                      "digest": f"sha256:{sha256_file(tree_path)}"}],
                 "operationalSettings": {"maxAttemptsPerEpoch": 3, "maxEpochs": 2, "leaseTtlSeconds": 21600}}
        root = self._call("open_convergence_root", {"schemaVersion": "1.0.0", "parentRootId": None,
                                                    "taskEnvelope": envelope, "frame": frame,
                                                    "userApprovalRefs": [], "responseMode": "compact"})
        lease = self._call("claim_workflow_attempt", {
            "schemaVersion": "1.0.0", "rootId": root["rootId"], "expectedRevision": root["revision"],
            "plan": workflow, "actorId": self.ENGINE_ACTOR, "outputTargets": [str(project_root)], "priorFailure": None})
        run = self._call("start_guarded_workflow", {"schemaVersion": "1.0.0", "leaseId": lease["leaseId"],
                                                    "expectedRootRevision": lease["rootRevision"],
                                                    "responseMode": "compact"})
        state = {"runId": run["runId"], "revision": run["revision"], "rootId": root["rootId"],
                 "envelope": envelope, "projectRoot": project_root, "targetDigest": "",
                 "stages": {"baseline": stages["change-scope-baseline-capture"],
                            "implementation": stages["minimal-implementation"],
                            "scope": stages["change-scope-assurance"],
                            "acceptance": stages["acceptance-evidence-validation"]}}
        try:
            request = self._write(task["id"], "capture-request.json", {
                "schemaVersion": "1.0.0", "repositoryRoot": str(project_root), "mode": "capture",
                "comparisonTarget": "working-tree", "taskEnvelope": envelope})
            baseline = self._node(f"{SCOPE_SCRIPTS}/capture-workspace-baseline.mjs", str(request))
            baseline_path = self._write(task["id"], "workspace-baseline.json", baseline)
            state["baseline"] = baseline
            state["targetDigest"] = baseline["manifestSha256"]
            observation = self._review("baseline", "변경 기준선", state["stages"]["baseline"].get("executionRequirement"), {
                "question": "이 기준선을 Worker 실행 전 상태로 쓸 수 있는가(쓰기 target에 이미 미커밋 변경이 없는가)?",
                "head": baseline["head"], "entries": len(baseline["entries"]),
                "dirty": [entry["path"] for entry in baseline["entries"] if entry["status"] != "clean"],
                "writeTargets": envelope["scope"]["included"]})
            self._record(state, "baseline", "passed", observation, output_file=baseline_path,
                         artifacts=(("workspace-baseline", "WorkspaceBaseline.v1", ""),),
                         note=f"capture-workspace-baseline working-tree, manifestSha256 {baseline['manifestSha256']}")
        except Exception:
            self._abort(state)
            raise
        return state

    def _worker_thread(self, task_id: str) -> tuple[str, str]:
        with self.service.ledger.read() as connection:
            row = connection.execute("SELECT id, binding_json FROM attempts WHERE task_id = ? AND kind = 'execution' "
                                     "ORDER BY attempt_no DESC LIMIT 1", (task_id,)).fetchone()
        if row is None or not row["binding_json"]:
            raise GovernanceBridgeError("WORKER_ATTEMPT_MISSING")
        return row["id"], json.loads(row["binding_json"])["thread_id"]

    def _close(self, task: Any, state: dict[str, Any]) -> str | None:
        attempt_id, thread_id = self._worker_thread(task["id"])
        worker = self.observe_worker(attempt_id, thread_id)
        self._log("worker-observation", attemptId=attempt_id, threadId=thread_id,
                  observation=None if worker is None else worker.__dict__)
        if worker is None:
            raise GovernanceBridgeError(f"WORKER_OBSERVATION_UNAVAILABLE: {attempt_id}")
        implementation = self._write(task["id"], "implementation.json", {
            "summary": "FlowMarshal Worker Attempt가 Task를 구현했다.", "attemptId": attempt_id,
            "threadId": thread_id, "observation": worker.__dict__})
        self._record(state, "implementation", "passed", worker, output_file=implementation,
                     note=f"Worker attempt {attempt_id} thread {thread_id} observed via {worker.source}")

        envelope, project_root, envelope_id = state["envelope"], state["projectRoot"], task["id"]
        request = self._write(envelope_id, "verify-request.json", {
            "schemaVersion": "1.0.0", "repositoryRoot": str(project_root), "mode": "verify",
            "comparisonTarget": "working-tree", "taskEnvelope": envelope, "baseline": state["baseline"],
            "baselineArtifactDigest": state["baseline"]["manifestSha256"]})
        report = self._node(f"{SCOPE_SCRIPTS}/compare-change-scope.mjs", str(request))
        report_path = self._write(envelope_id, "change-scope-report.json", report)
        state["targetDigest"] = report["currentDigest"]
        scope_state = SCOPE_STATES[report["verdict"]]
        observation = self._review("scope", "범위 확인", state["stages"]["scope"].get("executionRequirement"), {
            "question": "변경 목록과 판정이 Task의 쓰기 범위를 정확히 반영하는가?",
            "verdict": report["verdict"], "summary": report["summary"], "findings": report["findings"],
            "changes": report.get("changes", [])})
        self._record(state, "scope", scope_state, observation, output_file=report_path, artifacts=(
            ("change-scope-report", "ChangeScopeReport.v1", ""),
            ("scoped-change-inventory", "ChangeScopeReport.v1#/changes", "#/changes"),
            ("ownership-collision-ledger", "ChangeScopeReport.v1#/summary", "#/summary")),
            note=f"compare-change-scope verdict {report['verdict']} findings {report['findings']}")
        if scope_state != "passed":
            return f"CHANGE_SCOPE_{report['verdict']}: {report['findings']}"

        acceptance_request = self._acceptance_request(task, state)
        request = self._write(envelope_id, "acceptance-request.json", acceptance_request)
        acceptance = self._node(ACCEPTANCE_CLI, "--input", str(request))
        acceptance_path = self._write(envelope_id, "acceptance-report.json", acceptance)
        acceptance_state = ACCEPTANCE_STATES[acceptance["verdict"]]
        observation = self._review("acceptance", "수용 근거", state["stages"]["acceptance"].get("executionRequirement"), {
            "question": "각 수용 기준의 판정이 연결된 FlowMarshal 검증 근거와 맞는가?",
            "verdict": acceptance["verdict"], "criteria": acceptance.get("criteria"),
            "evidence": acceptance_request["evidence"]})
        state["targetDigest"] = acceptance_request["target"]["digest"]
        self._record(state, "acceptance", acceptance_state, observation, output_file=acceptance_path, artifacts=(
            ("acceptance-evidence-report", "AcceptanceEvidenceReport.v1", ""),
            ("verified-evidence-index", "AcceptanceEvidenceReport.v1#/verifiedEvidenceIndex", "#/verifiedEvidenceIndex")),
            note=f"acceptance cli verdict {acceptance['verdict']}")
        if acceptance_state != "passed":
            return f"ACCEPTANCE_{acceptance['verdict']}"
        final = self._call("finalize_workflow", {"runId": state["runId"], "expectedRevision": state["revision"],
                                                 "responseMode": "compact"})
        return None if final["state"] == "passed" else f"GOVERNANCE_NOT_PASSED: {final['state']}"

    def _acceptance_request(self, task: Any, state: dict[str, Any]) -> dict[str, Any]:
        contract = TaskContract.model_validate_json(task["payload_json"])
        project_root = state["projectRoot"]
        diff = _git(project_root, "diff", "HEAD", "--binary")
        target = f"sha256:{hashlib.sha256(diff).hexdigest()}"
        own = len(contract.acceptance_criteria)
        task_criteria = [f"AC-{index + 1:03d}" for index in range(own)]
        with self.service.ledger.read() as connection:
            results = self.service.effective_task_validation_results(connection, task["id"])
            evidence_rows = {row["id"]: dict(row) for row in connection.execute(
                "SELECT id, kind, source_ref, observation, content_digest FROM evidence_records WHERE task_id = ?",
                (task["id"],))}
        evidence = []
        for row in results:
            payload = json.loads(row["payload_json"])
            index = next(i for i, item in enumerate(contract.validations) if item.validation_id == row["validation_id"])
            # ponytail: Task 수용 기준(자유 문장)은 Task validation 전체 PASS에 대응시킨다(Task 완료 의미).
            criteria = [*task_criteria, f"AC-{own + index + 1:03d}"]
            path = self._write(task["id"], f"evidence-{row['validation_id']}.json", {
                "validationResult": payload,
                "evidence": [evidence_rows[item] for item in payload.get("evidence_ids", []) if item in evidence_rows]})
            evidence.append({"id": f"fm-{row['validation_id']}", "kind": "test", "locator": str(path),
                             "digest": f"sha256:{sha256_file(path)}", "targetDigest": target, "verified": True,
                             "criterionIds": criteria,
                             "direction": "supports" if row["status"] == "pass" else "refutes",
                             "observedResult": f"FlowMarshal {row['validation_id']} {row['status']}: {payload.get('rationale')}"})
        return {"schemaVersion": "1.0.0", "taskEnvelope": state["envelope"],
                "target": {"kind": "observable-state", "identifier": f"{project_root} git diff HEAD", "digest": target},
                "evidence": evidence, "verificationCommands": [],
                "knownLimitations": ["Task 수용 기준(자유 문장)은 FlowMarshal Task validation 전체 PASS에 대응시켰다."],
                "criterionOverrides": []}
