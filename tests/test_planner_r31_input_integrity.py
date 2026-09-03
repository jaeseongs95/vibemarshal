from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.planning.domain import ValidationCapability
from flowmarshal.planning.r31_domain import (
    ExplicitRequestConstraint,
    MutationPolicy,
    PlanningRunInput,
    ProfileSectionName,
    RequirementExtractionDraft,
    RequirementIntentReview,
    IntentReviewVerdict,
)
from flowmarshal.planning.r31_intent import RequestSpecAssemblyContext
from tests.test_planner_r31_domain import planning_input
from tests.test_planner_r31_requirement_analysis import (
    PROJECT_ID,
    _draft,
    _mission_selection,
    _snapshot,
    RAW_REQUEST,
)
from flowmarshal.planning.r31_intent import RequirementAnalyzer
from tests.test_planner_r31_domain import profile_revision


class PlannerR31InputIntegrityTests(unittest.TestCase):
    def test_frozen_input_requires_independent_mission_and_requirement_evidence(self) -> None:
        document = planning_input().model_dump(mode="python")
        del document["mission_review_evidence"]
        del document["requirement_review_evidence"]

        with self.assertRaisesRegex(ValidationError, "mission_review_evidence"):
            PlanningRunInput.model_validate(document)

    def test_review_evidence_is_cross_bound_to_frozen_artifacts(self) -> None:
        base = planning_input()
        mission_document = base.model_dump(mode="python")
        mission_document["mission_review_evidence"]["resolved_selection"][
            "raw_request_digest"
        ] = (
            "sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(ValidationError, "Mission review evidence"):
            PlanningRunInput.model_validate(mission_document)

        requirement_document = base.model_dump(mode="python")
        requirement_document["requirement_review_evidence"]["reviewer_receipt"][
            "output_digest"
        ] = "sha256:" + "e" * 64
        with self.assertRaisesRegex(ValidationError, "model output digest|reviewer receipt"):
            PlanningRunInput.model_validate(requirement_document)

    def test_effective_policy_cannot_replace_profile_defaults_without_receipts(self) -> None:
        document = planning_input().model_dump(mode="python")
        document["effective_policy"].update(
            {
                "lifecycle_stage": "legacy",
                "criticality": "low",
                "compatibility_policy": "flexible",
                "risk_tolerance": "exploratory",
                "override_receipts": (),
            }
        )

        with self.assertRaisesRegex(ValidationError, "override 이력이 없습니다"):
            PlanningRunInput.model_validate(document)

    def test_reviewed_context_change_requires_recapture_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "AGENTS.md"
            agents.write_text("검토된 지침\n", encoding="utf-8")
            snapshot = _snapshot(agents)
            analyzer = RequirementAnalyzer()
            context = analyzer.context(
                RAW_REQUEST,
                _mission_selection(),
                profile_revision(),
                (ProfileSectionName.ARCHITECTURE, ProfileSectionName.COMPATIBILITY),
                snapshot,
            )
            draft = _draft(snapshot)
            review = RequirementIntentReview(
                analysis_context_digest=context.context_digest,
                extraction_draft_digest=draft.draft_digest,
                verdict=IntentReviewVerdict.PASS,
            )
            agents.write_text("검토되지 않은 새 지침\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "다시.*재검토"):
                analyzer.assemble(
                    context,
                    draft,
                    RequestSpecAssemblyContext(
                        project_id=PROJECT_ID,
                        project_name="TOCTOU 테스트",
                        project_root=str(root),
                        project_description="검토 이후 파일 변경 차단",
                        available_validations=(
                            ValidationCapability(
                                capability_id="python-tests",
                                check_type="command",
                                description="Python 테스트",
                            ),
                        ),
                    ),
                    intent_review=review,
                )

    def test_snapshot_and_extraction_semantics_change_planning_digest(self) -> None:
        base = planning_input()
        changed_snapshot = base.project_snapshot.model_copy(
            update={"unknowns": ("운영 DB 버전 미확인",)}
        )
        changed_draft = RequirementExtractionDraft(
            raw_request_digest=base.requirement_extraction.raw_request_digest,
            mission_digest=base.mission_selection.mission.mission_digest,
            profile_definition_digest=base.profile_revision.definition_digest,
            project_snapshot_digest=changed_snapshot.snapshot_digest,
            request_summary=base.request_spec.request_summary,
            traces=base.requirement_extraction.traces,
            requirements=base.requirement_extraction.requirements,
            assumptions=base.requirement_extraction.assumptions,
            implementation_suggestions=(
                base.requirement_extraction.implementation_suggestions
            ),
            unresolved_items=("운영 DB 버전 미확인",),
        )
        changed_extraction = base.requirement_extraction.model_copy(
            update={
                "project_snapshot_digest": changed_snapshot.snapshot_digest,
                "source_project_snapshot_digest": changed_snapshot.snapshot_digest,
                "extraction_draft_digest": changed_draft.draft_digest,
                "unresolved_items": changed_draft.unresolved_items,
            }
        )
        changed_review = base.requirement_review_evidence.intent_review.model_copy(
            update={"extraction_draft_digest": changed_draft.draft_digest}
        )
        changed_extraction = changed_extraction.model_copy(
            update={"intent_review_digest": changed_review.review_digest}
        )
        changed_review_evidence = base.requirement_review_evidence.model_copy(
            update={
                "intent_review": changed_review,
                "extractor_receipt": (
                    base.requirement_review_evidence.extractor_receipt.model_copy(
                        update={"output_digest": changed_draft.draft_digest}
                    )
                ),
                "reviewer_receipt": (
                    base.requirement_review_evidence.reviewer_receipt.model_copy(
                        update={"output_digest": changed_review.review_digest}
                    )
                ),
            }
        )
        changed = PlanningRunInput(
            request_spec=base.request_spec,
            profile_revision=base.profile_revision,
            mission_selection=base.mission_selection,
            mission_review_evidence=base.mission_review_evidence,
            effective_policy=base.effective_policy,
            requirement_extraction=changed_extraction,
            requirement_review_evidence=changed_review_evidence,
            project_snapshot=changed_snapshot,
        )

        self.assertNotEqual(base.planning_input_digest, changed.planning_input_digest)

    def test_reviewed_requirement_content_cannot_be_rewritten(self) -> None:
        base = planning_input()
        document = base.model_dump(mode="python")
        document["requirement_extraction"]["requirements"][0]["statement"] = (
            "검토되지 않은 발명 요구"
        )

        with self.assertRaisesRegex(ValidationError, "extraction draft"):
            PlanningRunInput.model_validate(document)

    def test_policy_must_match_mission_and_explicit_constraint_trace(self) -> None:
        base = planning_input()
        mismatched = base.effective_policy.model_copy(
            update={"mutation_policy": MutationPolicy.STRUCTURAL_CHANGE}
        )
        with self.assertRaisesRegex(ValidationError, "mutation policy"):
            PlanningRunInput(
                request_spec=base.request_spec,
                profile_revision=base.profile_revision,
                mission_selection=base.mission_selection,
                mission_review_evidence=base.mission_review_evidence,
                effective_policy=mismatched,
                requirement_extraction=base.requirement_extraction,
                requirement_review_evidence=base.requirement_review_evidence,
                project_snapshot=base.project_snapshot,
            )

        invented = base.effective_policy.model_copy(
            update={
                "explicit_request_constraints": (
                    ExplicitRequestConstraint(
                        constraint_id="invented-constraint",
                        statement="원문에 없는 제약",
                        source_ref=base.requirement_extraction.traces[0].trace_id,
                    ),
                )
            }
        )
        with self.assertRaisesRegex(ValidationError, "검토된 constraint"):
            PlanningRunInput(
                request_spec=base.request_spec,
                profile_revision=base.profile_revision,
                mission_selection=base.mission_selection,
                mission_review_evidence=base.mission_review_evidence,
                effective_policy=invented,
                requirement_extraction=base.requirement_extraction,
                requirement_review_evidence=base.requirement_review_evidence,
                project_snapshot=base.project_snapshot,
            )


if __name__ == "__main__":
    unittest.main()
