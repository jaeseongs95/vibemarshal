from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .domain import (
    ApproachSignature,
    ApprovalClass,
    CommitHorizon,
    DependencyType,
    EffectContract,
    EngineModel,
    GoalCoverage,
    IntegrationValidationContract,
    ModelAssignmentContract,
    PlanContractDefinition,
    PlanContractRevision,
    PlanDependency,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    PlanningBudgetPolicy,
    PreconditionContract,
    ProjectMapRevision,
    RecoveryEnvelope,
    ReviewFinding,
    ReviewRatings,
    ReviewerSubmission,
    RevisionStatus,
    RiskLevel,
    SkeletonDependency,
    StateSnapshot,
    TaskContract,
    TaskKind,
    TaskSkeleton,
    ValidationContract,
    new_id,
    utc_now,
    validate_reviewer_submission_evidence,
)
from .goal import FindingDraft, ReviewDraft
from .planning import (
    compact_project_map,
    goal_validation_requirement_rows,
    skeleton_input_catalog,
    plan_review_evidence_catalog,
    plan_validation_scope_rows,
    validation_comparison_targets,
    skeleton_review_evidence_catalog,
)
from .plan_inspection import (
    PLAN_INSPECTION_INSTRUCTIONS,
    PlanInspection,
    inspection_file_content,
    validate_plan_inspection,
)
from .roles import RoleCallReceipt, RoleCallRequest, StructuredRolePort
from .domain import GoalContractRevision


READ_ONLY_REPORTING_INSTRUCTIONS = (
    "Goal이 read_only이고 파일을 쓰지 않는 분석·보고를 요청하면 보고는 Worker 응답 본문으로 "
    "제공하는 논리적 산출물로 명시한다. produces의 보고 key는 새 프로젝트 파일 생성 권한이 아니다. "
    "프로젝트 파일의 생성·수정·삭제 금지와 새 응답 본문 생성을 구분하고, 응답 본문까지 "
    "전후 무변경이어야 한다는 조건을 만들지 않는다. 보고 내용은 원본 근거와 대조하고 "
    "프로젝트 파일 무변경은 별도 검사로 둔다. 명시적인 파일 산출물 요구나 더 강한 금지 조건을 "
    "응답 보고로 몰래 바꾸거나 파일 쓰기 예외를 발명하지 않는다."
    "read_only의 응답 보고 Task는 inspect 또는 decide로 분류한다. '작성'이라는 동사만으로 "
    "change Task를 만들거나 분석과 보고를 별도 변경 Task로 분할하지 않는다."
    "현재 계획 역할에만 적용되는 명령 금지를 미래 Task에 추가하지 않는다. Goal에 명시된 "
    "실행 금지는 보존하되, 파일 무변경이나 정적 분석 요청만으로 명령 미실행 증명을 새 완료 "
    "조건으로 요구하지 않는다. 실제 명령 미실행이 계약 조건이면 file·diff만으로 입증했다고 "
    "하지 말고 실행 관측의 범위와 충분성을 검증 계약에 명시한다."
)


PLANNING_PROJECT_PATH_INSTRUCTIONS = (
    "대상 프로젝트는 Goal의 명시적 대상과 Project Map.root를 대조해 판단한다. "
    "Project Map.entries의 kind=reference 또는 registered_reference 자료는 등록 참고자료이며 "
    "그 파일의 부모 디렉터리가 대상 프로젝트라는 뜻이 아니다. 자료 경로의 실행명·날짜·버전이나 "
    "역할 실행 cwd가 다르다는 사실만으로 target 변경·stale root를 추론하지 않는다. 역할 cwd는 "
    "계획·검토 프로세스의 실행 위치이며 Goal의 대상 프로젝트를 변경하지 않는다. "
    "실제 Goal의 명시 대상과 Map.root의 충돌, 후보의 project_map_digest·State binding 불일치, "
    "State의 freshness 위반은 제공된 evidence에서 직접 확인해 지적한다. 관련 입력이 일치하면 "
    "참고자료의 위치에서 다른 workspace 경로를 만들어 finding의 근거로 사용하지 않는다."
)


PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS = (
    "Task validation은 해당 Task의 산출물·완료 조건을 검사한다. 필요한 테스트 작성·실행이나 "
    "선행 산출물의 독립 검토 Task는 허용한다. 반면 모든 Task 완료 후 Core가 수행하는 독립 "
    "Goal Test는 Plan.integration_validations의 책임이며 일반 Task로 재귀 배치하지 않는다. "
    "contributes_to와 goal_coverage.task_refs는 AC 충족에 기여하는 산출물·근거의 연결이지, "
    "연결된 Task가 그 AC의 모든 검사 절차를 직접 실행한다는 뜻이 아니다. 독립 Goal Test "
    "AC도 관련 산출물·근거를 제공하는 Task에 연결한다. 필요하면 Skeleton의 detail_requirements에 "
    "후속 integration validation 책임을 명확히 하고, 상세 Plan의 validation_ids로 해당 검사에 연결한다. "
    "Skeleton에 Goal Test 전용 노드나 상세 integration_validations가 없다는 이유만으로 "
    "Task 추가를 요구하지 않는다. Goal과 AC 기여 관계로 이미 전달된 요구를 선택 필드인 "
    "detail_requirements에 반복하지 않았다는 이유만으로 Skeleton을 차단하지 않는다. "
    "후속 단계에서 누락될 수 있다는 가정은 현재 결함의 직접 evidence가 아니다. 실제 AC 기여 "
    "누락·상충하는 Task 요구와 상세 Plan의 독립 검사·evidence_mode·validation ID 연결 누락은 검토한다. "
    "같은 대상을 검사한다는 이유만으로 Task validation과 Goal Test를 중복으로 판정하지 않는다. "
    "Task 이름·개수가 아니라 목적·선행조건·산출물의 책임을 대조한다. 일반 Task가 자신을 "
    "포함한 모든 Task의 검증 완료나 이후 Core Goal Test 결과를 기다리면 계약의 자기의존을 "
    "직접 evidence로 지적한다. 자연어 선행조건 충돌을 명시적 DAG cycle이나 관측된 교착으로 "
    "단정하지 않는다."
    "Goal의 AC 또는 전역 constraint가 각 Task 또는 특정 범위 Task의 완료 전에 요구한 검증은 AC 기여 관계와 별개인 "
    "해당 Task 자체의 필수 책임이다. Goal의 적용 범위를 각 Task와 대조하고, 상세 Plan에서는 "
    "그 Task.validations에 검사 목적·method·필수 evidence 종류를 보존한다. 후속 검증 Task나 "
    "integration validation에만 검사를 두거나 acceptance_criteria에 문장만 적어 이를 대체하지 않는다. "
    "Core는 선행 Task 자체의 validation을 통과한 뒤 dependency를 해제하므로, 그 Task의 완료에 "
    "필요한 evidence를 후속 Task에서 받도록 계획하지 않는다. Executor/Validator 모델 배정과 "
    "independence_required는 역할 배정이며 검증 호출·evidence를 대신하지 않는다. 예를 들어 AC가 "
    "해당 Task에 실제 테스트·파일 범위 검사와 독립 모델 검토를 요구하면 그 Task에 deterministic "
    "command/test·file/diff 검사와 semantic model_review 검사를 각각 명시한다. 이러한 검사 종류를 "
    "Goal의 요구 범위 밖 Task에 일괄 강제하지 않는다. Task별 필수 검증은 detail_requirements나 "
    "contributes_to에 반복되지 않아도 Goal에서 상속하며, 상세 Plan에서 실제 누락된 경우에만 "
    "Goal과 해당 Task.validations를 직접 근거로 지적한다."
    "Goal의 검사 요구는 두 출처로 분리한다. 전역 constraint가 Task 검증을 요구하면 적용 Task의 "
    "validations에 그 검사가 존재하는지 먼저 확인한다. 이 전역 의무만으로 특정 AC의 validation_ids "
    "연결을 추정하지 않는다. 반대로 AC의 statement 또는 validation_intent가 검사 대상·절차·적용 "
    "범위를 명시하면, 그 절차를 실제 수행하는 validation ID를 해당 AC에 연결한다. AC가 명시적으로 "
    "semantic 검토를 요구하면 그 semantic ID의 필수 연결도 같은 방식으로 확인한다. 명시 요구가 "
    "없는 기존 선택적 연결도 허용한다. 검사 문장의 동작·공개 "
    "계약 언급, 전역 의무, evidence 종류나 단순 선후조건만으로 다른 AC에 semantic ID를 일괄 연결하지 "
    "않는다. "
    "Skeleton과 Plan 작성 draft의 goal_coverage.task_refs는 기여 Task 집합이다. Compiler는 "
    "draft의 task_refs를 Task.task_id로 변환하여 PlanContractDefinition.goal_coverage.task_ids에 "
    "기록한다. Reviewer가 받는 컴파일된 Plan의 task_ids는 정상 필드다. 실제 Task.task_id에 "
    "대조해 연결을 확인하고 Task.task_ref로 대응시켜 기여 집합을 비교한다. 이를 task_refs로 "
    "바꾸라고 요구하거나 내부 task_id 자체를 잘못된 참조로 판정하지 않는다. Reviewer finding의 "
    "affected_task_refs는 별도로 Task.task_ref를 사용한다. "
    "기여 Task 연결과 validation_ids는 서로 다른 연결이다. draft의 task_refs는 Skeleton의 기여 "
    "Task 집합을 그대로 보존하고 validation_ids의 소유 Task를 그 task_refs로 제한하지 않는다. AC가 "
    "명시한 절차가 여러 적용 Task에서 수행되면 그 검사 ID를 모두 연결하되, 이 연결을 추가하려고 "
    "task_refs나 contributes_to를 바꾸지 않는다. 특정 Task에만 적용되는 요구는 그 범위를 유지한다. "
    "검토 시 전역 Task 검사 존재와 AC별 validation_ids 연결을 각각 확인한다. 검사 계약이 존재해도 "
    "명시 AC 연결만 빠졌으면 Goal·해당 Task.validations·goal_coverage를 직접 근거로 연결 누락을 "
    "지적하고, 실행 누락이나 새 검사 의무로 바꾸지 않는다."
)


PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS = (
    "검사 수단과 검사 주장은 다음 순서로 대조한다. "
    "1. 모든 Task.validations와 integration_validations의 statement를 읽고, 각 검사 목적에 "
    "대응하는 수단(도구·phase·mode 또는 별도 실제 검사)을 구분한다. 참조한 등록 자료의 관련 "
    "본문과 필요한 구현 분기·입력·출력을 Project Map의 경로에서 확인한다. 경로·도구 이름·"
    "evidence enum·Goal의 기대 동작만으로 그 수단의 검사 능력을 추정하지 않는다. "
    "2. 선택한 수단이 실제 관측하는 범위와 문장이 그 수단에 부여한 범위를 대조한다. 같은 "
    "도구라도 다른 phase의 검사를 합치지 않는다. 선언·시그니처 검사나 기존 unittest 통과는 "
    "해당 절차가 실행하지 않는 입력·호출 방식까지 검사했다는 근거가 아니다. 독립 실행 여부와 "
    "검사 범위도 별개다. 지원하지 않는 검사를 같은 phase로 새로 실행해도 범위 모순은 남는다. "
    "3. 수단에 없는 검사가 필요하면 별도로 무엇을 실행하고 어떤 기대 결과와 비교할지 "
    "statement에 명시한다. 같은 validation ID·문장 안에 이 별도 검사 책임을 둘 수 있으며 "
    "새 ID나 구체 argv가 필수는 아니다. 예를 들어 '도구 실행에 더해 별도로 입력을 호출하고 "
    "기대값과 비교한다'는 추가 책임이다. '도구 실행으로 입력 동작까지 확인한다'처럼 목적만 "
    "덧붙이면 그 도구의 능력 주장이다. tool·phase는 실제 절차와 그 절차의 관측 범위만 식별하며, "
    "검사 목적이나 independent라는 label만으로 Task scope·Goal scope 또는 다른 phase의 실행을 "
    "발명하지 않는다. 등록 수단의 범위만 정확히 쓰는 경우도 허용하되 Goal이 "
    "해당 Task에 요구한 필수 검사를 약화하거나 Goal Test에만 넘기지 않는다. Goal이 요구하지 "
    "않은 포괄 검사를 모든 Task에 추가하지 않는다. "
    "4. Reviewer는 각 문장과 실제 수단의 직접 모순을 verification finding으로 제출한다. "
    "summary에 validation ID·명시한 수단·실제로 지원하지 않는 주장과 근거를 간결히 적고 "
    "제공된 Plan·등록 자료의 evidence ref에 결속한다. 낮은 rating은 확인된 계약 모순의 finding을 "
    "대신하지 않는다. 다른 연결 결함을 발견해도 남은 검사 문장의 대조를 끝내며, 별도 직접 "
    "증거가 있는 범위 모순을 빠뜨리지 않는다. 최소 finding은 같은 원인의 중복·추측을 줄이라는 "
    "뜻이며 독립적으로 확인한 결함을 숨기라는 뜻이 아니다. "
    "검사 수단·phase는 계약 의미로 식별할 수 있고 구체 argv는 ready-time에 확정한다. 이미 "
    "명시한 수단과 검사 목적의 충돌을 이후 명령 변경으로 해결할 운영 상세로 분류하지 않는다. "
    "required_evidence_kinds는 해당 validation 계약이 요구하는 evidence 종류이지, 이후 semantic "
    "Validator에 제공할 직접 evidence catalog의 허용 목록이 아니다. 현재 실행의 file·diff·command·"
    "test·build 직접 관측은 이 목록에 없다는 이유로 제외됐다고 추정하지 않는다. "
    "external_observation Worker 보고의 포함 여부만 별도 계약으로 제어한다. 실행 전 Plan 검토에서 "
    "아직 없는 runtime catalog의 누락을 finding으로 만들지 않는다. 정상 Task 검사와 independent Goal "
    "Test의 범위 차이, 명시된 별도 실제 검사 책임은 허용한다. 불완전한 자료와 확인된 모순은 "
    "구분하며, 없는 검사 능력이나 근거 없는 누락을 만들지 않는다."
)


