from __future__ import annotations

from collections.abc import Iterable


EXPLICIT_VALIDATION_OBLIGATION_INSTRUCTIONS = (
    "필수 AC 검사 연결의 양성 기준은 하나다. 각 AC와 각 validation을 독립적으로 대조하여, "
    "AC statement 또는 validation_intent가 명시한 검사 절차·도구·phase·적용 범위를 실제 수행하는 "
    "supported scope가 하나라도 있으면 그 validation의 해당 scope를 모두 연결한다. 이 연결은 이미 "
    "계획된 검사의 완전한 기여 관계이며 AC를 통과시키는 최소 검사 집합의 선택이 아니다. 같은 명시 "
    "절차를 여러 validation ID가 실제 수행하면 각 ID를 모두 연결한다. 다른 ID의 동일 검사나 별도 "
    "실행이 이 관계를 대신하지 않는다. 먼저 validation이 명시한 등록 도구·phase의 실제 본문에서 "
    "내부 호출과 재실행을 포함한 검사 절차를 확인해 scope에 보존한다. 복합 도구의 이름이나 Plan의 "
    "요약 문장에 개별 검사 이름이 반복되지 않아도 실제 실행되는 절차의 관계는 유지한다. 같은 "
    "validation의 contradicted·unresolved scope는 별도 finding의 근거이며, 다른 supported scope의 "
    "유효한 AC 연결을 제거하지 않는다. AC가 phase나 적용 범위를 제한하면 그 제한을 따르고, "
    "제한하지 않은 명시 절차를 Task 또는 Goal 한쪽으로 임의 한정하지 않는다. Task와 Goal phase를 "
    "각각 명시하면 각 phase의 실제 scope를 모두 연결한다. 결과 내용·주제의 관련성, 같은 evidence, "
    "전역 constraint, 단순 선후관계·독립 실행 또는 같은 phase라는 사실만으로는 명시 절차의 수행이 "
    "아니므로 필수 연결하지 않는다. 보호 금지는 effect 계약으로 보존하며, 보호 자원의 검증까지 "
    "명시된 경우에만 그 자원·범위와 실제 관측 절차가 일치하는 검사를 필수 의무로 판정한다."
)


def explicit_obligation_selection_description(source_field: str) -> str:
    """출력 필드에도 공통 관계의 전수 선택·부분 scope 독립 경계를 유지한다."""
    return (
        f"AC {source_field}의 명시 검사 의무를 실제 수행하는 모든 supported scope의 완전한 목록. "
        "복합 도구 내부의 동일 절차와 여러 validation ID의 동일 절차도 각각 포함한다. 다른 scope의 "
        "결함이나 이미 선택한 다른 검사로 이 관계를 생략하지 않는다. 결과 관련성만 있거나 실제 "
        "명시 절차를 수행하지 않는 sibling 검사는 제외하며, 명시 검사 의무가 없으면 빈 목록."
    )


def merge_explicit_obligation_scope_ids(*scope_groups: Iterable[str]) -> tuple[str, ...]:
    """원문 필드별 명시 검사 의무를 입력 순서대로 합치고 중복 scope를 한 번만 보존한다."""

    merged: list[str] = []
    seen: set[str] = set()
    for group in scope_groups:
        for scope_id in group:
            if scope_id not in seen:
                seen.add(scope_id)
                merged.append(scope_id)
    return tuple(merged)
