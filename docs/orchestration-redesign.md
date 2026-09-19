# FlowMarshal 전면 재설계 권위 문서

- 상태: **현재 제품 설계 기준선 / 1.0 변경 계약 planned**
- 승인된 12항목·필수 검증·구현 연결: [1.0 승인 계약](redesign-1.0-contract.md). 새 계약의 구현·검증 완료를 주장하지 않는다.
- 최신 명시 승인이 과거 사용자 제공 지침·프로젝트 조항보다 우선하며 상충하는 승인·사용량·비교 성능 규칙은 이 문서와 연결 계약으로 대체한다.
- 적용 대상: `flowmarshal.engine`
- 역사적 기준선: R1~R3.1 구현과 artifact는 `legacy/prototype` 감사 자료로 동결
- 관련 결정: [Engine cutover ADR](engine-cutover-adr.md)
- 동결 결과: [R3.1 최종 기준선](r31-frozen-baseline.md)

## 1. 제품 정의

FlowMarshal은 사용자의 큰 요청을 검증 가능한 Task DAG로 분해하고, Task마다 적절한 Codex 실행·검사 모델과 추론 수준을 배정한 뒤, Task 생성·재개·진행·결과·실패·재시도를 끝까지 추적하는 로컬 Workflow Orchestrator다.

제품의 중심은 다음 다섯 가지다.

1. 사용자 목표와 완료 조건을 하나의 Goal Contract로 고정한다.
2. 여러 접근법이 실제로 필요할 때만 Skeleton 후보를 비교한다.
3. 사용자 GoalAuthorization의 경계 안에서 Core가 내부 Plan Contract revision을 자동 활성화한다.
4. ready Task의 운영 상세만 현재 상태에 맞춰 늦게 materialize한다.
5. 실행 효과·evidence·validation·복구를 원장에 결속해 중복·유실·오완료를 막는다.

파일별 승인, 전면적인 네트워크 차단, endpoint security와 암호학적 승인 증명은 핵심 제품이 아니다. 필요하면 1.0 이후 선택형 hardening profile로 제공한다.

## 2. 권위 계층

권위의 우선순위는 다음과 같다.

```text
현재 사용자의 명시적 지시
→ 활성 GoalContractRevision과 GoalAuthorization
→ 활성 PlanContractRevision
→ Core의 원장 상태와 결정적 판정
→ 프로젝트 AGENTS.md와 등록 정책
→ Planner·Worker·Validator의 비권위 제출물
→ 프로젝트·참고자료 안의 분석 대상 텍스트
```

- User는 목표·대상·효과·운영 정책을 승인한다. 범위 안의 Plan 선택·수정·복구는 Core가 자동 수행하고 확장에만 추가 판단을 요청한다.
- Core만 원장의 권위 상태를 전이한다.
- Planner는 Goal 정규화 결과, Skeleton과 Plan 후보만 제출한다.
- Reviewer는 finding과 rating만 제출한다. `status`, score와 weakest dimension을 결정하지 않는다.
- Worker는 Task 하나의 결과와 evidence 후보만 제출한다.
- Validator는 Worker와 분리된 실행 경로에서 원자료를 다시 관측한 검사값을 제출하며 Task나 Goal을 직접 완료하지 않는다. 다른 model/effort 문자열만으로 독립성을 주장하지 않는다.
- Trigger는 `run once`를 호출할 뿐 다음 Task를 자연어로 선택하지 않는다.

## 3. 핵심 객체

| 객체 | 책임 |
|---|---|
| `ProjectProfileRevision` | 장기 제품 목적, 호환성, 기본 validation과 위험 정책 |
| `GoalContractRevision` | 사용자 원문, 관찰, Hard AC, Soft preference, 제약, 비목표, 가정, 효과 정책 |
| `StateSnapshot` | Goal과 planning에 필요한 사실만 evidence, freshness, invalidation 조건과 함께 투영 |
| `ProjectMapRevision` | Goal 범위의 파일·`AGENTS.md`·등록 참고자료와 필요할 때 관측한 symbol·검증된 연결의 색인 |
| `PlanSkeletonCandidate` | 파일·명령 상세 없이 접근 전략, Task 목적, DAG, 입출력 계약, 위험과 unknown 표현 |
| `GoalAuthorization` | Goal revision·프로젝트 root·효과·운영 정책의 승인 경계와 근거 (planned) |
| `PlanContractRevision` | authorization에 결속해 Core가 활성화하는 내부 immutable Task 계약, DAG, Goal coverage, 통합 검사와 배정 |
| `RuntimeJob`·supervisor | 활성화 후 역할 호출과 영속 checkpoint·연결·deadline·관측 관리 (planned), 완료 권위 없음 |
| `TaskExecutionSpecRevision` | ready 시점에 해석한 파일·symbol·명령·Context Pack·lock·timeout·idempotency·snapshot binding |
| `Attempt` | 하나의 실행 또는 검사 시도와 thread·turn·외부 효과 intent/receipt |
| `EvidenceRecord` | 실제 관측된 산출물·명령·검사 결과와 출처 digest |
| `GoalVerdict` | Task 결과와 plan-level Goal Test에 근거한 최종 목표 판정 |
| `BudgetUsageRecord` | provider가 제공한 단계별 nullable input/cache/output/reasoning/total token, 로컬 latency, retry와 폐기 여부 |

모든 revision과 주요 artifact는 strict frozen schema, canonical JSON과 content digest를 사용한다. Goal과 Plan의 새 revision은 같은 ID 계보의 최신 revision을 명시적으로 supersede한다. 기존 `PlanningMission`, `RequestSpec`, `EffectivePlanningPolicy`, `PlanOutcomeContract`의 의미는 `ProjectProfileRevision + GoalContractRevision`으로 통합한다. Mission 종류는 `GoalContractRevision.mission_class`라는 routing label로만 남긴다.

## 4. 승인과 Lazy Expansion 경계

### 4.1 목표 승인과 내부 Plan activation — planned

사용자는 GoalAuthorization의 목표·범위·효과·운영 정책을 한 번 승인한다. Core는 프로젝트 root·알려진 효과·정책을 결정적으로 대조하고 의미 범위의 근거 있는 review를 거쳐 Plan revision·digest를 authorization에 결속해 자동 활성화한다. 사용자가 정확한 ID·digest를 직접 입력하는 절차는 필수가 아니다. 목표·범위·효과·정책 확장에만 추가 판단을 요청한다. 파일별 승인·HMAC proof·이중 승인 장부는 요구하지 않는다. 이 결속은 OS sandbox나 의미 안전성의 수학적 보장이 아니다.

현재 console 승인 경계는 설치 entrypoint의 `TrustedConsoleHost`가 정확한 Goal revision/digest, project root, Core가 실제 활성화할 eligible selected Plan의 ID·revision·definition/activation digest, 효과·운영·budget 정책과 전체 target digest를 먼저 표시하고, 대화형 사용자가 그 digest 전체를 확인한 경우에만 같은 process의 `ApplicationAuthority`를 호출한다. `ApplicationAuthority`는 `EngineApplication`에 Core issuer를 한 번 결속하고, `EngineApplication`이 표시 target 전용 Goal capability를 발급한다. Core는 authorization transaction 안에서 capability를 첫 시도에 소모한 뒤 selected Plan을 포함한 최신 target을 다시 계산해 타입·원장·프로젝트·digest를 검사한다. 거절·EOF·비대화형 입력·Goal/root/policy/Plan 선택의 stale target·wrong type/binding·replay와 capability 없는 내부 CLI 호출은 원장을 바꾸지 않고 거부한다. 효과 checkpoint는 별도 타입의 capability에 결속한다. 이는 process-local Python application-authority 경계이며 hostile same-process 코드나 같은 OS 사용자의 raw SQLite 직접 접근을 격리하지 않는다. raw SQLite 직접 접근은 1.0 known limitation이다.

`PlanContractRevision`은 다음을 고정한다.

- Goal Contract digest와 기준 State Snapshot
- Task별 목적·종류와 Hard AC 연결
- dependency와 produces/consumes 계약
- 기대 효과와 금지 효과
- 위험, 외부 효과와 checkpoint 등급
- 완료 조건과 validation 요구
- recovery envelope; 미사용 CommitHorizon은 새 schema에서 제거하거나 고정 불변조건으로 대체하고 역사 reader는 보존
- 실행·검사 역할, 기본 model/effort와 명시적 fallback envelope
- plan-level integration/Goal Test

다음 중 하나가 바뀌면 새 `PlanContractRevision`이 필요하다.

- 목표·비목표·Hard AC
- Task의 의미, 추가·삭제·분할·병합
- dependency와 produces/consumes 의미
- 대상 프로젝트
- 완료 조건과 validation의 의미
- 계획에 없던 외부 부작용 또는 금지 효과

새 revision은 기존 revision을 덮어쓰지 않는다. 승인 경계 내의 Task 분할·replan은 새 Plan을 Core가 자동 활성화하며 실행 중 Attempt를 보호한다. 목표 변경에는 Goal revision과 authorization 갱신도 필요하다. 완료 evidence는 입력·대상·검사 의미·freshness를 확인한 경우만 재사용한다.

### 4.2 ready 시점에 늦게 결정하는 상세

`TaskExecutionSpecRevision`은 활성 Plan Contract의 의미를 바꾸지 않는 범위에서 다음만 늦게 결정한다.

- 실제 파일·symbol과 관련 코드 범위
- 구체 명령과 작업 디렉터리
- 현재 상태에 맞는 최소 Context Pack
- resource lock, timeout과 idempotency key
- 최신 `model/list`로 지원 여부를 확인한 requested model/effort binding

Execution Spec은 Task Contract digest, Plan activation digest, State Snapshot digest, Project Map digest와 Context fragment digest에 결속된다. 실행 직전에 이 입력을 다시 검사하고 바뀌었으면 `STALE_EXECUTION_INPUT`으로 중단한다. stale 입력을 묵시적으로 다시 승인하거나 그대로 실행하지 않는다.

Task 준비 역할과 Goal Test 준비 역할의 입력·지침·출력은 분리한다. Task 준비에는 현재 Task, 관련 Goal 조건, 선행 산출물 evidence와 프로젝트 관찰을 투영하며 다른 Task 계약이나 integration validation을 함께 전달하지 않는다. provider 전용 Task proposal은 validation ID와 운영 상세만 제출하고, Core가 활성 Task 계약의 method와 필수 evidence 종류를 결합한 뒤 기존 ExecutionSpec 검사를 적용한다. 수동 proposal의 의미·정확한 집합 검사는 유지한다. Core operation은 축약 전 권위 Context digest도 별도로 결속해, 전송하지 않은 계약의 변경으로 과거 응답을 재사용하지 않는다.

## 5. Goal·State·Context

### 5.1 Goal 정규화

사용자 요청은 한 번만 Goal 후보로 정규화한다. 별도 reviewer는 source trace와 모순·누락을 검토하고 Core가 최종 `GoalContractRevision`을 컴파일한다. Goal revision에는 normalization·review digest, reviewer role과 finding 또는 rating을 preparation binding으로 남긴다. 모델은 Goal revision 번호나 권위 상태를 직접 정하지 않는다.

완료된 준비 결과가 blocking 질문 없이 수정 가능한 `conflict`이면, 원본 요청·Profile·관측과 proposal·독립 finding을 그대로 결속한 별도 `goal_refiner`가 한 번 응답할 수 있다. 정규화와 같은 모델 설정을 사용하되 finding을 정답으로 취급하지 않는다. 변경 proposal은 같은 Goal ID의 직전 revision을 supersede하고 기존 Reviewer와 compiler를 거친다. canonical 동일 후보는 재검토하지 않으며 `disputed`·`unresolved`는 새 Goal을 만들지 않는다. 입력 부족·비수정 가능 finding은 이 경로에 들어오지 않는다. 원본 preparation, 변경 이유·evidence, request/output receipt와 새 preparation을 함께 보존하며, 호출자는 원본별 한 번의 한도와 전체 호출 예산을 적용한다.

