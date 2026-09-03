from __future__ import annotations

import hmac
from collections import defaultdict
from typing import Iterable, Mapping, NamedTuple

from ..core.domain import PlanDraft
from .domain import PlannerContractError
from .r31_domain import (
    R31_SCORE_POLICY_ID,
    R31_SCORE_POLICY_VERSION,
    CandidateEnvelope,
    CandidateStatus,
    ConfidenceLevel,
    GateName,
    MissionPrimary,
    PlanQualityReport,
    PlanVerdict,
    PlanningRunInput,
    PlanningRunReceipt,
    PlanningRunStatus,
    PlanningSearchOutcome,
    RuntimeStatus,
    ScoreDimensionRatings,
    SearchOutcomeStatus,
    SelectionReceipt,
    SelectionSource,
)


BALANCED_MVP_WEIGHTS: Mapping[str, int] = {
    "goal_fit_change_safety": 25,
    "verification_evidence_strength": 25,
    "execution_risk_control": 20,
    "maintainability_reproducibility": 20,
    "resource_efficiency": 10,
}
BALANCED_WEIGHTS = BALANCED_MVP_WEIGHTS
BALANCED_MVP_POLICY = f"{R31_SCORE_POLICY_ID}-{R31_SCORE_POLICY_VERSION}"

MAX_INITIAL_CANDIDATES = 3
MAX_ANALYSIS_INITIAL_CANDIDATES = 2
MAX_TOP_K = 2
MAX_REFINEMENT_ROUNDS = 1
MAX_CANDIDATE_VERSIONS = 5
CLEAR_WIN_MARGIN = 10


# 값이 클수록 좋은 항목은 1, 작을수록 좋은 항목은 -1이다. Mission 전용
# 항목은 공통 tie-break보다 항상 먼저 비교한다.
MISSION_TIE_BREAK_ORDER: Mapping[MissionPrimary, tuple[tuple[str, int], ...]] = {
    MissionPrimary.NEW_BUILD: (
        ("usable_vertical_slice", 1),
        ("irreversible_foundation_decisions", -1),
    ),
    MissionPrimary.FEATURE_EXTENSION: (
        ("existing_behavior_compatibility", 1),
        ("consumer_compatibility", 1),
    ),
    MissionPrimary.LEGACY_REFACTOR: (
        ("behavior_equivalence", 1),
        ("incrementality", 1),
        ("blast_radius", -1),
    ),
    MissionPrimary.BUGFIX_STABILIZATION: (
        ("causal_evidence", 1),
        ("reproducibility", 1),
        ("causal_change", 1),
    ),
    MissionPrimary.MIGRATION_MODERNIZATION: (
        ("data_preservation", 1),
        ("recoverability", 1),
        ("intermediate_compatibility", 1),
        ("rerunnability", 1),
    ),
    MissionPrimary.ANALYSIS_AUDIT: (
        ("evidence_coverage", 1),
        ("reproducibility", 1),
        ("source_authority", 1),
    ),
}


class PlanningSearchError(PlannerContractError):
    """R3.1 검색 정책이나 immutable receipt 결속 위반."""


class SelectedPlanExport(NamedTuple):
    """Core 활성화 capability가 없는 선택 결과."""

    plan_draft: PlanDraft
    selection_receipt_digest: str

    @property
    def draft(self) -> PlanDraft:
        return self.plan_draft

    @property
    def receipt_reference(self) -> str:
        return self.selection_receipt_digest


def balanced_fitness_score(ratings: ScoreDimensionRatings) -> int:
    """balanced-mvp-v0의 유일한 0~100 정수 점수를 결정적으로 계산한다."""

    numerator = sum(
        BALANCED_MVP_WEIGHTS[field_name] * getattr(ratings, field_name)
        for field_name in BALANCED_MVP_WEIGHTS
    )
    # 양수 정수 입력에서 정확한 half-up 반올림이다. Python round의 bankers
    # rounding에 의존하지 않아 실행 환경과 무관하게 같은 receipt를 만든다.
    score = (numerator + 2) // 4
    if score != ratings.calculated_score:
        raise PlanningSearchError(
            "SCORE_POLICY_MISMATCH",
            "ScoreDimensionRatings와 balanced-mvp-v0 계산 결과가 다릅니다.",
        )
    return score


