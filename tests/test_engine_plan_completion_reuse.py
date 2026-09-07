from __future__ import annotations

import json
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from flowmarshal.engine.domain import ThreadBinding, ValidationResult, ValidationStatus, new_id, utc_now
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.cli import build_parser, _cmd_goal_authorize
from tests.test_engine_ledger_service import EngineServiceFixture
from tests import test_engine_goal_authorization as authorization_tests


class EnginePlanCompletionReuseTests(EngineServiceFixture):
    internal_revision_evaluation = authorization_tests.EngineGoalAuthorizationTests.internal_revision_evaluation
    register_internal_revision = authorization_tests.EngineGoalAuthorizationTests.register_internal_revision

    def finish_worker_and_validate(self, *, mutate=False):
        self.activate()
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        runtime = FakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(self.service, runtime)
        dispatched = dispatcher.run_once(self.project_id)
        with self.ledger.read() as connection:
            row = connection.execute("SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)).fetchone()
            binding = ThreadBinding.model_validate_json(row["binding_json"])
        if mutate:
            (self.root / "app.py").write_text("value = 2\n", encoding="utf-8")
        runtime.complete(binding.thread_id)
        dispatcher.run_once(self.project_id)
        dispatcher.run_once(self.project_id)
        # 다음 tick의 Goal 판정은 실행하지 않고 Task 완료까지만 관측한다.
        with self.ledger.read() as connection:
            status = connection.execute("SELECT status FROM task_contracts WHERE id = ?", (self.task.task_id,)).fetchone()[0]
        if status == "validating":
            self.service.complete_task(self.task.task_id)
        with self.ledger.read() as connection:
            self.assertEqual("completed", connection.execute("SELECT status FROM task_contracts WHERE id = ?", (self.task.task_id,)).fetchone()[0])
        # 정상 실행의 완료 후 강제 State 재관측을 포함한다.
        self.map, self.state = self.service.reobserve_project(self.project_id, force_state_revision=True)
        return dispatched.attempt_id

    def source_rows(self):
        with self.ledger.read() as connection:
            return {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("attempts", "runtime_intents", "runtime_receipts", "evidence_records", "validation_results")}

    def status_of(self, task_id):
        with self.ledger.read() as connection:
            return connection.execute("SELECT status FROM task_contracts WHERE id = ?", (task_id,)).fetchone()[0]

    def test_completed_evidence_reused_without_moving_attempt_or_fabricating_receipts(self):
        attempt_id = self.finish_worker_and_validate()
        before = self.source_rows()
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("completed", self.status_of(task.task_id))
        self.assertEqual(before, self.source_rows())
        with self.ledger.read() as connection:
            reuse = connection.execute("SELECT * FROM task_completion_reuse WHERE task_id = ?", (task.task_id,)).fetchone()
            self.assertEqual(self.task.task_id, reuse["source_task_id"])
            evidence = self.service.task_evidence_rows(connection, task.task_id)
            self.assertEqual(set(json.loads(reuse["evidence_ids_json"])), {row["id"] for row in evidence})
            results = self.service.effective_task_validation_results(connection, task.task_id)
            self.assertTrue(results)
            self.assertTrue(all(row["status"] == "pass" for row in results))
            self.assertEqual(attempt_id, connection.execute("SELECT id FROM attempts").fetchone()[0])
        self.assertTrue(self.ledger.verify_history(self.project_id))
        dispatcher = EngineDispatcher(self.service, FakeCodexRuntime(self.inventory))
        # 재사용 Task의 원본 validation이 새 Plan의 Goal coverage에도 들어간다.
        with self.ledger.read() as connection:
            from flowmarshal.engine.domain import ValidationResult, ValidationStatus
            results = self.service.effective_task_validation_results(connection, task.task_id)
        integration = revision.definition.integration_validations[0]
        self.service.record_validation(project_id=self.project_id, plan_revision_id=revision.plan_revision_id,
            result=ValidationResult(validation_result_id=new_id("validation_result"), validation_id=integration.validation_id,
                status=ValidationStatus.PASS, evidence_ids=tuple(json.loads(results[0]["payload_json"])["evidence_ids"]),
                rationale="독립 통합 검증 합성 관측", evaluated_at=utc_now()))
        outcome = dispatcher.run_once(self.project_id)
        self.assertEqual("completed", outcome.action.value)

    def test_cli_goal_authorization_activates_without_plan_id_or_digest(self):
        args = build_parser().parse_args(["goal", "authorize", "--project-id", self.project_id])
        output = io.StringIO()
        with patch("flowmarshal.engine.cli._service", return_value=self.service), redirect_stdout(output):
            _cmd_goal_authorize(args)
        result = json.loads(output.getvalue())
        self.assertTrue(result["activation_id"])
        self.assertEqual(self.project_id, result["authorization"]["project_id"])
        self.assertEqual("ready", self.status_of(self.task.task_id))

    def test_mutating_worker_completion_reused_after_normal_state_reobservation(self):
        self.finish_worker_and_validate(mutate=True)
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("completed", self.status_of(task.task_id))

    def test_pending_task_is_replaced_without_completion_reuse(self):
        self.activate()
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("superseded", self.status_of(self.task.task_id))
        self.assertEqual("ready", self.status_of(task.task_id))

    def test_repair_after_rejected_intermediate_revision_activates_in_same_authorization(self):
        from flowmarshal.engine.domain import ReviewFinding, FindingSeverity, GateName
        self.activate()
        rejected, _ = self.register_internal_revision(review_findings=(ReviewFinding(
            finding_code="PLAN_VERIFICATION_INCOMPLETE", gate=GateName.VERIFICATION,
            severity=FindingSeverity.ERROR, summary="추가 검사 근거가 필요하다.",
            evidence_refs=("artifact:plan_contract",), affected_task_refs=(self.task.task_ref,), remediable=True),))
        repaired, _, evaluation = self.internal_revision_evaluation()
        self.assertEqual(rejected.plan_revision_id, repaired.supersedes_plan_revision_id)
        self.service.register_authorized_plan_revision(evaluation)
        with self.ledger.read() as connection:
            self.assertEqual(repaired.plan_revision_id, connection.execute("SELECT active_plan_revision_id FROM projects").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM goal_authorizations").fetchone()[0])

    def test_changed_file_invalidates_reuse_but_preserves_original_completion(self):
        self.finish_worker_and_validate()
        before = self.source_rows()
        (self.root / "app.py").write_text("value = 2\n", encoding="utf-8")
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("ready", self.status_of(task.task_id))
        self.assertEqual("completed", self.status_of(self.task.task_id))
        self.assertEqual(before, self.source_rows())

    def test_new_file_invalidates_observed_project_fingerprint(self):
        self.finish_worker_and_validate()
        (self.root / "new.py").write_text("value = 2\n", encoding="utf-8")
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("ready", self.status_of(task.task_id))

    def test_changed_semantic_state_fact_cannot_hide_behind_target_prefix(self):
        from flowmarshal.engine.domain import StateFact
        from flowmarshal.canonical import sha256_digest
        self.finish_worker_and_validate()
        fact = StateFact(fact_id="fact_target_custom", predicate="사용자 환경 조건이 변경됨", value=False,
                         source_ref="environment", evidence_digest=sha256_digest(False))
        self.state = self.state.model_copy(update={"snapshot_id": new_id("snapshot"), "version": self.state.version + 1,
            "facts": (*self.state.facts, fact), "observed_at": utc_now()})
        self.service.record_state_snapshot(self.state)
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("ready", self.status_of(task.task_id))

    def test_validation_meaning_change_cannot_reuse_completion(self):
        self.finish_worker_and_validate()
        changed = self.task.model_copy(update={"task_id": new_id("task"), "validations": (
            self.task.validations[0].model_copy(update={"statement": "추가 경계 조건까지 검증한다."}),)})
        revision, _ = self.register_internal_revision(definition_updates={
            "tasks": (changed,), "goal_coverage": tuple(item.model_copy(update={"task_ids": (changed.task_id,)})
                                                       for item in self.plan.definition.goal_coverage)})
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("ready", self.status_of(changed.task_id))

    def test_latest_failed_validation_prevents_reuse(self):
        self.finish_worker_and_validate()
        with self.ledger.read() as connection:
            prior = self.service.effective_task_validation_results(connection, self.task.task_id)[-1]
            evidence_ids = tuple(json.loads(prior["payload_json"])["evidence_ids"])
        result = ValidationResult(validation_result_id=new_id("validation_result"),
                                  validation_id=self.task.validations[0].validation_id, task_id=self.task.task_id,
                                  status=ValidationStatus.FAIL, evidence_ids=evidence_ids,
                                  rationale="추가 관측에서 실패함", evaluated_at=utc_now())
        self.service.record_validation(project_id=self.project_id, plan_revision_id=self.plan.plan_revision_id, result=result)
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("ready", self.status_of(task.task_id))


if __name__ == "__main__":
    unittest.main()
