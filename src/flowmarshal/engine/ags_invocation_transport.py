"""AGS stdio 연결의 예약 dispatch ID를 단회 사용한다.

dispatch RPC 이름공간은 transport를 만들 때 producer가 확정한 profile binding으로 한 번 고정한다. A2
(``flowmarshal-same-user-v1``)는 ``fm/*``, binding 없음(보호 VM producer)은 ``vm/*``다. 호출마다 다시 고르지
않으며 오류 뒤 다른 이름공간으로 재시도하지 않는다. R16 승인 슬롯 control은 보호 VM 전용이라 ``vm/*``를 유지한다.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from .ags_observation_producer import _DIGEST, _DOMAIN, _VM_DISPATCH_DOMAIN, A2_PROFILE, A2_PROFILE_ID, _canonical


class DispatchUnavailable(ValueError):
    """인증 예약 문맥이 없거나 현재 연결과 맞지 않는다."""


@dataclass(frozen=True)
class DispatchBinding:
    """producer가 서명 body에 넣는 profile binding. transport는 생성 시 받은 값만 쓰고 바꾸지 않는다."""

    profile_id: str
    freeze_identity: str


@dataclass(frozen=True)
class DispatchTicket:
    call_id: str
    epoch: str
    token: str


def _body(signed: Any) -> dict[str, Any]:
    padded = signed["body"] + "=" * (-len(signed["body"]) % 4)
    body = json.loads(base64.urlsafe_b64decode(padded))
    if not isinstance(body, dict):
        raise ValueError("body is not an object")
    return body


class AuthenticatedDispatchTransport:
    def __init__(self, client: Any, binding: DispatchBinding | None = None) -> None:
        if binding is None:
            namespace, domains, profile = "vm", (_VM_DISPATCH_DOMAIN, _DOMAIN), None
        elif (type(binding) is DispatchBinding and binding.profile_id == A2_PROFILE_ID
              and isinstance(binding.freeze_identity, str) and _DIGEST.fullmatch(binding.freeze_identity)):
            namespace = "fm"
            domains = (A2_PROFILE["dispatchDomain"], A2_PROFILE["receiptDomain"])
            profile = {"profileId": binding.profile_id, "freezeIdentity": binding.freeze_identity}
        else:
            raise DispatchUnavailable("DISPATCH_PROFILE_UNSUPPORTED")
        self._client = client
        self._binding = binding
        self._namespace, (self._dispatch_domain, self._receipt_domain), self._profile = namespace, domains, profile
        self._epoch: str | None = None
        self._pending: dict[str, DispatchTicket] = {}

    @property
    def binding(self) -> DispatchBinding | None:
        return self._binding

    def _signed_for_profile(self, signed: Any, domain: str) -> dict[str, Any]:
        """서명 body의 domain과 profileBinding이 이 transport의 profile과 같을 때만 body를 돌려준다."""
        body = _body(signed)
        if body.get("domain") != domain or body.get("profileBinding") != self._profile:
            raise ValueError("profile binding mismatch")
        return body

    @property
    def epoch(self) -> str:
        if self._epoch is None:
            result = self._client._request(f"{self._namespace}/hello", {})
            epoch = result.get("serverEpoch") if isinstance(result, dict) else None
            if not isinstance(epoch, str) or len(epoch) < 32:
                raise DispatchUnavailable("VM_DISPATCH_EPOCH_INVALID")
            self._epoch = epoch
        return self._epoch

    def reserve(self, registration: dict[str, str]) -> DispatchTicket:
        epoch = self.epoch
        try:
            self._signed_for_profile(registration, self._dispatch_domain)
        except (KeyError, TypeError, ValueError, binascii.Error, UnicodeDecodeError) as error:
            raise DispatchUnavailable("DISPATCH_REGISTRATION_PROFILE_MISMATCH") from error
        result = self._client._request(f"{self._namespace}/reserve_dispatch", {"registration": registration})
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
            body = self._signed_for_profile(receipt, self._receipt_domain)
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
