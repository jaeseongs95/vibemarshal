from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import openai_codex
from codex_cli_bin import bundled_codex_path
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.errors import JsonRpcError
from pydantic import BaseModel

from flowmarshal.windows_sandbox import (
    AUTHORITATIVE_HOME_INVALID,
    HOST_PROVISIONING_FORBIDDEN,
    HOST_RUNTIME_NOT_SYSTEM,
    SANDBOX_HOME_CROSS_CONTAMINATION,
    SANDBOX_STATE_CHANGED_DURING_PROBE,
    SandboxContractError,
    SandboxFingerprint,
    SandboxHostContext,
    artifact_safety_violations,
    capture_sandbox_fingerprint,
    capture_windows_vm_identity,
    default_authoritative_home,
    discover_system_codex as discover_host_system_codex,
    disposable_vm_confirmed,
    fingerprint_is_complete,
    fingerprints_match,
    manual_setup_command,
    query_windows_sandbox_status,
    resolve_host_context,
)
from flowmarshal.vm_sandbox_suite import (
    VM_SCENARIOS,
    VM_SCENARIO_NOT_CLEAN,
    VM_SUITE_INVALID,
    VM_SUITE_SCHEMA_VERSION,
    VM_TEST_INJECTED_SETUP_FAILURE,
    VmSuiteError,
    evaluate_vm_suite,
    fingerprint_is_clean,
    load_attempts,
    read_json_object,
    scenario_root as vm_scenario_root,
    suite_root as vm_suite_root,
    validate_attempt_request,
    validate_component,
    write_json_object,
)


SCHEMA_VERSION = "1.3"
AUDIT_SCHEMA_VERSION = "1.0"
PERMISSION_RECHECK_SCHEMA_VERSION = "1.0"
PERMISSION_RECHECK_KIND = "fm_0a_p_permission_recheck"
SYSTEM_RUNTIME_NAME = "desktop-system"
GATE_NAME = "0A"
PROJECT_NAME = "FlowMarshal"
DEFAULT_TURN_TIMEOUT_SECONDS = 180
PERMISSION_PROFILE_ID = "flowmarshal_gate0a"
UNDECLARED_READ_EXCEPTION_ID = "native-windows-read-boundary-20260901"
UNDECLARED_READ_REVIEW_TRIGGER = (
    "지원 Codex 런타임 변경 또는 미등록 외부 읽기 카나리의 strict_read_isolation 전환"
)

BASE_CONFIG_OVERRIDES = (
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

RUNTIME_HARD_CHECKS = (
    "initialize",
    "thread_turn_separation",
    "turn_execution",
    "persisted_thread_listed",
    "resume_after_restart",
    "interrupt",
)

DESKTOP_INTEROP_HARD_CHECKS = (
    "project_grouping_correct",
    "sdk_resume_visible_in_desktop",
    "desktop_turn_visible_to_sdk",
    "desktop_thread_sdk_read_resume",
    "concurrent_access_consistent",
)

PERMISSION_HARD_CHECKS = (
    "permission_configuration",
    "windows_elevated_sandbox",
    "windows_sandbox_operational",
    "permission_profile_supported",
    "workspace_read_allowed",
    "workspace_write_allowed",
    "declared_reference_read_allowed",
    "declared_reference_write_blocked",
    "undeclared_path_read_policy",
    "undeclared_path_write_blocked",
    "protected_path_read_blocked",
    "protected_path_write_blocked",
    "control_file_read_allowed",
    "control_file_write_blocked",
    "command_network_blocked",
    "external_surfaces_disabled",
    "approvals_fail_closed",
    "sandbox_state_unchanged",
    "artifact_safety",
)

# 이전 코드가 import하던 이름은 런타임 하위 게이트의 의미로만 유지한다.
HARD_RUNTIME_CHECKS = RUNTIME_HARD_CHECKS

LEGACY_PERMISSION_CHECKS = (
    "restricted_project_outside_read",
    "restricted_localappdata_read",
    "outside_project_write_blocked",
    "localappdata_write_blocked",
)

LEGACY_SANDBOX_KEYS = {
    "sandbox_mode",
    "sandboxMode",
    "sandbox_workspace_write",
    "sandboxWorkspaceWrite",
}

APPROVAL_METHODS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
)

SECURITY_FEATURE_NAMES = (
    "web_search_request",
    "standalone_web_search",
    "apps",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "multi_agent",
    "in_app_browser",
    "browser_use",
    "browser_use_full_cdp_access",
    "browser_use_external",
    "computer_use",
    "plugin_sharing",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def artifact_root(root: Path) -> Path:
    return root / "spikes" / "gate0a" / "artifacts"


def audit_root(root: Path) -> Path:
    return artifact_root(root) / "audit"


def results_path(root: Path) -> Path:
    return artifact_root(root) / "gate0a-results.json"


def report_path(root: Path) -> Path:
    return artifact_root(root) / "gate0a-report.md"


def interop_root(root: Path) -> Path:
    return artifact_root(root) / "interop"


def permission_recheck_root(root: Path) -> Path:
    return artifact_root(root) / "permission-rechecks"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_artifact_component(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "unknown")).strip("._-")
    return cleaned or "unknown"


def load_evidence_audits(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    directory = audit_root(root)
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            records.append(
                {
                    "audit_id": path.stem,
                    "status": "invalid",
                    "path": path.relative_to(root).as_posix(),
                    "error": exception_record(exc),
                }
            )
            continue
        records.append(
            {
                "audit_id": record.get("audit_id", path.stem),
                "status": record.get("status", "recorded"),
                "summary": record.get("summary"),
                "path": path.relative_to(root).as_posix(),
                "affected_run_id": record.get("affected_run_id"),
                "source_schema_version": record.get("source_schema_version"),
                "preimage_available": record.get("preimage_available"),
                "kind": record.get("kind"),
                "reason_code": record.get("reason_code"),
                "gate_override": record.get("gate_override"),
            }
        )
    return records


def load_interop_attempts(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    directory = interop_root(root)
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*/attempt-result.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            records.append(
                {
                    "verification_id": path.parent.name,
                    "status": "invalid",
                    "path": path.relative_to(root).as_posix(),
                    "error": exception_record(exc),
                }
            )
            continue
        records.append(
            {
                "verification_id": record.get("verification_id", path.parent.name),
                "status": record.get("status", "unknown"),
                "checked_at": record.get("checked_at"),
                "runtime_name": nested_get(record, "runtime", "name"),
                "runtime_version": nested_get(record, "runtime", "version", "stdout"),
                "error_type": nested_get(record, "error", "type"),
                "error_message": nested_get(record, "error", "message"),
                "path": path.relative_to(root).as_posix(),
            }
        )
    return records


def load_permission_rechecks(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    directory = permission_recheck_root(root)
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*/attempt-result.json")):
        try:
            payload = path.read_bytes()
            record = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            records.append(
                {
                    "recheck_id": path.parent.name,
                    "status": "invalid",
                    "path": path.relative_to(root).as_posix(),
                    "error": exception_record(exc),
                }
            )
            continue
        hard_check_statuses = {
            name: nested_get(record, "checks", name, "status")
            for name in PERMISSION_HARD_CHECKS
        }
        records.append(
            {
                "recheck_id": record.get("recheck_id", path.parent.name),
                "status": record.get("status", "unknown"),
                "checked_at": record.get("checked_at"),
                "schema_version": record.get("schema_version"),
                "kind": record.get("kind"),
                "source_gate_run_id": record.get("source_gate_run_id"),
                "runtime_name": nested_get(record, "runtime", "name"),
                "runtime_version": nested_get(record, "runtime", "version", "stdout"),
                "runtime_executable": nested_get(record, "runtime", "executable"),
                "hard_check_statuses": hard_check_statuses,
                "hard_checks_complete": all(
                    status == "pass" for status in hard_check_statuses.values()
                ),
                "temporary_exception_used": nested_get(
                    record,
                    "checks",
                    "undeclared_path_read_policy",
                    "evidence",
                    "temporary_exception_used",
                )
                is True,
                "secret_contents_read": nested_get(
                    record,
                    "evidence",
                    "sandbox_state_fingerprint",
                    "secret_contents_read",
                ),
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_bytes(payload),
            }
        )
    return records


def check(status: str, summary: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "summary": summary, "evidence": evidence}


def permission_policy_contract() -> dict[str, Any]:
    """현재 승인된 외부 읽기 호환성 정책을 직렬화 가능한 형태로 반환한다."""

    return {
        "policy_version": "1.0",
        "target_mode": "strict_registered_read",
        "configured_default": "deny",
        "temporary_exception": {
            "id": UNDECLARED_READ_EXCEPTION_ID,
            "status": "allowed_only_when_native_windows_canary_confirms_broad_read",
            "approved_on": "2026-09-01",
            "reason": (
                "native Windows permission profile의 :root=deny가 미등록 외부 경로의 "
                "상속 읽기 권한을 현재 차단하지 못하는 런타임 결함"
            ),
            "review_trigger": UNDECLARED_READ_REVIEW_TRIGGER,
        },
        "retained_hard_boundaries": [
            "workspace_outside_write_blocked",
            "protected_path_read_write_blocked",
            "command_network_blocked",
            "external_tool_surfaces_disabled",
        ],
        "retained_future_controls": [
            "root_deny_configuration",
            "registered_reference_read_grant",
            "undeclared_read_canary",
        ],
    }


def exception_record(exc: BaseException) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc)[:2000],
    }
    if isinstance(exc, JsonRpcError):
        record["code"] = exc.code
        record["rpc_message"] = exc.message
        record["data"] = json_safe(exc.data)
    return record


def json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value):
        return {field.name: json_safe(getattr(value, field.name)) for field in dataclasses.fields(value)}
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


def redact_tokens(value: Any, tokens: Iterable[str]) -> Any:
    redacted = json_safe(value)
    token_list = [token for token in tokens if token]

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            result = item
            for token in token_list:
                result = result.replace(token, "<CANARY_TOKEN_REDACTED>")
            return result
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()}
        return item

    return visit(redacted)


