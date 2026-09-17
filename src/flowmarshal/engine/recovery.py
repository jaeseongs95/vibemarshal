"""직접 evidence를 우선하는 실행 실패 분류와 자동 복구 입력 생성.

모델의 의미 추론은 명시 error code와 transport/effect 관측으로 결정할 수 없는
경우에만 후속 진단 경계에서 사용한다. 이 모듈의 분류기는 근거가 부족한 failed
terminal을 구현 오류로 추정하지 않는다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from pydantic import Field

from ..canonical import sha256_digest
from .domain import EngineModel, FailureClass, RepairAction


_CODE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]{2,99})\b")
_CODE_KEYS = frozenset({"blocker_code", "code", "error_code", "failure_code"})

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

        # 직접 test/diff evidence는 의미 추측 없이 그 관측 필드로만 판정한다.
        for document in signal.evidence_documents:
            kind = str(document.get("kind", "")).casefold()
            observation = document.get("observation", document)
            if isinstance(observation, str):
                try:
                    observation = json.loads(observation)
                except ValueError:
                    observation = {"text": observation}
            rendered = json.dumps(observation, ensure_ascii=False).casefold()
            if kind in {"test", "diff", "file"} and (
                '"passed": false' in rendered
                or '"exit_code": 1' in rendered
                or "target contract violation" in rendered
                or "구현 실패" in rendered
            ):
                return FailureDiagnosis(
                    failure_class=FailureClass.IMPLEMENTATION,
                    repair_action=RepairAction.TASK_REPAIR,
                    evidence_ids=signal.evidence_ids,
                    rationale="Task에 직접 결속된 test/file/diff 실패 evidence가 구현 결함을 입증합니다.",
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
