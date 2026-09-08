"""직접 evidence를 우선하는 실행 실패 분류와 자동 복구 입력 생성.

모델의 의미 추론은 명시 error code와 transport/effect 관측으로 결정할 수 없는
경우에만 후속 진단 경계에서 사용한다. 이 모듈의 분류기는 근거가 부족한 failed
terminal을 구현 오류로 추정하지 않는다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import Field

from ..canonical import sha256_digest
from .domain import EngineModel, FailureClass, RepairAction


_CODE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]{2,99})\b")
_CODE_KEYS = frozenset({"blocker_code", "code", "error_code", "failure_code"})


class FailureDiagnosis(EngineModel):
    """분류 결과와 그 결정을 직접 지지하는 evidence 결속."""

    failure_class: FailureClass | None = None
    repair_action: RepairAction | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,99}$")
    evidence_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=5000)
    source: str = Field(pattern=r"^(explicit_code|effect|transport|direct_evidence|unclassified)$")

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

    _ACTION = {
        FailureClass.IMPLEMENTATION: RepairAction.TASK_REPAIR,
        FailureClass.CONTEXT: RepairAction.EXECUTION_SPEC_REVISION,
        FailureClass.TASK_CONTRACT: RepairAction.SUBGRAPH_REPLAN,
        FailureClass.DEPENDENCY: RepairAction.SUBGRAPH_REPLAN,
        FailureClass.ENVIRONMENT: RepairAction.CONTINUE,
        FailureClass.REQUIREMENT_CHANGE: RepairAction.GOAL_REVISION,
        FailureClass.EXTERNAL_UNKNOWN: RepairAction.WAIT_EXTERNAL,
    }

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

    @classmethod
    def _explicit_codes(cls, signal: FailureSignal) -> tuple[str, ...]:
        codes: list[str] = []
        for key, value in cls._walk(signal.provider_payload):
            if key in _CODE_KEYS and isinstance(value, str):
                match = _CODE_PATTERN.search(value)
                if match:
                    codes.append(match.group(1))
        # 최종 응답에서는 선두의 ``CODE: detail`` 형식만 명시 코드로 인정한다.
        response = (signal.final_response or "").strip()
        match = re.match(r"^([A-Z][A-Z0-9_]{2,99})(?:\s*[:;-]|$)", response)
        if match:
            codes.append(match.group(1))
        for document in signal.evidence_documents:
            for key, value in cls._walk(document):
                if key in _CODE_KEYS and isinstance(value, str):
                    match = _CODE_PATTERN.search(value)
                    if match:
                        codes.append(match.group(1))
        return tuple(dict.fromkeys(codes))

    @classmethod
    def classify(cls, signal: FailureSignal) -> FailureDiagnosis:
        codes = cls._explicit_codes(signal)
        for code in codes:
            failure_class = cls._CODE_CLASS.get(code)
            if failure_class is None and code.startswith("MODEL_LOCK_"):
                failure_class = FailureClass.ENVIRONMENT
            if failure_class is not None:
                return FailureDiagnosis(
                    failure_class=failure_class,
                    repair_action=cls._ACTION[failure_class],
                    error_code=code,
                    evidence_ids=signal.evidence_ids,
                    rationale=f"직접 관측된 error code {code}를 우선 적용했습니다.",
                    source="explicit_code",
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
                )

        # 한국어 fixture의 명시적인 구현 실패 표기는 일반 failed terminal과 구분한다.
        if (signal.final_response or "").strip() == "구현 실패":
            return FailureDiagnosis(
                failure_class=FailureClass.IMPLEMENTATION,
                repair_action=RepairAction.TASK_REPAIR,
                evidence_ids=signal.evidence_ids,
                rationale="최종 응답이 명시적으로 구현 실패를 보고했습니다.",
                source="direct_evidence",
            )

        return FailureDiagnosis(
            evidence_ids=signal.evidence_ids,
            rationale=(
                "명시 오류코드, transport/effect 상태 또는 직접 구현 evidence가 없어 "
                "failed terminal만으로 원인을 추정하지 않았습니다."
            ),
            source="unclassified",
        )
