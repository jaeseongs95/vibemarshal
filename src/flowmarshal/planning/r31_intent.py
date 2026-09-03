from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    ContextSourceKind,
    PlanningLimits,
    RequestSpec,
    RequirementPriority,
    RequirementSource,
    RequirementSpec,
    ValidationCapability,
)
from .input import ContextFileRegistration, RequestSpecAssembler, RequestSpecAssemblyInput
from .r31_domain import (
    BehaviorPreservation,
    CompatibilityPolicy,
    ConfidenceLevel,
    Criticality,
    EffectivePlanningPolicy,
    ExplicitRequestConstraint,
    ExtractedPlanningRequirement,
    IntentReviewVerdict,
    MissionPrimary,
    MissionResolutionHint,
    MissionResolutionHintKind,
    MissionResolutionStatus,
    MissionSelectedBy,
    MissionSelectionReceipt,
    MutationPolicy,
    LifecycleStage,
    PlanningMissionDefinition,
    ProfileFreshness,
    ProfileOverrideReceipt,
    ProfileSection,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    ProjectStateEntry,
    ProjectStateSnapshot,
    ProfileSectionName,
    R31Model,
    RequirementAnalysisContext,
    RequirementAnalysisNote,
    RequirementExtractionDraft,
    RequirementExtractionReceipt,
    RequirementIntentReview,
    RequirementKind,
    RiskTag,
    RiskTolerance,
    SelectedProfileSection,
    SnapshotEntryKind,
)


class PlanningMissionOption(R31Model):
    primary: MissionPrimary
    label: str
    planning_strategy: str
    default_mutation_policy: MutationPolicy
    default_behavior_preservation: BehaviorPreservation


def planning_mission_catalog() -> tuple[PlanningMissionOption, ...]:
    labels_and_strategies = {
        MissionPrimary.NEW_BUILD: (
            "새로운 것 만들기",
            "최소 vertical slice와 안정된 boundary를 만들고 외부 연동을 단계화합니다.",
        ),
        MissionPrimary.FEATURE_EXTENSION: (
            "기존 기능 확장",
            "additive 변경과 adapter/extension point를 우선하고 회귀를 검증합니다.",
        ),
        MissionPrimary.LEGACY_REFACTOR: (
            "레거시 리팩터링",
            "characterization test와 seam을 만든 뒤 점진적으로 교체합니다.",
        ),
        MissionPrimary.BUGFIX_STABILIZATION: (
            "버그 수정·안정화",
            "재현과 원인을 먼저 고정하고 causal minimal fix와 재발 방지 검사를 만듭니다.",
        ),
        MissionPrimary.MIGRATION_MODERNIZATION: (
            "마이그레이션·현대화",
            "expand–migrate–contract, checkpoint와 reconciliation을 사용합니다.",
        ),
        MissionPrimary.ANALYSIS_AUDIT: (
            "분석·감사",
            "제품 상태를 바꾸지 않고 권위 근거와 재현 가능한 분석 artifact를 만듭니다.",
        ),
    }
    return tuple(
        PlanningMissionOption(
            primary=primary,
            label=labels_and_strategies[primary][0],
            planning_strategy=labels_and_strategies[primary][1],
            default_mutation_policy=_mission_defaults(primary)[0],
            default_behavior_preservation=_mission_defaults(primary)[1],
        )
        for primary in MissionPrimary
    )


class ProfileInspectionReceipt(R31Model):
    profile_revision_id: str
    profile_definition_digest: str
    stale_sections: tuple[str, ...] = ()
    unknown_sections: tuple[str, ...] = ()
    digest_mismatch_sections: tuple[str, ...] = ()

    @property
    def has_integrity_failure(self) -> bool:
        return bool(self.digest_mismatch_sections)


class ProjectProfileInspector:
    SECTION_NAMES = ("architecture", "validation", "runtime", "risk", "compatibility")

    def inspect(
        self,
        profile: ProjectProfileRevision,
        *,
        observed_source_digests: dict[str, str] | None = None,
    ) -> ProfileInspectionReceipt:
        observed = observed_source_digests or {}
        stale: list[str] = []
        unknown: list[str] = []
        mismatched: list[str] = []
        for name in self.SECTION_NAMES:
            section: ProfileSection = getattr(profile.definition, name)
            if section.freshness is ProfileFreshness.STALE:
                stale.append(name)
            if section.unknown:
                unknown.append(name)
            if observed and section.source_digest is not None:
                observed_digest = observed.get(name)
                if observed_digest is None and len(section.source_refs) == 1:
                    observed_digest = observed.get(section.source_refs[0])
                if observed_digest is not None and observed_digest != section.source_digest:
                    mismatched.append(name)
        return ProfileInspectionReceipt(
            profile_revision_id=profile.profile_revision_id,
            profile_definition_digest=profile.definition_digest,
            stale_sections=tuple(stale),
            unknown_sections=tuple(unknown),
            digest_mismatch_sections=tuple(mismatched),
        )


