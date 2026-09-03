from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal_gate0a.probe import (
    APPROVAL_METHODS,
    DESKTOP_INTEROP_HARD_CHECKS,
    PERMISSION_HARD_CHECKS,
    RUNTIME_HARD_CHECKS,
    SCHEMA_VERSION,
    CanaryLayout,
    FailClosedApprovalHandler,
    archive_pre_migration_outputs,
    build_mcp_disable_overrides,
    build_config_overrides,
    canary_layout,
    check,
    derive_gate_decision,
    evaluate_allowed_read_check,
    evaluate_desktop_interop_bundle,
    evaluate_permission_configuration,
    evaluate_read_check,
    evaluate_undeclared_read_policy,
    evaluate_windows_sandbox_readiness,
    migrate_document,
    redact_tokens,
    sanitize_surface_observations,
    toml_quote,
    validate_fail_closed_handler,
)


def passing_runtime(name: str, thread_id: str) -> dict:
    checks = {
        item: check("pass", "ok")
        for item in (*RUNTIME_HARD_CHECKS, *PERMISSION_HARD_CHECKS)
    }
    return {"name": name, "thread_id": thread_id, "checks": checks}


def passing_desktop_interop() -> dict:
    return {
        "status": "pass",
        "checks": {
            item: check("pass", "ok") for item in DESKTOP_INTEROP_HARD_CHECKS
        },
    }


def passing_permission_recheck(run_id: str = "run-1") -> dict:
    return {
        "recheck_id": "recheck-1",
        "status": "pass",
        "checked_at": "2026-09-02T00:00:00Z",
        "schema_version": "1.0",
        "kind": "fm_0a_p_permission_recheck",
        "source_gate_run_id": run_id,
        "runtime_name": "desktop-system",
        "runtime_version": "codex-cli 0.151.0",
        "runtime_executable": "C:/Users/test/.codex/codex.exe",
        "hard_check_statuses": {
            name: "pass" for name in PERMISSION_HARD_CHECKS
        },
        "hard_checks_complete": True,
        "temporary_exception_used": False,
        "secret_contents_read": False,
        "path": "spikes/gate0a/artifacts/permission-rechecks/recheck-1/attempt-result.json",
        "sha256": "a" * 64,
    }


class FailClosedApprovalHandlerTests(unittest.TestCase):
    def test_every_known_method_has_explicit_denial_shape(self) -> None:
        handler = FailClosedApprovalHandler()
        outcome = validate_fail_closed_handler(handler)
        self.assertEqual("pass", outcome["status"])
        self.assertEqual(list(APPROVAL_METHODS), [item["method"] for item in handler.requests])

    def test_unknown_method_never_grants_anything(self) -> None:
        handler = FailClosedApprovalHandler()
        self.assertEqual({}, handler("unknown/request", {"secret": "not-recorded"}))
        self.assertNotIn("secret", handler.requests[0])


