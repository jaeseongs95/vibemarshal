"""사례별 기대표의 실제 입력 결속과 보정하지 않은 v6 거부 회귀."""
from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from flowmarshal.canonical import canonical_json, sha256_bytes, sha256_digest
from flowmarshal.engine.domain import GoalContractRevision, PlanContractRevision, ProjectMapRevision
from flowmarshal.engine.plan_inspection import PlanInspectionError, validate_plan_inspection
from flowmarshal.engine.plan_inspection_eval import (
    InspectionExpectationError, assess_case_inspection_review, assess_fixed_ac_link_requirements,
    bind_case_expectation, inspection_input_binding, verify_case_expectation,
)
from flowmarshal.engine.planner_roles import PlanReviewEnvelope
from flowmarshal.engine.planning import plan_validation_scope_rows, validation_comparison_targets
from flowmarshal.engine.roles import RoleCallRequest, strict_json_output_schema
from scripts.diagnostics.r_s06_10 import (
    RecordedRunner, case_expectation, completed_call_verification, verify_generation_pending, write_new,
)
from scripts.diagnostics.r_s06_10_fixtures import FixtureRevisionError, build_revision, verify_reviewed_case
from tests.test_engine_inspection_fixture_revision import _write_portable_source
from tests.test_engine_inspection_raw_regressions import _current_contract


FIXTURES = Path(__file__).parent / "fixtures/engine"
RAW = json.loads((FIXTURES / "plan-inspection-raw-v6-rejected.json").read_text(encoding="utf-8"))


