# Plan inspection v2 6차 finding target catalog 계약

## 변경 목적

5차 고정 static 11에서 `bad`는 실제 결함 종류와 scope 선택은 맞았지만, 단일 `validation_scope` target에 기계적으로 불필요한 validation ID를 `secondary_ref`로 함께 작성해 schema 후검증에서 실패했다. 이는 결함 의미를 놓친 실패가 아니라 모델이 target 종류와 참조 개수를 조립하는 과정의 실패다. `wrong-goal`은 의도한 phase overclaim finding을 검출했지만, AC가 하나의 독립 Goal validation 단계 안에 열거한 두 검사 책임의 양의 link를 누락했다.

6차 계약은 두 실패를 다음 일반 경계로 처리한다.

1. 고정된 복합 관계의 두 참조는 adapter가 `inspection_target_catalog`로 만든다.
2. Reviewer는 결함 종류와 의미 대상을 판단해 catalog ID 또는 해당 종류의 단일 ID만 선택한다.
3. adapter는 선택 ID를 내부 typed target으로 해석하고 기존 closure·evidence·영향 Task 계산을 수행한다.
4. AC가 독립 Task 또는 Goal validation 단계를 묶음으로 명시하고 검사 책임을 열거하면, 그 단계에서 열거된 책임을 실제 수행하는 각 validation을 연결한다. 같은 phase라는 이유만으로 열거되지 않은 sibling에는 전파하지 않는다.

## 직접 제출과 결정적 파생

`inspection_target_catalog`는 현재 Goal·Plan의 전체 AC×validation과 constraint×Task 조합을 고정 순서로 투영한다. 각 행은 `kind`, `primary_ref`, `secondary_ref`와 이 세 값의 SHA-256 앞 24 hex로 만든 `target_id`를 가진다. 호출 뒤 compiler가 같은 입력에서 catalog 전체를 다시 계산하며 ID·순서·내용이 하나라도 다르면 거부한다.

Reviewer finding은 다음만 직접 제출한다.

| 필드 | 직접 의미 |
|---|---|
| `primary_target_ids` | 결함 종류에 맞는 주 대상. missing link 종류는 catalog ID, scope 결함은 응답 안의 scope ID, result order는 validation ID, other는 citation ID |
| `direct_extra_refs` | 주 target closure 외에 직접 사용한 citation catalog ID |
| `direct_task_refs` | 주 target 소유 관계로 계산할 수 없는 직접 영향 Task ref |

Provider 출력 schema에는 내부 `InspectionTargetV2`, `kind`, `primary_ref`, `secondary_ref`, `target_refs` 장부가 없다. compiler는 모델이 선택한 값을 다음처럼만 전개한다.

- 복합 catalog ID를 원래 두 참조가 있는 내부 typed target으로 해석
- scope ID에서 validation과 소유 Task, validation ID에서 소유 Task를 조회
- target closure에서 citation·evidence ref와 영향 Task를 파생
- 다섯 표준 defect kind의 gate·severity와 summary를 동결 taxonomy에서 계산

compiler는 finding 종류나 target ID를 선택하지 않고, 잘못된 종류·없는 ID·catalog 불일치·closure 모순을 그대로 거부한다. scope claim·status, 희소 양의 AC link, 직접 citation과 Task 선택도 보정하지 않는다.

## 개발 검증

집중 v2 회귀 **22개**와 전체 **671개** 테스트가 통과했다. 최종 source의 결정적 Gate는 아래 값으로 5/5를 확인한다.

| 항목 | 결과 |
|---|---|
| artifact root | `D:\codex\fm-recovery\.flowmarshal-engine-eval\runs\inspection-v2r6-devgate-final\deterministic` |
| contract digest | `sha256:801757cf8fdf8d0f45889204b8839828365e5676464df86b160ab5a5fca3ad1a` |
| source manifest digest | `sha256:86b50c2e1e67fc1f41eef0430b5d043d8877d83394e44f7e68ee255d82f6eaa5` |
| report SHA-256 | `690a5a459b627f26b1d5cf693dd5dacce5a8888a6717dfe7a22ef3314a2e7b31` |
| compileall·full tests·pip check·synthetic lifecycle·legacy freeze | 5/5 PASS |
| 전체 tests | 671 PASS, 82.219초 |

결정적 clean 표본에서 Reviewer strict schema는 5차 9,357 bytes에서 6차 9,295 bytes로 줄었고, 요청에는 AC×validation 28개와 constraint×Task 3개를 합한 target catalog 31행이 추가됐다. 크기 변화는 구조 관측이며 실제 정확도·token·latency 개선은 고정 static 11의 provider usage로 별도 판정한다.

## 검증 경계

v1과 v2r1~v2r5의 raw·schema·validator·evaluator·checkpoint·실패 판정은 수정하지 않는다. 제품 기본 provider도 유지한다. 이 계약은 새 detached worktree에서 preflight·prepare를 다시 통과한 뒤 static 11을 독립 호출하며, schema·참조·의미 실패는 사례별로 보존하고 provider terminal failure나 `external_unknown`에서만 전체 실행을 중단한다.

static 11이 모두 통과해야 qualification 13의 기존 첫 실패 중단과 `expansion → 독립 생성 검토 → expanded-review` 경계로 진입한다. qualification과 실제 Goal 실행·독립 검증·State 재관측·GoalVerdict까지 완료되기 전에는 전체 목표나 1.0 cutover를 완료로 판정하지 않는다.
