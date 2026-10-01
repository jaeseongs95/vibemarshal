from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine.ledger import EngineLedgerError, SQLiteEngineLedger


class EngineLedgerSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = SQLiteEngineLedger(self.root / "engine.sqlite3")
        self.ledger.initialize()
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO projects(id,name,root,artifact_root,run_state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("project", "before", str(self.root / "workspace"),
                 str(self.root / "artifacts"), "idle", tx.now, tx.now),
            )
            tx.history("project", "project.created", "project", "project", {})

    def _commit_writer(self) -> None:
        # 별도 writer 연결에서 상태와 이력을 같은 commit에 묶는다.
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE projects SET name='after',run_state='active',updated_at=? WHERE id='project'",
                (tx.now,),
            )
            tx.history("project", "project.activated", "project", "project", {})

    def test_snapshot_keeps_one_commit_when_writer_commits_between_selects(self) -> None:
        original_read = self.ledger.read
        test = self
        committed = False

        class ProjectCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                nonlocal committed
                row = self.cursor.fetchone()
                # project SELECT가 끝난 뒤 별도 writer를 commit한다.
                test.assertEqual([], self.cursor.fetchall())
                self.cursor.close()
                test._commit_writer()
                committed = True
                return row

        class ReadConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, parameters=()):
                cursor = self.connection.execute(sql, parameters)
                if sql == "SELECT * FROM projects WHERE id = ?":
                    return ProjectCursor(cursor)
                return cursor

        @contextmanager
        def controlled_read():
            with original_read() as connection:
                yield ReadConnection(connection)

        with patch.object(self.ledger, "read", controlled_read):
            snapshot = self.ledger.project_snapshot("project")
        self.assertTrue(committed)
        self.assertEqual("before", snapshot["project"]["name"])
        self.assertEqual("idle", snapshot["project"]["run_state"])
        self.assertEqual(1, snapshot["history_count"])
        self.assertTrue(snapshot["history_valid"])
        latest = self.ledger.project_snapshot("project")
        self.assertEqual("after", latest["project"]["name"])
        self.assertEqual("active", latest["project"]["run_state"])
        self.assertEqual(2, latest["history_count"])
        self.assertTrue(latest["history_valid"])

    def test_full_history_tampering_is_still_rejected(self) -> None:
        self._commit_writer()
        with self.ledger.transaction() as tx:
            # disposable fixture에서만 trigger를 내려 첫 이력의 변조를 재현한다.
            tx.connection.execute("DROP TRIGGER tr_engine_history_no_update")
            tx.connection.execute(
                "UPDATE history_events SET payload_json='{}' WHERE sequence=1",
            )
            tx.connection.execute(
                "UPDATE history_events SET event_type='tampered' WHERE sequence=1",
            )
        self.assertFalse(self.ledger.verify_history("project"))
        snapshot = self.ledger.project_snapshot("project")
        self.assertEqual(2, snapshot["history_count"])
        self.assertFalse(snapshot["history_valid"])

    def test_read_still_allows_callers_to_begin_their_own_snapshot(self) -> None:
        with self.ledger.read() as connection:
            connection.execute("BEGIN")
            before = connection.execute("SELECT name FROM projects WHERE id='project'").fetchone()[0]
            self._commit_writer()
            after = connection.execute("SELECT name FROM projects WHERE id='project'").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0])
            connection.rollback()
        self.assertEqual("after", self.ledger.project_snapshot("project")["project"]["name"])

    def test_missing_project_keeps_the_existing_error(self) -> None:
        with self.assertRaisesRegex(EngineLedgerError, "프로젝트를 찾을 수 없습니다"):
            self.ledger.project_snapshot("missing")


if __name__ == "__main__":
    unittest.main()
