# ADR: FlowMarshal Engine 분리와 1.0 cutover

- 상태: Accepted
- 대상: FlowMarshal 새 권위 엔진

## Context

기존 R1~R3.1 prototype에는 유용한 Runtime·원장·Planner 실험 자산이 있지만, 서로 다른 승인 모델과 도메인 객체가 누적됐다. R3.1 전체 역할 campaign도 최종 `FAIL`이므로 기존 namespace와 DB를 계속 확장하면 새 권위 구조와 감사 기준선이 섞인다.

## Decision

1. 새 코드는 `flowmarshal.engine` namespace에서 구현한다.
2. 새 원장은 SQLite application ID `0x464D4531` (`FME1`)과 기본 파일명 `flowmarshal-engine.sqlite3`를 사용한다.
3. 새 artifact root는 기본적으로 `.flowmarshal-engine/artifacts`다.
4. 기존 R1~R3.1 source와 artifact는 legacy/prototype 기준선으로 동결한다.
5. 새 Engine은 기존 `flowmarshal.core`와 `flowmarshal.planning` 도메인을 import하지 않는다. Codex transport만 `CodexRuntimePort` adapter 뒤에서 재사용한다.
6. prototype DB를 자동 또는 제자리 migration하지 않는다. 필요성이 확인된 뒤 별도 검증을 거친 일회성 import만 허용한다.
7. 개발 CLI와 package는 `flowmarshal-engine` 이름을 사용한다.
8. `flowmarshal` package·기본 CLI 승격은 네 qualification 범위와 token/latency Gate가 모두 통과한 뒤 수행한다.

## Consequences

- 과거 artifact digest와 판정을 그대로 보존할 수 있다.
- 서로 다른 schema를 실수로 같은 DB에서 여는 일을 application ID 검사로 차단한다.
- 단기적으로 prototype CLI와 Engine CLI가 함께 존재하지만, 양쪽의 권위 상태는 공유하지 않는다.
- 1.0 이전 사용자는 `flowmarshal-engine`을 명시적으로 실행해야 한다.
- 실제 cutover가 지연되더라도 미검증 엔진을 `flowmarshal` 1.0으로 오인시키지 않는다.

## Cutover 조건

다음이 모두 독립적인 receipt와 digest를 가져야 한다.

- deterministic schema·DAG·ledger Gate
- 실제 역할 기반 R3.1 회귀 fixture Gate
- 전체 Skeleton-to-selection 실제 모델 pipeline Gate
- 실제 프로젝트 activation-to-recovery E2E Gate
- 같은 입력의 R3.1 대비 token/latency Gate

하나라도 누락되거나 실패하면 cutover는 `NO-GO`다. 합성 smoke나 일부 fixture 통과를 전체 qualification으로 승격하지 않는다.