def build_unknown_profile_definition(
    *,
    product_goal: str,
    lifecycle_stage: LifecycleStage = LifecycleStage.PROTOTYPE,
    criticality: Criticality = Criticality.STANDARD,
    compatibility_policy: CompatibilityPolicy = CompatibilityPolicy.PRESERVE,
    default_risk_tolerance: RiskTolerance = RiskTolerance.BALANCED,
) -> ProjectProfileDefinition:
    """등록 정보가 없을 때 사실을 발명하지 않는 명시적 unknown profile."""

    unknown = ProfileSection(
        freshness=ProfileFreshness.UNKNOWN,
        unknown=True,
        unknown_reasons=("프로젝트 profile source가 아직 등록되지 않았습니다.",),
    )
    return ProjectProfileDefinition(
        product_goal=product_goal,
        lifecycle_stage=lifecycle_stage,
        criticality=criticality,
        compatibility_policy=compatibility_policy,
        default_risk_tolerance=default_risk_tolerance,
        architecture=unknown,
        validation=unknown,
        runtime=unknown,
        risk=unknown,
        compatibility=unknown,
    )


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


_KEYWORDS: dict[MissionPrimary, tuple[str, ...]] = {
    MissionPrimary.ANALYSIS_AUDIT: (
        "분석",
        "감사",
        "검토",
        "진단",
        "audit",
        "analyze",
        "analysis",
        "review",
        "inspect",
    ),
    MissionPrimary.BUGFIX_STABILIZATION: (
        "버그",
        "오류",
        "장애",
        "고쳐",
        "bug",
        "fix",
        "regression",
        "stabilize",
    ),
    MissionPrimary.MIGRATION_MODERNIZATION: (
        "마이그레이션",
        "이관",
        "전환",
        "업그레이드",
        "modernize",
        "migration",
        "migrate",
        "upgrade",
    ),
    MissionPrimary.LEGACY_REFACTOR: (
        "레거시",
        "리팩터",
        "리팩토",
        "구조 개선",
        "legacy",
        "refactor",
    ),
    MissionPrimary.NEW_BUILD: (
        "새 프로젝트",
        "처음부터",
        "신규 구축",
        "새로 만들어",
        "create from scratch",
        "greenfield",
        "new build",
    ),
    MissionPrimary.FEATURE_EXTENSION: (
        "기능 추가",
        "확장",
        "연동 추가",
        "구현해",
        "feature",
        "extend",
        "add support",
        "implement",
    ),
}

_READ_ONLY_MARKERS = (
    "분석만",
    "검토만",
    "수정하지",
    "변경하지",
    "읽기 전용",
    "read only",
    "read-only",
    "do not change",
    "without changing",
)

_MUTATION_MARKERS = (
    "구현",
    "추가",
    "수정",
    "고쳐",
    "만들",
    "리팩터",
    "리팩토",
    "마이그레이션",
    "전환",
    "implement",
    "add ",
    "fix",
    "change",
    "refactor",
    "migrate",
)


def _mission_defaults(primary: MissionPrimary) -> tuple[MutationPolicy, BehaviorPreservation]:
    if primary is MissionPrimary.ANALYSIS_AUDIT:
        return MutationPolicy.READ_ONLY, BehaviorPreservation.NOT_APPLICABLE
    if primary is MissionPrimary.BUGFIX_STABILIZATION:
        return (
            MutationPolicy.MINIMAL_CHANGE,
            BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
        )
    if primary is MissionPrimary.LEGACY_REFACTOR:
        return (
            MutationPolicy.STRUCTURAL_CHANGE,
            BehaviorPreservation.PRESERVE_OBSERVED_BEHAVIOR,
        )
    if primary is MissionPrimary.MIGRATION_MODERNIZATION:
        return (
            MutationPolicy.MIGRATION_CHANGE,
            BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
        )
    if primary is MissionPrimary.NEW_BUILD:
        return MutationPolicy.SCOPED_CHANGE, BehaviorPreservation.NOT_APPLICABLE
    return (
        MutationPolicy.SCOPED_CHANGE,
        BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
    )


def _risk_tags(text: str, primary: MissionPrimary) -> tuple[RiskTag, ...]:
    tags: list[RiskTag] = []

    def add(tag: RiskTag, *keywords: str) -> None:
        if _contains_any(text, keywords) and tag not in tags:
            tags.append(tag)

    if primary in {
        MissionPrimary.FEATURE_EXTENSION,
        MissionPrimary.LEGACY_REFACTOR,
        MissionPrimary.BUGFIX_STABILIZATION,
        MissionPrimary.MIGRATION_MODERNIZATION,
    }:
        tags.append(RiskTag.EXISTING_BEHAVIOR)
    add(RiskTag.PUBLIC_CONTRACT_CHANGE, "api", "계약", "인터페이스", "breaking")
    add(RiskTag.PERSISTENT_STATE_CHANGE, "db", "database", "데이터", "schema", "스키마")
    add(RiskTag.DESTRUCTIVE_EFFECT, "삭제", "drop", "파괴", "overwrite")
    add(RiskTag.EXTERNAL_EFFECT, "외부", "webhook", "결제", "메일", "external")
    add(RiskTag.SHARED_CONCURRENCY, "동시", "worker", "queue", "경쟁", "concurr")
    add(RiskTag.SECURITY_SENSITIVE, "보안", "인증", "권한", "secret", "security", "auth")
    add(RiskTag.SCALE_OR_SLO, "성능", "slo", "대규모", "latency", "throughput")
    add(RiskTag.HUMAN_CHECKPOINT, "승인", "운영", "사람", "approval", "production")
    return tuple(tags)