class PermissionConfigurationTests(unittest.TestCase):
    def test_profile_only_configuration_passes(self) -> None:
        observation = {
            "ok": True,
            "result": {
                "config": {
                    "default_permissions": "flowmarshal_gate0a",
                    "permissions": {"flowmarshal_gate0a": {"filesystem": {}}},
                },
                "layers": [],
            },
        }
        self.assertEqual("pass", evaluate_permission_configuration(observation)["status"])

    def test_computed_null_legacy_fields_do_not_invalidate_profile_probe(self) -> None:
        observation = {
            "ok": True,
            "result": {
                "config": {
                    "sandbox_mode": None,
                    "sandbox_workspace_write": None,
                    "default_permissions": "flowmarshal_gate0a",
                    "permissions": {"flowmarshal_gate0a": {"filesystem": {}}},
                },
                "layers": [
                    {"name": {"type": "sessionFlags"}, "config": {}},
                    {"name": {"type": "user"}, "config": {}},
                ],
            },
        }
        self.assertEqual("pass", evaluate_permission_configuration(observation)["status"])

    def test_legacy_sandbox_in_any_layer_invalidates_profile_probe(self) -> None:
        observation = {
            "ok": True,
            "result": {
                "config": {
                    "default_permissions": "flowmarshal_gate0a",
                    "permissions": {"flowmarshal_gate0a": {"filesystem": {}}},
                },
                "layers": [{"config": {"sandbox_mode": "danger-full-access"}}],
            },
        }
        outcome = evaluate_permission_configuration(observation)
        self.assertEqual("invalid_configuration", outcome["status"])
        self.assertIn("layers[0].config.sandbox_mode", outcome["evidence"]["legacy_key_paths"])

    def test_generated_profile_distinguishes_resource_roles(self) -> None:
        layout = CanaryLayout(
            workspace=Path("D:/work/app"),
            declared_reference_root=Path("D:/work/contracts"),
            undeclared_root=Path("D:/work/private"),
            protected_root=Path("C:/Users/test/AppData/Local/FlowMarshal/control"),
            control_file=Path("D:/work/app/flowmarshal.toml"),
            permission_home=Path("D:/work/run/permission-home"),
        )
        overrides = "\n".join(build_config_overrides(layout))
        self.assertIn(f'{toml_quote(layout.declared_reference_root)}="read"', overrides)
        self.assertIn('":root"="deny"', overrides)
        self.assertIn(f'{toml_quote(layout.protected_root)}="deny"', overrides)
        self.assertIn(f'{toml_quote(layout.permission_home)}="deny"', overrides)
        self.assertIn('"flowmarshal.toml"="read"', overrides)
        self.assertIn('windows.sandbox="elevated"', overrides)
        self.assertNotIn("D:/work/private", overrides)
        self.assertIn(
            f'permissions.flowmarshal_gate0a.extends=":workspace"',
            overrides,
        )

    def test_windows_sandbox_readiness_requires_explicit_setup(self) -> None:
        outcome = evaluate_windows_sandbox_readiness(
            {"ok": True, "result": {"status": "notConfigured"}},
            setup_command="codex sandbox setup --elevated --user 'test' --codex-home 'C:\\Users\\test\\.codex'",
        )
        self.assertEqual("setup_required", outcome["status"])
        self.assertIn("codex sandbox setup", outcome["evidence"]["setup_command"])

    def test_mcp_servers_are_disabled_by_exact_safe_name(self) -> None:
        outcome, overrides = build_mcp_disable_overrides(
            {
                "ok": True,
                "result": {
                    "config": {
                        "mcp_servers": {
                            "node_repl": {"enabled": True},
                            "cua-repl": {"enabled": False},
                        }
                    }
                },
            }
        )
        self.assertEqual("pass", outcome["status"])
        self.assertEqual(
            (
                "mcp_servers.cua-repl.enabled=false",
                "mcp_servers.node_repl.enabled=false",
            ),
            overrides,
        )

    def test_unsafe_mcp_server_name_fails_closed(self) -> None:
        outcome, overrides = build_mcp_disable_overrides(
            {
                "ok": True,
                "result": {"config": {"mcp_servers": {"bad.name": {"enabled": True}}}},
            }
        )
        self.assertEqual("fail", outcome["status"])
        self.assertEqual((), overrides)


