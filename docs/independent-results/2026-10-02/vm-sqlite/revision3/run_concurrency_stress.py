"""기존 신규 두 경계 검사를 10회 반복한다. 신규 수량은 2개 그대로다."""
import sys,unittest
from test_sqlite_boundaries import SQLiteBoundaries
suite=unittest.TestSuite(SQLiteBoundaries(t) for i in range(10) for t in ['test_10_multi_process_history_has_no_gaps_or_forks','test_12_exact_cas_method_eight_processes_one_winner'])
r=unittest.TextTestRunner(verbosity=2).run(suite)
print('REPETITIONS=10 UNIQUE_TESTS=2 TOTAL_EXECUTIONS=20')
sys.exit(not r.wasSuccessful())
