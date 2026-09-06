from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, Literal

from pydantic import Field, field_validator, model_validator

from ..canonical import sha256_digest
from .domain import EngineModel


class RoleTimeoutOverride(EngineModel):
    """한 역할의 운영 timeout 변경 근거."""

    role: str = Field(min_length=1, max_length=100)
    timeout_seconds: float = Field(gt=0, le=3600)
    replaces_timeout_seconds: float = Field(default=900, gt=0, le=3600)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def timeout_actually_changes(self) -> "RoleTimeoutOverride":
        if self.timeout_seconds == self.replaces_timeout_seconds:
            raise ValueError("role timeout override는 기존 값과 달라야 합니다.")
        return self


class RoleTimeoutPolicy(EngineModel):
    """호출자가 주입하는 역할별 timeout 운영 설정."""

    format: Literal["flowmarshal-role-timeouts-v1"] = "flowmarshal-role-timeouts-v1"
    default_timeout_seconds: float = Field(default=900, gt=0, le=3600)
    overrides: tuple[RoleTimeoutOverride, ...] = ()

    @field_validator("overrides")
    @classmethod
    def roles_are_unique(
        cls, value: tuple[RoleTimeoutOverride, ...]
    ) -> tuple[RoleTimeoutOverride, ...]:
        roles = tuple(item.role for item in value)
        if len(roles) != len(set(roles)):
            raise ValueError("role timeout override의 역할이 중복됐습니다.")
        return value

    @property
    def policy_digest(self) -> str:
        return sha256_digest(self)

    def timeout_for(self, role: str) -> float:
        override = next((item for item in self.overrides if item.role == role), None)
        return self.default_timeout_seconds if override is None else override.timeout_seconds

    def override_for(self, role: str) -> RoleTimeoutOverride | None:
        return next((item for item in self.overrides if item.role == role), None)


_ACTIVE_ROLE_TIMEOUT_POLICY: ContextVar[RoleTimeoutPolicy | None] = ContextVar(
    "flowmarshal_active_role_timeout_policy", default=None
)


def set_role_timeout_policy(policy: RoleTimeoutPolicy) -> Token[RoleTimeoutPolicy | None]:
    return _ACTIVE_ROLE_TIMEOUT_POLICY.set(policy)


def reset_role_timeout_policy(token: Token[RoleTimeoutPolicy | None]) -> None:
    _ACTIVE_ROLE_TIMEOUT_POLICY.reset(token)


@contextmanager
def use_role_timeout_policy(policy: RoleTimeoutPolicy) -> Iterator[None]:
    token = set_role_timeout_policy(policy)
    try:
        yield
    finally:
        reset_role_timeout_policy(token)


def bind_active_role_timeout(role: str, values: dict[str, Any]) -> dict[str, Any]:
    """활성 운영 설정을 새 RoleCallRequest 생성 인수에 결속한다."""

    policy = _ACTIVE_ROLE_TIMEOUT_POLICY.get()
    if policy is None:
        return values
    timeout = policy.timeout_for(role)
    explicitly_requested = values.get("timeout_seconds")
    if explicitly_requested is not None and explicitly_requested != timeout:
        raise ValueError("ROLE_TIMEOUT_POLICY_REQUEST_MISMATCH")
    values["timeout_seconds"] = timeout
    values["timeout_policy_digest"] = policy.policy_digest
    return values


def verify_role_timeout_binding(
    *, role: str, timeout_seconds: float, timeout_policy_digest: str | None,
    policy: RoleTimeoutPolicy,
) -> None:
    if (
        timeout_policy_digest != policy.policy_digest
        or timeout_seconds != policy.timeout_for(role)
    ):
        raise ValueError("ROLE_TIMEOUT_POLICY_BINDING_MISMATCH")
