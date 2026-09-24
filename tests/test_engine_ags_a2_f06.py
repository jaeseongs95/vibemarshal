"""F06: 명시 A2 same-user profile producer loader와 필수 gate 전환의 fail-closed 검사.

AGS 쪽 A2 verifier·서버 선택(F02~F04)은 없으므로 여기서는 FM이 서명하는 bytes와 거부 경로만 확인한다. 실제
check-plugin PASS나 live dispatch 증거가 아니다.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from flowmarshal.engine import ags_observation_producer as producer_module
from flowmarshal.engine.ags_invocation_transport import DispatchTicket
from flowmarshal.engine.ags_observation_producer import (
    A2_CONFIG_FORMAT, A2_PROFILE, A2_PROFILE_ID, AGSObservationProducer, ProducerUnavailable, _a2_digest,
    _load_a2_producer,
)
from flowmarshal.engine.governance_gate import (
    A2_PROFILE_ENV, CONSUMED_SURFACE, GATE_KIND, CoreOperations, GovernanceContractMismatch, GovernanceSettings,
    GovernanceTaskGate, MissingGovernanceGate, _Run,
)
from flowmarshal.engine import governance_gate
from test_engine_ags_observation_producer import FIXTURE, _service

ROOT = Path(__file__).resolve().parents[1]
VM_PROFILE = {
    "profileId": "vm-protected-v1", "assuranceTier": "strong", "hostId": "flowmarshal-engine",
    "receiptDomain": "vm-provider-terminal-to-governance", "dispatchDomain": "ags-vm-dispatch-registration-v1",
    "modelClassSource": "operator-exact-observed-model", "actorSource": "operator-pinned-installation",
    "keyNamespace": "vm-protected-v1", "pinNamespace": "vm-protected-v1", "stateNamespace": "vm-protected-v1",
}


def _decode(signed: dict[str, str]) -> bytes:
    return base64.urlsafe_b64decode(signed["body"] + "==")


class A2Profile:
    """임시 폴더의 A2 설정·key·pin. mutate 뒤 write(recompute=True)는 digest를 다시 계산해 다른 결함만 남긴다."""

    def __init__(self, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.private = Ed25519PrivateKey.generate()
        self.key_path, self.pin_path, self.config_path = base / "a2-key.json", base / "a2-pins.json", base / "a2.json"
        self.key = self.key_for(self.private)
        self.pins = {"namespace": A2_PROFILE_ID, "pins": [self.pin_for(self.private)]}
        self.resources = {"key": {"namespace": A2_PROFILE_ID, "location": str(self.key_path)},
                          "pin": {"namespace": A2_PROFILE_ID, "location": str(self.pin_path)},
                          "state": {"namespace": A2_PROFILE_ID, "location": str(base / "ags-state")}}
        self.selection = {"source": "server-local-operator-config", "profile": dict(A2_PROFILE)}
        self.extra: dict = {}
        self.write()

    @staticmethod
    def key_for(private: Ed25519PrivateKey, key_id: str = "a2-key-1") -> dict:
        encoded = private.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
        return {"version": 1, "namespace": A2_PROFILE_ID, "keyId": key_id, "hostId": "flowmarshal",
                "privateKeyPkcs8": base64.b64encode(encoded).decode("ascii")}

    @staticmethod
    def pin_for(private: Ed25519PrivateKey, key_id: str = "a2-key-1", status: str = "active") -> dict:
        public = private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        return {"keyId": key_id, "publicKeySpki": base64.b64encode(public).decode("ascii"), "status": status}

    def write(self, *, recompute: bool = True) -> None:
        if recompute:
            self.selection["pinSetDigest"] = _a2_digest(self.pins)
            self.selection["resourceBindingDigest"] = _a2_digest(self.resources)
            self.selection.pop("freezeIdentity", None)
            self.selection["freezeIdentity"] = _a2_digest(self.selection)
        self.key_path.write_text(json.dumps(self.key), encoding="utf-8")
        self.pin_path.write_text(json.dumps(self.pins), encoding="utf-8")
        self.config_path.write_text(json.dumps({"format": A2_CONFIG_FORMAT, "selection": self.selection,
                                                "resources": self.resources, **self.extra}), encoding="utf-8")

    def load(self) -> AGSObservationProducer:
        return _load_a2_producer(self.config_path)


class _Transport:
    epoch = "e" * 43

    def __init__(self) -> None:
        self.registration = None

    def reserve(self, registration):
        self.registration = registration
        return DispatchTicket("fm-call-1", self.epoch, "ticket")


def _issue(producer, example, service, ref, transport, model_classes):
    return producer.issue(
        service=service, project_id="project-1", task_id="core-task-1",
        envelope_task_id=example["binding"]["taskId"], run_id=example["binding"]["runId"],
        attempt_id=example["binding"]["attemptId"], stage=example["core"]["stage"],
        operation_id=example["core"]["gateOperationKey"], tool=example["invocation"]["tool"],
        arguments=example["invocation"]["input"], terminal_ref=ref, transport=transport, model_classes=model_classes)


class A2ProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.profile = A2Profile(self.base)

    def test_a2_signs_its_own_domains_with_profile_binding_and_fm_assertions(self) -> None:
        producer = self.profile.load()
        self.assertEqual(A2_PROFILE_ID, producer.profile_id)
        freeze = self.profile.selection["freezeIdentity"]
        self.assertEqual(freeze, producer.freeze_identity)
        for example in FIXTURE["validCases"]:
            with self.subTest(kind=example["kind"]):
                service, connection, ref = _service(example)
                self.addCleanup(connection.close)
                model = example["terminal"]["model"]
                transport = _Transport()
                receipt, ticket = _issue(producer, example, service, ref, transport, {model: "deep"})
                actor = (f"flowmarshal-engine:steward:{example['core']['stage']}:{example['terminal']['threadId']}"
                         if example["kind"] == "steward"
                         else f"flowmarshal-engine:worker:{example['binding']['attemptId']}")
                for signed, domain in ((transport.registration, A2_PROFILE["dispatchDomain"]),
                                       (receipt, A2_PROFILE["receiptDomain"])):
                    raw = _decode(signed)
                    self.profile.private.public_key().verify(base64.urlsafe_b64decode(signed["signature"] + "=="), raw)
                    body = json.loads(raw)
                    self.assertEqual(domain, body["domain"])
                    self.assertEqual({"profileId": A2_PROFILE_ID, "freezeIdentity": freeze}, body["profileBinding"])
                    self.assertEqual({"modelClass": "deep", "actorId": actor}, body["assertions"])
                    self.assertEqual("flowmarshal", body["producer"]["hostId"])
                    self.assertEqual("flowmarshal", body["binding"]["hostId"])
                    self.assertEqual("a2-key-1", body["producer"]["keyId"])
                    self.assertNotIn(b"vm-provider-terminal-to-governance", raw)
                    self.assertNotIn(b"ags-vm-dispatch-registration-v1", raw)
                self.assertEqual("fm-call-1", json.loads(_decode(receipt))["binding"]["invocationId"])

    def test_undeclared_model_class_or_tampered_observation_signs_nothing(self) -> None:
        producer = self.profile.load()
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        transport = _Transport()
        with self.assertRaisesRegex(ProducerUnavailable, "A2_PRODUCER_MODEL_CLASS_UNDECLARED"):
            _issue(producer, example, service, ref, transport, {})
        self.assertIsNone(transport.registration)
        usage = json.loads(connection.execute("SELECT payload_json FROM budget_usage").fetchone()[0])
        usage["observed_model"] = "another-model"
        connection.execute("UPDATE budget_usage SET payload_json=?", (json.dumps(usage),))
        connection.commit()
        with self.assertRaisesRegex(ProducerUnavailable, "STEWARD_EVENT_MISMATCH"):
            _issue(producer, example, service, ref, transport, {example["terminal"]["model"]: "deep"})
        self.assertIsNone(transport.registration)

    def test_profile_change_after_load_stops_signing(self) -> None:
        producer = self.profile.load()
        other = Ed25519PrivateKey.generate()
        self.profile.key = self.profile.key_for(other)
        self.profile.pins["pins"] = [self.profile.pin_for(other)]
        self.profile.write()
        with self.assertRaisesRegex(ProducerUnavailable, "A2_PRODUCER_PROFILE_CHANGED"):
            producer.check_installed_pin()
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        transport = _Transport()
        with self.assertRaisesRegex(ProducerUnavailable, "A2_PRODUCER_PROFILE_CHANGED"):
            _issue(producer, example, service, ref, transport, {example["terminal"]["model"]: "deep"})
        self.assertIsNone(transport.registration)

    def test_loader_rejects_missing_mixed_or_stale_profile_material(self) -> None:
        vm_key = {"version": 1, "installationId": "i", "keyId": "a2-key-1", "hostId": "flowmarshal-engine",
                  "modelPolicyVersion": "p", "privateKeyPkcs8": self.profile.key["privateKeyPkcs8"]}
        cases = {
            "A2_PRODUCER_PROFILE_MISMATCH:vm profile": lambda p: p.selection.update(profile=dict(VM_PROFILE)),
            "A2_PRODUCER_PROFILE_MISMATCH:tier": lambda p: p.selection["profile"].update(assuranceTier="strong"),
            "A2_PRODUCER_PROFILE_MISMATCH:vm domain": lambda p: p.selection["profile"].update(
                receiptDomain=VM_PROFILE["receiptDomain"]),
            "A2_PRODUCER_PROFILE_MISMATCH:source": lambda p: p.selection.update(source="request-field"),
            "A2_PRODUCER_PROFILE_MISMATCH:extra": lambda p: p.extra.update(installed=True),
            "A2_PRODUCER_NAMESPACE_MISMATCH:key": lambda p: p.resources["key"].update(namespace="vm-protected-v1"),
            "A2_PRODUCER_NAMESPACE_MISMATCH:state": lambda p: p.resources["state"].update(namespace="vm-protected-v1"),
            "A2_PRODUCER_KEY_MISMATCH:vm key": lambda p: setattr(p, "key", vm_key),
            "A2_PRODUCER_KEY_MISMATCH:namespace": lambda p: p.key.update(namespace="vm-protected-v1"),
            "A2_PRODUCER_KEY_MISMATCH:host": lambda p: p.key.update(hostId="flowmarshal-engine"),
            "A2_PRODUCER_PIN_MISMATCH:namespace": lambda p: p.pins.update(namespace="vm-protected-v1"),
            "A2_PRODUCER_PIN_UNAVAILABLE:revoked": lambda p: p.pins.update(
                pins=[p.pin_for(p.private, status="revoked")]),
            "A2_PRODUCER_PIN_UNAVAILABLE:other key id": lambda p: p.pins.update(
                pins=[p.pin_for(p.private, key_id="a2-key-2")]),
            "A2_PRODUCER_KEY_PIN_MISMATCH:other public key": lambda p: p.pins.update(
                pins=[p.pin_for(Ed25519PrivateKey.generate())]),
            "A2_PRODUCER_KEY_PATH_INVALID:vm key path": lambda p: p.resources["key"].update(
                location=str(producer_module._KEY_PATH)),
            "A2_PRODUCER_PIN_PATH_INVALID:relative": lambda p: p.resources["pin"].update(location="a2-pins.json"),
            "A2_PRODUCER_KEY_UNAVAILABLE:missing": lambda p: p.resources["key"].update(
                location=str(p.base / "missing.json")),
        }
        for label, mutate in cases.items():
            code = label.split(":")[0]
            with self.subTest(case=label):
                profile = A2Profile(self.base / label.replace(":", "_").replace(" ", "_"))
                mutate(profile)
                profile.write()
                with self.assertRaisesRegex(ProducerUnavailable, code):
                    profile.load()
        # digest를 다시 계산하지 않은 변경은 결속 불일치로 멈춘다.
        stale = {
            "A2_PRODUCER_RESOURCE_BINDING_MISMATCH": lambda p: p.resources["state"].update(
                location=str(p.base / "other-state")),
            "A2_PRODUCER_PIN_SET_DIGEST_MISMATCH": lambda p: p.pins["pins"].append(
                p.pin_for(Ed25519PrivateKey.generate(), key_id="a2-key-2")),
            "A2_PRODUCER_FREEZE_IDENTITY_MISMATCH": lambda p: p.selection.update(freezeIdentity="sha256:" + "0" * 64),
        }
        for code, mutate in stale.items():
            with self.subTest(case=code):
                profile = A2Profile(self.base / code)
                mutate(profile)
                profile.write(recompute=False)
                with self.assertRaisesRegex(ProducerUnavailable, code):
                    profile.load()
        profile = A2Profile(self.base / "duplicate")
        profile.config_path.write_text('{"format": "x", "format": "y"}', encoding="utf-8")
        with self.assertRaisesRegex(ProducerUnavailable, "A2_PRODUCER_CONFIG_JSON_INVALID"):
            profile.load()
        with self.assertRaisesRegex(ProducerUnavailable, "A2_PRODUCER_PROFILE_MISMATCH"):
            AGSObservationProducer(private_key=Ed25519PrivateKey.generate(), installation_id="i", key_id="k",
                                   instance_id="n", session_id="s", profile=VM_PROFILE,
                                   freeze_identity="sha256:" + "0" * 64)


class A2GateTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.profile = A2Profile(self.base)

    def settings(self, profile: str | None) -> GovernanceSettings:
        environment = {"FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT": str(self.base / "plugin")}
        with patch.dict(os.environ, environment), patch(
                "flowmarshal.engine.ags_observation_producer._load_installed_producer",
                side_effect=AssertionError("보호 VM producer를 자동으로 읽으면 안 된다")):
            os.environ.pop(A2_PROFILE_ENV, None)
            if profile is not None:
                os.environ[A2_PROFILE_ENV] = profile
            return GovernanceSettings.from_environment(self.base / "state")

    def test_cli_is_not_a_consumed_entry_point_and_profile_ids_agree(self) -> None:
        self.assertNotIn("host-attestation-cli", CONSUMED_SURFACE["entry_points"])
        self.assertEqual(A2_PROFILE_ID, governance_gate.A2_PROFILE_ID)

    def test_product_settings_load_only_the_explicit_a2_profile(self) -> None:
        settings = self.settings(str(self.profile.config_path))
        self.assertEqual(A2_PROFILE_ID, settings.observation_producer.profile_id)
        gate = settings.open_gate(SimpleNamespace(), runtime=object(), roles=object(), runner=object())
        self.assertIsInstance(gate, GovernanceTaskGate)
        gate.close()
        self.profile.key["hostId"] = "flowmarshal-engine"
        self.profile.write()
        with self.assertRaisesRegex(ProducerUnavailable, "A2_PRODUCER_KEY_MISMATCH"):
            self.settings(str(self.profile.config_path))

    def test_missing_or_non_a2_producer_blocks_dispatch_without_fallback(self) -> None:
        missing = self.settings(None)
        self.assertIsNone(missing.observation_producer)
        vm = GovernanceSettings(self.base / "plugin", self.base / "state", observation_producer=AGSObservationProducer(
            private_key=Ed25519PrivateKey.generate(), installation_id="i", key_id="k", instance_id="n",
            session_id="s"))
        for settings in (missing, vm):
            gate = settings.open_gate(SimpleNamespace(), runtime=object(), roles=object(), runner=object())
            self.assertIsInstance(gate, MissingGovernanceGate)
            for decision in (gate.before_execution({}), gate.before_completion({})):
                self.assertTrue(decision.startswith("GOVERNANCE_A2_PROFILE_REQUIRED:"), decision)

    def test_approved_slot_registration_refuses_missing_or_a2_producer(self) -> None:
        class Plugin:
            def preflight(inner):
                raise AssertionError("producer 확인 전에 플러그인을 부르면 안 된다")

            def close(inner):
                return None

        for producer, observed in ((None, "unavailable"), (self.profile.load(), A2_PROFILE_ID)):
            with self.subTest(observed=observed):
                gate = GovernanceTaskGate(SimpleNamespace(), plugin=Plugin(), steward=object(),
                                          state_dir=self.base, operations=object(), producer=producer)
                with self.assertRaises(GovernanceContractMismatch) as raised:
                    gate.register_approved_role_slot_source("project-1", "task-1")
                self.assertIn("approved_slot_producer", str(raised.exception))
                self.assertIn(observed, str(raised.exception))

    def test_gate_sends_observed_calls_only_through_the_a2_producer(self) -> None:
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        connection.execute("DELETE FROM history_events WHERE event_type='operation.prepared'")
        test = self

        class Operations:
            def invoke(inner, *, project_id, kind, request, execute):
                operation_id = CoreOperations._operation_id(project_id, kind, request)[1]
                connection.execute("INSERT INTO history_events VALUES (?,?,?,?,?,?)", (
                    project_id, "core_operation", operation_id, "operation.prepared",
                    json.dumps({"request": request}), 1))
                connection.commit()
                return execute()

        class Plugin:
            receipt = None

            def authenticated_dispatch(inner):
                return _Transport()

            def model_classes(inner):
                return {example["terminal"]["model"]: "general"}

            def call(inner, tool, arguments, observation):
                test.fail("관측 호출이 producer 밖으로 나가면 안 된다")

            def call_authenticated(inner, ticket, receipt, tool, arguments):
                inner.receipt = receipt
                return {"content": [{"type": "text", "text": json.dumps({
                    "ok": True, "data": {"executionMode": "orchestrated", "stages": []}, "error": None})}]}

        plugin = Plugin()
        gate = GovernanceTaskGate(service, plugin=plugin, steward=object(), state_dir=self.base,
                                  operations=Operations(), producer=self.profile.load())
        self.assertEqual(GATE_KIND, "governance_gate")
        run = _Run(key={"task_id": "core-task-1", "execution_spec_revision_id": "spec-1", "attempt_no": 1},
                   root=self.base, folder=self.base, envelope={"taskId": example["binding"]["taskId"]}, c0={})
        result = gate._mcp({"id": "core-task-1", "project_id": "project-1"}, run, "plan",
                           example["invocation"]["tool"], example["invocation"]["input"],
                           {"terminalRef": ref, "stage": "bootstrap"}, replay=False)
        self.assertEqual("orchestrated", result["executionMode"])
        body = json.loads(_decode(plugin.receipt))
        self.assertEqual(A2_PROFILE["receiptDomain"], body["domain"])
        self.assertEqual("general", body["assertions"]["modelClass"])

        # 대응표에 없는 terminal model은 Task 거절이 아니라 서명 전 계약 불일치다(AGENTS.md 필수 연동 절).
        plugin.model_classes = lambda: {}
        plugin.receipt = None
        with self.assertRaises(GovernanceContractMismatch) as raised:
            gate._mcp({"id": "core-task-1", "project_id": "project-1"}, run, "plan",
                      example["invocation"]["tool"], {**example["invocation"]["input"], "retry": 1},
                      {"terminalRef": ref, "stage": "bootstrap"}, replay=False)
        self.assertIn("GOVERNANCE_CONTRACT_MISMATCH: model_class:", str(raised.exception))
        self.assertIsNone(plugin.receipt)


class A2ConformanceTransitionTests(unittest.TestCase):
    def test_signed_probe_checks_fail_closed_until_the_a2_probe_exists(self) -> None:
        from flowmarshal.engine.governance_conformance import run_conformance
        from flowmarshal.engine.governance_gate import GovernancePlugin
        from test_engine_governance_conformance import ConformanceFake

        class RealSigningPath(ConformanceFake):
            def call(self, tool, arguments, observation):
                if observation is not None:
                    return GovernancePlugin.call(self, tool, arguments, observation)
                return super().call(tool, arguments, observation)

        result = run_conformance(Path("unused-plugin-root"), plugin_factory=RealSigningPath)
        self.assertEqual("FAIL", result["verdict"])
        checks = {item["id"]: item for item in result["checks"]}
        self.assertEqual("PASS", checks["attestation_required"]["status"])
        self.assertEqual("FAIL", checks["plan"]["status"])
        self.assertIn("a2_profile", checks["plan"]["observed"])


class A2DocumentTests(unittest.TestCase):
    def test_contract_documents_state_same_user_limits_and_r16_follow_up(self) -> None:
        for relative in ("AGENTS.md", "README.md", "docs/redesign-1.0-contract.md"):
            with self.subTest(document=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(A2_PROFILE_ID, text)
                self.assertIn(A2_PROFILE_ENV, text)
                for phrase in ("같은 OS 사용자", "원장", "transcript", "key", "R16"):
                    self.assertIn(phrase, text)
                self.assertNotIn("호스트 중립 서명 CLI(`host-attestation-cli`)가 들어간 플러그인", text)
        contract = (ROOT / "docs/redesign-1.0-contract.md").read_text(encoding="utf-8")
        self.assertIn("최종 보고", contract)


if __name__ == "__main__":
    unittest.main()
