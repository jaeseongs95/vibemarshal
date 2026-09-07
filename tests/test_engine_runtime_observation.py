from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from openai_codex.generated.v2_all import (
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)
from openai_codex.models import Notification

from flowmarshal.engine.runtime import CodexAppServerRuntime, RuntimePolicyError


def _turn(turn_id: str, *, status: str = "completed", text: str | None = None) -> dict:
    items = [] if text is None else [{"type": "agentMessage", "id": f"item-{turn_id}", "text": text}]
    return {
        "id": turn_id,
        "status": status,
        "items": items,
        "itemsView": "full",
        "startedAt": 10,
        "completedAt": 12,
        "durationMs": 2000,
    }


class RuntimeObservationTests(unittest.TestCase):
    def _stored_runtime(self, pages: dict[str | None, dict]) -> tuple[CodexAppServerRuntime, list[dict]]:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        calls: list[dict] = []

        def raw(method: str, params: dict) -> dict:
            self.assertEqual("thread/turns/list", method)
            calls.append(params)
            return pages[params.get("cursor")]

        runtime._raw = raw  # type: ignore[method-assign]
        runtime._codex = SimpleNamespace(
            _client=SimpleNamespace(
                thread_read=lambda *_args, **_kwargs: self.fail(
                    "exact turn 조회가 thread/read의 최신 turn으로 대체됐습니다."
                )
            )
        )
        return runtime, calls

    def test_exact_old_turn_uses_complete_pagination_instead_of_latest(self) -> None:
        runtime, calls = self._stored_runtime(
            {
                None: {
                    "data": [_turn("turn-old", text="old result")],
                    "nextCursor": "page-2",
                    "backwardsCursor": None,
                },
                "page-2": {
                    "data": [_turn("turn-new", text="new result")],
                    "nextCursor": None,
                    "backwardsCursor": "back",
                },
            }
        )

        observation = runtime.read_stored(
            thread_id="thread-1", turn_id="turn-old", timeout_seconds=1,
        )

        self.assertEqual("turn-old", observation.turn_id)
        self.assertEqual("old result", observation.final_response)
        self.assertEqual("turn-old", observation.payload["requested_turn_id"])
        self.assertEqual("turn-old", observation.payload["provider_turn"]["id"])
        self.assertEqual(
            "old result", observation.payload["provider_turn"]["items"][0]["text"],
        )
        self.assertEqual("thread/turns/list", observation.payload["turn_history_source"])
        self.assertEqual(2, len(observation.payload["turn_history_pages"]))
        self.assertEqual([None, "page-2"], [call.get("cursor") for call in calls])

    def test_exact_turn_rejects_missing_duplicate_and_incomplete_pagination(self) -> None:
        cases = {
            "missing": {
                None: {"data": [_turn("other")], "nextCursor": None, "backwardsCursor": None}
            },
            "duplicate": {
                None: {"data": [_turn("wanted")], "nextCursor": "p2", "backwardsCursor": None},
                "p2": {"data": [_turn("wanted")], "nextCursor": None, "backwardsCursor": "back"},
            },
            "cursor-cycle": {
                None: {"data": [_turn("wanted")], "nextCursor": "p2", "backwardsCursor": None},
                "p2": {"data": [_turn("other")], "nextCursor": "p2", "backwardsCursor": "back"},
            },
            "partial-items": {
                None: {
                    "data": [_turn("wanted") | {"itemsView": "summary"}],
                    "nextCursor": None,
                    "backwardsCursor": None,
                }
            },
        }
        for name, pages in cases.items():
            with self.subTest(name=name):
                runtime, _calls = self._stored_runtime(pages)
                with self.assertRaisesRegex(
                    RuntimePolicyError,
                    "RUNTIME_OBSERVATION_(BINDING_MISMATCH|PAGINATION_INCOMPLETE)",
                ):
                    runtime.read_stored(
                        thread_id="thread-1", turn_id="wanted", timeout_seconds=1,
                    )

    def test_hung_stored_read_returns_within_rpc_deadline(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        release = threading.Event()
        runtime._raw = lambda *_args, **_kwargs: release.wait(10)  # type: ignore[method-assign]
        runtime._codex = SimpleNamespace(_client=SimpleNamespace())
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "read_stored"):
                runtime.read_stored(
                    thread_id="thread-hung", turn_id="turn-hung", timeout_seconds=0.05,
                )
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 0.5)

    def test_interrupt_timeout_is_not_retried_by_close(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        release = threading.Event()
        rpc_calls: list[str] = []
        handle_calls: list[str] = []
        runtime._raw = (  # type: ignore[method-assign]
            lambda _method, _params: rpc_calls.append("rpc") or release.wait(10)
        )
        future: Future[object] = Future()
        runtime._turn_futures = {
            "thread-1": (
                SimpleNamespace(id="turn-1", interrupt=lambda: handle_calls.append("handle")),
                future,
            )
        }
        runtime._completion_observers = {}
        runtime._codex = SimpleNamespace(close=lambda: None)
        try:
            with self.assertRaisesRegex(TimeoutError, "turn/interrupt"):
                runtime.interrupt(
                    thread_id="thread-1", turn_id="turn-1", timeout_seconds=0.05,
                )
            runtime.close(timeout_seconds=0.05)
        finally:
            release.set()
        self.assertEqual(["rpc"], rpc_calls)
        self.assertEqual([], handle_calls)

    def test_close_is_bounded_when_interrupt_and_sdk_close_hang(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        release = threading.Event()
        close_started = threading.Event()
        close_calls: list[str] = []
        future: Future[object] = Future()
        runtime._turn_futures = {
            "thread-1": (SimpleNamespace(id="turn-1", interrupt=lambda: release.wait(10)), future)
        }
        runtime._completion_observers = {}
        def close() -> None:
            close_calls.append("close")
            close_started.set()
            release.wait(10)

        runtime._codex = SimpleNamespace(close=close)
        started = time.monotonic()
        try:
            runtime.close(timeout_seconds=0.05)
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(close_started.wait(0.2))
        self.assertEqual(["close"], close_calls)

    def test_active_read_exposes_collected_usage_and_lifecycle(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        future: Future[object] = Future()
        runtime._turn_futures = {
            "thread-1": (SimpleNamespace(id="turn-1"), future)
        }
        runtime._turn_usage_context = {
            "thread-1": {
                "usage": {"total": {"inputTokens": 7}},
                "provider_events": [{"method": "thread/tokenUsage/updated"}],
                "lifecycle": {
                    "last_event": {"method": "thread/tokenUsage/updated"},
                    "terminal_status": None,
                },
            }
        }

        observation = runtime.read(thread_id="thread-1")

        self.assertTrue(observation.active)
        self.assertEqual(7, observation.payload["usage"]["total"]["inputTokens"])
        self.assertEqual(
            "thread/tokenUsage/updated",
            observation.payload["lifecycle"]["last_event"]["method"],
        )

    def test_sdk_exception_without_terminal_event_keeps_status_unknown(self) -> None:
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        future: Future[object] = Future()
        future.set_exception(RuntimeError("transport closed before terminal"))
        runtime._turn_futures = {
            "thread-1": (SimpleNamespace(id="turn-1"), future)
        }
        runtime._turn_usage_context = {
            "thread-1": {
                "usage": {"total": {"inputTokens": 9}},
                "provider_events": [{"method": "thread/tokenUsage/updated"}],
                "lifecycle": {
                    "last_event": {"method": "thread/tokenUsage/updated"},
                    "terminal_status": None,
                },
            }
        }

        observation = runtime.read(thread_id="thread-1")

        self.assertFalse(observation.active)
        self.assertIsNone(observation.terminal_status)
        self.assertEqual(9, observation.payload["usage"]["total"]["inputTokens"])
        self.assertIn("transport closed", observation.payload["error"])

    def test_failed_sdk_turn_keeps_usage_terminal_error_and_lifecycle(self) -> None:
        usage = {
            "last": {
                "cachedInputTokens": 2,
                "inputTokens": 11,
                "outputTokens": 4,
                "reasoningOutputTokens": 1,
                "totalTokens": 15,
            },
            "total": {
                "cachedInputTokens": 2,
                "inputTokens": 11,
                "outputTokens": 4,
                "reasoningOutputTokens": 1,
                "totalTokens": 15,
            },
        }
        events = iter(
            [
                Notification(
                    method="thread/tokenUsage/updated",
                    payload=ThreadTokenUsageUpdatedNotification.model_validate(
                        {"threadId": "thread-1", "turnId": "turn-1", "tokenUsage": usage}
                    ),
                ),
                Notification(
                    method="turn/completed",
                    payload=TurnCompletedNotification.model_validate(
                        {
                            "threadId": "thread-1",
                            "turn": _turn("turn-1", status="failed", text="raw terminal item")
                            | {"error": {"message": "provider failed"}},
                        }
                    ),
                ),
            ]
        )

        class Client:
            def turn_start(self, *_args, **_kwargs):
                return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

            def register_turn_notifications(self, _turn_id):
                return None

            def unregister_turn_notifications(self, _turn_id):
                return None

            def next_turn_notification(self, _turn_id):
                return next(events)

        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        runtime._codex = SimpleNamespace(_client=Client())
        runtime._turn_futures = {}
        runtime._first_empty_threads = set()
        runtime._turn_usage_context = {}
        runtime._completion_observers = {}
        runtime._completion_observer_lock = threading.Lock()
        runtime._consume_new_thread_project_proof = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
        runtime.verify_execution_policy = lambda _cwd: None  # type: ignore[method-assign]

        receipt = runtime.start_turn(
            thread_id="thread-1",
            cwd=Path.cwd(),
            prompt="test",
            model="selected-model",
            effort="low",
        )
        observed = []
        runtime.register_completion_observer(
            thread_id="thread-1", turn_id=receipt.operation_id, observer=observed.append,
        )
        self.assertTrue(runtime.wait_for_active_turns(timeout_seconds=1))
        self.assertEqual(1, len(observed))
        observation = observed[0]
        self.assertEqual("failed", observation.terminal_status)
        self.assertEqual(11, observation.payload["usage"]["total"]["inputTokens"])
        self.assertEqual(
            ["thread/tokenUsage/updated", "turn/completed"],
            [event["method"] for event in observation.payload["provider_events"]],
        )
        self.assertEqual(
            "raw terminal item",
            observation.payload["provider_events"][1]["payload"]["turn"]["items"][0]["text"],
        )
        self.assertEqual("provider failed", observation.payload["lifecycle"]["terminal_error"]["message"])
        self.assertEqual(10, observation.payload["lifecycle"]["provider_started_at"])
        self.assertEqual("turn/completed", observation.payload["lifecycle"]["last_event"]["method"])


if __name__ == "__main__":
    unittest.main()
