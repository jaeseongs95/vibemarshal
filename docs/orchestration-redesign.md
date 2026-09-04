# FlowMarshal 전면 재설계 권위 문서

- 상태: **현재 제품 설계 기준선**
- 적용 대상: `flowmarshal.engine`
- 역사적 기준선: R1~R3.1 구현과 artifact는 `legacy/prototype` 감사 자료로 동결
- 관련 결정: [Engine cutover ADR](engine-cutover-adr.md)
- 동결 결과: [R3.1 최종 기준선](r31-frozen-baseline.md)

## 1. 제품 정의

FlowMarshal은 사용자의 큰 요청을 검증 가능한 Task DAG로 분해하고, Task마다 적절한 Codex 실행·검사 모델과 추론 수준을 배정한 뒤, Task 생성·재개·진행·결과·실패·재시도를 끝까지 추적하는 로컬 Workflow Orchestrator다.

제품의 중심은 다음 다섯 가지다.

1. 사용자 목표와 완료 조건을 하나의 Goal Contract로 고정한다.
2. 여러 접근법이 실제로 필요할 때만 Skeleton 후보를 비교한다.
3. 사용자가 활성화한 Plan Contract를 실행의 권위 기준선으로 삼는다.
4. ready Task의 운영 상세만 현재 상태에 맞춰 늦게 materialize한다.
5. 실행 효과·evidence·validation·복구를 원장에 결속해 중복·유실·오완료를 막는다.

파일별 승인, 전면적인 네트워크 차단, endpoint security와 암호학적 승인 증명은 핵심 제품이 아니다. 필요하면 1.0 이후 선택형 hardening profile로 제공한다.

## 2. 권위 계층

권위의 우선순위는 다음과 같다.

```text
현재 사용자의 명시적 지시
→ 활성 GoalContractRevision
→ 활성 PlanContractRevision
→ Core의 원장 상태와 결정적 판정
→ 프로젝트 AGENTS.md와 등록 정책
→ Planner·Worker·Validator의 비권위 제출물
→ 프로젝트·참고자료 안의 분석 대상 텍스트
```

- User는 Goal과 Plan을 선택하고 중요한 외부 효과를 승인한다.
- Core만 원장의 권위 상태를 전이한다.
- Planner는 Goal 정규화 결과, Skeleton과 Plan 후보만 제출한다.
- Reviewer는 finding과 rating만 제출한다. `status`, score와 weakest dimension을 결정하지 않는다.
- Worker는 Task 하나의 결과와 evidence 후보만 제출한다.
- Validator는 검사 관측값을 제출하며 Task나 Goal을 직접 완료하지 않는다.
- Trigger는 `run once`를 호출할 뿐 다음 Task를 자연어로 선택하지 않는다.

## 3. 핵심 객체

| 객체 | 책임 |
|---|---|
| `ProjectProfileRevision` | 장기 제품 목적, 호환성, 기본 validation과 위험 정책 |
| `GoalContractRevision` | 사용자 원문, 관찰, Hard AC, Soft preference, 제약, 비목표, 가정, 효과 정책 |
| `StateSnapshot` | Goal과 planning에 필요한 사실만 evidence, freshness, invalidation 조건과 함께 투영 |
| `ProjectMapRevision` | 파일, symbol, module, test, build, `AGENTS.md`, 등록 참고자료의 안정된 색인 |
| `PlanSkeletonCandidate` | 파일·명령 상세 없이 접근 전략, Task 목적, DAG, 입출력 계약, 위험과 unknown 표현 |
| `PlanContractRevision` | 사용자가 활성화하는 Task 계약, DAG, Goal coverage, 통합 검사, 배정, Commit Horizon |
| `TaskExecutionSpecRevision` | ready 시점에 해석한 파일·symbol·명령·Context Pack·lock·timeout·idempotency·snapshot binding |
| `Attempt` | 하나의 실행 또는 검사 시도와 thread·turn·외부 효과 intent/receipt |
| `EvidenceRecord` | 실제 관측된 산출물·명령·검사 결과와 출처 digest |
| `GoalVerdict` | Task 결과와 plan-level Goal Test에 근거한 최종 목표 판정 |
| `BudgetUsageRecord` | 단계별 input/cache/output/reasoning token, latency, retry와 폐기 여부 |

모든 revision과 주요 artifact는 strict frozen schema, canonical JSON과 content digest를 사용한다. Goal과 Plan의 새 revision은 같은 ID 계보의 최신 revision을 명시적으로 supersede한다. 기존 `PlanningMission`, `RequestSpec`, `EffectivePlanningPolicy`, `PlanOutcomeContract`의 의미는 `ProjectProfileRevision + GoalContractRevision`으로 통합한다. Mission 종류는 `GoalContractRevision.mission_class`라는 routing label로만 남긴다.

