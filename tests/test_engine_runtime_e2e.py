from __future__ import annotations

import inspect
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import flowmarshal.engine.runtime as runtime_module
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.runtime import (
    CodexAppServerRuntime,
    ExecutionPolicyEvidence,
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    RuntimePolicyError,
)
from flowmarshal.engine.smoke import run_synthetic_lifecycle


class EngineRuntimeE2ETests(unittest.TestCase):
    def test_interrupt_does_not_resume_existing_thread(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        calls = []
        runtime._raw = lambda method, params: calls.append((method, params)) or {}
        receipt = runtime.interrupt(thread_id="stored-thread", turn_id="stored-turn")
        self.assertEqual("stored-turn", receipt.operation_id)
        self.assertEqual(
            [("turn/interrupt", {"threadId": "stored-thread", "turnId": "stored-turn"})], calls
        )

    def test_cli_runtime_lifetime_waits_without_interpreting_result(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        future = Future()
        runtime._turn_futures = {"thread": (object(), future)}
        self.assertFalse(runtime.wait_for_active_turns(timeout_seconds=0))
        future.set_exception(RuntimeError("provider failed"))
        self.assertTrue(runtime.wait_for_active_turns(timeout_seconds=0))

    def test_synthetic_full_lifecycle_reaches_evidence_backed_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("합성 지침", encoding="utf-8")
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
            status = run_synthetic_lifecycle(
                project_root=project,
                database_path=base / "state" / "flowmarshal-engine.sqlite3",
                artifact_root=base / "artifacts",
            )
        self.assertEqual("completed", status["project"]["run_state"])
        self.assertIsNone(status["project"]["active_plan_revision_id"])
        self.assertEqual("completed", status["plans"][0]["status"])
        self.assertEqual("completed", status["tasks"][0]["status"])
        self.assertEqual("succeeded", status["attempts"][0]["status"])
        self.assertTrue(status["history_valid"])

    def test_new_runtime_adapter_does_not_import_legacy_domains(self) -> None:
        source = inspect.getsource(runtime_module)
        self.assertNotIn("flowmarshal.core", source)
        self.assertNotIn("flowmarshal.planning", source)
        self.assertNotIn("from ..core", source)
        self.assertNotIn("from ..planning", source)

    def test_required_runtime_policy_is_full_access_without_approval(self) -> None:
        self.assertEqual(":danger-full-access", REQUIRED_PERMISSION_PROFILE)
        self.assertEqual("never", REQUIRED_APPROVAL_POLICY)
        evidence = ExecutionPolicyEvidence(
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            config_digest="sha256:" + "1" * 64,
            profile_catalog_digest="sha256:" + "2" * 64,
            cwd=str(Path.cwd()),
        )
        self.assertEqual("local", evidence.environment)

    def test_new_thread_turn_starts_without_resume_roundtrip(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def turn_start(self, thread_id, input_items, params=None):
                self.calls.append((thread_id, input_items, params))
                return SimpleNamespace(turn=SimpleNamespace(id="turn_runtime_smoke"))

        client = FakeClient()
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        runtime._codex = SimpleNamespace(  # type: ignore[attr-defined]
            _client=client,
            thread_resume=lambda *_args, **_kwargs: self.fail("새 thread에서 resume하면 안 됩니다."),
        )
        runtime._turn_futures = {}  # type: ignore[attr-defined]
        runtime._ephemeral_thread_ids = set()  # type: ignore[attr-defined]
        runtime.verify_execution_policy = lambda _cwd: ExecutionPolicyEvidence(  # type: ignore[method-assign]
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            config_digest="sha256:" + "1" * 64,
            profile_catalog_digest="sha256:" + "2" * 64,
            cwd=str(Path.cwd()),
        )
        receipt = runtime.start_turn(
            thread_id="thread_runtime_smoke",
            cwd=Path.cwd(),
            prompt="구조화 응답",
            model="inventory-selected-model",
            effort="low",
        )
        self.assertEqual("turn_runtime_smoke", receipt.operation_id)
        self.assertEqual(1, len(client.calls))
        self.assertEqual("thread_runtime_smoke", client.calls[0][0])

    def test_tracked_ephemeral_turn_is_read_from_completion_stream(self) -> None:
        future = Future()
        usage = SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "total": {
                    "inputTokens": 100,
                    "cachedInputTokens": 50,
                    "outputTokens": 10,
                    "reasoningOutputTokens": 4,
                }
            }
        )
        future.set_result(
            SimpleNamespace(
                id="turn_ephemeral",
                status="completed",
                final_response='{"status":"ok"}',
                usage=usage,
                duration_ms=25,
                items=[],
            )
        )
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        runtime._turn_futures = {  # type: ignore[attr-defined]
            "thread_ephemeral": (SimpleNamespace(id="turn_ephemeral"), future)
        }
        runtime._ephemeral_thread_ids = {"thread_ephemeral"}  # type: ignore[attr-defined]
        observation = runtime.read(thread_id="thread_ephemeral")
        self.assertFalse(observation.active)
        self.assertEqual("completed", observation.terminal_status)
        self.assertEqual(100, observation.payload["usage"]["total"]["inputTokens"])


if __name__ == "__main__":
    unittest.main()