def build_mission(
    primary: MissionPrimary,
    request_spec: RequestSpec,
    *,
    selected_by: MissionSelectedBy,
    confidence: ConfidenceLevel,
    secondary: MissionPrimary | None = None,
) -> PlanningMissionDefinition:
    mutation, preservation = _mission_defaults(primary)
    return PlanningMissionDefinition(
        primary=primary,
        secondary=secondary,
        observable_outcome=request_spec.request_summary,
        mutation_policy=mutation,
        behavior_preservation=preservation,
        allowed_external_effects=(),
        forbidden_scopes=request_spec.out_of_scope,
        risk_tags=_risk_tags(request_spec.user_request.casefold(), primary),
        selected_by=selected_by,
        confidence=confidence,
        mission_policy_id="flowmarshal-mission",
        mission_policy_version="v1",
    )


class PlanningMissionResolver:
    """Mission을 보수적으로 해석한다.

    이 클래스의 휴리스틱은 추천의 결정적 기준선이다. 모델이 더 풍부한 해석을
    제공할 때도 ``MissionResolutionHint``라는 typed 입력으로만 수용한다.
    """

    def resolve(
        self,
        request_spec: RequestSpec,
        profile_revision: ProjectProfileRevision,
        user_selection: PlanningMissionDefinition | MissionPrimary | str | None = None,
        *,
        hint: MissionResolutionHint | None = None,
        profile_inspection: ProfileInspectionReceipt | None = None,
    ) -> MissionSelectionReceipt:
        self._validate_binding(request_spec, profile_revision)
        inspection = profile_inspection or ProjectProfileInspector().inspect(profile_revision)
        if (
            inspection.profile_revision_id != profile_revision.profile_revision_id
            or inspection.profile_definition_digest != profile_revision.definition_digest
        ):
            raise ValueError("ProfileInspectionReceipt가 현재 profile과 다릅니다.")
        warnings = tuple(
            [f"stale profile section: {name}" for name in inspection.stale_sections]
            + [f"unknown profile section: {name}" for name in inspection.unknown_sections]
        )
        if inspection.has_integrity_failure:
            return self._receipt(
                request_spec,
                profile_revision,
                status=MissionResolutionStatus.BLOCKED,
                conflicts=tuple(
                    f"profile source digest mismatch: {name}"
                    for name in inspection.digest_mismatch_sections
                ),
                warnings=warnings,
            )
        text = request_spec.user_request.casefold()
        explicit_read_only = _contains_any(text, _READ_ONLY_MARKERS)
        mutation_evidence_text = text
        for marker in _READ_ONLY_MARKERS:
            mutation_evidence_text = mutation_evidence_text.replace(marker, " ")
        explicit_mutation = _contains_any(mutation_evidence_text, _MUTATION_MARKERS)
        selected = self._coerce_selection(request_spec, user_selection)
        if selected is not None:
            conflicts = self._explicit_mission_conflicts(
                explicit_read_only,
                explicit_mutation,
                selected,
            )
            if conflicts:
                return self._receipt(
                    request_spec,
                    profile_revision,
                    status=MissionResolutionStatus.BLOCKED,
                    conflicts=tuple(conflicts),
                    warnings=warnings,
                )
            return self._receipt(
                request_spec,
                profile_revision,
                status=MissionResolutionStatus.RESOLVED,
                mission=selected,
                warnings=warnings,
            )

        ranked = self._rank(text)
        mutating_primary = next(
            (primary for primary in ranked if primary is not MissionPrimary.ANALYSIS_AUDIT),
            MissionPrimary.FEATURE_EXTENSION,
        )
        if explicit_read_only and explicit_mutation:
            options = (
                build_mission(
                    MissionPrimary.ANALYSIS_AUDIT,
                    request_spec,
                    selected_by=MissionSelectedBy.INFERRED,
                    confidence=ConfidenceLevel.LOW,
                ),
                build_mission(
                    mutating_primary,
                    request_spec,
                    selected_by=MissionSelectedBy.INFERRED,
                    confidence=ConfidenceLevel.LOW,
                ),
            )
            return self._receipt(
                request_spec,
                profile_revision,
                status=MissionResolutionStatus.NEEDS_USER_INPUT,
                options=options,
                question="이번 요청은 분석 산출물까지만 필요한가요, 아니면 구현 변경까지 필요한가요?",
                warnings=warnings,
            )

        if hint is not None:
            hinted = self._resolve_hint(request_spec, profile_revision, None, hint)
            if hinted is not None:
                if (
                    hinted.status is MissionResolutionStatus.RESOLVED
                    and hinted.mission is not None
                ):
                    hint_conflicts = self._explicit_mission_conflicts(
                        explicit_read_only,
                        explicit_mutation,
                        hinted.mission,
                    )
                    if hint_conflicts:
                        warnings = tuple(
                            (
                                *warnings,
                                "명시적 요청과 충돌하는 모델 Mission 추천을 적용하지 않았습니다.",
                            )
                        )
                    else:
                        return hinted.model_copy(
                            update={"warnings": tuple((*hinted.warnings, *warnings))}
                        )
                else:
                    return hinted.model_copy(
                        update={"warnings": tuple((*hinted.warnings, *warnings))}
                    )

        primary = MissionPrimary.ANALYSIS_AUDIT if explicit_read_only else ranked[0]
        top_score = self._score(text, primary)
        next_score = self._score(text, ranked[1]) if len(ranked) > 1 else 0
        confidence = (
            ConfidenceLevel.HIGH
            if top_score >= 2 and top_score > next_score
            else ConfidenceLevel.MEDIUM
        )
        mission = build_mission(
            primary,
            request_spec,
            selected_by=MissionSelectedBy.INFERRED,
            confidence=confidence,
        )
        if warnings and mission.confidence is ConfidenceLevel.HIGH:
            mission = mission.model_copy(update={"confidence": ConfidenceLevel.MEDIUM})
        return self._receipt(
            request_spec,
            profile_revision,
            status=MissionResolutionStatus.RESOLVED,
            mission=mission,
            warnings=warnings,
        )

    @staticmethod
    def _explicit_mission_conflicts(
        explicit_read_only: bool,
        explicit_mutation: bool,
        mission: PlanningMissionDefinition,
    ) -> list[str]:
        conflicts: list[str] = []
        if explicit_read_only and mission.mutation_policy is not MutationPolicy.READ_ONLY:
            conflicts.append("현재 요청은 상태 변경을 금지하지만 선택 Mission은 변경을 요구합니다.")
        if (
            explicit_mutation
            and not explicit_read_only
            and mission.primary is MissionPrimary.ANALYSIS_AUDIT
        ):
            conflicts.append("현재 요청은 구현 결과를 요구하지만 analysis_audit가 선택됐습니다.")
        return conflicts

    @staticmethod
    def _validate_binding(request_spec: RequestSpec, profile: ProjectProfileRevision) -> None:
        if request_spec.project_id != profile.project_id:
            raise ValueError("RequestSpec과 ProjectProfile의 project_id가 다릅니다.")

    def _coerce_selection(
        self,
        request_spec: RequestSpec,
        selection: PlanningMissionDefinition | MissionPrimary | str | None,
    ) -> PlanningMissionDefinition | None:
        if selection is None:
            return None
        if isinstance(selection, PlanningMissionDefinition):
            return selection.model_copy(
                update={
                    "selected_by": MissionSelectedBy.USER,
                    "confidence": ConfidenceLevel.HIGH,
                }
            )
        primary = selection if isinstance(selection, MissionPrimary) else MissionPrimary(selection)
        return build_mission(
            primary,
            request_spec,
            selected_by=MissionSelectedBy.USER,
            confidence=ConfidenceLevel.HIGH,
        )

    def _resolve_hint(
        self,
        request_spec: RequestSpec,
        profile: ProjectProfileRevision,
        selection: PlanningMissionDefinition | MissionPrimary | str | None,
        hint: MissionResolutionHint,
    ) -> MissionSelectionReceipt | None:
        if selection is not None:
            return None
        if hint.kind is MissionResolutionHintKind.CONFLICT:
            return self._receipt(
                request_spec,
                profile,
                status=MissionResolutionStatus.BLOCKED,
                conflicts=hint.reasons or ("현재 요청과 Mission 후보가 충돌합니다.",),
            )
        if hint.kind is MissionResolutionHintKind.OUTCOME_AMBIGUOUS:
            return self._receipt(
                request_spec,
                profile,
                status=MissionResolutionStatus.NEEDS_USER_INPUT,
                options=hint.options,
                question=hint.question or "원하는 최종 결과를 선택해 주세요.",
            )
        if hint.recommended is not None:
            recommended = hint.recommended.model_copy(
                update={"selected_by": MissionSelectedBy.INFERRED}
            )
            return self._receipt(
                request_spec,
                profile,
                status=MissionResolutionStatus.RESOLVED,
                mission=recommended,
                options=hint.options,
            )
        return None

    @staticmethod
    def _score(text: str, primary: MissionPrimary) -> int:
        return sum(1 for keyword in _KEYWORDS[primary] if keyword in text)

    def _rank(self, text: str) -> list[MissionPrimary]:
        order = [
            MissionPrimary.BUGFIX_STABILIZATION,
            MissionPrimary.MIGRATION_MODERNIZATION,
            MissionPrimary.LEGACY_REFACTOR,
            MissionPrimary.NEW_BUILD,
            MissionPrimary.FEATURE_EXTENSION,
            MissionPrimary.ANALYSIS_AUDIT,
        ]
        ranked = sorted(order, key=lambda item: (-self._score(text, item), order.index(item)))
        if self._score(text, ranked[0]) == 0:
            ranked.remove(MissionPrimary.FEATURE_EXTENSION)
            ranked.insert(0, MissionPrimary.FEATURE_EXTENSION)
        return ranked

    @staticmethod
    def _receipt(
        request_spec: RequestSpec,
        profile: ProjectProfileRevision,
        *,
        status: MissionResolutionStatus,
        mission: PlanningMissionDefinition | None = None,
        options: tuple[PlanningMissionDefinition, ...] = (),
        question: str | None = None,
        conflicts: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
        override_receipts: tuple[ProfileOverrideReceipt, ...] = (),
    ) -> MissionSelectionReceipt:
        return MissionSelectionReceipt(
            request_spec_digest=request_spec.canonical_digest,
            raw_request_digest=sha256_bytes(request_spec.user_request.encode("utf-8")),
            profile_revision_id=profile.profile_revision_id,
            profile_definition_digest=profile.definition_digest,
            status=status,
            mission=mission,
            options=options,
            question=question,
            conflicts=conflicts,
            warnings=warnings,
            override_receipts=override_receipts,
        )


