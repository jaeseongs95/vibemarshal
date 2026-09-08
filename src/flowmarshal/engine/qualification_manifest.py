"""1.0 qualification 입력 동결과 책임별 판정 계약.

이 모듈은 모델이나 harness의 성공 선언을 신뢰하지 않는다. 평가 입력의 각 축과
실행 provenance를 별도로 결속하고, 필수 책임별 직접 evidence가 모두 있을 때만
qualification 책임 집합을 통과시킨다.
"""
from __future__ import annotations

import json
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from pydantic import Field, model_validator

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


class QualificationCellOutcome(EngineModel):
    cell_id: str = Field(min_length=1)
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    responsibility_ids: tuple[str, ...] = Field(min_length=1)
    status: QualificationCellStatus
    provenance: tuple[EvidenceProvenance, ...] = Field(min_length=1)
    pipeline_stages: tuple[str, ...] = ()
    evidence_kinds: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
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
        if self.status is QualificationCellStatus.PASSED:
            if self.failure_class is not None or self.failure_code is not None:
                raise ValueError("PASS cell에는 failure 분류가 있으면 안 됩니다.")
            if not self.evidence_refs:
                raise ValueError("PASS cell에는 독립 검토 가능한 evidence ref가 필요합니다.")
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
            if not set(cell.provenance) <= set(requirement.allowed_provenance):
                cell_failures.append("PROVENANCE_NOT_ALLOWED")
            if not set(requirement.required_provenance) <= set(cell.provenance):
                cell_failures.append("REQUIRED_PROVENANCE_MISSING")
            if not set(requirement.required_evidence_kinds) <= set(cell.evidence_kinds):
                cell_failures.append("REQUIRED_EVIDENCE_MISSING")
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
