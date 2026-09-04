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
    quote: str = Field(min_length=1, max_length=5000, description="선택한 문자열에 그대로 존재하는 연속 인용. AC 행의 validation statement 인용은 해당 문장 전체와 같아야 하며, 요약·생략 기호를 삽입하지 않는다.")


class ACValidationInspection(EngineModel):
    criterion_id: str
    validation_id: str
    ac_link_required: bool = Field(description="이 validation이 AC 일부를 직접 검증하여 goal_coverage 연결이 필수인지 여부. AC statement 또는 validation_intent가 동일 절차의 task/goal phase를 각각 명시하면, 명시된 각 phase를 실제 수행하는 validation은 각각 true다. 별도 실행은 실행·evidence 분리이며 task phase를 선택 사항으로 만들지 않는다. 명시되지 않은 sibling unittest·scope·semantic validation에는 이 규칙을 전염시키지 않는다. false는 선택적 연결을 금지하지 않는다.")
    scope_ids: tuple[str, ...] = Field(description="true 판정의 근거가 되는 동일 validation의 supported validation_scope_rows ID. true이면 하나 이상, false이면 빈 배열이다. 다른 scope 부분의 contradicted·unresolved 판정을 이 행의 false 근거로 자동 전파하지 않는다.")
    basis_refs: tuple[str, ...] = Field(min_length=2, description="해당 AC의 statement·validation_intent 각각, validation statement 전체, 참조한 scope 행의 claim·근거 및 같은 validation이 실제 범위 판단에 사용한 모든 project citation ID. 기존 citation을 재사용하며 새 citation을 만들지 않는다.")
    finding_codes: tuple[str, ...] = Field(description="ac_link_required=true인데 현재 ID 연결이 없는 경우만 missing_validation_link finding. 나머지는 빈 배열.")


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
    basis_refs: tuple[str, ...] = Field(min_length=1, description="등록 자료·구현의 실제 범위 또는 새로 계약한 검사 책임의 인용 ID. project citation으로 실제 범위를 판단했다면 해당 validation의 모든 AC 관계 행 basis_refs도 같은 ID를 공유한다.")


class ValidationInspection(EngineModel):
    validation_id: str
    claim_ref: str = Field(description="해당 validation statement의 주장 인용 ID.")
    mechanisms: tuple[InspectionMechanism, ...] = Field(min_length=1)
    separate_check_refs: tuple[str, ...] = Field(description="같은 statement 안 별도 실제 실행·기대값 비교 책임의 인용 ID. 목적만 덧붙인 문장은 포함하지 않는다.")


