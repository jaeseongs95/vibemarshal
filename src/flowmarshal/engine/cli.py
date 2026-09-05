from __future__ import annotations

import argparse
import json
import sys
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
    LifecycleStage,
    MissionClass,
    MutationPolicy,
    PlanContractRevision,
    ProjectMapRevision,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RevisionStatus,
    RuntimeReceipt,
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
from .models import EngineRoleConfiguration
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
from .planning import PlanningError, PlanningSearchOutcome
from .planning import SkeletonFirstPlanner
from .runtime import CodexAppServerRuntime, EngineDispatcher, RuntimePolicyError
from .validation_execution import GoalValidationRetryRequest
from .roles import CodexStructuredRoleRunner, RoleCallReceipt, StructuredRoleError
from .reporting import render_final
from .service import EngineService, EngineServiceError


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
        service.record_budget_usage(
            BudgetUsageRecord(
                usage_id=new_id("usage"),
                project_id=project_id,
                goal_contract_digest=goal_digest,
                stage=stage,
                logical_call_ref=receipt.call_id,
                role=receipt.role,
                call_status=receipt.status,
                model=receipt.model,
                effort=receipt.effort,
                permission_profile=receipt.permission_profile,
                approval_policy=receipt.approval_policy,
                thread_id=receipt.thread_id,
                turn_ids=receipt.turn_ids,
                input_digest=receipt.input_digest,
                output_digest=receipt.output_digest,
                output_schema_digest=receipt.output_schema_digest,
                runner_receipt_digest=sha256_digest(receipt),
                input_tokens=receipt.input_tokens,
                cached_input_tokens=receipt.cached_input_tokens,
                output_tokens=receipt.output_tokens,
                reasoning_tokens=receipt.reasoning_tokens,
                latency_ms=receipt.latency_ms,
                retry_count=receipt.schema_recovery_attempts,
                usage_available=receipt.usage_available,
                recorded_at=receipt.recorded_at,
            )
        )


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
        with CodexAppServerRuntime(codex_bin=arguments.codex_bin) as runtime:
            inventory = runtime.list_models()
            if role_config is not None:
                role_config.validate_inventory(inventory)
            runner = CodexStructuredRoleRunner(runtime)
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
            )
        service.register_goal(
            outcome.goal_contract,
            activate=outcome.goal_contract.status is RevisionStatus.READY,
        )
        _record_role_usage(
            service,
            project_id=arguments.project_id,
            goal_digest=outcome.goal_contract.definition_digest,
            stage=BudgetStage.GOAL_NORMALIZATION,
            receipts=(outcome.normalizer_receipt,),
        )
        _record_role_usage(
            service,
            project_id=arguments.project_id,
            goal_digest=outcome.goal_contract.definition_digest,
            stage=BudgetStage.GOAL_REVIEW,
            receipts=(outcome.reviewer_receipt,),
        )
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
        with CodexAppServerRuntime(codex_bin=arguments.codex_bin) as runtime:
            inventory = runtime.list_models()
            if role_config is not None:
                role_config.validate_inventory(inventory)
            runner = CodexStructuredRoleRunner(runtime)
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
        _record_role_usage(
            service,
            project_id=arguments.project_id,
            goal_digest=outcome.goal_contract.definition_digest,
            stage=BudgetStage.GOAL_NORMALIZATION,
            receipts=(outcome.normalizer_receipt,),
        )
        _record_role_usage(
            service,
            project_id=arguments.project_id,
            goal_digest=outcome.goal_contract.definition_digest,
            stage=BudgetStage.GOAL_REVIEW,
            receipts=(outcome.reviewer_receipt,),
        )
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
        with CodexAppServerRuntime(codex_bin=arguments.codex_bin) as runtime:
            inventory = runtime.list_models()
            if role_config is not None:
                role_config.validate_inventory(inventory)
            runner = CodexStructuredRoleRunner(runtime)
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
            )
            plan_reviewer = PlanReviewerAdapter(
                runner,
                model=reviewer_model,
                effort=reviewer_effort,
                inventory_digest=inventory.inventory_digest, inventory=inventory,
                cwd=root,
                critical_model=critical_model,
                critical_effort=critical_effort,
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
            )
        for evaluation in outcome.skeleton_evaluations:
            service.record_skeleton_evaluation(evaluation)
        for evaluation in outcome.plan_evaluations:
            service.register_plan_evaluation(evaluation)
        for stage, receipts in (
            (BudgetStage.SKELETON_GENERATION, generator.receipts),
            (BudgetStage.SKELETON_REVIEW, skeleton_reviewer.receipts),
            (BudgetStage.PLAN_EXPANSION, expander.receipts),
            (BudgetStage.PLAN_REVIEW, plan_reviewer.receipts),
        ):
            _record_role_usage(
                service,
                project_id=arguments.project_id,
                goal_digest=goal.definition_digest,
                stage=stage,
                receipts=receipts,
            )
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
    activation_id = _service(arguments).activate_plan(
        plan_revision_id=arguments.plan_revision_id,
        activation_digest=arguments.digest,
        source=arguments.source,
    )
    _emit({"activation_id": activation_id, "activation_digest": arguments.digest})


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


def _runtime(arguments: argparse.Namespace) -> CodexAppServerRuntime:
    return CodexAppServerRuntime(codex_bin=arguments.codex_bin)


