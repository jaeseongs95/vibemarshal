from __future__ import annotations

import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine import release_freeze as rf
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES, ModelCapability, ModelInventory
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


@unittest.skipUnless(FM12_WHEEL.is_file(), "FM-12 candidate wheel evidence가 이 환경에 없습니다.")
@unittest.skipUnless(INVENTORY_PATH.is_file(), "저장된 model inventory 관측이 이 환경에 없습니다.")
class ReleaseFreezeBuildTests(unittest.TestCase):
    """실제 저장소 root를 읽기 전용으로 쓰고, 쓰기는 모두 temp 아래에만 한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temp.name)
        cls.wheel_copy = cls.work / "candidate.whl"
        shutil.copyfile(FM12_WHEEL, cls.wheel_copy)
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
        with patch.object(rf, "_git", side_effect=_patched_git(clean)), \
             patch.object(rf, "_is_ancestor", return_value=True):
            return rf.build_release_freeze(
                source_root=ROOT,
                destination=destination,
                candidate_wheel=wheel,
                built_from_commit=_FAKE_BUILT_FROM,
                inventory_path=self.inventory_path if inventory_path is None else inventory_path,
                shard_plan=_basic_shard_plan(self.work),
                allow_dirty_rehearsal=allow_dirty,
            )

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

        with patch.object(rf, "source_manifest_files", wraps=rf.source_manifest_files):
            result = rf.verify_release_freeze(destination, source_root=ROOT, candidate_wheel=self.wheel_copy)
        self.assertTrue(result.valid, result.mismatches)
        self.assertFalse(result.refreeze_required)
        self.assertEqual((), result.mismatches)

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