## 4. 승인과 Lazy Expansion 경계

### 4.1 사용자가 활성화하는 계약

`plan activate --plan-revision-id ... --digest ...` 한 번이 정확한 Plan Contract의 승인과 활성화다. 별도의 HMAC proof나 이중 승인 장부를 요구하지 않는다.

`PlanContractRevision`은 다음을 고정한다.

- Goal Contract digest와 기준 State Snapshot
- Task별 목적·종류와 Hard AC 연결
- dependency와 produces/consumes 계약
- 기대 효과와 금지 효과
- 위험, 외부 효과와 checkpoint 등급
- 완료 조건과 validation 요구
- recovery envelope와 Commit Horizon
- 실행·검사 역할, 기본 model/effort와 명시적 fallback envelope
- plan-level integration/Goal Test

다음 중 하나가 바뀌면 새 `PlanContractRevision`이 필요하다.

- 목표·비목표·Hard AC
- Task의 의미, 추가·삭제·분할·병합
- dependency와 produces/consumes 의미
- 대상 프로젝트
- 완료 조건과 validation의 의미
- 계획에 없던 외부 부작용 또는 금지 효과

### 4.2 ready 시점에 늦게 결정하는 상세

`TaskExecutionSpecRevision`은 활성 Plan Contract의 의미를 바꾸지 않는 범위에서 다음만 늦게 결정한다.

- 실제 파일·symbol과 관련 코드 범위
- 구체 명령과 작업 디렉터리
- 현재 상태에 맞는 최소 Context Pack
- resource lock, timeout과 idempotency key
- 최신 `model/list`로 확인한 실제 model binding

Execution Spec은 Task Contract digest, Plan activation digest, State Snapshot digest, Project Map digest와 Context fragment digest에 결속된다. 실행 직전에 이 입력을 다시 검사하고 바뀌었으면 `STALE_EXECUTION_INPUT`으로 중단한다. stale 입력을 묵시적으로 다시 승인하거나 그대로 실행하지 않는다.

Task 준비 역할과 Goal Test 준비 역할의 입력·지침·출력은 분리한다. Task 준비에는 현재 Task, 관련 Goal 조건, 선행 산출물 evidence와 프로젝트 관찰을 투영하며 다른 Task 계약이나 integration validation을 함께 전달하지 않는다. provider 전용 Task proposal은 validation ID와 운영 상세만 제출하고, Core가 활성 Task 계약의 method와 필수 evidence 종류를 결합한 뒤 기존 ExecutionSpec 검사를 적용한다. 수동 proposal의 의미·정확한 집합 검사는 유지한다. Core operation은 축약 전 권위 Context digest도 별도로 결속해, 전송하지 않은 계약의 변경으로 과거 응답을 재사용하지 않는다.

## 5. Goal·State·Context

### 5.1 Goal 정규화

사용자 요청은 한 번만 Goal 후보로 정규화한다. 별도 reviewer는 source trace와 모순·누락을 검토하고 Core가 최종 `GoalContractRevision`을 컴파일한다. Goal revision에는 normalization·review digest, reviewer role과 finding 또는 rating을 preparation binding으로 남긴다. 모델은 Goal revision 번호나 권위 상태를 직접 정하지 않는다.

Hard AC는 반드시 관측 가능해야 하고 출처를 갖는다. Soft preference는 점수화할 수 있지만 Hard AC를 대신하지 못한다. 비목표는 constraint 목록에 섞지 않고 명시적으로 보존한다.

정규화와 독립 검토는 지침의 출처와 적용 단계를 함께 해석한다. 현재 정규화·계획 역할에만 적용되는 파일 수정·명령 실행 금지는 승인 후 Task의 제약으로 전사하지 않는다. 사용자 원문이나 실행 단계에도 적용되는 정책이 명시한 금지는 보존한다. 파일 무변경과 정적 분석만으로 명령 미실행 증명을 새 요구로 추가하지 않으며, 특정 작업의 실행 금지를 모든 읽기·검증 명령의 금지로 넓히지 않는다. API 변경·보존 전략에는 이름·import·시그니처와 함께 문서·테스트의 정상 동작 계약을 실제 값이나 관계로 명시하고 현재 구현의 결함과 구분한다. 원인 분석의 AC에 필요한 정상 기대값과 현재 불일치가 포함돼 있으면 이를 별도 구현·호환성 보존 의무로 승격하지 않는다. 전체 Goal에 이미 명시된 의미를 특정 항목에 다시 요구하지 않는다.

