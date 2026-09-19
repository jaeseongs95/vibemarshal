from __future__ import annotations

import tempfile
import unittest
import io
import json
import time
from contextlib import redirect_stdout
from pathlib import Path

from flowmarshal.canonical import canonical_json, sha256_bytes
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.cli import main
from flowmarshal.engine.domain import (
    CriterionVerdict,
    EvidenceKind,
    EvidenceRecord,
    GoalVerdict,
    GoalVerdictStatus,
    RunOnceAction,
    ValidationStatus,
    ThreadBinding,
    new_id,
    utc_now,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.models import EngineRoleConfiguration, RoleModelBinding
from flowmarshal.engine.runtime import FakeCodexRuntime
from flowmarshal.engine.service import EngineService
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from tests.test_engine_ledger_service import TrustedTestEngineService
from tests.engine_helpers import goal, inventory, plan, profile, project_map, skeleton, state
from tests.engine_inspection_helpers import InspectionScriptedRunner
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.qualification import default_role_configuration
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]


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
                "observable_outcome": "두 단계 변경이 검증된다.",
                "hard_acceptance": [
                    {
                        "statement": "두 Task 결과가 통합 검증된다.",
                        "validation_intent": "각 Task 검사와 독립 Goal 검사를 실행한다.",
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
                            "change_shape": "serial-two-task",
                            "compatibility": "preserve",
                            "rollout_recovery": "bounded retry",
                        },
                        "tasks": [
                            {
                                "task_ref": "task_one",
                                "kind": "change",
                                "objective": "첫 결과를 만든다.",
                                "contributes_to": ["ac_001"],
                                "produces": ["result:one"],
                                "consumes": ["input:request"],
                            },
                            {
                                "task_ref": "task_two",
                                "kind": "change",
                                "objective": "첫 결과를 사용해 최종 결과를 만든다.",
                                "contributes_to": ["ac_001"],
                                "produces": ["result:two"],
                                "consumes": ["result:one"],
                            },
                        ],
                        "dependencies": [
                            {
                                "producer_task_ref": "task_one",
                                "consumer_task_ref": "task_two",
                                "dependency_type": "data",
                                "produces": ["result:one"],
                                "consumes": ["result:one"],
                            }
                        ],
                        "goal_coverage": [
                            {
                                "criterion_id": "ac_001",
                                "task_refs": ["task_one", "task_two"],
                            }
                        ],
                        "unknowns": [],
                        "estimated_change_cost": 2,
                        "estimated_context_tokens": 200,
                    }
                ]
            }
        ],
        "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
        "plan_expander": [
            {
                "tasks": [
                    {
                        "task_ref": "task_one",
                        "kind": "change",
                        "objective": "첫 결과를 만든다.",
                        "goal_criterion_refs": ["ac_001"],
                        "produces": ["result:one"],
                        "consumes": ["input:request"],
                        "acceptance_criteria": ["첫 Task 검사가 PASS다."],
                        "validations": [
                            {
                                "validation_id": "validation_one",
                                "statement": "첫 결과를 검사한다.",
                                "method": "deterministic",
                                "required_evidence_kinds": ["test"],
                            }
                        ],
                        "risk_level": "low",
                        "recovery": {
                            "retryable_failure_classes": ["implementation", "context"]
                        },
                    },
                    {
                        "task_ref": "task_two",
                        "kind": "change",
                        "objective": "첫 결과를 사용해 최종 결과를 만든다.",
                        "goal_criterion_refs": ["ac_001"],
                        "produces": ["result:two"],
                        "consumes": ["result:one"],
                        "acceptance_criteria": ["둘째 Task 검사가 PASS다."],
                        "validations": [
                            {
                                "validation_id": "validation_two",
                                "statement": "최종 결과를 검사한다.",
                                "method": "deterministic",
                                "required_evidence_kinds": ["test"],
                            }
                        ],
                        "risk_level": "low",
                        "recovery": {
                            "retryable_failure_classes": ["implementation", "context"]
                        },
                    },
                ],
                "dependencies": [
                    {
                        "producer_task_ref": "task_one",
                        "consumer_task_ref": "task_two",
                        "dependency_type": "data",
                        "products": ["result:one"],
                    }
                ],
                "goal_coverage": [
                    {
                        "criterion_id": "ac_001",
                        "task_refs": ["task_one", "task_two"],
                        "validation_ids": [
                            "validation_one",
                            "validation_two",
                            "validation_goal",
                        ],
                    }
                ],
                "integration_validations": [
                    {
                        "validation_id": "validation_goal",
                        "statement": "두 Task 결과를 독립 통합 검사한다.",
                        "criterion_refs": ["ac_001"],
                        "method": "deterministic",
                        "required_evidence_kinds": ["test"],
                    }
                ],
            }
        ],
        "compact_plan_reviewer": [{"findings": [], "ratings": _ratings()}],
    }


class EngineUserFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("테스트 지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.db = base / "state" / "engine.sqlite3"
        self.artifacts = base / "artifacts"
        self.service = TrustedTestEngineService(SQLiteEngineLedger(self.db, artifact_root=self.artifacts))
        self.service.initialize()
        self.project_id = self.service.create_project(name="facade", root=self.root)
        self.service.register_profile(profile(self.project_id))
        self.runtime = FakeCodexRuntime(inventory())

    def application(self, runner=None) -> EngineApplication:
        return EngineApplication(
            self.service,
            runtime=self.runtime,
            role_configuration=_roles(),
            structured_runner=runner,
            governance=ALLOW_ALL,
        )

    def test_prepare_uses_real_goal_and_planning_roles_then_authorizes_selected_plan(self) -> None:
        runner = InspectionScriptedRunner(_responses())
        application = self.application(runner)

        prepared = application.prepare(
            self.project_id,
            source_request="두 단계 변경을 구현하고 각각 검증해 주세요.",
        )

        self.assertEqual("ready_for_authorization", prepared.status)
        self.assertEqual(2, len(prepared.planning.plan_evaluations[0].plan.definition.tasks))
        self.assertEqual(
            [
                "goal_normalizer",
                "goal_reviewer",
                "skeleton_generator",
                "skeleton_reviewer",
                "plan_expander",
                "compact_plan_reviewer",
            ],
            [call.role for call in runner.calls],
        )
        authorized = application.authorize(self.project_id, source="test-user")
        self.assertTrue(authorized.activation_id.startswith("activation_"))
        self.assertEqual("active", application.status(self.project_id)["project"]["run_state"])

    def test_pause_resume_cancel_and_duplicate_run_once_are_consistent(self) -> None:
        runner = InspectionScriptedRunner(_responses())
        application = self.application(runner)
        application.prepare(self.project_id, source_request="두 단계 변경을 구현해 주세요.")
        application.authorize(self.project_id, source="test-user")
        control_application = EngineApplication(self.service, runtime=self.runtime)

        first_pause = application.pause(self.project_id, reason="검토")
        second_pause = application.pause(self.project_id, reason="검토")
        self.assertEqual(first_pause, second_pause)
        blocked = application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
        self.assertEqual("WORKFLOW_PAUSED", blocked.blocker_code)

        resumed = control_application.run_once(self.project_id, resume=True)
        self.assertEqual(RunOnceAction.BLOCKED, resumed.action)
        self.assertNotEqual("WORKFLOW_PAUSED", resumed.blocker_code)
        cancelled = control_application.cancel(self.project_id, reason="사용자 취소")
        self.assertEqual("cancelled", cancelled["control_state"])
        duplicate = control_application.run_once(self.project_id)
        self.assertEqual("WORKFLOW_CANCELLED", duplicate.blocker_code)
        self.assertEqual("cancelled", application.status(self.project_id)["control_state"])

    def test_cli_pause_status_and_cancel_use_the_same_durable_control_state(self) -> None:
        runner = InspectionScriptedRunner(_responses())
        application = self.application(runner)
        application.prepare(self.project_id, source_request="두 단계 변경을 구현해 주세요.")
        application.authorize(self.project_id, source="test-user")
        common = ["--db", str(self.db), "--artifacts", str(self.artifacts)]

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                main(common + ["pause", "--project-id", self.project_id, "--reason", "검토"]),
            )
        self.assertEqual("paused", json.loads(output.getvalue())["control_state"])

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(common + ["status", "--project-id", self.project_id]))
        self.assertEqual("paused", json.loads(output.getvalue())["control_state"])

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(common + ["cancel", "--project-id", self.project_id]))
        self.assertEqual("cancelled", json.loads(output.getvalue())["control_state"])

    def test_read_only_final_report_checks_all_criteria_evidence_and_source_digest(self) -> None:
        read_goal = goal(self.project_id, profile(self.project_id).definition_digest, read_only=True)
        self.service.register_goal(read_goal)
        mapped = project_map(self.project_id, self.root)
        self.service.record_project_map(mapped)
        snapshot = state(self.project_id, read_goal.definition_digest, mapped.revision_digest)
        source = skeleton(read_goal, snapshot)
        planned, _task, _decision = plan(
            self.project_id,
            read_goal,
            snapshot,
            mapped.revision_digest,
            source,
            inventory(),
        )
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO plan_revisions "
                "(id,plan_id,project_id,revision_no,definition_digest,activation_digest,"
                "payload_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    planned.plan_revision_id,
                    planned.plan_id,
                    self.project_id,
                    planned.revision_no,
                    planned.definition_digest,
                    planned.activation_digest,
                    canonical_json(planned),
                    "ready",
                    planned.created_at.isoformat(),
                ),
            )
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=self.project_id,
            kind=EvidenceKind.TEST,
            source_ref="read-only-observation",
            observation="모든 AC를 직접 확인했다.",
            content_digest=sha256_bytes(b"read-only-evidence"),
            observed_at=utc_now(),
        )
        self.service.record_evidence(evidence)
        verdict = GoalVerdict(
            goal_verdict_id=new_id("goal_verdict"),
            goal_contract_digest=read_goal.definition_digest,
            plan_activation_digest=planned.activation_digest,
            status=GoalVerdictStatus.SATISFIED,
            criteria=(
                CriterionVerdict(
                    criterion_id="ac_one",
                    status=ValidationStatus.PASS,
                    evidence_ids=(evidence.evidence_id,),
                    rationale="직접 evidence로 확인했다.",
                ),
            ),
            integration_validation_result_ids=(new_id("validation_result"),),
            evaluated_at=utc_now(),
        )
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO goal_verdicts "
                "(id,project_id,plan_revision_id,goal_contract_digest,status,payload_json,evaluated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    verdict.goal_verdict_id,
                    self.project_id,
                    planned.plan_revision_id,
                    verdict.goal_contract_digest,
                    verdict.status.value,
                    canonical_json(verdict),
                    verdict.evaluated_at.isoformat(),
                ),
            )

        report = EngineApplication(self.service).final_report(self.project_id)
        self.assertTrue(report.read_only_verification.criteria_complete)
        self.assertTrue(report.read_only_verification.evidence_grounded)
        self.assertTrue(report.read_only_verification.source_unchanged)

        (self.root / "app.py").write_text("value = 2\n", encoding="utf-8")
        changed = EngineApplication(self.service).final_report(self.project_id)
        self.assertFalse(changed.read_only_verification.source_unchanged)
        self.assertEqual("READ_ONLY_REPORT_VERIFICATION_FAILED", changed.error_code)

    def test_automatic_recovery_revalidation_and_goal_verdict_finish_through_facade(self) -> None:
        base = Path(self.temp.name) / "recovery"
        base.mkdir()
        workspace, _ = _copy_fixture(ROOT, base)
        qualification_models = qualification_inventory()
        prepared = _prepare(
            workspace=workspace,
            state_root=base / "state",
            inventory=qualification_models,
            roles=default_role_configuration(ROOT),
        )
        runtime = FakeCodexRuntime(qualification_models)
        application = EngineApplication(prepared.service, runtime=runtime, governance=ALLOW_ALL)

        materialized = application.run_once(
            prepared.project_id, proposal=prepared.proposal
        )
        self.assertEqual(RunOnceAction.DISPATCHED, materialized.action)
        for _ in range(20):
            if materialized.action is RunOnceAction.MATERIALIZED:
                break
            time.sleep(0.01)
            materialized = application.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.MATERIALIZED, materialized.action)
        first = application.run_once(prepared.project_id)
        deadline = time.monotonic() + 2
        while True:
            with prepared.service.ledger.read() as connection:
                raw_binding = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id=?", (first.attempt_id,)
                ).fetchone()["binding_json"]
            binding = (
                None if raw_binding is None
                else ThreadBinding.model_validate_json(raw_binding)
            )
            if (binding is not None and binding.turn_id is not None) or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        runtime.fail(
            binding.thread_id,
            response="IMPLEMENTATION_ERROR: injected fault",
            error_code="IMPLEMENTATION_ERROR",
        )
        self.assertEqual(RunOnceAction.OBSERVED, application.run_once(prepared.project_id).action)
        recovered = application.run_once(prepared.project_id)
        for _ in range(20):
            if recovered.action is RunOnceAction.RECOVERED:
                break
            time.sleep(0.01)
            recovered = application.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, recovered.action)

        second = application.run_once(prepared.project_id)
        deadline = time.monotonic() + 2
        while True:
            with prepared.service.ledger.read() as connection:
                raw_binding = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id=?", (second.attempt_id,)
                ).fetchone()["binding_json"]
            binding = (
                None if raw_binding is None
                else ThreadBinding.model_validate_json(raw_binding)
            )
            if (binding is not None and binding.turn_id is not None) or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        (workspace / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n",
            encoding="utf-8",
        )
        runtime.complete(binding.thread_id, response="repair complete")
        self.assertEqual(RunOnceAction.OBSERVED, application.run_once(prepared.project_id).action)
        validated = application.run_once(prepared.project_id)
        for _ in range(10):
            if validated.action is RunOnceAction.VALIDATED:
                break
            time.sleep(0.01)
            validated = application.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.VALIDATED, validated.action)
        goal_validation_step = prepared.proposal.validation_steps[0].model_copy(
            update={"validation_id": "validation_goal"}
        )
        completed = application.run_once(
            prepared.project_id, goal_validation_step=goal_validation_step
        )
        self.assertEqual(RunOnceAction.COMPLETED, completed.action)
        for _ in range(4):
            if completed.goal_verdict_id is not None:
                break
            completed = application.run_once(
                prepared.project_id, goal_validation_step=goal_validation_step
            )
        self.assertEqual(RunOnceAction.COMPLETED, completed.action)
        self.assertIsNotNone(completed.goal_verdict_id)
        self.assertIsNotNone(
            application.final_report(
                prepared.project_id, goal_verdict_id=completed.goal_verdict_id
            ).verdict
        )


if __name__ == "__main__":
    unittest.main()
