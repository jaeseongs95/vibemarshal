from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..canonical import sha256_bytes, sha256_digest
from .context import ProjectMapper
from .domain import (
    BehaviorPolicy,
    BudgetStage,
    BudgetUsageRecord,
    Criticality,
    ContextSourceRegistration,
    ContextSourceRegistrationKind,
    EffectPolicy,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCriterion,
    GoalOperatingPolicy,
    LifecycleStage,
    MissionClass,
    MutationPolicy,
    PlanContractRevision,
    ProjectMapRevision,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RecoveryAssessment,
    RevisionStatus,
    RuntimeReceipt,
    RuntimeJobStatus,
    RunOnceResult,
    SourceTrace,
    StateFact,
    StateSnapshot,
    TaskExecutionSpecRevision,
    ThreadBinding,
    ValidationResult,
    GoalVerdict,
    ExecutionSpecProposal,
    ValidationExecutionStep,
    ManualValidationObservation,
    ExternalValidationObservation,
    ModelAssignmentContract,
    RoleAssignmentPolicy,
    new_id,
    utc_now,
)
from .ledger import (
    DEFAULT_ARTIFACT_DIRECTORY,
    DEFAULT_DB_NAME,
    EngineLedgerError,
    SQLiteEngineLedger,
)
from .models import AssignmentResolutionError, AssignmentResolver, ModelInventory
from .models import EngineRoleConfiguration, RoleModelBinding
from .goal import (
    GoalNormalizerAdapter,
    GoalPreparationError,
    GoalPreparationPipeline,
    GoalReviewerAdapter,
)
from .planner_roles import (
    PlanExpanderAdapter,
    PlanReviewerAdapter,
    PlannerRoleAdapterError,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter,
)
from .plan_inspection_provider import (
    PLAN_INSPECTION_PROVIDER_V1,
    PLAN_INSPECTION_PROVIDER_V2,
)
from .planning import PlanningError, PlanningSearchOutcome
from .planning import SkeletonFirstPlanner
from .planning_recovery import PlanningRecoveryPolicy
from .providers import add_provider_arguments, open_runtime, selection_from_arguments
from .runtime import (
    CodexAppServerRuntime, CodexProjectBinding, CodexRuntimePort, EngineDispatcher,
    RuntimePolicyError,
)
from .validation_execution import GoalValidationRetryRequest
from .roles import CodexStructuredRoleRunner, RoleCallReceipt, StructuredRoleError
from .reporting import render_final
from .service import EngineService, EngineServiceError
from .budget import BudgetManager, BudgetedRoleRunner, GoalBudgetPolicy, receipt_usage
from .application import EngineApplication, EngineApplicationError
from .governance_gate import GovernanceSettings
from .model_rebinding import ModelRebindRequest, ModelRebindingError
from .role_execution import RoleTimeoutPolicy, use_role_timeout_policy
from .capabilities import require_host_execution


def _json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _emit(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _service(arguments: argparse.Namespace) -> EngineService:
    ledger = SQLiteEngineLedger(arguments.db, artifact_root=arguments.artifacts)
    service = EngineService(ledger)
    service.initialize()
    return service


def _role_configuration(arguments: argparse.Namespace) -> EngineRoleConfiguration | None:
    path = getattr(arguments, "role_config", None)
    if path is None:
        return None
    return EngineRoleConfiguration.model_validate(_json(path))


def _role_override(value: str) -> tuple[str, RoleModelBinding]:
    try:
        role, choice = value.split("=", 1)
        model, effort = choice.rsplit(":", 1)
    except ValueError as error:
        raise EngineServiceError(
            "ROLE_CONFIGURATION_OVERRIDE_INVALID: --role은 ROLE=MODEL:EFFORT 형식이어야 합니다."
        ) from error
    if role not in EngineRoleConfiguration.model_fields:
        raise EngineServiceError(
            f"ROLE_CONFIGURATION_OVERRIDE_INVALID: 알 수 없는 역할입니다: {role}"
        )
    try:
        return role, RoleModelBinding(model=model, effort=effort)
    except ValidationError as error:
        raise EngineServiceError(
            f"ROLE_CONFIGURATION_OVERRIDE_INVALID: {role} binding이 유효하지 않습니다."
        ) from error


def _cmd_config_init(arguments: argparse.Namespace) -> None:
    """명시 입력을 현재 inventory에 대조한 뒤 user-owned 역할 설정을 만든다."""

    output = Path(arguments.output).resolve()
    if output.exists():
        raise EngineServiceError(
            f"ROLE_CONFIGURATION_ALREADY_EXISTS: 기존 파일을 덮어쓰지 않습니다: {output}"
        )
    common = RoleModelBinding(model=arguments.model, effort=arguments.effort)
    bindings = {role: common for role in EngineRoleConfiguration.model_fields}
    seen: set[str] = set()
    for raw_override in arguments.role or ():
        role, binding = _role_override(raw_override)
        if role in seen:
            raise EngineServiceError(
                f"ROLE_CONFIGURATION_OVERRIDE_DUPLICATE: 역할 override가 중복됐습니다: {role}"
            )
        seen.add(role)
        bindings[role] = binding
    configuration = EngineRoleConfiguration.model_validate(bindings)
    with _runtime(arguments) as runtime:
        inventory = runtime.list_models()
        configuration.validate_inventory(inventory)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(configuration.model_dump(mode="json"), ensure_ascii=False, indent=2))
        stream.write("\n")
    _emit(
        {
            "role_config": str(output),
            "configuration_digest": configuration.configuration_digest,
            "inventory_digest": inventory.inventory_digest,
            "inventory_validated": True,
        }
    )


def _application(
    arguments: argparse.Namespace,
    *,
    runtime: CodexRuntimePort | None = None,
) -> EngineApplication:
    return EngineApplication(
        _service(arguments),
        runtime=runtime,
        role_configuration=_role_configuration(arguments),
        inspection_contract=getattr(
            arguments, "inspection_contract", PLAN_INSPECTION_PROVIDER_V1
        ),
        # 실행 Task는 필수 governance gate를 지난다. 플러그인 위치와 model class 대응표는 환경 변수로만 받는다.
        governance=(
            None if runtime is None
            else GovernanceSettings.from_environment(Path(arguments.artifacts) / "governance")
        ),
    )


def _active_profile_digest(service: EngineService, project_id: str) -> str:
    with service.ledger.read() as connection:
        row = connection.execute(
            "SELECT r.definition_digest FROM profile_revisions r JOIN projects p "
            "ON p.active_profile_revision_id = r.id WHERE p.id = ?",
            (project_id,),
        ).fetchone()
    if row is None:
        raise EngineServiceError("active ProjectProfile이 없습니다.")
    return row["definition_digest"]


def _active_profile(service: EngineService, project_id: str) -> ProjectProfileRevision:
    with service.ledger.read() as connection:
        row = connection.execute(
            "SELECT r.payload_json FROM profile_revisions r JOIN projects p "
            "ON p.active_profile_revision_id = r.id WHERE p.id = ?",
            (project_id,),
        ).fetchone()
    if row is None:
        raise EngineServiceError("active ProjectProfile이 없습니다.")
    return ProjectProfileRevision.model_validate_json(row["payload_json"])


def _record_role_usage(
    service: EngineService,
    *,
    project_id: str,
    goal_digest: str,
    stage: BudgetStage,
    receipts: tuple[RoleCallReceipt, ...] | list[RoleCallReceipt],
) -> None:
    for receipt in receipts:
        service.record_budget_usage(receipt_usage(receipt, project_id=project_id,
                                                  goal_digest=goal_digest, stage=stage))


