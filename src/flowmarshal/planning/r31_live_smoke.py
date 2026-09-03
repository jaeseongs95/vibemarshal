from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from ..canonical import canonical_json, sha256_digest
from .domain import ContextSourceKind, ContextSourceSpec
from .input import ContextFileRegistration
from .r31_domain import (
    ModelCallReceipt,
    PlanningRole,
    PlanningRunInput,
    PlanningRunStatus,
    ProfileSectionName,
    SearchOutcomeStatus,
)
from .r31_intent import (
    EffectivePlanningPolicyBuilder,
    ProjectStateSnapshotBuilder,
    RequestSpecAssemblyContext,
    RequirementAnalyzer,
)
from .r31_models import (
    ExecutionPolicyEvidence,
    ModelBoundaryModel,
    ModelRolePreference,
    PolicyVerifiedCodex,
)
from .r31_runtime import (
    PlanningRoleInstructions,
    build_planning_runtime,
)
from .r31_search import SelectedPlanExporter
from .r31_smoke import (
    SMOKE_PROJECT_ID,
    _ensure_smoke_instruction,
    _profile_definition,
    _request_spec,
    _write_immutable_text,
)
from .r31_store import PlanningArtifactRepository, PlanningRunService, ProjectProfileStore


LIVE_SMOKE_SCHEMA = "flowmarshal.planner-r31.live-smoke.v1"
LIVE_PROJECT_INVENTORY = """# FlowMarshal R3.1 구현 표면 inventory

- `src/flowmarshal/planning/r31_domain.py`: Mission, candidate, quality report, selection과 session hint 계약.
- `src/flowmarshal/planning/r31_role_adapters.py`: structured model 역할 adapter와 독립 review 경계.
- `src/flowmarshal/planning/r31_pipeline.py`: Generate → deterministic validation → Hard Gate → Top-K → refine 상태 흐름.
- `src/flowmarshal/planning/r31_search.py`: balanced-mvp-v0 점수 계산, diversity dedupe와 추천 선택.
- `src/flowmarshal/planning/r31_store.py`: immutable JSON artifact, idempotency key, 최신 outcome pointer 저장.
- `tests/test_planner_r31_*.py`: domain, pipeline, search, role adapter와 smoke 회귀 테스트.

구현 계약:
- R3 Core DB schema와 Core 활성화 경계는 변경하지 않는다.
- `PlanningSearchPipeline.search`는 candidate artifact와 model-call receipt를 저장하고, 추천은 하되 Core PlanRevision을 활성화하지 않는다.
- immutable artifact 기록은 동일 digest 재사용, 충돌 거부, 부분 기록 후 receipt/outcome 재대조가 가능해야 한다.
- WorkItem assignment는 후속 Assigner 책임이므로 Planner 후보에서는 `null`을 유지한다.
- 실행 가능한 검증 capability는 `python-unittest`; 명령은 `.venv\\Scripts\\python.exe -m unittest discover -s tests -q`다.
- 계획 단계 validation의 runtime 성공을 주장하지 않고 command, 입력, assertion, 기대 결과와 evidence 경로만 정의한다.
"""
_RUNTIME_ROLES = frozenset(
    {
        PlanningRole.PURPOSE_RESOLVER,
        PlanningRole.INTENT_REVIEWER,
        PlanningRole.CANDIDATE_GENERATOR,
        PlanningRole.HARD_GATE_REVIEWER,
        PlanningRole.CRITICAL_REVIEWER,
        PlanningRole.SCORER_SELECTOR,
    }
)


class LiveRoleConfiguration(ModelBoundaryModel):
    configuration_id: str = Field(min_length=1, max_length=200)
    preferences: tuple[ModelRolePreference, ...] = Field(min_length=1)

    @field_validator("preferences")
    @classmethod
    def roles_are_exactly_supported(
        cls,
        value: tuple[ModelRolePreference, ...],
    ) -> tuple[ModelRolePreference, ...]:
        roles = [item.role for item in value]
        if len(roles) != len(set(roles)):
            raise ValueError("live smoke 역할 설정이 중복됐습니다.")
        missing = _RUNTIME_ROLES - set(roles)
        unexpected = set(roles) - _RUNTIME_ROLES
        if missing or unexpected:
            raise ValueError(
                "live smoke 역할 설정 집합이 다릅니다: "
                f"missing={sorted(item.value for item in missing)}, "
                f"unexpected={sorted(item.value for item in unexpected)}"
            )
        return value

    @property
    def configuration_digest(self) -> str:
        return sha256_digest(self)