class RedactionTests(unittest.TestCase):
    def test_redacts_nested_canary_values(self) -> None:
        value = {"stdout": "prefix SECRET suffix", "nested": ["SECRET"]}
        result = redact_tokens(value, ["SECRET"])
        self.assertEqual("prefix <CANARY_TOKEN_REDACTED> suffix", result["stdout"])
        self.assertEqual("<CANARY_TOKEN_REDACTED>", result["nested"][0])

    def test_denied_read_breach_is_flagged_without_persisting_token(self) -> None:
        token = "SYNTHETIC-SECRET"
        command = {"rpc_ok": True, "response": {"exitCode": 0, "stdout": token, "stderr": ""}}
        result = evaluate_read_check("undeclared", command, token, True, [token])
        self.assertEqual("fail", result["status"])
        self.assertTrue(result["evidence"]["token_exposed"])
        self.assertNotIn(token, str(result))

    def test_declared_read_must_return_canary(self) -> None:
        token = "SYNTHETIC-REFERENCE"
        command = {"rpc_ok": True, "response": {"exitCode": 0, "stdout": token, "stderr": ""}}
        result = evaluate_allowed_read_check("reference", command, token, True, [token])
        self.assertEqual("pass", result["status"])
        self.assertNotIn(token, str(result))

    def test_native_windows_broad_read_uses_temporary_exception(self) -> None:
        token = "SYNTHETIC-UNDECLARED"
        command = {
            "rpc_ok": True,
            "response": {"exitCode": 0, "stdout": token, "stderr": ""},
        }
        result = evaluate_undeclared_read_policy(
            "undeclared", command, token, True, [token]
        )
        self.assertEqual("pass", result["status"])
        self.assertEqual(
            "temporary_native_windows_broad_read", result["evidence"]["mode"]
        )
        self.assertTrue(result["evidence"]["temporary_exception_used"])
        self.assertFalse(result["evidence"]["strict_boundary_enforced"])
        self.assertNotIn(token, str(result))

    def test_fixed_runtime_uses_strict_read_isolation_without_exception(self) -> None:
        token = "SYNTHETIC-UNDECLARED"
        command = {
            "rpc_ok": True,
            "response": {
                "exitCode": 1,
                "stdout": "",
                "stderr": "PermissionDenied: Access is denied",
            },
        }
        result = evaluate_undeclared_read_policy(
            "undeclared", command, token, True, [token]
        )
        self.assertEqual("pass", result["status"])
        self.assertEqual("strict_read_isolation", result["evidence"]["mode"])
        self.assertFalse(result["evidence"]["temporary_exception_used"])
        self.assertTrue(result["evidence"]["strict_boundary_enforced"])

    def test_ambiguous_undeclared_read_failure_remains_fail_closed(self) -> None:
        token = "SYNTHETIC-UNDECLARED"
        command = {
            "rpc_ok": False,
            "error": {"type": "TransportError", "message": "connection closed"},
        }
        result = evaluate_undeclared_read_policy(
            "undeclared", command, token, True, [token]
        )
        self.assertEqual("fail", result["status"])
        self.assertEqual("ambiguous_failure", result["evidence"]["mode"])

    def test_surface_evidence_drops_mcp_env_and_tool_schema(self) -> None:
        raw = {
            "effective_config": {
                "ok": True,
                "result": {
                    "config": {
                        "web_search": "disabled",
                        "features": {"apps": False},
                        "windows": {"sandbox": "elevated"},
                        "mcp_servers": {"demo": {"env": {"TOKEN": "SECRET"}}},
                    }
                },
            },
            "features": {"ok": True, "result": {"data": []}},
            "mcp": {
                "ok": True,
                "result": {
                    "data": [
                        {
                            "name": "demo",
                            "runtimeStatus": "disabled",
                            "tools": {"tool": {"inputSchema": {"secret": "SECRET"}}},
                        }
                    ]
                },
            },
            "apps": {"ok": True, "result": {"apps": []}},
        }
        safe = sanitize_surface_observations(raw)
        self.assertNotIn("SECRET", str(safe))
        self.assertEqual(1, safe["mcp"]["result"]["data"][0]["toolCount"])
        self.assertIs(safe, sanitize_surface_observations(safe))


