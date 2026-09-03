from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..core.domain import PlanDraft, RequirementDisposition
from .domain import (
    CandidateReadiness,
    CoverageSummary,
    PlanIssueCode,
    PlanIssueSeverity,
    PlanValidationIssue,
    PlanValidationReport,
    RequestSpec,
)


_GENERIC_OBJECTIVES = frozenset(
    {
        "기능구현",
        "기능을알아서완성",
        "알아서완성",
        "작업수행",
        "dothetask",
        "implementfeature",
    }
)


def _normalized_change_target(value: str) -> str:
    target = value.strip().replace("\\", "/")
    while "//" in target:
        target = target.replace("//", "/")
    if target.startswith("./"):
        target = target[2:]
    return target.casefold()


def _objective_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", value).casefold()


class DeterministicPlanValidator:
    """모델 판단을 사용하지 않고 PlanDraft의 구조적 완전성을 검사한다."""

    def validate(self, request: RequestSpec, draft: PlanDraft) -> PlanValidationReport:
        issues: list[PlanValidationIssue] = []

        def add(
            code: PlanIssueCode,
            message: str,
            *,
            requirement_ids: tuple[str, ...] = (),
            work_item_refs: tuple[str, ...] = (),
            details: dict[str, Any] | None = None,
        ) -> None:
            issues.append(
                PlanValidationIssue(
                    code=code,
                    severity=PlanIssueSeverity.ERROR,
                    message=message,
                    requirement_ids=tuple(sorted(requirement_ids)),
                    work_item_refs=tuple(sorted(work_item_refs)),
                    details=details or {},
                )
            )

        if draft.project_id != request.project_id:
            add(
                PlanIssueCode.PROJECT_MISMATCH,
                "PlanDraft project_id가 RequestSpec과 다릅니다.",
                details={"expected": request.project_id, "observed": draft.project_id},
            )
        if draft.parent_revision_id != request.parent_revision_id:
            add(
                PlanIssueCode.PARENT_REVISION_MISMATCH,
                "PlanDraft parent revision이 RequestSpec과 다릅니다.",
                details={
                    "expected": request.parent_revision_id,
                    "observed": draft.parent_revision_id,
                },
            )
        if draft.request_spec_digest != request.canonical_digest:
            add(
                PlanIssueCode.REQUEST_DIGEST_MISMATCH,
                "PlanDraft가 정확한 RequestSpec digest에 binding되지 않았습니다.",
                details={
                    "expected": request.canonical_digest,
                    "observed": draft.request_spec_digest,
                },
            )

        expected_requirements = {
            item.requirement_id: item for item in request.requirements
        }
        coverage_by_id = {
            item.requirement_id: item for item in draft.requirement_coverage
        }
        missing_requirements = tuple(
            sorted(set(expected_requirements) - set(coverage_by_id))
        )
        unknown_requirements = tuple(
            sorted(set(coverage_by_id) - set(expected_requirements))
        )
        for requirement_id in missing_requirements:
            add(
                PlanIssueCode.REQUIREMENT_COVERAGE_MISSING,
                "RequestSpec 요구사항이 WorkItem·제외·질문 중 어디에도 연결되지 않았습니다.",
                requirement_ids=(requirement_id,),
            )
        for requirement_id in unknown_requirements:
            add(
                PlanIssueCode.REQUIREMENT_COVERAGE_UNKNOWN,
                "PlanDraft가 RequestSpec에 없는 요구사항을 참조합니다.",
                requirement_ids=(requirement_id,),
            )

        known_requirement_ids = set(expected_requirements)
        for clarification in draft.clarifications:
            unknown = tuple(
                sorted(set(clarification.requirement_ids) - known_requirement_ids)
            )
            if unknown:
                add(
                    PlanIssueCode.CLARIFICATION_REQUIREMENT_UNKNOWN,
                    "사용자 확인 질문이 RequestSpec에 없는 요구사항을 참조합니다.",
                    requirement_ids=unknown,
                    details={"clarification_id": clarification.clarification_id},
                )

        work_by_ref = {item.client_ref: item for item in draft.work_items}
        requirements_by_work: dict[str, set[str]] = defaultdict(set)
        work_items_by_requirement: dict[str, set[str]] = defaultdict(set)
        for coverage in draft.requirement_coverage:
            if (
                coverage.disposition is not RequirementDisposition.WORK_ITEMS
                or coverage.requirement_id not in known_requirement_ids
            ):
                continue
            for work_ref in coverage.work_item_refs:
                requirements_by_work[work_ref].add(coverage.requirement_id)
                work_items_by_requirement[coverage.requirement_id].add(work_ref)

        for work_ref in sorted(work_by_ref):
            if not requirements_by_work.get(work_ref):
                add(
                    PlanIssueCode.WORK_ITEM_WITHOUT_REQUIREMENT,
                    "어떤 요구사항에도 기여하지 않는 WorkItem입니다.",
                    work_item_refs=(work_ref,),
                )

        limits = request.planning_limits
        if len(draft.work_items) > limits.max_work_items:
            add(
                PlanIssueCode.PLAN_OVER_FRAGMENTED,
                "PlanDraft의 WorkItem 수가 요청에 지정된 상한을 넘었습니다.",
                work_item_refs=tuple(work_by_ref),
                details={
                    "limit": limits.max_work_items,
                    "observed": len(draft.work_items),
                },
            )
        for requirement_id, work_refs in sorted(work_items_by_requirement.items()):
            if len(work_refs) > limits.max_work_items_per_requirement:
                add(
                    PlanIssueCode.REQUIREMENT_OVER_FRAGMENTED,
                    "하나의 요구사항이 지나치게 많은 WorkItem으로 분할됐습니다.",
                    requirement_ids=(requirement_id,),
                    work_item_refs=tuple(work_refs),
                    details={
                        "limit": limits.max_work_items_per_requirement,
                        "observed": len(work_refs),
                    },
                )

        known_context = {item.source_id for item in request.context_sources}
        required_context = {
            item.source_id
            for item in request.context_sources
            if item.required_for_all_work_items
        }
        capabilities = {
            item.capability_id: item for item in request.available_validations
        }
        for work_ref, work in sorted(work_by_ref.items()):
            mapped_requirements = requirements_by_work.get(work_ref, set())
            if len(mapped_requirements) > limits.max_requirements_per_work_item:
                add(
                    PlanIssueCode.WORK_ITEM_TOO_LARGE,
                    "WorkItem이 한 번에 담당하는 요구사항 수가 상한을 넘었습니다.",
                    requirement_ids=tuple(mapped_requirements),
                    work_item_refs=(work_ref,),
                    details={
                        "dimension": "requirements",
                        "limit": limits.max_requirements_per_work_item,
                        "observed": len(mapped_requirements),
                    },
                )
            if len(work.expected_changes) > limits.max_change_targets_per_work_item:
                add(
                    PlanIssueCode.WORK_ITEM_TOO_LARGE,
                    "WorkItem의 예상 변경 대상 수가 상한을 넘었습니다.",
                    work_item_refs=(work_ref,),
                    details={
                        "dimension": "expected_changes",
                        "limit": limits.max_change_targets_per_work_item,
                        "observed": len(work.expected_changes),
                    },
                )
            objective_key = _objective_key(work.objective)
            if (
                len(objective_key) < limits.min_objective_characters
                or objective_key in _GENERIC_OBJECTIVES
            ):
                add(
                    PlanIssueCode.WORK_ITEM_OBJECTIVE_TOO_VAGUE,
                    "독립적으로 실행·판정하기에는 WorkItem 목표가 지나치게 짧거나 일반적입니다.",
                    work_item_refs=(work_ref,),
                    details={
                        "minimum_characters": limits.min_objective_characters,
                        "observed_characters": len(objective_key),
                    },
                )

            unknown_context = tuple(sorted(set(work.context_sources) - known_context))
            if unknown_context:
                add(
                    PlanIssueCode.CONTEXT_SOURCE_UNKNOWN,
                    "WorkItem이 RequestSpec에 없는 ContextSource를 참조합니다.",
                    work_item_refs=(work_ref,),
                    details={"source_ids": list(unknown_context)},
                )
            missing_context = tuple(sorted(required_context - set(work.context_sources)))
            if missing_context:
                add(
                    PlanIssueCode.REQUIRED_CONTEXT_MISSING,
                    "모든 작업에 필요한 ContextSource가 WorkItem에서 빠졌습니다.",
                    work_item_refs=(work_ref,),
                    details={"source_ids": list(missing_context)},
                )

            if work.assignment is not None:
                add(
                    PlanIssueCode.PREMATURE_ASSIGNMENT,
                    "Planner는 모델·추론 수준을 배정할 수 없습니다.",
                    work_item_refs=(work_ref,),
                )
            for validation in work.validations:
                capability_id = validation.capability_id
                if capability_id is None:
                    add(
                        PlanIssueCode.VALIDATION_CAPABILITY_MISSING,
                        "validation이 등록된 capability에 binding되지 않았습니다.",
                        work_item_refs=(work_ref,),
                        details={"criterion_id": validation.criterion_id},
                    )
                    continue
                capability = capabilities.get(capability_id)
                if capability is None:
                    add(
                        PlanIssueCode.VALIDATION_CAPABILITY_UNKNOWN,
                        "validation이 RequestSpec에 없는 capability를 참조합니다.",
                        work_item_refs=(work_ref,),
                        details={
                            "capability_id": capability_id,
                            "criterion_id": validation.criterion_id,
                        },
                    )
                elif validation.check_type != capability.check_type:
                    add(
                        PlanIssueCode.VALIDATION_TYPE_MISMATCH,
                        "validation check_type이 capability 계약과 다릅니다.",
                        work_item_refs=(work_ref,),
                        details={
                            "capability_id": capability_id,
                            "expected": capability.check_type,
                            "observed": validation.check_type,
                        },
                    )

        dependencies = {
            item.client_ref: set(item.dependencies) for item in draft.work_items
        }

        def ancestors(work_ref: str) -> set[str]:
            seen: set[str] = set()
            pending = list(dependencies[work_ref])
            while pending:
                dependency = pending.pop()
                if dependency in seen:
                    continue
                seen.add(dependency)
                pending.extend(dependencies[dependency])
            return seen

        ancestor_map = {work_ref: ancestors(work_ref) for work_ref in work_by_ref}
        ordered_refs = [item.client_ref for item in draft.work_items]
        for left_index, left_ref in enumerate(ordered_refs):
            left_targets = {
                _normalized_change_target(value)
                for value in work_by_ref[left_ref].expected_changes
            }
            for right_ref in ordered_refs[left_index + 1 :]:
                shared = tuple(
                    sorted(
                        left_targets
                        & {
                            _normalized_change_target(value)
                            for value in work_by_ref[right_ref].expected_changes
                        }
                    )
                )
                if not shared:
                    continue
                if (
                    left_ref in ancestor_map[right_ref]
                    or right_ref in ancestor_map[left_ref]
                ):
                    continue
                add(
                    PlanIssueCode.CHANGE_CONFLICT_UNORDERED,
                    "같은 변경 대상을 다루는 WorkItem 사이에 dependency 순서가 없습니다.",
                    work_item_refs=(left_ref, right_ref),
                    details={"shared_change_targets": list(shared)},
                )

        issues.sort(
            key=lambda item: (
                item.severity.value,
                item.code.value,
                item.requirement_ids,
                item.work_item_refs,
                item.message,
            )
        )
        valid = not any(
            issue.severity is PlanIssueSeverity.ERROR for issue in issues
        )
        requires_user_input = any(
            coverage.requires_user_confirmation
            for coverage in draft.requirement_coverage
        ) or any(clarification.blocking for clarification in draft.clarifications)
        if not valid:
            readiness = CandidateReadiness.INVALID
        elif requires_user_input:
            readiness = CandidateReadiness.NEEDS_USER_INPUT
        else:
            readiness = CandidateReadiness.READY_FOR_ASSIGNMENT

        known_coverage = [
            coverage
            for coverage in draft.requirement_coverage
            if coverage.requirement_id in known_requirement_ids
        ]
        return PlanValidationReport(
            request_spec_digest=request.canonical_digest,
            candidate_digest=draft.canonical_digest,
            valid=valid,
            readiness=readiness,
            coverage=CoverageSummary(
                requirement_count=len(expected_requirements),
                covered_count=sum(
                    item.disposition is RequirementDisposition.WORK_ITEMS
                    for item in known_coverage
                ),
                excluded_count=sum(
                    item.disposition is RequirementDisposition.EXCLUDED
                    for item in known_coverage
                ),
                clarification_count=sum(
                    item.disposition is RequirementDisposition.CLARIFICATION
                    for item in known_coverage
                ),
                missing_count=len(missing_requirements),
                unknown_count=len(unknown_requirements),
                work_item_count=len(draft.work_items),
            ),
            issues=tuple(issues),
        )
