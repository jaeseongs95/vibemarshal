from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine import release_freeze as rf
from flowmarshal.engine.evaluation_budget import load_evaluation_policies
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES, ModelCapability, ModelInventory
from flowmarshal.engine.models import EngineRoleConfiguration
from flowmarshal.engine.qualification import default_role_configuration, project_root


ROOT = project_root()
FM12_WHEEL = Path(
    "D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/"
    "FM-12-direct-20260917T2346Z/wheel/flowmarshal_engine-0.2.0a1-py3-none-any.whl"
)
INVENTORY_PATH = ROOT / ".flowmarshal-engine-eval" / "runs" / "alpha-a4-final-20260904" / "inventory.json"

_FAKE_COMMIT = "a" * 40
_FAKE_BUILT_FROM = "b" * 40
_TEST_EXECUTABLE_DIGEST = sha256_digest({"test_executable": "release-freeze"})


class _GovernanceSettings:
    def __init__(self, verdict: str = "PASS") -> None:
        self.calls = 0
        self.verdict = verdict

    def check_conformance(self):
        self.calls += 1
        return {
            "format": "flowmarshal-governance-conformance-v1",
            "provenance": "local_derived",
            "verdict": self.verdict,
            "check_set_digest": sha256_digest({"checks": "release-freeze-test"}),
            "identity": {
                "manifest_sha256": sha256_digest({"manifest": True}),
                "closure_tree_digest": sha256_bytes(b"dist/server.js\0" + b"0" * 64 + b"\n"),
                "entrypoint_table_digest": sha256_digest({"entrypoints": True}),
                "node_version": "v22.13.0",
                "consumed_surface_digest": sha256_digest({"surface": True}),
            },
            "identity_files": {"dist/server.js": "0" * 64},
            "identity_labels": [
                {"source": "plugin_manifest_file", "plugin": {"id": "fake", "version": "2.1.2"}},
                {"source": "mcp_server_info", "serverInfo": {"name": "fake", "version": "2.1.2"}},
            ],
            "installation_root": "D:/installed/agent-governance-suite",
            "cleanup_error": None,
            "checks": [
                {
                    "id": "all",
                    "status": self.verdict,
                    "expected": "PASS",
                    "observed": self.verdict,
                }
            ],
        }


def _inventory_with_executable(path: Path) -> ModelInventory:
    """저장 관측(raw model 목록)에 테스트용 executable/capability 결속만 더한다.

    실제 freeze는 adapter가 ``model/list`` 시점에 결속한 관측 파일을 그대로 요구하며,
    이 helper는 그 관측 형식을 테스트 안에서만 재현한다.
    """

    raw = ModelInventory.model_validate_json(path.read_text(encoding="utf-8"))
    return ModelInventory.model_validate(
        raw.model_dump(mode="python")
        | {
            "executable_digest": _TEST_EXECUTABLE_DIGEST,
            "runtime_capabilities": tuple(RUNTIME_CAPABILITIES),
        }
    )


def _current_source_candidate(destination: Path) -> None:
    """FM-12 wheel의 metadata에 현재 source product bytes를 넣은 테스트 전용 후보를 만든다.

    고정 FM-12 wheel은 built_from 이후의 source 변경과 비교하면 항상 달라진다. 이 테스트는
    freeze 도구 자체를 검사하므로 wheel·source 일치 조건을 현재 bytes로 재현한다.
    release 후보나 재동결 evidence가 아니다.
    """

    expected = rf._expected_source_product_files(ROOT)
    with zipfile.ZipFile(FM12_WHEEL) as source, \
         zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
        for name in source.namelist():
            if name.startswith("flowmarshal/") and not name.endswith("/"):
                continue
            out.writestr(name, source.read(name))
        for name in sorted(expected):
            out.writestr(name, (ROOT / "src" / name).read_bytes())


def _patched_git(clean: bool):
    def fake_git(root, *args):
        if args == ("rev-parse", "HEAD"):
            return _FAKE_COMMIT
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("status", "--porcelain"):
            return "" if clean else " M some/tracked/file.py"
        raise AssertionError(f"unexpected git invocation: {args}")

    return fake_git


