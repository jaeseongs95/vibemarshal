"""전체 inventory 감사 증거와 실행에 필요한 v2 projection을 분리한다."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator

from ..canonical import sha256_digest


LOCK_FORMAT = "flowmarshal-model-lock-v2"
DIGEST = r"^sha256:[0-9a-f]{64}$"
ModelId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
ROLE_CAPABILITIES = ("local_execution", "structured_output", "thread_read", "thread_start", "turn_start")
WORKER_CAPABILITIES = ("local_execution", "thread_read", "thread_resume", "thread_start", "turn_start")
ALL_CAPABILITIES = tuple(sorted(set(ROLE_CAPABILITIES + WORKER_CAPABILITIES)))


class LockModel(BaseModel):
    # inventory에서는 공백 제거·문자열 coercion·중복 제거를 허용하지 않는다.
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True,
                              revalidate_instances="always", str_strip_whitespace=False)


class ModelChoice(LockModel):
    model: ModelId
    effort: Effort


class ModelCapability(LockModel):
    model: ModelId
    supported_efforts: tuple[Effort, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_efforts(self):
        if len(self.supported_efforts) != len(set(self.supported_efforts)):
            raise ValueError("model reasoning effort가 중복됐습니다.")
        return self


class RuntimeCapability(LockModel):
    name: Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$")]
    contract: Annotated[str, StringConstraints(strict=True, min_length=1)]


# 이 adapter가 구현하고 executable identity와 함께 검증하는 protocol 계약이다.
# 전체 server feature 목록이나 무관한 설정을 실행 잠금에 포함하지 않는다.
RUNTIME_CAPABILITIES = tuple(
    RuntimeCapability(name=name, contract=(":danger-full-access/never" if name == "local_execution"
                                          else "codex-app-server-v2"))
    for name in ALL_CAPABILITIES
)


class ModelInventory(LockModel):
    source: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    models: tuple[ModelCapability, ...] = Field(min_length=1)
    # None/빈 값은 과거 evidence의 읽기만 허용한다. v2 실행 binding 생성에는 필수다.
    executable_digest: str | None = Field(default=None, pattern=DIGEST)
    runtime_capabilities: tuple[RuntimeCapability, ...] = ()
    raw_response: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_complete_inventory(self):
        names = [item.model for item in self.models]
        capabilities = [item.name for item in self.runtime_capabilities]
        if len(names) != len(set(names)) or len(capabilities) != len(set(capabilities)):
            raise ValueError("model inventory 또는 runtime capability가 중복됐습니다.")
        if self.raw_response is not None and parse_inventory_models(self.raw_response) != self.models:
            raise ValueError("MODEL_INVENTORY_OBSERVATION_MISMATCH")
        return self

    @property
    def inventory_digest(self) -> str:
        """provider observation과 adapter capability를 함께 묶는 감사 digest."""
        return sha256_digest(self)

    @property
    def provider_inventory_digest(self) -> str:
        """model/list가 광고한 모델/effort 원문만의 digest."""
        return sha256_digest({
            "source": self.source,
            "models": self.models,
            "raw_response": self.raw_response,
        })

    @property
    def adapter_capability_digest(self) -> str:
        """로컬 adapter/executable이 선언하고 검사한 capability digest."""
        return sha256_digest({
            "executable_digest": self.executable_digest,
            "runtime_capabilities": self.runtime_capabilities,
        })

    @property
    def inventory_provenance(self) -> str:
        """inventory 원문의 출처 label. Claude provider는 호출자가 주입한 카탈로그다."""
        if self.source.startswith("claude-code:configured-catalog@"):
            return "configured_catalog"
        return "model/list"

    def supports(self, model: str, effort: str) -> bool:
        return any(item.model == model and effort in item.supported_efforts for item in self.models)


def _inventory_field(row: dict[str, Any], snake: str, camel: str):
    if snake in row and camel in row:
        raise ValueError("model/list field alias가 중복됐습니다.")
    return row.get(snake, row.get(camel))


def parse_inventory_models(response: dict[str, Any]) -> tuple[ModelCapability, ...]:
    """수신된 모든 행을 검사한다. hidden 행도 버리거나 정규화하지 않는다."""
    if not isinstance(response, dict):
        raise ValueError("model/list 응답은 object여야 합니다.")
    if _inventory_field(response, "next_cursor", "nextCursor") is not None:
        raise ValueError("model/list pagination을 완전히 읽지 못했습니다.")
    rows = response.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("model/list inventory는 비어 있지 않은 배열이어야 합니다.")
    models = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("model/list의 모든 model 행은 object여야 합니다.")
        efforts = _inventory_field(row, "supported_reasoning_efforts", "supportedReasoningEfforts")
        if not isinstance(efforts, list) or not efforts:
            raise ValueError("model/list effort는 비어 있지 않은 배열이어야 합니다.")
        values = []
        for option in efforts:
            if not isinstance(option, dict):
                raise ValueError("model/list effort 행은 object여야 합니다.")
            values.append(_inventory_field(option, "reasoning_effort", "reasoningEffort"))
        models.append(ModelCapability(model=row.get("id"), supported_efforts=tuple(values)))
    if len(models) != len({item.model for item in models}):
        raise ValueError("model inventory에 모델이 중복됐습니다.")
    return tuple(models)


class SupportedChoice(ModelChoice):
    supported: StrictBool


class RoleLock(LockModel):
    role: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=100)]
    selected: SupportedChoice
    allowed_fallbacks: tuple[SupportedChoice, ...] = ()

    @model_validator(mode="after")
    def explicit_envelope(self):
        choices = [(x.model, x.effort) for x in (self.selected, *self.allowed_fallbacks)]
        if len(choices) != len(set(choices)):
            raise ValueError("선택 모델과 fallback envelope에 중복이 있습니다.")
        if not self.selected.supported:
            raise ValueError("MODEL_BINDING_UNAVAILABLE: 명시적 재결속이 필요합니다.")
        return self


class OperationalModelLock(LockModel):
    format: Literal["flowmarshal-model-lock-v2"]
    roles: tuple[RoleLock, ...] = Field(min_length=1)
    executable_digest: str = Field(pattern=DIGEST)
    runtime_capabilities: tuple[RuntimeCapability, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_keys(self):
        for keys in ([x.role for x in self.roles], [x.name for x in self.runtime_capabilities]):
            if keys != sorted(set(keys)):
                raise ValueError("v2 lock의 역할·capability key는 정렬되고 유일해야 합니다.")
        return self

    @property
    def lock_digest(self) -> str:
        return sha256_digest(self)


def project_lock(inventory: ModelInventory, roles: tuple[RoleLock, ...],
                 required_capabilities: tuple[str, ...]) -> OperationalModelLock:
    # model_copy/model_construct나 projection 밖 행으로 검증을 우회할 수 없다.
    inventory = ModelInventory.model_validate(inventory.model_dump(mode="python"))
    if inventory.executable_digest is None:
        raise ValueError("MODEL_LOCK_V2_RUNTIME_IDENTITY_REQUIRED")
    capabilities = {item.name: item for item in inventory.runtime_capabilities}
    if any(name not in capabilities for name in required_capabilities):
        raise ValueError("MODEL_LOCK_RUNTIME_CAPABILITY_UNAVAILABLE")

    def observed(choice: ModelChoice) -> SupportedChoice:
        return SupportedChoice(model=choice.model, effort=choice.effort,
                               supported=inventory.supports(choice.model, choice.effort))

    return OperationalModelLock(
        format=LOCK_FORMAT, executable_digest=inventory.executable_digest,
        runtime_capabilities=tuple(capabilities[name] for name in sorted(set(required_capabilities))),
        roles=tuple(RoleLock(role=role.role, selected=observed(role.selected),
                             allowed_fallbacks=tuple(observed(x) for x in role.allowed_fallbacks))
                    for role in sorted(roles, key=lambda x: x.role)),
    )


class OperationalBinding(LockModel):
    """실행 lock과 해당 lock을 만든 전체 감사 observation의 불변 결속."""
    inventory: ModelInventory
    inventory_digest: str = Field(pattern=DIGEST)
    lock: OperationalModelLock
    lock_digest: str = Field(pattern=DIGEST)

    @model_validator(mode="after")
    def verify_evidence(self):
        if self.inventory_digest != self.inventory.inventory_digest or self.lock_digest != self.lock.lock_digest:
            raise ValueError("MODEL_LOCK_EVIDENCE_DIGEST_MISMATCH")
        actual = project_lock(self.inventory, self.lock.roles,
                              tuple(item.name for item in self.lock.runtime_capabilities))
        if actual != self.lock:
            raise ValueError("MODEL_LOCK_OBSERVATION_MISMATCH")
        return self


def bind_models(inventory: ModelInventory, roles: tuple[RoleLock, ...], *,
                required_capabilities: tuple[str, ...] = ALL_CAPABILITIES) -> OperationalBinding:
    lock = project_lock(inventory, roles, required_capabilities)
    return OperationalBinding(inventory=inventory, inventory_digest=inventory.inventory_digest,
                              lock=lock, lock_digest=lock.lock_digest)


def role_lock(role: str, model: str, effort: str, fallbacks=()) -> RoleLock:
    # supported는 observation 생성 시 전체 검증 뒤 실제 값으로 대조한다.
    return RoleLock(role=role, selected=SupportedChoice(model=model, effort=effort, supported=True),
                    allowed_fallbacks=tuple(SupportedChoice(model=x.model, effort=x.effort, supported=False)
                                            for x in fallbacks))


def verify_binding(binding: OperationalBinding | None, inventory: ModelInventory, *,
                   role: str | None = None, model: str | None = None, effort: str | None = None) -> OperationalBinding:
    if binding is None:
        raise ValueError("MODEL_LOCK_VERSION_UNSUPPORTED: v2 operational binding이 필요합니다.")
    binding = OperationalBinding.model_validate(binding.model_dump(mode="python"))
    if role is not None:
        selected = next((x.selected for x in binding.lock.roles if x.role == role), None)
        if selected is None or (selected.model, selected.effort) != (model, effort):
            raise ValueError("MODEL_LOCK_ROLE_BINDING_MISMATCH")
    current = bind_models(inventory, binding.lock.roles,
                          required_capabilities=tuple(x.name for x in binding.lock.runtime_capabilities))
    if current.lock_digest != binding.lock_digest:
        raise ValueError("MODEL_OR_EXECUTABLE_LOCK_CHANGED")
    return current