Hard AC는 반드시 관측 가능해야 하고 출처를 갖는다. Soft preference는 점수화할 수 있지만 Hard AC를 대신하지 못한다. 비목표는 constraint 목록에 섞지 않고 명시적으로 보존한다.

정규화와 독립 검토는 지침의 출처와 적용 단계를 함께 해석한다. 현재 정규화·계획 역할에만 적용되는 파일 수정·명령 실행 금지는 승인 후 Task의 제약으로 전사하지 않는다. 사용자 원문이나 실행 단계에도 적용되는 정책이 명시한 금지는 보존한다. 파일 무변경과 정적 분석만으로 명령 미실행 증명을 새 요구로 추가하지 않으며, 특정 작업의 실행 금지를 모든 읽기·검증 명령의 금지로 넓히지 않는다. API 변경·보존 전략에는 이름·import·시그니처와 함께 문서·테스트의 정상 동작 계약을 실제 값이나 관계로 명시하고 현재 구현의 결함과 구분한다. 원인 분석의 AC에 필요한 정상 기대값과 현재 불일치가 포함돼 있으면 이를 별도 구현·호환성 보존 의무로 승격하지 않는다. 전체 Goal에 이미 명시된 의미를 특정 항목에 다시 요구하지 않는다.

Goal이 등록 검사 도구·자료의 특정 phase나 절차 실행을 명시하면 전체 AC·constraint·validation intent와 제공된 관측을 대조하여 참조 대상·범위·검사 의미를 확인한다. 명시 참조에 포함된 검사를 다른 AC에 반복하지 않은 것만으로 누락을 판정하지 않는다. 등록 자료의 존재만으로 계약 채택을 추정하지 않으며, 참조 부재·대상 또는 phase 불일치·필요한 검사 부재·불완전한 본문에서는 상속을 가정하지 않는다. 명시적 제외·충돌은 참조로 덮지 않는다. 정규화는 검사 대상·범위·목적을 식별 가능하게 보존하며 운영 명령은 ready-time 명세에서 확정한다.

Goal 준비 시 대상은 사용자 명시 대상과 프로젝트 관측의 `project_root`로 확인한다. 역할 cwd나 등록 참고자료 저장 위치는 대상 선택 근거가 아니다. 별도 역할 복사본과 관측 root의 경로 차이만으로 target 충돌·stale·selector 부재를 추정하거나 이미 관측된 대상을 새 질문·가정으로 바꾸지 않는다. 실제 사용자 대상 충돌·불명확한 참조·불완전한 관측은 계속 근거에 따라 검토한다.

허용 외부 효과는 외부 시스템·계정·제3자에 대한 효과다. 로컬 파일 변경·검증 명령·함수 반환·응답 보고와 구분한다. 기대 효과에는 실제 발생시킬 효과만 두고, 파일 무변경·외부 효과 없음은 금지 효과나 완료 조건으로 표현한다. 프로젝트 파일·의존성 변경 같은 로컬 변경 금지와 외부 서비스 변경·배포 금지는 별도 항목으로 유지한다. 한 문장으로 묶인 원문도 같은 의미로 분리하며 새 금지·허용 효과나 필연적인 네트워크·외부 계정 변경을 추정하지 않는다. 자연어의 의미 분류는 독립 검토 대상이며 문자열 키워드만으로 권위 판정을 대체하지 않는다.

파일 무변경 상태에서 분석·보고를 요청한 Goal의 보고는 Worker 응답 본문으로 제공하는 논리적 산출물로 계획할 수 있다. `produces`의 보고 key는 프로젝트 파일 생성 권한이 아니다. 프로젝트 파일의 생성·수정·삭제 금지와 새 응답 생성을 구분하고, 응답까지 전후 무변경이어야 한다는 모순된 완료 조건을 만들지 않는다. 보고 내용은 Core가 수집한 Worker 응답 관측과 원본 파일 근거를 대조하는 semantic validation으로 검증하고, 프로젝트 파일 무변경은 별도로 검사한다. 명시적인 파일 산출물 요구나 더 강한 금지 조건은 응답 보고로 대체하거나 임의 파일 쓰기 예외로 해결하지 않는다.

Task semantic 검사가 `external_observation`을 요구할 때 현재 Execution Spec의 최신 성공 실행 Attempt에 결속된 Worker 응답을 검사 대상으로 함께 제공한다. 응답의 완료 주장은 충족 증명이 아니며 보고 내용은 원본 `file` 근거와 대조한다. 검증 계약은 `model_review`, `external_observation`, `file`을 모두 요구한다. 현재 원장 관측은 10,000자까지 보존하므로 잘린 Worker 응답에는 `source_ref`의 `:truncated` 표식을 붙이고 Task·Goal semantic catalog에서 제외한다. 잘린 응답을 완전한 보고로 검증하거나 무조건 성공으로 처리하지 않는다.

### 5.2 Project Map과 State Projection

Project Map은 Goal 범위에서 다음 순서로 만든다.

```text
실제 관측 File Manifest·AGENTS·등록 자료
→ Task 준비나 ContextRequest에 필요한 symbol만 lazy 관측
→ 관측 근거가 있는 연결
→ ProjectMapRevision
```

Map은 Goal에 필요한 관측 범위의 색인이며 저장소 전체의 symbol/module graph를 미리 만들거나 완전한 의존 그래프라고 주장하지 않는다. `test`·`build` 표지는 관측 파일의 분류이고 module/test/build 관계는 직접 근거가 있을 때만 연결한다. Map 전체 revision digest와 planning 의미에 영향을 주는 semantic digest를 분리한다. Goal에 필요한 사실만 `StateSnapshot`으로 투영하고 각 사실에 evidence, freshness와 invalidation 조건을 둔다.

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

계획·검토 입력은 Goal의 명시 대상과 `ProjectMapRevision.root`를 대조한다. `reference`/`registered_reference` 자료의 저장 위치·부모 디렉터리와 역할 실행 `cwd`는 대상 프로젝트 변경의 근거가 아니다. 경로의 실행명·날짜·버전만으로 다른 workspace를 추정하지 않는다. Goal의 명시 대상 충돌, 후보의 Project Map·State digest binding 불일치와 State freshness 위반은 역할 호출 전에 Core·adapter가 해당 필드와 직접 evidence로 결정적으로 검사한다. 이 검사를 통과한 기계 메타데이터를 Reviewer에게 다시 관계 장부로 쓰게 하지 않으며, 등록 본문·Goal·Plan 사이의 의미 충돌은 별도 semantic 검토에 남긴다.

Context가 부족하면 모델이 추측하지 않고 필요한 source, selector와 이유를 담은 구조화된 추가 Context 요청을 반환한다.

정책과 required need를 먼저 선택하고 예산 적용 후에도 해당 need가 요구한 모든 매칭 source·symbol의 본문이 남아 있는지 확인한다. 한 need의 여러 path hint로 찾은 필수 자료도 일부만 포함해 성공으로 처리하지 않는다. 정책은 예산을 초과해 강제로 넣지 않으며, 선택적 전체 파일 요청이 필수 symbol의 범위를 확장해 예산을 소진하지 않도록 한다. 부족하면 누락 need와 이유를 포함한 `AdditionalContextRequest`를 반환하고 Execution Spec·Attempt·Worker를 생성하지 않는다. 허용된 로컬 ContextRequest는 초기 sample 밖까지 자동 탐색해 해소한다. Task 분할은 새 Plan revision으로 다루고 승인 경계 내에서는 검토·Gate 후 자동 활성화한다. 접근 불가 사실·사용자 취향·승인 경계 확장에만 질문한다.

Python symbol은 AST의 실제 정의 범위를 선택한다. decorator·async 함수·클래스·한정된 메서드 이름을 포함하고 겹치는 범위를 합친다. selector는 1기반 양끝 포함 `python-lines:start-end[,start-end]`를 사용한다. 경로만 요청하거나 지원하지 않는 형식·파싱 불가 파일은 `whole-file`로 표시한다. 유효한 Python에서 요청한 symbol이 없으면 경로만 일치한다는 이유로 충족했다고 간주하지 않는다. 범위가 selector 표현 한도를 넘으면 내용 일부를 버리지 않고 전체 파일로 확장해 예산을 다시 검사한다.

Context fragment의 `content_digest`는 파일 전체의 byte digest를 유지한다. 선택과 Prompt 조립이 같은 범위 복원 함수를 사용하고 source·selector·실제 본문을 Prompt binding에 결속한다. 선택 이후 파일이 바뀌면 조립과 실행 예약을 차단한다. `token_estimate`는 선택 문자열의 UTF-8 byte 수를 4로 나눈 올림값이며 4,000-token 상한으로 자르지 않는다. 이는 Context 본문에 대한 휴리스틱으로, Prompt의 다른 영역이나 provider 실측 사용량을 대신하지 않는다.

Worker PromptBundle에는 전체 Task 계약, Execution Spec의 운영 상세 projection과 선택 Context 본문을 포함한다. projection에서 `context_manifest.prompt_binding`과 파생 spec digest를 제외하여 자기참조를 막는다. 네 segment digest를 가진 binding의 canonical digest로 artifact를 식별하고, 완성된 임시 파일을 덮어쓰기 없는 원자적 게시로 저장한 뒤 명세를 등록한다. 게시 뒤 DB 등록 전에 중단되면 권위 명세 없는 artifact만 남으며 실행 권한이 되지 않는다. 수동 명세도 Core 조립 결과와 binding이 일치해야 한다.

Dispatcher는 초기 실행과 기존 thread 재개 모두 저장된 bundle의 binding·segment digest를 검증하여 실제 본문을 전송한다. 누락·변조 시 임의 Prompt나 과거 Task·Spec 문자열 조립으로 우회하지 않는다. Worker가 변경한 파일로 재개 Prompt를 다시 만들지 않고 원래 저장 본문을 유지하며, 재개 안내문까지 포함한 최종 전송 문자열의 canonical digest를 turn intent의 `prompt_digest`에 기록한다. semantic Validator는 Worker와 다른 Attempt·RuntimeJob·thread/turn에서 원자료를 새로 관측해 자체 terminal provider receipt와 `model_review` evidence binding을 구성한다. `SemanticValidationObservation`과 `ValidationResult`의 PASS/FAIL, validation/task ID와 content digest는 정확히 일치해야 하며, 사용한 evidence는 재사용하거나 재결속하지 않는다. Worker의 자연어 설명이나 evidence 후보는 재관측 없이 독립 evidence가 되지 않는다. 이 artifact 무결성 계약은 OS 권한을 제한하는 보안 경계나 저장장치 전원 장애에 대한 완전한 내구성 보장이 아니다.

### Worker 완료 usage의 영속 연결

Worker의 새 turn intent에는 기존 Prompt binding과 Execution Spec digest, 최종 전송 문자열 digest, 별도의 UTF-8 byte/4 Prompt token 추정치를 기록한다. adapter는 SDK에 실제로 넘긴 문자열의 digest를 start receipt에 남긴다. Core는 이 receipt와 완료 관측의 thread·turn·Prompt를 대조한 뒤 기존 `BudgetUsageRecord`에 원시 관측, 관측 digest, Attempt·intent·receipt 참조를 기록한다. 같은 provider thread/turn의 재관측은 한 행을 재사용하고 다른 실제 turn은 별도 행으로 보존한다. 충돌하는 실측 값은 덮어쓰지 않는다.

연결을 소유한 프로세스는 완료 usage를 원장에 기록한 뒤 정상 종료한다. 이 기록은 Task/Attempt의 완료 판정이 아니며 기존 다음 관측 단계가 evidence 수집과 상태 전이를 담당한다. 저장된 turn 조회에 usage가 없더라도 이미 기록한 usage는 유지한다. 기록 전에 프로세스가 강제 종료되거나 SDK가 실패 usage를 제공하지 않으면 unavailable과 이유를 남기며, 이를 완전한 crash-safe 수집으로 표현하지 않는다.

