from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import PlanContractRevision
from flowmarshal.engine.plan_inspection import PlanInspectionError
from flowmarshal.engine.plan_inspection_provider import (
    PLAN_INSPECTION_PROVIDER_V2,
    bind_plan_inspection_request,
    verify_plan_inspection_result_binding,
)
from flowmarshal.engine.plan_inspection_v2 import (
    InspectionTargetV2,
    PlanInspectionV2,
    ReviewFindingV2,
    compile_plan_inspection_v2,
)
from flowmarshal.engine.planning import plan_review_evidence_catalog, validation_comparison_targets
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter,
    PlanReviewerAdapter,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
)
from flowmarshal.engine.roles import RoleCallResult, ScriptedStructuredRoleRunner
from tests.engine_helpers import assignment, goal as simple_goal, inventory, profile, project_map, state
from tests.engine_inspection_helpers import inspection_fixture
from tests.test_engine_role_adapters import _plan_response, _skeleton_response
from tests.test_engine_plan_inspection import inputs


def v2_inputs():
    plan, goal, state, project_map = inputs("clean")
    old = inspection_fixture(
        plan.model_dump(mode="json"), goal.definition.model_dump(mode="json"), revision=True
    )
    mechanism_ids: dict[str, str] = {}
    validation_rows = []
    for index, row in enumerate(old["validation_rows"]):
        mechanism_id = f"mechanism_{index}"
        mechanism_ids[row["validation_id"]] = mechanism_id
        mechanism = row["mechanisms"][0]
        validation_rows.append({
            "validation_id": row["validation_id"],
            "claim_ref": row["claim_ref"],
            "mechanisms": [{
                "mechanism_id": mechanism_id,
                "tool": mechanism["tool"],
                "phase": mechanism["phase"],
                "direct_refs": mechanism["basis_refs"],
            }],
        })
    inspection = PlanInspectionV2.model_validate({
        "citations": old["citations"],
        "validation_rows": validation_rows,
        "validation_scope_rows": [{
            "scope_id": row["scope_id"],
            "validation_id": row["validation_id"],
            "mechanism_id": mechanism_ids[row["validation_id"]],
            "claim_ref": row["claim_ref"],
            "direct_extra_refs": [],
            "status": row["assessment"],
        } for row in old["validation_scope_rows"]],
        "ac_validation_rows": [{
            "criterion_id": row["criterion_id"],
            "validation_id": row["validation_id"],
            "ac_link_required": row["validation_id"] in next(
                item.validation_ids for item in plan.definition.goal_coverage
                if item.criterion_id == row["criterion_id"]
            ),
            "scope_ids": [next(
                item["scope_id"] for item in old["validation_scope_rows"]
                if item["validation_id"] == row["validation_id"] and item["assessment"] == "supported"
            )] if row["validation_id"] in next(
                item.validation_ids for item in plan.definition.goal_coverage
                if item.criterion_id == row["criterion_id"]
            ) else [],
            "direct_extra_refs": [],
        } for row in old["ac_validation_rows"]],
        "constraint_task_rows": [{
            "constraint_id": row["constraint_id"],
            "task_ref": row["task_ref"],
            "applicability": row["applicability"],
            "required_validation_ids": row["validation_ids"],
        } for row in old["constraint_task_rows"]],
    })
    catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
    return plan, goal, project_map, catalog, inspection


