# Inspection Provider v2 계약

## 목적과 권위 경계

이 문서는 Plan inspection provider의 v2 입력·출력과 adapter 경계를 정의한다. v2는 Reviewer가 제출하는 비권위 관측을 더 세밀하게 결속하는 형식이며, `GoalContractRevision`·`PlanContractRevision`의 의미, Domain Core의 상태 전이, admission·score·weakest task·Goal 판정을 변경하지 않는다. Core는 계속 원장과 결정적 규칙으로 판정하며 provider가 `status`, `admissible`, score 또는 최종 결정을 제출하지 않는다.

v1의 raw 입력, strict schema, validator, evaluator와 이미 기록된 artifact는 동결한다. v2 구현이나 평가를 위해 v1 원문·schema·validator·evaluator를 재해석, 덮어쓰기, 보정 또는 migration하지 않는다. 같은 사례를 v2로 호출하려면 v2 형식의 별도 입력·schema·평가 계약과 artifact를 만든다.

## v2의 직접 모델 작성물

모델은 아래 원자 관측만 직접 작성한다. adapter는 결속된 입력의 의미 원문에서 immutable `inspection_citation_catalog`를 먼저 만들며, 모델은 원문 citation 객체를 다시 쓰지 않고 실제 판단에 사용한 catalog ID만 선택한다. 모든 직접 참조는 현재 요청의 citation catalog에, typed target은 현재 요청의 비교 대상 집합에 존재해야 한다.

| 작성물 | 직접 제출해야 하는 내용 |
|---|---|
| citation 선택 | mechanism·scope·AC 행의 `direct_refs`와 finding의 citation target에서 실제 판단에 사용한 `inspection_citation_catalog` ID |
| validation claim | `validation_id`, `mechanism_id`, `tool`, `phase`, 직접 `direct_refs` |
| validation scope | 선택한 `mechanism_id`, 직접 추가 근거, scope `status` |
| AC×validation 행 | `criterion_id`, `validation_id`, `ac_link_required` bool, `scope_ids`, 직접 추가 근거 |
| constraint×Task 행 | `constraint_id`, `task_ref`, `applicability`, 적용될 때의 `required_validation_ids` |
| finding | `finding_code`, `defect_kind`, `remediable`, 단일 형태의 typed target. 표준 taxonomy 밖의 `other`는 직접 `gate`·`severity`도 제출 |

`tool`과 `phase`는 등록 자료·구현에서 직접 확인한 mechanism을 식별한다. 같은 이름의 도구, 다른 phase, 단순 evidence 종류, 인접한 validation 또는 선후관계만으로 검사 능력이나 scope를 확대하지 않는다. `status`는 해당 scope의 직접 관측 상태이며 다른 scope·AC 행·finding의 결론을 암시하지 않는다.

AC×validation의 bool은 필수 연결 여부만 표현한다. false는 선택 연결을 금지하지 않으며, true는 새 검사 책임·새 Task·새 validation ID를 만들지 않는다. constraint×Task의 `applicability`도 AC 관계를 추정하지 않는다. finding target은 모두 `{kind, primary_ref, secondary_ref}` 한 형태로 제출한다. `ac_validation`과 `constraint_task`만 두 ID를 순서대로 쓰고, 나머지는 `primary_ref` 하나와 `secondary_ref: null`을 쓴다. 이 표현은 provider strict schema가 배열 item의 `oneOf`를 거부하는 경계에서도 직접 target 선택을 그대로 보존한다. target은 요청에 제공된 Task·validation·criterion·scope 등의 허용 종류와 ID에 맞아야 하며, 존재하지 않는 target을 결함 설명으로 사용할 수 없다. 영향 Task는 이 target에서 adapter가 계산한다. `other` 결함이 특정 Task에 영향을 준다는 의미 판단은 citation target과 함께 `task` target을 직접 선택해 보존한다.

## Adapter가 결정적으로 파생하는 값

adapter는 직접 작성물을 받아 다음 값만 결정적으로 계산한다. 파생 값은 provider의 의미 판단을 대체하거나 고치지 않는다.

