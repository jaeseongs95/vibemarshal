"""Core의 완료 provider turn을 뒤따르는 AGS 호출에 결속해 서명한다.

서명 키는 Engine을 띄우는 신뢰 경계가 객체로 주입한다. CLI, workspace 파일이나 MCP 인자는 키를 만들거나
선택할 수 없다. 제품 경로는 명시 A2 profile(``flowmarshal-same-user-v1``) 설정 파일 하나로만 producer를
만든다(``_load_a2_producer``). 보호 VM producer(``_load_installed_producer``)는 후속 B 경로이며 제품 설정이
자동으로 고르지 않는다.

A2는 같은 OS 사용자 경계다. 같은 사용자는 FM 원장, Claude transcript, producer key와 A2 설정을 조작할 수 있으므로
A2 서명은 "선택한 FM producer가 이 내용을 냈고 현재 호출에 결속됐다"만 보인다. 원래 관측의 진실성, OS 격리나
보호 VM ``strong`` 근거가 아니다. model class와 actor는 FM 서명 주장이다.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_private_key

from ..canonical import sha256_digest
from .model_observation import authoritative_receipt_model_observation


class ProducerUnavailable(ValueError):
    """출처 또는 현재 Core 결속을 증명하지 못해 receipt를 발급하지 않는다."""


_HOST_ID = "flowmarshal-engine"
_DOMAIN = "vm-provider-terminal-to-governance"
_TERMINAL_STATUSES = frozenset({"succeeded", "completed"})
_SAFE_INTEGER = 2**53 - 1
_KEY_PATH = (Path(r"C:\ProgramData\flowmarshal\ags-producer-key.json") if os.name == "nt"
             else Path("/etc/flowmarshal/ags-producer-key.json"))
_PIN_PATH = (Path(r"C:\ProgramData\agent-governance-suite\vm-operator-policy.json") if os.name == "nt"
             else Path("/etc/agent-governance-suite/vm-operator-policy.json"))
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PRIVILEGED_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
_WINDOWS_READ_RIGHTS = 0x1200A9
_WINDOWS_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
# AGS F01 계약(host-integration.v1.schema.json $defs/trustProfile)의 A2 profile. 값 하나라도 다르면 A2가 아니다.
A2_PROFILE_ID = "flowmarshal-same-user-v1"
A2_PROFILE = {
    "profileId": A2_PROFILE_ID, "assuranceTier": "same-user", "hostId": "flowmarshal",
    "receiptDomain": "fm-same-user-provider-terminal-to-governance-v1",
    "dispatchDomain": "ags-fm-same-user-dispatch-registration-v1",
    "modelClassSource": "flowmarshal-signed-assertion", "actorSource": "flowmarshal-signed-assertion",
    "keyNamespace": A2_PROFILE_ID, "pinNamespace": A2_PROFILE_ID, "stateNamespace": A2_PROFILE_ID,
}
A2_CONFIG_FORMAT = "flowmarshal-governance-a2-profile-v1"
_VM_DISPATCH_DOMAIN = "ags-vm-dispatch-registration-v1"


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return all(getattr(before, name) == getattr(after, name) for name in (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_gid"))


def _windows_protected(path: Path, *, secret: bool, is_file: bool) -> bool:
    script = (
        "$ErrorActionPreference='Stop'; $p=[Console]::In.ReadToEnd(); "
        "$a=if ([IO.Directory]::Exists($p)) { [IO.Directory]::GetAccessControl($p) } "
        "else { [IO.File]::GetAccessControl($p) }; "
        "$owner=$a.GetOwner([Security.Principal.SecurityIdentifier]).Value; "
        "$rules=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | "
        "ForEach-Object { @{ sid=$_.IdentityReference.Value; rights=[int]$_.FileSystemRights; "
        "type=$_.AccessControlType.ToString() } }); "
        "@{ owner=$owner; rules=$rules } | ConvertTo-Json -Compress -Depth 4"
    )
    try:
        result = subprocess.run(
            [_WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
            input=str(path), text=True, capture_output=True, timeout=5, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        acl = json.loads(result.stdout)
        if not isinstance(acl, dict) or acl.get("owner") not in _PRIVILEGED_SIDS:
            return False
        rules = acl.get("rules")
        if not isinstance(rules, list):
            return False
        for rule in rules:
            if (not isinstance(rule, dict) or not isinstance(rule.get("sid"), str)
                    or type(rule.get("rights")) is not int or rule.get("type") not in {"Allow", "Deny"}):
                return False
            if rule["type"] == "Allow" and rule["sid"] not in _PRIVILEGED_SIDS:
                allowed = 0 if secret and is_file else _WINDOWS_READ_RIGHTS
                if rule["rights"] & ~allowed:
                    return False
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _protected(path: Path, status: os.stat_result, *, secret: bool, is_file: bool) -> bool:
    if os.name == "nt":
        return _windows_protected(path, secret=secret, is_file=is_file)
    allowed = 0o077 if secret and is_file else 0o022
    return status.st_uid == 0 and status.st_mode & allowed == 0


def _read_protected_json(
    path: Path, *, secret: bool,
    protection: Callable[[Path, os.stat_result, bool, bool], bool] | None = None,
    after_validation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """운영 경로는 보호 검사 기본값만 사용한다. 콜백은 합성 fixture 전용이다."""
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_INVALID")
    parts = path.parts
    targets = [Path(parts[0])]
    for part in parts[1:]:
        targets.append(targets[-1] / part)
    snapshots: list[os.stat_result] = []
    for index, target in enumerate(targets):
        try:
            current = target.lstat()
            is_file = index == len(targets) - 1
            if (stat.S_ISLNK(current.st_mode)
                    or (os.name == "nt" and getattr(current, "st_file_attributes", 0) & 0x400)
                    or (not stat.S_ISREG(current.st_mode) if is_file else not stat.S_ISDIR(current.st_mode))):
                raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_INVALID")
            checker = protection or (lambda p, s, sec, file: _protected(p, s, secret=sec, is_file=file))
            if not checker(target, current, secret, is_file):
                raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PERMISSIONS_INVALID")
            if not _same_file(current, target.lstat()):
                raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_CHANGED")
            snapshots.append(current)
        except OSError as error:
            raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_UNAVAILABLE") from error
    if after_validation is not None:
        after_validation()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                     | (getattr(os, "O_NOFOLLOW", 0) if os.name != "nt" else 0))
    except OSError as error:
        raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_CHANGED") from error
    with os.fdopen(fd, "rb") as stream:
        if not _same_file(snapshots[-1], os.fstat(stream.fileno())):
            raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_CHANGED")
        raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ProducerUnavailable("VM_PRODUCER_PROTECTED_FILE_TOO_LARGE")
        if not _same_file(snapshots[-1], os.fstat(stream.fileno())):
            raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_CHANGED")
        for target, original in zip(targets, snapshots):
            try:
                if not _same_file(original, target.lstat()):
                    raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_CHANGED")
            except OSError as error:
                raise ProducerUnavailable("VM_PRODUCER_PROTECTED_PATH_CHANGED") from error
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate")
        return dict(pairs)
    try:
        parsed = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError) as error:
        raise ProducerUnavailable("VM_PRODUCER_PROTECTED_JSON_INVALID") from error
    if not isinstance(parsed, dict):
        raise ProducerUnavailable("VM_PRODUCER_PROTECTED_JSON_INVALID")
    return parsed


def _load_installed_producer(
    key_path: Path = _KEY_PATH, pin_path: Path = _PIN_PATH,
    *, protection: Callable[[Path, os.stat_result, bool, bool], bool] | None = None,
) -> "AGSObservationProducer":
    def load() -> tuple[Ed25519PrivateKey, str, str]:
        key = _read_protected_json(key_path, secret=True, protection=protection)
        policy = _read_protected_json(pin_path, secret=False, protection=protection)
        if (set(key) != {"version", "installationId", "keyId", "hostId", "modelPolicyVersion", "privateKeyPkcs8"}
                or type(key["version"]) is not int or key["version"] != 1 or key["hostId"] != _HOST_ID
                or any(not isinstance(key[name], str) or not key[name]
                       for name in ("installationId", "keyId", "modelPolicyVersion", "privateKeyPkcs8"))
                or set(policy) != {"version", "modelPolicyVersion", "pins", "hostBuilds", "models"}
                or type(policy["version"]) is not int or policy["version"] != 1
                or not isinstance(policy["modelPolicyVersion"], str)
                or not isinstance(policy["pins"], list)
                or key["modelPolicyVersion"] != policy["modelPolicyVersion"]):
            raise ProducerUnavailable("VM_PRODUCER_INSTALLATION_POLICY_MISMATCH")
        pins = [pin for pin in policy["pins"] if isinstance(pin, dict) and pin.get("keyId") == key["keyId"]]
        if len(pins) != 1:
            raise ProducerUnavailable("VM_PRODUCER_PIN_UNAVAILABLE")
        pin = pins[0]
        if (set(pin) != {"keyId", "installationId", "hostId", "publicKeySpki", "hostBuildDigest", "modelPolicyVersion", "status"}
                or pin["installationId"] != key["installationId"] or pin["hostId"] != _HOST_ID
                or pin["modelPolicyVersion"] != key["modelPolicyVersion"] or pin["status"] != "active"
                or not isinstance(pin["hostBuildDigest"], str) or not _DIGEST.fullmatch(pin["hostBuildDigest"])
                or not isinstance(pin["publicKeySpki"], str)):
            raise ProducerUnavailable("VM_PRODUCER_PIN_MISMATCH")
        try:
            encoded = base64.b64decode(key["privateKeyPkcs8"], validate=True)
            private = load_der_private_key(encoded, password=None)
            public = base64.b64decode(pin["publicKeySpki"], validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise ProducerUnavailable("VM_PRODUCER_KEY_INVALID") from error
        if (not isinstance(private, Ed25519PrivateKey)
                or private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) != public):
            raise ProducerUnavailable("VM_PRODUCER_KEY_PIN_MISMATCH")
        return private, key["installationId"], key["keyId"]

    private, installation_id, key_id = load()

    def current() -> None:
        refreshed, current_installation, current_key = load()
        if (current_installation != installation_id or current_key != key_id
                or refreshed.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
                != private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)):
            raise ProducerUnavailable("VM_PRODUCER_INSTALLATION_CHANGED")

    return AGSObservationProducer(
        private_key=private, installation_id=installation_id, key_id=key_id,
        instance_id="vm-instance-" + secrets.token_hex(16),
        session_id="vm-session-" + secrets.token_hex(16), pin_check=current,
    )


def _a2_digest(value: Any) -> str:
    """AGS canonicalJson UTF-8 bytes의 SHA-256(F01 freezeIdentity·pinSetDigest·resourceBindingDigest 계산식)."""
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_a2_json(path: Any, code: str) -> dict[str, Any]:
    """same-user 파일을 읽는다. OS 보호는 검사·주장하지 않고 경로 형태·링크·크기·중복 key만 닫는다."""
    if (not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path
            or Path(path) in (_KEY_PATH, _PIN_PATH)):
        raise ProducerUnavailable(f"A2_PRODUCER_{code}_PATH_INVALID")
    try:
        status = Path(path).lstat()
        if (stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode)
                or getattr(status, "st_file_attributes", 0) & 0x400):
            raise ProducerUnavailable(f"A2_PRODUCER_{code}_PATH_INVALID")
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ProducerUnavailable(f"A2_PRODUCER_{code}_UNAVAILABLE") from error

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate")
        return dict(pairs)
    try:
        if len(raw) > 1_048_576:
            raise ValueError("too large")
        parsed = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError) as error:
        raise ProducerUnavailable(f"A2_PRODUCER_{code}_JSON_INVALID") from error
    if not isinstance(parsed, dict):
        raise ProducerUnavailable(f"A2_PRODUCER_{code}_JSON_INVALID")
    return parsed


def _load_a2_producer(config_path: Path) -> "AGSObservationProducer":
    """명시 A2 profile 설정 하나로 producer를 만든다. 어긋나면 ProducerUnavailable이며 다른 profile로 넘어가지 않는다.

    설정은 ``{"format", "selection", "resources"}``다. ``selection``은 AGS 서버가 고정하는 F01 ``serverProfileSelection``
    과 같은 객체이고 ``resources``는 그 ``resourceBindingDigest``의 입력이다. key·pin 파일은 A2 namespace여야 하며
    digest와 freezeIdentity를 다시 계산해 대조한다. state는 AGS 서버의 자원이라 FM은 digest 결속만 확인한다.
    """
    def load() -> tuple[Ed25519PrivateKey, str, str]:
        config = _read_a2_json(str(config_path), "CONFIG")
        selection, resources = config.get("selection"), config.get("resources")
        if (set(config) != {"format", "selection", "resources"} or config["format"] != A2_CONFIG_FORMAT
                or not isinstance(selection, dict) or not isinstance(resources, dict)
                or set(selection) != {"source", "profile", "pinSetDigest", "resourceBindingDigest", "freezeIdentity"}
                or selection["source"] != "server-local-operator-config"
                or selection["profile"] != A2_PROFILE or set(resources) != {"key", "pin", "state"}):
            raise ProducerUnavailable("A2_PRODUCER_PROFILE_MISMATCH")
        for kind in ("key", "pin", "state"):
            item = resources[kind]
            if (not isinstance(item, dict) or set(item) != {"namespace", "location"}
                    or item["namespace"] != A2_PROFILE[f"{kind}Namespace"]):
                raise ProducerUnavailable("A2_PRODUCER_NAMESPACE_MISMATCH")
        if selection["resourceBindingDigest"] != _a2_digest(resources):
            raise ProducerUnavailable("A2_PRODUCER_RESOURCE_BINDING_MISMATCH")
        if selection["freezeIdentity"] != _a2_digest(
                {name: value for name, value in selection.items() if name != "freezeIdentity"}):
            raise ProducerUnavailable("A2_PRODUCER_FREEZE_IDENTITY_MISMATCH")
        key = _read_a2_json(resources["key"]["location"], "KEY")
        pins = _read_a2_json(resources["pin"]["location"], "PIN")
        if (set(key) != {"version", "namespace", "keyId", "hostId", "privateKeyPkcs8"}
                or type(key["version"]) is not int or key["version"] != 1
                or key["namespace"] != A2_PROFILE["keyNamespace"] or key["hostId"] != A2_PROFILE["hostId"]
                or not isinstance(key["keyId"], str) or not key["keyId"]
                or not isinstance(key["privateKeyPkcs8"], str)):
            raise ProducerUnavailable("A2_PRODUCER_KEY_MISMATCH")
        if (set(pins) != {"namespace", "pins"} or pins["namespace"] != A2_PROFILE["pinNamespace"]
                or not isinstance(pins["pins"], list)):
            raise ProducerUnavailable("A2_PRODUCER_PIN_MISMATCH")
        if selection["pinSetDigest"] != _a2_digest(pins):
            raise ProducerUnavailable("A2_PRODUCER_PIN_SET_DIGEST_MISMATCH")
        matched = [pin for pin in pins["pins"] if isinstance(pin, dict) and pin.get("keyId") == key["keyId"]]
        if (len(matched) != 1 or set(matched[0]) != {"keyId", "publicKeySpki", "status"}
                or matched[0]["status"] != "active" or not isinstance(matched[0]["publicKeySpki"], str)):
            raise ProducerUnavailable("A2_PRODUCER_PIN_UNAVAILABLE")
        try:
            private = load_der_private_key(base64.b64decode(key["privateKeyPkcs8"], validate=True), password=None)
            public = base64.b64decode(matched[0]["publicKeySpki"], validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise ProducerUnavailable("A2_PRODUCER_KEY_INVALID") from error
        if (not isinstance(private, Ed25519PrivateKey)
                or private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) != public):
            raise ProducerUnavailable("A2_PRODUCER_KEY_PIN_MISMATCH")
        return private, key["keyId"], selection["freezeIdentity"]

    private, key_id, freeze_identity = load()

    def current() -> None:
        refreshed, current_key, current_freeze = load()
        if (current_key != key_id or current_freeze != freeze_identity
                or refreshed.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
                != private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)):
            raise ProducerUnavailable("A2_PRODUCER_PROFILE_CHANGED")

    # A2에는 operator installation이 없다. producer 식별은 profile·freezeIdentity·keyId다.
    return AGSObservationProducer(
        private_key=private, installation_id=f"{A2_PROFILE_ID}:{freeze_identity}", key_id=key_id,
        instance_id="fm-instance-" + secrets.token_hex(16), session_id="fm-session-" + secrets.token_hex(16),
        pin_check=current, profile=A2_PROFILE, freeze_identity=freeze_identity,
    )


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProducerUnavailable(f"VM_PRODUCER_{name.upper()}_MISSING")
    return value


def _time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as error:
        raise ProducerUnavailable("VM_PRODUCER_TIME_INVALID") from error
    if result.tzinfo is None:
        raise ProducerUnavailable("VM_PRODUCER_TIME_INVALID")
    return result.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    """AGS canonicalJson과 같은 허용 JSON subset. 미지원 숫자·값은 닫는다."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _SAFE_INTEGER:
        return str(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProducerUnavailable("VM_PRODUCER_STRING_INVALID") from error
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_canonical(key) + ":" + _canonical(value[key]) for key in keys) + "}"
    raise ProducerUnavailable("VM_PRODUCER_CANONICAL_INPUT_UNSUPPORTED")