def _basic_shard_plan(work: Path) -> rf.ShardIsolationPlan:
    return rf.ShardIsolationPlan(
        source_root=str(ROOT),
        shards=(
            rf.ShardRoot(lane="role", shard_id="role-s1", artifact_root=str(work / "shards" / "role-s1")),
            rf.ShardRoot(lane="planning", shard_id="planning-s1", artifact_root=str(work / "shards" / "planning-s1")),
            rf.ShardRoot(lane="e2e", shard_id="e2e-s1", artifact_root=str(work / "shards" / "e2e-s1")),
        ),
        shared_sqlite_paths=(),
        aggregate_join_root=str(work / "shards" / "aggregate"),
    )


def _inventory_for_roles(roles) -> ModelInventory:
    efforts: dict[str, set[str]] = {}
    for role in type(roles).model_fields:
        binding = roles.binding_for(role)
        for choice in (binding, *binding.allowed_fallbacks):
            efforts.setdefault(choice.model, set()).add(choice.effort)
    return ModelInventory(
        source="claude-code:configured-catalog@test",
        models=tuple(
            ModelCapability(model=model, supported_efforts=tuple(sorted(values)))
            for model, values in sorted(efforts.items())
        ),
        executable_digest=_TEST_EXECUTABLE_DIGEST,
        runtime_capabilities=tuple(RUNTIME_CAPABILITIES),
    )


def _fake_lane_contract(lane: str, policy_digest: str = "default"):
    digest = lambda field: sha256_digest(
        {"lane": lane, "field": field, "policy_digest": policy_digest}
    )
    return SimpleNamespace(
        contract_digest=digest("contract"),
        fixture_digests=(digest("fixture"),),
        scenario_set_digest=digest("scenarios"),
        order_seeds=(1,),
        expected_cell_count={"role": 48, "planning": 18, "e2e": 1}[lane],
        prompt_digest=digest("prompt"),
        output_schema_digest=digest("schema"),
        threshold_digest=digest("threshold"),
        taxonomy_digest=digest("taxonomy"),
    )


@contextmanager
def _deterministic_freeze_dependencies(plan: rf.ShardIsolationPlan, roles):
    source_files = {"src/flowmarshal/__init__.py": sha256_digest({"source": True})}
    inventory = _inventory_for_roles(roles)
    parser_surface = {"options": [], "subcommands": {}}
    package_identity = rf.PackageIdentityFreeze(
        distribution_name="flowmarshal-engine",
        distribution_version="0.2.0a1",
        console_entrypoint="flowmarshal-engine = flowmarshal.engine.console_host:main",
        parser_surface=parser_surface,
        parser_surface_digest=sha256_digest(parser_surface),
    )
    isolation = rf.IsolationPreflightReport(
        passed=True,
        source_manifest_digest_before=sha256_digest(source_files),
        source_manifest_digest_after=sha256_digest(source_files),
        plan_digest=sha256_digest(plan),
    )
    lane_contracts = unittest.mock.Mock(
        side_effect=lambda root, observed_inventory, observed_roles, policies: tuple(
            _fake_lane_contract(lane, getattr(policies, "policy_digest", "default"))
            for lane in ("role", "planning", "e2e")
        )
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(rf, "_git", side_effect=_patched_git(True)))
        stack.enter_context(patch.object(rf, "_is_ancestor", return_value=True))
        stack.enter_context(
            patch.object(
                rf,
                "_read_wheel",
                return_value=(
                    b"wheel",
                    {"flowmarshal/__init__.py": sha256_digest({"wheel": True})},
                    "flowmarshal-engine",
                    "0.2.0a1",
                    "[console_scripts]\nflowmarshal-engine = flowmarshal.engine.console_host:main\n",
                    "py3-none-any",
                ),
            )
        )
        stack.enter_context(patch.object(rf, "_diff_source_product_bytes", return_value=()))
        stack.enter_context(patch.object(rf, "_package_identity", return_value=package_identity))
        stack.enter_context(patch.object(rf, "source_manifest_files", return_value=source_files))
        stack.enter_context(patch.object(rf, "_tree_digest", return_value=sha256_digest({"fixtures": True})))
        stack.enter_context(
            patch.object(
                rf,
                "qualification_suite_manifest",
                return_value=SimpleNamespace(manifest_digest=sha256_digest({"suite": True})),
            )
        )
        stack.enter_context(patch.object(rf, "_default_policies", return_value=SimpleNamespace()))
        stack.enter_context(patch.object(rf, "_load_inventory_for_freeze", return_value=inventory))
        stack.enter_context(patch.object(rf, "_lane_contracts", lane_contracts))
        stack.enter_context(patch.object(rf, "preflight_shard_isolation", return_value=isolation))
        stack.enter_context(patch.object(rf, "_parser_surface", return_value=parser_surface))
        yield inventory, lane_contracts


class DeterministicReleaseFreezeGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.wheel = self.work / "candidate.whl"
        self.wheel.write_bytes(b"wheel")
        self.inventory_path = self.work / "inventory.json"
        self.inventory_path.write_text("{}\n", encoding="utf-8")
        self.plan = _basic_shard_plan(self.work)
        self.roles = EngineRoleConfiguration.model_validate_json(
            (ROOT / "config" / "qualification-roles.claude.json").read_text(encoding="utf-8")
        )
        self.policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        ).model_copy(update={"max_provider_calls": 7, "wall_timeout_seconds": 1234})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(
        self,
        destination: Path,
        governance: _GovernanceSettings,
        *,
        evaluation_policies=None,
    ):
        return rf.build_release_freeze(
            source_root=ROOT,
            destination=destination,
            candidate_wheel=self.wheel,
            built_from_commit=_FAKE_BUILT_FROM,
            inventory_path=self.inventory_path,
            shard_plan=self.plan,
            role_configuration=self.roles,
            evaluation_policies=evaluation_policies,
            governance_settings=governance,
        )

    def test_build_calls_conformance_once_and_requires_pass(self) -> None:
        passing = _GovernanceSettings()
        with _deterministic_freeze_dependencies(self.plan, self.roles):
            manifest = self._build(self.work / "pass", passing)
        self.assertEqual(1, passing.calls)
        self.assertEqual("PASS", manifest.governance_plugin.conformance_verdict)

        failing = _GovernanceSettings("FAIL")
        with _deterministic_freeze_dependencies(self.plan, self.roles):
            with self.assertRaisesRegex(
                rf.ReleaseFreezeError, "RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_NOT_PASS"
            ):
                self._build(self.work / "fail", failing)
        self.assertEqual(1, failing.calls)
        self.assertFalse((self.work / "fail" / "release-freeze.json").exists())

    def test_e2e_digest_binds_four_fields_and_verify_rejects_artifact_changes(self) -> None:
        destination = self.work / "freeze"
        with _deterministic_freeze_dependencies(self.plan, self.roles):
            manifest = self._build(destination, _GovernanceSettings())

        frozen = manifest.governance_plugin
        expected_fields = {
            "closure_tree_digest",
            "entrypoint_table_digest",
            "check_set_digest",
            "conformance_result_digest",
        }
        self.assertEqual(expected_fields, set(frozen.e2e_identity_fields))
        self.assertEqual(sha256_digest(frozen.e2e_identity_fields), frozen.e2e_identity_digest)
        for field in expected_fields:
            changed = frozen.model_dump(mode="python") | {field: sha256_digest({"changed": field})}
            with self.subTest(field=field), self.assertRaises(ValueError):
                rf.GovernancePluginFreeze.model_validate(changed)

        artifact = destination / frozen.conformance_result_relative_path
        artifact.write_text(artifact.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with _deterministic_freeze_dependencies(self.plan, self.roles):
            tampered = rf.verify_release_freeze(
                destination, source_root=ROOT, role_configuration=self.roles
            )
        self.assertIn(
            "RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_DIGEST_MISMATCH", tampered.mismatches
        )

        document = json.loads(artifact.read_text(encoding="utf-8"))
        document["verdict"] = "FAIL"
        artifact.write_text(rf._canonical_document(document), encoding="utf-8")
        with _deterministic_freeze_dependencies(self.plan, self.roles):
            non_pass = rf.verify_release_freeze(
                destination, source_root=ROOT, role_configuration=self.roles
            )
        self.assertIn("RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_NOT_PASS", non_pass.mismatches)

    def test_verify_rejects_governance_identity_label_mismatch(self) -> None:
        destination = self.work / "identity-labels"
        with _deterministic_freeze_dependencies(self.plan, self.roles):
            manifest = self._build(destination, _GovernanceSettings())

        artifact = destination / manifest.governance_plugin.conformance_result_relative_path
        original = json.loads(artifact.read_text(encoding="utf-8"))
        cases = (
            ("plugin_version_label", "plugin_manifest_file", "plugin", "version"),
            ("server_info", "mcp_server_info", "serverInfo", "name"),
        )
        for field, source, container, key in cases:
            document = json.loads(json.dumps(original))
            label = next(item for item in document["identity_labels"] if item["source"] == source)
            label[container][key] = "tampered"
            artifact.write_bytes(rf._canonical_document(document).encode("utf-8"))
            with self.subTest(field=field), _deterministic_freeze_dependencies(
                self.plan, self.roles
            ):
                result = rf.verify_release_freeze(
                    destination,
                    source_root=ROOT,
                    role_configuration=self.roles,
                )
                self.assertFalse(result.valid)
                self.assertIn("RELEASE_FREEZE_GOVERNANCE_BLOCK_MISMATCH", result.mismatches)

    def test_verify_rejects_refrozen_manifest_label_without_matching_artifact(self) -> None:
        cases = (
            ("plugin_version_label", "tampered"),
            ("server_info", {"name": "tampered", "version": "2.1.2"}),
        )
        for field, value in cases:
            destination = self.work / f"manifest-label-{field}"
            with _deterministic_freeze_dependencies(self.plan, self.roles):
                self._build(destination, _GovernanceSettings())
            manifest_path = destination / "release-freeze.json"
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["governance_plugin"][field] = value
            document.pop("freeze_digest")
            document["freeze_digest"] = sha256_digest(document)
            manifest_path.write_bytes(rf._canonical_document(document).encode("utf-8"))

            with self.subTest(field=field), _deterministic_freeze_dependencies(
                self.plan, self.roles
            ):
                result = rf.verify_release_freeze(
                    destination,
                    source_root=ROOT,
                    role_configuration=self.roles,
                )
                self.assertFalse(result.valid)
                self.assertIn("RELEASE_FREEZE_GOVERNANCE_BLOCK_MISMATCH", result.mismatches)

    def test_injected_claude_roles_are_reused_by_build_and_verify(self) -> None:
        destination = self.work / "claude-roles"
        with _deterministic_freeze_dependencies(self.plan, self.roles) as (_, lane_contracts), \
             patch.object(
                 rf,
                 "default_role_configuration",
                 side_effect=AssertionError("injected roles must not fall back to defaults"),
             ):
            manifest = self._build(destination, _GovernanceSettings())
            result = rf.verify_release_freeze(
                destination, source_root=ROOT, role_configuration=self.roles
            )

        self.assertEqual(self.roles.configuration_digest, manifest.inputs.roles_config_digest)
        self.assertTrue(result.valid, result.mismatches)
        self.assertEqual(2, lane_contracts.call_count)
        self.assertTrue(all(call.args[2] is self.roles for call in lane_contracts.call_args_list))

    def test_injected_evaluation_policies_are_reused_by_build_and_verify(self) -> None:
        destination = self.work / "custom-policies"
        with _deterministic_freeze_dependencies(self.plan, self.roles) as (_, lane_contracts), \
             patch.object(
                 rf,
                 "_default_policies",
                 side_effect=AssertionError("injected policies must not fall back to defaults"),
             ):
            self._build(
                destination,
                _GovernanceSettings(),
                evaluation_policies=self.policies,
            )
            result = rf.verify_release_freeze(
                destination,
                source_root=ROOT,
                role_configuration=self.roles,
                evaluation_policies=self.policies,
            )

        self.assertTrue(result.valid, result.mismatches)
        self.assertEqual(2, lane_contracts.call_count)
        self.assertTrue(all(call.args[3] is self.policies for call in lane_contracts.call_args_list))

    def test_verify_cli_wires_role_and_policy_configs(self) -> None:
        from flowmarshal.engine import eval_cli

        role_path = ROOT / "config" / "qualification-roles.claude.json"
        budget_path = self.work / "budget.json"
        timeout_path = self.work / "timeouts.json"
        project_path = self.work / "project.json"
        destination = self.work / "cli-freeze"
        result = SimpleNamespace(valid=True, model_dump=lambda mode: {"valid": True})
        with patch.object(eval_cli, "_roles", return_value=self.roles) as load_roles, \
             patch.object(
                 eval_cli, "load_evaluation_policies", return_value=self.policies
             ) as load_policies, \
             patch.object(eval_cli, "verify_release_freeze", return_value=result) as verify, \
             patch.object(eval_cli, "_emit"):
            exit_code = eval_cli.main(
                [
                    "verify-release-freeze",
                    "--project-root",
                    str(ROOT),
                    "--destination",
                    str(destination),
                    "--role-config",
                    str(role_path),
                    "--budget-policy",
                    str(budget_path),
                    "--role-timeout-policy",
                    str(timeout_path),
                    "--codex-project-binding",
                    str(project_path),
                ]
            )

        self.assertEqual(0, exit_code)
        load_roles.assert_called_once_with(str(role_path), ROOT.resolve())
        load_policies.assert_called_once_with(
            budget_policy_path=str(budget_path),
            role_timeout_policy_path=str(timeout_path),
            codex_project_binding_path=str(project_path),
        )
        self.assertIs(self.roles, verify.call_args.kwargs["role_configuration"])
        self.assertIs(self.policies, verify.call_args.kwargs["evaluation_policies"])


@unittest.skipUnless(FM12_WHEEL.is_file(), "FM-12 candidate wheel evidence가 이 환경에 없습니다.")
@unittest.skipUnless(INVENTORY_PATH.is_file(), "저장된 model inventory 관측이 이 환경에 없습니다.")
class ReleaseFreezeBuildTests(unittest.TestCase):
    """실제 저장소 root를 읽기 전용으로 쓰고, 쓰기는 모두 temp 아래에만 한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temp.name)
        cls.wheel_copy = cls.work / "candidate.whl"
        _current_source_candidate(cls.wheel_copy)
        cls.inventory_path = cls.work / "inventory-with-executable.json"
        cls.inventory_path.write_text(
            _inventory_with_executable(INVENTORY_PATH).model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _build(
        self,
        *,
        destination: Path,
        wheel: Path,
        clean: bool = True,
        allow_dirty: bool = False,
        inventory_path: Path | None = None,
    ):
        governance = _GovernanceSettings()
        with patch.object(rf, "_git", side_effect=_patched_git(clean)), \
             patch.object(rf, "_is_ancestor", return_value=True):
            manifest = rf.build_release_freeze(
                source_root=ROOT,
                destination=destination,
                candidate_wheel=wheel,
                built_from_commit=_FAKE_BUILT_FROM,
                inventory_path=self.inventory_path if inventory_path is None else inventory_path,
                shard_plan=_basic_shard_plan(self.work),
                governance_settings=governance,
                allow_dirty_rehearsal=allow_dirty,
            )
        self.assertEqual(1, governance.calls)
        return manifest

    def test_inventory_without_executable_digest_is_rejected(self) -> None:
        # 저장 관측에 adapter executable 결속이 없으면 placeholder로 합성하지 않고 거부한다.
        with self.assertRaises(rf.ReleaseFreezeError) as ctx:
            self._build(
                destination=self.work / "freeze-no-executable",
                wheel=self.wheel_copy,
                inventory_path=INVENTORY_PATH,
            )
        self.assertIn("RELEASE_FREEZE_INVENTORY_EXECUTABLE_DIGEST_REQUIRED", str(ctx.exception))
        self.assertFalse((self.work / "freeze-no-executable" / "release-freeze.json").exists())

    def test_rehearsal_manifest_is_rejected_as_release_freeze(self) -> None:
        destination = self.work / "freeze-rehearsal-verify"
        manifest = self._build(destination=destination, wheel=self.wheel_copy, clean=False, allow_dirty=True)
        self.assertTrue(manifest.rehearsal)
        result = rf.verify_release_freeze(destination, source_root=ROOT, candidate_wheel=self.wheel_copy)
        self.assertFalse(result.valid)
        self.assertIn("RELEASE_FREEZE_REHEARSAL_MANIFEST", result.mismatches)
        allowed = rf.verify_release_freeze(
            destination, source_root=ROOT, candidate_wheel=self.wheel_copy, allow_rehearsal=True
        )
        self.assertNotIn("RELEASE_FREEZE_REHEARSAL_MANIFEST", allowed.mismatches)
        self.assertTrue(allowed.valid, allowed.mismatches)

    def test_inventory_observation_tamper_detected_at_verify(self) -> None:
        destination = self.work / "freeze-inventory-tamper"
        manifest = self._build(destination=destination, wheel=self.wheel_copy, clean=True)
        observation = destination / manifest.model_inventory.inventory_observation_relative_path
        tampered = ModelInventory.model_validate(
            ModelInventory.model_validate_json(observation.read_text(encoding="utf-8")).model_dump(mode="python")
            | {"executable_digest": sha256_digest({"other_executable": True})}
        )
        observation.write_text(tampered.model_dump_json(indent=2) + "\n", encoding="utf-8")
        result = rf.verify_release_freeze(destination, source_root=ROOT, candidate_wheel=self.wheel_copy)
        self.assertFalse(result.valid)
        self.assertIn("RELEASE_FREEZE_INVENTORY_OBSERVATION_MISMATCH", result.mismatches)
        self.assertTrue(result.refreeze_required)

    def test_build_then_verify_pass(self) -> None:
        destination = self.work / "freeze-ok"
        manifest = self._build(destination=destination, wheel=self.wheel_copy, clean=True)
        self.assertEqual(manifest.source.commit, _FAKE_COMMIT)
        self.assertEqual(manifest.candidate_wheel.built_from_commit, _FAKE_BUILT_FROM)
        self.assertTrue(manifest.candidate_wheel.wheel_matches_source_product_bytes)
        # release_freeze.py 자신은 developer module이라 wheel에 없어야 하고,
        # 그래도 product bytes 대조는 통과해야 한다(브리핑 8번째 케이스).
        self.assertNotIn("flowmarshal/engine/release_freeze.py", manifest.candidate_wheel.package_file_digests)
        self.assertEqual({item.lane for item in manifest.lane_locks}, {"role", "planning", "e2e"})
        self.assertEqual(manifest.lane_lock("role").expected_cell_count, 48)
        self.assertEqual(manifest.lane_lock("planning").expected_cell_count, 18)
        self.assertTrue(manifest.isolation_preflight.passed)
        self.assertEqual("2.1.2", manifest.governance_plugin.plugin_version_label)
        self.assertEqual(
            sha256_digest(manifest.governance_plugin.e2e_identity_fields),
            manifest.governance_plugin.e2e_identity_digest,
        )
        self.assertTrue((destination / "governance-conformance.json").is_file())
        for name in ("config/claude-model-catalog.json", "config/qualification-roles.claude.json"):
            self.assertEqual(
                sha256_bytes((ROOT / name).read_bytes()), manifest.inputs.config_file_digests[name], name,
            )

        with patch.object(rf, "source_manifest_files", wraps=rf.source_manifest_files):
            result = rf.verify_release_freeze(destination, source_root=ROOT, candidate_wheel=self.wheel_copy)
        self.assertTrue(result.valid, result.mismatches)
        self.assertFalse(result.refreeze_required)
        self.assertEqual((), result.mismatches)

    def test_governance_conformance_artifact_tamper_is_detected_without_rerun(self) -> None:
        destination = self.work / "freeze-governance-tamper"
        self._build(destination=destination, wheel=self.wheel_copy, clean=True)
        artifact = destination / "governance-conformance.json"
        artifact.write_text(artifact.read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = rf.verify_release_freeze(destination, source_root=ROOT, candidate_wheel=self.wheel_copy)
        self.assertFalse(result.valid)
        self.assertIn("RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_DIGEST_MISMATCH", result.mismatches)

    def test_claude_config_change_detected_at_verify(self) -> None:
        destination = self.work / "freeze-for-claude-config"
        self._build(destination=destination, wheel=self.wheel_copy, clean=True)
        original_read_bytes = Path.read_bytes

        for name in ("config/claude-model-catalog.json", "config/qualification-roles.claude.json"):
            target = (ROOT / name).resolve()

            def drifted(path: Path, target=target) -> bytes:
                data = original_read_bytes(path)
                return data + b"\n" if path.resolve() == target else data

            with patch.object(Path, "read_bytes", drifted):
                result = rf.verify_release_freeze(destination, source_root=ROOT)
            self.assertFalse(result.valid, name)
            self.assertIn("RELEASE_FREEZE_CONFIG_DIGEST_MISMATCH", result.mismatches)
            self.assertTrue(result.refreeze_required)

    def test_tampered_wheel_bytes_rejected_at_build(self) -> None:
        tampered = self.work / "tampered.whl"
        with zipfile.ZipFile(self.wheel_copy) as source_zip:
            names = source_zip.namelist()
            target = next(n for n in names if n == "flowmarshal/engine/cli.py")
            with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as out_zip:
                for name in names:
                    data = source_zip.read(name)
                    if name == target:
                        data = data + b"\n# tampered\n"
                    out_zip.writestr(name, data)
        with self.assertRaises(rf.ReleaseFreezeError) as ctx:
            self._build(destination=self.work / "freeze-tampered", wheel=tampered, clean=True)
        self.assertIn("RELEASE_FREEZE_WHEEL_SOURCE_MISMATCH", str(ctx.exception))

    def test_tampered_wheel_bytes_rejected_at_verify(self) -> None:
        destination = self.work / "freeze-for-verify-tamper"
        self._build(destination=destination, wheel=self.wheel_copy, clean=True)
        tampered = self.work / "tampered-verify.whl"
        original = self.wheel_copy.read_bytes()
        mutated = bytearray(original)
        mutated[-1] ^= 0xFF
        tampered.write_bytes(bytes(mutated))
        result = rf.verify_release_freeze(destination, source_root=ROOT, candidate_wheel=tampered)
        self.assertFalse(result.valid)
        self.assertIn("RELEASE_FREEZE_WHEEL_MISMATCH", result.mismatches)
        self.assertTrue(result.refreeze_required)

    def test_source_change_detected_at_verify(self) -> None:
        destination = self.work / "freeze-for-source-change"
        self._build(destination=destination, wheel=self.wheel_copy, clean=True)
        real_files = rf.source_manifest_files(ROOT)
        drifted_files = dict(real_files)
        drifted_files["synthetic/drifted-file.py"] = "sha256:" + ("0" * 64)
        with patch.object(rf, "source_manifest_files", return_value=drifted_files):
            result = rf.verify_release_freeze(destination, source_root=ROOT)
        self.assertFalse(result.valid)
        self.assertIn("RELEASE_FREEZE_SOURCE_MISMATCH", result.mismatches)
        self.assertTrue(result.refreeze_required)

    def test_dirty_worktree_rejected_without_flag(self) -> None:
        with self.assertRaises(rf.ReleaseFreezeError) as ctx:
            self._build(destination=self.work / "freeze-dirty", wheel=self.wheel_copy, clean=False, allow_dirty=False)
        self.assertIn("RELEASE_FREEZE_DIRTY_WORKTREE", str(ctx.exception))

    def test_dirty_worktree_allowed_as_rehearsal(self) -> None:
        destination = self.work / "freeze-dirty-rehearsal"
        manifest = self._build(destination=destination, wheel=self.wheel_copy, clean=False, allow_dirty=True)
        self.assertTrue(manifest.rehearsal)
        self.assertFalse(manifest.source.clean)

    def test_existing_manifest_cannot_be_overwritten_with_different_content(self) -> None:
        destination = self.work / "freeze-immutable"
        destination.mkdir(parents=True)
        (destination / "release-freeze.json").write_text('{"different": true}\n', encoding="utf-8")
        with self.assertRaises(rf.ReleaseFreezeError) as ctx:
            self._build(destination=destination, wheel=self.wheel_copy, clean=True)
        self.assertIn("RELEASE_FREEZE_IMMUTABLE_OVERWRITE", str(ctx.exception))


class IsolationPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_clean_plan_passes(self) -> None:
        plan = _basic_shard_plan(self.work)
        report = rf.preflight_shard_isolation(plan)
        self.assertTrue(report.passed)
        self.assertEqual((), report.violations)

    def test_overlapping_shard_roots_rejected(self) -> None:
        role_root = self.work / "shards" / "role-s1"
        plan = rf.ShardIsolationPlan(
            source_root=str(ROOT),
            shards=(
                rf.ShardRoot(lane="role", shard_id="role-s1", artifact_root=str(role_root)),
                rf.ShardRoot(lane="role", shard_id="role-s2", artifact_root=str(role_root / "nested")),
            ),
            shared_sqlite_paths=(),
            aggregate_join_root=str(self.work / "shards" / "aggregate"),
        )
        report = rf.preflight_shard_isolation(plan)
        self.assertFalse(report.passed)
        self.assertTrue(any(v.startswith("ISOLATION_SHARD_ROOT_OVERLAP") for v in report.violations))

    def test_root_inside_source_root_rejected(self) -> None:
        plan = rf.ShardIsolationPlan(
            source_root=str(ROOT),
            shards=(
                rf.ShardRoot(lane="role", shard_id="role-s1", artifact_root=str(ROOT / "shard-inside-source")),
            ),
            shared_sqlite_paths=(),
            aggregate_join_root=str(self.work / "shards" / "aggregate"),
        )
        report = rf.preflight_shard_isolation(plan)
        self.assertFalse(report.passed)
        self.assertTrue(any(v.startswith("ISOLATION_ROOT_INSIDE_SOURCE_ROOT") for v in report.violations))

    def test_shared_sqlite_inside_shard_root_rejected(self) -> None:
        role_root = self.work / "shards" / "role-s1"
        plan = rf.ShardIsolationPlan(
            source_root=str(ROOT),
            shards=(rf.ShardRoot(lane="role", shard_id="role-s1", artifact_root=str(role_root)),),
            shared_sqlite_paths=(str(role_root / "orchestration-status.sqlite3"),),
            aggregate_join_root=str(self.work / "shards" / "aggregate"),
        )
        report = rf.preflight_shard_isolation(plan)
        self.assertFalse(report.passed)
        self.assertTrue(any(v.startswith("ISOLATION_SHARED_SQLITE_INSIDE_SHARD_ROOT") for v in report.violations))


@unittest.skipUnless(INVENTORY_PATH.is_file(), "저장된 model inventory 관측이 이 환경에 없습니다.")
class InventoryChangeClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.roles = default_role_configuration(ROOT)
        cls.previous = _inventory_with_executable(INVENTORY_PATH)

    def test_raw_only_change_is_audit_only(self) -> None:
        candidate = ModelInventory.model_validate(
            self.previous.model_dump(mode="python")
            | {
                "models": tuple(self.previous.models)
                + (ModelCapability(model="synthetic-extra-model", supported_efforts=("high",)),)
            }
        )
        result = rf.classify_inventory_change(previous=self.previous, candidate=candidate, roles=self.roles)
        self.assertFalse(result.refreeze_required)
        self.assertIn("provider_inventory_digest", result.changed_fields)
        self.assertIn("provider_inventory_digest", result.audit_only_fields)

    def test_executable_change_requires_refreeze(self) -> None:
        candidate = ModelInventory.model_validate(
            self.previous.model_dump(mode="python")
            | {"executable_digest": sha256_digest({"other_executable": True})}
        )
        result = rf.classify_inventory_change(previous=self.previous, candidate=candidate, roles=self.roles)
        self.assertTrue(result.refreeze_required)
        self.assertIn("executable_digest", result.changed_fields)
        self.assertIn("selected_fallback_projection_digest", result.changed_fields)

    def test_no_change_is_a_noop(self) -> None:
        result = rf.classify_inventory_change(previous=self.previous, candidate=self.previous, roles=self.roles)
        self.assertFalse(result.refreeze_required)
        self.assertEqual((), result.changed_fields)


if __name__ == "__main__":
    unittest.main()
