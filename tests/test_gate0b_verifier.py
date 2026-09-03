from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flowmarshal.adapters.authority import FileHumanControlAuthority
from flowmarshal.adapters.evidence import FileEvidenceStore
from flowmarshal.adapters.runtime import FakeAgentRuntime
from flowmarshal.adapters.sqlite import SQLiteLedger
from flowmarshal.application import FlowMarshalService
from flowmarshal.domain import (
    CompletionCriterion,
    PlanDraft,
    VerificationSpec,
    VerificationType,
    WorkItemDefinition,
)
from flowmarshal.gate0b.verifier import Gate0BVerifier


class FixedClock:
    def now(self) -> str:
        return datetime(2026, 9, 2, tzinfo=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )


class Gate0BVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        workspace = self.root / "workspace"
        workspace.mkdir()
        clock = FixedClock()
        self.database = self.root / "flowmarshal.db"
        ledger = SQLiteLedger(self.database, clock=clock)
        ledger.initialize()
        authority = FileHumanControlAuthority(
            clock=clock,
            capability_path=self.root / "control" / "approval-capability",
        )
        authority.initialize()
        service = FlowMarshalService(
            ledger=ledger,
            evidence_store=FileEvidenceStore(self.root / "evidence"),
            authority=authority,
            runtime=FakeAgentRuntime(),
        )
        registered = service.register_project(name="검증", workspace=workspace)
        draft = PlanDraft(
            project_id=registered.project_id,
            summary="독립 verifier 합성 계획",
            work_items=(
                WorkItemDefinition(
                    client_ref="task_1",
                    goal="검증한다.",
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=(
                        CompletionCriterion(
                            criterion_id="workspace",
                            description="workspace 존재",
                            verification=VerificationSpec(
                                type=VerificationType.WORKSPACE_PREDICATE,
                                relative_path=".",
                                predicate="exists",
                            ),
                        ),
                    ),
                ),
            ),
        )
        revision = service.import_candidate_plan(draft)
        service.approve_plan(
            revision, authority.issue("approve_plan", draft.content_digest)
        )
        service.activate_plan(
            revision, authority.issue("activate_plan", draft.content_digest)
        )
        outcome = service.run_once(registered.project_id)
        self.assertEqual("completed", outcome.status)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_ledger_is_go(self) -> None:
        report = Gate0BVerifier(self.database).verify()
        failures = [check for check in report.checks if not check.passed]
        self.assertEqual([], failures)
        self.assertEqual("GO", report.overall)

    def test_raw_state_tampering_is_detected_independently(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER tr_terminal_attempt_immutable")
            connection.execute(
                "UPDATE attempts SET failure_code = 'RAW_TAMPER' WHERE status = 'completed'"
            )
            connection.commit()
        finally:
            connection.close()

        report = Gate0BVerifier(self.database).verify()

        self.assertEqual("NO_GO", report.overall)
        failed = {check.check_id for check in report.checks if not check.passed}
        self.assertIn("state_attestations", failed)
        self.assertIn("database_guards", failed)

    def test_evidence_tampering_is_detected(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            path = Path(
                connection.execute(
                    "SELECT storage_path FROM evidence_records LIMIT 1"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        path.write_text("변조", encoding="utf-8")

        report = Gate0BVerifier(self.database).verify()

        self.assertEqual("NO_GO", report.overall)
        failed = {check.check_id for check in report.checks if not check.passed}
        self.assertIn("evidence_invariants", failed)


if __name__ == "__main__":
    unittest.main()
