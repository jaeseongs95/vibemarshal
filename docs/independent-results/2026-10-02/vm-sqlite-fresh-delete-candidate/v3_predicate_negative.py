"""파일을 수정하지 않고 frozen v3 predicate에 새 거절 회귀를 적용한다."""
import hashlib
import subprocess
import sys
import unittest
from flowmarshal.engine import ledger

source = subprocess.check_output(['git', 'show', '211a5a0f5891a44f217aa7aae18bdb8825775878:src/flowmarshal/engine/ledger.py'])
assert hashlib.sha256(source).hexdigest() == '7c1b089720f5b270bd192902aea7db7f793f7903859f11a20e5c55cccb216fc2'
exec(compile(source, '<frozen-v3-ledger>', 'exec'), ledger.__dict__)
suite = unittest.defaultTestLoader.loadTestsFromName(
    'tests.test_engine_ledger_initialize.EngineLedgerInitializeTests.test_nonzero_logical_empty_containers_are_preserved')
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