class ValidationScopeInspection(EngineModel):
    scope_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
    validation_id: str
    claim_ref: str = Field(description="validation statement에서 이 행이 판정하는 부분 주장의 연속 원문 인용 ID.")
    procedure: str = Field(min_length=1, max_length=240, description="이 부분 주장을 실제로 수행한다고 대조한 절차·검사 이름. 근거 인용으로 결속하며 새 검사 능력을 뜻하지 않는다.")
    phase: str | None = Field(description="해당 절차가 실제 실행되는 phase/mode. 원문에 없으면 null.")
    basis_refs: tuple[str, ...] = Field(min_length=1, description="claim_ref와 실제 절차·phase·범위 판단의 직접 근거 citation ID. claim_ref를 반드시 포함한다.")
    assessment: Literal["supported", "contradicted", "unresolved"] = Field(description="해당 부분 주장만 지지됨/직접 모순/근거 부족으로 판정한다. 한 부분의 결과를 같은 validation의 다른 부분이나 AC 연결 판정으로 전파하지 않는다.")
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
    validation_rows: tuple[ValidationInspection, ...]
    validation_scope_rows: tuple[ValidationScopeInspection, ...]
    ac_validation_rows: tuple[ACValidationInspection, ...]
    constraint_task_rows: tuple[ConstraintTaskInspection, ...]
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
    "finding의 source:project_map evidence에 대응한다. quote는 연속 원문이며 의역·생략 표시를 "
    "넣지 않는다. AC × validation 행의 validation statement 인용은 문장 전체를 그대로 쓴다. 등록 문서가 부족하면 관련 구현도 읽되 제공된 원문으로 "
    "확인한 범위만 주장한다. "
    "citations 다음에는 모든 validation_rows를 먼저 작성해 validation statement, mechanism, phase와 "
    "별도 실제 검사 책임의 근거를 확정한다. 이어 validation_scope_rows에서 복합 statement의 책임을 "
    "실제 절차·phase·부분 claim별로 나누어 supported·contradicted·unresolved를 각각 판정한다. 각 validation은 "
    "scope 행이 하나 이상 있어야 하며 scope 행의 claim_ref와 basis_refs는 원문 주장과 실제 절차 근거를 "
    "함께 결속한다. 그 뒤 ac_validation_rows와 constraint_task_rows를 작성한다. "
    "모든 AC × 모든 Task·integration validation 쌍을 ac_validation_rows에 정확히 한 번씩 쓴다. "
    "작성자는 완성한 plan의 모든 validation ID에서 이 곱집합을 구성한다. 각 행의 "
    "ac_link_required는 validation이 AC 일부를 직접 검증하면 true이고 아니면 false다. AC statement 또는 "
    "validation_intent가 동일 절차의 task/goal phase를 각각 명시하면, 명시된 각 phase를 실제 수행하는 "
    "validation은 각각 true다. true 행은 그 validation에서 해당 절차를 실제 수행하는 supported scope_id를 "
    "하나 이상 참조한다. 한 scope 부분의 contradicted·unresolved 판정은 실제 수행되는 다른 절차와 명시 "
    "phase의 ac_link_required를 자동으로 false로 만들지 않는다. 예를 들어 task validation이 goal 전용 입력 "
    "검사까지 수행한다고 과장했어도 task phase가 실제 unittest를 수행하고 AC가 그 task phase를 명시했다면 "
    "그 unittest 책임을 나타내는 supported scope를 근거로 연결은 true다. false 행의 scope_ids는 비운다. "
    "명시되지 않은 sibling unittest·scope·semantic validation에는 연결 의무를 전염시키지 않으며 false는 "
    "기존 선택적 연결을 금지하지 않는다. 모든 행의 basis_refs에는 해당 AC의 비어 있지 않은 "
    "statement와 validation_intent를 각각 인용하고 validation statement 전체 인용도 연결한다. 전역 의무이면 "
    "constraint 인용도 붙인다. validation_rows의 mechanism으로 등록 자료·구현을 검사 범위 판단에 "
    "사용했다면 그 정확한 project:<entry_id>/content citation_id를 같은 validation의 모든 AC 관계 행 "
    "basis_refs에도 재사용해 두 판단을 함께 추적한다. 제출 직전 validation별 mechanism의 project citation "
    "집합이 관련 모든 AC 행에 들어 있는지 직접 대조한다. 같은 "
    "citation_id를 여러 행에서 재사용할 수 있다. 다른 ID가 연결되어도 AC 일부를 직접 검증하는 "
    "검사 ID를 빠뜨리지 않는다. 반대로 검사 문장의 연관 표현·전역 의무만으로 AC 직접 검증을 "
    "추정하지 않는다. 전역 Task 검사 의무는 이 bool에 섞지 않고 constraint_task_rows에서만 판정한다. "
    "모든 constraint × 모든 Task 쌍을 constraint_task_rows에 한 번씩 쓰고 전역 Task 검사 의무와 "
    "그 Task의 실제 validation ID를 대조한다. 검사 의무가 아닌 제약은 not_applicable과 빈 ID로 두되 "
    "모든 행에 해당 constraint 원문을 인용한다. required 의무의 일부만 빠져도 존재하는 ID는 "
    "보존하고 누락 책임의 missing_task_validation finding을 함께 연결한다. 검사 자체의 존재와 "
    "AC 연결 누락을 구분하고, 실제 수단 범위 모순은 validation_scope_rows에 둔다. "
    "모든 validation을 validation_rows에 한 번씩 쓰고 주장·도구·phase·등록 범위와 같은 문장의 "
    "별도 실제 실행 및 기대값 비교 책임을 구분한다. 도구·phase는 실제 실행 절차와 그 절차가 관측하는 "
    "범위만 나타내며, 검사 목적을 덧붙인 문장만으로 새 절차나 다른 phase의 능력을 만들지 않는다. "
    "각 부분 책임은 validation_scope_rows에 별도 행으로 쓰며 부분 claim·procedure·phase·근거를 결속한다. "
    "전체 문장에 일부 실제 책임과 일부 과장 책임이 함께 있으면 supported와 contradicted 행을 함께 제출하고, "
    "문장 전체를 한쪽으로 뭉뚱그리지 않는다. Task validation과 independent Goal validation의 scope·소유자를 구분하고, 전역 Task 검사 책임의 "
    "출처와 AC 연결 책임의 출처를 섞지 않는다. 같은 절차가 여러 적용 Task에서 실제 수행되면 각 ID의 "
    "필수 연결을 개별 대조하되, 선택 AC 연결은 유지한다. 도구·phase 언급이나 별도 검사 ID가 있다는 "
    "사실만으로 다른 AC에 연결 의무를 전염시키지 않는다. 독립 결함은 각각 finding과 finding_links로 "
    "연결한다. finding_code는 제출물 안에서 유일해야 한다. finding_links는 AC·validation·Task ID와 "
    "직접 원문 인용을 지정하고 finding.evidence_refs에는 그 인용의 원본 evidence ref를 모두 포함한다. "
    "연결 누락 행은 Goal AC·validation statement·현재 coverage 인용을, 범위 모순 행은 검사 주장과 "
    "실제 수단 범위 인용을 해당 finding_link에도 붙인다. finding summary에는 해당 AC ID와 "
    "validation ID를 원문 그대로 포함한다. integration 결함의 task_refs는 직접 영향 Task만 쓰며 "
    "없으면 비운다. finding의 affected_task_refs와 link.task_refs는 같아야 한다. "
    "ac_link_required=true인데 ID가 빠진 행만 missing_validation_link finding에 연결하고 나머지 AC 행의 "
    "finding_codes는 비운다. 단순 file/diff evidence 언급을 test 입력의 명시적 제외로 추정하지 않는다. "
    "원문이 입력을 제한·제외하여 수단과 충돌하는 경우와 원문 정보가 부족한 경우를 구분한다. contradicted 또는 "
    "unresolved scope 행은 각각 validation_scope 또는 insufficient_evidence finding에 연결한다. "
    "독립적으로 확인한 다른 결함을 이미 제출한 finding이나 낮은 rating으로 대신하지 않는다. "
    "작성자는 결함을 보정한 완성 plan과 대조표를 제출하므로 finding_links와 모든 finding_codes는 "
    "비운다. adapter는 selector·quote·ref·행 집합·scope 참조와 내부 일관성만 검사하며 scope·관계 의미 "
    "정답이나 finding을 추정하지 않고 coverage·scope 행·citation을 생성·보정하거나 Plan을 수정하지 않는다."
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
    criterion_selectors = {
        item.criterion_id: {
            "statement": f"/hard_acceptance/{i}/statement",
            "validation_intent": f"/hard_acceptance/{i}/validation_intent",
        }
        for i, item in enumerate(goal.definition.hard_acceptance)
    }
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

    _require(_unique((row.validation_id for row in inspection.validation_rows), "검사 능력 행") == validation_by_id.keys(),
             "대조표 검사 능력 행 집합 불완전")
    validation_rows_by_id = {row.validation_id: row for row in inspection.validation_rows}
    mechanism_project_refs_by_validation: dict[str, set[str]] = {}
    for validation_row in inspection.validation_rows:
        validation = validation_by_id[validation_row.validation_id]
        selector = validation["selector"] + "/statement"
        _require(has((validation_row.claim_ref,), plan_ref, {selector}), "대조표 검사 주장 인용 오류")
        for mechanism in validation_row.mechanisms:
            refs(mechanism.basis_refs)
        for ref in validation_row.separate_check_refs:
            _require(has((ref,), plan_ref, {selector}), "대조표 별도 검사 책임 인용 오류")
        mechanism_project_refs_by_validation[validation_row.validation_id] = {
            citation.citation_id for mechanism in validation_row.mechanisms
            for citation in refs(mechanism.basis_refs)
            if citation.source_ref in entries
        }

    scope_ids = _unique((row.scope_id for row in inspection.validation_scope_rows), "검사 scope ID")
    scope_by_id = {row.scope_id: row for row in inspection.validation_scope_rows}
    _require({row.validation_id for row in inspection.validation_scope_rows} == validation_by_id.keys(),
             "대조표 검사 scope 행 집합 불완전")
    for row in inspection.validation_scope_rows:
        _require(row.validation_id in validation_by_id, "대조표 검사 scope validation ID 오류")
        validation = validation_by_id[row.validation_id]
        selector = validation["selector"] + "/statement"
        claim = citations.get(row.claim_ref)
        _require(claim is not None and claim.source_ref == plan_ref and claim.selector == selector,
                 "대조표 검사 scope 주장 인용 오류")
        _require(row.claim_ref in row.basis_refs, "대조표 검사 scope claim 근거 누락")
        scope_basis = refs(row.basis_refs)
        _require(any(
            row.procedure == mechanism.tool and row.phase == mechanism.phase and
            set(mechanism.basis_refs) <= set(row.basis_refs)
            for mechanism in validation_rows_by_id[row.validation_id].mechanisms
        ), "대조표 검사 scope 절차·phase·근거 결속 오류")
        mechanism_project_refs_by_validation[row.validation_id].update(
            citation.citation_id for citation in scope_basis if citation.source_ref in entries
        )

    pairs = _unique(((row.criterion_id, row.validation_id) for row in inspection.ac_validation_rows), "AC 검사 행")
    _require(pairs == {(row["criterion_id"], row["validation_id"]) for row in targets["ac_validation_pairs"]},
             "대조표 AC 검사 행 집합 불완전")
    for row in inspection.ac_validation_rows:
        criterion = criterion_selectors[row.criterion_id]
        _require(has(row.basis_refs, "source:goal", {criterion["statement"]}), "대조표 해당 AC statement 인용 누락")
        _require(has(row.basis_refs, "source:goal", {criterion["validation_intent"]}), "대조표 해당 AC validation_intent 인용 누락")
        validation = validation_by_id[row.validation_id]
        validation_selector = validation["selector"] + "/statement"
        validation_statement = _pointer(sources[plan_ref], validation_selector)
        _require(any(
            item.source_ref == plan_ref and item.selector == validation_selector and item.quote == validation_statement
            for item in refs(row.basis_refs)
        ), "대조표 해당 검사 전체 문장 인용 누락")
        _require(mechanism_project_refs_by_validation.get(row.validation_id, set()) <= set(row.basis_refs),
                  "대조표 AC 관계 등록 자료 인용 누락")
        selected_scope_ids = _unique(row.scope_ids, "AC 검사 scope")
        _require(selected_scope_ids <= scope_ids, "대조표 AC 검사 scope ID 오류")
        selected_scopes = [scope_by_id[scope_id] for scope_id in row.scope_ids]
        _require(all(scope.validation_id == row.validation_id for scope in selected_scopes),
                 "대조표 AC 검사 scope validation 불일치")
        if row.ac_link_required:
            _require(bool(selected_scopes) and all(scope.assessment == "supported" for scope in selected_scopes),
                     "대조표 필수 AC 연결의 supported scope 누락")
            _require(all(
                {scope.claim_ref, *scope.basis_refs} <= set(row.basis_refs)
                for scope in selected_scopes
            ), "대조표 AC 연결 scope 근거 누락")
        else:
            _require(not selected_scopes, "대조표 비필수 AC 연결의 scope 참조 모순")
        missing = row.ac_link_required and row.validation_id not in coverage[row.criterion_id].validation_ids
        if missing:
            codes(row.finding_codes, kind="missing_validation_link", criterion=row.criterion_id,
                  validation=row.validation_id, task=validation["task_ref"])
            ci = next(i for i, item in enumerate(definition.goal_coverage) if item.criterion_id == row.criterion_id)
            for code in row.finding_codes:
                basis = links[code].basis_refs
                _require(has(basis, "source:goal", {criterion["statement"]}) and
                         has(basis, "source:goal", {criterion["validation_intent"]}) and
                         any(item.source_ref == plan_ref and item.selector == validation_selector and item.quote == validation_statement
                             for item in refs(basis)) and
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

    for row in inspection.validation_scope_rows:
        validation = validation_by_id[row.validation_id]
        if row.assessment != "supported":
            codes(row.finding_codes,
                  kind="validation_scope" if row.assessment == "contradicted" else "insufficient_evidence",
                  validation=row.validation_id, task=validation["task_ref"])
            for code in row.finding_codes:
                _require(set(row.basis_refs) <= set(links[code].basis_refs),
                         "대조표 검사 scope finding의 직접 근거 누락")
        else:
            _require(not row.finding_codes, "대조표 정상 검사 scope와 finding 모순")

    # 연결·범위 finding을 표 밖에만 선언하거나 정상 행과 모순시키지 않는다.
    linked_codes = {code for row in (*inspection.ac_validation_rows, *inspection.constraint_task_rows,
                                     *inspection.validation_scope_rows)
                    for code in row.finding_codes}
    _require(all(link.finding_code in linked_codes for link in links.values()
                 if link.defect_kind in {"missing_validation_link", "validation_scope", "missing_task_validation", "insufficient_evidence"}),
             "대조표 finding에 대응하는 결함 행이 없습니다.")
    _require(used == citations.keys(), "대조표 미사용 인용")