calculate_fitness_score = balanced_fitness_score


def _assert_scoreable(candidate: CandidateEnvelope) -> PlanQualityReport:
    report = candidate.quality_report
    if report is None:
        raise PlanningSearchError(
            "QUALITY_REPORT_MISSING", "점수화할 후보에 PlanQualityReport가 없습니다."
        )
    if report.planning_input_digest != candidate.planning_input_digest:
        raise PlanningSearchError(
            "QUALITY_INPUT_DIGEST_MISMATCH",
            "후보와 품질 보고서의 planning_input_digest가 다릅니다.",
        )
    if report.plan_digest != candidate.plan.canonical_digest:
        raise PlanningSearchError(
            "QUALITY_PLAN_DIGEST_MISMATCH",
            "품질 보고서가 후보의 정확한 PlanDraft에 결속되지 않았습니다.",
        )
    if report.plan_verdict is not PlanVerdict.PASS:
        raise PlanningSearchError(
            "HARD_GATE_NOT_PASSED",
            "Hard Gate를 통과하지 않은 후보는 점수화할 수 없습니다.",
        )
    findings = {finding.gate: finding for finding in report.gate_findings}
    if set(findings) != set(GateName) or len(findings) != len(report.gate_findings):
        raise PlanningSearchError(
            "HARD_GATE_COVERAGE_INVALID",
            "5개 Hard Gate가 정확히 한 번씩 평가되지 않았습니다.",
        )
    if any(
        finding.plan_verdict in {PlanVerdict.FAIL, PlanVerdict.BLOCKED}
        for finding in report.gate_findings
    ):
        raise PlanningSearchError(
            "HARD_GATE_NOT_PASSED",
            "fail 또는 blocked Hard Gate가 있는 후보는 점수화할 수 없습니다.",
        )
    if any(
        finding.runtime_status is not RuntimeStatus.NOT_RUN
        for finding in report.gate_findings
    ):
        raise PlanningSearchError(
            "PREMATURE_RUNTIME_SUCCESS_CLAIM",
            "계획 후보는 validation 실행 성공을 주장할 수 없습니다.",
        )
    if any(
        finding.plan_verdict is PlanVerdict.NOT_APPLICABLE
        and not finding.not_applicable_rationale
        for finding in report.gate_findings
    ):
        raise PlanningSearchError(
            "UNJUSTIFIED_NOT_APPLICABLE",
            "근거 없는 not_applicable Hard Gate는 통과로 처리할 수 없습니다.",
        )
    if report.dimension_ratings is None or report.fitness_score is None:
        raise PlanningSearchError(
            "SCORE_INPUT_MISSING", "Hard Gate 통과 후보의 점수 차원 평가가 없습니다."
        )
    expected_refs = {item.client_ref for item in candidate.plan.work_items}
    actual_refs = {item.work_item_ref for item in report.work_item_quality}
    if actual_refs != expected_refs:
        raise PlanningSearchError(
            "WORK_ITEM_QUALITY_COVERAGE_INVALID",
            "PlanDraft의 모든 WorkItem에 정확히 하나의 품질 평가가 필요합니다.",
        )
    weak_items = sorted(
        item.work_item_ref
        for item in report.work_item_quality
        if item.weakest_rating == 0
    )
    if weak_items:
        raise PlanningSearchError(
            "WORK_ITEM_QUALITY_ZERO",
            "0점 품질 축이 있는 WorkItem은 정제 전까지 점수화할 수 없습니다: "
            + ", ".join(weak_items),
        )
    expected_score = balanced_fitness_score(report.dimension_ratings)
    if report.fitness_score != expected_score:
        raise PlanningSearchError(
            "FITNESS_SCORE_MISMATCH",
            "fitness_score가 balanced-mvp-v0 결정적 계산값과 다릅니다.",
        )
    if report.tie_break_evidence is None:
        raise PlanningSearchError(
            "TIE_BREAK_EVIDENCE_MISSING",
            "점수화된 후보에는 결정적 tie-break 근거가 필요합니다.",
        )
    return report


