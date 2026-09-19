from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.context import (
    AdditionalContextRequest,
    ContextNeed,
    ProjectMapper,
    resolve_additional_context_request,
)
from flowmarshal.engine.domain import (
    CandidateStatus,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    PlanContractRevision,
    PlanSkeletonCandidate,
    RepairAction,
    RecoveryAssessment,
    RevisionStatus,
    RunOnceAction,
    ThreadBinding,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.execution import ExecutionProposalAdapter
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.planning import (
    ExpandedPlanEvaluation,
    plan_gate,
    plan_review_evidence_catalog,
)
from flowmarshal.engine.recovery import (
    FAILURE_REPAIR_ACTIONS,
    TRANSIENT_LOCAL_CODES,
    EvidenceFirstFailureClassifier,
    FailureDiagnosis,
    FailureSignal,
)
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests.test_engine_qualification import qualification_inventory
from tests.engine_helpers import clean_review


ROOT = Path(__file__).resolve().parents[1]


class EvidenceFirstClassifierTests(unittest.TestCase):
    def test_explicit_codes_and_transport_precede_terminal_semantics(self) -> None:
        classifier = EvidenceFirstFailureClassifier()
        cases = (
            ({"error_code": "CONTEXT_REQUIRED"}, FailureClass.CONTEXT),
            ({"code": "PERMISSION_POLICY_MISMATCH"}, FailureClass.ENVIRONMENT),
            ({"transport_error": {"message": "socket closed"}}, FailureClass.EXTERNAL_UNKNOWN),
            ({"effect_status": "unknown"}, FailureClass.EXTERNAL_UNKNOWN),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                diagnosis = classifier.classify(FailureSignal(
                    terminal_status="failed",
                    final_response="worker failed",
                    provider_payload=payload,
                ))
                self.assertEqual(expected, diagnosis.failure_class)
                self.assertNotEqual(FailureClass.IMPLEMENTATION, diagnosis.failure_class)

    def test_failed_terminal_without_direct_evidence_stays_unclassified(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response="작업을 마치지 못했습니다.",
            provider_payload={"thread_id": "thread"},
        ))
        self.assertIsNone(diagnosis.failure_class)
        self.assertEqual("unclassified", diagnosis.source)

    def test_model_reported_code_is_diagnostic_only(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response="RATE_LIMITED: 모델이 추측한 제한",
            provider_payload={"thread_id": "thread"},
        ))
        self.assertIsNone(diagnosis.failure_class)
        self.assertEqual("unclassified", diagnosis.source)
        self.assertEqual(("RATE_LIMITED",), diagnosis.model_reported_codes)

    def test_model_reported_korean_failure_is_not_direct_evidence(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response="구현 실패",
            provider_payload={"thread_id": "thread"},
        ))
        self.assertIsNone(diagnosis.failure_class)
        self.assertEqual("unclassified", diagnosis.source)

    def test_three_code_sources_are_stored_separately(self) -> None:
        """provider code·local engine code·model 자칭 code를 각각 다른 필드에 남긴다."""

        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response="IMPLEMENTATION_ERROR: 모델이 자칭한 원인",
            provider_payload={"thread_id": "thread", "error_code": "RATE_LIMITED"},
            local_engine_codes=("STALE_EXECUTION_INPUT",),
        ))
        self.assertEqual("RATE_LIMITED", diagnosis.provider_error_code)
        self.assertIsNone(diagnosis.local_engine_code)
        self.assertEqual(("IMPLEMENTATION_ERROR",), diagnosis.model_reported_codes)
        self.assertEqual(FailureClass.ENVIRONMENT, diagnosis.failure_class)

    def test_local_engine_code_is_attributed_to_the_engine_not_the_provider(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response=None,
            provider_payload={"thread_id": "thread"},
            local_engine_codes=("STALE_EXECUTION_INPUT",),
        ))
        self.assertEqual("STALE_EXECUTION_INPUT", diagnosis.local_engine_code)
        self.assertIsNone(diagnosis.provider_error_code)
        self.assertEqual(FailureClass.CONTEXT, diagnosis.failure_class)

    def test_model_reported_code_alone_selects_no_repair_action(self) -> None:
        """모델 자칭 code만 있으면 retry·replan·effect recovery를 고르지 않는다."""

        for reported in (
            "IMPLEMENTATION_ERROR", "TASK_CONTRACT_INVALID", "EXTERNAL_EFFECT_UNKNOWN",
        ):
            with self.subTest(reported=reported):
                diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
                    terminal_status="failed",
                    final_response=f"{reported}: 모델이 적은 진단",
                    provider_payload={"thread_id": "thread"},
                ))
                self.assertIsNone(diagnosis.failure_class)
                self.assertIsNone(diagnosis.repair_action)
                self.assertIsNone(diagnosis.provider_error_code)
                self.assertIsNone(diagnosis.local_engine_code)
                self.assertEqual((reported,), diagnosis.model_reported_codes)

    def test_context_environment_transport_and_unknown_are_not_implementation(self) -> None:
        """C2: 네 종류의 비구현 실패가 각각 implementation으로 오분류되지 않는다."""

        classifier = EvidenceFirstFailureClassifier()
        context = classifier.classify(FailureSignal(
            terminal_status="failed",
            final_response="구현 실패",
            provider_payload={"error_code": "CONTEXT_REQUIRED"},
        ))
        self.assertEqual(FailureClass.CONTEXT, context.failure_class)
        self.assertNotEqual(FailureClass.IMPLEMENTATION, context.failure_class)

        environment = classifier.classify(FailureSignal(
            terminal_status="failed",
            final_response="테스트가 실패했습니다",
            provider_payload={"error_code": "PERMISSION_POLICY_MISMATCH"},
        ))
        self.assertEqual(FailureClass.ENVIRONMENT, environment.failure_class)
        self.assertNotEqual(FailureClass.IMPLEMENTATION, environment.failure_class)

        transport = classifier.classify(FailureSignal(
            terminal_status="failed",
            final_response="assertion failed",
            provider_payload={"rpc_error": {"message": "stream closed"}},
        ))
        self.assertEqual(FailureClass.EXTERNAL_UNKNOWN, transport.failure_class)
        self.assertEqual("transport", transport.source)
        self.assertNotEqual(FailureClass.IMPLEMENTATION, transport.failure_class)

        unknown = classifier.classify(FailureSignal(
            terminal_status="failed",
            final_response="테스트가 깨졌습니다",
            provider_payload={"thread_id": "thread"},
            evidence_documents=({
                "kind": "test",
                "observation": json.dumps({"passed": True, "exit_code": 0}),
            },),
        ))
        self.assertIsNone(unknown.failure_class)
        self.assertEqual("unclassified", unknown.source)
        self.assertNotEqual(FailureClass.IMPLEMENTATION, unknown.failure_class)


