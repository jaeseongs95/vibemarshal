from shipping_rules import BASE_FEE_WON, FREE_SHIPPING_MIN_WON, PER_KG_WON


def shipping_fee(weight_kg: int, order_total_won: int) -> int:
    """주문 한 건의 배송비(원)를 돌려준다. 계산 규칙은 shipping_rules 모듈 문서를 따른다."""
    raise NotImplementedError
