from __future__ import annotations

import json

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from flowmarshal.engine.domain import ThreadBinding, new_id, utc_now
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.roles import (
    CodexStructuredRoleRunner,
    RoleCallRequest, make_role_request,
    StructuredRoleError,
    _usage,
    strict_json_output_schema,
)
from flowmarshal.engine.role_execution import (
    RoleTimeoutOverride,
    RoleTimeoutPolicy,
    use_role_timeout_policy,
)
from flowmarshal.engine.runtime import (
    ExecutionPolicyEvidence,
    RuntimeObservation,
    RuntimeOperationReceipt,
)


class ImmediateRoleRuntime:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.latest: dict[str, tuple[str, str]] = {}
        self.inventory = ModelInventory(
            executable_digest="sha256:" + "0" * 64, runtime_capabilities=RUNTIME_CAPABILITIES,
            source="test:model/list",
            models=(ModelCapability(model="available", supported_efforts=("low",)),),
        )

    def verify_execution_policy(self, cwd):
        return ExecutionPolicyEvidence(
            permission_profile=":danger-full-access",
            approval_policy="never",
            config_digest="sha256:" + "1" * 64,
            profile_catalog_digest="sha256:" + "2" * 64,
            cwd=str(Path(cwd)),
        )

    def list_models(self):
        return self.inventory

    def create_thread(self, *, cwd, title, model, developer_instructions, ephemeral=False):
        del cwd, title, model, developer_instructions, ephemeral
        thread_id = new_id("thread")
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload={"thread_id": thread_id, "thread": {"id": thread_id, "turns": []}},
            binding=ThreadBinding(thread_id=thread_id, bound_at=utc_now()),
        )

    def start_turn(self, *, thread_id, cwd, prompt, model, effort, output_schema=None):
        del cwd, prompt, model, effort, output_schema
        turn_id = new_id("turn")
        first_empty_thread = thread_id not in self.latest
        self.latest[thread_id] = (turn_id, self.outputs.pop(0))
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={
                "thread_id": thread_id,
                "turn_id": turn_id,
                "first_empty_thread": first_empty_thread,
            },
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )

    def read(self, *, thread_id):
        turn_id, output = self.latest[thread_id]
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=turn_id,
            active=False,
            terminal_status="completed",
            final_response=output,
            payload={
                "usage": {
                    "total": {
                        "inputTokens": 100,
                        "cachedInputTokens": 40,
                        "outputTokens": 10,
                        "reasoningOutputTokens": 4,
                        "totalTokens": 110,
                    }
                },
                "usage_scope": "thread",
            },
        )

    def resume(self, *, thread_id, cwd):
        raise AssertionError((thread_id, cwd))

    def interrupt(self, *, thread_id, turn_id):
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id, "interrupted": True},
        )

    def close(self):
        return None


class InterruptedRoleRuntime(ImmediateRoleRuntime):
    def __init__(self) -> None:
        super().__init__([])
        self.interrupted = False

    def start_turn(self, *, thread_id, cwd, prompt, model, effort, output_schema=None):
        del cwd, prompt, model, effort, output_schema
        turn_id = new_id("turn")
        self.latest[thread_id] = (turn_id, "")
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={
                "thread_id": thread_id,
                "turn_id": turn_id,
                "first_empty_thread": True,
            },
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )

    def read(self, *, thread_id):
        turn_id, _ = self.latest[thread_id]
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=turn_id,
            active=not self.interrupted,
            terminal_status=None if not self.interrupted else "interrupted",
            final_response=None,
            payload={},
        )

    def interrupt(self, *, thread_id, turn_id):
        self.interrupted = True
        return super().interrupt(thread_id=thread_id, turn_id=turn_id)


