"""원장 lifecycle v2 수집기.

v1 checkpoint의 원시 본문은 보존하며 새 수집은 모든 ExecutionSpec revision과
외부 효과 관측을 별도 목록으로 기록한다.
"""
from __future__ import annotations

import json

from ..canonical import sha256_digest
from .domain import PlanContractRevision, TaskExecutionSpecRevision
from .evaluation import (
    BenchmarkLifecycleObservation,
    BenchmarkMaterializedExecutionSpecObservation,
    BenchmarkTaskLifecycleObservation,
)
from .ledger import SQLiteEngineLedger
from .qualification import QualificationRunError

def _successful_worker_for_current_spec(connection, task_id: str, current_spec_digest: str):
    """validator-only model 재결속 계보를 거슬러 실제 Worker Spec과 Attempt를 찾는다."""

    digest = current_spec_digest
    seen: set[str] = set()
    while digest not in seen:
        seen.add(digest)
        attempt = connection.execute(
            "SELECT * FROM attempts WHERE task_id = ? AND kind = 'execution' "
            "AND execution_spec_digest = ? AND status = 'succeeded' "
            "ORDER BY attempt_no DESC LIMIT 1",
            (task_id, digest),
        ).fetchone()
        if attempt is not None:
            spec = connection.execute(
                "SELECT * FROM execution_spec_revisions WHERE task_id = ? "
                "AND definition_digest = ? ORDER BY revision_no DESC LIMIT 1",
                (task_id, digest),
            ).fetchone()
            return spec, attempt
        selection = connection.execute(
            "SELECT s.previous_execution_spec_digest FROM model_rebinding_selections s "
            "JOIN attempts a ON a.id = s.attempt_id AND a.task_id = s.task_id "
            "AND a.kind = 'validation' "
            "AND a.execution_spec_digest = s.new_execution_spec_digest "
            "WHERE s.task_id = ? AND s.role = 'validator' "
            "AND s.new_execution_spec_digest = ? "
            "ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1",
            (task_id, digest),
        ).fetchone()
        if selection is None:
            return None, None
        digest = selection["previous_execution_spec_digest"]
    return None, None


def _receipt_digest(rows) -> str:
    return sha256_digest([
        {"id": row["id"], "response_digest": row["response_digest"],
         "payload": json.loads(row["payload_json"])}
        for row in rows
    ])


