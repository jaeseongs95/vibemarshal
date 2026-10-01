"""독립 초기화 검토: 외부 연결은 일회용 합성 DB에만 접근한다."""
from __future__ import annotations
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ['VM_SOURCE']).resolve()
sys.path.insert(0, str(SOURCE / 'src'))
from flowmarshal.engine import ledger as module
from flowmarshal.engine.ledger import SQLiteEngineLedger, EngineLedgerError


def foreign_writer(path, queue):
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE independent_app_data(value TEXT)')
        c.execute("INSERT INTO independent_app_data VALUES ('preserve-me')")
        c.execute('PRAGMA application_id=12345')
        c.execute('PRAGMA user_version=9')
    queue.put('committed')


def crash_writer(path, phase):
    ledger = SQLiteEngineLedger(path)
    original = ledger._connect
    class Connection:
        def __init__(self, c): self.c = c
        def __getattr__(self, name): return getattr(self.c, name)
        def executescript(self, sql):
            result = self.c.executescript(sql)
            if phase == 'schema': os._exit(71)
            return result
        def execute(self, sql, *args):
            result = self.c.execute(sql, *args)
            if phase == 'schema' and sql.lstrip().startswith('CREATE TRIGGER tr_engine_usage_reconciliation_no_delete'):
                os._exit(71)
            if phase == 'identity' and sql.startswith('PRAGMA application_id ='):
                os._exit(72)
            return result
    ledger._connect = lambda **kw: Connection(original(**kw))
    ledger.initialize()


class IndependentInitializeReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='synthetic-review-', dir=HERE)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fixture.sqlite3'
        self.ledger = SQLiteEngineLedger(self.path)

    def test_existing_empty_db_became_foreign_before_lock_must_be_rejected(self):
        with sqlite3.connect(self.path) as c:
            c.execute('PRAGMA user_version=0')
        original = self.ledger._connect
        committed = []
        def connect(*, readonly=False, **options):
            if not readonly and not committed:
                ctx = mp.get_context('fork'); queue = ctx.Queue()
                child = ctx.Process(target=foreign_writer, args=(str(self.path), queue))
                child.start(); child.join(10)
                self.assertEqual(0, child.exitcode)
                self.assertEqual('committed', queue.get(timeout=2))
                committed.append(True)
            return original(readonly=readonly, **options)
        with patch.object(self.ledger, '_connect', connect):
            with self.assertRaises(EngineLedgerError):
                self.ledger.initialize()
        # Baseline refuses the already-existing empty DB before invoking a writer.
        if committed:
            with sqlite3.connect(self.path) as c:
                self.assertEqual(12345, c.execute('PRAGMA application_id').fetchone()[0])
                self.assertEqual(9, c.execute('PRAGMA user_version').fetchone()[0])
                self.assertEqual([('independent_app_data',)], c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

    def test_existing_engine_data_and_schema_are_unchanged_by_initialize(self):
        self.ledger.initialize()
        with self.ledger.transaction() as tx:
            tx.connection.execute("INSERT INTO projects(id,name,root,artifact_root,run_state,created_at,updated_at) VALUES('project_synthetic','keep','synthetic','synthetic','idle','2026-10-01','2026-10-01')")
            tx.history('project_synthetic', 'synthetic.created', 'project', 'project_synthetic', {'keep': True})
        def snapshot():
            with sqlite3.connect(self.path) as c:
                return list(c.iterdump()), c.execute('PRAGMA application_id').fetchone(), c.execute('PRAGMA user_version').fetchone()
        before = snapshot(); file_before = self.path.read_bytes()
        self.ledger.initialize(); self.ledger.initialize()
        self.assertEqual(before, snapshot())
        self.assertEqual(file_before, self.path.read_bytes())
        self.assertTrue(self.ledger.verify_history('project_synthetic'))

    def test_schema_and_identity_share_one_transaction(self):
        original = self.ledger._connect; traces = []; phases = []
        class Connection:
            def __init__(self, c): self.c = c; c.set_trace_callback(traces.append)
            def __getattr__(self, name): return getattr(self.c, name)
            def executescript(self, sql):
                result = self.c.executescript(sql); phases.append(('schema', self.c.in_transaction)); return result
            def executemany(self, *args):
                phases.append(('metadata-before', self.c.in_transaction)); return self.c.executemany(*args)
            def execute(self, sql, *args):
                result = self.c.execute(sql, *args)
                if sql.lstrip().startswith('CREATE '): phases.append(('DDL', self.c.in_transaction))
                if sql.startswith('PRAGMA application_id =') or sql.startswith('PRAGMA user_version ='):
                    phases.append((sql, self.c.in_transaction))
                return result
        with patch.object(self.ledger, '_connect', lambda **kw: Connection(original(**kw))):
            self.ledger.initialize()
        self.assertTrue(all(value for _, value in phases), phases)
        boundary = [sql.strip().rstrip(';').upper() for sql in traces if sql.strip().upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK'))]
        self.assertEqual(['BEGIN IMMEDIATE', 'COMMIT'], boundary)

    def _crash_rollback(self, phase, code):
        child = mp.get_context('fork').Process(target=crash_writer, args=(str(self.path), phase))
        child.start(); child.join(10); self.assertEqual(code, child.exitcode)
        with sqlite3.connect(self.path) as c:
            self.assertEqual([], c.execute('SELECT name FROM sqlite_master').fetchall())
            self.assertEqual((0,), c.execute('PRAGMA application_id').fetchone())
            self.assertEqual((0,), c.execute('PRAGMA user_version').fetchone())
        self.ledger.initialize(); self.ledger._assert_identity()

    def test_process_exit_after_ddl_is_recoverable(self): self._crash_rollback('schema', 71)
    def test_process_exit_after_application_id_is_recoverable(self): self._crash_rollback('identity', 72)

    def test_foreign_view_only_database_is_preserved(self):
        with sqlite3.connect(self.path) as c: c.execute('CREATE VIEW foreign_view AS SELECT 7 AS keep')
        before = self.path.read_bytes()
        with self.assertRaises(EngineLedgerError): self.ledger.initialize()
        self.assertEqual(before, self.path.read_bytes())

    def test_rollback_failure_does_not_mask_primary_exception(self):
        class PrimaryFailure(RuntimeError): pass
        class RollbackFailure(RuntimeError): pass
        original = self.ledger._connect
        class Connection:
            def __init__(self, c): self.c = c
            def __getattr__(self, name): return getattr(self.c, name)
            def executemany(self, *args): raise PrimaryFailure('synthetic primary initialization error')
            def rollback(self): raise RollbackFailure('synthetic rollback failure')
        with patch.object(self.ledger, '_connect', lambda **kw: Connection(original(**kw))):
            with self.assertRaises(PrimaryFailure): self.ledger.initialize()

if __name__ == '__main__': unittest.main(verbosity=2)
