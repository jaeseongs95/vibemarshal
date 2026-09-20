"""1.0 qualification 입력 동결과 책임별 판정 계약.

이 모듈은 모델이나 harness의 성공 선언을 신뢰하지 않는다. 평가 입력의 각 축과
실행 provenance를 별도로 결속하고, 필수 책임별 직접 evidence가 모두 있을 때만
qualification 책임 집합을 통과시킨다.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from email.parser import BytesParser
from enum import StrEnum
from importlib import metadata as importlib_metadata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .domain import EngineModel


class QualificationManifestError(RuntimeError):
    """동결 입력 또는 qualification 결과의 결속이 깨진 경우."""


class EvidenceProvenance(StrEnum):
    LIVE = "live"
    FAKE = "fake"
    FAULT_INJECTED = "fault_injected"
    REUSED = "reused"


class QualificationCellStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


class QualificationFailureClass(StrEnum):
    PRODUCT = "product"
    MODEL = "model"
    ENVIRONMENT = "environment"
    FIXTURE = "fixture"
    CONTRACT = "contract"


class RoleGateThresholds(EngineModel):
    fixture_count: int = Field(ge=1)
    order_seeds: tuple[int, ...] = Field(min_length=1)
    expected_cell_count: int = Field(ge=1)
    required_finding_recall: float = Field(ge=0, le=1)
    finding_precision: float = Field(ge=0, le=1)
    critical_false_admission_count: int = Field(ge=0)
    clean_false_block_count: int = Field(ge=0)
    schema_failure_count: int = Field(ge=0)
    critical_admission_seed_instability_count: int = Field(ge=0)


class PlanningGateThresholds(EngineModel):
    scenario_count: int = Field(ge=1)
    order_seeds: tuple[int, ...] = Field(min_length=1)
    expected_cell_count: int = Field(ge=1)
    selected_scenario_count: int = Field(ge=0)
    blocking_question_scenario_count: int = Field(ge=0)
    max_role_calls: int = Field(ge=1)
    max_candidate_versions: int = Field(ge=1)
    selected_deterministic_finding_count: int = Field(ge=0)


class NonBlockingComparisonPolicy(EngineModel):
    suites: tuple[str, ...] = Field(min_length=1)
    release_blocking: bool
    missing_value_semantics: str = Field(min_length=1)


class E2EResponsibility(EngineModel):
    responsibility_id: str = Field(pattern=r"^E2E-[0-9]{2}$")
    name: str = Field(min_length=1)
    fixture_id: str = Field(min_length=1)
    expected_semantics: tuple[str, ...] = Field(min_length=1)
    required_pipeline_stages: tuple[str, ...] = Field(min_length=1)
    allowed_provenance: tuple[EvidenceProvenance, ...] = Field(min_length=1)
    required_provenance: tuple[EvidenceProvenance, ...] = Field(default=())
    required_evidence_kinds: tuple[str, ...] = Field(min_length=1)
    raw_request_pipeline_required: bool = True

    @model_validator(mode="after")
    def provenance_contract_is_consistent(self) -> "E2EResponsibility":
        if len(self.allowed_provenance) != len(set(self.allowed_provenance)):
            raise ValueError("allowed provenance가 중복됐습니다.")
        if len(self.required_provenance) != len(set(self.required_provenance)):
            raise ValueError("required provenance가 중복됐습니다.")
        if not set(self.required_provenance) <= set(self.allowed_provenance):
            raise ValueError("required provenance는 allowed provenance에 포함돼야 합니다.")
        return self


class QualificationSuiteManifest(EngineModel):
    schema_version: int = Field(ge=1)
    role_gate: RoleGateThresholds
    planning_gate: PlanningGateThresholds
    e2e_responsibilities: tuple[E2EResponsibility, ...] = Field(min_length=1)
    non_blocking_comparisons: NonBlockingComparisonPolicy

    @model_validator(mode="after")
    def approved_thresholds_and_responsibilities_are_preserved(
        self,
    ) -> "QualificationSuiteManifest":
        role = self.role_gate
        if (
            role.fixture_count != 16
            or role.order_seeds != (17, 43, 89)
            or role.expected_cell_count != 48
            or role.required_finding_recall != 0.90
            or role.finding_precision != 0.85
            or any(
                value != 0
                for value in (
                    role.critical_false_admission_count,
                    role.clean_false_block_count,
                    role.schema_failure_count,
                    role.critical_admission_seed_instability_count,
                )
            )
        ):
            raise ValueError("승인된 실제 역할 48회 threshold가 변경됐습니다.")
        planning = self.planning_gate
        if (
            planning.scenario_count != 6
            or planning.order_seeds != (17, 43, 89)
            or planning.expected_cell_count != 18
            or planning.selected_scenario_count != 4
            or planning.blocking_question_scenario_count != 2
            or planning.max_role_calls != 14
            or planning.max_candidate_versions != 5
            or planning.selected_deterministic_finding_count != 0
        ):
            raise ValueError("승인된 실제 Planning 18회 의미 기준이 변경됐습니다.")
        ids = tuple(item.responsibility_id for item in self.e2e_responsibilities)
        fixtures = tuple(item.fixture_id for item in self.e2e_responsibilities)
        if len(ids) != len(set(ids)) or len(fixtures) != len(set(fixtures)):
            raise ValueError("E2E responsibility 또는 fixture ID가 중복됐습니다.")
        if self.non_blocking_comparisons.release_blocking:
            raise ValueError("성능/R3.1/lifecycle 비교를 필수 release gate로 만들 수 없습니다.")
        return self

    @property
    def manifest_digest(self) -> str:
        return sha256_digest(self)

    @classmethod
    def load(cls, path: Path | str) -> "QualificationSuiteManifest":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class QualificationFreezeManifest(EngineModel):
    format: str = "flowmarshal.qualification-freeze.v1"
    suite_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    threshold_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    taxonomy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluator_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    file_digests: dict[str, str] = Field(min_length=1)
    bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bundle_digest_matches_body(self) -> "QualificationFreezeManifest":
        expected = sha256_digest(
            self.model_dump(mode="json", exclude={"bundle_digest"})
        )
        if self.bundle_digest != expected:
            raise ValueError("qualification bundle digest가 manifest 본문과 다릅니다.")
        return self


class CandidateWheelBinding(EngineModel):
    """release E2E가 실제 non-editable 설치와 결속한 candidate wheel이다."""

    format: Literal["flowmarshal.candidate-wheel-binding.v1"] = (
        "flowmarshal.candidate-wheel-binding.v1"
    )
    wheel_path: str = Field(min_length=1)
    wheel_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    distribution_name: str = Field(min_length=1)
    distribution_version: str = Field(min_length=1)
    distribution_root: str = Field(min_length=1)
    import_root: str = Field(min_length=1)
    editable: Literal[False] = False
    package_file_digests: dict[str, str] = Field(min_length=1)
    wheel_package_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    installed_package_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("wheel_path", "distribution_root", "import_root")
    @classmethod
    def paths_are_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("candidate wheel binding 경로는 절대경로여야 합니다.")
        return value

    @model_validator(mode="after")
    def package_digest_matches_files(self) -> "CandidateWheelBinding":
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
        expected = sha256_digest(self.package_file_digests)
        if (
            self.wheel_package_digest != expected
            or self.installed_package_digest != expected
        ):
            raise ValueError("candidate package 집계 digest가 파일 장부와 다릅니다.")
        return self

    @property
    def binding_digest(self) -> str:
        return sha256_digest(self)


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def verify_candidate_wheel_installation(
    candidate_wheel: Path | str,
    *,
    _distribution: importlib_metadata.Distribution | None = None,
    _import_root: Path | str | None = None,
) -> CandidateWheelBinding:
    """wheel, 현재 import, 설치된 distribution의 동일 바이트 결속을 검사한다.

    밑줄 인자는 파일시스템 fixture로 설치 검증기를 시험하기 위한 주입점이며 release
    호출자는 사용하지 않는다.
    """

    supplied = Path(candidate_wheel)
    if not supplied.is_absolute():
        raise QualificationManifestError("CANDIDATE_WHEEL_ABSOLUTE_PATH_REQUIRED")
    try:
        wheel = supplied.resolve(strict=True)
    except OSError as error:
        raise QualificationManifestError("CANDIDATE_WHEEL_NOT_FOUND") from error
    if not wheel.is_file() or wheel.suffix.casefold() != ".whl":
        raise QualificationManifestError("CANDIDATE_WHEEL_FILE_REQUIRED")

    try:
        wheel_bytes = wheel.read_bytes()
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            infos = archive.infolist()
            names = tuple(item.filename for item in infos)
            if len(names) != len(set(names)):
                raise QualificationManifestError("CANDIDATE_WHEEL_DUPLICATE_PATH")
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                    raise QualificationManifestError("CANDIDATE_WHEEL_PATH_ESCAPE")
            metadata_names = tuple(
                name
                for name in names
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            )
            if len(metadata_names) != 1:
                raise QualificationManifestError("CANDIDATE_WHEEL_METADATA_INVALID")
            message = BytesParser().parsebytes(archive.read(metadata_names[0]))
            distribution_name = str(message.get("Name") or "").strip()
            distribution_version = str(message.get("Version") or "").strip()
            if not distribution_name or not distribution_version:
                raise QualificationManifestError("CANDIDATE_WHEEL_METADATA_INVALID")
            package_file_digests = {
                name: sha256_bytes(archive.read(name))
                for name in names
                if name.startswith("flowmarshal/") and not name.endswith("/")
            }
    except (OSError, zipfile.BadZipFile) as error:
        raise QualificationManifestError("CANDIDATE_WHEEL_ARCHIVE_INVALID") from error
    if "flowmarshal/__init__.py" not in package_file_digests:
        raise QualificationManifestError("CANDIDATE_WHEEL_FLOWMARSHAL_PACKAGE_MISSING")

    try:
        distribution = _distribution or importlib_metadata.distribution(
            distribution_name
        )
    except importlib_metadata.PackageNotFoundError as error:
        raise QualificationManifestError("CANDIDATE_DISTRIBUTION_NOT_INSTALLED") from error
    installed_name = str(distribution.metadata.get("Name") or "").strip()
    installed_version = str(distribution.version or "").strip()
    if (
        _normalized_distribution_name(installed_name)
        != _normalized_distribution_name(distribution_name)
        or installed_version != distribution_version
    ):
        raise QualificationManifestError("CANDIDATE_DISTRIBUTION_IDENTITY_MISMATCH")
    try:
        direct_url_text = distribution.read_text("direct_url.json")
        direct_url = None if direct_url_text is None else json.loads(direct_url_text)
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationManifestError("CANDIDATE_DISTRIBUTION_DIRECT_URL_INVALID") from error
    if direct_url is not None and not isinstance(direct_url, dict):
        raise QualificationManifestError("CANDIDATE_DISTRIBUTION_DIRECT_URL_INVALID")
    if isinstance(direct_url, dict):
        dir_info = direct_url.get("dir_info")
        if dir_info is not None and not isinstance(dir_info, dict):
            raise QualificationManifestError(
                "CANDIDATE_DISTRIBUTION_DIRECT_URL_INVALID"
            )
        if isinstance(dir_info, dict) and bool(dir_info.get("editable")):
            raise QualificationManifestError("CANDIDATE_DISTRIBUTION_EDITABLE")

    try:
        distribution_root = Path(distribution.locate_file("")).resolve(strict=True)
        import_root = Path(
            _import_root if _import_root is not None else Path(__file__).parents[1]
        ).resolve(strict=True)
        import_root.relative_to(distribution_root)
    except (OSError, ValueError) as error:
        raise QualificationManifestError("CANDIDATE_IMPORT_OUTSIDE_DISTRIBUTION") from error
    expected_import_root = (distribution_root / "flowmarshal").resolve(strict=True)
    if import_root != expected_import_root:
        raise QualificationManifestError("CANDIDATE_IMPORT_ROOT_MISMATCH")

    distribution_files = distribution.files
    if distribution_files is None:
        raise QualificationManifestError("CANDIDATE_DISTRIBUTION_FILESET_MISSING")
    installed_package_files = {
        PurePosixPath(str(item).replace("\\", "/")).as_posix()
        for item in distribution_files
        if str(item).replace("\\", "/").startswith("flowmarshal/")
        and "__pycache__" not in PurePosixPath(
            str(item).replace("\\", "/")
        ).parts
        and not str(item).casefold().endswith((".pyc", ".pyo"))
    }
    if installed_package_files != set(package_file_digests):
        raise QualificationManifestError(
            "CANDIDATE_INSTALLED_PACKAGE_FILESET_MISMATCH"
        )

    installed_file_digests: dict[str, str] = {}
    for relative, expected_digest in package_file_digests.items():
        try:
            installed = (distribution_root / relative).resolve(strict=True)
            installed.relative_to(distribution_root)
        except (OSError, ValueError) as error:
            raise QualificationManifestError(
                f"CANDIDATE_INSTALLED_FILE_MISSING:{relative}"
            ) from error
        if not installed.is_file():
            raise QualificationManifestError(
                f"CANDIDATE_INSTALLED_FILE_INVALID:{relative}"
            )
        actual_digest = sha256_bytes(installed.read_bytes())
        if actual_digest != expected_digest:
            raise QualificationManifestError(
                f"CANDIDATE_INSTALLED_FILE_DIGEST_MISMATCH:{relative}"
            )
        installed_file_digests[relative] = actual_digest
    wheel_package_digest = sha256_digest(package_file_digests)
    installed_package_digest = sha256_digest(installed_file_digests)
    if wheel_package_digest != installed_package_digest:
        raise QualificationManifestError("CANDIDATE_INSTALLED_PACKAGE_DIGEST_MISMATCH")
    return CandidateWheelBinding(
        wheel_path=str(wheel),
        wheel_digest=sha256_bytes(wheel_bytes),
        distribution_name=distribution_name,
        distribution_version=distribution_version,
        distribution_root=str(distribution_root),
        import_root=str(import_root),
        package_file_digests=package_file_digests,
        wheel_package_digest=wheel_package_digest,
        installed_package_digest=installed_package_digest,
    )


def verify_candidate_wheel_metadata(metadata: Mapping[str, Any]) -> CandidateWheelBinding:
    """immutable run metadata의 wheel binding을 현재 파일·설치와 다시 대조한다."""

    try:
        saved = CandidateWheelBinding.model_validate(metadata["candidate_wheel_binding"])
    except (KeyError, ValueError) as error:
        raise QualificationManifestError("CANDIDATE_WHEEL_BINDING_MISSING") from error
    if metadata.get("candidate_wheel_binding_digest") != saved.binding_digest:
        raise QualificationManifestError("CANDIDATE_WHEEL_BINDING_DIGEST_MISMATCH")
    current = verify_candidate_wheel_installation(saved.wheel_path)
    if current != saved:
        raise QualificationManifestError("CANDIDATE_WHEEL_BINDING_CHANGED")
    return saved


class QualificationEvidenceRecord(EngineModel):
    """1.0 qualification PASS에 사용할 바이트·cell 결속 evidence다.

    ``evidence_refs`` 문자열만 저장한 pre-1.0 checkpoint는 계속 읽을 수 있지만,
    이 record 없이 새 release PASS의 직접 근거로 승격하지 않는다.
    """

    format: Literal["flowmarshal.qualification-evidence.v1"] = (
        "flowmarshal.qualification-evidence.v1"
    )
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    kind: str = Field(min_length=1)
    cell_id: str = Field(min_length=1)
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    order_seed: int = Field(ge=0)
    freeze_bundle_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    governance_plugin_identity_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    candidate_wheel_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    candidate_wheel_binding_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    candidate_distribution_name: str | None = Field(default=None, min_length=1)
    candidate_distribution_version: str | None = Field(default=None, min_length=1)

    @field_validator("relative_path")
    @classmethod
    def path_is_portable_and_relative(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("qualification evidence 경로는 POSIX 상대경로여야 합니다.")
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if posix.is_absolute() or windows.is_absolute() or windows.drive:
            raise ValueError("qualification evidence 경로는 run root 상대경로여야 합니다.")
        if value in {"", "."} or any(part in {"", ".", ".."} for part in posix.parts):
            raise ValueError("qualification evidence 경로에 빈/상위 경로를 쓸 수 없습니다.")
        return value


class QualificationCellOutcome(EngineModel):
    cell_id: str = Field(min_length=1)
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    responsibility_ids: tuple[str, ...] = Field(min_length=1)
    status: QualificationCellStatus
    provenance: tuple[EvidenceProvenance, ...] = Field(min_length=1)
    pipeline_stages: tuple[str, ...] = ()
    evidence_kinds: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evidence_records: tuple[QualificationEvidenceRecord, ...] = ()
    failure_class: QualificationFailureClass | None = None
    failure_code: str | None = None
    reused_from_contract_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def outcome_shape_matches_status(self) -> "QualificationCellOutcome":
        if len(self.responsibility_ids) != len(set(self.responsibility_ids)):
            raise ValueError("cell responsibility가 중복됐습니다.")
        if len(self.provenance) != len(set(self.provenance)):
            raise ValueError("cell provenance가 중복됐습니다.")
        record_keys = tuple(
            (item.kind, item.relative_path) for item in self.evidence_records
        )
        if len(record_keys) != len(set(record_keys)):
            raise ValueError("qualification evidence kind/path 결속이 중복됐습니다.")
        if self.status is QualificationCellStatus.PASSED:
            if self.failure_class is not None or self.failure_code is not None:
                raise ValueError("PASS cell에는 failure 분류가 있으면 안 됩니다.")
        elif self.status is QualificationCellStatus.FAILED:
            if self.failure_class is None or not self.failure_code:
                raise ValueError("FAIL cell에는 failure class와 code가 필요합니다.")
        else:
            if self.failure_class is not None:
                raise ValueError("NOT_RUN은 실행 실패 분류와 구분해야 합니다.")
        if EvidenceProvenance.REUSED in self.provenance:
            if self.reused_from_contract_digest is None:
                raise ValueError("reused evidence에는 원 evaluation 계약 digest가 필요합니다.")
        elif self.reused_from_contract_digest is not None:
            raise ValueError("reused provenance 없이 reuse 계약을 지정할 수 없습니다.")
        return self


def build_qualification_evidence_record(
    *,
    run_root: Path | str,
    path: Path | str,
    kind: str,
    cell_id: str,
    evaluation_contract_digest: str,
    fixture_digest: str,
    order_seed: int,
    freeze_bundle_digest: str | None = None,
    governance_plugin_identity_digest: str | None = None,
    candidate_wheel_digest: str | None = None,
    candidate_wheel_binding_digest: str | None = None,
    candidate_distribution_name: str | None = None,
    candidate_distribution_version: str | None = None,
) -> QualificationEvidenceRecord:
    """run root 안의 현재 파일을 검증하고 typed evidence record를 만든다."""

    try:
        root = Path(run_root).resolve(strict=True)
        resolved = Path(path).resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise QualificationManifestError(
            f"qualification evidence가 run root 안의 기존 파일이 아닙니다: {path}"
        ) from error
    if not resolved.is_file():
        raise QualificationManifestError(
            f"qualification evidence가 일반 파일이 아닙니다: {path}"
        )
    return QualificationEvidenceRecord(
        relative_path=relative.as_posix(),
        sha256=sha256_bytes(resolved.read_bytes()),
        kind=kind,
        cell_id=cell_id,
        evaluation_contract_digest=evaluation_contract_digest,
        fixture_digest=fixture_digest,
        order_seed=order_seed,
        freeze_bundle_digest=freeze_bundle_digest,
        governance_plugin_identity_digest=governance_plugin_identity_digest,
        candidate_wheel_digest=candidate_wheel_digest,
        candidate_wheel_binding_digest=candidate_wheel_binding_digest,
        candidate_distribution_name=candidate_distribution_name,
        candidate_distribution_version=candidate_distribution_version,
    )


def verify_qualification_evidence_records(
    outcome: QualificationCellOutcome,
    *,
    run_root: Path | str,
    expected_fixture_digest: str,
    expected_order_seed: int,
    expected_freeze_bundle_digest: str | None = None,
    expected_governance_plugin_identity_digest: str | None = None,
    expected_candidate_wheel_digest: str | None = None,
    expected_candidate_wheel_binding_digest: str | None = None,
    expected_candidate_distribution_name: str | None = None,
    expected_candidate_distribution_version: str | None = None,
    required_evidence_kinds: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """저장된 typed evidence의 경로·바이트·cell 계약 결속을 fail-closed 검사한다."""

    failures: list[str] = []
    if not outcome.evidence_records:
        # pre-1.0 문자열 ref는 읽기 호환만 제공하고 release PASS 근거로 쓰지 않는다.
        return ("LEGACY_EVIDENCE_UNVERIFIED",)
    try:
        root = Path(run_root).resolve(strict=True)
    except OSError:
        return ("EVIDENCE_RUN_ROOT_INVALID",)

    verified_kinds: set[str] = set()
    for record in outcome.evidence_records:
        prefix = f"{record.kind}:{record.relative_path}"
        if record.cell_id != outcome.cell_id:
            failures.append(f"EVIDENCE_CELL_MISMATCH:{prefix}")
        if record.evaluation_contract_digest != outcome.evaluation_contract_digest:
            failures.append(f"EVIDENCE_CONTRACT_MISMATCH:{prefix}")
        if record.fixture_digest != expected_fixture_digest:
            failures.append(f"EVIDENCE_FIXTURE_MISMATCH:{prefix}")
        if record.order_seed != expected_order_seed:
            failures.append(f"EVIDENCE_ORDER_SEED_MISMATCH:{prefix}")
        if record.freeze_bundle_digest != expected_freeze_bundle_digest:
            failures.append(f"EVIDENCE_FREEZE_MISMATCH:{prefix}")
        if record.governance_plugin_identity_digest != expected_governance_plugin_identity_digest:
            failures.append(f"EVIDENCE_GOVERNANCE_PLUGIN_MISMATCH:{prefix}")
        if record.candidate_wheel_digest != expected_candidate_wheel_digest:
            failures.append(f"EVIDENCE_WHEEL_MISMATCH:{prefix}")
        if (
            record.candidate_wheel_binding_digest
            != expected_candidate_wheel_binding_digest
        ):
            failures.append(f"EVIDENCE_WHEEL_BINDING_MISMATCH:{prefix}")
        if record.candidate_distribution_name != expected_candidate_distribution_name:
            failures.append(f"EVIDENCE_DISTRIBUTION_NAME_MISMATCH:{prefix}")
        if (
            record.candidate_distribution_version
            != expected_candidate_distribution_version
        ):
            failures.append(f"EVIDENCE_DISTRIBUTION_VERSION_MISMATCH:{prefix}")
        try:
            candidate = (root / record.relative_path).resolve(strict=True)
            candidate.relative_to(root)
        except FileNotFoundError:
            failures.append(f"EVIDENCE_FILE_MISSING:{prefix}")
            continue
        except (OSError, ValueError):
            failures.append(f"EVIDENCE_PATH_ESCAPE:{prefix}")
            continue
        if not candidate.is_file():
            failures.append(f"EVIDENCE_FILE_INVALID:{prefix}")
            continue
        if sha256_bytes(candidate.read_bytes()) != record.sha256:
            failures.append(f"EVIDENCE_DIGEST_MISMATCH:{prefix}")
            continue
        verified_kinds.add(record.kind)
    for kind in sorted(set(required_evidence_kinds) - verified_kinds):
        failures.append(f"REQUIRED_EVIDENCE_MISSING:{kind}")
    return tuple(failures)


class QualificationHarnessReport(EngineModel):
    suite_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    passed: bool
    responsibility_count: int = Field(ge=1)
    passed_responsibility_count: int = Field(ge=0)
    not_run_responsibility_ids: tuple[str, ...] = ()
    product_failure_responsibility_ids: tuple[str, ...] = ()
    model_failure_responsibility_ids: tuple[str, ...] = ()
    environment_failure_responsibility_ids: tuple[str, ...] = ()
    fixture_failure_responsibility_ids: tuple[str, ...] = ()
    contract_failure_responsibility_ids: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


def classify_qualification_failure(error: BaseException) -> QualificationFailureClass:
    """오류를 제품 FAIL과 model/environment/fixture/contract 결함으로 분리한다."""

    name = type(error).__name__.casefold()
    message = str(error).casefold()
    value = f"{name}:{message}"
    if isinstance(error, QualificationManifestError) or any(
        marker in value
        for marker in (
            "checkpointcontract",
            "contract_mismatch",
            "contract mismatch",
            "reuse_invalidated",
            "raw_request_pipeline_bypassed",
        )
    ):
        return QualificationFailureClass.CONTRACT
    if any(
        marker in value
        for marker in (
            "fixture",
            "source_manifest",
            "source manifest",
            "input_digest",
            "payload digest",
            "oracle",
        )
    ):
        return QualificationFailureClass.FIXTURE
    if any(
        marker in value
        for marker in (
            "structuredrole",
            "role_schema",
            "model output",
            "model failed",
            "schema recovery",
            "semantic response",
        )
    ):
        return QualificationFailureClass.MODEL
    if any(
        marker in value
        for marker in (
            "permission_policy",
            "permission policy",
            "codex_unavailable",
            "executable",
            "connection",
            "network",
            "rate limit",
            "rate_limit",
            "usage limit",
            "usage_limit",
            "quota",
            "timeout",
        )
    ):
        return QualificationFailureClass.ENVIRONMENT
    return QualificationFailureClass.PRODUCT


def _ordered_contains(actual: tuple[str, ...], required: tuple[str, ...]) -> bool:
    cursor = 0
    for value in actual:
        if cursor < len(required) and value == required[cursor]:
            cursor += 1
    return cursor == len(required)


def evaluate_qualification_responsibilities(
    suite: QualificationSuiteManifest,
    outcomes: tuple[QualificationCellOutcome, ...],
    *,
    evaluation_contract_digest: str,
    run_root: Path | str,
    expected_cell_bindings: Mapping[str, tuple[str, int]],
    expected_freeze_bundle_digest: str | None = None,
    expected_governance_plugin_identity_digest: str | None = None,
    expected_candidate_wheel_digest: str | None = None,
    expected_candidate_wheel_binding_digest: str | None = None,
    expected_candidate_distribution_name: str | None = None,
    expected_candidate_distribution_version: str | None = None,
) -> QualificationHarnessReport:
    """필수 책임을 cell 수가 아닌 의미·provenance·직접 evidence로 판정한다."""

    expected = {item.responsibility_id: item for item in suite.e2e_responsibilities}
    observed: dict[str, list[QualificationCellOutcome]] = {key: [] for key in expected}
    failures: list[str] = []
    for outcome in outcomes:
        for responsibility_id in outcome.responsibility_ids:
            if responsibility_id not in expected:
                failures.append(
                    f"UNKNOWN_RESPONSIBILITY:{outcome.cell_id}:{responsibility_id}"
                )
                continue
            observed[responsibility_id].append(outcome)

    passed: set[str] = set()
    not_run: set[str] = set()
    classified: dict[QualificationFailureClass, set[str]] = {
        item: set() for item in QualificationFailureClass
    }
    for responsibility_id, requirement in expected.items():
        cells = observed[responsibility_id]
        if not cells:
            not_run.add(responsibility_id)
            failures.append(f"NOT_RUN:{responsibility_id}:cell 없음")
            continue
        valid_pass = False
        for cell in cells:
            if cell.status is QualificationCellStatus.NOT_RUN:
                not_run.add(responsibility_id)
                continue
            if cell.status is QualificationCellStatus.FAILED:
                assert cell.failure_class is not None
                classified[cell.failure_class].add(responsibility_id)
                failures.append(
                    f"{cell.failure_class.value.upper()}_FAILURE:{responsibility_id}:"
                    f"{cell.failure_code}"
                )
                continue
            cell_failures: list[str] = []
            if cell.evaluation_contract_digest != evaluation_contract_digest:
                cell_failures.append("CELL_CONTRACT_MISMATCH")
            if any(
                value is None
                for value in (
                    expected_candidate_wheel_digest,
                    expected_candidate_wheel_binding_digest,
                    expected_candidate_distribution_name,
                    expected_candidate_distribution_version,
                )
            ):
                cell_failures.append("CANDIDATE_WHEEL_BINDING_MISSING")
            binding = expected_cell_bindings.get(cell.cell_id)
            if binding is None:
                cell_failures.append("CELL_EVIDENCE_BINDING_MISSING")
            else:
                cell_failures.extend(
                    verify_qualification_evidence_records(
                        cell,
                        run_root=run_root,
                        expected_fixture_digest=binding[0],
                        expected_order_seed=binding[1],
                        expected_freeze_bundle_digest=(
                            expected_freeze_bundle_digest
                        ),
                        expected_governance_plugin_identity_digest=(
                            expected_governance_plugin_identity_digest
                        ),
                        expected_candidate_wheel_digest=(
                            expected_candidate_wheel_digest
                        ),
                        expected_candidate_wheel_binding_digest=(
                            expected_candidate_wheel_binding_digest
                        ),
                        expected_candidate_distribution_name=(
                            expected_candidate_distribution_name
                        ),
                        expected_candidate_distribution_version=(
                            expected_candidate_distribution_version
                        ),
                        required_evidence_kinds=(
                            requirement.required_evidence_kinds
                        ),
                    )
                )
            if not set(cell.provenance) <= set(requirement.allowed_provenance):
                cell_failures.append("PROVENANCE_NOT_ALLOWED")
            if not set(requirement.required_provenance) <= set(cell.provenance):
                cell_failures.append("REQUIRED_PROVENANCE_MISSING")
            if requirement.raw_request_pipeline_required and not _ordered_contains(
                cell.pipeline_stages, requirement.required_pipeline_stages
            ):
                cell_failures.append("RAW_REQUEST_PIPELINE_BYPASSED")
            if (
                EvidenceProvenance.REUSED in cell.provenance
                and cell.reused_from_contract_digest != evaluation_contract_digest
            ):
                cell_failures.append("REUSE_INVALIDATED_BY_CONTRACT_CHANGE")
            if cell_failures:
                classified[QualificationFailureClass.CONTRACT].add(responsibility_id)
                failures.extend(
                    f"{code}:{responsibility_id}:{cell.cell_id}"
                    for code in cell_failures
                )
            else:
                valid_pass = True
        if valid_pass:
            passed.add(responsibility_id)
            not_run.discard(responsibility_id)
        elif responsibility_id in not_run and not any(
            item.startswith(f"NOT_RUN:{responsibility_id}:") for item in failures
        ):
            failures.append(f"NOT_RUN:{responsibility_id}:cell이 실행되지 않음")

    return QualificationHarnessReport(
        suite_manifest_digest=suite.manifest_digest,
        evaluation_contract_digest=evaluation_contract_digest,
        passed=len(passed) == len(expected) and not failures,
        responsibility_count=len(expected),
        passed_responsibility_count=len(passed),
        not_run_responsibility_ids=tuple(sorted(not_run)),
        product_failure_responsibility_ids=tuple(
            sorted(classified[QualificationFailureClass.PRODUCT])
        ),
        model_failure_responsibility_ids=tuple(
            sorted(classified[QualificationFailureClass.MODEL])
        ),
        environment_failure_responsibility_ids=tuple(
            sorted(classified[QualificationFailureClass.ENVIRONMENT])
        ),
        fixture_failure_responsibility_ids=tuple(
            sorted(classified[QualificationFailureClass.FIXTURE])
        ),
        contract_failure_responsibility_ids=tuple(
            sorted(classified[QualificationFailureClass.CONTRACT])
        ),
        failures=tuple(failures),
    )


def write_qualification_reproduction_bundle(
    *,
    source_root: Path,
    destination: Path,
    suite: QualificationSuiteManifest,
    source_files: Mapping[str, str],
    source_manifest_digest: str,
    fixture_documents: Mapping[str, Any],
    prompt_documents: Mapping[str, Any],
    schema_documents: Mapping[str, Any],
    threshold_documents: Mapping[str, Any],
    taxonomy_documents: Mapping[str, Any],
    model_inventory_document: Mapping[str, Any],
    model_lock_document: Mapping[str, Any],
    evaluator_files: tuple[str, ...],
) -> QualificationFreezeManifest:
    """source-tree 밖에서도 바이트 단위로 검사 가능한 immutable 입력 bundle을 쓴다."""

    root = source_root.resolve(strict=True)
    payload_root = destination.resolve() / "payload"
    file_digests: dict[str, str] = {}
    for relative, expected_digest in sorted(source_files.items()):
        source = (root / relative).resolve(strict=True)
        try:
            source.relative_to(root)
        except ValueError as error:
            raise QualificationManifestError(
                f"bundle source가 source root 밖입니다: {relative}"
            ) from error
        actual = sha256_bytes(source.read_bytes())
        if actual != expected_digest:
            raise QualificationManifestError(
                f"bundle 생성 중 source digest가 변경됐습니다: {relative}"
            )
        target = payload_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_bytes(target.read_bytes()) != actual:
                raise QualificationManifestError(
                    f"기존 bundle payload를 다른 바이트로 덮어쓸 수 없습니다: {relative}"
                )
        else:
            shutil.copyfile(source, target)
        file_digests[f"payload/{relative}"] = actual

    generated_documents: dict[str, Any] = {
        "suite-manifest.json": suite.model_dump(mode="json"),
        "fixture-inputs.json": dict(sorted(fixture_documents.items())),
        "prompt-inputs.json": dict(sorted(prompt_documents.items())),
        "schema-inputs.json": dict(sorted(schema_documents.items())),
        "threshold-inputs.json": dict(sorted(threshold_documents.items())),
        "taxonomy-inputs.json": dict(sorted(taxonomy_documents.items())),
        "model-inventory.json": dict(model_inventory_document),
        "model-lock.json": dict(model_lock_document),
    }
    for name, document in generated_documents.items():
        target = payload_root / "inputs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            document, ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        payload_digest = sha256_bytes(payload.encode("utf-8"))
        if target.exists():
            if target.read_text(encoding="utf-8") != payload:
                raise QualificationManifestError(
                    f"기존 bundle 입력을 다른 내용으로 덮어쓸 수 없습니다: {name}"
                )
        else:
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
        file_digests[f"payload/inputs/{name}"] = payload_digest

    evaluator_map: dict[str, str] = {}
    for relative in evaluator_files:
        if relative not in source_files:
            raise QualificationManifestError(
                f"evaluator가 source manifest에 없습니다: {relative}"
            )
        evaluator_map[relative] = source_files[relative]

    fixture_digest = sha256_digest(dict(sorted(fixture_documents.items())))
    prompt_digest = sha256_digest(dict(sorted(prompt_documents.items())))
    schema_digest = sha256_digest(dict(sorted(schema_documents.items())))
    threshold_digest = sha256_digest(dict(sorted(threshold_documents.items())))
    taxonomy_digest = sha256_digest(dict(sorted(taxonomy_documents.items())))
    evaluator_digest = sha256_digest(evaluator_map)
    body = {
        "format": "flowmarshal.qualification-freeze.v1",
        "suite_manifest_digest": suite.manifest_digest,
        "source_manifest_digest": source_manifest_digest,
        "fixture_digest": fixture_digest,
        "prompt_digest": prompt_digest,
        "schema_digest": schema_digest,
        "threshold_digest": threshold_digest,
        "taxonomy_digest": taxonomy_digest,
        "model_inventory_digest": sha256_digest(model_inventory_document),
        "model_lock_digest": sha256_digest(model_lock_document),
        "evaluator_digest": evaluator_digest,
        "file_digests": file_digests,
    }
    manifest = QualificationFreezeManifest.model_validate(
        body | {"bundle_digest": sha256_digest(body)}
    )
    manifest_path = destination.resolve() / "qualification-freeze.json"
    payload = json.dumps(
        manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists():
        if manifest_path.read_text(encoding="utf-8") != payload:
            raise QualificationManifestError(
                "기존 qualification freeze manifest를 다른 계약으로 덮어쓸 수 없습니다."
            )
    else:
        manifest_path.write_text(payload, encoding="utf-8")
    verify_qualification_reproduction_bundle(destination)
    return manifest


def verify_qualification_reproduction_bundle(
    destination: Path | str,
) -> QualificationFreezeManifest:
    root = Path(destination).resolve(strict=True)
    manifest_path = root / "qualification-freeze.json"
    manifest = QualificationFreezeManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    for relative, expected in manifest.file_digests.items():
        path = (root / relative).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise QualificationManifestError(
                f"bundle payload가 bundle root 밖입니다: {relative}"
            ) from error
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected:
            raise QualificationManifestError(
                f"qualification bundle payload digest가 다릅니다: {relative}"
            )
    return manifest