def _materialized_observations(connection, *, project_id: str, selected_plan, goal_digest: str):
    """선택 Plan의 명시적 상위 계보만 대상으로, 원장 row에서 분모를 재계산한다."""

    selected_goal = connection.execute(
        "SELECT goal_id FROM goal_revisions WHERE project_id=? AND definition_digest=?",
        (project_id, goal_digest),
    ).fetchone()
    if selected_goal is None:
        raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: 선택 Plan Goal revision")
    lineage: list[str] = []
    current = selected_plan
    seen: set[str] = set()
    while current is not None:
        if current["id"] in seen:
            raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: Plan supersedes loop")
        seen.add(current["id"])
        if current["project_id"] != project_id:
            raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: Plan ancestor project")
        payload = json.loads(current["payload_json"])
        ancestor_digest = payload.get("definition", {}).get("goal_contract_digest")
        ancestor_goal = connection.execute(
            "SELECT goal_id FROM goal_revisions WHERE project_id=? AND definition_digest=?",
            (project_id, ancestor_digest),
        ).fetchone()
        if ancestor_goal is None or ancestor_goal["goal_id"] != selected_goal["goal_id"]:
            raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: Plan ancestor Goal scope")
        lineage.append(current["id"])
        if current["supersedes_id"] is None:
            break
        current = connection.execute(
            "SELECT * FROM plan_revisions WHERE id = ?", (current["supersedes_id"],)
        ).fetchone()
        if current is None:
            raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: missing Plan ancestor")
    placeholders = ",".join("?" for _ in lineage)
    rows = connection.execute(
        "SELECT e.*, t.plan_revision_id FROM execution_spec_revisions e "
        "JOIN task_contracts t ON t.id=e.task_id "
        f"WHERE t.project_id=? AND t.plan_revision_id IN ({placeholders}) "
        "ORDER BY t.plan_revision_id, t.position, e.revision_no",
        (project_id, *lineage),
    ).fetchall()

    by_digest = {row["definition_digest"]: row for row in rows}
    cache: dict[str, BenchmarkMaterializedExecutionSpecObservation] = {}

    def classify(row, chain: set[str]) -> BenchmarkMaterializedExecutionSpecObservation:
        cached = cache.get(row["id"])
        if cached is not None:
            return cached
        event = connection.execute(
            "SELECT event_hash FROM history_events WHERE project_id=? AND event_type='task.materialized' "
            "AND entity_type='execution_spec_revision' AND entity_id=? ORDER BY sequence DESC LIMIT 1",
            (project_id, row["id"]),
        ).fetchone()
        if event is None:
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: ExecutionSpec materialization history가 없습니다.")
        attempts = connection.execute(
            "SELECT * FROM attempts WHERE task_id=? AND kind='execution' AND execution_spec_digest=? "
            "ORDER BY attempt_no",
            (row["task_id"], row["definition_digest"]),
        ).fetchall()
        receipt_rows = connection.execute(
            "SELECT r.id,r.response_digest,r.payload_json,a.id AS attempt_id FROM runtime_receipts r "
            "JOIN runtime_intents i ON i.id=r.intent_id JOIN attempts a ON a.id=i.attempt_id "
            "WHERE a.task_id=? AND a.kind='execution' AND a.execution_spec_digest=? "
            "AND i.kind IN ('start_turn','resume_turn') ORDER BY r.received_at",
            (row["task_id"], row["definition_digest"]),
        ).fetchall()
        base = dict(
            plan_revision_id=row["plan_revision_id"], task_id=row["task_id"],
            execution_spec_revision_id=row["id"], execution_spec_digest=row["definition_digest"],
            materialization_event_digest=event["event_hash"],
        )
        selection = connection.execute(
            "SELECT previous_execution_spec_digest FROM model_rebinding_selections s "
            "JOIN attempts a ON a.id=s.attempt_id AND a.task_id=s.task_id "
            "AND a.kind='validation' AND a.execution_spec_digest=s.new_execution_spec_digest "
            "WHERE s.task_id=? AND s.role='validator' AND s.new_execution_spec_digest=? "
            "ORDER BY s.created_at DESC,s.rowid DESC LIMIT 1",
            (row["task_id"], row["definition_digest"]),
        ).fetchone()
        previous = by_digest.get(selection["previous_execution_spec_digest"]) if selection else None
        if receipt_rows:
            result = BenchmarkMaterializedExecutionSpecObservation(
                **base, execution_state="executed", provenance="worker_turn_receipt",
                worker_attempt_ids=tuple(dict.fromkeys(item["attempt_id"] for item in receipt_rows)),
                runtime_receipt_digests=(_receipt_digest(receipt_rows),),
            )
        elif not attempts and previous is None:
            result = BenchmarkMaterializedExecutionSpecObservation(
                **base, execution_state="unexecuted", provenance="no_effect"
            )
        else:
            released = all(
                attempt["status"] == "abandoned" and connection.execute(
                    "SELECT 1 FROM history_events WHERE project_id=? AND event_type='attempt.released_before_effect' "
                    "AND entity_type='attempt' AND entity_id=?", (project_id, attempt["id"])
                ).fetchone() is not None
                for attempt in attempts
            )
            if attempts and released:
                result = BenchmarkMaterializedExecutionSpecObservation(
                    **base, execution_state="unexecuted", provenance="no_effect"
                )
            else:
                if previous is not None and previous["id"] not in chain:
                    prior = classify(previous, chain | {row["id"]})
                    if prior.execution_state == "executed":
                        result = BenchmarkMaterializedExecutionSpecObservation(
                            **base, execution_state="executed", provenance="validator_rebind_worker_turn",
                            worker_attempt_ids=prior.worker_attempt_ids,
                            runtime_receipt_digests=prior.runtime_receipt_digests,
                            reused_worker_execution_spec_digest=prior.execution_spec_digest,
                        )
                    elif prior.execution_state == "unexecuted":
                        result = BenchmarkMaterializedExecutionSpecObservation(
                            **base, execution_state="unexecuted", provenance="no_effect"
                        )
                    else:
                        result = BenchmarkMaterializedExecutionSpecObservation(
                            **base, execution_state="unknown_effect", provenance="unknown_effect"
                        )
                else:
                    result = BenchmarkMaterializedExecutionSpecObservation(
                        **base, execution_state="unknown_effect", provenance="unknown_effect"
                    )
        cache[row["id"]] = result
        return result

    return tuple(classify(row, set()) for row in rows)


