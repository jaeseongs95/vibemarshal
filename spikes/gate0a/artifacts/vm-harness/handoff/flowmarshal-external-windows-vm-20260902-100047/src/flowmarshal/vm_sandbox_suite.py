"""폐기 가능한 Windows VM의 sandbox provisioning 증거를 관리한다.

이 모듈은 실제 provisioning API를 호출하지 않는다. Gate 0A CLI가 남긴 구조화된
증거의 순서와 완전성을 검사하고, 실제 VM 증거가 빠졌을 때 fail-closed한다.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from flowmarshal.windows_sandbox import artifact_safety_violations


VM_SUITE_SCHEMA_VERSION = "1.0"
VM_SUITE_INCOMPLETE = "VM_SUITE_INCOMPLETE"
VM_SUITE_INVALID = "VM_SUITE_INVALID"
VM_SCENARIO_NOT_CLEAN = "VM_SCENARIO_NOT_CLEAN"
VM_SUITE_SEQUENCE_INVALID = "VM_SUITE_SEQUENCE_INVALID"
VM_TEST_INJECTED_SETUP_FAILURE = "VM_TEST_INJECTED_SETUP_FAILURE"

VM_SCENARIOS = ("current-first", "cross-version", "failure-retry")
SUCCESSFUL_SETUP_STATUSES = {"completed", "already_ready"}
RETRYABLE_SETUP_STATUSES = {"failed", "timeout"}
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


class VmSuiteError(ValueError):
    """VM suite 계약 또는 시퀀스가 유효하지 않은 경우의 오류."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def validate_component(value: str, *, label: str) -> str:
    if not value or _SAFE_COMPONENT.fullmatch(value) is None:
        raise VmSuiteError(
            VM_SUITE_INVALID,
            f"{label}에는 영문자, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다.",
        )
    return value


def suite_root(artifacts: Path, suite_id: str) -> Path:
    return artifacts / "vm-harness" / "suites" / validate_component(
        suite_id, label="vm-suite-id"
    )


