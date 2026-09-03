"""FlowMarshal의 새 오케스트레이션 제품 Core.

이 패키지의 공개 표면은 계획 활성화, 작업 선택, 실행 추적과 검증 결과
기록에 집중한다. Gate 0B의 HMAC 승인과 파일별 AccessGrant 호환 API는
``flowmarshal.compat.gate0b``에 격리돼 있으며 이 패키지의 dependency가 아니다.
"""

from .domain import (
    CORE_SCHEMA_ID,
    CORE_SCHEMA_REVISION,
    ActivationSource,
    Assignment,
    AttemptReservation,
    AttemptStatus,
    CoreDomainError,
    ClarificationRequest,
    EffectiveWorkItemStatus,
    PlanDraft,
    PlanRevisionStatus,
    ProjectDefinition,
    RequirementCoverage,
    RequirementDisposition,
    RuntimeBindingReceipt,
    RuntimeIntentKind,
    RuntimeIntentStatus,
    StoredWorkItemStatus,
    ValidationDefinition,
    ValidationResultInput,
    ValidationStatus,
    WorkItemDefinition,
)
from .ledger import SQLiteCoreLedger
from .service import ActivationResult, FlowMarshalCore

__all__ = [
    "CORE_SCHEMA_ID",
    "CORE_SCHEMA_REVISION",
    "ActivationResult",
    "ActivationSource",
    "Assignment",
    "AttemptReservation",
    "AttemptStatus",
    "CoreDomainError",
    "ClarificationRequest",
    "EffectiveWorkItemStatus",
    "FlowMarshalCore",
    "PlanDraft",
    "PlanRevisionStatus",
    "ProjectDefinition",
    "RequirementCoverage",
    "RequirementDisposition",
    "RuntimeBindingReceipt",
    "RuntimeIntentKind",
    "RuntimeIntentStatus",
    "SQLiteCoreLedger",
    "StoredWorkItemStatus",
    "ValidationDefinition",
    "ValidationResultInput",
    "ValidationStatus",
    "WorkItemDefinition",
]