provider의 `last`·`total` 원형과 원시 scope를 보존한다. provider가 turn 단위를 명시한 경우에는 해당 값을 사용한다. input·cached·output·reasoning·total 구성요소는 제공된 값만 기록하고 누락된 각 항목을 독립적인 null/unknown과 사유로 남긴다. 일부 구성요소가 없다고 제공된 다른 값을 폐기하거나 0·예약량·추정치로 채우지 않는다. 새 빈 thread 생성과 그 첫 turn의 receipt가 확인된 경우에는 그 thread의 raw total 전체를 해당 유일한 turn에 귀속하고 `first_empty_thread` 근거를 남긴다. 이는 provider가 `total`을 turn 단위로 보장한다는 뜻이 아니다. 재개 turn의 누적값은 차분·임의 배분하지 않고 raw 관측과 unavailable로 남긴다. 새 Worker unavailable의 token 필드는 null이며 measured zero와 구분한다. 과거 usage 및 기존 계측 계약 표시가 없는 turn은 자동 backfill하지 않는다. 전체 역할 계측·Goal 집계·운영 중단 정책은 별도 완료 단위다.

## 6. Skeleton-first Planning

```text
Goal Contract 정규화·독립 검토
→ 관련 State Projection과 Project Map 수집
→ Skeleton 1~3개 생성
→ 결정적 coverage·grounding·DAG·cycle·scope Gate
→ compact semantic review
→ 동일 의미 dedupe·근거 있는 dead-end 제거·목표 적합성/품질/위험 비교
→ 최대 2개 shortlist
→ shortlisted Skeleton만 Plan Contract 후보로 상세화
→ 5개 Hard Gate와 위험별 review
→ 수정 가능한 상세 실패를 근거와 함께 최대 한 번 피드백
→ 상세 revision의 재검토 또는 같은 shortlist 자리의 Skeleton 수정·검토·상세화
→ admissible 후보만 score·비교
→ GoalAuthorization 내 Core 자동 활성화
```

기본 search budget은 다음과 같다.

- 논리 역할 호출 최대 14회
- 전체 candidate version 최대 5개
- 초기 후보 최대 3개
- shortlist 최대 2개
- 후보별 refinement 최대 1회
- 과거 token replan reserve 25%는 schema 3 역사 계약으로 보존하고 새 실행 한도로 재해석하지 않음

같은 의미/canonical Plan만 dedupe하며 서로 다른 전략을 비용 추정만으로 우월 판정·가지치기하지 않는다. 명확한 단일 변경은 후보 1개만 만든다. 실제 trade-off가 있을 때만 2~3개를 생성한다. 첫 feasible plan을 확보한 뒤 남은 budget에서만 anytime improvement를 수행한다.

상세 Plan의 `needs_revision`은 수정 제안의 시작 조건이며 finding의 진실성을 승인하지 않는다. `plan_refiner`는 원본 Goal·State·Project Map·Skeleton·Plan과 직접 finding을 대조하여 `detail_revision`, `skeleton_revision`, `disputed`, `unresolved` 중 하나와 이유·직접 evidence ref를 제출한다. 상세 수정은 기존 Skeleton 의미를 보존하는 compiler를 거친다. Task 목적·DAG를 바꿔야 하는 수정은 같은 전략 계열의 새 Skeleton로 돌아가 기존 Gate와 독립 검토를 거친다. refiner의 반박이나 미해결 응답만으로는 원래 거절을 바꾸지 않으며 같은 Plan을 통과할 때까지 재호출하지 않는다. 수정 결과는 이전 평가를 보존하고 새 Gate·독립 Reviewer 검토를 통과해야만 선택된다.

복구 정책이 없는 기존 검색은 후보별 refinement 1회를 최초 Skeleton 계보 전체에서 공유한다. 명시적 `planning-recovery-v2` 검색은 Skeleton 준비와 상세 Plan 수정에 각각 1회를 배정하되 전체 14회 역할 호출·5개 candidate version·Goal 운영 한도는 공유한다. 상세 실패에서 생성하는 새 Skeleton도 상세 수정 슬롯을 소비한다. `candidate_versions`는 평가한 Skeleton과 추가로 생성한 상세 수정 후보를 세며 최초 상세화는 중복 계산하지 않는다. 변경 없는 수정 제안도 생성 시도·예산으로 보존하되 새 독립 검토는 하지 않는다. 상세 수정에는 최소 2회, Skeleton 수정 경로에는 최소 4회의 잔여 호출을 먼저 확보한다. 원장 ID·검사 ID 이름·배열 순서·비용 추정만 달라진 동일 후보는 무진전으로 중단한다. 후속 Skeleton은 기존 shortlist 자리를 이어받으며 별도의 초기 전략 슬롯을 얻지 않는다. `replan_reserve_percent`는 이 검색의 호출 한도에서 별도 감산하지 않으며 과거 token 예약·정산은 역사 reader로 보존하고 새 실행 admission과 usage 관측은 분리한다.

`planning-recovery-v2`에서는 원검토가 수정 가능하게 거절한 동일 Plan에 대해 직접 원문 반증을 제출한 `disputed`를 별도 독립 재심으로 보낼 수 있다. 최초 후보 계보별 재심은 최대 1회이며 원 Plan·Goal·원검토·반박·request/output/receipt를 결속한다. 원 finding마다 `upheld` 또는 `withdrawn`과 직접 근거를 정확히 한 번 제출하고, 유지 finding은 원객체 그대로 보존한다. 불확실성은 유지로 남기고 deterministic finding은 이 경로로 뒤집지 않는다. 추가로 발견한 직접 결함도 별도로 제출한다. Core는 이 새로운 관측으로 판정을 재계산하며 원검토·원판정·새판정을 History에 함께 기록한다. 재심은 실제 후보 수정 슬롯이나 version을 소비하지 않으며 피드백과 재심 호출·토큰은 같은 전체 운영 한도에 포함한다. 남은 상세 수정 슬롯이 있을 때만 재심에서 확인한 결함을 수정하고, 수정된 Plan이 다시 실패하면 명시적 중단 사유를 기록한다.

새 복구 정책의 후보별 schema 실패 격리는 단일 terminal turn과 유효 결과 귀속, 원 요청·receipt·정책 결속이 모두 확인된 경우에만 허용한다. 해당 후보를 성공으로 바꾸거나 같은 호출을 다시 실행하지 않고 다른 admissible 후보를 보존한다. 검색 결과에는 실패 단계·Skeleton/Plan·provider call·receipt를 기록하며 qualification의 schema/전체 판정은 FAIL을 유지한다. 운영 한도·권한·model lock·입력 무결성·불명 효과 오류는 전체를 중단한다. 기존 v1 계약과 과거 실패 artifact는 새 복구 결과로 재해석하지 않는다.

초기 후보와 초기 수정 후보의 검토 실패는 `initial_skeleton_review`, 초기 Skeleton 수정 실패는 `skeleton_refine`이다. 두 operation은 원본 Skeleton digest가 필수이고 Plan digest는 없어야 한다. 기존 `skeleton_review`는 상세 Plan 복구에서 반환된 수정 Skeleton을 재검토하는 단계로서 Skeleton과 원본 Plan을 모두 요구한다. 최초 일괄 생성 실패는 전체 중단한다. 초기·상세 검토 실패는 `semantic_submission=None`인 `REJECTED`를 남기고 성공 제출의 동시 기록, 추가 수정, shortlist와 선택을 차단한다. 초기 수정 실패는 원본 후보·판정을 보존하고 수정 기회를 한 번 소모한다. 정산 실패 호출은 호출 수·비용·수정 시도에 한 번만 포함하며 완성된 새 후보가 없으면 `candidate_versions`를 늘리지 않는다.

원장은 `skeleton_refine`을 실제 요청의 `payload.candidate`로, 검토는 `payload.evidence_catalog["artifact:skeleton"]`로 대조하며 Goal·provider call·receipt 결속을 함께 검사한다. 후보 계보의 성공·실패 수정 이력은 합산하고, 효과 전 해제된 호출만 소비에서 제외한다. 동일 후보 또는 ID만 달라진 동일 의미의 실패 요청은 새 검색에서도 provider 호출 예약 전에 차단한다. 이미 저장된 동일 검색의 재등록은 멱등적이지만, 실패를 누락하거나 성공으로 바꾸거나 수정 슬롯을 되살린 새 검색은 거부한다. 이것은 입력·효과가 불명확한 호출을 자동 재실행하는 허가가 아니다.

같은 Plan 계보의 수정은 동일 `plan_id`, 증가한 `revision_no`, 직전 `supersedes_plan_revision_id`를 가진다. 원장의 전역 Task ID는 새로 생성하며 `task_ref`와 의미 관계로 후보를 비교한다. 검색 입력·모델 inventory 결속을 수정으로 교체하지 않는다. 실제 refiner의 요청·응답·receipt digest와 원본 평가·제안 digest는 프로그램이 계산해 결속하며 모델에게 다시 작성시키지 않는다. `PlanningSearchOutcome`의 원본 실패·수정·중단 이유·선택 결과는 등록된 후보와 Core 판정에 대조한 뒤 `planning.search_recorded` History에 보존한다. 외부 outcome 입력과 합성 회귀는 실제 provider receipt 없는 상태를 그대로 보존한다.

Task의 `contributes_to`와 Skeleton의 `goal_coverage.task_refs`는 AC 충족에 기여하는 산출물·근거의 연결이다. 해당 Task가 연결된 AC의 모든 검사 절차를 직접 실행한다는 뜻은 아니다. 모든 Task 완료 후의 독립 Goal Test AC도 관련 산출물을 제공하는 Task와 연결하고, 상세 Plan의 `goal_coverage.validation_ids`에서 `integration_validations`의 검사 ID로 연결한다. 필요하면 Skeleton의 `detail_requirements`에 이 책임을 명확히 한다. Skeleton 단계에서 Goal Test 전용 Task나 상세 integration validation 필드가 없다는 이유만으로 추가 Task를 요구하지 않는다. Goal과 AC 기여 관계로 이미 전달된 요구는 선택 `detail_requirements`의 반복 부재만으로 차단하지 않는다. 후속 상세화에서 누락될 수 있다는 가정은 현재 결함의 직접 evidence가 아니다. 실제 AC 기여 누락·Task 요구 충돌이나 상세 Plan의 독립 검사·evidence mode·validation ID 연결 결함은 계속 검토한다.

생성·검토·보정·상세화 역할은 이 책임 경계를 공유한다. 테스트 작성·실행이나 선행 산출물의 독립 검토처럼 목적이 있는 Task 검증은 허용한다. 같은 대상을 검사한다는 이유만으로 Task 검증과 독립 Goal Test를 중복으로 판정하지 않는다. 반면 일반 Task가 자신을 포함한 모든 Task의 검증 완료 또는 이후 Core Goal Test 결과를 선행조건으로 요구하면 직접 evidence가 있는 계약 충돌로 검토한다. 자연어 자기의존을 명시적 DAG cycle이나 실제 runtime 교착으로 단정하지 않는다. refiner는 finding을 Goal과 단계별 책임에 대조하고, expander는 남아 있는 충돌을 Task 삭제·재정의로 숨기지 않으며 기존 의미 보존 검사와 독립 review를 유지한다.