def load_role_configuration(path: str | Path) -> LiveRoleConfiguration:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return LiveRoleConfiguration.model_validate(payload)


def _read_required(path: Path) -> str:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Planner 스킬 참조가 파일이 아닙니다: {resolved}")
    return resolved.read_text(encoding="utf-8")


def load_role_instructions(skill_root: str | Path) -> PlanningRoleInstructions:
    root = Path(skill_root).resolve(strict=True)
    common = _read_required(root / "SKILL.md")
    intent = _read_required(root / "references" / "intent-contract.md")
    plan = _read_required(root / "references" / "plan-contract.md")
    gates = _read_required(root / "references" / "quality-gates.md")
    selection = _read_required(root / "references" / "candidate-selection.md")
    session = _read_required(root / "references" / "session-strategy.md")
    preamble = (
        "FlowMarshal R3.1의 비권위 planning 역할이다. 제공된 JSON artifact만 사용하고 "
        "파일·shell·network·Core·계획 활성화·dispatch 기능을 사용하지 않는다. "
        "계획 단계의 validation은 실행됐다고 주장하지 않는다. 출력은 요청된 JSON "
        "Schema만 충족한다.\n\n"
    )

    def combine(role: str, *references: str) -> str:
        return preamble + f"현재 역할: {role}\n\n" + common + "\n\n" + "\n\n".join(references)

    return PlanningRoleInstructions(
        purpose_resolver=combine("purpose_resolver", intent),
        intent_reviewer=combine("intent_reviewer", intent, gates),
        candidate_generator=combine(
            "candidate_generator",
            plan,
            gates,
            selection,
            session,
        ),
        hard_gate_reviewer=combine("hard_gate_reviewer", gates, plan, session),
        critical_reviewer=combine("critical_reviewer", gates, plan, session),
        scorer_selector=combine("scorer_selector", selection, gates, plan, session),
    )


def _active_profile(root: Path):
    store = ProjectProfileStore(root)
    active = store.get_active(SMOKE_PROJECT_ID)
    definition = _profile_definition()
    if active is None:
        revision = store.create_revision(SMOKE_PROJECT_ID, None, definition)
        return store.activate_revision(
            revision.profile_revision_id,
            revision.definition_digest,
        )
    if active.definition_digest != definition.definition_digest:
        revision = store.create_revision(
            SMOKE_PROJECT_ID,
            active.definition_digest,
            definition,
        )
        return store.activate_revision(
            revision.profile_revision_id,
            revision.definition_digest,
        )
    return active


def _live_request_spec(artifact_root: Path):
    source_root = Path(__file__).resolve().parents[3]
    project_agents = (source_root / "AGENTS.md").resolve(strict=True)
    inventory_path = artifact_root / "flowmarshal-r31-inventory.md"
    _write_immutable_text(inventory_path, LIVE_PROJECT_INVENTORY)
    base = _request_spec(artifact_root)
    return base.model_copy(
        update={
            "project_root": str(source_root),
            "project_description": (
                "FlowMarshal R3.1 sidecar Planner의 실제 구현 표면을 대상으로 한 "
                "읽기 전용 forward smoke"
            ),
            "context_sources": (
                ContextSourceSpec.from_text(
                    source_id="project-agents",
                    kind=ContextSourceKind.PROJECT_INSTRUCTIONS,
                    path=str(project_agents),
                    purpose="FlowMarshal 저장소의 프로젝트 지침",
                    content=project_agents.read_text(encoding="utf-8"),
                    required_for_all_work_items=True,
                ),
                ContextSourceSpec.from_text(
                    source_id="r31-project-inventory",
                    kind=ContextSourceKind.REFERENCE,
                    path=str(inventory_path),
                    purpose="R3.1 구현 모듈, 저장 경계와 테스트 진입점 inventory",
                    content=LIVE_PROJECT_INVENTORY,
                    required_for_all_work_items=True,
                ),
            ),
            "product_capabilities": (
                "immutable planning artifact persistence",
                "deterministic PlanDraft validation",
                "structured independent model review",
                "Python unittest regression suite",
            ),
        }
    )


