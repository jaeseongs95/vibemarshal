from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.canonical import sha256_bytes
from flowmarshal.planning.r31_domain import (
    ConfidenceLevel,
    CompatibilityPolicy,
    ExtractedPlanningRequirement,
    IntentReviewVerdict,
    MissionPrimary,
    MissionResolutionStatus,
    MissionSelectedBy,
    PlanningRunInput,
    PlanningRunStatus,
    ProfileRevisionStatus,
    ProfileFreshness,
    ProfileSectionName,
    ProjectProfileRevision,
    RawRequestTrace,
    RequirementExtractionDraft,
    RequirementExtractionReceipt,
    RequirementIntentReview,
    RequirementKind,
)
from flowmarshal.planning.r31_prototype import (
    fixture_mission_review_evidence,
    fixture_requirement_review_evidence,
)
from flowmarshal.planning.r31_intent import (
    EffectivePlanningPolicyBuilder,
    MissionResolutionHint,
    MissionResolutionHintKind,
    PlanningMissionResolver,
    ProjectProfileInspector,
    ProjectStateSnapshotBuilder,
    build_unknown_profile_definition,
    planning_mission_catalog,
    build_mission,
)
from flowmarshal.planning.r31_store import (
    PlanningArtifactConflictError,
    PlanningRunService,
    ProjectProfileStore,
)
from tests.test_planner_r31_domain import (
    NOW,
    PROJECT_ID,
    planning_input,
    profile_definition,
    profile_revision,
    request_spec,
)


