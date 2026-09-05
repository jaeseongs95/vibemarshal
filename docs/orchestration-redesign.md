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

Goal이 등록 검사 도구·자료의 특정 phase나 절차 실행을 명시하면 전체 AC·constraint·validation intent와 제공된 관측을 대조하여 참조 대상·범위·검사 의미를 확인한다. 명시 참조에 포함된 검사를 다른 AC에 반복하지 않은 것만으로 누락을 판정하지 않는다. 등록 자료의 존재만으로 계약 채택을 추정하지 않으며, 참조 부재·대상 또는 phase 불일치·필요한 검사 부재·불완전한 본문에서는 상속을 가정하지 않는다. 명시적 제외·충돌은 참조로 덮지 않는다. 정규화는 검사 대상·범위·목적을 식별 가능하게 보존하며 운영 명령은 ready-time 명세에서 확정한다.

Goal 준비 시 대상은 사용자 명시 대상과 프로젝트 관측의 `project_root`로 확인한다. 역할 cwd나 등록 참고자료 저장 위치는 대상 선택 근거가 아니다. 별도 역할 복사본과 관측 root의 경로 차이만으로 target 충돌·stale·selector 부재를 추정하거나 이미 관측된 대상을 새 질문·가정으로 바꾸지 않는다. 실제 사용자 대상 충돌·불명확한 참조·불완전한 관측은 계속 근거에 따라 검토한다.

허용 외부 효과는 외부 시스템·계정·제3자에 대한 효과다. 로컬 파일 변경·검증 명령·함수 반환·응답 보고와 구분한다. 기대 효과에는 실제 발생시킬 효과만 두고, 파일 무변경·외부 효과 없음은 금지 효과나 완료 조건으로 표현한다. 프로젝트 파일·의존성 변경 같은 로컬 변경 금지와 외부 서비스 변경·배포 금지는 별도 항목으로 유지한다. 한 문장으로 묶인 원문도 같은 의미로 분리하며 새 금지·허용 효과나 필연적인 네트워크·외부 계정 변경을 추정하지 않는다. 자연어의 의미 분류는 독립 검토 대상이며 문자열 키워드만으로 권위 판정을 대체하지 않는다.

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

계획·검토 입력은 Goal의 명시 대상과 `ProjectMapRevision.root`를 대조한다. `reference`/`registered_reference` 자료의 저장 위치·부모 디렉터리와 역할 실행 `cwd`는 대상 프로젝트 변경의 근거가 아니다. 경로의 실행명·날짜·버전만으로 다른 workspace를 추정하지 않는다. Goal의 명시 대상 충돌, 후보의 Project Map·State digest binding 불일치와 State freshness 위반은 역할 호출 전에 Core·adapter가 해당 필드와 직접 evidence로 결정적으로 검사한다. 이 검사를 통과한 기계 메타데이터를 Reviewer에게 다시 관계 장부로 쓰게 하지 않으며, 등록 본문·Goal·Plan 사이의 의미 충돌은 별도 semantic 검토에 남긴다.

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

Goal의 AC 또는 전역 constraint가 각 Task 또는 특정 범위 Task의 완료 전에 요구한 검증은 AC 기여 관계와 별개인 Task 자체의 필수 책임이다. 상세화는 Goal의 적용 범위를 각 Task에 대조하고 해당 `Task.validations`에 검사 목적·method·필수 evidence 종류를 보존한다. `detail_requirements`나 Task의 AC 연결에 반복되지 않아도 Goal의 명시적 요구는 유지한다. 후속 검증 Task·`integration_validations`·완료 조건 문장 또는 `independence_required` 모델 배정만으로 이를 대체하지 않는다. Core는 선행 Task 자체의 검증을 통과한 뒤 dependency를 해제하므로 선행 Task의 완료에 필요한 evidence를 후속 Task에 의존하게 만들지 않는다. 명시적으로 요구한 실제 테스트·파일 범위·독립 모델 검토에는 적용 대상 Task의 deterministic command/test·file/diff 및 semantic model_review 검사를 둔다. Goal의 요구 밖 Task에 이 검사 종류를 일괄 강제하지 않는다. Reviewer는 상세 Plan의 실제 누락을 Goal과 해당 Task의 validation 계약으로 검토하며, Skeleton 선택 필드의 반복 부재를 결함으로 승격하지 않는다.

