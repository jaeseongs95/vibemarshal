from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.domain import PlanContractRevision, ProjectMapRevision
from scripts.diagnostics.inspection_inputs import export_fixture_package
from scripts.diagnostics.inspection_materialization import (
    InspectionMaterializationError,
    prepare_relocated_inputs,
)
from scripts.diagnostics.r_s06_10 import STATIC_CASES
from tests.test_engine_inspection_fixture_revision import _write_portable_source
from tests.test_engine_inspection_inputs import _synthetic_source


ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILES = ROOT / "tests/fixtures/engine/project-e2e"
REFERENCE = ROOT / "tests/fixtures/engine/plan-inspection-reference.md"


def _detached_package(root: Path) -> Path:
    source = root / "portable-source"
    source.mkdir()
    _write_portable_source(source)
    package = root / "package"
    records = [
        {
            "byte_digest": sha256_bytes((PROJECT_FILES / name).read_bytes()),
            "entry_id": entry_id,
            "materialized_path": f"workspace/{name}",
            "origin_path": str(Path(r"D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\s05-bugfix-trace-20260904\workspace") / name),
            "package_path": f"workspace/{name}",
            "purpose": "workspace_entry",
            "size": (PROJECT_FILES / name).stat().st_size,
        }
        for name, entry_id in (
            ("AGENTS.md", "entry_a1440f0933c4047ccf0dff60"),
            ("app.py", "entry_e223b090bcffafe2127c34a7"),
            ("test_app.py", "entry_306ad59137986ddd9d5cc8b8"),
        )
    ]
    reference_path = Path(r"D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\s06-bugfix-trace-20260904-v4\validation-reference.md")
    records.append({
        "byte_digest": sha256_bytes(REFERENCE.read_bytes()),
        "entry_id": "entry_ad363844d3392d9ef718d2d6",
        "materialized_path": "project-references/entry_ad363844d3392d9ef718d2d6/content.md",
        "origin_path": str(reference_path),
        "package_path": "project-references/entry_ad363844d3392d9ef718d2d6/content.md",
        "purpose": "registered_reference",
        "size": REFERENCE.stat().st_size,
    })
    contents = {
        "workspace/AGENTS.md": (PROJECT_FILES / "AGENTS.md").read_bytes(),
        "workspace/app.py": (PROJECT_FILES / "app.py").read_bytes(),
        "workspace/test_app.py": (PROJECT_FILES / "test_app.py").read_bytes(),
        "project-references/entry_ad363844d3392d9ef718d2d6/content.md": REFERENCE.read_bytes(),
    }
    with patch("scripts.diagnostics.inspection_inputs._project_files", return_value=(records, contents)):
        export_fixture_package(source, package)
    return package


class InspectionMaterializationTests(unittest.TestCase):
    def test_relocates_only_operational_bindings_and_preserves_fixture_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _detached_package(root)
            run = root / "new-run"
            result = prepare_relocated_inputs(package, run)
            proof = json.loads((run / "relocation-proof.json").read_text(encoding="utf-8"))
            original_map = json.loads((run / "fixture-revision/input-project-map.json").read_text(encoding="utf-8"))
            relocated_map = json.loads((run / "input-project-map.json").read_text(encoding="utf-8"))

            self.assertEqual({"/root", "/entries/3/path"}, set(proof["project_map"]["changed_fields"]))
            self.assertEqual(result["project_root"], relocated_map["root"])
            self.assertEqual(original_map["entries"][:3], relocated_map["entries"][:3])
            self.assertEqual(result["project_map_digest"], ProjectMapRevision.model_validate(relocated_map).revision_digest)
            self.assertEqual(set(STATIC_CASES), set(result["static_case_plan_digests"]))
            self.assertTrue(all(value == "PASS" for value in proof["plan_gate"].values()))
            self.assertTrue(all(value == "PASS" for value in proof["verify_reviewed_case"].values()))
            self.assertEqual(sha256_digest({
                key: value for key, value in proof.items() if key != "proof_digest"
            }), proof["proof_digest"])

            for name in STATIC_CASES:
                before_path = run / "fixture-revision" / f"input-{name}-plan.json"
                after_path = run / f"input-{name}-plan.json"
                before = json.loads(before_path.read_text(encoding="utf-8"))
                after = json.loads(after_path.read_text(encoding="utf-8"))
                self.assertEqual(before["plan_id"], after["plan_id"])
                self.assertEqual(before["plan_revision_id"], after["plan_revision_id"])
                self.assertNotEqual(before["definition_digest"], after["definition_digest"])
                self.assertEqual(result["project_map_digest"], after["definition"]["project_map_digest"])
                PlanContractRevision.model_validate(after)
            for filename in ("input-goal.json", "input-state.json", "input-skeleton.json",
                             "expectations.json", "input-historical-r-s06-09-clean-plan.json",
                             "source-input-goal.json", "source-input-clean-plan.json"):
                self.assertEqual(
                    (run / "fixture-revision" / filename).read_bytes(),
                    (run / filename).read_bytes(),
                    filename,
                )

    def test_reports_user_goal_physical_root_conflict_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = _synthetic_source(root)
            project_map = json.loads((source / "input-project-map.json").read_text(encoding="utf-8"))
            files = json.loads((ROOT / "tests/fixtures/engine/plan-inspection-regressions.json").read_text(
                encoding="utf-8"
            ))["files"]
            goal = deepcopy(files["input-goal.json"])
            source_request = goal["definition"]["source_request"] + f" 대상 root는 {project_map['root']}이다."
            goal["definition"]["source_request"] = source_request
            goal["definition"]["source_request_digest"] = sha256_bytes(source_request.encode("utf-8"))
            user_trace = next(item for item in goal["definition"]["source_traces"]
                              if item["source_ref"] == "user-request")
            user_trace["statement"] = source_request
            user_trace["source_digest"] = goal["definition"]["source_request_digest"]
            goal["definition_digest"] = sha256_digest(goal["definition"])
            for filename in ("input-goal.json", "input-semantic-explicit-goal.json"):
                (source / filename).write_text(
                    json.dumps(goal, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
            package = root / "package"
            export_fixture_package(source, package)
            with self.assertRaisesRegex(InspectionMaterializationError, "USER_GOAL_PHYSICAL_ROOT_CONFLICT"):
                prepare_relocated_inputs(package, root / "run")


if __name__ == "__main__":
    unittest.main()