class EngineStructuredRoleTests(unittest.TestCase):
    def test_usage_requires_explicit_scope_single_turn_and_complete_exact_counts(self) -> None:
        proof = {
            "thread_id": "thread_1",
            "turn_ids": ("turn_1",),
            "observation_thread_id": "thread_1",
            "observation_turn_id": "turn_1",
            "turn_binding_proven": True,
        }
        valid = {
            "usage_scope": "turn",
            "usage": {
                "inputTokens": 100,
                "cachedInputTokens": 40,
                "outputTokens": 10,
                "reasoningOutputTokens": 4,
                "totalTokens": 110,
            },
        }
        self.assertEqual((100, 40, 10, 4, True), _usage(valid, **proof))

        invalid = (
            {"usage": valid["usage"]},
            {**valid, "usage": {key: value for key, value in valid["usage"].items()
                                  if key != "reasoningOutputTokens"}},
            {**valid, "usage": {**valid["usage"], "inputTokens": True}},
            {**valid, "usage": {**valid["usage"], "inputTokens": -1}},
            {**valid, "usage": {**valid["usage"], "cachedInputTokens": 101}},
            {**valid, "usage": {**valid["usage"], "reasoningOutputTokens": 11}},
            {**valid, "usage": {**valid["usage"], "totalTokens": 109}},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual((None, None, None, None, False), _usage(payload, **proof))
        self.assertEqual(
            (None, None, None, None, False),
            _usage(valid, **{**proof, "turn_ids": ("turn_0", "turn_1")}),
        )

    def test_thread_aggregate_requires_actual_empty_creation_and_first_turn_receipts(self) -> None:
        payload = {
            "usage_scope": "thread",
            "usage": {"total": {
                "inputTokens": 100,
                "cachedInputTokens": 40,
                "outputTokens": 10,
                "reasoningOutputTokens": 4,
                "totalTokens": 110,
            }},
        }
        proof = {
            "thread_id": "thread_1",
            "turn_ids": ("turn_1",),
            "observation_thread_id": "thread_1",
            "observation_turn_id": "turn_1",
            "turn_binding_proven": True,
            "empty_thread_creation_proven": True,
            "first_empty_turn_proven": True,
        }
        self.assertEqual((100, 40, 10, 4, True), _usage(payload, **proof))
        for missing_proof in ("empty_thread_creation_proven", "first_empty_turn_proven"):
            with self.subTest(missing_proof=missing_proof):
                self.assertEqual(
                    (None, None, None, None, False),
                    _usage(payload, **{**proof, missing_proof: False}),
                )

        class MissingCreationProofRuntime(ImmediateRoleRuntime):
            def create_thread(self, **kwargs):
                receipt = super().create_thread(**kwargs)
                return receipt.model_copy(update={"payload": {"thread_id": receipt.operation_id}})

        runtime = MissingCreationProofRuntime(['{"answer":"ok"}'])
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(
                inventory=runtime.inventory, role="test-role", instructions="JSON만 반환",
                payload={"request": "ok"}, output_schema={"type": "object"},
                model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
                cwd=temp,
            )
            receipt = CodexStructuredRoleRunner(runtime).run(request).receipt
        self.assertFalse(receipt.usage_available)
        self.assertEqual((None, None, None, None), (
            receipt.input_tokens, receipt.cached_input_tokens,
            receipt.output_tokens, receipt.reasoning_tokens,
        ))

    def test_permission_preflight_failure_explicitly_records_no_effect_started(self) -> None:
        class MismatchedPolicyRuntime(ImmediateRoleRuntime):
            def verify_execution_policy(self, cwd):
                value = super().verify_execution_policy(cwd)
                return value.model_copy(update={"approval_policy": "on-request"})

        runtime = MismatchedPolicyRuntime([])
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(
                inventory=runtime.inventory, role="test-role", instructions="JSON만 반환",
                payload={"request": "ok"}, output_schema={"type": "object", "properties": {}},
                model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
                cwd=temp,
            )
            with self.assertRaises(StructuredRoleError) as raised:
                CodexStructuredRoleRunner(runtime).run(request)
        self.assertFalse(raised.exception.effects_started)
        self.assertEqual((), raised.exception.receipts)

    def test_role_timeout_policy_is_bound_to_request_and_success_receipt(self) -> None:
        runtime = ImmediateRoleRuntime(['{"answer":"ok"}'])
        policy = RoleTimeoutPolicy(overrides=(RoleTimeoutOverride(
            role="test-role", timeout_seconds=1200, replaces_timeout_seconds=900,
            reason="실제 장시간 reviewer 관측에 맞춘 명시적 운영 변경",
        ),))
        with tempfile.TemporaryDirectory() as temp, use_role_timeout_policy(policy):
            request = make_role_request(
                inventory=runtime.inventory, role="test-role", instructions="JSON만 반환",
                payload={"request": "ok"},
                output_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
                model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
                cwd=temp,
            )
            result = CodexStructuredRoleRunner(runtime).run(request)
        self.assertEqual(1200, request.timeout_seconds)
        self.assertEqual(policy.policy_digest, request.timeout_policy_digest)
        self.assertEqual(policy.policy_digest, result.receipt.timeout_policy_digest)

    def test_timeout_records_interrupt_receipt_then_terminal_observation(self) -> None:
        runtime = InterruptedRoleRuntime()
        policy = RoleTimeoutPolicy(overrides=(RoleTimeoutOverride(
            role="test-role", timeout_seconds=0.001, replaces_timeout_seconds=900,
            reason="unit timeout",
        ),))
        progress = []
        with tempfile.TemporaryDirectory() as temp, use_role_timeout_policy(policy):
            request = make_role_request(
                inventory=runtime.inventory, role="test-role", instructions="JSON만 반환",
                payload={"request": "wait"}, output_schema={"type": "object", "properties": {}},
                model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
                cwd=temp,
            )
            with self.assertRaises(StructuredRoleError) as raised:
                CodexStructuredRoleRunner(
                    runtime, poll_interval_seconds=0,
                    interrupt_observation_seconds=0,
                    progress_sink=progress.append,
                ).run(request)
        receipt = raised.exception.receipt
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual("timed_out", receipt.status)
        self.assertIsNotNone(receipt.interrupt_request_digest)
        self.assertIsNotNone(receipt.interrupt_receipt_digest)
        self.assertIsNotNone(receipt.terminal_observation_digest)
        self.assertEqual("interrupted", receipt.terminal_status_after_interrupt)
        self.assertEqual(
            ["interrupt_requested", "interrupt_receipt", "terminal_observed_after_interrupt"],
            [item["event"] for item in progress if item["event"].startswith("interrupt")
             or item["event"].startswith("terminal_")],
        )

    def test_plan_reviewer_transport_schema_and_post_validator_share_two_review_branches(self) -> None:
        """전송 strict schema와 Pydantic 사후 검증이 같은 두 review 조합만 허용한다."""
        from flowmarshal.engine.planner_roles import PlanReviewEnvelope

        schema = strict_json_output_schema(PlanReviewEnvelope.model_json_schema())
        review = schema["properties"]["review"]
        branches = review["anyOf"]
        self.assertNotIn("allOf", schema)
        self.assertEqual(2, len(branches))
        self.assertEqual(0, branches[0]["properties"]["findings"]["maxItems"])
        self.assertEqual(1, branches[1]["properties"]["findings"]["minItems"])
        self.assertEqual({"type": "null"}, branches[1]["properties"]["ratings"])
        ratings = branches[0]["properties"]["ratings"]["anyOf"][0]
        self.assertEqual(
            ["goal_fit", "grounding", "engineering", "verification", "execution_safety"],
            ratings["required"],
        )
        for field in ratings["properties"].values():
            self.assertEqual((0, 4), (field["minimum"], field["maximum"]))

        inspection = {
            "citations": [{"citation_id": "C1", "source_ref": "source:goal", "selector": "/x", "quote": "x"}],
            "ac_validation_rows": [], "constraint_task_rows": [], "validation_rows": [],
            "validation_scope_rows": [], "finding_links": [],
        }
        ratings_at_bounds = {
            "goal_fit": 0, "grounding": 1, "engineering": 2, "verification": 3, "execution_safety": 4,
        }
        finding = {
            "finding_code": "F1", "gate": "verification", "severity": "error", "summary": "직접 근거 결함",
            "evidence_refs": ["source:goal"], "affected_task_refs": [], "remediable": True,
        }

        def payload(review_value):
            return {"review": review_value, "inspection": inspection}

        # 정상 rating(다섯 항목과 0/4 경계), 단일·복수 finding+null은 사후 validator가 수용한다.
        for review_value in (
            {"findings": [], "ratings": ratings_at_bounds},
            {"findings": [finding], "ratings": None},
            {"findings": [finding, {**finding, "finding_code": "F2"}], "ratings": None},
        ):
            with self.subTest(valid=review_value):
                PlanReviewEnvelope.model_validate(payload(review_value))

        # 빈 finding+null, 동시 제출, key 누락, 범위 밖 rating, Core 권위 필드는 모두 거부한다.
        invalid = (
            {"findings": [], "ratings": None},
            {"findings": [finding], "ratings": ratings_at_bounds},
            {"findings": []},
            {"ratings": ratings_at_bounds},
            {"findings": [], "ratings": {**ratings_at_bounds, "goal_fit": 5}},
            {"findings": [], "ratings": {key: value for key, value in ratings_at_bounds.items() if key != "goal_fit"}},
            {"findings": [], "ratings": ratings_at_bounds, "status": "admissible"},
            {"findings": [], "ratings": ratings_at_bounds, "score": 100},
        )
        for review_value in invalid:
            with self.subTest(invalid=review_value):
                with self.assertRaises(ValidationError):
                    PlanReviewEnvelope.model_validate(payload(review_value))

    def test_bounded_diagnostic_records_first_schema_failure_without_retry(self):
        runtime = ImmediateRoleRuntime(["not-json", '{"answer":"would-pass"}'])
        runner = CodexStructuredRoleRunner(runtime, max_schema_recovery_attempts=0)
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(inventory=runtime.inventory,role="bounded-diagnostic", instructions="고정 응답 계약을 따른다.",
                                      payload={}, output_schema={"type": "object"}, model="available", effort="low",
                                      inventory_digest=runtime.inventory.inventory_digest, cwd=temp)
            with self.assertRaises(StructuredRoleError) as raised:
                runner.run(request)
        self.assertEqual(1, len(runtime.outputs))
        receipt = raised.exception.receipt
        self.assertEqual("schema_failed", receipt.status)
        self.assertEqual(0, receipt.schema_recovery_attempts)
        self.assertEqual(1, len(receipt.turn_ids))
        self.assertEqual(100, receipt.input_tokens)
        self.assertEqual(10, receipt.output_tokens)

    def test_role_thread_persistence_is_explicit_without_changing_request_or_resuming(self):
        for ephemeral in (True, False):
            with self.subTest(ephemeral=ephemeral), tempfile.TemporaryDirectory() as temp:
                runtime = ImmediateRoleRuntime(['{"answer":"ok"}'])
                progress = []
                runner = CodexStructuredRoleRunner(
                    runtime, ephemeral_threads=ephemeral, progress_sink=progress.append,
                    max_schema_recovery_attempts=0,
                )
                request = make_role_request(
                    inventory=runtime.inventory, role="stored-diagnostic", instructions="JSON 응답",
                    payload={"request":"ok"}, output_schema={"type":"object"},
                    model="available", effort="low", cwd=temp,
                    inventory_digest=runtime.inventory.inventory_digest,
                )
                before = request.model_dump(mode="json")
                with patch.object(runtime, "create_thread", wraps=runtime.create_thread) as create:
                    result = runner.run(request)
                self.assertIs(ephemeral, create.call_args.kwargs["ephemeral"])
                self.assertEqual(before, request.model_dump(mode="json"))
                self.assertEqual("succeeded", result.receipt.status)
                self.assertEqual(1, len(result.receipt.turn_ids))
                created = next(item for item in progress if item["event"] == "thread_created")
                self.assertIs(ephemeral, created["ephemeral"])
        for invalid in (None, 0, 1, "false"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "명시적 bool"):
                CodexStructuredRoleRunner(ImmediateRoleRuntime([]), ephemeral_threads=invalid)

    def test_all_role_schemas_remove_defaults_without_changing_core_schema(self) -> None:
        from flowmarshal.engine.domain import ExecutionSpecProposal
        from flowmarshal.engine.goal import GoalNormalizationProposal, ReviewDraft
        from flowmarshal.engine.planner_roles import (
            PlanExpansionDraft, PlanExpansionEnvelope, PlanReviewEnvelope, SkeletonBatchDraft, SkeletonCandidateDraft,
        )

        def verify(node):
            if isinstance(node, list):
                for child in node:
                    verify(child)
            elif isinstance(node, dict):
                self.assertNotIn("default", node)
                if node.get("type") == "object":
                    self.assertIs(False, node["additionalProperties"])
                    self.assertEqual(list(node.get("properties", {})), node["required"])
                for child in node.values():
                    verify(child)

        for model in (GoalNormalizationProposal, ReviewDraft, PlanExpansionDraft, PlanExpansionEnvelope, PlanReviewEnvelope,
                      SkeletonBatchDraft, SkeletonCandidateDraft, ExecutionSpecProposal):
            with self.subTest(model=model.__name__):
                original = model.model_json_schema()
                normalized = strict_json_output_schema(original)
                verify(normalized)
                self.assertEqual(model.model_json_schema(), original)
        original = PlanExpansionDraft.model_json_schema()
        self.assertIn("default", original["$defs"]["DetailedTaskDraft"]["properties"]["approval_class"])

    def test_strict_schema_preserves_declared_property_order_through_canonical_roundtrip(self) -> None:
        source = {
            "type": "object",
            "properties": {
                "zeta": {"type": "string"},
                "alpha": {
                    "type": "object",
                    "default": {},
                    "properties": {"zulu": {"type": "string"}, "able": {"type": "string"}},
                },
                "union": {
                    "anyOf": [
                        {"type": "object", "properties": {"later": {"type": "string"}, "earlier": {"type": "string"}}},
                        {"type": "array", "items": {"type": "object", "properties": {"right": {"type": "integer"}, "left": {"type": "integer"}}}},
                    ]
                },
            },
            "prefixItems": [
                {"type": "object", "properties": {"last": {"type": "string"}, "first": {"type": "string"}}},
                {"enum": ["second", "first"]},
            ],
            "anyOf": [{"const": "first"}, {"const": "second"}],
            "oneOf": [{"const": 2}, {"const": 1}],
        }
        normalized = strict_json_output_schema(source)
        restored = strict_json_output_schema(json.loads(json.dumps(normalized, sort_keys=True)))

        self.assertEqual(["zeta", "alpha", "union"], list(normalized["properties"]))
        self.assertEqual(["zeta", "alpha", "union"], normalized["required"])
        self.assertEqual(["zulu", "able"], list(normalized["properties"]["alpha"]["properties"]))
        self.assertEqual(["zulu", "able"], normalized["properties"]["alpha"]["required"])
        self.assertNotIn("default", normalized["properties"]["alpha"])
        self.assertEqual(["last", "first"], normalized["prefixItems"][0]["required"])
        self.assertEqual(["later", "earlier"], normalized["properties"]["union"]["anyOf"][0]["required"])
        self.assertEqual(["right", "left"], normalized["properties"]["union"]["anyOf"][1]["items"]["required"])
        self.assertEqual(["second", "first"], normalized["prefixItems"][1]["enum"])
        self.assertEqual([{"const": "first"}, {"const": "second"}], normalized["anyOf"])
        self.assertEqual([{"const": 2}, {"const": 1}], normalized["oneOf"])
        self.assertEqual(normalized, restored)
        self.assertEqual(sha256_digest(normalized), sha256_digest(restored))

        request = make_role_request(
            role="schema_test", instructions="결정적 schema 검사", payload={"input": True}, output_schema=source,
            model="fixture-model", effort="high", inventory_digest="sha256:" + "0" * 64, cwd="D:\\fixture",
        )
        self.assertEqual(normalized, request.output_schema)
        reloaded = RoleCallRequest.model_validate(json.loads(json.dumps(request.model_dump(mode="json"), sort_keys=True)))
        self.assertEqual(strict_json_output_schema(request.output_schema), strict_json_output_schema(reloaded.output_schema))
        self.assertEqual(sha256_digest(strict_json_output_schema(request.output_schema)),
                         sha256_digest(strict_json_output_schema(reloaded.output_schema)))

        reordered = json.loads(json.dumps(source))
        reordered["prefixItems"].reverse()
        self.assertNotEqual(sha256_digest(normalized), sha256_digest(strict_json_output_schema(reordered)))

    def test_validation_argv_preserves_repeated_arguments(self) -> None:
        from flowmarshal.engine.domain import ValidationExecutionStep
        step = ValidationExecutionStep(
            validation_id="repeated-args", method="deterministic",
            argv=("python", "-c", "print(1)", "same", "same"),
            working_directory=".", required_evidence_kinds=("command",),
        )
        self.assertEqual(("same", "same"), step.argv[-2:])

    def test_runner_returns_all_prior_receipts_when_later_call_fails(self) -> None:
        runtime = ImmediateRoleRuntime(['{"answer":"ok"}', "not-json", "still-not-json"])
        progress = []
        runner = CodexStructuredRoleRunner(runtime, progress_sink=progress.append)
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(inventory=runtime.inventory,
                role="test-role",
                instructions="JSON만 반환한다.",
                payload={"request": "ok"},
                output_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                },
                model="available",
                effort="low",
                inventory_digest=runtime.inventory.inventory_digest,
                cwd=temp,
            )
            success = runner.run(request)
            self.assertEqual("succeeded", success.receipt.status)
            self.assertEqual(100, success.receipt.input_tokens)
            with self.assertRaises(StructuredRoleError) as raised:
                runner.run(request.model_copy(update={"payload": {"request": "invalid"}}))
        receipts = raised.exception.receipts
        self.assertEqual(("succeeded", "schema_failed"), tuple(item.status for item in receipts))
        self.assertEqual(1, receipts[-1].schema_recovery_attempts)
        self.assertEqual(2, len(receipts[-1].turn_ids))
        self.assertFalse(receipts[-1].usage_available)
        self.assertIsNone(receipts[-1].input_tokens)
        first_call = [item["event"] for item in progress if item["call_id"] == receipts[0].call_id]
        self.assertEqual(["role_requested", "thread_created", "turn_started", "role_receipt"], first_call)
        second_call = [item["event"] for item in progress if item["call_id"] == receipts[1].call_id]
        self.assertEqual(2, second_call.count("turn_started"))
        self.assertEqual("schema_failed", progress[-1]["receipt"]["status"])


if __name__ == "__main__":
    unittest.main()
