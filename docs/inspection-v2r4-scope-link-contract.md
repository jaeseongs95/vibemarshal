# plan-inspection-v2 scope 양의 연결 계약 보정

> 이 문서는 커밋 `12e496a`의 4차 계약과 그 결정적 검증을 기록한 역사 문서다. 후속 실제 실행에서 scope의 검사 능력과 AC 연결 의미가 다시 결합되는 문제가 확인되어 [v2r4 부분 실행 기준선](inspection-v2r4-partial-baseline.md)과 [v2r5 희소 양의 링크 계약](inspection-v2r5-sparse-link-contract.md)으로 이어졌다.

## 변경 결과

v2r3에서 모델은 원자 scope의 검사 능력을 먼저 판단한 뒤 같은 의미를 28개 `ac_validation_rows`에 다시 옮겼다. clean 응답은 supported scope를 만들고도 AC-003의 Task·Goal oracle과 unittest 연결을 누락했고, `semantic-explicit`은 sibling Task 검사 세 개를 과잉 연결했다. 새 계약은 이 중복 제출을 제거한다.

모델의 직접 제출은 다음 의미 필드로 제한한다.

- 각 validation의 실제 `tool`, `phase`, 직접 근거
- 각 원자 scope의 명시적 `claim`, `supported|contradicted|unresolved` 상태
- supported scope가 실제 수행하는 절차를 명시적으로 요구하는 AC의 양의 `criterion_refs`
- constraint×Task 적용 판단과 실제 Task validation ID
- finding 종류·복구 가능성·typed target과 직접 근거

`ac_validation_rows`는 provider schema에서 제거했다. compiler는 Goal AC와 validation의 고정 순서를 사용해 전체 cross-product를 만들고, 각 조합에 대응하는 supported scope의 criterion refs가 하나 이상이면 true와 해당 scope ID 집합, 없으면 false와 빈 집합을 파생한다. 이 결정은 `CompiledPlanInspectionV2.ac_validation_decisions`에 남는다. evaluator는 기존 동결 28행 기대값을 이 파생 결정과 대조한다.

## 권위 경계

adapter는 scope의 claim·status·criterion refs를 추정하거나 고치지 않는다. 존재하지 않는 AC ID와 contradicted·unresolved scope의 양의 연결은 거부한다. 전체 행렬, Goal·validation·scope citation closure, coverage membership witness와 finding의 파생 evidence·Task는 제출된 의미 선택과 권위 입력의 ID join으로만 만든다.

필수 coverage가 빠진 Plan에서 Reviewer가 supported scope에 해당 AC를 양의 연결하고 `missing_validation_link` target을 제출하면, compiler가 파생한 true와 실제 Plan membership false가 결함을 입증한다. 모델이 양의 의미 연결을 제출하지 않으면 adapter가 관계나 finding을 만들어 주지 않으며 고정 evaluator가 누락을 검출한다.

v1 raw·schema·validator·evaluator·checkpoint는 변경하지 않았다. v1의 상세 반복 장부 규칙은 권위 설계 문서에 보존하고, 자동 주입되는 저장소 `AGENTS.md`에는 v1/v2 적용 경계를 나눠 선택되지 않은 version의 작성 규칙이 역할 지침으로 해석되지 않게 했다.

## 결정적 검증

변경 직후 집중 v2 회귀 22개와 전체 671개 테스트가 통과했다. 첫 개발 결정적 Gate는 다음 결과를 냈다.

| 항목 | 결과 |
|---|---|
| artifact root | `D:\codex\fm-recovery\.flowmarshal-engine-eval\runs\inspection-v2r4-devgate` |
| contract digest | `sha256:7e9f81c564f0ffd60baa2fad5a96e98664112ade86702ab8d97b1fedfcbd5a30` |
| report digest | `sha256:eadac37ffeb7088333879a019e690251fb05a49d5ab15331abe89cb6b6714068` |
| compileall·full tests·pip check·synthetic lifecycle·legacy freeze | 5/5 PASS |
| 전체 tests | 671 PASS, 82.977초 |

동일 clean Reviewer 표본에서 strict schema는 8,725 bytes다. citation catalog는 86개·21,035자이고, instructions 12,976자·payload 53,386자·schema 7,931자를 단순 합산한 request 표본은 74,293자다. provider inspection 최상위 필드는 `validation_rows`, `validation_scope_rows`, `constraint_task_rows` 세 개이며 scope 필드는 `scope_id`, `validation_id`, `mechanism_id`, `claim`, `criterion_refs`, `direct_extra_refs`, `status`다.

이 수치는 구조 검증이며 실제 모델 정확도 증거가 아니다. 후속 고정 실행은 4번째 사례 timeout으로 전수 관측되지 않았고, 완료된 clean에서 scope 22개와 양의 criterion ref 32개, 잘못된 관계 3개가 관측됐다. 원본과 판정은 [v2r4 부분 실행 기준선](inspection-v2r4-partial-baseline.md)에 보존한다.
