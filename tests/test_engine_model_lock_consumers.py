from __future__ import annotations

import unittest

from flowmarshal.engine.domain import IntegrationValidationContract, RunOnceAction, ThreadBinding
from flowmarshal.engine.execution import ExecutionProposalAdapter, execution_context
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import EngineDispatcher, RuntimePolicyError
from tests import test_engine_execution_automation as automation_tests
from tests.test_engine_model_lock import unrelated_change


class ModelLockConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = automation_tests.ExecutionAutomationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_materialization_dispatch_and_both_resume_paths_accept_audit_drift(self):
        for resume_kind in ("internal", "public"):
            with self.subTest(resume=resume_kind):
                prepared, runtime = self.fixture.prepared(resume_kind)
                plan_before = execution_context(prepared.service, prepared.project_id)["plan"]
                dispatcher = EngineDispatcher(prepared.service, runtime)
                self.assertEqual(RunOnceAction.MATERIALIZED, dispatcher.run_once(
                    prepared.project_id, proposal=prepared.proposal).action)
                runtime.inventory = unrelated_change(runtime.inventory)
                result = dispatcher.run_once(prepared.project_id)
                self.assertEqual(RunOnceAction.DISPATCHED, result.action)
                self.assertEqual(1, runtime.create_calls)
                row, spec = dispatcher._attempt_context(result.attempt_id)
                binding = ThreadBinding.model_validate_json(row["binding_json"])
                runtime.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
                # 이미 추가한 무관 모델을 삭제하여 resume 시에도 다른 전체 digest를 사용한다.
                runtime.inventory = self.fixture.inventory
                if resume_kind == "internal":
                    dispatcher._resume_bound_attempt(row, spec)
                else:
                    dispatcher.resume_attempt(result.attempt_id)
                self.assertEqual(1, runtime.resume_calls)
                self.assertEqual(1, runtime.create_calls)
                self.assertEqual(plan_before, execution_context(prepared.service, prepared.project_id)["plan"])

    def test_dispatch_and_resume_block_selected_or_runtime_changes_before_effects(self):
        for change in ("model", "effort", "executable", "capability"):
            with self.subTest(change=change):
                prepared, runtime = self.fixture.prepared(change)
                dispatcher = EngineDispatcher(prepared.service, runtime)
                dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
                baseline = runtime.inventory
                selected = self.fixture.roles.executor
                if change == "model":
                    updated = baseline.model_copy(update={"models": tuple(x for x in baseline.models if x.model != selected.model)})
                elif change == "effort":
                    updated = baseline.model_copy(update={"models": tuple(x.model_copy(update={
                        "supported_efforts": tuple(e for e in x.supported_efforts if e != selected.effort)})
                        if x.model == selected.model else x for x in baseline.models)})
                elif change == "executable":
                    updated = baseline.model_copy(update={"executable_digest": "sha256:" + "f" * 64})
                else:
                    updated = baseline.model_copy(update={"runtime_capabilities": baseline.runtime_capabilities[1:]})
                runtime.inventory = updated
                with self.assertRaises(RuntimePolicyError):
                    dispatcher.run_once(prepared.project_id)
                self.assertEqual(0, runtime.create_calls)
                self.assertEqual(0, runtime.resume_calls)
                runtime.inventory = baseline
                dispatched = dispatcher.run_once(prepared.project_id)
                row, spec = dispatcher._attempt_context(dispatched.attempt_id)
                binding = ThreadBinding.model_validate_json(row["binding_json"])
                runtime.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
                runtime.inventory = updated
                for resume in (lambda: dispatcher._resume_bound_attempt(row, spec),
                               lambda: dispatcher.resume_attempt(dispatched.attempt_id)):
                    with self.assertRaises(RuntimePolicyError):
                        resume()
                self.assertEqual(1, runtime.create_calls)
                self.assertEqual(0, runtime.resume_calls)

    def test_independent_goal_test_accepts_audit_drift_and_blocks_used_model_change(self):
        for changed_selection in (False, True):
            with self.subTest(changed_selection=changed_selection):
                contract = IntegrationValidationContract(
                    validation_id="validation_goal", statement="독립적으로 공개 계약 유지 여부를 검토한다.",
                    criterion_refs=("ac_fix",), method="semantic", required_evidence_kinds=("model_review",),
                )
                prepared, runtime = self.fixture.prepared(str(changed_selection), goal_validation=contract)
                self.fixture.task_completed(prepared, runtime)
                with prepared.service.ledger.read() as connection:
                    evidence_id = connection.execute(
                        "SELECT id FROM evidence_records WHERE task_id = ? AND kind = 'test'", (prepared.task_id,),
                    ).fetchone()[0]
                runner = ScriptedStructuredRoleRunner({
                    "goal_test_preparation": [{"step": {"validation_id": "validation_goal", "method": "semantic",
                        "semantic_instruction": "직접 테스트 관측으로 공개 계약 유지 여부를 확인한다.",
                        "required_evidence_kinds": ["model_review"]}}],
                    "goal_validator": [{"passed": True, "rationale": "직접 관측을 확인했다.", "evidence_refs": [evidence_id]}],
                })
                provider = ExecutionProposalAdapter(prepared.service, runner, self.fixture.roles)
                dispatcher = EngineDispatcher(prepared.service, runtime, proposal_provider=provider)
                self.assertEqual(RunOnceAction.MATERIALIZED, dispatcher.run_once(prepared.project_id).action)
                runtime.inventory = unrelated_change(runtime.inventory)
                if changed_selection:
                    runtime.inventory = runtime.inventory.model_copy(update={"models": tuple(
                        x for x in runtime.inventory.models if x.model != self.fixture.roles.validator.model)})
                observed = dispatcher.run_once(prepared.project_id)
                if changed_selection:
                    self.assertEqual(RunOnceAction.BLOCKED, observed.action)
                    self.assertEqual(1, len(runner.calls))
                else:
                    self.assertEqual(RunOnceAction.VALIDATED, observed.action)
                    self.assertEqual("goal_validator", runner.calls[-1].role)
                    receipt_binding = runner.calls[-1].operational_binding
                    self.assertEqual(runtime.inventory.inventory_digest, receipt_binding.inventory_digest)


if __name__ == "__main__":
    unittest.main()