Goal의 AC 또는 전역 constraint가 각 Task 또는 특정 범위 Task의 완료 전에 요구한 검증은 AC 기여 관계와 별개인 Task 자체의 필수 책임이다. 상세화는 Goal의 적용 범위를 각 Task에 대조하고 해당 `Task.validations`에 검사 목적·method·필수 evidence 종류를 보존한다. `detail_requirements`나 Task의 AC 연결에 반복되지 않아도 Goal의 명시적 요구는 유지한다. 후속 검증 Task·`integration_validations`·완료 조건 문장 또는 `independence_required` 모델 배정만으로 이를 대체하지 않는다. Core는 선행 Task 자체의 검증을 통과한 뒤 dependency를 해제하므로 선행 Task의 완료에 필요한 evidence를 후속 Task에 의존하게 만들지 않는다. 명시적으로 요구한 실제 테스트·파일 범위·독립 모델 검토에는 적용 대상 Task의 deterministic command/test·file/diff 및 semantic model_review 검사를 둔다. Goal의 요구 밖 Task에 이 검사 종류를 일괄 강제하지 않는다. Reviewer는 상세 Plan의 실제 누락을 Goal과 해당 Task의 validation 계약으로 검토하며, Skeleton 선택 필드의 반복 부재를 결함으로 승격하지 않는다.

### 6.1 Reviewer와 Core 판정

검사 연결은 먼저 모든 검사 문장의 복합 책임과 등록 수단·phase의 실제 절차를 확인하고, 다음 전역 constraint가 요구한 Task 자체 validation, 다음 Goal의 statement·validation_intent·적용 범위, 마지막 현재 연결과 finding을 양방향 대조한다. AC가 동일 절차의 task/goal phase를 각각 명시하면 명시된 각 phase를 실제 수행하는 validation은 각각 필수 연결이며, 별도 실행은 실행·evidence 분리일 뿐 task phase를 선택 사항으로 만들지 않고 이 규칙을 명시되지 않은 sibling 검사에 전염시키지 않는다. AC가 절차 자체를 직접 요구한 경우와 특정 도구·phase 실행을 요구하여 그 실제 phase가 절차를 포함하는 경우를 구분하며, 같은 목적의 별도 검사나 다른 phase까지 확대하지 않는다. 전역 constraint가 요구한 Task 자체 validation의 존재와 AC가 명시한 절차의 validation ID 연결은 별도 판정이다. ID 이름이나 이미 연결된 대표 검사만으로 AC가 명시한 필수 검사 ID를 생략하지 않으며, 전역 semantic 의무·검사 문장의 연관 표현·evidence 종류나 단순 선후조건만으로 모든 AC에 연결을 강제하지 않는다. 연결만 빠졌다면 실행 누락이나 새 검사 의무로 확대하지 않는다. 상세화와 Reviewer는 같은 기준을 공유하며 Goal·검사 원문·현재 coverage를 직접 근거로 사용한다.

Worker의 작업·응답 제출, 이후 Task 검증과 Core의 완료 판정을 구분한다. Task 완료 조건에 독립 Validator 통과를 요구할 수 있지만, Worker 응답을 입력으로 뒤에 수행하는 Validator의 결과를 같은 Worker가 미리 제출하도록 요구하지 않는다. 검증된 선행 Task 결과의 후속 인용은 허용하며 자연어 시점 충돌을 실제 runtime 교착으로 단정하지 않는다. produces·consumes·preconditions·완료 조건과 validation 입력을 함께 대조하여 Worker 실행 보고와 Validator의 별도 검사 결과를 구분한다.

`Task.produces`는 검증까지 포함한 Task 전체의 논리적 산출물 key이며 Worker 응답의 필수 항목 목록이 아니다. 독립 Validator의 별도 결과를 Task 산출물로 선언할 수 있다. 실제 Worker 제출 요구·작성 주체·시점 충돌은 계약 문장과 후속 검사의 입력으로 판단하며 key 이름으로 추정하지 않는다. v2의 생성·검토 요청은 `task_result_field_semantics`에 이 기존 필드 의미를 함께 결속한다. 이 입력 설명은 개별 계약의 정상·결함 판정이나 새로운 evidence가 아니며 원본 계약·직접 finding·citation 선택을 바꾸지 않는다. v1 요청·schema와 기존 권위 객체의 형식은 유지한다.

검증 계약의 `statement`는 Goal이 명시한 검사 대상·종류·실행 목적을 보존한다. `required_evidence_kinds`의 `test`는 evidence 종류이며 특정 검사 절차를 보장하지 않는다. 기존 unittest 실행을 요구했다면 적용 대상 Task의 검사 문장에도 해당 실행·통과 확인을 보존하고 일반 동작 검사로 바꾸지 않는다. 실제 명령은 ready-time Execution Spec에서 확정한다.

상세 Plan의 Task·integration validation이 등록 검사 도구·자료의 phase·mode·절차를 참조하면 상세화와 Reviewer는 등록 경로의 관련 본문과 필요한 구현 분기를 읽어 실제 검사 범위를 대조한다. 같은 도구의 다른 phase가 수행하는 검사를 합쳐 설명하거나 선언·시그니처 검사를 실제 입력·호출 방식 검사로 확대하지 않는다. Goal의 검사 목적과 수단의 실제 능력을 구분하며, 부족한 필수 검사는 별도 실제 검사 책임으로 보존한다. Goal이 Task에 요구하지 않은 검사를 일괄 추가하지 않고 정상 Task 검사와 독립 Goal Test의 범위 차이를 허용한다. 도구·phase 참조로 검사 의미를 식별하는 것은 계획 단계에서 허용하되 argv 등 운영 상세는 ready-time에 확정한다. 이미 명시된 phase와 검사 의미의 충돌은 Plan Contract 결함이며 새 revision으로 수정한다. 자료 부족과 직접 확인된 모순을 구분하고 원래 검사 의무나 합격선을 약화하지 않는다.

별도 검사 책임은 같은 validation ID·문장 안에도 둘 수 있다. 이때 도구 실행에 더해 무엇을 실제 실행하고 어떤 기대 결과와 비교하는지 식별해야 하며 새 ID나 argv를 강제하지 않는다. 도구가 관측하지 않는 목적을 도구 실행의 결과로 덧붙이는 문장은 별도 책임이 아니다. 독립 실행과 범위 충분성은 별개이므로 같은 잘못된 phase를 새로 실행해도 범위 모순은 해소되지 않는다. Reviewer는 전체 검사 문장을 대조해 직접 확인한 모순의 validation ID·수단·주장과 evidence를 finding으로 반환한다. 낮은 rating이나 다른 결함에 의한 후보 차단은 그 모순의 검출을 대신하지 않는다. 최소 finding 원칙은 중복·추측을 배제하는 원칙이며 별도 직접 증거가 있는 독립 결함을 생략하는 근거가 아니다. provider용 Plan 검토 schema의 필드 설명은 이 경계를 안내할 수 있지만 Core의 finding·rating 상호배타성, 판정 권위나 점수 정책을 바꾸지 않는다.

Plan Reviewer에는 모든 Task·integration validation을 원래 순서대로 펼친 비권위 검사 색인과, AC의 statement·validation_intent 및 전역 constraint를 구분한 Goal 원문 색인을 함께 제공할 수 있다. 두 색인은 ID·순서·selector·원문과 현재 AC 연결만 투영하고 필수 연결·phase 능력·runtime evidence 범위의 판정을 추가하지 않는다. `required_evidence_kinds`는 각 validation 계약의 필요 evidence 종류이지 이후 semantic Validator 직접 catalog의 허용 목록이 아니다. 현재 연결과 필요한 연결은 구분하며, 전역 constraint나 단순 선후조건을 근거로 모든 검사 ID를 모든 AC에 연결하지 않는다. finding의 evidence ref는 원본 catalog만 사용하고 색인은 새 권위나 evidence가 아니다. Task·integration 작성 draft의 statement 설명도 수단별 범위를 안내하되 원래 권위 validation schema의 필드와 값 제약을 유지한다.

상세 Plan의 `goal_coverage.task_ids`는 Skeleton의 AC 기여 Task 집합을 보존하며, `validation_ids`의 소유 Task를 제한하지 않는다. **AC가 각 Task에 명시한 검사 절차**가 있으면 해당 AC의 `validation_ids`에 적용 대상 모든 Task의 자체 필수 검사 ID를 연결한다. 전역 constraint가 요구한 검사는 해당 Task에 존재해야 하지만 그 전역 의무만으로 특정 AC 연결을 만들지 않는다. 검사 소유 Task가 AC 기여 목록에 없어도 명시 AC 연결은 필요하며, 연결을 추가하기 위해 기여 집합이나 Task 의미를 바꾸지 않는다. 특정 Task에만 적용되는 요구의 범위도 유지한다. Reviewer는 자체 검사 존재와 해당 AC의 검사 ID 연결을 각각 확인하고, 연결 누락은 Goal·Task validation·Goal coverage를 직접 근거로 제출한다.

v2 Reviewer는 최종 제출 전에 양의 AC scope 선택을 소유 validation ID로 전개해 현재 `goal_coverage.validation_ids`와 대조한다. 필수로 판단한 관계가 실제 coverage에 없으면 해당 AC×validation target의 `missing_validation_link` finding을 제출한다. 다른 scope의 검사 능력 finding은 이 연결 누락 finding을 대신하지 않는다. 이미 연결된 관계나 필수로 판단하지 않은 관계에 연결 누락 finding을 붙이지 않으며, adapter는 누락 finding을 자동 생성하지 않고 제출의 일관성을 검증한다.

Skeleton·`PlanExpansionDraft`의 `task_refs`는 Compiler가 `Task.task_id`에 대응시켜 권위 `PlanContractDefinition.goal_coverage.task_ids`로 변환한다. 컴파일된 Plan을 받는 Reviewer는 `task_ids`를 실제 Task ID에 대조하고 대응되는 `task_ref` 집합으로 기여 관계를 확인한다. `task_ids`를 draft의 `task_refs`로 바꾸도록 요구하지 않는다. Reviewer finding의 `affected_task_refs`는 별도 입력 계약에 따라 `Task.task_ref`를 사용한다.

#### `plan-inspection-v1` 동결 형식

상세화·검토 provider 응답은 기존 Plan 작성 draft 또는 review와 검사 근거 대조표를 감싼 strict envelope다. strict 변환은 최초 schema의 `properties` 선언 순서를 전체 `required` 배열에 보존하고, canonical JSON 저장이 object key를 정렬한 뒤에도 그 배열을 사용해 같은 property·required 순서와 digest를 재구성한다. 대조표는 `citations → validation_rows → validation_scope_rows → ac_validation_rows` 순서로 원문·수단·부분 scope 근거를 먼저 작성한다. AC 행도 대상 ID와 `basis_refs`·`scope_ids`를 `ac_link_required` bool보다 앞에 둔다. 이후 모든 AC×모든 Task·integration validation과 모든 전역 constraint×Task를 중복 없이 제출한다. 원문 인용은 source ref·JSON pointer·짧은 연속 인용으로 한 번 등록하고 각 행에서 참조한다. AC×validation 행은 해당 검사가 AC 일부를 직접 검증해 연결이 필수인지 `ac_link_required` bool로 표현하며 false는 선택적 연결을 금지하지 않는다. 실제 연결 존재는 evaluator가 Plan의 `goal_coverage`에서 계산한다. 전역 Task 책임은 `constraint_task_rows`, 검사 주장·수단·phase·별도 실제 검사 책임은 `validation_rows`에서 판정한다. 필수 연결이 없다고 제출한 행이나 확인한 범위 모순은 각각 실제 finding과 직접 evidence에 결속한다. schema 출력 순서는 의미 정답이나 실제 모델 성공을 보장하지 않는다.

각 `validation_scope_row`는 같은 validation·같은 phase의 mechanism 하나를 선택할 수 있어야 하며 자신의 `claim_ref`와 그 mechanism의 전체 `basis_refs`를 포함한다. 여러 mechanism의 합집합을 요구하거나 일부 공통 근거만으로 결속을 인정하지 않는다. 모든 `ac_validation_row`는 true/false와 무관하게 해당 validation의 mechanism 및 모든 scope에서 실제 범위 판단에 사용한 project citation을 포함한다. true 행은 선택한 supported scope 각각의 `claim_ref`·전체 `basis_refs`도 포함하고 false 행은 `scope_ids=[]`를 유지한다. 참조는 목록 안에서 중복 없이 반복 사용한다. 이 포함관계는 추적 결속이며 다른 부분의 검사 능력이나 supported·contradicted·unresolved 판정을 해당 scope에 부여하지 않는다. 부분 scope의 의미 판단과 AC 연결 판단을 독립적으로 유지한다. sibling 비전염 규칙은 Goal에 명시된 task/goal 독립 검사 의무를 없애지 않는다.

