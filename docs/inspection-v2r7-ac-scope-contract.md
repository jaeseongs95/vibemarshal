# Plan inspection v2 7차 AC scope 선택 계약

## 문제

6차 provider는 AC×validation 양의 관계마다 `criterion_id`, `validation_id`, `scope_ids`, `requirement_claim`을 제출했다. 이 중 `validation_id`는 선택한 scope의 소유 관계에서 결정적으로 계산할 수 있고, `requirement_claim`은 compiler의 판정·closure·evidence에 사용되지 않았다. 모델이 의미 관계와 기계적 validation join을 함께 작성하면서 clean 사례에서 최대 28개 관계 조합을 계속 의식해야 했다.

6차 실제 실행의 `bad` 사례에서는 `ac_004`가 명시한 Task oracle 절차를 Task unittest·파일 보존 sibling까지 확대했다. 같은 실행의 `wrong-goal`은 독립 Goal Test 책임을 올바르게 연결했다. 따라서 특정 사례의 기대 ID를 prompt에 넣지 않고, 단계 경계와 단계에 결속된 검사 책임을 구분하면서 중복 참조 작성을 제거한다.

## 직접 모델 판단

모델은 기존과 같이 validation mechanism, 최소 scope의 claim·status·직접 근거, constraint×Task 적용 판단, finding 종류·주 target·추가 직접 근거를 제출한다.

AC 관계는 `ac_scope_requirements`로 제출한다.

```json
{
  "criterion_id": "ac_004",
  "scope_ids": [
    "scope_task_oracle_phase",
    "scope_goal_behavior",
    "scope_goal_unittest",
    "scope_goal_file_diff"
  ]
}
```

- 같은 AC의 모든 양의 scope는 한 행에 모은다.
- 실제 Goal statement·validation intent가 요구하는 `supported` scope만 선택한다.
- false 관계, validation ID, 자유 서술형 관계 설명은 제출하지 않는다.
- `Task 검증과 별도로`, `모든 Task 검증 완료 후` 같은 경계·순서 표현은 Task validation 전체를 요구하지 않는다.
- 도구·절차·검사 책임 또는 evidence 목록이 특정 단계에 결속된 경우에만 그 단계의 scope를 선택한다.
- 같은 Task·phase·순서·evidence·결과 주제만 공유하는 sibling scope로 선택을 전파하지 않는다.

## Adapter 전개

compiler는 현재 응답의 scope catalog에서 각 선택 scope의 소유 `validation_id`를 찾는다. 같은 `(criterion_id, validation_id)`에 속한 scope를 입력 순서대로 모아 양의 결정을 만들고, 고정 AC×validation 조합의 나머지는 false와 빈 scope 집합으로 확장한다.

adapter가 검사하는 불변조건은 다음과 같다.

- 한 `criterion_id`는 `ac_scope_requirements`에 최대 한 번만 나타난다.
- criterion은 현재 Goal에 존재해야 한다.
- scope ID는 현재 응답에 존재하고 `supported`여야 하며 한 AC 행에서 중복될 수 없다.
- scope가 소유한 validation은 현재 Plan의 고정 validation 집합에 있어야 한다.
- 전개된 AC×validation 행, coverage witness, citation closure와 finding target 일관성은 기존과 같이 모두 검사한다.

adapter는 빠진 scope를 추정하거나 과잉 scope를 삭제하지 않는다. 잘못된 의미 선택은 전개된 전체 행렬과 동결 기대표의 차이로 그대로 검출된다.

## 참조 책임 변화

| 항목 | 6차 | 7차 |
|---|---|---|
| 모델의 AC 관계 단위 | AC×validation 행 | AC별 supported scope 집합 |
| 모델이 쓰는 validation ID | 각 양의 관계마다 반복 | 쓰지 않음 |
| 자유 서술형 `requirement_claim` | 필수, compiler 미사용 | 제거 |
| validation 소유 join | 모델과 adapter가 중복 확인 | adapter가 단독 계산 |
| 전체 AC×validation 행렬 | adapter가 확장 | adapter가 확장 |
| 의미 오차 처리 | 동결 기대표와 비교 | 동일 |

이 변경은 관계의 의미를 프로그램이 추정하는 규칙이 아니다. 모델은 어떤 AC가 어떤 실제 검사 scope를 요구하는지 직접 선택하고, adapter는 선택된 scope의 이미 결속된 소유 관계만 전개한다.

## 검증 경계

집중 회귀는 strict schema에서 이전 `ac_validation_links`·`validation_id`·`requirement_claim`이 사라지고, AC별 scope 선택이 validation 소유 관계를 통해 전체 행렬로 확장되며, unknown criterion·unknown scope·non-supported scope·중복 criterion이 거부되는지 확인한다. 관련 **22개**와 전체 **671개** 테스트가 통과했다. 결정적 Gate도 다음 결속으로 5/5를 통과했다.

| 항목 | 결과 |
|---|---|
| artifact root | `D:\codex\fm-recovery\.flowmarshal-engine-eval\runs\inspection-v2r7-devgate-final2\deterministic` |
| contract digest | `sha256:65ff56d07589b5c4d9d35d7b3a91a7792f53f9ce4707e3a612452baa287f3d68` |
| source manifest digest | `sha256:24023076cb7df9abaefc78473d68166bf85b58e4af26e659e7fb5de1d9e4b914` |
| qualification report SHA-256 | `3b876e5f89910a5b65be9ae0c065e2d7305427535581213e0578452e6dcf8e51` |
| compileall·full tests·pip check·synthetic lifecycle·legacy freeze | 5/5 PASS |
| Gate 안 전체 tests | 671 PASS, 82.582초 |

결정적 Reviewer strict schema를 compact UTF-8 JSON으로 직렬화하면 6차 9,295 bytes에서 7차 9,116 bytes로 179 bytes 줄었다. 크기 감소보다 중요한 변화는 clean 입력에서 최대 28개 AC×validation 관계 행을 쓰던 형식을 AC 4개 이하의 행으로 제한하고, 각 양의 관계에 반복되던 validation ID와 compiler가 사용하지 않던 자유 서술을 제거한 것이다. 실제 output token·latency·정확도 개선은 새 고정 static 11에서 별도로 판정한다.

이제 새 고정 checkout에서 독립 11사례를 다시 관측한다.

실제 11사례가 모두 호출되고 고정 의미 기대를 통과하기 전에는 S06 qualification 성공으로 보지 않는다. 그 뒤에도 qualification 13, 전체 planning pipeline, Plan 활성화 이후 실제 프로젝트 E2E와 token/latency Gate는 별도로 통과해야 한다.
