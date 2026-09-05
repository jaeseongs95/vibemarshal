from __future__ import annotations

import json
import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine.domain import (
    DeterministicValidationObservation, EvidenceKind, EvidenceRecord, ExecutionContextNeed,
    ExternalValidationObservation, FailureClass, IntegrationValidationContract, ManualValidationObservation,
    RunOnceAction, ThreadBinding, ValidationExecutionStep, ValidationResult, ValidationStatus, new_id, utc_now,
)
from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.context import AdditionalContextRequest, ContextSelector, PromptAssembler, read_context_fragment
from flowmarshal.engine.service import EngineServiceError
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.execution import (
    ExecutionProposalAdapter, ProviderExecutionPreparation, compile_task_preparation,
    execution_context, task_preparation_payload,
)
from pydantic import ValidationError
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.roles import CodexStructuredRoleRunner, RoleCallReceipt, StructuredRoleError
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.validation_execution import GoalValidationBinding, GoalValidationRetryRequest
from tests.test_engine_qualification import qualification_inventory
from tests.test_engine_roles import ImmediateRoleRuntime


ROOT = Path(__file__).resolve().parents[1]


class ExecutionAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.inventory = qualification_inventory()
        self.roles = default_role_configuration(ROOT)

    def prepared(self, name="work", *, goal_validation=None, semantic_task_validation=False,
                 source_text=None, state_inside_project=False):
        base = self.base / name
        base.mkdir()
        workspace, _ = _copy_fixture(ROOT, base)
        if source_text is not None:
            (workspace / "app.py").write_bytes(source_text.encode("utf-8"))
        state_root = workspace / "custom-state" if state_inside_project else base / "state"
        prepared = _prepare(workspace=workspace, state_root=state_root, inventory=self.inventory,
                            roles=self.roles, goal_validation=goal_validation, semantic_task_validation=semantic_task_validation)
        return prepared, FakeCodexRuntime(self.inventory)

    def test_context_budget_failure_returns_a_request_before_spec_or_worker_creation(self):
        prepared, runtime = self.prepared()
        proposal = prepared.proposal.model_copy(update={"context_token_budget": 100})
        outcome = EngineDispatcher(prepared.service, runtime).run_once(prepared.project_id, proposal=proposal)
        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("CONTEXT_REQUIRED", outcome.blocker_code)
        request = AdditionalContextRequest.model_validate_json(outcome.detail)
        self.assertEqual(prepared.task_id, request.task_id)
        self.assertTrue(request.missing_needs)
        self.assertIn("예산", request.reason)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
            self.assertEqual("ready", connection.execute("SELECT status FROM task_contracts WHERE id = ?",
                                                        (prepared.task_id,)).fetchone()[0])
        self.assertEqual(0, runtime.create_calls)

    def test_compiler_binds_selected_body_and_range_to_prompt_but_keeps_full_file_freshness(self):
        source = "def add(left, right):\n    return left - right\n\ndef unrelated():\n    return 999\n"
        prepared, _ = self.prepared(source_text=source)
        proposal = prepared.proposal.model_copy(update={"context_needs": (
            ExecutionContextNeed(need_id="add", description="수정 함수", symbol_hints=("add",)),
        )})
        bundles = []
        assemble = PromptAssembler.assemble

        def capture(assembler, **kwargs):
            bundle = assemble(assembler, **kwargs)
            bundles.append(bundle)
            return bundle

        with patch.object(PromptAssembler, "assemble", capture):
            spec = prepared.service.compile_execution_spec(proposal, inventory=self.inventory)
        manifest = spec.definition.context_manifest
        fragment = next(item for item in manifest.fragments if item.source_ref == "app.py")
        self.assertEqual("python-lines:1-2", fragment.selector)
        self.assertEqual(sha256_bytes(source.encode("utf-8")), fragment.content_digest)
        self.assertIn('source="app.py#python-lines:1-2"', bundles[-1].dynamic_suffix)
        self.assertIn(read_context_fragment(prepared.workspace, fragment), bundles[-1].dynamic_suffix)
        self.assertNotIn("unrelated", bundles[-1].dynamic_suffix)
        self.assertEqual(bundles[-1].binding, manifest.prompt_binding)
        (prepared.workspace / "app.py").write_text(source.replace("999", "1000"), encoding="utf-8")
        with self.assertRaisesRegex(EngineServiceError, "STALE_EXECUTION_INPUT"):
            prepared.service.reserve_attempt(task_id=prepared.task_id)

    def test_context_changed_between_selection_and_prompt_is_rejected(self):
        prepared, _ = self.prepared()
        select = ContextSelector.select

        def change_after_select(selector, **kwargs):
            selection = select(selector, **kwargs)
            (prepared.workspace / "AGENTS.md").write_text("선택 후 변경한 지침", encoding="utf-8")
            return selection

        with patch.object(ContextSelector, "select", change_after_select):
            with self.assertRaisesRegex(EngineServiceError, "STALE_EXECUTION_INPUT"):
                prepared.service.compile_execution_spec(prepared.proposal, inventory=self.inventory)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0])

    def test_custom_artifact_updates_do_not_invalidate_goal_or_execution_observations(self):
        prepared, _ = self.prepared(state_inside_project=True)
        service = prepared.service
        before_map, before_state = service.reobserve_project(prepared.project_id)
        before_context = execution_context(service, prepared.project_id)
        artifact = service.ledger.artifact_root / "runtime-receipt.json"
        artifact.write_text('{"status": "running"}', encoding="utf-8")
        after_map, after_state = service.reobserve_project(prepared.project_id)
        after_context = execution_context(service, prepared.project_id)
        self.assertEqual(before_map.revision_digest, after_map.revision_digest)
        self.assertEqual(before_state.snapshot_digest, after_state.snapshot_digest)
        self.assertEqual(before_context, after_context)
        facts = service.observe_goal_inputs(prepared.project_id, "runtime-receipt.json")
        self.assertFalse(any("runtime-receipt" in fact.get("path", "") for fact in facts))
        (prepared.workspace / "app.py").write_text("def add(left, right): return left + right", encoding="utf-8")
        with self.assertRaisesRegex(EngineServiceError, "STALE_EXECUTION_INPUT"):
            execution_context(service, prepared.project_id)

    def validating(self, prepared, runtime, *, worker_response=None):
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)).fetchone()
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        (prepared.workspace / "app.py").write_text("def add(left: int, right: int) -> int:\n    return left + right\n", encoding="utf-8")
        runtime.complete(binding.thread_id, response=worker_response)
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

    def test_environment_goal_retry_rebinds_and_preserves_failed_result(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        self.assertEqual(RunOnceAction.MATERIALIZED,
                         dispatcher.run_once(prepared.project_id, goal_validation_step=step).action)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 9009, b"", b"Python")):
            failed = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.VALIDATED, failed.action)
        retry = GoalValidationRetryRequest(
            step=step.model_copy(update={"argv": ("repaired-python", "-m", "unittest")}),
            failed_validation_result_id=failed.validation_result_id,
            failure_class=FailureClass.ENVIRONMENT,
            failure_evidence_id=failed.evidence_ids[0],
            rationale="직접 command evidence가 Windows Python alias 환경 실패를 보인다.",
        )
        rebound = dispatcher.run_once(prepared.project_id, goal_validation_retry=retry)
        self.assertEqual(RunOnceAction.MATERIALIZED, rebound.action)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"direct test", b"")) as command:
            observed = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.VALIDATED, observed.action)
        self.assertEqual(1, command.call_count)
        with prepared.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM validation_results WHERE validation_id = 'validation_goal' ORDER BY evaluated_at, rowid"
            ).fetchall()
            history = connection.execute(
                "SELECT event_type, payload_json FROM history_events WHERE project_id = ? "
                "AND event_type = 'goal_test.retry_bound'",
                (prepared.project_id,),
            ).fetchone()
        self.assertEqual(["fail", "pass"], [json.loads(row["payload_json"])["status"] for row in rows])
        retry_binding = GoalValidationBinding.model_validate_json(history["payload_json"])
        self.assertEqual(failed.validation_result_id, retry_binding.retry.failed_validation_result_id)
        self.assertEqual(FailureClass.ENVIRONMENT, retry_binding.retry.failure_class)
        passed = ValidationResult.model_validate_json(rows[-1]["payload_json"])
        self.assertEqual(retry_binding.binding_digest, passed.goal_validation_binding_digest)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)
        self.assertTrue(prepared.service.ledger.verify_history(prepared.project_id))

    def test_environment_goal_retry_accepts_legacy_unbound_failure(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        observation = DeterministicValidationObservation(
            validation_id="validation_goal", task_id=None, argv=step.argv,
            working_directory=str(prepared.workspace), timeout_seconds=step.timeout_seconds,
            expected_exit_codes=step.expected_exit_codes, actual_exit_code=9009, timed_out=False,
            stdout="", stderr="Python", observed_at=utc_now(),
        )
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=prepared.project_id, task_id=None,
            kind=EvidenceKind.TEST, source_ref="legacy:goal-test",
            observation=observation.model_dump_json(),
            content_digest=sha256_digest(observation), observed_at=observation.observed_at,
        )
        prepared.service.record_evidence(evidence)
        legacy = ValidationResult(
            validation_result_id=new_id("validation_result"), validation_id="validation_goal",
            task_id=None, status=ValidationStatus.FAIL, evidence_ids=(evidence.evidence_id,),
            rationale="legacy 환경 실패", evaluated_at=observation.observed_at,
        )
        prepared.service.record_validation(
            project_id=prepared.project_id, plan_revision_id=prepared.plan_revision_id, result=legacy,
        )
        retry = GoalValidationRetryRequest(
            step=step.model_copy(update={"argv": ("repaired-python", "-m", "unittest")}),
            failed_validation_result_id=legacy.validation_result_id,
            failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=evidence.evidence_id,
            rationale="legacy direct test evidence에 근거한 환경 복구다.",
        )
        self.assertEqual(RunOnceAction.MATERIALIZED,
                         dispatcher.run_once(prepared.project_id, goal_validation_retry=retry).action)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"direct test", b"")):
            self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(prepared.project_id).action)

    def test_environment_goal_retry_requires_changed_step_and_has_no_side_effect(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 9009, b"", b"Python")):
            failed = dispatcher.run_once(prepared.project_id)
        retry = GoalValidationRetryRequest(
            step=step, failed_validation_result_id=failed.validation_result_id,
            failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=failed.evidence_ids[0],
            rationale="변경되지 않은 step은 거부돼야 한다.",
        )
        before = prepared.service.status(prepared.project_id)["history_count"]
        outcome = dispatcher.run_once(prepared.project_id, goal_validation_retry=retry)
        self.assertEqual("GOAL_VALIDATION_RETRY_INVALID", outcome.blocker_code)
        self.assertEqual(before, prepared.service.status(prepared.project_id)["history_count"])

    def test_environment_goal_retry_preserves_exit_and_artifact_contract(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 9009, b"", b"Python")):
            failed = dispatcher.run_once(prepared.project_id)
        for changed in (
            step.model_copy(update={"argv": ("repaired-python",), "expected_exit_codes": (0, 9009)}),
            step.model_copy(update={"argv": ("repaired-python",), "artifact_paths": ("app.py",)}),
        ):
            with self.subTest(changed=changed):
                retry = GoalValidationRetryRequest(
                    step=changed, failed_validation_result_id=failed.validation_result_id,
                    failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=failed.evidence_ids[0],
                    rationale="실패 evidence를 보존한 환경 복구 요청이다.",
                )
                outcome = dispatcher.run_once(prepared.project_id, goal_validation_retry=retry)
                self.assertEqual("GOAL_VALIDATION_RETRY_INVALID", outcome.blocker_code)

    def test_environment_goal_retry_rejects_unowned_evidence_and_stale_inputs(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 9009, b"", b"Python")):
            failed = dispatcher.run_once(prepared.project_id)
        retry_step = step.model_copy(update={"argv": ("repaired-python",)})
        with prepared.service.ledger.read() as connection:
            other_evidence_id = connection.execute(
                "SELECT id FROM evidence_records WHERE task_id IS NOT NULL ORDER BY observed_at LIMIT 1"
            ).fetchone()[0]
        before = prepared.service.status(prepared.project_id)["history_count"]
        unowned = GoalValidationRetryRequest(
            step=retry_step, failed_validation_result_id=failed.validation_result_id,
            failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=other_evidence_id,
            rationale="다른 Task 증거는 Goal Test 재시도 근거가 될 수 없다.",
        )
        self.assertEqual("GOAL_VALIDATION_RETRY_INVALID",
                         dispatcher.run_once(prepared.project_id, goal_validation_retry=unowned).blocker_code)
        self.assertEqual(before, prepared.service.status(prepared.project_id)["history_count"])
        (prepared.workspace / "new-input.txt").write_text("stale", encoding="utf-8")
        stale = GoalValidationRetryRequest(
            step=retry_step, failed_validation_result_id=failed.validation_result_id,
            failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=failed.evidence_ids[0],
            rationale="입력 변경 뒤에는 기존 실패 evidence를 재결속하지 않는다.",
        )
        outcome = dispatcher.run_once(prepared.project_id, goal_validation_retry=stale)
        self.assertEqual("STALE_EXECUTION_INPUT", outcome.blocker_code)
        self.assertEqual(before, prepared.service.status(prepared.project_id)["history_count"])

    def test_environment_goal_retry_is_limited_to_two_rebindings(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 9009, b"", b"Python")):
            failed = dispatcher.run_once(prepared.project_id)
        for retry_no in (1, 2):
            retry = GoalValidationRetryRequest(
                step=step.model_copy(update={"argv": (f"repaired-python-{retry_no}",)}),
                failed_validation_result_id=failed.validation_result_id,
                failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=failed.evidence_ids[0],
                rationale="환경 복구 명령을 변경한다.",
            )
            self.assertEqual(RunOnceAction.MATERIALIZED,
                             dispatcher.run_once(prepared.project_id, goal_validation_retry=retry).action)
            with patch("flowmarshal.engine.validation_execution.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 9009, b"", b"Python")):
                failed = dispatcher.run_once(prepared.project_id)
        retry = GoalValidationRetryRequest(
            step=step.model_copy(update={"argv": ("repaired-python-3",)}),
            failed_validation_result_id=failed.validation_result_id,
            failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=failed.evidence_ids[0],
            rationale="세 번째 재결속은 한도를 넘어야 한다.",
        )
        outcome = dispatcher.run_once(prepared.project_id, goal_validation_retry=retry)
        self.assertEqual("GOAL_VALIDATION_RETRY_INVALID", outcome.blocker_code)

    def test_terminal_goal_verdict_rejects_goal_retry_without_new_binding(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        with patch("flowmarshal.engine.validation_execution.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 9009, b"", b"Python")):
            failed = dispatcher.run_once(prepared.project_id)
        terminal = dispatcher.run_once(prepared.project_id)
        self.assertEqual("GOAL_NOT_SATISFIED", terminal.blocker_code)
        retry = GoalValidationRetryRequest(
            step=step.model_copy(update={"argv": ("repaired-python",)}),
            failed_validation_result_id=failed.validation_result_id,
            failure_class=FailureClass.ENVIRONMENT, failure_evidence_id=failed.evidence_ids[0],
            rationale="이미 terminal verdict가 있으면 재결속하지 않는다.",
        )
        before = prepared.service.status(prepared.project_id)["history_count"]
        outcome = dispatcher.run_once(prepared.project_id, goal_validation_retry=retry)
        self.assertEqual("GOAL_NOT_SATISFIED", outcome.blocker_code)
        self.assertEqual(before, prepared.service.status(prepared.project_id)["history_count"])

    def test_legacy_goal_binding_payload_and_digest_are_preserved(self):
        prepared, runtime = self.prepared()
        dispatcher = self.task_completed(prepared, runtime)
        step = prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        dispatcher.run_once(prepared.project_id, goal_validation_step=step)
        with prepared.service.ledger.read() as connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id = ? AND event_type = 'goal_test.bound'",
                (prepared.project_id,),
            ).fetchone()[0])
        payload.pop("goal_validation_binding_id")
        payload.pop("retry")
        legacy = GoalValidationBinding.model_validate(payload)
        self.assertEqual(payload, legacy.payload())
        self.assertEqual(sha256_digest(payload), legacy.binding_digest)

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

    def test_semantic_goal_report_requires_response_and_source_evidence(self):
        required = ("model_review", "external_observation", "file")
        for include_source, truncated in ((True, False), (False, False), (True, True)):
            with self.subTest(include_source=include_source, truncated=truncated):
                contract = IntegrationValidationContract(
                    validation_id="validation_goal", statement="Worker 응답 보고를 원본 파일 근거와 대조한다.",
                    criterion_refs=("ac_fix",), method="semantic", required_evidence_kinds=required,
                )
                prepared, runtime = self.prepared(name=f"report-{include_source}-{truncated}", goal_validation=contract)
                completed = self.validating(prepared, runtime, worker_response="분석" * 6000 if truncated else "분석 보고")
                self.assertEqual(RunOnceAction.VALIDATED, completed.run_once(prepared.project_id).action)
                self.assertEqual(RunOnceAction.COMPLETED, completed.run_once(prepared.project_id).action)
                with prepared.service.ledger.read() as connection:
                    rows = connection.execute(
                        "SELECT id, kind FROM evidence_records WHERE task_id = ? AND kind IN ('external_observation','file')",
                        (prepared.task_id,),
                    ).fetchall()
                refs = [row["id"] for row in rows if (include_source or row["kind"] != "file")
                        and (not truncated or row["kind"] != "external_observation")]
                self.assertTrue(any(row["kind"] == "external_observation" for row in rows))
                runner = ScriptedStructuredRoleRunner({
                    "goal_test_preparation": [{"step": {"validation_id": "validation_goal", "method": "semantic",
                        "semantic_instruction": "응답 보고의 주장을 파일 근거와 대조한다.",
                        "required_evidence_kinds": list(required)}}],
                    "goal_validator": [{"passed": True, "rationale": "보고 내용과 원본을 대조했다.", "evidence_refs": refs}],
                })
                dispatcher = EngineDispatcher(prepared.service, runtime,
                    proposal_provider=ExecutionProposalAdapter(prepared.service, runner, self.roles))
                self.assertEqual(RunOnceAction.MATERIALIZED, dispatcher.run_once(prepared.project_id).action)
                if include_source and not truncated:
                    self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(prepared.project_id).action)
                    self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)
                else:
                    with self.assertRaises(EngineServiceError):
                        dispatcher.run_once(prepared.project_id)
                    with prepared.service.ledger.read() as connection:
                        self.assertEqual(0, connection.execute(
                            "SELECT COUNT(*) FROM validation_results WHERE task_id IS NULL AND status = 'pass'",
                        ).fetchone()[0])
                self.assertTrue(set(refs).issubset(runner.calls[-1].payload["evidence_catalog"]))
                if truncated:
                    self.assertFalse(any(evidence["kind"] == "external_observation"
                                         for evidence in runner.calls[-1].payload["evidence_catalog"].values()))
                self.assertIn("완료 주장은 검증 근거가 아니다", runner.calls[-1].instructions)

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
        _, task = self.task_contract(prepared)
        semantic_contract = next(item for item in task.validations if item.method == "semantic")
        self.assertEqual(("model_review", "file", "test"), semantic_contract.required_evidence_kinds)
        dispatcher = self.validating(prepared, runtime)
        dispatcher.run_once(prepared.project_id)
        dispatched = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT binding_json, execution_spec_digest FROM attempts WHERE id = ?",
                                     (dispatched.attempt_id,)).fetchone()
            intent = connection.execute("SELECT request_json FROM runtime_intents WHERE attempt_id = ? AND kind = 'create_thread'",
                                        (dispatched.attempt_id,)).fetchone()
            evidence_rows = connection.execute(
                "SELECT id, kind FROM evidence_records WHERE task_id = ?", (prepared.task_id,)
            ).fetchall()
        provided = json.loads(intent["request_json"])["semantic_evidence_ids"]
        self.assertGreaterEqual(len(provided), 2)
        provided_kinds = {item["kind"] for item in evidence_rows if item["id"] in provided}
        self.assertIn("file", provided_kinds)
        self.assertIn("test", provided_kinds)
        # 필수 종류를 model_review로 한정해도 앞선 file/test는 직접 catalog에 남는다.
        minimal_catalog = dispatcher._task_evidence_catalog(
            prepared.task_id, row["execution_spec_digest"], required_evidence_kinds=("model_review",)
        )
        self.assertEqual(set(provided), set(minimal_catalog))
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        runtime.complete(binding.thread_id, response=json.dumps({"passed": True, "rationale": "직접 evidence 확인",
                                                                "evidence_refs": provided}))
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)

    def test_task_semantic_catalog_includes_only_current_core_worker_report_when_required(self):
        prepared, runtime = self.prepared(semantic_task_validation=True)
        dispatcher = self.validating(prepared, runtime, worker_response="근거를 대조한 읽기 전용 분석 보고")
        with prepared.service.ledger.read() as connection:
            execution = connection.execute(
                "SELECT id, execution_spec_digest FROM attempts WHERE task_id = ? "
                "AND kind = 'execution' AND status = 'succeeded'",
                (prepared.task_id,),
            ).fetchone()
            report = connection.execute(
                "SELECT id, source_ref FROM evidence_records WHERE attempt_id = ? "
                "AND kind = 'external_observation'",
                (execution["id"],),
            ).fetchone()
        self.assertEqual("codex-thread:", report["source_ref"][:13])
        unrelated = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=prepared.project_id,
            task_id=prepared.task_id, attempt_id=execution["id"],
            kind=EvidenceKind.EXTERNAL_OBSERVATION, source_ref="fixture:external-report",
            observation="임의 외부 제출", content_digest=sha256_digest({"fixture": "external-report"}),
            observed_at=utc_now(),
        )
        prepared.service.record_evidence(unrelated)
        without_report = dispatcher._task_evidence_catalog(
            prepared.task_id, execution["execution_spec_digest"],
        )
        with_report = dispatcher._task_evidence_catalog(
            prepared.task_id,
            execution["execution_spec_digest"],
            required_evidence_kinds=("external_observation",),
        )
        self.assertNotIn(report["id"], without_report)
        self.assertIn(report["id"], with_report)
        self.assertNotIn(unrelated.evidence_id, with_report)
        self.assertEqual("근거를 대조한 읽기 전용 분석 보고", with_report[report["id"]]["observation"])
        self.assertTrue(
            {"file", "diff"}.issubset({item["kind"] for item in with_report.values()})
        )

    def test_task_semantic_catalog_excludes_truncated_worker_report(self):
        prepared, runtime = self.prepared(name="truncated-report", semantic_task_validation=True)
        dispatcher = self.validating(prepared, runtime, worker_response="x" * 10_001)
        with prepared.service.ledger.read() as connection:
            execution = connection.execute(
                "SELECT id, execution_spec_digest FROM attempts WHERE task_id = ? "
                "AND kind = 'execution' AND status = 'succeeded'",
                (prepared.task_id,),
            ).fetchone()
            report = connection.execute(
                "SELECT id, source_ref FROM evidence_records WHERE attempt_id = ? "
                "AND kind = 'external_observation'",
                (execution["id"],),
            ).fetchone()
        catalog = dispatcher._task_evidence_catalog(
            prepared.task_id,
            execution["execution_spec_digest"],
            required_evidence_kinds=("external_observation",),
        )
        self.assertTrue(report["source_ref"].endswith(":truncated"))
        self.assertNotIn(report["id"], catalog)

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

    def test_auto_preparation_recovers_contract_invalid_semantic_artifacts_once(self):
        prepared, runtime = self.prepared(semantic_task_validation=True)
        bad = self.provider_response(prepared)
        next(item for item in bad["proposal"]["validation_steps"]
             if item["validation_id"] == "validation_public_contract")["artifact_paths"] = ["app.py", "test_app.py"]
        good = self.provider_response(prepared)
        role_runtime = ImmediateRoleRuntime([json.dumps(bad), json.dumps(good)])
        role_runtime.inventory = self.inventory
        runner = CodexStructuredRoleRunner(role_runtime, poll_interval_seconds=0)
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)

        outcome = EngineDispatcher(prepared.service, runtime, proposal_provider=provider).run_once(prepared.project_id)

        self.assertEqual(RunOnceAction.MATERIALIZED, outcome.action)
        self.assertEqual(1, runner.receipts[-1].schema_recovery_attempts)
        self.assertEqual(2, len(runner.receipts[-1].turn_ids))
        with prepared.service.ledger.read() as connection:
            spec = json.loads(connection.execute(
                "SELECT payload_json FROM execution_spec_revisions WHERE task_id = ?", (prepared.task_id,)
            ).fetchone()[0])
            completed = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE event_type = 'operation.completed'"
            ).fetchone()[0]
        semantic = next(item for item in spec["definition"]["validation_steps"]
                        if item["validation_id"] == "validation_public_contract")
        self.assertEqual([], semantic["artifact_paths"])
        self.assertEqual(1, completed)

    def test_auto_preparation_fails_after_second_contract_invalid_output(self):
        prepared, runtime = self.prepared(semantic_task_validation=True)
        bad = self.provider_response(prepared)
        next(item for item in bad["proposal"]["validation_steps"]
             if item["validation_id"] == "validation_public_contract")["artifact_paths"] = ["app.py"]
        role_runtime = ImmediateRoleRuntime([json.dumps(bad), json.dumps(bad)])
        role_runtime.inventory = self.inventory
        runner = CodexStructuredRoleRunner(role_runtime, poll_interval_seconds=0)
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)

        with self.assertRaises(StructuredRoleError) as raised:
            EngineDispatcher(prepared.service, runtime, proposal_provider=provider).run_once(prepared.project_id)

        receipt = raised.exception.receipt
        self.assertIsNotNone(receipt)
        self.assertEqual("schema_failed", receipt.status)
        self.assertEqual(1, receipt.schema_recovery_attempts)
        self.assertEqual(2, len(receipt.turn_ids))
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def test_run_revalidates_replayed_execution_preparation_without_runtime_or_spec_and_deduplicates_usage(self):
        prepared, _ = self.prepared(semantic_task_validation=True)
        context, task = self.task_contract(prepared)
        context["predecessor_outputs"] = []
        payload = task_preparation_payload(context, task)
        bad = self.provider_response(prepared)
        next(item for item in bad["proposal"]["validation_steps"]
             if item["validation_id"] == "validation_public_contract")["artifact_paths"] = ["app.py"]
        runner = ScriptedStructuredRoleRunner({"execution_preparation": []})
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)
        receipt = RoleCallReceipt(
            call_id="model_call_replayed_invalid_payload",
            role="execution_preparation",
            status="succeeded",
            model=self.roles.plan_expander.model,
            effort=self.roles.plan_expander.effort,
            inventory_digest=self.inventory.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            input_digest="sha256:" + "1" * 64,
            output_digest=sha256_digest(bad),
            output_schema_digest="sha256:" + "2" * 64,
            input_tokens=101,
            cached_input_tokens=50,
            output_tokens=23,
            reasoning_tokens=11,
            usage_available=True,
            latency_ms=7,
            recorded_at=utc_now(),
        )

        def validate(value):
            raw = ProviderExecutionPreparation.model_validate(value)
            compile_task_preparation(raw, task)
            return raw

        with prepared.service.ledger.read() as connection:
            before_usage = connection.execute("SELECT COUNT(*) FROM budget_usage").fetchone()[0]
        def replay(**kwargs):
            from flowmarshal.engine.roles import RoleCallRequest, strict_json_output_schema
            request = RoleCallRequest.model_validate(kwargs["request"]["role_request"])
            bound = receipt.model_copy(update={
                "input_digest": request.request_digest,
                "output_schema_digest": sha256_digest(strict_json_output_schema(request.output_schema)),
                "observed_binding": request.operational_binding,
            })
            return {"payload": bad, "receipt": bound.model_dump(mode="json")}

        for _ in range(2):
            with patch.object(provider.operations, "invoke", side_effect=replay):
                with self.assertRaises(ValidationError):
                    provider._run(
                        prepared.project_id, self.inventory, context, ProviderExecutionPreparation,
                        "execution_preparation", payload=payload, validator=validate,
                    )
        with prepared.service.ledger.read() as connection:
            after_usage = connection.execute("SELECT COUNT(*) FROM budget_usage").fetchone()[0]
            stored_usage = json.loads(connection.execute(
                "SELECT payload_json FROM budget_usage WHERE logical_call_ref = ?", (receipt.call_id,)
            ).fetchone()[0])
        self.assertEqual([], runner.calls)
        self.assertEqual(before_usage + 1, after_usage)
        self.assertEqual((101, 23, 11), (
            stored_usage["input_tokens"], stored_usage["output_tokens"], stored_usage["reasoning_tokens"],
        ))
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def test_goal_preparation_recovers_changed_contract_once(self):
        prepared, _ = self.prepared()
        good_step = prepared.proposal.validation_steps[0].model_dump(mode="json")
        good_step["validation_id"] = "validation_goal"
        bad_step = copy.deepcopy(good_step)
        bad_step["validation_id"] = "validation_wrong"
        role_runtime = ImmediateRoleRuntime([
            json.dumps({"step": bad_step}), json.dumps({"step": good_step}),
        ])
        role_runtime.inventory = self.inventory
        runner = CodexStructuredRoleRunner(role_runtime, poll_interval_seconds=0)
        provider = ExecutionProposalAdapter(prepared.service, runner, self.roles)

        step = provider.prepare_goal(
            project_id=prepared.project_id, validation_id="validation_goal", inventory=self.inventory,
        )

        self.assertEqual("validation_goal", step.validation_id)
        self.assertEqual(1, runner.receipts[-1].schema_recovery_attempts)
        self.assertEqual(2, len(runner.receipts[-1].turn_ids))


if __name__ == "__main__":
    unittest.main()
