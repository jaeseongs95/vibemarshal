from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import RevisionStatus
from flowmarshal.engine.goal import (
    GoalNormalizationProposal,
    GoalNormalizerAdapter,
    GoalPreparationPipeline,
    GoalReviewerAdapter,
)
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner

from tests.engine_helpers import inventory, profile


SOURCE_REQUEST = (
    "add 함수의 합산 결함을 최소 수정하고, 등록 검사 도구로 독립 Goal Test를 수행한다. "
    "외부 서비스 변경·배포·의존성 추가는 수행하지 않는다."
)

VALIDATION_REFERENCE = {
    "kind": "project_file",
    "path": "validation-reference.md",
    "content_complete": True,
    "content_excerpt": (
        "등록 validation 도구의 goal phase는 7개 정수 쌍을 위치 인자와 키워드 인자로 "
        "각각 호출하고 결과·공개 계약·파일 보존을 검사한다."
    ),
}


def _ratings() -> dict[str, int]:
    return {
        "goal_fit": 4,
        "grounding": 4,
        "engineering": 4,
        "verification": 4,
        "execution_safety": 4,
    }


def _proposal(*, validation_intent: str, prohibited_effects: list[str]) -> dict[str, object]:
    return {
        "mission_class": "bugfix_stabilization",
        "observable_outcome": "add가 공개 호출 계약을 보존하며 두 정수의 합을 반환한다.",
        "hard_acceptance": [
            {
                "statement": "add가 양수·음수·0을 포함한 두 정수의 합을 반환한다.",
                "validation_intent": "기존 unittest와 독립 Goal Test에서 결과를 확인한다.",
            },
            {
                "statement": "공개 함수의 위치·키워드 인자 호출 계약을 보존한다.",
                "validation_intent": validation_intent,
            },
        ],
        "constraints": [
            {"category": "검증", "statement": "Task 검증과 독립 Goal Test를 분리한다."},
        ],
        "mutation_policy": "minimal_change",
        "behavior_policy": "preserve_public_contracts",
        "allowed_external_effects": [],
        "prohibited_effects": prohibited_effects,
    }


def _finding(*, code: str, severity: str = "error") -> dict[str, object]:
    verification = code.startswith("GOAL_TEST")
    return {
        "finding_code": code,
        "gate": "verification" if verification else "intent",
        "severity": severity,
        "summary": ("Goal이 채택한 검사 범위에 키워드 인자의 실제 호출 검사가 없다." if verification
                    else "로컬 의존성 추가 금지를 외부 서비스 변경·배포 금지와 같은 항목에 묶었다."),
        "evidence_refs": ["artifact:goal_proposal", "source:observation_001" if verification else "source:user_request"],
        "remediable": True,
    }


