from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.context import ProjectMapper
from flowmarshal.engine.domain import CandidateStatus, GoalCriterion, GoalContractRevision, PlanningBudgetPolicy, SourceTrace
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter,
    PlanReviewerAdapter,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter,
)
from flowmarshal.engine.planning import SkeletonFirstPlanner
from tests.engine_inspection_helpers import InspectionScriptedRunner as ScriptedStructuredRoleRunner
from flowmarshal.engine.service import EngineService, EngineServiceError

from tests.engine_helpers import assignment, goal, inventory, profile, state


def _ratings() -> dict[str, int]:
    return {
        "goal_fit": 4,
        "grounding": 4,
        "engineering": 4,
        "verification": 4,
        "execution_safety": 4,
    }


def _skeleton_response() -> dict[str, object]:
    return {
        "candidates": [{
            "approach": {
                "strategy_family": "synthetic_contract",
                "change_shape": "single",
                "compatibility": "preserve",
                "rollout_recovery": "bounded retry",
            },
            "tasks": [{
                "task_ref": "task_change",
                "kind": "change",
                "objective": "합성 함수의 구현과 검증 계약을 보존한다.",
                "contributes_to": ["ac_unittest", "ac_validator"],
                "produces": ["result:synthetic_change"],
                "consumes": ["input:request"],
            }],
            "dependencies": [],
            "goal_coverage": [
                {"criterion_id": "ac_unittest", "task_refs": ["task_change"]},
                {"criterion_id": "ac_validator", "task_refs": ["task_change"]},
            ],
            "unknowns": [],
            "estimated_change_cost": 1,
            "estimated_context_tokens": 100,
        }],
    }


def _plan_response(
    *,
    omit_composite_trace: bool,
    worker_requires_future_validator_result: bool,
) -> dict[str, object]:
    unittest_ids = ["val_task_unittest", "val_goal_test"]
    if not omit_composite_trace:
        unittest_ids.insert(0, "val_task_composite")
    worker_condition = (
        "Worker 응답이 이후 분리 Validator의 직접 검토 결과를 포함한다."
        if worker_requires_future_validator_result
        else "Worker가 직접 실행 관측을 응답으로 제출하고, 분리 Validator 검토가 통과한다."
    )
    return {
        "tasks": [{
            "task_ref": "task_change",
            "kind": "change",
            "objective": "합성 함수의 구현과 검증 계약을 보존한다.",
            "goal_criterion_refs": ["ac_unittest", "ac_validator"],
            "produces": ["result:synthetic_change"],
            "consumes": ["input:request"],
            "acceptance_criteria": [worker_condition],
            "validations": [
                {
                    "validation_id": "val_task_composite",
                    "statement": "Task phase의 복합 검사가 파일 범위와 기존 unittest를 실제 실행해 통과 여부를 확인한다.",
                    "method": "deterministic",
                    "required_evidence_kinds": ["file", "diff", "command", "test"],
                },
                {
                    "validation_id": "val_task_unittest",
                    "statement": "기존 unittest를 새 프로세스로 실제 실행해 통과 여부를 확인한다.",
                    "method": "deterministic",
                    "required_evidence_kinds": ["command", "test"],
                },
                {
                    "validation_id": "val_task_validator",
                    "statement": "분리 Validator가 Worker 응답의 직접 실행 관측과 file evidence를 입력으로 의미 검토하고 별도 결과를 제출한다.",
                    "method": "semantic",
                    "required_evidence_kinds": ["model_review", "external_observation", "file"],
                },
            ],
            "risk_level": "low",
            "recovery": {"retryable_failure_classes": ["implementation"]},
        }],
        "dependencies": [],
        "goal_coverage": [
            {
                "criterion_id": "ac_unittest",
                "task_refs": ["task_change"],
                "validation_ids": unittest_ids,
            },
            {
                "criterion_id": "ac_validator",
                "task_refs": ["task_change"],
                "validation_ids": ["val_task_validator"],
            },
        ],
        "integration_validations": [{
            "validation_id": "val_goal_test",
            "statement": "모든 Task 검증 뒤 독립 Goal Test를 새로 실행한다.",
            "criterion_refs": ["ac_unittest"],
            "evidence_mode": "independent",
            "method": "deterministic",
            "required_evidence_kinds": ["command", "test", "file", "diff"],
        }],
    }