def _wait_dispatched_turn(
    service: EngineService, runtime: CodexAppServerRuntime, attempt_id: str | None
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
    service = _service(arguments)
    proposal = (
        None
        if arguments.proposal_file is None
        else ExecutionSpecProposal.model_validate(_json(arguments.proposal_file))
    )
    with _runtime(arguments) as runtime:
        from .execution import ExecutionProposalAdapter
        role_configuration = _role_configuration(arguments)
        provider = None if role_configuration is None else ExecutionProposalAdapter(
            service, CodexStructuredRoleRunner(runtime), role_configuration,
        )
        outcome = EngineDispatcher(service, runtime, proposal_provider=provider).run_once(
            arguments.project_id,
            proposal=proposal,
            goal_validation_step=(None if arguments.goal_validation_file is None
                                  else ValidationExecutionStep.model_validate(_json(arguments.goal_validation_file))),
            goal_validation_retry=(
                None if arguments.goal_validation_retry_file is None
                else GoalValidationRetryRequest.model_validate(_json(arguments.goal_validation_retry_file))
            ),
        )
        # 이 CLI 프로세스가 App Server의 소유자다. dispatch 단계만 전이한 뒤
        # 현재 turn 종료까지 연결을 유지하고 결과 판정은 다음 호출에 맡긴다.
        _wait_dispatched_turn(service, runtime, outcome.attempt_id)
    _emit(outcome)


def _cmd_run_status(arguments: argparse.Namespace) -> None:
    _emit(_service(arguments).status(arguments.project_id))


def _cmd_attempt_show(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with service.ledger.read() as connection:
        attempt = connection.execute("SELECT * FROM attempts WHERE id = ?", (arguments.attempt_id,)).fetchone()
        if attempt is None:
            raise EngineServiceError("Attempt를 찾을 수 없습니다.")
        intents = connection.execute(
            "SELECT * FROM runtime_intents WHERE attempt_id = ? ORDER BY prepared_at, rowid",
            (arguments.attempt_id,),
        ).fetchall()
    _emit({"attempt": dict(attempt), "intents": [dict(row) for row in intents]})


def _cmd_attempt_retry(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    service.retry_task(task_id=arguments.task_id, new_evidence_ids=tuple(arguments.evidence_id or ()))
    attempt = service.reserve_attempt(task_id=arguments.task_id)
    _emit(attempt)


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
    service = _service(arguments)
    result = ValidationResult.model_validate(_json(arguments.result_file))
    service.record_validation(
        project_id=arguments.project_id,
        plan_revision_id=arguments.plan_revision_id,
        result=result,
    )
    ready = service.complete_task(result.task_id) if arguments.complete and result.task_id else ()
    _emit({"validation_result_id": result.validation_result_id, "new_ready_task_ids": ready})


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
    _emit({"unknown_intent_ids": service.recover_inspect(arguments.project_id)})


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
    _emit(_progress_report(_service(arguments), arguments.project_id))


def _cmd_report_final(arguments: argparse.Namespace) -> None:
    service = _service(arguments)
    with service.ledger.read() as connection:
        verdict = connection.execute(
            "SELECT plan_revision_id, payload_json FROM goal_verdicts WHERE project_id = ? "
            "ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
            (arguments.project_id,),
        ).fetchone()
        usage = connection.execute(
            "SELECT payload_json FROM budget_usage WHERE project_id = ? ORDER BY recorded_at, rowid",
            (arguments.project_id,),
        ).fetchall()
    if verdict is None:
        raise EngineServiceError("최종 GoalVerdict가 아직 없습니다.")
    usage_docs = [json.loads(row["payload_json"]) for row in usage]
    if arguments.format == "markdown":
        with service.ledger.read() as connection:
            plan_row = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ?",
                (verdict["plan_revision_id"],),
            ).fetchone()
        if plan_row is None:
            raise EngineServiceError("GoalVerdict의 PlanContract를 찾을 수 없습니다.")
        print(
            render_final(
                goal=service.load_active_goal(arguments.project_id),
                plan=PlanContractRevision.model_validate_json(plan_row["payload_json"]),
                verdict=GoalVerdict.model_validate_json(verdict["payload_json"]),
                usage=tuple(BudgetUsageRecord.model_validate(item) for item in usage_docs),
            ),
            end="",
        )
        return
    _emit(
        {
            "verdict": json.loads(verdict["payload_json"]),
            "budget": {
                "records": len(usage_docs),
                "input_tokens": sum(item["input_tokens"] for item in usage_docs),
                "cached_input_tokens": sum(item["cached_input_tokens"] for item in usage_docs),
                "output_tokens": sum(item["output_tokens"] for item in usage_docs),
                "latency_ms": sum(item["latency_ms"] for item in usage_docs),
            },
            "ledger_history_valid": service.ledger.verify_history(arguments.project_id),
        }
    )


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
    parser.add_argument("--db", default=str(Path.cwd() / ".flowmarshal-engine" / DEFAULT_DB_NAME))
    parser.add_argument("--artifacts", default=str(Path.cwd() / ".flowmarshal-engine" / DEFAULT_ARTIFACT_DIRECTORY))
    commands = parser.add_subparsers(dest="command", required=True)

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

    plan = commands.add_parser("plan")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    search = plan_commands.add_parser("search")
    search.add_argument("--project-id", required=True)
    search.add_argument("--outcome-file")
    search.add_argument("--live", action="store_true")
    search.add_argument("--codex-bin")
    search.add_argument("--role-config")
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
    activate.add_argument("--plan-revision-id", required=True)
    activate.add_argument("--digest", required=True)
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
    final.set_defaults(handler=_cmd_report_final)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
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
        _emit({"error": type(error).__name__, "message": str(error)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
