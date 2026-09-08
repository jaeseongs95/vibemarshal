# R3.1 비교 성능 보고 — 비차단 후속

현재 1.0 필수 판정은 [승인 계약 V01·V02](redesign-1.0-contract.md)와 [cutover ADR](engine-cutover-adr.md)을 따른다. R3.1 대비 token/speed, performance36, 비교 lifecycle 최적화는 별도 비차단 보고이며 비교 결과 미관측이나 목표 미달만으로 제품 실행·1.0 전환을 차단하지 않는다.

[승인 이전 Release Performance Floor 계약](performance-release-floor-before-redesign-1.0.md)을 원문 bytes 그대로 보존했다. 그 안의 36 cell·18 pair, 여섯 하한·최적화 scorecard, v3/v4 schema와 raw 결과는 당시의 비교 계약이다. 과거 `release_floor_passed`·`cutover_eligible` 필드를 현재 릴리스 권위로 사용하지 않는다. 기존 계산/reader/fixture 변경은 이 문서 작업에 포함하지 않는다.

비교 보고를 수행하면 동일 입력·source·fixture·정책·model lock과 실제 receipt를 결속한다. 미관측 token·latency·분모는 null/NOT_OBSERVED로 남기고 0·예약량·추정치로 채우지 않는다. 실제 token 기록은 유효한 근거지만 API 가격·계정 사용률 %로 구독 한도 차감량이나 정확한 작업 요금을 계산하지 않는다. 원본 R1~R3.1 source·campaign·fixture·결과를 수정하거나 재실행해 GO로 바꾸지 않는다.
