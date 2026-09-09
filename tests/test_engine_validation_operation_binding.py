from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    DeterministicValidationObservation,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    RecoveryAssessment,
    RepairAction,
    RunOnceAction,
    ValidationResult,
    ValidationExecutionStep,
    ValidationStatus,
    new_id,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.operations import CoreOperations
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineServiceError
from flowmarshal.engine.validation_execution import run_command_validation
from tests.test_engine_qualification import qualification_inventory
from tests.test_engine_model_rebinding import ModelRebindingFixture, _inventory
from flowmarshal.engine.model_lock import ModelChoice


ROOT = Path(__file__).resolve().parents[1]


class ValidationOperationBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        workspace, _ = _copy_fixture(ROOT, base)
        self.inventory = qualification_inventory()
        self.prepared = _prepare(
            workspace=workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=default_role_configuration(ROOT),
        )
        self.service = self.prepared.service
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        self.step = self.prepared.proposal.validation_steps[0]

    def task(self):
        with self.service.ledger.read() as connection:
            return connection.execute(
                "SELECT * FROM task_contracts WHERE id = ?", (self.prepared.task_id,)
            ).fetchone()

    def successful_worker(self):
        attempt = self.service.reserve_attempt(task_id=self.prepared.task_id)
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        return attempt

    def retry_after(self, attempt_id: str, result_id: str, evidence_id: str) -> None:
        self.service.block_task_from_validation(
            task_id=self.prepared.task_id,
            detail=f"validation FAIL: {self.step.validation_id}",
        )
        assessment = RecoveryAssessment(
            assessment_id=new_id("recovery_assessment"),
            attempt_id=attempt_id,
            failure_class=FailureClass.IMPLEMENTATION,
            action=RepairAction.TASK_REPAIR,
            rationale="직접 test FAIL에 근거해 같은 Task 구현을 다시 수행한다.",
            new_evidence_ids=(evidence_id,),
            same_failure_replan_count=0,
            goal_replan_count=0,
        )
        self.service.retry_task(
            task_id=self.prepared.task_id,
            recovery_assessment=assessment,
            failed_validation_result_id=result_id,
        )

    def test_different_worker_uses_new_operation_and_binds_evidence(self):
        first = self.successful_worker()
        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, b"", b"failed"),
        ) as command:
            failed = run_command_validation(self.service, self.task(), self.step)
        self.retry_after(first.attempt_id, failed.validation_result_id, failed.evidence_ids[0])
        second = self.successful_worker()
        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"passed", b""),
        ) as command_again:
            passed = run_command_validation(self.service, self.task(), self.step)

        self.assertEqual(1, command.call_count)
        self.assertEqual(1, command_again.call_count)
        with self.service.ledger.read() as connection:
            operations = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE event_type = 'operation.completed' "
                "AND json_extract(payload_json, '$.kind') = 'validation_command'"
            ).fetchone()[0]
            evidence = connection.execute(
                "SELECT DISTINCT attempt_id FROM evidence_records WHERE id IN ("
                + ",".join("?" for _ in passed.evidence_ids)
                + ")",
                passed.evidence_ids,
            ).fetchall()
            effective = self.service.effective_task_validation_results(
                connection, self.prepared.task_id
            )
        self.assertEqual(2, operations)
        self.assertEqual([second.attempt_id], [row["attempt_id"] for row in evidence])
        self.assertEqual([passed.validation_result_id], [row["id"] for row in effective])

    def test_same_worker_restart_reuses_completed_operation(self):
        worker = self.successful_worker()
        completed = subprocess.CompletedProcess([], 0, b"passed", b"")
        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run", return_value=completed
        ) as command:
            first = run_command_validation(self.service, self.task(), self.step)
            second = run_command_validation(self.service, self.task(), self.step)

        self.assertEqual(1, command.call_count)
        with self.service.ledger.read() as connection:
            bindings = [
                json.loads(row["payload_json"])["operation_binding"]
                for row in connection.execute(
                    "SELECT payload_json FROM history_events WHERE event_type = 'validation.recorded' "
                    "AND entity_id IN (?, ?) ORDER BY sequence",
                    (first.validation_result_id, second.validation_result_id),
                )
            ]
            evidence = connection.execute(
                "SELECT DISTINCT attempt_id FROM evidence_records WHERE id IN ("
                + ",".join("?" for _ in (*first.evidence_ids, *second.evidence_ids))
                + ")",
                (*first.evidence_ids, *second.evidence_ids),
            ).fetchall()
        self.assertEqual(bindings[0], bindings[1])
        self.assertEqual(worker.attempt_id, bindings[0]["worker_attempt_id"])
        self.assertEqual([worker.attempt_id], [row["attempt_id"] for row in evidence])

    def test_same_worker_unknown_operation_is_not_executed_again(self):
        self.successful_worker()

        def fault(point: str) -> None:
            if point == "after_validation_command_intent":
                raise RuntimeError(point)

        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"passed", b""),
        ) as command:
            with self.assertRaisesRegex(RuntimeError, "after_validation_command_intent"):
                run_command_validation(
                    self.service, self.task(), self.step, fault_hook=fault
                )
            outcome = run_command_validation(self.service, self.task(), self.step)

        self.assertEqual(0, command.call_count)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", outcome.blocker_code)
        with self.service.ledger.read() as connection:
            counts = {
                event: connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type = ?", (event,)
                ).fetchone()[0]
                for event in (
                    "operation.prepared",
                    "operation.completed",
                    "operation.external_unknown",
                    "validation.recorded",
                )
            }
        self.assertEqual(1, counts["operation.prepared"])
        self.assertEqual(0, counts["operation.completed"])
        self.assertEqual(1, counts["operation.external_unknown"])
        self.assertEqual(0, counts["validation.recorded"])

    def test_tampered_operation_binding_is_rejected_before_recording(self):
        self.successful_worker()
        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"passed", b""),
        ):
            observed = run_command_validation(self.service, self.task(), self.step)
        with self.service.ledger.read() as connection:
            recorded = ValidationResult.model_validate_json(connection.execute(
                "SELECT payload_json FROM validation_results WHERE id = ?",
                (observed.validation_result_id,),
            ).fetchone()["payload_json"])
            binding = json.loads(connection.execute(
                "SELECT payload_json FROM history_events WHERE entity_id = ? "
                "AND event_type = 'validation.recorded'",
                (observed.validation_result_id,),
            ).fetchone()["payload_json"])["operation_binding"]
            before = connection.execute(
                "SELECT COUNT(*) FROM validation_results"
            ).fetchone()[0]
        tampered = binding | {
            "source_worker_execution_spec_digest": "sha256:" + "f" * 64
        }
        duplicate = recorded.model_copy(
            update={"validation_result_id": new_id("validation_result")}
        )
        with self.assertRaisesRegex(EngineServiceError, "현재 성공 Worker epoch"):
            self.service.record_validation(
                project_id=self.prepared.project_id,
                plan_revision_id=self.prepared.plan_revision_id,
                result=duplicate,
                operation_binding=tampered,
            )
        with self.service.ledger.read() as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM validation_results"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_legacy_cached_result_before_new_worker_is_excluded_and_reexecuted(self):
        first = self.successful_worker()
        fixed_time = utc_now()
        observation = DeterministicValidationObservation(
            validation_id=self.step.validation_id,
            task_id=self.prepared.task_id,
            argv=self.step.argv,
            working_directory=self.step.working_directory,
            timeout_seconds=self.step.timeout_seconds,
            expected_exit_codes=self.step.expected_exit_codes,
            actual_exit_code=1,
            timed_out=False,
            stdout="",
            stderr="old failure",
            observed_at=fixed_time,
        )
        legacy_request = {
            "plan_revision_id": self.prepared.plan_revision_id,
            "task_id": self.prepared.task_id,
            "execution_spec_digest": first.execution_spec_digest,
            "goal_binding": None,
            "step": self.step.model_dump(mode="json"),
            "artifacts": (),
        }
        response = CoreOperations(self.service).invoke(
            project_id=self.prepared.project_id,
            kind="validation_command",
            request=legacy_request,
            execute=lambda: {
                "observation": observation.model_dump(mode="json"),
                "artifacts": [],
            },
        )

        def record_cached_result():
            evidence = EvidenceRecord(
                evidence_id=new_id("evidence"),
                project_id=self.prepared.project_id,
                task_id=self.prepared.task_id,
                kind=EvidenceKind.TEST,
                source_ref=f"validation:{self.step.validation_id}:{self.step.argv[0]}",
                observation=observation.model_dump_json(),
                content_digest=sha256_digest(
                    {"kind": "test", "observation": observation}
                ),
                observed_at=fixed_time,
            )
            self.service.record_evidence(evidence)
            result = ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id=self.step.validation_id,
                task_id=self.prepared.task_id,
                status=ValidationStatus.FAIL,
                evidence_ids=(evidence.evidence_id,),
                rationale="legacy cached failure",
                evaluated_at=fixed_time,
            )
            self.service.record_validation(
                project_id=self.prepared.project_id,
                plan_revision_id=self.prepared.plan_revision_id,
                result=result,
            )
            return result, evidence

        failed, evidence = record_cached_result()
        self.retry_after(first.attempt_id, failed.validation_result_id, evidence.evidence_id)
        second = self.successful_worker()
        stale, _ = record_cached_result()
        with self.service.ledger.read() as connection:
            self.assertEqual(
                (),
                self.service.effective_task_validation_results(
                    connection, self.prepared.task_id
                ),
            )

        dispatcher = EngineDispatcher(
            self.service, FakeCodexRuntime(self.inventory)
        )
        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"passed", b""),
        ) as command, patch(
            "flowmarshal.engine.validation_execution.utc_now", return_value=fixed_time
        ):
            fresh = dispatcher.run_once(self.prepared.project_id)

        self.assertEqual(RunOnceAction.VALIDATED, fresh.action)
        self.assertEqual(1, command.call_count)
        self.assertNotEqual(stale.validation_result_id, fresh.validation_result_id)
        with self.service.ledger.read() as connection:
            effective = self.service.effective_task_validation_results(
                connection, self.prepared.task_id
            )
            preserved = connection.execute(
                "SELECT status, evaluated_at FROM validation_results WHERE id = ?",
                (stale.validation_result_id,),
            ).fetchone()
            fresh_row = connection.execute(
                "SELECT evaluated_at FROM validation_results WHERE id = ?",
                (fresh.validation_result_id,),
            ).fetchone()
        self.assertEqual([fresh.validation_result_id], [row["id"] for row in effective])
        self.assertEqual("fail", preserved["status"])
        self.assertEqual(preserved["evaluated_at"], fresh_row["evaluated_at"])


