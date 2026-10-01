"""합성 malformed DB/설정 오류에서 연결 획득 후 cleanup 회귀를 고정한다."""
import os,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(Path(os.environ['VM_SOURCE']).resolve()/'src'))
from flowmarshal.engine.ledger import SQLiteEngineLedger

class ConnectionSetupCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='setup-cleanup-',dir=ROOT);self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'fixture.sqlite3';self.ledger=SQLiteEngineLedger(self.path)
    def test_malformed_existing_file_closes_acquired_connection_and_preserves_bytes(self):
        original_bytes=b'synthetic-not-a-sqlite-database';self.path.write_bytes(original_bytes)
        real_connect=sqlite3.connect;acquired=[]
        class Tracked(sqlite3.Connection):
            closed=False
            def close(self):self.closed=True;return super().close()
        def connect(*a,**kw):kw['factory']=Tracked;c=real_connect(*a,**kw);acquired.append(c);return c
        with patch('sqlite3.connect',connect):
            with self.assertRaisesRegex(sqlite3.DatabaseError,'not a database'):self.ledger.initialize()
        self.assertTrue(acquired);self.assertTrue(all(c.closed for c in acquired));self.assertEqual(original_bytes,self.path.read_bytes())
    def test_pragma_configuration_failure_closes_writable_and_readonly_connections(self):
        self.ledger.initialize();real_connect=sqlite3.connect
        for readonly in (False,True):
            acquired=[]
            class Tracked(sqlite3.Connection):
                closed=False
                def close(self):self.closed=True;return super().close()
                def execute(self,sql,*a,**kw):
                    if sql=='PRAGMA busy_timeout = 10000':raise RuntimeError('synthetic configuration failure')
                    return super().execute(sql,*a,**kw)
            def connect(*a,**kw):kw['factory']=Tracked;c=real_connect(*a,**kw);acquired.append(c);return c
            with patch('sqlite3.connect',connect):
                with self.assertRaisesRegex(RuntimeError,'synthetic configuration failure'):self.ledger._connect(readonly=readonly)
            self.assertTrue(acquired);self.assertTrue(all(c.closed for c in acquired))
    def test_cleanup_failure_does_not_mask_configuration_failure(self):
        real_connect=sqlite3.connect
        class Tracked(sqlite3.Connection):
            def close(self):super().close();raise RuntimeError('secondary cleanup failure')
            def execute(self,sql,*a,**kw):raise ValueError('primary configuration failure')
        def connect(*a,**kw):kw['factory']=Tracked;return real_connect(*a,**kw)
        with patch('sqlite3.connect',connect):
            with self.assertRaisesRegex(ValueError,'primary configuration failure'):self.ledger._connect()

if __name__=='__main__':unittest.main(verbosity=2)
