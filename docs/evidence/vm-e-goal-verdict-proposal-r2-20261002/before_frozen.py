import importlib.util
import json
import unittest
from unittest.mock import patch

import flowmarshal.engine.service as service

print(json.dumps({'service_import': service.__file__,
                  'revision_under_test': '98bb122970f82bb5fcef122003ee10c7eb787cf4',
                  'regression_file': '/workspace/vm-e-r2/tests/test_engine_goal_verdict_authority.py'}))
spec = importlib.util.spec_from_file_location('vm_e_r2_regression',
    '/workspace/vm-e-r2/tests/test_engine_goal_verdict_authority.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                           for cls in (module.GoalVerdictAuthorityTests,
                                       module.GoalTestBindingCompatibilityTests))
with patch('flowmarshal.engine.validation_execution.subprocess.run',
           side_effect=AssertionError('native command outside explicit synthetic stub')):
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
