"""역할 종료를 관측하는 운영 정책과 유한한 SDK 호출 대기."""
from __future__ import annotations

import math
import threading
from concurrent.futures import Future
from typing import Callable, Literal, TypeVar

from pydantic import Field

from ..canonical import sha256_digest
from .domain import EngineModel


class RoleObservationPolicy(EngineModel):
    """모델 실행 시간과 별개인 중단 뒤 관측 한도."""

    format: Literal["flowmarshal-role-observation-v1"] = "flowmarshal-role-observation-v1"
    interrupt_observation_seconds: float = Field(default=30, ge=0, le=300, strict=True, allow_inf_nan=False)
    rpc_timeout_seconds: float = Field(default=5, gt=0, le=5, strict=True, allow_inf_nan=False)

    @property
    def policy_digest(self) -> str:
        return sha256_digest(self)


_Result = TypeVar("_Result")


def bounded_observation_call(
    operation: Callable[[], _Result], *, timeout_seconds: float, operation_name: str,
) -> _Result:
    """응답 없는 SDK RPC도 한도 내 반환하며 같은 효과를 재전송하지 않는다.

    대기 만료는 원격 요청 취소가 아니다. 호출자는 미확정 결과와 이미 전송한
    intent를 보존해야 한다. daemon은 정지한 RPC 때문에 프로세스 종료를 막지 않는다.
    """
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise TimeoutError(f"{operation_name}: 관측 대기 기한이 만료됐습니다.")
    result: Future[_Result] = Future()

    def invoke() -> None:
        try:
            result.set_result(operation())
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=invoke, name=f"flowmarshal-observe-{operation_name}", daemon=True).start()
    try:
        return result.result(timeout=timeout_seconds)
    except TimeoutError as error:
        if result.done():
            raise
        raise TimeoutError(f"{operation_name}: {timeout_seconds:g}초 안에 RPC 응답을 받지 못했습니다.") from error
