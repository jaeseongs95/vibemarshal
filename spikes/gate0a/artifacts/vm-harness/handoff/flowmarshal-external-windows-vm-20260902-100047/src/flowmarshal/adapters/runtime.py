from __future__ import annotations

import threading
import json
import os
import tomllib
import queue
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain import IntentKind, new_id
from ..ports import RuntimeObservation, RuntimeReceipt


@dataclass
class _FakeThread:
    thread_id: str
    workspace: Path
    title: str
    turn_ids: list[str] = field(default_factory=list)
    terminal_status: str | None = None


class FakeAgentRuntime:
    """중복 외부 효과와 crash window를 검증하는 결정적 테스트 런타임."""

    def __init__(self, *, finish_turns_immediately: bool = True) -> None:
        self.finish_turns_immediately = finish_turns_immediately
        self._lock = threading.Lock()
        self._threads: dict[str, _FakeThread] = {}
        self.create_calls = 0
        self.turn_calls = 0
        self.interrupt_calls = 0
        self.last_execution_profile: dict[str, object] | None = None

    @property
    def thread_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._threads)

    def create_thread(
        self, *, project_id: str, workspace: Path, title: str
    ) -> RuntimeReceipt:
        del project_id
        with self._lock:
            self.create_calls += 1
            thread_id = new_id("external_thread")
            self._threads[thread_id] = _FakeThread(
                thread_id=thread_id,
                workspace=workspace,
                title=title,
            )
        return RuntimeReceipt(
            effect_kind=IntentKind.CREATE_THREAD,
            external_id=thread_id,
            payload={"thread_id": thread_id, "title": title},
        )

    def start_turn(
        self,
        *,
        thread_id: str,
        workspace: Path,
        prompt: str,
        execution_profile: dict[str, object] | None = None,
    ) -> RuntimeReceipt:
        del workspace, prompt
        with self._lock:
            self.turn_calls += 1
            self.last_execution_profile = execution_profile
            thread = self._threads[thread_id]
            turn_id = new_id("external_turn")
            thread.turn_ids.append(turn_id)
            if self.finish_turns_immediately:
                thread.terminal_status = "completed"
        return RuntimeReceipt(
            effect_kind=IntentKind.START_TURN,
            external_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id},
        )

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> RuntimeReceipt:
        with self._lock:
            self.interrupt_calls += 1
            thread = self._threads[thread_id]
            if turn_id not in thread.turn_ids:
                raise KeyError(turn_id)
            thread.terminal_status = "interrupted"
        return RuntimeReceipt(
            effect_kind=IntentKind.INTERRUPT_TURN,
            external_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id, "interrupted": True},
        )

    def observe(self, *, thread_id: str) -> RuntimeObservation:
        with self._lock:
            thread = self._threads[thread_id]
            turns = tuple(thread.turn_ids)
            active = bool(turns) and thread.terminal_status is None
            return RuntimeObservation(
                thread_id=thread_id,
                turn_id=None if not turns else turns[-1],
                active_turn=active,
                terminal_status=thread.terminal_status,
                known_turn_ids=turns,
                payload={
                    "thread_id": thread_id,
                    "turn_ids": list(turns),
                    "terminal_status": thread.terminal_status,
                },
            )


CODEX_DISABLED_SURFACE_OVERRIDES = (
    'web_search="disabled"',
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.skill_mcp_dependency_install=false",
    "features.multi_agent=false",
    "features.js_repl=false",
    "features.in_app_browser=false",
    "features.browser_use=false",
    "features.browser_use_full_cdp_access=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.plugin_sharing=false",
    "mcp_servers={}",
)


