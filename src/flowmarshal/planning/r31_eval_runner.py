from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, TypeVar

from pydantic import Field, field_validator, model_validator

from ..canonical import sha256_digest
from .r31_domain import ModelCallReceipt, ModelCallStatus, PlanningRole
from .r31_evaluation import (
    EVALUATION_RULE_VERSION,
    EvaluationFixture,
    EvaluationModel,
    EvaluationObservation,
    EvaluationObservationBatch,
    EvaluationScope,
    EvaluationSuite,
    EvaluationThresholds,
    RepeatedEvaluationReport,
    evaluate_repeated_runs,
    ordered_model_inputs,
)
from .r31_live_smoke import LiveRoleConfiguration
from .r31_models import (
    CodexModelInventoryAdapter,
    CodexStructuredRoleRunner,
    ExecutionPolicyEvidence,
    ResolvedPlanningModel,
    StructuredRoleError,
    StructuredRoleRequest,
    StructuredRoleResult,
    resolve_model_role,
    strict_json_output_schema,
)
from .r31_store import PlanningArtifactRepository


MissionName = Literal[
    "new_build",
    "feature_extension",
    "legacy_refactor",
    "bugfix_stabilization",
    "migration_modernization",
    "analysis_audit",
]
EVALUATION_CONTRACT_VERSION = "flowmarshal.planner-r31.role-fixture-contract.v4"


class ClauseDisposition(StrEnum):
    REQUIREMENT = "requirement"
    EXCLUSION = "exclusion"
    OTHER = "other"


class ClauseAssessment(EvaluationModel):
    clause_id: str = Field(min_length=1, max_length=120)
    disposition: ClauseDisposition
    rationale: str = Field(min_length=1, max_length=1000)


class MissionMajorDefect(StrEnum):
    MISSION_DIGEST_MISMATCH = "MISSION_DIGEST_MISMATCH"


