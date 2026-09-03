from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .domain import (
    EngineModel,
    ModelAssignmentContract,
    ResolvedRoleAssignment,
    RoleAssignmentPolicy,
)


class ModelCapability(EngineModel):
    model: str = Field(min_length=1, max_length=200)
    supported_efforts: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def efforts_are_unique(self) -> "ModelCapability":
        if len(self.supported_efforts) != len(set(self.supported_efforts)):
            raise ValueError("model reasoning effort가 중복됐습니다.")
        return self


class ModelInventory(EngineModel):
    source: str = Field(min_length=1, max_length=500)
    models: tuple[ModelCapability, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def models_are_unique(self) -> "ModelInventory":
        names = tuple(item.model for item in self.models)
        if len(names) != len(set(names)):
            raise ValueError("model inventory에 모델이 중복됐습니다.")
        return self

    @property
    def inventory_digest(self) -> str:
        return sha256_digest(self)

    def supports(self, model: str, effort: str) -> bool:
        return any(
            item.model == model and effort in item.supported_efforts for item in self.models
        )


class ModelInventoryPort(Protocol):
    def list_models(self) -> ModelInventory: ...


class RoleModelBinding(EngineModel):
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)


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
        if inventory.supports(policy.preferred_model, policy.preferred_effort):
            return ResolvedRoleAssignment(
                role=policy.role,
                model=policy.preferred_model,
                effort=policy.preferred_effort,
                inventory_digest=inventory.inventory_digest,
                fallback_used=False,
            )
        for fallback in policy.allowed_fallbacks:
            if inventory.supports(fallback.model, fallback.effort):
                return ResolvedRoleAssignment(
                    role=policy.role,
                    model=fallback.model,
                    effort=fallback.effort,
                    inventory_digest=inventory.inventory_digest,
                    fallback_used=True,
                )
        raise AssignmentResolutionError(
            f"역할 {policy.role!r}에 허용된 model/effort가 현재 inventory에 없습니다."
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
