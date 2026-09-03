# FlowMarshal R3 Planner 계약·검증

- 상태: **GO**
- 완료일: 2026-09-02
- 실행 증거: [R3 Planner 보고서](../spikes/orchestration/r3/artifacts/runs/r3-20260902T092930Z-5112125a/planner-report.md)
- 구조화 증거: [R3 Planner receipt](../spikes/orchestration/r3/artifacts/runs/r3-20260902T092930Z-5112125a/planner-receipt.json)
- 예제 입력·출력: [RequestSpec](../spikes/orchestration/r3/artifacts/runs/r3-20260902T092930Z-5112125a/request-spec.json), [PlanDraft 후보](../spikes/orchestration/r3/artifacts/runs/r3-20260902T092930Z-5112125a/plan-candidate.json), [검증 보고서](../spikes/orchestration/r3/artifacts/runs/r3-20260902T092930Z-5112125a/validation-report.json)

## 결과

사용자 요청과 프로젝트 입력을 strict `RequestSpec`으로 만들고, generator가 낸
`PlanDraft` 후보를 프로그램이 재현 가능하게 검사하는 R3 경계를 구현했다.

```text
사용자 요청 + Project + AGENTS.md + 승인 참고자료 + validation 목록
  → RequestSpec
  → PlanGenerator
  → strict PlanDraft 후보
  → DeterministicPlanValidator
  → invalid | needs_user_input | ready_for_assignment
```

Planner는 `FlowMarshalCore`나 `SQLiteCoreLedger`를 받지 않는다. 따라서 후보 생성
중 PlanRevision을 만들거나 활성화할 인터페이스가 없다. 정상 후보를 신뢰 계층이
명시적으로 Core에 가져와도 R3 시점에는 Assignment가 없으므로 `draft`로만
저장되고 활성화되지 않는다.

## RequestSpec

`RequestSpec`은 다음 정보를 하나의 canonical digest로 묶는다.

- 사용자가 입력한 원문 요청과 정리된 request summary
- 프로젝트 ID·이름·root·설명과 현재 parent revision
- 원자적 requirement ID·내용·출처·우선순위
- 프로젝트 `AGENTS.md`, 프로젝트 파일과 승인 참고자료의 경로·실제 내용·digest
- 모든 WorkItem에 필수인 ContextSource 표시
- 사용할 수 있는 validation capability와 check type
- 현재 제품 capability와 명시적 제외 범위
- 최대 WorkItem 수, 작업당 requirement·변경 대상 수 등 분해 한계

ContextSource의 줄바꿈과 앞뒤 공백도 digest에 포함한다. 즉 내용을 정리해서 다른
문서로 바꾸지 않고 Planner가 받은 정확한 입력을 식별한다. `AGENTS.md`와 승인
참고자료는 파일별 허가증이 아니라 정상 Planner 입력이다.

## PlanDraft와 coverage

기존 R2 `PlanDraft`에 다음 필드를 추가했다.

- `request_spec_digest`
- `requirement_coverage[]`
- `clarifications[]`
- `planning_notes[]`

각 requirement의 disposition은 정확히 하나다.

| disposition | 의미 | 다음 상태 |
|---|---|---|
| `work_items` | 하나 이상의 WorkItem이 요구사항을 구현·검사 | 검증 통과 시 `ready_for_assignment` |
| `excluded` | 후보에서 제외하며 구체적인 이유 기록 | 사용자 확인 필요 |
| `clarification` | 결과를 바꾸는 질문과 영향 기록 | 사용자 답변 필요 |

Planner 단계의 출력 schema는 `assignment`를 `null`로 제한한다. 모델과 추론 수준은
R4 Assigner만 추가할 수 있다.

## deterministic 검사

다음 항목은 모델의 자기평가가 아니라 고정된 코드로 검사한다.

- strict JSON schema와 알 수 없는 필드
- requirement 누락·중복·알 수 없는 참조
- dependency 자기 참조·누락·cycle
- 산출물·완료 조건·validation이 없는 WorkItem
- 어떤 requirement에도 기여하지 않는 orphan WorkItem
- 설정된 크기 상한을 넘는 과대 작업과 과도한 미세 분할
- 너무 짧거나 일반적인 objective
- 등록되지 않은 ContextSource와 모든 작업에 필요한 `AGENTS.md` 누락
- 등록되지 않은 validation capability와 check type 불일치
- Planner가 미리 넣은 Assignment
- 같은 `expected_changes` 대상을 공유하지만 dependency 순서가 없는 작업

동일 변경 대상 검사는 정규화된 정확한 target 문자열을 기준으로 한다. 파일뿐 아니라
`component:auth`처럼 Planner가 같은 구성요소 식별자를 출력하면 동일한 방식으로
충돌을 검출할 수 있다.

## 검증 결과

R3 스모크의 정상 후보는 requirement 3개를 WorkItem 2개에 모두 연결했고 issue 0개로
`ready_for_assignment`가 됐다. 다음 실패 후보는 모두 예상한 경계에서 거부됐다.

- coverage에서 requirement 하나 누락
- 두 WorkItem의 dependency cycle
- validation이 비어 있는 WorkItem
- 같은 변경 대상을 다루지만 dependency가 없는 WorkItem 쌍

Planner 호출 직후 Core 원장의 PlanRevision 수는 0이었다. 이후 별도 trusted import를
수행한 revision도 `draft`, 프로젝트의 `active_revision_id`는 `null`로 확인했다.

- R3 단위·통합 검사: 14개 통과
- 저장소 전체 회귀 검사: 168개 통과
- compileall: 통과
- R3 스모크: GO

## 남은 범위

R3는 Planner 계약과 deterministic 검사를 완성한 프로토타입 단계다. 스모크는 semantic
계획 품질이 아니라 계약과 거부 규칙을 고정하기 위해 합성 generator를 사용했다.
deterministic coverage가 보장하는 것은 `RequestSpec.requirements`에 등록된 요구사항의
누락 방지다. 사용자 원문에서 requirement 목록을 의미상 빠짐없이 추출했는지는 아직
사용자 검토 또는 실제 Codex Planner 검사가 필요하다.

다음 단계는 R4 Assigner가 아니라 [R3.1 목적 기반 다중 후보 Planner](planner-r31.md)다.
R3.1에서 ProjectProfile과 이번 PlanningMission을 분리하고, 실제 Codex adapter로 같은
목적의 여러 계획을 생성한 뒤 5개 Hard Gate·soft score·다양성 선택·독립 forward test를
검증한다. 이 계층을 통과해 선택된 `PlanDraft` 하나가 R4로 전달되면, 그때 App Server
`model/list` inventory를 읽어 WorkItem별 실행·검사 model/effort를 Assignment에 기록한다.