class PlannerR31IntentTests(unittest.TestCase):
    def test_clear_request_is_resolved_without_blocking_question(self) -> None:
        receipt = PlanningMissionResolver().resolve(request_spec(), profile_revision())
        self.assertEqual(MissionResolutionStatus.RESOLVED, receipt.status)
        self.assertEqual(MissionPrimary.FEATURE_EXTENSION, receipt.mission.primary)
        self.assertIsNone(receipt.question)

    def test_explicit_read_only_request_overrides_conflicting_model_hint(self) -> None:
        request = request_spec().model_copy(
            update={
                "user_request": "파일이나 코드는 수정하지 말고 현재 구조를 분석만 해줘.",
                "request_summary": "현재 구조 분석 보고서",
            }
        )
        conflicting = MissionResolutionHint(
            kind=MissionResolutionHintKind.UNAMBIGUOUS,
            recommended=build_mission(
                MissionPrimary.FEATURE_EXTENSION,
                request,
                selected_by=MissionSelectedBy.INFERRED,
                confidence=ConfidenceLevel.HIGH,
            ),
        )
        receipt = PlanningMissionResolver().resolve(
            request,
            profile_revision(),
            hint=conflicting,
        )
        self.assertEqual(MissionResolutionStatus.RESOLVED, receipt.status)
        self.assertEqual(MissionPrimary.ANALYSIS_AUDIT, receipt.mission.primary)
        self.assertTrue(any("모델 Mission 추천" in item for item in receipt.warnings))

    def test_ui_mission_catalog_exposes_all_six_purposes(self) -> None:
        catalog = planning_mission_catalog()
        self.assertEqual(set(MissionPrimary), {item.primary for item in catalog})
        self.assertEqual(6, len(catalog))

    def test_outcome_ambiguity_is_asked_but_explicit_conflict_is_blocked(self) -> None:
        request = request_spec().model_copy(
            update={
                "user_request": "코드는 수정하지 말고 분석만 한 뒤 기능을 구현해줘.",
                "request_summary": "분석 또는 구현",
            }
        )
        resolver = PlanningMissionResolver()
        ambiguous = resolver.resolve(request, profile_revision())
        self.assertEqual(MissionResolutionStatus.NEEDS_USER_INPUT, ambiguous.status)
        self.assertEqual(2, len(ambiguous.options))

        blocked = resolver.resolve(
            request,
            profile_revision(),
            user_selection=MissionPrimary.FEATURE_EXTENSION,
        )
        self.assertEqual(MissionResolutionStatus.BLOCKED, blocked.status)
        self.assertTrue(blocked.conflicts)

    def test_analysis_is_read_only_and_receipts_form_a_bound_run_input(self) -> None:
        request = request_spec().model_copy(
            update={
                "user_request": "코드는 변경하지 말고 현재 구조를 분석만 해줘.",
                "request_summary": "현재 구조 분석 보고서",
            }
        )
        profile = profile_revision()
        mission = PlanningMissionResolver().resolve(request, profile)
        self.assertEqual(MissionPrimary.ANALYSIS_AUDIT, mission.mission.primary)
        self.assertEqual("read_only", mission.mission.mutation_policy.value)

        policy = EffectivePlanningPolicyBuilder().build(profile, mission)
        snapshot = ProjectStateSnapshotBuilder(
            now=lambda: NOW,
            id_factory=lambda: "snapshot_intent_test",
        ).capture(request, mission)
        trace = RawRequestTrace(
            trace_id="analysis_request",
            start_offset=0,
            end_offset=len(request.user_request),
            excerpt=request.user_request,
        )
        extracted_requirements = (
            *(
                ExtractedPlanningRequirement(
                    requirement_id=item.requirement_id,
                    kind=RequirementKind.REQUIREMENT,
                    statement=item.statement,
                    mandatory=True,
                    trace_refs=(trace.trace_id,),
                )
                for item in request.requirements
            ),
            *(
                ExtractedPlanningRequirement(
                    requirement_id=f"exclusion_{index}",
                    kind=RequirementKind.EXCLUSION,
                    statement=statement,
                    mandatory=True,
                    mission_grounded=True,
                )
                for index, statement in enumerate(request.out_of_scope, start=1)
            ),
            ExtractedPlanningRequirement(
                requirement_id="mission_outcome",
                kind=RequirementKind.OBSERVABLE_OUTCOME,
                statement=mission.mission.observable_outcome,
                mandatory=True,
                mission_grounded=True,
            ),
        )
        extraction_draft = RequirementExtractionDraft(
            raw_request_digest=sha256_bytes(request.user_request.encode("utf-8")),
            mission_digest=mission.mission.mission_digest,
            profile_definition_digest=profile.definition_digest,
            project_snapshot_digest=snapshot.snapshot_digest,
            request_summary=request.request_summary,
            traces=(trace,),
            requirements=extracted_requirements,
        )
        analysis_context_digest = sha256_bytes(b"analysis-intent-context")
        review = RequirementIntentReview(
            analysis_context_digest=analysis_context_digest,
            extraction_draft_digest=extraction_draft.draft_digest,
            verdict=IntentReviewVerdict.PASS,
        )
        extraction = RequirementExtractionReceipt(
            analysis_context_digest=analysis_context_digest,
            raw_request_digest=sha256_bytes(request.user_request.encode("utf-8")),
            request_spec_digest=request.canonical_digest,
            mission_resolution_digest=mission.mission_resolution_digest,
            project_snapshot_digest=snapshot.snapshot_digest,
            traces=(trace,),
            requirements=extracted_requirements,
            profile_definition_digest=profile.definition_digest,
            source_project_snapshot_digest=snapshot.snapshot_digest,
            extraction_draft_digest=extraction_draft.draft_digest,
            intent_review_digest=review.review_digest,
            selected_profile_sections=(ProfileSectionName.ARCHITECTURE,),
        )
        frozen = PlanningRunInput(
            request_spec=request,
            profile_revision=profile,
            mission_selection=mission,
            mission_review_evidence=fixture_mission_review_evidence(mission),
            effective_policy=policy,
            requirement_extraction=extraction,
            requirement_review_evidence=fixture_requirement_review_evidence(
                extraction_draft,
                review,
            ),
            project_snapshot=snapshot,
        )
        self.assertTrue(frozen.planning_input_digest.startswith("sha256:"))
        self.assertEqual(len(extraction.traces), 1)
        self.assertIsNotNone(extraction.intent_review_digest)

    def test_stale_profile_is_visible_and_digest_mismatch_blocks(self) -> None:
        definition = profile_definition()
        stale_section = definition.architecture.model_copy(
            update={"freshness": ProfileFreshness.STALE}
        )
        stale_definition = definition.model_copy(update={"architecture": stale_section})
        profile = ProjectProfileRevision(
            profile_revision_id="profile_revision_stale_inspection",
            project_id=PROJECT_ID,
            definition=stale_definition,
            definition_digest=stale_definition.definition_digest,
            status=ProfileRevisionStatus.ACTIVE,
            created_at=NOW,
        )
        resolver = PlanningMissionResolver()
        stale = resolver.resolve(request_spec(), profile)
        self.assertTrue(any("stale profile" in item for item in stale.warnings))

        inspection = ProjectProfileInspector().inspect(
            profile,
            observed_source_digests={"architecture": "sha256:" + "0" * 64},
        )
        blocked = resolver.resolve(
            request_spec(),
            profile,
            profile_inspection=inspection,
        )
        self.assertEqual(MissionResolutionStatus.BLOCKED, blocked.status)
        self.assertTrue(any("digest mismatch" in item for item in blocked.conflicts))

    def test_missing_profile_can_be_represented_without_inventing_facts(self) -> None:
        definition = build_unknown_profile_definition(product_goal="새 제품의 목적")
        self.assertTrue(definition.architecture.unknown)
        self.assertEqual(ProfileFreshness.UNKNOWN, definition.runtime.freshness)

    def test_wrong_requirement_list_cannot_pass_by_declaring_coverage(self) -> None:
        value = planning_input()
        document = value.model_dump(mode="python")
        first = document["requirement_extraction"]["requirements"][0]
        first["requirement_id"] = "invented.requirement"
        first["statement"] = "원문에 없는 필수 요구"
        with self.assertRaises(ValidationError):
            PlanningRunInput.model_validate(document)

    def test_explicit_request_policy_overrides_profile_with_receipt(self) -> None:
        profile = profile_revision()
        mission = PlanningMissionResolver().resolve(request_spec(), profile)
        policy = EffectivePlanningPolicyBuilder().build(
            profile,
            mission,
            explicit_compatibility_policy=CompatibilityPolicy.STRICT,
        )
        self.assertEqual(CompatibilityPolicy.STRICT, policy.compatibility_policy)
        self.assertEqual("explicit_request", policy.override_receipts[-1].winning_authority)


