"""활성화 뒤 실행 Task를 agent-governance-suite workflow로 진행시키는 필수 governance gate.

모든 실행 Task는 Worker dispatch 직전(``before_execution``)과 Task 완료 직전(``before_completion``)에
이 gate를 지난다. gate 판정은 Core 완료 판정에 더하는 AND 차단 조건일 뿐이다. steward 판단은
Task validation·semantic Validator·Goal Test가 아니며 Core evidence로 세지 않는다.

- 기준선: dispatch 직전 작업 트리를 임시 index로 스냅샷 commit C0에 담고, 완료 직전 C1과 플러그인
  범위 확인의 commit 모드로 비교한다. 사용자 HEAD·브랜치·index·작업 트리는 바꾸지 않는다. 앞 Task가
  커밋 없이 남긴 변경도 C0 안에 들어가므로 같은 파일을 여러 Task가 차례로 고칠 수 있다.
- 사용자 변경: 쓰기 target이 HEAD와 다르고 이 Goal의 Attempt가 만든 변경으로 설명되지 않으면 steward·Worker
  호출 전에 막는다. Goal 시작 전부터 있던 변경과 Goal 도중 사용자 편집은 막고, 앞 Attempt(실패한 Attempt 포함)의
  Worker 결과는 막지 않아 Task repair·재계획을 끊지 않는다.
- 기록: 스냅샷·steward·MCP·스크립트 효과는 (Task, Execution Spec revision, Attempt 번호) 키로
  CoreOperations에 효과 전 intent와 완료 결과를 남긴다. 재시작·부분 실패 뒤에는 같은 키의 완료 결과를
  그대로 재사용하고, 결과 없는 효과는 external_unknown으로 멈춘다.
- 관측: 플러그인 stage 관측은 steward·Worker receipt의 권위 관측만 쓴다. 관측 근거가 확인되지 않은
  provider(Codex)는 GOVERNANCE_PROVIDER_UNSUPPORTED로 멈추며 다른 provider로 넘어가지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..canonical import sha256_bytes, sha256_digest
from .context import DEFAULT_IGNORED_DIRECTORIES
from .domain import BudgetStage, BudgetUsageRecord, PlanContractRevision, TaskContract, TaskExecutionSpecRevision, new_id
from .model_observation import authoritative_receipt_model_observation
from .operations import CoreOperations
from .runtime import TaskGatePending

SERVER = "mcp-server/dist/server.mjs"
SIGNER = "mcp-server/dist/engine-attestation.mjs"
SCOPE_SCRIPTS = "skills/change-scope-guardian/scripts"
ACCEPTANCE_CLI = "skills/acceptance-evidence-validator/scripts/cli.mjs"
SUPPORTED_CAPABILITIES = frozenset({
    "change-scope-baseline-capture", "minimal-implementation", "change-scope-assurance",
    "acceptance-evidence-validation",
})
SCOPE_STATES = {"PASS": "passed", "NEEDS_APPROVAL": "needs-approval", "BLOCKED": "blocked", "INCONCLUSIVE": "needs-input"}
ACCEPTANCE_STATES = {"PASS": "passed", "NEEDS_INPUT": "needs-input", "FAIL": "failed", "BLOCKED": "blocked"}
STATE_ERRORS = {("scope", "blocked"): "INVALID_INPUT", ("acceptance", "failed"): "GATE_FAILED",
                ("acceptance", "blocked"): "MISSING_EVIDENCE"}
# 플러그인 stage 하한(model class)을 역할 설정 binding에 대응시킨다. 모델 이름은 역할 설정에만 있다.
STEWARD_ROLE_BINDINGS = {"lightweight": "general_reviewer", "general": "general_reviewer",
                         "deep": "critical_reviewer", "frontier": "critical_reviewer"}
STEWARD_ROLE = "governance_steward"
DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["accept", "rationale"],
    "properties": {"accept": {"type": "boolean"}, "rationale": {"type": "string"}},
}
STEWARD_INSTRUCTIONS = (
    "당신은 FlowMarshal governance steward다. payload.question에 payload에 담긴 근거만으로 답한다. "
    "파일을 만들거나 고치지 않고 명령도 실행하지 않는다. 근거가 질문을 충족할 때만 accept를 true로 하고 "
    "rationale에 한국어 한두 문장으로 판단 근거를 쓴다."
)
GATE_KIND = "governance_gate"
MCP_TIMEOUT_SECONDS = 120.0
SCRIPT_TIMEOUT_SECONDS = 300.0
GIT_TIMEOUT_SECONDS = 120.0
SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "flowmarshal-engine", "GIT_AUTHOR_EMAIL": "flowmarshal-engine@localhost",
    "GIT_COMMITTER_NAME": "flowmarshal-engine", "GIT_COMMITTER_EMAIL": "flowmarshal-engine@localhost",
    # 같은 트리·부모·메시지면 같은 commit이 되도록 날짜를 고정한다.
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+00:00",
}


class GovernanceRejected(RuntimeError):
    """확정 차단이다. 같은 키에서는 원장에 저장된 결과로 같은 판정이 재생된다.

    효과 실행 도중(CoreOperations execute 안) 나는 거절은 고정값 불일치·서명 거부·입력 오류처럼 외부 상태를
    바꾸기 전의 실패이므로 no_effect로 기록한다.
    """

    effects_started = False


class GovernanceUnavailable(RuntimeError):
    """외부 효과를 시작하지 않은 실패다. CoreOperations가 no_effect로 기록하고 다음 tick에 다시 시도한다."""

    effects_started = False


class GovernanceTimeout(RuntimeError):
    """응답 시간을 넘겼다. 효과 여부를 모르므로 다음 tick은 external_unknown으로 멈춘다."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: list[str], *, timeout: float, code: str, effects_started: bool, **kwargs: Any) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as error:
        if effects_started:
            raise GovernanceTimeout(f"GOVERNANCE_TIMEOUT: {code}: {timeout}초") from error
        raise GovernanceUnavailable(f"GOVERNANCE_TIMEOUT: {code}: {timeout}초") from error
    except OSError as error:
        raise GovernanceUnavailable(f"{code}: {error}") from error
    return result


