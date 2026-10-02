"""정상 source를 사용하며 nonzero 원본의 native recovery와 WAL-before-DDL을 구별한다."""
from contextlib import closing
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
from flowmarshal.engine.ledger import SQLiteEngineLedger, EngineLedgerError

def crash_nonzero_delete(path):
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        connection.execute('PRAGMA cache_size=10')
        connection.execute('PRAGMA cache_spill=ON')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE transient_payload(value BLOB)')
        connection.executemany('INSERT INTO transient_payload VALUES (zeroblob(8192))', [()] * 80)
        os._exit(83)

def crash_wal_before_ddl(path):
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        connection.execute('BEGIN IMMEDIATE')
        os._exit(84)

def spawn(target, path, expected):
    process = multiprocessing.get_context('spawn').Process(target=target, args=(str(path),))
    process.start()
    try:
        process.join(15)
        assert not process.is_alive(), 'synthetic writer timeout'
        assert process.exitcode == expected, process.exitcode
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()

def observe_gate(ledger):
    original_connect = ledger._connect
    original_stat = Path.stat
    acquired = []
    samples = []
    def connect(**options):
        connection = original_connect(**options)
        acquired.append(connection)
        return connection
    def stat(target, *args, **options):
        result = original_stat(target, *args, **options)
        if target == ledger.path:
            connection = acquired[0]
            samples.append(dict(in_transaction=connection.in_transaction, recovered_size=result.st_size,
                                application_id=connection.execute('PRAGMA application_id').fetchone()[0],
                                user_version=connection.execute('PRAGMA user_version').fetchone()[0],
                                objects=[tuple(row) for row in connection.execute('SELECT name FROM sqlite_master')],
                                journal_mode=connection.execute('PRAGMA journal_mode').fetchone()[0]))
        return result
    with patch.object(ledger, '_connect', connect), patch.object(Path, 'stat', stat):
        try:
            ledger.initialize()
        except EngineLedgerError as error:
            rejection = str(error)
        else:
            raise AssertionError('nonzero external residue was adopted')
    assert len(samples) == 1
    sample = samples[0]
    assert sample['in_transaction'] and sample['recovered_size'] > 0
    assert sample['application_id'] == sample['user_version'] == 0 and sample['objects'] == []
    assert not ledger.artifact_root.exists()
    for connection in acquired:
        try:
            connection.execute('SELECT 1')
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError('acquired connection leaked')
    return rejection, sample

def nonzero_original_hot_journal(root):
    path = root / 'nonzero-original.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA user_version=0')
    before = path.read_bytes()
    assert len(before) == 4096
    spawn(crash_nonzero_delete, path, 83)
    crashed_size = path.stat().st_size
    journal = Path(str(path) + '-journal')
    journal_size = journal.stat().st_size
    magic = journal.read_bytes()[:8].hex()
    assert crashed_size > len(before) and journal_size > 512
    assert magic == 'd9d505f920a163d7'
    ledger = SQLiteEngineLedger(path, artifact_root=root / 'nonzero-artifacts')
    rejection, sample = observe_gate(ledger)
    assert path.read_bytes() == before
    assert sample['recovered_size'] == len(before) and sample['journal_mode'] == 'delete'
    print(json.dumps(dict(case='nonzero_original_DELETE_hot_journal', child_exit=83,
                         original_size=len(before), crashed_size=crashed_size, hot_journal_size=journal_size,
                         hot_journal_magic=magic, same_connection_gate_sample=sample,
                         rejection=rejection, original_main_bytes_restored_by_native_recovery=True,
                         original_main_sha256=hashlib.sha256(before).hexdigest(),
                         engine_adoption=False, recovery_is_SQLite_native=True,
                         journal_exists_after=journal.exists()), ensure_ascii=False, sort_keys=True))

def wal_before_ddl(root):
    path = root / 'wal-before-ddl.sqlite3'
    spawn(crash_wal_before_ddl, path, 84)
    crashed_size = path.stat().st_size
    assert crashed_size > 0
    ledger = SQLiteEngineLedger(path, artifact_root=root / 'wal-artifacts')
    with closing(sqlite3.connect(path)) as keeper:
        assert keeper.execute('SELECT name FROM sqlite_master').fetchall() == []
        assert keeper.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        watched = [path, Path(str(path) + '-wal')]
        before = {p.name:p.read_bytes() for p in watched if p.exists()}
        rejection, sample = observe_gate(ledger)
        assert before == {p.name:p.read_bytes() for p in watched if p.exists()}
        assert sample['journal_mode'] == 'wal'
        assert keeper.execute('SELECT name FROM sqlite_master').fetchall() == []
        print(json.dumps(dict(case='old_WAL_before_DDL_process_exit', child_exit=84, crashed_size=crashed_size,
                             same_connection_gate_sample=sample, rejection=rejection,
                             main_and_held_WAL_bytes_preserved=True,
                             hashes={name:hashlib.sha256(data).hexdigest() for name,data in before.items()},
                             engine_adoption=False, shm_byte_identity='NOT_ASSERTED'), ensure_ascii=False, sort_keys=True))

if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        nonzero_original_hot_journal(root)
        wal_before_ddl(root)
    print('INDEPENDENT_RECOVERY_BOUNDARIES_PASS; Linux spawn only; no engine provenance claim')
