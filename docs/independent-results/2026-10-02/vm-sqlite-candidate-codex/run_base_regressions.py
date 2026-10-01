"""기준 ledger만 memory에서 교체해 회귀의 검출력을 확인한다. 파일은 수정하지 않는다."""
import hashlib
import subprocess
import sys
import unittest

from flowmarshal.engine import ledger

source = subprocess.check_output(['git', 'show', '32bb0f9dd9f024045d24487312b50f5b703573a3:src/flowmarshal/engine/ledger.py'])
assert hashlib.sha256(source).hexdigest() == 'cecb5d6b50e736933005c6c849d6018992434ccdc8a9b13cb012f8e5fc5c0d41'
exec(compile(source, '<base-32bb0f9-ledger>', 'exec'), ledger.__dict__)
tests = [
    'tests.test_engine_ledger_initialize.EngineLedgerInitializeTests.test_schema_metadata_and_identity_commit_together',
    'tests.test_engine_ledger_initialize.EngineLedgerInitializeTests.test_initialization_failures_roll_back_and_retry',
    'tests.test_engine_ledger_initialize.EngineLedgerInitializeTests.test_malformed_file_closes_acquired_connection_and_preserves_bytes',
]
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(tests))
sys.exit(not result.wasSuccessful())
