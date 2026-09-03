from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..canonical import sha256_digest
from .r31_domain import (
    ModelCallReceipt,
    ModelCallStatus,
    ModelUsageMetric,
    PlanningRole,
)


class ModelBoundaryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


PlanningModelRole = PlanningRole


class AvailablePlanningModel(ModelBoundaryModel):
    model_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    supported_efforts: tuple[str, ...] = Field(min_length=1)
    is_default: bool = False

    @field_validator("supported_efforts")
    @classmethod
    def efforts_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("지원 추론 수준이 중복됐습니다.")
        return value


class ModelRolePreference(ModelBoundaryModel):
    role: PlanningModelRole
    preferred_model_ids: tuple[str, ...] = Field(min_length=1)
    preferred_effort: str = Field(min_length=1, max_length=40)

    @field_validator("preferred_model_ids")
    @classmethod
    def models_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("선호 모델 ID가 중복됐습니다.")
        return value


class ResolvedPlanningModel(ModelBoundaryModel):
    role: PlanningModelRole
    model_id: str
    reasoning_effort: str
    inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class StructuredRoleRequest(ModelBoundaryModel):
    role: PlanningModelRole
    instructions: str = Field(min_length=1, max_length=50_000)
    payload: dict[str, Any]
    output_schema: dict[str, Any]
    model_id: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cwd: str = Field(min_length=3, max_length=2000)

    @property
    def request_digest(self) -> str:
        return sha256_digest(self)


class StructuredRoleResult(ModelBoundaryModel):
    payload: dict[str, Any]
    receipt: ModelCallReceipt


class ModelResolutionError(RuntimeError):
    pass


class ExecutionPolicyError(RuntimeError):
    pass