class PlannerR31StoreTests(unittest.TestCase):
    def test_profile_revision_compare_and_swap_and_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ids = iter(
                (
                    "profile_revision_first",
                    "profile_revision_second",
                    "profile_revision_stale",
                )
            )
            store = ProjectProfileStore(
                directory,
                now=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc),
                revision_id_factory=lambda: next(ids),
            )
            first = store.create_revision(PROJECT_ID, None, profile_definition())
            self.assertEqual(ProfileRevisionStatus.SUPERSEDED, first.status)
            active = store.activate_revision(first.profile_revision_id, first.definition_digest)
            self.assertEqual(ProfileRevisionStatus.ACTIVE, active.status)
            self.assertEqual(first.profile_revision_id, store.get_active(PROJECT_ID).profile_revision_id)

            changed_definition = profile_definition().model_copy(
                update={"product_goal": "새 기능을 안전하게 확장한다."}
            )
            second = store.create_revision(
                PROJECT_ID,
                active.definition_digest,
                changed_definition,
            )
            stale_definition = profile_definition().model_copy(
                update={"product_goal": "경쟁 갱신을 탐지한다."}
            )
            stale = store.create_revision(
                PROJECT_ID,
                active.definition_digest,
                stale_definition,
            )
            store.activate_revision(second.profile_revision_id, second.definition_digest)
            with self.assertRaises(PlanningArtifactConflictError):
                store.activate_revision(stale.profile_revision_id, stale.definition_digest)
            self.assertEqual(second.profile_revision_id, store.get_active(PROJECT_ID).profile_revision_id)

    def test_profile_creation_is_content_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectProfileStore(
                directory,
                now=lambda: NOW,
                revision_id_factory=lambda: "profile_revision_only",
            )
            first = store.create_revision(PROJECT_ID, None, profile_definition())
            repeated = store.create_revision(PROJECT_ID, None, profile_definition())
            self.assertEqual(first.profile_revision_id, repeated.profile_revision_id)

    def test_planning_run_freeze_is_idempotent_and_lifecycle_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PlanningRunService(
                directory,
                now=lambda: NOW,
                run_id_factory=lambda: "planning_run_fixed",
            )
            frozen = service.freeze(planning_input(), "request-42")
            repeated = service.freeze(planning_input(), "request-42")
            self.assertEqual(frozen.receipt_digest, repeated.receipt_digest)
            with self.assertRaises(PlanningArtifactConflictError):
                service.freeze(
                    planning_input(primary=MissionPrimary.LEGACY_REFACTOR),
                    "request-42",
                )

            searching = service.transition(
                frozen.run_id,
                PlanningRunStatus.SEARCHING,
            )
            ready = service.transition(
                searching.run_id,
                PlanningRunStatus.READY_FOR_REVIEW,
            )
            superseded = service.supersede(ready.run_id, "Mission이 변경됐습니다.")
            self.assertEqual(PlanningRunStatus.SUPERSEDED, superseded.status)
            with self.assertRaises(PlanningArtifactConflictError):
                service.transition(superseded.run_id, PlanningRunStatus.SEARCHING)

    def test_mission_change_produces_a_new_semantic_digest(self) -> None:
        base = request_spec()
        profile = profile_revision()
        resolver = PlanningMissionResolver()
        first = resolver.resolve(base, profile, MissionPrimary.FEATURE_EXTENSION)
        second = resolver.resolve(base, profile, MissionPrimary.LEGACY_REFACTOR)
        self.assertNotEqual(first.mission_resolution_digest, second.mission_resolution_digest)
        self.assertEqual(MissionSelectedBy.USER, first.mission.selected_by)
        self.assertEqual(ConfidenceLevel.HIGH, first.mission.confidence)


if __name__ == "__main__":
    unittest.main()
