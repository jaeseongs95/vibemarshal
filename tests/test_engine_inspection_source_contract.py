from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowmarshal.canonical import canonical_json, sha256_bytes
from flowmarshal.engine.domain import ProjectMapRevision
from flowmarshal.engine.plan_inspection import PlanInspectionError
from flowmarshal.engine.planner_roles import inspection_source_catalog
from flowmarshal.engine.roles import RoleCallRequest, strict_json_output_schema
from scripts.diagnostics.r_s06_10 import CALL_ORDER, MAXIMUM_CALLS, CapturingRuntime, claim_turn, locked_input_files, verify_instruction_sources, write_new
from tests.test_engine_plan_inspection import inputs


class InspectionSourceContractTests(unittest.TestCase):
    def test_preflight_does_not_lock_its_ongoing_output_log_but_keeps_nested_input_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "prepare.log").write_text("", encoding="utf-8")
            (run / "workspace").mkdir()
            (run / "workspace/evidence.log").write_text("원문 입력", encoding="utf-8")
            write_new(run / "expectations.json", {"clean": []})
            locked = locked_input_files(run)
            (run / "prepare.log").write_text("잠금 완료 후 기록되는 출력", encoding="utf-8")
            self.assertEqual(locked, locked_input_files(run))
            self.assertIn("expectations.json", locked)
            self.assertIn(str(Path("workspace/evidence.log")), locked)
            (run / "workspace/evidence.log").write_text("변조된 입력", encoding="utf-8")
            self.assertNotEqual(locked, locked_input_files(run))

    def test_projection_keeps_registered_bytes_and_address_without_trace_indices(self):
        _, _, _, project_map = inputs("clean")
        catalog = inspection_source_catalog(project_map, {"source:goal": "payload.goal"})
        for entry in project_map.entries:
            item = catalog[f"project:{entry.entry_id}"]
            self.assertEqual(entry.content_digest, item["content_digest"])
            self.assertEqual("/content", item["selector"])
            self.assertEqual(f"project:{entry.entry_id}", item["source_ref"])
            if entry.kind.value in {"reference", "instruction"}:
                self.assertEqual(entry.content_digest, sha256_bytes(item["content"].encode("utf-8")))

    def test_registered_source_digest_and_utf8_fail_before_provider_request(self):
        _, _, _, project_map = inputs("clean")
        raw = project_map.model_dump(mode="json")
        entry = next(item for item in raw["entries"] if item["kind"] == "reference")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "등록.md"
            path.write_bytes(b"different")
            entry["path"] = str(path)
            with self.assertRaisesRegex(PlanInspectionError, "digest"):
                inspection_source_catalog(ProjectMapRevision.model_validate(raw), {})
            path.write_bytes(b"\xff")
            entry["content_digest"] = sha256_bytes(path.read_bytes())
            with self.assertRaisesRegex(PlanInspectionError, "원문을 확인"):
                inspection_source_catalog(ProjectMapRevision.model_validate(raw), {})
            path.unlink()
            with self.assertRaisesRegex(PlanInspectionError, "원문을 확인"):
                inspection_source_catalog(ProjectMapRevision.model_validate(raw), {})

    def test_actual_injected_paths_and_content_must_match_locked_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            paths = [run / name / "AGENTS.md" for name in ("global", "project", "workspace")]
            for index, path in enumerate(paths):
                path.parent.mkdir()
                path.write_text(f"지침 {index}", encoding="utf-8")
            write_new(run / "instruction-binding.json", {"sources": [
                {"path": str(path), "content_digest": sha256_bytes(path.read_bytes())} for path in paths]})
            observed = list(map(str, paths))
            verify_instruction_sources(run, observed)
            for altered in (observed[:-1], list(reversed(observed)), observed + [str(run / "extra.md")]):
                with self.assertRaisesRegex(RuntimeError, "ACTUAL_INSTRUCTION_SOURCES_MISMATCH"):
                    verify_instruction_sources(run, altered)
            paths[0].write_text("변경된 지침", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "INSTRUCTION_CONTENT_CHANGED"):
                verify_instruction_sources(run, observed)

    def test_new_diagnostic_keeps_thirteen_single_attempt_slots(self):
        self.assertEqual(13, MAXIMUM_CALLS)
        self.assertEqual(13, len(set(CALL_ORDER)))
        self.assertEqual("clean", CALL_ORDER[0])
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            for index, name in enumerate(CALL_ORDER):
                claim_turn(run, run / "calls" / f"{index:02d}-{name}", {"case": name})
            with self.assertRaisesRegex(RuntimeError, "MAXIMUM_PROVIDER_CALLS"):
                claim_turn(run, run / "calls/extra", {})

    def test_turn_uses_locked_actual_schema_after_request_json_key_sorting(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            capture = run / "calls/01-review"
            request = RoleCallRequest(role="compact_plan_reviewer", instructions="검사 지침", payload={"입력": "원문"},
                output_schema={"type": "object", "properties": {"z": {"type": "string"}, "a": {"type": "string"}}},
                model="fixture-model", effort="high", inventory_digest="sha256:" + "1" * 64, cwd=str(run))
            schema = strict_json_output_schema(request.output_schema)
            write_new(capture / "request.json", request)
            write_new(capture / "strict-schema.json", schema)
            write_new(capture / "thread.receipt.json", {"binding": {"thread_id": "thread_fixture"},
                                                       "payload": {"instructionSources": []}})
            write_new(run / "instruction-binding.json", {"sources": []})
            runtime = object.__new__(CapturingRuntime)
            runtime.run, runtime.capture = run, capture
            arguments = dict(thread_id="thread_fixture", cwd=run, prompt=canonical_json(request.payload),
                             model=request.model, effort=request.effort, output_schema=schema)
            with patch("scripts.diagnostics.r_s06_10.verify_lock"), patch(
                    "flowmarshal.engine.runtime.CodexAppServerRuntime.start_turn", return_value={"fixture": True}) as provider:
                runtime.start_turn(**arguments)
                self.assertEqual(1, provider.call_count)
                altered = dict(schema, required=["a", "z"])
                with self.assertRaisesRegex(RuntimeError, "ACTUAL_TURN_BINDING_MISMATCH"):
                    runtime.start_turn(**dict(arguments, output_schema=altered))
                self.assertEqual(1, provider.call_count)


if __name__ == "__main__":
    unittest.main()
