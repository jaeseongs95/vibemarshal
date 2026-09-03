from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..canonical import sha256_digest
from .r31_domain import ModelCallReceipt, ModelCallStatus, PlanningRole
from .r31_intent import PlanningMissionResolver, RequirementAnalyzer
from .r31_models import (
    CodexLike,
    CodexModelInventoryAdapter,
    CodexStructuredRoleRunner,
    ModelResolutionError,
    ModelRolePreference,
    ResolvedPlanningModel,
    resolve_model_role,
)
from .r31_pipeline import PlanningSearchPipeline
from .r31_role_adapters import (
    MissionPlanningService,
    RequirementPlanningService,
    RiskRoutedHardGateReviewer,
    StructuredApproachGenerator,
    StructuredCandidateExpander,
    StructuredCandidateRefiner,
    StructuredHardGateReviewer,
    StructuredIntentReviewer,
    StructuredMissionProposer,
    StructuredRequirementExtractor,
    StructuredRequirementIntentReviewer,
    StructuredTopKWalkthrough,
)
from .r31_session import SessionAdvisor
from .r31_store import PlanningArtifactRepository, PlanningRunService


@dataclass(frozen=True)
class PlanningRoleInstructions:
    purpose_resolver: str
    intent_reviewer: str
    candidate_generator: str
    hard_gate_reviewer: str
    critical_reviewer: str
    scorer_selector: str

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not value.strip():
                raise ValueError(f"{name} 역할 instruction은 비어 있을 수 없습니다.")


@dataclass(frozen=True)
class PlanningRuntimeComposition:
    """실제 model adapter와 deterministic Core-free sidecar를 조립한 runtime."""

    resolved_models: tuple[ResolvedPlanningModel, ...]
    mission_proposer: StructuredMissionProposer
    mission_intent_reviewer: StructuredIntentReviewer
    mission_service: MissionPlanningService
    requirement_service: RequirementPlanningService
    search_service: PlanningSearchPipeline
    session_advisor: SessionAdvisor

    def model_for(self, role: PlanningRole) -> ResolvedPlanningModel:
        for model in self.resolved_models:
            if model.role is role:
                return model
        raise KeyError(role.value)


class PlanningRuntimeResolutionError(RuntimeError):
    def __init__(self, message: str, *, receipt: ModelCallReceipt) -> None:
        super().__init__(message)
        self.receipt = receipt


def _resolve_required_models(
    inventory: tuple[Any, ...],
    preferences: tuple[ModelRolePreference, ...],
) -> tuple[ResolvedPlanningModel, ...]:
    required = {
        PlanningRole.PURPOSE_RESOLVER,
        PlanningRole.INTENT_REVIEWER,
        PlanningRole.CANDIDATE_GENERATOR,
        PlanningRole.HARD_GATE_REVIEWER,
        PlanningRole.CRITICAL_REVIEWER,
        PlanningRole.SCORER_SELECTOR,
    }
    by_role = {preference.role: preference for preference in preferences}
    if len(by_role) != len(preferences):
        raise ValueError("planning role preference가 중복됐습니다.")
    missing_preferences = required - set(by_role)
    if missing_preferences:
        raise ValueError(
            "필수 planning role preference가 없습니다: "
            + ", ".join(sorted(role.value for role in missing_preferences))
        )
    unexpected_preferences = set(by_role) - required
    if unexpected_preferences:
        raise ValueError(
            "runtime이 소비하지 않는 planning role preference가 있습니다: "
            + ", ".join(sorted(role.value for role in unexpected_preferences))
        )
    inventory_digest = sha256_digest(inventory)
    resolved: list[ResolvedPlanningModel] = []
    for role in sorted(required, key=lambda item: item.value):
        preference = by_role[role]
        try:
            resolved.append(resolve_model_role(inventory, preference))
        except ModelResolutionError as exc:
            receipt = ModelCallReceipt(
                call_id=f"model_resolution_{uuid4().hex}",
                role=role,
                model_id="|".join(preference.preferred_model_ids),
                reasoning_effort=preference.preferred_effort,
                inventory_digest=inventory_digest,
                input_digest=sha256_digest(preference),
                output_schema_digest=sha256_digest(
                    ResolvedPlanningModel.model_json_schema()
                ),
                status=ModelCallStatus.REQUIRED_MODEL_UNAVAILABLE,
                error_summary=str(exc),
            )
            raise PlanningRuntimeResolutionError(str(exc), receipt=receipt) from exc
    return tuple(resolved)