def validate_scored_candidate(candidate: CandidateEnvelope) -> int:
    """후보가 Top-K 진입 자격을 갖췄는지 검사하고 권위 점수를 반환한다."""

    if candidate.status not in {CandidateStatus.ADMISSIBLE, CandidateStatus.SELECTED}:
        raise PlanningSearchError(
            "CANDIDATE_NOT_ADMISSIBLE",
            "admissible 또는 selected 상태 후보만 순위화할 수 있습니다.",
        )
    return _assert_scoreable(candidate).fitness_score  # type: ignore[return-value]


def _tie_break_key(candidate: CandidateEnvelope) -> tuple[object, ...]:
    report = _assert_scoreable(candidate)
    evidence = report.tie_break_evidence
    assert evidence is not None
    mission_values: list[int] = []
    for metric_name, direction in MISSION_TIE_BREAK_ORDER[candidate.mission_primary]:
        value = evidence.mission_metrics.get(metric_name, 0)
        mission_values.append(-value if direction > 0 else value)
    weakest = report.weakest_work_item_rating
    if weakest is None:
        raise PlanningSearchError(
            "WORK_ITEM_QUALITY_MISSING",
            "tie-break에 필요한 최약 WorkItem 평가가 없습니다.",
        )
    return (
        *mission_values,
        -weakest,
        -evidence.reversibility,
        int(evidence.public_contract_change),
        evidence.change_surface,
        evidence.cost,
        candidate.candidate_id,
    )


def _rank_scoreable(candidates: Iterable[CandidateEnvelope]) -> tuple[CandidateEnvelope, ...]:
    """10점 미만 차이를 동점 cluster로 취급해 순서 독립적으로 순위화한다."""

    remaining = sorted(
        tuple(candidates),
        key=lambda item: (-validate_scored_candidate(item), item.candidate_id),
    )
    ranked: list[CandidateEnvelope] = []
    while remaining:
        cluster_max = validate_scored_candidate(remaining[0])
        tied = [
            item
            for item in remaining
            if cluster_max - validate_scored_candidate(item) < CLEAR_WIN_MARGIN
        ]
        tied_ids = {item.candidate_id for item in tied}
        ranked.extend(sorted(tied, key=_tie_break_key))
        remaining = [item for item in remaining if item.candidate_id not in tied_ids]
    return tuple(ranked)


