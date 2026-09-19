from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.application import ApplicationAuthority, EngineApplication
from flowmarshal.engine.cli import main
from flowmarshal.engine.domain import RunOnceAction, ThreadBinding
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.models import EngineRoleConfiguration, RoleModelBinding
from flowmarshal.engine.runtime import FakeCodexRuntime
from flowmarshal.engine.service import EngineService
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from tests.engine_helpers import inventory, profile
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
            {
                "tasks": [
                    {
                        "task_ref": "single_task",
                        "kind": "change",
                        "objective": "app.py 값을 2로 바꾼다.",
                        "goal_criterion_refs": ["ac_001"],
                        "produces": ["result:app_value"],
                        "consumes": ["input:request"],
                        "acceptance_criteria": ["Task 검사가 PASS다."],
                        "validations": [
                            {
                                "validation_id": "validation_task",
                                "statement": "app.py의 값이 2인지 실행 검사한다.",
                                "method": "deterministic",
                                "required_evidence_kinds": ["test"],
                            }
                        ],
                        "risk_level": "low",
                        "recovery": {
                            "retryable_failure_classes": ["implementation", "context"]
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
        ],
        "compact_plan_reviewer": [{"findings": [], "ratings": _ratings()}],
    }


class EngineFm08CoreIntegrationTests(unittest.TestCase):
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
            SQLiteEngineLedger(base / "state" / "engine.sqlite3", artifact_root=base / "artifacts")
        )
        self.service.initialize()
        self.project_id = self.service.create_project(name="fm08", root=self.root)
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
        self.addCleanup(self._close_supervisors)
        self.supervisors = [self.application.supervisor]

    def _close_supervisors(self) -> None:
        for supervisor in self.supervisors:
            if supervisor is not None:
                supervisor.close(timeout_seconds=0.1)

    def _prepare(self) -> tuple[str, dict]:
        prepared = self.application.prepare(
            self.project_id,
            source_request="app.py의 value를 2로 바꾸고 독립적으로 검증해 주세요.",
        )
        self.assertEqual("ready_for_authorization", prepared.status)
        plan = prepared.planning.plan_evaluations[0].plan
        self.assertEqual(1, len(plan.definition.tasks))
        return plan.definition.tasks[0].task_id, prepared.model_dump(mode="json")

    def _authorize(self) -> None:
        target = self.authority.authorization_target(self.project_id)
        self.authority.authorize(
            self.project_id,
            target=target,
            source="fm08-test-user",
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
                            "expected_content_digest": sha256_bytes(self.app_file.read_bytes()),
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
                    "idempotency_hint": "fm08-single-task",
                },
                "context_request": None,
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

    def test_raw_request_reaches_one_task_independent_validator_and_goal_verdict(self) -> None:
        task_id, _prepared = self._prepare()
        self._authorize()
        self._queue_execution_preparation(task_id)

        self._run_until(RunOnceAction.MATERIALIZED)
        dispatched = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        worker_binding = self._worker_binding(dispatched.attempt_id)
        self.app_file.write_text("value = 2\n", encoding="utf-8")
        self.runtime.complete(worker_binding.thread_id, response="single task complete")

        observed = self.application.observe(self.project_id)
        self.assertEqual(self.project_id, observed["project_id"])
        self._run_until(RunOnceAction.VALIDATED)
        with self.service.ledger.read() as connection:
            task_evidence_id = connection.execute(
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
                "evidence_refs": [task_evidence_id],
            }
        )

        completed = self._run_until(RunOnceAction.COMPLETED)
        if completed.goal_verdict_id is None:
            completed = self._run_until(RunOnceAction.COMPLETED)
        self.assertIsNotNone(completed.goal_verdict_id)
        status = self.application.status(self.project_id)
        report = self.application.final_report(
            self.project_id, goal_verdict_id=completed.goal_verdict_id
        )

        with self.service.ledger.read() as connection:
            tasks = connection.execute(
                "SELECT id,status FROM task_contracts WHERE project_id=?", (self.project_id,)
            ).fetchall()
            worker_attempt = connection.execute(
                "SELECT id,binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
            ).fetchone()
            validator_attempt = connection.execute(
                "SELECT a.id,a.binding_json FROM attempts a JOIN runtime_jobs j "
                "ON j.attempt_id=a.id WHERE a.project_id=? "
                "AND j.kind='goal_semantic_validate' ORDER BY j.rowid DESC LIMIT 1",
                (self.project_id,),
            ).fetchone()
        self.assertEqual([(task_id, "completed")], [tuple(row) for row in tasks])
        self.assertNotEqual(worker_attempt["id"], validator_attempt["id"])
        self.assertNotEqual(
            ThreadBinding.model_validate_json(worker_attempt["binding_json"]).thread_id,
            ThreadBinding.model_validate_json(validator_attempt["binding_json"]).thread_id,
        )
        self.assertEqual("completed", status["current_stage"])
        self.assertEqual("satisfied", report.verdict.status.value)
        self.assertTrue(report.ledger_history_valid)
        self.assertTrue(report.execution_summary.model_observations)
        self.assertTrue(
            all(item.observed_model is None for item in report.execution_summary.model_observations)
        )
        self.assertEqual("missing", report.execution_summary.usage_status)
        self.assertEqual("none", report.execution_summary.external_effect_status)

        common = [
            "--db",
            str(self.service.ledger.path),
            "--artifacts",
            str(self.service.ledger.artifact_root),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                main(common + ["status", "--project-id", self.project_id]),
            )
        cli_status = json.loads(output.getvalue())
        self.assertEqual("completed", cli_status["current_stage"])
        self.assertIn("execution_summary", cli_status)
        self.assertEqual("preserved", cli_status["drill_down"]["tasks"][0]["record_status"])

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                main(
                    common
                    + [
                        "final-report",
                        "--project-id",
                        self.project_id,
                        "--goal-verdict-id",
                        completed.goal_verdict_id,
                        "--format",
                        "markdown",
                    ]
                ),
            )
        self.assertIn("## 실행 관측 축", output.getvalue())
        self.assertIn("external effect: `none`", output.getvalue())

    def test_status_pairs_approval_and_stale_with_preservation_reasons(self) -> None:
        task_id, _prepared = self._prepare()
        before = self.application.status(self.project_id)
        self.assertEqual("authorization_required", before["current_stage"])
        self.assertEqual("required", before["authorization_state"])
        self.assertEqual("preserved", before["drill_down"]["tasks"][0]["record_status"])
        self.assertEqual("current", before["drill_down"]["tasks"][0]["validity"])
        self.assertEqual("pending", before["drill_down"]["checks"][0]["validity"])

        self._authorize()
        approved = self.application.status(self.project_id)
        self.assertEqual("task_ready", approved["current_stage"])
        self.assertEqual("authorized", approved["authorization_state"])
        self.assertEqual(
            [item["task_id"] for item in before["drill_down"]["tasks"]],
            [item["task_id"] for item in approved["drill_down"]["tasks"]],
        )
        self.assertEqual(task_id, approved["drill_down"]["tasks"][0]["task_id"])

        self.app_file.write_text("value = 99\n", encoding="utf-8")
        stale = self.application.status(self.project_id)
        self.assertEqual("stale_execution_input", stale["current_stage"])
        self.assertIn("Execution Spec", stale["next_action"])
        self.assertEqual("preserved", stale["drill_down"]["tasks"][0]["record_status"])
        self.assertEqual("invalidated", stale["drill_down"]["tasks"][0]["validity"])
        self.assertTrue(
            all(item["validity"] == "invalidated" for item in stale["drill_down"]["checks"])
        )
        changed_file = next(
            item for item in stale["drill_down"]["files"] if item["path"] == "app.py"
        )
        self.assertEqual("preserved", changed_file["record_status"])
        self.assertEqual("invalidated", changed_file["validity"])
        self.assertNotEqual(changed_file["expected_digest"], changed_file["observed_digest"])

    def test_restart_status_keeps_active_job_and_requires_observe_first(self) -> None:
        task_id, _prepared = self._prepare()
        self._authorize()
        self._queue_execution_preparation(task_id)
        self._run_until(RunOnceAction.MATERIALIZED)
        dispatched = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        self._worker_binding(dispatched.attempt_id)
        before_calls = (self.runtime.create_calls, self.runtime.turn_calls)
        before = self.application.status(self.project_id)

        restarted = EngineApplication(
            self.service,
            runtime=self.runtime,
            role_configuration=_roles(),
            structured_runner=self.runner,
            governance=ALLOW_ALL,
        )
        self.supervisors.append(restarted.supervisor)
        after = restarted.status(self.project_id)

        self.assertEqual("executing_task", before["current_stage"])
        self.assertEqual(before["current_stage"], after["current_stage"])
        self.assertEqual(before["active_runtime_job"]["job_id"], after["active_runtime_job"]["job_id"])
        self.assertEqual(before["drill_down"], after["drill_down"])
        self.assertIn("observe", after["next_action"])
        self.assertEqual(before_calls, (self.runtime.create_calls, self.runtime.turn_calls))


if __name__ == "__main__":
    unittest.main()
