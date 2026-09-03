from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.engine.context import ProjectMapper, goal_context_observations
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.service import EngineService

from tests.engine_helpers import profile


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "engine"


class GoalContextTests(unittest.TestCase):
    def test_goal_inputs_are_observed_before_a_goal_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            workspace = base / "project"
            workspace.mkdir()
            (workspace / "app.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
            service = EngineService(SQLiteEngineLedger(base / "engine.sqlite3"))
            service.initialize()
            project_id = service.create_project(name="관찰", root=workspace)
            service.register_profile(profile(project_id))
            observations = service.observe_goal_inputs(project_id, "app.py의 add 수정")
            file = next(fact for fact in observations if fact["kind"] == "project_file")
            self.assertEqual("app.py", file["path"])
            self.assertIn("return a - b", file["content_excerpt"])
            self.assertTrue(file["content_complete"])

    def test_observation_does_not_hide_truncation_or_accept_changed_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "app.py"
            source.write_text("value = 1\n" * 1000, encoding="utf-8")
            (root / "other.py").write_text("other = 2", encoding="utf-8")
            project_map = ProjectMapper().build(project_id="project_" + "a" * 32, root=root, revision_no=1)
            facts = goal_context_observations(project_map, "app.py", max_files=1, excerpt_chars=20)
            self.assertFalse(facts[0]["inventory_complete"])
            self.assertFalse(facts[1]["content_complete"])
            self.assertEqual(20, len(facts[1]["content_excerpt"]))
            source.write_text("value = 2", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "STALE_GOAL_INPUT"):
                goal_context_observations(project_map, "app.py")

    def test_fixture_clarification_preserves_every_expected_verdict_and_code(self):
        fields = ("fixture_id", "expected_admissible", "critical", "required_finding_codes", "allowed_correlated_codes")
        for name in ("goal-reviewer-regressions", "r31-reviewer-regressions"):
            current = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
            original = json.loads((FIXTURES / "archive" / f"{name}-v1.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [{key: case[key] for key in fields} for case in original["fixtures"]],
                [{key: case[key] for key in fields} for case in current["fixtures"]],
            )

    def test_g02_claimed_synthetic_failure_matches_provided_source(self):
        catalog = json.loads((FIXTURES / "goal-reviewer-regressions.json").read_text(encoding="utf-8"))
        case = next(item for item in catalog["fixtures"] if item["fixture_id"] == "G02-clean")
        namespace = {}
        exec(case["artifact"]["context"]["files"]["app.py"], namespace)
        self.assertEqual(-1, namespace["add"](2, 3))
        self.assertNotEqual(5, namespace["add"](2, 3))

    def test_g01_and_g08_clarify_sources_without_changing_goal_or_request(self):
        catalog = json.loads((FIXTURES / "goal-reviewer-regressions.json").read_text(encoding="utf-8"))
        prior = json.loads((FIXTURES / "archive" / "goal-reviewer-regressions-v2.json").read_text(encoding="utf-8"))
        by_id = {item["fixture_id"]: item for item in prior["fixtures"]}
        for case in catalog["fixtures"]:
            previous = by_id[case["fixture_id"]]
            self.assertEqual(previous["artifact"]["request"], case["artifact"]["request"])
            self.assertEqual(previous["artifact"]["goal"], case["artifact"]["goal"])
            for key in ("expected_admissible", "critical", "required_finding_codes", "allowed_correlated_codes"):
                self.assertEqual(previous[key], case[key])
        first = next(item for item in catalog["fixtures"] if item["fixture_id"] == "G01-clean")
        context = first["artifact"]["context"]
        namespace = {}
        exec(context["files"]["app.py"], namespace)
        self.assertEqual(-1, namespace["add"](2, 3))
        self.assertEqual(first["artifact"]["goal"]["mutation_policy"], context["project_profile"]["mutation_default"])
        last = next(item for item in catalog["fixtures"] if item["fixture_id"] == "G08-clean")
        self.assertEqual(2, len(last["artifact"]["context"]["proposed_strategies"]))
        self.assertEqual("read_only", last["artifact"]["goal"]["mutation_policy"])

    def test_planning_fixture_documents_a_real_standard_library_test(self):
        import subprocess
        import sys
        fixture = FIXTURES / "live-smoke-project"
        command = "from test_app import test_add_returns_sum; test_add_returns_sum()"
        readme = (fixture / "README.md").read_text(encoding="utf-8")
        self.assertIn(command, readme)
        result = subprocess.run((sys.executable, "-B", "-c", command), cwd=fixture,
                                capture_output=True, check=False, timeout=15)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"AssertionError", result.stderr)

    def test_p11_selection_gap_is_independent_of_duplicate_pair(self):
        catalog = json.loads((FIXTURES / "r31-reviewer-regressions.json").read_text(encoding="utf-8"))
        artifact = next(item["artifact"] for item in catalog["fixtures"] if item["fixture_id"] == "P11-adversarial")
        repaired_pair = copy.deepcopy(artifact)
        repaired_pair["candidates"][1]["strategy"] = "rebuild from verified input"
        selected = {item["strategy"] for item in repaired_pair["candidates"] if item["ref"] in repaired_pair["selected_candidate_refs"]}
        self.assertNotEqual(set(repaired_pair["selection_requirements"]), selected)
        repaired_selection = copy.deepcopy(artifact)
        repaired_selection["candidates"].append({"ref": "candidate-c", "strategy": "rebuild from verified input", "dag": ["rebuild", "validate"]})
        repaired_selection["selected_candidate_refs"].append("candidate-c")
        selected = {item["strategy"] for item in repaired_selection["candidates"] if item["ref"] in repaired_selection["selected_candidate_refs"]}
        self.assertEqual(set(repaired_selection["selection_requirements"]), selected)
        self.assertEqual(repaired_selection["candidates"][0]["strategy"], repaired_selection["candidates"][1]["strategy"])


if __name__ == "__main__":
    unittest.main()
