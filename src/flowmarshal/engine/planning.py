from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .domain import (
    CandidateDecision,
    CandidateStatus,
    EngineModel,
    FindingSeverity,
    GateName,
    GoalContractRevision,
    MissionClass,
    MutationPolicy,
    PlanContractRevision,
    PlanSkeletonCandidate,
    PlanningBudgetPolicy,
    ProjectMapRevision,
    ReviewFinding,
    ReviewerSubmission,
    RevisionStatus,
    StateSnapshot,
    TaskKind,
    derive_candidate_decision,
    validate_reviewer_submission_evidence,
)
from .planning_feedback import (
    PlanRefinementAttempt,
    PlanRefinementProposal,
    PlanRefinementStop,
    plan_semantic_digest,
    skeleton_semantic_digest,
    validate_plan_revision,
)


class PlanningError(RuntimeError):
    pass


def compact_project_map(project_map: ProjectMapRevision) -> dict[str, Any]:
    return {
        "revision_digest": project_map.revision_digest,
        "root": project_map.root,
        "instruction_source_refs": project_map.instruction_source_refs,
        "entries": [
            {
                "entry_id": item.entry_id,
                "kind": item.kind.value,
                "path": item.path,
                "content_digest": item.content_digest,
                "symbols": item.symbols,
                "tags": item.tags,
            }
            for item in project_map.entries
        ],
    }


def skeleton_input_catalog(state: StateSnapshot, project_map: ProjectMapRevision) -> dict[str, Any]:
    """Task 산출물이 아닌 consumes의 정확한 허용 selector를 제공한다."""

    return {
        "input:request": "Goal 원문 요청",
        "project:root": project_map.root,
        "project:map": project_map.semantic_digest,
        **{f"input:{entry.path}": entry.content_digest for entry in project_map.entries},
        **{f"project:{entry.entry_id}": entry.path for entry in project_map.entries},
        **{f"state:{fact.fact_id}": fact.predicate for fact in state.facts},
    }


def skeleton_review_evidence_catalog(
    candidate: PlanSkeletonCandidate,
    goal: GoalContractRevision,
    state: StateSnapshot,
    project_map: ProjectMapRevision,
) -> dict[str, Any]:
    return {
        "artifact:skeleton": candidate.model_dump(mode="json"),
        "source:goal": goal.definition.model_dump(mode="json"),
        "source:state": state.model_dump(mode="json"),
        "source:project_map": compact_project_map(project_map),
    }


def plan_review_evidence_catalog(
    plan: PlanContractRevision,
    goal: GoalContractRevision,
    state: StateSnapshot,
    project_map: ProjectMapRevision,
) -> dict[str, Any]:
    return {
        "artifact:plan_contract": plan.model_dump(mode="json"),
        "source:goal": goal.definition.model_dump(mode="json"),
        "source:state": state.model_dump(mode="json"),
        "source:project_map": compact_project_map(project_map),
    }


def goal_validation_requirement_rows(goal: GoalContractRevision) -> list[dict[str, Any]]:
    """AC별 검사 요구와 전역 검사 제약을 구분해 원문 순서대로 투영한다.

    이 색인은 작성·검토 역할이 원본 Goal을 탐색할 수 있게 돕는 비권위 입력이다.
    필수 검사 ID, 적용 Task, 수단의 phase나 evidence catalog 범위를 추정하지 않는다.
    """

    rows: list[dict[str, Any]] = []
    for order, criterion in enumerate(goal.definition.hard_acceptance):
        rows.append({
            "source_kind": "acceptance_criterion",
            "source_id": criterion.criterion_id,
            "source_order": order,
            "selector": f"hard_acceptance[{order}]",
            "statement": criterion.statement,
            "validation_intent": criterion.validation_intent,
            "trace_refs": criterion.trace_refs,
        })
    for order, constraint in enumerate(goal.definition.constraints):
        rows.append({
            "source_kind": "global_constraint",
            "source_id": constraint.constraint_id,
            "source_order": order,
            "selector": f"constraints[{order}]",
            "category": constraint.category,
            "statement": constraint.statement,
            "trace_refs": constraint.trace_refs,
        })
    return rows


def plan_validation_scope_rows(plan: PlanContractRevision) -> list[dict[str, Any]]:
    """검사 원문과 연결을 빠짐없이 펼친 비권위 검토 색인이다."""

    linked: dict[str, list[str]] = {}
    for coverage in plan.definition.goal_coverage:
        for validation_id in coverage.validation_ids:
            linked.setdefault(validation_id, []).append(coverage.criterion_id)
    rows = []
    owners = [
        ("task", task.task_ref, validation)
        for task in plan.definition.tasks
        for validation in task.validations
    ] + [
        ("integration", None, validation)
        for validation in plan.definition.integration_validations
    ]
    for scope, task_ref, validation in owners:
        rows.append({
            "scope": scope,
            "task_ref": task_ref,
            "validation_id": validation.validation_id,
            "statement": validation.statement,
            "method": validation.method,
            "evidence_mode": validation.evidence_mode if scope == "integration" else None,
            "required_evidence_kinds": validation.required_evidence_kinds,
            "linked_criterion_ids": tuple(linked.get(validation.validation_id, ())),
            "declared_criterion_ids": validation.criterion_refs if scope == "integration" else None,
        })
    return rows


def validation_comparison_targets(goal: GoalContractRevision, plan: Any) -> dict[str, Any]:
    """권위 Plan 또는 작성 draft의 전체 대조 집합을 원문 ID·JSON pointer로만 투영한다."""
    definition = getattr(plan, "definition", plan)
    prefix = "/definition" if hasattr(plan, "definition") else ""
    validations = [
        {"validation_id": validation.validation_id, "task_ref": task.task_ref,
         "selector": f"{prefix}/tasks/{ti}/validations/{vi}"}
        for ti, task in enumerate(definition.tasks)
        for vi, validation in enumerate(task.validations)
    ] + [
        {"validation_id": validation.validation_id, "task_ref": None,
         "selector": f"{prefix}/integration_validations/{vi}"}
        for vi, validation in enumerate(definition.integration_validations)
    ]
    return {
        "validations": validations,
        "ac_validation_pairs": [
            {"criterion_id": criterion.criterion_id, "validation_id": row["validation_id"]}
            for criterion in goal.definition.hard_acceptance for row in validations
        ],
        "constraint_task_pairs": [
            {"constraint_id": constraint.constraint_id, "task_ref": task.task_ref}
            for constraint in goal.definition.constraints for task in definition.tasks
        ],
    }