class InspectionCaseBindingTests(unittest.TestCase):
    def setUp(self):
        self.payload = deepcopy(RAW["request"]["payload"])
        plan = self.payload["evidence_catalog"]["artifact:plan_contract"]
        for validation in plan["definition"]["integration_validations"]:
            validation["criterion_refs"] = [
                coverage["criterion_id"] for coverage in plan["definition"]["goal_coverage"]
                if validation["validation_id"] in coverage["validation_ids"]
            ]
        plan["definition_digest"] = sha256_digest(plan["definition"])
        self.rows = [
            {key: value for key, value in row.items() if key != "relation"}
            | {"ac_link_required": row["relation"] == "explicit_procedure"}
            for row in deepcopy(RAW["original_relation_rows"])
        ]
        self.expected = bind_case_expectation(
            case_id="clean", payload=self.payload, rows=self.rows, defects=[], review_digest="sha256:" + "1" * 64,
        )

    def test_same_ids_do_not_authorize_changed_meaning_or_registered_material(self):
        def validation(payload, scope="tasks"):
            plan = payload["evidence_catalog"]["artifact:plan_contract"]["definition"]
            return plan["tasks"][0]["validations"][0] if scope == "tasks" else plan["integration_validations"][0]

        def changed_content(payload):
            source = next(value for value in payload["inspection_source_catalog"].values()
                          if isinstance(value, dict) and "Task 검사" in value.get("content", ""))
            source["content"] += "\n자료가 변경되었다.\n"
            source["content_digest"] = sha256_bytes(source["content"].encode("utf-8"))

        mutations = {
            "statement": lambda p: validation(p).update(statement="새 검사 계약"),
            "phase": lambda p: validation(p, "integration").update(
                statement=validation(p, "integration")["statement"].replace("goal phase", "task phase")),
            "method": lambda p: validation(p).update(method="semantic"),
            "mode": lambda p: validation(p, "integration").update(evidence_mode="task_aggregate"),
            "owner": lambda p: p["evidence_catalog"]["artifact:plan_contract"]["definition"]["tasks"][0].update(task_ref="다른_Task"),
            "evidence": lambda p: validation(p).update(required_evidence_kinds=["file"]),
            "goal": lambda p: p["evidence_catalog"]["source:goal"]["hard_acceptance"][0].update(validation_intent="새 절차"),
            "registered_content": changed_content,
            "incomplete_reference": lambda p: next(v for v in p["inspection_source_catalog"].values()
                                                    if isinstance(v, dict) and "content" in v).pop("content"),
            "projection": lambda p: p["validation_scope_rows"][0].update(method="semantic"),
        }
        for name, mutate in mutations.items():
            with self.subTest(change=name):
                changed = deepcopy(self.payload)
                mutate(changed)
                with self.assertRaisesRegex(InspectionExpectationError, "CASE_SEMANTIC_INPUT_MISMATCH"):
                    verify_case_expectation(self.expected, case_id="clean", payload=changed)
        verify_case_expectation(self.expected, case_id="clean", payload=self.payload)

    def test_actual_bad_wrong_goal_combined_cannot_reuse_clean_expectation(self):
        files = json.loads((FIXTURES / "plan-inspection-regressions.json").read_text(encoding="utf-8"))["files"]
        for name in ("bad", "wrong-goal", "combined"):
            with self.subTest(case=name):
                changed = deepcopy(self.payload)
                changed["evidence_catalog"]["artifact:plan_contract"] = files[f"input-{name}-plan.json"]
                with self.assertRaisesRegex(InspectionExpectationError, "CASE_SEMANTIC_INPUT_MISMATCH"):
                    verify_case_expectation(self.expected, case_id="clean", payload=changed)
                with self.assertRaisesRegex(InspectionExpectationError, "CASE_SEMANTIC_INPUT_MISMATCH"):
                    verify_case_expectation(self.expected, case_id=name, payload=self.payload)

    def test_missing_duplicate_table_or_changed_expectations_are_rejected(self):
        for rows in ([], self.rows[:-1], self.rows + [self.rows[0]]):
            with self.assertRaisesRegex(InspectionExpectationError, "MISSING_OR_INCOMPLETE"):
                bind_case_expectation(case_id="clean", payload=self.payload, rows=rows, defects=[],
                                      review_digest="sha256:" + "1" * 64)
        changed = deepcopy(self.expected)
        changed["ac_validation_rows"][0]["ac_link_required"] = False
        with self.assertRaisesRegex(InspectionExpectationError, "CASE_EXPECTATION_DIGEST_MISMATCH"):
            verify_case_expectation(changed, case_id="clean", payload=self.payload)
        changed = deepcopy(self.payload)
        source = next(value for value in changed["inspection_source_catalog"].values()
                      if isinstance(value, dict) and "content" in value)
        source["content"] += "변조"
        with self.assertRaisesRegex(InspectionExpectationError, "CONTENT_DIGEST_MISMATCH"):
            inspection_input_binding(changed)

    def test_missing_case_table_stops_before_provider_effect(self):
        request = RoleCallRequest.model_validate(RAW["request"])
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            write_new(run / "requests/clean.json", request)
            with self.assertRaises(FileNotFoundError):
                case_expectation(run, "clean", request)
            runtime = Mock()
            runner = RecordedRunner(runtime, run, {})
            runner.name = "clean"
            with patch("scripts.diagnostics.r_s06_10.verify_lock"):
                with self.assertRaises(FileNotFoundError):
                    runner.run(request)
            runtime.create_thread.assert_not_called()
            runtime.start_turn.assert_not_called()

    def test_evaluator_reports_fixed_scope_without_modifying_submission(self):
        raw_response = _current_contract(RAW["raw_response"])
        envelope = PlanReviewEnvelope.model_validate(raw_response)
        before = envelope.model_dump(mode="json")
        report = assess_case_inspection_review(envelope, self.expected, case_id="clean", payload=self.payload)
        self.assertFalse(report["passed"])
        self.assertEqual(2, len(report["fixed_ac_link_requirement_assessment"]["requirement_differences"]))
        self.assertTrue(all("actual_link_exists" in row
                            for row in report["fixed_ac_link_requirement_assessment"]["link_presence"]))
        self.assertEqual("not_scored", report["evaluation_scope"]["constraint_semantics"])
        self.assertEqual(before, envelope.model_dump(mode="json"))

    def test_generation_failure_or_incomplete_calls_cannot_enable_thirteenth_review(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            pending = {"status": "GENERATION_REVIEW_REQUIRED", "checks": {"source": True},
                       "logical_calls": 12, "provider_turns": 12}
            path = run / "generation-pending.json"
            write_new(path, pending)
            with self.assertRaisesRegex(RuntimeError, "FAILED_OR_INCOMPLETE"):
                verify_generation_pending(run)
            for number in range(12):
                write_new(run / "calls" / str(number) / "result.json", {})
                write_new(run / "calls" / str(number) / "turn.intent.json", {})
            verify_generation_pending(run)
            for altered in (dict(pending, status="FAIL"), dict(pending, checks={"preserved_originals": False}),
                            dict(pending, provider_turns=11)):
                path.write_text(json.dumps(altered), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "FAILED_OR_INCOMPLETE"):
                    verify_generation_pending(run)

    def test_wrong_receipt_thread_stops_at_completed_call_before_next_case(self):
        request = RoleCallRequest.model_validate(RAW["request"])
        schema = strict_json_output_schema(request.output_schema)
        prompt = canonical_json(request.payload)
        receipt = {"input_digest": request.request_digest, "thread_id": "thread_actual", "turn_ids": ["turn_actual"],
                   "output_schema_digest": sha256_digest(schema), "schema_recovery_attempts": 0,
                   "model": request.model, "effort": request.effort, "role": request.role,
                   "inventory_digest": request.inventory_digest, "permission_profile": ":danger-full-access",
                   "approval_policy": "never"}
        with tempfile.TemporaryDirectory() as temp:
            capture = Path(temp)
            values = {
                "request.json": request, "strict-schema.json": schema,
                "terminal.json": {"payload": {"thread_id": "thread_actual", "turn_id": "turn_actual", "prompt_digest": sha256_digest(prompt)}},
                "thread.receipt.json": {"payload": {"thread": {"id": "thread_actual"}}},
                "thread.intent.json": {"developer_instructions": request.instructions},
                "turn.intent.json": {"prompt": prompt, "thread_id": "thread_actual", "model": request.model,
                                     "effort": request.effort, "output_schema": schema},
                "turn.receipt.json": {"operation_id": "turn_actual"},
            }
            for name, value in values.items():
                write_new(capture / name, value)
            self.assertTrue(completed_call_verification(capture, receipt)["passed"])
            for altered in (dict(receipt, thread_id="thread_other"), dict(receipt, model="other_model"),
                            dict(receipt, approval_policy="on-request")):
                self.assertFalse(completed_call_verification(capture, altered)["passed"])

    def test_v6_raw_keeps_valid_ratings_original_fail_and_rejects_missing_ac_basis(self):
        self.assertEqual(RAW["raw_response"], json.loads(RAW["raw_final_response"]))
        self.assertEqual(RAW["provenance"]["final_response_bytes_digest"],
                         sha256_bytes(RAW["raw_final_response"].encode("utf-8")))
        self.assertEqual("FAIL", RAW["provenance"]["original_status"])
        self.assertFalse(RAW["original_assessment"]["passed"])
        self.assertIsNotNone(RAW["raw_response"]["review"]["ratings"])
        self.assertFalse(RAW["raw_response"]["review"]["findings"])
        self.assertFalse(any(c["selector"].endswith("/validation_intent")
                             for c in RAW["raw_response"]["inspection"]["citations"]))
        catalog = deepcopy(self.payload["evidence_catalog"])
        # 검증한 실제 전송 본문을 사용해 로컬 과거 경로에 의존하지 않는다. 원시는 수정하지 않는다.
        catalog.update({ref: value for ref, value in self.payload["inspection_source_catalog"].items()
                        if ref.startswith("project:") and "content" in value})
        plan = PlanContractRevision.model_validate(catalog["artifact:plan_contract"])
        goal_data = deepcopy(json.loads((FIXTURES / "plan-inspection-regressions.json").read_text(encoding="utf-8"))["files"]["input-goal.json"])
        goal = GoalContractRevision.model_validate(goal_data)
        project_map = ProjectMapRevision.model_validate(RAW["request"]["payload"]["project_map"]) if "project_map" in RAW["request"]["payload"] else None
        if project_map is None:
            files = json.loads((FIXTURES / "plan-inspection-regressions.json").read_text(encoding="utf-8"))["files"]
            project_map = ProjectMapRevision.model_validate(files["input-project-map.json"])
        with self.assertRaisesRegex(ValueError, "ac_link_required"):
            PlanReviewEnvelope.model_validate(RAW["raw_response"])

    def test_distinct_mechanisms_may_reuse_one_citation_but_one_list_cannot_duplicate_it(self):
        from tests.test_engine_inspection_raw_regressions import (
            _clean_non_target_conflicts, _payload, _validate, FILES,
        )
        payload = _clean_non_target_conflicts(_payload(), GoalContractRevision.model_validate(FILES["input-goal.json"]))
        validation = payload["inspection"]["validation_rows"][0]
        additional = deepcopy(validation["mechanisms"][0])
        additional["tool"] = "같은 등록 원문에 근거한 별도 수단"
        validation["mechanisms"].append(additional)
        _validate(payload)
        additional["basis_refs"].append(additional["basis_refs"][0])
        with self.assertRaisesRegex(PlanInspectionError, "인용 참조 중복"):
            _validate(payload)


class IndependentlyReviewedCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        source = Path(cls.temp.name) / "source"
        source.mkdir()
        _write_portable_source(source)
        cls.fixture_root = Path(cls.temp.name) / "revision"
        build_revision(source, cls.fixture_root)
        cls.expected = json.loads((cls.fixture_root / "expectations.json").read_text(encoding="utf-8"))
        cls.review = json.loads((cls.fixture_root / "independent-fixture-review.json").read_text(encoding="utf-8"))

    def payload_for(self, name):
        case = self.review["case_reviews"][name]
        plan = PlanContractRevision.model_validate(json.loads((self.fixture_root / case["input"]).read_text(encoding="utf-8")))
        goal = GoalContractRevision.model_validate(json.loads((self.fixture_root / case["goal_input"]).read_text(encoding="utf-8")))
        payload = deepcopy(RAW["request"]["payload"])
        payload["evidence_catalog"]["artifact:plan_contract"] = plan.model_dump(mode="json")
        payload["evidence_catalog"]["source:goal"] = goal.definition.model_dump(mode="json")
        payload["validation_scope_rows"] = plan_validation_scope_rows(plan)
        payload["validation_comparison_targets"] = validation_comparison_targets(goal, plan)
        return payload

    def test_every_fixed_and_deterministic_case_matches_independent_source_review(self):
        self.assertEqual(18, len(self.review["case_reviews"]))
        for name in self.review["case_reviews"]:
            with self.subTest(case=name):
                payload = self.payload_for(name)
                verify_reviewed_case(name, payload, self.expected, self.review)
                expectation = bind_case_expectation(
                    case_id=name, payload=payload, rows=self.expected["case_ac_validation_rows"][name],
                    defects=[] if name == "evidence-simple-mention" else self.expected[name],
                    review_digest=sha256_bytes((self.fixture_root / "independent-fixture-review.json").read_bytes()),
                )
                verify_case_expectation(expectation, case_id=name, payload=payload)

    def test_reviewed_contract_cannot_be_rebound_to_a_changed_same_id_statement(self):
        payload = self.payload_for("clean")
        payload["validation_scope_rows"][0]["statement"] += " 새로운 책임"
        with self.assertRaisesRegex(FixtureRevisionError, "CONTRACT_MISMATCH"):
            verify_reviewed_case("clean", payload, self.expected, self.review)
        payload = self.payload_for("clean")
        payload["evidence_catalog"]["source:goal"]["hard_acceptance"][2]["validation_intent"] = "새 프로세스 검사 제거"
        with self.assertRaises(FixtureRevisionError):
            verify_reviewed_case("clean", payload, self.expected, self.review)
        payload = self.payload_for("clean")
        payload["evidence_catalog"]["source:goal"]["hard_acceptance"][0]["validation_intent"] += " 분리 Validator의 검토도 명시한다."
        with self.assertRaisesRegex(FixtureRevisionError, "GOAL_CONTRACT_MISMATCH"):
            verify_reviewed_case("clean", payload, self.expected, self.review)

    def test_revised_sources_keep_goal_task_boundary_and_optional_duplicate_ids(self):
        rows = {(r["criterion_id"], r["validation_id"]): r for r in self.expected["case_ac_validation_rows"]["clean"]}
        self.assertTrue(rows[("ac_003", "val_goal_independent_behavior_contract")]["ac_link_required"])
        for criterion, validation in (("ac_001", "val_task_scope_preservation"), ("ac_002", "val_task_scope_preservation"),
                                     ("ac_003", "val_task_scope_preservation"), ("ac_004", "val_task_scope_preservation"),
                                     ("ac_002", "val_task_unittest"), ("ac_004", "val_task_unittest")):
            row = rows[(criterion, validation)]
            self.assertFalse(row["ac_link_required"])
        self.assertEqual(6, sum(row["changed_expectation"] for row in self.review["focused_row_decisions"]))
        baseline = json.loads((FIXTURES / "plan-inspection-v2-expectations.json").read_text(encoding="utf-8"))
        self.assertEqual(self.expected["parent_expectations_byte_digest"],
                         sha256_bytes((FIXTURES / "plan-inspection-v4-expectations.json").read_bytes()))
        self.assertEqual("optional_or_unrelated", next(row for row in baseline["ac_validation_rows"]
                         if (row["criterion_id"], row["validation_id"]) == ("ac_004", "val_task_scope_preservation"))["relation"])

    def test_link_omission_and_missing_responsibility_are_separate_fixed_defects(self):
        missing_link = self.expected["missing-ac003-goal-behavior-link"][0]
        missing_responsibility = self.expected["partial-task-inspection-missing"][0]
        self.assertEqual("missing_validation_link", missing_link["defect_kind"])
        self.assertEqual("missing_task_validation", missing_responsibility["defect_kind"])
        linked_payload = self.payload_for("missing-ac003-goal-behavior-link")
        self.assertIn(missing_link["validation_ids"][0],
                      [row["validation_id"] for row in linked_payload["validation_scope_rows"]])
        partial_payload = self.payload_for("partial-task-inspection-missing")
        self.assertNotIn("val_task_validator_review", [row["validation_id"] for row in partial_payload["validation_scope_rows"]])
        # 중복 파일 검사를 제거해도 전역 파일 검사 책임을 수행하는 task oracle은 남는다.
        payload = self.payload_for("clean")
        plan = payload["evidence_catalog"]["artifact:plan_contract"]
        task = plan["definition"]["tasks"][0]
        task["validations"] = [v for v in task["validations"] if v["validation_id"] != "val_task_scope_preservation"]
        for row in plan["definition"]["goal_coverage"]:
            row["validation_ids"] = [v for v in row["validation_ids"] if v != "val_task_scope_preservation"]
        plan["definition_digest"] = sha256_digest(plan["definition"])
        revised = PlanContractRevision.model_validate(plan)
        self.assertIn("파일 집합", revised.definition.tasks[0].validations[0].statement)
        self.assertIn("val_task_validator_review", [v.validation_id for v in revised.definition.tasks[0].validations])
        self.assertNotEqual(inspection_input_binding(self.payload_for("clean")), inspection_input_binding(payload))

    def test_registered_oracle_really_runs_fresh_unittest_in_both_phases(self):
        path = FIXTURES / "bugfix-trace/oracle.py"
        spec = importlib.util.spec_from_file_location("inspection_oracle_regression", path)
        oracle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oracle)
        # 원래 결함 fixture는 바꾸지 않는다. 성공 여부가 아니라 실제 절차·phase 경계를 관측한다.
        workspace = Path(__file__).resolve().parents[1] / oracle.load_contract()["project_fixture"]
        for phase in ("task", "goal"):
            observed = oracle.observe(workspace, phase)
            checks = {row["name"]: row for row in observed["checks"]}
            self.assertIn("fresh_unittest", checks)
            self.assertIn("-m", checks["fresh_unittest"]["argv"])
            self.assertIn("unittest", checks["fresh_unittest"]["argv"])
            self.assertEqual(phase == "goal", any(name.startswith("behavior:") for name in checks))
            self.assertTrue(checks["inspection_did_not_mutate_sources"]["passed"])
        with self.assertRaisesRegex(ValueError, "지원하지 않는"):
            oracle.observe(workspace, "불완전한_phase")


if __name__ == "__main__":
    unittest.main()
