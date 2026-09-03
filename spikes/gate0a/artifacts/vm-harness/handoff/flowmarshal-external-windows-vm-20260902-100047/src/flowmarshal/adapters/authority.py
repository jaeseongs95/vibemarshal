from __future__ import annotations

import csv
import hashlib
import hmac
import os
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..canonical import canonical_bytes
from ..ports import AuthorityProof


def default_control_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "FlowMarshal" / "control" / "approval-capability"
    return Path.home() / ".local" / "share" / "FlowMarshal" / "control" / "approval-capability"


class FileHumanControlAuthority:
    """프로젝트 밖 보호 파일에 보관된 사람 승인 capability.

    capability 원문은 명령행·환경변수·원장·로그로 전달하지 않는다. 원장에는
    검증된 proof의 digest와 한 번 사용된 nonce만 저장한다.
    """

    def __init__(self, *, clock: object, capability_path: Path | str | None = None) -> None:
        self.clock = clock
        self.capability_path = (
            default_control_path() if capability_path is None else Path(capability_path)
        )

    def initialize(self) -> Path:
        path = self.capability_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._read_secret()
            return path
        secret = secrets.token_bytes(32)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(secret)
                stream.flush()
                os.fsync(stream.fileno())
            self._protect(path)
            created = False
            return path
        finally:
            secret = b""
            if created:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def _protect(self, path: Path) -> None:
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
            return
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        parsed = next(csv.reader([result.stdout.strip()]))
        if len(parsed) < 2 or not parsed[1].startswith("S-"):
            raise RuntimeError("현재 Windows 사용자 SID를 확인하지 못했습니다.")
        user_sid = parsed[1]
        for target, grants in (
            (
                path.parent,
                [f"*{user_sid}:(OI)(CI)(F)", "*S-1-5-18:(OI)(CI)(F)"],
            ),
            (path, [f"*{user_sid}:(F)", "*S-1-5-18:(F)"]),
        ):
            subprocess.run(
                ["icacls", str(target), "/inheritance:r", "/grant:r", *grants],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

    def _read_secret(self) -> bytes:
        secret = self.capability_path.read_bytes()
        if len(secret) != 32:
            raise RuntimeError("HumanControlAuthority capability 크기가 올바르지 않습니다.")
        return secret

    @staticmethod
    def _proof_message(
        action: str,
        target_digest: str,
        nonce: str,
        issued_at: str,
        expires_at: str,
    ) -> bytes:
        return canonical_bytes(
            {
                "action": action,
                "target_digest": target_digest,
                "nonce": nonce,
                "issued_at": issued_at,
                "expires_at": expires_at,
            }
        )

    def issue(
        self, action: str, target_digest: str, *, ttl_seconds: int = 300
    ) -> AuthorityProof:
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("승인 proof 유효 시간은 1~3600초여야 합니다.")
        now = _parse_timestamp(self.clock.now())
        issued_at = _format_timestamp(now)
        expires_at = _format_timestamp(now + timedelta(seconds=ttl_seconds))
        nonce = secrets.token_hex(24)
        secret = self._read_secret()
        try:
            signature = hmac.new(
                secret,
                self._proof_message(
                    action, target_digest, nonce, issued_at, expires_at
                ),
                hashlib.sha256,
            ).hexdigest()
        finally:
            secret = b""
        return AuthorityProof(
            action=action,
            target_digest=target_digest,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
            proof=signature,
        )

    def verify(
        self, proof: AuthorityProof, action: str, target_digest: str
    ) -> bool:
        if proof.action != action or proof.target_digest != target_digest:
            return False
        try:
            now = _parse_timestamp(self.clock.now())
            issued = _parse_timestamp(proof.issued_at)
            expires = _parse_timestamp(proof.expires_at)
        except ValueError:
            return False
        if expires <= issued or now >= expires or issued > now + timedelta(seconds=5):
            return False
        try:
            secret = self._read_secret()
        except (OSError, RuntimeError):
            return False
        try:
            expected = hmac.new(
                secret,
                self._proof_message(
                    proof.action,
                    proof.target_digest,
                    proof.nonce,
                    proof.issued_at,
                    proof.expires_at,
                ),
                hashlib.sha256,
            ).hexdigest()
        finally:
            secret = b""
        return hmac.compare_digest(expected, proof.proof)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone이 없는 시각입니다.")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
