from __future__ import annotations

import hashlib
import inspect
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from flowmarshal.core import (
    ActivationSource,
    Assignment,
    AttemptStatus,
    CoreDomainError,
    FlowMarshalCore,
    PlanDraft,
    ProjectDefinition,
    RuntimeBindingReceipt,
    SQLiteCoreLedger,
    ValidationDefinition,
    ValidationResultInput,
    ValidationStatus,
    WorkItemDefinition,
)
from flowmarshal.core.smoke import run_r2_smoke


FORBIDDEN_LEGACY_TABLES = {
    "access_grants",
    "access_request_decisions",
    "access_requests",
    "authority_uses",
    "protected_paths",
}

REQUIRED_CORE_TABLES = {
    "attempts",
    "evidence_records",
    "history_events",
    "plan_revisions",
    "projects",
    "runtime_action_intents",
    "runtime_bindings",
    "validation_results",
    "work_item_dependencies",
    "work_items",
}


class TickClock:
    def __init__(self) -> None:
        self.tick = 0

    def now(self) -> str:
        self.tick += 1
        return f"2026-09-02T00:00:{self.tick:02d}Z"


def assignment() -> Assignment:
    return Assignment(
        execution_model_id="catalog-balanced",
        execution_reasoning_effort="medium",
        validation_model_id="catalog-strong",
        validation_reasoning_effort="high",
        selection_reason="일반 구현과 독립 검사를 분리한다.",
        fallback_policy={"mode": "stop_and_report"},
    )


def work_item(
    client_ref: str,
    *,
    dependencies: tuple[str, ...] = (),
    title_suffix: str = "",
) -> WorkItemDefinition:
    return WorkItemDefinition(
        client_ref=client_ref,
        title=f"{client_ref} 구현{title_suffix}",
        objective=f"{client_ref}를 독립적으로 구현한다.",
        dependencies=dependencies,
        context_sources=("AGENTS.md", "requirements.md"),
        expected_changes=(f"src/{client_ref}.py",),
        out_of_scope=("배포",),
        deliverables=(f"src/{client_ref}.py",),
        acceptance_criteria=(f"{client_ref} 검사가 통과한다.",),
        validations=(
            ValidationDefinition(
                criterion_id=f"{client_ref}.tests",
                check_type="command",
                specification={"command": f"test-{client_ref}"},
            ),
        ),
        execution_requirements={"network": "normal"},
        assignment=assignment(),
    )