### 6.1 Reviewer와 Core 판정

검사 연결은 먼저 모든 검사 문장의 복합 책임과 등록 수단·phase의 실제 절차를 확인하고, 다음 전역 constraint가 요구한 Task 자체 validation, 다음 Goal의 statement·validation_intent·적용 범위, 마지막 현재 연결과 finding을 양방향 대조한다. AC가 동일 절차의 task/goal phase를 각각 명시하면 명시된 각 phase를 실제 수행하는 validation은 각각 필수 연결이며, 별도 실행은 실행·evidence 분리일 뿐 task phase를 선택 사항으로 만들지 않고 이 규칙을 명시되지 않은 sibling 검사에 전염시키지 않는다. AC가 절차 자체를 직접 요구한 경우와 특정 도구·phase 실행을 요구하여 그 실제 phase가 절차를 포함하는 경우를 구분하며, 같은 목적의 별도 검사나 다른 phase까지 확대하지 않는다. 전역 constraint가 요구한 Task 자체 validation의 존재와 AC가 명시한 절차의 validation ID 연결은 별도 판정이다. ID 이름이나 이미 연결된 대표 검사만으로 AC가 명시한 필수 검사 ID를 생략하지 않으며, 전역 semantic 의무·검사 문장의 연관 표현·evidence 종류나 단순 선후조건만으로 모든 AC에 연결을 강제하지 않는다. 연결만 빠졌다면 실행 누락이나 새 검사 의무로 확대하지 않는다. 상세화와 Reviewer는 같은 기준을 공유하며 Goal·검사 원문·현재 coverage를 직접 근거로 사용한다.

Worker의 작업·응답 제출, 이후 Task 검증과 Core의 완료 판정을 구분한다. Task 완료 조건에 독립 Validator 통과를 요구할 수 있지만, Worker 응답을 입력으로 뒤에 수행하는 Validator의 결과를 같은 Worker가 미리 제출하도록 요구하지 않는다. 검증된 선행 Task 결과의 후속 인용은 허용하며 자연어 시점 충돌을 실제 runtime 교착으로 단정하지 않는다. produces·consumes·preconditions·완료 조건과 validation 입력을 함께 대조하여 Worker 실행 보고와 Validator의 별도 검사 결과를 구분한다.

검증 계약의 `statement`는 Goal이 명시한 검사 대상·종류·실행 목적을 보존한다. `required_evidence_kinds`의 `test`는 evidence 종류이며 특정 검사 절차를 보장하지 않는다. 기존 unittest 실행을 요구했다면 적용 대상 Task의 검사 문장에도 해당 실행·통과 확인을 보존하고 일반 동작 검사로 바꾸지 않는다. 실제 명령은 ready-time Execution Spec에서 확정한다.

상세 Plan의 Task·integration validation이 등록 검사 도구·자료의 phase·mode·절차를 참조하면 상세화와 Reviewer는 등록 경로의 관련 본문과 필요한 구현 분기를 읽어 실제 검사 범위를 대조한다. 같은 도구의 다른 phase가 수행하는 검사를 합쳐 설명하거나 선언·시그니처 검사를 실제 입력·호출 방식 검사로 확대하지 않는다. Goal의 검사 목적과 수단의 실제 능력을 구분하며, 부족한 필수 검사는 별도 실제 검사 책임으로 보존한다. Goal이 Task에 요구하지 않은 검사를 일괄 추가하지 않고 정상 Task 검사와 독립 Goal Test의 범위 차이를 허용한다. 도구·phase 참조로 검사 의미를 식별하는 것은 계획 단계에서 허용하되 argv 등 운영 상세는 ready-time에 확정한다. 이미 명시된 phase와 검사 의미의 충돌은 Plan Contract 결함이며 새 revision으로 수정한다. 자료 부족과 직접 확인된 모순을 구분하고 원래 검사 의무나 합격선을 약화하지 않는다.