class SkeletonGenerator(Protocol):
    def generate(
        self,
        *,
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
        candidate_count: int,
    ) -> tuple[PlanSkeletonCandidate, ...]: ...

    def refine(
        self,
        *,
        candidate: PlanSkeletonCandidate,
        findings: tuple[ReviewFinding, ...],
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
    ) -> PlanSkeletonCandidate: ...


class SkeletonReviewer(Protocol):
    def review(
        self,
        *,
        candidate: PlanSkeletonCandidate,
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
    ) -> ReviewerSubmission: ...


class PlanExpander(Protocol):
    def expand(
        self,
        *,
        candidate: PlanSkeletonCandidate,
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
        planning_budget: PlanningBudgetPolicy | None = None,
        previous_plan: PlanContractRevision | None = None,
    ) -> PlanContractRevision: ...

    def refine(
        self, *, evaluation: ExpandedPlanEvaluation, candidate: PlanSkeletonCandidate,
        goal: GoalContractRevision, state: StateSnapshot, project_map: ProjectMapRevision,
        planning_budget: PlanningBudgetPolicy, allow_skeleton_revision: bool,
    ) -> PlanRefinementProposal: ...


class PlanReviewer(Protocol):
    def review(
        self,
        *,
        plan: PlanContractRevision,
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
        risk_route: str,
    ) -> ReviewerSubmission: ...


class CandidateEvaluation(EngineModel):
    candidate: PlanSkeletonCandidate
    deterministic_findings: tuple[ReviewFinding, ...] = ()
    semantic_submission: ReviewerSubmission | None = None
    decision: CandidateDecision

    @model_validator(mode="after")
    def evaluation_is_bound(self) -> "CandidateEvaluation":
        digest = sha256_digest(self.candidate)
        if self.decision.candidate_digest != digest:
            raise ValueError("Candidate decision이 다른 skeleton에 결속됐습니다.")
        if self.semantic_submission is not None and self.semantic_submission.candidate_digest != digest:
            raise ValueError("Reviewer submission이 다른 skeleton에 결속됐습니다.")
        if self.semantic_submission is None:
            if self.deterministic_findings:
                expected = derive_candidate_decision(
                    candidate_digest=digest,
                    findings=self.deterministic_findings,
                    ratings=None,
                )
            else:
                expected = CandidateDecision(
                    candidate_digest=digest,
                    status=CandidateStatus.REJECTED,
                    finding_codes=("MISSING_SEMANTIC_REVIEW",),
                )
        else:
            findings = self.deterministic_findings + self.semantic_submission.findings
            expected = derive_candidate_decision(
                candidate_digest=digest,
                findings=findings,
                ratings=self.semantic_submission.ratings if not findings else None,
            )
        if self.decision != expected:
            raise ValueError("Skeleton decision이 Core 결정 규칙과 다릅니다.")
        return self


class ExpandedPlanEvaluation(EngineModel):
    plan: PlanContractRevision
    deterministic_findings: tuple[ReviewFinding, ...] = ()
    semantic_submissions: tuple[ReviewerSubmission, ...] = ()
    decision: CandidateDecision

    @model_validator(mode="after")
    def evaluation_is_core_derived(self) -> "ExpandedPlanEvaluation":
        digest = self.plan.activation_digest
        if self.decision.candidate_digest != digest:
            raise ValueError("Plan decision이 다른 PlanContract에 결속됐습니다.")
        if len(self.semantic_submissions) > 1:
            raise ValueError("현재 Plan risk route에는 reviewer 하나만 허용됩니다.")
        if any(item.candidate_digest != digest for item in self.semantic_submissions):
            raise ValueError("Plan reviewer submission이 다른 PlanContract에 결속됐습니다.")
        findings = self.deterministic_findings + tuple(
            finding
            for submission in self.semantic_submissions
            for finding in submission.findings
        )
        ratings = self.semantic_submissions[0].ratings if self.semantic_submissions else None
        expected = derive_candidate_decision(
            candidate_digest=digest,
            findings=findings,
            ratings=ratings if not findings else None,
        )
        if self.decision != expected:
            raise ValueError("Plan decision이 Core 결정 규칙과 다릅니다.")
        return self


