from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from ..canonical import canonical_json, sha256_bytes
from .domain import (
    ContextSourceKind,
    ContextSourceSpec,
    RequestSpec,
    RequirementPriority,
    RequirementSource,
    RequirementSpec,
    ValidationCapability,
)
from .r31_domain import (
    CompatibilityPolicy,
    Criticality,
    ExtractedPlanningRequirement,
    IntentReviewVerdict,
    LifecycleStage,
    MissionPrimary,
    PlanningRunInput,
    PlanningRunStatus,
    ProfileFreshness,
    ProfileSection,
    ProfileSectionName,
    ProjectProfileDefinition,
    RawRequestTrace,
    RequirementExtractionDraft,
    RequirementIntentReview,
    RequirementKind,
    RiskTolerance,
    SearchOutcomeStatus,
)
from .r31_intent import (
    EffectivePlanningPolicyBuilder,
    PlanningMissionResolver,
    ProjectStateSnapshotBuilder,
    RequestSpecAssemblyContext,
    RequirementAnalyzer,
)
from .r31_pipeline import PlanningSearchPipeline
from .r31_prototype import (
    FixtureApproachGenerator,
    FixtureCandidateExpander,
    FixtureCandidateRefiner,
    FixtureHardGateReviewer,
    FixtureTopKWalkthrough,
    fixture_mission_review_evidence,
    fixture_requirement_review_evidence,
)
from .r31_search import SelectedPlanExporter
from .r31_store import PlanningArtifactRepository, PlanningRunService, ProjectProfileStore


SMOKE_PROJECT_ID = "project_31313131313131313131313131313131"
SMOKE_INSTRUCTION = "사용자 요청을 우선하고 Hard Gate 실패 후보는 점수화하지 않는다.\n"
SMOKE_REQUEST = (
    "같은 Mission의 여러 계획 후보를 Hard Gate 후 비교한다. "
    "추천 선택과 Core 계획 활성화를 분리한다. "
    "Core DB schema 변경은 제외한다. "
    "사용자 승인 없는 PlanRevision 활성화는 제외한다."
)


def _profile_definition() -> ProjectProfileDefinition:
    section = ProfileSection(
        source_refs=("smoke-profile",),
        source_digest=sha256_bytes(b"flowmarshal-r31-smoke-profile-v1"),
        freshness=ProfileFreshness.CURRENT,
    )
    return ProjectProfileDefinition(
        product_goal="검증 가능한 목적 기반 다중 후보 계획을 만든다.",
        lifecycle_stage=LifecycleStage.PROTOTYPE,
        criticality=Criticality.STANDARD,
        compatibility_policy=CompatibilityPolicy.PRESERVE,
        default_risk_tolerance=RiskTolerance.BALANCED,
        architecture=section,
        validation=section,
        runtime=section,
        risk=section,
        compatibility=section,
    )


def _request_spec(artifact_root: Path) -> RequestSpec:
    context = ContextSourceSpec.from_text(
        source_id="project-agents",
        kind=ContextSourceKind.PROJECT_INSTRUCTIONS,
        path=str(artifact_root / "AGENTS.md"),
        purpose="모든 WorkItem에 적용되는 프로젝트 지침",
        content=SMOKE_INSTRUCTION,
        required_for_all_work_items=True,
    )
    return RequestSpec(
        project_id=SMOKE_PROJECT_ID,
        project_name="FlowMarshal R3.1 smoke",
        project_root=str(artifact_root),
        project_description="sidecar와 제한형 검색의 결정적 통합 검사",
        user_request=SMOKE_REQUEST,
        request_summary="목적 기반 다중 후보 Planner",
        requirements=(
            RequirementSpec(
                requirement_id="req.r31.search",
                statement="같은 Mission의 여러 계획 후보를 Hard Gate 후 비교한다.",
                source=RequirementSource.USER,
                priority=RequirementPriority.MUST,
            ),
            RequirementSpec(
                requirement_id="req.r31.activation",
                statement="추천 선택과 Core 계획 활성화를 분리한다.",
                source=RequirementSource.USER,
                priority=RequirementPriority.MUST,
            ),
        ),
        context_sources=(context,),
        available_validations=(
            ValidationCapability(
                capability_id="python-unittest",
                check_type="command",
                description="Python unittest 종료 상태를 확인한다.",
                configuration={"command": "python -m unittest discover -s tests"},
            ),
        ),
        out_of_scope=(
            "Core DB schema 변경은 제외한다.",
            "사용자 승인 없는 PlanRevision 활성화는 제외한다.",
        ),
    )


