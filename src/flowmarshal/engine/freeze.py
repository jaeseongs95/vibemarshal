from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .domain import EngineModel


class FreezeRoot(EngineModel):
    scope: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=2000)
    recursive: bool = True


class FreezeEntry(EngineModel):
    scope: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=2000)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class LegacyFreezeManifest(EngineModel):
    schema_version: str = "1.0"
    roots: tuple[FreezeRoot, ...] = Field(min_length=1)
    entries: tuple[FreezeEntry, ...] = Field(min_length=1)

    @field_validator("roots")
    @classmethod
    def roots_are_unique(cls, value: tuple[FreezeRoot, ...]) -> tuple[FreezeRoot, ...]:
        keys = tuple((item.scope, item.path) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("freeze root가 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def entries_are_unique_and_scoped(self) -> "LegacyFreezeManifest":
        paths = tuple(item.path for item in self.entries)
        if len(paths) != len(set(paths)):
            raise ValueError("freeze manifest path가 중복됐습니다.")
        scopes = {item.scope for item in self.roots}
        unknown = {item.scope for item in self.entries} - scopes
        if unknown:
            raise ValueError(f"freeze entry의 scope root가 없습니다: {sorted(unknown)}")
        return self

    @property
    def manifest_digest(self) -> str:
        return sha256_digest(self)

    @classmethod
    def load(cls, path: Path | str) -> "LegacyFreezeManifest":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class FreezeVerificationReport(EngineModel):
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checked_file_count: int = Field(ge=0)
    missing_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    unexpected_paths: tuple[str, ...] = ()
    passed: bool

    @model_validator(mode="after")
    def pass_matches_findings(self) -> "FreezeVerificationReport":
        expected = not (self.missing_paths or self.changed_paths or self.unexpected_paths)
        if self.passed != expected:
            raise ValueError("freeze verification pass 값이 finding과 다릅니다.")
        return self


def _resolved(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def _display(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return Path("..", path.resolve().relative_to(project_root.resolve().parent)).as_posix()


def _discover(project_root: Path, root: FreezeRoot) -> set[str]:
    directory = _resolved(project_root, root.path)
    if not directory.is_dir():
        return set()
    candidates = directory.rglob("*") if root.recursive else directory.iterdir()
    return {
        _display(project_root, path)
        for path in candidates
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    }


def verify_legacy_freeze(
    manifest: LegacyFreezeManifest,
    *,
    project_root: Path | str,
) -> FreezeVerificationReport:
    root = Path(project_root).resolve(strict=True)
    expected = {item.path: item for item in manifest.entries}
    actual_paths: set[str] = set()
    missing_roots: set[str] = set()
    for freeze_root in manifest.roots:
        directory = _resolved(root, freeze_root.path)
        if not directory.is_dir():
            missing_roots.add(freeze_root.path)
            continue
        actual_paths.update(_discover(root, freeze_root))
    missing = sorted((set(expected) - actual_paths) | missing_roots)
    unexpected = sorted(actual_paths - set(expected))
    changed: list[str] = []
    for path in sorted(set(expected) & actual_paths):
        actual = sha256_bytes(_resolved(root, path).read_bytes())
        if actual != expected[path].sha256:
            changed.append(path)
    return FreezeVerificationReport(
        manifest_digest=manifest.manifest_digest,
        checked_file_count=len(set(expected) & actual_paths),
        missing_paths=tuple(missing),
        changed_paths=tuple(changed),
        unexpected_paths=tuple(unexpected),
        passed=not (missing or changed or unexpected),
    )


def report_json(report: FreezeVerificationReport) -> str:
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