class PlanningSearchOutcome(EngineModel):
    goal_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state_snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    budget_policy: PlanningBudgetPolicy = Field(default_factory=PlanningBudgetPolicy)
    skeleton_evaluations: tuple[CandidateEvaluation, ...]
    shortlist_digests: tuple[str, ...]
    plan_evaluations: tuple[ExpandedPlanEvaluation, ...]
    plan_refinements: tuple[PlanRefinementAttempt, ...] = ()
    plan_refinement_stops: tuple[PlanRefinementStop, ...] = ()
    selected_activation_digest: str | None = None
    logical_role_calls: int = Field(ge=0)
    candidate_versions: int = Field(ge=0)
    budget_exhausted: bool = False

    @model_validator(mode="after")
    def outcome_is_bounded_and_selected_by_core(self) -> "PlanningSearchOutcome":
        policy = self.budget_policy
        skeleton_digests = tuple(
            sha256_digest(item.candidate) for item in self.skeleton_evaluations
        )
        if len(skeleton_digests) != len(set(skeleton_digests)):
            raise ValueError("Planning outcome에 같은 Skeleton version이 중복됐습니다.")
        extra_versions = sum(
            item.proposal.plan is not None or (
                item.proposal.skeleton is not None
                and sha256_digest(item.proposal.skeleton) not in skeleton_digests
            ) for item in self.plan_refinements
        )
        if self.candidate_versions != len(self.skeleton_evaluations) + extra_versions:
            raise ValueError("candidate_versions가 실제 Skeleton·상세 수정 version 수와 다릅니다.")
        if self.candidate_versions > policy.max_candidate_versions:
            raise ValueError("candidate version budget을 초과했습니다.")

        candidates_by_id = {
            item.candidate.candidate_id: item for item in self.skeleton_evaluations
        }
        if len(candidates_by_id) != len(self.skeleton_evaluations):
            raise ValueError("Planning outcome에 candidate ID가 중복됐습니다.")
        initial = [
            item for item in self.skeleton_evaluations if item.candidate.parent_candidate_id is None
        ]
        if not initial or len(initial) > policy.max_initial_candidates:
            raise ValueError("초기 Skeleton 후보 수가 planning budget과 다릅니다.")
        refinement_counts: dict[str, int] = {}
        roots: dict[str, str] = {}
        seen_candidate_ids: set[str] = set()
        for item in self.skeleton_evaluations:
            parent_id = item.candidate.parent_candidate_id
            if parent_id is not None:
                if parent_id not in seen_candidate_ids:
                    raise ValueError(
                        "정제 Skeleton의 parent candidate가 자신보다 먼저 기록되지 않았습니다."
                    )
                root = roots[parent_id]
                roots[item.candidate.candidate_id] = root
                refinement_counts[root] = refinement_counts.get(root, 0) + 1
            else:
                roots[item.candidate.candidate_id] = item.candidate.candidate_id
            seen_candidate_ids.add(item.candidate.candidate_id)
        plans_by_digest = {item.plan.activation_digest: item for item in self.plan_evaluations}
        skeletons_by_digest = {sha256_digest(item.candidate): item for item in self.skeleton_evaluations}
        refined_plan_digests: set[str] = set()
        refined_skeleton_ids: set[str] = set()
        attempted_plans: set[str] = set()
        for attempt in self.plan_refinements:
            if attempt.source_plan_digest in attempted_plans:
                raise ValueError("같은 Plan 실패에 두 번 수정 응답할 수 없습니다.")
            attempted_plans.add(attempt.source_plan_digest)
            source = plans_by_digest.get(attempt.source_plan_digest)
            if source is None or source.decision.status is not CandidateStatus.NEEDS_REVISION:
                raise ValueError("Plan 수정은 기록된 수정 가능한 실패에 결속해야 합니다.")
            source_skeleton_evaluation = skeletons_by_digest.get(source.plan.definition.source_skeleton_digest)
            if source_skeleton_evaluation is None:
                raise ValueError("수정 원본 Plan의 Skeleton 평가가 없습니다.")
            source_skeleton = source_skeleton_evaluation.candidate
            proposal = attempt.proposal
            if proposal.provenance is not None and proposal.provenance.source_evaluation_digest != sha256_digest(source):
                raise ValueError("Plan 수정의 생성 관측이 다른 원본 평가에 결속됐습니다.")
            known_refs = {"artifact:plan_contract", "artifact:skeleton", "source:goal", "source:state", "source:project_map"}
            if not set(proposal.evidence_refs).issubset(known_refs):
                raise ValueError("Plan 수정 제안에 알 수 없는 evidence가 있습니다.")
            root = roots[source_skeleton.candidate_id]
            skeleton_was_evaluated = (
                proposal.skeleton is not None and sha256_digest(proposal.skeleton) in skeletons_by_digest
            )
            if not skeleton_was_evaluated:
                refinement_counts[root] = refinement_counts.get(root, 0) + 1
            if proposal.plan is not None:
                revised = proposal.plan
                validate_plan_revision(source.plan, revised)
                if revised.definition.source_skeleton_digest != source.plan.definition.source_skeleton_digest:
                    raise ValueError("상세 수정이 원본 Skeleton 결속을 바꿨습니다.")
                unchanged = plan_semantic_digest(revised) == plan_semantic_digest(source.plan)
                if attempt.result != ("unchanged_candidate" if unchanged else "evaluated"):
                    raise ValueError("상세 수정 결과가 실제 의미 변경과 다릅니다.")
                if unchanged:
                    if revised.activation_digest in plans_by_digest:
                        raise ValueError("변경 없는 Plan을 재검토할 수 없습니다.")
                else:
                    result = plans_by_digest.get(revised.activation_digest)
                    if result is None or result.plan != revised:
                        raise ValueError("수정 Plan의 독립 평가 기록이 없습니다.")
                    refined_plan_digests.add(revised.activation_digest)
            elif proposal.skeleton is not None:
                revised = proposal.skeleton
                if (
                    revised.parent_candidate_id != source_skeleton.candidate_id
                    or revised.version != source_skeleton.version + 1
                    or revised.refinement_round != 1
                    or revised.approach.strategy_family != source_skeleton.approach.strategy_family
                    or revised.goal_contract_digest != source_skeleton.goal_contract_digest
                    or revised.state_signature != source_skeleton.state_signature
                ):
                    raise ValueError("상세 실패에서 수정한 Skeleton의 계보·고정 입력이 다릅니다.")
                unchanged = skeleton_semantic_digest(revised) == skeleton_semantic_digest(source_skeleton)
                if attempt.result != ("unchanged_candidate" if unchanged else "evaluated"):
                    raise ValueError("Skeleton 수정 결과가 실제 의미 변경과 다릅니다.")
                if unchanged == skeleton_was_evaluated:
                    raise ValueError("Skeleton 수정의 독립 평가 여부가 실제 변경과 다릅니다.")
                if skeleton_was_evaluated:
                    refined_skeleton_ids.add(revised.candidate_id)
            elif attempt.result != proposal.action:
                raise ValueError("판단 충돌·미해결 제안을 수정 성공으로 기록할 수 없습니다.")
        if any(
            stop.source_plan_digest not in plans_by_digest
            or plans_by_digest[stop.source_plan_digest].decision.status
            is not CandidateStatus.NEEDS_REVISION
            for stop in self.plan_refinement_stops
        ):
            raise ValueError("수정 중단 기록은 수정 가능한 원본 Plan에 결속해야 합니다.")
        stopped_plans = [stop.source_plan_digest for stop in self.plan_refinement_stops]
        if len(stopped_plans) != len(set(stopped_plans)) or attempted_plans.intersection(stopped_plans):
            raise ValueError("같은 Plan에 수정 실행·중단 기록이 중복됐습니다.")
        if any(
            count > policy.max_refinement_per_candidate
            for count in refinement_counts.values()
        ):
            raise ValueError("후보별 refinement budget을 초과했습니다.")

        expected_calls = (
            1
            + sum(1 for item in self.skeleton_evaluations if item.candidate.parent_candidate_id
                  and item.candidate.candidate_id not in refined_skeleton_ids)
            + sum(1 for item in self.skeleton_evaluations if item.semantic_submission)
            + len(self.plan_evaluations)
            - len(refined_plan_digests)
            + len(self.plan_refinements)
            + sum(len(item.semantic_submissions) for item in self.plan_evaluations)
        )
        if self.logical_role_calls != expected_calls:
            raise ValueError("logical role call 수가 outcome 내용과 다릅니다.")
        if self.logical_role_calls > policy.max_logical_role_calls:
            raise ValueError("logical role call budget을 초과했습니다.")
        if self.budget_exhausted != (
            self.logical_role_calls >= policy.max_logical_role_calls
        ):
            raise ValueError("budget_exhausted가 실제 call budget 상태와 다릅니다.")

        if len(self.shortlist_digests) != len(set(self.shortlist_digests)):
            raise ValueError("shortlist digest가 중복됐습니다.")
        if len({roots[skeletons_by_digest[digest].candidate.candidate_id]
                for digest in self.shortlist_digests if digest in skeletons_by_digest}) > policy.max_shortlist:
            raise ValueError("shortlist budget을 초과했습니다.")
        admissible_skeletons = {
            sha256_digest(item.candidate): item
            for item in self.skeleton_evaluations
            if item.decision.status is CandidateStatus.ADMISSIBLE
        }
        if not set(self.shortlist_digests).issubset(admissible_skeletons):
            raise ValueError("shortlist에는 admissible Skeleton만 들어갈 수 있습니다.")
        shortlisted_signatures = [
            (admissible_skeletons[digest].candidate.graph_signature,
             roots[admissible_skeletons[digest].candidate.candidate_id])
            for digest in self.shortlist_digests
        ]
        if any(signature == other_signature and root != other_root
               for signature, root in shortlisted_signatures
               for other_signature, other_root in shortlisted_signatures):
            raise ValueError("동일 graph signature의 Skeleton을 중복 shortlist할 수 없습니다.")

        plan_digests = tuple(item.plan.activation_digest for item in self.plan_evaluations)
        if len(plan_digests) != len(set(plan_digests)):
            raise ValueError("같은 PlanContract evaluation이 중복됐습니다.")
        if any(
            item.plan.definition.source_skeleton_digest not in self.shortlist_digests
            for item in self.plan_evaluations
        ):
            raise ValueError("shortlist에 없는 Skeleton을 PlanContract로 상세화했습니다.")
        if any(
            item.plan.definition.planning_budget != policy
            for item in self.plan_evaluations
        ):
            raise ValueError("PlanContract의 planning budget이 실제 검색 budget과 다릅니다.")
        latest_by_plan_id: dict[str, ExpandedPlanEvaluation] = {}
        for evaluation in self.plan_evaluations:
            plan = evaluation.plan
            previous = latest_by_plan_id.get(plan.plan_id)
            if previous is None:
                if plan.revision_no != 1 or plan.supersedes_plan_revision_id is not None:
                    raise ValueError("최초 Plan revision의 이전 평가가 없습니다.")
            else:
                validate_plan_revision(previous.plan, plan)
                matching = [attempt for attempt in self.plan_refinements
                            if attempt.source_plan_digest == previous.plan.activation_digest
                            and attempt.result == "evaluated"]
                if not any(
                    attempt.proposal.plan == plan
                    or (attempt.proposal.skeleton is not None
                        and sha256_digest(attempt.proposal.skeleton) == plan.definition.source_skeleton_digest)
                    for attempt in matching
                ):
                    raise ValueError("새 Plan revision을 만든 실패 피드백 기록이 없습니다.")
            latest_by_plan_id[plan.plan_id] = evaluation
        feasible = [
            item
            for item in self.plan_evaluations
            if item.decision.status is CandidateStatus.ADMISSIBLE
        ]
        selected = min(
            feasible,
            key=lambda item: (
                -(item.decision.fitness_score or 0),
                item.plan.activation_digest,
            ),
            default=None,
        )
        expected_selected = selected.plan.activation_digest if selected is not None else None
        if self.selected_activation_digest != expected_selected:
            raise ValueError("selected Plan이 Core의 결정적 순위와 다릅니다.")
        return self


