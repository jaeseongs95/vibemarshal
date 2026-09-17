"""기존 Core 경계 회귀용 비의미적 대조표 fixture. 의미 검출의 증명이 아니다."""
from copy import deepcopy

from flowmarshal.engine.roles import ScriptedStructuredRoleRunner


def inspection_fixture(plan: dict, goal: dict, *, revision: bool = False, review: dict | None = None) -> dict:
    definition = plan["definition"] if revision else plan
    prefix = "/definition" if revision else ""
    plan_ref = "artifact:plan_contract" if revision else "artifact:plan_draft"
    citations = []

    def cite(source, selector, text):
        ref = f"c{len(citations)}"
        citations.append({"citation_id": ref, "source_ref": source, "selector": selector, "quote": text})
        return ref

    ac_refs = {
        ac["criterion_id"]: (
            cite("source:goal", f"/hard_acceptance/{i}/statement", ac["statement"]),
            cite("source:goal", f"/hard_acceptance/{i}/validation_intent", ac["validation_intent"]),
        )
        for i, ac in enumerate(goal["hard_acceptance"])
    }
    validations = [(task["task_ref"], validation, f"{prefix}/tasks/{ti}/validations/{vi}")
                   for ti, task in enumerate(definition["tasks"]) for vi, validation in enumerate(task["validations"])]
    validations += [(None, validation, f"{prefix}/integration_validations/{vi}")
                    for vi, validation in enumerate(definition["integration_validations"])]
    val_refs = {validation["validation_id"]: cite(plan_ref, path + "/statement", validation["statement"])
                for _, validation, path in validations}
    constraint_refs = {constraint["constraint_id"]: cite("source:goal", f"/constraints/{i}/statement", constraint["statement"])
                       for i, constraint in enumerate(goal.get("constraints", []))}
    links = []
    for finding in (review or {}).get("findings", []):
        basis = []
        if "source:goal" in finding["evidence_refs"]:
            basis.append(next(iter(ac_refs.values()))[0])
        if "artifact:plan_contract" in finding["evidence_refs"]:
            basis.append(next(iter(val_refs.values())))
        links.append({"finding_code": finding["finding_code"], "defect_kind": "other",
                      "criterion_ids": [], "validation_ids": [], "task_refs": finding.get("affected_task_refs", []),
                      "basis_refs": basis})
    return {
        "citations": citations,
        "ac_validation_rows": [{"criterion_id": ac, "validation_id": vid, "ac_link_required": False,
                                "scope_ids": [],
                                "basis_refs": [*arefs, vref], "finding_codes": []}
                               for ac, arefs in ac_refs.items() for vid, vref in val_refs.items()],
        "constraint_task_rows": [{"constraint_id": cid, "task_ref": task["task_ref"], "applicability": "not_applicable",
                                  "validation_ids": [], "basis_refs": [ref], "finding_codes": []}
                                 for cid, ref in constraint_refs.items() for task in definition["tasks"]],
        "validation_rows": [{"validation_id": vid, "claim_ref": ref,
                             "mechanisms": [{"tool": "합성 검사 책임", "phase": None, "basis_refs": [ref]}],
                             "separate_check_refs": []}
                            for vid, ref in val_refs.items()],
        "validation_scope_rows": [{"scope_id": f"scope_{index}", "validation_id": vid,
                                   "claim_ref": ref, "procedure": "합성 검사 책임", "phase": None,
                                   "basis_refs": [ref], "assessment": "supported", "finding_codes": []}
                                  for index, (vid, ref) in enumerate(val_refs.items())],
        "finding_links": links,
    }


class InspectionScriptedRunner(ScriptedStructuredRoleRunner):
    """이전 회귀의 scripted 의미 판단을 명시적 새 envelope에 담는 테스트 전용 fixture."""

    def run(self, request, *, validator=None):
        responses = self.responses.get(request.role, [])
        if responses:
            raw = deepcopy(responses[0])
            if request.role == "plan_expander" and "inspection" not in raw:
                responses[0] = {"plan": raw, "inspection": inspection_fixture(raw, request.payload["goal"])}
            elif request.role in {"compact_plan_reviewer", "critical_effect_reviewer", "high_risk_reviewer", "external_effect_reviewer", "recovery_plan_reviewer"} and "inspection" not in raw:
                catalog = request.payload["evidence_catalog"]
                responses[0] = {"review": raw, "inspection": inspection_fixture(
                    catalog["artifact:plan_contract"], catalog["source:goal"], revision=True, review=raw)}
        return super().run(request, validator=validator)
