from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.planning.r31_smoke import run_smoke


class PlannerR31SmokeTests(unittest.TestCase):
    def test_smoke_is_idempotent_and_exports_without_core_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = run_smoke(directory)
            second = run_smoke(directory)
            self.assertEqual("PASS", first["status"])
            self.assertEqual("NOT_RUN", first["live_model_qualification"])
            self.assertFalse(first["core_activated"])
            self.assertEqual(first["planning_run_id"], second["planning_run_id"])
            self.assertEqual(first["selection_digest"], second["selection_digest"])
            self.assertTrue(Path(first["export_path"]).is_file())
            self.assertTrue(Path(first["receipt_path"]).is_file())
            self.assertTrue(Path(first["report_path"]).is_file())
            self.assertEqual(first["receipt_path"], second["receipt_path"])
            self.assertEqual(first["report_path"], second["report_path"])


if __name__ == "__main__":
    unittest.main()
