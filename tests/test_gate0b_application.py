from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flowmarshal.adapters.authority import FileHumanControlAuthority
from flowmarshal.adapters.evidence import FileEvidenceStore
from flowmarshal.adapters.runtime import FakeAgentRuntime
from flowmarshal.adapters.sqlite import SQLiteLedger
from flowmarshal.application import FlowMarshalService
from flowmarshal.domain import (
    AccessMode,
    CompletionCriterion,
    DomainError,
    IntentKind,
    PlanDraft,
    ResourceKind,
    VerificationSpec,
    VerificationType,
    WorkItemDefinition,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)

    def now(self) -> str:
        return self.value.isoformat().replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class Gate0BApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.clock = MutableClock()
        self.ledger = SQLiteLedger(self.root / "flowmarshal.db", clock=self.clock)
        self.ledger.initialize()
        self.evidence = FileEvidenceStore(self.root / "evidence")
        self.authority = FileHumanControlAuthority(
            clock=self.clock,
            capability_path=self.root / "control" / "approval-capability",
        )
        self.authority.initialize()
        self.runtime = FakeAgentRuntime()
        self.service = FlowMarshalService(
            ledger=self.ledger,
            evidence_store=self.evidence,
            authority=self.authority,
            runtime=self.runtime,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _active_single_item_plan(
        self,
        *,
        predicate: str = "exists",
        relative_path: str = ".",
    ) -> tuple[str, str, str]:
        registered = self.service.register_project(
            name="합성 프로젝트", workspace=self.workspace
        )
        draft = PlanDraft(
            project_id=registered.project_id,
            summary="Gate 0B 합성 작업",
            work_items=(
                WorkItemDefinition(
                    client_ref="task_1",
                    goal="합성 작업을 완료한다.",
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=(
                        CompletionCriterion(
                            criterion_id="workspace_check",
                            description="workspace가 존재한다.",
                            verification=VerificationSpec(
                                type=VerificationType.WORKSPACE_PREDICATE,
                                relative_path=relative_path,
                                predicate=predicate,
                            ),
                        ),
                    ),
                    execution_profile={
                        "model_role": "balanced",
                        "reasoning_effort": "medium",
                    },
                    validation_profile={
                        "model_role": "strong",
                        "reasoning_effort": "high",
                    },
                ),
            ),
        )
        revision_id = self.service.import_candidate_plan(draft)
        approval = self.authority.issue("approve_plan", draft.content_digest)
        self.service.approve_plan(revision_id, approval)
        activation = self.authority.issue("activate_plan", draft.content_digest)
        self.service.activate_plan(revision_id, activation)
        return registered.project_id, revision_id, draft.content_digest

    def test_complete_one_work_item_end_to_end(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        outcome = self.service.run_once(project_id)

        self.assertEqual("completed", outcome.status)
        self.assertIsNotNone(outcome.thread_id)
        self.assertIsNotNone(outcome.turn_id)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(1, self.runtime.turn_calls)
        self.assertEqual(
            {"model_role": "balanced", "reasoning_effort": "medium"},
            self.runtime.last_execution_profile,
        )
        with self.ledger.raw_connection() as connection:
            attempt = connection.execute("SELECT * FROM attempts").fetchone()
            self.assertEqual("completed", attempt["status"])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM attempt_evidence").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM resource_leases WHERE released_at IS NULL").fetchone()[0])

    def test_trusted_check_exception_becomes_failed_evidence(self) -> None:
        registered = self.service.register_project(
            name="검사 예외", workspace=self.workspace
        )

        def broken_check(_workspace: Path) -> tuple[bool, dict[str, object]]:
            raise RuntimeError("합성 검사기 오류")

        self.service.validation_checks["broken"] = broken_check
        draft = PlanDraft(
            project_id=registered.project_id,
            summary="검사기 예외를 닫힌 실패로 처리한다.",
            work_items=(
                WorkItemDefinition(
                    client_ref="task_1",
                    goal="검사 예외를 검증한다.",
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=(
                        CompletionCriterion(
                            criterion_id="broken_check",
                            description="예외가 실패 evidence가 된다.",
                            verification=VerificationSpec(
                                type=VerificationType.CHECK,
                                check_id="broken",
                            ),
                        ),
                    ),
                ),
            ),
        )
        revision = self.service.import_candidate_plan(draft)
        self.service.approve_plan(
            revision, self.authority.issue("approve_plan", draft.content_digest)
        )
        self.service.activate_plan(
            revision, self.authority.issue("activate_plan", draft.content_digest)
        )

        outcome = self.service.run_once(registered.project_id)

        self.assertEqual("failed", outcome.status)
        self.assertEqual("VALIDATION_FAILED", outcome.reason_code)
        with self.ledger.raw_connection() as connection:
            check_intent = connection.execute(
                "SELECT status FROM runtime_action_intents "
                "WHERE kind = 'run_check'"
            ).fetchone()
            evidence_path = connection.execute(
                "SELECT e.storage_path FROM evidence_records e "
                "JOIN attempt_evidence ae ON ae.evidence_id = e.id "
                "WHERE ae.criterion_id = 'broken_check'"
            ).fetchone()["storage_path"]
        self.assertEqual("succeeded", check_intent["status"])
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        self.assertFalse(evidence["passed"])
        self.assertEqual(
            "TRUSTED_CHECK_EXCEPTION", evidence["details"]["error"]
        )

    def test_crash_after_create_effect_becomes_unknown_and_is_not_reissued(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_runtime_create_thread":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        self.assertEqual(1, self.runtime.create_calls)

        self.service.crash_hook = None
        recovered = self.service.startup_recover()
        self.assertEqual(1, len(recovered))
        outcome = self.service.run_once(project_id)
        self.assertEqual("halted", outcome.status)
        self.assertEqual("PROJECT_QUARANTINED", outcome.reason_code)
        self.assertEqual(1, self.runtime.create_calls)

    def test_reserved_intent_after_crash_is_continued_once(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_reservation_commit":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        self.assertEqual(0, self.runtime.create_calls)

        self.service.crash_hook = None
        self.assertEqual((), self.service.startup_recover())
        outcome = self.service.run_once(project_id)
        self.assertEqual("completed", outcome.status)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(1, self.runtime.turn_calls)

    def test_unknown_create_is_reconciled_by_explicit_binding(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_runtime_create_thread":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        unknown_id = self.service.startup_recover()[0]
        external_thread = self.runtime.thread_ids[0]
        target = self.service.recovery_target_digest(unknown_id)
        proof = self.authority.issue("recover_bind", target)

        recovered = self.service.recover_bind(unknown_id, external_thread, proof)
        self.assertEqual("RECOVERED_THREAD_BINDING", recovered.reason_code)

        self.service.crash_hook = None
        outcome = self.service.run_once(project_id)
        self.assertEqual("completed", outcome.status)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(1, self.runtime.turn_calls)

    def test_unknown_turn_is_observed_and_bound_without_duplicate_turn(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_runtime_start_turn":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        unknown_id = self.service.startup_recover()[0]

        observation = self.service.recover_observe(unknown_id)
        self.assertEqual(1, len(observation.candidate_external_ids))
        target = self.service.recovery_target_digest(unknown_id)
        proof = self.authority.issue("recover_bind", target)
        self.service.recover_bind(
            unknown_id, observation.candidate_external_ids[0], proof
        )

        self.service.crash_hook = None
        outcome = self.service.run_once(project_id)
        self.assertEqual("completed", outcome.status)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(1, self.runtime.turn_calls)

    def test_crash_after_receipt_commit_does_not_repeat_effect(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_receipt_commit_create_thread":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(0, self.runtime.turn_calls)

        self.service.crash_hook = None
        self.assertEqual((), self.service.startup_recover())
        outcome = self.service.run_once(project_id)
        self.assertEqual("completed", outcome.status)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(1, self.runtime.turn_calls)

    def test_crash_after_turn_receipt_commit_only_resumes_observation(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_receipt_commit_start_turn":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(1, self.runtime.turn_calls)

        self.service.crash_hook = None
        self.assertEqual((), self.service.startup_recover())
        outcome = self.service.run_once(project_id)
        self.assertEqual("completed", outcome.status)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(1, self.runtime.turn_calls)

    def test_explicit_interrupt_reconciles_unknown_active_turn(self) -> None:
        self.runtime.finish_turns_immediately = False
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_runtime_start_turn":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        unknown_id = self.service.startup_recover()[0]
        observed = self.service.recover_observe(unknown_id)
        self.assertTrue(observed.active_turn)
        target = self.service.recovery_target_digest(unknown_id)
        proof = self.authority.issue("recover_interrupt", target)

        outcome = self.service.recover_interrupt(
            unknown_id,
            thread_id=observed.thread_id or "",
            turn_id=observed.candidate_external_ids[0],
            proof=proof,
        )

        self.assertEqual("reconciled", outcome.status)
        self.assertEqual(1, self.runtime.interrupt_calls)
        with self.ledger.raw_connection() as connection:
            project = connection.execute(
                "SELECT state FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            attempt = connection.execute("SELECT status FROM attempts").fetchone()
        self.assertEqual("active", project["state"])
        self.assertEqual("cancelled", attempt["status"])

    def test_abandon_retains_quarantine_and_write_lease(self) -> None:
        project_id, _, _ = self._active_single_item_plan()

        def crash(point: str) -> None:
            if point == "after_runtime_create_thread":
                raise SimulatedCrash(point)

        self.service.crash_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        unknown_id = self.service.startup_recover()[0]
        target = self.service.recovery_target_digest(unknown_id)
        proof = self.authority.issue("recover_abandon", target)

        outcome = self.service.recover_abandon(unknown_id, proof)

        self.assertEqual("quarantined", outcome.status)
        with self.ledger.raw_connection() as connection:
            project = connection.execute(
                "SELECT state FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            attempt = connection.execute("SELECT status FROM attempts").fetchone()
            lease_count = connection.execute(
                "SELECT COUNT(*) FROM resource_leases WHERE released_at IS NULL"
            ).fetchone()[0]
        self.assertEqual("quarantined", project["state"])
        self.assertEqual("abandoned_external_unknown", attempt["status"])
        self.assertEqual(1, lease_count)

    def test_authority_proof_replay_and_expiry_are_rejected(self) -> None:
        registered = self.service.register_project(
            name="승인 테스트", workspace=self.workspace
        )
        draft = PlanDraft(
            project_id=registered.project_id,
            summary="승인 테스트",
            work_items=(
                WorkItemDefinition(
                    client_ref="task_1",
                    goal="테스트",
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=(
                        CompletionCriterion(
                            criterion_id="c",
                            description="존재",
                            verification=VerificationSpec(
                                type=VerificationType.ARTIFACT,
                                relative_path=".",
                                predicate="exists",
                            ),
                        ),
                    ),
                ),
            ),
        )
        revision = self.service.import_candidate_plan(draft)
        proof = self.authority.issue("approve_plan", draft.content_digest, ttl_seconds=1)
        self.service.approve_plan(revision, proof)
        with self.assertRaises(DomainError):
            with self.ledger.transaction() as transaction:
                self.service._consume_proof(  # replay 검사를 원자적으로 직접 확인한다.
                    transaction,
                    proof,
                    "approve_plan",
                    draft.content_digest,
                )

        expired = self.authority.issue("activate_plan", draft.content_digest, ttl_seconds=1)
        self.clock.advance(2)
        with self.assertRaises(DomainError):
            self.service.activate_plan(revision, expired)

    def test_access_grant_is_read_only_and_drift_halts_execution(self) -> None:
        project_id, _, _ = self._active_single_item_plan()
        self.service.crash_hook = lambda point: (_ for _ in ()).throw(SimulatedCrash(point)) if point == "after_reservation_commit" else None
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        self.service.crash_hook = None
        with self.ledger.raw_connection() as connection:
            attempt_id = connection.execute("SELECT id FROM attempts").fetchone()["id"]
        reference = self.root / "outside.log"
        reference.write_text("처음", encoding="utf-8")
        request_id = self.service.submit_access_request(
            attempt_id=attempt_id,
            exact_path=reference,
            reason="테스트 로그가 필요함",
            access_mode=AccessMode.READ,
            resource_kind=ResourceKind.REFERENCE,
        )
        digest = self.service.access_request_target_digest(request_id)
        proof = self.authority.issue("grant_access", digest)
        self.service.grant_access(request_id, proof)
        with self.ledger.raw_connection() as connection:
            grant = connection.execute("SELECT * FROM access_grants").fetchone()
            self.assertEqual("read", grant["access_mode"])

        reference.write_text("변경됨", encoding="utf-8")
        outcome = self.service.run_once(project_id)
        self.assertEqual("INPUT_DRIFT", outcome.reason_code)
        self.assertEqual(0, self.runtime.create_calls)

    def test_concurrent_run_once_reserves_only_one_effect(self) -> None:
        project_id, _, _ = self._active_single_item_plan()
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def crash_after_reservation(point: str) -> None:
            if point == "after_reservation_commit":
                barrier.wait(timeout=5)
                raise SimulatedCrash(point)

        self.service.crash_hook = crash_after_reservation

        def call() -> None:
            try:
                outcomes.append(self.service.run_once(project_id))
            except SimulatedCrash as error:
                outcomes.append(error)
            except threading.BrokenBarrierError:
                outcomes.append("barrier")

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        with self.ledger.raw_connection() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM runtime_action_intents").fetchone()[0])

    def test_dependencies_execute_in_order_and_candidate_import_is_idempotent(self) -> None:
        registered = self.service.register_project(
            name="의존성 프로젝트", workspace=self.workspace
        )
        completion = (
            CompletionCriterion(
                criterion_id="workspace",
                description="workspace 존재",
                verification=VerificationSpec(
                    type=VerificationType.WORKSPACE_PREDICATE,
                    relative_path=".",
                    predicate="exists",
                ),
            ),
        )
        draft = PlanDraft(
            project_id=registered.project_id,
            summary="순차 의존성",
            work_items=(
                WorkItemDefinition(
                    client_ref="first",
                    goal="첫 번째",
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=completion,
                ),
                WorkItemDefinition(
                    client_ref="second",
                    goal="두 번째",
                    dependencies=("first",),
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=completion,
                ),
            ),
        )
        revision = self.service.import_candidate_plan(draft)
        self.assertEqual(revision, self.service.import_candidate_plan(draft))
        self.service.approve_plan(
            revision, self.authority.issue("approve_plan", draft.content_digest)
        )
        self.service.activate_plan(
            revision, self.authority.issue("activate_plan", draft.content_digest)
        )

        first = self.service.run_once(registered.project_id)
        second = self.service.run_once(registered.project_id)

        self.assertEqual("completed", first.status)
        self.assertEqual("completed", second.status)
        with self.ledger.raw_connection() as connection:
            refs = [
                row["client_ref"]
                for row in connection.execute(
                    "SELECT wi.client_ref FROM attempts a "
                    "JOIN work_items wi ON wi.id = a.work_item_id ORDER BY a.created_at, a.rowid"
                )
            ]
        self.assertEqual(["first", "second"], refs)

    def test_gate0b_rejects_parallel_activation_but_schema_keeps_slot_shape(self) -> None:
        with self.assertRaises(DomainError):
            self.service.register_project(
                name="병렬은 아직 비활성", workspace=self.workspace, execution_slots=2
            )

    def test_protected_capability_cannot_be_registered_as_reference(self) -> None:
        with self.assertRaises(DomainError):
            self.service.register_project(
                name="보호 경로",
                workspace=self.workspace,
                references=(self.authority.capability_path,),
            )

    def test_core_state_paths_are_automatically_protected(self) -> None:
        registered = self.service.register_project(
            name="Core 보호 경로", workspace=self.workspace
        )
        with self.ledger.raw_connection() as connection:
            paths = {
                row["canonical_path"]
                for row in connection.execute(
                    "SELECT canonical_path FROM protected_paths WHERE project_id = ?",
                    (registered.project_id,),
                )
            }

        expected = {
            str(self.ledger.path.resolve()).lower(),
            str(Path(f"{self.ledger.path}-wal").resolve()).lower(),
            str(Path(f"{self.ledger.path}-shm").resolve()).lower(),
            str(Path(f"{self.ledger.path}-journal").resolve()).lower(),
            str(self.evidence.root.resolve()).lower(),
            str(self.authority.capability_path.resolve()).lower(),
        }
        self.assertTrue(expected.issubset({path.lower() for path in paths}))

    def test_workspace_cannot_overlap_core_storage(self) -> None:
        nested_workspace = self.evidence.root / "nested-workspace"
        nested_workspace.mkdir(parents=True)
        with self.assertRaises(DomainError):
            self.service.register_project(
                name="Core 저장소와 겹침", workspace=nested_workspace
            )

    def test_human_review_requires_separate_authority_proof(self) -> None:
        registered = self.service.register_project(
            name="사람 검토", workspace=self.workspace
        )
        draft = PlanDraft(
            project_id=registered.project_id,
            summary="사람 검토가 있는 계획",
            work_items=(
                WorkItemDefinition(
                    client_ref="review",
                    goal="검토 대기",
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=(
                        CompletionCriterion(
                            criterion_id="human",
                            description="사람이 결과를 승인한다.",
                            verification=VerificationSpec(
                                type=VerificationType.HUMAN_REVIEW
                            ),
                        ),
                    ),
                ),
            ),
        )
        revision = self.service.import_candidate_plan(draft)
        self.service.approve_plan(
            revision, self.authority.issue("approve_plan", draft.content_digest)
        )
        self.service.activate_plan(
            revision, self.authority.issue("activate_plan", draft.content_digest)
        )
        pending = self.service.run_once(registered.project_id)
        self.assertEqual("awaiting_human_review", pending.status)
        target = self.service.human_review_target_digest(pending.attempt_id or "")
        wrong = self.authority.issue("activate_plan", target)
        with self.assertRaises(DomainError):
            self.service.approve_human_review(pending.attempt_id or "", wrong)

        proof = self.authority.issue("approve_human_review", target)
        completed = self.service.approve_human_review(
            pending.attempt_id or "", proof
        )
        self.assertEqual("completed", completed.status)

    def test_unknown_trigger_rejects_new_normal_intent(self) -> None:
        project_id, _, _ = self._active_single_item_plan()
        self.service.crash_hook = (
            lambda point: (_ for _ in ()).throw(SimulatedCrash(point))
            if point == "after_runtime_create_thread"
            else None
        )
        with self.assertRaises(SimulatedCrash):
            self.service.run_once(project_id)
        unknown_id = self.service.startup_recover()[0]
        with self.ledger.raw_connection() as connection:
            unknown = connection.execute(
                "SELECT * FROM runtime_action_intents WHERE id = ?", (unknown_id,)
            ).fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.transaction() as transaction:
                transaction.create_intent(
                    project_id=project_id,
                    attempt_id=unknown["attempt_id"],
                    kind=IntentKind.CREATE_THREAD,
                    request={"duplicate": True},
                )


class SimulatedCrash(RuntimeError):
    pass


if __name__ == "__main__":
    unittest.main()