PLAN_VALIDATION_TRACE_INSTRUCTIONS = (
    "상세 Plan의 검사 검토는 다음 순서로 한다. 1) Goal의 전역 constraints가 요구한 Task 자체 "
    "validation의 존재를 확인한다. 2) AC의 statement·validation_intent가 명시한 검사 대상·절차·"
    "적용 범위를 읽고, 모든 validation.statement 전체에서 그 절차를 실제 수행하는 ID를 찾아 해당 "
    "goal_coverage.validation_ids와 양방향 대조한다. 3) 등록 수단의 phase·mode 능력과 검사 문장의 "
    "주장을 대조한다. 4) Worker의 응답 제출과 후속 Validator 결과의 순서를 확인한다. "
    "전역 constraint의 검사 존재와 AC별 ID 연결은 별도 판정이다. 전역 semantic 의무나 검사 문장의 "
    "동작·공개 계약 언급을 모든 AC의 연결 의무로 확대하지 않는다. 반대로 AC가 명시한 절차라면 "
    "이름에 test가 없어도 그 절차를 포함하는 복합 검사 ID를 빠뜨리지 않는다. 다른 Task나 Goal 검사 "
    "ID가 이미 연결되어 있어도 적용 대상의 명시 검사 연결을 대신하지 않는다. 검사 자체가 존재하지만 "
    "ID만 빠진 경우에는 연결 누락으로 지적하고 실행 누락이나 새 검사 의무로 바꾸지 않는다. 같은 "
    "절차가 복수 Task 또는 Goal scope에 실제 존재하면 각 validation ID의 적용 범위와 필수 연결을 "
    "개별 판정하며, Task 기여 scope나 전역 책임만으로 이를 합치지 않는다. 전역 검사 책임의 출처와 "
    "AC별 연결 책임의 출처도 분리하고, 도구·phase 언급이나 다른 ID의 별도 검사만으로 필수 연결을 "
    "전염시키지 않는다. 단순 "
    "선후조건, 같은 파일·evidence 종류 또는 선택적 부가 검사만으로 모든 AC에 연결하지 않는다. "
    "필수 연결이 아니라는 분류는 이미 존재하는 선택적 연결을 금지하지 않는다. 전역 semantic 의무와 "
    "선택적 semantic 연결은 함께 존재할 수 있다. Plan이 특정 evidence를 언급해도 다른 evidence를 "
    "명시적으로 제외한 것으로 추정하지 않는다. 명시적 제외·범위 충돌과 단순 언급은 구분한다. "
    "Reviewer는 Goal·검사 원문·실제 coverage를 직접 evidence로 대조하고, 작성자는 동일한 기준으로 "
    "검사 문장과 연결을 함께 완성한다. 등록 자료·구현의 범위를 판단에 썼다면 정식 project citation으로 "
    "그 범위 판단과 AC 관계 판단을 각각 추적 가능하게 남긴다."
)


def inspection_source_catalog(project_map: ProjectMapRevision, sources: dict[str, str]) -> dict[str, Any]:
    """등록 자료의 정식 주소와 검증한 원문을 두 Plan 역할에 동일하게 투영한다.

    원래 ID·본문을 보존하며 관계·검사 능력·필수 연결 판정을 추가하지 않는다.
    """
    catalog: dict[str, Any] = dict(sources)
    for entry in project_map.entries:
        source_ref = f"project:{entry.entry_id}"
        item = {
            "source_ref": source_ref,
            "selector": "/content",
            "path": entry.path,
            "content_digest": entry.content_digest,
            "evidence_ref": "source:project_map",
        }
        if entry.kind.value in {"reference", "instruction"}:
            item["content"] = inspection_file_content(entry, project_map)
        catalog[source_ref] = item
    return catalog


PLAN_TASK_RESULT_BOUNDARY_INSTRUCTIONS = (
    "Task 실행 순서는 Worker의 작업·응답 제출 → Core의 evidence 수집과 해당 Task 검증 "
    "→ Task 완료 판정이다. Worker 실행 종료와 Task 검증 완료를 구분한다. Worker는 자기 작업·"
    "관측·산출물 근거를 제출하고, 이후 독립 Validator가 이를 검토해 별도 결과를 제출한다. "
    "Task 완료 조건에 독립 Validator 통과를 요구하는 것은 정상이다. 다만 그 후속 Validator "
    "결과를 같은 Task의 Worker 응답·산출물에 포함하거나 Worker 종료 전에 확보하라고 요구하면 "
    "생성·소비 순서의 충돌이다. produces·consumes·preconditions·acceptance_criteria와 validation "
    "입력을 함께 대조한다. 이미 검증된 선행 Task의 Validator 결과를 후속 Task가 인용하는 것은 "
    "허용하며, 자신의 미래 검토 결과를 인용하는 경우와 구분한다. read_only 응답 보고의 작성 "
    "주체를 자동으로 Worker로 지정해 후속 Validator의 독립 보고까지 Worker에게 전가하지 않는다. "
    "Reviewer는 충돌한 완료 조건·검사 입력을 직접 근거로 제출한다. 자연어의 시점 충돌을 명시적 "
    "DAG cycle이나 실제 관측된 runtime 교착으로 확대하지 않는다."
)


class PlannerRoleAdapterError(RuntimeError):
    pass


class PlanReviewDraft(ReviewDraft):
    findings: tuple[FindingDraft, ...] = Field(
        description="직접 확인한 Plan 계약 결함. 검사 수단·phase의 범위, 복합 검사 문장 전체와 AC의 필수 ID 연결, Worker 산출물과 후속 Validator 입력·결과 순서를 각각 대조한다. 연결 누락과 실행 누락, Task의 Validator 통과 조건과 Worker가 미래 검토 결과를 미리 제출하는 충돌을 구분한다. 독립 결함은 각각 직접 evidence로 제출하고 다른 finding이나 낮은 rating으로 대신하지 않는다. 같은 원인의 중복·추측은 제외한다.",
    )
    ratings: ReviewRatings | None = Field(
        description="직접 근거가 있는 finding이 전혀 없을 때만 후보 품질을 평가한다. 확인된 계약 모순이 있으면 findings를 제출하고 ratings는 null이다. 낮은 점수는 후보 차단이나 finding을 대신하지 않는다.",
    )


class SkeletonTaskDraft(EngineModel):
    task_ref: str
    kind: TaskKind
    objective: str
    contributes_to: tuple[str, ...] = Field(description="산출물·근거로 기여하는 Goal AC ID. 연결된 모든 검사 절차를 이 Task가 직접 실행한다는 뜻은 아니다.")
    produces: tuple[str, ...]
    consumes: tuple[str, ...] = Field(default=(), description="제공된 external_input_catalog의 정확한 key 또는 data dependency producer의 산출물 key. 경로·설명을 임의 key로 만들지 않는다.")
    risk_tags: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    no_op_when: tuple[str, ...] = ()
    unknown_refs: tuple[str, ...] = Field(default=(), description="현재 StateSnapshot.unknowns에 실제 있는 unknown_id만 참조한다. 없으면 빈 배열. 새 질문은 candidate.unknowns에 설명한다.")
    detail_requirements: tuple[str, ...] = Field(default=(), description="상세 Plan에서 보존할 책임. Goal이 해당 Task에 요구한 자체 검증과 모든 Task 완료 후 Core의 integration validation을 구분한다. 이 필드의 생략은 Goal의 Task별 검증 요구를 면제하지 않으며 실행 명령은 넣지 않는다.")


class SkeletonDependencyDraft(EngineModel):
    producer_task_ref: str
    consumer_task_ref: str
    dependency_type: DependencyType
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()