class GoalReferenceBindingTests(unittest.TestCase):
    """Scripted role 결과의 adapter·binding 보존만 검증하며 의미 탐지 능력을 평가하지 않는다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_id = "project_" + "9" * 32
        self.profile = profile(self.project_id)
        self.inventory = inventory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _prepare(
        self,
        proposal: dict[str, object],
        review: dict[str, object],
        *,
        observed_facts: tuple[dict[str, object], ...] = (VALIDATION_REFERENCE,),
        role_cwd: Path | None = None,
    ):
        runner = ScriptedStructuredRoleRunner(
            {
                "goal_normalizer": [proposal],
                "goal_reviewer": [review],
            }
        )
        pipeline = GoalPreparationPipeline(
            GoalNormalizerAdapter(
                runner,
                model="worker",
                effort="medium",
                inventory_digest=self.inventory.inventory_digest,
                cwd=role_cwd or self.root,
            ),
            GoalReviewerAdapter(
                runner,
                model="validator",
                effort="high",
                inventory_digest=self.inventory.inventory_digest,
                cwd=role_cwd or self.root,
            ),
        )
        outcome = pipeline.prepare(
            project_id=self.project_id,
            profile=self.profile,
            source_request=SOURCE_REQUEST,
            observed_facts=observed_facts,
        )
        self.assertEqual(["goal_normalizer", "goal_reviewer"], [call.role for call in runner.calls])
        self.assertEqual(
            [str((role_cwd or self.root).resolve())] * 2,
            [call.cwd for call in runner.calls],
        )
        self.assertEqual(
            runner.calls[0].payload["observed_facts"],
            tuple(
                runner.calls[1].payload["evidence_catalog"][f"source:observation_{index:03d}"]
                for index in range(1, len(observed_facts) + 1)
            ),
        )
        self.assertEqual(
            GoalNormalizationProposal.model_validate(proposal).model_dump(mode="json"),
            runner.calls[1].payload["evidence_catalog"]["artifact:goal_proposal"],
        )
        self.assertEqual(
            sha256_digest(runner.calls[1].payload["evidence_catalog"]),
            outcome.review.evidence_catalog_digest,
        )
        self.assertEqual(
            sha256_digest(outcome.proposal),
            outcome.goal_contract.preparation_binding.normalization_proposal_digest,
        )
        self.assertEqual(
            sha256_digest(outcome.review),
            outcome.goal_contract.preparation_binding.reviewer_submission_digest,
        )
        return outcome

    def test_selected_project_root_is_preserved_when_role_cwd_is_a_copy(self) -> None:
        selected_root = self.root / "selected-workspace"
        role_copy = self.root / "role-copy"
        selected_root.mkdir()
        role_copy.mkdir()
        observations = (
            {
                "kind": "project_inventory",
                "project_root": str(selected_root.resolve()),
                "inventory_complete": True,
                "indexed_file_count": 3,
                "observed_file_count": 3,
            },
            VALIDATION_REFERENCE,
        )

        outcome = self._prepare(
            _proposal(
                validation_intent="등록 validation 도구의 goal phase를 새로 실행한다.",
                prohibited_effects=["외부 서비스 변경과 배포를 수행하지 않는다.", "새 의존성을 추가하지 않는다."],
            ),
            {"findings": [], "ratings": _ratings()},
            observed_facts=observations,
            role_cwd=role_copy,
        )

        self.assertEqual(RevisionStatus.READY, outcome.goal_contract.status)
        self.assertEqual(
            str(selected_root.resolve()),
            json.loads(outcome.goal_contract.definition.source_traces[1].statement)["project_root"],
        )

    def test_explicit_goal_phase_reference_and_separated_effects_are_ready(self) -> None:
        outcome = self._prepare(
            _proposal(
                validation_intent="등록 validation 도구의 goal phase를 새로 실행한다.",
                prohibited_effects=["외부 서비스 변경과 배포를 수행하지 않는다.", "새 의존성을 추가하지 않는다."],
            ),
            {"findings": [], "ratings": _ratings()},
        )

        self.assertEqual(RevisionStatus.READY, outcome.goal_contract.status)
        self.assertNotIn("키워드", outcome.goal_contract.definition.hard_acceptance[1].validation_intent)
        self.assertEqual((), outcome.review.findings)
        self.assertIsNotNone(outcome.goal_contract.preparation_binding.ratings)
        self.assertEqual((), outcome.goal_contract.definition.effect_policy.allowed_external_effects)
        self.assertEqual(
            ("외부 서비스 변경과 배포를 수행하지 않는다.", "새 의존성을 추가하지 않는다."),
            outcome.goal_contract.definition.effect_policy.prohibited_effects,
        )

    def test_reference_presence_without_goal_adoption_preserves_conflict_finding(self) -> None:
        outcome = self._prepare(
            _proposal(
                validation_intent="독립 Goal Test에서 함수 시그니처를 확인한다.",
                prohibited_effects=["외부 서비스 변경과 배포를 수행하지 않는다.", "새 의존성을 추가하지 않는다."],
            ),
            {"findings": [_finding(code="GOAL_TEST_KEYWORD_INVOCATION_COVERAGE_OMITTED")], "ratings": None},
        )

        self.assertEqual(RevisionStatus.CONFLICT, outcome.goal_contract.status)
        self.assertEqual(
            ("GOAL_TEST_KEYWORD_INVOCATION_COVERAGE_OMITTED",),
            outcome.goal_contract.preparation_binding.finding_codes,
        )
        self.assertEqual(outcome.review.findings[0].evidence_refs, ("artifact:goal_proposal", "source:observation_001"))

    def test_wrong_phase_reference_preserves_conflict_finding(self) -> None:
        outcome = self._prepare(
            _proposal(
                validation_intent="등록 validation 도구의 task phase를 새로 실행한다.",
                prohibited_effects=["외부 서비스 변경과 배포를 수행하지 않는다.", "새 의존성을 추가하지 않는다."],
            ),
            {"findings": [_finding(code="GOAL_TEST_KEYWORD_INVOCATION_COVERAGE_OMITTED")], "ratings": None},
        )

        self.assertEqual(RevisionStatus.CONFLICT, outcome.goal_contract.status)
        self.assertEqual(
            ("GOAL_TEST_KEYWORD_INVOCATION_COVERAGE_OMITTED",),
            outcome.goal_contract.preparation_binding.finding_codes,
        )
        self.assertIn("task phase", outcome.goal_contract.definition.hard_acceptance[1].validation_intent)

    def test_explicit_phase_with_mixed_effects_preserves_warning_and_conflict(self) -> None:
        outcome = self._prepare(
            _proposal(
                validation_intent="등록 validation 도구의 goal phase를 새로 실행한다.",
                prohibited_effects=["외부 서비스 변경, 배포 또는 새 의존성 추가를 수행하지 않는다."],
            ),
            {"findings": [_finding(code="PROHIBITED_EFFECT_MIXES_LOCAL_AND_EXTERNAL_EFFECTS", severity="warning")], "ratings": None},
        )

        self.assertEqual(RevisionStatus.CONFLICT, outcome.goal_contract.status)
        self.assertEqual(
            ("PROHIBITED_EFFECT_MIXES_LOCAL_AND_EXTERNAL_EFFECTS",),
            outcome.goal_contract.preparation_binding.finding_codes,
        )
        self.assertEqual(
            ("외부 서비스 변경, 배포 또는 새 의존성 추가를 수행하지 않는다.",),
            outcome.goal_contract.definition.effect_policy.prohibited_effects,
        )


if __name__ == "__main__":
    unittest.main()
