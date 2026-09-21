"""사내 배송비 규칙.

- 무게가 0kg 이하이면 ValueError를 낸다.
- 주문 금액이 FREE_SHIPPING_MIN_WON 이상이면 배송비는 0원이다.
- 그 밖에는 BASE_FEE_WON에 무게 1kg마다 PER_KG_WON을 더한다.
"""

BASE_FEE_WON = 3000
PER_KG_WON = 700
FREE_SHIPPING_MIN_WON = 50000