허용 외부 효과는 외부 시스템·계정·제3자에 대한 효과다. 로컬 파일 변경·검증 명령·함수 반환·응답 보고와 구분한다. 기대 효과에는 실제 발생시킬 효과만 두고, 파일 무변경·외부 효과 없음은 금지 효과나 완료 조건으로 표현한다. 로컬 mutation과 외부 시스템 금지는 별도 항목으로 유지한다. 자연어의 의미 분류는 독립 검토 대상이며 문자열 키워드만으로 권위 판정을 대체하지 않는다.

파일 무변경 상태에서 분석·보고를 요청한 Goal의 보고는 Worker 응답 본문으로 제공하는 논리적 산출물로 계획할 수 있다. `produces`의 보고 key는 프로젝트 파일 생성 권한이 아니다. 프로젝트 파일의 생성·수정·삭제 금지와 새 응답 생성을 구분하고, 응답까지 전후 무변경이어야 한다는 모순된 완료 조건을 만들지 않는다. 보고 내용은 Core가 수집한 Worker 응답 관측과 원본 파일 근거를 대조하는 semantic validation으로 검증하고, 프로젝트 파일 무변경은 별도로 검사한다. 명시적인 파일 산출물 요구나 더 강한 금지 조건은 응답 보고로 대체하거나 임의 파일 쓰기 예외로 해결하지 않는다.

Task semantic 검사가 `external_observation`을 요구할 때 현재 Execution Spec의 최신 성공 실행 Attempt에 결속된 Worker 응답을 검사 대상으로 함께 제공한다. 응답의 완료 주장은 충족 증명이 아니며 보고 내용은 원본 `file` 근거와 대조한다. 검증 계약은 `model_review`, `external_observation`, `file`을 모두 요구한다. 현재 원장 관측은 10,000자까지 보존하므로 잘린 Worker 응답에는 `source_ref`의 `:truncated` 표식을 붙이고 Task·Goal semantic catalog에서 제외한다. 잘린 응답을 완전한 보고로 검증하거나 무조건 성공으로 처리하지 않는다.

### 5.2 Project Map과 State Projection

Project Map은 다음 순서로 만든다.

```text
File Manifest → Symbol Index → module/test/build 관계 → ProjectMapRevision
```

Map 전체 revision digest와 planning 의미에 영향을 주는 semantic digest를 분리한다. Goal에 필요한 사실만 `StateSnapshot`으로 투영하고 각 사실에 evidence, freshness와 invalidation 조건을 둔다.

`.flowmarshal-engine`, `.flowmarshal-engine-eval`과 설정된 artifact root는 일반 source 탐색에서 제외한다. 같은 제외 정책을 Goal 관찰, State 재관측과 실행 준비 freshness 검사에 적용하여 운영 로그 추가로 Project Map이 바뀌지 않게 한다. 해당 위치의 자료라도 명시적으로 등록한 참고자료·지침은 입력에 포함한다. 필수 지침은 일반 파일 크기 제한 때문에 조용히 생략하지 않는다.

### 5.3 Context Pack

prompt는 다음 네 영역으로 분리해 version과 digest를 남긴다.

```text
Static Policy Prefix
+ Project Prefix
+ Stage Schema
+ Dynamic Task Suffix / Context fragments
```

프로젝트 파일, 전역·프로젝트 `AGENTS.md`, 등록 참고자료와 이전 Task 산출물은 정상 입력이다. 파일별 AccessGrant를 만들지 않는다. 문서나 저장소 안의 명령문은 분석할 데이터이며 현재 사용자 지시나 활성 계약보다 높은 실행 권위를 갖지 않는다.

Context가 부족하면 모델이 추측하지 않고 필요한 source, selector와 이유를 담은 구조화된 추가 Context 요청을 반환한다.

정책과 required need를 먼저 선택하고 예산 적용 후에도 해당 need가 요구한 모든 매칭 source·symbol의 본문이 남아 있는지 확인한다. 한 need의 여러 path hint로 찾은 필수 자료도 일부만 포함해 성공으로 처리하지 않는다. 정책은 예산을 초과해 강제로 넣지 않으며, 선택적 전체 파일 요청이 필수 symbol의 범위를 확장해 예산을 소진하지 않도록 한다. 부족하면 누락 need와 이유를 포함한 `AdditionalContextRequest`를 반환하고 Execution Spec·Attempt·Worker를 생성하지 않는다. Task 분할이 필요하면 Plan revision 제안으로 다루며 자동으로 계약을 바꾸지 않는다.