각 non-supported scope의 finding_codes에 있는 각 code에 대해 자신의 claim_ref를 포함한 scope.basis_refs 전체가 같은 finding_code의 link.basis_refs에 포함되어야 한다. 같은 code의 복수 scope는 전체 근거를 중복 없이 합치고, 한 scope의 복수 code는 각 link가 전체 근거를 포함한다. 전체 validation 문장·동일 source/selector의 다른 citation ID·다른 finding의 인용으로 필요한 citation ID를 대체하지 않는다. 무관한 finding이나 supported sibling의 근거는 강제하지 않는다. supported의 finding_codes는 빈 배열이며 contradicted는 validation_scope, unresolved는 insufficient_evidence로 연결한다. 제출 직전 scope→link→finding evidence→catalog 순서로 대조한다. adapter는 기존 부분집합 검사와 행 순서·첫 실패 중단을 유지하고, scope→link 누락은 `대조표 검사 scope finding의 직접 근거 누락` 뒤에 finding_code·validation_id·scope_id·phase의 repr·claim_ref와 정렬된 required_citation_ids·actual_citation_ids·missing_citation_ids로 진단한다. 전체 오류 수집·자동 참조 보정은 하지 않는다.

adapter는 행 집합의 완전성·중복·ID·selector·인용 일치와 제출물 내부 일관성만 검증한다. 참조 결속 실패는 validation·scope·AC 식별자와 실제 누락 ref를 결정론적으로 보고하며 같은 phase의 여러 mechanism은 후보별 누락으로 구분한다. 파일 인용은 Project Map의 정확한 entry 경로와 content digest를 검증하고 원본 Project Map evidence에 대응시킨다. 관계나 도구 능력의 의미 정답을 코드로 추정하거나 참조·인용·boolean·coverage를 자동 보정하지 않는다. 상세화는 결함을 보정한 완성 draft를 제출하므로 남은 결함을 선언한 대조표와 성공 draft를 함께 통과시키지 않는다. 구조적으로 일관된 잘못된 의미 판단은 별도 고정 의미 평가에서 검출한다. 대조표를 GoalContractRevision·PlanContractRevision·ReviewerSubmission 또는 DB schema에 추가하지 않으며 Core의 판정·Skeleton 기여 집합·독립 Goal Test·ready-time 명령 경계는 유지한다. 평가 digest는 실제 adapter의 strict 출력 schema와 공유 지침을 함께 결속한다.

Reviewer의 각 `InspectionFindingLink.basis_refs`는 citation ID를 참조한다. 제출 직전에 각 ID의 `source_ref`를 원본 evidence ref로 환산한다. `source:goal`과 `artifact:plan_contract`는 그대로 사용하고, 검증된 `project:<entry_id>`는 `source:project_map`으로 환산한다. **해당 link의 환산 집합 ⊆ 같은 finding_code의 finding.evidence_refs ⊆ 실제 evidence_catalog key 집합**을 유지한다. Goal ref는 그 link가 실제 Goal을 인용할 때 필요하며, 대조표 전체 또는 다른 link의 모든 citation·Goal ref를 각 finding에 강제하지 않는다. 유효한 추가 catalog ref는 허용한다. citation ID·`project:<entry_id>`·`source:plan`은 이 Reviewer request의 직접 evidence ref가 아니다. Field 설명과 공유 지침은 이 조건부 경계를 안내하며, adapter는 기존 부분집합 비교를 완화하지 않는다. 실패 시 `대조표 finding evidence ref 불일치` 접두사와 finding_code, 정렬된 required/actual/missing evidence refs 및 누락 ref에 대응하는 정렬된 citation IDs를 보고한다. 진단을 위해 evidence를 추가하거나 인용을 삭제하거나 finding을 생성하거나 의미 판정·silent fallback으로 제출물을 보정하지 않는다.

등록 참고자료와 지침 entry의 본문은 공통 입력 projection에서 실제 bytes의 digest를 확인한 뒤 정식 `source_ref=project:<entry_id>`, `selector=/content`, `content_digest`와 함께 제공한다. 등록 자료의 검사 범위 인용은 이 정식 주소를 사용한다. mechanism이 실제 범위 판단에 사용한 project citation 집합은 같은 validation의 모든 AC 관계 행에도 그대로 재사용하고, 제출자가 최종 응답 전에 이 포함 관계를 확인한다. Goal trace 배열의 복제 본문은 파일 주소의 대체물이 아니며 잘못된 주소에 대한 자동 교정·사후 alias는 금지한다. 인용 수용 시에도 원본 파일의 digest와 선택 문자열을 다시 확인한다. 다른 Project Map 파일은 정확한 경로를 통해 읽고 같은 인용 검사를 적용한다. adapter는 이 내부 일관성만 검사하며 관계 의미·인용·coverage를 자동 생성하거나 보정하지 않는다.

`ac_link_required=false`인 행에 이미 연결된 ID가 있다는 사실만으로 결함을 만들지 않는다. 모든 constraint 행은 비적용일 때도 해당 원문을 인용한다. 전역 검사 의무의 일부만 빠지면 존재하는 validation ID와 `missing_task_validation` finding을 함께 제출한다. `ac_link_required=true`의 ID 연결만 빠진 경우는 `missing_validation_link`, 검사 주장과 수단의 직접 모순은 `validation_scope`/`contradicted`, 근거 부족은 `insufficient_evidence`/`unresolved`로 분리한다. Plan의 file·diff 등 단순 evidence 언급을 다른 입력의 명시적 제외로 간주하지 않는다.

앞의 반복 `basis_refs`·`finding_codes`·`finding_links` 계약은 `plan-inspection-v1`로 동결한다. 기존 raw 응답·strict schema·validator·evaluator·checkpoint와 판정은 수정하거나 v2 결과로 덮어쓰지 않는다. provider 형식은 요청 전에 명시적으로 선택하고, 선택한 version·지침·역할별 strict schema·실제 request와 receipt를 digest로 결속한다. 실행 중 다른 version으로 자동 fallback하거나 과거 version의 기대값·checkpoint를 새 version의 합격 근거로 사용하지 않는다.

#### `plan-inspection-v2` 의미 제출과 기계 전개

v2의 공통 의미는 `validation_obligations.py`에 둔다. AC 연결은 이미 계획된 모든 검사에 대한 기여 관계이며 최소 충족 검사 집합의 선택이 아니다. 각 AC×validation을 독립적으로 대조해 명시 절차·도구·phase·범위를 실제 수행하는 supported scope를 모두 선택한다. 동일 절차를 여러 ID가 수행하면 각 관계를 보존하고, 등록 도구의 내부 호출·재실행도 실제 본문에 따라 포함한다. 같은 validation의 contradicted·unresolved 부분은 별도 finding의 근거이며 다른 supported 부분의 관계를 제거하지 않는다. AC가 제한하지 않은 phase·범위를 임의 한정하지 않고, 관련 결과·evidence나 전역 의무만으로 새 절차 요구를 추정하지 않는다. 역할 지침과 출력 필드 설명은 이 같은 경계를 사용하며, 원시 선택의 실제 의미 정확성은 별도 평가로 확인한다.

`plan-inspection-v2`에서 adapter는 결속된 입력 원문으로 immutable citation catalog를 먼저 만들고 모델은 실제 판단에 사용한 ID만 선택한다. 모델은 validation의 tool·phase·직접 근거와 실제 절차·상태만 나타내는 최소 scope, 모든 constraint×Task의 적용 여부와 실제 validation ID, Reviewer finding의 종류·복구 가능성·주 target ID를 직접 제출한다. AC 연결은 scope 판정과 분리한다. 모델은 `ac_scope_requirements`에 모든 AC를 정확히 한 행씩 쓰고 criterion과 `statement_scope_ids`·`validation_intent_scope_ids`를 제출한다. 각 원문 필드가 명시한 실제 절차의 supported scope를 해당 목록에서 선택하며, 그 필드에 절차 요구가 없으면 빈 목록을 쓴다. 두 필드는 전체 AC 문맥으로 해석하되 한 필드의 단계·독립성 설명이 다른 필드의 명시적 절차를 면제하지 않는다. 두 원문이 같은 절차를 요구하면 같은 scope를 양쪽에 선택할 수 있다. 합집합·validation ID·전체 AC×validation 행렬은 다시 작성하지 않는다. 동일 절차를 Task와 Goal validation이 각각 실행하면 각 validation 소유 scope를 선택한다. AC가 특정 Task 또는 Goal validation 단계에 검사 책임을 열거하면 그 단계에서 열거된 책임을 실제 수행하는 scope를 선택하되, 단계의 경계·순서만 나타내는 문구나 같은 Task·phase·evidence·결과 주제를 묶음 의무로 확대하지 않는다. 고정 복합 target인 AC×validation과 constraint×Task는 adapter가 content hash `inspection_target_catalog` ID로 제공한다. Reviewer는 결함 종류에 맞는 복합 catalog ID, 자신이 만든 scope ID, validation ID 또는 citation ID를 `primary_target_ids`에서 선택하고, 추가 직접 citation과 소유 관계로 계산할 수 없는 영향 Task만 별도 목록에 둔다. 다섯 표준 defect kind 밖의 직접 결함은 `other`와 직접 gate·severity, citation ID로 보존한다. 이 선택은 의미 판단이며 adapter가 생성·삭제·교정하지 않는다. Expander는 같은 직접 검사 구조를 사용하되 finding 없이 일관된 완성 Plan만 제출한다. Reviewer의 finding/rating 상호배타성과 Core의 admission·score·상태 판정 권위는 그대로 유지한다.

v2 adapter는 Goal의 사용자 요청·outcome·AC·constraint·preference·assumption·effect와 Skeleton/Plan의 목적·입출력·완료·검사·효과 문장, 등록 자료 본문만 semantic citation catalog로 투영한다. Goal source trace와 State·ProjectMap의 ID·digest·path 장부는 직접 의미 근거 후보에서 제외하며, 그 revision·digest·root·freshness·요청 binding은 기존 Core·preflight가 역할 호출 전에 결정적으로 검사한다. 역할에는 선택된 provider version의 규칙만 넣고 v2에는 v1의 반복 장부 작성 지침을 제공하지 않는다. citation catalog는 source·selector·연속 quote의 content hash ID로 만들고, 복합 target catalog는 kind와 두 원본 ref의 content hash ID로 만든다. 호출 뒤 같은 입력에서 두 catalog를 다시 계산해 변조·누락을 차단한다. 등록 파일 본문은 요청에 노출한 instruction·reference entry만 포함한다. validation statement와 Goal AC·constraint의 고정 claim ref는 원본 ID·selector join으로 붙인다. 각 AC의 두 원문 선택 목록을 순서를 보존하는 합집합으로 만들고 scope 소유 validation과 고정 AC×validation 조합에 join하여 모든 조합의 `ac_link_required`와 `scope_ids`를 결정적으로 확장하고, validation·scope·AC·constraint·finding의 참조 closure와 coverage membership witness를 계산한다. 생략된 조합은 false와 빈 scope 집합이다. Reviewer가 고른 target ID는 결함 종류에 맞는 내부 typed target으로 해석한다. 검증된 `project:<entry_id>` citation은 Reviewer evidence에서 `source:project_map`으로 환산하고, finding의 `evidence_refs`와 `affected_task_refs`, 다섯 표준 kind의 gate·severity와 모든 finding의 결정적 summary는 해석된 target closure와 동결 taxonomy에서 계산한다. `other`의 직접 gate·severity는 그대로 보존한다. 이 파생은 모델이 선택한 scope claim·status, 양의 AC scope 선택, finding 종류·target ID·직접 evidence를 보정하거나 없는 관계·검사 능력·근거를 새로 만드는 작업이 아니다. 존재하지 않는 ID, 원문 불일치, closure 모순은 제출 실패로 보존한다.

