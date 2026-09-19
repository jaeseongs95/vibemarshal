# ADR: FlowMarshal Engine 분리와 1.0 cutover

- 상태: **Accepted（설계）/ 재설계 구현·검증 planned**
- 대상: 기존 FlowMarshal Domain Core를 유지하는 1.0 Engine
- 권위: [승인 12항목과 필수 검증](redesign-1.0-contract.md), [제품 설계](orchestration-redesign.md)

## Context

R1~R3.1 prototype은 유용한 transport·원장·Planner 실험 자산이자 동결된 감사 기준선이다. 기존 Engine Core·revision·DAG·binding·evidence·validation을 전면 재작성하지 않고 승인·운영 계약을 바꾼다. 현재 사용자의 최신 명시 승인은 과거 사용자 제공 지침과 프로젝트 문서의 usage 누락 전역 차단·exact Plan 수동 승인·비교 성능 필수 릴리스 조건보다 우선한다.

## Decision

1. 새 제품 코드는 `flowmarshal.engine`에 유지한다. 기존 `flowmarshal.core`·`flowmarshal.planning` 도메인은 import하지 않고 검증된 Codex transport만 `CodexRuntimePort` 뒤에서 재사용한다.
2. SQLite application ID `0x464D4531` (`FME1`), 기본 파일명 `flowmarshal-engine.sqlite3`, 기본 artifact root `.flowmarshal-engine/artifacts`의 분리 원칙을 유지한다. application ID와 schema revision을 각각 검사한다.
3. schema 4는 별도 새 DB로 만든다 (**planned**). schema 3/raw receipt/history는 제품 runtime과 분리한 최소 read-only inspector로 원래 상태·receipt·usage/history만 조회한다. 새 Engine 실행·import·상태 전이는 지원하지 않으며 운영·prototype DB의 자동 제자리 변환, 가짜 Goal/Profile 생성, 옛 token budget 재해석을 금지한다. 조율 메타데이터 DB migration과 혼동하지 않는다.
4. 사용자 목표·범위·효과·운영 정책을 GoalAuthorization으로 승인받는다. Core는 적합한 내부 immutable Plan revision을 자동 활성화한다 (**planned**). root·알려진 효과·정책의 결정적 대조와 의미 범위 review를 구분하며 OS sandbox나 의미 안전성 보장을 주장하지 않는다.
5. 실행·provider turn·외부 효과·슬롯과 usage 관측을 분리한다 (**planned**). 외부 효과는 provider/system, target·account, operation, scope, idempotency key와 checkpoint policy를 가진 typed identity로 승인·intent·receipt·재관측을 결속한다. 1.0 Task는 내부 파일·명령 효과와 외부 효과를 섞지 않고 dependency로 분리한다. 현재 1.0의 외부 효과 완료는 같은 실행 Attempt의 terminal·valid provider call과 thread/turn, 같은 identity의 typed adapter receipt, provider가 일치하는 대상 재관측을 모두 요구한다. `(provider, system, provider_operation_id)`는 프로젝트와 무관하게 원장 전체에서 한 Attempt에만 결속하며, 같은 Attempt의 완전히 같은 receipt 재기록만 멱등 허용한다. provider terminal은 외부 효과 완료 증거가 아니다. 유효 terminal 결과는 usage 누락만으로 진행을 막지 않고 외부 효과 미확정은 기존 binding과 대상을 먼저 관측한다. usage 구성요소는 제공된 값만 보존하고 각 결측은 null/unknown으로 둔다. 늦은 usage는 기존 실행·효과 상태를 바꾸지 않는 append-only 회계 관측만 추가한다.
6. RuntimeJobSupervisor와 EngineApplication의 짧은 run_once tick을 연결한다 (**planned**). 활성화 후 준비·worker·검사·recovery/replanning을 모두 checkpoint에 포함하고 supervisor는 활성 job 동안만 연결·deadline을 관리한다. 완료 판정은 Core가 한다.
7. 기본 provider는 qualification된 v1이다. v2 static 11/qualification 13은 v2 자체 채택 조건이며 모든 제품 실행의 선행조건이 아니다. inventory 전체 원문은 감사용, 선택·허용 조합/executable/필수 capability projection은 운영 binding으로 유지한다. `model/list`는 요청 조합의 지원 여부만 증명한다. provider가 turn별 model/effort를 응답이나 자체 session 기록에 명시하지 않으면 요청값을 실제 적용값으로 표기하지 않는다. 임의 fallback·version 간 checkpoint 재사용을 금지한다.
8. package는 Engine-only 사용자 CLI와 필요한 shared canonical 자산을 포함한다. source checkout 없이 `config init`으로 inventory 검증된 사용자 소유 역할 설정을 만들며, 선택적 budget JSON은 사용자가 따로 관리한다. `call_reservation_tokens`는 deprecated 호환 입력일 뿐 admission·요금·구독 한도 계산에 쓰지 않는다. legacy/eval/developer 도구 entrypoint를 분리하고 source-tree 평가는 명시 source root와 재현 입력 bundle을 요구한다. wheel에 없는 fixture/config를 가정하지 않는다.
9. 개발 CLI/package 이름은 `flowmarshal-engine`이다. 최종 1.0 package name·version·entrypoint·사용자 설정 표면은 release freeze 전에 확정하고, 아래 필수 검증과 독립 감사는 그 최종 candidate wheel digest를 대상으로 수행한다. qualification 뒤 metadata를 바꾼 새 wheel에 기존 동일-digest 근거를 재사용하지 않는다. 최신 작업 지시에 따라 개발·커밋은 `main` 브랜치 main checkout에서 수행한다. main에서 작업했다는 사실은 릴리스 Gate 통과가 아니다. 원격 push·공개 릴리스·PyPI 업로드는 이 승인의 범위가 아니다. main의 무관한 변경은 보존한다.
10. 원장·receipt 값은 `provider_observed`, `client_requested`, `local_derived`, `model_reported` provenance를 구분한다. requested/observed model·effort, provider inventory digest, adapter capability digest와 provenance를 직렬화한다. observed model/effort는 `provider_raw_response`로 식별된 원문에서 provider가 명시적으로 제공한 경우, 또는 Claude CLI session 기록에서 그 turn의 assistant 줄이 명시한 한 쌍인 경우(`claude_session_transcript`)에만 출처와 함께 기록하며 request echo·표식 없는 payload·부분 관측은 null로 둔다. 모델의 error code·완료·효과·confidence 주장을 provider 관측이나 Core 판정으로 승격하지 않는다. 모든 활성 실행은 `max_provider_calls`와 `absolute_deadline`을 hard stop으로 결속하며, token stop은 사용자 opt-in인 관측량 기반 best-effort 정책으로만 제공한다.
11. semantic Validator는 Worker와 다른 Attempt·RuntimeJob·thread/turn에서 원자료를 새로 관측하고 자체 terminal provider receipt와 `model_review` evidence binding을 남긴다. `SemanticValidationObservation`과 `ValidationResult`의 PASS/FAIL, validation/task ID와 content digest가 정확히 일치해야 하며, 사용한 evidence를 재사용하거나 재결속하지 않는다. 다른 model/effort 표기만으로 독립성을 충족했다고 보지 않는다.
12. ProjectMap은 Goal 범위의 파일·지침·등록 자료에서 시작해 필요한 symbol·검증된 연결만 lazy 관측한다. 관측 경로가 비어 있으면 적용 `AGENTS.md`와 명시 등록 자료만 기록하며 저장소 파일·symbol·module/test/build 관계를 기본 탐색하거나 추론하지 않는다. Goal 원문의 파일명·상대 경로 후보 탐색은 bounded path 비교만 하며 본문·symbol·연결 관측을 대신하지 않는다. 1.0 복구는 로컬 Context 해소·effect unknown observe-first와 직접 evidence에 결속한 실제 repair/replan 한 경로를 입증하되 범용 자율 복구기나 전체 symbol/module graph를 포함하지 않는다.

