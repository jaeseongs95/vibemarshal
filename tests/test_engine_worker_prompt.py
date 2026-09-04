from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.domain import ExecutionContextNeed, RunOnceAction, TaskExecutionSpecRevision, new_id
from flowmarshal.engine.runtime import EngineDispatcher
from flowmarshal.engine.service import EngineService, EngineServiceError
from flowmarshal.engine.worker_prompt import PromptArtifactError, PromptArtifactStore
from tests import test_engine_execution_automation as fixtures


class WorkerPromptTests(unittest.TestCase):
    setUp = fixtures.ExecutionAutomationTests.setUp
    prepared = fixtures.ExecutionAutomationTests.prepared
    validating = fixtures.ExecutionAutomationTests.validating

    def compiled(self, **kwargs):
        prepared, runtime = self.prepared(**kwargs)
        proposal = prepared.proposal.model_copy(update={"context_needs": (
            ExecutionContextNeed(need_id="add", description="수정 함수", symbol_hints=("add",)),
        )})
        spec = prepared.service.compile_execution_spec(proposal, inventory=self.inventory)
        store = PromptArtifactStore(prepared.service.ledger.artifact_root)
        bundle = store.load(spec.definition.context_manifest.prompt_binding)
        return prepared, runtime, spec, store, bundle

    def test_persisted_body_is_exact_runtime_input_with_contract_operations_and_selected_context(self):
        source = "def add(left, right):\r\n    return left - right\r\n\r\ndef unrelated():\r\n    return 999\r\n"
        prepared, runtime, spec, store, bundle = self.compiled(source_text=source)
        with patch.object(runtime, "start_turn", wraps=runtime.start_turn) as send:
            outcome = EngineDispatcher(prepared.service, runtime).run_once(prepared.project_id)
        prompt = send.call_args.kwargs["prompt"]
        self.assertEqual(bundle.rendered, prompt)
        self.assertIn("TaskContract:", prompt)
        projection = json.loads(prompt.split("ExecutionSpec 운영 상세:\n", 1)[1].split("\n</task-instruction>", 1)[0])
        self.assertEqual(spec.definition.model_dump(mode="json")["actions"], projection["actions"])
        self.assertIn(spec.definition.idempotency_key, prompt)
        self.assertIn("return left - right\r\n", prompt)
        self.assertIn("app.py#python-lines:1-2", prompt)
        self.assertNotIn("def unrelated", prompt)
        self.assertNotIn('"prompt_binding"', prompt)
        self.assertNotIn(spec.definition_digest, prompt)
        self.assertNotIn(bundle.binding.binding_digest, prompt)
        with prepared.service.ledger.read() as connection:
            request = json.loads(connection.execute(
                "SELECT request_json FROM runtime_intents WHERE attempt_id = ? AND kind = 'start_turn'",
                (outcome.attempt_id,),
            ).fetchone()[0])
        self.assertEqual(sha256_digest(prompt), request["prompt_digest"])
        self.assertTrue(store.path_for(bundle.binding).is_file())

    def test_missing_corrupt_and_rebound_artifacts_cannot_dispatch(self):
        for damage in ("missing", "json", "segment", "rebound", "swapped"):
            with self.subTest(damage=damage):
                prepared, runtime, spec, store, bundle = self.compiled(name=damage)
                path = store.path_for(bundle.binding)
                payload = bundle.model_dump(mode="json")
                if damage == "missing":
                    path.unlink()
                elif damage == "json":
                    path.write_text("{", encoding="utf-8")
                else:
                    if damage == "swapped":
                        payload["static_policy_prefix"], payload["stage_schema"] = payload["stage_schema"], payload["static_policy_prefix"]
                    else:
                        payload["dynamic_suffix"] = "임의 지시"
                    if damage == "rebound":
                        payload["binding"]["dynamic_suffix_digest"] = sha256_bytes(payload["dynamic_suffix"].encode())
                    path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(EngineServiceError, "PROMPT_ARTIFACT_INVALID"):
                    EngineDispatcher(prepared.service, runtime).run_once(prepared.project_id)
                self.assertEqual(0, runtime.create_calls)
                self.assertEqual(0, runtime.turn_calls)
                with prepared.service.ledger.read() as connection:
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM runtime_intents").fetchone()[0])

    def test_same_binding_put_is_concurrent_atomic_and_never_overwrites_damage(self):
        _, _, _, store, bundle = self.compiled()
        destination = store.path_for(bundle.binding)
        destination.unlink()
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = tuple(pool.map(lambda _: store.put(bundle), range(8)))
        self.assertEqual({destination}, set(paths))
        self.assertEqual(bundle, store.load(bundle.binding))
        self.assertEqual([], list(store.root.glob("*.tmp")))
        destination.write_bytes(b"broken")
        with self.assertRaises(PromptArtifactError):
            store.put(bundle)
        self.assertEqual(b"broken", destination.read_bytes())
        self.assertEqual([], list(store.root.glob("*.tmp")))

    def test_incomplete_write_never_publishes_or_registers_a_spec(self):
        prepared, runtime = self.prepared()
        with patch("flowmarshal.engine.worker_prompt.os.fsync", side_effect=OSError("저장 실패")):
            with self.assertRaisesRegex(EngineServiceError, "저장 실패"):
                prepared.service.compile_execution_spec(prepared.proposal, inventory=self.inventory)
        store = PromptArtifactStore(prepared.service.ledger.artifact_root)
        self.assertEqual([], list(store.root.iterdir()))
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0])
            self.assertEqual("ready", connection.execute("SELECT status FROM task_contracts").fetchone()[0])
        self.assertEqual(0, runtime.create_calls)

    def test_artifact_is_complete_before_spec_registration_and_orphan_is_safe(self):
        prepared, runtime = self.prepared()
        original = PromptArtifactStore.put
        saved = []

        def stop_after_publish(store, bundle):
            path = original(store, bundle)
            saved.append(path)
            self.assertEqual(bundle, store.load(bundle.binding))
            with prepared.service.ledger.read() as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0])
            raise OSError("게시 후 중단")

        with patch.object(PromptArtifactStore, "put", stop_after_publish):
            with self.assertRaisesRegex(EngineServiceError, "게시 후 중단"):
                prepared.service.compile_execution_spec(prepared.proposal, inventory=self.inventory)
        self.assertTrue(saved[0].is_file())
        self.assertEqual((prepared.task_id,), prepared.service.list_ready_tasks(prepared.project_id))
        self.assertEqual(RunOnceAction.BLOCKED, EngineDispatcher(prepared.service, runtime).run_once(prepared.project_id).action)

    def test_manual_spec_cannot_replace_core_prompt_binding(self):
        prepared, _, spec, _, _ = self.compiled()
        definition = spec.definition.model_copy(update={"timeout_seconds": spec.definition.timeout_seconds + 1})
        changed = TaskExecutionSpecRevision(
            execution_spec_revision_id=new_id("execution_spec"), task_id=spec.task_id, revision_no=2,
            definition=definition, definition_digest=definition.definition_digest,
            supersedes_execution_spec_revision_id=spec.execution_spec_revision_id, created_at=spec.created_at,
        )
        with self.assertRaisesRegex(EngineServiceError, "PROMPT_BINDING_MISMATCH"):
            prepared.service.materialize_execution_spec(changed, inventory=self.inventory)

    def test_corruption_after_thread_receipt_is_rechecked_before_turn(self):
        prepared, runtime, _, store, bundle = self.compiled()

        def corrupt(point):
            if point == "after_thread_receipt":
                store.path_for(bundle.binding).write_bytes(b"broken")

        with self.assertRaisesRegex(EngineServiceError, "PROMPT_ARTIFACT_INVALID"):
            EngineDispatcher(prepared.service, runtime, fault_hook=corrupt).run_once(prepared.project_id)
        self.assertEqual(1, runtime.create_calls)
        self.assertEqual(0, runtime.turn_calls)

    def test_restart_and_resume_send_saved_body_and_bind_final_notice(self):
        prepared, runtime, _, _, bundle = self.compiled()
        dispatcher = EngineDispatcher(prepared.service, runtime)
        first = dispatcher.run_once(prepared.project_id)
        thread = next(iter(runtime.threads.values()))
        runtime.interrupt(thread_id=thread.thread_id, turn_id=thread.turn_id)
        # Worker 변경은 원래 입력 본문을 다시 조립할 근거가 아니다.
        (prepared.workspace / "app.py").write_text("def add(left, right): return left + right", encoding="utf-8")
        restarted = EngineDispatcher(EngineService(prepared.service.ledger), runtime)
        with patch.object(runtime, "start_turn", wraps=runtime.start_turn) as send:
            resumed = restarted.run_once(prepared.project_id)
        self.assertEqual(first.attempt_id, resumed.attempt_id)
        self.assertEqual(1, runtime.create_calls)
        self.assertEqual(1, runtime.resume_calls)
        prompt = send.call_args.kwargs["prompt"]
        self.assertEqual("이전 turn이 중단되었습니다. 같은 TaskContract 범위에서 재개하세요.\n" + bundle.rendered, prompt)
        with prepared.service.ledger.read() as connection:
            requests = [json.loads(row[0]) for row in connection.execute(
                "SELECT request_json FROM runtime_intents WHERE kind = 'start_turn' ORDER BY rowid")]
        self.assertEqual(sha256_digest(bundle.rendered), requests[0]["prompt_digest"])
        self.assertEqual(sha256_digest(prompt), requests[1]["prompt_digest"])
        self.assertNotEqual(requests[0]["prompt_digest"], requests[1]["prompt_digest"])

    def test_resume_rejects_missing_bundle_before_provider_resume(self):
        prepared, runtime, _, store, bundle = self.compiled()
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id)
        thread = next(iter(runtime.threads.values()))
        runtime.interrupt(thread_id=thread.thread_id, turn_id=thread.turn_id)
        store.path_for(bundle.binding).unlink()
        result = EngineDispatcher(EngineService(prepared.service.ledger), runtime).run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, result.action)
        self.assertIn("PROMPT_ARTIFACT_INVALID", result.detail)
        self.assertEqual(0, runtime.resume_calls)
        self.assertEqual(1, runtime.turn_calls)

    def test_restart_after_thread_receipt_uses_artifact_without_creating_another_thread(self):
        prepared, runtime, _, _, bundle = self.compiled()

        def crash(point):
            if point == "after_thread_receipt":
                raise RuntimeError("thread receipt 뒤 중단")

        with self.assertRaisesRegex(RuntimeError, "receipt 뒤 중단"):
            EngineDispatcher(prepared.service, runtime, fault_hook=crash).run_once(prepared.project_id)
        self.assertEqual(0, runtime.turn_calls)
        restarted = EngineDispatcher(EngineService(prepared.service.ledger), runtime)
        with patch.object(runtime, "start_turn", wraps=runtime.start_turn) as send:
            outcome = restarted.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, outcome.action)
        self.assertEqual(1, runtime.create_calls)
        self.assertEqual(1, runtime.resume_calls)
        self.assertEqual(1, runtime.turn_calls)
        self.assertTrue(send.call_args.kwargs["prompt"].endswith(bundle.rendered))

    def test_semantic_validator_uses_post_execution_evidence_without_worker_bundle(self):
        prepared, runtime = self.prepared(semantic_task_validation=True)
        dispatcher = self.validating(prepared, runtime)
        self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(prepared.project_id).action)
        for path in PromptArtifactStore(prepared.service.ledger.artifact_root).root.glob("*.json"):
            path.unlink()
        with patch.object(runtime, "start_turn", wraps=runtime.start_turn) as send:
            result = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, result.action)
        prompt = send.call_args.kwargs["prompt"]
        self.assertIn("직접 관측 evidence catalog:", prompt)
        self.assertIn("after_digest", prompt)
        self.assertNotIn("<reference-data", prompt)
        self.assertIsNotNone(send.call_args.kwargs["output_schema"])


if __name__ == "__main__":
    unittest.main()
