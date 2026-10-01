"""V2 atomic initialize의 독립 acceptance: 격리 DB만 쓰며 V1 원본은 보존한다."""
from __future__ import annotations
import hashlib,multiprocessing as mp,os,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent
SOURCE=Path(os.environ['VM_SOURCE']).resolve()
sys.path.insert(0,str(SOURCE/'src'))
from flowmarshal.engine.ledger import SQLiteEngineLedger,EngineLedgerError,SQLITE_APPLICATION_ID

def foreign_writer(path,q):
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE independent_app_data(value TEXT)')
        c.execute("INSERT INTO independent_app_data VALUES('preserve-me')")
        c.execute('PRAGMA application_id=12345');c.execute('PRAGMA user_version=9')
    q.put('committed')

def blocked_writer(path,q):
    try:
        with sqlite3.connect(path,timeout=.1) as c:c.execute('CREATE TABLE should_not_exist(x)')
        q.put('unexpected-write')
    except sqlite3.OperationalError as e:q.put(str(e))

class RevalidationRegression(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='revalidation-',dir=ROOT);self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'synthetic.sqlite3';self.ledger=SQLiteEngineLedger(self.path)
    def assert_racing_foreign_db_rejected(self,preexisting):
        if preexisting:
            with sqlite3.connect(self.path) as c:c.execute('PRAGMA user_version=0')
        original=self.ledger._connect;before=[]
        def connect(*,readonly=False,**kwargs):
            if not readonly and not before:
                ctx=mp.get_context('fork');q=ctx.Queue();p=ctx.Process(target=foreign_writer,args=(str(self.path),q));p.start();p.join(10)
                self.assertEqual(0,p.exitcode);self.assertEqual('committed',q.get(timeout=2));before.append(self.path.read_bytes())
            return original(readonly=readonly,**kwargs)
        with patch.object(self.ledger,'_connect',connect):
            with self.assertRaises(EngineLedgerError):self.ledger.initialize()
        self.assertTrue(before);self.assertEqual(before[0],self.path.read_bytes())
        with sqlite3.connect(self.path) as c:
            self.assertEqual(12345,c.execute('PRAGMA application_id').fetchone()[0]);self.assertEqual(9,c.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual([('preserve-me',)],c.execute('SELECT value FROM independent_app_data').fetchall())
            self.assertEqual([('independent_app_data',)],c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            self.assertEqual('delete',c.execute('PRAGMA journal_mode').fetchone()[0])
    def test_existing_empty_db_becoming_foreign_is_rejected(self):self.assert_racing_foreign_db_rejected(True)
    def test_missing_path_becoming_foreign_is_rejected(self):self.assert_racing_foreign_db_rejected(False)
    def test_acceptance_reads_hold_authoritative_writer_transaction(self):
        with sqlite3.connect(self.path) as c:c.execute('PRAGMA user_version=0')
        original=self.ledger._connect;observations=[]
        def connect(**kwargs):
            c=original(**kwargs)
            def trace(sql):
                normalized=sql.strip().upper()
                if normalized in {'PRAGMA APPLICATION_ID','PRAGMA USER_VERSION','SELECT 1 FROM SQLITE_MASTER LIMIT 1'}:
                    observations.append((normalized,c.in_transaction))
            c.set_trace_callback(trace)
            return c
        with patch.object(self.ledger,'_connect',connect):self.ledger.initialize()
        self.assertTrue(observations)
        self.assertTrue(all(in_transaction for sql,in_transaction in observations),observations)
    def test_rollback_failure_preserves_primary_exception_and_connection_cleanup(self):
        class PrimaryFailure(RuntimeError):pass
        class RollbackFailure(RuntimeError):pass
        original=self.ledger._connect;connections=[]
        class Connection:
            def __init__(self,c):self.c=c;connections.append(c)
            def __getattr__(self,n):return getattr(self.c,n)
            def executemany(self,*args):raise PrimaryFailure('primary')
            def rollback(self):raise RollbackFailure('secondary')
        with patch.object(self.ledger,'_connect',lambda **kw:Connection(original(**kw))):
            with self.assertRaisesRegex(PrimaryFailure,'primary'):self.ledger.initialize()
        for c in connections:
            with self.assertRaises(sqlite3.ProgrammingError):c.execute('SELECT 1')
        self.ledger.initialize()
        with self.ledger.read() as c:self.assertEqual(SQLITE_APPLICATION_ID,c.execute('PRAGMA application_id').fetchone()[0])

if __name__=='__main__':unittest.main(verbosity=2)
