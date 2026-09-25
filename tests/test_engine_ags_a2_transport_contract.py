"""F06-c: A2 authenticated dispatch transport의 이름공간·profile 결속 계약 검사(층 A, fake 서버).

실제 AGS 0e88cb6 서버는 A2 profile을 선택하면 ``fm/hello``·``fm/reserve_dispatch``만 등록하고 ``vm/*``는
보호 VM profile에서만 등록한다(server.ts 466-488). 여기의 fake 서버는 그 이름공간 규칙만 흉내 낸다. 이 결과는
실제 AGS subprocess 왕복(F06-c2)이나 FM service admission(F06-c3)의 근거가 아니다.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flowmarshal.engine.ags_invocation_transport import (
    AuthenticatedDispatchTransport, DispatchBinding, DispatchUnavailable,
)
from flowmarshal.engine.ags_observation_producer import A2_PROFILE, A2_PROFILE_ID, AGSObservationProducer, _canonical
from flowmarshal.engine.governance_gate import (
    CONSUMED_SURFACE, CONSUMED_SURFACE_DIGEST, GovernanceContractMismatch, GovernancePlugin, GovernanceSettings,
    GovernanceTaskGate, _Run,
)

FREEZE = "sha256:" + "1" * 64
OTHER_FREEZE = "sha256:" + "2" * 64
EPOCH = "e" * 43
A2 = DispatchBinding(A2_PROFILE_ID, FREEZE)
# fed976f(F06-c 착수 기준)에서 계산한 값이다. dispatch RPC 선언은 F07-a 몫이라 이 leaf는 digest를 바꾸지 않는다.
FED976F_CONSUMED_SURFACE_DIGEST = "sha256:b2deec19b239615ddce89248ddefe38b1bd859b6c0016261551575a5d748265b"


def _encode(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode("utf-8")).rstrip(b"=").decode("ascii")


def _registration(domain: str, profile_binding: dict | None) -> dict[str, str]:
    body = {"version": 1, "domain": domain}
    if profile_binding is not None:
        body["profileBinding"] = profile_binding
    return {"body": _encode(body), "signature": "fixture-signature", "keyId": "fixture-key"}


def _receipt(call_id: str, epoch: str, tool: str, arguments: dict, *, domain: str,
             profile_binding: dict | None) -> dict[str, str]:
    body = {"version": 2, "domain": domain, "binding": {"invocationId": call_id},
            "transport": {"serverEpoch": epoch},
            "invocation": {"tool": tool, "inputDigest": "sha256:" + hashlib.sha256(
                _canonical(arguments).encode("utf-8")).hexdigest()}}
    if profile_binding is not None:
        body["profileBinding"] = profile_binding
    return {"body": _encode(body), "signature": "fixture-signature", "keyId": "fixture-key"}


A2_REGISTRATION = _registration(A2_PROFILE["dispatchDomain"], {"profileId": A2_PROFILE_ID, "freezeIdentity": FREEZE})
VM_REGISTRATION = _registration("ags-vm-dispatch-registration-v1", None)


class FakeServer:
    """namespace 하나의 dispatch RPC만 등록한 fake AGS 서버. 다른 이름공간은 JSON-RPC method not found다."""

    def __init__(self, namespace: str, *, epochs: list[str] | None = None, call_ids: list[str] | None = None) -> None:
        self.namespace, self.methods, self.calls = namespace, [], []
        self.epochs = list(epochs or [EPOCH, EPOCH])
        self.call_ids = list(call_ids or ["call-1", "call-2", "call-3"])

    def _request(self, method: str, params: dict, *, request_id: str | None = None) -> dict:
        self.methods.append(method)
        if not method.startswith(f"{self.namespace}/"):
            # McpStdioClient가 JSON-RPC error를 바꾸는 방식과 같다.
            raise GovernanceContractMismatch(f"mcp_protocol:{method}", "result",
                                             {"code": -32601, "message": f"Method not found: {method}"})
        if method.endswith("/hello"):
            return {"serverEpoch": self.epochs[0]}
        if method.endswith("/reserve_dispatch"):
            return {"callId": self.call_ids.pop(0), "serverEpoch": self.epochs.pop(1) if len(self.epochs) > 1
                    else self.epochs[0]}
        raise AssertionError(method)

    def call_reserved(self, call_id: str, tool: str, arguments: dict) -> dict:
        self.calls.append((call_id, tool, arguments))
        return {"content": []}


def _a2_producer(freeze: str = FREEZE) -> AGSObservationProducer:
    return AGSObservationProducer(private_key=Ed25519PrivateKey.generate(), installation_id="a2", key_id="key-1",
                                  instance_id="instance", session_id="session", profile=A2_PROFILE,
                                  freeze_identity=freeze)


class ProductWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)

    def _plugin(self, gate) -> GovernancePlugin:
        self.addCleanup(gate.close)
        return gate.plugin

    def test_product_gate_for_a2_producer_opens_fm_namespace(self) -> None:
        # 반증 기준: fed976f의 transport는 A2에서도 vm/hello를 보내 A2 서버에서 method not found로 실패했다.
        settings = GovernanceSettings(self.base / "plugin", self.base / "state", observation_producer=_a2_producer())
        plugin = self._plugin(settings.open_gate(SimpleNamespace(), runtime=object(), roles=object(), runner=object()))
        server = FakeServer("fm")
        with patch.object(plugin, "_client", return_value=server):
            transport = plugin.authenticated_dispatch()
            self.assertEqual(EPOCH, transport.epoch)
            ticket = transport.reserve(A2_REGISTRATION)
        self.assertEqual(["fm/hello", "fm/reserve_dispatch"], server.methods)
        self.assertEqual("call-1", ticket.call_id)
        self.assertEqual(A2, transport.binding)

    def test_plugin_without_binding_keeps_vm_namespace(self) -> None:
        plugin = GovernancePlugin(self.base / "plugin", self.base / "state")
        self.addCleanup(plugin.close)
        server = FakeServer("vm")
        with patch.object(plugin, "_client", return_value=server):
            transport = plugin.authenticated_dispatch()
            transport.reserve(VM_REGISTRATION)
        self.assertEqual(["vm/hello", "vm/reserve_dispatch"], server.methods)
        self.assertIsNone(transport.binding)


class NamespaceTests(unittest.TestCase):
    def test_namespace_is_fixed_at_construction_and_never_falls_back(self) -> None:
        for binding, server_namespace, sent in ((A2, "vm", "fm/hello"), (None, "fm", "vm/hello")):
            with self.subTest(binding=binding):
                server = FakeServer(server_namespace)
                transport = AuthenticatedDispatchTransport(server, binding)
                with self.assertRaises(GovernanceContractMismatch):
                    transport.epoch
                with self.assertRaises(GovernanceContractMismatch):
                    transport.epoch
                # 오류 뒤 다른 이름공간으로 다시 묻지 않는다.
                self.assertEqual([sent, sent], server.methods)

    def test_unknown_or_malformed_profile_binding_is_rejected_at_construction(self) -> None:
        for binding in (DispatchBinding("vm-protected-v1", FREEZE), DispatchBinding("strong", FREEZE),
                        DispatchBinding(A2_PROFILE_ID, "sha256:bad"), DispatchBinding(A2_PROFILE_ID, None),
                        (A2_PROFILE_ID, FREEZE), "fm"):
            with self.subTest(binding=binding):
                server = FakeServer("fm")
                with self.assertRaisesRegex(DispatchUnavailable, "PROFILE"):
                    AuthenticatedDispatchTransport(server, binding)
                self.assertEqual([], server.methods)


class BindingTests(unittest.TestCase):
    def test_caller_cannot_change_the_producer_binding(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            A2.freeze_identity = OTHER_FREEZE
        transport = AuthenticatedDispatchTransport(FakeServer("fm"), A2)
        with self.assertRaises(AttributeError):
            transport.binding = DispatchBinding(A2_PROFILE_ID, OTHER_FREEZE)
        with tempfile.TemporaryDirectory() as folder:
            plugin = GovernancePlugin(Path(folder) / "plugin", Path(folder) / "state", dispatch_binding=A2)
            with self.assertRaises(AttributeError):
                plugin.dispatch_binding = None
            with self.assertRaises(TypeError):
                plugin.authenticated_dispatch(None)
            plugin.close()

    def test_gate_refuses_a_plugin_bound_to_another_profile_before_signing(self) -> None:
        producer = _a2_producer()
        for plugin_binding in (None, DispatchBinding(A2_PROFILE_ID, OTHER_FREEZE)):
            with self.subTest(plugin_binding=plugin_binding), tempfile.TemporaryDirectory() as folder:
                plugin = GovernancePlugin(Path(folder) / "plugin", Path(folder) / "state",
                                          dispatch_binding=plugin_binding)
                self.addCleanup(plugin.close)
                gate = GovernanceTaskGate(SimpleNamespace(ledger=None), plugin=plugin, steward=object(),
                                          state_dir=Path(folder), operations=object(), producer=producer)
                run = _Run(key={"task_id": "t", "execution_spec_revision_id": "s", "attempt_no": 1},
                           root=Path(folder), folder=Path(folder), envelope={"taskId": "fm-t-a1"}, c0={})
                with patch.object(producer, "issue", side_effect=AssertionError("서명하면 안 된다")), \
                        self.assertRaisesRegex(GovernanceContractMismatch, "dispatch_profile"):
                    gate._mcp({"id": "t", "project_id": "p"}, run, "plan", "plan_workflow", {},
                              {"terminalRef": {}, "stage": "bootstrap"}, replay=False)


class CrossProfileTests(unittest.TestCase):
    def test_cross_profile_registration_is_rejected_before_reservation(self) -> None:
        cases = {
            "vm registration on A2": (A2, VM_REGISTRATION),
            "other freeze": (A2, _registration(A2_PROFILE["dispatchDomain"],
                                               {"profileId": A2_PROFILE_ID, "freezeIdentity": OTHER_FREEZE})),
            "A2 domain without binding": (A2, _registration(A2_PROFILE["dispatchDomain"], None)),
            "A2 registration on vm": (None, A2_REGISTRATION),
            "undecodable": (A2, {"body": "!!", "signature": "s", "keyId": "k"}),
        }
        for label, (binding, registration) in cases.items():
            with self.subTest(case=label):
                server = FakeServer("fm" if binding else "vm")
                transport = AuthenticatedDispatchTransport(server, binding)
                with self.assertRaisesRegex(DispatchUnavailable, "REGISTRATION"):
                    transport.reserve(registration)
                self.assertNotIn(f"{'fm' if binding else 'vm'}/reserve_dispatch", server.methods)

    def test_cross_profile_receipt_is_rejected_before_the_call(self) -> None:
        arguments = {"schemaVersion": "1.0.0"}
        a2_binding = {"profileId": A2_PROFILE_ID, "freezeIdentity": FREEZE}
        cases = {
            "vm domain": dict(domain="vm-provider-terminal-to-governance", profile_binding=None),
            "other freeze": dict(domain=A2_PROFILE["receiptDomain"],
                                 profile_binding={"profileId": A2_PROFILE_ID, "freezeIdentity": OTHER_FREEZE}),
            "missing binding": dict(domain=A2_PROFILE["receiptDomain"], profile_binding=None),
            "dispatch domain": dict(domain=A2_PROFILE["dispatchDomain"], profile_binding=a2_binding),
        }
        for label, receipt in cases.items():
            with self.subTest(case=label):
                server = FakeServer("fm")
                transport = AuthenticatedDispatchTransport(server, A2)
                ticket = transport.reserve(A2_REGISTRATION)
                with self.assertRaisesRegex(DispatchUnavailable, "RECEIPT"):
                    transport.call(ticket, _receipt(ticket.call_id, EPOCH, "plan_workflow", arguments, **receipt),
                                   "plan_workflow", arguments)
                self.assertEqual([], server.calls)
        server = FakeServer("fm")
        transport = AuthenticatedDispatchTransport(server, A2)
        ticket = transport.reserve(A2_REGISTRATION)
        transport.call(ticket, _receipt(ticket.call_id, EPOCH, "plan_workflow", arguments,
                                        domain=A2_PROFILE["receiptDomain"], profile_binding=a2_binding),
                       "plan_workflow", arguments)
        self.assertEqual([("call-1", "plan_workflow")], [call[:2] for call in server.calls])
        self.assertIn("_hostAttestation", server.calls[0][2])


class EpochAndReuseTests(unittest.TestCase):
    def test_stale_epoch_and_call_id_reuse_are_rejected(self) -> None:
        stale = AuthenticatedDispatchTransport(FakeServer("fm", epochs=[EPOCH, "f" * 43]), A2)
        with self.assertRaisesRegex(DispatchUnavailable, "RESERVATION"):
            stale.reserve(A2_REGISTRATION)
        repeated = AuthenticatedDispatchTransport(FakeServer("fm", epochs=[EPOCH] * 3, call_ids=["same", "same"]), A2)
        repeated.reserve(A2_REGISTRATION)
        with self.assertRaisesRegex(DispatchUnavailable, "RESERVATION"):
            repeated.reserve(A2_REGISTRATION)

        arguments = {"schemaVersion": "1.0.0"}
        server = FakeServer("fm", epochs=[EPOCH] * 3)
        transport = AuthenticatedDispatchTransport(server, A2)
        ticket = transport.reserve(A2_REGISTRATION)
        receipt = _receipt(ticket.call_id, EPOCH, "plan_workflow", arguments, domain=A2_PROFILE["receiptDomain"],
                           profile_binding={"profileId": A2_PROFILE_ID, "freezeIdentity": FREEZE})
        transport.call(ticket, receipt, "plan_workflow", arguments)
        with self.assertRaisesRegex(DispatchUnavailable, "TICKET"):
            transport.call(ticket, receipt, "plan_workflow", arguments)
        other = AuthenticatedDispatchTransport(FakeServer("fm", epochs=["g" * 43] * 2), A2)
        with self.assertRaisesRegex(DispatchUnavailable, "TICKET"):
            other.call(ticket, receipt, "plan_workflow", arguments)
        self.assertEqual(1, len(server.calls))


class ConsumedSurfaceTests(unittest.TestCase):
    def test_consumed_surface_digest_is_unchanged_and_dispatch_rpc_is_a_known_gap(self) -> None:
        self.assertEqual(FED976F_CONSUMED_SURFACE_DIGEST, CONSUMED_SURFACE_DIGEST)
        # known gap(F07-a): authenticated dispatch RPC(vm/*·fm/*)는 소비 표면에 선언돼 있지 않다.
        declared = json.dumps(CONSUMED_SURFACE)
        for method in ("vm/hello", "vm/reserve_dispatch", "fm/hello", "fm/reserve_dispatch"):
            self.assertNotIn(method, declared)


if __name__ == "__main__":
    unittest.main()
