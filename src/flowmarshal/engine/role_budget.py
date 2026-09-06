"""Core가 정한 한정 수정 경로의 호출 비용 분류를 전달한다."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from .domain import BudgetStage


_STAGE: ContextVar[BudgetStage | None] = ContextVar("flowmarshal_role_budget_stage", default=None)


def current_role_budget_stage() -> BudgetStage | None:
    return _STAGE.get()


@contextmanager
def replan_budget() -> Iterator[None]:
    """수정과 필수 독립 재검토가 같은 재계획 예산을 사용한다."""
    token = _STAGE.set(BudgetStage.REPLAN)
    try:
        yield
    finally:
        _STAGE.reset(token)
