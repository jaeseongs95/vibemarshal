"""Plan 교체 시 로컬 완료 근거의 보수적인 재사용. 원본 Attempt/receipt는 이동하지 않는다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .context import (
    ProjectMapper,
    project_map_reobservation_scope,
    workspace_path_inventory_digest,
)
from .domain import (
    DeterministicValidationObservation, EvidenceRecord, PlanContractRevision,
    ProjectMapRevision, TaskExecutionSpecRevision, ValidationResult,
)


def observation_checkpoint(service: Any, connection: Any, task_id: str) -> dict[str, Any] | None:
    task = connection.execute("SELECT * FROM task_contracts WHERE id = ?", (task_id,)).fetchone()
    contract = json.loads(task["payload_json"])
    # 외부 시스템의 freshness를 파일 관측으로 증명할 수 없다.
    if any(effect["external"] for effect in contract["expected_effects"]):
        return None
    project = connection.execute("SELECT * FROM projects WHERE id = ?", (task["project_id"],)).fetchone()
    spec_row = connection.execute("SELECT payload_json FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                                  (task_id,)).fetchone()
    if spec_row is None:
        return None
    spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
    sources = connection.execute("SELECT kind, path FROM context_source_registrations WHERE project_id = ?",
                                 (task["project_id"],)).fetchall()
    root = Path(project["root"]).resolve()
    try:
        map_row = connection.execute(
            "SELECT revision_digest, payload_json FROM project_map_revisions "
            "WHERE project_id = ? AND is_current = 1",
            (task["project_id"],),
        ).fetchone()
        if map_row is None:
            return None
        mapped = ProjectMapRevision.model_validate_json(map_row["payload_json"])
        observed = ProjectMapper().build(
            project_id=task["project_id"],
            root=root,
            revision_no=mapped.revision_no + 1,
            registered_references=(row["path"] for row in sources if row["kind"] == "reference"),
            instruction_sources=(row["path"] for row in sources if row["kind"] == "instruction"),
            excluded_paths=(service.ledger.artifact_root.resolve(),),
            **project_map_reobservation_scope(mapped),
        )
        paths = {item.path for item in spec.definition.resolved_targets}
        paths.update(item.source_ref for item in spec.definition.context_manifest.fragments)
        paths.update(path for step in spec.definition.validation_steps for path in step.artifact_paths)
        files = {}
        for path in sorted(paths):
            target = (root / path).resolve()
            if not target.is_file():
                return None
            files[str(target)] = sha256_bytes(target.read_bytes())
        state_row = connection.execute("SELECT payload_json FROM state_snapshots WHERE project_id = ? AND is_current = 1",
                                       (task["project_id"],)).fetchone()
        if state_row is None:
            return None
        state = json.loads(state_row["payload_json"])
        if any(fact["freshness"] != "current" for fact in state["facts"]):
            return None
        mapped_paths = {item.path: item.content_digest for item in mapped.entries}
        workspace_inventory = workspace_path_inventory_digest(
            root,
            excluded_paths=(service.ledger.artifact_root.resolve(),),
        )

        def derived_file_fact(fact: dict[str, Any]) -> bool:
            if fact["source_ref"] == "project-map":
                return fact["value"] == fact["evidence_digest"] == map_row["revision_digest"]
            if fact["source_ref"] == "project-workspace:path-inventory":
                return (
                    fact["fact_id"] == "fact_workspace_path_inventory"
                    and fact["predicate"] == "current project file path inventory"
                    and fact["value"] == fact["evidence_digest"] == workspace_inventory
                )
            path = fact["source_ref"]
            digest = mapped_paths.get(path)
            return (fact["fact_id"] == "fact_target_" + sha256_digest(path).split(":", 1)[1][:20]
                    and fact["predicate"] == f"Goal 관련 target의 현재 content digest: {path}"
                    and fact["value"] == digest
                    and fact["evidence_digest"] == sha256_digest({"path": path, "content_digest": digest,
                                                                  "map": map_row["revision_digest"]}))
        # Core가 재관측으로 교체하는 map/target 사실은 위의 실제 파일 관측으로 대조한다.
        # revision ID만 새로 생기는 것과 별도 의미를 가진 State 사실 변경을 구분한다.
        semantic_facts = [fact for fact in state["facts"] if not derived_file_fact(fact)]
        return {"format": "local-completion-observation-v1", "root": str(root),
                "map_semantic_digest": observed.semantic_digest, "files": files,
                "state_facts_digest": sha256_digest({"facts": semantic_facts, "unknowns": state["unknowns"]})}
    except (OSError, ValueError):
        # 읽을 수 없는 근거는 재사용 대상에서 제외한다. 원래 완료 판정을 소급 변경하지 않는다.
        return None


def _contract_semantics(task: Any) -> dict[str, Any]:
    value = task.model_dump(mode="json")
    value.pop("task_id")
    return value


def _incoming(plan: PlanContractRevision, task_id: str) -> tuple[Any, ...]:
    refs = {task.task_id: task.task_ref for task in plan.definition.tasks}
    return tuple(sorted((refs[item.producer_task_id], item.dependency_type.value, item.products)
                        for item in plan.definition.dependencies if item.consumer_task_id == task_id))


def _verified_local_evidence(connection: Any, row: Any, checkpoint: dict[str, Any]) -> bool:
    """원장 결속과 관측 본문을 재검사한다. 해석할 수 없는 근거는 재사용하지 않는다."""
    try:
        record = EvidenceRecord.model_validate_json(row["payload_json"])
        if any(row[column] != getattr(record, field) for column, field in (
            ("id", "evidence_id"), ("project_id", "project_id"), ("task_id", "task_id"),
            ("attempt_id", "attempt_id"), ("kind", "kind"), ("source_ref", "source_ref"),
            ("observation", "observation"), ("content_digest", "content_digest"),
        )):
            return False
        history = connection.execute(
            "SELECT payload_json FROM history_events WHERE project_id = ? "
            "AND entity_id = ? AND event_type = 'evidence.recorded'",
            (record.project_id, record.evidence_id),
        ).fetchone()
        if history is None:
            return False
        event = json.loads(history["payload_json"])
        if any(event.get(key) != row[key] for key in ("task_id", "kind", "content_digest")):
            return False
        if row["kind"] in {"command", "test", "build"}:
            observed = DeterministicValidationObservation.model_validate_json(record.observation)
            return (observed.task_id == record.task_id and observed.passed
                    and record.content_digest == sha256_digest({"kind": row["kind"], "observation": observed}))
        if row["kind"] in {"file", "diff"}:
            observed = json.loads(record.observation)
            path = str((Path(checkpoint["root"]) / record.source_ref).resolve())
            if (observed.get("path") != record.source_ref or path not in checkpoint["files"]
                    or observed.get("after_digest") != checkpoint["files"][path]):
                return False
            return record.content_digest in {
                sha256_digest(observed), sha256_digest({"diff": observed}),
                sha256_digest({"kind": row["kind"], "direct_file_observation": observed}),
            }
        # 외부 관측·사용자 판단·semantic review는 별도 freshness 검증 없이 옮기지 않는다.
        return False
    except (ValueError, TypeError, KeyError, OSError):
        return False


def _verified_worker_files(service: Any, connection: Any, task_id: str,
                           checkpoint: dict[str, Any]) -> set[str]:
    spec = connection.execute(
        "SELECT definition_digest FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
        (task_id,),
    ).fetchone()
    worker = None if spec is None else service.resolve_task_validation_worker(
        connection, task_id=task_id, current_spec_digest=spec["definition_digest"],
    )
    if worker is None:
        return set()
    return {row["id"] for row in connection.execute(
        "SELECT * FROM evidence_records WHERE task_id = ? AND attempt_id = ? AND kind = 'file'",
        (task_id, worker["id"]),
    ) if _verified_local_evidence(connection, row, checkpoint)}


def reuse_completed_tasks(service: Any, tx: Any, plan: PlanContractRevision, previous_id: str) -> tuple[str, ...]:
    previous = PlanContractRevision.model_validate_json(tx.one("SELECT payload_json FROM plan_revisions WHERE id = ?",
                                                              (previous_id,))["payload_json"])
    if previous.definition.goal_contract_digest != plan.definition.goal_contract_digest:
        return ()
    previous_tasks = {task.task_ref: task for task in previous.definition.tasks}
    reused: dict[str, str] = {}
    remaining = list(plan.definition.tasks)
    while remaining:
        progressed = False
        for task in tuple(remaining):
            incoming = _incoming(plan, task.task_id)
            if any(ref not in reused for ref, _, _ in incoming):
                continue
            remaining.remove(task)
            old = previous_tasks.get(task.task_ref)
            if old is None or _contract_semantics(old) != _contract_semantics(task) or _incoming(previous, old.task_id) != incoming:
                continue
            row = tx.one("SELECT status FROM task_contracts WHERE id = ?", (old.task_id,))
            if row["status"] != "completed":
                continue
            origin = tx.maybe_one("SELECT * FROM task_completion_reuse WHERE task_id = ?", (old.task_id,))
            source_task_id = old.task_id if origin is None else origin["source_task_id"]

            def reject(reason: str) -> None:
                tx.history(
                    task.project_id,
                    "task.completion_reuse_rejected",
                    "task_contract",
                    task.task_id,
                    {
                        "source_task_id": source_task_id,
                        "source_plan_revision_id": previous_id,
                        "reason": reason,
                    },
                )

            completion = tx.maybe_one("SELECT payload_json FROM history_events WHERE entity_id = ? "
                                     "AND event_type = 'task.completed' ORDER BY sequence DESC LIMIT 1", (source_task_id,))
            if completion is None:
                reject("COMPLETION_EVENT_MISSING")
                continue
            checkpoint = json.loads(completion["payload_json"]).get("reuse_checkpoint")
            if checkpoint is None or observation_checkpoint(service, tx.connection, source_task_id) != checkpoint:
                reject("REUSE_CHECKPOINT_STALE_OR_MISSING")
                continue
            validations = service.effective_task_validation_results(tx.connection, source_task_id)
            latest = {row["validation_id"]: row for row in validations}
            if set(latest) != {item.validation_id for item in task.validations}:
                reject("VALIDATION_SET_MISMATCH")
                continue
            if origin is not None:
                inherited = {row["validation_id"]: row for row in
                             service.effective_task_validation_results(tx.connection, old.task_id)}
                # 중간 revision의 추가 검사도 원래 근거의 유효성을 바꾼다.
                # 새 PASS라도 원본 완료 checkpoint에 결속되지 않았으면 재사용하지 않는다.
                bound_ids = set(json.loads(origin["validation_ids_json"]))
                if ({row["id"] for row in inherited.values()} != bound_ids
                        or {row["id"] for row in latest.values()} != bound_ids):
                    reject("INHERITED_VALIDATION_BINDING_MISMATCH")
                    continue
            evidence_ids: set[str] = set()
            valid = True
            for contract in task.validations:
                result_row = latest[contract.validation_id]
                result = ValidationResult.model_validate_json(result_row["payload_json"])
                history = json.loads(result_row["history_payload_json"])
                if result.status.value != "pass" or not result.evidence_ids or history.get("reuse_checkpoint") != checkpoint:
                    valid = False
                    break
                evidence = [tx.maybe_one("SELECT * FROM evidence_records WHERE id = ?", (item,)) for item in result.evidence_ids]
                if (any(item is None or item["project_id"] != task.project_id or item["task_id"] != source_task_id
                        or not _verified_local_evidence(tx.connection, item, checkpoint) for item in evidence)
                        or not set(contract.required_evidence_kinds).issubset({item["kind"] for item in evidence if item})):
                    valid = False
                    break
                evidence_ids.update(result.evidence_ids)
            if not valid:
                reject("EVIDENCE_INVALID_OR_INSUFFICIENT")
                continue
            # 후속 Task에 필요한 현재 Worker 파일만 연결한다. 실패 시도·미검증 보고서는 제외한다.
            evidence_ids.update(_verified_worker_files(service, tx.connection, source_task_id, checkpoint))
            tx.connection.execute("INSERT INTO task_completion_reuse "
                                  "(task_id,source_task_id,validation_ids_json,evidence_ids_json,checkpoint_json,created_at) "
                                  "VALUES (?,?,?,?,?,?)", (task.task_id, source_task_id,
                                  canonical_json(sorted(row["id"] for row in latest.values())),
                                  canonical_json(sorted(evidence_ids)), canonical_json(checkpoint), tx.now))
            tx.connection.execute("UPDATE task_contracts SET status = 'completed', updated_at = ? WHERE id = ?",
                                  (tx.now, task.task_id))
            tx.history(task.project_id, "task.completion_reused", "task_contract", task.task_id,
                       {"source_task_id": source_task_id, "source_plan_revision_id": previous_id,
                        "checkpoint": checkpoint, "evidence_ids": sorted(evidence_ids)})
            reused[task.task_ref] = task.task_id
            progressed = True
        if not progressed:
            break
    return tuple(reused.values())
