from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_codex import MethodNotFoundError

from flowmarshal.engine.runtime import (
    CodexAppServerRuntime,
    CodexProjectBinding,
    ExecutionPolicyEvidence,
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    RuntimePolicyError,
)


PROJECT_ID = "01a074f8-b78e-7b21-9390-99e251da49bb"


def _policy(cwd: Path) -> ExecutionPolicyEvidence:
    return ExecutionPolicyEvidence(
        permission_profile=REQUIRED_PERMISSION_PROFILE,
        approval_policy=REQUIRED_APPROVAL_POLICY,
        config_digest="sha256:" + "1" * 64,
        profile_catalog_digest="sha256:" + "2" * 64,
        cwd=str(cwd),
    )


class EngineRuntimeProjectBindingTests(unittest.TestCase):
    def _runtime(self, root: Path) -> CodexAppServerRuntime:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        runtime._project_binding = CodexProjectBinding(
            project_id=PROJECT_ID,
            expected_root=str(root),
            expected_name="자동화테스트",
        )
        runtime._turn_futures = {}
        runtime._ephemeral_thread_ids = set()
        runtime._first_empty_threads = set()
        runtime._new_thread_project_proofs = {}
        runtime.executable_digest = "sha256:" + "e" * 64
        runtime._codex_bin = root / "codex.exe"
        return runtime

    @staticmethod
    def _project_response(root: Path, **updates: object) -> dict[str, object]:
        project: dict[str, object] = {
            "id": PROJECT_ID,
            "name": "자동화테스트",
            "roots": [{"path": str(root)}],
        }
        project.update(updates)
        return {"project": project}

    def _create_bound_empty_thread(
        self,
        runtime: CodexAppServerRuntime,
        *,
        project_root: Path,
        cwd: Path,
        thread_id: str = "new-empty-thread",
    ) -> None:
        runtime.verify_execution_policy = lambda value: _policy(Path(value))

        def raw(method: str, _params: dict[str, object]) -> dict[str, object]:
            if method == "project/read":
                return self._project_response(project_root)
            return {
                "activePermissionProfile": {"id": REQUIRED_PERMISSION_PROFILE},
                "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                "cwd": str(cwd),
                "model": "inventory-selected-model",
                "thread": {
                    "id": thread_id,
                    "ephemeral": False,
                    "turns": [],
                    "projectId": PROJECT_ID,
                    "path": str(cwd / "rollout.jsonl"),
                },
            }

        runtime._raw = raw
        runtime.create_thread(
            cwd=cwd,
            title="ignored",
            model="inventory-selected-model",
            developer_instructions="instructions",
        )

    @staticmethod
    def _empty_metadata(cwd: Path, **updates: object) -> dict[str, object]:
        thread: dict[str, object] = {
            "id": "new-empty-thread",
            "projectId": None,
            "ephemeral": False,
            "turns": [],
            "cwd": str(cwd),
            "path": str(cwd / "rollout.jsonl"),
            "status": {"type": "notLoaded"},
        }
        thread.update(updates)
        return {"thread": thread}

    @staticmethod
    def _install_peer(runtime: CodexAppServerRuntime, raw):
        class Peer:
            executable_digest = runtime.executable_digest

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            _raw = staticmethod(raw)

        peer = Peer()
        runtime._open_project_read_peer = lambda: peer
        return peer

    def test_project_preflight_requires_exact_server_identity_root_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = self._runtime(root)
            calls: list[tuple[str, dict[str, object]]] = []
            runtime._raw = lambda method, params: (
                calls.append((method, params)) or self._project_response(root)
            )

            runtime._verify_project_binding()

        self.assertEqual(
            [("project/read", {"projectId": PROJECT_ID})],
            calls,
        )

    def test_project_preflight_accepts_other_registered_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = self._runtime(root)
            runtime._raw = lambda _method, _params: self._project_response(
                root,
                roots=[{"path": str(root)}, {"path": str(root.parent / "other-project")}],
            )

            runtime._verify_project_binding()

    def test_project_preflight_rejects_identity_name_missing_or_duplicate_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cases = (
                self._project_response(root, id="a9b87a2d-7530-42a9-841c-4f23c99e8c22"),
                self._project_response(
                    root,
                    roots=[{"path": str(root / "unexpected")}],
                ),
                self._project_response(root, roots=[{"path": str(root)}, {"path": str(root)}]),
                self._project_response(
                    root,
                    roots=[{"path": str(root)}, {"path": "relative-project-root"}],
                ),
                self._project_response(root, name="다른 프로젝트"),
            )
            for response in cases:
                with self.subTest(response=response):
                    runtime = self._runtime(root)
                    runtime._raw = lambda _method, _params, value=response: value
                    with self.assertRaisesRegex(RuntimePolicyError, "PROJECT_BINDING_MISMATCH"):
                        runtime._verify_project_binding()

    def test_project_preflight_rejects_relative_expected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = self._runtime(root)
            runtime._project_binding = runtime.project_binding.model_copy(
                update={"expected_root": "relative-project-root"}
            )
            runtime._raw = lambda _method, _params: self._project_response(root)

            with self.assertRaisesRegex(RuntimePolicyError, "PROJECT_BINDING_MISMATCH"):
                runtime._verify_project_binding()

    def test_bound_thread_start_sends_project_id_and_preserves_fixture_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "runs" / "fixture"
            fixture.mkdir(parents=True)
            runtime = self._runtime(root)
            runtime.verify_execution_policy = lambda cwd: _policy(Path(cwd))
            calls: list[tuple[str, dict[str, object]]] = []

            def raw(method: str, params: dict[str, object]) -> dict[str, object]:
                calls.append((method, params))
                if method == "project/read":
                    return self._project_response(root)
                return {
                    "activePermissionProfile": {"id": REQUIRED_PERMISSION_PROFILE},
                    "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                    "cwd": str(fixture),
                    "model": "inventory-selected-model",
                    "thread": {
                        "id": "thread-bound",
                        "ephemeral": False,
                        "turns": [],
                        "projectId": PROJECT_ID,
                        "path": str(fixture / "rollout.jsonl"),
                    },
                }

            runtime._raw = raw
            receipt = runtime.create_thread(
                cwd=fixture,
                title="ignored",
                model="inventory-selected-model",
                developer_instructions="instructions",
            )

        self.assertEqual("thread-bound", receipt.operation_id)
        self.assertEqual(["project/read", "thread/start"], [call[0] for call in calls])
        start = calls[1][1]
        self.assertEqual(PROJECT_ID, start["projectId"])
        self.assertEqual(str(fixture), start["cwd"])

    def test_bound_thread_start_rejects_missing_canonical_project_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = self._runtime(root)
            runtime.verify_execution_policy = lambda cwd: _policy(Path(cwd))

            def raw(method: str, _params: dict[str, object]) -> dict[str, object]:
                if method == "project/read":
                    return self._project_response(root)
                return {
                    "activePermissionProfile": {"id": REQUIRED_PERMISSION_PROFILE},
                    "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                    "cwd": str(root),
                    "model": "inventory-selected-model",
                    "thread": {"id": "old-unassigned-thread", "turns": []},
                }

            runtime._raw = raw
            with self.assertRaisesRegex(RuntimePolicyError, "PROJECT_BINDING_MISMATCH"):
                runtime.create_thread(
                    cwd=root,
                    title="ignored",
                    model="inventory-selected-model",
                    developer_instructions="instructions",
                )

    def test_bound_thread_start_requires_absolute_rollout_path_for_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for rollout_path in (None, "relative-rollout.jsonl"):
                with self.subTest(rollout_path=rollout_path):
                    runtime = self._runtime(root)
                    runtime.verify_execution_policy = lambda cwd: _policy(Path(cwd))

                    def raw(method: str, _params: dict[str, object]):
                        if method == "project/read":
                            return self._project_response(root)
                        thread = {
                            "id": "invalid-rollout-thread",
                            "ephemeral": False,
                            "turns": [],
                            "projectId": PROJECT_ID,
                        }
                        if rollout_path is not None:
                            thread["path"] = rollout_path
                        return {
                            "activePermissionProfile": {
                                "id": REQUIRED_PERMISSION_PROFILE
                            },
                            "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                            "cwd": str(root),
                            "model": "inventory-selected-model",
                            "thread": thread,
                        }

                    runtime._raw = raw
                    with self.assertRaisesRegex(
                        RuntimePolicyError, "절대 rollout 경로"
                    ):
                        runtime.create_thread(
                            cwd=root,
                            title="ignored",
                            model="inventory-selected-model",
                            developer_instructions="instructions",
                        )
                    self.assertFalse(runtime._new_thread_project_proofs)

    def test_bound_thread_start_rejects_ephemeral_before_provider_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = self._runtime(root)
            runtime.verify_execution_policy = lambda cwd: _policy(Path(cwd))
            calls: list[str] = []

            def raw(method: str, _params: dict[str, object]) -> dict[str, object]:
                calls.append(method)
                if method == "project/read":
                    return self._project_response(root)
                self.fail("ephemeral 프로젝트 thread를 생성하면 안 됩니다.")

            runtime._raw = raw
            with self.assertRaisesRegex(RuntimePolicyError, "저장형"):
                runtime.create_thread(
                    cwd=root,
                    title="ignored",
                    model="inventory-selected-model",
                    developer_instructions="instructions",
                    ephemeral=True,
                )

        self.assertEqual(["project/read"], calls)

    def test_owner_materialization_error_then_peer_empty_list_preserves_proof(self) -> None:
        class OwnerClient:
            def __init__(self) -> None:
                self.turn_calls = 0

            def thread_read(self, *_args, **_kwargs):
                raise MethodNotFoundError(-32601, "list_turns is not supported yet")

            def turn_start(self, _thread_id, _input_items, params=None):
                del params
                self.turn_calls += 1
                return SimpleNamespace(turn=SimpleNamespace(id="first-turn"))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            runtime = self._runtime(root)
            self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
            proof = runtime._new_thread_project_proofs["new-empty-thread"]
            Path(proof.rollout_path).write_text("session_meta only\n", encoding="utf-8")
            owner_calls: list[tuple[str, dict[str, object]]] = []
            runtime._raw = lambda method, params: (
                owner_calls.append((method, params)) or self._empty_metadata(fixture)
            )
            peer_calls: list[tuple[str, dict[str, object]]] = []

            def peer_raw(method: str, params: dict[str, object]):
                peer_calls.append((method, params))
                if method == "thread/turns/list":
                    return {"data": [], "nextCursor": None, "backwardsCursor": None}
                return self._empty_metadata(fixture, status={"type": "idle"})

            peer = self._install_peer(runtime, peer_raw)
            owner = OwnerClient()
            runtime._codex = SimpleNamespace(_client=owner)

            observation = runtime.read_stored(thread_id="new-empty-thread")

            self.assertEqual(0, observation.payload["turn_count"])
            self.assertTrue(observation.payload["turn_history_available"])
            self.assertEqual("thread/turns/list", observation.payload["turn_history_source"])
            self.assertEqual("thread/turns/list", observation.payload["usage_source"])
            self.assertEqual("independent_app_server", observation.payload["turn_history_connection"])
            self.assertEqual(
                runtime.executable_digest,
                observation.payload["independent_reader_executable_digest"],
            )
            self.assertEqual({
                "method": "thread/read",
                "params": {"threadId": "new-empty-thread", "includeTurns": True},
                "response": None,
                "error": {
                    "type": "MethodNotFoundError", "code": -32601,
                    "message": "list_turns is not supported yet",
                },
            }, observation.payload["materialization_read"])
            self.assertEqual([], observation.payload["read_retry_errors"])
            verification = observation.payload["project_binding_verification"]
            self.assertEqual(
                "same_connection_thread_start_receipt_and_empty_turns_list",
                verification["source"],
            )
            self.assertEqual("independent_app_server", verification["raw_metadata_connection"])
            self.assertEqual(2, verification["raw_metadata_confirmation_count"])
            self.assertNotIn("confirmation_count", verification)
            self.assertEqual(PROJECT_ID, verification["creation_project_id"])
            self.assertEqual(proof.receipt_digest, verification["creation_receipt_digest"])
            self.assertEqual(["thread/read"], [item[0] for item in owner_calls])
            self.assertEqual(
                ["thread/read", "thread/turns/list", "thread/read"],
                [item[0] for item in peer_calls],
            )
            self.assertTrue(peer.closed)
            self.assertIn("new-empty-thread", runtime._new_thread_project_proofs)

            runtime._raw = lambda *_args: self.fail("read가 first-turn proof를 소비했습니다.")
            receipt = runtime.start_turn(
                thread_id="new-empty-thread", cwd=fixture, prompt="prompt",
                model="inventory-selected-model", effort="high",
            )
            self.assertEqual("first-turn", receipt.operation_id)
            self.assertEqual(1, owner.turn_calls)
            self.assertNotIn("new-empty-thread", runtime._new_thread_project_proofs)

    def test_owner_successful_empty_materialization_receipt_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            runtime = self._runtime(root)
            self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
            runtime._raw = lambda _method, _params: self._empty_metadata(fixture)
            typed_document = {"thread": {"id": "new-empty-thread", "turns": []}}
            typed = SimpleNamespace(
                thread=SimpleNamespace(id="new-empty-thread", turns=()),
                model_dump=lambda **_kwargs: typed_document,
            )
            runtime._codex = SimpleNamespace(
                _client=SimpleNamespace(thread_read=lambda *_args, **_kwargs: typed)
            )
            self._install_peer(
                runtime,
                lambda method, _params: (
                    {"data": [], "nextCursor": None, "backwardsCursor": None}
                    if method == "thread/turns/list" else self._empty_metadata(fixture)
                ),
            )

            observation = runtime.read_stored(thread_id="new-empty-thread")

            self.assertEqual(typed_document, observation.payload["materialization_read"]["response"])
            self.assertIsNone(observation.payload["materialization_read"]["error"])

    def test_owner_materialization_rejects_wrong_thread_turns_or_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            owner_values = (
                SimpleNamespace(
                    thread=SimpleNamespace(id="foreign-thread", turns=()),
                    model_dump=lambda **_kwargs: {},
                ),
                SimpleNamespace(
                    thread=SimpleNamespace(
                        id="new-empty-thread", turns=(SimpleNamespace(id="turn-1"),)
                    ),
                    model_dump=lambda **_kwargs: {},
                ),
                MethodNotFoundError(-32601, "different message"),
                MethodNotFoundError(-32600, "list_turns is not supported yet"),
                RuntimeError("list_turns is not supported yet"),
            )
            for value in owner_values:
                with self.subTest(value=repr(value)):
                    runtime = self._runtime(root)
                    self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
                    runtime._raw = lambda _method, _params: self._empty_metadata(fixture)
                    if isinstance(value, BaseException):
                        reader = lambda *_args, item=value, **_kwargs: (
                            _ for _ in ()
                        ).throw(item)
                    else:
                        reader = lambda *_args, item=value, **_kwargs: item
                    runtime._codex = SimpleNamespace(
                        _client=SimpleNamespace(thread_read=reader)
                    )
                    runtime._open_project_read_peer = lambda: self.fail(
                        "owner materialization 실패 뒤 peer를 열면 안 됩니다."
                    )
                    expected_error = (
                        type(value) if isinstance(value, BaseException)
                        else RuntimePolicyError
                    )
                    with self.assertRaises(expected_error):
                        runtime.read_stored(thread_id="new-empty-thread")

    def test_empty_read_requires_exact_creation_proof_and_owner_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            cases = (
                ("missing-proof", {}),
                ("changed-cwd", {"cwd": str(root / "other")}),
                ("raw-turn", {"turns": [{"id": "unexpected-turn"}]}),
                ("foreign-project", {"projectId": "foreign-project"}),
                ("changed-path", {"path": str(root / "other-rollout.jsonl")}),
                ("wrong-status", {"status": {"type": "running"}}),
            )
            for name, updates in cases:
                with self.subTest(name=name):
                    runtime = self._runtime(root)
                    if name != "missing-proof":
                        self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
                    runtime._raw = lambda _method, _params, value=updates: (
                        self._empty_metadata(fixture, **value)
                    )
                    with self.assertRaisesRegex(RuntimePolicyError, "PROJECT_BINDING_MISMATCH"):
                        runtime.read_stored(thread_id="new-empty-thread")

            runtime = self._runtime(root)
            self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
            runtime._project_binding = runtime.project_binding.model_copy(
                update={"project_id": "changed-project"}
            )
            runtime._raw = lambda *_args: self.fail("변경 binding은 raw 전에 차단해야 합니다.")
            with self.assertRaisesRegex(RuntimePolicyError, "현재 계약과 다릅니다"):
                runtime.read_stored(thread_id="new-empty-thread")

    def test_peer_rejects_digest_metadata_turn_cursor_or_malformed_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            list_responses = (
                {"data": [{"id": "turn-1"}], "nextCursor": None, "backwardsCursor": None},
                {"data": [], "nextCursor": "more", "backwardsCursor": None},
                {"data": [], "nextCursor": None, "backwardsCursor": "before"},
                {"data": None, "nextCursor": None, "backwardsCursor": None},
                {"data": []},
            )
            for response in list_responses:
                with self.subTest(response=response):
                    runtime = self._runtime(root)
                    self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
                    runtime._raw = lambda _method, _params: self._empty_metadata(fixture)
                    runtime._codex = SimpleNamespace(
                        _client=SimpleNamespace(
                            thread_read=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                MethodNotFoundError(-32601, "list_turns is not supported yet")
                            )
                        )
                    )
                    self._install_peer(
                        runtime,
                        lambda method, _params, value=response: (
                            value if method == "thread/turns/list"
                            else self._empty_metadata(fixture)
                        ),
                    )
                    with self.assertRaisesRegex(RuntimePolicyError, "paginated turn 목록"):
                        runtime.read_stored(thread_id="new-empty-thread")

            changes = (
                {"projectId": "foreign-project"},
                {"cwd": str(root / "other")},
                {"path": str(root / "other-rollout.jsonl")},
                {"status": {"type": "running"}},
                {"turns": [{"id": "unexpected-turn"}]},
            )
            for updates in changes:
                with self.subTest(updates=updates):
                    runtime = self._runtime(root)
                    self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
                    runtime._raw = lambda _method, _params: self._empty_metadata(fixture)
                    runtime._codex = SimpleNamespace(
                        _client=SimpleNamespace(
                            thread_read=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                MethodNotFoundError(-32601, "list_turns is not supported yet")
                            )
                        )
                    )
                    metadata_reads = 0

                    def peer_raw(method: str, _params: dict[str, object]):
                        nonlocal metadata_reads
                        if method == "thread/turns/list":
                            return {"data": [], "nextCursor": None, "backwardsCursor": None}
                        metadata_reads += 1
                        return self._empty_metadata(
                            fixture, **(updates if metadata_reads == 2 else {})
                        )

                    self._install_peer(runtime, peer_raw)
                    with self.assertRaisesRegex(RuntimePolicyError, "PROJECT_BINDING_MISMATCH"):
                        runtime.read_stored(thread_id="new-empty-thread")

            runtime = self._runtime(root)
            self._create_bound_empty_thread(runtime, project_root=root, cwd=fixture)
            runtime._raw = lambda _method, _params: self._empty_metadata(fixture)
            runtime._codex = SimpleNamespace(
                _client=SimpleNamespace(
                    thread_read=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        MethodNotFoundError(-32601, "list_turns is not supported yet")
                    )
                )
            )
            peer = self._install_peer(runtime, lambda *_args: self._empty_metadata(fixture))
            peer.executable_digest = "sha256:" + "f" * 64
            with self.assertRaisesRegex(RuntimePolicyError, "executable"):
                runtime.read_stored(thread_id="new-empty-thread")

    def test_persisted_read_reports_exact_provider_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = self._runtime(root)
            runtime._raw = lambda _method, _params: {"thread": {
                "id": "stored-thread",
                "projectId": PROJECT_ID,
            }}
            runtime._codex = SimpleNamespace(
                _client=SimpleNamespace(
                    thread_read=lambda *_args, **_kwargs: SimpleNamespace(
                        thread=SimpleNamespace(turns=())
                    )
                )
            )

            observation = runtime.read_stored(thread_id="stored-thread")

            self.assertEqual(PROJECT_ID, observation.payload["project_id"])
            self.assertEqual(
                "thread/read(includeTurns=true)",
                observation.payload["turn_history_source"],
            )
            self.assertEqual(
                {"source": "thread/read.projectId"},
                observation.payload["project_binding_verification"],
            )

    def test_unbound_thread_start_keeps_legacy_request_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
            runtime._project_binding = None
            runtime._ephemeral_thread_ids = set()
            runtime._first_empty_threads = set()
            runtime._new_thread_project_proofs = {}
            runtime.verify_execution_policy = lambda cwd: _policy(Path(cwd))
            calls: list[tuple[str, dict[str, object]]] = []

            def raw(method: str, params: dict[str, object]) -> dict[str, object]:
                calls.append((method, params))
                return {
                    "activePermissionProfile": {"id": REQUIRED_PERMISSION_PROFILE},
                    "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                    "cwd": str(root),
                    "model": "inventory-selected-model",
                    "thread": {"id": "legacy-thread", "turns": []},
                }

            runtime._raw = raw
            runtime.create_thread(
                cwd=root,
                title="ignored",
                model="inventory-selected-model",
                developer_instructions="instructions",
            )

        self.assertEqual(["thread/start"], [call[0] for call in calls])
        self.assertNotIn("projectId", calls[0][1])

    def test_exact_creation_proof_allows_only_the_first_turn_attempt(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.turn_calls = 0

            def turn_start(self, _thread_id, _input_items, params=None):
                del params
                self.turn_calls += 1
                return SimpleNamespace(turn=SimpleNamespace(id="first-turn"))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            runtime = self._runtime(root)
            runtime.verify_execution_policy = lambda cwd: _policy(Path(cwd))

            def create_raw(method: str, _params: dict[str, object]) -> dict[str, object]:
                if method == "project/read":
                    return self._project_response(root)
                return {
                    "activePermissionProfile": {"id": REQUIRED_PERMISSION_PROFILE},
                    "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                    "cwd": str(fixture),
                    "model": "inventory-selected-model",
                    "thread": {
                        "id": "new-empty-thread",
                        "ephemeral": False,
                        "turns": [],
                        "projectId": PROJECT_ID,
                        "path": str(fixture / "rollout.jsonl"),
                    },
                }

            runtime._raw = create_raw
            runtime.create_thread(
                cwd=fixture,
                title="ignored",
                model="inventory-selected-model",
                developer_instructions="instructions",
            )
            client = FakeClient()
            runtime._codex = SimpleNamespace(_client=client)
            runtime._raw = lambda _method, _params: self.fail(
                "검증된 같은 연결의 첫 turn 전에 저장 projection을 읽으면 안 됩니다."
            )

            receipt = runtime.start_turn(
                thread_id="new-empty-thread",
                cwd=fixture,
                prompt="prompt",
                model="inventory-selected-model",
                effort="high",
            )

            self.assertEqual("first-turn", receipt.operation_id)
            self.assertEqual(1, client.turn_calls)
            self.assertNotIn("new-empty-thread", runtime._new_thread_project_proofs)

            runtime._raw = lambda _method, _params: {
                "thread": {"id": "new-empty-thread", "projectId": None}
            }
            with self.assertRaisesRegex(RuntimePolicyError, "PROJECT_BINDING_MISMATCH"):
                runtime.start_turn(
                    thread_id="new-empty-thread",
                    cwd=fixture,
                    prompt="must-not-retry",
                    model="inventory-selected-model",
                    effort="high",
                )
            self.assertEqual(1, client.turn_calls)

    def test_changed_binding_consumes_and_rejects_creation_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = self._runtime(root)
            runtime.verify_execution_policy = lambda cwd: _policy(Path(cwd))

            def create_raw(method: str, _params: dict[str, object]) -> dict[str, object]:
                if method == "project/read":
                    return self._project_response(root)
                return {
                    "activePermissionProfile": {"id": REQUIRED_PERMISSION_PROFILE},
                    "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                    "cwd": str(root),
                    "model": "inventory-selected-model",
                    "thread": {
                        "id": "binding-changed-thread",
                        "ephemeral": False,
                        "turns": [],
                        "projectId": PROJECT_ID,
                        "path": str(root / "rollout.jsonl"),
                    },
                }

            runtime._raw = create_raw
            runtime.create_thread(
                cwd=root,
                title="ignored",
                model="inventory-selected-model",
                developer_instructions="instructions",
            )
            runtime._project_binding = runtime.project_binding.model_copy(
                update={"project_id": "different-server-project"}
            )
            runtime._codex = SimpleNamespace(
                _client=SimpleNamespace(
                    turn_start=lambda *_args, **_kwargs: self.fail(
                        "변경된 결속으로 turn을 시작하면 안 됩니다."
                    )
                )
            )

            with self.assertRaisesRegex(RuntimePolicyError, "현재 계약과 다릅니다"):
                runtime.start_turn(
                    thread_id="binding-changed-thread",
                    cwd=root,
                    prompt="prompt",
                    model="inventory-selected-model",
                    effort="high",
                )

            self.assertNotIn("binding-changed-thread", runtime._new_thread_project_proofs)

    def test_changed_first_turn_cwd_consumes_and_rejects_creation_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            other = root / "other"
            other.mkdir()
            runtime = self._runtime(root)
            self._create_bound_empty_thread(
                runtime, project_root=root, cwd=fixture
            )
            runtime._codex = SimpleNamespace(
                _client=SimpleNamespace(
                    turn_start=lambda *_args, **_kwargs: self.fail(
                        "다른 cwd로 첫 turn을 시작하면 안 됩니다."
                    )
                )
            )

            with self.assertRaisesRegex(RuntimePolicyError, "현재 계약과 다릅니다"):
                runtime.start_turn(
                    thread_id="new-empty-thread",
                    cwd=other,
                    prompt="prompt",
                    model="inventory-selected-model",
                    effort="high",
                )

            self.assertNotIn("new-empty-thread", runtime._new_thread_project_proofs)

    def test_project_mismatch_blocks_start_resume_and_stored_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for operation in ("start", "resume", "read"):
                with self.subTest(operation=operation):
                    runtime = self._runtime(root)
                    raw_calls: list[tuple[str, dict[str, object]]] = []
                    runtime._raw = lambda method, params: (
                        raw_calls.append((method, params))
                        or {"thread": {"id": "stored-thread", "projectId": None}}
                    )
                    runtime.verify_execution_policy = lambda _cwd: self.fail(
                        "프로젝트 불일치 뒤 정책이나 provider 호출로 진행하면 안 됩니다."
                    )
                    runtime._codex = SimpleNamespace(
                        _client=SimpleNamespace(
                            thread_read=lambda *_args, **_kwargs: self.fail(
                                "typed thread/read로 진행하면 안 됩니다."
                            )
                        ),
                        thread_resume=lambda *_args, **_kwargs: self.fail(
                            "thread/resume으로 진행하면 안 됩니다."
                        ),
                    )

                    with self.assertRaisesRegex(RuntimePolicyError, "PROJECT_BINDING_MISMATCH"):
                        if operation == "start":
                            runtime.start_turn(
                                thread_id="stored-thread",
                                cwd=root,
                                prompt="prompt",
                                model="inventory-selected-model",
                                effort="high",
                            )
                        elif operation == "resume":
                            runtime.resume(thread_id="stored-thread", cwd=root)
                        else:
                            runtime.read_stored(thread_id="stored-thread")

                    self.assertEqual(
                        [("thread/read", {"threadId": "stored-thread", "includeTurns": False})],
                        raw_calls,
                    )


if __name__ == "__main__":
    unittest.main()
