from __future__ import annotations

import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EnginePackagingContractTests(unittest.TestCase):
    def test_user_wheel_exposes_only_engine_entrypoint_and_packages(self) -> None:
        document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            document["project"]["scripts"],
            {"flowmarshal-engine": "flowmarshal.engine.cli:main"},
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
            "FLOWMARSHAL_ENGINE_SOURCE_ROOT",
        ):
            self.assertIn(required, document)


if __name__ == "__main__":
    unittest.main()
