from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.vm_sandbox_suite import (
    VM_SUITE_SEQUENCE_INVALID,
    VM_TEST_INJECTED_SETUP_FAILURE,
    VmSuiteError,
    evaluate_vm_suite,
    fingerprint_is_clean,
    scenario_root,
    suite_root,
    validate_attempt_request,
    write_json_object,
)


def clean_fingerprint() -> dict[str, object]:
    return {
        "accounts": [
            {"Name": "CodexSandboxOffline", "Missing": True},
            {"Name": "CodexSandboxOnline", "Missing": True},
        ],
        "accounts_error": None,
        "setup_marker": {"exists": False, "error": None},
        "secrets_directory": {
            "exists": False,
            "mtime_ns": None,
            "file_count": 0,
            "error": None,
        },
    }


def runtime(name: str, version: str) -> dict[str, object]:
    return {
        "name": name,
        "executable": f"C:/Codex/{name}/codex.exe",
        "version": {"exit_code": 0, "stdout": version, "stderr": ""},
    }


class VmSuiteContractTests(unittest.TestCase):
    def test_clean_fingerprint_requires_both_missing_accounts(self) -> None:
        self.assertTrue(fingerprint_is_clean(clean_fingerprint()))
        dirty = clean_fingerprint()
        dirty["accounts"] = [
            {"Name": "CodexSandboxOffline", "Missing": False},
            {"Name": "CodexSandboxOnline", "Missing": True},
        ]
        self.assertFalse(fingerprint_is_clean(dirty))

    def test_failure_retry_allows_exactly_one_injected_failure_and_retry(self) -> None:
        first_sequence = validate_attempt_request(
            "failure-retry",
            [],
            runtime_name="desktop-system",
            inject_failure=True,
        )
        self.assertEqual(1, first_sequence)
        attempts = [
            {
                "status": "failed",
                "runtime": {"name": "desktop-system"},
                "error": {"reason_code": VM_TEST_INJECTED_SETUP_FAILURE},
            }
        ]
        self.assertEqual(
            2,
            validate_attempt_request(
                "failure-retry",
                attempts,
                runtime_name="desktop-system",
                inject_failure=False,
            ),
        )
        attempts.append(
            {"status": "completed", "runtime": {"name": "desktop-system"}}
        )
        with self.assertRaises(VmSuiteError) as raised:
            validate_attempt_request(
                "failure-retry",
                attempts,
                runtime_name="desktop-system",
                inject_failure=False,
            )
        self.assertEqual(VM_SUITE_SEQUENCE_INVALID, raised.exception.reason_code)

    def test_cross_version_rejects_system_runtime_before_pinned_success(self) -> None:
        with self.assertRaises(VmSuiteError) as raised:
            validate_attempt_request(
                "cross-version",
                [],
                runtime_name="desktop-system",
                inject_failure=False,
            )
        self.assertEqual(VM_SUITE_SEQUENCE_INVALID, raised.exception.reason_code)

        pinned_success = [
            {"status": "completed", "runtime": {"name": "sdk-pinned"}}
        ]
        self.assertEqual(
            2,
            validate_attempt_request(
                "cross-version",
                pinned_success,
                runtime_name="desktop-system",
                inject_failure=False,
            ),
        )


