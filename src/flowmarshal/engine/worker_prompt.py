from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..canonical import canonical_json
from .context import PromptAssembler, PromptBundle, read_context_fragment
from .domain import ProjectProfileDefinition, PromptBinding, TaskContract, TaskExecutionSpecDefinition


class PromptArtifactError(ValueError):
    pass


def assemble_worker_prompt(
    *, task: TaskContract, definition: TaskExecutionSpecDefinition,
    profile: ProjectProfileDefinition, root: Path,
) -> PromptBundle:
    """자기참조 binding과 파생 digest를 제외한 실행 입력을 조립한다."""
    projection = definition.model_dump(mode="json")
    projection["context_manifest"].pop("prompt_binding")
    return PromptAssembler().assemble(
        static_policy=(
            "활성 PlanContract가 지정한 Task 하나만 수행한다. Core 원장을 직접 변경하거나 "
            "다음 Task를 선택하지 않는다."
        ),
        project_policy=canonical_json(profile),
        stage_schema=(
            "실제 변경과 실행 결과를 보고하되 완료 여부는 주장하지 말고, "
            "검증 가능한 파일·명령 evidence 위치를 제시한다."
        ),
        task_instruction="TaskContract:\n" + canonical_json(task)
        + "\nExecutionSpec 운영 상세:\n" + canonical_json(projection),
        reference_blocks=(
            (f"{fragment.source_ref}#{fragment.selector}", read_context_fragment(root, fragment))
            for fragment in definition.context_manifest.fragments
        ),
    )


class PromptArtifactStore:
    """완성된 임시 파일을 덮어쓰기 없는 원자적 link로 게시한다."""

    def __init__(self, artifact_root: Path) -> None:
        self.root = artifact_root / "worker-prompts"

    def path_for(self, binding: PromptBinding) -> Path:
        return self.root / (binding.binding_digest.split(":", 1)[1] + ".json")

    def load(self, binding: PromptBinding) -> PromptBundle:
        try:
            bundle = PromptBundle.model_validate_json(self.path_for(binding).read_bytes())
        except (OSError, ValueError) as error:
            raise PromptArtifactError("PROMPT_ARTIFACT_INVALID: 본문 누락 또는 segment 변조") from error
        if bundle.binding != binding:
            raise PromptArtifactError("PROMPT_ARTIFACT_INVALID: ExecutionSpec binding 불일치")
        return bundle

    def put(self, bundle: PromptBundle) -> Path:
        # model_copy/model_construct로 validation을 우회한 값도 저장하지 않는다.
        bundle = PromptBundle.model_validate_json(bundle.model_dump_json())
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.path_for(bundle.binding)
        descriptor, name = tempfile.mkstemp(prefix=".prompt-", suffix=".tmp", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json(bundle).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                # 같은 key는 검증 후 재사용하며 손상된 기존 파일은 복구·덮어쓰지 않는다.
                pass
            if self.load(bundle.binding) != bundle:
                raise PromptArtifactError("PROMPT_ARTIFACT_INVALID: 같은 binding의 본문 충돌")
        finally:
            temporary.unlink(missing_ok=True)
        return destination
