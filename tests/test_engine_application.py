from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from flowmarshal.canonical import canonical_json
from flowmarshal.engine.application import EngineApplication, summarize_usage_records
from flowmarshal.engine.domain import (
    BudgetStage,
    BudgetUsageRecord,
    GoalContractRevision,
    GoalVerdict,
    GoalVerdictStatus,
    RevisionStatus,
    ValidationStatus,
    CriterionVerdict,
    new_id,
    utc_now,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.read_models import (
    EntityRef,
    HistoryCursor,
    ProviderCallExpectation,
    ReadPresentation,
)
from flowmarshal.engine.service import EngineService
from tests.engine_helpers import (
    clean_review, goal, inventory, plan, profile, project_map, skeleton, state,
)
from flowmarshal.engine.planning import CandidateEvaluation, ExpandedPlanEvaluation, plan_review_evidence_catalog, skeleton_review_evidence_catalog
from flowmarshal.engine.domain import CandidateDecision, CandidateStatus


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


class EngineApplicationTests(unittest.TestCase):
    def test_recovery_query_exposes_exact_validation_failure_without_mutation(self):
        from tests.test_engine_task_validation_recovery import TaskValidationRecoveryTests

        fixture = TaskValidationRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        attempt, failed, _evidence, _time = fixture.successful_worker_then_failed_validation()
        before = fixture.service.ledger.verify_history(fixture.prepared.project_id)
        application = EngineApplication(fixture.service)
        first = application.recovery_status(fixture.prepared.project_id)
        second = application.recovery_status(fixture.prepared.project_id)
        self.assertTrue(before)
        self.assertEqual(first, second)
        self.assertEqual("TASK_VALIDATION_RECOVERY_REQUIRED", first.error_code)
        self.assertEqual(failed.validation_result_id, first.task_validation_recovery.validation_result_id)
        self.assertEqual(attempt.attempt_id, first.task_validation_recovery.attempt_id)
        self.assertEqual(failed.evidence_ids, first.task_validation_recovery.evidence_ids)
        self.assertIn(failed.validation_result_id, {item.entity_id for item in first.entity_refs})
        self.assertEqual({"goal_revision", "plan_revision"},
                         {item.entity_type for item in first.entity_refs if item.digest is not None})
        self.assertGreater(first.history_cursor.sequence, 0)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "project"
        root.mkdir()
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.service = EngineService(SQLiteEngineLedger(Path(self.temp.name) / "engine.sqlite3"))
        self.service.initialize()
        self.project_id = self.service.create_project(name="조회", root=root)
        self.profile = profile(self.project_id)
        self.service.register_profile(self.profile)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.service.register_goal(self.goal)
        self.map = project_map(self.project_id, root)
        self.service.record_project_map(self.map)
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.service.record_state_snapshot(self.state)
        self.inventory = inventory()
        self.skeleton = skeleton(self.goal, self.state)
        self.plan, self.task, self.decision = plan(
            self.project_id, self.goal, self.state, self.map.revision_digest, self.skeleton, self.inventory,
        )
        self.service.record_skeleton_evaluation(CandidateEvaluation(
            candidate=self.skeleton,
            semantic_submission=clean_review(
                self.plan.definition.source_skeleton_digest, role="skeleton_reviewer",
                evidence_catalog=skeleton_review_evidence_catalog(self.skeleton, self.goal, self.state, self.map),
            ),
            decision=CandidateDecision(
                candidate_digest=self.plan.definition.source_skeleton_digest,
                status=CandidateStatus.ADMISSIBLE, fitness_score=100, weakest_dimension="engineering",
            ),
        ))
        self.service.register_plan_evaluation(ExpandedPlanEvaluation(
            plan=self.plan,
            semantic_submissions=(clean_review(
                self.plan.activation_digest, role="plan_reviewer",
                evidence_catalog=plan_review_evidence_catalog(self.plan, self.goal, self.state, self.map),
            ),),
            decision=self.decision,
        ))
        self.application = EngineApplication(self.service)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def usage(self, *, digest: str, call_ref: str, receipt: str, input_tokens: int | None = 10,
              latency_ms: int | None = 20, available: bool = True) -> BudgetUsageRecord:
        return BudgetUsageRecord(
            usage_id=new_id("usage"), project_id=self.project_id, goal_contract_digest=digest,
            stage=BudgetStage.EXECUTION, logical_call_ref=call_ref, role="executor",
            call_status="succeeded", model="worker", effort="medium",
            permission_profile=":danger-full-access", approval_policy="never",
            input_digest=DIGEST_A, output_schema_digest=DIGEST_B, runner_receipt_digest=receipt,
            input_tokens=input_tokens, cached_input_tokens=0 if input_tokens is not None else None,
            output_tokens=3 if input_tokens is not None else None,
            reasoning_tokens=1 if input_tokens is not None else None,
            latency_ms=latency_ms, usage_available=available, recorded_at=utc_now(),
        )

    def test_usage_keeps_zero_distinct_from_missing_and_excludes_other_goal(self) -> None:
        zero = self.usage(digest=self.goal.definition_digest, call_ref="zero", receipt=DIGEST_A,
                          input_tokens=0, latency_ms=0)
        missing = self.usage(digest=self.goal.definition_digest, call_ref="missing", receipt=DIGEST_B,
                             input_tokens=None, latency_ms=None, available=False)
        other = self.usage(digest=DIGEST_C, call_ref="other", receipt=DIGEST_C)
        for item in (zero, missing, other):
            self.service.record_budget_usage(item)

        summary = self.application.usage_summary(self.project_id, goal_id=self.goal.goal_id)

        self.assertEqual(2, summary.logical_call_count)
        self.assertEqual(0, summary.input_tokens.known_subtotal)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(1, summary.input_tokens.incomplete_call_count)
        self.assertEqual(0, summary.latency_ms.known_subtotal)
        self.assertIsNone(summary.latency_ms.total)
        self.assertEqual({"zero", "missing"}, {item.logical_call_ref for item in summary.usage_records})

    def test_unavailable_tokens_keep_observed_latency_subtotal(self) -> None:
        unavailable = self.usage(
            digest=self.goal.definition_digest,
            call_ref="timeout-with-latency",
            receipt=DIGEST_A,
            input_tokens=None,
            latency_ms=37,
            available=False,
        )
        self.service.record_budget_usage(unavailable)

        summary = self.application.usage_summary(self.project_id, goal_id=self.goal.goal_id)

        self.assertEqual(0, summary.input_tokens.known_subtotal)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(37, summary.latency_ms.known_subtotal)
        self.assertEqual(37, summary.latency_ms.total)
        self.assertEqual(0, summary.latency_ms.incomplete_call_count)
        self.assertEqual(37, summary.by_stage[0].latency_ms.known_subtotal)

    def test_unsettled_provider_usage_keeps_its_observed_latency(self) -> None:
        usage = self.usage(
            digest=self.goal.definition_digest,
            call_ref="timeout-provider-call",
            receipt=DIGEST_A,
            input_tokens=None,
            latency_ms=41,
            available=False,
        )
        self.service.record_budget_usage(usage)
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO provider_calls (id,project_id,goal_id,goal_contract_digest,call_key,role,stage,"
                "request_digest,request_json,estimated_tokens,policy_digest,status,actual_tokens,receipt_json,usage_id,attempt_id,created_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,? ,NULL,NULL,?,NULL,?,NULL)",
                (new_id("provider_call"), self.project_id, self.goal.goal_id, self.goal.definition_digest,
                 "timeout-provider-call", "executor", "execution", DIGEST_A, "{}", 99, None,
                 "usage_unknown", usage.usage_id, utc_now().isoformat()),
            )

        summary = self.application.usage_summary(self.project_id, goal_id=self.goal.goal_id)

        self.assertEqual(0, summary.logical_call_count)
        self.assertEqual(("timeout-provider-call",), summary.provider_calls_without_usage)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(41, summary.latency_ms.known_subtotal)
        self.assertEqual(41, summary.latency_ms.total)
        self.assertEqual(0, summary.latency_ms.incomplete_call_count)
        self.assertEqual(41, summary.by_stage[0].latency_ms.known_subtotal)

    def test_provider_usage_id_on_duplicate_receipt_excludes_logical_tokens_but_keeps_latency(self) -> None:
        presentation = ReadPresentation(
            entity_refs=(EntityRef(entity_type="project", entity_id=self.project_id),),
            history_cursor=HistoryCursor(project_id=self.project_id, sequence=0),
        )
        selected_duplicate = self.usage(
            digest=self.goal.definition_digest,
            call_ref="same-provider-call",
            receipt=DIGEST_A,
            input_tokens=10,
            latency_ms=17,
        )
        linked_duplicate = self.usage(
            digest=self.goal.definition_digest,
            call_ref="same-provider-call",
            receipt=DIGEST_A,
            input_tokens=None,
            latency_ms=19,
            available=False,
        )
        # 빠른 연속 생성 시 같은 시각과 무작위 ID가 선택 순서를 바꾸지 않도록
        # 이 사례가 검증하려는 선행 기록·후행 provider 사본의 시간 관계를 고정한다.
        linked_duplicate = linked_duplicate.model_copy(update={
            "recorded_at": selected_duplicate.recorded_at + timedelta(seconds=1),
        })
        for records in ((selected_duplicate, linked_duplicate), (linked_duplicate, selected_duplicate)):
            with self.subTest(input_order=tuple(item.usage_id for item in records)):
                summary = summarize_usage_records(
                    project_id=self.project_id,
                    goal_id=self.goal.goal_id,
                    goal_revision_digests=(self.goal.definition_digest,),
                    records=records,
                    presentation=presentation,
                    provider_call_expectations=(ProviderCallExpectation(
                        provider_call_id="provider_call_duplicate",
                        call_key="same-provider-call",
                        status="usage_unknown",
                        role="executor",
                        stage="execution",
                        usage_id=linked_duplicate.usage_id,
                    ),),
                )

                self.assertEqual(0, summary.logical_call_count)
                self.assertEqual(0, summary.input_tokens.known_subtotal)
                self.assertIsNone(summary.input_tokens.total)
                self.assertEqual(17, summary.latency_ms.known_subtotal)
                self.assertEqual(17, summary.latency_ms.total)
                self.assertEqual(0, summary.latency_ms.incomplete_call_count)

    def test_unobserved_provider_call_makes_total_incomplete_without_using_reservation_actual(self) -> None:
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO provider_calls (id,project_id,goal_id,goal_contract_digest,call_key,role,stage,"
                "request_digest,request_json,estimated_tokens,policy_digest,status,actual_tokens,receipt_json,usage_id,attempt_id,created_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,? ,NULL,NULL,NULL,NULL,?,NULL)",
                (new_id("provider_call"), self.project_id, self.goal.goal_id, self.goal.definition_digest,
                 "reserved-without-usage", "executor", "execution", DIGEST_A, "{}", 99, None,
                 "usage_unknown", utc_now().isoformat()),
            )
        summary = self.application.usage_summary(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual(0, summary.input_tokens.known_subtotal)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(1, summary.input_tokens.incomplete_call_count)
        self.assertEqual(("reserved-without-usage",), summary.provider_calls_without_usage)
        self.assertEqual("execution", summary.by_stage[0].value)
        self.assertIsNone(summary.by_stage[0].input_tokens.total)

    def test_conflicting_duplicate_receipts_are_shown_and_not_summed(self) -> None:
        presentation = ReadPresentation(
            entity_refs=(EntityRef(entity_type="project", entity_id=self.project_id),),
            history_cursor=HistoryCursor(project_id=self.project_id, sequence=0),
        )
        first = self.usage(digest=self.goal.definition_digest, call_ref="same", receipt=DIGEST_A)
        duplicate = self.usage(digest=self.goal.definition_digest, call_ref="same", receipt=DIGEST_A)
        conflict = self.usage(digest=self.goal.definition_digest, call_ref="conflict", receipt=DIGEST_B)
        conflicting_second = self.usage(digest=self.goal.definition_digest, call_ref="conflict", receipt=DIGEST_C)
        summary = summarize_usage_records(
            project_id=self.project_id, goal_id=self.goal.goal_id,
            goal_revision_digests=(self.goal.definition_digest,),
            records=(first, duplicate, conflict, conflicting_second), presentation=presentation,
        )
        self.assertEqual(1, summary.logical_call_count)
        self.assertEqual(10, summary.input_tokens.known_subtotal)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(1, summary.input_tokens.incomplete_call_count)
        self.assertEqual("USAGE_RECEIPT_CONFLICT", summary.error_code)
        self.assertEqual("LOGICAL_CALL_RECEIPT_CONFLICT", summary.incomplete_reasons[0].code)
        self.assertEqual(("same",), tuple(item.logical_call_ref for item in summary.deduplicated))
        self.assertEqual(("conflict",), tuple(item.logical_call_ref for item in summary.conflicts))
        self.assertIsNone(summary.by_stage[0].input_tokens.total)

    def test_missing_expected_call_is_counted_once_and_does_not_invent_a_stage(self) -> None:
        presentation = ReadPresentation(
            entity_refs=(EntityRef(entity_type="project", entity_id=self.project_id),),
            history_cursor=HistoryCursor(project_id=self.project_id, sequence=0),
        )
        observed = self.usage(digest=self.goal.definition_digest, call_ref="observed", receipt=DIGEST_A)
        summary = summarize_usage_records(
            project_id=self.project_id, goal_id=self.goal.goal_id,
            goal_revision_digests=(self.goal.definition_digest,), records=(observed,), presentation=presentation,
            expected_logical_call_refs=("missing", "missing"),
        )
        self.assertEqual(("missing",), summary.missing_expected_logical_call_refs)
        self.assertEqual(10, summary.input_tokens.known_subtotal)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(1, summary.input_tokens.incomplete_call_count)
        self.assertEqual("USAGE_INCOMPLETE", summary.error_code)
        self.assertEqual(10, summary.by_stage[0].input_tokens.total)

    def test_reserved_provider_call_with_usage_id_is_not_a_completed_measurement(self) -> None:
        usage = self.usage(digest=self.goal.definition_digest, call_ref="early-usage", receipt=DIGEST_A)
        self.service.record_budget_usage(usage)
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO provider_calls (id,project_id,goal_id,goal_contract_digest,call_key,role,stage,"
                "request_digest,request_json,estimated_tokens,policy_digest,status,actual_tokens,receipt_json,usage_id,attempt_id,created_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,? ,NULL,NULL,?,NULL,?,NULL)",
                (new_id("provider_call"), self.project_id, self.goal.goal_id, self.goal.definition_digest,
                 "still-reserved", "executor", "execution", DIGEST_A, "{}", 99, None,
                 "reserved", usage.usage_id, utc_now().isoformat()),
            )
        summary = self.application.usage_summary(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual(0, summary.logical_call_count)
        self.assertEqual(1, summary.provider_call_count)
        self.assertEqual(0, summary.input_tokens.known_subtotal)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(1, summary.input_tokens.incomplete_call_count)
        self.assertEqual("PROVIDER_CALL_NOT_SETTLED", summary.incomplete_reasons[0].code)
        self.assertIsNone(summary.by_stage[0].input_tokens.total)

    def test_entity_refs_and_history_cursor_include_query_bindings(self) -> None:
        summary = self.application.usage_summary(self.project_id, goal_id=self.goal.goal_id)
        revision = next(item for item in summary.entity_refs if item.entity_type == "goal_revision")
        self.assertEqual(self.goal.goal_revision_id, revision.entity_id)
        self.assertEqual(self.goal.revision_no, revision.revision_no)
        self.assertEqual(self.goal.definition_digest, revision.digest)
        self.assertGreater(summary.history_cursor.sequence, 0)
        self.assertIsNotNone(summary.history_cursor.event_hash)
        self.assertIsNotNone(summary.history_cursor.created_at)

    def test_final_report_uses_verdict_goal_revision_not_active_goal(self) -> None:
        definition = self.goal.definition.model_copy(update={"observable_outcome": "수정된 evidence가 존재한다."})
        revised = self.goal.model_copy(update={
            "goal_revision_id": new_id("goal_revision"), "revision_no": 2,
            "status": RevisionStatus.READY, "supersedes_goal_revision_id": self.goal.goal_revision_id,
            "created_at": utc_now(), "definition": definition,
            "definition_digest": definition.definition_digest,
        })
        self.service.register_goal(revised)
        old_usage = self.usage(digest=self.goal.definition_digest, call_ref="old", receipt=DIGEST_A)
        new_usage = self.usage(digest=revised.definition_digest, call_ref="new", receipt=DIGEST_B)
        self.service.record_budget_usage(old_usage)
        self.service.record_budget_usage(new_usage)
        verdict = GoalVerdict(
            goal_verdict_id=new_id("goal_verdict"), goal_contract_digest=self.goal.definition_digest,
            plan_activation_digest=self.plan.activation_digest, status=GoalVerdictStatus.SATISFIED,
            criteria=(CriterionVerdict(criterion_id="ac_one", status=ValidationStatus.PASS,
                                      evidence_ids=(new_id("evidence"),), rationale="확인"),),
            integration_validation_result_ids=(new_id("validation_result"),), evaluated_at=utc_now(),
        )
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO goal_verdicts (id, project_id, plan_revision_id, goal_contract_digest, status, payload_json, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (verdict.goal_verdict_id, self.project_id, self.plan.plan_revision_id,
                 verdict.goal_contract_digest, verdict.status.value, canonical_json(verdict), verdict.evaluated_at.isoformat()),
            )

        report = self.application.final_report(self.project_id)

        self.assertEqual(self.goal.goal_revision_id, report.goal.goal_revision_id)
        self.assertEqual(self.goal.goal_id, report.usage.goal_id)
        self.assertEqual(2, report.usage.logical_call_count)
        self.assertEqual({self.goal.definition_digest, revised.definition_digest}, set(report.usage.goal_revision_digests))

    def test_model_binding_needs_observed_inventory_without_calling_provider(self) -> None:
        status = self.application.model_binding_status(self.project_id, inventory=None)
        self.assertEqual("ACTIVE_PLAN_NOT_FOUND", status.error_code)
        self.service.activate_plan(
            plan_revision_id=self.plan.plan_revision_id, activation_digest=self.plan.activation_digest, source="test",
        )
        observed = self.application.model_binding_status(self.project_id, inventory=self.inventory)
        self.assertIsNone(observed.error_code)
        self.assertTrue(all(item.supported for item in observed.bindings))


if __name__ == "__main__":
    unittest.main()
