from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class VerificationCheck:
    check_id: str
    passed: bool
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gate0BReport:
    schema_revision: int | None
    overall: str
    checks: tuple[VerificationCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_revision": self.schema_revision,
            "overall": self.overall,
            "checks": [asdict(check) for check in self.checks],
        }


class Gate0BVerifier:
    """Core의 상태 투영 코드를 사용하지 않는 원시 SQLite 독립 검사기."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def verify(self) -> Gate0BReport:
        if not self.database_path.is_file():
            return Gate0BReport(
                schema_revision=None,
                overall="NO_GO",
                checks=(
                    VerificationCheck(
                        "database_exists", False, "원장 데이터베이스가 없습니다."
                    ),
                ),
            )
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            revision = self._schema_revision(connection)
            checks = (
                self._integrity(connection),
                self._schema(connection, revision),
                self._plan_invariants(connection),
                self._execution_invariants(connection),
                self._recovery_invariants(connection),
                self._access_invariants(connection),
                self._evidence_invariants(connection),
                self._history_chain(connection),
                self._state_attestations(connection),
                self._immutability_guards(connection),
            )
            overall = "GO" if all(check.passed for check in checks) else "NO_GO"
            return Gate0BReport(revision, overall, checks)
        except sqlite3.DatabaseError as error:
            return Gate0BReport(
                schema_revision=None,
                overall="NO_GO",
                checks=(
                    VerificationCheck(
                        "database_readable",
                        False,
                        "원장을 읽을 수 없습니다.",
                        (f"{type(error).__name__}: {error}",),
                    ),
                ),
            )
        finally:
            connection.close()

    @staticmethod
    def _schema_revision(connection: sqlite3.Connection) -> int | None:
        try:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_revision'"
            ).fetchone()
            return None if row is None else int(row["value"])
        except (sqlite3.DatabaseError, ValueError):
            return None

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if [row[0] for row in integrity] != ["ok"]:
            details.extend(str(row[0]) for row in integrity)
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        details.extend(
            f"foreign key: table={row[0]}, rowid={row[1]}, parent={row[2]}"
            for row in foreign_keys
        )
        return _check(
            "sqlite_integrity",
            not details,
            "SQLite 무결성과 외래 키가 정상입니다.",
            "SQLite 무결성 또는 외래 키 오류가 있습니다.",
            details,
        )

    @staticmethod
    def _schema(
        connection: sqlite3.Connection, revision: int | None
    ) -> VerificationCheck:
        required = {
            "projects",
            "resources",
            "plan_revisions",
            "work_items",
            "revision_work_items",
            "attempts",
            "runtime_action_intents",
            "runtime_bindings",
            "resource_leases",
            "access_requests",
            "access_grants",
            "evidence_records",
            "attempt_evidence",
            "history_events",
            "state_attestations",
        }
        actual = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(required - actual)
        details = [f"누락 table: {name}" for name in missing]
        if revision != 1:
            details.append(f"schema revision: {revision!r} (expected 1)")
        return _check(
            "schema_contract",
            not details,
            "Gate 0B 내부 schema revision 1 계약이 갖춰졌습니다.",
            "Gate 0B schema 계약이 맞지 않습니다.",
            details,
        )

    @staticmethod
    def _plan_invariants(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        duplicates = connection.execute(
            "SELECT project_id, COUNT(*) AS count FROM plan_revisions "
            "WHERE status = 'active' GROUP BY project_id HAVING COUNT(*) > 1"
        ).fetchall()
        details.extend(
            f"active revision 중복: {row['project_id']} ({row['count']})"
            for row in duplicates
        )
        pointers = connection.execute(
            "SELECT p.id, p.active_revision_id, r.status, r.project_id AS revision_project "
            "FROM projects p LEFT JOIN plan_revisions r ON r.id = p.active_revision_id"
        ).fetchall()
        for row in pointers:
            if row["active_revision_id"] is None:
                continue
            if row["status"] != "active" or row["revision_project"] != row["id"]:
                details.append(f"잘못된 active revision pointer: {row['id']}")
        revisions = connection.execute(
            "SELECT id, content_digest, draft_json FROM plan_revisions"
        ).fetchall()
        for row in revisions:
            try:
                draft = json.loads(row["draft_json"])
                digest = _digest(draft)
            except (ValueError, TypeError) as error:
                details.append(f"plan JSON 오류: {row['id']} ({error})")
                continue
            if digest != row["content_digest"]:
                details.append(f"plan content digest 불일치: {row['id']}")
        cycles = _dependency_cycles(connection)
        details.extend(f"dependency 순환: {item}" for item in cycles)
        return _check(
            "plan_invariants",
            not details,
            "PlanRevision 단일 활성화·digest·dependency 불변식이 정상입니다.",
            "PlanRevision 불변식 위반이 있습니다.",
            details,
        )

    @staticmethod
    def _execution_invariants(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        queries = (
            (
                "active Attempt 중복",
                "SELECT revision_id || ':' || work_item_id AS key, COUNT(*) AS count "
                "FROM attempts WHERE status IN "
                "('reserved','running','awaiting_validation','awaiting_human_review') "
                "GROUP BY revision_id, work_item_id HAVING COUNT(*) > 1",
            ),
            (
                "active intent 중복",
                "SELECT attempt_id AS key, COUNT(*) AS count FROM runtime_action_intents "
                "WHERE status IN ('reserved','executing') GROUP BY attempt_id "
                "HAVING COUNT(*) > 1",
            ),
            (
                "write lease 중복",
                "SELECT resource_id AS key, COUNT(*) AS count FROM resource_leases "
                "WHERE released_at IS NULL GROUP BY resource_id HAVING COUNT(*) > 1",
            ),
        )
        for label, sql in queries:
            for row in connection.execute(sql):
                details.append(f"{label}: {row['key']} ({row['count']})")
        active = connection.execute(
            "SELECT a.id, a.status, "
            " (SELECT COUNT(*) FROM project_execution_slots s WHERE s.attempt_id = a.id) AS slots, "
            " (SELECT COUNT(*) FROM resource_leases l WHERE l.attempt_id = a.id "
            "   AND l.released_at IS NULL) AS leases "
            "FROM attempts a WHERE a.status IN "
            "('reserved','running','awaiting_validation','awaiting_human_review')"
        ).fetchall()
        for row in active:
            if row["slots"] != 1 or row["leases"] != 1:
                details.append(
                    f"active Attempt slot/lease 불일치: {row['id']} "
                    f"slots={row['slots']} leases={row['leases']}"
                )
        terminal = connection.execute(
            "SELECT a.id, a.status, "
            " (SELECT COUNT(*) FROM project_execution_slots s WHERE s.attempt_id = a.id) AS slots, "
            " (SELECT COUNT(*) FROM resource_leases l WHERE l.attempt_id = a.id "
            "   AND l.released_at IS NULL) AS leases "
            "FROM attempts a WHERE a.status IN "
            "('completed','blocked','failed','cancelled')"
        ).fetchall()
        for row in terminal:
            if row["slots"] or row["leases"]:
                details.append(f"terminal Attempt가 리소스를 보유함: {row['id']}")
        bindings = connection.execute(
            "SELECT b.id, i.status FROM runtime_bindings b "
            "JOIN runtime_action_intents i ON i.id = b.source_intent_id"
        ).fetchall()
        for row in bindings:
            if row["status"] not in {"succeeded", "reconciled"}:
                details.append(f"확정되지 않은 intent의 binding: {row['id']}")
        return _check(
            "execution_invariants",
            not details,
            "Attempt·intent·slot·write lease 중복 방지 불변식이 정상입니다.",
            "실행 중복 방지 불변식 위반이 있습니다.",
            details,
        )

    @staticmethod
    def _recovery_invariants(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        unknowns = connection.execute(
            "SELECT i.id, i.project_id, i.attempt_id, i.ordinal, p.state, "
            " rwi.projection_state FROM runtime_action_intents i "
            "JOIN projects p ON p.id = i.project_id "
            "JOIN attempts a ON a.id = i.attempt_id "
            "JOIN revision_work_items rwi ON rwi.revision_id = a.revision_id "
            " AND rwi.work_item_id = a.work_item_id "
            "WHERE i.status = 'unknown'"
        ).fetchall()
        for row in unknowns:
            if row["state"] != "quarantined":
                details.append(f"unknown인데 프로젝트가 격리되지 않음: {row['id']}")
            if row["projection_state"] != "recovery_required":
                details.append(f"unknown인데 WorkItem이 recovery_required가 아님: {row['id']}")
            later_normal = connection.execute(
                "SELECT id FROM runtime_action_intents WHERE attempt_id = ? "
                "AND ordinal > ? AND recovery_of_intent_id IS NULL LIMIT 1",
                (row["attempt_id"], row["ordinal"]),
            ).fetchone()
            if later_normal is not None:
                details.append(
                    f"미해결 unknown 뒤 정상 intent가 생성됨: {row['id']} -> {later_normal['id']}"
                )
        recoveries = connection.execute(
            "SELECT child.id, child.ordinal, parent.id AS parent_id, parent.ordinal AS parent_ordinal, "
            "parent.status AS parent_status FROM runtime_action_intents child "
            "LEFT JOIN runtime_action_intents parent ON parent.id = child.recovery_of_intent_id "
            "WHERE child.recovery_of_intent_id IS NOT NULL"
        ).fetchall()
        for row in recoveries:
            if row["parent_id"] is None or row["parent_ordinal"] >= row["ordinal"]:
                details.append(f"복구 lineage 오류: {row['id']}")
            if row["parent_status"] not in {"unknown", "reconciled"}:
                details.append(f"복구 parent 상태 오류: {row['id']}")
        return _check(
            "recovery_invariants",
            not details,
            "unknown 효과가 격리되고 복구 lineage가 보존됩니다.",
            "unknown 효과 격리 또는 복구 lineage 오류가 있습니다.",
            details,
        )

    @staticmethod
    def _access_invariants(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        invalid = connection.execute(
            "SELECT id FROM access_grants WHERE access_mode <> 'read'"
        ).fetchall()
        details.extend(f"쓰기 AccessGrant: {row['id']}" for row in invalid)
        resource_mismatch = connection.execute(
            "SELECT g.id FROM access_grants g JOIN resources r ON r.id = g.resource_id "
            "WHERE r.max_access <> 'read' OR r.kind NOT IN ('reference','runtime_dependency') "
            "OR r.canonical_path <> g.exact_path OR r.file_identity <> g.file_identity "
            "OR r.manifest_digest <> g.manifest_digest"
        ).fetchall()
        details.extend(f"grant/resource 불일치: {row['id']}" for row in resource_mismatch)
        unresolved_ready = connection.execute(
            "SELECT DISTINCT rwi.work_item_id FROM revision_work_items rwi "
            "JOIN access_grants g ON g.work_item_id = rwi.work_item_id "
            "JOIN access_grant_invalidations i ON i.grant_id = g.id "
            "WHERE rwi.projection_state IN ('ready','active','completed')"
        ).fetchall()
        details.extend(
            f"무효 grant를 사용하는 WorkItem: {row['work_item_id']}"
            for row in unresolved_ready
        )
        replay = connection.execute(
            "SELECT nonce, COUNT(*) AS count FROM authority_uses "
            "GROUP BY nonce HAVING COUNT(*) > 1"
        ).fetchall()
        details.extend(f"승인 nonce 재사용: {row['nonce']}" for row in replay)
        return _check(
            "access_invariants",
            not details,
            "추가 입력은 읽기 전용이며 승인 nonce와 입력 drift가 통제됩니다.",
            "접근 승인 불변식 위반이 있습니다.",
            details,
        )

    @staticmethod
    def _evidence_invariants(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        records = connection.execute("SELECT * FROM evidence_records").fetchall()
        for row in records:
            path = Path(row["storage_path"])
            try:
                payload = path.read_bytes()
            except OSError as error:
                details.append(f"evidence 없음: {row['id']} ({error})")
                continue
            if len(payload) != row["size"] or _digest_bytes(payload) != row["digest"]:
                details.append(f"evidence digest/size 불일치: {row['id']}")
        completed = connection.execute(
            "SELECT id, revision_id, work_item_id FROM attempts WHERE status = 'completed'"
        ).fetchall()
        for attempt in completed:
            rows = connection.execute(
                "SELECT c.criterion_id, e.storage_path FROM completion_criteria c "
                "LEFT JOIN attempt_evidence ae ON ae.attempt_id = ? "
                " AND ae.criterion_id = c.criterion_id "
                "LEFT JOIN evidence_records e ON e.id = ae.evidence_id "
                "WHERE c.revision_id = ? AND c.work_item_id = ?",
                (attempt["id"], attempt["revision_id"], attempt["work_item_id"]),
            ).fetchall()
            if not rows:
                details.append(f"완료 Attempt에 completion criterion이 없음: {attempt['id']}")
                continue
            for row in rows:
                if row["storage_path"] is None:
                    details.append(
                        f"완료 evidence 누락: {attempt['id']}:{row['criterion_id']}"
                    )
                    continue
                try:
                    result = json.loads(Path(row["storage_path"]).read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    details.append(
                        f"완료 evidence 해석 실패: {attempt['id']}:{row['criterion_id']} ({error})"
                    )
                    continue
                if result.get("passed") is not True:
                    details.append(
                        f"실패 evidence로 완료됨: {attempt['id']}:{row['criterion_id']}"
                    )
        completed_projection = connection.execute(
            "SELECT rwi.revision_id, rwi.work_item_id FROM revision_work_items rwi "
            "WHERE rwi.projection_state = 'completed' AND NOT EXISTS ("
            " SELECT 1 FROM attempts a WHERE a.revision_id = rwi.revision_id "
            " AND a.work_item_id = rwi.work_item_id AND a.status = 'completed')"
        ).fetchall()
        details.extend(
            f"완료 projection에 완료 Attempt 없음: {row['revision_id']}:{row['work_item_id']}"
            for row in completed_projection
        )
        return _check(
            "evidence_invariants",
            not details,
            "완료 판정에 필요한 content-addressed evidence가 모두 유효합니다.",
            "완료 evidence가 누락되거나 변조됐습니다.",
            details,
        )

    @staticmethod
    def _history_chain(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        projects = connection.execute("SELECT id FROM projects ORDER BY id").fetchall()
        for project in projects:
            previous_hash: str | None = None
            expected_sequence = 1
            events = connection.execute(
                "SELECT * FROM history_events WHERE project_id = ? ORDER BY sequence",
                (project["id"],),
            ).fetchall()
            if not events:
                details.append(f"history가 없는 프로젝트: {project['id']}")
                continue
            for event in events:
                try:
                    payload = json.loads(event["payload_json"])
                except ValueError:
                    details.append(f"history payload JSON 오류: {event['id']}")
                    payload = None
                body = {
                    "id": event["id"],
                    "project_id": event["project_id"],
                    "sequence": event["sequence"],
                    "event_type": event["event_type"],
                    "entity_type": event["entity_type"],
                    "entity_id": event["entity_id"],
                    "payload": payload,
                    "previous_hash": event["previous_hash"],
                    "created_at": event["created_at"],
                }
                if event["sequence"] != expected_sequence:
                    details.append(f"history sequence 간격: {event['id']}")
                if event["previous_hash"] != previous_hash:
                    details.append(f"history 이전 hash 불일치: {event['id']}")
                if _digest(body) != event["event_hash"]:
                    details.append(f"history event hash 불일치: {event['id']}")
                previous_hash = event["event_hash"]
                expected_sequence += 1
        return _check(
            "history_chain",
            not details,
            "append-only history hash chain이 정상입니다.",
            "history hash chain이 끊겼거나 변조됐습니다.",
            details,
        )

    @staticmethod
    def _state_attestations(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        table_specs: dict[str, tuple[str, str]] = {
            "project": ("projects", "id"),
            "resource": ("resources", "id"),
            "plan_revision": ("plan_revisions", "id"),
            "work_item": ("work_items", "id"),
            "attempt": ("attempts", "id"),
            "intent": ("runtime_action_intents", "id"),
            "lease": ("resource_leases", "id"),
            "access_request": ("access_requests", "id"),
        }
        latest_rows = connection.execute(
            "SELECT entity_type, entity_id, state_digest FROM ("
            " SELECT sa.entity_type, sa.entity_id, sa.state_digest, "
            " ROW_NUMBER() OVER (PARTITION BY sa.entity_type, sa.entity_id "
            " ORDER BY he.sequence DESC, sa.id DESC) AS rank "
            " FROM state_attestations sa "
            " JOIN history_events he ON he.id = sa.history_event_id"
            ") WHERE rank = 1"
        ).fetchall()
        attestations = {
            (row["entity_type"], row["entity_id"]): row["state_digest"]
            for row in latest_rows
        }
        expected: set[tuple[str, str]] = set()
        for entity_type, (table, key) in table_specs.items():
            for row in connection.execute(f"SELECT * FROM {table}"):
                entity_id = row[key]
                expected.add((entity_type, entity_id))
                attested = attestations.get((entity_type, entity_id))
                if attested is None:
                    details.append(f"state attestation 누락: {entity_type}:{entity_id}")
                elif _digest(dict(row)) != attested:
                    details.append(f"state attestation 불일치: {entity_type}:{entity_id}")
        for row in connection.execute("SELECT * FROM revision_work_items"):
            entity_id = f"{row['revision_id']}:{row['work_item_id']}"
            expected.add(("revision_work_item", entity_id))
            attested = attestations.get(("revision_work_item", entity_id))
            if attested is None:
                details.append(f"state attestation 누락: revision_work_item:{entity_id}")
            elif _digest(dict(row)) != attested:
                details.append(f"state attestation 불일치: revision_work_item:{entity_id}")
        extras = set(attestations) - expected
        details.extend(
            f"대상이 사라진 state attestation: {entity_type}:{entity_id}"
            for entity_type, entity_id in sorted(extras)
        )
        return _check(
            "state_attestations",
            not details,
            "권위 상태의 최신 attestation이 원시 행과 일치합니다.",
            "원시 권위 상태가 attestation과 다릅니다.",
            details,
        )

    @staticmethod
    def _immutability_guards(connection: sqlite3.Connection) -> VerificationCheck:
        required = {
            "tr_plan_content_immutable",
            "tr_plan_status_transition",
            "tr_attempt_status_transition",
            "tr_intent_status_transition",
            "tr_terminal_attempt_immutable",
            "tr_terminal_intent_immutable",
            "tr_intent_request_immutable",
            "tr_attempt_identity_immutable",
            "tr_project_definition_immutable",
            "tr_access_request_content_immutable",
            "tr_work_item_projection_transition",
            "tr_unknown_blocks_normal_intent",
            "tr_recovery_requires_unknown",
            "tr_no_update_access_grants",
            "tr_no_delete_access_grants",
            "tr_no_update_history_events",
            "tr_no_delete_history_events",
            "tr_no_update_state_attestations",
            "tr_no_delete_state_attestations",
        }
        actual = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        missing = sorted(required - actual)
        return _check(
            "database_guards",
            not missing,
            "상태 전이·append-only·unknown 차단 DB guard가 설치됐습니다.",
            "필수 DB guard가 누락됐습니다.",
            [f"누락 trigger: {name}" for name in missing],
        )


def write_report(
    report: Gate0BReport,
    *,
    json_path: Path | str,
    markdown_path: Path | str,
) -> None:
    json_target = Path(json_path)
    markdown_target = Path(markdown_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# FlowMarshal Gate 0B 검증 보고서",
        "",
        f"- 판정: **{report.overall}**",
        f"- 내부 schema revision: `{report.schema_revision}`",
        "",
        "## 검사 결과",
        "",
    ]
    for check in report.checks:
        mark = "통과" if check.passed else "실패"
        lines.extend([f"### {check.check_id}: {mark}", "", check.summary, ""])
        if check.details:
            lines.extend(f"- {detail}" for detail in check.details)
            lines.append("")
    markdown_target.write_text("\n".join(lines), encoding="utf-8")


def _check(
    check_id: str,
    passed: bool,
    success: str,
    failure: str,
    details: Iterable[str],
) -> VerificationCheck:
    return VerificationCheck(
        check_id=check_id,
        passed=passed,
        summary=success if passed else failure,
        details=tuple(details),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _dependency_cycles(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT revision_id, work_item_id, depends_on_work_item_id "
        "FROM work_item_dependencies"
    ).fetchall()
    graphs: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        graph = graphs.setdefault(row["revision_id"], {})
        graph.setdefault(row["work_item_id"], set()).add(row["depends_on_work_item_id"])
        graph.setdefault(row["depends_on_work_item_id"], set())
    cycles: list[str] = []
    for revision_id, graph in graphs.items():
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(dependency) for dependency in graph[node]):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        if any(visit(node) for node in graph if node not in visited):
            cycles.append(revision_id)
    return tuple(cycles)
