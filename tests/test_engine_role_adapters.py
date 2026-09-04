from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from flowmarshal.engine.domain import CandidateStatus, GoalCriterion, RevisionStatus
from flowmarshal.engine.goal import (
    GoalNormalizerAdapter,
    GoalPreparationPipeline,
    GoalReviewerAdapter,
    ReviewDraft,
)
from flowmarshal.engine.planner_roles import (
    PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS,
    PlanExpanderAdapter,
    PlanReviewerAdapter,
    PlannerRoleAdapterError,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter,
)
from flowmarshal.engine.planning import SkeletonFirstPlanner
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner

from tests.engine_helpers import assignment, goal, inventory, profile, project_map, state


def _ratings():
    return {
        "goal_fit": 4,
        "grounding": 4,
        "engineering": 4,
        "verification": 4,
        "execution_safety": 4,
    }


def _skeleton_response():
    return {
        "candidates": [
            {
                "approach": {
                    "strategy_family": "direct",
                    "change_shape": "single",
                    "compatibility": "preserve",
                    "rollout_recovery": "bounded retry",
                },
                "tasks": [
                    {
                        "task_ref": "task_one",
                        "kind": "change",
                        "objective": "요구를 구현한다.",
                        "contributes_to": ["ac_one"],
                        "produces": ["result:one"],
                        "consumes": ["input:request"],
                    }
                ],
                "dependencies": [],
                "goal_coverage": [{"criterion_id": "ac_one", "task_refs": ["task_one"]}],
                "unknowns": [],
                "estimated_change_cost": 1,
                "estimated_context_tokens": 100,
            }
        ]
    }


