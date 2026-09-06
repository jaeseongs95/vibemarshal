from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.benchmark_lifecycle import _materialized_observations, collect_lifecycle_observation
from flowmarshal.engine.domain import PlanContractRevision, TaskExecutionSpecRevision, new_id
from flowmarshal.engine.evaluation import (
    BenchmarkCell,
    BenchmarkLifecycleObservation,
    BenchmarkMaterializedExecutionSpecObservation,
    BenchmarkTaskLifecycleObservation,
    evaluate_token_latency_gate,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.smoke import run_synthetic_lifecycle
from flowmarshal.engine.qualification import QualificationRunError


def _digest(char: str) -> str:
    return "sha256:" + char * 64


class BenchmarkLifecycleV2Tests(unittest.TestCase):
    def _completed_ledger(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        project = root / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("합성 지침", encoding="utf-8")
        (project / "app.py").write_text("value = 1\n", encoding="utf-8")
        database, artifacts = root / "state.sqlite3", root / "artifacts"
        status = run_synthetic_lifecycle(project_root=project, database_path=database, artifact_root=artifacts)
        ledger = SQLiteEngineLedger(database, artifact_root=artifacts)
        project_id = status["project"]["id"]
        with ledger.read() as connection:
            row = connection.execute(
                "SELECT p.payload_json FROM plan_revisions p JOIN plan_activations a "
                "ON a.plan_revision_id=p.id WHERE a.project_id=?", (project_id,)
            ).fetchone()
        return temporary, ledger, project_id, PlanContractRevision.model_validate_json(row["payload_json"])

    def _collect(self, ledger, project_id, plan):
        return collect_lifecycle_observation(
            ledger, project_id=project_id, plan_activation_digest=plan.activation_digest,
            model_lock_digest=_digest("a"), neutral_input_digest=_digest("b"),
        )

    def _prepend_spec(self, ledger, project_id, *, execution_attempt: str | None = None):
        """현재 성공 Spec 앞에 폐기 revision을 넣고 필요하면 실제 start receipt도 남긴다."""
        with ledger.transaction() as tx:
            base = tx.one("SELECT * FROM execution_spec_revisions WHERE is_current=1")
            task = tx.one("SELECT * FROM task_contracts WHERE id=?", (base["task_id"],))
            old_id, old_digest = new_id("execution_spec_revision"), sha256_digest({"old": new_id("ref")})
            tx.connection.execute(
                "INSERT INTO execution_spec_revisions (id,task_id,revision_no,definition_digest,payload_json,supersedes_id,created_at,is_current) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (old_id, base["task_id"], 2, old_digest, "{}", base["id"], base["created_at"]),
            )
            tx.history(project_id, "task.materialized", "execution_spec_revision", old_id,
                       {"task_id": base["task_id"], "definition_digest": old_digest})
            if execution_attempt == "failed_receipt":
                attempt_id, intent_id, receipt_id = new_id("attempt"), new_id("runtime_intent"), new_id("runtime_receipt")
                now = tx.now
                tx.connection.execute(
                    "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (attempt_id, project_id, task["plan_revision_id"], base["task_id"], old_digest, 2,
                     "execution", "failed", "{}", "implementation", "failed after worker turn", now, now, now, now),
                )
                tx.connection.execute(
                    "INSERT INTO runtime_intents VALUES (?,?,?,?,?,?,?,?,?)",
                    (intent_id, attempt_id, "start_turn", new_id("key"), _digest("c"), "{}", "received", now, now),
                )
                tx.connection.execute(
                    "INSERT INTO runtime_receipts VALUES (?,?,?,?,?,?,?)",
                    (receipt_id, intent_id, new_id("provider"), _digest("d"), "{}", "{}", now),
                )
            elif execution_attempt == "unknown":
                now = tx.now
                tx.connection.execute(
                    "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("attempt"), project_id, task["plan_revision_id"], base["task_id"], old_digest, 2,
                     "execution", "unknown", None, "external_unknown", "provider outcome missing", now, now, now, now),
                )
        return old_id

    def _validator_rebind(self, ledger, project_id):
        with ledger.transaction() as tx:
            base = tx.one("SELECT * FROM execution_spec_revisions WHERE is_current=1")
            task = tx.one("SELECT * FROM task_contracts WHERE id=?", (base["task_id"],))
            original = TaskExecutionSpecRevision.model_validate_json(base["payload_json"])
            definition = original.definition.model_copy(update={"idempotency_key": "rebound-" + "x" * 20})
            rebound = TaskExecutionSpecRevision(
                execution_spec_revision_id=new_id("execution_spec_revision"), task_id=original.task_id,
                revision_no=2, definition=definition, definition_digest=definition.definition_digest,
                supersedes_execution_spec_revision_id=original.execution_spec_revision_id, created_at=original.created_at,
            )
            tx.connection.execute("UPDATE execution_spec_revisions SET is_current=0 WHERE id=?", (base["id"],))
            tx.connection.execute(
                "INSERT INTO execution_spec_revisions VALUES (?,?,?,?,?,?,?,1)",
                (rebound.execution_spec_revision_id, rebound.task_id, rebound.revision_no, rebound.definition_digest,
                 canonical_json(rebound), original.execution_spec_revision_id, tx.now),
            )
            tx.history(project_id, "task.materialized", "execution_spec_revision", rebound.execution_spec_revision_id,
                       {"task_id": rebound.task_id, "definition_digest": rebound.definition_digest})
            validation_attempt = new_id("attempt")
            tx.connection.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (validation_attempt, project_id, task["plan_revision_id"], rebound.task_id, rebound.definition_digest, 2,
                 "validation", "succeeded", "{}", None, None, tx.now, tx.now, tx.now, tx.now),
            )
            tx.connection.execute(
                "INSERT INTO model_rebinding_selections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("model_rebinding"), project_id, task["plan_revision_id"], rebound.task_id, "validator",
                 _digest("1"), original.definition.plan_activation_digest, original.definition_digest, "validator", "low",
                 "test", _digest("2"), _digest("3"), rebound.execution_spec_revision_id, rebound.definition_digest,
                 validation_attempt, "{}", tx.now),
            )

    def test_stale_unstarted_spec_is_denominator_and_successor_is_executed(self):
        temporary, ledger, project_id, plan = self._completed_ledger()
        with temporary:
            self._prepend_spec(ledger, project_id)
            observed = self._collect(ledger, project_id, plan)
        self.assertEqual("2.0", observed.schema_version)
        self.assertEqual(2, observed.detailed_execution_spec_count)
        self.assertEqual(1, observed.unexecuted_execution_spec_count)
        self.assertEqual({"executed", "unexecuted"}, {item.execution_state for item in observed.materialized_execution_specs})

    def test_failed_worker_turn_is_executed_not_unexecuted(self):
        temporary, ledger, project_id, plan = self._completed_ledger()
        with temporary:
            self._prepend_spec(ledger, project_id, execution_attempt="failed_receipt")
            observed = self._collect(ledger, project_id, plan)
        self.assertEqual(2, observed.detailed_execution_spec_count)
        self.assertEqual(0, observed.unexecuted_execution_spec_count)
        self.assertEqual(2, sum(item.execution_state == "executed" for item in observed.materialized_execution_specs))

    def test_unresolved_attempt_effect_keeps_unexecuted_count_null(self):
        temporary, ledger, project_id, plan = self._completed_ledger()
        with temporary:
            self._prepend_spec(ledger, project_id, execution_attempt="unknown")
            observed = self._collect(ledger, project_id, plan)
        self.assertTrue(observed.has_unknown_execution_effect)
        self.assertIsNone(observed.unexecuted_execution_spec_count)

    def test_validator_only_rebind_reuses_explicit_previous_worker_receipt(self):
        temporary, ledger, project_id, plan = self._completed_ledger()
        with temporary:
            self._validator_rebind(ledger, project_id)
            observed = self._collect(ledger, project_id, plan)
        self.assertEqual(2, observed.detailed_execution_spec_count)
        rebound = next(item for item in observed.materialized_execution_specs if item.provenance == "validator_rebind_worker_turn")
        self.assertEqual("executed", rebound.execution_state)
        self.assertTrue(rebound.worker_attempt_ids)
        self.assertIsNotNone(rebound.reused_worker_execution_spec_digest)

    def test_same_goal_revision_ancestor_is_counted_and_other_goal_is_rejected(self):
        temporary, ledger, project_id, plan = self._completed_ledger()
        with temporary:
            with ledger.transaction() as tx:
                selected = dict(tx.one("SELECT * FROM plan_revisions WHERE id=?", (plan.plan_revision_id,)))
                goal = tx.one("SELECT * FROM goal_revisions WHERE project_id=? AND definition_digest=?", (project_id, plan.definition.goal_contract_digest))
                prior_goal_digest = sha256_digest({"prior-goal": project_id})
                tx.connection.execute(
                    "INSERT INTO goal_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_id("goal_revision"), goal["goal_id"], project_id, 2, prior_goal_digest, "{}", "superseded", goal["id"], tx.now, None),
                )
                ancestor = new_id("plan_revision")
                tx.connection.execute(
                    "INSERT INTO plan_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ancestor, new_id("plan"), project_id, 1, sha256_digest({"ancestor": 1}), sha256_digest({"activation": 1}),
                     canonical_json({"definition": {"goal_contract_digest": prior_goal_digest}}), "superseded", None, tx.now, None, None, None),
                )
                task_id, spec_id, spec_digest = new_id("task"), new_id("execution_spec_revision"), sha256_digest({"ancestor-spec": 1})
                tx.connection.execute(
                    "INSERT INTO task_contracts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (task_id, project_id, ancestor, "old", 0, sha256_digest({"task": 1}), "{}", "superseded", tx.now, tx.now),
                )
                tx.connection.execute(
                    "INSERT INTO execution_spec_revisions VALUES (?,?,?,?,?,?,?,0)",
                    (spec_id, task_id, 1, spec_digest, "{}", None, tx.now),
                )
                tx.history(project_id, "task.materialized", "execution_spec_revision", spec_id,
                           {"task_id": task_id, "definition_digest": spec_digest})
                selected["supersedes_id"] = ancestor
            with ledger.read() as connection:
                values = _materialized_observations(connection, project_id=project_id, selected_plan=selected,
                                                       goal_digest=plan.definition.goal_contract_digest)
            self.assertEqual(2, len(values))
            self.assertIn(spec_id, {item.execution_spec_revision_id for item in values})

            with ledger.transaction() as tx:
                other_goal_digest = sha256_digest({"other-goal": project_id})
                tx.connection.execute(
                    "INSERT INTO goal_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_id("goal_revision"), new_id("goal"), project_id, 1, other_goal_digest, "{}", "ready", None, tx.now, None),
                )
                other = new_id("plan_revision")
                tx.connection.execute(
                    "INSERT INTO plan_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (other, new_id("plan"), project_id, 1, sha256_digest({"other": 1}), sha256_digest({"other-activation": 1}),
                     canonical_json({"definition": {"goal_contract_digest": other_goal_digest}}), "superseded", None, tx.now, None, None, None),
                )
                selected["supersedes_id"] = other
            with ledger.read() as connection:
                with self.assertRaisesRegex(QualificationRunError, "Goal scope"):
                    _materialized_observations(connection, project_id=project_id, selected_plan=selected,
                                                 goal_digest=plan.definition.goal_contract_digest)

    def _v2_lifecycle(self, state: str):
        task = BenchmarkTaskLifecycleObservation(
            task_id="task_" + "2" * 32, execution_spec_revision_id="execution_spec_" + "3" * 32,
            execution_spec_digest=_digest("4"), attempt_id="attempt_" + "6" * 32,
            runtime_receipt_digest=_digest("7"), validation_result_digests=(_digest("8"),),
        )
        executed = BenchmarkMaterializedExecutionSpecObservation(
            plan_revision_id="plan_revision_" + "1" * 32,
            task_id="task_" + "2" * 32,
            execution_spec_revision_id="execution_spec_" + "3" * 32,
            execution_spec_digest=_digest("4"), materialization_event_digest=_digest("5"),
            execution_state="executed", provenance="worker_turn_receipt",
            worker_attempt_ids=(task.attempt_id,), runtime_receipt_digests=(task.runtime_receipt_digest,),
        )
        additional = BenchmarkMaterializedExecutionSpecObservation(
            plan_revision_id="plan_revision_" + "1" * 32, task_id=task.task_id,
            execution_spec_revision_id="execution_spec_" + "9" * 32, execution_spec_digest=_digest("a"),
            materialization_event_digest=_digest("b"), execution_state=state,
            provenance=("unknown_effect" if state == "unknown_effect" else "no_effect"),
        )
        return BenchmarkLifecycleObservation(
            schema_version="2.0", collector="flowmarshal.engine.lifecycle-ledger-v2",
            project_id="project_" + "9" * 32, plan_revision_id="plan_revision_" + "1" * 32,
            plan_activation_digest=_digest("a"), model_lock_digest=_digest("b"), neutral_input_digest=_digest("c"),
            tasks=(task,), materialized_execution_specs=(executed, additional), integration_validation_result_digests=(_digest("d"),),
            state_before_digest=_digest("e"), state_after_digest=_digest("f"), state_reobservation_event_digest=_digest("0"),
            goal_verdict_digest=_digest("1"), history_head_digest=_digest("2"),
        )

    def test_unknown_effect_requires_null_ratio_and_blocks_gate(self):
        lifecycle = self._v2_lifecycle("unknown_effect")
        engine = BenchmarkCell(
            scenario_id="s", scenario_digest=_digest("a"), neutral_input_digest=_digest("c"), order_seed=1,
            path_kind="single_path", implementation="skeleton_engine", model_lock_digest=_digest("b"),
            functional_result_digest=_digest("d"), runner_receipt_digest=_digest("e"), expected_disposition="selected", disposition="selected",
            uncached_input_tokens=1, output_tokens=1, latency_ms_to_first_feasible=1, latency_ms_to_disposition=1,
            selected_plan_activation_digest=_digest("a"), lifecycle_observation=lifecycle,
            lifecycle_evidence_digest=lifecycle.observation_digest, detailed_task_count=2, unexecuted_detailed_task_count=None,
            candidate_output_tokens=1, discarded_candidate_output_tokens=0,
        )
        baseline = engine.model_copy(update={"implementation": "r31_baseline", "selected_plan_activation_digest": None,
            "lifecycle_observation": None, "lifecycle_evidence_digest": None, "detailed_task_count": 0,
            "unexecuted_detailed_task_count": 0})
        report = evaluate_token_latency_gate((baseline, engine), functional_gate_passed=True)
        self.assertIsNone(report.unexecuted_detail_ratio)
        self.assertTrue(any("denominator" in item for item in report.failures))

    def test_cell_rejects_raw_count_tampering(self):
        lifecycle = self._v2_lifecycle("unexecuted")
        values = dict(
            scenario_id="s", scenario_digest=_digest("a"), neutral_input_digest=_digest("c"), order_seed=1,
            path_kind="single_path", implementation="skeleton_engine", model_lock_digest=_digest("b"),
            functional_result_digest=_digest("d"), runner_receipt_digest=_digest("e"), expected_disposition="selected", disposition="selected",
            uncached_input_tokens=1, output_tokens=1, latency_ms_to_first_feasible=1, latency_ms_to_disposition=1,
            selected_plan_activation_digest=_digest("a"), lifecycle_observation=lifecycle,
            lifecycle_evidence_digest=lifecycle.observation_digest, detailed_task_count=0, unexecuted_detailed_task_count=0,
            candidate_output_tokens=1, discarded_candidate_output_tokens=0,
        )
        with self.assertRaisesRegex(ValueError, "materialized 목록"):
            BenchmarkCell(**values)

    def test_v2_rejects_materialized_list_omitting_completed_task_worker(self):
        lifecycle = self._v2_lifecycle("unexecuted")
        raw = lifecycle.model_dump(mode="json")
        raw["materialized_execution_specs"] = raw["materialized_execution_specs"][1:]
        with self.assertRaisesRegex(ValueError, "성공 Worker Spec/Attempt"):
            BenchmarkLifecycleObservation.model_validate(raw)

    def test_v1_checkpoint_remains_readable_but_is_not_new_gate_observation(self):
        v2 = self._v2_lifecycle("unexecuted")
        raw = v2.model_dump(mode="json")
        raw.update({"schema_version": "1.0", "collector": "flowmarshal.engine.lifecycle-ledger-v1",
                    "materialized_execution_specs": []})
        v1 = BenchmarkLifecycleObservation.model_validate(raw)
        engine = BenchmarkCell(
            scenario_id="s", scenario_digest=_digest("a"), neutral_input_digest=_digest("c"), order_seed=1,
            path_kind="single_path", implementation="skeleton_engine", model_lock_digest=_digest("b"),
            functional_result_digest=_digest("d"), runner_receipt_digest=_digest("e"), expected_disposition="selected", disposition="selected",
            uncached_input_tokens=1, output_tokens=1, latency_ms_to_first_feasible=1, latency_ms_to_disposition=1,
            selected_plan_activation_digest=_digest("a"), lifecycle_observation=v1,
            lifecycle_evidence_digest=v1.observation_digest, detailed_task_count=2, unexecuted_detailed_task_count=0,
            candidate_output_tokens=1, discarded_candidate_output_tokens=0,
        )
        baseline = engine.model_copy(update={"implementation": "r31_baseline", "selected_plan_activation_digest": None,
            "lifecycle_observation": None, "lifecycle_evidence_digest": None, "detailed_task_count": 0,
            "unexecuted_detailed_task_count": 0})
        report = evaluate_token_latency_gate((baseline, engine), functional_gate_passed=True)
        self.assertEqual(0, report.lifecycle_observed_engine_cell_count)
        self.assertIsNone(report.unexecuted_detail_ratio)
        self.assertTrue(any("NOT_OBSERVED" in item for item in report.failures))


if __name__ == "__main__":
    unittest.main()
