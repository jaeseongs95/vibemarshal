"""R-S06-26 clean 응답의 구조 성공과 서로 다른 의미 실패를 보존하는 회귀."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from flowmarshal.canonical import canonical_json, sha256_bytes, sha256_digest
from flowmarshal.engine.domain import (
    GoalContractRevision,
    PlanContractRevision,
    ProjectMapRevision,
    StateSnapshot,
)
from flowmarshal.engine.plan_inspection import validate_plan_inspection
from flowmarshal.engine.models import EngineRoleConfiguration
from flowmarshal.engine.plan_inspection_eval import assess_case_inspection_review
from flowmarshal.engine.planner_roles import PlanReviewEnvelope
from flowmarshal.engine.planning import plan_review_evidence_catalog


FIXTURES = Path(__file__).parent / "fixtures/engine"
FIXTURE_PATH = FIXTURES / "r-s06-26-clean-semantic-failure.json"


class RS0626SemanticFailureRegressionTests(unittest.TestCase):
    def test_original_payload_plan_expectation_and_output_reproduce_two_axis_errors(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        provenance = fixture["provenance"]
        payload = fixture["original_request_payload"]

        self.assertEqual("model_call_c87d2346518444719b668dae66556490", provenance["call_id"])
        self.assertEqual("01a07044-5163-73b2-8a37-386c25d83980", provenance["thread_id"])
        self.assertEqual("01a07044-57a6-7433-bbd6-159488c9d0b3", provenance["turn_id"])
        self.assertEqual(("gpt-5.6-terra", "high"), (provenance["model"], provenance["effort"]))
        self.assertTrue(provenance["structural_validation_passed"])
        self.assertFalse(provenance["original_assessment_passed"])
        self.assertEqual(provenance["request_payload_digest"], sha256_digest(payload))
        self.assertEqual(fixture["plan"], payload["evidence_catalog"]["artifact:plan_contract"])
        self.assertEqual(
            provenance["plan_activation_digest"],
            PlanContractRevision.model_validate(fixture["plan"]).activation_digest,
        )
        self.assertEqual(provenance["expectation_digest"], fixture["case_expectation"]["expectation_digest"])
        self.assertEqual(
            provenance["role_configuration_digest"],
            EngineRoleConfiguration.model_validate(fixture["source_role_configuration"]).configuration_digest,
        )
        self.assertEqual(provenance["output_digest"], sha256_digest(fixture["raw_response"]))
        self.assertEqual(fixture["raw_response"], json.loads(fixture["raw_final_response"]))
        self.assertEqual(
            provenance["final_response_bytes_digest"],
            sha256_bytes(fixture["raw_final_response"].encode("utf-8")),
        )

        envelope = PlanReviewEnvelope.model_validate(fixture["raw_response"])
        plan = PlanContractRevision.model_validate(fixture["plan"])
        goal = GoalContractRevision.model_validate(fixture["goal"])
        state = StateSnapshot.model_validate(payload["evidence_catalog"]["source:state"])
        original_project_map = ProjectMapRevision.model_validate(fixture["project_map"])
        self.assertEqual(
            canonical_json(payload["evidence_catalog"]["source:project_map"]),
            canonical_json(plan_review_evidence_catalog(plan, goal, state, original_project_map)["source:project_map"]),
        )
        project_map_data = deepcopy(fixture["project_map"])
        registered = fixture["registered_source"]
        self.assertEqual(registered["content_digest"], sha256_bytes(registered["content"].encode("utf-8")))
        for entry in original_project_map.entries:
            content = (registered["content"] if entry.path == registered["path"]
                       else fixture["portable_project_files"][entry.path])
            self.assertEqual(entry.content_digest, sha256_bytes(content.encode("utf-8")))

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            for name, content in fixture["portable_project_files"].items():
                workspace.joinpath(name).write_text(content, encoding="utf-8", newline="\n")
            reference_path = Path(temporary) / "validation-reference.md"
            reference_path.write_text(registered["content"], encoding="utf-8", newline="\n")
            project_map_data["root"] = str(workspace)
            entry_id = registered["source_ref"].removeprefix("project:")
            next(entry for entry in project_map_data["entries"] if entry["entry_id"] == entry_id)["path"] = str(reference_path)
            project_map = ProjectMapRevision.model_validate(project_map_data)
            validate_plan_inspection(
                envelope.inspection,
                plan=plan,
                goal=goal,
                project_map=project_map,
                evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
                findings=envelope.review.findings,
            )

        assessment = assess_case_inspection_review(
            envelope,
            fixture["case_expectation"],
            case_id="clean",
            payload=payload,
        )
        self.assertEqual(fixture["original_assessment"], assessment)
        self.assertFalse(assessment["passed"])
        self.assertEqual(["VAL_SCOPE_001"], assessment["unexpected_findings"])
        self.assertEqual(
            [
                ("ac_004", "val_task_scope_preservation", False, True),
                ("ac_004", "val_task_unittest", False, True),
            ],
            [
                (row["criterion_id"], row["validation_id"], row["expected"], row["actual"])
                for row in assessment["fixed_ac_link_requirement_assessment"]["requirement_differences"]
            ],
        )

    def test_r25_and_r26_failures_keep_distinct_meaning(self) -> None:
        r25 = json.loads((FIXTURES / "r-s06-25-clean-semantic-failure.json").read_text(encoding="utf-8"))
        r26 = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        r25_differences = {
            (row["criterion_id"], row["validation_id"], row["expected"], row["actual"])
            for row in r25["expected_requirement_differences"]
        }
        r26_differences = {
            (row["criterion_id"], row["validation_id"], row["expected"], row["actual"])
            for row in r26["original_assessment"]["fixed_ac_link_requirement_assessment"]["requirement_differences"]
        }
        self.assertEqual(3, len(r25_differences))
        self.assertEqual(2, len(r26_differences))
        self.assertTrue(all(expected and not actual for _, _, expected, actual in r25_differences))
        self.assertTrue(all(not expected and actual for _, _, expected, actual in r26_differences))
        self.assertEqual(["VAL_SCOPE_001"], r26["original_assessment"]["unexpected_findings"])


if __name__ == "__main__":
    unittest.main()
