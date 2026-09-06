from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.domain import PlanContractRevision
from flowmarshal.engine.eval_cli import main
from flowmarshal.engine.evaluation import (
    BenchmarkCell,
    EvaluationCellCheckpoint,
    EvaluationContract,
    ImmutableCheckpointStore,
)
from flowmarshal.engine.evaluation_budget import EvaluationPolicies, metadata_with_policies
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.qualification import (
    ORDER_SEEDS,
    PlanningScenarioCatalog,
    default_role_configuration,
    source_manifest_digest,
)
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.smoke import run_synthetic_lifecycle


ROOT = Path(__file__).resolve().parents[1]


class BenchmarkObservationCliTests(unittest.TestCase):
    def _prepare_run(self, *, pending: bool = False):
        temporary = tempfile.TemporaryDirectory()
        base = Path(temporary.name)
        run_root = base / "benchmark-run"
        catalog = PlanningScenarioCatalog.load(
            ROOT / "tests/fixtures/engine/planning-scenarios.json"
        )
        scenario = next(
            item for item in catalog.scenarios if item.expected_disposition == "selected"
        )
        neutral = {
            item.scenario_id: {"scenario_id": item.scenario_id, "source": item.source_request}
            for item in catalog.scenarios
        }
        identities = {
            (item.scenario_id, implementation): sha256_digest({
                "scenario": item.scenario_digest,
                "implementation": implementation,
                "neutral": sha256_digest(neutral[item.scenario_id]),
            })
            for item in catalog.scenarios
            for implementation in ("r31_baseline", "skeleton_engine")
        }
        roles = default_role_configuration(ROOT)
        contract = EvaluationContract(
            model_lock_format="flowmarshal-model-lock-v2",
            scope="full_skeleton_to_selection_pipeline",
            fixture_digests=tuple(identities.values()),
            scenario_set_digest=sha256_digest(neutral),
            order_seeds=ORDER_SEEDS,
            expected_cell_count=36,
            role_configuration_digest=roles.configuration_digest,
            source_manifest_digest=source_manifest_digest(ROOT),
            rules_digest="sha256:" + "1" * 64,
            threshold_digest="sha256:" + "2" * 64,
            taxonomy_digest="sha256:" + "3" * 64,
            prompt_digest="sha256:" + "4" * 64,
            output_schema_digest="sha256:" + "5" * 64,
            model_lock_digest="sha256:" + "6" * 64,
        )
        store = ImmutableCheckpointStore(run_root, contract)
        store.initialize()
        policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=10_000, call_reservation_tokens=1_000),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )
        metadata = metadata_with_policies({
            "scope": "benchmark",
            "project_root": str(ROOT),
            "evaluation_contract_digest": contract.contract_digest,
            "role_configuration": roles.model_dump(mode="json"),
        }, policies)
        (run_root / "run-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (run_root / "neutral-inputs.json").write_text(
            json.dumps(neutral, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (run_root / "benchmark-run-report.json").write_text(
            '{"original":"report"}\n', encoding="utf-8"
        )
        (run_root / "benchmark-cells.json").write_text(
            '{"original":"cells"}\n', encoding="utf-8"
        )

        seed = ORDER_SEEDS[0]
        attempt = (
            run_root / "work" / f"seed-{seed}" / scenario.scenario_id
            / "skeleton_engine" / "attempt-observed"
        )
        project = attempt / "project"
        project.mkdir(parents=True)
        (project / "AGENTS.md").write_text("합성 지침\n", encoding="utf-8")
        (project / "app.py").write_text("value = 1\n", encoding="utf-8")
        state_root = attempt / "budget-state"
        database = state_root / "flowmarshal-engine.sqlite3"
        artifacts = state_root / "artifacts"
        status = run_synthetic_lifecycle(
            project_root=project,
            database_path=database,
            artifact_root=artifacts,
        )
        ledger = SQLiteEngineLedger(database, artifact_root=artifacts)
        with ledger.read() as connection:
            plan = PlanContractRevision.model_validate_json(connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE project_id=?",
                (status["project"]["id"],),
            ).fetchone()["payload_json"])
            goal = connection.execute(
                "SELECT goal_id,definition_digest FROM goal_revisions WHERE project_id=?",
                (status["project"]["id"],),
            ).fetchone()
        if pending:
            with ledger.transaction() as transaction:
                transaction.connection.execute(
                    "UPDATE projects SET run_state='active' WHERE id=?",
                    (status["project"]["id"],),
                )
        planning_outcome = {"selected": plan.activation_digest}
        execution = {
            "format": "flowmarshal-benchmark-execution-checkpoint-v1",
            "project_id": status["project"]["id"],
            "goal_id": goal["goal_id"],
            "goal_contract_digest": goal["definition_digest"],
            "plan_revision_id": plan.plan_revision_id,
            "activation_digest": plan.activation_digest,
            "workspace": str(project.resolve()),
            "database_path": str(database.resolve()),
            "artifact_root": str(artifacts.resolve()),
            "planning_outcome_digest": sha256_digest(planning_outcome),
        }
        (attempt / "execution-checkpoint.json").write_text(
            json.dumps(execution), encoding="utf-8"
        )
        (attempt / "selected-plan.json").write_text(
            plan.model_dump_json(), encoding="utf-8"
        )
        receipts = ({"call_id": "preserved-receipt", "usage_available": True},)
        cell = BenchmarkCell(
            scenario_id=scenario.scenario_id,
            scenario_digest=scenario.scenario_digest,
            order_seed=seed,
            path_kind=scenario.path_kind,
            implementation="skeleton_engine",
            model_lock_digest=contract.model_lock_digest,
            neutral_input_digest=sha256_digest(neutral[scenario.scenario_id]),
            functional_result_digest="sha256:" + "7" * 64,
            runner_receipt_digest=sha256_digest(receipts),
            expected_disposition=scenario.expected_disposition,
            disposition="selected",
            uncached_input_tokens=1,
            output_tokens=1,
            latency_ms_to_first_feasible=1,
            latency_ms_to_disposition=1,
            selected_plan_activation_digest=plan.activation_digest,
            candidate_output_tokens=1,
            discarded_candidate_output_tokens=0,
        )
        raw = {
            "scenario_id": scenario.scenario_id,
            "order_seed": seed,
            "selected_activation_digest": plan.activation_digest,
            "neutral_input_digest": cell.neutral_input_digest,
            "planning_outcome": planning_outcome,
            "execution_checkpoint": execution,
            "receipts": list(receipts),
        }
        checkpoint = EvaluationCellCheckpoint(
            model_lock_format="flowmarshal-model-lock-v2",
            contract_digest=contract.contract_digest,
            fixture_digest=identities[scenario.scenario_id, "skeleton_engine"],
            order_seed=seed,
            raw_structured_assessment={
                "benchmark_cell": cell.model_dump(mode="json"),
                "raw": raw,
            },
            runner_receipts=receipts,
        )
        checkpoint_path = store.put(checkpoint)
        protected = {
            path: path.read_bytes()
            for path in (
                run_root / "evaluation-contract.json",
                run_root / "run-metadata.json",
                run_root / "neutral-inputs.json",
                run_root / "run-state.json",
                run_root / "benchmark-run-report.json",
                run_root / "benchmark-cells.json",
                checkpoint_path,
            )
        }
        return temporary, run_root, contract, checkpoint_path, protected

    @staticmethod
    def _invoke(run_root: Path) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output), patch(
            "flowmarshal.engine.roles.CodexStructuredRoleRunner.run",
            side_effect=AssertionError("관측 CLI는 모델을 호출할 수 없습니다."),
        ):
            code = main([
                "observe-benchmark-lifecycle",
                "--project-root", str(ROOT),
                "--run-root", str(run_root),
            ])
        return code, json.loads(output.getvalue())

    def test_partial_matrix_observes_one_cell_idempotently_without_mutating_source(self):
        temporary, run_root, _contract, _checkpoint, protected = self._prepare_run()
        with temporary:
            first_code, first = self._invoke(run_root)
            assessment_root = Path(first["assessment_root"])
            self.assertEqual(
                sha256_digest(first["assessment"])[7:], assessment_root.name
            )
            generated = {
                path.relative_to(run_root): path.read_bytes()
                for directory in ("lifecycle-assessments", "lifecycle-observations")
                for path in (run_root / directory).rglob("*") if path.is_file()
            }
            second_code, second = self._invoke(run_root)
            self.assertEqual(first["assessment_root"], second["assessment_root"])
            self.assertEqual(generated, {
                path.relative_to(run_root): path.read_bytes()
                for directory in ("lifecycle-assessments", "lifecycle-observations")
                for path in (run_root / directory).rglob("*") if path.is_file()
            })
            self.assertTrue((run_root / "lifecycle-observations").is_dir())
            self.assertEqual(protected, {path: path.read_bytes() for path in protected})
        self.assertEqual((1, 1), (first_code, second_code))
        self.assertFalse(first["assessment"]["passed"])
        self.assertEqual(35, len(first["assessment"]["missing_cells"]))
        self.assertEqual("observed", first["assessment"]["observations"][0]["status"])

    def test_pending_lifecycle_and_partial_matrix_cannot_pass(self):
        temporary, run_root, _contract, _checkpoint, protected = self._prepare_run(
            pending=True
        )
        with temporary:
            code, result = self._invoke(run_root)
            self.assertEqual(protected, {path: path.read_bytes() for path in protected})
        self.assertEqual(1, code)
        self.assertFalse(result["assessment"]["passed"])
        self.assertEqual("pending", result["assessment"]["observations"][0]["status"])
        self.assertEqual(35, len(result["assessment"]["missing_cells"]))

    def test_source_metadata_roles_fixture_seed_and_receipt_mismatch_are_rejected(self):
        cases = ("source", "metadata", "roles", "fixture", "seed", "receipt")
        for case in cases:
            with self.subTest(case=case):
                temporary, run_root, contract, checkpoint_path, protected = self._prepare_run()
                with temporary:
                    if case == "source":
                        context = patch(
                            "flowmarshal.engine.eval_cli.source_manifest_digest",
                            return_value="sha256:" + "f" * 64,
                        )
                    else:
                        context = patch("flowmarshal.engine.eval_cli.source_manifest_digest", wraps=source_manifest_digest)
                    if case in {"metadata", "roles"}:
                        metadata_path = run_root / "run-metadata.json"
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        if case == "metadata":
                            metadata["project_root"] = str(run_root)
                        else:
                            metadata["role_configuration"]["normalizer"]["effort"] = "low"
                            body = dict(metadata)
                            body.pop("metadata_digest")
                            metadata["metadata_digest"] = sha256_digest(body)
                        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                    elif case in {"fixture", "seed"}:
                        changed = contract.model_copy(update=(
                            {"fixture_digests": tuple(
                                ("sha256:" + "e" * 64) if index == 0 else value
                                for index, value in enumerate(contract.fixture_digests)
                            )}
                            if case == "fixture"
                            else {"order_seeds": (1, 2, 99)}
                        ))
                        (run_root / "evaluation-contract.json").write_text(
                            changed.model_dump_json(), encoding="utf-8"
                        )
                        metadata_path = run_root / "run-metadata.json"
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        metadata["evaluation_contract_digest"] = changed.contract_digest
                        body = dict(metadata)
                        body.pop("metadata_digest")
                        metadata["metadata_digest"] = sha256_digest(body)
                        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                    elif case == "receipt":
                        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                        checkpoint["raw_structured_assessment"]["raw"]["receipts"][0]["call_id"] = "changed"
                        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                    with context:
                        code, payload = self._invoke(run_root)
                    self.assertEqual(2, code)
                    self.assertIn("error", payload)
                    self.assertFalse((run_root / "lifecycle-assessments").exists())
                    if case == "source":
                        self.assertEqual(protected, {path: path.read_bytes() for path in protected})


if __name__ == "__main__":
    unittest.main()
