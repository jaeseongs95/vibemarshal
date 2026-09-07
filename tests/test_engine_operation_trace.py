from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.operation_trace import (
    OperationTrace,
    recalculate_trace_manifest,
    verify_operation_trace,
)
from flowmarshal.engine.role_observations import RoleCallReceipt
from flowmarshal.engine.roles import CodexStructuredRoleRunner, make_role_request
from flowmarshal.engine.domain import utc_now
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.role_execution import (
    RoleTimeoutOverride,
    RoleTimeoutPolicy,
    use_role_timeout_policy,
)
from flowmarshal.engine.role_observations import StructuredRoleError
from flowmarshal.engine.runtime import (
    CodexAppServerRuntime,
    EngineDispatcher,
    FakeCodexRuntime,
    RuntimeOperationReceipt,
)
from tests.test_engine_qualification import qualification_inventory
from tests.test_engine_roles import ImmediateRoleRuntime, InterruptedRoleRuntime


ROOT = Path(__file__).resolve().parents[1]


class OperationTraceTests(unittest.TestCase):
    def test_logical_operation_requires_actual_rpc_child(self) -> None:
        trace = OperationTrace({"call_id": "call-logical"}, expected_operations=("create",))
        logical = trace.begin(
            "create", {"cwd": "C:/project"}, call_id="call-logical",
            category="logical", deadline_seconds=30,
        )
        preflight = trace.begin(
            "read", {"method": "config/read"}, call_id="call-logical",
            category="rpc", parent_operation_id=logical.operation_id,
            rpc_method="config/read", deadline_seconds=30,
        )
        trace.finish(preflight, response={"config": {}})
        trace.finish(logical, response={"thread_id": "thread-1"})

        verified = verify_operation_trace(trace.seal())

        self.assertTrue(verified.valid, verified.errors)
        self.assertFalse(verified.complete)
        self.assertEqual(
            [logical.operation_id],
            verified.manifest["rpc_coverage_missing_operation_ids"],
        )

    def test_rpc_method_kind_and_required_parent_are_verified(self) -> None:
        trace = OperationTrace({"call_id": "call-parent"}, expected_operations=("create",))
        logical = trace.begin(
            "create", {"cwd": "C:/project"}, call_id="call-parent",
            category="logical", deadline_seconds=30,
        )
        wrong = trace.begin(
            "create", {"method": "config/read"}, call_id="call-parent",
            category="rpc", rpc_method="config/read", deadline_seconds=5,
        )
        trace.finish(wrong, response={"config": {}})
        trace.finish(logical, response={"thread_id": "thread-1"})

        verified = verify_operation_trace(trace.seal())

        self.assertFalse(verified.valid)
        self.assertIn(f"TRACE_RPC_KIND_MISMATCH:{wrong.operation_id}", verified.errors)
        self.assertIn(f"TRACE_RPC_PARENT_MISSING:{wrong.operation_id}", verified.errors)

    def test_codex_create_records_each_raw_rpc_under_one_logical_operation(self) -> None:
        class Client:
            def __init__(self, cwd):
                self.cwd = cwd

            def _request_raw(self, method, params):
                if method == "config/read":
                    return {"config": {
                        "default_permissions": ":danger-full-access",
                        "approval_policy": "never",
                    }}
                if method == "permissionProfile/list":
                    return {"data": [{"id": ":danger-full-access", "allowed": True}],
                            "nextCursor": None}
                if method == "thread/start":
                    return {
                        "activePermissionProfile": {"id": ":danger-full-access"},
                        "approvalPolicy": "never", "cwd": str(self.cwd),
                        "model": "test-model",
                        "thread": {"id": "thread-1", "turns": []},
                    }
                raise AssertionError(method)

        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp).resolve()
            runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
            runtime._project_binding = None
            runtime._thread_trace_scopes = {}
            runtime._ephemeral_thread_ids = set()
            runtime._first_empty_threads = set()
            runtime._codex = SimpleNamespace(_client=Client(cwd))
            trace = OperationTrace(
                {"call_id": "call-create"}, expected_operations=("create",),
            )
            logical = trace.begin(
                "create", {"cwd": str(cwd)}, call_id="call-create",
                category="logical", deadline_seconds=30,
            )
            with trace.operation_scope(logical):
                receipt = runtime.create_thread(
                    cwd=cwd, title="test", model="test-model",
                    developer_instructions="test", ephemeral=True,
                )
            trace.finish(logical, response=receipt)
            document = trace.seal()

        verified = verify_operation_trace(document)
        self.assertTrue(verified.complete, verified.errors)
        self.assertEqual({"create": 1}, verified.manifest["actual_started_counts"])
        self.assertEqual(
            {"create": 1, "read": 2}, verified.manifest["actual_rpc_counts"],
        )
        rpc_rows = [row for row in document["rows"] if row["category"] == "rpc"]
        self.assertEqual(
            ["config/read", "permissionProfile/list", "thread/start"],
            [row["rpc_method"] for row in rpc_rows],
        )
        self.assertTrue(all(row["parent_operation_id"] == logical.operation_id for row in rpc_rows))
        self.assertTrue(all(0 <= row["deadline_seconds"] <= 5 for row in rpc_rows if row["kind"] == "read"))
        # 조회 5초 정책을 새 thread 생성의 기존 실행 기한으로 확대하지 않는다.
        self.assertTrue(all(5 < row["deadline_seconds"] <= 30 for row in rpc_rows if row["kind"] == "create"))

    def test_stored_turn_pagination_records_every_raw_rpc_with_shared_deadline(self) -> None:
        class Client:
            def _request_raw(self, method, params):
                self_method = method
                if self_method != "thread/turns/list":
                    raise AssertionError(self_method)
                if "cursor" not in params:
                    return {
                        "data": [{"id": "turn-1", "items": []}],
                        "nextCursor": "page-2", "backwardsCursor": None,
                    }
                return {
                    "data": [{"id": "turn-2", "items": []}],
                    "nextCursor": None, "backwardsCursor": "page-1",
                }

        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        runtime._codex = SimpleNamespace(_client=Client())
        trace = OperationTrace({"attempt_id": "attempt-1"}, expected_operations=("read",))
        logical = trace.begin(
            "read", {"thread_id": "thread-1", "turn_id": "turn-2"},
            attempt_id="attempt-1", thread_id="thread-1", turn_id="turn-2",
            deadline_seconds=5, category="logical",
        )
        with trace.operation_scope(logical):
            turns, pages = runtime._read_exact_turn_history(thread_id="thread-1")
        trace.finish(
            logical, response={"turn_count": len(turns), "page_count": len(pages)},
            attempt_id="attempt-1", thread_id="thread-1", turn_id="turn-2",
        )
        document = trace.seal()

        verified = verify_operation_trace(document)
        self.assertTrue(verified.complete, verified.errors)
        self.assertEqual(2, verified.manifest["actual_rpc_counts"]["read"])
        rpc_rows = [row for row in document["rows"] if row["category"] == "rpc"]
        self.assertEqual(2, len(rpc_rows))
        self.assertTrue(all(row["rpc_method"] == "thread/turns/list" for row in rpc_rows))
        self.assertTrue(all(row["parent_operation_id"] == logical.operation_id for row in rpc_rows))
        self.assertTrue(all(0 <= row["deadline_seconds"] <= 5 for row in rpc_rows))

    def test_sdk_start_resume_and_interrupt_boundaries_are_individual_rpc_rows(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        trace = OperationTrace(
            {"attempt_id": "attempt-1"},
            expected_operations=("start", "resume", "interrupt"),
        )
        for kind, method, operation_id in (
            ("start", "sdk.turn/start", "turn-1"),
            ("resume", "sdk.thread/resume", "thread-1"),
            ("interrupt", "sdk.turn/interrupt", "turn-1"),
        ):
            logical = trace.begin(
                kind, {"operation": kind}, attempt_id="attempt-1",
                thread_id="thread-1", turn_id=("turn-1" if kind != "resume" else None),
                deadline_seconds=5, category="logical",
            )
            with trace.operation_scope(logical):
                runtime._actual_rpc(
                    method, {"threadId": "thread-1"},
                    lambda value=operation_id: SimpleNamespace(id=value),
                    thread_id="thread-1",
                    turn_id=("turn-1" if kind != "resume" else None),
                    response_projection=lambda value: {"operationId": value.id},
                )
            trace.finish(logical, response={"operation_id": operation_id})
        document = trace.seal()

        verified = verify_operation_trace(document)
        self.assertTrue(verified.complete, verified.errors)
        self.assertEqual(
            {"interrupt": 1, "resume": 1, "start": 1},
            verified.manifest["actual_rpc_counts"],
        )
        self.assertEqual(
            ["sdk.turn/start", "sdk.thread/resume", "sdk.turn/interrupt"],
            [row["rpc_method"] for row in document["rows"] if row["category"] == "rpc"],
        )

    def test_runtime_close_records_its_sdk_interrupt_as_detached_actual_rpc(self) -> None:
        class Handle:
            id = "turn-1"

            def __init__(self):
                self.interrupt_calls = 0

            def interrupt(self):
                self.interrupt_calls += 1

        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        handle = Handle()
        runtime._turn_futures = {"thread-1": (handle, Future())}
        runtime._completion_observers = {}
        runtime._completion_observer_lock = threading.Lock()
        runtime._interrupted_turn_ids = set()
        runtime._codex = SimpleNamespace(close=lambda: None)
        trace = OperationTrace({"attempt_id": "attempt-1"}, expected_operations=("start",))
        logical = trace.begin(
            "start", {"thread_id": "thread-1"}, attempt_id="attempt-1",
            thread_id="thread-1", turn_id="turn-1", deadline_seconds=5,
            category="logical",
        )
        with trace.operation_scope(logical) as scope:
            runtime._actual_rpc(
                "sdk.turn/start", {"threadId": "thread-1"},
                lambda: SimpleNamespace(id="turn-1"), thread_id="thread-1",
                turn_id="turn-1", response_projection=lambda value: {"turnId": value.id},
            )
        trace.finish(logical, response={"operation_id": "turn-1"})
        runtime._thread_trace_scopes = {"thread-1": scope}

        runtime.close(timeout_seconds=0.02)
        document = trace.seal()

        self.assertEqual(1, handle.interrupt_calls)
        verified = verify_operation_trace(document)
        self.assertTrue(verified.complete, verified.errors)
        interrupts = [
            row for row in document["rows"]
            if row["category"] == "rpc" and row["kind"] == "interrupt"
        ]
        self.assertEqual(1, len(interrupts))
        self.assertIsNone(interrupts[0]["parent_operation_id"])
        self.assertEqual("sdk.turn/interrupt", interrupts[0]["rpc_method"])
        self.assertLessEqual(interrupts[0]["deadline_seconds"], 0.02)

    def test_success_is_sealed_and_independently_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "call.operation-trace.jsonl"
            trace = OperationTrace(
                {"call_id": "call-1"}, path=path,
                expected_operations=("create", "start"),
            )
            create = trace.begin("create", {"cwd": temp}, call_id="call-1")
            trace.finish(
                create, response={"thread_id": "thread-1"},
                provider_call_id="thread-1", thread_id="thread-1",
            )
            start = trace.begin(
                "start", {"thread_id": "thread-1"}, call_id="call-1",
                thread_id="thread-1",
            )
            trace.finish(
                start, response={"turn_id": "turn-1"}, provider_call_id="turn-1",
                thread_id="thread-1", turn_id="turn-1",
            )
            document = trace.seal()

            verified = verify_operation_trace(
                document, expected_call_ids=("call-1",)
            )
            self.assertTrue(verified.valid, verified.errors)
            self.assertTrue(verified.complete, verified.errors)
            self.assertEqual(2, verified.manifest["row_count"])
            self.assertEqual({"create": 1, "start": 1}, verified.manifest["actual_started_counts"])
            self.assertEqual(verified.manifest, recalculate_trace_manifest(path))
            self.assertEqual(document, OperationTrace.from_path(path).snapshot())

    def test_unfinished_file_is_unsealed_and_pending_after_process_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "lost.operation-trace.jsonl"
            trace = OperationTrace(
                {"attempt_id": "attempt-1"}, path=path,
                expected_operations=("create", "start"),
            )
            trace.begin("create", {"cwd": temp}, attempt_id="attempt-1")

            verified = verify_operation_trace(path)
            self.assertTrue(verified.valid, verified.errors)
            self.assertFalse(verified.complete)
            self.assertFalse(verified.manifest["sealed"])
            self.assertEqual(1, len(verified.manifest["pending_operation_ids"]))
            self.assertEqual({"start": 1}, verified.manifest["missing_operation_counts"])

    def test_post_seal_operation_is_append_only_and_invalidates_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "post-seal.operation-trace.jsonl"
            trace = OperationTrace({"call_id": "call-post"}, path=path)
            first = trace.begin("read", {"step": 1}, call_id="call-post")
            trace.finish(first, response={"active": False})
            sealed = trace.seal()
            second = trace.begin("resume", {"step": 2}, call_id="call-post")
            trace.finish(second, response={"resumed": True})
            replayed = OperationTrace.from_path(path).seal()

        self.assertNotEqual(sealed["manifest"]["trace_digest"], replayed["manifest"]["trace_digest"])
        verified = verify_operation_trace(replayed)
        self.assertTrue(verified.valid, verified.errors)
        self.assertFalse(verified.complete)
        self.assertEqual(
            [second.operation_id], verified.manifest["post_seal_operation_ids"]
        )

    def test_errors_deadlines_and_call_bindings_are_explicit(self) -> None:
        trace = OperationTrace(
            {"call_id": "call-2"}, expected_operations=("read", "interrupt")
        )
        failed = trace.begin("read", {"thread_id": "t"}, call_id="call-2")
        trace.finish(failed, error=RuntimeError("read failed"), thread_id="t")
        timed_out = trace.begin(
            "interrupt", {"thread_id": "t", "turn_id": "u"},
            call_id="call-2", deadline_seconds=5, thread_id="t", turn_id="u",
        )
        trace.finish(
            timed_out, error=TimeoutError("deadline"), thread_id="t", turn_id="u"
        )
        document = trace.seal()
        verified = verify_operation_trace(document, expected_call_ids=("call-2",))

        self.assertTrue(verified.valid, verified.errors)
        self.assertFalse(verified.complete)
        self.assertEqual(1, len(verified.manifest["error_operation_ids"]))
        self.assertEqual(1, len(verified.manifest["deadline_exceeded_operation_ids"]))
        self.assertEqual(2, len(verified.manifest["call_bindings"]["call-2"]))

    def test_late_completion_is_distinct_from_deadline_status(self) -> None:
        trace = OperationTrace({"call_id": "call-late"}, expected_operations=("sdk_wait",))
        with patch(
            "flowmarshal.engine.operation_trace.time.monotonic_ns",
            side_effect=(1_000_000_000, 1_010_000_000),
        ):
            token = trace.begin(
                "sdk_wait", {"operation": "late"}, call_id="call-late",
                deadline_seconds=0.001,
            )
            trace.finish(token, response={"completed": True})
        document = trace.seal()

        row = document["rows"][0]
        self.assertEqual("deadline_exceeded", row["status"])
        self.assertTrue(row["late"])
        self.assertEqual([row["operation_id"]], document["manifest"]["late_operation_ids"])

    def test_body_and_chain_tampering_are_rejected(self) -> None:
        trace = OperationTrace({"call_id": "call-3"}, expected_operations=("read",))
        token = trace.begin("read", {"thread_id": "t"}, call_id="call-3")
        trace.finish(token, response={"active": False}, thread_id="t")
        document = trace.seal()
        tampered = copy.deepcopy(document)
        tampered["rows"][0]["response"]["active"] = True

        verified = verify_operation_trace(tampered)
        self.assertFalse(verified.valid)
        self.assertIn(
            f"TRACE_RESPONSE_DIGEST_MISMATCH:{tampered['rows'][0]['operation_id']}",
            verified.errors,
        )

    def test_old_receipt_serialization_omits_all_trace_fields(self) -> None:
        receipt = RoleCallReceipt(
            call_id="call", role="worker", status="succeeded", model="m", effort="low",
            inventory_digest="sha256:" + "0" * 64,
            permission_profile=":danger-full-access", approval_policy="never",
            input_digest="sha256:" + "1" * 64,
            output_digest="sha256:" + "2" * 64,
            output_schema_digest="sha256:" + "3" * 64,
            latency_ms=0, recorded_at=utc_now(),
        )
        serialized = receipt.model_dump(mode="json")
        self.assertNotIn("operation_trace", serialized)
        self.assertNotIn("operation_trace_ref", serialized)
        self.assertNotIn("operation_trace_digest", serialized)
        runtime_serialized = RuntimeOperationReceipt(
            operation_id="operation", payload={"ok": True}
        ).model_dump(mode="json")
        self.assertNotIn("operation_trace", runtime_serialized)
        self.assertNotIn("operation_trace_ref", runtime_serialized)
        self.assertNotIn("operation_trace_digest", runtime_serialized)

    def test_role_receipt_contains_sealed_call_bound_trace(self) -> None:
        runtime = ImmediateRoleRuntime(['{"answer":"ok"}'])
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(
                inventory=runtime.inventory, role="test-role", instructions="JSON만 반환",
                payload={"request": "ok"},
                output_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
                model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
                cwd=temp,
            )
            receipt = CodexStructuredRoleRunner(
                runtime, operation_trace_path=Path(temp) / "traces"
            ).run(request).receipt

        self.assertIsNotNone(receipt.operation_trace)
        assert receipt.operation_trace is not None
        self.assertEqual(sha256_digest(receipt.operation_trace), receipt.operation_trace_digest)
        verified = verify_operation_trace(
            receipt.operation_trace, expected_call_ids=(receipt.call_id,)
        )
        self.assertTrue(verified.complete, verified.errors)
        self.assertEqual(receipt.call_id, receipt.operation_trace["context"]["call_id"])
        self.assertEqual(
            {"create": 1, "sdk_wait": 1, "start": 1},
            verified.manifest["actual_started_counts"],
        )

    def test_engine_attempt_payload_traces_direct_interrupt_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            workspace, _ = _copy_fixture(ROOT, base)
            inventory = qualification_inventory()
            prepared = _prepare(
                workspace=workspace, state_root=base / "state", inventory=inventory,
                roles=default_role_configuration(ROOT),
            )
            runtime = FakeCodexRuntime(inventory)
            dispatcher = EngineDispatcher(prepared.service, runtime)
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
            dispatched = dispatcher.run_once(prepared.project_id)

            receipt = dispatcher.interrupt_attempt(dispatched.attempt_id)

        trace = receipt.payload["operation_trace"]
        verified = verify_operation_trace(trace)
        self.assertTrue(verified.valid, verified.errors)
        self.assertFalse(verified.complete)
        self.assertEqual(1, verified.manifest["actual_started_counts"]["interrupt"])
        interrupt = [row for row in trace["rows"] if row["kind"] == "interrupt"]
        self.assertEqual(1, len(interrupt))
        self.assertEqual(dispatched.attempt_id, interrupt[0]["attempt_id"])
        self.assertEqual(receipt.binding.thread_id, interrupt[0]["thread_id"])
        self.assertEqual(receipt.binding.turn_id, interrupt[0]["turn_id"])
        self.assertEqual(5.0, interrupt[0]["deadline_seconds"])

    def test_sdk_timeout_keeps_late_interrupt_artifact_unsealed_without_duplicate(self) -> None:
        class SlowInterruptRuntime(InterruptedRoleRuntime):
            def __init__(self):
                super().__init__()
                self.interrupt_calls = 0

            def interrupt(self, *, thread_id, turn_id):
                time.sleep(0.02)
                self.interrupt_calls += 1
                return super().interrupt(thread_id=thread_id, turn_id=turn_id)

        runtime = SlowInterruptRuntime()
        timeout_policy = RoleTimeoutPolicy(overrides=(RoleTimeoutOverride(
            role="test-role", timeout_seconds=0.001, replaces_timeout_seconds=900,
            reason="operation trace late interrupt test",
        ),))
        with tempfile.TemporaryDirectory() as temp, use_role_timeout_policy(timeout_policy):
            request = make_role_request(
                inventory=runtime.inventory, role="test-role", instructions="JSON만 반환",
                payload={"request": "wait"}, output_schema={"type": "object", "properties": {}},
                model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
                cwd=temp,
            )
            runner = CodexStructuredRoleRunner(
                runtime, poll_interval_seconds=0, interrupt_observation_seconds=0.001,
                operation_trace_path=Path(temp) / "traces",
            )
            with self.assertRaises(StructuredRoleError):
                runner.run(request)
            receipt = runner.receipts[-1]
            self.assertFalse(receipt.operation_trace["manifest"]["sealed"])
            self.assertTrue(receipt.operation_trace["manifest"]["pending_operation_ids"])
            trace_path = Path(receipt.operation_trace_ref)
            time.sleep(0.05)
            verified = verify_operation_trace(trace_path)
            current_trace = OperationTrace.from_path(trace_path).snapshot()

        self.assertTrue(verified.valid, verified.errors)
        self.assertFalse(verified.complete)
        self.assertFalse(verified.manifest["sealed"])
        self.assertFalse(verified.manifest["pending_operation_ids"])
        self.assertTrue(verified.manifest["late_operation_ids"])
        self.assertEqual(1, runtime.interrupt_calls)
        interrupt = [row for row in current_trace["rows"] if row["kind"] == "interrupt"]
        self.assertEqual(1, len(interrupt))
        self.assertGreater(interrupt[0]["deadline_seconds"], 0)
        self.assertLessEqual(interrupt[0]["deadline_seconds"], 5.0)
        self.assertEqual(receipt.thread_id, interrupt[0]["thread_id"])
        self.assertEqual(receipt.turn_ids[-1], interrupt[0]["turn_id"])


if __name__ == "__main__":
    unittest.main()