def _finding(
    code: str,
    gate: GateName,
    summary: str,
    evidence: str,
    *,
    task_refs: tuple[str, ...] = (),
    remediable: bool = False,
    severity: FindingSeverity = FindingSeverity.ERROR,
) -> ReviewFinding:
    return ReviewFinding(
        finding_code=code,
        gate=gate,
        severity=severity,
        summary=summary,
        evidence_refs=(evidence,),
        affected_task_refs=task_refs,
        remediable=remediable,
    )


def skeleton_gate(
    candidate: PlanSkeletonCandidate,
    *,
    goal: GoalContractRevision,
    state: StateSnapshot,
    project_map: ProjectMapRevision,
) -> tuple[ReviewFinding, ...]:
    findings: list[ReviewFinding] = []
    digest = sha256_digest(candidate)
    if candidate.goal_contract_digest != goal.definition_digest:
        findings.append(
            _finding(
                "GOAL_BINDING_MISMATCH",
                GateName.GOAL,
                "Skeleton이 현재 GoalContract에 결속되지 않았습니다.",
                digest,
            )
        )
    if candidate.state_signature != state.semantic_digest:
        findings.append(
            _finding(
                "STATE_BINDING_MISMATCH",
                GateName.GROUNDING,
                "Skeleton의 State signature가 현재 projection과 다릅니다.",
                digest,
                remediable=True,
            )
        )
    if state.goal_contract_digest != goal.definition_digest:
        findings.append(
            _finding(
                "STATE_GOAL_MISMATCH",
                GateName.GROUNDING,
                "StateSnapshot이 현재 GoalContract용 projection이 아닙니다.",
                state.snapshot_digest,
            )
        )
    stale = tuple(item.fact_id for item in state.facts if item.freshness.value != "current")
    if stale:
        findings.append(
            _finding(
                "STALE_STATE_FACT",
                GateName.GROUNDING,
                "현재 계약에 쓰이는 State fact가 최신 상태가 아닙니다.",
                state.snapshot_digest,
                remediable=True,
            )
        )
    if not project_map.entries:
        findings.append(
            _finding(
                "EMPTY_PROJECT_MAP",
                GateName.GROUNDING,
                "Project Map에 grounding 가능한 entry가 없습니다.",
                project_map.revision_digest,
                remediable=True,
            )
        )

    expected_criteria = {item.criterion_id for item in goal.definition.hard_acceptance}
    coverage = {item.criterion_id for item in candidate.goal_coverage}
    missing_criteria = expected_criteria - coverage
    extra_criteria = coverage - expected_criteria
    if missing_criteria or extra_criteria:
        findings.append(
            _finding(
                "GOAL_COVERAGE_MISMATCH",
                GateName.GOAL,
                f"Hard AC coverage가 정확하지 않습니다: missing={sorted(missing_criteria)}, extra={sorted(extra_criteria)}",
                digest,
                remediable=True,
            )
        )
    known_criteria = expected_criteria
    bad_tasks = tuple(
        item.task_ref
        for item in candidate.tasks
        if not set(item.contributes_to).issubset(known_criteria)
    )
    if bad_tasks:
        findings.append(
            _finding(
                "TASK_GOAL_REFERENCE_INVALID",
                GateName.GOAL,
                "Task가 알 수 없는 Goal criterion을 참조합니다.",
                digest,
                task_refs=bad_tasks,
                remediable=True,
            )
        )

    incoming_products: dict[str, set[str]] = {item.task_ref: set() for item in candidate.tasks}
    external_inputs = set(skeleton_input_catalog(state, project_map))
    invalid_inputs = tuple(
        task.task_ref for task in candidate.tasks
        if any(
            value.startswith(("input:", "state:", "project:")) and value not in external_inputs
            for value in task.consumes
        )
    )
    if invalid_inputs:
        findings.append(_finding(
            "INPUT_REFERENCE_INVALID", GateName.GROUNDING,
            "Task가 관측 catalog에 없는 외부 입력 selector를 참조합니다.", digest,
            task_refs=invalid_inputs, remediable=True,
        ))
    for edge in candidate.dependencies:
        incoming_products[edge.consumer_task_ref].update(edge.products if hasattr(edge, "products") else edge.produces)
    disconnected: list[str] = []
    for task in candidate.tasks:
        dynamic_consumes = {
            item for item in task.consumes if not item.startswith(("input:", "state:", "project:"))
        }
        if dynamic_consumes - incoming_products[task.task_ref]:
            disconnected.append(task.task_ref)
    if disconnected:
        findings.append(
            _finding(
                "DISCONNECTED_CONSUME",
                GateName.PLAN,
                "Task consumes 일부가 dependency producer와 연결되지 않았습니다.",
                digest,
                task_refs=tuple(disconnected),
                remediable=True,
            )
        )

    if (
        goal.definition.effect_policy.mutation_policy is MutationPolicy.READ_ONLY
        and any(item.kind is TaskKind.CHANGE for item in candidate.tasks)
    ):
        mutation_tasks = tuple(
            item.task_ref for item in candidate.tasks if item.kind is TaskKind.CHANGE
        )
        findings.append(
            _finding(
                "READ_ONLY_MUTATION",
                GateName.INTENT,
                "read-only Goal에 변경 Task가 포함됐습니다.",
                digest,
                task_refs=mutation_tasks,
            )
        )
    unknown_ids = {item.unknown_id for item in state.unknowns}
    invalid_unknown_refs = {
        ref for task in candidate.tasks for ref in task.unknown_refs if ref not in unknown_ids
    }
    if invalid_unknown_refs:
        findings.append(
            _finding(
                "UNKNOWN_REFERENCE_INVALID",
                GateName.GROUNDING,
                f"StateSnapshot에 없는 unknown을 참조합니다: {sorted(invalid_unknown_refs)}",
                digest,
                remediable=True,
            )
        )
    return tuple(findings)


