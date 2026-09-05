"""제한 qualification 실행용 worktree 결속 검사.

이 모듈은 실험 실행이 시작할 때의 checkout과 실행 환경을 고정한다. 원격 추적
브랜치와 다른 checkout은 시작 provenance일 뿐, 실행 중 재검사 대상이 아니다.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.qualification import source_manifest_files


_QUALIFICATION_RUNTIME_FIXTURES = (
    (
        "tests/test_gate0c_verifier.py",
        "spikes/gate0c/artifacts/control/gate0c.sqlite3",
    ),
    (
        "tests/test_gate0c_e2e.py",
        "spikes/gate0c/runs/profile-20260902-r7/unregistered/secret.txt",
    ),
)


class WorkspaceBindingError(RuntimeError):
    """고정한 qualification worktree 또는 실행 환경이 달라졌을 때 발생한다."""


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "알 수 없는 git 오류"
        raise WorkspaceBindingError(f"worktree git 검사 실패: {' '.join(arguments)}: {detail}")
    return completed.stdout.strip()


def _resolve_root(root: Path) -> Path:
    try:
        base = root.resolve(strict=True)
    except OSError as error:
        raise WorkspaceBindingError(f"workspace root를 찾을 수 없습니다: {root}") from error
    if not base.is_dir():
        raise WorkspaceBindingError(f"workspace root가 디렉터리가 아닙니다: {base}")
    if _git(base, "rev-parse", "--is-inside-work-tree") != "true":
        raise WorkspaceBindingError(f"workspace root가 git worktree가 아닙니다: {base}")
    git_root = Path(_git(base, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if git_root != base:
        raise WorkspaceBindingError(
            f"workspace root는 worktree 최상위여야 합니다: 요청={base}, git={git_root}"
        )
    return base


def _require_detached_worktree(root: Path) -> None:
    symbolic = subprocess.run(
        ("git", "-C", str(root), "symbolic-ref", "-q", "HEAD"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if symbolic.returncode == 0:
        raise WorkspaceBindingError(
            "capture와 verify는 실험용 detached worktree에서만 허용됩니다: "
            f"현재 {symbolic.stdout.strip()!r}"
        )
    if symbolic.returncode != 1:
        detail = symbolic.stderr.strip() or "알 수 없는 git 오류"
        raise WorkspaceBindingError(f"detached HEAD 검사 실패: {detail}")


def _require_clean_tracked_files(root: Path) -> None:
    # ignored artifact는 실행 입력이 아니며, untracked Python은 별도 source manifest가 결속한다.
    changed = _git(root, "status", "--porcelain=v1", "--untracked-files=no")
    if changed:
        entries = "; ".join(changed.splitlines()[:5])
        raise WorkspaceBindingError(f"추적 source 파일에 미결 변경이 있어 실행을 차단합니다: {entries}")


def _dedicated_interpreter(root: Path) -> Path:
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise WorkspaceBindingError(
        "전용 .venv interpreter를 찾을 수 없습니다: "
        + ", ".join(str(candidate) for candidate in candidates)
    )


def _required_file_digest(path: Path, description: str) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as error:
        raise WorkspaceBindingError(f"{description}를 읽을 수 없습니다: {path}") from error


def _base_executable_identity() -> tuple[str | None, str | None]:
    raw_base = getattr(sys, "_base_executable", None)
    if not isinstance(raw_base, str) or not raw_base:
        return None, None
    try:
        base = Path(raw_base).resolve(strict=True)
    except OSError:
        return None, None
    if not base.is_file():
        return None, None
    return str(base), _required_file_digest(base, "base Python executable")


def _installed_distributions() -> list[dict[str, str]]:
    distributions: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name") or distribution.metadata.get("name")
        if not name:
            raise WorkspaceBindingError("설치 distribution의 Name metadata가 없습니다.")
        distributions.append({"name": name, "version": distribution.version})
    return sorted(distributions, key=lambda item: (item["name"].casefold(), item["version"]))


def _interpreter_identity(root: Path) -> dict[str, Any]:
    """현재 프로세스가 이 worktree 전용 virtual environment인지 확인한다."""

    expected = _dedicated_interpreter(root)
    actual = Path(sys.executable).resolve(strict=True)
    if actual != expected:
        raise WorkspaceBindingError(
            "실행 Python이 worktree 전용 .venv interpreter와 다릅니다: "
            f"actual={actual}, expected={expected}"
        )
    pyvenv_config = root / ".venv" / "pyvenv.cfg"
    base_executable, base_executable_digest = _base_executable_identity()
    return {
        "executable": str(actual),
        "executable_digest": _required_file_digest(actual, "전용 .venv interpreter"),
        "prefix": str(Path(sys.prefix).resolve(strict=True)),
        "implementation": sys.implementation.name,
        "version_info": list(sys.version_info[:3]),
        "base_executable": base_executable,
        "base_executable_digest": base_executable_digest,
        "pyvenv_cfg_digest": _required_file_digest(pyvenv_config, "pyvenv.cfg"),
        "distributions": _installed_distributions(),
    }


def _flowmarshal_import_origin(root: Path) -> str:
    module = importlib.import_module("flowmarshal")
    raw_origin = getattr(module, "__file__", None)
    if not isinstance(raw_origin, str) or not raw_origin:
        raise WorkspaceBindingError("flowmarshal import origin을 확인할 수 없습니다.")
    try:
        origin = Path(raw_origin).resolve(strict=True)
    except OSError as error:
        raise WorkspaceBindingError(f"flowmarshal import origin이 존재하지 않습니다: {raw_origin}") from error
    expected = (root / "src" / "flowmarshal" / "__init__.py").resolve()
    if origin != expected:
        raise WorkspaceBindingError(
            "flowmarshal가 해당 worktree src에서 import되지 않았습니다: "
            f"actual={origin}, expected={expected}"
        )
    return str(origin)


def _qualification_fixture_files(root: Path) -> dict[str, str]:
    """전체 Gate가 명시적으로 읽는 ignored runtime fixture만 별도 결속한다."""

    fixtures: dict[str, str] = {}
    for test_relative, fixture_relative in _QUALIFICATION_RUNTIME_FIXTURES:
        if not (root / test_relative).is_file():
            continue
        fixture = root / fixture_relative
        if fixture.is_symlink() or not fixture.is_file():
            raise WorkspaceBindingError(
                "qualification fixture가 없거나 일반 파일이 아닙니다: "
                f"test={test_relative}, fixture={fixture_relative}"
            )
        fixtures[fixture_relative] = _required_file_digest(
            fixture, f"qualification fixture {fixture_relative}"
        )
    return fixtures


def _optional_git(root: Path, *arguments: str) -> str | None:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _starting_provenance(root: Path) -> dict[str, Any]:
    """재검사하지 않는 외부 checkout 관측을 시작 시점에만 남긴다."""

    records: list[dict[str, str | None]] = []
    current: dict[str, str | None] | None = None
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            if current is not None:
                records.append(current)
            current = {"path": line.removeprefix("worktree "), "head": None, "branch": None}
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ")
        elif current is not None and line.startswith("branch "):
            current["branch"] = line.removeprefix("branch ")
    if current is not None:
        records.append(current)
    return {
        "origin_url": _optional_git(root, "remote", "get-url", "origin"),
        "origin_main": _optional_git(root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"),
        "main": _optional_git(root, "rev-parse", "--verify", "--quiet", "refs/heads/main"),
        "worktrees": records,
    }


def _current_binding_state(root: Path) -> dict[str, Any]:
    _require_detached_worktree(root)
    _require_clean_tracked_files(root)
    manifest = source_manifest_files(root)
    return {
        "root": str(root),
        "head": _git(root, "rev-parse", "HEAD"),
        "source_manifest": manifest,
        "source_manifest_digest": sha256_digest(manifest),
        "qualification_fixture_files": _qualification_fixture_files(root),
        "python_identity": _interpreter_identity(root),
        "flowmarshal_import_origin": _flowmarshal_import_origin(root),
    }


def _require_binding_field(binding: Mapping[str, Any], name: str) -> Any:
    if name not in binding:
        raise WorkspaceBindingError(f"workspace binding에 필수 필드가 없습니다: {name}")
    return binding[name]


def _manifest_difference(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> str:
    changed = sorted(
        path for path in set(expected) & set(actual) if expected[path] != actual[path]
    )
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    details = [
        *[f"changed={path}" for path in changed[:3]],
        *[f"added={path}" for path in added[:3]],
        *[f"removed={path}" for path in removed[:3]],
    ]
    return ", ".join(details) or "digest 불일치"


def capture_workspace_binding(root: Path) -> dict:
    """실험용 detached worktree의 실행 입력과 환경을 고정해 반환한다."""

    base = _resolve_root(root)
    state = _current_binding_state(base)
    return {
        "schema_version": "inspection-workspace-binding-v1",
        **state,
        # 원격·main·다른 checkout은 비교 대상이 아니라 capture 시점 provenance다.
        "starting_provenance": _starting_provenance(base),
    }


def verify_workspace_binding(root: Path, binding: dict) -> None:
    """고정된 worktree·HEAD·source·Python·import origin이 그대로인지 검사한다."""

    if not isinstance(binding, Mapping):
        raise WorkspaceBindingError("workspace binding은 객체여야 합니다.")
    if _require_binding_field(binding, "schema_version") != "inspection-workspace-binding-v1":
        raise WorkspaceBindingError("지원하지 않는 workspace binding schema_version입니다.")
    base = _resolve_root(root)
    if _require_binding_field(binding, "root") != str(base):
        raise WorkspaceBindingError(
            "workspace root가 capture 시점과 다릅니다: "
            f"actual={base}, expected={binding['root']}"
        )

    expected_head = _require_binding_field(binding, "head")
    expected_manifest = _require_binding_field(binding, "source_manifest")
    expected_manifest_digest = _require_binding_field(binding, "source_manifest_digest")
    expected_qualification_fixtures = _require_binding_field(binding, "qualification_fixture_files")
    expected_python = _require_binding_field(binding, "python_identity")
    expected_origin = _require_binding_field(binding, "flowmarshal_import_origin")
    if not isinstance(expected_manifest, Mapping):
        raise WorkspaceBindingError("workspace binding의 source_manifest가 객체가 아닙니다.")
    if not isinstance(expected_qualification_fixtures, Mapping):
        raise WorkspaceBindingError("workspace binding의 qualification_fixture_files가 객체가 아닙니다.")
    if sha256_digest(expected_manifest) != expected_manifest_digest:
        raise WorkspaceBindingError("workspace binding의 source_manifest digest가 손상됐습니다.")

    state = _current_binding_state(base)
    if state["head"] != expected_head:
        raise WorkspaceBindingError(
            f"HEAD가 capture 시점과 다릅니다: actual={state['head']}, expected={expected_head}"
        )
    if state["source_manifest"] != dict(expected_manifest):
        raise WorkspaceBindingError(
            "실행 source_manifest가 capture 시점과 다릅니다: "
            + _manifest_difference(expected_manifest, state["source_manifest"])
        )
    if state["source_manifest_digest"] != expected_manifest_digest:
        raise WorkspaceBindingError("실행 source_manifest digest가 capture 시점과 다릅니다.")
    if state["qualification_fixture_files"] != dict(expected_qualification_fixtures):
        raise WorkspaceBindingError(
            "qualification_fixture_files가 capture 시점과 다릅니다: "
            + _manifest_difference(expected_qualification_fixtures, state["qualification_fixture_files"])
        )
    if state["python_identity"] != expected_python:
        raise WorkspaceBindingError(
            "실행 Python environment가 capture 시점과 다릅니다: "
            f"actual={state['python_identity']}, expected={expected_python}"
        )
    if state["flowmarshal_import_origin"] != expected_origin:
        raise WorkspaceBindingError(
            "flowmarshal import origin이 capture 시점과 다릅니다: "
            f"actual={state['flowmarshal_import_origin']}, expected={expected_origin}"
        )