별도 검사 책임은 같은 validation ID·문장 안에도 둘 수 있다. 이때 도구 실행에 더해 무엇을 실제 실행하고 어떤 기대 결과와 비교하는지 식별해야 하며 새 ID나 argv를 강제하지 않는다. 도구가 관측하지 않는 목적을 도구 실행의 결과로 덧붙이는 문장은 별도 책임이 아니다. 독립 실행과 범위 충분성은 별개이므로 같은 잘못된 phase를 새로 실행해도 범위 모순은 해소되지 않는다. Reviewer는 전체 검사 문장을 대조해 직접 확인한 모순의 validation ID·수단·주장과 evidence를 finding으로 반환한다. 낮은 rating이나 다른 결함에 의한 후보 차단은 그 모순의 검출을 대신하지 않는다. 최소 finding 원칙은 중복·추측을 배제하는 원칙이며 별도 직접 증거가 있는 독립 결함을 생략하는 근거가 아니다. provider용 Plan 검토 schema의 필드 설명은 이 경계를 안내할 수 있지만 Core의 finding·rating 상호배타성, 판정 권위나 점수 정책을 바꾸지 않는다.

Plan Reviewer에는 모든 Task·integration validation을 원래 순서대로 펼친 비권위 검사 색인과, AC의 statement·validation_intent 및 전역 constraint를 구분한 Goal 원문 색인을 함께 제공할 수 있다. 두 색인은 ID·순서·selector·원문과 현재 AC 연결만 투영하고 필수 연결·phase 능력·runtime evidence 범위의 판정을 추가하지 않는다. `required_evidence_kinds`는 각 validation 계약의 필요 evidence 종류이지 이후 semantic Validator 직접 catalog의 허용 목록이 아니다. 현재 연결과 필요한 연결은 구분하며, 전역 constraint나 단순 선후조건을 근거로 모든 검사 ID를 모든 AC에 연결하지 않는다. finding의 evidence ref는 원본 catalog만 사용하고 색인은 새 권위나 evidence가 아니다. Task·integration 작성 draft의 statement 설명도 수단별 범위를 안내하되 원래 권위 validation schema의 필드와 값 제약을 유지한다.

상세 Plan의 `goal_coverage.task_ids`는 Skeleton의 AC 기여 Task 집합을 보존하며, `validation_ids`의 소유 Task를 제한하지 않는다. **AC가 각 Task에 명시한 검사 절차**가 있으면 해당 AC의 `validation_ids`에 적용 대상 모든 Task의 자체 필수 검사 ID를 연결한다. 전역 constraint가 요구한 검사는 해당 Task에 존재해야 하지만 그 전역 의무만으로 특정 AC 연결을 만들지 않는다. 검사 소유 Task가 AC 기여 목록에 없어도 명시 AC 연결은 필요하며, 연결을 추가하기 위해 기여 집합이나 Task 의미를 바꾸지 않는다. 특정 Task에만 적용되는 요구의 범위도 유지한다. Reviewer는 자체 검사 존재와 해당 AC의 검사 ID 연결을 각각 확인하고, 연결 누락은 Goal·Task validation·Goal coverage를 직접 근거로 제출한다.

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

