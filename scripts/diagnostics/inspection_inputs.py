"""과거 실행 전체와 분리된 plan-inspection 입력 패키지를 만든다.

패키지는 고정 source input과 그 ProjectMap이 실제로 등록한 본문만 보존한다.
원장, provider call, receipt, raw 응답, credential은 탐색하거나 복사하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from flowmarshal.canonical import canonical_json, sha256_bytes
from flowmarshal.engine.domain import ProjectMapRevision
from scripts.diagnostics.r_s06_10_fixtures import SOURCE_INPUT_FILENAMES


PACKAGE_SCHEMA_VERSION = "flowmarshal-inspection-input-package-v1"
MANIFEST_FILENAME = "manifest.json"
SOURCE_INPUTS_DIRECTORY = "source-inputs"
WORKSPACE_DIRECTORY = "workspace"
REGISTERED_REFERENCES_DIRECTORY = "project-references"


class InspectionInputPackageError(RuntimeError):
    """검사 입력 패키지의 whitelist 또는 무결성이 맞지 않을 때 발생한다."""


def _read_json_object(path: Path) -> dict[str, Any]:
    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise InspectionInputPackageError(f"JSON key가 중복됐습니다: {path}: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InspectionInputPackageError(f"JSON 원문을 읽을 수 없습니다: {path}") from exc
    if not isinstance(value, dict):
        raise InspectionInputPackageError(f"JSON 최상위 값이 object가 아닙니다: {path}")
    return value


def _write_bytes_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    _write_bytes_new(path, (canonical_json(value) + "\n").encode("utf-8"))


def _safe_relative_path(value: str, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise InspectionInputPackageError(f"{field}가 안전한 POSIX 상대 경로가 아닙니다: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InspectionInputPackageError(f"{field}가 package 밖을 가리킵니다: {value!r}")
    return path


def _assert_no_symlink(path: Path, *, boundary: Path | None = None) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise InspectionInputPackageError(f"symlink는 허용하지 않습니다: {current}")
        if boundary is not None and current == boundary:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _contained_file(root: Path, relative: Path, *, label: str) -> Path:
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise InspectionInputPackageError(f"{label} 경로가 root 밖을 가리킵니다: {relative}")
    candidate = root / relative
    _assert_no_symlink(candidate, boundary=root)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise InspectionInputPackageError(f"{label} 원문을 확인할 수 없습니다: {candidate}") from exc
    if not resolved.is_file():
        raise InspectionInputPackageError(f"{label}가 일반 파일이 아닙니다: {candidate}")
    return resolved


def _file_record(
    *, package_path: str, materialized_path: str, purpose: str, source_path: Path, content: bytes,
    entry_id: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "byte_digest": sha256_bytes(content),
        "materialized_path": materialized_path,
        "origin_path": str(source_path),
        "package_path": package_path,
        "purpose": purpose,
        "size": len(content),
    }
    if entry_id is not None:
        record["entry_id"] = entry_id
    return record


def _project_files(project_map: ProjectMapRevision) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    original_root = Path(project_map.root)
    if not original_root.is_absolute():
        raise InspectionInputPackageError("ProjectMap root는 절대 경로여야 합니다.")
    _assert_no_symlink(original_root)
    try:
        project_root = original_root.resolve(strict=True)
    except OSError as exc:
        raise InspectionInputPackageError(f"ProjectMap root를 확인할 수 없습니다: {original_root}") from exc
    if not project_root.is_dir():
        raise InspectionInputPackageError(f"ProjectMap root가 디렉터리가 아닙니다: {original_root}")

    records: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    target_keys: set[str] = set()
    for entry in project_map.entries:
        declared = Path(entry.path)
        if declared.is_absolute():
            if entry.kind.value != "reference" or "registered_reference" not in entry.tags:
                raise InspectionInputPackageError(
                    f"ProjectMap root 밖에서는 registered_reference만 패키지할 수 있습니다: {entry.entry_id}"
                )
            _assert_no_symlink(declared)
            try:
                source = declared.resolve(strict=True)
            except OSError as exc:
                raise InspectionInputPackageError(f"등록 reference 본문을 확인할 수 없습니다: {declared}") from exc
            if not source.is_file():
                raise InspectionInputPackageError(f"등록 reference가 일반 파일이 아닙니다: {declared}")
            suffix = source.suffix if source.suffix else ".txt"
            package_path = f"{REGISTERED_REFERENCES_DIRECTORY}/{entry.entry_id}/content{suffix}"
            materialized_path = package_path
            purpose = "registered_reference"
        else:
            source = _contained_file(project_root, declared, label=f"ProjectMap entry {entry.entry_id}")
            relative = source.relative_to(project_root).as_posix()
            package_path = f"{WORKSPACE_DIRECTORY}/{relative}"
            materialized_path = package_path
            purpose = "workspace_entry"

        target_key = package_path.casefold()
        if target_key in target_keys:
            raise InspectionInputPackageError(f"ProjectMap package 경로가 중복됐습니다: {package_path}")
        target_keys.add(target_key)
        content = source.read_bytes()
        if sha256_bytes(content) != entry.content_digest:
            raise InspectionInputPackageError(f"ProjectMap entry 본문 digest가 다릅니다: {entry.entry_id}")
        records.append(_file_record(
            package_path=package_path,
            materialized_path=materialized_path,
            purpose=purpose,
            source_path=source,
            content=content,
            entry_id=entry.entry_id,
        ))
        contents[package_path] = content
    return records, contents


def export_fixture_package(source_run: Path, destination: Path) -> dict[str, Any]:
    """고정 입력과 등록 본문만 새 self-contained package에 x-쓰기한다."""
    source_run = Path(source_run)
    destination = Path(destination)
    _assert_no_symlink(source_run)
    try:
        source_run = source_run.resolve(strict=True)
    except OSError as exc:
        raise InspectionInputPackageError(f"source run을 확인할 수 없습니다: {source_run}") from exc
    if not source_run.is_dir():
        raise InspectionInputPackageError(f"source run이 디렉터리가 아닙니다: {source_run}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)

    source_records: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    for filename in SOURCE_INPUT_FILENAMES:
        source = _contained_file(source_run, Path(filename), label="source input")
        content = source.read_bytes()
        package_path = f"{SOURCE_INPUTS_DIRECTORY}/{filename}"
        source_records.append(_file_record(
            package_path=package_path,
            materialized_path=package_path,
            purpose="source_input",
            source_path=source,
            content=content,
        ))
        contents[package_path] = content

    try:
        project_map = ProjectMapRevision.model_validate(_read_json_object(source_run / "input-project-map.json"))
    except Exception as exc:
        if isinstance(exc, InspectionInputPackageError):
            raise
        raise InspectionInputPackageError("input-project-map.json 계약이 유효하지 않습니다.") from exc
    project_records, project_contents = _project_files(project_map)
    overlap = set(contents) & set(project_contents)
    if overlap:
        raise InspectionInputPackageError(f"package 경로가 중복됐습니다: {sorted(overlap)}")
    contents.update(project_contents)

    entry_by_id = {entry.entry_id: entry for entry in project_map.entries}
    project_entries = []
    for record in project_records:
        entry = entry_by_id[record["entry_id"]]
        project_entries.append({
            "content_digest": entry.content_digest,
            "entry_id": entry.entry_id,
            "kind": entry.kind.value,
            "materialized_path": record["materialized_path"],
            "original_path": entry.path,
            "package_path": record["package_path"],
        })

    files = sorted(source_records + project_records, key=lambda item: item["package_path"])
    manifest = {
        "files": files,
        "project_map": {
            "entries": project_entries,
            "input": "input-project-map.json",
            "original_root": project_map.root,
            "project_map_revision_id": project_map.project_map_revision_id,
        },
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "source_inputs": list(SOURCE_INPUT_FILENAMES),
        "source_run": {"name": source_run.name, "path": str(source_run)},
    }

    destination.mkdir(parents=True)
    for package_path, content in sorted(contents.items()):
        _write_bytes_new(destination / Path(*PurePosixPath(package_path).parts), content)
    _write_json_new(destination / MANIFEST_FILENAME, manifest)
    return verify_fixture_package(destination)


def _require_exact_keys(value: dict[str, Any], keys: set[str], *, label: str) -> None:
    if set(value) != keys:
        raise InspectionInputPackageError(
            f"{label} field 집합이 다릅니다: expected={sorted(keys)!r}, actual={sorted(value)!r}"
        )


def _validated_manifest(package: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = package / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InspectionInputPackageError("package manifest가 없거나 symlink입니다.")
    manifest = _read_json_object(manifest_path)
    _require_exact_keys(
        manifest, {"files", "project_map", "schema_version", "source_inputs", "source_run"}, label="manifest",
    )
    if manifest["schema_version"] != PACKAGE_SCHEMA_VERSION:
        raise InspectionInputPackageError("package schema_version이 다릅니다.")
    if manifest["source_inputs"] != list(SOURCE_INPUT_FILENAMES):
        raise InspectionInputPackageError("source input 목록·순서가 고정 목록과 다릅니다.")
    if not isinstance(manifest["source_run"], dict):
        raise InspectionInputPackageError("source_run provenance가 object가 아닙니다.")
    _require_exact_keys(manifest["source_run"], {"name", "path"}, label="source_run")
    if not all(isinstance(manifest["source_run"][key], str) and manifest["source_run"][key]
               for key in ("name", "path")):
        raise InspectionInputPackageError("source_run provenance가 비어 있습니다.")
    project_map = manifest["project_map"]
    if not isinstance(project_map, dict):
        raise InspectionInputPackageError("project_map metadata가 object가 아닙니다.")
    _require_exact_keys(
        project_map, {"entries", "input", "original_root", "project_map_revision_id"}, label="project_map",
    )
    if project_map["input"] != "input-project-map.json" or not isinstance(project_map["entries"], list):
        raise InspectionInputPackageError("project_map metadata가 유효하지 않습니다.")
    if not isinstance(project_map["original_root"], str) or not project_map["original_root"]:
        raise InspectionInputPackageError("project_map original_root가 비어 있습니다.")
    if not isinstance(project_map["project_map_revision_id"], str) or not project_map["project_map_revision_id"]:
        raise InspectionInputPackageError("project_map revision provenance가 비어 있습니다.")

    raw_files = manifest["files"]
    if not isinstance(raw_files, list):
        raise InspectionInputPackageError("files가 배열이 아닙니다.")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(raw_files):
        if not isinstance(record, dict):
            raise InspectionInputPackageError(f"files[{index}]가 object가 아닙니다.")
        common = {"byte_digest", "materialized_path", "origin_path", "package_path", "purpose", "size"}
        expected_keys = common | ({"entry_id"} if record.get("purpose") != "source_input" else set())
        _require_exact_keys(record, expected_keys, label=f"files[{index}]")
        package_path = _safe_relative_path(record["package_path"], field=f"files[{index}].package_path")
        materialized = _safe_relative_path(
            record["materialized_path"], field=f"files[{index}].materialized_path",
        )
        if package_path != materialized:
            raise InspectionInputPackageError("현재 package의 materialized_path는 package_path와 같아야 합니다.")
        key = package_path.as_posix().casefold()
        if key in seen:
            raise InspectionInputPackageError(f"package 파일 경로가 중복됐습니다: {package_path}")
        seen.add(key)
        purpose = record["purpose"]
        if purpose not in {"source_input", "workspace_entry", "registered_reference"}:
            raise InspectionInputPackageError(f"허용되지 않은 package 파일 목적입니다: {purpose!r}")
        if not isinstance(record["origin_path"], str) or not record["origin_path"]:
            raise InspectionInputPackageError("origin_path provenance가 비어 있습니다.")
        if not isinstance(record["byte_digest"], str) or not record["byte_digest"].startswith("sha256:"):
            raise InspectionInputPackageError("byte_digest 형식이 유효하지 않습니다.")
        if not isinstance(record["size"], int) or isinstance(record["size"], bool) or record["size"] < 0:
            raise InspectionInputPackageError("package 파일 크기가 유효하지 않습니다.")
        records.append(record)
    if [record["package_path"] for record in records] != sorted(record["package_path"] for record in records):
        raise InspectionInputPackageError("package file record 순서가 고정 정렬과 다릅니다.")

    expected_source_paths = {f"{SOURCE_INPUTS_DIRECTORY}/{name}" for name in SOURCE_INPUT_FILENAMES}
    actual_source_paths = {record["package_path"] for record in records if record["purpose"] == "source_input"}
    if actual_source_paths != expected_source_paths:
        raise InspectionInputPackageError("source input package 파일이 중복·누락됐습니다.")

    entries = project_map["entries"]
    entry_ids: set[str] = set()
    entry_paths: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InspectionInputPackageError(f"project_map.entries[{index}]가 object가 아닙니다.")
        _require_exact_keys(
            entry,
            {"content_digest", "entry_id", "kind", "materialized_path", "original_path", "package_path"},
            label=f"project_map.entries[{index}]",
        )
        package_path = _safe_relative_path(entry["package_path"], field="project entry package_path").as_posix()
        materialized = _safe_relative_path(entry["materialized_path"], field="project entry materialized_path")
        if package_path != materialized.as_posix():
            raise InspectionInputPackageError("project entry 경로 결속이 다릅니다.")
        if not all(isinstance(entry[key], str) and entry[key]
                   for key in ("content_digest", "entry_id", "kind", "original_path")):
            raise InspectionInputPackageError("project entry metadata가 비어 있습니다.")
        if entry["entry_id"] in entry_ids or package_path.casefold() in entry_paths:
            raise InspectionInputPackageError("project entry ID 또는 경로가 중복됐습니다.")
        entry_ids.add(entry["entry_id"])
        entry_paths.add(package_path.casefold())
        matching = [record for record in records if record.get("entry_id") == entry["entry_id"]]
        if len(matching) != 1 or matching[0]["package_path"] != package_path:
            raise InspectionInputPackageError("project entry와 file record 결속이 다릅니다.")
        if matching[0]["byte_digest"] != entry["content_digest"]:
            raise InspectionInputPackageError("project entry content digest 결속이 다릅니다.")
    record_entry_ids = {record["entry_id"] for record in records if record["purpose"] != "source_input"}
    if record_entry_ids != entry_ids:
        raise InspectionInputPackageError("ProjectMap entry 파일이 중복·누락됐습니다.")
    return manifest, records


def _same_lexical_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _verify_packaged_provenance(
    package: Path, manifest: dict[str, Any], records: list[dict[str, Any]],
) -> None:
    source_run_path = Path(manifest["source_run"]["path"])
    if source_run_path.name != manifest["source_run"]["name"]:
        raise InspectionInputPackageError("source_run name과 path provenance가 다릅니다.")
    records_by_path = {record["package_path"]: record for record in records}
    for filename in SOURCE_INPUT_FILENAMES:
        record = records_by_path[f"{SOURCE_INPUTS_DIRECTORY}/{filename}"]
        if not _same_lexical_path(record["origin_path"], source_run_path / filename):
            raise InspectionInputPackageError(f"source input origin provenance가 다릅니다: {filename}")

    project_map_path = package / SOURCE_INPUTS_DIRECTORY / manifest["project_map"]["input"]
    try:
        project_map = ProjectMapRevision.model_validate(_read_json_object(project_map_path))
    except Exception as exc:
        if isinstance(exc, InspectionInputPackageError):
            raise
        raise InspectionInputPackageError("package의 ProjectMap 계약이 유효하지 않습니다.") from exc
    if (
        project_map.root != manifest["project_map"]["original_root"]
        or project_map.project_map_revision_id != manifest["project_map"]["project_map_revision_id"]
    ):
        raise InspectionInputPackageError("package ProjectMap provenance 결속이 다릅니다.")

    expected_entries: list[dict[str, Any]] = []
    records_by_entry = {record.get("entry_id"): record for record in records if "entry_id" in record}
    original_root = Path(project_map.root)
    for entry in project_map.entries:
        declared = Path(entry.path)
        if declared.is_absolute():
            if entry.kind.value != "reference" or "registered_reference" not in entry.tags:
                raise InspectionInputPackageError(
                    f"ProjectMap root 밖에서는 registered_reference만 패키지할 수 있습니다: {entry.entry_id}"
                )
            suffix = declared.suffix if declared.suffix else ".txt"
            package_path = f"{REGISTERED_REFERENCES_DIRECTORY}/{entry.entry_id}/content{suffix}"
            expected_origin = declared
            expected_purpose = "registered_reference"
        else:
            if not declared.parts or any(part in {"", ".", ".."} for part in declared.parts):
                raise InspectionInputPackageError(f"ProjectMap 상대 entry 경로가 유효하지 않습니다: {entry.path}")
            package_path = f"{WORKSPACE_DIRECTORY}/{declared.as_posix()}"
            expected_origin = original_root / declared
            expected_purpose = "workspace_entry"
        record = records_by_entry.get(entry.entry_id)
        if (
            record is None
            or record["package_path"] != package_path
            or record["purpose"] != expected_purpose
            or not _same_lexical_path(record["origin_path"], expected_origin)
        ):
            raise InspectionInputPackageError(f"ProjectMap entry provenance 결속이 다릅니다: {entry.entry_id}")
        expected_entries.append({
            "content_digest": entry.content_digest,
            "entry_id": entry.entry_id,
            "kind": entry.kind.value,
            "materialized_path": package_path,
            "original_path": entry.path,
            "package_path": package_path,
        })
    if manifest["project_map"]["entries"] != expected_entries:
        raise InspectionInputPackageError("manifest ProjectMap entry 목록이 원본 ProjectMap과 다릅니다.")


def verify_fixture_package(package: Path) -> dict[str, Any]:
    """package whitelist, 경로, symlink와 모든 원문 byte digest를 검사한다."""
    package = Path(package)
    _assert_no_symlink(package)
    try:
        package = package.resolve(strict=True)
    except OSError as exc:
        raise InspectionInputPackageError(f"package를 확인할 수 없습니다: {package}") from exc
    if not package.is_dir():
        raise InspectionInputPackageError(f"package가 디렉터리가 아닙니다: {package}")

    for item in package.rglob("*"):
        if item.is_symlink():
            raise InspectionInputPackageError(f"package 내부 symlink는 허용하지 않습니다: {item}")
    manifest, records = _validated_manifest(package)
    expected_files = {MANIFEST_FILENAME, *(record["package_path"] for record in records)}
    actual_files = {item.relative_to(package).as_posix() for item in package.rglob("*") if item.is_file()}
    if actual_files != expected_files:
        raise InspectionInputPackageError(
            f"package whitelist와 실제 파일 집합이 다릅니다: missing={sorted(expected_files - actual_files)!r}, "
            f"extra={sorted(actual_files - expected_files)!r}"
        )
    allowed_directories = {""}
    for value in expected_files:
        parent = PurePosixPath(value).parent
        while parent.as_posix() != ".":
            allowed_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = {item.relative_to(package).as_posix() for item in package.rglob("*") if item.is_dir()}
    if actual_directories != allowed_directories - {""}:
        raise InspectionInputPackageError("package에 whitelist 밖 디렉터리가 있습니다.")

    for record in records:
        path = package / Path(*PurePosixPath(record["package_path"]).parts)
        content = path.read_bytes()
        if len(content) != record["size"] or sha256_bytes(content) != record["byte_digest"]:
            raise InspectionInputPackageError(f"package 원문 bytes가 변조됐습니다: {record['package_path']}")
    _verify_packaged_provenance(package, manifest, records)

    manifest_digest = sha256_bytes((package / MANIFEST_FILENAME).read_bytes())
    return {
        "file_count": len(records),
        "manifest_digest": manifest_digest,
        "package": str(package),
        "project_entry_count": len(manifest["project_map"]["entries"]),
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "source_input_count": len(SOURCE_INPUT_FILENAMES),
        "source_inputs_dir": str(package / SOURCE_INPUTS_DIRECTORY),
    }


def _assert_materialization_target(run: Path, relative: PurePosixPath) -> Path:
    target = run / Path(*relative.parts)
    current = target.parent
    while current != run:
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise InspectionInputPackageError(f"materialize 상위 경로가 안전한 디렉터리가 아닙니다: {current}")
        current = current.parent
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    return target


def materialize_fixture_package(package: Path, run: Path) -> dict[str, Any]:
    """검증된 package를 새 run 경로에 복제하고 경로 대응표를 반환한다."""
    verification = verify_fixture_package(package)
    package = Path(verification["package"])
    manifest, records = _validated_manifest(package)
    run = Path(run)
    if run.is_symlink():
        raise InspectionInputPackageError(f"run은 symlink일 수 없습니다: {run}")
    run = run.resolve(strict=False)
    if run.exists() and not run.is_dir():
        raise InspectionInputPackageError(f"run이 디렉터리가 아닙니다: {run}")
    _assert_no_symlink(run.parent)

    targets: list[tuple[dict[str, Any], Path, bytes]] = []
    seen: set[str] = set()
    for record in records:
        relative = _safe_relative_path(record["materialized_path"], field="materialized_path")
        key = relative.as_posix().casefold()
        if key in seen:
            raise InspectionInputPackageError(f"materialize 경로가 중복됐습니다: {relative}")
        seen.add(key)
        target = _assert_materialization_target(run, relative)
        source = package / Path(*PurePosixPath(record["package_path"]).parts)
        targets.append((record, target, source.read_bytes()))

    run.mkdir(parents=True, exist_ok=True)
    for _, target, content in targets:
        _write_bytes_new(target, content)

    materialized_entries = []
    record_by_entry = {record["entry_id"]: record for record in records if record["purpose"] != "source_input"}
    original_root = Path(manifest["project_map"]["original_root"])
    for entry in manifest["project_map"]["entries"]:
        record = record_by_entry[entry["entry_id"]]
        destination = run / Path(*PurePosixPath(record["materialized_path"]).parts)
        original = Path(entry["original_path"])
        source_path = original if original.is_absolute() else original_root / original
        materialized_entries.append({
            "entry_id": entry["entry_id"],
            "kind": entry["kind"],
            "source": str(source_path),
            "destination": str(destination),
        })

    source_inputs_dir = run / SOURCE_INPUTS_DIRECTORY
    project_root = run / WORKSPACE_DIRECTORY
    return {
        "file_count": len(records),
        "manifest_digest": verification["manifest_digest"],
        "package": str(package),
        "path_mappings": [
            {
                "kind": "project_root",
                "source": manifest["project_map"]["original_root"],
                "destination": str(project_root),
            },
            *materialized_entries,
        ],
        "project_entries": materialized_entries,
        "project_root": str(project_root),
        "run": str(run),
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "source_inputs_dir": str(source_inputs_dir),
    }


def _absolute_cli_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("절대 경로가 필요합니다.")
    return path


def main(argv: list[str] | None = None) -> int:
    """검사 입력 package export와 verify API의 얇은 CLI 진입점."""
    parser = argparse.ArgumentParser(description="독립 plan-inspection 입력 package 관리")
    commands = parser.add_subparsers(dest="command", required=True)
    export_parser = commands.add_parser("export", help="고정 source run에서 package를 생성합니다.")
    export_parser.add_argument("--source-run", type=_absolute_cli_path, required=True)
    export_parser.add_argument("--package", type=_absolute_cli_path, required=True)
    verify_parser = commands.add_parser("verify", help="package whitelist와 byte digest를 검사합니다.")
    verify_parser.add_argument("--package", type=_absolute_cli_path, required=True)
    arguments = parser.parse_args(argv)
    result = (
        export_fixture_package(arguments.source_run, arguments.package)
        if arguments.command == "export"
        else verify_fixture_package(arguments.package)
    )
    print(canonical_json(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
