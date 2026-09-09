from __future__ import annotations

import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path
import importlib.util
import os


ROOT = Path(__file__).resolve().parents[1]


class EnginePackagingContractTests(unittest.TestCase):
    def test_user_wheel_exposes_only_engine_entrypoint_and_packages(self) -> None:
        document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            document["project"]["scripts"],
            {"flowmarshal-engine": "flowmarshal.engine.console_host:main"},
        )
        self.assertEqual(
            document["tool"]["setuptools"]["packages"]["find"]["include"],
            ["flowmarshal", "flowmarshal.engine", "flowmarshal.engine.*"],
        )
        setup_source = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn('shutil.rmtree(Path(self.build_lib) / "flowmarshal", ignore_errors=True)', setup_source)
        self.assertIn('if package == "flowmarshal"', setup_source)
        self.assertIn('{"__init__", "canonical", "time"}', setup_source)
        self.assertIn("ENGINE_DEVELOPER_MODULES", setup_source)
        self.assertIn('"scope_report_verification"', setup_source)

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
