from __future__ import annotations

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
    claim_ref: str
    mechanisms: tuple[InspectionMechanismV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def mechanism_ids_are_unique(self) -> "ValidationInspectionV2":
        _unique(tuple(item.mechanism_id for item in self.mechanisms), "validation mechanism_id")
        return self


class ValidationScopeInspectionV2(EngineModel):
    scope_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
    validation_id: str
    mechanism_id: str
    claim_ref: str
    direct_extra_refs: tuple[str, ...]
    status: ScopeStatus

    @field_validator("direct_extra_refs")
    @classmethod
    def refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "scope direct_extra_refs")


class ACValidationInspectionV2(EngineModel):
    criterion_id: str
    validation_id: str
    ac_link_required: bool
    scope_ids: tuple[str, ...]
    direct_extra_refs: tuple[str, ...]

    @field_validator("scope_ids", "direct_extra_refs")
    @classmethod
    def refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "AC 행 참조")


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
    citations: tuple[InspectionCitation, ...] = Field(min_length=1)
    validation_rows: tuple[ValidationInspectionV2, ...] = Field(min_length=1)
    validation_scope_rows: tuple[ValidationScopeInspectionV2, ...] = Field(min_length=1)
    ac_validation_rows: tuple[ACValidationInspectionV2, ...]
    constraint_task_rows: tuple[ConstraintTaskInspectionV2, ...]


class InspectionTargetV2(EngineModel):
    """Provider가 직접 선택하는 단일 형태의 typed target.

    배열 item의 discriminated union은 Codex strict response schema에서 허용되지
    않으므로 kind별 객체를 한 형태로 표현한다. 복합 target만 secondary_ref를
    사용하며 model validator가 kind와 ref 개수를 엄격히 대조한다.
    """

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
    affected_task_refs: tuple[str, ...]
    remediable: bool
    target_refs: tuple[InspectionTargetV2, ...] = Field(min_length=1)

    @field_validator("affected_task_refs")
    @classmethod
    def tasks_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "finding affected_task_refs")

    @model_validator(mode="after")
    def direct_classification_and_targets_are_valid(self) -> "ReviewFindingV2":
        if self.defect_kind == "other":
            if self.gate is None or self.severity is None:
                raise ValueError("other finding은 gate와 severity를 직접 제출해야 합니다.")
        elif self.gate is not None or self.severity is not None:
            raise ValueError("표준 finding의 gate와 severity는 adapter가 계산하므로 null이어야 합니다.")
        keys = tuple(repr(item.model_dump(mode="json")) for item in self.target_refs)
        _unique(keys, "finding target_refs")
        return self


class CoverageMembershipWitnessV2(EngineModel):
    criterion_id: str
    validation_id: str
    collection_selector: str
    observed_present: bool
    member_selector: str | None


class InspectionRowClosureV2(EngineModel):
    row_kind: Literal[
        "validation", "mechanism", "validation_scope", "ac_validation", "constraint_task", "finding"
    ]
    row_id: str
    citation_ids: tuple[str, ...]


class CompiledPlanInspectionV2(EngineModel):
    derived_findings: tuple[ReviewFinding, ...]
    row_closures: tuple[InspectionRowClosureV2, ...]
    membership_witnesses: tuple[CoverageMembershipWitnessV2, ...]


FINDING_TAXONOMY_V2 = MappingProxyType({
    "missing_validation_link": (GateName.VERIFICATION, FindingSeverity.ERROR),
    "validation_scope": (GateName.VERIFICATION, FindingSeverity.ERROR),
    "missing_task_validation": (GateName.VERIFICATION, FindingSeverity.ERROR),
    "result_order": (GateName.EXECUTION, FindingSeverity.ERROR),
    "insufficient_evidence": (GateName.VERIFICATION, FindingSeverity.ERROR),
})