개발 진단의 static 11사례는 완료된 독립 provider 응답의 schema·참조·의미 실패를 사례별로 보존하면서 다음 사례를 계속 관측한다. 명시적 provider 실패 종료나 완료·귀속이 불명확한 외부 효과는 전체 실행을 중단한다. qualification은 기존 첫 실패 중단과 `expansion → 독립 생성 검토 → expanded-review` 경계를 유지한다. v2 승격에는 별도 static 11, qualification 13, 실제 Goal 경로 evidence가 모두 필요하며 그전까지 제품 기본 provider는 v1이다.

제한 실제 진단은 실제 adapter schema·공유 지침·입력 projection·고정한 원문 근거와 기대값을 하나의 새 평가 계약으로 잠근다. 자동 주입되는 전역·프로젝트·workspace 지침은 경로와 본문 digest를 잠그고 실제 thread receipt의 `instructionSources`와 provider turn 전에 대조한다. 과거 입력의 숨은 결함이 확인되면 과거 fixture와 판정을 보존하고 별도 revision의 정상·독립 결함 사례를 만든다. 결정적 구조 회귀의 통과를 실제 모델의 의미 검출 성공으로 대체하지 않는다.

각 파생 AC 관계 closure는 비어 있지 않은 AC statement와 validation_intent, validation 전체 문장, 모델이 양의 연결로 선택한 supported scope의 근거를 연결한다. 명시 도구·phase에 포함된 실제 절차는 문장에 반복하지 않아도 검토하지만, 해당 범위를 수행하지 않는 phase나 불완전 자료에서 능력을 추정하지 않는다. 독립 Goal 검사의 범위를 별도 Task 검사에 옮기지 않고, 전역 책임을 중복 충족하는 여러 검사 scope 각각의 필수성과 그 책임의 출처를 분리한다. 특정 phase 실행을 명시했다고 같은 목적의 모든 별도 검사가 해당 AC의 필수 절차가 되는 것은 아니다. 현재 존재하는 명시 절차의 연결 누락과 실제 검사 책임의 누락도 별도로 판단한다.

기대표는 사례별로 사전 검토한 Goal 전체·Plan 전체(검사 statement·소유 Task/Goal·method·mode·evidence·현재 연결 포함)와 등록 source의 주소·본문·digest에 결속한다. 같은 ID의 검사라도 문장·phase·소유자·method·mode·등록 본문이 바뀌면 기존 표를 적용하지 않는다. 사례별 AC 연결 필수성 전수 평가와 독립 결함의 직접 근거 대조, 평가하지 않는 constraint·수단 의미 범위를 명시하며 필요한 표가 없으면 호출 전에 중단한다. fixture review와 builder는 `integration_validations[].criterion_refs`와 `goal_coverage[].validation_ids`의 integration 연결을 양방향 대조하고 불일치하면 provider 호출 전에 중단한다. 생성 Plan은 사전 기준에 따른 독립 정상성 대조와 전용 필수성 표를 실제 생성 입력 digest에 결속한 뒤에만 Reviewer에게 전달한다. 고정 clean 표를 생성물에 재사용하거나 모델 결과를 본 뒤 기대값·threshold를 바꾸지 않는다. 실제 완료 receipt의 결속 실패도 다음 사례 전에 중단한다.

Core에 전달하는 ReviewerSubmission은 다음으로 제한한다.

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
- 실제 호출 직전에 App Server `model/list` 원본 JSON 전체를 엄격히 검증하고 감사용 inventory 원문·digest와 실행용 v2 operational lock을 각각 결속한다. 이 관측은 요청 조합의 지원 여부를 증명할 뿐 해당 turn의 실제 적용 model/effort를 증명하지 않는다.
- 지원되지 않는 모델이나 effort를 조용히 다른 값으로 바꾸지 않는다.
- 실행과 검사는 별도 역할로 배정하며, 중요한 작업에서는 서로 다른 역할 설정을 우선한다.
- model 변경은 허용 envelope 안에서도 이유가 있는 새 operational binding과 Attempt를 요구한다. Goal·Task 의미가 바뀌면 기존 규칙대로 Goal·Plan revision을 만든다.

### 7.1 Inventory 감사와 operational lock v2

`flowmarshal-model-lock-v2`는 qualification/execution의 운영 계약 format이다. Goal·Plan의 의미 변경이나 Plan 재승인이 아니다. `PlanContractDefinition.model_inventory_digest`는 계획 작성 당시의 historical evidence로 그대로 보존한다. 과거 run·raw·artifact와 v1 lock을 덮어쓰거나 새 의미로 계산하지 않는다. v1 evaluation contract·checkpoint·진단 preflight에는 명시적 버전 거부를 적용하고 새 run에서만 v2를 만든다.

전체 `model/list` JSON은 SDK의 typed coercion 전에 검사한다. 수신한 모든 행은 hidden 여부와 무관하게 검사하며, duplicate model/effort, 빈 목록, 빈/null/공백 포함·잘못된 model ID와 effort를 제거·정규화·생략하지 않는다. 불완전한 pagination도 거부한다. 전체 원문·모델 순서·effort 순서와 전체 digest는 audit observation에 보존한다.

실행 잠금에는 다음만 포함한다.

- 역할별 선택 model/effort와 현재 지원 여부
- 명시적으로 허용된 fallback model/effort의 순서와 각 조합의 현재 지원 여부
- 실행 중인 Codex executable digest
- 해당 경로가 사용하는 runtime capability의 이름과 protocol 계약

역할과 capability key는 정렬하지만 fallback 순서는 유지한다. 무관한 모델 추가·삭제, inventory/model/effort 순서, 사용하지 않는 effort와 capability 변화는 전체 감사 digest가 달라도 같은 실행 projection이다. 선택 model/effort가 사라지면 차단한다. fallback 추가·삭제·effort·순서·가용성 또는 필요한 runtime capability·executable이 달라져도 기존 lock은 사용할 수 없다. 초기 binding에서 사용할 수 없는 fallback은 `supported=false`로 기록할 수 있으며, 이후 가용성 변화도 새 binding을 요구한다. resolver는 preferred가 없을 때 허용 fallback을 자동 선택하지 않는다.

runtime capability는 adapter가 사용하는 `thread/start`, `turn/start`, `thread/read`, 필요한 `thread/resume`·구조화 출력·실제 local 권한 계약이다. 이 값은 전체 서버 기능 목록에 대한 추정이 아니며 executable identity와 실제 정책 검증에 결속된 adapter 계약이다. Worker와 구조화 역할은 각각 사용하는 capability만 잠근다.

제한 diagnostics의 역할 설정 후보는 `prepare --role-config <절대 경로>`로만 주입하며 생략 시 기존 기본 설정을 사용한다. 역할 ID 집합·typed schema와 중복 JSON key를 먼저 검사하고, 입력 경로·선택 이유·원문 bytes digest·canonical JSON digest·typed configuration digest를 원문 snapshot과 함께 preflight 및 planning binding에 기록한다. canonical 표현이 같아도 bytes가 바뀌거나 원본·복사본·요청·v2 lock이 다르면 새 실행으로 자동 대체하지 않고 차단한다. 요청에는 해당 역할의 선택과 순서 있는 fallback을 그대로 결속한다. 이 제한 진단은 선택과 fallback 모두 fresh inventory에서 지원되는 조합만 수용하는 더 좁은 진입 조건을 사용하며, 일반 v2의 `supported=false` 표현 능력은 바꾸지 않는다. 후보 설정은 제품 기본 역할과 Goal·Plan·검사 의미를 변경하지 않는다.

`OperationalBinding`은 전체 inventory와 그 digest, v2 projection과 그 digest를 함께 보존하고 역산 검증한다. 역할 요청은 준비 당시 binding을 보유하고, receipt는 호출 직전 실제 observation을 별도로 기록한다. 두 전체 digest가 달라도 projection이 같으면 실행하며, 요청·관측·receipt를 서로 다른 digest로 위조한 경우에는 거부한다. Task intent에는 provider inventory observation과 `client_requested` model/effort를 구분해 기록한다. requested/observed model·effort, provider inventory digest, adapter capability digest와 provenance는 계산 property에만 두지 않고 receipt·원장·read model의 직렬화 필드로 보존한다. provider가 turn별 model/effort를 `model_observation_source=provider_raw_response`로 식별된 원문 응답에 명시적으로 echo한 경우에만 해당 값을 `provider_observed` 실제 적용값으로 추가한다. 요청 echo·표식 없는 payload·부분 관측은 승격하지 않는다. echo가 없으면 observed 값은 null이고 요청값을 `actual_model`·`actual_effort`로 바꾸지 않는다. materialize, 역할 호출, dispatch, 내부·공개 resume, 독립 Goal Test가 같은 검증기를 사용한다. 선택을 바꿔 실패를 감추거나 preflight 실패 후 schema recovery로 재호출하지 않는다.

원장과 receipt의 provenance는 `provider_observed`, `client_requested`, `local_derived`, `model_reported`를 구분한다. thread/turn·provider status·반환 usage는 provider 원문, 요청 model/effort·GoalAuthorization은 client 요청, latency·합계·Core 판정은 로컬 파생, 역할의 finding·rating·error 주장은 모델 제출이다. 모델 제출값을 provider 확인·사용자 승인·Core 판정으로 승격하지 않는다.

제한 진단의 새 prepare는 과거 입력·모델 설정·executable 기준을 provenance로 읽고 새로운 v2 preflight를 만든다. 과거 전체 inventory digest와의 정확한 일치를 실행 조건으로 사용하지 않는다. 평가의 검사·threshold·oracle·taxonomy, 역할 모델 설정과 fallback 정책은 이 revision으로 보정하지 않는다. 결정적 구현 검증과 실제 역할 qualification은 계속 별개의 Gate다.

현재 transport는 `CodexRuntimePort` 뒤에 둔다. 새 엔진은 기존 `flowmarshal.core`나 `flowmarshal.planning` 도메인을 import하지 않는다.

로컬 Codex task와 역할 thread는 실제 유효 정책 `:danger-full-access`, `approval_policy=never`에서만 시작한다. 첫 파일 조회나 명령 실행 전에 실제 config와 permission profile을 검증하며, 다르면 권한 상승을 요청하지 않고 `PERMISSION_POLICY_MISMATCH`로 종료한다.

전체 권한은 Planner나 Worker가 Core 상태를 변경할 수 있다는 뜻이 아니다. 권위 경계는 축소 OS sandbox가 아니라 typed 입력·출력, Core capability 미제공, digest와 receipt 검증으로 유지한다.

### 7.2 Claude Code runtime provider

runtime provider는 기본 Codex App Server와 명시적 `--provider claude`로 고르는 Claude Code CLI 두 가지다. Claude adapter도 `CodexRuntimePort` 계약(thread 생성·turn 시작·관측·저장 관측·resume·interrupt)을 구현하며, 한 thread는 `claude -p` stream-json 프로세스 하나를 유지한다. provider 사이 자동 fallback은 없고, Codex 경로의 동작과 run metadata digest는 바뀌지 않는다. Claude run metadata에는 provider 이름과 카탈로그 원문 digest를 더한다. 이 선택은 plan-inspection provider version(v1/v2)과 별개다.