class EffectivePlanningPolicyBuilder:
    """명시 요청 > 사용자 Mission > profile 순서를 실제 정책으로 고정한다."""

    def build(
        self,
        profile: ProjectProfileRevision,
        mission_selection: MissionSelectionReceipt,
        *,
        explicit_request_constraints: tuple[ExplicitRequestConstraint, ...] = (),
        requirement_extraction: RequirementExtractionReceipt | None = None,
        explicit_compatibility_policy: CompatibilityPolicy | None = None,
        explicit_risk_tolerance: RiskTolerance | None = None,
    ) -> EffectivePlanningPolicy:
        if mission_selection.status is not MissionResolutionStatus.RESOLVED:
            raise ValueError("확정되지 않은 Mission으로 정책을 만들 수 없습니다.")
        if mission_selection.profile_revision_id != profile.profile_revision_id:
            raise ValueError("Mission과 ProjectProfile revision이 다릅니다.")
        mission = mission_selection.mission
        assert mission is not None
        if requirement_extraction is not None:
            if explicit_request_constraints:
                raise ValueError(
                    "검토된 extraction과 수동 explicit constraint를 동시에 지정할 수 없습니다."
                )
            if (
                requirement_extraction.mission_resolution_digest
                != mission_selection.mission_resolution_digest
            ):
                raise ValueError("정책 입력 extraction과 Mission resolution이 다릅니다.")
            explicit_request_constraints = tuple(
                ExplicitRequestConstraint(
                    constraint_id=(
                        "constraint."
                        + sha256_digest(
                            {
                                "statement": requirement.statement,
                                "trace_ref": trace_ref,
                            }
                        )[7:23]
                    ),
                    statement=requirement.statement,
                    source_ref=trace_ref,
                )
                for requirement in requirement_extraction.requirements
                if requirement.kind is RequirementKind.CONSTRAINT
                for trace_ref in requirement.trace_refs
            )

        compatibility = profile.definition.compatibility_policy
        risk_tolerance = profile.definition.default_risk_tolerance
        overrides = list(mission_selection.override_receipts)
        if (
            mission.behavior_preservation
            is BehaviorPreservation.ALLOW_EXPLICIT_BREAKING_CHANGES
            and mission.selected_by is MissionSelectedBy.USER
            and compatibility is not CompatibilityPolicy.FLEXIBLE
        ):
            overrides.append(
                ProfileOverrideReceipt(
                    field_name="compatibility_policy",
                    profile_value=compatibility.value,
                    effective_value=CompatibilityPolicy.FLEXIBLE.value,
                    winning_authority="user_mission",
                    reason="사용자가 Mission에서 breaking change를 명시적으로 허용했습니다.",
                )
            )
            compatibility = CompatibilityPolicy.FLEXIBLE
        if explicit_compatibility_policy is not None and explicit_compatibility_policy != compatibility:
            overrides.append(
                ProfileOverrideReceipt(
                    field_name="compatibility_policy",
                    profile_value=compatibility.value,
                    effective_value=explicit_compatibility_policy.value,
                    winning_authority="explicit_request",
                    reason="현재 요청의 명시적 호환성 제약이 기본값보다 우선합니다.",
                )
            )
            compatibility = explicit_compatibility_policy
        if explicit_risk_tolerance is not None and explicit_risk_tolerance != risk_tolerance:
            overrides.append(
                ProfileOverrideReceipt(
                    field_name="risk_tolerance",
                    profile_value=risk_tolerance.value,
                    effective_value=explicit_risk_tolerance.value,
                    winning_authority="explicit_request",
                    reason="현재 요청의 명시적 위험 허용도가 profile 기본값보다 우선합니다.",
                )
            )
            risk_tolerance = explicit_risk_tolerance

        return EffectivePlanningPolicy(
            policy_id="balanced-mvp",
            policy_version="v0",
            profile_definition_digest=profile.definition_digest,
            mission_resolution_digest=mission_selection.mission_resolution_digest,
            lifecycle_stage=profile.definition.lifecycle_stage,
            criticality=profile.definition.criticality,
            compatibility_policy=compatibility,
            risk_tolerance=risk_tolerance,
            mutation_policy=mission.mutation_policy,
            behavior_preservation=mission.behavior_preservation,
            allowed_external_effects=mission.allowed_external_effects,
            forbidden_scopes=mission.forbidden_scopes,
            explicit_request_constraints=explicit_request_constraints,
            override_receipts=tuple(overrides),
        )


