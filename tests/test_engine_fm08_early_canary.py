"""FM-08-EARLY-CANARY: skinny core 수직 경로와 대표 external_unknown 안전 경계의 값싼 canary.

C1(FM-08-CANARY-C1): raw request → GoalAuthorization → Plan 자동 활성화 → Task 1개 실행 →
    독립 Validator(다른 Attempt/thread) → GoalVerdict가 결정적 fixture에서 완료된다.
C2(FM-08-CANARY-C2): 실행 Attempt가 provider terminal에 도달했지만 typed adapter effect
    receipt가 없는 대표 fault에서 같은 Task의 자동 재실행과 다음 비가역 외부 효과가 0건이고,
    효과 미확정 상태가 원장에 보존된다.

기존 fixture·helper를 재사용하며 실제 Codex·네트워크는 사용하지 않는다.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.domain import (
    ApprovalClass,
    DependencyType,
    EffectContract,
    EffectIdentity,
    ExecutionAction,
    ExecutionContextNeed,
    ExecutionSpecProposal,
    GoalCoverage,
    GoalContractRevision,
    IntegrationValidationContract,
    PlanContractDefinition,
    PlanContractRevision,
    PlanDependency,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    RecoveryEnvelope,
    RevisionStatus,
    RiskLevel,
    RunOnceAction,
    SkeletonDependency,
    TaskContract,
    TaskKind,
    TaskSkeleton,
    ThreadBinding,
    ValidationContract,
    ValidationExecutionStep,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests import engine_helpers as fixtures
from tests import test_engine_fm08_core_integration as fm08
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from tests.test_engine_ledger_service import TrustedTestEngineService


class Fm08EarlyCanaryVerticalPathTests(unittest.TestCase):
    """FM-08-CANARY-C1: 최소 수직 경로가 결정적 fixture에서 완료된다."""

    setUp = fm08.EngineFm08CoreIntegrationTests.setUp
    _close_supervisors = fm08.EngineFm08CoreIntegrationTests._close_supervisors
    _prepare = fm08.EngineFm08CoreIntegrationTests._prepare
    _authorize = fm08.EngineFm08CoreIntegrationTests._authorize
    _queue_execution_preparation = fm08.EngineFm08CoreIntegrationTests._queue_execution_preparation
    _run_until = fm08.EngineFm08CoreIntegrationTests._run_until
    _worker_binding = fm08.EngineFm08CoreIntegrationTests._worker_binding

    def test_c1_skinny_core_vertical_path_completes_on_deterministic_fixture(self) -> None:
        # raw request → Goal 정규화·검토 → Plan 후보 (승인 전)
        task_id, _prepared = self._prepare()
        before = self.application.status(self.project_id)
        self.assertEqual("authorization_required", before["current_stage"])
        self.assertEqual(0, self.runtime.create_calls)

        # GoalAuthorization → Core가 승인 경계 안 Plan을 자동 활성화
        self._authorize()
        authorized = self.application.status(self.project_id)
        self.assertEqual("authorized", authorized["authorization_state"])
        self.assertEqual("task_ready", authorized["current_stage"])
        with self.service.ledger.read() as connection:
            project = connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id=?", (self.project_id,)
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM plan_revisions WHERE id=?",
                (project["active_plan_revision_id"],),
            ).fetchone()[0]
        self.assertIsNotNone(project["active_plan_revision_id"])
        self.assertEqual("active", plan_status)

        # Task 1개: materialize → dispatch → Worker terminal 관측 → 결정적 Task validation
        self._queue_execution_preparation(task_id)
        self._run_until(RunOnceAction.MATERIALIZED)
        dispatched = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        worker_binding = self._worker_binding(dispatched.attempt_id)
        self.app_file.write_text("value = 2\n", encoding="utf-8")
        self.runtime.complete(worker_binding.thread_id, response="single task complete")
        self._run_until(RunOnceAction.VALIDATED)
        with self.service.ledger.read() as connection:
            task_evidence_id = connection.execute(
                "SELECT id FROM evidence_records WHERE task_id=? AND kind='test' "
                "ORDER BY rowid LIMIT 1",
                (task_id,),
            ).fetchone()[0]

        # 독립 Validator(별도 Attempt/thread) → GoalVerdict
        self.runner.responses.setdefault("goal_test_preparation", []).append(
            {
                "step": {
                    "validation_id": "validation_goal",
                    "method": "semantic",
                    "semantic_instruction": "최종 파일과 Task evidence를 독립적으로 검토한다.",
                    "required_evidence_kinds": ["model_review"],
                }
            }
        )
        self.runner.responses.setdefault("goal_validator", []).append(
            {
                "passed": True,
                "rationale": "최종 파일과 직접 실행된 Task evidence가 요구를 충족한다.",
                "evidence_refs": [task_evidence_id],
            }
        )
        completed = self._run_until(RunOnceAction.COMPLETED)
        if completed.goal_verdict_id is None:
            completed = self._run_until(RunOnceAction.COMPLETED)
        self.assertIsNotNone(completed.goal_verdict_id)

        report = self.application.final_report(
            self.project_id, goal_verdict_id=completed.goal_verdict_id
        )
        status = self.application.status(self.project_id)
        with self.service.ledger.read() as connection:
            tasks = connection.execute(
                "SELECT id,status FROM task_contracts WHERE project_id=?", (self.project_id,)
            ).fetchall()
            execution_attempts = connection.execute(
                "SELECT id,status,binding_json FROM attempts WHERE task_id=? AND kind='execution' "
                "ORDER BY attempt_no",
                (task_id,),
            ).fetchall()
            validator_attempts = connection.execute(
                "SELECT a.id,a.status,a.binding_json FROM attempts a JOIN runtime_jobs j "
                "ON j.attempt_id=a.id WHERE a.project_id=? AND j.kind='goal_semantic_validate' "
                "ORDER BY j.rowid",
                (self.project_id,),
            ).fetchall()
            goal_results = connection.execute(
                "SELECT validation_id,status FROM validation_results WHERE plan_revision_id=? "
                "AND task_id IS NULL",
                (project["active_plan_revision_id"],),
            ).fetchall()
            verdict_count = connection.execute(
                "SELECT COUNT(*) FROM goal_verdicts WHERE project_id=?", (self.project_id,)
            ).fetchone()[0]
            validator_intents = connection.execute(
                "SELECT i.kind,i.status,r.binding_json FROM runtime_intents i "
                "JOIN runtime_receipts r ON r.intent_id=i.id "
                "JOIN attempts a ON a.id=i.attempt_id JOIN runtime_jobs j ON j.attempt_id=a.id "
                "WHERE j.kind='goal_semantic_validate' AND a.project_id=? ORDER BY i.rowid",
                (self.project_id,),
            ).fetchall()
        self.assertEqual([(task_id, "completed")], [tuple(row) for row in tasks])
        self.assertEqual(1, len(execution_attempts))
        self.assertEqual("succeeded", execution_attempts[0]["status"])
        self.assertEqual(1, len(validator_attempts))
        self.assertEqual("succeeded", validator_attempts[0]["status"])
        self.assertNotEqual(execution_attempts[0]["id"], validator_attempts[0]["id"])
        worker_thread = ThreadBinding.model_validate_json(execution_attempts[0]["binding_json"])
        validator_thread = ThreadBinding.model_validate_json(validator_attempts[0]["binding_json"])
        self.assertNotEqual(worker_thread.thread_id, validator_thread.thread_id)
        self.assertEqual([("validation_goal", "pass")], [tuple(row) for row in goal_results])
        self.assertEqual(1, verdict_count)
        # Worker는 FakeCodexRuntime에서 thread 1·turn 1·resume 0으로 실행됐다.
        self.assertEqual((1, 1, 0), (self.runtime.create_calls, self.runtime.turn_calls,
                                     self.runtime.resume_calls))
        # 독립 Validator는 별도 role 경로에서 자체 terminal turn intent·receipt를 남겼다.
        self.assertEqual([("start_turn", "received")], [tuple(row[:2]) for row in validator_intents])
        validator_receipt_binding = ThreadBinding.model_validate_json(validator_intents[0]["binding_json"])
        self.assertEqual(validator_thread.thread_id, validator_receipt_binding.thread_id)
        self.assertIsNotNone(validator_receipt_binding.turn_id)
        self.assertNotEqual(worker_thread.thread_id, validator_receipt_binding.thread_id)
        self.assertEqual("completed", status["current_stage"])
        self.assertEqual("satisfied", report.verdict.status.value)
        self.assertEqual("none", report.execution_summary.external_effect_status)
        self.assertTrue(report.ledger_history_valid)
        self.assertTrue(self.service.ledger.verify_history(self.project_id))


class Fm08EarlyCanaryExternalUnknownTests(unittest.TestCase):
    """FM-08-CANARY-C2: effect 미확정 상태에서 중복 실행과 다음 비가역 효과가 0건이다."""

    @staticmethod
    def _identity(**updates) -> EffectIdentity:
        base = EffectIdentity(
            provider="github", system="github-api", target="repo:owner/name",
            account="account:owner", operation="publish-release", scope="release:v1",
            idempotency_key="release-v1-idempotent", checkpoint_policy="none",
        )
        return base.model_copy(update=updates)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.ledger = SQLiteEngineLedger(
            self.base / "state" / "flowmarshal-engine.sqlite3",
            artifact_root=self.base / "engine-artifacts",
        )
        self.service = TrustedTestEngineService(self.ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="fm08-canary", root=self.root)
        self.profile = fixtures.profile(self.project_id)
        self.service.register_profile(self.profile)

        # 첫 Task: 가역 외부 효과, 둘째 Task: 첫 Task 산출물에 의존하는 비가역 외부 효과.
        self.reversible = self._identity()
        self.irreversible = self._identity(
            operation="publish-assets", scope="release-assets:v1",
            idempotency_key="release-assets-v1-idempotent",
            checkpoint_policy="before_irreversible",
        )
        self.effect_one = EffectContract(
            effect_id="publish_one", statement="publish_one", external=True, reversible=True,
            identity_version="2.0", identity=self.reversible,
        )
        self.effect_two = EffectContract(
            effect_id="publish_two", statement="publish_two", external=True, reversible=False,
            identity_version="2.0", identity=self.irreversible,
        )
        base_goal = fixtures.goal(self.project_id, self.profile.definition_digest)
        policy = base_goal.definition.effect_policy.model_copy(update={
            "allowed_external_effects": ("publish_one", "publish_two"),
            "allowed_external_effect_contracts": (self.reversible, self.irreversible),
        })
        definition = base_goal.definition.model_copy(update={"effect_policy": policy})
        self.goal = GoalContractRevision(
            goal_revision_id=new_id("goal_revision"), goal_id=base_goal.goal_id, revision_no=1,
            definition=definition, definition_digest=definition.definition_digest,
            status=RevisionStatus.READY, created_at=utc_now(),
        )
        self.service.register_goal(self.goal)
        self.map = fixtures.project_map(self.project_id, self.root)
        self.service.record_project_map(self.map)
        self.state = fixtures.state(
            self.project_id, self.goal.definition_digest, self.map.revision_digest,
        )
        self.service.record_state_snapshot(self.state)
        self.inventory = fixtures.inventory()
        self.runtime = FakeCodexRuntime(self.inventory)

        self.skeleton = self._skeleton()
        skeleton_review = fixtures.clean_review(
            sha256_digest(self.skeleton), role="skeleton_reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(
                self.skeleton, self.goal, self.state, self.map,
            ),
        )
        self.service.record_skeleton_evaluation(CandidateEvaluation(
            candidate=self.skeleton,
            semantic_submission=skeleton_review,
            decision=derive_candidate_decision(
                candidate_digest=sha256_digest(self.skeleton),
                findings=(), ratings=skeleton_review.ratings,
            ),
        ))
        self.plan, self.task_one, self.task_two = self._plan()
        plan_review = fixtures.clean_review(
            self.plan.activation_digest, role="external_effect_reviewer",
            evidence_catalog=plan_review_evidence_catalog(
                self.plan, self.goal, self.state, self.map,
            ),
        )
        self.service.authorize_goal(project_id=self.project_id, source="fm08-canary-user")
        self.service.register_authorized_plan_revision(ExpandedPlanEvaluation(
            plan=self.plan,
            semantic_submissions=(plan_review,),
            decision=derive_candidate_decision(
                candidate_digest=self.plan.activation_digest,
                findings=(), ratings=plan_review.ratings,
            ),
        ))

    def _skeleton(self) -> PlanSkeletonCandidate:
        base = fixtures.skeleton(self.goal, self.state)
        first = base.tasks[0]
        second = TaskSkeleton(
            task_ref="task_two", kind=TaskKind.CHANGE, objective="비가역 자산을 게시한다.",
            contributes_to=("ac_one",), produces=("result:two",), consumes=("result:one",),
        )
        return base.model_copy(update={
            "tasks": (first, second),
            "dependencies": (SkeletonDependency(
                producer_task_ref="task_one", consumer_task_ref="task_two",
                dependency_type=DependencyType.DATA,
                produces=("result:one",), consumes=("result:one",),
            ),),
            "goal_coverage": (GoalCoverage(criterion_id="ac_one", task_refs=("task_one", "task_two")),),
        })

    def _plan(self) -> tuple[PlanContractRevision, TaskContract, TaskContract]:
        def validation(validation_id: str) -> ValidationContract:
            return ValidationContract(
                validation_id=validation_id, statement="외부 대상을 재관측한다.",
                method="external_observation", required_evidence_kinds=("external_observation",),
            )

        task_one = TaskContract(
            task_id=new_id("task"), task_ref="task_one", project_id=self.project_id,
            kind=TaskKind.CHANGE, objective="요구를 구현한다.", goal_criterion_refs=("ac_one",),
            produces=("result:one",), consumes=("input:request",),
            acceptance_criteria=("validation이 PASS다.",),
            validations=(validation("validation_task_one"),),
            expected_effects=(self.effect_one,),
            risk_level=RiskLevel.LOW, approval_class=ApprovalClass.PLAN_ACTIVATION,
            recovery=RecoveryEnvelope(retryable_failure_classes=("implementation",)),
            assignment=fixtures.assignment(),
        )
        task_two = TaskContract(
            task_id=new_id("task"), task_ref="task_two", project_id=self.project_id,
            kind=TaskKind.CHANGE, objective="비가역 자산을 게시한다.", goal_criterion_refs=("ac_one",),
            produces=("result:two",), consumes=("result:one",),
            acceptance_criteria=("validation이 PASS다.",),
            validations=(validation("validation_task_two"),),
            expected_effects=(self.effect_two,),
            risk_level=RiskLevel.LOW, approval_class=ApprovalClass.EXECUTION_CHECKPOINT,
            recovery=RecoveryEnvelope(retryable_failure_classes=("implementation",)),
            assignment=fixtures.assignment(),
        )
        definition = PlanContractDefinition(
            project_id=self.project_id,
            goal_contract_digest=self.goal.definition_digest,
            base_state_snapshot_digest=self.state.snapshot_digest,
            project_map_digest=self.map.revision_digest,
            source_skeleton_digest=sha256_digest(self.skeleton),
            tasks=(task_one, task_two),
            dependencies=(PlanDependency(
                producer_task_id=task_one.task_id, consumer_task_id=task_two.task_id,
                dependency_type=DependencyType.DATA, products=("result:one",),
            ),),
            goal_coverage=(PlanGoalCoverage(
                criterion_id="ac_one", task_ids=(task_one.task_id, task_two.task_id),
                validation_ids=("validation_task_one", "validation_task_two", "validation_goal"),
            ),),
            integration_validations=(IntegrationValidationContract(
                validation_id="validation_goal", statement="Goal을 확인한다.",
                evidence_mode="task_aggregate", criterion_refs=("ac_one",),
                method="deterministic", required_evidence_kinds=("test",),
            ),),
            model_inventory_digest=self.inventory.inventory_digest,
        )
        revision = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"), plan_id=new_id("plan"), revision_no=1,
            definition=definition, definition_digest=definition.definition_digest,
            status=RevisionStatus.READY, created_at=utc_now(),
        )
        return revision, task_one, task_two

    def _proposal(self) -> ExecutionSpecProposal:
        return ExecutionSpecProposal(
            task_id=self.task_one.task_id,
            context_needs=(ExecutionContextNeed(
                need_id="policy", description="프로젝트 지침", path_hints=("AGENTS.md",),
            ),),
            resolved_targets=(),
            actions=(ExecutionAction(
                action_ref="publish", kind="external_effect", description="release를 게시한다.",
                effect_id="publish_one",
            ),),
            validation_steps=(ValidationExecutionStep(
                validation_id="validation_task_one", method="external_observation",
                external_selector="release:v1", required_evidence_kinds=("external_observation",),
            ),),
            timeout_seconds=60, context_token_budget=12_000, idempotency_hint="fm08-canary",
        )

    def _ledger_snapshot(self) -> dict[str, object]:
        with self.ledger.read() as connection:
            attempts = connection.execute(
                "SELECT id,task_id,kind,attempt_no,status,failure_class FROM attempts "
                "WHERE project_id=? ORDER BY rowid", (self.project_id,),
            ).fetchall()
            intents = connection.execute(
                "SELECT i.id,i.attempt_id,i.kind,i.status FROM runtime_intents i "
                "JOIN attempts a ON a.id=i.attempt_id WHERE a.project_id=? ORDER BY i.rowid",
                (self.project_id,),
            ).fetchall()
            receipts = connection.execute(
                "SELECT r.id,r.intent_id FROM runtime_receipts r JOIN runtime_intents i "
                "ON i.id=r.intent_id JOIN attempts a ON a.id=i.attempt_id "
                "WHERE a.project_id=? ORDER BY r.rowid", (self.project_id,),
            ).fetchall()
            calls = connection.execute(
                "SELECT id,attempt_id,execution_status,effect_status,result_status "
                "FROM provider_calls WHERE project_id=? ORDER BY rowid", (self.project_id,),
            ).fetchall()
            tasks = connection.execute(
                "SELECT id,status FROM task_contracts WHERE project_id=? ORDER BY position",
                (self.project_id,),
            ).fetchall()
            checkpoints = connection.execute(
                "SELECT COUNT(*) FROM effect_checkpoints WHERE task_id IN (?,?)",
                (self.task_one.task_id, self.task_two.task_id),
            ).fetchone()[0]
            effect_events = connection.execute(
                "SELECT event_type,COUNT(*) FROM history_events WHERE project_id=? "
                "AND event_type IN ('effect.receipt_recorded','provider_effect.confirmed',"
                "'effect.checkpointed','attempt.failed','task.completed','task.retry_enabled') "
                "GROUP BY event_type ORDER BY event_type", (self.project_id,),
            ).fetchall()
        return {
            "attempts": [tuple(row) for row in attempts],
            "intents": [tuple(row) for row in intents],
            "receipts": [tuple(row) for row in receipts],
            "calls": [tuple(row) for row in calls],
            "tasks": {row["id"]: row["status"] for row in tasks},
            "checkpoints": checkpoints,
            "effect_events": {row[0]: row[1] for row in effect_events},
        }

    def _observe_after_fault(self, dispatcher: EngineDispatcher) -> str:
        """terminal 관측 tick의 표면 결과를 문자열로 돌려준다(BLOCKED 또는 EngineServiceError)."""
        try:
            outcome = dispatcher.run_once(self.project_id)
        except EngineServiceError as error:
            return f"error:{error}"
        return f"{outcome.action.value}:{outcome.blocker_code}"

    def test_c2_terminal_without_effect_receipt_blocks_rerun_and_next_irreversible_effect(self) -> None:
        dispatcher = EngineDispatcher(self.service, self.runtime)
        materialized = dispatcher.run_once(self.project_id, proposal=self._proposal())
        self.assertEqual(RunOnceAction.MATERIALIZED, materialized.action)
        dispatched = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        self.assertEqual(self.task_one.task_id, dispatched.task_id)
        with self.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,),
            ).fetchone()[0])
        self.assertEqual((1, 1, 0), (self.runtime.create_calls, self.runtime.turn_calls,
                                     self.runtime.resume_calls))

        # 대표 fault: provider turn은 terminal이지만 typed adapter effect receipt가 없다.
        self.runtime.complete(binding.thread_id, response="release published")
        first_tick = self._observe_after_fault(dispatcher)
        self.assertIn("EXTERNAL_EFFECT", first_tick)
        after_fault = self._ledger_snapshot()

        # 효과 미확정 보존: terminal·valid provider call의 effect_status가 unknown이다.
        self.assertEqual(
            [(dispatched.attempt_id, "terminal", "unknown", "valid")],
            [row[1:] for row in after_fault["calls"]],
        )
        self.assertEqual({}, {
            key: value for key, value in after_fault["effect_events"].items()
            if key in {"effect.receipt_recorded", "provider_effect.confirmed",
                       "effect.checkpointed", "task.completed", "task.retry_enabled"}
        })
        self.assertEqual("pending", after_fault["tasks"][self.task_two.task_id])
        self.assertNotEqual("completed", after_fault["tasks"][self.task_one.task_id])
        self.assertEqual(
            [("create_thread", "received"), ("start_turn", "received")],
            [row[2:] for row in after_fault["intents"]],
        )
        self.assertEqual(2, len(after_fault["receipts"]))
        self.assertEqual(0, after_fault["checkpoints"])

        # 후속 tick(같은 dispatcher와 재시작한 dispatcher)에서 자동 재실행·resume·새 intent가 없다.
        restarted = EngineDispatcher(EngineService(self.ledger), self.runtime)
        surfaces = [
            self._observe_after_fault(dispatcher),
            self._observe_after_fault(restarted),
            self._observe_after_fault(restarted),
        ]
        for surface in surfaces:
            self.assertIn("EXTERNAL_EFFECT", surface)
        after_ticks = self._ledger_snapshot()
        self.assertEqual((1, 1, 0), (self.runtime.create_calls, self.runtime.turn_calls,
                                     self.runtime.resume_calls))
        self.assertEqual(after_fault["attempts"], after_ticks["attempts"])
        self.assertEqual(after_fault["intents"], after_ticks["intents"])
        self.assertEqual(after_fault["receipts"], after_ticks["receipts"])
        self.assertEqual(after_fault["calls"], after_ticks["calls"])
        self.assertEqual(after_fault["tasks"], after_ticks["tasks"])
        self.assertEqual(0, after_ticks["checkpoints"])
        self.assertEqual(after_fault["effect_events"], after_ticks["effect_events"])
        # 같은 Task의 execution Attempt는 정확히 1개, 둘째 Task의 Attempt·intent는 0개다.
        self.assertEqual(
            [(self.task_one.task_id, "execution", 1)],
            [(row[1], row[2], row[3]) for row in after_ticks["attempts"]],
        )
        self.assertEqual({dispatched.attempt_id}, {row[1] for row in after_ticks["intents"]})

        # Core 완료 판정·다음 Task 예약도 효과 확정 전에는 거부된다.
        with self.assertRaises(EngineServiceError):
            self.service.complete_task(self.task_one.task_id)
        with self.assertRaises(EngineServiceError):
            self.service.reserve_attempt(task_id=self.task_two.task_id)

        # 읽기 전용 status는 external_effect_unknown 단계와 미확정 provider call을 보고한다.
        application = EngineApplication(EngineService(self.ledger), runtime=self.runtime, governance=ALLOW_ALL)
        self.addCleanup(application.supervisor.close, timeout_seconds=0.1)
        status = application.status(self.project_id)
        self.assertEqual("external_effect_unknown", status["current_stage"])
        summary = status["execution_summary"]
        self.assertEqual("unknown", summary["external_effect_status"])
        self.assertIn(after_ticks["calls"][0][0], summary["external_effect_refs"])
        self.assertTrue(self.ledger.verify_history(self.project_id))


if __name__ == "__main__":
    unittest.main()