def plan_gate(
    plan: PlanContractRevision,
    *,
    source: PlanSkeletonCandidate,
    goal: GoalContractRevision,
    state: StateSnapshot,
    project_map: ProjectMapRevision,
) -> tuple[ReviewFinding, ...]:
    findings: list[ReviewFinding] = []
    definition = plan.definition
    digest = plan.activation_digest
    checks = (
        (
            definition.goal_contract_digest == goal.definition_digest,
            "PLAN_GOAL_BINDING_MISMATCH",
            GateName.GOAL,
            "PlanContract가 현재 GoalContract에 결속되지 않았습니다.",
        ),
        (
            definition.base_state_snapshot_digest == state.snapshot_digest,
            "PLAN_STATE_BINDING_MISMATCH",
            GateName.GROUNDING,
            "PlanContract의 기준 StateSnapshot이 다릅니다.",
        ),
        (
            definition.project_map_digest == project_map.revision_digest,
            "PLAN_MAP_BINDING_MISMATCH",
            GateName.GROUNDING,
            "PlanContract의 Project Map이 다릅니다.",
        ),
        (
            definition.source_skeleton_digest == sha256_digest(source),
            "PLAN_SKELETON_BINDING_MISMATCH",
            GateName.PLAN,
            "상세 Plan이 선택된 Skeleton과 결속되지 않았습니다.",
        ),
        (
            plan.status is RevisionStatus.READY,
            "PLAN_NOT_READY",
            GateName.EXECUTION,
            "활성화 후보 PlanContract가 ready 상태가 아닙니다.",
        ),
    )
    for passed, code, gate, summary in checks:
        if not passed:
            findings.append(_finding(code, gate, summary, digest))

    source_tasks = {item.task_ref: item for item in source.tasks}
    plan_tasks = {item.task_ref: item for item in definition.tasks}
    drifted_task_refs = set(source_tasks) ^ set(plan_tasks)
    for task_ref in set(source_tasks) & set(plan_tasks):
        source_task = source_tasks[task_ref]
        plan_task = plan_tasks[task_ref]
        if (
            plan_task.kind != source_task.kind
            or plan_task.objective != source_task.objective
            or set(plan_task.goal_criterion_refs) != set(source_task.contributes_to)
            or set(plan_task.produces) != set(source_task.produces)
            or set(plan_task.consumes) != set(source_task.consumes)
        ):
            drifted_task_refs.add(task_ref)
    if drifted_task_refs:
        findings.append(
            _finding(
                "PLAN_SKELETON_TASK_DRIFT",
                GateName.PLAN,
                "Plan 상세화가 Skeleton의 Task 집합 또는 의미를 바꿨습니다.",
                digest,
                task_refs=tuple(sorted(drifted_task_refs)),
            )
        )

    if not drifted_task_refs:
        plan_ref_by_id = {item.task_id: item.task_ref for item in definition.tasks}
        source_edges = {
            (
                item.producer_task_ref,
                item.consumer_task_ref,
                item.dependency_type,
                tuple(sorted(item.produces)),
            )
            for item in source.dependencies
        }
        plan_edges = {
            (
                plan_ref_by_id[item.producer_task_id],
                plan_ref_by_id[item.consumer_task_id],
                item.dependency_type,
                tuple(sorted(item.products)),
            )
            for item in definition.dependencies
        }
        if source_edges != plan_edges:
            findings.append(
                _finding(
                    "PLAN_SKELETON_DEPENDENCY_DRIFT",
                    GateName.PLAN,
                    "Plan 상세화가 Skeleton dependency 또는 전달 product를 바꿨습니다.",
                    digest,
                )
            )

        source_coverage = {
            item.criterion_id: set(item.task_refs) for item in source.goal_coverage
        }
        plan_coverage = {
            item.criterion_id: {plan_ref_by_id[task_id] for task_id in item.task_ids}
            for item in definition.goal_coverage
        }
        if source_coverage != plan_coverage:
            findings.append(
                _finding(
                    "PLAN_SKELETON_COVERAGE_DRIFT",
                    GateName.GOAL,
                    "Plan 상세화가 Skeleton의 Goal-to-Task coverage를 바꿨습니다.",
                    digest,
                )
            )
    expected = {item.criterion_id for item in goal.definition.hard_acceptance}
    covered = {item.criterion_id for item in definition.goal_coverage}
    if covered != expected:
        findings.append(
            _finding(
                "PLAN_GOAL_COVERAGE_MISMATCH",
                GateName.GOAL,
                f"PlanContract Hard AC coverage가 정확하지 않습니다: missing={sorted(expected-covered)}, extra={sorted(covered-expected)}",
                digest,
                remediable=True,
            )
        )
    if any(not set(task.goal_criterion_refs).issubset(expected) for task in definition.tasks):
        findings.append(
            _finding(
                "PLAN_TASK_GOAL_REFERENCE_INVALID",
                GateName.GOAL,
                "Plan Task가 현재 Goal에 없는 criterion을 참조합니다.",
                digest,
                remediable=True,
            )
        )
    allowed = set(goal.definition.effect_policy.allowed_external_effects)
    prohibited = set(goal.definition.effect_policy.prohibited_effects)
    unexpected_external = {
        effect.statement
        for task in definition.tasks
        for effect in task.expected_effects
        if effect.external and effect.statement not in allowed
    }
    forbidden = {
        effect.statement
        for task in definition.tasks
        for effect in task.expected_effects
        if effect.statement in prohibited
    }
    if unexpected_external or forbidden:
        findings.append(
            _finding(
                "PLAN_EFFECT_POLICY_VIOLATION",
                GateName.INTENT,
                f"Goal 효과 정책 밖의 외부 효과입니다: unexpected={sorted(unexpected_external)}, prohibited={sorted(forbidden)}",
                digest,
            )
        )
    return tuple(findings)