class CodexAppServerRuntime:
    """고정 SDK를 이용하는 Gate 0B 실제 합성 실행 adapter.

    Gate 0C의 최종 역할별 permission profile을 대신하지 않는다. 이 adapter의
    Gate 0B smoke는 합성 workspace, deny-all 승인, 외부 기능 비활성화와
    workspace-write sandbox를 함께 사용한다.
    """

    def __init__(
        self,
        *,
        codex_bin: Path | str | None = None,
        codex_home: Path | str | None = None,
        observation_timeout_seconds: float = 15.0,
    ) -> None:
        from openai_codex import Codex, CodexConfig

        home = None if codex_home is None else Path(codex_home).resolve()
        overrides = (
            *CODEX_DISABLED_SURFACE_OVERRIDES,
            *_disabled_mcp_overrides(home),
        )
        config = CodexConfig(
            codex_bin=None if codex_bin is None else str(codex_bin),
            config_overrides=overrides,
            env=None if home is None else {"CODEX_HOME": str(home)},
            client_name="flowmarshal_gate0b",
            client_title="FlowMarshal Gate 0B",
            client_version="0.2.0a0",
            experimental_api=True,
        )
        self._config = config
        self._client = Codex(config)
        self.observation_timeout_seconds = observation_timeout_seconds
        self._last_agent_messages: dict[str, str] = {}
        self._successful_command_threads: set[str] = set()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CodexAppServerRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_thread(
        self, *, project_id: str, workspace: Path, title: str
    ) -> RuntimeReceipt:
        from openai_codex import ApprovalMode, Sandbox

        del project_id
        thread = self._client.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=str(workspace),
            developer_instructions=(
                "이 task는 FlowMarshal Gate 0B 합성 실행이다. 지정된 workspace와 "
                "승인 자료만 사용하고 외부 서비스나 사용자 실제 자료에 접근하지 않는다."
            ),
            ephemeral=False,
            sandbox=Sandbox.workspace_write,
            service_name="flowmarshal_gate0b",
        )
        thread.set_name(title)
        return RuntimeReceipt(
            effect_kind=IntentKind.CREATE_THREAD,
            external_id=thread.id,
            payload={"thread_id": thread.id, "title": title},
        )

    def start_turn(
        self,
        *,
        thread_id: str,
        workspace: Path,
        prompt: str,
        execution_profile: dict[str, Any] | None = None,
    ) -> RuntimeReceipt:
        from openai_codex import ApprovalMode, Sandbox

        profile = execution_profile or {}
        model = profile.get("model_id")
        effort = profile.get("reasoning_effort")
        thread = self._client.thread_resume(thread_id, cwd=str(workspace))
        handle = thread.turn(
            prompt,
            approval_mode=ApprovalMode.deny_all,
            cwd=str(workspace),
            effort=effort,
            model=model,
            sandbox=Sandbox.workspace_write,
        )
        return RuntimeReceipt(
            effect_kind=IntentKind.START_TURN,
            external_id=handle.id,
            payload={
                "thread_id": thread_id,
                "turn_id": handle.id,
                "model_id": model,
                "reasoning_effort": effort,
            },
        )

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> RuntimeReceipt:
        from openai_codex import TurnHandle

        thread = self._client.thread_resume(thread_id)
        TurnHandle(thread._client, thread_id, turn_id).interrupt()  # noqa: SLF001
        return RuntimeReceipt(
            effect_kind=IntentKind.INTERRUPT_TURN,
            external_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id, "interrupted": True},
        )

    def observe(self, *, thread_id: str) -> RuntimeObservation:
        result_queue: queue.Queue[RuntimeObservation | BaseException] = queue.Queue(
            maxsize=1
        )

        def read_state() -> None:
            try:
                # 관측은 새 App Server나 thread/resume이 아니라 turn을 시작한
                # 동일 연결의 순수 thread/read여야 한다. 별도 writer 연결을 열고
                # 닫는 것만으로도 실행 중 turn 소유권에 영향을 줄 수 있다.
                read = self._client._client.thread_read(  # noqa: SLF001
                    thread_id, include_turns=True
                )
                result_queue.put(self._observation_from_read(thread_id, read))
            except BaseException as error:
                result_queue.put(error)

        worker = threading.Thread(
            target=read_state,
            daemon=True,
            name=f"flowmarshal-observe-{thread_id[-8:]}",
        )
        worker.start()
        try:
            result = result_queue.get(timeout=self.observation_timeout_seconds)
        except queue.Empty as error:
            raise TimeoutError("Codex thread 관측이 제한 시간 안에 끝나지 않았습니다.") from error
        if isinstance(result, BaseException):
            raise result
        return result

    def _observation_from_read(self, thread_id: str, read: Any) -> RuntimeObservation:
        turns = tuple(read.thread.turns)
        known_turn_ids = tuple(turn.id for turn in turns)
        latest = None if not turns else turns[-1]
        status = None if latest is None else _enum_value(latest.status)
        active = status == "inProgress"
        terminal = None if latest is None or active else status
        last_message: str | None = None
        if latest is not None:
            for item in latest.items:
                try:
                    document = item.model_dump(mode="json", by_alias=True)
                except AttributeError:
                    continue
                if document.get("type") == "agentMessage" and isinstance(
                    document.get("text"), str
                ):
                    last_message = document["text"]
                if (
                    document.get("type") == "commandExecution"
                    and document.get("status") == "completed"
                    and document.get("exitCode") == 0
                ):
                    self._successful_command_threads.add(thread_id)
        if last_message is not None:
            self._last_agent_messages[thread_id] = last_message
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=None if latest is None else latest.id,
            active_turn=active,
            terminal_status=terminal,
            known_turn_ids=known_turn_ids,
            payload={
                "thread_id": thread_id,
                "turn_id": None if latest is None else latest.id,
                "turn_status": status,
                "turn_count": len(turns),
                "last_agent_message_digest": None
                if last_message is None
                else "sha256:"
                + hashlib.sha256(last_message.encode("utf-8")).hexdigest(),
            },
        )

    def latest_agent_message(self) -> str | None:
        if not self._last_agent_messages:
            return None
        return tuple(self._last_agent_messages.values())[-1]

    def command_execution_succeeded(self) -> bool:
        return bool(self._successful_command_threads)

    def resolve_execution_profile(
        self, *, model_role: str, reasoning_effort: str
    ) -> dict[str, str]:
        models = tuple(self._client.models().data)
        if not models:
            raise RuntimeError("Codex가 사용 가능한 모델 목록을 반환하지 않았습니다.")
        preferred = models
        if model_role == "fast":
            fast = tuple(
                model
                for model in models
                if any(
                    marker in model.id.lower()
                    for marker in ("luna", "mini", "spark")
                )
            )
            if fast:
                preferred = fast
        selected = next(
            (model for model in preferred if getattr(model, "is_default", False)),
            preferred[0],
        )
        supported = {
            _enum_value(option.reasoning_effort)
            for option in selected.supported_reasoning_efforts
        }
        if reasoning_effort not in supported:
            raise RuntimeError(
                f"선택 모델 {selected.id}가 {reasoning_effort} 추론 수준을 지원하지 않습니다."
            )
        return {
            "model_role": model_role,
            "model_id": selected.id,
            "reasoning_effort": reasoning_effort,
        }


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _disabled_mcp_overrides(codex_home: Path | None = None) -> tuple[str, ...]:
    codex_home = codex_home or Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    )
    config_path = codex_home / "config.toml"
    try:
        with config_path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return ()
    overrides: list[str] = []
    for name in sorted(servers):
        prefix = f"mcp_servers.{json.dumps(str(name))}"
        overrides.append(f"{prefix}.enabled=false")
        definition = servers[name]
        if isinstance(definition, dict) and "command" in definition:
            # 일부 Desktop 전용 항목은 SDK 런타임이 원래 command를 transport로
            # 해석하지 못한다. 비활성화와 함께 실행 불가능한 inert stdio 모양으로
            # 덮어써야 기본 config 파싱 단계도 fail-closed로 통과한다.
            overrides.extend(
                (
                    f'{prefix}.command="__flowmarshal_disabled_mcp__"',
                    f"{prefix}.args=[]",
                )
            )
        elif isinstance(definition, dict) and "url" in definition:
            overrides.append(f'{prefix}.url="http://127.0.0.1:9"')
    return tuple(overrides)