class PlanResultBoundaryRegressionTests(unittest.TestCase):
    """Scripted finding의 입력·Compiler·Core 경계만 검증한다.

    이 검사는 실제 역할이 의미 결함을 탐지하는 능력을 주장하지 않는다. 합성 Draft가
    자동 보정되지 않고, scripted Reviewer finding이 Core의 활성화 차단으로 전달되는지만 확인한다.
    """

    def _goal(self, project_id: str, profile_digest: str) -> GoalContractRevision:
        request = "합성 변경의 검사 ID 연결과 Worker·Validator 결과 경계를 검증한다."
        request_digest = sha256_bytes(request.encode("utf-8"))
        base = goal(project_id, profile_digest)
        definition = base.definition.model_copy(update={
            "source_request": request,
            "source_request_digest": request_digest,
            "source_traces": (
                SourceTrace(
                    trace_id="trace_synthetic",
                    source_ref="synthetic-request",
                    statement=request,
                    source_digest=request_digest,
                ),
            ),
            "hard_acceptance": (
                GoalCriterion(
                    criterion_id="ac_unittest",
                    statement="각 적용 Task의 기존 unittest 실행이 추적된다.",
                    validation_intent="복합 Task 검사와 별도 unittest 검사, 독립 Goal Test의 검사 ID를 연결한다.",
                    trace_refs=("trace_synthetic",),
                ),
                GoalCriterion(
                    criterion_id="ac_validator",
                    statement="Worker 관측을 분리 Validator가 검토한다.",
                    validation_intent="Worker 보고를 입력으로 한 후속 semantic validation이 별도 결과를 제출한다.",
                    trace_refs=("trace_synthetic",),
                ),
            ),
        })
        return base.model_copy(update={
            "definition": definition,
            "definition_digest": definition.definition_digest,
        })

    def _run_case(
        self,
        *,
        omit_composite_trace: bool,
        worker_requires_future_validator_result: bool,
        finding_code: str | None,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "synthetic-workspace"
        workspace.mkdir()
        (workspace / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n",
            encoding="utf-8",
        )
        (workspace / "test_app.py").write_text("# 합성 테스트 자료\n", encoding="utf-8")
        (workspace / "AGENTS.md").write_text("# 합성 회귀 자료\n", encoding="utf-8")

        service = EngineService(SQLiteEngineLedger(root / "state.sqlite3"))
        service.initialize()
        project_id = service.create_project(name="합성 경계 회귀", root=workspace)
        project_profile = profile(project_id)
        service.register_profile(project_profile)
        contract = self._goal(project_id, project_profile.definition_digest)
        service.register_goal(contract)
        project_map = ProjectMapper().build(project_id=project_id, root=workspace, revision_no=1)
        service.record_project_map(project_map)
        snapshot = state(project_id, contract.definition_digest, project_map.revision_digest)
        service.record_state_snapshot(snapshot)

        review: dict[str, object] = {"findings": [], "ratings": _ratings()}
        if finding_code is not None:
            review = {
                "findings": [{
                    "finding_code": finding_code,
                    "gate": "verification",
                    "severity": "error",
                    "summary": "합성 Plan의 직접 계약 결함이다.",
                    "evidence_refs": ["artifact:plan_contract", "source:goal"],
                    "affected_task_refs": ["task_change"],
                    "remediable": True,
                }],
                "ratings": None,
            }
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [_skeleton_response()],
            "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
            "plan_expander": [_plan_response(
                omit_composite_trace=omit_composite_trace,
                worker_requires_future_validator_result=worker_requires_future_validator_result,
            )],
            "compact_plan_reviewer": [review],
        })
        models = inventory()
        options = {
            "model": "worker",
            "effort": "medium",
            "inventory_digest": models.inventory_digest,
            "cwd": workspace,
        }
        outcome = SkeletonFirstPlanner(
            SkeletonGeneratorAdapter(runner, **options),
            SkeletonReviewerAdapter(runner, **{**options, "model": "validator", "effort": "high"}),
            PlanExpanderAdapter(
                runner,
                RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
                **options,
            ),
            PlanReviewerAdapter(runner, **{**options, "model": "validator", "effort": "high"}),
        ).search(goal=contract, state=snapshot, project_map=project_map, candidate_count=1,
                 budget=PlanningBudgetPolicy(max_refinement_per_candidate=0))
        evaluation = outcome.plan_evaluations[0]
        service.record_skeleton_evaluation(outcome.skeleton_evaluations[0])
        service.register_plan_evaluation(evaluation)
        return service, outcome, runner, evaluation

    def test_trace_and_worker_validator_boundaries_reach_reviewer_and_core(self) -> None:
        cases = (
            (False, False, None),
            (True, False, "PLAN_VALIDATION_COVERAGE_LINK_MISSING"),
            (False, True, "PLAN_VALIDATOR_RESULT_IN_WORKER_RESPONSE"),
        )
        for omit_trace, future_validator_result, finding_code in cases:
            with self.subTest(
                omit_composite_trace=omit_trace,
                worker_requires_future_validator_result=future_validator_result,
            ):
                service, outcome, runner, evaluation = self._run_case(
                    omit_composite_trace=omit_trace,
                    worker_requires_future_validator_result=future_validator_result,
                    finding_code=finding_code,
                )
                task = evaluation.plan.definition.tasks[0]
                self.assertEqual(
                    CandidateStatus.ADMISSIBLE if finding_code is None else CandidateStatus.NEEDS_REVISION,
                    evaluation.decision.status,
                )
                if finding_code is None:
                    self.assertEqual((), evaluation.decision.finding_codes)
                else:
                    self.assertIn(finding_code, evaluation.decision.finding_codes)

                calls = {call.role: call for call in runner.calls}
                self.assertIn("복합 검사 ID", calls["plan_expander"].instructions)
                self.assertIn("후속 Validator 결과", calls["compact_plan_reviewer"].instructions)
                self.assertEqual(
                    "acceptance_criterion",
                    calls["plan_expander"].payload["goal_validation_requirement_rows"][0]["source_kind"],
                )
                self.assertEqual(
                    "ac_unittest",
                    calls["compact_plan_reviewer"].payload[
                        "goal_validation_requirement_rows"
                    ][0]["source_id"],
                )
                coverage = next(
                    item for item in calls["compact_plan_reviewer"].payload[
                        "evidence_catalog"
                    ]["artifact:plan_contract"]["definition"]["goal_coverage"]
                    if item["criterion_id"] == "ac_unittest"
                )
                expected_ids = ["val_task_unittest", "val_goal_test"]
                if not omit_trace:
                    expected_ids.insert(0, "val_task_composite")
                self.assertEqual(expected_ids, coverage["validation_ids"])
                self.assertIn("기존 unittest를 실제 실행", task.validations[0].statement)
                self.assertEqual(
                    future_validator_result,
                    "Validator의 직접 검토 결과" in task.acceptance_criteria[0],
                )
                self.assertIn("Worker 응답의 직접 실행 관측", task.validations[2].statement)

                if finding_code is None:
                    self.assertIsNotNone(outcome.selected_activation_digest)
                    service.activate_plan(
                        plan_revision_id=evaluation.plan.plan_revision_id,
                        activation_digest=evaluation.plan.activation_digest,
                        source="합성 정상 회귀",
                    )
                    self.assertEqual((task.task_id,), service.list_ready_tasks(task.project_id))
                else:
                    self.assertIsNone(outcome.selected_activation_digest)
                    with self.assertRaisesRegex(EngineServiceError, "ready"):
                        service.activate_plan(
                            plan_revision_id=evaluation.plan.plan_revision_id,
                            activation_digest=evaluation.plan.activation_digest,
                            source="합성 오류 회귀",
                        )


if __name__ == "__main__":
    unittest.main()
