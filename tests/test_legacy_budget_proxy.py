from __future__ import annotations

import tempfile
import threading
import time
import unittest
import copy
from pathlib import Path
from types import SimpleNamespace

from flowmarshal.engine.budget import BudgetBlocked, GoalBudgetPolicy
from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark import (
    _legacy_hard_timeout_contract,
    _verify_legacy_budget_evidence,
)
from flowmarshal.engine.domain import BudgetStage
from flowmarshal.engine.evaluation_budget import (
    EvaluationPolicies,
    initialize_cell_budget,
    register_and_attach_goal,
)
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.model_lock import ModelCapability, ModelInventory
from flowmarshal.engine.roles import strict_json_output_schema
from flowmarshal.legacy_budget_proxy import (
    BudgetedPolicyVerifiedCodex,
    LEGACY_SCHEMA_RECOVERY_ERROR,
    LegacyBudgetJournal,
    LegacyRoleScope,
    build_legacy_budget_evidence,
    legacy_role_scope,
)
from flowmarshal.planning.r31_domain import PlanningRole
from flowmarshal.planning.r31_models import _normalize_model_inventory
from tests.engine_helpers import goal, profile


class FakeHandle:
    def __init__(self, owner, *, block=False):
        self.owner = owner
        self.id = f"turn-{owner.turn_effects}"
        self.block = block
        self.release = threading.Event()

    def run(self):
        if self.block:
            self.release.wait(5)
        return SimpleNamespace(
            id=self.id,
            status=SimpleNamespace(value="completed"),
            final_response="{}",
            duration_ms=5,
            usage=None if self.owner.missing_usage else SimpleNamespace(
                last=SimpleNamespace(
                    input_tokens=5,
                    cached_input_tokens=2,
                    output_tokens=3,
                    reasoning_output_tokens=1,
                ),
                total=SimpleNamespace(
                    input_tokens=500,
                    cached_input_tokens=200,
                    output_tokens=300,
                    reasoning_output_tokens=100,
                ),
            ),
        )

    def interrupt(self):
        self.owner.interrupts += 1
        if self.owner.block_interrupt:
            self.owner.interrupt_release.wait(5)
        if self.owner.complete_on_interrupt:
            self.release.set()
            time.sleep(0.02)
        return {"interrupted": True, "turn_id": self.id}


class FakeThread:
    def __init__(self, owner):
        self.owner = owner
        self.id = f"thread-{owner.thread_effects}"

    def turn(self, _input, **_kwargs):
        self.owner.turn_effects += 1
        handle = FakeHandle(self.owner, block=self.owner.block)
        self.owner.handles.append(handle)
        return handle


class FakeVerifiedCodex:
    def __init__(
        self, reservation_probe, *, block=False, missing_usage=False,
        complete_on_interrupt=False, block_thread_start=False, block_interrupt=False,
    ):
        self.reservation_probe = reservation_probe
        self.block = block
        self.missing_usage = missing_usage
        self.complete_on_interrupt = complete_on_interrupt
        self.block_thread_start = block_thread_start
        self.block_interrupt = block_interrupt
        self.thread_start_release = threading.Event()
        self.interrupt_release = threading.Event()
        self.thread_effects = 0
        self.turn_effects = 0
        self.interrupts = 0
        self.handles = []

    def models(self, *, include_hidden=False):
        return {"data": [{
            "id": "legacy-model",
            "display_name": "Legacy",
            "supported_reasoning_efforts": ["medium"],
        }]}

    def verify_execution_policy(self, _cwd):
        return {"permission_profile": ":danger-full-access", "approval_policy": "never"}

    def thread_start(self, **_kwargs):
        self.reservation_probe()
        if self.block_thread_start:
            self.thread_start_release.wait(5)
        self.thread_effects += 1
        return FakeThread(self)

    def close(self):
        return None


class LegacyBudgetProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.project_id = "project_" + "1" * 32
        self.state_root = Path(self.temp.name) / "legacy-state"
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest, read_only=True)

    def tearDown(self):
        for handle in getattr(self, "handles", []):
            handle.release.set()
        for wrapped in getattr(self, "wrapped_clients", []):
            wrapped.thread_start_release.set()
            wrapped.interrupt_release.set()
        self.temp.cleanup()

    def setup_proxy(
        self, *, timeout=1.0, block=False, missing_usage=False,
        complete_on_interrupt=False, block_thread_start=False, block_interrupt=False,
    ):
        policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=timeout),
        )
        service, manager = initialize_cell_budget(
            state_root=self.state_root / "engine-budget",
            workspace=self.workspace,
            project_id=self.project_id,
            profile=self.profile,
            policies=policies,
        )
        register_and_attach_goal(service, manager, self.goal)
        binding = {
            "format": "flowmarshal-legacy-budget-cell-v1",
            "project_id": self.project_id,
            "goal_id": self.goal.goal_id,
            "goal_contract_digest": self.goal.definition_digest,
            "evaluation_policy_digest": policies.policy_digest,
            "implementation": "r31_baseline",
            "ephemeral_threads": True,
            "max_schema_recovery_attempts": 0,
        }
        def reservation_probe():
            with service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT status FROM provider_calls ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual("reserved", row["status"])

        wrapped = FakeVerifiedCodex(
            reservation_probe,
            block=block,
            missing_usage=missing_usage,
            complete_on_interrupt=complete_on_interrupt,
            block_thread_start=block_thread_start,
            block_interrupt=block_interrupt,
        )
        self.wrapped_clients = [*getattr(self, "wrapped_clients", []), wrapped]
        legacy_inventory_digest = sha256_digest(_normalize_model_inventory(wrapped.models()))
        inventory = ModelInventory(
            source="test:model/list",
            models=(ModelCapability(model="legacy-model", supported_efforts=("medium",)),),
            executable_digest="sha256:" + "a" * 64,
        )
        timeout_contract = _legacy_hard_timeout_contract(policies)
        binding.update({
            "role_configuration_digest": "sha256:" + "b" * 64,
            "expected_engine_role_bindings": {
                "normalizer": {"model": "legacy-model", "effort": "medium"},
            },
            "expected_role_bindings": {
                "purpose_resolver": {"model": "legacy-model", "effort": "medium"},
            },
            "expected_engine_inventory": inventory.model_dump(mode="json"),
            "engine_inventory_digest": inventory.inventory_digest,
            "legacy_inventory_digest": legacy_inventory_digest,
            "codex_executable_digest": inventory.executable_digest,
            "parent_hard_timeout_contract": timeout_contract,
            "parent_hard_timeout_seconds": timeout_contract["timeout_seconds"],
            "parent_hard_timeout_policy_digest": timeout_contract["policy_digest"],
            "budget_policy_digest": sha256_digest(policies.budget),
            "role_timeout_policy_digest": policies.role_timeouts.policy_digest,
        })
        journal = LegacyBudgetJournal(binding)
        proxy = BudgetedPolicyVerifiedCodex(
            wrapped,
            manager,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest,
            policies=policies,
            cell_binding=binding,
            role_instructions={PlanningRole.PURPOSE_RESOLVER: "purpose instructions"},
            journal=journal,
            expected_inventory_digest=legacy_inventory_digest,
        )
        proxy.models()
        return proxy, wrapped, service, manager, journal, policies

    def lazy_thread(self, proxy):
        with legacy_role_scope(LegacyRoleScope(
            stage=BudgetStage.GOAL_NORMALIZATION,
            allowed_roles=(PlanningRole.PURPOSE_RESOLVER,),
        )):
            return proxy.thread_start(
                base_instructions="purpose instructions",
                cwd=str(self.workspace),
                ephemeral=True,
                model="legacy-model",
                approval_mode=SimpleNamespace(value="deny_all"),
                sandbox=SimpleNamespace(value="full-access"),
            )

    @staticmethod
    def run_turn(thread):
        return thread.run(
            '{"input":true}',
            effort=SimpleNamespace(value="medium"),
            model="legacy-model",
            output_schema=strict_json_output_schema({
                "type": "object", "properties": {},
            }),
            sandbox=SimpleNamespace(value="full-access"),
        )

    def test_lazy_start_reserves_before_thread_and_settles_only_usage_last(self):
        proxy, wrapped, service, manager, journal, _policies = self.setup_proxy()
        thread = self.lazy_thread(proxy)
        self.assertEqual((0, 0), (wrapped.thread_effects, wrapped.turn_effects))
        result = self.run_turn(thread)
        self.assertEqual("{}", result.final_response)
        self.assertEqual((1, 1), (wrapped.thread_effects, wrapped.turn_effects))
        with service.ledger.read() as connection:
            row = connection.execute("SELECT * FROM provider_calls").fetchone()
        self.assertEqual("settled", row["status"])
        self.assertEqual(8, row["actual_tokens"])
        evidence = build_legacy_budget_evidence(
            manager,
            journal,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            legacy_receipts=[SimpleNamespace(turn_ids=(result.id,))],
        )
        self.assertTrue(evidence["turn_coverage_complete"])
        self.assertEqual(5, evidence["calls"][0]["receipt"]["input_tokens"])
        self.assertEqual(3, evidence["calls"][0]["receipt"]["output_tokens"])

    def test_second_run_is_blocked_before_reservation_or_provider_effect(self):
        proxy, wrapped, service, _manager, _journal, _policies = self.setup_proxy()
        thread = self.lazy_thread(proxy)
        self.run_turn(thread)
        with self.assertRaisesRegex(RuntimeError, LEGACY_SCHEMA_RECOVERY_ERROR):
            self.run_turn(thread)
        self.assertEqual((1, 1), (wrapped.thread_effects, wrapped.turn_effects))
        with service.ledger.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0]
        self.assertEqual(1, count)

    def test_parent_verifies_sqlite_evidence_and_exact_turn_coverage(self):
        proxy, _wrapped, _service, manager, journal, policies = self.setup_proxy()
        result = self.run_turn(self.lazy_thread(proxy))
        legacy_receipt = SimpleNamespace(turn_ids=(result.id,))
        evidence = build_legacy_budget_evidence(
            manager,
            journal,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            legacy_receipts=[legacy_receipt],
        )
        raw = {
            "receipts": [{
                "call_id": "raw-call-1",
                "status": "succeeded",
                "schema_recovery_attempts": 0,
                "role": "purpose_resolver",
                "model_id": "legacy-model",
                "reasoning_effort": "medium",
                "inventory_digest": evidence["calls"][0]["receipt"]["inventory_digest"],
                "output_schema_digest": evidence["calls"][0]["receipt"]["output_schema_digest"],
                "output_digest": evidence["calls"][0]["receipt"]["output_digest"],
                "thread_id": evidence["calls"][0]["receipt"]["thread_id"],
                "turn_ids": [result.id],
                "token_count": 8,
                "usage": [
                    {"name": f"{scope}.{name}", "value": value}
                    for scope in ("last", "total")
                    for name, value in (
                        ("input_tokens", 5),
                        ("cached_input_tokens", 2),
                        ("output_tokens", 3),
                        ("reasoning_output_tokens", 1),
                        ("total_tokens", 8),
                    )
                ],
            }],
            "budget_evidence": evidence,
        }
        _verify_legacy_budget_evidence(
            raw=raw,
            expected_binding=journal.cell_binding,
            state_root=self.state_root,
            evaluation_policies=policies,
        )
        tampered = dict(raw)
        tampered["budget_evidence"] = dict(evidence, turn_coverage_complete=False)
        with self.assertRaisesRegex(Exception, "LEGACY_BUDGET_EVIDENCE_DIGEST_MISMATCH"):
            _verify_legacy_budget_evidence(
                raw=tampered,
                expected_binding=journal.cell_binding,
                state_root=self.state_root,
                evaluation_policies=policies,
            )

        duplicate = copy.deepcopy(raw)
        duplicate["receipts"].append(copy.deepcopy(duplicate["receipts"][0]))
        with self.assertRaisesRegex(Exception, "RAW_RECEIPT_CARDINALITY"):
            _verify_legacy_budget_evidence(
                raw=duplicate, expected_binding=journal.cell_binding,
                state_root=self.state_root, evaluation_policies=policies,
            )

        total_mismatch = copy.deepcopy(raw)
        next(item for item in total_mismatch["receipts"][0]["usage"]
             if item["name"] == "total.input_tokens")["value"] = 500
        with self.assertRaisesRegex(Exception, "RAW_USAGE_SCOPE_MISMATCH"):
            _verify_legacy_budget_evidence(
                raw=total_mismatch, expected_binding=journal.cell_binding,
                state_root=self.state_root, evaluation_policies=policies,
            )

        thread_mismatch = copy.deepcopy(raw)
        thread_mismatch["receipts"][0]["thread_id"] = "foreign-thread"
        with self.assertRaisesRegex(Exception, "RAW_RECEIPT_BINDING"):
            _verify_legacy_budget_evidence(
                raw=thread_mismatch, expected_binding=journal.cell_binding,
                state_root=self.state_root, evaluation_policies=policies,
            )

        invalid_parent_binding = copy.deepcopy(journal.cell_binding)
        invalid_parent_binding["codex_executable_digest"] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(Exception, "PARENT_BINDING_INVALID"):
            _verify_legacy_budget_evidence(
                raw=raw, expected_binding=invalid_parent_binding,
                state_root=self.state_root, evaluation_policies=policies,
            )

        for field, value in (("call_id", "replayed-call"), ("input_tokens", 9)):
            tampered_call = copy.deepcopy(raw)
            tampered_evidence = tampered_call["budget_evidence"]
            tampered_evidence["calls"][0]["receipt"][field] = value
            if field == "input_tokens":
                tampered_evidence["calls"][0]["actual_tokens"] = 12
            evidence_body = dict(tampered_evidence)
            evidence_body.pop("evidence_digest")
            tampered_evidence["evidence_digest"] = sha256_digest(evidence_body)
            with self.assertRaisesRegex(Exception, "LEDGER_EVIDENCE"):
                _verify_legacy_budget_evidence(
                    raw=tampered_call, expected_binding=journal.cell_binding,
                    state_root=self.state_root, evaluation_policies=policies,
                )

    def test_missing_usage_is_unknown_and_blocks_following_provider_effect(self):
        proxy, wrapped, service, _manager, _journal, _policies = self.setup_proxy(
            missing_usage=True
        )
        self.run_turn(self.lazy_thread(proxy))
        with service.ledger.read() as connection:
            row = connection.execute("SELECT status,actual_tokens FROM provider_calls").fetchone()
        self.assertEqual("usage_unknown", row["status"])
        self.assertIsNone(row["actual_tokens"])
        second_wrapped = FakeVerifiedCodex(lambda: self.fail("provider effect must be blocked"))
        proxy._wrapped = second_wrapped
        proxy.models()
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_USAGE_UNKNOWN"):
            self.run_turn(self.lazy_thread(proxy))
        self.assertEqual(0, second_wrapped.thread_effects)

    def test_timeout_requests_interrupt_keeps_unknown_and_blocks_next_call(self):
        proxy, wrapped, service, _manager, _journal, _policies = self.setup_proxy(
            timeout=0.01, block=True
        )
        thread = self.lazy_thread(proxy)
        with self.assertRaisesRegex(TimeoutError, "LEGACY_ROLE_TIMEOUT"):
            self.run_turn(thread)
        self.handles = wrapped.handles
        self.assertEqual(1, wrapped.interrupts)
        with service.ledger.read() as connection:
            row = connection.execute("SELECT status,actual_tokens,receipt_json FROM provider_calls").fetchone()
        self.assertEqual("reserved", row["status"])
        self.assertIsNone(row["actual_tokens"])
        self.assertIn("interrupt_request_digest", row["receipt_json"])

        second_wrapped = FakeVerifiedCodex(lambda: self.fail("provider effect must be blocked"))
        proxy._wrapped = second_wrapped
        proxy.models()
        next_thread = self.lazy_thread(proxy)
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_USAGE_UNKNOWN"):
            self.run_turn(next_thread)
        self.assertEqual(0, second_wrapped.thread_effects)

    def test_thread_start_and_interrupt_rpcs_use_policy_watchdogs(self):
        proxy, wrapped, service, _manager, journal, _policies = self.setup_proxy(
            timeout=0.01, block_thread_start=True
        )
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "LEGACY_THREAD_START_TIMEOUT"):
            self.run_turn(self.lazy_thread(proxy))
        self.assertLess(time.monotonic() - started, 0.5)
        with service.ledger.read() as connection:
            self.assertEqual("reserved", connection.execute(
                "SELECT status FROM provider_calls"
            ).fetchone()[0])
        self.assertEqual("effect_unknown", journal.records[0]["outcome"])

        self.state_root = Path(self.temp.name) / "legacy-state-interrupt"
        proxy, wrapped, _service, _manager, journal, _policies = self.setup_proxy(
            timeout=0.01, block=True, block_interrupt=True
        )
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "LEGACY_ROLE_TIMEOUT"):
            self.run_turn(self.lazy_thread(proxy))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("LEGACY_INTERRUPT_TIMEOUT", journal.records[0]["interrupt_error"])

    def test_timeout_records_terminal_only_when_observed_after_interrupt(self):
        proxy, wrapped, service, _manager, _journal, _policies = self.setup_proxy(
            timeout=0.01, block=True, complete_on_interrupt=True
        )
        with self.assertRaisesRegex(TimeoutError, "LEGACY_ROLE_TIMEOUT"):
            self.run_turn(self.lazy_thread(proxy))
        with service.ledger.read() as connection:
            row = connection.execute("SELECT status,actual_tokens,receipt_json FROM provider_calls").fetchone()
        self.assertEqual("settled", row["status"])
        self.assertEqual(8, row["actual_tokens"])
        self.assertIn('"terminal_status_after_interrupt":"completed"', row["receipt_json"])
        self.assertEqual(1, wrapped.interrupts)


if __name__ == "__main__":
    unittest.main()
