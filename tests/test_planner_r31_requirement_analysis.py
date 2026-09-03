from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.planning.domain import RequirementSource, ValidationCapability
from flowmarshal.planning.r31_domain import (
    CompatibilityPolicy,
    ExtractedPlanningRequirement,
    ExplicitRequestConstraint,
    IntentReviewFinding,
    IntentReviewVerdict,
    MissionResolutionStatus,
    MissionSelectionReceipt,
    ModelCallReceipt,
    ModelCallStatus,
    PlanningRole,
    ProfileSectionName,
    ProjectStateEntry,
    ProjectStateSnapshot,
    RawRequestTrace,
    RequirementAnalysisNote,
    RequirementExtractionDraft,
    RequirementIntentReview,
    RequirementKind,
    SnapshotEntryKind,
    PlanningRunInput,
)
from flowmarshal.planning.r31_intent import (
    EffectivePlanningPolicyBuilder,
    RequestSpecAssemblyContext,
    RequirementAnalyzer,
)
from flowmarshal.planning.r31_pipeline import RoleOutput
from flowmarshal.planning.r31_prototype import (
    fixture_mission_review_evidence,
    fixture_requirement_review_evidence,
)
from flowmarshal.planning.r31_role_adapters import (
    ModelCallReceiptValidationError,
    RequirementIntentReviewRejected,
    RequirementPlanningService,
)
from tests.test_planner_r31_domain import NOW, PROJECT_ID, mission, profile_revision


RAW_REQUEST = (
    "사용자 비활성화 API를 추가한다. "
    "기존 API 계약은 보존한다. "
    "데이터 삭제는 하지 않는다."
)


def _mission_selection() -> MissionSelectionReceipt:
    selected_mission = mission()
    profile = profile_revision()
    return MissionSelectionReceipt(
        request_spec_digest=sha256_bytes(b"requirement-analysis-intake"),
        raw_request_digest=sha256_bytes(RAW_REQUEST.encode("utf-8")),
        profile_revision_id=profile.profile_revision_id,
        profile_definition_digest=profile.definition_digest,
        status=MissionResolutionStatus.RESOLVED,
        mission=selected_mission,
    )


def _trace(trace_id: str, excerpt: str) -> RawRequestTrace:
    start = RAW_REQUEST.index(excerpt)
    return RawRequestTrace(
        trace_id=trace_id,
        start_offset=start,
        end_offset=start + len(excerpt),
        excerpt=excerpt,
    )


def _snapshot(agents_path: Path) -> ProjectStateSnapshot:
    mission_selection = _mission_selection()
    return ProjectStateSnapshot(
        snapshot_id="snapshot_requirement_analysis_source",
        project_id=PROJECT_ID,
        mission_resolution_digest=mission_selection.mission_resolution_digest,
        entries=(
            ProjectStateEntry(
                entry_id="project_agents",
                kind=SnapshotEntryKind.REFERENCE,
                path=str(agents_path),
                content_digest=sha256_bytes(agents_path.read_bytes()),
                relevance="프로젝트 계약과 호환성 정책",
            ),
        ),
        unknowns=("운영 DB 버전 미확인",),
        captured_at=NOW,
    )


