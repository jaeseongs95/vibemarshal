"""동결 v1 사례 기대값으로 v2 직접 제출물을 평가하는 회귀."""
from __future__ import annotations

from copy import deepcopy
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import FindingSeverity, GateName, ReviewFinding
from flowmarshal.engine.plan_inspection_eval import bind_case_expectation
from flowmarshal.engine.plan_inspection_eval_v2 import (
    V2_EVALUATION_CONTRACT, assess_case_inspection_review_v2, assess_inspection_review_v2,
)
from flowmarshal.engine.plan_inspection_v2 import (
    CompiledPlanInspectionV2, InspectionRowClosureV2, PlanInspectionV2,
    ReviewFindingV2, plan_inspection_citation_catalog_v2,
)
from flowmarshal.engine.planning import plan_review_evidence_catalog, plan_validation_scope_rows, validation_comparison_targets
from tests.engine_inspection_helpers import inspection_fixture
from tests.test_engine_inspection_case_binding import RAW
from tests.test_engine_plan_inspection import EXPECTED, inputs, submission


RATINGS = {key: 4 for key in ("goal_fit", "grounding", "engineering", "verification", "execution_safety")}


def _citation_ref_map(old, citation_catalog):
    mapped = {}
    for old_citation in old["citations"]:
        candidates = [
            item for item in citation_catalog
            if item.source_ref == old_citation["source_ref"]
            and item.selector == old_citation["selector"]
            and (old_citation["quote"] in item.quote or item.quote in old_citation["quote"])
        ]
        if candidates:
            mapped[old_citation["citation_id"]] = max(candidates, key=lambda item: len(item.quote)).citation_id
    return mapped


