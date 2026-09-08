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
        *,
        selection: ModelChoice | None = None,
    ) -> ResolvedRoleAssignment:
        preferred = ModelChoice(model=policy.preferred_model, effort=policy.preferred_effort)
        envelope = (preferred, *(ModelChoice(model=item.model, effort=item.effort)
                                  for item in policy.allowed_fallbacks))
        selected = selection or preferred
        selected_key = (selected.model, selected.effort)
        if selected_key not in {(item.model, item.effort) for item in envelope}:
            raise AssignmentResolutionError(
                f"PLAN_MODEL_REVISION_REQUIRED: {policy.role} 선택이 활성 Plan의 model envelope 밖입니다."
            )
        if not inventory.supports(selected.model, selected.effort):
            raise AssignmentResolutionError(
                f"MODEL_BINDING_UNAVAILABLE: {policy.role}의 명시적 새 binding과 Attempt가 필요합니다."
            )
        alternatives = tuple(item for item in envelope
                             if (item.model, item.effort) != selected_key)
        try:
            binding = bind_models(
                inventory, (role_lock(policy.role, selected.model, selected.effort,
                                      alternatives),),
                required_capabilities=(ROLE_CAPABILITIES if policy.role == "validator" else WORKER_CAPABILITIES),
            )
        except ValueError as error:
            raise AssignmentResolutionError(str(error)) from error
        return ResolvedRoleAssignment(
            role=policy.role, model=selected.model, effort=selected.effort,
            inventory_digest=inventory.inventory_digest,
            fallback_used=selected_key != (preferred.model, preferred.effort),
            operational_binding=binding,
        )

    def resolve_contract(
        self,
        contract: ModelAssignmentContract,
        inventory: ModelInventory,
        *,
        executor_selection: ModelChoice | None = None,
        validator_selection: ModelChoice | None = None,
    ) -> tuple[ResolvedRoleAssignment, ResolvedRoleAssignment | None]:
        executor = self.resolve_policy(
            contract.executor, inventory, selection=executor_selection,
        )
        if contract.validator is None and validator_selection is not None:
            raise AssignmentResolutionError(
                "PLAN_MODEL_REVISION_REQUIRED: validator가 없는 Task에 validator를 선택할 수 없습니다."
            )
        validator = (
            self.resolve_policy(
                contract.validator, inventory, selection=validator_selection,
            )
            if contract.validator is not None
            else None
        )
        return executor, validator