class MissionFixtureAssessment(EvaluationModel):
    observed_mission: MissionName | None
    clause_assessments: tuple[ClauseAssessment, ...] = Field(min_length=1)
    question_asked: bool = Field(
        description=(
            "최종 결과·범위·대상·호환성·외부 효과를 사용자가 정해야 해 Mission을 "
            "확정할 수 없으면 true. 구현 세부 선택만 남은 경우는 false다."
        )
    )
    blocked: bool
    mission_resolved: bool
    candidate_generated: bool
    scored_candidate_count: int = Field(ge=0)
    mixed_mission_ranking: bool
    request_precedence_ok: bool
    stale_or_digest_mismatch_detected: bool
    previous_mission_context_leaked: bool
    detected_major_defects: tuple[MissionMajorDefect, ...]
    control_evidence_codes: tuple[str, ...]

    @field_validator("clause_assessments")
    @classmethod
    def clause_ids_are_unique(
        cls,
        value: tuple[ClauseAssessment, ...],
    ) -> tuple[ClauseAssessment, ...]:
        ids = [item.clause_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Mission fixture clause 판정이 중복됐습니다.")
        return value

    @field_validator("detected_major_defects", "control_evidence_codes")
    @classmethod
    def finding_codes_are_unique(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Mission fixture finding code가 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def resolution_shape_is_consistent(self) -> "MissionFixtureAssessment":
        if self.mission_resolved != (self.observed_mission is not None):
            raise ValueError("mission_resolved와 observed_mission이 일치하지 않습니다.")
        if self.blocked and self.mission_resolved:
            raise ValueError("blocked Mission은 resolved일 수 없습니다.")
        if self.question_asked and self.mission_resolved:
            raise ValueError("blocking 질문과 Mission 확정을 동시에 반환할 수 없습니다.")
        if not self.mission_resolved and (
            self.candidate_generated or self.scored_candidate_count
        ):
            raise ValueError("미확정 Mission에서 후보를 생성하거나 점수화할 수 없습니다.")
        return self


class PlanFixtureStatus(StrEnum):
    ADMISSIBLE = "admissible"
    NEEDS_REVISION = "needs_revision"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class StructuralDefectCode(StrEnum):
    OVER_FRAGMENTED = "OVER_FRAGMENTED"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    MIGRATION_ORDER_INVALID = "MIGRATION_ORDER_INVALID"
    DUPLICATE_DISPATCH = "DUPLICATE_DISPATCH"
    SECRET_HARDCODED = "SECRET_HARDCODED"
    UNBOUNDED_SCAN = "UNBOUNDED_SCAN"
    REGRESSION_MISSING = "REGRESSION_MISSING"
    HUMAN_CHECKPOINT_MISSING = "HUMAN_CHECKPOINT_MISSING"
    OVER_GATED = "OVER_GATED"
    HARD_GATE_BYPASS = "HARD_GATE_BYPASS"
    DUPLICATE_STRATEGY = "DUPLICATE_STRATEGY"
    HANDOFF_CONTEXT_MISSING = "HANDOFF_CONTEXT_MISSING"


class MajorDefectCode(StrEnum):
    FUNCTIONAL_COHESION = "FUNCTIONAL_COHESION"
    CONTRACT_BREAK = "CONTRACT_BREAK"
    DATA_LOSS_RISK = "DATA_LOSS_RISK"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"
    SECURITY_DEFECT = "SECURITY_DEFECT"
    RESOURCE_RISK = "RESOURCE_RISK"
    VERIFICATION_GAP = "VERIFICATION_GAP"
    AUTHORITY_GAP = "AUTHORITY_GAP"
    EXECUTION_FRICTION = "EXECUTION_FRICTION"
    DIVERSITY_FAILURE = "DIVERSITY_FAILURE"
    RECOVERY_GAP = "RECOVERY_GAP"
    MISSION_DIGEST_MISMATCH = "MISSION_DIGEST_MISMATCH"


class PlanFixtureAssessment(EvaluationModel):
    status: PlanFixtureStatus = Field(
        description=(
            "독립 reviewer의 최종 판정. 입력 candidate의 기존 status나 proposed_top_2를 "
            "그대로 복사하지 않는다."
        )
    )
    analyzed_clause_ids: tuple[str, ...] = Field(
        description=(
            "독립 검토에서 실제로 대조한 모든 fixture.input.source_clauses의 clause_id. "
            "후보가 clause를 만족했는지와 무관하며 완전한 검토는 제공된 ID를 모두 "
            "포함한다. 요구 만족 여부는 status와 defect로 별도 판정한다."
        )
    )
    detected_structural_defects: tuple[StructuralDefectCode, ...]
    detected_major_defects: tuple[MajorDefectCode, ...]
    question_asked: bool = Field(
        description=(
            "status=blocked의 원인이 사용자만 정할 수 있는 범위·대상·보존기간·외부 "
            "효과 결정이면 true. 단순 결함 수정은 false다."
        )
    )
    scored_candidate_count: int = Field(
        ge=0,
        description=(
            "현재 reviewer가 Hard Gate 통과 뒤 실제로 soft score 단계에 보낸 후보 수. "
            "입력 candidate 수나 proposed_top_2 길이가 아니다."
        ),
    )
    hard_fail_candidate_scored: bool = Field(
        description=(
            "현재 reviewer의 최종 처리에서 Hard Gate 실패 후보를 score하거나 Top-2에 "
            "유지했을 때만 true. 입력 proposed_top_2에 있었더라도 reviewer가 제거하고 "
            "scored_candidate_count=0으로 판정했다면 false다."
        )
    )
    criterion_validation_complete: bool
    consumes_dependencies_complete: bool
    dependency_cycle_present: bool
    distinct_candidate_count: int = Field(
        ge=0,
        description=(
            "Hard Gate와 의미 중복 제거 뒤 남은 score/admission 적격 후보의 서로 다른 "
            "approach signature 수. 부적격 입력 후보는 세지 않는다."
        ),
    )
    duplicate_cluster_in_top2: bool = Field(
        description=(
            "현재 reviewer의 최종 Top-2에 같은 의미 cluster가 함께 남았을 때만 true. "
            "입력에서 중복을 발견했지만 제거했다면 false다."
        )
    )
    unsupported_validation_success_claimed: bool
    selected_candidate_ref: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "admissible일 때 선택한 candidate_ref. 미통과 판정이면 null이다."
        ),
    )
    reconstructed_mission: MissionName | None = Field(
        description="선택 후보가 달성할 fixture.input.mission. 미통과면 null이다."
    )
    reconstructed_work_item_refs: tuple[str, ...] = Field(
        description=(
            "선택 후보의 work_items에 있는 모든 work_item_ref. 미통과면 빈 배열이다."
        )
    )
    reconstructed_dependency_edges: tuple[str, ...] = Field(
        description=(
            "선택 후보의 모든 dependency를 'dependency_ref->consumer_work_item_ref' "
            "형식으로 적은 집합. dependency가 없거나 미통과면 빈 배열이다."
        )
    )
    reconstructed_failure_policy_refs: tuple[str, ...] = Field(
        description=(
            "선택 후보에서 비어 있지 않은 failure_policy를 가진 모든 work_item_ref. "
            "미통과면 빈 배열이다."
        )
    )
    reconstructed_validation_refs: tuple[str, ...] = Field(
        description=(
            "선택 후보에서 하나 이상의 validation을 가진 모든 work_item_ref. "
            "criterion_id가 아니라 work_item_ref를 사용하며, 미통과면 빈 배열이다."
        )
    )
    hidden_context_required: bool

    @field_validator(
        "analyzed_clause_ids",
        "detected_structural_defects",
        "detected_major_defects",
        "reconstructed_work_item_refs",
        "reconstructed_dependency_edges",
        "reconstructed_failure_policy_refs",
        "reconstructed_validation_refs",
    )
    @classmethod
    def entries_are_unique(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Plan fixture assessment 항목이 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def selection_shape_is_consistent(self) -> "PlanFixtureAssessment":
        if self.status is PlanFixtureStatus.ADMISSIBLE:
            if self.selected_candidate_ref is None:
                raise ValueError("admissible fixture 판정에는 선택 후보 참조가 필요합니다.")
            if self.scored_candidate_count == 0 or self.distinct_candidate_count == 0:
                raise ValueError("admissible fixture 판정에는 점수화된 적격 후보가 필요합니다.")
        elif self.selected_candidate_ref is not None:
            raise ValueError("미통과 fixture 판정에는 선택 후보를 둘 수 없습니다.")
        if self.hard_fail_candidate_scored and self.scored_candidate_count == 0:
            raise ValueError(
                "Hard Gate 실패 후보가 점수화됐다는 판정에는 점수화 후보가 필요합니다."
            )
        if self.distinct_candidate_count > self.scored_candidate_count:
            raise ValueError("적격 distinct 후보 수는 점수화 후보 수보다 클 수 없습니다.")
        if self.status is not PlanFixtureStatus.ADMISSIBLE and (
            self.reconstructed_mission is not None
            or self.reconstructed_work_item_refs
            or self.reconstructed_dependency_edges
            or self.reconstructed_failure_policy_refs
            or self.reconstructed_validation_refs
        ):
            raise ValueError("미통과 fixture 판정에는 선택 후보 복원값을 둘 수 없습니다.")
        return self


@dataclass(frozen=True)
class EvaluationRoleInstructions:
    purpose_resolver: str
    intent_reviewer: str
    hard_gate_reviewer: str
    critical_reviewer: str


class FixtureAssessmentTrace(EvaluationModel):
    case_id: str = Field(min_length=1, max_length=200)
    model_case_ref: str = Field(pattern=r"^case-[0-9a-f]{16}$")
    model_input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    suite: Literal["mission", "plan_quality"]
    proposal: dict[str, Any] | None = None
    reviewed_assessment: dict[str, Any] | None = None
    error_summary: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def success_or_error_is_present(self) -> "FixtureAssessmentTrace":
        if self.reviewed_assessment is None and self.error_summary is None:
            raise ValueError("fixture trace에는 최종 assessment 또는 오류가 필요합니다.")
        if self.reviewed_assessment is not None and self.error_summary is not None:
            raise ValueError("성공 assessment와 오류를 동시에 기록할 수 없습니다.")
        return self


@dataclass(frozen=True)
class LiveEvaluationResult:
    configuration_id: str
    configuration_digest: str
    fixture_ids: tuple[str, ...]
    batches: tuple[EvaluationObservationBatch, ...]
    report: RepeatedEvaluationReport
    model_call_receipts: tuple[ModelCallReceipt, ...]
    execution_policy_evidence: tuple[ExecutionPolicyEvidence, ...]
    resolved_models: tuple[ResolvedPlanningModel, ...]
    assessment_traces: tuple[FixtureAssessmentTrace, ...] = ()


class StructuredRunnerLike(Protocol):
    def run(
        self,
        request: StructuredRoleRequest,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> StructuredRoleResult: ...


TAssessment = TypeVar("TAssessment", MissionFixtureAssessment, PlanFixtureAssessment)


STRUCTURAL_DEFECT_CODES = tuple(item.value for item in StructuralDefectCode)
MAJOR_DEFECT_CODES = tuple(item.value for item in MajorDefectCode)

STRUCTURAL_DEFECT_TAXONOMY = {
    "OVER_FRAGMENTED": "하나의 관찰 가능한 기능이 파일·계층별 조각으로 과도하게 분리됨",
    "DEPENDENCY_MISSING": "consumer가 필요한 producer dependency 또는 consumes 연결이 없음",
    "MIGRATION_ORDER_INVALID": "expand/backfill/검증/contract의 안전한 migration 순서가 깨짐",
    "DUPLICATE_DISPATCH": "재시작·중복 trigger가 같은 외부 작업을 다시 생성할 수 있음",
    "SECRET_HARDCODED": "credential 또는 secret literal이 계획·prompt·artifact에 직접 포함됨",
    "UNBOUNDED_SCAN": "행·메모리·시간·재시도 상한 없는 대규모 읽기나 집계",
    "REGRESSION_MISSING": "보존해야 할 기존 동작·consumer의 회귀 검증이 없음",
    "HUMAN_CHECKPOINT_MISSING": "범위·대상·외부 효과를 정할 사용자 판단 지점이 누락됨",
    "OVER_GATED": "승인된 저위험 로컬 입력에 파일별 승인·일괄 네트워크 차단을 반복함",
    "HARD_GATE_BYPASS": "Hard Gate 실패를 soft score나 기존 Top-2 표기로 상쇄함",
    "DUPLICATE_STRATEGY": "이름만 다르고 의미 signature와 DAG가 같은 후보가 중복됨",
    "HANDOFF_CONTEXT_MISSING": "독립 재개에 필요한 상태·결과·digest가 대화에만 남음",
}

MAJOR_DEFECT_TAXONOMY = {
    "FUNCTIONAL_COHESION": "WorkItem이 독립 검증 가능한 기능 결과가 아니라 기술 조각에 그침",
    "CONTRACT_BREAK": "명시 호환성 또는 producer/consumer 계약이 깨짐",
    "DATA_LOSS_RISK": "파괴 순서·범위·중간 상태 때문에 데이터 유실 가능성이 있음",
    "NON_IDEMPOTENT": "재시도나 중복 실행이 중복 효과 또는 다른 최종 상태를 만듦",
    "SECURITY_DEFECT": "비밀·인증·인가·입력 검증 등 실제 보안 불변조건을 위반함",
    "RESOURCE_RISK": "규모에 대한 메모리·시간·호출량 상한이 없어 자원 고갈 위험이 있음",
    "VERIFICATION_GAP": "요구·회귀·완료조건을 객관적으로 검증할 계약이 부족함",
    "AUTHORITY_GAP": "중요한 범위·대상·외부 효과에 필요한 사용자 결정을 받지 못함",
    "EXECUTION_FRICTION": "안전한 저위험 작업을 불필요한 반복 승인·차단으로 실행 곤란하게 만듦",
    "DIVERSITY_FAILURE": "후보 이름만 다르고 실질적 접근 전략이 중복됨",
    "RECOVERY_GAP": "부분 실패 뒤 rollback·roll-forward·checkpoint 재개 경로가 부족함",
    "MISSION_DIGEST_MISMATCH": "선택 Mission snapshot과 요청에 결속된 digest가 다름",
}

PLAN_ASSESSMENT_RULES = (
    "결함을 찾았어도 admission했다면 그대로 status와 결함을 기록한다.",
    (
        "HUMAN_CHECKPOINT_MISSING 또는 AUTHORITY_GAP 때문에 사용자 결정 없이는 "
        "진행할 수 없어 blocked라면 question_asked=true다."
    ),
    (
        "analyzed_clause_ids는 요구 만족 목록이 아니라 검토 추적이다. 제공된 모든 "
        "source clause ID를 분석해 포함하고, 만족 여부는 status와 defect로 판정한다."
    ),
    "Hard Gate 실패 후보를 score하거나 최종 Top-2에 남기지 않는다.",
    (
        "scored_candidate_count와 distinct_candidate_count는 입력 후보 수가 아니라 "
        "현재 reviewer의 Gate·dedupe 이후 결과다."
    ),
    (
        "hard_fail_candidate_scored와 duplicate_cluster_in_top2도 입력 proposed_top_2가 "
        "아니라 현재 reviewer의 최종 결과를 뜻한다."
    ),
    (
        "selected_candidate_ref가 있으면 그 후보의 모든 work_item_ref를 "
        "reconstructed_work_item_refs에 기록한다."
    ),
    (
        "dependency edge는 'dependency_ref->consumer_work_item_ref'로, failure_policy와 "
        "validation 참조는 각각 해당 필드를 가진 모든 work_item_ref로 기록한다. "
        "criterion_id를 참조 배열에 넣지 않는다."
    ),
    "이전 대화나 숨은 문맥이 필요하면 hidden_context_required=true다.",
)


def _read_required(path: Path) -> str:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"평가 스킬 참조가 파일이 아닙니다: {resolved}")
    return resolved.read_text(encoding="utf-8")


def load_evaluation_role_instructions(
    skill_root: str | Path,
) -> EvaluationRoleInstructions:
    root = Path(skill_root).resolve(strict=True)
    common = _read_required(root / "SKILL.md")
    evaluation = _read_required(root / "references" / "evaluation.md")
    intent = _read_required(root / "references" / "intent-contract.md")
    plan = _read_required(root / "references" / "plan-contract.md")
    gates = _read_required(root / "references" / "quality-gates.md")
    preamble = (
        "FlowMarshal R3.1의 비권위 독립 fixture 평가 역할이다. 제공된 JSON만 사용하고 "
        "파일·shell·network·Core·계획 활성화·dispatch 기능을 사용하지 않는다. "
        "case_ref는 상관관계 값일 뿐 정답 단서가 아니다. 숨은 oracle을 추측하지 말고 "
        "관찰한 근거만 구조화 출력한다. 계획 단계 validation은 not_run이다.\n\n"
    )

    def combine(role: str, *references: str) -> str:
        return preamble + f"현재 역할: {role}\n\n" + common + "\n\n" + "\n\n".join(references)

    return EvaluationRoleInstructions(
        purpose_resolver=combine("purpose_resolver fixture 제안", intent, evaluation),
        intent_reviewer=combine("intent_reviewer 독립 fixture 검토", intent, evaluation),
        hard_gate_reviewer=combine("hard_gate_reviewer fixture 검토", gates, plan, evaluation),
        critical_reviewer=combine("critical_reviewer fixture 검토", gates, plan, evaluation),
    )


def evaluation_contract_digest(
    instructions: EvaluationRoleInstructions,
    *,
    evaluation_scope: EvaluationScope = EvaluationScope.ROLE_FIXTURE_PROBE,
) -> str:
    """재개 campaign이 서로 다른 prompt·schema 계약을 섞지 않게 결속한다."""

    return sha256_digest(
        {
            "contract_version": EVALUATION_CONTRACT_VERSION,
            "evaluation_rule_version": EVALUATION_RULE_VERSION,
            "evaluation_scope": evaluation_scope.value,
            "thresholds": EvaluationThresholds().model_dump(mode="json"),
            "metric_applicability": {
                "mission_candidate_diversity": (
                    evaluation_scope is EvaluationScope.FULL_PIPELINE
                ),
                "plan_candidate_diversity": True,
            },
            "instructions": {
                "purpose_resolver": instructions.purpose_resolver,
                "intent_reviewer": instructions.intent_reviewer,
                "hard_gate_reviewer": instructions.hard_gate_reviewer,
                "critical_reviewer": instructions.critical_reviewer,
            },
            "mission_output_schema": strict_json_output_schema(
                MissionFixtureAssessment.model_json_schema()
            ),
            "plan_output_schema": strict_json_output_schema(
                PlanFixtureAssessment.model_json_schema()
            ),
            "structural_defect_taxonomy": STRUCTURAL_DEFECT_TAXONOMY,
            "major_defect_taxonomy": MAJOR_DEFECT_TAXONOMY,
            "plan_assessment_rules": PLAN_ASSESSMENT_RULES,
            "reconstruction_contract": {
                "dependency_edge": "dependency_ref->consumer_work_item_ref",
                "failure_policy_refs": "all work_item_ref values with failure_policy",
                "validation_refs": "all work_item_ref values with validations",
            },
        }
    )


def _receipt_token_count(receipt: ModelCallReceipt) -> int:
    if receipt.token_count is not None:
        return receipt.token_count
    metrics = {item.name: item.value for item in receipt.usage}
    for name in ("total.totalTokens", "total.total_tokens", "total.total_token_count"):
        if name in metrics:
            return metrics[name]
    for input_name, output_name in (
        ("total.inputTokens", "total.outputTokens"),
        ("total.input_tokens", "total.output_tokens"),
    ):
        if input_name in metrics or output_name in metrics:
            return metrics.get(input_name, 0) + metrics.get(output_name, 0)
    return 0


def _receipt_latency(receipt: ModelCallReceipt) -> int:
    return receipt.latency_ms or 0


def _source_clause_ids(model_input: dict[str, Any]) -> set[str]:
    clauses = model_input["input"].get("source_clauses", [])
    return {
        item["clause_id"]
        for item in clauses
        if isinstance(item, dict) and isinstance(item.get("clause_id"), str)
    }


class RoleFixtureEvaluator:
    def __init__(
        self,
        *,
        runner: StructuredRunnerLike,
        models: dict[PlanningRole, ResolvedPlanningModel],
        cwd: str | Path,
        instructions: EvaluationRoleInstructions,
        receipt_sink: Callable[[ModelCallReceipt], None],
        trace_sink: Callable[[FixtureAssessmentTrace], None] | None = None,
    ) -> None:
        self._runner = runner
        self._models = models
        self._cwd = str(Path(cwd).resolve(strict=True))
        self._instructions = instructions
        self._receipt_sink = receipt_sink
        self._trace_sink = trace_sink

    def _trace(self, trace: FixtureAssessmentTrace) -> None:
        if self._trace_sink is not None:
            self._trace_sink(trace)

    def _call(
        self,
        role: PlanningRole,
        instructions: str,
        payload: dict[str, Any],
        result_type: type[TAssessment],
        validator: Callable[[TAssessment], None],
    ) -> tuple[TAssessment, ModelCallReceipt]:
        model = self._models[role]
        request = StructuredRoleRequest(
            role=role,
            instructions=instructions,
            payload=payload,
            output_schema=strict_json_output_schema(result_type.model_json_schema()),
            model_id=model.model_id,
            reasoning_effort=model.reasoning_effort,
            inventory_digest=model.inventory_digest,
            cwd=self._cwd,
        )

        def validate(raw: dict[str, Any]) -> TAssessment:
            parsed = result_type.model_validate(raw)
            validator(parsed)
            return parsed

        try:
            result = self._runner.run(request, validator=validate)
        except StructuredRoleError as exc:
            if exc.receipt is not None:
                self._receipt_sink(exc.receipt)
            raise
        expected_schema_digest = sha256_digest(request.output_schema)
        receipt_matches = (
            result.receipt.role is request.role
            and result.receipt.model_id == request.model_id
            and result.receipt.reasoning_effort == request.reasoning_effort
            and hmac.compare_digest(
                result.receipt.inventory_digest,
                request.inventory_digest,
            )
            and hmac.compare_digest(
                result.receipt.input_digest,
                request.request_digest,
            )
            and hmac.compare_digest(
                result.receipt.output_schema_digest,
                expected_schema_digest,
            )
            and result.receipt.status
            in {ModelCallStatus.SUCCEEDED, ModelCallStatus.SCHEMA_RECOVERED}
        )
        if not receipt_matches:
            self._receipt_sink(result.receipt)
            raise StructuredRoleError(
                f"{role.value} fixture receipt가 실제 structured request와 다릅니다.",
                receipt=result.receipt,
            )
        parsed = result_type.model_validate(result.payload)
        validator(parsed)
        receipt = result.receipt.model_copy(
            update={"output_digest": sha256_digest(parsed)}
        )
        self._receipt_sink(receipt)
        return parsed, receipt

    def evaluate(
        self,
        fixture: EvaluationFixture,
        model_input: dict[str, Any],
    ) -> tuple[EvaluationObservation, tuple[ModelCallReceipt, ...]]:
        if model_input.get("case_ref") != fixture.model_case_ref:
            raise ValueError("fixture와 model input case_ref가 다릅니다.")
        if fixture.suite is EvaluationSuite.MISSION:
            return self._evaluate_mission(fixture, model_input)
        return self._evaluate_plan(fixture, model_input)

    def _evaluate_mission(
        self,
        fixture: EvaluationFixture,
        model_input: dict[str, Any],
    ) -> tuple[EvaluationObservation, tuple[ModelCallReceipt, ...]]:
        clause_ids = _source_clause_ids(model_input)

        def validate(assessment: MissionFixtureAssessment) -> None:
            observed = {item.clause_id for item in assessment.clause_assessments}
            if observed != clause_ids:
                raise ValueError(
                    "Mission fixture는 모든 source clause를 정확히 한 번 판정해야 합니다."
                )

        receipts: list[ModelCallReceipt] = []
        proposal: MissionFixtureAssessment | None = None
        try:
            proposal, proposal_receipt = self._call(
                PlanningRole.PURPOSE_RESOLVER,
                self._instructions.purpose_resolver,
                {
                    "fixture": model_input,
                    "authority_order": [
                        "explicit_request",
                        "user_selected_mission",
                        "project_profile",
                        "model_recommendation",
                    ],
                    "output_rule": "모든 source_clauses를 requirement/exclusion/other 중 하나로 판정한다.",
                    "finding_rule": (
                        "실제 무결성 결함은 detected_major_defects에, 정상적인 override·"
                        "차단·무시 증거는 control_evidence_codes에 기록한다."
                    ),
                },
                MissionFixtureAssessment,
                validate,
            )
            receipts.append(proposal_receipt)
            reviewed, reviewer_receipt = self._call(
                PlanningRole.INTENT_REVIEWER,
                self._instructions.intent_reviewer,
                {
                    "fixture": model_input,
                    "anonymous_proposal": proposal.model_dump(mode="json"),
                    "review_rule": (
                        "제안을 독립적으로 원문·profile 권위·digest에 대조하고 틀린 필드를 "
                        "그대로 복사하지 말고 최종 판정을 반환한다."
                    ),
                },
                MissionFixtureAssessment,
                validate,
            )
            receipts.append(reviewer_receipt)
        except StructuredRoleError as exc:
            if exc.receipt is not None:
                receipts.append(exc.receipt)
            self._trace(
                FixtureAssessmentTrace(
                    case_id=fixture.case_id,
                    model_case_ref=fixture.model_case_ref,
                    model_input_digest=sha256_digest(model_input),
                    suite=fixture.suite.value,
                    proposal=(
                        proposal.model_dump(mode="json")
                        if proposal is not None
                        else None
                    ),
                    error_summary=str(exc),
                )
            )
            return self._failed_observation(fixture, tuple(receipts))

        self._trace(
            FixtureAssessmentTrace(
                case_id=fixture.case_id,
                model_case_ref=fixture.model_case_ref,
                model_input_digest=sha256_digest(model_input),
                suite=fixture.suite.value,
                proposal=proposal.model_dump(mode="json"),
                reviewed_assessment=reviewed.model_dump(mode="json"),
            )
        )

        requirements = tuple(
            item.clause_id
            for item in reviewed.clause_assessments
            if item.disposition is ClauseDisposition.REQUIREMENT
        )
        exclusions = tuple(
            item.clause_id
            for item in reviewed.clause_assessments
            if item.disposition is ClauseDisposition.EXCLUSION
        )
        return (
            EvaluationObservation(
                case_id=fixture.case_id,
                observed_mission=reviewed.observed_mission,
                matched_requirement_ids=requirements,
                matched_exclusion_ids=exclusions,
                question_asked=reviewed.question_asked,
                blocked=reviewed.blocked,
                mission_resolved=reviewed.mission_resolved,
                candidate_generated=reviewed.candidate_generated,
                scored_candidate_count=reviewed.scored_candidate_count,
                mixed_mission_ranking=reviewed.mixed_mission_ranking,
                request_precedence_ok=reviewed.request_precedence_ok,
                stale_or_digest_mismatch_detected=(
                    reviewed.stale_or_digest_mismatch_detected
                ),
                previous_mission_context_leaked=(
                    reviewed.previous_mission_context_leaked
                ),
                detected_major_defects=tuple(
                    item.value for item in reviewed.detected_major_defects
                ),
                admitted=reviewed.mission_resolved and not reviewed.blocked,
                first_pass_schema_valid=all(
                    item.status is ModelCallStatus.SUCCEEDED
                    and item.schema_recovery_attempts == 0
                    for item in receipts
                ),
                token_usage=sum(_receipt_token_count(item) for item in receipts),
                latency_ms=sum(_receipt_latency(item) for item in receipts),
            ),
            tuple(receipts),
        )

    def _evaluate_plan(
        self,
        fixture: EvaluationFixture,
        model_input: dict[str, Any],
    ) -> tuple[EvaluationObservation, tuple[ModelCallReceipt, ...]]:
        clause_ids = _source_clause_ids(model_input)
        candidates = model_input["input"].get("candidate_set", [])
        candidate_by_ref = {
            item.get("candidate_ref"): item
            for item in candidates
            if isinstance(item, dict) and isinstance(item.get("candidate_ref"), str)
        }

        def validate(assessment: PlanFixtureAssessment) -> None:
            analyzed = set(assessment.analyzed_clause_ids)
            unknown = analyzed - clause_ids
            if unknown:
                raise ValueError(
                    f"Plan fixture가 알 수 없는 source clause를 참조합니다: {sorted(unknown)}"
                )
            missing = clause_ids - analyzed
            if missing:
                raise ValueError(
                    f"Plan fixture가 source clause를 분석하지 않았습니다: {sorted(missing)}"
                )
            if (
                assessment.selected_candidate_ref is not None
                and assessment.selected_candidate_ref not in candidate_by_ref
            ):
                raise ValueError("Plan fixture가 입력에 없는 후보를 선택했습니다.")

        role = (
            PlanningRole.CRITICAL_REVIEWER
            if fixture.critical
            else PlanningRole.HARD_GATE_REVIEWER
        )
        instructions = (
            self._instructions.critical_reviewer
            if fixture.critical
            else self._instructions.hard_gate_reviewer
        )
        receipts: list[ModelCallReceipt] = []
        try:
            reviewed, receipt = self._call(
                role,
                instructions,
                {
                    "fixture": model_input,
                    "structural_defect_taxonomy": STRUCTURAL_DEFECT_TAXONOMY,
                    "major_defect_taxonomy": MAJOR_DEFECT_TAXONOMY,
                    "assessment_rule": PLAN_ASSESSMENT_RULES,
                },
                PlanFixtureAssessment,
                validate,
            )
            receipts.append(receipt)
        except StructuredRoleError as exc:
            if exc.receipt is not None:
                receipts.append(exc.receipt)
            self._trace(
                FixtureAssessmentTrace(
                    case_id=fixture.case_id,
                    model_case_ref=fixture.model_case_ref,
                    model_input_digest=sha256_digest(model_input),
                    suite=fixture.suite.value,
                    error_summary=str(exc),
                )
            )
            return self._failed_observation(fixture, tuple(receipts))

        self._trace(
            FixtureAssessmentTrace(
                case_id=fixture.case_id,
                model_case_ref=fixture.model_case_ref,
                model_input_digest=sha256_digest(model_input),
                suite=fixture.suite.value,
                reviewed_assessment=reviewed.model_dump(mode="json"),
            )
        )

        reconstruction_complete = self._reconstruction_complete(
            model_input,
            reviewed,
        )
        admitted = reviewed.status is PlanFixtureStatus.ADMISSIBLE
        return (
            EvaluationObservation(
                case_id=fixture.case_id,
                observed_mission=model_input["input"].get("mission"),
                matched_requirement_ids=reviewed.analyzed_clause_ids,
                question_asked=reviewed.question_asked,
                blocked=reviewed.status is PlanFixtureStatus.BLOCKED,
                mission_resolved=True,
                scored_candidate_count=reviewed.scored_candidate_count,
                detected_structural_defects=tuple(
                    item.value for item in reviewed.detected_structural_defects
                ),
                detected_major_defects=tuple(
                    item.value for item in reviewed.detected_major_defects
                ),
                admitted=admitted,
                hard_fail_candidate_scored=reviewed.hard_fail_candidate_scored,
                criterion_validation_complete=reviewed.criterion_validation_complete,
                consumes_dependencies_complete=reviewed.consumes_dependencies_complete,
                dependency_cycle_present=reviewed.dependency_cycle_present,
                distinct_candidate_count=reviewed.distinct_candidate_count,
                duplicate_cluster_in_top2=reviewed.duplicate_cluster_in_top2,
                unsupported_validation_success_claimed=(
                    reviewed.unsupported_validation_success_claimed
                ),
                first_pass_schema_valid=(
                    receipt.status is ModelCallStatus.SUCCEEDED
                    and receipt.schema_recovery_attempts == 0
                ),
                token_usage=_receipt_token_count(receipt),
                latency_ms=_receipt_latency(receipt),
                forward_reconstruction_complete=reconstruction_complete,
                hidden_context_required=reviewed.hidden_context_required,
            ),
            tuple(receipts),
        )

    @staticmethod
    def _reconstruction_complete(
        model_input: dict[str, Any],
        assessment: PlanFixtureAssessment,
    ) -> bool:
        if assessment.selected_candidate_ref is None:
            return False
        candidates = model_input["input"].get("candidate_set", [])
        selected = next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and item.get("candidate_ref") == assessment.selected_candidate_ref
            ),
            None,
        )
        if selected is None:
            return False
        work_items = selected.get("work_items", [])
        expected_refs = {
            item.get("work_item_ref")
            for item in work_items
            if isinstance(item, dict) and isinstance(item.get("work_item_ref"), str)
        }
        expected_edges = {
            f"{dependency}->{item['work_item_ref']}"
            for item in work_items
            if isinstance(item, dict) and isinstance(item.get("work_item_ref"), str)
            for dependency in item.get("dependencies", [])
            if isinstance(dependency, str)
        }
        return bool(expected_refs) and all(
            (
                assessment.reconstructed_mission == model_input["input"].get("mission"),
                set(assessment.reconstructed_work_item_refs) == expected_refs,
                set(assessment.reconstructed_dependency_edges) == expected_edges,
                set(assessment.reconstructed_failure_policy_refs) == expected_refs,
                set(assessment.reconstructed_validation_refs) == expected_refs,
                not assessment.hidden_context_required,
            )
        )

    @staticmethod
    def _failed_observation(
        fixture: EvaluationFixture,
        receipts: tuple[ModelCallReceipt, ...],
    ) -> tuple[EvaluationObservation, tuple[ModelCallReceipt, ...]]:
        return (
            EvaluationObservation(
                case_id=fixture.case_id,
                blocked=True,
                mission_resolved=False,
                admitted=False,
                first_pass_schema_valid=False,
                token_usage=sum(_receipt_token_count(item) for item in receipts),
                latency_ms=sum(_receipt_latency(item) for item in receipts),
                forward_reconstruction_complete=False,
            ),
            receipts,
        )


