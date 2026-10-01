"""합성 DB 전용 SQLite 경계 검사. 제품 source는 수정하지 않는다."""
from __future__ import annotations
import hashlib, json, multiprocessing as mp, os, sqlite3, sys, tempfile, time, unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from typing import Any

HERE=Path(__file__).resolve().parent
SOURCE=Path(os.environ.get('VM_SOURCE',str(HERE.parent/'vm-portable'))).resolve()
sys.path.insert(0,str(SOURCE/'src'))
from flowmarshal.canonical import canonical_json,sha256_digest
from flowmarshal.engine import ledger as module
from flowmarshal.engine.ledger import SQLiteEngineLedger,SQLiteEngineHistoryReader,EngineLedgerError
from flowmarshal.engine.capabilities import role_execution_scope,CoreCapabilityError
from flowmarshal.engine.domain import RuntimeJob,RuntimeJobObservation,RuntimeJobObservationKind,RuntimeJobStatus,new_id

from flowmarshal.engine.service import EngineService

def make_service(ledger):
    return EngineService(ledger)

P='project_'+'1'*32
J='job_'+'2'*32
STAMP='2026-10-01T00:00:00Z'

def seed(ledger):
    ledger.initialize()
    with ledger.transaction() as tx:
        tx.connection.execute('INSERT INTO projects(id,name,root,artifact_root,run_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(P,'before',str(ledger.path.parent/'project'),str(ledger.artifact_root),'idle',STAMP,STAMP))
        tx.history(P,'project.created','project',P,{'nested':{'한글':[None,1,True]}})

def job(ledger,status='scheduled'):
    with ledger.transaction() as tx:
        tx.connection.execute('INSERT INTO runtime_jobs(id,project_id,kind,status,checkpoint_key,request_digest,request_json,absolute_deadline_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(J,P,'worker_turn',status,'synthetic-checkpoint',sha256_digest({}),'{}','2027-01-01T00:00:00Z',STAMP,STAMP))

def append_worker(path,start,queue,worker_id,count):
    try:
        ledger=SQLiteEngineLedger(path); start.wait()
        for i in range(count):
            with ledger.transaction() as tx: tx.history(P,'synthetic.concurrent','project',P,{'worker':worker_id,'i':i})
        queue.put(('ok',worker_id))
    except BaseException as exc: queue.put(('error',repr(exc)))

def claim_worker(path,start,queue):
    try:
        svc=make_service(SQLiteEngineLedger(path));start.wait()
        _,claimed,obs=svc._claim_runtime_job_start_epoch(J)
        queue.put(('ok',claimed,obs))
    except BaseException as exc: queue.put(('error',repr(exc)))

def lock_worker(path,ready,hold):
    ledger=SQLiteEngineLedger(path)
    with ledger.transaction() as tx:
        tx.connection.execute('UPDATE projects SET name=? WHERE id=?',('writer',P))
        ready.set();time.sleep(hold)

def crash_worker(path,ready):
    ledger=SQLiteEngineLedger(path)
    with ledger.transaction() as tx:
        tx.connection.execute("UPDATE projects SET name='uncommitted' WHERE id=?",(P,))
        tx.history(P,'synthetic.uncommitted','project',P,{})
        ready.set();os._exit(9)

class SQLiteBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='sqlite-boundary-',dir=HERE/'artifacts')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ledger=SQLiteEngineLedger(self.root/'합성 ledger #?.sqlite3');seed(self.ledger)
    def run_children(self,target,n,*args):
        ctx=mp.get_context('fork');start=ctx.Event();queue=ctx.Queue()
        procs=[ctx.Process(target=target,args=(str(self.ledger.path),start,queue,*args(i))) for i in range(n)] if args else [ctx.Process(target=target,args=(str(self.ledger.path),start,queue)) for _ in range(n)]
        for p in procs:p.start()
        start.set();out=[queue.get(timeout=30) for p in procs]
        for p in procs:p.join(30);self.assertEqual(0,p.exitcode)
        self.assertTrue(all(x[0]=='ok' for x in out),out)
        return out
    def count(self,table):
        with self.ledger.read() as c:return c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def test_01_connection_pragmas_and_uri_escaping(self):
        with self.ledger.transaction() as tx:
            c=tx.connection
            self.assertEqual(1,c.execute('PRAGMA foreign_keys').fetchone()[0]);self.assertEqual(10000,c.execute('PRAGMA busy_timeout').fetchone()[0])
            self.assertEqual('wal',c.execute('PRAGMA journal_mode').fetchone()[0]);self.assertEqual(2,c.execute('PRAGMA synchronous').fetchone()[0])
        with self.ledger.read() as c:self.assertEqual(1,c.execute('PRAGMA query_only').fetchone()[0])
        self.assertTrue(self.ledger.verify_history(P))
    def test_02_foreign_keys_reject_orphan_and_rollback(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.transaction() as tx:
                tx.connection.execute("UPDATE projects SET name='bad' WHERE id=?",(P,))
                tx.history('absent','orphan','project','absent',{})
        self.assertEqual('before',self.ledger.project_snapshot(P)['project']['name']);self.assertEqual(1,self.count('history_events'))
        with self.ledger.read() as c:self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())
    def test_03_baseexception_rolls_back_state_and_history(self):
        class Abort(BaseException):pass
        with self.assertRaises(Abort):
            with self.ledger.transaction() as tx:
                tx.connection.execute("UPDATE projects SET name='bad' WHERE id=?",(P,));tx.history(P,'bad','project',P,{});raise Abort()
        self.assertEqual('before',self.ledger.project_snapshot(P)['project']['name']);self.assertEqual(1,self.count('history_events'))
    def test_04_read_handle_refuses_main_and_attached_writes(self):
        auxiliary=self.root/'aux.sqlite3'
        with sqlite3.connect(auxiliary) as c:c.execute('CREATE TABLE allowed(x)')
        with self.ledger.read() as c:
            with self.assertRaises(sqlite3.OperationalError):c.execute("UPDATE projects SET name='bad'")
            c.execute('ATTACH DATABASE ? AS aux',(str(auxiliary),))
            with self.assertRaises(sqlite3.OperationalError):c.execute('INSERT INTO aux.allowed VALUES(1)')
        with sqlite3.connect(auxiliary) as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM allowed').fetchone()[0])
    def test_05_role_scope_denies_ledger_read_and_write(self):
        with role_execution_scope('synthetic-worker'):
            with self.assertRaises(CoreCapabilityError):
                with self.ledger.read():pass
            with self.assertRaises(CoreCapabilityError):
                with self.ledger.transaction():pass
    def test_06_connections_close_on_success_and_exception(self):
        for fail in [False,True]:
            captured=None
            try:
                with self.ledger.read() as c:
                    captured=c
                    if fail:raise RuntimeError('fault')
            except RuntimeError:pass
            with self.assertRaises(sqlite3.ProgrammingError):captured.execute('SELECT 1')
    def test_07_snapshot_releases_reader_before_hashing(self):
        original=module._verify_history_rows
        def verify(rows):
            with sqlite3.connect(self.ledger.path) as c:
                busy,log,checkpointed=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                self.assertEqual(0,busy)
            return original(rows)
        with patch.object(module,'_verify_history_rows',verify):self.assertTrue(self.ledger.project_snapshot(P)['history_valid'])
    def test_08_wal_read_snapshot_survives_process_writer(self):
        ctx=mp.get_context('fork');ready=ctx.Event()
        with self.ledger.read() as c:
            c.execute('BEGIN');self.assertEqual('before',c.execute('SELECT name FROM projects').fetchone()[0])
            p=ctx.Process(target=lock_worker,args=(str(self.ledger.path),ready,.05));p.start();self.assertTrue(ready.wait(5));p.join(5);self.assertEqual(0,p.exitcode)
            self.assertEqual('before',c.execute('SELECT name FROM projects').fetchone()[0])
        self.assertEqual('writer',self.ledger.project_snapshot(P)['project']['name'])
    def test_09_busy_writer_waits_then_succeeds(self):
        ctx=mp.get_context('fork');ready=ctx.Event();p=ctx.Process(target=lock_worker,args=(str(self.ledger.path),ready,.35));p.start();self.assertTrue(ready.wait(5));t=time.monotonic()
        with self.ledger.transaction() as tx:tx.history(P,'after.wait','project',P,{})
        elapsed=time.monotonic()-t;p.join(5);self.assertEqual(0,p.exitcode);self.assertGreater(elapsed,.15);self.assertLess(elapsed,5);self.assertTrue(self.ledger.verify_history(P))
    def test_10_multi_process_history_has_no_gaps_or_forks(self):
        ctx=mp.get_context('fork');start=ctx.Event();q=ctx.Queue();procs=[ctx.Process(target=append_worker,args=(str(self.ledger.path),start,q,i,25)) for i in range(6)]
        for p in procs:p.start()
        start.set();out=[q.get(timeout=30) for p in procs]
        for p in procs:p.join(30);self.assertEqual(0,p.exitcode)
        self.assertTrue(all(x[0]=='ok' for x in out),out);self.assertEqual(151,self.count('history_events'));self.assertTrue(self.ledger.verify_history(P))
    def test_11_process_crash_rolls_back_uncommitted_wal(self):
        ctx=mp.get_context('fork');ready=ctx.Event();p=ctx.Process(target=crash_worker,args=(str(self.ledger.path),ready));p.start();self.assertTrue(ready.wait(5));p.join(5);self.assertEqual(9,p.exitcode)
        s=self.ledger.project_snapshot(P);self.assertEqual('before',s['project']['name']);self.assertEqual(1,s['history_count']);self.assertTrue(s['history_valid'])
        with self.ledger.transaction() as tx:tx.history(P,'after.crash','project',P,{})
        self.assertTrue(self.ledger.verify_history(P))
    def test_12_exact_cas_method_eight_processes_one_winner(self):
        job(self.ledger);out=self.run_children(claim_worker,8)
        self.assertEqual(1,sum(x[1] for x in out));self.assertEqual(1,self.count('runtime_job_observations'));self.assertEqual(2,self.count('history_events'));self.assertTrue(self.ledger.verify_history(P))
    def test_13_exact_cas_rejected_terminal_state_has_no_side_effect(self):
        job(self.ledger,'consumed');svc=make_service(self.ledger)
        state,won,obs=svc._claim_runtime_job_start_epoch(J)
        self.assertFalse(won);self.assertIsNone(obs);self.assertEqual('consumed',state.status.value);self.assertEqual(0,self.count('runtime_job_observations'));self.assertEqual(1,self.count('history_events'))
    def test_14_cas_observation_failure_rolls_back_running_transition(self):
        job(self.ledger);svc=make_service(self.ledger)
        with patch.object(svc,'_append_runtime_job_observation',side_effect=RuntimeError('fault')):
            with self.assertRaises(RuntimeError):svc._claim_runtime_job_start_epoch(J)
        with self.ledger.read() as c:self.assertEqual('scheduled',c.execute('SELECT status FROM runtime_jobs').fetchone()[0])
        self.assertEqual(1,self.count('history_events'));self.assertEqual(0,self.count('runtime_job_observations'))
    def test_15_partial_unique_active_job_enforced(self):
        job(self.ledger)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.transaction() as tx:
                tx.connection.execute("INSERT INTO runtime_jobs SELECT ?,project_id,kind,status,?,request_digest,request_json,attempt_id,task_id,thread_id,turn_id,absolute_deadline_at,provider_terminal_status,result_digest,result_json,created_at,started_at,ended_at,updated_at FROM runtime_jobs",(new_id('job'),'second'))
        self.assertEqual(1,self.count('runtime_jobs'))
    def test_16_current_snapshot_unique_per_goal_not_per_project(self):
        with self.ledger.transaction() as tx:
            for g in ['goal-A','goal-B']:
                tx.connection.execute('INSERT INTO state_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',(g,P,g,1,g,'semantic','scope','{}',STAMP,1))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.transaction() as tx:tx.connection.execute('INSERT INTO state_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',('duplicate',P,'goal-A',2,'digest-2','semantic','scope','{}',STAMP,1))
        self.assertEqual(2,self.count('state_snapshots'))
    def test_17_history_append_only_and_hash_tamper_detection(self):
        for query in ['UPDATE history_events SET event_type="bad"','DELETE FROM history_events']:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'APPEND_ONLY'):
                with self.ledger.transaction() as tx:tx.connection.execute(query)
        with self.ledger.transaction() as tx:
            tx.connection.execute('DROP TRIGGER tr_engine_history_no_update');tx.connection.execute("UPDATE history_events SET previous_hash='forged'")
        self.assertFalse(self.ledger.verify_history(P));self.assertFalse(self.ledger.project_snapshot(P)['history_valid'])
    def test_18_identity_rejects_foreign_without_mutation(self):
        path=self.root/'foreign.sqlite3'
        with sqlite3.connect(path) as c:c.execute('CREATE TABLE private(x)');c.execute('INSERT INTO private VALUES(7)')
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(EngineLedgerError):SQLiteEngineLedger(path).initialize()
        self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest())
    def test_19_schema3_rejected_by_writer_but_read_with_original_semantics(self):
        historical=self.root/'synthetic-schema3.sqlite3'
        # 최소 합성 schema3: 새 execution/effect 필드가 아예 없는 역사 reader fixture.
        with sqlite3.connect(historical) as c:
            c.execute('CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            c.executemany('INSERT INTO schema_meta VALUES(?,?)',[('schema_id',module.ENGINE_SCHEMA_ID),('schema_revision','3')])
            c.execute(f'PRAGMA application_id={module.SQLITE_APPLICATION_ID}');c.execute('PRAGMA user_version=3')
            c.execute('CREATE TABLE provider_calls(id TEXT PRIMARY KEY,project_id TEXT,status TEXT,actual_tokens INTEGER)')
            c.executemany('INSERT INTO provider_calls VALUES(?,?,?,?)',[('unknown',P,'usage_unknown',None),('zero',P,'settled',0),('reserved',P,'reserved',None)])
        digest=hashlib.sha256(historical.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EngineLedgerError,'역사'):SQLiteEngineLedger(historical).initialize()
        reader=SQLiteEngineHistoryReader(historical);self.assertEqual(3,reader.schema_revision)
        history=reader.provider_call_history(P);self.assertEqual(['unavailable','measured','unobserved'],[x['usage_measurement_status'] for x in history])
        self.assertEqual(['usage_unknown','settled','reserved'],[x['historical_status'] for x in history]);self.assertTrue(all(x['execution_status'] is None and x['effect_status'] is None for x in history))
        self.assertEqual(digest,hashlib.sha256(historical.read_bytes()).hexdigest())
    def test_20_bad_identity_versions_fail_closed(self):
        for pragma,value in [('application_id',123),('user_version',5)]:
            with self.ledger.transaction() as tx:tx.connection.execute(f'PRAGMA {pragma}={value}')
            with self.assertRaises(EngineLedgerError):self.ledger.initialize()
            with self.ledger.transaction() as tx:tx.connection.execute(f'PRAGMA {pragma}={module.SQLITE_APPLICATION_ID if pragma=="application_id" else 4}')
    def test_21_read_does_not_implicitly_pin_snapshot(self):
        with self.ledger.read() as c:
            self.assertEqual('before',c.execute('SELECT name FROM projects').fetchone()[0])
            with self.ledger.transaction() as tx:tx.connection.execute("UPDATE projects SET name='after'")
            self.assertEqual('after',c.execute('SELECT name FROM projects').fetchone()[0])
    def test_22_history_project_isolation(self):
        p2='project_'+'3'*32
        with self.ledger.transaction() as tx:
            tx.connection.execute('INSERT INTO projects(id,name,root,artifact_root,run_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(p2,'second','/synthetic/second','/synthetic/second/artifacts','idle',STAMP,STAMP))
            tx.history(p2,'created','project',p2,{})
            tx.history(P,'updated','project',P,{})
        self.assertTrue(self.ledger.verify_history(P));self.assertTrue(self.ledger.verify_history(p2));self.assertEqual(1,self.ledger.project_snapshot(p2)['history_count'])
    def test_23_strict_types_and_check_constraints(self):
        for column,value in [('run_state','made-up'),('created_at',None)]:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.ledger.transaction() as tx:tx.connection.execute(f'UPDATE projects SET {column}=?',(value,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.transaction() as tx:tx.connection.execute('UPDATE projects SET name=?',(b'blob',))
    def test_24_transaction_handles_closed_after_rollback(self):
        captured=None
        with self.assertRaises(RuntimeError):
            with self.ledger.transaction() as tx:captured=tx.connection;raise RuntimeError('fault')
        with self.assertRaises(sqlite3.ProgrammingError):captured.execute('SELECT 1')

    def test_25_busy_timeout_expires_without_partial_write(self):
        ctx=mp.get_context('fork');ready=ctx.Event();p=ctx.Process(target=lock_worker,args=(str(self.ledger.path),ready,10.7));p.start();self.assertTrue(ready.wait(5));started=time.monotonic()
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                with self.ledger.transaction() as tx:tx.history(P,'must.not.commit','project',P,{})
            elapsed=time.monotonic()-started;self.assertGreater(elapsed,9.5);self.assertLess(elapsed,12)
        finally:p.join(15)
        self.assertEqual(0,p.exitcode);self.assertEqual(1,self.count('history_events'))
        with self.ledger.transaction() as tx:tx.history(P,'after.timeout','project',P,{})
        self.assertTrue(self.ledger.verify_history(P))
    def test_26_project_snapshot_all_query_boundaries_share_one_commit(self):
        original_read=self.ledger.read;commits=[0];test=self
        class Cursor:
            def __init__(self,inner,sql):self.inner=inner;self.sql=sql
            def __getattr__(self,name):return getattr(self.inner,name)
            def advance(self):
                if not self.sql.startswith('SELECT'):return
                commits[0]+=1
                with test.ledger.transaction() as tx:
                    tx.connection.execute('UPDATE projects SET name=?',('writer-'+str(commits[0]),));tx.history(P,'interleave.'+str(commits[0]),'project',P,{})
            def fetchone(self):row=self.inner.fetchone();self.advance();return row
            def fetchall(self):rows=self.inner.fetchall();self.advance();return rows
        class Conn:
            def __init__(self,inner):self.inner=inner
            def execute(self,sql,p=()):return Cursor(self.inner.execute(sql,p),sql)
        @contextmanager
        def read():
            with original_read() as c:yield Conn(c)
        with patch.object(self.ledger,'read',read):snapshot=self.ledger.project_snapshot(P)
        self.assertEqual(7,commits[0]);self.assertEqual('before',snapshot['project']['name']);self.assertEqual(1,snapshot['history_count']);self.assertTrue(snapshot['history_valid'])
        latest=self.ledger.project_snapshot(P);self.assertEqual('writer-7',latest['project']['name']);self.assertEqual(8,latest['history_count']);self.assertTrue(latest['history_valid'])

if __name__=='__main__':unittest.main(verbosity=2)
