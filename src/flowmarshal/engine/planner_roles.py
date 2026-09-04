from __future__ import annotations

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
    skeleton_input_catalog,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
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
    "Goal이 각 Task 또는 특정 범위 Task의 완료 전에 요구한 검증은 AC 기여 관계와 별개인 "
    "해당 Task 자체의 필수 책임이다. Goal의 적용 범위를 각 Task와 대조하고, 상세 Plan에서는 "
    "그 Task.validations에 검사 목적·method·필수 evidence 종류를 보존한다. 후속 검증 Task나 "
    "integration validation에만 검사를 두거나 acceptance_criteria에 문장만 적어 이를 대체하지 않는다. "
    "Core는 선행 Task 자체의 validation을 통과한 뒤 dependency를 해제하므로, 그 Task의 완료에 "
    "필요한 evidence를 후속 Task에서 받도록 계획하지 않는다. Executor/Validator 모델 배정과 "
    "independence_required는 역할 배정이며 검증 호출·evidence를 대신하지 않는다. 예를 들어 Goal이 "
    "해당 Task에 실제 테스트·파일 범위 검사와 독립 모델 검토를 요구하면 그 Task에 deterministic "
    "command/test·file/diff 검사와 semantic model_review 검사를 각각 명시한다. 이러한 검사 종류를 "
    "Goal의 요구 범위 밖 Task에 일괄 강제하지 않는다. Task별 필수 검증은 detail_requirements나 "
    "contributes_to에 반복되지 않아도 Goal에서 상속하며, 상세 Plan에서 실제 누락된 경우에만 "
    "Goal과 해당 Task.validations를 직접 근거로 지적한다."
    "Skeleton과 Plan 작성 draft의 goal_coverage.task_refs는 기여 Task 집합이다. Compiler는 "
    "draft의 task_refs를 Task.task_id로 변환하여 PlanContractDefinition.goal_coverage.task_ids에 "
    "기록한다. Reviewer가 받는 컴파일된 Plan의 task_ids는 정상 필드다. 실제 Task.task_id에 "
    "대조해 연결을 확인하고 Task.task_ref로 대응시켜 기여 집합을 비교한다. 이를 task_refs로 "
    "바꾸라고 요구하거나 내부 task_id 자체를 잘못된 참조로 판정하지 않는다. Reviewer finding의 "
    "affected_task_refs는 별도로 Task.task_ref를 사용한다. "
    "기여 Task 연결과 validation_ids는 서로 다른 연결이다. draft의 task_refs는 "
    "Skeleton의 기여 Task 집합을 그대로 보존한다. validation_ids의 소유 Task를 그 task_refs로 "
    "제한하지 않는다. Goal이 각 Task에 검증을 요구하면 해당 AC의 validation_ids에는 적용 대상 "
    "모든 Task의 자체 검사 ID를 연결한다. 예를 들어 검증 AC의 task_refs가 후속 검증 Task B뿐이어도 "
    "Goal이 변경 Task A와 B 각각의 검증을 요구하면 A와 B의 검사 ID가 모두 필요하다. 이 연결을 "
    "추가하려고 task_refs나 contributes_to를 바꾸지 않는다. 특정 Task에만 적용되는 요구는 그 "
    "범위를 유지한다. 검토 시 Task 자체의 필수 검사 존재와 해당 AC.validation_ids의 연결을 "
    "각각 확인한다. 검사 계약이 존재해도 AC 연결이 빠졌으면 Goal·해당 Task.validations·"
    "goal_coverage를 직접 근거로 누락을 지적한다."
)


PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS = (
    "상세 Plan의 Task validation과 integration validation마다 검사 목적과 그 목적을 실제로 "
    "수행하는 수단의 범위를 대조한다. Goal이나 후보가 등록 검사 도구·자료의 특정 phase·mode·"
    "절차를 참조하면 Project Map의 등록 경로에서 관련 본문을 읽고, 필요하면 그 본문이 가리킨 "
    "검사 구현의 분기·입력·출력을 확인한다. 경로·도구 이름·test evidence 종류만으로 검사 능력을 "
    "추정하지 않는다. Goal이 요구하는 동작 범위와 특정 검사 수단이 관측하는 범위는 다르다. "
    "같은 도구라도 선택한 phase에 없는 검사 능력을 부여하거나 다른 phase의 검사를 합쳐 "
    "설명하지 않는다. 선언·시그니처 검사나 기존 테스트의 통과를 실제로 실행하지 않는 입력·"
    "호출 방식의 검사로 확대하지 않는다. "
    "계약의 검사 도구·phase 참조는 의미를 식별하기 위해 보존할 수 있으며, 실제 argv·실행 경로 "
    "등 운영 상세는 ready-time 명세에서 확정한다. 이미 명시한 phase와 검사 범위가 충돌하면 "
    "현재 Plan의 의미 결함이다. 나중에 명령을 바꾸면 된다는 이유로 허용하지 않는다. "
    "상세화는 Goal의 Task별 필수 검증을 유지하면서 참조 도구가 지원하는 범위만 명시한다. "
    "그 도구로 부족한 필수 검사는 별도의 실제 검사 책임으로 계약하고, 단순한 도구 설명 수정으로 "
    "Task의 검증 의무를 약화하거나 독립 Goal Test로 넘기지 않는다. Goal이 Task에 요구하지 않은 "
    "검사를 Goal 전체의 동작 요구만으로 모든 Task에 추가하지도 않는다. "
    "Reviewer는 후보 검사 문장·Goal·등록 자료를 직접 대조하고 범위 충돌을 최소 finding으로 "
    "제출한다. 정상 Task 검사와 별도 independent Goal Test가 각각 자기 범위를 보존하면 허용한다. "
    "자료가 불완전해 능력을 확인하지 못한 경우와 자료로 확인된 모순을 구분하고, 없는 검사 "
    "능력이나 근거 없는 누락을 만들어 판단하지 않는다."
)