class StructuredRoleError(RuntimeError):
    def __init__(self, message: str, *, receipt: ModelCallReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class CodexLike(Protocol):
    def models(self, *, include_hidden: bool = False) -> Any: ...

    def verify_execution_policy(self, cwd: str | Path) -> "ExecutionPolicyEvidence": ...

    def thread_start(self, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


class ExecutionPolicyEvidence(ModelBoundaryModel):
    environment: Literal["local"] = "local"
    permission_profile: Literal[":danger-full-access"] = ":danger-full-access"
    approval_policy: Literal["never"] = "never"
    config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_catalog_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    thread_id: str | None = None
    thread_response_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    cwd: str | None = None
    model_id: str | None = None
    instruction_sources: tuple[str, ...] = ()
    runtime_workspace_roots: tuple[str, ...] = ()


def _same_local_path(left: str | Path, right: str | Path) -> bool:
    def canonical(value: str | Path) -> str:
        raw = str(value)
        if raw.startswith("\\\\?\\"):
            raw = raw[4:]
        return os.path.normcase(os.path.normpath(os.path.abspath(raw)))

    return canonical(left) == canonical(right)


class PolicyVerifiedCodex:
    """App Server 원시 응답으로 로컬 full-access/never를 검증하는 SDK wrapper."""

    def __init__(
        self,
        *,
        codex_bin: str | Path,
        evidence_sink: Callable[[ExecutionPolicyEvidence], None] | None = None,
    ) -> None:
        from openai_codex import Codex
        from openai_codex.client import CodexConfig

        resolved_bin = Path(codex_bin).resolve(strict=True)
        if not resolved_bin.is_file():
            raise ExecutionPolicyError("Codex 실행 파일이 아닙니다.")
        self._codex = Codex(CodexConfig(codex_bin=str(resolved_bin)))
        self._evidence_sink = evidence_sink
        self._preflight: ExecutionPolicyEvidence | None = None

    def models(self, *, include_hidden: bool = False) -> Any:
        return self._codex.models(include_hidden=include_hidden)

    def verify_execution_policy(self, cwd: str | Path) -> ExecutionPolicyEvidence:
        workspace = Path(cwd).resolve(strict=True)
        if not workspace.is_dir():
            raise ExecutionPolicyError("PERMISSION_POLICY_MISMATCH: cwd가 디렉터리가 아닙니다.")
        config_response = self._raw_object(
            "config/read",
            {"cwd": str(workspace), "includeLayers": True},
        )
        config = config_response.get("config")
        if not isinstance(config, dict):
            raise ExecutionPolicyError(
                "PERMISSION_POLICY_MISMATCH: config/read에서 실제 설정을 확인하지 못했습니다."
            )
        approval = config.get("approval_policy", config.get("approvalPolicy"))
        permission = config.get(
            "default_permissions",
            config.get("defaultPermissions"),
        )
        if approval != "never" or permission != ":danger-full-access":
            raise ExecutionPolicyError(
                "PERMISSION_POLICY_MISMATCH: 실제 기본 정책이 "
                f":danger-full-access/never가 아닙니다: {permission!r}/{approval!r}"
            )
        profiles_response = self._raw_object(
            "permissionProfile/list",
            {"cwd": str(workspace), "limit": 100},
        )
        profiles = profiles_response.get("data")
        if not isinstance(profiles, list) or any(
            not isinstance(item, dict) for item in profiles
        ):
            raise ExecutionPolicyError(
                "PERMISSION_POLICY_MISMATCH: permission profile 목록이 유효하지 않습니다."
            )
        if profiles_response.get("nextCursor") is not None:
            raise ExecutionPolicyError(
                "PERMISSION_POLICY_MISMATCH: permission profile pagination을 완전히 읽지 못했습니다."
            )
        selected = next(
            (item for item in profiles if item.get("id") == ":danger-full-access"),
            None,
        )
        if selected is None or selected.get("allowed") is not True:
            raise ExecutionPolicyError(
                "PERMISSION_POLICY_MISMATCH: :danger-full-access가 허용되지 않았습니다."
            )
        evidence = ExecutionPolicyEvidence(
            config_digest=sha256_digest(config),
            profile_catalog_digest=sha256_digest(profiles),
        )
        self._preflight = evidence
        return evidence

    def thread_start(self, **kwargs: Any) -> Any:
        from openai_codex.api import Thread

        cwd = Path(str(kwargs.get("cwd", ""))).resolve(strict=True)
        model = str(kwargs.get("model", "")).strip()
        approval_mode = getattr(kwargs.get("approval_mode"), "value", None)
        sandbox = getattr(kwargs.get("sandbox"), "value", None)
        if approval_mode != "deny_all" or sandbox != "full-access":
            raise ExecutionPolicyError(
                "PERMISSION_POLICY_MISMATCH: 역할 thread 요청값이 full-access/deny_all이 아닙니다."
            )
        if not model:
            raise ExecutionPolicyError("MODEL_PROVENANCE_MISMATCH: thread 모델이 비었습니다.")
        preflight = self.verify_execution_policy(cwd)
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "baseInstructions": kwargs.get("base_instructions"),
            "cwd": str(cwd),
            "ephemeral": bool(kwargs.get("ephemeral", True)),
            "model": model,
            "permissions": ":danger-full-access",
        }
        response = self._raw_object("thread/start", params)
        profile = response.get("activePermissionProfile")
        active_profile = profile.get("id") if isinstance(profile, dict) else profile
        if active_profile != ":danger-full-access" or response.get("approvalPolicy") != "never":
            raise ExecutionPolicyError(
                "PERMISSION_POLICY_MISMATCH: thread/start 실제 정책이 "
                f"{active_profile!r}/{response.get('approvalPolicy')!r}입니다."
            )
        if not _same_local_path(str(response.get("cwd", "")), cwd):
            raise ExecutionPolicyError("THREAD_PROVENANCE_MISMATCH: 실제 cwd가 요청과 다릅니다.")
        if response.get("model") != model:
            raise ExecutionPolicyError("MODEL_PROVENANCE_MISMATCH: 실제 thread 모델이 요청과 다릅니다.")
        roots = response.get("runtimeWorkspaceRoots")
        if not isinstance(roots, list) or not any(
            isinstance(item, str) and _same_local_path(item, cwd) for item in roots
        ):
            raise ExecutionPolicyError(
                "THREAD_PROVENANCE_MISMATCH: runtimeWorkspaceRoots에 요청 cwd가 없습니다."
            )
        thread = response.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise ExecutionPolicyError("THREAD_PROVENANCE_MISMATCH: thread ID가 없습니다.")
        sources = response.get("instructionSources")
        if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
            raise ExecutionPolicyError(
                "INSTRUCTION_PROVENANCE_MISSING: instructionSources가 유효하지 않습니다."
            )
        evidence = preflight.model_copy(
            update={
                "thread_id": thread_id,
                "thread_response_digest": sha256_digest(response),
                "cwd": str(cwd),
                "model_id": model,
                "instruction_sources": tuple(sources),
                "runtime_workspace_roots": tuple(str(item) for item in roots),
            }
        )
        if self._evidence_sink is not None:
            self._evidence_sink(evidence)
        return Thread(self._codex._client, thread_id)

    def close(self) -> None:
        self._codex.close()

    def _raw_object(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        value = self._codex._client._request_raw(method, params)
        if not isinstance(value, dict):
            raise ExecutionPolicyError(f"{method} 응답이 JSON object가 아닙니다.")
        return value


class CodexModelInventoryAdapter:
    """Codex model/list 응답을 Planner가 소비하는 작은 계약으로 정규화한다."""

    def __init__(self, factory: Callable[[], CodexLike]) -> None:
        self._factory = factory

    def list_models(self) -> tuple[AvailablePlanningModel, ...]:
        client = self._factory()
        try:
            return _normalize_model_inventory(client.models(include_hidden=False))
        finally:
            _close_client_safely(client)


_REASONING_EFFORT_ORDER = {
    name: index
    for index, name in enumerate(
        ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    )
}


def _effort_sort_key(effort: str) -> tuple[int, str]:
    return (_REASONING_EFFORT_ORDER.get(effort, len(_REASONING_EFFORT_ORDER)), effort)


def _normalize_model_inventory(response: Any) -> tuple[AvailablePlanningModel, ...]:
    next_cursor = _value(response, "next_cursor", None)
    if next_cursor is not None:
        raise ModelResolutionError(
            "model/list 응답에 다음 페이지가 있지만 현재 adapter는 pagination을 지원하지 않습니다."
        )
    raw_models = getattr(response, "data", None)
    if raw_models is None and isinstance(response, dict):
        raw_models = response.get("data")
    if not isinstance(raw_models, list):
        raise ModelResolutionError("model/list 응답에 모델 목록이 없습니다.")
    models: list[AvailablePlanningModel] = []
    visible_model_ids: set[str] = set()
    for raw in raw_models:
        hidden = _value(raw, "hidden", False)
        if hidden:
            continue
        model_id = str(_value(raw, "id", _value(raw, "model", ""))).strip()
        if model_id:
            if model_id in visible_model_ids:
                raise ModelResolutionError(
                    f"model/list 응답에 모델 ID가 중복됐습니다: {model_id}"
                )
            visible_model_ids.add(model_id)
        display_name = str(_value(raw, "display_name", model_id)).strip()
        effort_options = _value(raw, "supported_reasoning_efforts", [])
        efforts = tuple(
            sorted(
                (
                    _string_value(
                        _value(
                            option,
                            "reasoning_effort",
                            _value(option, "effort", option),
                        )
                    ).strip()
                    for option in effort_options
                ),
                key=_effort_sort_key,
            )
        )
        if model_id and efforts:
            models.append(
                AvailablePlanningModel(
                    model_id=model_id,
                    display_name=display_name or model_id,
                    supported_efforts=efforts,
                    is_default=bool(_value(raw, "is_default", False)),
                )
            )
    if not models:
        raise ModelResolutionError("model/list에 사용할 수 있는 visible 모델이 없습니다.")
    return tuple(sorted(models, key=lambda item: item.model_id))


def resolve_model_role(
    inventory: tuple[AvailablePlanningModel, ...],
    preference: ModelRolePreference,
) -> ResolvedPlanningModel:
    inventory_digest = sha256_digest(inventory)
    by_id = {item.model_id: item for item in inventory}
    for model_id in preference.preferred_model_ids:
        model = by_id.get(model_id)
        if model is None:
            continue
        if preference.preferred_effort not in model.supported_efforts:
            raise ModelResolutionError(
                f"{model_id}가 {preference.preferred_effort} 추론 수준을 지원하지 않습니다."
            )
        return ResolvedPlanningModel(
            role=preference.role,
            model_id=model.model_id,
            reasoning_effort=preference.preferred_effort,
            inventory_digest=inventory_digest,
        )
    raise ModelResolutionError(
        f"{preference.role.value} 역할의 선호 모델을 model/list에서 찾지 못했습니다."
    )


def strict_json_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic schema를 OpenAI strict structured-output 계약으로 정규화한다."""

    normalized = copy.deepcopy(schema)

    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    string_array = {"type": "array", "items": {"type": "string"}}
    typed_freeform_fields: dict[str, dict[str, Any]] = {
        "specification": {
            "command": copy.deepcopy(nullable_string),
            "working_directory": copy.deepcopy(nullable_string),
            "procedure": copy.deepcopy(string_array),
            "inputs": copy.deepcopy(string_array),
            "assertions": copy.deepcopy(string_array),
            "expected_results": copy.deepcopy(string_array),
            "evidence_paths": copy.deepcopy(string_array),
        },
        "execution_requirements": {
            "runtime": copy.deepcopy(nullable_string),
            "permission_profile": copy.deepcopy(nullable_string),
            "network": copy.deepcopy(nullable_string),
            "preconditions": copy.deepcopy(string_array),
            "session_strategy": copy.deepcopy(nullable_string),
            "handoff_contract": copy.deepcopy(nullable_string),
            "resource_limits": copy.deepcopy(string_array),
        },
        "fallback_policy": {
            "mode": copy.deepcopy(nullable_string),
            "triggers": copy.deepcopy(string_array),
            "action": copy.deepcopy(nullable_string),
        },
    }

    def visit(node: Any, field_name: str | None = None) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, field_name)
            return
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        if "$ref" in node:
            # Responses API structured output은 $ref sibling keyword를
            # 허용하지 않는다. Pydantic이 Field의 title/description을 같은
            # node에 붙여도 wire schema에서는 순수 참조로 정규화한다.
            reference = node["$ref"]
            node.clear()
            node["$ref"] = reference
            return
        if node.get("type") == "object":
            # Responses API strict schema는 자유형 map까지 모든 object에
            # additionalProperties=false를 요구한다. 속성 schema가 없는 map은
            # 이 경계에서 알려진 의미 map은 제한된 typed object로 승격하고,
            # 나머지 자유형 map은 빈 object로 제한한다.
            node["additionalProperties"] = False
            properties = node.setdefault("properties", {})
            if not properties and field_name in typed_freeform_fields:
                properties.update(copy.deepcopy(typed_freeform_fields[field_name]))
            node["required"] = list(properties)
        else:
            properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                visit(child, name)
        for key, value in tuple(node.items()):
            if key != "properties":
                visit(value, field_name)

    visit(normalized)
    return normalized


class CodexStructuredRoleRunner:
    """전체 권한·무승인 ephemeral Codex thread에서 strict JSON 역할을 실행한다.

    Planner의 권위는 OS sandbox 강등이 아니라 전달받는 typed artifact와 서비스
    capability로 제한한다. ``Sandbox.full_access``는 App Server wire의
    ``danger-full-access``로, ``ApprovalMode.deny_all``은 ``never``로 변환된다.
    """

    def __init__(
        self,
        factory: Callable[[], CodexLike],
        *,
        call_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._factory = factory
        self._call_id_factory = call_id_factory or (lambda: f"model_call_{uuid4().hex}")

    def run(
        self,
        request: StructuredRoleRequest,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> StructuredRoleResult:
        from openai_codex import ApprovalMode, Sandbox
        from openai_codex.types import ReasoningEffort, TurnStatus

        bound_request = request.model_copy(deep=True)
        input_digest = bound_request.request_digest
        output_schema_digest = sha256_digest(bound_request.output_schema)
        payload_wire = json.dumps(bound_request.payload, ensure_ascii=False)
        output_schema = copy.deepcopy(bound_request.output_schema)
        try:
            effort = ReasoningEffort(bound_request.reasoning_effort)
        except ValueError as exc:
            raise self._preflight_error(
                bound_request,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                message=(
                    f"현재 SDK가 {bound_request.reasoning_effort} 추론 수준을 지원하지 "
                    "않습니다."
                ),
            ) from exc
        try:
            cwd = Path(bound_request.cwd).resolve(strict=True)
        except OSError as exc:
            raise self._preflight_error(
                bound_request,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                message="Planner 역할 cwd를 확인할 수 없습니다.",
            ) from exc
        if not cwd.is_dir():
            raise self._preflight_error(
                bound_request,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                message="Planner 역할 cwd는 디렉터리여야 합니다.",
            )

        try:
            client = self._factory()
        except Exception as exc:
            raise self._preflight_error(
                bound_request,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                message=f"Planner model client 생성 실패: {type(exc).__name__}",
            ) from exc
        try:
            try:
                client.verify_execution_policy(cwd)
            except Exception as exc:
                raise self._preflight_error(
                    bound_request,
                    input_digest=input_digest,
                    output_schema_digest=output_schema_digest,
                    message=(
                        "PERMISSION_POLICY_MISMATCH: 실제 역할 실행 정책을 확인하지 "
                        f"못했습니다: {type(exc).__name__}: {exc}"
                    ),
                ) from exc
            try:
                inventory = _normalize_model_inventory(
                    client.models(include_hidden=False)
                )
            except Exception as exc:
                raise self._unavailable_call(
                    bound_request,
                    input_digest=input_digest,
                    output_schema_digest=output_schema_digest,
                    inventory_digest=bound_request.inventory_digest,
                    message=f"실행 직전 model/list 확인 실패: {type(exc).__name__}",
                ) from exc
            live_inventory_digest = sha256_digest(inventory)
            selected = next(
                (item for item in inventory if item.model_id == bound_request.model_id),
                None,
            )
            if live_inventory_digest != bound_request.inventory_digest:
                raise self._unavailable_call(
                    bound_request,
                    input_digest=input_digest,
                    output_schema_digest=output_schema_digest,
                    inventory_digest=live_inventory_digest,
                    message="model/list inventory가 역할 해석 시점 이후 변경됐습니다.",
                )
            if (
                selected is None
                or bound_request.reasoning_effort not in selected.supported_efforts
            ):
                raise self._unavailable_call(
                    bound_request,
                    input_digest=input_digest,
                    output_schema_digest=output_schema_digest,
                    inventory_digest=live_inventory_digest,
                    message="필수 모델 또는 reasoning effort를 실행 직전 inventory에서 찾지 못했습니다.",
                )
            try:
                thread = client.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    base_instructions=bound_request.instructions,
                    cwd=str(cwd),
                    ephemeral=True,
                    model=bound_request.model_id,
                    sandbox=Sandbox.full_access,
                )
            except Exception as exc:
                raise self._exception_call(
                    bound_request,
                    None,
                    [],
                    exc,
                    input_digest=input_digest,
                    output_schema_digest=output_schema_digest,
                    inventory_digest=live_inventory_digest,
                ) from exc
            turns: list[Any] = []
            try:
                turn = thread.run(
                    payload_wire,
                    effort=effort,
                    model=bound_request.model_id,
                    output_schema=copy.deepcopy(output_schema),
                    sandbox=Sandbox.full_access,
                )
            except Exception as exc:
                raise self._exception_call(
                    bound_request,
                    str(thread.id),
                    turns,
                    exc,
                    input_digest=input_digest,
                    output_schema_digest=output_schema_digest,
                    inventory_digest=live_inventory_digest,
                ) from exc
            turns.append(turn)
            if turn.status is not TurnStatus.completed or not turn.final_response:
                raise self._failed_call(
                    bound_request,
                    thread_id=str(thread.id),
                    turns=turns,
                    message=f"Planner 역할 turn이 완료되지 않았습니다: {turn.status}",
                    input_digest=input_digest,
                    output_schema_digest=output_schema_digest,
                    inventory_digest=live_inventory_digest,
                )
            recovered = False
            try:
                payload = _decode_structured_output(turn.final_response, output_schema)
                _apply_typed_validator(payload, validator)
            except StructuredRoleError as first_error:
                recovered = True
                try:
                    recovery_inventory = _normalize_model_inventory(
                        client.models(include_hidden=False)
                    )
                except Exception as exc:
                    raise self._unavailable_call(
                        bound_request,
                        input_digest=input_digest,
                        output_schema_digest=output_schema_digest,
                        inventory_digest=live_inventory_digest,
                        message=(
                            "schema 복구 직전 model/list 확인 실패: "
                            f"{type(exc).__name__}"
                        ),
                        thread_id=str(thread.id),
                        turns=turns,
                    ) from exc
                recovery_inventory_digest = sha256_digest(recovery_inventory)
                recovery_selected = next(
                    (
                        item
                        for item in recovery_inventory
                        if item.model_id == bound_request.model_id
                    ),
                    None,
                )
                if recovery_inventory_digest != live_inventory_digest:
                    raise self._unavailable_call(
                        bound_request,
                        input_digest=input_digest,
                        output_schema_digest=output_schema_digest,
                        inventory_digest=recovery_inventory_digest,
                        message="schema 복구 직전 model/list inventory가 변경됐습니다.",
                        thread_id=str(thread.id),
                        turns=turns,
                    )
                if (
                    recovery_selected is None
                    or bound_request.reasoning_effort
                    not in recovery_selected.supported_efforts
                ):
                    raise self._unavailable_call(
                        bound_request,
                        input_digest=input_digest,
                        output_schema_digest=output_schema_digest,
                        inventory_digest=recovery_inventory_digest,
                        message=(
                            "schema 복구에 필요한 모델 또는 reasoning effort를 "
                            "model/list에서 찾지 못했습니다."
                        ),
                        thread_id=str(thread.id),
                        turns=turns,
                    )
                try:
                    recovery_turn = thread.run(
                        json.dumps(
                            {
                                "original_input": bound_request.payload,
                                "schema_recovery": {
                                    "error": str(first_error),
                                    "instruction": (
                                        "직전 출력을 버리고 지정된 JSON Schema에 맞는 "
                                        "object만 다시 반환하세요."
                                    ),
                                },
                            },
                            ensure_ascii=False,
                        ),
                        effort=effort,
                        model=bound_request.model_id,
                        output_schema=copy.deepcopy(output_schema),
                        sandbox=Sandbox.full_access,
                    )
                except Exception as exc:
                    raise self._exception_call(
                        bound_request,
                        str(thread.id),
                        turns,
                        exc,
                        schema_recovery_attempts=1,
                        input_digest=input_digest,
                        output_schema_digest=output_schema_digest,
                        inventory_digest=live_inventory_digest,
                    ) from exc
                turns.append(recovery_turn)
                if (
                    recovery_turn.status is not TurnStatus.completed
                    or not recovery_turn.final_response
                ):
                    raise self._failed_call(
                        bound_request,
                        thread_id=str(thread.id),
                        turns=turns,
                        message="schema 복구 turn이 완료되지 않았습니다.",
                        schema_recovery_attempts=1,
                        input_digest=input_digest,
                        output_schema_digest=output_schema_digest,
                        inventory_digest=live_inventory_digest,
                    )
                try:
                    payload = _decode_structured_output(
                        recovery_turn.final_response,
                        output_schema,
                    )
                    _apply_typed_validator(payload, validator)
                except StructuredRoleError as exc:
                    raise self._failed_call(
                        bound_request,
                        thread_id=str(thread.id),
                        turns=turns,
                        message=(
                            "1회 schema 복구 후에도 출력 계약을 충족하지 못했습니다: "
                            f"{exc}"
                        ),
                        schema_recovery_attempts=1,
                        input_digest=input_digest,
                        output_schema_digest=output_schema_digest,
                        inventory_digest=live_inventory_digest,
                    ) from exc
            usage = _merge_usage(turns)
            receipt = ModelCallReceipt(
                call_id=self._call_id_factory(),
                role=bound_request.role,
                model_id=bound_request.model_id,
                reasoning_effort=bound_request.reasoning_effort,
                inventory_digest=live_inventory_digest,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                output_digest=sha256_digest(payload),
                status=(
                    ModelCallStatus.SCHEMA_RECOVERED
                    if recovered
                    else ModelCallStatus.SUCCEEDED
                ),
                schema_recovery_attempts=int(recovered),
                thread_id=str(thread.id),
                turn_ids=tuple(str(item.id) for item in turns),
                token_count=_token_count(usage),
                latency_ms=sum(
                    int(item.duration_ms)
                    for item in turns
                    if getattr(item, "duration_ms", None) is not None
                ),
                usage=_usage_metrics(usage),
            )
            return StructuredRoleResult(payload=payload, receipt=receipt)
        finally:
            _close_client_safely(client)

    def _failed_call(
        self,
        request: StructuredRoleRequest,
        *,
        thread_id: str,
        turns: list[Any],
        message: str,
        input_digest: str,
        output_schema_digest: str,
        inventory_digest: str,
        schema_recovery_attempts: int = 0,
    ) -> StructuredRoleError:
        usage = _merge_usage(turns)
        return StructuredRoleError(
            message,
            receipt=ModelCallReceipt(
                call_id=self._call_id_factory(),
                role=request.role,
                model_id=request.model_id,
                reasoning_effort=request.reasoning_effort,
                inventory_digest=inventory_digest,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                status=ModelCallStatus.FAILED,
                schema_recovery_attempts=schema_recovery_attempts,
                thread_id=thread_id,
                turn_ids=tuple(str(item.id) for item in turns),
                token_count=_token_count(usage),
                latency_ms=sum(
                    int(item.duration_ms)
                    for item in turns
                    if getattr(item, "duration_ms", None) is not None
                ),
                usage=_usage_metrics(usage),
                error_summary=message,
            ),
        )

    def _exception_call(
        self,
        request: StructuredRoleRequest,
        thread_id: str | None,
        turns: list[Any],
        error: Exception,
        *,
        input_digest: str,
        output_schema_digest: str,
        inventory_digest: str,
        schema_recovery_attempts: int = 0,
    ) -> StructuredRoleError:
        status = (
            ModelCallStatus.TIMED_OUT
            if isinstance(error, TimeoutError)
            or "timeout" in type(error).__name__.casefold()
            else ModelCallStatus.FAILED
        )
        usage = _merge_usage(turns)
        detail = str(error).strip().replace("\r", " ").replace("\n", " ")
        if len(detail) > 500:
            detail = detail[:497] + "..."
        message = f"Planner 역할 호출 실패: {type(error).__name__}"
        if detail:
            message += f": {detail}"
        return StructuredRoleError(
            message,
            receipt=ModelCallReceipt(
                call_id=self._call_id_factory(),
                role=request.role,
                model_id=request.model_id,
                reasoning_effort=request.reasoning_effort,
                inventory_digest=inventory_digest,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                status=status,
                schema_recovery_attempts=schema_recovery_attempts,
                thread_id=thread_id,
                turn_ids=tuple(str(item.id) for item in turns),
                token_count=_token_count(usage),
                latency_ms=sum(
                    int(item.duration_ms)
                    for item in turns
                    if getattr(item, "duration_ms", None) is not None
                ),
                usage=_usage_metrics(usage),
                error_summary=message,
            ),
        )

    def _preflight_error(
        self,
        request: StructuredRoleRequest,
        *,
        input_digest: str,
        output_schema_digest: str,
        message: str,
    ) -> StructuredRoleError:
        return StructuredRoleError(
            message,
            receipt=ModelCallReceipt(
                call_id=self._call_id_factory(),
                role=request.role,
                model_id=request.model_id,
                reasoning_effort=request.reasoning_effort,
                inventory_digest=request.inventory_digest,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                status=ModelCallStatus.FAILED,
                error_summary=message,
            ),
        )

    def _unavailable_call(
        self,
        request: StructuredRoleRequest,
        *,
        input_digest: str,
        output_schema_digest: str,
        inventory_digest: str,
        message: str,
        thread_id: str | None = None,
        turns: list[Any] | None = None,
    ) -> StructuredRoleError:
        completed_turns = turns or []
        usage = _merge_usage(completed_turns)
        return StructuredRoleError(
            message,
            receipt=ModelCallReceipt(
                call_id=self._call_id_factory(),
                role=request.role,
                model_id=request.model_id,
                reasoning_effort=request.reasoning_effort,
                inventory_digest=inventory_digest,
                input_digest=input_digest,
                output_schema_digest=output_schema_digest,
                status=ModelCallStatus.REQUIRED_MODEL_UNAVAILABLE,
                thread_id=thread_id,
                turn_ids=tuple(str(item.id) for item in completed_turns),
                token_count=_token_count(usage),
                latency_ms=sum(
                    int(item.duration_ms)
                    for item in completed_turns
                    if getattr(item, "duration_ms", None) is not None
                ),
                usage=_usage_metrics(usage),
                error_summary=message,
            ),
        )


def _value(value: Any, name: str, default: Any) -> Any:
    camel = {
        "display_name": "displayName",
        "supported_reasoning_efforts": "supportedReasoningEfforts",
        "is_default": "isDefault",
        "reasoning_effort": "reasoningEffort",
        "next_cursor": "nextCursor",
    }.get(name, name)
    if isinstance(value, dict):
        return value.get(name, value.get(camel, default))
    return getattr(value, name, getattr(value, camel, default))


def _close_client_safely(client: CodexLike) -> None:
    """정상 결과나 본래 호출 예외를 client 정리 오류로 가리지 않는다."""

    try:
        client.close()
    except Exception:
        pass


def _string_value(value: Any) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value)


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        return dumped if isinstance(dumped, dict) else {}
    return value if isinstance(value, dict) else {}


def _merge_usage(turns: list[Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for turn in turns:
        usage = _model_dump(getattr(turn, "usage", None))
        for key, value in usage.items():
            if isinstance(value, int) and isinstance(merged.get(key, 0), int):
                merged[key] = int(merged.get(key, 0)) + value
            else:
                merged[key] = value
    return merged


def _usage_metrics(usage: dict[str, Any]) -> tuple[ModelUsageMetric, ...]:
    flattened: dict[str, int] = {}

    def visit(prefix: str, value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", prefix) or "metric"
            if not name[0].isalpha():
                name = f"metric.{name}"
            flattened[name] = value
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                visit(child_prefix, child)

    visit("", usage)
    return tuple(
        ModelUsageMetric(name=name, value=value)
        for name, value in sorted(flattened.items())
    )


def _token_count(usage: dict[str, Any]) -> int | None:
    direct = (
        usage.get("total_tokens")
        or usage.get("totalTokens")
        or usage.get("total_token_count")
    )
    if isinstance(direct, int):
        return direct
    nested_total = usage.get("total")
    if isinstance(nested_total, dict):
        nested_direct = (
            nested_total.get("total_tokens")
            or nested_total.get("totalTokens")
            or nested_total.get("total_token_count")
        )
        if isinstance(nested_direct, int):
            return nested_direct
        nested_values = [
            nested_total.get(key)
            for key in ("input_tokens", "inputTokens", "output_tokens", "outputTokens")
        ]
        nested_integer_values = [
            value for value in nested_values if isinstance(value, int)
        ]
        if nested_integer_values:
            return sum(nested_integer_values)
    keys = (
        "input_tokens",
        "inputTokens",
        "output_tokens",
        "outputTokens",
    )
    values = [usage.get(key) for key in keys]
    integer_values = [value for value in values if isinstance(value, int)]
    return sum(integer_values) if integer_values else None


def _decode_structured_output(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredRoleError("Planner 역할이 유효한 JSON을 반환하지 않았습니다.") from exc
    if not isinstance(payload, dict):
        raise StructuredRoleError("Planner 역할 출력의 최상위 값은 object여야 합니다.")
    _validate_schema_value(payload, schema, path="$")
    return payload


def _apply_typed_validator(
    payload: dict[str, Any],
    validator: Callable[[dict[str, Any]], Any] | None,
) -> None:
    if validator is None:
        return
    try:
        validator(payload)
    except (ValidationError, ValueError, TypeError) as exc:
        detail = str(exc).strip().replace("\r", " ").replace("\n", " | ")
        if len(detail) > 3000:
            detail = detail[:2997] + "..."
        raise StructuredRoleError(
            "Planner 역할 출력이 typed 계약을 충족하지 못했습니다: "
            f"{type(exc).__name__}: {detail}"
        ) from exc


def _validate_schema_value(value: Any, schema: dict[str, Any], *, path: str) -> None:
    """SDK structured output 뒤의 최소 방어 검증.

    전체 JSON Schema 구현이 아니라 Planner가 발행하는 object/array/primitive,
    required, enum 계약만 검사한다. 이후 typed Pydantic adapter가 최종 검증한다.
    """

    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected_type, str) and expected_type in type_matches:
        if not type_matches[expected_type]:
            raise StructuredRoleError(f"{path} 값이 JSON Schema type={expected_type}과 다릅니다.")
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        raise StructuredRoleError(f"{path} 값이 허용된 enum에 없습니다.")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [name for name in required if name not in value]
            if missing:
                raise StructuredRoleError(f"{path}에 필수 필드가 없습니다: {missing}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, child_schema in properties.items():
                if name in value and isinstance(child_schema, dict):
                    _validate_schema_value(value[name], child_schema, path=f"{path}.{name}")
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, path=f"{path}[{index}]")