def deduplicate_candidates(
    candidates: Iterable[CandidateEnvelope],
) -> tuple[CandidateEnvelope, ...]:
    """접근 signature 또는 동일 계획·계약별로 전체 후보 하나만 남긴다.

    ApproachBrief 문구만 바꾼 후보뿐 아니라 DAG·계약·산출물이 동일한 후보도
    같은 cluster로 묶는다. 두 동일성 관계가 서로 이어지면 하나의 cluster가
    되도록 union-find를 사용해 입력 순서의 영향을 없앤다.
    """

    materialized = tuple(candidates)
    parents = list(range(len(materialized)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    identities: dict[tuple[str, ...], int] = {}
    for index, candidate in enumerate(materialized):
        keys = (
            ("approach", candidate.approach.signature),
            (
                "plan-contract",
                candidate.plan.canonical_digest,
                candidate.contract.contract_digest,
            ),
        )
        for key in keys:
            prior = identities.get(key)
            if prior is None:
                identities[key] = index
            else:
                union(index, prior)

    clusters: dict[int, list[CandidateEnvelope]] = defaultdict(list)
    for index, candidate in enumerate(materialized):
        clusters[find(index)].append(candidate)
    selected: list[CandidateEnvelope] = []
    for cluster_id in sorted(clusters):
        cluster = clusters[cluster_id]
        scoreable = [
            item
            for item in cluster
            if item.status in {CandidateStatus.ADMISSIBLE, CandidateStatus.SELECTED}
        ]
        if scoreable:
            selected.append(_rank_scoreable(scoreable)[0])
        else:
            selected.append(min(cluster, key=lambda item: item.candidate_id))
    return tuple(sorted(selected, key=lambda item: item.candidate_id))


def _failure_reasons(candidates: tuple[CandidateEnvelope, ...]) -> tuple[str, ...]:
    if not candidates:
        return ("후보가 생성되지 않았습니다.",)
    reasons: list[str] = []
    for status in (
        CandidateStatus.BLOCKED,
        CandidateStatus.NEEDS_REVISION,
        CandidateStatus.REJECTED,
        CandidateStatus.GENERATED,
    ):
        count = sum(candidate.status is status for candidate in candidates)
        if count:
            reasons.append(f"{status.value} 후보 {count}개")
    return tuple(reasons) or ("순위화 가능한 admissible 후보가 없습니다.",)


def _selection_reason(
    ranked: tuple[CandidateEnvelope, ...], mission: MissionPrimary
) -> tuple[str, ...]:
    if len(ranked) < 2:
        return ("단일 admissible signature 후보",)
    first = ranked[0]
    second = ranked[1]
    first_score = validate_scored_candidate(first)
    second_score = validate_scored_candidate(second)
    score_gap = abs(first_score - second_score)
    if score_gap >= CLEAR_WIN_MARGIN:
        return (f"fitness_score 차이 {score_gap}점으로 명확한 우위",)
    evidence_a = _assert_scoreable(first).tie_break_evidence
    evidence_b = _assert_scoreable(second).tie_break_evidence
    assert evidence_a is not None and evidence_b is not None
    reasons = [f"fitness_score 차이 {score_gap}점은 10점 미만이므로 동점 처리"]
    for name, direction in MISSION_TIE_BREAK_ORDER[mission]:
        left = evidence_a.mission_metrics.get(name, 0)
        right = evidence_b.mission_metrics.get(name, 0)
        if left != right:
            preference = "높은 값" if direction > 0 else "낮은 값"
            reasons.append(f"Mission tie-break {name}: {preference} 우선")
            return tuple(reasons)
    common_values = (
        ("최약 WorkItem", _assert_scoreable(first).weakest_work_item_rating, _assert_scoreable(second).weakest_work_item_rating, "높은 값"),
        ("가역성", evidence_a.reversibility, evidence_b.reversibility, "높은 값"),
        ("public contract 변경", int(evidence_a.public_contract_change), int(evidence_b.public_contract_change), "없는 후보"),
        ("변경 표면", evidence_a.change_surface, evidence_b.change_surface, "작은 값"),
        ("비용", evidence_a.cost, evidence_b.cost, "작은 값"),
    )
    for label, left, right, preference in common_values:
        if left != right:
            reasons.append(f"공통 tie-break {label}: {preference} 우선")
            return tuple(reasons)
    reasons.append("모든 품질 근거가 같아 candidate_id 사전순으로 결정")
    return tuple(reasons)


class DeterministicPlanningSearch:
    """모델·Core capability 없이 평가 완료 후보를 제한형 검색·선택한다."""

    def search(
        self,
        frozen_run_input: PlanningRunInput | PlanningRunReceipt,
        candidates: Iterable[CandidateEnvelope],
        *,
        run_id: str | None = None,
        user_selected_candidate_id: str | None = None,
    ) -> PlanningSearchOutcome:
        if isinstance(frozen_run_input, PlanningRunReceipt):
            if frozen_run_input.status not in {
                PlanningRunStatus.FROZEN,
                PlanningRunStatus.SEARCHING,
            }:
                raise PlanningSearchError(
                    "PLANNING_RUN_NOT_FROZEN",
                    "frozen 또는 searching PlanningRun만 검색할 수 있습니다.",
                )
            if run_id is not None and run_id != frozen_run_input.run_id:
                raise PlanningSearchError(
                    "RUN_ID_MISMATCH", "명시 run_id가 PlanningRunReceipt와 다릅니다."
                )
            run_id = frozen_run_input.run_id
            planning_input = frozen_run_input.planning_input
        else:
            planning_input = frozen_run_input
        if run_id is None:
            raise PlanningSearchError(
                "RUN_ID_REQUIRED", "PlanningRunInput 검색에는 run_id가 필요합니다."
            )
        mission = planning_input.mission_selection.mission
        if mission is None:
            raise PlanningSearchError(
                "MISSION_NOT_RESOLVED", "확정되지 않은 Mission으로 후보를 검색할 수 없습니다."
            )
        return self.search_candidates(
            run_id=run_id,
            planning_input_digest=planning_input.planning_input_digest,
            mission_resolution_digest=(
                planning_input.mission_selection.mission_resolution_digest
            ),
            mission_primary=mission.primary,
            candidates=candidates,
            user_selected_candidate_id=user_selected_candidate_id,
        )

    def search_candidates(
        self,
        *,
        run_id: str,
        planning_input_digest: str,
        mission_resolution_digest: str,
        mission_primary: MissionPrimary,
        candidates: Iterable[CandidateEnvelope],
        user_selected_candidate_id: str | None = None,
    ) -> PlanningSearchOutcome:
        try:
            mission_primary = MissionPrimary(mission_primary)
        except ValueError as exc:
            raise PlanningSearchError(
                "MISSION_PRIMARY_INVALID", "알 수 없는 PlanningMission primary입니다."
            ) from exc
        materialized = tuple(candidates)
        self._validate_run_scope(
            materialized,
            planning_input_digest=planning_input_digest,
            mission_resolution_digest=mission_resolution_digest,
            mission_primary=mission_primary,
        )
        self._validate_budget(materialized, mission_primary)

        # 정제는 새 version 후보를 추가할 뿐 정상 부모를 파괴하지 않는다. 부모와 자식이
        # 같은 signature면 아래 diversity dedupe가 재평가 근거로 더 나은 version을 고른다.
        # blocked/rejected 자식 때문에 기존 admissible fallback이 사라져서는 안 된다.
        frontier = materialized
        scoreable = tuple(
            candidate
            for candidate in frontier
            if candidate.status in {CandidateStatus.ADMISSIBLE, CandidateStatus.SELECTED}
        )
        # 이 호출은 status뿐 아니라 Gate·WorkItem·점수 결속을 다시 검증한다.
        for candidate in scoreable:
            validate_scored_candidate(candidate)

        diverse = deduplicate_candidates(scoreable)
        pruned_duplicate_ids = tuple(
            sorted(
                {item.candidate_id for item in scoreable}
                - {item.candidate_id for item in diverse}
            )
        )
        ranked_all = _rank_scoreable(diverse)
        top_k = ranked_all[:MAX_TOP_K]
        if not top_k:
            blocked = any(
                candidate.status is CandidateStatus.BLOCKED for candidate in frontier
            )
            status = (
                SearchOutcomeStatus.BLOCKED
                if blocked
                else SearchOutcomeStatus.NO_ADMISSIBLE
            )
            receipt = SelectionReceipt(
                run_id=run_id,
                planning_input_digest=planning_input_digest,
                recommended_candidate_id=None,
                selected_candidate_id=None,
                selection_source=SelectionSource.NONE_NO_ADMISSIBLE,
                ranked_candidate_ids=(),
                alternative_candidate_ids=(),
                tie_break_reasons=(),
                policy_id=R31_SCORE_POLICY_ID,
                policy_version=R31_SCORE_POLICY_VERSION,
            )
            return PlanningSearchOutcome(
                run_id=run_id,
                planning_input_digest=planning_input_digest,
                mission_primary=mission_primary,
                status=status,
                candidates=materialized,
                top_k_candidate_ids=(),
                selection_receipt=receipt,
                failure_reasons=_failure_reasons(frontier),
            )

        top_ids = tuple(candidate.candidate_id for candidate in top_k)
        recommended = top_k[0]
        if user_selected_candidate_id is not None:
            if user_selected_candidate_id not in set(top_ids):
                raise PlanningSearchError(
                    "USER_SELECTION_NOT_IN_TOP_K",
                    "사용자 대안 선택은 이번 run의 admissible Top-K 후보여야 합니다.",
                )
            selected_id = user_selected_candidate_id
            source = SelectionSource.USER_OVERRIDE
        elif recommended.quality_report is not None and (
            recommended.quality_report.confidence is not ConfidenceLevel.LOW
        ):
            selected_id = recommended.candidate_id
            source = SelectionSource.RECOMMENDED_DEFAULT
        else:
            selected_id = None
            source = SelectionSource.NONE_LOW_CONFIDENCE

        finalized: list[CandidateEnvelope] = []
        for candidate in materialized:
            if candidate.candidate_id == selected_id:
                finalized.append(
                    CandidateEnvelope.model_validate(
                        {
                            **candidate.model_dump(mode="python"),
                            "status": CandidateStatus.SELECTED,
                        }
                    )
                )
            elif candidate.status is CandidateStatus.SELECTED:
                finalized.append(
                    CandidateEnvelope.model_validate(
                        {
                            **candidate.model_dump(mode="python"),
                            "status": CandidateStatus.ADMISSIBLE,
                        }
                    )
                )
            else:
                finalized.append(candidate)

        alternatives = tuple(
            candidate_id
            for candidate_id in top_ids
            if candidate_id not in {selected_id, recommended.candidate_id}
        )
        if selected_id is not None and selected_id != recommended.candidate_id:
            alternatives = (recommended.candidate_id, *alternatives)
        receipt = SelectionReceipt(
            run_id=run_id,
            planning_input_digest=planning_input_digest,
            recommended_candidate_id=recommended.candidate_id,
            selected_candidate_id=selected_id,
            selection_source=source,
            ranked_candidate_ids=top_ids,
            alternative_candidate_ids=alternatives,
            pruned_duplicate_candidate_ids=pruned_duplicate_ids,
            tie_break_reasons=_selection_reason(top_k, mission_primary),
            policy_id=R31_SCORE_POLICY_ID,
            policy_version=R31_SCORE_POLICY_VERSION,
        )
        return PlanningSearchOutcome(
            run_id=run_id,
            planning_input_digest=planning_input_digest,
            mission_primary=mission_primary,
            status=SearchOutcomeStatus.READY_FOR_REVIEW,
            candidates=tuple(finalized),
            top_k_candidate_ids=top_ids,
            selection_receipt=receipt,
        )

    def override_selection(
        self, outcome: PlanningSearchOutcome, candidate_id: str
    ) -> PlanningSearchOutcome:
        """재검색 없이 현재 Top-K에 대한 새 immutable 사용자 선택 receipt를 만든다."""

        if outcome.status is not SearchOutcomeStatus.READY_FOR_REVIEW:
            raise PlanningSearchError(
                "SEARCH_NOT_READY_FOR_REVIEW",
                "ready_for_review 검색 결과에서만 대안을 선택할 수 있습니다.",
            )
        if candidate_id not in set(outcome.top_k_candidate_ids):
            raise PlanningSearchError(
                "USER_SELECTION_NOT_IN_TOP_K", "선택 후보가 현재 Top-K에 없습니다."
            )
        finalized = tuple(
            CandidateEnvelope.model_validate(
                {
                    **candidate.model_dump(mode="python"),
                    "status": (
                        CandidateStatus.SELECTED
                        if candidate.candidate_id == candidate_id
                        else CandidateStatus.ADMISSIBLE
                        if candidate.status is CandidateStatus.SELECTED
                        else candidate.status
                    ),
                }
            )
            for candidate in outcome.candidates
        )
        receipt = SelectionReceipt(
            run_id=outcome.run_id,
            planning_input_digest=outcome.planning_input_digest,
            recommended_candidate_id=(
                outcome.selection_receipt.recommended_candidate_id
            ),
            selected_candidate_id=candidate_id,
            selection_source=SelectionSource.USER_OVERRIDE,
            ranked_candidate_ids=outcome.selection_receipt.ranked_candidate_ids,
            alternative_candidate_ids=tuple(
                item for item in outcome.top_k_candidate_ids if item != candidate_id
            ),
            pruned_duplicate_candidate_ids=(
                outcome.selection_receipt.pruned_duplicate_candidate_ids
            ),
            tie_break_reasons=outcome.selection_receipt.tie_break_reasons,
            supersedes_selection_digest=outcome.selection_receipt.selection_digest,
            policy_id=outcome.selection_receipt.policy_id,
            policy_version=outcome.selection_receipt.policy_version,
        )
        return PlanningSearchOutcome.model_validate(
            {
                **outcome.model_dump(mode="python"),
                "candidates": finalized,
                "selection_receipt": receipt,
            }
        )

    @staticmethod
    def _validate_run_scope(
        candidates: tuple[CandidateEnvelope, ...],
        *,
        planning_input_digest: str,
        mission_resolution_digest: str,
        mission_primary: MissionPrimary,
    ) -> None:
        for candidate in candidates:
            if candidate.planning_input_digest != planning_input_digest:
                raise PlanningSearchError(
                    "CROSS_PLANNING_INPUT_RANKING_FORBIDDEN",
                    "서로 다른 planning input의 후보는 같은 순위표에서 비교할 수 없습니다.",
                )
            if (
                candidate.mission_resolution_digest != mission_resolution_digest
                or candidate.mission_primary is not mission_primary
            ):
                raise PlanningSearchError(
                    "CROSS_MISSION_RANKING_FORBIDDEN",
                    "서로 다른 Mission의 후보는 같은 순위표에서 비교할 수 없습니다.",
                )
            if (
                candidate.policy_id != R31_SCORE_POLICY_ID
                or candidate.policy_version != R31_SCORE_POLICY_VERSION
            ):
                raise PlanningSearchError(
                    "SCORE_POLICY_MISMATCH",
                    "R3.1 검색은 balanced-mvp-v0 정책 하나만 허용합니다.",
                )

    @staticmethod
    def _validate_budget(
        candidates: tuple[CandidateEnvelope, ...], mission: MissionPrimary
    ) -> None:
        identifiers = [candidate.candidate_id for candidate in candidates]
        if len(identifiers) != len(set(identifiers)):
            raise PlanningSearchError(
                "CANDIDATE_ID_DUPLICATED", "candidate_id가 중복됐습니다."
            )
        if len(candidates) > MAX_CANDIDATE_VERSIONS:
            raise PlanningSearchError(
                "CANDIDATE_VERSION_BUDGET_EXCEEDED",
                f"전체 candidate version은 {MAX_CANDIDATE_VERSIONS}개를 넘을 수 없습니다.",
            )
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        initial = [candidate for candidate in candidates if candidate.parent_candidate_id is None]
        initial_limit = (
            MAX_ANALYSIS_INITIAL_CANDIDATES
            if mission is MissionPrimary.ANALYSIS_AUDIT
            else MAX_INITIAL_CANDIDATES
        )
        if len(initial) > initial_limit:
            raise PlanningSearchError(
                "INITIAL_CANDIDATE_BUDGET_EXCEEDED",
                f"{mission.value}의 초기 후보 상한은 {initial_limit}개입니다.",
            )
        child_count: dict[str, int] = defaultdict(int)
        for candidate in candidates:
            if candidate.refinement_round > MAX_REFINEMENT_ROUNDS:
                raise PlanningSearchError(
                    "REFINEMENT_BUDGET_EXCEEDED", "후보 정제는 최대 1회만 허용됩니다."
                )
            parent_id = candidate.parent_candidate_id
            if parent_id is None:
                if candidate.version != 1 or candidate.refinement_round != 0:
                    raise PlanningSearchError(
                        "CANDIDATE_LINEAGE_INVALID",
                        "초기 후보는 version 1, refinement_round 0이어야 합니다.",
                    )
                continue
            parent = by_id.get(parent_id)
            if parent is None:
                raise PlanningSearchError(
                    "CANDIDATE_PARENT_MISSING", "정제 후보의 parent가 검색 run에 없습니다."
                )
            if parent.parent_candidate_id is not None:
                raise PlanningSearchError(
                    "RECURSIVE_SEARCH_FORBIDDEN",
                    "R3.1에서는 재귀 후보 정제를 허용하지 않습니다.",
                )
            if candidate.version != parent.version + 1 or candidate.refinement_round != 1:
                raise PlanningSearchError(
                    "CANDIDATE_LINEAGE_INVALID",
                    "정제 후보는 parent 다음 version과 refinement_round 1이어야 합니다.",
                )
            child_count[parent_id] += 1
            if child_count[parent_id] > 1:
                raise PlanningSearchError(
                    "REFINEMENT_BUDGET_EXCEEDED",
                    "각 후보는 하나의 정제 version만 가질 수 있습니다.",
                )


class SelectedPlanExporter:
    """선택 receipt 결속을 확인하고 PlanDraft 하나만 내보낸다."""

    def export(
        self,
        outcome: PlanningSearchOutcome,
        expected_selection_receipt_digest: str,
    ) -> SelectedPlanExport:
        observed_digest = outcome.selection_receipt.selection_digest
        if not hmac.compare_digest(observed_digest, expected_selection_receipt_digest):
            raise PlanningSearchError(
                "SELECTION_RECEIPT_DIGEST_MISMATCH",
                "기대한 SelectionReceipt digest와 현재 선택이 다릅니다.",
            )
        if outcome.status is not SearchOutcomeStatus.READY_FOR_REVIEW:
            raise PlanningSearchError(
                "SEARCH_NOT_READY_FOR_EXPORT",
                "ready_for_review 검색 결과만 export할 수 있습니다.",
            )
        receipt = outcome.selection_receipt
        if receipt.planning_input_digest != outcome.planning_input_digest:
            raise PlanningSearchError(
                "SELECTION_INPUT_DIGEST_MISMATCH",
                "SelectionReceipt가 검색 결과의 planning input에 결속되지 않았습니다.",
            )
        if (
            receipt.policy_id != R31_SCORE_POLICY_ID
            or receipt.policy_version != R31_SCORE_POLICY_VERSION
        ):
            raise PlanningSearchError(
                "SELECTION_POLICY_MISMATCH",
                "SelectionReceipt가 balanced-mvp-v0 정책으로 생성되지 않았습니다.",
            )
        selected_id = receipt.selected_candidate_id
        if selected_id is None:
            raise PlanningSearchError(
                "SELECTION_REQUIRED",
                "low confidence 또는 미선택 결과는 사용자 선택 전 export할 수 없습니다.",
            )
        matches = [
            candidate
            for candidate in outcome.candidates
            if candidate.candidate_id == selected_id
        ]
        if len(matches) != 1:
            raise PlanningSearchError(
                "SELECTED_CANDIDATE_INVALID",
                "SelectionReceipt가 정확히 하나의 후보를 참조하지 않습니다.",
            )
        selected = matches[0]
        if selected.status is not CandidateStatus.SELECTED:
            raise PlanningSearchError(
                "SELECTED_CANDIDATE_STATUS_INVALID",
                "export 후보의 상태가 selected가 아닙니다.",
            )
        if selected.candidate_id not in set(outcome.top_k_candidate_ids):
            raise PlanningSearchError(
                "SELECTED_CANDIDATE_NOT_IN_TOP_K",
                "선택 후보가 검토 가능한 Top-K에 없습니다.",
            )
        if selected.planning_input_digest != outcome.planning_input_digest:
            raise PlanningSearchError(
                "SELECTED_INPUT_DIGEST_MISMATCH",
                "선택 후보가 검색 결과의 planning input과 다릅니다.",
            )
        if (
            selected.policy_id != receipt.policy_id
            or selected.policy_version != receipt.policy_version
        ):
            raise PlanningSearchError(
                "SELECTED_POLICY_MISMATCH",
                "선택 후보와 SelectionReceipt의 평가 정책이 다릅니다.",
            )
        validate_scored_candidate(selected)
        return SelectedPlanExport(selected.plan, observed_digest)


# 권위 설계의 port 이름을 유지하되 구현은 명시적으로 deterministic하다.
PlanningSearchService = DeterministicPlanningSearch