_CONTEXT_KIND_TO_SNAPSHOT = {
    ContextSourceKind.PROJECT_INSTRUCTIONS: SnapshotEntryKind.REFERENCE,
    ContextSourceKind.PROJECT_FILE: SnapshotEntryKind.FILE,
    ContextSourceKind.REFERENCE: SnapshotEntryKind.REFERENCE,
    ContextSourceKind.EXTERNAL_REFERENCE: SnapshotEntryKind.REFERENCE,
}


class ProjectStateSnapshotBuilder:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: f"snapshot_{uuid4().hex}")

    def capture(
        self,
        request_spec: RequestSpec,
        mission_selection: MissionSelectionReceipt,
        *,
        extra_entries: tuple[ProjectStateEntry, ...] = (),
        unknowns: tuple[str, ...] = (),
    ) -> ProjectStateSnapshot:
        if mission_selection.status is not MissionResolutionStatus.RESOLVED:
            raise ValueError("Mission 확정 전에는 ProjectStateSnapshot을 만들 수 없습니다.")
        entries = tuple(
            ProjectStateEntry(
                entry_id=f"context_{index:03d}",
                kind=_CONTEXT_KIND_TO_SNAPSHOT[source.kind],
                path=source.path,
                content_digest=source.content_digest,
                relevance=source.purpose,
            )
            for index, source in enumerate(request_spec.context_sources, start=1)
        )
        return ProjectStateSnapshot(
            snapshot_id=self._id_factory(),
            project_id=request_spec.project_id,
            mission_resolution_digest=mission_selection.mission_resolution_digest,
            entries=(*entries, *extra_entries),
            unknowns=unknowns,
            captured_at=self._now(),
        )


