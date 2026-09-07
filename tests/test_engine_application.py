from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.application import (
    EngineApplication,
    EngineApplicationError,
    summarize_usage_records,
)
from flowmarshal.engine.budget import BudgetManager, GoalBudgetPolicy
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
from flowmarshal.engine.roles import RoleCallReceipt, RoleCallRequest, strict_json_output_schema
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


class PreGoalUsageSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.ledger = SQLiteEngineLedger(Path(self.temp.name) / "engine.sqlite3")
        self.service = EngineService(self.ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="Goal 준비 사용량", root=self.root)
        self.profile = profile(self.project_id)
        self.service.register_profile(self.profile)
        self.manager = BudgetManager(self.service)
        self.manager.configure(
            self.project_id,
            GoalBudgetPolicy(total_tokens=1_000, call_reservation_tokens=100),
        )
        self.application = EngineApplication(self.service)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, role: str, *, key: str) -> RoleCallRequest:
        schema = strict_json_output_schema({
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        })
        return RoleCallRequest(
            role=role,
            instructions="고정 schema에 맞는 결과만 반환합니다.",
            payload={"key": key},
            output_schema=schema,
            model="test-model",
            effort="medium",
            inventory_digest=DIGEST_A,
            cwd=str(self.root),
        )

    def receipt(
        self,
        request: RoleCallRequest,
        *,
        key: str,
        input_tokens: int | None = 11,
        cached_input_tokens: int | None = 3,
        output_tokens: int | None = 4,
        reasoning_tokens: int | None = 2,
        usage_available: bool = True,
        latency_ms: int = 19,
    ) -> RoleCallReceipt:
        return RoleCallReceipt(
            call_id=key,
            role=request.role,
            status="schema_failed",
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            input_digest=request.request_digest,
            output_digest=DIGEST_B,
            output_schema_digest=sha256_digest(
                strict_json_output_schema(request.output_schema)
            ),
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_available=usage_available,
            latency_ms=latency_ms,
            recorded_at=utc_now(),
        )

    def settled_pre_goal_call(
        self,
        *,
        goal_id: str,
        key: str,
        role: str = "goal_reviewer",
        usage_available: bool = True,
        input_tokens: int | None = 11,
        output_tokens: int | None = 4,
        logical_call_ref: str | None = None,
    ) -> str:
        request = self.request(role, key=key)
        call_id = self.manager.reserve(
            project_id=self.project_id,
            goal_id=goal_id,
            goal_digest=None,
            call_key=key,
            role=role,
            request=request.model_dump(mode="json"),
        )
        receipt = self.receipt(
            request,
            key=logical_call_ref or key,
            input_tokens=input_tokens,
            cached_input_tokens=(0 if input_tokens is not None else None),
            output_tokens=output_tokens,
            reasoning_tokens=(0 if output_tokens is not None else None),
            usage_available=usage_available,
        )
        self.manager.settle(call_id, receipt)
        return call_id

    def test_pre_goal_receipt_is_typed_then_attach_keeps_the_same_total_once(self) -> None:
        pending_goal_id = new_id("goal")
        call_id = self.settled_pre_goal_call(goal_id=pending_goal_id, key="pre-goal")
        before_history = self.service.status(self.project_id)["history_count"]

        before = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)

        self.assertEqual(15, before.input_tokens.known_subtotal + before.output_tokens.known_subtotal)
        self.assertEqual(11, before.input_tokens.total)
        self.assertEqual(4, before.output_tokens.total)
        self.assertEqual(19, before.latency_ms.total)
        self.assertEqual(1, before.logical_call_count)
        self.assertEqual((), before.goal_revision_digests)
        self.assertEqual("GOAL_REVISION_NOT_CREATED", before.goal_revision_unavailable_reason)
        self.assertIn("임의로 만들지", before.next_action)
        self.assertEqual((), before.usage_records)
        self.assertEqual((call_id,), tuple(item.provider_call_id for item in before.provider_receipt_usage))
        self.assertIn(call_id, {item.entity_id for item in before.entity_refs})
        self.assertEqual(before_history, before.history_cursor.sequence)
        self.assertEqual(before_history, self.service.status(self.project_id)["history_count"])
        with self.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM budget_usage").fetchone()[0])

        pending_goal = goal(self.project_id, self.profile.definition_digest).model_copy(
            update={"goal_id": pending_goal_id}
        )
        self.service.register_goal(pending_goal)
        self.manager.attach_goal(
            self.project_id, pending_goal_id, pending_goal.definition_digest
        )

        after = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)
        self.assertEqual((11, 4, 19), (
            after.input_tokens.total, after.output_tokens.total, after.latency_ms.total,
        ))
        self.assertEqual(1, after.logical_call_count)
        self.assertEqual(1, len(after.usage_records))
        self.assertEqual((), after.provider_receipt_usage)
        self.assertIsNone(after.goal_revision_unavailable_reason)

    def test_pre_goal_projection_reads_legacy_call_settled_history(self) -> None:
        pending_goal_id = new_id("goal")
        request = self.request("goal_reviewer", key="legacy-history")
        call_id = self.manager.reserve(
            project_id=self.project_id, goal_id=pending_goal_id, goal_digest=None,
            call_key="legacy-history", role=request.role,
            request=request.model_dump(mode="json"),
        )
        receipt = self.receipt(request, key="legacy-history")
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE provider_calls SET status='settled',actual_tokens=?,receipt_json=?,completed_at=? "
                "WHERE id=?",
                (receipt.input_tokens + receipt.output_tokens, canonical_json(receipt), tx.now, call_id),
            )
            # schema revision 3의 기존 writer는 관측과 정산을 이 이벤트 하나로 기록했다.
            tx.history(self.project_id, "budget.call_settled", "provider_call", call_id, {
                "actual_tokens": receipt.input_tokens + receipt.output_tokens,
                "usage_available": True, "receipt": receipt.model_dump(mode="json"),
            })

        summary = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)
        self.assertEqual((11, 4, 19, "provider_receipt"), (
            summary.input_tokens.total, summary.output_tokens.total,
            summary.latency_ms.total, summary.provider_receipt_usage[0].projection_source,
        ))

    def test_legacy_unavailable_receipt_omitted_and_null_tokens_compare_equal(self) -> None:
        pending_goal_id = new_id("goal")
        request = self.request("goal_reviewer", key="legacy-unavailable")
        call_id = self.manager.reserve(
            project_id=self.project_id, goal_id=pending_goal_id, goal_digest=None,
            call_key="legacy-unavailable", role=request.role,
            request=request.model_dump(mode="json"),
        )
        receipt = self.receipt(
            request, key="legacy-unavailable", input_tokens=None,
            cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
            usage_available=False,
        )
        # canonical 원본은 null token을 생략하지만 구 History writer는 model_dump의
        # 명시적 null을 기록했던 실제 운영 원장 형태를 재현한다.
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE provider_calls SET status='usage_unknown',actual_tokens=NULL,receipt_json=?,completed_at=? "
                "WHERE id=?", (canonical_json(receipt), tx.now, call_id),
            )
            tx.history(self.project_id, "budget.call_settled", "provider_call", call_id, {
                "actual_tokens": None, "usage_available": False,
                "receipt": receipt.model_dump(mode="json"),
            })

        summary = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)
        self.assertEqual(1, summary.logical_call_count)
        self.assertEqual((None, 0, 19), (
            summary.input_tokens.total, summary.input_tokens.known_subtotal,
            summary.latency_ms.total,
        ))
        self.assertEqual("provider_receipt", summary.provider_receipt_usage[0].projection_source)
        self.assertFalse(summary.provider_receipt_usage[0].usage_available)
        self.assertFalse(any(
            item.code == "PROVIDER_CALL_HISTORY_BINDING_INVALID"
            for item in summary.incomplete_reasons
        ))

    def test_unknown_and_zero_are_distinct_and_goal_scope_is_explicit(self) -> None:
        first_goal = new_id("goal")
        second_goal = new_id("goal")
        self.settled_pre_goal_call(
            goal_id=first_goal, key="zero", input_tokens=0, output_tokens=0
        )
        self.settled_pre_goal_call(
            goal_id=second_goal,
            key="unknown",
            usage_available=False,
            input_tokens=None,
            output_tokens=None,
        )

        with self.assertRaisesRegex(EngineApplicationError, "GOAL_ID_REQUIRED"):
            self.application.usage_summary(self.project_id)
        zero = self.application.usage_summary(self.project_id, goal_id=first_goal)
        unknown = self.application.usage_summary(self.project_id, goal_id=second_goal)
        self.assertEqual((0, 0, 0), (
            zero.input_tokens.total, zero.output_tokens.total, zero.input_tokens.known_subtotal,
        ))
        self.assertEqual(1, zero.logical_call_count)
        self.assertEqual(0, unknown.input_tokens.known_subtotal)
        self.assertIsNone(unknown.input_tokens.total)
        self.assertEqual(1, unknown.input_tokens.incomplete_call_count)
        self.assertEqual(19, unknown.latency_ms.total)
        self.assertEqual("USAGE_INCOMPLETE", unknown.error_code)
        self.assertEqual("USAGE_UNAVAILABLE", unknown.incomplete_reasons[0].code)
        self.assertEqual(1, unknown.provider_call_count)

    def test_pre_goal_schema_recovery_keeps_latency_as_unavailable_projection(self) -> None:
        pending_goal_id = new_id("goal")
        request = self.request("goal_reviewer", key="schema-recovery")
        call_id = self.manager.reserve(
            project_id=self.project_id,
            goal_id=pending_goal_id,
            goal_digest=None,
            call_key="schema-recovery",
            role=request.role,
            request=request.model_dump(mode="json"),
        )
        self.manager.settle(
            call_id,
            self.receipt(request, key="schema-recovery").model_copy(
                update={"schema_recovery_attempts": 1}
            ),
        )

        summary = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)

        self.assertEqual((None, 19), (summary.input_tokens.total, summary.latency_ms.total))
        self.assertEqual("USAGE_UNAVAILABLE", summary.incomplete_reasons[-1].code)
        self.assertEqual((call_id,), tuple(item.provider_call_id for item in summary.provider_receipt_usage))

    def test_conflicting_provider_receipt_is_reported_without_inventing_usage(self) -> None:
        pending_goal_id = new_id("goal")
        call_id = self.settled_pre_goal_call(goal_id=pending_goal_id, key="conflict")
        with self.ledger.transaction() as tx:
            row = tx.one("SELECT receipt_json FROM provider_calls WHERE id=?", (call_id,))
            receipt = RoleCallReceipt.model_validate_json(row["receipt_json"]).model_copy(
                update={"input_digest": DIGEST_C}
            )
            tx.connection.execute(
                "UPDATE provider_calls SET receipt_json=? WHERE id=?",
                (receipt.model_dump_json(), call_id),
            )

        summary = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)

        self.assertEqual(0, summary.logical_call_count)
        self.assertEqual((), summary.provider_receipt_usage)
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(1, summary.input_tokens.incomplete_call_count)
        self.assertEqual("PROVIDER_RECEIPT_BINDING_INVALID", summary.incomplete_reasons[0].code)

    def test_conflicting_pre_goal_logical_receipts_are_not_summed(self) -> None:
        pending_goal_id = new_id("goal")
        first = self.settled_pre_goal_call(
            goal_id=pending_goal_id, key="first", logical_call_ref="same-logical-call"
        )
        second = self.settled_pre_goal_call(
            goal_id=pending_goal_id, key="second", logical_call_ref="same-logical-call"
        )

        summary = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)

        self.assertEqual(0, summary.logical_call_count)
        self.assertEqual(2, len(summary.provider_receipt_usage))
        self.assertEqual(1, len(summary.conflicts))
        self.assertEqual({first, second}, set(summary.conflicts[0].provider_call_ids))
        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual(1, summary.input_tokens.incomplete_call_count)
        self.assertEqual("USAGE_RECEIPT_CONFLICT", summary.error_code)

    def test_malformed_provider_request_keeps_a_typed_incomplete_summary(self) -> None:
        pending_goal_id = new_id("goal")
        call_id = self.settled_pre_goal_call(goal_id=pending_goal_id, key="malformed")
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE provider_calls SET request_json='{}' WHERE id=?", (call_id,)
            )

        summary = self.application.usage_summary(self.project_id, goal_id=pending_goal_id)

        self.assertIsNone(summary.input_tokens.total)
        self.assertEqual("USAGE_INCOMPLETE", summary.error_code)
        self.assertEqual("PROVIDER_RECEIPT_BINDING_INVALID", summary.incomplete_reasons[0].code)
        self.assertLessEqual(len(summary.incomplete_reasons[0].detail), 1000)


if __name__ == "__main__":
    unittest.main()
