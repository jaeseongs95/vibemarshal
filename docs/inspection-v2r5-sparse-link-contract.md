# Plan inspection v2 5차 희소 양의 링크 계약

> 이 문서는 커밋 `de16afb`의 5차 계약을 기록한다. 후속 고정 static 11은 9 PASS·2 FAIL이었으며, 원본 판정과 남은 경계는 [5차 독립 11사례 기준선](inspection-v2r5-static11-baseline.md)에 보존한다.

## 변경 목적

v2r3의 전체 28행 직접 제출은 검사 능력 판단과 관계 장부를 중복시켰다. v2r4는 전체 행을 제거했지만 AC ID를 scope의 `criterion_refs`에 넣어 scope 능력과 Goal의 validation 요구 관계를 한 객체에 결합했다. 실제 clean 응답은 22개 scope와 32개 양의 ref를 만들고 관련 sibling validation 세 개를 과잉 연결했다.

5차 계약은 두 의미 질문을 분리한다.

1. `validation_scope_rows`는 validation이 실제로 수행하는 절차와 그 `supported|contradicted|unresolved` 상태만 최소 개수로 제출한다.
2. `ac_validation_links`는 Goal이 해당 validation의 supported 절차를 명시적으로 요구한다고 판단한 양의 조합만 별도로 제출한다.
3. adapter는 양의 link가 없는 나머지 AC×validation 조합을 false와 빈 scope 목록으로 확장한다.

## 직접 제출과 파생

양의 link는 `criterion_id`, `validation_id`, 하나 이상의 같은 validation 소유 `supported` `scope_ids`, 간결한 `requirement_claim`을 갖는다. 같은 AC×validation 조합은 한 번만 제출한다. scope는 AC나 citation마다 복제하지 않고 같은 mechanism·status로 함께 판정되는 책임을 합친다.

compiler는 다음만 결정적으로 수행한다.

- 제출된 양의 조합이 고정 AC×validation 대상 집합의 부분집합인지 확인
- 각 scope ID의 존재, validation 소유권과 `supported` 상태 확인
- 고정 순서의 전체 cross-product에서 양의 link는 true와 제출 scope, 생략 조합은 false와 빈 scope로 확장
- Goal·validation·scope의 citation closure, coverage membership witness, finding evidence·affected Task와 taxonomy 값 파생

compiler는 양의 link를 추가·삭제하거나 scope 선택과 `requirement_claim`을 고치지 않는다. 모델이 필수 관계를 생략하거나 잘못 추가하면 전체 행렬로 확장된 결과를 동결 기대표가 의미 FAIL로 검출한다. 현재 Plan에 이미 있는 선택적 연결은 파생 false만으로 결함이 되지 않는다.

## 개발 검증

집중 v2 회귀 22개와 전체 671개 테스트가 통과했다. 개발 checkout의 새 artifact root에서 결정적 Gate도 5/5 통과했다.

| 항목 | 결과 |
|---|---|
| artifact root | `D:\codex\fm-recovery\.flowmarshal-engine-eval\runs\inspection-v2r5-devgate\deterministic` |
| contract digest | `sha256:f8016844b24a240b5880589834379423ed00b98ef52690cdfddec2ebd3f63295` |
| report SHA-256 | `de59d597c273014e345259b306e48f4532739e1abd0380212071ab5a3990224c` |
| compileall·full tests·pip check·synthetic lifecycle·legacy freeze | 5/5 PASS |
| 전체 tests | 671 PASS, 82.571초 |

이 결과는 당시 source의 구조·회귀 검증이다. 후속 실제 모델 결과는 별도 기준선에서 판정하며 이 Gate를 의미 성공으로 확대하지 않는다.

## 검증 경계

v1 raw·schema·validator·evaluator·checkpoint는 동결한다. v2r4 artifact와 timeout intent도 수정하거나 재실행하지 않는다. v2r5는 새 schema·instructions·request binding으로 검증하며 제품 기본 provider는 계속 v1이다.

승격 판단에는 다음 evidence가 모두 필요하다.

1. 전체 단위·통합 회귀와 결정적 Gate 5/5
2. 새 detached worktree에서 preflight·prepare를 거친 static 11 전수 결과
3. qualification 13의 static·expansion·독립 생성 검토·expanded-review 경계
4. 정확한 Plan revision 활성화 뒤 실제 Goal 실행·독립 검증·State 재관측·GoalVerdict

구조 테스트 통과는 실제 모델 정확도나 전체 qualification 성공을 뜻하지 않는다.