Python symbol은 AST의 실제 정의 범위를 선택한다. decorator·async 함수·클래스·한정된 메서드 이름을 포함하고 겹치는 범위를 합친다. selector는 1기반 양끝 포함 `python-lines:start-end[,start-end]`를 사용한다. 경로만 요청하거나 지원하지 않는 형식·파싱 불가 파일은 `whole-file`로 표시한다. 유효한 Python에서 요청한 symbol이 없으면 경로만 일치한다는 이유로 충족했다고 간주하지 않는다. 범위가 selector 표현 한도를 넘으면 내용 일부를 버리지 않고 전체 파일로 확장해 예산을 다시 검사한다.

Context fragment의 `content_digest`는 파일 전체의 byte digest를 유지한다. 선택과 Prompt 조립이 같은 범위 복원 함수를 사용하고 source·selector·실제 본문을 Prompt binding에 결속한다. 선택 이후 파일이 바뀌면 조립과 실행 예약을 차단한다. `token_estimate`는 선택 문자열의 UTF-8 byte 수를 4로 나눈 올림값이며 4,000-token 상한으로 자르지 않는다. 이는 Context 본문에 대한 휴리스틱으로, Prompt의 다른 영역이나 provider 실측 사용량을 대신하지 않는다.

Worker PromptBundle에는 전체 Task 계약, Execution Spec의 운영 상세 projection과 선택 Context 본문을 포함한다. projection에서 `context_manifest.prompt_binding`과 파생 spec digest를 제외하여 자기참조를 막는다. 네 segment digest를 가진 binding의 canonical digest로 artifact를 식별하고, 완성된 임시 파일을 덮어쓰기 없는 원자적 게시로 저장한 뒤 명세를 등록한다. 게시 뒤 DB 등록 전에 중단되면 권위 명세 없는 artifact만 남으며 실행 권한이 되지 않는다. 수동 명세도 Core 조립 결과와 binding이 일치해야 한다.

Dispatcher는 초기 실행과 기존 thread 재개 모두 저장된 bundle의 binding·segment digest를 검증하여 실제 본문을 전송한다. 누락·변조 시 임의 Prompt나 과거 Task·Spec 문자열 조립으로 우회하지 않는다. Worker가 변경한 파일로 재개 Prompt를 다시 만들지 않고 원래 저장 본문을 유지하며, 재개 안내문까지 포함한 최종 전송 문자열의 canonical digest를 turn intent의 `prompt_digest`에 기록한다. semantic Validator는 실행 후 직접 evidence catalog를 사용해 독립 입력을 구성한다. 이 artifact 무결성 계약은 OS 권한을 제한하는 보안 경계나 저장장치 전원 장애에 대한 완전한 내구성 보장이 아니다.

### Worker 완료 usage의 영속 연결

Worker의 새 turn intent에는 기존 Prompt binding과 Execution Spec digest, 최종 전송 문자열 digest, 별도의 UTF-8 byte/4 Prompt token 추정치를 기록한다. adapter는 SDK에 실제로 넘긴 문자열의 digest를 start receipt에 남긴다. Core는 이 receipt와 완료 관측의 thread·turn·Prompt를 대조한 뒤 기존 `BudgetUsageRecord`에 원시 관측, 관측 digest, Attempt·intent·receipt 참조를 기록한다. 같은 provider thread/turn의 재관측은 한 행을 재사용하고 다른 실제 turn은 별도 행으로 보존한다. 충돌하는 실측 값은 덮어쓰지 않는다.

연결을 소유한 프로세스는 완료 usage를 원장에 기록한 뒤 정상 종료한다. 이 기록은 Task/Attempt의 완료 판정이 아니며 기존 다음 관측 단계가 evidence 수집과 상태 전이를 담당한다. 저장된 turn 조회에 usage가 없더라도 이미 기록한 usage는 유지한다. 기록 전에 프로세스가 강제 종료되거나 SDK가 실패 usage를 제공하지 않으면 unavailable과 이유를 남기며, 이를 완전한 crash-safe 수집으로 표현하지 않는다.