def _git(root: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = _run(["git", "-C", str(root), *args], timeout=GIT_TIMEOUT_SECONDS, code="GOVERNANCE_GIT",
                  effects_started=False, env=env)
    if check and result.returncode != 0:
        raise GovernanceUnavailable(
            f"GOVERNANCE_GIT_FAILED: git {' '.join(args[:2])}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""


def _excluded_pathspecs(root: Path, excluded: tuple[Path, ...]) -> list[str]:
    specs = [f":(exclude,glob)**/{name}/**" for name in sorted(DEFAULT_IGNORED_DIRECTORIES) if name != ".git"]
    for path in excluded:
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        specs.append(f":(exclude,glob){relative}/**")
    return specs


def snapshot_worktree(root: Path, *, ref: str, message: str, excluded: tuple[Path, ...] = ()) -> dict[str, Any]:
    """작업 트리(.gitignore 적용, untracked 포함)를 스냅샷 commit으로 만들고 보호 ref에 고정한다.

    임시 index만 쓰므로 사용자 HEAD·브랜치·index·작업 트리는 바뀌지 않는다. Engine 무시 경로(가상환경,
    캐시, 빌드 산출물, Engine artifact)는 빼서 validation 산출물이 범위 판정에 섞이지 않게 한다.
    """
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False) or None
    with tempfile.TemporaryDirectory(prefix="flowmarshal-governance-") as temp:
        env = {**os.environ, **SNAPSHOT_IDENTITY, "GIT_INDEX_FILE": str(Path(temp) / "index")}
        if head is not None:
            _git(root, "read-tree", head, env=env)
        _git(root, "add", "-A", "--", ".", *_excluded_pathspecs(root, excluded), env=env)
        tree = _git(root, "write-tree", env=env)
        parents = () if head is None else ("-p", head)
        commit = _git(root, "commit-tree", "--no-gpg-sign", tree, *parents, "-m", message, env=env)
    _git(root, "update-ref", ref, commit)
    return {"commit": commit, "tree": tree, "head": head, "ref": ref}


def _blob(root: Path, spec: str) -> str | None:
    return _git(root, "rev-parse", "--verify", "-q", spec, check=False) or None


def user_change_overlap(root: Path, writes: list[str], goal_start: dict[str, Any] | None,
                        engine_output: Callable[[str], tuple[bool, str | None]]) -> list[str]:
    """쓰기 target 중 이 Goal의 Attempt가 만들지 않은 미커밋 변경(사용자 변경)이 있는 경로를 돌려준다.

    지금 내용이 HEAD와 같으면(커밋·복원) 막지 않는다. goal_start는 이 Goal의 첫 governed dispatch 스냅샷(C0)이다.
    Goal 시작 때 HEAD와 같았던 경로를 이 Goal의 앞 dispatch가 쓰기 target으로 삼았다면 Engine 변경일 수 있다.
    engine_output(path)는 (앞 dispatch가 그 경로를 썼는가, 마지막 Worker 결과 sha256 또는 기록 없음)을 돌려준다.
    Worker 결과 기록이 있으면 지금 내용이 그와 같을 때만, 기록이 없으면(Worker가 실패해 결과를 남기지 못함) 그대로
    Engine 변경으로 본다. 그 밖은 Goal 시작 전부터 있던 변경이나 Goal 도중 사용자 편집이다.
    """
    overlaps = []
    for path in writes:
        current = _git(root, "hash-object", "--", path, check=False) if (root / path).is_file() else None
        if current == _blob(root, f"HEAD:{path}"):
            continue
        if goal_start is not None:
            start_head = None if goal_start["head"] is None else _blob(root, f"{goal_start['head']}:{path}")
            written, output = engine_output(path)
            if written and _blob(root, f"{goal_start['commit']}:{path}") == start_head:
                content = sha256_bytes((root / path).read_bytes()) if (root / path).is_file() else None
                if output is None or output == content:
                    continue
        overlaps.append(path)
    return overlaps


@dataclass(frozen=True)
class PluginPin:
    """플러그인 commit과 dist 번들 digest. 하나라도 다르면 gate를 시작하지 않는다."""

    commit: str
    server_sha256: str
    signer_sha256: str

    def verify(self, root: Path) -> dict[str, str]:
        actual = {"commit": _git(root, "rev-parse", "HEAD") if (root / ".git").exists() else "",
                  "server_sha256": _sha256_file(root / SERVER) if (root / SERVER).is_file() else "",
                  "signer_sha256": _sha256_file(root / SIGNER) if (root / SIGNER).is_file() else ""}
        expected = {"commit": self.commit, "server_sha256": self.server_sha256, "signer_sha256": self.signer_sha256}
        if actual != expected:
            raise GovernanceRejected(f"PLUGIN_PIN_MISMATCH: expected {expected}, found {actual}")
        return {"root": str(root), **actual}


# ponytail: 플러그인 host adapter가 들어간 개발 커밋이다. release 버전·content digest pin과 freeze 결속은
# 플러그인 release 뒤 별도 작업으로 바꾼다(marketplace 설치본에는 .git과 서명 CLI가 없다).
DEFAULT_PIN = PluginPin(
    commit="98db1302db8371883bc87c58017f7747bac2e77f",
    server_sha256="6e44ff66325df9f315675fe6e1faa8eb3591da1bc78b42aba289f3cb1704bca7",
    signer_sha256="d0232aa7e8780d7c8ccf611fa07b3c084760297253467353c24868054cc74592",
)
PLUGIN_ROOT_ENV = "FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT"


class McpStdioClient:
    """플러그인 MCP 서버와 줄 단위 JSON-RPC로 통신한다. 모든 응답 대기에 timeout이 있다."""

    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str], stderr_path: Path,
                 timeout: float = MCP_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._stderr = stderr_path.open("ab")
        try:
            self.process = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=self._stderr)
        except OSError as error:
            self._stderr.close()
            raise GovernanceUnavailable(f"MCP_SERVER_START_FAILED: {error}") from error
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()
        self.sequence = 0
        try:
            self._request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "flowmarshal-engine-governance", "version": "1.0"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except BaseException:
            self.process.kill()
            self.close()
            raise

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError as error:
            raise GovernanceUnavailable(f"MCP_SERVER_EXITED: {error}") from error

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        self._send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        while True:
            try:
                line = self._lines.get(timeout=self.timeout)
            except queue.Empty as error:
                raise GovernanceTimeout(f"GOVERNANCE_TIMEOUT: MCP {method}: {self.timeout}초") from error
            if line is None:
                raise GovernanceTimeout(f"MCP_SERVER_EXITED: {method} 응답 전에 서버가 끝났습니다.")
            message = json.loads(line)
            if message.get("id") == self.sequence:
                if "error" in message:
                    raise GovernanceRejected(f"MCP_PROTOCOL_ERROR: {message['error']}")
                return message["result"]

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        return json.loads(result["content"][0]["text"])

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self._pump_thread.join(timeout=10)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr.close()