class FailureRoutingTableTests(unittest.TestCase):
    def test_every_failure_class_routes_to_one_deterministic_action(self) -> None:
        """여덟 분류 전부가 한 표에서 정확히 하나의 RepairAction으로 결정된다."""

        self.assertEqual(set(FailureClass), set(FAILURE_REPAIR_ACTIONS))
        for failure_class in FailureClass:
            with self.subTest(failure_class=failure_class):
                action = FAILURE_REPAIR_ACTIONS[failure_class]
                self.assertIsInstance(action, RepairAction)
                self.assertIs(action, EngineService.repair_action_for(failure_class))
                self.assertIs(
                    action, EvidenceFirstFailureClassifier._ACTION[failure_class]
                )
        self.assertIs(FAILURE_REPAIR_ACTIONS, EvidenceFirstFailureClassifier._ACTION)

    def test_transient_marking_only_covers_retryable_environment_codes(self) -> None:
        classifier = EvidenceFirstFailureClassifier()
        for code in TRANSIENT_LOCAL_CODES:
            with self.subTest(code=code):
                diagnosis = classifier.classify(FailureSignal(
                    terminal_status="failed",
                    final_response=None,
                    provider_payload={"error_code": code},
                ))
                self.assertEqual(FailureClass.ENVIRONMENT, diagnosis.failure_class)
                self.assertTrue(diagnosis.transient)
        for code in ("PERMISSION_POLICY_MISMATCH", "EXECUTABLE_NOT_FOUND", "ENVIRONMENT_ERROR"):
            with self.subTest(code=code):
                diagnosis = classifier.classify(FailureSignal(
                    terminal_status="failed",
                    final_response=None,
                    provider_payload={"error_code": code},
                ))
                self.assertEqual(FailureClass.ENVIRONMENT, diagnosis.failure_class)
                self.assertFalse(diagnosis.transient)


class AutomaticRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.inventory = qualification_inventory()
        self.roles = default_role_configuration(ROOT)

    def prepared(self, name="work"):
        base = self.base / name
        base.mkdir()
        workspace, _ = _copy_fixture(ROOT, base)
        prepared = _prepare(
            workspace=workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=self.roles,
        )
        return prepared, FakeCodexRuntime(self.inventory)

    def _failed_attempt(self, prepared, runtime, dispatcher, response):
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
            ).fetchone()
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        error_code = response.partition(":")[0] if ":" in response else None
        runtime.fail(binding.thread_id, response=response, error_code=error_code)
        observed = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, observed.action)
        return dispatched

    def _finish_runtime_job_tick(self, dispatcher, project_id):
        """job 예약/관측/소비를 서로 다른 scheduler tick으로 진행한다."""

        outcome = dispatcher.run_once(project_id)
        for _ in range(4):
            if outcome.action not in {
                RunOnceAction.DISPATCHED,
                RunOnceAction.OBSERVED,
            }:
                return outcome
            outcome = dispatcher.run_once(project_id)
        self.fail("runtime job이 bounded tick 안에서 terminal 소비로 수렴하지 않았습니다.")

    def test_fault_injection_repairs_without_manual_assessment_and_revalidates(self) -> None:
        prepared, runtime = self.prepared(name="automatic-repair")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        first = self._failed_attempt(
            prepared, runtime, dispatcher, "IMPLEMENTATION_ERROR: injected fault"
        )

        recovered = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, recovered.action)
        with prepared.service.ledger.read() as connection:
            assessment = connection.execute(
                "SELECT payload_json FROM recovery_assessments"
            ).fetchone()
            job = connection.execute(
                "SELECT kind,status FROM runtime_jobs WHERE kind='recovery'"
            ).fetchone()
        self.assertEqual(first.attempt_id, json.loads(assessment["payload_json"])["attempt_id"])
        self.assertEqual(("recovery", "consumed"), (job["kind"], job["status"]))

        second = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, second.action)
        with prepared.service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (second.attempt_id,)
            ).fetchone()["binding_json"])
        (prepared.workspace / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n",
            encoding="utf-8",
        )
        runtime.complete(binding.thread_id, response="repair complete")
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)

    def test_repeated_failure_stops_at_task_limit_with_fresh_evidence_each_time(self) -> None:
        prepared, runtime = self.prepared(name="recovery-limit")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        self._failed_attempt(prepared, runtime, dispatcher, "IMPLEMENTATION_ERROR: same fault")
        self.assertEqual(
            RunOnceAction.RECOVERED,
            self._finish_runtime_job_tick(dispatcher, prepared.project_id).action,
        )

        for ordinal in (2, 3):
            dispatched = dispatcher.run_once(prepared.project_id)
            with prepared.service.ledger.read() as connection:
                binding = ThreadBinding.model_validate_json(connection.execute(
                    "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
                ).fetchone()["binding_json"])
            runtime.fail(
                binding.thread_id,
                response="IMPLEMENTATION_ERROR: same fault",
                error_code="IMPLEMENTATION_ERROR",
            )
            dispatcher.run_once(prepared.project_id)
            outcome = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
            if ordinal == 2:
                self.assertEqual(RunOnceAction.RECOVERED, outcome.action)
            else:
                self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
                self.assertEqual("SAME_FAILURE_RECOVERY_LIMIT", outcome.blocker_code)

    def test_context_failure_recovers_into_a_new_execution_spec_revision(self) -> None:
        """로컬 Context 실패는 같은 Task 의미의 새 ExecutionSpec 준비로 자동 복구한다."""

        prepared, runtime = self.prepared(name="context-recovery")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        failed = self._failed_attempt(
            prepared, runtime, dispatcher, "CONTEXT_REQUIRED: 필요한 본문이 선택되지 않았습니다"
        )

        recovered = self._finish_runtime_job_tick(dispatcher, prepared.project_id)

        self.assertEqual(RunOnceAction.RECOVERED, recovered.action)
        with prepared.service.ledger.read() as connection:
            assessment = json.loads(connection.execute(
                "SELECT payload_json FROM recovery_assessments"
            ).fetchone()["payload_json"])
            task_status = connection.execute(
                "SELECT status FROM task_contracts WHERE id=?", (prepared.task_id,)
            ).fetchone()["status"]
            events = [row["event_type"] for row in connection.execute(
                "SELECT event_type FROM history_events WHERE entity_id=? ORDER BY sequence",
                (prepared.task_id,),
            ).fetchall()]
        self.assertEqual(FailureClass.CONTEXT.value, assessment["failure_class"])
        self.assertEqual(RepairAction.EXECUTION_SPEC_REVISION.value, assessment["action"])
        self.assertEqual(failed.attempt_id, assessment["attempt_id"])
        self.assertEqual("ready", task_status)
        self.assertIn("task.execution_spec_recovery_enabled", events)

        redispatched = dispatcher.run_once(
            prepared.project_id, proposal=prepared.proposal
        )
        self.assertEqual(RunOnceAction.MATERIALIZED, redispatched.action)

    def test_transient_local_code_is_retried_but_other_environment_failures_stop(self) -> None:
        """직접 관측한 일시 code만 제한 재시도하고 나머지 환경 실패는 typed stop이다."""

        prepared, runtime = self.prepared(name="transient-retry")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        self._failed_attempt(
            prepared, runtime, dispatcher, "RATE_LIMITED: 계정 제한을 관측했습니다"
        )

        recovered = self._finish_runtime_job_tick(dispatcher, prepared.project_id)

        self.assertEqual(RunOnceAction.RECOVERED, recovered.action)
        with prepared.service.ledger.read() as connection:
            assessment = json.loads(connection.execute(
                "SELECT payload_json FROM recovery_assessments"
            ).fetchone()["payload_json"])
        self.assertEqual(FailureClass.ENVIRONMENT.value, assessment["failure_class"])
        self.assertEqual(RepairAction.CONTINUE.value, assessment["action"])
        self.assertEqual(0, assessment["same_failure_replan_count"])
        self.assertEqual(0, assessment["goal_replan_count"])
        self.assertEqual(RunOnceAction.DISPATCHED, dispatcher.run_once(prepared.project_id).action)

        other, other_runtime = self.prepared(name="environment-stop")
        other_dispatcher = EngineDispatcher(other.service, other_runtime)
        self._failed_attempt(
            other, other_runtime, other_dispatcher,
            "PERMISSION_POLICY_MISMATCH: sandbox 정책이 다릅니다",
        )
        blocked = other_dispatcher.run_once(other.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
        self.assertEqual("ENVIRONMENT_RECOVERY_REQUIRED", blocked.blocker_code)
        self.assertEqual(FailureClass.ENVIRONMENT, blocked.failure_class)
        with other.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments"
            ).fetchone()[0])

    def test_transient_retry_after_the_worker_wrote_its_target_prepares_a_new_spec(self) -> None:
        """일시 실패 code의 CONTINUE 재시도도 Worker가 바꾼 쓰기 target이면 새 Execution Spec으로 준비한다."""

        prepared, runtime = self.prepared(name="transient-retry-after-write")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
            ).fetchone()["binding_json"])
        target = prepared.workspace / "app.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# partial worker edit\n", encoding="utf-8")
        runtime.fail(binding.thread_id, response="RATE_LIMITED: 계정 제한을 관측했습니다", error_code="RATE_LIMITED")
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)

        recovered = self._finish_runtime_job_tick(dispatcher, prepared.project_id)

        self.assertEqual(RunOnceAction.RECOVERED, recovered.action, recovered)
        self.assertIn("새 ExecutionSpec", recovered.detail)
        with prepared.service.ledger.read() as connection:
            assessment = json.loads(connection.execute(
                "SELECT payload_json FROM recovery_assessments"
            ).fetchone()["payload_json"])
            status = connection.execute(
                "SELECT status FROM task_contracts WHERE id=?", (dispatched.task_id,)
            ).fetchone()["status"]
            retry = json.loads(connection.execute(
                "SELECT payload_json FROM history_events WHERE event_type='task.retry_enabled'"
            ).fetchone()["payload_json"])
        self.assertEqual(RepairAction.CONTINUE.value, assessment["action"])
        self.assertEqual("ready", status)
        self.assertTrue(retry["execution_spec_refresh"])

    def test_evidence_poor_failure_stops_typed_without_selecting_a_repair(self) -> None:
        """근거 없는 failed terminal은 자동 복구 대신 typed stop으로 끝난다."""

        prepared, runtime = self.prepared(name="unclassified-stop")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
            ).fetchone()["binding_json"])
        runtime.fail(binding.thread_id, response="IMPLEMENTATION_ERROR: 모델이 자칭한 원인")
        dispatcher.run_once(prepared.project_id)

        blocked = dispatcher.run_once(prepared.project_id)

        self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
        self.assertEqual("RECOVERY_DIAGNOSIS_REQUIRED", blocked.blocker_code)
        self.assertIsNone(blocked.suggested_repair_action)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE event_type IN "
                "('task.retry_enabled','task.execution_spec_recovery_enabled')"
            ).fetchone()[0])

    def test_repeat_without_new_evidence_is_blocked_by_the_ledger(self) -> None:
        """C3: 첫 복구 뒤 새 evidence 없는 반복만 차단하고 새 근거는 통과시킨다."""

        prepared, runtime = self.prepared(name="no-new-evidence")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        first = self._failed_attempt(
            prepared, runtime, dispatcher, "IMPLEMENTATION_ERROR: injected fault"
        )
        self.assertEqual(
            RunOnceAction.RECOVERED,
            self._finish_runtime_job_tick(dispatcher, prepared.project_id).action,
        )

        stale = FailureDiagnosis(
            failure_class=FailureClass.IMPLEMENTATION,
            repair_action=RepairAction.TASK_REPAIR,
            error_code="TEST_FAILED",
            evidence_ids=(),
            rationale="원인·새 근거 없이 같은 복구를 다시 요청합니다.",
            source="explicit_code",
        )
        blocked = dispatcher._automatic_recovery(
            project_id=prepared.project_id,
            task_id=prepared.task_id,
            attempt_id=first.attempt_id,
            diagnosis=stale,
            evidence_documents=(),
        )
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
        self.assertEqual("NEW_RECOVERY_EVIDENCE_REQUIRED", blocked.blocker_code)
        self.assertEqual(RepairAction.TASK_REPAIR, blocked.suggested_repair_action)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments"
            ).fetchone()[0])

        fresh = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=prepared.project_id,
            task_id=prepared.task_id,
            attempt_id=first.attempt_id,
            kind=EvidenceKind.TEST,
            source_ref="fixture:fresh-after-recovery",
            observation=json.dumps({"passed": False, "label": "fresh"}),
            content_digest=sha256_digest({"label": "fresh"}),
            observed_at=utc_now(),
        )
        prepared.service.record_evidence(fresh)
        self.assertIsNone(dispatcher._recovery_limit_blocker(
            project_id=prepared.project_id,
            task_id=prepared.task_id,
            attempt_id=first.attempt_id,
            diagnosis=stale.model_copy(update={"evidence_ids": (fresh.evidence_id,)}),
            validation_result_id=None,
        ))

    def test_scope_expansion_is_asked_instead_of_automatically_replanned(self) -> None:
        prepared, runtime = self.prepared(name="scope-expansion")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        self._failed_attempt(
            prepared, runtime, dispatcher,
            "SCOPE_EXPANSION_REQUIRED: another project is required",
        )
        blocked = dispatcher.run_once(prepared.project_id)
        self.assertEqual("AUTHORIZATION_EXPANSION_REQUIRED", blocked.blocker_code)
        self.assertEqual(FailureClass.REQUIREMENT_CHANGE, blocked.failure_class)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments"
            ).fetchone()[0])

    def test_subgraph_replan_is_reviewed_activated_and_keeps_checkpoints(self) -> None:
        prepared, runtime = self.prepared(name="subgraph-replan")
        service = prepared.service
        with service.ledger.read() as connection:
            old_plan = PlanContractRevision.model_validate_json(connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id=?",
                (prepared.plan_revision_id,),
            ).fetchone()["payload_json"])
            skeleton = PlanSkeletonCandidate.model_validate_json(connection.execute(
                "SELECT payload_json FROM skeleton_candidates WHERE candidate_digest=?",
                (old_plan.definition.source_skeleton_digest,),
            ).fetchone()["payload_json"])
            project_map = service.load_current_project_map(prepared.project_id)
            goal = service.load_active_goal(prepared.project_id)
            state = service.load_current_state(
                prepared.project_id, goal.definition_digest
            )
        replacement_task = old_plan.definition.tasks[0].model_copy(
            update={"task_id": new_id("task")}
        )
        coverage = tuple(
            item.model_copy(update={
                "task_ids": tuple(
                    replacement_task.task_id if task_id == prepared.task_id else task_id
                    for task_id in item.task_ids
                )
            })
            for item in old_plan.definition.goal_coverage
        )
        definition = old_plan.definition.model_copy(update={
            "tasks": (replacement_task,),
            "goal_coverage": coverage,
        })
        replacement = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=old_plan.plan_id,
            revision_no=old_plan.revision_no + 1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=old_plan.plan_revision_id,
            created_at=utc_now(),
        )
        deterministic = plan_gate(
            replacement,
            source=skeleton,
            goal=goal,
            state=state,
            project_map=project_map,
        )
        catalog = plan_review_evidence_catalog(
            replacement, goal, state, project_map
        )
        review = clean_review(
            replacement.activation_digest,
            role="independent_recovery_reviewer",
            evidence_catalog=catalog,
        )
        decision = derive_candidate_decision(
            candidate_digest=replacement.activation_digest,
            findings=deterministic + review.findings,
            ratings=review.ratings if not deterministic else None,
        )
        self.assertEqual(CandidateStatus.ADMISSIBLE, decision.status)
        evaluation = ExpandedPlanEvaluation(
            plan=replacement,
            deterministic_findings=deterministic,
            semantic_submissions=(review,),
            decision=decision,
        )

        class Provider:
            def replan(self, **_kwargs):
                return evaluation

        dispatcher = EngineDispatcher(
            service, runtime, recovery_provider=Provider()
        )
        self._failed_attempt(
            prepared,
            runtime,
            dispatcher,
            "TASK_CONTRACT_INVALID: injected contract defect",
        )
        assessed = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, assessed.action)
        activated = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, activated.action)
        with service.ledger.read() as connection:
            project = connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id=?",
                (prepared.project_id,),
            ).fetchone()
            jobs = connection.execute(
                "SELECT kind,status FROM runtime_jobs WHERE kind IN ('recovery','replanning') "
                "ORDER BY created_at,rowid"
            ).fetchall()
            reviews = connection.execute(
                "SELECT reviewer_role FROM candidate_reviews WHERE artifact_kind='plan' "
                "AND artifact_digest=?",
                (replacement.activation_digest,),
            ).fetchall()
        self.assertEqual(replacement.plan_revision_id, project["active_plan_revision_id"])
        self.assertEqual(
            [("recovery", "consumed"), ("replanning", "consumed")],
            [(row["kind"], row["status"]) for row in jobs],
        )
        self.assertEqual(
            ["independent_recovery_reviewer"],
            [row["reviewer_role"] for row in reviews],
        )

    def test_goal_wide_replan_limit_and_new_evidence_are_ledger_derived(self) -> None:
        prepared, _runtime = self.prepared(name="goal-replan-limit")
        service = prepared.service
        service.compile_execution_spec(prepared.proposal, inventory=self.inventory)
        attempt = service.reserve_attempt(task_id=prepared.task_id)

        def evidence(label: str) -> str:
            item = EvidenceRecord(
                evidence_id=new_id("evidence"),
                project_id=prepared.project_id,
                task_id=prepared.task_id,
                attempt_id=attempt.attempt_id,
                kind=EvidenceKind.TEST,
                source_ref=f"fixture:{label}",
                observation=json.dumps({"passed": False, "label": label}),
                content_digest=sha256_digest({"label": label}),
                observed_at=utc_now(),
            )
            service.record_evidence(item)
            return item.evidence_id

        first_evidence = evidence("first")
        service.record_recovery_assessment(
            prepared.project_id,
            RecoveryAssessment(
                assessment_id=new_id("recovery_assessment"),
                attempt_id=attempt.attempt_id,
                failure_class=FailureClass.TASK_CONTRACT,
                action=RepairAction.SUBGRAPH_REPLAN,
                rationale="첫 재계획",
                failure_fingerprint=sha256_digest("same-failure"),
                new_evidence_ids=(first_evidence,),
                same_failure_replan_count=1,
                goal_replan_count=1,
            ),
        )
        with self.assertRaisesRegex(EngineServiceError, "새 evidence"):
            service.record_recovery_assessment(
                prepared.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale="같은 evidence로 반복",
                    failure_fingerprint=sha256_digest("same-failure"),
                    new_evidence_ids=(),
                    same_failure_replan_count=2,
                    goal_replan_count=2,
                ),
            )

        for ordinal in range(2, 6):
            service.record_recovery_assessment(
                prepared.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale=f"독립 실패 {ordinal}",
                    failure_fingerprint=sha256_digest(f"failure-{ordinal}"),
                    new_evidence_ids=(evidence(f"fresh-{ordinal}"),),
                    same_failure_replan_count=1,
                    goal_replan_count=ordinal,
                ),
            )
        with self.assertRaisesRegex(EngineServiceError, "Goal 전체 재계획 한도 5회"):
            service.record_recovery_assessment(
                prepared.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale="여섯 번째 독립 실패",
                    failure_fingerprint=sha256_digest("failure-6"),
                    new_evidence_ids=(evidence("fresh-6"),),
                    same_failure_replan_count=1,
                    goal_replan_count=6,
                ),
            )


