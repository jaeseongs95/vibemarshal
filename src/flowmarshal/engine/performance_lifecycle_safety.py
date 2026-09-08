"""lifecycle의 실제 runtime 효과와 Core의 승인·복구 근거를 읽기 전용으로 대조한다."""
from __future__ import annotations

import json
from typing import Any

from ..canonical import sha256_digest
from .domain import RuntimeReceipt


def _json(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def audit_runtime_lifecycle(
    snapshot: dict[str, Any],
    trace_rows: list[dict[str, Any]],
) -> tuple[int, list[str], list[str], dict[str, Any]]:
    """실제 runtime 효과를 append-only intent/receipt/History/trace에 교차 결속한다."""

    failures: list[str] = []
    missing: list[str] = []
    unapproved = 0
    attempts = {row["id"]: row for row in snapshot["attempts"]}
    provider_calls = {row["id"]: row for row in snapshot["provider_calls"]}
    intents = {row["id"]: row for row in snapshot["runtime_intents"]}
    receipt_rows = {row["intent_id"]: row for row in snapshot["runtime_receipts"]}
    history = snapshot["history_events"]
    history_by_entity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in history:
        history_by_entity.setdefault((event["event_type"], event["entity_id"]), []).append(event)

    def history_payload(event: dict[str, Any]) -> dict[str, Any]:
        value = _json(event["payload_json"])
        if not isinstance(value, dict):
            raise ValueError("History payload가 object가 아닙니다.")
        return value

    def one_history(event_type: str, entity_id: str) -> dict[str, Any] | None:
        rows = history_by_entity.get((event_type, entity_id), [])
        if len(rows) != 1:
            failures.append(
                "PERFORMANCE_RUNTIME_HISTORY_COVERAGE_MISMATCH:"
                f"{event_type}:{entity_id}:{len(rows)}"
            )
            return None
        return rows[0]

    parsed_receipts: dict[str, RuntimeReceipt] = {}
    for intent_id, row in receipt_rows.items():
        intent = intents.get(intent_id)
        if intent is None:
            failures.append("PERFORMANCE_RUNTIME_RECEIPT_ORPHAN")
            continue
        try:
            receipt = RuntimeReceipt.model_validate_json(row["payload_json"])
        except ValueError:
            failures.append("PERFORMANCE_RUNTIME_RECEIPT_INVALID")
            continue
        binding = None if row["binding_json"] is None else _json(row["binding_json"])
        if (
            receipt.receipt_id != row["id"]
            or receipt.intent_id != intent_id
            or receipt.provider_operation_id != row["provider_operation_id"]
            or receipt.response_digest != row["response_digest"]
            or (
                None if receipt.binding is None
                else receipt.binding.model_dump(mode="json", exclude_none=True)
            ) != binding
        ):
            failures.append("PERFORMANCE_RUNTIME_RECEIPT_COLUMN_BINDING_MISMATCH")
            continue
        # RuntimeReceipt validator가 create/start response body digest도 검증한다.
        if intent["kind"] in {"create_thread", "start_turn"} and receipt.response_payload is None:
            missing.append("PERFORMANCE_RUNTIME_RECEIPT_BODY_MISSING")
        parsed_receipts[intent_id] = receipt

        recorded = one_history("runtime.receipt_recorded", row["id"])
        if recorded is not None:
            payload = history_payload(recorded)
            if (
                payload.get("intent_id") != intent_id
                or payload.get("provider_operation_id") != receipt.provider_operation_id
            ):
                failures.append("PERFORMANCE_RUNTIME_RECEIPT_HISTORY_BINDING_MISMATCH")

    intent_order = {row["id"]: index for index, row in enumerate(snapshot["runtime_intents"])}
    receipt_order = {
        row["intent_id"]: index for index, row in enumerate(snapshot["runtime_receipts"])
    }
    by_attempt: dict[str, list[dict[str, Any]]] = {}
    for intent in snapshot["runtime_intents"]:
        by_attempt.setdefault(intent["attempt_id"], []).append(intent)
        attempt = attempts.get(intent["attempt_id"])
        if attempt is None:
            failures.append("PERFORMANCE_RUNTIME_INTENT_ATTEMPT_MISSING")
            continue
        request = _json(intent["request_json"])
        if not isinstance(request, dict) or sha256_digest(request) != intent["request_digest"]:
            failures.append("PERFORMANCE_RUNTIME_INTENT_DIGEST_MISMATCH")
        prepared = one_history("runtime.intent_prepared", intent["id"])
        if prepared is not None:
            payload = history_payload(prepared)
            if (
                payload.get("attempt_id") != intent["attempt_id"]
                or payload.get("kind") != intent["kind"]
                or payload.get("request_digest") != intent["request_digest"]
            ):
                failures.append("PERFORMANCE_RUNTIME_INTENT_HISTORY_BINDING_MISMATCH")
        has_receipt = intent["id"] in parsed_receipts
        if intent["status"] == "received" and not has_receipt:
            missing.append("PERFORMANCE_RUNTIME_RECEIPT_MISSING")
        if intent["status"] != "received" and has_receipt:
            failures.append("PERFORMANCE_RUNTIME_RECEIPT_STATUS_MISMATCH")

    kind_for_trace = {
        "create": "create_thread",
        "start": "start_turn",
        "resume": "resume_turn",
    }
    trace_by_intent: dict[str, list[dict[str, Any]]] = {}
    role_call_keys = {
        call["call_key"] for call in snapshot["provider_calls"]
        if isinstance(receipt := _json(call["receipt_json"]), dict)
        and "role" in receipt and "call_id" in receipt
    }
    lifecycle_actual_rows = [
        row for row in trace_rows
        if row.get("kind") in kind_for_trace
        and row.get("parent_operation_id") is None
        and row.get("category") in {"logical", "rpc"}
        and not (row.get("call_id") in role_call_keys and row.get("attempt_id") is None)
    ]
    for row in lifecycle_actual_rows:
        intent_id = row.get("intent_id")
        intent = intents.get(intent_id) if isinstance(intent_id, str) else None
        if (
            intent is None
            or intent["kind"] != kind_for_trace[row["kind"]]
            or intent["status"] != "received"
            or intent["attempt_id"] != row.get("attempt_id")
        ):
            unapproved += 1
            failures.append(
                "PERFORMANCE_RUNTIME_OPERATION_WITHOUT_RECEIVED_INTENT:"
                f"{row.get('operation_id')}"
            )
            continue
        trace_by_intent.setdefault(intent_id, []).append(row)
        call = provider_calls.get(row.get("call_id"))
        if call is None or call.get("attempt_id") != intent["attempt_id"]:
            failures.append("PERFORMANCE_RUNTIME_TRACE_PROVIDER_CALL_BINDING_MISMATCH")
        request = _json(intent["request_json"])
        if row["kind"] in {"create", "start"}:
            if row.get("request_digest") != intent["request_digest"]:
                failures.append("PERFORMANCE_RUNTIME_TRACE_INTENT_DIGEST_MISMATCH")
        elif not isinstance(request, dict) or (
            row.get("request", {}).get("thread_id") != request.get("thread_id")
            or row.get("request", {}).get("cwd") != request.get("cwd")
        ):
            failures.append("PERFORMANCE_RUNTIME_RESUME_REQUEST_BINDING_MISMATCH")

        receipt = parsed_receipts.get(intent_id)
        response = row.get("response")
        operation_id = response.get("operation_id") if isinstance(response, dict) else None
        expected_provider_operation_id = (
            f"resume:{operation_id}:{intent['attempt_id']}"
            if row["kind"] == "resume" and isinstance(operation_id, str)
            else operation_id
        )
        if receipt is None or receipt.provider_operation_id != expected_provider_operation_id:
            failures.append("PERFORMANCE_RUNTIME_TRACE_RECEIPT_OPERATION_MISMATCH")
        elif receipt.binding is not None and (
            row.get("thread_id") != receipt.binding.thread_id
            or row.get("turn_id") != receipt.binding.turn_id
        ):
            failures.append("PERFORMANCE_RUNTIME_TRACE_RECEIPT_BINDING_MISMATCH")

    for intent in intents.values():
        if intent["kind"] not in {"create_thread", "start_turn", "resume_turn"}:
            continue
        if intent["status"] == "received" and len(trace_by_intent.get(intent["id"], [])) != 1:
            missing.append("PERFORMANCE_RUNTIME_INTENT_TRACE_COVERAGE_MISMATCH")

    for attempt_id, rows in by_attempt.items():
        rows.sort(key=lambda item: intent_order[item["id"]])
        received = [
            row for row in rows
            if row["status"] == "received"
            and row["kind"] in {"create_thread", "start_turn", "resume_turn"}
        ]
        kinds = [row["kind"] for row in received]
        if not received:
            continue
        # 정상 dispatch: create,start. 정상 1회 resume: create,start,resume,start.
        complete_sequences = (
            ["create_thread", "start_turn"],
            ["create_thread", "start_turn", "resume_turn", "start_turn"],
        )
        valid_prefix = any(kinds == sequence[:len(kinds)] for sequence in complete_sequences)
        if not valid_prefix:
            unapproved += max(1, kinds.count("resume_turn") + max(0, kinds.count("start_turn") - 1))
            failures.append(f"PERFORMANCE_RUNTIME_INTENT_SEQUENCE_INVALID:{attempt_id}")
            continue
        if kinds not in complete_sequences:
            missing.append(f"PERFORMANCE_RUNTIME_INTENT_SEQUENCE_INCOMPLETE:{attempt_id}")
        receipts_for_attempt = [
            receipt_order[row["id"]] for row in received if row["id"] in receipt_order
        ]
        if receipts_for_attempt != sorted(receipts_for_attempt):
            failures.append(f"PERFORMANCE_RUNTIME_RECEIPT_SEQUENCE_INVALID:{attempt_id}")
        parsed = [parsed_receipts.get(row["id"]) for row in received]
        bindings = [receipt.binding for receipt in parsed if receipt is not None and receipt.binding]
        thread_ids = {binding.thread_id for binding in bindings}
        if len(thread_ids) != 1:
            failures.append(f"PERFORMANCE_RUNTIME_THREAD_LINEAGE_MISMATCH:{attempt_id}")
        start_turn_ids = [
            parsed_receipts[row["id"]].binding.turn_id
            for row in received
            if row["kind"] == "start_turn"
            and row["id"] in parsed_receipts
            and parsed_receipts[row["id"]].binding is not None
        ]
        if any(turn_id is None for turn_id in start_turn_ids) or len(start_turn_ids) != len(set(start_turn_ids)):
            failures.append(f"PERFORMANCE_RUNTIME_START_TURN_BINDING_INVALID:{attempt_id}")

    attempts_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for attempt in snapshot["attempts"]:
        attempts_by_key.setdefault((attempt["task_id"], attempt["kind"]), []).append(attempt)

    def validation_id_for(attempt_id: str) -> str | None:
        values = {
            request.get("validation_id")
            for intent in by_attempt.get(attempt_id, [])
            if isinstance(request := _json(intent["request_json"]), dict)
            and isinstance(request.get("validation_id"), str)
        }
        if len(values) > 1:
            failures.append(f"PERFORMANCE_VALIDATOR_ID_BINDING_CONFLICT:{attempt_id}")
            return None
        return next(iter(values), None)

    retry_events = [event for event in history if event["event_type"] == "task.retry_enabled"]
    used_retry_event_ids: set[str] = set()
    for (task_id, kind), rows in attempts_by_key.items():
        rows.sort(key=lambda row: row["attempt_no"])
        if [row["attempt_no"] for row in rows] != list(range(1, len(rows) + 1)):
            failures.append(f"PERFORMANCE_ATTEMPT_NUMBER_GAP:{task_id}:{kind}")
        for previous, current in zip(rows, rows[1:]):
            current_reserved = one_history("attempt.reserved", current["id"])
            previous_reserved = one_history("attempt.reserved", previous["id"])
            if current_reserved is None or previous_reserved is None:
                unapproved += 1
                continue
            if kind == "validation":
                candidates = [
                    event for event in history
                    if event["event_type"] == "model.rebound"
                    and history_payload(event).get("attempt_id") == current["id"]
                ]
                previous_validation_id = validation_id_for(previous["id"])
                current_validation_id = validation_id_for(current["id"])
                if not candidates and (
                    previous_validation_id is not None
                    and current_validation_id is not None
                    and previous_validation_id != current_validation_id
                ):
                    # 한 Task의 서로 다른 semantic validation 단계는 각각 새 Attempt다.
                    continue
                if len(candidates) != 1:
                    unapproved += 1
                    failures.append("PERFORMANCE_VALIDATOR_RETRY_REBIND_MISSING")
                else:
                    payload = history_payload(candidates[0])
                    if (
                        payload.get("task_id") != task_id
                        or payload.get("attempt_kind") != "validation"
                        or payload.get("new_execution_spec_digest") != current["execution_spec_digest"]
                        or not (
                            previous_reserved["sequence"]
                            < candidates[0]["sequence"]
                            < current_reserved["sequence"]
                        )
                    ):
                        unapproved += 1
                        failures.append("PERFORMANCE_VALIDATOR_RETRY_REBIND_INVALID")
                continue

            candidates = [
                event for event in retry_events
                if event["entity_id"] == task_id
                and history_payload(event).get("previous_attempt_id") == previous["id"]
                and previous_reserved["sequence"] < event["sequence"] < current_reserved["sequence"]
            ]
            if len(candidates) != 1:
                unapproved += 1
                failures.append("PERFORMANCE_EXECUTION_RETRY_AUTHORIZATION_MISSING")
                continue
            retry = candidates[0]
            used_retry_event_ids.add(retry["id"])
            payload = history_payload(retry)
            if payload.get("failure_class") != previous.get("failure_class") and previous.get("failure_class") is not None:
                unapproved += 1
                failures.append("PERFORMANCE_EXECUTION_RETRY_FAILURE_BINDING_MISMATCH")
            if previous["attempt_no"] > 1 and not payload.get("new_evidence_ids"):
                unapproved += 1
                failures.append("PERFORMANCE_REPEAT_RETRY_NEW_EVIDENCE_MISSING")
            assessment_id = payload.get("recovery_assessment_id")
            if previous.get("failure_class") is not None:
                if assessment_id is not None:
                    failures.append("PERFORMANCE_FAILED_ATTEMPT_RETRY_HAS_UNEXPECTED_ASSESSMENT")
            else:
                assessment = next(
                    (row for row in snapshot["recovery_assessments"] if row["id"] == assessment_id),
                    None,
                )
                assessed = (
                    None if assessment is None
                    else one_history("recovery.assessed", assessment["id"])
                )
                assessment_body = (
                    None if assessment is None else _json(assessment["payload_json"])
                )
                assessed_payload = (
                    None if assessed is None else history_payload(assessed)
                )
                if (
                    previous["status"] != "succeeded"
                    or not isinstance(payload.get("failed_validation_result_id"), str)
                    or assessment is None
                    or assessment["attempt_id"] != previous["id"]
                    or assessment["failure_class"] != payload.get("failure_class")
                    or not isinstance(assessment_body, dict)
                    or assessment_body.get("assessment_id") != assessment["id"]
                    or assessment_body.get("attempt_id") != previous["id"]
                    or assessment_body.get("failure_class") != assessment["failure_class"]
                    or assessed is None
                    or assessed_payload.get("attempt_id") != previous["id"]
                    or assessed_payload.get("action") != assessment["action"]
                    or not (
                        previous_reserved["sequence"]
                        < assessed["sequence"]
                        < retry["sequence"]
                    )
                ):
                    unapproved += 1
                    failures.append("PERFORMANCE_VALIDATION_RECOVERY_BINDING_INVALID")

    # 승인 event만 있고 다음 execution Attempt가 없는 경우도 완결된 lifecycle 증거가 아니다.
    orphan_retry_ids = {row["id"] for row in retry_events} - used_retry_event_ids
    if orphan_retry_ids:
        missing.append("PERFORMANCE_RETRY_WITHOUT_SUBSEQUENT_ATTEMPT")

    evidence = {
        "runtime_intent_count": len(intents),
        "runtime_receipt_count": len(receipt_rows),
        "trace_lifecycle_operation_count": len(lifecycle_actual_rows),
        "execution_retry_event_count": len(retry_events),
        "unapproved_count": unapproved,
    }
    return unapproved, failures, missing, evidence