def _plan_response(*, changed_objective: bool = False):
    return {
        "tasks": [
            {
                "task_ref": "task_one",
                "kind": "change",
                "objective": "다른 목표로 바꾼다." if changed_objective else "요구를 구현한다.",
                "goal_criterion_refs": ["ac_one"],
                "produces": ["result:one"],
                "consumes": ["input:request"],
                "acceptance_criteria": ["Task validation이 PASS다."],
                "validations": [
                    {
                        "validation_id": "validation_task",
                        "statement": "Task 결과를 확인한다.",
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
                "criterion_id": "ac_one",
                "task_refs": ["task_one"],
                "validation_ids": ["validation_task", "validation_goal"],
            }
        ],
        "integration_validations": [
            {
                "validation_id": "validation_goal",
                "statement": "Goal을 확인한다.",
                "criterion_refs": ["ac_one"],
                "method": "deterministic",
                "required_evidence_kinds": ["test"],
            }
        ],
    }


class EngineRoleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "AGENTS.md").write_text("지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "7" * 32
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.map = project_map(self.project_id, self.root)
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.inventory = inventory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_goal_is_normalized_once_and_independently_reviewed(self) -> None:
        runner = ScriptedStructuredRoleRunner(
            {
                "goal_normalizer": [
                    {
                        "mission_class": "feature_extension",
                        "observable_outcome": "evidence가 존재한다.",
                        "hard_acceptance": [
                            {
                                "statement": "요구가 충족된다.",
                                "validation_intent": "실제 evidence를 확인한다.",
                            }
                        ],
                        "mutation_policy": "scoped_change",
                        "behavior_policy": "preserve_public_contracts",
                    }
                ],
                "goal_reviewer": [{"findings": [], "ratings": _ratings()}],
            }
        )
        normalizer = GoalNormalizerAdapter(
            runner,
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        reviewer = GoalReviewerAdapter(
            runner,
            model="validator",
            effort="high",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        outcome = GoalPreparationPipeline(normalizer, reviewer).prepare(
            project_id=self.project_id,
            profile=self.profile,
            source_request="검증 가능한 변경을 수행한다.",
            observed_facts=({"path": "app.py", "content": "value = 1"},),
        )
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(["goal_normalizer", "goal_reviewer"], [item.role for item in runner.calls])
        self.assertEqual(RevisionStatus.READY, outcome.goal_contract.status)
        self.assertEqual("검증 가능한 변경을 수행한다.", outcome.goal_contract.definition.source_request)
        self.assertNotIn("status", runner.calls[1].output_schema.get("properties", {}))
        self.assertEqual(
            runner.calls[0].payload["observed_facts"][0],
            runner.calls[1].payload["evidence_catalog"]["source:observation_001"],
        )
        self.assertEqual(2, len(outcome.goal_contract.definition.source_traces))
        self.assertIn("trace_observation_001", outcome.goal_contract.definition.hard_acceptance[0].trace_refs)

    def test_skeleton_to_plan_role_pipeline_preserves_semantic_boundary(self) -> None:
        runner = ScriptedStructuredRoleRunner(
            {
                "skeleton_generator": [_skeleton_response()],
                "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                "plan_expander": [_plan_response()],
                "compact_plan_reviewer": [{"findings": [], "ratings": _ratings()}],
            }
        )
        generator = SkeletonGeneratorAdapter(
            runner,
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        skeleton_reviewer = SkeletonReviewerAdapter(
            runner,
            model="validator",
            effort="high",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        assigner = RuleBasedTaskAssigner(assignment(), assignment(), assignment())
        expander = PlanExpanderAdapter(
            runner,
            assigner,
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        plan_reviewer = PlanReviewerAdapter(
            runner,
            model="validator",
            effort="high",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        outcome = SkeletonFirstPlanner(
            generator,
            skeleton_reviewer,
            expander,
            plan_reviewer,
        ).search(goal=self.goal, state=self.state, project_map=self.map)
        self.assertIsNotNone(outcome.selected_activation_digest)
        self.assertEqual(CandidateStatus.ADMISSIBLE, outcome.plan_evaluations[0].decision.status)
        self.assertEqual(4, len(runner.calls))
        for call in runner.calls:
            self.assertRegex(str(call.payload), r"case-[0-9a-f]{16}")
            self.assertNotIn("adversarial", str(call.payload))
        self.assertIn("input:request", runner.calls[0].payload["external_input_catalog"])
        self.assertIn("아직 없는 활성화 증적", runner.calls[0].instructions)
        self.assertIn("Skeleton schema에 없는", runner.calls[1].instructions)
        self.assertIn("integration_validations", runner.calls[2].instructions)
        self.assertIn("미래 activation receipt", runner.calls[3].instructions)

    def test_plan_expander_rejects_task_objective_drift(self) -> None:
        runner = ScriptedStructuredRoleRunner(
            {
                "skeleton_generator": [_skeleton_response()],
                "plan_expander": [_plan_response(changed_objective=True)],
            }
        )
        generator = SkeletonGeneratorAdapter(
            runner,
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        candidate = generator.generate(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=1,
        )[0]
        expander = PlanExpanderAdapter(
            runner,
            RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        with self.assertRaisesRegex(PlannerRoleAdapterError, "의미"):
            expander.expand(
                candidate=candidate,
                goal=self.goal,
                state=self.state,
                project_map=self.map,
            )

    def _validation_boundary_case(self, check_ref="task_check"):
        definition = self.goal.definition.model_copy(update={"hard_acceptance": (
            *self.goal.definition.hard_acceptance,
            GoalCriterion(criterion_id="ac_independent", statement="모든 Task 완료 후 Goal을 독립 검사한다.",
                          validation_intent="Core가 같은 workspace에서 새 검사 evidence를 수집한다.",
                          trace_refs=("trace_one",)),
        )})
        self.goal = self.goal.model_copy(update={"definition": definition, "definition_digest": definition.definition_digest})
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        skeleton = _skeleton_response()
        candidate = skeleton["candidates"][0]
        candidate["tasks"].append({
            "task_ref": check_ref, "kind": "inspect", "objective": "선행 산출물과 Task evidence를 검토한다.",
            "contributes_to": ["ac_independent"], "produces": ["result:checked"], "consumes": ["result:one"],
        })
        candidate["dependencies"] = [{
            "producer_task_ref": "task_one", "consumer_task_ref": check_ref, "dependency_type": "data",
            "produces": ["result:one"], "consumes": ["result:one"],
        }]
        candidate["goal_coverage"].append({"criterion_id": "ac_independent", "task_refs": [check_ref]})
        plan = _plan_response()
        check = deepcopy(plan["tasks"][0])
        check.update(task_ref=check_ref, kind="inspect", objective=candidate["tasks"][1]["objective"],
                     goal_criterion_refs=["ac_independent"], produces=["result:checked"], consumes=["result:one"])
        check["validations"][0].update(validation_id="validation_check", statement="해당 Task의 검토 산출물을 검사한다.")
        plan["tasks"].append(check)
        plan["dependencies"] = [{"producer_task_ref": "task_one", "consumer_task_ref": check_ref,
                                 "dependency_type": "data", "products": ["result:one"]}]
        plan["goal_coverage"].append({"criterion_id": "ac_independent", "task_refs": [check_ref],
                                      "validation_ids": ["validation_goal"]})
        plan["integration_validations"][0].update(
            criterion_refs=["ac_one", "ac_independent"], evidence_mode="independent",
            statement="모든 Task 완료 후 Core가 같은 workspace에서 Goal을 새 evidence로 독립 검사한다.",
        )
        return skeleton, plan

    def _boundary_search(self, runner):
        options = dict(model="worker", effort="medium", inventory_digest=self.inventory.inventory_digest, cwd=self.root)
        reviewer_options = {**options, "model": "validator", "effort": "high"}
        return SkeletonFirstPlanner(
            SkeletonGeneratorAdapter(runner, **options), SkeletonReviewerAdapter(runner, **reviewer_options),
            PlanExpanderAdapter(runner, RuleBasedTaskAssigner(assignment(), assignment(), assignment()), **options),
            PlanReviewerAdapter(runner, **reviewer_options),
        ).search(goal=self.goal, state=self.state, project_map=self.map)

    def test_goal_test_coverage_survives_refinement_without_an_execution_task(self) -> None:
        """책임 안내·후보 보정·재검토의 연결을 검증하며 모델의 의미 판단을 모사하지 않는다."""
        skeleton, plan = self._validation_boundary_case()
        refined = deepcopy(skeleton["candidates"][0])
        refined["tasks"][1]["detail_requirements"] = [
            "검토 산출물은 ac_independent에 기여하며 독립 Goal Test 실행은 Plan.integration_validations에 둔다."
        ]
        finding = {"finding_code": "SKEL_GOAL_001", "gate": "goal", "severity": "error",
                   "summary": "독립 Goal Test를 수행할 후속 작업이 DAG에 없다.",
                   "evidence_refs": ["artifact:skeleton"], "affected_task_refs": ["task_check"], "remediable": True}
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [skeleton], "skeleton_refiner": [refined],
            "skeleton_reviewer": [{"findings": [finding]}, {"findings": [], "ratings": _ratings()}],
            "plan_expander": [plan], "compact_plan_reviewer": [{"findings": [], "ratings": _ratings()}],
        })
        outcome = self._boundary_search(runner)
        self.assertIsNotNone(outcome.selected_activation_digest)
        self.assertEqual(CandidateStatus.NEEDS_REVISION, outcome.skeleton_evaluations[0].decision.status)
        result = outcome.plan_evaluations[0].plan.definition
        self.assertEqual(["task_one", "task_check"], [item.task_ref for item in result.tasks])
        self.assertEqual("independent", result.integration_validations[0].evidence_mode)
        coverage = next(item for item in result.goal_coverage if item.criterion_id == "ac_independent")
        self.assertEqual((result.tasks[1].task_id,), coverage.task_ids)
        self.assertEqual(("validation_goal",), coverage.validation_ids)
        self.assertEqual("validation_check", result.tasks[1].validations[0].validation_id)
        calls = {call.role: call for call in runner.calls}
        self.assertEqual(finding, calls["skeleton_refiner"].payload["findings"][0])
        self.assertEqual(refined["tasks"][1]["detail_requirements"],
                         calls["plan_expander"].payload["skeleton"]["tasks"][1]["detail_requirements"])
        self.assertEqual({"skeleton_generator", "skeleton_refiner", "skeleton_reviewer", "plan_expander",
                          "compact_plan_reviewer"}, set(calls))
        for call in runner.calls:
            self.assertIn(PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS, call.instructions)
        properties = calls["skeleton_refiner"].output_schema["$defs"]["SkeletonTaskDraft"]["properties"]
        self.assertIn("직접 실행", properties["contributes_to"]["description"])

    def test_recursive_goal_test_finding_is_preserved_without_task_name_rules(self) -> None:
        """일반 검증 Task의 이름은 허용하고 직접 evidence가 있는 의미 충돌 finding은 유지한다."""
        for defect, check_ref in ((None, "task_goal_test"), ("recursive", "task_check"), ("aggregate", "task_check")):
            with self.subTest(defect=defect):
                # 각 사례는 별도 Goal에서 같은 추가 AC를 한 번만 구성한다.
                self.goal = goal(self.project_id, self.profile.definition_digest)
                skeleton, plan = self._validation_boundary_case(check_ref)
                review = {"findings": [], "ratings": _ratings()}
                if defect == "recursive":
                    plan["tasks"][1]["preconditions"] = [{"precondition_id": "all_completed",
                                                          "statement": "자신을 포함한 모든 Task 검증이 완료됐다."}]
                    plan["tasks"][1]["validations"][0]["statement"] = "이후 Core 독립 Goal Test의 실행 결과를 확인한다."
                    review = {"findings": [{
                        "finding_code": "VERIFICATION_GOAL_TEST_SELF_DEPENDENCY", "gate": "verification",
                        "severity": "error", "summary": "Task가 자신을 포함한 모든 Task 검증과 이후 Goal Test를 기다린다.",
                        "evidence_refs": ["artifact:plan_contract"], "affected_task_refs": [check_ref], "remediable": True,
                    }]}
                elif defect == "aggregate":
                    plan["integration_validations"][0]["evidence_mode"] = "task_aggregate"
                    review = {"findings": [{
                        "finding_code": "VERIFICATION_INDEPENDENT_MODE_MISMATCH", "gate": "verification",
                        "severity": "error", "summary": "새 독립 검사를 요구하는 AC에 Task evidence 집계를 배정했다.",
                        "evidence_refs": ["artifact:plan_contract", "source:goal"],
                        "affected_task_refs": [check_ref], "remediable": True,
                    }]}
                runner = ScriptedStructuredRoleRunner({
                    "skeleton_generator": [skeleton], "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                    "plan_expander": [plan], "compact_plan_reviewer": [review],
                })
                outcome = self._boundary_search(runner)
                self.assertEqual(defect is None, outcome.selected_activation_digest is not None)
                evaluation = outcome.plan_evaluations[0]
                self.assertEqual(CandidateStatus.NEEDS_REVISION if defect else CandidateStatus.ADMISSIBLE,
                                 evaluation.decision.status)
                if defect:
                    self.assertIn(review["findings"][0]["finding_code"], evaluation.decision.finding_codes)
                    self.assertIsNone(evaluation.decision.fitness_score)
                else:
                    self.assertFalse(outcome.skeleton_evaluations[0].candidate.tasks[1].detail_requirements)
                    self.assertIn("detail_requirements에 반복하지 않았다는 이유만으로 Skeleton을 차단하지 않는다",
                                  runner.calls[1].instructions)

    def _per_task_validation_requirement_case(self, *, omit_change_validation: bool):
        """Goal의 Task별 검사 요구와 plan-level Goal Test를 함께 가진 축소 S06 사례다."""
        definition = self.goal.definition.model_copy(update={"hard_acceptance": (
            *self.goal.definition.hard_acceptance,
            GoalCriterion(
                criterion_id="ac_task_evidence",
                statement="각 변경·검증 Task는 실제 unittest·파일 범위와 분리 Validator 검토를 완료 전에 확인한다.",
                validation_intent="각 Task의 validation에 command·test·file·diff 및 semantic model_review를 직접 결속한다.",
                trace_refs=("trace_one",),
            ),
            GoalCriterion(
                criterion_id="ac_independent_goal",
                statement="모든 Task 뒤 새 evidence로 Goal을 독립 검사한다.",
                validation_intent="Plan.integration_validations의 independent Goal Test를 수행한다.",
                trace_refs=("trace_one",),
            ),
        )})
        self.goal = self.goal.model_copy(update={
            "definition": definition,
            "definition_digest": definition.definition_digest,
        })
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)

        change_ref = "task_update_contract"
        skeleton = _skeleton_response()
        change = skeleton["candidates"][0]["tasks"][0]
        change["task_ref"] = change_ref
        change["detail_requirements"] = []
        follow_ref = "task_followup_evidence"
        skeleton["candidates"][0]["tasks"].append({
            "task_ref": follow_ref,
            "kind": "validate",
            "objective": "변경 산출물의 Task 수준 검증 evidence를 수집한다.",
            "contributes_to": ["ac_task_evidence", "ac_independent_goal"],
            "produces": ["artifact:followup_evidence"],
            "consumes": ["result:one"],
            "detail_requirements": [],
        })
        skeleton["candidates"][0]["dependencies"] = [{
            "producer_task_ref": change_ref,
            "consumer_task_ref": follow_ref,
            "dependency_type": "data",
            "produces": ["result:one"],
            "consumes": ["result:one"],
        }]
        skeleton["candidates"][0]["goal_coverage"] = [
            {"criterion_id": "ac_one", "task_refs": [change_ref]},
            {"criterion_id": "ac_task_evidence", "task_refs": [follow_ref]},
            {"criterion_id": "ac_independent_goal", "task_refs": [follow_ref]},
        ]

        plan = _plan_response()
        change = plan["tasks"][0]
        change["task_ref"] = change_ref
        change["validations"] = [
            {
                "validation_id": "validation_change_scope",
                "statement": "변경 Task의 파일 범위와 diff를 직접 검사한다.",
                "method": "deterministic",
                "required_evidence_kinds": ["file", "diff"],
            }
        ]
        if not omit_change_validation:
            change["validations"] = [
                {
                    "validation_id": "validation_change_direct",
                    "statement": "변경 Task의 unittest와 파일 범위를 직접 검사한다.",
                    "method": "deterministic",
                    "required_evidence_kinds": ["command", "test", "file", "diff"],
                },
                {
                    "validation_id": "validation_change_review",
                    "statement": "분리 Validator가 변경 Task evidence를 의미 검토한다.",
                    "method": "semantic",
                    "required_evidence_kinds": ["model_review"],
                },
            ]
        follow = deepcopy(change)
        follow.update(
            task_ref=follow_ref,
            kind="validate",
            objective="변경 산출물의 Task 수준 검증 evidence를 수집한다.",
            goal_criterion_refs=["ac_task_evidence", "ac_independent_goal"],
            produces=["artifact:followup_evidence"],
            consumes=["result:one"],
            acceptance_criteria=["후속 검증 Task의 validation이 PASS다."],
            validations=[
                {
                    "validation_id": "validation_followup_direct",
                    "statement": "후속 검증 Task의 unittest와 파일 범위를 직접 검사한다.",
                    "method": "deterministic",
                    "required_evidence_kinds": ["command", "test", "file", "diff"],
                },
                {
                    "validation_id": "validation_followup_review",
                    "statement": "분리 Validator가 후속 검증 Task evidence를 의미 검토한다.",
                    "method": "semantic",
                    "required_evidence_kinds": ["model_review"],
                },
            ],
        )
        plan["tasks"].append(follow)
        plan["dependencies"] = [{
            "producer_task_ref": change_ref,
            "consumer_task_ref": follow_ref,
            "dependency_type": "data",
            "products": ["result:one"],
        }]
        change_validation_ids = [item["validation_id"] for item in change["validations"]]
        plan["goal_coverage"] = [
            {"criterion_id": "ac_one", "task_refs": [change_ref],
             "validation_ids": [*change_validation_ids, "validation_goal"]},
            {"criterion_id": "ac_task_evidence", "task_refs": [follow_ref],
             "validation_ids": [*change_validation_ids, "validation_followup_direct", "validation_followup_review"]},
            {"criterion_id": "ac_independent_goal", "task_refs": [follow_ref],
             "validation_ids": ["validation_goal"]},
        ]
        plan["integration_validations"][0].update(
            criterion_refs=["ac_one", "ac_task_evidence", "ac_independent_goal"],
            evidence_mode="independent",
            statement="모든 Task validation 뒤 새 command·test·file·diff evidence로 Goal을 독립 검사한다.",
            required_evidence_kinds=["command", "test", "file", "diff"],
        )
        return skeleton, plan, change_ref, follow_ref

    def test_task_validation_requirements_are_not_replaced_by_followup_or_goal_test(self) -> None:
        """Task별 필수 검증, AC 기여, 독립 Goal Test의 계약 경계를 실제 상세 Plan으로 확인한다."""
        for omit_change_validation in (True, False):
            with self.subTest(omit_change_validation=omit_change_validation):
                self.goal = goal(self.project_id, self.profile.definition_digest)
                skeleton, plan, change_ref, follow_ref = self._per_task_validation_requirement_case(
                    omit_change_validation=omit_change_validation,
                )
                review = {"findings": [], "ratings": _ratings()}
                if omit_change_validation:
                    review = {"findings": [{
                        "finding_code": "VERIFICATION_TASK_VALIDATOR_GAP",
                        "gate": "verification",
                        "severity": "error",
                        "summary": "변경 Task의 validation이 file·diff뿐이어서 unittest와 분리 Validator 검토를 대체할 수 없다.",
                        "evidence_refs": ["artifact:plan_contract", "source:goal"],
                        "affected_task_refs": [change_ref],
                        "remediable": True,
                    }]}
                runner = ScriptedStructuredRoleRunner({
                    "skeleton_generator": [skeleton],
                    "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                    "plan_expander": [plan],
                    "compact_plan_reviewer": [review],
                })
                outcome = self._boundary_search(runner)
                evaluation = outcome.plan_evaluations[0]
                self.assertEqual(
                    CandidateStatus.NEEDS_REVISION if omit_change_validation else CandidateStatus.ADMISSIBLE,
                    evaluation.decision.status,
                )
                self.assertEqual(not omit_change_validation, outcome.selected_activation_digest is not None)
                if omit_change_validation:
                    self.assertIn("VERIFICATION_TASK_VALIDATOR_GAP", evaluation.decision.finding_codes)

                plan_definition = evaluation.plan.definition
                change_task = next(item for item in plan_definition.tasks if item.kind.value == "change")
                follow_task = next(item for item in plan_definition.tasks if item.task_ref == follow_ref)
                self.assertTrue(change_task.assignment.independence_required)
                self.assertEqual("independent", plan_definition.integration_validations[0].evidence_mode)
                self.assertEqual({"command", "test", "file", "diff"}, set(
                    follow_task.validations[0].required_evidence_kinds,
                ))
                self.assertEqual({"model_review"}, set(follow_task.validations[1].required_evidence_kinds))
                evidence_coverage = next(item for item in plan_definition.goal_coverage
                                         if item.criterion_id == "ac_task_evidence")
                self.assertEqual((follow_task.task_id,), evidence_coverage.task_ids)
                self.assertTrue({item.validation_id for item in change_task.validations}
                                <= set(evidence_coverage.validation_ids))
                if omit_change_validation:
                    self.assertEqual({"file", "diff"}, set(change_task.validations[0].required_evidence_kinds))
                else:
                    self.assertEqual({"command", "test", "file", "diff"}, set(
                        change_task.validations[0].required_evidence_kinds,
                    ))
                    self.assertEqual({"model_review"}, set(change_task.validations[1].required_evidence_kinds))

                expander_call = next(item for item in runner.calls if item.role == "plan_expander")
                required_ac = next(item for item in expander_call.payload["goal"]["hard_acceptance"]
                                   if item["criterion_id"] == "ac_task_evidence")
                self.assertIn("각 Task의 validation", required_ac["validation_intent"])
                self.assertTrue(all(not item["detail_requirements"] for item in
                                    expander_call.payload["skeleton"]["tasks"]))

    def test_task_scoped_validation_requirement_does_not_become_a_global_rule(self) -> None:
        """Goal이 특정 변경 Task에만 요구한 semantic 검사를 무관한 후속 Task에 강제하지 않는다."""
        self.goal = goal(self.project_id, self.profile.definition_digest)
        skeleton, plan, change_ref, follow_ref = self._per_task_validation_requirement_case(
            omit_change_validation=False,
        )
        scoped_criteria = tuple(
            GoalCriterion(
                criterion_id="ac_task_evidence",
                statement="변경 Task는 실제 unittest·파일 범위와 분리 Validator 검토를 완료 전에 확인한다.",
                validation_intent="변경 Task의 validation에 command·test·file·diff 및 semantic model_review를 직접 결속한다.",
                trace_refs=("trace_one",),
            ) if item.criterion_id == "ac_task_evidence" else item
            for item in self.goal.definition.hard_acceptance
        )
        definition = self.goal.definition.model_copy(update={"hard_acceptance": scoped_criteria})
        self.goal = self.goal.model_copy(update={
            "definition": definition,
            "definition_digest": definition.definition_digest,
        })
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)

        follow_skeleton = next(item for item in skeleton["candidates"][0]["tasks"]
                               if item["task_ref"] == follow_ref)
        change_skeleton = next(item for item in skeleton["candidates"][0]["tasks"]
                               if item["task_ref"] == change_ref)
        change_skeleton["contributes_to"] = ["ac_one", "ac_task_evidence"]
        follow_skeleton["contributes_to"] = ["ac_independent_goal"]
        skeleton["candidates"][0]["goal_coverage"][1]["task_refs"] = [change_ref]
        change_plan = next(item for item in plan["tasks"] if item["task_ref"] == change_ref)
        change_plan["goal_criterion_refs"] = ["ac_one", "ac_task_evidence"]
        follow_plan = next(item for item in plan["tasks"] if item["task_ref"] == follow_ref)
        follow_plan["goal_criterion_refs"] = ["ac_independent_goal"]
        follow_plan["validations"] = [{
            "validation_id": "validation_followup_scope_only",
            "statement": "후속 evidence 산출물의 파일 범위만 확인한다.",
            "method": "deterministic",
            "required_evidence_kinds": ["file", "diff"],
        }]
        plan["goal_coverage"][1].update(
            task_refs=[change_ref],
            validation_ids=["validation_change_direct", "validation_change_review"],
        )
        plan["goal_coverage"][2]["validation_ids"] = ["validation_goal"]
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [skeleton],
            "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
            "plan_expander": [plan],
            "compact_plan_reviewer": [{"findings": [], "ratings": _ratings()}],
        })
        outcome = self._boundary_search(runner)
        self.assertIsNotNone(outcome.selected_activation_digest)
        definition = outcome.plan_evaluations[0].plan.definition
        change_task = next(item for item in definition.tasks if item.kind.value == "change")
        follow_task = next(item for item in definition.tasks if item.task_ref == follow_ref)
        self.assertEqual({"model_review"}, set(change_task.validations[1].required_evidence_kinds))
        self.assertEqual(1, len(follow_task.validations))
        self.assertEqual({"file", "diff"}, set(follow_task.validations[0].required_evidence_kinds))
        coverage = next(item for item in definition.goal_coverage if item.criterion_id == "ac_task_evidence")
        self.assertEqual((change_task.task_id,), coverage.task_ids)

    def test_read_only_response_report_preserves_scope_and_reviewer_authority(self) -> None:
        """응답 산출물의 계약 전달과 실제 모순 finding의 차단을 함께 검사한다."""
        read_goal = goal(self.project_id, self.profile.definition_digest, read_only=True)
        read_state = state(self.project_id, read_goal.definition_digest, self.map.revision_digest)
        for contradictory in (False, True):
            with self.subTest(contradictory=contradictory):
                skeleton = _skeleton_response()
                plan = _plan_response()
                for task in (skeleton["candidates"][0]["tasks"][0], plan["tasks"][0]):
                    task.update(kind="inspect", objective="원본 근거를 읽고 Worker 응답 본문으로 분석을 보고한다.",
                                produces=["analysis_report"])
                detailed = plan["tasks"][0]
                detailed["acceptance_criteria"] = ["응답 보고가 원본 근거와 일치한다.", "프로젝트 파일이 변경되지 않는다."]
                detailed["expected_effects"] = [{"effect_id": "report", "statement": "Worker 응답 본문에 분석 보고를 생성한다."}]
                detailed["prohibited_effects"] = [{"effect_id": "no_file_change", "statement": "프로젝트 파일 생성·수정·삭제 금지"}]
                detailed["validations"] = [
                    {"validation_id": "validation_task", "statement": "응답 관측과 원본 파일 근거를 대조한다.",
                     "method": "semantic", "required_evidence_kinds": ["model_review", "external_observation", "file"]},
                    {"validation_id": "validation_unchanged", "statement": "프로젝트 파일의 전후 변경을 검사한다.",
                     "method": "deterministic", "required_evidence_kinds": ["diff"]},
                ]
                plan["integration_validations"][0].update(
                    statement="원본 근거와 응답을 독립적으로 대조해 Goal을 검증한다.", method="semantic",
                    required_evidence_kinds=["model_review", "external_observation", "file"],
                )
                review = {"findings": [], "ratings": _ratings()}
                if contradictory:
                    detailed["acceptance_criteria"].append("새 응답 보고까지 전후 무변경이어야 한다.")
                    review = {"findings": [{
                        "finding_code": "ENG_READ_ONLY_OUTPUT_CONTRADICTION", "gate": "engineering",
                        "severity": "error", "summary": "새 응답 생성과 응답 무변경 조건이 충돌한다.",
                        "evidence_refs": ["artifact:plan_contract"], "affected_task_refs": ["task_one"],
                        "remediable": True,
                    }]}
                runner = ScriptedStructuredRoleRunner({
                    "skeleton_generator": [skeleton],
                    "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                    "plan_expander": [plan], "compact_plan_reviewer": [review],
                })
                options = dict(model="worker", effort="high", inventory_digest=self.inventory.inventory_digest, cwd=self.root)
                outcome = SkeletonFirstPlanner(
                    SkeletonGeneratorAdapter(runner, **options), SkeletonReviewerAdapter(runner, **options),
                    PlanExpanderAdapter(runner, RuleBasedTaskAssigner(assignment(), assignment(), assignment()), **options),
                    PlanReviewerAdapter(runner, **options),
                ).search(goal=read_goal, state=read_state, project_map=self.map)
                self.assertEqual(not contradictory, outcome.selected_activation_digest is not None)
                result = outcome.plan_evaluations[0]
                self.assertEqual(CandidateStatus.NEEDS_REVISION if contradictory else CandidateStatus.ADMISSIBLE,
                                 result.decision.status)
                self.assertEqual(tuple(detailed["acceptance_criteria"]), result.plan.definition.tasks[0].acceptance_criteria)
                self.assertEqual("read_only", runner.calls[2].payload["goal"]["effect_policy"]["mutation_policy"])
                for call in (runner.calls[0], runner.calls[2]):
                    self.assertIn("Worker 응답 본문", call.instructions)
                    self.assertIn("파일 쓰기 예외를 발명하지 않는다", call.instructions)
                    self.assertIn("inspect 또는 decide", call.instructions)
                self.assertIn("한 항목에 두 범위를 섞지 않는다", runner.calls[2].instructions)
                self.assertEqual(["model_review", "external_observation", "file"],
                                 list(result.plan.definition.tasks[0].validations[0].required_evidence_kinds))

    def test_review_draft_requires_findings_xor_ratings(self) -> None:
        with self.assertRaisesRegex(ValueError, "fitness rating"):
            ReviewDraft.model_validate({"findings": [], "ratings": None})
        with self.assertRaisesRegex(ValueError, "함께"):
            ReviewDraft.model_validate(
                {
                    "findings": [
                        {
                            "finding_code": "DIRECT_WARNING",
                            "gate": "engineering",
                            "severity": "warning",
                            "summary": "직접 증거가 있습니다.",
                            "evidence_refs": ["sha256:" + "1" * 64],
                            "affected_task_refs": [],
                            "remediable": True,
                        }
                    ],
                    "ratings": _ratings(),
                }
            )

    def test_reviewer_must_reference_supplied_evidence_catalog(self) -> None:
        runner = ScriptedStructuredRoleRunner(
            {
                "skeleton_generator": [_skeleton_response()],
                "skeleton_reviewer": [
                    {
                        "findings": [
                            {
                                "finding_code": "DIRECT_WARNING",
                                "gate": "engineering",
                                "severity": "warning",
                                "summary": "제공되지 않은 근거를 참조합니다.",
                                "evidence_refs": ["source:not-supplied"],
                                "affected_task_refs": ["task_one"],
                                "remediable": True,
                            }
                        ]
                    }
                ],
            }
        )
        generator = SkeletonGeneratorAdapter(
            runner,
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        candidate = generator.generate(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=1,
        )[0]
        reviewer = SkeletonReviewerAdapter(
            runner,
            model="validator",
            effort="high",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        with self.assertRaisesRegex(ValueError, "제공되지 않은 evidence"):
            reviewer.review(
                candidate=candidate,
                goal=self.goal,
                state=self.state,
                project_map=self.map,
            )


if __name__ == "__main__":
    unittest.main()