def risk_route(plan: PlanContractRevision) -> str:
    levels = {task.risk_level.value for task in plan.definition.tasks}
    if "critical" in levels:
        return "critical_effect_reviewer"
    if "high" in levels:
        return "high_risk_reviewer"
    if any(effect.external for task in plan.definition.tasks for effect in task.expected_effects):
        return "external_effect_reviewer"
    return "compact_plan_reviewer"


def _merge_submission(
    *,
    digest: str,
    deterministic: tuple[ReviewFinding, ...],
    submission: ReviewerSubmission | None,
) -> CandidateDecision:
    if submission is None:
        # 결정적 Hard Gate에서 이미 중단한 후보에는 실행하지 않은 semantic review를
        # 별도 결함으로 덧붙이지 않는다. Gate를 통과했는데 review만 없는 경우에만 결함이다.
        findings = deterministic or (
            _finding(
                "MISSING_SEMANTIC_REVIEW",
                GateName.ENGINEERING,
                "독립 semantic review가 없습니다.",
                digest,
                remediable=True,
            ),
        )
        return derive_candidate_decision(
            candidate_digest=digest,
            findings=findings,
            ratings=None,
        )
    findings = list(deterministic)
    if submission.candidate_digest != digest:
        findings.append(
            _finding(
                "REVIEW_BINDING_MISMATCH",
                GateName.SCHEMA,
                "Reviewer 결과가 평가 대상 artifact와 결속되지 않았습니다.",
                digest,
            )
        )
    else:
        findings.extend(submission.findings)
    ratings = submission.ratings if not findings else None
    return derive_candidate_decision(
        candidate_digest=digest,
        findings=tuple(findings),
        ratings=ratings,
    )