class GovernancePlugin:
    """고정한 플러그인의 MCP 서버·서명 CLI·skill 스크립트 실행기. 서버는 첫 호출 때 띄운다."""

    def __init__(self, root: Path, state_dir: Path, pin: PluginPin = DEFAULT_PIN) -> None:
        self.root, self.state_dir, self.pin = root, state_dir, pin
        state_dir.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ,
                    "AGENT_GOVERNANCE_HOST_ATTESTATION": "flowmarshal-engine",
                    "AGENT_GOVERNANCE_DB_PATH": str(state_dir / "workflows.sqlite3"),
                    "AGENT_GOVERNANCE_CONTINUITY_DB_PATH": str(state_dir / "continuity.sqlite3")}
        self._mcp: McpStdioClient | None = None
        self.identity: dict[str, str] | None = None

    def _client(self) -> McpStdioClient:
        if self._mcp is None:
            self.identity = self.pin.verify(self.root)
            self._mcp = McpStdioClient(["node", str(self.root / SERVER)], cwd=self.root, env=self.env,
                                       stderr_path=self.state_dir / "mcp-server.stderr.log")
        return self._mcp

    def call(self, tool: str, arguments: dict[str, Any], observation: dict[str, Any] | None) -> dict[str, Any]:
        client = self._client()
        if observation is not None:
            signed = _run(["node", str(self.root / SIGNER)], timeout=SCRIPT_TIMEOUT_SECONDS,
                          code="GOVERNANCE_ATTESTATION", effects_started=False, env=self.env,
                          input=json.dumps({"tool": tool, "input": arguments, "model": observation["model"],
                                            "reasoningEffort": observation["effort"],
                                            "actorId": observation["actorId"]}, ensure_ascii=False).encode("utf-8"))
            if signed.returncode != 0:
                raise GovernanceRejected(f"ATTESTATION_REFUSED: {signed.stderr.decode('utf-8', 'replace').strip()}")
            arguments = {**arguments, "_hostAttestation": json.loads(signed.stdout)["token"]}
        return client.call(tool, arguments)

    def script(self, name: str, request_path: Path) -> dict[str, Any]:
        args = ["--input", str(request_path)] if name == ACCEPTANCE_CLI else [str(request_path)]
        result = _run(["node", str(self.root / name), *args], timeout=SCRIPT_TIMEOUT_SECONDS,
                      code=f"GOVERNANCE_SCRIPT {name}", effects_started=False, env=self.env)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
            raise GovernanceRejected(f"SKILL_SCRIPT_FAILED: {name}: {detail}")
        return json.loads(result.stdout)

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None


def _active_goal(service: Any, project_id: str) -> Any:
    with service.ledger.read() as connection:
        return connection.execute(
            "SELECT g.goal_id, g.definition_digest FROM projects p "
            "JOIN goal_revisions g ON g.id = p.active_goal_revision_id WHERE p.id = ?", (project_id,)).fetchone()


