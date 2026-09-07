"""모델 호출 없이 지연 종료·RPC 대기·운영 정책 결속을 검증한다."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.role_execution import RoleTimeoutPolicy, use_role_timeout_policy, verify_role_timeout_binding
from flowmarshal.engine.roles import (
    CodexStructuredRoleRunner, RoleCallRequest, ScriptedStructuredRoleRunner,
    StructuredRoleError, make_role_request, verify_role_receipt,
)
from flowmarshal.engine.runtime import RuntimeObservation
from flowmarshal.engine.runtime_observation import RoleObservationPolicy
from tests.test_engine_roles import ImmediateRoleRuntime, InterruptedRoleRuntime


class DelayedTerminalRuntime(InterruptedRoleRuntime):
    def __init__(self):
        super().__init__()
        self.post_interrupt_reads = 0
        self.interrupt_count = 0

    def interrupt(self, **kwargs):
        self.interrupt_count += 1
        return super().interrupt(**kwargs)

    def read(self, *, thread_id):
        if self.interrupted:
            self.post_interrupt_reads += 1
            if self.post_interrupt_reads == 1:
                return RuntimeObservation(thread_id=thread_id, turn_id=self.latest[thread_id][0], active=True, payload={})
        return super().read(thread_id=thread_id)


class RoleObservationPolicyTests(unittest.TestCase):
    def request(self, runtime, root, *, policy=None, timeout=0.001):
        return make_role_request(
            inventory=runtime.inventory, role="test-role", instructions="모의 JSON 검사",
            payload={}, output_schema={"type": "object", "properties": {}},
            model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
            cwd=root, timeout_seconds=timeout, **({} if policy is None else {"observation_policy": policy}),
        )

    def test_legacy_serialization_and_explicit_new_policy_binding(self):
        runtime = ImmediateRoleRuntime(["{}"])
        with tempfile.TemporaryDirectory() as root:
            legacy = self.request(runtime, root)
            document = legacy.model_dump(mode="json")
            self.assertNotIn("observation_policy", document)
            self.assertNotIn("observation_policy_digest", document)
            self.assertEqual(legacy.request_digest, RoleCallRequest.model_validate(document).request_digest)
            self.assertNotIn("observation_policy", RoleTimeoutPolicy().model_dump(mode="json"))
            policy = RoleTimeoutPolicy(default_timeout_seconds=1800, observation_policy=RoleObservationPolicy())
            with use_role_timeout_policy(policy):
                request = self.request(runtime, root, timeout=1800)
            self.assertEqual(30, request.observation_policy.interrupt_observation_seconds)
            self.assertEqual(5, request.observation_policy.rpc_timeout_seconds)
            result = CodexStructuredRoleRunner(runtime).run(request)
            verify_role_receipt(request, result)
            self.assertEqual(request.observation_policy.policy_digest, result.receipt.observation_policy_digest)
            verify_role_timeout_binding(role=request.role, timeout_seconds=request.timeout_seconds,
                timeout_policy_digest=request.timeout_policy_digest, policy=policy,
                observation_policy=request.observation_policy, observation_policy_digest=request.observation_policy_digest)
            forged = result.model_copy(update={"receipt": result.receipt.model_copy(update={"observation_policy_digest": sha256_digest("other")})})
            with self.assertRaisesRegex(StructuredRoleError, "ROLE_RECEIPT_BINDING_MISMATCH"):
                verify_role_receipt(request, forged)
            scripted = ScriptedStructuredRoleRunner({"test-role": [{}]}).run(request)
            verify_role_receipt(request, scripted)

    def test_policy_tamper_and_runner_override_fail_before_effect(self):
        runtime = ImmediateRoleRuntime([])
        with tempfile.TemporaryDirectory() as root:
            request = self.request(runtime, root, policy=RoleObservationPolicy())
            with self.assertRaises(ValueError):
                RoleCallRequest.model_validate(request.model_dump(mode="json") | {"observation_policy_digest": sha256_digest("forged")})
            with self.assertRaises(StructuredRoleError) as error:
                CodexStructuredRoleRunner(runtime, interrupt_observation_seconds=0).run(request)
            self.assertFalse(error.exception.effects_started)
            self.assertEqual({}, runtime.latest)
        for value in (-1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RoleObservationPolicy(interrupt_observation_seconds=value)

    def test_delayed_terminal_is_captured_without_inventing_usage(self):
        runtime = DelayedTerminalRuntime()
        policy = RoleObservationPolicy(interrupt_observation_seconds=0.05, rpc_timeout_seconds=0.02)
        with tempfile.TemporaryDirectory() as root:
            runner = CodexStructuredRoleRunner(runtime, poll_interval_seconds=0.001)
            with self.assertRaises(StructuredRoleError) as error:
                runner.run(self.request(runtime, root, policy=policy))
        receipt = error.exception.receipt
        self.assertEqual("timed_out", receipt.status)
        self.assertEqual("interrupted", receipt.terminal_status_after_interrupt)
        self.assertFalse(receipt.usage_available)
        self.assertIsNone(receipt.input_tokens)
        self.assertEqual(1, runtime.interrupt_count)
        self.assertGreaterEqual(runtime.post_interrupt_reads, 2)
        self.assertEqual(receipt.turn_ids[0], runner.pending_terminal_observations[receipt.call_id].turn_id)

    def test_hung_interrupt_or_read_returns_within_recovery_deadline(self):
        for operation in ("interrupt", "read"):
            release = threading.Event()
            class HangingRuntime(InterruptedRoleRuntime):
                def interrupt(self, **kwargs):
                    if operation == "interrupt":
                        release.wait()
                    return super().interrupt(**kwargs)
                def read(self, **kwargs):
                    if operation == "read" and self.interrupted:
                        release.wait()
                    return super().read(**kwargs)
            runtime = HangingRuntime()
            progress = []
            policy = RoleObservationPolicy(interrupt_observation_seconds=0.06, rpc_timeout_seconds=0.02)
            try:
                with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                    runner = CodexStructuredRoleRunner(runtime, poll_interval_seconds=0.001, progress_sink=progress.append)
                    started = time.monotonic()
                    with self.assertRaises(StructuredRoleError) as error:
                        runner.run(self.request(runtime, root, policy=policy))
                    self.assertLess(time.monotonic() - started, 0.4)
                    self.assertIsNone(error.exception.receipt.terminal_observation_digest)
                    self.assertFalse(error.exception.receipt.usage_available)
                    self.assertTrue(any("failed" in entry["event"] for entry in progress))
            finally:
                release.set()

    def test_late_callback_keeps_original_call_sink(self):
        class ObservedRuntime(ImmediateRoleRuntime):
            def register_completion_observer(self, **kwargs):
                self.callback = kwargs["observer"]
        runtime = ObservedRuntime(["{}"])
        original, later = [], []
        runner = CodexStructuredRoleRunner(runtime, progress_sink=original.append)
        with tempfile.TemporaryDirectory() as root:
            result = runner.run(self.request(runtime, root, policy=RoleObservationPolicy()))
        runner.progress_sink = later.append
        runtime.callback(runtime.read(thread_id=result.receipt.thread_id))
        self.assertEqual([], later)
        self.assertEqual(result.receipt.call_id, original[-1]["call_id"])
        self.assertEqual("role_terminal_observed", original[-1]["event"])

    def test_sdk_exception_without_terminal_does_not_claim_final_usage(self):
        class DisconnectedRuntime(ImmediateRoleRuntime):
            def read(self, **kwargs):
                observed = super().read(**kwargs)
                return observed.model_copy(update={"terminal_status": None,
                    "payload": observed.payload | {"error": "SDK transport disconnected"}})
        runtime = DisconnectedRuntime(["{}"])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(StructuredRoleError) as error:
                CodexStructuredRoleRunner(runtime).run(self.request(runtime, root, policy=RoleObservationPolicy()))
        self.assertEqual("external_unknown", error.exception.receipt.status)
        self.assertFalse(error.exception.receipt.usage_available)
        self.assertIsNone(error.exception.receipt.input_tokens)
        from flowmarshal.engine.budget import _terminal_receipt
        self.assertFalse(_terminal_receipt(error.exception.receipt))


if __name__ == "__main__":
    unittest.main()
