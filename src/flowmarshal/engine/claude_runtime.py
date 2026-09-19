"""Claude Code CLI를 Engine runtime port 뒤에 둔 local provider adapter.

Codex App Server adapter와 같은 port 계약(thread 생성·turn 시작·관측·저장 관측·
resume·interrupt)을 구현한다. provider 실행은 `claude -p`의 stream-json 입출력으로
하며 한 thread는 한 CLI 프로세스를 유지한다.

provenance 원칙:

- Claude Code에는 `model/list`가 없다. inventory는 호출자가 주입한 모델 카탈로그와
  실제 executable digest·CLI 버전으로 만들고, 출처를 `configured_catalog`로 남긴다.
  카탈로그는 typed 변환 전에 원문 JSON을 검사하고 원문 digest를 보존한다.
- executable은 `.cmd`/`.bat`/`.ps1` shim을 거부한다. 프로세스를 띄우기 직전 digest를,
  매 turn init에서 `claude_code_version`을 잠금 값과 다시 대조한다.
- 자식 세션은 `--safe-mode`로 CLAUDE.md·skills·plugins·hooks·MCP·auto-memory 상속을 끈다.
  프로젝트 지침은 Engine Context Pack과 역할 지침으로 직접 공급한다.
- 실행 정책은 매 turn의 `system/init.permissionMode`가 `bypassPermissions`인지 직접
  관측해 확인한다. Engine 공통 정책 식별자(`:danger-full-access`/`never`)는 이
  관측에서 로컬로 도출한 대응값(`local_derived`)이며 provider 관측값이 아니다.
- turn ID는 provider가 되돌려준 user 메시지 uuid(`--replay-user-messages`)다. replay가
  없으면 다른 값으로 대체하지 않고 turn 시작을 미확인으로 멈춘다.
- `result.permission_denials`가 비어 있지 않은 turn은 `success`여도 실패다.
- stream 응답에는 effort가 없다. terminal turn의 model/effort 관측값은 CLI가 저장한
  session 기록에서 그 turn의 assistant 줄이 명시한 값이 정확히 한 쌍일 때만 출처
  `claude_session_transcript`로 싣는다. 기록이 없거나 쌍이 없거나 둘 이상이면 null과
  이유를 남긴다. start_turn receipt의 요청값은 관측값이 아니다. 응답 모델 목록은 진단
  필드 `provider_reported_models`로 따로 보존한다.
- usage는 `result.usage`의 제공 구성요소만 Engine usage 키로 투영하며 total은
  만들지 않는다. turn 범위는 성공 result에 API 호출 기록(`iterations`)이 있을 때만
  인정하고, 그 밖(interrupt 등으로 0이 채워진 값 포함)은 unknown으로 둔다.
  `modelUsage`는 session 누적이라 원문으로만 보존한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from pydantic import Field, PrivateAttr, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .domain import ThreadBinding, utc_now
from .model_lock import (
    ALL_CAPABILITIES,
    LockModel,
    ModelCapability,
    ModelInventory,
    RuntimeCapability,
)
from .model_observation import CLAUDE_SESSION_TRANSCRIPT_MODEL_OBSERVATION_SOURCE
from .operation_trace import current_operation_trace_scope
from .runtime import (
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    ExecutionPolicyEvidence,
    RuntimeObservation,
    RuntimeOperationReceipt,
    RuntimePolicyError,
    _same_path,
)
from .runtime_observation import bounded_observation_call


CLAUDE_PROVIDER = "claude-code"
CLAUDE_INVENTORY_SOURCE_PREFIX = "claude-code:configured-catalog@"
CLAUDE_MODEL_CATALOG_FORMAT = "flowmarshal-claude-model-catalog-v1"
CLAUDE_PERMISSION_MODE = "bypassPermissions"
CLAUDE_PERMISSION_PROMPTS = "none"
CLAUDE_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_USAGE_PROJECTION = "claude-code-result-usage-v2"
CLAUDE_STREAM_CONTRACT = "claude-code-cli-stream-json-v1"
# cmd.exe를 거치는 shim은 8,191자 명령줄 한도와 인자 재해석 때문에 쓰지 않는다.
CLAUDE_SHIM_SUFFIXES = frozenset({".cmd", ".bat", ".ps1"})
POLICY_VALUE_PROVENANCE = "local_derived"


def claude_runtime_capabilities(cli_version: str) -> tuple[RuntimeCapability, ...]:
    """실행 잠금에 투영하는 capability. CLI 버전이 바뀌면 잠금도 바뀐다."""
    return tuple(
        RuntimeCapability(
            name=name,
            contract=(
                f"{CLAUDE_PERMISSION_MODE}/permission-prompts:{CLAUDE_PERMISSION_PROMPTS}/safe-mode"
                if name == "local_execution"
                else f"{CLAUDE_STREAM_CONTRACT}@{cli_version}"
            ),
        )
        for name in ALL_CAPABILITIES
    )
# Windows CreateProcess 명령줄 한도(32,767자)보다 여유를 둔다. 초과하면 호출하지 않는다.
MAX_COMMAND_LINE_CHARS = 30_000
INTERRUPTED_MARKER = "[Request interrupted by user]"
PERMISSION_DENIED_PREFIX = "Permission for this tool use was denied"
_STDERR_TAIL_LINES = 40


class ClaudeModelCatalog(LockModel):
    """Claude provider가 받을 수 있는 model/effort 조합을 호출자가 명시한 카탈로그."""

    format: Literal["flowmarshal-claude-model-catalog-v1"]
    models: tuple[ModelCapability, ...] = Field(min_length=1)
    # `load`로 읽은 원문 bytes의 digest. typed 값에서 다시 만들 수 없는 감사 근거다.
    _source_sha256: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def claude_effort_values(self):
        names = [item.model for item in self.models]
        if len(names) != len(set(names)):
            raise ValueError("Claude model catalog에 모델이 중복됐습니다.")
        for item in self.models:
            unsupported = set(item.supported_efforts) - CLAUDE_SUPPORTED_EFFORTS
            if unsupported:
                raise ValueError(
                    "Claude CLI가 받지 않는 effort가 카탈로그에 있습니다: "
                    + ", ".join(sorted(unsupported))
                )
        return self

    @classmethod
    def load(cls, path: Path | str) -> "ClaudeModelCatalog":
        data = Path(path).read_bytes()
        catalog = cls.model_validate(check_raw_catalog(data))
        catalog._source_sha256 = sha256_bytes(data)
        return catalog

    @property
    def source_sha256(self) -> str | None:
        return self._source_sha256

    @property
    def catalog_digest(self) -> str:
        return sha256_digest(self)

    def raw_response(self) -> dict[str, Any]:
        """`parse_inventory_models`가 읽는 inventory 원문 형태로 카탈로그를 투영한다."""
        return {
            "data": [
                {
                    "id": item.model,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": effort} for effort in item.supported_efforts
                    ],
                }
                for item in self.models
            ],
            "nextCursor": None,
            "inventorySource": "configured_catalog",
            "catalogFormat": self.format,
            "catalogDigest": self.catalog_digest,
            "catalogSourceSha256": self.source_sha256,
        }


def check_raw_catalog(data: bytes) -> dict[str, Any]:
    """typed 변환 전에 카탈로그 원문을 검사한다. 정규화·중복 제거·생략 없이 거부한다."""

    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: JSON key가 중복됐습니다.")
        return dict(pairs)

    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except UnicodeDecodeError as error:
        raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: UTF-8 JSON이 아닙니다.") from error
    if not isinstance(document, dict) or set(document) != {"format", "models"}:
        raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: 최상위는 format·models만 가진 object여야 합니다.")
    rows = document["models"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: models는 비어 있지 않은 배열이어야 합니다.")
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"model", "supported_efforts"}:
            raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: model 행은 model·supported_efforts만 가집니다.")
        model, efforts = row["model"], row["supported_efforts"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: 빈/null model이 있습니다.")
        if not isinstance(efforts, list) or not efforts or any(
            not isinstance(effort, str) or not effort.strip() for effort in efforts
        ):
            raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: 빈/null effort가 있습니다.")
        if len(efforts) != len(set(efforts)):
            raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: effort가 중복됐습니다.")
        names.append(model)
    if len(names) != len(set(names)):
        raise ValueError("CLAUDE_MODEL_CATALOG_INVALID: model이 중복됐습니다.")
    return document


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def project_claude_usage(usage: Any) -> dict[str, int] | None:
    """`result.usage`의 제공 구성요소만 Engine provider usage 키로 옮긴다.

    Claude의 `input_tokens`는 cache 입력을 제외하므로 inputTokens는 세 입력 구성요소의
    합, cachedInputTokens는 cache read다. reasoning은 제공된 thinking token만 쓴다.
    totalTokens는 provider가 주지 않으므로 만들지 않는다.
    """
    if not isinstance(usage, dict):
        return None
    projected: dict[str, int] = {}

    def count(value: Any) -> int | None:
        return value if type(value) is int and value >= 0 else None

    uncached = count(usage.get("input_tokens"))
    cache_creation = count(usage.get("cache_creation_input_tokens"))
    cache_read = count(usage.get("cache_read_input_tokens"))
    if uncached is not None and cache_creation is not None and cache_read is not None:
        projected["inputTokens"] = uncached + cache_creation + cache_read
        projected["cachedInputTokens"] = cache_read
    output = count(usage.get("output_tokens"))
    if output is not None:
        projected["outputTokens"] = output
        details = usage.get("output_tokens_details")
        thinking = count(details.get("thinking_tokens")) if isinstance(details, dict) else None
        if thinking is not None and thinking <= output:
            projected["reasoningOutputTokens"] = thinking
    return projected


def claude_turn_usage(result: dict[str, Any] | None) -> tuple[dict[str, int] | None, str, str]:
    """(usage, usage_scope, 판단 근거)를 돌려준다. turn 범위를 확인할 수 없으면 usage는 None이다.

    실측(2.1.277): 성공 result의 `usage`는 turn마다 따로 나오고 `iterations`에 API 호출이
    기록된다. interrupt로 끝난 result는 모든 값이 0이고 `iterations`가 비어 있다.
    """
    if result is None:
        return None, "unavailable", "result_unobserved"
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None, "unavailable", "result_usage_missing"
    if result.get("subtype") != "success" or result.get("is_error") is True:
        return None, "unavailable", "non_success_result_usage_unverified"
    iterations = usage.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        return None, "unavailable", "result_usage_without_iterations"
    projected = project_claude_usage(usage)
    if not projected:
        return None, "unavailable", "result_usage_components_missing"
    return projected, "turn", "success_result_usage_with_iterations"


@dataclass
class _ClaudeTurn:
    prompt: str
    prompt_digest: str
    model: str
    effort: str
    sent_at: str
    turn_id: str | None = None
    init_event: dict[str, Any] | None = None
    replay_event: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    assistant_models: list[str] = field(default_factory=list)
    event_count: int = 0
    first_event_at: str | None = None
    last_event_at: str | None = None
    last_rate_limit_event: dict[str, Any] | None = None
    policy_violation: str | None = None
    collector_error: str | None = None
    interrupt_requested: bool = False
    first_empty_thread: bool = False
    replayed: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    observer: Callable[[RuntimeObservation], None] | None = None
    observer_error: str | None = None
    transcript_observation: dict[str, Any] | None = None


@dataclass
class _ClaudeThread:
    thread_id: str
    cwd: Path
    model: str
    developer_instructions_path: Path
    developer_instructions_digest: str
    ephemeral: bool
    resumed: bool = False
    process: subprocess.Popen | None = None
    process_key: tuple[str, str, str | None] | None = None
    started_turns: int = 0
    current: _ClaudeTurn | None = None
    turns: list[_ClaudeTurn] = field(default_factory=list)
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES))
    protocol_errors: list[str] = field(default_factory=list)
    control_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    control_waiters: dict[str, threading.Event] = field(default_factory=dict)
    reader: threading.Thread | None = None
    exit_code: int | None = None


class ClaudeCodeRuntime:
    """Claude Code CLI(`claude -p`, stream-json) runtime adapter.

    Codex adapter처럼 파일·네트워크를 별도로 축소하지 않는다. 대신 매 turn의
    provider init 이벤트가 `bypassPermissions`인지 확인하고 다르면 turn을 중단한다.
    """

    provider = CLAUDE_PROVIDER
    requires_budget_policy = False
    emits_rpc_operation_trace = False

    def __init__(
        self,
        *,
        model_catalog: ClaudeModelCatalog,
        claude_bin: Path | str | None = None,
        interpreter: Path | str | None = None,
        state_root: Path | str | None = None,
        setting_sources: str = "",
        config_dir: Path | str | None = None,
        turn_start_timeout_seconds: float = 300.0,
        env: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(model_catalog, ClaudeModelCatalog):
            raise RuntimePolicyError("CLAUDE_MODEL_CATALOG_REQUIRED: 모델 카탈로그를 명시해야 합니다.")
        if claude_bin is None:
            located = shutil.which("claude")
            if located is None:
                raise RuntimePolicyError("CLAUDE_EXECUTABLE_NOT_FOUND: claude 실행 파일이 없습니다.")
            claude_bin = located
        resolved = Path(claude_bin).resolve(strict=True)
        if resolved.suffix.lower() in CLAUDE_SHIM_SUFFIXES:
            raise RuntimePolicyError(
                "CLAUDE_EXECUTABLE_SHIM_UNSUPPORTED: "
                f"{resolved.name}는 shell shim입니다. 실제 claude 실행 파일을 지정해야 합니다."
            )
        self.executable_digest = _file_digest(resolved)
        self._claude_bin = resolved
        self._launcher: tuple[str, ...] = (
            (str(Path(interpreter).resolve(strict=True)), str(resolved))
            if interpreter is not None
            else (str(resolved),)
        )
        self._env = env
        self.cli_version = self._probe_cli_version()
        self.model_catalog = model_catalog
        self._setting_sources = setting_sources
        self._config_dir = (
            Path(config_dir)
            if config_dir is not None
            else Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        )
        self._turn_start_timeout_seconds = turn_start_timeout_seconds
        self._owned_state_root: Path | None = None
        if state_root is None:
            self._owned_state_root = Path(tempfile.mkdtemp(prefix="flowmarshal-claude-"))
            self._state_root = self._owned_state_root
        else:
            self._state_root = Path(state_root).resolve()
            self._state_root.mkdir(parents=True, exist_ok=True)
        self._threads: dict[str, _ClaudeThread] = {}
        self._interrupted_turn_ids: set[str] = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 속성
    @property
    def project_binding(self) -> None:
        return None

    @property
    def claude_executable(self) -> Path:
        return self._claude_bin

    def __enter__(self) -> "ClaudeCodeRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------ executable 잠금
    def _probe_cli_version(self) -> str:
        """`claude --version` 첫 토큰을 잠금 값으로 쓴다. 모델 호출은 없다."""
        try:
            completed = subprocess.run(
                [*self._launcher, "--version"], capture_output=True, timeout=60, env=self._env,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimePolicyError(f"CLAUDE_CLI_VERSION_UNAVAILABLE: {error}") from error
        words = completed.stdout.decode("utf-8", errors="replace").split()
        if completed.returncode != 0 or not words:
            raise RuntimePolicyError("CLAUDE_CLI_VERSION_UNAVAILABLE: claude --version 출력이 없습니다.")
        return words[0]

    def _verify_executable(self) -> None:
        """프로세스를 띄우기 직전 잠금한 executable bytes와 같은지 다시 확인한다."""
        try:
            current = _file_digest(self._claude_bin)
        except OSError as error:
            raise RuntimePolicyError(f"CLAUDE_EXECUTABLE_CHANGED: {error}") from error
        if current != self.executable_digest:
            raise RuntimePolicyError(
                "CLAUDE_EXECUTABLE_CHANGED: claude 실행 파일이 잠금 뒤 바뀌었습니다. 새 binding이 필요합니다."
            )

    # ------------------------------------------------------------ 정책·inventory
    def _policy_document(self) -> dict[str, Any]:
        return {
            "provider": CLAUDE_PROVIDER,
            "permission_mode": CLAUDE_PERMISSION_MODE,
            "permission_prompts": CLAUDE_PERMISSION_PROMPTS,
            "safe_mode": True,
            "setting_sources": self._setting_sources,
            "strict_mcp_config": True,
            "executable_digest": self.executable_digest,
            "cli_version": self.cli_version,
            "engine_policy": {
                "permission_profile": REQUIRED_PERMISSION_PROFILE,
                "approval_policy": REQUIRED_APPROVAL_POLICY,
                "provenance": POLICY_VALUE_PROVENANCE,
            },
            "turn_policy_observation": "system/init.permissionMode",
        }

    def verify_execution_policy(self, cwd: Path | str) -> ExecutionPolicyEvidence:
        workspace = Path(cwd).resolve(strict=True)
        if not workspace.is_dir():
            raise RuntimePolicyError("PERMISSION_POLICY_MISMATCH: cwd가 디렉터리가 아닙니다.")
        return ExecutionPolicyEvidence(
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            config_digest=sha256_digest(self._policy_document()),
            profile_catalog_digest=sha256_digest([CLAUDE_PERMISSION_MODE]),
            cwd=str(workspace),
        )

    def list_models(self) -> ModelInventory:
        self._verify_executable()
        try:
            raw = {**self.model_catalog.raw_response(), "cliVersion": self.cli_version}
            return ModelInventory(
                source=CLAUDE_INVENTORY_SOURCE_PREFIX + self.executable_digest,
                models=self.model_catalog.models,
                raw_response=raw,
                executable_digest=self.executable_digest,
                runtime_capabilities=claude_runtime_capabilities(self.cli_version),
            )
        except ValueError as error:
            raise RuntimePolicyError(f"INVALID_MODEL_INVENTORY: {error}") from error

    # ------------------------------------------------------------ thread 상태
    def _thread_state_path(self, thread_id: str) -> Path:
        return self._state_root / thread_id / "thread.json"

    def create_thread(
        self,
        *,
        cwd: Path,
        title: str,
        model: str,
        developer_instructions: str,
        ephemeral: bool = False,
    ) -> RuntimeOperationReceipt:
        del title
        workspace = Path(cwd).resolve(strict=True)
        self.verify_execution_policy(workspace)
        if not any(item.model == model for item in self.model_catalog.models):
            raise RuntimePolicyError("MODEL_BINDING_UNAVAILABLE: Claude 카탈로그에 없는 모델입니다.")
        thread_id = str(uuid.uuid4())
        thread_dir = self._state_root / thread_id
        thread_dir.mkdir(parents=True, exist_ok=False)
        instructions_path = thread_dir / "developer-instructions.txt"
        instructions_path.write_bytes(developer_instructions.encode("utf-8"))
        instructions_digest = sha256_digest(developer_instructions)
        state = {
            "format": "flowmarshal-claude-thread-v1",
            "thread_id": thread_id,
            "cwd": str(workspace),
            "model": model,
            "ephemeral": ephemeral,
            "developer_instructions_path": str(instructions_path),
            "developer_instructions_digest": instructions_digest,
            "created_at": utc_now().isoformat(),
        }
        self._thread_state_path(thread_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with self._lock:
            self._threads[thread_id] = _ClaudeThread(
                thread_id=thread_id,
                cwd=workspace,
                model=model,
                developer_instructions_path=instructions_path,
                developer_instructions_digest=instructions_digest,
                ephemeral=ephemeral,
            )
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload={
                "provider": CLAUDE_PROVIDER,
                "thread": {
                    "id": thread_id,
                    "turns": [],
                    "ephemeral": ephemeral,
                    "cwd": str(workspace),
                },
                "model": model,
                "cwd": str(workspace),
                "developer_instructions_digest": instructions_digest,
                "session_persistence": not ephemeral,
                "thread_id_provenance": "client_issued_session_uuid",
                "provider_session_creation": "first_turn_with_session_id",
            },
            binding=ThreadBinding(thread_id=thread_id, bound_at=utc_now()),
        )

    # ------------------------------------------------------------ 프로세스
    def _argv(
        self,
        thread: _ClaudeThread,
        *,
        model: str,
        effort: str,
        output_schema: dict[str, Any] | None,
        resume: bool,
    ) -> list[str]:
        argv = [
            *self._launcher,
            "-p",
            "--safe-mode",
            *(["--resume", thread.thread_id] if resume else ["--session-id", thread.thread_id]),
            "--model", model,
            "--effort", effort,
            "--permission-mode", CLAUDE_PERMISSION_MODE,
            "--permission-prompts", CLAUDE_PERMISSION_PROMPTS,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--replay-user-messages",
            "--verbose",
            "--setting-sources", self._setting_sources,
            "--strict-mcp-config",
            "--append-system-prompt-file", str(thread.developer_instructions_path),
        ]
        if output_schema is not None:
            argv += [
                "--json-schema",
                json.dumps(output_schema, ensure_ascii=False, separators=(",", ":")),
            ]
        if thread.ephemeral:
            argv.append("--no-session-persistence")
        command_line = subprocess.list2cmdline(argv)
        if len(command_line) > MAX_COMMAND_LINE_CHARS:
            raise RuntimePolicyError(
                "CLAUDE_COMMAND_LINE_TOO_LONG: 명령줄이 "
                f"{len(command_line)}자로 한도 {MAX_COMMAND_LINE_CHARS}자를 넘습니다."
            )
        return argv

    def _spawn(self, thread: _ClaudeThread, argv: list[str]) -> None:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(thread.cwd),
            env=self._env,
        )
        thread.process = process
        thread.exit_code = None
        reader = threading.Thread(
            target=self._read_stdout, args=(thread, process),
            name=f"flowmarshal-claude-{thread.thread_id}", daemon=True,
        )
        thread.reader = reader
        reader.start()
        threading.Thread(
            target=self._read_stderr, args=(thread, process),
            name=f"flowmarshal-claude-stderr-{thread.thread_id}", daemon=True,
        ).start()

    @staticmethod
    def _read_stderr(thread: _ClaudeThread, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        try:
            for raw in process.stderr:
                thread.stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
        finally:
            process.stderr.close()

    def _read_stdout(self, thread: _ClaudeThread, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        try:
            for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    with self._lock:
                        thread.protocol_errors.append(line[:500])
                    continue
                if isinstance(event, dict):
                    self._dispatch(thread, process, event)
        finally:
            process.stdout.close()
            exit_code = process.wait()
            finished: _ClaudeTurn | None = None
            with self._lock:
                if thread.process is process:
                    thread.exit_code = exit_code
                turn = thread.current
                if turn is not None and not turn.done.is_set():
                    turn.collector_error = (
                        "CLAUDE_COLLECTOR_ENDED_WITHOUT_RESULT: "
                        f"exit={exit_code}"
                    )
                    turn.replayed.set()
                    turn.done.set()
                    finished = turn
                for waiter in thread.control_waiters.values():
                    waiter.set()
            if finished is not None:
                self._notify_observer(thread, finished)

    def _dispatch(self, thread: _ClaudeThread, process: subprocess.Popen, event: dict[str, Any]) -> None:
        kind = event.get("type")
        finished: _ClaudeTurn | None = None
        transcript = None
        if kind == "result":
            # done 전에 읽어 둔다. 완료 대기·관측자 전달이 파일 읽기를 기다리지 않게 한다.
            current = thread.current
            transcript = self._live_transcript_model_observation(
                thread.thread_id, None if current is None else current.turn_id,
            )
        with self._lock:
            if kind == "control_response":
                response = event.get("response")
                request_id = response.get("request_id") if isinstance(response, dict) else None
                if isinstance(request_id, str):
                    thread.control_responses[request_id] = event
                    waiter = thread.control_waiters.get(request_id)
                    if waiter is not None:
                        waiter.set()
                return
            turn = thread.current
            if turn is None or turn.done.is_set():
                thread.protocol_errors.append(f"unbound event: {kind}")
                return
            observed_at = utc_now().isoformat()
            turn.event_count += 1
            turn.first_event_at = turn.first_event_at or observed_at
            turn.last_event_at = observed_at
            if kind == "system" and event.get("subtype") == "init":
                turn.init_event = {
                    key: event.get(key)
                    for key in (
                        "session_id", "model", "permissionMode", "cwd",
                        "claude_code_version", "uuid", "apiKeySource",
                    )
                }
                violation = None
                if event.get("session_id") != thread.thread_id:
                    violation = "THREAD_PROVENANCE_MISMATCH: provider session이 다릅니다."
                elif event.get("permissionMode") != CLAUDE_PERMISSION_MODE:
                    violation = (
                        "PERMISSION_POLICY_MISMATCH: 실제 permissionMode가 "
                        f"{event.get('permissionMode')!r}입니다."
                    )
                elif not isinstance(event.get("cwd"), str) or not _same_path(event["cwd"], thread.cwd):
                    violation = "THREAD_PROVENANCE_MISMATCH: provider cwd가 다릅니다."
                elif event.get("model") != turn.model:
                    violation = (
                        "MODEL_PROVENANCE_MISMATCH: provider model이 "
                        f"{event.get('model')!r}입니다."
                    )
                elif event.get("claude_code_version") != self.cli_version:
                    violation = (
                        "CLAUDE_CLI_VERSION_MISMATCH: provider 버전이 "
                        f"{event.get('claude_code_version')!r}로 잠금 값 {self.cli_version!r}와 다릅니다."
                    )
                if violation is not None:
                    turn.policy_violation = violation
                    turn.replayed.set()
                    self._kill(process)
            elif kind == "user" and event.get("isReplay") is True:
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if turn.turn_id is None and content == turn.prompt and isinstance(event.get("uuid"), str):
                    turn.turn_id = event["uuid"]
                    turn.replay_event = {
                        "uuid": event["uuid"],
                        "session_id": event.get("session_id"),
                        "timestamp": event.get("timestamp"),
                        "content_digest": sha256_digest(content),
                    }
                    turn.replayed.set()
                else:
                    thread.protocol_errors.append("unexpected replayed user message")
            elif kind == "assistant":
                message = event.get("message")
                model = message.get("model") if isinstance(message, dict) else None
                if isinstance(model, str) and model not in turn.assistant_models:
                    turn.assistant_models.append(model)
            elif kind == "rate_limit_event":
                turn.last_rate_limit_event = event
            elif kind == "result":
                turn.result = event
                turn.transcript_observation = transcript
                turn.replayed.set()
                turn.done.set()
                finished = turn
        if finished is not None:
            self._notify_observer(thread, finished)

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            process.kill()
        except OSError:
            pass

    def _write(self, thread: _ClaudeThread, document: dict[str, Any]) -> None:
        process = thread.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimePolicyError("CLAUDE_PROCESS_NOT_RUNNING: provider 프로세스가 없습니다.")
        process.stdin.write((json.dumps(document, ensure_ascii=True) + "\n").encode("utf-8"))
        process.stdin.flush()

    def _stop_process(self, thread: _ClaudeThread, *, timeout_seconds: float) -> None:
        process = thread.process
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=max(0.0, timeout_seconds))
        except subprocess.TimeoutExpired:
            self._kill(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if thread.reader is not None:
            thread.reader.join(timeout=5)

    # ------------------------------------------------------------ turn
    def _start_timeout(self) -> float:
        timeout = self._turn_start_timeout_seconds
        scope = current_operation_trace_scope()
        if scope is not None and scope.deadline_monotonic_ns is not None:
            remaining = (scope.deadline_monotonic_ns - time.monotonic_ns()) / 1_000_000_000
            timeout = min(timeout, max(0.0, remaining))
        return timeout

    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        prompt: str,
        model: str,
        effort: str,
        output_schema: dict[str, Any] | None = None,
    ) -> RuntimeOperationReceipt:
        workspace = Path(cwd).resolve(strict=True)
        self.verify_execution_policy(workspace)
        if effort not in CLAUDE_SUPPORTED_EFFORTS:
            raise RuntimePolicyError(f"MODEL_BINDING_UNAVAILABLE: Claude가 받지 않는 effort입니다: {effort}")
        if not any(
            item.model == model and effort in item.supported_efforts
            for item in self.model_catalog.models
        ):
            raise RuntimePolicyError("MODEL_BINDING_UNAVAILABLE: Claude 카탈로그에 없는 model/effort입니다.")
        with self._lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                raise RuntimePolicyError(
                    "CLAUDE_THREAD_NOT_OPEN: 이 연결에서 만들거나 resume한 thread가 아닙니다."
                )
            if not _same_path(workspace, thread.cwd):
                raise RuntimePolicyError("THREAD_PROVENANCE_MISMATCH: thread cwd가 다릅니다.")
            if thread.current is not None and not thread.current.done.is_set():
                raise RuntimePolicyError("CLAUDE_TURN_ALREADY_ACTIVE: 진행 중인 turn이 있습니다.")
            schema_digest = None if output_schema is None else sha256_digest(output_schema)
            process_key = (model, effort, schema_digest)
            process_alive = thread.process is not None and thread.process.poll() is None
            restart_required = process_alive and thread.process_key != process_key
        if restart_required:
            if thread.ephemeral:
                raise RuntimePolicyError(
                    "CLAUDE_TURN_CONFIGURATION_CHANGE_REQUIRES_PERSISTED_THREAD: "
                    "ephemeral thread의 model/effort/schema는 바꿀 수 없습니다."
                )
            self._stop_process(thread, timeout_seconds=30)
            process_alive = False
        first_empty_thread = thread.started_turns == 0 and not thread.resumed
        if not process_alive:
            if thread.started_turns > 0 and thread.ephemeral:
                raise RuntimePolicyError(
                    "CLAUDE_EPHEMERAL_THREAD_PROCESS_LOST: 저장하지 않은 thread는 다시 열 수 없습니다."
                )
            resume = thread.started_turns > 0 or thread.resumed
            argv = self._argv(
                thread, model=model, effort=effort, output_schema=output_schema, resume=resume,
            )
            self._verify_executable()
        prompt_digest = sha256_digest(prompt)
        turn = _ClaudeTurn(
            prompt=prompt,
            prompt_digest=prompt_digest,
            model=model,
            effort=effort,
            sent_at=utc_now().isoformat(),
            first_empty_thread=first_empty_thread,
        )
        with self._lock:
            thread.current = turn
            thread.turns.append(turn)
            thread.started_turns += 1
            if not process_alive:
                self._spawn(thread, argv)
                thread.process_key = process_key
            self._write(
                thread,
                {"type": "user", "message": {"role": "user", "content": prompt}},
            )
        if not turn.replayed.wait(timeout=self._start_timeout()):
            raise TimeoutError(
                "CLAUDE_TURN_START_UNCONFIRMED: provider가 turn 수신을 확인하지 않았습니다."
            )
        if turn.policy_violation is not None:
            raise RuntimePolicyError(turn.policy_violation)
        # 실측(2.1.277): 매 turn `system/init` → replay user 순서다(첫 turn 포함). replay uuid가
        # 없으면 client uuid 등으로 대체하지 않는다. provider 확인 없는 turn ID는 결속 근거가 아니다.
        if turn.turn_id is None:
            raise RuntimePolicyError(
                "CLAUDE_TURN_START_UNCONFIRMED: "
                + (turn.collector_error or "provider replay가 없습니다.")
            )
        if turn.init_event is None:
            raise RuntimePolicyError("PERMISSION_POLICY_MISMATCH: turn init 관측이 없습니다.")
        return RuntimeOperationReceipt(
            operation_id=turn.turn_id,
            payload={
                "provider": CLAUDE_PROVIDER,
                "thread_id": thread_id,
                "turn_id": turn.turn_id,
                "model": model,
                "effort": effort,
                "requested_model": model,
                "requested_effort": effort,
                "model_provenance": "requested",
                "effort_provenance": "requested",
                "permission_profile": REQUIRED_PERMISSION_PROFILE,
                "approval_policy": REQUIRED_APPROVAL_POLICY,
                "permission_profile_provenance": POLICY_VALUE_PROVENANCE,
                "approval_policy_provenance": POLICY_VALUE_PROVENANCE,
                "provider_permission_mode": turn.init_event.get("permissionMode"),
                "provider_permission_mode_provenance": "provider_observed",
                "provider_cli_version": turn.init_event.get("claude_code_version"),
                "provider_session_init": turn.init_event,
                "provider_replay": turn.replay_event,
                "turn_id_provenance": "provider_replayed_user_message_uuid",
                "prompt_digest": prompt_digest,
                "output_schema_digest": schema_digest,
                "first_empty_thread": first_empty_thread,
            },
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn.turn_id, bound_at=utc_now()),
        )

    # ------------------------------------------------------------ 관측
    @staticmethod
    def _terminal_status(turn: _ClaudeTurn) -> str | None:
        result = turn.result
        if result is None:
            return None
        if result.get("permission_denials"):
            # 실측: 거부된 도구가 있어도 result는 success다. 요청 작업을 다 했다는 근거가 아니다.
            return "failed"
        if result.get("subtype") == "success" and result.get("is_error") is not True:
            return "completed"
        if turn.interrupt_requested:
            return "interrupted"
        return "failed"

    @staticmethod
    def _final_response(turn: _ClaudeTurn) -> str | None:
        result = turn.result
        if result is None:
            return turn.collector_error or turn.policy_violation
        structured = result.get("structured_output")
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)
        text = result.get("result")
        if isinstance(text, str):
            return text
        errors = result.get("errors")
        return None if errors is None else json.dumps(errors, ensure_ascii=False)

    def _observation(self, thread: _ClaudeThread, turn: _ClaudeTurn) -> RuntimeObservation:
        with self._lock:
            active = not turn.done.is_set()
            status = None if active else self._terminal_status(turn)
            result = turn.result
            provider_result = None
            if result is not None:
                provider_result = {key: value for key, value in result.items() if key != "result"}
                if isinstance(result.get("result"), str):
                    provider_result["result_digest"] = sha256_digest(result["result"])
            usage, usage_scope, usage_scope_basis = claude_turn_usage(result)
            denials = None if result is None else result.get("permission_denials")
            reported = list(turn.assistant_models)
            if turn.init_event is not None and isinstance(turn.init_event.get("model"), str):
                if turn.init_event["model"] not in reported:
                    reported.insert(0, turn.init_event["model"])
            payload: dict[str, Any] = {
                "provider": CLAUDE_PROVIDER,
                "thread_id": thread.thread_id,
                "turn_id": turn.turn_id,
                "turn_status": status,
                "prompt_digest": turn.prompt_digest,
                "usage": usage,
                "usage_scope": usage_scope,
                "usage_scope_basis": usage_scope_basis,
                "usage_source": "claude-code:stream-json:result.usage",
                "usage_projection": CLAUDE_USAGE_PROJECTION,
                "provider_usage_raw": None if result is None else result.get("usage"),
                "provider_model_usage": None if result is None else result.get("modelUsage"),
                "provider_model_usage_scope": "session_cumulative",
                "provider_permission_denials": denials,
                "provider_reported_models": reported,
                "provider_session_init": turn.init_event,
                "provider_result": provider_result,
                "interrupt_requested": turn.interrupt_requested,
                "lifecycle": {
                    "observation_started_at": turn.sent_at,
                    "first_event_at": turn.first_event_at,
                    "last_event_at": turn.last_event_at,
                    "event_count": turn.event_count,
                    "provider_duration_ms": None if result is None else result.get("duration_ms"),
                    "terminal_status": status,
                    "terminal_error": (
                        None
                        if status in {None, "completed"}
                        else {
                            "code": "CLAUDE_PERMISSION_DENIED" if denials else None,
                            "subtype": None if result is None else result.get("subtype"),
                            "terminal_reason": None if result is None else result.get("terminal_reason"),
                            "api_error_status": None if result is None else result.get("api_error_status"),
                        }
                    ),
                },
            }
            if turn.last_rate_limit_event is not None:
                payload["provider_rate_limit"] = turn.last_rate_limit_event.get("rate_limit_info")
            if turn.collector_error is not None or turn.policy_violation is not None:
                payload["error"] = turn.policy_violation or turn.collector_error
                payload["exit_code"] = thread.exit_code
                payload["stderr_tail"] = list(thread.stderr_tail)
        if status is not None:
            payload.update(turn.transcript_observation or self._transcript_model_observation(None))
        return RuntimeObservation(
            thread_id=thread.thread_id,
            turn_id=turn.turn_id,
            active=active,
            terminal_status=status,
            final_response=None if active else self._final_response(turn),
            payload=payload,
        )

    def read(self, *, thread_id: str) -> RuntimeObservation:
        with self._lock:
            thread = self._threads.get(thread_id)
            turn = None if thread is None else thread.current
        if thread is None or turn is None:
            return self.read_stored(thread_id=thread_id)
        return self._observation(thread, turn)

    def operation_kind_for_read(self, thread_id: str) -> str:
        with self._lock:
            thread = self._threads.get(thread_id)
            live = thread is not None and thread.current is not None
        return "sdk_wait" if live else "read"

    def register_completion_observer(
        self, *, thread_id: str, turn_id: str, observer: Callable[[RuntimeObservation], None],
    ) -> None:
        """start receipt 기록 뒤 terminal 관측을 영속 관측자에게 한 번 전달한다."""
        with self._lock:
            thread = self._threads.get(thread_id)
            turn = None if thread is None else thread.current
            if thread is None or turn is None or turn.turn_id != turn_id:
                raise RuntimePolicyError("WORKER_USAGE_BINDING_MISMATCH: 완료 handle이 다릅니다.")
            turn.observer = observer
            already_done = turn.done.is_set()
        if already_done:
            self._notify_observer(thread, turn)

    def _notify_observer(self, thread: _ClaudeThread, turn: _ClaudeTurn) -> None:
        with self._lock:
            observer = turn.observer
            turn.observer = None
        if observer is None:
            return
        try:
            observer(self._observation(thread, turn))
        except Exception as error:  # noqa: BLE001 - reader thread를 보존한다.
            turn.observer_error = f"{type(error).__name__}: {error}"

    def wait_for_active_turns(self, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._lock:
            turns = [
                thread.current for thread in self._threads.values()
                if thread.current is not None
            ]
        for turn in turns:
            if not turn.done.wait(timeout=max(0.0, deadline - time.monotonic())):
                return False
        return True

    # ------------------------------------------------------------ 저장 관측
    def _session_transcript(self, thread_id: str) -> Path:
        matches = sorted((self._config_dir / "projects").glob(f"*/{thread_id}.jsonl"))
        if len(matches) != 1:
            raise RuntimePolicyError(
                "CLAUDE_SESSION_TRANSCRIPT_NOT_FOUND: 저장 session 기록을 정확히 하나 찾지 못했습니다."
                if not matches
                else "CLAUDE_SESSION_TRANSCRIPT_AMBIGUOUS: 같은 session 기록이 여러 개입니다."
            )
        return matches[0]

    @staticmethod
    def _parse_transcript(data: bytes) -> list[dict[str, Any]]:
        lines = data.split(b"\n")
        records: list[dict[str, Any]] = []
        for index, raw in enumerate(lines):
            if not raw.strip():
                continue
            try:
                document = json.loads(raw.decode("utf-8"))
            except ValueError as error:
                # 쓰는 중인 마지막 줄만 허용한다. 중간 손상은 숨기지 않는다.
                if index == len(lines) - 1:
                    break
                raise RuntimePolicyError(
                    "CLAUDE_SESSION_TRANSCRIPT_CORRUPT: 저장 session 기록을 읽지 못했습니다."
                ) from error
            if isinstance(document, dict):
                records.append(document)
        return records

    @staticmethod
    def _transcript_model_observation(turn: dict[str, Any] | None) -> dict[str, Any]:
        """저장 turn의 assistant 줄이 명시한 model·effort가 정확히 한 쌍일 때만 관측값으로 싣는다."""
        pairs = {
            # 문자열이 아닌 effort는 명시된 값이 아니므로 없는 것과 같게 둔다.
            (record["message"]["model"], record["effort"] if isinstance(record.get("effort"), str) else None)
            for record in ([] if turn is None else turn["records"])
            if record.get("type") == "assistant"
            and isinstance(record.get("message"), dict)
            and isinstance(record["message"].get("model"), str)
            and record["message"]["model"] not in {"", "<synthetic>"}
        }
        if len(pairs) == 1:
            ((model, effort),) = pairs
            if isinstance(effort, str) and effort:
                return {
                    "observed_model": model,
                    "observed_effort": effort,
                    "model_observation_source": CLAUDE_SESSION_TRANSCRIPT_MODEL_OBSERVATION_SOURCE,
                    "model_observation_reason": None,
                }
        return {
            "observed_model": None,
            "observed_effort": None,
            "model_observation_source": None,
            "model_observation_reason": (
                "CLAUDE_TRANSCRIPT_MODEL_EFFORT_AMBIGUOUS"
                if len(pairs) > 1
                else "CLAUDE_TRANSCRIPT_MODEL_EFFORT_NOT_REPORTED"
            ),
        }

    def _live_transcript_model_observation(self, thread_id: str, turn_id: str | None) -> dict[str, Any]:
        # reader thread의 result 처리 안에서 부른다. 진단용 기록 읽기가 어떤 이유로 실패해도
        # terminal 판정을 막지 않도록 관측값 없이(null과 이유) 돌려준다.
        try:
            records = self._parse_transcript(self._session_transcript(thread_id).read_bytes())
            matches = [item for item in self._stored_turns(records) if item["start"].get("uuid") == turn_id]
            return self._transcript_model_observation(matches[0] if len(matches) == 1 else None)
        except Exception:  # noqa: BLE001 - 비저장 thread(--no-session-persistence), 읽기·해석 실패
            return {
                **self._transcript_model_observation(None),
                "model_observation_reason": "CLAUDE_SESSION_TRANSCRIPT_UNAVAILABLE",
            }

    @staticmethod
    def _stored_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        by_prompt: dict[str, dict[str, Any]] = {}
        for record in records:
            # resume 때 CLI가 넣는 meta 안내와 synthetic 응답은 어느 turn의 관측도 아니다.
            if record.get("isSidechain") is True or record.get("isMeta") is True:
                continue
            message = record.get("message")
            if (
                record.get("type") == "assistant"
                and isinstance(message, dict)
                and message.get("model") == "<synthetic>"
                and record.get("isApiErrorMessage") is not True
            ):
                continue
            kind = record.get("type")
            prompt_id = record.get("promptId")
            if kind == "user" and "promptSource" in record and isinstance(record.get("uuid"), str):
                turn = {"start": record, "records": [], "prompt_id": prompt_id}
                turns.append(turn)
                if isinstance(prompt_id, str):
                    by_prompt[prompt_id] = turn
                continue
            if not turns:
                continue
            owner = by_prompt.get(prompt_id) if isinstance(prompt_id, str) else None
            (owner or turns[-1])["records"].append(record)
        return turns

    @staticmethod
    def _stored_status(turn: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
        status: str | None = None
        final_response: str | None = None
        models: list[str] = []
        structured_tool_ids: set[str] = set()
        denied = False
        for record in turn["records"]:
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if record.get("type") == "assistant" and isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model not in models:
                    models.append(model)
                if record.get("isApiErrorMessage") is True and status is None:
                    status = "failed"
                texts = []
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name") == "StructuredOutput":
                        final_response = json.dumps(block.get("input"), ensure_ascii=False)
                        if isinstance(block.get("id"), str):
                            structured_tool_ids.add(block["id"])
                    elif block.get("type") == "text" and isinstance(block.get("text"), str):
                        texts.append(block["text"])
                if texts and not structured_tool_ids:
                    final_response = "".join(texts)
                if message.get("stop_reason") == "end_turn":
                    status = "completed"
            elif record.get("type") == "user":
                blocks = content if isinstance(content, list) else []
                if any(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    and block.get("is_error") is True
                    and str(block.get("content")).startswith(PERMISSION_DENIED_PREFIX)
                    for block in blocks
                ):
                    # ponytail: 저장 기록에는 구조화 거부 필드가 없어 CLI 문구 접두어로 판정한다.
                    # 문구가 바뀌면 놓칠 수 있으니 live result.permission_denials가 우선 근거다.
                    denied = True
                if any(
                    isinstance(block, dict) and block.get("type") == "text"
                    and block.get("text") == INTERRUPTED_MARKER
                    for block in blocks
                ):
                    status = "interrupted"
                elif record.get("toolEndsTurn") is True and any(
                    isinstance(block, dict) and block.get("tool_use_id") in structured_tool_ids
                    for block in blocks
                ):
                    status = "completed"
        if denied and status == "completed":
            status = "failed"
        return status, final_response, models

    def read_stored(
        self, *, thread_id: str, turn_id: str | None = None, timeout_seconds: float = 5.0,
    ) -> RuntimeObservation:
        """저장 session 기록을 유한 시간에 읽고, 지정한 경우 정확한 turn만 반환한다."""
        return bounded_observation_call(
            lambda: self._read_stored_impl(thread_id=thread_id, turn_id=turn_id),
            timeout_seconds=timeout_seconds,
            operation_name=f"claude.read_stored:{thread_id}:{turn_id or 'latest'}",
        )

    def _read_stored_impl(self, *, thread_id: str, turn_id: str | None) -> RuntimeObservation:
        path = self._session_transcript(thread_id)
        data = path.read_bytes()
        records = self._parse_transcript(data)
        turns = self._stored_turns(records)
        with self._lock:
            known = self._threads.get(thread_id)
        expected_cwd = None if known is None else known.cwd
        if expected_cwd is None:
            state = self._load_thread_state(thread_id, required=False)
            expected_cwd = None if state is None else Path(state["cwd"])
        if turn_id is None:
            selected = turns[-1] if turns else None
        else:
            matches = [item for item in turns if item["start"].get("uuid") == turn_id]
            if len(matches) != 1:
                raise RuntimePolicyError(
                    "RUNTIME_OBSERVATION_BINDING_MISMATCH: 요청한 exact turn을 정확히 찾지 못했습니다."
                )
            selected = matches[0]
        if selected is not None and expected_cwd is not None:
            stored_cwd = selected["start"].get("cwd")
            if not isinstance(stored_cwd, str) or not _same_path(stored_cwd, expected_cwd):
                raise RuntimePolicyError("THREAD_PROVENANCE_MISMATCH: 저장 turn의 cwd가 다릅니다.")
        status, final_response, models = (
            (None, None, []) if selected is None else self._stored_status(selected)
        )
        model_observation = (
            {} if status is None else self._transcript_model_observation(selected)
        )
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=None if selected is None else selected["start"]["uuid"],
            active=False,
            terminal_status=status,
            final_response=final_response,
            payload={
                "provider": CLAUDE_PROVIDER,
                "thread_id": thread_id,
                "requested_turn_id": turn_id,
                "turn_count": len(turns),
                "turn_status": status if status is not None else (
                    None if selected is None else "terminal_unobserved"
                ),
                "provider_prompt_id": None if selected is None else selected.get("prompt_id"),
                "provider_reported_models": models,
                "usage": None,
                "usage_scope": "unavailable",
                "usage_scope_basis": "session_transcript_has_no_turn_usage",
                "usage_source": "claude-code:session-transcript",
                "turn_history_available": True,
                "turn_history_error": None,
                "turn_history_source": "claude-code:session-transcript",
                "session_transcript_path": str(path),
                "session_transcript_digest": sha256_bytes(data),
                "lifecycle": {"terminal_status": status, "terminal_error": None},
                **model_observation,
            },
        )

    # ------------------------------------------------------------ resume·interrupt
    def _load_thread_state(self, thread_id: str, *, required: bool) -> dict[str, Any] | None:
        path = self._thread_state_path(thread_id)
        if not path.is_file():
            if required:
                raise RuntimePolicyError(
                    "CLAUDE_THREAD_STATE_REQUIRED: 같은 state root의 thread 기록이 없습니다."
                )
            return None
        state = json.loads(path.read_text(encoding="utf-8"))
        instructions = Path(state["developer_instructions_path"])
        if (
            state.get("format") != "flowmarshal-claude-thread-v1"
            or state.get("thread_id") != thread_id
            or not instructions.is_file()
            or sha256_digest(instructions.read_bytes().decode("utf-8"))
            != state.get("developer_instructions_digest")
        ):
            raise RuntimePolicyError("CLAUDE_THREAD_STATE_MISMATCH: thread 기록이 손상됐습니다.")
        return state

    def resume(self, *, thread_id: str, cwd: Path) -> RuntimeOperationReceipt:
        workspace = Path(cwd).resolve(strict=True)
        self.verify_execution_policy(workspace)
        with self._lock:
            live = self._threads.get(thread_id)
        if live is not None:
            if not _same_path(workspace, live.cwd):
                raise RuntimePolicyError("THREAD_PROVENANCE_MISMATCH: thread cwd가 다릅니다.")
            return RuntimeOperationReceipt(
                operation_id=thread_id,
                payload={
                    "provider": CLAUDE_PROVIDER, "thread_id": thread_id,
                    "cwd": str(workspace), "resumed": True, "live_connection": True,
                },
                binding=ThreadBinding(thread_id=thread_id, bound_at=utc_now()),
            )
        state = self._load_thread_state(thread_id, required=True)
        assert state is not None
        if state.get("ephemeral") is True:
            raise RuntimePolicyError(
                "CLAUDE_EPHEMERAL_THREAD_PROCESS_LOST: 저장하지 않은 thread는 resume할 수 없습니다."
            )
        if not _same_path(state["cwd"], workspace):
            raise RuntimePolicyError("THREAD_PROVENANCE_MISMATCH: thread cwd가 다릅니다.")
        transcript = self._session_transcript(thread_id)
        data = transcript.read_bytes()
        with self._lock:
            self._threads[thread_id] = _ClaudeThread(
                thread_id=thread_id,
                cwd=workspace,
                model=state["model"],
                developer_instructions_path=Path(state["developer_instructions_path"]),
                developer_instructions_digest=state["developer_instructions_digest"],
                ephemeral=False,
                resumed=True,
            )
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload={
                "provider": CLAUDE_PROVIDER,
                "thread_id": thread_id,
                "cwd": str(workspace),
                "resumed": True,
                "live_connection": False,
                "session_transcript_path": str(transcript),
                "session_transcript_digest": sha256_bytes(data),
                "developer_instructions_digest": state["developer_instructions_digest"],
            },
            binding=ThreadBinding(thread_id=thread_id, bound_at=utc_now()),
        )

    def interrupt(
        self, *, thread_id: str, turn_id: str, timeout_seconds: float = 5.0,
    ) -> RuntimeOperationReceipt:
        with self._lock:
            if turn_id in self._interrupted_turn_ids:
                raise RuntimePolicyError(
                    "RUNTIME_INTERRUPT_ALREADY_REQUESTED: 같은 turn에 interrupt를 다시 보내지 않습니다."
                )
            thread = self._threads.get(thread_id)
            turn = None if thread is None else thread.current
            if thread is None or turn is None or turn.turn_id != turn_id:
                raise RuntimePolicyError(
                    "CLAUDE_INTERRUPT_TARGET_NOT_LIVE: 이 연결에서 실행 중인 turn이 아닙니다."
                )
            if turn.done.is_set():
                raise RuntimePolicyError(
                    "CLAUDE_INTERRUPT_TARGET_NOT_ACTIVE: turn이 이미 종료됐습니다."
                )
            # 전송 전에 표시한다. 대기 만료는 원격 미실행 증명이 아니다.
            self._interrupted_turn_ids.add(turn_id)
            turn.interrupt_requested = True
            request_id = f"flowmarshal-interrupt-{turn_id}"
            waiter = threading.Event()
            thread.control_waiters[request_id] = waiter
            self._write(
                thread,
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": "interrupt"},
                },
            )
        if not waiter.wait(timeout=max(0.0, timeout_seconds)):
            raise TimeoutError(f"turn/interrupt:{turn_id}: provider 응답 대기 기한이 만료됐습니다.")
        with self._lock:
            response = thread.control_responses.get(request_id)
            thread.control_waiters.pop(request_id, None)
        if response is None:
            raise RuntimePolicyError(
                "CLAUDE_INTERRUPT_UNCONFIRMED: provider가 interrupt 응답 없이 종료됐습니다."
            )
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={
                "provider": CLAUDE_PROVIDER,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "interrupted": True,
                "response": response,
            },
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )

    # ------------------------------------------------------------ 종료
    def close(self, *, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            turn = thread.current
            if (
                turn is not None
                and not turn.done.is_set()
                and turn.turn_id is not None
                and turn.turn_id not in self._interrupted_turn_ids
            ):
                try:
                    self.interrupt(
                        thread_id=thread.thread_id, turn_id=turn.turn_id,
                        timeout_seconds=max(0.0, min(5.0, deadline - time.monotonic())),
                    )
                except BaseException:  # noqa: BLE001 - 종료 경로는 계속 진행한다.
                    pass
            if turn is not None:
                turn.done.wait(timeout=max(0.0, deadline - time.monotonic()))
            self._stop_process(thread, timeout_seconds=max(0.0, deadline - time.monotonic()))
        if self._owned_state_root is not None:
            shutil.rmtree(self._owned_state_root, ignore_errors=True)
            self._owned_state_root = None


def open_claude_runtime(
    *,
    model_catalog_path: Path | str,
    claude_bin: Path | str | None = None,
    state_root: Path | str | None = None,
    setting_sources: str = "",
) -> ClaudeCodeRuntime:
    return ClaudeCodeRuntime(
        model_catalog=ClaudeModelCatalog.load(model_catalog_path),
        claude_bin=claude_bin,
        state_root=state_root,
        setting_sources=setting_sources,
    )


__all__: Sequence[str] = (
    "CLAUDE_INVENTORY_SOURCE_PREFIX",
    "CLAUDE_MODEL_CATALOG_FORMAT",
    "CLAUDE_PROVIDER",
    "ClaudeCodeRuntime",
    "ClaudeModelCatalog",
    "check_raw_catalog",
    "claude_runtime_capabilities",
    "claude_turn_usage",
    "open_claude_runtime",
    "project_claude_usage",
)
