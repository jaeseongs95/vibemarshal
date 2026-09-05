"""독립 plan-inspection 입력 package를 새 진단 run에 결속한다."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

from flowmarshal.canonical import canonical_json, sha256_bytes, sha256_digest
from flowmarshal.engine.domain import (
    GoalContractRevision,
    PlanContractRevision,
    PlanSkeletonCandidate,
    ProjectMapRevision,
    StateSnapshot,
)
from flowmarshal.engine.planner_roles import inspection_source_catalog
from flowmarshal.engine.planning import (
    goal_validation_requirement_rows,
    plan_gate,
    plan_review_evidence_catalog,
    plan_validation_scope_rows,
    validation_comparison_targets,
)
from scripts.diagnostics.inspection_inputs import (
    materialize_fixture_package,
    verify_fixture_package,
)
from scripts.diagnostics.r_s06_10_fixtures import build_revision, verify_reviewed_case


RELOCATION_SCHEMA_VERSION = "flowmarshal-inspection-relocation-v1"
RELOCATION_MANIFEST = "relocation-proof.json"
FIXTURE_REVISION_DIRECTORY = "fixture-revision"
_FINAL_NON_STATIC_CASES = ("expansion", "expanded-review")


class InspectionMaterializationError(RuntimeError):
    """진단 입력의 물리 경로 재결속 또는 보존 증명이 실패할 때 발생한다."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InspectionMaterializationError(f"JSON을 읽을 수 없습니다: {path}") from exc
    if not isinstance(value, dict):
        raise InspectionMaterializationError(f"JSON 최상위 값이 object가 아닙니다: {path}")
    return value


def _write_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _copy_new(source: Path, destination: Path) -> None:
    content = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _json_differences(before: Any, after: Any, pointer: str = "") -> set[str]:
    if type(before) is not type(after):
        return {pointer or "/"}
    if isinstance(before, dict):
        differences: set[str] = set()
        for key in set(before) | set(after):
            child = f"{pointer}/{_pointer_token(key)}"
            if key not in before or key not in after:
                differences.add(child)
            else:
                differences.update(_json_differences(before[key], after[key], child))
        return differences
    if isinstance(before, list):
        if len(before) != len(after):
            return {pointer or "/"}
        differences: set[str] = set()
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            differences.update(_json_differences(left, right, f"{pointer}/{index}"))
        return differences
    return set() if before == after else {pointer or "/"}


def _require_exact_changes(before: Any, after: Any, allowed: set[str], *, label: str) -> list[str]:
    differences = _json_differences(before, after)
    if differences != allowed:
        raise InspectionMaterializationError(
            f"{label}의 변경 field가 허용 집합과 다릅니다: "
            f"expected={sorted(allowed)!r}, actual={sorted(differences)!r}"
        )
    return sorted(differences)


def _static_cases(expectations: dict[str, Any]) -> tuple[str, ...]:
    order = expectations.get("provider_call_order")
    if (
        not isinstance(order, list)
        or len(order) != 13
        or tuple(order[-2:]) != _FINAL_NON_STATIC_CASES
        or len(set(order)) != len(order)
    ):
        raise InspectionMaterializationError("STATIC_CASES 11개를 expectations에서 확인할 수 없습니다.")
    return tuple(order[:-2])


def _check_user_goal_paths(source_inputs: Path, original_project_map: ProjectMapRevision) -> list[dict[str, str]]:
    physical_paths = [original_project_map.root, *(
        entry.path for entry in original_project_map.entries if Path(entry.path).is_absolute()
    )]
    checked: list[dict[str, str]] = []
    for filename in ("input-goal.json", "input-semantic-explicit-goal.json"):
        raw = _read(source_inputs / filename)
        goal = GoalContractRevision.model_validate(raw)
        source_request = goal.definition.source_request
        mentions = [path for path in physical_paths if path.casefold() in source_request.casefold()]
        checked.append({"input": filename, "source_request_digest": goal.definition.source_request_digest})
        if mentions:
            raise InspectionMaterializationError(
                "USER_GOAL_PHYSICAL_ROOT_CONFLICT: " + filename + ": " + ", ".join(mentions)
            )
    return checked


