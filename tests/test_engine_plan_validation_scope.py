from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.context import ProjectMapper
from flowmarshal.engine.domain import (
    CandidateStatus,
    GoalCriterion,
    GoalContractRevision,
    IntegrationValidationContract,
    SourceTrace,
    ValidationContract,
)
from flowmarshal.engine.goal import ReviewDraft
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter,
    PlanIntegrationValidationDraft,
    PlanReviewDraft,
    PlanReviewerAdapter,
    PlanTaskValidationDraft,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter,
)
from flowmarshal.engine.planning import SkeletonFirstPlanner, plan_validation_scope_rows
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.service import EngineService, EngineServiceError

from tests.engine_helpers import assignment, goal, inventory, profile, state


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "engine"
ORACLE_PATH = FIXTURE_ROOT / "bugfix-trace" / "oracle.py"
PROJECT_FIXTURE = FIXTURE_ROOT / "project-e2e"

_oracle_spec = importlib.util.spec_from_file_location("plan_validation_scope_oracle", ORACLE_PATH)
assert _oracle_spec is not None and _oracle_spec.loader is not None
oracle = importlib.util.module_from_spec(_oracle_spec)
_oracle_spec.loader.exec_module(oracle)


def _ratings() -> dict[str, int]:
    return {
        "goal_fit": 4,
        "grounding": 4,
        "engineering": 4,
        "verification": 4,
        "execution_safety": 4,
    }


def _statement_contract(schema: dict[str, object]) -> dict[str, object]:
    """안내문을 제외한 statement schema 값 제약만 비교한다."""
    statement = dict(schema["properties"]["statement"])
    statement.pop("description", None)
    return statement


def _skeleton_response() -> dict[str, object]:
    return {
        "candidates": [{
            "approach": {
                "strategy_family": "minimal_bugfix",
                "change_shape": "single",
                "compatibility": "preserve",
                "rollout_recovery": "bounded retry",
            },
            "tasks": [{
                "task_ref": "task_fix_add",
                "kind": "change",
                "objective": "add 구현만 수정해 정수 합산을 복구한다.",
                "contributes_to": ["ac_task_scope", "ac_behavior", "ac_goal_phase", "ac_003"],
                "produces": ["result:add_fixed"],
                "consumes": ["input:request"],
            }],
            "dependencies": [],
            "goal_coverage": [
                {"criterion_id": "ac_task_scope", "task_refs": ["task_fix_add"]},
                {"criterion_id": "ac_behavior", "task_refs": ["task_fix_add"]},
                {"criterion_id": "ac_goal_phase", "task_refs": ["task_fix_add"]},
                {"criterion_id": "ac_003", "task_refs": ["task_fix_add"]},
            ],
            "unknowns": [],
            "estimated_change_cost": 1,
            "estimated_context_tokens": 300,
        }],
    }


