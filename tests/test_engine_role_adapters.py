from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from flowmarshal.engine.context import ProjectMapper
from flowmarshal.engine.domain import (
    CandidateStatus,
    GoalCriterion,
    ReviewFinding,
    RevisionStatus,
    TaskSkeleton,
)
from flowmarshal.engine.goal import (
    GoalNormalizerAdapter,
    GoalPreparationPipeline,
    GoalReviewerAdapter,
    ReviewDraft,
)
from flowmarshal.engine.planner_roles import (
    PLANNING_PROJECT_PATH_INSTRUCTIONS,
    PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS,
    PlanExpanderAdapter,
    PlanReviewerAdapter,
    PlannerRoleAdapterError,
    RuleBasedTaskAssigner,
    SkeletonBatchDraft,
    SkeletonRefinementDraft,
    SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter,
)
from flowmarshal.engine.planning import SkeletonFirstPlanner
from tests.engine_inspection_helpers import InspectionScriptedRunner as ScriptedStructuredRoleRunner

from tests.engine_helpers import assignment, goal, inventory, profile, project_map, state


def _ratings():
    return {
        "goal_fit": 4,
        "grounding": 4,
        "engineering": 4,
        "verification": 4,
        "execution_safety": 4,
    }


def _unresolved_refinement():
    return {"action": "unresolved", "rationale": "이 경계 회귀는 원본 결함의 거부를 확인하며 수정 후보는 제공하지 않는다.",
            "evidence_refs": ["artifact:plan_contract"], "plan": None, "skeleton": None}


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
        self.assertIn("각 candidate는 서로 다른 전체 해결 방법", runner.calls[0].instructions)
        self.assertIn("A와 B를 candidate별로 나눠", runner.calls[0].instructions)
        self.assertIn("사용자 산출물 안의 비교 대상 이름이 아니다", runner.calls[0].instructions)
        self.assertIn("아직 없는 활성화 증적", runner.calls[0].instructions)
        self.assertIn("Skeleton schema에 없는", runner.calls[1].instructions)
        self.assertIn("integration_validations", runner.calls[2].instructions)
        self.assertIn("미래 activation receipt", runner.calls[3].instructions)

    def test_skeleton_task_schema_rejects_empty_core_links(self) -> None:
        for field in ("contributes_to", "produces"):
            with self.subTest(field=field):
                response = _skeleton_response()
                response["candidates"][0]["tasks"][0][field] = []
                runner = ScriptedStructuredRoleRunner({"skeleton_generator": [response]})
                generator = SkeletonGeneratorAdapter(
                    runner,
                    model="worker",
                    effort="medium",
                    inventory_digest=self.inventory.inventory_digest,
                    cwd=self.root,
                )

                with self.assertRaisesRegex(ValueError, "at least 1 item"):
                    generator.generate(
                        goal=self.goal,
                        state=self.state,
                        project_map=self.map,
                        candidate_count=1,
                    )

                properties = runner.calls[0].output_schema["$defs"]["SkeletonTaskDraft"]["properties"]
                core_schema = TaskSkeleton.model_json_schema()
                core_properties = core_schema["properties"]
                self.assertLessEqual(set(core_schema["required"]), set(
                    runner.calls[0].output_schema["$defs"]["SkeletonTaskDraft"]["required"]
                ))
                self.assertEqual(core_properties["task_ref"]["pattern"], properties["task_ref"]["pattern"])
                self.assertEqual(core_properties["objective"]["minLength"], properties["objective"]["minLength"])
                self.assertEqual(core_properties["objective"]["maxLength"], properties["objective"]["maxLength"])
                self.assertEqual(1, properties["contributes_to"]["minItems"])
                self.assertEqual(1, properties["produces"]["minItems"])

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

    def test_expander_rejects_incomplete_inspection_before_returning_plan(self):
        from tests.engine_inspection_helpers import inspection_fixture
        from flowmarshal.engine.plan_inspection import PlanInspectionError
        response = _plan_response()
        table = inspection_fixture(response, self.goal.definition.model_dump(mode="json"))
        table["ac_validation_rows"].pop()
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [_skeleton_response()],
            "plan_expander": [{"plan": response, "inspection": table}],
        })
        options = dict(model="worker", effort="medium", inventory_digest=self.inventory.inventory_digest, cwd=self.root)
        candidate = SkeletonGeneratorAdapter(runner, **options).generate(
            goal=self.goal, state=self.state, project_map=self.map, candidate_count=1)[0]
        expander = PlanExpanderAdapter(runner, RuleBasedTaskAssigner(assignment(), assignment(), assignment()), **options)
        with self.assertRaisesRegex(PlanInspectionError, "행 집합 불완전"):
            expander.expand(candidate=candidate, goal=self.goal, state=self.state, project_map=self.map)

    def test_expander_accepts_bound_draft_and_skeleton_citations(self) -> None:
        from tests.engine_inspection_helpers import inspection_fixture

        skeleton = _skeleton_response()
        detail = "Goal 결과를 Task validation으로 보존한다."
        skeleton["candidates"][0]["tasks"][0]["detail_requirements"] = [detail]
        plan = _plan_response()
        inspection = inspection_fixture(
            plan, self.goal.definition.model_dump(mode="json")
        )
        citation_id = "c_skeleton_detail"
        inspection["citations"].append({
            "citation_id": citation_id,
            "source_ref": "artifact:skeleton",
            "selector": "/tasks/0/detail_requirements/0",
            "quote": detail,
        })
        inspection["validation_rows"][0]["mechanisms"][0]["basis_refs"].append(
            citation_id
        )
        inspection["validation_scope_rows"][0]["basis_refs"].append(citation_id)
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [skeleton],
            "plan_expander": [{"plan": plan, "inspection": inspection}],
        })
        options = dict(
            model="worker", effort="medium",
            inventory_digest=self.inventory.inventory_digest, cwd=self.root,
        )
        candidate = SkeletonGeneratorAdapter(runner, **options).generate(
            goal=self.goal, state=self.state, project_map=self.map, candidate_count=1,
        )[0]

        result = PlanExpanderAdapter(
            runner,
            RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
            **options,
        ).expand(
            candidate=candidate, goal=self.goal, state=self.state, project_map=self.map,
        )

        request = runner.calls[-1]
        self.assertEqual("payload.skeleton", request.payload["inspection_source_catalog"]["artifact:skeleton"])
        self.assertEqual("output.plan", request.payload["inspection_source_catalog"]["artifact:plan_draft"])
        self.assertEqual("요구를 구현한다.", result.definition.tasks[0].objective)

    def test_expander_still_rejects_wrong_source_selector_and_undefined_claim(self) -> None:
        from flowmarshal.engine.plan_inspection import PlanInspectionError
        from tests.engine_inspection_helpers import inspection_fixture

        def attempt(mutate):
            skeleton = _skeleton_response()
            detail = "Goal 결과를 Task validation으로 보존한다."
            skeleton["candidates"][0]["tasks"][0]["detail_requirements"] = [detail]
            plan = _plan_response()
            inspection = inspection_fixture(
                plan, self.goal.definition.model_dump(mode="json")
            )
            citation_id = "c_skeleton_detail"
            inspection["citations"].append({
                "citation_id": citation_id,
                "source_ref": "artifact:skeleton",
                "selector": "/tasks/0/detail_requirements/0",
                "quote": detail,
            })
            inspection["validation_rows"][0]["mechanisms"][0]["basis_refs"].append(
                citation_id
            )
            inspection["validation_scope_rows"][0]["basis_refs"].append(citation_id)
            mutate(inspection)
            runner = ScriptedStructuredRoleRunner({
                "skeleton_generator": [skeleton],
                "plan_expander": [{"plan": plan, "inspection": inspection}],
            })
            options = dict(
                model="worker", effort="medium",
                inventory_digest=self.inventory.inventory_digest, cwd=self.root,
            )
            candidate = SkeletonGeneratorAdapter(runner, **options).generate(
                goal=self.goal, state=self.state, project_map=self.map, candidate_count=1,
            )[0]
            PlanExpanderAdapter(
                runner,
                RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
                **options,
            ).expand(
                candidate=candidate, goal=self.goal, state=self.state,
                project_map=self.map,
            )

        cases = (
            (
                "source_ref",
                lambda inspection: inspection["citations"][-1].update(
                    source_ref="artifact:plan_contract"
                ),
            ),
            (
                "selector",
                lambda inspection: inspection["citations"][-1].update(
                    selector="/skeleton/tasks/0/detail_requirements/0"
                ),
            ),
            (
                "없는 인용 ID",
                lambda inspection: inspection["validation_rows"][0].update(
                    claim_ref="claim://undefined"
                ),
            ),
        )
        for error, mutate in cases:
            with self.subTest(error=error), self.assertRaisesRegex(
                PlanInspectionError, error
            ):
                attempt(mutate)

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

    def _boundary_search(self, runner, *, reviewer_cwd=None):
        runner.responses.setdefault("plan_refiner", [_unresolved_refinement()])
        options = dict(model="worker", effort="medium", inventory_digest=self.inventory.inventory_digest, cwd=self.root)
        reviewer_options = {**options, "model": "validator", "effort": "high", "cwd": reviewer_cwd or self.root}
        return SkeletonFirstPlanner(
            SkeletonGeneratorAdapter(runner, **options), SkeletonReviewerAdapter(runner, **reviewer_options),
            PlanExpanderAdapter(runner, RuleBasedTaskAssigner(assignment(), assignment(), assignment()), **options),
            PlanReviewerAdapter(runner, **reviewer_options),
        ).search(goal=self.goal, state=self.state, project_map=self.map)

    def test_registered_reference_and_role_cwd_preserve_target_project(self) -> None:
        """대상 밖 참고자료와 별도 역할 cwd를 대상 Project Map과 구분해 전달한다."""
        with tempfile.TemporaryDirectory() as outside:
            reference = Path(outside) / "reference-run" / "validation-reference.md"
            reference.parent.mkdir()
            reference.write_text(f"검증 대상은 {self.root}이다.\n", encoding="utf-8")
            role_cwd = Path(outside) / "role-copy"
            role_cwd.mkdir()
            self.map = ProjectMapper().build(
                project_id=self.project_id, root=self.root, revision_no=1,
                registered_references=(reference,),
            )
            self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
            runner = ScriptedStructuredRoleRunner({
                "skeleton_generator": [_skeleton_response()],
                "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                "plan_expander": [_plan_response()],
                "compact_plan_reviewer": [{"findings": [], "ratings": _ratings()}],
            })
            outcome = self._boundary_search(runner, reviewer_cwd=role_cwd)
            self.assertIsNotNone(outcome.selected_activation_digest)
            for call in runner.calls:
                self.assertIn(PLANNING_PROJECT_PATH_INSTRUCTIONS, call.instructions)
                if "reviewer" not in call.role:
                    continue
                mapped = call.payload["evidence_catalog"]["source:project_map"]
                self.assertEqual(str(self.root), mapped["root"])
                self.assertEqual(self.map.revision_digest, mapped["revision_digest"])
                self.assertEqual(str(role_cwd), call.cwd)
                self.assertNotEqual(mapped["root"], call.cwd)
                entry = next(item for item in mapped["entries"] if item["path"] == str(reference))
                self.assertEqual("reference", entry["kind"])
                self.assertIn("registered_reference", entry["tags"])
                self.assertIn("Goal의 명시 대상과 Map.root의 충돌", call.instructions)
                self.assertIn("State의 freshness 위반", call.instructions)

    def test_goal_test_coverage_survives_refinement_without_an_execution_task(self) -> None:
        """책임 안내·후보 보정·재검토의 연결을 검증하며 모델의 의미 판단을 모사하지 않는다."""
        skeleton, plan = self._validation_boundary_case()
        refined = deepcopy(skeleton["candidates"][0])
        refined.pop("approach")
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
        from flowmarshal.engine.roles import strict_json_output_schema
        self.assertEqual(
            strict_json_output_schema(SkeletonRefinementDraft.model_json_schema()),
            strict_json_output_schema(calls["skeleton_refiner"].output_schema),
        )
        self.assertEqual(
            strict_json_output_schema(SkeletonBatchDraft.model_json_schema()),
            strict_json_output_schema(calls["skeleton_generator"].output_schema),
        )
        self.assertNotIn("approach", calls["skeleton_refiner"].output_schema["properties"])
        self.assertIn("approach", calls["skeleton_generator"].output_schema["$defs"]["SkeletonCandidateDraft"]["properties"])
        parent = outcome.skeleton_evaluations[0].candidate
        repaired = outcome.skeleton_evaluations[1].candidate
        self.assertEqual(parent.approach, repaired.approach)
        self.assertEqual(parent.candidate_id, repaired.parent_candidate_id)
        self.assertEqual(parent.version + 1, repaired.version)
        self.assertEqual(1, repaired.refinement_round)
        self.assertEqual(refined["tasks"][1]["detail_requirements"],
                         calls["plan_expander"].payload["skeleton"]["tasks"][1]["detail_requirements"])
        self.assertEqual({"skeleton_generator", "skeleton_refiner", "skeleton_reviewer", "plan_expander",
                          "compact_plan_reviewer"}, set(calls))
        for call in runner.calls:
            self.assertIn(PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS, call.instructions)
        properties = calls["skeleton_refiner"].output_schema["$defs"]["SkeletonTaskDraft"]["properties"]
        self.assertIn("직접 실행", properties["contributes_to"]["description"])

    def test_skeleton_refiner_rejects_repeated_parent_approach(self) -> None:
        skeleton, _ = self._validation_boundary_case()
        refined = deepcopy(skeleton["candidates"][0])
        refined["approach"]["strategy_family"] = "두 대안을 비교하는 새 전략"
        finding = {
            "finding_code": "SKEL_STRATEGY_PAIR_UNSPECIFIED",
            "gate": "goal",
            "severity": "error",
            "summary": "비교 대상 두 개를 Task 요구에 명시한다.",
            "evidence_refs": ["artifact:skeleton", "source:goal"],
            "affected_task_refs": ["task_check"],
            "remediable": True,
        }
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [skeleton],
            "skeleton_refiner": [refined],
        })
        options = dict(
            model="worker", effort="medium",
            inventory_digest=self.inventory.inventory_digest, cwd=self.root,
        )
        candidate = SkeletonGeneratorAdapter(runner, **options).generate(
            goal=self.goal, state=self.state, project_map=self.map, candidate_count=1,
        )[0]

        with self.assertRaisesRegex(ValueError, "extra_forbidden"):
            SkeletonGeneratorAdapter(runner, **options).refine(
                candidate=candidate,
                findings=(ReviewFinding.model_validate(finding),),
                goal=self.goal,
                state=self.state,
                project_map=self.map,
            )

    def test_skeleton_refiner_inherits_parent_approach_and_binds_receipt(self) -> None:
        from flowmarshal.canonical import sha256_digest
        from flowmarshal.engine.roles import strict_json_output_schema

        generated = _skeleton_response()
        mutable = deepcopy(generated["candidates"][0])
        mutable.pop("approach")
        mutable["tasks"][0]["detail_requirements"] = [
            "비교 대상과 결론을 같은 Task에서 모두 다룬다."
        ]
        finding = ReviewFinding(
            finding_code="SKEL_DETAIL_MISSING",
            gate="goal",
            severity="error",
            summary="Task의 비교 책임을 구체화한다.",
            evidence_refs=("artifact:skeleton", "source:goal"),
            affected_task_refs=("task_one",),
            remediable=True,
        )
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [generated],
            "skeleton_refiner": [mutable],
        })
        adapter = SkeletonGeneratorAdapter(
            runner,
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=self.root,
        )
        parent = adapter.generate(
            goal=self.goal, state=self.state, project_map=self.map, candidate_count=1,
        )[0]

        refined = adapter.refine(
            candidate=parent,
            findings=(finding,),
            goal=self.goal,
            state=self.state,
            project_map=self.map,
        )

        request = runner.calls[-1]
        receipt = adapter.receipts[-1]
        self.assertEqual(parent.approach, refined.approach)
        self.assertEqual(parent.candidate_id, refined.parent_candidate_id)
        self.assertEqual(parent.version + 1, refined.version)
        self.assertEqual(1, refined.refinement_round)
        self.assertEqual(request.request_digest, receipt.input_digest)
        self.assertEqual(sha256_digest(mutable), receipt.output_digest)
        self.assertEqual(
            sha256_digest(strict_json_output_schema(request.output_schema)),
            receipt.output_schema_digest,
        )
        self.assertEqual("succeeded", receipt.status)

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
                    }], "ratings": None}
                elif defect == "aggregate":
                    plan["integration_validations"][0]["evidence_mode"] = "task_aggregate"
                    review = {"findings": [{
                        "finding_code": "VERIFICATION_INDEPENDENT_MODE_MISMATCH", "gate": "verification",
                        "severity": "error", "summary": "새 독립 검사를 요구하는 AC에 Task evidence 집계를 배정했다.",
                        "evidence_refs": ["artifact:plan_contract", "source:goal"],
                        "affected_task_refs": [check_ref], "remediable": True,
                    }], "ratings": None}
                runner = ScriptedStructuredRoleRunner({
                    "skeleton_generator": [skeleton], "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                    "plan_expander": [plan], "compact_plan_reviewer": [review],
                    "plan_refiner": [_unresolved_refinement()],
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

    def _per_task_validation_requirement_case(self, *, omit_change_validation: bool, generic_change_test: bool = False):
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
        if generic_change_test:
            change["validations"][0]["statement"] = "변경 Task의 일반 동작과 파일 범위를 직접 검사한다."
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
        for omit_change_validation, generic_change_test in ((True, False), (False, False), (False, True)):
            with self.subTest(omit_change_validation=omit_change_validation, generic_change_test=generic_change_test):
                self.goal = goal(self.project_id, self.profile.definition_digest)
                skeleton, plan, change_ref, follow_ref = self._per_task_validation_requirement_case(
                    omit_change_validation=omit_change_validation,
                    generic_change_test=generic_change_test,
                )
                review = {"findings": [], "ratings": _ratings()}
                has_gap = omit_change_validation or generic_change_test
                finding_code = "TASK_VALIDATION_UNITTEST_MISSING" if generic_change_test else "VERIFICATION_TASK_VALIDATOR_GAP"
                if has_gap:
                    review = {"findings": [{
                        "finding_code": finding_code,
                        "gate": "verification",
                        "severity": "error",
                        "summary": ("test evidence 종류만으로는 Goal이 요구한 기존 unittest 실행을 대신할 수 없다."
                                    if generic_change_test else "변경 Task의 validation이 file·diff뿐이어서 unittest와 분리 Validator 검토를 대체할 수 없다."),
                        "evidence_refs": ["artifact:plan_contract", "source:goal"],
                        "affected_task_refs": [change_ref],
                        "remediable": True,
                    }], "ratings": None}
                runner = ScriptedStructuredRoleRunner({
                    "skeleton_generator": [skeleton],
                    "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                    "plan_expander": [plan],
                    "compact_plan_reviewer": [review],
                })
                outcome = self._boundary_search(runner)
                evaluation = outcome.plan_evaluations[0]
                self.assertEqual(
                    CandidateStatus.NEEDS_REVISION if has_gap else CandidateStatus.ADMISSIBLE,
                    evaluation.decision.status,
                )
                self.assertEqual(not has_gap, outcome.selected_activation_digest is not None)
                if has_gap:
                    self.assertIn(finding_code, evaluation.decision.finding_codes)

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
                if generic_change_test:
                    self.assertNotIn("unittest", change_task.validations[0].statement)
                    self.assertIn("test", change_task.validations[0].required_evidence_kinds)
                    self.assertIn("기존 unittest 실행을 요구하면 각 Task의 검사 문장에도", expander_call.instructions)

    def test_task_validation_coverage_is_independent_of_contributing_tasks(self) -> None:
        """자체 검사가 모두 있어도 Goal 연결 누락은 보존하고 Reviewer finding으로 거부한다."""
        for omit_link in (False, True):
            with self.subTest(omit_link=omit_link):
                self.goal = goal(self.project_id, self.profile.definition_digest)
                skeleton, plan, change_ref, follow_ref = self._per_task_validation_requirement_case(
                    omit_change_validation=False,
                )
                if omit_link:
                    plan["goal_coverage"][1]["validation_ids"] = [
                        "validation_followup_direct", "validation_followup_review",
                    ]
                review = {"findings": [], "ratings": _ratings()}
                if omit_link:
                    review = {"findings": [{
                        "finding_code": "VERIFICATION_TASK_COVERAGE_GAP",
                        "gate": "verification", "severity": "error",
                        "summary": "변경 Task의 자체 검사는 존재하지만 각 Task 검증 AC의 validation_ids에서 빠졌다.",
                        "evidence_refs": ["source:goal", "artifact:plan_contract"],
                        "affected_task_refs": [change_ref], "remediable": True,
                    }], "ratings": None}
                runner = ScriptedStructuredRoleRunner({
                    "skeleton_generator": [skeleton],
                    "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                    "plan_expander": [plan], "compact_plan_reviewer": [review],
                    "plan_refiner": [_unresolved_refinement()],
                })
                outcome = self._boundary_search(runner)
                evaluation = outcome.plan_evaluations[0]
                coverage = evaluation.plan.definition.goal_coverage[1]
                tasks = {task.task_ref: task for task in evaluation.plan.definition.tasks}
                self.assertEqual((tasks[follow_ref].task_id,), coverage.task_ids)
                self.assertEqual(2, len(tasks[change_ref].validations))
                linked_change_ids = {item.validation_id for item in tasks[change_ref].validations}
                self.assertEqual(not omit_link, linked_change_ids <= set(coverage.validation_ids))
                self.assertEqual(not omit_link, outcome.selected_activation_digest is not None)
                if omit_link:
                    self.assertIn("VERIFICATION_TASK_COVERAGE_GAP", evaluation.decision.finding_codes)
                for call in runner.calls:
                    if call.role in {"plan_expander", "compact_plan_reviewer"}:
                        self.assertIn("validation_ids의 소유 Task를 그 task_refs로 제한하지 않는다", call.instructions)
                        self.assertIn("각각 확인한다", call.instructions)
                expander = next(call for call in runner.calls if call.role == "plan_expander")
                fields = expander.output_schema["$defs"]["PlanGoalCoverageDraft"]["properties"]
                self.assertIn("허용 목록이 아니며", fields["task_refs"]["description"])
                self.assertIn("task_refs에 없어도", fields["validation_ids"]["description"])

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
                    }], "ratings": None}
                runner = ScriptedStructuredRoleRunner({
                    "skeleton_generator": [skeleton],
                    "skeleton_reviewer": [{"findings": [], "ratings": _ratings()}],
                    "plan_expander": [plan], "compact_plan_reviewer": [review],
                })
                runner.responses["plan_refiner"] = [_unresolved_refinement()]
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
                if contradictory:
                    refiner_call = next(call for call in runner.calls if call.role == "plan_refiner")
                    properties = refiner_call.output_schema["$defs"]["SkeletonTaskDraft"]["properties"]
                    core_properties = TaskSkeleton.model_json_schema()["properties"]
                    self.assertEqual(core_properties["task_ref"]["pattern"], properties["task_ref"]["pattern"])
                    self.assertEqual(1, properties["contributes_to"]["minItems"])
                    self.assertEqual(1, properties["produces"]["minItems"])

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
