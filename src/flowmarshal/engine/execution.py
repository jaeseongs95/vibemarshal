from __future__ import annotations

from .roles import make_role_request, verify_role_receipt, RoleCallResult

import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .context import (
    AdditionalContextRequest,
    ProjectMapper,
    goal_context_observations,
    resolve_additional_context_request,
)
from .domain import (
    BudgetStage, BudgetUsageRecord, EngineModel, ExecutionSpecProposal, GoalContractRevision, PlanContractRevision,
    ProjectMapRevision, ProjectProfileRevision, TaskContract, ValidationExecutionStep,
    new_id,
)
from .models import EngineRoleConfiguration, ModelInventory
from .operations import CoreOperations
from .roles import RoleCallReceipt, RoleCallRequest, StructuredRolePort
from .service import EngineService, EngineServiceError


_GOAL_VALIDATION_ATTEMPT_ID: ContextVar[str | None] = ContextVar(
    "flowmarshal_goal_validation_attempt_id", default=None,
)


@contextmanager
def goal_validation_attempt_scope(attempt_id: str):
    token = _GOAL_VALIDATION_ATTEMPT_ID.set(attempt_id)
    try:
        yield
    finally:
        _GOAL_VALIDATION_ATTEMPT_ID.reset(token)


class ExecutionPreparation(EngineModel):
    proposal: ExecutionSpecProposal | None = None
    context_request: AdditionalContextRequest | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.proposal is None) == (self.context_request is None):
            raise ValueError("실행 후보와 Context 요청 중 하나만 제출해야 합니다.")
        return self


class GoalTestPreparation(EngineModel):
    step: ValidationExecutionStep


class ProviderValidationDetail(EngineModel):
    """모델은 validation의 의미 대신 운영 상세만 제출한다."""

    validation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    argv: tuple[str, ...] = ()
    working_directory: str | None = Field(default=None, max_length=2000)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = (0,)
    semantic_instruction: str | None = Field(default=None, max_length=5000)
    manual_instruction: str | None = Field(default=None, max_length=5000)
    external_selector: str | None = Field(default=None, max_length=3000)
    artifact_paths: tuple[str, ...] = ()


class ProviderExecutionSpecProposal(ExecutionSpecProposal):
    """수동 proposal 계약은 유지하고 provider의 validation 출력만 축소한다."""

    validation_steps: tuple[ProviderValidationDetail, ...] = Field(min_length=1)


class ProviderExecutionPreparation(EngineModel):
    proposal: ProviderExecutionSpecProposal | None = None
    context_request: AdditionalContextRequest | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.proposal is None) == (self.context_request is None):
            raise ValueError("실행 후보와 Context 요청 중 하나만 제출해야 합니다.")
        return self