def _cmd_project_init(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    project_id = service.create_project(name=arguments.name, root=arguments.root)
    if arguments.profile_file:
        definition = ProjectProfileDefinition.model_validate(_json(arguments.profile_file))
    else:
        definition = ProjectProfileDefinition(
            product_goal=arguments.product_goal,
            lifecycle_stage=LifecycleStage(arguments.lifecycle),
            criticality=Criticality(arguments.criticality),
            compatibility_policy=arguments.compatibility_policy,
            validation_policy=tuple(arguments.validation or ("프로젝트 결정적 테스트를 통과한다.",)),
            runtime_requirements=tuple(arguments.runtime_requirement or ("local Windows", "Python 3.10+")),
        )
    revision = ProjectProfileRevision(
        profile_revision_id=new_id("profile_revision"),
        project_id=project_id,
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    service.register_profile(revision)
    _emit({"project_id": project_id, "profile_revision_id": revision.profile_revision_id})


def _cmd_project_show(arguments: argparse.Namespace) -> None:
    _emit(_service(arguments).status(arguments.project_id))


def _cmd_project_source_add(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    source = Path(arguments.path).resolve(strict=True)
    if not source.is_file():
        raise EngineServiceError("Context source는 파일이어야 합니다.")
    with service.ledger.read() as connection:
        project = connection.execute(
            "SELECT active_goal_revision_id FROM projects WHERE id = ?",
            (arguments.project_id,),
        ).fetchone()
    if project is None:
        raise EngineServiceError("프로젝트를 찾을 수 없습니다.")
    if project["active_goal_revision_id"] is not None and not arguments.register_only:
        raise EngineServiceError(
            "active Goal이 있으므로 source를 profile에 자동 결속할 수 없습니다. "
            "--register-only 후 새 ProjectProfile·Goal revision을 명시적으로 만드세요."
        )
    registration = ContextSourceRegistration(
        context_source_id=new_id("context_source"),
        project_id=arguments.project_id,
        kind=ContextSourceRegistrationKind(arguments.kind),
        path=str(source),
        content_digest=sha256_bytes(source.read_bytes()),
        registered_at=utc_now(),
    )
    service.register_context_source(registration)
    profile_revision_id = None
    if not arguments.register_only:
        current = service.load_active_profile(arguments.project_id)
        definition = current.definition.model_copy(
            update={
                "context_source_refs": tuple(
                    sorted({*current.definition.context_source_refs, registration.context_source_id})
                )
            }
        )
        revision = ProjectProfileRevision(
            profile_revision_id=new_id("profile_revision"),
            project_id=arguments.project_id,
            revision_no=current.revision_no + 1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_profile_revision_id=current.profile_revision_id,
            created_at=utc_now(),
        )
        service.register_profile(revision)
        profile_revision_id = revision.profile_revision_id
    _emit(
        {
            "context_source": registration,
            "profile_revision_id": profile_revision_id,
            "attached_to_profile": not arguments.register_only,
        }
    )


def _cmd_project_source_list(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    sources = service.list_context_sources(arguments.project_id)
    statuses = []
    for source in sources:
        path = Path(source.path)
        actual = sha256_bytes(path.read_bytes()) if path.is_file() else None
        statuses.append(
            {
                "registration": source,
                "current_digest": actual,
                "valid": actual == source.content_digest,
            }
        )
    _emit(statuses)


def _goal_definition_from_args(
    service: EngineService,
    arguments: argparse.Namespace,
) -> GoalContractDefinition:
    profile_digest = _active_profile_digest(service, arguments.project_id)
    if arguments.file:
        document = _json(arguments.file)
        if "definition" in document:
            document = document["definition"]
        document["project_id"] = arguments.project_id
        document["profile_definition_digest"] = profile_digest
        return GoalContractDefinition.model_validate(document)
    if not arguments.request or not arguments.outcome or not arguments.acceptance:
        raise EngineServiceError("--file 또는 --request/--outcome/--acceptance가 필요합니다.")
    request_digest = sha256_bytes(arguments.request.encode("utf-8"))
    trace = SourceTrace(
        trace_id="trace_user_request",
        source_ref="user-request",
        statement=arguments.request,
        source_digest=request_digest,
    )
    criteria = tuple(
        GoalCriterion(
            criterion_id=f"ac_{index}",
            statement=statement,
            validation_intent=f"{statement}를 실제 evidence로 검증한다.",
            trace_refs=(trace.trace_id,),
        )
        for index, statement in enumerate(arguments.acceptance, start=1)
    )
    return GoalContractDefinition(
        project_id=arguments.project_id,
        source_request=arguments.request,
        source_request_digest=request_digest,
        mission_class=MissionClass(arguments.mission),
        observable_outcome=arguments.outcome,
        hard_acceptance=criteria,
        non_goals=tuple(arguments.non_goal or ()),
        source_traces=(trace,),
        effect_policy=EffectPolicy(
            mutation_policy=MutationPolicy(arguments.mutation_policy),
            behavior_policy=BehaviorPolicy(arguments.behavior_policy),
            allowed_external_effects=tuple(arguments.allowed_external_effect or ()),
            prohibited_effects=tuple(arguments.prohibited_effect or ()),
        ),
        profile_definition_digest=profile_digest,
    )


def _cmd_goal_create(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    if arguments.live:
        goal_id = new_id("goal")
        role_config = _role_configuration(arguments)
        normalizer_model = (
            role_config.normalizer.model if role_config else arguments.normalizer_model
        )
        normalizer_effort = (
            role_config.normalizer.effort if role_config else arguments.normalizer_effort
        )
        reviewer_model = (
            role_config.critical_reviewer.model if role_config else arguments.reviewer_model
        )
        reviewer_effort = (
            role_config.critical_reviewer.effort if role_config else arguments.reviewer_effort
        )
        if not all(
            (
                arguments.request,
                normalizer_model,
                normalizer_effort,
                reviewer_model,
                reviewer_effort,
            )
        ):
            raise EngineServiceError(
                "--live에는 request와 normalizer/reviewer model·effort가 모두 필요합니다."
            )
        profile_revision = _active_profile(service, arguments.project_id)
        with _runtime(arguments) as runtime:
            inventory = runtime.list_models()
            if role_config is not None:
                role_config.validate_inventory(inventory)
            runner = BudgetedRoleRunner(CodexStructuredRoleRunner(runtime, max_schema_recovery_attempts=0, ephemeral_threads=False),
                service, project_id=arguments.project_id, goal_id=goal_id)
            normalizer = GoalNormalizerAdapter(
                runner,
                model=normalizer_model,
                effort=normalizer_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=Path(service.status(arguments.project_id)["project"]["root"]),
            )
            reviewer = GoalReviewerAdapter(
                runner,
                model=reviewer_model,
                effort=reviewer_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=Path(service.status(arguments.project_id)["project"]["root"]),
            )
            outcome = GoalPreparationPipeline(normalizer, reviewer).prepare(
                project_id=arguments.project_id,
                goal_id=goal_id,
                profile=profile_revision,
                source_request=arguments.request,
                observed_facts=service.observe_goal_inputs(arguments.project_id, arguments.request),
            )
        service.register_goal(
            outcome.goal_contract,
            activate=outcome.goal_contract.status is RevisionStatus.READY,
        )
        BudgetManager(service).attach_goal(arguments.project_id, goal_id, outcome.goal_contract.definition_digest)
        _emit(outcome)
        return
    definition = _goal_definition_from_args(service, arguments)
    revision = GoalContractRevision(
        goal_revision_id=new_id("goal_revision"),
        goal_id=new_id("goal"),
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    service.register_goal(revision)
    _emit(
        {
            "goal_id": revision.goal_id,
            "goal_revision_id": revision.goal_revision_id,
            "definition_digest": revision.definition_digest,
        }
    )


def _cmd_goal_revise(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    latest = service.load_latest_goal(arguments.project_id)
    if arguments.goal_id and arguments.goal_id != latest.goal_id:
        raise EngineServiceError("--goal-id가 최신 Goal과 다릅니다.")
    if arguments.live:
        role_config = _role_configuration(arguments)
        normalizer_model = (
            role_config.normalizer.model if role_config else arguments.normalizer_model
        )
        normalizer_effort = (
            role_config.normalizer.effort if role_config else arguments.normalizer_effort
        )
        reviewer_model = (
            role_config.critical_reviewer.model if role_config else arguments.reviewer_model
        )
        reviewer_effort = (
            role_config.critical_reviewer.effort if role_config else arguments.reviewer_effort
        )
        if not all(
            (
                arguments.request,
                normalizer_model,
                normalizer_effort,
                reviewer_model,
                reviewer_effort,
            )
        ):
            raise EngineServiceError(
                "--live에는 request와 normalizer/reviewer model·effort가 모두 필요합니다."
            )
        profile_revision = _active_profile(service, arguments.project_id)
        with _runtime(arguments) as runtime:
            inventory = runtime.list_models()
            if role_config is not None:
                role_config.validate_inventory(inventory)
            runner = BudgetedRoleRunner(CodexStructuredRoleRunner(runtime, max_schema_recovery_attempts=0, ephemeral_threads=False),
                service, project_id=arguments.project_id, goal_id=latest.goal_id)
            normalizer = GoalNormalizerAdapter(
                runner,
                model=normalizer_model,
                effort=normalizer_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=Path(service.status(arguments.project_id)["project"]["root"]),
            )
            reviewer = GoalReviewerAdapter(
                runner,
                model=reviewer_model,
                effort=reviewer_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=Path(service.status(arguments.project_id)["project"]["root"]),
            )
            outcome = GoalPreparationPipeline(normalizer, reviewer).prepare(
                project_id=arguments.project_id,
                profile=profile_revision,
                source_request=arguments.request,
                observed_facts=service.observe_goal_inputs(arguments.project_id, arguments.request),
                goal_id=latest.goal_id,
                revision_no=latest.revision_no + 1,
                supersedes_goal_revision_id=latest.goal_revision_id,
            )
        service.register_goal(
            outcome.goal_contract,
            activate=outcome.goal_contract.status is RevisionStatus.READY,
        )
        BudgetManager(service).attach_goal(arguments.project_id, latest.goal_id, outcome.goal_contract.definition_digest)
        _emit(outcome)
        return
    definition = _goal_definition_from_args(service, arguments)
    revision = GoalContractRevision(
        goal_revision_id=new_id("goal_revision"),
        goal_id=latest.goal_id,
        revision_no=latest.revision_no + 1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        supersedes_goal_revision_id=latest.goal_revision_id,
        created_at=utc_now(),
    )
    service.register_goal(revision)
    _emit(
        {
            "goal_id": revision.goal_id,
            "goal_revision_id": revision.goal_revision_id,
            "definition_digest": revision.definition_digest,
        }
    )


def _cmd_goal_show(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    goal = service.load_latest_goal(arguments.project_id)
    with service.ledger.read() as connection:
        row = connection.execute(
            "SELECT g.status, g.activated_at, p.active_goal_revision_id "
            "FROM goal_revisions g JOIN projects p ON p.id = g.project_id "
            "WHERE g.id = ?",
            (goal.goal_revision_id,),
        ).fetchone()
    if row is None:
        raise EngineServiceError("최신 GoalContract 원장 상태를 찾을 수 없습니다.")
    _emit(
        {
            "goal_contract": goal.model_dump(mode="json"),
            "ledger_status": row["status"],
            "is_active": row["active_goal_revision_id"] == goal.goal_revision_id,
            "activated_at": row["activated_at"],
        }
    )


def _ensure_planning_context(
    service: EngineService,
    project_id: str,
) -> tuple[GoalContractRevision, ProjectMapRevision, StateSnapshot]:
    goal = service.load_active_goal(project_id)
    project_map, state = service.reobserve_project(project_id)
    return goal, project_map, state


def _cmd_plan_search(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    goal, project_map, state = _ensure_planning_context(service, arguments.project_id)
    if arguments.outcome_file:
        outcome = PlanningSearchOutcome.model_validate(_json(arguments.outcome_file))
        if outcome.goal_contract_digest != goal.definition_digest:
            raise EngineServiceError("Planning outcome이 active Goal과 다릅니다.")
        if outcome.state_snapshot_digest != state.snapshot_digest:
            raise EngineServiceError("Planning outcome이 current StateSnapshot과 다릅니다.")
        for evaluation in outcome.skeleton_evaluations:
            service.record_skeleton_evaluation(evaluation)
        for evaluation in outcome.plan_evaluations:
            service.register_plan_evaluation(evaluation)
        service.record_planning_search(outcome)
        _activate_search_if_authorized(service, arguments.project_id, outcome)
        _emit(outcome)
        return
    if arguments.live:
        role_config = _role_configuration(arguments)
        generator_model = (
            role_config.skeleton_generator.model if role_config else arguments.generator_model
        )
        generator_effort = (
            role_config.skeleton_generator.effort if role_config else arguments.generator_effort
        )
        expander_model = (
            role_config.plan_expander.model if role_config else arguments.generator_model
        )
        expander_effort = (
            role_config.plan_expander.effort if role_config else arguments.generator_effort
        )
        reviewer_model = (
            role_config.general_reviewer.model if role_config else arguments.reviewer_model
        )
        reviewer_effort = (
            role_config.general_reviewer.effort if role_config else arguments.reviewer_effort
        )
        critical_model = (
            role_config.critical_reviewer.model if role_config else arguments.reviewer_model
        )
        critical_effort = (
            role_config.critical_reviewer.effort if role_config else arguments.reviewer_effort
        )
        executor_model = role_config.executor.model if role_config else arguments.executor_model
        executor_effort = role_config.executor.effort if role_config else arguments.executor_effort
        validator_model = role_config.validator.model if role_config else arguments.validator_model
        validator_effort = role_config.validator.effort if role_config else arguments.validator_effort
        required = (
            generator_model,
            generator_effort,
            expander_model,
            expander_effort,
            reviewer_model,
            reviewer_effort,
            critical_model,
            critical_effort,
            executor_model,
            executor_effort,
            validator_model,
            validator_effort,
        )
        if not all(required):
            raise EngineServiceError(
                "--live에는 generator/reviewer/executor/validator model·effort가 모두 필요합니다."
            )
        root = Path(service.status(arguments.project_id)["project"]["root"])
        with _runtime(arguments) as runtime:
            inventory = runtime.list_models()
            if role_config is not None:
                role_config.validate_inventory(inventory)
            runner = BudgetedRoleRunner(CodexStructuredRoleRunner(runtime, max_schema_recovery_attempts=0, ephemeral_threads=False),
                service, project_id=arguments.project_id, goal_id=goal.goal_id, goal_digest=goal.definition_digest)
            worker_assignment = ModelAssignmentContract(
                executor=RoleAssignmentPolicy(
                    role="executor",
                    preferred_model=executor_model,
                    preferred_effort=executor_effort,
                ),
                validator=RoleAssignmentPolicy(
                    role="validator",
                    preferred_model=validator_model,
                    preferred_effort=validator_effort,
                ),
                independence_required=True,
            )
            AssignmentResolver().resolve_contract(worker_assignment, inventory)
            assigner = RuleBasedTaskAssigner(
                inspect_assignment=worker_assignment,
                standard_assignment=worker_assignment,
                critical_assignment=worker_assignment,
            )
            generator = SkeletonGeneratorAdapter(
                runner,
                model=generator_model,
                effort=generator_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=root,
            )
            skeleton_reviewer = SkeletonReviewerAdapter(
                runner,
                model=reviewer_model,
                effort=reviewer_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=root,
            )
            expander = PlanExpanderAdapter(
                runner,
                assigner,
                model=expander_model,
                effort=expander_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=root,
                inspection_provider_contract=arguments.inspection_contract,
            )
            plan_reviewer = PlanReviewerAdapter(
                runner,
                model=reviewer_model,
                effort=reviewer_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=root,
                critical_model=critical_model,
                critical_effort=critical_effort,
                inspection_provider_contract=arguments.inspection_contract,
            )
            outcome = SkeletonFirstPlanner(
                generator,
                skeleton_reviewer,
                expander,
                plan_reviewer,
            ).search(
                goal=goal,
                state=state,
                project_map=project_map,
                candidate_count=arguments.candidate_count,
                recovery_policy=(PlanningRecoveryPolicy()
                                 if arguments.inspection_contract == PLAN_INSPECTION_PROVIDER_V2 else None),
            )
        for evaluation in outcome.skeleton_evaluations:
            service.record_skeleton_evaluation(evaluation)
        for evaluation in outcome.plan_evaluations:
            service.register_plan_evaluation(evaluation)
        service.record_planning_search(outcome)
        _activate_search_if_authorized(service, arguments.project_id, outcome)
        _emit(outcome)
        return
    _emit(
        {
            "status": "context_ready",
            "message": (
                "Goal·Project Map·State projection을 고정했습니다. role provider가 만든 "
                "PlanningSearchOutcome을 --outcome-file로 전달하면 Core 검증 후 등록합니다."
            ),
            "goal_contract_digest": goal.definition_digest,
            "project_map_digest": project_map.revision_digest,
            "state_snapshot_digest": state.snapshot_digest,
            "model_input": {
                "goal": goal.model_dump(mode="json"),
                "project_map": project_map.model_dump(mode="json"),
                "state": state.model_dump(mode="json"),
            },
        }
    )


def _cmd_plan_compare(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with service.ledger.read() as connection:
        rows = connection.execute(
            "SELECT p.id AS plan_revision_id, p.plan_id, p.revision_no, p.activation_digest, "
            "p.status, d.fitness_score, d.payload_json AS decision_json "
            "FROM plan_revisions p LEFT JOIN candidate_decisions d "
            "ON d.artifact_kind = 'plan' AND d.artifact_digest = p.activation_digest "
            "WHERE p.project_id = ? ORDER BY p.created_at, p.rowid",
            (arguments.project_id,),
        ).fetchall()
    _emit([dict(row) | {"decision": json.loads(row["decision_json"])} for row in rows])


def _cmd_plan_activate(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    if arguments.project_id:
        activation_id = service.activate_selected_plan(project_id=arguments.project_id)
    elif arguments.digest:
        activation_id = service.activate_plan(plan_revision_id=arguments.plan_revision_id,
                                               activation_digest=arguments.digest, source=arguments.source)
    else:
        activation_id = service.activate_authorized_plan(plan_revision_id=arguments.plan_revision_id)
    _emit({"activation_id": activation_id})


def _cmd_goal_authorize(arguments: argparse.Namespace) -> None:
    policy = _goal_authorization_policy(arguments)
    _emit(
        _application(arguments).authorize(
            arguments.project_id,
            source=arguments.source,
            operating_policy=policy,
        )
    )


def _goal_authorization_policy(arguments: argparse.Namespace) -> GoalOperatingPolicy | None:
    return (
        None
        if arguments.policy_file is None
        else GoalOperatingPolicy.model_validate(_json(arguments.policy_file))
    )


def _activate_search_if_authorized(service: EngineService, project_id: str, outcome: PlanningSearchOutcome) -> None:
    with service.ledger.read() as connection:
        authorized = connection.execute("SELECT id FROM goal_authorizations WHERE project_id = ?", (project_id,)).fetchone()
    if authorized is not None and outcome.selected_activation_digest is not None:
        service.activate_selected_plan(project_id=project_id)


def _cmd_plan_status(arguments: argparse.Namespace) -> None:
    _emit(_service(arguments).status(arguments.project_id))


def _cmd_task_show(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with service.ledger.read() as connection:
        task = connection.execute("SELECT * FROM task_contracts WHERE id = ?", (arguments.task_id,)).fetchone()
        if task is None:
            raise EngineServiceError("Task를 찾을 수 없습니다.")
        specs = connection.execute(
            "SELECT id, revision_no, definition_digest, is_current, payload_json FROM execution_spec_revisions "
            "WHERE task_id = ? ORDER BY revision_no",
            (arguments.task_id,),
        ).fetchall()
    _emit(
        {
            "task": dict(task) | {"contract": json.loads(task["payload_json"])},
            "execution_specs": [dict(row) | {"spec": json.loads(row["payload_json"])} for row in specs],
        }
    )


def _cmd_task_materialize(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    if arguments.spec_file:
        if not arguments.inventory_file:
            raise EngineServiceError("--spec-file 수동 경로에는 --inventory-file이 필요합니다.")
        spec = TaskExecutionSpecRevision.model_validate(_json(arguments.spec_file))
        inventory = ModelInventory.model_validate(_json(arguments.inventory_file))
        service.materialize_execution_spec(spec, inventory=inventory)
    else:
        if not arguments.proposal_file:
            raise EngineServiceError("--spec-file 또는 --proposal-file이 필요합니다.")
        proposal = ExecutionSpecProposal.model_validate(_json(arguments.proposal_file))
        with service.ledger.read() as connection:
            row = connection.execute(
                "SELECT p.root FROM projects p JOIN task_contracts t ON t.project_id = p.id "
                "WHERE t.id = ?",
                (proposal.task_id,),
            ).fetchone()
        if row is None:
            raise EngineServiceError("proposal Task를 찾을 수 없습니다.")
        with _runtime(arguments) as runtime:
            runtime.verify_execution_policy(Path(row["root"]))
            inventory = runtime.list_models()
            spec = service.compile_execution_spec(proposal, inventory=inventory)
    _emit({"task_id": spec.task_id, "execution_spec_digest": spec.definition_digest})


def _runtime(arguments: argparse.Namespace) -> CodexRuntimePort:
    selection = selection_from_arguments(
        arguments, default_state_root=Path(arguments.artifacts) / "claude-threads",
    )
    binding_path = getattr(arguments, "codex_project_binding", None)
    project_binding = (
        None if binding_path is None
        else CodexProjectBinding.model_validate(_json(binding_path))
    )
    if selection.provider == "codex":
        if project_binding is not None:
            return CodexAppServerRuntime(
                codex_bin=selection.codex_bin, project_binding=project_binding,
            )
        return CodexAppServerRuntime(codex_bin=selection.codex_bin)
    return open_runtime(selection, project_binding=project_binding)


def _wait_dispatched_turn(
    service: EngineService, runtime: CodexRuntimePort, attempt_id: str | None
) -> None:
    if attempt_id is None:
        return
    with service.ledger.read() as connection:
        row = connection.execute(
            "SELECT s.payload_json FROM attempts a JOIN execution_spec_revisions s "
            "ON s.task_id = a.task_id AND s.definition_digest = a.execution_spec_digest "
            "WHERE a.id = ?",
            (attempt_id,),
        ).fetchone()
    if row is None:
        raise EngineServiceError("dispatch한 Attempt의 ExecutionSpec이 없습니다.")
    spec = TaskExecutionSpecRevision.model_validate_json(row["payload_json"])
    runtime.wait_for_active_turns(timeout_seconds=spec.definition.timeout_seconds)


def _cmd_run_once(arguments: argparse.Namespace) -> None:
    owner_result = os.environ.get("FLOWMARSHAL_RUNTIME_OWNER_RESULT")
    if owner_result is None:
        _emit(_spawn_run_once_owner(tuple(arguments._raw_argv)))
        return
    _run_once_owned(arguments, Path(owner_result))


def _spawn_run_once_owner(argv: tuple[str, ...]) -> RunOnceResult:
    """별도 process가 SDK와 local typed 후처리 수명을 계속 소유하게 한다."""

    result_path = Path(tempfile.gettempdir()) / f"flowmarshal-owner-{uuid.uuid4().hex}.json"
    environment = os.environ.copy()
    environment["FLOWMARSHAL_RUNTIME_OWNER_RESULT"] = str(result_path)
    command = [sys.executable, "-m", "flowmarshal.engine.cli", *argv]
    popen_kwargs: dict[str, Any] = {
        "cwd": os.getcwd(),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **popen_kwargs)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if result_path.exists():
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            result_path.unlink(missing_ok=True)
            if "owner_error" in payload:
                raise EngineServiceError(payload["owner_error"])
            return RunOnceResult.model_validate(payload)
        if process.poll() is not None:
            break
        time.sleep(0.01)
    raise EngineServiceError(
        "RUNTIME_OWNER_START_FAILED: background owner가 초기 tick 결과를 게시하지 못했습니다. "
        f"result_path={result_path}"
    )


def _publish_owner_result(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _run_once_owned(arguments: argparse.Namespace, result_path: Path) -> None:
    published = False
    proposal = (
        None
        if arguments.proposal_file is None
        else ExecutionSpecProposal.model_validate(_json(arguments.proposal_file))
    )
    runtime = _runtime(arguments)
    application = _application(arguments, runtime=runtime)
    try:
        outcome = application.run_once(
            arguments.project_id,
            proposal=proposal,
            resume=getattr(arguments, "resume", False),
            goal_validation_step=(None if arguments.goal_validation_file is None
                                  else ValidationExecutionStep.model_validate(_json(arguments.goal_validation_file))),
            goal_validation_retry=(
                None if arguments.goal_validation_retry_file is None
                else GoalValidationRetryRequest.model_validate(_json(arguments.goal_validation_retry_file))
            ),
        )
        _publish_owner_result(result_path, outcome.model_dump(mode="json"))
        published = True
        supervisor = application.supervisor
        job_id = outcome.runtime_job_id
        if supervisor is not None and job_id is not None:
            while True:
                job = application.service.load_runtime_job(job_id)
                if job.status in {
                    RuntimeJobStatus.PROVIDER_TERMINAL,
                    RuntimeJobStatus.CONSUMED,
                    RuntimeJobStatus.CANCELLED,
                    RuntimeJobStatus.COLLECTOR_LOST,
                }:
                    break
                if (job.absolute_deadline_at - utc_now()).total_seconds() <= 0:
                    supervisor.request_interrupt(job_id)
                supervisor.tick(job_id, wait_seconds=0.05)
                time.sleep(0.01)
    except BaseException as error:
        if not published:
            _publish_owner_result(
                result_path,
                {"owner_error": f"{type(error).__name__}: {error}"},
            )
        raise
    finally:
        application.close_task_gate()
        if application.supervisor is not None:
            application.supervisor.close()
        else:
            runtime.close()


def _cmd_run_status(arguments: argparse.Namespace) -> None:
    _emit(_application(arguments).status(arguments.project_id))


def _cmd_attempt_show(arguments: argparse.Namespace) -> None:
    _emit(EngineApplication(_service(arguments)).attempt_detail(arguments.attempt_id))


def _cmd_attempt_retry(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    assessment = (
        None
        if arguments.recovery_assessment_file is None
        else RecoveryAssessment.model_validate(_json(arguments.recovery_assessment_file))
    )
    service.retry_task(
        task_id=arguments.task_id,
        new_evidence_ids=tuple(arguments.evidence_id or ()),
        recovery_assessment=assessment,
        failed_validation_result_id=arguments.failed_validation_result_id,
    )
    # 새 실행 Attempt는 run-once가 governance gate를 거친 뒤에만 예약·dispatch한다.
    with service.ledger.read() as connection:
        status = connection.execute(
            "SELECT status FROM task_contracts WHERE id = ?", (arguments.task_id,)
        ).fetchone()["status"]
    _emit({"task_id": arguments.task_id, "status": status,
           "next": "run-once가 governance gate를 거쳐 새 Attempt를 예약·dispatch합니다."})


def _cmd_attempt_observe(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with _runtime(arguments) as runtime:
        observation = EngineDispatcher(service, runtime).observe_attempt(arguments.attempt_id)
    _emit(observation)


def _cmd_attempt_resume(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with _runtime(arguments) as runtime:
        receipt = EngineDispatcher(service, runtime).resume_attempt(arguments.attempt_id)
        _wait_dispatched_turn(service, runtime, arguments.attempt_id)
    _emit(receipt)


def _cmd_attempt_interrupt(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with _runtime(arguments) as runtime:
        receipt = EngineDispatcher(service, runtime).interrupt_attempt(arguments.attempt_id)
    _emit(receipt)


def _cmd_validate_task(arguments: argparse.Namespace) -> None:
    if arguments.complete:
        # Task 완료는 run-once의 governance gate(before_completion)를 거쳐서만 한다.
        raise EngineServiceError(
            "GOVERNANCE_GATE_REQUIRED: validate task --complete는 governance gate를 우회하므로 받지 않습니다. "
            "validation을 기록한 뒤 run-once로 완료하십시오."
        )
    service = _service(arguments)
    result = ValidationResult.model_validate(_json(arguments.result_file))
    service.record_validation(
        project_id=arguments.project_id,
        plan_revision_id=arguments.plan_revision_id,
        result=result,
    )
    _emit({"validation_result_id": result.validation_result_id, "new_ready_task_ids": ()})


def _cmd_validate_goal(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    verdict = GoalVerdict.model_validate(_json(arguments.verdict_file))
    service.record_goal_verdict(
        project_id=arguments.project_id,
        plan_revision_id=arguments.plan_revision_id,
        verdict=verdict,
    )
    _emit({"goal_verdict_id": verdict.goal_verdict_id, "status": verdict.status.value})


def _cmd_validate_observe(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    document = _json(arguments.observation_file)
    observation = (
        ManualValidationObservation.model_validate(document)
        if arguments.kind == "manual"
        else ExternalValidationObservation.model_validate(document)
    )
    result = service.record_typed_validation_observation(
        project_id=arguments.project_id,
        plan_revision_id=arguments.plan_revision_id,
        observation=observation,
    )
    _emit(result)


def _cmd_recover_inspect(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    service.recover_inspect(arguments.project_id)
    _emit(EngineApplication(service).recovery_status(arguments.project_id))


def _cmd_governance_check_plugin(arguments: argparse.Namespace) -> None:
    """플러그인 채택 전 확인: preflight와 적합성 검사를 임시 자원에서 실행한다. 프로젝트·원장 없이 동작한다."""
    from .governance_conformance import first_failure, run_conformance
    from .governance_gate import PLUGIN_ROOT_ENV, GovernanceContractMismatch, GovernanceUnavailable

    root = arguments.plugin_root or os.environ.get(PLUGIN_ROOT_ENV)
    if not root:
        raise ValueError(f"GOVERNANCE_PLUGIN_ROOT_REQUIRED: --plugin-root 또는 {PLUGIN_ROOT_ENV}가 필요합니다.")
    try:
        result = run_conformance(Path(root))
    except (GovernanceContractMismatch, GovernanceUnavailable) as error:
        raise ValueError(str(error)) from error
    _emit(result)
    if first_failure(result) is not None:
        arguments.exit_code = 1


def _cmd_model_status(arguments: argparse.Namespace) -> None:
    application = EngineApplication(_service(arguments))
    if arguments.live:
        with _runtime(arguments) as runtime:
            result = application.model_binding_status(arguments.project_id, inventory=runtime.list_models())
    else:
        result = application.model_binding_status(arguments.project_id)
    _emit(result)


def _cmd_model_rebind(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    request = ModelRebindRequest.model_validate(_json(arguments.request_file))
    with _runtime(arguments) as runtime:
        _emit(service.rebind_model(request=request, inventory=runtime.list_models()))


def _cmd_recover_resume(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    document = _json(arguments.receipt_file)
    binding = ThreadBinding.model_validate(document["binding"]) if document.get("binding") else None
    receipt = service.record_runtime_receipt(
        intent_id=arguments.intent_id,
        provider_operation_id=document["provider_operation_id"],
        response=document["response"],
        binding=binding,
        allow_reconcile_unknown=True,
    )
    _emit(receipt)


def _cmd_recover_abandon(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    service.abandon_unknown_intent(intent_id=arguments.intent_id, rationale=arguments.rationale)
    _emit({"intent_id": arguments.intent_id, "status": "abandoned"})


def _progress_report(service: EngineService, project_id: str) -> dict[str, Any]:
    snapshot = service.status(project_id)
    counts: dict[str, int] = {}
    for task in snapshot["tasks"]:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    snapshot["task_status_counts"] = counts
    return snapshot


def _cmd_report_progress(arguments: argparse.Namespace) -> None:
    _emit(_application(arguments).status(arguments.project_id))


def _cmd_report_final(arguments: argparse.Namespace) -> None:
    report = EngineApplication(_service(arguments)).final_report(arguments.project_id, goal_verdict_id=arguments.goal_verdict_id)
    if arguments.format == "markdown":
        print(render_final(goal=report.goal, plan=report.plan, verdict=report.verdict,
                           usage=report.usage.usage_records, usage_summary=report.usage,
                           execution_summary=report.execution_summary,
                           governance_plugin_tasks=report.governance_plugin_tasks,
                           read_only_verification=report.read_only_verification), end="")
        return
    _emit(report)


def _cmd_budget_set(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    policy = GoalBudgetPolicy.model_validate(_json(arguments.policy_file))
    identifier = BudgetManager(service).configure(arguments.project_id, policy, goal_id=arguments.goal_id or "")
    _emit({"policy_revision_id": identifier, "policy": policy.model_dump(mode="json")})


def _cmd_budget_show(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    try:
        usage = EngineApplication(service).usage_summary(
            arguments.project_id, goal_id=arguments.goal_id
        )
    except EngineApplicationError as error:
        if str(error) not in {"GOAL_NOT_FOUND", "ACTIVE_GOAL_NOT_FOUND"}:
            raise
        usage = None
    status = BudgetManager(service).status(
        arguments.project_id,
        goal_id=arguments.goal_id if usage is None else usage.goal_id,
    )
    _emit({"budget": status.model_dump(mode="json"), "usage": None if usage is None else usage.model_dump(mode="json")})


def _cmd_budget_adjust(arguments: argparse.Namespace) -> None:
    BudgetManager(_service(arguments)).adjust_unknown(call_id=arguments.call_id,
        charge_tokens=arguments.charge_tokens, reason=arguments.reason)
    _emit({"call_id": arguments.call_id, "actual_usage_known": False, "charge_tokens": arguments.charge_tokens})


def _cmd_budget_observe(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with service.ledger.read() as connection:
        call = connection.execute("SELECT receipt_json FROM provider_calls WHERE id=?", (arguments.call_id,)).fetchone()
    if call is None or call["receipt_json"] is None:
        raise EngineServiceError("BUDGET_ROLE_RECEIPT_REQUIRED: 원본 역할 receipt가 없습니다.")
    receipt = RoleCallReceipt.model_validate_json(call["receipt_json"])
    if receipt.thread_id is None:
        raise EngineServiceError("BUDGET_ROLE_RECEIPT_REQUIRED: 원본 역할 thread 결속이 없습니다.")
    with _runtime(arguments) as runtime:
        if len(receipt.turn_ids) != 1:
            raise EngineServiceError("BUDGET_ROLE_TURN_BINDING_REQUIRED: 단일 원본 turn이 필요합니다.")
        observation = runtime.read_stored(thread_id=receipt.thread_id, turn_id=receipt.turn_ids[0])
    BudgetManager(service).observe_role_terminal(arguments.call_id, observation)
    _emit({"call_id": arguments.call_id, "observation": observation})


def _cmd_prepare(arguments: argparse.Namespace) -> None:
    if arguments.role_config is None:
        raise EngineApplicationError("ROLE_CONFIGURATION_REQUIRED")
    with _runtime(arguments) as runtime:
        result = _application(arguments, runtime=runtime).prepare(
            arguments.project_id,
            source_request=arguments.request,
            candidate_count=arguments.candidate_count,
        )
    _emit(result)


def _cmd_revise(arguments: argparse.Namespace) -> None:
    if arguments.role_config is None:
        raise EngineApplicationError("ROLE_CONFIGURATION_REQUIRED")
    with _runtime(arguments) as runtime:
        result = _application(arguments, runtime=runtime).revise(
            arguments.project_id,
            source_request=arguments.request,
            candidate_count=arguments.candidate_count,
        )
    _emit(result)


def _cmd_observe(arguments: argparse.Namespace) -> None:
    with _runtime(arguments) as runtime:
        result = _application(arguments, runtime=runtime).observe(arguments.project_id)
    _emit(result)


def _cmd_pause(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    if service.active_runtime_job(arguments.project_id) is None:
        result = EngineApplication(service).pause(arguments.project_id, reason=arguments.reason)
    else:
        with _runtime(arguments) as runtime:
            result = EngineApplication(service, runtime=runtime).pause(
                arguments.project_id, reason=arguments.reason
            )
    _emit(result)


def _cmd_cancel(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    if service.active_runtime_job(arguments.project_id) is None:
        result = EngineApplication(service).cancel(arguments.project_id, reason=arguments.reason)
    else:
        with _runtime(arguments) as runtime:
            result = EngineApplication(service, runtime=runtime).cancel(
                arguments.project_id, reason=arguments.reason
            )
    _emit(result)


def _add_goal_arguments(parser: argparse.ArgumentParser, *, revise: bool = False) -> None:
    parser.add_argument("--project-id", required=True)
    if revise:
        parser.add_argument("--goal-id")
    parser.add_argument("--file")
    parser.add_argument("--request")
    parser.add_argument("--outcome")
    parser.add_argument("--acceptance", action="append")
    parser.add_argument("--non-goal", action="append")
    parser.add_argument("--mission", choices=[item.value for item in MissionClass], default=MissionClass.FEATURE_EXTENSION.value)
    parser.add_argument("--mutation-policy", choices=[item.value for item in MutationPolicy], default=MutationPolicy.SCOPED_CHANGE.value)
    parser.add_argument("--behavior-policy", choices=[item.value for item in BehaviorPolicy], default=BehaviorPolicy.PRESERVE_PUBLIC_CONTRACTS.value)
    parser.add_argument("--allowed-external-effect", action="append")
    parser.add_argument("--prohibited-effect", action="append")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--codex-bin")
    parser.add_argument("--role-config")
    parser.add_argument("--normalizer-model")
    parser.add_argument("--normalizer-effort")
    parser.add_argument("--reviewer-model")
    parser.add_argument("--reviewer-effort")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowmarshal-engine", description="FlowMarshal 새 authority engine")
    parser.add_argument("--role-timeout-policy", help="역할별 timeout 운영 설정 JSON")
    parser.add_argument("--codex-project-binding", help="새 역할·Worker thread의 App Server 프로젝트 결속 JSON")
    parser.add_argument("--db", default=str(Path.cwd() / ".flowmarshal-engine" / DEFAULT_DB_NAME))
    parser.add_argument("--artifacts", default=str(Path.cwd() / ".flowmarshal-engine" / DEFAULT_ARTIFACT_DIRECTORY))
    add_provider_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser(
        "config", help="user-owned Engine 설정 생성"
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_init = config_commands.add_parser(
        "init",
        help="명시한 model/effort를 현재 model inventory에 대조하고 역할 설정 생성",
    )
    config_init.add_argument("--output", required=True)
    config_init.add_argument("--model", required=True)
    config_init.add_argument(
        "--effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        required=True,
    )
    config_init.add_argument(
        "--role",
        action="append",
        metavar="ROLE=MODEL:EFFORT",
        help="특정 역할만 다른 binding으로 설정합니다. 역할별로 한 번씩 지정할 수 있습니다.",
    )
    config_init.add_argument("--codex-bin")
    config_init.set_defaults(handler=_cmd_config_init)

    prepare = commands.add_parser(
        "prepare",
        help="raw request를 실제 Goal 정규화·독립 review·Planning으로 준비",
    )
    prepare.add_argument("--project-id", required=True)
    prepare.add_argument("--request", required=True)
    prepare.add_argument("--role-config", required=True)
    prepare.add_argument("--codex-bin")
    prepare.add_argument("--candidate-count", type=int, choices=(1, 2, 3))
    prepare.add_argument(
        "--inspection-contract",
        choices=(PLAN_INSPECTION_PROVIDER_V1, PLAN_INSPECTION_PROVIDER_V2),
        default=PLAN_INSPECTION_PROVIDER_V1,
    )
    prepare.set_defaults(handler=_cmd_prepare)

    revise_facade = commands.add_parser(
        "revise",
        help="최신 Goal의 다음 revision을 실제 정규화·독립 review·Planning으로 준비(활성화는 authorize)",
    )
    revise_facade.add_argument("--project-id", required=True)
    revise_facade.add_argument("--request", required=True)
    revise_facade.add_argument("--role-config", required=True)
    revise_facade.add_argument("--codex-bin")
    revise_facade.add_argument("--candidate-count", type=int, choices=(1, 2, 3))
    revise_facade.add_argument(
        "--inspection-contract",
        choices=(PLAN_INSPECTION_PROVIDER_V1, PLAN_INSPECTION_PROVIDER_V2),
        default=PLAN_INSPECTION_PROVIDER_V1,
    )
    revise_facade.set_defaults(handler=_cmd_revise)

    authorize_facade = commands.add_parser(
        "authorize", help="목표·범위·효과·운영 정책을 승인하고 선택 Plan을 활성화"
    )
    authorize_facade.add_argument("--project-id", required=True)
    authorize_facade.add_argument("--source", default="cli-user")
    authorize_facade.add_argument("--policy-file")
    authorize_facade.set_defaults(
        handler=_cmd_goal_authorize,
        trusted_action="goal.authorize",
    )

    run_once_facade = commands.add_parser("run-once", help="bounded scheduler tick 한 번 실행")
    run_once_facade.add_argument("--project-id", required=True)
    run_once_facade.add_argument("--codex-bin")
    run_once_facade.add_argument("--proposal-file")
    run_once_facade.add_argument("--role-config")
    run_once_facade.add_argument("--resume", action="store_true")
    facade_goal_validation = run_once_facade.add_mutually_exclusive_group()
    facade_goal_validation.add_argument("--goal-validation-file")
    facade_goal_validation.add_argument("--goal-validation-retry-file")
    run_once_facade.set_defaults(handler=_cmd_run_once)

    observe_facade = commands.add_parser("observe", help="active provider job을 한 번 관측")
    observe_facade.add_argument("--project-id", required=True)
    observe_facade.add_argument("--codex-bin")
    observe_facade.set_defaults(handler=_cmd_observe)

    pause_facade = commands.add_parser("pause", help="현재 Goal workflow 일시정지")
    pause_facade.add_argument("--project-id", required=True)
    pause_facade.add_argument("--reason", default="사용자 요청")
    pause_facade.add_argument("--codex-bin")
    pause_facade.set_defaults(handler=_cmd_pause)

    cancel_facade = commands.add_parser("cancel", help="현재 Goal workflow 취소")
    cancel_facade.add_argument("--project-id", required=True)
    cancel_facade.add_argument("--reason", default="사용자 요청")
    cancel_facade.add_argument("--codex-bin")
    cancel_facade.set_defaults(handler=_cmd_cancel)

    status_facade = commands.add_parser("status", help="Core·control·job 상태 표시")
    status_facade.add_argument("--project-id", required=True)
    status_facade.set_defaults(handler=_cmd_run_status)

    final_report_facade = commands.add_parser(
        "final-report", help="GoalVerdict에 결속된 최종 보고 표시"
    )
    final_report_facade.add_argument("--project-id", required=True)
    final_report_facade.add_argument("--format", choices=("json", "markdown"), default="json")
    final_report_facade.add_argument("--goal-verdict-id")
    final_report_facade.set_defaults(handler=_cmd_report_final)

    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    init = project_commands.add_parser("init")
    init.add_argument("--name", required=True)
    init.add_argument("--root", required=True)
    init.add_argument("--profile-file")
    init.add_argument("--product-goal", default="검증 가능한 로컬 workflow orchestration")
    init.add_argument("--lifecycle", choices=[item.value for item in LifecycleStage], default="development")
    init.add_argument("--criticality", choices=[item.value for item in Criticality], default="medium")
    init.add_argument("--compatibility-policy", default="공개 계약을 보존한다.")
    init.add_argument("--validation", action="append")
    init.add_argument("--runtime-requirement", action="append")
    init.set_defaults(handler=_cmd_project_init)
    show = project_commands.add_parser("show")
    show.add_argument("--project-id", required=True)
    show.set_defaults(handler=_cmd_project_show)
    source = project_commands.add_parser("source")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_add = source_commands.add_parser("add")
    source_add.add_argument("--project-id", required=True)
    source_add.add_argument("--path", required=True)
    source_add.add_argument(
        "--kind",
        choices=[item.value for item in ContextSourceRegistrationKind],
        required=True,
    )
    source_add.add_argument("--register-only", action="store_true")
    source_add.set_defaults(handler=_cmd_project_source_add)
    source_list = source_commands.add_parser("list")
    source_list.add_argument("--project-id", required=True)
    source_list.set_defaults(handler=_cmd_project_source_list)

    budget = project_commands.add_parser("budget")
    budget_commands = budget.add_subparsers(dest="budget_command", required=True)
    budget_set = budget_commands.add_parser("set")
    budget_set.add_argument("--project-id", required=True)
    budget_set.add_argument("--goal-id")
    budget_set.add_argument("--policy-file", required=True)
    budget_set.set_defaults(handler=_cmd_budget_set)
    budget_show = budget_commands.add_parser("show")
    budget_show.add_argument("--project-id", required=True)
    budget_show.add_argument("--goal-id")
    budget_show.set_defaults(handler=_cmd_budget_show)
    budget_adjust = budget_commands.add_parser("adjust-unknown")
    budget_adjust.add_argument("--call-id", required=True)
    budget_adjust.add_argument("--charge-tokens", type=int, required=True)
    budget_adjust.add_argument("--reason", required=True)
    budget_adjust.set_defaults(handler=_cmd_budget_adjust)
    budget_observe = budget_commands.add_parser("observe-role")
    budget_observe.add_argument("--call-id", required=True)
    budget_observe.add_argument("--codex-bin")
    budget_observe.set_defaults(handler=_cmd_budget_observe)

    governance = commands.add_parser("governance")
    governance_commands = governance.add_subparsers(dest="governance_command", required=True)
    check_plugin = governance_commands.add_parser(
        "check-plugin", help="agent-governance-suite 플러그인 채택 전 적합성 검사(임시 자원에서 실행)")
    check_plugin.add_argument("--plugin-root")
    check_plugin.set_defaults(handler=_cmd_governance_check_plugin)

    model = commands.add_parser("model")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_status = model_commands.add_parser("status")
    model_status.add_argument("--project-id", required=True)
    model_status.add_argument("--live", action="store_true")
    model_status.add_argument("--codex-bin")
    model_status.set_defaults(handler=_cmd_model_status)
    model_rebind = model_commands.add_parser("rebind")
    model_rebind.add_argument("--request-file", required=True)
    model_rebind.add_argument("--codex-bin")
    model_rebind.set_defaults(handler=_cmd_model_rebind)

    goal = commands.add_parser("goal")
    goal_commands = goal.add_subparsers(dest="goal_command", required=True)
    create = goal_commands.add_parser("create")
    _add_goal_arguments(create)
    create.set_defaults(handler=_cmd_goal_create)
    revise = goal_commands.add_parser("revise")
    _add_goal_arguments(revise, revise=True)
    revise.set_defaults(handler=_cmd_goal_revise)
    goal_show = goal_commands.add_parser("show")
    goal_show.add_argument("--project-id", required=True)
    goal_show.set_defaults(handler=_cmd_goal_show)
    authorize = goal_commands.add_parser("authorize", help="목표·대상·효과·운영 정책 승인 후 선택 Plan 자동 활성화")
    authorize.add_argument("--project-id", required=True)
    authorize.add_argument("--source", default="cli-user")
    authorize.add_argument("--policy-file")
    authorize.set_defaults(
        handler=_cmd_goal_authorize,
        trusted_action="goal.authorize",
    )

    plan = commands.add_parser("plan")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    search = plan_commands.add_parser("search")
    search.add_argument("--project-id", required=True)
    search.add_argument("--outcome-file")
    search.add_argument("--live", action="store_true")
    search.add_argument("--codex-bin")
    search.add_argument("--role-config")
    search.add_argument(
        "--inspection-contract",
        choices=(PLAN_INSPECTION_PROVIDER_V1, PLAN_INSPECTION_PROVIDER_V2),
        default=PLAN_INSPECTION_PROVIDER_V1,
        help="Plan expander/reviewer provider 형식. 기본값은 v1입니다.",
    )
    search.add_argument("--candidate-count", type=int, choices=(1, 2, 3))
    search.add_argument("--generator-model")
    search.add_argument("--generator-effort")
    search.add_argument("--reviewer-model")
    search.add_argument("--reviewer-effort")
    search.add_argument("--executor-model")
    search.add_argument("--executor-effort")
    search.add_argument("--validator-model")
    search.add_argument("--validator-effort")
    search.set_defaults(handler=_cmd_plan_search)
    compare = plan_commands.add_parser("compare")
    compare.add_argument("--project-id", required=True)
    compare.set_defaults(handler=_cmd_plan_compare)
    activate = plan_commands.add_parser("activate")
    activation_target = activate.add_mutually_exclusive_group(required=True)
    activation_target.add_argument("--project-id", help="Core가 선택한 후보를 활성화")
    activation_target.add_argument("--plan-revision-id", help="내부 진단용 후보 식별자")
    activate.add_argument("--digest", help="선택적 내부 digest 검증")
    activate.add_argument("--source", default="cli")
    activate.set_defaults(handler=_cmd_plan_activate)
    plan_status = plan_commands.add_parser("status")
    plan_status.add_argument("--project-id", required=True)
    plan_status.set_defaults(handler=_cmd_plan_status)

    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_show = task_commands.add_parser("show")
    task_show.add_argument("--task-id", required=True)
    task_show.set_defaults(handler=_cmd_task_show)
    materialize = task_commands.add_parser("materialize")
    materialize_input = materialize.add_mutually_exclusive_group(required=True)
    materialize_input.add_argument("--spec-file")
    materialize_input.add_argument("--proposal-file")
    materialize.add_argument("--inventory-file")
    materialize.add_argument("--codex-bin")
    materialize.set_defaults(handler=_cmd_task_materialize)

    run = commands.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_once = run_commands.add_parser("once")
    run_once.add_argument("--project-id", required=True)
    run_once.add_argument("--codex-bin")
    run_once.add_argument("--proposal-file")
    run_once.add_argument("--role-config")
    run_once.add_argument("--resume", action="store_true")
    goal_validation_input = run_once.add_mutually_exclusive_group()
    goal_validation_input.add_argument("--goal-validation-file")
    goal_validation_input.add_argument("--goal-validation-retry-file")
    run_once.set_defaults(handler=_cmd_run_once)
    run_status = run_commands.add_parser("status")
    run_status.add_argument("--project-id", required=True)
    run_status.set_defaults(handler=_cmd_run_status)

    attempt = commands.add_parser("attempt")
    attempt_commands = attempt.add_subparsers(dest="attempt_command", required=True)
    attempt_show = attempt_commands.add_parser("show")
    attempt_show.add_argument("--attempt-id", required=True)
    attempt_show.set_defaults(handler=_cmd_attempt_show)
    observe = attempt_commands.add_parser("observe")
    observe.add_argument("--attempt-id", required=True)
    observe.add_argument("--codex-bin")
    observe.set_defaults(handler=_cmd_attempt_observe)
    retry = attempt_commands.add_parser("retry")
    retry.add_argument("--task-id", required=True)
    retry.add_argument("--evidence-id", action="append")
    retry.add_argument("--recovery-assessment-file", type=Path)
    retry.add_argument("--failed-validation-result-id")
    retry.set_defaults(handler=_cmd_attempt_retry)
    resume = attempt_commands.add_parser("resume")
    resume.add_argument("--attempt-id", required=True)
    resume.add_argument("--codex-bin")
    resume.set_defaults(handler=_cmd_attempt_resume)
    interrupt = attempt_commands.add_parser("interrupt")
    interrupt.add_argument("--attempt-id", required=True)
    interrupt.add_argument("--codex-bin")
    interrupt.set_defaults(handler=_cmd_attempt_interrupt)

    validate = commands.add_parser("validate")
    validate_commands = validate.add_subparsers(dest="validate_command", required=True)
    validate_task = validate_commands.add_parser("task")
    validate_task.add_argument("--project-id", required=True)
    validate_task.add_argument("--plan-revision-id", required=True)
    validate_task.add_argument("--result-file", required=True)
    validate_task.add_argument("--complete", action="store_true")
    validate_task.set_defaults(handler=_cmd_validate_task)
    validate_goal = validate_commands.add_parser("goal")
    validate_goal.add_argument("--project-id", required=True)
    validate_goal.add_argument("--plan-revision-id", required=True)
    validate_goal.add_argument("--verdict-file", required=True)
    validate_goal.set_defaults(handler=_cmd_validate_goal)
    validate_observe = validate_commands.add_parser("observe")
    validate_observe.add_argument("--project-id", required=True)
    validate_observe.add_argument("--plan-revision-id", required=True)
    validate_observe.add_argument("--kind", choices=("manual", "external"), required=True)
    validate_observe.add_argument("--observation-file", required=True)
    validate_observe.set_defaults(handler=_cmd_validate_observe)

    recover = commands.add_parser("recover")
    recover_commands = recover.add_subparsers(dest="recover_command", required=True)
    recover_inspect = recover_commands.add_parser("inspect")
    recover_inspect.add_argument("--project-id", required=True)
    recover_inspect.set_defaults(handler=_cmd_recover_inspect)
    recover_resume = recover_commands.add_parser("resume")
    recover_resume.add_argument("--intent-id", required=True)
    recover_resume.add_argument("--receipt-file", required=True)
    recover_resume.set_defaults(handler=_cmd_recover_resume)
    recover_abandon = recover_commands.add_parser("abandon")
    recover_abandon.add_argument("--intent-id", required=True)
    recover_abandon.add_argument("--rationale", required=True)
    recover_abandon.set_defaults(handler=_cmd_recover_abandon)

    report = commands.add_parser("report")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    progress = report_commands.add_parser("progress")
    progress.add_argument("--project-id", required=True)
    progress.set_defaults(handler=_cmd_report_progress)
    final = report_commands.add_parser("final")
    final.add_argument("--project-id", required=True)
    final.add_argument("--format", choices=("json", "markdown"), default="json")
    final.add_argument("--goal-verdict-id")
    final.set_defaults(handler=_cmd_report_final)
    return parser


def _execute_parsed(
    arguments: argparse.Namespace,
    *,
    raw_argv: list[str],
) -> int:
    arguments._raw_argv = raw_argv
    try:
        require_host_execution()
        policy = (RoleTimeoutPolicy() if arguments.role_timeout_policy is None else
                  RoleTimeoutPolicy.model_validate(_json(arguments.role_timeout_policy)))
        with use_role_timeout_policy(policy):
            arguments.handler(arguments)
    except StructuredRoleError as error:
        _emit(
            {
                "error": type(error).__name__,
                "message": str(error),
                "receipts": [item.model_dump(mode="json") for item in error.receipts],
            }
        )
        return 2
    except (
        AssignmentResolutionError,
        EngineServiceError,
        EngineApplicationError,
        ModelRebindingError,
        EngineLedgerError,
        GoalPreparationError,
        PlannerRoleAdapterError,
        PlanningError,
        RuntimePolicyError,
        ValidationError,
        ValueError,
        OSError,
        KeyError,
    ) as error:
        prefix = str(error).split(":", 1)[0]
        code = getattr(error, "code", None) or (prefix if prefix.isupper() and " " not in prefix else None)
        _emit({"error": type(error).__name__, "error_code": code, "message": str(error)})
        return 2
    return getattr(arguments, "exit_code", 0)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    arguments = build_parser().parse_args(raw_argv)
    return _execute_parsed(arguments, raw_argv=raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
