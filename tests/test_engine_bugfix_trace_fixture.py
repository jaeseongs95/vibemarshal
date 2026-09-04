from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

from flowmarshal.engine.domain import EvidenceKind
from flowmarshal.engine.goal import GoalNormalizationProposal


ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / "tests/fixtures/engine/bugfix-trace/oracle.py"
spec = importlib.util.spec_from_file_location("bugfix_trace_oracle", ORACLE_PATH)
assert spec is not None and spec.loader is not None
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


class BugfixTraceFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        shutil.copytree(ROOT / "tests/fixtures/engine/project-e2e", self.workspace,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))

    def implementation(self, body: str) -> None:
        (self.workspace / "app.py").write_text(
            'def add(left: int, right: int) -> int:\n'
            '    """두 정수의 합을 반환한다."""\n\n'
            f'    {body}\n', encoding="utf-8")

    def test_contract_uses_existing_proposal_schema_without_authority_ids(self) -> None:
        contract = oracle.load_contract()
        proposal = GoalNormalizationProposal.model_validate_json(
            oracle.json.dumps(contract["expected_goal_proposal"]))
        self.assertEqual(len(proposal.hard_acceptance), 6)
        self.assertEqual(contract["authority"], "non_authoritative_goal_input")
        self.assertNotIn("plan_revision_id", contract)
        self.assertEqual(oracle.snapshot(self.workspace), contract["initial_snapshot"]["files"])
        for phase in ("task", "goal"):
            for kind in contract["oracles"][phase]["required_evidence_kinds"]:
                EvidenceKind(kind)

    def test_original_bug_is_observed_without_modifying_fixture(self) -> None:
        before = oracle.snapshot(self.workspace)
        for phase in ("task", "goal"):
            report = oracle.observe(self.workspace, phase)
            self.assertFalse(report["passed"])
            self.assertFalse(next(c for c in report["checks"] if c["name"] == "fresh_unittest")["passed"])
        self.assertEqual(before, oracle.snapshot(self.workspace))

    def test_behavior_accepts_distinct_correct_implementations(self) -> None:
        for body in ("return left + right", "return sum((left, right))"):
            with self.subTest(body=body):
                self.implementation(body)
                for phase in ("task", "goal"):
                    self.assertTrue(oracle.observe(self.workspace, phase)["passed"])

    def test_goal_rejects_constant_that_passes_existing_unittest(self) -> None:
        self.implementation("return 5")
        self.assertTrue(oracle.observe(self.workspace, "task")["passed"])
        self.assertFalse(oracle.observe(self.workspace, "goal")["passed"])

    def test_public_contract_change_is_rejected(self) -> None:
        self.implementation("return left + right")
        path = self.workspace / "app.py"
        path.write_text(path.read_text(encoding="utf-8").replace("right: int", "right: int = 0"), encoding="utf-8")
        report = oracle.observe(self.workspace, "goal")
        self.assertFalse(report["passed"])
        self.assertFalse(next(c for c in report["checks"] if c["name"] == "public_signature")["passed"])

    def test_changed_test_cannot_hide_the_bug(self) -> None:
        path = self.workspace / "test_app.py"
        path.write_text(path.read_text(encoding="utf-8").replace("assertEqual(5,", "assertEqual(-1,"), encoding="utf-8")
        report = oracle.observe(self.workspace, "task")
        self.assertTrue(next(c for c in report["checks"] if c["name"] == "fresh_unittest")["passed"])
        self.assertFalse(report["passed"])

    def test_additional_deleted_and_out_of_scope_sources_are_rejected(self) -> None:
        self.implementation("return left + right")
        extra = self.workspace / "extra.py"
        extra.write_text("VALUE = 1\n", encoding="utf-8")
        self.assertFalse(oracle.observe(self.workspace, "goal")["passed"])
        extra.unlink()
        with (self.workspace / "app.py").open("a", encoding="utf-8") as stream:
            stream.write("VALUE = 1\n")
        self.assertFalse(oracle.observe(self.workspace, "goal")["passed"])
        self.implementation("return left + right")
        (self.workspace / "AGENTS.md").unlink()
        self.assertFalse(oracle.observe(self.workspace, "goal")["passed"])
