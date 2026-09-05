from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.diagnostics.inspection_workspace import (
    WorkspaceBindingError,
    capture_workspace_binding,
    verify_workspace_binding,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(("git", "-C", str(root), *arguments), check=True, capture_output=True)


class InspectionWorkspaceTests(unittest.TestCase):
    def _detached_worktree(
        self, temporary: Path, *, qualification_fixtures: bool = False,
    ) -> tuple[Path, Path]:
        repository = temporary / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "inspection@example.test")
        _git(repository, "config", "user.name", "Inspection test")
        (repository / "src/flowmarshal").mkdir(parents=True)
        (repository / "src/flowmarshal/__init__.py").write_text("VALUE = 'initial'\n", encoding="utf-8")
        (repository / "docs").mkdir()
        (repository / "docs/orchestration-redesign.md").write_text("initial contract\n", encoding="utf-8")
        (repository / "tests/fixtures/engine").mkdir(parents=True)
        (repository / "tests/fixtures/engine/case.md").write_text("fixture input\n", encoding="utf-8")
        if qualification_fixtures:
            (repository / "tests/test_gate0c_verifier.py").write_text("# gate fixture dependency\n", encoding="utf-8")
            (repository / "tests/test_gate0c_e2e.py").write_text("# gate fixture dependency\n", encoding="utf-8")
            (repository / ".gitignore").write_text(
                "spikes/gate0c/artifacts/control/gate0c.sqlite3\n"
                "spikes/gate0c/runs/profile-20260902-r7/unregistered/secret.txt\n",
                encoding="utf-8",
            )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "initial")
        detached = temporary / "detached"
        _git(repository, "worktree", "add", "--detach", str(detached), "HEAD")
        if qualification_fixtures:
            database = detached / "spikes/gate0c/artifacts/control/gate0c.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"qualification database fixture")
            canary = detached / "spikes/gate0c/runs/profile-20260902-r7/unregistered/secret.txt"
            canary.parent.mkdir(parents=True)
            canary.write_bytes(b"qualification canary fixture")
        return repository, detached

    @contextmanager
    def _flowmarshal_from(self, root: Path):
        original_path = sys.path[:]
        original_modules = {
            name: module for name, module in sys.modules.items() if name == "flowmarshal" or name.startswith("flowmarshal.")
        }
        for name in tuple(original_modules):
            del sys.modules[name]
        sys.path.insert(0, str(root / "src"))
        importlib.invalidate_caches()
        try:
            yield
        finally:
            for name in tuple(sys.modules):
                if name == "flowmarshal" or name.startswith("flowmarshal."):
                    del sys.modules[name]
            sys.modules.update(original_modules)
            sys.path[:] = original_path
            importlib.invalidate_caches()

    @staticmethod
    def _identity(root: Path) -> dict[str, object]:
        return {
            "executable": str(root / ".venv/Scripts/python.exe"),
            "executable_digest": "sha256:" + "1" * 64,
            "prefix": str(root / ".venv"),
            "implementation": "CPython",
            "version_info": [3, 12, 0],
            "base_executable": "C:/Python/python.exe",
            "base_executable_digest": "sha256:" + "2" * 64,
            "pyvenv_cfg_digest": "sha256:" + "3" * 64,
            "distributions": [{"name": "fixture", "version": "1.0"}],
        }

    def _capture(self, worktree: Path) -> dict:
        with self._flowmarshal_from(worktree), patch(
            "scripts.diagnostics.inspection_workspace._interpreter_identity",
            side_effect=self._identity,
        ):
            return capture_workspace_binding(worktree)

    def _verify(self, worktree: Path, binding: dict) -> None:
        with self._flowmarshal_from(worktree), patch(
            "scripts.diagnostics.inspection_workspace._interpreter_identity",
            side_effect=self._identity,
        ):
            verify_workspace_binding(worktree, binding)

    def test_detached_binding_ignores_other_checkout_document_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository, worktree = self._detached_worktree(Path(temp))
            binding = self._capture(worktree)

            (repository / "docs/orchestration-redesign.md").write_text("new main contract\n", encoding="utf-8")
            _git(repository, "add", "docs/orchestration-redesign.md")
            _git(repository, "commit", "-m", "main documentation update")
            artifact = worktree / ".flowmarshal-engine-eval/runs/incomplete.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text('{"runtime": "ignored"}\n', encoding="utf-8")

            self._verify(worktree, binding)
            self.assertEqual(str(worktree.resolve()), binding["root"])
            self.assertEqual(str((worktree / "src/flowmarshal/__init__.py").resolve()), binding["flowmarshal_import_origin"])
            self.assertEqual({}, binding["qualification_fixture_files"])
            self.assertIn("starting_provenance", binding)

    def test_verify_blocks_tracked_source_mutation_and_head_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, worktree = self._detached_worktree(Path(temp))
            binding = self._capture(worktree)
            source = worktree / "src/flowmarshal/__init__.py"
            source.write_text("VALUE = 'mutated'\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceBindingError, "추적 source 파일"):
                self._verify(worktree, binding)

            _git(worktree, "add", "src/flowmarshal/__init__.py")
            _git(worktree, "commit", "-m", "detached source update")
            with self.assertRaisesRegex(WorkspaceBindingError, "HEAD"):
                self._verify(worktree, binding)

    def test_verify_blocks_flowmarshal_import_from_another_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, worktree = self._detached_worktree(Path(temp))
            binding = self._capture(worktree)
            with patch(
                "scripts.diagnostics.inspection_workspace._interpreter_identity",
                side_effect=self._identity,
            ):
                with self.assertRaisesRegex(WorkspaceBindingError, "import되지 않았습니다"):
                    verify_workspace_binding(worktree, binding)

    def test_verify_blocks_changed_python_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, worktree = self._detached_worktree(Path(temp))
            binding = self._capture(worktree)
            changed_identity = self._identity(worktree) | {"executable_digest": "sha256:" + "4" * 64}
            with self._flowmarshal_from(worktree), patch(
                "scripts.diagnostics.inspection_workspace._interpreter_identity",
                return_value=changed_identity,
            ), self.assertRaisesRegex(WorkspaceBindingError, "Python environment"):
                verify_workspace_binding(worktree, binding)

    def test_verify_blocks_missing_or_changed_qualification_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, worktree = self._detached_worktree(Path(temp), qualification_fixtures=True)
            binding = self._capture(worktree)
            self.assertEqual(
                {
                    "spikes/gate0c/artifacts/control/gate0c.sqlite3",
                    "spikes/gate0c/runs/profile-20260902-r7/unregistered/secret.txt",
                },
                set(binding["qualification_fixture_files"]),
            )

            database = worktree / "spikes/gate0c/artifacts/control/gate0c.sqlite3"
            database.write_bytes(b"changed qualification database fixture")
            with self.assertRaisesRegex(WorkspaceBindingError, "qualification_fixture_files"):
                self._verify(worktree, binding)

            database.write_bytes(b"qualification database fixture")
            (worktree / "spikes/gate0c/runs/profile-20260902-r7/unregistered/secret.txt").unlink()
            with self.assertRaisesRegex(WorkspaceBindingError, "qualification fixture가 없거나"):
                self._verify(worktree, binding)

    def test_capture_rejects_attached_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository, _ = self._detached_worktree(Path(temp))
            with patch(
                "scripts.diagnostics.inspection_workspace._interpreter_identity",
                side_effect=self._identity,
            ), self.assertRaisesRegex(WorkspaceBindingError, "detached worktree"):
                capture_workspace_binding(repository)


if __name__ == "__main__":
    unittest.main()