class GateDecisionTests(unittest.TestCase):
    def test_schema_migration_archives_exact_1_1_preimage_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifacts = root / "spikes" / "gate0a" / "artifacts"
            artifacts.mkdir(parents=True)
            original = {
                "schema_version": "1.1",
                "run_id": "run-1",
                "gate_decision": {"status": "GO"},
            }
            result_bytes = (json.dumps(original, ensure_ascii=False, indent=2) + "\n").encode()
            report_bytes = b"# original report\n"
            (artifacts / "gate0a-results.json").write_bytes(result_bytes)
            (artifacts / "gate0a-report.md").write_bytes(report_bytes)

            receipt = archive_pre_migration_outputs(root, original)

            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertEqual("1.1", receipt["from_schema_version"])
            self.assertEqual("GO", receipt["previous_gate_decision"]["status"])
            archived_results = root / receipt["sources"]["results"]["archive_path"]
            archived_report = root / receipt["sources"]["report"]["archive_path"]
            self.assertEqual(result_bytes, archived_results.read_bytes())
            self.assertEqual(report_bytes, archived_report.read_bytes())
            self.assertTrue((root / receipt["receipt_path"]).is_file())

    def test_current_schema_does_not_create_migration_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(
                archive_pre_migration_outputs(
                    Path(temp),
                    {"schema_version": SCHEMA_VERSION, "run_id": "current"},
                )
            )

    def test_schema_1_1_migration_requires_desktop_interop_evidence(self) -> None:
        document = {
            "schema_version": "1.1",
            "runtimes": [passing_runtime("runtime", "thread-1")],
            "desktop_visibility": {
                "status": "pass",
                "visible_thread_ids": ["thread-1"],
            },
        }
        migrate_document(document)
        self.assertEqual(SCHEMA_VERSION, document["schema_version"])
        self.assertEqual("pending", document["desktop_interop"]["status"])
        self.assertEqual(
            set(DESKTOP_INTEROP_HARD_CHECKS),
            set(document["desktop_interop"]["checks"]),
        )

    def test_permission_failure_is_no_go(self) -> None:
        runtime = passing_runtime("runtime", "thread-1")
        runtime["checks"]["protected_path_read_blocked"] = check("fail", "breach")
        document = {
            "schema_version": "1.2",
            "runtimes": [runtime],
            "desktop_visibility": {"status": "pass", "visible_thread_ids": ["thread-1"]},
            "desktop_interop": passing_desktop_interop(),
        }
        decision = derive_gate_decision(document)
        self.assertEqual("NO-GO", decision["status"])
        self.assertEqual("GO", decision["subgates"]["runtime"]["status"])
        self.assertEqual("NO-GO", decision["subgates"]["permission"]["status"])

    def test_complete_system_permission_recheck_restores_permission_gate(self) -> None:
        runtime = passing_runtime("sdk-pinned", "thread-1")
        runtime["checks"]["sandbox_state_unchanged"] = check("not_run", "old run")
        runtime["checks"]["artifact_safety"] = check("not_run", "old run")
        document = {
            "schema_version": "1.3",
            "run_id": "run-1",
            "runtimes": [runtime],
            "permission_rechecks": [passing_permission_recheck()],
            "desktop_visibility": {
                "status": "pass",
                "visible_thread_ids": ["thread-1"],
            },
            "desktop_interop": passing_desktop_interop(),
        }
        decision = derive_gate_decision(document)
        permission = decision["subgates"]["permission"]
        self.assertEqual("GO", decision["status"])
        self.assertEqual("GO", permission["status"])
        self.assertEqual(["recheck-1"], permission["candidate_permission_rechecks"])
        self.assertEqual([], permission["blocking_failures"])

    def test_incomplete_permission_recheck_cannot_restore_permission_gate(self) -> None:
        runtime = passing_runtime("sdk-pinned", "thread-1")
        runtime["checks"]["sandbox_state_unchanged"] = check("not_run", "old run")
        runtime["checks"]["artifact_safety"] = check("not_run", "old run")
        recheck = passing_permission_recheck()
        recheck["hard_check_statuses"]["artifact_safety"] = None
        recheck["hard_checks_complete"] = False
        document = {
            "schema_version": "1.3",
            "run_id": "run-1",
            "runtimes": [runtime],
            "permission_rechecks": [recheck],
            "desktop_visibility": {
                "status": "pass",
                "visible_thread_ids": ["thread-1"],
            },
            "desktop_interop": passing_desktop_interop(),
        }
        decision = derive_gate_decision(document)
        self.assertEqual("NO-GO", decision["status"])
        self.assertEqual("NO-GO", decision["subgates"]["permission"]["status"])

    def test_permission_recheck_from_another_gate_run_is_rejected(self) -> None:
        runtime = passing_runtime("sdk-pinned", "thread-1")
        runtime["checks"]["sandbox_state_unchanged"] = check("not_run", "old run")
        runtime["checks"]["artifact_safety"] = check("not_run", "old run")
        document = {
            "schema_version": "1.3",
            "run_id": "run-2",
            "runtimes": [runtime],
            "permission_rechecks": [passing_permission_recheck("run-1")],
            "desktop_visibility": {
                "status": "pass",
                "visible_thread_ids": ["thread-1"],
            },
            "desktop_interop": passing_desktop_interop(),
        }
        decision = derive_gate_decision(document)
        self.assertEqual("NO-GO", decision["status"])
        self.assertEqual(
            [],
            decision["subgates"]["permission"]["candidate_permission_rechecks"],
        )
        document["run_id"] = None
        document["permission_rechecks"][0]["source_gate_run_id"] = None
        decision = derive_gate_decision(document)
        self.assertEqual("NO-GO", decision["status"])

    def test_mixed_configuration_requires_retest_without_runtime_side_effects(self) -> None:
        runtime = passing_runtime("runtime", "")
        runtime["checks"]["permission_configuration"] = check("invalid_configuration", "mixed")
        for name in RUNTIME_HARD_CHECKS:
            if name != "initialize":
                runtime["checks"][name] = check("not_run", "preflight")
        document = {
            "schema_version": "1.2",
            "runtimes": [runtime],
            "desktop_visibility": {"status": "pending", "visible_thread_ids": []},
        }
        decision = derive_gate_decision(document)
        self.assertEqual("RETEST_REQUIRED", decision["status"])
        self.assertEqual("NOT_RUN", decision["subgates"]["runtime"]["status"])
        self.assertEqual("INVALID_CONFIGURATION", decision["subgates"]["permission"]["status"])

    def test_candidate_waits_for_desktop(self) -> None:
        document = {
            "schema_version": "1.2",
            "runtimes": [passing_runtime("runtime", "thread-1")],
            "desktop_visibility": {"status": "pending", "visible_thread_ids": []},
        }
        self.assertEqual("PENDING-DESKTOP-CHECK", derive_gate_decision(document)["status"])

    def test_permission_sandbox_setup_is_distinct_from_policy_failure(self) -> None:
        runtime = passing_runtime("runtime", "thread-1")
        runtime["checks"]["windows_sandbox_operational"] = check(
            "setup_required", "setup"
        )
        document = {
            "schema_version": "1.2",
            "runtimes": [runtime],
            "desktop_visibility": {"status": "pending", "visible_thread_ids": []},
        }
        self.assertEqual("SETUP_REQUIRED", derive_gate_decision(document)["status"])

    def test_visible_candidate_requires_bidirectional_interop(self) -> None:
        document = {
            "schema_version": "1.2",
            "runtimes": [passing_runtime("runtime", "thread-1")],
            "desktop_visibility": {
                "status": "pass",
                "visible_thread_ids": ["thread-1"],
                "missing_thread_ids": [],
            },
        }
        decision = derive_gate_decision(document)
        self.assertEqual("PENDING-INTEROP-CHECK", decision["status"])
        self.assertEqual(
            "PENDING-INTEROP-CHECK", decision["subgates"]["runtime"]["status"]
        )

    def test_bidirectional_interop_evidence_is_required_for_go(self) -> None:
        document = {
            "schema_version": "1.2",
            "runtimes": [passing_runtime("runtime", "thread-1")],
            "desktop_visibility": {
                "status": "pass",
                "visible_thread_ids": ["thread-1"],
                "missing_thread_ids": [],
            },
            "desktop_interop": passing_desktop_interop(),
        }
        self.assertEqual("GO", derive_gate_decision(document)["status"])

    def test_bidirectional_interop_failure_is_no_go(self) -> None:
        interop = passing_desktop_interop()
        interop["checks"]["concurrent_access_consistent"] = check("fail", "mismatch")
        document = {
            "schema_version": "1.2",
            "runtimes": [passing_runtime("runtime", "thread-1")],
            "desktop_visibility": {
                "status": "pass",
                "visible_thread_ids": ["thread-1"],
                "missing_thread_ids": [],
            },
            "desktop_interop": interop,
        }
        decision = derive_gate_decision(document)
        self.assertEqual("NO-GO", decision["status"])
        self.assertEqual("NO-GO", decision["subgates"]["runtime"]["status"])

    def test_schema_1_0_blanket_read_result_is_retest_not_no_go(self) -> None:
        runtime = {
            "name": "legacy",
            "thread_id": "thread-1",
            "checks": {
                **{name: check("pass", "ok") for name in RUNTIME_HARD_CHECKS},
                "restricted_project_outside_read": check("fail", "outside readable"),
                "restricted_localappdata_read": check("fail", "local readable"),
            },
        }
        document = {
            "schema_version": "1.0",
            "runtimes": [runtime],
            "desktop_visibility": {"status": "pass", "visible_thread_ids": ["thread-1"]},
        }
        decision = derive_gate_decision(document)
        self.assertEqual("RETEST_REQUIRED", decision["status"])
        self.assertEqual(
            "PENDING-INTEROP-CHECK", decision["subgates"]["runtime"]["status"]
        )
        self.assertEqual("INVALID_CONFIGURATION", decision["subgates"]["permission"]["status"])