def _terminal(fields: dict[str, Any]) -> dict[str, Any]:
    if fields["status"] not in _TERMINAL_STATUSES:
        raise ProducerUnavailable("VM_PRODUCER_TERMINAL_INCOMPLETE")
    for name in ("eventId", "callId", "threadId", "turnId", "model", "effort", "provenance"):
        _required(fields.get(name), name)
    observed_at = _required(fields.get("observedAt"), "observedAt")
    _time(observed_at)
    return {**fields, "digest": sha256_digest(fields)}


def _steward_terminal(connection: Any, project_id: str, call_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT u.id,u.payload_json,c.receipt_json FROM budget_usage u "
        "JOIN provider_calls c ON c.usage_id=u.id "
        "WHERE u.project_id=? AND u.stage='validation' AND u.logical_call_ref=?",
        (project_id, call_id),
    ).fetchall()
    if len(rows) != 1:
        raise ProducerUnavailable("VM_PRODUCER_STEWARD_EVENT_MISSING")
    row = rows[0]
    usage, receipt = json.loads(row["payload_json"]), json.loads(row["receipt_json"])
    model, effort = authoritative_receipt_model_observation(
        observed_model=usage.get("observed_model"),
        observed_effort=usage.get("observed_effort"),
        binding_provenance=usage.get("binding_provenance"),
    )
    turns = usage.get("turn_ids")
    if (
        model is None or effort is None or not isinstance(turns, list) or len(turns) != 1
        or usage.get("call_status") != "succeeded"
        or usage.get("runner_receipt_digest") != sha256_digest(receipt)
        or receipt.get("call_id") != call_id or receipt.get("turn_ids") != turns
        or receipt.get("thread_id") != usage.get("thread_id")
        or receipt.get("status") != "succeeded"
        or receipt.get("observed_model") != model
        or receipt.get("observed_effort") != effort
        or (receipt.get("binding_provenance") or {}).get("observed")
           != usage["binding_provenance"]["observed"]
    ):
        raise ProducerUnavailable("VM_PRODUCER_STEWARD_EVENT_MISMATCH")
    return _terminal({
        "eventId": row["id"], "callId": call_id, "threadId": usage["thread_id"],
        "turnId": turns[0], "status": "succeeded", "observedAt": usage["recorded_at"],
        "model": model, "effort": effort, "provenance": usage["binding_provenance"]["observed"],
    })


