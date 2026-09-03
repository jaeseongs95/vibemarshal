"""별도 폐기 Windows VM으로 전달할 실행 번들과 증거 반출입을 관리한다.

번들은 명시적인 허용 목록으로만 구성한다. VM에서 반출하는 자료도 시나리오
계약과 setup 결과 JSON으로 제한해 Codex 인증·세션·sandbox 비밀이 호스트로
복사되지 않게 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from flowmarshal.vm_sandbox_suite import (
    VM_SCENARIOS,
    read_json_object,
    suite_root,
    validate_component,
)
from flowmarshal.windows_sandbox import artifact_safety_violations


VM_HANDOFF_SCHEMA_VERSION = "1.0"
VM_HANDOFF_INVALID = "VM_HANDOFF_INVALID"
VM_HANDOFF_TAMPERED = "VM_HANDOFF_TAMPERED"
VM_HANDOFF_DESTINATION_EXISTS = "VM_HANDOFF_DESTINATION_EXISTS"

_BUNDLE_ROOT_FILES = (
    "README.md",
    "pyproject.toml",
    "requirements.lock",
)
_BUNDLE_DOCUMENTS = (
    "docs/windows-sandbox-vm-revalidation.md",
    "docs/windows-vm-external-handoff.md",
)
_BUNDLE_MANIFEST = "bundle-manifest.json"
_SCENARIO_EXPORT_MANIFEST = "scenario-export.json"
_FORBIDDEN_FILE_NAMES = {"auth.json"}
_FORBIDDEN_DIRECTORY_NAMES = {".sandbox-secrets", ".codex", "sessions"}
_FORBIDDEN_DATABASE_PREFIXES = (
    "state_",
    "goals_",
    "logs_",
    "memories_",
    "queue_",
)


class VmHandoffError(ValueError):
    """VM 전달 번들 또는 반출 증거 계약이 유효하지 않은 경우의 오류."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise VmHandoffError(
            VM_HANDOFF_INVALID,
            f"상대경로 계약을 벗어난 항목입니다: {value}",
        )
    return path


def _forbidden_relative_path(path: PurePosixPath) -> bool:
    lowered = tuple(part.casefold() for part in path.parts)
    name = lowered[-1]
    if any(part in _FORBIDDEN_DIRECTORY_NAMES for part in lowered):
        return True
    if name in _FORBIDDEN_FILE_NAMES:
        return True
    return name.endswith((".sqlite", ".sqlite-shm", ".sqlite-wal")) and name.startswith(
        _FORBIDDEN_DATABASE_PREFIXES
    )


def _bundle_source_files(project_root: Path) -> list[tuple[PurePosixPath, Path]]:
    project_root = project_root.resolve(strict=True)
    relative_paths: set[PurePosixPath] = {
        PurePosixPath(value) for value in (*_BUNDLE_ROOT_FILES, *_BUNDLE_DOCUMENTS)
    }
    for folder, pattern in (
        (project_root / "src", "*.py"),
        (project_root / "scripts" / "windows-vm", "*.ps1"),
    ):
        if not folder.is_dir():
            raise VmHandoffError(
                VM_HANDOFF_INVALID,
                f"번들 필수 폴더가 없습니다: {folder}",
            )
        relative_paths.update(
            PurePosixPath(path.relative_to(project_root).as_posix())
            for path in folder.rglob(pattern)
            if path.is_file()
        )

    result: list[tuple[PurePosixPath, Path]] = []
    for relative in sorted(relative_paths, key=str):
        if _forbidden_relative_path(relative):
            raise VmHandoffError(
                VM_HANDOFF_INVALID,
                f"반출 금지 경로가 번들 허용 목록에 포함됐습니다: {relative}",
            )
        source = project_root.joinpath(*relative.parts)
        if not source.is_file() or source.is_symlink():
            raise VmHandoffError(
                VM_HANDOFF_INVALID,
                f"번들 필수 파일이 없거나 심볼릭 링크입니다: {source}",
            )
        result.append((relative, source))
    return result


def _zip_write_bytes(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload)