PLAN_INSPECTION_V2_INSTRUCTIONS = (
    "응답은 기존 plan 또는 review와 inspection을 감싼 plan-inspection-v2 전용 envelope다. "
    "inspection은 의미 판단에 필요한 원자 관측만 제출하고 반복 가능한 참조 closure는 쓰지 않는다. "
    "citations에는 실제 사용한 source_ref·JSON pointer selector·연속 quote를 한 번씩 등록한다. "
    "source:goal은 Goal definition, Reviewer의 artifact:plan_contract는 revision 전체, Expander의 "
    "artifact:plan_draft는 출력 plan 전체다. 등록 파일은 inspection_source_catalog의 정확한 "
    "project:<entry_id>와 selector=/content를 사용한다. quote를 의역하거나 없는 원문을 만들지 않는다. "
    "모든 Task·integration validation마다 validation_rows를 정확히 하나 만들고 statement 전체 citation을 "
    "claim_ref로 선택한다. mechanisms에는 실제 확인한 tool·phase와 그 판단에 직접 필요한 citation ID만 "
    "direct_refs로 쓴다. mechanism_id는 응답 전체에서 유일해야 한다. "
    "각 validation의 주장 범위를 빠짐없이 validation_scope_rows로 나누고, 같은 validation의 mechanism_id와 "
    "plan statement를 가리키는 claim_ref를 선택한 뒤 supported·contradicted·unresolved 중 하나를 직접 판단한다. "
    "mechanism의 근거를 direct_extra_refs에 반복하지 말고 해당 scope에만 추가로 필요한 citation만 쓴다. "
    "모든 AC×모든 validation 조합을 ac_validation_rows에 정확히 한 번씩 제출한다. ac_link_required는 "
    "AC 원문이 그 검사를 필수로 연결하는지 직접 판단한다. true이면 해당 validation의 supported scope ID를 "
    "하나 이상 선택하고 false이면 scope_ids를 빈 배열로 둔다. Goal·validation·mechanism 근거는 adapter가 "
    "closure로 파생하므로 direct_extra_refs에 반복하지 않는다. false는 기존 선택 연결을 금지하지 않는다. "
    "모든 전역 constraint×Task 조합도 constraint_task_rows에 정확히 한 번씩 제출한다. Task 검사가 직접 "
    "요구되면 applicability=required와 실제 Task validation ID를 쓰고, 그렇지 않으면 not_applicable과 빈 "
    "required_validation_ids를 쓴다. AC 관계와 전역 Task 의무를 서로 추정하지 않는다. "
    "Reviewer finding은 finding_code·defect_kind·gate·severity·affected_task_refs·remediable·target_refs를 제출한다. "
    "다섯 표준 defect_kind의 gate·severity는 null이고 adapter가 taxonomy에서 계산한다. 표준 taxonomy로 "
    "표현할 수 없는 직접 결함은 defect_kind=other와 직접 gate·severity를 사용한다. "
    "target_refs는 단일 {kind, primary_ref, secondary_ref} 형태를 사용한다. ac_validation은 "
    "criterion_id와 validation_id, constraint_task는 constraint_id와 task_ref를 각각 primary_ref와 "
    "secondary_ref에 쓰고, 나머지 kind는 자신의 ID를 primary_ref에 쓰며 secondary_ref는 null이다. "
    "defect_kind에 맞는 typed target을 사용한다: missing_validation_link=ac_validation, "
    "validation_scope 또는 insufficient_evidence=validation_scope, missing_task_validation=constraint_task, "
    "result_order=validation, other=citation. 직접 관련된 citation이나 Task target만 추가할 수 있다. 표준 "
    "finding의 gate·severity와 모든 finding의 summary·evidence_refs·finding_links는 adapter가 taxonomy와 "
    "target closure에서 파생한다. "
    "모델은 citation, bool, status, finding, target 또는 direct ref를 adapter가 채울 것이라고 가정하지 않는다. "
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

    citations = {item.citation_id: item for item in inspection.citations}
    _require(len(citations) == len(inspection.citations), "v2 대조표 citation ID 중복")
    entries = {f"project:{entry.entry_id}": entry for entry in project_map.entries}
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
        missing = set(values) - citations.keys()
        _require(not missing, f"v2 대조표에 없는 인용 ID: {context}, missing_refs={sorted(missing)}")
        used.update(values)
        return values

    def source_citation(source_ref: str, selector: str, context: str, *, full_quote: bool) -> str:
        selected = _pointer(sources[source_ref], selector)
        matches = [item.citation_id for item in inspection.citations
                   if item.source_ref == source_ref and item.selector == selector and
                   (not full_quote or item.quote == selected)]
        qualifier = "전체 " if full_quote else ""
        _require(len(matches) == 1, f"v2 대조표 {qualifier}원문 인용 누락 또는 중복: {context}")
        used.add(matches[0])
        return matches[0]

    targets = validation_comparison_targets(goal, plan)
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
        _require(
            row.claim_ref == source_citation(
                plan_ref, selector, f"validation_id={validation_id}", full_quote=True
            ),
            f"v2 검사 주장 인용 오류: validation_id={validation_id}",
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
        closure = _ordered_union((row.claim_ref,), *mechanism_refs)
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
        claim = citations.get(row.claim_ref)
        _require(
            claim is not None and claim.source_ref == plan_ref and claim.selector == validation_selector,
            f"v2 scope 주장 인용 오류: scope_id={scope_id}",
        )
        used.add(row.claim_ref)
        extras = direct_refs(row.direct_extra_refs, f"scope_id={scope_id}")
        mechanism = mechanism_binding[1]
        closure = _ordered_union((row.claim_ref,), mechanism.direct_refs, extras)
        scope_closures[scope_id] = closure
        project_refs_by_validation[row.validation_id].update(
            ref for ref in closure if citations[ref].source_ref in entries
        )
        row_closures.append(InspectionRowClosureV2(
            row_kind="validation_scope", row_id=scope_id, citation_ids=closure
        ))

    ac_pairs = {(row.criterion_id, row.validation_id): row for row in inspection.ac_validation_rows}
    _require(len(ac_pairs) == len(inspection.ac_validation_rows), "v2 AC 검사 행 중복")
    expected_ac_pairs = {(row["criterion_id"], row["validation_id"]) for row in targets["ac_validation_pairs"]}
    _require(ac_pairs.keys() == expected_ac_pairs, "v2 AC 검사 행 집합 불완전")
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
        if row.ac_link_required:
            _require(bool(selected_scopes) and all(item.status == "supported" for item in selected_scopes),
                     "v2 필수 AC 연결에는 supported scope가 필요합니다.")
        else:
            _require(not row.scope_ids, "v2 비필수 AC 연결의 scope_ids는 비어야 합니다.")
        extras = direct_refs(row.direct_extra_refs, f"AC={criterion_id}, validation={validation_id}")
        closure = _ordered_union(
            required_refs,
            tuple(sorted(project_refs_by_validation[validation_id])),
            *(scope_closures[item.scope_id] for item in selected_scopes),
            extras,
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
    finding_targets: dict[str, set[str]] = {}
    for finding in findings:
        target_kinds = {item.kind for item in finding.target_refs}
        primary = allowed_primary[finding.defect_kind]
        _require(primary in target_kinds, f"v2 finding target 종류 불일치: {finding.finding_code}")
        allowed = {primary, "citation", "task"}
        _require(target_kinds <= allowed, f"v2 finding에 호환되지 않는 target이 있습니다: {finding.finding_code}")
        closure_parts: list[tuple[str, ...]] = []
        affected: set[str] = set()
        labels: list[str] = []
        for target in finding.target_refs:
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
        _require(affected == set(finding.affected_task_refs),
                 f"v2 finding affected Task 불일치: {finding.finding_code}")
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
            affected_task_refs=finding.affected_task_refs,
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

    unused = citations.keys() - used
    _require(not unused, f"v2 사용되지 않은 citation이 있습니다: {sorted(unused)}")
    _require(inspection.model_dump(mode="json") == before_inspection and
             tuple(item.model_dump(mode="json") for item in findings) == before_findings,
             "v2 compiler가 provider 의미 필드를 변경했습니다.")
    return CompiledPlanInspectionV2(
        derived_findings=tuple(derived_findings),
        row_closures=tuple(row_closures),
        membership_witnesses=tuple(witnesses.values()),
    )
