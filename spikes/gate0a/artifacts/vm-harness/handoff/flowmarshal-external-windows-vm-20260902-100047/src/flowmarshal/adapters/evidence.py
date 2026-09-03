from __future__ import annotations

import os
import secrets
from pathlib import Path

from ..canonical import sha256_bytes
from ..ports import EvidenceObject


class FileEvidenceStore:
    """내용 주소 방식의 변경 불가능한 증거 저장소."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @staticmethod
    def _hex_digest(digest: str) -> str:
        prefix = "sha256:"
        if not digest.startswith(prefix):
            raise ValueError("지원하지 않는 증거 digest 형식입니다.")
        value = digest[len(prefix) :]
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("올바르지 않은 SHA-256 digest입니다.")
        return value

    def path_for(self, digest: str) -> Path:
        value = self._hex_digest(digest)
        return self.root / "sha256" / value

    def put(self, payload: bytes) -> EvidenceObject:
        digest = sha256_bytes(payload)
        destination = self.path_for(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != payload:
                raise RuntimeError("기존 증거 파일의 digest와 내용이 다릅니다.")
            return EvidenceObject(digest=digest, size=len(payload), path=destination)

        temporary = destination.with_name(
            f".{destination.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temporary, destination)
            except OSError:
                if not destination.exists() or destination.read_bytes() != payload:
                    raise
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if destination.read_bytes() != payload:
            raise RuntimeError("증거 파일 원자 저장 검증에 실패했습니다.")
        return EvidenceObject(digest=digest, size=len(payload), path=destination)

    def verify(self, digest: str, size: int) -> bool:
        try:
            path = self.path_for(digest)
            if not path.is_file() or path.stat().st_size != size:
                return False
            return sha256_bytes(path.read_bytes()) == digest
        except (OSError, ValueError):
            return False
