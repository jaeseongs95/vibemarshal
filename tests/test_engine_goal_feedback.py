from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import RevisionStatus
from flowmarshal.engine.goal import (
    GoalNormalizationProposal,
    GoalNormalizerAdapter,
    GoalPreparationError,
    GoalPreparationPipeline,
    GoalReviewerAdapter,
)
from flowmarshal.engine.goal_feedback import (
    GoalPreparationRefiner,
    GoalRefinementOutcome,
)
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner

from tests.engine_helpers import inventory, profile


def _ratings() -> dict[str, int]:
    return {
        "goal_fit": 4,
        "grounding": 4,
        "engineering": 4,
        "verification": 4,
        "execution_safety": 4,
    }


def _proposal() -> dict:
    return {
        "mission_class": "feature_extension",
        "observable_outcome": "수정 계획이 수립된다.",
        "hard_acceptance": [{
            "statement": "app.py의 결함이 수정된다.",
            "validation_intent": "수정 결과와 기존 검사를 확인한다.",
        }],
        "constraints": [],
        "non_goals": ["사내 API와 운영 계정을 변경하지 않는다."],
        "mutation_policy": "scoped_change",
        "behavior_policy": "preserve_public_contracts",
    }


def _revised_proposal() -> dict:
    value = _proposal()
    value["observable_outcome"] = "app.py의 결함이 수정되고 기존 검사가 통과한다."
    value["constraints"] = [{
        "category": "scope",
        "statement": "ProjectProfile의 공개 계약과 기존 검사를 보존한다.",
    }]
    value["non_goals"] = []
    return value


def _conflict_review(*, remediable: bool = True) -> dict:
    return {
        "findings": [
            {
                "finding_code": "OUTCOME_STAGE_CONFLICT",
                "gate": "goal",
                "severity": "error",
                "summary": "observable outcome이 계획 단계에 머뭅니다.",
                "evidence_refs": ["source:user_request", "artifact:goal_proposal"],
                "affected_task_refs": [],
                "remediable": remediable,
            },
            {
                "finding_code": "PROFILE_CONSTRAINT_MISSING",
                "gate": "engineering",
                "severity": "error",
                "summary": "ProjectProfile의 보존 제약이 빠졌습니다.",
                "evidence_refs": ["source:project_profile", "artifact:goal_proposal"],
                "affected_task_refs": [],
                "remediable": remediable,
            },
            {
                "finding_code": "UNGROUNDED_NON_GOAL",
                "gate": "grounding",
                "severity": "error",
                "summary": "관측되지 않은 운영 시스템 금지가 추가됐습니다.",
                "evidence_refs": ["source:user_request", "artifact:goal_proposal"],
                "affected_task_refs": [],
                "remediable": remediable,
            },
        ]
    }


class GoalFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "AGENTS.md").write_text("지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "8" * 32
        self.profile = profile(self.project_id)
        self.inventory = inventory()
        self.source_request = "app.py의 결함을 수정하고 기존 동작을 보존한다."
        self.observed_facts = ({"path": "app.py", "content": "value = 1"},)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _prepare(
        self,
        *,
        remediable: bool = True,
        refiner_response: dict | None = None,
        revised_review: dict | None = None,
    ):
        responses = {
            "goal_normalizer": [_proposal()],
            "goal_reviewer": [_conflict_review(remediable=remediable)],
        }
        if revised_review is not None:
            responses["goal_reviewer"].append(revised_review)
        if refiner_response is not None:
            refiner_payload = deepcopy(refiner_response)
            if refiner_payload["proposal"] is not None:
                refiner_payload["proposal"] = GoalNormalizationProposal.model_validate(
                    refiner_payload["proposal"]
                ).model_dump(mode="json")
            responses["goal_refiner"] = [refiner_payload]
        runner = ScriptedStructuredRoleRunner(responses)
        normalizer = GoalNormalizerAdapter(
            runner,
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            inventory=self.inventory,
            cwd=self.root,
        )
        reviewer = GoalReviewerAdapter(
            runner,
            model="validator",
            effort="high",
            inventory_digest=self.inventory.inventory_digest,
            inventory=self.inventory,
            cwd=self.root,
        )
        previous = GoalPreparationPipeline(normalizer, reviewer).prepare(
            project_id=self.project_id,
            profile=self.profile,
            source_request=self.source_request,
            observed_facts=self.observed_facts,
        )
        return runner, previous, GoalPreparationRefiner(normalizer, reviewer)

    def test_changed_proposal_is_independently_reviewed_as_next_goal_revision(self) -> None:
        response = {
            "action": "revision",
            "rationale": "원문과 profile에 맞춰 결과·제약·비목표를 수정했습니다.",
            "evidence_refs": [
                "source:user_request",
                "source:project_profile",
                "artifact:goal_proposal",
                "artifact:goal_review",
            ],
            "proposal": _revised_proposal(),
        }
        runner, previous, refiner = self._prepare(
            refiner_response=response,
            revised_review={"findings": [], "ratings": _ratings()},
        )

        outcome = refiner.refine(
            previous=previous,
            profile=self.profile,
            observed_facts=self.observed_facts,
        )

        self.assertEqual("revised", outcome.result)
        self.assertEqual(1, outcome.round)
        self.assertEqual(sha256_digest(previous), outcome.source_outcome_digest)
        self.assertEqual(previous, outcome.original_outcome)
        revised = outcome.revised_outcome
        self.assertEqual(RevisionStatus.READY, revised.goal_contract.status)
        self.assertEqual(previous.goal_contract.goal_id, revised.goal_contract.goal_id)
        self.assertEqual(previous.goal_contract.revision_no + 1, revised.goal_contract.revision_no)
        self.assertEqual(
            previous.goal_contract.goal_revision_id,
            revised.goal_contract.supersedes_goal_revision_id,
        )
        self.assertEqual(
            previous.goal_contract.definition.source_request,
            revised.goal_contract.definition.source_request,
        )
        self.assertEqual(
            previous.goal_contract.definition.profile_definition_digest,
            revised.goal_contract.definition.profile_definition_digest,
        )
        self.assertEqual(
            ["goal_normalizer", "goal_reviewer", "goal_refiner", "goal_reviewer"],
            [request.role for request in runner.calls],
        )
        request = runner.calls[2]
        self.assertEqual(("worker", "medium"), (request.model, request.effort))
        self.assertEqual(previous.model_dump(mode="json")["proposal"], request.payload[
            "evidence_catalog"
        ]["artifact:goal_proposal"])
        self.assertEqual(previous.review.model_dump(mode="json"), request.payload[
            "evidence_catalog"
        ]["artifact:goal_review"])
        self.assertNotIn("status", request.output_schema["properties"])
        self.assertNotIn("score", request.output_schema["properties"])
        self.assertEqual("goal_refiner", outcome.refiner_receipt.role)
        self.assertEqual(outcome.refiner_receipt, revised.normalizer_receipt)

    def test_dispute_preserves_original_conflict_without_re_review(self) -> None:
        runner, previous, refiner = self._prepare(refiner_response={
            "action": "disputed",
            "rationale": "finding의 적용 단계가 사용자 원문과 충돌합니다.",
            "evidence_refs": ["source:user_request", "artifact:goal_review"],
            "proposal": None,
        })

        outcome = refiner.refine(
            previous=previous,
            profile=self.profile,
            observed_facts=self.observed_facts,
        )

        self.assertEqual("disputed", outcome.result)
        self.assertIsNone(outcome.revised_outcome)
        self.assertEqual(RevisionStatus.CONFLICT, outcome.original_outcome.goal_contract.status)
        self.assertEqual(3, len(runner.calls))

    def test_revised_proposal_can_remain_conflict_after_independent_review(self) -> None:
        runner, previous, refiner = self._prepare(
            refiner_response={
                "action": "revision",
                "rationale": "확인된 일부 결함을 수정했지만 독립 재검토가 필요합니다.",
                "evidence_refs": ["source:user_request", "artifact:goal_review"],
                "proposal": _revised_proposal(),
            },
            revised_review=_conflict_review(),
        )

        outcome = refiner.refine(
            previous=previous,
            profile=self.profile,
            observed_facts=self.observed_facts,
        )

        self.assertEqual("revised", outcome.result)
        self.assertEqual(RevisionStatus.CONFLICT, outcome.revised_outcome.goal_contract.status)
        self.assertEqual(4, len(runner.calls))

    def test_canonical_same_revision_is_unchanged_without_re_review(self) -> None:
        runner, previous, refiner = self._prepare(refiner_response={
            "action": "revision",
            "rationale": "원본과 같은 후보여서 새 revision을 만들 수 없습니다.",
            "evidence_refs": ["artifact:goal_proposal"],
            "proposal": deepcopy(_proposal()),
        })

        outcome = refiner.refine(
            previous=previous,
            profile=self.profile,
            observed_facts=self.observed_facts,
        )

        self.assertEqual("unchanged", outcome.result)
        self.assertIsNone(outcome.revised_outcome)
        self.assertEqual(3, len(runner.calls))

    def test_unknown_evidence_ref_is_rejected(self) -> None:
        runner, previous, refiner = self._prepare(refiner_response={
            "action": "unresolved",
            "rationale": "근거를 찾지 못했습니다.",
            "evidence_refs": ["source:invented"],
            "proposal": None,
        })

        with self.assertRaisesRegex(GoalPreparationError, "제공되지 않은 evidence"):
            refiner.refine(
                previous=previous,
                profile=self.profile,
                observed_facts=self.observed_facts,
            )
        self.assertEqual("goal_refiner", runner.calls[-1].role)

    def test_nonremediable_conflict_is_refused_before_refiner_call(self) -> None:
        runner, previous, refiner = self._prepare(remediable=False)

        with self.assertRaisesRegex(GoalPreparationError, "수정 가능한 CONFLICT"):
            refiner.refine(
                previous=previous,
                profile=self.profile,
                observed_facts=self.observed_facts,
            )
        self.assertEqual(2, len(runner.calls))

    def test_changed_observation_is_refused_before_refiner_call(self) -> None:
        runner, previous, refiner = self._prepare()

        with self.assertRaisesRegex(GoalPreparationError, "원본이 source"):
            refiner.refine(
                previous=previous,
                profile=self.profile,
                observed_facts=({"path": "app.py", "content": "value = 2"},),
            )
        self.assertEqual(2, len(runner.calls))

    def test_persisted_outcome_rejects_receipt_and_result_forgery(self) -> None:
        _, previous, refiner = self._prepare(
            refiner_response={
                "action": "revision",
                "rationale": "원문과 profile에 근거해 수정했습니다.",
                "evidence_refs": ["source:user_request", "source:project_profile"],
                "proposal": _revised_proposal(),
            },
            revised_review={"findings": [], "ratings": _ratings()},
        )
        outcome = refiner.refine(
            previous=previous,
            profile=self.profile,
            observed_facts=self.observed_facts,
        )

        changed_rationale = outcome.model_dump(mode="json")
        changed_rationale["rationale"] = "저장 뒤 바꾼 rationale"
        with self.assertRaisesRegex(ValueError, "refiner 원시 출력"):
            GoalRefinementOutcome.model_validate(changed_rationale)

        forged_unchanged = outcome.model_dump(mode="json")
        forged_unchanged["result"] = "unchanged"
        forged_unchanged["revised_outcome"] = None
        with self.assertRaisesRegex(ValueError, "proposal이 원본과 다릅니다"):
            GoalRefinementOutcome.model_validate(forged_unchanged)


if __name__ == "__main__":
    unittest.main()
