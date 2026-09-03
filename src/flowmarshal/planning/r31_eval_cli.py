from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from ..canonical import canonical_json, sha256_digest
from .r31_evaluation import (
    EvaluationObservationBatch,
    build_builtin_fixtures,
    evaluate_repeated_runs,
    ordered_model_inputs,
)
from .r31_eval_campaign import CampaignStatus, run_live_evaluation_campaign
from .r31_eval_runner import (
    evaluation_contract_digest,
    load_evaluation_role_instructions,
    run_live_evaluation,
)
from .r31_live_smoke import (
    _ensure_smoke_instruction,
    load_role_configuration,
)
from .r31_models import ExecutionPolicyEvidence, PolicyVerifiedCodex


def _atomic_output(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".tmp-{uuid4().hex[:16]}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(value))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def export_evaluation_inputs(output: str | Path, order_seeds: tuple[int, ...]) -> dict:
    if not order_seeds or len(order_seeds) != len(set(order_seeds)):
        raise ValueError("order seed는 하나 이상의 중복 없는 정수여야 합니다.")
    fixtures = build_builtin_fixtures()
    payload = {
        "schema_version": "3.1",
        "fixture_count": len(fixtures),
        "fixture_catalog_digest": sha256_digest(fixtures),
        "oracle_included": False,
        "batches": [
            {
                "run_id": f"forward-{seed}",
                "order_seed": seed,
                "model_inputs": ordered_model_inputs(fixtures, order_seed=seed),
            }
            for seed in order_seeds
        ],
    }
    _atomic_output(Path(output), payload)
    return payload


def evaluate_observation_file(path: str | Path, output: str | Path | None = None):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("batches"), list):
        raise ValueError("observation 파일에는 batches 배열이 필요합니다.")
    batches = tuple(EvaluationObservationBatch.model_validate(item) for item in raw["batches"])
    if not batches:
        raise ValueError("observation 파일에는 하나 이상의 batch가 필요합니다.")
    observed_case_sets = [
        {item.case_id for item in batch.observations} for batch in batches
    ]
    if any(case_ids != observed_case_sets[0] for case_ids in observed_case_sets[1:]):
        raise ValueError("모든 observation batch의 case 집합이 같아야 합니다.")
    catalog = build_builtin_fixtures()
    fixture_by_id = {item.case_id: item for item in catalog}
    unknown = sorted(observed_case_sets[0] - set(fixture_by_id))
    if unknown:
        raise ValueError(f"observation에 알 수 없는 case ID가 있습니다: {unknown}")
    fixtures = tuple(
        fixture_by_id[case_id] for case_id in sorted(observed_case_sets[0])
    )
    report = evaluate_repeated_runs(fixtures, batches)
    if output is not None:
        _atomic_output(Path(output), report)
    return report


