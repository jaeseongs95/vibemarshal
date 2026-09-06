# FlowMarshal 문서 지도

- [현재 구현 현황](engine-implementation-status.md): 현재 상태의 단일 진입점
- [승인된 1.0 선행 로드맵](pre-1.0-roadmap.md): 보고·완료 경로·예산·모델 복구와 후속 기능 구분
- [현재 작업 인계](pre-1.0-handoff.md): 이번 변경의 검증 결과와 재개 조건

- [검사 계약 구조 개선 진행 기록](inspection-recovery-progress.md): 고정 검증 환경, 독립 11사례 기준선, 참조 계약 개선과 실제 Goal 경로의 공통 완료 조건
- [길 찾기 알고리즘과 현재 개발 방향의 비교](inspection-search-direction-review.md): 다른 대화의 과거 근거와 최신 결과 구분, 상세 Plan 실패 피드백 부재의 4/14 호출 재현과 우선순위 재검토
- [상세 Plan 실패 피드백 구현과 검증](planning-feedback-loop.md): 제한된 수정·재검토, 판단 충돌 보존, 예산·원장 계보와 실제 계획 준비 결과
- [Plan inspection v1 독립 11사례 기준선](inspection-v1-static11-baseline.md): 고정 환경에서 전수 관측한 7 PASS·4 FAIL 분포와 실측 usage
- [Plan inspection v2 독립 11사례 기준선](inspection-v2-static11-baseline.md): 첫 완전 v2 실행의 1 PASS·10 FAIL 분포, 기계 장부 실패와 의미 실패 분리, v1 대비 실측 usage
- [Plan inspection v2 2차 구조 보정 부분 실행](inspection-v2r2-partial-baseline.md): 7개 schema/compiler 통과 뒤 timeout으로 중단된 실행, 의미 실패군과 prompt/catalog 구조 원인
- [Plan inspection v2 3차 구조 보정 독립 11사례 기준선](inspection-v2r3-static11-baseline.md): 11/11 형식 통과, 4 PASS·7 관계 의미 FAIL과 scope/전체 행렬 중복 판단 원인
- [Plan inspection v2 4차 scope 양의 연결 계약](inspection-v2r4-scope-link-contract.md): 커밋 `12e496a`의 역사적 계약과 결정적 검증
- [Plan inspection v2 4차 부분 실행 기준선](inspection-v2r4-partial-baseline.md): clean의 scope 확장·3개 과잉 연결과 4번째 사례 timeout을 보존한 실행 결과
- [Plan inspection v2 5차 희소 양의 링크 계약](inspection-v2r5-sparse-link-contract.md): scope 능력과 AC 요구 관계를 분리하고 전체 음의 행을 adapter에서 파생하는 현재 개발 계약
- [Plan inspection v2 5차 독립 11사례 기준선](inspection-v2r5-static11-baseline.md): 9 PASS·2 FAIL, scope 축소와 남은 target arity·독립 Goal Test 구성 관계 실패
- [Plan inspection v2 6차 finding target catalog 계약](inspection-v2r6-target-catalog-contract.md): 복합 target 참조 조립을 adapter catalog로 이전하고 독립 validation 단계의 열거 책임 연결 규칙을 명시한 당시 개발 계약
- [Plan inspection v2 6차 부분 실행 기준선](inspection-v2r6-partial-baseline.md): clean·wrong-goal PASS, bad 관계 과잉과 combined timeout을 원 intent·usage 경계와 함께 보존한 결과
- [Plan inspection v2 7차 AC scope 선택 계약](inspection-v2r7-ac-scope-contract.md): AC별 실제 검사 scope만 모델이 선택하고 validation 소유 join·전체 행렬을 adapter로 이전한 계약 이력
- [Plan inspection v2 7차 부분 실행 기준선](inspection-v2r7-partial-baseline.md): 첫 3사례 PASS 뒤 프로세스 소실로 남은 combined unknown과 7사례 미실행을 보존한 결과
- [저장형 진단 실행 준비](inspection-durable-runtime.md): 저장형 역할 thread 결속과 포그라운드 세션 밖 실행을 위한 운영 보완
- [Reviewer 참조·scope 축소와 저장형 복구 경계 인계](inspection-v2r8-boundary-handoff.md): 8차 11사례의 9 PASS·2 의미 FAIL, 같은 AC-004 누락 원인군, token·latency와 종료 후 저장 조회 검증
- [AC 원문별 검사 의무 분리](inspection-v2r9-source-requirements.md): 원문별 직접 선택·adapter 합집합 계약, 677개 Gate와 실제 static 11/11 PASS·사용량 비교
- [S06 재진입 기록](inspection-s06-reentry.md): 첫 clean의 Task/Worker 산출물 의미 오판·12 NOT_RUN, 저장 응답과 실패 근거 보존, 실제 Goal 완료까지 남은 경계
- [Task 산출물 책임의 입력 설명](inspection-v2r10-task-result-context.md): Task 필드 의미를 v2 생성·검토 요청에 결속하고 실제 순서 충돌의 직접 판단을 보존하는 다음 검증 계약
- [Inspection Provider v2 계약](inspection-provider-v2.md): 모델의 직접 의미 판단과 adapter의 결정적 참조 전개 경계, 호환성·평가·승격 조건

