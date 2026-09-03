from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.context import RuntimeRole
from flowmarshal.gate0c.profile_probe import (
    PROFILE_IDS,
    PLANNER_PROFILE_ID,
    ProfileProbeError,
    RolePathLayout,
    build_config_overrides,
    build_permission_profiles,
    build_retest_permission_profiles,
    preflight_model,
    preflight_profiles,
    sensitive_codex_home_selectors,
    start_profiled_thread,
    surface_policy,
    thread_start_params,
)


class FakeRawClient:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def _request_raw(self, method, params=None):
        self.requests.append((method, params))
        value = self.responses[method]
        return value(params) if callable(value) else value


def profile_set(tmp: Path):
    planner = tmp / "planner"
    runner = tmp / "runner"
    reference = tmp / "reference"
    protected = tmp / "control"
    home = tmp / "codex-home"
    for path in (planner, runner, reference, protected, home):
        path.mkdir(exist_ok=True)
    approved_reference = reference / "approved.txt"
    approved_reference.write_text("approved\n", encoding="utf-8")
    (home / "AGENTS.md").write_text("# test policy\n", encoding="utf-8")
    (home / "auth.json").write_text("{}\n", encoding="utf-8")
    (home / "sessions").mkdir(exist_ok=True)
    layout = RolePathLayout.from_paths(
        planner_context_root=planner,
        runner_workspace_root=runner,
        runner_write_root=runner,
        reference_roots=(reference,),
        reference_files=(approved_reference,),
        protected_roots=(protected,),
        authoritative_codex_home=home,
    )
    return build_permission_profiles(layout), layout


