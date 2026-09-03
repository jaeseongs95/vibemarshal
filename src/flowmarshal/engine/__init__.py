"""FlowMarshal의 새 권위 기반 workflow engine.

기존 R1~R3.1 prototype은 감사 기준선으로 유지한다. 이 패키지는 기존
``flowmarshal.core`` 또는 ``flowmarshal.planning`` 도메인 타입을 import하지 않는다.
"""

from .domain import (
    ENGINE_SCHEMA_VERSION,
    GoalContractRevision,
    PlanContractRevision,
    PlanSkeletonCandidate,
    ProjectProfileRevision,
    StateSnapshot,
    TaskExecutionSpecRevision,
)

__all__ = [
    "ENGINE_SCHEMA_VERSION",
    "GoalContractRevision",
    "PlanContractRevision",
    "PlanSkeletonCandidate",
    "ProjectProfileRevision",
    "StateSnapshot",
    "TaskExecutionSpecRevision",
]
