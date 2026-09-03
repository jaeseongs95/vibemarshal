from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


SCHEMA_VERSION = "1.0"
GATE_NAME = "R1"
RESULT_KIND = "flowmarshal_r1_runtime_receipt"
REQUIRED_PERMISSION_PROFILE = ":danger-full-access"
REQUIRED_APPROVAL_POLICY = "never"
SERVICE_NAME = "flowmarshal"
DEFAULT_TIMEOUT_SECONDS = 180.0
class RuntimeProbeError(RuntimeError):
    """R1의 실패 원인과 안정적인 오류 코드를 함께 보존한다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AppServerSession(Protocol):
    initialize_payload: Any
    unexpected_requests: tuple[str, ...]

    def __enter__(self) -> "AppServerSession": ...

    def __exit__(self, *_: object) -> None: ...

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any: ...

    def next_turn_notification(self, turn_id: str) -> Any: ...

    def unregister_turn_notifications(self, turn_id: str) -> None: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value):
        return {
            field.name: json_safe(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        return json_safe(value.value)
    return repr(value)


def sha256_json(value: Any) -> str:
    payload = json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _object(
    session: AppServerSession,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = json_safe(session.request(method, params))
    if not isinstance(value, dict):
        raise RuntimeProbeError(
            "INVALID_RPC_RESPONSE", f"{method} 응답이 JSON object가 아닙니다."
        )
    return value


def _canonical_path(value: str | Path) -> str:
    raw = str(value)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(raw)))


def _same_path(left: str | Path, right: str | Path) -> bool:
    return _canonical_path(left) == _canonical_path(right)


def _global_instruction_file(codex_home: Path) -> Path:
    override = codex_home / "AGENTS.override.md"
    selected = override if override.is_file() else codex_home / "AGENTS.md"
    if not selected.is_file():
        raise RuntimeProbeError(
            "INSTRUCTION_SOURCE_MISSING",
            f"전역 instruction 파일을 찾을 수 없습니다: {selected}",
        )
    return selected.resolve(strict=True)


def _project_instruction_file(workspace: Path) -> Path:
    selected = workspace / "AGENTS.md"
    if not selected.is_file():
        raise RuntimeProbeError(
            "INSTRUCTION_SOURCE_MISSING",
            f"프로젝트 instruction 파일을 찾을 수 없습니다: {selected}",
        )
    return selected.resolve(strict=True)


def _extract_config_value(config: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in config:
            return config[key]
    return None


def _list_permission_profiles(
    session: AppServerSession, workspace: Path
) -> tuple[dict[str, Any], ...]:
    cursor: str | None = None
    profiles: list[dict[str, Any]] = []
    while True:
        params: dict[str, Any] = {"cwd": str(workspace), "limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        response = _object(session, "permissionProfile/list", params)
        page = response.get("data")
        if not isinstance(page, list) or any(
            not isinstance(item, dict) for item in page
        ):
            raise RuntimeProbeError(
                "PERMISSION_POLICY_MISMATCH",
                "permissionProfile/list에서 유효한 profile 목록을 받지 못했습니다.",
            )
        profiles.extend(page)
        next_cursor = response.get("nextCursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or next_cursor == cursor:
            raise RuntimeProbeError(
                "INVALID_RPC_RESPONSE",
                "permissionProfile/list pagination cursor가 유효하지 않습니다.",
            )
        cursor = next_cursor
    return tuple(profiles)


def preflight_execution_policy(
    session: AppServerSession,
    workspace: Path,
) -> dict[str, Any]:
    """task 생성 전에 실제 유효 기본 권한을 확인한다."""

    response = _object(
        session,
        "config/read",
        {"cwd": str(workspace), "includeLayers": True},
    )
    config = response.get("config")
    if not isinstance(config, dict):
        raise RuntimeProbeError(
            "PERMISSION_POLICY_MISMATCH",
            "config/read에서 유효 설정을 확인하지 못했습니다.",
        )
    approval_policy = _extract_config_value(
        config, "approvalPolicy", "approval_policy"
    )
    default_permissions = _extract_config_value(
        config, "defaultPermissions", "default_permissions"
    )
    if (
        approval_policy != REQUIRED_APPROVAL_POLICY
        or default_permissions != REQUIRED_PERMISSION_PROFILE
    ):
        raise RuntimeProbeError(
            "PERMISSION_POLICY_MISMATCH",
            "실제 기본 실행 정책이 :danger-full-access / approval_policy=never가 아닙니다. "
            f"관측값: permissions={default_permissions!r}, approval={approval_policy!r}",
        )

    profiles = _list_permission_profiles(session, workspace)
    selected = next(
        (item for item in profiles if item.get("id") == REQUIRED_PERMISSION_PROFILE),
        None,
    )
    if selected is None or selected.get("allowed") is not True:
        raise RuntimeProbeError(
            "PERMISSION_POLICY_MISMATCH",
            f"{REQUIRED_PERMISSION_PROFILE} profile을 현재 런타임에서 사용할 수 없습니다.",
        )
    return {
        "environment": "local",
        "approval_policy": approval_policy,
        "default_permissions": default_permissions,
        "selected_profile_allowed": True,
        "config_digest": sha256_json(config),
        "profile_catalog_digest": sha256_json(profiles),
    }


def _reasoning_efforts(model: dict[str, Any]) -> tuple[str, ...]:
    raw = model.get("supportedReasoningEfforts")
    if not isinstance(raw, list):
        return ()
    efforts: list[str] = []
    for item in raw:
        if isinstance(item, str):
            efforts.append(item)
        elif isinstance(item, dict):
            value = item.get("reasoningEffort")
            if isinstance(value, str):
                efforts.append(value)
    return tuple(dict.fromkeys(efforts))


def select_probe_model(session: AppServerSession) -> dict[str, Any]:
    """하드코딩된 모델명이 아니라 현재 catalog 기본값을 사용한다."""

    cursor: str | None = None
    models: list[dict[str, Any]] = []
    while True:
        params: dict[str, Any] = {"includeHidden": False, "limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        response = _object(session, "model/list", params)
        page = response.get("data")
        if not isinstance(page, list) or any(
            not isinstance(item, dict) for item in page
        ):
            raise RuntimeProbeError(
                "MODEL_INVENTORY_INVALID",
                "model/list에서 유효한 모델 목록을 받지 못했습니다.",
            )
        models.extend(page)
        next_cursor = response.get("nextCursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or next_cursor == cursor:
            raise RuntimeProbeError(
                "INVALID_RPC_RESPONSE", "model/list pagination cursor가 유효하지 않습니다."
            )
        cursor = next_cursor

    visible = [item for item in models if item.get("hidden") is not True]
    if not visible:
        raise RuntimeProbeError(
            "MODEL_UNAVAILABLE", "현재 사용할 수 있는 visible Codex 모델이 없습니다."
        )
    selected = next((item for item in visible if item.get("isDefault") is True), visible[0])
    model_id = selected.get("id") or selected.get("model")
    if not isinstance(model_id, str) or not model_id:
        raise RuntimeProbeError(
            "MODEL_INVENTORY_INVALID", "선택된 모델에 유효한 ID가 없습니다."
        )
    efforts = _reasoning_efforts(selected)
    if not efforts:
        raise RuntimeProbeError(
            "MODEL_INVENTORY_INVALID",
            f"선택된 모델 {model_id}의 추론 수준 목록이 없습니다.",
        )
    default_effort = selected.get("defaultReasoningEffort")
    if "low" in efforts:
        effort = "low"
        effort_basis = "R1 최소 검사용으로 지원 목록의 low 선택"
    elif isinstance(default_effort, str) and default_effort in efforts:
        effort = default_effort
        effort_basis = "low 미지원으로 catalog 기본 추론 수준 선택"
    else:
        effort = efforts[0]
        effort_basis = "low·catalog 기본값 미지원으로 첫 지원 수준 선택"
    return {
        "model_id": model_id,
        "catalog_model": selected.get("model"),
        "reasoning_effort": effort,
        "supported_efforts": list(efforts),
        "selection_basis": "model/list의 visible catalog 기본 모델",
        "effort_basis": effort_basis,
        "catalog_count": len(visible),
        "catalog_digest": sha256_json(models),
    }


def _validate_instruction_sources(
    raw_sources: Any,
    expected_sources: tuple[Path, ...],
) -> tuple[str, ...]:
    if not isinstance(raw_sources, list) or any(
        not isinstance(item, str) for item in raw_sources
    ):
        raise RuntimeProbeError(
            "INSTRUCTION_PROVENANCE_MISSING",
            "thread 응답의 instructionSources 형식이 유효하지 않습니다.",
        )
    sources = tuple(raw_sources)
    missing = [
        str(expected)
        for expected in expected_sources
        if not any(_same_path(source, expected) for source in sources)
    ]
    if missing:
        raise RuntimeProbeError(
            "INSTRUCTION_PROVENANCE_MISSING",
            f"정상 입력인 instruction file이 Runner 문맥에 없습니다: {missing}",
        )
    return sources


def _validate_thread_response(
    response: dict[str, Any],
    *,
    workspace: Path,
    expected_sources: tuple[Path, ...],
    model_id: str,
    expected_thread_id: str | None = None,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    profile = response.get("activePermissionProfile")
    active_profile = profile.get("id") if isinstance(profile, dict) else None
    if active_profile != REQUIRED_PERMISSION_PROFILE:
        raise RuntimeProbeError(
            "PERMISSION_POLICY_MISMATCH",
            "thread의 실제 activePermissionProfile이 :danger-full-access가 아닙니다. "
            f"관측값: {active_profile!r}",
        )
    if response.get("approvalPolicy") != REQUIRED_APPROVAL_POLICY:
        raise RuntimeProbeError(
            "PERMISSION_POLICY_MISMATCH",
            "thread의 실제 approvalPolicy가 never가 아닙니다. "
            f"관측값: {response.get('approvalPolicy')!r}",
        )
    if not _same_path(str(response.get("cwd", "")), workspace):
        raise RuntimeProbeError(
            "THREAD_PROVENANCE_MISMATCH",
            f"thread cwd가 요청 workspace와 다릅니다: {response.get('cwd')!r}",
        )
    response_model = response.get("model")
    if response_model != model_id:
        raise RuntimeProbeError(
            "MODEL_PROVENANCE_MISMATCH",
            f"요청 모델({model_id})과 실제 모델({response_model})이 다릅니다.",
        )
    roots = response.get("runtimeWorkspaceRoots")
    if not isinstance(roots, list) or not any(
        isinstance(item, str) and _same_path(item, workspace) for item in roots
    ):
        raise RuntimeProbeError(
            "THREAD_PROVENANCE_MISMATCH",
            "runtimeWorkspaceRoots에서 요청 workspace를 확인하지 못했습니다.",
        )
    thread = response.get("thread")
    if not isinstance(thread, dict):
        raise RuntimeProbeError(
            "INVALID_RPC_RESPONSE", "thread 응답에 thread object가 없습니다."
        )
    thread_id = thread.get("id")
    session_id = thread.get("sessionId")
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeProbeError(
            "INVALID_RPC_RESPONSE", "thread ID를 확인하지 못했습니다."
        )
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeProbeError(
            "INVALID_RPC_RESPONSE", "thread sessionId를 확인하지 못했습니다."
        )
    if expected_thread_id is not None and thread_id != expected_thread_id:
        raise RuntimeProbeError(
            "THREAD_PROVENANCE_MISMATCH",
            f"resume thread ID가 원래 ID와 다릅니다: {thread_id}",
        )
    if expected_session_id is not None and session_id != expected_session_id:
        raise RuntimeProbeError(
            "THREAD_PROVENANCE_MISMATCH",
            f"resume sessionId가 원래 sessionId와 다릅니다: {session_id}",
        )
    instruction_sources = _validate_instruction_sources(
        response.get("instructionSources"), expected_sources
    )
    return {
        "thread_id": thread_id,
        "session_id": session_id,
        "active_permission_profile": active_profile,
        "approval_policy": response.get("approvalPolicy"),
        "cwd": response.get("cwd"),
        "runtime_workspace_roots": roots,
        "model_id": response_model,
        "reasoning_effort": response.get("reasoningEffort"),
        "instruction_sources": list(instruction_sources),
        "response_digest": sha256_json(response),
    }


def _thread_turns(response: dict[str, Any]) -> list[dict[str, Any]]:
    thread = response.get("thread")
    turns = thread.get("turns") if isinstance(thread, dict) else None
    if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
        raise RuntimeProbeError(
            "INVALID_RPC_RESPONSE", "thread/read에서 turn 목록을 확인하지 못했습니다."
        )
    return turns


def _find_turn(response: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    return next(
        (turn for turn in _thread_turns(response) if turn.get("id") == turn_id),
        None,
    )


def _notification_to_dict(notification: Any) -> dict[str, Any]:
    if isinstance(notification, dict):
        return json_safe(notification)
    return {
        "method": getattr(notification, "method", "unknown"),
        "payload": json_safe(getattr(notification, "payload", None)),
    }


def _wait_for_turn_notification(
    session: AppServerSession,
    *,
    thread_id: str,
    turn_id: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    outcome: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
    methods: list[str] = []

    def consume() -> None:
        try:
            while True:
                event = _notification_to_dict(
                    session.next_turn_notification(turn_id)
                )
                method = event.get("method")
                if isinstance(method, str):
                    methods.append(method)
                if method == "turn/completed":
                    outcome.put(event)
                    return
        except BaseException as exc:
            outcome.put(exc)

    waiter = threading.Thread(
        target=consume,
        daemon=True,
        name=f"flowmarshal-r1-turn-{turn_id[-8:]}",
    )
    waiter.start()
    try:
        completed = outcome.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        try:
            _object(
                session,
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
            )
        except BaseException:
            pass
        raise RuntimeProbeError(
            "TURN_TIMEOUT",
            f"turn {turn_id}가 {timeout_seconds:g}초 안에 끝나지 않았습니다.",
        ) from exc
    finally:
        session.unregister_turn_notifications(turn_id)
    if isinstance(completed, BaseException):
        raise completed
    payload = completed.get("payload")
    completed_thread_id = payload.get("threadId") if isinstance(payload, dict) else None
    if completed_thread_id != thread_id:
        raise RuntimeProbeError(
            "THREAD_PROVENANCE_MISMATCH",
            "turn/completed의 thread ID가 요청 binding과 다릅니다.",
        )
    turn = payload.get("turn") if isinstance(payload, dict) else None
    if not isinstance(turn, dict) or turn.get("id") != turn_id:
        raise RuntimeProbeError(
            "INVALID_RPC_RESPONSE",
            "turn/completed에서 요청 turn을 확인하지 못했습니다.",
        )
    return turn, tuple(methods)


def _agent_output(turn: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    items = turn.get("items")
    if not isinstance(items, list):
        raise RuntimeProbeError(
            "TURN_OUTPUT_INVALID", "완료 turn의 item 목록을 확인하지 못했습니다."
        )
    item_types: list[str] = []
    messages: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if isinstance(item_type, str):
            item_types.append(item_type)
        if item_type == "agentMessage" and isinstance(item.get("text"), str):
            messages.append(item["text"])
    if not messages:
        raise RuntimeProbeError(
            "TURN_OUTPUT_INVALID", "완료 turn에서 agentMessage를 찾지 못했습니다."
        )
    return messages[-1], tuple(item_types)


def _validate_marker_output(text: str, marker: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeProbeError(
            "TURN_OUTPUT_INVALID", "Runner의 최종 응답이 유효한 JSON이 아닙니다."
        ) from exc
    if not isinstance(payload, dict) or payload != {
        "status": "R1_RUNTIME_OK",
        "marker": marker,
    }:
        raise RuntimeProbeError(
            "TURN_OUTPUT_INVALID", "Runner의 구조화 응답이 R1 marker 계약과 다릅니다."
        )
    return {
        "marker_matched": True,
        "output_digest": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _pass(
    result: dict[str, Any],
    name: str,
    summary: str,
    **evidence: Any,
) -> None:
    result["checks"][name] = {
        "status": "pass",
        "summary": summary,
        "evidence": json_safe(evidence),
    }


def run_r1_probe(
    *,
    session_factory: Callable[[], AppServerSession],
    workspace: Path,
    codex_home: Path,
    run_id: str,
    runtime_metadata: dict[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    marker: str | None = None,
) -> dict[str, Any]:
    """정상 사용자 권한에서 최소 Runner lifecycle을 실제로 검증한다."""

    workspace = workspace.resolve(strict=True)
    codex_home = codex_home.resolve(strict=True)
    expected_sources = (
        _global_instruction_file(codex_home),
        _project_instruction_file(workspace),
    )
    probe_marker = marker or f"r1-{uuid.uuid4()}"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "gate": GATE_NAME,
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": None,
        "decision": "NO-GO",
        "current_stage": "initialize",
        "environment": {
            "type": "local",
            "workspace": str(workspace),
            "codex_home": str(codex_home),
            **(runtime_metadata or {}),
        },
        "checks": {},
        "receipts": {},
        "error": None,
    }

    thread_id: str | None = None
    session_id: str | None = None
    selected_model: dict[str, Any] | None = None
    try:
        with session_factory() as first:
            result["receipts"]["initialize_first"] = {
                "response_digest": sha256_json(first.initialize_payload)
            }
            _pass(result, "initialize", "첫 App Server initialize/initialized가 완료됨")

            result["current_stage"] = "permission_preflight"
            policy = preflight_execution_policy(first, workspace)
            result["receipts"]["execution_policy"] = policy
            _pass(
                result,
                "permission_preflight",
                "task 생성 전에 로컬 full-access/never 유효 정책을 확인함",
                active_profile=policy["default_permissions"],
                approval_policy=policy["approval_policy"],
            )

            result["current_stage"] = "model_inventory"
            selected_model = select_probe_model(first)
            result["receipts"]["model_selection"] = selected_model
            _pass(
                result,
                "model_inventory",
                "model/list에서 현재 기본 모델과 지원 추론 수준을 선택함",
                model_id=selected_model["model_id"],
                reasoning_effort=selected_model["reasoning_effort"],
            )

            result["current_stage"] = "thread_start"
            thread_params = {
                "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                "approvalsReviewer": "user",
                "cwd": str(workspace),
                "developerInstructions": (
                    "이 task는 FlowMarshal R1 최소 Runtime 검사다. 파일, 명령, 네트워크와 "
                    "외부 도구를 사용하지 말고 요청된 구조화 응답만 반환한다. 프로젝트를 "
                    "수정하거나 다음 작업을 선택하지 않는다."
                ),
                "ephemeral": False,
                "model": selected_model["model_id"],
                "permissions": REQUIRED_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [str(workspace)],
                "serviceName": SERVICE_NAME,
            }
            if "sandbox" in thread_params:
                raise AssertionError("R1 thread/start에는 legacy sandbox를 보내지 않습니다.")
            started = _object(first, "thread/start", thread_params)
            start_receipt = _validate_thread_response(
                started,
                workspace=workspace,
                expected_sources=expected_sources,
                model_id=selected_model["model_id"],
            )
            start_receipt["request_digest"] = sha256_json(thread_params)
            result["receipts"]["thread_start"] = start_receipt
            thread_id = start_receipt["thread_id"]
            session_id = start_receipt["session_id"]
            _pass(
                result,
                "thread_start",
                "정상 사용자 권한에서 실제 Runner thread가 시작됨",
                thread_id=thread_id,
                session_id=session_id,
            )
            _pass(
                result,
                "instruction_sources",
                "전역·프로젝트 AGENTS.md가 정상 Runner 입력으로 로드됨",
                instruction_sources=start_receipt["instruction_sources"],
            )
            try:
                _object(
                    first,
                    "thread/name/set",
                    {"threadId": thread_id, "name": f"FlowMarshal R1 {run_id}"},
                )
                result["receipts"]["thread_name_set"] = True
            except BaseException as exc:
                result["receipts"]["thread_name_set"] = False
                result["receipts"]["thread_name_warning"] = str(exc)[:500]

            result["current_stage"] = "thread_turn_separation"
            before_turn = _object(
                first,
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            turns_before = _thread_turns(before_turn)
            if turns_before:
                raise RuntimeProbeError(
                    "THREAD_TURN_SEPARATION_FAILED",
                    "thread/start가 turn/start 전에 예기치 않은 turn을 만들었습니다.",
                )
            _pass(
                result,
                "thread_turn_separation",
                "thread/start와 turn/start가 분리되어 있음",
                turns_before=0,
            )

            result["current_stage"] = "turn_start"
            turn_params = {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": (
                            "도구를 사용하지 말고 다음 두 필드만 가진 JSON object로 답하세요. "
                            f"status는 R1_RUNTIME_OK, marker는 {probe_marker} 입니다."
                        ),
                    }
                ],
                "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                "approvalsReviewer": "user",
                "cwd": str(workspace),
                "effort": selected_model["reasoning_effort"],
                "model": selected_model["model_id"],
                "permissions": REQUIRED_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [str(workspace)],
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "const": "R1_RUNTIME_OK"},
                        "marker": {"type": "string", "const": probe_marker},
                    },
                    "required": ["status", "marker"],
                    "additionalProperties": False,
                },
            }
            turn_started = _object(first, "turn/start", turn_params)
            turn = turn_started.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise RuntimeProbeError(
                    "INVALID_RPC_RESPONSE", "turn/start 응답에서 turn ID를 찾지 못했습니다."
                )
            result["receipts"]["turn_start"] = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "model_id": selected_model["model_id"],
                "reasoning_effort": selected_model["reasoning_effort"],
                "permission_profile": REQUIRED_PERMISSION_PROFILE,
                "approval_policy": REQUIRED_APPROVAL_POLICY,
                "request_digest": sha256_json(turn_params),
                "response_digest": sha256_json(turn_started),
            }
            _pass(
                result,
                "turn_start",
                "실제 Runner turn이 시작됨",
                thread_id=thread_id,
                turn_id=turn_id,
            )

            result["current_stage"] = "thread_read"
            notification_turn, notification_methods = _wait_for_turn_notification(
                first,
                thread_id=thread_id,
                turn_id=turn_id,
                timeout_seconds=timeout_seconds,
            )
            read = _object(
                first,
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            completed_turn = _find_turn(read, turn_id)
            if completed_turn is None:
                raise RuntimeProbeError(
                    "THREAD_RECOVERY_FAILED",
                    "turn/completed 뒤 thread/read에서 같은 turn을 찾지 못했습니다.",
                )
            if completed_turn.get("status") != notification_turn.get("status"):
                raise RuntimeProbeError(
                    "THREAD_PROVENANCE_MISMATCH",
                    "turn/completed와 thread/read의 종료 상태가 다릅니다.",
                )
            if completed_turn.get("status") != "completed":
                raise RuntimeProbeError(
                    "TURN_NOT_COMPLETED",
                    f"Runner turn의 종료 상태가 completed가 아닙니다: {completed_turn.get('status')!r}",
                )
            output, item_types = _agent_output(completed_turn)
            marker_receipt = _validate_marker_output(output, probe_marker)
            result["receipts"]["thread_read"] = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn_status": completed_turn.get("status"),
                "notification_methods": list(notification_methods),
                "completion_notification_digest": sha256_json(notification_turn),
                "turn_count": len(_thread_turns(read)),
                "item_types": list(item_types),
                "read_response_digest": sha256_json(read),
                **marker_receipt,
            }
            _pass(
                result,
                "thread_read",
                "thread/read에서 완료 turn과 구조화 결과를 관측함",
                turn_id=turn_id,
                turn_status="completed",
                marker_matched=True,
            )
            result["receipts"]["first_session_unexpected_requests"] = list(
                first.unexpected_requests
            )

        result["current_stage"] = "thread_resume"
        with session_factory() as second:
            result["receipts"]["initialize_second"] = {
                "response_digest": sha256_json(second.initialize_payload)
            }
            persisted = _object(
                second,
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            persisted_thread = persisted.get("thread")
            if not isinstance(persisted_thread, dict):
                raise RuntimeProbeError(
                    "THREAD_RECOVERY_FAILED",
                    "새 App Server에서 기존 thread를 읽지 못했습니다.",
                )
            if (
                persisted_thread.get("id") != thread_id
                or persisted_thread.get("sessionId") != session_id
                or not _thread_turns(persisted)
            ):
                raise RuntimeProbeError(
                    "THREAD_RECOVERY_FAILED",
                    "재시작 후 thread/read 결과가 기존 binding과 다릅니다.",
                )

            resume_params = {
                "threadId": thread_id,
                "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                "approvalsReviewer": "user",
                "cwd": str(workspace),
                "model": selected_model["model_id"],
                "permissions": REQUIRED_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [str(workspace)],
            }
            if "sandbox" in resume_params:
                raise AssertionError("R1 thread/resume에는 legacy sandbox를 보내지 않습니다.")
            resumed = _object(second, "thread/resume", resume_params)
            resume_receipt = _validate_thread_response(
                resumed,
                workspace=workspace,
                expected_sources=expected_sources,
                model_id=selected_model["model_id"],
                expected_thread_id=thread_id,
                expected_session_id=session_id,
            )
            resume_receipt.update(
                {
                    "request_digest": sha256_json(resume_params),
                    "read_before_resume_digest": sha256_json(persisted),
                    "persisted_turn_count": len(_thread_turns(persisted)),
                }
            )
            result["receipts"]["thread_resume"] = resume_receipt
            result["receipts"]["second_session_unexpected_requests"] = list(
                second.unexpected_requests
            )
            _pass(
                result,
                "thread_resume",
                "새 App Server에서 같은 thread/session binding을 읽고 재개함",
                thread_id=thread_id,
                session_id=session_id,
                persisted_turn_count=resume_receipt["persisted_turn_count"],
            )

        result["decision"] = "GO"
        result["current_stage"] = "completed"
    except BaseException as exc:
        code = exc.code if isinstance(exc, RuntimeProbeError) else "RUNTIME_ERROR"
        result["error"] = {
            "code": code,
            "type": type(exc).__name__,
            "message": str(exc)[:2000],
            "stage": result["current_stage"],
        }
        result["checks"].setdefault(
            result["current_stage"],
            {
                "status": "fail",
                "summary": str(exc)[:500],
                "evidence": {"code": code},
            },
        )
    finally:
        result["completed_at"] = utc_now()
    return result


class _NonInteractiveHandler:
    """R1이 의도하지 않은 상호작용을 기다리지 않도록 즉시 거절한다."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        del params
        self.requests.append(method)
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "decline"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn", "strictAutoReview": True}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline", "content": None}
        return {}


