from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.engine.domain import CandidateStatus, RevisionStatus
from flowmarshal.engine.goal import (
    GoalNormalizerAdapter,
    GoalPreparationPipeline,
    GoalReviewerAdapter,
    ReviewDraft,
)
from flowmarshal.engine.planner_roles import (
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
