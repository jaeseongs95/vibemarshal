from __future__ import annotations

import json
from typing import Any, Callable

from ..canonical import sha256_digest
from .service import EngineService, EngineServiceError


class ExternalOperationUnknown(EngineServiceError):
    pass


class CoreOperations:
    """Task 밖의 준비·검증 효과도 기존 append-only Core History에 먼저 예약한다.

    receipt 없는 작업은 재호출하지 않는다. 완료 관측만 동일 입력에서 재사용한다.
    원장은 사용자 checkpoint 없이 불명확한 외부 효과를 성공으로 승격하지 않는다.
    """

    def __init__(self, service: EngineService, fault_hook: Callable[[str], None] | None = None):
        self.service = service
        self.fault_hook = fault_hook

    def _hit(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def recover_unfinished(self, project_id: str) -> tuple[str, ...]:
        with self.service.ledger.transaction() as tx:
            rows = tx.all(
                "SELECT p.entity_id FROM history_events p WHERE p.project_id = ? "
                "AND p.event_type = 'operation.prepared' AND NOT EXISTS ("
                "SELECT 1 FROM history_events c WHERE c.project_id = p.project_id "
                "AND c.entity_id = p.entity_id AND c.event_type = 'operation.completed')",
                (project_id,),
            )
            for row in rows:
                known = tx.maybe_one(
                    "SELECT id FROM history_events WHERE project_id = ? AND entity_id = ? "
                    "AND event_type = 'operation.external_unknown'", (project_id, row["entity_id"]),
                )
                if known is None:
                    tx.history(project_id, "operation.external_unknown", "core_operation", row["entity_id"],
                               {"reason": "완료 관측 없는 Core operation을 재실행하지 않습니다."})
            if rows:
                tx.connection.execute(
                    "UPDATE projects SET run_state = 'recovery_required', recovery_reason = ?, updated_at = ? WHERE id = ?",
                    ("EXTERNAL_EFFECT_UNKNOWN: 완료 관측 없는 Core operation", tx.now, project_id),
                )
            return tuple(row["entity_id"] for row in rows)

    def invoke(
        self, *, project_id: str, kind: str, request: dict[str, Any],
        execute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        request_digest = sha256_digest({"kind": kind, "request": request})
        operation_id = "operation_" + sha256_digest(
            {"project_id": project_id, "request_digest": request_digest}
        )[7:39]
        self._hit(f"before_{kind}_intent")
        unknown = False
        with self.service.ledger.transaction() as tx:
            project = tx.one("SELECT * FROM projects WHERE id = ?", (project_id,))
            rows = tx.all(
                "SELECT event_type, payload_json FROM history_events "
                "WHERE project_id = ? AND entity_type = 'core_operation' AND entity_id = ? "
                "ORDER BY sequence", (project_id, operation_id),
            )
            for row in rows:
                if row["event_type"] == "operation.completed":
                    payload = json.loads(row["payload_json"])
                    if payload["request_digest"] != request_digest:
                        raise EngineServiceError("Core operation 입력 digest가 다릅니다.")
                    if sha256_digest(payload["result"]) != payload["result_digest"]:
                        raise EngineServiceError("Core operation 관측 digest가 다릅니다.")
                    return payload["result"]
            if rows:
                unknown = True
                if not any(row["event_type"] == "operation.external_unknown" for row in rows):
                    tx.history(project_id, "operation.external_unknown", "core_operation", operation_id,
                               {"kind": kind, "request_digest": request_digest})
                tx.connection.execute(
                    "UPDATE projects SET run_state = 'recovery_required', recovery_reason = ? WHERE id = ?",
                    (f"EXTERNAL_EFFECT_UNKNOWN: {kind}/{operation_id}", project_id),
                )
            else:
                if project["run_state"] == "recovery_required":
                    raise ExternalOperationUnknown(project["recovery_reason"])
                tx.history(project_id, "operation.prepared", "core_operation", operation_id,
                           {"kind": kind, "request_digest": request_digest, "request": request})
        if unknown:
            raise ExternalOperationUnknown(f"EXTERNAL_EFFECT_UNKNOWN: {kind}/{operation_id}")
        self._hit(f"after_{kind}_intent")
        result = execute()
        self._hit(f"before_{kind}_receipt")
        with self.service.ledger.transaction() as tx:
            tx.history(project_id, "operation.completed", "core_operation", operation_id,
                       {"kind": kind, "request_digest": request_digest,
                        "result_digest": sha256_digest(result), "result": result})
        self._hit(f"after_{kind}_receipt")
        return result
