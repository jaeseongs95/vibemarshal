from __future__ import annotations

from typing import Any, Mapping


PROVIDER_RAW_MODEL_OBSERVATION_SOURCE = "provider_raw_response"
# Claude CLI는 stream 응답에 effort를 싣지 않고 자기 session 기록 파일에만 남긴다.
# stream 원문과 출처를 구분해 그 turn의 assistant 줄이 명시한 한 쌍만 관측값으로 쓴다.
CLAUDE_SESSION_TRANSCRIPT_MODEL_OBSERVATION_SOURCE = "claude_session_transcript"
MODEL_OBSERVATION_SOURCES = frozenset(
    {PROVIDER_RAW_MODEL_OBSERVATION_SOURCE, CLAUDE_SESSION_TRANSCRIPT_MODEL_OBSERVATION_SOURCE}
)


def authoritative_model_observation(
    payload: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    """provider 원문(또는 Claude session 기록)이 model/effort를 모두 제공했을 때만
    권위 관측으로 반환한다.

    요청값 echo, provenance marker 없는 투영, 부분 관측과 늦은 회계 재관측은
    진단 자료일 뿐 실제 적용값의 근거가 아니다. 늦은 재관측 경로는 새 payload가
    아니라 원래 receipt 투영을 아래 receipt helper로 다시 검증해야 한다.
    """

    if not isinstance(payload, Mapping):
        return None, None
    if payload.get("model_observation_source") not in MODEL_OBSERVATION_SOURCES:
        return None, None
    model = payload.get("observed_model")
    effort = payload.get("observed_effort")
    if not isinstance(model, str) or not model.strip():
        return None, None
    if not isinstance(effort, str) or not effort.strip():
        return None, None
    return model, effort


def model_observation_source(payload: Mapping[str, Any] | None) -> str | None:
    """권위 관측이 성립할 때 그 출처를, 아니면 None을 돌려준다."""

    if authoritative_model_observation(payload) == (None, None):
        return None
    return payload["model_observation_source"]  # type: ignore[index]


def authoritative_receipt_model_observation(
    *,
    observed_model: str | None,
    observed_effort: str | None,
    binding_provenance: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    """저장된 receipt 투영을 legacy alias 신뢰 없이 다시 검증한다."""

    return authoritative_model_observation(
        {
            "model_observation_source": (
                None if binding_provenance is None else binding_provenance.get("observed")
            ),
            "observed_model": observed_model,
            "observed_effort": observed_effort,
        }
    )