13. 활성화 뒤 모든 실행 Task는 agent-governance-suite workflow gate를 반드시 지난다(구현·결정적 검증, 실측·qualification 미실행). gate 판정은 Core 완료 판정에 더하는 AND 차단 조건이며 steward 판단은 validation·Goal Test가 아니다. 기준선은 작업 트리 스냅샷 commit이고, gate 효과는 CoreOperations intent·완료 결과로 원장에 남겨 재시작 뒤 같은 run을 이어 간다. Codex provider는 관측 근거가 확인될 때까지 명시적으로 멈춘다. 상세는 [재설계 문서 9.1](orchestration-redesign.md#91-agent-governance-suite-필수-gate)이 권위다.

승인 경계는 `TrustedConsoleHost → ApplicationAuthority → EngineApplication → EngineService / CoreActionAuthority → SQLite Core ledger`다. Console의 전체 `target_digest` 확인 뒤에만 Goal 전용 일회성 capability를 발급하고, effect checkpoint는 별도 target·capability 타입을 사용한다. Worker·Validator의 역할 scope에서는 권위 객체와 DB handle을 제공하거나 host에 재진입할 수 없다. 이 Application 권위 보장은 같은 OS 사용자의 raw SQLite 직접 쓰기나 hostile same-process 코드 격리를 포함하지 않는다. 두 경계는 1.0 known limitation이며 후속 broker/process identity/ACL hardening의 대상이다.

## Cutover 조건 — planned / 미실행

다음 책임을 모두 충족해야 한다. 정확한 임계값·책임별 evidence와 구현 task 연결은 [V01·V02](redesign-1.0-contract.md)를 따른다.

- 전체 campaign보다 먼저 실제 요청 수직 canary와 대표 effect-unknown fault를 통과. 동일한 동결 입력의 canary cell은 아래 수량에 포함
- 결정적 schema/DAG/원장/정책/검사 Gate·변경 영향 회귀 및 schema 호환 검증
- 실제 역할 48회 전 cell 완료와 recall·precision·critical/clean/schema/seed 기준
- 실제 Planning 18회 전 cell 완료와 정상 선택·진짜 정보 부족 질문·호출/후보 한도
- 실제 요청부터 Goal 정규화·독립 review·Plan 선택·한 번의 승인·다중 Task/복구·독립 검사·최종 결과까지의 E2E와 안전 책임 전부. 모든 evidence artifact는 허용 root·SHA-256·cell/fixture/seed/freeze에 결속한다. release project E2E는 절대 경로 candidate wheel과 SHA-256·배포판 이름/버전·non-editable 설치·import 경로·wheel package bytes를 harness와 최종 verifier에서 재확인하며 source 기반 진단은 release PASS로 승격하지 않음
- 깨끗한 non-editable 설치, 서로 독립인 두 최종 감사와 결정적 finding join
- agent-governance-suite 필수 연동의 후속 책임: `host-integration.json`과 호스트 중립 서명 CLI가 들어간 플러그인 release, 적합성 검사 모듈과 그 호출 지점(채택 명령, freeze build, E2E preflight, 처음 보는 identity의 런타임 검사), release freeze·E2E cell evidence의 플러그인 identity 결속과 final report 표기, 실제 steward로 같은 파일을 여러 Task가 고치는 Goal 실측. 플러그인은 commit·digest·버전으로 고정하지 않는다([1.0 계약 D13](redesign-1.0-contract.md))

하나라도 필수 책임이 누락·실패·미실행이면 1.0 전환은 NO-GO다. 합성 smoke·과거 51개/1,056개 검사·미리 작성한 Goal/Plan/rating으로 실제 신규 qualification을 대신하지 않는다. 공통 계약이 바뀐 최초 실제 역할·Planning 평가는 새로 수행하며 판정 후 oracle·threshold를 낮추지 않는다.

결정적/fake canary로 계약 우회를 먼저 막은 뒤 최종 package identity를 포함한 candidate wheel·최소 harness·결정적/설치 검증과 release input freeze를 마친다. 같은 freeze의 live canary가 통과하면 Role 48과 Planning 18을 독립 shard로 병렬 실행하고, 가능한 FM-14 lane도 immutable 입력과 분리 artifact 조건에서 병렬화한다. 제품 scheduler Gate는 격리 Engine DB에서 연속·동시·재시작 `run_once` subprocess tick으로 검증하며 사용자의 Codex 예약 `ACTIVE`/`PAUSED` 상태를 입력으로 쓰지 않는다. 두 최종 감사는 서로의 결론을 보지 않고 병렬 수행하며 Core가 finding을 결정적으로 join한다. FM-09 개발 조율 자동화와 실제 Codex 예약 연동은 delivery/선택형 운영 검사로 유지하고 제품 기능의 critical path에 두지 않는다.

동결 candidate wheel의 깨끗한 non-editable 설치는 한 번 완전 검증한다. cutover postverify는 같은 wheel digest·lock·환경 provenance와 최종 package identity가 유지됐는지 확인하고 로컬 활성화·설정·smoke만 검사한다. qualification 뒤 entrypoint·package name·version을 바꾸지 않으며 wheel이나 필수 환경이 바뀌면 이전 설치·E2E 근거를 재사용하지 않는다.

## Consequences와 비차단 범위

GoalAuthorization과 자동 activation, schema 4, supervisor, 패키징, 새 qualification은 각 구현 task의 증거와 함께 검증한다. 이 ADR 자체는 1.0 qualification 완료를 뜻하지 않는다. 과거 원시 결과·fixture·receipt·History·R1~R3.1 동결 근거는 수정하지 않는다.

R3.1 token/speed·performance36·비교 lifecycle 최적화는 별도 비차단 보고다. 과거 `TokenLatencyGateReport` v3.0, `PerformanceQualificationReport` v4.0과 `ReleasePerformanceFloor` 수치·판정은 [이전 비교 계약](performance-release-floor-before-redesign-1.0.md)으로 보존한다. 과거 보고서의 cutover 필드를 현재 1.0 권위로 사용하지 않는다. GUI·Localizer/번역·MCTS/광범위 graph·동일 프로젝트 병렬·remote/multiOS hardening은 1.0 이후다.
