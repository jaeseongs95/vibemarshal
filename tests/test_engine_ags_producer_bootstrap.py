"""운영 key·AGS pin bootstrap의 보호 경계와 fail-closed 검사."""
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

from flowmarshal.engine.ags_invocation_transport import DispatchTicket
from flowmarshal.engine.ags_observation_producer import (
    ProducerUnavailable, _load_installed_producer, _read_protected_json, _windows_protected,
)
from flowmarshal.engine.governance_gate import GovernanceSettings
from test_engine_ags_observation_producer import FIXTURE, _service


class InstalledProducerBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.key_path = self.root / "operator-key" / "ags-producer-key.json"
        self.pin_path = self.root / "operator-pin" / "vm-operator-policy.json"
        self.key_path.parent.mkdir()
        self.pin_path.parent.mkdir()
        self.private = Ed25519PrivateKey.generate()
        example = FIXTURE["validCases"][0]
        self.installation_id = example["producer"]["installationId"]
        self.key_id = example["producer"]["keyId"]
        self.key = self._key(self.private, self.key_id)
        self.policy = self._policy(self.private, self.key_id)
        self._write()

    def _key(self, private: Ed25519PrivateKey, key_id: str) -> dict:
        encoded = private.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
        return {"version": 1, "installationId": self.installation_id, "keyId": key_id,
                "hostId": "flowmarshal-engine", "modelPolicyVersion": "policy-v1",
                "privateKeyPkcs8": base64.b64encode(encoded).decode("ascii")}

    def _policy(self, private: Ed25519PrivateKey, key_id: str) -> dict:
        public = private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        digest = "sha256:" + "a" * 64
        return {"version": 1, "modelPolicyVersion": "policy-v1", "pins": [{
            "keyId": key_id, "installationId": self.installation_id, "hostId": "flowmarshal-engine",
            "publicKeySpki": base64.b64encode(public).decode("ascii"),
            "hostBuildDigest": digest, "modelPolicyVersion": "policy-v1", "status": "active",
        }], "hostBuilds": [{"hostId": "flowmarshal-engine", "hostBuildDigest": digest,
                            "status": "verified"}], "models": []}

    def _write(self) -> None:
        self.key_path.write_text(json.dumps(self.key), encoding="utf-8")
        self.pin_path.write_text(json.dumps(self.policy), encoding="utf-8")

    def _bootstrap(self):
        return _load_installed_producer(self.key_path, self.pin_path,
                                        protection=lambda _path, _status, _secret, _file: True)

    def test_protected_installation_signs_call_registration(self) -> None:
        producer = self._bootstrap()
        self.assertEqual(self.installation_id, producer.installation_id)
        self.assertEqual(self.key_id, producer.key_id)
        producer.check_installed_pin()
        example = FIXTURE["validCases"][0]
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)

        class Transport:
            epoch = "e" * 40
            registration = None

            def reserve(inner, registration):
                inner.registration = registration
                return DispatchTicket("vmr-bootstrap", inner.epoch, "ticket")

        transport = Transport()
        receipt, ticket = producer.issue(
            service=service, project_id="project-1", task_id="core-task-1",
            envelope_task_id=example["binding"]["taskId"], run_id=example["binding"]["runId"],
            attempt_id=example["binding"]["attemptId"], stage=example["core"]["stage"],
            operation_id=example["core"]["gateOperationKey"], tool=example["invocation"]["tool"],
            arguments=example["invocation"]["input"], terminal_ref=ref, transport=transport,
        )
        self.assertEqual("vmr-bootstrap", ticket.call_id)
        for signed in (transport.registration, receipt):
            raw = base64.urlsafe_b64decode(signed["body"] + "==")
            self.private.public_key().verify(base64.urlsafe_b64decode(signed["signature"] + "=="), raw)
            body = json.loads(raw)
            self.assertEqual(self.installation_id, body["producer"]["installationId"])
            self.assertEqual(self.key_id, body["producer"]["keyId"])
        self.assertEqual("vmr-bootstrap", json.loads(base64.urlsafe_b64decode(receipt["body"] + "=="))
                         ["binding"]["invocationId"])

    def test_missing_permissions_symlink_and_swap_fail_closed(self) -> None:
        self.key_path.unlink()
        with self.assertRaisesRegex(ProducerUnavailable, "PROTECTED_PATH_UNAVAILABLE"):
            self._bootstrap()
        self._write()
        self.pin_path.unlink()
        with self.assertRaisesRegex(ProducerUnavailable, "PROTECTED_PATH_UNAVAILABLE"):
            self._bootstrap()
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "PROTECTED_PERMISSIONS_INVALID"):
            _load_installed_producer(self.key_path, self.pin_path,
                                     protection=lambda path, *_: path != self.key_path.parent)
        for denied in (self.key_path, self.pin_path):
            with self.assertRaisesRegex(ProducerUnavailable, "PROTECTED_PERMISSIONS_INVALID"):
                _load_installed_producer(self.key_path, self.pin_path,
                                         protection=lambda path, *_: path != denied)
        with self.assertRaisesRegex(ProducerUnavailable, "PROTECTED_PATH_CHANGED"):
            _read_protected_json(self.key_path, secret=True, protection=lambda *_: True,
                                 after_validation=lambda: self._replace_key())

    def test_symlink_or_windows_reparse_point_is_rejected(self) -> None:
        if os.name == "nt":
            original = Path.lstat

            def reparse(path):
                status = original(path)
                if path == self.key_path:
                    return SimpleNamespace(st_mode=status.st_mode, st_file_attributes=0x400)
                return status

            with patch.object(Path, "lstat", autospec=True, side_effect=reparse):
                with self.assertRaisesRegex(ProducerUnavailable, "PROTECTED_PATH_INVALID"):
                    self._bootstrap()
        else:
            link = self.root / "linked-key.json"
            link.symlink_to(self.key_path)
            with self.assertRaisesRegex(ProducerUnavailable, "PROTECTED_PATH_INVALID"):
                _load_installed_producer(link, self.pin_path, protection=lambda *_: True)

    def _replace_key(self) -> None:
        self.key_path.rename(self.root / "approved-backup.json")
        self.key_path.write_text(json.dumps({"attacker": True}), encoding="utf-8")

    def test_revocation_and_rotation_require_matching_pin(self) -> None:
        producer = self._bootstrap()
        self.policy["pins"][0]["status"] = "revoked"
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "PIN_MISMATCH"):
            producer.check_installed_pin()
        self.policy["pins"][0]["status"] = "active"
        next_private = Ed25519PrivateKey.generate()
        self.key = self._key(next_private, "next-key")
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "PIN_UNAVAILABLE"):
            producer.check_installed_pin()
        self.policy = self._policy(next_private, "next-key")
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "INSTALLATION_CHANGED"):
            producer.check_installed_pin()
        rotated = self._bootstrap()
        self.assertEqual(producer.installation_id, rotated.installation_id)
        self.assertNotEqual(producer.instance_id, rotated.instance_id)
        self.assertEqual("next-key", rotated.key_id)

    def test_invalid_public_pin_policy_version_and_private_key_fail_closed(self) -> None:
        self.policy["pins"][0]["publicKeySpki"] = base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes(
                Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode("ascii")
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "KEY_PIN_MISMATCH"):
            self._bootstrap()
        self.policy = self._policy(self.private, self.key_id)
        self.policy["pins"][0]["modelPolicyVersion"] = "old-policy"
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "PIN_MISMATCH"):
            self._bootstrap()
        self.policy = self._policy(self.private, self.key_id)
        self.key["privateKeyPkcs8"] = "not-base64"
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "KEY_INVALID"):
            self._bootstrap()
        self.key = self._key(self.private, self.key_id)
        self.key["version"] = True
        self._write()
        with self.assertRaisesRegex(ProducerUnavailable, "INSTALLATION_POLICY_MISMATCH"):
            self._bootstrap()

    def test_windows_acl_rejects_readable_secret_and_writable_ancestors(self) -> None:
        def check(owner, rights, *, secret, is_file):
            acl = {"owner": owner, "rules": [
                {"sid": "S-1-5-32-544", "rights": 2032127, "type": "Allow"},
                {"sid": "S-1-5-32-545", "rights": rights, "type": "Allow"},
            ]}
            with patch("flowmarshal.engine.ags_observation_producer.subprocess.run",
                       return_value=SimpleNamespace(stdout=json.dumps(acl))):
                return _windows_protected(self.key_path, secret=secret, is_file=is_file)

        self.assertTrue(check("S-1-5-18", 0x1200A9, secret=False, is_file=True))
        self.assertFalse(check("S-1-5-18", 0x1200A9, secret=True, is_file=True))
        self.assertFalse(check("S-1-5-18", 197055, secret=True, is_file=False))
        self.assertFalse(check("S-1-5-21-1", 0, secret=True, is_file=True))

    def test_product_settings_never_load_the_protected_vm_producer(self) -> None:
        # 제품 설정은 명시 A2 profile만 읽는다(F06). 보호 VM producer는 설치돼 있어도 자동으로 고르지 않는다.
        environment = {"FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT": str(self.root),
                       "FLOWMARSHAL_VM_PRODUCER_KEY_PATH": str(self.key_path)}
        with patch.dict(os.environ, environment), patch.dict(os.environ, {}, clear=False) as env, patch(
            "flowmarshal.engine.ags_observation_producer._load_installed_producer", return_value=self._bootstrap(),
        ) as loader:
            env.pop("FLOWMARSHAL_GOVERNANCE_A2_PROFILE", None)
            settings = GovernanceSettings.from_environment(self.root / "state")
        loader.assert_not_called()
        self.assertIsNone(settings.observation_producer)


if __name__ == "__main__":
    unittest.main()