class CoreR2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
        self.reference = self.root / "requirements.md"
        self.reference.write_text("승인된 요구사항", encoding="utf-8")
        self.database = self.root / "state" / "core.sqlite3"
        self.ledger = SQLiteCoreLedger(self.database, clock=TickClock())
        self.ledger.initialize()
        self.core = FlowMarshalCore(self.ledger)
        self.project_id = self.core.register_project(
            ProjectDefinition(
                name="합성 프로젝트",
                root=str(self.workspace),
                context_sources=(
                    str(self.workspace / "AGENTS.md"),
                    str(self.reference),
                ),
                default_validations=("unit",),
                runtime_requirements={
                    "permissions": ":danger-full-access",
                    "approval_policy": "never",
                },
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _draft(
        self,
        *,
        parent_revision_id: str | None = None,
        suffix: str = "",
        two_items: bool = True,
    ) -> PlanDraft:
        items = [work_item("foundation", title_suffix=suffix)]
        if two_items:
            items.append(
                work_item(
                    "integration",
                    dependencies=("foundation",),
                    title_suffix=suffix,
                )
            )
        return PlanDraft(
            project_id=self.project_id,
            request_summary=f"합성 요청{suffix}",
            parent_revision_id=parent_revision_id,
            work_items=tuple(items),
        )

    def _activate(self, draft: PlanDraft) -> str:
        revision_id = self.core.create_plan_draft(draft)
        self.core.activate_plan(
            revision_id,
            expected_digest=draft.canonical_digest,
            source=ActivationSource.CLI,
        )
        return revision_id

    def _complete_first(self) -> tuple[str, str, str]:
        draft = self._draft()
        revision_id = self._activate(draft)
        first = self.core.ready_work_items(self.project_id)[0]
        reservation = self.core.reserve_attempt(
            first["work_item_id"],
            thread_request={"title": "foundation", "cwd": str(self.workspace)},
        )
        self.core.begin_intent(reservation.intent_id)
        instruction_sources = (
            str(self.workspace / "AGENTS.md"),
            str(self.reference),
        )
        binding_id = self.core.record_thread_binding(
            reservation.intent_id,
            RuntimeBindingReceipt(
                thread_id="thread-r2-1",
                cwd=str(self.workspace),
                instruction_sources=instruction_sources,
                runtime_receipt={"session_id": "session-r2-1"},
            ),
        )
        turn_intent_id = self.core.reserve_turn_intent(
            reservation.attempt_id,
            turn_request={"prompt_digest": "sha256:synthetic"},
        )
        self.core.begin_intent(turn_intent_id)
        self.core.record_turn_binding(
            turn_intent_id,
            RuntimeBindingReceipt(
                thread_id="thread-r2-1",
                turn_id="turn-r2-1",
                cwd=str(self.workspace),
                instruction_sources=instruction_sources,
                runtime_receipt={"status": "inProgress"},
            ),
        )
        self.core.mark_worker_finished(
            reservation.attempt_id,
            summary="합성 Worker 결과가 도착했다.",
        )
        self.core.record_validation_result(
            reservation.attempt_id,
            ValidationResultInput(
                criterion_id="foundation.tests",
                check_type="command",
                status=ValidationStatus.PASSED,
                summary="합성 단위 검사가 통과했다.",
                evidence={"command": "test-foundation", "exit_code": 0},
            ),
        )
        self.assertFalse(self.core.complete_attempt(reservation.attempt_id))
        return revision_id, reservation.attempt_id, binding_id

    def test_new_schema_contains_core_only(self) -> None:
        tables = set(self.ledger.table_names())
        self.assertTrue(REQUIRED_CORE_TABLES <= tables)
        self.assertFalse(FORBIDDEN_LEGACY_TABLES & tables)
        self.assertEqual(("ok",), self.ledger.integrity_check())

    def test_new_core_constructor_and_activation_need_no_authority(self) -> None:
        constructor = inspect.signature(FlowMarshalCore.__init__)
        self.assertEqual(("self", "ledger"), tuple(constructor.parameters))
        activation = inspect.signature(FlowMarshalCore.activate_plan)
        self.assertEqual(
            ("self", "revision_id", "expected_digest", "source"),
            tuple(activation.parameters),
        )

        draft = self._draft()
        revision_id = self.core.create_plan_draft(draft)
        with self.assertRaises(CoreDomainError):
            self.core.activate_plan(
                revision_id,
                expected_digest="sha256:wrong",
                source=ActivationSource.UI,
            )
        result = self.core.activate_plan(
            revision_id,
            expected_digest=draft.canonical_digest,
            source=ActivationSource.UI,
        )
        self.assertEqual("ui", result.activation_source)
        self.assertEqual(draft.canonical_digest, result.canonical_digest)
        self.assertTrue(
            self.core.activate_plan(
                revision_id,
                expected_digest=draft.canonical_digest,
                source=ActivationSource.UI,
            ).idempotent
        )
        with self.ledger.raw_connection() as connection:
            row = connection.execute(
                "SELECT activation_source FROM plan_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT payload_json FROM history_events "
                "WHERE event_type = 'plan.activated'"
            ).fetchall()
        self.assertEqual("ui", row["activation_source"])
        self.assertEqual(1, len(events))
        self.assertIn(draft.canonical_digest, events[0]["payload_json"])

    def test_core_import_is_clean_and_legacy_api_has_explicit_compat_boundary(self) -> None:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import flowmarshal.core; "
                "blocked={'flowmarshal.application','flowmarshal.adapters.authority',"
                "'flowmarshal.adapters.sqlite'}; "
                "seen=sorted(blocked.intersection(sys.modules)); "
                "raise SystemExit('legacy-loaded:'+','.join(seen) if seen else 0)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(0, probe.returncode, probe.stderr)

        from flowmarshal.application import FlowMarshalService
        from flowmarshal.compat.gate0b import (
            LegacyGate0BLedger,
            LegacyGate0BService,
        )
        from flowmarshal.adapters.sqlite import SQLiteLedger

        self.assertIs(FlowMarshalService, LegacyGate0BService)
        self.assertIs(SQLiteLedger, LegacyGate0BLedger)

    def test_ready_is_derived_and_lifecycle_preserves_assignment_and_binding(self) -> None:
        revision_id, attempt_id, binding_id = self._complete_first()
        ready = self.core.ready_work_items(self.project_id)
        self.assertEqual(["integration"], [item["client_ref"] for item in ready])
        listed = self.core.list_work_items(self.project_id)
        self.assertEqual(
            ["completed", "ready"],
            [item["effective_status"] for item in listed],
        )
        with self.ledger.raw_connection() as connection:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            binding = connection.execute(
                "SELECT * FROM runtime_bindings WHERE id = ?", (binding_id,)
            ).fetchone()
            revision = connection.execute(
                "SELECT status FROM plan_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        self.assertEqual("completed", attempt["status"])
        self.assertEqual("catalog-balanced", attempt["model_id"])
        self.assertEqual("medium", attempt["reasoning_effort"])
        self.assertEqual("catalog-strong", attempt["validation_model_id"])
        self.assertEqual("high", attempt["validation_reasoning_effort"])
        self.assertEqual("thread-r2-1", binding["thread_id"])
        self.assertEqual("turn-r2-1", binding["turn_id"])
        self.assertIn("AGENTS.md", binding["instruction_sources_json"])
        self.assertEqual("active", revision["status"])
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_only_one_attempt_can_be_reserved_and_reserved_intent_is_cancelled(self) -> None:
        self._activate(self._draft())
        first = self.core.ready_work_items(self.project_id)[0]
        reservation = self.core.reserve_attempt(
            first["work_item_id"], thread_request={"title": "one"}
        )
        with self.assertRaises(CoreDomainError):
            self.core.reserve_attempt(
                first["work_item_id"], thread_request={"title": "duplicate"}
            )
        self.core.finish_attempt(
            reservation.attempt_id,
            failure_class="worker_error",
            summary="실행 전 실패",
            disposition=AttemptStatus.RETRYABLE,
        )
        with self.ledger.raw_connection() as connection:
            intent = connection.execute(
                "SELECT status FROM runtime_action_intents WHERE id = ?",
                (reservation.intent_id,),
            ).fetchone()
        self.assertEqual("cancelled", intent["status"])
        retried = self.core.reserve_attempt(
            first["work_item_id"],
            thread_request={"title": "retry"},
            retry=True,
        )
        self.assertEqual(2, retried.attempt_no)

    def test_revision_supersede_keeps_old_work_attempt_and_history(self) -> None:
        first_draft = self._draft(two_items=False)
        first_revision = self._activate(first_draft)
        old_work = self.core.ready_work_items(self.project_id)[0]
        reservation = self.core.reserve_attempt(
            old_work["work_item_id"], thread_request={"title": "old"}
        )
        self.core.finish_attempt(
            reservation.attempt_id,
            failure_class="plan_change",
            summary="새 revision으로 대체한다.",
            disposition=AttemptStatus.FAILED,
        )
        second_draft = self._draft(
            parent_revision_id=first_revision,
            suffix=" v2",
            two_items=False,
        )
        second_revision = self._activate(second_draft)

        with self.ledger.raw_connection() as connection:
            old_revision = connection.execute(
                "SELECT status FROM plan_revisions WHERE id = ?", (first_revision,)
            ).fetchone()
            old_work_row = connection.execute(
                "SELECT stored_status FROM work_items WHERE id = ?",
                (old_work["work_item_id"],),
            ).fetchone()
            old_attempt = connection.execute(
                "SELECT status FROM attempts WHERE id = ?", (reservation.attempt_id,)
            ).fetchone()
            current = connection.execute(
                "SELECT active_revision_id FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()
        self.assertEqual("superseded", old_revision["status"])
        self.assertEqual("superseded", old_work_row["stored_status"])
        self.assertEqual("failed", old_attempt["status"])
        self.assertEqual(second_revision, current["active_revision_id"])
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_append_only_and_plan_snapshot_triggers_reject_mutation(self) -> None:
        _, attempt_id, _ = self._complete_first()
        with self.ledger.raw_connection(readonly=False) as connection:
            history_id = connection.execute(
                "SELECT id FROM history_events ORDER BY sequence LIMIT 1"
            ).fetchone()["id"]
            evidence_id = connection.execute(
                "SELECT id FROM evidence_records WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()["id"]
            work_id = connection.execute(
                "SELECT work_item_id FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()["work_item_id"]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CORE_HISTORY_APPEND_ONLY"):
                connection.execute(
                    "UPDATE history_events SET event_type = 'tampered' WHERE id = ?",
                    (history_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CORE_EVIDENCE_APPEND_ONLY"):
                connection.execute(
                    "DELETE FROM evidence_records WHERE id = ?", (evidence_id,)
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "CORE_WORK_ITEM_DEFINITION_IMMUTABLE"
            ):
                connection.execute(
                    "UPDATE work_items SET objective = 'tampered' WHERE id = ?", (work_id,)
                )
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_crash_window_becomes_unknown_without_duplicate_attempt(self) -> None:
        self._activate(self._draft())
        first = self.core.ready_work_items(self.project_id)[0]
        reservation = self.core.reserve_attempt(
            first["work_item_id"], thread_request={"title": "crash"}
        )
        self.core.begin_intent(reservation.intent_id)
        self.assertEqual(
            (reservation.intent_id,), self.core.mark_executing_intents_unknown()
        )
        self.assertEqual((), self.core.mark_executing_intents_unknown())
        with self.ledger.raw_connection() as connection:
            project = connection.execute(
                "SELECT run_state, recovery_reason FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT status FROM attempts WHERE id = ?", (reservation.attempt_id,)
            ).fetchone()
            intent = connection.execute(
                "SELECT status FROM runtime_action_intents WHERE id = ?",
                (reservation.intent_id,),
            ).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE work_item_id = ?",
                (first["work_item_id"],),
            ).fetchone()[0]
        self.assertEqual(("recovery_required", "EXTERNAL_EFFECT_UNKNOWN"), tuple(project))
        self.assertEqual("external_unknown", attempt["status"])
        self.assertEqual("unknown", intent["status"])
        self.assertEqual(1, count)
        with self.assertRaises(CoreDomainError):
            self.core.reserve_attempt(
                first["work_item_id"],
                thread_request={"title": "unsafe-duplicate"},
            )

    def test_foreign_gate_database_is_rejected_without_mutation(self) -> None:
        foreign = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(foreign)
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_revision', '1')"
        )
        connection.execute("CREATE TABLE authority_uses (id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        before_bytes = foreign.read_bytes()
        before_names = sorted(path.name for path in foreign.parent.iterdir())

        with self.assertRaises(CoreDomainError):
            SQLiteCoreLedger(foreign).initialize()

        self.assertEqual(
            hashlib.sha256(before_bytes).hexdigest(),
            hashlib.sha256(foreign.read_bytes()).hexdigest(),
        )
        self.assertEqual(before_names, sorted(path.name for path in foreign.parent.iterdir()))

    def test_r2_smoke_preserves_historical_trees(self) -> None:
        gate0b = self.root / "spikes" / "gate0b" / "artifacts"
        gate0c = self.root / "spikes" / "gate0c" / "artifacts"
        gate0b.mkdir(parents=True)
        gate0c.mkdir(parents=True)
        old_database = gate0b / "historical.db"
        old_receipt = gate0c / "historical-receipt.json"
        old_database.write_bytes(b"historical-gate0b")
        old_receipt.write_bytes(b'{"historical":true}\n')
        before = (old_database.read_bytes(), old_receipt.read_bytes())

        result = run_r2_smoke(
            project_root=self.root,
            output_dir=self.root / "r2-artifact",
            run_id="r2-unit-smoke",
        )

        self.assertEqual("GO", result["decision"])
        self.assertIsNone(result["error"])
        self.assertEqual(before, (old_database.read_bytes(), old_receipt.read_bytes()))
        self.assertEqual(
            result["receipts"]["historical_before"],
            result["receipts"]["historical_after"],
        )
        self.assertEqual(
            [],
            result["checks"]["reduced_core_schema"]["evidence"][
                "forbidden_tables_present"
            ],
        )


if __name__ == "__main__":
    unittest.main()