def v2_table_from_v1(old: dict, coverage: dict[str, list[str]]) -> dict:
    mechanism_ids = {
        row["validation_id"]: f"mechanism_{index}"
        for index, row in enumerate(old["validation_rows"])
    }
    supported_scopes = {
        row["validation_id"]: row["scope_id"]
        for row in old["validation_scope_rows"]
        if row["assessment"] == "supported"
    }
    return {
        "citations": old["citations"],
        "validation_rows": [{
            "validation_id": row["validation_id"],
            "claim_ref": row["claim_ref"],
            "mechanisms": [{
                "mechanism_id": mechanism_ids[row["validation_id"]],
                "tool": mechanism["tool"],
                "phase": mechanism["phase"],
                "direct_refs": mechanism["basis_refs"],
            } for mechanism in row["mechanisms"]],
        } for row in old["validation_rows"]],
        "validation_scope_rows": [{
            "scope_id": row["scope_id"],
            "validation_id": row["validation_id"],
            "mechanism_id": mechanism_ids[row["validation_id"]],
            "claim_ref": row["claim_ref"],
            "direct_extra_refs": [],
            "status": row["assessment"],
        } for row in old["validation_scope_rows"]],
        "ac_validation_rows": [{
            "criterion_id": row["criterion_id"],
            "validation_id": row["validation_id"],
            "ac_link_required": row["validation_id"] in coverage[row["criterion_id"]],
            "scope_ids": [supported_scopes[row["validation_id"]]]
            if row["validation_id"] in coverage[row["criterion_id"]] else [],
            "direct_extra_refs": [],
        } for row in old["ac_validation_rows"]],
        "constraint_task_rows": [{
            "constraint_id": row["constraint_id"],
            "task_ref": row["task_ref"],
            "applicability": row["applicability"],
            "required_validation_ids": row["validation_ids"],
        } for row in old["constraint_task_rows"]],
    }


def compile_fixture(inspection, findings=(), *, plan=None):
    base_plan, goal, project_map, catalog, _ = v2_inputs()
    plan = plan or base_plan
    catalog = dict(catalog)
    catalog["artifact:plan_contract"] = plan.model_dump(mode="json")
    return compile_plan_inspection_v2(
        inspection,
        findings=tuple(ReviewFindingV2.model_validate(item) for item in findings),
        plan=plan,
        goal=goal,
        project_map=project_map,
        evidence_catalog=catalog,
    )


def owner_for(plan, goal, validation_id):
    return next(row["task_ref"] for row in validation_comparison_targets(goal, plan)["validations"]
                if row["validation_id"] == validation_id)