provider의 `last`·`total` 원형과 원시 scope를 보존한다. provider가 turn 단위를 명시한 경우에는 해당 값을 사용한다. 새 빈 thread 생성과 그 첫 turn의 receipt가 확인된 경우에는 그 thread의 raw total 전체를 해당 유일한 turn에 귀속하고 `first_empty_thread` 근거를 남긴다. 이는 provider가 `total`을 turn 단위로 보장한다는 뜻이 아니다. 재개 turn의 누적값은 차분·임의 배분하지 않고 raw 관측과 unavailable로 남긴다. 새 Worker unavailable의 token 필드는 null이며 measured zero와 구분한다. 과거 usage 및 기존 계측 계약 표시가 없는 turn은 자동 backfill하지 않는다. 전체 역할 계측·Goal 집계·예산 집행은 별도 완료 단위다.

## 6. Skeleton-first Planning

```text
Goal Contract 정규화·독립 검토
→ 관련 State Projection과 Project Map 수집
→ Skeleton 1~3개 생성
→ 결정적 coverage·grounding·DAG·cycle·scope Gate
→ compact semantic review
→ dedupe·dead-end·dominance pruning
→ 최대 2개 shortlist
→ shortlisted Skeleton만 Plan Contract 후보로 상세화
→ 5개 Hard Gate와 위험별 review
→ admissible 후보만 score·비교
→ 사용자 활성화
```

기본 search budget은 다음과 같다.

- 논리 역할 호출 최대 14회
- 전체 candidate version 최대 5개
- 초기 후보 최대 3개
- shortlist 최대 2개
- 후보별 refinement 최대 1회
- replan reserve 25%

명확한 단일 변경은 후보 1개만 만든다. 실제 trade-off가 있을 때만 2~3개를 생성한다. 첫 feasible plan을 확보한 뒤 남은 budget에서만 anytime improvement를 수행한다.

Task의 `contributes_to`와 Skeleton의 `goal_coverage.task_refs`는 AC 충족에 기여하는 산출물·근거의 연결이다. 해당 Task가 연결된 AC의 모든 검사 절차를 직접 실행한다는 뜻은 아니다. 모든 Task 완료 후의 독립 Goal Test AC도 관련 산출물을 제공하는 Task와 연결하고, 상세 Plan의 `goal_coverage.validation_ids`에서 `integration_validations`의 검사 ID로 연결한다. 필요하면 Skeleton의 `detail_requirements`에 이 책임을 명확히 한다. Skeleton 단계에서 Goal Test 전용 Task나 상세 integration validation 필드가 없다는 이유만으로 추가 Task를 요구하지 않는다. Goal과 AC 기여 관계로 이미 전달된 요구는 선택 `detail_requirements`의 반복 부재만으로 차단하지 않는다. 후속 상세화에서 누락될 수 있다는 가정은 현재 결함의 직접 evidence가 아니다. 실제 AC 기여 누락·Task 요구 충돌이나 상세 Plan의 독립 검사·evidence mode·validation ID 연결 결함은 계속 검토한다.

생성·검토·보정·상세화 역할은 이 책임 경계를 공유한다. 테스트 작성·실행이나 선행 산출물의 독립 검토처럼 목적이 있는 Task 검증은 허용한다. 같은 대상을 검사한다는 이유만으로 Task 검증과 독립 Goal Test를 중복으로 판정하지 않는다. 반면 일반 Task가 자신을 포함한 모든 Task의 검증 완료 또는 이후 Core Goal Test 결과를 선행조건으로 요구하면 직접 evidence가 있는 계약 충돌로 검토한다. 자연어 자기의존을 명시적 DAG cycle이나 실제 runtime 교착으로 단정하지 않는다. refiner는 finding을 Goal과 단계별 책임에 대조하고, expander는 남아 있는 충돌을 Task 삭제·재정의로 숨기지 않으며 기존 의미 보존 검사와 독립 review를 유지한다.

Goal이 각 Task 또는 특정 범위 Task의 완료 전에 요구한 검증은 AC 기여 관계와 별개인 Task 자체의 필수 책임이다. 상세화는 Goal의 적용 범위를 각 Task에 대조하고 해당 `Task.validations`에 검사 목적·method·필수 evidence 종류를 보존한다. `detail_requirements`나 Task의 AC 연결에 반복되지 않아도 Goal의 명시적 요구는 유지한다. 후속 검증 Task·`integration_validations`·완료 조건 문장 또는 `independence_required` 모델 배정만으로 이를 대체하지 않는다. Core는 선행 Task 자체의 검증을 통과한 뒤 dependency를 해제하므로 선행 Task의 완료에 필요한 evidence를 후속 Task에 의존하게 만들지 않는다. 명시적으로 요구한 실제 테스트·파일 범위·독립 모델 검토에는 적용 대상 Task의 deterministic command/test·file/diff 및 semantic model_review 검사를 둔다. Goal의 요구 밖 Task에 이 검사 종류를 일괄 강제하지 않는다. Reviewer는 상세 Plan의 실제 누락을 Goal과 해당 Task의 validation 계약으로 검토하며, Skeleton 선택 필드의 반복 부재를 결함으로 승격하지 않는다.