def scenario_root(artifacts: Path, suite_id: str, scenario: str) -> Path:
    if scenario not in VM_SCENARIOS:
        raise VmSuiteError(VM_SUITE_INVALID, f"알 수 없는 VM 시나리오입니다: {scenario}")
    return suite_root(artifacts, suite_id) / "scenarios" / scenario


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VmSuiteError(
            VM_SUITE_INVALID,
            f"VM 증거 JSON을 읽을 수 없습니다: {path}: {type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise VmSuiteError(VM_SUITE_INVALID, f"VM 증거는 JSON 객체여야 합니다: {path}")
    return value


def write_json_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def fingerprint_is_clean(value: dict[str, Any]) -> bool:
    """비밀 내용 없이 새 VM의 샌드박스 상태가 비어 있는지 확인한다."""

    if value.get("accounts_error") is not None:
        return False
    accounts = value.get("accounts")
    if not isinstance(accounts, (list, tuple)) or len(accounts) != 2:
        return False
    expected = {"CodexSandboxOffline", "CodexSandboxOnline"}
    observed = {str(item.get("Name")) for item in accounts if isinstance(item, dict)}
    if observed != expected:
        return False
    if not all(
        isinstance(item, dict) and item.get("Missing") is True for item in accounts
    ):
        return False
    marker = value.get("setup_marker")
    secrets = value.get("secrets_directory")
    return bool(
        isinstance(marker, dict)
        and marker.get("exists") is False
        and marker.get("error") is None
        and isinstance(secrets, dict)
        and secrets.get("exists") is False
        and secrets.get("error") is None
    )


def load_attempts(root: Path) -> list[dict[str, Any]]:
    attempts_root = root / "attempts"
    if not attempts_root.exists():
        return []
    attempts: list[dict[str, Any]] = []
    for path in sorted(attempts_root.glob("*/setup-result.json")):
        value = read_json_object(path)
        value["_evidence_path"] = path.relative_to(root).as_posix()
        attempts.append(value)
    return sorted(
        attempts,
        key=lambda item: (
            int(item.get("sequence", 0)),
            str(item.get("recorded_at", "")),
            str(item.get("attempt_id", "")),
        ),
    )


def validate_attempt_request(
    scenario: str,
    attempts: list[dict[str, Any]],
    *,
    runtime_name: str,
    inject_failure: bool,
) -> int:
    """다음 Attempt의 허용 런타임·재시도 수·결함 주입 순서를 검증한다."""

    if scenario == "current-first":
        if runtime_name != "desktop-system" or inject_failure:
            raise VmSuiteError(
                VM_SUITE_SEQUENCE_INVALID,
                "current-first는 시스템 Codex의 실제 setup만 허용합니다.",
            )
        if not attempts:
            return 1
        if len(attempts) == 1 and attempts[0].get("status") in RETRYABLE_SETUP_STATUSES:
            return 2
        raise VmSuiteError(
            VM_SUITE_SEQUENCE_INVALID,
            "current-first는 최초 실행과 실패 후 1회 재시도까지만 허용합니다.",
        )

    if scenario == "cross-version":
        if inject_failure:
            raise VmSuiteError(
                VM_SUITE_SEQUENCE_INVALID,
                "cross-version에서는 결함 주입을 사용할 수 없습니다.",
            )
        pinned = [item for item in attempts if item.get("runtime", {}).get("name") == "sdk-pinned"]
        system = [item for item in attempts if item.get("runtime", {}).get("name") == "desktop-system"]
        pinned_succeeded = any(item.get("status") in SUCCESSFUL_SETUP_STATUSES for item in pinned)
        system_succeeded = any(item.get("status") in SUCCESSFUL_SETUP_STATUSES for item in system)
        if not pinned_succeeded:
            if runtime_name != "sdk-pinned" or system or len(pinned) >= 2:
                raise VmSuiteError(
                    VM_SUITE_SEQUENCE_INVALID,
                    "cross-version은 먼저 고정 구버전을 실행하며 단계별 재시도는 1회뿐입니다.",
                )
            return len(attempts) + 1
        if not system_succeeded:
            if runtime_name != "desktop-system" or len(system) >= 2:
                raise VmSuiteError(
                    VM_SUITE_SEQUENCE_INVALID,
                    "구버전 성공 뒤 현재 시스템 Codex를 실행해야 하며 재시도는 1회뿐입니다.",
                )
            return len(attempts) + 1
        raise VmSuiteError(
            VM_SUITE_SEQUENCE_INVALID,
            "cross-version 시나리오는 이미 성공했습니다.",
        )

    if scenario == "failure-retry":
        if runtime_name != "desktop-system":
            raise VmSuiteError(
                VM_SUITE_SEQUENCE_INVALID,
                "failure-retry는 시스템 Codex만 사용합니다.",
            )
        if not attempts:
            if not inject_failure:
                raise VmSuiteError(
                    VM_SUITE_SEQUENCE_INVALID,
                    "failure-retry의 첫 Attempt는 결정적 결함 주입이어야 합니다.",
                )
            return 1
        first_error = attempts[0].get("error")
        if (
            len(attempts) == 1
            and attempts[0].get("status") == "failed"
            and isinstance(first_error, dict)
            and first_error.get("reason_code") == VM_TEST_INJECTED_SETUP_FAILURE
            and not inject_failure
        ):
            return 2
        raise VmSuiteError(
            VM_SUITE_SEQUENCE_INVALID,
            "failure-retry는 주입된 실패 1회와 실제 재시도 1회만 허용합니다.",
        )

    raise VmSuiteError(VM_SUITE_INVALID, f"알 수 없는 VM 시나리오입니다: {scenario}")


def _status_at(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    result = value.get("result")
    return str(result.get("status")) if isinstance(result, dict) and result.get("status") else None


def _runtime_name(attempt: dict[str, Any]) -> str | None:
    runtime = attempt.get("runtime")
    return str(runtime.get("name")) if isinstance(runtime, dict) and runtime.get("name") else None


def _runtime_version(attempt: dict[str, Any]) -> str | None:
    runtime = attempt.get("runtime")
    version = runtime.get("version") if isinstance(runtime, dict) else None
    if not isinstance(version, dict):
        return None
    stdout = version.get("stdout")
    return str(stdout).strip() if stdout else None


def _check(
    check_id: str,
    status: str,
    summary: str,
    **evidence: Any,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "summary": summary,
        "evidence": evidence,
    }


def _scenario_contract(root: Path) -> dict[str, Any] | None:
    path = root / "scenario.json"
    return read_json_object(path) if path.exists() else None


def _current_first_check(root: Path) -> dict[str, Any]:
    attempts = load_attempts(root)
    if not attempts:
        return _check("current_first_setup", "pending", "현재 Codex 최초 설정 증거가 없습니다.")
    valid_count = len(attempts) <= 2
    valid_runtime = all(_runtime_name(item) == "desktop-system" for item in attempts)
    prior_valid = len(attempts) == 1 or all(
        item.get("status") in RETRYABLE_SETUP_STATUSES for item in attempts[:-1]
    )
    final = attempts[-1]
    passed = bool(
        valid_count
        and valid_runtime
        and prior_valid
        and final.get("status") == "completed"
        and final.get("setup_api_called") is True
        and _status_at(final.get("readiness_after")) == "ready"
    )
    return _check(
        "current_first_setup",
        "pass" if passed else "fail",
        "깨끗한 VM에서 현재 Codex 최초 설정을 확인했습니다."
        if passed
        else "현재 Codex 최초 설정 증거의 순서나 결과가 유효하지 않습니다.",
        attempts=len(attempts),
        final_status=final.get("status"),
    )


def _cross_version_check(root: Path) -> dict[str, Any]:
    attempts = load_attempts(root)
    if not attempts:
        return _check("cross_version_setup", "pending", "버전 교차 설정 증거가 없습니다.")
    pinned_attempts = [item for item in attempts if _runtime_name(item) == "sdk-pinned"]
    system_attempts = [item for item in attempts if _runtime_name(item) == "desktop-system"]
    ordered_phases = [
        _runtime_name(item) for item in attempts
    ] == ["sdk-pinned"] * len(pinned_attempts) + ["desktop-system"] * len(system_attempts)
    pinned_final = pinned_attempts[-1] if pinned_attempts else None
    system_final = system_attempts[-1] if system_attempts else None
    pinned_prior_valid = all(
        item.get("status") in RETRYABLE_SETUP_STATUSES for item in pinned_attempts[:-1]
    )
    system_prior_valid = all(
        item.get("status") in RETRYABLE_SETUP_STATUSES for item in system_attempts[:-1]
    )
    pinned_succeeded = bool(
        pinned_final
        and pinned_final.get("status") == "completed"
        and pinned_final.get("setup_api_called") is True
        and _status_at(pinned_final.get("readiness_after")) == "ready"
    )
    system_succeeded = bool(
        system_final
        and system_final.get("status") in SUCCESSFUL_SETUP_STATUSES
        and _status_at(system_final.get("readiness_after")) == "ready"
        and (
            system_final.get("setup_api_called") is True
            if system_final.get("status") == "completed"
            else system_final.get("setup_api_called") is False
        )
    )
    versions_differ = False
    if pinned_final is not None and system_final is not None:
        versions_differ = _runtime_version(pinned_final) != _runtime_version(
            system_final
        ) and None not in {
            _runtime_version(pinned_final),
            _runtime_version(system_final),
        }
    passed = bool(
        pinned_succeeded
        and system_succeeded
        and ordered_phases
        and pinned_prior_valid
        and system_prior_valid
        and 1 <= len(pinned_attempts) <= 2
        and 1 <= len(system_attempts) <= 2
        and len(attempts) == len(pinned_attempts) + len(system_attempts)
        and versions_differ
    )
    return _check(
        "cross_version_setup",
        "pass" if passed else "fail",
        "같은 홈에서 구버전 설정 뒤 현재 버전 호환성 확인을 통과했습니다."
        if passed
        else "버전 교차 설정의 순서·재시도 수·버전 증거가 유효하지 않습니다.",
        attempts=len(attempts),
        pinned_version=_runtime_version(pinned_final) if pinned_final else None,
        system_version=_runtime_version(system_final) if system_final else None,
    )


def _second_home_guard_check(root: Path) -> dict[str, Any]:
    guards_root = root / "guards"
    paths = sorted(guards_root.glob("*.json")) if guards_root.exists() else []
    if not paths:
        return _check("second_home_guard", "pending", "두 번째 홈 차단 증거가 없습니다.")
    guards = [read_json_object(path) for path in paths]
    passed = any(
        item.get("reason_code") == "HOST_PROVISIONING_FORBIDDEN"
        and item.get("runtime_started") is False
        and item.get("setup_api_called") is False
        and item.get("claimed_home")
        and item.get("requested_home")
        and item.get("claimed_home") != item.get("requested_home")
        for item in guards
    )
    return _check(
        "second_home_guard",
        "pass" if passed else "fail",
        "두 번째 authoritative home 요청을 런타임 시작 전에 차단했습니다."
        if passed
        else "두 번째 홈 차단 증거가 불완전합니다.",
        guard_records=len(guards),
    )


def _failure_retry_check(root: Path) -> dict[str, Any]:
    attempts = load_attempts(root)
    if not attempts:
        return _check("limited_failure_retry", "pending", "실패·재시도 증거가 없습니다.")
    first_error = attempts[0].get("error") if attempts else None
    passed = bool(
        len(attempts) == 2
        and _runtime_name(attempts[0]) == "desktop-system"
        and attempts[0].get("status") == "failed"
        and attempts[0].get("setup_api_called") is False
        and isinstance(first_error, dict)
        and first_error.get("reason_code") == VM_TEST_INJECTED_SETUP_FAILURE
        and _runtime_name(attempts[1]) == "desktop-system"
        and attempts[1].get("status") == "completed"
        and attempts[1].get("setup_api_called") is True
        and _status_at(attempts[1].get("readiness_after")) == "ready"
    )
    return _check(
        "limited_failure_retry",
        "pass" if passed else "fail",
        "결정적 실패 뒤 같은 홈에서 한 번만 재시도해 성공했습니다."
        if passed
        else "실패·재시도 횟수나 결과가 유효하지 않습니다.",
        attempts=len(attempts),
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_attempt_time(roots: Iterable[Path]) -> datetime | None:
    values = [
        parsed
        for root in roots
        for attempt in load_attempts(root)
        if (parsed := _parse_time(attempt.get("recorded_at"))) is not None
    ]
    return max(values) if values else None


def _manifest(root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        if path.name == "suite-result.json":
            continue
        payload = path.read_bytes()
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return result


def evaluate_vm_suite(artifacts: Path, suite_id: str) -> dict[str, Any]:
    """완전한 세 시나리오와 VM 폐기 증거가 있을 때만 GO를 반환한다."""

    root = suite_root(artifacts, suite_id)
    checks: list[dict[str, Any]] = []
    roots = {scenario: scenario_root(artifacts, suite_id, scenario) for scenario in VM_SCENARIOS}

    contracts: dict[str, dict[str, Any] | None] = {}
    for scenario, scenario_path in roots.items():
        contract = _scenario_contract(scenario_path)
        contracts[scenario] = contract
        if contract is None:
            checks.append(
                _check(
                    f"{scenario}_clean_start",
                    "pending",
                    f"{scenario}의 깨끗한 VM 시작 증거가 없습니다.",
                )
            )
            continue
        fingerprint = contract.get("initial_fingerprint")
        identity = contract.get("vm_identity")
        contract_matches = bool(
            contract.get("suite_id") == suite_id
            and contract.get("scenario") == scenario
            and contract.get("authoritative_codex_home")
        )
        clean = isinstance(fingerprint, dict) and fingerprint_is_clean(fingerprint)
        recognized = isinstance(identity, dict) and identity.get("recognized_virtual_machine") is True
        checks.append(
            _check(
                f"{scenario}_clean_start",
                "pass" if contract_matches and clean and recognized else "fail",
                f"{scenario}가 인식된 VM의 깨끗한 상태에서 시작했습니다."
                if contract_matches and clean and recognized
                else f"{scenario}의 깨끗한 VM 시작 증거가 유효하지 않습니다.",
                contract_matches=contract_matches,
                clean=clean,
                recognized_virtual_machine=recognized,
            )
        )

    checks.append(_current_first_check(roots["current-first"]))
    checks.append(_cross_version_check(roots["cross-version"]))
    checks.append(_second_home_guard_check(roots["cross-version"]))
    checks.append(_failure_retry_check(roots["failure-retry"]))

    violations = artifact_safety_violations(root)
    checks.append(
        _check(
            "artifact_safety",
            "pass" if not violations else "fail",
            "VM 증거 폴더에 Codex 비밀·인증·상태 DB가 없습니다."
            if not violations
            else "VM 증거 폴더에서 반출 금지 Codex 상태를 발견했습니다.",
            violations=violations,
        )
    )

    disposal_path = root / "lifecycle" / "disposal.json"
    if not disposal_path.exists():
        checks.append(
            _check(
                "vm_disposal",
                "pending",
                "검사 완료 후 VM 초기화 또는 폐기 증거가 없습니다.",
            )
        )
    else:
        disposal = read_json_object(disposal_path)
        recorded_at = _parse_time(disposal.get("recorded_at"))
        latest_attempt = _latest_attempt_time(roots.values())
        passed = bool(
            disposal.get("action") in {"reset", "disposed"}
            and disposal.get("provider")
            and disposal.get("vm_identifier")
            and disposal.get("evidence_reference")
            and recorded_at is not None
            and latest_attempt is not None
            and recorded_at >= latest_attempt
        )
        checks.append(
            _check(
                "vm_disposal",
                "pass" if passed else "fail",
                "검사 뒤 VM 초기화 또는 폐기 증거를 확인했습니다."
                if passed
                else "VM 생명주기 증거가 없거나 마지막 Attempt보다 오래됐습니다.",
                action=disposal.get("action"),
                provider=disposal.get("provider"),
            )
        )

    statuses = {item["status"] for item in checks}
    if "fail" in statuses:
        status = "NO-GO"
        reason_code = VM_SUITE_INVALID
    elif "pending" in statuses:
        status = "PENDING"
        reason_code = VM_SUITE_INCOMPLETE
    else:
        status = "GO"
        reason_code = None
    return {
        "schema_version": VM_SUITE_SCHEMA_VERSION,
        "kind": "disposable_windows_vm_sandbox_suite_result",
        "suite_id": suite_id,
        "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "reason_code": reason_code,
        "gate_restoration_allowed": status == "GO",
        "checks": checks,
        "source_manifest": _manifest(root) if root.exists() else [],
        "secret_contents_read": False,
    }