class SemanticJudgement(EngineModel):
    passed: bool
    rationale: str = Field(min_length=1, max_length=5000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


EXECUTION_PREPARATION_INSTRUCTIONS = """활성 계약의 운영 상세만 제안하는 비권위 역할이다.
파일·명령을 실행하거나 수정하지 않고 제공된 관찰과 프로젝트 정책만 근거로 JSON을 반환한다.
준비 중인 Task 하나의 목적·효과·validation 의미·모델 배정을 변경하거나 다음 Task를 상세화하지 않는다.
존재하는 파일 경로는 Project Map과 관찰에서 선택한다. 새 산출물은 계약상 create 대상일 때만 제안한다.
없는 사내 자료·외부 계정·환경을 추측하지 않는다. 필요한 context가 없으면 source/selector/이유를
context_request.missing_needs에 작성하고 proposal은 null로 둔다.
validation_id는 Task 계약과 정확히 대응한다. method와 required_evidence_kinds는 Core가 채우므로 출력하지 않는다.
명령을 실행할 action만 kind를 command로 두고 command에 argv를 넣는다. inspect/edit/validate/external_effect
action의 command는 반드시 빈 배열로 둔다.
deterministic validation에는 실제 argv, 프로젝트 내부 cwd, timeout, 예상 종료 코드를 제시한다.
semantic/manual/external 관측은 해당 종류의 필드만 채우고 artifact_paths는 반드시 빈 배열로 둔다.
worker 완료 선언은 검증이 아니다.
최종 authority digest, idempotency key와 상태는 Core가 결정한다."""

GOAL_TEST_PREPARATION_INSTRUCTIONS = """활성 Goal Test 하나의 운영 상세만 제안하는 비권위 역할이다.
파일·명령을 실행하거나 수정하지 않는다. 제공된 goal_test 계약의 validation_id, method,
required_evidence_kinds를 보존하고 독립 통합 검사의 step만 제출한다.
Task 완료 증거를 단순 합산하지 않는다. authority와 상태는 Core가 결정한다."""


def execution_context(service: EngineService, project_id: str) -> dict[str, Any]:
    """Core의 freshness 검사용 전체 권위·관찰을 읽는다. 전송 입력은 별도 투영한다."""
    with service.ledger.read() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None or project["active_plan_revision_id"] is None:
            raise EngineServiceError("활성 PlanContract가 없습니다.")
        values = {}
        for name, table, entity_id in (
            ("plan", "plan_revisions", project["active_plan_revision_id"]),
            ("goal", "goal_revisions", project["active_goal_revision_id"]),
            ("profile", "profile_revisions", project["active_profile_revision_id"]),
        ):
            row = connection.execute(f"SELECT payload_json FROM {table} WHERE id = ?", (entity_id,)).fetchone()
            if row is None:
                raise EngineServiceError(f"실행 준비 authority가 없습니다: {name}")
            values[name] = json.loads(row["payload_json"])
        row = connection.execute("SELECT payload_json FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
                                 (project_id,)).fetchone()
        if row is None:
            raise EngineServiceError("실행 준비 Project Map이 없습니다.")
    goal = GoalContractRevision.model_validate(values["goal"])
    profile = ProjectProfileRevision.model_validate(values["profile"])
    sources = service.validate_context_sources(project_id, required_refs=profile.definition.context_source_refs)
    project_map = ProjectMapRevision.model_validate_json(row["payload_json"])
    observed_map = ProjectMapper().build(
        project_id=project_id, root=project_map.root, revision_no=project_map.revision_no,
        registered_references=(item.path for item in sources if item.kind.value == "reference"),
        instruction_sources=(item.path for item in sources if item.kind.value == "instruction"),
        observed_paths=(item.path for item in project_map.entries if not Path(item.path).is_absolute()),
        requested_symbols={
            item.path: item.symbols
            for item in project_map.entries
            if item.symbols and not Path(item.path).is_absolute()
        },
        excluded_paths=(service.ledger.artifact_root.resolve(),),
    )
    if observed_map.semantic_digest != project_map.semantic_digest:
        raise EngineServiceError("STALE_EXECUTION_INPUT: Project Map과 현재 파일 관찰이 다릅니다.")
    state = service.load_current_state(project_id, goal.definition_digest)
    return values | {
        "project_map": project_map.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
        "runtime_observations": {"python_executable": sys.executable, "python_version": sys.version,
                                 "platform": sys.platform},
        "observed_facts": list(goal_context_observations(project_map, goal.definition.source_request)),
    }


def task_preparation_payload(context: dict[str, Any], task: TaskContract) -> dict[str, Any]:
    """다른 Task 계약과 Goal Test를 노출하지 않는 준비 역할 입력."""
    goal = context["goal"]["definition"]
    return {
        "ready_task": task.model_dump(mode="json"),
        "goal_projection": {
            "goal_contract_digest": context["goal"]["definition_digest"],
            "observable_outcome": goal["observable_outcome"],
            "hard_acceptance": [item for item in goal["hard_acceptance"]
                                if item["criterion_id"] in task.goal_criterion_refs],
            "constraints": goal["constraints"],
            "non_goals": goal["non_goals"],
            "effect_policy": goal["effect_policy"],
        },
        "predecessor_outputs": context["predecessor_outputs"],
        **{key: context[key] for key in ("profile", "project_map", "state", "runtime_observations", "observed_facts")},
    }


def compile_task_preparation(raw: ProviderExecutionPreparation, task: TaskContract) -> ExecutionPreparation:
    if raw.proposal is None:
        result = ExecutionPreparation(context_request=raw.context_request)
    else:
        contracts = {item.validation_id: item for item in task.validations}
        ids = [item.validation_id for item in raw.proposal.validation_steps]
        if len(ids) != len(set(ids)) or set(contracts) != set(ids):
            raise EngineServiceError("ExecutionSpec validation step이 TaskContract와 정확히 대응하지 않습니다.")
        proposal = raw.proposal.model_dump(mode="json")
        proposal["validation_steps"] = [dict(
            item.model_dump(mode="json"), method=contracts[item.validation_id].method,
            required_evidence_kinds=contracts[item.validation_id].required_evidence_kinds,
        ) for item in raw.proposal.validation_steps]
        result = ExecutionPreparation(proposal=ExecutionSpecProposal.model_validate(proposal))
    supplied_id = result.proposal.task_id if result.proposal is not None else result.context_request.task_id
    if supplied_id != task.task_id:
        raise EngineServiceError("실행 준비 응답의 Task binding이 다릅니다.")
    return result


class ExecutionProposalAdapter:
    def __init__(self, service: EngineService, runner: StructuredRolePort,
                 roles: EngineRoleConfiguration, fault_hook: Callable[[str], None] | None = None):
        self.service = service
        self.runner = runner
        self.roles = roles
        self.operations = CoreOperations(service, fault_hook)

    def _budgeted_runner(self, project_id: str, context: dict[str, Any]):
        from .budget import BudgetedRoleRunner
        return BudgetedRoleRunner(self.runner, self.service, project_id=project_id,
                                  goal_id=context["goal"]["goal_id"],
                                  goal_digest=context["goal"]["definition_digest"],
                                  attempt_id=_GOAL_VALIDATION_ATTEMPT_ID.get())

    def _predecessor_outputs(self, plan: PlanContractRevision, task: TaskContract) -> list[dict[str, Any]]:
        outputs = []
        with self.service.ledger.read() as connection:
            for dependency in plan.definition.dependencies:
                if dependency.consumer_task_id != task.task_id:
                    continue
                rows = self.service.task_evidence_rows(connection, dependency.producer_task_id)
                outputs.append({"producer_task_id": dependency.producer_task_id,
                                "products": list(dependency.products),
                                "evidence": [json.loads(row["payload_json"]) for row in rows]})
        return outputs

    def _record_usage(self, project_id, context, result, stage):
        receipt = RoleCallReceipt.model_validate(result["receipt"])
        with self.service.ledger.read() as connection:
            if connection.execute("SELECT id FROM budget_usage WHERE project_id = ? AND logical_call_ref = ?",
                                  (project_id, receipt.call_id)).fetchone() is not None:
                return
        values = receipt.model_dump()
        fields = ("role", "model", "effort", "permission_profile", "approval_policy", "thread_id", "turn_ids",
                  "input_digest", "output_digest", "output_schema_digest", "input_tokens", "cached_input_tokens",
                  "output_tokens", "reasoning_tokens", "latency_ms", "usage_available", "recorded_at")
        self.service.record_budget_usage(BudgetUsageRecord(
            usage_id=new_id("usage"), project_id=project_id,
            goal_contract_digest=context["goal"]["definition_digest"], stage=stage,
            logical_call_ref=receipt.call_id, call_status=receipt.status,
            runner_receipt_digest=sha256_digest(receipt), retry_count=receipt.schema_recovery_attempts,
            **{key: values[key] for key in fields},
        ))

    def _run(
        self,
        project_id: str,
        inventory: ModelInventory,
        context: dict[str, Any],
        model_type: type[EngineModel],
        kind: str,
        *,
        payload: dict[str, Any],
        instructions: str | None = None,
        validator: Callable[[dict[str, Any]], EngineModel] | None = None,
    ) -> EngineModel:
        self.roles.validate_inventory(inventory)
        output_validator = validator or model_type.model_validate
        binding = self.roles.plan_expander
        request = make_role_request(
            inventory=inventory, allowed_fallbacks=binding.allowed_fallbacks,
            role=kind, instructions=instructions or EXECUTION_PREPARATION_INSTRUCTIONS,
            payload=payload, output_schema=model_type.model_json_schema(),
            model=binding.model, effort=binding.effort,
            inventory_digest=inventory.inventory_digest,
            cwd=context["project_map"]["root"],
        )
        runner = self._budgeted_runner(project_id, context)
        def execute_authorized():
            self.service.assert_project_authorized(project_id)
            return runner.run(request, validator=output_validator).model_dump(mode="json")
        result = self.operations.invoke(
            project_id=project_id, kind=kind,
            request={"role_request": request.model_dump(mode="json"), "authority_context_digest": sha256_digest(context)},
            execute=execute_authorized,
        )
        verify_role_receipt(request, RoleCallResult.model_validate(result))
        self._record_usage(project_id, context, result,
                           BudgetStage.EXECUTION_PREPARATION if kind == "execution_preparation" else BudgetStage.VALIDATION)
        # operation.completed 재생도 최초 role 출력과 같은 계약으로 검증한다.
        return output_validator(result["payload"])

    def prepare_task(self, *, project_id: str, task_id: str, inventory: ModelInventory) -> ExecutionPreparation:
        context = execution_context(self.service, project_id)
        plan = PlanContractRevision.model_validate(context["plan"])
        task = next((item for item in plan.definition.tasks if item.task_id == task_id), None)
        if task is None:
            raise EngineServiceError("활성 Plan에 없는 Task입니다.")
        context["predecessor_outputs"] = self._predecessor_outputs(plan, task)
        payload = task_preparation_payload(context, task)

        def validate_task_preparation(value: dict[str, Any]) -> ProviderExecutionPreparation:
            raw = ProviderExecutionPreparation.model_validate(value)
            # TaskContract와 결속한 최종 ExecutionSpec 검증은 recovery 대상이다.
            compile_task_preparation(raw, task)
            return raw

        raw = self._run(
            project_id,
            inventory,
            context,
            ProviderExecutionPreparation,
            "execution_preparation",
            payload=payload,
            validator=validate_task_preparation,
        )
        if raw.context_request is not None:
            resolution = resolve_additional_context_request(
                project_map=ProjectMapRevision.model_validate(context["project_map"]),
                request=raw.context_request,
                token_budget=12_000,
            )
            # RuntimeJob 하나는 exact provider turn 하나만 소유한다. 비동기 준비
            # 경계에서는 후속 role call을 같은 job 안에서 시작하지 않고 ContextRequest를
            # Core에 돌려 다음 materialization 입력으로 명시적으로 처리하게 한다.
            from .runtime import active_runtime_job_id

            if resolution.resolved and active_runtime_job_id() is None:
                payload = payload | {
                    "additional_context": [
                        item.model_dump(mode="json") for item in resolution.resolved
                    ],
                    "unresolved_context_request": (
                        None
                        if resolution.unresolved_request is None
                        else resolution.unresolved_request.model_dump(mode="json")
                    ),
                }
                raw = self._run(
                    project_id,
                    inventory,
                    context,
                    ProviderExecutionPreparation,
                    "execution_preparation",
                    payload=payload,
                    instructions=(
                        EXECUTION_PREPARATION_INSTRUCTIONS
                        + "\n직전 ContextRequest에 대해 Project Map 전체를 검색한 additional_context가 "
                        "제공됐다. 그 본문을 사용해 proposal을 완성하되 unresolved 항목은 추측하지 않는다."
                    ),
                    validator=validate_task_preparation,
                )
        result = compile_task_preparation(raw, task)
        # 모델 호출 사이 원장 revision·관찰이 바뀌면 이전 응답을 새 snapshot에 세탁하지 않는다.
        current = execution_context(self.service, project_id)
        current["predecessor_outputs"] = self._predecessor_outputs(PlanContractRevision.model_validate(current["plan"]), task)
        if sha256_digest(current) != sha256_digest(context):
            raise EngineServiceError("STALE_EXECUTION_INPUT: 실행 준비 중 authority 또는 관찰이 바뀌었습니다.")
        return result

    def prepare_goal(self, *, project_id: str, validation_id: str, inventory: ModelInventory) -> ValidationExecutionStep:
        context = execution_context(self.service, project_id)
        plan = PlanContractRevision.model_validate(context["plan"])
        contract = next(item for item in plan.definition.integration_validations if item.validation_id == validation_id)
        payload = {key: value for key, value in context.items() if key != "plan"}
        payload["goal_test"] = contract.model_dump(mode="json")

        def validate_goal_preparation(value: dict[str, Any]) -> GoalTestPreparation:
            result = GoalTestPreparation.model_validate(value)
            if (result.step.validation_id != contract.validation_id or result.step.method != contract.method
                    or set(result.step.required_evidence_kinds) != set(contract.required_evidence_kinds)):
                raise EngineServiceError("Goal Test 운영 상세가 활성 integration validation과 정확히 대응하지 않습니다.")
            return result

        result = self._run(
            project_id,
            inventory,
            context,
            GoalTestPreparation,
            "goal_test_preparation",
            payload=payload,
            instructions=GOAL_TEST_PREPARATION_INSTRUCTIONS,
            validator=validate_goal_preparation,
        )
        current = execution_context(self.service, project_id)
        if sha256_digest(current) != sha256_digest(context):
            raise EngineServiceError("STALE_EXECUTION_INPUT: Goal Test 준비 중 입력이 바뀌었습니다.")
        return result.step

    def validate_goal(self, *, project_id: str, inventory: ModelInventory,
                      context: dict[str, Any], evidence_catalog: dict[str, Any],
                      step: ValidationExecutionStep,
                      goal_validation_binding_digest: str) -> dict[str, Any]:
        self.roles.validate_inventory(inventory)
        binding = self.roles.validator
        request = make_role_request(
            inventory=inventory, allowed_fallbacks=binding.allowed_fallbacks,
            role="goal_validator",
            instructions=("독립 Goal Validator다. 파일을 수정하거나 명령을 실행하지 않는다. "
                          "제공된 직접 관측과 evidence만 검토하고 Goal Test 의미의 충족 여부를 제출한다. "
                          "Worker 응답의 완료 주장은 검증 근거가 아니다. 보고 내용 자체가 검사 대상이면 "
                          "응답 관측을 원본 파일 등 직접 근거와 대조하고 관련 evidence를 함께 참조한다. "
                          "Task나 Goal 상태를 결정하지 않는다. evidence_refs에는 제공된 evidence ID만 사용한다."),
            payload={
                "context": context,
                "step": step.model_dump(mode="json"),
                "goal_validation_binding_digest": goal_validation_binding_digest,
                "evidence_catalog": evidence_catalog,
            },
            output_schema=SemanticJudgement.model_json_schema(),
            model=binding.model, effort=binding.effort, inventory_digest=inventory.inventory_digest,
            cwd=context["project_map"]["root"],
        )

        def validate(raw):
            judgement = SemanticJudgement.model_validate(raw)
            if len(judgement.evidence_refs) != len(set(judgement.evidence_refs)):
                raise ValueError("중복 semantic evidence ref입니다.")
            if set(judgement.evidence_refs) - set(evidence_catalog):
                raise ValueError("제공되지 않은 semantic evidence ref입니다.")
            return judgement

        runner = self._budgeted_runner(project_id, context)
        def execute_authorized():
            self.service.assert_project_authorized(project_id)
            return runner.run(request, validator=validate).model_dump(mode="json")
        result = self.operations.invoke(
            project_id=project_id, kind="goal_validation", request=request.model_dump(mode="json"),
            execute=execute_authorized,
        )
        verify_role_receipt(request, RoleCallResult.model_validate(result))
        self._record_usage(project_id, context, result, BudgetStage.VALIDATION)
        return result