def build_vm_bundle(project_root: Path, output: Path) -> dict[str, Any]:
    """별도 VM에 복사할 최소 실행 번들을 생성한다."""

    project_root = project_root.resolve(strict=True)
    output = output.expanduser().resolve(strict=False)
    if output.suffix.casefold() != ".zip":
        raise VmHandoffError(VM_HANDOFF_INVALID, "번들 출력 파일은 .zip이어야 합니다.")
    sources = _bundle_source_files(project_root)
    files: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    for relative, source in sources:
        payload = source.read_bytes()
        name = relative.as_posix()
        payloads.append((name, payload))
        files.append(
            {
                "path": name,
                "bytes": len(payload),
                "sha256": _sha256_bytes(payload),
            }
        )

    manifest = {
        "schema_version": VM_HANDOFF_SCHEMA_VERSION,
        "kind": "flowmarshal_external_windows_vm_bundle",
        "created_at": _utc_now(),
        "files": files,
        "contains_codex_home": False,
        "contains_authentication": False,
        "contains_sandbox_secrets": False,
    }
    manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, payload in payloads:
                _zip_write_bytes(archive, name, payload)
            _zip_write_bytes(archive, _BUNDLE_MANIFEST, manifest_payload)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)

    archive_hash = _sha256_file(output)
    checksum_path = output.with_name(f"{output.name}.sha256")
    checksum_path.write_text(f"{archive_hash}  {output.name}\n", encoding="ascii")
    return {
        "status": "READY_FOR_TRANSFER",
        "bundle": str(output),
        "bundle_sha256": archive_hash,
        "checksum_file": str(checksum_path),
        "file_count": len(files),
        "contains_codex_home": False,
        "contains_authentication": False,
        "contains_sandbox_secrets": False,
    }


def verify_vm_bundle(archive_path: Path) -> dict[str, Any]:
    """번들의 파일 집합과 해시를 검증한다."""

    archive_path = archive_path.expanduser().resolve(strict=True)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or _BUNDLE_MANIFEST not in names:
                raise VmHandoffError(
                    VM_HANDOFF_TAMPERED,
                    "번들에 중복 경로나 manifest 누락이 있습니다.",
                )
            if any(_forbidden_relative_path(_safe_relative_path(name)) for name in names):
                raise VmHandoffError(
                    VM_HANDOFF_TAMPERED,
                    "번들에서 Codex 인증·상태 반출 금지 경로를 발견했습니다.",
                )
            manifest = json.loads(archive.read(_BUNDLE_MANIFEST))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != VM_HANDOFF_SCHEMA_VERSION:
                raise VmHandoffError(VM_HANDOFF_TAMPERED, "번들 manifest 버전이 유효하지 않습니다.")
            rows = manifest.get("files")
            if not isinstance(rows, list):
                raise VmHandoffError(VM_HANDOFF_TAMPERED, "번들 파일 manifest가 없습니다.")
            expected = {_BUNDLE_MANIFEST}
            for row in rows:
                if not isinstance(row, dict):
                    raise VmHandoffError(VM_HANDOFF_TAMPERED, "번들 파일 항목이 유효하지 않습니다.")
                relative = _safe_relative_path(str(row.get("path", "")))
                name = relative.as_posix()
                expected.add(name)
                payload = archive.read(name)
                if len(payload) != row.get("bytes") or _sha256_bytes(payload) != row.get("sha256"):
                    raise VmHandoffError(VM_HANDOFF_TAMPERED, f"번들 파일 해시가 다릅니다: {name}")
            if set(names) != expected:
                raise VmHandoffError(VM_HANDOFF_TAMPERED, "manifest에 없는 파일이 번들에 포함됐습니다.")
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, VmHandoffError):
            raise
        raise VmHandoffError(
            VM_HANDOFF_TAMPERED,
            f"번들을 검증할 수 없습니다: {type(exc).__name__}: {exc}",
        ) from exc
    return {
        "status": "VALID",
        "bundle": str(archive_path),
        "bundle_sha256": _sha256_file(archive_path),
        "file_count": len(expected) - 1,
    }


def _allowed_scenario_evidence(path: PurePosixPath) -> bool:
    if path.parts == ("scenario.json",):
        return True
    if len(path.parts) == 3 and path.parts[0] == "attempts" and path.parts[2] == "setup-result.json":
        validate_component(path.parts[1], label="vm-attempt-id")
        return True
    if len(path.parts) == 2 and path.parts[0] == "guards" and path.suffix == ".json":
        validate_component(path.stem, label="vm-guard-id")
        return True
    return False