def _unique_receipts(*groups: tuple[ModelCallReceipt, ...]) -> tuple[ModelCallReceipt, ...]:
    by_id: dict[str, ModelCallReceipt] = {}
    for group in groups:
        for receipt in group:
            existing = by_id.get(receipt.call_id)
            if existing is not None and existing != receipt:
                raise RuntimeError(f"같은 model call ID에 다른 receipt가 있습니다: {receipt.call_id}")
            by_id[receipt.call_id] = receipt
    return tuple(by_id[key] for key in sorted(by_id))


def _receipt_token_count(receipt: ModelCallReceipt) -> int:
    if receipt.token_count is not None:
        return receipt.token_count
    metrics = {item.name: item.value for item in receipt.usage}
    for name in (
        "total.totalTokens",
        "total.total_tokens",
        "total.total_token_count",
    ):
        if name in metrics:
            return metrics[name]
    for input_name, output_name in (
        ("total.inputTokens", "total.outputTokens"),
        ("total.input_tokens", "total.output_tokens"),
    ):
        if input_name in metrics or output_name in metrics:
            return metrics.get(input_name, 0) + metrics.get(output_name, 0)
    return 0


def _receipt_latency(receipt: ModelCallReceipt) -> int:
    return receipt.latency_ms or 0


def _render_report(result: dict[str, Any]) -> str:
    models = "\n".join(
        f"- `{item['role']}`: `{item['model_id']}` / `{item['reasoning_effort']}`"
        for item in result["resolved_models"]
    )
    return (
        "# FlowMarshal Planner R3.1 실제 모델 smoke 보고서\n\n"
        f"- 판정: **{result['status']}**\n"
        f"- qualification 범위: **{result['qualification_scope']}**\n"
        f"- 역할 설정: `{result['configuration_id']}`\n"
        f"- 역할 설정 digest: `{result['configuration_digest']}`\n"
        f"- model inventory digest: `{result['inventory_digest']}`\n"
        f"- 실제 model call: {result['model_call_count']}회\n"
        f"- schema recovery: {result['schema_recovery_count']}회\n"
        f"- 합계 token: {result['total_tokens']}\n"
        f"- 합계 model latency: {result['total_model_latency_ms']} ms\n"
        f"- 후보 version: {result['candidate_versions']}개\n"
        f"- 추천 후보: `{result['selected_candidate_id']}`\n"
        f"- Core 계획 활성화: `{str(result['core_activated']).lower()}`\n\n"
        "## 실제 역할 배정\n\n"
        f"{models}\n\n"
        "## 판정 범위\n\n"
        "이 실행은 실제 모델로 Mission 제안·독립 intent review·요구 추출·독립 review·"
        "후보 생성·Hard Gate·Top-K walkthrough·정제·단일 PlanDraft export를 관통한다. "
        "Core 활성화나 WorkItem 실행은 하지 않는다.\n\n"
        "단일 시나리오 smoke이므로 전체 50개 fixture, 순서 변형과 독립 복원 평가를 "
        "대체하지 않는다. 이 결과만으로 R3.1을 GO로 선언하지 않는다.\n\n"
        "## 결속 digest\n\n"
        f"- planning input: `{result['planning_input_digest']}`\n"
        f"- selection: `{result['selection_digest']}`\n"
        f"- exported PlanDraft: `{result['exported_plan_digest']}`\n"
        f"- export 경로: `{result['export_path']}`\n"
    )


