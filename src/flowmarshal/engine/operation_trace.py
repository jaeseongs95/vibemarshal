from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..canonical import canonical_json, json_value, sha256_digest
from .domain import new_id, utc_now


TRACE_SCHEMA_VERSION = "flowmarshal-operation-trace-v1"
OPERATION_KINDS = frozenset({"create", "start", "read", "resume", "interrupt", "sdk_wait"})
TERMINAL_STATUSES = frozenset({"ok", "error", "deadline_exceeded"})
OPERATION_CATEGORIES = frozenset({"logical", "rpc", "wait"})
RPC_METHOD_KINDS = {
    "config/read": "read",
    "model/list": "read",
    "permissionProfile/list": "read",
    "project/read": "read",
    "sdk.thread/read": "read",
    "thread/read": "read",
    "thread/turns/list": "read",
    "thread/start": "create",
    "sdk.thread/start": "create",
    "turn/start": "start",
    "sdk.turn/start": "start",
    "thread/resume": "resume",
    "sdk.thread/resume": "resume",
    "turn/interrupt": "interrupt",
    "sdk.turn/interrupt": "interrupt",
}


@dataclass(frozen=True)
class OperationToken:
    trace_id: str
    operation_id: str
    sequence: int


@dataclass(frozen=True)
class OperationTraceScope:
    trace: "OperationTrace"
    parent: OperationToken
    deadline_monotonic_ns: int | None
    deadline_at: datetime | None
    call_id: str | None
    attempt_id: str | None
    intent_id: str | None
    thread_id: str | None
    turn_id: str | None


_ACTIVE_OPERATION_TRACE_SCOPE: ContextVar[OperationTraceScope | None] = ContextVar(
    "flowmarshal_active_operation_trace_scope", default=None
)


def current_operation_trace_scope() -> OperationTraceScope | None:
    return _ACTIVE_OPERATION_TRACE_SCOPE.get()


@contextmanager
def use_operation_trace_scope(scope: OperationTraceScope):
    token: Token[OperationTraceScope | None] = _ACTIVE_OPERATION_TRACE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_OPERATION_TRACE_SCOPE.reset(token)


@dataclass(frozen=True)
class TraceVerification:
    valid: bool
    complete: bool
    errors: tuple[str, ...]
    manifest: dict[str, Any]