class PlanInspectionV2Tests(unittest.TestCase):
    def test_clean_compiles_repeated_closure_without_provider_repetition(self):
        plan, goal, _, _, inspection = v2_inputs()
        before = inspection.model_dump(mode="json")
        self.assertTrue(all(not row.direct_extra_refs for row in inspection.ac_validation_rows))
        self.assertTrue(any(row.ac_link_required for row in inspection.ac_validation_rows))
        compiled = compile_fixture(inspection)
        ac_closures = [row for row in compiled.row_closures if row.row_kind == "ac_validation"]
        self.assertEqual(len(inspection.ac_validation_rows), len(ac_closures))
        self.assertTrue(all(len(row.citation_ids) >= 3 for row in ac_closures))
        self.assertEqual((), compiled.derived_findings)
        self.assertEqual(before, inspection.model_dump(mode="json"))
        self.assertEqual(
            len(goal.definition.hard_acceptance) *
            len(validation_comparison_targets(goal, plan)["validations"]),
            len(compiled.membership_witnesses),
        )

    def test_missing_coverage_member_has_typed_witness_without_fake_quote(self):
        plan, goal, _, _, inspection = v2_inputs()
        row = inspection.ac_validation_rows[0]
        raw_plan = plan.model_dump(mode="json")
        coverage_index = next(i for i, item in enumerate(raw_plan["definition"]["goal_coverage"])
                              if item["criterion_id"] == row.criterion_id)
        raw_plan["definition"]["goal_coverage"][coverage_index]["validation_ids"].remove(row.validation_id)
        raw_plan["definition_digest"] = sha256_digest(raw_plan["definition"])
        changed_plan = PlanContractRevision.model_validate(raw_plan)
        scope = next(item for item in inspection.validation_scope_rows
                     if item.validation_id == row.validation_id and item.status == "supported")
        raw_inspection = inspection.model_dump(mode="json")
        target_row = next(item for item in raw_inspection["ac_validation_rows"]
                          if (item["criterion_id"], item["validation_id"]) ==
                          (row.criterion_id, row.validation_id))
        target_row.update(ac_link_required=True, scope_ids=[scope.scope_id])
        changed = PlanInspectionV2.model_validate(raw_inspection)
        owner = owner_for(changed_plan, goal, row.validation_id)
        findings = ({
            "finding_code": "MISSING_LINK",
            "defect_kind": "missing_validation_link",
            "affected_task_refs": [owner] if owner else [],
            "remediable": True,
            "target_refs": [{
                "kind": "ac_validation", "primary_ref": row.criterion_id,
                "secondary_ref": row.validation_id,
            }],
        },)
        compiled = compile_fixture(changed, findings, plan=changed_plan)
        witness = next(item for item in compiled.membership_witnesses
                       if (item.criterion_id, item.validation_id) ==
                       (row.criterion_id, row.validation_id))
        self.assertFalse(witness.observed_present)
        self.assertIsNone(witness.member_selector)
        self.assertTrue(witness.collection_selector.endswith("/validation_ids"))

    def test_unknown_direct_ref_is_rejected(self):
        *_, inspection = v2_inputs()
        raw = inspection.model_dump(mode="json")
        raw["validation_rows"][0]["mechanisms"][0]["direct_refs"] = ["not_a_citation"]
        with self.assertRaisesRegex(PlanInspectionError, "없는 인용 ID"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

    def test_scope_cannot_use_foreign_validation_mechanism(self):
        *_, inspection = v2_inputs()
        raw = inspection.model_dump(mode="json")
        raw["validation_scope_rows"][0]["mechanism_id"] = raw["validation_rows"][1]["mechanisms"][0]["mechanism_id"]
        with self.assertRaisesRegex(PlanInspectionError, "소유 validation 불일치"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

    def test_ac_true_requires_supported_scope_and_false_requires_no_scopes(self):
        *_, inspection = v2_inputs()
        for case in ("true_nonsupported", "false_with_scope"):
            with self.subTest(case=case):
                raw = inspection.model_dump(mode="json")
                row = raw["ac_validation_rows"][0]
                scope = next(item for item in raw["validation_scope_rows"]
                             if item["validation_id"] == row["validation_id"])
                row["scope_ids"] = [scope["scope_id"]]
                if case == "true_nonsupported":
                    row["ac_link_required"] = True
                    scope["status"] = "unresolved"
                    expected = "supported scope"
                else:
                    row["ac_link_required"] = False
                    expected = "scope_ids는 비어야"
                with self.assertRaisesRegex(PlanInspectionError, expected):
                    compile_fixture(PlanInspectionV2.model_validate(raw))

    def test_constraint_partial_is_explicit_and_empty_missing_is_rejected(self):
        plan, goal, _, _, inspection = v2_inputs()
        raw = inspection.model_dump(mode="json")
        row = raw["constraint_task_rows"][0]
        validation_id = plan.definition.tasks[0].validations[0].validation_id
        row.update(applicability="required", required_validation_ids=[validation_id])
        finding = {
            "finding_code": "PARTIAL_TASK_VALIDATION",
            "defect_kind": "missing_task_validation",
            "affected_task_refs": [row["task_ref"]],
            "remediable": True,
            "target_refs": [{"kind": "constraint_task", "primary_ref": row["constraint_id"],
                             "secondary_ref": row["task_ref"]}],
        }
        compiled = compile_fixture(PlanInspectionV2.model_validate(raw), (finding,))
        self.assertEqual("PARTIAL_TASK_VALIDATION", compiled.derived_findings[0].finding_code)

        raw["constraint_task_rows"][0]["required_validation_ids"] = []
        with self.assertRaisesRegex(PlanInspectionError, "validation과 finding이 모두 없습니다"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

    def test_supported_scope_finding_and_nonsupported_scope_without_finding_are_rejected(self):
        plan, goal, _, _, inspection = v2_inputs()
        scope = inspection.validation_scope_rows[0]
        owner = owner_for(plan, goal, scope.validation_id)
        finding = {
            "finding_code": "FALSE_SCOPE_FINDING",
            "defect_kind": "validation_scope",
            "affected_task_refs": [owner] if owner else [],
            "remediable": True,
            "target_refs": [{"kind": "validation_scope", "primary_ref": scope.scope_id,
                             "secondary_ref": None}],
        }
        with self.assertRaisesRegex(PlanInspectionError, "supported scope에 finding"):
            compile_fixture(inspection, (finding,))

        raw = inspection.model_dump(mode="json")
        raw["validation_scope_rows"][0]["status"] = "contradicted"
        changed_validation_id = raw["validation_scope_rows"][0]["validation_id"]
        for ac_row in raw["ac_validation_rows"]:
            if ac_row["validation_id"] == changed_validation_id:
                ac_row.update(ac_link_required=False, scope_ids=[])
        with self.assertRaisesRegex(PlanInspectionError, "finding이 누락"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

    def test_finding_target_must_match_affected_task(self):
        plan, goal, _, _, inspection = v2_inputs()
        validation_id = inspection.validation_rows[0].validation_id
        self.assertIsNotNone(owner_for(plan, goal, validation_id))
        finding = {
            "finding_code": "ORDER_ERROR",
            "defect_kind": "result_order",
            "affected_task_refs": [],
            "remediable": True,
            "target_refs": [{"kind": "validation", "primary_ref": validation_id,
                             "secondary_ref": None}],
        }
        with self.assertRaisesRegex(PlanInspectionError, "affected Task 불일치"):
            compile_fixture(inspection, (finding,))

    def test_finding_kind_rejects_incompatible_target_kind(self):
        *_, inspection = v2_inputs()
        finding = {
            "finding_code": "WRONG_TARGET",
            "defect_kind": "missing_validation_link",
            "affected_task_refs": [],
            "remediable": True,
            "target_refs": [{"kind": "validation_scope",
                             "primary_ref": inspection.validation_scope_rows[0].scope_id,
                             "secondary_ref": None}],
        }
        with self.assertRaisesRegex(PlanInspectionError, "target 종류 불일치"):
            compile_fixture(inspection, (finding,))

    def test_multi_target_closure_unions_direct_sources(self):
        plan, goal, _, _, inspection = v2_inputs()
        validation_ids = [row.validation_id for row in inspection.validation_rows[:2]]
        owner = owner_for(plan, goal, validation_ids[0])
        finding = {
            "finding_code": "MULTI_ORDER",
            "defect_kind": "result_order",
            "affected_task_refs": [owner],
            "remediable": True,
            "target_refs": [{"kind": "validation", "primary_ref": item, "secondary_ref": None}
                            for item in validation_ids],
        }
        compiled = compile_fixture(inspection, (finding,))
        closure = next(row for row in compiled.row_closures if row.row_id == "MULTI_ORDER")
        claims = {row.claim_ref for row in inspection.validation_rows[:2]}
        self.assertTrue(claims <= set(closure.citation_ids))

    def test_project_citation_maps_only_to_project_map_evidence(self):
        plan, goal, project_map, _, inspection = v2_inputs()
        entry = next(item for item in project_map.entries if item.kind.value == "reference")
        from pathlib import Path
        content = Path(entry.path).read_text(encoding="utf-8")
        raw = inspection.model_dump(mode="json")
        raw["citations"].append({
            "citation_id": "project_scope",
            "source_ref": f"project:{entry.entry_id}",
            "selector": "/content",
            "quote": content[:80],
        })
        validation_id = raw["validation_rows"][0]["validation_id"]
        raw["validation_rows"][0]["mechanisms"][0]["direct_refs"].append("project_scope")
        changed = PlanInspectionV2.model_validate(raw)
        owner = owner_for(plan, goal, validation_id)
        finding = {
            "finding_code": "PROJECT_ORDER",
            "defect_kind": "result_order",
            "affected_task_refs": [owner] if owner else [],
            "remediable": True,
            "target_refs": [{"kind": "validation", "primary_ref": validation_id,
                             "secondary_ref": None}],
        }
        compiled = compile_fixture(changed, (finding,))
        self.assertIn("source:project_map", compiled.derived_findings[0].evidence_refs)
        self.assertNotIn(f"project:{entry.entry_id}", compiled.derived_findings[0].evidence_refs)

    def test_other_finding_preserves_direct_classification_and_citation_targets(self):
        plan, _goal, _project_map, catalog, inspection = v2_inputs()
        raw = inspection.model_dump(mode="json")
        state_value = catalog["source:state"]["facts"][0]["value"]
        raw["citations"].append({
            "citation_id": "state_map_value",
            "source_ref": "source:state",
            "selector": "/facts/0/value",
            "quote": state_value,
        })
        finding = {
            "finding_code": "STATE_PROJECT_MAP_BINDING_MISMATCH",
            "defect_kind": "other",
            "gate": "grounding",
            "severity": "error",
            "affected_task_refs": [plan.definition.tasks[0].task_ref],
            "remediable": True,
            "target_refs": [
                {"kind": "citation", "primary_ref": "state_map_value", "secondary_ref": None},
                {"kind": "task", "primary_ref": plan.definition.tasks[0].task_ref,
                 "secondary_ref": None},
            ],
        }
        compiled = compile_fixture(PlanInspectionV2.model_validate(raw), (finding,))
        derived = compiled.derived_findings[0]
        self.assertEqual("grounding", derived.gate.value)
        self.assertEqual("error", derived.severity.value)
        self.assertEqual(("source:state",), derived.evidence_refs)

    def test_standard_and_other_finding_classification_boundaries_are_strict(self):
        common = {
            "finding_code": "CLASSIFICATION_BOUNDARY",
            "affected_task_refs": [],
            "remediable": True,
            "target_refs": [{"kind": "validation", "primary_ref": "val_example",
                             "secondary_ref": None}],
        }
        with self.assertRaisesRegex(ValueError, "표준 finding"):
            ReviewFindingV2.model_validate(
                common | {"defect_kind": "result_order", "gate": "execution", "severity": "error"}
            )
        with self.assertRaisesRegex(ValueError, "other finding"):
            ReviewFindingV2.model_validate(
                common | {
                    "defect_kind": "other",
                    "target_refs": [{"kind": "citation", "primary_ref": "citation_example",
                                     "secondary_ref": None}],
                }
            )

    def test_uniform_target_requires_exact_reference_arity(self):
        with self.assertRaisesRegex(ValueError, "secondary_ref가 필요"):
            InspectionTargetV2(
                kind="ac_validation", primary_ref="ac_001", secondary_ref=None
            )
        with self.assertRaisesRegex(ValueError, "secondary_ref는 null"):
            InspectionTargetV2(
                kind="validation", primary_ref="val_001", secondary_ref="unexpected"
            )

    def test_compiler_preserves_all_provider_semantic_fields(self):
        *_, inspection = v2_inputs()
        finding = ()
        before_inspection = deepcopy(inspection.model_dump(mode="json"))
        before_findings = deepcopy(finding)
        compile_fixture(inspection, finding)
        self.assertEqual(before_inspection, inspection.model_dump(mode="json"))
        self.assertEqual(before_findings, finding)


class PlanInspectionV2AdapterTests(unittest.TestCase):
    def test_expander_v2_uses_separate_schema_and_binds_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("지침", encoding="utf-8")
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            project_id = "project_" + "6" * 32
            project_profile = profile(project_id)
            current_goal = simple_goal(project_id, project_profile.definition_digest)
            current_map = project_map(project_id, root)
            current_state = state(project_id, current_goal.definition_digest, current_map.revision_digest)
            current_inventory = inventory()
            draft = _plan_response()
            old = inspection_fixture(draft, current_goal.definition.model_dump(mode="json"))
            coverage = {row["criterion_id"]: row["validation_ids"] for row in draft["goal_coverage"]}
            envelope = {"inspection": v2_table_from_v1(old, coverage), "plan": draft}
            runner = ScriptedStructuredRoleRunner({
                "skeleton_generator": [_skeleton_response()],
                "plan_expander": [envelope],
            })
            options = {
                "model": "worker", "effort": "medium",
                "inventory_digest": current_inventory.inventory_digest, "cwd": root,
            }
            candidate = SkeletonGeneratorAdapter(runner, **options).generate(
                goal=current_goal, state=current_state, project_map=current_map, candidate_count=1,
            )[0]
            adapter = PlanExpanderAdapter(
                runner,
                RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
                inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2,
                **options,
            )
            plan = adapter.expand(
                candidate=candidate, goal=current_goal, state=current_state, project_map=current_map,
            )
            request = runner.calls[-1]
            self.assertEqual(draft["tasks"][0]["objective"], plan.definition.tasks[0].objective)
            self.assertIn("plan-inspection-v2", request.instructions)
            self.assertIn("direct_extra_refs", str(request.output_schema))
            self.assertNotIn("finding_links", str(request.output_schema))
            binding = bind_plan_inspection_request(request, PLAN_INSPECTION_PROVIDER_V2)
            verify_plan_inspection_result_binding(
                binding, request, RoleCallResult(payload=envelope, receipt=adapter.receipts[-1]),
            )

    def test_reviewer_v2_returns_only_compiled_core_submission(self):
        plan, current_goal, current_map, _catalog, inspection = v2_inputs()
        _, _, current_state, _ = inputs("clean")
        ratings = {
            "goal_fit": 4, "grounding": 4, "engineering": 4,
            "verification": 4, "execution_safety": 4,
        }
        envelope = {
            "inspection": inspection.model_dump(mode="json"),
            "review": {"findings": [], "ratings": ratings},
        }
        runner = ScriptedStructuredRoleRunner({"compact_plan_reviewer": [envelope]})
        adapter = PlanReviewerAdapter(
            runner,
            model="validator", effort="high", inventory_digest="sha256:" + "1" * 64,
            cwd=Path(current_map.root), inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2,
        )
        submission = adapter.review(
            plan=plan, goal=current_goal, state=current_state, project_map=current_map,
            risk_route="compact_plan_reviewer",
        )
        request = runner.calls[-1]
        self.assertEqual((), submission.findings)
        self.assertEqual(4, submission.ratings.verification)
        self.assertIn("target_refs", str(request.output_schema))
        finding_properties = request.output_schema["properties"]["review"]["anyOf"][1][
            "properties"
        ]["findings"]["items"]["properties"]
        self.assertNotIn("evidence_refs", finding_properties)
        self.assertNotIn("summary", finding_properties)
        target_items = finding_properties["target_refs"]["items"]
        self.assertNotIn("discriminator", target_items)
        self.assertNotIn("oneOf", target_items)
        self.assertEqual(
            {"kind", "primary_ref", "secondary_ref"},
            set(target_items["properties"]),
        )
        binding = bind_plan_inspection_request(request, PLAN_INSPECTION_PROVIDER_V2)
        verify_plan_inspection_result_binding(
            binding, request, RoleCallResult(payload=envelope, receipt=adapter.receipts[-1]),
        )
        changed = binding.model_copy(update={"provider_version": "plan-inspection-v1"})
        with self.assertRaisesRegex(ValueError, "REQUEST_BINDING_MISMATCH"):
            verify_plan_inspection_result_binding(
                changed, request, RoleCallResult(payload=envelope, receipt=adapter.receipts[-1]),
            )


if __name__ == "__main__":
    unittest.main()
