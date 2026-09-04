from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.engine.domain import ThreadBinding, new_id, utc_now
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.roles import (
    CodexStructuredRoleRunner,
    RoleCallRequest,
    StructuredRoleError,
    strict_json_output_schema,
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
            payload={"thread_id": thread_id},
            binding=ThreadBinding(thread_id=thread_id, bound_at=utc_now()),
        )

    def start_turn(self, *, thread_id, cwd, prompt, model, effort, output_schema=None):
        del cwd, prompt, model, effort, output_schema
        turn_id = new_id("turn")
        self.latest[thread_id] = (turn_id, self.outputs.pop(0))
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id},
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
                    }
                }
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


class EngineStructuredRoleTests(unittest.TestCase):
    def test_bounded_diagnostic_records_first_schema_failure_without_retry(self):
        runtime = ImmediateRoleRuntime(["not-json", '{"answer":"would-pass"}'])
        runner = CodexStructuredRoleRunner(runtime, max_schema_recovery_attempts=0)
        with tempfile.TemporaryDirectory() as temp:
            request = RoleCallRequest(role="bounded-diagnostic", instructions="고정 응답 계약을 따른다.",
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
            request = RoleCallRequest(
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
        first_call = [item["event"] for item in progress if item["call_id"] == receipts[0].call_id]
        self.assertEqual(["role_requested", "thread_created", "turn_started", "role_receipt"], first_call)
        second_call = [item["event"] for item in progress if item["call_id"] == receipts[1].call_id]
        self.assertEqual(2, second_call.count("turn_started"))
        self.assertEqual("schema_failed", progress[-1]["receipt"]["status"])


if __name__ == "__main__":
    unittest.main()
