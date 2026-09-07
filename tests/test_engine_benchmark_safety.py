from __future__ import annotations

import tempfile
import unittest
import shutil
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark_lifecycle import collect_lifecycle_observation
from flowmarshal.engine.benchmark_safety import (
    capture_planning_safety_checkpoint,
    observe_benchmark_safety,
)
from flowmarshal.engine.budget import BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.domain import BudgetStage, utc_now
from flowmarshal.engine.e2e_qualification import _prepare
from flowmarshal.engine.evaluation import BenchmarkCell, EvaluationCellCheckpoint
from flowmarshal.engine.evaluation_budget import (
    EvaluationPolicies,
    initialize_cell_budget,
    register_and_attach_goal,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.operation_trace import OperationTrace
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import RoleCallReceipt, RoleCallRequest, strict_json_output_schema
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeObservation
from tests.engine_helpers import goal, profile
from tests.test_engine_qualification import qualification_inventory


def _digest(character: str) -> str:
    return "sha256:" + character * 64


class BenchmarkSafetyCollectorTests(unittest.TestCase):
    """실제 평가 원장이 아닌 독립 임시 원장으로 collector 경계를 검증한다."""

    def _prepared(self, *, trace: bool = True, expected_disposition: str = "blocked"):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        run_root = root / "run"
        work = run_root / "work" / "seed-1" / "safety-scenario" / "skeleton_engine" / "attempt"
        project = work / "project"
        project.mkdir(parents=True)
        project_id = "project_" + "1" * 32
        profile_revision = profile(project_id)
        goal_revision = goal(project_id, profile_revision.definition_digest, read_only=True)
        policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )
        service, manager = initialize_cell_budget(
            state_root=work / "budget-state",
            workspace=project,
            project_id=project_id,
            profile=profile_revision,
            policies=policies,
        )
        register_and_attach_goal(service, manager, goal_revision)
        request = RoleCallRequest(
            role="goal_normalizer",
            instructions="안전성 collector 합성 역할",
            payload={"case": "safety"},
            output_schema={"type": "object", "properties": {}},
            model="safety-model",
            effort="medium",
            inventory_digest=_digest("a"),
            cwd=str(project),
            timeout_seconds=30,
            timeout_policy_digest=policies.role_timeouts.policy_digest,
        )
        call_key = "safety-call-1"
        provider_call_id = manager.reserve(
            project_id=project_id,
            goal_id=goal_revision.goal_id,
            goal_digest=goal_revision.definition_digest,
            call_key=call_key,
            role=request.role,
            request=request.model_dump(mode="json"),
            stage=BudgetStage.GOAL_NORMALIZATION,
        )
        operation_trace = None
        operation_trace_ref = None
        if trace:
            trace_path = (
                service.ledger.artifact_root
                / "operation-traces"
                / f"{provider_call_id}.operation-trace.jsonl"
            )
            trace_document = OperationTrace(
                context={"call_id": call_key, "provider_call_id": provider_call_id},
                path=trace_path,
                expected_operations=("create", "start", "sdk_wait"),
            )
            for kind, turn_id in (
                ("create", None),
                ("start", "turn-safety"),
                ("sdk_wait", "turn-safety"),
            ):
                token = trace_document.begin(
                    kind,
                    {"request_digest": request.request_digest, "operation": kind},
                    call_id=call_key,
                    provider_call_id=provider_call_id,
                    thread_id="thread-safety",
                    turn_id=turn_id,
                    deadline_seconds=30,
                )
                trace_document.finish(token, response={"status": "completed"})
            operation_trace = trace_document.seal()
            operation_trace_ref = str(trace_path.resolve())
        receipt = RoleCallReceipt(
            call_id=call_key,
            role=request.role,
            status="succeeded",
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id="thread-safety",
            turn_ids=("turn-safety",),
            input_digest=request.request_digest,
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            timeout_policy_digest=request.timeout_policy_digest,
            input_tokens=5,
            cached_input_tokens=1,
            output_tokens=3,
            reasoning_tokens=1,
            usage_available=True,
            latency_ms=10,
            schema_recovery_attempts=0,
            recorded_at=utc_now(),
            operation_trace=operation_trace,
            operation_trace_ref=operation_trace_ref,
            operation_trace_digest=(
                None if operation_trace is None else sha256_digest(operation_trace)
            ),
        )
        manager.settle(provider_call_id, receipt)
        receipts = [receipt.model_dump(mode="json")]
        raw = {
            "passed": True,
            "receipts": receipts,
            "goal_preparation": {
                "goal_contract": {
                    "project_id": project_id,
                    "goal_id": goal_revision.goal_id,
                }
            },
        }
        safety_checkpoint = capture_planning_safety_checkpoint(
            work_root=work,
            implementation="skeleton_engine",
            raw=raw,
            policies=policies,
        )
        raw["safety_checkpoint"] = safety_checkpoint
        cell = BenchmarkCell(
            scenario_id="safety-scenario",
            scenario_digest=_digest("b"),
            neutral_input_digest=_digest("c"),
            order_seed=1,
            path_kind="single_path",
            implementation="skeleton_engine",
            model_lock_digest=_digest("d"),
            functional_result_digest=_digest("e"),
            runner_receipt_digest=sha256_digest(receipts),
            expected_disposition=expected_disposition,
            disposition=expected_disposition,
            uncached_input_tokens=4,
            output_tokens=3,
            latency_ms_to_first_feasible=(10 if expected_disposition == "selected" else None),
            latency_ms_to_disposition=12,
            selected_plan_activation_digest=(
                _digest("f") if expected_disposition == "selected" else None
            ),
            candidate_output_tokens=3,
            discarded_candidate_output_tokens=0,
        )
        checkpoint = EvaluationCellCheckpoint(
            model_lock_format="flowmarshal-model-lock-v2",
            contract_digest=_digest("0"),
            fixture_digest=_digest("1"),
            order_seed=1,
            raw_structured_assessment={"raw": raw},
            runner_receipts=tuple(receipts),
        )
        return temporary, run_root, work, policies, cell, checkpoint, receipt

    def _observe(self, checkpoint, cell, run_root, policies, *, stage="planning"):
        return observe_benchmark_safety(
            checkpoint=checkpoint,
            cell=cell,
            run_root=run_root,
            policies=policies,
            assessment_stage=stage,
        )

    def test_planning_checkpoint_observes_one_manifest_cell_from_real_temp_ledger(self):
        temporary, run_root, _work, policies, cell, checkpoint, _receipt = self._prepared()
        with temporary:
            observation, evidence = self._observe(checkpoint, cell, run_root, policies)
        self.assertEqual(("safety-scenario", 1, "skeleton_engine"), observation.identity)
        self.assertTrue(observation.complete)
        self.assertTrue(observation.safety_passed)
        self.assertEqual(0, observation.planning_counters.deadline_violation_count)
        self.assertEqual(4, observation.planning_usage.uncached_input_tokens)
        self.assertEqual([], evidence["failures"])

    def test_missing_trace_is_not_converted_to_zero_safety_counters(self):
        temporary, run_root, _work, policies, cell, checkpoint, _receipt = self._prepared(trace=False)
        with temporary:
            observation, _evidence = self._observe(checkpoint, cell, run_root, policies)
        self.assertFalse(observation.complete)
        self.assertIn("PERFORMANCE_OPERATION_TRACE_MISSING_OR_UNBOUND", observation.not_observed)
        self.assertIsNone(observation.planning_counters.deadline_violation_count)

    def test_usage_conflict_is_detected_from_the_captured_ledger(self):
        temporary, run_root, work, policies, cell, checkpoint, _receipt = self._prepared()
        with temporary:
            ledger = SQLiteEngineLedger(
                work / "budget-state" / "flowmarshal-engine.sqlite3",
                artifact_root=work / "budget-state" / "artifacts",
            )
            with ledger.transaction() as transaction:
                transaction.connection.execute(
                    "UPDATE provider_calls SET actual_tokens=9",
                )
            raw = checkpoint.raw_structured_assessment["raw"]
            raw["safety_checkpoint"] = capture_planning_safety_checkpoint(
                work_root=work,
                implementation="skeleton_engine",
                raw=raw,
                policies=policies,
            )
            observation, _evidence = self._observe(checkpoint, cell, run_root, policies)
        self.assertFalse(observation.safety_passed)
        self.assertIn("PERFORMANCE_ACTUAL_USAGE_CONFLICT", observation.failures)

    def test_original_receipt_binding_tamper_is_not_observed_as_a_valid_cell(self):
        temporary, run_root, _work, policies, cell, checkpoint, _receipt = self._prepared()
        with temporary:
            altered = checkpoint.model_copy(deep=True)
            altered.raw_structured_assessment["raw"]["receipts"][0]["call_id"] = "rewritten"
            observation, _evidence = self._observe(altered, cell, run_root, policies)
        self.assertFalse(observation.complete)
        self.assertTrue(any("PERFORMANCE_ORIGINAL_RECEIPT_BINDING_INVALID" in item for item in observation.not_observed))

    def test_history_tamper_is_reported_from_the_original_ledger_snapshot(self):
        temporary, run_root, work, policies, cell, checkpoint, _receipt = self._prepared()
        with temporary:
            ledger = SQLiteEngineLedger(
                work / "budget-state" / "flowmarshal-engine.sqlite3",
                artifact_root=work / "budget-state" / "artifacts",
            )
            with ledger.transaction() as transaction:
                transaction.connection.execute("DROP TRIGGER tr_engine_history_no_update")
                transaction.connection.execute(
                    "UPDATE history_events SET event_hash=? WHERE sequence=1",
                    (_digest("f"),),
                )
            observation, _evidence = self._observe(checkpoint, cell, run_root, policies)
        self.assertFalse(observation.complete)
        self.assertTrue(any("PERFORMANCE_PLANNING_LEDGER_CHANGED" in item for item in observation.not_observed))

    def test_planning_usage_record_change_is_reported_from_the_original_snapshot(self):
        temporary, run_root, work, policies, cell, checkpoint, _receipt = self._prepared()
        with temporary:
            ledger = SQLiteEngineLedger(
                work / "budget-state" / "flowmarshal-engine.sqlite3",
                artifact_root=work / "budget-state" / "artifacts",
            )
            with ledger.transaction() as transaction:
                transaction.connection.execute(
                    "UPDATE budget_usage SET payload_json=?",
                    ('{"changed":"after-planning-checkpoint"}',),
                )
            observation, _evidence = self._observe(checkpoint, cell, run_root, policies)
        self.assertFalse(observation.complete)
        self.assertTrue(any(
            "PERFORMANCE_PLANNING_USAGE_LEDGER_CHANGED" in item
            for item in observation.not_observed
        ))

    def test_missing_trace_file_is_not_observed_as_a_complete_planning_cell(self):
        temporary, run_root, _work, policies, cell, checkpoint, receipt = self._prepared()
        with temporary:
            Path(receipt.operation_trace_ref).unlink()
            observation, _evidence = self._observe(checkpoint, cell, run_root, policies)
        self.assertFalse(observation.complete)
        self.assertIn(
            "PERFORMANCE_OPERATION_TRACE_FILE_MISSING_OR_UNBOUND",
            observation.not_observed,
        )

    def test_append_after_trace_seal_is_detected_from_current_artifact_file(self):
        temporary, run_root, _work, policies, cell, checkpoint, receipt = self._prepared()
        with temporary:
            trace = OperationTrace.from_path(receipt.operation_trace_ref)
            token = trace.begin(
                "read",
                {"operation": "unexpected-after-terminal"},
                call_id=receipt.call_id,
                thread_id=receipt.thread_id,
                turn_id=receipt.turn_ids[0],
                deadline_seconds=5,
            )
            trace.finish(token, response={"status": "completed"})
            trace.seal()
            observation, _evidence = self._observe(checkpoint, cell, run_root, policies)
        self.assertFalse(observation.safety_passed)
        self.assertTrue(any(
            "PERFORMANCE_POST_TERMINAL_OPERATION" in item
            for item in observation.failures
        ))

    def test_final_blocked_cell_is_not_applicable_but_selected_cell_requires_lifecycle(self):
        temporary, run_root, _work, policies, blocked, checkpoint, _receipt = self._prepared()
        with temporary:
            blocked_observation, evidence = self._observe(
                checkpoint, blocked, run_root, policies, stage="final"
            )
        self.assertTrue(blocked_observation.complete)
        self.assertEqual(0, blocked_observation.lifecycle_counters.timeout_count)
        self.assertIn("lifecycle_not_applicable", evidence)

        temporary, run_root, _work, policies, selected, checkpoint, _receipt = self._prepared(
            expected_disposition="selected"
        )
        with temporary:
            selected_observation, _evidence = self._observe(
                checkpoint, selected, run_root, policies, stage="final"
            )
        self.assertFalse(selected_observation.complete)
        self.assertIn("PERFORMANCE_LIFECYCLE_NOT_OBSERVED", selected_observation.not_observed)

    def test_final_selected_lifecycle_is_complete_from_fake_runtime_ledger_receipts_and_traces(self):
        """합성 runtime도 실제 원장·receipt·trace를 모두 통과해야 final pass가 된다."""

        class UsageRuntime(FakeCodexRuntime):
            def __init__(self, inventory):
                super().__init__(inventory)
                self._prompt_digests = {}

            def start_turn(self, **kwargs):
                receipt = super().start_turn(**kwargs)
                self._prompt_digests[kwargs["thread_id"]] = receipt.payload["prompt_digest"]
                return receipt

            def read(self, *, thread_id: str) -> RuntimeObservation:
                observation = super().read(thread_id=thread_id)
                payload = dict(observation.payload)
                payload.update({
                    "prompt_digest": self._prompt_digests[thread_id],
                    "usage_scope": "turn",
                    "usage_source": "safety-test-runtime",
                    "usage": {
                        "inputTokens": 7, "cachedInputTokens": 2,
                        "outputTokens": 3, "reasoningOutputTokens": 1, "totalTokens": 10,
                    },
                    "duration_ms": 9,
                })
                return observation.model_copy(update={"payload": payload})

        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        run_root = root / "run"
        work = run_root / "work" / "seed-1" / "safety-scenario" / "skeleton_engine" / "attempt"
        project = work / "project"
        fixture = Path(__file__).resolve().parent / "fixtures" / "engine" / "project-e2e"
        shutil.copytree(fixture, project)
        policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )
        inventory = qualification_inventory()
        prepared = _prepare(
            workspace=project,
            state_root=work / "budget-state",
            inventory=inventory,
            roles=default_role_configuration(Path(__file__).resolve().parents[1]),
            evaluation_policies=policies,
        )
        manager = BudgetManager(prepared.service)
        with prepared.service.ledger.read() as connection:
            goal_row = connection.execute(
                "SELECT goal_id, definition_digest FROM goal_revisions WHERE project_id=?",
                (prepared.project_id,),
            ).fetchone()
        request = RoleCallRequest(
            role="goal_normalizer", instructions="실제 planning budget receipt",
            payload={"case": "selected-final"}, output_schema={"type": "object", "properties": {}},
            model="safety-model", effort="medium", inventory_digest=_digest("a"), cwd=str(project),
            timeout_seconds=30, timeout_policy_digest=policies.role_timeouts.policy_digest,
        )
        call_id = manager.reserve(
            project_id=prepared.project_id, goal_id=goal_row["goal_id"],
            goal_digest=goal_row["definition_digest"], call_key="planning-safety-call",
            role=request.role, request=request.model_dump(mode="json"), stage=BudgetStage.GOAL_NORMALIZATION,
        )
        trace_path = prepared.service.ledger.artifact_root / "operation-traces" / "planning-safety.operation-trace.jsonl"
        trace = OperationTrace(
            context={"call_id": "planning-safety-call", "provider_call_id": call_id}, path=trace_path,
            expected_operations=("create", "start", "sdk_wait"),
        )
        for kind, turn in (("create", None), ("start", "planning-turn"), ("sdk_wait", "planning-turn")):
            token = trace.begin(kind, {"operation": kind}, call_id="planning-safety-call",
                                provider_call_id=call_id, thread_id="planning-thread", turn_id=turn,
                                deadline_seconds=30)
            trace.finish(token, response={"status": "completed"})
        planning_trace = trace.seal()
        receipt = RoleCallReceipt(
            call_id="planning-safety-call", role=request.role, status="succeeded", model=request.model,
            effort=request.effort, inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access", approval_policy="never", thread_id="planning-thread",
            turn_ids=("planning-turn",), input_digest=request.request_digest,
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            timeout_policy_digest=request.timeout_policy_digest, input_tokens=5, cached_input_tokens=1,
            output_tokens=3, reasoning_tokens=1, usage_available=True, latency_ms=10,
            schema_recovery_attempts=0, recorded_at=utc_now(), operation_trace=planning_trace,
            operation_trace_ref=str(trace_path.resolve()), operation_trace_digest=sha256_digest(planning_trace),
        )
        manager.settle(call_id, receipt)
        receipts = [receipt.model_dump(mode="json")]
        raw = {"passed": True, "receipts": receipts, "goal_preparation": {"goal_contract": {
            "project_id": prepared.project_id, "goal_id": goal_row["goal_id"],
        }}}
        raw["safety_checkpoint"] = capture_planning_safety_checkpoint(
            work_root=work, implementation="skeleton_engine", raw=raw, policies=policies,
        )
        runtime = UsageRuntime(inventory)
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            binding = connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
            ).fetchone()["binding_json"]
        from flowmarshal.engine.domain import ThreadBinding
        thread = ThreadBinding.model_validate_json(binding).thread_id
        (project / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n", encoding="utf-8",
        )
        runtime.complete(thread, response="worker completed")
        actions = []
        for _ in range(12):
            outcome = dispatcher.run_once(
                prepared.project_id,
                goal_validation_step=prepared.proposal.validation_steps[0].model_copy(
                    update={"validation_id": "validation_goal"}
                ),
            )
            actions.append(outcome.action.value)
            if prepared.service.status(prepared.project_id)["project"]["run_state"] == "completed":
                break
        self.assertEqual("completed", prepared.service.status(prepared.project_id)["project"]["run_state"], actions)
        lifecycle = collect_lifecycle_observation(
            prepared.service.ledger, project_id=prepared.project_id,
            plan_activation_digest=prepared.activation_digest, model_lock_digest=_digest("d"),
            neutral_input_digest=_digest("c"),
        )
        cell = BenchmarkCell(
            scenario_id="safety-scenario", scenario_digest=_digest("b"), neutral_input_digest=_digest("c"),
            order_seed=1, path_kind="single_path", implementation="skeleton_engine", model_lock_digest=_digest("d"),
            functional_result_digest=_digest("e"), runner_receipt_digest=sha256_digest(receipts),
            expected_disposition="selected", disposition="selected", uncached_input_tokens=4, output_tokens=3,
            latency_ms_to_first_feasible=10, latency_ms_to_disposition=12,
            selected_plan_activation_digest=prepared.activation_digest, lifecycle_observation=lifecycle,
            lifecycle_evidence_digest=lifecycle.observation_digest, detailed_task_count=1,
            unexecuted_detailed_task_count=0, candidate_output_tokens=3, discarded_candidate_output_tokens=0,
        )
        checkpoint = EvaluationCellCheckpoint(
            model_lock_format="flowmarshal-model-lock-v2", contract_digest=_digest("0"), fixture_digest=_digest("1"),
            order_seed=1, raw_structured_assessment={"raw": raw}, runner_receipts=tuple(receipts),
        )
        with temporary:
            observation, _evidence = self._observe(checkpoint, cell, run_root, policies, stage="final")
        self.assertTrue(observation.complete, observation.not_observed)
        self.assertTrue(observation.safety_passed, observation.failures)


if __name__ == "__main__":
    unittest.main()