def _parse_seeds(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("order seed는 쉼표로 구분한 정수여야 합니다.") from exc


def _parse_case_ids(value: str) -> tuple[str, ...]:
    ids = tuple(item.strip() for item in value.split(",") if item.strip())
    if not ids or len(ids) != len(set(ids)):
        raise argparse.ArgumentTypeError("case ID는 쉼표로 구분한 중복 없는 값이어야 합니다.")
    return ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FlowMarshal Planner R3.1 평가 하네스")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export-inputs",
        help="숨은 oracle 없이 순서 변형 모델 입력을 export합니다.",
    )
    export.add_argument("--output", required=True)
    export.add_argument("--order-seeds", type=_parse_seeds, default=(1, 2, 3))
    evaluate = commands.add_parser(
        "evaluate",
        help="모델/runner observation batch를 고정 oracle과 비교합니다.",
    )
    evaluate.add_argument("--observations", required=True)
    evaluate.add_argument("--output")
    live = commands.add_parser(
        "run-models",
        help="실제 역할 모델로 선택 fixture를 실행하고 숨은 oracle로 평가합니다.",
    )
    live.add_argument("--artifact-root", required=True)
    live.add_argument("--skill-root", required=True)
    live.add_argument("--role-config", required=True)
    live.add_argument(
        "--codex-bin",
        default=os.environ.get("FLOWMARSHAL_CODEX_BIN"),
    )
    live.add_argument("--order-seeds", type=_parse_seeds, default=(1,))
    live.add_argument(
        "--resume",
        action="store_true",
        help="기존 full-catalog campaign의 검증된 cell부터 이어서 실행합니다.",
    )
    live.add_argument(
        "--max-new-cells",
        type=int,
        help="이번 호출에서 새로 실행할 campaign cell 수를 제한합니다.",
    )
    scope = live.add_mutually_exclusive_group(required=True)
    scope.add_argument("--case-ids", type=_parse_case_ids)
    scope.add_argument("--full-catalog", action="store_true")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.command == "export-inputs":
        payload = export_evaluation_inputs(arguments.output, arguments.order_seeds)
        print(canonical_json(payload))
        return 0
    if arguments.command == "run-models":
        if not arguments.codex_bin:
            raise SystemExit("--codex-bin 또는 FLOWMARSHAL_CODEX_BIN이 필요합니다.")
        catalog = build_builtin_fixtures()
        if arguments.full_catalog:
            contract_digest = evaluation_contract_digest(
                load_evaluation_role_instructions(arguments.skill_root)
            )
            fixtures = catalog
        else:
            if arguments.resume or arguments.max_new_cells is not None:
                raise SystemExit(
                    "--resume과 --max-new-cells는 --full-catalog에서만 사용합니다."
                )
            by_id = {item.case_id: item for item in catalog}
            unknown = sorted(set(arguments.case_ids) - set(by_id))
            if unknown:
                raise SystemExit(f"알 수 없는 case ID: {', '.join(unknown)}")
            fixtures = tuple(by_id[case_id] for case_id in arguments.case_ids)
        root = Path(arguments.artifact_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        _ensure_smoke_instruction(root)
        configuration = load_role_configuration(arguments.role_config)

        if arguments.full_catalog:

            def execute_cell(fixture, seed, model_input, cell_root):
                del model_input, cell_root
                # model-call journal의 hash 계층까지 붙어도 Windows MAX_PATH에
                # 닿지 않도록 실제 Runner cwd는 짧은 campaign 전용 경로를 쓴다.
                runtime_key = fixture.model_case_ref.removeprefix("case-")
                runtime_root = root / "r" / f"s{seed}-{runtime_key}"
                runtime_root.mkdir(parents=True, exist_ok=True)
                _ensure_smoke_instruction(runtime_root)
                cell_policy_evidence: list[ExecutionPolicyEvidence] = []

                def cell_client_factory():
                    return PolicyVerifiedCodex(
                        codex_bin=arguments.codex_bin,
                        evidence_sink=cell_policy_evidence.append,
                    )

                return run_live_evaluation(
                    (fixture,),
                    order_seeds=(seed,),
                    artifact_root=runtime_root,
                    skill_root=arguments.skill_root,
                    configuration=configuration,
                    client_factory=cell_client_factory,
                    policy_evidence=cell_policy_evidence,
                )

            campaign = run_live_evaluation_campaign(
                fixtures,
                order_seeds=arguments.order_seeds,
                artifact_root=root,
                configuration_id=configuration.configuration_id,
                configuration_digest=configuration.configuration_digest,
                evaluation_contract_digest=contract_digest,
                execute_cell=execute_cell,
                resume=arguments.resume,
                max_new_cells=arguments.max_new_cells,
            )
            full_scope = (
                set(arguments.order_seeds) == {1, 2, 3}
                and len(arguments.order_seeds) == 3
            )
            campaign_payload = {
                "receipt_schema": (
                    "flowmarshal.planner-r31.role-fixture-evaluation-campaign.v1"
                ),
                "status": campaign.status.value,
                "qualification_scope": (
                    "FULL_ROLE_FIXTURE_PROBE"
                    if full_scope
                    else "PARTIAL_FULL_CATALOG_SEEDS"
                ),
                "go_eligible": False,
                "go_limitation": (
                    "전체 role fixture probe도 실제 candidate generator 전체 pipeline과 "
                    "독립 end-to-end forward qualification을 단독으로 대체하지 않는다."
                ),
                "configuration_id": configuration.configuration_id,
                "configuration_digest": configuration.configuration_digest,
                "evaluation_contract_digest": contract_digest,
                "campaign_manifest": campaign.manifest.model_dump(mode="json"),
                "campaign_manifest_digest": campaign.manifest_digest,
                "completed_cell_count": campaign.completed_cell_count,
                "total_cell_count": campaign.total_cell_count,
                "newly_completed_cell_count": campaign.newly_completed_cell_count,
                "remaining_cell_count": campaign.remaining_cell_count,
                "pause_reason": campaign.pause_reason,
                "attempt_receipt_path": campaign.attempt_receipt_path,
                "resolved_models": [
                    item.model_dump(mode="json") for item in campaign.resolved_models
                ],
                "model_call_receipts": [
                    item.model_dump(mode="json")
                    for item in campaign.model_call_receipts
                ],
                "execution_policy_evidence": [
                    item.model_dump(mode="json")
                    for item in campaign.execution_policy_evidence
                ],
                "batches": [
                    item.model_dump(mode="json") for item in campaign.batches
                ],
                "evaluation_report": (
                    campaign.report.model_dump(mode="json")
                    if campaign.report is not None
                    else None
                ),
                "evaluation_report_digest": (
                    campaign.report.report_digest
                    if campaign.report is not None
                    else None
                ),
            }
            invocation_path = (
                root
                / "campaign-invocations"
                / f"invocation-{uuid4().hex}.json"
            )
            _atomic_output(invocation_path, campaign_payload)
            final_path = None
            if campaign.report is not None:
                final_path = root / "r31-role-fixture-evaluation.json"
                _atomic_output(final_path, campaign_payload)
            print(
                canonical_json(
                    {
                        "status": campaign.status.value,
                        "qualification_scope": campaign_payload[
                            "qualification_scope"
                        ],
                        "completed_cell_count": campaign.completed_cell_count,
                        "total_cell_count": campaign.total_cell_count,
                        "newly_completed_cell_count": (
                            campaign.newly_completed_cell_count
                        ),
                        "remaining_cell_count": campaign.remaining_cell_count,
                        "pause_reason": campaign.pause_reason,
                        "invocation_receipt_path": str(invocation_path),
                        "final_receipt_path": (
                            str(final_path) if final_path is not None else None
                        ),
                        "failures": (
                            campaign.report.failures
                            if campaign.report is not None
                            else ()
                        ),
                    }
                )
            )
            return {
                CampaignStatus.PASS: 0,
                CampaignStatus.FAIL: 2,
                CampaignStatus.PARTIAL: 3,
                CampaignStatus.PAUSED_RATE_LIMIT: 3,
                CampaignStatus.ERROR: 4,
            }[campaign.status]

        policy_evidence: list[ExecutionPolicyEvidence] = []

        def client_factory():
            return PolicyVerifiedCodex(
                codex_bin=arguments.codex_bin,
                evidence_sink=policy_evidence.append,
            )

        result = run_live_evaluation(
            fixtures,
            order_seeds=arguments.order_seeds,
            artifact_root=root,
            skill_root=arguments.skill_root,
            configuration=configuration,
            client_factory=client_factory,
            policy_evidence=policy_evidence,
        )
        full_scope = (
            len(fixtures) == len(catalog)
            and set(arguments.order_seeds) == {1, 2, 3}
            and len(arguments.order_seeds) == 3
        )
        payload = {
            "receipt_schema": "flowmarshal.planner-r31.role-fixture-evaluation.v1",
            "status": "PASS" if result.report.passed else "FAIL",
            "qualification_scope": (
                "FULL_ROLE_FIXTURE_PROBE" if full_scope else "PARTIAL_SELECTED_FIXTURES"
            ),
            "go_eligible": False,
            "go_limitation": (
                "role fixture 평가는 실제 candidate generator 전체 pipeline의 50개 반복과 "
                "Core-free end-to-end qualification을 단독으로 대체하지 않는다."
            ),
            "configuration_id": result.configuration_id,
            "configuration_digest": result.configuration_digest,
            "fixture_ids": result.fixture_ids,
            "order_seeds": arguments.order_seeds,
            "resolved_models": [
                item.model_dump(mode="json") for item in result.resolved_models
            ],
            "model_call_receipts": [
                item.model_dump(mode="json") for item in result.model_call_receipts
            ],
            "execution_policy_evidence": [
                item.model_dump(mode="json")
                for item in result.execution_policy_evidence
            ],
            "batches": [item.model_dump(mode="json") for item in result.batches],
            "assessment_traces": [
                item.model_dump(mode="json") for item in result.assessment_traces
            ],
            "evaluation_report": result.report.model_dump(mode="json"),
            "evaluation_report_digest": result.report.report_digest,
        }
        destination = root / "r31-role-fixture-evaluation.json"
        _atomic_output(destination, payload)
        print(
            canonical_json(
                {
                    "status": payload["status"],
                    "qualification_scope": payload["qualification_scope"],
                    "fixture_count": len(fixtures),
                    "order_seeds": arguments.order_seeds,
                    "model_call_count": len(result.model_call_receipts),
                    "evaluation_report_digest": result.report.report_digest,
                    "receipt_path": str(destination),
                    "failures": result.report.failures,
                }
            )
        )
        return 0 if result.report.passed else 2
    report = evaluate_observation_file(arguments.observations, arguments.output)
    print(canonical_json(report))
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
