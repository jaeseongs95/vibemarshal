"""G1: 자동 SUBGRAPH_REPLAN 후보의 단일 등록·typed 차단·zero-mutation 재생·status 정합.

재계획 job 결과를 소비한 run_once가 비적격 후보나 활성화 실패 후보에서 예외를 밖으로
내지 않고, 같은 후보를 다시 등록하지 않으며, status가 같은 판정을 표시하는지 공개 경로
(run_once·status·recovery_explanation)와 원장 조회로 확인한다.
"""
from __future__ import annotations

import threading
import time
import unittest

import tests.test_engine_automatic_recovery as automatic
import tests.test_engine_fm08_recovery_integration as fm08
from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.domain import (
    CandidateStatus,
    EffectContract,
    FailureClass,
    FindingSeverity,
    GateName,
    GoalOperatingPolicy,
    PlanContractRevision,
    PlanSkeletonCandidate,
    RepairAction,
    ReviewFinding,
    ReviewerSubmission,
    RevisionStatus,
    RunOnceAction,
    RuntimeJobStatus,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.planning import (
    ExpandedPlanEvaluation,
    plan_gate,
    plan_review_evidence_catalog,
)
from flowmarshal.engine.runtime import EngineDispatcher, notify_active_runtime_job_progress
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests.engine_helpers import clean_review


class _Crash(BaseException):
    """등록 직후 반환 전에 프로세스가 죽은 것을 흉내 낸다."""


class ReplanCandidateTests(unittest.TestCase):
    # 기반 TestCase를 모듈 이름으로 두면 그 테스트가 여기서 다시 수집되므로 메서드만 빌린다.
    setUp = automatic.AutomaticRecoveryIntegrationTests.setUp
    prepared = automatic.AutomaticRecoveryIntegrationTests.prepared
    _failed_attempt = automatic.AutomaticRecoveryIntegrationTests._failed_attempt
    _finish_runtime_job_tick = automatic.AutomaticRecoveryIntegrationTests._finish_runtime_job_tick

    # --- 후보 --------------------------------------------------------------

    def _evaluation(self, prepared, *, task_update=None, reviewer_finding=False):
        """RecoveryPlanProvider.replan과 같은 모양의 후보(결정적 finding이 있으면 reviewer 없음)."""

        service = prepared.service
        with service.ledger.read() as connection:
            old = PlanContractRevision.model_validate_json(connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id=?", (prepared.plan_revision_id,),
            ).fetchone()["payload_json"])
            skeleton = PlanSkeletonCandidate.model_validate_json(connection.execute(
                "SELECT payload_json FROM skeleton_candidates WHERE candidate_digest=?",
                (old.definition.source_skeleton_digest,),
            ).fetchone()["payload_json"])
        goal = service.load_active_goal(prepared.project_id)
        state = service.load_current_state(prepared.project_id, goal.definition_digest)
        project_map = service.load_current_project_map(prepared.project_id)
        task = old.definition.tasks[0].model_copy(
            update={"task_id": new_id("task"), **(task_update or {})}
        )
        coverage = tuple(
            item.model_copy(update={"task_ids": tuple(
                task.task_id if task_id == prepared.task_id else task_id
                for task_id in item.task_ids
            )})
            for item in old.definition.goal_coverage
        )
        definition = old.definition.model_copy(update={"tasks": (task,), "goal_coverage": coverage})
        plan = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=old.plan_id,
            revision_no=old.revision_no + 1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=old.plan_revision_id,
            created_at=utc_now(),
        )
        deterministic = plan_gate(plan, source=skeleton, goal=goal, state=state, project_map=project_map)
        catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        if reviewer_finding:
            review = ReviewerSubmission(
                reviewer_role="independent_recovery_reviewer",
                candidate_digest=plan.activation_digest,
                findings=(ReviewFinding(
                    finding_code="G1_REVIEW_DEFECT",
                    gate=GateName.GOAL,
                    severity=FindingSeverity.ERROR,
                    summary="G1 remediable blocking finding",
                    evidence_refs=(sorted(catalog)[0],),
                    remediable=True,
                ),),
                evidence_catalog_digest=sha256_digest(catalog),
            )
        else:
            review = clean_review(
                plan.activation_digest, role="independent_recovery_reviewer", evidence_catalog=catalog,
            )
        reviews = () if deterministic else (review,)
        findings = deterministic + tuple(item for sub in reviews for item in sub.findings)
        decision = derive_candidate_decision(
            candidate_digest=plan.activation_digest,
            findings=findings,
            ratings=None if findings else review.ratings,
        )
        return ExpandedPlanEvaluation(
            plan=plan, deterministic_findings=deterministic, semantic_submissions=reviews,
            decision=decision,
        )

    def _effect_violation(self, prepared):
        return self._evaluation(prepared, task_update={"expected_effects": (EffectContract(
            effect_id="g1-external", statement="G1 외부 서비스 배포", external=True,
        ),)})

    def _authorization_expanding(self, prepared):
        """Gate·review는 통과하지만 승인 운영 정책(max_same_failure_replans=2)을 넘는 후보."""

        with prepared.service.ledger.read() as connection:
            old = PlanContractRevision.model_validate_json(connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id=?", (prepared.plan_revision_id,),
            ).fetchone()["payload_json"])
        recovery = old.definition.tasks[0].recovery.model_copy(update={"max_same_failure_replans": 3})
        return self._evaluation(prepared, task_update={"recovery": recovery})

    # --- 구동·관측 ----------------------------------------------------------

    @staticmethod
    def _provider(evaluation, calls, gate=None):
        class Provider:
            def replan(self, **_kwargs):
                calls.append(1)
                if gate is not None:
                    gate.wait(10)
                return evaluation

        return Provider()

    def _to_replan(self, prepared, runtime, evaluation, *, gate=None):
        """실패 Attempt와 assessment checkpoint까지 진행한다. 다음 tick이 재계획을 소비한다."""

        calls: list[int] = []
        dispatcher = EngineDispatcher(
            prepared.service, runtime, recovery_provider=self._provider(evaluation, calls, gate),
        )
        self._failed_attempt(
            prepared, runtime, dispatcher, "TASK_CONTRACT_INVALID: injected contract defect",
        )
        assessed = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, assessed.action)
        return dispatcher, calls

    @staticmethod
    def _count(service, *names):
        calls = {name: 0 for name in names}
        for name in names:
            real = getattr(service, name)

            def counted(*args, _name=name, _real=real, **kwargs):
                calls[_name] += 1
                return _real(*args, **kwargs)

            setattr(service, name, counted)
        return calls

    @staticmethod
    def _ledger(service, project_id):
        """원장·history·runtime journal(job·observation) 전체 상태의 비교용 사본."""

        with service.ledger.read() as c:
            def rows(query):
                return [tuple(row) for row in c.execute(query, (project_id,))]

            return {
                "project": rows("SELECT active_plan_revision_id,run_state,updated_at FROM projects WHERE id=?"),
                "plans": rows("SELECT id,revision_no,status,activated_at FROM plan_revisions "
                              "WHERE project_id=? ORDER BY rowid"),
                "decisions": rows("SELECT id,status FROM candidate_decisions WHERE project_id=? ORDER BY rowid"),
                "reviews": rows("SELECT id FROM candidate_reviews WHERE project_id=? ORDER BY rowid"),
                "activations": rows("SELECT id FROM plan_activations WHERE project_id=? ORDER BY rowid"),
                "tasks": rows("SELECT id,status,updated_at FROM task_contracts WHERE project_id=? ORDER BY rowid"),
                "history": rows("SELECT sequence,event_type FROM history_events WHERE project_id=? ORDER BY sequence"),
                "jobs": rows("SELECT id,status,updated_at FROM runtime_jobs WHERE project_id=? ORDER BY rowid"),
                "journal": rows("SELECT id FROM runtime_job_observations WHERE project_id=? ORDER BY rowid"),
                "attempts": rows("SELECT id,status FROM attempts WHERE project_id=? ORDER BY rowid"),
                "state": rows("SELECT id,is_current FROM state_snapshots WHERE project_id=? ORDER BY rowid"),
                "authorizations": rows("SELECT id FROM goal_authorizations WHERE project_id=? ORDER BY rowid"),
            }

    @staticmethod
    def _events(service, project_id, event_type, entity_id):
        with service.ledger.read() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE project_id=? AND event_type=? AND entity_id=?",
                (project_id, event_type, entity_id),
            ).fetchone()[0]

    @staticmethod
    def _plan_status(service, plan_revision_id):
        with service.ledger.read() as connection:
            row = connection.execute(
                "SELECT status FROM plan_revisions WHERE id=?", (plan_revision_id,)
            ).fetchone()
        return None if row is None else row["status"]

    @staticmethod
    def _active_plan(service, project_id):
        with service.ledger.read() as connection:
            return connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()[0]

    def _assert_replan_blocked(self, outcome, code):
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action, outcome)
        self.assertEqual(code, outcome.blocker_code)
        self.assertEqual(FailureClass.TASK_CONTRACT, outcome.failure_class)
        self.assertEqual(RepairAction.SUBGRAPH_REPLAN, outcome.suggested_repair_action)
        self.assertTrue(outcome.checkpoint_required)

    def _assert_replays(self, prepared, dispatcher, first, *, ticks=2):
        """이후 tick은 register·활성화를 부르지 않고 원장 변화 없이 같은 판정을 재생한다."""

        service = prepared.service
        calls = self._count(
            service, "_register_plan", "register_plan_evaluation",
            "register_authorized_plan_revision", "activate_plan",
        )
        before = self._ledger(service, prepared.project_id)
        for _ in range(ticks):
            again = dispatcher.run_once(prepared.project_id)
            self.assertEqual(
                (first.action, first.blocker_code, first.detail, first.failure_class),
                (again.action, again.blocker_code, again.detail, again.failure_class),
            )
            self.assertEqual(before, self._ledger(service, prepared.project_id))
        self.assertEqual({name: 0 for name in calls}, calls)

    def _reauthorize(self, service, project_id, policy):
        """사용자 재승인(새 GoalAuthorization revision)으로 authorization binding을 바꾼다."""

        service.authorize_goal(
            project_id=project_id,
            source="g1 재승인",
            operating_policy=policy,
            capability=service._action_authority.issue_goal_authorization(
                ledger_path=service.ledger.path,
                target=service.goal_authorization_target(
                    project_id=project_id, operating_policy=policy,
                ),
            ),
        )

    def _application_status(self, prepared, runtime, dispatcher):
        application = EngineApplication(prepared.service, runtime=runtime)
        application._dispatcher = lambda: dispatcher
        return application

    # --- AC1·AC2: 비적격 후보 ------------------------------------------------

    def test_not_admissible_candidate_blocks_typed_and_replays_without_mutation(self) -> None:
        cases = {
            "blocked": (self._effect_violation, CandidateStatus.BLOCKED, "PLAN_EFFECT_POLICY_VIOLATION"),
            "needs_revision": (
                lambda prepared: self._evaluation(prepared, reviewer_finding=True),
                CandidateStatus.NEEDS_REVISION,
                "G1_REVIEW_DEFECT",
            ),
        }
        for label, (build, status, finding) in cases.items():
            with self.subTest(label):
                prepared, runtime = self.prepared(name=f"not-admissible-{label}")
                service = prepared.service
                evaluation = build(prepared)
                self.assertIs(status, evaluation.decision.status)
                self.assertIn(finding, evaluation.decision.finding_codes)
                dispatcher, provider_calls = self._to_replan(prepared, runtime, evaluation)

                first = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
                self._assert_replan_blocked(first, "REPLAN_CANDIDATE_NOT_ADMISSIBLE")
                self.assertIn(status.value, first.detail)
                self.assertIn(finding, first.detail)
                candidate = evaluation.plan.plan_revision_id
                self.assertEqual("draft", self._plan_status(service, candidate))
                self.assertEqual(prepared.plan_revision_id, self._active_plan(service, prepared.project_id))
                self.assertEqual(1, self._events(service, prepared.project_id, "plan.registered", candidate))
                self.assertEqual(0, self._events(service, prepared.project_id, "plan.activated", candidate))

                self._assert_replays(prepared, dispatcher, first)
                # 재시작한 service·dispatcher도 기존 등록을 찾아 같은 판정을 재생한다.
                restarted = EngineService(SQLiteEngineLedger(
                    service.ledger.path, artifact_root=service.ledger.artifact_root,
                ))
                before = self._ledger(service, prepared.project_id)
                again = EngineDispatcher(
                    restarted, runtime, recovery_provider=self._provider(evaluation, provider_calls),
                ).run_once(prepared.project_id)
                self.assertEqual((first.blocker_code, first.detail), (again.blocker_code, again.detail))
                self.assertEqual(before, self._ledger(service, prepared.project_id))
                self.assertEqual(1, len(provider_calls))

    # --- AC1·AC5: binding mismatch와 원자적 ensure ---------------------------

    def test_existing_row_with_a_different_binding_is_a_typed_mismatch(self) -> None:
        prepared, runtime = self.prepared(name="binding-mismatch")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        # 같은 plan_id·revision_no 자리를 다른 후보가 먼저 차지했다.
        other = self._evaluation(prepared, reviewer_finding=True)
        service.register_plan_evaluation(other)

        first = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self._assert_replan_blocked(first, "REPLAN_CANDIDATE_BINDING_MISMATCH")
        self.assertIsNone(self._plan_status(service, evaluation.plan.plan_revision_id))
        self.assertEqual(prepared.plan_revision_id, self._active_plan(service, prepared.project_id))
        self._assert_replays(prepared, dispatcher, first)

        explanation = self._application_status(prepared, runtime, dispatcher).recovery_explanation(
            prepared.project_id
        )
        self.assertEqual("user_decision_required", explanation.state)
        self.assertEqual("REPLAN_CANDIDATE_BINDING_MISMATCH", explanation.next_action.blocker_code)

    def test_ensure_is_idempotent_only_for_the_exact_binding(self) -> None:
        prepared, _runtime = self.prepared(name="ensure-binding")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        self.assertTrue(service._ensure_replan_candidate_registered(evaluation))
        before = self._ledger(service, prepared.project_id)
        self.assertFalse(service._ensure_replan_candidate_registered(evaluation))
        self.assertEqual(before, self._ledger(service, prepared.project_id))

        # 같은 plan_revision_id·plan_id·revision_no라도 decision이나 payload가 다르면 멱등이 아니다.
        other_decision = evaluation.model_copy(update={"decision": derive_candidate_decision(
            candidate_digest=evaluation.plan.activation_digest,
            findings=(ReviewFinding(
                finding_code="G1_OTHER", gate=GateName.GOAL, severity=FindingSeverity.ERROR,
                summary="다른 decision", evidence_refs=("source:goal",), remediable=True,
            ),),
            ratings=None,
        )})
        other_payload = evaluation.model_copy(update={
            "plan": evaluation.plan.model_copy(update={"created_at": utc_now()}),
        })
        for changed in (other_decision, other_payload):
            with self.assertRaisesRegex(EngineServiceError, "^REPLAN_CANDIDATE_BINDING_MISMATCH"):
                service._ensure_replan_candidate_registered(changed)
        self.assertEqual(before, self._ledger(service, prepared.project_id))

    def test_concurrent_ensure_registers_exactly_once(self) -> None:
        prepared, _runtime = self.prepared(name="ensure-concurrent")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        # 존재 확인과 등록이 한 transaction이 아니면 두 호출 모두 등록 단계에 들어와 만난다.
        rendezvous = threading.Barrier(2, timeout=0.5)
        real = service._register_plan_evaluation

        def meet(*args, **kwargs):
            try:
                rendezvous.wait()
            except threading.BrokenBarrierError:
                pass
            return real(*args, **kwargs)

        service._register_plan_evaluation = meet
        results: list[object] = []

        def call():
            try:
                results.append(service._ensure_replan_candidate_registered(evaluation))
            except BaseException as error:  # noqa: BLE001 - 동시 호출 결과 관측
                results.append(error)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([False, True], sorted(results, key=repr), results)
        candidate = evaluation.plan.plan_revision_id
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.registered", candidate))
        with service.ledger.read() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM plan_revisions WHERE plan_id=? AND revision_no=?",
                (evaluation.plan.plan_id, evaluation.plan.revision_no),
            ).fetchone()[0])

    # --- AC4: 활성화 실패 ------------------------------------------------------

    def test_stale_state_blocks_typed_and_never_activates_on_a_different_new_state(self) -> None:
        prepared, runtime = self.prepared(name="stale-preflight")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        self.assertIs(CandidateStatus.ADMISSIBLE, evaluation.decision.status)
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        service.reobserve_project(prepared.project_id, force_state_revision=True)
        activations = self._count(service, "activate_plan")

        first = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self._assert_replan_blocked(first, "PLAN_STATE_SNAPSHOT_STALE")
        self.assertEqual({"activate_plan": 0}, activations)
        self.assertEqual("ready", self._plan_status(service, evaluation.plan.plan_revision_id))
        self._assert_replays(prepared, dispatcher, first)

        # 다른 새 state로 바뀌어도 후보가 결속한 원래 snapshot이 아니므로 활성화를 시도하지 않는다.
        service.reobserve_project(prepared.project_id, force_state_revision=True)
        before = self._ledger(service, prepared.project_id)
        again = dispatcher.run_once(prepared.project_id)
        self._assert_replan_blocked(again, "PLAN_STATE_SNAPSHOT_STALE")
        self.assertEqual(before, self._ledger(service, prepared.project_id))
        self.assertEqual({"activate_plan": 0}, activations)
        self.assertEqual(prepared.plan_revision_id, self._active_plan(service, prepared.project_id))

        explanation = self._application_status(prepared, runtime, dispatcher).recovery_explanation(
            prepared.project_id
        )
        self.assertEqual("user_decision_required", explanation.state)
        self.assertEqual("PLAN_STATE_SNAPSHOT_STALE", explanation.next_action.blocker_code)

    def test_stale_state_at_the_activation_call_is_typed_and_not_retried(self) -> None:
        prepared, runtime = self.prepared(name="stale-call")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        real = service.activate_plan
        activations = []

        def race(**kwargs):
            # helper 통과 뒤 활성화 transaction 전에 다른 관측이 state를 바꾼 경우다.
            activations.append(1)
            service.reobserve_project(prepared.project_id, force_state_revision=True)
            return real(**kwargs)

        service.activate_plan = race
        first = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self._assert_replan_blocked(first, "PLAN_STATE_SNAPSHOT_STALE")
        self.assertEqual([1], activations)
        self.assertEqual("ready", self._plan_status(service, evaluation.plan.plan_revision_id))
        self.assertEqual(prepared.plan_revision_id, self._active_plan(service, prepared.project_id))
        service.activate_plan = real
        self._assert_replays(prepared, dispatcher, first)

    def test_authorization_change_blocks_until_reauthorized_then_activates_once(self) -> None:
        prepared, runtime = self.prepared(name="authorization")
        service = prepared.service
        evaluation = self._authorization_expanding(prepared)
        self.assertIs(CandidateStatus.ADMISSIBLE, evaluation.decision.status)
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        candidate = evaluation.plan.plan_revision_id
        activations = self._count(service, "activate_plan")

        first = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self._assert_replan_blocked(first, "GOAL_AUTHORIZATION_REQUIRED")
        self.assertIn("recovery.max_same_failure_replans", first.detail)
        self.assertEqual({"activate_plan": 0}, activations)
        self._assert_replays(prepared, dispatcher, first)
        explanation = self._application_status(prepared, runtime, dispatcher).recovery_explanation(
            prepared.project_id
        )
        self.assertEqual("user_decision_required", explanation.state)
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", explanation.next_action.blocker_code)

        # binding이 바뀌어 helper가 통과하면 같은 후보를 한 번 시도한다. 그 사이 승인이 다시
        # 좁아지면 같은 typed blocker로 남고 다음 tick은 활성화를 다시 부르지 않는다.
        widened = GoalOperatingPolicy(max_same_failure_replans=3)
        self._reauthorize(service, prepared.project_id, widened)
        real = service.activate_plan

        def narrowed(**kwargs):
            self._reauthorize(service, prepared.project_id, GoalOperatingPolicy())
            return real(**kwargs)

        service.activate_plan = narrowed
        failed = dispatcher.run_once(prepared.project_id)
        self._assert_replan_blocked(failed, "GOAL_AUTHORIZATION_REQUIRED")
        self.assertEqual({"activate_plan": 1}, activations)
        self.assertEqual("ready", self._plan_status(service, candidate))
        service.activate_plan = real
        self._assert_replays(prepared, dispatcher, failed)

        self._reauthorize(service, prepared.project_id, widened)
        recovered = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, recovered.action, recovered)
        self.assertEqual(candidate, self._active_plan(service, prepared.project_id))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.registered", candidate))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.activated", candidate))

    # --- AC1: 적격 경로 유지 --------------------------------------------------

    def test_admissible_candidate_is_registered_and_activated_once(self) -> None:
        prepared, runtime = self.prepared(name="admissible")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        dispatcher, calls = self._to_replan(prepared, runtime, evaluation)
        activated = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, activated.action, activated)
        candidate = evaluation.plan.plan_revision_id
        self.assertEqual(candidate, self._active_plan(service, prepared.project_id))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.registered", candidate))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.activated", candidate))
        self.assertEqual(1, len(calls))
        explanation = self._application_status(prepared, runtime, dispatcher).recovery_explanation(
            prepared.project_id
        )
        self.assertEqual("recovered", explanation.state)

    # --- AC3: facade status ---------------------------------------------------

    def test_facade_run_once_and_status_show_the_same_replan_blocker(self) -> None:
        prepared, runtime = self.prepared(name="facade-status")
        evaluation = self._effect_violation(prepared)
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        application = self._application_status(prepared, runtime, dispatcher)
        pending = application.recovery_explanation(prepared.project_id)
        self.assertEqual("automatic_pending", pending.state)

        outcome = application.run_once(prepared.project_id)
        for _ in range(4):
            if outcome.action not in {RunOnceAction.DISPATCHED, RunOnceAction.OBSERVED}:
                break
            outcome = application.run_once(prepared.project_id)
        self._assert_replan_blocked(outcome, "REPLAN_CANDIDATE_NOT_ADMISSIBLE")
        for recovery in (
            application.recovery_explanation(prepared.project_id).model_dump(mode="json"),
            application.status(prepared.project_id)["recovery"],
        ):
            self.assertEqual("user_decision_required", recovery["state"])
            self.assertEqual("user_decision", recovery["next_action"]["mode"])
            self.assertEqual(outcome.blocker_code, recovery["next_action"]["blocker_code"])
            self.assertEqual("subgraph_replan", recovery["next_action"]["suggested_repair_action"])
            self.assertTrue(recovery["next_action"]["checkpoint_required"])
            self.assertEqual(outcome.detail, recovery["next_action"]["detail"])

    # --- 재계획 job 결과 불명 -------------------------------------------------

    def test_replanning_job_error_after_provider_terminal_is_typed_and_replays(self) -> None:
        prepared, runtime = self.prepared(name="replan-job-error")
        service = prepared.service

        class FailingProvider:
            def replan(self, **_kwargs):
                # 역할 runner가 provider terminal을 관측한 뒤 결과 처리에서 실패한 경우다.
                notify_active_runtime_job_progress({
                    "event": "role_terminal_observed",
                    "terminal_observation": {"terminal_status": "failed"},
                })
                raise RuntimeError("replan target failed after provider terminal")

        dispatcher = EngineDispatcher(service, runtime, recovery_provider=FailingProvider())
        self._failed_attempt(
            prepared, runtime, dispatcher, "TASK_CONTRACT_INVALID: injected contract defect",
        )
        self.assertEqual(
            RunOnceAction.RECOVERED,
            self._finish_runtime_job_tick(dispatcher, prepared.project_id).action,
        )

        first = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, first.action, first)
        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", first.blocker_code)
        self.assertEqual(FailureClass.EXTERNAL_UNKNOWN, first.failure_class)
        self.assertEqual(RepairAction.WAIT_EXTERNAL, first.suggested_repair_action)
        self.assertTrue(first.checkpoint_required)
        with service.ledger.read() as connection:
            job = connection.execute(
                "SELECT status,result_json FROM runtime_jobs WHERE project_id=? AND kind='replanning'",
                (prepared.project_id,),
            ).fetchone()
            plans = connection.execute(
                "SELECT COUNT(*) FROM plan_revisions WHERE project_id=?", (prepared.project_id,),
            ).fetchone()[0]
        self.assertEqual("consumed", job["status"])
        self.assertIn("job_error", job["result_json"])
        self.assertEqual(1, plans)
        self.assertEqual(prepared.plan_revision_id, self._active_plan(service, prepared.project_id))
        self._assert_replays(prepared, dispatcher, first)

    # --- AC5: 동시 run_once와 재시작 --------------------------------------------

    def test_concurrent_run_once_registers_and_activates_once_with_typed_outcomes(self) -> None:
        prepared, runtime = self.prepared(name="concurrent-run-once")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        gate = threading.Event()
        first_dispatcher, calls = self._to_replan(prepared, runtime, evaluation, gate=gate)
        scheduled = first_dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        gate.set()
        with service.ledger.read() as connection:
            job_id = connection.execute(
                "SELECT id FROM runtime_jobs WHERE project_id=? AND kind='replanning'",
                (prepared.project_id,),
            ).fetchone()[0]
        deadline = time.monotonic() + 10
        while first_dispatcher._recovery_supervisor.tick(job_id).status is not RuntimeJobStatus.PROVIDER_TERMINAL:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

        # 두 run_once가 모두 활성화 직전까지 온 뒤에만 진행해 활성화 경쟁을 만든다.
        rendezvous = threading.Barrier(2, timeout=10)
        real = service.activate_plan
        activation_errors: list[str] = []

        def both_arrive(**kwargs):
            rendezvous.wait()
            try:
                return real(**kwargs)
            except EngineServiceError as error:
                activation_errors.append(str(error))
                raise

        service.activate_plan = both_arrive
        dispatchers = (
            first_dispatcher,
            EngineDispatcher(service, runtime, recovery_provider=self._provider(evaluation, calls)),
        )
        outcomes: list[object] = []

        def tick(dispatcher):
            try:
                outcomes.append(dispatcher.run_once(prepared.project_id))
            except BaseException as error:  # noqa: BLE001 - 동시 호출 결과 관측
                outcomes.append(error)

        threads = [threading.Thread(target=tick, args=(item,)) for item in dispatchers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(
            [RunOnceAction.RECOVERED, RunOnceAction.RECOVERED],
            [getattr(item, "action", item) for item in outcomes],
        )
        # 이미 active인 같은 후보의 두 번째 활성화는 service에서 거절되고 run_once는 그 활성화를 관측한다.
        self.assertEqual(["admissible이며 ready인 PlanContract만 활성화할 수 있습니다."], activation_errors)
        candidate = evaluation.plan.plan_revision_id
        self.assertEqual(candidate, self._active_plan(service, prepared.project_id))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.registered", candidate))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.activated", candidate))
        self.assertEqual(1, len(calls))

    def test_restart_after_registration_before_return_continues_the_same_candidate(self) -> None:
        prepared, runtime = self.prepared(name="restart")
        service = prepared.service
        evaluation = self._evaluation(prepared)
        dispatcher, calls = self._to_replan(prepared, runtime, evaluation)
        real = service._ensure_replan_candidate_registered

        def crash(item):
            real(item)
            raise _Crash()

        service._ensure_replan_candidate_registered = crash
        with self.assertRaises(_Crash):
            self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        candidate = evaluation.plan.plan_revision_id
        self.assertEqual("ready", self._plan_status(service, candidate))

        restarted = EngineService(SQLiteEngineLedger(
            service.ledger.path, artifact_root=service.ledger.artifact_root,
        ))
        resumed = EngineDispatcher(
            restarted, runtime, recovery_provider=self._provider(evaluation, calls),
        ).run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, resumed.action, resumed)
        self.assertEqual(candidate, self._active_plan(service, prepared.project_id))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.registered", candidate))
        self.assertEqual(1, self._events(service, prepared.project_id, "plan.activated", candidate))
        self.assertEqual(1, len(calls))