def _worker_terminal(connection: Any, project_id: str, attempt_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT o.id,o.payload_json,o.observed_at,o.terminal_status,j.thread_id,j.turn_id "
        "FROM runtime_job_observations o JOIN runtime_jobs j ON j.id=o.job_id "
        "WHERE o.project_id=? AND j.attempt_id=? AND o.provider_terminal=1 "
        "ORDER BY o.rowid DESC",
        (project_id, attempt_id),
    ).fetchall()
    usage_rows = connection.execute(
        "SELECT payload_json FROM budget_usage WHERE project_id=? AND stage='execution' "
        "AND json_extract(payload_json,'$.attempt_id')=? ORDER BY rowid DESC",
        (project_id, attempt_id),
    ).fetchall()
    if len(rows) != 1 or len(usage_rows) != 1:
        raise ProducerUnavailable("VM_PRODUCER_WORKER_EVENT_MISSING")
    row, usage = rows[0], json.loads(usage_rows[0]["payload_json"])
    result = json.loads(row["payload_json"]).get("result")
    stored_observation = usage.get("provider_observation")
    original_payload = result.get("payload") if isinstance(result, dict) else None
    stored_payload = (stored_observation.get("payload")
                      if isinstance(stored_observation, dict) else None)
    raw_core_payload = ({key: value for key, value in original_payload.items()
                         if key not in {"operation_trace", "operation_trace_ref", "operation_trace_digest"}}
                        if isinstance(original_payload, dict) else None)
    core_payload = ({key: value for key, value in stored_payload.items()
                     if key not in {"operation_trace", "operation_trace_ref", "operation_trace_digest"}}
                    if isinstance(stored_payload, dict) else None)
    model, effort = authoritative_receipt_model_observation(
        observed_model=usage.get("observed_model"),
        observed_effort=usage.get("observed_effort"),
        binding_provenance=usage.get("binding_provenance"),
    )
    if (
        not isinstance(result, dict) or not isinstance(stored_observation, dict)
        or model is None or effort is None
        or row["terminal_status"] != "completed"
        or result.get("active") is not False
        or result.get("terminal_status") != row["terminal_status"]
        or result.get("thread_id") != row["thread_id"]
        or result.get("turn_id") != row["turn_id"]
        or usage.get("thread_id") != row["thread_id"]
        or usage.get("turn_ids") != [row["turn_id"]]
        or usage.get("call_status") != row["terminal_status"]
        or stored_observation.get("thread_id") != row["thread_id"]
        or stored_observation.get("turn_id") != row["turn_id"]
        or stored_observation.get("terminal_status") != row["terminal_status"]
        or raw_core_payload is None or core_payload != raw_core_payload
        or usage.get("runner_receipt_digest") != sha256_digest(stored_observation)
        or original_payload.get("observed_model") != model
        or original_payload.get("observed_effort") != effort
        or original_payload.get("model_observation_source")
           != usage["binding_provenance"]["observed"]
        or not usage.get("runtime_receipt_id")
    ):
        raise ProducerUnavailable("VM_PRODUCER_WORKER_EVENT_MISMATCH")
    return _terminal({
        "eventId": row["id"], "callId": usage["runtime_receipt_id"],
        "threadId": row["thread_id"], "turnId": row["turn_id"],
        "status": row["terminal_status"], "observedAt": row["observed_at"],
        "model": model, "effort": effort, "provenance": usage["binding_provenance"]["observed"],
    })


