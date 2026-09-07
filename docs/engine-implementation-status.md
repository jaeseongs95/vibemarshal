# Engine 재설계 구현과 qualification 현황

현재 제품 1.0은 **미완료 / NO-GO**다. 이유는 새 승인 계약의 구현·필수 검증·독립 감사 미완료이며, usage 누락 자체나 비교 성능 미관측을 전역 차단 이유로 사용하지 않는다. FM-01은 문서 계약을 정합화했으며 후속 구현·검증은 **planned**다.

권위 설계는 [승인 12항목·필수 검증](redesign-1.0-contract.md), [제품 설계](orchestration-redesign.md), [cutover ADR](engine-cutover-adr.md)이다. [로드맵](pre-1.0-roadmap.md)은 태스크별 책임, [현재 인계](pre-1.0-handoff.md)는 실행 대상과 원장 조회 위치를 제공한다. 정확한 진행 상태·검사 판정은 구현 조율 원장에서 읽는다.

| 계약 | 상태와 책임 |
|---|---|
| 기존 Core·revision·DAG·binding·evidence·validation | 재사용 대상. 과거 source별 검증을 현재 PASS로 승격하지 않음 |
| 실행/usage 분리·schema 4 reader | planned, FM-02 및 FM-10/12 |
| GoalAuthorization·자동 Plan activation | planned, FM-03/05 |
| supervisor·짧은 tick·전 역할 checkpoint | planned, FM-04/08 |
| 효과 직전 안전·복구·Context·Planning | planned, FM-05/06/07 |
| 응용 CLI·조율·Engine-only 패키징 | planned, FM-08/09/10 |
| harness·필수 실제/결정적/설치 검증·감사·전환 | planned, FM-11~FM-16 |

기본 provider는 qualification된 v1을 유지한다. v2의 별도 채택 평가와 R3.1 성능 비교는 모든 제품 실행의 필수 선행조건이 아니다. 1.0 필수 Gate의 정확한 기준·책임은 [V01·V02](redesign-1.0-contract.md)에 있다. 문서 변경으로 실제 새 검사를 수행했다고 보고하지 않는다.

[승인 이전 구현 현황 원문](engine-implementation-status-before-redesign-1.0.md)은 바이트 그대로 보존했다. 당시 schema 3 구현, v29·1,056 tests, BLOCKED_USAGE_UNKNOWN, 과거 Plan 활성화와 실측 수치는 역사 사실이다. 원본 receipt·실행 기록·R3.1 결과를 수정하지 않으며 새 제품의 완료 판정으로 재사용하지 않는다.