class PlannerRoleAdapterError(RuntimeError):
    pass


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
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    validations: tuple[ValidationContract, ...] = Field(min_length=1, description="이 Task 완료 전에 필요한 실제 검사 계약. statement에 Goal이 이 Task에 요구한 검사 대상·종류·실행 목적을 보존하고 method·필수 evidence 종류를 함께 명시한다. 등록 도구·phase를 참조하면 제공된 자료의 실제 검사 범위와 일치해야 한다. 부족한 필수 검사는 별도 검사 책임으로 명시하며 다른 phase의 능력을 부여하지 않는다. 기존 unittest 실행 요구를 일반 동작 확인이나 test enum만으로 대체하지 않는다. 후속 Task 검사, 모델 배정, AC 문장이나 integration validation으로 자체 필수 검증을 대체하지 않는다.")
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
    validation_ids: tuple[str, ...] = Field(min_length=1, description="해당 AC를 검사하는 실제 Task 또는 integration validation ID. Goal의 요구가 적용되는 Task는 task_refs에 없어도 자체 필수 검사 ID를 모두 연결한다. task_refs로 검사 소유 Task를 제한하지 않는다. Core의 독립 Goal Test는 integration validation ID로 연결한다.")


class PlanExpansionDraft(EngineModel):
    tasks: tuple[DetailedTaskDraft, ...] = Field(min_length=1)
    dependencies: tuple[PlanDependencyDraft, ...] = ()
    goal_coverage: tuple[PlanGoalCoverageDraft, ...] = Field(min_length=1)
    integration_validations: tuple[IntegrationValidationContract, ...] = Field(min_length=1, description="Task 검사와 분리한 plan-level Goal 검사 계약. 참조한 등록 도구·phase가 statement의 검사 범위를 실제 지원하는지 대조한다. 다른 phase의 검사 능력이나 Task evidence로 independent 검사를 대체하지 않는다.")
    expected_effects: tuple[str, ...] = Field(default=(), description="Plan이 실제 발생시키는 효과. 효과가 없다는 부정형 조건은 포함하지 않는다.")
    prohibited_effects: tuple[str, ...] = Field(default=(), description="Goal이 금지한 효과와 범위를 보존하며 현재 계획 역할의 행동 제한을 새로 추가하지 않는다.")


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
                "각 AC의 적용 범위와 Task별 검사 ID를 대조해 goal_coverage.validation_ids에도 연결한다. "
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
            ) + PLANNING_PROJECT_PATH_INSTRUCTIONS + PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS + PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS + READ_ONLY_REPORTING_INSTRUCTIONS,
            payload={
                "case_ref": _case_ref(sha256_digest(candidate)),
                "goal": goal.definition.model_dump(mode="json"),
                "state": state.model_dump(mode="json"),
                "project_map": compact_project_map(project_map),
                "skeleton": candidate.model_dump(mode="json"),
            },
            output_schema=PlanExpansionDraft.model_json_schema(),
            model=self.model,
            effort=self.effort,
            inventory_digest=self.inventory_digest,
            cwd=str(Path(self.cwd).resolve()),
        )
        def validate_expansion(value: dict[str, Any]) -> PlanExpansionDraft:
            draft = PlanExpansionDraft.model_validate(value)
            self._compile(
                draft,
                candidate=candidate,
                goal=goal,
                state=state,
                project_map=project_map,
                planning_budget=applied_budget,
            )
            return draft

        result = self.runner.run(request, validator=validate_expansion)
        self.receipts.append(result.receipt)
        draft = PlanExpansionDraft.model_validate(result.payload)
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
                "PlanContract의 goal, intent, engineering, verification, execution gate를 독립 검토한다. "
                "직접 evidence가 있는 최소 finding만 제출한다. finding이 있으면 rating을 생략하고, "
                "status·fitness score·weakest task는 선언하지 않는다."
                "현재는 활성화 전 Plan 후보이므로 미래 activation receipt는 아직 없는 것이 정상이다. "
                "파일·명령의 운영 상세는 ExecutionSpec에 확정한다. read_only는 산출물 mutation 정책이며 "
                "읽기 검사와 계획 생성 자체를 금지하지 않는다. 외부 효과는 외부 시스템·계정·제3자에 대한 효과다."
                "affected_task_refs는 Task.task_ref를 참조하며 Core의 task_id와 혼동하지 않는다."
            ) + PLANNING_PROJECT_PATH_INSTRUCTIONS + PLANNING_VALIDATION_BOUNDARY_INSTRUCTIONS + PLANNING_VALIDATION_CAPABILITY_INSTRUCTIONS,
            payload={"case_ref": _case_ref(digest), "evidence_catalog": evidence_catalog},
            output_schema=ReviewDraft.model_json_schema(),
            model=selected_model,
            effort=selected_effort,
            inventory_digest=self.inventory_digest,
            cwd=str(Path(self.cwd).resolve()),
        )
        def validate_review(value: dict[str, Any]) -> ReviewDraft:
            draft = ReviewDraft.model_validate(value)
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
            return draft

        result = self.runner.run(request, validator=validate_review)
        self.receipts.append(result.receipt)
        return _review_submission(
            role=risk_route,
            artifact_digest=digest,
            evidence_catalog=evidence_catalog,
            draft=ReviewDraft.model_validate(result.payload),
        )
