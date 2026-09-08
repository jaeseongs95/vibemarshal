from __future__ import annotations

from copy import deepcopy
import unittest

from flowmarshal.engine.plan_inspection_v2 import (
    ACScopeRequirementInspectionV2,
    PLAN_INSPECTION_V2_INSTRUCTIONS,
    PlanInspectionV2,
)
from flowmarshal.engine.validation_obligations import (
    EXPLICIT_VALIDATION_OBLIGATION_INSTRUCTIONS,
    merge_explicit_obligation_scope_ids,
)
from tests.test_engine_plan_inspection_v2 import compile_fixture, finding_targets, v2_inputs


class ValidationObligationContractTests(unittest.TestCase):
    def test_v2_schema_and_instructions_use_explicit_obligation_truth_condition(self):
        schema = ACScopeRequirementInspectionV2.model_json_schema()["properties"]

        self.assertEqual(
            1,
            PLAN_INSPECTION_V2_INSTRUCTIONS.count(EXPLICIT_VALIDATION_OBLIGATION_INSTRUCTIONS),
        )
        self.assertIn("명시 검사 의무", schema["statement_scope_ids"]["description"])
        self.assertIn("명시 검사 의무", schema["validation_intent_scope_ids"]["description"])
        self.assertNotIn("AC 일부를 직접 검증하면", PLAN_INSPECTION_V2_INSTRUCTIONS)

    def test_statement_and_intent_obligations_preserve_both_phases_without_duplication(self):
        *_, inspection = v2_inputs()
        raw = inspection.model_dump(mode="json")
        requirement = raw["ac_scope_requirements"][0]
        selected = list(requirement["validation_intent_scope_ids"])
        self.assertGreaterEqual(len(selected), 2)

        requirement["statement_scope_ids"] = selected[:1]
        requirement["validation_intent_scope_ids"] = selected
        compiled = compile_fixture(PlanInspectionV2.model_validate(raw))
        required = {
            row.validation_id: row.scope_ids
            for row in compiled.ac_validation_decisions
            if row.criterion_id == requirement["criterion_id"] and row.ac_link_required
        }

        self.assertEqual(
            merge_explicit_obligation_scope_ids(selected[:1], selected),
            tuple(scope_id for scope_ids in required.values() for scope_id in scope_ids),
        )

    def test_unselected_related_scope_remains_optional_even_when_coverage_contains_it(self):
        plan, _goal, _project_map, _catalog, inspection = v2_inputs()
        raw = deepcopy(inspection.model_dump(mode="json"))
        requirement = raw["ac_scope_requirements"][0]
        criterion_id = requirement["criterion_id"]
        coverage = next(
            row for row in plan.definition.goal_coverage if row.criterion_id == criterion_id
        )
        optional_validation_id = coverage.validation_ids[0]
        optional_scope_ids = {
            row["scope_id"]
            for row in raw["validation_scope_rows"]
            if row["validation_id"] == optional_validation_id
        }
        requirement["statement_scope_ids"] = []
        requirement["validation_intent_scope_ids"] = [
            scope_id
            for scope_id in requirement["validation_intent_scope_ids"]
            if scope_id not in optional_scope_ids
        ]

        compiled = compile_fixture(PlanInspectionV2.model_validate(raw))
        decision = next(
            row
            for row in compiled.ac_validation_decisions
            if row.criterion_id == criterion_id
            and row.validation_id == optional_validation_id
        )
        witness = next(
            row
            for row in compiled.membership_witnesses
            if row.criterion_id == criterion_id
            and row.validation_id == optional_validation_id
        )

        self.assertFalse(decision.ac_link_required)
        self.assertEqual((), decision.scope_ids)
        self.assertTrue(witness.observed_present)
        self.assertEqual((), compiled.derived_findings)

    def test_partial_scope_defect_preserves_every_supported_relation_and_the_finding(self):
        *_, inspection = v2_inputs()
        original = compile_fixture(inspection)
        raw = deepcopy(inspection.model_dump(mode="json"))
        overclaim = deepcopy(raw["validation_scope_rows"][0])
        overclaim.update(
            scope_id="scope_unsupported_extra_procedure",
            claim="등록 수단이 실제 수행하지 않는 별도 검사까지 수행한다는 주장",
            status="contradicted",
        )
        raw["validation_scope_rows"].append(overclaim)
        finding = {
            "finding_code": "PARTIAL_SCOPE_OVERCLAIM",
            "defect_kind": "validation_scope",
            "remediable": True,
            **finding_targets("validation_scope", overclaim["scope_id"]),
        }

        compiled = compile_fixture(PlanInspectionV2.model_validate(raw), (finding,))

        def required_relations(value):
            return {
                (row.criterion_id, row.validation_id): row.scope_ids
                for row in value.ac_validation_decisions if row.ac_link_required
            }

        self.assertEqual(required_relations(original), required_relations(compiled))
        self.assertTrue(any(row.validation_id == overclaim["validation_id"]
                            and row.ac_link_required for row in compiled.ac_validation_decisions))
        self.assertEqual(["PARTIAL_SCOPE_OVERCLAIM"],
                         [item.finding_code for item in compiled.derived_findings])


if __name__ == "__main__":
    unittest.main()
