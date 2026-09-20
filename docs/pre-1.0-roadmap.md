# FlowMarshal 1.0 승인 로드맵

현재 상태: **재설계 계약 반영 / 제품 1.0 미완료**. FM-01 문서 변경은 후속 구현·qualification 완료가 아니다. [승인 계약](redesign-1.0-contract.md)의 12항목과 필수 검증을 따른다. 가변 task 상태·정확한 의존성·배정·필수 검사 판정의 권위는 구현 조율 SQLite 원장이며 아래 표는 완료 목록이 아니다.

## 구현 책임과 순서

| 구간 | 태스크 | 산출물·종료 책임 |
|---|---|---|
| 조율 기반·계약 | FM-00, FM-01 | 조율 원장 기반과 일관된 문서·지침, 요구사항 연결표 |
| 실행·승인 | FM-02, FM-03 | 실행/usage 분리·schema 4, GoalAuthorization 및 내부 Plan 자동 활성화 |
| 비동기 운영·안전 | FM-04, FM-05 | RuntimeJobSupervisor·짧은 tick, 효과 직전 재검증·재시작·Attempt 보호 |
| 복구·계획 | FM-06, FM-07 | 근거 기반 복구·로컬 Context 해결, canonical dedupe·CommitHorizon·ProjectMap 정합성 |
| 제품 연결 | FM-08, FM-10 | EngineApplication/사용자 CLI, 최종 package identity·사용자 config bootstrap |
| 개발 조율 보조 | FM-09 | release evidence 수집을 돕는 delivery 도구; 제품 critical path 밖 |
| 검증 도구 | FM-11 | 필수 책임과 provenance를 보존하는 qualification harness |
| 독립 검증 | FM-12, FM-13, FM-14 | 결정적·호환·설치, 실제 역할48/Planning18, 실제 요청 E2E·스케줄러 |
| 최종 감사·전환 | FM-15, FM-16 | 필수 근거 독립 감사 후 main의 검증된 변경 확인·1.0 전환과 조율 종료 |

revision 5의 제품 실행 순서는 FM-00 → FM-01 → FM-02 → FM-03 → FM-04 → FM-05 → FM-06 → FM-07 → FM-08 → FM-10 → FM-11 → FM-12 → release freeze → live canary와 대표 effect-unknown fault다. canary 통과 뒤 Role 48·Planning 18·독립 E2E lane을 병렬 실행하고 aggregate → 두 독립 최종 감사 → FM-16 동일 wheel 로컬 활성화·postverify로 연결한다. shard는 공통 immutable 입력과 각자 배정된 fixture slice를 읽고 별도 artifact root에 기록하며 sibling 결과를 소비하지 않는다. FM-09 개발 조율과 실제 Codex 예약 연동은 비차단 보조 검사다. 실제 ready 선택은 등록된 dependency·order_index로만 결정하며 이 문서에서 다음 앱 작업을 생성하거나 원장의 완료 상태를 변경하지 않는다.

FM-02~FM-16 계약은 이 문서에서 **planned**로 유지한다. 이는 코드 부재나 다른 태스크 상태를 판정한 결과가 아니다. 각 태스크의 완료는 실제 source/계약/검증 입력 digest·receipt·필수 검사 evidence를 확인한 원장 판정이 필요하다. 과거 51개 재검증과 1,056개 결과는 시점·대상이 다른 provenance이며 이번 새 실행이 아니다.

권위 통합 대상은 `D:/codex/flowmarshal`의 `main` 브랜치다. 단일 작성자와 비중첩 소유권이 명시된 목적별 worktree·브랜치에서 개발·검증하고 통과한 커밋만 `main`에 순차 통합할 수 있다. detached HEAD는 읽기·qualification 전용이며 커밋하지 않는다. FM-16은 감사 후 1.0 전환 책임이며, 앞선 작업의 분리 개발과 main 통합을 지연시키는 조건이 아니다. 정확한 입력 출처와 보존 범위는 [인계](pre-1.0-handoff.md)를 따른다.

## 1.0 수용 기준

[V01의 수치 기준과 V02의 E2E 책임 전체](redesign-1.0-contract.md)를 필수로 연결한다. 실제 역할48과 Planning18은 전 cell 완료가 필요하고 recall 90%를 모든 finding 100%라는 다른 합격선으로 바꾸지 않는다. E2E는 실제 요청·Goal 독립 review·Plan 선택·한 번의 승인부터 최종 결과까지 연결하며 harness의 미리 작성된 Goal/Plan/rating은 대체 근거가 아니다. 실제 provider·stub·fault injection·과거 evidence를 구분한다.

사용량 누락만으로 후속 실행을 차단하지 않는 새 계약과, 미확정 외부 효과를 기존 binding에서 먼저 관측하는 안전 책임은 별개다. 구 구현을 그대로 실행하면 새 계약을 충족한다고 가정하지 않는다. 구현·검증을 먼저 완료한다. 필수 기능·안전·역할·Planning·E2E·설치·독립 감사 전에는 1.0 전환을 하지 않는다.

## 비차단 후속

R3.1 token/speed·performance36·비교 lifecycle 최적화는 별도 비차단 보고다. GUI·Localizer/번역 최적화·MCTS/광범위 graph·동일 프로젝트 병렬·remote/multiOS hardening은 이후 범위다. 활성 job 동안의 supervisor는 1.0 필수이며 GUI/상주 daemon 후속 범위에 미루지 않는다. 필수 Gate 전 `origin/main` push와 공개 배포·외부 메시지·삭제·인증 변경은 승인 범위가 아니다. 최종 Gate 통과 뒤 검증된 `main`의 `origin/main` push만 적용 중인 Git 지침에 따라 수행한다.

## 역사 기록

[승인 이전 로드맵 원문](pre-1.0-roadmap-before-redesign-1.0.md)은 바이트 그대로 보존했다. 그 안의 현재/다음 실행, BLOCKED_USAGE_UNKNOWN, exact Plan 수동 승인과 performance36 필수 전환 순서는 당시 계약이다. 현재 실행 지시로 사용하지 않는다. [이전 인계 원문](pre-1.0-handoff-before-redesign-1.0.md), [반복 검증 기록](pre-1.0-iterative-validation.md), [R3.1 동결 근거](r31-frozen-baseline.md)의 수치·실패·receipt를 새 PASS로 바꾸지 않는다.