## 현재 권위 문서

- [전면 재설계 권위 문서](orchestration-redesign.md): `flowmarshal.engine`의 제품 목적, 권위 모델, 승인·lazy expansion 경계, 실행·복구와 cutover Gate
- [Engine cutover ADR](engine-cutover-adr.md): legacy 동결, 별도 namespace·DB, migration과 1.0 승격 결정
- [입력 자료와 실행 범위 정책](file-access-policy.md): 프로젝트·`AGENTS.md`·등록 참고자료·localhost를 정상 입력으로 다루는 정책
- [저장소 작업 지침](../AGENTS.md): 구현 시 지켜야 할 안정된 불변조건
- [Engine 1.0 qualification](engine-qualification.md): 네 실행 scope, immutable checkpoint, benchmark와 cutover 절차
- [Engine 실행 구현 현황](engine-implementation-status.md): 실제 실행 결과, NO-GO 근거와 남은 qualification 범위
- [근거 우선 strict schema 순서 보정](r-s06-evidence-first-schema-order-fix-handoff.md): 선언 property 순서의 canonical 왕복 보존, 근거 우선 Reviewer envelope와 R25 의미 실패 회귀
- [참조 결속 규격 설명·진단 보정](r-s06-scope-binding-spec-diagnostics-handoff.md): mechanism→scope→AC 추적 규칙, R27 원본 거부·boolean 오류 보존 회귀와 fresh 결정론 Gate 5/5
- [R-S06-28 Sol/high 제한 실제 검증](r-s06-28-sol-high-limited-validation-handoff.md): 새 결정론 Gate 5/5, 첫 clean 구조 PASS·AC 27/28 의미 FAIL, 이후 12사례 NOT_RUN·1.0 NO-GO
- [R-S06-29 Sol/xhigh 제한 실제 검증 후보](r-s06-29-sol-xhigh-limited-validation-handoff.md): Sol/high 탈락을 보존하고 general Reviewer effort만 올린 미검증 후보의 절대 입력·v2 결속과 다음 fresh 검증 조건
- [R-S06-29 Sol/xhigh 제한 실제 검증 결과](r-s06-29-sol-xhigh-actual-validation-handoff.md): 새 Gate 5/5, 사례 PASS 1·FAIL 1·NOT_RUN 11, 1.0 NO-GO

- [Finding evidence 참조 결속 보정](r-s06-finding-evidence-binding-diagnostics-handoff.md): link별 조건부 원본 ref 결속, R29 원문 거부·진단·receipt 회귀와 R30 fresh 제한 검증 조건
- [R-S06-30 Sol/xhigh 제한 실제 검증 결과](r-s06-30-sol-xhigh-actual-validation-handoff.md): 새 Gate 5/5, 사례 PASS 1·FAIL 1·NOT_RUN 11, 1.0 NO-GO

