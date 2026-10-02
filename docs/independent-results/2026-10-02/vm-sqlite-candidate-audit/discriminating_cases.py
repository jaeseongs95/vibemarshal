"""운영 자원 없이 실제 SQLite lock과 정책 경계를 독립 관측한다."""
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
from flowmarshal.engine import ledger as module


def emit(case, **values):
    print(json.dumps(dict(case=case, **values), sort_keys=True, ensure_ascii=False))


def empty(connection):
    assert connection.execute('SELECT name FROM sqlite_master').fetchall() == []
    assert connection.execute('PRAGMA application_id').fetchone()[0] == 0
    assert connection.execute('PRAGMA user_version').fetchone()[0] == 0


def valid(ledger):
    ledger._assert_identity()
    with ledger.read() as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert dict(connection.execute('SELECT key,value FROM schema_meta')) == {
            'schema_id': module.ENGINE_SCHEMA_ID, 'schema_revision': '4'}


class Proxy:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)


def real_commit_busy(root):
    path = root / 'commit-busy.sqlite3'
    ledger = module.SQLiteEngineLedger(path, artifact_root=root / 'commit-busy-artifacts')
    with closing(sqlite3.connect(path)) as reader:
        reader.execute('PRAGMA user_version = 0')
        reader.execute('BEGIN')
        reader.execute('SELECT name FROM sqlite_master').fetchall()
        original = ledger._connect
        acquired = []

        def connect(**options):
            connection = original(**options)
            connection.execute('PRAGMA busy_timeout = 50')
            acquired.append(connection)
            return connection

        with patch.object(ledger, '_connect', connect):
            try:
                ledger.initialize()
            except sqlite3.OperationalError as error:
                assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                observed_error = str(error)
            else:
                raise AssertionError('expected actual commit lock failure')
        empty(reader)
        assert not ledger.artifact_root.exists()
        for connection in acquired:
            try:
                connection.execute('SELECT 1')
            except sqlite3.ProgrammingError:
                pass
            else:
                raise AssertionError('acquired connection leaked')
        reader.rollback()
    with closing(sqlite3.connect(path)) as connection:
        empty(connection)
    ledger.initialize()
    valid(ledger)
    emit('real_SQLITE_BUSY_at_commit', error=observed_error, error_code=5,
         rollback_schema_and_identity_empty=True, acquired_connections_closed=True, retry='PASS')


def real_postcommit_wal_busy(root):
    path = root / 'wal-busy.sqlite3'
    ledger = module.SQLiteEngineLedger(path, artifact_root=root / 'wal-busy-artifacts')
    original = ledger._connect
    readers = []
    dumps = []

    class WALBusy(Proxy):
        def execute(self, sql, *args):
            if sql == 'PRAGMA journal_mode = WAL':
                assert not self.connection.in_transaction
                reader = sqlite3.connect(path)
                readers.append(reader)
                reader.execute('BEGIN')
                assert dict(reader.execute('SELECT key,value FROM schema_meta')) == {
                    'schema_id': module.ENGINE_SCHEMA_ID, 'schema_revision': '4'}
                dumps.append(list(reader.iterdump()))
            return self.connection.execute(sql, *args)

    def connect(**options):
        connection = original(**options)
        connection.execute('PRAGMA busy_timeout = 50')
        return WALBusy(connection)

    try:
        with patch.object(ledger, '_connect', connect):
            try:
                ledger.initialize()
            except sqlite3.OperationalError as error:
                assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                observed_error = str(error)
            else:
                raise AssertionError('expected actual WAL lock failure')
        assert not ledger.artifact_root.exists()
        valid(ledger)
        assert readers[0].execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    finally:
        for reader in readers:
            reader.rollback()
            reader.close()
    ledger.initialize()
    valid(ledger)
    with closing(sqlite3.connect(path)) as connection:
        assert list(connection.iterdump()) == dumps[0]
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert ledger.artifact_root.is_dir()
    emit('real_SQLITE_BUSY_after_commit_at_WAL', error=observed_error, error_code=5,
         committed_database='VALID', initial_artifact_root_absent=True, retry_preserved_dump=True,
         retry_journal='wal', retry_artifact_root_created=True)


