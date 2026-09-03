from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.context import RuntimeRole
from flowmarshal.gate0c.e2e import (
    Gate0CE2EError,
    _command_probe,
    _known_synthetic_trust_entries,
    _load_corpus,
    append_retest_correction,
    fixed_e2e_layout,
    record_retest_ledger,
    stage_attack_corpus,
)
from flowmarshal.gate0c.profile_probe import (
    FailClosedApprovalHandler,
    build_retest_permission_profiles,
)


PROJECT_ROOT = Path(__file__).parents[1]


class _FakeCommandClient:
    def __init__(self, response):
        self.response = response

    def _request_raw(self, method, params):
        self.method = method
        self.params = params
        return self.response


class Gate0CE2ETests(unittest.TestCase):
    def test_attack_files_are_staged_by_distinct_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw) / "run"
            workspace = run / "workspace"
            workspace.mkdir(parents=True)
            corpus = _load_corpus(PROJECT_ROOT, run)
            roots = stage_attack_corpus(corpus, workspace)
            files = [item for root in roots.values() for item in root.iterdir()]
            self.assertEqual(30, len(files))
            self.assertEqual(30, len({item.resolve() for item in files}))
            # 같은 승인 corpus를 다시 staging해도 내용이 같을 때만 idempotent다.
            self.assertEqual(roots, stage_attack_corpus(corpus, workspace))

    def test_command_probe_requires_exact_deny_receipt(self) -> None:
        handler = FailClosedApprovalHandler()
        denied = _command_probe(
            _FakeCommandClient({"exitCode": 77, "stdout": "READ_BLOCKED", "stderr": ""}),
            handler,
            probe_id="deny",
            role=RuntimeRole.RUNNER,
            profile_id="profile",
            cwd=Path("D:/synthetic"),
            script="fixed",
            expectation="deny_read",
        )
        self.assertTrue(denied.passed)
        ambiguous = _command_probe(
            _FakeCommandClient({"exitCode": 1, "stdout": "", "stderr": "error"}),
            handler,
            probe_id="ambiguous",
            role=RuntimeRole.RUNNER,
            profile_id="profile",
            cwd=Path("D:/synthetic"),
            script="fixed",
            expectation="deny_read",
        )
        self.assertFalse(ambiguous.passed)

    def test_unregistered_canary_is_not_an_explicit_profile_rule(self) -> None:
        run_root, layout, _ = fixed_e2e_layout(PROJECT_ROOT)
        profiles = build_retest_permission_profiles(layout)
        unregistered = str((run_root / "unregistered").resolve())
        self.assertNotIn(unregistered, layout.protected_roots)
        for definition in profiles.definitions:
            self.assertNotIn(
                unregistered,
                {rule.selector for rule in definition.filesystem_rules},
            )

    def test_only_known_synthetic_trust_entries_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "config.toml"
            config.write_text(
                "[projects.'D:\\\\unrelated']\ntrust_level='trusted'\n"
                "[projects.'D:\\\\x\\\\profile-20260902-r4\\\\workspace']\n"
                "trust_level='trusted'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                ("profile-20260902-r4/workspace",),
                _known_synthetic_trust_entries(config),
            )

    def test_retest_ledger_is_new_append_only_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "plan_revision_id": "flowmarshal-gate0c-r2-test",
                        "status": "approved",
                        "approved_at": "2026-09-02T00:00:00Z",
                        "document": {"sha256": "a" * 64},
                        "approval_source": {
                            "kind": "user",
                            "text": "다시 테스트해",
                        },
                    }
                ),
                encoding="utf-8",
            )
            evidence_paths = []
            for label in ("r2", "r2b", "boundary"):
                path = root / f"{label}.json"
                path.write_text(
                    json.dumps(
                        {
                            "status": "NO-GO",
                            "profile_set_digest": f"sha256:{label}",
                            "reason_codes": ["TEST_FAILURE"],
                            "failed_probe_ids": ["probe"] if label == "boundary" else [],
                            "host_invariants": {"unchanged": True},
                        }
                    ),
                    encoding="utf-8",
                )
                evidence_paths.append((label, path))
            database = root / "control" / "gate0c-r2.sqlite3"
            result = record_retest_ledger(
                project_root=root,
                database_path=database,
                plan_artifact_path=plan,
                evidence_paths=tuple(evidence_paths),
            )
            self.assertEqual(3, result["attempt_count"])
            self.assertEqual("failed", result["fm0c1_state"])
            correction = root / "r2c.json"
            correction.write_text(
                json.dumps(
                    {
                        "status": "NO-GO",
                        "profile_set_digest": "sha256:r2c",
                        "reason_codes": ["PERMISSION_BOUNDARY_FAILED"],
                        "failed_probe_ids": ["runner_network_loopback"],
                        "host_invariants": {"unchanged": True},
                        "probes": [
                            {
                                "probe_id": "runner_read_unregistered",
                                "passed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            appended = append_retest_correction(
                project_root=root,
                database_path=database,
                label="r2c",
                evidence_path=correction,
                correction="미등록 canary를 실제 기본 deny 대상으로 교정",
                evidence_kind="permission_boundary_matrix",
            )
            self.assertEqual("attempt_fm0c1_r2c_20260902", appended["attempt_id"])
            with self.assertRaises(Gate0CE2EError) as caught:
                record_retest_ledger(
                    project_root=root,
                    database_path=database,
                    plan_artifact_path=plan,
                    evidence_paths=tuple(evidence_paths),
                )
            self.assertEqual("RETEST_LEDGER_ALREADY_EXISTS", caught.exception.reason_code)


if __name__ == "__main__":
    unittest.main()
