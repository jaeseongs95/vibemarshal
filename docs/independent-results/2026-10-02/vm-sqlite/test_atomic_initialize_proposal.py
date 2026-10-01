"""초기화 atomicity 제안의 failure/기존-DB 보호 회귀. 합성 DB만 사용한다."""
import hashlib,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_sqlite_boundaries import HERE,SQLiteEngineLedger,EngineLedgerError,module

class AtomicInitializeProposal(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='atomic-init-',dir=HERE/'artifacts');self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'new.sqlite3';self.ledger=SQLiteEngineLedger(self.path)
    def assert_empty_then_retry(self):
        with sqlite3.connect(self.path) as c:
            self.assertEqual([],c.execute('SELECT name FROM sqlite_master').fetchall())
            self.assertEqual(0,c.execute('PRAGMA application_id').fetchone()[0]);self.assertEqual(0,c.execute('PRAGMA user_version').fetchone()[0])
        self.ledger.initialize()
        with self.ledger.read() as c:
            self.assertEqual(module.SQLITE_APPLICATION_ID,c.execute('PRAGMA application_id').fetchone()[0]);self.assertEqual(4,c.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual('4',c.execute("SELECT value FROM schema_meta WHERE key='schema_revision'").fetchone()[0])
            self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0])
        self.ledger.initialize()
    def fault(self,phase,exc=RuntimeError):
        original=self.ledger._connect
        class Connection:
            def __init__(self,c):self.c=c
            def __getattr__(self,n):return getattr(self.c,n)
            def executemany(self,*a,**k):
                result=self.c.executemany(*a,**k)
                if phase=='metadata':raise exc('injected after metadata')
                return result
            def execute(self,sql,*a,**k):
                result=self.c.execute(sql,*a,**k)
                if phase=='identity' and sql.startswith('PRAGMA application_id ='):raise exc('injected after application_id')
                return result
            def commit(self):
                if phase=='commit':raise exc('injected before commit')
                return self.c.commit()
        with patch.object(self.ledger,'_connect',lambda **k:Connection(original(**k))):
            with self.assertRaises(exc):self.ledger.initialize()
        self.assert_empty_then_retry()
    def test_metadata_failure_rolls_back_schema_and_retries(self):self.fault('metadata')
    def test_identity_failure_rolls_back_metadata_and_retries(self):self.fault('identity')
    def test_commit_failure_rolls_back_everything_and_retries(self):self.fault('commit')
    def test_keyboard_interrupt_rolls_back_everything(self):self.fault('metadata',KeyboardInterrupt)
    def test_mid_schema_failure_rolls_back_ddl_and_retries(self):
        with patch.object(module,'SCHEMA_SQL',module.SCHEMA_SQL+'\nSELECT missing_synthetic_function();'):
            with self.assertRaises(sqlite3.OperationalError):self.ledger.initialize()
        self.assert_empty_then_retry()
    def test_nonzero_foreign_identity_is_never_adopted(self):
        for field,value in [('application_id',99),('user_version',1)]:
            path=self.path.with_name(field+'.sqlite3')
            with sqlite3.connect(path) as c:c.execute(f'PRAGMA {field}={value}')
            before=path.read_bytes()
            with self.assertRaises(EngineLedgerError):SQLiteEngineLedger(path).initialize()
            self.assertEqual(before,path.read_bytes())
    def test_existing_foreign_schema_and_rows_are_never_adopted(self):
        with sqlite3.connect(self.path) as c:c.execute('CREATE TABLE foreign_data(value)');c.execute('INSERT INTO foreign_data VALUES(7)')
        before=self.path.read_bytes()
        with self.assertRaises(EngineLedgerError):self.ledger.initialize()
        self.assertEqual(before,self.path.read_bytes())
    def test_zero_byte_and_empty_sqlite_are_accepted_without_deletion(self):
        self.path.touch();self.ledger.initialize()
        empty=self.path.with_name('empty.sqlite3')
        with sqlite3.connect(empty) as c:c.execute('PRAGMA user_version=0')
        self.assertGreater(empty.stat().st_size,0)
        SQLiteEngineLedger(empty).initialize()
        with sqlite3.connect(empty) as c:self.assertEqual(4,c.execute('PRAGMA user_version').fetchone()[0])

if __name__=='__main__':unittest.main(verbosity=2)
