from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes
from scripts.diagnostics.inspection_inputs import (
    InspectionInputPackageError,
    MANIFEST_FILENAME,
    PACKAGE_SCHEMA_VERSION,
    export_fixture_package,
    main,
    materialize_fixture_package,
    verify_fixture_package,
)
from scripts.diagnostics.r_s06_10_fixtures import SOURCE_INPUT_FILENAMES


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _synthetic_source(root: Path) -> tuple[Path, Path, Path]:
    source = root / "historical-run"
    workspace = root / "historical-workspace"
    reference = root / "historical-reference" / "validation-reference.md"
    source.mkdir()
    workspace.mkdir()
    reference.parent.mkdir()
    workspace_files = {
        "AGENTS.md": b"# synthetic instruction\n",
        "app.py": b"def add(left: int, right: int) -> int:\n    return left - right\n",
        "test_app.py": b"from app import add\nassert add(2, 3) == 5\n",
    }
    for name, content in workspace_files.items():
        (workspace / name).write_bytes(content)
    reference.write_bytes(b"# fixed validation reference\n")
    entries = [
        {
            "content_digest": sha256_bytes(workspace_files["AGENTS.md"]),
            "dependency_refs": [],
            "entry_id": "entry_instruction",
            "kind": "instruction",
            "path": "AGENTS.md",
            "symbols": [],
            "tags": ["instruction_source", "md"],
        },
        {
            "content_digest": sha256_bytes(workspace_files["app.py"]),
            "dependency_refs": [],
            "entry_id": "entry_app",
            "kind": "file",
            "path": "app.py",
            "symbols": ["add"],
            "tags": ["py", "symbol_indexed"],
        },
        {
            "content_digest": sha256_bytes(workspace_files["test_app.py"]),
            "dependency_refs": [],
            "entry_id": "entry_test",
            "kind": "test",
            "path": "test_app.py",
            "symbols": ["test_add"],
            "tags": ["py", "symbol_indexed"],
        },
        {
            "content_digest": sha256_bytes(reference.read_bytes()),
            "dependency_refs": [],
            "entry_id": "entry_reference",
            "kind": "reference",
            "path": str(reference),
            "symbols": [],
            "tags": ["md", "registered_reference"],
        },
    ]
    project_map = {
        "created_at": "2026-09-04T00:00:00Z",
        "entries": entries,
        "instruction_source_refs": ["entry_instruction"],
        "project_id": "project_11111111111111111111111111111111",
        "project_map_revision_id": "project_map_22222222222222222222222222222222",
        "revision_no": 1,
        "root": str(workspace),
        "schema_version": "1.0",
    }
    for index, filename in enumerate(SOURCE_INPUT_FILENAMES):
        value = project_map if filename == "input-project-map.json" else {"filename": filename, "index": index}
        _write_json(source / filename, value)
    return source, workspace, reference.parent


