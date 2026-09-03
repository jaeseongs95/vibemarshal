from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..canonical import canonical_json, sha256_digest
from ..windows_sandbox import capture_sandbox_fingerprint


EXPECTED_SCHEMA_REVISION = 3
EXPECTED_TASK_IDS = tuple(f"FM-0C-{index}" for index in range(1, 9))
EXPECTED_PROFILE_IDS = (
    "flowmarshal_gate0c_planner",
    "flowmarshal_gate0c_runner",
    "flowmarshal_gate0c_validator",
)
APPEND_ONLY_TABLES = (
    "plan_revisions",
    "tasks",
    "task_events",
    "attempts",
    "attempt_events",
    "context_bundles",
    "role_executions",
    "agent_submissions",
    "core_validations",
    "evidence_records",
    "history",
)


@dataclass(frozen=True)
class VerificationCheck:
    check_id: str
    passed: bool
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gate0CReport:
    schema_version: str
    generated_at: str
    gate: str
    overall: str
    schema_revision: int | None
    reason_codes: tuple[str, ...]
    task_states: dict[str, str | None]
    checks: tuple[VerificationCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "gate": self.gate,
            "overall": self.overall,
            "schema_revision": self.schema_revision,
            "reason_codes": list(self.reason_codes),
            "task_states": self.task_states,
            "checks": [asdict(check) for check in self.checks],
        }


def _check(
    check_id: str,
    passed: bool,
    success: str,
    failure: str,
    details: Iterable[str] = (),
) -> VerificationCheck:
    return VerificationCheck(
        check_id=check_id,
        passed=passed,
        summary=success if passed else failure,
        details=tuple(details),
    )


def _json(raw: str) -> Any:
    return json.loads(raw)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_fingerprint(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
        info = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "size": len(data),
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "file_id": [info.st_dev, info.st_ino],
    }


def _latest_task_states(connection: sqlite3.Connection) -> dict[str, str | None]:
    return {
        str(row["task_id"]): row["state"]
        for row in connection.execute(
            """
            SELECT t.task_id,
              (SELECT e.state FROM task_events e
               WHERE e.task_id=t.task_id ORDER BY e.sequence DESC LIMIT 1) AS state
            FROM tasks t ORDER BY t.task_id
            """
        )
    }


