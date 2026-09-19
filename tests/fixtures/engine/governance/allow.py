"""governance와 무관한 기능 테스트가 실행 Task를 dispatch할 때 주입하는 통과 gate.

제품 경로는 governance 설정 없이 실행 Task를 dispatch하지 않는다. 복구·E2E harness·facade처럼 다른 기능을
검사하는 테스트는 이 gate로 그 조건만 충족하고, gate 자체의 동작은 test_engine_governance_gate.py가 검사한다.
"""
from __future__ import annotations

from typing import Any


class AllowAllGate:
    def before_execution(self, task: Any) -> None:
        return None

    def before_completion(self, task: Any) -> None:
        return None

    def close(self) -> None:
        return None


class AllowAllGovernance:
    def open_gate(self, service: Any, *, runtime: Any, roles: Any, runner: Any) -> AllowAllGate:
        return AllowAllGate()


ALLOW_ALL = AllowAllGovernance()
