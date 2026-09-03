from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from flowmarshal.orchestration.runtime_probe import run_r1_probe


class FakeSession:
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]],
        *,
        notifications: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.responses = {name: list(values) for name, values in responses.items()}
        self.notifications = {
            turn_id: list(values)
            for turn_id, values in (notifications or {}).items()
        }
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.initialize_payload = {"userAgent": "fake"}
        self.unexpected_requests: tuple[str, ...] = ()

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        queue = self.responses.get(method)
        if not queue:
            raise AssertionError(f"예상하지 않은 RPC: {method}")
        return queue.pop(0)

    def next_turn_notification(self, turn_id: str) -> dict[str, Any]:
        queue = self.notifications.get(turn_id)
        if not queue:
            raise AssertionError(f"예상하지 않은 turn notification: {turn_id}")
        return queue.pop(0)

    def unregister_turn_notifications(self, turn_id: str) -> None:
        del turn_id


def config_response(
    *, permissions: str = ":danger-full-access", approval: str = "never"
) -> dict[str, Any]:
    return {
        "config": {
            "default_permissions": permissions,
            "approval_policy": approval,
        }
    }


def permission_response(*, allowed: bool = True) -> dict[str, Any]:
    return {
        "data": [
            {"id": ":workspace", "allowed": True},
            {"id": ":danger-full-access", "allowed": allowed},
        ],
        "nextCursor": None,
    }


def model_response() -> dict[str, Any]:
    return {
        "data": [
            {
                "id": "catalog-default",
                "model": "catalog-default",
                "hidden": False,
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "medium"},
                ],
            }
        ],
        "nextCursor": None,
    }


def thread_document(
    workspace: Path,
    sources: list[str],
    *,
    turns: list[dict[str, Any]] | None = None,
    profile: str = ":danger-full-access",
) -> dict[str, Any]:
    return {
        "activePermissionProfile": {"id": profile, "extends": None},
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": str(workspace),
        "instructionSources": sources,
        "model": "catalog-default",
        "modelProvider": "openai",
        "reasoningEffort": "low",
        "runtimeWorkspaceRoots": [str(workspace)],
        "sandbox": {"type": "dangerFullAccess"},
        "thread": {
            "id": "thread-1",
            "sessionId": "session-1",
            "turns": turns or [],
        },
    }


class RuntimeProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "project"
        self.codex_home = root / "codex-home"
        self.workspace.mkdir()
        self.codex_home.mkdir()
        (self.workspace / "AGENTS.md").write_text("project", encoding="utf-8")
        (self.codex_home / "AGENTS.md").write_text("global", encoding="utf-8")
        self.sources = [
            str(self.codex_home / "AGENTS.md"),
            str(self.workspace / "AGENTS.md"),
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _successful_sessions(self) -> tuple[FakeSession, FakeSession]:
        completed_turn = {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {
                    "type": "agentMessage",
                    "text": json.dumps(
                        {"status": "R1_RUNTIME_OK", "marker": "marker-1"},
                        separators=(",", ":"),
                    ),
                }
            ],
        }
        started = thread_document(self.workspace, self.sources)
        before = {"thread": started["thread"]}
        after = {
            "thread": {
                "id": "thread-1",
                "sessionId": "session-1",
                "turns": [completed_turn],
            }
        }
        first = FakeSession(
            {
                "config/read": [config_response()],
                "permissionProfile/list": [permission_response()],
                "model/list": [model_response()],
                "thread/start": [started],
                "thread/name/set": [{}],
                "thread/read": [before, after],
                "turn/start": [{"turn": {"id": "turn-1", "status": "inProgress"}}],
            },
            notifications={
                "turn-1": [
                    {
                        "method": "turn/started",
                        "payload": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "inProgress"},
                        },
                    },
                    {
                        "method": "turn/completed",
                        "payload": {
                            "threadId": "thread-1",
                            "turn": completed_turn,
                        },
                    },
                ]
            },
        )
        second = FakeSession(
            {
                "thread/read": [after],
                "thread/resume": [
                    thread_document(
                        self.workspace, self.sources, turns=[completed_turn]
                    )
                ],
            }
        )
        return first, second

    def test_full_lifecycle_is_go_and_uses_no_legacy_sandbox(self) -> None:
        first, second = self._successful_sessions()
        sessions = iter((first, second))
        result = run_r1_probe(
            session_factory=lambda: next(sessions),
            workspace=self.workspace,
            codex_home=self.codex_home,
            run_id="test-r1",
            marker="marker-1",
        )

        self.assertEqual("GO", result["decision"])
        self.assertEqual("thread-1", result["receipts"]["thread_start"]["thread_id"])
        self.assertEqual("session-1", result["receipts"]["thread_resume"]["session_id"])
        self.assertTrue(result["receipts"]["thread_read"]["marker_matched"])
        calls = first.calls + second.calls
        for method, params in calls:
            if method in {"thread/start", "turn/start", "thread/resume"}:
                self.assertNotIn("sandbox", params or {})
                self.assertEqual(":danger-full-access", (params or {}).get("permissions"))
                self.assertEqual("never", (params or {}).get("approvalPolicy"))

    def test_permission_mismatch_stops_before_thread_creation(self) -> None:
        first = FakeSession({"config/read": [config_response(permissions=":workspace")]})
        result = run_r1_probe(
            session_factory=lambda: first,
            workspace=self.workspace,
            codex_home=self.codex_home,
            run_id="test-policy",
            marker="marker-1",
        )

        self.assertEqual("NO-GO", result["decision"])
        self.assertEqual("PERMISSION_POLICY_MISMATCH", result["error"]["code"])
        self.assertNotIn("thread/start", [method for method, _ in first.calls])

    def test_missing_project_instruction_stops_before_turn(self) -> None:
        started = thread_document(self.workspace, [self.sources[0]])
        first = FakeSession(
            {
                "config/read": [config_response()],
                "permissionProfile/list": [permission_response()],
                "model/list": [model_response()],
                "thread/start": [started],
            }
        )
        result = run_r1_probe(
            session_factory=lambda: first,
            workspace=self.workspace,
            codex_home=self.codex_home,
            run_id="test-sources",
            marker="marker-1",
        )

        self.assertEqual("NO-GO", result["decision"])
        self.assertEqual("INSTRUCTION_PROVENANCE_MISSING", result["error"]["code"])
        self.assertNotIn("turn/start", [method for method, _ in first.calls])

    def test_resume_requires_same_full_access_profile(self) -> None:
        first, second = self._successful_sessions()
        second.responses["thread/resume"] = [
            thread_document(
                self.workspace,
                self.sources,
                turns=[
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [],
                    }
                ],
                profile=":workspace",
            )
        ]
        sessions = iter((first, second))
        result = run_r1_probe(
            session_factory=lambda: next(sessions),
            workspace=self.workspace,
            codex_home=self.codex_home,
            run_id="test-resume-policy",
            marker="marker-1",
        )

        self.assertEqual("NO-GO", result["decision"])
        self.assertEqual("PERMISSION_POLICY_MISMATCH", result["error"]["code"])
        self.assertEqual("thread_resume", result["error"]["stage"])


if __name__ == "__main__":
    unittest.main()