class RoleSteward:
    """governance stage의 의미 판단을 역할 설정의 reviewer binding으로 호출한다.

    예산 예약·정산은 BudgetedRoleRunner, usage는 validation 예산 stage의 governance_steward 역할로 남긴다.
    관측은 receipt의 권위 관측(model_observation_source가 있는 observed model·effort)만 쓴다.
    """

    def __init__(self, service: Any, *, runtime: Any, roles: Any, runner: Any, cwd: Path) -> None:
        self.service, self.runtime, self.roles, self.runner, self.cwd = service, runtime, roles, runner, cwd

    def ensure_supported(self) -> None:
        # Claude provider(설정 카탈로그 inventory)만 session 기록에서 turn별 model·effort를 관측한다. Codex App
        # Server는 그 근거가 확인되지 않았으므로 관측 없는 stage 기록을 만들지 않도록 steward 호출 전에 멈추고
        # 다른 provider로 넘어가지 않는다. 평가용 wrapper를 거쳐도 inventory 출처는 그대로다.
        provenance = self.runtime.list_models().inventory_provenance
        if provenance != "configured_catalog":
            raise GovernanceRejected(
                "GOVERNANCE_PROVIDER_UNSUPPORTED: 필수 governance 연동은 관측 근거가 확인된 Claude provider에서만 "
                f"실행합니다(현재 inventory 출처 {provenance}).")

    def review(self, task: Any, stage: str, requirement: dict[str, Any] | None, brief: dict[str, Any]) -> dict[str, Any]:
        from .budget import BudgetedRoleRunner
        from .roles import RoleCallReceipt, StructuredRoleError, make_role_request, verify_role_receipt

        model_class = (requirement or {}).get("minimumModelClass") or "general"
        binding = self.roles.binding_for(STEWARD_ROLE_BINDINGS[model_class])
        inventory = self.runtime.list_models()
        self.cwd.mkdir(parents=True, exist_ok=True)
        request = make_role_request(
            inventory=inventory, allowed_fallbacks=binding.allowed_fallbacks, role=STEWARD_ROLE,
            instructions=STEWARD_INSTRUCTIONS, payload=json.loads(json.dumps({"stage": stage, **brief}, default=str)),
            output_schema=DECISION_SCHEMA, model=binding.model, effort=binding.effort,
            inventory_digest=inventory.inventory_digest, cwd=str(self.cwd))
        goal = _active_goal(self.service, task["project_id"])
        runner = BudgetedRoleRunner(self.runner, self.service, project_id=task["project_id"],
                                    goal_id=goal["goal_id"], goal_digest=goal["definition_digest"])
        try:
            result = runner.run(request)
        except StructuredRoleError as error:
            if error.receipt is None:
                raise
            # provider가 종료했지만 유효한 판단을 내지 못했다. 재호출하지 않고 실패를 판정으로 남긴다.
            return {"decision": None, "observation": None, "failure": str(error), "callId": error.receipt.call_id}
        verify_role_receipt(request, result)
        receipt = RoleCallReceipt.model_validate(result.receipt)
        self._record_usage(task["project_id"], goal["definition_digest"], receipt)
        model, effort = authoritative_receipt_model_observation(
            observed_model=receipt.observed_model, observed_effort=receipt.observed_effort,
            binding_provenance=receipt.binding_provenance)
        observation = None if model is None else {
            "model": model, "effort": effort, "source": (receipt.binding_provenance or {}).get("observed"),
            "actorId": f"flowmarshal-engine:steward:{stage}:{receipt.thread_id}"}
        return {"decision": result.payload, "observation": observation, "failure": None, "callId": receipt.call_id}

    def _record_usage(self, project_id: str, goal_digest: str, receipt: Any) -> None:
        with self.service.ledger.read() as connection:
            if connection.execute("SELECT id FROM budget_usage WHERE project_id = ? AND logical_call_ref = ?",
                                  (project_id, receipt.call_id)).fetchone() is not None:
                return
        values = receipt.model_dump()
        fields = ("role", "model", "effort", "permission_profile", "approval_policy", "thread_id", "turn_ids",
                  "input_digest", "output_digest", "output_schema_digest", "input_tokens", "cached_input_tokens",
                  "output_tokens", "reasoning_tokens", "latency_ms", "usage_available", "recorded_at")
        provenance = {key: values[key] for key in (
            "binding_provenance_version", "requested_model", "requested_effort", "observed_model",
            "observed_effort", "provider_inventory_digest", "adapter_capability_digest", "binding_provenance")
            if values.get(key) is not None}
        self.service.record_budget_usage(BudgetUsageRecord(
            usage_id=new_id("usage"), project_id=project_id, goal_contract_digest=goal_digest,
            stage=BudgetStage.VALIDATION, logical_call_ref=receipt.call_id, call_status=receipt.status,
            runner_receipt_digest=sha256_digest(receipt), retry_count=receipt.schema_recovery_attempts,
            **{key: values[key] for key in fields}, **provenance,
        ))


def ledger_worker_observation(service: Any, attempt_id: str) -> dict[str, Any] | None:
    """Worker Attempt의 원장 usage에 기록된 권위 관측을 돌려준다. 없으면 None."""
    with service.ledger.read() as connection:
        attempt = connection.execute("SELECT project_id, binding_json FROM attempts WHERE id = ?",
                                     (attempt_id,)).fetchone()
        if attempt is None or not attempt["binding_json"]:
            return None
        thread_id = json.loads(attempt["binding_json"]).get("thread_id")
        rows = connection.execute(
            "SELECT payload_json FROM budget_usage WHERE project_id = ? AND stage = 'execution' "
            "AND json_extract(payload_json, '$.thread_id') = ? ORDER BY recorded_at DESC, rowid DESC",
            (attempt["project_id"], thread_id)).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        model, effort = authoritative_receipt_model_observation(
            observed_model=payload.get("observed_model"), observed_effort=payload.get("observed_effort"),
            binding_provenance=payload.get("binding_provenance"))
        if model is not None:
            return {"model": model, "effort": effort, "source": payload["binding_provenance"]["observed"],
                    "actorId": f"flowmarshal-engine:worker:{attempt_id}"}
    return None


class MissingGovernanceGate:
    """governance 설정 없이 실행 Task를 dispatch하지 못하게 막는 자리표시 gate."""

    def __init__(self, reason: str = ("GOVERNANCE_GATE_REQUIRED: 실행 Task는 agent-governance-suite gate를 거쳐야 "
                                      f"합니다. {PLUGIN_ROOT_ENV}와 역할 설정을 지정하십시오.")) -> None:
        self.reason = reason

    def before_execution(self, task: Any) -> str:
        return self.reason

    def before_completion(self, task: Any) -> str:
        return self.reason

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class GovernanceSettings:
    """제품 경로가 필수 gate를 만드는 입력. 플러그인 위치와 gate 산출물 폴더만 받는다."""

    plugin_root: Path
    state_dir: Path
    pin: PluginPin = DEFAULT_PIN

    @classmethod
    def from_environment(cls, state_dir: Path) -> "GovernanceSettings | None":
        root = os.environ.get(PLUGIN_ROOT_ENV)
        return None if not root else cls(Path(root), state_dir)

    def open_gate(self, service: Any, *, runtime: Any, roles: Any, runner: Any) -> Any:
        if roles is None or runner is None:
            return MissingGovernanceGate(
                "GOVERNANCE_ROLE_CONFIGURATION_REQUIRED: steward binding에 쓸 역할 설정이 필요합니다.")
        return GovernanceTaskGate(
            service, plugin=GovernancePlugin(self.plugin_root, self.state_dir / "plugin", self.pin),
            steward=RoleSteward(service, runtime=runtime, roles=roles, runner=runner,
                                cwd=self.state_dir / "steward-cwd"),
            state_dir=self.state_dir)


