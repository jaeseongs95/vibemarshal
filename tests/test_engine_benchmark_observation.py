from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark_observation import (
    BenchmarkObservationError,
    observe_benchmark_execution_checkpoint,
)
from flowmarshal.engine.domain import PlanContractRevision
from flowmarshal.engine.evaluation import BenchmarkCell, EvaluationCellCheckpoint, EvaluationContract
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.smoke import run_synthetic_lifecycle


def _digest(char: str) -> str:
    return "sha256:" + char * 64


class BenchmarkObservationTests(unittest.TestCase):
    def _prepared_checkpoint(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        run_root = root / "run"
        project = run_root / "work" / "seed-1" / "scenario" / "skeleton_engine" / "attempt" / "project"
        project.mkdir(parents=True)
        (project / "AGENTS.md").write_text("합성 지침", encoding="utf-8")
        (project / "app.py").write_text("value = 1\n", encoding="utf-8")
        state_root = project.parent / "budget-state"
        database, artifacts = state_root / "flowmarshal-engine.sqlite3", state_root / "artifacts"
        status = run_synthetic_lifecycle(project_root=project, database_path=database, artifact_root=artifacts)
        project_id = status["project"]["id"]
        ledger = SQLiteEngineLedger(database, artifact_root=artifacts)
        with ledger.read() as connection:
            plan = PlanContractRevision.model_validate_json(connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE project_id=?", (project_id,)
            ).fetchone()["payload_json"])
            goal = connection.execute("SELECT goal_id,definition_digest FROM goal_revisions WHERE project_id=?", (project_id,)).fetchone()
        contract = EvaluationContract(
            model_lock_format="flowmarshal-model-lock-v2", scope="full_skeleton_to_selection_pipeline",
            fixture_digests=(_digest("1"),), scenario_set_digest=_digest("2"), order_seeds=(1,), expected_cell_count=1,
            role_configuration_digest=_digest("3"), source_manifest_digest=_digest("4"), rules_digest=_digest("5"),
            threshold_digest=_digest("6"), taxonomy_digest=_digest("7"), prompt_digest=_digest("8"),
            output_schema_digest=_digest("9"), model_lock_digest=_digest("a"),
        )
        run_root.mkdir(exist_ok=True)
        (run_root / "evaluation-contract.json").write_text(json.dumps(contract.model_dump(mode="json")), encoding="utf-8")
        (run_root / "run-metadata.json").write_text(json.dumps({"evaluation_contract_digest": contract.contract_digest, "project_root": str(project)}), encoding="utf-8")
        outcome = {"selected": plan.activation_digest}
        execution = {
            "format": "flowmarshal-benchmark-execution-checkpoint-v1", "project_id": project_id,
            "goal_id": goal["goal_id"], "goal_contract_digest": goal["definition_digest"],
            "plan_revision_id": plan.plan_revision_id, "activation_digest": plan.activation_digest,
            "workspace": str(project.resolve()), "database_path": str(database.resolve()),
            "artifact_root": str(artifacts.resolve()), "planning_outcome_digest": sha256_digest(outcome),
        }
        (project.parent / "execution-checkpoint.json").write_text(json.dumps(execution), encoding="utf-8")
        (project.parent / "selected-plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
        cell = BenchmarkCell(
            scenario_id="scenario", scenario_digest=_digest("b"), order_seed=1, path_kind="single_path",
            implementation="skeleton_engine", model_lock_digest=_digest("a"), neutral_input_digest=_digest("c"),
            functional_result_digest=_digest("d"), runner_receipt_digest=_digest("e"), expected_disposition="selected",
            disposition="selected", uncached_input_tokens=1, output_tokens=1, latency_ms_to_first_feasible=1,
            latency_ms_to_disposition=1, selected_plan_activation_digest=plan.activation_digest,
            candidate_output_tokens=1, discarded_candidate_output_tokens=0,
        )
        raw = {"scenario_id": "scenario", "order_seed": 1, "selected_activation_digest": plan.activation_digest,
               "neutral_input_digest": _digest("c"), "planning_outcome": outcome, "execution_checkpoint": execution}
        checkpoint = EvaluationCellCheckpoint(
            model_lock_format="flowmarshal-model-lock-v2", contract_digest=contract.contract_digest,
            fixture_digest=_digest("1"), order_seed=1,
            raw_structured_assessment={"benchmark_cell": cell.model_dump(mode="json"), "raw": raw},
            runner_receipts=({"receipt": "preserved"},),
        )
        return temporary, run_root, contract, checkpoint, execution

    def test_completed_ledger_adds_idempotent_evidence_without_changing_checkpoint(self):
        temporary, run_root, contract, checkpoint, _ = self._prepared_checkpoint()
        with temporary:
            original = checkpoint.model_dump(mode="json")
            first = observe_benchmark_execution_checkpoint(checkpoint, run_root=run_root, contract=contract)
            second = observe_benchmark_execution_checkpoint(checkpoint, run_root=run_root, contract=contract)
            self.assertTrue(Path(first.lifecycle_evidence_path).is_file())
        self.assertEqual("observed", first.status)
        self.assertEqual(first.cell, second.cell)
        self.assertEqual(first.lifecycle_evidence_path, second.lifecycle_evidence_path)
        self.assertEqual(original, checkpoint.model_dump(mode="json"))
        self.assertEqual("2.0", first.cell.lifecycle_observation.schema_version)

    def test_goal_and_workspace_or_planning_digest_mismatch_are_rejected(self):
        temporary, run_root, contract, checkpoint, execution = self._prepared_checkpoint()
        with temporary:
            altered = checkpoint.model_copy(deep=True)
            altered.raw_structured_assessment["raw"]["execution_checkpoint"]["goal_id"] = "goal_" + "f" * 32
            (Path(execution["workspace"]).parent / "execution-checkpoint.json").write_text(
                json.dumps(altered.raw_structured_assessment["raw"]["execution_checkpoint"]), encoding="utf-8"
            )
            with self.assertRaisesRegex(BenchmarkObservationError, "GOAL_OR_PLAN"):
                observe_benchmark_execution_checkpoint(altered, run_root=run_root, contract=contract)

            altered = checkpoint.model_copy(deep=True)
            altered.raw_structured_assessment["raw"]["planning_outcome"] = {"changed": True}
            with self.assertRaisesRegex(BenchmarkObservationError, "ORIGINAL_BINDING"):
                observe_benchmark_execution_checkpoint(altered, run_root=run_root, contract=contract)

            altered = checkpoint.model_copy(deep=True)
            altered.raw_structured_assessment["raw"]["execution_checkpoint"]["workspace"] = str(run_root / "outside" / "project")
            (Path(execution["workspace"]).parent / "execution-checkpoint.json").write_text(
                json.dumps(altered.raw_structured_assessment["raw"]["execution_checkpoint"]), encoding="utf-8"
            )
            with self.assertRaisesRegex(BenchmarkObservationError, "WORKSPACE_PATH"):
                observe_benchmark_execution_checkpoint(altered, run_root=run_root, contract=contract)


if __name__ == "__main__":
    unittest.main()
