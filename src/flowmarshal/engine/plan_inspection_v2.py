from __future__ import annotations

import hashlib
from types import MappingProxyType
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .domain import (
    EngineModel,
    FindingSeverity,
    GateName,
    GoalContractRevision,
    ProjectMapRevision,
    ReviewFinding,
)
from .plan_inspection import InspectionCitation, PlanInspectionError, inspection_file_content
from .planning import validation_comparison_targets


ScopeStatus = Literal["supported", "contradicted", "unresolved"]
DefectKindV2 = Literal[
    "missing_validation_link",
    "validation_scope",
    "missing_task_validation",
    "result_order",
    "insufficient_evidence",
    "other",
]


def _unique(value: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{label}가 중복됐습니다.")
    return value


class InspectionMechanismV2(EngineModel):
    mechanism_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
    tool: str = Field(min_length=1, max_length=120)
    phase: str | None
    direct_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("direct_refs")
    @classmethod
    def refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "mechanism direct_refs")


class ValidationInspectionV2(EngineModel):
    validation_id: str
    mechanisms: tuple[InspectionMechanismV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def mechanism_ids_are_unique(self) -> "ValidationInspectionV2":
        _unique(tuple(item.mechanism_id for item in self.mechanisms), "validation mechanism_id")
        return self


class ValidationScopeInspectionV2(EngineModel):
    scope_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
    validation_id: str
    mechanism_id: str
    claim: str = Field(min_length=1, max_length=1000)
    direct_extra_refs: tuple[str, ...]
    status: ScopeStatus

    @field_validator("direct_extra_refs")
    @classmethod
    def refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "scope direct_extra_refs")


class ACScopeRequirementInspectionV2(EngineModel):
    """모델이 각 AC 원문 필드에서 직접 판단한 필수 supported scope 선택."""

    criterion_id: str
    statement_scope_ids: tuple[str, ...] = Field(
        description="AC statement가 명시적으로 요구하는 실제 검사 절차의 supported scope. 없으면 빈 목록."
    )
    validation_intent_scope_ids: tuple[str, ...] = Field(
        description="AC validation_intent가 명시적으로 요구하는 실제 검사 절차의 supported scope. 없으면 빈 목록."
    )

    @field_validator("statement_scope_ids", "validation_intent_scope_ids")
    @classmethod
    def scopes_are_unique(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _unique(value, f"AC scope requirement {info.field_name}")


class ConstraintTaskInspectionV2(EngineModel):
    constraint_id: str
    task_ref: str
    applicability: Literal["required", "not_applicable"]
    required_validation_ids: tuple[str, ...]

    @field_validator("required_validation_ids")
    @classmethod
    def validations_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "constraint required_validation_ids")


class PlanInspectionV2(EngineModel):
    validation_rows: tuple[ValidationInspectionV2, ...] = Field(min_length=1)
    validation_scope_rows: tuple[ValidationScopeInspectionV2, ...] = Field(min_length=1)
    ac_scope_requirements: tuple[ACScopeRequirementInspectionV2, ...]
    constraint_task_rows: tuple[ConstraintTaskInspectionV2, ...]


class InspectionTargetV2(EngineModel):
    """모델이 선택한 target ID를 adapter가 해석한 내부 typed target."""

    kind: Literal[
        "ac_validation", "validation_scope", "constraint_task", "validation", "task", "citation"
    ]
    primary_ref: str = Field(min_length=1)
    secondary_ref: str | None

    @model_validator(mode="after")
    def reference_arity_matches_kind(self) -> "InspectionTargetV2":
        composite = self.kind in {"ac_validation", "constraint_task"}
        if composite and self.secondary_ref is None:
            raise ValueError(f"{self.kind} target에는 secondary_ref가 필요합니다.")
        if not composite and self.secondary_ref is not None:
            raise ValueError(f"{self.kind} target의 secondary_ref는 null이어야 합니다.")
        return self


class InspectionTargetCatalogEntryV2(EngineModel):
    """모델이 두 참조를 다시 조립하지 않도록 제공하는 고정 복합 target."""

    target_id: str = Field(pattern=r"^target_[0-9a-f]{24}$")
    kind: Literal["ac_validation", "constraint_task"]
    primary_ref: str = Field(min_length=1)
    secondary_ref: str = Field(min_length=1)