### 6.1 Reviewer와 Core 판정

Reviewer 출력은 다음으로 제한한다.

- `finding_code`
- 직접 `evidence_ref`
- `affected_task`
- `remediable`
- finding이 없을 때의 항목별 rating

Reviewer schema에는 `status`, `admissible`, `score`, `weakest_task` 필드가 없다. unknown field는 거부한다. finding이 하나라도 있으면 rating을 함께 제출할 수 없고, Core는 해당 후보를 `needs_revision` 또는 `rejected`로 계산한다. 따라서 “결함을 찾았지만 admissible”인 모순 상태를 표현할 수 없다.

결함 코드는 직접 증거가 있는 최소·구체적 진단만 허용한다. evidence ref와 affected Task ref는 실제 reviewer 입력 catalog와 평가 대상 Task 집합에 존재해야 한다. 같은 증상에서 파생한 상관 결함은 별도 증거가 있을 때만 추가한다. 외부 outcome의 deterministic finding과 decision은 권위값으로 받지 않고 Core가 원장의 Goal·State·Project Map·Skeleton로 재계산한다. Hard Gate를 모두 통과한 후보만 score를 얻는다.

## 7. 모델 배정과 Runtime

- 실제 모델 이름은 제품 코드에 하드코딩하지 않는다.
- 호출자는 역할별 기본 model/effort와 허용 fallback을 제공한다.
- 실제 호출 직전에 App Server `model/list`를 읽고 inventory digest를 결속한다.
- 지원되지 않는 모델이나 effort를 조용히 다른 값으로 바꾸지 않는다.
- 실행과 검사는 별도 역할로 배정하며, 중요한 작업에서는 서로 다른 역할 설정을 우선한다.
- model 변경 재시도는 새 Attempt 또는 새 Plan revision에 이유와 receipt를 남긴다.

현재 transport는 `CodexRuntimePort` 뒤에 둔다. 새 엔진은 기존 `flowmarshal.core`나 `flowmarshal.planning` 도메인을 import하지 않는다.

로컬 Codex task와 역할 thread는 실제 유효 정책 `:danger-full-access`, `approval_policy=never`에서만 시작한다. 첫 파일 조회나 명령 실행 전에 실제 config와 permission profile을 검증하며, 다르면 권한 상승을 요청하지 않고 `PERMISSION_POLICY_MISMATCH`로 종료한다.

전체 권한은 Planner나 Worker가 Core 상태를 변경할 수 있다는 뜻이 아니다. 권위 경계는 축소 OS sandbox가 아니라 typed 입력·출력, Core capability 미제공, digest와 receipt 검증으로 유지한다.

localhost와 인터넷을 일괄 차단하지 않는다. Task가 필요한 연결과 외부 효과를 계약에 표시한다. 배포·삭제·공개·외부 메시지·권한 확대처럼 비가역적이거나 제3자에게 영향을 주는 효과만 실행 직전 checkpoint를 둔다.

## 8. 원장과 상태 전이

새 엔진은 별도 SQLite 원장과 artifact root를 사용한다.

- 기본 DB: `.flowmarshal-engine/flowmarshal-engine.sqlite3`
- SQLite application ID: `0x464D4531` (`FME1`)
- 기존 prototype DB의 제자리 migration: 금지
- History: append-only hash chain
- revision payload: immutable trigger로 변경·삭제 차단
- 외부 효과: prepare intent → provider call → receipt/binding 순서

대화나 모델의 완료 선언만으로 Task를 완료하지 않는다. Task validation과 plan-level Goal Test를 분리하며, 모든 필수 Task·criterion·integration validation evidence가 확인된 뒤에만 Goal을 `satisfied`로 판정한다.

Task·Goal validation 계약과 Execution Spec의 `required_evidence_kinds`는 `EvidenceKind`의 실제 지원 집합으로 제한한다. 같은 집합을 provider JSON Schema에 공개하고 Core의 입력 검증에도 적용한다. 구체적인 검사 목적은 statement에 기술하며 새로운 evidence 종류를 임의로 만들어 실행 준비 시점까지 넘기지 않는다. 기존 유효 문자열의 canonical 표현은 유지한다.