def _draft(snapshot: ProjectStateSnapshot) -> RequirementExtractionDraft:
    selected_mission = mission()
    profile = profile_revision()
    return RequirementExtractionDraft(
        raw_request_digest=sha256_bytes(RAW_REQUEST.encode("utf-8")),
        mission_digest=selected_mission.mission_digest,
        profile_definition_digest=profile.definition_digest,
        project_snapshot_digest=snapshot.snapshot_digest,
        request_summary="호환성을 보존하는 사용자 비활성화 API 계획",
        traces=(
            _trace("span_feature", "사용자 비활성화 API를 추가한다."),
            _trace("span_contract", "기존 API 계약은 보존한다."),
            _trace("span_no_delete", "데이터 삭제는 하지 않는다."),
        ),
        requirements=(
            ExtractedPlanningRequirement(
                requirement_id="req.feature",
                kind=RequirementKind.REQUIREMENT,
                statement="사용자 비활성화 API를 추가한다.",
                mandatory=True,
                trace_refs=("span_feature",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="constraint.contract",
                kind=RequirementKind.CONSTRAINT,
                statement="기존 API 계약은 보존한다.",
                mandatory=True,
                trace_refs=("span_contract",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="constraint.project_guideline",
                kind=RequirementKind.CONSTRAINT,
                statement="프로젝트 지침의 호환성 정책을 적용한다.",
                mandatory=True,
                profile_source_refs=("project-agents",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="exclude.delete",
                kind=RequirementKind.EXCLUSION,
                statement="데이터 삭제는 하지 않는다.",
                mandatory=True,
                trace_refs=("span_no_delete",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="exclude.breaking_api",
                kind=RequirementKind.EXCLUSION,
                statement="공개 API 파괴",
                mandatory=True,
                mission_grounded=True,
            ),
            ExtractedPlanningRequirement(
                requirement_id="mission.outcome",
                kind=RequirementKind.OBSERVABLE_OUTCOME,
                statement=selected_mission.observable_outcome,
                mandatory=True,
                mission_grounded=True,
            ),
        ),
        assumptions=(
            RequirementAnalysisNote(
                note_id="assumption.db_transaction",
                statement="운영 DB가 트랜잭션을 지원한다고 가정한다.",
                requires_confirmation=True,
            ),
        ),
        implementation_suggestions=(
            RequirementAnalysisNote(
                note_id="suggestion.adapter",
                statement="기존 공개 계약 앞에 adapter를 둘 수 있다.",
                profile_source_refs=("project-agents",),
                snapshot_entry_refs=("project_agents",),
            ),
        ),
        unresolved_items=("운영 DB 버전 미확인",),
    )


class PlannerR31RequirementAnalysisTests(unittest.TestCase):
    def _context_and_draft(self, agents_path: Path):
        analyzer = RequirementAnalyzer()
        snapshot = _snapshot(agents_path)
        context = analyzer.context(
            RAW_REQUEST,
            _mission_selection(),
            profile_revision(),
            (ProfileSectionName.ARCHITECTURE, ProfileSectionName.COMPATIBILITY),
            snapshot,
        )
        return analyzer, context, _draft(snapshot)

    def test_typed_draft_is_assembled_and_bound_to_final_request_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "AGENTS.md"
            agents.write_text("공개 API 호환성을 보존한다.\n", encoding="utf-8")
            analyzer, context, draft = self._context_and_draft(agents)
            review = RequirementIntentReview(
                analysis_context_digest=context.context_digest,
                extraction_draft_digest=draft.draft_digest,
                verdict=IntentReviewVerdict.PASS,
            )
            assembly = analyzer.assemble(
                context,
                draft,
                RequestSpecAssemblyContext(
                    project_id=PROJECT_ID,
                    project_name="요구 추출 테스트",
                    project_root=str(root),
                    project_description="typed extraction 조립 검증",
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

        self.assertEqual(
            assembly.request_spec.canonical_digest,
            assembly.mission_selection.request_spec_digest,
        )
        self.assertEqual(
            assembly.project_snapshot.snapshot_digest,
            assembly.extraction_receipt.project_snapshot_digest,
        )
        self.assertEqual(draft.draft_digest, assembly.extraction_draft_digest)
        self.assertEqual(
            review.review_digest,
            assembly.extraction_receipt.intent_review_digest,
        )
        self.assertEqual(3, len(assembly.request_spec.requirements))
        self.assertEqual(
            {RequirementSource.USER, RequirementSource.PROJECT_INSTRUCTION},
            {item.source for item in assembly.request_spec.requirements},
        )
        self.assertEqual(
            ("데이터 삭제는 하지 않는다.", "공개 API 파괴"),
            assembly.request_spec.out_of_scope,
        )
        self.assertEqual(1, len(assembly.extraction_receipt.assumptions))
        self.assertEqual(
            1, len(assembly.extraction_receipt.implementation_suggestions)
        )
        self.assertNotIn(
            "운영 DB가 트랜잭션을 지원한다고 가정한다.",
            {item.statement for item in assembly.request_spec.requirements},
        )

        mission_evidence = fixture_mission_review_evidence(
            _mission_selection()
        )
        requirement_evidence = fixture_requirement_review_evidence(draft, review)
        incomplete_policy = EffectivePlanningPolicyBuilder().build(
            profile_revision(),
            assembly.mission_selection,
        )
        with self.assertRaisesRegex(ValidationError, "명시 constraint 전체"):
            PlanningRunInput(
                request_spec=assembly.request_spec,
                profile_revision=profile_revision(),
                mission_selection=assembly.mission_selection,
                mission_review_evidence=mission_evidence,
                effective_policy=incomplete_policy,
                requirement_extraction=assembly.extraction_receipt,
                requirement_review_evidence=requirement_evidence,
                project_snapshot=assembly.project_snapshot,
            )

        derived_policy = EffectivePlanningPolicyBuilder().build(
            profile_revision(),
            assembly.mission_selection,
            requirement_extraction=assembly.extraction_receipt,
        )
        derived_frozen = PlanningRunInput(
            request_spec=assembly.request_spec,
            profile_revision=profile_revision(),
            mission_selection=assembly.mission_selection,
            mission_review_evidence=mission_evidence,
            effective_policy=derived_policy,
            requirement_extraction=assembly.extraction_receipt,
            requirement_review_evidence=requirement_evidence,
            project_snapshot=assembly.project_snapshot,
        )
        self.assertEqual(
            1,
            len(derived_frozen.effective_policy.explicit_request_constraints),
        )

        policy = EffectivePlanningPolicyBuilder().build(
            profile_revision(),
            assembly.mission_selection,
            explicit_request_constraints=(
                ExplicitRequestConstraint(
                    constraint_id="constraint.contract",
                    statement="기존 API 계약은 보존한다.",
                    source_ref="span_contract",
                ),
            ),
            explicit_compatibility_policy=CompatibilityPolicy.STRICT,
        )
        frozen = PlanningRunInput(
            request_spec=assembly.request_spec,
            profile_revision=profile_revision(),
            mission_selection=assembly.mission_selection,
            mission_review_evidence=mission_evidence,
            effective_policy=policy,
            requirement_extraction=assembly.extraction_receipt,
            requirement_review_evidence=requirement_evidence,
            project_snapshot=assembly.project_snapshot,
        )
        self.assertTrue(frozen.planning_input_digest.startswith("sha256:"))
        self.assertEqual(CompatibilityPolicy.STRICT, frozen.effective_policy.compatibility_policy)

    def test_assembly_cannot_bypass_independent_intent_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "AGENTS.md"
            agents.write_text("공개 API 호환성을 보존한다.\n", encoding="utf-8")
            analyzer, context, draft = self._context_and_draft(agents)
            assembly_context = RequestSpecAssemblyContext(
                project_id=PROJECT_ID,
                project_name="review 우회 방지 테스트",
                project_root=str(root),
                project_description="intent review 필수 경계",
                available_validations=(
                    ValidationCapability(
                        capability_id="python-tests",
                        check_type="command",
                        description="Python 테스트",
                    ),
                ),
            )
            failed_review = RequirementIntentReview(
                analysis_context_digest=context.context_digest,
                extraction_draft_digest=draft.draft_digest,
                verdict=IntentReviewVerdict.FAIL,
                findings=(
                    IntentReviewFinding(
                        finding_code="INTENT_REVIEW_FAILED",
                        message="독립 검토 실패 fixture",
                        blocking=True,
                    ),
                ),
            )
            with self.assertRaisesRegex(ValueError, "통과하지 못한"):
                analyzer.assemble(
                    context,
                    draft,
                    assembly_context,
                    intent_review=failed_review,
                )

    def test_span_tampering_and_unknown_profile_source_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "AGENTS.md"
            agents.write_text("지침\n", encoding="utf-8")
            analyzer, context, draft = self._context_and_draft(agents)

            bad_trace = draft.traces[0].model_copy(update={"excerpt": "발명한 원문"})
            with self.assertRaisesRegex(ValueError, "excerpt"):
                analyzer.analyze(
                    context,
                    draft.model_copy(update={"traces": (bad_trace, *draft.traces[1:])}),
                )

            bad_note = draft.implementation_suggestions[0].model_copy(
                update={"profile_source_refs": ("unselected-profile-source",)}
            )
            with self.assertRaisesRegex(ValueError, "profile source"):
                analyzer.analyze(
                    context,
                    draft.model_copy(update={"implementation_suggestions": (bad_note,)}),
                )

    def test_missing_mission_outcome_and_unsupported_requirement_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "AGENTS.md"
            agents.write_text("지침\n", encoding="utf-8")
            analyzer, context, draft = self._context_and_draft(agents)

            without_outcome = draft.model_copy(
                update={
                    "requirements": tuple(
                        item
                        for item in draft.requirements
                        if item.kind is not RequirementKind.OBSERVABLE_OUTCOME
                    )
                }
            )
            with self.assertRaisesRegex(ValueError, "observable outcome"):
                analyzer.analyze(context, without_outcome)

        with self.assertRaises(ValidationError):
            ExtractedPlanningRequirement(
                requirement_id="invented.requirement",
                kind=RequirementKind.REQUIREMENT,
                statement="어디에도 근거가 없는 필수 요구",
                mandatory=True,
            )

    def test_requirement_service_requires_independent_intent_review(self) -> None:
        def successful_output(value, role, thread_id):
            return RoleOutput(
                value,
                (
                    ModelCallReceipt(
                        call_id=f"model_call_{role.value}_{thread_id}",
                        role=role,
                        model_id="fixture-model",
                        reasoning_effort="medium",
                        inventory_digest=sha256_bytes(b"inventory"),
                        input_digest=sha256_digest({"thread_id": thread_id}),
                        output_schema_digest=sha256_digest({"type": "object"}),
                        output_digest=sha256_digest(value),
                        status=ModelCallStatus.SUCCEEDED,
                        thread_id=thread_id,
                        turn_ids=(f"turn_{thread_id}",),
                    ),
                ),
            )

        class Extractor:
            def __init__(self, value, *, with_receipt=True):
                self.value = value
                self.with_receipt = with_receipt

            def extract(self, context):
                if not self.with_receipt:
                    return RoleOutput(self.value)
                return successful_output(
                    self.value,
                    PlanningRole.PURPOSE_RESOLVER,
                    "requirement_extractor",
                )

        class Reviewer:
            def __init__(self, verdict, *, thread_id="requirement_reviewer"):
                self.verdict = verdict
                self.thread_id = thread_id

            def review(self, context, draft):
                findings = ()
                if self.verdict is not IntentReviewVerdict.PASS:
                    findings = (
                        IntentReviewFinding(
                            finding_code="MISSING_REQUIRED_INTENT",
                            message="명시 필수 요구가 누락됐습니다.",
                            blocking=True,
                        ),
                    )
                review = RequirementIntentReview(
                    analysis_context_digest=context.context_digest,
                    extraction_draft_digest=draft.draft_digest,
                    verdict=self.verdict,
                    findings=findings,
                )
                return successful_output(
                    review,
                    PlanningRole.INTENT_REVIEWER,
                    self.thread_id,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "AGENTS.md"
            agents.write_text("공개 API 호환성을 보존한다.\n", encoding="utf-8")
            analyzer, context, draft = self._context_and_draft(agents)
            assembly_context = RequestSpecAssemblyContext(
                project_id=PROJECT_ID,
                project_name="독립 intent review 테스트",
                project_root=str(root),
                project_description="review 통과 전 조립 금지",
                available_validations=(
                    ValidationCapability(
                        capability_id="python-tests",
                        check_type="command",
                        description="Python 테스트",
                    ),
                ),
            )
            service = RequirementPlanningService(
                analyzer=analyzer,
                extractor=Extractor(draft),
                reviewer=Reviewer(IntentReviewVerdict.PASS),
            )
            result = service.analyze_and_assemble(context, assembly_context)
            self.assertEqual(
                result.intent_review.review_digest,
                result.assembly.extraction_receipt.intent_review_digest,
            )

            rejected = RequirementPlanningService(
                analyzer=analyzer,
                extractor=Extractor(draft),
                reviewer=Reviewer(IntentReviewVerdict.FAIL),
            )
            with self.assertRaises(RequirementIntentReviewRejected):
                rejected.analyze_and_assemble(context, assembly_context)

            missing_receipt = RequirementPlanningService(
                analyzer=analyzer,
                extractor=Extractor(draft, with_receipt=False),
                reviewer=Reviewer(IntentReviewVerdict.PASS),
            )
            with self.assertRaises(ModelCallReceiptValidationError):
                missing_receipt.analyze_and_assemble(context, assembly_context)

            same_session = RequirementPlanningService(
                analyzer=analyzer,
                extractor=Extractor(draft),
                reviewer=Reviewer(
                    IntentReviewVerdict.PASS,
                    thread_id="requirement_extractor",
                ),
            )
            with self.assertRaises(ModelCallReceiptValidationError):
                same_session.analyze_and_assemble(context, assembly_context)


if __name__ == "__main__":
    unittest.main()
