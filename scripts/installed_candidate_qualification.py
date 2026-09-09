"""설치된 candidate wheel로 개발 qualification 진입점을 검증한다.

이 launcher는 사용자 배포 wheel에 개발용 qualification 모듈을 다시 넣지
않는다. 대신 clean candidate wheel Python에서 제품 모듈을 먼저 고정한 뒤,
명시한 source root의 ``ENGINE_DEVELOPER_MODULES``만 meta-path hook으로
읽어 ``flowmarshal.engine.eval_cli``를 실행한다.

실제 provider 호출은 하지 않는다. ``probe``는 eval CLI의 의존성 import와
candidate wheel의 설치/바이트 결속을 검사하는 dry mode다. ``eval``은 같은
격리 경계에서 나머지 eval_cli 인자를 전달한다.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.abc
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable


DEVELOPER_MODULES_NAME = "ENGINE_DEVELOPER_MODULES"
DEVELOPER_PACKAGE = "flowmarshal.engine"
DEVELOPER_SOURCE_ROOT_ENV = "FLOWMARSHAL_ENGINE_SOURCE_ROOT"
REQUIRED_DEVELOPER_MODULES = frozenset({"eval_cli", "scope_report_verification"})


class InstalledCandidateLauncherError(RuntimeError):
    """source harness와 installed candidate의 경계 위반."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _source_product_root(source_root: Path) -> Path:
    root = source_root.resolve(strict=True)
    product = (root / "src" / "flowmarshal").resolve(strict=True)
    engine = product / "engine"
    if not (root / "setup.py").is_file() or not engine.is_dir():
        raise InstalledCandidateLauncherError("SOURCE_ROOT_ENGINE_LAYOUT_REQUIRED")
    return product


def load_developer_modules(source_root: Path) -> frozenset[str]:
    """setup.py를 실행하지 않고 명시 개발 allowlist를 읽는다."""
    root = source_root.resolve(strict=True)
    _source_product_root(root)
    try:
        tree = ast.parse((root / "setup.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        raise InstalledCandidateLauncherError("DEVELOPER_ALLOWLIST_UNREADABLE") from error
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == DEVELOPER_MODULES_NAME
            for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        )
    ]
    if len(assigned) != 1:
        raise InstalledCandidateLauncherError("DEVELOPER_ALLOWLIST_DECLARATION_REQUIRED")
    try:
        values = ast.literal_eval(assigned[0])
    except ValueError as error:
        raise InstalledCandidateLauncherError("DEVELOPER_ALLOWLIST_LITERAL_REQUIRED") from error
    if not isinstance(values, (set, frozenset, tuple, list)):
        raise InstalledCandidateLauncherError("DEVELOPER_ALLOWLIST_COLLECTION_REQUIRED")
    modules = frozenset(values)
    if (
        not modules
        or not all(isinstance(item, str) and item.isidentifier() for item in modules)
        or not REQUIRED_DEVELOPER_MODULES.issubset(modules)
    ):
        raise InstalledCandidateLauncherError("DEVELOPER_ALLOWLIST_INVALID")
    engine = root / "src" / "flowmarshal" / "engine"
    if any(not (engine / f"{module}.py").is_file() for module in modules):
        raise InstalledCandidateLauncherError("DEVELOPER_ALLOWLIST_SOURCE_MISSING")
    return modules


class DeveloperSourceFinder(importlib.abc.MetaPathFinder):
    """명시 allowlist의 top-level engine module만 source에서 제공한다."""

    def __init__(self, *, source_root: Path, developer_modules: Iterable[str]) -> None:
        self.source_root = source_root.resolve(strict=True)
        self.engine_root = (
            self.source_root / "src" / "flowmarshal" / "engine"
        ).resolve(strict=True)
        self.developer_modules = frozenset(developer_modules)

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        prefix = f"{DEVELOPER_PACKAGE}."
        if not fullname.startswith(prefix):
            return None
        module = fullname.removeprefix(prefix)
        # Dotted/nested requests must never escape the declared source files.
        if "." in module or module not in self.developer_modules:
            return None
        candidate = (self.engine_root / f"{module}.py").resolve(strict=True)
        if not _is_within(candidate, self.engine_root):
            raise InstalledCandidateLauncherError("DEVELOPER_SOURCE_PATH_ESCAPE")
        return importlib.util.spec_from_file_location(fullname, candidate)


def _remove_source_product_paths(source_root: Path) -> None:
    source_root = source_root.resolve(strict=True)
    source_src = (source_root / "src").resolve(strict=True)
    retained: list[str] = []
    for entry in sys.path:
        try:
            current = Path(entry or ".").resolve(strict=True)
        except OSError:
            retained.append(entry)
            continue
        if current in {source_root, source_src}:
            continue
        retained.append(entry)
    sys.path[:] = retained


def _require_installed_product(source_root: Path) -> ModuleType:
    product = _source_product_root(source_root)
    try:
        module = importlib.import_module("flowmarshal.engine.qualification_manifest")
        package = importlib.import_module("flowmarshal")
        module_file = Path(str(module.__file__)).resolve(strict=True)
        package_file = Path(str(package.__file__)).resolve(strict=True)
    except (ModuleNotFoundError, OSError, TypeError) as error:
        raise InstalledCandidateLauncherError("CANDIDATE_PRODUCT_IMPORT_REQUIRED") from error
    if _is_within(module_file, product) or _is_within(package_file, product):
        raise InstalledCandidateLauncherError("SOURCE_PRODUCT_IMPORT_FORBIDDEN")
    return module