필수 외부 사실(계약 문서·계정·삭제 selector)과 계획이 제안할 설계 선택(대안·새 산출물 배치·검증 명령)을 구분한다. 전자는 근거가 없으면 질문·차단하고, 후자는 사용자 Goal과 관찰된 프로젝트의 범위에서 정한다. materialization에서 확정할 운영 상세의 미확정만으로 Goal을 차단하지 않는다.

`IntegrationValidationContract.evidence_mode`의 기본값은 `independent`다. Task 완료 후 Core가 최신 Plan·State·Project Map에 독립 Goal Test의 운영 상세 binding을 만들고 실제 명령 또는 별도 Validator 관측을 기록한다. Task evidence를 다시 합산하는 검사는 Plan에 `task_aggregate`가 명시된 경우에만 수행한다. 운영 상세가 같은 의미를 유지하는 한 Plan을 다시 승인하지 않지만 binding 이후 입력 변경은 `STALE_EXECUTION_INPUT`으로 차단한다.

최종 GoalVerdict가 없는 상태에서 독립 deterministic Goal Test의 환경·명령 상세를 복구할 때는 기존 실패 결과 ID, 직접 실패 evidence ID, 원인 분류와 이유, 변경된 검사 명세를 명시적으로 제출한다. Core는 최신 FAIL과 evidence 소유 관계, 기존 binding, 동일 validation ID·method·필수 evidence 종류, 현재 입력 freshness와 최대 두 번의 복구 한도를 확인한다. 환경 원인은 요청자가 제출한 분류이며 exit code만으로 Core가 추정하지 않는다. 새 binding과 재시도 History는 이전 실패를 연결하고 원래 evidence·validation을 보존한다. 새 결과가 나오기 전에는 과거 실패를 새 binding의 실행 결과로 취급하지 않으며, 정상 command intent·receipt와 새 validation을 기록한 뒤 최종 Goal을 판정한다. 같은 명세 반복, 불명확한 효과의 재실행과 terminal verdict 이후 덮어쓰기는 허용하지 않는다. 검사 의미·완료 조건 변경은 여전히 새 Plan이 필요하다.

실행 상세화 역할과 검증 명령도 기존 append-only History의 `operation.prepared` → 외부 호출 → `operation.completed`에 결속한다. 완료 관측을 기록한 뒤 중단되면 이를 재사용하고, 완료 관측이 없는 효과는 `external_unknown`으로 보존한다. 다른 입력을 제출해 불명확한 이전 효과를 우회하지 않는다. 이 보수적 복구는 exactly-once 보장이 아니다.

dependency를 만족한 Task만 `ready`가 된다. 먼저 프로젝트별 직렬 실행을 적용한다. 병렬 실행은 resource lock과 충돌 검증이 qualification을 통과한 뒤 선택적으로 연다.

## 9. 실행과 복구

```text
ready Task
→ Execution Spec materialize
→ precondition·snapshot·context·effect checkpoint
→ Attempt reserve
→ intent 기록
→ thread start/resume와 turn start
→ receipt·binding
→ 결과 관측
→ Task validation
→ State 재관측
→ Goal Test
→ Continue | Task Repair | ExecutionSpec Revision | Subgraph Replan | Goal Revision
```

실패 분류는 다음 일곱 가지다.

| 분류 | 기본 처리 |
|---|---|
| `implementation` | 같은 Task 의미 안에서 Task repair |
| `context` | Execution Spec과 Context Pack revision |
| `task_contract` | 영향을 받은 subgraph 재계획 |
| `dependency` | dependency subgraph 재계획 |
| `environment` | 환경을 복구한 뒤 동일 계약 재개 |
| `requirement_change` | 새 Goal revision |
| `external_unknown` | 기존 intent·binding·receipt 우선 대조 |

동일 실패 재계획은 최대 2회, Goal 전체 재계획은 최대 5회다. 횟수는 원장에서 계산하며 호출자가 제공한 값을 신뢰하지 않는다. 첫 재계획 이후에는 새 evidence 없는 반복을 차단한다.

PC 종료, thread 생성 결과 불명, turn 중단 뒤에는 새 task를 추측 생성하지 않는다. unreceipted intent를 `external_unknown`으로 표시하고 기존 provider operation·thread binding을 먼저 관측한다. 마지막 validated checkpoint에서만 재개한다.

## 10. 개발·공개 인터페이스

1.0 전에는 `flowmarshal-engine` CLI를 사용한다.

```text
project init|show
goal create|revise|show
plan search|compare|activate|status
task show|materialize
run once|status
attempt show|retry|resume|interrupt
validate task|goal
recover inspect|resume|abandon
report progress|final
```