class InspectionInputPackageTests(unittest.TestCase):
    def build(self) -> tuple[tempfile.TemporaryDirectory, Path, Path, Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, workspace, reference_root = _synthetic_source(root)
        package = root / "package"
        export_fixture_package(source, package)
        return temporary, source, workspace, reference_root, package

    def test_exports_only_fixed_inputs_and_registered_project_bodies(self):
        _, _, _, _, package = self.build()
        result = verify_fixture_package(package)
        manifest = json.loads((package / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(PACKAGE_SCHEMA_VERSION, result["schema_version"])
        self.assertEqual(17, result["source_input_count"])
        self.assertEqual(4, result["project_entry_count"])
        self.assertEqual(21, result["file_count"])
        self.assertEqual(
            {"source_input", "workspace_entry", "registered_reference"},
            {item["purpose"] for item in manifest["files"]},
        )
        self.assertFalse(any("calls" in item["package_path"] or "receipt" in item["package_path"]
                             for item in manifest["files"]))

    def test_materializes_after_every_original_location_is_removed_and_preserves_bytes(self):
        _, source, workspace, reference_root, package = self.build()
        expected_inputs = {name: (source / name).read_bytes() for name in SOURCE_INPUT_FILENAMES}
        expected_workspace = {name: (workspace / name).read_bytes() for name in ("AGENTS.md", "app.py", "test_app.py")}
        shutil.rmtree(source)
        shutil.rmtree(workspace)
        shutil.rmtree(reference_root)

        run = package.parent / "new-run"
        result = materialize_fixture_package(package, run)
        source_inputs = Path(result["source_inputs_dir"])
        for name, content in expected_inputs.items():
            self.assertEqual(content, (source_inputs / name).read_bytes())
        for name, content in expected_workspace.items():
            self.assertEqual(content, (Path(result["project_root"]) / name).read_bytes())
        self.assertEqual(5, len(result["path_mappings"]))
        self.assertTrue(next(row for row in result["project_entries"]
                             if row["entry_id"] == "entry_reference")["destination"].endswith("content.md"))
        self.assertEqual(
            expected_inputs["input-project-map.json"],
            (source_inputs / "input-project-map.json").read_bytes(),
            "원본 ProjectMap을 새 경로로 몰래 rewrite하면 안 된다.",
        )

    def test_changed_registered_body_stops_export_before_destination_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, workspace, _ = _synthetic_source(root)
            (workspace / "app.py").write_text("changed", encoding="utf-8")
            destination = root / "package"
            with self.assertRaisesRegex(InspectionInputPackageError, "digest"):
                export_fixture_package(source, destination)
            self.assertFalse(destination.exists())

    def test_rejects_changed_missing_and_extra_package_files(self):
        mutations = ("changed", "missing", "extra")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                _, _, _, _, package = self.build()
                target = package / "source-inputs" / SOURCE_INPUT_FILENAMES[0]
                if mutation == "changed":
                    target.write_bytes(target.read_bytes() + b"changed")
                elif mutation == "missing":
                    target.unlink()
                else:
                    (package / "raw-provider-response.json").write_text("{}", encoding="utf-8")
                with self.assertRaises(InspectionInputPackageError):
                    verify_fixture_package(package)

    def test_manifest_rejects_escape_and_duplicate_paths(self):
        for mutation in ("escape", "duplicate"):
            with self.subTest(mutation=mutation):
                _, _, _, _, package = self.build()
                manifest_path = package / MANIFEST_FILENAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "escape":
                    manifest["files"][0]["package_path"] = "../outside.json"
                    manifest["files"][0]["materialized_path"] = "../outside.json"
                else:
                    manifest["files"][1]["package_path"] = manifest["files"][0]["package_path"]
                    manifest["files"][1]["materialized_path"] = manifest["files"][0]["materialized_path"]
                _write_json(manifest_path, manifest)
                with self.assertRaises(InspectionInputPackageError):
                    verify_fixture_package(package)

    def test_manifest_provenance_must_match_the_packaged_project_map(self):
        _, _, _, _, package = self.build()
        manifest_path = package / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["project_map"]["original_root"] = str(package / "forged-workspace")
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(InspectionInputPackageError, "provenance"):
            verify_fixture_package(package)

    def test_materialize_never_overwrites_an_existing_target(self):
        _, _, _, _, package = self.build()
        run = package.parent / "new-run"
        target = run / "source-inputs" / SOURCE_INPUT_FILENAMES[0]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"user-owned")
        with self.assertRaises(FileExistsError):
            materialize_fixture_package(package, run)
        self.assertEqual(b"user-owned", target.read_bytes())
        self.assertEqual([target], [item for item in run.rglob("*") if item.is_file()])

    def test_package_symlink_is_rejected(self):
        _, _, _, _, package = self.build()
        target = package / "source-inputs" / SOURCE_INPUT_FILENAMES[0]
        path_type = type(package)
        original = path_type.is_symlink
        with patch.object(path_type, "is_symlink", autospec=True,
                          side_effect=lambda path: path == target or original(path)):
            with self.assertRaisesRegex(InspectionInputPackageError, "symlink"):
                verify_fixture_package(package)

    def test_export_and_verify_cli_use_absolute_paths_and_return_api_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = _synthetic_source(root)
            package = root / "package"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main([
                    "export", "--source-run", str(source.resolve()), "--package", str(package.resolve()),
                ]))
            exported = json.loads(output.getvalue())
            self.assertEqual(21, exported["file_count"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["verify", "--package", str(package.resolve())]))
            verified = json.loads(output.getvalue())
            self.assertEqual(exported["manifest_digest"], verified["manifest_digest"])

            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["verify", "--package", "relative-package"])


if __name__ == "__main__":
    unittest.main()