def _plan_response(
    *,
    overclaims_task_phase: bool,
    assigns_goal_work_to_task_phase: bool,
    has_separate_task_behavior_check: bool,
) -> dict[str, object]:
    task_statement = (
        "등록 oracle.py의 task phase가 양수·음수·0과 위치·키워드 호출을 실제 실행해 확인한다."
        if overclaims_task_phase
        else "등록 oracle.py의 task phase를 실제 실행하여 현재 파일 집합, test_app.py·AGENTS.md 보존 해시, add 본문 밖 AST, 공개 함수명·인자명·int annotation·시그니처와 기존 unittest 통과를 검사한다."
    )
    if has_separate_task_behavior_check and not overclaims_task_phase:
        task_statement += (
            " 이 task validation은 task phase 결과와 별개로 고정 입력의 add 호출 결과를 기대 합과 "
            "직접 대조한다. 이 별도 실제 검사를 task phase가 수행한다고 주장하지 않는다."
        )
    goal_statement = (
        "등록 oracle.py의 task phase를 새로 실행해 양수·음수·0과 위치·키워드 호출을 확인한다."
        if assigns_goal_work_to_task_phase
        else "등록 oracle.py의 goal phase를 새로 실행해 양수·음수·0과 위치·키워드 호출을 확인한다."
    )
    return {
        "tasks": [{
            "task_ref": "task_fix_add",
            "kind": "change",
            "objective": "add 구현만 수정해 정수 합산을 복구한다.",
            "goal_criterion_refs": ["ac_task_scope", "ac_behavior", "ac_goal_phase", "ac_003"],
            "produces": ["result:add_fixed"],
            "consumes": ["input:request"],
            "acceptance_criteria": ["등록 검사 계약의 Task 수준 검증을 통과한다."],
            "validations": [
                {
                    "validation_id": "val_task_add_behavior_contract",
                    "statement": task_statement,
                    "method": "deterministic",
                    "required_evidence_kinds": ["file", "diff", "command", "test"],
                },
                {
                    "validation_id": "validation_task_review",
                    "statement": "분리 Validator가 task phase의 file·diff·test evidence를 검토한다.",
                    "method": "semantic",
                    "required_evidence_kinds": ["model_review"],
                },
            ],
            "risk_level": "low",
            "recovery": {"retryable_failure_classes": ["implementation"]},
        }],
        "dependencies": [],
        "goal_coverage": [
            {
                "criterion_id": "ac_task_scope",
                "task_refs": ["task_fix_add"],
                "validation_ids": ["val_task_add_behavior_contract", "validation_task_review"],
            },
            {
                "criterion_id": "ac_behavior",
                "task_refs": ["task_fix_add"],
                "validation_ids": ["validation_goal_phase"],
            },
            {
                "criterion_id": "ac_goal_phase",
                "task_refs": ["task_fix_add"],
                "validation_ids": ["validation_goal_phase"],
            },
            {
                "criterion_id": "ac_003",
                "task_refs": ["task_fix_add"],
                "validation_ids": ["val_task_add_behavior_contract"],
            },
        ],
        "integration_validations": [{
            "validation_id": "validation_goal_phase",
            "statement": goal_statement,
            "criterion_refs": ["ac_behavior", "ac_goal_phase"],
            "evidence_mode": "independent",
            "method": "deterministic",
            "required_evidence_kinds": ["command", "test", "file", "diff"],
        }],
    }


