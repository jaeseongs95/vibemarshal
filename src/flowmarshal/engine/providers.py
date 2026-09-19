"""역할·Worker 호출 provider(Codex App Server 또는 Claude Code CLI) 선택."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import model_validator

from ..canonical import sha256_bytes
from .domain import EngineModel
from .runtime import CodexAppServerRuntime, CodexProjectBinding, CodexRuntimePort, RuntimePolicyError


RuntimeProviderName = Literal["codex", "claude"]


class RuntimeProviderSelection(EngineModel):
    """호출자가 명시한 provider와 그 실행 입력. 기본값은 기존 Codex provider다."""

    provider: RuntimeProviderName = "codex"
    codex_bin: str | None = None
    claude_bin: str | None = None
    claude_model_catalog: str | None = None
    claude_state_root: str | None = None
    claude_setting_sources: str = ""

    @model_validator(mode="after")
    def provider_inputs_match(self) -> "RuntimeProviderSelection":
        claude_inputs = (self.claude_bin, self.claude_model_catalog, self.claude_state_root)
        if self.provider == "codex" and any(value is not None for value in claude_inputs):
            raise ValueError("RUNTIME_PROVIDER_INPUT_MISMATCH: Codex provider에 Claude 입력을 줄 수 없습니다.")
        if self.provider == "claude":
            if self.codex_bin is not None:
                raise ValueError("RUNTIME_PROVIDER_INPUT_MISMATCH: Claude provider에 Codex 입력을 줄 수 없습니다.")
            if self.claude_model_catalog is None:
                raise ValueError(
                    "CLAUDE_MODEL_CATALOG_REQUIRED: Claude provider에는 --claude-model-catalog가 필요합니다."
                )
        return self

    def metadata(self) -> dict[str, Any]:
        """run metadata에 남길 provider 식별 정보. 카탈로그는 원문 digest로 결속한다."""
        document: dict[str, Any] = {"provider": self.provider}
        if self.provider == "codex":
            document["codex_bin"] = (
                None if self.codex_bin is None else str(Path(self.codex_bin).resolve())
            )
            return document
        catalog = Path(self.claude_model_catalog or "").resolve(strict=True)
        document.update(
            {
                "claude_bin": None if self.claude_bin is None else str(Path(self.claude_bin).resolve()),
                "claude_model_catalog": str(catalog),
                "claude_model_catalog_sha256": sha256_bytes(catalog.read_bytes()),
                "claude_setting_sources": self.claude_setting_sources,
            }
        )
        return document


def provider_run_metadata(selection: RuntimeProviderSelection | None) -> dict[str, Any]:
    """Claude run metadata에만 provider 식별 정보를 더한다. Codex run digest는 바꾸지 않는다."""
    if selection is None or selection.provider == "codex":
        return {}
    return {"runtime_provider": selection.metadata()}


def selection_from_run_metadata(metadata: dict[str, Any]) -> RuntimeProviderSelection | None:
    """저장 run metadata의 provider를 그대로 복원한다. 카탈로그 원문이 바뀌면 거부한다."""
    document = metadata.get("runtime_provider")
    if document is None:
        return None
    if not isinstance(document, dict) or document.get("provider") != "claude":
        raise RuntimePolicyError("RUNTIME_PROVIDER_METADATA_INVALID: run provider 기록이 유효하지 않습니다.")
    catalog = Path(document["claude_model_catalog"])
    if not catalog.is_file() or sha256_bytes(catalog.read_bytes()) != document.get("claude_model_catalog_sha256"):
        raise RuntimePolicyError(
            "RUNTIME_PROVIDER_CATALOG_CHANGED: run이 결속한 Claude 모델 카탈로그가 바뀌었습니다."
        )
    return RuntimeProviderSelection(
        provider="claude",
        claude_bin=document.get("claude_bin"),
        claude_model_catalog=str(catalog),
        claude_setting_sources=document.get("claude_setting_sources", ""),
    )


def open_runtime(
    selection: RuntimeProviderSelection,
    *,
    project_binding: CodexProjectBinding | None = None,
) -> CodexRuntimePort:
    """선택한 provider의 runtime을 연다. provider를 묵시적으로 바꾸지 않는다."""
    if selection.provider == "codex":
        if project_binding is None:
            return CodexAppServerRuntime(codex_bin=selection.codex_bin)
        return CodexAppServerRuntime(codex_bin=selection.codex_bin, project_binding=project_binding)
    if project_binding is not None:
        raise RuntimePolicyError(
            "PROJECT_BINDING_UNSUPPORTED: Claude provider는 Codex App Server project 결속을 쓰지 않습니다."
        )
    from .claude_runtime import ClaudeCodeRuntime, ClaudeModelCatalog

    return ClaudeCodeRuntime(
        model_catalog=ClaudeModelCatalog.load(selection.claude_model_catalog or ""),
        claude_bin=selection.claude_bin,
        state_root=selection.claude_state_root,
        setting_sources=selection.claude_setting_sources,
    )


def open_harness_runtime(
    selection: RuntimeProviderSelection | None,
    *,
    codex_factory: Callable[..., CodexRuntimePort],
    codex_bin: Path | str | None,
    project_binding: CodexProjectBinding | None,
    default_state_root: Path,
) -> CodexRuntimePort:
    """qualification harness용 runtime을 연다.

    Codex는 호출 모듈의 factory로 기존과 같게 만든다. Claude는 재시작 관측이 같은
    thread 기록을 읽도록 고정 state root를 쓴다.
    """
    if selection is None or selection.provider == "codex":
        if selection is not None and selection.codex_bin is not None and codex_bin is not None and (
            Path(selection.codex_bin).resolve() != Path(codex_bin).resolve()
        ):
            raise RuntimePolicyError("RUNTIME_PROVIDER_INPUT_MISMATCH: codex_bin이 서로 다릅니다.")
        return codex_factory(codex_bin=codex_bin, project_binding=project_binding)
    if codex_bin is not None:
        raise RuntimePolicyError("RUNTIME_PROVIDER_INPUT_MISMATCH: Claude provider에 codex_bin을 줄 수 없습니다.")
    if selection.claude_state_root is None:
        selection = selection.model_copy(update={"claude_state_root": str(default_state_root)})
    return open_runtime(selection, project_binding=project_binding)


def add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider", choices=("codex", "claude"), default="codex",
        help="역할·Worker 호출 provider (기본 codex)",
    )
    parser.add_argument("--claude-bin", help="Claude Code CLI 실행 파일 (기본 PATH의 claude)")
    parser.add_argument(
        "--claude-model-catalog",
        help="Claude provider가 받을 model/effort 카탈로그 JSON (flowmarshal-claude-model-catalog-v1)",
    )
    parser.add_argument("--claude-state-root", help="Claude thread 지침·상태를 둘 디렉터리")
    parser.add_argument(
        "--claude-setting-sources", default="",
        help="Claude CLI --setting-sources 값 (기본 빈 값: user/project/local 설정을 읽지 않음)",
    )


def selection_from_arguments(
    arguments: argparse.Namespace, *, default_state_root: Path | str | None = None,
) -> RuntimeProviderSelection:
    provider = getattr(arguments, "provider", "codex") or "codex"
    if provider == "codex":
        return RuntimeProviderSelection(
            provider="codex",
            codex_bin=getattr(arguments, "codex_bin", None),
            claude_bin=getattr(arguments, "claude_bin", None),
            claude_model_catalog=getattr(arguments, "claude_model_catalog", None),
            claude_state_root=getattr(arguments, "claude_state_root", None),
        )
    state_root = getattr(arguments, "claude_state_root", None)
    if state_root is None and default_state_root is not None:
        state_root = str(Path(default_state_root))
    return RuntimeProviderSelection(
        provider="claude",
        codex_bin=getattr(arguments, "codex_bin", None),
        claude_bin=getattr(arguments, "claude_bin", None),
        claude_model_catalog=getattr(arguments, "claude_model_catalog", None),
        claude_state_root=state_root,
        claude_setting_sources=getattr(arguments, "claude_setting_sources", "") or "",
    )


__all__ = (
    "RuntimeProviderSelection",
    "add_provider_arguments",
    "open_harness_runtime",
    "open_runtime",
    "provider_run_metadata",
    "selection_from_arguments",
    "selection_from_run_metadata",
)
