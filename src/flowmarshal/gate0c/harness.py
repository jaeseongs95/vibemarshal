from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ..canonical import sha256_digest
from ..context import RuntimeRole, UntrustedDataBlock, UntrustedSourceKind
from ..path_policy import PathInspection, PathPolicyError, inspect_resource, revalidate_unchanged


class HarnessContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class AttackTemplate(StrictFrozenModel):
    attack_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,80}$")
    text_template: str = Field(min_length=1, max_length=10_000)
    expected_invariant: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,100}$")


class AttackCase(StrictFrozenModel):
    case_id: str
    attack_id: str
    source_kind: UntrustedSourceKind
    content: str
    content_digest: str
    expected_invariant: str


class AttackCorpus(StrictFrozenModel):
    schema_version: str
    cases: tuple[AttackCase, ...]

    @property
    def digest(self) -> str:
        return sha256_digest(self)


class InvariantSnapshot(StrictFrozenModel):
    label: str
    inspection: PathInspection

    @property
    def digest(self) -> str:
        return self.inspection.snapshot_digest


_SOURCE_EXTENSIONS = {
    UntrustedSourceKind.DOCUMENT: ".txt",
    UntrustedSourceKind.SOURCE: ".py",
    UntrustedSourceKind.TOOL_OUTPUT: ".txt",
}


def load_attack_corpus(
    path: Path,
    *,
    substitutions: Mapping[str, str],
) -> AttackCorpus:
    document = json.loads(path.read_text(encoding="utf-8"))
    if set(document) != {"schema_version", "sources", "attacks"}:
        raise HarnessContractError(
            "ATTACK_CORPUS_SCHEMA_INVALID", "공격 corpus 최상위 field가 계약과 다릅니다."
        )
    if document["schema_version"] != "1.0":
        raise HarnessContractError("ATTACK_CORPUS_SCHEMA_INVALID", "지원하지 않는 corpus revision입니다.")
    try:
        sources = tuple(UntrustedSourceKind(value) for value in document["sources"])
        templates = tuple(AttackTemplate.model_validate(item) for item in document["attacks"])
    except (TypeError, ValueError) as error:
        raise HarnessContractError(
            "ATTACK_CORPUS_SCHEMA_INVALID", f"공격 corpus 항목이 유효하지 않습니다: {error}"
        ) from error
    if set(sources) != set(UntrustedSourceKind) or len(sources) != len(set(sources)):
        raise HarnessContractError(
            "ATTACK_CORPUS_SCHEMA_INVALID", "document/source/tool_output 경로가 각각 하나씩 필요합니다."
        )
    attack_ids = [item.attack_id for item in templates]
    if len(attack_ids) != len(set(attack_ids)):
        raise HarnessContractError("ATTACK_CORPUS_SCHEMA_INVALID", "attack ID가 중복됐습니다.")

    cases: list[AttackCase] = []
    for template in templates:
        content = template.text_template
        placeholders = set()
        for name in substitutions:
            marker = "{{" + name + "}}"
            if marker in content:
                placeholders.add(name)
                content = content.replace(marker, substitutions[name])
        if "{{" in content or "}}" in content:
            raise HarnessContractError(
                "ATTACK_CORPUS_SUBSTITUTION_MISSING",
                f"치환되지 않은 placeholder가 있습니다: {template.attack_id}",
            )
        for source in sources:
            case_id = f"{source.value}:{template.attack_id}"
            cases.append(
                AttackCase(
                    case_id=case_id,
                    attack_id=template.attack_id,
                    source_kind=source,
                    content=content,
                    content_digest=sha256_digest(content),
                    expected_invariant=template.expected_invariant,
                )
            )
    return AttackCorpus(
        schema_version="1.0",
        cases=tuple(sorted(cases, key=lambda item: item.case_id)),
    )


def attack_blocks(
    corpus: AttackCorpus,
    *,
    source_resource_ids: Mapping[UntrustedSourceKind, str],
) -> tuple[UntrustedDataBlock, ...]:
    return tuple(
        UntrustedDataBlock(
            block_id="attack_" + case.case_id.replace(":", "_"),
            source_kind=case.source_kind,
            source_resource_id=source_resource_ids[case.source_kind],
            relative_path=f"{case.attack_id}{_SOURCE_EXTENSIONS[case.source_kind]}",
            media_type="text/plain",
            content_digest=case.content_digest,
            content=case.content,
        )
        for case in corpus.cases
    )


def capture_invariants(paths: Mapping[str, Path]) -> tuple[InvariantSnapshot, ...]:
    snapshots: list[InvariantSnapshot] = []
    for label, path in sorted(paths.items()):
        snapshots.append(InvariantSnapshot(label=label, inspection=inspect_resource(path)))
    return tuple(snapshots)


def assert_invariants_unchanged(before: tuple[InvariantSnapshot, ...]) -> None:
    for snapshot in before:
        try:
            revalidate_unchanged(snapshot.inspection)
        except PathPolicyError as error:
            raise HarnessContractError(
                "PROTECTED_INVARIANT_CHANGED",
                f"보호 invariant가 바뀌었습니다: {snapshot.label}: {error}",
            ) from error


def deterministic_attack_matrix(corpus: AttackCorpus) -> dict[str, Any]:
    by_source = {
        source.value: tuple(
            case.case_id for case in corpus.cases if case.source_kind is source
        )
        for source in UntrustedSourceKind
    }
    invariants = tuple(sorted({case.expected_invariant for case in corpus.cases}))
    return {
        "schema_version": "1.0",
        "case_count": len(corpus.cases),
        "corpus_digest": corpus.digest,
        "case_ids": tuple(case.case_id for case in corpus.cases),
        "by_source": by_source,
        "expected_invariants": invariants,
    }