`plan-inspection-v2`에서 adapter는 결속된 입력 원문으로 immutable citation catalog를 먼저 만들고 모델은 실제 판단에 사용한 ID만 선택한다. 모델은 validation의 tool·phase·직접 근거와 실제 절차·상태만 나타내는 최소 scope, 모든 constraint×Task의 적용 여부와 실제 validation ID, Reviewer finding의 종류·복구 가능성·주 target ID를 직접 제출한다. AC 연결은 scope 판정과 분리한다. 모델은 Goal이 실제 supported 절차를 명시적으로 요구한다고 판단한 `ac_scope_requirements`만 AC별 criterion과 supported scope ID 집합으로 제출하며, validation ID와 전체 AC×validation 행렬은 다시 작성하지 않는다. 동일 절차를 Task와 Goal validation이 각각 실행하면 각 validation 소유 scope를 선택한다. AC가 특정 Task 또는 Goal validation 단계에 검사 책임을 열거하면 그 단계에서 열거된 책임을 실제 수행하는 scope를 선택하되, 단계의 경계·순서만 나타내는 문구나 같은 Task·phase·evidence·결과 주제를 묶음 의무로 확대하지 않는다. 고정 복합 target인 AC×validation과 constraint×Task는 adapter가 content hash `inspection_target_catalog` ID로 제공한다. Reviewer는 결함 종류에 맞는 복합 catalog ID, 자신이 만든 scope ID, validation ID 또는 citation ID를 `primary_target_ids`에서 선택하고, 추가 직접 citation과 소유 관계로 계산할 수 없는 영향 Task만 별도 목록에 둔다. 다섯 표준 defect kind 밖의 직접 결함은 `other`와 직접 gate·severity, citation ID로 보존한다. 이 선택은 의미 판단이며 adapter가 생성·삭제·교정하지 않는다. Expander는 같은 직접 검사 구조를 사용하되 finding 없이 일관된 완성 Plan만 제출한다. Reviewer의 finding/rating 상호배타성과 Core의 admission·score·상태 판정 권위는 그대로 유지한다.

v2 adapter는 Goal의 사용자 요청·outcome·AC·constraint·preference·assumption·effect와 Skeleton/Plan의 목적·입출력·완료·검사·효과 문장, 등록 자료 본문만 semantic citation catalog로 투영한다. Goal source trace와 State·ProjectMap의 ID·digest·path 장부는 직접 의미 근거 후보에서 제외하며, 그 revision·digest·root·freshness·요청 binding은 기존 Core·preflight가 역할 호출 전에 결정적으로 검사한다. 역할에는 선택된 provider version의 규칙만 넣고 v2에는 v1의 반복 장부 작성 지침을 제공하지 않는다. citation catalog는 source·selector·연속 quote의 content hash ID로 만들고, 복합 target catalog는 kind와 두 원본 ref의 content hash ID로 만든다. 호출 뒤 같은 입력에서 두 catalog를 다시 계산해 변조·누락을 차단한다. 등록 파일 본문은 요청에 노출한 instruction·reference entry만 포함한다. validation statement와 Goal AC·constraint의 고정 claim ref는 원본 ID·selector join으로 붙인다. 희소 양의 `ac_scope_requirements`를 scope 소유 validation과 고정 AC×validation 조합에 join하여 모든 조합의 `ac_link_required`와 `scope_ids`를 결정적으로 확장하고, validation·scope·AC·constraint·finding의 참조 closure와 coverage membership witness를 계산한다. 생략된 조합은 false와 빈 scope 집합이다. Reviewer가 고른 target ID는 결함 종류에 맞는 내부 typed target으로 해석한다. 검증된 `project:<entry_id>` citation은 Reviewer evidence에서 `source:project_map`으로 환산하고, finding의 `evidence_refs`와 `affected_task_refs`, 다섯 표준 kind의 gate·severity와 모든 finding의 결정적 summary는 해석된 target closure와 동결 taxonomy에서 계산한다. `other`의 직접 gate·severity는 그대로 보존한다. 이 파생은 모델이 선택한 scope claim·status, 양의 AC scope 선택, finding 종류·target ID·직접 evidence를 보정하거나 없는 관계·검사 능력·근거를 새로 만드는 작업이 아니다. 존재하지 않는 ID, 원문 불일치, closure 모순은 제출 실패로 보존한다.

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
- 실제 호출 직전에 App Server `model/list` 원본 JSON 전체를 엄격히 검증하고 감사용 inventory 원문·digest와 실행용 v2 operational lock을 각각 결속한다.
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

