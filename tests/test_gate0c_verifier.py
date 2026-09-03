from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowmarshal.gate0c.verifier import Gate0CVerifier


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = PROJECT_ROOT / "spikes" / "gate0c" / "artifacts" / "control" / "gate0c.sqlite3"


class Gate0CVerifierTests(unittest.TestCase):
    def test_current_blocker_is_independently_reported_as_no_go(self) -> None:
        report = Gate0CVerifier(project_root=PROJECT_ROOT).verify()
        checks = {item.check_id: item for item in report.checks}
        self.assertEqual("NO-GO", report.overall)
        self.assertTrue(checks["sqlite_integrity"].passed)
        self.assertTrue(checks["evidence_integrity"].passed)
        self.assertTrue(checks["history_chain"].passed)
        self.assertFalse(checks["profile_provenance"].passed)
        self.assertFalse(checks["historical_host_invariant"].passed)
        self.assertFalse(checks["task_completion"].passed)
        self.assertIn("GLOBAL_INSTRUCTION_UNREADABLE", report.reason_codes)

    def test_evidence_tamper_is_detected_in_database_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / "gate0c-copy.sqlite3"
            source = sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)
            target = sqlite3.connect(copied)
            source.backup(target)
            source.close()
            target.execute("DROP TRIGGER evidence_records_no_update")
            target.execute(
                "UPDATE evidence_records SET payload_digest=? WHERE evidence_id=?",
                ("sha256:" + "0" * 64, "evidence_fm0c1_profile_r7_strict_failure"),
            )
            target.commit()
            target.close()

            report = Gate0CVerifier(
                project_root=PROJECT_ROOT,
                database_path=copied,
            ).verify()
            checks = {item.check_id: item for item in report.checks}
            self.assertEqual("NO-GO", report.overall)
            self.assertFalse(checks["append_only_guards"].passed)
            self.assertFalse(checks["evidence_integrity"].passed)


if __name__ == "__main__":
    unittest.main()