Claude Code에는 `model/list`가 없다. inventory는 호출자가 주입한 `flowmarshal-claude-model-catalog-v1` 카탈로그를 `configured_catalog` provenance로 투영한다. 카탈로그는 provider 관측이 아니며 조합의 실제 지원을 증명하지 않는다. 7.1의 원문 검사 원칙을 그대로 적용해 typed 변환 전에 duplicate JSON key·model·effort, 빈/null 값과 선언 밖 field를 거부하고 원문 순서와 bytes digest(`catalogSourceSha256`)를 보존한다. v2 실행 잠금의 executable digest는 실제 `claude` 실행 파일의 digest이고, runtime capability 계약에는 `claude --version`으로 읽은 CLI 버전을 넣는다. 따라서 CLI 버전이나 실행 파일이 바뀌면 새 binding이 필요하다. adapter는 프로세스를 띄우기 직전 실행 파일 digest를 다시 계산하고(`CLAUDE_EXECUTABLE_CHANGED`), 매 turn `system/init`의 `claude_code_version`을 잠금 값과 대조한다(`CLAUDE_CLI_VERSION_MISMATCH`). `cmd.exe`를 거치는 `.cmd`·`.bat`·`.ps1` shim은 명령줄 한도와 인자 재해석 때문에 거부한다.

매 turn `system/init`의 session·cwd·model과 `permissionMode=bypassPermissions`를 관측해 결속하고, 다르면 프로세스를 종료하고 turn을 거부한다. receipt의 `:danger-full-access`/`never`는 이 관측에서 로컬로 도출한 대응값이므로 `permission_profile_provenance`·`approval_policy_provenance=local_derived`로, `provider_permission_mode`는 `provider_observed`로 기록한다. `--permission-prompts none`에서는 승인이 필요한 도구가 조용히 거부되고 result는 `success`로 끝난다. 그래서 `result.permission_denials`가 비어 있지 않으면 turn을 `failed`(`CLAUDE_PERMISSION_DENIED`)로 둔다. 저장 기록에는 구조화 거부 필드가 없어 CLI 거부 문구의 접두어로만 판정할 수 있으므로, live result의 `permission_denials`를 우선 근거로 쓴다.

turn ID는 provider가 되돌려준 user 메시지 uuid(`--replay-user-messages`)다. 2.1.277 실측에서 같은 프로세스의 모든 turn(첫 turn 포함)은 `system/init` 다음에 replay user를 냈고, 저장 기록의 user uuid와 같았다. replay가 없으면 client uuid 등으로 대체하지 않고 `CLAUDE_TURN_START_UNCONFIRMED`로 멈춘다.

provider는 effort를 echo하지 않으므로 observed model/effort는 null이고 응답 모델은 진단 필드 `provider_reported_models`로만 둔다. usage는 `result.usage`의 제공 구성요소만 투영하고 total을 만들지 않는다. 실측에서 성공 result의 `usage`는 turn마다 따로 나오고 `iterations`에 API 호출이 기록됐지만, interrupt로 끝난 result는 모든 값이 0이고 `iterations`가 비어 있었다. 따라서 성공 result에 `iterations`가 있을 때만 `usage_scope=turn`으로 기록하고 나머지는 usage를 null, scope를 `unavailable`로 두며 판단 근거를 `usage_scope_basis`에 남긴다. `modelUsage`는 session 누적이므로 `provider_model_usage_scope=session_cumulative` 원문으로만 보존한다.

자식 세션은 `--safe-mode`, 빈 `--setting-sources`, `--strict-mcp-config`로 실행한다. `--safe-mode`는 CLAUDE.md·skills·plugins·hooks·MCP 등 사용자 설정을 끄며, 실측에서 프로젝트 CLAUDE.md와 auto-memory가 모두 상속되지 않았다. `--safe-mode` 없이 빈 `--setting-sources`만 쓰면 auto-memory가 상속됐다. 필요한 프로젝트 `AGENTS.md`는 5.3 Context Pack의 필수 정책과 역할 지침으로 Engine이 직접 공급한다. 관리자 정책(policy) 설정은 `--safe-mode`에서도 적용되므로 known limitation으로 남긴다.

release freeze는 `config/claude-model-catalog.json`과 `config/qualification-roles.claude.json`의 bytes digest를 다른 qualification 설정과 함께 결속한다. 이 결속 추가만으로 기존 freeze를 다시 만들지 않는다.

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

provider turn의 terminal은 외부 효과의 terminal이 아니다. 외부 `EffectContract`는 provider/system, target·account, operation, scope, idempotency key와 checkpoint policy를 가진 typed identity로 만들고 GoalAuthorization·runtime intent·adapter receipt·대상 재관측에 같은 identity를 결속한다. 1.0 Plan은 내부 파일·명령 효과와 외부 효과를 같은 Task에 섞지 않고 별도 Task와 dependency로 분리한다. 현재 1.0 외부 효과는 같은 실행 Attempt의 terminal·valid provider call과 thread/turn, 같은 identity의 typed adapter receipt, identity와 provider가 일치하는 대상 재관측을 모두 갖춰야 완료된다. `(provider, system, provider_operation_id)`는 프로젝트와 무관하게 원장 전체에서 한 Attempt에만 결속하며, 같은 Attempt의 완전히 같은 receipt 재기록만 멱등 허용한다. turn 상태는 `provider_observed`로 보존하고, 파일 효과는 diff/hash로 별도 확인한다. 직접 확인이 하나라도 없으면 효과를 `external_unknown`으로 유지하며 provider terminal이나 모델의 완료 선언에서 효과 완료 상태를 합성하지 않는다.

Task·Goal validation 계약과 Execution Spec의 `required_evidence_kinds`는 `EvidenceKind`의 실제 지원 집합으로 제한한다. 같은 집합을 provider JSON Schema에 공개하고 Core의 입력 검증에도 적용한다. 구체적인 검사 목적은 statement에 기술하며 새로운 evidence 종류를 임의로 만들어 실행 준비 시점까지 넘기지 않는다. 기존 유효 문자열의 canonical 표현은 유지한다.

필수 외부 사실(계약 문서·계정·삭제 selector)과 계획이 제안할 설계 선택(대안·새 산출물 배치·검증 명령)을 구분한다. 전자는 근거가 없으면 질문·차단하고, 후자는 사용자 Goal과 관찰된 프로젝트의 범위에서 정한다. materialization에서 확정할 운영 상세의 미확정만으로 Goal을 차단하지 않는다.

