"""R-S06-15의 AC 연결 필수성 v5 fixture를 v4에서 독립 파생한다.

v4 기대값·독립 검토·원시 실행은 읽기만 하며 새 v5 파일만 x 모드로 쓴다.
관계 provenance를 합격 기준으로 재사용하지 않고, 기존 직접 검토 결과에서
``explicit_procedure``였던 행만 ``ac_link_required=true``로 투영한다.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

from flowmarshal.canonical import json_value, sha256_bytes, sha256_digest
from flowmarshal.engine.domain import PlanContractRevision


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/engine"
V4_EXPECTATIONS = FIXTURES / "plan-inspection-v4-expectations.json"
V4_REVIEW = FIXTURES / "plan-inspection-v4-independent-fixture-review.json"
V5_EXPECTATIONS = FIXTURES / "plan-inspection-v5-expectations.json"
V5_REVIEW = FIXTURES / "plan-inspection-v5-independent-fixture-review.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _convert_row(row: dict[str, Any]) -> dict[str, Any]:
    converted = deepcopy(row)
    relation = converted.pop("relation")
    converted["ac_link_required"] = relation == "explicit_procedure"
    converted.pop("required_goal_coverage_links", None)
    converted.pop("optional_connection_allowed", None)
    return converted


def _expectations() -> dict[str, Any]:
    value = _read(V4_EXPECTATIONS)
    value["parent_fixture_revision"] = value["fixture_revision"]
    value["parent_expectations_byte_digest"] = sha256_bytes(V4_EXPECTATIONS.read_bytes())
    value["fixture_revision"] = "plan-inspection-v5-r-s06-15"
    value["ac_validation_rows"] = [_convert_row(row) for row in value["ac_validation_rows"]]
    value["case_ac_validation_rows"] = {
        case_id: [_convert_row(row) for row in rows]
        for case_id, rows in value["case_ac_validation_rows"].items()
    }
    value["normalization_assertions"] = [_convert_row(row) for row in value["normalization_assertions"]]
    for mutation in value["normalizations"]:
        mutation["add_integration_criterion_refs"] = [
            pair for pair in mutation.get("add_goal_coverage_links", []) if pair[1].startswith("val_goal_")
        ]
    for mutation in value["derived_cases"]:
        mutation["remove_integration_criterion_refs"] = [
            pair for pair in mutation.get("remove_goal_coverage_links", []) if pair[1].startswith("val_goal_")
        ]
    scope = value["semantic_evaluation_scope"]
    scope["fixed_case_ac_link_tables"] = scope.pop("fixed_case_relation_tables")
    scope["fixed_case_ac_link_tables"] = scope["fixed_case_ac_link_tables"].replace("관계", "AC 연결 필수성")
    value["r_s06_15_provenance"] = {
        "parent_expectations": V4_EXPECTATIONS.name,
        "parent_expectations_byte_digest": sha256_bytes(V4_EXPECTATIONS.read_bytes()),
        "parent_independent_review": V4_REVIEW.name,
        "parent_independent_review_byte_digest": sha256_bytes(V4_REVIEW.read_bytes()),
        "r_s06_14_summary_byte_digest": "sha256:8b15196685080a00c03ab7a33299597bac695bda52b2435220a421639bbb986d",
        "r_s06_14_result_byte_digest": "sha256:66a964cf1f0425ec6833c9c0aa9c5bd21af67d52ad65acd836fab3bc4582875d",
        "r_s06_14_receipt_canonical_digest": "sha256:de1537a319743f800ea1c5ab61c3f1ab572937d6c294a3735c43e31e984a1b49",
        "contract_change": "AC×validation의 ac_link_required bool만 hard 의미 atom으로 평가하고 실제 link는 Plan goal_coverage에서 계산한다.",
    }
    return value


def _portable_source() -> dict[str, Any]:
    files = deepcopy(_read(FIXTURES / "plan-inspection-regressions.json")["files"])
    files.update(deepcopy(_read(FIXTURES / "plan-inspection-v2-source-inputs.json")))
    semantic = deepcopy(files["input-semantic-explicit-plan.json"])
    next(row for row in semantic["definition"]["goal_coverage"] if row["criterion_id"] == "ac_001")[
        "validation_ids"
    ].remove("val_task_validator_review")
    semantic["definition_digest"] = sha256_digest(semantic["definition"])
    files["input-semantic-missing-link-plan.json"] = semantic
    files["input-stored-multi-defect-plan.json"] = deepcopy(files["expanded-plan.json"])
    return files


def _coverage(plan: dict[str, Any], criterion_id: str) -> list[str]:
    return next(row["validation_ids"] for row in plan["definition"]["goal_coverage"]
                if row["criterion_id"] == criterion_id)


def _integration(plan: dict[str, Any], validation_id: str) -> dict[str, Any]:
    return next(row for row in plan["definition"]["integration_validations"]
                if row["validation_id"] == validation_id)


def _mutate(plan: dict[str, Any], mutation: dict[str, Any]) -> dict[str, Any]:
    revised = deepcopy(plan)
    for criterion_id, validation_id in mutation.get("add_goal_coverage_links", []):
        _coverage(revised, criterion_id).append(validation_id)
    for criterion_id, validation_id in mutation.get("remove_goal_coverage_links", []):
        _coverage(revised, criterion_id).remove(validation_id)
    for criterion_id, validation_id in mutation.get("add_integration_criterion_refs", []):
        _integration(revised, validation_id)["criterion_refs"].append(criterion_id)
    for criterion_id, validation_id in mutation.get("remove_integration_criterion_refs", []):
        _integration(revised, validation_id)["criterion_refs"].remove(criterion_id)
    for validation_id in mutation.get("remove_task_validations", []):
        for task in revised["definition"]["tasks"]:
            task["validations"] = [row for row in task["validations"] if row["validation_id"] != validation_id]
    for validation_id, statement in mutation.get("replace_task_validation_statements", []):
        next(row for task in revised["definition"]["tasks"] for row in task["validations"]
             if row["validation_id"] == validation_id)["statement"] = statement
    revised["definition_digest"] = sha256_digest(revised["definition"])
    _verify_consistency(revised)
    return revised


def _verify_consistency(plan: dict[str, Any]) -> None:
    expected = {row["validation_id"]: set(row["criterion_refs"])
                for row in plan["definition"]["integration_validations"]}
    actual = {validation_id: set() for validation_id in expected}
    for coverage in plan["definition"]["goal_coverage"]:
        for validation_id in coverage["validation_ids"]:
            if validation_id in actual:
                actual[validation_id].add(coverage["criterion_id"])
    if actual != expected:
        raise RuntimeError("INTEGRATION_CRITERION_COVERAGE_MISMATCH")


def _plans(expectations: dict[str, Any]) -> dict[str, Any]:
    working = _portable_source()
    for mutation in expectations["normalizations"]:
        working[mutation["input"]] = _mutate(working[mutation["input"]], mutation)
    working["input-historical-r-s06-09-clean-plan.json"] = deepcopy(
        _portable_source()["input-clean-plan.json"]
    )
    for mutation in expectations["derived_cases"]:
        working[mutation["input"]] = _mutate(working[mutation["base_input"]], mutation)
    for name, plan in working.items():
        if name.endswith("-plan.json"):
            _verify_consistency(plan)
    return working


def _review(expectations: dict[str, Any]) -> dict[str, Any]:
    value = _read(V4_REVIEW)
    value["prior_fixture_revision"] = value["fixture_revision"]
    value["parent_independent_review_byte_digest"] = sha256_bytes(V4_REVIEW.read_bytes())
    value["fixture_revision"] = expectations["fixture_revision"]
    value["semantic_evaluation_scope"] = expectations["semantic_evaluation_scope"]
    for row in value.get("focused_row_decisions", []):
        relation = row.get("independent_v3_relation")
        row["prior_relation"] = relation
        row["ac_link_required"] = relation == "explicit_procedure"
    plans = _plans(expectations)
    value["case_plan_contract_digests"] = {
        case_id: sha256_digest(
            PlanContractRevision.model_validate(plans[case["input"]]).model_dump(mode="json")
        )
        for case_id, case in value["case_reviews"].items()
    }
    legacy = _read(FIXTURES / "plan-inspection-expectations.json")
    runtime = expectations | (legacy | expectations["revision_review_expectations"])
    runtime_bytes = (
        json.dumps(json_value(runtime), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    value["expectations_digest"] = sha256_bytes(runtime_bytes)
    value["r_s06_15_provenance"] = expectations["r_s06_15_provenance"]
    return value


def main() -> None:
    expectations = _expectations()
    review = _review(expectations)
    _write_new(V5_EXPECTATIONS, expectations)
    _write_new(V5_REVIEW, review)
    print(json.dumps({
        "fixture_revision": expectations["fixture_revision"],
        "expectations_digest": sha256_bytes(V5_EXPECTATIONS.read_bytes()),
        "independent_review_digest": sha256_bytes(V5_REVIEW.read_bytes()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