class FailureEvidenceBoundingTests(unittest.TestCase):
    """실패 terminal evidence는 한도 안의 유효한 JSON이어야 하고 복구 단계가 재파싱에 의존하지 않는다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inventory = qualification_inventory()

    def _failed_evidence(self, response: str):
        base = self.root / "bounded"
        base.mkdir()
        workspace, _ = _copy_fixture(ROOT, base)
        prepared = _prepare(
            workspace=workspace, state_root=base / "state", inventory=self.inventory,
            roles=default_role_configuration(ROOT),
        )
        runtime = FakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
            ).fetchone()["binding_json"])
        error_code = response.partition(":")[0] if ":" in response else None
        runtime.fail(binding.thread_id, response=response, error_code=error_code)
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)
        with prepared.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT observation, content_digest FROM evidence_records "
                "WHERE attempt_id=? AND kind='external_observation'",
                (dispatched.attempt_id,),
            ).fetchall()
        self.assertEqual(1, len(rows))
        return dispatcher, dispatched.attempt_id, rows[0]

    def test_failed_terminal_evidence_is_valid_json_within_limit_and_digest_bound(self) -> None:
        """operation_trace가 붙은 payload도 잘린 문자열이 아니라 한도 안의 유효 JSON으로 남는다."""

        dispatcher, attempt_id, row = self._failed_evidence("RATE_LIMITED: 계정 제한을 관측했습니다")
        self.assertLessEqual(len(row["observation"]), 10_000)
        stored = json.loads(row["observation"])
        self.assertEqual(sha256_digest(stored), row["content_digest"])
        self.assertEqual("failed", stored["terminal_status"])
        projection = stored["failure_diagnosis"]
        self.assertEqual("local_derived", projection["provenance"])
        self.assertEqual(FailureClass.ENVIRONMENT.value, projection["failure_class"])
        self.assertEqual("RATE_LIMITED", projection["provider_error_code"])
        self.assertIsNone(projection["local_engine_code"])
        self.assertTrue(projection["transient"])
        payload = stored["provider_payload"]
        self.assertEqual("RATE_LIMITED", payload["error_code"])
        if "operation_trace" not in payload:
            # 줄인 경우에는 명시 표식과 trace digest가 남아야 한다.
            self.assertTrue(
                payload.get("operation_trace_omitted") or payload.get("provider_payload_projected")
            )
            self.assertIn("operation_trace_digest", payload)

        _, documents = dispatcher._failure_evidence(attempt_id)
        observed = dispatcher._terminal_failure_diagnosis(documents)
        self.assertIsNotNone(observed)
        self.assertEqual(FailureClass.ENVIRONMENT, observed.failure_class)
        self.assertEqual("RATE_LIMITED", observed.provider_error_code)
        self.assertTrue(observed.transient)

    def test_bounded_document_never_stores_a_truncated_json_string(self) -> None:
        huge_trace = {"operations": [{"id": f"op{i}", "body": "x" * 200} for i in range(200)]}
        payload = {
            "thread_id": "thread", "turn_id": "turn", "error_code": "RATE_LIMITED",
            "operation_trace": huge_trace, "operation_trace_digest": sha256_digest(huge_trace),
            "nested": {"deep": ["y" * 50]},
        }
        document = {"terminal_status": "failed", "final_response": "z" * 20_000, "provider_payload": payload}

        bounded = EngineDispatcher._bounded_failure_document(document)

        encoded = json.dumps(bounded, ensure_ascii=False, sort_keys=True)
        self.assertLessEqual(len(encoded), 10_000)
        self.assertEqual(bounded, json.loads(encoded))
        reduced = bounded["provider_payload"]
        self.assertNotIn("operation_trace", reduced)
        self.assertEqual("RATE_LIMITED", reduced["error_code"])
        self.assertEqual(payload["operation_trace_digest"], reduced["operation_trace_digest"])
        self.assertTrue(reduced["provider_payload_projected"])
        self.assertEqual(sha256_digest(payload), reduced["provider_payload_digest"])
        self.assertNotIn("nested", reduced)
        self.assertTrue(bounded["final_response_truncated"])
        self.assertTrue(bounded["final_response"].startswith("z"))
        self.assertLess(len(bounded["final_response"]), 20_000)

        small = {"terminal_status": "failed", "final_response": "짧음", "provider_payload": {"error_code": "X_Y"}}
        self.assertIs(small, EngineDispatcher._bounded_failure_document(small))

    def test_legacy_document_without_projection_is_reclassified_from_payload(self) -> None:
        """projection이 없는 이전 형식 evidence는 보존된 provider_payload를 재관측한다."""

        dispatcher = EngineDispatcher.__new__(EngineDispatcher)
        dispatcher.failure_classifier = EvidenceFirstFailureClassifier()
        legacy = {"kind": "external_observation", "observation": json.dumps({
            "terminal_status": "failed", "final_response": "실패",
            "provider_payload": {"error_code": "CONTEXT_REQUIRED"},
        })}
        truncated = {"kind": "external_observation", "observation": "{\"terminal_status\": \"fai"}

        observed = dispatcher._terminal_failure_diagnosis((truncated, legacy))

        self.assertIsNotNone(observed)
        self.assertEqual(FailureClass.CONTEXT, observed.failure_class)
        self.assertEqual("CONTEXT_REQUIRED", observed.provider_error_code)
        self.assertFalse(observed.transient)
        self.assertIsNone(dispatcher._terminal_failure_diagnosis((truncated,)))


class AutomaticContextResolutionTests(unittest.TestCase):
    def test_project_map_searches_beyond_initial_sample_and_reinvokes_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
            (workspace / "app.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
            (workspace / "test_app.py").write_text("def test_add(): pass\n", encoding="utf-8")
            for index in range(15):
                (workspace / f"sample_{index:02}.txt").write_text("sample", encoding="utf-8")
            hidden = workspace / "z_hidden.py"
            hidden.write_text("def hidden_selector():\n    return 42\n", encoding="utf-8")
            inventory = qualification_inventory()
            roles = default_role_configuration(ROOT)
            prepared = _prepare(
                workspace=workspace,
                state_root=Path(temp) / "state",
                inventory=inventory,
                roles=roles,
            )
            proposal = prepared.proposal.model_dump(mode="json")
            for step in proposal["validation_steps"]:
                step.pop("method")
                step.pop("required_evidence_kinds")
            response = {"proposal": proposal, "context_request": None}
            request = {
                "proposal": None,
                "context_request": {
                    "task_id": prepared.task_id,
                    "missing_needs": [{
                        "need_id": "hidden",
                        "description": "hidden_selector 구현 본문",
                        "path_hints": [],
                        "symbol_hints": ["hidden_selector"],
                        "tag_hints": [],
                        "required": True,
                    }],
                    "reason": "초기 관측 sample에 본문이 없습니다.",
                },
            }
            runner = ScriptedStructuredRoleRunner({
                "execution_preparation": [request, response]
            })
            adapter = ExecutionProposalAdapter(prepared.service, runner, roles)

            result = adapter.prepare_task(
                project_id=prepared.project_id,
                task_id=prepared.task_id,
                inventory=inventory,
            )

            self.assertIsNotNone(result.proposal)
            self.assertEqual(2, len(runner.calls))
            additional = runner.calls[1].payload["additional_context"]
            self.assertEqual("z_hidden.py", additional[0]["source_ref"])
            self.assertIn("hidden_selector", additional[0]["content"])

    def test_preference_request_is_not_resolved_from_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "choice.txt").write_text("blue", encoding="utf-8")
            project_map = ProjectMapper().build(
                project_id="project_" + "1" * 32,
                root=root,
                revision_no=1,
            )
            request = AdditionalContextRequest(
                task_id="task_" + "2" * 32,
                missing_needs=(ContextNeed(
                    need_id="preference",
                    description="색상 선택",
                    path_hints=("choice.txt",),
                ),),
                reason="사용자 선호가 필요합니다.",
            )
            resolution = resolve_additional_context_request(
                project_map=project_map, request=request
            )
            self.assertEqual((), resolution.resolved)
            self.assertEqual(request, resolution.unresolved_request)


if __name__ == "__main__":
    unittest.main()
