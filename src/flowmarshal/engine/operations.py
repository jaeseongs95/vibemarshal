from __future__ import annotations

import json
from typing import Any, Callable

from ..canonical import canonical_json, sha256_digest
from .service import EngineService, EngineServiceError


class ExternalOperationUnknown(EngineServiceError):
    pass


class KnownOperationFailed(EngineServiceError):
    """외부 효과의 terminal 실패가 확인되어 같은 operation을 재호출할 수 없음."""

    code = "ROLE_SCHEMA_FAILED"


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

    @staticmethod
    def _role_request(request: dict[str, Any]):
        from .roles import RoleCallRequest

        value = request.get("role_request", request)
        try:
            return RoleCallRequest.model_validate(value)
        except Exception as error:
            raise EngineServiceError(
                "OPERATION_ROLE_REQUEST_INVALID: 저장된 RoleCallRequest가 유효하지 않습니다."
            ) from error

    @staticmethod
    def _validate_schema_failure_binding(
        tx: Any, *, project_id: str, request: dict[str, Any], receipt: Any,
    ) -> tuple[Any, str]:
        """terminal schema failure를 원래 요청·예산 정산 계보에 엄격히 결속한다."""
        from .model_lock import verify_binding
        from .roles import (
            REQUIRED_APPROVAL_POLICY,
            REQUIRED_PERMISSION_PROFILE,
            RoleCallReceipt,
            strict_json_output_schema,
        )

        role_request = CoreOperations._role_request(request)
        try:
            receipt = RoleCallReceipt.model_validate(receipt)
        except Exception as error:
            raise EngineServiceError(
                "OPERATION_ROLE_RECEIPT_INVALID: 저장된 role receipt가 유효하지 않습니다."
            ) from error
        if (
            receipt.status != "schema_failed"
            or receipt.input_digest != role_request.request_digest
            or receipt.output_schema_digest
            != sha256_digest(strict_json_output_schema(role_request.output_schema))
            or receipt.timeout_policy_digest != role_request.timeout_policy_digest
            or (receipt.role, receipt.model, receipt.effort, receipt.inventory_digest)
            != (
                role_request.role,
                role_request.model,
                role_request.effort,
                role_request.inventory_digest,
            )
            or receipt.permission_profile != REQUIRED_PERMISSION_PROFILE
            or receipt.approval_policy != REQUIRED_APPROVAL_POLICY
            or receipt.output_digest is not None
            or receipt.thread_id is None
            or not receipt.turn_ids
        ):
            raise EngineServiceError(
                "OPERATION_ROLE_FAILURE_BINDING_MISMATCH: schema failure가 원래 요청과 다릅니다."
            )
        if role_request.operational_binding is not None:
            if receipt.observed_binding is None:
                raise EngineServiceError("OPERATION_ROLE_FAILURE_INVENTORY_MISSING")
            try:
                observed = verify_binding(
                    role_request.operational_binding,
                    receipt.observed_binding.inventory,
                    role=role_request.role,
                    model=role_request.model,
                    effort=role_request.effort,
                )
            except ValueError as error:
                raise EngineServiceError(
                    "OPERATION_ROLE_FAILURE_INVENTORY_MISMATCH: " + str(error)
                ) from error
            if observed != receipt.observed_binding:
                raise EngineServiceError("OPERATION_ROLE_FAILURE_INVENTORY_MISMATCH")

        receipt_json = canonical_json(receipt)
        rows = tx.all(
            "SELECT c.id,c.role,c.request_digest,c.request_json,c.status,c.receipt_json,c.usage_id,"
            "u.payload_json AS usage_json FROM provider_calls c "
            "LEFT JOIN budget_usage u ON u.id=c.usage_id "
            "WHERE c.project_id=? AND c.receipt_json=?",
            (project_id, receipt_json),
        )
        matches = []
        for row in rows:
            try:
                usage = json.loads(row["usage_json"]) if row["usage_json"] else None
                stored_request = CoreOperations._role_request(
                    json.loads(row["request_json"])
                )
            except (TypeError, ValueError, EngineServiceError):
                usage = None
                stored_request = None
            if (
                row["role"] == receipt.role
                and stored_request == role_request
                and row["request_digest"] == sha256_digest(role_request.model_dump(mode="json"))
                and row["status"] in {"settled", "usage_unknown"}
                and row["usage_id"] is not None
                and isinstance(usage, dict)
                and usage.get("logical_call_ref") == receipt.call_id
                and usage.get("runner_receipt_digest") == sha256_digest(receipt)
                and usage.get("call_status") == "schema_failed"
            ):
                matches.append(row["id"])
        if len(matches) != 1:
            raise EngineServiceError(
                "OPERATION_ROLE_FAILURE_PROVIDER_BINDING_MISMATCH: "
                "유일한 provider 정산 계보가 필요합니다."
            )
        return receipt, matches[0]

    @staticmethod
    def _failed_payload(
        *, kind: str, request_digest: str, receipt: Any, provider_call_id: str,
        error: str, receipts: tuple[Any, ...] = (), observation: Any | None = None,
    ) -> dict[str, Any]:
        value = {
            "kind": kind,
            "request_digest": request_digest,
            "failure_code": "ROLE_SCHEMA_FAILED",
            "error": error,
            "provider_call_id": provider_call_id,
            "receipt_digest": sha256_digest(receipt),
            "receipt": receipt.model_dump(mode="json"),
            "receipts": [item.model_dump(mode="json") for item in receipts],
        }
        if observation is not None:
            value.update({
                "terminal_observation_digest": sha256_digest(observation),
                "terminal_observation": observation.model_dump(mode="json"),
            })
        return value

    def observe_terminal_role_failure(
        self, *, project_id: str, operation_id: str, observation: Any,
    ) -> str:
        """기존 prepared operation의 저장 receipt와 read-only terminal을 실패로 닫는다."""
        from .runtime import RuntimeObservation

        observation = RuntimeObservation.model_validate(observation)
        with self.service.ledger.transaction() as tx:
            project = tx.one("SELECT * FROM projects WHERE id=?", (project_id,))
            rows = tx.all(
                "SELECT sequence,event_type,payload_json FROM history_events "
                "WHERE project_id=? AND entity_type='core_operation' AND entity_id=? "
                "ORDER BY sequence",
                (project_id, operation_id),
            )
            prepared = [row for row in rows if row["event_type"] == "operation.prepared"]
            if not prepared:
                raise EngineServiceError("OPERATION_PREPARED_NOT_FOUND")
            latest = prepared[-1]
            later_terminal = [
                row for row in rows
                if row["sequence"] > latest["sequence"]
                and row["event_type"] in {
                    "operation.completed", "operation.no_effect", "operation.failed"
                }
            ]
            if later_terminal:
                raise EngineServiceError("OPERATION_ALREADY_TERMINAL")
            payload = json.loads(latest["payload_json"])
            kind = payload.get("kind")
            request = payload.get("request")
            request_digest = payload.get("request_digest")
            if (
                not isinstance(kind, str)
                or not isinstance(request, dict)
                or request_digest != sha256_digest({"kind": kind, "request": request})
                or operation_id != "operation_" + sha256_digest({
                    "project_id": project_id, "request_digest": request_digest,
                })[7:39]
            ):
                raise EngineServiceError("OPERATION_PREPARED_BINDING_MISMATCH")
            role_request = self._role_request(request)
            provider_rows = tx.all(
                "SELECT request_json,receipt_json FROM provider_calls WHERE project_id=? "
                "AND receipt_json IS NOT NULL",
                (project_id,),
            )
            candidates = []
            for row in provider_rows:
                try:
                    stored_request = self._role_request(json.loads(row["request_json"]))
                    stored_receipt = json.loads(row["receipt_json"])
                except (TypeError, ValueError, EngineServiceError):
                    continue
                if (
                    stored_request == role_request
                    and stored_receipt.get("status") == "schema_failed"
                    and stored_receipt.get("thread_id") == observation.thread_id
                ):
                    candidates.append(stored_receipt)
            if len(candidates) != 1:
                raise EngineServiceError("OPERATION_ROLE_FAILURE_PROVIDER_BINDING_MISMATCH")
            receipt, provider_call_id = self._validate_schema_failure_binding(
                tx,
                project_id=project_id,
                request=request,
                receipt=candidates[0],
            )
            if (
                observation.active
                or observation.terminal_status not in {"completed", "success", "succeeded"}
                or observation.thread_id != receipt.thread_id
                or observation.turn_id != receipt.turn_ids[-1]
                or observation.final_response is None
            ):
                raise EngineServiceError(
                    "OPERATION_ROLE_FAILURE_TERMINAL_MISMATCH: completed terminal 관측이 다릅니다."
                )
            tx.history(
                project_id,
                "operation.failed",
                "core_operation",
                operation_id,
                self._failed_payload(
                    kind=kind,
                    request_digest=request_digest,
                    receipt=receipt,
                    provider_call_id=provider_call_id,
                    error=receipt.error_summary or "structured output schema failure",
                    observation=observation,
                ),
            )
            unresolved = tx.maybe_one(
                "SELECT p.entity_id FROM history_events p WHERE p.project_id=? "
                "AND p.event_type='operation.prepared' AND NOT EXISTS ("
                "SELECT 1 FROM history_events c WHERE c.project_id=p.project_id "
                "AND c.entity_id=p.entity_id AND c.sequence>p.sequence "
                "AND c.event_type IN ('operation.completed','operation.no_effect','operation.failed')) "
                "LIMIT 1",
                (project_id,),
            )
            unknown_intent = tx.maybe_one(
                "SELECT i.id FROM runtime_intents i JOIN attempts a ON a.id=i.attempt_id "
                "WHERE a.project_id=? AND i.status='unknown' LIMIT 1",
                (project_id,),
            )
            unknown_attempt = tx.maybe_one(
                "SELECT id FROM attempts WHERE project_id=? AND status='unknown' LIMIT 1",
                (project_id,),
            )
            if (
                unresolved is None
                and unknown_intent is None
                and unknown_attempt is None
                and project["run_state"] == "recovery_required"
                and (project["recovery_reason"] or "").startswith("EXTERNAL_EFFECT_UNKNOWN")
            ):
                tx.connection.execute(
                    "UPDATE projects SET run_state=?,recovery_reason=NULL,updated_at=? "
                    "WHERE id=?",
                    (
                        "active" if project["active_plan_revision_id"] is not None else "idle",
                        tx.now,
                        project_id,
                    ),
                )
        return "ROLE_SCHEMA_FAILED"

    def recover_unfinished(self, project_id: str) -> tuple[str, ...]:
        with self.service.ledger.transaction() as tx:
            rows = tx.all(
                "SELECT p.entity_id FROM history_events p WHERE p.project_id = ? "
                "AND p.event_type = 'operation.prepared' AND NOT EXISTS ("
                "SELECT 1 FROM history_events c WHERE c.project_id = p.project_id "
                "AND c.entity_id = p.entity_id AND c.sequence > p.sequence "
                "AND c.event_type IN ('operation.completed','operation.no_effect','operation.failed'))",
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
                if row["event_type"] == "operation.failed":
                    payload = json.loads(row["payload_json"])
                    if payload.get("request_digest") != request_digest:
                        raise EngineServiceError("Core operation 실패 입력 digest가 다릅니다.")
                    raise KnownOperationFailed(
                        f"ROLE_SCHEMA_FAILED: {kind}/{operation_id}"
                    )
            retry_without_effect = bool(rows) and rows[-1]["event_type"] == "operation.no_effect"
            if rows and not retry_without_effect:
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
        try:
            result = execute()
        except Exception as error:
            from .budget import BudgetBlocked
            from .roles import StructuredRoleError
            if (isinstance(error, BudgetBlocked) and error.code in {
                "BUDGET_BLOCKED", "BUDGET_USAGE_UNKNOWN", "PROVIDER_EFFECT_UNKNOWN",
                "BUDGET_POLICY_REQUIRED",
            }) or getattr(error, "effects_started", True) is False:
                with self.service.ledger.transaction() as tx:
                    tx.history(project_id, "operation.no_effect", "core_operation", operation_id,
                               {"request_digest": request_digest, "blocker_code": getattr(error, "code", str(error)), "detail": str(error)})
            elif (
                isinstance(error, StructuredRoleError)
                and error.receipt is not None
                and error.receipt.status == "schema_failed"
            ):
                with self.service.ledger.transaction() as tx:
                    receipt, provider_call_id = self._validate_schema_failure_binding(
                        tx,
                        project_id=project_id,
                        request=request,
                        receipt=error.receipt,
                    )
                    if not error.receipts or error.receipts[-1] != receipt:
                        raise EngineServiceError(
                            "OPERATION_ROLE_FAILURE_RECEIPT_HISTORY_MISMATCH"
                        ) from error
                    tx.history(
                        project_id,
                        "operation.failed",
                        "core_operation",
                        operation_id,
                        self._failed_payload(
                            kind=kind,
                            request_digest=request_digest,
                            receipt=receipt,
                            provider_call_id=provider_call_id,
                            error=str(error),
                            receipts=error.receipts,
                        ),
                    )
            raise
        self._hit(f"before_{kind}_receipt")
        with self.service.ledger.transaction() as tx:
            tx.history(project_id, "operation.completed", "core_operation", operation_id,
                       {"kind": kind, "request_digest": request_digest,
                        "result_digest": sha256_digest(result), "result": result})
        self._hit(f"after_{kind}_receipt")
        return result
