from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from ..canonical import sha256_digest
from .domain import EffectPolicy, EngineModel, GoalOperatingPolicy


class CoreCapabilityError(ValueError):
    """역할 제출물과 Core action의 권한 경계 위반."""


_role_stack: ContextVar[tuple[str, ...]] = ContextVar("flowmarshal_role_stack", default=())


def require_host_execution() -> None:
    if _role_stack.get():
        raise CoreCapabilityError("CORE_CAPABILITY_DENIED: 역할 실행에는 Core 쓰기 권한이 없습니다.")


@contextmanager
def role_execution_scope(role: str) -> Iterator[None]:
    """중첩된 callback도 부모 역할의 제한을 유지한다. OS sandbox가 아니다."""
    token = _role_stack.set((*_role_stack.get(), role))
    try:
        yield
    finally:
        _role_stack.reset(token)


class GoalAuthorizationTarget(EngineModel):
    """console에 표시하고 일회성 승인 handle에 결속하는 전체 Goal 경계."""

    project_id: str
    project_root: str
    goal_id: str
    goal_revision_id: str
    goal_contract_digest: str
    profile_definition_digest: str
    plan_id: str
    plan_revision_id: str
    plan_revision_no: int
    plan_definition_digest: str
    plan_activation_digest: str
    effect_policy: EffectPolicy
    operating_policy: GoalOperatingPolicy
    budget_policies: tuple[str, ...] = ()

    @property
    def target_digest(self) -> str:
        return sha256_digest(self)


class EffectCheckpointTarget(EngineModel):
    """비가역 외부 효과 하나와 현재 ExecutionSpec의 결속."""

    project_id: str
    task_id: str
    effect_id: str
    execution_spec_digest: str
    effect_identity: dict[str, Any]

    @property
    def target_digest(self) -> str:
        return sha256_digest(self)


class _CoreActionCapability:
    """직렬화하거나 문자열로 재구성할 수 없는 host 소유 action handle."""

    __slots__ = ("__weakref__",)

    def __reduce__(self):
        raise TypeError("Core action capability cannot be serialized")


class GoalAuthorizationCapability(_CoreActionCapability):
    __slots__ = ()


class EffectCheckpointCapability(_CoreActionCapability):
    __slots__ = ()


class CoreActionAuthority:
    """신뢰된 host가 보관하는 발급자. 역할 도구/입력에 전달하지 않는다.

    같은 Python 프로세스의 비신뢰 코드를 격리하거나 동일 OS 사용자의
    filesystem 접근을 막는 보안 sandbox로 사용해서는 안 된다.
    """

    def __init__(self) -> None:
        require_host_execution()
        self._issued: dict[_CoreActionCapability, tuple[str, str, str]] = {}

    def issue_goal_authorization(
        self, *, ledger_path: Path | str, target: GoalAuthorizationTarget,
    ) -> GoalAuthorizationCapability:
        require_host_execution()
        if type(target) is not GoalAuthorizationTarget:
            raise CoreCapabilityError("CORE_CAPABILITY_INVALID: Goal 승인 target 타입이 잘못됐습니다.")
        capability = GoalAuthorizationCapability()
        self._issued[capability] = (
            str(Path(ledger_path).resolve()), target.project_id, target.target_digest,
        )
        return capability

    def issue_effect_checkpoint(
        self, *, ledger_path: Path | str, target: EffectCheckpointTarget,
    ) -> EffectCheckpointCapability:
        require_host_execution()
        if type(target) is not EffectCheckpointTarget:
            raise CoreCapabilityError("CORE_CAPABILITY_INVALID: 효과 checkpoint target 타입이 잘못됐습니다.")
        capability = EffectCheckpointCapability()
        self._issued[capability] = (
            str(Path(ledger_path).resolve()), target.project_id, target.target_digest,
        )
        return capability

    def consume_goal_authorization(
        self, capability: object, *, ledger_path: Path | str, target: GoalAuthorizationTarget,
    ) -> None:
        self._consume(
            capability, capability_type=GoalAuthorizationCapability,
            ledger_path=ledger_path, project_id=target.project_id,
            target_digest=target.target_digest,
        )

    def consume_effect_checkpoint(
        self, capability: object, *, ledger_path: Path | str, target: EffectCheckpointTarget,
    ) -> None:
        self._consume(
            capability, capability_type=EffectCheckpointCapability,
            ledger_path=ledger_path, project_id=target.project_id,
            target_digest=target.target_digest,
        )

    def _consume(
        self,
        capability: object,
        *,
        capability_type: type[_CoreActionCapability],
        ledger_path: Path | str,
        project_id: str,
        target_digest: str,
    ) -> None:
        """DB transaction 안의 첫 사용에서 handle을 소모하고 binding을 검증한다."""
        require_host_execution()
        if type(capability) is not capability_type:
            raise CoreCapabilityError("CORE_CAPABILITY_DENIED: 승인 capability 타입이 일치하지 않습니다.")
        issued = self._issued.pop(capability, None)
        expected = (str(Path(ledger_path).resolve()), project_id, target_digest)
        if issued is None:
            raise CoreCapabilityError("CORE_CAPABILITY_DENIED: 이미 소모되거나 발급되지 않은 capability입니다.")
        if issued != expected:
            raise CoreCapabilityError("CORE_CAPABILITY_DENIED: 승인 target binding이 일치하지 않습니다.")


def consume_goal_authorization(
    authority: CoreActionAuthority | None,
    capability: object,
    *,
    ledger_path: Path | str,
    target: GoalAuthorizationTarget,
) -> None:
    require_host_execution()
    if type(authority) is not CoreActionAuthority:
        raise CoreCapabilityError("CORE_CAPABILITY_REQUIRED: 신뢰된 host의 action capability가 필요합니다.")
    authority.consume_goal_authorization(capability, ledger_path=ledger_path, target=target)


def consume_effect_checkpoint(
    authority: CoreActionAuthority | None,
    capability: object,
    *,
    ledger_path: Path | str,
    target: EffectCheckpointTarget,
) -> None:
    require_host_execution()
    if type(authority) is not CoreActionAuthority:
        raise CoreCapabilityError("CORE_CAPABILITY_REQUIRED: 신뢰된 host의 action capability가 필요합니다.")
    authority.consume_effect_checkpoint(capability, ledger_path=ledger_path, target=target)
