"""확인된 재현 3개의 제안 acceptance regression. 현재 candidate에서는 실패한다.

원본 source를 수정하지 않고 synthetic tempfile DB만 쓴다. concurrent initialize의
멱등 수용과 corrupted JSON의 false 반환은 제안 계약이며 기존 명시 보장은 아니다.
"""
import multiprocessing as mp, sqlite3, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from test_sqlite_boundaries import HERE, SQLiteEngineLedger, seed, P

def concurrent_init(path,barrier,q):
    class SyncedLedger(SQLiteEngineLedger):
        def _connect(self,*,readonly=False):
            if not readonly:barrier.wait(timeout=10)
            return super()._connect(readonly=readonly)
    try:SyncedLedger(path).initialize();q.put('ok')
    except BaseException as e:q.put(type(e).__name__+': '+str(e))

class ProposedRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='sqlite-proposal-',dir=HERE/'artifacts');self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'new.sqlite3'
    def test_init_failure_can_be_retried_without_deleting_db(self):
        ledger=SQLiteEngineLedger(self.path);original=ledger._connect
        class FaultConnection:
            def __init__(self,c):self.c=c
            def __getattr__(self,n):return getattr(self.c,n)
            def executemany(self,*args,**kwargs):raise RuntimeError('injected metadata failure')
        with patch.object(ledger,'_connect',lambda **k:FaultConnection(original(**k))):
            with self.assertRaises(RuntimeError):ledger.initialize()
        ledger.initialize()
        with ledger.read() as c:self.assertEqual('4',c.execute("SELECT value FROM schema_meta WHERE key='schema_revision'").fetchone()[0])
    def test_concurrent_first_initialization_is_idempotent(self):
        ctx=mp.get_context('fork');barrier=ctx.Barrier(2);q=ctx.Queue();ps=[ctx.Process(target=concurrent_init,args=(str(self.path),barrier,q)) for _ in range(2)]
        for p in ps:p.start()
        out=[q.get(timeout=20) for _ in ps]
        for p in ps:p.join(20)
        self.assertEqual(['ok','ok'],sorted(out))
    def test_corrupt_history_json_is_reported_invalid_without_crash(self):
        ledger=SQLiteEngineLedger(self.path);seed(ledger)
        with ledger.transaction() as tx:
            tx.connection.execute('DROP TRIGGER tr_engine_history_no_update');tx.connection.execute("UPDATE history_events SET payload_json='{broken'")
        self.assertFalse(ledger.verify_history(P))
        self.assertFalse(ledger.project_snapshot(P)['history_valid'])

if __name__=='__main__':unittest.main(verbosity=2)