class ReviewFindingV2(EngineModel):
    finding_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,79}$")
    defect_kind: DefectKindV2
    gate: GateName | None = Field(
        default=None,
        description=(
            "동결 taxonomy에 없는 other 결함의 직접 gate. 다섯 표준 defect_kind에는 null이며 "
            "adapter가 taxonomy에서 계산한다."
        ),
    )
    severity: FindingSeverity | None = Field(
        default=None,
        description=(
            "동결 taxonomy에 없는 other 결함의 직접 severity. 다섯 표준 defect_kind에는 null이며 "
            "adapter가 taxonomy에서 계산한다."
        ),
    )
    remediable: bool
    primary_target_ids: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "결함 종류가 missing_validation_link 또는 missing_task_validation이면 payload의 "
            "inspection_target_catalog.target_id, validation_scope 또는 insufficient_evidence면 "
            "직접 작성한 scope_id, result_order면 validation_id, other면 citation ID를 쓴다."
        ),
    )
    direct_extra_refs: tuple[str, ...] = Field(
        description="주 target closure 외에 이 finding이 직접 사용한 citation catalog ID."
    )
    direct_task_refs: tuple[str, ...] = Field(
        description="주 target 소유 관계로 계산할 수 없는 직접 영향 Task의 task_ref."
    )

    @field_validator("primary_target_ids", "direct_extra_refs", "direct_task_refs")
    @classmethod
    def direct_ids_are_unique(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _unique(value, f"finding {info.field_name}")

    @model_validator(mode="after")
    def direct_classification_and_targets_are_valid(self) -> "ReviewFindingV2":
        if self.defect_kind == "other":
            if self.gate is None or self.severity is None:
                raise ValueError("other finding은 gate와 severity를 직접 제출해야 합니다.")
        elif self.gate is not None or self.severity is not None:
            raise ValueError("표준 finding의 gate와 severity는 adapter가 계산하므로 null이어야 합니다.")
        return self


class CoverageMembershipWitnessV2(EngineModel):
    criterion_id: str
    validation_id: str
    collection_selector: str
    observed_present: bool
    member_selector: str | None


class ACValidationDecisionV2(EngineModel):
    """희소 양의 의미 연결에서 compiler가 확장한 전체 AC×validation 행."""

    criterion_id: str
    validation_id: str
    ac_link_required: bool
    scope_ids: tuple[str, ...]


class InspectionRowClosureV2(EngineModel):
    row_kind: Literal[
        "validation", "mechanism", "validation_scope", "ac_validation", "constraint_task", "finding"
    ]
    row_id: str
    citation_ids: tuple[str, ...]


class ResolvedFindingTargetsV2(EngineModel):
    finding_code: str
    target_refs: tuple[InspectionTargetV2, ...] = Field(min_length=1)


class CompiledPlanInspectionV2(EngineModel):
    ac_validation_decisions: tuple[ACValidationDecisionV2, ...]
    derived_findings: tuple[ReviewFinding, ...]
    resolved_finding_targets: tuple[ResolvedFindingTargetsV2, ...]
    row_closures: tuple[InspectionRowClosureV2, ...]
    membership_witnesses: tuple[CoverageMembershipWitnessV2, ...]
    used_citations: tuple[InspectionCitation, ...]


FINDING_TAXONOMY_V2 = MappingProxyType({
    "missing_validation_link": (GateName.VERIFICATION, FindingSeverity.ERROR),
    "validation_scope": (GateName.VERIFICATION, FindingSeverity.ERROR),
    "missing_task_validation": (GateName.VERIFICATION, FindingSeverity.ERROR),
    "result_order": (GateName.EXECUTION, FindingSeverity.ERROR),
    "insufficient_evidence": (GateName.VERIFICATION, FindingSeverity.ERROR),
})


TASK_RESULT_FIELD_SEMANTICS_V2 = MappingProxyType({
    "produces": (
        "검증까지 포함한 Task 전체가 제공하는 논리적 산출물 key다. Worker 응답의 필수 항목 "
        "목록이나 Worker 단독 작성 책임을 뜻하지 않는다. 독립 Validator의 별도 결과도 Task "
        "산출물로 선언할 수 있으며, key 이름만으로 작성 주체·제출 시점·검사 입력을 정하지 않는다."
    ),
    "acceptance_criteria": (
        "Worker 작업·응답 제출 이후 Task 검증까지 포함한 완료 조건이다. Worker가 직접 제출할 "
        "내용과 독립 Validator가 이후 제출할 내용은 조건 원문의 주체·시점으로 구분한다."
    ),
    "validations": (
        "Worker 제출 뒤 Core가 수행하는 Task 검증이다. 독립 Validator는 직접 evidence를 입력으로 "
        "별도 결과를 제출하고 Core가 Task 완료를 판정한다. 실제 입력 요구는 각 statement에 따른다."
    ),
})


PLAN_INSPECTION_V2_INSTRUCTIONS = (
    "응답은 기존 plan 또는 review와 inspection을 감싼 plan-inspection-v2 전용 envelope다. "
    "inspection은 의미 판단에 필요한 원자 관측만 제출하고 반복 가능한 참조 closure는 쓰지 않는다. "
    "원문 citation 객체를 다시 쓰지 않는다. 요청의 inspection_citation_catalog는 adapter가 원문에서 "
    "고정한 비권위 후보이며, 모델은 실제 판단에 직접 사용한 citation ID만 direct_refs에서 선택한다. "
    "task_result_field_semantics는 기존 Task 필드의 책임 범위를 설명하는 입력이며 특정 Task의 "
    "정상·결함 판정은 아니다. produces의 논리적 산출물 선언과 Worker 응답의 제출 요구를 구분한다. "
    "result_order finding에는 결과가 필요한 시점·주체를 명시한 계약 원문과 그 결과를 이후 생성하는 "
    "검사의 입력을 함께 대조해 직접 근거를 선택한다. 필드 설명을 finding evidence로 인용하거나 "
    "실제 Worker 선제 제출 요구를 정상적인 Task 산출물 선언으로 바꾸지 않는다. "
    "모든 Task·integration validation마다 validation_rows를 정확히 하나 만든다. validation statement와 "
    "Goal AC·constraint의 고정 claim citation은 ID join으로 adapter가 붙인다. mechanisms에는 실제 확인한 "
    "tool·phase와 그 판단에 직접 필요한 catalog citation ID만 "
    "direct_refs로 쓴다. mechanism_id는 응답 전체에서 유일해야 한다. "
    "각 validation의 실제 검사 책임을 최소한의 validation_scope_rows로 나누고, 각 scope가 실제로 검사하는 "
    "절차·주장을 claim에 명시한다. 같은 mechanism·status로 함께 판정되는 책임은 하나의 scope로 합치고, "
    "실제 절차나 status가 다를 때만 나눈다. AC마다 scope를 다시 만들거나 citation별로 쪼개지 않는다. 같은 validation의 "
    "mechanism_id와 supported·contradicted·unresolved 중 하나를 직접 판단한다. "
    "mechanism의 근거를 direct_extra_refs에 반복하지 말고 해당 scope에만 추가로 필요한 citation만 쓴다. "
    "AC의 검사 요구는 Plan의 현재 연결을 근거로 정하지 않는다. 각 AC의 statement와 validation_intent를 "
    "전체 문맥으로 읽되 각 필드가 명시한 검사 의무를 구분한다. 두 필드는 상호 보완하며 한 필드의 단계·독립성 "
    "설명이 다른 필드의 명시적 절차를 면제하지 않는다. ac_scope_requirements에는 모든 AC를 정확히 한 행씩 "
    "쓰고 criterion_id, statement_scope_ids, validation_intent_scope_ids를 제출한다. 각 목록에는 해당 원문 "
    "필드가 명시적으로 요구하는 실제 절차의 supported scope만 선택한다. 해당 필드에 검사 절차 요구가 없으면 "
    "빈 목록을 쓴다. 두 필드가 같은 절차를 요구하면 같은 scope를 양쪽 목록에서 선택할 수 있다. 대명사나 "
    "축약 표현은 AC 전체 문맥으로 해석하며, 결과·주제의 관련성만으로 절차를 추가하지 않는다. "
    "요구한 절차·도구·phase·Task 또는 integration 범위가 일치해야 한다. 동일 절차의 task/goal phase를 "
    "각각 명시하면 각 phase의 실제 scope를 선택한다. 단계에 검사 책임을 열거하면 그 단계에서 해당 책임을 "
    "실제 수행하는 scope를 선택한다. 독립 실행이나 완료 순서만 나타내는 표현은 다른 단계의 모든 검사 "
    "의무가 아니며, 같은 Task·phase·evidence·주제만으로 열거되지 않은 sibling에 전파하지 않는다. "
    "한 scope의 모순은 실제 수행되는 다른 절차의 명시적 의무를 없애지 않는다. 같은 요구 절차를 여러 "
    "validation이 실제 수행하면 각 소유 scope를 선택하며 별도 validation의 존재만으로 복합 validation "
    "안의 실제 절차를 생략하지 않는다. contradicted·unresolved scope는 선택하지 않는다. Adapter가 두 원문 "
    "목록의 합집합과 scope의 소유 validation을 join해 전체 AC×validation의 true/false 행렬·scope_ids·근거 "
    "closure를 파생한다. 원문 목록의 선택을 다시 합쳐 쓰거나 false 조합을 나열하지 않는다. "
    "모든 전역 constraint×Task 조합도 constraint_task_rows에 정확히 한 번씩 제출한다. Task 검사가 직접 "
    "요구되면 applicability=required와 실제 Task validation ID를 쓰고, 그렇지 않으면 not_applicable과 빈 "
    "required_validation_ids를 쓴다. AC 관계와 전역 Task 의무를 서로 추정하지 않는다. "
    "Reviewer finding은 finding_code·defect_kind·gate·severity·remediable·primary_target_ids·direct_extra_refs·"
    "direct_task_refs를 제출한다. "
    "다섯 표준 defect_kind의 gate·severity는 null이고 adapter가 taxonomy에서 계산한다. 표준 taxonomy로 "
    "표현할 수 없는 직접 결함은 defect_kind=other와 직접 gate·severity를 사용한다. "
    "missing_validation_link와 missing_task_validation의 primary_target_ids는 payload의 inspection_target_catalog에서 "
    "각각 ac_validation·constraint_task target_id를 선택한다. validation_scope와 insufficient_evidence는 직접 만든 "
    "scope_id, result_order는 validation_id, other는 citation ID를 쓴다. target kind·primary·secondary 참조를 다시 "
    "작성하지 않는다. 직접 관련된 추가 citation은 direct_extra_refs, 소유 관계로 계산할 수 없는 영향 Task는 "
    "direct_task_refs에 쓴다. 영향 Task는 주 target 소유 관계와 이 직접 Task 선택에서 adapter가 계산한다. 표준 "
    "finding의 gate·severity와 모든 finding의 summary·evidence_refs·finding_links는 adapter가 taxonomy와 "
    "target closure에서 파생한다. "
    "모델은 scope claim·status, 양의 AC scope 선택, finding 종류·target ID 또는 direct evidence 선택을 adapter가 "
    "채울 것이라고 "
    "가정하지 않는다. "
    "Adapter는 이 의미 판단을 생성·삭제·교정하지 않으며 존재와 closure만 검증한다. "
    "Expander 응답에는 finding branch가 없다. 생성 Plan에 contradicted/unresolved scope, 필수 coverage 누락 또는 "
    "필수 Task validation 누락이 있으면 출력으로 숨기지 말고 유효한 Plan을 작성한다. Reviewer는 findings와 "
    "ratings 두 key를 모두 제출한다. finding이 없으면 다섯 rating을 모두 쓰고, finding이 하나 이상이면 "
    "ratings:null로 둔다. status·admissible·score·weakest task와 장황한 사고 과정은 제출하지 않는다."
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanInspectionError(message)


def _pointer(value: Any, selector: str) -> Any:
    _require(selector.startswith("/"), "v2 대조표 selector는 JSON pointer여야 합니다.")
    try:
        for token in selector[1:].split("/"):
            _require(re.search(r"~(?![01])", token) is None, "v2 대조표 selector escape 오류")
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, (list, tuple)):
                _require(re.fullmatch(r"0|[1-9][0-9]*", token) is not None, "v2 대조표 배열 selector 오류")
                value = value[int(token)]
            else:
                value = value[token]
        return value
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise PlanInspectionError("v2 대조표 selector가 원문에 없습니다.") from error


def _pointer_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _string_leaves(value: Any, selector: str = ""):
    if isinstance(value, str):
        if value:
            yield selector or "/", value
        return
    if isinstance(value, dict):
        for key in sorted(value, key=str):
            yield from _string_leaves(
                value[key], f"{selector}/{_pointer_token(key)}"
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _string_leaves(item, f"{selector}/{index}")


def _selected_string_leaves(
        value: Any, selectors: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """원본 selector를 유지하며 명시적으로 선택한 의미 문자열만 투영한다."""
    rows: list[tuple[str, str]] = []
    for selector in selectors:
        selected = _pointer(value, selector)
        if isinstance(selected, str):
            if selected:
                rows.append((selector, selected))
        elif isinstance(selected, (list, tuple)):
            for index, item in enumerate(selected):
                if isinstance(item, str) and item:
                    rows.append((f"{selector}/{index}", item))
    return tuple(rows)


def _goal_semantic_leaves(value: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Goal provenance 장부를 제외하고 사용자가 판단할 의미만 고른다."""
    selectors = ["/source_request", "/observable_outcome", "/non_goals"]
    for index, _item in enumerate(value.get("hard_acceptance", ())):
        selectors.extend((
            f"/hard_acceptance/{index}/statement",
            f"/hard_acceptance/{index}/validation_intent",
        ))
    for index, _item in enumerate(value.get("quality_preferences", ())):
        selectors.append(f"/quality_preferences/{index}/statement")
    for index, _item in enumerate(value.get("constraints", ())):
        selectors.extend((
            f"/constraints/{index}/category",
            f"/constraints/{index}/statement",
        ))
    for index, _item in enumerate(value.get("assumptions", ())):
        selectors.append(f"/assumptions/{index}/statement")
    for index, _item in enumerate(value.get("unresolved_questions", ())):
        selectors.extend((
            f"/unresolved_questions/{index}/question",
            f"/unresolved_questions/{index}/impact",
        ))
    effect_policy = value.get("effect_policy", {})
    if isinstance(effect_policy, dict):
        selectors.extend((
            "/effect_policy/allowed_external_effects",
            "/effect_policy/prohibited_effects",
        ))
    return _selected_string_leaves(value, tuple(selectors))


def _skeleton_semantic_leaves(value: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Skeleton의 의미·위험·입출력을 남기고 ID·digest 장부는 제외한다."""
    selectors: list[str] = []
    approach = value.get("approach", {})
    if isinstance(approach, dict):
        selectors.extend(f"/approach/{key}" for key in (
            "strategy_family", "change_shape", "compatibility", "rollout_recovery"
        ))
    for index, _item in enumerate(value.get("tasks", ())):
        selectors.extend(
            f"/tasks/{index}/{key}" for key in (
                "objective", "produces", "consumes", "risk_tags",
                "required_capabilities", "no_op_when", "unknown_refs", "detail_requirements",
            )
        )
    for index, _item in enumerate(value.get("dependencies", ())):
        selectors.extend(
            f"/dependencies/{index}/{key}" for key in ("produces", "consumes")
        )
    selectors.append("/unknowns")
    return _selected_string_leaves(value, tuple(selectors))


def _plan_semantic_leaves(value: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Plan의 계약 문장을 남기고 기계적 ID·digest·membership 장부는 제외한다."""
    is_revision = isinstance(value.get("definition"), dict)
    definition = value["definition"] if is_revision else value
    prefix = "/definition" if is_revision else ""
    selectors: list[str] = []
    for index, task in enumerate(definition.get("tasks", ())):
        base = f"{prefix}/tasks/{index}"
        selectors.extend(f"{base}/{key}" for key in (
            "objective", "produces", "consumes", "required_capabilities",
            "acceptance_criteria", "risk_tags",
        ))
        for item_index, _item in enumerate(task.get("preconditions", ())):
            selectors.append(f"{base}/preconditions/{item_index}/statement")
        for collection in ("expected_effects", "prohibited_effects"):
            for item_index, _item in enumerate(task.get(collection, ())):
                selectors.append(f"{base}/{collection}/{item_index}/statement")
        for item_index, _item in enumerate(task.get("validations", ())):
            selectors.append(f"{base}/validations/{item_index}/statement")
    for index, _item in enumerate(definition.get("dependencies", ())):
        selectors.append(f"{prefix}/dependencies/{index}/products")
    for index, _item in enumerate(definition.get("integration_validations", ())):
        selectors.append(f"{prefix}/integration_validations/{index}/statement")
    selectors.extend((f"{prefix}/expected_effects", f"{prefix}/prohibited_effects"))
    return _selected_string_leaves(value, tuple(selectors))


def _semantic_citation_leaves_v2(
        evidence_catalog: dict[str, Any], project_map: ProjectMapRevision,
) -> tuple[tuple[str, str, str], ...]:
    """Reviewer 의미 판단에 필요한 원문만 catalog 후보로 투영한다.

    State·ProjectMap의 revision/digest 결속은 Core가 결정적으로 검사한다. 이 함수는
    그 기계 메타데이터와 Goal source trace 장부를 모델의 직접 evidence 후보로 만들지 않는다.
    """
    rows: list[tuple[str, str, str]] = []
    for source_ref in sorted(evidence_catalog):
        value = evidence_catalog[source_ref]
        if not isinstance(value, dict):
            continue
        if source_ref == "source:goal":
            leaves = _goal_semantic_leaves(value)
        elif source_ref == "artifact:skeleton":
            leaves = _skeleton_semantic_leaves(value)
        elif source_ref in {"artifact:plan_contract", "artifact:plan_draft"}:
            leaves = _plan_semantic_leaves(value)
        else:
            # source:state와 source:project_map을 포함한 운영 장부는 여기서 제외한다.
            continue
        rows.extend((source_ref, selector, text) for selector, text in leaves)
    for entry in sorted(project_map.entries, key=lambda item: item.entry_id):
        if entry.kind.value in {"reference", "instruction"}:
            rows.append((
                f"project:{entry.entry_id}", "/content",
                inspection_file_content(entry, project_map),
            ))
    return tuple(rows)


def _quote_segments(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    if len(value) <= 5000:
        candidates.append(value)
    for block in re.split(r"\n\s*\n", value):
        block = block.strip()
        if not block or block == value:
            continue
        if len(block) <= 5000:
            candidates.append(block)
        else:
            candidates.extend(
                block[index:index + 5000]
                for index in range(0, len(block), 5000)
            )
    if not candidates:
        candidates.extend(
            value[index:index + 5000]
            for index in range(0, len(value), 5000)
            if value[index:index + 5000]
        )
    return tuple(dict.fromkeys(candidates))


def _citation_catalog_from_sources(sources: dict[str, Any]) -> tuple[InspectionCitation, ...]:
    return _citation_catalog_from_leaves(
        (source_ref, selector, value)
        for source_ref in sorted(sources)
        for selector, value in _string_leaves(sources[source_ref])
    )


def _citation_catalog_from_leaves(
        leaves: Any,
) -> tuple[InspectionCitation, ...]:
    rows: list[InspectionCitation] = []
    seen_ids: dict[str, tuple[str, str, str]] = {}
    seen_values: set[tuple[str, str, str]] = set()
    for source_ref, selector, value in leaves:
        for quote in _quote_segments(value):
            identity = (source_ref, selector, quote)
            if identity in seen_values:
                continue
            seen_values.add(identity)
            digest = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
            citation_id = f"cite_{digest[:32]}"
            previous = seen_ids.get(citation_id)
            if previous is not None and previous != identity:
                raise PlanInspectionError("v2 citation catalog ID 충돌")
            seen_ids[citation_id] = identity
            rows.append(InspectionCitation(
                citation_id=citation_id,
                source_ref=source_ref,
                selector=selector,
                quote=quote,
            ))
    return tuple(rows)


def plan_inspection_citation_catalog_v2(
        evidence_catalog: dict[str, Any], project_map: ProjectMapRevision,
) -> tuple[InspectionCitation, ...]:
    """의미 원문을 바꾸지 않고 모델이 선택할 수 있는 고정 citation 후보를 만든다."""
    return _citation_catalog_from_leaves(
        _semantic_citation_leaves_v2(evidence_catalog, project_map)
    )


def plan_inspection_target_catalog_v2(
        goal: GoalContractRevision, plan: Any,
) -> tuple[InspectionTargetCatalogEntryV2, ...]:
    """복합 finding target의 두 참조를 하나의 고정 선택 ID로 투영한다."""
    targets = validation_comparison_targets(goal, plan)
    identities = [
        ("ac_validation", row["criterion_id"], row["validation_id"])
        for row in targets["ac_validation_pairs"]
    ] + [
        ("constraint_task", row["constraint_id"], row["task_ref"])
        for row in targets["constraint_task_pairs"]
    ]
    rows: list[InspectionTargetCatalogEntryV2] = []
    seen: dict[str, tuple[str, str, str]] = {}
    for kind, primary_ref, secondary_ref in identities:
        identity = (kind, primary_ref, secondary_ref)
        digest = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
        target_id = f"target_{digest[:24]}"
        previous = seen.get(target_id)
        if previous is not None and previous != identity:
            raise PlanInspectionError("v2 target catalog ID 충돌")
        seen[target_id] = identity
        rows.append(InspectionTargetCatalogEntryV2(
            target_id=target_id,
            kind=kind,
            primary_ref=primary_ref,
            secondary_ref=secondary_ref,
        ))
    return tuple(rows)


def _ordered_union(*groups: Any) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if value not in seen:
                seen.add(value)
                result.append(value)
    return tuple(result)


def _target_key(target: InspectionTargetV2) -> str:
    values = (target.kind, target.primary_ref, target.secondary_ref)
    return ":".join(str(value) for value in values if value is not None)


def compile_plan_inspection_v2(
    inspection: PlanInspectionV2,
    *,
    findings: tuple[ReviewFindingV2, ...],
    plan: Any,
    goal: GoalContractRevision,
    project_map: ProjectMapRevision,
    evidence_catalog: dict[str, Any],
    citation_catalog: tuple[InspectionCitation, ...],
    target_catalog: tuple[InspectionTargetCatalogEntryV2, ...] | None = None,
) -> CompiledPlanInspectionV2:
    """직접 제출된 의미 판단을 검증하고 반복 인용 closure만 결정적으로 파생한다."""
    before_inspection = inspection.model_dump(mode="json")
    before_findings = tuple(item.model_dump(mode="json") for item in findings)
    definition = getattr(plan, "definition", plan)
    is_revision = hasattr(plan, "definition")
    plan_ref = "artifact:plan_contract" if is_revision else "artifact:plan_draft"
    prefix = "/definition" if is_revision else ""
    sources = dict(evidence_catalog)
    sources[plan_ref] = plan.model_dump(mode="json")

    entries = {f"project:{entry.entry_id}": entry for entry in project_map.entries}
    expected_catalog = plan_inspection_citation_catalog_v2(evidence_catalog, project_map)
    _require(
        citation_catalog == expected_catalog,
        "v2 citation catalog 입력 결속 불일치",
    )
    for source_ref, entry in entries.items():
        if entry.kind.value in {"reference", "instruction"}:
            sources[source_ref] = {"content": inspection_file_content(entry, project_map)}
    selectable_citations = {item.citation_id: item for item in citation_catalog}
    _require(
        len(selectable_citations) == len(citation_catalog),
        "v2 citation catalog ID 중복",
    )
    automatic = _citation_catalog_from_sources({
        "source:goal": goal.definition.model_dump(mode="json"),
        plan_ref: plan.model_dump(mode="json"),
    })
    citations = dict(selectable_citations)
    for item in automatic:
        previous = citations.get(item.citation_id)
        _require(
            previous is None or previous == item,
            "v2 citation catalog ID 충돌",
        )
        citations[item.citation_id] = item
    for citation in citations.values():
        if citation.source_ref in entries:
            sources[citation.source_ref] = {
                "content": inspection_file_content(entries[citation.source_ref], project_map)
            }
        _require(citation.source_ref in sources, "v2 대조표 source_ref가 입력에 없습니다.")
        selected = _pointer(sources[citation.source_ref], citation.selector)
        _require(
            isinstance(selected, str) and citation.quote in selected,
            "v2 대조표 인용이 선택한 원문 문자열과 일치하지 않습니다.",
        )

    used: set[str] = set()

    def direct_refs(values: tuple[str, ...], context: str) -> tuple[str, ...]:
        _require(len(values) == len(set(values)), f"v2 대조표 직접 인용 중복: {context}")
        missing = set(values) - selectable_citations.keys()
        _require(not missing, f"v2 대조표에 없는 인용 ID: {context}, missing_refs={sorted(missing)}")
        used.update(values)
        return values

    def source_citation(source_ref: str, selector: str, context: str, *, full_quote: bool) -> str:
        selected = _pointer(sources[source_ref], selector)
        matches = [item.citation_id for item in citations.values()
                   if item.source_ref == source_ref and item.selector == selector and
                   (not full_quote or item.quote == selected)]
        qualifier = "전체 " if full_quote else ""
        _require(len(matches) == 1, f"v2 대조표 {qualifier}원문 인용 누락 또는 중복: {context}")
        used.add(matches[0])
        return matches[0]

    targets = validation_comparison_targets(goal, plan)
    expected_target_catalog = plan_inspection_target_catalog_v2(goal, plan)
    if target_catalog is not None:
        _require(target_catalog == expected_target_catalog, "v2 target catalog 입력 결속 불일치")
    elif findings:
        raise PlanInspectionError("v2 Reviewer finding target catalog가 없습니다.")
    target_catalog_by_id = {row.target_id: row for row in expected_target_catalog}
    _require(
        len(target_catalog_by_id) == len(expected_target_catalog),
        "v2 target catalog ID 중복",
    )
    validation_by_id = {row["validation_id"]: row for row in targets["validations"]}
    _require(len(validation_by_id) == len(targets["validations"]), "v2 입력 validation ID 중복")
    task_by_ref = {task.task_ref: task for task in definition.tasks}
    criterion_selectors = {
        item.criterion_id: {
            "statement": f"/hard_acceptance/{index}/statement",
            "validation_intent": f"/hard_acceptance/{index}/validation_intent",
        }
        for index, item in enumerate(goal.definition.hard_acceptance)
    }
    constraint_selectors = {
        item.constraint_id: f"/constraints/{index}/statement"
        for index, item in enumerate(goal.definition.constraints)
    }

    validation_rows = {row.validation_id: row for row in inspection.validation_rows}
    _require(len(validation_rows) == len(inspection.validation_rows), "v2 검사 능력 행 중복")
    _require(validation_rows.keys() == validation_by_id.keys(), "v2 검사 능력 행 집합 불완전")
    mechanism_by_id: dict[str, tuple[str, InspectionMechanismV2]] = {}
    validation_closures: dict[str, tuple[str, ...]] = {}
    row_closures: list[InspectionRowClosureV2] = []
    for validation_id, row in validation_rows.items():
        selector = validation_by_id[validation_id]["selector"] + "/statement"
        claim_ref = source_citation(
            plan_ref, selector, f"validation_id={validation_id}", full_quote=True
        )
        mechanism_refs: list[tuple[str, ...]] = []
        for mechanism in row.mechanisms:
            _require(mechanism.mechanism_id not in mechanism_by_id, "v2 mechanism_id가 전역 중복됐습니다.")
            refs = direct_refs(mechanism.direct_refs, f"mechanism_id={mechanism.mechanism_id}")
            mechanism_by_id[mechanism.mechanism_id] = (validation_id, mechanism)
            mechanism_refs.append(refs)
            row_closures.append(InspectionRowClosureV2(
                row_kind="mechanism", row_id=mechanism.mechanism_id, citation_ids=refs
            ))
        closure = _ordered_union((claim_ref,), *mechanism_refs)
        validation_closures[validation_id] = closure
        row_closures.append(InspectionRowClosureV2(
            row_kind="validation", row_id=validation_id, citation_ids=closure
        ))

    scope_by_id = {row.scope_id: row for row in inspection.validation_scope_rows}
    _require(len(scope_by_id) == len(inspection.validation_scope_rows), "v2 검사 scope ID 중복")
    scope_validation_ids = {row.validation_id for row in inspection.validation_scope_rows}
    _require(scope_validation_ids == validation_by_id.keys(), "v2 검사 scope 행 집합 불완전")
    scope_closures: dict[str, tuple[str, ...]] = {}
    project_refs_by_validation = {
        validation_id: {ref for ref in closure if citations[ref].source_ref in entries}
        for validation_id, closure in validation_closures.items()
    }
    for scope_id, row in scope_by_id.items():
        _require(row.validation_id in validation_by_id, f"v2 scope validation ID 오류: {scope_id}")
        mechanism_binding = mechanism_by_id.get(row.mechanism_id)
        _require(
            mechanism_binding is not None and mechanism_binding[0] == row.validation_id,
            f"v2 scope mechanism 소유 validation 불일치: scope_id={scope_id}",
        )
        validation_selector = validation_by_id[row.validation_id]["selector"] + "/statement"
        claim_ref = source_citation(
            plan_ref, validation_selector, f"scope_id={scope_id}", full_quote=True
        )
        extras = direct_refs(row.direct_extra_refs, f"scope_id={scope_id}")
        mechanism = mechanism_binding[1]
        closure = _ordered_union((claim_ref,), mechanism.direct_refs, extras)
        scope_closures[scope_id] = closure
        project_refs_by_validation[row.validation_id].update(
            ref for ref in closure if citations[ref].source_ref in entries
        )
        row_closures.append(InspectionRowClosureV2(
            row_kind="validation_scope", row_id=scope_id, citation_ids=closure
        ))

    expected_ac_rows = targets["ac_validation_pairs"]
    expected_ac_keys = tuple((row["criterion_id"], row["validation_id"]) for row in expected_ac_rows)
    _require(len(expected_ac_keys) == len(set(expected_ac_keys)), "v2 입력 AC 검사 조합 중복")
    requirements_by_criterion = {
        row.criterion_id: row for row in inspection.ac_scope_requirements
    }
    _require(
        len(requirements_by_criterion) == len(inspection.ac_scope_requirements),
        "v2 AC scope requirement criterion 중복",
    )
    unknown_criteria = set(requirements_by_criterion) - set(criterion_selectors)
    _require(
        not unknown_criteria,
        f"v2 AC scope requirement criterion 오류: unknown={sorted(unknown_criteria)}",
    )
    _require(
        requirements_by_criterion.keys() == criterion_selectors.keys(),
        "v2 AC scope requirement 행 집합 불완전: 모든 AC의 두 원문 필드를 판정해야 합니다.",
    )
    scopes_by_pair: dict[tuple[str, str], list[str]] = {}
    for criterion_id, requirement in requirements_by_criterion.items():
        for scope_id in _ordered_union(
            requirement.statement_scope_ids, requirement.validation_intent_scope_ids
        ):
            _require(scope_id in scope_by_id, f"v2 AC requirement scope ID 오류: {scope_id}")
            scope = scope_by_id[scope_id]
            _require(
                scope.status == "supported",
                "v2 양의 AC requirement에는 supported scope가 필요합니다.",
            )
            scopes_by_pair.setdefault((criterion_id, scope.validation_id), []).append(scope_id)
    derived_ac_rows = tuple(
        ACValidationDecisionV2(
            criterion_id=criterion_id,
            validation_id=validation_id,
            scope_ids=tuple(scopes_by_pair.get((criterion_id, validation_id), ())),
            ac_link_required=(criterion_id, validation_id) in scopes_by_pair,
        )
        for criterion_id, validation_id in expected_ac_keys
    )
    ac_pairs = {(row.criterion_id, row.validation_id): row for row in derived_ac_rows}
    coverage = {item.criterion_id: item for item in definition.goal_coverage}
    ac_closures: dict[tuple[str, str], tuple[str, ...]] = {}
    witnesses: dict[tuple[str, str], CoverageMembershipWitnessV2] = {}
    for (criterion_id, validation_id), row in ac_pairs.items():
        selectors = criterion_selectors[criterion_id]
        required_refs = (
            source_citation(
                "source:goal", selectors["statement"], f"criterion_id={criterion_id} statement",
                full_quote=False,
            ),
            source_citation(
                "source:goal", selectors["validation_intent"], f"criterion_id={criterion_id} intent",
                full_quote=False,
            ),
            source_citation(
                plan_ref, validation_by_id[validation_id]["selector"] + "/statement",
                f"validation_id={validation_id} statement", full_quote=True,
            ),
        )
        selected_scopes = []
        for scope_id in row.scope_ids:
            _require(scope_id in scope_by_id, f"v2 AC scope ID 오류: {scope_id}")
            scope = scope_by_id[scope_id]
            _require(scope.validation_id == validation_id, "v2 AC scope validation 불일치")
            selected_scopes.append(scope)
        _require(
            row.ac_link_required == bool(selected_scopes)
            and all(item.status == "supported" for item in selected_scopes),
            "v2 파생 AC 연결과 supported scope가 일치하지 않습니다.",
        )
        closure = _ordered_union(
            required_refs,
            tuple(sorted(project_refs_by_validation[validation_id])),
            *(scope_closures[item.scope_id] for item in selected_scopes),
        )
        used.update(closure)
        ac_closures[(criterion_id, validation_id)] = closure
        coverage_row = coverage.get(criterion_id)
        _require(coverage_row is not None, f"v2 Plan goal coverage 누락: {criterion_id}")
        validation_ids = tuple(coverage_row.validation_ids)
        present = validation_id in validation_ids
        collection = f"{prefix}/goal_coverage/{tuple(coverage).index(criterion_id)}/validation_ids"
        witnesses[(criterion_id, validation_id)] = CoverageMembershipWitnessV2(
            criterion_id=criterion_id,
            validation_id=validation_id,
            collection_selector=collection,
            observed_present=present,
            member_selector=f"{collection}/{validation_ids.index(validation_id)}" if present else None,
        )
        row_closures.append(InspectionRowClosureV2(
            row_kind="ac_validation", row_id=f"{criterion_id}:{validation_id}", citation_ids=closure
        ))

    constraint_pairs = {(row.constraint_id, row.task_ref): row for row in inspection.constraint_task_rows}
    _require(len(constraint_pairs) == len(inspection.constraint_task_rows), "v2 constraint Task 행 중복")
    expected_constraint_pairs = {(row["constraint_id"], row["task_ref"])
                                 for row in targets["constraint_task_pairs"]}
    _require(constraint_pairs.keys() == expected_constraint_pairs, "v2 constraint Task 행 집합 불완전")
    constraint_closures: dict[tuple[str, str], tuple[str, ...]] = {}
    for (constraint_id, task_ref), row in constraint_pairs.items():
        constraint_ref = source_citation(
            "source:goal", constraint_selectors[constraint_id], f"constraint_id={constraint_id}",
            full_quote=False,
        )
        task_validation_ids = {item.validation_id for item in task_by_ref[task_ref].validations}
        _require(set(row.required_validation_ids) <= task_validation_ids,
                 "v2 constraint 행이 다른 Task 또는 없는 validation을 참조합니다.")
        if row.applicability == "not_applicable":
            _require(not row.required_validation_ids,
                     "v2 not_applicable constraint 행의 required_validation_ids는 비어야 합니다.")
        closure = _ordered_union((constraint_ref,),
                                 *(validation_closures[item] for item in row.required_validation_ids))
        used.update(closure)
        constraint_closures[(constraint_id, task_ref)] = closure
        row_closures.append(InspectionRowClosureV2(
            row_kind="constraint_task", row_id=f"{constraint_id}:{task_ref}", citation_ids=closure
        ))

    finding_by_code = {item.finding_code: item for item in findings}
    _require(len(finding_by_code) == len(findings), "v2 finding_code 중복")
    allowed_primary = {
        "missing_validation_link": "ac_validation",
        "validation_scope": "validation_scope",
        "missing_task_validation": "constraint_task",
        "result_order": "validation",
        "insufficient_evidence": "validation_scope",
        "other": "citation",
    }
    derived_findings: list[ReviewFinding] = []
    resolved_finding_targets: list[ResolvedFindingTargetsV2] = []
    finding_targets: dict[str, set[str]] = {}
    for finding in findings:
        primary = allowed_primary[finding.defect_kind]
        primary_targets: list[InspectionTargetV2] = []
        for target_id in finding.primary_target_ids:
            if primary in {"ac_validation", "constraint_task"}:
                catalog_target = target_catalog_by_id.get(target_id)
                _require(
                    catalog_target is not None and catalog_target.kind == primary,
                    f"v2 finding 복합 target ID 종류 불일치: {finding.finding_code}, target_id={target_id}",
                )
                primary_targets.append(InspectionTargetV2(
                    kind=catalog_target.kind,
                    primary_ref=catalog_target.primary_ref,
                    secondary_ref=catalog_target.secondary_ref,
                ))
            else:
                primary_targets.append(InspectionTargetV2(
                    kind=primary,
                    primary_ref=target_id,
                    secondary_ref=None,
                ))
        target_refs = tuple(primary_targets) + tuple(
            InspectionTargetV2(kind="citation", primary_ref=ref, secondary_ref=None)
            for ref in finding.direct_extra_refs
        ) + tuple(
            InspectionTargetV2(kind="task", primary_ref=ref, secondary_ref=None)
            for ref in finding.direct_task_refs
        )
        target_keys = tuple(_target_key(item) for item in target_refs)
        _require(
            len(target_keys) == len(set(target_keys)),
            f"v2 finding target 중복: {finding.finding_code}",
        )
        resolved_finding_targets.append(ResolvedFindingTargetsV2(
            finding_code=finding.finding_code,
            target_refs=target_refs,
        ))
        closure_parts: list[tuple[str, ...]] = []
        affected: set[str] = set()
        labels: list[str] = []
        for target in target_refs:
            labels.append(_target_key(target))
            if target.kind == "ac_validation":
                key = (target.primary_ref, target.secondary_ref)
                _require(key in ac_closures, "v2 finding AC target이 없습니다.")
                closure_parts.append(ac_closures[key])
                owner = validation_by_id[target.secondary_ref]["task_ref"]
                if owner is not None:
                    affected.add(owner)
            elif target.kind == "validation_scope":
                _require(target.primary_ref in scope_closures, "v2 finding scope target이 없습니다.")
                closure_parts.append(scope_closures[target.primary_ref])
                owner = validation_by_id[scope_by_id[target.primary_ref].validation_id]["task_ref"]
                if owner is not None:
                    affected.add(owner)
            elif target.kind == "constraint_task":
                key = (target.primary_ref, target.secondary_ref)
                _require(key in constraint_closures, "v2 finding constraint target이 없습니다.")
                closure_parts.append(constraint_closures[key])
                affected.add(target.secondary_ref)
            elif target.kind == "validation":
                _require(target.primary_ref in validation_closures, "v2 finding validation target이 없습니다.")
                closure_parts.append(validation_closures[target.primary_ref])
                owner = validation_by_id[target.primary_ref]["task_ref"]
                if owner is not None:
                    affected.add(owner)
            elif target.kind == "task":
                _require(target.primary_ref in task_by_ref, "v2 finding Task target이 없습니다.")
                affected.add(target.primary_ref)
            else:
                _require(target.primary_ref in citations, "v2 finding citation target이 없습니다.")
                direct_refs((target.primary_ref,), f"finding={finding.finding_code}")
                closure_parts.append((target.primary_ref,))
        closure = _ordered_union(*closure_parts)
        used.update(closure)
        evidence_refs = _ordered_union(*(
            ("source:project_map" if citations[ref].source_ref in entries else citations[ref].source_ref,)
            for ref in closure
        ))
        missing_evidence = set(evidence_refs) - evidence_catalog.keys()
        _require(not missing_evidence,
                 f"v2 finding evidence catalog 누락: {finding.finding_code}, missing={sorted(missing_evidence)}")
        if finding.defect_kind == "other":
            _require(finding.gate is not None and finding.severity is not None,
                     f"v2 other finding 분류 누락: {finding.finding_code}")
            gate, severity = finding.gate, finding.severity
        else:
            gate, severity = FINDING_TAXONOMY_V2[finding.defect_kind]
        derived_findings.append(ReviewFinding(
            finding_code=finding.finding_code,
            gate=gate,
            severity=severity,
            summary=f"{finding.defect_kind}: {', '.join(labels)}",
            evidence_refs=evidence_refs,
            affected_task_refs=tuple(sorted(affected)),
            remediable=finding.remediable,
        ))
        finding_targets[finding.finding_code] = set(labels)
        row_closures.append(InspectionRowClosureV2(
            row_kind="finding", row_id=finding.finding_code, citation_ids=closure
        ))

    def targeted(code_kind: str, label: str) -> tuple[ReviewFindingV2, ...]:
        return tuple(item for item in findings
                     if item.defect_kind == code_kind and label in finding_targets[item.finding_code])

    for scope_id, row in scope_by_id.items():
        label = _target_key(InspectionTargetV2(
            kind="validation_scope", primary_ref=scope_id, secondary_ref=None
        ))
        scope_findings = tuple(item for item in findings if label in finding_targets[item.finding_code])
        expected_kind = {"contradicted": "validation_scope", "unresolved": "insufficient_evidence"}.get(row.status)
        if expected_kind is None:
            _require(not scope_findings, f"v2 supported scope에 finding이 있습니다: {scope_id}")
        else:
            _require(bool(scope_findings) and all(item.defect_kind == expected_kind for item in scope_findings),
                     f"v2 non-supported scope finding이 누락되거나 종류가 다릅니다: {scope_id}")

    for key, row in ac_pairs.items():
        label = _target_key(InspectionTargetV2(
            kind="ac_validation", primary_ref=key[0], secondary_ref=key[1]
        ))
        missing = row.ac_link_required and not witnesses[key].observed_present
        linked = targeted("missing_validation_link", label)
        _require(bool(linked) == missing, f"v2 AC coverage finding 일관성 오류: {key}")

    for key, row in constraint_pairs.items():
        label = _target_key(InspectionTargetV2(
            kind="constraint_task", primary_ref=key[0], secondary_ref=key[1]
        ))
        linked = targeted("missing_task_validation", label)
        if row.applicability == "not_applicable":
            _require(not linked, f"v2 not_applicable constraint에 finding이 있습니다: {key}")
        else:
            _require(bool(row.required_validation_ids) or bool(linked),
                     f"v2 required constraint의 validation과 finding이 모두 없습니다: {key}")

    _require(inspection.model_dump(mode="json") == before_inspection and
             tuple(item.model_dump(mode="json") for item in findings) == before_findings,
             "v2 compiler가 provider 의미 필드를 변경했습니다.")
    return CompiledPlanInspectionV2(
        ac_validation_decisions=derived_ac_rows,
        derived_findings=tuple(derived_findings),
        resolved_finding_targets=tuple(resolved_finding_targets),
        row_closures=tuple(row_closures),
        membership_witnesses=tuple(witnesses.values()),
        used_citations=tuple(citations[item] for item in sorted(used)),
    )
