"""R-S06-10의 고정 검사 fixture revision을 만든다.

과거 R-S06-09 실행은 감사 기준으로만 읽는다. 여기서는 원문 문자열을
추론해 일반적인 의미 oracle을 만들지 않고, 고정한 criterion/validation ID와
사전에 기록한 근거에 따라서만 새 입력을 파생한다.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from typing import Any

from flowmarshal.canonical import json_value, sha256_bytes, sha256_digest
from flowmarshal.engine.domain import PlanContractRevision


ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS_PATH = ROOT / "tests/fixtures/engine/plan-inspection-v4-expectations.json"
INDEPENDENT_REVIEW_PATH = ROOT / "tests/fixtures/engine/plan-inspection-v4-independent-fixture-review.json"
RAW_REJECTED_MANIFEST_PATH = ROOT / "tests/fixtures/engine/r-s06-12-raw-rejected/manifest.json"
SYNTHETIC_NORMAL_FIXTURE_PATH = ROOT / "tests/fixtures/engine/plan-inspection-r-s06-13-synthetic-normal.json"
LEGACY_EXPECTATIONS_PATH = ROOT / "tests/fixtures/engine/plan-inspection-expectations.json"
SOURCE_INPUT_FILENAMES = (
    "input-bad-plan.json",
    "input-boundary-clean-plan.json",
    "input-clean-plan.json",
    "input-combined-plan.json",
    "input-future-result-plan.json",
    "input-goal.json",
    "input-missing-link-plan.json",
    "input-project-map.json",
    "input-semantic-explicit-goal.json",
    "input-semantic-explicit-plan.json",
    "input-semantic-explicit-state.json",
    "input-semantic-missing-link-plan.json",
    "input-skeleton.json",
    "input-state.json",
    "input-stored-expanded-plan.json",
    "input-stored-multi-defect-plan.json",
    "input-wrong-goal-plan.json",
)


class FixtureRevisionError(RuntimeError):
    """고정 fixture 원본이나 파생 규칙이 예상과 다를 때 발생한다."""


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _copy_new(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream)
        output_stream.flush()
        os.fsync(output_stream.fileno())


def _coverage(plan: dict[str, Any], criterion_id: str) -> list[str]:
    rows = [row for row in plan["definition"]["goal_coverage"] if row["criterion_id"] == criterion_id]
    if len(rows) != 1:
        raise FixtureRevisionError(f"criterion_id가 하나여야 합니다: {criterion_id}")
    return rows[0]["validation_ids"]


def _add_link(plan: dict[str, Any], criterion_id: str, validation_id: str) -> None:
    validation_ids = _coverage(plan, criterion_id)
    if validation_id in validation_ids:
        raise FixtureRevisionError(f"이미 존재하는 AC 연결입니다: {criterion_id} -> {validation_id}")
    validation_ids.append(validation_id)


def _remove_link(plan: dict[str, Any], criterion_id: str, validation_id: str) -> None:
    validation_ids = _coverage(plan, criterion_id)
    if validation_id not in validation_ids:
        raise FixtureRevisionError(f"제거할 AC 연결이 없습니다: {criterion_id} -> {validation_id}")
    validation_ids.remove(validation_id)


def _remove_task_validation(plan: dict[str, Any], validation_id: str) -> None:
    tasks = plan["definition"]["tasks"]
    matches = [(task, index) for task in tasks for index, validation in enumerate(task["validations"])
               if validation["validation_id"] == validation_id]
    if len(matches) != 1:
        raise FixtureRevisionError(f"Task validation이 하나여야 합니다: {validation_id}")
    task, index = matches[0]
    task["validations"].pop(index)


def _replace_validation_statement(plan: dict[str, Any], validation_id: str, statement: str) -> None:
    matches = [validation for task in plan["definition"]["tasks"] for validation in task["validations"]
               if validation["validation_id"] == validation_id]
    if len(matches) != 1:
        raise FixtureRevisionError(f"바꿀 Task validation이 하나여야 합니다: {validation_id}")
    matches[0]["statement"] = statement


def _refresh_and_validate(plan: dict[str, Any]) -> None:
    plan["definition_digest"] = sha256_digest(plan["definition"])
    PlanContractRevision.model_validate(plan)


def _apply_mutation(plan: dict[str, Any], mutation: dict[str, Any]) -> dict[str, Any]:
    revised = deepcopy(plan)
    for criterion_id, validation_id in mutation.get("add_goal_coverage_links", []):
        _add_link(revised, criterion_id, validation_id)
    for criterion_id, validation_id in mutation.get("remove_goal_coverage_links", []):
        _remove_link(revised, criterion_id, validation_id)
    for validation_id in mutation.get("remove_task_validations", []):
        _remove_task_validation(revised, validation_id)
    for validation_id, statement in mutation.get("replace_task_validation_statements", []):
        _replace_validation_statement(revised, validation_id, statement)
    _refresh_and_validate(revised)
    return revised


def _require_source_inputs(source_run: Path) -> None:
    missing = [name for name in SOURCE_INPUT_FILENAMES if not (source_run / name).is_file()]
    if missing:
        raise FixtureRevisionError("R-S06-09 입력이 없습니다: " + ", ".join(missing))


def _verify_source_input_digests(source_run: Path, expectations: dict[str, Any]) -> None:
    expected = expectations["source_input_canonical_digests"]
    if set(expected) != set(SOURCE_INPUT_FILENAMES):
        raise FixtureRevisionError("source_input_canonical_digests가 R-S06-09 전체 입력을 고정하지 않았습니다.")
    actual = {filename: sha256_digest(_read(source_run / filename)) for filename in SOURCE_INPUT_FILENAMES}
    changed = [filename for filename in SOURCE_INPUT_FILENAMES if actual[filename] != expected[filename]]
    if changed:
        raise FixtureRevisionError("R-S06-09 고정 입력 digest가 다릅니다: " + ", ".join(changed))


def _runtime_review_expectations(expectations: dict[str, Any]) -> dict[str, Any]:
    """기존 evaluator가 쓰는 case -> defect 배열을 새 metadata와 분리해 조립한다."""
    if sha256_bytes(LEGACY_EXPECTATIONS_PATH.read_bytes()) != expectations["legacy_expectations_digest"]:
        raise FixtureRevisionError("과거 plan-inspection 기대값이 변경됐습니다.")
    legacy = _read(LEGACY_EXPECTATIONS_PATH)
    revised = expectations["revision_review_expectations"]
    overlap = set(legacy) & set(revised)
    if overlap:
        raise FixtureRevisionError("과거 기대값을 새 revision이 덮어쓰려 합니다: " + ", ".join(sorted(overlap)))
    return legacy | revised


def _independent_fixture_review(
    expectations: dict[str, Any], runtime_expectations: dict[str, Any],
) -> dict[str, Any]:
    """사전 독립 원문 대조의 고정 입력을 새 run에 그대로 결속한다."""
    review = _read(INDEPENDENT_REVIEW_PATH)
    expected_cases = expectations["provider_call_order"][:-2]
    runtime_bytes = (
        json.dumps(json_value(expectations | runtime_expectations), ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if (
        review.get("review_complete") is not True
        or review.get("reviewed_cases") != expected_cases
        or review.get("expectations_digest") != sha256_bytes(runtime_bytes)
    ):
        raise FixtureRevisionError("독립 fixture review의 case·기대값 결속이 다릅니다.")
    provenance = expectations.get("r_s06_13_provenance")
    if not isinstance(provenance, dict) or provenance.get("raw_rejection_fixture") != "r-s06-12-raw-rejected/manifest.json":
        raise FixtureRevisionError("R-S06-13 원시 거부 fixture provenance가 없습니다.")
    if not RAW_REJECTED_MANIFEST_PATH.is_file() or not SYNTHETIC_NORMAL_FIXTURE_PATH.is_file():
        raise FixtureRevisionError("R-S06-13 원시 거부 또는 정상 합성 fixture가 없습니다.")
    raw_manifest = _read(RAW_REJECTED_MANIFEST_PATH)
    normal = _read(SYNTHETIC_NORMAL_FIXTURE_PATH)
    if (
        raw_manifest.get("expected_status") != "FAIL"
        or raw_manifest.get("files", {}).get("summary.json") != provenance.get("raw_summary_byte_digest")
        or normal.get("provenance", {}).get("relation_rows_selector") != "/expected_ac_validation_rows"
        or normal.get("provenance", {}).get("expectation_revision") != expectations.get("fixture_revision")
        or review.get("r_s06_13_provenance", {}).get("expectations_path") != EXPECTATIONS_PATH.name
    ):
        raise FixtureRevisionError("R-S06-13 provenance 또는 직접 selector 결속이 다릅니다.")
    return review


def verify_reviewed_case(
    case_id: str, payload: dict[str, Any], expectations: dict[str, Any], review: dict[str, Any],
) -> None:
    """사전 검토한 문장·소유 단계·method·mode와 직접 인용을 실제 요청에 재대조한다."""
    if sha256_digest(payload["evidence_catalog"]["source:goal"]) != review["case_goal_contract_digests"][case_id]:
        raise FixtureRevisionError("CASE_REVIEWED_GOAL_CONTRACT_MISMATCH")
    if sha256_digest(payload["evidence_catalog"]["artifact:plan_contract"]) != review["case_plan_contract_digests"][case_id]:
        raise FixtureRevisionError("CASE_REVIEWED_PLAN_CONTRACT_MISMATCH")
    expected = expectations["case_validation_scope_rows"][case_id]
    actual = payload["validation_scope_rows"]
    if len(expected) != len(actual):
        raise FixtureRevisionError("CASE_REVIEWED_VALIDATION_SET_MISMATCH")
    for checked, provided in zip(expected, actual, strict=True):
        projected = {"validation_id": provided["validation_id"], "statement": provided["statement"],
                     "owner_scope": "goal" if provided["scope"] == "integration" else "task",
                     "owner_task_ref": provided["task_ref"], "method": provided["method"],
                     "evidence_mode": provided["evidence_mode"]}
        if any(checked[key] != value for key, value in projected.items()):
            raise FixtureRevisionError("CASE_REVIEWED_VALIDATION_CONTRACT_MISMATCH")
    sources = dict(payload["evidence_catalog"]) | {
        ref: value for ref, value in payload["inspection_source_catalog"].items() if ref.startswith("project:")
    }
    rows = expectations["case_ac_validation_rows"][case_id] + expected
    for ref in {ref for row in rows for ref in row["basis_refs"]}:
        citation = review["citations"][ref]
        source = sources[citation["source_ref"]]
        selected = source
        for token in citation["selector"][1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            selected = selected[int(token)] if isinstance(selected, (list, tuple)) else selected[token]
        if not isinstance(selected, str) or citation["quote"] not in selected:
            raise FixtureRevisionError("CASE_REVIEWED_CITATION_MISMATCH")
        if "content_digest" in citation and source.get("content_digest") != citation["content_digest"]:
            raise FixtureRevisionError("CASE_REVIEWED_SOURCE_DIGEST_MISMATCH")


def build_revision(source_run: Path, destination: Path) -> None:
    """R-S06-09 입력을 읽어 R-S06-10 전용 정상·결함 fixture를 x-쓰기한다.

    기존 run과 그 원시 응답은 수정하지 않는다. destination은 새 디렉터리여야 하며,
    일부 파일이 이미 있으면 ``x`` 모드가 즉시 중단시켜 잠금 전 덮어쓰기를 막는다.
    """
    source_run = source_run.resolve()
    destination = destination.resolve()
    _require_source_inputs(source_run)
    expectations = _read(EXPECTATIONS_PATH)
    _verify_source_input_digests(source_run, expectations)
    runtime_expectations = _runtime_review_expectations(expectations)
    independent_review = _independent_fixture_review(expectations, runtime_expectations)

    originals: dict[str, Any] = {}
    working: dict[str, Any] = {}
    for filename in SOURCE_INPUT_FILENAMES:
        source = source_run / filename
        _copy_new(source, destination / f"source-{filename}")
        value = _read(source)
        originals[filename] = value
        working[filename] = deepcopy(value)

    for mutation in expectations["normalizations"]:
        filename = mutation["input"]
        if filename not in working:
            raise FixtureRevisionError(f"정상화 원본 입력이 없습니다: {filename}")
        working[filename] = _apply_mutation(working[filename], mutation)

    for filename, value in working.items():
        _write_new(destination / filename, value)

    historical_name = "input-historical-r-s06-09-clean-plan.json"
    _write_new(destination / historical_name, originals["input-clean-plan.json"])

    derived_digests: dict[str, str] = {}
    for case in expectations["derived_cases"]:
        base_input = case["base_input"]
        if base_input not in working:
            raise FixtureRevisionError(f"파생 fixture의 base 입력이 없습니다: {base_input}")
        derived = _apply_mutation(working[base_input], case)
        output = case["input"]
        _write_new(destination / output, derived)
        derived_digests[output] = derived["definition_digest"]

    if (source_run / "expanded-plan.json").is_file():
        _copy_new(source_run / "expanded-plan.json", destination / "source-expanded-plan.json")
    if (source_run / "raw-clean-assessment.json").is_file():
        _copy_new(source_run / "raw-clean-assessment.json", destination / "source-raw-clean-assessment.json")
    _write_new(destination / "expectations.json", expectations | runtime_expectations)
    _write_new(destination / "independent-fixture-review.json", independent_review)

    normalized_digests = {
        filename: value["definition_digest"]
        for filename, value in working.items()
        if filename.endswith("-plan.json")
    }
    assessment = {
        "fixture_revision": expectations["fixture_revision"],
        "source_run": source_run.name,
        "source_input_canonical_digests": expectations["source_input_canonical_digests"],
        "source_input_byte_digests": {filename: sha256_bytes((source_run / filename).read_bytes())
                                      for filename in SOURCE_INPUT_FILENAMES},
        "historical_counterexamples": expectations["historical_counterexamples"],
        "normalization_assertions": expectations["normalization_assertions"],
        "case_expectations": expectations["cases"],
        "normalized_definition_digests": normalized_digests,
        "derived_definition_digests": derived_digests,
        "expectations_digest": sha256_bytes(EXPECTATIONS_PATH.read_bytes()),
        "runtime_review_expectation_ids": {case_id: [item["defect_id"] for item in defects]
                                           for case_id, defects in runtime_expectations.items()},
        "historical_clean_input": historical_name,
        "provider_call_order": expectations["provider_call_order"],
        "deterministic_only_cases": expectations["deterministic_only_cases"],
    }
    _write_new(destination / "fixture-assessment.json", assessment)