- [R-S06-30 scope→finding link 참조 결속 보정](r-s06-30-scope-finding-binding-handoff.md): citation ID 포함관계 설명·정렬된 진단·R30 원본 회귀와 결정적 검증
- [R-S06-31 Sol/xhigh 제한 실제 재검증 결과](r-s06-31-sol-xhigh-actual-validation-handoff.md): 새 Gate 5/5, 호출 전 HEAD 불일치로 경계 FAIL·13사례 NOT_RUN·0/0/0, 1.0 NO-GO
- [R-S06-32 Sol/xhigh 제한 실제 재검증 결과](r-s06-32-sol-xhigh-actual-validation-handoff.md): 새 Gate 5/5, clean·bad PASS 뒤 wrong-goal AC 관계 26/28로 FAIL·후속 10사례 NOT_RUN·3/3/0, Functional Alpha·1.0 NO-GO

## 동결 기준선과 회귀 입력

- [R3.1 최종 동결 기준선](r31-frozen-baseline.md): campaign 10의 150/150 최종 `FAIL`, telemetry와 새 엔진으로 이관한 실패 유형
- [R3.1 회귀 fixture](../tests/fixtures/engine/r31-reviewer-regressions.json): 모순 admission, clean 오차단, schema 실패와 correlated-defect 사례
- [R3.1 상세 계약](planner-r31.md): 당시 prototype 계약. 현재 설계가 아닌 감사 기준선
- [R3.1 원본 campaign 결과](../spikes/orchestration/r31/artifacts/runs/r31-role-eval-campaign10-20260903/r31-role-fixture-evaluation.json): 수정하지 않는 원시 결과

## 현재 Engine 구현

- `src/flowmarshal/engine/domain.py`: frozen schema와 Core-derived 판정
- `src/flowmarshal/engine/ledger.py`: 별도 SQLite 원장, immutable revision과 History
- `src/flowmarshal/engine/context.py`: Project Map, Context Selector와 prompt binding
- `src/flowmarshal/engine/goal.py`: Goal 정규화·독립 검토·Core compilation
- `src/flowmarshal/engine/planning.py`: bounded Skeleton-first search와 Hard Gate
- `src/flowmarshal/engine/planner_roles.py`: 실제 구조화 역할 adapter
- `src/flowmarshal/engine/models.py`: `model/list` inventory와 명시적 fallback 배정
- `src/flowmarshal/engine/runtime.py`: Codex Runtime Port, dispatcher와 복구 표면
- `src/flowmarshal/engine/service.py`: activation, lazy materialization, 상태 전이와 validation
- `src/flowmarshal/engine/evaluation.py`: 회귀 지표, immutable checkpoint, token/latency와 cutover Gate
- `src/flowmarshal/engine/cli.py`: 개발용 `flowmarshal-engine` 인터페이스
- `src/flowmarshal/engine/eval_cli.py`: 개발용 `flowmarshal-engine-eval` qualification 인터페이스

## 역사적 설계와 증거

다음 자료는 당시 계약과 실험 결과를 재현하기 위한 감사 이력이다. 현재 Engine의 권위 API로 import하거나 현재 상태로 재해석하지 않는다.

- [R1 Runtime 보고서](../spikes/orchestration/r1/artifacts/runs/r1-20260902T081113Z-51e2d5b8/runtime-report.md)
- [R2 Core 설계·검증](core-r2.md)
- [R3 Planner 계약·검증](planner-r3.md)
- [Gate 0B 설계](gate0b-design.md)
- [Gate 0C 상세 구현 계획](gate0c-implementation-plan.md)
- [Gate 0C 권한 경계 재시험](gate0c-retest-plan.md)
- [Windows sandbox VM 재검증](windows-sandbox-vm-revalidation.md)
- [Windows VM 외부 전달 절차](windows-vm-external-handoff.md)

역사적 결과의 `GO`·`NO-GO`는 당시 계약에 대한 판정으로 유지한다. 새 제품의 1.0 여부는 권위 문서의 cutover Gate만으로 판단한다.