class LiveAppServerSession:
    """시스템 Codex App Server의 작은 stdio adapter."""

    def __init__(self, *, codex_bin: Path, codex_home: Path, workspace: Path) -> None:
        from openai_codex.client import CodexClient, CodexConfig

        self._handler = _NonInteractiveHandler()
        self._client = CodexClient(
            CodexConfig(
                codex_bin=str(codex_bin),
                cwd=str(workspace),
                env={"CODEX_HOME": str(codex_home)},
                client_name="flowmarshal",
                client_title="FlowMarshal",
                client_version="0.1.0",
                experimental_api=True,
            ),
            approval_handler=self._handler,
        )
        self.initialize_payload: Any = None

    @property
    def unexpected_requests(self) -> tuple[str, ...]:
        return tuple(self._handler.requests)

    def __enter__(self) -> "LiveAppServerSession":
        self._client.start()
        self.initialize_payload = json_safe(self._client.initialize())
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        result = self._client._request_raw(method, params)  # noqa: SLF001
        if method == "turn/start":
            document = json_safe(result)
            turn = document.get("turn") if isinstance(document, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if isinstance(turn_id, str):
                self._client.register_turn_notifications(turn_id)
        return result

    def next_turn_notification(self, turn_id: str) -> Any:
        return self._client.next_turn_notification(turn_id)

    def unregister_turn_notifications(self, turn_id: str) -> None:
        self._client.unregister_turn_notifications(turn_id)


def _runtime_version(codex_bin: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(codex_bin), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    return {
        "executable": str(codex_bin),
        "exit_code": completed.returncode,
        "version": completed.stdout.strip(),
        "stderr": completed.stderr.strip()[:500],
    }


def render_report(result: dict[str, Any]) -> str:
    decision = result.get("decision", "UNKNOWN")
    checks = result.get("checks", {})
    lines = [
        "# FlowMarshal R1 최소 Runtime 재검증",
        "",
        f"- 판정: **{decision}**",
        f"- 실행 ID: `{result.get('run_id', '-')}`",
        f"- 시작: `{result.get('started_at', '-')}`",
        f"- 완료: `{result.get('completed_at', '-')}`",
        "",
        "## 검사 결과",
        "",
        "| 검사 | 상태 | 설명 |",
        "|---|---|---|",
    ]
    for name, check in checks.items():
        lines.append(
            f"| `{name}` | {check.get('status', 'unknown')} | "
            f"{check.get('summary', '-')} |"
        )
    thread = result.get("receipts", {}).get("thread_start", {})
    if thread:
        lines.extend(
            [
                "",
                "## Runtime binding",
                "",
                f"- thread ID: `{thread.get('thread_id', '-')}`",
                f"- session ID: `{thread.get('session_id', '-')}`",
                f"- 모델: `{thread.get('model_id', '-')}`",
                f"- active permission profile: `{thread.get('active_permission_profile', '-')}`",
                f"- approval policy: `{thread.get('approval_policy', '-')}`",
                "- instruction sources:",
            ]
        )
        lines.extend(
            f"  - `{source}`" for source in thread.get("instruction_sources", [])
        )
    error = result.get("error")
    if error:
        lines.extend(
            [
                "",
                "## 실패",
                "",
                f"- 코드: `{error.get('code', '-')}`",
                f"- 단계: `{error.get('stage', '-')}`",
                f"- 원인: {error.get('message', '-')}",
            ]
        )
    lines.extend(
        [
            "",
            "## 범위",
            "",
            "이 Gate는 정상 로컬 권한에서 `thread/start → turn/start → thread/read → thread/resume`과 instruction provenance만 검사한다. localhost 차단이나 별도 파일 샌드박스는 판정에 포함하지 않는다.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(output_dir: Path, result: dict[str, Any]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt_path = output_dir / "runtime-receipt.json"
    report_path = output_dir / "runtime-report.md"
    receipt_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path.write_text(render_report(result), encoding="utf-8")
    return receipt_path, report_path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"r1-{stamp}-{uuid.uuid4().hex[:8]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FlowMarshal R1 최소 Codex Runtime lifecycle을 실제로 검증합니다."
    )
    parser.add_argument("--project-root", type=Path, default=_project_root())
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--codex-bin", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    workspace = args.project_root.resolve(strict=True)
    codex_home = (
        args.codex_home
        or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    ).resolve(strict=True)
    raw_bin = args.codex_bin or shutil.which("codex")
    if raw_bin is None:
        print("시스템 Codex 실행 파일을 찾을 수 없습니다.", file=sys.stderr)
        return 2
    codex_bin = Path(raw_bin).resolve(strict=True)
    run_id = args.run_id or _default_run_id()
    output_dir = args.output_dir or (
        workspace / "spikes" / "orchestration" / "r1" / "artifacts" / "runs" / run_id
    )
    runtime_metadata = _runtime_version(codex_bin)
    try:
        import openai_codex

        runtime_metadata["sdk_version"] = getattr(openai_codex, "__version__", "unknown")
    except ImportError:
        runtime_metadata["sdk_version"] = "unavailable"

    factory = lambda: LiveAppServerSession(  # noqa: E731
        codex_bin=codex_bin,
        codex_home=codex_home,
        workspace=workspace,
    )
    result = run_r1_probe(
        session_factory=factory,
        workspace=workspace,
        codex_home=codex_home,
        run_id=run_id,
        runtime_metadata=runtime_metadata,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        receipt_path, report_path = write_artifacts(output_dir, result)
    except FileExistsError:
        print(f"artifact 디렉터리가 이미 존재합니다: {output_dir}", file=sys.stderr)
        return 2
    print(f"R1 판정: {result['decision']}")
    print(f"receipt: {receipt_path}")
    print(f"report: {report_path}")
    thread_id = result.get("receipts", {}).get("thread_start", {}).get("thread_id")
    if thread_id:
        print(f"thread: {thread_id}")
    if result.get("error"):
        print(
            f"error: {result['error']['code']} - {result['error']['message']}",
            file=sys.stderr,
        )
    return 0 if result["decision"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
