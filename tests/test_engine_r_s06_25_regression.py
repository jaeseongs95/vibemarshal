"""R-S06-25 clean 응답의 구조 성공과 의미 실패를 보존하는 회귀."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.domain import (
    GoalContractRevision,
    PlanContractRevision,
    ProjectMapRevision,
    StateSnapshot,
)
from flowmarshal.engine.plan_inspection import validate_plan_inspection
from flowmarshal.engine.plan_inspection_eval import assess_fixed_ac_link_requirements
from flowmarshal.engine.planner_roles import PlanReviewEnvelope
from flowmarshal.engine.planning import plan_review_evidence_catalog


FIXTURE_PATH = Path(__file__).parent / "fixtures/engine/r-s06-25-clean-semantic-failure.json"


class RS0625SemanticFailureRegressionTests(unittest.TestCase):
    def test_original_clean_response_remains_structurally_valid_and_semantically_failed(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        provenance = fixture["provenance"]
        self.assertEqual("model_call_14959e9166524ad19294666c6352e488", provenance["call_id"])
        self.assertEqual("01a06ffa-d0df-72f2-9900-4540b211bb54", provenance["thread_id"])
        self.assertEqual("01a06ffa-d6f4-7d62-9c25-5d32da1a067a", provenance["turn_id"])
        self.assertEqual(("gpt-5.6-terra", "high"), (provenance["model"], provenance["effort"]))
        self.assertFalse(provenance["original_assessment_passed"])
        self.assertEqual(provenance["output_digest"], sha256_digest(fixture["raw_response"]))

        # Pydantic schema와 adapter의 구조 검사는 통과하되 의미 기대값은 별도로 재계산한다.
        envelope = PlanReviewEnvelope.model_validate(fixture["raw_response"])
        plan = PlanContractRevision.model_validate(fixture["plan"])
        goal = GoalContractRevision.model_validate(fixture["goal"])
        state = StateSnapshot.model_validate(fixture["state"])
        project_map_data = deepcopy(fixture["project_map"])
        registered = fixture["registered_source"]
        self.assertEqual(registered["content_digest"], sha256_bytes(registered["content"].encode("utf-8")))

        with tempfile.TemporaryDirectory() as temporary:
            reference_path = Path(temporary) / "validation-reference.md"
            reference_path.write_bytes(registered["content"].encode("utf-8"))
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

        assessment = assess_fixed_ac_link_requirements(
            envelope.inspection,
            fixture["expected_ac_validation_rows"],
            fixture["plan"],
        )
        self.assertTrue(assessment["pair_set_matches"])
        self.assertEqual(fixture["expected_requirement_differences"], assessment["requirement_differences"])
        self.assertEqual(
            [
                ("ac_003", "val_goal_independent_behavior_contract", True, False),
                ("ac_003", "val_task_add_behavior_contract", True, False),
                ("ac_004", "val_task_add_behavior_contract", True, False),
            ],
            [
                (row["criterion_id"], row["validation_id"], row["expected"], row["actual"])
                for row in assessment["requirement_differences"]
            ],
        )
        self.assertFalse(assessment["passed"])


if __name__ == "__main__":
    unittest.main()
