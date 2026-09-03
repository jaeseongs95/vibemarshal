"""Windows elevated sandbox를 호스트에서 안전하게 점검하는 공통 계층.

이 모듈은 준비 상태를 읽고 상태 변화를 탐지할 뿐 provisioning을 수행하지 않는다.
실제 provisioning은 폐기 가능한 Windows VM에서만 별도 명시적으로 실행한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


AUTHORITATIVE_HOME_INVALID = "AUTHORITATIVE_HOME_INVALID"
HOST_RUNTIME_NOT_SYSTEM = "HOST_RUNTIME_NOT_SYSTEM"
SANDBOX_NOT_READY = "SANDBOX_NOT_READY"
HOST_PROVISIONING_FORBIDDEN = "HOST_PROVISIONING_FORBIDDEN"
SANDBOX_STATE_CHANGED_DURING_PROBE = "SANDBOX_STATE_CHANGED_DURING_PROBE"
SANDBOX_HOME_CROSS_CONTAMINATION = "SANDBOX_HOME_CROSS_CONTAMINATION"

SANDBOX_ACCOUNT_NAMES = ("CodexSandboxOffline", "CodexSandboxOnline")
VM_CONFIRMATION_ENV = "FLOWMARSHAL_DISPOSABLE_WINDOWS_VM"

STATUS_CONFIG_OVERRIDES = (
    'web_search="disabled"',
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.skill_mcp_dependency_install=false",
    "features.multi_agent=false",
    "features.in_app_browser=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "mcp_servers={}",
    'windows.sandbox="elevated"',
)


class SandboxContractError(ValueError):
    """호스트 샌드박스 계약을 만족하지 못한 경우의 구조화된 오류."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class SandboxHostContext:
    project_root: Path
    authoritative_home: Path
    system_codex: Path
    username: str

    def public_dict(self) -> dict[str, str]:
        return {
            "project_root": str(self.project_root),
            "authoritative_codex_home": str(self.authoritative_home),
            "system_codex": str(self.system_codex),
            "username": self.username,
        }