class AGSObservationProducer:
    """신뢰된 Engine bootstrap이 만든 signer. 제품 gate는 명시 A2 profile로 만든 것만 받고, 없으면 dispatch를 막는다."""

    def __init__(
        self, *, private_key: Ed25519PrivateKey, installation_id: str, key_id: str,
        instance_id: str, session_id: str,
        clock: Callable[[], datetime] | None = None,
        nonce: Callable[[], str] | None = None,
        invocation_id: Callable[[], str] | None = None,
        pin_check: Callable[[], None] | None = None,
        profile: dict[str, str] | None = None, freeze_identity: str | None = None,
    ) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ProducerUnavailable("VM_PRODUCER_KEY_UNAVAILABLE")
        # profile이 없으면 기존 보호 VM domain이다. A2는 F01 profile 전체와 freezeIdentity가 함께 있어야 한다.
        if profile is None and freeze_identity is None:
            self.profile_id = None
            self._host_id, self._domain, self._dispatch_domain = _HOST_ID, _DOMAIN, _VM_DISPATCH_DOMAIN
        elif profile == A2_PROFILE and isinstance(freeze_identity, str) and _DIGEST.fullmatch(freeze_identity):
            self.profile_id = A2_PROFILE_ID
            self._host_id, self._domain = profile["hostId"], profile["receiptDomain"]
            self._dispatch_domain = profile["dispatchDomain"]
        else:
            raise ProducerUnavailable("A2_PRODUCER_PROFILE_MISMATCH")
        self.freeze_identity = freeze_identity
        self._key = private_key
        self.installation_id = _required(installation_id, "installationId")
        self.key_id = _required(key_id, "keyId")
        self.instance_id = _required(instance_id, "instanceId")
        self.session_id = _required(session_id, "sessionId")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._nonce = nonce or (lambda: secrets.token_urlsafe(24))
        self._invocation_id = invocation_id or (lambda: "vm-invocation-" + secrets.token_hex(16))
        self._pin_check = pin_check

    def check_installed_pin(self) -> None:
        if self._pin_check is not None:
            self._pin_check()

    def public_key_spki(self) -> bytes:
        """운영자 pin 절차에 전달할 공개키 DER. private key bytes는 내보내지 않는다."""
        return self._key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)

    def issue(
        self, *, service: Any, project_id: str, task_id: str, envelope_task_id: str,
        run_id: str | None, attempt_id: str | None, stage: str, operation_id: str,
        tool: str, arguments: dict[str, Any], terminal_ref: dict[str, str],
        transport: Any | None = None, model_classes: dict[str, str] | None = None,
    ) -> Any:
        """Core prepared operation와 완료 terminal을 다시 읽은 직후 새 invocation을 서명한다.

        A2는 원장에서 다시 읽은 terminal model을 호출자 대응표(model_classes)로 class에 대응시키고, actor를 terminal에서
        유도해 둘 다 FM 서명 주장으로 body에 넣는다. 대응표에 없는 model이면 서명하지 않는다.
        """
        self.check_installed_pin()
        if ((stage in {"bootstrap", "baseline"} and attempt_id is not None)
                or (stage in {"implementation", "scope", "acceptance"} and attempt_id is None)
                or stage not in {"bootstrap", "baseline", "implementation", "scope", "acceptance"}):
            raise ProducerUnavailable("VM_PRODUCER_STAGE_ATTEMPT_MISMATCH")
        with service.ledger.transaction() as tx:
            connection = tx.connection
            core = connection.execute(
                "SELECT g.id AS goal_revision_id,g.revision_no AS goal_revision,"
                "s.id AS spec_revision_id,s.revision_no AS task_revision,"
                "t.project_id,t.plan_revision_id,t.status,p.active_plan_revision_id "
                "FROM task_contracts t JOIN projects p ON p.id=t.project_id "
                "JOIN goal_revisions g ON g.id=p.active_goal_revision_id "
                "JOIN execution_spec_revisions s ON s.task_id=t.id AND s.is_current=1 "
                "WHERE t.id=?",
                (task_id,),
            ).fetchone()
            prepared = connection.execute(
                "SELECT payload_json,sequence FROM history_events WHERE project_id=? "
                "AND entity_type='core_operation' AND entity_id=? "
                "AND event_type='operation.prepared' ORDER BY sequence DESC LIMIT 1",
                (project_id, operation_id),
            ).fetchone()
            if (
                core is None or core["project_id"] != project_id
                or core["plan_revision_id"] != core["active_plan_revision_id"]
                or core["status"] not in {"ready", "materialized", "reserved", "running", "validating"}
                or prepared is None
            ):
                raise ProducerUnavailable("VM_PRODUCER_CORE_STALE")
            request = json.loads(prepared["payload_json"]).get("request", {})
            if (
                request.get("key", {}).get("task_id") != task_id
                or request["key"].get("execution_spec_revision_id") != core["spec_revision_id"]
                or request.get("goalRevisionId") != core["goal_revision_id"]
                or request.get("step") != ("plan" if stage == "bootstrap" else f"record:{stage}")
                or request.get("tool") != tool
                or request.get("arguments") != arguments
                or request.get("observation", {}).get("terminalRef") != terminal_ref
            ):
                raise ProducerUnavailable("VM_PRODUCER_OPERATION_MISMATCH")
            key = request["key"]
            lineage = connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id=? "
                "AND entity_type='core_operation' AND event_type='operation.completed' "
                "AND json_extract(payload_json,'$.kind')='governance_gate' ORDER BY sequence",
                (project_id,),
            ).fetchall()
            completed = [json.loads(row["payload_json"]).get("result", {}) for row in lineage]
            plan = [item for item in completed if item.get("step") == "plan" and item.get("key") == key]
            if stage == "bootstrap":
                planned_task_id = (arguments.get("taskEnvelope") or {}).get("taskId")
            else:
                if len(plan) != 1:
                    raise ProducerUnavailable("VM_PRODUCER_TASK_BINDING_MISMATCH")
                planned_task_id = None
                prior = connection.execute(
                    "SELECT payload_json FROM history_events WHERE project_id=? "
                    "AND entity_type='core_operation' AND event_type='operation.prepared' "
                    "AND json_extract(payload_json,'$.request.step')='plan' ORDER BY sequence DESC",
                    (project_id,),
                ).fetchall()
                for row in prior:
                    plan_request = json.loads(row["payload_json"]).get("request", {})
                    if plan_request.get("key") == key:
                        planned_task_id = ((plan_request.get("arguments") or {}).get("taskEnvelope") or {}).get("taskId")
                        break
            if planned_task_id != envelope_task_id:
                raise ProducerUnavailable("VM_PRODUCER_TASK_BINDING_MISMATCH")
            starts = [item for item in completed if item.get("step") == "start" and item.get("key") == key]
            if stage == "bootstrap":
                if run_id is not None or starts:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH")
            else:
                if len(starts) != 1:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH")
                raw = (starts[0].get("data") or {}).get("raw") or {}
                content = raw.get("content") if isinstance(raw, dict) else None
                try:
                    actual_run_id = json.loads(content[0]["text"])["data"]["runId"]
                except (IndexError, KeyError, TypeError, ValueError) as error:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH") from error
                if actual_run_id != run_id:
                    raise ProducerUnavailable("VM_PRODUCER_RUN_BINDING_MISMATCH")
            attempt_ordinal = None
            if attempt_id is not None:
                attempt = connection.execute(
                    "SELECT attempt_no,status FROM attempts WHERE id=? AND task_id=? "
                    "AND project_id=? AND kind='execution'",
                    (attempt_id, task_id, project_id),
                ).fetchone()
                latest = connection.execute(
                    "SELECT MAX(attempt_no) FROM attempts WHERE task_id=? AND kind='execution'",
                    (task_id,),
                ).fetchone()[0]
                if (attempt is None or attempt["status"] != "succeeded"
                        or attempt["attempt_no"] != latest
                        or request["key"].get("attempt_no") != attempt["attempt_no"]):
                    raise ProducerUnavailable("VM_PRODUCER_ATTEMPT_STALE")
                attempt_ordinal = attempt["attempt_no"]
            else:
                latest = connection.execute(
                    "SELECT COALESCE(MAX(attempt_no),0) FROM attempts WHERE task_id=? AND kind='execution'",
                    (task_id,),
                ).fetchone()[0]
                if key.get("attempt_no") != latest + 1:
                    raise ProducerUnavailable("VM_PRODUCER_ATTEMPT_STALE")
            if terminal_ref.get("kind") == "steward":
                reviews = [item for item in completed if item.get("step") == f"steward:{stage}"
                           and item.get("key") == key]
                if len(reviews) != 1 or (reviews[0].get("data") or {}).get("callId") != terminal_ref.get("callId"):
                    raise ProducerUnavailable("VM_PRODUCER_STEWARD_BINDING_MISMATCH")
                terminal = _steward_terminal(connection, project_id, _required(
                    terminal_ref.get("callId"), "callId"))
            elif terminal_ref.get("kind") == "worker" and attempt_id is not None:
                if terminal_ref.get("attemptId") != attempt_id:
                    raise ProducerUnavailable("VM_PRODUCER_TERMINAL_SOURCE_INVALID")
                terminal = _worker_terminal(connection, project_id, attempt_id)
            else:
                raise ProducerUnavailable("VM_PRODUCER_TERMINAL_SOURCE_INVALID")
            previous = connection.execute(
                "SELECT payload_json,sequence FROM history_events WHERE project_id=? "
                "AND entity_type='core_operation' AND entity_id=? "
                "AND event_type='vm_observation.issued' ORDER BY sequence DESC LIMIT 1",
                (project_id, operation_id),
            ).fetchone()
            if previous is not None:
                payload = json.loads(previous["payload_json"])
                if (payload.get("terminal") != terminal or payload.get("tool") != tool
                        or payload.get("argumentsDigest") != sha256_digest(arguments)):
                    raise ProducerUnavailable("VM_PRODUCER_ISSUED_BINDING_MISMATCH")
                no_effect = connection.execute(
                    "SELECT sequence FROM history_events WHERE project_id=? "
                    "AND entity_type='core_operation' AND entity_id=? "
                    "AND event_type='operation.no_effect' AND sequence>? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (project_id, operation_id, previous["sequence"]),
                ).fetchone()
                if no_effect is None or prepared["sequence"] <= no_effect["sequence"]:
                    if transport is not None:
                        raise ProducerUnavailable("VM_PRODUCER_OUTCOME_UNKNOWN")
                    return payload["receipt"]
            issued = self._clock()
            if issued.tzinfo is None or _time(terminal["observedAt"]) >= issued.astimezone(timezone.utc):
                raise ProducerUnavailable("VM_PRODUCER_CAUSAL_ORDER_INVALID")
            input_value = {key: value for key, value in arguments.items() if key != "_hostAttestation"}
            input_digest = "sha256:" + hashlib.sha256(_canonical(input_value).encode("utf-8")).hexdigest()
            timestamp = _utc(issued)
            encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
            producer = {"installationId": self.installation_id, "keyId": self.key_id,
                        "hostId": self._host_id, "instanceId": self.instance_id}
            binding = {"turnId": terminal["turnId"], "taskId": envelope_task_id,
                       "runId": run_id, "attemptId": attempt_id, "hostId": self._host_id,
                       "sessionId": self.session_id, "instanceId": self.instance_id}
            invocation = {"tool": _required(tool, "tool"), "inputDigest": input_digest,
                          "observedAt": timestamp}
            a2: dict[str, Any] = {}
            if self.profile_id is not None:
                model_class = (model_classes or {}).get(terminal["model"])
                if model_class is None:
                    raise ProducerUnavailable("A2_PRODUCER_MODEL_CLASS_UNDECLARED")
                actor = (f"flowmarshal-engine:steward:{stage}:{terminal['threadId']}"
                         if terminal_ref.get("kind") == "steward" else f"flowmarshal-engine:worker:{attempt_id}")
                a2 = {"profileBinding": {"profileId": self.profile_id, "freezeIdentity": self.freeze_identity},
                      "assertions": {"modelClass": model_class, "actorId": actor}}
            ticket = None
            transport_binding = None
            if transport is not None:
                registration_body = {
                    "version": 1, "domain": self._dispatch_domain, **a2,
                    "serverEpoch": transport.epoch, "nonce": _required(self._nonce(), "nonce"),
                    "issuedAt": timestamp, "expiresAt": _utc(issued + timedelta(seconds=60)),
                    "producer": producer, "binding": binding,
                    "terminal": terminal,
                    "core": {"goalRevision": core["goal_revision"], "taskRevision": core["task_revision"],
                             "attemptOrdinal": attempt_ordinal, "gateOperationKey": operation_id,
                             "stage": _required(stage, "stage")},
                    "invocation": invocation,
                }
                registration_data = _canonical(registration_body).encode("utf-8")
                ticket = transport.reserve({"body": encode(registration_data),
                                            "signature": encode(self._key.sign(registration_data)),
                                            "keyId": self.key_id})
                transport_binding = {"serverEpoch": ticket.epoch,
                                     "registrationDigest": "sha256:" + hashlib.sha256(registration_data).hexdigest()}
            body = {
                "version": 2 if ticket is not None else 1, "domain": self._domain, **a2,
                "producer": producer,
                "binding": {"invocationId": ticket.call_id if ticket is not None else _required(
                    self._invocation_id(), "invocationId"), **binding},
                "terminal": terminal,
                "core": {"goalRevision": core["goal_revision"], "taskRevision": core["task_revision"],
                         "attemptOrdinal": attempt_ordinal, "gateOperationKey": operation_id,
                         "stage": _required(stage, "stage")},
                "invocation": invocation,
                "nonce": _required(self._nonce(), "nonce"), "issuedAt": timestamp,
                "expiresAt": _utc(issued + timedelta(seconds=60)),
            }
            if transport_binding is not None:
                body["transport"] = transport_binding
            data = _canonical(body).encode("utf-8")
            receipt = {"body": encode(data), "signature": encode(self._key.sign(data)), "keyId": self.key_id}
            tx.history(project_id, "vm_observation.issued", "core_operation", operation_id, {
                "receipt": receipt, "terminal": terminal, "tool": tool,
                "argumentsDigest": sha256_digest(arguments),
            })
            return (receipt, ticket) if ticket is not None else receipt
