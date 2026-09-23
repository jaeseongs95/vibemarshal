"""VM Core 전용 AGS stdio 연결의 예약 dispatch ID를 단회 사용한다."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from .ags_observation_producer import _canonical


class DispatchUnavailable(ValueError):
    """인증 예약 문맥이 없거나 현재 연결과 맞지 않는다."""


@dataclass(frozen=True)
class DispatchTicket:
    call_id: str
    epoch: str
    token: str


class AuthenticatedDispatchTransport:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._epoch: str | None = None
        self._pending: dict[str, DispatchTicket] = {}

    @property
    def epoch(self) -> str:
        if self._epoch is None:
            result = self._client._request("vm/hello", {})
            epoch = result.get("serverEpoch") if isinstance(result, dict) else None
            if not isinstance(epoch, str) or len(epoch) < 32:
                raise DispatchUnavailable("VM_DISPATCH_EPOCH_INVALID")
            self._epoch = epoch
        return self._epoch

    def reserve(self, registration: dict[str, str]) -> DispatchTicket:
        epoch = self.epoch
        result = self._client._request("vm/reserve_dispatch", {"registration": registration})
        call_id = result.get("callId") if isinstance(result, dict) else None
        returned_epoch = result.get("serverEpoch") if isinstance(result, dict) else None
        if (not isinstance(call_id, str) or not call_id or len(call_id) > 256
                or returned_epoch != epoch or call_id in self._pending):
            raise DispatchUnavailable("VM_DISPATCH_RESERVATION_INVALID")
        ticket = DispatchTicket(call_id, epoch, secrets.token_urlsafe(24))
        self._pending[call_id] = ticket
        return ticket

    def call(self, ticket: DispatchTicket, receipt: dict[str, str], tool: str,
             arguments: dict[str, Any]) -> Any:
        if self._pending.pop(ticket.call_id, None) != ticket or ticket.epoch != self._epoch:
            raise DispatchUnavailable("VM_DISPATCH_TICKET_INVALID")
        try:
            padded = receipt["body"] + "=" * (-len(receipt["body"]) % 4)
            body = json.loads(base64.urlsafe_b64decode(padded))
            binding = body["binding"]
            unsigned = {key: value for key, value in arguments.items() if key != "_hostAttestation"}
            digest = "sha256:" + hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
            if (binding["invocationId"] != ticket.call_id
                    or body["transport"]["serverEpoch"] != ticket.epoch
                    or body["invocation"]["tool"] != tool
                    or body["invocation"]["inputDigest"] != digest):
                raise ValueError("receipt binding mismatch")
        except (KeyError, TypeError, ValueError, binascii.Error, UnicodeDecodeError) as error:
            raise DispatchUnavailable("VM_DISPATCH_RECEIPT_MISMATCH") from error
        return self._client.call_reserved(ticket.call_id, tool, {**arguments, "_hostAttestation": receipt})


class ApprovedSlotControlTransport:
    """승인 source를 tools/call과 분리된 자식 MCP pipe의 control RPC로 보낸다."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def register(self, sign: Any) -> Any:
        hello = self._client._request("vm/hello", {})
        epoch = hello.get("serverEpoch") if isinstance(hello, dict) else None
        if not isinstance(epoch, str) or len(epoch) < 32:
            raise DispatchUnavailable("VM_APPROVED_SLOT_EPOCH_INVALID")
        invocation_id = "vm-approved-slot-" + secrets.token_urlsafe(24)
        receipt, snapshot_digest = sign(epoch, invocation_id)
        result = self._client._request(
            "vm/register_approved_slot", {"signedSource": receipt}, request_id=invocation_id,
        )
        if (not isinstance(result, dict) or result.get("accepted") is not True
                or result.get("invocationId") != invocation_id
                or result.get("serverEpoch") != epoch
                or result.get("snapshotDigest") != snapshot_digest):
            raise DispatchUnavailable("VM_APPROVED_SLOT_CONTROL_RESPONSE_INVALID")
        return result