@dataclass(frozen=True, slots=True)
class SandboxFingerprint:
    accounts: tuple[dict[str, Any], ...]
    accounts_error: str | None
    setup_marker: dict[str, Any]
    secrets_directory: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_existing(path: Path, *, label: str) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SandboxContractError(
            AUTHORITATIVE_HOME_INVALID,
            f"{label} 경로가 존재하지 않거나 확인할 수 없습니다: {path}",
        ) from exc


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def default_authoritative_home(
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    user_profile = env.get("USERPROFILE")
    return (Path(user_profile) if user_profile else Path.home()) / ".codex"


def discover_system_codex(
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    explicit = env.get("FLOWMARSHAL_SYSTEM_CODEX")
    if explicit:
        candidates.append(Path(explicit))
    located = shutil.which("codex")
    if located:
        candidates.append(Path(located))
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def resolve_host_context(
    *,
    project_root: Path,
    authoritative_home: Path | str | None = None,
    requested_codex: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SandboxHostContext:
    """호스트에서 사용할 실제 Codex 홈과 시스템 실행 파일을 확정한다.

    CODEX_HOME이 기본 홈과 다르면 암묵적으로 상속하지 않는다. 호출자가 같은 경로를
    ``authoritative_home``으로 명시해야만 사용할 수 있다.
    """

    env = os.environ if environ is None else environ
    project = _resolve_existing(Path(project_root), label="프로젝트")
    default_home = default_authoritative_home(env).expanduser().resolve(strict=False)
    inherited_home = env.get("CODEX_HOME")

    if authoritative_home is None:
        if inherited_home:
            inherited = Path(inherited_home).expanduser().resolve(strict=False)
            if inherited != default_home:
                raise SandboxContractError(
                    AUTHORITATIVE_HOME_INVALID,
                    "기본 홈과 다른 CODEX_HOME은 자동 상속하지 않습니다. "
                    "--authoritative-codex-home으로 명시하십시오.",
                )
        selected_home = default_home
    else:
        selected_home = Path(authoritative_home).expanduser().resolve(strict=False)
        if inherited_home:
            inherited = Path(inherited_home).expanduser().resolve(strict=False)
            if inherited != selected_home:
                raise SandboxContractError(
                    AUTHORITATIVE_HOME_INVALID,
                    "명시한 authoritative home이 현재 CODEX_HOME과 다릅니다.",
                )

    home = _resolve_existing(selected_home, label="authoritative Codex 홈")
    if _is_within(home, project):
        raise SandboxContractError(
            AUTHORITATIVE_HOME_INVALID,
            "authoritative Codex 홈은 프로젝트 또는 artifacts 아래일 수 없습니다.",
        )

    temp_candidates = {
        Path(value).expanduser().resolve(strict=False)
        for value in (env.get("TEMP"), env.get("TMP"), tempfile.gettempdir())
        if value
    }
    if any(_is_within(home, temp_root) for temp_root in temp_candidates):
        raise SandboxContractError(
            AUTHORITATIVE_HOME_INVALID,
            "authoritative Codex 홈은 임시 디렉터리 아래일 수 없습니다.",
        )

    system_codex = discover_system_codex(env)
    if system_codex is None:
        raise SandboxContractError(
            HOST_RUNTIME_NOT_SYSTEM,
            "현재 설치된 시스템 Codex 실행 파일을 찾지 못했습니다.",
        )
    if requested_codex is not None:
        requested = _resolve_existing(Path(requested_codex), label="요청한 Codex")
        if requested != system_codex:
            raise SandboxContractError(
                HOST_RUNTIME_NOT_SYSTEM,
                "실제 PC의 샌드박스 검사는 현재 시스템 Codex만 사용할 수 있습니다.",
            )

    username = env.get("USERNAME") or Path.home().name
    return SandboxHostContext(project, home, system_codex, username)


def manual_setup_command(context: SandboxHostContext) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return (
        "codex sandbox setup --elevated "
        f"--user {quote(context.username)} "
        f"--codex-home {quote(str(context.authoritative_home))}"
    )


def query_windows_sandbox_status(
    context: SandboxHostContext,
    *,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """App Server의 준비상태만 읽는다. setupStart는 절대 호출하지 않는다."""

    from openai_codex.client import CodexClient, CodexConfig

    client: CodexClient | None = None
    try:
        working_directory = (cwd or context.project_root).resolve(strict=True)
        client = CodexClient(
            config=CodexConfig(
                codex_bin=str(context.system_codex),
                config_overrides=STATUS_CONFIG_OVERRIDES,
                cwd=str(working_directory),
                env={"CODEX_HOME": str(context.authoritative_home)},
                client_name="flowmarshal_sandbox_status",
                client_title="FlowMarshal Sandbox Status",
                client_version="0.2.0a0",
                experimental_api=True,
            ),
            approval_handler=lambda _method, _params: {},
        )
        client.start()
        client.initialize()
        response = client._request_raw(  # noqa: SLF001 - 공식 App Server readiness API
            "windowsSandbox/readiness", None
        )
        document = response.model_dump(mode="json", by_alias=True) if hasattr(response, "model_dump") else response
        native_status = document.get("status") if isinstance(document, dict) else None
        if native_status == "ready":
            status = "READY"
            reason_code = None
        elif native_status in {"notConfigured", "updateRequired"}:
            status = "SETUP_REQUIRED"
            reason_code = SANDBOX_NOT_READY
        else:
            status = "ERROR"
            reason_code = SANDBOX_NOT_READY
        return {
            "status": status,
            "reason_code": reason_code,
            "native_status": native_status,
            "setup_command": manual_setup_command(context)
            if status == "SETUP_REQUIRED"
            else None,
            "context": context.public_dict(),
        }
    except BaseException as exc:
        return {
            "status": "ERROR",
            "reason_code": SANDBOX_NOT_READY,
            "native_status": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "setup_command": manual_setup_command(context),
            "context": context.public_dict(),
        }
    finally:
        if client is not None:
            client.close()


def _file_metadata(path: Path, *, include_hash: bool) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "error": None}
    try:
        stat = path.stat()
        result: dict[str, Any] = {
            "exists": True,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "error": None,
        }
        if include_hash:
            result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result
    except OSError as exc:
        return {"exists": True, "error": f"{type(exc).__name__}: {exc}"}


def _secrets_directory_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "mtime_ns": None, "file_count": 0, "error": None}
    try:
        stat = path.stat()
        # 비밀 파일의 이름·내용·해시는 기록하지 않는다.
        file_count = sum(1 for item in path.rglob("*") if item.is_file())
        return {
            "exists": True,
            "mtime_ns": stat.st_mtime_ns,
            "file_count": file_count,
            "error": None,
        }
    except OSError as exc:
        return {
            "exists": True,
            "mtime_ns": None,
            "file_count": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _read_sandbox_accounts() -> tuple[tuple[dict[str, Any], ...], str | None]:
    script = (
        "$names=@('CodexSandboxOffline','CodexSandboxOnline');"
        "$items=foreach($name in $names){try{$u=Get-LocalUser -Name $name -ErrorAction Stop;"
        "[pscustomobject]@{Name=$u.Name;SID=$u.SID.Value;Enabled=$u.Enabled;"
        "PasswordLastSet=if($null -eq $u.PasswordLastSet){$null}else{"
        "$u.PasswordLastSet.ToUniversalTime().ToString('o')}}}catch{"
        "[pscustomobject]@{Name=$name;Missing=$true}}};"
        "$items|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            return (), f"PowerShell exit {completed.returncode}: {completed.stderr.strip()}"
        value = json.loads(completed.stdout)
        values = value if isinstance(value, list) else [value]
        accounts = tuple(sorted((dict(item) for item in values), key=lambda item: str(item.get("Name"))))
        return accounts, None
    except BaseException as exc:
        return (), f"{type(exc).__name__}: {exc}"


def capture_sandbox_fingerprint(home: Path) -> SandboxFingerprint:
    accounts, accounts_error = _read_sandbox_accounts()
    return SandboxFingerprint(
        accounts=accounts,
        accounts_error=accounts_error,
        setup_marker=_file_metadata(home / ".sandbox" / "setup_marker.json", include_hash=True),
        secrets_directory=_secrets_directory_metadata(home / ".sandbox-secrets"),
    )


def fingerprint_is_complete(fingerprint: SandboxFingerprint) -> bool:
    return (
        fingerprint.accounts_error is None
        and fingerprint.setup_marker.get("error") is None
        and fingerprint.secrets_directory.get("error") is None
    )


def fingerprints_match(
    before: SandboxFingerprint,
    after: SandboxFingerprint,
) -> bool:
    return before == after


def artifact_safety_violations(artifacts: Path) -> list[dict[str, str]]:
    """Codex 홈·인증 상태가 프로젝트 산출물에 들어왔는지 내용 없이 검사한다."""

    if not artifacts.exists():
        return []
    violations: list[dict[str, str]] = []
    database_prefixes = ("state_", "goals_", "logs_", "memories_", "queue_")
    for path in artifacts.rglob("*"):
        name = path.name.lower()
        kind: str | None = None
        if name == ".sandbox-secrets":
            kind = "sandbox_secrets"
        elif path.is_file() and name == "auth.json":
            kind = "auth_file"
        elif path.is_file() and name.endswith((".sqlite", ".sqlite-shm", ".sqlite-wal")) and name.startswith(database_prefixes):
            kind = "codex_state_database"
        if kind is not None:
            violations.append(
                {"kind": kind, "path": path.relative_to(artifacts).as_posix()}
            )
    return sorted(violations, key=lambda item: (item["kind"], item["path"]))


def capture_windows_vm_identity() -> dict[str, Any]:
    """고유 장치 식별자를 수집하지 않고 Windows VM 여부를 보조 확인한다."""

    if os.name != "nt":
        return {
            "recognized_virtual_machine": False,
            "manufacturer": None,
            "model": None,
            "hypervisor_present": None,
            "error": "Windows가 아닌 환경입니다.",
        }
    script = (
        "$c=Get-CimInstance Win32_ComputerSystem;"
        "[pscustomobject]@{Manufacturer=$c.Manufacturer;Model=$c.Model;"
        "HypervisorPresent=$c.HypervisorPresent}|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            return {
                "recognized_virtual_machine": False,
                "manufacturer": None,
                "model": None,
                "hypervisor_present": None,
                "error": f"PowerShell exit {completed.returncode}: {completed.stderr.strip()}",
            }
        value = json.loads(completed.stdout)
        manufacturer = str(value.get("Manufacturer") or "")
        model = str(value.get("Model") or "")
        signature = f"{manufacturer} {model}".casefold()
        vm_markers = (
            "virtual machine",
            "vmware",
            "virtualbox",
            "kvm",
            "qemu",
            "xen",
            "parallels",
            "google compute engine",
            "amazon ec2",
        )
        recognized = any(marker in signature for marker in vm_markers)
        return {
            "recognized_virtual_machine": recognized,
            "manufacturer": manufacturer or None,
            "model": model or None,
            "hypervisor_present": value.get("HypervisorPresent"),
            "error": None,
        }
    except BaseException as exc:
        return {
            "recognized_virtual_machine": False,
            "manufacturer": None,
            "model": None,
            "hypervisor_present": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def disposable_vm_confirmed(
    environment: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    return environment == "disposable-vm" and env.get(VM_CONFIRMATION_ENV) == "1"