def build_planning_runtime(
    *,
    client_factory: Callable[[], CodexLike],
    run_service: PlanningRunService,
    artifact_repository: PlanningArtifactRepository,
    cwd: str | Path,
    instructions: PlanningRoleInstructions,
    preferences: tuple[ModelRolePreference, ...],
    allow_multiple_analysis_methods: bool = False,
) -> PlanningRuntimeComposition:
    """호출 직전 inventory 결속을 사용하는 R3.1 실제 adapter 구성을 만든다.

    이 함수와 생성된 서비스에는 Core 활성화·PlanRevision 승인·dispatch capability가 없다.
    """

    resolved_root = Path(cwd).resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("planning runtime cwd는 디렉터리여야 합니다.")
    inventory = CodexModelInventoryAdapter(client_factory).list_models()
    try:
        models = _resolve_required_models(
            inventory,
            preferences,
        )
    except PlanningRuntimeResolutionError as exc:
        artifact_repository.save_preflight_model_call_receipt(
            exc.receipt.input_digest,
            exc.receipt,
        )
        raise
    by_role = {model.role: model for model in models}
    runner = CodexStructuredRoleRunner(client_factory)

    def persist_model_receipt(receipt: ModelCallReceipt) -> None:
        artifact_repository.save_model_call_journal_receipt(receipt)

    def adapter_kwargs(
        role: PlanningRole,
        instruction: str,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "runner": runner,
            "model": by_role[role],
            "cwd": resolved_root,
            "instructions": instruction,
            "receipt_sink": persist_model_receipt,
        }
        return values

    mission_proposer = StructuredMissionProposer(
        **adapter_kwargs(
            PlanningRole.PURPOSE_RESOLVER,
            instructions.purpose_resolver,
        )
    )
    mission_intent_reviewer = StructuredIntentReviewer(
        **adapter_kwargs(
            PlanningRole.INTENT_REVIEWER,
            instructions.intent_reviewer,
        )
    )
    mission_service = MissionPlanningService(
        resolver=PlanningMissionResolver(),
        proposer=mission_proposer,
        reviewer=mission_intent_reviewer,
    )
    requirement_analyzer = RequirementAnalyzer()
    requirement_service = RequirementPlanningService(
        analyzer=requirement_analyzer,
        extractor=StructuredRequirementExtractor(
            analyzer=requirement_analyzer,
            **adapter_kwargs(
                PlanningRole.PURPOSE_RESOLVER,
                instructions.purpose_resolver,
            )
        ),
        reviewer=StructuredRequirementIntentReviewer(
            **adapter_kwargs(
                PlanningRole.INTENT_REVIEWER,
                instructions.intent_reviewer,
            )
        ),
    )
    general_reviewer = StructuredHardGateReviewer(
        **adapter_kwargs(
            PlanningRole.HARD_GATE_REVIEWER,
            instructions.hard_gate_reviewer,
        )
    )
    critical_reviewer = StructuredHardGateReviewer(
        **adapter_kwargs(
            PlanningRole.CRITICAL_REVIEWER,
            instructions.critical_reviewer,
        )
    )
    search_service = PlanningSearchPipeline(
        run_service=run_service,
        artifact_repository=artifact_repository,
        approach_generator=StructuredApproachGenerator(
            **adapter_kwargs(
                PlanningRole.CANDIDATE_GENERATOR,
                instructions.candidate_generator,
            )
        ),
        candidate_expander=StructuredCandidateExpander(
            **adapter_kwargs(
                PlanningRole.CANDIDATE_GENERATOR,
                instructions.candidate_generator,
            )
        ),
        hard_gate_reviewer=RiskRoutedHardGateReviewer(
            general=general_reviewer,
            critical=critical_reviewer,
        ),
        candidate_refiner=StructuredCandidateRefiner(
            **adapter_kwargs(
                PlanningRole.CANDIDATE_GENERATOR,
                instructions.candidate_generator,
            )
        ),
        top_k_walkthrough=StructuredTopKWalkthrough(
            **adapter_kwargs(
                PlanningRole.SCORER_SELECTOR,
                instructions.scorer_selector,
            )
        ),
        allow_multiple_analysis_methods=allow_multiple_analysis_methods,
    )
    return PlanningRuntimeComposition(
        resolved_models=models,
        mission_proposer=mission_proposer,
        mission_intent_reviewer=mission_intent_reviewer,
        mission_service=mission_service,
        requirement_service=requirement_service,
        search_service=search_service,
        session_advisor=SessionAdvisor(),
    )


__all__ = [
    "PlanningRoleInstructions",
    "PlanningRuntimeComposition",
    "PlanningRuntimeResolutionError",
    "build_planning_runtime",
]