class PlanInspectionEvalV2Tests(unittest.TestCase):
    def _baseline(self):
        payload = deepcopy(RAW["request"]["payload"])
        plan, goal, state, project_map = inputs("clean")
        rows = [
            {key: value for key, value in row.items() if key != "relation"}
            | {"ac_link_required": row["relation"] == "explicit_procedure"}
            for row in RAW["original_relation_rows"]
        ]
        normalized_plan = plan.model_dump(mode="json")
        coverage = {row["criterion_id"]: row["validation_ids"]
                    for row in normalized_plan["definition"]["goal_coverage"]}
        for row in rows:
            if row["ac_link_required"] and row["validation_id"] not in coverage[row["criterion_id"]]:
                coverage[row["criterion_id"]].append(row["validation_id"])
        for validation in normalized_plan["definition"]["integration_validations"]:
            validation["criterion_refs"] = [
                row["criterion_id"] for row in normalized_plan["definition"]["goal_coverage"]
                if validation["validation_id"] in row["validation_ids"]
            ]
        normalized_plan["definition_digest"] = sha256_digest(normalized_plan["definition"])
        plan = type(plan).model_validate(normalized_plan)
        payload["evidence_catalog"]["artifact:plan_contract"] = plan.model_dump(mode="json")
        payload["evidence_catalog"]["source:goal"] = goal.definition.model_dump(mode="json")
        payload["evidence_catalog"]["source:state"] = state.model_dump(mode="json")
        payload["evidence_catalog"]["source:project_map"] = project_map.model_dump(mode="json")
        payload["validation_scope_rows"] = plan_validation_scope_rows(plan)
        payload["validation_comparison_targets"] = validation_comparison_targets(goal, plan)
        evidence_catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        citation_catalog = plan_inspection_citation_catalog_v2(evidence_catalog, project_map)
        payload["inspection_citation_catalog"] = [
            item.model_dump(mode="json") for item in citation_catalog
        ]
        expectation = bind_case_expectation(
            case_id="clean", payload=payload, rows=rows, defects=[], review_digest="sha256:" + "1" * 64,
        )
        v1 = inspection_fixture(plan.model_dump(mode="json"), goal.definition.model_dump(mode="json"), revision=True)
        expected_bool = {(row["criterion_id"], row["validation_id"]): row["ac_link_required"] for row in rows}
        scopes = {row["validation_id"]: row["scope_id"] for row in v1["validation_scope_rows"]}
        for row in v1["ac_validation_rows"]:
            required = expected_bool[(row["criterion_id"], row["validation_id"])]
            row["ac_link_required"] = required
            row["scope_ids"] = [scopes[row["validation_id"]]] if required else []
        ref_map = _citation_ref_map(v1, citation_catalog)
        v2 = {
            "validation_rows": [
                {"validation_id": row["validation_id"],
                 "mechanisms": [{"mechanism_id": f"mech_{row['validation_id']}_{index}", "tool": mechanism["tool"],
                                  "phase": mechanism["phase"], "direct_refs": list(dict.fromkeys(
                                      ref_map[item] for item in mechanism["basis_refs"]
                                  ))}
                                for index, mechanism in enumerate(row["mechanisms"])]}
                for row in v1["validation_rows"]
            ],
            "validation_scope_rows": [
                {"scope_id": row["scope_id"], "validation_id": row["validation_id"],
                 "mechanism_id": f"mech_{row['validation_id']}_0",
                 "claim": row["procedure"],
                 "direct_extra_refs": [], "status": row["assessment"]}
                for row in v1["validation_scope_rows"]
            ],
            "ac_validation_links": [
                {"criterion_id": row["criterion_id"], "validation_id": row["validation_id"],
                 "scope_ids": row["scope_ids"],
                 "requirement_claim": f"{row['criterion_id']}의 명시 검사 절차"}
                for row in v1["ac_validation_rows"] if row["ac_link_required"]
            ],
            "constraint_task_rows": [
                {"constraint_id": row["constraint_id"], "task_ref": row["task_ref"],
                 "applicability": row["applicability"], "required_validation_ids": row["validation_ids"]}
                for row in v1["constraint_task_rows"]
            ],
        }
        return (payload, plan, goal, project_map, expectation, PlanInspectionV2.model_validate(v2),
                evidence_catalog)

    def test_clean_reuses_fixed_28_boolean_expectations_and_rating_branch(self):
        payload, plan, goal, project_map, expectation, inspection, catalog = self._baseline()
        report = assess_case_inspection_review_v2(
            inspection, (), RATINGS, expectation, case_id="clean", payload=payload, plan=plan,
            goal=goal, project_map=project_map, evidence_catalog=catalog,
        )
        self.assertTrue(report["passed"])
        self.assertEqual(28, len(report["fixed_ac_link_requirement_assessment"]["link_presence"]))
        self.assertTrue(report["fixed_ac_link_requirement_assessment"]["passed"])
        self.assertEqual(V2_EVALUATION_CONTRACT, report["v2_contract"])

    def test_wrong_positive_scope_link_fails_without_treating_compiled_closure_as_meaning_answer(self):
        payload, plan, goal, project_map, expectation, inspection, catalog = self._baseline()
        raw = inspection.model_dump(mode="json")
        expected = expectation["ac_validation_rows"][0]
        key = (expected["criterion_id"], expected["validation_id"])
        if expected["ac_link_required"]:
            raw["ac_validation_links"] = [
                row for row in raw["ac_validation_links"]
                if (row["criterion_id"], row["validation_id"]) != key
            ]
        else:
            scope = next(row for row in raw["validation_scope_rows"]
                         if row["validation_id"] == expected["validation_id"]
                         and row["status"] == "supported")
            raw["ac_validation_links"].append({
                "criterion_id": expected["criterion_id"],
                "validation_id": expected["validation_id"],
                "scope_ids": [scope["scope_id"]],
                "requirement_claim": "잘못 추가한 필수 관계",
            })
        report = assess_case_inspection_review_v2(
            PlanInspectionV2.model_validate(raw), (), RATINGS, expectation, case_id="clean", payload=payload,
            plan=plan, goal=goal, project_map=project_map, evidence_catalog=catalog,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(1, len(report["fixed_ac_link_requirement_assessment"]["requirement_differences"]))

    def test_direct_finding_matching_rejects_missing_extra_shared_target_and_closure_evidence(self):
        payload, plan, _goal, _project_map, _expectation, inspection, _catalog = self._baseline()
        validation = inspection.validation_rows[0]
        owner = plan.definition.tasks[0].task_ref
        validation_row = next(
            item for item in payload["validation_comparison_targets"]["validations"]
            if item["validation_id"] == validation.validation_id
        )
        claim_selector = validation_row["selector"] + "/statement"
        claim_citation = next(
            item for item in payload["inspection_citation_catalog"]
            if item["source_ref"] == "artifact:plan_contract"
            and item["selector"] == claim_selector
        )
        claim_ref = claim_citation["citation_id"]
        defect = {
            "defect_id": "required-order", "defect_kind": "result_order", "criterion_ids": [],
            "validation_ids": [validation.validation_id], "allowed_criterion_ids": [],
            "allowed_task_ref_sets": [[owner]], "required_evidence_refs": ["artifact:plan_contract"],
            "required_citations": [{"source_ref": "artifact:plan_contract",
                                    "selectors": [claim_selector]}],
        }

        def report(findings, *, evidence=("artifact:plan_contract",), closure=(claim_ref,), expected=(defect,)):
            compiled = CompiledPlanInspectionV2(
                ac_validation_decisions=(),
                derived_findings=tuple(ReviewFinding(
                    finding_code=item.finding_code, gate=GateName.EXECUTION, severity=FindingSeverity.ERROR,
                    summary="derived", evidence_refs=evidence, affected_task_refs=(owner,),
                    remediable=item.remediable,
                ) for item in findings),
                row_closures=tuple(InspectionRowClosureV2(row_kind="finding", row_id=item.finding_code,
                                                          citation_ids=closure) for item in findings),
                membership_witnesses=(),
                used_citations=(claim_citation,),
            )
            return assess_inspection_review_v2(inspection, tuple(findings), None, compiled, list(expected), plan=plan)

        valid = ReviewFindingV2(finding_code="ORDER_DEFECT", defect_kind="result_order",
                                remediable=True,
                                target_refs=({"kind": "validation", "primary_ref": validation.validation_id,
                                              "secondary_ref": None},))
        self.assertTrue(report((valid,))["passed"])
        self.assertEqual(["required-order"], report(())["missing_defects"])
        self.assertEqual(["ORDER_DEFECT"], report((valid,), expected=())["unexpected_findings"])
        self.assertFalse(report((valid,), evidence=("source:goal",))["passed"])
        wrong_raw = valid.model_dump(mode="json")
        wrong_raw["target_refs"] = [{"kind": "validation",
                                     "primary_ref": inspection.validation_rows[1].validation_id,
                                     "secondary_ref": None}]
        wrong_target = ReviewFindingV2.model_validate(wrong_raw)
        self.assertFalse(report((wrong_target,))["passed"])
        duplicate = dict(defect, defect_id="independent-order")
        shared = report((valid,), expected=(defect, duplicate))
        self.assertEqual({"required-order", "independent-order"}, set(shared["missing_defects"]))

    def test_fixed_v1_defect_expectation_matches_v2_direct_scope_target(self):
        plan, goal, state, project_map = inputs("bad")
        raw = submission("bad")["inspection"]
        evidence_catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        citation_catalog = plan_inspection_citation_catalog_v2(evidence_catalog, project_map)
        ref_map = _citation_ref_map(raw, citation_catalog)
        mechanisms = {
            row["validation_id"]: f"mech_{index}"
            for index, row in enumerate(raw["validation_rows"])
        }
        mechanism_refs = {
            row["validation_id"]: set(row["mechanisms"][0]["basis_refs"])
            for row in raw["validation_rows"]
        }
        inspection = PlanInspectionV2.model_validate({
            "validation_rows": [{
                "validation_id": row["validation_id"],
                "mechanisms": [{"mechanism_id": mechanisms[row["validation_id"]],
                                "tool": item["tool"], "phase": item["phase"],
                                "direct_refs": list(dict.fromkeys(
                                    ref_map[ref] for ref in item["basis_refs"]
                                ))} for item in row["mechanisms"]],
            } for row in raw["validation_rows"]],
            "validation_scope_rows": [{
                "scope_id": row["scope_id"], "validation_id": row["validation_id"],
                "mechanism_id": mechanisms[row["validation_id"]],
                "claim": row["procedure"],
                "direct_extra_refs": list(dict.fromkeys(
                    ref_map[ref] for ref in row["basis_refs"]
                    if ref != row["claim_ref"] and ref not in mechanism_refs[row["validation_id"]]
                )),
                "status": row["assessment"],
            } for row in raw["validation_scope_rows"]],
            "ac_validation_links": [{
                "criterion_id": row["criterion_id"], "validation_id": row["validation_id"],
                "scope_ids": row["scope_ids"],
                "requirement_claim": f"{row['criterion_id']}의 명시 검사 절차",
            } for row in raw["ac_validation_rows"] if row["ac_link_required"]],
            "constraint_task_rows": [{
                "constraint_id": row["constraint_id"], "task_ref": row["task_ref"],
                "applicability": row["applicability"], "required_validation_ids": row["validation_ids"],
            } for row in raw["constraint_task_rows"]],
        })
        source_finding = submission("bad")["review"]["findings"][0]
        scope = next(row for row in raw["validation_scope_rows"] if row["finding_codes"])
        finding = ReviewFindingV2.model_validate({
            "finding_code": source_finding["finding_code"], "defect_kind": "validation_scope",
            "remediable": True,
            "target_refs": [{"kind": "validation_scope", "primary_ref": scope["scope_id"],
                             "secondary_ref": None}],
        })
        from flowmarshal.engine.plan_inspection_v2 import compile_plan_inspection_v2
        compiled = compile_plan_inspection_v2(
            inspection, findings=(finding,), plan=plan, goal=goal, project_map=project_map,
            evidence_catalog=evidence_catalog,
            citation_catalog=citation_catalog,
        )
        report = assess_inspection_review_v2(inspection, (finding,), None, compiled, EXPECTED["bad"])
        self.assertTrue(report["passed"])
        self.assertEqual({EXPECTED["bad"][0]["defect_id"]: finding.finding_code}, report["matched_findings"])


if __name__ == "__main__":
    unittest.main()
