"""candidate wheel의 깨끗한 non-editable 설치(E2E-18)를 typed evidence로 판정한다.

이 harness는 제품 흐름을 우회하지 않는다. 격리 Python에 candidate wheel을
non-editable로 설치한 뒤, 사용자 CLI·import origin·launcher probe와
project-e2e의 pre-provider dry invocation을 모두 그 설치본으로 실행하고,
각 단계의 실제 산출물을 run root 안의 typed ``QualificationEvidenceRecord``로
결속한다. 모델이나 harness의 성공 선언은 근거가 아니며, 파일이 없거나
바이트·cell·계약 결속이 깨지면 PASS를 만들지 않는다.

provider를 호출하는 단계는 여기에 없다. pre-provider dry invocation은 실제
``run_project_e2e``와 같은 결속 함수를 호출하되 provider 연결 전에 멈추는
명시적 fake 경계이며, 그 자체로 release PASS가 되지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import Field

from ..canonical import sha256_bytes, sha256_digest
from .domain import EngineModel
from .qualification import (
    QualificationRunError,
    _default_run_root,
    _write_json,
    project_root,
    qualification_suite_manifest,
    source_manifest_digest,
)
from .qualification_manifest import (
    CandidateWheelBinding,
    EvidenceProvenance,
    QualificationCellOutcome,
    QualificationCellStatus,
    QualificationFailureClass,
    QualificationManifestError,
    QualificationSuiteManifest,
    build_qualification_evidence_record,
    classify_qualification_failure,
    evaluate_qualification_responsibilities,
    verify_candidate_wheel_installation,
    verify_qualification_reproduction_bundle,
)


class CleanInstallError(RuntimeError):
    """clean install harness의 입력·경계 계약이 깨진 경우."""


CLEAN_INSTALL_FORMAT = "flowmarshal.clean-install-qualification.v1"
CLEAN_INSTALL_CELL_ID = "clean-noneditable-install"
CLEAN_INSTALL_RESPONSIBILITY_ID = "E2E-18"
CLEAN_INSTALL_ORDER_SEED = 0
CLEAN_INSTALL_STAGES = (
    "clean_environment",
    "wheel_install",
    "import_origin_check",
    "user_cli_smoke",
    "qualification_input_check",
)
# suite manifest의 E2E-18 required_evidence_kinds와 harness가 추가로 남기는 kind.
CLEAN_INSTALL_EVIDENCE_KINDS = (
    "wheel",
    "install_log",
    "import_origin",
    "cli_output",
    "bundle",
    "candidate_probe",
    "pre_provider_dry_run",
)
_COMMAND_TIMEOUT_SECONDS = 1800


class CleanInstallStageObservation(EngineModel):
    """한 단계의 실제 명령과 관측 결과다. 요약이 아니라 원자료 참조를 남긴다."""

    stage: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    returncode: int | None = None
    passed: bool
    detail: str = ""


class CleanInstallReport(EngineModel):
    """E2E-18 cell 하나의 판정과 결속이다."""

    format: Literal["flowmarshal.clean-install-qualification.v1"] = CLEAN_INSTALL_FORMAT
    cell_id: Literal["clean-noneditable-install"] = CLEAN_INSTALL_CELL_ID
    passed: bool
    suite_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    order_seed: int = CLEAN_INSTALL_ORDER_SEED
    freeze_bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reproduction_bundle_source: str = Field(min_length=1)
    candidate_wheel_path: str = Field(min_length=1)
    candidate_wheel_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_wheel_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_distribution_name: str = Field(min_length=1)
    candidate_distribution_version: str = Field(min_length=1)
    candidate_python: str = Field(min_length=1)
    observations: tuple[CleanInstallStageObservation, ...] = Field(min_length=1)
    outcome: QualificationCellOutcome
    failures: tuple[str, ...] = ()

    @property
    def report_digest(self) -> str:
        return sha256_digest(self)


@dataclass(frozen=True)
class CleanInstallVerification:
    """최종 scope 검증에서 저장 산출물만으로 다시 계산한 결과다."""

    valid: bool
    errors: tuple[str, ...]
    recalculated_fixture_digest: str | None
    recalculated_contract_digest: str | None


def clean_install_fixture_digest(
    *, candidate_wheel_digest: str, freeze_bundle_digest: str
) -> str:
    """cell 입력(설치 대상 wheel bytes + 동결 bundle)의 fixture digest다."""

    return sha256_digest(
        {
            "fixture_id": CLEAN_INSTALL_CELL_ID,
            "candidate_wheel_digest": candidate_wheel_digest,
            "freeze_bundle_digest": freeze_bundle_digest,
        }
    )


def clean_install_contract_digest(
    *,
    suite_manifest_digest: str,
    source_manifest_digest_value: str,
    fixture_digest: str,
    candidate_wheel_binding_digest: str,
    distribution_name: str,
    distribution_version: str,
) -> str:
    """이 cell의 evaluation 계약 digest다. 한 축이라도 바뀌면 재사용을 무효화한다."""

    return sha256_digest(
        {
            "format": CLEAN_INSTALL_FORMAT,
            "cell_id": CLEAN_INSTALL_CELL_ID,
            "responsibility_id": CLEAN_INSTALL_RESPONSIBILITY_ID,
            "suite_manifest_digest": suite_manifest_digest,
            "source_manifest_digest": source_manifest_digest_value,
            "fixture_digest": fixture_digest,
            "candidate_wheel_binding_digest": candidate_wheel_binding_digest,
            "distribution_name": distribution_name,
            "distribution_version": distribution_version,
            "stages": CLEAN_INSTALL_STAGES,
        }
    )


def clean_install_suite(root: Path) -> QualificationSuiteManifest:
    """승인된 threshold를 유지한 채 E2E-18 책임만 남긴 판정용 manifest다."""

    suite = qualification_suite_manifest(root)
    selected = [
        item.model_dump(mode="json")
        for item in suite.e2e_responsibilities
        if item.responsibility_id == CLEAN_INSTALL_RESPONSIBILITY_ID
    ]
    if len(selected) != 1:
        raise CleanInstallError("CLEAN_INSTALL_RESPONSIBILITY_NOT_DECLARED")
    return QualificationSuiteManifest.model_validate(
        suite.model_dump(mode="json") | {"e2e_responsibilities": selected}
    )


def _scripts_directory(venv_root: Path) -> Path:
    return venv_root / ("Scripts" if os.name == "nt" else "bin")


def candidate_python_path(venv_root: Path) -> Path:
    return _scripts_directory(venv_root) / (
        "python.exe" if os.name == "nt" else "python"
    )


def _user_cli_path(venv_root: Path) -> Path:
    return _scripts_directory(venv_root) / (
        "flowmarshal-engine.exe" if os.name == "nt" else "flowmarshal-engine"
    )


def _run(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise CleanInstallError(f"CLEAN_INSTALL_ARTIFACT_EXISTS:{path.name}")
    path.write_text(payload, encoding="utf-8", newline="\n")


def run_clean_install(
    *,
    root: Path | None = None,
    run_root: Path | None = None,
    candidate_wheel: Path | str,
    reproduction_bundle: Path | str,
    base_python: Path | str | None = None,
    pip_install_arguments: Sequence[str] = (),
) -> tuple[Path, CleanInstallReport]:
    """빈 환경에 candidate wheel을 설치하고 E2E-18 책임을 직접 관측한다."""

    base = (root or project_root()).resolve(strict=True)
    wheel = Path(candidate_wheel)
    if not wheel.is_absolute():
        raise CleanInstallError("CANDIDATE_WHEEL_ABSOLUTE_PATH_REQUIRED")
    try:
        wheel = wheel.resolve(strict=True)
    except OSError as error:
        raise CleanInstallError("CANDIDATE_WHEEL_NOT_FOUND") from error
    if not wheel.is_file() or wheel.suffix.casefold() != ".whl":
        raise CleanInstallError("CANDIDATE_WHEEL_FILE_REQUIRED")
    wheel_bytes = wheel.read_bytes()
    wheel_digest = sha256_bytes(wheel_bytes)

    bundle_source = Path(reproduction_bundle)
    if not bundle_source.is_absolute():
        raise CleanInstallError("REPRODUCTION_BUNDLE_ABSOLUTE_PATH_REQUIRED")
    try:
        freeze_manifest = verify_qualification_reproduction_bundle(bundle_source)
    except (QualificationManifestError, OSError, ValueError) as error:
        raise CleanInstallError(f"REPRODUCTION_BUNDLE_INVALID:{error}") from error
    bundle_source = bundle_source.resolve(strict=True)

    suite = clean_install_suite(base)
    requirement = suite.e2e_responsibilities[0]
    source_digest = source_manifest_digest(base)
    fixture_digest = clean_install_fixture_digest(
        candidate_wheel_digest=wheel_digest,
        freeze_bundle_digest=freeze_manifest.bundle_digest,
    )

    destination = (
        run_root
        if run_root is not None
        else _default_run_root(base, "clean-install", fixture_digest[7:15])
    )
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise CleanInstallError("CLEAN_INSTALL_RUN_ROOT_NOT_EMPTY")
    destination.mkdir(parents=True, exist_ok=True)

    # 동결 입력은 run root 안으로 복사해 바이트 그대로 재검증한다.
    bundle_root = destination / "reproduction-bundle"
    shutil.copytree(bundle_source, bundle_root)
    copied_manifest = verify_qualification_reproduction_bundle(bundle_root)
    if copied_manifest.bundle_digest != freeze_manifest.bundle_digest:
        raise CleanInstallError("REPRODUCTION_BUNDLE_COPY_DIGEST_MISMATCH")
    wheel_copy = destination / "candidate-wheel" / wheel.name
    wheel_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(wheel, wheel_copy)
    if sha256_bytes(wheel_copy.read_bytes()) != wheel_digest:
        raise CleanInstallError("CANDIDATE_WHEEL_COPY_DIGEST_MISMATCH")

    venv_root = destination / "candidate-venv"
    interpreter = str(Path(base_python).resolve(strict=True)) if base_python else sys.executable
    candidate_python = candidate_python_path(venv_root)
    observations: list[CleanInstallStageObservation] = []
    failures: list[str] = []
    failure_class: QualificationFailureClass | None = None

    def _record(
        stage: str,
        command: Sequence[str],
        *,
        returncode: int | None,
        passed: bool,
        detail: str = "",
    ) -> bool:
        observations.append(
            CleanInstallStageObservation(
                stage=stage,
                command=tuple(str(item) for item in command),
                returncode=returncode,
                passed=passed,
                detail=detail[:2000],
            )
        )
        if not passed:
            failures.append(f"{stage.upper()}_FAILED")
        return passed

    logs: list[str] = []
    stage_ok = True
    try:
        # 1. clean_environment: 기존 site-packages를 상속하지 않는 빈 venv.
        created = _run(
            [interpreter, "-m", "venv", "--clear", str(venv_root)], cwd=destination
        )
        logs.append(f"$ {interpreter} -m venv --clear {venv_root}\n{created.stdout}{created.stderr}")
        stage_ok = _record(
            "clean_environment",
            (interpreter, "-m", "venv", "--clear", str(venv_root)),
            returncode=created.returncode,
            passed=created.returncode == 0 and candidate_python.is_file(),
            detail=(created.stderr or created.stdout),
        )

        # 2. wheel_install: non-editable 설치와 pip check.
        if stage_ok:
            install_command = [
                str(candidate_python),
                "-m",
                "pip",
                "install",
                *[str(item) for item in pip_install_arguments],
                str(wheel),
            ]
            installed = _run(install_command, cwd=destination)
            logs.append(f"$ {' '.join(install_command)}\n{installed.stdout}{installed.stderr}")
            checked = None
            if installed.returncode == 0:
                checked = _run(
                    [str(candidate_python), "-m", "pip", "check"], cwd=destination
                )
                logs.append(
                    f"$ {candidate_python} -m pip check\n{checked.stdout}{checked.stderr}"
                )
            stage_ok = _record(
                "wheel_install",
                install_command,
                returncode=installed.returncode,
                passed=installed.returncode == 0
                and checked is not None
                and checked.returncode == 0,
                detail=(installed.stderr or installed.stdout)
                + ("" if checked is None else checked.stdout + checked.stderr),
            )

        # 3. import_origin_check: 제품 import가 설치본인지 직접 관측한다.
        origin_document: dict[str, Any] = {}
        if stage_ok:
            probe_source = (
                "import json, flowmarshal, flowmarshal.engine.qualification_manifest as m,"
                " sys, sysconfig\n"
                "print(json.dumps({'package': flowmarshal.__file__,"
                " 'qualification_manifest': m.__file__,"
                " 'purelib': sysconfig.get_paths()['purelib'],"
                " 'executable': sys.executable,"
                " 'version': sys.version}))"
            )
            command = [str(candidate_python), "-c", probe_source]
            observed = _run(command, cwd=destination)
            detail = observed.stderr
            passed = observed.returncode == 0
            if passed:
                try:
                    origin_document = json.loads(observed.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    passed = False
                    detail = "IMPORT_ORIGIN_OUTPUT_INVALID"
            if passed:
                package_file = Path(str(origin_document["package"]))
                purelib = Path(str(origin_document["purelib"]))
                if not _is_within(package_file, purelib) or not _is_within(
                    purelib, venv_root
                ):
                    passed = False
                    detail = "IMPORT_ORIGIN_OUTSIDE_CANDIDATE_VENV"
                elif _is_within(package_file, base / "src"):
                    passed = False
                    detail = "SOURCE_PRODUCT_IMPORT_FORBIDDEN"
            stage_ok = _record(
                "import_origin_check",
                command,
                returncode=observed.returncode,
                passed=passed,
                detail=detail,
            )

        # 4. user_cli_smoke: source checkout 없이 사용자 entrypoint가 동작한다.
        cli_document: dict[str, Any] = {}
        if stage_ok:
            cli = _user_cli_path(venv_root)
            command = [str(cli), "--help"]
            passed = cli.is_file()
            detail = "" if passed else "USER_CLI_ENTRYPOINT_MISSING"
            returncode: int | None = None
            if passed:
                helped = _run(command, cwd=destination)
                config_help = _run([str(cli), "config", "init", "--help"], cwd=destination)
                returncode = helped.returncode
                cli_document = {
                    "cli": str(cli),
                    "help_returncode": helped.returncode,
                    "help_stdout": helped.stdout,
                    "config_init_help_returncode": config_help.returncode,
                    "config_init_help_stdout": config_help.stdout,
                }
                passed = (
                    helped.returncode == 0
                    and config_help.returncode == 0
                    and "flowmarshal" in helped.stdout.casefold()
                )
                detail = helped.stderr + config_help.stderr
            stage_ok = _record(
                "user_cli_smoke",
                command,
                returncode=returncode,
                passed=passed,
                detail=detail,
            )

        # 5. qualification_input_check: launcher probe와 pre-provider dry invocation.
        probe_document: dict[str, Any] = {}
        dry_document: dict[str, Any] = {}
        if stage_ok:
            launcher = base / "scripts" / "installed_candidate_qualification.py"
            probe_command = [
                str(candidate_python),
                str(launcher),
                "--source-root",
                str(base),
                "--candidate-wheel",
                str(wheel),
                "probe",
            ]
            probed = _run(probe_command, cwd=destination)
            passed = probed.returncode == 0
            detail = probed.stderr
            if passed:
                try:
                    probe_document = json.loads(probed.stdout)
                    CandidateWheelBinding.model_validate(
                        probe_document["candidate_wheel_binding"]
                    )
                except (ValueError, KeyError):
                    passed = False
                    detail = "CANDIDATE_PROBE_OUTPUT_INVALID"
            if passed and not _is_within(
                Path(str(probe_document["eval_cli_source"])), base / "src"
            ):
                passed = False
                detail = "DEVELOPER_HARNESS_SOURCE_ROOT_MISMATCH"
            if passed:
                dry_command = [
                    str(candidate_python),
                    str(launcher),
                    "--source-root",
                    str(base),
                    "--candidate-wheel",
                    str(wheel),
                    "eval",
                    "--",
                    "run",
                    "--scope",
                    "project-e2e",
                    "--project-root",
                    str(base),
                    "--pre-provider-dry-run",
                ]
                dried = _run(dry_command, cwd=destination)
                passed = dried.returncode == 0
                detail = dried.stderr
                if passed:
                    try:
                        dry_document = json.loads(dried.stdout)
                    except ValueError:
                        passed = False
                        detail = "PRE_PROVIDER_DRY_RUN_OUTPUT_INVALID"
                if passed and (
                    dry_document.get("mode") != "pre_provider_dry_run"
                    or dry_document.get("release_pass") is not False
                    or dry_document.get("candidate_wheel_binding", {}).get("wheel_digest")
                    != wheel_digest
                ):
                    passed = False
                    detail = "PRE_PROVIDER_DRY_RUN_BINDING_MISMATCH"
            stage_ok = _record(
                "qualification_input_check",
                probe_command,
                returncode=probed.returncode,
                passed=passed,
                detail=detail,
            )
    except (OSError, subprocess.SubprocessError) as error:
        failure_class = classify_qualification_failure(error)
        _record("environment", (interpreter,), returncode=None, passed=False, detail=str(error))
        stage_ok = False

    install_log_path = destination / "install-log.txt"
    _write_text(install_log_path, "\n".join(logs) + "\n")
    origin_path = destination / "import-origin.json"
    _write_json(origin_path, origin_document)
    cli_path = destination / "cli-output.json"
    _write_json(cli_path, cli_document)
    probe_path = destination / "candidate-probe.json"
    _write_json(probe_path, probe_document)
    dry_path = destination / "pre-provider-dry-run.json"
    _write_json(dry_path, dry_document)

    # 설치가 성공한 경우에만 wheel/설치 package bytes 결속을 격리 Python에서 확인한다.
    binding: CandidateWheelBinding | None = None
    if probe_document:
        binding = CandidateWheelBinding.model_validate(
            probe_document["candidate_wheel_binding"]
        )
        if binding.wheel_digest != wheel_digest or binding.editable is not False:
            failures.append("CANDIDATE_WHEEL_BINDING_MISMATCH")
            stage_ok = False
    if binding is None:
        # 결속 없이 PASS를 만들지 않는다. 계약 digest만 결정적으로 유지한다.
        stage_ok = False
        binding_digest = sha256_digest({"unbound": fixture_digest})
        distribution_name = "unknown"
        distribution_version = "unknown"
    else:
        binding_digest = binding.binding_digest
        distribution_name = binding.distribution_name
        distribution_version = binding.distribution_version

    contract_digest = clean_install_contract_digest(
        suite_manifest_digest=suite.manifest_digest,
        source_manifest_digest_value=source_digest,
        fixture_digest=fixture_digest,
        candidate_wheel_binding_digest=binding_digest,
        distribution_name=distribution_name,
        distribution_version=distribution_version,
    )
    artifact_by_kind = {
        "wheel": wheel_copy,
        "install_log": install_log_path,
        "import_origin": origin_path,
        "cli_output": cli_path,
        "bundle": bundle_root / "qualification-freeze.json",
        "candidate_probe": probe_path,
        "pre_provider_dry_run": dry_path,
    }
    evidence_records = tuple(
        build_qualification_evidence_record(
            run_root=destination,
            path=artifact_by_kind[kind],
            kind=kind,
            cell_id=CLEAN_INSTALL_CELL_ID,
            evaluation_contract_digest=contract_digest,
            fixture_digest=fixture_digest,
            order_seed=CLEAN_INSTALL_ORDER_SEED,
            freeze_bundle_digest=freeze_manifest.bundle_digest,
            candidate_wheel_digest=wheel_digest,
            candidate_wheel_binding_digest=binding_digest,
            candidate_distribution_name=distribution_name,
            candidate_distribution_version=distribution_version,
        )
        for kind in CLEAN_INSTALL_EVIDENCE_KINDS
    )
    passed = stage_ok and not failures
    outcome = QualificationCellOutcome(
        cell_id=CLEAN_INSTALL_CELL_ID,
        evaluation_contract_digest=contract_digest,
        responsibility_ids=(CLEAN_INSTALL_RESPONSIBILITY_ID,),
        status=(
            QualificationCellStatus.PASSED
            if passed
            else QualificationCellStatus.FAILED
        ),
        provenance=(EvidenceProvenance.LIVE,),
        pipeline_stages=CLEAN_INSTALL_STAGES,
        evidence_kinds=tuple(requirement.required_evidence_kinds),
        evidence_refs=tuple(item.relative_path for item in evidence_records),
        evidence_records=evidence_records,
        failure_class=(
            None
            if passed
            else (failure_class or QualificationFailureClass.PRODUCT)
        ),
        failure_code=None if passed else (failures[0] if failures else "CLEAN_INSTALL_FAILED"),
    )
    report = CleanInstallReport(
        passed=passed,
        suite_manifest_digest=suite.manifest_digest,
        source_manifest_digest=source_digest,
        evaluation_contract_digest=contract_digest,
        fixture_digest=fixture_digest,
        freeze_bundle_digest=freeze_manifest.bundle_digest,
        reproduction_bundle_source=str(bundle_source),
        candidate_wheel_path=str(wheel),
        candidate_wheel_digest=wheel_digest,
        candidate_wheel_binding_digest=binding_digest,
        candidate_distribution_name=distribution_name,
        candidate_distribution_version=distribution_version,
        candidate_python=str(candidate_python),
        observations=tuple(observations),
        outcome=outcome,
        failures=tuple(failures),
    )
    _write_json(destination / "clean-install-report.json", report)
    return destination, report


def verify_clean_install_report(
    *, root: Path | str, run_root: Path | str, report: CleanInstallReport
) -> CleanInstallVerification:
    """저장된 산출물만으로 경로 confinement·존재·digest·결속을 다시 검사한다.

    이 함수는 파일을 만들거나 바꾸지 않는다.
    """

    errors: list[str] = []
    base = Path(root).resolve(strict=True)
    destination = Path(run_root).resolve(strict=True)
    if report.source_manifest_digest != source_manifest_digest(base):
        errors.append("CLEAN_INSTALL_SOURCE_MANIFEST_MISMATCH")
    try:
        suite = clean_install_suite(base)
    except CleanInstallError:
        return CleanInstallVerification(
            False, ("CLEAN_INSTALL_RESPONSIBILITY_NOT_DECLARED",), None, None
        )
    if report.suite_manifest_digest != suite.manifest_digest:
        errors.append("CLEAN_INSTALL_SUITE_MANIFEST_MISMATCH")
    try:
        bundle_digest = verify_qualification_reproduction_bundle(
            destination / "reproduction-bundle"
        ).bundle_digest
    except Exception:
        bundle_digest = None
        errors.append("CLEAN_INSTALL_FREEZE_BUNDLE_INVALID")
    if bundle_digest is not None and bundle_digest != report.freeze_bundle_digest:
        errors.append("CLEAN_INSTALL_FREEZE_BUNDLE_MISMATCH")

    wheel_copy = destination / "candidate-wheel" / Path(report.candidate_wheel_path).name
    wheel_digest: str | None = None
    try:
        resolved = wheel_copy.resolve(strict=True)
        resolved.relative_to(destination)
        wheel_digest = sha256_bytes(resolved.read_bytes())
    except (OSError, ValueError):
        errors.append("CLEAN_INSTALL_CANDIDATE_WHEEL_MISSING")
    if wheel_digest is not None and wheel_digest != report.candidate_wheel_digest:
        errors.append("CLEAN_INSTALL_CANDIDATE_WHEEL_DIGEST_MISMATCH")

    fixture_digest = None
    contract_digest = None
    if bundle_digest is not None and wheel_digest is not None:
        fixture_digest = clean_install_fixture_digest(
            candidate_wheel_digest=wheel_digest, freeze_bundle_digest=bundle_digest
        )
        if fixture_digest != report.fixture_digest:
            errors.append("CLEAN_INSTALL_FIXTURE_DIGEST_MISMATCH")
        contract_digest = clean_install_contract_digest(
            suite_manifest_digest=suite.manifest_digest,
            source_manifest_digest_value=source_manifest_digest(base),
            fixture_digest=fixture_digest,
            candidate_wheel_binding_digest=report.candidate_wheel_binding_digest,
            distribution_name=report.candidate_distribution_name,
            distribution_version=report.candidate_distribution_version,
        )
        if contract_digest != report.evaluation_contract_digest:
            errors.append("CLEAN_INSTALL_CONTRACT_DIGEST_MISMATCH")

    responsibility = evaluate_qualification_responsibilities(
        suite,
        (report.outcome,),
        evaluation_contract_digest=report.evaluation_contract_digest,
        run_root=destination,
        expected_cell_bindings={
            report.outcome.cell_id: (report.fixture_digest, report.order_seed)
        },
        expected_freeze_bundle_digest=report.freeze_bundle_digest,
        expected_candidate_wheel_digest=report.candidate_wheel_digest,
        expected_candidate_wheel_binding_digest=report.candidate_wheel_binding_digest,
        expected_candidate_distribution_name=report.candidate_distribution_name,
        expected_candidate_distribution_version=report.candidate_distribution_version,
    )
    errors.extend(responsibility.failures)
    if report.passed and not responsibility.passed:
        errors.append("CLEAN_INSTALL_PASS_WITHOUT_RESPONSIBILITY_EVIDENCE")
    if report.passed is not (report.outcome.status is QualificationCellStatus.PASSED):
        errors.append("CLEAN_INSTALL_STATUS_MISMATCH")
    if report.outcome.cell_id != report.cell_id:
        errors.append("CLEAN_INSTALL_CELL_ID_MISMATCH")
    return CleanInstallVerification(
        not errors, tuple(errors), fixture_digest, contract_digest
    )


def load_clean_install_report(run_root: Path | str) -> CleanInstallReport:
    path = Path(run_root).resolve(strict=True) / "clean-install-report.json"
    try:
        return CleanInstallReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise QualificationRunError(f"CLEAN_INSTALL_REPORT_INVALID:{error}") from error
