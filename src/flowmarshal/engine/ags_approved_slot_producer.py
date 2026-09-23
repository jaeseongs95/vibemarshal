"""VM Core의 현재 승인 RoleSlot source만 AGS 전용 control RPC에 서명한다."""
from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..canonical import sha256_digest
from .ags_observation_producer import AGSObservationProducer, ProducerUnavailable, _canonical, _utc


_DOMAIN = "ags-vm-approved-slot-source-v1"
_HOST_ID = "flowmarshal-engine"


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


class ApprovedSlotProducer:
    """설치 signer를 사용하되 provider-terminal 서명 domain과 발급 경로를 공유하지 않는다."""

    def __init__(self, signer: AGSObservationProducer, *, clock: Callable[[], datetime] | None = None,
                 nonce: Callable[[], str] | None = None) -> None:
        if not isinstance(signer, AGSObservationProducer):
            raise TypeError("trusted AGSObservationProducer instance required")
        self._signer = signer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._nonce = nonce or (lambda: secrets.token_urlsafe(24))

    def register(self, *, service: Any, project_id: str, task_id: str,
                 transport: Any, expected: dict[str, Any] | None = None) -> Any:
        if not isinstance(project_id, str) or not project_id or not isinstance(task_id, str) or not task_id:
            raise ProducerUnavailable("VM_APPROVED_SLOT_TARGET_INVALID")
        self._signer.check_installed_pin()

        def sign(epoch: str, invocation_id: str) -> tuple[dict[str, str], str]:
            # Source 본문은 호출자가 제공할 수 없다. Port가 현재 승인 상태를 한 read transaction에서 판정한다.
            source = service.read_current_approved_role_slot_source(project_id, task_id, expected)
            self._signer.check_installed_pin()
            if source.get("owner") != _HOST_ID or source.get("project_id") != project_id or source.get("task_id") != task_id:
                raise ProducerUnavailable("VM_APPROVED_SLOT_SOURCE_MISMATCH")
            snapshot_digest = source.get("snapshot_digest")
            if snapshot_digest != sha256_digest({key: value for key, value in source.items()
                                                 if key != "snapshot_digest"}):
                raise ProducerUnavailable("VM_APPROVED_SLOT_SOURCE_DIGEST_INVALID")
            issued = self._clock()
            if not isinstance(issued, datetime) or issued.tzinfo is None:
                raise ProducerUnavailable("VM_APPROVED_SLOT_TIME_INVALID")
            issued = issued.astimezone(timezone.utc)
            nonce = self._nonce()
            if not isinstance(nonce, str) or len(nonce) < 24:
                raise ProducerUnavailable("VM_APPROVED_SLOT_NONCE_INVALID")
            body = {
                "version": 1, "domain": _DOMAIN,
                "producer": {
                    "installationId": self._signer.installation_id, "keyId": self._signer.key_id,
                    "hostId": _HOST_ID, "instanceId": self._signer.instance_id,
                    "sessionId": self._signer.session_id,
                },
                "binding": {"invocationId": invocation_id, "serverEpoch": epoch,
                            "projectId": project_id, "taskId": task_id,
                            "snapshotDigest": snapshot_digest},
                "nonce": nonce, "issuedAt": _utc(issued),
                "expiresAt": _utc(issued + timedelta(seconds=60)),
                "source": _json_value(source),
            }
            data = _canonical(body).encode("utf-8")
            encode = lambda item: base64.urlsafe_b64encode(item).rstrip(b"=").decode("ascii")
            return ({"body": encode(data), "signature": encode(self._signer._key.sign(data)),
                     "keyId": self._signer.key_id}, snapshot_digest)

        return transport.register(sign)