`IntegrationValidationContract.evidence_mode`의 기본값은 `independent`다. semantic validation은 `model_review` evidence를 필수로 요구하며 terminal PASS/FAIL은 제출된 evidence 종류와 무관하게 독립성 검사를 실행한다. Task 완료 후 Core가 최신 Plan·State·Project Map에 독립 Goal Test의 운영 상세 binding을 만들고 실제 명령 또는 Worker와 다른 Attempt·RuntimeJob·thread/turn을 가진 Validator의 새 원자료 관측을 기록한다. Validator는 자체 terminal provider receipt를 가져야 하며 `SemanticValidationObservation`과 `ValidationResult`의 PASS/FAIL, validation/task ID와 content digest를 정확히 일치시켜야 한다. 사용한 model-review evidence는 재사용하거나 재결속하지 않는다. Task evidence를 다시 합산하는 검사는 Plan에 `task_aggregate`가 명시된 경우에만 수행한다. 운영 상세가 같은 의미를 유지하는 한 Plan을 다시 승인하지 않지만 binding 이후 입력 변경은 `STALE_EXECUTION_INPUT`으로 차단한다.

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
→ 실제 효과 직전 freshness·target/context/prompt/model/policy 재검사
→ thread start/resume와 turn start
→ receipt·binding
→ 결과 관측
→ Task validation
→ State 재관측
→ Goal Test
→ Continue | Task Repair | ExecutionSpec Revision | Subgraph Replan | Goal Revision
```

provider가 구조화해 반환한 error code, 로컬 Engine이 직접 관측해 만든 code와 직접 evidence를 먼저 대조한다. 모델 응답에 적힌 code·원인·효과 상태는 `model_reported` 진단 가설이며 provider/local code나 자동 복구 근거로 승격하지 않는다. 의미가 불명확하면 진단 모델을 쓰되 근거가 부족하면 unclassified를 유지한다. failed terminal을 모두 implementation으로 분류하지 않는다. 기본 분류는 다음과 같다.

| 분류 | 기본 처리 |
|---|---|
| `implementation` | 같은 Task 의미 안에서 Task repair |
| `context` | Execution Spec과 Context Pack revision |
| `task_contract` | 영향을 받은 subgraph 재계획 |
| `dependency` | dependency subgraph 재계획 |
| `environment` | 환경을 복구한 뒤 동일 계약 재개 |
| `requirement_change` | 새 Goal revision |
| `external_unknown` | 기존 intent·binding·receipt 우선 대조 |
| `unclassified` | 근거 보강 전 분류·재시도 확정 금지 |

동일 실패 재계획은 최대 2회, Goal 전체 재계획은 최대 5회다. 횟수는 원장에서 계산하며 호출자가 제공한 값을 신뢰하지 않는다. 첫 재계획 이후에는 새 evidence 없는 반복을 차단한다.

1.0의 자동 복구 필수 범위는 허용된 로컬 ContextRequest 해소, `external_unknown`의 observe-first 처리와 직접 evidence에 결속한 실제 Task repair 또는 subgraph replan 한 경로다. 나머지 분류는 typed vocabulary와 명시적 정지·Execution Spec/Plan/Goal revision routing을 제공하되 범용 자율 복구기를 필수 범위로 확대하지 않는다. 최소 경로의 성공만으로 모든 분류의 자동 복구를 지원한다고 주장하지 않는다.

PC 종료, thread 생성 결과 불명, turn 중단 뒤에는 새 task를 추측 생성하지 않는다. unreceipted intent를 `external_unknown`으로 표시하고 기존 provider operation·thread binding을 먼저 관측한다. 마지막 validated checkpoint에서만 재개한다.

실행 직전 검증은 reserve 이후 입력 변화도 잡아야 한다. 생성 응답 유실·abrupt process death·timeout을 각각 검증한다. lease 만료·collector 종료·interrupt ACK를 terminal로 보지 않는다. 기존 binding을 먼저 관측하고 필요할 때만 새 turn을 만든다. 부분 쓰기 후 허용 resume와 immutable 입력 변경을 구분한다.

## 10. 개발·공개 인터페이스

EngineApplication의 prepare/authorize/run_once/observe/pause/cancel/status/final-report 경계 연결은 **planned**다. run_once는 RuntimeJob 예약/시작 또는 관측 소비 후 신속히 반환한다. 활성화 후 준비·worker·semantic validation·Goal Test·recovery/replanning 역할 모두 job/checkpoint에 포함한다. supervisor는 활성 job 동안만 연결·stream·receipt·terminal·usage·절대 deadline을 관리하고 Core만 완료를 판정한다. 승인 전 대화형 준비는 동기 Coordinator/RoleRunner를 유지할 수 있으며 전역 daemon은 필수가 아니다.

아래는 schema 3 개발 CLI의 기존 명령 표면이다. 새 응용 명령이 모두 구현됐다는 증거가 아니다. 1.0 전에는 `flowmarshal-engine` CLI를 사용한다.

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

최종 1.0 package name·version·entrypoint·사용자 설정 표면은 release freeze 전에 확정한다. cutover Gate 뒤에는 이미 검증된 동일 wheel을 로컬 활성화하며 CLI·package 이름이나 metadata를 다시 바꾸지 않는다. 기존 prototype CLI는 감사 재현 도구로 남기고 새 제품 엔진에서 import하지 않는다.

## 11. Qualification과 cutover Gate

현재 필수 조건은 [1.0 승인 계약 V01·V02](redesign-1.0-contract.md)의 결정적·영향 회귀, 실제 역할48·Planning18, 실제 요청부터 한 번의 승인·실행·독립 검사·최종 결과까지의 E2E, 깨끗한 non-editable 설치와 독립 최종 감사다. 책임별 실제 provider·synthetic stub·fault injection·과거 evidence를 구분하며 고정 case 수만으로 완료를 주장하지 않는다. 상세 임계값과 E2E 책임을 빠짐없이 충족해야 한다. 모두 **planned / 미실행**이며 문서 변경은 PASS가 아니다.

각 cell은 source/fixture/prompt/schema/lock/evaluator digest와 seed, 실제 evidence artifact의 허용 root·SHA-256·cell/계약 ID에 결속한다. harness가 결과를 만들 때와 최종 scope verifier가 집계할 때 파일 존재·경로 confinement·digest·cell/fixture/seed/freeze 결속을 각각 확인한다. release project E2E는 절대 경로 candidate wheel과 그 SHA-256·배포판 이름/버전·non-editable 설치·import 경로·wheel 내부 package bytes를 같은 실행 계약에 결속한다. source 기반 진단과 경로 문자열만 있는 결과를 release PASS로 만들지 않는다. 이번 공통 계약 변경의 최초 실제 역할·Planning qualification은 새로 실행하고, 과거 51개/1,056개 검사 결과를 새 실행으로 세지 않는다. 이후 재사용은 보수적 영향 매트릭스로 유효성을 확인한다. 판정 후 oracle·threshold를 낮추지 않는다.

구현 중에는 결정적/fake canary로 계약 우회를 조기에 차단한다. 최종 package identity를 포함한 candidate wheel과 release input을 freeze한 뒤 `요청 → Goal 정규화·독립 review → Plan 선택 → 승인 → Task 실행 → 독립 Validator → Verdict` 수직 live canary와 대표 effect-unknown fault를 먼저 실행한다. canary가 실패하면 복구·ProjectMap·평가 harness 범위를 더 넓히지 않고 실패한 계약을 먼저 줄이거나 고친다. 본 campaign과 같은 동결 입력을 사용한 통과 cell은 역할 48회·Planning 18회에 포함하며 별도 추가 수량으로 만들지 않는다.

Role 48과 Planning 18은 입력을 먼저 freeze하고 서로 독립인 fixture/seed shard를 병렬 실행한 뒤 aggregate에서 누락·중복·digest 불일치를 결정적으로 검사한다. 실제 E2E의 독립 lane도 공유 immutable 입력과 분리 artifact 조건을 만족하면 병렬화할 수 있다. 두 최종 감사는 서로의 중간 결론을 보지 않고 병렬 수행하며, Core가 evidence ref와 finding을 결정적으로 join한다. 필수 finding 충돌·누락을 다수결로 숨기지 않는다.

FM-09의 개발 조율·dispatch·원장 자동화는 제품 runtime 기능이나 1.0 품질 Gate가 아니다. release evidence 수집을 돕는 delivery 도구로 유지하되 그 자체의 확장·완성도와 사용자의 Codex 예약 `ACTIVE`/`PAUSED` 상태를 제품 critical path에 두지 않는다. 제품 scheduler의 `run_once`·supervisor 결속은 동결 wheel과 격리 Engine DB에서 연속·동시·재시작 subprocess tick으로 직접 검증한다. 실제 Codex 예약 연동은 사용자 opt-in 비차단 운영 검사다.

최종 1.0 package name·version·entrypoint·사용자 설정 표면은 release freeze 전에 확정한다. 그 최종 candidate wheel의 깨끗한 non-editable 설치를 한 번 완전 검증하고 후속 E2E는 같은 설치 환경을 사용한다. cutover postverify는 같은 wheel digest·lock·환경 provenance와 최종 identity가 유지됐는지 확인하고 로컬 활성화·설정·smoke만 검증한다. qualification 뒤 metadata를 바꾼 새 wheel에 이전 설치·E2E 근거를 재사용하지 않는다.

### 11.1 v2 채택용 Development-diagnostic 단계 A

기본 provider는 qualification된 v1이다. 아래 static 11/qualification 13은 v2 자체 채택 조건이며 모든 제품 실행의 필수 선행조건이 아니다.

단계 A의 development-diagnostic 실행 모드는 사전에 고정한 서로 독립적인 static 11사례를 관측한다. 모델 호출은 사례당 하나로 하고 전체 최대 11회이며 schema recovery는 0회다. 이는 기존 Reviewer v1의 행 내부 검사와 첫 실패 중단을 바꾸지 않는다. qualification 13의 기존 첫 실패 정책과 `expansion → 독립 생성 검토 → expanded-review` 경계도 유지한다.

정상 완료 사례와, receipt·terminal·lock 귀속이 완료된 model/schema/semantic FAIL만 다음 독립 사례로 진행할 수 있다. 환경, 계약, 입력 stale 또는 외부 효과 불명은 즉시 전체 실행을 중단한다. 관측한 실패는 FAIL로 그대로 보존하고 호출하지 못한 나머지 사례는 NOT_RUN으로 기록한다. 이 흐름은 실패를 재시도하거나 사례 사이에서 의미 판단을 보정하는 경로가 아니다.

공통 preflight는 현재 승인된 checkout의 HEAD, source manifest, clean tracked files, 전용 Python identity와 실제 `flowmarshal` import origin을 결속한다. fixture whitelist package와 relocation proof, 명시한 Codex executable, roles와 instruction의 actual source, model lock도 같은 실행 입력으로 고정한다. origin/main과 다른 checkout의 HEAD는 시작 provenance로만 기록하며 실행 중 비교하지 않는다. 실험용 detached worktree는 과거 diagnostics의 실행 조건이며 최신 Git 작업 위치 승인을 대체하지 않는다. 기존 harness가 이 조건에 고정되어 있으면 계약·도구 정합화와 재검증이 필요하다. 현재 작업 위치와 한시 규칙은 [인계의 실행 대상](pre-1.0-handoff.md)을 따른다.

새 고정 diagnostics는 실제 역할 thread의 `ephemeral=false`를 preflight에 고정하고 thread 생성 intent와 provider receipt를 대조한다. 모델 turn 없는 지침 probe는 기존 ephemeral 방식이고 일반 역할 runner의 기본값도 유지한다. 프로세스 중단 뒤에는 저장된 thread를 `thread/read`로 먼저 관측하며, 저장 설정 자체를 완료·usage 복구·재실행 권한으로 해석하지 않는다. 장시간 진단은 대화의 포그라운드 실행 세션 밖에서 시작하고 launch intent·PID·시작 시각·출력 경로를 별도 운영 기록에 남긴다. 기존 실행의 summary나 미확인 turn을 새 결과로 덮어쓰지 않는다.

단계 A는 payload 의미나 oracle을 수정하지 않고 과거 FAIL을 보정하지 않는다. 11사례가 모두 관측되어도 이는 development-diagnostic 완료일 뿐 기존 qualification 또는 cutover PASS를 의미하지 않는다.

### 11.2 비교 성능 — 비차단 후속

R3.1 대비 token/speed, performance36와 비교 lifecycle 최적화는 별도 비차단 보고로 분리한다. [이전 Release Performance Floor 계약](performance-release-floor-before-redesign-1.0.md)의 36 cell·18 pair·v3/v4·여섯 하한·최적화 지표와 원본 결과는 보존한다. 그 보고서의 cutover 필드나 미관측 값을 현재 제품 릴리스 권위로 사용하지 않는다. 미관측은 null/NOT_OBSERVED이며 0·추정치로 채우지 않는다. 내부 exact Plan digest 결속은 수동 승인 의무와 다르다.

GUI, Localizer/번역 최적화, MCTS/광범위 graph, 동일 프로젝트 병렬, remote/multiOS hardening은 1.0 이후다. 결정적 테스트나 합성 smoke는 실제 qualification을 대신하지 않는다. 현재 필수 기능·안전·역할·Planning·E2E·설치·독립 감사가 통과하기 전에는 package와 CLI를 flowmarshal 1.0으로 승격하지 않는다.

## 12. Legacy 동결과 migration 정책

- R1~R3.1 source와 해당 artifact는 수정·삭제하지 않는다.
- R3.1 campaign을 GO로 만들기 위한 추가 보정이나 재실행은 하지 않는다.
- 완료된 campaign의 실패 사례만 provenance와 함께 새 회귀 fixture로 복사한다.
- Engine schema 4는 별도 새 DB로 만든다 (planned). schema 3/raw receipt/history는 제품 runtime과 분리한 최소 read-only inspector로 원래 상태·receipt·usage/history를 조회한다. 새 Engine 실행·import·상태 전이는 지원하지 않으며 prototype/운영 DB의 자동 제자리 변환, 가짜 Goal/Profile 생성, 옛 token budget 재해석을 금지한다. 조율 메타데이터 migration은 별개다.
- migration 수요가 확인되면 안정화 후 검증된 일회성 import 도구를 별도 계획으로 만든다.
- 역사적 문서의 당시 판정은 감사 기록으로 유지하되 현재 제품 상태의 권위로 사용하지 않는다.

## 13. 실행 admission과 usage 운영 계약 — planned

실행 상태·provider turn·외부 효과·호출 슬롯과 usage 관측을 분리한다. provider terminal과 유효 결과가 확인되면 사용량 누락만으로 후속 실행을 막지 않는다. provider terminal만으로 외부 효과 완료를 만들지 않는다. 필수 evidence·validation·운영 한도는 계속 검사한다. 효과가 불명확한 호출은 기존 intent·thread/turn binding·receipt와 대상을 먼저 관측하고 재실행하지 않는다. 늦은 usage는 원본 receipt를 보존한 회계 관측만 멱등 추가하며 실행 슬롯 환불·재차감·재실행을 유발하지 않는다.

input·cached·output·reasoning·total 중 미제공 token 구성요소는 각각 null/unknown과 이유로 남기고 0·예약량·추정값으로 채우지 않는다. 제공된 구성요소는 다른 항목의 결측과 무관하게 보존한다. 실제 token은 유효한 근거지만 API 가격·계정 사용률 %와 함께 구독 한도 차감량이나 정확한 작업 요금으로 환산하지 않는다. 모든 활성 실행은 immutable `max_provider_calls`와 `absolute_deadline`을 결정적 hard stop으로 결속한다. token stop은 사용자 opt-in일 때만 관측된 token 소계에 적용하는 best-effort 정책이며, usage 결측이 있으면 정확한 잔여량이나 strict cap을 주장하지 않는다. exact usage backfill·실시간 지원·계정 조회 성공을 필수 의존성으로 만들지 않는다. provider/local의 구조화된 rate-limit 오류·reset 관측에 따른 대기는 허용하되 모델의 문자열 주장만으로 대기하지 않는다.

planning 준비 포함 역할14·후보 version5·refinement1, 동일 실패 replan2/Goal5, resume1, schema retry 기본0, 역할 timeout900초/명시 compact reviewer1800초, 관측30초/RPC5초를 보존한다. 실제 적용 범위·기산점·소비 조건은 [D07](redesign-1.0-contract.md)에 따라 구현과 대조하며 새 ID로 한도를 초기화하지 않는다. 역할 timeout·절대 deadline·관측 정책은 요청·receipt·checkpoint에 결속하고 RPC 대기도 관측 창에 포함한다.

Goal 등록 전 호출도 같은 프로젝트·Goal 계보·요청·원본 receipt·thread/turn에 결속한다. 가짜 Goal/Profile을 만들지 않는다. 최종 보고는 Verdict가 참조한 정확한 Goal·Plan revision과 동일 Goal 계보만 집계한다. 확인된 소계·누락/충돌 이유와 불완전 총량 null을 구분하며 과거 token budget·잠정 차감·reserved/usage_unknown/settled의 원래 의미는 read-only reader에서 보존한다.

요청 model/effort는 fresh inventory와 허용 envelope로 지원 여부를 확인하며 미지원 조합을 조용히 fallback하지 않는다. provider의 turn별 echo가 없으면 이를 실제 적용 model/effort라고 부르지 않는다. 명시적인 변경 사유·새 binding·Spec/Attempt를 기록하고 완료 Worker를 Validator 변경으로 재실행하지 않는다. 필요한 목표·정책 확장은 authorization 변경으로 다룬다.

구현 순서는 [로드맵](pre-1.0-roadmap.md), 승인 12항목별 책임은 [연결표 M01](redesign-1.0-contract.md)을 따른다. 이 문서는 FM-02~FM-16의 구현·검사 완료 상태를 갱신하지 않는다.