class ValidatorRebindDeterministicBindingTests(ModelRebindingFixture):
    def test_semantic_contract_rejects_deterministic_evidence_after_validator_rebind(self) -> None:
        worker, _ = self.complete_worker_with_evidence()
        rebound = self.rebinding.rebind_and_reserve(
            self.request(
                ModelChoice(model="validator_backup", effort="medium"),
                role="validator",
            ),
            _inventory("worker", "validator_backup"),
        )
        # 혼합 Task의 선행 semantic 검사 성공 뒤 deterministic 검사로 넘어간 상태다.
        self.service.finish_attempt(
            attempt_id=rebound.attempt.attempt_id,
            succeeded=True,
        )
        semantic = rebound.execution_spec.definition.validation_steps[0]
        deterministic = ValidationExecutionStep.model_validate(
            semantic.model_dump(mode="json")
            | {
                "method": "deterministic",
                "argv": ("validator-rebind-test",),
                "working_directory": str(self.root),
                "semantic_instruction": None,
                "required_evidence_kinds": ("test",),
            }
        )
        with self.ledger.read() as connection:
            task = connection.execute(
                "SELECT * FROM task_contracts WHERE id = ?", (self.task.task_id,)
            ).fetchone()
        with patch(
            "flowmarshal.engine.validation_execution.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"passed", b""),
        ), self.assertRaisesRegex(
            EngineServiceError, "INDEPENDENT_VALIDATION_ATTEMPT_REQUIRED",
        ):
            run_command_validation(self.service, task, deterministic)
        with self.ledger.read() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM validation_results WHERE task_id=?",
                (self.task.task_id,),
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
