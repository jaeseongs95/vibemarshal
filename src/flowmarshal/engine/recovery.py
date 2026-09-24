"""직접 evidence를 우선하는 실행 실패 분류와 자동 복구 입력 생성.

모델의 의미 추론은 명시 error code와 transport/effect 관측으로 결정할 수 없는
경우에만 후속 진단 경계에서 사용한다. 이 모듈의 분류기는 근거가 부족한 failed
terminal을 구현 오류로 추정하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from pydantic import Field, ValidationError

from ..canonical import sha256_digest
from .domain import (
    DeterministicValidationObservation,
    EngineModel,
    FailureClass,
    RepairAction,
)


_CODE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]{2,99})\b")
_CODE_KEYS = frozenset({"blocker_code", "code", "error_code", "failure_code"})

#: Engine이 직접 실행해 관측한 결정적 검사 evidence 종류.
_DETERMINISTIC_VALIDATION_EVIDENCE_KINDS = frozenset({"test", "command", "build"})


def _deterministic_validation_observation(
    document: Mapping[str, Any],
    signal: "FailureSignal",
) -> DeterministicValidationObservation | None:
    """검사 evidence 하나를 strict 관측으로 읽고 결속이 맞을 때만 돌려준다.

    evidence 본문은 기록 한도에서 잘릴 수 있으므로 절단·불완전 JSON은 분류하지 않는다.
    evidence record가 스스로 밝힌 Task·Attempt·validation 결속이 안의 관측이나 실패한
    Attempt·validation과 다르면 다른 Attempt나 다른 검사의 관측이므로 분류 근거로 쓰지 않는다.
    """

    if signal.failed_attempt_id is None or signal.failed_validation_id is None:
        return None
    if str(document.get("kind", "")).casefold() not in _DETERMINISTIC_VALIDATION_EVIDENCE_KINDS:
        return None
    observation = document.get("observation")
    if not isinstance(observation, str):
        return None
    try:
        parsed = DeterministicValidationObservation.model_validate_json(observation)
    except ValidationError:
        return None
    if (
        document.get("attempt_id") != signal.failed_attempt_id
        or parsed.validation_id != signal.failed_validation_id
        or document.get("task_id") != parsed.task_id
    ):
        return None
    if document.get("source_ref") != f"validation:{parsed.validation_id}:{parsed.argv[0]}":
        return None
    return parsed


#: `FailureClass` → `RepairAction`의 유일한 결정적 routing 표.
#:
#: 여덟 분류(`unclassified` 포함) 전부가 정확히 하나의 action으로 대응한다. 분류마다
#: 별도 지능형 복구기를 두지 않고 Core·분류기·service가 모두 이 표만 참조한다.
FAILURE_REPAIR_ACTIONS: Mapping[FailureClass, RepairAction] = MappingProxyType({
    FailureClass.UNCLASSIFIED: RepairAction.ABANDON,
    FailureClass.IMPLEMENTATION: RepairAction.TASK_REPAIR,
    FailureClass.CONTEXT: RepairAction.EXECUTION_SPEC_REVISION,
    FailureClass.TASK_CONTRACT: RepairAction.SUBGRAPH_REPLAN,
    FailureClass.DEPENDENCY: RepairAction.SUBGRAPH_REPLAN,
    FailureClass.ENVIRONMENT: RepairAction.CONTINUE,
    FailureClass.REQUIREMENT_CHANGE: RepairAction.GOAL_REVISION,
    FailureClass.EXTERNAL_UNKNOWN: RepairAction.WAIT_EXTERNAL,
})

#: provider/local이 명시한 code 중 "같은 입력으로 다시 시도할 수 있음"을 직접 뜻하는 집합.
#:
#: 이 집합의 code만 bounded transient local retry를 얻는다. 권한·실행 파일·model lock
#: 처럼 재시도로 해소되지 않는 environment 실패는 여기에 넣지 않는다.
TRANSIENT_LOCAL_CODES = frozenset({
    "RATE_LIMITED",
    "PROCESS_TIMEOUT",
    "RESOURCE_LOCK_CONTENDED",
    "TEMPORARY_LOCAL_FAILURE",
})


class FailureDiagnosis(EngineModel):
    """분류 결과와 그 결정을 직접 지지하는 evidence 결속."""

    failure_class: FailureClass | None = None
    repair_action: RepairAction | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,99}$")
    provider_error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,99}$")
    local_engine_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,99}$")
    evidence_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=5000)
    source: str = Field(pattern=r"^(explicit_code|effect|transport|direct_evidence|unclassified)$")
    model_reported_codes: tuple[str, ...] = ()
    transient: bool = False

    @property
    def failure_fingerprint(self) -> str:
        return sha256_digest({
            "failure_class": None if self.failure_class is None else self.failure_class.value,
            "error_code": self.error_code,
            "source": self.source,
        })


@dataclass(frozen=True)
class FailureSignal:
    terminal_status: str | None
    final_response: str | None
    provider_payload: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()
    evidence_documents: tuple[dict[str, Any], ...] = ()
    local_engine_codes: tuple[str, ...] = ()
    # Task validation FAIL을 분류할 때만 채운다. 직접 검사 evidence는 이 둘과 결속돼야 한다.
    failed_attempt_id: str | None = None
    failed_validation_id: str | None = None


class EvidenceFirstFailureClassifier:
    """명시 코드 → effect/transport → 직접 evidence 순서로 분류한다."""

    _CODE_CLASS = {
        # Context와 입력 freshness
        "CONTEXT_REQUIRED": FailureClass.CONTEXT,
        "CONTEXT_NOT_FOUND": FailureClass.CONTEXT,
        "CONTEXT_SOURCE_NOT_MAPPED": FailureClass.CONTEXT,
        "MISSING_CONTEXT": FailureClass.CONTEXT,
        "STALE_EXECUTION_INPUT": FailureClass.CONTEXT,
        "PROMPT_BINDING_MISMATCH": FailureClass.CONTEXT,
        # 실행 환경/도구
        "PERMISSION_POLICY_MISMATCH": FailureClass.ENVIRONMENT,
        "EXECUTABLE_NOT_FOUND": FailureClass.ENVIRONMENT,
        "ENVIRONMENT_ERROR": FailureClass.ENVIRONMENT,
        "ABSOLUTE_DEADLINE_EXCEEDED": FailureClass.ENVIRONMENT,
        "RESUME_EXHAUSTED": FailureClass.ENVIRONMENT,
        "PROCESS_TIMEOUT": FailureClass.ENVIRONMENT,
        "RATE_LIMITED": FailureClass.ENVIRONMENT,
        "RESOURCE_LOCK_CONTENDED": FailureClass.ENVIRONMENT,
        "TEMPORARY_LOCAL_FAILURE": FailureClass.ENVIRONMENT,
        # 계약과 dependency
        "TASK_CONTRACT_INVALID": FailureClass.TASK_CONTRACT,
        "PROPOSAL_TASK_MISMATCH": FailureClass.TASK_CONTRACT,
        "DEPENDENCY_FAILED": FailureClass.DEPENDENCY,
        "DEPENDENCY_MISSING": FailureClass.DEPENDENCY,
        # 사용자 승인 경계
        "REQUIREMENT_CHANGE": FailureClass.REQUIREMENT_CHANGE,
        "SCOPE_EXPANSION_REQUIRED": FailureClass.REQUIREMENT_CHANGE,
        "GOAL_AUTHORIZATION_REQUIRED": FailureClass.REQUIREMENT_CHANGE,
        # 외부 효과/transport 불명
        "EXTERNAL_EFFECT_UNKNOWN": FailureClass.EXTERNAL_UNKNOWN,
        "TRANSPORT_UNKNOWN": FailureClass.EXTERNAL_UNKNOWN,
        "RESPONSE_LOST": FailureClass.EXTERNAL_UNKNOWN,
        "CONNECTION_LOST": FailureClass.EXTERNAL_UNKNOWN,
        "RUNTIME_OBSERVATION_BINDING_MISMATCH": FailureClass.EXTERNAL_UNKNOWN,
        # 명시 구현 결함
        "IMPLEMENTATION_ERROR": FailureClass.IMPLEMENTATION,
        "TARGET_CONTRACT_VIOLATION": FailureClass.IMPLEMENTATION,
        "TEST_FAILED": FailureClass.IMPLEMENTATION,
        "ASSERTION_FAILED": FailureClass.IMPLEMENTATION,
    }

    #: Core·service와 같은 단일 routing 표를 참조한다.
    _ACTION = FAILURE_REPAIR_ACTIONS

    @staticmethod
    def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
        if isinstance(value, dict):
            for key, child in value.items():
                yield str(key), child
                yield from EvidenceFirstFailureClassifier._walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield None, child
                yield from EvidenceFirstFailureClassifier._walk(child)

    @staticmethod
    def _codes_in(value: Any) -> Iterable[str]:
        for key, child in EvidenceFirstFailureClassifier._walk(value):
            if key in _CODE_KEYS and isinstance(child, str):
                match = _CODE_PATTERN.search(child)
                if match:
                    yield match.group(1)

    @classmethod
    def _provider_codes(cls, signal: FailureSignal) -> tuple[str, ...]:
        """provider가 구조화해 반환한 payload의 code만 반환한다.

        ``final_response``는 모델이 작성한 비권위 텍스트다. 선두에 알려진 코드가
        있더라도 자동 recovery의 explicit error로 승격하지 않는다.
        """

        return tuple(dict.fromkeys(cls._codes_in(signal.provider_payload)))

    @classmethod
    def _local_codes(cls, signal: FailureSignal) -> tuple[str, ...]:
        """로컬 Engine이 직접 관측해 만든 code와 Core가 기록한 evidence 장부의 code."""

        codes: list[str] = []
        for code in signal.local_engine_codes:
            match = _CODE_PATTERN.search(code)
            if match:
                codes.append(match.group(1))
        for document in signal.evidence_documents:
            codes.extend(cls._codes_in(document))
        return tuple(dict.fromkeys(codes))

    @staticmethod
    def _model_reported_codes(signal: FailureSignal) -> tuple[str, ...]:
        response = (signal.final_response or "").strip()
        match = re.match(r"^([A-Z][A-Z0-9_]{2,99})(?:\s*[:;-]|$)", response)
        return () if match is None else (match.group(1),)

    @classmethod
    def classify(cls, signal: FailureSignal) -> FailureDiagnosis:
        provider_codes = cls._provider_codes(signal)
        # 같은 code가 Core evidence 장부에도 복제돼 있으면 provider 출처를 유지한다.
        local_codes = tuple(
            code for code in cls._local_codes(signal) if code not in provider_codes
        )
        codes = tuple(dict.fromkeys(provider_codes + local_codes))
        model_reported_codes = cls._model_reported_codes(signal)
        for code in codes:
            failure_class = cls._CODE_CLASS.get(code)
            if failure_class is None and code.startswith("MODEL_LOCK_"):
                failure_class = FailureClass.ENVIRONMENT
            if failure_class is not None:
                return FailureDiagnosis(
                    failure_class=failure_class,
                    repair_action=cls._ACTION[failure_class],
                    error_code=code,
                    provider_error_code=code if code in provider_codes else None,
                    local_engine_code=code if code in local_codes else None,
                    evidence_ids=signal.evidence_ids,
                    rationale=f"직접 관측된 error code {code}를 우선 적용했습니다.",
                    source="explicit_code",
                    model_reported_codes=model_reported_codes,
                    transient=(
                        failure_class is FailureClass.ENVIRONMENT
                        and code in TRANSIENT_LOCAL_CODES
                    ),
                )

        flattened = json.dumps(signal.provider_payload, ensure_ascii=False).casefold()
        if any(token in flattened for token in (
            '"effect_status": "unknown"', '"intent_status": "unknown"',
            '"receipt_missing": true', '"external_effect_unknown": true',
        )):
            return FailureDiagnosis(
                failure_class=FailureClass.EXTERNAL_UNKNOWN,
                repair_action=RepairAction.WAIT_EXTERNAL,
                evidence_ids=signal.evidence_ids,
                rationale="외부 효과 또는 receipt 상태가 unknown인 직접 관측을 우선했습니다.",
                source="effect",
                model_reported_codes=model_reported_codes,
            )
        if any(token in flattened for token in (
            '"transport_error"', '"rpc_error"', '"connection_error"',
            '"response_lost": true',
        )):
            return FailureDiagnosis(
                failure_class=FailureClass.EXTERNAL_UNKNOWN,
                repair_action=RepairAction.WAIT_EXTERNAL,
                evidence_ids=signal.evidence_ids,
                rationale="transport 결과가 불명인 직접 관측이 있어 구현 재시도를 금지했습니다.",
                source="transport",
                model_reported_codes=model_reported_codes,
            )

        # Engine이 직접 실행한 결정적 검사 evidence는 의미 추측 없이 typed 관측 필드로만
        # 판정한다. timeout·실행 실패 같은 환경 증거와 구조가 불완전한 관측은 분류하지
        # 않고 environment/unclassified 경계에 남긴다.
        for document in signal.evidence_documents:
            observation = _deterministic_validation_observation(document, signal)
            if observation is None or observation.timed_out or observation.passed:
                continue
            return FailureDiagnosis(
                failure_class=FailureClass.IMPLEMENTATION,
                repair_action=RepairAction.TASK_REPAIR,
                evidence_ids=signal.evidence_ids,
                rationale=(
                    f"직접 실행한 검사 {observation.validation_id}의 종료 코드 "
                    f"{observation.actual_exit_code}가 기대 "
                    f"{list(observation.expected_exit_codes)} 밖입니다."
                ),
                source="direct_evidence",
                model_reported_codes=model_reported_codes,
            )

        return FailureDiagnosis(
            evidence_ids=signal.evidence_ids,
            rationale=(
                "명시 오류코드, transport/effect 상태 또는 직접 구현 evidence가 없어 "
                "failed terminal만으로 원인을 추정하지 않았습니다."
            ),
            source="unclassified",
            model_reported_codes=model_reported_codes,
        )


#: 실패 기록 시점에 남기는 분류 projection의 provenance 표식.
FAILURE_DIAGNOSIS_PROVENANCE = "local_derived"


def stable_recovery_assessment_id(attempt_id: str, failure_fingerprint: str) -> str:
    """같은 Attempt·실패 지문에는 언제나 같은 assessment ID를 만든다."""

    digest = hashlib.sha256(
        f"{attempt_id}:{failure_fingerprint}".encode("utf-8")
    ).hexdigest()
    return f"recovery_assessment_{digest[:32]}"


def replan_retry_assessment_id(
    attempt_id: str, failure_fingerprint: str, blocked_plan_revision_id: str
) -> str:
    """차단 재계획 후보 하나에 대한 사용자 재시도 assessment ID. 후보당 하나로 결정적이다."""

    digest = hashlib.sha256(
        f"{attempt_id}:{failure_fingerprint}:{blocked_plan_revision_id}".encode("utf-8")
    ).hexdigest()
    return f"recovery_assessment_{digest[:32]}"


def current_replan_assessment(
    connection: Any, *, project_id: str, attempt_id: str, failure_fingerprint: str,
) -> str:
    """run_once·status·replan 명령·재승인 target이 함께 쓰는 재계획 head assessment ID.

    같은 Attempt·실패 지문의 stable assessment에서 시작한다. 그 `replanning:{ID}` checkpoint에서
    소비된 후보에 대해 Core가 기록한 재시도 행이 있으면 그 행으로 옮겨 가고, 없으면 멈춘다.
    재시도 행이 없으면 stable ID 그대로이므로 기존 자동 경로의 checkpoint는 바뀌지 않는다.
    읽기 전용이다.
    """

    head = stable_recovery_assessment_id(attempt_id, failure_fingerprint)
    seen: set[str] = set()
    while head not in seen:
        seen.add(head)
        job = connection.execute(
            "SELECT result_json FROM runtime_jobs WHERE project_id=? AND checkpoint_key=? "
            "AND status='consumed'",
            (project_id, f"replanning:{head}"),
        ).fetchone()
        try:
            blocked = json.loads(job["result_json"])["plan"]["plan_revision_id"]
        except (TypeError, KeyError, ValueError):
            return head
        retry = replan_retry_assessment_id(attempt_id, failure_fingerprint, blocked)
        if connection.execute(
            "SELECT 1 FROM recovery_assessments WHERE id=? AND project_id=?", (retry, project_id)
        ).fetchone() is None:
            return head
        head = retry
    return head


def terminal_failure_diagnosis(
    documents: tuple[dict[str, Any], ...],
    *,
    classifier: "EvidenceFirstFailureClassifier",
) -> FailureDiagnosis | None:
    """보존된 failed terminal evidence에서 code 출처와 일시 실패 표시를 읽는다.

    Attempt 행에는 `failure_class`만 남으므로 provider/local code 출처와 일시 실패
    표시는 실패 관측 evidence에서만 복원할 수 있다. 기록 시점에 남긴
    `failure_diagnosis` projection(`local_derived`)을 우선 읽고, projection이 없는
    이전 형식 문서만 보존된 `provider_payload`를 같은 분류기로 다시 관측한다.
    실패 판정 자체는 바꾸지 않으며 호출자가 원장 분류와 일치할 때만 사용한다.
    """

    for document in documents:
        observation = document.get("observation")
        if isinstance(observation, str):
            try:
                observation = json.loads(observation)
            except ValueError:
                continue
        if not isinstance(observation, dict):
            continue
        projection = observation.get("failure_diagnosis")
        if (
            isinstance(projection, dict)
            and projection.get("provenance") == FAILURE_DIAGNOSIS_PROVENANCE
        ):
            failure_class = projection.get("failure_class")
            try:
                parsed_class = (
                    None if failure_class is None else FailureClass(failure_class)
                )
                return FailureDiagnosis(
                    failure_class=parsed_class,
                    repair_action=(
                        None if parsed_class is None
                        else FAILURE_REPAIR_ACTIONS[parsed_class]
                    ),
                    error_code=projection.get("error_code"),
                    provider_error_code=projection.get("provider_error_code"),
                    local_engine_code=projection.get("local_engine_code"),
                    rationale="실패 기록 시점의 local_derived 분류 projection",
                    source=projection.get("source") or "unclassified",
                    model_reported_codes=tuple(
                        projection.get("model_reported_codes") or ()
                    ),
                    transient=bool(projection.get("transient")),
                )
            except (ValueError, ValidationError):
                continue
        if "provider_payload" not in observation:
            continue
        payload = observation.get("provider_payload")
        return classifier.classify(FailureSignal(
            terminal_status=observation.get("terminal_status"),
            final_response=observation.get("final_response"),
            provider_payload=payload if isinstance(payload, dict) else {},
        ))
    return None


class RecoveryRoute(EngineModel):
    """분류 하나가 결정하는 자동 복구 경로. 원장 한도 검사 이전 단계다."""

    mode: str = Field(pattern=r"^(automatic|user_decision|observe_first|unrouted)$")
    blocker_code: str | None = Field(default=None, max_length=100)
    suggested_repair_action: RepairAction | None = None
    checkpoint_required: bool = False
    detail: str = Field(min_length=1, max_length=2000)


def recovery_route(
    diagnosis: FailureDiagnosis,
    *,
    validation_result_id: str | None = None,
) -> RecoveryRoute:
    """직접 근거로 분류된 실패만 자동 경로로 보내고 나머지는 사용자 판단에 남긴다.

    model-reported code는 진단 가설일 뿐이므로 `unclassified`를 자동 복구로 승격하지
    않는다. 이 함수는 원장을 읽지 않으며 한도·evidence 검사는 별도 단계다.
    """

    failure = diagnosis.failure_class
    if failure is None or failure is FailureClass.UNCLASSIFIED:
        if validation_result_id is not None:
            return RecoveryRoute(
                mode="unrouted",
                detail=(
                    "직접 근거가 없는 validation 실패는 기존 validation recovery 경계로 넘깁니다."
                ),
            )
        return RecoveryRoute(
            mode="user_decision",
            blocker_code="RECOVERY_DIAGNOSIS_REQUIRED",
            detail=diagnosis.rationale,
        )
    if failure is FailureClass.EXTERNAL_UNKNOWN:
        return RecoveryRoute(
            mode="observe_first",
            suggested_repair_action=RepairAction.WAIT_EXTERNAL,
            checkpoint_required=True,
            detail=(
                "효과가 확인되지 않아 기존 intent·receipt와 대상을 먼저 관측하고 "
                "자동 재실행하지 않습니다."
            ),
        )
    if failure is FailureClass.REQUIREMENT_CHANGE:
        return RecoveryRoute(
            mode="user_decision",
            blocker_code="AUTHORIZATION_EXPANSION_REQUIRED",
            suggested_repair_action=RepairAction.GOAL_REVISION,
            checkpoint_required=True,
            detail="목표·범위·효과·운영 정책 확장은 사용자 승인이 필요합니다.",
        )
    if failure is FailureClass.ENVIRONMENT and not diagnosis.transient:
        # provider/local code가 "다시 시도 가능"을 직접 말하지 않는 환경 실패는
        # 사람이 환경 복구를 관측하기 전까지 같은 실행을 반복하지 않는다.
        return RecoveryRoute(
            mode="user_decision",
            blocker_code="ENVIRONMENT_RECOVERY_REQUIRED",
            suggested_repair_action=RepairAction.CONTINUE,
            checkpoint_required=True,
            detail="환경 복구를 직접 관측하기 전에는 같은 실행을 반복하지 않습니다.",
        )
    return RecoveryRoute(
        mode="automatic",
        suggested_repair_action=diagnosis.repair_action,
        detail="직접 근거로 분류된 실패에 원장 한도 안의 자동 복구를 적용합니다.",
    )


class RecoveryLimitObservation(EngineModel):
    """원장에서 직접 센 복구 한도 관측과 그 결과 차단 code."""

    assessment_id: str = Field(min_length=1, max_length=200)
    assessment_recorded: bool = False
    failure_class_retryable: bool = True
    task_recovery_count: int = Field(ge=0)
    max_task_recovery: int = Field(ge=0)
    same_failure_replan_count: int = Field(ge=0)
    max_same_failure_replans: int = Field(ge=0)
    goal_replan_count: int = Field(ge=0)
    max_goal_replans: int = Field(ge=0)
    requires_new_evidence: bool = True
    has_new_evidence: bool = True
    limit_code: str | None = Field(default=None, max_length=100)
    detail: str | None = Field(default=None, max_length=2000)


_DEFAULT_OPERATING_LIMITS = {
    "max_same_failure_replans": 2,
    "max_goal_replans": 5,
    "requires_new_evidence": True,
}


def recovery_limit_decision(
    connection: Any,
    *,
    project_id: str,
    task_id: str,
    attempt_id: str,
    diagnosis: FailureDiagnosis,
    assessment_id: str | None = None,
    basis_sequence: int | None = None,
) -> RecoveryLimitObservation:
    """TaskContract·GoalAuthorization 한도와 새 evidence 요구를 원장에서 계산한다.

    읽기 전용이며 실행 경로와 사용자 status 표시가 같은 결과를 쓰도록 한 곳에 둔다.
    기본 대상은 head assessment다. 사용자 재시도는 아직 없는 재시도 ID를 넘겨 기록 전 한도를
    계산하며, 그때 새 근거는 EvidenceRecord가 아니라 Core가 결속한 typed basis의 History
    순번(`basis_sequence`)으로만 판정한다.
    """

    action = diagnosis.repair_action
    if assessment_id is None:
        assessment_id = current_replan_assessment(
            connection, project_id=project_id, attempt_id=attempt_id,
            failure_fingerprint=diagnosis.failure_fingerprint,
        )
    task = connection.execute(
        "SELECT payload_json FROM task_contracts WHERE id=?", (task_id,)
    ).fetchone()
    authorization = connection.execute(
        "SELECT payload_json FROM goal_authorizations WHERE project_id=? "
        "ORDER BY revision_no DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    task_recovery = json.loads(task["payload_json"])["recovery"]
    existing_assessment = connection.execute(
        "SELECT 1 FROM recovery_assessments WHERE id=?", (assessment_id,)
    ).fetchone()
    operating = (
        dict(_DEFAULT_OPERATING_LIMITS)
        if authorization is None
        else json.loads(authorization["payload_json"])["operating_policy"]
    )
    task_attempts = connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE project_id=? AND entity_id=? "
        "AND event_type IN ('task.retry_enabled','task.execution_spec_recovery_enabled')",
        (project_id, task_id),
    ).fetchone()[0]
    same_replans = connection.execute(
        "SELECT COUNT(*) FROM recovery_assessments WHERE project_id=? "
        "AND action='subgraph_replan' "
        "AND json_extract(payload_json,'$.failure_fingerprint')=?",
        (project_id, diagnosis.failure_fingerprint),
    ).fetchone()[0]
    goal_replans = connection.execute(
        "SELECT COUNT(*) FROM recovery_assessments WHERE project_id=? "
        "AND action IN ('subgraph_replan','goal_revision')",
        (project_id,),
    ).fetchone()[0]
    last_recovery = connection.execute(
        "SELECT MAX(sequence) FROM history_events WHERE project_id=? AND ("
        "event_type='recovery.assessed' OR (entity_id=? AND event_type IN "
        "('task.retry_enabled','task.execution_spec_recovery_enabled')))",
        (project_id, task_id),
    ).fetchone()[0]
    if diagnosis.evidence_ids:
        placeholders = ",".join("?" for _ in diagnosis.evidence_ids)
        newest_evidence = connection.execute(
            "SELECT MAX(sequence) FROM history_events WHERE project_id=? "
            "AND event_type='evidence.recorded' "
            f"AND entity_id IN ({placeholders})",
            (project_id, *diagnosis.evidence_ids),
        ).fetchone()[0]
    else:
        newest_evidence = None

    retryable = (
        diagnosis.failure_class is not None
        and diagnosis.failure_class.value
        in set(task_recovery["retryable_failure_classes"])
    )
    if basis_sequence is not None:
        newest_evidence = basis_sequence
    has_new_evidence = not (
        last_recovery is not None
        and (newest_evidence is None or int(newest_evidence) <= int(last_recovery))
    )
    observation = {
        "assessment_id": assessment_id,
        "assessment_recorded": existing_assessment is not None,
        "failure_class_retryable": retryable,
        "task_recovery_count": int(task_attempts),
        "max_task_recovery": int(task_recovery["max_same_failure_replans"]),
        "same_failure_replan_count": int(same_replans),
        "max_same_failure_replans": int(operating["max_same_failure_replans"]),
        "goal_replan_count": int(goal_replans),
        "max_goal_replans": int(operating["max_goal_replans"]),
        "requires_new_evidence": bool(operating.get("requires_new_evidence", True)),
        "has_new_evidence": has_new_evidence,
    }
    # 같은 assessment의 다음 checkpoint(replan/activation)는 반복 복구가 아니다.
    if action is None or existing_assessment is not None:
        return RecoveryLimitObservation.model_validate(observation)

    limit_code = None
    detail = None
    if action in {
        RepairAction.TASK_REPAIR,
        RepairAction.EXECUTION_SPEC_REVISION,
        RepairAction.CONTINUE,
    }:
        maximum = observation["max_task_recovery"]
        if not retryable:
            limit_code = "RECOVERY_NOT_AUTHORIZED"
            detail = (
                f"TaskContract가 {diagnosis.failure_class.value} 자동 복구를 허용하지 않습니다."
            )
        elif observation["task_recovery_count"] >= maximum:
            limit_code = "SAME_FAILURE_RECOVERY_LIMIT"
            detail = f"동일 Task recovery 상한 {maximum}회에 도달했습니다."
    elif action is RepairAction.SUBGRAPH_REPLAN:
        maximum = observation["max_same_failure_replans"]
        if observation["same_failure_replan_count"] >= maximum:
            limit_code = "SAME_FAILURE_REPLAN_LIMIT"
            detail = f"동일 실패 재계획 상한 {maximum}회에 도달했습니다."
    if (
        limit_code is None
        and action in {RepairAction.SUBGRAPH_REPLAN, RepairAction.GOAL_REVISION}
        and observation["goal_replan_count"] >= observation["max_goal_replans"]
    ):
        limit_code = "GOAL_REPLAN_LIMIT"
        detail = f"Goal 전체 재계획 상한 {observation['max_goal_replans']}회에 도달했습니다."
    if limit_code is None and observation["requires_new_evidence"] and not has_new_evidence:
        limit_code = "NEW_RECOVERY_EVIDENCE_REQUIRED"
        detail = "첫 복구 이후에는 이전 checkpoint 뒤에 기록된 새 evidence가 필요합니다."
    return RecoveryLimitObservation.model_validate(
        observation | {"limit_code": limit_code, "detail": detail}
    )


def _failure_evidence_documents(
    service: Any, attempt_id: str, *, evidence_ids: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """실패 evidence 원문을 읽기 전용으로 읽는다. 실행 경로와 같은 순서를 쓴다."""

    with service.ledger.read() as connection:
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            rows = connection.execute(
                f"SELECT id,payload_json FROM evidence_records WHERE id IN ({placeholders}) "
                "ORDER BY observed_at,rowid",
                tuple(evidence_ids),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id,payload_json FROM evidence_records WHERE attempt_id=? "
                "ORDER BY observed_at,rowid",
                (attempt_id,),
            ).fetchall()
    return (
        tuple(row["id"] for row in rows),
        tuple(json.loads(row["payload_json"]) for row in rows),
    )


def current_failure_diagnosis(
    service: Any, *, project_id: str, plan_revision_id: str | None,
) -> tuple[dict[str, Any], FailureDiagnosis] | None:
    """현재 미해결 실패 하나와 그 원장 분류를 실행 경로와 같은 규칙으로 읽는다.

    status와 replan 명령이 같은 대상·실패 지문을 쓰도록 한 곳에 둔다. 읽기 전용이다.
    """

    if plan_revision_id is None:
        return None
    classifier = EvidenceFirstFailureClassifier()
    with service.ledger.read() as connection:
        failed = connection.execute(
            "SELECT t.id AS task_id, a.id AS attempt_id, a.failure_class, a.failure_detail "
            "FROM task_contracts t JOIN attempts a ON a.id = ("
            "SELECT latest.id FROM attempts latest WHERE latest.task_id = t.id "
            "AND latest.kind = 'execution' "
            "ORDER BY latest.attempt_no DESC, latest.rowid DESC LIMIT 1"
            ") WHERE t.plan_revision_id = ? AND t.status IN ('failed','blocked') "
            "AND a.failure_class IS NOT NULL "
            "ORDER BY t.position LIMIT 1",
            (plan_revision_id,),
        ).fetchone()
    if failed is not None:
        failure_class = FailureClass(failed["failure_class"])
        evidence_ids, documents = _failure_evidence_documents(service, failed["attempt_id"])
        observed = terminal_failure_diagnosis(documents, classifier=classifier)
        matched = observed is not None and observed.failure_class is failure_class
        diagnosis = FailureDiagnosis(
            failure_class=failure_class,
            repair_action=FAILURE_REPAIR_ACTIONS[failure_class],
            provider_error_code=observed.provider_error_code if matched else None,
            local_engine_code=observed.local_engine_code if matched else None,
            evidence_ids=evidence_ids,
            rationale=failed["failure_detail"] or "원장에 기록된 Attempt 실패",
            source=(
                "unclassified"
                if failure_class is FailureClass.UNCLASSIFIED
                else "direct_evidence"
            ),
            model_reported_codes=(
                () if observed is None else observed.model_reported_codes
            ),
            transient=bool(matched and observed.transient),
        )
        return (
            {
                "task_id": failed["task_id"],
                "attempt_id": failed["attempt_id"],
                "validation_result_id": None,
            },
            diagnosis,
        )
    validation_failure = service.task_validation_recovery_blocker(
        project_id, plan_revision_id=plan_revision_id
    )
    if validation_failure is None:
        return None
    evidence_ids, documents = _failure_evidence_documents(
        service,
        validation_failure["attempt_id"],
        evidence_ids=tuple(validation_failure["evidence_ids"]),
    )
    diagnosis = classifier.classify(FailureSignal(
        terminal_status="validation_failed",
        final_response=None,
        provider_payload={
            "validation_result_id": validation_failure["validation_result_id"]
        },
        evidence_ids=evidence_ids,
        evidence_documents=documents,
        failed_attempt_id=validation_failure["attempt_id"],
        failed_validation_id=validation_failure["validation_id"],
    ))
    return (
        {
            "task_id": validation_failure["task_id"],
            "attempt_id": validation_failure["attempt_id"],
            "validation_result_id": validation_failure["validation_result_id"],
        },
        diagnosis,
    )