| 파생 값 | 계산 규칙 |
|---|---|
| citation catalog | 결속된 Goal의 사용자 요청·outcome·AC·constraint·preference·assumption·effect, Skeleton/Plan의 목적·입출력·완료·검사·효과 문장과 등록 자료 본문만 `source_ref`·JSON pointer·연속 quote의 content hash ID로 고정한다. Goal source trace와 State·ProjectMap의 ID·digest·path 장부는 직접 의미 근거 후보에서 제외한다. 등록 파일 본문은 요청에 실제 노출한 instruction·reference entry만 포함한다. |
| 고정 claim ref | validation statement, Goal AC statement·validation intent, constraint statement의 citation ID를 원본 ID·selector join으로 붙인다. 모델이 같은 원문 주소를 반복 제출하지 않는다. |
| 행 closure | 고정 claim ref, 모델이 선택한 직접 refs, 그 행이 선택한 mechanism·scope의 refs를 대조한다. |
| target closure | finding이 가리키는 typed target과 그 finding의 직접 근거가 현재 target·evidence catalog 안에서 닫히는지 확인한다. |
| project evidence 환산 | 검증된 `project:*` citation은 Reviewer evidence에서 `source:project_map`으로 환산한다. `source:goal`과 `artifact:plan_contract`는 그대로 유지한다. |
| Reviewer evidence refs | finding의 typed target closure를 원본 evidence ref로 환산하고, 그 결과가 실제 evidence catalog의 부분집합인지 확인한다. provider는 `finding_links`나 `evidence_refs`를 다시 작성하지 않는다. |
| affected Task refs | `ac_validation`·`validation_scope`·`validation` target의 검사 소유 Task, `constraint_task`의 Task, 직접 선택한 `task` target을 합집합으로 계산한다. integration validation처럼 소유 Task가 없으면 임의 Task를 붙이지 않는다. |
| taxonomy 값 | 다섯 표준 `defect_kind`의 gate·severity와 모든 finding의 결정적인 summary 형식을 계산한다. `other`의 gate·severity는 모델의 직접 제출값을 보존한다. |
| 빈 coverage membership witness | 빈 coverage나 빈 scope membership은 후보 집합과 join 결과가 실제로 비어 있음을 보여 주는 witness로 남긴다. adapter는 빈 집합을 연결 누락·무결함·새 관계로 해석하지 않는다. |

adapter가 만든 catalog와 고정 claim ref는 입력 원문의 기계적 주소와 내용만 표현한다. adapter는 모델의 direct ref 선택, 행의 bool·status, finding·typed target, `other`의 직접 gate·severity, coverage, 관계 의미를 생성·삭제·교정하지 않는다. closure 실패는 정확한 행·target·누락 ref를 오류로 보고하고 제출을 거부한다. `project:*` 환산과 영향 Task 계산도 모델이 선택한 target의 표현 변환일 뿐, 해당 Project Map 본문·검사 능력·finding 근거를 새로 추가하는 작업이 아니다.

State·ProjectMap revision, digest, root, freshness와 요청 binding은 기존 Core·preflight가 역할 호출 전에 결정적으로 검사한다. v2 Reviewer에게는 v1 장부 작성 지침을 함께 제공하지 않으며, 이 기계 메타데이터의 문자열 비교를 semantic finding으로 요구하지 않는다. 등록 본문과 Goal·Plan 의미의 실제 충돌은 계속 모델이 직접 판단한다.

## 생성과 보정의 금지

다음은 model과 adapter 모두에게 금지된다.

- bool, scope status, finding, typed target, 모델의 direct ref 선택을 추측해 채우거나 수정하는 행위
- 선언·시그니처·evidence 종류만으로 새 검사 능력, tool/phase 범위 또는 의미 관계를 만드는 행위
- AC 연결 누락을 근거로 새 validation·Task·완료 조건을 추가하는 행위
- 찾은 결함을 이유로 Goal·Plan 원문, oracle, taxonomy, 기대값 또는 과거 판정을 보정하는 행위
- 다른 scope·행·finding의 근거를 현재 행의 직접 근거로 전파하는 행위

v2가 제출할 수 없는 입력은 provider 재질문이나 silent fallback으로 메우지 않는다. 필요한 citation·target·등록 본문이 없으면 호출 전 입력 계약을 중단하거나, 이미 호출했다면 해당 artifact를 실패 evidence로 보존한다.

## Strict 검증 순서

v2 adapter는 다음 순서를 유지한다. 앞 단계가 실패하면 뒤 단계의 의미 판정이나 자동 보정을 수행하지 않는다.

