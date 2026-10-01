import importlib.util
import json
import subprocess
import unittest
from unittest.mock import patch

import flowmarshal.engine.service as service
print(json.dumps({'tested_source': service.__file__}))
spec = importlib.util.spec_from_file_location('vm_e_verdict_regression',
    '/workspace/vm-e-proposal/tests/test_engine_goal_verdict_authority.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.GoalVerdictAuthorityTests)
with patch('flowmarshal.engine.validation_execution.subprocess.run',
           side_effect=AssertionError('native effects prohibited')):
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