@dataclass
class _Run:
    key: dict[str, Any]
    root: Path
    folder: Path
    envelope: dict[str, Any]
    c0: dict[str, Any]
    run_id: str = ""
    revision: int = 0
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    target_digest: str = ""


class GovernanceTaskGate:
    ENGINE_ACTOR = "flowmarshal-engine:governance-gate"

    def __init__(self, service: Any, *, plugin: Any, steward: Any, state_dir: Path,
                 observe_worker: Callable[[str], dict[str, Any] | None] | None = None,
                 operations: CoreOperations | None = None) -> None:
        self.service, self.plugin, self.steward, self.state_dir = service, plugin, steward, state_dir
        self.observe_worker = observe_worker or (lambda attempt_id: ledger_worker_observation(service, attempt_id))
        self.operations = operations or CoreOperations(service)

    def close(self) -> None:
        self.plugin.close()

    # ------------------------------------------------------------ hook
    def before_execution(self, task: Any) -> str | None:
        return self._decide(lambda: self._open(task, self._key(task, dispatching=True), replay=False))

    def before_completion(self, task: Any) -> str | None:
        return self._decide(lambda: self._close(task, self._key(task, dispatching=False)))

    def _decide(self, action: Callable[[], Any]) -> str | None:
        from .operations import ExternalOperationUnknown

        try:
            action()
            return None
        except GovernanceRejected as error:
            return str(error)
        except ExternalOperationUnknown:
            raise
        except Exception as error:  # 판정하지 못한 실패는 Task 상태를 바꾸지 않고 다음 tick에 다시 본다.
            raise TaskGatePending(str(error) if isinstance(error, (GovernanceUnavailable, GovernanceTimeout))
                                  else f"GOVERNANCE_GATE_ERROR: {error!r}") from error

    # ------------------------------------------------------------ 원장 결속
    def _key(self, task: Any, *, dispatching: bool) -> dict[str, Any]:
        with self.service.ledger.read() as connection:
            spec = connection.execute("SELECT id FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                                      (task["id"],)).fetchone()
            attempts = connection.execute("SELECT COALESCE(MAX(attempt_no), 0) FROM attempts WHERE task_id = ? "
                                          "AND kind = 'execution'", (task["id"],)).fetchone()[0]
        if spec is None:
            raise GovernanceRejected("GOVERNANCE_EXECUTION_SPEC_MISSING: 현재 Execution Spec이 없습니다.")
        return {"task_id": task["id"], "execution_spec_revision_id": spec["id"],
                "attempt_no": int(attempts) + (1 if dispatching else 0)}

    def _op(self, task: Any, run: _Run | dict[str, Any], step: str, request: dict[str, Any],
            execute: Callable[[], dict[str, Any]], *, replay: bool) -> dict[str, Any]:
        key = run.key if isinstance(run, _Run) else run
        full = {"key": key, "step": step, **request}
        if replay:
            stored = self.operations.completed_result(project_id=task["project_id"], kind=GATE_KIND, request=full)
            if stored is None:
                raise GovernanceRejected(f"GOVERNANCE_RUN_MISSING: {step} 기록이 없습니다. Worker dispatch 전에 "
                                         "governance workflow가 열리지 않았습니다.")
            return stored["data"]
        return self.operations.invoke(project_id=task["project_id"], kind=GATE_KIND, request=full,
                                      execute=lambda: {"step": step, "key": key, "data": execute()})["data"]

    def _mcp(self, task: Any, run: _Run, step: str, tool: str, arguments: dict[str, Any],
             observation: dict[str, Any] | None, *, replay: bool) -> dict[str, Any]:
        envelope = self._op(task, run, step, {"tool": tool, "arguments": arguments, "observation": observation},
                            lambda: self.plugin.call(tool, arguments, observation), replay=replay)
        if not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise GovernanceRejected(f"GOVERNANCE_PLUGIN_REJECTED: {tool}: {error.get('code')} {error.get('message')}")
        return envelope["data"]

    def _review(self, task: Any, run: _Run, stage: str, requirement: dict[str, Any] | None,
                brief: dict[str, Any], *, replay: bool) -> dict[str, Any]:
        result = self._op(task, run, f"steward:{stage}", {"requirement": requirement, "brief": brief},
                          lambda: self.steward.review(task, stage, requirement, brief), replay=replay)
        if result.get("failure"):
            raise GovernanceRejected(f"STEWARD_FAILED: {stage}: {result['failure']}")
        if result["observation"] is None:
            raise GovernanceRejected(f"STEWARD_OBSERVATION_UNAVAILABLE: {stage}")
        if not result["decision"].get("accept"):
            raise GovernanceRejected(f"STEWARD_REJECTED: {stage}: {result['decision'].get('rationale')}")
        return result["observation"]

    def _script(self, task: Any, run: _Run, step: str, name: str, request: dict[str, Any], *,
                replay: bool) -> tuple[dict[str, Any], Path]:
        request_path = self._write(run.folder / f"{step}-request.json", request)
        output_path = run.folder / f"{step}.json"

        def execute() -> dict[str, Any]:
            output = self.plugin.script(name, request_path)
            self._write(output_path, output)
            return {"path": str(output_path), "sha256": _sha256_file(output_path)}

        stored = self._op(task, run, step, {"script": name, "request": sha256_digest(request)}, execute, replay=replay)
        if not output_path.is_file() or _sha256_file(output_path) != stored["sha256"]:
            raise GovernanceRejected(f"GOVERNANCE_ARTIFACT_CHANGED: {output_path}")
        return json.loads(output_path.read_text(encoding="utf-8")), output_path

    @staticmethod
    def _write(path: Path, value: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
        return path

    def _record(self, task: Any, run: _Run, stage_key: str, stage_state: str, observation: dict[str, Any] | None,
                *, output_file: Path, artifacts: tuple[tuple[str, str, str], ...] = (), note: str,
                replay: bool) -> None:
        stage = run.stages[stage_key]
        digest = _sha256_file(output_file)
        code = STATE_ERRORS.get((stage_key, stage_state))
        error = None if code is None else {"code": code, "message": note, "details": None}
        arguments: dict[str, Any] = {
            "schemaVersion": "1.0.0", "runId": run.run_id, "stageId": stage["stageId"],
            "expectedRevision": run.revision, "state": stage_state,
            "output": {"schemaVersion": "1.0.0", "kind": "output", "output": None, "error": error, "artifacts": [
                {"artifactId": artifact_id, "schemaId": schema_id, "locator": f"{output_file}{pointer}",
                 # 고정한 플러그인(v1.20.2 기준)은 범위 stage는 16진수만, 수용 근거 stage는 sha256: 접두사만 받는다.
                 "digest": f"{'sha256:' if stage_key == 'acceptance' else ''}{digest}",
                 "targetDigest": run.target_digest, "verified": True}
                for artifact_id, schema_id, pointer in artifacts]},
            # 통과 stage는 검증된 evidence가 하나 이상 있어야 한다. 산출물이 없으면 출력 파일 자체를 근거로 둔다.
            "evidence": [{"artifactId": artifact_id, "kind": "tool", "locator": f"{output_file}{pointer}",
                          "verified": True, "note": note} for artifact_id, _, pointer in artifacts or (("stage-output", "", ""),)],
            "findings": [], "blockers": [], "responseMode": "compact", "error": error,
            "outputFile": {"locator": str(output_file), "digest": f"sha256:{digest}"},
        }
        run.revision = self._mcp(task, run, f"record:{stage_key}", "record_stage_result", arguments, observation,
                                 replay=replay)["revision"]

    def _abort(self, task: Any, run: _Run) -> None:
        if not run.run_id:
            return
        try:
            self._mcp(task, run, "abort", "abort_workflow",
                      {"runId": run.run_id, "expectedRevision": run.revision, "responseMode": "compact"}, None,
                      replay=False)
        except GovernanceRejected:
            pass

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
    def _envelope(key: dict[str, Any], contract: TaskContract, spec: TaskExecutionSpecRevision,
                  plan: PlanContractRevision) -> dict[str, Any]:
        targets = spec.definition.resolved_targets
        writes = sorted({item.path for item in targets if item.access != "read"})
        reads = sorted({item.path for item in targets if item.access == "read"} - set(writes))
        prohibited = list(plan.definition.prohibited_effects) or ["쓰기 target 밖 변경"]
        return {
            "schemaVersion": "1.0.0", "taskId": f"fm-{key['task_id']}-a{key['attempt_no']}",
            "objective": contract.objective,
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

    def _goal_dispatches(self, task: Any, key: dict[str, Any]) -> list[dict[str, Any]]:
        """활성 Goal에서 gate가 앞서 연 dispatch의 C0 기록(오래된 순). 현재 키는 뺀다."""
        with self.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT h.payload_json FROM history_events h "
                "JOIN task_contracts t ON t.id = json_extract(h.payload_json, '$.result.key.task_id') "
                "JOIN plan_revisions p ON p.id = t.plan_revision_id "
                "JOIN projects project ON project.id = h.project_id "
                "JOIN goal_revisions g ON g.id = project.active_goal_revision_id "
                "WHERE h.project_id = ? AND h.event_type = 'operation.completed' "
                "AND json_extract(h.payload_json, '$.kind') = ? "
                "AND json_extract(h.payload_json, '$.result.step') = 'snapshot_c0' "
                "AND json_extract(p.payload_json, '$.definition.goal_contract_digest') = g.definition_digest "
                "ORDER BY h.sequence", (task["project_id"], GATE_KIND)).fetchall()
        results = [json.loads(row["payload_json"])["result"] for row in rows]
        return [item for item in results if item["key"] != key]

    def _engine_output(self, dispatches: list[dict[str, Any]], path: str) -> tuple[bool, str | None]:
        """path를 쓰기 target으로 삼은 마지막 앞 dispatch의 Worker 결과 sha256. 기록이 없으면 None."""
        writer = next((item for item in reversed(dispatches) if path in item["data"].get("writes", ())), None)
        if writer is None:
            return False, None
        with self.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT id FROM attempts WHERE task_id = ? AND kind = 'execution' AND attempt_no = ?",
                (writer["key"]["task_id"], writer["key"]["attempt_no"])).fetchone()
            rows = [] if attempt is None else connection.execute(
                "SELECT observation FROM evidence_records WHERE attempt_id = ? AND source_ref = ? "
                "AND kind IN ('file', 'diff')", (attempt["id"], path)).fetchall()
        for row in rows:
            observed = json.loads(row["observation"])
            if observed.get("access") != "read":
                return True, observed.get("after_digest")
        return True, None

    # ------------------------------------------------------------ gate 단계
    def _open(self, task: Any, key: dict[str, Any], *, replay: bool) -> _Run:
        self.steward.ensure_supported()
        contract, spec, plan, root = self._task_inputs(task)
        envelope = self._envelope(key, contract, spec, plan)
        writes = envelope["scope"]["included"]
        folder = self.state_dir / "tasks" / key["task_id"] / f"attempt-{key['attempt_no']}"
        envelope_path = self._write(folder / "envelope.json", envelope)
        if not replay:
            top = _git(root, "rev-parse", "--show-toplevel", check=False)
            if not top or Path(top).resolve() != root.resolve():
                raise GovernanceRejected(f"GIT_REPOSITORY_REQUIRED: {root}는 git 저장소 루트가 아닙니다.")
            dispatches = self._goal_dispatches(task, key)
            overlaps = user_change_overlap(root, writes, dispatches[0]["data"] if dispatches else None,
                                           lambda path: self._engine_output(dispatches, path))
            if overlaps:
                raise GovernanceRejected(
                    "GOVERNANCE_USER_CHANGE_OVERLAP: 쓰기 target에 이 Goal의 Attempt가 만들지 않은 미커밋 변경이 "
                    f"있습니다: {', '.join(overlaps)}")
        ref = f"refs/flowmarshal/governance/{task['project_id']}/{key['task_id']}/{key['attempt_no']}"
        excluded = (self.service.ledger.artifact_root, self.state_dir)
        c0 = self._op(task, key, "snapshot_c0", {"ref": f"{ref}/c0"}, lambda: {**snapshot_worktree(
            root, ref=f"{ref}/c0", message=f"flowmarshal governance C0 {json.dumps(key, sort_keys=True)}",
            excluded=excluded), "writes": writes}, replay=replay)
        run = _Run(key=key, root=root, folder=folder, envelope=envelope, c0=c0)
        observation = self._review(task, run, "bootstrap", None, {
            "question": "이 TaskEnvelope가 FlowMarshal Task 계약의 목표·쓰기 범위·수용 기준을 빠짐없이 옮겼는가?",
            "taskEnvelope": envelope, "task": contract.model_dump(mode="json"),
            "resolvedTargets": [item.model_dump(mode="json") for item in spec.definition.resolved_targets]},
            replay=replay)
        workflow = self._mcp(task, run, "plan", "plan_workflow", {"schemaVersion": "1.0.0", "taskEnvelope": envelope},
                             observation, replay=replay)
        stages = {stage["requiredCapability"]: stage for stage in workflow["stages"]}
        unsupported = sorted(set(stages) - SUPPORTED_CAPABILITIES)
        if workflow["executionMode"] != "orchestrated" or unsupported:
            raise GovernanceRejected(f"UNSUPPORTED_PLAN: mode={workflow['executionMode']} stages={unsupported}")
        run.stages = {"baseline": stages["change-scope-baseline-capture"],
                      "implementation": stages["minimal-implementation"],
                      "scope": stages["change-scope-assurance"],
                      "acceptance": stages["acceptance-evidence-validation"]}
        frame = {"schemaVersion": "1.0.0",
                 "workspace": {"workspaceId": f"flowmarshal-{task['project_id']}", "locator": str(root)},
                 "controlArtifacts": [{"artifactId": "task-envelope", "role": "pass-condition",
                                       "locator": str(envelope_path), "digest": f"sha256:{_sha256_file(envelope_path)}"}],
                 "targetArtifacts": [{"artifactId": "snapshot-c0", "role": "target",
                                      "locator": f"{root} {c0['commit']}",
                                      "digest": sha256_digest(c0["commit"])}],
                 "operationalSettings": {"maxAttemptsPerEpoch": 3, "maxEpochs": 2, "leaseTtlSeconds": 86400}}
        root_record = self._mcp(task, run, "root", "open_convergence_root", {
            "schemaVersion": "1.0.0", "parentRootId": None, "taskEnvelope": envelope, "frame": frame,
            "userApprovalRefs": [], "responseMode": "compact"}, None, replay=replay)
        lease = self._mcp(task, run, "claim", "claim_workflow_attempt", {
            "schemaVersion": "1.0.0", "rootId": root_record["rootId"], "expectedRevision": root_record["revision"],
            "plan": workflow, "actorId": self.ENGINE_ACTOR, "outputTargets": [str(root)], "priorFailure": None},
            None, replay=replay)
        started = self._mcp(task, run, "start", "start_guarded_workflow", {
            "schemaVersion": "1.0.0", "leaseId": lease["leaseId"], "expectedRootRevision": lease["rootRevision"],
            "responseMode": "compact"}, None, replay=replay)
        run.run_id, run.revision = started["runId"], started["revision"]
        try:
            run.baseline, baseline_path = self._script(task, run, "baseline", f"{SCOPE_SCRIPTS}/capture-workspace-baseline.mjs", {
                "schemaVersion": "1.0.0", "repositoryRoot": str(root), "mode": "capture",
                "comparisonTarget": "commit", "commit": c0["commit"], "taskEnvelope": envelope}, replay=replay)
            run.target_digest = run.baseline["manifestSha256"]
            observation = self._review(task, run, "baseline", run.stages["baseline"].get("executionRequirement"), {
                "question": ("이 기준선이 Worker 실행 직전 작업 트리의 스냅샷 commit이고, 쓰기 target에 이 Goal의 "
                             "Attempt가 만들지 않은 사용자 변경이 없다는 결정적 검사를 통과했는가?"),
                "snapshotCommit": c0["commit"], "head": c0["head"], "entries": len(run.baseline["entries"]),
                "userChangeOverlapCheck": "passed", "writeTargets": writes}, replay=replay)
            self._record(task, run, "baseline", "passed", observation, output_file=baseline_path,
                         artifacts=(("workspace-baseline", "WorkspaceBaseline.v1", ""),),
                         note=f"capture-workspace-baseline commit {c0['commit']}, manifestSha256 "
                              f"{run.baseline['manifestSha256']}", replay=replay)
        except GovernanceRejected:
            if not replay:
                self._abort(task, run)
            raise
        return run

    def _close(self, task: Any, key: dict[str, Any]) -> None:
        run = self._open(task, key, replay=True)
        try:
            self._finish(task, run)
        except GovernanceRejected:
            self._abort(task, run)
            raise

    def _finish(self, task: Any, run: _Run) -> None:
        with self.service.ledger.read() as connection:
            attempt = connection.execute("SELECT id, binding_json FROM attempts WHERE task_id = ? AND kind = 'execution' "
                                         "AND attempt_no = ?", (task["id"], run.key["attempt_no"])).fetchone()
        if attempt is None or not attempt["binding_json"]:
            raise GovernanceRejected(f"WORKER_ATTEMPT_MISSING: attempt {run.key['attempt_no']}")
        ref = f"refs/flowmarshal/governance/{task['project_id']}/{run.key['task_id']}/{run.key['attempt_no']}/c1"
        c1 = self._op(task, run, "snapshot_c1", {"ref": ref, "c0": run.c0["commit"]}, lambda: snapshot_worktree(
            run.root, ref=ref, message=f"flowmarshal governance C1 {json.dumps(run.key, sort_keys=True)}",
            excluded=(self.service.ledger.artifact_root, self.state_dir)), replay=False)
        worker = self.observe_worker(attempt["id"])
        if worker is None:
            raise GovernanceRejected(f"WORKER_OBSERVATION_UNAVAILABLE: {attempt['id']}")
        implementation = self._write(run.folder / "implementation.json", {
            "summary": "FlowMarshal Worker Attempt가 Task를 구현했다.", "attemptId": attempt["id"],
            "threadId": json.loads(attempt["binding_json"]).get("thread_id"), "observation": worker})
        self._record(task, run, "implementation", "passed", worker, output_file=implementation,
                     note=f"Worker attempt {attempt['id']} observed via {worker['source']}", replay=False)

        report, report_path = self._script(task, run, "scope", f"{SCOPE_SCRIPTS}/compare-change-scope.mjs", {
            "schemaVersion": "1.0.0", "repositoryRoot": str(run.root), "mode": "verify", "comparisonTarget": "commit",
            "commit": c1["commit"], "taskEnvelope": run.envelope, "baseline": run.baseline,
            "baselineArtifactDigest": run.baseline["manifestSha256"]}, replay=False)
        run.target_digest = report["currentDigest"]
        scope_state = SCOPE_STATES[report["verdict"]]
        observation = self._review(task, run, "scope", run.stages["scope"].get("executionRequirement"), {
            "question": "스냅샷 C0→C1 변경 목록과 판정이 Task의 쓰기 범위를 정확히 반영하는가?",
            "verdict": report["verdict"], "summary": report["summary"], "findings": report["findings"],
            "changes": report.get("changes", [])}, replay=False)
        self._record(task, run, "scope", scope_state, observation, output_file=report_path, artifacts=(
            ("change-scope-report", "ChangeScopeReport.v1", ""),
            ("scoped-change-inventory", "ChangeScopeReport.v1#/changes", "#/changes"),
            ("ownership-collision-ledger", "ChangeScopeReport.v1#/summary", "#/summary")),
            note=f"compare-change-scope commit {run.c0['commit']}..{c1['commit']} verdict {report['verdict']} "
                 f"findings {report['findings']}", replay=False)
        if scope_state != "passed":
            raise GovernanceRejected(f"CHANGE_SCOPE_{report['verdict']}: {report['findings']}")

        request = self._acceptance_request(task, run, c1["commit"])
        acceptance, acceptance_path = self._script(task, run, "acceptance", ACCEPTANCE_CLI, request, replay=False)
        acceptance_state = ACCEPTANCE_STATES[acceptance["verdict"]]
        observation = self._review(task, run, "acceptance", run.stages["acceptance"].get("executionRequirement"), {
            "question": "각 수용 기준의 판정이 연결된 FlowMarshal 검증 근거와 맞는가?",
            "verdict": acceptance["verdict"], "criteria": acceptance.get("criteria"),
            "evidence": request["evidence"]}, replay=False)
        run.target_digest = request["target"]["digest"]
        self._record(task, run, "acceptance", acceptance_state, observation, output_file=acceptance_path, artifacts=(
            ("acceptance-evidence-report", "AcceptanceEvidenceReport.v1", ""),
            ("verified-evidence-index", "AcceptanceEvidenceReport.v1#/verifiedEvidenceIndex", "#/verifiedEvidenceIndex")),
            note=f"acceptance cli verdict {acceptance['verdict']}", replay=False)
        if acceptance_state != "passed":
            raise GovernanceRejected(f"ACCEPTANCE_{acceptance['verdict']}")
        final = self._mcp(task, run, "finalize", "finalize_workflow",
                          {"runId": run.run_id, "expectedRevision": run.revision, "responseMode": "compact"}, None,
                          replay=False)
        if final["state"] != "passed":
            raise GovernanceRejected(f"GOVERNANCE_NOT_PASSED: {final['state']}")

    def _acceptance_request(self, task: Any, run: _Run, c1: str) -> dict[str, Any]:
        contract = TaskContract.model_validate_json(task["payload_json"])
        target = sha256_digest(c1)
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
            path = self._write(run.folder / f"evidence-{row['validation_id']}.json", {
                "validationResult": payload,
                "evidence": [evidence_rows[item] for item in payload.get("evidence_ids", []) if item in evidence_rows]})
            evidence.append({"id": f"fm-{row['validation_id']}", "kind": "test", "locator": str(path),
                             "digest": f"sha256:{_sha256_file(path)}", "targetDigest": target, "verified": True,
                             "criterionIds": criteria,
                             "direction": "supports" if row["status"] == "pass" else "refutes",
                             "observedResult": f"FlowMarshal {row['validation_id']} {row['status']}: {payload.get('rationale')}"})
        return {"schemaVersion": "1.0.0", "taskEnvelope": run.envelope,
                "target": {"kind": "commit", "identifier": c1, "digest": target},
                "evidence": evidence, "verificationCommands": [],
                "knownLimitations": ["Task 수용 기준(자유 문장)은 FlowMarshal Task validation 전체 PASS에 대응시켰다."],
                "criterionOverrides": []}