class SkeletonCandidateDraft(EngineModel):
    approach: ApproachSignature
    tasks: tuple[SkeletonTaskDraft, ...] = Field(min_length=1)
    dependencies: tuple[SkeletonDependencyDraft, ...] = ()
    goal_coverage: tuple[GoalCoverage, ...] = Field(min_length=1, description="각 AC에 기여하는 Task 연결. 독립 Goal Test AC의 연결은 검사 실행 주체가 아니라 입력 산출물·근거의 제공 책임이다.")
    unknowns: tuple[str, ...] = ()
    estimated_change_cost: int = Field(ge=0)
    estimated_context_tokens: int = Field(ge=0)


class SkeletonBatchDraft(EngineModel):
    candidates: tuple[SkeletonCandidateDraft, ...] = Field(min_length=1, max_length=3)


class PlanTaskValidationDraft(ValidationContract):
    statement: str = Field(
        min_length=1,
        max_length=3000,
        description="이 Task 완료 전에 실제 수행할 검사 수단과 그 수단이 관측하는 범위. 선언·annotation·시그니처를 확인하는 절차에는 실행하지 않는 입력·호출 검사를 부여하지 않는다. 별도 실제 검사가 필요하면 같은 문장에 추가 실행과 기대값 비교 책임을 명시한다. Goal이 요구한 해당 Task의 검사 의무를 유지하며 구체 argv는 나중에 확정한다.",
    )


class PlanIntegrationValidationDraft(IntegrationValidationContract):
    statement: str = Field(
        min_length=1,
        max_length=5000,
        description="Task 검사 이후 수행할 Goal 검사 수단과 실제 검사 범위. independent는 새 실행·evidence의 구분이며 도구의 선택 phase가 수행하지 않는 검사 능력을 보충하지 않는다. 필요한 입력·호출·기대값 비교를 지원하는 수단 또는 별도 실제 검사 책임을 명시한다. 구체 argv는 나중에 확정한다.",
    )


class DetailedTaskDraft(EngineModel):
    task_ref: str
    kind: TaskKind
    objective: str
    goal_criterion_refs: tuple[str, ...] = Field(min_length=1, description="Skeleton의 contributes_to를 그대로 보존한다. 다른 AC의 필수 검사 ID를 연결하려고 이 기여 집합을 바꾸지 않는다. 검사 연결은 goal_coverage.validation_ids에 둔다.")
    produces: tuple[str, ...] = Field(min_length=1)
    consumes: tuple[str, ...] = ()
    preconditions: tuple[PreconditionContract, ...] = ()
    expected_effects: tuple[EffectContract, ...] = Field(default=(), description="Task가 실제 발생시키는 효과만 포함한다. 파일 무변경·외부 효과 없음 같은 미발생 조건은 금지 효과나 완료 조건에 둔다.")
    prohibited_effects: tuple[EffectContract, ...] = Field(default=(), description="Task가 발생시키면 안 되는 효과. 로컬 파일 mutation과 외부 시스템 효과를 별도 항목으로 작성하고 각 external 값을 해당 범위에 맞춘다.")
    required_capabilities: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, description="Worker 작업·응답 제출 뒤 검증까지 포함한 Task 완료 조건. 독립 Validator 통과는 정상 조건이나, Worker 응답을 입력으로 나중에 수행하는 Validator의 결과를 같은 Worker가 미리 제출하도록 요구하지 않는다. Worker의 실행 보고와 Validator의 별도 검사 결과를 구분한다.")
    validations: tuple[PlanTaskValidationDraft, ...] = Field(min_length=1, description="이 Task 완료 전에 필요한 실제 검사 계약. statement에 Goal이 이 Task에 요구한 검사 대상·종류·실행 목적을 보존하고 method·필수 evidence 종류를 함께 명시한다. 등록 도구·phase를 참조하면 제공된 자료의 실제 검사 범위와 일치해야 한다. 부족한 필수 검사는 별도 검사 책임으로 명시하며 다른 phase의 능력을 부여하지 않는다. 기존 unittest 실행 요구를 일반 동작 확인이나 test enum만으로 대체하지 않는다. 후속 Task 검사, 모델 배정, AC 문장이나 integration validation으로 자체 필수 검증을 대체하지 않는다.")
    risk_level: RiskLevel
    risk_tags: tuple[str, ...] = ()
    approval_class: ApprovalClass = ApprovalClass.PLAN_ACTIVATION
    recovery: RecoveryEnvelope


class PlanDependencyDraft(EngineModel):
    producer_task_ref: str
    consumer_task_ref: str
    dependency_type: DependencyType
    products: tuple[str, ...] = ()


class PlanGoalCoverageDraft(EngineModel):
    criterion_id: str
    task_refs: tuple[str, ...] = Field(min_length=1, description="Skeleton의 해당 AC 기여 Task 집합을 그대로 보존한다. validation_ids 소유 Task의 허용 목록이 아니며 검사 ID를 연결하려고 이 집합을 확대하지 않는다.")
    validation_ids: tuple[str, ...] = Field(min_length=1, description="해당 AC의 statement·validation_intent가 명시한 절차를 실제 수행하는 Task 또는 integration validation ID. 모든 검사 statement의 복합 책임과 AC의 적용 범위를 대조한다. 전역 constraint가 요구한 Task 자체 검사는 존재를 보존하되, 그 전역 의무만으로 각 AC에 ID 연결을 추정하지 않는다. AC가 명시한 절차를 수행하는 필수 Task 검사 ID는 소유 Task가 task_refs에 없어도 다른 ID가 이미 연결돼도 빠뜨리지 않는다. 단순 선후조건·검사 문장의 연관 표현·선택적 부가 검사로 전체 ID를 일괄 연결하지 않는다. Core의 독립 Goal Test는 integration validation ID로 연결한다.")


class PlanExpansionDraft(EngineModel):
    tasks: tuple[DetailedTaskDraft, ...] = Field(min_length=1)
    dependencies: tuple[PlanDependencyDraft, ...] = ()
    goal_coverage: tuple[PlanGoalCoverageDraft, ...] = Field(min_length=1)
    integration_validations: tuple[PlanIntegrationValidationDraft, ...] = Field(min_length=1, description="Task 검사와 분리한 plan-level Goal 검사 계약. 참조한 등록 도구·phase가 statement의 검사 범위를 실제 지원하는지 대조한다. 다른 phase의 검사 능력이나 Task evidence로 independent 검사를 대체하지 않는다.")
    expected_effects: tuple[str, ...] = Field(default=(), description="Plan이 실제 발생시키는 효과. 효과가 없다는 부정형 조건은 포함하지 않는다.")
    prohibited_effects: tuple[str, ...] = Field(default=(), description="Goal이 금지한 효과와 범위를 보존하며 현재 계획 역할의 행동 제한을 새로 추가하지 않는다.")


