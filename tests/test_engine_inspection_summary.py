"""R-S06 저장 artifact의 실패 안전 요약과 효과·사용량 귀속 회귀."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.model_lock import (
    ROLE_CAPABILITIES, ModelCapability, ModelInventory, RUNTIME_CAPABILITIES, bind_models, role_lock,
)
from flowmarshal.engine.roles import StructuredRoleError, RoleCallReceipt, RoleCallRequest, strict_json_output_schema
from scripts.diagnostics.r_s06_10 import completed_call_verification, execute, summarize, write_new
from tests.test_engine_inspection_case_binding import RAW


class InspectionSummaryTests(unittest.TestCase):
    def setUp(self):
        request = RoleCallRequest.model_validate(RAW["request"])
        inventory = ModelInventory(
            source="synthetic:model/list", executable_digest="sha256:" + "0" * 64,
            runtime_capabilities=RUNTIME_CAPABILITIES,
            models=(ModelCapability(model=request.model, supported_efforts=(request.effort,)),),
        )
        binding = bind_models(
            inventory, (role_lock(request.role, request.model, request.effort),),
            required_capabilities=ROLE_CAPABILITIES,
        )
        self.request = RoleCallRequest.model_validate(request.model_dump(mode="python") | {
            "inventory_digest": inventory.inventory_digest,
            "operational_binding": binding,
        })

    def _run(self, root: Path) -> None:
        write_new(root / "preflight.json", {
            "lock_digest": "sha256:" + "1" * 64,
            "source_manifest_digest": "sha256:" + "2" * 64,
            "original_files": {}, "maximum_logical_calls": 13, "maximum_provider_turns": 13,
        })
        write_new(root / "instruction-binding.json", {"sources": []})
        (root / "workspace").mkdir()

    def _receipt(self, *, call_id: str, thread_id: str, turn_id: str,
                 status: str = "succeeded", output_digest: str | None = None) -> dict:
        if output_digest is None and status == "succeeded":
            output_digest = sha256_digest({})
        return RoleCallReceipt(
            call_id=call_id, role=self.request.role, status=status,
            model=self.request.model, effort=self.request.effort,
            inventory_digest=self.request.inventory_digest,
            permission_profile=":danger-full-access", approval_policy="never",
            thread_id=thread_id, turn_ids=(turn_id,), input_digest=self.request.request_digest,
            output_digest=output_digest,
            output_schema_digest=sha256_digest(strict_json_output_schema(self.request.output_schema)),
            input_tokens=100, cached_input_tokens=20, output_tokens=30, reasoning_tokens=10,
            usage_available=True, latency_ms=200, schema_recovery_attempts=0,
            error_summary=None if status == "succeeded" else "대조표 계약 오류",
            recorded_at="2026-09-05T00:00:00Z", observed_binding=self.request.operational_binding,
        ).model_dump(mode="json")

    def _call(self, run: Path, number: int, *, status: str = "success",
              failed_receipts: list[dict] | None = None) -> tuple[Path, dict]:
        capture = run / "calls" / f"{number:02d}-{self.request.role}"
        thread_id, turn_id = f"thread-{number}", f"turn-{number}"
        receipt_status = "succeeded" if status == "success" else "schema_failed"
        receipt = self._receipt(call_id=f"call-{number}", thread_id=thread_id,
                                turn_id=turn_id, status=receipt_status)
        schema = strict_json_output_schema(self.request.output_schema)
        prompt = canonical_json(self.request.payload)
        for name, value in {
            "request.json": self.request,
            "strict-schema.json": schema,
            "thread.intent.json": {"developer_instructions": self.request.instructions},
            "thread.receipt.json": {"payload": {"thread": {"id": thread_id, "turns": []}}},
            "turn.intent.json": {"prompt": prompt, "thread_id": thread_id, "model": self.request.model,
                                 "effort": self.request.effort, "output_schema": schema},
            "turn.receipt.json": {"operation_id": turn_id},
            "terminal.json": {"active": False, "terminal_status": "completed", "final_response": "{}",
                              "payload": {"thread_id": thread_id, "turn_id": turn_id,
                                          "prompt_digest": sha256_digest(prompt), "duration_ms": 300,
                                          "usage_source": "thread/tokenUsage/updated", "usage_scope": "thread",
                                          "usage": {"total": {"inputTokens": 100, "cachedInputTokens": 20,
                                                                "outputTokens": 30, "reasoningOutputTokens": 10,
                                                                "totalTokens": 130}}}},
        }.items():
            write_new(capture / name, value)
        if status == "success":
            write_new(capture / "result.json", {"payload": {}, "receipt": receipt})
            write_new(capture / "binding-verification.json", completed_call_verification(capture, receipt))
        elif status == "failure":
            write_new(capture / "failed.json", {
                "error_type": "StructuredRoleError",
                "error": "structured output이 유효하지 않습니다. schema recovery 0회",
                "receipts": failed_receipts if failed_receipts is not None else [receipt],
                "observed_at": "2026-09-05T00:00:00Z",
            })
        return capture, receipt

    def _summarize(self, run: Path, status="FAIL", error="StructuredRoleError: 원래 역할 실패"):
        with patch("scripts.diagnostics.r_s06_10.source_manifest_digest",
                   return_value="sha256:" + "2" * 64), patch(
            "scripts.diagnostics.r_s06_10.preserved_files", return_value={}
        ), patch("scripts.diagnostics.r_s06_10.files", return_value={}):
            return summarize(run, status, error)

    def test_failed_receipt_completed_terminal_without_result_preserves_failure_and_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            _capture, receipt = self._call(run, 1, status="failure")
            summary = self._summarize(run)

            self.assertEqual("FAIL", summary["status"])
            self.assertEqual("StructuredRoleError: 원래 역할 실패", summary["error"])
            self.assertEqual({"success": 0, "failure": 1, "provider_terminal_failed": 0, "external_unknown": 0, "incomplete": 0},
                             summary["outcomes"])
            self.assertEqual("StructuredRoleError", summary["call_artifacts"][0]["role_failure"]["error_type"])
            self.assertEqual("대조표 계약 오류",
                             summary["call_artifacts"][0]["role_failure"]["receipt_error_summary"])
            self.assertEqual({"status": "NOT_APPLICABLE", "passed": None,
                              "reason": "역할 실패에는 성공 전용 output payload 결속을 적용하지 않습니다."},
                             summary["call_artifacts"][0]["output_binding"])
            self.assertEqual((1, 1, 0), (summary["logical_calls"], summary["provider_turns"],
                                         summary["schema_recovery_attempts"]))
            self.assertEqual(130, summary["usage"]["total_tokens"])
            self.assertEqual(10, summary["usage"]["reasoning_tokens"])
            self.assertEqual(30, summary["usage"]["output_tokens"])
            self.assertTrue(summary["usage"]["reasoning_included_in_output"])
            self.assertEqual(receipt["call_id"], summary["receipts"][0]["call_id"])

    def test_legacy_receipt_without_v2_model_provenance_remains_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            _capture, receipt = self._call(run, 1, status="success")
            self.assertNotIn("binding_provenance_version", receipt)

            summary = self._summarize(run, status="PASS", error=None)

            self.assertEqual("PASS", summary["status"])
            self.assertEqual(1, summary["outcomes"]["success"])
            self.assertNotIn(
                "PARTIAL_RECEIPT",
                [item["code"] for item in summary["diagnostic_errors"]],
            )

    def test_missing_terminal_is_external_unknown_and_keeps_consumed_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            capture, _receipt = self._call(run, 1, status="failure")
            (capture / "terminal.json").unlink()
            (capture / "failed.json").unlink()
            summary = self._summarize(run)

            self.assertEqual(1, summary["outcomes"]["external_unknown"])
            self.assertEqual(1, summary["effect_counts"]["turn_intents"])
            self.assertEqual(1, summary["effect_counts"]["turn_start_receipts"])
            self.assertEqual(0, summary["effect_counts"]["terminal_observations"])
            self.assertEqual(12, summary["budget"]["remaining_provider_turns"])
            self.assertIsNone(summary["schema_recovery_attempts"])
            self.assertIsNone(summary["usage"]["total_tokens"])
            self.assertIsNone(summary["usage"]["reasoning_included_in_output"])

    def test_malformed_terminal_and_partial_receipt_are_separate_artifact_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            capture, _receipt = self._call(run, 1, status="failure")
            (capture / "terminal.json").write_bytes(b'{"truncated"')
            summary = self._summarize(run)
            self.assertEqual("incomplete", summary["call_artifacts"][0]["outcome"])
            self.assertIn("MALFORMED_ARTIFACT", [item["code"] for item in summary["diagnostic_errors"]])
            self.assertNotEqual("external_unknown", summary["call_artifacts"][0]["outcome"])

        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            capture, _receipt = self._call(run, 1, status="failure")
            failed = json.loads((capture / "failed.json").read_text(encoding="utf-8"))
            failed["receipts"] = [{"call_id": "partial"}]
            (capture / "failed.json").write_text(json.dumps(failed), encoding="utf-8")
            summary = self._summarize(run)
            self.assertEqual("incomplete", summary["call_artifacts"][0]["outcome"])
            self.assertIn("PARTIAL_RECEIPT", [item["code"] for item in summary["diagnostic_errors"]])
            self.assertIsNone(summary["schema_recovery_attempts"])

    def test_accumulated_receipts_dedupe_by_content_and_conflicts_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            _first, receipt_one = self._call(run, 1, status="success")
            capture_two, receipt_two = self._call(run, 2, status="failure")
            historical = self._receipt(call_id="historical-call", thread_id="historical-thread",
                                       turn_id="historical-turn", status="schema_failed")
            failed = json.loads((capture_two / "failed.json").read_text(encoding="utf-8"))
            failed["receipts"] = [historical, receipt_one, receipt_two]
            (capture_two / "failed.json").write_text(json.dumps(failed), encoding="utf-8")
            summary = self._summarize(run)
            self.assertEqual(2, len(summary["receipts"]))
            self.assertIn("UNATTRIBUTED_RECEIPT", [item["code"] for item in summary["diagnostic_errors"]])
            self.assertEqual(260, summary["usage"]["total_tokens"])
            self.assertEqual({"success": 1, "failure": 1, "provider_terminal_failed": 0, "external_unknown": 0, "incomplete": 0},
                             summary["outcomes"])

        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            _first, receipt_one = self._call(run, 1, status="success")
            capture_two, receipt_two = self._call(run, 2, status="failure")
            conflicting = deepcopy(receipt_one)
            conflicting["latency_ms"] += 1
            failed = json.loads((capture_two / "failed.json").read_text(encoding="utf-8"))
            failed["receipts"] = [conflicting, receipt_two]
            (capture_two / "failed.json").write_text(json.dumps(failed), encoding="utf-8")
            summary = self._summarize(run)
            self.assertIn("DUPLICATE_CALL_ID_CONFLICT", [item["code"] for item in summary["diagnostic_errors"]])
            self.assertEqual(2, len(summary["receipts"]))
            self.assertEqual(200, next(item for item in summary["receipts"]
                                       if item["call_id"] == receipt_one["call_id"])["latency_ms"])

    def test_success_binding_and_summary_reexecution_are_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            self._call(run, 1, status="success")
            first = self._summarize(run, status="PASS", error=None)
            before = {str(path.relative_to(run)): path.read_bytes() for path in run.rglob("*") if path.is_file()}
            second = self._summarize(run, status="PASS", error=None)
            after = {str(path.relative_to(run)): path.read_bytes() for path in run.rglob("*") if path.is_file()}
            self.assertEqual("PASS", first["status"])
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertEqual(1, first["effect_counts"]["turn_intents"])
            self.assertEqual(1, first["effect_counts"]["turn_start_receipts"])
            self.assertEqual(1, first["effect_counts"]["terminal_observations"])

    def test_execute_records_expected_role_failure_without_nonzero_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            self._run(run)
            write_new(run / "roles.json", {})

            def fail_after_completed_provider(*_args, **_kwargs):
                self._call(run, 1, status="failure")
                raise StructuredRoleError("structured output이 유효하지 않습니다. schema recovery 0회")

            runtime_context = MagicMock()
            runtime_context.__enter__.return_value = MagicMock()
            with patch("scripts.diagnostics.r_s06_10.STATIC_CASES", ()), patch(
                "scripts.diagnostics.r_s06_10.CALL_ORDER", ("failed-case", "expanded-review")
            ), patch("scripts.diagnostics.r_s06_10.verify_lock", return_value={
                "lock_digest": "sha256:" + "1" * 64, "codex_bin": "unused",
                "source_manifest_digest": "sha256:" + "2" * 64, "original_files": {},
                "maximum_logical_calls": 13, "maximum_provider_turns": 13,
                "operational_binding": self.request.operational_binding.model_dump(mode="json"),
            }), patch("scripts.diagnostics.r_s06_10.EngineRoleConfiguration.model_validate"), patch(
                "scripts.diagnostics.r_s06_10.CapturingRuntime", return_value=runtime_context
            ), patch("scripts.diagnostics.r_s06_10.invoke", side_effect=fail_after_completed_provider), patch(
                "scripts.diagnostics.r_s06_10.source_manifest_digest", return_value="sha256:" + "2" * 64
            ), patch("scripts.diagnostics.r_s06_10.preserved_files", return_value={}), patch(
                "scripts.diagnostics.r_s06_10.files", return_value={}
            ):
                self.assertIsNone(execute(run))

            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("FAIL", summary["status"])
            self.assertIn("StructuredRoleError", summary["error"])
            self.assertEqual(1, summary["outcomes"]["failure"])
            runtime_context.__enter__.return_value.create_thread.assert_not_called()
            runtime_context.__enter__.return_value.start_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