class Gate0CVerifier:
    """FlowMarshal Core의 상태 투영·완료 판정을 재사용하지 않는 원시 증거 검사기."""

    def __init__(
        self,
        *,
        project_root: Path,
        database_path: Path | None = None,
        profile_artifact_path: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        artifact_root = self.project_root / "spikes" / "gate0c" / "artifacts"
        self.database_path = database_path or artifact_root / "control" / "gate0c.sqlite3"
        self.profile_artifact_path = (
            profile_artifact_path or artifact_root / "profile-provenance-reuse.json"
        )

    def verify(self) -> Gate0CReport:
        generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not self.database_path.is_file():
            return Gate0CReport(
                schema_version="1.0",
                generated_at=generated_at,
                gate="0C",
                overall="NO-GO",
                schema_revision=None,
                reason_codes=("DATABASE_MISSING",),
                task_states={},
                checks=(
                    VerificationCheck(
                        "database_exists", False, "Gate 0C 권위 원장이 없습니다."
                    ),
                ),
            )

        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            revision = self._schema_revision(connection)
            task_states = _latest_task_states(connection)
            checks = (
                self._sqlite_integrity(connection),
                self._schema_contract(connection, revision),
                self._append_only_guards(connection),
                self._plan_integrity(connection),
                self._row_digests(connection),
                self._evidence_integrity(connection),
                self._history_chain(connection),
                self._prerequisites(),
                self._profile_provenance(connection),
                self._current_host_invariant(),
                self._historical_host_invariant(connection),
                self._task_completion(connection, task_states),
                self._runtime_completion(connection),
            )
            overall = "GO" if all(item.passed for item in checks) else "NO-GO"
            return Gate0CReport(
                schema_version="1.0",
                generated_at=generated_at,
                gate="0C",
                overall=overall,
                schema_revision=revision,
                reason_codes=self._reason_codes(connection, checks),
                task_states=task_states,
                checks=checks,
            )
        except (sqlite3.DatabaseError, ValueError, TypeError, json.JSONDecodeError) as error:
            return Gate0CReport(
                schema_version="1.0",
                generated_at=generated_at,
                gate="0C",
                overall="NO-GO",
                schema_revision=None,
                reason_codes=("LEDGER_UNREADABLE",),
                task_states={},
                checks=(
                    VerificationCheck(
                        "ledger_readable",
                        False,
                        "Gate 0C 원시 원장을 독립 검증할 수 없습니다.",
                        (f"{type(error).__name__}: {error}",),
                    ),
                ),
            )
        finally:
            connection.close()

    @staticmethod
    def _schema_revision(connection: sqlite3.Connection) -> int | None:
        row = connection.execute(
            "SELECT value FROM gate0c_meta WHERE key='schema_revision'"
        ).fetchone()
        try:
            return None if row is None else int(row["value"])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _sqlite_integrity(connection: sqlite3.Connection) -> VerificationCheck:
        details = [
            str(row[0])
            for row in connection.execute("PRAGMA integrity_check")
            if row[0] != "ok"
        ]
        details.extend(
            f"foreign key: table={row[0]}, rowid={row[1]}, parent={row[2]}"
            for row in connection.execute("PRAGMA foreign_key_check")
        )
        return _check(
            "sqlite_integrity",
            not details,
            "SQLite 무결성과 외래 키가 정상입니다.",
            "SQLite 무결성 또는 외래 키 오류가 있습니다.",
            details,
        )

    @staticmethod
    def _schema_contract(
        connection: sqlite3.Connection, revision: int | None
    ) -> VerificationCheck:
        required = {"gate0c_meta", *APPEND_ONLY_TABLES}
        actual = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        details = [f"누락 table: {name}" for name in sorted(required - actual)]
        if revision != EXPECTED_SCHEMA_REVISION:
            details.append(
                f"schema revision={revision!r}, expected={EXPECTED_SCHEMA_REVISION}"
            )
        return _check(
            "schema_contract",
            not details,
            "Gate 0C schema revision 3 계약이 갖춰졌습니다.",
            "Gate 0C schema 계약이 맞지 않습니다.",
            details,
        )

    @staticmethod
    def _append_only_guards(connection: sqlite3.Connection) -> VerificationCheck:
        actual = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        expected = {
            f"{table}_{suffix}"
            for table in APPEND_ONLY_TABLES
            for suffix in ("no_update", "no_delete")
        }
        missing = sorted(expected - actual)
        return _check(
            "append_only_guards",
            not missing,
            "모든 권위·evidence 표에 update/delete 차단 trigger가 있습니다.",
            "append-only trigger가 누락됐습니다.",
            (f"누락 trigger: {name}" for name in missing),
        )

    def _plan_integrity(self, connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        rows = connection.execute("SELECT * FROM plan_revisions").fetchall()
        if len(rows) != 1:
            details.append(f"PlanRevision 개수={len(rows)}, expected=1")
        for row in rows:
            try:
                payload = _json(row["payload_json"])
                document = payload["document"]
                approval = payload["approval_source"]
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                details.append(f"PlanRevision JSON 오류: {error}")
                continue
            if sha256_digest(payload) != row["row_digest"]:
                details.append("PlanRevision row digest 불일치")
            if document.get("sha256") != row["document_digest"]:
                details.append("PlanRevision document digest binding 불일치")
            if sha256_digest(approval) != row["approval_digest"]:
                details.append("PlanRevision approval digest 불일치")
            document_path = self.project_root / str(document.get("path", ""))
            if not document_path.is_file():
                details.append(f"승인 문서 누락: {document_path}")
            elif _sha256_file(document_path) != document.get("sha256"):
                details.append("승인 뒤 계획 문서 bytes가 변경됐습니다.")

        tasks = connection.execute(
            "SELECT task_id,definition_json,definition_digest FROM tasks ORDER BY task_id"
        ).fetchall()
        actual_ids = tuple(str(row["task_id"]) for row in tasks)
        if actual_ids != EXPECTED_TASK_IDS:
            details.append(f"task ID 집합 불일치: {actual_ids}")
        for row in tasks:
            try:
                definition = _json(row["definition_json"])
            except json.JSONDecodeError as error:
                details.append(f"task JSON 오류: {row['task_id']} ({error})")
                continue
            if sha256_digest(definition) != row["definition_digest"]:
                details.append(f"task definition digest 불일치: {row['task_id']}")
            if definition.get("task_id") != row["task_id"]:
                details.append(f"task ID binding 불일치: {row['task_id']}")
        return _check(
            "approved_plan_integrity",
            not details,
            "승인 PlanRevision·task 정의와 원문 digest가 일치합니다.",
            "승인 PlanRevision 또는 task 정의가 변조·누락됐습니다.",
            details,
        )

    @staticmethod
    def _row_digests(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        for row in connection.execute("SELECT * FROM task_events"):
            payload = {
                "task_id": row["task_id"],
                "state": row["state"],
                "reason_code": row["reason_code"],
                "detail": _json(row["detail_json"]),
                "occurred_at": row["occurred_at"],
            }
            if sha256_digest(payload) != row["row_digest"]:
                details.append(f"task event digest 불일치: sequence={row['sequence']}")
        for row in connection.execute("SELECT * FROM attempts"):
            payload = {
                "attempt_id": row["attempt_id"],
                "task_id": row["task_id"],
                "role": row["role"],
                "model": row["model"],
                "effort": row["effort"],
                "profile_digest": row["profile_digest"],
                "created_at": row["created_at"],
            }
            if sha256_digest(payload) != row["definition_digest"]:
                details.append(f"Attempt digest 불일치: {row['attempt_id']}")
        for row in connection.execute("SELECT * FROM attempt_events"):
            payload = {
                "attempt_id": row["attempt_id"],
                "stage": row["stage"],
                "reason_code": row["reason_code"],
                "detail": _json(row["detail_json"]),
                "occurred_at": row["occurred_at"],
            }
            if sha256_digest(payload) != row["row_digest"]:
                details.append(f"Attempt event digest 불일치: sequence={row['sequence']}")
        for row in connection.execute("SELECT * FROM role_executions"):
            payload = {
                "execution_id": row["execution_id"],
                "attempt_id": row["attempt_id"],
                "role": row["role"],
                "action_kind": row["action_kind"],
                "stage": row["stage"],
                "request_digest": row["request_digest"],
                "profile_digest": row["profile_digest"],
                "thread_id": row["thread_id"],
                "turn_id": row["turn_id"],
                "receipt_digest": row["receipt_digest"],
                "detail": _json(row["detail_json"]),
                "occurred_at": row["occurred_at"],
            }
            if sha256_digest(payload) != row["row_digest"]:
                details.append(f"role execution digest 불일치: sequence={row['sequence']}")
        for row in connection.execute("SELECT * FROM core_validations"):
            payload = {
                "validation_id": row["validation_id"],
                "runner_attempt_id": row["runner_attempt_id"],
                "validator_attempt_id": row["validator_attempt_id"],
                "named_checks_digest": row["named_checks_digest"],
                "artifact_digest": row["artifact_digest"],
                "validator_submission_digest": row["validator_submission_digest"],
                "passed": bool(row["passed"]),
                "detail": _json(row["detail_json"]),
                "created_at": row["created_at"],
            }
            if sha256_digest(payload) != row["row_digest"]:
                details.append(f"Core validation digest 불일치: {row['validation_id']}")
        return _check(
            "authority_row_digests",
            not details,
            "권위 상태 행의 canonical digest가 모두 일치합니다.",
            "권위 상태 행 digest 불일치가 있습니다.",
            details,
        )

    @staticmethod
    def _evidence_integrity(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        for row in connection.execute("SELECT * FROM evidence_records"):
            try:
                payload = _json(row["payload_json"])
            except json.JSONDecodeError as error:
                details.append(f"evidence JSON 오류: {row['evidence_id']} ({error})")
                continue
            if sha256_digest(payload) != row["payload_digest"]:
                details.append(f"evidence payload digest 불일치: {row['evidence_id']}")
            row_material = {
                "evidence_id": row["evidence_id"],
                "kind": row["kind"],
                "payload_digest": row["payload_digest"],
                "created_at": row["created_at"],
            }
            if sha256_digest(row_material) != row["row_digest"]:
                details.append(f"evidence row digest 불일치: {row['evidence_id']}")
        if connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0] == 0:
            details.append("evidence record가 없습니다.")
        return _check(
            "evidence_integrity",
            not details,
            "content-addressed evidence가 원장과 일치합니다.",
            "evidence가 누락되거나 digest가 일치하지 않습니다.",
            details,
        )

    @staticmethod
    def _history_chain(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        previous: str | None = None
        expected_sequence = 1
        for row in connection.execute("SELECT * FROM history ORDER BY sequence"):
            if int(row["sequence"]) != expected_sequence:
                details.append(
                    f"history sequence gap: actual={row['sequence']} expected={expected_sequence}"
                )
            try:
                payload = _json(row["payload_json"])
            except json.JSONDecodeError as error:
                details.append(f"history JSON 오류: sequence={row['sequence']} ({error})")
                break
            material = {
                "sequence": int(row["sequence"]),
                "event_type": row["event_type"],
                "entity_id": row["entity_id"],
                "payload": payload,
                "previous_hash": previous,
                "occurred_at": row["occurred_at"],
            }
            expected_hash = sha256_digest(material)
            if row["previous_hash"] != previous:
                details.append(f"history previous hash 불일치: sequence={row['sequence']}")
            if row["entry_hash"] != expected_hash:
                details.append(f"history entry hash 불일치: sequence={row['sequence']}")
            previous = str(row["entry_hash"])
            expected_sequence += 1
        if expected_sequence == 1:
            details.append("history chain이 비어 있습니다.")
        return _check(
            "history_chain",
            not details,
            "append-only history hash chain이 연속적입니다.",
            "history hash chain이 누락되거나 변조됐습니다.",
            details,
        )

    def _prerequisites(self) -> VerificationCheck:
        details: list[str] = []
        paths = {
            "Gate 0A": self.project_root / "spikes" / "gate0a" / "artifacts" / "gate0a-results.json",
            "Gate 0B": self.project_root / "spikes" / "gate0b" / "artifacts" / "gate0b-results.json",
        }
        for label, path in paths.items():
            if not path.is_file():
                details.append(f"{label} 결과 누락")
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                details.append(f"{label} 결과 읽기 실패: {error}")
                continue
            status = (
                document.get("gate_decision", {}).get("status")
                if label == "Gate 0A"
                else document.get("overall")
            )
            if status != "GO":
                details.append(f"{label} status={status!r}")
        return _check(
            "prerequisite_gates",
            not details,
            "Gate 0A와 Gate 0B 권위 결과가 계속 GO입니다.",
            "선행 Gate 결과가 GO가 아니거나 누락됐습니다.",
            details,
        )

    def _read_profile_artifact(self) -> dict[str, Any]:
        return json.loads(self.profile_artifact_path.read_text(encoding="utf-8"))

    def _profile_provenance(self, connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        if not self.profile_artifact_path.is_file():
            details.append(f"profile artifact 누락: {self.profile_artifact_path}")
            return _check(
                "profile_provenance",
                False,
                "세 역할의 profile provenance가 확인됐습니다.",
                "세 역할의 profile provenance를 확인할 수 없습니다.",
                details,
            )
        artifact = self._read_profile_artifact()
        profile_set = artifact.get("profile_set")
        if not isinstance(profile_set, dict):
            details.append("profile_set이 없습니다.")
        else:
            if sha256_digest(profile_set) != artifact.get("profile_set_digest"):
                details.append("profile_set digest 불일치")
            ids = tuple(
                item.get("profile_id")
                for item in profile_set.get("definitions", [])
                if isinstance(item, dict)
            )
            if ids != EXPECTED_PROFILE_IDS:
                details.append(f"profile ID 불일치: {ids}")
        if artifact.get("status") != "GO":
            error = artifact.get("error", {})
            details.append(
                "profile probe status="
                f"{artifact.get('status')!r}, reason={error.get('reason_code')!r}"
            )
        evidence = connection.execute(
            "SELECT payload_json,payload_digest FROM evidence_records "
            "WHERE kind='profile_provenance_failure'"
        ).fetchall()
        if not evidence:
            details.append("profile provenance evidence가 원장에 없습니다.")
        elif not any(
            _json(row["payload_json"]) == artifact
            and row["payload_digest"] == sha256_digest(artifact)
            for row in evidence
        ):
            details.append("profile artifact와 원장 evidence binding이 다릅니다.")
        return _check(
            "profile_provenance",
            not details,
            "세 역할의 requested·allowed·active profile provenance가 확인됐습니다.",
            "필수 profile provenance가 실패·누락됐습니다.",
            details,
        )

    def _current_host_invariant(self) -> VerificationCheck:
        details: list[str] = []
        try:
            artifact = self._read_profile_artifact()
            host = artifact["host_invariants"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            details.append(f"host invariant artifact 오류: {error}")
        else:
            if host.get("config_before") != host.get("config_after"):
                details.append("최종 probe 전후 config fingerprint 불일치")
            if host.get("sandbox_before_digest") != host.get("sandbox_after_digest"):
                details.append("최종 probe 전후 sandbox fingerprint 불일치")
            if host.get("unchanged") is not True:
                details.append("최종 probe host unchanged attestation이 true가 아님")
            definitions = artifact.get("profile_set", {}).get("definitions", [])
            instruction_paths = {
                str(rule.get("selector"))
                for definition in definitions
                if isinstance(definition, dict)
                for rule in definition.get("filesystem_rules", [])
                if isinstance(rule, dict)
                and rule.get("access") == "read"
                and str(rule.get("selector", "")).lower().endswith(
                    ("\\agents.md", "\\agents.override.md")
                )
            }
            if len(instruction_paths) != 1:
                details.append("authoritative CODEX_HOME instruction 경로를 단일 확정할 수 없음")
            else:
                codex_home = Path(next(iter(instruction_paths))).parent
                current_config = _file_fingerprint(codex_home / "config.toml")
                if current_config != host.get("config_after"):
                    details.append("기록 뒤 현재 config fingerprint가 달라졌습니다.")
                current_sandbox = sha256_digest(
                    capture_sandbox_fingerprint(codex_home).as_dict()
                )
                if current_sandbox != host.get("sandbox_after_digest"):
                    details.append("기록 뒤 현재 sandbox fingerprint가 달라졌습니다.")
        return _check(
            "current_host_invariant",
            not details,
            "최종 엄격 probe는 사용자 config와 sandbox 상태를 변경하지 않았습니다.",
            "최종 엄격 probe 중 host 상태가 바뀌었거나 증거가 없습니다.",
            details,
        )

    @staticmethod
    def _historical_host_invariant(connection: sqlite3.Connection) -> VerificationCheck:
        incidents = connection.execute(
            "SELECT evidence_id,payload_json FROM evidence_records WHERE kind='host_state_incident'"
        ).fetchall()
        details: list[str] = []
        for row in incidents:
            payload = _json(row["payload_json"])
            entries = payload.get("known_synthetic_trust_entries", [])
            details.append(
                f"{row['evidence_id']}: 합성 trust entry {len(entries)}개가 사용자 config에 남아 있음"
            )
        return _check(
            "historical_host_invariant",
            not details,
            "검증 전체 과정에서 사용자 config·sandbox 상태가 불변입니다.",
            "이전 진단 실행에서 사용자 config 변경이 발생했습니다.",
            details,
        )

    @staticmethod
    def _task_completion(
        connection: sqlite3.Connection, states: dict[str, str | None]
    ) -> VerificationCheck:
        del connection
        details = [
            f"{task_id}: {states.get(task_id)!r}"
            for task_id in EXPECTED_TASK_IDS
            if states.get(task_id) != "completed"
        ]
        return _check(
            "task_completion",
            not details,
            "승인된 Gate 0C task가 모두 검증 완료됐습니다.",
            "승인된 Gate 0C task가 모두 완료되지 않았습니다.",
            details,
        )

    @staticmethod
    def _runtime_completion(connection: sqlite3.Connection) -> VerificationCheck:
        details: list[str] = []
        successful_roles = {
            str(row["role"])
            for row in connection.execute(
                "SELECT DISTINCT role FROM role_executions WHERE stage='succeeded'"
            )
        }
        missing = sorted({"planner", "runner", "validator"} - successful_roles)
        details.extend(f"성공 runtime receipt 누락: {role}" for role in missing)
        validation_count = connection.execute(
            "SELECT COUNT(*) FROM core_validations WHERE passed=1"
        ).fetchone()[0]
        if validation_count < 1:
            details.append("성공 Core validation receipt가 없습니다.")
        return _check(
            "actual_runtime_completion",
            not details,
            "세 역할 분리 실행과 Core validation 증거가 있습니다.",
            "실제 세 역할 E2E·Core validation 증거가 없습니다.",
            details,
        )

    @staticmethod
    def _reason_codes(
        connection: sqlite3.Connection, checks: tuple[VerificationCheck, ...]
    ) -> tuple[str, ...]:
        values = {
            str(row["reason_code"])
            for row in connection.execute(
                "SELECT reason_code FROM task_events WHERE reason_code IS NOT NULL "
                "UNION SELECT reason_code FROM attempt_events WHERE reason_code IS NOT NULL"
            )
        }
        if any(not item.passed for item in checks):
            values.update(
                item.check_id.upper()
                for item in checks
                if not item.passed
            )
        return tuple(sorted(values))


def write_report(
    report: Gate0CReport,
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# FlowMarshal Gate 0C 검증 보고서",
        "",
        f"- 최종 판정: **{report.overall}**",
        f"- 생성 시각(UTC): `{report.generated_at}`",
        f"- 원장 schema revision: `{report.schema_revision}`",
        "- 판정 원칙: 하나라도 누락·실패·변조·출처 불일치이면 통과로 추정하지 않음",
        "",
        "## 핵심 상황",
        "",
    ]
    if report.overall == "GO":
        lines.append("승인된 Gate 0C 조건과 실제 세 역할 검증이 모두 통과했습니다.")
    else:
        lines.extend(
            (
                "`FM-0C-1`의 엄격 Runner permission profile이 전역 `AGENTS.md`를 읽지 못해 "
                "thread 초기화에 실패했습니다. 보호 범위를 완화하거나 full access로 대체하지 않고 "
                "후속 task를 중단했으므로 현재 Gate는 `NO-GO`입니다.",
            )
        )
    lines.extend(("", "## 독립 검증 결과", "", "| 검사 | 결과 | 설명 |", "|---|---:|---|"))
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"| `{check.check_id}` | {mark} | {check.summary} |")
    failed = [check for check in report.checks if not check.passed and check.details]
    if failed:
        lines.extend(("", "## 실패 근거", ""))
        for check in failed:
            lines.append(f"### `{check.check_id}`")
            lines.append("")
            lines.extend(f"- {detail}" for detail in check.details)
            lines.append("")
    lines.extend(("## Task 상태", "", "| Task | 상태 |", "|---|---|"))
    for task_id in EXPECTED_TASK_IDS:
        lines.append(f"| `{task_id}` | `{report.task_states.get(task_id)}` |")
    lines.extend(
        (
            "",
            "## 권장 후속 조치",
            "",
            "새 PlanRevision에서 native Windows 직접 실행 대신 disposable VM, WSL 또는 brokered filesystem 중 하나를 선택해 재검증합니다. 사용자 config의 합성 trust entry 정리는 별도 명시적 승인 뒤 정확한 경로만 대상으로 수행해야 합니다.",
            "",
        )
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
