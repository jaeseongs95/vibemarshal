"""Gemini CLI를 Engine runtime port 뒤에 둔 local provider adapter.

Codex App Server·Claude Code adapter와 같은 port 계약(thread 생성·turn 시작·관측·저장 관측·
resume·interrupt)을 구현한다. provider 실행은 `gemini -p`의 stream-json 입출력으로
하며 한 thread는 한 CLI 프로세스를 유지한다.

provenance 원칙:

- Gemini CLI에는 `model/list`가 없다. inventory는 호출자가 주입한 모델 카탈로그와
  실제 executable digest·CLI 버전으로 만들고, 출처를 `configured_catalog`로 남긴다.
  카탈로그는 typed 변환 전에 원문 JSON을 검사하고 원문 digest를 보존한다.
- executable은 `.cmd`/`.bat`/`.ps1` shim을 거부한다. 프로세스를 띄우기 직전 digest를,
  매 turn init에서 `gemini_cli_version`을 잠금 값과 다시 대조한다.
- 자식 세션은 `--safe-mode`로 외부 설정·확장 상속을 끈다.
  프로젝트 지침은 Engine Context Pack과 역할 지침으로 직접 공급한다.
- 실행 정책은 매 turn의 `system/init.permissionMode`가 `bypassPermissions`인지 직접
  관측해 확인한다. Engine 공통 정책 식별자(`:danger-full-access`/`never`)는 이
  관측에서 로컬로 도출한 대응값(`local_derived`)이며 provider 관측값이 아니다.
- turn ID는 provider가 되돌려준 user 메시지 uuid(`--replay-user-messages`)다. replay가
  없으면 다른 값으로 대체하지 않고 turn 시작을 미확인으로 멈춘다.
- `result.permission_denials`가 비어 있지 않은 turn은 `success`여도 실패다.
- stream 응답에는 effort가 없다. terminal turn의 model/effort 관측값은 CLI가 저장한
  session 기록에서 그 turn의 assistant 줄이 명시한 값이 정확히 한 쌍일 때만 출처
  `gemini_session_transcript`로 싣는다. 기록이 없거나 쌍이 없거나 둘 이상이면 null과
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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from pydantic import Field, PrivateAttr, model_validator

if __name__ == "__main__" and not __package__:
    _pkg_parent = str(Path(__file__).resolve().parents[2])
    if _pkg_parent not in sys.path:
        sys.path.insert(0, _pkg_parent)
    __package__ = "flowmarshal.engine"

from ..canonical import sha256_bytes, sha256_digest
from .domain import ThreadBinding, utc_now
from .model_lock import (
    ALL_CAPABILITIES,
    LockModel,
    ModelCapability,
    ModelInventory,
    RuntimeCapability,
)
from .model_observation import GEMINI_SESSION_TRANSCRIPT_MODEL_OBSERVATION_SOURCE
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


GEMINI_PROVIDER = "gemini-cli"
GEMINI_INVENTORY_SOURCE_PREFIX = "gemini-cli:configured-catalog@"
GEMINI_MODEL_CATALOG_FORMAT = "flowmarshal-gemini-model-catalog-v1"
GEMINI_PERMISSION_MODE = "bypassPermissions"
GEMINI_PERMISSION_PROMPTS = "none"
GEMINI_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
GEMINI_USAGE_PROJECTION = "gemini-cli-result-usage-v1"
GEMINI_STREAM_CONTRACT = "gemini-cli-stream-json-v1"
# cmd.exe를 거치는 shim은 8,191자 명령줄 한도와 인자 재해석 때문에 쓰지 않는다.
GEMINI_SHIM_SUFFIXES = frozenset({".cmd", ".bat", ".ps1"})
POLICY_VALUE_PROVENANCE = "local_derived"


def gemini_runtime_capabilities(cli_version: str) -> tuple[RuntimeCapability, ...]:
    """실행 잠금에 투영하는 capability. CLI 버전이 바뀌면 잠금도 바뀐다."""
    return tuple(
        RuntimeCapability(
            name=name,
            contract=(
                f"{GEMINI_PERMISSION_MODE}/permission-prompts:{GEMINI_PERMISSION_PROMPTS}/safe-mode"
                if name == "local_execution"
                else f"{GEMINI_STREAM_CONTRACT}@{cli_version}"
            ),
        )
        for name in ALL_CAPABILITIES
    )


# Windows CreateProcess 명령줄 한도(32,767자)보다 여유를 둔다. 초과하면 호출하지 않는다.
MAX_COMMAND_LINE_CHARS = 30_000
INTERRUPTED_MARKER = "[Request interrupted by user]"
PERMISSION_DENIED_PREFIX = "Permission for this tool use was denied"
_STDERR_TAIL_LINES = 40


class GeminiModelCatalog(LockModel):
    """Gemini provider가 받을 수 있는 model/effort 조합을 호출자가 명시한 카탈로그."""

    format: Literal["flowmarshal-gemini-model-catalog-v1"]
    models: tuple[ModelCapability, ...] = Field(min_length=1)
    # `load`로 읽은 원문 bytes의 digest. typed 값에서 다시 만들 수 없는 감사 근거다.
    _source_sha256: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def gemini_effort_values(self):
        names = [item.model for item in self.models]
        if len(names) != len(set(names)):
            raise ValueError("Gemini model catalog에 모델이 중복됐습니다.")
        for item in self.models:
            unsupported = set(item.supported_efforts) - GEMINI_SUPPORTED_EFFORTS
            if unsupported:
                raise ValueError(
                    "Gemini CLI가 받지 않는 effort가 카탈로그에 있습니다: "
                    + ", ".join(sorted(unsupported))
                )
        return self

    @classmethod
    def load(cls, path: Path | str) -> "GeminiModelCatalog":
        data = Path(path).read_bytes()
        catalog = cls.model_validate(check_raw_gemini_catalog(data))
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


def check_raw_gemini_catalog(data: bytes) -> dict[str, Any]:
    """typed 변환 전에 카탈로그 원문을 검사한다. 정규화·중복 제거·생략 없이 거부한다."""

    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("GEMINI_MODEL_CATALOG_INVALID: JSON key가 중복됐습니다.")
        return dict(pairs)

    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except UnicodeDecodeError as error:
        raise ValueError("GEMINI_MODEL_CATALOG_INVALID: UTF-8 JSON이 아닙니다.") from error
    if not isinstance(document, dict) or set(document) != {"format", "models"}:
        raise ValueError("GEMINI_MODEL_CATALOG_INVALID: 최상위는 format·models만 가진 object여야 합니다.")
    rows = document["models"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("GEMINI_MODEL_CATALOG_INVALID: models는 비어 있지 않은 배열이어야 합니다.")
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"model", "supported_efforts"}:
            raise ValueError("GEMINI_MODEL_CATALOG_INVALID: model 행은 model·supported_efforts만 가집니다.")
        model, efforts = row["model"], row["supported_efforts"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError("GEMINI_MODEL_CATALOG_INVALID: 빈/null model이 있습니다.")
        if not isinstance(efforts, list) or not efforts or any(
            not isinstance(effort, str) or not effort.strip() for effort in efforts
        ):
            raise ValueError("GEMINI_MODEL_CATALOG_INVALID: 빈/null effort가 있습니다.")
        if len(efforts) != len(set(efforts)):
            raise ValueError("GEMINI_MODEL_CATALOG_INVALID: effort가 중복됐습니다.")
        names.append(model)
    if len(names) != len(set(names)):
        raise ValueError("GEMINI_MODEL_CATALOG_INVALID: model이 중복됐습니다.")
    return document


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def project_gemini_usage(usage: Any) -> dict[str, int] | None:
    """`result.usage`의 제공 구성요소만 Engine provider usage 키로 옮긴다.

    inputTokens는 세 입력 구성요소의 합, cachedInputTokens는 cache read다.
    reasoning은 제공된 thinking token만 쓴다. totalTokens는 만들지 않는다.
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