def _inline_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic 단일 model schema의 local $defs를 provider branch 안에 전개한다."""
    root = deepcopy(schema)
    definitions = root.pop("$defs", {})

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name not in definitions:
                raise RuntimeError("Plan review schema의 local $defs ref를 찾지 못했습니다: " + name)
            resolved = deepcopy(definitions[name])
            resolved.update({key: item for key, item in value.items() if key != "$ref"})
            return visit(resolved)
        return {key: visit(item) for key, item in value.items()}

    return visit(root)


class PlanExpansionEnvelope(EngineModel):
    plan: PlanExpansionDraft
    inspection: PlanInspection


class PlanReviewEnvelope(EngineModel):
    review: PlanReviewDraft
    inspection: PlanInspection

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        """지원되지 않는 root allOf 대신 review 속성의 nested anyOf로 배타 조건을 전송한다."""
        schema = handler(core_schema)
        # parent handler 뒤에 $defs를 조립하므로 review branch에는 local ref를 남기지 않는다.
        review_schema = _inline_local_schema_refs(PlanReviewDraft.model_json_schema())
        properties = review_schema["properties"]
        empty_findings = deepcopy(properties["findings"])
        empty_findings["maxItems"] = 0
        nonempty_findings = deepcopy(properties["findings"])
        nonempty_findings["minItems"] = 1
        rating_value = deepcopy(properties["ratings"])
        rating_value["anyOf"] = [
            value for value in rating_value.get("anyOf", ()) if value != {"type": "null"}
        ]
        if not rating_value["anyOf"]:
            raise RuntimeError("Plan review rating schema에 non-null ReviewRatings branch가 없습니다.")
        schema["properties"]["review"] = {
            "description": "finding/rating 두 key가 항상 있는 비권위 Reviewer 제출물",
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"findings": empty_findings, "ratings": rating_value},
                    "required": ["findings", "ratings"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"findings": nonempty_findings, "ratings": {"type": "null"}},
                    "required": ["findings", "ratings"],
                    "additionalProperties": False,
                },
            ],
        }
        return schema


class TaskAssigner(Protocol):
    def assign(self, task: DetailedTaskDraft) -> ModelAssignmentContract: ...


@dataclass(frozen=True)
class RuleBasedTaskAssigner:
    inspect_assignment: ModelAssignmentContract
    standard_assignment: ModelAssignmentContract
    critical_assignment: ModelAssignmentContract

    def assign(self, task: DetailedTaskDraft) -> ModelAssignmentContract:
        if task.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return self.critical_assignment
        if task.kind in {TaskKind.INSPECT, TaskKind.DECIDE} and task.risk_level is RiskLevel.LOW:
            return self.inspect_assignment
        return self.standard_assignment


def _case_ref(digest: str) -> str:
    return "case-" + digest.split(":", 1)[1][:16]


def _review_submission(
    *,
    role: str,
    artifact_digest: str,
    evidence_catalog: dict[str, Any],
    draft: ReviewDraft,
) -> ReviewerSubmission:
    findings = tuple(
        ReviewFinding(
            finding_code=item.finding_code,
            gate=item.gate,
            severity=item.severity,
            summary=item.summary,
            evidence_refs=item.evidence_refs,
            affected_task_refs=item.affected_task_refs,
            remediable=item.remediable,
        )
        for item in draft.findings
    )
    return ReviewerSubmission(
        reviewer_role=role,
        candidate_digest=artifact_digest,
        findings=findings,
        ratings=draft.ratings,
        evidence_catalog_digest=sha256_digest(evidence_catalog),
    )


@dataclass
class SkeletonGeneratorAdapter:
    runner: StructuredRolePort
    model: str
    effort: str
    inventory_digest: str
    cwd: Path | str
    receipts: list[RoleCallReceipt] = field(default_factory=list)

    def generate(
        self,
        *,
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
        candidate_count: int,
    ) -> tuple[PlanSkeletonCandidate, ...]:
        request = RoleCallRequest(
            role="skeleton_generator",
            instructions=(
                "Goal과 현재 State에 맞는 최소 Plan Skeleton을 만든다. 실제 파일·symbol·명령은 "
                "상세화하지 않는다. 명확한 단일 변경은 하나만 만들고, 요청된 후보가 복수일 때는 "
                "이름이 아니라 전략이 달라야 한다. Task 목적·DAG·produces/consumes·unknown만 출력한다."
                "외부 입력 consumes는 external_input_catalog의 정확한 key만 사용한다. "
                "Task 산출물을 소비하면 같은 key의 data dependency로 producer와 연결한다. "
                "unknown_refs는 StateSnapshot에 실제 있는 unknown_id만 쓰고 없으면 비운다."
                "현재 출력은 실행 전 계획 후보다. 사용자가 이후 정확한 Plan digest를 활성화하며, "
                "아직 없는 활성화 증적을 consumes로 요구하거나 별도 승인 Task를 발명하지 않는다. "
                "read_only는 프로젝트 산출물 변경 금지이며 읽기 관측·계획 제안 자체를 금지하지 않는다."
            ) + PLANNING_PROJECT_PATH_INSTRUCTIONS + PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS + READ_ONLY_REPORTING_INSTRUCTIONS,
            payload={
                "case_ref": _case_ref(goal.definition_digest),
                "candidate_count": candidate_count,
                "goal": goal.definition.model_dump(mode="json"),
                "state": state.model_dump(mode="json"),
                "project_map": compact_project_map(project_map),
                "external_input_catalog": skeleton_input_catalog(state, project_map),
            },
            output_schema=SkeletonBatchDraft.model_json_schema(),
            model=self.model,
            effort=self.effort,
            inventory_digest=self.inventory_digest,
            cwd=str(Path(self.cwd).resolve()),
        )
        def validate_batch(value: dict[str, Any]) -> SkeletonBatchDraft:
            batch = SkeletonBatchDraft.model_validate(value)
            if len(batch.candidates) != candidate_count:
                raise PlannerRoleAdapterError("generator 후보 수가 Core 요청과 다릅니다.")
            for item in batch.candidates:
                self._compile(
                    item,
                    goal=goal,
                    state=state,
                    parent_candidate_id=None,
                    version=1,
                    refinement_round=0,
                )
            return batch

        result = self.runner.run(request, validator=validate_batch)
        self.receipts.append(result.receipt)
        batch = SkeletonBatchDraft.model_validate(result.payload)
        if len(batch.candidates) != candidate_count:
            raise PlannerRoleAdapterError("generator 후보 수가 Core 요청과 다릅니다.")
        return tuple(
            self._compile(
                item,
                goal=goal,
                state=state,
                parent_candidate_id=None,
                version=1,
                refinement_round=0,
            )
            for item in batch.candidates
        )

    def refine(
        self,
        *,
        candidate: PlanSkeletonCandidate,
        findings: tuple[ReviewFinding, ...],
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
    ) -> PlanSkeletonCandidate:
        request = RoleCallRequest(
            role="skeleton_refiner",
            instructions=(
                "기존 Skeleton의 remediable finding만 한 번 수정한다. Goal 의미나 접근 전략을 "
                "몰래 바꾸지 말고 실제 파일·명령을 상세화하지 않는다. "
                "finding은 비권위 관측이므로 Goal과 단계별 책임에 대조해 보정한다. 독립 Goal Test "
                "책임은 관련 Task의 기여·detail_requirements로 명확히 하며, 지적을 그대로 수행해 "
                "Core의 검사를 기다리는 일반 Task를 추가하지 않는다. 실제 Task 검증 결함은 수정한다. "
                "consumes는 external_input_catalog의 key 또는 data dependency의 산출물과 일치해야 한다. "
                "unknown_refs는 StateSnapshot.unknowns의 실제 ID만 쓴다. 새 정보는 발명하지 않는다."
            ) + PLANNING_PROJECT_PATH_INSTRUCTIONS + PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS + READ_ONLY_REPORTING_INSTRUCTIONS,
            payload={
                "case_ref": _case_ref(sha256_digest(candidate)),
                "goal": goal.definition.model_dump(mode="json"),
                "state": state.model_dump(mode="json"),
                "project_map": compact_project_map(project_map),
                "external_input_catalog": skeleton_input_catalog(state, project_map),
                "candidate": candidate.model_dump(mode="json"),
                "findings": [item.model_dump(mode="json") for item in findings],
            },
            output_schema=SkeletonCandidateDraft.model_json_schema(),
            model=self.model,
            effort=self.effort,
            inventory_digest=self.inventory_digest,
            cwd=str(Path(self.cwd).resolve()),
        )
        def validate_refinement(value: dict[str, Any]) -> SkeletonCandidateDraft:
            draft = SkeletonCandidateDraft.model_validate(value)
            refined = self._compile(
                draft,
                goal=goal,
                state=state,
                parent_candidate_id=candidate.candidate_id,
                version=candidate.version + 1,
                refinement_round=1,
            )
            if refined.approach.strategy_family != candidate.approach.strategy_family:
                raise PlannerRoleAdapterError("Skeleton refinement가 접근 전략을 바꿨습니다.")
            return draft

        result = self.runner.run(request, validator=validate_refinement)
        self.receipts.append(result.receipt)
        refined = self._compile(
            SkeletonCandidateDraft.model_validate(result.payload),
            goal=goal,
            state=state,
            parent_candidate_id=candidate.candidate_id,
            version=candidate.version + 1,
            refinement_round=1,
        )
        if refined.approach.strategy_family != candidate.approach.strategy_family:
            raise PlannerRoleAdapterError("Skeleton refinement가 접근 전략을 바꿨습니다.")
        return refined

    @staticmethod
    def _compile(
        draft: SkeletonCandidateDraft,
        *,
        goal: GoalContractRevision,
        state: StateSnapshot,
        parent_candidate_id: str | None,
        version: int,
        refinement_round: int,
    ) -> PlanSkeletonCandidate:
        return PlanSkeletonCandidate(
            candidate_id=new_id("candidate"),
            goal_contract_digest=goal.definition_digest,
            state_signature=state.semantic_digest,
            approach=draft.approach,
            tasks=tuple(
                TaskSkeleton(
                    task_ref=item.task_ref,
                    kind=item.kind,
                    objective=item.objective,
                    contributes_to=item.contributes_to,
                    produces=item.produces,
                    consumes=item.consumes,
                    risk_tags=item.risk_tags,
                    required_capabilities=item.required_capabilities,
                    no_op_when=item.no_op_when,
                    unknown_refs=item.unknown_refs,
                    detail_requirements=item.detail_requirements,
                )
                for item in draft.tasks
            ),
            dependencies=tuple(
                SkeletonDependency(
                    producer_task_ref=item.producer_task_ref,
                    consumer_task_ref=item.consumer_task_ref,
                    dependency_type=item.dependency_type,
                    produces=item.produces,
                    consumes=item.consumes,
                )
                for item in draft.dependencies
            ),
            goal_coverage=draft.goal_coverage,
            unknowns=draft.unknowns,
            estimated_change_cost=draft.estimated_change_cost,
            estimated_context_tokens=draft.estimated_context_tokens,
            parent_candidate_id=parent_candidate_id,
            version=version,
            refinement_round=refinement_round,
        )


@dataclass
class SkeletonReviewerAdapter:
    runner: StructuredRolePort
    model: str
    effort: str
    inventory_digest: str
    cwd: Path | str
    receipts: list[RoleCallReceipt] = field(default_factory=list)

    def review(self, *, candidate, goal, state, project_map) -> ReviewerSubmission:
        digest = sha256_digest(candidate)
        evidence_catalog = skeleton_review_evidence_catalog(
            candidate, goal, state, project_map
        )
        request = RoleCallRequest(
            role="skeleton_reviewer",
            instructions=(
                "Skeleton의 goal fit, grounding, DAG, dead-end, 실제 전략 차이를 compact하게 검토한다. "
                "직접 evidence가 있는 최소 finding만 제출하고 status·최종 score를 선언하지 않는다."
                "Skeleton schema에 없는 상세 validation·integration_validations·실행 명령은 "
                "다음 Plan/ExecutionSpec 단계의 책임이다. 그 필드 부재만으로 Skeleton을 차단하지 않는다. "
                "Plan 후보는 아직 승인되지 않은 것이 정상이며 미래 activation 증적의 부재는 dead-end가 아니다. "
                "read_only는 파일 mutation 계약이지 모든 명령·검사를 금지하는 sandbox가 아니다."
            ) + PLANNING_PROJECT_PATH_INSTRUCTIONS + PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS,
            payload={"case_ref": _case_ref(digest), "evidence_catalog": evidence_catalog},
            output_schema=ReviewDraft.model_json_schema(),
            model=self.model,
            effort=self.effort,
            inventory_digest=self.inventory_digest,
            cwd=str(Path(self.cwd).resolve()),
        )
        def validate_review(value: dict[str, Any]) -> ReviewDraft:
            draft = ReviewDraft.model_validate(value)
            submission = _review_submission(
                role="skeleton_reviewer",
                artifact_digest=digest,
                evidence_catalog=evidence_catalog,
                draft=draft,
            )
            validate_reviewer_submission_evidence(
                submission,
                evidence_catalog=evidence_catalog,
                known_task_refs={item.task_ref for item in candidate.tasks},
            )
            return draft

        result = self.runner.run(request, validator=validate_review)
        self.receipts.append(result.receipt)
        return _review_submission(
            role="skeleton_reviewer",
            artifact_digest=digest,
            evidence_catalog=evidence_catalog,
            draft=ReviewDraft.model_validate(result.payload),
        )


@dataclass
class PlanExpanderAdapter:
    runner: StructuredRolePort
    assigner: TaskAssigner
    model: str
    effort: str
    inventory_digest: str
    cwd: Path | str
    planning_budget: PlanningBudgetPolicy = PlanningBudgetPolicy()
    commit_horizon: CommitHorizon = CommitHorizon()
    receipts: list[RoleCallReceipt] = field(default_factory=list)

    def expand(
        self,
        *,
        candidate,
        goal,
        state,
        project_map,
        planning_budget: PlanningBudgetPolicy | None = None,
    ) -> PlanContractRevision:
        applied_budget = planning_budget or self.planning_budget
        request = RoleCallRequest(
            role="plan_expander",
            instructions=(
                "선택된 Skeleton 하나만 Task 계약으로 상세화한다. 목표·Task 목적·dependency·"
                "produces/consumes 의미는 바꾸지 않는다. 파일·symbol·실행 명령·Context Pack은 "
                "ready-time 상세이므로 넣지 않는다. 완료조건·validation·recovery만 구체화한다."
                "독립 Goal Test는 integration_validations에 넣고 Task validation과 분리한다. "
                "Goal의 Task별 필수 검증은 적용 대상 Task의 validations에 보존한다. "
                "각 validation.statement는 해당 Task에 적용되는 Goal의 검사 대상·종류·실행 목적을 "
                "구체적으로 보존한다. required_evidence_kinds의 test는 evidence 종류일 뿐 검사 절차가 "
                "아니다. Goal이 각 Task에 기존 unittest 실행을 요구하면 각 Task의 검사 문장에도 기존 "
                "unittest를 실제 실행해 통과를 확인한다고 명시한다. 이를 일반 동작 확인·test evidence "
                "종류나 후속 Task의 unittest 실행으로 대체하지 않는다. 명시된 검사 대상과 종류를 "
                "보존하되 실제 실행 명령은 ready-time 명세로 남긴다. "
                "payload의 goal_validation_requirement_rows는 AC 원문과 전역 constraint 원문을 구분한 "
                "비권위 색인이다. 전역 constraint가 요구한 Task 검사 존재를 먼저 보존하고, 각 AC의 "
                "statement·validation_intent가 명시한 절차의 필수 ID를 goal_coverage.validation_ids에 연결한다. "
                "선택적 연결은 허용하되 필수 연결로 확대하지 않는다. "
                "검사 statement를 작성한 뒤 각 AC가 명시한 검사 절차에 해당하는 ID를 다시 확인한다. "
                "예를 들어 한 AC가 Task phase와 Goal phase를 구분해 요구하면 두 phase의 검사 ID를 "
                "연결한다. 단순한 선후조건만으로 모든 검사 ID를 모든 AC에 연결하지 않는다. "
                "기여 Task 집합인 task_refs를 검사 ID의 소유 Task 제한으로 해석하지 않는다. "
                "Skeleton의 detail_requirements도 같은 적용 범위로 해석하며, 모든 Task 완료 후 "
                "Goal Test 책임만 별도 integration validation에 반영한다. "
                "Skeleton에 책임 충돌이 남아 있어도 Task를 몰래 삭제·재정의해 우회하지 않는다. "
                "required_evidence_kinds는 schema의 enum만 사용한다. 구체적인 검사 목적은 statement에 쓴다. "
                "deterministic 검사는 file·diff·command·test·build, semantic 검사는 model_review, "
                "manual은 user_decision, external_observation은 external_observation 증거를 사용한다. "
                "검증용 프로세스 밖의 함수 반환값·프로젝트 파일 수정은 external 효과가 아니다. "
                "external=true는 외부 시스템·계정·제3자에 대한 효과이며 Goal의 허용 외부 효과에 결속해야 한다. "
                "expected_effects와 prohibited_effects 모두 로컬 파일 mutation과 외부 시스템 효과를 "
                "각각 별도 항목으로 작성하고 external 값을 구분한다. 한 항목에 두 범위를 섞지 않는다. "
                "expected_effects에는 실제 발생시키는 효과만 넣는다. '파일을 변경하지 않는다', "
                "'외부 효과가 없다'는 미발생 조건은 prohibited_effects나 완료 조건에만 둔다. "
                "Core의 이후 Plan digest 활성화를 별도 승인 Task나 현재 필요한 승인 입력으로 발명하지 않는다."
                "Worker 응답 보고는 Core가 external_observation evidence로 수집한다. 응답 내용의 "
                "의미 검사는 원본 file 근거와 응답 관측을 함께 참조하는 semantic validation으로 "
                "계약하고 required_evidence_kinds에 model_review·external_observation·file을 모두 "
                "요구한다. 이는 외부 시스템 변경 효과를 뜻하지 않는다."
            ) + PLANNING_PROJECT_PATH_INSTRUCTIONS + PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS + PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS + READ_ONLY_REPORTING_INSTRUCTIONS + PLAN_VALIDATION_TRACE_INSTRUCTIONS + PLAN_TASK_RESULT_BOUNDARY_INSTRUCTIONS + PLAN_INSPECTION_INSTRUCTIONS,
            payload={
                "case_ref": _case_ref(sha256_digest(candidate)),
                "goal": goal.definition.model_dump(mode="json"),
                "state": state.model_dump(mode="json"),
                "project_map": compact_project_map(project_map),
                "skeleton": candidate.model_dump(mode="json"),
                "goal_validation_requirement_rows": goal_validation_requirement_rows(goal),
                "inspection_source_catalog": inspection_source_catalog(project_map, {
                    "source:goal": "payload.goal",
                    "artifact:plan_draft": "output.plan",
                }),
            },
            output_schema=PlanExpansionEnvelope.model_json_schema(),
            model=self.model,
            effort=self.effort,
            inventory_digest=self.inventory_digest,
            cwd=str(Path(self.cwd).resolve()),
        )
        def validate_expansion(value: dict[str, Any]) -> PlanExpansionEnvelope:
            envelope = PlanExpansionEnvelope.model_validate(value)
            draft = envelope.plan
            self._compile(
                draft,
                candidate=candidate,
                goal=goal,
                state=state,
                project_map=project_map,
                planning_budget=applied_budget,
            )
            validate_plan_inspection(
                envelope.inspection,
                plan=draft,
                goal=goal,
                project_map=project_map,
                evidence_catalog={
                    "source:goal": goal.definition.model_dump(mode="json"),
                    "source:state": state.model_dump(mode="json"),
                    "source:project_map": compact_project_map(project_map),
                    "artifact:skeleton": candidate.model_dump(mode="json"),
                },
                findings=(),
            )
            return envelope

        result = self.runner.run(request, validator=validate_expansion)
        self.receipts.append(result.receipt)
        draft = validate_expansion(result.payload).plan
        return self._compile(
            draft,
            candidate=candidate,
            goal=goal,
            state=state,
            project_map=project_map,
            planning_budget=applied_budget,
        )

    def _compile(
        self,
        draft,
        *,
        candidate,
        goal,
        state,
        project_map,
        planning_budget,
    ) -> PlanContractRevision:
        skeleton_by_ref = {item.task_ref: item for item in candidate.tasks}
        draft_by_ref = {item.task_ref: item for item in draft.tasks}
        if set(skeleton_by_ref) != set(draft_by_ref):
            raise PlannerRoleAdapterError("Plan expansion이 Skeleton Task 집합을 바꿨습니다.")
        for ref, detailed in draft_by_ref.items():
            source = skeleton_by_ref[ref]
            semantic_fields = (
                detailed.kind == source.kind,
                detailed.objective == source.objective,
                set(detailed.goal_criterion_refs) == set(source.contributes_to),
                set(detailed.produces) == set(source.produces),
                set(detailed.consumes) == set(source.consumes),
            )
            if not all(semantic_fields):
                raise PlannerRoleAdapterError(f"Plan expansion이 {ref}의 Skeleton 의미를 바꿨습니다.")
        skeleton_edges = {
            (
                item.producer_task_ref,
                item.consumer_task_ref,
                item.dependency_type,
                tuple(sorted(item.produces)),
            )
            for item in candidate.dependencies
        }
        draft_edges = {
            (
                item.producer_task_ref,
                item.consumer_task_ref,
                item.dependency_type,
                tuple(sorted(item.products)),
            )
            for item in draft.dependencies
        }
        if skeleton_edges != draft_edges:
            raise PlannerRoleAdapterError("Plan expansion이 Skeleton dependency 의미를 바꿨습니다.")
        skeleton_coverage = {
            item.criterion_id: set(item.task_refs) for item in candidate.goal_coverage
        }
        draft_coverage = {
            item.criterion_id: set(item.task_refs) for item in draft.goal_coverage
        }
        if skeleton_coverage != draft_coverage:
            raise PlannerRoleAdapterError("Plan expansion이 Skeleton Goal coverage를 바꿨습니다.")
        task_ids = {ref: new_id("task") for ref in draft_by_ref}
        tasks = tuple(
            TaskContract(
                task_id=task_ids[item.task_ref],
                task_ref=item.task_ref,
                project_id=goal.definition.project_id,
                kind=item.kind,
                objective=item.objective,
                goal_criterion_refs=item.goal_criterion_refs,
                produces=item.produces,
                consumes=item.consumes,
                preconditions=item.preconditions,
                expected_effects=item.expected_effects,
                prohibited_effects=item.prohibited_effects,
                required_capabilities=item.required_capabilities,
                acceptance_criteria=item.acceptance_criteria,
                validations=item.validations,
                risk_level=item.risk_level,
                risk_tags=item.risk_tags,
                approval_class=item.approval_class,
                recovery=item.recovery,
                assignment=self.assigner.assign(item),
            )
            for item in draft.tasks
        )
        dependencies = tuple(
            PlanDependency(
                producer_task_id=task_ids[item.producer_task_ref],
                consumer_task_id=task_ids[item.consumer_task_ref],
                dependency_type=item.dependency_type,
                products=item.products,
            )
            for item in draft.dependencies
        )
        coverage = tuple(
            PlanGoalCoverage(
                criterion_id=item.criterion_id,
                task_ids=tuple(task_ids[ref] for ref in item.task_refs),
                validation_ids=item.validation_ids,
            )
            for item in draft.goal_coverage
        )
        definition = PlanContractDefinition(
            project_id=goal.definition.project_id,
            goal_contract_digest=goal.definition_digest,
            base_state_snapshot_digest=state.snapshot_digest,
            project_map_digest=project_map.revision_digest,
            source_skeleton_digest=sha256_digest(candidate),
            tasks=tasks,
            dependencies=dependencies,
            goal_coverage=coverage,
            integration_validations=draft.integration_validations,
            commit_horizon=self.commit_horizon,
            planning_budget=planning_budget,
            model_inventory_digest=self.inventory_digest,
            expected_effects=draft.expected_effects,
            prohibited_effects=draft.prohibited_effects,
        )
        return PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=new_id("plan"),
            revision_no=1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            created_at=utc_now(),
        )


@dataclass
class PlanReviewerAdapter:
    runner: StructuredRolePort
    model: str
    effort: str
    inventory_digest: str
    cwd: Path | str
    critical_model: str | None = None
    critical_effort: str | None = None
    receipts: list[RoleCallReceipt] = field(default_factory=list)

    def review(self, *, plan, goal, state, project_map, risk_route) -> ReviewerSubmission:
        digest = plan.activation_digest
        evidence_catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        use_critical = risk_route != "compact_plan_reviewer"
        selected_model = (self.critical_model or self.model) if use_critical else self.model
        selected_effort = (
            (self.critical_effort or self.effort) if use_critical else self.effort
        )
        request = RoleCallRequest(
            role=risk_route,
            instructions=(
                "반드시 review.findings와 review.ratings 두 key를 함께 제출한다. 허용 조합은 정확히 둘이다: "
                "(1) findings가 빈 배열이면 ratings는 goal_fit·grounding·engineering·verification·execution_safety "
                "다섯 정수(각 0~4)를 모두 가진 객체, (2) finding이 하나 이상이면 ratings는 null이다. "
                "rating 생략은 key를 빼는 뜻이 아니라 ratings:null 제출이다. 예: 정상은 "
                "{\"findings\":[],\"ratings\":{\"goal_fit\":4,\"grounding\":4,\"engineering\":4,\"verification\":4,\"execution_safety\":4}}, "
                "결함은 {\"findings\":[{\"finding_code\":\"EXAMPLE_FINDING\",\"...\":\"직접 근거 finding\"}],\"ratings\":null}이다. "
                "이 rating은 Reviewer의 비권위 관찰이며 Core만 admission과 0~100 종합 score를 결정한다. "
                "status·admissible·score·fitness score·weakest task는 선언하지 않는다. "
                "PlanContract의 goal, intent, engineering, verification, execution gate를 독립 검토한다. "
                "직접 evidence가 있는 최소 finding만 제출한다. "
                "현재는 활성화 전 Plan 후보이므로 미래 activation receipt는 아직 없는 것이 정상이다. "
                "파일·명령의 운영 상세는 ExecutionSpec에 확정한다. read_only는 산출물 mutation 정책이며 "
                "읽기 검사와 계획 생성 자체를 금지하지 않는다. 외부 효과는 외부 시스템·계정·제3자에 대한 효과다."
                "affected_task_refs는 Task.task_ref를 참조하며 Core의 task_id와 혼동하지 않는다."
                "goal_validation_requirement_rows는 AC 원문과 전역 constraint 원문을 구분한 비권위 "
                "색인이다. 전역 constraint의 Task 검사 존재와 AC별 명시 절차의 validation_ids 연결을 "
                "각각 검토한다. 색인에서 필수 연결 ID·phase 능력·runtime evidence 누락을 추정하지 않는다. "
                "validation_scope_rows는 원본 Plan의 모든 Task·integration 검사를 펼친 비권위 색인이다. "
                "각 행의 statement를 등록 자료의 실제 수단·phase와 대조하고 마지막 integration 행까지 "
                "확인한다. linked_criterion_ids는 현재 연결 사실이며 필수 연결의 판정이 아니다. "
                "색인 자체를 새 evidence ref나 별도 권위로 사용하지 않고 finding은 원본 evidence_catalog에 결속한다."
            ) + PLANNING_PROJECT_PATH_INSTRUCTIONS + PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS + PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS + PLAN_VALIDATION_TRACE_INSTRUCTIONS + PLAN_TASK_RESULT_BOUNDARY_INSTRUCTIONS + PLAN_INSPECTION_INSTRUCTIONS,
            payload={
                "case_ref": _case_ref(digest),
                "evidence_catalog": evidence_catalog,
                "validation_scope_rows": plan_validation_scope_rows(plan),
                "goal_validation_requirement_rows": goal_validation_requirement_rows(goal),
                "validation_comparison_targets": validation_comparison_targets(goal, plan),
                "inspection_source_catalog": inspection_source_catalog(project_map, {
                    key: f"payload.evidence_catalog.{key}" for key in evidence_catalog
                }),
            },
            output_schema=PlanReviewEnvelope.model_json_schema(),
            model=selected_model,
            effort=selected_effort,
            inventory_digest=self.inventory_digest,
            cwd=str(Path(self.cwd).resolve()),
        )
        def validate_review(value: dict[str, Any]) -> PlanReviewEnvelope:
            envelope = PlanReviewEnvelope.model_validate(value)
            draft = envelope.review
            submission = _review_submission(
                role=risk_route,
                artifact_digest=digest,
                evidence_catalog=evidence_catalog,
                draft=draft,
            )
            validate_reviewer_submission_evidence(
                submission,
                evidence_catalog=evidence_catalog,
                known_task_refs={item.task_ref for item in plan.definition.tasks},
            )
            validate_plan_inspection(
                envelope.inspection,
                plan=plan,
                goal=goal,
                project_map=project_map,
                evidence_catalog=evidence_catalog,
                findings=draft.findings,
            )
            return envelope

        result = self.runner.run(request, validator=validate_review)
        self.receipts.append(result.receipt)
        return _review_submission(
            role=risk_route,
            artifact_digest=digest,
            evidence_catalog=evidence_catalog,
            draft=validate_review(result.payload).review,
        )