`OperationalBinding`은 전체 inventory와 그 digest, v2 projection과 그 digest를 함께 보존하고 역산 검증한다. 역할 요청은 준비 당시 binding을 보유하고, receipt는 호출 직전 실제 observation을 별도로 기록한다. 두 전체 digest가 달라도 projection이 같으면 실행하며, 요청·관측·receipt를 서로 다른 digest로 위조한 경우에는 거부한다. Task intent에는 실제 inventory observation을 기록한다. materialize, 역할 호출, dispatch, 내부·공개 resume, 독립 Goal Test가 같은 검증기를 사용한다. 선택을 바꿔 실패를 감추거나 preflight 실패 후 schema recovery로 재호출하지 않는다.

제한 진단의 새 prepare는 과거 입력·모델 설정·executable 기준을 provenance로 읽고 새로운 v2 preflight를 만든다. 과거 전체 inventory digest와의 정확한 일치를 실행 조건으로 사용하지 않는다. 평가의 검사·threshold·oracle·taxonomy, 역할 모델 설정과 fallback 정책은 이 revision으로 보정하지 않는다. 결정적 구현 검증과 실제 역할 qualification은 계속 별개의 Gate다.

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

### 11.1 Development-diagnostic 단계 A

단계 A의 development-diagnostic 실행 모드는 사전에 고정한 서로 독립적인 static 11사례를 관측한다. 모델 호출은 사례당 하나로 하고 전체 최대 11회이며 schema recovery는 0회다. 이는 기존 Reviewer v1의 행 내부 검사와 첫 실패 중단을 바꾸지 않는다. qualification 13의 기존 첫 실패 정책과 `expansion → 독립 생성 검토 → expanded-review` 경계도 유지한다.

정상 완료 사례와, receipt·terminal·lock 귀속이 완료된 model/schema/semantic FAIL만 다음 독립 사례로 진행할 수 있다. 환경, 계약, 입력 stale 또는 외부 효과 불명은 즉시 전체 실행을 중단한다. 관측한 실패는 FAIL로 그대로 보존하고 호출하지 못한 나머지 사례는 NOT_RUN으로 기록한다. 이 흐름은 실패를 재시도하거나 사례 사이에서 의미 판단을 보정하는 경로가 아니다.

공통 preflight는 실험용 detached worktree의 HEAD, source manifest, clean tracked files, 전용 Python identity와 실제 `flowmarshal` import origin을 결속한다. fixture whitelist package와 relocation proof, 명시한 Codex executable, roles와 instruction의 actual source, model lock도 같은 실행 입력으로 고정한다. origin/main과 다른 checkout의 HEAD는 시작 provenance로만 기록하며 실행 중 비교하지 않는다.

새 고정 diagnostics는 실제 역할 thread의 `ephemeral=false`를 preflight에 고정하고 thread 생성 intent와 provider receipt를 대조한다. 모델 turn 없는 지침 probe는 기존 ephemeral 방식이고 일반 역할 runner의 기본값도 유지한다. 프로세스 중단 뒤에는 저장된 thread를 `thread/read`로 먼저 관측하며, 저장 설정 자체를 완료·usage 복구·재실행 권한으로 해석하지 않는다. 장시간 진단은 대화의 포그라운드 실행 세션 밖에서 시작하고 launch intent·PID·시작 시각·출력 경로를 별도 운영 기록에 남긴다. 기존 실행의 summary나 미확인 turn을 새 결과로 덮어쓰지 않는다.

단계 A는 payload 의미나 oracle을 수정하지 않고 과거 FAIL을 보정하지 않는다. 11사례가 모두 관측되어도 이는 development-diagnostic 완료일 뿐 기존 qualification 또는 cutover PASS를 의미하지 않는다.

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
