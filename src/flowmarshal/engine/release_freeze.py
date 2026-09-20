"""1.0 release freeze: Role48/Planning18/E2E가 공유하는 RC source·wheel·qualification 입력을 한 번 고정한다.

이 모듈은 scope별 reproduction bundle(``QualificationFreezeManifest``,
``write_qualification_reproduction_bundle``)을 대체하지 않는다. 그 위에
common source commit·candidate wheel·package identity·model inventory
projection·lane별 immutable lock과 병렬 shard 격리 preflight를 한 번 더
고정하는 developer-only 계약이다. 사용자 wheel에는 포함되지 않는다
(``setup.py``의 ``ENGINE_DEVELOPER_MODULES``에 등록한다).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .domain import EngineModel
from .evaluation_budget import EvaluationPolicies, load_evaluation_policies
from .e2e_qualification import _contract as _build_e2e_contract
from .e2e_qualification import _project_e2e_fixture_source_digest
from .model_lock import ModelInventory
from .models import EngineRoleConfiguration
from .qualification import (
    PlanningScenarioCatalog,
    _combined_role_catalog,
    _planning_contract,
    _role_contract,
    default_role_configuration,
    qualification_suite_manifest,
    source_manifest_files,
)


class ReleaseFreezeError(RuntimeError):
    """release freeze 생성·검증 계약이 깨진 경우."""


DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$"
FORMAT = "flowmarshal.release-freeze.v1"

CONFIG_FILES: tuple[str, ...] = (
    "config/claude-model-catalog.json",
    "config/legacy-freeze-manifest.json",
    "config/pre-1.0-performance-thresholds.json",
    "config/pre-1.0-role-timeouts.json",
    "config/pre-1.0-validation-budget.json",
    "config/qualification-finding-taxonomy.json",
    "config/qualification-roles.claude.json",
    "config/qualification-roles.json",
    "config/qualification-suite.json",
)

# build_qualification_reproduction_bundle의 evaluator_files와 같은 6개 + 이 모듈 자신.
EVALUATOR_FILES: tuple[str, ...] = (
    "scripts/installed_candidate_qualification.py",
    "src/flowmarshal/engine/evaluation.py",
    "src/flowmarshal/engine/qualification.py",
    "src/flowmarshal/engine/qualification_manifest.py",
    "src/flowmarshal/engine/e2e_qualification.py",
    "src/flowmarshal/engine/benchmark_safety.py",
    "src/flowmarshal/engine/release_freeze.py",
)


# --------------------------------------------------------------------------
# typed manifest sections
# --------------------------------------------------------------------------


class SourceFreeze(EngineModel):
    commit: str = Field(pattern=COMMIT_PATTERN)
    branch: str = Field(min_length=1)
    clean: bool
    source_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    source_manifest_file_count: int = Field(ge=1)


class CandidateWheelFreeze(EngineModel):
    wheel_path: str = Field(min_length=1)
    wheel_digest: str = Field(pattern=DIGEST_PATTERN)
    wheel_size_bytes: int = Field(ge=1)
    distribution_name: str = Field(min_length=1)
    distribution_version: str = Field(min_length=1)
    entry_points_text: str = Field(min_length=1)
    python_tag: str = Field(min_length=1)
    package_file_digests: dict[str, str] = Field(min_length=1)
    wheel_package_digest: str = Field(pattern=DIGEST_PATTERN)
    built_from_commit: str = Field(pattern=COMMIT_PATTERN)
    built_from_commit_is_ancestor: Literal[True] = True
    wheel_matches_source_product_bytes: Literal[True] = True

    @field_validator("wheel_path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("candidate wheel 경로는 절대경로여야 합니다.")
        return value

    @model_validator(mode="after")
    def _package_digest_matches_files(self) -> "CandidateWheelFreeze":
        for relative, digest in self.package_file_digests.items():
            path = PurePosixPath(relative)
            if (
                not relative.startswith("flowmarshal/")
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("candidate package 파일 경로가 안전하지 않습니다.")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("candidate package 파일 digest가 올바르지 않습니다.")
        if sha256_digest(self.package_file_digests) != self.wheel_package_digest:
            raise ValueError("wheel package 집계 digest가 파일 장부와 다릅니다.")
        return self


class PackageIdentityFreeze(EngineModel):
    distribution_name: str = Field(min_length=1)
    distribution_version: str = Field(min_length=1)
    console_entrypoint: str = Field(min_length=1)
    parser_surface: dict[str, Any]
    parser_surface_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def _digest_matches(self) -> "PackageIdentityFreeze":
        if sha256_digest(self.parser_surface) != self.parser_surface_digest:
            raise ValueError("package identity parser surface digest가 본문과 다릅니다.")
        return self


class InputsFreeze(EngineModel):
    config_file_digests: dict[str, str] = Field(min_length=1)
    fixtures_tree_digest: str = Field(pattern=DIGEST_PATTERN)
    suite_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    roles_config_digest: str = Field(pattern=DIGEST_PATTERN)
    evaluator_file_digests: dict[str, str] = Field(min_length=1)
    role_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    role_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    planning_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    planning_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    e2e_prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    e2e_schema_digest: str = Field(pattern=DIGEST_PATTERN)


class ModelInventoryFreeze(EngineModel):
    inventory_digest: str = Field(pattern=DIGEST_PATTERN)
    provider_inventory_digest: str = Field(pattern=DIGEST_PATTERN)
    adapter_capability_digest: str = Field(pattern=DIGEST_PATTERN)
    executable_digest: str = Field(pattern=DIGEST_PATTERN)
    selected_fallback_projection: dict[str, Any]
    selected_fallback_projection_digest: str = Field(pattern=DIGEST_PATTERN)
    inventory_observation_relative_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def _digest_matches(self) -> "ModelInventoryFreeze":
        if sha256_digest(self.selected_fallback_projection) != self.selected_fallback_projection_digest:
            raise ValueError("selected/fallback projection digest가 본문과 다릅니다.")
        return self


class RefreezePolicy(EngineModel):
    format: Literal["flowmarshal.release-freeze-refreeze-policy.v1"] = (
        "flowmarshal.release-freeze-refreeze-policy.v1"
    )
    # 이 필드들의 변화만 실행 재동결 조건이다.
    refreeze_required_fields: tuple[str, ...] = (
        "selected_fallback_projection_digest",
        "adapter_capability_digest",
        "executable_digest",
    )
    # raw provider 전체 관측 변화는 audit 자료로만 보존한다.
    audit_only_fields: tuple[str, ...] = ("provider_inventory_digest",)


class LaneLock(EngineModel):
    lane: Literal["role", "planning", "e2e"]
    evaluation_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    fixture_digests: tuple[str, ...] = Field(min_length=1)
    scenario_set_digest: str = Field(pattern=DIGEST_PATTERN)
    order_seeds: tuple[int, ...] = Field(min_length=1)
    expected_cell_count: int = Field(ge=1)
    prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    threshold_digest: str = Field(pattern=DIGEST_PATTERN)
    taxonomy_digest: str = Field(pattern=DIGEST_PATTERN)
    evidence_record_formats: tuple[str, ...] = Field(min_length=1)
    run_root_policy: str = Field(min_length=1)


class ShardRoot(EngineModel):
    lane: Literal["role", "planning", "e2e"]
    shard_id: str = Field(min_length=1)
    artifact_root: str = Field(min_length=1)

    @field_validator("artifact_root")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("shard artifact root는 절대경로여야 합니다.")
        return value


class ShardIsolationPlan(EngineModel):
    format: Literal["flowmarshal.shard-isolation-plan.v1"] = "flowmarshal.shard-isolation-plan.v1"
    source_root: str = Field(min_length=1)
    shards: tuple[ShardRoot, ...] = Field(min_length=1)
    shared_sqlite_paths: tuple[str, ...] = ()
    aggregate_join_root: str = Field(min_length=1)

    @field_validator("source_root", "aggregate_join_root")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("isolation plan 경로는 절대경로여야 합니다.")
        return value

    @field_validator("shared_sqlite_paths")
    @classmethod
    def _absolute_sqlite(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not Path(item).is_absolute():
                raise ValueError("공유 SQLite 경로는 절대경로여야 합니다.")
        return value

    @model_validator(mode="after")
    def _shard_ids_unique(self) -> "ShardIsolationPlan":
        keys = [(shard.lane, shard.shard_id) for shard in self.shards]
        if len(keys) != len(set(keys)):
            raise ValueError("shard (lane, shard_id) 조합이 중복됐습니다.")
        return self


class IsolationPreflightReport(EngineModel):
    format: Literal["flowmarshal.isolation-preflight.v1"] = "flowmarshal.isolation-preflight.v1"
    passed: bool
    violations: tuple[str, ...] = ()
    source_manifest_digest_before: str = Field(pattern=DIGEST_PATTERN)
    source_manifest_digest_after: str = Field(pattern=DIGEST_PATTERN)
    plan_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def _passed_matches_violations(self) -> "IsolationPreflightReport":
        if self.passed != (len(self.violations) == 0):
            raise ValueError("passed 값이 violations 목록과 모순됩니다.")
        return self


class GovernancePluginFreeze(EngineModel):
    """release qualification이 재사용할 설치형 governance plugin의 동결 관측."""

    manifest_sha256: str = Field(pattern=DIGEST_PATTERN)
    closure_tree_digest: str = Field(pattern=DIGEST_PATTERN)
    closure_file_digests: dict[str, str]
    entrypoint_table_digest: str = Field(pattern=DIGEST_PATTERN)
    node_version: str = Field(min_length=1)
    consumed_surface_digest: str = Field(pattern=DIGEST_PATTERN)
    check_set_digest: str = Field(pattern=DIGEST_PATTERN)
    conformance_verdict: Literal["PASS"] = "PASS"
    conformance_result_digest: str = Field(pattern=DIGEST_PATTERN)
    conformance_result_relative_path: str = "governance-conformance.json"
    plugin_version_label: str | None = None
    server_info: dict[str, Any] | None = None
    installation_root: str = Field(min_length=1)
    e2e_identity_digest: str = Field(pattern=DIGEST_PATTERN)

    @property
    def e2e_identity_fields(self) -> dict[str, str]:
        return {
            "closure_tree_digest": self.closure_tree_digest,
            "entrypoint_table_digest": self.entrypoint_table_digest,
            "check_set_digest": self.check_set_digest,
            "conformance_result_digest": self.conformance_result_digest,
        }

    @model_validator(mode="after")
    def _identity_digest_matches_exact_rebind_fields(self) -> "GovernancePluginFreeze":
        if not self.closure_file_digests or any(
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in self.closure_file_digests.values()
        ):
            raise ValueError("governance closure 파일 digest가 비었거나 sha256 형식이 아닙니다.")
        closure_digest = sha256_bytes("".join(
            f"{path}\0{digest}\n" for path, digest in sorted(self.closure_file_digests.items())
        ).encode("utf-8"))
        if self.closure_tree_digest != closure_digest:
            raise ValueError("governance closure tree digest가 파일 map과 다릅니다.")
        if self.e2e_identity_digest != sha256_digest(self.e2e_identity_fields):
            raise ValueError("governance E2E identity digest가 네 rebind 필드와 다릅니다.")
        return self


class ReleaseFreezeManifest(EngineModel):
    format: Literal["flowmarshal.release-freeze.v1"] = FORMAT
    rehearsal: bool = False
    source: SourceFreeze
    candidate_wheel: CandidateWheelFreeze
    package_identity: PackageIdentityFreeze
    inputs: InputsFreeze
    model_inventory: ModelInventoryFreeze
    governance_plugin: GovernancePluginFreeze
    lane_locks: tuple[LaneLock, ...] = Field(min_length=3, max_length=3)
    isolation_preflight: IsolationPreflightReport
    refreeze_policy: RefreezePolicy
    freeze_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def _lanes_are_role_planning_e2e(self) -> "ReleaseFreezeManifest":
        lanes = tuple(sorted(item.lane for item in self.lane_locks))
        if lanes != ("e2e", "planning", "role"):
            raise ValueError("lane_locks는 role/planning/e2e를 정확히 하나씩 포함해야 합니다.")
        return self

    @model_validator(mode="after")
    def _digest_matches_body(self) -> "ReleaseFreezeManifest":
        expected = sha256_digest(self.model_dump(mode="json", exclude={"freeze_digest"}))
        if self.freeze_digest != expected:
            raise ValueError("release freeze digest가 manifest 본문과 다릅니다.")
        return self

    def lane_lock(self, lane: str) -> LaneLock:
        return next(item for item in self.lane_locks if item.lane == lane)


class InventoryChangeClassification(EngineModel):
    refreeze_required: bool
    changed_fields: tuple[str, ...] = ()
    audit_only_fields: tuple[str, ...] = ()


class ReleaseFreezeVerification(EngineModel):
    valid: bool
    refreeze_required: bool
    mismatches: tuple[str, ...] = ()
    audit_only_changes: tuple[str, ...] = ()
    manifest: ReleaseFreezeManifest


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        raise ReleaseFreezeError(
            f"RELEASE_FREEZE_GIT_COMMAND_FAILED:{' '.join(args)}:{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode not in (0, 1):
        raise ReleaseFreezeError(
            f"RELEASE_FREEZE_GIT_ANCESTOR_CHECK_FAILED:{result.stderr.strip()}"
        )
    return result.returncode == 0


def _tree_digest(root: Path) -> str:
    return sha256_digest({
        path.relative_to(root).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    })


def _read_wheel(path: Path) -> tuple[bytes, dict[str, str], str, str, str, str]:
    """wheel bytes에서 package identity·package file digest를 직접 읽는다.

    설치를 요구하지 않는다(``verify_candidate_wheel_installation``과 달리 이
    freeze 단계는 아직 어떤 venv에도 설치되지 않은 후보 wheel을 다룬다).
    """

    try:
        wheel_bytes = path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_DUPLICATE_PATH")
            for name in names:
                posix = PurePosixPath(name)
                if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
                    raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_PATH_ESCAPE")
            metadata_names = tuple(
                name for name in names
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            )
            if len(metadata_names) != 1:
                raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_METADATA_INVALID")
            message = BytesParser().parsebytes(archive.read(metadata_names[0]))
            distribution_name = str(message.get("Name") or "").strip()
            distribution_version = str(message.get("Version") or "").strip()
            if not distribution_name or not distribution_version:
                raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_METADATA_INVALID")
            dist_info_prefix = metadata_names[0].rsplit("/", 1)[0]
            entry_points_name = f"{dist_info_prefix}/entry_points.txt"
            if entry_points_name not in names:
                raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_ENTRY_POINTS_MISSING")
            entry_points_text = archive.read(entry_points_name).decode("utf-8")
            wheel_meta_name = f"{dist_info_prefix}/WHEEL"
            if wheel_meta_name not in names:
                raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_TAG_MISSING")
            wheel_message = BytesParser().parsebytes(archive.read(wheel_meta_name))
            tags = wheel_message.get_all("Tag") or []
            if not tags:
                raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_TAG_MISSING")
            python_tag = str(tags[0]).split("-")[0]
            package_file_digests = {
                name: sha256_bytes(archive.read(name))
                for name in names
                if name.startswith("flowmarshal/") and not name.endswith("/")
            }
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_ARCHIVE_INVALID") from error
    if "flowmarshal/__init__.py" not in package_file_digests:
        raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_FLOWMARSHAL_PACKAGE_MISSING")
    return wheel_bytes, package_file_digests, distribution_name, distribution_version, entry_points_text, python_tag


def _launcher_module(root: Path):
    """``scripts/installed_candidate_qualification.py``의 allowlist 관례를 재사용한다."""

    import importlib.util

    path = root / "scripts" / "installed_candidate_qualification.py"
    spec = importlib.util.spec_from_file_location("_flowmarshal_release_freeze_launcher", path)
    if spec is None or spec.loader is None:
        raise ReleaseFreezeError("RELEASE_FREEZE_LAUNCHER_MODULE_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_source_product_files(root: Path) -> dict[str, str]:
    module = _launcher_module(root)
    developer_modules = module.load_developer_modules(root)
    expected: dict[str, str] = {}
    top_level = root / "src" / "flowmarshal"
    for name in ("__init__.py", "canonical.py", "time.py"):
        expected[f"flowmarshal/{name}"] = sha256_bytes((top_level / name).read_bytes())
    engine_dir = top_level / "engine"
    for path in sorted(engine_dir.glob("*.py")):
        if path.stem in developer_modules:
            continue
        expected[f"flowmarshal/engine/{path.name}"] = sha256_bytes(path.read_bytes())
    return expected


def _diff_source_product_bytes(root: Path, package_file_digests: Mapping[str, str]) -> list[str]:
    expected = _expected_source_product_files(root)
    expected_keys = set(expected)
    actual_keys = set(package_file_digests)
    diffs: list[str] = []
    for missing in sorted(expected_keys - actual_keys):
        diffs.append(f"MISSING_IN_WHEEL:{missing}")
    for extra in sorted(actual_keys - expected_keys):
        diffs.append(f"UNEXPECTED_IN_WHEEL:{extra}")
    for key in sorted(expected_keys & actual_keys):
        if expected[key] != package_file_digests[key]:
            diffs.append(f"DIGEST_MISMATCH:{key}")
    return diffs


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _parser_surface(parser: argparse.ArgumentParser) -> dict[str, Any]:
    options = sorted(
        option
        for action in parser._actions  # noqa: SLF001 - argparse는 구조 introspection 공개 API가 없다.
        for option in action.option_strings
        if option not in ("-h", "--help")
    )
    subcommands: dict[str, Any] = {}
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, subparser in action.choices.items():
                subcommands[name] = _parser_surface(subparser)
    return {"options": options, "subcommands": subcommands}


def _package_identity(root: Path, distribution_name: str, distribution_version: str, entry_points_text: str) -> PackageIdentityFreeze:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    expected_name = str(project["name"])
    expected_version = str(project["version"])
    scripts = project["scripts"]
    if (
        _normalize_distribution_name(distribution_name) != _normalize_distribution_name(expected_name)
        or distribution_version != expected_version
    ):
        raise ReleaseFreezeError("RELEASE_FREEZE_PACKAGE_METADATA_MISMATCH")
    if len(scripts) != 1:
        raise ReleaseFreezeError("RELEASE_FREEZE_ENTRY_POINT_COUNT_UNSUPPORTED")
    (script_name, script_target), = scripts.items()
    expected_entry_points = "[console_scripts]\n" + f"{script_name} = {script_target}\n"
    if entry_points_text.replace("\r\n", "\n") != expected_entry_points:
        raise ReleaseFreezeError("RELEASE_FREEZE_ENTRY_POINTS_MISMATCH")
    from .cli import build_parser

    surface = _parser_surface(build_parser())
    return PackageIdentityFreeze(
        distribution_name=expected_name,
        distribution_version=expected_version,
        console_entrypoint=f"{script_name} = {script_target}",
        parser_surface=surface,
        parser_surface_digest=sha256_digest(surface),
    )


def _default_policies(root: Path) -> EvaluationPolicies:
    return load_evaluation_policies(
        budget_policy_path=root / "config" / "pre-1.0-validation-budget.json",
        role_timeout_policy_path=root / "config" / "pre-1.0-role-timeouts.json",
    )


def _load_inventory_for_freeze(path: Path) -> ModelInventory:
    """저장된 provider 관측을 그대로 로드한다.

    ``executable_digest``와 runtime capability는 실제 adapter가 ``model/list``
    관측 시점에 결속한 값이어야 한다. 없는 값을 placeholder로 합성하면 재동결
    조건(executable/capability 변화)이 가짜 기준이 되므로 fail-closed로 거부한다.
    """

    inventory = ModelInventory.model_validate_json(path.read_text(encoding="utf-8"))
    if inventory.executable_digest is None:
        raise ReleaseFreezeError("RELEASE_FREEZE_INVENTORY_EXECUTABLE_DIGEST_REQUIRED")
    if not inventory.runtime_capabilities:
        raise ReleaseFreezeError("RELEASE_FREEZE_INVENTORY_RUNTIME_CAPABILITIES_REQUIRED")
    return inventory


def _lane_contracts(root: Path, inventory: ModelInventory, roles: EngineRoleConfiguration, policies: EvaluationPolicies):
    role_catalog = _combined_role_catalog(root)
    role_contract = _role_contract(root, role_catalog, inventory, roles, policies)
    planning_catalog = PlanningScenarioCatalog.load(
        root / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
    )
    planning_contract = _planning_contract(root, planning_catalog, inventory, roles, policies)
    e2e_source_digest = _project_e2e_fixture_source_digest(root)
    e2e_contract = _build_e2e_contract(root, inventory, roles, e2e_source_digest, policies, None)
    return role_contract, planning_contract, e2e_contract


_RUN_ROOT_POLICY = (
    "명시 절대경로이며 source root 밖이다. 기본값 패턴은 "
    "<root>/.flowmarshal-engine-eval/runs/<scope>-<UTC timestamp>-<contract digest hint>이고, "
    "shard 실행은 이 policy를 각자의 격리 artifact root로 대체해야 한다."
)


def _lane_lock(lane: str, contract, evidence_record_formats: tuple[str, ...]) -> LaneLock:
    return LaneLock(
        lane=lane,
        evaluation_contract_digest=contract.contract_digest,
        fixture_digests=contract.fixture_digests,
        scenario_set_digest=contract.scenario_set_digest,
        order_seeds=contract.order_seeds,
        expected_cell_count=contract.expected_cell_count,
        prompt_digest=contract.prompt_digest,
        output_schema_digest=contract.output_schema_digest,
        threshold_digest=contract.threshold_digest,
        taxonomy_digest=contract.taxonomy_digest,
        evidence_record_formats=evidence_record_formats,
        run_root_policy=_RUN_ROOT_POLICY,
    )


def _write_immutable_json(path: Path, payload: str) -> None:
    encoded = payload.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ReleaseFreezeError(f"RELEASE_FREEZE_IMMUTABLE_OVERWRITE:{path}")
        return
    path.write_bytes(encoded)


def _canonical_document(document: Any) -> str:
    import json

    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


# --------------------------------------------------------------------------
# 병렬 shard 격리 preflight
# --------------------------------------------------------------------------


def _normalize_path(value: str) -> str:
    return str(Path(value).resolve())


def _contains(parent: str, child: str) -> bool:
    parent_path, child_path = Path(parent), Path(child)
    if parent_path == child_path:
        return True
    try:
        child_path.relative_to(parent_path)
        return True
    except ValueError:
        return False


def preflight_shard_isolation(plan: ShardIsolationPlan) -> IsolationPreflightReport:
    violations: list[str] = []
    source_root = _normalize_path(plan.source_root)
    aggregate_root = _normalize_path(plan.aggregate_join_root)

    shard_roots: dict[str, str] = {}
    for shard in plan.shards:
        key = f"{shard.lane}:{shard.shard_id}"
        shard_roots[key] = _normalize_path(shard.artifact_root)

    items = list(shard_roots.items())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (key_a, root_a), (key_b, root_b) = items[i], items[j]
            if _contains(root_a, root_b) or _contains(root_b, root_a):
                violations.append(f"ISOLATION_SHARD_ROOT_OVERLAP:{key_a}:{key_b}")

    for key, root in shard_roots.items():
        if _contains(aggregate_root, root) or _contains(root, aggregate_root):
            violations.append(f"ISOLATION_AGGREGATE_ROOT_OVERLAP:{key}")

    for label, root in (("aggregate", aggregate_root), *shard_roots.items()):
        if _contains(source_root, root):
            violations.append(f"ISOLATION_ROOT_INSIDE_SOURCE_ROOT:{label}")
        if any(part == ".flowmarshal-engine" for part in Path(root).parts):
            violations.append(f"ISOLATION_ROOT_INSIDE_PRODUCT_ARTIFACT_ROOT:{label}")

    for sqlite_path in plan.shared_sqlite_paths:
        normalized = _normalize_path(sqlite_path)
        for key, root in shard_roots.items():
            if _contains(root, normalized):
                violations.append(f"ISOLATION_SHARED_SQLITE_INSIDE_SHARD_ROOT:{key}")

    before = source_manifest_digest_value = sha256_digest(source_manifest_files(Path(plan.source_root)))
    after = before  # preflight 시점에는 이전/이후 사이 어떤 쓰기도 발생하지 않는다.

    return IsolationPreflightReport(
        passed=not violations,
        violations=tuple(violations),
        source_manifest_digest_before=before,
        source_manifest_digest_after=after,
        plan_digest=sha256_digest(plan),
    )


# --------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------


def build_release_freeze(
    *,
    source_root: Path | str,
    destination: Path | str,
    candidate_wheel: Path | str,
    built_from_commit: str,
    inventory_path: Path | str,
    shard_plan: ShardIsolationPlan,
    role_configuration: EngineRoleConfiguration | None = None,
    evaluation_policies: EvaluationPolicies | None = None,
    governance_settings: Any | None = None,
    allow_dirty_rehearsal: bool = False,
) -> ReleaseFreezeManifest:
    root = Path(source_root).resolve(strict=True)
    wheel_path = Path(candidate_wheel)
    if not wheel_path.is_absolute():
        raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_ABSOLUTE_PATH_REQUIRED")
    wheel_path = wheel_path.resolve(strict=True)
    destination_root = Path(destination).resolve()

    commit = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseFreezeError("RELEASE_FREEZE_GIT_HEAD_INVALID")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(root, "status", "--porcelain")
    clean = status == ""
    if not clean and not allow_dirty_rehearsal:
        raise ReleaseFreezeError("RELEASE_FREEZE_DIRTY_WORKTREE")
    if not re.fullmatch(r"[0-9a-f]{40}", built_from_commit):
        raise ReleaseFreezeError("RELEASE_FREEZE_BUILT_FROM_COMMIT_INVALID")
    if not _is_ancestor(root, built_from_commit, commit):
        raise ReleaseFreezeError("RELEASE_FREEZE_BUILT_FROM_COMMIT_NOT_ANCESTOR")

    files = source_manifest_files(root)
    source = SourceFreeze(
        commit=commit, branch=branch, clean=clean,
        source_manifest_digest=sha256_digest(files),
        source_manifest_file_count=len(files),
    )

    (
        wheel_bytes, package_file_digests, distribution_name,
        distribution_version, entry_points_text, python_tag,
    ) = _read_wheel(wheel_path)
    wheel_package_digest = sha256_digest(package_file_digests)
    diffs = _diff_source_product_bytes(root, package_file_digests)
    if diffs:
        raise ReleaseFreezeError("RELEASE_FREEZE_WHEEL_SOURCE_MISMATCH:" + ";".join(diffs))

    candidate_wheel_freeze = CandidateWheelFreeze(
        wheel_path=str(wheel_path),
        wheel_digest=sha256_bytes(wheel_bytes),
        wheel_size_bytes=len(wheel_bytes),
        distribution_name=distribution_name,
        distribution_version=distribution_version,
        entry_points_text=entry_points_text,
        python_tag=python_tag,
        package_file_digests=package_file_digests,
        wheel_package_digest=wheel_package_digest,
        built_from_commit=built_from_commit,
    )

    package_identity = _package_identity(root, distribution_name, distribution_version, entry_points_text)

    config_file_digests = {name: sha256_bytes((root / name).read_bytes()) for name in CONFIG_FILES}
    fixtures_tree_digest = _tree_digest(root / "tests" / "fixtures" / "engine")
    suite = qualification_suite_manifest(root)
    roles = role_configuration or default_role_configuration(root)
    policies = evaluation_policies or _default_policies(root)
    evaluator_file_digests = {name: sha256_bytes((root / name).read_bytes()) for name in EVALUATOR_FILES}

    inventory = _load_inventory_for_freeze(Path(inventory_path).resolve(strict=True))
    binding = roles.operational_binding(inventory)

    role_contract, planning_contract, e2e_contract = _lane_contracts(root, inventory, roles, policies)

    inputs = InputsFreeze(
        config_file_digests=config_file_digests,
        fixtures_tree_digest=fixtures_tree_digest,
        suite_manifest_digest=suite.manifest_digest,
        roles_config_digest=roles.configuration_digest,
        evaluator_file_digests=evaluator_file_digests,
        role_prompt_digest=role_contract.prompt_digest,
        role_schema_digest=role_contract.output_schema_digest,
        planning_prompt_digest=planning_contract.prompt_digest,
        planning_schema_digest=planning_contract.output_schema_digest,
        e2e_prompt_digest=e2e_contract.prompt_digest,
        e2e_schema_digest=e2e_contract.output_schema_digest,
    )

    inventory_relative = f"inventory-observation-{inventory.inventory_digest.split(':', 1)[1]}.json"
    model_inventory = ModelInventoryFreeze(
        inventory_digest=inventory.inventory_digest,
        provider_inventory_digest=inventory.provider_inventory_digest,
        adapter_capability_digest=inventory.adapter_capability_digest,
        executable_digest=inventory.executable_digest,
        selected_fallback_projection=binding.lock.model_dump(mode="json"),
        selected_fallback_projection_digest=binding.lock_digest,
        inventory_observation_relative_path=inventory_relative,
    )

    lane_locks = (
        _lane_lock("role", role_contract, ("flowmarshal.qualification-evidence.v1", "QualificationCellOutcome")),
        _lane_lock("planning", planning_contract, ("flowmarshal.qualification-evidence.v1", "QualificationCellOutcome")),
        _lane_lock(
            "e2e", e2e_contract,
            (
                "flowmarshal.qualification-evidence.v1",
                "QualificationCellOutcome",
                "flowmarshal.clean-install-qualification.v1",
                "flowmarshal.candidate-wheel-binding.v1",
            ),
        ),
    )

    isolation_report = preflight_shard_isolation(shard_plan)
    if not isolation_report.passed:
        raise ReleaseFreezeError(
            "RELEASE_FREEZE_ISOLATION_PREFLIGHT_FAILED:" + ";".join(isolation_report.violations)
        )

    if governance_settings is None:
        from .governance_gate import GovernanceSettings

        governance_settings = GovernanceSettings.from_environment(destination_root / "governance")
    if governance_settings is None:
        raise ReleaseFreezeError("RELEASE_FREEZE_GOVERNANCE_SETTINGS_REQUIRED")
    try:
        conformance = governance_settings.check_conformance()
    except Exception as error:
        raise ReleaseFreezeError(f"RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_FAILED:{error}") from error
    if conformance.get("verdict") != "PASS":
        failed = next((item for item in conformance.get("checks", ()) if item.get("status") != "PASS"), None)
        raise ReleaseFreezeError(f"RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_NOT_PASS:{failed}")
    identity = conformance.get("identity", {})
    labels = conformance.get("identity_labels", ())
    manifest_label = next((item.get("plugin") for item in labels
                           if item.get("source") == "plugin_manifest_file"), None)
    server_label = next((item.get("serverInfo") for item in labels
                         if item.get("source") == "mcp_server_info"), None)
    conformance_document = _canonical_document(conformance)
    conformance_digest = sha256_bytes(conformance_document.encode("utf-8"))
    e2e_fields = {
        "closure_tree_digest": identity.get("closure_tree_digest"),
        "entrypoint_table_digest": identity.get("entrypoint_table_digest"),
        "check_set_digest": conformance.get("check_set_digest"),
        "conformance_result_digest": conformance_digest,
    }
    try:
        governance_plugin = GovernancePluginFreeze(
            manifest_sha256=identity.get("manifest_sha256"),
            closure_tree_digest=identity.get("closure_tree_digest"),
            closure_file_digests=conformance.get("identity_files", {}),
            entrypoint_table_digest=identity.get("entrypoint_table_digest"),
            node_version=identity.get("node_version"),
            consumed_surface_digest=identity.get("consumed_surface_digest"),
            check_set_digest=conformance.get("check_set_digest"),
            conformance_result_digest=conformance_digest,
            plugin_version_label=manifest_label.get("version") if isinstance(manifest_label, dict) else None,
            server_info=server_label if isinstance(server_label, dict) else None,
            installation_root=conformance.get("installation_root"),
            e2e_identity_digest=sha256_digest(e2e_fields),
        )
    except (TypeError, ValueError) as error:
        raise ReleaseFreezeError(f"RELEASE_FREEZE_GOVERNANCE_IDENTITY_INVALID:{error}") from error

    refreeze_policy = RefreezePolicy()

    body: dict[str, Any] = {
        "format": FORMAT,
        "rehearsal": bool(allow_dirty_rehearsal),
        "source": source.model_dump(mode="json"),
        "candidate_wheel": candidate_wheel_freeze.model_dump(mode="json"),
        "package_identity": package_identity.model_dump(mode="json"),
        "inputs": inputs.model_dump(mode="json"),
        "model_inventory": model_inventory.model_dump(mode="json"),
        "governance_plugin": governance_plugin.model_dump(mode="json"),
        "lane_locks": [item.model_dump(mode="json") for item in lane_locks],
        "isolation_preflight": isolation_report.model_dump(mode="json"),
        "refreeze_policy": refreeze_policy.model_dump(mode="json"),
    }
    manifest = ReleaseFreezeManifest.model_validate(body | {"freeze_digest": sha256_digest(body)})

    _write_immutable_json(
        destination_root / governance_plugin.conformance_result_relative_path,
        conformance_document,
    )
    _write_immutable_json(destination_root / "release-freeze.json", _canonical_document(manifest.model_dump(mode="json")))
    _write_immutable_json(
        destination_root / "isolation-preflight.json", _canonical_document(isolation_report.model_dump(mode="json"))
    )
    _write_immutable_json(
        destination_root / inventory_relative, _canonical_document(inventory.model_dump(mode="json"))
    )
    return manifest


def verify_release_freeze(
    destination: Path | str,
    *,
    source_root: Path | str,
    candidate_wheel: Path | str | None = None,
    role_configuration: EngineRoleConfiguration | None = None,
    evaluation_policies: EvaluationPolicies | None = None,
    allow_rehearsal: bool = False,
) -> ReleaseFreezeVerification:
    root = Path(source_root).resolve(strict=True)
    dest = Path(destination).resolve(strict=True)
    manifest = ReleaseFreezeManifest.model_validate_json(
        (dest / "release-freeze.json").read_text(encoding="utf-8")
    )
    preflight_echo = IsolationPreflightReport.model_validate_json(
        (dest / "isolation-preflight.json").read_text(encoding="utf-8")
    )

    mismatches: list[str] = []
    audit_only: list[str] = []

    # dirty worktree에서 만든 rehearsal freeze는 release 결속으로 쓸 수 없다.
    if manifest.rehearsal and not allow_rehearsal:
        mismatches.append("RELEASE_FREEZE_REHEARSAL_MANIFEST")

    if preflight_echo != manifest.isolation_preflight:
        mismatches.append("RELEASE_FREEZE_ISOLATION_ECHO_MISMATCH")

    try:
        governance_bytes = (dest / manifest.governance_plugin.conformance_result_relative_path).read_bytes()
        governance_echo = json.loads(governance_bytes)
    except (OSError, ValueError):
        governance_echo = None
        mismatches.append("RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_MISSING")
    if governance_echo is not None:
        if sha256_bytes(governance_bytes) != manifest.governance_plugin.conformance_result_digest:
            mismatches.append("RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_DIGEST_MISMATCH")
        if governance_echo.get("verdict") != "PASS":
            mismatches.append("RELEASE_FREEZE_GOVERNANCE_CONFORMANCE_NOT_PASS")
        echo_identity = governance_echo.get("identity", {})
        echo_labels = governance_echo.get("identity_labels", ())
        if isinstance(echo_labels, list):
            manifest_label = next(
                (
                    item.get("plugin")
                    for item in echo_labels
                    if isinstance(item, dict)
                    and item.get("source") == "plugin_manifest_file"
                ),
                None,
            )
            server_label = next(
                (
                    item.get("serverInfo")
                    for item in echo_labels
                    if isinstance(item, dict) and item.get("source") == "mcp_server_info"
                ),
                None,
            )
        else:
            manifest_label = None
            server_label = None
        echo_fields = {
            "manifest_sha256": echo_identity.get("manifest_sha256"),
            "closure_tree_digest": echo_identity.get("closure_tree_digest"),
            "entrypoint_table_digest": echo_identity.get("entrypoint_table_digest"),
            "node_version": echo_identity.get("node_version"),
            "consumed_surface_digest": echo_identity.get("consumed_surface_digest"),
            "check_set_digest": governance_echo.get("check_set_digest"),
            "closure_file_digests": governance_echo.get("identity_files", {}),
            "installation_root": governance_echo.get("installation_root"),
            "plugin_version_label": (
                manifest_label.get("version") if isinstance(manifest_label, dict) else None
            ),
            "server_info": server_label if isinstance(server_label, dict) else None,
        }
        frozen_fields = {
            key: getattr(manifest.governance_plugin, key) for key in echo_fields
        }
        if echo_fields != frozen_fields:
            mismatches.append("RELEASE_FREEZE_GOVERNANCE_BLOCK_MISMATCH")

    current_files = source_manifest_files(root)
    if (
        sha256_digest(current_files) != manifest.source.source_manifest_digest
        or len(current_files) != manifest.source.source_manifest_file_count
    ):
        mismatches.append("RELEASE_FREEZE_SOURCE_MISMATCH")

    if candidate_wheel is not None:
        wheel_path = Path(candidate_wheel).resolve(strict=True)
        wheel_bytes, package_file_digests, *_ = _read_wheel(wheel_path)
        if sha256_bytes(wheel_bytes) != manifest.candidate_wheel.wheel_digest:
            mismatches.append("RELEASE_FREEZE_WHEEL_MISMATCH")
        if sha256_digest(package_file_digests) != manifest.candidate_wheel.wheel_package_digest:
            mismatches.append("RELEASE_FREEZE_WHEEL_PACKAGE_MISMATCH")

    try:
        config_file_digests = {name: sha256_bytes((root / name).read_bytes()) for name in CONFIG_FILES}
    except OSError:
        config_file_digests = {}
        mismatches.append("RELEASE_FREEZE_CONFIG_FILE_MISSING")
    if config_file_digests != manifest.inputs.config_file_digests:
        mismatches.append("RELEASE_FREEZE_CONFIG_DIGEST_MISMATCH")

    if _tree_digest(root / "tests" / "fixtures" / "engine") != manifest.inputs.fixtures_tree_digest:
        mismatches.append("RELEASE_FREEZE_FIXTURES_DIGEST_MISMATCH")

    suite = qualification_suite_manifest(root)
    if suite.manifest_digest != manifest.inputs.suite_manifest_digest:
        mismatches.append("RELEASE_FREEZE_SUITE_MANIFEST_MISMATCH")

    roles = role_configuration or default_role_configuration(root)
    if roles.configuration_digest != manifest.inputs.roles_config_digest:
        mismatches.append("RELEASE_FREEZE_ROLES_CONFIG_MISMATCH")

    try:
        evaluator_file_digests = {name: sha256_bytes((root / name).read_bytes()) for name in EVALUATOR_FILES}
    except OSError:
        evaluator_file_digests = {}
        mismatches.append("RELEASE_FREEZE_EVALUATOR_FILE_MISSING")
    if evaluator_file_digests != manifest.inputs.evaluator_file_digests:
        mismatches.append("RELEASE_FREEZE_EVALUATOR_DIGEST_MISMATCH")

    try:
        from .cli import build_parser

        surface = _parser_surface(build_parser())
        if sha256_digest(surface) != manifest.package_identity.parser_surface_digest:
            mismatches.append("RELEASE_FREEZE_PACKAGE_IDENTITY_MISMATCH")
    except Exception:  # noqa: BLE001 - CLI 표면 재구성 실패도 명시 mismatch로 보존한다.
        mismatches.append("RELEASE_FREEZE_PACKAGE_IDENTITY_RECOMPUTE_FAILED")

    inventory_path = dest / manifest.model_inventory.inventory_observation_relative_path
    try:
        inventory = _load_inventory_for_freeze(inventory_path)
        if (
            inventory.inventory_digest != manifest.model_inventory.inventory_digest
            or inventory.executable_digest != manifest.model_inventory.executable_digest
            or inventory.adapter_capability_digest != manifest.model_inventory.adapter_capability_digest
        ):
            mismatches.append("RELEASE_FREEZE_INVENTORY_OBSERVATION_MISMATCH")
        if roles.operational_binding(inventory).lock_digest != (
            manifest.model_inventory.selected_fallback_projection_digest
        ):
            mismatches.append("RELEASE_FREEZE_SELECTED_FALLBACK_PROJECTION_MISMATCH")
        policies = evaluation_policies or _default_policies(root)
        role_contract, planning_contract, e2e_contract = _lane_contracts(root, inventory, roles, policies)
        recomputed = {
            "role": role_contract.contract_digest,
            "planning": planning_contract.contract_digest,
            "e2e": e2e_contract.contract_digest,
        }
        for lane_lock in manifest.lane_locks:
            if recomputed[lane_lock.lane] != lane_lock.evaluation_contract_digest:
                mismatches.append(f"RELEASE_FREEZE_LANE_LOCK_MISMATCH:{lane_lock.lane}")
    except (OSError, ValueError, ReleaseFreezeError) as error:
        mismatches.append(f"RELEASE_FREEZE_LANE_LOCK_RECOMPUTE_FAILED:{type(error).__name__}")

    audit_only_prefixes = ("RELEASE_FREEZE_ISOLATION_ECHO_MISMATCH",)
    refreeze_required = any(code not in audit_only_prefixes for code in mismatches)
    return ReleaseFreezeVerification(
        valid=not mismatches,
        refreeze_required=refreeze_required,
        mismatches=tuple(mismatches),
        audit_only_changes=tuple(audit_only),
        manifest=manifest,
    )


def classify_inventory_change(
    *,
    previous: ModelInventory,
    candidate: ModelInventory,
    roles: EngineRoleConfiguration,
    policy: RefreezePolicy | None = None,
) -> InventoryChangeClassification:
    """새 model inventory 관측이 재동결을 요구하는지, audit-only인지 판정한다."""

    active_policy = policy or RefreezePolicy()
    changed: list[str] = []
    if previous.provider_inventory_digest != candidate.provider_inventory_digest:
        changed.append("provider_inventory_digest")
    if previous.adapter_capability_digest != candidate.adapter_capability_digest:
        changed.append("adapter_capability_digest")
    if previous.executable_digest != candidate.executable_digest:
        changed.append("executable_digest")
    previous_lock_digest = roles.operational_binding(previous).lock_digest
    candidate_lock_digest = roles.operational_binding(candidate).lock_digest
    if previous_lock_digest != candidate_lock_digest:
        changed.append("selected_fallback_projection_digest")

    refreeze_required = any(field in active_policy.refreeze_required_fields for field in changed)
    audit_only_fields = tuple(field for field in changed if field not in active_policy.refreeze_required_fields)
    return InventoryChangeClassification(
        refreeze_required=refreeze_required,
        changed_fields=tuple(changed),
        audit_only_fields=audit_only_fields,
    )
