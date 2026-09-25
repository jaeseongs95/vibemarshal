"""F06-c2: FM A2 transport와 실제 AGS subprocess의 wire 왕복 진단(층 B).

AGS 고정 SHA의 커밋된 번들(``mcp-server/dist/server.mjs``)을 ``git archive``로 임시 폴더에 풀어 실행한다. AGS 원
checkout·branch·worktree에는 쓰지 않으며, AGS 상태 경로는 모두 임시 폴더로 돌린다. FM 쪽은 실제
``_load_a2_producer``·``AuthenticatedDispatchTransport``·``McpStdioClient``를 쓰고, Core 원장은 producer 계약 검사와
같은 합성 sqlite 원장이다. 결과는 진단 범위이며 GovernanceTaskGate·EngineService admission·release 근거가 아니다.

실행 조건: node 24 이상, ``FLOWMARSHAL_AGS_REPO``(기본 ``D:/codex/거버전스 3.0/agent-governance-suite``)에 고정 SHA가
있어야 한다. ``FLOWMARSHAL_F06C2_EVIDENCE``를 주면 redacted 결과를 그 JSON 파일에 쓴다.
``FLOWMARSHAL_AGS_BUILT_ROOT``를 주면 archive 대신 그 폴더(pin을 worktree 밖에서 새로 build한 root)의 사본을 실행한다.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from flowmarshal.engine.ags_invocation_transport import AuthenticatedDispatchTransport, DispatchBinding
from flowmarshal.engine.ags_observation_producer import (
    A2_CONFIG_FORMAT, A2_PROFILE, A2_PROFILE_ID, _a2_digest, _canonical, _load_a2_producer,
)
from flowmarshal.engine.governance_conformance import _envelope
from flowmarshal.engine.governance_gate import (
    HOST_ID, GovernanceContractMismatch, GovernanceTimeout, GovernanceUnavailable, McpStdioClient, mcp_envelope,
)
from test_engine_ags_observation_producer import FIXTURE, _service

AGS_PIN = "0e88cb6039e143e79dd8c68c7aecc3e45da41d43"
AGS_REPO = Path(os.environ.get("FLOWMARSHAL_AGS_REPO", r"D:\codex\거버전스 3.0\agent-governance-suite"))
SERVER = Path("mcp-server") / "dist" / "server.mjs"
MODEL_CLASS = "deep"


def _unavailable() -> str | None:
    try:
        node = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30)
        major = int(node.stdout.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return "node를 실행할 수 없다"
    if major < 24:
        return f"node 24 이상이 필요하다({node.stdout.strip()})"
    probe = subprocess.run(["git", "-C", str(AGS_REPO), "cat-file", "-t", AGS_PIN], capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() != "commit":
        return f"AGS 고정 SHA {AGS_PIN}를 {AGS_REPO}에서 찾을 수 없다"
    return None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(signed: dict[str, str]) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(signed["body"] + "=" * (-len(signed["body"]) % 4)))


def _resign(key: Ed25519PrivateKey, key_id: str, body: dict[str, Any]) -> dict[str, str]:
    data = _canonical(body).encode("utf-8")
    return {"body": _b64(data), "signature": _b64(key.sign(data)), "keyId": key_id}


def _redact(value: Any) -> Any:
    """receipt·registration의 서명·body 원문은 digest로만 남긴다."""
    if isinstance(value, dict):
        return {key: (f"sha256:{hashlib.sha256(str(item).encode()).hexdigest()}"
                      if key in {"body", "signature", "privateKeyPkcs8", "_hostAttestation"} else _redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@unittest.skipIf(_unavailable(), _unavailable() or "")
class A2SubprocessRoundTripTests(unittest.TestCase):
    """한 AGS 서버 프로세스에서 정상 왕복과 음성 거절을 차례로 관측한다."""

    evidence: dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls._folder = tempfile.TemporaryDirectory(prefix="fm-f06c2-")
        base = Path(cls._folder.name).resolve()
        cls.root = base / "ags"
        built = os.environ.get("FLOWMARSHAL_AGS_BUILT_ROOT")
        if built:
            # pin을 worktree 밖에서 새로 build한 root를 쓴다. profile 파일을 쓰므로 사본에서 실행한다.
            shutil.copytree(built, cls.root, ignore=shutil.ignore_patterns("node_modules", ".git"))
            source = {"ags_source": "fresh_build", "ags_built_root": str(Path(built).resolve())}
        else:
            cls.root.mkdir()
            archive = subprocess.run(["git", "-C", str(AGS_REPO), "archive", "--format=tar", AGS_PIN],
                                     capture_output=True, check=True, timeout=300)
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                tar.extractall(cls.root, filter="data")
            source = {"ags_source": "committed_archive",
                      "ags_archive_sha256": hashlib.sha256(archive.stdout).hexdigest()}
        server = cls.root / SERVER
        cls.evidence = {
            "ags_pin": AGS_PIN, "ags_repo": str(AGS_REPO), **source,
            "server_bundle_sha256": hashlib.sha256(server.read_bytes()).hexdigest(),
            "node": subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip(),
            "checks": {},
        }

        # A2 profile: AGS는 번들 기준 ../../flowmarshal-same-user-v1에서 고정 파일 이름으로만 읽는다.
        profile_dir = cls.root / "flowmarshal-same-user-v1"
        profile_dir.mkdir()
        if os.name == "nt":
            # AGS same-user 검사: 소유자=현재 사용자, 허용은 현재 사용자·SYSTEM·Administrators만. %TEMP% 상속 ACL을 끊는다.
            self_sid = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                capture_output=True, text=True, check=True, timeout=30).stdout.strip()
            subprocess.run(["icacls", str(profile_dir), "/inheritance:r", "/grant:r",
                            f"*{self_sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                           capture_output=True, check=True, timeout=30)
        cls.key = Ed25519PrivateKey.generate()
        cls.key_id = "f06c2-key-1"
        public = cls.key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        resources = {kind: {"namespace": A2_PROFILE_ID, "location": str(profile_dir / name)}
                     for kind, name in (("key", "producer-key.json"), ("pin", "pins.json"),
                                        ("state", "state.sqlite3"))}
        pins = {"namespace": A2_PROFILE_ID,
                "pins": [{"keyId": cls.key_id, "publicKeySpki": base64.b64encode(public).decode("ascii"),
                          "status": "active"}]}
        selection = {"source": "server-local-operator-config", "profile": dict(A2_PROFILE),
                     "pinSetDigest": _a2_digest(pins), "resourceBindingDigest": _a2_digest(resources)}
        selection["freezeIdentity"] = _a2_digest(selection)
        cls.freeze = selection["freezeIdentity"]
        private = cls.key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
        (profile_dir / "producer-key.json").write_text(json.dumps({
            "version": 1, "namespace": A2_PROFILE_ID, "keyId": cls.key_id, "hostId": "flowmarshal",
            "privateKeyPkcs8": base64.b64encode(private).decode("ascii")}), encoding="utf-8")
        (profile_dir / "pins.json").write_text(json.dumps(pins), encoding="utf-8")
        (profile_dir / "server-profile.json").write_text(json.dumps(
            {"version": 1, "selection": selection, "resources": resources}), encoding="utf-8")
        fm_config = base / "fm-a2-profile.json"
        fm_config.write_text(json.dumps({"format": A2_CONFIG_FORMAT, "selection": selection,
                                         "resources": resources}), encoding="utf-8")
        cls.producer = _load_a2_producer(fm_config)
        cls.evidence.update(freeze_identity=cls.freeze, profile_id=A2_PROFILE_ID)

        state = base / "ags-state"
        state.mkdir()
        cls.env = {**os.environ, "AGENT_GOVERNANCE_HOST_ATTESTATION": HOST_ID,
                   "AGENT_GOVERNANCE_DB_PATH": str(state / "workflows.sqlite3"),
                   "AGENT_GOVERNANCE_CONTINUITY_DB_PATH": str(state / "continuity.sqlite3"),
                   "AGENT_GOVERNANCE_SHARED_STATE_DIR": str(state / "shared"),
                   "AGENT_GOVERNANCE_SESSION_MESSAGE_STATE_DIR": str(state / "messages"),
                   "AGENT_GOVERNANCE_TRUST_DB_PATH": str(state / "trust.sqlite3"),
                   "AGENT_GOVERNANCE_SESSION_BOARD_DB_PATH": str(state / "board.sqlite3"),
                   "LOCALAPPDATA": str(state / "localappdata"), "XDG_STATE_HOME": str(state / "xdg")}
        cls.base = base
        cls.client = cls._start()

    @classmethod
    def _start(cls) -> McpStdioClient:
        log = cls.base / "ags-server.stderr.log"
        try:
            return McpStdioClient(["node", str(cls.root / SERVER)], cwd=cls.root, env=cls.env,
                                  stderr_path=log, timeout=60)
        except (GovernanceTimeout, GovernanceUnavailable) as error:
            stderr = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.is_file() else ""
            raise AssertionError(f"AGS 서버 시작 실패: {error} / stderr: {stderr}") from error

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.client.close()
        except GovernanceUnavailable:
            pass
        target = os.environ.get("FLOWMARSHAL_F06C2_EVIDENCE")
        if target:
            Path(target).write_text(json.dumps(cls.evidence, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                    encoding="utf-8")
        shutil.rmtree(cls._folder.name, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _issue(self, transport: AuthenticatedDispatchTransport) -> tuple[dict[str, str], Any, dict[str, Any], Any]:
        """합성 Core 원장에서 bootstrap steward terminal을 만들고 producer가 등록·receipt를 서명한다."""
        example = copy.deepcopy(FIXTURE["validCases"][0])
        observed = datetime.now(timezone.utc) - timedelta(seconds=10)
        example["terminal"]["observedAt"] = observed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        envelope = _envelope()
        example["binding"]["taskId"] = envelope["taskId"]
        example["invocation"]["input"] = {"schemaVersion": "1.0.0", "taskEnvelope": envelope}
        service, connection, ref = _service(example)
        self.addCleanup(connection.close)
        arguments = example["invocation"]["input"]
        receipt, ticket = self.producer.issue(
            service=service, project_id="project-1", task_id="core-task-1", envelope_task_id=envelope["taskId"],
            run_id=None, attempt_id=None, stage="bootstrap", operation_id=example["core"]["gateOperationKey"],
            tool="plan_workflow", arguments=arguments, terminal_ref=ref, transport=transport,
            model_classes={example["terminal"]["model"]: MODEL_CLASS})
        return receipt, ticket, arguments, transport.registration_for_test

    def _rejected(self, action) -> str:
        """AGS가 거절하면 그 사유 문자열을 돌려준다. JSON-RPC error와 ok:false envelope 둘 다 거절이다."""
        try:
            raw = action()
        except GovernanceContractMismatch as error:
            return str(error)
        envelope = mcp_envelope(raw)
        self.assertIsInstance(envelope, dict, raw)
        self.assertIs(False, envelope.get("ok"), raw)
        return json.dumps(envelope.get("error"), ensure_ascii=False)

    def _transport(self, client: McpStdioClient | None = None) -> AuthenticatedDispatchTransport:
        return RecordingTransport(client or self.client, DispatchBinding(A2_PROFILE_ID, self.freeze))

    # ------------------------------------------------------------ checks
    def test_1_hello_reserve_receipt_and_single_claim_round_trip(self) -> None:
        transport = self._transport()
        receipt, ticket, arguments, registration = self._issue(transport)
        self.assertEqual(["fm/hello", "fm/reserve_dispatch"], transport.methods)
        self.assertTrue(ticket.call_id.startswith("fmr-"), ticket.call_id)
        raw = transport.call(ticket, receipt, "plan_workflow", arguments)
        envelope = mcp_envelope(raw)
        self.assertIsInstance(envelope, dict, raw)
        self.assertIs(True, envelope.get("ok"), raw)
        data = envelope["data"]
        replay = self._rejected(lambda: self.client.call_reserved(
            ticket.call_id, "plan_workflow", {**arguments, "_hostAttestation": receipt}))
        self.evidence["checks"]["round_trip"] = {
            "methods": transport.methods, "call_id_prefix": ticket.call_id[:4],
            "registration_domain": _decode(registration)["domain"], "receipt_domain": _decode(receipt)["domain"],
            "plan_ok": True, "execution_mode": data.get("executionMode"),
            "stages": [stage.get("requiredCapability") for stage in data.get("stages", [])],
            "replay_rejected": replay[:300]}

    def test_2_stale_epoch_after_server_restart_is_rejected(self) -> None:
        transport = self._transport()
        receipt, ticket, arguments, registration = self._issue(transport)
        restarted = self._start()
        self.addCleanup(restarted.close)
        stale_call = self._rejected(lambda: restarted.call_reserved(
            ticket.call_id, "plan_workflow", {**arguments, "_hostAttestation": receipt}))
        stale_reserve = self._rejected(lambda: restarted._request("fm/reserve_dispatch", {"registration": registration}))
        self.evidence["checks"]["stale_epoch"] = {"call": stale_call[:300], "reserve": stale_reserve[:300]}

    def test_3_call_id_reuse_is_rejected(self) -> None:
        transport = self._transport()
        receipt, ticket, arguments, registration = self._issue(transport)
        again = self._rejected(lambda: self.client._request("fm/reserve_dispatch", {"registration": registration}))
        transport.call(ticket, receipt, "plan_workflow", arguments)
        reused = self._rejected(lambda: self.client.call_reserved(
            ticket.call_id, "plan_workflow", {**arguments, "_hostAttestation": receipt}))
        self.evidence["checks"]["call_id_reuse"] = {"registration_nonce_reuse": again[:300],
                                                   "reserved_call_reuse": reused[:300]}

    def test_4_key_profile_binding_version_and_digest_tampering_is_rejected(self) -> None:
        results = {}
        other = Ed25519PrivateKey.generate()
        for label in ("unpinned_key", "profile_binding", "registration_version", "added_key"):
            # 정상 흐름의 등록 body를 받아 nonce를 바꾸고(재사용 거절과 구분) 변조·재서명한다.
            receipt, ticket, arguments, registration = self._issue(self._transport())
            body = _decode(registration)
            body["nonce"] = body["nonce"] + f"-{label}"
            key = self.key
            if label == "unpinned_key":
                key = other
            elif label == "profile_binding":
                body["profileBinding"] = {**body["profileBinding"], "freezeIdentity": "sha256:" + "0" * 64}
            elif label == "registration_version":
                body["version"] = 2
            else:
                body["unexpected"] = True
            results[label] = self._rejected(lambda: self.client._request(
                "fm/reserve_dispatch", {"registration": _resign(key, self.key_id, body)}))

        # receipt 변조: 정상 예약을 받은 뒤 version·profileBinding을 바꿔 재서명하거나 입력 digest를 어긋낸다.
        for label in ("receipt_version", "receipt_profile_binding", "input_digest"):
            transport = self._transport()
            receipt, ticket, arguments, registration = self._issue(transport)
            body = _decode(receipt)
            call_arguments = arguments
            if label == "receipt_version":
                body["version"] = 1
            elif label == "receipt_profile_binding":
                body["profileBinding"] = {**body["profileBinding"], "freezeIdentity": "sha256:" + "0" * 64}
            else:
                call_arguments = {**arguments, "taskEnvelope": {**arguments["taskEnvelope"], "objective": "변조"}}
            tampered = _resign(self.key, self.key_id, body) if label != "input_digest" else receipt
            results[label] = self._rejected(lambda: self.client.call_reserved(
                ticket.call_id, "plan_workflow", {**call_arguments, "_hostAttestation": tampered}))
            # 거절된 receipt는 예약을 태우지 않는다: 원래 receipt로는 아직 한 번 통과해야 한다.
            if label != "input_digest":
                raw = self.client.call_reserved(ticket.call_id, "plan_workflow",
                                                {**arguments, "_hostAttestation": receipt})
                self.assertIs(True, (mcp_envelope(raw) or {}).get("ok"), raw)
        self.evidence["checks"]["tampering"] = {label: reason[:300] for label, reason in results.items()}


class RecordingTransport(AuthenticatedDispatchTransport):
    """실제 transport에 보낸 RPC 이름과 마지막 등록을 기록만 한다(검사·전송 동작은 그대로)."""

    def __init__(self, client: McpStdioClient, binding: DispatchBinding) -> None:
        self.methods: list[str] = []
        self.registration_for_test: dict[str, str] | None = None

        class Recorder:
            def __init__(inner, wrapped: McpStdioClient) -> None:
                inner.wrapped = wrapped

            def _request(inner, method: str, params: dict, **kwargs: Any) -> Any:
                self.methods.append(method)
                if method.endswith("/reserve_dispatch"):
                    self.registration_for_test = params["registration"]
                return inner.wrapped._request(method, params, **kwargs)

            def call_reserved(inner, call_id: str, tool: str, arguments: dict) -> Any:
                return inner.wrapped.call_reserved(call_id, tool, arguments)

        super().__init__(Recorder(client), binding)


if __name__ == "__main__":
    unittest.main()