def foreign_live_wal(root):
    path = root / 'foreign-live-wal.sqlite3'
    ledger = module.SQLiteEngineLedger(path, artifact_root=root / 'foreign-artifacts')
    with closing(sqlite3.connect(path)) as keeper:
        assert keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute('CREATE TABLE foreign_payload(value TEXT)')
        keeper.execute("INSERT INTO foreign_payload VALUES ('synthetic-only')")
        keeper.execute('PRAGMA application_id=12345')
        keeper.execute('PRAGMA user_version=9')
        keeper.commit()
        paths = [path, Path(str(path) + '-wal')]
        before = {p.name: p.read_bytes() for p in paths}
        dump = list(keeper.iterdump())
        try:
            ledger.initialize()
        except module.EngineLedgerError as error:
            observed_error = str(error)
        else:
            raise AssertionError('foreign database adopted')
        assert {p.name: p.read_bytes() for p in paths} == before
        assert list(keeper.iterdump()) == dump
        assert keeper.execute('PRAGMA application_id').fetchone()[0] == 12345
        assert keeper.execute('PRAGMA user_version').fetchone()[0] == 9
        assert keeper.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert not ledger.artifact_root.exists()
        emit('foreign_committed_data_in_live_WAL', rejection=observed_error,
             main_and_WAL_bytes_preserved=True, hashes={n: hashlib.sha256(v).hexdigest() for n, v in before.items()},
             dump_identity_and_WAL_mode_preserved=True, artifact_root_absent=True,
             shm_byte_identity='NOT_ASSERTED; transient SQLite coordination state')


def schema_equivalence(root):
    ledger = module.SQLiteEngineLedger(root / 'schema.sqlite3', artifact_root=root / 'schema-artifacts')
    ledger.initialize()
    query = 'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'
    with closing(sqlite3.connect(':memory:')) as reference, closing(sqlite3.connect(ledger.path)) as actual:
        reference.executescript(module.SCHEMA_SQL)
        expected = reference.execute(query).fetchall()
        got = actual.execute(query).fetchall()
        assert got == expected
        counts = Counter(row[0] for row in got)
        assert counts['trigger'] > 0
        encoded = json.dumps(got, separators=(',', ':'), ensure_ascii=False).encode()
        emit('static_schema_and_trigger_equivalence', object_counts=dict(counts),
             sqlite_master_sha256=hashlib.sha256(encoded).hexdigest(), exact_reference_equality=True)


def empty_policy_effects(root):
    observations = []
    for kind in ('header_only', 'previous_foreign_data_dropped'):
        path = root / (kind + '.sqlite3')
        with closing(sqlite3.connect(path)) as connection:
            if kind == 'header_only':
                connection.execute('PRAGMA user_version=0')
            else:
                connection.execute('PRAGMA secure_delete=OFF')
                connection.execute('CREATE TABLE old_foreign_data(value TEXT)')
                connection.execute('INSERT INTO old_foreign_data VALUES (?)', ('synthetic-disposable-marker' * 500,))
                connection.commit()
                connection.execute('DROP TABLE old_foreign_data')
                connection.commit()
            empty(connection)
            freelist = connection.execute('PRAGMA freelist_count').fetchone()[0]
            page_count = connection.execute('PRAGMA page_count').fetchone()[0]
        assert path.stat().st_size > 0
        before_size = path.stat().st_size
        marker_before = b'synthetic-disposable-marker' in path.read_bytes()
        ledger = module.SQLiteEngineLedger(path, artifact_root=root / (kind + '-artifacts'))
        ledger.initialize()
        valid(ledger)
        observations.append(dict(kind=kind, before_size=before_size, before_pages=page_count,
                                 before_freelist=freelist, synthetic_residual_marker_before=marker_before,
                                 candidate_adopted=True))
    emit('empty_zero_identity_policy_effects', observations=observations,
         acceptance_decision='PENDING_PARENT; observations only',
         predicate_proves='zero application_id, zero user_version, no sqlite_master objects',
         predicate_does_not_prove='new provenance, absence of prior data or empty freelist')


def initializer_double_failure(root):
    ledger = module.SQLiteEngineLedger(root / 'double-failure.sqlite3', artifact_root=root / 'double-artifacts')
    original = ledger._connect

    class DoubleFailure(Proxy):
        def execute(self, sql, *args):
            result = self.connection.execute(sql, *args)
            if sql.lstrip().startswith('CREATE TABLE'):
                raise ValueError('synthetic-primary-schema-error')
            return result

        def close(self):
            self.connection.close()
            raise RuntimeError('synthetic-secondary-finally-close-error')

    with patch.object(ledger, '_connect', lambda **options: DoubleFailure(original(**options))):
        try:
            ledger.initialize()
        except RuntimeError as error:
            assert isinstance(error.__context__, ValueError)
            error_type = type(error).__name__
            context_type = type(error.__context__).__name__
        else:
            raise AssertionError('expected double failure')
    with closing(sqlite3.connect(ledger.path)) as connection:
        empty(connection)
    emit('initializer_secondary_close_failure_limit', surfaced_error=error_type,
         original_error_in_context=context_type, atomic_rollback='PASS',
         primary_exception_preservation='NOT_GUARANTEED in initialize finally; inherited close call')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        real_commit_busy(root)
        real_postcommit_wal_busy(root)
        foreign_live_wal(root)
        schema_equivalence(root)
        empty_policy_effects(root)
        initializer_double_failure(root)
    print('INDEPENDENT_CASES_PASS; empty policy not decided; cleanup limit observed')
