from pathlib import Path
import tempfile
import unittest

from scripts.diagnostics.r_s06_09 import CALL_ORDER, MAXIMUM_CALLS, claim_turn


class InspectionDiagnosticTests(unittest.TestCase):
    def test_physical_call_budget_and_duplicate_attempt_are_consumed_before_effect(self):
        self.assertEqual(13, MAXIMUM_CALLS)
        self.assertEqual(13, len(CALL_ORDER))
        self.assertEqual(13, len(set(CALL_ORDER)))
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            capture = run / "calls/01-review"
            claim_turn(run, capture, {"attempt": "실패해도 소비한 호출"})
            with self.assertRaises(FileExistsError):
                claim_turn(run, capture, {"attempt": "자동 재시도"})
            for number in range(2, 14):
                claim_turn(run, run / "calls" / str(number), {"attempt": number})
            with self.assertRaisesRegex(RuntimeError, "MAXIMUM_PROVIDER_CALLS"):
                claim_turn(run, run / "calls/14", {})
            self.assertEqual(13, len(list((run / "calls").glob("*/turn.intent.json"))))


if __name__ == "__main__":
    unittest.main()