def _ensure_smoke_instruction(root: Path) -> None:
    path = root / "AGENTS.md"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(SMOKE_INSTRUCTION)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_text(encoding="utf-8") != SMOKE_INSTRUCTION:
            raise RuntimeError("smoke artifact root의 AGENTS.md가 예상 계약과 다릅니다.")


def _write_immutable_text(path: Path, content: str) -> None:
    """같은 실행을 재개할 때 동일 내용만 허용하는 최상위 증거를 쓴다."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_text(encoding="utf-8") != content:
            raise RuntimeError(f"기존 R3.1 smoke 증거와 현재 결과가 다릅니다: {path}")


def _render_smoke_report(result: dict[str, Any]) -> str:
    top_k = ", ".join(f"`{item}`" for item in result["top_k"])
    return (
        "# FlowMarshal Planner R3.1 결정적 smoke 보고서\n\n"
        f"- 판정: **{result['status']}**\n"
        f"- 실행 모드: `{result['mode']}`\n"
        f"- 실제 모델 forward qualification: **{result['live_model_qualification']}**\n"
        f"- PlanningRun: `{result['planning_run_id']}`\n"
        f"- Mission: `{result['mission']}`\n"
        f"- 후보 version 수: {result['candidate_versions']}\n"
        f"- Diversity Top-K: {top_k}\n"
        f"- 추천 후보: `{result['selected_candidate_id']}`\n"
        f"- Core 계획 활성화: `{str(result['core_activated']).lower()}`\n\n"
        "## 결론\n\n"
        "R3의 `RequestSpec`과 `PlanDraft`를 유지한 채 R3.1 sidecar가 같은 Mission의 "
        "후보를 생성하고, Hard Gate 통과 후보만 비교·정제한 뒤 완전한 계획 하나를 "
        "export했다. 이 smoke는 Core를 활성화하지 않는다.\n\n"
        "이 결과는 결정적 fixture 통합 검사의 증거이며 실제 모델 품질 검증을 "
        "대체하지 않는다. R3.1 `GO`에는 별도의 live forward qualification이 필요하다.\n\n"
        "## 결속 digest\n\n"
        f"- planning input: `{result['planning_input_digest']}`\n"
        f"- selection: `{result['selection_digest']}`\n"
        f"- exported PlanDraft: `{result['exported_plan_digest']}`\n"
        f"- export 경로: `{result['export_path']}`\n"
    )


def _trace(raw_request: str, trace_id: str, excerpt: str) -> RawRequestTrace:
    start = raw_request.index(excerpt)
    return RawRequestTrace(
        trace_id=trace_id,
        start_offset=start,
        end_offset=start + len(excerpt),
        excerpt=excerpt,
    )


def _reviewed_requirement_assembly(request, profile, mission_selection, snapshot):
    mission = mission_selection.mission
    assert mission is not None
    analyzer = RequirementAnalyzer()
    context = analyzer.context(
        request.user_request,
        mission_selection,
        profile,
        (ProfileSectionName.ARCHITECTURE, ProfileSectionName.COMPATIBILITY),
        snapshot,
    )
    traces = (
        _trace(
            request.user_request,
            "requirement_search",
            "같은 Mission의 여러 계획 후보를 Hard Gate 후 비교한다.",
        ),
        _trace(
            request.user_request,
            "requirement_activation",
            "추천 선택과 Core 계획 활성화를 분리한다.",
        ),
        _trace(
            request.user_request,
            "exclusion_core_schema",
            "Core DB schema 변경은 제외한다.",
        ),
        _trace(
            request.user_request,
            "exclusion_unapproved_activation",
            "사용자 승인 없는 PlanRevision 활성화는 제외한다.",
        ),
    )
    draft = RequirementExtractionDraft(
        raw_request_digest=context.raw_request_digest,
        mission_digest=mission.mission_digest,
        profile_definition_digest=profile.definition_digest,
        project_snapshot_digest=snapshot.snapshot_digest,
        request_summary=request.request_summary,
        traces=traces,
        requirements=(
            ExtractedPlanningRequirement(
                requirement_id="req.r31.search",
                kind=RequirementKind.REQUIREMENT,
                statement="같은 Mission의 여러 계획 후보를 Hard Gate 후 비교한다.",
                mandatory=True,
                trace_refs=("requirement_search",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="req.r31.activation",
                kind=RequirementKind.REQUIREMENT,
                statement="추천 선택과 Core 계획 활성화를 분리한다.",
                mandatory=True,
                trace_refs=("requirement_activation",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="exclude.core_schema",
                kind=RequirementKind.EXCLUSION,
                statement="Core DB schema 변경은 제외한다.",
                mandatory=True,
                trace_refs=("exclusion_core_schema",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="exclude.unapproved_activation",
                kind=RequirementKind.EXCLUSION,
                statement="사용자 승인 없는 PlanRevision 활성화는 제외한다.",
                mandatory=True,
                trace_refs=("exclusion_unapproved_activation",),
            ),
            ExtractedPlanningRequirement(
                requirement_id="mission.outcome",
                kind=RequirementKind.OBSERVABLE_OUTCOME,
                statement=mission.observable_outcome,
                mandatory=True,
                mission_grounded=True,
            ),
        ),
        unresolved_items=snapshot.unknowns,
    )
    analyzer.analyze(context, draft)
    review = RequirementIntentReview(
        analysis_context_digest=context.context_digest,
        extraction_draft_digest=draft.draft_digest,
        verdict=IntentReviewVerdict.PASS,
    )
    assembly = analyzer.assemble(
        context,
        draft,
        RequestSpecAssemblyContext(
            project_id=request.project_id,
            project_name=request.project_name,
            project_root=request.project_root,
            project_description=request.project_description,
            project_instruction_path=str(Path(request.project_root) / "AGENTS.md"),
            available_validations=request.available_validations,
            product_capabilities=request.product_capabilities,
            planning_limits=request.planning_limits,
        ),
        intent_review=review,
    )
    return assembly, fixture_requirement_review_evidence(draft, review)


def run_smoke(artifact_root: str | Path) -> dict[str, Any]:
    root = Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _ensure_smoke_instruction(root)
    profile_store = ProjectProfileStore(root)
    active = profile_store.get_active(SMOKE_PROJECT_ID)
    definition = _profile_definition()
    if active is None:
        revision = profile_store.create_revision(SMOKE_PROJECT_ID, None, definition)
        active = profile_store.activate_revision(
            revision.profile_revision_id,
            revision.definition_digest,
        )
    elif active.definition_digest != definition.definition_digest:
        revision = profile_store.create_revision(
            SMOKE_PROJECT_ID,
            active.definition_digest,
            definition,
        )
        active = profile_store.activate_revision(
            revision.profile_revision_id,
            revision.definition_digest,
        )

    request = _request_spec(root)
    mission = PlanningMissionResolver().resolve(
        request,
        active,
        user_selection=MissionPrimary.FEATURE_EXTENSION,
    )
    snapshot = ProjectStateSnapshotBuilder().capture(request, mission)
    assembly, requirement_review_evidence = _reviewed_requirement_assembly(
        request,
        active,
        mission,
        snapshot,
    )
    request = assembly.request_spec
    mission = assembly.mission_selection
    snapshot = assembly.project_snapshot
    extraction = assembly.extraction_receipt
    policy = EffectivePlanningPolicyBuilder().build(active, mission)
    run_input = PlanningRunInput(
        request_spec=request,
        profile_revision=active,
        mission_selection=mission,
        mission_review_evidence=fixture_mission_review_evidence(mission),
        effective_policy=policy,
        requirement_extraction=extraction,
        requirement_review_evidence=requirement_review_evidence,
        project_snapshot=snapshot,
    )
    runs = PlanningRunService(root)
    idempotency_key = f"r31-smoke:{run_input.planning_input_digest}"
    receipt = runs.freeze(run_input, idempotency_key)
    artifacts = PlanningArtifactRepository(root)
    if receipt.status in {PlanningRunStatus.FROZEN, PlanningRunStatus.SEARCHING}:
        outcome = PlanningSearchPipeline(
            run_service=runs,
            artifact_repository=artifacts,
            approach_generator=FixtureApproachGenerator(),
            candidate_expander=FixtureCandidateExpander(),
            hard_gate_reviewer=FixtureHardGateReviewer(),
            top_k_walkthrough=FixtureTopKWalkthrough(),
            candidate_refiner=FixtureCandidateRefiner(),
        ).search(receipt)
    elif receipt.status is PlanningRunStatus.READY_FOR_REVIEW:
        outcome = artifacts.load_search_outcome(receipt.run_id)
    else:
        raise RuntimeError(
            f"기존 smoke PlanningRun을 안전하게 재사용할 수 없습니다: {receipt.status.value}"
        )
    if outcome.status is not SearchOutcomeStatus.READY_FOR_REVIEW:
        raise RuntimeError(f"R3.1 smoke 검색 실패: {outcome.failure_reasons}")
    exported = SelectedPlanExporter().export(
        outcome,
        outcome.selection_receipt.selection_digest,
    )
    export_path = artifacts.save_selected_plan(
        outcome.run_id,
        exported.selection_receipt_digest,
        exported.plan_draft,
    )
    result = {
        "receipt_schema": "flowmarshal.planner-r31.smoke.v1",
        "status": "PASS",
        "mode": "deterministic_fixture",
        "live_model_qualification": "NOT_RUN",
        "planning_run_id": outcome.run_id,
        "planning_input_digest": outcome.planning_input_digest,
        "mission": outcome.mission_primary.value,
        "candidate_versions": len(outcome.candidates),
        "top_k": list(outcome.top_k_candidate_ids),
        "selected_candidate_id": outcome.selection_receipt.selected_candidate_id,
        "selection_digest": outcome.selection_receipt.selection_digest,
        "exported_plan_digest": exported.plan_draft.canonical_digest,
        "export_path": str(export_path),
        "core_activated": False,
    }
    receipt_path = root / "r31-smoke-receipt.json"
    report_path = root / "r31-smoke-report.md"
    _write_immutable_text(receipt_path, canonical_json(result) + "\n")
    _write_immutable_text(report_path, _render_smoke_report(result))
    return {
        **result,
        "receipt_path": str(receipt_path),
        "report_path": str(report_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FlowMarshal Planner R3.1 결정적 sidecar smoke를 실행합니다."
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("FLOWMARSHAL_PLANNING_ARTIFACT_ROOT"),
        help="planning artifact root. 환경변수 FLOWMARSHAL_PLANNING_ARTIFACT_ROOT도 사용 가능합니다.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    arguments = parser.parse_args()
    if not arguments.artifact_root:
        parser.error("--artifact-root 또는 FLOWMARSHAL_PLANNING_ARTIFACT_ROOT가 필요합니다.")
    result = run_smoke(arguments.artifact_root)
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