def _assert_source_modules_are_allowlisted(
    source_root: Path, developer_modules: frozenset[str]
) -> None:
    product = _source_product_root(source_root)
    prefix = f"{DEVELOPER_PACKAGE}."
    for name, module in tuple(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str) or not name.startswith("flowmarshal"):
            continue
        try:
            is_source = _is_within(Path(module_file), product)
        except OSError:
            is_source = False
        if not is_source:
            continue
        if not name.startswith(prefix):
            raise InstalledCandidateLauncherError("SOURCE_PRODUCT_IMPORT_FORBIDDEN")
        module_name = name.removeprefix(prefix)
        if "." in module_name or module_name not in developer_modules:
            raise InstalledCandidateLauncherError("SOURCE_DEVELOPER_IMPORT_NOT_ALLOWLISTED")


def prepare_candidate_harness(source_root: Path, candidate_wheel: Path) -> tuple[ModuleType, frozenset[str]]:
    """installed product 검증 뒤 허용 개발 source hook을 설치한다."""
    root = source_root.resolve(strict=True)
    if not candidate_wheel.is_absolute():
        raise InstalledCandidateLauncherError("CANDIDATE_WHEEL_ABSOLUTE_PATH_REQUIRED")
    _remove_source_product_paths(root)
    manifest = _require_installed_product(root)
    developer_modules = load_developer_modules(root)
    # qualification.py는 자동 cwd 추론을 금지한다. 이 launcher가 확인한 source
    # root만 developer harness의 명시 입력으로 전달한다.
    os.environ[DEVELOPER_SOURCE_ROOT_ENV] = str(root)
    sys.meta_path.insert(0, DeveloperSourceFinder(source_root=root, developer_modules=developer_modules))
    return manifest, developer_modules


def probe(source_root: Path, candidate_wheel: Path) -> dict[str, object]:
    """installed candidate import와 source-only eval dependency loading을 검증한다."""
    manifest, developer_modules = prepare_candidate_harness(source_root, candidate_wheel)
    binding = manifest.verify_candidate_wheel_installation(candidate_wheel)
    eval_cli = importlib.import_module("flowmarshal.engine.eval_cli")
    _assert_source_modules_are_allowlisted(source_root, developer_modules)
    return {
        "mode": "probe",
        "candidate_wheel_binding": binding.model_dump(mode="json"),
        "eval_cli_source": str(Path(str(eval_cli.__file__)).resolve(strict=True)),
        "developer_modules": sorted(developer_modules),
    }


def _option_values(arguments: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, value in enumerate(arguments):
        if value == option:
            if index + 1 >= len(arguments):
                raise InstalledCandidateLauncherError(f"{option.upper().replace('-', '_')}_VALUE_REQUIRED")
            values.append(arguments[index + 1])
        elif value.startswith(f"{option}="):
            values.append(value.removeprefix(f"{option}="))
    return values


def bind_project_e2e_candidate(arguments: list[str], candidate_wheel: Path) -> list[str]:
    """outer candidate와 project-e2e의 eval_cli 입력을 하나로 고정한다."""
    if not arguments or arguments[0] != "run":
        return arguments
    scopes = _option_values(arguments, "--scope")
    if not scopes or scopes[-1] != "project-e2e":
        return arguments
    if len(scopes) != 1:
        raise InstalledCandidateLauncherError("PROJECT_E2E_SCOPE_DUPLICATE")
    outer = candidate_wheel.resolve(strict=True)
    inner_values = _option_values(arguments, "--candidate-wheel")
    if len(inner_values) > 1:
        raise InstalledCandidateLauncherError("PROJECT_E2E_CANDIDATE_WHEEL_DUPLICATE")
    if not inner_values:
        return [*arguments, "--candidate-wheel", str(outer)]
    try:
        inner = Path(inner_values[0])
        if not inner.is_absolute() or inner.resolve(strict=True) != outer:
            raise InstalledCandidateLauncherError("PROJECT_E2E_CANDIDATE_WHEEL_MISMATCH")
    except OSError as error:
        raise InstalledCandidateLauncherError("PROJECT_E2E_CANDIDATE_WHEEL_MISMATCH") from error
    return arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="installed candidate wheel qualification launcher")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--candidate-wheel", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("probe", help="provider 호출 없이 import와 wheel binding만 검사")
    evaluate = subparsers.add_parser("eval", help="source eval_cli에 남은 인자를 전달")
    evaluate.add_argument("eval_arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "probe":
            print(json.dumps(probe(arguments.source_root, arguments.candidate_wheel), ensure_ascii=False, indent=2))
            return 0
        prepare_candidate_harness(arguments.source_root, arguments.candidate_wheel)
        eval_cli = importlib.import_module("flowmarshal.engine.eval_cli")
        _assert_source_modules_are_allowlisted(arguments.source_root, load_developer_modules(arguments.source_root))
        forwarded = list(arguments.eval_arguments)
        # launcher argparse와 eval_cli argparse의 option 경계를 위해 쓴 ``--``는
        # eval_cli 자체의 인자로 전달하지 않는다.
        if forwarded[:1] == ["--"]:
            forwarded = forwarded[1:]
        forwarded = bind_project_e2e_candidate(forwarded, arguments.candidate_wheel)
        return int(eval_cli.main(forwarded))
    except InstalledCandidateLauncherError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
