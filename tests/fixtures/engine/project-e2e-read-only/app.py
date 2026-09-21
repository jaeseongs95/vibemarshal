def add(left: int, right: int) -> int:
    """두 정수의 합을 반환한다."""

    return left + right


def total(values: list[int]) -> int:
    """정수 목록의 합을 add로 누적한다."""

    result = 0
    for value in values:
        result = add(result, value)
    return result
