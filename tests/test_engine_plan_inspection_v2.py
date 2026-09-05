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
    PlanInspectionV2,
    ReviewFindingV2,
    compile_plan_inspection_v2,
    plan_inspection_citation_catalog_v2,
    plan_inspection_target_catalog_v2,
)
from flowmarshal.engine.planning import plan_review_evidence_catalog, validation_comparison_targets
from flowmarshal.engine.planner_roles import (
    PLAN_VALIDATION_TRACE_V2_INSTRUCTIONS,
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
    catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
    citation_catalog = plan_inspection_citation_catalog_v2(catalog, project_map)
    old = inspection_fixture(
        plan.model_dump(mode="json"), goal.definition.model_dump(mode="json"), revision=True
    )
    ref_map = citation_ref_map(old, citation_catalog)
    mechanism_ids: dict[str, str] = {}
    validation_rows = []
    for index, row in enumerate(old["validation_rows"]):
        mechanism_id = f"mechanism_{index}"
        mechanism_ids[row["validation_id"]] = mechanism_id
        mechanism = row["mechanisms"][0]
        validation_rows.append({
            "validation_id": row["validation_id"],
            "mechanisms": [{
                "mechanism_id": mechanism_id,
                "tool": mechanism["tool"],
                "phase": mechanism["phase"],
                "direct_refs": list(dict.fromkeys(ref_map[item] for item in mechanism["basis_refs"])),
            }],
        })
    supported_scopes = {
        row["validation_id"]: row["scope_id"]
        for row in old["validation_scope_rows"]
        if row["assessment"] == "supported"
    }
    inspection = PlanInspectionV2.model_validate({
        "validation_rows": validation_rows,
        "validation_scope_rows": [{
            "scope_id": row["scope_id"],
            "validation_id": row["validation_id"],
            "mechanism_id": mechanism_ids[row["validation_id"]],
            "claim": row["procedure"],
            "direct_extra_refs": [],
            "status": row["assessment"],
        } for row in old["validation_scope_rows"]],
        "ac_validation_links": [
            {
                "criterion_id": coverage.criterion_id,
                "validation_id": validation_id,
                "scope_ids": [supported_scopes[validation_id]],
                "requirement_claim": f"{coverage.criterion_id}의 명시 검사 절차",
            }
            for coverage in plan.definition.goal_coverage
            for validation_id in coverage.validation_ids
        ],
        "constraint_task_rows": [{
            "constraint_id": row["constraint_id"],
            "task_ref": row["task_ref"],
            "applicability": row["applicability"],
            "required_validation_ids": row["validation_ids"],
        } for row in old["constraint_task_rows"]],
    })
    return plan, goal, project_map, catalog, inspection


def citation_ref_map(old: dict, citation_catalog) -> dict[str, str]:
    mapped: dict[str, str] = {}
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


def v2_table_from_v1(
        old: dict, coverage: dict[str, list[str]], citation_catalog,
) -> dict:
    ref_map = citation_ref_map(old, citation_catalog)
    fallback_ref = citation_catalog[0].citation_id

    def translated(values):
        return list(dict.fromkeys(ref_map.get(item, fallback_ref) for item in values))

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
        "validation_rows": [{
            "validation_id": row["validation_id"],
            "mechanisms": [{
                "mechanism_id": mechanism_ids[row["validation_id"]],
                "tool": mechanism["tool"],
                "phase": mechanism["phase"],
                "direct_refs": translated(mechanism["basis_refs"]),
            } for mechanism in row["mechanisms"]],
        } for row in old["validation_rows"]],
        "validation_scope_rows": [{
            "scope_id": row["scope_id"],
            "validation_id": row["validation_id"],
            "mechanism_id": mechanism_ids[row["validation_id"]],
            "claim": row["procedure"],
            "direct_extra_refs": [],
            "status": row["assessment"],
        } for row in old["validation_scope_rows"]],
        "ac_validation_links": [
            {
                "criterion_id": criterion_id,
                "validation_id": validation_id,
                "scope_ids": [supported_scopes[validation_id]],
                "requirement_claim": f"{criterion_id}의 명시 검사 절차",
            }
            for criterion_id, validation_ids in coverage.items()
            for validation_id in validation_ids
        ],
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
    citation_catalog = plan_inspection_citation_catalog_v2(catalog, project_map)
    return compile_plan_inspection_v2(
        inspection,
        findings=tuple(ReviewFindingV2.model_validate(item) for item in findings),
        plan=plan,
        goal=goal,
        project_map=project_map,
        evidence_catalog=catalog,
        citation_catalog=citation_catalog,
        target_catalog=plan_inspection_target_catalog_v2(goal, plan),
    )


def finding_targets(
        kind: str, *primary_refs: str, secondary_ref: str | None = None,
        plan=None, direct_extra_refs=(), direct_task_refs=(),
) -> dict:
    """테스트 finding의 의미 target 선택을 provider v2r6 계약으로 작성한다."""
    if kind in {"ac_validation", "constraint_task"}:
        if len(primary_refs) != 1 or secondary_ref is None:
            raise AssertionError("복합 target 테스트 입력에는 primary/secondary 참조가 하나씩 필요합니다.")
        base_plan, goal, *_ = v2_inputs()
        selected_plan = plan or base_plan
        target = next(
            item for item in plan_inspection_target_catalog_v2(goal, selected_plan)
            if item.kind == kind and item.primary_ref == primary_refs[0]
            and item.secondary_ref == secondary_ref
        )
        primary_target_ids = [target.target_id]
    else:
        if secondary_ref is not None:
            raise AssertionError("단일 target 테스트 입력에는 secondary 참조를 쓰지 않습니다.")
        primary_target_ids = list(primary_refs)
    return {
        "primary_target_ids": primary_target_ids,
        "direct_extra_refs": list(direct_extra_refs),
        "direct_task_refs": list(direct_task_refs),
    }


def owner_for(plan, goal, validation_id):
    return next(row["task_ref"] for row in validation_comparison_targets(goal, plan)["validations"]
                if row["validation_id"] == validation_id)


class PlanInspectionV2Tests(unittest.TestCase):
    def test_clean_compiles_repeated_closure_without_provider_repetition(self):
        plan, goal, _, _, inspection = v2_inputs()
        before = inspection.model_dump(mode="json")
        self.assertTrue(inspection.ac_validation_links)
        compiled = compile_fixture(inspection)
        self.assertTrue(any(row.ac_link_required for row in compiled.ac_validation_decisions))
        self.assertTrue(any(not row.ac_link_required for row in compiled.ac_validation_decisions))
        self.assertTrue(all(row.scope_ids if row.ac_link_required else not row.scope_ids
                            for row in compiled.ac_validation_decisions))
        ac_closures = [row for row in compiled.row_closures if row.row_kind == "ac_validation"]
        self.assertEqual(len(compiled.ac_validation_decisions), len(ac_closures))
        self.assertTrue(all(len(row.citation_ids) >= 3 for row in ac_closures))
        self.assertEqual((), compiled.derived_findings)
        self.assertEqual(before, inspection.model_dump(mode="json"))
        self.assertEqual(
            len(goal.definition.hard_acceptance) *
            len(validation_comparison_targets(goal, plan)["validations"]),
            len(compiled.membership_witnesses),
        )
        validation = inspection.validation_rows[0]
        selector = next(
            row["selector"] for row in validation_comparison_targets(goal, plan)["validations"]
            if row["validation_id"] == validation.validation_id
        ) + "/statement"
        claim = next(
            item for item in compiled.used_citations
            if item.source_ref == "artifact:plan_contract" and item.selector == selector
        )
        closure = next(
            row for row in compiled.row_closures
            if row.row_kind == "validation" and row.row_id == validation.validation_id
        )
        self.assertIn(claim.citation_id, closure.citation_ids)

    def test_citation_catalog_is_deterministic_exposes_only_registered_text_and_is_bound(self):
        plan, goal, project_map, catalog, inspection = v2_inputs()
        first = plan_inspection_citation_catalog_v2(catalog, project_map)
        second = plan_inspection_citation_catalog_v2(catalog, project_map)
        self.assertEqual(first, second)
        exposed_project_refs = {
            f"project:{entry.entry_id}" for entry in project_map.entries
            if entry.kind.value in {"reference", "instruction"}
        }
        observed_project_refs = {
            item.source_ref for item in first if item.source_ref.startswith("project:")
        }
        self.assertEqual(exposed_project_refs, observed_project_refs)
        self.assertNotIn("source:state", {item.source_ref for item in first})
        self.assertNotIn("source:project_map", {item.source_ref for item in first})
        self.assertFalse(any(item.selector.startswith("/source_traces") for item in first))
        goal_sources = {(item.selector, item.quote) for item in first
                        if item.source_ref == "source:goal"}
        for index, criterion in enumerate(goal.definition.hard_acceptance):
            self.assertIn((f"/hard_acceptance/{index}/statement", criterion.statement), goal_sources)
            self.assertIn(
                (f"/hard_acceptance/{index}/validation_intent", criterion.validation_intent),
                goal_sources,
            )
        for index, constraint in enumerate(goal.definition.constraints):
            self.assertIn((f"/constraints/{index}/statement", constraint.statement), goal_sources)
        self.assertTrue(all(item.citation_id.startswith("cite_") for item in first))

        machine_only_changes = deepcopy(catalog)
        machine_only_changes["source:state"]["facts"][0]["value"] = "sha256:" + "f" * 64
        machine_only_changes["source:project_map"]["revision_digest"] = "sha256:" + "e" * 64
        machine_only_changes["source:goal"]["source_traces"][0]["statement"] = "변경된 provenance 장부"
        self.assertEqual(
            first,
            plan_inspection_citation_catalog_v2(machine_only_changes, project_map),
        )

        changed = list(first)
        changed[0] = changed[0].model_copy(update={"citation_id": "cite_" + "f" * 32})
        with self.assertRaisesRegex(PlanInspectionError, "catalog 입력 결속 불일치"):
            compile_plan_inspection_v2(
                inspection,
                findings=(),
                plan=plan,
                goal=goal,
                project_map=project_map,
                evidence_catalog=catalog,
                citation_catalog=tuple(changed),
            )

    def test_missing_coverage_member_has_typed_witness_without_fake_quote(self):
        plan, goal, _, _, inspection = v2_inputs()
        row = next(item for item in compile_fixture(inspection).ac_validation_decisions
                   if item.ac_link_required)
        raw_plan = plan.model_dump(mode="json")
        coverage_index = next(i for i, item in enumerate(raw_plan["definition"]["goal_coverage"])
                              if item["criterion_id"] == row.criterion_id)
        raw_plan["definition"]["goal_coverage"][coverage_index]["validation_ids"].remove(row.validation_id)
        raw_plan["definition_digest"] = sha256_digest(raw_plan["definition"])
        changed_plan = PlanContractRevision.model_validate(raw_plan)
        findings = ({
            "finding_code": "MISSING_LINK",
            "defect_kind": "missing_validation_link",
            "remediable": True,
            **finding_targets(
                "ac_validation", row.criterion_id,
                secondary_ref=row.validation_id, plan=changed_plan,
            ),
        },)
        compiled = compile_fixture(inspection, findings, plan=changed_plan)
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

    def test_positive_links_require_supported_scope_and_known_pair(self):
        *_, inspection = v2_inputs()
        raw = inspection.model_dump(mode="json")
        link = raw["ac_validation_links"][0]
        scope = next(item for item in raw["validation_scope_rows"]
                     if item["scope_id"] == link["scope_ids"][0])
        scope["status"] = "unresolved"
        with self.assertRaisesRegex(PlanInspectionError, "supported scope"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

        raw = inspection.model_dump(mode="json")
        raw["ac_validation_links"][0]["criterion_id"] = "ac_unknown"
        with self.assertRaisesRegex(PlanInspectionError, "연결 ID 오류"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

        raw = inspection.model_dump(mode="json")
        raw["ac_validation_links"].append(dict(raw["ac_validation_links"][0]))
        with self.assertRaisesRegex(PlanInspectionError, "양의 AC 검사 연결 중복"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

        raw = inspection.model_dump(mode="json")
        link = raw["ac_validation_links"][0]
        link["scope_ids"] = [next(
            item["scope_id"] for item in raw["validation_scope_rows"]
            if item["validation_id"] != link["validation_id"] and item["status"] == "supported"
        )]
        with self.assertRaisesRegex(PlanInspectionError, "scope validation 불일치"):
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
            "remediable": True,
            **finding_targets(
                "constraint_task", row["constraint_id"], secondary_ref=row["task_ref"]
            ),
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
            "remediable": True,
            **finding_targets("validation_scope", scope.scope_id),
        }
        with self.assertRaisesRegex(PlanInspectionError, "supported scope에 finding"):
            compile_fixture(inspection, (finding,))

        raw = inspection.model_dump(mode="json")
        raw["validation_scope_rows"][0]["status"] = "contradicted"
        changed_scope_id = raw["validation_scope_rows"][0]["scope_id"]
        raw["ac_validation_links"] = [
            link for link in raw["ac_validation_links"]
            if changed_scope_id not in link["scope_ids"]
        ]
        with self.assertRaisesRegex(PlanInspectionError, "finding이 누락"):
            compile_fixture(PlanInspectionV2.model_validate(raw))

    def test_finding_affected_tasks_are_derived_from_targets(self):
        plan, goal, _, _, inspection = v2_inputs()
        validation_id = inspection.validation_rows[0].validation_id
        owner = owner_for(plan, goal, validation_id)
        self.assertIsNotNone(owner)
        finding = {
            "finding_code": "ORDER_ERROR",
            "defect_kind": "result_order",
            "remediable": True,
            **finding_targets("validation", validation_id),
        }
        compiled = compile_fixture(inspection, (finding,))
        self.assertEqual((owner,), compiled.derived_findings[0].affected_task_refs)

    def test_finding_kind_rejects_incompatible_target_kind(self):
        *_, inspection = v2_inputs()
        finding = {
            "finding_code": "WRONG_TARGET",
            "defect_kind": "missing_validation_link",
            "remediable": True,
            **finding_targets(
                "validation_scope", inspection.validation_scope_rows[0].scope_id
            ),
        }
        with self.assertRaisesRegex(PlanInspectionError, "target ID 종류 불일치"):
            compile_fixture(inspection, (finding,))

    def test_multi_target_closure_unions_direct_sources(self):
        plan, goal, _, _, inspection = v2_inputs()
        validation_ids = [row.validation_id for row in inspection.validation_rows[:2]]
        owner = owner_for(plan, goal, validation_ids[0])
        finding = {
            "finding_code": "MULTI_ORDER",
            "defect_kind": "result_order",
            "remediable": True,
            **finding_targets("validation", *validation_ids),
        }
        compiled = compile_fixture(inspection, (finding,))
        closure = next(row for row in compiled.row_closures if row.row_id == "MULTI_ORDER")
        validation_refs = set().union(*(
            set(row.citation_ids) for row in compiled.row_closures
            if row.row_kind == "validation" and row.row_id in validation_ids
        ))
        self.assertTrue(validation_refs <= set(closure.citation_ids))

    def test_project_citation_maps_only_to_project_map_evidence(self):
        plan, goal, project_map, _, inspection = v2_inputs()
        entry = next(item for item in project_map.entries if item.kind.value == "reference")
        from pathlib import Path
        evidence_catalog = plan_review_evidence_catalog(plan, goal, inputs("clean")[2], project_map)
        citation_catalog = plan_inspection_citation_catalog_v2(evidence_catalog, project_map)
        project_ref = next(
            item.citation_id for item in citation_catalog
            if item.source_ref == f"project:{entry.entry_id}" and item.selector == "/content"
        )
        raw = inspection.model_dump(mode="json")
        validation_id = raw["validation_rows"][0]["validation_id"]
        raw["validation_rows"][0]["mechanisms"][0]["direct_refs"].append(project_ref)
        changed = PlanInspectionV2.model_validate(raw)
        finding = {
            "finding_code": "PROJECT_ORDER",
            "defect_kind": "result_order",
            "remediable": True,
            **finding_targets("validation", validation_id),
        }
        compiled = compile_fixture(changed, (finding,))
        self.assertIn("source:project_map", compiled.derived_findings[0].evidence_refs)
        self.assertNotIn(f"project:{entry.entry_id}", compiled.derived_findings[0].evidence_refs)

    def test_other_finding_preserves_direct_classification_and_citation_targets(self):
        plan, current_goal, project_map, catalog, inspection = v2_inputs()
        goal_value = current_goal.definition.observable_outcome
        citation_catalog = plan_inspection_citation_catalog_v2(catalog, project_map)
        goal_ref = next(
            item.citation_id for item in citation_catalog
            if item.source_ref == "source:goal" and item.selector == "/observable_outcome"
            and item.quote == goal_value
        )
        finding = {
            "finding_code": "GOAL_OUTCOME_CONTRACT_MISMATCH",
            "defect_kind": "other",
            "gate": "grounding",
            "severity": "error",
            "remediable": True,
            **finding_targets(
                "citation", goal_ref,
                direct_task_refs=(plan.definition.tasks[0].task_ref,),
            ),
        }
        compiled = compile_fixture(inspection, (finding,))
        derived = compiled.derived_findings[0]
        self.assertEqual("grounding", derived.gate.value)
        self.assertEqual("error", derived.severity.value)
        self.assertEqual(("source:goal",), derived.evidence_refs)
        self.assertEqual((plan.definition.tasks[0].task_ref,), derived.affected_task_refs)

    def test_standard_and_other_finding_classification_boundaries_are_strict(self):
        common = {
            "finding_code": "CLASSIFICATION_BOUNDARY",
            "remediable": True,
            **finding_targets("validation", "val_example"),
        }
        with self.assertRaisesRegex(ValueError, "표준 finding"):
            ReviewFindingV2.model_validate(
                common | {"defect_kind": "result_order", "gate": "execution", "severity": "error"}
            )
        with self.assertRaisesRegex(ValueError, "other finding"):
            ReviewFindingV2.model_validate(
                common | {
                    "defect_kind": "other",
                    **finding_targets("citation", "citation_example"),
                }
            )

    def test_target_catalog_is_deterministic_and_exactly_bound(self):
        plan, goal, project_map, catalog, inspection = v2_inputs()
        first = plan_inspection_target_catalog_v2(goal, plan)
        self.assertEqual(first, plan_inspection_target_catalog_v2(goal, plan))
        self.assertEqual(len(first), len({item.target_id for item in first}))
        self.assertTrue(all(item.target_id.startswith("target_") for item in first))

        changed = list(first)
        changed[0] = changed[0].model_copy(update={"target_id": "target_" + "f" * 24})
        citation_catalog = plan_inspection_citation_catalog_v2(catalog, project_map)
        with self.assertRaisesRegex(PlanInspectionError, "target catalog 입력 결속 불일치"):
            compile_plan_inspection_v2(
                inspection,
                findings=(),
                plan=plan,
                goal=goal,
                project_map=project_map,
                evidence_catalog=catalog,
                citation_catalog=citation_catalog,
                target_catalog=tuple(changed),
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
            citation_catalog = plan_inspection_citation_catalog_v2({
                "source:goal": current_goal.definition.model_dump(mode="json"),
                "source:state": current_state.model_dump(mode="json"),
                "source:project_map": current_map.model_dump(mode="json"),
            }, current_map)
            envelope = {
                "inspection": v2_table_from_v1(old, coverage, citation_catalog),
                "plan": draft,
            }
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
            self.assertIn(PLAN_VALIDATION_TRACE_V2_INSTRUCTIONS, request.instructions)
            for v1_field in (
                "citations에 원문", "basis_refs", "claim_ref", "affected_task_refs",
                "ac_validation_rows",
            ):
                self.assertNotIn(v1_field, request.instructions)
            self.assertIn("기계 메타데이터를 서로 비교해 semantic finding을 만들지 않는다", request.instructions)
            self.assertIn("검사 책임을 열거하면", request.instructions)
            self.assertIn("direct_extra_refs", str(request.output_schema))
            self.assertNotIn("finding_links", str(request.output_schema))
            inspection_properties = request.output_schema["$defs"]["PlanInspectionV2"]["properties"]
            self.assertNotIn("citations", inspection_properties)
            self.assertNotIn("ac_validation_rows", inspection_properties)
            self.assertNotIn(
                "claim_ref", request.output_schema["$defs"]["ValidationInspectionV2"]["properties"]
            )
            self.assertNotIn(
                "claim_ref", request.output_schema["$defs"]["ValidationScopeInspectionV2"]["properties"]
            )
            self.assertEqual(
                {"scope_id", "validation_id", "mechanism_id", "claim",
                 "direct_extra_refs", "status"},
                set(request.output_schema["$defs"]["ValidationScopeInspectionV2"]["properties"]),
            )
            self.assertIn("ac_validation_links", inspection_properties)
            self.assertEqual(
                {"criterion_id", "validation_id", "scope_ids", "requirement_claim"},
                set(request.output_schema["$defs"]["ACValidationLinkInspectionV2"]["properties"]),
            )
            expected_catalog = plan_inspection_citation_catalog_v2(
                {
                    "source:goal": request.payload["goal"],
                    "source:state": request.payload["state"],
                    "source:project_map": request.payload["project_map"],
                    "artifact:skeleton": request.payload["skeleton"],
                },
                current_map,
            )
            self.assertEqual(
                [item.model_dump(mode="json") for item in expected_catalog],
                request.payload["inspection_citation_catalog"],
            )
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
        self.assertNotIn("target_refs", str(request.output_schema))
        self.assertNotIn("InspectionTargetV2", str(request.output_schema))
        finding_properties = request.output_schema["properties"]["review"]["anyOf"][1][
            "properties"
        ]["findings"]["items"]["properties"]
        self.assertNotIn("evidence_refs", finding_properties)
        self.assertNotIn("summary", finding_properties)
        self.assertNotIn("affected_task_refs", finding_properties)
        self.assertEqual(
            {
                "finding_code", "defect_kind", "gate", "severity", "remediable",
                "primary_target_ids", "direct_extra_refs", "direct_task_refs",
            },
            set(finding_properties),
        )
        self.assertEqual(
            [item.model_dump(mode="json")
             for item in plan_inspection_target_catalog_v2(current_goal, plan)],
            request.payload["inspection_target_catalog"],
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