def _resolve_evaluation_models(
    configuration: LiveRoleConfiguration,
    client_factory: Callable[[], Any],
) -> tuple[ResolvedPlanningModel, ...]:
    required = {
        PlanningRole.PURPOSE_RESOLVER,
        PlanningRole.INTENT_REVIEWER,
        PlanningRole.HARD_GATE_REVIEWER,
        PlanningRole.CRITICAL_REVIEWER,
    }
    preference_by_role = {item.role: item for item in configuration.preferences}
    inventory = CodexModelInventoryAdapter(client_factory).list_models()
    missing = required - set(preference_by_role)
    if missing:
        raise ValueError(
            "평가에 필요한 역할 설정이 없습니다: "
            + ", ".join(sorted(item.value for item in missing))
        )
    return tuple(
        resolve_model_role(inventory, preference_by_role[role])
        for role in sorted(required, key=lambda item: item.value)
    )


def run_live_evaluation(
    fixtures: tuple[EvaluationFixture, ...],
    *,
    order_seeds: tuple[int, ...],
    artifact_root: str | Path,
    skill_root: str | Path,
    configuration: LiveRoleConfiguration,
    client_factory: Callable[[], Any],
    policy_evidence: list[ExecutionPolicyEvidence],
) -> LiveEvaluationResult:
    if not fixtures:
        raise ValueError("실제 평가에는 fixture가 하나 이상 필요합니다.")
    if not order_seeds or len(order_seeds) != len(set(order_seeds)):
        raise ValueError("실제 평가 order seed는 하나 이상의 중복 없는 값이어야 합니다.")
    root = Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repository = PlanningArtifactRepository(root)
    resolved_models = _resolve_evaluation_models(configuration, client_factory)
    model_by_role = {item.role: item for item in resolved_models}
    receipts: list[ModelCallReceipt] = []
    traces: list[FixtureAssessmentTrace] = []

    def persist(receipt: ModelCallReceipt) -> None:
        repository.save_model_call_journal_receipt(receipt)
        receipts.append(receipt)

    evaluator = RoleFixtureEvaluator(
        runner=CodexStructuredRoleRunner(client_factory),
        models=model_by_role,
        cwd=root,
        instructions=load_evaluation_role_instructions(skill_root),
        receipt_sink=persist,
        trace_sink=traces.append,
    )
    fixture_by_ref = {item.model_case_ref: item for item in fixtures}
    batches: list[EvaluationObservationBatch] = []
    for seed in order_seeds:
        observations: list[EvaluationObservation] = []
        for model_input in ordered_model_inputs(fixtures, order_seed=seed):
            fixture = fixture_by_ref[model_input["case_ref"]]
            observation, _ = evaluator.evaluate(fixture, model_input)
            observations.append(observation)
        batches.append(
            EvaluationObservationBatch(
                run_id=f"live-role-eval-{seed}",
                order_seed=seed,
                observations=tuple(observations),
            )
        )

    receipt_threads = [item.thread_id for item in receipts if item.thread_id is not None]
    evidence_threads = [
        item.thread_id for item in policy_evidence if item.thread_id is not None
    ]
    if (
        len(receipt_threads) != len(set(receipt_threads))
        or len(evidence_threads) != len(set(evidence_threads))
        or set(receipt_threads) != set(evidence_threads)
    ):
        raise RuntimeError("평가 model call과 실제 실행 권한 증거가 일대일로 다릅니다.")
    report = evaluate_repeated_runs(
        fixtures,
        tuple(batches),
        evaluation_scope=EvaluationScope.ROLE_FIXTURE_PROBE,
    )
    return LiveEvaluationResult(
        configuration_id=configuration.configuration_id,
        configuration_digest=configuration.configuration_digest,
        fixture_ids=tuple(sorted(item.case_id for item in fixtures)),
        batches=tuple(batches),
        report=report,
        model_call_receipts=tuple(receipts),
        execution_policy_evidence=tuple(policy_evidence),
        resolved_models=resolved_models,
        assessment_traces=tuple(traces),
    )


__all__ = [
    "ClauseAssessment",
    "ClauseDisposition",
    "EVALUATION_CONTRACT_VERSION",
    "EvaluationRoleInstructions",
    "FixtureAssessmentTrace",
    "LiveEvaluationResult",
    "MajorDefectCode",
    "MissionMajorDefect",
    "MissionFixtureAssessment",
    "PlanFixtureAssessment",
    "PlanFixtureStatus",
    "RoleFixtureEvaluator",
    "StructuralDefectCode",
    "evaluation_contract_digest",
    "load_evaluation_role_instructions",
    "run_live_evaluation",
]
