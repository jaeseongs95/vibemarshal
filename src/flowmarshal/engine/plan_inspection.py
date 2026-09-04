"""검사 근거 대조표의 비권위 provider 계약과 제출물 일관성 검사."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Literal

from pydantic import Field

from ..canonical import sha256_bytes
from .domain import EngineModel, GoalContractRevision, ProjectMapEntry, ProjectMapRevision
from .planning import validation_comparison_targets


class InspectionCitation(EngineModel):
    citation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,59}$")
    source_ref: str = Field(description="Goal/Plan 원문 ref 또는 정확한 project:<entry_id>. 등록 자료의 검사 범위는 inspection_source_catalog의 정식 project ref로 인용한다.")
    selector: str = Field(description="source_ref 원문 값에 대한 RFC 6901 JSON pointer. 파일 본문은 /content.")
    quote: str = Field(min_length=1, max_length=240, description="선택한 문자열에 그대로 존재하는 짧은 연속 인용. 요약·생략 기호를 삽입하지 않는다.")


class ACValidationInspection(EngineModel):
    criterion_id: str
    validation_id: str
    relation: Literal["explicit_procedure", "global_constraint_only", "optional_or_unrelated"] = Field(description="AC 연결 의무의 출처 분류. 필수가 아닌 기존 선택적 연결을 금지한다는 뜻이 아니다.")
    basis_refs: tuple[str, ...] = Field(min_length=2, description="해당 AC와 validation statement 인용 ID. global_constraint_only이면 constraint 원문도 포함한다.")
    finding_codes: tuple[str, ...] = Field(description="explicit_procedure인데 현재 ID 연결이 없는 경우만 missing_validation_link finding. 나머지는 빈 배열.")


class ConstraintTaskInspection(EngineModel):
    constraint_id: str
    task_ref: str
    applicability: Literal["required", "not_applicable"]
    validation_ids: tuple[str, ...] = Field(description="해당 Task에 실제 존재하는 검사 ID. 일부 의무가 누락되어도 존재하는 ID와 누락 finding을 함께 제출한다.")
    basis_refs: tuple[str, ...] = Field(min_length=1, description="required와 not_applicable 모두 해당 constraint 원문 인용을 반드시 포함한다.")
    finding_codes: tuple[str, ...] = Field(description="required 의무의 전체/부분 실행 계약 누락은 missing_task_validation. AC 연결 누락이나 검사 범위 모순은 각 전용 행에 둔다.")


class InspectionMechanism(EngineModel):
    tool: str = Field(min_length=1, max_length=120)
    phase: str | None = Field(description="원문이 식별하는 phase/mode. 없으면 null.")
    basis_refs: tuple[str, ...] = Field(min_length=1, description="등록 자료·구현의 실제 범위 또는 새로 계약한 검사 책임의 인용 ID.")


class ValidationInspection(EngineModel):
    validation_id: str
    claim_ref: str = Field(description="해당 validation statement의 주장 인용 ID.")
    mechanisms: tuple[InspectionMechanism, ...] = Field(min_length=1)
    separate_check_refs: tuple[str, ...] = Field(description="같은 statement 안 별도 실제 실행·기대값 비교 책임의 인용 ID. 목적만 덧붙인 문장은 포함하지 않는다.")
    assessment: Literal["supported", "contradicted", "unresolved"] = Field(description="원문 수단 범위와 주장: 지지됨/직접 모순/근거 부족. evidence의 단순 언급은 다른 입력의 명시적 제외가 아니다.")
    finding_codes: tuple[str, ...] = Field(description="supported이면 빈 배열. contradicted는 validation_scope, unresolved는 insufficient_evidence finding에 연결한다.")


class InspectionFindingLink(EngineModel):
    finding_code: str
    defect_kind: Literal["missing_validation_link", "validation_scope", "missing_task_validation", "result_order", "insufficient_evidence", "other"]
    criterion_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    task_refs: tuple[str, ...]
    basis_refs: tuple[str, ...] = Field(min_length=1)


class PlanInspection(EngineModel):
    citations: tuple[InspectionCitation, ...] = Field(min_length=1)
    ac_validation_rows: tuple[ACValidationInspection, ...]
    constraint_task_rows: tuple[ConstraintTaskInspection, ...]
    validation_rows: tuple[ValidationInspection, ...]
    finding_links: tuple[InspectionFindingLink, ...]


PLAN_INSPECTION_INSTRUCTIONS = (
    "응답은 기존 plan 또는 review와 inspection을 감싼 provider 전용 envelope다. inspection은 "
    "검증 가능한 사실·참조의 간결한 대조표이며 장황한 사고 과정이나 Core 판정을 쓰지 않는다. "
    "review를 제출하는 Reviewer는 findings와 ratings 두 key를 모두 제출한다. findings가 비면 "
    "goal_fit·grounding·engineering·verification·execution_safety 다섯 0~4 rating 객체를 쓰고, "
    "finding이 하나 이상이면 ratings:null을 쓴다. rating key 자체를 생략하지 않는다. 이 rating은 "
    "비권위 관찰이며 admission과 0~100 score는 Core가 결정하므로 status·admissible·score·weakest task를 쓰지 않는다. "
    "같은 원문 인용은 citations에 한 번 등록하고 나머지 행은 citation_id를 참조한다. "
    "source:goal은 Goal definition, Reviewer의 artifact:plan_contract는 revision 전체, 작성자의 "
    "artifact:plan_draft는 응답 plan 전체다. JSON pointer는 이 값의 루트부터 쓰며 배열은 /0 형식이다. "
    "등록 자료의 검사 범위는 inspection_source_catalog가 제공하는 정식 project:<entry_id>와 "
    "selector=/content를 반드시 사용한다. 함께 제공한 content는 content_digest로 검증한 UTF-8 "
    "원문이다. Goal source_traces의 복제 본문이나 배열 번호로 등록 파일의 주소를 재구성하지 않는다. "
    "그 밖의 Project Map 파일도 정확한 entry_id와 /content로 실제 본문을 인용한다. 파일 인용은 "
    "finding의 source:project_map evidence에 대응한다. 모든 quote는 240자 이내의 연속 원문이며 "
    "의역·생략 표시를 넣지 않는다. 등록 문서가 부족하면 관련 구현도 읽되 제공된 원문으로 "
    "확인한 범위만 주장한다. "
    "모든 AC × 모든 Task·integration validation 쌍을 ac_validation_rows에 정확히 한 번씩 쓴다. "
    "작성자는 완성한 plan의 모든 validation ID에서 이 곱집합을 구성한다. 각 행에서 AC가 "
    "명시한 절차(explicit_procedure), 전역 constraint만의 의무(global_constraint_only), 선택적·무관 "
    "관계(optional_or_unrelated)를 구분한다. AC와 검사 statement 인용을 모두 연결하고 전역 의무이면 "
    "constraint 인용도 붙인다. 다른 ID가 연결되어도 AC가 명시한 복합 검사 절차 ID를 빠뜨리지 않는다. "
    "반대로 검사 문장의 연관 표현·전역 의무만으로 AC 명시 절차로 분류하지 않는다. relation은 "
    "연결 의무의 출처이며 선택적 연결 금지가 아니다. global_constraint_only 또는 "
    "optional_or_unrelated여도 기존 연결을 허용하고 그 이유만으로 finding을 만들지 않는다. "
    "모든 constraint × 모든 Task 쌍을 constraint_task_rows에 한 번씩 쓰고 전역 Task 검사 의무와 "
    "그 Task의 실제 validation ID를 대조한다. 검사 의무가 아닌 제약은 not_applicable과 빈 ID로 두되 "
    "모든 행에 해당 constraint 원문을 인용한다. required 의무의 일부만 빠져도 존재하는 ID는 "
    "보존하고 누락 책임의 missing_task_validation finding을 함께 연결한다. 검사 자체의 존재와 "
    "AC 연결 누락을 구분하고, 실제 수단 범위 모순은 validation_rows에 둔다. "
    "모든 validation을 validation_rows에 한 번씩 쓰고 주장·도구·phase·등록 범위와 같은 문장의 "
    "별도 실제 실행 및 기대값 비교 책임을 구분한다. 독립 결함은 각각 finding과 finding_links로 "
    "연결한다. finding_code는 제출물 안에서 유일해야 한다. finding_links는 AC·validation·Task ID와 "
    "직접 원문 인용을 지정하고 finding.evidence_refs에는 그 인용의 원본 evidence ref를 모두 포함한다. "
    "연결 누락 행은 Goal AC·validation statement·현재 coverage 인용을, 범위 모순 행은 검사 주장과 "
    "실제 수단 범위 인용을 해당 finding_link에도 붙인다. finding summary에는 해당 AC ID와 "
    "validation ID를 원문 그대로 포함한다. integration 결함의 task_refs는 직접 영향 Task만 쓰며 "
    "없으면 비운다. finding의 affected_task_refs와 link.task_refs는 같아야 한다. "
    "명시 절차인데 ID가 빠진 행만 missing_validation_link finding에 연결하고 나머지 AC 행의 "
    "finding_codes는 비운다. 단순 file/diff evidence 언급을 test 입력의 명시적 제외로 추정하지 않는다. "
    "원문이 입력을 제한·제외하여 수단과 충돌하는 경우와 원문 정보가 부족한 경우를 구분한다. contradicted 또는 "
    "unresolved 검사는 각각 validation_scope 또는 insufficient_evidence finding에 연결한다. "
    "독립적으로 확인한 다른 결함을 이미 제출한 finding이나 낮은 rating으로 대신하지 않는다. "
    "작성자는 결함을 보정한 완성 plan과 대조표를 제출하므로 finding_links와 모든 finding_codes는 "
    "비운다. adapter는 원문·집합·내부 일관성만 검사하며 의미 정답을 추정하거나 Plan을 수정하지 않는다."
)


class PlanInspectionError(ValueError):
    pass


def inspection_file_content(entry: ProjectMapEntry, project_map: ProjectMapRevision) -> str:
    """입력 projection과 인용 검증에서 동일한 원본 bytes·digest 검사를 사용한다."""
    path = Path(entry.path)
    path = path if path.is_absolute() else Path(project_map.root) / path
    try:
        body = path.read_bytes()
        _require(sha256_bytes(body) == entry.content_digest, "대조표 파일 원문 digest 불일치")
        return body.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise PlanInspectionError("대조표 파일 원문을 확인할 수 없습니다.") from error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanInspectionError(message)


def _unique(values: Any, label: str) -> set:
    items = list(values)
    _require(len(items) == len(set(items)), f"대조표 {label} 중복")
    return set(items)


def _pointer(value: Any, selector: str) -> Any:
    _require(selector.startswith("/"), "대조표 selector는 JSON pointer여야 합니다.")
    try:
        for token in selector[1:].split("/"):
            _require(re.search(r"~(?![01])", token) is None, "대조표 selector escape 오류")
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, (list, tuple)):
                _require(re.fullmatch(r"0|[1-9][0-9]*", token) is not None, "대조표 배열 selector 오류")
                value = value[int(token)]
            else:
                value = value[token]
        return value
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise PlanInspectionError("대조표 selector가 원문에 없습니다.") from error


def validate_plan_inspection(
    inspection: PlanInspection, *, plan: Any, goal: GoalContractRevision,
    project_map: ProjectMapRevision, evidence_catalog: dict[str, Any], findings: tuple,
) -> None:
    """관계·수단의 의미 정답은 평가하지 않고 제출된 판단과 원문의 일관성만 검사한다."""
    definition = getattr(plan, "definition", plan)
    is_revision = hasattr(plan, "definition")
    plan_ref = "artifact:plan_contract" if is_revision else "artifact:plan_draft"
    prefix = "/definition" if is_revision else ""
    sources = dict(evidence_catalog) | {plan_ref: plan.model_dump(mode="json")}
    citations = {item.citation_id: item for item in inspection.citations}
    _unique((item.citation_id for item in inspection.citations), "citation ID")
    entries = {f"project:{entry.entry_id}": entry for entry in project_map.entries}
    for citation in citations.values():
        if citation.source_ref not in sources and citation.source_ref in entries:
            sources[citation.source_ref] = {"content": inspection_file_content(entries[citation.source_ref], project_map)}
        _require(citation.source_ref in sources, "대조표 source_ref가 입력에 없습니다.")
        selected = _pointer(sources[citation.source_ref], citation.selector)
        _require(isinstance(selected, str) and citation.quote in selected,
                 "대조표 인용이 선택한 원문 문자열과 일치하지 않습니다.")

    used: set[str] = set()

    def refs(values: tuple[str, ...]) -> tuple[InspectionCitation, ...]:
        _require(_unique(values, "인용 참조") <= citations.keys(), "대조표에 없는 인용 ID")
        used.update(values)
        return tuple(citations[value] for value in values)

    def has(values: tuple[str, ...], source: str, selectors: set[str]) -> bool:
        return any(item.source_ref == source and item.selector in selectors for item in refs(values))

    targets = validation_comparison_targets(goal, plan)
    validation_by_id = {row["validation_id"]: row for row in targets["validations"]}
    _unique((row["validation_id"] for row in targets["validations"]), "입력 validation ID")
    task_by_ref = {task.task_ref: task for task in definition.tasks}
    criterion_selectors = {item.criterion_id: {f"/hard_acceptance/{i}/statement", f"/hard_acceptance/{i}/validation_intent"}
                           for i, item in enumerate(goal.definition.hard_acceptance)}
    constraint_selectors = {item.constraint_id: f"/constraints/{i}/statement"
                            for i, item in enumerate(goal.definition.constraints)}
    coverage = {item.criterion_id: item for item in definition.goal_coverage}
    findings_by_code = {item.finding_code: item for item in findings}
    _unique((item.finding_code for item in findings), "finding code")
    links = {item.finding_code: item for item in inspection.finding_links}
    _require(_unique((item.finding_code for item in inspection.finding_links), "finding link") == findings_by_code.keys(),
             "대조표 finding link 집합 불일치")
    for link in links.values():
        _require(_unique(link.criterion_ids, "finding AC") <= criterion_selectors.keys(), "대조표 finding AC ID 오류")
        _require(_unique(link.validation_ids, "finding validation") <= validation_by_id.keys(), "대조표 finding validation ID 오류")
        _require(_unique(link.task_refs, "finding Task") <= task_by_ref.keys(), "대조표 finding Task ID 오류")
        finding = findings_by_code[link.finding_code]
        _require(set(link.task_refs) == set(finding.affected_task_refs), "대조표 finding affected Task 불일치")
        cited_sources = {"source:project_map" if item.source_ref in entries else item.source_ref for item in refs(link.basis_refs)}
        _require(cited_sources <= set(finding.evidence_refs), "대조표 finding evidence ref 불일치")

    def codes(values: tuple[str, ...], *, kind: str, criterion: str | None = None,
              validation: str | None = None, task: str | None = None) -> None:
        _require(bool(values), "대조표 결함에 finding 연결이 없습니다.")
        _require(_unique(values, "행 finding") <= links.keys(), "대조표에 없는 finding code")
        for code in values:
            link = links[code]
            _require(link.defect_kind == kind, "대조표 finding 결함 종류 불일치")
            _require(criterion is None or criterion in link.criterion_ids, "대조표 finding AC 불일치")
            _require(validation is None or validation in link.validation_ids, "대조표 finding validation 불일치")
            _require(task is None or task in link.task_refs, "대조표 finding Task 불일치")

    pairs = _unique(((row.criterion_id, row.validation_id) for row in inspection.ac_validation_rows), "AC 검사 행")
    _require(pairs == {(row["criterion_id"], row["validation_id"]) for row in targets["ac_validation_pairs"]},
             "대조표 AC 검사 행 집합 불완전")
    for row in inspection.ac_validation_rows:
        _require(has(row.basis_refs, "source:goal", criterion_selectors[row.criterion_id]), "대조표 해당 AC 인용 누락")
        validation = validation_by_id[row.validation_id]
        _require(has(row.basis_refs, plan_ref, {validation["selector"] + "/statement"}), "대조표 해당 검사 인용 누락")
        if row.relation == "global_constraint_only":
            _require(has(row.basis_refs, "source:goal", set(constraint_selectors.values())), "대조표 전역 constraint 인용 누락")
        missing = row.relation == "explicit_procedure" and row.validation_id not in coverage[row.criterion_id].validation_ids
        if missing:
            codes(row.finding_codes, kind="missing_validation_link", criterion=row.criterion_id,
                  validation=row.validation_id, task=validation["task_ref"])
            ci = next(i for i, item in enumerate(definition.goal_coverage) if item.criterion_id == row.criterion_id)
            for code in row.finding_codes:
                basis = links[code].basis_refs
                _require(has(basis, "source:goal", criterion_selectors[row.criterion_id]) and
                         has(basis, plan_ref, {validation["selector"] + "/statement"}) and
                         any(item.source_ref == plan_ref and item.selector.startswith(f"{prefix}/goal_coverage/{ci}/validation_ids/")
                             for item in refs(basis)), "대조표 연결 finding의 직접 근거 누락")
        else:
            _require(not row.finding_codes, "대조표 정상 연결 행의 finding 모순")

    pairs = _unique(((row.constraint_id, row.task_ref) for row in inspection.constraint_task_rows), "constraint Task 행")
    _require(pairs == {(row["constraint_id"], row["task_ref"]) for row in targets["constraint_task_pairs"]},
             "대조표 constraint Task 행 집합 불완전")
    for row in inspection.constraint_task_rows:
        _require(has(row.basis_refs, "source:goal", {constraint_selectors[row.constraint_id]}), "대조표 해당 constraint 인용 누락")
        task_ids = {item.validation_id for item in task_by_ref[row.task_ref].validations}
        _require(_unique(row.validation_ids, "constraint 검사") <= task_ids, "대조표 constraint 검사 소유 Task 불일치")
        if row.applicability == "required" and (not row.validation_ids or row.finding_codes):
            codes(row.finding_codes, kind="missing_task_validation", task=row.task_ref)
        else:
            _require(not row.finding_codes, "대조표 constraint finding 모순")
        if row.applicability == "not_applicable":
            _require(not row.validation_ids, "대조표 비적용 constraint 검사 모순")

    _require(_unique((row.validation_id for row in inspection.validation_rows), "검사 능력 행") == validation_by_id.keys(),
             "대조표 검사 능력 행 집합 불완전")
    for row in inspection.validation_rows:
        validation = validation_by_id[row.validation_id]
        selector = validation["selector"] + "/statement"
        _require(has((row.claim_ref,), plan_ref, {selector}), "대조표 검사 주장 인용 오류")
        for mechanism in row.mechanisms:
            refs(mechanism.basis_refs)
        for ref in row.separate_check_refs:
            _require(has((ref,), plan_ref, {selector}), "대조표 별도 검사 책임 인용 오류")
        if row.assessment != "supported":
            codes(row.finding_codes, kind="validation_scope" if row.assessment == "contradicted" else "insufficient_evidence",
                  validation=row.validation_id, task=validation["task_ref"])
            for code in row.finding_codes:
                _require(row.claim_ref in links[code].basis_refs and
                         any(set(mechanism.basis_refs) <= set(links[code].basis_refs) for mechanism in row.mechanisms),
                         "대조표 검사 범위 finding의 직접 근거 누락")
        else:
            _require(not row.finding_codes, "대조표 정상 검사와 finding 모순")
    # 연결·범위 finding을 표 밖에만 선언하거나 정상 행과 모순시키지 않는다.
    linked_codes = {code for row in (*inspection.ac_validation_rows, *inspection.constraint_task_rows, *inspection.validation_rows)
                    for code in row.finding_codes}
    _require(all(link.finding_code in linked_codes for link in links.values()
                 if link.defect_kind in {"missing_validation_link", "validation_scope", "missing_task_validation", "insufficient_evidence"}),
             "대조표 finding에 대응하는 결함 행이 없습니다.")
    _require(used == citations.keys(), "대조표 미사용 인용")