def _relocated_project_map(
    original: dict[str, Any], materialization: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    relocated = deepcopy(original)
    destination_by_entry = {
        item["entry_id"]: item["destination"] for item in materialization["project_entries"]
    }
    relocated["root"] = materialization["project_root"]
    allowed = {"/root"}
    mappings = [{
        "field": "/root",
        "source": original["root"],
        "destination": relocated["root"],
    }]
    for index, entry in enumerate(original["entries"]):
        if not Path(entry["path"]).is_absolute():
            continue
        try:
            destination = destination_by_entry[entry["entry_id"]]
        except KeyError as exc:
            raise InspectionMaterializationError(
                f"절대 ProjectMap entry의 materialized 경로가 없습니다: {entry['entry_id']}"
            ) from exc
        relocated["entries"][index]["path"] = destination
        pointer = f"/entries/{index}/path"
        allowed.add(pointer)
        mappings.append({"entry_id": entry["entry_id"], "field": pointer,
                         "source": entry["path"], "destination": destination})
    changed = _require_exact_changes(original, relocated, allowed, label="ProjectMap")
    validated = ProjectMapRevision.model_validate(relocated)
    proof = {
        "after_canonical_digest": sha256_digest(relocated),
        "allowed_fields": sorted(allowed),
        "before_canonical_digest": sha256_digest(original),
        "changed_fields": changed,
        "mappings": mappings,
        "relocated_revision_digest": validated.revision_digest,
    }
    return relocated, proof


def _relocated_plan(
    original: dict[str, Any], project_map_digest: str,
) -> tuple[dict[str, Any], list[str]]:
    relocated = deepcopy(original)
    relocated["definition"]["project_map_digest"] = project_map_digest
    relocated["definition_digest"] = sha256_digest(relocated["definition"])
    changed = _require_exact_changes(
        original,
        relocated,
        {"/definition/project_map_digest", "/definition_digest"},
        label="PlanContract",
    )
    PlanContractRevision.model_validate(relocated)
    return relocated, changed


def _review_plan_digest(value: dict[str, Any]) -> str:
    """독립 review가 실제 payload에 받은 model_dump 표현의 digest를 계산한다."""
    plan = PlanContractRevision.model_validate(value)
    return sha256_digest(plan.model_dump(mode="json"))


def _review_payload(
    *, plan: PlanContractRevision, goal: GoalContractRevision, state: StateSnapshot,
    project_map: ProjectMapRevision,
) -> dict[str, Any]:
    evidence = plan_review_evidence_catalog(plan, goal, state, project_map)
    return {
        "evidence_catalog": evidence,
        "goal_validation_requirement_rows": goal_validation_requirement_rows(goal),
        "inspection_source_catalog": inspection_source_catalog(
            project_map, {key: f"payload.evidence_catalog.{key}" for key in evidence},
        ),
        "validation_comparison_targets": validation_comparison_targets(goal, plan),
        "validation_scope_rows": plan_validation_scope_rows(plan),
    }


def _workspace_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def prepare_relocated_inputs(package: Path, run: Path) -> dict[str, Any]:
    """package를 복제하고 고정 fixture의 물리 경로 결속만 새 run에 맞춘다."""
    package_binding = verify_fixture_package(package)
    package_root = Path(package_binding["package"])
    run = Path(run).resolve(strict=False)
    try:
        run.relative_to(package_root)
    except ValueError:
        pass
    else:
        raise InspectionMaterializationError("run은 입력 package 내부에 만들 수 없습니다.")
    try:
        package_root.relative_to(run)
    except ValueError:
        pass
    else:
        raise InspectionMaterializationError("입력 package는 run 내부에 둘 수 없습니다.")
    fixture_revision = run / FIXTURE_REVISION_DIRECTORY
    if fixture_revision.exists() or fixture_revision.is_symlink():
        raise FileExistsError(fixture_revision)
    if (run / RELOCATION_MANIFEST).exists() or (run / RELOCATION_MANIFEST).is_symlink():
        raise FileExistsError(run / RELOCATION_MANIFEST)

    materialization = materialize_fixture_package(package, run)
    source_inputs = Path(materialization["source_inputs_dir"])
    original_project_map = ProjectMapRevision.model_validate(_read(source_inputs / "input-project-map.json"))
    user_goal_checks = _check_user_goal_paths(source_inputs, original_project_map)
    build_revision(source_inputs, fixture_revision)

    expectations = _read(fixture_revision / "expectations.json")
    static_cases = _static_cases(expectations)
    original_review = _read(fixture_revision / "independent-fixture-review.json")
    if original_review.get("reviewed_cases") != list(static_cases):
        raise InspectionMaterializationError("원본 독립 review의 STATIC_CASES 결속이 다릅니다.")

    original_map_raw = _read(fixture_revision / "input-project-map.json")
    if original_map_raw != original_project_map.model_dump(mode="json"):
        raise InspectionMaterializationError("fixture revision ProjectMap이 package 원본과 다릅니다.")
    relocated_map_raw, project_map_proof = _relocated_project_map(original_map_raw, materialization)
    relocated_map = ProjectMapRevision.model_validate(relocated_map_raw)

    relocated_plans: dict[str, dict[str, Any]] = {}
    plan_proofs: list[dict[str, Any]] = []
    case_input_paths: dict[str, str] = {}
    for name in static_cases:
        filename = f"input-{name}-plan.json"
        case_input_paths[name] = filename
        original_path = fixture_revision / filename
        original = _read(original_path)
        original_digest = _review_plan_digest(original)
        if original_review["case_plan_contract_digests"].get(name) != original_digest:
            raise InspectionMaterializationError(f"원본 review가 원본 Plan에 결속되지 않았습니다: {name}")
        relocated, changed = _relocated_plan(original, relocated_map.revision_digest)
        relocated_plans[filename] = relocated
        plan_proofs.append({
            "after_byte_digest": sha256_bytes((canonical_json(relocated) + "\n").encode("utf-8")),
            "after_canonical_digest": sha256_digest(relocated),
            "allowed_fields": ["/definition/project_map_digest", "/definition_digest"],
            "before_byte_digest": sha256_bytes(original_path.read_bytes()),
            "before_canonical_digest": sha256_digest(original),
            "before_review_payload_digest": original_digest,
            "case_id": name,
            "changed_fields": changed,
            "input": filename,
        })

    relocated_review = deepcopy(original_review)
    rebound_cases: dict[str, dict[str, str]] = {}
    for case_id, case in original_review["case_reviews"].items():
        filename = case["input"]
        if filename not in relocated_plans:
            continue
        before_plan = _read(fixture_revision / filename)
        before_digest = _review_plan_digest(before_plan)
        if original_review["case_plan_contract_digests"].get(case_id) != before_digest:
            raise InspectionMaterializationError(f"공유 원본 review Plan 결속이 다릅니다: {case_id}")
        after_digest = _review_plan_digest(relocated_plans[filename])
        relocated_review["case_plan_contract_digests"][case_id] = after_digest
        rebound_cases[case_id] = {"input": filename, "before": before_digest, "after": after_digest}
    review_allowed = {f"/case_plan_contract_digests/{_pointer_token(case_id)}" for case_id in rebound_cases}
    review_changed = _require_exact_changes(
        original_review, relocated_review, review_allowed, label="independent fixture review",
    )

    replaced = {"input-project-map.json", "independent-fixture-review.json", *relocated_plans}
    fixture_files = [path for path in sorted(fixture_revision.rglob("*")) if path.is_file()]
    root_targets = [run / path.relative_to(fixture_revision) for path in fixture_files]
    for target in root_targets:
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
    for path in fixture_files:
        relative = path.relative_to(fixture_revision).as_posix()
        if relative not in replaced:
            _copy_new(path, run / relative)
    _write_new(run / "input-project-map.json", relocated_map_raw)
    for filename, plan in relocated_plans.items():
        _write_new(run / filename, plan)
    _write_new(run / "independent-fixture-review.json", relocated_review)
    preserved_files = {
        path.relative_to(fixture_revision).as_posix(): sha256_bytes(path.read_bytes())
        for path in fixture_files if path.relative_to(fixture_revision).as_posix() not in replaced
    }
    if any(sha256_bytes((run / relative).read_bytes()) != digest
           for relative, digest in preserved_files.items()):
        raise InspectionMaterializationError("relocation 비대상 fixture 원문이 보존되지 않았습니다.")

    goal = GoalContractRevision.model_validate(_read(run / "input-goal.json"))
    state = StateSnapshot.model_validate(_read(run / "input-state.json"))
    semantic_goal = GoalContractRevision.model_validate(_read(run / "input-semantic-explicit-goal.json"))
    semantic_state = StateSnapshot.model_validate(_read(run / "input-semantic-explicit-state.json"))
    skeleton = PlanSkeletonCandidate.model_validate(_read(run / "input-skeleton.json"))
    gate_results: dict[str, str] = {}
    review_results: dict[str, str] = {}
    for name in static_cases:
        case_goal, case_state = (semantic_goal, semantic_state) if name.startswith("semantic-") else (goal, state)
        plan = PlanContractRevision.model_validate(relocated_plans[case_input_paths[name]])
        findings = plan_gate(plan, source=skeleton, goal=case_goal, state=case_state, project_map=relocated_map)
        if findings:
            raise InspectionMaterializationError(
                f"relocated plan_gate가 실패했습니다: {name}: "
                + ", ".join(item.finding_code for item in findings)
            )
        payload = _review_payload(plan=plan, goal=case_goal, state=case_state, project_map=relocated_map)
        verify_reviewed_case(name, payload, expectations, relocated_review)
        gate_results[name] = "PASS"
        review_results[name] = "PASS"

    workspace_files = _workspace_files(Path(materialization["project_root"]))
    proof_body = {
        "authority_effect": "diagnostic_fixture_only_not_activated",
        "fixture_revision": str(fixture_revision),
        "independent_review": {
            "after_byte_digest": sha256_bytes((run / "independent-fixture-review.json").read_bytes()),
            "after_canonical_digest": sha256_digest(relocated_review),
            "allowed_fields": sorted(review_allowed),
            "before_byte_digest": sha256_bytes((fixture_revision / "independent-fixture-review.json").read_bytes()),
            "before_canonical_digest": sha256_digest(original_review),
            "changed_fields": review_changed,
            "rebound_cases": rebound_cases,
        },
        "materialization_path_mappings": materialization["path_mappings"],
        "package_manifest_digest": package_binding["manifest_digest"],
        "plan_gate": gate_results,
        "plans": plan_proofs,
        "preserved_fixture_files": preserved_files,
        "project_map": project_map_proof | {
            "after_byte_digest": sha256_bytes((run / "input-project-map.json").read_bytes()),
            "before_byte_digest": sha256_bytes((fixture_revision / "input-project-map.json").read_bytes()),
        },
        "schema_version": RELOCATION_SCHEMA_VERSION,
        "source_inputs_dir": str(source_inputs),
        "user_goal_physical_path_checks": user_goal_checks,
        "verify_reviewed_case": review_results,
        "workspace_files": workspace_files,
        "workspace_files_digest": sha256_digest(workspace_files),
    }
    proof = proof_body | {"proof_digest": sha256_digest(proof_body)}
    _write_new(run / RELOCATION_MANIFEST, proof)
    return {
        "fixture_revision": str(fixture_revision),
        "independent_review_digest": sha256_bytes((run / "independent-fixture-review.json").read_bytes()),
        "materialization_path_mappings": materialization["path_mappings"],
        "package_binding": package_binding,
        "project_map_digest": relocated_map.revision_digest,
        "project_root": materialization["project_root"],
        "relocation_manifest": str(run / RELOCATION_MANIFEST),
        "relocation_manifest_digest": sha256_bytes((run / RELOCATION_MANIFEST).read_bytes()),
        "relocation_proof_digest": proof["proof_digest"],
        "run": str(run),
        "schema_version": RELOCATION_SCHEMA_VERSION,
        "source_inputs_dir": str(source_inputs),
        "static_case_plan_digests": {
            name: _review_plan_digest(relocated_plans[case_input_paths[name]]) for name in static_cases
        },
        "workspace_files": workspace_files,
        "workspace_files_digest": sha256_digest(workspace_files),
    }