class DesktopInteropEvaluatorTests(unittest.TestCase):
    def test_matching_two_sided_bundle_passes_every_required_check(self) -> None:
        challenge = {
            "challenge_nonce": "nonce-1",
            "thread_id": "thread-1",
            "expected_project_id": "project-1",
            "expected_cwd": "D:/codex/project",
            "sdk_turn_id": "turn-sdk",
            "ready_at": "2026-09-01T01:00:00Z",
            "expires_at": "2026-09-01T01:03:00Z",
        }
        observation = {
            "challenge_nonce": "nonce-1",
            "thread_id": "thread-1",
            "observed_at": "2026-09-01T01:00:30Z",
            "thread_found": True,
            "project_id": "project-1",
            "observed_cwd": "d:\\codex\\project\\",
            "sdk_marker_visible": True,
            "sdk_turn_id": "turn-sdk",
            "status_during_sdk_wait": "idle",
        }
        sdk_state = {
            "read_thread_id": "thread-1",
            "resumed_thread_id": "thread-1",
            "final_thread_id": "thread-1",
            "desktop_marker_visible_before": True,
            "desktop_marker_visible_final": True,
            "sdk_marker_visible_after_turn": True,
            "sdk_turn_completed": True,
        }

        checks = evaluate_desktop_interop_bundle(challenge, observation, sdk_state)

        self.assertEqual(
            {"pass"},
            {item["status"] for item in checks.values()},
        )

    def test_nonce_time_path_and_turn_mismatches_fail_closed(self) -> None:
        challenge = {
            "challenge_nonce": "nonce-expected",
            "thread_id": "thread-1",
            "expected_project_id": "project-1",
            "expected_cwd": "D:/codex/project",
            "sdk_turn_id": "turn-sdk",
            "ready_at": "2026-09-01T01:00:00Z",
            "expires_at": "2026-09-01T01:03:00Z",
        }
        observation = {
            "challenge_nonce": "nonce-stale",
            "thread_id": "thread-1",
            "observed_at": "2026-09-01T01:04:00Z",
            "thread_found": True,
            "project_id": "project-1",
            "observed_cwd": "D:/codex/other",
            "sdk_marker_visible": True,
            "sdk_turn_id": "turn-other",
            "status_during_sdk_wait": "idle",
        }
        sdk_state = {
            "read_thread_id": "thread-1",
            "resumed_thread_id": "thread-1",
            "final_thread_id": "thread-1",
            "desktop_marker_visible_before": True,
            "desktop_marker_visible_final": True,
            "sdk_marker_visible_after_turn": True,
            "sdk_turn_completed": False,
        }

        checks = evaluate_desktop_interop_bundle(challenge, observation, sdk_state)

        self.assertEqual("fail", checks["project_grouping_correct"]["status"])
        self.assertEqual("fail", checks["sdk_resume_visible_in_desktop"]["status"])
        self.assertEqual("fail", checks["concurrent_access_consistent"]["status"])


class PathSmokeTests(unittest.TestCase):
    def test_reference_and_undeclared_roots_are_distinct_from_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project" / "workspace"
            reference = root / "reference"
            undeclared = root / "undeclared"
            for path in (workspace, reference, undeclared):
                path.mkdir(parents=True)
            self.assertFalse(reference.is_relative_to(workspace))
            self.assertFalse(undeclared.is_relative_to(workspace))
            self.assertNotEqual(reference.resolve(), undeclared.resolve())

    def test_undeclared_canary_is_outside_project_collection_tree(self) -> None:
        layout = canary_layout(
            Path("D:/codex/flowmarshal"),
            "run-1",
            "sdk-pinned",
        )
        self.assertEqual(
            Path("D:/FlowMarshal-Gate0A-Canary/run-1/sdk-pinned/undeclared"),
            layout.undeclared_root,
        )
        self.assertFalse(layout.undeclared_root.is_relative_to(Path("D:/codex")))


if __name__ == "__main__":
    unittest.main()
