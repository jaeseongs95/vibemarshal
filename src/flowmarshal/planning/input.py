from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, model_validator

from .domain import (
    ContextSourceKind,
    ContextSourceSpec,
    PlanningLimits,
    PlanningModel,
    PlannerContractError,
    RequestSpec,
    RequirementSpec,
    ValidationCapability,
)


MAX_CONTEXT_SOURCE_BYTES = 2_000_000


class ContextFileRegistration(PlanningModel):
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    kind: ContextSourceKind
    path: str = Field(min_length=1, max_length=2000)
    purpose: str = Field(min_length=1, max_length=2000)
    required_for_all_work_items: bool = False


class RequestSpecAssemblyInput(PlanningModel):
    project_id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    project_name: str = Field(min_length=1, max_length=120)
    project_root: str = Field(min_length=3, max_length=2000)
    project_description: str = Field(min_length=1, max_length=5000)
    project_instruction_path: str | None = Field(default=None, max_length=2000)
    user_request: str = Field(min_length=1, max_length=50_000)
    request_summary: str = Field(min_length=1, max_length=5000)
    parent_revision_id: str | None = Field(
        default=None, pattern=r"^revision_[0-9a-f]{32}$"
    )
    requirements: tuple[RequirementSpec, ...] = Field(min_length=1)
    registered_context_sources: tuple[ContextFileRegistration, ...] = ()
    available_validations: tuple[ValidationCapability, ...] = Field(min_length=1)
    product_capabilities: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    planning_limits: PlanningLimits = Field(default_factory=PlanningLimits)

    @model_validator(mode="after")
    def registered_source_ids_are_unique(self) -> "RequestSpecAssemblyInput":
        identifiers = [item.source_id for item in self.registered_context_sources]
        if "project-agents" in identifiers:
            raise ValueError("project-agents는 자동 등록되는 예약 source_id입니다.")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("등록 ContextSource ID가 중복됐습니다.")
        return self


def _canonical_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=True))))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except ValueError:
        return False


def _read_context(registration: ContextFileRegistration) -> ContextSourceSpec:
    path = Path(registration.path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PlannerContractError(
            "CONTEXT_SOURCE_MISSING",
            f"등록된 ContextSource를 찾을 수 없습니다: {path}",
        ) from exc
    if not resolved.is_file():
        raise PlannerContractError(
            "CONTEXT_SOURCE_NOT_FILE",
            f"ContextSource는 파일이어야 합니다: {resolved}",
        )
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise PlannerContractError(
            "CONTEXT_SOURCE_READ_FAILED",
            f"ContextSource를 읽지 못했습니다: {resolved}",
        ) from exc
    if len(payload) > MAX_CONTEXT_SOURCE_BYTES:
        raise PlannerContractError(
            "CONTEXT_SOURCE_TOO_LARGE",
            f"ContextSource가 {MAX_CONTEXT_SOURCE_BYTES} byte 상한을 넘었습니다: {resolved}",
        )
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlannerContractError(
            "CONTEXT_SOURCE_NOT_UTF8",
            f"ContextSource가 UTF-8 텍스트가 아닙니다: {resolved}",
        ) from exc
    if len(content) > 500_000:
        raise PlannerContractError(
            "CONTEXT_SOURCE_TOO_LARGE",
            f"ContextSource가 500000자 상한을 넘었습니다: {resolved}",
        )
    return ContextSourceSpec.from_text(
        source_id=registration.source_id,
        kind=registration.kind,
        path=_canonical_path(resolved),
        purpose=registration.purpose,
        content=content,
        required_for_all_work_items=registration.required_for_all_work_items,
    )


class RequestSpecAssembler:
    """정확히 등록된 파일과 프로젝트 AGENTS.md만 읽는 신뢰 입력 계층."""

    def build(self, source: RequestSpecAssemblyInput) -> RequestSpec:
        root = Path(source.project_root)
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise PlannerContractError(
                "PROJECT_ROOT_MISSING", f"프로젝트 root를 찾을 수 없습니다: {root}"
            ) from exc
        if not resolved_root.is_dir():
            raise PlannerContractError(
                "PROJECT_ROOT_NOT_DIRECTORY",
                f"프로젝트 root는 디렉터리여야 합니다: {resolved_root}",
            )

        instruction_path = (
            Path(source.project_instruction_path)
            if source.project_instruction_path is not None
            else resolved_root / "AGENTS.md"
        )
        if not instruction_path.is_file():
            raise PlannerContractError(
                "PROJECT_INSTRUCTIONS_MISSING",
                f"Planner 필수 입력인 프로젝트 AGENTS.md가 없습니다: {instruction_path}",
            )
        if not _is_within(instruction_path, resolved_root):
            raise PlannerContractError(
                "PROJECT_INSTRUCTIONS_OUTSIDE_ROOT",
                "프로젝트 instruction 파일은 project root 안에 있어야 합니다.",
            )
        registrations = (
            ContextFileRegistration(
                source_id="project-agents",
                kind=ContextSourceKind.PROJECT_INSTRUCTIONS,
                path=str(instruction_path),
                purpose="모든 WorkItem에 적용되는 프로젝트 지침",
                required_for_all_work_items=True,
            ),
            *source.registered_context_sources,
        )
        paths: set[str] = set()
        for registration in registrations:
            try:
                resolved = Path(registration.path).resolve(strict=True)
            except OSError as exc:
                raise PlannerContractError(
                    "CONTEXT_SOURCE_MISSING",
                    f"등록된 ContextSource를 찾을 수 없습니다: {registration.path}",
                ) from exc
            key = os.path.normcase(os.path.normpath(str(resolved)))
            if key in paths:
                raise PlannerContractError(
                    "CONTEXT_SOURCE_DUPLICATE_PATH",
                    f"같은 ContextSource path가 중복 등록됐습니다: {resolved}",
                )
            paths.add(key)
            if registration.kind in {
                ContextSourceKind.PROJECT_INSTRUCTIONS,
                ContextSourceKind.PROJECT_FILE,
            } and not _is_within(resolved, resolved_root):
                raise PlannerContractError(
                    "PROJECT_CONTEXT_OUTSIDE_ROOT",
                    f"project context로 등록된 파일이 project root 밖에 있습니다: {resolved}",
                )

        contexts = tuple(_read_context(registration) for registration in registrations)
        return RequestSpec(
            project_id=source.project_id,
            project_name=source.project_name,
            project_root=_canonical_path(resolved_root),
            project_description=source.project_description,
            user_request=source.user_request,
            request_summary=source.request_summary,
            parent_revision_id=source.parent_revision_id,
            requirements=source.requirements,
            context_sources=contexts,
            available_validations=source.available_validations,
            product_capabilities=source.product_capabilities,
            out_of_scope=source.out_of_scope,
            planning_limits=source.planning_limits,
        )