def _scenario_evidence_files(root: Path) -> list[tuple[PurePosixPath, Path]]:
    if not root.is_dir():
        raise VmHandoffError(VM_HANDOFF_INVALID, f"VM 시나리오 증거 폴더가 없습니다: {root}")
    result: list[tuple[PurePosixPath, Path]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise VmHandoffError(VM_HANDOFF_INVALID, f"증거에 심볼릭 링크를 사용할 수 없습니다: {path}")
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if not _allowed_scenario_evidence(relative):
            raise VmHandoffError(VM_HANDOFF_INVALID, f"반출 허용 목록 밖의 파일입니다: {relative}")
        result.append((relative, path))
    if not any(relative.parts == ("scenario.json",) for relative, _ in result):
        raise VmHandoffError(VM_HANDOFF_INVALID, "시나리오 계약 증거가 없습니다.")
    if not any(relative.parts[0] == "attempts" for relative, _ in result):
        raise VmHandoffError(VM_HANDOFF_INVALID, "setup Attempt 증거가 없습니다.")
    return result


def _validate_evidence_identity(path: Path, suite_id: str, scenario: str) -> None:
    value = read_json_object(path)
    if value.get("suite_id") != suite_id or value.get("scenario") != scenario:
        raise VmHandoffError(
            VM_HANDOFF_INVALID,
            f"증거의 suite 또는 scenario가 요청과 다릅니다: {path}",
        )


def export_vm_scenario(
    project_root: Path,
    evidence_root: Path,
    suite_id: str,
    scenario: str,
) -> dict[str, Any]:
    """VM의 한 시나리오에서 허용된 JSON 증거만 공유 위치로 반출한다."""

    validate_component(suite_id, label="vm-suite-id")
    if scenario not in VM_SCENARIOS:
        raise VmHandoffError(VM_HANDOFF_INVALID, f"알 수 없는 VM 시나리오입니다: {scenario}")
    project_root = project_root.expanduser().resolve(strict=True)
    if not evidence_root.is_absolute():
        raise VmHandoffError(VM_HANDOFF_INVALID, "증거 반출 루트는 절대경로여야 합니다.")
    evidence_root = evidence_root.expanduser().resolve(strict=False)
    if _is_within(evidence_root, project_root):
        raise VmHandoffError(
            VM_HANDOFF_INVALID,
            "증거 반출 루트는 snapshot으로 되돌아갈 프로젝트 밖이어야 합니다.",
        )
    source = (
        project_root
        / "spikes"
        / "gate0a"
        / "artifacts"
        / "vm-harness"
        / "suites"
        / suite_id
        / "scenarios"
        / scenario
    )
    violations = artifact_safety_violations(source)
    if violations:
        raise VmHandoffError(
            VM_HANDOFF_INVALID,
            f"시나리오 폴더에 반출 금지 Codex 상태가 있습니다: {violations}",
        )
    sources = _scenario_evidence_files(source)
    for _, path in sources:
        _validate_evidence_identity(path, suite_id, scenario)

    destination = evidence_root / suite_id / "scenarios" / scenario
    if destination.exists():
        raise VmHandoffError(
            VM_HANDOFF_DESTINATION_EXISTS,
            f"기존 반출 증거를 덮어쓰지 않습니다: {destination}",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{scenario}-", dir=destination.parent))
    try:
        rows: list[dict[str, Any]] = []
        for relative, source_path in sources:
            target = staging.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
            rows.append(
                {
                    "path": relative.as_posix(),
                    "bytes": target.stat().st_size,
                    "sha256": _sha256_file(target),
                }
            )
        manifest = {
            "schema_version": VM_HANDOFF_SCHEMA_VERSION,
            "kind": "flowmarshal_vm_scenario_export",
            "suite_id": suite_id,
            "scenario": scenario,
            "exported_at": _utc_now(),
            "files": rows,
            "contains_codex_home": False,
            "contains_authentication": False,
            "contains_sandbox_secrets": False,
        }
        (staging / _SCENARIO_EXPORT_MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.replace(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "status": "EXPORTED",
        "suite_id": suite_id,
        "scenario": scenario,
        "destination": str(destination),
        "file_count": len(sources),
        "secret_contents_read": False,
    }


def verify_scenario_export(root: Path, suite_id: str, scenario: str) -> dict[str, Any]:
    """공유 위치의 시나리오 반출물이 허용 목록·manifest와 일치하는지 확인한다."""

    manifest_path = root / _SCENARIO_EXPORT_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise VmHandoffError(VM_HANDOFF_TAMPERED, f"시나리오 export manifest가 없습니다: {root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VmHandoffError(VM_HANDOFF_TAMPERED, f"export manifest를 읽을 수 없습니다: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != VM_HANDOFF_SCHEMA_VERSION
        or manifest.get("suite_id") != suite_id
        or manifest.get("scenario") != scenario
    ):
        raise VmHandoffError(VM_HANDOFF_TAMPERED, "export manifest의 suite 또는 scenario가 다릅니다.")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise VmHandoffError(VM_HANDOFF_TAMPERED, "export 파일 manifest가 없습니다.")
    expected = {_SCENARIO_EXPORT_MANIFEST}
    for row in rows:
        if not isinstance(row, dict):
            raise VmHandoffError(VM_HANDOFF_TAMPERED, "export 파일 항목이 유효하지 않습니다.")
        relative = _safe_relative_path(str(row.get("path", "")))
        if not _allowed_scenario_evidence(relative):
            raise VmHandoffError(VM_HANDOFF_TAMPERED, f"허용되지 않은 export 경로입니다: {relative}")
        path = root.joinpath(*relative.parts)
        if not path.is_file() or path.is_symlink():
            raise VmHandoffError(VM_HANDOFF_TAMPERED, f"export 파일이 없거나 링크입니다: {relative}")
        if path.stat().st_size != row.get("bytes") or _sha256_file(path) != row.get("sha256"):
            raise VmHandoffError(VM_HANDOFF_TAMPERED, f"export 파일 해시가 다릅니다: {relative}")
        _validate_evidence_identity(path, suite_id, scenario)
        expected.add(relative.as_posix())
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise VmHandoffError(VM_HANDOFF_TAMPERED, "manifest에 없거나 누락된 export 파일이 있습니다.")
    return {"status": "VALID", "scenario": scenario, "file_count": len(rows)}


def import_vm_suite(project_root: Path, evidence_root: Path, suite_id: str) -> dict[str, Any]:
    """세 VM 시나리오의 반출 증거를 프로젝트 suite로 한 번만 가져온다."""

    validate_component(suite_id, label="vm-suite-id")
    project_root = project_root.expanduser().resolve(strict=True)
    evidence_root = evidence_root.expanduser().resolve(strict=True)
    source_suite = evidence_root / suite_id
    source_scenarios = source_suite / "scenarios"
    for scenario in VM_SCENARIOS:
        verify_scenario_export(source_scenarios / scenario, suite_id, scenario)

    artifacts = project_root / "spikes" / "gate0a" / "artifacts"
    destination = suite_root(artifacts, suite_id)
    if destination.exists():
        raise VmHandoffError(
            VM_HANDOFF_DESTINATION_EXISTS,
            f"기존 suite 증거를 덮어쓰지 않습니다: {destination}",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{suite_id}-", dir=destination.parent))
    try:
        imported_files = 0
        for scenario in VM_SCENARIOS:
            source = source_scenarios / scenario
            manifest = json.loads((source / _SCENARIO_EXPORT_MANIFEST).read_text(encoding="utf-8"))
            for row in manifest["files"]:
                relative = _safe_relative_path(str(row["path"]))
                target = staging / "scenarios" / scenario
                target = target.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source.joinpath(*relative.parts), target)
                imported_files += 1
            receipt = staging / "imports" / f"{scenario}-export.json"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / _SCENARIO_EXPORT_MANIFEST, receipt)
        violations = artifact_safety_violations(staging)
        if violations:
            raise VmHandoffError(
                VM_HANDOFF_INVALID,
                f"가져올 suite에서 반출 금지 Codex 상태를 발견했습니다: {violations}",
            )
        staging.replace(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "status": "IMPORTED",
        "suite_id": suite_id,
        "destination": str(destination),
        "scenario_count": len(VM_SCENARIOS),
        "file_count": imported_files,
        "secret_contents_read": False,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FlowMarshal 별도 Windows VM 전달 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-bundle", help="별도 VM 전달용 ZIP을 만든다")
    build.add_argument("--project-root", type=Path, default=Path.cwd())
    build.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify-bundle", help="VM 전달용 ZIP의 무결성을 검사한다")
    verify.add_argument("--archive", type=Path, required=True)

    export = subparsers.add_parser("export-scenario", help="VM 시나리오 JSON 증거만 공유 위치로 반출한다")
    export.add_argument("--project-root", type=Path, required=True)
    export.add_argument("--evidence-root", type=Path, required=True)
    export.add_argument("--vm-suite-id", required=True)
    export.add_argument("--vm-scenario", choices=VM_SCENARIOS, required=True)

    import_suite = subparsers.add_parser("import-suite", help="세 VM 시나리오 증거를 호스트로 가져온다")
    import_suite.add_argument("--project-root", type=Path, required=True)
    import_suite.add_argument("--evidence-root", type=Path, required=True)
    import_suite.add_argument("--vm-suite-id", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "build-bundle":
            result = build_vm_bundle(args.project_root, args.output)
        elif args.command == "verify-bundle":
            result = verify_vm_bundle(args.archive)
        elif args.command == "export-scenario":
            result = export_vm_scenario(
                args.project_root,
                args.evidence_root,
                args.vm_suite_id,
                args.vm_scenario,
            )
        else:
            result = import_vm_suite(args.project_root, args.evidence_root, args.vm_suite_id)
    except VmHandoffError as exc:
        _print({"status": "ERROR", "reason_code": exc.reason_code, "message": str(exc)})
        return 2
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
