"""추가 독립 lifecycle 검사. 합성 fixture만 만들고 source를 변경하지 않는다."""
from __future__ import annotations
import ast
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from review_initialize_v2 import HERE, module, SQLiteEngineLedger, EngineLedgerError


class InitializationLifecycleReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='synthetic-lifecycle-', dir=HERE)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fixture.sqlite3'
        self.ledger = SQLiteEngineLedger(self.path)

    def test_malformed_existing_db_closes_connection_and_preserves_bytes(self):
        self.path.write_bytes(b'synthetic-not-a-database' * 100)
        before = self.path.read_bytes(); created = []; original = sqlite3.connect
        class Tracking(sqlite3.Connection):
            explicitly_closed = False
            def close(self):
                self.explicitly_closed = True
                return super().close()
        def connect(*args, **kw):
            connection = original(*args, **kw, factory=Tracking)
            created.append(connection)
            return connection
        try:
            with patch.object(sqlite3, 'connect', connect):
                with self.assertRaises(sqlite3.DatabaseError): self.ledger.initialize()
            self.assertEqual(before, self.path.read_bytes())
            self.assertTrue(created)
            self.assertTrue(all(c.explicitly_closed for c in created), 'configuration failure leaked acquired connection')
        finally:
            for c in created: c.close()

    def test_all_schema_objects_match_original_executescript_exactly(self):
        baseline = Path(os.environ['VM_BASELINE']) / 'src/flowmarshal/engine/ledger.py'
        tree = ast.parse(baseline.read_text())
        original_schema = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCHEMA_SQL' for t in node.targets))
        self.assertEqual(original_schema, module.SCHEMA_SQL)
        sql = 'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'
        with sqlite3.connect(':memory:') as expected:
            expected.executescript(original_schema)
            expected_rows = expected.execute(sql).fetchall()
        self.ledger.initialize()
        with sqlite3.connect(self.path) as actual:
            self.assertEqual(expected_rows, actual.execute(sql).fetchall())
            self.assertEqual('ok', actual.execute('PRAGMA integrity_check').fetchone()[0])
            self.assertEqual([], actual.execute('PRAGMA foreign_key_check').fetchall())

    def test_after_commit_wal_failure_is_retryable_valid_database(self):
        original = self.ledger._connect
        class Connection:
            def __init__(self, c): self.c = c
            def __getattr__(self, name): return getattr(self.c, name)
            def execute(self, sql, *args):
                if sql == 'PRAGMA journal_mode = WAL': raise RuntimeError('synthetic postcommit WAL failure')
                return self.c.execute(sql, *args)
        with patch.object(self.ledger, '_connect', lambda **kw: Connection(original(**kw))):
            with self.assertRaisesRegex(RuntimeError, 'postcommit WAL failure'):
                self.ledger.initialize()
        self.ledger._assert_identity()
        with sqlite3.connect(self.path) as c:
            before = list(c.iterdump())
        self.ledger.initialize()
        with sqlite3.connect(self.path) as c:
            self.assertEqual(before, list(c.iterdump()))
            self.assertEqual('wal', c.execute('PRAGMA journal_mode').fetchone()[0])

    def test_borrowed_identity_connection_keeps_transaction_owned_by_caller(self):
        self.ledger.initialize()
        c = self.ledger._connect(configure_journal=False)
        try:
            c.execute('BEGIN IMMEDIATE')
            self.ledger._assert_identity(connection=c)
            self.assertTrue(c.in_transaction)
            c.execute('PRAGMA user_version=9')
            with self.assertRaises(EngineLedgerError): self.ledger._assert_identity(connection=c)
            self.assertTrue(c.in_transaction)
            c.rollback()
            self.assertEqual(4, c.execute('PRAGMA user_version').fetchone()[0])
        finally: c.close()

    def test_uncommitted_schema_and_identity_are_invisible_to_other_connection(self):
        original = self.ledger._connect; observed = []
        class Connection:
            def __init__(self, c): self.c = c
            def __getattr__(self, name): return getattr(self.c, name)
            def commit(self):
                with sqlite3.connect(self_path) as reader:
                    observed.append((reader.execute('SELECT name FROM sqlite_master').fetchall(), reader.execute('PRAGMA application_id').fetchone(), reader.execute('PRAGMA user_version').fetchone()))
                return self.c.commit()
        self_path = self.path
        with patch.object(self.ledger, '_connect', lambda **kw: Connection(original(**kw))):
            self.ledger.initialize()
        self.assertEqual([([], (0,), (0,))], observed)
        self.ledger._assert_identity()

if __name__ == '__main__': unittest.main(verbosity=2)