class PlanValidationScopeRegressionTests(unittest.TestCase):
    """Scripted finding 전달과 Draft→Compiler→Core 결속을 검증한다.

    실제 등록 도구의 phase 의미 탐지는 제한된 실제 역할 진단에서 별도로 평가한다.
    따라서 이 검사는 phase 모순 finding의 전달과 검사 ID 결속만 확인한다.
    """

    def _goal(self, project_id: str, profile_digest: str) -> GoalContractRevision:
        source_request = "add 구현만 고치고 task 검증과 독립 goal 검사를 분리한다."
        base = goal(project_id, profile_digest)
        source_digest = sha256_bytes(source_request.encode("utf-8"))
        definition = base.definition.model_copy(update={
            "source_request": source_request,
            "source_request_digest": source_digest,
            "source_traces": (
                SourceTrace(
                    trace_id="trace_one",
                    source_ref="user-request",
                    statement=source_request,
                    source_digest=source_digest,
                ),
            ),
            "hard_acceptance": (
                GoalCriterion(
                    criterion_id="ac_task_scope",
                    statement="Task 검증은 task phase가 실제 지원하는 파일·보존·공개 계약·기존 unittest 범위를 확인한다.",
                    validation_intent="등록 oracle.py task phase와 분리 Validator evidence를 사용한다.",
                    trace_refs=("trace_one",),
                ),
                GoalCriterion(
                    criterion_id="ac_behavior",
                    statement="add는 양수·음수·0 정수의 합을 반환한다.",
                    validation_intent="독립 goal phase의 고정 동작 검사를 실행한다.",
                    trace_refs=("trace_one",),
                ),
                GoalCriterion(
                    criterion_id="ac_goal_phase",
                    statement="공개 함수의 위치·키워드 호출 계약을 보존한다.",
                    validation_intent="독립 goal phase의 위치·키워드 호출 검사를 실행한다.",
                    trace_refs=("trace_one",),
                ),
                GoalCriterion(
                    criterion_id="ac_003",
                    statement="기존 unittest가 Task 검증에서 실제로 실행되어 통과한다.",
                    validation_intent="task phase가 실제 실행하는 기존 unittest command/test evidence를 연결한다.",
                    trace_refs=("trace_one",),
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
        overclaims_task_phase: bool,
        assigns_goal_work_to_task_phase: bool,
        has_separate_task_behavior_check: bool,
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        workspace = base / "workspace"
        shutil.copytree(PROJECT_FIXTURE, workspace)

        service = EngineService(SQLiteEngineLedger(base / "state.sqlite3"))
        service.initialize()
        project_id = service.create_project(name="검사 phase 회귀", root=workspace)
        project_profile = profile(project_id)
        service.register_profile(project_profile)
        contract = self._goal(project_id, project_profile.definition_digest)
        service.register_goal(contract)
        project_map = ProjectMapper().build(
            project_id=project_id,
            root=workspace,
            revision_no=1,
            registered_references=(ORACLE_PATH,),
        )
        service.record_project_map(project_map)
        snapshot = state(project_id, contract.definition_digest, project_map.revision_digest)
        service.record_state_snapshot(snapshot)

        review = {"findings": [], "ratings": _ratings()}
        if overclaims_task_phase or assigns_goal_work_to_task_phase:
            finding_code = (
                "PLAN_TASK_PHASE_CAPABILITY_MISMATCH"
                if overclaims_task_phase
                else "PLAN_GOAL_PHASE_CAPABILITY_MISMATCH"
            )
            review = {
                "findings": [{
                    "finding_code": finding_code,
                    "gate": "verification",
                    "severity": "error",
                    "summary": "task phase에 없는 동작·호출 검사를 해당 Plan validation에 부여했다.",
                    "evidence_refs": ["artifact:plan_contract", "source:project_map"],
                    "affected_task_refs": ["task_fix_add"],
                    "remediable": True,
                }],
            }
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [_skeleton_response()],
            "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
            "plan_expander": [_plan_response(
                overclaims_task_phase=overclaims_task_phase,
                assigns_goal_work_to_task_phase=assigns_goal_work_to_task_phase,
                has_separate_task_behavior_check=has_separate_task_behavior_check,
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
        ).search(goal=contract, state=snapshot, project_map=project_map, candidate_count=1)
        self.assertEqual(1, len(outcome.skeleton_evaluations))
        self.assertEqual(1, len(outcome.plan_evaluations))
        service.record_skeleton_evaluation(outcome.skeleton_evaluations[0])
        service.register_plan_evaluation(outcome.plan_evaluations[0])
        return service, outcome, runner, project_map, contract, snapshot

    def test_registered_oracle_phase_scope_reaches_detail_review_and_core(self) -> None:
        """고정 phase 반례와 scripted finding의 전달·Core 결속을 같은 selector/digest 경계에서 확인한다."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(PROJECT_FIXTURE, workspace)
            (workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n"
                "    \"\"\"두 정수의 합을 반환한다.\"\"\"\n\n"
                "    return 5\n",
                encoding="utf-8",
            )
            self.assertTrue(oracle.observe(workspace, "task")["passed"])
            self.assertFalse(oracle.observe(workspace, "goal")["passed"])

        for (
            overclaims_task_phase,
            assigns_goal_work_to_task_phase,
            has_separate_task_behavior_check,
            expected_status,
            finding_code,
        ) in (
            (True, False, False, CandidateStatus.NEEDS_REVISION, "PLAN_TASK_PHASE_CAPABILITY_MISMATCH"),
            (False, False, False, CandidateStatus.ADMISSIBLE, None),
            (False, False, True, CandidateStatus.ADMISSIBLE, None),
            (False, True, False, CandidateStatus.NEEDS_REVISION, "PLAN_GOAL_PHASE_CAPABILITY_MISMATCH"),
        ):
            with self.subTest(
                overclaims_task_phase=overclaims_task_phase,
                assigns_goal_work_to_task_phase=assigns_goal_work_to_task_phase,
                has_separate_task_behavior_check=has_separate_task_behavior_check,
            ):
                service, outcome, runner, project_map, contract, snapshot = self._run_case(
                    overclaims_task_phase=overclaims_task_phase,
                    assigns_goal_work_to_task_phase=assigns_goal_work_to_task_phase,
                    has_separate_task_behavior_check=has_separate_task_behavior_check,
                )
                evaluation = outcome.plan_evaluations[0]
                self.assertEqual(expected_status, evaluation.decision.status)
                if finding_code is not None:
                    self.assertIn(finding_code, evaluation.decision.finding_codes)
                else:
                    self.assertEqual((), evaluation.decision.finding_codes)

                calls = {call.role: call for call in runner.calls}
                self.assertEqual(
                    {"skeleton_generator", "skeleton_reviewer", "plan_expander", "compact_plan_reviewer"},
                    set(calls),
                )
                draft_coverage_schema = calls["plan_expander"].output_schema[
                    "$defs"
                ]["PlanGoalCoverageDraft"]["properties"]
                self.assertIn("task_refs", draft_coverage_schema)
                self.assertNotIn("task_ids", draft_coverage_schema)
                plan_review_schema = calls["compact_plan_reviewer"].output_schema
                base_review_schema = ReviewDraft.model_json_schema()
                self.assertEqual(
                    set(base_review_schema["properties"]),
                    set(plan_review_schema["properties"]),
                )
                self.assertEqual(base_review_schema.get("required"), plan_review_schema.get("required"))
                self.assertEqual(
                    ReviewDraft.model_validate({"findings": [], "ratings": _ratings()}).model_dump(),
                    PlanReviewDraft.model_validate({"findings": [], "ratings": _ratings()}).model_dump(),
                )
                for base_model, detailed_model in (
                    (ValidationContract, PlanTaskValidationDraft),
                    (IntegrationValidationContract, PlanIntegrationValidationDraft),
                ):
                    base_schema = base_model.model_json_schema()
                    detailed_schema = detailed_model.model_json_schema()
                    self.assertEqual(set(base_schema["properties"]), set(detailed_schema["properties"]))
                    self.assertEqual(base_schema.get("required"), detailed_schema.get("required"))
                    self.assertEqual(
                        _statement_contract(base_schema),
                        _statement_contract(detailed_schema),
                    )
                reference = next(entry for entry in project_map.entries if entry.path == str(ORACLE_PATH.resolve()))
                self.assertEqual("reference", reference.kind.value)
                self.assertIn("registered_reference", reference.tags)
                for catalog in (
                    calls["plan_expander"].payload["project_map"],
                    calls["compact_plan_reviewer"].payload["evidence_catalog"]["source:project_map"],
                ):
                    delivered = next(item for item in catalog["entries"] if item["entry_id"] == reference.entry_id)
                    self.assertEqual("reference", delivered["kind"])
                    self.assertEqual(str(ORACLE_PATH.resolve()), delivered["path"])
                    self.assertEqual(reference.content_digest, delivered["content_digest"])
                    self.assertIn("registered_reference", delivered["tags"])

                task = evaluation.plan.definition.tasks[0]
                task_validation = task.validations[0]
                goal_validation = evaluation.plan.definition.integration_validations[0]
                reviewer_plan = calls["compact_plan_reviewer"].payload["evidence_catalog"][
                    "artifact:plan_contract"
                ]["definition"]
                for coverage in reviewer_plan["goal_coverage"]:
                    self.assertIn("task_ids", coverage)
                    self.assertNotIn("task_refs", coverage)
                    self.assertEqual([task.task_id], coverage["task_ids"])
                unittest_coverage = next(
                    coverage for coverage in reviewer_plan["goal_coverage"]
                    if coverage["criterion_id"] == "ac_003"
                )
                self.assertEqual(["val_task_add_behavior_contract"], unittest_coverage["validation_ids"])
                self.assertEqual("independent", goal_validation.evidence_mode)
                self.assertEqual(assigns_goal_work_to_task_phase, "task phase" in goal_validation.statement)
                if overclaims_task_phase:
                    self.assertIn("양수·음수·0", task_validation.statement)
                    finding = evaluation.semantic_submissions[0].findings[0]
                    self.assertEqual((task.task_ref,), finding.affected_task_refs)
                    self.assertNotIn(task.task_id, finding.affected_task_refs)
                    self.assertIsNone(outcome.selected_activation_digest)
                    with self.assertRaisesRegex(EngineServiceError, "ready"):
                        service.activate_plan(
                            plan_revision_id=evaluation.plan.plan_revision_id,
                            activation_digest=evaluation.plan.activation_digest,
                            source="회귀 검사",
                        )
                elif assigns_goal_work_to_task_phase:
                    self.assertIn("양수·음수·0", goal_validation.statement)
                    finding = evaluation.semantic_submissions[0].findings[0]
                    self.assertEqual((task.task_ref,), finding.affected_task_refs)
                    self.assertNotIn(task.task_id, finding.affected_task_refs)
                    self.assertIsNone(outcome.selected_activation_digest)
                    with self.assertRaisesRegex(EngineServiceError, "ready"):
                        service.activate_plan(
                            plan_revision_id=evaluation.plan.plan_revision_id,
                            activation_digest=evaluation.plan.activation_digest,
                            source="회귀 검사",
                        )
                else:
                    self.assertIn("파일 집합", task_validation.statement)
                    self.assertIn("goal phase", goal_validation.statement)
                    self.assertEqual(
                        has_separate_task_behavior_check,
                        "task phase 결과와 별개로" in task_validation.statement,
                    )
                    if has_separate_task_behavior_check:
                        self.assertIn("직접 대조", task_validation.statement)
                        self.assertNotIn("task phase가 양수·음수·0", task_validation.statement)
                    self.assertIsNotNone(outcome.selected_activation_digest)
                    service.activate_plan(
                        plan_revision_id=evaluation.plan.plan_revision_id,
                        activation_digest=evaluation.plan.activation_digest,
                        source="회귀 검사",
                    )
                    self.assertEqual((evaluation.plan.definition.tasks[0].task_id,), service.list_ready_tasks(
                        evaluation.plan.definition.project_id,
                    ))

    def test_validation_scope_rows_preserve_every_owner_and_request_binding(self) -> None:
        """비권위 색인이 연결되지 않은 Task 검사까지 원문 순서로 전달하는지 확인한다."""
        _, outcome, _, project_map, contract, snapshot = self._run_case(
            overclaims_task_phase=False,
            assigns_goal_work_to_task_phase=False,
            has_separate_task_behavior_check=False,
        )
        plan = outcome.plan_evaluations[0].plan
        first_task = plan.definition.tasks[0]
        unlinked_validation = first_task.validations[1].model_copy(update={
            "validation_id": "validation_unlinked",
            "statement": "기여 AC에 연결하지 않은 별도 Validator 검토를 수행한다.",
        })
        second_task = first_task.model_copy(update={
            "task_id": "task_second_scope_index",
            "task_ref": "task_second_scope_index",
            "goal_criterion_refs": ("ac_task_scope",),
            "validations": (unlinked_validation,),
        })
        coverage = tuple(
            item.model_copy(update={"task_ids": (*item.task_ids, second_task.task_id)})
            if item.criterion_id == "ac_task_scope" else item
            for item in plan.definition.goal_coverage
        )
        definition = plan.definition.model_copy(update={
            "tasks": (first_task, second_task),
            "goal_coverage": coverage,
        })
        indexed_plan = plan.model_copy(update={
            "definition": definition,
            "definition_digest": definition.definition_digest,
        })
        expected_rows = [
            {
                "scope": "task", "task_ref": "task_fix_add",
                "validation_id": "val_task_add_behavior_contract",
                "statement": "등록 oracle.py의 task phase를 실제 실행하여 현재 파일 집합, test_app.py·AGENTS.md 보존 해시, add 본문 밖 AST, 공개 함수명·인자명·int annotation·시그니처와 기존 unittest 통과를 검사한다.",
                "method": "deterministic", "evidence_mode": None,
                "required_evidence_kinds": ("file", "diff", "command", "test"),
                "linked_criterion_ids": ("ac_task_scope", "ac_003"),
                "declared_criterion_ids": None,
            },
            {
                "scope": "task", "task_ref": "task_fix_add",
                "validation_id": "validation_task_review",
                "statement": "분리 Validator가 task phase의 file·diff·test evidence를 검토한다.",
                "method": "semantic", "evidence_mode": None,
                "required_evidence_kinds": ("model_review",),
                "linked_criterion_ids": ("ac_task_scope",),
                "declared_criterion_ids": None,
            },
            {
                "scope": "task", "task_ref": "task_second_scope_index",
                "validation_id": "validation_unlinked",
                "statement": "기여 AC에 연결하지 않은 별도 Validator 검토를 수행한다.",
                "method": "semantic", "evidence_mode": None,
                "required_evidence_kinds": ("model_review",),
                "linked_criterion_ids": (), "declared_criterion_ids": None,
            },
            {
                "scope": "integration", "task_ref": None,
                "validation_id": "validation_goal_phase",
                "statement": "등록 oracle.py의 goal phase를 새로 실행해 양수·음수·0과 위치·키워드 호출을 확인한다.",
                "method": "deterministic", "evidence_mode": "independent",
                "required_evidence_kinds": ("command", "test", "file", "diff"),
                "linked_criterion_ids": ("ac_behavior", "ac_goal_phase"),
                "declared_criterion_ids": ("ac_behavior", "ac_goal_phase"),
            },
        ]
        self.assertEqual(expected_rows, plan_validation_scope_rows(indexed_plan))

        changed_validation = unlinked_validation.model_copy(update={
            "statement": "기여 AC에 연결하지 않은 변경된 Validator 검토를 수행한다.",
        })
        changed_second_task = second_task.model_copy(update={"validations": (changed_validation,)})
        changed_definition = definition.model_copy(update={"tasks": (first_task, changed_second_task)})
        changed_plan = indexed_plan.model_copy(update={
            "definition": changed_definition,
            "definition_digest": changed_definition.definition_digest,
        })
        runner = ScriptedStructuredRoleRunner({
            "compact_plan_reviewer": [
                {"findings": [], "ratings": _ratings()},
                {"findings": [], "ratings": _ratings()},
            ],
        })
        reviewer = PlanReviewerAdapter(
            runner,
            model="validator",
            effort="high",
            inventory_digest=inventory().inventory_digest,
            cwd=PROJECT_FIXTURE,
        )
        for candidate in (indexed_plan, changed_plan):
            reviewer.review(
                plan=candidate,
                goal=contract,
                state=snapshot,
                project_map=project_map,
                risk_route="compact_plan_reviewer",
            )
        self.assertEqual(expected_rows, runner.calls[0].payload["validation_scope_rows"])
        self.assertNotEqual(runner.calls[0].request_digest, runner.calls[1].request_digest)


if __name__ == "__main__":
    unittest.main()