def run_live_smoke(
    artifact_root: str | Path,
    *,
    skill_root: str | Path,
    configuration: LiveRoleConfiguration,
    client_factory,
    policy_evidence: list[ExecutionPolicyEvidence],
) -> dict[str, Any]:
    root = Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _ensure_smoke_instruction(root)
    active = _active_profile(root)
    request = _live_request_spec(root)
    run_service = PlanningRunService(root)
    artifact_repository = PlanningArtifactRepository(root)
    runtime = build_planning_runtime(
        client_factory=client_factory,
        run_service=run_service,
        artifact_repository=artifact_repository,
        cwd=root,
        instructions=load_role_instructions(skill_root),
        preferences=configuration.preferences,
    )
    reviewed_mission = runtime.mission_service.resolve(
        request,
        active,
        user_selection="feature_extension",
    )
    mission = reviewed_mission.mission_selection
    if mission.mission is None:
        raise RuntimeError(f"실제 모델 Mission smoke가 확정되지 않았습니다: {mission.status.value}")
    snapshot = ProjectStateSnapshotBuilder().capture(request, mission)
    analyzer = RequirementAnalyzer()
    context = analyzer.context(
        request.user_request,
        mission,
        active,
        (ProfileSectionName.ARCHITECTURE, ProfileSectionName.COMPATIBILITY),
        snapshot,
    )
    reviewed_requirements = runtime.requirement_service.analyze_and_assemble(
        context,
        RequestSpecAssemblyContext(
            project_id=request.project_id,
            project_name=request.project_name,
            project_root=request.project_root,
            project_description=request.project_description,
            project_instruction_path=next(
                item.path
                for item in request.context_sources
                if item.source_id == "project-agents"
            ),
            registered_context_sources=tuple(
                ContextFileRegistration(
                    source_id=item.source_id,
                    kind=item.kind,
                    path=item.path,
                    purpose=item.purpose,
                    required_for_all_work_items=item.required_for_all_work_items,
                )
                for item in request.context_sources
                if item.source_id != "project-agents"
            ),
            available_validations=request.available_validations,
            product_capabilities=request.product_capabilities,
            planning_limits=request.planning_limits,
        ),
    )
    assembly = reviewed_requirements.assembly
    policy = EffectivePlanningPolicyBuilder().build(
        active,
        assembly.mission_selection,
        requirement_extraction=assembly.extraction_receipt,
    )
    run_input = PlanningRunInput(
        request_spec=assembly.request_spec,
        profile_revision=active,
        mission_selection=assembly.mission_selection,
        mission_review_evidence=reviewed_mission.review_evidence,
        effective_policy=policy,
        requirement_extraction=assembly.extraction_receipt,
        requirement_review_evidence=reviewed_requirements.review_evidence,
        project_snapshot=assembly.project_snapshot,
    )
    frozen = run_service.freeze(
        run_input,
        f"r31-live-smoke:{configuration.configuration_digest}:{run_input.planning_input_digest}",
    )
    if frozen.status not in {PlanningRunStatus.FROZEN, PlanningRunStatus.SEARCHING}:
        raise RuntimeError(f"live smoke run을 시작할 수 없습니다: {frozen.status.value}")
    outcome = runtime.search_service.search(frozen)
    if outcome.status is not SearchOutcomeStatus.READY_FOR_REVIEW:
        raise RuntimeError(f"실제 모델 PlanningSearch 실패: {outcome.failure_reasons}")
    if outcome.selection_receipt.selected_candidate_id is None:
        raise RuntimeError("실제 모델 smoke가 export할 추천 후보를 선택하지 못했습니다.")
    exported = SelectedPlanExporter().export(
        outcome,
        outcome.selection_receipt.selection_digest,
    )
    export_path = artifact_repository.save_selected_plan(
        outcome.run_id,
        exported.selection_receipt_digest,
        exported.plan_draft,
    )
    receipts = _unique_receipts(
        reviewed_mission.model_call_receipts,
        reviewed_requirements.model_call_receipts,
        outcome.model_call_receipts,
    )
    inventory_digests = {item.inventory_digest for item in receipts}
    if len(inventory_digests) != 1:
        raise RuntimeError("실제 모델 smoke 중 model inventory digest가 변경됐습니다.")
    thread_ids = [item.thread_id for item in receipts if item.thread_id is not None]
    if len(thread_ids) != len(set(thread_ids)):
        raise RuntimeError("독립 역할 model call이 같은 ephemeral thread를 재사용했습니다.")
    evidence_by_thread = {
        item.thread_id: item for item in policy_evidence if item.thread_id is not None
    }
    if len(evidence_by_thread) != len(policy_evidence) or set(thread_ids) != set(
        evidence_by_thread
    ):
        raise RuntimeError("model call과 실제 thread 권한 증거가 일대일로 일치하지 않습니다.")
    result = {
        "receipt_schema": LIVE_SMOKE_SCHEMA,
        "status": "PASS",
        "qualification_scope": "PARTIAL_SINGLE_SCENARIO",
        "configuration_id": configuration.configuration_id,
        "configuration_digest": configuration.configuration_digest,
        "verified_execution_policy": {
            "permission_profile": ":danger-full-access",
            "approval_policy": "never",
        },
        "execution_policy_evidence": [
            evidence_by_thread[thread_id].model_dump(mode="json")
            for thread_id in sorted(evidence_by_thread)
        ],
        "resolved_models": [
            item.model_dump(mode="json") for item in runtime.resolved_models
        ],
        "inventory_digest": next(iter(inventory_digests)),
        "model_call_count": len(receipts),
        "schema_recovery_count": sum(item.schema_recovery_attempts for item in receipts),
        "total_tokens": sum(_receipt_token_count(item) for item in receipts),
        "total_model_latency_ms": sum(_receipt_latency(item) for item in receipts),
        "model_call_receipts": [item.model_dump(mode="json") for item in receipts],
        "planning_run_id": outcome.run_id,
        "planning_input_digest": outcome.planning_input_digest,
        "candidate_versions": len(outcome.candidates),
        "selected_candidate_id": outcome.selection_receipt.selected_candidate_id,
        "selection_digest": outcome.selection_receipt.selection_digest,
        "exported_plan_digest": exported.plan_draft.canonical_digest,
        "export_path": str(export_path),
        "core_activated": False,
    }
    receipt_path = root / "r31-live-smoke-receipt.json"
    report_path = root / "r31-live-smoke-report.md"
    _write_immutable_text(receipt_path, canonical_json(result) + "\n")
    _write_immutable_text(report_path, _render_report(result))
    return {
        **result,
        "receipt_path": str(receipt_path),
        "report_path": str(report_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FlowMarshal Planner R3.1 실제 모델 단일 시나리오 smoke"
    )
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--skill-root", required=True)
    parser.add_argument("--role-config", required=True)
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("FLOWMARSHAL_CODEX_BIN"),
        help="검증할 시스템 Codex 실행 파일. FLOWMARSHAL_CODEX_BIN도 사용 가능합니다.",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if not arguments.codex_bin:
        raise SystemExit("--codex-bin 또는 FLOWMARSHAL_CODEX_BIN이 필요합니다.")
    root = Path(arguments.artifact_root).resolve()
    configuration = load_role_configuration(arguments.role_config)
    policy_evidence: list[ExecutionPolicyEvidence] = []

    def client_factory():
        return PolicyVerifiedCodex(
            codex_bin=arguments.codex_bin,
            evidence_sink=policy_evidence.append,
        )

    try:
        result = run_live_smoke(
            root,
            skill_root=arguments.skill_root,
            configuration=configuration,
            client_factory=client_factory,
            policy_evidence=policy_evidence,
        )
    except Exception as exc:
        journal_paths = sorted(root.rglob("model-calls/*.json"))
        failure = {
            "receipt_schema": LIVE_SMOKE_SCHEMA,
            "status": "ERROR",
            "qualification_scope": "PARTIAL_SINGLE_SCENARIO",
            "configuration_id": configuration.configuration_id,
            "configuration_digest": configuration.configuration_digest,
            "error_type": type(exc).__name__,
            "error_summary": str(exc),
            "model_call_journal_root": str(root / "model-call-journal"),
            "model_call_journal_count": len(journal_paths),
            "planning_run_receipts": sorted(
                str(path) for path in (root / "runs").glob("run-*/receipt.json")
            ),
        }
        _write_immutable_text(
            root / "r31-live-smoke-failure.json",
            canonical_json(failure) + "\n",
        )
        print(canonical_json(failure))
        return 2
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
