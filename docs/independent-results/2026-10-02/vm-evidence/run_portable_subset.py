"""D1 Windows-only 실행 계약과 분리한 Linux 검사 선택. skip은 성공이 아니다."""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(os.environ['VM_SOURCE_ROOT']).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
MODULES = [
    'tests.test_engine_cli',
    'tests.test_engine_cli_revise',
    'tests.test_engine_application',
    'tests.test_engine_application_revise',
    'tests.test_engine_user_facade',
    'tests.test_engine_task_validation_recovery',
    'tests.test_engine_clean_install_qualification',
    'tests.test_engine_packaging',
    'tests.test_engine_inspection_portable_preflight',
    'tests.test_engine_governance_conformance.ConformanceModuleTests',
    'tests.test_engine_governance_conformance.CallSiteTests',
    'tests.test_engine_governance_gate.SnapshotTests',
    'tests.test_engine_governance_gate.PluginSurfaceTests',
    'tests.test_engine_governance_gate.McpClientCleanupTests',
    'tests.test_engine_governance_gate.TimeoutTests',
    'tests.test_engine_g1b_owner_lease.ReplanningOwnerProcessTests.test_l7_posix_fails_closed_before_any_ledger_write_but_other_facades_work',
    'tests.test_engine_g1b_owner_lease.OwnerStatusReplanTests.test_s4_posix_status_is_unchanged_before_activation_and_blocks_after_it',
]
EXCLUDED = {
    'tests.test_engine_application_revise.EngineApplicationReviseTests.test_revision_after_satisfied_goal_plans_without_activation': 'D1: completed Goal fixture requires Windows runtime owner',
    'tests.test_engine_application_revise.EngineApplicationReviseTests.test_widened_effect_revision_needs_new_target_authorization': 'D1: completed Goal fixture requires Windows runtime owner',
    'tests.test_engine_user_facade.EngineUserFacadeTests.test_automatic_recovery_revalidation_and_goal_verdict_finish_through_facade': 'D1: Windows runtime owner required',
    'tests.test_engine_task_validation_recovery.TaskValidationRecoveryTests.test_run_once_reports_typed_validation_recovery_blocker': 'D1: Windows runtime owner required',
}

def leaves(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from leaves(item)
        else:
            yield item

selected = [case for case in leaves(unittest.defaultTestLoader.loadTestsFromNames(MODULES)) if case.id() not in EXCLUDED]
inventory = [case.id() for case in selected]
class Result(unittest.TextTestResult):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.successes = []
    def addSuccess(self, test):
        self.successes.append(test.id())
        super().addSuccess(test)

result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(unittest.TestSuite(selected))
doc = {'selected': inventory, 'excluded': EXCLUDED, 'testsRun': result.testsRun,
       'successes': result.successes, 'failures': [(t.id(), s) for t, s in result.failures],
       'errors': [(t.id(), s) for t, s in result.errors], 'skipped': [(t.id(), s) for t, s in result.skipped],
       'zero_skip_pass': result.wasSuccessful() and not result.skipped,
       'limits': ['Windows native owner/restart tests not qualified', 'Real-provider and real-plugin workflows not selected', 'Historical fixture packets excluded from source archive']}
(Path(os.environ['VM_EVIDENCE_ROOT']) / 'portable-subset-result.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2))
sys.exit(0 if doc['zero_skip_pass'] else 1)