class Gate0CProfileTests(unittest.TestCase):
    def test_profile_definitions_and_digest_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            left, layout = profile_set(Path(raw))
            right, _ = profile_set(Path(raw))
            self.assertEqual(PROFILE_IDS, tuple(item.profile_id for item in left.definitions))
            self.assertEqual(left.digest, right.digest)
            self.assertEqual("write", left.for_role(RuntimeRole.RUNNER).workspace_access)
            self.assertEqual(":read-only", left.for_role(RuntimeRole.RUNNER).extends)
            self.assertEqual("read", left.for_role(RuntimeRole.VALIDATOR).workspace_access)
            self.assertIn(
                layout.runner_write_root,
                {
                    item.selector
                    for item in left.for_role(RuntimeRole.RUNNER).filesystem_rules
                    if item.access == "write"
                },
            )
            for definition in left.definitions:
                rules = {
                    item.selector: item.access
                    for item in definition.filesystem_rules
                }
                self.assertEqual("deny", rules[layout.authoritative_codex_home])
                self.assertEqual(
                    "read",
                    rules[str((Path(layout.authoritative_codex_home) / "AGENTS.md").resolve())],
                )

    def test_retest_profile_allows_policy_input_and_denies_sensitive_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            original, layout = profile_set(Path(raw))
            retest = build_retest_permission_profiles(layout)
            home = Path(layout.authoritative_codex_home)

            self.assertNotEqual(original.digest, retest.digest)
            self.assertEqual(":read-only", retest.for_role(RuntimeRole.RUNNER).extends)
            self.assertEqual(
                layout.reference_roots,
                retest.for_role(RuntimeRole.RUNNER).workspace_roots,
            )
            self.assertEqual((), retest.for_role(RuntimeRole.PLANNER).workspace_roots)
            self.assertEqual(
                set(sensitive_codex_home_selectors(home)),
                {
                    item.selector
                    for item in retest.for_role(RuntimeRole.RUNNER).filesystem_rules
                    if item.access == "deny"
                    and item.selector not in {":root", ":tmpdir", ":slash_tmp"}
                    and item.selector != layout.protected_roots[0]
                },
            )
            for definition in retest.definitions:
                rules = {
                    item.selector: item.access
                    for item in definition.filesystem_rules
                }
                self.assertNotIn(layout.authoritative_codex_home, rules)
                self.assertEqual(
                    "read",
                    rules[str((home / "AGENTS.md").resolve())],
                )
                self.assertEqual("deny", rules[str(home / "auth.json")])
                self.assertEqual("deny", rules[str(home / ".sandbox-secrets")])
                self.assertEqual("deny", rules[str(home / "*.sqlite*")])
            runner_rules = {
                item.selector: item.access
                for item in retest.for_role(RuntimeRole.RUNNER).filesystem_rules
            }
            self.assertEqual("write", runner_rules[layout.runner_write_root])
            self.assertEqual(
                "read",
                runner_rules[layout.reference_files[0]],
            )

    def test_global_tool_surface_is_role_specific_and_external_surfaces_are_off(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            profiles, layout = profile_set(Path(raw))
            home = Path(layout.authoritative_codex_home)
            planner = build_config_overrides(
                profiles, active_role=RuntimeRole.PLANNER, codex_home=home
            )
            runner = build_config_overrides(
                profiles, active_role=RuntimeRole.RUNNER, codex_home=home
            )
            self.assertIn("features.shell_tool=false", planner)
            self.assertIn('project_root_markers=[".flowmarshal-synthetic-root"]', planner)
            self.assertIn("features.shell_tool=true", runner)
            self.assertIn('web_search="disabled"', planner)
            self.assertIn("features.multi_agent=false", runner)
            self.assertTrue(
                any(
                    item.startswith("permissions.flowmarshal_gate0c_runner.workspace_roots=")
                    for item in runner
                )
            )
            self.assertFalse(any("sandbox_mode" in item for item in planner))
            self.assertFalse(surface_policy(RuntimeRole.PLANNER).shell_tool_enabled)

            scoped = build_config_overrides(
                profiles,
                active_role=RuntimeRole.PLANNER,
                codex_home=home,
                cwd=Path(layout.planner_context_root),
            )
            self.assertTrue(
                any(item.startswith("projects.") and "trust_level" in item for item in scoped)
            )

    def test_preflight_requires_allowed_profiles_no_legacy_and_shell_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            profiles, layout = profile_set(Path(raw))
            config = {
                "defaultPermissions": PLANNER_PROFILE_ID,
                "features": {"shell_tool": False},
            }
            client = FakeRawClient(
                {
                    "permissionProfile/list": {
                        "data": [
                            {"id": profile_id, "allowed": True}
                            for profile_id in PROFILE_IDS
                        ],
                        "nextCursor": None,
                    },
                    "config/read": {"config": config, "layers": []},
                }
            )
            evidence = preflight_profiles(
                client,
                cwd=Path(layout.planner_context_root),
                role=RuntimeRole.PLANNER,
                profile_set=profiles,
            )
            self.assertEqual(PLANNER_PROFILE_ID, evidence.active_profile_id)

            client.responses["config/read"] = {
                "config": config,
                "layers": [{"config": {"sandbox_mode": "workspace-write"}}],
            }
            with self.assertRaises(ProfileProbeError) as caught:
                preflight_profiles(
                    client,
                    cwd=Path(layout.planner_context_root),
                    role=RuntimeRole.PLANNER,
                    profile_set=profiles,
                )
            self.assertEqual("INVALID_CONFIGURATION", caught.exception.reason_code)

    def test_thread_start_uses_permissions_and_validates_active_profile(self) -> None:
        cwd = Path("D:/synthetic/planner")
        params = thread_start_params(
            role=RuntimeRole.PLANNER,
            cwd=cwd,
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        self.assertNotIn("sandbox", params)
        self.assertEqual(PLANNER_PROFILE_ID, params["permissions"])
        response = {
            "thread": {"id": "thread_gate0c"},
            "activePermissionProfile": {
                "id": PLANNER_PROFILE_ID,
                "extends": ":read-only",
            },
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "model": "gpt-5.6-sol",
            "reasoningEffort": "high",
            "cwd": str(cwd),
            "runtimeWorkspaceRoots": [str(cwd)],
            "sandbox": {"type": "readOnly"},
        }
        client = FakeRawClient({"thread/start": response})
        receipt = start_profiled_thread(
            client,
            role=RuntimeRole.PLANNER,
            cwd=cwd,
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        self.assertEqual(PLANNER_PROFILE_ID, receipt.active_profile_id)
        sent = client.requests[0][1]
        self.assertNotIn("sandbox", sent)

        response["activePermissionProfile"] = {"id": ":danger-full-access"}
        with self.assertRaises(ProfileProbeError) as caught:
            start_profiled_thread(
                client,
                role=RuntimeRole.PLANNER,
                cwd=cwd,
                model="gpt-5.6-sol",
                reasoning_effort="high",
            )
        self.assertEqual("PROFILE_PROVENANCE_MISSING", caught.exception.reason_code)

    def test_profile_list_missing_or_denied_fails_before_thread_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            profiles, layout = profile_set(Path(raw))
            client = FakeRawClient(
                {
                    "permissionProfile/list": {
                        "data": [{"id": PROFILE_IDS[0], "allowed": False}],
                        "nextCursor": None,
                    },
                    "config/read": {},
                }
            )
            with self.assertRaises(ProfileProbeError) as caught:
                preflight_profiles(
                    client,
                    cwd=Path(layout.planner_context_root),
                    role=RuntimeRole.PLANNER,
                    profile_set=profiles,
                )
            self.assertEqual("PROFILE_UNSUPPORTED", caught.exception.reason_code)
            self.assertNotIn("thread/start", [item[0] for item in client.requests])

    def test_model_and_effort_are_revalidated_before_dispatch(self) -> None:
        client = FakeRawClient(
            {
                "model/list": {
                    "data": [
                        {
                            "id": "gpt-5.6-sol",
                            "model": "gpt-5.6-sol",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "high"},
                                {"reasoningEffort": "xhigh"},
                            ],
                        }
                    ],
                    "nextCursor": None,
                }
            }
        )
        evidence = preflight_model(
            client, model="gpt-5.6-sol", effort="xhigh"
        )
        self.assertEqual("xhigh", evidence.requested_effort)
        with self.assertRaises(ProfileProbeError) as caught:
            preflight_model(client, model="gpt-5.6-sol", effort="ultra")
        self.assertEqual("MODEL_EFFORT_UNAVAILABLE", caught.exception.reason_code)


if __name__ == "__main__":
    unittest.main()