def collect_lifecycle_observation(
    ledger: SQLiteEngineLedger,
    *,
    project_id: str,
    plan_activation_digest: str,
    model_lock_digest: str,
    neutral_input_digest: str,
) -> BenchmarkLifecycleObservation:
    """완료된 SQLite 원장에서만 benchmark lifecycle evidence를 투영한다."""

    with ledger.read() as connection:
        project = connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None or project["run_state"] != "completed":
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: project가 completed가 아닙니다.")
        plan_row = connection.execute(
            "SELECT p.* FROM plan_revisions p JOIN plan_activations a ON a.plan_revision_id = p.id "
            "WHERE a.project_id = ? AND a.activation_digest = ? ORDER BY a.activated_at DESC LIMIT 1",
            (project_id, plan_activation_digest),
        ).fetchone()
        if plan_row is None or plan_row["status"] not in {"active", "completed"}:
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: 활성화된 완료 Plan이 없습니다.")
        plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
        if plan.activation_digest != plan_activation_digest:
            raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: Plan activation digest")

        task_rows = connection.execute(
            "SELECT * FROM task_contracts WHERE plan_revision_id = ? ORDER BY position",
            (plan.plan_revision_id,),
        ).fetchall()
        if (
            len(task_rows) != len(plan.definition.tasks)
            or not task_rows
            or any(row["status"] != "completed" for row in task_rows)
        ):
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: 모든 Plan Task가 completed가 아닙니다.")

        observations: list[BenchmarkTaskLifecycleObservation] = []
        for task_row in task_rows:
            spec_row = connection.execute(
                "SELECT * FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                (task_row["id"],),
            ).fetchone()
            if spec_row is None:
                raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: current ExecutionSpec이 없습니다.")
            current_spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
            if current_spec.definition.plan_activation_digest != plan_activation_digest:
                raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: ExecutionSpec Plan binding")
            worker_spec_row, attempt = _successful_worker_for_current_spec(
                connection,
                task_row["id"],
                current_spec.definition_digest,
            )
            if worker_spec_row is None or attempt is None or attempt["binding_json"] is None:
                raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: 완료 runtime Attempt binding이 없습니다.")
            worker_spec = TaskExecutionSpecRevision.model_validate_json(
                worker_spec_row["payload_json"]
            )
            if worker_spec.definition.plan_activation_digest != plan_activation_digest:
                raise QualificationRunError("LIFECYCLE_BINDING_MISMATCH: Worker Spec Plan binding")
            receipts = connection.execute(
                "SELECT r.id, r.response_digest, r.payload_json FROM runtime_receipts r "
                "JOIN runtime_intents i ON i.id = r.intent_id WHERE i.attempt_id = ? ORDER BY r.received_at",
                (attempt["id"],),
            ).fetchall()
            if not receipts:
                raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: runtime receipt가 없습니다.")
            required_validation_ids = {
                item.validation_id
                for item in next(
                    task for task in plan.definition.tasks if task.task_id == task_row["id"]
                ).validations
            }
            validation_rows = connection.execute(
                "SELECT id, validation_id, payload_json FROM validation_results "
                "WHERE plan_revision_id = ? AND task_id = ? AND status = 'pass'",
                (plan.plan_revision_id, task_row["id"]),
            ).fetchall()
            by_validation = {row["validation_id"]: row for row in validation_rows}
            if not required_validation_ids.issubset(by_validation):
                raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: Task validation PASS가 부족합니다.")
            observations.append(
                BenchmarkTaskLifecycleObservation(
                    task_id=task_row["id"],
                    execution_spec_revision_id=worker_spec.execution_spec_revision_id,
                    execution_spec_digest=worker_spec.definition_digest,
                    attempt_id=attempt["id"],
                    runtime_receipt_digest=sha256_digest(
                        [
                            {
                                "id": row["id"],
                                "response_digest": row["response_digest"],
                                "payload": json.loads(row["payload_json"]),
                            }
                            for row in receipts
                        ]
                    ),
                    validation_result_digests=tuple(
                        sha256_digest(
                            {"id": by_validation[validation_id]["id"],
                             "payload": json.loads(by_validation[validation_id]["payload_json"])}
                        )
                        for validation_id in sorted(required_validation_ids)
                    ),
                )
            )

        integration_ids = {item.validation_id for item in plan.definition.integration_validations}
        integration_rows = connection.execute(
            "SELECT id, validation_id, payload_json FROM validation_results "
            "WHERE plan_revision_id = ? AND task_id IS NULL AND status = 'pass'",
            (plan.plan_revision_id,),
        ).fetchall()
        by_integration = {row["validation_id"]: row for row in integration_rows}
        if not integration_ids.issubset(by_integration):
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: integration validation PASS가 부족합니다.")

        before = connection.execute(
            "SELECT snapshot_digest, version FROM state_snapshots "
            "WHERE project_id = ? AND snapshot_digest = ?",
            (project_id, plan.definition.base_state_snapshot_digest),
        ).fetchone()
        after = connection.execute(
            "SELECT snapshot_digest, version FROM state_snapshots "
            "WHERE project_id = ? AND goal_contract_digest = ? AND is_current = 1",
            (project_id, plan.definition.goal_contract_digest),
        ).fetchone()
        if before is None or after is None or int(after["version"]) <= int(before["version"]):
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: 실행 후 State 재관측 revision이 없습니다.")
        state_event = connection.execute(
            "SELECT event_hash, payload_json FROM history_events WHERE project_id = ? "
            "AND event_type = 'state.observed' ORDER BY sequence DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if (
            state_event is None
            or json.loads(state_event["payload_json"]).get("snapshot_digest")
            != after["snapshot_digest"]
        ):
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: State history evidence가 없습니다.")
        verdict = connection.execute(
            "SELECT id, payload_json FROM goal_verdicts WHERE plan_revision_id = ? "
            "AND status = 'satisfied' ORDER BY evaluated_at DESC LIMIT 1",
            (plan.plan_revision_id,),
        ).fetchone()
        head = connection.execute(
            "SELECT event_hash FROM history_events WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if verdict is None or head is None or not ledger.verify_history(project_id):
            raise QualificationRunError("LIFECYCLE_NOT_OBSERVED: Goal verdict/history 검증이 없습니다.")
        materialized = _materialized_observations(
            connection,
            project_id=project_id,
            selected_plan=plan_row,
            goal_digest=plan.definition.goal_contract_digest,
        )

    return BenchmarkLifecycleObservation(
        schema_version="2.0",
        collector="flowmarshal.engine.lifecycle-ledger-v2",
        project_id=project_id,
        plan_revision_id=plan.plan_revision_id,
        plan_activation_digest=plan_activation_digest,
        model_lock_digest=model_lock_digest,
        neutral_input_digest=neutral_input_digest,
        tasks=tuple(observations),
        materialized_execution_specs=materialized,
        integration_validation_result_digests=tuple(
            sha256_digest(
                {"id": by_integration[validation_id]["id"],
                 "payload": json.loads(by_integration[validation_id]["payload_json"])}
            )
            for validation_id in sorted(integration_ids)
        ),
        state_before_digest=before["snapshot_digest"],
        state_after_digest=after["snapshot_digest"],
        state_reobservation_event_digest=state_event["event_hash"],
        goal_verdict_digest=sha256_digest(
            {"id": verdict["id"], "payload": json.loads(verdict["payload_json"])}
        ),
        history_head_digest=head["event_hash"],
    )