class FacadeReplanStatusTests(unittest.TestCase):
    """역할 설정이 있는 EngineApplication의 실제 RecoveryPlanProvider 경로."""

    _Base = fm08.EngineFm08RecoveryIntegrationTests
    setUp = _Base.setUp
    _close_supervisors = _Base._close_supervisors
    _prepare = _Base._prepare
    _authorize = _Base._authorize
    _queue_execution_preparation = _Base._queue_execution_preparation
    _run_until = _Base._run_until
    _worker_binding = _Base._worker_binding
    _dispatch_worker = _Base._dispatch_worker
    _attempt_status = _Base._attempt_status
    _fail_worker = _Base._fail_worker

    def test_effect_violating_replan_is_typed_blocked_and_status_agrees(self) -> None:
        task_id = self._prepare()
        self._authorize()
        attempt, binding = self._dispatch_worker(task_id)
        self._fail_worker(
            attempt, binding,
            response="Task 계약이 현재 대상과 맞지 않습니다.",
            error_code="TASK_CONTRACT_INVALID",
        )
        expansion = fm08._plan_expansion(
            acceptance=["Task 검사가 PASS다."], statement="app.py의 값이 2인지 실행 검사한다.",
        )
        expansion["tasks"][0]["expected_effects"] = [
            {"effect_id": "g1-external", "statement": "G1 외부 서비스 배포", "external": True}
        ]
        self.runner.responses.setdefault("plan_expander", []).append(expansion)

        outcome = None
        for _ in range(40):
            outcome = self.application.run_once(self.project_id)
            if outcome.action is RunOnceAction.BLOCKED:
                break
            time.sleep(0.01)
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", outcome.blocker_code, outcome)
        self.assertIn("PLAN_EFFECT_POLICY_VIOLATION", outcome.detail)
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual("user_decision_required", recovery["state"])
        self.assertEqual(outcome.blocker_code, recovery["next_action"]["blocker_code"])

        with self.service.ledger.read() as connection:
            before = connection.execute(
                "SELECT (SELECT MAX(sequence) FROM history_events WHERE project_id=?),"
                "(SELECT COUNT(*) FROM provider_calls WHERE project_id=?)",
                (self.project_id, self.project_id),
            ).fetchone()
        again = self.application.run_once(self.project_id)
        self.assertEqual((outcome.blocker_code, outcome.detail), (again.blocker_code, again.detail))
        with self.service.ledger.read() as connection:
            after = connection.execute(
                "SELECT (SELECT MAX(sequence) FROM history_events WHERE project_id=?),"
                "(SELECT COUNT(*) FROM provider_calls WHERE project_id=?)",
                (self.project_id, self.project_id),
            ).fetchone()
        self.assertEqual(tuple(before), tuple(after))


if __name__ == "__main__":
    unittest.main()