class RequestSpecAssemblyContext(R31Model):
    """요구 추출 결과와 무관한 R3 RequestSpec 조립 입력."""

    project_id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    project_name: str = Field(min_length=1, max_length=120)
    project_root: str = Field(min_length=3, max_length=2000)
    project_description: str = Field(min_length=1, max_length=5000)
    project_instruction_path: str | None = Field(default=None, max_length=2000)
    parent_revision_id: str | None = Field(
        default=None, pattern=r"^revision_[0-9a-f]{32}$"
    )
    registered_context_sources: tuple[ContextFileRegistration, ...] = ()
    available_validations: tuple[ValidationCapability, ...] = Field(min_length=1)
    product_capabilities: tuple[str, ...] = ()
    planning_limits: PlanningLimits = Field(default_factory=PlanningLimits)


class RequirementAnalysisAssembly(R31Model):
    """최종 R3 입력과 그 semantic provenance를 함께 반환하는 결과."""

    analysis_context_digest: str
    extraction_draft_digest: str
    assembly_input: RequestSpecAssemblyInput
    request_spec: RequestSpec
    mission_selection: MissionSelectionReceipt
    project_snapshot: ProjectStateSnapshot
    extraction_receipt: RequirementExtractionReceipt


class RequirementAnalyzer:
    """typed extraction을 검증하고 기존 R3 입력에 결정적으로 결속한다."""

    def __init__(self, assembler: RequestSpecAssembler | None = None) -> None:
        self._assembler = assembler or RequestSpecAssembler()

    def context(
        self,
        raw_request: str,
        mission_selection: MissionSelectionReceipt,
        profile_revision: ProjectProfileRevision,
        profile_sections: tuple[ProfileSectionName, ...],
        project_snapshot: ProjectStateSnapshot,
    ) -> RequirementAnalysisContext:
        """요구 추출 역할에 넘길 최소·불변 입력을 만든다."""

        if project_snapshot.project_id != profile_revision.project_id:
            raise ValueError("ProjectStateSnapshot과 ProjectProfile의 project_id가 다릅니다.")
        if (
            mission_selection.status is not MissionResolutionStatus.RESOLVED
            or mission_selection.mission is None
        ):
            raise ValueError("확정되지 않은 Mission에서는 요구 분석 context를 만들 수 없습니다.")
        if mission_selection.profile_revision_id != profile_revision.profile_revision_id:
            raise ValueError("Mission과 ProjectProfile revision이 다릅니다.")
        if mission_selection.profile_definition_digest != profile_revision.definition_digest:
            raise ValueError("Mission과 ProjectProfile definition digest가 다릅니다.")
        if (
            project_snapshot.mission_resolution_digest
            != mission_selection.mission_resolution_digest
        ):
            raise ValueError("ProjectStateSnapshot과 Mission resolution digest가 다릅니다.")
        if not profile_sections:
            raise ValueError("요구 추출에는 하나 이상의 ProjectProfile section이 필요합니다.")
        selected = tuple(
            SelectedProfileSection(
                name=name,
                section=getattr(profile_revision.definition, name.value),
            )
            for name in profile_sections
        )
        return RequirementAnalysisContext(
            raw_request=raw_request,
            raw_request_digest=sha256_bytes(raw_request.encode("utf-8")),
            source_request_spec_digest=mission_selection.request_spec_digest,
            mission=mission_selection.mission,
            mission_resolution_digest=mission_selection.mission_resolution_digest,
            mission_options=mission_selection.options,
            mission_warnings=mission_selection.warnings,
            mission_override_receipts=mission_selection.override_receipts,
            profile_revision_id=profile_revision.profile_revision_id,
            profile_definition_digest=profile_revision.definition_digest,
            profile_sections=selected,
            project_snapshot=project_snapshot,
        )

    def analyze(
        self,
        context: RequirementAnalysisContext,
        draft: RequirementExtractionDraft,
    ) -> RequirementExtractionDraft:
        """모델 draft의 span·source·semantic binding을 결정적으로 검증한다."""

        if draft.raw_request_digest != context.raw_request_digest:
            raise ValueError("extraction draft의 raw request digest가 다릅니다.")
        if draft.mission_digest != context.mission.mission_digest:
            raise ValueError("extraction draft의 Mission digest가 다릅니다.")
        if draft.profile_definition_digest != context.profile_definition_digest:
            raise ValueError("extraction draft의 ProjectProfile digest가 다릅니다.")
        if draft.project_snapshot_digest != context.project_snapshot.snapshot_digest:
            raise ValueError("extraction draft의 ProjectStateSnapshot digest가 다릅니다.")

        for trace in draft.traces:
            if trace.end_offset > len(context.raw_request):
                raise ValueError("raw request trace 범위가 사용자 원문을 벗어납니다.")
            observed = context.raw_request[trace.start_offset : trace.end_offset]
            if observed != trace.excerpt:
                raise ValueError("raw request trace excerpt가 선언한 원문 범위와 다릅니다.")

        allowed_profile_refs = {
            ref
            for selected in context.profile_sections
            for ref in selected.section.source_refs
        }
        known_snapshot_entries = {
            entry.entry_id for entry in context.project_snapshot.entries
        }
        for item in (
            *draft.requirements,
            *draft.assumptions,
            *draft.implementation_suggestions,
        ):
            unknown_profile = set(item.profile_source_refs) - allowed_profile_refs
            if unknown_profile:
                raise ValueError(
                    "선택되지 않았거나 알려지지 않은 profile source를 참조합니다: "
                    f"{sorted(unknown_profile)}"
                )
            unknown_snapshot = set(item.snapshot_entry_refs) - known_snapshot_entries
            if unknown_snapshot:
                raise ValueError(
                    "알 수 없는 ProjectStateSnapshot entry를 참조합니다: "
                    f"{sorted(unknown_snapshot)}"
                )

        outcomes = [
            item
            for item in draft.requirements
            if item.kind is RequirementKind.OBSERVABLE_OUTCOME
        ]
        if len(outcomes) != 1 or outcomes[0].statement != context.mission.observable_outcome:
            raise ValueError(
                "extraction draft는 확정 Mission의 observable outcome을 정확히 한 번 포함해야 합니다."
            )
        exclusions = {
            item.statement
            for item in draft.requirements
            if item.kind is RequirementKind.EXCLUSION
        }
        missing_forbidden = set(context.mission.forbidden_scopes) - exclusions
        if missing_forbidden:
            raise ValueError(
                "Mission forbidden scope가 extraction draft에서 누락됐습니다: "
                f"{sorted(missing_forbidden)}"
            )
        if not set(context.project_snapshot.unknowns).issubset(draft.unresolved_items):
            raise ValueError("ProjectStateSnapshot의 unknown을 extraction draft가 숨겼습니다.")
        return draft

    @staticmethod
    def _normalized_path(value: str) -> str:
        return str(Path(value).resolve(strict=False)).casefold()

    def _request_requirement(
        self,
        item: ExtractedPlanningRequirement,
        assembly_context: RequestSpecAssemblyContext,
        project_snapshot: ProjectStateSnapshot,
    ) -> RequirementSpec:
        registered_ids = {
            "project-agents",
            *(source.source_id for source in assembly_context.registered_context_sources),
        }
        instruction_path = assembly_context.project_instruction_path or str(
            Path(assembly_context.project_root) / "AGENTS.md"
        )
        path_to_source = {
            self._normalized_path(instruction_path): "project-agents",
            **{
                self._normalized_path(source.path): source.source_id
                for source in assembly_context.registered_context_sources
            },
        }
        snapshot_by_id = {entry.entry_id: entry for entry in project_snapshot.entries}
        snapshot_sources: set[str] = set()
        for entry_id in item.snapshot_entry_refs:
            source_id = path_to_source.get(
                self._normalized_path(snapshot_by_id[entry_id].path)
            )
            if source_id is None:
                raise ValueError(
                    "requirement 근거인 snapshot entry가 RequestSpec context로 등록되지 "
                    f"않았습니다: {entry_id}"
                )
            snapshot_sources.add(source_id)
        source_refs = tuple(sorted({*item.profile_source_refs, *snapshot_sources}))
        unknown_sources = set(source_refs) - registered_ids
        if unknown_sources:
            raise ValueError(
                "profile source가 RequestSpec context로 등록되지 않았습니다: "
                f"{sorted(unknown_sources)}"
            )
        has_raw_trace = bool(item.trace_refs)
        if has_raw_trace:
            source = RequirementSource.USER
        elif source_refs == ("project-agents",):
            source = RequirementSource.PROJECT_INSTRUCTION
        else:
            source = RequirementSource.REFERENCE
        if source is not RequirementSource.USER and not source_refs:
            raise ValueError(
                f"requirement {item.requirement_id}를 RequestSpec source에 결속할 수 없습니다."
            )
        return RequirementSpec(
            requirement_id=item.requirement_id,
            statement=item.statement,
            source=source,
            priority=(
                RequirementPriority.MUST
                if item.mandatory
                else RequirementPriority.SHOULD
            ),
            source_refs=source_refs,
        )

    def prepare_assembly(
        self,
        context: RequirementAnalysisContext,
        draft: RequirementExtractionDraft,
        assembly_context: RequestSpecAssemblyContext,
    ) -> RequestSpecAssemblyInput:
        """검증된 draft에서 기존 R3 RequestSpecAssemblyInput을 만든다."""

        draft = self.analyze(context, draft)
        if assembly_context.project_id != context.project_snapshot.project_id:
            raise ValueError("조립 context와 ProjectStateSnapshot의 project_id가 다릅니다.")
        request_requirements = tuple(
            self._request_requirement(
                item,
                assembly_context,
                context.project_snapshot,
            )
            for item in draft.requirements
            if item.kind in {RequirementKind.REQUIREMENT, RequirementKind.CONSTRAINT}
        )
        if not request_requirements:
            raise ValueError("RequestSpec에는 하나 이상의 requirement 또는 constraint가 필요합니다.")
        out_of_scope = tuple(
            item.statement
            for item in draft.requirements
            if item.kind is RequirementKind.EXCLUSION
        )
        return RequestSpecAssemblyInput(
            project_id=assembly_context.project_id,
            project_name=assembly_context.project_name,
            project_root=assembly_context.project_root,
            project_description=assembly_context.project_description,
            project_instruction_path=assembly_context.project_instruction_path,
            user_request=context.raw_request,
            request_summary=draft.request_summary,
            parent_revision_id=assembly_context.parent_revision_id,
            requirements=request_requirements,
            registered_context_sources=assembly_context.registered_context_sources,
            available_validations=assembly_context.available_validations,
            product_capabilities=assembly_context.product_capabilities,
            out_of_scope=out_of_scope,
            planning_limits=assembly_context.planning_limits,
        )

    def _assert_context_matches_reviewed_snapshot(
        self,
        request_spec: RequestSpec,
        project_snapshot: ProjectStateSnapshot,
    ) -> None:
        """assembler 재조회 결과가 intent review 당시 파일과 같은지 확인한다."""

        snapshot_by_path: dict[str, str] = {}
        for entry in project_snapshot.entries:
            normalized_path = self._normalized_path(entry.path)
            if normalized_path in snapshot_by_path:
                raise ValueError("ProjectStateSnapshot에 동일한 정규화 경로가 중복됐습니다.")
            snapshot_by_path[normalized_path] = entry.content_digest
        for source in request_spec.context_sources:
            reviewed_digest = snapshot_by_path.get(self._normalized_path(source.path))
            if reviewed_digest is None:
                raise ValueError(
                    f"조립된 context가 intent review snapshot에 없습니다: {source.path}"
                )
            if reviewed_digest != source.content_digest:
                raise ValueError(
                    "intent review 뒤 context 파일이 변경됐습니다. snapshot을 다시 "
                    f"수집하고 재검토해야 합니다: {source.path}"
                )

    def assemble(
        self,
        context: RequirementAnalysisContext,
        draft: RequirementExtractionDraft,
        assembly_context: RequestSpecAssemblyContext,
        *,
        intent_review: RequirementIntentReview,
    ) -> RequirementAnalysisAssembly:
        """RequestSpecAssembler로 최종 digest를 만든 뒤 모든 receipt를 결속한다."""

        if intent_review.analysis_context_digest != context.context_digest:
            raise ValueError("intent review의 analysis context digest가 다릅니다.")
        if intent_review.extraction_draft_digest != draft.draft_digest:
            raise ValueError("intent review의 extraction draft digest가 다릅니다.")
        if intent_review.verdict is not IntentReviewVerdict.PASS:
            raise ValueError("독립 intent review를 통과하지 못한 추출 결과입니다.")
        assembly_input = self.prepare_assembly(context, draft, assembly_context)
        request_spec = self._assembler.build(assembly_input)
        self._assert_context_matches_reviewed_snapshot(
            request_spec,
            context.project_snapshot,
        )
        mission_selection = MissionSelectionReceipt(
            request_spec_digest=request_spec.canonical_digest,
            raw_request_digest=context.raw_request_digest,
            profile_revision_id=context.profile_revision_id,
            profile_definition_digest=context.profile_definition_digest,
            status=MissionResolutionStatus.RESOLVED,
            mission=context.mission,
            options=context.mission_options,
            warnings=context.mission_warnings,
            override_receipts=context.mission_override_receipts,
        )
        bound_snapshot_digest_seed = sha256_digest(
            {
                "source_snapshot_digest": context.project_snapshot.snapshot_digest,
                "mission_resolution_digest": (
                    mission_selection.mission_resolution_digest
                ),
            }
        )
        bound_snapshot = ProjectStateSnapshot(
            snapshot_id=f"snapshot_{bound_snapshot_digest_seed[7:39]}",
            project_id=context.project_snapshot.project_id,
            mission_resolution_digest=mission_selection.mission_resolution_digest,
            entries=context.project_snapshot.entries,
            unknowns=context.project_snapshot.unknowns,
            captured_at=context.project_snapshot.captured_at,
        )
        receipt = RequirementExtractionReceipt(
            analysis_context_digest=context.context_digest,
            raw_request_digest=context.raw_request_digest,
            request_spec_digest=request_spec.canonical_digest,
            mission_resolution_digest=mission_selection.mission_resolution_digest,
            project_snapshot_digest=bound_snapshot.snapshot_digest,
            traces=draft.traces,
            requirements=draft.requirements,
            profile_definition_digest=context.profile_definition_digest,
            source_project_snapshot_digest=context.project_snapshot.snapshot_digest,
            extraction_draft_digest=draft.draft_digest,
            intent_review_digest=intent_review.review_digest,
            selected_profile_sections=tuple(
                item.name for item in context.profile_sections
            ),
            assumptions=draft.assumptions,
            implementation_suggestions=draft.implementation_suggestions,
            unresolved_items=draft.unresolved_items,
        )
        return RequirementAnalysisAssembly(
            analysis_context_digest=context.context_digest,
            extraction_draft_digest=draft.draft_digest,
            assembly_input=assembly_input,
            request_spec=request_spec,
            mission_selection=mission_selection,
            project_snapshot=bound_snapshot,
            extraction_receipt=receipt,
        )