class FailClosedApprovalHandler:
    """App Server가 요청하는 모든 권한 확대와 입력을 거절한다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []

    def __call__(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            self.requests.append({"method": method, "received_at": utc_now()})
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "decline"}
        if method == "item/permissions/requestApproval":
            return {
                "permissions": {},
                "scope": "turn",
                "strictAutoReview": True,
            }
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline", "content": None}
        return {}


def validate_fail_closed_handler(handler: FailClosedApprovalHandler) -> dict[str, Any]:
    responses = {method: handler(method, {}) for method in APPROVAL_METHODS}
    expected = {
        "item/commandExecution/requestApproval": {"decision": "decline"},
        "item/fileChange/requestApproval": {"decision": "decline"},
        "item/permissions/requestApproval": {
            "permissions": {},
            "scope": "turn",
            "strictAutoReview": True,
        },
        "item/tool/requestUserInput": {"answers": {}},
        "mcpServer/elicitation/request": {"action": "decline", "content": None},
    }
    passed = responses == expected
    return check(
        "pass" if passed else "fail",
        "모든 알려진 서버 승인/입력 요청이 명시적 거절 응답으로 매핑됨"
        if passed
        else "fail-closed 응답 매핑이 예상과 다름",
        responses=responses,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class RuntimeSpec:
    name: str
    executable: Path
    role: str


@dataclasses.dataclass(frozen=True, slots=True)
class CanaryLayout:
    """권한 primitive 검사용 합성 경로. 생성은 preflight 통과 뒤에만 수행한다."""

    workspace: Path
    declared_reference_root: Path
    undeclared_root: Path
    protected_root: Path
    control_file: Path
    permission_home: Path


def canary_layout(
    root: Path,
    run_id: str,
    runtime_name: str,
    authoritative_codex_home: Path | None = None,
) -> CanaryLayout:
    run_root = artifact_root(root) / "runs" / run_id / runtime_name
    workspace = run_root / "workspace"
    outside_root = root.parent / "flowmarshal-gate0a-canary" / run_id / runtime_name
    drive_canary_root = (
        Path(root.anchor)
        / "FlowMarshal-Gate0A-Canary"
        / run_id
        / runtime_name
    )
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(root.parent)))
    protected_root = local_app_data / "FlowMarshal" / "Gate0A" / run_id / runtime_name
    return CanaryLayout(
        workspace=workspace,
        declared_reference_root=outside_root / "declared-reference",
        undeclared_root=drive_canary_root / "undeclared",
        protected_root=protected_root,
        control_file=workspace / "flowmarshal.toml",
        permission_home=authoritative_codex_home or default_authoritative_home(),
    )


def toml_quote(value: Path | str) -> str:
    """config override에서 사용할 TOML 기본 문자열을 안전하게 만든다."""

    return json.dumps(str(value), ensure_ascii=False)


def build_config_overrides(layout: CanaryLayout) -> tuple[str, ...]:
    filesystem = ",".join(
        (
            '":root"="deny"',
            '":minimal"="read"',
            '":workspace_roots"={"."="write","flowmarshal.toml"="read"}',
            f"{toml_quote(layout.declared_reference_root)}=\"read\"",
            f"{toml_quote(layout.protected_root)}=\"deny\"",
            f"{toml_quote(layout.permission_home)}=\"deny\"",
            '":tmpdir"="deny"',
            '":slash_tmp"="deny"',
        )
    )
    return (
        *BASE_CONFIG_OVERRIDES,
        f'default_permissions="{PERMISSION_PROFILE_ID}"',
        f'permissions.{PERMISSION_PROFILE_ID}.extends=":workspace"',
        f"permissions.{PERMISSION_PROFILE_ID}.filesystem={{{filesystem}}}",
        f"permissions.{PERMISSION_PROFILE_ID}.network.enabled=false",
        'windows.sandbox="elevated"',
    )


def discover_system_codex() -> Path | None:
    return discover_host_system_codex()


def runtime_specs(selection: str) -> list[RuntimeSpec]:
    pinned = Path(bundled_codex_path()).resolve()
    specs = [RuntimeSpec("sdk-pinned", pinned, "openai-codex 패키지 동봉 런타임")]
    system = discover_system_codex()
    if system is not None and system.resolve() != pinned:
        specs.append(RuntimeSpec("desktop-system", system.resolve(), "Codex Desktop/시스템 런타임"))
    if selection == "pinned":
        return [specs[0]]
    if selection == "system":
        return [spec for spec in specs if spec.name == "desktop-system"]
    return specs


def runtime_version(executable: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except BaseException as exc:
        return {"error": exception_record(exc)}


def make_client(
    spec: RuntimeSpec,
    cwd: Path,
    handler: FailClosedApprovalHandler,
    config_overrides: tuple[str, ...],
    env: dict[str, str] | None = None,
) -> CodexClient:
    config = CodexConfig(
        codex_bin=str(spec.executable),
        config_overrides=config_overrides,
        cwd=str(cwd),
        env=env,
        client_name="flowmarshal_gate0a",
        client_title="FlowMarshal Gate 0A",
        client_version="0.1.0",
        experimental_api=True,
    )
    return CodexClient(config=config, approval_handler=handler)


def ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def command_exec(
    client: CodexClient,
    cwd: Path,
    script: str,
    permission_profile: str,
    timeout_ms: int = 20_000,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = client._request_raw(  # noqa: SLF001 - Gate 0A는 원시 프로토콜 호환성 검증이다.
            "command/exec",
            {
                "command": [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                "cwd": str(cwd),
                "permissionProfile": permission_profile,
                "timeoutMs": timeout_ms,
            },
        )
        return {
            "rpc_ok": True,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "response": json_safe(response),
        }
    except BaseException as exc:
        return {
            "rpc_ok": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "error": exception_record(exc),
        }


def command_succeeded(result: dict[str, Any]) -> bool:
    response = result.get("response")
    return bool(result.get("rpc_ok") and isinstance(response, dict) and response.get("exitCode") == 0)


def command_output(result: dict[str, Any]) -> str:
    response = result.get("response")
    if not isinstance(response, dict):
        return ""
    return f"{response.get('stdout', '')}\n{response.get('stderr', '')}"


def write_canary(path: Path, prefix: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{prefix}-{uuid.uuid4()}"
    path.write_text(token, encoding="utf-8")
    return token


def sanitize_command_result(result: dict[str, Any], tokens: Iterable[str]) -> dict[str, Any]:
    return redact_tokens(result, tokens)


def read_effective_config(client: CodexClient, cwd: Path) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "result": json_safe(
                client._request_raw(  # noqa: SLF001 - Gate 0A는 원시 프로토콜 검증이다.
                    "config/read",
                    {"cwd": str(cwd), "includeLayers": True},
                )
            ),
        }
    except BaseException as exc:
        return {"ok": False, "error": exception_record(exc)}


def build_mcp_disable_overrides(
    observation: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """현재 config의 MCP 서버를 다음 세션 시작 전에 전부 명시적으로 끈다."""

    if not observation.get("ok"):
        return (
            check(
                "error",
                "MCP 비활성화 preflight를 위해 유효 설정을 읽지 못함",
                error=observation.get("error"),
            ),
            (),
        )
    config = nested_get(observation, "result", "config") or {}
    servers = config.get("mcp_servers", {}) if isinstance(config, dict) else {}
    if not isinstance(servers, dict):
        return check("error", "mcp_servers 설정 형식이 예상과 다름"), ()
    names = sorted(str(name) for name in servers)
    unsafe = [name for name in names if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None]
    if unsafe:
        return (
            check(
                "fail",
                "안전한 config override로 비활성화할 수 없는 MCP 서버 이름이 있음",
                unsafe_server_names=unsafe,
            ),
            (),
        )
    overrides = tuple(f"mcp_servers.{name}.enabled=false" for name in names)
    return (
        check(
            "pass",
            "다음 runtime 세션에서 모든 구성 MCP 서버를 비활성화하도록 고정함",
            server_names=names,
            override_count=len(overrides),
        ),
        overrides,
    )


def find_named_key_paths(value: Any, names: set[str], prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in names:
                found.append(path)
            found.extend(find_named_key_paths(child, names, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_named_key_paths(child, names, f"{prefix}[{index}]"))
    return found


def find_explicit_legacy_paths(payload: Any) -> list[str]:
    """config/read의 실제 설정 레이어에 명시된 구형 sandbox 키만 찾는다."""

    layers = payload.get("layers", []) if isinstance(payload, dict) else []
    found: list[str] = []
    if isinstance(layers, list):
        for index, layer in enumerate(layers):
            layer_config = layer.get("config", {}) if isinstance(layer, dict) else {}
            found.extend(
                find_named_key_paths(
                    layer_config,
                    LEGACY_SANDBOX_KEYS,
                    f"layers[{index}].config",
                )
            )
    return sorted(set(found))


def evaluate_permission_configuration(observation: dict[str, Any]) -> dict[str, Any]:
    if not observation.get("ok"):
        return check(
            "error",
            "유효 Codex 설정을 읽지 못해 permission profile preflight를 완료할 수 없음",
            error=observation.get("error"),
        )

    payload = observation.get("result", {})
    config = payload.get("config", {}) if isinstance(payload, dict) else {}
    # 최종 계산 config에는 미설정 legacy 필드도 null로 나타난다. 실제 충돌은
    # config/read가 돌려준 개별 설정 레이어의 명시적 키로만 판정한다.
    legacy_paths = find_explicit_legacy_paths(payload)
    default_permissions = (
        config.get("defaultPermissions", config.get("default_permissions"))
        if isinstance(config, dict)
        else None
    )
    permissions = config.get("permissions", {}) if isinstance(config, dict) else {}
    profile_present = isinstance(permissions, dict) and PERMISSION_PROFILE_ID in permissions

    if legacy_paths:
        return check(
            "invalid_configuration",
            "구형 sandbox 설정과 permission profile이 함께 로드되어 권한 판정을 중단함",
            legacy_key_paths=legacy_paths,
            expected_default_permissions=PERMISSION_PROFILE_ID,
            actual_default_permissions=default_permissions,
        )
    if default_permissions != PERMISSION_PROFILE_ID or not profile_present:
        return check(
            "invalid_configuration",
            "요청한 permission profile이 유효 설정으로 확인되지 않음",
            legacy_key_paths=[],
            expected_default_permissions=PERMISSION_PROFILE_ID,
            actual_default_permissions=default_permissions,
            profile_present=profile_present,
        )
    return check(
        "pass",
        "permission profile 전용 설정이 확인됨",
        legacy_key_paths=[],
        default_permissions=default_permissions,
        profile_present=True,
    )


def evaluate_allowed_read_check(
    label: str,
    result: dict[str, Any],
    token: str,
    policy_supported: bool,
    known_tokens: Iterable[str],
) -> dict[str, Any]:
    exposed = token in command_output(result)
    sanitized = sanitize_command_result(result, known_tokens)
    if not policy_supported:
        return check(
            "unsupported",
            f"{label}: permission profile을 사용할 수 없어 읽기 허용을 검증하지 못함",
            token_exposed=exposed,
            command=sanitized,
        )
    allowed = command_succeeded(result) and exposed
    return check(
        "pass" if allowed else "fail",
        f"{label}: 승인된 읽기가 허용됨" if allowed else f"{label}: 승인된 읽기가 실패함",
        token_exposed=exposed,
        command=sanitized,
    )


def evaluate_read_check(
    label: str,
    result: dict[str, Any],
    token: str,
    policy_supported: bool,
    known_tokens: Iterable[str],
) -> dict[str, Any]:
    exposed = token in command_output(result)
    sanitized = sanitize_command_result(result, known_tokens)
    if not policy_supported:
        return check(
            "unsupported",
            f"{label}: 런타임이 제한 읽기 정책을 수락하지 않아 차단을 보장할 수 없음",
            token_exposed=exposed,
            command=sanitized,
        )
    blocked = not command_succeeded(result) and not exposed
    return check(
        "pass" if blocked else "fail",
        f"{label}: 미승인 읽기가 차단됨" if blocked else f"{label}: 읽기 경계가 깨짐",
        token_exposed=exposed,
        command=sanitized,
    )


def evaluate_undeclared_read_policy(
    label: str,
    result: dict[str, Any],
    token: str,
    policy_supported: bool,
    known_tokens: Iterable[str],
) -> dict[str, Any]:
    """미등록 외부 읽기의 엄격 모드 또는 승인된 Windows 임시 예외를 판정한다."""

    exposed = token in command_output(result)
    sanitized = sanitize_command_result(result, known_tokens)
    common_evidence = {
        "token_exposed": exposed,
        "exception_id": UNDECLARED_READ_EXCEPTION_ID,
        "review_trigger": UNDECLARED_READ_REVIEW_TRIGGER,
        "configured_default": "deny",
        "command": sanitized,
    }
    if not policy_supported:
        return check(
            "unsupported",
            f"{label}: permission profile을 사용할 수 없어 읽기 정책을 검증하지 못함",
            mode="unverified",
            strict_boundary_enforced=False,
            temporary_exception_used=False,
            **common_evidence,
        )

    response = result.get("response")
    output = command_output(result).casefold()
    denial_markers = (
        "access is denied",
        "access denied",
        "permission denied",
        "permissiondenied",
        "unauthorizedaccess",
        "액세스가 거부",
        "접근이 거부",
        "권한이 없습니다",
        "사용 권한이 없습니다",
    )
    strict_blocked = bool(
        result.get("rpc_ok")
        and isinstance(response, dict)
        and response.get("exitCode") != 0
        and not exposed
        and any(marker in output for marker in denial_markers)
    )
    compatibility_read = command_succeeded(result) and exposed

    if strict_blocked:
        return check(
            "pass",
            f"{label}: 엄격 읽기 차단이 실제로 집행됨",
            mode="strict_read_isolation",
            strict_boundary_enforced=True,
            temporary_exception_used=False,
            **common_evidence,
        )
    if compatibility_read:
        return check(
            "pass",
            f"{label}: Windows 읽기 차단 결함에 대한 승인된 임시 호환성 예외를 적용함",
            mode="temporary_native_windows_broad_read",
            strict_boundary_enforced=False,
            temporary_exception_used=True,
            **common_evidence,
        )
    return check(
        "fail",
        f"{label}: 엄격 차단과 임시 호환성 읽기 중 어느 상태인지 판별하지 못함",
        mode="ambiguous_failure",
        strict_boundary_enforced=False,
        temporary_exception_used=False,
        **common_evidence,
    )


def evaluate_write_check(
    label: str,
    result: dict[str, Any],
    target: Path,
    policy_supported: bool,
) -> dict[str, Any]:
    if not policy_supported:
        return check(
            "unsupported",
            f"{label}: 제한 permission profile을 사용할 수 없어 쓰기 차단을 보장할 수 없음",
            target_exists=target.exists(),
            command=result,
        )
    target_exists = target.exists()
    blocked = not target_exists and not command_succeeded(result)
    return check(
        "pass" if blocked else "fail",
        f"{label}: 쓰기가 차단됨" if blocked else f"{label}: 쓰기 경계가 깨짐",
        target_exists=target_exists,
        command=result,
    )


def evaluate_existing_write_blocked_check(
    label: str,
    result: dict[str, Any],
    target: Path,
    original: str,
    policy_supported: bool,
    known_tokens: Iterable[str],
) -> dict[str, Any]:
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    unchanged = current == original
    sanitized = sanitize_command_result(result, known_tokens)
    if not policy_supported:
        return check(
            "unsupported",
            f"{label}: permission profile을 사용할 수 없어 쓰기 차단을 검증하지 못함",
            unchanged=unchanged,
            command=sanitized,
        )
    blocked = unchanged and not command_succeeded(result)
    return check(
        "pass" if blocked else "fail",
        f"{label}: 읽기 전용 파일 수정이 차단됨" if blocked else f"{label}: 읽기 전용 경계가 깨짐",
        unchanged=unchanged,
        command=sanitized,
    )


def inspect_permission_profile(
    client: CodexClient,
    workspace: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    try:
        inventory = json_safe(
            client._request_raw(  # noqa: SLF001 - Gate 0A는 원시 프로토콜 검증이다.
                "permissionProfile/list",
                {"cwd": str(workspace), "limit": 100},
            )
        )
    except BaseException as exc:
        inventory = {"error": exception_record(exc)}
    profiles = inventory.get("data", []) if isinstance(inventory, dict) else []
    entry = next(
        (
            item
            for item in profiles
            if isinstance(item, dict) and item.get("id") == PERMISSION_PROFILE_ID
        ),
        None,
    )
    available = isinstance(entry, dict) and entry.get("allowed") is True
    outcome = check(
        "pass" if available else "unsupported",
        "permission profile이 런타임에 등록되고 허용됨"
        if available
        else "permission profile을 런타임에서 찾지 못했거나 허용되지 않음",
        profile_id=PERMISSION_PROFILE_ID,
        profile_entry=entry,
    )
    return outcome, inventory, entry


def run_sandbox_checks(
    client: CodexClient,
    layout: CanaryLayout,
    profile_inventory: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[str]]:
    workspace = layout.workspace
    inside_read = workspace / "inside-read.txt"
    declared_read = layout.declared_reference_root / "reference-read.txt"
    undeclared_read = layout.undeclared_root / "undeclared-read.txt"
    protected_read = layout.protected_root / "protected-read.txt"
    inside_token = write_canary(inside_read, "FM-IN")
    declared_token = write_canary(declared_read, "FM-REF")
    undeclared_token = write_canary(undeclared_read, "FM-UNDECLARED")
    protected_token = write_canary(protected_read, "FM-PROTECTED")
    control_token = write_canary(layout.control_file, "FM-CONTROL")
    tokens = [inside_token, declared_token, undeclared_token, protected_token, control_token]

    if profile_inventory is None:
        _, profile_inventory, _ = inspect_permission_profile(client, workspace)
    profiles = profile_inventory.get("data", []) if isinstance(profile_inventory, dict) else []
    profile_entry = next(
        (
            item
            for item in profiles
            if isinstance(item, dict) and item.get("id") == PERMISSION_PROFILE_ID
        ),
        None,
    )
    profile_available = isinstance(profile_entry, dict) and profile_entry.get("allowed") is True

    if profile_available:
        inside_read_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Get-Content -LiteralPath {ps_quote(inside_read)} -Raw",
            PERMISSION_PROFILE_ID,
        )
    else:
        inside_read_result = {
            "rpc_ok": False,
            "error": {
                "type": "PermissionProfileUnavailable",
                "message": f"{PERMISSION_PROFILE_ID} profile을 목록에서 찾지 못했거나 허용되지 않음",
            },
        }
    sandbox_operational = profile_available and inside_read_result.get("rpc_ok") is True
    policy_supported = sandbox_operational

    checks: dict[str, dict[str, Any]] = {}
    checks["permission_profile_supported"] = check(
        "pass" if profile_available else "unsupported",
        "permission profile이 런타임에 등록되고 허용됨"
        if profile_available
        else "permission profile을 런타임에서 찾지 못했거나 허용되지 않음",
        profile_id=PERMISSION_PROFILE_ID,
        profile_entry=profile_entry,
    )
    checks["windows_sandbox_operational"] = check(
        "pass" if sandbox_operational else "fail",
        "elevated Windows sandbox에서 command/exec 프로세스를 시작함"
        if sandbox_operational
        else "elevated Windows sandbox가 command/exec 프로세스를 시작하지 못함",
        command=sanitize_command_result(inside_read_result, tokens),
    )
    checks["workspace_read_allowed"] = evaluate_allowed_read_check(
        "작업 루트", inside_read_result, inside_token, policy_supported, tokens
    )

    if policy_supported:
        declared_read_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Get-Content -LiteralPath {ps_quote(declared_read)} -Raw",
            PERMISSION_PROFILE_ID,
        )
        undeclared_read_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Get-Content -LiteralPath {ps_quote(undeclared_read)} -Raw",
            PERMISSION_PROFILE_ID,
        )
        protected_read_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Get-Content -LiteralPath {ps_quote(protected_read)} -Raw",
            PERMISSION_PROFILE_ID,
        )
        control_read_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Get-Content -LiteralPath {ps_quote(layout.control_file)} -Raw",
            PERMISSION_PROFILE_ID,
        )
    else:
        skipped_read = {
            "rpc_ok": False,
            "error": {"type": "PolicyUnsupported", "message": "permission profile 미지원으로 실행 생략"},
        }
        declared_read_result = skipped_read
        undeclared_read_result = skipped_read
        protected_read_result = skipped_read
        control_read_result = skipped_read

    checks["declared_reference_read_allowed"] = evaluate_allowed_read_check(
        "승인된 외부 참조", declared_read_result, declared_token, policy_supported, tokens
    )
    checks["undeclared_path_read_policy"] = evaluate_undeclared_read_policy(
        "미등록 외부 경로", undeclared_read_result, undeclared_token, policy_supported, tokens
    )
    checks["protected_path_read_blocked"] = evaluate_read_check(
        "FlowMarshal 보호 경로", protected_read_result, protected_token, policy_supported, tokens
    )
    checks["control_file_read_allowed"] = evaluate_allowed_read_check(
        "작업 계약 제어 파일", control_read_result, control_token, policy_supported, tokens
    )

    inside_write = workspace / "inside-write.txt"
    if policy_supported:
        inside_write_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Set-Content -LiteralPath {ps_quote(inside_write)} -Value 'INSIDE_WRITE_OK'; Get-Content -LiteralPath {ps_quote(inside_write)} -Raw",
            PERMISSION_PROFILE_ID,
        )
    else:
        inside_write_result = {
            "rpc_ok": False,
            "error": {"type": "PolicyUnsupported", "message": "제한 permission profile 미지원으로 실행 생략"},
        }
    checks["workspace_write_allowed"] = check(
        "pass"
        if policy_supported and inside_write.exists() and command_succeeded(inside_write_result)
        else ("unsupported" if not policy_supported else "fail"),
        "워크스페이스 내부 쓰기 성공"
        if inside_write.exists() and command_succeeded(inside_write_result)
        else "워크스페이스 내부 쓰기 실패",
        target_exists=inside_write.exists(),
        command=sanitize_command_result(inside_write_result, tokens),
    )

    declared_write = layout.declared_reference_root / "reference-write-must-not-exist.txt"
    undeclared_write = layout.undeclared_root / "undeclared-write-must-not-exist.txt"
    protected_write = layout.protected_root / "protected-write-must-not-exist.txt"
    if policy_supported:
        declared_write_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Set-Content -LiteralPath {ps_quote(declared_write)} -Value 'BOUNDARY_BREACH'; Write-Output 'WRITE_OK'",
            PERMISSION_PROFILE_ID,
        )
        undeclared_write_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Set-Content -LiteralPath {ps_quote(undeclared_write)} -Value 'BOUNDARY_BREACH'; Write-Output 'WRITE_OK'",
            PERMISSION_PROFILE_ID,
        )
        protected_write_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Set-Content -LiteralPath {ps_quote(protected_write)} -Value 'BOUNDARY_BREACH'; Write-Output 'WRITE_OK'",
            PERMISSION_PROFILE_ID,
        )
        control_write_result = command_exec(
            client,
            workspace,
            f"$ErrorActionPreference='Stop'; Set-Content -LiteralPath {ps_quote(layout.control_file)} -Value 'BOUNDARY_BREACH'; Write-Output 'WRITE_OK'",
            PERMISSION_PROFILE_ID,
        )
    else:
        skipped = {
            "rpc_ok": False,
            "error": {"type": "PolicyUnsupported", "message": "제한 permission profile 미지원으로 실행 생략"},
        }
        declared_write_result = skipped
        undeclared_write_result = skipped
        protected_write_result = skipped
        control_write_result = skipped
    checks["declared_reference_write_blocked"] = evaluate_write_check(
        "승인된 외부 참조",
        sanitize_command_result(declared_write_result, tokens),
        declared_write,
        policy_supported,
    )
    checks["undeclared_path_write_blocked"] = evaluate_write_check(
        "미승인 외부 경로",
        sanitize_command_result(undeclared_write_result, tokens),
        undeclared_write,
        policy_supported,
    )
    checks["protected_path_write_blocked"] = evaluate_write_check(
        "FlowMarshal 보호 경로",
        sanitize_command_result(protected_write_result, tokens),
        protected_write,
        policy_supported,
    )
    checks["control_file_write_blocked"] = evaluate_existing_write_blocked_check(
        "작업 계약 제어 파일",
        control_write_result,
        layout.control_file,
        control_token,
        policy_supported,
        tokens,
    )

    network_script = (
        "$ErrorActionPreference='Stop'; "
        "try { Invoke-WebRequest -UseBasicParsing -Uri 'https://example.com/' -TimeoutSec 8 | Out-Null; "
        "Write-Output 'NETWORK_OK'; exit 0 } "
        "catch { Write-Output 'NETWORK_BLOCKED'; exit 23 }"
    )
    if policy_supported:
        network_result = command_exec(
            client,
            workspace,
            network_script,
            PERMISSION_PROFILE_ID,
            timeout_ms=15_000,
        )
    else:
        network_result = {
            "rpc_ok": False,
            "error": {"type": "PolicyUnsupported", "message": "제한 permission profile 미지원으로 실행 생략"},
        }
    network_reached = "NETWORK_OK" in command_output(network_result) and command_succeeded(network_result)
    checks["command_network_blocked"] = check(
        "unsupported" if not policy_supported else ("fail" if network_reached else "pass"),
        "제한 permission profile 미지원으로 네트워크 차단을 보장할 수 없음"
        if not policy_supported
        else ("명령 네트워크가 차단됨" if not network_reached else "명령 네트워크 경계가 깨짐"),
        network_reached=network_reached,
        command=sanitize_command_result(network_result, tokens),
    )

    evidence = {
        "workspace": str(workspace),
        "authoritative_codex_home": str(layout.permission_home),
        "declared_reference_root": str(layout.declared_reference_root),
        "undeclared_root": str(layout.undeclared_root),
        "protected_root": str(layout.protected_root),
        "control_file": str(layout.control_file),
        "canary_hashes": {
            "workspace": hashlib.sha256(inside_token.encode()).hexdigest(),
            "declared_reference": hashlib.sha256(declared_token.encode()).hexdigest(),
            "undeclared": hashlib.sha256(undeclared_token.encode()).hexdigest(),
            "protected": hashlib.sha256(protected_token.encode()).hexdigest(),
            "control": hashlib.sha256(control_token.encode()).hexdigest(),
        },
        "permission_profile_id": PERMISSION_PROFILE_ID,
        "permission_profile_inventory": profile_inventory,
        "fallback_policy_used": False,
        "read_policy_contract": permission_policy_contract(),
    }
    return checks, evidence, tokens


def nested_get(mapping: Any, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        if key in current:
            current = current[key]
            continue
        aliases = {
            "webSearch": "web_search",
            "runtimeStatus": "runtime_status",
        }
        alias = aliases.get(key)
        if alias is None or alias not in current:
            return None
        current = current[alias]
    return current


def inspect_external_surfaces(client: CodexClient, cwd: Path, thread_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    observations: dict[str, Any] = {}
    for label, method, params in (
        ("effective_config", "config/read", {"cwd": str(cwd), "includeLayers": True}),
        ("features", "experimentalFeature/list", {"limit": 200}),
        (
            "mcp",
            "mcpServerStatus/list",
            {"threadId": thread_id, "limit": 200, "detail": "toolsAndAuthOnly"},
        ),
        ("apps", "app/installed", {"threadId": thread_id, "forceRefresh": False}),
    ):
        try:
            observations[label] = {
                "ok": True,
                "result": json_safe(client._request_raw(method, params)),  # noqa: SLF001
            }
        except BaseException as exc:
            observations[label] = {"ok": False, "error": exception_record(exc)}

    config = nested_get(observations, "effective_config", "result", "config") or {}
    features = config.get("features", {}) if isinstance(config, dict) else {}
    mcp_config = config.get("mcp_servers", {}) if isinstance(config, dict) else {}
    web_value = config.get("webSearch", config.get("web_search")) if isinstance(config, dict) else None
    web_disabled = web_value == "disabled"
    apps_flag_disabled = isinstance(features, dict) and features.get("apps") is False
    plugins_flag_disabled = isinstance(features, dict) and features.get("plugins") is False

    feature_rows = nested_get(observations, "features", "result", "data")
    feature_states = {
        item.get("name"): item.get("enabled")
        for item in feature_rows
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    } if isinstance(feature_rows, list) else {}
    browser_feature_names = (
        "in_app_browser",
        "browser_use",
        "browser_use_full_cdp_access",
        "browser_use_external",
        "computer_use",
    )
    browser_computer_disabled = all(feature_states.get(name) is False for name in browser_feature_names)

    mcp_config_disabled = isinstance(mcp_config, dict) and all(
        isinstance(value, dict) and value.get("enabled") is False
        for value in mcp_config.values()
    )
    mcp_data = nested_get(observations, "mcp", "result", "data")
    mcp_inventory_disabled = isinstance(mcp_data, list) and all(
        isinstance(item, dict)
        and not item.get("tools")
        and not item.get("serverInfo")
        for item in mcp_data
    )
    mcp_disabled = mcp_config_disabled and mcp_inventory_disabled
    apps_data = nested_get(observations, "apps", "result", "apps")
    apps_inventory_disabled = isinstance(apps_data, list) and all(
        isinstance(item, dict) and not item.get("enabled") and not item.get("callable")
        for item in apps_data
    )

    passed = all(
        (
            web_disabled,
            apps_flag_disabled,
            plugins_flag_disabled,
            browser_computer_disabled,
            mcp_disabled,
            apps_inventory_disabled,
        )
    )
    outcome = check(
        "pass" if passed else "fail",
        "Web Search/MCP/Plugin/Connector 및 Browser·Computer Use 진입 표면이 비활성화됨"
        if passed
        else "외부 도구 표면의 비활성화를 모두 입증하지 못함",
        web_search_disabled=web_disabled,
        apps_feature_disabled=apps_flag_disabled,
        plugins_feature_disabled=plugins_flag_disabled,
        browser_computer_features_disabled=browser_computer_disabled,
        mcp_configuration_disabled=mcp_config_disabled,
        mcp_inventory_disabled=mcp_inventory_disabled,
        apps_inventory_disabled=apps_inventory_disabled,
    )
    return outcome, observations


def effective_windows_elevated(observations: dict[str, Any]) -> dict[str, Any]:
    config = nested_get(observations, "effective_config", "result", "config") or {}
    windows = config.get("windows", {}) if isinstance(config, dict) else {}
    mode = windows.get("sandbox") if isinstance(windows, dict) else None
    return check(
        "pass" if mode == "elevated" else "fail",
        "Windows elevated sandbox가 유효 설정으로 확인됨"
        if mode == "elevated"
        else "Windows elevated sandbox 유효 설정을 확인하지 못함",
        effective_mode=mode,
    )


def read_windows_sandbox_readiness(client: CodexClient) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "result": json_safe(
                client._request_raw(  # noqa: SLF001 - Gate 0A는 원시 프로토콜 검증이다.
                    "windowsSandbox/readiness",
                    None,
                )
            ),
        }
    except BaseException as exc:
        return {"ok": False, "error": exception_record(exc)}


def evaluate_windows_sandbox_readiness(
    observation: dict[str, Any],
    *,
    setup_command: str | None = None,
) -> dict[str, Any]:
    if not observation.get("ok"):
        return check(
            "error",
            "Windows sandbox 준비 상태를 읽지 못함",
            error=observation.get("error"),
        )
    status = nested_get(observation, "result", "status")
    if status == "ready":
        return check("pass", "실제 Codex 홈의 Windows sandbox가 준비됨", readiness=status)
    if status in {"notConfigured", "updateRequired"}:
        return check(
            "setup_required",
            "실제 Codex 홈에 관리자 승인 sandbox 설정이 필요함",
            readiness=status,
            setup_command=setup_command,
        )
    return check(
        "error",
        "알 수 없는 Windows sandbox 준비 상태",
        readiness=status,
    )


def sanitize_config_observation(observation: Any) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {"ok": False, "result": {"config": {}}}
    payload = observation.get("result", {}) if observation.get("ok") else {}
    config = payload.get("config", {}) if isinstance(payload, dict) else {}
    features = config.get("features", {}) if isinstance(config, dict) else {}
    permissions = config.get("permissions", {}) if isinstance(config, dict) else {}
    profile = permissions.get(PERMISSION_PROFILE_ID, {}) if isinstance(permissions, dict) else {}
    profile_network = profile.get("network", {}) if isinstance(profile, dict) else {}
    profile_filesystem = profile.get("filesystem", {}) if isinstance(profile, dict) else {}
    windows = config.get("windows", {}) if isinstance(config, dict) else {}
    legacy_paths = find_explicit_legacy_paths(payload)
    safe_config = {
        "web_search": config.get("webSearch", config.get("web_search"))
        if isinstance(config, dict)
        else None,
        "features": {
            name: features.get(name)
            for name in SECURITY_FEATURE_NAMES
            if isinstance(features, dict) and name in features
        },
        "windows": {"sandbox": windows.get("sandbox") if isinstance(windows, dict) else None},
        "legacy_sandbox_present": bool(legacy_paths),
        "legacy_sandbox_key_paths": legacy_paths,
        "default_permissions": config.get("defaultPermissions", config.get("default_permissions"))
        if isinstance(config, dict)
        else None,
        "permission_profile": {
            "id": PERMISSION_PROFILE_ID,
            "present": isinstance(profile, dict) and bool(profile),
            "extends": profile.get("extends") if isinstance(profile, dict) else None,
            "filesystem": profile_filesystem if isinstance(profile_filesystem, dict) else {},
            "network_enabled": profile_network.get("enabled")
            if isinstance(profile_network, dict)
            else None,
        },
    }
    return {
        "ok": bool(observation.get("ok")),
        "result": {"config": safe_config},
        **({"error": observation.get("error")} if observation.get("error") else {}),
    }


def sanitize_surface_observations(observations: Any) -> dict[str, Any]:
    """판정에 불필요한 config/env/tool schema를 결과 파일에서 제거한다."""

    if not isinstance(observations, dict):
        return {}
    if observations.get("storage_sanitized") is True:
        return observations

    config_observation = observations.get("effective_config", {})
    config = nested_get(config_observation, "result", "config") or {}
    config_features = config.get("features", {}) if isinstance(config, dict) else {}
    permissions = config.get("permissions", {}) if isinstance(config, dict) else {}
    profile = permissions.get(PERMISSION_PROFILE_ID, {}) if isinstance(permissions, dict) else {}
    profile_network = profile.get("network", {}) if isinstance(profile, dict) else {}
    profile_filesystem = profile.get("filesystem", {}) if isinstance(profile, dict) else {}
    windows = config.get("windows", {}) if isinstance(config, dict) else {}

    safe_config = {
        "web_search": config.get("webSearch", config.get("web_search"))
        if isinstance(config, dict)
        else None,
        "features": {
            name: config_features.get(name)
            for name in SECURITY_FEATURE_NAMES
            if isinstance(config_features, dict) and name in config_features
        },
        "windows": {"sandbox": windows.get("sandbox") if isinstance(windows, dict) else None},
        "default_permissions": config.get("defaultPermissions", config.get("default_permissions"))
        if isinstance(config, dict)
        else None,
        "permission_profile": {
            "id": PERMISSION_PROFILE_ID,
            "extends": profile.get("extends") if isinstance(profile, dict) else None,
            "filesystem": profile_filesystem if isinstance(profile_filesystem, dict) else {},
            "network_enabled": profile_network.get("enabled")
            if isinstance(profile_network, dict)
            else None,
        },
    }

    feature_rows = nested_get(observations, "features", "result", "data")
    safe_features = [
        {
            "name": item.get("name"),
            "enabled": item.get("enabled"),
            "defaultEnabled": item.get("defaultEnabled"),
            "stage": item.get("stage"),
        }
        for item in feature_rows
        if isinstance(item, dict) and item.get("name") in SECURITY_FEATURE_NAMES
    ] if isinstance(feature_rows, list) else []

    mcp_rows = nested_get(observations, "mcp", "result", "data")
    safe_mcp = [
        {
            "name": item.get("name"),
            "authStatus": item.get("authStatus", item.get("auth_status")),
            "toolCount": len(item.get("tools", {})) if isinstance(item.get("tools"), dict) else 0,
            "serverInfoPresent": isinstance(item.get("serverInfo"), dict),
        }
        for item in mcp_rows
        if isinstance(item, dict)
    ] if isinstance(mcp_rows, list) else []

    app_rows = nested_get(observations, "apps", "result", "apps")
    safe_apps = [
        {
            "id": item.get("id"),
            "runtimeName": item.get("runtimeName"),
            "enabled": item.get("enabled"),
            "callable": item.get("callable"),
        }
        for item in app_rows
        if isinstance(item, dict)
    ] if isinstance(app_rows, list) else []

    return {
        "storage_sanitized": True,
        "effective_config": sanitize_config_observation(config_observation),
        "features": {
            "ok": bool(nested_get(observations, "features", "ok")),
            "result": {"data": safe_features},
        },
        "mcp": {
            "ok": bool(nested_get(observations, "mcp", "ok")),
            "result": {"data": safe_mcp},
        },
        "apps": {
            "ok": bool(nested_get(observations, "apps", "ok")),
            "result": {"apps": safe_apps},
        },
    }


def sanitize_document_for_storage(document: dict[str, Any]) -> None:
    for runtime in document.get("runtimes", []):
        evidence = runtime.get("evidence", {})
        if isinstance(evidence, dict) and "external_surfaces" in evidence:
            evidence["external_surfaces"] = sanitize_surface_observations(
                evidence["external_surfaces"]
            )


def notification_to_dict(notification: Any) -> dict[str, Any]:
    return {
        "method": getattr(notification, "method", "unknown"),
        "payload": json_safe(getattr(notification, "payload", None)),
    }


def wait_for_turn(
    client: CodexClient,
    thread_id: str,
    turn_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    result_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    events: list[dict[str, Any]] = []

    def consume() -> None:
        try:
            while True:
                notification = client.next_turn_notification(turn_id)
                serialized = notification_to_dict(notification)
                events.append(serialized)
                if serialized["method"] == "turn/completed":
                    result_queue.put({"completed": True, "event": serialized})
                    return
        except BaseException as exc:
            result_queue.put({"completed": False, "error": exception_record(exc)})

    waiter = threading.Thread(target=consume, daemon=True, name=f"gate0a-turn-{turn_id}")
    waiter.start()
    try:
        outcome = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        try:
            client.turn_interrupt(thread_id, turn_id)
        except BaseException:
            pass
        outcome = {"completed": False, "timed_out": True}
    finally:
        client.unregister_turn_notifications(turn_id)
    outcome["events"] = events
    return outcome


def interrupt_active_turn(
    client: CodexClient,
    thread_id: str,
    turn_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    result_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    started_event = threading.Event()
    events: list[dict[str, Any]] = []

    def consume() -> None:
        try:
            while True:
                notification = client.next_turn_notification(turn_id)
                serialized = notification_to_dict(notification)
                events.append(serialized)
                if serialized["method"] == "turn/started":
                    started_event.set()
                if serialized["method"] == "turn/completed":
                    result_queue.put({"completed": True, "event": serialized})
                    return
        except BaseException as exc:
            result_queue.put({"completed": False, "error": exception_record(exc)})

    waiter = threading.Thread(target=consume, daemon=True, name=f"gate0a-interrupt-{turn_id}")
    waiter.start()
    saw_started = started_event.wait(timeout=min(30, timeout_seconds))
    interrupt_rpc_ok = False
    interrupt_error: dict[str, Any] | None = None
    if saw_started:
        for _attempt in range(5):
            try:
                client.turn_interrupt(thread_id, turn_id)
                interrupt_rpc_ok = True
                interrupt_error = None
                break
            except BaseException as exc:
                interrupt_error = exception_record(exc)
                if "no active turn" not in str(exc):
                    break
                time.sleep(0.1)
    else:
        interrupt_error = {
            "type": "TurnStartTimeout",
            "message": "turn/started 알림을 기다리는 동안 제한 시간이 지남",
        }

    try:
        outcome = result_queue.get(timeout=max(1, timeout_seconds - min(30, timeout_seconds)))
    except queue.Empty:
        outcome = {"completed": False, "timed_out": True}
    finally:
        client.unregister_turn_notifications(turn_id)
    outcome.update(
        {
            "events": events,
            "saw_turn_started": saw_started,
            "interrupt_rpc_ok": interrupt_rpc_ok,
            "interrupt_rpc_error": interrupt_error,
        }
    )
    return outcome


def turn_status(wait_result: dict[str, Any]) -> str | None:
    return nested_get(wait_result, "event", "payload", "turn", "status")


def run_thread_checks_first_session(
    client: CodexClient,
    spec: RuntimeSpec,
    workspace: Path,
    timeout_seconds: int,
    run_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    evidence: dict[str, Any] = {}
    title = f"FlowMarshal Gate 0A {spec.name} {run_id[:8]}"
    started = client.thread_start(
        {
            "cwd": str(workspace),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
            "ephemeral": False,
            "serviceName": "flowmarshal_gate0a",
            "developerInstructions": (
                "이 task는 FlowMarshal Gate 0A 합성 호환성 검사다. 사용자의 실제 파일이나 외부 서비스에 "
                "접근하지 말고, 도구 사용 지시가 없으면 텍스트로만 응답한다."
            ),
        }
    )
    thread_id = started.thread.id
    evidence["thread_id"] = thread_id
    evidence["thread_start"] = json_safe(started)
    try:
        evidence["name_set"] = json_safe(client.thread_set_name(thread_id, title))
    except BaseException as exc:
        evidence["name_set_error"] = exception_record(exc)
    evidence["thread_title"] = title

    before = client.thread_read(thread_id, include_turns=True)
    turns_before = len(before.thread.turns)
    checks["thread_turn_separation"] = check(
        "pass" if turns_before == 0 else "fail",
        "thread/start 직후 turn이 생성되지 않음" if turns_before == 0 else "thread/start가 예기치 않은 turn을 생성함",
        turns_before_turn_start=turns_before,
    )

    turn = client.turn_start(
        thread_id,
        "도구를 사용하지 말고 GATE0A_OK 한 줄만 답하세요.",
        params={
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "effort": "low",
            "outputSchema": {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["GATE0A_OK"]}},
                "required": ["status"],
                "additionalProperties": False,
            },
        },
    )
    wait = wait_for_turn(client, thread_id, turn.turn.id, timeout_seconds)
    status = turn_status(wait)
    evidence["turn_id"] = turn.turn.id
    evidence["turn_wait"] = wait
    checks["turn_execution"] = check(
        "pass" if wait.get("completed") and status == "completed" else "fail",
        "turn/start로 시작한 최소 turn이 완료됨"
        if wait.get("completed") and status == "completed"
        else "최소 turn이 정상 완료되지 않음",
        turn_id=turn.turn.id,
        completion_status=status,
    )
    return checks, evidence


def run_thread_checks_second_session(
    client: CodexClient,
    thread_id: str,
    workspace: Path,
    timeout_seconds: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    evidence: dict[str, Any] = {}

    listed = client.thread_list(
        {
            "sourceKinds": ["appServer", "vscode", "cli", "exec", "unknown"],
            "cwd": str(workspace),
            "limit": 100,
            "sortDirection": "desc",
        }
    )
    listed_ids = [thread.id for thread in listed.data]
    checks["persisted_thread_listed"] = check(
        "pass" if thread_id in listed_ids else "fail",
        "재시작 후 thread/list에서 저장 task를 찾음"
        if thread_id in listed_ids
        else "재시작 후 thread/list에서 저장 task를 찾지 못함",
        listed_count=len(listed_ids),
        target_found=thread_id in listed_ids,
    )
    evidence["thread_list_ids"] = listed_ids
    evidence["thread_list_sources"] = {
        thread.id: json_safe(thread.source) for thread in listed.data
    }

    read = client.thread_read(thread_id, include_turns=True)
    resumed = client.thread_resume(thread_id)
    resume_ok = read.thread.id == thread_id and resumed.thread.id == thread_id and len(read.thread.turns) >= 1
    checks["resume_after_restart"] = check(
        "pass" if resume_ok else "fail",
        "App Server 재시작 후 thread/read와 thread/resume 성공"
        if resume_ok
        else "App Server 재시작 후 task 복구가 불완전함",
        read_turn_count=len(read.thread.turns),
        resumed_thread_id=resumed.thread.id,
    )
    evidence["thread_read_after_restart"] = json_safe(read)
    evidence["thread_resume_after_restart"] = json_safe(resumed)

    interrupt_turn = client.turn_start(
        thread_id,
        "이 메시지에는 응답하지 말고 대기하세요. 이 turn은 즉시 interrupt 검사를 받습니다.",
        params={"approvalPolicy": "on-request", "approvalsReviewer": "user", "effort": "low"},
    )
    wait = interrupt_active_turn(
        client,
        thread_id,
        interrupt_turn.turn.id,
        min(timeout_seconds, 60),
    )
    status = turn_status(wait)
    interrupt_rpc_ok = bool(wait.get("interrupt_rpc_ok"))
    interrupted = interrupt_rpc_ok and status == "interrupted"
    checks["interrupt"] = check(
        "pass" if interrupted else "fail",
        "turn/interrupt가 in-flight turn을 interrupted 상태로 종료함"
        if interrupted
        else "turn/interrupt 종료 상태를 입증하지 못함",
        turn_id=interrupt_turn.turn.id,
        rpc_ok=interrupt_rpc_ok,
        rpc_error=wait.get("interrupt_rpc_error"),
        saw_turn_started=wait.get("saw_turn_started"),
        completion_status=status,
    )
    evidence["interrupt_wait"] = wait
    return checks, evidence


def probe_runtime(
    spec: RuntimeSpec,
    root: Path,
    run_id: str,
    timeout_seconds: int,
    host_context: SandboxHostContext,
    fingerprint_before: SandboxFingerprint,
) -> dict[str, Any]:
    started_at = utc_now()
    permission_handler = FailClosedApprovalHandler()
    runtime_handler = FailClosedApprovalHandler()
    result: dict[str, Any] = {
        "name": spec.name,
        "role": spec.role,
        "executable": str(spec.executable),
        "version": runtime_version(spec.executable),
        "started_at": started_at,
        "checks": {},
        "evidence": {},
        "thread_id": None,
        "thread_title": None,
    }
    result["checks"]["approvals_fail_closed"] = validate_fail_closed_handler(runtime_handler)

    layout = canary_layout(
        root,
        run_id,
        spec.name,
        host_context.authoritative_home,
    )
    permission_overrides = build_config_overrides(layout)
    runtime_overrides = BASE_CONFIG_OVERRIDES
    tokens: list[str] = []
    configuration_status: str | None = None
    readiness_status: str | None = None

    # 0A-P는 현재 시스템 Codex와 실제 authoritative home만 사용한다. App Server는
    # 홈을 신뢰 계층에서 읽지만 command sandbox에는 홈 전체를 deny로 전달한다.
    permission_client: CodexClient | None = None
    try:
        layout.workspace.mkdir(parents=True, exist_ok=True)
        permission_client = make_client(
            spec,
            layout.workspace,
            permission_handler,
            permission_overrides,
            env={"CODEX_HOME": str(layout.permission_home)},
        )
        permission_client.start()
        result["evidence"]["permission_initialize"] = json_safe(
            permission_client.initialize()
        )

        config_observation = read_effective_config(permission_client, layout.workspace)
        configuration_check = evaluate_permission_configuration(config_observation)
        configuration_status = configuration_check.get("status")
        result["checks"]["permission_configuration"] = configuration_check
        result["checks"]["windows_elevated_sandbox"] = effective_windows_elevated(
            {"effective_config": config_observation}
        )
        result["evidence"]["configuration_preflight"] = {
            "check": configuration_check,
            "requested_profile": PERMISSION_PROFILE_ID,
            "authoritative_codex_home": str(layout.permission_home),
            "authentication_copied": False,
            "config_observation": sanitize_config_observation(config_observation),
        }

        if configuration_status == "pass":
            readiness_observation = read_windows_sandbox_readiness(permission_client)
            readiness_check = evaluate_windows_sandbox_readiness(
                readiness_observation,
                setup_command=manual_setup_command(host_context),
            )
            readiness_status = readiness_check.get("status")
            result["checks"]["windows_sandbox_operational"] = readiness_check
            result["evidence"]["windows_sandbox_readiness"] = readiness_observation

            profile_check, profile_inventory, _ = inspect_permission_profile(
                permission_client,
                layout.workspace,
            )
            result["checks"]["permission_profile_supported"] = profile_check
            result["evidence"]["permission_profile_inventory"] = profile_inventory

            if readiness_status == "pass" and profile_check.get("status") == "pass":
                sandbox_checks, sandbox_evidence, tokens = run_sandbox_checks(
                    permission_client,
                    layout,
                    profile_inventory,
                )
                result["checks"].update(sandbox_checks)
                result["evidence"]["sandbox"] = sandbox_evidence
            elif readiness_status == "setup_required":
                for name in PERMISSION_HARD_CHECKS:
                    if name not in {
                        "permission_configuration",
                        "windows_elevated_sandbox",
                        "windows_sandbox_operational",
                        "permission_profile_supported",
                        "external_surfaces_disabled",
                        "approvals_fail_closed",
                    }:
                        result["checks"].setdefault(
                            name,
                            check(
                                "not_run",
                                "Windows sandbox 설정이 필요해 카나리 검사를 실행하지 않음",
                            ),
                        )
    except BaseException as exc:
        result["permission_error"] = exception_record(exc)
        result["checks"].setdefault(
            "permission_configuration",
            check("error", "authoritative home permission 세션 초기화 또는 preflight 실패", error=exception_record(exc)),
        )
        configuration_status = result["checks"]["permission_configuration"].get("status")
    finally:
        if permission_client is not None:
            permission_client.close()

    # 구성된 MCP 이름을 먼저 읽고, 다음 프로세스 시작 인자에서 각각 비활성화한다.
    # 빈 mcp_servers 테이블은 TOML 병합에서 기존 서버를 제거하지 않기 때문이다.
    surface_preflight_status: str | None = None
    surface_handler = FailClosedApprovalHandler()
    surface_client: CodexClient | None = None
    if configuration_status == "pass":
        try:
            surface_client = make_client(
                spec,
                layout.workspace,
                surface_handler,
                BASE_CONFIG_OVERRIDES,
                env={"CODEX_HOME": str(host_context.authoritative_home)},
            )
            surface_client.start()
            surface_client.initialize()
            surface_observation = read_effective_config(surface_client, layout.workspace)
            surface_preflight, mcp_overrides = build_mcp_disable_overrides(
                surface_observation
            )
            surface_preflight_status = surface_preflight.get("status")
            runtime_overrides = (*BASE_CONFIG_OVERRIDES, *mcp_overrides)
            result["evidence"]["runtime_surface_preflight"] = {
                "check": surface_preflight,
                "mcp_disable_overrides": list(mcp_overrides),
            }
            if surface_preflight_status != "pass":
                result["checks"]["external_surfaces_disabled"] = check(
                    "fail",
                    "외부 도구 표면을 fail-closed로 비활성화하지 못함",
                    preflight=surface_preflight,
                )
        except BaseException as exc:
            surface_preflight_status = "error"
            result["surface_preflight_error"] = exception_record(exc)
            result["checks"]["external_surfaces_disabled"] = check(
                "error",
                "runtime 외부 도구 표면 preflight 실패",
                error=exception_record(exc),
            )
        finally:
            if surface_client is not None:
                surface_client.close()

    # 0A-R은 기존 인증·Desktop 상태를 사용하되 permission profile과 섞지 않고
    # 최소 read-only thread sandbox로 실행한다.
    runtime_client: CodexClient | None = None
    if configuration_status == "pass" and surface_preflight_status == "pass":
        try:
            runtime_client = make_client(
                spec,
                layout.workspace,
                runtime_handler,
                runtime_overrides,
                env={"CODEX_HOME": str(host_context.authoritative_home)},
            )
            runtime_client.start()
            initialized = runtime_client.initialize()
            result["checks"]["initialize"] = check(
                "pass",
                "initialize 요청과 initialized 알림 완료",
                response=json_safe(initialized),
            )
            thread_checks, thread_evidence = run_thread_checks_first_session(
                runtime_client, spec, layout.workspace, timeout_seconds, run_id
            )
            result["checks"].update(redact_tokens(thread_checks, tokens))
            result["evidence"]["first_session"] = redact_tokens(thread_evidence, tokens)
            result["thread_id"] = thread_evidence["thread_id"]
            result["thread_title"] = thread_evidence["thread_title"]

            surfaces, surface_observations = inspect_external_surfaces(
                runtime_client, layout.workspace, result["thread_id"]
            )
            result["checks"]["external_surfaces_disabled"] = surfaces
            result["evidence"]["external_surfaces"] = redact_tokens(surface_observations, tokens)
        except BaseException as exc:
            result["runtime_error"] = exception_record(exc)
            result["checks"].setdefault(
                "initialize",
                check("fail", "App Server 초기화 또는 첫 runtime 세션 검사 실패", error=exception_record(exc)),
            )
        finally:
            if runtime_client is not None:
                runtime_client.close()

    if result.get("thread_id"):
        restart_handler = FailClosedApprovalHandler()
        restarted: CodexClient | None = None
        try:
            restarted = make_client(
                spec,
                layout.workspace,
                restart_handler,
                runtime_overrides,
                env={"CODEX_HOME": str(host_context.authoritative_home)},
            )
            restarted.start()
            initialized = restarted.initialize()
            result["evidence"]["restart_initialize"] = json_safe(initialized)
            restart_checks, restart_evidence = run_thread_checks_second_session(
                restarted,
                str(result["thread_id"]),
                layout.workspace,
                timeout_seconds,
            )
            result["checks"].update(restart_checks)
            result["evidence"]["second_session"] = restart_evidence
        except BaseException as exc:
            result["restart_error"] = exception_record(exc)
            result["checks"].setdefault(
                "persisted_thread_listed",
                check("fail", "재시작 후 저장 task 목록 검사 실패", error=exception_record(exc)),
            )
            result["checks"].setdefault(
                "resume_after_restart",
                check("fail", "재시작 후 resume 검사 실패", error=exception_record(exc)),
            )
            result["checks"].setdefault(
                "interrupt",
                check("fail", "interrupt 검사 실패", error=exception_record(exc)),
            )
        finally:
            if restarted is not None:
                restarted.close()

    fingerprint_after = capture_sandbox_fingerprint(host_context.authoritative_home)
    fingerprint_ok = (
        fingerprint_is_complete(fingerprint_before)
        and fingerprint_is_complete(fingerprint_after)
        and fingerprints_match(fingerprint_before, fingerprint_after)
    )
    result["checks"]["sandbox_state_unchanged"] = check(
        "pass" if fingerprint_ok else "fail",
        "권한 검사 전후 샌드박스 계정·marker·secret 메타데이터가 동일함"
        if fingerprint_ok
        else "권한 검사 중 컴퓨터 전역 샌드박스 상태가 변경됐거나 지문을 완전하게 읽지 못함",
        reason_code=None if fingerprint_ok else SANDBOX_STATE_CHANGED_DURING_PROBE,
    )
    result["evidence"]["sandbox_state_fingerprint"] = {
        "before": fingerprint_before.as_dict(),
        "after": fingerprint_after.as_dict(),
        "secret_contents_read": False,
    }

    unsafe_artifacts = artifact_safety_violations(artifact_root(root))
    result["checks"]["artifact_safety"] = check(
        "pass" if not unsafe_artifacts else "fail",
        "프로젝트 artifacts에 Codex 인증·상태 저장소가 없음"
        if not unsafe_artifacts
        else "프로젝트 artifacts에 격리해야 할 과거 Codex 상태 저장소가 남아 있음",
        violations=unsafe_artifacts,
    )

    missing_status = "not_run" if configuration_status == "invalid_configuration" else "error"
    missing_summary = (
        "설정 preflight 실패로 검사를 실행하지 않음"
        if missing_status == "not_run"
        else "선행 실패로 검사를 완료하지 못함"
    )
    for name in (*RUNTIME_HARD_CHECKS, *PERMISSION_HARD_CHECKS):
        result["checks"].setdefault(name, check(missing_status, missing_summary))

    runtime_requests = [
        *permission_handler.requests,
        *surface_handler.requests,
        *runtime_handler.requests,
    ]
    if "restart_handler" in locals():
        runtime_requests.extend(restart_handler.requests)
    result["evidence"]["approval_requests"] = runtime_requests
    result["evidence"]["session_isolation"] = {
        "authoritative_codex_home": str(layout.permission_home),
        "permission_authentication_copied": False,
        "permission_model_turns_created": False,
        "runtime_codex_home": str(host_context.authoritative_home),
        "runtime_thread_sandbox": "read-only",
    }
    result["completed_at"] = utc_now()
    return result


def pending_desktop_interop() -> dict[str, Any]:
    return {
        "status": "pending",
        "checked_at": None,
        "checked_via": None,
        "desktop_version": None,
        "desktop_thread_id": None,
        "checks": {
            name: check("not_run", "신뢰된 Desktop 양방향 상호운용성 검사가 필요함")
            for name in DESKTOP_INTEROP_HARD_CHECKS
        },
    }


def value_contains_marker(value: Any, marker: str) -> bool:
    if not marker:
        return False
    return marker in json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True)


def normalized_windows_path(value: Any) -> str:
    return os.path.normcase(os.path.normpath(str(value or ""))).rstrip("\\/")


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def evaluate_desktop_interop_bundle(
    challenge: dict[str, Any],
    observation: dict[str, Any],
    sdk_state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    expected_thread_id = challenge.get("thread_id")
    expected_project_id = challenge.get("expected_project_id")
    expected_cwd = normalized_windows_path(challenge.get("expected_cwd"))
    observed_cwd = normalized_windows_path(observation.get("observed_cwd"))
    project_ok = bool(
        observation.get("thread_found")
        and expected_cwd
        and observed_cwd == expected_cwd
        and observation.get("project_id") == expected_project_id
    )

    sdk_turn_id = challenge.get("sdk_turn_id")
    sdk_visible = bool(
        observation.get("sdk_marker_visible")
        and observation.get("sdk_turn_id") == sdk_turn_id
        and sdk_state.get("sdk_marker_visible_after_turn")
        and sdk_state.get("sdk_turn_completed")
    )
    desktop_visible = bool(sdk_state.get("desktop_marker_visible_final"))
    sdk_read_resume = bool(
        sdk_state.get("read_thread_id") == expected_thread_id
        and sdk_state.get("resumed_thread_id") == expected_thread_id
        and sdk_state.get("final_thread_id") == expected_thread_id
        and sdk_state.get("desktop_marker_visible_before")
    )

    ready_at = parse_utc_timestamp(challenge.get("ready_at"))
    expires_at = parse_utc_timestamp(challenge.get("expires_at"))
    observed_at = parse_utc_timestamp(observation.get("observed_at"))
    concurrent_ok = bool(
        observation.get("challenge_nonce") == challenge.get("challenge_nonce")
        and observation.get("thread_id") == expected_thread_id
        and observation.get("sdk_turn_id") == sdk_turn_id
        and observation.get("status_during_sdk_wait") in {"active", "idle", "notLoaded"}
        and ready_at is not None
        and expires_at is not None
        and observed_at is not None
        and observed_at >= ready_at
        and observed_at <= expires_at
    )

    return {
        "project_grouping_correct": check(
            "pass" if project_ok else "fail",
            "Desktop가 대상 task를 예상 project와 cwd에 표시함"
            if project_ok
            else "Desktop project grouping 증거가 예상과 다름",
            expected_project_id=expected_project_id,
            observed_project_id=observation.get("project_id"),
            expected_cwd=challenge.get("expected_cwd"),
            observed_cwd=observation.get("observed_cwd"),
        ),
        "sdk_resume_visible_in_desktop": check(
            "pass" if sdk_visible else "fail",
            "SDK가 resume 후 추가한 turn을 Desktop에서 확인함"
            if sdk_visible
            else "SDK turn의 Desktop 표시 증거가 불완전함",
            sdk_turn_id=sdk_turn_id,
            desktop_observed=observation.get("sdk_marker_visible"),
            sdk_turn_completed=sdk_state.get("sdk_turn_completed"),
        ),
        "desktop_turn_visible_to_sdk": check(
            "pass" if desktop_visible else "fail",
            "Desktop에서 완료된 turn 증거를 SDK thread/read에서 확인함"
            if desktop_visible
            else "Desktop turn 증거를 SDK가 읽지 못함",
        ),
        "desktop_thread_sdk_read_resume": check(
            "pass" if sdk_read_resume else "fail",
            "Desktop task ID를 SDK가 thread/read와 thread/resume로 동일하게 복구함"
            if sdk_read_resume
            else "Desktop task의 SDK read/resume 증거가 불완전함",
            read_thread_id=sdk_state.get("read_thread_id"),
            resumed_thread_id=sdk_state.get("resumed_thread_id"),
            final_thread_id=sdk_state.get("final_thread_id"),
        ),
        "concurrent_access_consistent": check(
            "pass" if concurrent_ok else "fail",
            "SDK 연결이 열린 동안 Desktop이 같은 task와 SDK turn을 일관되게 읽음"
            if concurrent_ok
            else "동시 접근 challenge/observation 증거가 일치하지 않음",
            ready_at=challenge.get("ready_at"),
            expires_at=challenge.get("expires_at"),
            observed_at=observation.get("observed_at"),
            desktop_status=observation.get("status_during_sdk_wait"),
        ),
    }


def runtime_is_candidate(runtime: dict[str, Any]) -> bool:
    checks = runtime.get("checks", {})
    return all(nested_get(checks, name, "status") == "pass" for name in RUNTIME_HARD_CHECKS)


def collect_failures(
    document: dict[str, Any],
    check_names: Iterable[str],
    subgate: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for runtime in document.get("runtimes", []):
        for name in check_names:
            status = nested_get(runtime, "checks", name, "status")
            if status != "pass":
                failures.append(
                    {
                        "subgate": subgate,
                        "runtime": runtime.get("name"),
                        "check": name,
                        "status": status,
                        "summary": nested_get(runtime, "checks", name, "summary"),
                    }
                )
    return failures


def permission_recheck_is_candidate(
    document: dict[str, Any],
    recheck: dict[str, Any],
) -> bool:
    """현재 Gate 실행에 속한 완전한 시스템 권한 재검사만 복구 후보로 인정한다."""

    check_statuses = recheck.get("hard_check_statuses")
    return bool(
        isinstance(document.get("run_id"), str)
        and bool(document.get("run_id"))
        and recheck.get("status") == "pass"
        and recheck.get("schema_version") == PERMISSION_RECHECK_SCHEMA_VERSION
        and recheck.get("kind") == PERMISSION_RECHECK_KIND
        and recheck.get("source_gate_run_id") == document.get("run_id")
        and recheck.get("runtime_name") == SYSTEM_RUNTIME_NAME
        and isinstance(recheck.get("checked_at"), str)
        and bool(recheck.get("checked_at"))
        and isinstance(recheck.get("runtime_executable"), str)
        and bool(recheck.get("runtime_executable"))
        and re.fullmatch(r"[0-9a-f]{64}", str(recheck.get("sha256") or ""))
        and recheck.get("secret_contents_read") is False
        and recheck.get("hard_checks_complete") is True
        and isinstance(check_statuses, dict)
        and all(check_statuses.get(name) == "pass" for name in PERMISSION_HARD_CHECKS)
    )


def evaluate_desktop_interop(document: dict[str, Any]) -> dict[str, Any]:
    interop = document.get("desktop_interop")
    if not isinstance(interop, dict):
        interop = pending_desktop_interop()
    checks = interop.get("checks") if isinstance(interop.get("checks"), dict) else {}
    failures = [
        {
            "subgate": "0A-R",
            "runtime": "desktop-interop",
            "check": name,
            "status": nested_get(checks, name, "status"),
            "summary": nested_get(checks, name, "summary"),
        }
        for name in DESKTOP_INTEROP_HARD_CHECKS
        if nested_get(checks, name, "status") != "pass"
    ]
    if any(item["status"] in {"fail", "error"} for item in failures):
        status = "fail"
        summary = "Desktop 양방향 상호운용성 필수 검사에 실패함"
    elif failures:
        status = "pending"
        summary = "Desktop 양방향 상호운용성 증거가 아직 완성되지 않음"
    else:
        status = "pass"
        summary = "Desktop 양방향 상호운용성 필수 검사를 모두 통과함"
    return {"status": status, "summary": summary, "blocking_failures": failures}


def derive_runtime_decision(document: dict[str, Any]) -> dict[str, Any]:
    runtimes = document.get("runtimes", [])
    candidates = [runtime for runtime in runtimes if runtime_is_candidate(runtime)]
    failures = collect_failures(document, RUNTIME_HARD_CHECKS, "0A-R")
    interop = evaluate_desktop_interop(document)
    config_invalid = any(
        nested_get(runtime, "checks", "permission_configuration", "status")
        == "invalid_configuration"
        for runtime in runtimes
    )

    desktop = document.get("desktop_visibility", {})
    desktop_status = desktop.get("status", "pending")
    if not candidates:
        non_initialize = [name for name in RUNTIME_HARD_CHECKS if name != "initialize"]
        stopped_by_preflight = config_invalid and all(
            nested_get(runtime, "checks", name, "status") in {None, "not_run"}
            for runtime in runtimes
            for name in non_initialize
        )
        status = "NOT_RUN" if stopped_by_preflight else "NO-GO"
        reason = (
            "permission 설정 preflight에서 중단되어 런타임 상호운용성 검사를 실행하지 않음"
            if stopped_by_preflight
            else "필수 런타임 검사를 모두 통과한 조합이 없음"
        )
    elif desktop_status == "pending":
        status = "PENDING-DESKTOP-CHECK"
        reason = "런타임 검사는 통과했으나 Codex Desktop 표시 확인이 남음"
    elif desktop_status != "pass":
        status = "NO-GO"
        reason = "SDK 생성 task의 Codex Desktop 표시를 입증하지 못함"
    else:
        visible = set(desktop.get("visible_thread_ids", []))
        visible_candidates = [runtime for runtime in candidates if runtime.get("thread_id") in visible]
        if visible_candidates and interop["status"] == "pass":
            status = "GO"
            reason = "런타임 내구성과 Desktop 양방향 상호운용성 조건을 모두 통과함"
        elif visible_candidates and interop["status"] == "pending":
            status = "PENDING-INTEROP-CHECK"
            reason = "런타임 내구성과 Desktop 표시는 확인했지만 양방향 상호운용성 증거가 남음"
        elif visible_candidates:
            status = "NO-GO"
            reason = "Desktop 양방향 상호운용성 필수 검사를 통과하지 못함"
        else:
            status = "NO-GO"
            reason = "통과 후보 런타임의 task가 Codex Desktop에서 확인되지 않음"
    return {
        "status": status,
        "reason": reason,
        "candidate_runtimes": [runtime.get("name") for runtime in candidates],
        "blocking_failures": [*failures, *interop["blocking_failures"]],
    }


def derive_permission_decision(document: dict[str, Any]) -> dict[str, Any]:
    runtimes = document.get("runtimes", [])
    has_legacy_policy = any(
        any(name in runtime.get("checks", {}) for name in LEGACY_PERMISSION_CHECKS)
        and "permission_configuration" not in runtime.get("checks", {})
        for runtime in runtimes
    )
    config_invalid = any(
        nested_get(runtime, "checks", "permission_configuration", "status")
        == "invalid_configuration"
        for runtime in runtimes
    )
    failures = collect_failures(document, PERMISSION_HARD_CHECKS, "0A-P")
    if has_legacy_policy:
        return {
            "status": "INVALID_CONFIGURATION",
            "reason": "schema 1.0 결과는 구형 sandbox와 permission profile 충돌을 preflight하지 않아 재검증이 필요함",
            "candidate_runtimes": [],
            "blocking_failures": [
                {
                    "subgate": "0A-P",
                    "runtime": runtime.get("name"),
                    "check": "permission_configuration",
                    "status": "invalid_configuration",
                    "summary": "schema 1.0은 permission profile 전용 설정을 입증하지 않음",
                }
                for runtime in runtimes
            ],
        }
    if config_invalid:
        return {
            "status": "INVALID_CONFIGURATION",
            "reason": "구형 sandbox 설정과 permission profile이 함께 로드되어 권한 primitive를 판정하지 않음",
            "candidate_runtimes": [],
            "blocking_failures": failures,
        }
    setup_required = any(
        nested_get(runtime, "checks", "windows_sandbox_operational", "status")
        == "setup_required"
        for runtime in runtimes
    )
    if setup_required:
        return {
            "status": "SETUP_REQUIRED",
            "reason": "실제 authoritative home의 elevated Windows sandbox 설정이 필요함",
            "candidate_runtimes": [],
            "blocking_failures": failures,
        }

    runtime_candidates = [
        runtime
        for runtime in runtimes
        if all(
            nested_get(runtime, "checks", name, "status") == "pass"
            for name in PERMISSION_HARD_CHECKS
        )
    ]
    recheck_candidates = [
        recheck
        for recheck in document.get("permission_rechecks", [])
        if isinstance(recheck, dict)
        and permission_recheck_is_candidate(document, recheck)
    ]
    if runtime_candidates or recheck_candidates:
        status = "GO"
        temporary_exception_used = any(
            nested_get(
                runtime,
                "checks",
                "undeclared_path_read_policy",
                "evidence",
                "temporary_exception_used",
            )
            is True
            for runtime in runtime_candidates
        ) or any(
            recheck.get("temporary_exception_used") is True
            for recheck in recheck_candidates
        )
        reason = (
            "외부 쓰기·보호 경로·네트워크 경계를 통과했고, 미등록 외부 읽기는 승인된 "
            "native Windows 임시 호환성 예외로 기록됨"
            if temporary_exception_used
            else "하나 이상의 런타임 조합이 엄격한 Gate 0A-P 권한 조건을 통과함"
        )
    else:
        status = "NO-GO"
        reason = "승인 리소스와 보호 경계를 구분하는 권한 primitive를 입증하지 못함"
    return {
        "status": status,
        "reason": reason,
        "candidate_runtimes": [runtime.get("name") for runtime in runtime_candidates]
        + [recheck.get("runtime_name") for recheck in recheck_candidates],
        "candidate_permission_rechecks": [
            recheck.get("recheck_id") for recheck in recheck_candidates
        ],
        "blocking_failures": []
        if runtime_candidates or recheck_candidates
        else failures,
    }


def derive_gate_decision(document: dict[str, Any]) -> dict[str, Any]:
    runtime = derive_runtime_decision(document)
    permission = derive_permission_decision(document)
    active_sandbox_incident = next(
        (
            audit
            for audit in document.get("evidence_audits", [])
            if audit.get("kind") == "sandbox_home_cross_contamination"
            and audit.get("status") in {"active", "mitigated_pending_revalidation"}
        ),
        None,
    )
    if active_sandbox_incident is not None:
        permission = {
            "status": "NO-GO",
            "reason": "임시 CODEX_HOME provisioning 사고로 기존 0A-P 증거가 무효화됐으며 재검증이 필요함",
            "candidate_runtimes": [],
            "blocking_failures": [
                {
                    "subgate": "0A-P",
                    "runtime": None,
                    "check": "sandbox_home_contract",
                    "status": "fail",
                    "summary": SANDBOX_HOME_CROSS_CONTAMINATION,
                    "audit_id": active_sandbox_incident.get("audit_id"),
                }
            ],
        }
    if permission["status"] == "INVALID_CONFIGURATION":
        status = "RETEST_REQUIRED"
        reason = "런타임 결과와 권한 정책 결과를 분리했으며, 권한 설정 정정 후 재검증이 필요함"
    elif runtime["status"] == "NO-GO" or permission["status"] == "NO-GO":
        status = "NO-GO"
        reason = "Gate 0A-R 또는 Gate 0A-P 필수 조건을 통과하지 못함"
    elif permission["status"] == "SETUP_REQUIRED":
        status = "SETUP_REQUIRED"
        reason = permission["reason"]
    elif runtime["status"] in {"PENDING-DESKTOP-CHECK", "PENDING-INTEROP-CHECK"}:
        status = runtime["status"]
        reason = runtime["reason"]
    elif runtime["status"] == "GO" and permission["status"] == "GO":
        status = "GO"
        reason = "Gate 0A-R과 Gate 0A-P를 모두 통과함"
    else:
        status = "RETEST_REQUIRED"
        reason = "하위 게이트 중 실행되지 않은 검사가 있어 재검증이 필요함"
    return {
        "status": status,
        "reason": reason,
        "subgates": {"runtime": runtime, "permission": permission},
        "candidate_runtimes": sorted(
            set(runtime.get("candidate_runtimes", []))
            & set(permission.get("candidate_runtimes", []))
        ),
        "blocking_failures": [
            *runtime.get("blocking_failures", []),
            *permission.get("blocking_failures", []),
        ],
    }


def markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(document: dict[str, Any]) -> str:
    decision = document.get("gate_decision", {})
    runtime_decision = nested_get(decision, "subgates", "runtime") or {}
    permission_decision = nested_get(decision, "subgates", "permission") or {}
    temporary_exception_runtimes = [
        str(runtime.get("name") or "unknown")
        for runtime in document.get("runtimes", [])
        if nested_get(
            runtime,
            "checks",
            "undeclared_path_read_policy",
            "evidence",
            "temporary_exception_used",
        )
        is True
    ]
    strict_read_runtimes = [
        str(runtime.get("name") or "unknown")
        for runtime in document.get("runtimes", [])
        if nested_get(
            runtime,
            "checks",
            "undeclared_path_read_policy",
            "evidence",
            "mode",
        )
        == "strict_read_isolation"
    ]
    lines = [
        "# FlowMarshal Gate 0A 기술 스파이크 보고서",
        "",
        f"- 실행 ID: `{document.get('run_id', '-')}`",
        f"- 실행 시각: `{document.get('started_at', '-')}`",
        f"- 판정: **{decision.get('status', 'UNKNOWN')}**",
        f"- 사유: {decision.get('reason', '-')}",
        f"- Gate 0A-R Runtime: **{runtime_decision.get('status', 'UNKNOWN')}** — {runtime_decision.get('reason', '-')}",
        f"- Gate 0A-P Permission: **{permission_decision.get('status', 'UNKNOWN')}** — {permission_decision.get('reason', '-')}",
        "",
        "## 결론",
        "",
    ]
    if decision.get("status") == "GO":
        if temporary_exception_runtimes:
            lines.append(
                "Gate 0A를 통과했다. 단, 미등록 외부 읽기는 native Windows 런타임 결함에 대한 "
                "승인된 임시 호환성 예외를 사용했다. Gate 0B는 별도 승인과 계획으로 진행할 수 있고, "
                "지원 런타임이 바뀌면 같은 카나리로 엄격 차단 복구 여부를 다시 확인한다."
            )
        else:
            lines.append("Gate 0A를 통과했다. 이후 Gate 0B는 별도 승인과 계획으로 진행할 수 있다.")
    elif decision.get("status") == "SETUP_REQUIRED":
        if runtime_decision.get("status") == "GO":
            lines.append(
                "Gate 0A-R은 통과했지만, 실제 authoritative home의 elevated Windows sandbox를 "
                "사용자가 관리자 PowerShell에서 직접 설정해야 한다. 설정 전에는 Gate 0B로 넘어가지 않는다."
            )
        else:
            lines.append(
                f"Gate 0A-R은 {runtime_decision.get('status', 'UNKNOWN')} 상태이고 Gate 0A-P에는 elevated Windows "
                "sandbox 설정이 필요하다. 두 하위 게이트를 모두 통과하기 전에는 Gate 0B로 넘어가지 않는다."
            )
    elif decision.get("status") == "PENDING-DESKTOP-CHECK":
        lines.append("런타임 검사는 통과했지만 Codex Desktop에서 생성 task가 보이는지 확인해야 한다. Gate 0B는 아직 시작하지 않는다.")
    elif decision.get("status") == "PENDING-INTEROP-CHECK":
        lines.append("런타임 내구성과 Desktop 표시는 확인했지만 양방향 상호운용성 증거가 남아 있다. Gate 0B는 아직 시작하지 않는다.")
    elif decision.get("status") == "RETEST_REQUIRED":
        lines.append(
            "기존 결과는 런타임 상호운용성과 파일 권한 정책을 분리하지 않았거나 설정 preflight에서 중단됐다. "
            "permission profile 전용 설정으로 Gate 0A-P를 다시 실행하기 전에는 Gate 0B로 넘어가지 않는다."
        )
    else:
        lines.append("Gate 0A를 통과하지 못했다. 아래 실패를 해소하기 전에는 Gate 0B 또는 제품 기능 구현으로 넘어가지 않는다.")

    policy = document.get("permission_policy")
    if not isinstance(policy, dict):
        policy = permission_policy_contract()
    exception = policy.get("temporary_exception", {})
    lines.extend(
        [
            "",
            "## 외부 읽기 호환성 정책",
            "",
            "- 목표 상태: 등록된 외부 참조만 읽고 미등록 외부 경로는 OS sandbox가 차단한다.",
            "- 현재 설정: `:root=deny`와 미등록 경로 카나리를 유지한다.",
            (
                "- 현재 실측: 다음 런타임에서 임시 예외가 사용됐다 — "
                + ", ".join(f"`{item}`" for item in temporary_exception_runtimes)
                if temporary_exception_runtimes
                else (
                    "- 현재 실측: 엄격 읽기 차단이 집행됐다 — "
                    + ", ".join(f"`{item}`" for item in strict_read_runtimes)
                    if strict_read_runtimes
                    else "- 현재 실측: 읽기 호환성 모드를 확인하지 못했다."
                )
            ),
            f"- 임시 예외 ID: `{exception.get('id', UNDECLARED_READ_EXCEPTION_ID)}`",
            f"- 재검토 조건: {exception.get('review_trigger', UNDECLARED_READ_REVIEW_TRIGGER)}",
            "- 이 예외는 프로젝트 밖 쓰기, 보호 경로 읽기·쓰기, 명령 네트워크와 외부 도구 표면 차단에는 적용되지 않는다.",
            "- 현재 native Windows 모드는 파일 내용의 완전한 비밀 보장 경계가 아니라 사용자 데이터 훼손 방지 경계로 취급한다.",
        ]
    )

    legacy = document.get("legacy_gate_decision")
    if isinstance(legacy, dict):
        lines.extend(
            [
                "",
                "## 정정 이력",
                "",
                f"- schema 1.0 당시 판정: `{legacy.get('status', 'UNKNOWN')}`",
                f"- 당시 사유: {legacy.get('reason', '-')}",
                "- 당시의 ‘프로젝트 밖 읽기’ 실패는 외부 경로 전체를 금지한 잘못된 제품 기준과 혼합 sandbox 설정에 근거하므로 권한 정책의 기술적 실패로 사용하지 않는다.",
                "- 원본 JSON과 보고서는 `spikes/gate0a/artifacts/legacy/`에 보존한다.",
            ]
        )

    for runtime in document.get("runtimes", []):
        lines.extend(
            [
                "",
                f"## 런타임: {runtime.get('name')}",
                "",
                f"- 역할: {runtime.get('role')}",
                f"- 실행 파일: `{runtime.get('executable')}`",
                f"- 버전: `{nested_get(runtime, 'version', 'stdout') or '확인 실패'}`",
                f"- 생성 task ID: `{runtime.get('thread_id') or '없음'}`",
                f"- 생성 task 이름: {runtime.get('thread_title') or '없음'}",
                "",
                "| 검사 | 상태 | 요약 |",
                "|---|---:|---|",
            ]
        )
        for name, outcome in runtime.get("checks", {}).items():
            lines.append(
                f"| `{markdown_escape(name)}` | {markdown_escape(outcome.get('status'))} | {markdown_escape(outcome.get('summary'))} |"
            )

    desktop = document.get("desktop_visibility", {})
    lines.extend(
        [
            "",
            "## Codex Desktop 표시 확인",
            "",
            f"- 상태: `{desktop.get('status', 'pending')}`",
            f"- 확인 방법: {desktop.get('checked_via') or '아직 확인하지 않음'}",
            f"- 확인된 task ID: {', '.join(f'`{item}`' for item in desktop.get('visible_thread_ids', [])) or '없음'}",
            f"- 누락 task ID: {', '.join(f'`{item}`' for item in desktop.get('missing_thread_ids', [])) or '없음'}",
        ]
    )

    interop = document.get("desktop_interop", {})
    lines.extend(
        [
            "",
            "## Codex Desktop 양방향 상호운용성",
            "",
            f"- 상태: `{interop.get('status', 'pending')}`",
            f"- 확인 방법: {interop.get('checked_via') or '신뢰된 검증기 미구현'}",
            f"- Desktop 버전: `{interop.get('desktop_version') or '미기록'}`",
            f"- Desktop 생성 task ID: `{interop.get('desktop_thread_id') or '미기록'}`",
            "",
            "| 검사 | 상태 | 요약 |",
            "|---|---:|---|",
        ]
    )
    interop_checks = interop.get("checks") if isinstance(interop.get("checks"), dict) else {}
    for name in DESKTOP_INTEROP_HARD_CHECKS:
        outcome = interop_checks.get(name, check("not_run", "신뢰된 검증 필요"))
        lines.append(
            f"| `{markdown_escape(name)}` | {markdown_escape(outcome.get('status'))} | {markdown_escape(outcome.get('summary'))} |"
        )

    failures = decision.get("blocking_failures", [])
    if failures:
        lines.extend(["", "## 차단 항목", ""])
        for failure in failures:
            lines.append(
                f"- `{failure.get('subgate', '0A')}/{failure.get('runtime')}/{failure.get('check')}`: "
                f"{failure.get('status')} — {failure.get('summary')}"
            )

    audits = document.get("evidence_audits", [])
    if audits:
        lines.extend(["", "## 증거 감사 기록", ""])
        for audit in audits:
            lines.append(
                f"- `{audit.get('audit_id', 'unknown')}`: {audit.get('status', 'recorded')} — "
                f"{audit.get('summary') or '상세 감사 파일 참조'} "
                f"(`{audit.get('path', '-')}`)"
            )

    attempts = document.get("desktop_interop_attempts", [])
    if attempts:
        lines.extend(["", "## Desktop 상호운용성 시도 기록", ""])
        for attempt in attempts:
            detail = attempt.get("error_type") or "필수 검사 평가 완료"
            lines.append(
                f"- `{attempt.get('verification_id', 'unknown')}`: "
                f"{attempt.get('status', 'unknown')} / "
                f"{attempt.get('runtime_name') or 'unknown'} "
                f"`{attempt.get('runtime_version') or '버전 미기록'}` — {detail} "
                f"(`{attempt.get('path', '-')}`)"
            )

    permission_rechecks = document.get("permission_rechecks", [])
    if permission_rechecks:
        lines.extend(["", "## 권한 카나리 재검사 기록", ""])
        for recheck in permission_rechecks:
            lines.append(
                f"- `{recheck.get('recheck_id', 'unknown')}`: "
                f"{recheck.get('status', 'unknown')} / "
                f"{recheck.get('runtime_name') or 'unknown'} "
                f"`{recheck.get('runtime_version') or '버전 미기록'}` "
                f"(`{recheck.get('path', '-')}`)"
            )

    lines.extend(
        [
            "",
            "## 범위와 주의사항",
            "",
            "- 이 결과는 합성 카나리와 현재 설치된 로컬 런타임 조합에만 적용된다.",
            "- 실제 사용자 파일의 내용은 카나리로 사용하지 않았다. 결과 JSON에는 카나리 원문 대신 SHA-256과 치환된 출력만 저장한다.",
            "- 승인된 외부 참조는 정상 입력으로 등록한다. 현재 임시 예외가 미등록 읽기를 기술적으로 가능하게 해도 등록·digest·출처 기록 계약을 생략하지 않는다.",
            "- 미등록 외부 읽기 이외의 permission profile 필수 경계가 미지원이거나 판정 불명확하면 Gate 0A-P를 실패 처리한다.",
            "- 현재 구현은 Gate 0A뿐이며 SQLite 원장, Planner, 스케줄러, 복구 엔진, 전체 CLI, Skill/Starter를 포함하지 않는다.",
            "",
            "## 공식 계약",
            "",
            "- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)",
            "- [Codex App Server](https://learn.chatgpt.com/docs/app-server)",
            "- [Permissions](https://learn.chatgpt.com/docs/permissions)",
            "- [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)",
            "- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)",
            "",
        ]
    )
    return "\n".join(lines)


def archive_pre_migration_outputs(
    root: Path,
    document: dict[str, Any],
) -> dict[str, Any] | None:
    previous_version = str(document.get("schema_version") or "unknown")
    if previous_version == SCHEMA_VERSION:
        return None
    run_id = safe_artifact_component(document.get("run_id"))
    version_label = safe_artifact_component(previous_version)
    archive_root = artifact_root(root) / "legacy"
    archive_root.mkdir(parents=True, exist_ok=True)
    sources: dict[str, dict[str, Any]] = {}
    result_bytes = results_path(root).read_bytes() if results_path(root).is_file() else b""
    result_digest = sha256_bytes(result_bytes) if result_bytes else "missing"
    base = f"gate0a-{run_id}-schema-{version_label}-{result_digest[:12]}"
    for key, source, suffix in (
        ("results", results_path(root), "results.json"),
        ("report", report_path(root), "report.md"),
    ):
        if not source.is_file():
            sources[key] = {"available": False}
            continue
        content = source.read_bytes()
        target = archive_root / f"{base}-{suffix}"
        if target.exists() and target.read_bytes() != content:
            raise RuntimeError(f"마이그레이션 원본 보관 파일 충돌: {target}")
        if not target.exists():
            target.write_bytes(content)
        sources[key] = {
            "available": True,
            "sha256": sha256_bytes(content),
            "bytes": len(content),
            "archive_path": target.relative_to(root).as_posix(),
        }

    receipt = {
        "receipt_schema_version": AUDIT_SCHEMA_VERSION,
        "kind": "gate0a_schema_migration",
        "run_id": str(document.get("run_id") or "unknown"),
        "from_schema_version": previous_version,
        "to_schema_version": SCHEMA_VERSION,
        "archived_at": utc_now(),
        "previous_gate_decision": json_safe(document.get("gate_decision", {})),
        "sources": sources,
    }
    receipt_path = archive_root / f"{base}-migration-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    receipt["receipt_path"] = receipt_path.relative_to(root).as_posix()
    return receipt


def migrate_document(
    document: dict[str, Any],
    migration_receipt: dict[str, Any] | None = None,
) -> None:
    previous_version = str(document.get("schema_version"))
    if previous_version == "1.0":
        document.setdefault("legacy_gate_decision", json_safe(document.get("gate_decision", {})))
        document["policy_reassessment"] = {
            "reassessed_at": utc_now(),
            "reason": "런타임 상호운용성과 역할·리소스 기반 파일 권한 primitive를 분리함",
            "legacy_blanket_outside_read_is_authoritative": False,
        }
    if previous_version in {"1.0", "1.1", "1.2"}:
        document["schema_version"] = SCHEMA_VERSION
    for runtime in document.get("runtimes", []):
        checks = runtime.get("checks")
        if isinstance(checks, dict):
            # schema 1.2의 기준은 원본 마이그레이션 보관물에 남긴다. 새 보고서에
            # 폐기된 실패 항목이 현재 필수 검사처럼 보이지 않도록 제거한다.
            checks.pop("undeclared_path_read_blocked", None)
    document.setdefault("desktop_interop", pending_desktop_interop())
    if migration_receipt is not None:
        history = document.setdefault("migration_history", [])
        receipt_path_value = migration_receipt.get("receipt_path")
        if not any(item.get("receipt_path") == receipt_path_value for item in history):
            history.append(json_safe(migration_receipt))


def write_outputs(root: Path, document: dict[str, Any]) -> None:
    output_root = artifact_root(root)
    output_root.mkdir(parents=True, exist_ok=True)
    migration_receipt = archive_pre_migration_outputs(root, document)
    migrate_document(document, migration_receipt)
    document["permission_policy"] = permission_policy_contract()
    document["desktop_interop"]["status"] = evaluate_desktop_interop(document)["status"]
    document["evidence_audits"] = load_evidence_audits(root)
    document["desktop_interop_attempts"] = load_interop_attempts(root)
    document["permission_rechecks"] = load_permission_rechecks(root)
    sanitize_document_for_storage(document)
    document["gate_decision"] = derive_gate_decision(document)
    results_path(root).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path(root).write_text(render_report(document), encoding="utf-8")


def sandbox_status(args: argparse.Namespace) -> int:
    """실제 Codex 홈의 Windows sandbox 준비상태를 변경 없이 조회한다."""

    root = Path(args.project_root).resolve() if args.project_root else project_root()
    try:
        context = resolve_host_context(
            project_root=root,
            authoritative_home=args.authoritative_codex_home,
        )
    except SandboxContractError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "reason_code": exc.reason_code, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    result = query_windows_sandbox_status(context, cwd=root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "READY":
        return 0
    if result.get("status") == "SETUP_REQUIRED":
        return 3
    return 2


def run_probe(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve() if args.project_root else project_root()
    if args.runtime != "system":
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason_code": HOST_RUNTIME_NOT_SYSTEM,
                    "message": "실제 PC의 Gate 0A 실행은 현재 시스템 Codex만 사용할 수 있습니다.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        host_context = resolve_host_context(
            project_root=root,
            authoritative_home=getattr(args, "authoritative_codex_home", None),
        )
    except SandboxContractError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "reason_code": exc.reason_code, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    fingerprint_before = capture_sandbox_fingerprint(host_context.authoritative_home)
    preflight = query_windows_sandbox_status(host_context, cwd=root)
    if preflight.get("status") != "READY" or not fingerprint_is_complete(fingerprint_before):
        if not fingerprint_is_complete(fingerprint_before):
            preflight = {
                **preflight,
                "status": "ERROR",
                "reason_code": SANDBOX_STATE_CHANGED_DURING_PROBE,
                "fingerprint": fingerprint_before.as_dict(),
            }
        print(json.dumps(preflight, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3 if preflight.get("status") == "SETUP_REQUIRED" else 2

    run_id = args.run_id or f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    specs = runtime_specs(args.runtime)
    if not specs:
        print("요청한 시스템 Codex 런타임을 찾지 못했습니다.", file=sys.stderr)
        return 2

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project": PROJECT_NAME,
        "gate": GATE_NAME,
        "run_id": run_id,
        "started_at": utc_now(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
            "python_executable": sys.executable,
            "sdk_version": openai_codex.__version__,
            "project_root": str(root),
            "config_strategy": (
                "authoritative host home + system Codex + read-only readiness preflight"
            ),
            "authoritative_codex_home": str(host_context.authoritative_home),
            "system_codex": str(host_context.system_codex),
            "sandbox_preflight": preflight,
            "base_config_overrides": list(BASE_CONFIG_OVERRIDES),
        },
        "runtimes": [],
        "desktop_visibility": {
            "status": "pending",
            "checked_at": None,
            "checked_via": None,
            "visible_thread_ids": [],
            "missing_thread_ids": [],
        },
        "desktop_interop": pending_desktop_interop(),
    }

    for spec in specs:
        print(f"[{spec.name}] Gate 0A 검사를 시작합니다.", flush=True)
        runtime = probe_runtime(
            spec,
            root,
            run_id,
            args.turn_timeout,
            host_context,
            fingerprint_before,
        )
        document["runtimes"].append(runtime)
        print(
            f"[{spec.name}] 완료: task={runtime.get('thread_id') or '-'}",
            flush=True,
        )
        write_outputs(root, document)

    document["completed_at"] = utc_now()
    write_outputs(root, document)
    print(str(results_path(root)), flush=True)
    print(str(report_path(root)), flush=True)
    return 0 if document["gate_decision"]["status"] in {
        "GO",
        "PENDING-DESKTOP-CHECK",
        "PENDING-INTEROP-CHECK",
    } else 1


def setup_permission_sandbox(args: argparse.Namespace) -> int:
    """호스트에서는 안내만 하고, 이중 확인된 폐기 VM에서만 setup을 실행한다."""

    root = Path(args.project_root).resolve() if args.project_root else project_root()
    if not disposable_vm_confirmed(args.environment):
        payload: dict[str, Any] = {
            "status": "EXTERNAL_SETUP_REQUIRED",
            "reason_code": HOST_PROVISIONING_FORBIDDEN,
            "message": "실제 PC에서 FlowMarshal의 자동 Windows sandbox provisioning은 금지됩니다.",
        }
        try:
            context = resolve_host_context(
                project_root=root,
                authoritative_home=args.authoritative_codex_home,
            )
            payload["setup_command"] = manual_setup_command(context)
            payload["context"] = context.public_dict()
        except SandboxContractError as exc:
            payload["contract_error"] = {
                "reason_code": exc.reason_code,
                "message": str(exc),
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3

    if args.authoritative_codex_home is None:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason_code": AUTHORITATIVE_HOME_INVALID,
                    "message": "폐기 VM 모드에서는 --authoritative-codex-home을 명시해야 합니다.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    vm_home = Path(args.authoritative_codex_home).expanduser().resolve(strict=False)
    if vm_home == default_authoritative_home().expanduser().resolve(strict=False):
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason_code": HOST_PROVISIONING_FORBIDDEN,
                    "message": "폐기 VM 검사는 호스트의 기본 Codex 홈을 대상으로 실행할 수 없습니다.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    vm_suite_id = getattr(args, "vm_suite_id", None)
    vm_scenario = getattr(args, "vm_scenario", None)
    vm_attempt_id = getattr(args, "vm_attempt_id", None)
    if not vm_suite_id or vm_scenario not in VM_SCENARIOS or not vm_attempt_id:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason_code": VM_SUITE_INVALID,
                    "message": "폐기 VM에서는 --vm-suite-id, --vm-scenario, --vm-attempt-id를 모두 명시해야 합니다.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        validate_component(vm_suite_id, label="vm-suite-id")
        validate_component(vm_attempt_id, label="vm-attempt-id")
        scenario_path = vm_scenario_root(
            artifact_root(root), vm_suite_id, vm_scenario
        )
    except VmSuiteError as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason_code": exc.reason_code,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    contract_path = scenario_path / "scenario.json"
    if contract_path.exists():
        try:
            contract = read_json_object(contract_path)
        except VmSuiteError as exc:
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "reason_code": exc.reason_code,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        claimed_home = Path(
            str(contract.get("authoritative_codex_home", ""))
        ).resolve(strict=False)
        if claimed_home != vm_home:
            guard_path = scenario_path / "guards" / f"{vm_attempt_id}.json"
            if guard_path.exists():
                print(f"이미 존재하는 VM guard 증거입니다: {guard_path}", file=sys.stderr)
                return 2
            write_json_object(
                guard_path,
                {
                    "schema_version": VM_SUITE_SCHEMA_VERSION,
                    "kind": "disposable_windows_vm_second_home_rejection",
                    "suite_id": vm_suite_id,
                    "scenario": vm_scenario,
                    "attempt_id": vm_attempt_id,
                    "recorded_at": utc_now(),
                    "status": "rejected",
                    "reason_code": HOST_PROVISIONING_FORBIDDEN,
                    "claimed_home": str(claimed_home),
                    "requested_home": str(vm_home),
                    "runtime_started": False,
                    "setup_api_called": False,
                    "secret_contents_read": False,
                },
            )
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "reason_code": HOST_PROVISIONING_FORBIDDEN,
                        "message": "한 VM 시나리오에서는 하나의 authoritative home만 사용할 수 있습니다.",
                        "claimed_home": str(claimed_home),
                        "requested_home": str(vm_home),
                        "guard_evidence": str(guard_path),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
    else:
        initial_fingerprint = capture_sandbox_fingerprint(vm_home).as_dict()
        vm_identity = capture_windows_vm_identity()
        if not fingerprint_is_clean(initial_fingerprint):
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "reason_code": VM_SCENARIO_NOT_CLEAN,
                        "message": "VM 시나리오는 샌드박스 사용자·marker·secrets가 없는 깨끗한 snapshot에서 시작해야 합니다.",
                        "initial_fingerprint": initial_fingerprint,
                        "secret_contents_read": False,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        if vm_identity.get("recognized_virtual_machine") is not True:
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "reason_code": HOST_PROVISIONING_FORBIDDEN,
                        "message": "가상 머신으로 인식되지 않는 환경에서는 provisioning을 실행하지 않습니다.",
                        "vm_identity": vm_identity,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        contract = {
            "schema_version": VM_SUITE_SCHEMA_VERSION,
            "kind": "disposable_windows_vm_scenario_contract",
            "suite_id": vm_suite_id,
            "scenario": vm_scenario,
            "created_at": utc_now(),
            "authoritative_codex_home": str(vm_home),
            "environment_guard": "--environment disposable-vm",
            "environment_confirmation": "FLOWMARSHAL_DISPOSABLE_WINDOWS_VM=1",
            "vm_identity": vm_identity,
            "initial_fingerprint": initial_fingerprint,
            "initial_state_clean": True,
            "secret_contents_read": False,
        }
        write_json_object(contract_path, contract)

    specs = runtime_specs(args.runtime)
    if not specs:
        print("요청한 Codex 런타임을 찾지 못했습니다.", file=sys.stderr)
        return 2
    spec = specs[0]
    try:
        existing_attempts = load_attempts(scenario_path)
        sequence = validate_attempt_request(
            vm_scenario,
            existing_attempts,
            runtime_name=spec.name,
            inject_failure=bool(getattr(args, "simulate_setup_failure", False)),
        )
    except VmSuiteError as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason_code": exc.reason_code,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    vm_attempt_path = scenario_path / "attempts" / vm_attempt_id / "setup-result.json"
    if vm_attempt_path.exists():
        print(f"이미 존재하는 VM setup Attempt입니다: {vm_attempt_path}", file=sys.stderr)
        return 2

    def record_vm_attempt(
        status: str,
        *,
        readiness_before: dict[str, Any] | None = None,
        readiness_after: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        setup_api_called: bool = False,
        fingerprint_before: SandboxFingerprint | None = None,
        fingerprint_after: SandboxFingerprint | None = None,
    ) -> None:
        write_json_object(
            vm_attempt_path,
            {
                "schema_version": VM_SUITE_SCHEMA_VERSION,
                "kind": "disposable_windows_vm_sandbox_setup_attempt",
                "suite_id": vm_suite_id,
                "scenario": vm_scenario,
                "attempt_id": vm_attempt_id,
                "sequence": sequence,
                "recorded_at": utc_now(),
                "status": status,
                "runtime": {
                    "name": spec.name,
                    "executable": str(spec.executable),
                    "version": runtime_version(spec.executable),
                },
                "authoritative_codex_home": str(vm_home),
                "readiness_before": readiness_before,
                "readiness_after": readiness_after,
                "error": error,
                "setup_api_called": setup_api_called,
                "fingerprint_before": (
                    fingerprint_before.as_dict() if fingerprint_before else None
                ),
                "fingerprint_after": (
                    fingerprint_after.as_dict() if fingerprint_after else None
                ),
                "secret_contents_read": False,
                "vm_reset_required_after_suite": True,
            },
        )

    fingerprint_before = capture_sandbox_fingerprint(vm_home)
    if getattr(args, "simulate_setup_failure", False):
        record_vm_attempt(
            "failed",
            error={
                "reason_code": VM_TEST_INJECTED_SETUP_FAILURE,
                "type": "InjectedFailure",
                "message": "제한된 재시도 경로 검증을 위한 결정적 결함 주입",
            },
            setup_api_called=False,
            fingerprint_before=fingerprint_before,
            fingerprint_after=capture_sandbox_fingerprint(vm_home),
        )
        print("폐기 VM 재시도 검사용 결함을 1회 기록했습니다.", file=sys.stderr)
        return 1

    run_component = f"vm-{vm_suite_id}-{vm_scenario}-{vm_attempt_id}"
    layout = canary_layout(root, run_component, spec.name, vm_home)
    layout.workspace.mkdir(parents=True, exist_ok=True)
    layout.permission_home.mkdir(parents=True, exist_ok=True)
    client: CodexClient | None = None
    setup_api_called = False
    try:
        client = make_client(
            spec,
            layout.workspace,
            FailClosedApprovalHandler(),
            build_config_overrides(layout),
            env={"CODEX_HOME": str(layout.permission_home)},
        )
        client.start()
        client.initialize()
        before = read_windows_sandbox_readiness(client)
        if nested_get(before, "result", "status") == "ready":
            record_vm_attempt(
                "already_ready",
                readiness_before=before,
                readiness_after=before,
                setup_api_called=False,
                fingerprint_before=fingerprint_before,
                fingerprint_after=capture_sandbox_fingerprint(vm_home),
            )
            print("폐기 VM의 elevated Windows sandbox가 이미 준비돼 있습니다.")
            return 0
        started = client._request_raw(  # noqa: SLF001 - 명시적 setup App Server API 호출이다.
            "windowsSandbox/setupStart",
            {"mode": args.mode, "cwd": str(layout.workspace)},
        )
        setup_api_called = True
        print(f"Windows sandbox 설정 시작: {json_safe(started)}", flush=True)
        deadline = time.monotonic() + args.timeout
        last_status: str | None = None
        while time.monotonic() < deadline:
            observation = read_windows_sandbox_readiness(client)
            status = nested_get(observation, "result", "status")
            if status != last_status:
                print(f"Windows sandbox 준비 상태: {status or 'error'}", flush=True)
                last_status = status
            if status == "ready":
                record_vm_attempt(
                    "completed",
                    readiness_before=before,
                    readiness_after=observation,
                    setup_api_called=True,
                    fingerprint_before=fingerprint_before,
                    fingerprint_after=capture_sandbox_fingerprint(vm_home),
                )
                print("폐기 VM의 elevated Windows sandbox 설정이 완료됐습니다.")
                return 0
            time.sleep(1)
        record_vm_attempt(
            "timeout",
            readiness_before=before,
            readiness_after=observation if "observation" in locals() else None,
            setup_api_called=setup_api_called,
            fingerprint_before=fingerprint_before,
            fingerprint_after=capture_sandbox_fingerprint(vm_home),
        )
        print("Windows sandbox 설정 완료를 제한 시간 안에 확인하지 못했습니다.", file=sys.stderr)
        return 1
    except BaseException as exc:
        record_vm_attempt(
            "failed",
            error=exception_record(exc),
            setup_api_called=setup_api_called,
            fingerprint_before=fingerprint_before,
            fingerprint_after=capture_sandbox_fingerprint(vm_home),
        )
        print(
            f"Windows sandbox 설정 실패: {exception_record(exc).get('message')}",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()


def record_vm_disposal(args: argparse.Namespace) -> int:
    """VM 관리 계층에서 확인한 초기화·폐기 사실을 suite에 연결한다."""

    root = Path(args.project_root).resolve() if args.project_root else project_root()
    try:
        path = vm_suite_root(artifact_root(root), args.vm_suite_id)
    except VmSuiteError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "reason_code": exc.reason_code, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if not path.exists():
        print("먼저 실제 VM 시나리오 증거를 가져와야 합니다.", file=sys.stderr)
        return 2
    lifecycle_path = path / "lifecycle" / "disposal.json"
    if lifecycle_path.exists():
        print(f"VM 생명주기 증거가 이미 존재합니다: {lifecycle_path}", file=sys.stderr)
        return 2
    write_json_object(
        lifecycle_path,
        {
            "schema_version": VM_SUITE_SCHEMA_VERSION,
            "kind": "disposable_windows_vm_lifecycle_attestation",
            "suite_id": args.vm_suite_id,
            "recorded_at": utc_now(),
            "action": args.action,
            "provider": args.provider,
            "vm_identifier": args.vm_identifier,
            "evidence_reference": args.evidence_reference,
            "attestation": "검사에 사용한 VM을 마지막 Attempt 뒤 초기화하거나 폐기했습니다.",
            "secret_contents_read": False,
        },
    )
    print(str(lifecycle_path))
    return 0


def verify_vm_suite_evidence(args: argparse.Namespace) -> int:
    """반출된 VM 증거를 검사하되 Gate 판정 파일은 자동 변경하지 않는다."""

    root = Path(args.project_root).resolve() if args.project_root else project_root()
    try:
        result = evaluate_vm_suite(artifact_root(root), args.vm_suite_id)
        result_path = (
            vm_suite_root(artifact_root(root), args.vm_suite_id) / "suite-result.json"
        )
        write_json_object(result_path, result)
    except VmSuiteError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "reason_code": exc.reason_code, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(str(result_path))
    if result["status"] == "GO":
        return 0
    if result["status"] == "PENDING":
        return 3
    return 1


def run_permission_recheck(
    spec: RuntimeSpec,
    root: Path,
    run_id: str,
    existing_thread_id: str | None,
    host_context: SandboxHostContext,
    fingerprint_before: SandboxFingerprint,
) -> dict[str, Any]:
    """모델 turn을 만들지 않고 0A-P 검사만 새로 실행한다."""

    permission_handler = FailClosedApprovalHandler()
    approval_validation_handler = FailClosedApprovalHandler()
    result: dict[str, Any] = {
        "runtime": {
            "name": spec.name,
            "role": spec.role,
            "executable": str(spec.executable),
            "version": runtime_version(spec.executable),
        },
        "started_at": utc_now(),
        "checks": {
            "approvals_fail_closed": validate_fail_closed_handler(
                approval_validation_handler
            )
        },
        "evidence": {},
    }
    layout = canary_layout(
        root,
        run_id,
        spec.name,
        host_context.authoritative_home,
    )
    tokens: list[str] = []
    configuration_status: str | None = None
    readiness_status: str | None = None
    permission_client: CodexClient | None = None
    try:
        layout.workspace.mkdir(parents=True, exist_ok=True)
        permission_client = make_client(
            spec,
            layout.workspace,
            permission_handler,
            build_config_overrides(layout),
            env={"CODEX_HOME": str(layout.permission_home)},
        )
        permission_client.start()
        result["evidence"]["permission_initialize"] = json_safe(
            permission_client.initialize()
        )
        config_observation = read_effective_config(permission_client, layout.workspace)
        configuration_check = evaluate_permission_configuration(config_observation)
        configuration_status = configuration_check.get("status")
        result["checks"]["permission_configuration"] = configuration_check
        result["checks"]["windows_elevated_sandbox"] = effective_windows_elevated(
            {"effective_config": config_observation}
        )
        result["evidence"]["configuration_preflight"] = {
            "check": configuration_check,
            "requested_profile": PERMISSION_PROFILE_ID,
            "authoritative_codex_home": str(layout.permission_home),
            "authentication_copied": False,
            "config_observation": sanitize_config_observation(config_observation),
        }

        if configuration_status == "pass":
            readiness_observation = read_windows_sandbox_readiness(permission_client)
            readiness_check = evaluate_windows_sandbox_readiness(
                readiness_observation,
                setup_command=manual_setup_command(host_context),
            )
            readiness_status = readiness_check.get("status")
            result["checks"]["windows_sandbox_operational"] = readiness_check
            result["evidence"]["windows_sandbox_readiness"] = readiness_observation

            profile_check, profile_inventory, _ = inspect_permission_profile(
                permission_client,
                layout.workspace,
            )
            result["checks"]["permission_profile_supported"] = profile_check
            result["evidence"]["permission_profile_inventory"] = profile_inventory
            if readiness_status == "pass" and profile_check.get("status") == "pass":
                sandbox_checks, sandbox_evidence, tokens = run_sandbox_checks(
                    permission_client,
                    layout,
                    profile_inventory,
                )
                result["checks"].update(sandbox_checks)
                result["evidence"]["sandbox"] = sandbox_evidence
    except BaseException as exc:
        result["permission_error"] = exception_record(exc)
        result["checks"].setdefault(
            "permission_configuration",
            check(
                "error",
                "authoritative home permission 세션 초기화 또는 preflight 실패",
                error=exception_record(exc),
            ),
        )
        configuration_status = result["checks"]["permission_configuration"].get(
            "status"
        )
    finally:
        if permission_client is not None:
            permission_client.close()

    surface_handler = FailClosedApprovalHandler()
    surface_discovery: CodexClient | None = None
    surface_client: CodexClient | None = None
    if configuration_status == "pass" and existing_thread_id:
        try:
            surface_discovery = make_client(
                spec,
                layout.workspace,
                surface_handler,
                BASE_CONFIG_OVERRIDES,
                env={"CODEX_HOME": str(host_context.authoritative_home)},
            )
            surface_discovery.start()
            surface_discovery.initialize()
            config_observation = read_effective_config(
                surface_discovery,
                layout.workspace,
            )
            surface_preflight, mcp_overrides = build_mcp_disable_overrides(
                config_observation
            )
            result["evidence"]["runtime_surface_preflight"] = {
                "check": surface_preflight,
                "mcp_disable_overrides": list(mcp_overrides),
            }
            if surface_preflight.get("status") != "pass":
                raise RuntimeError("외부 도구 표면 preflight를 통과하지 못했습니다.")
            surface_discovery.close()
            surface_discovery = None

            surface_client = make_client(
                spec,
                layout.workspace,
                surface_handler,
                (*BASE_CONFIG_OVERRIDES, *mcp_overrides),
                env={"CODEX_HOME": str(host_context.authoritative_home)},
            )
            surface_client.start()
            surface_client.initialize()
            raw_thread_resume(
                surface_client,
                existing_thread_id,
                layout.workspace,
            )
            surface_check, surface_observations = inspect_external_surfaces(
                surface_client,
                layout.workspace,
                existing_thread_id,
            )
            result["checks"]["external_surfaces_disabled"] = surface_check
            result["evidence"]["external_surfaces"] = sanitize_surface_observations(
                surface_observations
            )
        except BaseException as exc:
            result["checks"]["external_surfaces_disabled"] = check(
                "error",
                "외부 도구 표면 비활성화 재검사 실패",
                error=exception_record(exc),
            )
        finally:
            if surface_discovery is not None:
                surface_discovery.close()
            if surface_client is not None:
                surface_client.close()
    elif not existing_thread_id:
        result["checks"]["external_surfaces_disabled"] = check(
            "error",
            "외부 표면 검증에 사용할 기존 runtime task ID가 없음",
        )

    fingerprint_after = capture_sandbox_fingerprint(host_context.authoritative_home)
    fingerprint_ok = (
        fingerprint_is_complete(fingerprint_before)
        and fingerprint_is_complete(fingerprint_after)
        and fingerprints_match(fingerprint_before, fingerprint_after)
    )
    result["checks"]["sandbox_state_unchanged"] = check(
        "pass" if fingerprint_ok else "fail",
        "권한 재검사 전후 샌드박스 상태가 동일함"
        if fingerprint_ok
        else "권한 재검사 중 샌드박스 상태가 변경됐거나 지문을 읽지 못함",
        reason_code=None if fingerprint_ok else SANDBOX_STATE_CHANGED_DURING_PROBE,
    )
    result["evidence"]["sandbox_state_fingerprint"] = {
        "before": fingerprint_before.as_dict(),
        "after": fingerprint_after.as_dict(),
        "secret_contents_read": False,
    }
    unsafe_artifacts = artifact_safety_violations(artifact_root(root))
    result["checks"]["artifact_safety"] = check(
        "pass" if not unsafe_artifacts else "fail",
        "프로젝트 artifacts에 Codex 인증·상태 저장소가 없음"
        if not unsafe_artifacts
        else "프로젝트 artifacts에 격리해야 할 과거 Codex 상태 저장소가 남아 있음",
        violations=unsafe_artifacts,
    )

    if configuration_status == "invalid_configuration":
        missing_status = "not_run"
        missing_summary = "설정 preflight 실패로 검사를 실행하지 않음"
    elif readiness_status == "setup_required":
        missing_status = "not_run"
        missing_summary = "Windows sandbox 설정이 필요해 카나리 검사를 실행하지 않음"
    else:
        missing_status = "error"
        missing_summary = "선행 실패로 권한 검사를 완료하지 못함"
    for name in PERMISSION_HARD_CHECKS:
        result["checks"].setdefault(name, check(missing_status, missing_summary))

    result["evidence"]["approval_requests"] = [
        *permission_handler.requests,
        *surface_handler.requests,
    ]
    result["evidence"]["session_isolation"] = {
        "authoritative_codex_home": str(layout.permission_home),
        "permission_authentication_copied": False,
        "permission_model_turns_created": False,
        "runtime_thread_id_for_surface_read": existing_thread_id,
    }
    result["evidence"] = redact_tokens(result["evidence"], tokens)
    result["completed_at"] = utc_now()
    result["status"] = (
        "pass"
        if all(
            nested_get(result, "checks", name, "status") == "pass"
            for name in PERMISSION_HARD_CHECKS
        )
        else "fail"
    )
    return result


def verify_permission(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve() if args.project_root else project_root()
    if args.runtime != "system":
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason_code": HOST_RUNTIME_NOT_SYSTEM,
                    "message": "실제 PC의 권한 재검사는 현재 시스템 Codex만 사용할 수 있습니다.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        host_context = resolve_host_context(
            project_root=root,
            authoritative_home=args.authoritative_codex_home,
        )
    except SandboxContractError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "reason_code": exc.reason_code, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    fingerprint_before = capture_sandbox_fingerprint(host_context.authoritative_home)
    preflight = query_windows_sandbox_status(host_context, cwd=root)
    if preflight.get("status") != "READY" or not fingerprint_is_complete(fingerprint_before):
        if not fingerprint_is_complete(fingerprint_before):
            preflight = {
                **preflight,
                "status": "ERROR",
                "reason_code": SANDBOX_STATE_CHANGED_DURING_PROBE,
                "fingerprint": fingerprint_before.as_dict(),
            }
        print(json.dumps(preflight, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3 if preflight.get("status") == "SETUP_REQUIRED" else 2

    path = results_path(root)
    if not path.is_file():
        print(f"결과 파일이 없습니다: {path}", file=sys.stderr)
        return 2
    document = json.loads(path.read_text(encoding="utf-8"))
    specs = runtime_specs(args.runtime)
    if not specs:
        print("요청한 Codex 런타임을 찾지 못했습니다.", file=sys.stderr)
        return 2
    spec = specs[0]
    runtime = next(
        (
            item
            for item in document.get("runtimes", [])
            if item.get("name") == spec.name
        ),
        None,
    )
    if runtime is None and not args.record_only:
        print(
            f"현재 결과에 병합할 {spec.name} runtime 기록이 없습니다.",
            file=sys.stderr,
        )
        return 2
    existing_thread_id = args.surface_thread_id or (
        runtime.get("thread_id") if runtime is not None else None
    )
    if not existing_thread_id:
        print("외부 표면 검사에 사용할 surface-thread-id가 없습니다.", file=sys.stderr)
        return 2

    recheck_id = args.recheck_id or (
        f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    if safe_artifact_component(recheck_id) != recheck_id:
        print("recheck-id에는 영문자, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다.", file=sys.stderr)
        return 2
    directory = permission_recheck_root(root) / recheck_id
    if directory.exists():
        print(f"이미 존재하는 권한 재검사 ID입니다: {directory}", file=sys.stderr)
        return 2
    directory.mkdir(parents=True)

    attempt = run_permission_recheck(
        spec,
        root,
        recheck_id,
        existing_thread_id,
        host_context,
        fingerprint_before,
    )
    attempt.update(
        {
            "schema_version": PERMISSION_RECHECK_SCHEMA_VERSION,
            "kind": PERMISSION_RECHECK_KIND,
            "recheck_id": recheck_id,
            "source_gate_run_id": document.get("run_id"),
            "checked_at": utc_now(),
        }
    )
    attempt_path = directory / "attempt-result.json"
    attempt_payload = write_json_artifact(attempt_path, attempt)
    if args.record_only:
        document["permission_rechecks"] = load_permission_rechecks(root)
        write_outputs(root, document)
        print(f"권한 카나리 기록 전용 검사: {attempt['status']}")
        print("현재 Gate runtime 기록에는 병합하지 않았습니다.")
        return 0 if attempt["status"] == "pass" else 1

    assert runtime is not None
    for name in PERMISSION_HARD_CHECKS:
        runtime.setdefault("checks", {})[name] = json_safe(attempt["checks"][name])
    runtime.setdefault("evidence", {})["permission_recheck"] = {
        "recheck_id": recheck_id,
        "checked_at": attempt["checked_at"],
        "path": attempt_path.relative_to(root).as_posix(),
        "sha256": sha256_bytes(attempt_payload),
        "model_turns_created": False,
    }
    document["completed_at"] = utc_now()
    write_outputs(root, document)
    permission_status = document["gate_decision"]["subgates"]["permission"]["status"]
    print(f"권한 카나리 재검사: {attempt['status']}")
    print(f"0A-P 판정: {permission_status}")
    print(f"Gate 판정: {document['gate_decision']['status']}")
    return 0 if permission_status == "GO" else 1


def interop_checks_with_error(summary: str, exc: BaseException) -> dict[str, dict[str, Any]]:
    error = exception_record(exc)
    return {
        name: check("error", summary, error=error)
        for name in DESKTOP_INTEROP_HARD_CHECKS
    }


def write_json_artifact(path: Path, value: dict[str, Any]) -> bytes:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return payload


def read_interop_observation(path: Path) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    if not path.exists():
        return None, None, None
    if path.is_symlink():
        return None, None, "관찰 파일은 심볼릭 링크일 수 없습니다."
    try:
        if path.stat().st_size > 65_536:
            return None, None, "관찰 파일이 64 KiB 제한을 초과했습니다."
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, None, f"관찰 파일을 아직 읽을 수 없습니다: {type(exc).__name__}"
    if not isinstance(value, dict):
        return None, None, "관찰 파일의 최상위 값은 JSON 객체여야 합니다."
    return value, payload, None


def raw_thread_read(client: CodexClient, thread_id: str) -> dict[str, Any]:
    """새 Desktop 항목에 대한 이전 SDK 모델의 역직렬화 실패를 피하는 v2 JSON 경로."""

    value = client._request_raw(  # noqa: SLF001 - App Server v2 forward-compat 검증이다.
        "thread/read",
        {"threadId": thread_id, "includeTurns": True},
    )
    result = json_safe(value)
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise RuntimeError("thread/read 원시 응답에 thread 객체가 없습니다.")
    return result


def raw_thread_resume(
    client: CodexClient,
    thread_id: str,
    cwd: Path,
) -> dict[str, Any]:
    value = client._request_raw(  # noqa: SLF001 - App Server v2 forward-compat 검증이다.
        "thread/resume",
        {
            "threadId": thread_id,
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
        },
    )
    result = json_safe(value)
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise RuntimeError("thread/resume 원시 응답에 thread 객체가 없습니다.")
    return result


def raw_turn_start(
    client: CodexClient,
    thread_id: str,
    cwd: Path,
    marker: str,
) -> tuple[str, dict[str, Any]]:
    value = client._request_raw(  # noqa: SLF001 - App Server v2 forward-compat 검증이다.
        "turn/start",
        {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": (
                        f"FM-0A-R SDK marker {marker}. 도구를 사용하지 말고, "
                        "marker 값을 JSON 형식으로 그대로 답하세요."
                    ),
                }
            ],
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "cwd": str(cwd),
            "effort": "low",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "outputSchema": {
                "type": "object",
                "properties": {"marker": {"type": "string", "const": marker}},
                "required": ["marker"],
                "additionalProperties": False,
            },
        },
    )
    result = json_safe(value)
    turn_id = nested_get(result, "turn", "id")
    if not isinstance(turn_id, str) or not turn_id:
        raise RuntimeError("turn/start 원시 응답에서 turn ID를 찾지 못했습니다.")
    client.register_turn_notifications(turn_id)
    return turn_id, result


def raw_thread_id(response: dict[str, Any]) -> str | None:
    value = nested_get(response, "thread", "id")
    return value if isinstance(value, str) else None


def raw_thread_turn_count(response: dict[str, Any]) -> int:
    turns = nested_get(response, "thread", "turns")
    return len(turns) if isinstance(turns, list) else 0


def verify_desktop_interop(args: argparse.Namespace) -> int:
    """SDK challenge와 Codex Desktop 관찰을 맞대조해 0A-R 증거를 기록한다."""

    root = Path(args.project_root).resolve() if args.project_root else project_root()
    path = results_path(root)
    if not path.is_file():
        print(f"결과 파일이 없습니다: {path}", file=sys.stderr)
        return 2
    document = json.loads(path.read_text(encoding="utf-8"))

    verification_id = args.verification_id or (
        f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    if safe_artifact_component(verification_id) != verification_id:
        print("verification-id에는 영문자, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다.", file=sys.stderr)
        return 2
    if bool(args.desktop_marker) == bool(args.desktop_turn_id):
        print("desktop-marker와 desktop-turn-id 중 정확히 하나를 지정해야 합니다.", file=sys.stderr)
        return 2
    if args.desktop_marker and not re.fullmatch(
        r"FM0AR-DESKTOP-[A-Za-z0-9-]{8,128}",
        args.desktop_marker,
    ):
        print("desktop-marker는 FM0AR-DESKTOP- 접두사의 합성 marker여야 합니다.", file=sys.stderr)
        return 2
    if args.desktop_turn_id and not re.fullmatch(
        r"[A-Za-z0-9_-]{12,128}",
        args.desktop_turn_id,
    ):
        print("desktop-turn-id 형식이 안전한 식별자 규칙과 맞지 않습니다.", file=sys.stderr)
        return 2
    if not args.expected_project_id.strip():
        print("expected-project-id는 비어 있을 수 없습니다.", file=sys.stderr)
        return 2

    try:
        expected_cwd = Path(args.expected_cwd).resolve(strict=True)
    except OSError as exc:
        print(f"expected-cwd를 확인할 수 없습니다: {exc}", file=sys.stderr)
        return 2
    if not expected_cwd.is_dir():
        print(f"expected-cwd가 디렉터리가 아닙니다: {expected_cwd}", file=sys.stderr)
        return 2

    specs = runtime_specs(args.runtime)
    if not specs:
        print("요청한 Codex 런타임을 찾지 못했습니다.", file=sys.stderr)
        return 2
    spec = specs[0]
    attempt_root = interop_root(root) / verification_id
    if attempt_root.exists():
        print(f"이미 존재하는 검증 ID입니다: {attempt_root}", file=sys.stderr)
        return 2
    attempt_root.mkdir(parents=True)
    challenge_path = attempt_root / "challenge.json"
    observation_path = attempt_root / "desktop-observation.json"
    attempt_path = attempt_root / "attempt-result.json"

    surface_handler = FailClosedApprovalHandler()
    handler = FailClosedApprovalHandler()
    surface_client: CodexClient | None = None
    client: CodexClient | None = None
    challenge: dict[str, Any] | None = None
    observation: dict[str, Any] = {}
    observation_payload: bytes | None = None
    surface_check: dict[str, Any] | None = None
    surface_observations: dict[str, Any] | None = None
    sdk_state: dict[str, Any] = {}
    checks: dict[str, dict[str, Any]]
    try:
        # 사용자 설정에 등록된 MCP 이름을 먼저 안전하게 읽고, 실제 검증 세션에서는
        # 각각을 명시적으로 비활성화한다.
        surface_client = make_client(
            spec,
            expected_cwd,
            surface_handler,
            BASE_CONFIG_OVERRIDES,
        )
        surface_client.start()
        surface_client.initialize()
        config_observation = read_effective_config(surface_client, expected_cwd)
        surface_preflight, mcp_overrides = build_mcp_disable_overrides(config_observation)
        if surface_preflight.get("status") != "pass":
            raise RuntimeError("MCP 비활성화 preflight를 통과하지 못했습니다.")
        surface_client.close()
        surface_client = None

        client = make_client(
            spec,
            expected_cwd,
            handler,
            (*BASE_CONFIG_OVERRIDES, *mcp_overrides),
        )
        client.start()
        client.initialize()

        before = raw_thread_read(client, args.desktop_thread_id)
        sdk_state["read_thread_id"] = raw_thread_id(before)
        desktop_evidence_value = args.desktop_marker or args.desktop_turn_id
        sdk_state["desktop_marker_visible_before"] = value_contains_marker(
            before,
            desktop_evidence_value,
        )
        sdk_state["turn_count_before"] = raw_thread_turn_count(before)

        resumed = raw_thread_resume(
            client,
            args.desktop_thread_id,
            expected_cwd,
        )
        sdk_state["resumed_thread_id"] = raw_thread_id(resumed)

        surface_check, surface_observations = inspect_external_surfaces(
            client,
            expected_cwd,
            args.desktop_thread_id,
        )
        if surface_check.get("status") != "pass":
            raise RuntimeError("실제 검증 세션의 외부 도구 표면 비활성화를 입증하지 못했습니다.")

        sdk_marker = f"FM0AR-SDK-{uuid.uuid4()}"
        sdk_turn_id, _turn_response = raw_turn_start(
            client,
            args.desktop_thread_id,
            expected_cwd,
            sdk_marker,
        )
        sdk_state["sdk_turn_id"] = sdk_turn_id
        turn_wait = wait_for_turn(
            client,
            args.desktop_thread_id,
            sdk_turn_id,
            args.turn_timeout,
        )
        sdk_state["sdk_turn_completed"] = bool(
            turn_wait.get("completed") and turn_status(turn_wait) == "completed"
        )
        after_turn = raw_thread_read(client, args.desktop_thread_id)
        sdk_state["sdk_marker_visible_after_turn"] = value_contains_marker(
            after_turn,
            sdk_marker,
        )
        sdk_state["turn_count_after_sdk_turn"] = raw_thread_turn_count(after_turn)

        ready_time = datetime.now(timezone.utc)
        expires_time = ready_time + timedelta(seconds=args.observation_timeout)
        challenge = {
            "schema_version": "1.0",
            "kind": "fm_0a_r_desktop_interop_challenge",
            "verification_id": verification_id,
            "challenge_nonce": str(uuid.uuid4()),
            "thread_id": args.desktop_thread_id,
            "expected_project_id": args.expected_project_id,
            "expected_cwd": str(expected_cwd),
            "desktop_evidence_kind": "synthetic_marker"
            if args.desktop_marker
            else "completed_turn_id",
            "desktop_evidence_sha256": sha256_bytes(
                desktop_evidence_value.encode("utf-8")
            ),
            "sdk_marker": sdk_marker,
            "sdk_turn_id": sdk_turn_id,
            "app_server_access_mode": "raw_json_v2_via_openai_codex_client",
            "ready_at": ready_time.isoformat().replace("+00:00", "Z"),
            "expires_at": expires_time.isoformat().replace("+00:00", "Z"),
            "observation_contract": {
                "required_fields": [
                    "challenge_nonce",
                    "thread_id",
                    "observed_at",
                    "thread_found",
                    "project_id",
                    "observed_cwd",
                    "sdk_marker_visible",
                    "sdk_turn_id",
                    "status_during_sdk_wait",
                ],
                "trusted_writer": "현재 Codex Desktop 호스트에서 별도로 수행한 app 관찰",
            },
        }
        challenge_payload = write_json_artifact(challenge_path, challenge)
        print("FM0A_R_CHALLENGE_READY", flush=True)
        print(f"challenge={challenge_path}", flush=True)
        print(f"observation={observation_path}", flush=True)
        print(f"sdk_turn_id={sdk_turn_id}", flush=True)

        deadline = time.monotonic() + args.observation_timeout
        observation_error: str | None = None
        while time.monotonic() < deadline:
            candidate, payload, error = read_interop_observation(observation_path)
            if candidate is not None:
                observation = candidate
                observation_payload = payload
                observation_error = None
                break
            observation_error = error
            time.sleep(0.25)
        if not observation:
            observation = {
                "observation_error": observation_error
                or "제한 시간 안에 Desktop 관찰 파일이 생성되지 않았습니다."
            }

        final_read = raw_thread_read(client, args.desktop_thread_id)
        sdk_state["final_thread_id"] = raw_thread_id(final_read)
        sdk_state["desktop_marker_visible_final"] = value_contains_marker(
            final_read,
            desktop_evidence_value,
        )
        sdk_state["sdk_marker_visible_final"] = value_contains_marker(
            final_read,
            sdk_marker,
        )
        sdk_state["turn_count_final"] = raw_thread_turn_count(final_read)
        checks = evaluate_desktop_interop_bundle(challenge, observation, sdk_state)
        passed = all(item.get("status") == "pass" for item in checks.values())
        status = "pass" if passed else "fail"
        attempt = {
            "schema_version": "1.0",
            "kind": "fm_0a_r_desktop_interop_attempt",
            "verification_id": verification_id,
            "status": status,
            "checked_at": utc_now(),
            "runtime": {
                "name": spec.name,
                "executable": str(spec.executable),
                "version": runtime_version(spec.executable),
            },
            "checks": checks,
            "sdk_state": sdk_state,
            "surface_check": surface_check,
            "surface_observations": sanitize_surface_observations(
                surface_observations or {}
            ),
            "approval_requests": [*surface_handler.requests, *handler.requests],
            "challenge": {
                "path": challenge_path.relative_to(root).as_posix(),
                "sha256": sha256_bytes(challenge_payload),
            },
            "observation": {
                "path": observation_path.relative_to(root).as_posix(),
                "available": observation_payload is not None,
                "sha256": sha256_bytes(observation_payload)
                if observation_payload is not None
                else None,
                "checked_via": observation.get("checked_via"),
            },
        }
        attempt_payload = write_json_artifact(attempt_path, attempt)
        document["desktop_interop"] = {
            "status": status,
            "checked_at": attempt["checked_at"],
            "checked_via": observation.get("checked_via")
            or "Codex Desktop app 관찰 + SDK/App Server challenge",
            "desktop_version": observation.get("desktop_version"),
            "desktop_thread_id": args.desktop_thread_id,
            "verification_id": verification_id,
            "checks": checks,
            "evidence": {
                "runtime": attempt["runtime"],
                "challenge_path": attempt["challenge"]["path"],
                "challenge_sha256": attempt["challenge"]["sha256"],
                "observation_path": attempt["observation"]["path"],
                "observation_sha256": attempt["observation"]["sha256"],
                "attempt_path": attempt_path.relative_to(root).as_posix(),
                "attempt_sha256": sha256_bytes(attempt_payload),
                "desktop_evidence_kind": challenge["desktop_evidence_kind"],
                "desktop_evidence_sha256": challenge["desktop_evidence_sha256"],
                "sdk_marker_sha256": sha256_bytes(sdk_marker.encode("utf-8")),
                "sdk_turn_id": sdk_turn_id,
                "app_server_access_mode": challenge["app_server_access_mode"],
                "approval_request_count": len(attempt["approval_requests"]),
                "external_surfaces_disabled": surface_check,
            },
        }
        write_outputs(root, document)
        print(f"Desktop 양방향 상호운용성: {status}", flush=True)
        print(f"Gate 판정: {document['gate_decision']['status']}", flush=True)
        return 0 if status == "pass" else 1
    except BaseException as exc:
        checks = interop_checks_with_error("FM-0A-R 검증 절차가 완료되지 않음", exc)
        failed_attempt = {
            "schema_version": "1.0",
            "kind": "fm_0a_r_desktop_interop_attempt",
            "verification_id": verification_id,
            "status": "error",
            "checked_at": utc_now(),
            "runtime": {
                "name": spec.name,
                "executable": str(spec.executable),
                "version": runtime_version(spec.executable),
            },
            "checks": checks,
            "sdk_state": sdk_state,
            "error": exception_record(exc),
            "approval_requests": [*surface_handler.requests, *handler.requests],
        }
        attempt_payload = write_json_artifact(attempt_path, failed_attempt)
        document["desktop_interop"] = {
            "status": "error",
            "checked_at": failed_attempt["checked_at"],
            "checked_via": "SDK/App Server challenge (절차 중단)",
            "desktop_version": None,
            "desktop_thread_id": args.desktop_thread_id,
            "verification_id": verification_id,
            "checks": checks,
            "evidence": {
                "attempt_path": attempt_path.relative_to(root).as_posix(),
                "attempt_sha256": sha256_bytes(attempt_payload),
            },
        }
        write_outputs(root, document)
        print(
            f"FM-0A-R 검증 실패: {exception_record(exc).get('message')}",
            file=sys.stderr,
        )
        return 1
    finally:
        if surface_client is not None:
            surface_client.close()
        if client is not None:
            client.close()


def record_desktop(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve() if args.project_root else project_root()
    path = results_path(root)
    if not path.is_file():
        print(f"결과 파일이 없습니다: {path}", file=sys.stderr)
        return 2
    document = json.loads(path.read_text(encoding="utf-8"))
    visible = list(dict.fromkeys(args.visible or []))
    missing = list(dict.fromkeys(args.missing or []))
    overlap = set(visible) & set(missing)
    if overlap:
        print(f"visible과 missing에 동시에 포함된 ID: {sorted(overlap)}", file=sys.stderr)
        return 2
    expected = {runtime.get("thread_id") for runtime in document.get("runtimes", []) if runtime.get("thread_id")}
    provided = set(visible) | set(missing)
    unclassified = expected - provided
    status = "pending" if unclassified else ("pass" if visible and not missing else "fail")
    document["desktop_visibility"] = {
        "status": status,
        "checked_at": utc_now(),
        "checked_via": args.checked_via,
        "visible_thread_ids": visible,
        "missing_thread_ids": missing,
        "unclassified_thread_ids": sorted(unclassified),
    }
    write_outputs(root, document)
    print(f"Desktop 확인 상태: {status}")
    print(f"Gate 판정: {document['gate_decision']['status']}")
    return 0 if document["gate_decision"]["status"] == "GO" else 1


def regenerate_report(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve() if args.project_root else project_root()
    path = results_path(root)
    if not path.is_file():
        print(f"결과 파일이 없습니다: {path}", file=sys.stderr)
        return 2
    document = json.loads(path.read_text(encoding="utf-8"))
    write_outputs(root, document)
    print(str(report_path(root)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FlowMarshal Gate 0A 기술 스파이크")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser(
        "sandbox-status",
        help="실제 Codex 홈의 Windows sandbox 준비상태를 변경 없이 조회한다",
    )
    status.add_argument("--authoritative-codex-home", type=Path)
    status.add_argument("--project-root")
    status.set_defaults(func=sandbox_status)

    run = subparsers.add_parser("run", help="Gate 0A 런타임 검사를 실행한다")
    run.add_argument("--runtime", choices=("pinned", "system", "all"), default="system")
    run.add_argument("--turn-timeout", type=int, default=DEFAULT_TURN_TIMEOUT_SECONDS)
    run.add_argument("--run-id")
    run.add_argument("--authoritative-codex-home", type=Path)
    run.add_argument("--project-root")
    run.set_defaults(func=run_probe)

    setup = subparsers.add_parser(
        "setup-sandbox",
        help="호스트에서는 수동 복구 명령만 안내하고 폐기 VM에서만 설정한다",
    )
    setup.add_argument("--runtime", choices=("pinned", "system"), default="system")
    setup.add_argument("--mode", choices=("elevated",), default="elevated")
    setup.add_argument("--timeout", type=int, default=300)
    setup.add_argument("--vm-suite-id")
    setup.add_argument("--vm-scenario", choices=VM_SCENARIOS)
    setup.add_argument("--vm-attempt-id")
    setup.add_argument(
        "--simulate-setup-failure",
        action="store_true",
        help="failure-retry 첫 Attempt에서 setup API 호출 전 결정적 실패를 기록한다",
    )
    setup.add_argument(
        "--environment",
        choices=("host", "disposable-vm"),
        default="host",
    )
    setup.add_argument("--authoritative-codex-home", type=Path)
    setup.add_argument("--project-root")
    setup.set_defaults(func=setup_permission_sandbox)

    disposal = subparsers.add_parser(
        "record-vm-disposal",
        help="VM 관리 계층에서 확인한 초기화·폐기 증거를 suite에 기록한다",
    )
    disposal.add_argument("--vm-suite-id", required=True)
    disposal.add_argument("--action", choices=("reset", "disposed"), required=True)
    disposal.add_argument("--provider", required=True)
    disposal.add_argument("--vm-identifier", required=True)
    disposal.add_argument("--evidence-reference", required=True)
    disposal.add_argument("--project-root")
    disposal.set_defaults(func=record_vm_disposal)

    vm_verify = subparsers.add_parser(
        "verify-vm-suite",
        help="폐기 Windows VM provisioning suite의 구조화된 증거를 판정한다",
    )
    vm_verify.add_argument("--vm-suite-id", required=True)
    vm_verify.add_argument("--project-root")
    vm_verify.set_defaults(func=verify_vm_suite_evidence)

    permission = subparsers.add_parser(
        "verify-permission",
        help="기존 0A-R 결과를 보존하고 FM-0A-P 권한 카나리만 재검사한다",
    )
    permission.add_argument("--runtime", choices=("pinned", "system"), default="system")
    permission.add_argument("--authoritative-codex-home", type=Path)
    permission.add_argument("--recheck-id")
    permission.add_argument("--surface-thread-id")
    permission.add_argument("--record-only", action="store_true")
    permission.add_argument("--project-root")
    permission.set_defaults(func=verify_permission)

    desktop = subparsers.add_parser("record-desktop", help="Codex Desktop 표시 확인을 기록한다")
    desktop.add_argument("--visible", action="append", default=[])
    desktop.add_argument("--missing", action="append", default=[])
    desktop.add_argument("--checked-via", required=True)
    desktop.add_argument("--project-root")
    desktop.set_defaults(func=record_desktop)

    interop = subparsers.add_parser(
        "verify-desktop-interop",
        help="신뢰된 Desktop 관찰과 SDK challenge를 맞대조해 FM-0A-R을 검증한다",
    )
    interop.add_argument("--desktop-thread-id", required=True)
    interop.add_argument("--expected-cwd", required=True)
    interop.add_argument("--expected-project-id", required=True)
    interop.add_argument("--desktop-marker")
    interop.add_argument("--desktop-turn-id")
    interop.add_argument("--verification-id")
    interop.add_argument("--runtime", choices=("pinned", "system"), default="pinned")
    interop.add_argument("--turn-timeout", type=int, default=DEFAULT_TURN_TIMEOUT_SECONDS)
    interop.add_argument("--observation-timeout", type=int, default=180)
    interop.add_argument("--project-root")
    interop.set_defaults(func=verify_desktop_interop)

    report = subparsers.add_parser("report", help="결과 JSON에서 Markdown 보고서를 재생성한다")
    report.add_argument("--project-root")
    report.set_defaults(func=regenerate_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