def _utc_text(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("operation trace UTC 시각에는 timezone이 필요합니다.")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _error_document(error: BaseException | Mapping[str, Any] | str | None) -> dict[str, Any] | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return {"type": type(error).__name__, "message": str(error)}
    if isinstance(error, Mapping):
        return json_value(dict(error))
    return {"type": "Error", "message": str(error)}


def _normalize_expected(values: Iterable[str] | None) -> tuple[str, ...]:
    normalized = tuple(values or ())
    unknown = set(normalized) - OPERATION_KINDS
    if unknown:
        raise ValueError(f"지원하지 않는 operation kind입니다: {sorted(unknown)}")
    return normalized


def _row_without_digest(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "row_digest"}


def _manifest_for(
    *,
    trace_id: str,
    context: Mapping[str, Any],
    rows: list[dict[str, Any]],
    expected_operations: Iterable[str],
    sealed: bool,
) -> dict[str, Any]:
    expected = Counter(expected_operations)
    actual = Counter(str(row.get("kind")) for row in rows if row.get("parent_operation_id") is None)
    rpc_actual = Counter(str(row.get("kind")) for row in rows if row.get("category") == "rpc")
    logical_actual = Counter(str(row.get("kind")) for row in rows if row.get("category") == "logical")
    missing_counts = {
        kind: count - actual.get(kind, 0)
        for kind, count in sorted(expected.items())
        if actual.get(kind, 0) < count
    }
    calls: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        call_key = row.get("call_id") or row.get("provider_call_id")
        if call_key is None:
            continue
        calls.setdefault(str(call_key), []).append(
            {
                "operation_id": row.get("operation_id"),
                "kind": row.get("kind"),
                "category": row.get("category"),
                "parent_operation_id": row.get("parent_operation_id"),
                "rpc_method": row.get("rpc_method"),
                "detached": row.get("detached"),
                "status": row.get("status"),
                "provider_call_id": row.get("provider_call_id"),
                "attempt_id": row.get("attempt_id"),
                "intent_id": row.get("intent_id"),
                "thread_id": row.get("thread_id"),
                "turn_id": row.get("turn_id"),
            }
        )
    row_digests = [row.get("row_digest") for row in rows]
    return {
        "trace_id": trace_id,
        "context_digest": sha256_digest(context),
        "expected_operation_counts": dict(sorted(expected.items())),
        "actual_started_counts": dict(sorted(actual.items())),
        "actual_rpc_counts": dict(sorted(rpc_actual.items())),
        "logical_operation_counts": dict(sorted(logical_actual.items())),
        "row_count": len(rows),
        "trace_digest": sha256_digest(
            {"trace_id": trace_id, "context_digest": sha256_digest(context), "row_digests": row_digests}
        ),
        "call_bindings": {key: value for key, value in sorted(calls.items())},
        "pending_operation_ids": [row["operation_id"] for row in rows if row.get("status") == "pending"],
        "missing_operation_counts": missing_counts,
        "error_operation_ids": [row["operation_id"] for row in rows if row.get("status") == "error"],
        "deadline_exceeded_operation_ids": [
            row["operation_id"] for row in rows if row.get("status") == "deadline_exceeded"
        ],
        "late_operation_ids": [row["operation_id"] for row in rows if row.get("late") is True],
        "post_seal_operation_ids": [
            row["operation_id"] for row in rows if row.get("after_seal") is True
        ],
        "rpc_coverage_missing_operation_ids": [
            row["operation_id"]
            for row in rows
            if row.get("category") == "logical"
            and not any(
                child.get("category") == "rpc"
                and child.get("parent_operation_id") == row.get("operation_id")
                and child.get("kind") == row.get("kind")
                for child in rows
            )
        ],
        "sealed": sealed,
    }


def recalculate_trace_manifest(
    document_or_path: Mapping[str, Any] | Path | str,
    *,
    expected_operations: Iterable[str] | None = None,
) -> dict[str, Any]:
    document = _load_document(document_or_path)
    expected = (
        _normalize_expected(expected_operations)
        if expected_operations is not None
        else _normalize_expected(document.get("expected_operations", ()))
    )
    return _manifest_for(
        trace_id=str(document.get("trace_id", "")),
        context=dict(document.get("context") or {}),
        rows=[dict(row) for row in document.get("rows") or ()],
        expected_operations=expected,
        sealed=bool(document.get("manifest", {}).get("sealed", False)),
    )


def verify_operation_trace(
    document_or_path: Mapping[str, Any] | Path | str,
    *,
    expected_operations: Iterable[str] | None = None,
    expected_call_ids: Iterable[str] | None = None,
) -> TraceVerification:
    try:
        document = _load_document(document_or_path)
    except Exception as error:
        return TraceVerification(
            valid=False, complete=False,
            errors=(f"TRACE_DOCUMENT_INVALID:{type(error).__name__}:{error}",),
            manifest={},
        )
    errors: list[str] = []
    if document.get("schema_version") != TRACE_SCHEMA_VERSION:
        errors.append("TRACE_SCHEMA_VERSION_MISMATCH")
    rows = [dict(row) for row in document.get("rows") or ()]
    operation_ids: set[str] = set()
    prior_rows: dict[str, dict[str, Any]] = {}
    previous_digest: str | None = None
    for index, row in enumerate(rows, 1):
        operation_id = row.get("operation_id")
        if row.get("sequence") != index:
            errors.append(f"TRACE_SEQUENCE_INVALID:{operation_id}")
        if not isinstance(operation_id, str) or operation_id in operation_ids:
            errors.append(f"TRACE_OPERATION_ID_DUPLICATE:{operation_id}")
        elif operation_id:
            operation_ids.add(operation_id)
        if row.get("kind") not in OPERATION_KINDS:
            errors.append(f"TRACE_KIND_INVALID:{operation_id}")
        category = row.get("category")
        if category not in OPERATION_CATEGORIES:
            errors.append(f"TRACE_CATEGORY_INVALID:{operation_id}")
        parent_operation_id = row.get("parent_operation_id")
        if parent_operation_id is not None and parent_operation_id not in operation_ids:
            errors.append(f"TRACE_PARENT_INVALID:{operation_id}")
        if category == "rpc" and not isinstance(row.get("rpc_method"), str):
            errors.append(f"TRACE_RPC_METHOD_MISSING:{operation_id}")
        if category != "rpc" and row.get("rpc_method") is not None:
            errors.append(f"TRACE_RPC_METHOD_UNEXPECTED:{operation_id}")
        rpc_method = row.get("rpc_method")
        expected_kind = (
            str(rpc_method).removeprefix("port.")
            if isinstance(rpc_method, str) and rpc_method.startswith("port.")
            else RPC_METHOD_KINDS.get(rpc_method)
        )
        if category == "rpc" and expected_kind in OPERATION_KINDS and row.get("kind") != expected_kind:
            errors.append(f"TRACE_RPC_KIND_MISMATCH:{operation_id}")
        if row.get("detached") is True and (category != "rpc" or parent_operation_id is not None):
            errors.append(f"TRACE_DETACHED_INVALID:{operation_id}")
        parent = prior_rows.get(parent_operation_id)
        if parent is not None:
            if category == "wait" or parent.get("category") == "rpc":
                errors.append(f"TRACE_PARENT_CATEGORY_INVALID:{operation_id}")
            for key in ("call_id", "attempt_id", "intent_id"):
                if row.get(key) != parent.get(key):
                    errors.append(f"TRACE_PARENT_BINDING_MISMATCH:{operation_id}:{key}")
            if row.get("deadline_at") is not None and parent.get("deadline_at") is not None:
                child_deadline = datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00"))
                parent_deadline = datetime.fromisoformat(parent["deadline_at"].replace("Z", "+00:00"))
                if child_deadline > parent_deadline:
                    errors.append(f"TRACE_CHILD_DEADLINE_EXCEEDS_PARENT:{operation_id}")
        elif category == "rpc" and row.get("detached") is not True and any(
            candidate.get("category") == "logical"
            and candidate.get("kind") == row.get("kind")
            and candidate.get("call_id") == row.get("call_id")
            and candidate.get("attempt_id") == row.get("attempt_id")
            and candidate.get("intent_id") == row.get("intent_id")
            for candidate in prior_rows.values()
        ):
            errors.append(f"TRACE_RPC_PARENT_MISSING:{operation_id}")
        if row.get("status") not in TERMINAL_STATUSES | {"pending"}:
            errors.append(f"TRACE_STATUS_INVALID:{operation_id}")
        if row.get("request_digest") != sha256_digest(row.get("request")):
            errors.append(f"TRACE_REQUEST_DIGEST_MISMATCH:{operation_id}")
        if row.get("response") is not None:
            if row.get("response_digest") != sha256_digest(row.get("response")):
                errors.append(f"TRACE_RESPONSE_DIGEST_MISMATCH:{operation_id}")
        elif row.get("response_digest") is not None:
            errors.append(f"TRACE_RESPONSE_DIGEST_WITHOUT_BODY:{operation_id}")
        if row.get("previous_row_digest") != previous_digest:
            errors.append(f"TRACE_CHAIN_PREVIOUS_MISMATCH:{operation_id}")
        calculated_row_digest = sha256_digest(_row_without_digest(row))
        if row.get("row_digest") != calculated_row_digest:
            errors.append(f"TRACE_ROW_DIGEST_MISMATCH:{operation_id}")
        previous_digest = row.get("row_digest")
        if isinstance(operation_id, str):
            prior_rows[operation_id] = row
    recalculated = recalculate_trace_manifest(document, expected_operations=expected_operations)
    recorded = dict(document.get("manifest") or {})
    for key in (
        "trace_id", "context_digest", "expected_operation_counts", "actual_started_counts",
        "actual_rpc_counts", "logical_operation_counts",
        "row_count", "trace_digest", "call_bindings", "pending_operation_ids",
        "missing_operation_counts", "error_operation_ids", "deadline_exceeded_operation_ids",
        "late_operation_ids", "sealed",
        "post_seal_operation_ids",
        "rpc_coverage_missing_operation_ids",
    ):
        if recorded.get(key) != recalculated.get(key):
            errors.append(f"TRACE_MANIFEST_MISMATCH:{key}")
    for call_id in expected_call_ids or ():
        if call_id not in recalculated["call_bindings"]:
            errors.append(f"TRACE_CALL_BINDING_MISSING:{call_id}")
    complete = (
        not errors
        and recalculated["sealed"]
        and not recalculated["pending_operation_ids"]
        and not recalculated["missing_operation_counts"]
        and not recalculated["error_operation_ids"]
        and not recalculated["deadline_exceeded_operation_ids"]
        and not recalculated["post_seal_operation_ids"]
        and not recalculated["rpc_coverage_missing_operation_ids"]
    )
    return TraceVerification(valid=not errors, complete=complete, errors=tuple(errors), manifest=recalculated)


def _load_document(document_or_path: Mapping[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(document_or_path, Mapping):
        return json_value(dict(document_or_path))
    path = Path(document_or_path)
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not events or events[0].get("event") != "header":
        raise ValueError("operation trace header가 없습니다.")
    header = events[0]
    begins: dict[str, dict[str, Any]] = {}
    finishes: dict[str, dict[str, Any]] = {}
    sealed = False
    for event in events[1:]:
        event_type = event.get("event")
        operation_id = event.get("operation_id")
        if event.get("trace_id") != header.get("trace_id"):
            raise ValueError("operation trace event의 trace_id가 header와 다릅니다.")
        if sealed and event_type != "reopen":
            raise ValueError("seal 뒤에 operation trace event가 추가됐습니다.")
        if event_type == "reopen":
            sealed = False
        elif event_type == "begin":
            if operation_id in begins:
                raise ValueError(f"operation begin이 중복됐습니다: {operation_id}")
            begins[operation_id] = event
        elif event_type == "finish":
            if operation_id not in begins:
                raise ValueError(f"begin 없는 operation finish입니다: {operation_id}")
            if operation_id in finishes:
                raise ValueError(f"operation finish가 중복됐습니다: {operation_id}")
            if event.get("sequence") != begins[operation_id].get("sequence"):
                raise ValueError(f"operation finish sequence가 begin과 다릅니다: {operation_id}")
            finishes[operation_id] = event
        elif event_type == "seal":
            sealed = True
        else:
            raise ValueError(f"알 수 없는 operation trace event입니다: {event_type}")
    rows: list[dict[str, Any]] = []
    previous_digest: str | None = None
    for begin in sorted(begins.values(), key=lambda item: item["sequence"]):
        finish = finishes.get(begin["operation_id"])
        row = _project_row(begin, finish, previous_digest=previous_digest)
        previous_digest = row["row_digest"]
        rows.append(row)
    sealed_expected = None
    for event in reversed(events):
        if event.get("event") == "seal" and "expected_operations" in event:
            sealed_expected = event["expected_operations"]
            break
    document = {
        "schema_version": header["schema_version"],
        "trace_id": header["trace_id"],
        "context": header["context"],
        "expected_operations": (
            header.get("expected_operations", []) if sealed_expected is None else sealed_expected
        ),
        "rows": rows,
    }
    document["manifest"] = _manifest_for(
        trace_id=document["trace_id"], context=document["context"], rows=rows,
        expected_operations=document["expected_operations"], sealed=sealed,
    )
    return document


def _project_row(
    begin: Mapping[str, Any], finish: Mapping[str, Any] | None, *, previous_digest: str | None,
) -> dict[str, Any]:
    row = {
        key: begin.get(key)
        for key in (
            "operation_id", "sequence", "kind", "call_id", "provider_call_id", "attempt_id",
            "intent_id", "thread_id", "turn_id", "started_at", "started_monotonic_ns",
            "deadline_seconds", "deadline_at", "request", "request_digest", "after_seal",
            "category", "parent_operation_id", "rpc_method",
            "detached",
        )
    }
    row.update(
        {
            "status": "pending" if finish is None else finish["status"],
            "finished_at": None if finish is None else finish["finished_at"],
            "elapsed_ms": None if finish is None else finish["elapsed_ms"],
            "response": None if finish is None else finish.get("response"),
            "response_digest": None if finish is None else finish.get("response_digest"),
            "error": None if finish is None else finish.get("error"),
            "late": False if finish is None else finish.get("late", False),
            "previous_row_digest": previous_digest,
        }
    )
    if finish is not None:
        for key in ("provider_call_id", "attempt_id", "intent_id", "thread_id", "turn_id"):
            if finish.get(key) is not None:
                row[key] = finish[key]
    row["row_digest"] = sha256_digest(row)
    return row


class OperationTrace:
    """실제 provider/SDK 작업을 append-only event와 완전성 manifest로 기록한다."""

    def __init__(
        self,
        context: Mapping[str, Any],
        path: Path | str | None = None,
        expected_operations: Iterable[str] = (),
        digest_chain: bool = True,
    ) -> None:
        self.trace_id = new_id("operation_trace")
        self.context = json_value(dict(context))
        self.expected_operations = _normalize_expected(expected_operations)
        self.digest_chain = bool(digest_chain)
        self.path = None if path is None else Path(path)
        self._events: list[dict[str, Any]] = []
        self._active: dict[str, dict[str, Any]] = {}
        self._sealed = False
        self._reopened = False
        self._lock = threading.RLock()
        header = {
            "event": "header", "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": self.trace_id, "context": self.context,
            "expected_operations": list(self.expected_operations), "digest_chain": self.digest_chain,
            "recorded_at": _utc_text(),
        }
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(canonical_json(header) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self._events.append(header)

    @classmethod
    def from_path(cls, path: Path | str) -> "OperationTrace":
        source = Path(path)
        events = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not events or events[0].get("event") != "header":
            raise ValueError("operation trace header가 없습니다.")
        header = events[0]
        document = _load_document(source)
        trace = cls.__new__(cls)
        trace.trace_id = header["trace_id"]
        trace.context = header["context"]
        trace.expected_operations = _normalize_expected(document.get("expected_operations", ()))
        trace.digest_chain = bool(header.get("digest_chain", True))
        trace.path = source
        trace._events = events
        trace._active = {
            event["operation_id"]: event for event in events if event.get("event") == "begin"
        }
        trace._sealed = bool(document["manifest"]["sealed"])
        trace._reopened = any(event.get("event") == "reopen" for event in events)
        trace._lock = threading.RLock()
        verification = verify_operation_trace(source)
        if not verification.valid:
            raise ValueError("operation trace 무결성 실패: " + ", ".join(verification.errors))
        return trace

    def _append(self, event: dict[str, Any]) -> None:
        if self.path is not None:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(canonical_json(event) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self._events.append(event)

    def begin(
        self,
        kind: str,
        request: Any,
        *,
        call_id: str | None = None,
        provider_call_id: str | None = None,
        attempt_id: str | None = None,
        intent_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        deadline_seconds: float | None = None,
        deadline_at: datetime | str | None = None,
        category: str = "rpc",
        parent_operation_id: str | None = None,
        rpc_method: str | None = None,
        detached: bool = False,
    ) -> OperationToken:
        if kind not in OPERATION_KINDS:
            raise ValueError(f"지원하지 않는 operation kind입니다: {kind}")
        if deadline_seconds is not None and deadline_seconds < 0:
            raise ValueError("deadline_seconds는 0 이상이어야 합니다.")
        if category not in OPERATION_CATEGORIES:
            raise ValueError(f"지원하지 않는 operation category입니다: {category}")
        if category == "rpc" and not isinstance(rpc_method, str):
            rpc_method = f"port.{kind}"
        if category != "rpc" and rpc_method is not None:
            raise ValueError("rpc_method는 rpc category에만 기록할 수 있습니다.")
        if detached and (category != "rpc" or parent_operation_id is not None):
            raise ValueError("detached RPC는 parent 없는 rpc category여야 합니다.")
        with self._lock:
            if self._sealed:
                prior = self.snapshot()["manifest"]
                self._append({
                    "event": "reopen", "trace_id": self.trace_id,
                    "prior_trace_digest": prior["trace_digest"],
                    "recorded_at": _utc_text(),
                })
                self._sealed = False
                self._reopened = True
            sequence = len(self._active) + 1
            operation_id = new_id("operation")
            started_at = utc_now()
            started_monotonic_ns = time.monotonic_ns()
            if isinstance(deadline_at, str):
                deadline_at = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
            if deadline_at is not None:
                if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
                    raise ValueError("deadline_at에는 timezone이 필요합니다.")
                shared_remaining = max(0.0, (deadline_at - started_at).total_seconds())
                deadline_seconds = (
                    shared_remaining if deadline_seconds is None
                    else min(deadline_seconds, shared_remaining)
                )
                deadline_at = started_at + timedelta(seconds=deadline_seconds)
            elif deadline_seconds is not None:
                deadline_at = started_at + timedelta(seconds=deadline_seconds)
            request_body = json_value(request)
            event = {
                "event": "begin", "trace_id": self.trace_id,
                "operation_id": operation_id, "sequence": sequence, "kind": kind,
                "category": category, "parent_operation_id": parent_operation_id,
                "rpc_method": rpc_method,
                "detached": detached,
                "call_id": call_id, "provider_call_id": provider_call_id,
                "attempt_id": attempt_id, "intent_id": intent_id,
                "thread_id": thread_id, "turn_id": turn_id,
                "started_at": _utc_text(started_at),
                "started_monotonic_ns": started_monotonic_ns,
                "deadline_seconds": deadline_seconds,
                "deadline_at": None if deadline_at is None else _utc_text(deadline_at),
                "request": request_body, "request_digest": sha256_digest(request_body),
                "after_seal": self._reopened,
            }
            self._active[operation_id] = event
            self._append(event)
            return OperationToken(self.trace_id, operation_id, sequence)

    @contextmanager
    def operation_scope(self, token: OperationToken):
        with self._lock:
            begin = self._active.get(token.operation_id)
            if token.trace_id != self.trace_id or begin is None:
                raise ValueError("operation scope token이 trace와 일치하지 않습니다.")
            deadline_monotonic_ns = (
                None
                if begin["deadline_seconds"] is None
                else begin["started_monotonic_ns"]
                + round(float(begin["deadline_seconds"]) * 1_000_000_000)
            )
            scope = OperationTraceScope(
                trace=self, parent=token, deadline_monotonic_ns=deadline_monotonic_ns,
                deadline_at=(
                    None if begin["deadline_at"] is None
                    else datetime.fromisoformat(begin["deadline_at"].replace("Z", "+00:00"))
                ),
                call_id=begin.get("call_id"), attempt_id=begin.get("attempt_id"),
                intent_id=begin.get("intent_id"), thread_id=begin.get("thread_id"),
                turn_id=begin.get("turn_id"),
            )
        with use_operation_trace_scope(scope):
            yield scope

    def finish(
        self,
        token: OperationToken,
        *,
        response: Any = None,
        error: BaseException | Mapping[str, Any] | str | None = None,
        status: str | None = None,
        provider_call_id: str | None = None,
        attempt_id: str | None = None,
        intent_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._sealed:
                raise RuntimeError("sealed operation trace에는 완료를 추가할 수 없습니다.")
            if token.trace_id != self.trace_id or token.operation_id not in self._active:
                raise ValueError("operation token이 이 trace의 미완료 작업과 일치하지 않습니다.")
            begin = self._active[token.operation_id]
            if any(
                event.get("event") == "finish" and event.get("operation_id") == token.operation_id
                for event in self._events
            ):
                raise ValueError("operation은 한 번만 완료할 수 있습니다.")
            elapsed_ms = max(0, round((time.monotonic_ns() - begin["started_monotonic_ns"]) / 1_000_000))
            late = (
                begin["deadline_seconds"] is not None
                and elapsed_ms > round(float(begin["deadline_seconds"]) * 1000)
            )
            error_body = _error_document(error)
            if status is None:
                status = "deadline_exceeded" if late or isinstance(error, TimeoutError) else (
                    "error" if error_body is not None else "ok"
                )
            if status not in TERMINAL_STATUSES:
                raise ValueError(f"완료 status가 유효하지 않습니다: {status}")
            response_body = None if response is None else json_value(response)
            event = {
                "event": "finish", "trace_id": self.trace_id,
                "operation_id": token.operation_id, "sequence": token.sequence,
                "status": status, "finished_at": _utc_text(), "elapsed_ms": elapsed_ms,
                "response": response_body,
                "response_digest": None if response_body is None else sha256_digest(response_body),
                "error": error_body, "late": late,
                "provider_call_id": provider_call_id, "attempt_id": attempt_id,
                "intent_id": intent_id, "thread_id": thread_id, "turn_id": turn_id,
            }
            self._append(event)
            return next(row for row in self.snapshot()["rows"] if row["operation_id"] == token.operation_id)

    def snapshot(self, *, expected_operations: Iterable[str] | None = None) -> dict[str, Any]:
        with self._lock:
            document = _load_document_from_events(
                self._events,
                expected_operations=(
                    self.expected_operations
                    if expected_operations is None else _normalize_expected(expected_operations)
                ),
            )
            return document

    def seal(self, *, expected_operations: Iterable[str] | None = None) -> dict[str, Any]:
        with self._lock:
            if not self._sealed:
                if expected_operations is not None:
                    self.expected_operations = _normalize_expected(expected_operations)
                self._append({
                    "event": "seal", "trace_id": self.trace_id,
                    "expected_operations": list(self.expected_operations),
                    "recorded_at": _utc_text(),
                })
                self._sealed = True
            elif expected_operations is not None and tuple(expected_operations) != self.expected_operations:
                raise ValueError("sealed operation trace의 expected_operations를 바꿀 수 없습니다.")
            return self.snapshot()

    @staticmethod
    def verify(
        document_or_path: Mapping[str, Any] | Path | str,
        *,
        expected_operations: Iterable[str] | None = None,
        expected_call_ids: Iterable[str] | None = None,
    ) -> TraceVerification:
        return verify_operation_trace(
            document_or_path,
            expected_operations=expected_operations,
            expected_call_ids=expected_call_ids,
        )

    @staticmethod
    def recalculate(
        document_or_path: Mapping[str, Any] | Path | str,
        *,
        expected_operations: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        return recalculate_trace_manifest(document_or_path, expected_operations=expected_operations)


def _load_document_from_events(
    events: list[dict[str, Any]], *, expected_operations: Iterable[str],
) -> dict[str, Any]:
    header = events[0]
    begins = {event["operation_id"]: event for event in events if event.get("event") == "begin"}
    finishes = {event["operation_id"]: event for event in events if event.get("event") == "finish"}
    rows: list[dict[str, Any]] = []
    previous_digest: str | None = None
    for begin in sorted(begins.values(), key=lambda item: item["sequence"]):
        row = _project_row(begin, finishes.get(begin["operation_id"]), previous_digest=previous_digest)
        previous_digest = row["row_digest"]
        rows.append(row)
    sealed = False
    for event in events:
        if event.get("event") == "seal":
            sealed = True
        elif event.get("event") == "reopen":
            sealed = False
    expected = tuple(expected_operations)
    document = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_id": header["trace_id"], "context": header["context"],
        "expected_operations": list(expected), "rows": rows,
    }
    document["manifest"] = _manifest_for(
        trace_id=header["trace_id"], context=header["context"], rows=rows,
        expected_operations=expected, sealed=sealed,
    )
    return document
