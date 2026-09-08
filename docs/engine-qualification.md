# FlowMarshal Engine 1.0 qualification 계약

상태: **새 qualification planned / 미실행**. 필수 검증은 [승인 계약 V01·V02](redesign-1.0-contract.md)의 전체 책임과 수치 기준이다. FM-11에서 harness·provenance를 연결하고 FM-12 결정적/호환/설치, FM-13 실제 역할48/Planning18, FM-14 실제 요청 E2E/스케줄러, FM-15 독립 감사를 수행한다. 이 문서만으로 새 evaluator나 CLI가 구현되었다고 간주하지 않는다.

- 역할48: Plan8+Goal8 × 3 seed, 전 cell 완료; required finding recall ≥90%, precision ≥85%, critical false admission·clean false block·schema failure·critical admission seed instability 각각 0.
- Planning18: 6 fixture × 3 seed, 전 cell 완료; 정상4종은 선택, 실제 정보부족2종은 의미 있는 blocking question. 일반 오류는 정상 blocked가 아니다. 준비 포함 역할호출 ≤14, 후보 version ≤5, 선택 후보 deterministic finding 0.
- E2E: 실제 요청·Goal 정규화/독립 review·Plan 선택·한 번의 승인·Task 실행·독립 검사·최종 결과를 연결한다. 다중 DAG/replan, read_only 완전 응답/무변경, 로컬 추가 context, usage missing/late, 재시작, 생성 응답 유실/강제종료/timeout, reserve/start stale, partial-write resume, 금지효과/범위확장/cancel/실행 중 replan 보호와 설치 책임 전부가 필수다.
- schema/DAG/원장/정책/검사와 영향 회귀·schema 3 read-only/schema 4 신규 DB·깨끗한 non-editable 설치를 확인한다. 실제 provider·synthetic stub·fault injection·과거 evidence를 책임별로 구분한다.

source/fixture/prompt/schema/lock/evaluator digest를 실행 전에 고정한다. 변경된 공통 계약의 최초 실제 역할·Planning 평가는 새로 수행한다. 과거 51개/1,056개 검사는 새로운 실행으로 세지 않는다. 판정 후 oracle·threshold를 낮추거나 모든 finding 100%라는 새 합격선을 추가하지 않는다. 고정된 E2E case 수나 미리 작성한 Goal/Plan/rating은 책임 충족을 대신하지 못한다.

종료와 유효 결과가 확인되면 usage 누락만으로 후속 실행을 막지 않는다. null은 null로 남기고 외부 효과 미확정은 기존 binding을 먼저 관측한다. run_once는 job 예약/시작 또는 관측 소비 뒤 신속히 반환해야 하며 전체 모델 turn 대기는 supervisor가 활성 job 동안 관리한다.

R3.1 token/speed·performance36·비교 lifecycle은 비차단 별도 보고다. v2 static11/qualification13은 v2 자체 채택 조건이며 기본 v1 제품의 선행조건이 아니다. 모든 필수 기능·안전·역할·Planning·E2E·설치·독립 감사 후에만 로컬 1.0 전환을 한다.

[이전 qualification 실행기 계약 원문](engine-qualification-before-redesign-1.0.md)은 바이트 그대로 보존했다. 그 문서의 schema 3 동작·예산·긴 run_once 대기·성능 cutover 명령은 역사/호환 조사 자료다. 현재 실행 순서나 새 CLI 지원의 근거가 아니며 원시 결과·fixture·receipt는 수정하지 않는다.