`flowmarshal` 이름으로의 CLI·package 승격은 cutover Gate가 통과한 뒤에만 한다. 기존 prototype CLI는 감사 재현 도구로 남기고 새 제품 엔진에서 import하지 않는다.

## 11. Qualification과 cutover Gate

다음 네 범위는 독립 artifact와 immutable evaluation contract를 가져야 한다.

1. strict schema·DAG·ledger의 결정적 검사
2. R3.1 실패 fixture를 포함한 실제 Goal/Reviewer 역할 평가
3. Skeleton 생성부터 Plan 선택까지의 전체 실제 모델 pipeline과 순서 변형
4. 활성화·실행·Task validation·Goal Test·중단 후 복구를 포함한 실제 프로젝트 E2E

각 evaluation cell은 `(fixture digest, order seed)`에 결속하고 원시 structured assessment와 Runner receipt를 저장한다. fixture, prompt, schema, threshold, taxonomy 또는 model lock digest가 다르면 checkpoint를 재사용할 수 없다. 사용량 한도 중단은 완료 cell이 아니다.

기능 Gate와 함께 같은 입력의 R3.1 baseline 대비 다음 token/latency Gate를 확인한다.

- multi-path planning token 중앙값 30% 이상 감소
- 전체 평균 token 20% 이상 감소
- single-path token 회귀 최대 5%
- 상세화됐지만 실행되지 않은 Task 비율 최대 10%
- 폐기 후보 상세 출력 비율 최대 25%
- Time to First Feasible Plan 중앙값 20% 이상 개선

정상 결과가 질문·차단인 시나리오는 최초 feasible plan 시간이 없으므로 이 지표에서는 제외한다. 해당 값은 `null`이고 질문·차단 판정 지연을 별도로 기록하며, 기존 token 비교에는 포함한다. 정상 Plan을 요구한 입력이 실패하면 시간 표본에서 조용히 제외하지 않고 기능 Gate 실패로 남긴다. 차단 지연에는 별도 합격선을 임의로 추가하지 않는다.

성능 비교의 중립 입력은 요청과 실제 fixture 파일·프로젝트 정책을 함께 digest에 결속한다. 상세 Task는 파일·명령을 확정한 운영 실행 명세이며 semantic TaskContract를 세지 않는다. 최초 feasible 시각은 Core admission 직후, 후보별 상세 출력은 expander/refiner receipt에서 측정한다. batched Skeleton token을 임의로 후보별 배분하거나 사용량이 없는 호출을 0으로 채우지 않는다. R3.1 호출은 Engine 밖의 별도 benchmark harness에서 수행하며 원본 source와 동결 판정을 변경하지 않는다.

결정적 테스트나 합성 smoke는 구현 검증이지 1.0 qualification을 대신하지 않는다. 네 범위와 token/latency Gate가 모두 통과하기 전에는 package와 기본 CLI를 `flowmarshal`로 승격하지 않는다.

## 12. Legacy 동결과 migration 정책

- R1~R3.1 source와 해당 artifact는 수정·삭제하지 않는다.
- R3.1 campaign을 GO로 만들기 위한 추가 보정이나 재실행은 하지 않는다.
- 완료된 campaign의 실패 사례만 provenance와 함께 새 회귀 fixture로 복사한다.
- 기존 prototype DB를 자동 또는 제자리 migration하지 않는다.
- migration 수요가 확인되면 안정화 후 검증된 일회성 import 도구를 별도 계획으로 만든다.
- 역사적 문서의 당시 판정은 감사 기록으로 유지하되 현재 제품 상태의 권위로 사용하지 않는다.

## 13. 구현 단계

| 단계 | 산출물과 종료 조건 |
|---|---|
| M0 | R3.1 최종 수치 정정, legacy 동결, 회귀 fixture, schema/DB/cutover ADR |
| M1 | strict authority schema, 별도 SQLite, digest·History·intent/receipt, 결정적 불변조건 |
| M2 | Goal 독립 검토, Project Map, State Projection, Context Selector, prompt digest |
| M3 | Skeleton-first bounded search, Core-derived 판정, shortlist-only expansion, 5 Hard Gate |
| M4 | `model/list` 배정, exact digest activation, lazy Execution Spec, Runtime Port와 dispatcher |
| M5 | Task/Goal validation 분리, 실패 분류, 제한 재계획, crash recovery와 최종 보고 |
| Cutover | 네 실제 qualification 범위와 token/latency Gate 통과 후 package·CLI 1.0 승격 |