@dataclass
class SkeletonFirstPlanner:
    generator: SkeletonGenerator
    skeleton_reviewer: SkeletonReviewer
    expander: PlanExpander
    plan_reviewer: PlanReviewer

    def search(
        self,
        *,
        goal: GoalContractRevision,
        state: StateSnapshot,
        project_map: ProjectMapRevision,
        budget: PlanningBudgetPolicy | None = None,
        candidate_count: int | None = None,
        feasible_observer: Callable[[ExpandedPlanEvaluation], None] | None = None,
    ) -> PlanningSearchOutcome:
        policy = budget or PlanningBudgetPolicy()
        requested = candidate_count or self._candidate_count(goal)
        requested = max(1, min(requested, policy.max_initial_candidates))
        calls = 1
        initial = self.generator.generate(
            goal=goal,
            state=state,
            project_map=project_map,
            candidate_count=requested,
        )
        if not 1 <= len(initial) <= requested:
            raise PlanningError("Skeleton generator가 요청 범위 밖의 후보 수를 반환했습니다.")
        versions = len(initial)
        evaluations: list[CandidateEvaluation] = []

        for candidate in initial:
            deterministic = skeleton_gate(
                candidate,
                goal=goal,
                state=state,
                project_map=project_map,
            )
            submission = None
            if not deterministic and calls < policy.max_logical_role_calls:
                submission = self.skeleton_reviewer.review(
                    candidate=candidate,
                    goal=goal,
                    state=state,
                    project_map=project_map,
                )
                validate_reviewer_submission_evidence(
                    submission,
                    evidence_catalog=skeleton_review_evidence_catalog(
                        candidate, goal, state, project_map
                    ),
                    known_task_refs={item.task_ref for item in candidate.tasks},
                )
                calls += 1
            decision = _merge_submission(
                digest=sha256_digest(candidate),
                deterministic=deterministic,
                submission=submission,
            )
            evaluations.append(
                CandidateEvaluation(
                    candidate=candidate,
                    deterministic_findings=deterministic,
                    semantic_submission=submission,
                    decision=decision,
                )
            )

        # 수정 가능한 초기 실패만 후보별 한 번 정제한다.
        refinement_candidates = [
            item
            for item in evaluations
            if item.decision.status is CandidateStatus.NEEDS_REVISION
        ]
        for evaluation in refinement_candidates:
            if (
                policy.max_refinement_per_candidate == 0
                or versions >= policy.max_candidate_versions
                or calls >= policy.max_logical_role_calls
            ):
                break
            findings = evaluation.deterministic_findings
            if evaluation.semantic_submission is not None:
                findings += evaluation.semantic_submission.findings
            refined = self.generator.refine(
                candidate=evaluation.candidate,
                findings=findings,
                goal=goal,
                state=state,
                project_map=project_map,
            )
            calls += 1
            versions += 1
            deterministic = skeleton_gate(
                refined,
                goal=goal,
                state=state,
                project_map=project_map,
            )
            submission = None
            if not deterministic and calls < policy.max_logical_role_calls:
                submission = self.skeleton_reviewer.review(
                    candidate=refined,
                    goal=goal,
                    state=state,
                    project_map=project_map,
                )
                validate_reviewer_submission_evidence(
                    submission,
                    evidence_catalog=skeleton_review_evidence_catalog(
                        refined, goal, state, project_map
                    ),
                    known_task_refs={item.task_ref for item in refined.tasks},
                )
                calls += 1
            evaluations.append(
                CandidateEvaluation(
                    candidate=refined,
                    deterministic_findings=deterministic,
                    semantic_submission=submission,
                    decision=_merge_submission(
                        digest=sha256_digest(refined),
                        deterministic=deterministic,
                        submission=submission,
                    ),
                )
            )

        admissible = [
            item for item in evaluations if item.decision.status is CandidateStatus.ADMISSIBLE
        ]
        pruned = self._prune(admissible)
        shortlist = sorted(
            pruned,
            key=lambda item: (
                -(item.decision.fitness_score or 0),
                item.candidate.estimated_change_cost,
                item.candidate.estimated_context_tokens,
                sha256_digest(item.candidate),
            ),
        )[: policy.max_shortlist]

        plan_evaluations: list[ExpandedPlanEvaluation] = []
        refinements: list[PlanRefinementAttempt] = []
        refinement_stops: list[PlanRefinementStop] = []
        shortlist_history = [sha256_digest(item.candidate) for item in shortlist]
        candidates_by_id = {item.candidate.candidate_id: item.candidate for item in evaluations}

        def root_id(candidate: PlanSkeletonCandidate) -> str:
            while candidate.parent_candidate_id is not None:
                candidate = candidates_by_id[candidate.parent_candidate_id]
            return candidate.candidate_id

        used_refinements: dict[str, int] = {}
        for evaluation in evaluations:
            if evaluation.candidate.parent_candidate_id is not None:
                root = root_id(evaluation.candidate)
                used_refinements[root] = used_refinements.get(root, 0) + 1

        def evaluate_plan(plan, candidate):
            nonlocal calls
            evaluation, review_calls = self._evaluate_plan(
                plan=plan, candidate=candidate, goal=goal, state=state, project_map=project_map,
                review_allowed=calls < policy.max_logical_role_calls,
            )
            calls += review_calls
            plan_evaluations.append(evaluation)
            if evaluation.decision.status is CandidateStatus.ADMISSIBLE and feasible_observer is not None:
                feasible_observer(evaluation)
            return evaluation

        for item in shortlist:
            if calls >= policy.max_logical_role_calls:
                break
            plan = self.expander.expand(
                candidate=item.candidate,
                goal=goal,
                state=state,
                project_map=project_map,
                planning_budget=policy,
            )
            calls += 1
            evaluation = evaluate_plan(plan, item.candidate)
            if evaluation.decision.status is not CandidateStatus.NEEDS_REVISION:
                continue
            root = root_id(item.candidate)
            stop_reason = None
            if used_refinements.get(root, 0) >= policy.max_refinement_per_candidate:
                stop_reason = "refinement_limit"
            elif versions >= policy.max_candidate_versions:
                stop_reason = "candidate_version_budget"
            elif policy.max_logical_role_calls - calls < 2:
                stop_reason = "insufficient_call_budget"
            elif not callable(getattr(self.expander, "refine", None)):
                stop_reason = "refiner_unavailable"
            if stop_reason is not None:
                refinement_stops.append(PlanRefinementStop(
                    source_plan_digest=plan.activation_digest, reason=stop_reason,
                ))
                continue

            allow_skeleton = policy.max_logical_role_calls - calls >= 4
            proposal = self.expander.refine(
                evaluation=evaluation, candidate=item.candidate, goal=goal, state=state,
                project_map=project_map, planning_budget=policy, allow_skeleton_revision=allow_skeleton,
            )
            calls += 1
            used_refinements[root] = used_refinements.get(root, 0) + 1
            known_refs = set(plan_review_evidence_catalog(plan, goal, state, project_map)) | {"artifact:skeleton"}
            if not set(proposal.evidence_refs).issubset(known_refs):
                raise PlanningError("Plan 수정 제안이 제공되지 않은 evidence를 참조합니다.")
            if proposal.plan is not None:
                validate_plan_revision(plan, proposal.plan)
                if proposal.plan.definition.source_skeleton_digest != sha256_digest(item.candidate):
                    raise PlanningError("상세 Plan 수정이 Skeleton 결속을 바꿨습니다.")
                versions += 1
                unchanged = plan_semantic_digest(plan) == plan_semantic_digest(proposal.plan)
                refinements.append(PlanRefinementAttempt(
                    source_plan_digest=plan.activation_digest, proposal=proposal,
                    result="unchanged_candidate" if unchanged else "evaluated",
                ))
                if not unchanged:
                    evaluate_plan(proposal.plan, item.candidate)
            elif proposal.skeleton is not None:
                refined = proposal.skeleton
                if not allow_skeleton:
                    raise PlanningError("Skeleton 수정·검토·상세화의 전체 호출 예산이 없습니다.")
                if (
                    refined.candidate_id in candidates_by_id
                    or refined.parent_candidate_id != item.candidate.candidate_id
                    or refined.version != item.candidate.version + 1
                    or refined.refinement_round != 1
                    or refined.approach.strategy_family != item.candidate.approach.strategy_family
                    or refined.goal_contract_digest != goal.definition_digest
                    or refined.state_signature != state.semantic_digest
                ):
                    raise PlanningError("상세 Plan 실패에서 수정한 Skeleton의 계보·입력이 다릅니다.")
                versions += 1
                unchanged = skeleton_semantic_digest(refined) == skeleton_semantic_digest(item.candidate)
                refinements.append(PlanRefinementAttempt(
                    source_plan_digest=plan.activation_digest, proposal=proposal,
                    result="unchanged_candidate" if unchanged else "evaluated",
                ))
                if unchanged:
                    continue
                candidates_by_id[refined.candidate_id] = refined
                deterministic = skeleton_gate(refined, goal=goal, state=state, project_map=project_map)
                submission = None
                if not deterministic:
                    submission = self.skeleton_reviewer.review(
                        candidate=refined, goal=goal, state=state, project_map=project_map,
                    )
                    calls += 1
                    validate_reviewer_submission_evidence(
                        submission,
                        evidence_catalog=skeleton_review_evidence_catalog(refined, goal, state, project_map),
                        known_task_refs={task.task_ref for task in refined.tasks},
                    )
                revised_evaluation = CandidateEvaluation(
                    candidate=refined, deterministic_findings=deterministic,
                    semantic_submission=submission,
                    decision=_merge_submission(
                        digest=sha256_digest(refined), deterministic=deterministic, submission=submission,
                    ),
                )
                evaluations.append(revised_evaluation)
                if revised_evaluation.decision.status is not CandidateStatus.ADMISSIBLE:
                    continue
                # 기존 shortlist 한 자리의 후속 후보이며 다른 전략과 중복 상세화하지 않는다.
                if any(
                    other.candidate.graph_signature == refined.graph_signature
                    and root_id(other.candidate) != root
                    and sha256_digest(other.candidate) in shortlist_history
                    for other in evaluations
                ):
                    continue
                shortlist_history.append(sha256_digest(refined))
                revised_plan = self.expander.expand(
                    candidate=refined, goal=goal, state=state, project_map=project_map,
                    planning_budget=policy, previous_plan=plan,
                )
                calls += 1
                validate_plan_revision(plan, revised_plan)
                evaluate_plan(revised_plan, refined)
            else:
                refinements.append(PlanRefinementAttempt(
                    source_plan_digest=plan.activation_digest, proposal=proposal, result=proposal.action,
                ))

        feasible = [
            item
            for item in plan_evaluations
            if item.decision.status is CandidateStatus.ADMISSIBLE
        ]
        selected = min(
            feasible,
            key=lambda item: (
                -(item.decision.fitness_score or 0),
                item.plan.activation_digest,
            ),
            default=None,
        )
        return PlanningSearchOutcome(
            goal_contract_digest=goal.definition_digest,
            state_snapshot_digest=state.snapshot_digest,
            budget_policy=policy,
            skeleton_evaluations=tuple(evaluations),
            shortlist_digests=tuple(shortlist_history),
            plan_evaluations=tuple(plan_evaluations),
            plan_refinements=tuple(refinements),
            plan_refinement_stops=tuple(refinement_stops),
            selected_activation_digest=(
                selected.plan.activation_digest if selected is not None else None
            ),
            logical_role_calls=calls,
            candidate_versions=versions,
            budget_exhausted=calls >= policy.max_logical_role_calls,
        )

    def _evaluate_plan(self, *, plan, candidate, goal, state, project_map, review_allowed):
        deterministic = plan_gate(plan, source=candidate, goal=goal, state=state, project_map=project_map)
        submissions = ()
        if not deterministic and review_allowed:
            submission = self.plan_reviewer.review(
                plan=plan, goal=goal, state=state, project_map=project_map, risk_route=risk_route(plan),
            )
            validate_reviewer_submission_evidence(
                submission, evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
                known_task_refs={task.task_ref for task in plan.definition.tasks},
            )
            submissions = (submission,)
        findings = deterministic + tuple(finding for submission in submissions for finding in submission.findings)
        return ExpandedPlanEvaluation(
            plan=plan, deterministic_findings=deterministic, semantic_submissions=submissions,
            decision=derive_candidate_decision(
                candidate_digest=plan.activation_digest, findings=findings,
                ratings=submissions[0].ratings if submissions and not findings else None,
            ),
        ), len(submissions)

    @staticmethod
    def _candidate_count(goal: GoalContractRevision) -> int:
        # 명확한 단일 변경은 1개, 실제 전략 선택 가능성이 큰 구조 작업만 복수 후보다.
        if goal.definition.mission_class in {
            MissionClass.LEGACY_REFACTOR,
            MissionClass.MIGRATION_MODERNIZATION,
        }:
            return 3
        if len(goal.definition.hard_acceptance) >= 4:
            return 2
        return 1

    @staticmethod
    def _prune(evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
        deduped: dict[str, CandidateEvaluation] = {}
        for item in evaluations:
            signature = item.candidate.graph_signature
            existing = deduped.get(signature)
            if existing is None or (
                item.candidate.estimated_change_cost,
                item.candidate.estimated_context_tokens,
            ) < (
                existing.candidate.estimated_change_cost,
                existing.candidate.estimated_context_tokens,
            ):
                deduped[signature] = item
        values = list(deduped.values())
        kept: list[CandidateEvaluation] = []
        for item in values:
            dominated = any(
                other is not item
                and other.candidate.approach.strategy_family
                == item.candidate.approach.strategy_family
                and other.candidate.estimated_change_cost
                <= item.candidate.estimated_change_cost
                and other.candidate.estimated_context_tokens
                <= item.candidate.estimated_context_tokens
                and (
                    other.candidate.estimated_change_cost
                    < item.candidate.estimated_change_cost
                    or other.candidate.estimated_context_tokens
                    < item.candidate.estimated_context_tokens
                )
                for other in values
            )
            if not dominated:
                kept.append(item)
        return kept
