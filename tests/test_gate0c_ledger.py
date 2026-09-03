from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.context import RuntimeRole
from flowmarshal.gate0c.ledger import (
    AttemptStage,
    ExecutionStage,
    Gate0CLedger,
    Gate0CLedgerError,
    TaskState,
)


def plan_artifact():
    return {
        "plan_revision_id": "flowmarshal-gate0c-r1",
        "status": "approved",
        "approved_at": "2026-09-02T03:00:00Z",
        "document": {"sha256": "a" * 64},
        "approval_source": {"kind": "user", "text": "승인"},
    }


class Gate0CLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "gate0c.sqlite3"
        self.ledger = Gate0CLedger(self.path)
        self.ledger.import_approved_plan(plan_artifact())
        self.ledger.register_approved_tasks("flowmarshal-gate0c-r1")

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_task_and_attempt_transitions_are_event_sourced(self) -> None:
        self.ledger.append_task_state("FM-0C-1", TaskState.READY)
        self.ledger.append_task_state("FM-0C-1", TaskState.ACTIVE)
        self.ledger.create_attempt(
            attempt_id="attempt_runner",
            task_id="FM-0C-1",
            role=RuntimeRole.RUNNER,
            model="gpt-5.6-sol",
            effort="high",
            profile_digest=sha256_digest("profile"),
        )
        self.ledger.append_attempt_stage("attempt_runner", AttemptStage.DISPATCHED)
        rows = self.ledger.connection.execute("SELECT * FROM history ORDER BY sequence").fetchall()
        self.assertGreaterEqual(len(rows), 13)
        for previous, current in zip(rows, rows[1:]):
            self.assertEqual(previous["entry_hash"], current["previous_hash"])

    def test_append_only_triggers_reject_update_and_delete(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "UPDATE tasks SET definition_json='{}' WHERE task_id='FM-0C-1'"
            )
        self.ledger.connection.rollback()

        self.ledger.record_evidence(
            evidence_id="evidence_test",
            kind="synthetic_test",
            payload={"passed": True},
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "UPDATE evidence_records SET kind='tampered' "
                "WHERE evidence_id='evidence_test'"
            )
        self.ledger.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("DELETE FROM history")
        self.ledger.connection.rollback()

    def test_cross_role_receipt_substitution_is_rejected(self) -> None:
        profile = sha256_digest("profile")
        for attempt, role in (
            ("attempt_runner", RuntimeRole.RUNNER),
            ("attempt_validator", RuntimeRole.VALIDATOR),
        ):
            self.ledger.create_attempt(
                attempt_id=attempt,
                task_id="FM-0C-4",
                role=role,
                model="gpt-5.6-sol",
                effort="xhigh",
                profile_digest=profile,
            )
        self.ledger.append_execution(
            execution_id="exec_runner",
            attempt_id="attempt_runner",
            role=RuntimeRole.RUNNER,
            action_kind="thread_start",
            stage=ExecutionStage.RESERVED,
            request_digest=sha256_digest("request"),
            profile_digest=profile,
        )
        self.ledger.append_execution(
            execution_id="exec_runner",
            attempt_id="attempt_runner",
            role=RuntimeRole.RUNNER,
            action_kind="thread_start",
            stage=ExecutionStage.DISPATCHED,
            request_digest=sha256_digest("request"),
            profile_digest=profile,
        )
        self.ledger.append_execution(
            execution_id="exec_runner",
            attempt_id="attempt_runner",
            role=RuntimeRole.RUNNER,
            action_kind="thread_start",
            stage=ExecutionStage.SUCCEEDED,
            request_digest=sha256_digest("request"),
            profile_digest=profile,
            thread_id="thread_same",
            receipt_digest=sha256_digest("receipt"),
        )
        self.ledger.append_execution(
            execution_id="exec_validator",
            attempt_id="attempt_validator",
            role=RuntimeRole.VALIDATOR,
            action_kind="thread_start",
            stage=ExecutionStage.RESERVED,
            request_digest=sha256_digest("request2"),
            profile_digest=profile,
        )
        with self.assertRaises(Gate0CLedgerError) as caught:
            self.ledger.append_execution(
                execution_id="exec_validator",
                attempt_id="attempt_validator",
                role=RuntimeRole.VALIDATOR,
                action_kind="thread_start",
                stage=ExecutionStage.DISPATCHED,
                request_digest=sha256_digest("request2"),
                profile_digest=profile,
                thread_id="thread_same",
            )
        self.assertEqual("CROSS_ROLE_THREAD_REUSE", caught.exception.reason_code)

    def test_crash_window_becomes_unknown_without_duplicate_effect(self) -> None:
        profile = sha256_digest("profile")
        self.ledger.create_attempt(
            attempt_id="attempt_crash",
            task_id="FM-0C-4",
            role=RuntimeRole.RUNNER,
            model="gpt-5.6-sol",
            effort="xhigh",
            profile_digest=profile,
        )
        for stage in (ExecutionStage.RESERVED, ExecutionStage.DISPATCHED):
            self.ledger.append_execution(
                execution_id="exec_crash",
                attempt_id="attempt_crash",
                role=RuntimeRole.RUNNER,
                action_kind="thread_start",
                stage=stage,
                request_digest=sha256_digest("request"),
                profile_digest=profile,
            )
        self.assertEqual(("exec_crash",), self.ledger.quarantine_unsettled_executions())
        self.assertEqual((), self.ledger.quarantine_unsettled_executions())


if __name__ == "__main__":
    unittest.main()