class VmSuiteEvaluationTests(unittest.TestCase):
    def _write_complete_suite(self, artifacts: Path, *, disposal: bool) -> None:
        suite_id = "suite-1"
        for scenario in ("current-first", "cross-version", "failure-retry"):
            root = scenario_root(artifacts, suite_id, scenario)
            write_json_object(
                root / "scenario.json",
                {
                    "schema_version": "1.0",
                    "kind": "disposable_windows_vm_scenario_contract",
                    "suite_id": suite_id,
                    "scenario": scenario,
                    "authoritative_codex_home": f"C:/vm/{scenario}",
                    "vm_identity": {"recognized_virtual_machine": True},
                    "initial_fingerprint": clean_fingerprint(),
                },
            )

        current = scenario_root(artifacts, suite_id, "current-first")
        write_json_object(
            current / "attempts" / "current" / "setup-result.json",
            {
                "sequence": 1,
                "attempt_id": "current",
                "recorded_at": "2026-09-02T00:01:00Z",
                "status": "completed",
                "runtime": runtime("desktop-system", "codex-cli 0.151.0"),
                "setup_api_called": True,
                "readiness_after": {"result": {"status": "ready"}},
            },
        )

        cross = scenario_root(artifacts, suite_id, "cross-version")
        write_json_object(
            cross / "attempts" / "old" / "setup-result.json",
            {
                "sequence": 1,
                "attempt_id": "old",
                "recorded_at": "2026-09-02T00:02:00Z",
                "status": "completed",
                "runtime": runtime("sdk-pinned", "codex-cli 0.147.0"),
                "setup_api_called": True,
                "readiness_after": {"result": {"status": "ready"}},
            },
        )
        write_json_object(
            cross / "attempts" / "new" / "setup-result.json",
            {
                "sequence": 2,
                "attempt_id": "new",
                "recorded_at": "2026-09-02T00:03:00Z",
                "status": "already_ready",
                "runtime": runtime("desktop-system", "codex-cli 0.151.0"),
                "setup_api_called": False,
                "readiness_after": {"result": {"status": "ready"}},
            },
        )
        write_json_object(
            cross / "guards" / "second-home.json",
            {
                "reason_code": "HOST_PROVISIONING_FORBIDDEN",
                "claimed_home": "C:/vm/cross-version",
                "requested_home": "C:/vm/other-home",
                "runtime_started": False,
                "setup_api_called": False,
            },
        )

        retry = scenario_root(artifacts, suite_id, "failure-retry")
        write_json_object(
            retry / "attempts" / "failure" / "setup-result.json",
            {
                "sequence": 1,
                "attempt_id": "failure",
                "recorded_at": "2026-09-02T00:04:00Z",
                "status": "failed",
                "runtime": runtime("desktop-system", "codex-cli 0.151.0"),
                "setup_api_called": False,
                "error": {"reason_code": VM_TEST_INJECTED_SETUP_FAILURE},
            },
        )
        write_json_object(
            retry / "attempts" / "retry" / "setup-result.json",
            {
                "sequence": 2,
                "attempt_id": "retry",
                "recorded_at": "2026-09-02T00:05:00Z",
                "status": "completed",
                "runtime": runtime("desktop-system", "codex-cli 0.151.0"),
                "setup_api_called": True,
                "readiness_after": {"result": {"status": "ready"}},
            },
        )
        if disposal:
            write_json_object(
                suite_root(artifacts, suite_id) / "lifecycle" / "disposal.json",
                {
                    "recorded_at": "2026-09-02T00:06:00Z",
                    "action": "disposed",
                    "provider": "Hyper-V",
                    "vm_identifier": "flowmarshal-test",
                    "evidence_reference": "hyper-v/remove-vm/event-1",
                },
            )

    def test_complete_suite_is_go(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            self._write_complete_suite(artifacts, disposal=True)
            result = evaluate_vm_suite(artifacts, "suite-1")
        self.assertEqual("GO", result["status"])
        self.assertTrue(result["gate_restoration_allowed"])
        self.assertTrue(result["source_manifest"])

    def test_missing_disposal_keeps_suite_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            self._write_complete_suite(artifacts, disposal=False)
            result = evaluate_vm_suite(artifacts, "suite-1")
        self.assertEqual("PENDING", result["status"])
        self.assertFalse(result["gate_restoration_allowed"])

    def test_forbidden_codex_state_in_suite_is_no_go(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            self._write_complete_suite(artifacts, disposal=True)
            forbidden = suite_root(artifacts, "suite-1") / "auth.json"
            forbidden.write_text("{}", encoding="utf-8")
            result = evaluate_vm_suite(artifacts, "suite-1")
        self.assertEqual("NO-GO", result["status"])
        artifact_check = next(
            item for item in result["checks"] if item["id"] == "artifact_safety"
        )
        self.assertEqual("fail", artifact_check["status"])


if __name__ == "__main__":
    unittest.main()
