from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import model_validator

from ..canonical import sha256_digest
from .domain import (
    EngineModel,
    ModelAssignmentContract,
    ResolvedRoleAssignment,
    RoleAssignmentPolicy,
)


from .model_lock import (
    ModelCapability, ModelInventory, ModelChoice, OperationalBinding,
    WORKER_CAPABILITIES, ROLE_CAPABILITIES, bind_models, role_lock,
)


class ModelInventoryPort(Protocol):
    def list_models(self) -> ModelInventory: ...


class RoleModelBinding(ModelChoice):
    allowed_fallbacks: tuple[ModelChoice, ...] = ()

    @model_validator(mode="after")
    def explicit_fallbacks(self):
        role_lock("configured", self.model, self.effort, self.allowed_fallbacks)
        return self


class EngineRoleConfiguration(EngineModel):
    """호출자가 주입하는 실제 역할별 model/effort 고정 설정."""

    normalizer: RoleModelBinding
    skeleton_generator: RoleModelBinding
    plan_expander: RoleModelBinding
    general_reviewer: RoleModelBinding
    critical_reviewer: RoleModelBinding
    executor: RoleModelBinding
    validator: RoleModelBinding

    @model_validator(mode="after")
    def executor_and_validator_are_independent(self) -> "EngineRoleConfiguration":
        if (self.executor.model, self.executor.effort) == (
            self.validator.model,
            self.validator.effort,
        ):
            raise ValueError("실행과 독립 validation 역할 binding이 동일합니다.")
        return self

    @property
    def configuration_digest(self) -> str:
        return sha256_digest(self)

    def binding_for(self, role: str) -> RoleModelBinding:
        try:
            return {
                "normalizer": self.normalizer,
                "skeleton_generator": self.skeleton_generator,
                "plan_expander": self.plan_expander,
                "general_reviewer": self.general_reviewer,
                "critical_reviewer": self.critical_reviewer,
                "executor": self.executor,
                "validator": self.validator,
            }[role]
        except KeyError as error:
            raise AssignmentResolutionError(f"알 수 없는 Engine 역할입니다: {role}") from error

    def operational_binding(self, inventory: ModelInventory) -> OperationalBinding:
        return bind_models(inventory, tuple(
            role_lock(name, self.binding_for(name).model, self.binding_for(name).effort,
                      self.binding_for(name).allowed_fallbacks)
            for name in type(self).model_fields
        ))

    def validate_inventory(self, inventory: ModelInventory) -> None:
        missing = [
            role
            for role in (
                "normalizer",
                "skeleton_generator",
                "plan_expander",
                "general_reviewer",
                "critical_reviewer",
                "executor",
                "validator",
            )
            if not inventory.supports(
                self.binding_for(role).model,
                self.binding_for(role).effort,
            )
        ]
        if missing:
            raise AssignmentResolutionError(
                "model/list에 정확한 역할 binding이 없습니다: " + ", ".join(missing)
            )


class AssignmentResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssignmentResolver:
    """계약에 열거된 model/effort만 현재 inventory에 맞춰 해석한다."""

    def resolve_policy(
        self,
        policy: RoleAssignmentPolicy,
        inventory: ModelInventory,
    ) -> ResolvedRoleAssignment:
        if not inventory.supports(policy.preferred_model, policy.preferred_effort):
            raise AssignmentResolutionError(
                f"MODEL_BINDING_UNAVAILABLE: {policy.role}의 명시적 새 binding과 Attempt가 필요합니다."
            )
        try:
            binding = bind_models(
                inventory, (role_lock(policy.role, policy.preferred_model, policy.preferred_effort,
                                      policy.allowed_fallbacks),),
                required_capabilities=(ROLE_CAPABILITIES if policy.role == "validator" else WORKER_CAPABILITIES),
            )
        except ValueError as error:
            raise AssignmentResolutionError(str(error)) from error
        return ResolvedRoleAssignment(
            role=policy.role, model=policy.preferred_model, effort=policy.preferred_effort,
            inventory_digest=inventory.inventory_digest, fallback_used=False, operational_binding=binding,
        )

    def resolve_contract(
        self,
        contract: ModelAssignmentContract,
        inventory: ModelInventory,
    ) -> tuple[ResolvedRoleAssignment, ResolvedRoleAssignment | None]:
        executor = self.resolve_policy(contract.executor, inventory)
        validator = (
            self.resolve_policy(contract.validator, inventory)
            if contract.validator is not None
            else None
        )
        if contract.independence_required and validator is not None:
            if (executor.model, executor.effort) == (validator.model, validator.effort):
                raise AssignmentResolutionError(
                    "독립 검사가 필요한데 실행·검사 배정이 동일합니다."
                )
        return executor, validator