1. raw JSON의 중복 key, 최상위 타입, strict schema, unknown field와 필수 field를 검사한다.
2. 요청 전에 의미 projection으로 생성한 citation catalog의 ID·순서·source·selector·quote·content digest를 현재 입력에서 다시 계산해 결속한다. projection 밖의 기계 장부 변경은 별도 입력 binding 검사가 담당한다.
3. validation claim, mechanism, scope, AC×validation, constraint×Task, finding의 ID·typed target·형식을 각각 검사한다.
4. 원본 ID·selector join으로 고정 claim ref를 찾고 각 행 closure와 scope가 선택한 mechanism의 직접 근거를 검사한다.
5. target closure, `project:* → source:project_map` 환산, finding별 Reviewer evidence refs와 영향 Task refs를 계산한다.
6. taxonomy gate·severity·결정적 summary와 빈 coverage membership witness를 계산하고 제출물 내부 일관성을 검사한다.
7. 통과한 비권위 submission만 Core 입력으로 전달한다. Core는 기존 규칙으로 status와 판정을 다시 계산한다.

이 순서는 v1의 행 내부 검사와 첫 실패 중단을 약화하지 않는다. v2는 전체 오류 수집을 명분으로 첫 실패 이후의 citation·finding·관계 값을 생성하거나 의미 평가를 계속하지 않는다.

## v1에서 v2로의 전환

전환은 형식별로 분리한다.

- v1 raw/schema/validator/evaluator, checkpoint, fixture oracle, 과거 receipt와 결과는 그대로 보존한다.
- v2는 versioned schema, prompt, direct citation catalog, target catalog, taxonomy, adapter와 evaluator digest를 새 evaluation contract에 함께 결속한다.
- v1 checkpoint·평가 결과·PASS/FAIL을 v2의 입력이나 합격 근거로 재사용하지 않는다. v2 artifact도 v1 evaluator에 역으로 넣지 않는다.
- 같은 Goal·Plan을 다루어도 v2는 새 Plan revision, 새 활성화 또는 Core 상태 변경을 뜻하지 않는다. 의미 변경이 필요하면 기존 Goal/Plan revision 절차를 따른다.

## Expander 경계

Expander는 기존처럼 Goal·Skeleton·State·Project Map에 근거한 Plan 후보를 제출한다. 같은 v2 inspection 구조를 사용하되 생성 결과에는 finding branch가 없고, 모든 scope가 supported이며 필수 coverage·constraint 결함이 없는 완성 후보만 허용한다. 출력 Plan은 호출 전 citation catalog에 존재할 수 없으므로 adapter가 생성 결과의 validation statement claim ref만 동일한 content hash 규칙으로 계산한다. 이 계산은 Plan 문장의 의미를 판단하거나 고치지 않는다. adapter가 finding을 생성해 Plan을 고치거나 새 검사·직접 근거 선택을 발명하지 않으며, 결함이 있으면 생성 결과 전체를 거부한다.

`expansion → 독립 생성 검토 → expanded-review` 경계는 유지한다. 독립 생성 검토는 생성된 Plan의 입력·근거·기준을 별도 artifact로 대조한 뒤에만 expanded-review에 전달하며, v2가 생성 Plan을 성공으로 선언하거나 그 검토 단계를 생략할 수 없다.

## 호환성, 평가, 롤백과 승격

호환성은 v1과 v2 artifact를 같은 역사 보관소에 둘 수 있다는 뜻에 한정한다. 서로의 schema, evaluator, checkpoint 또는 oracle을 섞어 실행·판정하지 않는다. provider·schema·adapter·입력 결속 실패는 v2 artifact에 FAIL 또는 NOT_RUN으로 남기고, 과거 v1 결과를 대체하지 않는다.

롤백은 v2 호출을 중지하고 이미 생성한 raw·receipt·closure 오류·평가 artifact를 보존하는 운영 조치다. v1으로 자동 fallback하거나 v2 실패를 숨기기 위한 재호출·oracle 완화·payload 보정은 허용하지 않는다. 이전 v1 흐름을 별도로 실행할 필요가 있으면 그 자체의 동결 계약과 평가 경계를 다시 적용한다.

v2 승격은 다음을 모두 별도 evidence로 충족한 뒤에만 검토한다.

1. static 11사례의 v2 계약·raw·receipt·closure·평가 artifact
2. qualification 13사례의 기존 첫 실패와 `expansion → 독립 생성 검토 → expanded-review` 경계를 보존한 v2 evidence
3. 실제 Goal에서 입력 결속, 직접 근거, 독립 검토와 Core의 기존 판정 경계를 확인한 evidence

위 증거는 v2 provider 형식의 승격 검토 조건일 뿐, 전체 qualification·cutover·`flowmarshal` 1.0 승격을 자동으로 뜻하지 않는다. 기존 qualification 범위와 token/latency Gate, 실제 프로젝트 E2E 요구는 계속 별도로 적용한다.
