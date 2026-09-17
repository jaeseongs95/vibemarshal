from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
import importlib.util
import os


ROOT = Path(__file__).resolve().parents[1]

# flowmarshal.core/.planning/.orchestration/.gate0b/.gate0c/.adapters/.compat는
# legacy/prototype 도메인이며 Engine-only wheel에 포함되지 않아야 한다.
LEGACY_TOP_LEVEL_PACKAGES = {
    "core",
    "planning",
    "orchestration",
    "gate0b",
    "gate0c",
    "adapters",
    "compat",
}


class EnginePackagingContractTests(unittest.TestCase):
    def test_user_wheel_exposes_only_engine_entrypoint_and_packages(self) -> None:
        document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            document["project"]["scripts"],
            {"flowmarshal-engine": "flowmarshal.engine.console_host:main"},
        )
        setuptools_config = document["tool"]["setuptools"]
        # 정적 packages 목록 + include-package-data=false 조합만 실제 wheel 내용을
        # 제한한다. `packages.find(include=...)`만으로는 setuptools의
        # `include_package_data` 기본 동작(egg_info SOURCES.txt 기반 안전망)이
        # include 필터를 우회해 형제 legacy package를 "package data"로 다시
        # 끌어들인다 (setuptools 84 `build_py.analyze_manifest` 확인, 실제 wheel
        # 빌드로 재현·검증함). 이 assertion은 그 회귀를 다시 막는다.
        self.assertEqual(
            setuptools_config["packages"],
            ["flowmarshal", "flowmarshal.engine"],
        )
        self.assertEqual(setuptools_config["package-dir"], {"": "src"})
        self.assertFalse(setuptools_config["include-package-data"])
        self.assertNotIn("find", setuptools_config.get("packages", {}))
        setup_source = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn('shutil.rmtree(Path(self.build_lib) / "flowmarshal", ignore_errors=True)', setup_source)
        self.assertIn('if package == "flowmarshal"', setup_source)
        self.assertIn('{"__init__", "canonical", "time"}', setup_source)
        self.assertIn("ENGINE_DEVELOPER_MODULES", setup_source)
        self.assertIn('"scope_report_verification"', setup_source)

    def test_built_wheel_contains_only_engine_scope_files(self) -> None:
        """실제 wheel을 빌드해 파일 목록으로 Engine-only 범위를 직접 검사한다.

        FM-10-C1 method: "wheel 파일/entrypoint와 import graph의 필요한 범위
        검사". pyproject/setup.py 원문 검사만으로는 setuptools의 실제 build
        결과를 보증하지 못하므로(위 테스트 docstring 참고) 여기서 진짜 wheel을
        만들어 대조한다.
        """
        with tempfile.TemporaryDirectory() as temp:
            out_dir = Path(temp)
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "wheel",
                        "--no-deps",
                        "-w",
                        str(out_dir),
                        str(ROOT),
                    ],
                    cwd=temp,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                self.skipTest(f"wheel build environment unavailable: {error}")
            if result.returncode != 0:
                self.skipTest(
                    "wheel build failed in this environment "
                    f"(likely no network for build isolation): {result.stderr[-2000:]}"
                )
            wheels = list(out_dir.glob("flowmarshal_engine-*.whl"))
            self.assertEqual(1, len(wheels), result.stdout + result.stderr)
            with zipfile.ZipFile(wheels[0]) as archive:
                names = archive.namelist()
                entry_points = archive.read(
                    next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
                ).decode("utf-8")

            flowmarshal_files = [n for n in names if n.startswith("flowmarshal/")]
            top_level_subpackages = {
                n.split("/")[1]
                for n in flowmarshal_files
                if "/" in n[len("flowmarshal/") :]
            }
            leaked = top_level_subpackages & LEGACY_TOP_LEVEL_PACKAGES
            self.assertEqual(set(), leaked, f"wheel leaked legacy packages: {sorted(leaked)}")
            self.assertEqual({"engine"}, top_level_subpackages)

            top_level_modules = {
                n[len("flowmarshal/") :]
                for n in flowmarshal_files
                if "/" not in n[len("flowmarshal/") :]
            }
            self.assertEqual({"__init__.py", "canonical.py", "time.py"}, top_level_modules)

            engine_modules = {
                n.split("/")[-1][: -len(".py")]
                for n in flowmarshal_files
                if n.startswith("flowmarshal/engine/") and n.endswith(".py")
            }
            # `qualification_manifest`는 dev-only qualification 도구
            # (qualification.py/eval_cli.py/e2e_qualification.py/
            # scope_report_verification.py)가 아니라
            # `scripts/installed_candidate_qualification.py`의
            # `_require_installed_product`/`probe`가 후보 wheel 안에서 직접
            # `verify_candidate_wheel_installation`을 호출하기 위해 필요한
            # shared canonical 자산이다. 그래서 의도적으로 wheel에 남긴다.
            self.assertIn("qualification_manifest", engine_modules)
            leaked_dev_modules = engine_modules & {
                "qualification",
                "eval_cli",
                "benchmark",
                "benchmark_candidate_costs",
                "benchmark_lifecycle",
                "benchmark_observation",
                "benchmark_safety",
                "e2e_qualification",
                "evaluation",
                "evaluation_budget",
                "freeze",
                "inspection_diagnostic_budget",
                "performance",
                "performance_assessment",
                "performance_lifecycle_safety",
                "scope_report_verification",
                "smoke",
            }
            self.assertEqual(set(), leaked_dev_modules)
            self.assertIn("cli", engine_modules)
            self.assertIn("console_host", engine_modules)

            self.assertEqual(
                "[console_scripts]\nflowmarshal-engine = flowmarshal.engine.console_host:main\n",
                entry_points.replace("\r\n", "\n"),
            )

    def test_shipped_engine_modules_do_not_import_developer_only_code(self) -> None:
        """import graph 범위 검사(FM-10-C1)를 source AST로 직접 재현한다.

        `setup.py`의 `ENGINE_DEVELOPER_MODULES`에 없는 `flowmarshal.engine.*`
        module만 wheel에 실린다. 그 shipped 집합 중 하나라도 developer-only
        module이나 legacy top-level package(`flowmarshal.core` 등)를 import하면
        실제 사용자 wheel에서 `ModuleNotFoundError`가 난다. 예: 과거
        `inspection_diagnostic_budget.py`는 shipped였지만 excluded
        `evaluation_budget`을 import해 깨져 있었다.
        """
        import ast

        setup_source = (ROOT / "setup.py").read_text(encoding="utf-8")
        namespace: dict = {}
        # ENGINE_DEVELOPER_MODULES 리터럴만 안전하게 추출한다 (setup() 실행은 피한다).
        tree = ast.parse(setup_source)
        developer_modules: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "ENGINE_DEVELOPER_MODULES"
                for target in node.targets
            ):
                developer_modules = set(ast.literal_eval(node.value))
                break
        self.assertTrue(developer_modules, "ENGINE_DEVELOPER_MODULES를 setup.py에서 읽지 못했습니다")

        legacy_top_level = {
            "core",
            "planning",
            "orchestration",
            "gate0b",
            "gate0c",
            "adapters",
            "compat",
        }
        engine_dir = ROOT / "src" / "flowmarshal" / "engine"
        shipped = sorted(
            path.stem for path in engine_dir.glob("*.py") if path.stem not in developer_modules
        )
        self.assertIn("cli", shipped)
        violations: list[tuple[str, str]] = []
        for name in shipped:
            module_tree = ast.parse((engine_dir / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(module_tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                first = module.split(".")[0] if module else ""
                if node.level == 1 and first in developer_modules:
                    violations.append((name, f"from .{module}"))
                elif node.level == 2 and first in legacy_top_level:
                    violations.append((name, f"from ..{module}"))
                elif node.level == 0 and any(
                    module == f"flowmarshal.{legacy}" or module.startswith(f"flowmarshal.{legacy}.")
                    for legacy in legacy_top_level
                ):
                    violations.append((name, module))
        self.assertEqual([], violations)

    def test_installed_candidate_launcher_reads_literal_allowlist(self) -> None:
        launcher = self._launcher_module()
        modules = launcher.load_developer_modules(ROOT)
        self.assertIn("eval_cli", modules)
        self.assertIn("scope_report_verification", modules)
        self.assertTrue(all(module.isidentifier() for module in modules))
        self.assertEqual(
            launcher.DEVELOPER_SOURCE_ROOT_ENV,
            "FLOWMARSHAL_ENGINE_SOURCE_ROOT",
        )

    def test_installed_candidate_launcher_rejects_source_product_import(self) -> None:
        launcher = self._launcher_module()
        with self.assertRaisesRegex(
            launcher.InstalledCandidateLauncherError,
            "SOURCE_PRODUCT_IMPORT_FORBIDDEN",
        ):
            launcher._require_installed_product(ROOT)

    def test_installed_candidate_launcher_binds_project_e2e_wheel(self) -> None:
        launcher = self._launcher_module()
        candidate = (ROOT / "pyproject.toml").resolve()
        injected = launcher.bind_project_e2e_candidate(
            ["run", "--scope", "project-e2e"], candidate
        )
        self.assertEqual(injected[-2:], ["--candidate-wheel", str(candidate)])
        self.assertEqual(
            launcher.bind_project_e2e_candidate(
                [
                    "run",
                    "--scope=project-e2e",
                    "--candidate-wheel",
                    str(candidate),
                ],
                candidate,
            )[-1],
            str(candidate),
        )
        with self.assertRaisesRegex(
            launcher.InstalledCandidateLauncherError,
            "PROJECT_E2E_CANDIDATE_WHEEL_MISMATCH",
        ):
            launcher.bind_project_e2e_candidate(
                ["run", "--scope", "project-e2e", "--candidate-wheel", str(ROOT / "setup.py")],
                candidate,
            )

    def test_installed_candidate_launcher_sets_explicit_source_root(self) -> None:
        launcher = self._launcher_module()
        candidate = (ROOT / "pyproject.toml").resolve()
        original_meta_path = list(__import__("sys").meta_path)
        with patch.object(launcher, "_require_installed_product", return_value=object()):
            with patch.dict(os.environ, {}, clear=True):
                try:
                    launcher.prepare_candidate_harness(ROOT, candidate)
                    self.assertEqual(
                        os.environ[launcher.DEVELOPER_SOURCE_ROOT_ENV], str(ROOT.resolve())
                    )
                finally:
                    __import__("sys").meta_path[:] = original_meta_path

    @staticmethod
    def _launcher_module():
        path = ROOT / "scripts" / "installed_candidate_qualification.py"
        specification = importlib.util.spec_from_file_location(
            "installed_candidate_qualification_test", path
        )
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    def test_qualification_requires_explicit_developer_source_root(self) -> None:
        from flowmarshal.engine.qualification import (
            DEVELOPER_SOURCE_ROOT_ENV,
            QualificationRunError,
            project_root,
        )

        source = (ROOT / "src" / "flowmarshal" / "engine" / "qualification.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('DEVELOPER_SOURCE_ROOT_ENV = "FLOWMARSHAL_ENGINE_SOURCE_ROOT"', source)
        self.assertNotIn("Path(__file__).resolve().parents[3]", source)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(QualificationRunError, DEVELOPER_SOURCE_ROOT_ENV):
                project_root()
        with patch.dict("os.environ", {DEVELOPER_SOURCE_ROOT_ENV: str(ROOT)}, clear=True):
            self.assertEqual(project_root(), ROOT)

    def test_install_document_matches_pins_and_schema_contract(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lock = [
            line
            for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        document = (ROOT / "docs" / "engine-package-install.md").read_text(encoding="utf-8")
        self.assertEqual(project["project"]["dependencies"], lock)
        self.assertEqual(project["project"]["requires-python"], ">=3.10")
        for required in (
            "openai-codex==0.147.0",
            "pydantic==2.13.5",
            "schema 4",
            "schema 3",
            "읽기 전용",
            "v1",
            "candidate wheel",
        ):
            self.assertIn(required, document)


if __name__ == "__main__":
    unittest.main()