def gemini_turn_usage(result: dict[str, Any] | None) -> tuple[dict[str, int] | None, str, str]:
    """(usage, usage_scope, 판단 근거)를 돌려준다. turn 범위를 확인할 수 없으면 usage는 None이다."""
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
    projected = project_gemini_usage(usage)
    if not projected:
        return None, "unavailable", "result_usage_components_missing"
    return projected, "turn", "success_result_usage_with_iterations"


@dataclass
class _GeminiTurn:
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
class _GeminiThread:
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
    current: _GeminiTurn | None = None
    turns: list[_GeminiTurn] = field(default_factory=list)
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES))
    protocol_errors: list[str] = field(default_factory=list)
    control_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    control_waiters: dict[str, threading.Event] = field(default_factory=dict)
    reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None
    exit_code: int | None = None


class GeminiRuntime:
    """Gemini CLI(`gemini -p`, stream-json) runtime adapter.

    매 turn의 provider init 이벤트가 `bypassPermissions`인지 확인하고 다르면 turn을 중단한다.
    """

    provider = GEMINI_PROVIDER
    requires_budget_policy = False
    emits_rpc_operation_trace = False

    def __init__(
        self,
        *,
        model_catalog: GeminiModelCatalog,
        gemini_bin: Path | str | None = None,
        interpreter: Path | str | None = None,
        state_root: Path | str | None = None,
        setting_sources: str = "",
        config_dir: Path | str | None = None,
        turn_start_timeout_seconds: float = 300.0,
        env: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(model_catalog, GeminiModelCatalog):
            raise RuntimePolicyError("GEMINI_MODEL_CATALOG_REQUIRED: 모델 카탈로그를 명시해야 합니다.")
        if gemini_bin is None:
            located = shutil.which("gemini")
            if located is None:
                raise RuntimePolicyError("GEMINI_EXECUTABLE_NOT_FOUND: gemini 실행 파일이 없습니다.")
            gemini_bin = located
        resolved = Path(gemini_bin).resolve(strict=True)
        if resolved.suffix.lower() in GEMINI_SHIM_SUFFIXES:
            raise RuntimePolicyError(
                "GEMINI_EXECUTABLE_SHIM_UNSUPPORTED: "
                f"{resolved.name}는 shell shim입니다. 실제 gemini 실행 파일을 지정해야 합니다."
            )
        self.executable_digest = _file_digest(resolved)
        self._gemini_bin = resolved
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
            else Path(os.environ.get("GEMINI_CONFIG_DIR") or (Path.home() / ".gemini"))
        )
        self._turn_start_timeout_seconds = turn_start_timeout_seconds
        self._owned_state_root: Path | None = None
        if state_root is None:
            self._owned_state_root = Path(tempfile.mkdtemp(prefix="flowmarshal-gemini-"))
            self._state_root = self._owned_state_root
        else:
            self._state_root = Path(state_root).resolve()
            self._state_root.mkdir(parents=True, exist_ok=True)
        self._threads: dict[str, _GeminiThread] = {}
        self._interrupted_turn_ids: set[str] = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 속성
    @property
    def project_binding(self) -> None:
        return None

    @property
    def gemini_executable(self) -> Path:
        return self._gemini_bin

    def __enter__(self) -> "GeminiRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------ executable 잠금
    def _probe_cli_version(self) -> str:
        """`gemini --version` 첫 토큰을 잠금 값으로 쓴다. 모델 호출은 없다."""
        try:
            completed = subprocess.run(
                [*self._launcher, "--version"],
                capture_output=True,
                timeout=60,
                env=self._env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimePolicyError(f"GEMINI_CLI_VERSION_UNAVAILABLE: {error}") from error
        words = completed.stdout.decode("utf-8", errors="replace").split()
        if completed.returncode != 0 or not words:
            raise RuntimePolicyError("GEMINI_CLI_VERSION_UNAVAILABLE: gemini --version 출력이 없습니다.")
        return words[0]

    def _verify_executable(self) -> None:
        """프로세스를 띄우기 직전 잠금한 executable bytes와 같은지 다시 확인한다."""
        try:
            current = _file_digest(self._gemini_bin)
        except OSError as error:
            raise RuntimePolicyError(f"GEMINI_EXECUTABLE_CHANGED: {error}") from error
        if current != self.executable_digest:
            raise RuntimePolicyError(
                "GEMINI_EXECUTABLE_CHANGED: gemini 실행 파일이 잠금 뒤 바뀌었습니다. 새 binding이 필요합니다."
            )

    # ------------------------------------------------------------ 정책·inventory
    def _policy_document(self) -> dict[str, Any]:
        return {
            "provider": GEMINI_PROVIDER,
            "permission_mode": GEMINI_PERMISSION_MODE,
            "permission_prompts": GEMINI_PERMISSION_PROMPTS,
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
            profile_catalog_digest=sha256_digest([GEMINI_PERMISSION_MODE]),
            cwd=str(workspace),
        )

    def list_models(self) -> ModelInventory:
        self._verify_executable()
        try:
            raw = {**self.model_catalog.raw_response(), "cliVersion": self.cli_version}
            return ModelInventory(
                source=GEMINI_INVENTORY_SOURCE_PREFIX + self.executable_digest,
                models=self.model_catalog.models,
                raw_response=raw,
                executable_digest=self.executable_digest,
                runtime_capabilities=gemini_runtime_capabilities(self.cli_version),
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
            raise RuntimePolicyError("MODEL_BINDING_UNAVAILABLE: Gemini 카탈로그에 없는 모델입니다.")
        thread_id = str(uuid.uuid4())
        thread_dir = self._state_root / thread_id
        thread_dir.mkdir(parents=True, exist_ok=False)
        instructions_path = thread_dir / "developer-instructions.txt"
        instructions_path.write_bytes(developer_instructions.encode("utf-8"))
        instructions_digest = sha256_digest(developer_instructions)
        state = {
            "format": "flowmarshal-gemini-thread-v1",
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
            self._threads[thread_id] = _GeminiThread(
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
                "provider": GEMINI_PROVIDER,
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
        thread: _GeminiThread,
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
            "--permission-mode", GEMINI_PERMISSION_MODE,
            "--permission-prompts", GEMINI_PERMISSION_PROMPTS,
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
                "GEMINI_COMMAND_LINE_TOO_LONG: 명령줄이 "
                f"{len(command_line)}자로 한도 {MAX_COMMAND_LINE_CHARS}자를 넘습니다."
            )
        return argv

    def _spawn(self, thread: _GeminiThread, argv: list[str]) -> None:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(thread.cwd),
            env=self._env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        thread.process = process
        thread.exit_code = None
        reader = threading.Thread(
            target=self._read_stdout, args=(thread, process),
            name=f"flowmarshal-gemini-{thread.thread_id}", daemon=True,
        )
        thread.reader = reader
        reader.start()
        stderr_reader = threading.Thread(
            target=self._read_stderr, args=(thread, process),
            name=f"flowmarshal-gemini-stderr-{thread.thread_id}", daemon=True,
        )
        thread.stderr_reader = stderr_reader
        stderr_reader.start()

    @staticmethod
    def _read_stderr(thread: _GeminiThread, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        try:
            for raw in process.stderr:
                thread.stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
        finally:
            process.stderr.close()

    def _read_stdout(self, thread: _GeminiThread, process: subprocess.Popen) -> None:
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
            finished: _GeminiTurn | None = None
            with self._lock:
                if thread.process is process:
                    thread.exit_code = exit_code
                turn = thread.current
                if turn is not None and not turn.done.is_set():
                    turn.collector_error = (
                        "GEMINI_COLLECTOR_ENDED_WITHOUT_RESULT: "
                        f"exit={exit_code}"
                    )
                    turn.replayed.set()
                    turn.done.set()
                    finished = turn
                for waiter in thread.control_waiters.values():
                    waiter.set()
            if finished is not None:
                self._notify_observer(thread, finished)

    def _dispatch(self, thread: _GeminiThread, process: subprocess.Popen, event: dict[str, Any]) -> None:
        kind = event.get("type")
        finished: _GeminiTurn | None = None
        transcript = None
        if kind == "result":
            with self._lock:
                current = thread.current
                current_turn_id = None if current is None else current.turn_id
            transcript = self._live_transcript_model_observation(
                thread.thread_id, current_turn_id,
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
                        "gemini_cli_version", "uuid", "apiKeySource",
                    )
                }
                violation = None
                if event.get("session_id") != thread.thread_id:
                    violation = "THREAD_PROVENANCE_MISMATCH: provider session이 다릅니다."
                elif event.get("permissionMode") != GEMINI_PERMISSION_MODE:
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
                elif event.get("gemini_cli_version") != self.cli_version:
                    violation = (
                        "GEMINI_CLI_VERSION_MISMATCH: provider 버전이 "
                        f"{event.get('gemini_cli_version')!r}로 잠금 값 {self.cli_version!r}와 다릅니다."
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

    def _write(self, thread: _GeminiThread, document: dict[str, Any]) -> None:
        process = thread.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimePolicyError("GEMINI_PROCESS_NOT_RUNNING: provider 프로세스가 없습니다.")
        process.stdin.write((json.dumps(document, ensure_ascii=True) + "\n").encode("utf-8"))
        process.stdin.flush()

    def _stop_process(self, thread: _GeminiThread, *, timeout_seconds: float) -> None:
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
        if thread.stderr_reader is not None:
            thread.stderr_reader.join(timeout=5)

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
        if effort not in GEMINI_SUPPORTED_EFFORTS:
            raise RuntimePolicyError(f"MODEL_BINDING_UNAVAILABLE: Gemini가 받지 않는 effort입니다: {effort}")
        if not any(
            item.model == model and effort in item.supported_efforts
            for item in self.model_catalog.models
        ):
            raise RuntimePolicyError("MODEL_BINDING_UNAVAILABLE: Gemini 카탈로그에 없는 model/effort입니다.")
        with self._lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                raise RuntimePolicyError(
                    "GEMINI_THREAD_NOT_OPEN: 이 연결에서 만들거나 resume한 thread가 아닙니다."
                )
            if not _same_path(workspace, thread.cwd):
                raise RuntimePolicyError("THREAD_PROVENANCE_MISMATCH: thread cwd가 다릅니다.")
            if thread.current is not None and not thread.current.done.is_set():
                raise RuntimePolicyError("GEMINI_TURN_ALREADY_ACTIVE: 진행 중인 turn이 있습니다.")
            schema_digest = None if output_schema is None else sha256_digest(output_schema)
            process_key = (model, effort, schema_digest)
            process_alive = thread.process is not None and thread.process.poll() is None
            restart_required = process_alive and thread.process_key != process_key
        if restart_required:
            if thread.ephemeral:
                raise RuntimePolicyError(
                    "GEMINI_TURN_CONFIGURATION_CHANGE_REQUIRES_PERSISTED_THREAD: "
                    "ephemeral thread의 model/effort/schema는 바꿀 수 없습니다."
                )
            self._stop_process(thread, timeout_seconds=30)
            process_alive = False
        first_empty_thread = thread.started_turns == 0 and not thread.resumed
        if not process_alive:
            if thread.started_turns > 0 and thread.ephemeral:
                raise RuntimePolicyError(
                    "GEMINI_EPHEMERAL_THREAD_PROCESS_LOST: 저장하지 않은 thread는 다시 열 수 없습니다."
                )
            resume = thread.started_turns > 0 or thread.resumed
            argv = self._argv(
                thread, model=model, effort=effort, output_schema=output_schema, resume=resume,
            )
            self._verify_executable()
        prompt_digest = sha256_digest(prompt)
        turn = _GeminiTurn(
            prompt=prompt,
            prompt_digest=prompt_digest,
            model=model,
            effort=effort,
            sent_at=utc_now().isoformat(),
            first_empty_thread=first_empty_thread,
        )
        try:
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
                    "GEMINI_TURN_START_UNCONFIRMED: provider가 turn 수신을 확인하지 않았습니다."
                )
            if turn.policy_violation is not None:
                raise RuntimePolicyError(turn.policy_violation)
            if turn.turn_id is None:
                raise RuntimePolicyError(
                    "GEMINI_TURN_START_UNCONFIRMED: "
                    + (turn.collector_error or "provider replay가 없습니다.")
                )
            if turn.init_event is None:
                raise RuntimePolicyError("PERMISSION_POLICY_MISMATCH: turn init 관측이 없습니다.")
        except BaseException:
            turn.done.set()
            turn.replayed.set()
            self._stop_process(thread, timeout_seconds=1.0)
            raise
        return RuntimeOperationReceipt(
            operation_id=turn.turn_id,
            payload={
                "provider": GEMINI_PROVIDER,
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
                "provider_cli_version": turn.init_event.get("gemini_cli_version"),
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
    def _terminal_status(turn: _GeminiTurn) -> str | None:
        result = turn.result
        if result is None:
            return None
        if result.get("permission_denials"):
            return "failed"
        if result.get("subtype") == "success" and result.get("is_error") is not True:
            return "completed"
        if turn.interrupt_requested:
            return "interrupted"
        return "failed"

    @staticmethod
    def _final_response(turn: _GeminiTurn) -> str | None:
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

    def _observation(self, thread: _GeminiThread, turn: _GeminiTurn) -> RuntimeObservation:
        with self._lock:
            active = not turn.done.is_set()
            status = None if active else self._terminal_status(turn)
            result = turn.result
            provider_result = None
            if result is not None:
                provider_result = {key: value for key, value in result.items() if key != "result"}
                if isinstance(result.get("result"), str):
                    provider_result["result_digest"] = sha256_digest(result["result"])
            usage, usage_scope, usage_scope_basis = gemini_turn_usage(result)
            denials = None if result is None else result.get("permission_denials")
            reported = list(turn.assistant_models)
            if turn.init_event is not None and isinstance(turn.init_event.get("model"), str):
                if turn.init_event["model"] not in reported:
                    reported.insert(0, turn.init_event["model"])
            payload: dict[str, Any] = {
                "provider": GEMINI_PROVIDER,
                "thread_id": thread.thread_id,
                "turn_id": turn.turn_id,
                "turn_status": status,
                "prompt_digest": turn.prompt_digest,
                "duration_ms": None if result is None else result.get("duration_ms"),
                "usage": usage,
                "usage_scope": usage_scope,
                "usage_scope_basis": usage_scope_basis,
                "usage_source": "gemini-cli:stream-json:result.usage",
                "usage_projection": GEMINI_USAGE_PROJECTION,
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
                            "code": "GEMINI_PERMISSION_DENIED" if denials else None,
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

    def _notify_observer(self, thread: _GeminiThread, turn: _GeminiTurn) -> None:
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
                "GEMINI_SESSION_TRANSCRIPT_NOT_FOUND: 저장 session 기록을 정확히 하나 찾지 못했습니다."
                if not matches
                else "GEMINI_SESSION_TRANSCRIPT_AMBIGUOUS: 같은 session 기록이 여러 개입니다."
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
                if index == len(lines) - 1:
                    break
                raise RuntimePolicyError(
                    "GEMINI_SESSION_TRANSCRIPT_CORRUPT: 저장 session 기록을 읽지 못했습니다."
                ) from error
            if isinstance(document, dict):
                records.append(document)
        return records

    @staticmethod
    def _transcript_model_observation(turn: dict[str, Any] | None) -> dict[str, Any]:
        """저장 turn의 assistant 줄이 명시한 model·effort가 정확히 한 쌍일 때만 관측값으로 싣는다."""
        pairs = {
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
                    "model_observation_source": GEMINI_SESSION_TRANSCRIPT_MODEL_OBSERVATION_SOURCE,
                    "model_observation_reason": None,
                }
        return {
            "observed_model": None,
            "observed_effort": None,
            "model_observation_source": None,
            "model_observation_reason": (
                "GEMINI_TRANSCRIPT_MODEL_EFFORT_AMBIGUOUS"
                if len(pairs) > 1
                else "GEMINI_TRANSCRIPT_MODEL_EFFORT_NOT_REPORTED"
            ),
        }

    def _live_transcript_model_observation(self, thread_id: str, turn_id: str | None) -> dict[str, Any]:
        try:
            records = self._parse_transcript(self._session_transcript(thread_id).read_bytes())
            matches = [item for item in self._stored_turns(records) if item["start"].get("uuid") == turn_id]
            return self._transcript_model_observation(matches[0] if len(matches) == 1 else None)
        except Exception:  # noqa: BLE001
            return {
                **self._transcript_model_observation(None),
                "model_observation_reason": "GEMINI_SESSION_TRANSCRIPT_UNAVAILABLE",
            }

    @staticmethod
    def _stored_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        by_prompt: dict[str, dict[str, Any]] = {}
        for record in records:
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
            operation_name=f"gemini.read_stored:{thread_id}:{turn_id or 'latest'}",
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
                "provider": GEMINI_PROVIDER,
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
                "usage_source": "gemini-cli:session-transcript",
                "turn_history_available": True,
                "turn_history_error": None,
                "turn_history_source": "gemini-cli:session-transcript",
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
                    "GEMINI_THREAD_STATE_REQUIRED: 같은 state root의 thread 기록이 없습니다."
                )
            return None
        state = json.loads(path.read_text(encoding="utf-8"))
        instructions = Path(state["developer_instructions_path"])
        if (
            state.get("format") != "flowmarshal-gemini-thread-v1"
            or state.get("thread_id") != thread_id
            or not instructions.is_file()
            or sha256_digest(instructions.read_bytes().decode("utf-8"))
            != state.get("developer_instructions_digest")
        ):
            raise RuntimePolicyError("GEMINI_THREAD_STATE_MISMATCH: thread 기록이 손상됐습니다.")
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
                    "provider": GEMINI_PROVIDER, "thread_id": thread_id,
                    "cwd": str(workspace), "resumed": True, "live_connection": True,
                },
                binding=ThreadBinding(thread_id=thread_id, bound_at=utc_now()),
            )
        state = self._load_thread_state(thread_id, required=True)
        assert state is not None
        if state.get("ephemeral") is True:
            raise RuntimePolicyError(
                "GEMINI_EPHEMERAL_THREAD_PROCESS_LOST: 저장하지 않은 thread는 resume할 수 없습니다."
            )
        if not _same_path(state["cwd"], workspace):
            raise RuntimePolicyError("THREAD_PROVENANCE_MISMATCH: thread cwd가 다릅니다.")
        transcript = self._session_transcript(thread_id)
        data = transcript.read_bytes()
        with self._lock:
            self._threads[thread_id] = _GeminiThread(
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
                "provider": GEMINI_PROVIDER,
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
                    "GEMINI_INTERRUPT_TARGET_NOT_LIVE: 이 연결에서 실행 중인 turn이 아닙니다."
                )
            if turn.done.is_set():
                raise RuntimePolicyError(
                    "GEMINI_INTERRUPT_TARGET_NOT_ACTIVE: turn이 이미 종료됐습니다."
                )
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
                "GEMINI_INTERRUPT_UNCONFIRMED: provider가 interrupt 응답 없이 종료됐습니다."
            )
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={
                "provider": GEMINI_PROVIDER,
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
                except BaseException:  # noqa: BLE001
                    pass
            if turn is not None:
                turn.done.wait(timeout=max(0.0, deadline - time.monotonic()))
            self._stop_process(thread, timeout_seconds=max(0.0, deadline - time.monotonic()))
        if self._owned_state_root is not None:
            shutil.rmtree(self._owned_state_root, ignore_errors=True)
            self._owned_state_root = None


def open_gemini_runtime(
    *,
    model_catalog_path: Path | str,
    gemini_bin: Path | str | None = None,
    state_root: Path | str | None = None,
    setting_sources: str = "",
) -> GeminiRuntime:
    return GeminiRuntime(
        model_catalog=GeminiModelCatalog.load(model_catalog_path),
        gemini_bin=gemini_bin,
        state_root=state_root,
        setting_sources=setting_sources,
    )


# ------------------------------------------------------------ Antigravity (agy.exe) stream-json bridge
AGY_CLI_VERSION = "0.66.0-agy"
DEFAULT_AGY_EXE = Path.home() / ".gemini" / "bin" / ("agy.exe" if os.name == "nt" else "agy")

AGY_MODEL_MAP: dict[tuple[str, str], str] = {
    ("gemini-3-flash", "low"): "Gemini 3.8 Flash (Low)",
    ("gemini-3-flash", "medium"): "Gemini 3.8 Flash (Medium)",
    ("gemini-3-flash", "high"): "Gemini 3.8 Flash (High)",
    ("gemini-3.1-pro", "low"): "Gemini 4 Argon (Low)",
    ("gemini-3.1-pro", "medium"): "Gemini 4 Argon (Medium)",
    ("gemini-3.1-pro", "high"): "Gemini 4 Argon (High)",
}


def resolve_agy_exe(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    env_override = os.environ.get("FLOWMARSHAL_AGY_EXE")
    if env_override:
        return Path(env_override).expanduser().resolve()
    if DEFAULT_AGY_EXE.exists():
        return DEFAULT_AGY_EXE
    which_agy = shutil.which("agy.exe" if os.name == "nt" else "agy") or shutil.which("agy")
    if which_agy:
        return Path(which_agy).resolve()
    return DEFAULT_AGY_EXE


def map_to_agy_model(model: str, effort: str | None) -> str:
    normalized_effort = (effort or "high").strip().lower()
    if normalized_effort not in {"low", "medium", "high"}:
        normalized_effort = "high"
    effective_effort = os.environ.get("FLOWMARSHAL_AGY_EFFECTIVE_EFFORT", normalized_effort).strip().lower()
    if effective_effort not in {"low", "medium", "high"}:
        effective_effort = normalized_effort
    mapped = AGY_MODEL_MAP.get((model, effective_effort))
    if mapped is not None:
        return mapped
    title_effort = effective_effort.capitalize()
    if "pro" in model or "argon" in model:
        return f"Gemini 4 Argon ({title_effort})"
    return f"Gemini 3.8 Flash ({title_effort})"


def default_agy_gemini_model_catalog() -> GeminiModelCatalog:
    return GeminiModelCatalog(
        format=GEMINI_MODEL_CATALOG_FORMAT,
        models=(
            ModelCapability(
                model="gemini-3-flash",
                supported_efforts=("low", "medium", "high"),
            ),
            ModelCapability(
                model="gemini-3.1-pro",
                supported_efforts=("low", "medium", "high"),
            ),
        ),
    )


def write_agy_gemini_model_catalog(path: Path) -> Path:
    catalog = default_agy_gemini_model_catalog()
    payload = {
        "format": catalog.format,
        "models": [
            {
                "model": entry.model,
                "supported_efforts": list(entry.supported_efforts),
            }
            for entry in catalog.models
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def default_agy_gemini_role_configuration() -> Any:
    from .models import EngineRoleConfiguration, RoleModelBinding

    flash_low = RoleModelBinding(model="gemini-3-flash", effort="low")
    flash_high = RoleModelBinding(model="gemini-3-flash", effort="high")
    return EngineRoleConfiguration(
        normalizer=flash_low,
        skeleton_generator=flash_low,
        plan_expander=flash_high,
        general_reviewer=flash_high,
        critical_reviewer=flash_high,
        executor=flash_high,
        validator=flash_low,
    )


def create_agy_gemini_runtime(
    *,
    state_root: Path,
    catalog_path: Path | None = None,
) -> GeminiRuntime:
    resolved_state_root = state_root.expanduser().resolve()
    resolved_state_root.mkdir(parents=True, exist_ok=True)
    if catalog_path is not None:
        write_agy_gemini_model_catalog(catalog_path)
    return GeminiRuntime(
        model_catalog=default_agy_gemini_model_catalog(),
        gemini_bin=Path(__file__).resolve(),
        state_root=resolved_state_root,
        interpreter=sys.executable,
    )


def _agy_transcript_path(config_dir: Path, cwd: str, session_id: str) -> Path:
    project_dir = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir / f"{session_id}.jsonl"


def _agy_append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _agy_emit_stdout(payload: dict[str, Any]) -> None:
    line = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()


def _agy_parse_json_loose(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            parsed = json.loads(stripped[first_brace : last_brace + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _agy_extract_python_files_from_prompt(prompt: str, cwd: Path) -> list[str]:
    candidates: list[str] = []
    for match in re.findall(r"([A-Za-z0-9_./\\-]+\.py)", prompt):
        cleaned = match.strip().replace("\\", "/").lstrip("./")
        if not cleaned or cleaned.startswith("src/flowmarshal") or "agy_gemini_bridge" in cleaned:
            continue
        if Path(cleaned).is_absolute():
            continue
        if cleaned not in candidates:
            candidates.append(cleaned)
    if not candidates and cwd.exists():
        for py_file in sorted(cwd.glob("*.py")):
            if py_file.name not in candidates:
                candidates.append(py_file.name)
    return candidates


def _agy_compose_full_prompt(
    *,
    user_prompt: str,
    developer_instructions: str,
    schema: dict[str, Any] | None,
) -> str:
    sections: list[str] = []
    if developer_instructions.strip():
        sections.append(f"<SYSTEM_INSTRUCTIONS>\n{developer_instructions.strip()}\n</SYSTEM_INSTRUCTIONS>")
    sections.append(user_prompt)
    if schema is not None:
        schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        sections.append(
            "<OUTPUT_JSON_SCHEMA>\n"
            "Do NOT call any file-reading, shell, or external tools. "
            "Keep all rationale and text fields concise (1-2 sentences each) to stay well within output token limits. "
            "Respond with ONLY a single valid JSON object strictly conforming to this JSON Schema:\n"
            f"{schema_json}\n"
            "</OUTPUT_JSON_SCHEMA>"
        )
    else:
        sections.append(
            "<WORKER_EXECUTION_INSTRUCTIONS>\n"
            "You are executing a Task in the current workspace directory. "
            "Respond by writing the complete file contents for EVERY target Python file requested in the task "
            "inside fenced ```python code blocks where the VERY FIRST LINE of each code block is "
            "`# filename: <relative_path.py>` (for example, `# filename: calc_app.py` or `# filename: test_calc_app.py`). "
            "Do NOT omit any target file.\n"
            "</WORKER_EXECUTION_INSTRUCTIONS>"
        )
    return "\n\n".join(sections)


def _agy_invoke_cli(
    *,
    prompt_for_agy: str,
    model: str,
    effort: str | None,
    cwd: str,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agy_exe = resolve_agy_exe()
    mapped_model = map_to_agy_model(model, effort)
    resolved_cwd = Path(cwd).resolve()
    resolved_cwd.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(agy_exe),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        mapped_model,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--project",
        "default-cli-project",
    ]
    if schema is not None:
        cmd.extend(["--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":"))])
    cmd.append("--print=")

    ndjson_input = (
        json.dumps(
            {
                "event": "user",
                "prompt": prompt_for_agy,
                "message": {"role": "user", "content": prompt_for_agy},
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    timeout_seconds = int(os.environ.get("FLOWMARSHAL_AGY_TIMEOUT_SECONDS") or 300)
    try:
        completed = subprocess.run(
            cmd,
            input=ndjson_input,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Antigravity CLI (agy.exe) timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise RuntimeError(f"Antigravity CLI (agy.exe) invocation failed: {exc}") from exc
    stdout_text = completed.stdout or ""
    stderr_text = completed.stderr or ""
    if completed.returncode != 0:
        raise RuntimeError(
            f"Antigravity CLI (agy.exe) exited with non-zero code {completed.returncode}: {stderr_text or stdout_text}"
        )
    result_event: dict[str, Any] | None = None
    init_event: dict[str, Any] | None = None
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "init":
            init_event = event
        elif event.get("event") == "result":
            result_event = event
    if result_event is None:
        raise RuntimeError(
            f"Antigravity CLI (agy.exe) did not emit a result event (rc={completed.returncode}): {stderr_text or stdout_text}"
        )
    result_body = result_event.get("result") if isinstance(result_event.get("result"), dict) else {}
    status_str = str(result_body.get("status") or "SUCCESS")
    if status_str.upper() in {"ERROR", "FAILED", "CANCELLED"} or result_event.get("is_error") is True:
        error_detail = str(result_body.get("error") or stderr_text)
        if schema is not None and "output token limit" in error_detail.lower():
            concise_prompt = (
                prompt_for_agy
                + "\n\nCRITICAL: Your previous output exceeded the token limit. "
                "Keep all text/rationale fields extremely brief (under 15 words each) and output ONLY the compact JSON object."
            )
            completed_retry = subprocess.run(
                cmd,
                input=json.dumps(
                    {
                        "event": "user",
                        "prompt": concise_prompt,
                        "message": {"role": "user", "content": concise_prompt},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                cwd=str(resolved_cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            retry_event = None
            for raw_line in (completed_retry.stdout or "").splitlines():
                line = raw_line.strip()
                if line.startswith("{"):
                    try:
                        ev = json.loads(line)
                        if isinstance(ev, dict) and ev.get("event") == "result":
                            retry_event = ev
                    except Exception:
                        pass
            if retry_event is not None:
                retry_body = retry_event.get("result") if isinstance(retry_event.get("result"), dict) else {}
                retry_status = str(retry_body.get("status") or "SUCCESS")
                if retry_status.upper() not in {"ERROR", "FAILED", "CANCELLED"} and retry_event.get("is_error") is not True:
                    result_event = retry_event
                    result_body = retry_body
                    status_str = retry_status
        if status_str.upper() in {"ERROR", "FAILED", "CANCELLED"} or result_event.get("is_error") is True:
            raise RuntimeError(
                f"Antigravity CLI (agy.exe) reported failure status={status_str}: {result_body.get('error') or stderr_text}"
            )
    usage_body = result_body.get("usage") if isinstance(result_body.get("usage"), dict) else {}
    duration_seconds = float(result_body.get("duration_seconds") or 0.1)
    return {
        "returncode": completed.returncode,
        "mapped_model": mapped_model,
        "conversation_id": str(result_body.get("conversation_id") or (init_event or {}).get("conversation_id") or ""),
        "status": status_str,
        "response_text": str(result_body.get("response") or ""),
        "structured_output": result_body.get("structured_output"),
        "duration_ms": max(1, int(round(duration_seconds * 1000))),
        "num_turns": int(result_body.get("num_turns") or 1),
        "usage": {
            "input_tokens": int(usage_body.get("input_tokens") or 1),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": int(usage_body.get("cache_read_tokens") or 0),
            "output_tokens": int(usage_body.get("output_tokens") or 1),
            "output_tokens_details": {
                "thinking_tokens": int(usage_body.get("thinking_tokens") or 0),
            },
            "iterations": [
                {
                    "input_tokens": int(usage_body.get("input_tokens") or 1),
                    "output_tokens": int(usage_body.get("output_tokens") or 1),
                }
            ],
        },
    }


def _agy_extract_verified_structured_output(
    *,
    schema: dict[str, Any],
    agy_result: dict[str, Any],
) -> dict[str, Any]:
    raw_structured = agy_result.get("structured_output")
    response_text = str(agy_result.get("response_text") or "")
    parsed = (
        raw_structured
        if isinstance(raw_structured, dict)
        else _agy_parse_json_loose(response_text)
    )
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"AGY_STRUCTURED_OUTPUT_INVALID: LLM response did not contain a JSON object: {response_text[:500]}"
        )
    required_keys = set(schema.get("required") or ())
    missing = sorted(required_keys - set(parsed.keys()))
    if missing:
        raise RuntimeError(
            f"AGY_STRUCTURED_OUTPUT_MISSING_KEYS: missing required keys {missing} in {list(parsed.keys())}"
        )
    return parsed


def _agy_apply_worker_effects(user_prompt: str, agy_result: dict[str, Any], cwd: str) -> str:
    cwd_path = Path(cwd).resolve()
    cwd_path.mkdir(parents=True, exist_ok=True)
    response_text = str(agy_result.get("response_text") or "")

    written_files: list[str] = []
    fenced_blocks = re.findall(
        r"```(?:python|py)?(?:[:\s#]+([A-Za-z0-9_./\\-]+\.py))?\s*\n(.*?)```",
        response_text,
        flags=re.DOTALL,
    )
    prompt_files = _agy_extract_python_files_from_prompt(user_prompt, cwd_path)

    for idx, (header_path, code_body) in enumerate(fenced_blocks):
        rel_path = header_path.strip() if header_path else ""
        if not rel_path:
            first_line_match = re.search(
                r"^\s*#\s*(?:file|path|filename)?\s*:?\s*([A-Za-z0-9_./\\-]+\.py)",
                code_body,
                flags=re.MULTILINE,
            )
            if first_line_match:
                rel_path = first_line_match.group(1).strip()
        if not rel_path:
            if "unittest" in code_body or "def test_" in code_body:
                for pf in prompt_files:
                    if "test" in pf.lower():
                        rel_path = pf
                        break
        if not rel_path and idx < len(prompt_files):
            rel_path = prompt_files[idx]
        if rel_path:
            safe_rel = rel_path.replace("\\", "/").lstrip("./")
            target = (cwd_path / safe_rel).resolve()
            if str(target).startswith(str(cwd_path)) and code_body.strip():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(code_body.strip() + "\n", encoding="utf-8")
                written_files.append(safe_rel)

    existing_targets = [
        rel
        for rel in prompt_files
        if (cwd_path / rel.replace("\\", "/").lstrip("./")).is_file()
    ]
    if not written_files and not existing_targets and not response_text.strip():
        raise RuntimeError(
            "AGY_WORKER_EMPTY_RESPONSE: Worker response did not produce files or output text."
        )
    return response_text.strip() or f"Completed worker turn ({', '.join(written_files or existing_targets)})."


def agy_bridge_main(argv: list[str] | None = None) -> int:
    import argparse

    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        sys.stdout.write(AGY_CLI_VERSION + "\n")
        sys.stdout.flush()
        return 0

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true")
    parser.add_argument("--input-format", default="stream-json")
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--permission-mode", default="bypassPermissions")
    parser.add_argument("--model", default="gemini-3-flash")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--append-system-prompt-file", default=None)
    parser.add_argument("--json-schema", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--resume", default=None)
    parsed, _ = parser.parse_known_args(args)

    session_id = str(parsed.session_id or parsed.resume or uuid.uuid4())
    model = str(parsed.model or "gemini-3-flash")
    effort = str(parsed.effort or "high")
    cwd = os.getcwd()
    config_dir = Path(os.environ.get("GEMINI_CONFIG_DIR") or (Path.home() / ".gemini")).resolve()
    transcript_file = _agy_transcript_path(config_dir, cwd, session_id)

    developer_instructions = ""
    if parsed.append_system_prompt_file:
        try:
            prompt_path = Path(parsed.append_system_prompt_file)
            if prompt_path.is_file():
                developer_instructions = prompt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            developer_instructions = ""

    schema: dict[str, Any] | None = None
    if parsed.json_schema:
        try:
            loaded_schema = json.loads(parsed.json_schema)
            if isinstance(loaded_schema, dict):
                schema = loaded_schema
        except Exception:
            schema = None

    while True:
        raw_stdin_bytes = sys.stdin.buffer.readline()
        if not raw_stdin_bytes:
            break
        raw_stdin = raw_stdin_bytes.decode("utf-8", errors="replace").strip()
        if not raw_stdin:
            continue
        user_prompt = ""
        try:
            stdin_obj = json.loads(raw_stdin)
            if isinstance(stdin_obj, dict):
                if stdin_obj.get("type") == "control_request":
                    req_id = stdin_obj.get("request_id") or ""
                    _agy_emit_stdout(
                        {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": req_id,
                            },
                        }
                    )
                    continue
                msg = stdin_obj.get("message")
                if isinstance(msg, dict):
                    user_prompt = str(msg.get("content") or "")
        except Exception:
            user_prompt = raw_stdin

        user_uuid = f"user-{uuid.uuid4().hex[:12]}"
        _agy_append_jsonl(
            transcript_file,
            {
                "type": "user",
                "sessionId": session_id,
                "uuid": user_uuid,
                "promptId": user_uuid,
                "promptSource": "stdin",
                "cwd": cwd,
                "parentUuid": None,
                "message": {"role": "user", "content": user_prompt},
            },
        )
        _agy_emit_stdout(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_id,
                "model": model,
                "permissionMode": parsed.permission_mode,
                "cwd": cwd,
                "gemini_cli_version": AGY_CLI_VERSION,
                "tools": ["Bash", "Read", "Edit"],
            }
        )
        _agy_emit_stdout(
            {
                "type": "user",
                "session_id": session_id,
                "uuid": user_uuid,
                "message": {"role": "user", "content": user_prompt},
                "isReplay": True,
            }
        )

        prompt_for_agy = _agy_compose_full_prompt(
            user_prompt=user_prompt,
            developer_instructions=developer_instructions,
            schema=schema,
        )
        try:
            agy_result = _agy_invoke_cli(
                prompt_for_agy=prompt_for_agy,
                model=model,
                effort=effort,
                cwd=cwd,
                schema=schema,
            )
            if schema is not None:
                structured = _agy_extract_verified_structured_output(
                    schema=schema,
                    agy_result=agy_result,
                )
            else:
                structured = None
        except Exception as exc:
            error_msg = str(exc)
            _agy_emit_stdout(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "session_id": session_id,
                    "uuid": f"result-{uuid.uuid4().hex[:12]}",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "num_turns": 1,
                    "result": error_msg,
                    "error": error_msg,
                    "stop_reason": "error",
                }
            )
            return 1

        usage = agy_result["usage"]
        assistant_uuid = f"assistant-{uuid.uuid4().hex[:12]}"
        message_id = f"msg-{uuid.uuid4().hex[:12]}"

        if schema is not None and structured is not None:
            tool_use_id = f"toolu_{uuid.uuid4().hex[:12]}"
            assistant_entry = {
                "type": "assistant",
                "sessionId": session_id,
                "uuid": assistant_uuid,
                "parentUuid": user_uuid,
                "model": model,
                "effort": effort,
                "message": {
                    "id": message_id,
                    "role": "assistant",
                    "model": model,
                    "effort": effort,
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "StructuredOutput",
                            "input": structured,
                        }
                    ],
                    "usage": usage,
                },
            }
            tool_result_entry = {
                "type": "user",
                "sessionId": session_id,
                "uuid": f"toolres-{uuid.uuid4().hex[:12]}",
                "parentUuid": assistant_uuid,
                "toolEndsTurn": True,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "Structured output accepted.",
                        }
                    ],
                },
            }
            _agy_append_jsonl(transcript_file, assistant_entry)
            _agy_append_jsonl(transcript_file, tool_result_entry)
            _agy_emit_stdout(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "uuid": assistant_uuid,
                    "message": assistant_entry["message"],
                }
            )
            _agy_emit_stdout(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": session_id,
                    "uuid": f"result-{uuid.uuid4().hex[:12]}",
                    "duration_ms": agy_result["duration_ms"],
                    "duration_api_ms": agy_result["duration_ms"],
                    "num_turns": agy_result["num_turns"],
                    "result": "",
                    "stop_reason": "tool_use",
                    "structured_output": structured,
                    "usage": usage,
                    "agy_conversation_id": agy_result["conversation_id"],
                    "agy_mapped_model": agy_result["mapped_model"],
                }
            )
            continue

        worker_text = _agy_apply_worker_effects(user_prompt, agy_result, cwd)
        assistant_entry = {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": assistant_uuid,
            "parentUuid": user_uuid,
            "model": model,
            "effort": effort,
            "message": {
                "id": message_id,
                "role": "assistant",
                "model": model,
                "effort": effort,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": worker_text}],
                "usage": usage,
            },
        }
        _agy_append_jsonl(transcript_file, assistant_entry)
        _agy_emit_stdout(
            {
                "type": "assistant",
                "session_id": session_id,
                "uuid": assistant_uuid,
                "message": assistant_entry["message"],
            }
        )
        _agy_emit_stdout(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "uuid": f"result-{uuid.uuid4().hex[:12]}",
                "duration_ms": agy_result["duration_ms"],
                "duration_api_ms": agy_result["duration_ms"],
                "num_turns": agy_result["num_turns"],
                "result": worker_text,
                "stop_reason": "end_turn",
                "usage": usage,
                "agy_conversation_id": agy_result["conversation_id"],
                "agy_mapped_model": agy_result["mapped_model"],
            }
        )
    return 0


__all__: Sequence[str] = (
    "GEMINI_INVENTORY_SOURCE_PREFIX",
    "GEMINI_MODEL_CATALOG_FORMAT",
    "GEMINI_PROVIDER",
    "GeminiModelCatalog",
    "GeminiRuntime",
    "agy_bridge_main",
    "check_raw_gemini_catalog",
    "create_agy_gemini_runtime",
    "default_agy_gemini_model_catalog",
    "default_agy_gemini_role_configuration",
    "gemini_runtime_capabilities",
    "gemini_turn_usage",
    "open_gemini_runtime",
    "project_gemini_usage",
    "write_agy_gemini_model_catalog",
)


if __name__ == "__main__":
    raise SystemExit(agy_bridge_main())
