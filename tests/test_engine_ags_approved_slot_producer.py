from __future__ import annotations

import base64
import json
import queue
import threading
import unittest
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.ags_approved_slot_producer import ApprovedSlotProducer
from flowmarshal.engine.ags_invocation_transport import ApprovedSlotControlTransport, DispatchUnavailable
from flowmarshal.engine.ags_observation_producer import AGSObservationProducer, ProducerUnavailable, _canonical
from flowmarshal.engine.domain import new_id
from flowmarshal.engine.governance_gate import GovernanceTaskGate, McpStdioClient
from flowmarshal.engine.service import EngineServiceError
from tests import test_engine_approved_stage_slots as slot_fixtures


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class FakeControlReceiver:
    def __init__(self, key: Ed25519PrivateKey, service, project_id: str, task_id: str) -> None:
        self.public = key.public_key()
        self.service, self.project_id, self.task_id = service, project_id, task_id
        self.epoch = "epoch-" + "a" * 40
        self.seen: set[str] = set()
        self.receipt = None
        self.invocation_id = None
        self.methods: list[str] = []

    def _request(self, method, params, *, request_id=None):
        self.methods.append(method)
        if method == "vm/hello":
            return {"serverEpoch": self.epoch}
        if method != "vm/register_approved_slot":
            raise DispatchUnavailable("control pipe rejects generic tools/call")
        receipt = params["signedSource"]
        self.receipt, self.invocation_id = receipt, request_id
        raw = decode(receipt["body"])
        try:
            self.public.verify(decode(receipt["signature"]), raw)
        except InvalidSignature as error:
            raise DispatchUnavailable("signature invalid") from error
        body = json.loads(raw)
        if raw != _canonical(body).encode("utf-8"):
            raise DispatchUnavailable("noncanonical body")
        if body["domain"] != "ags-vm-approved-slot-source-v1" or body["version"] != 1:
            raise DispatchUnavailable("wrong domain")
        producer = body["producer"]
        if (receipt["keyId"] != "test-key" or producer != {
                "installationId": "test-install", "keyId": "test-key", "hostId": "flowmarshal-engine",
                "instanceId": "test-instance", "sessionId": "test-session"}):
            raise DispatchUnavailable("pin or identity mismatch")
        binding, source = body["binding"], body["source"]
        if (binding["invocationId"] != request_id or binding["serverEpoch"] != self.epoch
                or binding["projectId"] != self.project_id or binding["taskId"] != self.task_id
                or binding["snapshotDigest"] != source["snapshot_digest"]
                or source != json.loads(json.dumps(self.service.read_current_approved_role_slot_source(
                    self.project_id, self.task_id), ensure_ascii=False))):
            raise DispatchUnavailable("current source binding mismatch")
        issued = datetime.fromisoformat(body["issuedAt"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(body["expiresAt"].replace("Z", "+00:00"))
        if (expires - issued != timedelta(seconds=60) or not issued <= datetime.now(timezone.utc) < expires
                or body["nonce"] in self.seen):
            raise DispatchUnavailable("expired or replayed")
        self.seen.add(body["nonce"])
        return {"accepted": True, "invocationId": request_id, "serverEpoch": self.epoch,
                "snapshotDigest": source["snapshot_digest"]}


class ApprovedSlotProducerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = slot_fixtures.ApprovedStageSlotTests("test_authorize_read_reauthorize_cancel_and_restart")
        self.fixture.setUp()
        self.fixture.authorize()
        self.service = self.fixture.service
        self.project_id, self.task_id = self.fixture.project_id, self.fixture.task.task_id
        self.key = Ed25519PrivateKey.generate()
        self.pin_active = True
        self.signer = AGSObservationProducer(
            private_key=self.key, installation_id="test-install", key_id="test-key",
            instance_id="test-instance", session_id="test-session", pin_check=self._check_pin,
        )
        self.receiver = FakeControlReceiver(self.key, self.service, self.project_id, self.task_id)
        self.transport = ApprovedSlotControlTransport(self.receiver)
        self.producer = ApprovedSlotProducer(self.signer)

    def tearDown(self):
        self.fixture.tearDown()

    def _check_pin(self):
        if not self.pin_active:
            raise ProducerUnavailable("PIN_CHANGED")

    def register(self, expected=None):
        return self.producer.register(service=self.service, project_id=self.project_id,
                                      task_id=self.task_id, expected=expected, transport=self.transport)

    def test_current_core_snapshot_is_signed_for_control_invocation(self):
        result = self.register()
        self.assertTrue(result["accepted"])
        self.assertEqual(["vm/hello", "vm/register_approved_slot"], self.receiver.methods)
        body = json.loads(decode(self.receiver.receipt["body"]))
        source = body["source"]
        self.assertEqual(self.receiver.invocation_id, body["binding"]["invocationId"])
        self.assertEqual(source["plan_revision_id"], self.fixture.plan.plan_revision_id)
        self.assertEqual(source["activation_id"], self.fixture.app.read_current_approved_role_slot_source(
            self.project_id, self.task_id)["activation_id"])
        self.assertEqual(source["authorization_id"], self.fixture.app.read_current_approved_role_slot_source(
            self.project_id, self.task_id)["authorization_id"])
        self.assertEqual(source["participation"]["watermark"], source["source_revision"])

    def test_stale_authorization_replacement_and_revoke_fail_closed(self):
        first = self.service.read_current_approved_role_slot_source(self.project_id, self.task_id)
        self.fixture.authorize()
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_STALE"):
            self.register({"authorization_id": first["authorization_id"]})
        self.assertNotIn("vm/register_approved_slot", self.receiver.methods)
        self.service.revoke_approved_role_slots(self.project_id, reason="합성 철회")
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_UNAVAILABLE"):
            self.register()
        self.assertNotIn("vm/register_approved_slot", self.receiver.methods)

    def test_replay_epoch_change_and_generic_call_rejected(self):
        self.register()
        receipt, invocation_id = self.receiver.receipt, self.receiver.invocation_id
        with self.assertRaisesRegex(DispatchUnavailable, "replayed"):
            self.receiver._request("vm/register_approved_slot", {"signedSource": receipt}, request_id=invocation_id)
        self.receiver.epoch = "epoch-" + "b" * 40
        with self.assertRaisesRegex(DispatchUnavailable, "binding mismatch"):
            self.receiver._request("vm/register_approved_slot", {"signedSource": receipt}, request_id=invocation_id)
        with self.assertRaisesRegex(DispatchUnavailable, "generic"):
            self.receiver._request("tools/call", {"name": "register_approved_slot", "arguments": receipt})

    def test_pipe_restart_between_hello_and_registration_fails_closed(self):
        original = self.receiver._request

        def restart(method, params, *, request_id=None):
            result = original(method, params, request_id=request_id)
            if method == "vm/hello":
                self.receiver.epoch = "epoch-" + "c" * 40
            return result

        self.receiver._request = restart
        with self.assertRaisesRegex(DispatchUnavailable, "binding mismatch"):
            self.register()

    def test_pin_change_after_hello_blocks_registration(self):
        original = self.receiver._request

        def change_pin(method, params, *, request_id=None):
            result = original(method, params, request_id=request_id)
            if method == "vm/hello":
                self.pin_active = False
            return result

        self.receiver._request = change_pin
        with self.assertRaisesRegex(ProducerUnavailable, "PIN_CHANGED"):
            self.register()
        self.assertNotIn("vm/register_approved_slot", self.receiver.methods)

    def test_pin_drift_blocks_before_send_and_bad_signature_is_rejected(self):
        self.pin_active = False
        with self.assertRaisesRegex(ProducerUnavailable, "PIN_CHANGED"):
            self.register()
        self.assertEqual([], self.receiver.methods)
        self.pin_active = True
        self.receiver.public = Ed25519PrivateKey.generate().public_key()
        with self.assertRaisesRegex(DispatchUnavailable, "signature invalid"):
            self.register()

    def test_participation_watermark_change(self):
        before = self.service.read_current_approved_role_slot_source(self.project_id, self.task_id)
        with self.fixture.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO role_slot_participation "
                "(id,project_id,plan_revision_id,task_id,actor_id,host,session_id,receipt_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id("participation"), self.project_id, self.fixture.plan.plan_revision_id,
                 self.task_id, "actor_one", "host_one", "session_one",
                 sha256_digest("receipt"), tx.now),
            )
            tx.history(self.project_id, "role_slot.participated", "task", self.task_id,
                       {"actor_id": "actor_one"})
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_STALE"):
            self.register({"source_revision": before["source_revision"]})
        self.register()
        current = json.loads(decode(self.receiver.receipt["body"]))["source"]
        self.assertGreater(current["source_revision"], before["source_revision"])
        self.assertEqual(current["participation"]["watermark"], current["source_revision"])

    def test_plan_replacement_invalidates_old_task(self):
        # R16-b fixture가 Plan 교체와 구 Task invalidation을 실제 writer로 수행한다.
        self.fixture.test_participation_schema_read_and_replacement()
        calls = self.receiver.methods.count("vm/register_approved_slot")
        with self.assertRaises(EngineServiceError):
            self.register()
        self.assertEqual(calls, self.receiver.methods.count("vm/register_approved_slot"))

    def test_actual_json_rpc_request_id_is_signed(self):
        client = object.__new__(McpStdioClient)
        client.deadline, client.timeout, client.sequence = None, 1.0, 0
        client._lines = queue.Queue()
        client._request_lock = threading.RLock()

        def send(message):
            result = self.receiver._request(message["method"], message["params"],
                                            request_id=message.get("id"))
            client._lines.put(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                          "result": result}).encode("utf-8"))

        client._send = send
        result = self.producer.register(
            service=self.service, project_id=self.project_id, task_id=self.task_id,
            transport=ApprovedSlotControlTransport(client),
        )
        self.assertEqual(result["invocationId"], self.receiver.invocation_id)
        self.assertTrue(self.receiver.invocation_id.startswith("vm-approved-slot-"))

    def test_gate_uses_dedicated_transport(self):
        class Plugin:
            def preflight(inner):
                return {}

            def approved_slot_control(inner):
                return self.transport

        gate = GovernanceTaskGate(self.service, plugin=Plugin(), steward=object(),
                                  state_dir=self.fixture.base, producer=self.signer)
        self.assertTrue(gate.register_approved_role_slot_source(self.project_id, self.task_id)["accepted"])


if __name__ == "__main__":
    unittest.main()
