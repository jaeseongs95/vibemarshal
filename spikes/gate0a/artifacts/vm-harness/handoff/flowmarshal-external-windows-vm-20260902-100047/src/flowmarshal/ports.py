from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .domain import CommandEnvelope, Decision, IntentKind, PlanDraft, ProjectDefinition, ResourceDefinition


class Clock(Protocol):
    def now(self) -> str: ...


@dataclass(frozen=True)
class RuntimeReceipt:
    effect_kind: IntentKind
    external_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class RuntimeObservation:
    thread_id: str | None
    turn_id: str | None
    active_turn: bool | None
    terminal_status: str | None
    known_turn_ids: tuple[str, ...]
    payload: dict[str, Any]


class AgentRuntime(Protocol):
    def create_thread(self, *, project_id: str, workspace: Path, title: str) -> RuntimeReceipt: ...

    def start_turn(
        self,
        *,
        thread_id: str,
        workspace: Path,
        prompt: str,
        execution_profile: dict[str, Any] | None = None,
    ) -> RuntimeReceipt: ...

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> RuntimeReceipt: ...

    def observe(self, *, thread_id: str) -> RuntimeObservation: ...


@dataclass(frozen=True)
class EvidenceObject:
    digest: str
    size: int
    path: Path


class EvidenceStore(Protocol):
    def put(self, payload: bytes) -> EvidenceObject: ...

    def verify(self, digest: str, size: int) -> bool: ...


@dataclass(frozen=True)
class AuthorityProof:
    action: str
    target_digest: str
    nonce: str
    issued_at: str
    expires_at: str
    proof: str


class HumanControlAuthority(Protocol):
    def issue(self, action: str, target_digest: str, *, ttl_seconds: int = 300) -> AuthorityProof: ...

    def verify(self, proof: AuthorityProof, action: str, target_digest: str) -> bool: ...


class LedgerTransaction(Protocol):
    def register_project(self, definition: ProjectDefinition, resources: tuple[ResourceDefinition, ...]) -> str: ...

    def import_candidate_plan(self, draft: PlanDraft) -> str: ...

    def record_decision(self, decision: Decision, envelope: CommandEnvelope) -> None: ...


class LedgerStore(Protocol):
    def initialize(self) -> None: ...

    def transaction(self) -> AbstractContextManager[LedgerTransaction]: ...

    def raw_connection(self) -> AbstractContextManager[Any]: ...
