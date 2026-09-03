from __future__ import annotations

import json
import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine.domain import (
    ExternalValidationObservation, IntegrationValidationContract, ManualValidationObservation,
    RunOnceAction, ThreadBinding, ValidationExecutionStep, utc_now,
)
from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.service import EngineServiceError
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.execution import (
    ExecutionProposalAdapter, ProviderExecutionPreparation, compile_task_preparation,
    execution_context, task_preparation_payload,
)
from pydantic import ValidationError
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]


class ExecutionAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.inventory = qualification_inventory()
        self.roles = default_role_configuration(ROOT)

    def prepared(self, name="work", *, goal_validation=None, semantic_task_validation=False):
        base = self.base / name
        base.mkdir()
        workspace, _ = _copy_fixture(ROOT, base)
        prepared = _prepare(workspace=workspace, state_root=base / "state", inventory=self.inventory,
                            roles=self.roles, goal_validation=goal_validation, semantic_task_validation=semantic_task_validation)
        return prepared, FakeCodexRuntime(self.inventory)

    def validating(self, prepared, runtime):
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)).fetchone()
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        (prepared.workspace / "app.py").write_text("def add(left: int, right: int) -> int:\n    return left + right\n", encoding="utf-8")
        runtime.complete(binding.thread_id)
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)
        return dispatcher

    def task_completed(self, prepared, runtime):
        dispatcher = self.validating(prepared, runtime)
        self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)
        return dispatcher

    @staticmethod
    def provider_response(prepared):
        proposal = prepared.proposal.model_dump(mode="json")
        for step in proposal["validation_steps"]:
            step.pop("method")
            step.pop("required_evidence_kinds")
        return {"proposal": proposal, "context_request": None}

    @staticmethod
    def task_contract(prepared):
        from flowmarshal.engine.domain import PlanContractRevision
        context = execution_context(prepared.service, prepared.project_id)
        task = PlanContractRevision.model_validate(context["plan"]).definition.tasks[0]
        return context, task

    def test_auto_materialization_has_intent_and_no_worker_dispatch(self):
        prepared, runtime = self.prepared()
        runner = ScriptedStructuredRoleRunner({"execution_preparation": [
            self.provider_response(prepared),
        ]})
        original = runner.run

        def verify_intent(*args, **kwargs):
            with prepared.service.ledger.read() as connection:
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type = 'operation.prepared'").fetchone()[0])
            return original(*args, **kwargs)

        runner.run = verify_intent
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)
        outcome = EngineDispatcher(prepared.service, runtime, proposal_provider=provider).run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.MATERIALIZED, outcome.action)
        self.assertIsNotNone(outcome.execution_spec_revision_id)
        self.assertEqual(0, runtime.create_calls)
        self.assertEqual(prepared.task_id, runner.calls[0].payload["ready_task"]["task_id"])
        self.assertNotIn("plan", runner.calls[0].payload)
        self.assertNotIn("goal", runner.calls[0].payload)
        self.assertNotIn("Goal Test", runner.calls[0].instructions)
        import sys
        self.assertEqual(sys.executable, runner.calls[0].payload["runtime_observations"]["python_executable"])
        self.assertTrue(prepared.service.ledger.verify_history(prepared.project_id))

    def test_provider_compilation_preserves_manual_validation_contract(self):
        prepared, _ = self.prepared(semantic_task_validation=True)
        _, task = self.task_contract(prepared)
        raw = ProviderExecutionPreparation.model_validate(self.provider_response(prepared))
        result = compile_task_preparation(raw, task)
        self.assertEqual(prepared.proposal, result.proposal)
        self.assertEqual({v.validation_id: (v.method, set(v.required_evidence_kinds)) for v in task.validations},
                         {v.validation_id: (v.method, set(v.required_evidence_kinds)) for v in result.proposal.validation_steps})

    def test_audit_f01_and_missing_duplicate_validation_ids_are_rejected(self):
        prepared, _ = self.prepared(semantic_task_validation=True)
        _, task = self.task_contract(prepared)
        for case in ("goal_test_added", "missing", "duplicate"):
            with self.subTest(case=case):
                response = self.provider_response(prepared)
                steps = response["proposal"]["validation_steps"]
                if case == "goal_test_added":
                    extra = copy.deepcopy(steps[0])
                    extra["validation_id"] = "validation_goal"
                    steps.append(extra)
                elif case == "missing":
                    steps.pop()
                else:
                    steps.append(copy.deepcopy(steps[0]))
                with self.assertRaises((EngineServiceError, ValidationError)):
                    compile_task_preparation(ProviderExecutionPreparation.model_validate(response), task)

    def test_provider_cannot_submit_validation_authority_fields_or_other_task(self):
        prepared, _ = self.prepared()
        _, task = self.task_contract(prepared)
        for key, value in (("method", "deterministic"), ("required_evidence_kinds", ["test"])):
            with self.subTest(key=key):
                response = self.provider_response(prepared)
                response["proposal"]["validation_steps"][0][key] = value
                with self.assertRaises(ValidationError):
                    ProviderExecutionPreparation.model_validate(response)
        response = self.provider_response(prepared)
        response["proposal"]["task_id"] = "task_" + "f" * 32
        with self.assertRaisesRegex(EngineServiceError, "Task binding"):
            compile_task_preparation(ProviderExecutionPreparation.model_validate(response), task)

    def test_task_projection_contains_relevant_criteria_and_predecessor_evidence_only(self):
        prepared, _ = self.prepared()
        context, task = self.task_contract(prepared)
        context["goal"]["definition"]["hard_acceptance"].append({"criterion_id": "unrelated", "statement": "다른 작업"})
        context["plan"]["definition"]["tasks"].append({"task_id": "unrelated-task"})
        context["predecessor_outputs"] = [{"producer_task_id": "completed-task", "products": ["input"], "evidence": [{"evidence_id": "direct"}]}]
        payload = task_preparation_payload(context, task)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("validation_goal", serialized)
        self.assertNotIn("unrelated-task", serialized)
        self.assertNotIn("unrelated", serialized)
        self.assertEqual(list(task.goal_criterion_refs), [v["criterion_id"] for v in payload["goal_projection"]["hard_acceptance"]])
        self.assertEqual(context["predecessor_outputs"], payload["predecessor_outputs"])

    def test_preparation_rejects_source_change_during_role_call(self):
        prepared, runtime = self.prepared()
        runner = ScriptedStructuredRoleRunner({"execution_preparation": [self.provider_response(prepared)]})
        original = runner.run
        def change_source(*args, **kwargs):
            result = original(*args, **kwargs)
            with (prepared.workspace / "app.py").open("a", encoding="utf-8") as stream:
                stream.write("\n# 준비 중 입력 변경\n")
            return result
        runner.run = change_source
        with self.assertRaisesRegex(EngineServiceError, "STALE_EXECUTION_INPUT"):
            ExecutionProposalAdapter(prepared.service, runner, self.roles).prepare_task(
                project_id=prepared.project_id, task_id=prepared.task_id, inventory=self.inventory)
        self.assertEqual(0, runtime.create_calls)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0])

    def test_unprojected_authority_is_bound_and_completed_response_is_not_reused(self):
        prepared, _ = self.prepared()
        context, _ = self.task_contract(prepared)
        runner = ScriptedStructuredRoleRunner({"execution_preparation": [self.provider_response(prepared), self.provider_response(prepared)]})
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)
        for revision in (1, 2):
            current = copy.deepcopy(context)
            current["unprojected_authority_revision"] = revision
            with patch("flowmarshal.engine.execution.execution_context", return_value=current):
                provider.prepare_task(project_id=prepared.project_id, task_id=prepared.task_id, inventory=self.inventory)
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(runner.calls[0].payload, runner.calls[1].payload)
        with prepared.service.ledger.read() as connection:
            requests = [json.loads(row[0])["request"] for row in connection.execute(
                "SELECT payload_json FROM history_events WHERE event_type = 'operation.prepared'")]
        self.assertNotEqual(requests[0]["authority_context_digest"], requests[1]["authority_context_digest"])

    def test_unprojected_authority_change_during_call_is_rejected(self):
        prepared, _ = self.prepared()
        context, _ = self.task_contract(prepared)
        changed = copy.deepcopy(context)
        changed["unprojected_authority_revision"] = 2
        runner = ScriptedStructuredRoleRunner({"execution_preparation": [self.provider_response(prepared)]})
        with patch("flowmarshal.engine.execution.execution_context", side_effect=[context, changed]):
            with self.assertRaisesRegex(EngineServiceError, "STALE_EXECUTION_INPUT"):
                ExecutionProposalAdapter(prepared.service, runner, self.roles).prepare_task(
                    project_id=prepared.project_id, task_id=prepared.task_id, inventory=self.inventory)

    def test_provider_operational_fields_must_match_core_selected_method(self):
        prepared, _ = self.prepared()
        _, task = self.task_contract(prepared)
        response = self.provider_response(prepared)
        response["proposal"]["validation_steps"][0]["semantic_instruction"] = "명령 대신 의미 검토"
        with self.assertRaises(ValidationError):
            compile_task_preparation(ProviderExecutionPreparation.model_validate(response), task)

    def test_goal_preparation_rejects_changed_validation_contract(self):
        for field in ("validation_id", "method", "required_evidence_kinds"):
            with self.subTest(field=field):
                prepared, _ = self.prepared(field)
                step = prepared.proposal.validation_steps[0].model_dump(mode="json")
                step["validation_id"] = "validation_goal"
                if field == "validation_id":
                    step[field] = "validation_unittest"
                elif field == "required_evidence_kinds":
                    step[field] = ["file"]
                else:
                    step = {"validation_id": "validation_goal", "method": "semantic",
                            "semantic_instruction": "변경된 검사", "required_evidence_kinds": ["test"]}
                runner = ScriptedStructuredRoleRunner({"goal_test_preparation": [{"step": step}]})
                with self.assertRaisesRegex(EngineServiceError, "integration validation"):
                    ExecutionProposalAdapter(prepared.service, runner, self.roles).prepare_goal(
                        project_id=prepared.project_id, validation_id="validation_goal", inventory=self.inventory)

    def test_missing_context_returns_structured_request_without_guessing(self):
        prepared, runtime = self.prepared()
        runner = ScriptedStructuredRoleRunner({"execution_preparation": [{
            "proposal": None, "context_request": {"task_id": prepared.task_id,
                "missing_needs": [{"need_id": "contract", "description": "사내 API 계약의 지정 버전 필요",
                                   "path_hints": [], "symbol_hints": [], "tag_hints": [], "required": True}],
                "reason": "등록되지 않은 계약을 추측할 수 없습니다."},
        }]})
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)
        outcome = EngineDispatcher(prepared.service, runtime, proposal_provider=provider).run_once(prepared.project_id)
        self.assertEqual("CONTEXT_REQUIRED", outcome.blocker_code)
        self.assertEqual("contract", json.loads(outcome.detail)["missing_needs"][0]["need_id"])
        self.assertEqual(0, runtime.create_calls)

    def test_unknown_preparation_does_not_call_role_again(self):
        prepared, runtime = self.prepared()
        runner = ScriptedStructuredRoleRunner({})

        def fault(point):
            if point == "after_execution_preparation_intent":
                raise RuntimeError(point)

        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles, fault)
        with self.assertRaisesRegex(RuntimeError, "after_execution_preparation_intent"):
            EngineDispatcher(prepared.service, runtime, proposal_provider=provider).run_once(prepared.project_id)
        resumed = ExecutionProposalAdapter(prepared.service, runner, self.roles)
        outcome = EngineDispatcher(prepared.service, runtime, proposal_provider=resumed).run_once(prepared.project_id)
        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", outcome.blocker_code)
        self.assertEqual([], runner.calls)

    def test_command_fault_boundaries_never_duplicate_external_effect(self):
        for point, expected_calls, expected_action in (
            ("before_validation_command_intent", 1, RunOnceAction.VALIDATED),
            ("after_validation_command_intent", 0, RunOnceAction.BLOCKED),
            ("before_validation_command_receipt", 1, RunOnceAction.BLOCKED),
            ("after_validation_command_receipt", 1, RunOnceAction.VALIDATED),
        ):
            with self.subTest(point=point):
                prepared, runtime = self.prepared(point)
                dispatcher = self.validating(prepared, runtime)

                def fault(actual):
                    if actual == point:
                        raise RuntimeError(actual)

                with patch("flowmarshal.engine.validation_execution.subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0, b"direct test", b"")) as command:
                    with self.assertRaisesRegex(RuntimeError, point):
                        EngineDispatcher(prepared.service, runtime, fault_hook=fault).run_once(prepared.project_id)
                    outcome = dispatcher.run_once(prepared.project_id)
                    self.assertEqual(expected_action, outcome.action)
                    self.assertEqual(expected_calls, command.call_count)
                self.assertTrue(prepared.service.ledger.verify_history(prepared.project_id))

    def test_task_success_cannot_substitute_for_failing_independent_goal_test(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        self.assertEqual("GOAL_VALIDATION_SPEC_REQUIRED", dispatcher.run_once(prepared.project_id).blocker_code)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        self.assertEqual(RunOnceAction.MATERIALIZED,
                         dispatcher.run_once(prepared.project_id, goal_validation_step=step).action)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, b"independent integration failed", b"")) as command:
            observed = dispatcher.run_once(prepared.project_id)
            self.assertEqual(RunOnceAction.VALIDATED, observed.action)
            self.assertEqual(1, command.call_count)
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT status, task_id FROM validation_results WHERE id = ?",
                                     (observed.validation_result_id,)).fetchone()
        self.assertEqual("fail", row["status"])
        self.assertIsNone(row["task_id"])
        dispatcher.run_once(prepared.project_id)
        self.assertNotEqual("completed", prepared.service.status(prepared.project_id)["project"]["run_state"])

    def test_goal_binding_rejects_file_change_before_execution(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        (prepared.workspace / "new_input.txt").write_text("새 관찰", encoding="utf-8")
        with patch("flowmarshal.engine.validation_execution.subprocess.run") as command:
            outcome = dispatcher.run_once(prepared.project_id)
            self.assertEqual("STALE_EXECUTION_INPUT", outcome.blocker_code)
            command.assert_not_called()

    def test_independent_semantic_goal_uses_validator_and_direct_evidence(self):
        contract = IntegrationValidationContract(
            validation_id="validation_goal", statement="독립적으로 공개 계약 유지 여부를 검토한다.",
            criterion_refs=("ac_fix",), method="semantic", required_evidence_kinds=("model_review",),
        )
        prepared, runtime = self.prepared(goal_validation=contract)
        self.task_completed(prepared, runtime)
        with prepared.service.ledger.read() as connection:
            evidence_id = connection.execute("SELECT id FROM evidence_records WHERE task_id = ? AND kind = 'test'",
                                             (prepared.task_id,)).fetchone()[0]
        runner = ScriptedStructuredRoleRunner({
            "goal_test_preparation": [{"step": {"validation_id": "validation_goal", "method": "semantic",
                "semantic_instruction": "직접 파일·테스트 관측으로 공개 계약 유지 여부를 검토한다.",
                "required_evidence_kinds": ["model_review"]}}],
            "goal_validator": [{"passed": True, "rationale": "직접 테스트 관측과 파일 비교를 확인했다.",
                                 "evidence_refs": [evidence_id]}],
        })
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)
        dispatcher = EngineDispatcher(prepared.service, runtime, proposal_provider=provider)
        self.assertEqual(RunOnceAction.MATERIALIZED, dispatcher.run_once(prepared.project_id).action)
        observed = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.VALIDATED, observed.action)
        self.assertIsNone(observed.task_id)
        self.assertEqual(self.roles.validator.model, runner.calls[-1].model)
        self.assertEqual("goal_validator", runner.calls[-1].role)
        goal_request = runner.calls[0]
        self.assertNotIn("plan", goal_request.payload)
        self.assertNotIn("ready_task", goal_request.payload)
        self.assertIn("goal_test", goal_request.payload)
        self.assertIn("독립 통합 검사", goal_request.instructions)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)

    def test_goal_mixed_command_file_evidence_is_collected_directly(self):
        contract = IntegrationValidationContract(
            validation_id="validation_goal", statement="통합 테스트와 파일 실재를 확인한다.",
            criterion_refs=("ac_fix",), method="deterministic", required_evidence_kinds=("test", "file"),
        )
        prepared, runtime = self.prepared(goal_validation=contract)
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={
            "validation_id": "validation_goal", "required_evidence_kinds": ("test", "file"),
            "artifact_paths": ("app.py",),
        })
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        result = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.VALIDATED, result.action)
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT observation FROM evidence_records WHERE task_id IS NULL AND kind = 'file'").fetchone()
        observation = json.loads(row["observation"])
        self.assertEqual(sha256_bytes((prepared.workspace / "app.py").read_bytes()), observation["after_digest"])
        self.assertNotIn("stdout", observation)

    def test_goal_artifact_escape_is_rejected(self):
        contract = IntegrationValidationContract(
            validation_id="validation_goal", statement="파일 실재 검사", criterion_refs=("ac_fix",),
            method="deterministic", required_evidence_kinds=("file",),
        )
        prepared, runtime = self.prepared(goal_validation=contract)
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={
            "validation_id": "validation_goal", "required_evidence_kinds": ("file",),
            "artifact_paths": ("../outside",),
        })
        with self.assertRaisesRegex(EngineServiceError, "프로젝트 내부"):
            dispatcher.run_once(prepared.project_id, goal_validation_step=step)

    def test_manual_goal_requires_binding_and_fresh_observation(self):
        contract = IntegrationValidationContract(
            validation_id="validation_goal", statement="사용자가 결과를 확인한다.", criterion_refs=("ac_fix",),
            method="manual", required_evidence_kinds=("user_decision",),
        )
        prepared, runtime = self.prepared(goal_validation=contract)
        dispatcher = self.task_completed(prepared, runtime)
        def submit():
            return prepared.service.record_typed_validation_observation(
                project_id=prepared.project_id, plan_revision_id=prepared.plan_revision_id,
                observation=ManualValidationObservation(
                    validation_id="validation_goal", observer="fixture 사용자", passed=True,
                    observation="출력을 직접 확인했다.", source_ref="manual:fixture", observed_at=utc_now(),
                ),
            )
        with self.assertRaisesRegex(EngineServiceError, "binding이 없습니다"):
            submit()
        dispatcher.run_once(prepared.project_id, goal_validation_step=ValidationExecutionStep(
            validation_id="validation_goal", method="manual", manual_instruction="출력 직접 확인",
            required_evidence_kinds=("user_decision",),
        ))
        (prepared.workspace / "new-input.txt").write_text("변경", encoding="utf-8")
        with self.assertRaisesRegex(EngineServiceError, "STALE_EXECUTION_INPUT"):
            submit()

    def test_external_goal_selector_must_match_binding(self):
        contract = IntegrationValidationContract(
            validation_id="validation_goal", statement="외부 관측 확인", criterion_refs=("ac_fix",),
            method="external_observation", required_evidence_kinds=("external_observation",),
        )
        prepared, runtime = self.prepared(goal_validation=contract)
        dispatcher = self.task_completed(prepared, runtime)
        dispatcher.run_once(prepared.project_id, goal_validation_step=ValidationExecutionStep(
            validation_id="validation_goal", method="external_observation", external_selector="fixture:expected",
            required_evidence_kinds=("external_observation",),
        ))
        with self.assertRaisesRegex(EngineServiceError, "selector"):
            prepared.service.record_typed_validation_observation(
                project_id=prepared.project_id, plan_revision_id=prepared.plan_revision_id,
                observation=ExternalValidationObservation(
                    validation_id="validation_goal", provider="fixture", selector="fixture:wrong", passed=True,
                    observation="잘못된 selector", receipt_digest="sha256:" + "1" * 64, observed_at=utc_now(),
                ),
            )


    def test_task_semantic_validation_uses_bound_direct_catalog(self):
        prepared, runtime = self.prepared(semantic_task_validation=True)
        dispatcher = self.validating(prepared, runtime)
        dispatcher.run_once(prepared.project_id)
        dispatched = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)).fetchone()
            intent = connection.execute("SELECT request_json FROM runtime_intents WHERE attempt_id = ? AND kind = 'create_thread'",
                                        (dispatched.attempt_id,)).fetchone()
        provided = json.loads(intent["request_json"])["semantic_evidence_ids"]
        self.assertGreaterEqual(len(provided), 2)
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        runtime.complete(binding.thread_id, response=json.dumps({"passed": True, "rationale": "직접 evidence 확인",
                                                                "evidence_refs": provided}))
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)

    def test_task_semantic_validation_rejects_invented_evidence(self):
        prepared, runtime = self.prepared(semantic_task_validation=True)
        dispatcher = self.validating(prepared, runtime)
        dispatcher.run_once(prepared.project_id)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)).fetchone()
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        runtime.complete(binding.thread_id, response=json.dumps({"passed": True, "rationale": "발명한 참조",
                                                                "evidence_refs": ["evidence_" + "0" * 32]}))
        with self.assertRaisesRegex(EngineServiceError, "제공되지 않은"):
            dispatcher.run_once(prepared.project_id)


if __name__ == "__main__":
    unittest.main()
