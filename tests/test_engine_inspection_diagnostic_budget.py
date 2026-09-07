from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetBlocked, BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.domain import new_id
from flowmarshal.engine.inspection_diagnostic_budget import (
    DiagnosticBudgetRegistry,
    bind_diagnostic_policy_input,
    diagnostic_budget_ledger_observation,
    diagnostic_budget_state_root,
    verify_diagnostic_policy_input,
)
from flowmarshal.engine.role_execution import (
    RoleTimeoutPolicy, use_role_timeout_policy, verify_role_timeout_binding,
)
from flowmarshal.engine.roles import RoleCallReceipt, RoleCallResult, make_role_request, strict_json_output_schema
from flowmarshal.engine.runtime import RuntimeObservation
from tests.engine_helpers import goal


def _s06_module():
    path = Path(__file__).parents[1] / "scripts" / "diagnostics" / "r_s06_10.py"
    spec = importlib.util.spec_from_file_location("r_s06_10_budget_wrapper_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _UnknownUsageRunner:
    max_schema_recovery_attempts = 0
    receipts = ()

    def run(self, request, *, validator=None):
        receipt = RoleCallReceipt(
            call_id="provider-unknown", role=request.role, status="succeeded",
            model=request.model, effort=request.effort, inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access", approval_policy="never",
            thread_id="thread-one", turn_ids=("turn-one",), input_digest=request.request_digest,
            output_digest=sha256_digest({"ok": True}),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            timeout_policy_digest=request.timeout_policy_digest,
            observation_policy_digest=request.observation_policy_digest,
            input_tokens=None, cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
            usage_available=False, latency_ms=1, recorded_at=datetime.now(timezone.utc),
        )
        self.receipts = (receipt,)
        return RoleCallResult(payload={"ok": True}, receipt=receipt)


class _KnownUsageRecordedRunner(_UnknownUsageRunner):
    def __init__(self):
        self.runtime = SimpleNamespace(requires_budget_policy=False)
        self.name = None
        self.last_result = None

    def run(self, request, *, validator=None):
        receipt = RoleCallReceipt(
            call_id="provider-known", role=request.role, status="succeeded",
            model=request.model, effort=request.effort, inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access", approval_policy="never",
            thread_id="thread-one", turn_ids=("turn-one",), input_digest=request.request_digest,
            output_digest=sha256_digest({"ok": True}),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            timeout_policy_digest=request.timeout_policy_digest,
            observation_policy_digest=request.observation_policy_digest,
            input_tokens=3, cached_input_tokens=0, output_tokens=2, reasoning_tokens=0,
            usage_available=True, latency_ms=1, recorded_at=datetime.now(timezone.utc),
        )
        self.receipts = (receipt,)
        self.last_result = RoleCallResult(payload={"ok": True}, receipt=receipt)
        return self.last_result


class InspectionDiagnosticBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.budget = self.root / "budget.json"
        self.timeout = self.root / "timeouts.json"
        self.project = self.root / "project.json"
        self.budget.write_text(json.dumps({"schema_version": "1.0", "total_tokens": 1_000_000,
                                           "call_reservation_tokens": 100_000,
                                           "replan_reserve_percent": 25}), encoding="utf-8")
        self.timeout.write_text(json.dumps({"format": "flowmarshal-role-timeouts-v1",
                                            "default_timeout_seconds": 30}), encoding="utf-8")
        self.project.write_text(json.dumps({"project_id": "project-bound", "expected_root": str(self.workspace),
                                            "expected_name": "자동화테스트"}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def bind(self):
        return bind_diagnostic_policy_input(
            snapshot_root=self.root / "run" / "policy-inputs",
            budget_policy_path=self.budget, role_timeout_policy_path=self.timeout,
            codex_project_binding_path=self.project, require_project_binding=True,
        )

    def test_binding_requires_project_and_rejects_source_tamper(self):
        with self.assertRaisesRegex(ValueError, "CODEX_PROJECT_BINDING_REQUIRED"):
            bind_diagnostic_policy_input(
                snapshot_root=self.root / "missing", budget_policy_path=self.budget,
                role_timeout_policy_path=self.timeout, codex_project_binding_path=None,
                require_project_binding=True,
            )
        binding, _policies = self.bind()
        self.project.write_text(json.dumps({"project_id": "other", "expected_root": str(self.workspace)}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "POLICY_INPUT_CHANGED"):
            verify_diagnostic_policy_input(binding, run_root=self.root / "run", require_project_binding=True)

    def test_raw_policy_is_authoritative_over_forged_typed_lock_and_snapshot_escape(self):
        binding, policies = self.bind()
        forged_budget = policies.budget.model_copy(update={"total_tokens": 2_000_000})
        forged = binding.model_copy(update={
            "budget_policy": forged_budget.model_dump(mode="json"),
            "policy_digest": sha256_digest({
                "budget": forged_budget,
                "role_timeouts": policies.role_timeouts,
                "codex_project": policies.codex_project,
            }),
        })
        with self.assertRaisesRegex(ValueError, "TYPED_DIGEST_CHANGED"):
            verify_diagnostic_policy_input(forged, run_root=self.root / "run", require_project_binding=True)
        escaped = binding.model_copy(update={
            "source_snapshots": binding.source_snapshots | {"budget_policy": "../budget.json"},
        })
        with self.assertRaisesRegex(ValueError, "POLICY_INPUT_CHANGED"):
            verify_diagnostic_policy_input(escaped, run_root=self.root / "run", require_project_binding=True)

    def test_timeout_is_bound_and_same_goal_uses_one_revision_independent_state_root(self):
        binding, policies = self.bind()
        verified = verify_diagnostic_policy_input(binding, run_root=self.root / "run", require_project_binding=True)
        with use_role_timeout_policy(verified.role_timeouts):
            request = make_role_request(
                role="compact_plan_reviewer", instructions="test", payload={"case": "one"},
                output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
                inventory_digest=sha256_digest("inventory"), cwd=str(self.workspace),
            )
        self.assertEqual(request.timeout_seconds, 30)
        self.assertEqual(request.timeout_policy_digest, verified.role_timeouts.policy_digest)
        unbound = make_role_request(
            role="compact_plan_reviewer", instructions="test", payload={"case": "unbound"},
            output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
            inventory_digest=sha256_digest("inventory"), cwd=str(self.workspace),
        )
        with self.assertRaisesRegex(ValueError, "ROLE_TIMEOUT_POLICY_BINDING_MISMATCH"):
            verify_role_timeout_binding(
                role=unbound.role, timeout_seconds=unbound.timeout_seconds,
                timeout_policy_digest=unbound.timeout_policy_digest,
                policy=verified.role_timeouts,
            )
        project_id = "project_" + "4" * 32
        first = goal(project_id, sha256_digest("fixture-profile"))
        registry = DiagnosticBudgetRegistry(run_root=self.root / "run", workspace=self.workspace, policies=verified)
        first_service = registry.service_for(first)
        second_definition = first.definition.model_copy(update={
            "observable_outcome": "동일 Goal 계보의 수정된 관측 결과가 존재한다.",
        })
        second = first.model_copy(update={
            "goal_revision_id": new_id("goal_revision"), "revision_no": 2,
            "supersedes_goal_revision_id": first.goal_revision_id,
            "definition": second_definition, "definition_digest": second_definition.definition_digest,
        })
        self.assertIs(first_service, registry.service_for(second))
        # key는 revision digest를 포함하지 않으므로 같은 Goal 계보의 후속 revision도 별도 cap을 얻지 못한다.
        self.assertTrue(diagnostic_budget_state_root(
            self.root / "run", project_id=project_id, goal_id=first.goal_id,
        ).joinpath("flowmarshal-engine.sqlite3").is_file())

    def test_goal_binding_avoids_nested_windows_path_limit_and_preserves_full_digest(self):
        _binding, policies = self.bind()
        # 실제 검증 checkout에서는 기존 budget key + Goal digest 경로가 264자였다.
        # 짧은 임시 디렉터리에서만 검사하면 이 파일 열기 실패가 가려진다.
        run = self.root / ("deep-" + "x" * max(1, 110 - len(str(self.root)) - 6))
        project_id = "project_" + "9" * 32
        current_goal = goal(project_id, sha256_digest("fixture-profile"))
        digest = current_goal.definition_digest.split(":", 1)[1]
        state_root = diagnostic_budget_state_root(run, project_id=project_id, goal_id=current_goal.goal_id)
        nested = state_root / "goal-bindings" / f"{digest}.json"
        self.assertGreaterEqual(len(str(nested)), 260)
        registry = DiagnosticBudgetRegistry(run_root=run, workspace=self.workspace, policies=policies)
        service = registry.service_for(current_goal)
        binding_path = run / "goal-bindings" / f"{digest}.json"
        self.assertLess(len(str(binding_path)), 260)
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        self.assertEqual(current_goal.model_dump(mode="json"), binding["goal_contract"])
        self.assertEqual(current_goal.definition_digest, binding["goal_definition_digest"])
        self.assertIn(str(binding_path.relative_to(run)), _s06_module().generation_input_manifest(run))
        self.assertIs(service, registry.service_for(current_goal))
        binding_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DIAGNOSTIC_GOAL_BINDING_CHANGED"):
            registry.service_for(current_goal)

    def test_unknown_usage_blocks_later_call_in_same_goal_ledger(self):
        _binding, policies = self.bind()
        project_id = "project_" + "5" * 32
        current_goal = goal(project_id, sha256_digest("fixture-profile"))
        registry = DiagnosticBudgetRegistry(run_root=self.root / "run", workspace=self.workspace, policies=policies)
        with use_role_timeout_policy(policies.role_timeouts):
            request = make_role_request(
                role="compact_plan_reviewer", instructions="test", payload={"case": "one"},
                output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
                inventory_digest=sha256_digest("inventory"), cwd=str(self.workspace),
            )
        runner = _UnknownUsageRunner()
        registry.runner_for(runner, current_goal).run(request)
        observation = diagnostic_budget_ledger_observation(self.root / "run")
        self.assertTrue(observation["unresolved_provider_call_ids"])
        self.assertTrue(observation["all_history_chains_valid"])
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_USAGE_UNKNOWN"):
            registry.runner_for(runner, current_goal).run(request)

    def test_diagnostic_snapshot_distinguishes_original_receipt_from_late_observation(self):
        _binding, policies = self.bind()
        run = self.root / "observed-run"
        project_id = "project_" + "3" * 32
        current_goal = goal(project_id, sha256_digest("fixture-profile"))
        registry = DiagnosticBudgetRegistry(run_root=run, workspace=self.workspace, policies=policies)
        with use_role_timeout_policy(policies.role_timeouts):
            request = make_role_request(
                role="compact_plan_reviewer", instructions="test", payload={"case": "late"},
                output_schema={"type": "object", "properties": {}}, model="test-model",
                effort="medium", inventory_digest=sha256_digest("inventory"), cwd=str(self.workspace),
            )
        runner = _UnknownUsageRunner()
        registry.runner_for(runner, current_goal).run(request)
        service = registry.service_for(current_goal)
        with service.ledger.read() as connection:
            call_id = connection.execute("SELECT id FROM provider_calls").fetchone()["id"]
        BudgetManager(service).observe_role_terminal(call_id, RuntimeObservation(
            thread_id="thread-one", turn_id="turn-one", active=False,
            terminal_status="completed", final_response="완료",
            payload={"usage_scope": "turn", "usage": {
                "inputTokens": 3, "cachedInputTokens": 0, "outputTokens": 2,
                "reasoningOutputTokens": 0, "totalTokens": 5,
            }},
        ))

        snapshot = diagnostic_budget_ledger_observation(run)
        entry = next(iter(snapshot["entries"].values()))
        self.assertEqual([], snapshot["unresolved_provider_call_ids"])
        self.assertTrue(snapshot["all_observations_well_formed"])
        self.assertFalse(entry["provider_receipts"][0]["receipt"]["usage_available"])
        self.assertEqual("runtime_observation", entry["effective_provider_usage"][0]["source"])
        self.assertEqual(5, entry["effective_provider_usage"][0]["actual_tokens"])
        self.assertEqual(1, len(entry["provider_observations"][0]["observations"]))

    def test_generation_pending_rejects_late_input_and_ledger_rollback(self):
        module = _s06_module()
        _binding, policies = self.bind()
        run = self.root / "pending-run"
        run.mkdir()
        (run / "preflight.json").write_text(json.dumps({
            "diagnostic_policy_input": {"bound": True},
        }), encoding="utf-8")
        project_id = "project_" + "7" * 32
        current_goal = goal(project_id, sha256_digest("fixture-profile"))
        DiagnosticBudgetRegistry(run_root=run, workspace=self.workspace, policies=policies).service_for(current_goal)
        for index in range(12):
            capture = run / "calls" / f"{index:02d}"
            capture.mkdir(parents=True)
            (capture / "result.json").write_text("{}", encoding="utf-8")
            (capture / "turn.intent.json").write_text("{}", encoding="utf-8")
        pending = {
            "status": "GENERATION_REVIEW_REQUIRED", "checks": {"bound": True},
            "logical_calls": 12, "provider_turns": 12,
            "generation_input_manifest": module.generation_input_manifest(run),
            "generation_ledger_snapshot": diagnostic_budget_ledger_observation(run),
        }
        (run / "generation-pending.json").write_text(json.dumps(pending), encoding="utf-8")
        (run / "generation-assessment.json").write_text("{}", encoding="utf-8")
        (run / "generation-review-notes.md").write_text("독립 검토 기록", encoding="utf-8")
        module.verify_generation_pending(run)
        pending_path = run / "generation-pending.json"
        for missing in ("generation_input_manifest", "generation_ledger_snapshot"):
            with self.subTest(missing=missing):
                altered = dict(pending)
                altered.pop(missing)
                pending_path.write_text(json.dumps(altered), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "PENDING_(INPUT|LEDGER)_CHANGED"):
                    module.verify_generation_pending(run)
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        original_result = run / "calls" / "00" / "result.json"
        original_result.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "PENDING_INPUT_CHANGED"):
            module.verify_generation_pending(run)
        original_result.write_text("{}", encoding="utf-8")
        (run / "late-input.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "PENDING_INPUT_CHANGED"):
            module.verify_generation_pending(run)
        (run / "late-input.json").unlink()
        db = next((run / "budget-state").glob("*/flowmarshal-engine.sqlite3"))
        db.unlink()
        with self.assertRaisesRegex(RuntimeError, "PENDING_LEDGER_CHANGED"):
            module.verify_generation_pending(run)

    def test_summary_cannot_publish_pass_when_last_budget_receipt_usage_is_unknown(self):
        module = _s06_module()
        _binding, policies = self.bind()
        run = self.root / "summary-run"
        workspace = run / "workspace"
        workspace.mkdir(parents=True)
        project_id = "project_" + "8" * 32
        current_goal = goal(project_id, sha256_digest("fixture-profile"))
        registry = DiagnosticBudgetRegistry(run_root=run, workspace=workspace, policies=policies)
        with use_role_timeout_policy(policies.role_timeouts):
            request = make_role_request(
                role="compact_plan_reviewer", instructions="test", payload={"case": "one"},
                output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
                inventory_digest=sha256_digest("inventory"), cwd=str(workspace),
            )
        provider = _UnknownUsageRunner()
        registry.runner_for(provider, current_goal).run(request)
        raw_receipt = provider.receipts[0].model_dump(mode="json")
        (run / "preflight.json").write_text(json.dumps({
            "source_manifest_digest": "source", "original_files": {}, "maximum_logical_calls": 13,
            "maximum_provider_turns": 13, "diagnostic_policy_input": {"bound": True},
            "execution_mode": "qualification", "lock_digest": "sha256:" + "0" * 64,
        }), encoding="utf-8")
        (run / "instruction-binding.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
        original = (module.source_manifest_digest, module.preserved_files, module.files, module.collect_call_artifacts)
        try:
            module.source_manifest_digest = lambda _root: "source"
            module.preserved_files = lambda _run: {}
            module.files = lambda _root: {}
            module.collect_call_artifacts = lambda _run: {
                "calls": [], "receipts": [raw_receipt], "receipt_sources": {},
                "issues": [], "provider_turn_usage": [],
            }
            summary = module.summarize(run, "PASS")
        finally:
            (module.source_manifest_digest, module.preserved_files, module.files,
             module.collect_call_artifacts) = original
        self.assertEqual(summary["status"], "FAIL")
        self.assertFalse(summary["checks"]["budget_ledger_usage_complete"])
        self.assertFalse(summary["checks"]["provider_receipt_usage_available"])
        self.assertFalse(summary["checks"]["provider_terminal_usage_complete"])
        self.assertGreater(summary["new_ledger_writes"], 0)

    def test_budgeted_recorded_wrapper_calls_its_method_and_settles(self):
        _binding, policies = self.bind()
        project_id = "project_" + "6" * 32
        current_goal = goal(project_id, sha256_digest("fixture-profile"))
        run = self.root / "run"
        (run / "input-goal.json").parent.mkdir(parents=True, exist_ok=True)
        (run / "input-goal.json").write_text(
            json.dumps(current_goal.model_dump(mode="json")), encoding="utf-8",
        )
        registry = DiagnosticBudgetRegistry(run_root=run, workspace=self.workspace, policies=policies)
        wrapper = _s06_module().BudgetedRecordedRunner(_KnownUsageRecordedRunner(), registry, run)
        wrapper.name = "clean"
        with use_role_timeout_policy(policies.role_timeouts):
            request = make_role_request(
                role="compact_plan_reviewer", instructions="test", payload={"case": "one"},
                output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
                inventory_digest=sha256_digest("inventory"), cwd=str(self.workspace),
            )
        self.assertEqual(wrapper.run(request).payload, {"ok": True})
        self.assertEqual(wrapper.runner.name, "clean")

    def test_project_bound_instruction_probe_is_persistent_and_capturing_runtime_accepts_it(self):
        module = _s06_module()
        _binding, policies = self.bind()
        roles = SimpleNamespace(general_reviewer=SimpleNamespace(model="test-model"))
        run = self.root / "probe-run"
        workspace = run / "workspace"
        workspace.mkdir(parents=True)
        args = module.instruction_probe_arguments(run, roles, policies)
        self.assertIs(args["ephemeral"], False)
        capture = run / "runtime-preflight" / "prepare"
        capture.mkdir(parents=True)
        (run / "preflight.json").write_text(json.dumps({"role_threads_ephemeral": False}), encoding="utf-8")
        (run / "instruction-binding.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
        rollout = self.root / "rollout.jsonl"
        rollout.write_text("", encoding="utf-8")
        runtime = object.__new__(module.CapturingRuntime)
        runtime.run, runtime.phase, runtime.capture, runtime.indices = run, "prepare", capture, {}
        runtime._project_binding = policies.codex_project
        runtime._ephemeral_thread_ids, runtime._first_empty_threads = set(), set()
        runtime._new_thread_project_proofs, runtime._turn_futures = {}, {}
        runtime._verify_project_binding = lambda: None
        runtime.verify_execution_policy = lambda _cwd: None
        runtime._raw = lambda method, params: ({
            "activePermissionProfile": ":danger-full-access", "approvalPolicy": "never",
            "cwd": str(workspace.resolve()), "model": "test-model", "instructionSources": [],
            "thread": {"id": "thread-probe", "projectId": policies.codex_project.project_id,
                       "ephemeral": False, "turns": [], "path": str(rollout.resolve())},
        } if method == "thread/start" else (_ for _ in ()).throw(AssertionError(method)))
        receipt = module.CapturingRuntime.create_thread(runtime, **args)
        self.assertEqual(receipt.payload["thread"]["projectId"], policies.codex_project.project_id)
        self.assertFalse(receipt.payload["thread"]["ephemeral"])


if __name__ == "__main__":
    unittest.main()
