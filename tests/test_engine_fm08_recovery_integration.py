"""FM-08-RECOVERY-INTEGRATION: 사용자 경로(facade/CLI)의 최소 자동 복구 통합 검사.

`tests/test_engine_automatic_recovery.py`는 `EngineDispatcher`를 직접 만들어 recovery
provider를 주입한다. 이 모듈은 사용자가 실제로 쓰는 `EngineApplication`·CLI 경로에서
bounded repair와 subgraph replan이 연결되는지, 복구 상태가 분류 근거·보존/폐기 범위·
다음 동작으로 표시되는지, 모델 자기보고 code만으로는 복구가 시작되지 않는지를 본다.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.application import ApplicationAuthority, EngineApplication
from flowmarshal.engine.cli import main
from flowmarshal.engine.domain import (
    ApprovalClass,
    DependencyType,
    IntegrationValidationContract,
    PlanContractDefinition,
    PlanContractRevision,
    PlanDependency,
    PlanGoalCoverage,
    RecoveryEnvelope,
    RevisionStatus,
    RiskLevel,
    RunOnceAction,
    TaskContract,
    TaskKind,
    ThreadBinding,
    ValidationContract,
    new_id,
    utc_now,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.models import EngineRoleConfiguration, RoleModelBinding
from flowmarshal.engine.recovery_planning import (
    RECOVERY_PLAN_REVIEWER_ROLE,
    RecoveryPlanError,
    failed_subgraph_task_refs,
    replace_failed_subgraph,
)
from flowmarshal.engine.runtime import FakeCodexRuntime
from flowmarshal.engine.service import EngineService
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from tests.engine_helpers import assignment, inventory, profile
from tests.engine_inspection_helpers import InspectionScriptedRunner


def _ratings() -> dict[str, int]:
    return {
        "goal_fit": 4,
        "grounding": 4,
        "engineering": 4,
        "verification": 4,
        "execution_safety": 4,
    }


def _roles() -> EngineRoleConfiguration:
    worker = RoleModelBinding(model="worker", effort="medium")
    validator = RoleModelBinding(model="validator", effort="high")
    return EngineRoleConfiguration(
        normalizer=worker,
        skeleton_generator=worker,
        plan_expander=worker,
        general_reviewer=validator,
        critical_reviewer=validator,
        executor=worker,
        validator=validator,
    )


def _plan_expansion(*, acceptance: list[str], statement: str) -> dict:
    """같은 Skeleton 의미를 유지하며 상세 검사 계약만 달라지는 Plan 상세화."""

    return {
        "tasks": [
            {
                "task_ref": "single_task",
                "kind": "change",
                "objective": "app.py 값을 2로 바꾼다.",
                "goal_criterion_refs": ["ac_001"],
                "produces": ["result:app_value"],
                "consumes": ["input:request"],
                "acceptance_criteria": acceptance,
                "validations": [
                    {
                        "validation_id": "validation_task",
                        "statement": statement,
                        "method": "deterministic",
                        "required_evidence_kinds": ["test"],
                    }
                ],
                "risk_level": "low",
                "recovery": {
                    "retryable_failure_classes": [
                        "implementation",
                        "context",
                        "task_contract",
                        "dependency",
                    ]
                },
            }
        ],
        "dependencies": [],
        "goal_coverage": [
            {
                "criterion_id": "ac_001",
                "task_refs": ["single_task"],
                "validation_ids": ["validation_task", "validation_goal"],
            }
        ],
        "integration_validations": [
            {
                "validation_id": "validation_goal",
                "statement": "Task와 분리된 Validator가 최종 파일과 evidence를 검토한다.",
                "criterion_refs": ["ac_001"],
                "method": "semantic",
                "required_evidence_kinds": ["model_review"],
            }
        ],
    }


def _responses() -> dict[str, list[dict]]:
    return {
        "goal_normalizer": [
            {
                "mission_class": "feature_extension",
                "observable_outcome": "한 파일 변경이 Task와 독립 Goal 검사로 검증된다.",
                "hard_acceptance": [
                    {
                        "statement": "app.py 값 변경이 독립적으로 검증된다.",
                        "validation_intent": "Task 검사와 별도 Goal Validator를 실행한다.",
                    }
                ],
                "mutation_policy": "scoped_change",
                "behavior_policy": "preserve_public_contracts",
            }
        ],
        "goal_reviewer": [{"findings": [], "ratings": _ratings()}],
        "skeleton_generator": [
            {
                "candidates": [
                    {
                        "approach": {
                            "strategy_family": "direct",
                            "change_shape": "single-task",
                            "compatibility": "preserve",
                            "rollout_recovery": "bounded retry",
                        },
                        "tasks": [
                            {
                                "task_ref": "single_task",
                                "kind": "change",
                                "objective": "app.py 값을 2로 바꾼다.",
                                "contributes_to": ["ac_001"],
                                "produces": ["result:app_value"],
                                "consumes": ["input:request"],
                            }
                        ],
                        "dependencies": [],
                        "goal_coverage": [
                            {"criterion_id": "ac_001", "task_refs": ["single_task"]}
                        ],
                        "unknowns": [],
                        "estimated_change_cost": 1,
                        "estimated_context_tokens": 100,
                    }
                ]
            }
        ],
        "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
        "plan_expander": [
            _plan_expansion(
                acceptance=["Task 검사가 PASS다."],
                statement="app.py의 값이 2인지 실행 검사한다.",
            )
        ],
        "compact_plan_reviewer": [{"findings": [], "ratings": _ratings()}],
    }


class EngineFm08RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("테스트 지침\n", encoding="utf-8")
        self.app_file = self.root / "app.py"
        self.app_file.write_text("value = 1\n", encoding="utf-8")
        self.service = EngineService(
            SQLiteEngineLedger(
                base / "state" / "engine.sqlite3", artifact_root=base / "artifacts"
            )
        )
        self.service.initialize()
        self.project_id = self.service.create_project(name="fm08-recovery", root=self.root)
        self.service.register_profile(profile(self.project_id))
        self.runtime = FakeCodexRuntime(inventory())
        self.runner = InspectionScriptedRunner(_responses())
        self.application = EngineApplication(
            self.service,
            runtime=self.runtime,
            role_configuration=_roles(),
            structured_runner=self.runner,
            governance=ALLOW_ALL,
        )
        self.authority = ApplicationAuthority(self.application)
        self.supervisors = [self.application.supervisor]
        self.addCleanup(self._close_supervisors)

    def _close_supervisors(self) -> None:
        for supervisor in self.supervisors:
            if supervisor is not None:
                supervisor.close(timeout_seconds=0.1)

    # --- harness -------------------------------------------------------

    def _prepare(self) -> str:
        prepared = self.application.prepare(
            self.project_id,
            source_request="app.py의 value를 2로 바꾸고 독립적으로 검증해 주세요.",
        )
        self.assertEqual("ready_for_authorization", prepared.status)
        plan = prepared.planning.plan_evaluations[0].plan
        self.assertEqual(1, len(plan.definition.tasks))
        return plan.definition.tasks[0].task_id

    def _authorize(self) -> None:
        target = self.authority.authorization_target(self.project_id)
        self.authority.authorize(
            self.project_id, target=target, source="fm08-recovery-test-user"
        )

    def _queue_execution_preparation(self, task_id: str) -> None:
        self.runner.responses.setdefault("execution_preparation", []).append(
            {
                "proposal": {
                    "task_id": task_id,
                    "context_needs": [
                        {
                            "need_id": "app_source",
                            "description": "변경 대상 app.py 본문이 필요하다.",
                            "path_hints": ["app.py"],
                        }
                    ],
                    "resolved_targets": [
                        {
                            "target_ref": "app",
                            "path": "app.py",
                            "expected_content_digest": sha256_bytes(
                                self.app_file.read_bytes()
                            ),
                            "access": "write",
                        }
                    ],
                    "actions": [
                        {
                            "action_ref": "edit_app",
                            "kind": "edit",
                            "description": "app.py의 value를 2로 변경한다.",
                        }
                    ],
                    "validation_steps": [
                        {
                            "validation_id": "validation_task",
                            "argv": [
                                sys.executable,
                                "-c",
                                (
                                    "from pathlib import Path; "
                                    "assert Path('app.py').read_text(encoding='utf-8') "
                                    "== 'value = 2\\n'"
                                ),
                            ],
                            "working_directory": str(self.root),
                            "timeout_seconds": 30,
                            "expected_exit_codes": [0],
                            "artifact_paths": ["app.py"],
                        }
                    ],
                    "timeout_seconds": 60,
                    "context_token_budget": 12000,
                    "idempotency_hint": "fm08-recovery-single-task",
                },
                "context_request": None,
            }
        )

    def _queue_goal_validation(self, task_id: str) -> None:
        with self.service.ledger.read() as connection:
            evidence_id = connection.execute(
                "SELECT id FROM evidence_records WHERE task_id=? AND kind='test' "
                "ORDER BY rowid LIMIT 1",
                (task_id,),
            ).fetchone()[0]
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
                "evidence_refs": [evidence_id],
            }
        )

    def _run_until(self, action: RunOnceAction, *, limit: int = 80):
        result = None
        for _ in range(limit):
            result = self.application.run_once(self.project_id)
            if result.action is action:
                return result
            self.assertNotEqual(RunOnceAction.BLOCKED, result.action, result)
            time.sleep(0.01)
        self.fail(f"{action.value} 전이에 도달하지 못했습니다: {result}")

    def _settle(self, *, limit: int = 40):
        """job 예약·관측만 반복하고 첫 결정적 전이를 그대로 반환한다."""

        result = None
        for _ in range(limit):
            result = self.application.run_once(self.project_id)
            if result.action not in {RunOnceAction.DISPATCHED, RunOnceAction.OBSERVED}:
                return result
            time.sleep(0.01)
        self.fail(f"bounded tick 안에서 수렴하지 않았습니다: {result}")

    def _worker_binding(self, attempt_id: str) -> ThreadBinding:
        deadline = time.monotonic() + 2
        binding = None
        while time.monotonic() < deadline:
            with self.service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id=?", (attempt_id,)
                ).fetchone()
            if row is not None and row["binding_json"] is not None:
                binding = ThreadBinding.model_validate_json(row["binding_json"])
                if binding.turn_id is not None:
                    return binding
            time.sleep(0.01)
        self.fail(f"Worker turn binding을 관측하지 못했습니다: {binding}")

    def _dispatch_worker(self, task_id: str) -> tuple[str, ThreadBinding]:
        self._queue_execution_preparation(task_id)
        self._run_until(RunOnceAction.MATERIALIZED)
        dispatched = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        return dispatched.attempt_id, self._worker_binding(dispatched.attempt_id)

    def _attempt_status(self, attempt_id: str) -> str:
        with self.service.ledger.read() as connection:
            return connection.execute(
                "SELECT status FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()[0]

    def _fail_worker(
        self, attempt_id: str, binding: ThreadBinding, *, response: str, error_code: str | None
    ):
        """provider 실패를 주입하고 Core가 실패를 기록한 직후에서 멈춘다."""

        self.runtime.fail(binding.thread_id, response=response, error_code=error_code)
        for _ in range(40):
            outcome = self.application.run_once(self.project_id)
            if self._attempt_status(attempt_id) == "failed":
                return outcome
            self.assertIn(
                outcome.action,
                {RunOnceAction.OBSERVED, RunOnceAction.DISPATCHED},
                outcome,
            )
            time.sleep(0.01)
        self.fail("Attempt 실패가 원장에 기록되지 않았습니다.")

    def _codes(self, recovery: dict, provenance: str) -> list[dict]:
        return [
            item
            for item in recovery["classification"]["codes"]
            if item["provenance"] == provenance
        ]

    def _ledger_counts(self) -> dict[str, int]:
        with self.service.ledger.read() as connection:
            return {
                "assessments": connection.execute(
                    "SELECT COUNT(*) FROM recovery_assessments WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0],
                "recovery_history": connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE project_id=? AND event_type IN "
                    "('task.retry_enabled','task.execution_spec_recovery_enabled')",
                    (self.project_id,),
                ).fetchone()[0],
                "attempts": connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE project_id=? AND kind='execution'",
                    (self.project_id,),
                ).fetchone()[0],
                "replanning_jobs": connection.execute(
                    "SELECT COUNT(*) FROM runtime_jobs WHERE project_id=? AND kind='replanning'",
                    (self.project_id,),
                ).fetchone()[0],
            }

    # --- FM-08-RECOVERY-C1 ---------------------------------------------

    def test_implementation_repair_recovers_and_revalidates_through_the_facade(self) -> None:
        """C1-a: provider error code 하나로 시작한 repair가 독립 재검증까지 끝난다."""

        task_id = self._prepare()
        self._authorize()
        first_attempt, first_binding = self._dispatch_worker(task_id)
        self._fail_worker(
            first_attempt,
            first_binding,
            response="구현이 요구를 만족하지 못했습니다.",
            error_code="IMPLEMENTATION_ERROR",
        )

        before = self.application.status(self.project_id)
        recovery = before["recovery"]
        self.assertEqual("automatic_pending", recovery["state"])
        self.assertEqual("implementation", recovery["classification"]["failure_class"])
        self.assertEqual("direct_evidence", recovery["classification"]["basis"])
        self.assertEqual(
            [["IMPLEMENTATION_ERROR"]],
            [item["values"] for item in self._codes(recovery, "provider_observed")],
        )
        self.assertTrue(
            all(item["authoritative"] for item in self._codes(recovery, "provider_observed"))
        )
        self.assertEqual("automatic", recovery["next_action"]["mode"])
        self.assertEqual("task_repair", recovery["next_action"]["suggested_repair_action"])
        self.assertIn(first_attempt, recovery["scope"]["preserved_attempt_ids"])
        self.assertTrue(recovery["scope"]["preserved_evidence_ids"])
        self.assertEqual(0, recovery["limits"]["task_recovery_count"])
        self.assertIsNone(recovery["limits"]["limit_code"])
        self.assertEqual("recovery_required", before["current_stage"])
        self.assertIn("implementation", before["reason"])

        recovered = self._run_until(RunOnceAction.RECOVERED)
        self.assertEqual(first_attempt, recovered.attempt_id)

        second = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, second.action)
        self.assertNotEqual(first_attempt, second.attempt_id)
        second_binding = self._worker_binding(second.attempt_id)
        self.assertNotEqual(first_binding.thread_id, second_binding.thread_id)

        self.app_file.write_text("value = 2\n", encoding="utf-8")
        self.runtime.complete(second_binding.thread_id, response="repair complete")
        self._run_until(RunOnceAction.VALIDATED)
        self._queue_goal_validation(task_id)
        completed = self._run_until(RunOnceAction.COMPLETED)
        if completed.goal_verdict_id is None:
            completed = self._run_until(RunOnceAction.COMPLETED)
        self.assertIsNotNone(completed.goal_verdict_id)

        with self.service.ledger.read() as connection:
            attempts = connection.execute(
                "SELECT id,attempt_no,status,failure_class FROM attempts "
                "WHERE project_id=? AND kind='execution' ORDER BY attempt_no",
                (self.project_id,),
            ).fetchall()
            failure_evidence = connection.execute(
                "SELECT COUNT(*) FROM evidence_records WHERE attempt_id=?",
                (first_attempt,),
            ).fetchone()[0]
            assessment = connection.execute(
                "SELECT attempt_id,action,failure_class FROM recovery_assessments "
                "WHERE project_id=?",
                (self.project_id,),
            ).fetchall()
        self.assertEqual(
            [(first_attempt, 1, "failed", "implementation"), (second.attempt_id, 2, "succeeded", None)],
            [tuple(row) for row in attempts],
        )
        self.assertTrue(failure_evidence, "원 실패 Attempt의 evidence가 보존돼야 합니다.")
        self.assertEqual(
            [(first_attempt, "task_repair", "implementation")],
            [tuple(row) for row in assessment],
        )

        report = self.application.final_report(
            self.project_id, goal_verdict_id=completed.goal_verdict_id
        )
        self.assertEqual("satisfied", report.verdict.status.value)
        after = self.application.status(self.project_id)
        self.assertEqual("completed", after["current_stage"])
        self.assertEqual("recovered", after["recovery"]["state"])
        self.assertIn(first_attempt, after["recovery"]["scope"]["preserved_attempt_ids"])
        self.assertEqual(
            [assessment[0]["attempt_id"]],
            [item["attempt_id"] for item in after["recovery"]["assessments"]],
        )

    def test_subgraph_replan_is_reviewed_activated_and_finishes_through_the_facade(self) -> None:
        """C1-b: task_contract 실패가 사용자 경로에서 실제 subgraph replan으로 복구된다."""

        task_id = self._prepare()
        self._authorize()
        first_attempt, first_binding = self._dispatch_worker(task_id)
        with self.service.ledger.read() as connection:
            original_plan_id = connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id=?",
                (self.project_id,),
            ).fetchone()[0]
        self._fail_worker(
            first_attempt,
            first_binding,
            response="Task 계약이 현재 대상과 맞지 않습니다.",
            error_code="TASK_CONTRACT_INVALID",
        )

        before = self.application.status(self.project_id)
        self.assertEqual("task_contract", before["recovery"]["classification"]["failure_class"])
        self.assertEqual(
            "subgraph_replan", before["recovery"]["next_action"]["suggested_repair_action"]
        )
        self.assertEqual("automatic", before["recovery"]["next_action"]["mode"])

        # 재계획 역할 응답: 같은 Skeleton 의미를 유지한 상세 계약 수정과 독립 검토.
        self.runner.responses.setdefault("plan_expander", []).append(
            _plan_expansion(
                acceptance=[
                    "Task 검사가 PASS다.",
                    "재계획한 검사 statement가 실패 근거를 다시 관측한다.",
                ],
                statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
            )
        )
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(
            {"findings": [], "ratings": _ratings()}
        )

        assessed = self._run_until(RunOnceAction.RECOVERED)
        self.assertEqual(first_attempt, assessed.attempt_id)
        activated = self._run_until(RunOnceAction.RECOVERED)
        self.assertEqual(first_attempt, activated.attempt_id)

        with self.service.ledger.read() as connection:
            active_plan_id = connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id=?",
                (self.project_id,),
            ).fetchone()[0]
            plan_rows = connection.execute(
                "SELECT id,revision_no,status,supersedes_id FROM plan_revisions "
                "WHERE project_id=? ORDER BY revision_no",
                (self.project_id,),
            ).fetchall()
            task_rows = connection.execute(
                "SELECT id,plan_revision_id,status FROM task_contracts WHERE project_id=? "
                "ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
            jobs = connection.execute(
                "SELECT kind,status FROM runtime_jobs WHERE project_id=? "
                "AND kind IN ('recovery','replanning') ORDER BY created_at,rowid",
                (self.project_id,),
            ).fetchall()
            reviews = connection.execute(
                "SELECT reviewer_role FROM candidate_reviews WHERE project_id=? "
                "AND artifact_kind='plan' ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
            preserved_evidence = connection.execute(
                "SELECT COUNT(*) FROM evidence_records WHERE attempt_id=?",
                (first_attempt,),
            ).fetchone()[0]
            preserved_attempt = connection.execute(
                "SELECT status,failure_class FROM attempts WHERE id=?", (first_attempt,)
            ).fetchone()
        self.assertNotEqual(original_plan_id, active_plan_id)
        self.assertEqual(
            [(original_plan_id, 1, "superseded", None), (active_plan_id, 2, "active", original_plan_id)],
            [tuple(row) for row in plan_rows],
        )
        self.assertEqual(
            [("recovery", "consumed"), ("replanning", "consumed")],
            [(row["kind"], row["status"]) for row in jobs],
        )
        self.assertIn(RECOVERY_PLAN_REVIEWER_ROLE, [row["reviewer_role"] for row in reviews])
        self.assertNotEqual(
            RECOVERY_PLAN_REVIEWER_ROLE, reviews[0]["reviewer_role"],
            "원 planning reviewer와 복구 검토 역할은 구분돼야 합니다.",
        )
        self.assertEqual(("failed", "task_contract"), tuple(preserved_attempt))
        self.assertTrue(preserved_evidence)
        superseded = [row["id"] for row in task_rows if row["status"] == "superseded"]
        new_tasks = [row["id"] for row in task_rows if row["plan_revision_id"] == active_plan_id]
        self.assertEqual([task_id], superseded)
        self.assertEqual(1, len(new_tasks))
        self.assertNotEqual(task_id, new_tasks[0])

        # 활성화된 Plan은 실제 PlanExpanderAdapter가 만든 재계획 상세 계약이다.
        with self.service.ledger.read() as connection:
            activated = PlanContractRevision.model_validate_json(
                connection.execute(
                    "SELECT payload_json FROM plan_revisions WHERE id=?", (active_plan_id,)
                ).fetchone()["payload_json"]
            )
        replanned_task = activated.definition.tasks[0]
        self.assertIn(
            "재계획한 검사 statement가 실패 근거를 다시 관측한다.",
            replanned_task.acceptance_criteria,
        )
        self.assertEqual(
            "app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
            replanned_task.validations[0].statement,
        )

        status = self.application.status(self.project_id)
        scope = status["recovery"]["scope"]
        self.assertEqual([original_plan_id], scope["superseded_plan_revision_ids"])
        self.assertEqual([task_id], scope["superseded_task_ids"])
        self.assertEqual(active_plan_id, scope["active_plan_revision_id"])
        self.assertIn(first_attempt, scope["preserved_attempt_ids"])
        self.assertTrue(scope["preserved_assessment_ids"])
        self.assertEqual("recovered", status["recovery"]["state"])

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                main(
                    [
                        "--db",
                        str(self.service.ledger.path),
                        "--artifacts",
                        str(self.service.ledger.artifact_root),
                        "status",
                        "--project-id",
                        self.project_id,
                    ]
                ),
            )
        cli_status = json.loads(output.getvalue())
        self.assertEqual(status["recovery"]["scope"], cli_status["recovery"]["scope"])
        self.assertEqual("recovered", cli_status["recovery"]["state"])

        self._queue_execution_preparation(new_tasks[0])
        self._run_until(RunOnceAction.MATERIALIZED)
        dispatched = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        binding = self._worker_binding(dispatched.attempt_id)
        self.app_file.write_text("value = 2\n", encoding="utf-8")
        self.runtime.complete(binding.thread_id, response="replanned task complete")
        self._run_until(RunOnceAction.VALIDATED)
        self._queue_goal_validation(new_tasks[0])
        completed = self._run_until(RunOnceAction.COMPLETED)
        if completed.goal_verdict_id is None:
            completed = self._run_until(RunOnceAction.COMPLETED)
        report = self.application.final_report(
            self.project_id, goal_verdict_id=completed.goal_verdict_id
        )
        self.assertEqual("satisfied", report.verdict.status.value)

    # --- FM-08-RECOVERY-C2 ---------------------------------------------

    def test_forged_final_response_code_does_not_start_recovery(self) -> None:
        """C2: provider error code 없이 본문에만 있는 모델 자기보고는 복구를 시작하지 않는다."""

        task_id = self._prepare()
        self._authorize()
        first_attempt, binding = self._dispatch_worker(task_id)
        self._fail_worker(
            first_attempt,
            binding,
            response="IMPLEMENTATION_ERROR: 모델이 스스로 보고한 코드입니다.",
            error_code=None,
        )

        blocked = self._settle()
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action, blocked)
        self.assertIn(
            blocked.blocker_code,
            {"RECOVERY_DIAGNOSIS_REQUIRED", "TASK_RECOVERY_REQUIRED"},
        )
        counts = self._ledger_counts()
        self.assertEqual(0, counts["assessments"])
        self.assertEqual(0, counts["recovery_history"])
        self.assertEqual(1, counts["attempts"])
        self.assertEqual(0, counts["replanning_jobs"])

        status = self.application.status(self.project_id)
        recovery = status["recovery"]
        self.assertEqual("user_decision_required", recovery["state"])
        self.assertEqual("unclassified", recovery["classification"]["failure_class"])
        self.assertEqual("unclassified", recovery["classification"]["basis"])
        self.assertEqual([], self._codes(recovery, "provider_observed"))
        model_reported = self._codes(recovery, "model_reported")
        self.assertEqual([["IMPLEMENTATION_ERROR"]], [item["values"] for item in model_reported])
        self.assertFalse(any(item["authoritative"] for item in model_reported))
        self.assertIn("진단 가설", model_reported[0]["note"])
        self.assertEqual("user_decision", recovery["next_action"]["mode"])
        self.assertEqual(
            "RECOVERY_DIAGNOSIS_REQUIRED", recovery["next_action"]["blocker_code"]
        )
        self.assertIn(first_attempt, recovery["scope"]["preserved_attempt_ids"])

        # 같은 tick을 다시 호출해도 자기보고만으로 상태가 바뀌지 않는다.
        again = self._settle()
        self.assertEqual(RunOnceAction.BLOCKED, again.action)
        self.assertEqual(counts, self._ledger_counts())

    def test_forged_json_code_and_self_declared_completion_are_not_authority(self) -> None:
        """C2: JSON 본문 code와 Worker의 완료 자칭도 Core 판정을 만들지 않는다."""

        task_id = self._prepare()
        self._authorize()
        attempt_id, binding = self._dispatch_worker(task_id)
        self._fail_worker(
            attempt_id,
            binding,
            response='{"error_code": "TASK_CONTRACT_INVALID", "status": "failed"}',
            error_code=None,
        )
        blocked = self._settle()
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action, blocked)
        counts = self._ledger_counts()
        self.assertEqual(0, counts["assessments"])
        self.assertEqual(0, counts["replanning_jobs"])
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual("unclassified", recovery["classification"]["failure_class"])
        self.assertEqual("user_decision", recovery["next_action"]["mode"])

    def test_worker_completion_claim_without_evidence_does_not_complete_the_task(self) -> None:
        """Worker의 완료 선언은 Task 완료가 아니며 복구 근거는 직접 검사 evidence다."""

        task_id = self._prepare()
        self._authorize()
        _attempt, binding = self._dispatch_worker(task_id)
        # 파일을 바꾸지 않고 완료만 자칭한다.
        self.runtime.complete(
            binding.thread_id, response='{"status": "completed", "result": "done"}'
        )
        self.assertEqual(RunOnceAction.OBSERVED, self.application.run_once(self.project_id).action)
        validated = self._run_until(RunOnceAction.VALIDATED)
        self.assertIsNotNone(validated.validation_result_id)
        blocked = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action, blocked)
        self.assertEqual("TASK_VALIDATION_FAILED", blocked.blocker_code)

        with self.service.ledger.read() as connection:
            task_status = connection.execute(
                "SELECT status FROM task_contracts WHERE id=?", (task_id,)
            ).fetchone()[0]
            results = connection.execute(
                "SELECT status FROM validation_results WHERE task_id=?", (task_id,)
            ).fetchall()
        self.assertNotEqual("completed", task_status)
        self.assertIn("fail", [row["status"] for row in results])
        self.assertEqual(0, self._ledger_counts()["assessments"])

        # Worker의 완료 선언은 provider code도 직접 실패 근거도 아니므로 자동 복구를
        # 시작하지 않고 실패 validation 근거를 요구하는 사용자 결정으로 멈춘다.
        stopped = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, stopped.action, stopped)
        self.assertEqual("TASK_VALIDATION_RECOVERY_REQUIRED", stopped.blocker_code)
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertIsNotNone(recovery["classification"])
        self.assertEqual("unclassified", recovery["classification"]["basis"])
        self.assertEqual(
            stopped.validation_result_id,
            recovery["classification"]["validation_result_id"],
        )
        self.assertEqual([], self._codes(recovery, "model_reported"))
        self.assertEqual("user_decision", recovery["next_action"]["mode"])
        self.assertEqual(
            "TASK_VALIDATION_RECOVERY_REQUIRED", recovery["next_action"]["blocker_code"]
        )
        self.assertEqual(0, self._ledger_counts()["recovery_history"])

    # --- 반복 차단 -----------------------------------------------------

    def test_repeated_failure_is_blocked_at_the_ledger_limit_with_a_shown_reason(self) -> None:
        """facade 경로의 반복 복구는 원장 한도에서 멈추고 status가 사유를 표시한다."""

        task_id = self._prepare()
        self._authorize()
        attempt_id, binding = self._dispatch_worker(task_id)
        self._fail_worker(
            attempt_id,
            binding,
            response="같은 결함이 반복됩니다.",
            error_code="IMPLEMENTATION_ERROR",
        )
        self.assertEqual(
            RunOnceAction.RECOVERED, self._run_until(RunOnceAction.RECOVERED).action
        )

        for ordinal in (2, 3):
            dispatched = self.application.run_once(self.project_id)
            self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
            retry_binding = self._worker_binding(dispatched.attempt_id)
            self._fail_worker(
                dispatched.attempt_id,
                retry_binding,
                response="같은 결함이 반복됩니다.",
                error_code="IMPLEMENTATION_ERROR",
            )
            outcome = self._settle()
            if ordinal == 2:
                self.assertEqual(RunOnceAction.RECOVERED, outcome.action, outcome)
            else:
                self.assertEqual(RunOnceAction.BLOCKED, outcome.action, outcome)
                self.assertEqual("SAME_FAILURE_RECOVERY_LIMIT", outcome.blocker_code)

        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual("user_decision_required", recovery["state"])
        self.assertEqual("user_decision", recovery["next_action"]["mode"])
        self.assertEqual(
            "SAME_FAILURE_RECOVERY_LIMIT", recovery["next_action"]["blocker_code"]
        )
        self.assertEqual("SAME_FAILURE_RECOVERY_LIMIT", recovery["limits"]["limit_code"])
        self.assertEqual(2, recovery["limits"]["task_recovery_count"])
        self.assertEqual(2, recovery["limits"]["max_task_recovery"])
        self.assertIn("상한", recovery["next_action"]["detail"])



#: 합성 subgraph fixture가 쓰는 원장 ID 형식의 프로젝트 ID.
_PROJECT_ID = new_id("project")


class SubgraphReplacementTests(unittest.TestCase):
    """실패 Task subgraph만 교체하고 나머지 Task 계약을 보존하는 결정적 규칙."""

    @staticmethod
    def _task(*, ref: str, validation_id: str, statement: str, produces, consumes) -> TaskContract:
        return TaskContract(
            task_id=new_id("task"),
            task_ref=ref,
            project_id=_PROJECT_ID,
            kind=TaskKind.CHANGE,
            objective=f"{ref}의 목적",
            goal_criterion_refs=("ac_one",),
            produces=produces,
            consumes=consumes,
            acceptance_criteria=("검사가 PASS다.",),
            validations=(
                ValidationContract(
                    validation_id=validation_id,
                    statement=statement,
                    method="deterministic",
                    required_evidence_kinds=("test",),
                ),
            ),
            risk_level=RiskLevel.LOW,
            approval_class=ApprovalClass.PLAN_ACTIVATION,
            recovery=RecoveryEnvelope(retryable_failure_classes=("implementation",)),
            assignment=assignment(),
        )

    def _revision(
        self,
        *,
        revision_no: int,
        first_validation: str,
        second_validation: str,
        goal_validation: str,
        statement: str,
        plan_id: str,
        supersedes: str | None = None,
    ) -> PlanContractRevision:
        first = self._task(
            ref="task_one",
            validation_id=first_validation,
            statement=statement,
            produces=("result:one",),
            consumes=("input:request",),
        )
        second = self._task(
            ref="task_two",
            validation_id=second_validation,
            statement=statement,
            produces=("result:two",),
            consumes=("result:one",),
        )
        definition = PlanContractDefinition(
            project_id=_PROJECT_ID,
            goal_contract_digest=sha256_digest("goal"),
            base_state_snapshot_digest=sha256_digest("state"),
            project_map_digest=sha256_digest("map"),
            source_skeleton_digest=sha256_digest("skeleton"),
            tasks=(first, second),
            dependencies=(
                PlanDependency(
                    producer_task_id=first.task_id,
                    consumer_task_id=second.task_id,
                    dependency_type=DependencyType.DATA,
                    products=("result:one",),
                ),
            ),
            goal_coverage=(
                PlanGoalCoverage(
                    criterion_id="ac_one",
                    task_ids=(first.task_id, second.task_id),
                    validation_ids=(first_validation, second_validation, goal_validation),
                ),
            ),
            integration_validations=(
                IntegrationValidationContract(
                    validation_id=goal_validation,
                    statement="Goal을 확인한다.",
                    evidence_mode="task_aggregate",
                    criterion_refs=("ac_one",),
                    method="deterministic",
                    required_evidence_kinds=("test",),
                ),
            ),
            model_inventory_digest=inventory().inventory_digest,
        )
        return PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=plan_id,
            revision_no=revision_no,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=supersedes,
            created_at=utc_now(),
        )

    def setUp(self) -> None:
        plan_id = new_id("plan")
        self.previous = self._revision(
            revision_no=1,
            first_validation="validation_one",
            second_validation="validation_two",
            goal_validation="validation_goal",
            statement="원래 검사 문장",
            plan_id=plan_id,
        )
        self.expanded = self._revision(
            revision_no=2,
            first_validation="validation_one_v2",
            second_validation="validation_two_v2",
            goal_validation="validation_goal_v2",
            statement="재계획 검사 문장",
            plan_id=plan_id,
            supersedes=self.previous.plan_revision_id,
        )

    def test_only_the_failed_task_and_its_dependents_are_in_the_subgraph(self) -> None:
        self.assertEqual(
            frozenset({"task_one", "task_two"}),
            failed_subgraph_task_refs(self.previous, failed_task_ref="task_one"),
        )
        self.assertEqual(
            frozenset({"task_two"}),
            failed_subgraph_task_refs(self.previous, failed_task_ref="task_two"),
        )

    def test_whole_graph_replacement_keeps_the_new_expansion(self) -> None:
        replaced = replace_failed_subgraph(
            previous=self.previous,
            expanded=self.expanded,
            replaced_task_refs=frozenset({"task_one", "task_two"}),
        )
        self.assertIs(self.expanded, replaced)

    def test_partial_replacement_preserves_the_untouched_task_contract(self) -> None:
        replaced = replace_failed_subgraph(
            previous=self.previous,
            expanded=self.expanded,
            replaced_task_refs=frozenset({"task_two"}),
        )
        by_ref = {item.task_ref: item for item in replaced.definition.tasks}
        expanded_by_ref = {item.task_ref: item for item in self.expanded.definition.tasks}
        previous_by_ref = {item.task_ref: item for item in self.previous.definition.tasks}

        # 보존 Task는 직전 revision의 계약 의미를 유지하되 새 원장 ID를 받는다.
        self.assertEqual(
            ("validation_one",),
            tuple(item.validation_id for item in by_ref["task_one"].validations),
        )
        self.assertEqual(
            previous_by_ref["task_one"].validations[0].statement,
            by_ref["task_one"].validations[0].statement,
        )
        self.assertEqual(expanded_by_ref["task_one"].task_id, by_ref["task_one"].task_id)
        self.assertNotEqual(previous_by_ref["task_one"].task_id, by_ref["task_one"].task_id)

        # 실패 subgraph Task만 새 상세화를 취한다.
        self.assertEqual(
            ("validation_two_v2",),
            tuple(item.validation_id for item in by_ref["task_two"].validations),
        )
        self.assertEqual("재계획 검사 문장", by_ref["task_two"].validations[0].statement)

        self.assertEqual(
            ("validation_goal",),
            tuple(
                item.validation_id
                for item in replaced.definition.integration_validations
            ),
        )
        self.assertEqual(
            ("validation_one", "validation_goal", "validation_two_v2"),
            replaced.definition.goal_coverage[0].validation_ids,
        )
        self.assertEqual(
            replaced.definition.definition_digest, replaced.definition_digest
        )
        self.assertEqual(
            self.previous.plan_revision_id, replaced.supersedes_plan_revision_id
        )

    def test_task_set_drift_is_rejected(self) -> None:
        drifted = self.expanded.definition.model_copy(
            update={
                "tasks": self.expanded.definition.tasks[:1],
                "dependencies": (),
                "goal_coverage": (
                    self.expanded.definition.goal_coverage[0].model_copy(
                        update={
                            "task_ids": (self.expanded.definition.tasks[0].task_id,),
                        }
                    ),
                ),
            }
        )
        with self.assertRaises(RecoveryPlanError):
            replace_failed_subgraph(
                previous=self.previous,
                expanded=self.expanded.model_copy(update={"definition": drifted}),
                replaced_task_refs=frozenset({"task_two"}),
            )


if __name__ == "__main__":
    unittest.main()
