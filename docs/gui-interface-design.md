# FlowMarshal GUI 인터페이스 설계명세

- 문서 상태: 제안(구현 전 설계 기준)
- 대상: `flowmarshal.engine` / 개발용 `flowmarshal-engine 0.2.0a1`
- 작성일: 2026-09-05
- 관련 권위 문서: [전면 재설계 권위 문서](orchestration-redesign.md), [Engine cutover ADR](engine-cutover-adr.md), [R3.1 동결 기준선](r31-frozen-baseline.md)

## 1. 목적

현재 CLI 중심으로 개발하는 FlowMarshal에 향후 데스크톱 GUI를 추가하되, 다음을 보장하는 구조를 정의한다.

1. CLI와 GUI가 동일한 Core 상태 전이와 검증 규칙을 사용한다.
2. GUI를 추가해도 SQLite 원장, revision, digest, evidence, validation, recovery의 권위가 분산되지 않는다.
3. GUI가 종료되거나 다시 시작되어도 실행을 중복 생성하지 않고 기존 intent·receipt·thread binding을 먼저 관측한다.
4. 화면 기술을 바꾸더라도 Engine의 공개 application contract는 유지한다.
5. 현재 CLI 개발 단계에서 GUI 친화적인 경계를 미리 만들되, 아직 필요하지 않은 배포·동기화 복잡성은 도입하지 않는다.

이 문서는 GUI 화면 시안만을 다루지 않는다. CLI와 GUI가 공유할 application API, 프로세스 경계, 상태 동기화, 사용자 승인, 복구 UX, 보안, 배포와 검증 기준을 함께 고정한다.

## 2. 설계 결정 요약

### 2.1 핵심 결정

GUI는 SQLite를 직접 읽거나 쓰는 독립 애플리케이션이 아니라, `flowmarshal.engine`의 **비권위 클라이언트**로 만든다.

```text
CLI ───────────────┐
                   │
Desktop GUI ───────┼─> EngineApplication ─> EngineService ─> SQLiteEngineLedger
                   │            │
Automation/Trigger ┘            └─> EngineDispatcher ─> CodexRuntimePort
                                                        └─> Codex App Server
```

- `EngineService`와 Domain Core만 권위 상태를 전이한다.
- `EngineDispatcher.run_once()`가 실행을 한 단계 전진시키는 기본 단위다.
- CLI, GUI, 자동화는 같은 `EngineApplication` command/query API를 호출한다.
- GUI는 후보·상태·가능한 동작을 표시하지만 status, admission, score, 완료 여부를 자체 계산하지 않는다.
- 데스크톱 패키징의 우선 후보는 **Tauri 2 + 웹 프런트엔드 + Python Engine sidecar**다. 다만 Engine 공개 계약은 Tauri에 의존하지 않게 설계한다.
- GUI와 Engine sidecar 사이의 1차 transport는 loopback HTTP/JSON + SSE로 한다. 추후 named pipe 등으로 바꿀 수 있도록 transport adapter로 격리한다.

### 2.2 지금 해야 할 일과 나중에 할 일

| 시점 | 해야 할 일 |
|---|---|
| CLI 개발 중 | CLI handler에 흩어진 조립·직접 SQL을 `EngineApplication`과 typed query로 이동한다. command/query DTO, 오류 계약, History cursor, runtime session 경계를 만든다. |
| 읽기 전용 GUI 단계 | 프로젝트·Goal·Plan·Task·Attempt·evidence·비용·History를 조회하는 로컬 API와 웹 대시보드를 추가한다. |
| 제어 GUI 단계 | Goal 작성, Plan 비교/활성화, `run once`, interrupt, recovery checkpoint 같은 mutation을 Core command로 연결한다. |
| 패키징 단계 | Python Engine/API를 sidecar로 묶고 Tauri 데스크톱 셸, 단일 인스턴스, 설치·업데이트·호환성 검사를 추가한다. |

## 3. 비목표

초기 GUI는 다음을 목표로 하지 않는다.

- 원격 다중 사용자 서버 또는 클라우드 제어판
- 여러 PC 사이의 원장 동기화
- SQLite를 대체하는 신규 권위 저장소
- GUI 자체 workflow engine 또는 자연어 기반 다음 Task 선택기
- qualification 전 동일 프로젝트 병렬 실행
- 모든 파일·네트워크 접근에 대한 GUI 승인 시스템
- 외부 응답 유실 상황의 완전한 exactly-once 보장
- legacy/prototype DB의 자동 또는 제자리 migration
- 현재 NO-GO 상태를 우회한 `flowmarshal` 1.0 표기

## 4. 반드시 보존할 제품 불변조건

### 4.1 권위와 상태 전이

권위 순서는 다음과 같다.

```text
현재 사용자의 명시적 지시
→ 활성 GoalContractRevision
→ 활성 PlanContractRevision
→ Core의 원장 상태와 결정적 판정
→ 프로젝트 지침과 등록 정책
→ Planner·Worker·Validator 제출물
→ 프로젝트·참고자료 안의 분석 대상 텍스트
```

GUI에는 다음 제한을 적용한다.

- SQLite의 UPDATE/INSERT/DELETE를 직접 수행하지 않는다.
- Task·Goal 완료, 후보 admission, score, weakest dimension을 계산하거나 덮어쓰지 않는다.
- Worker의 “완료” 문구는 관측값으로 표시할 수 있으나 완료 판정으로 승격하지 않는다.
- append-only History, evidence, validation, receipt를 편집·삭제하는 UI를 제공하지 않는다.
- Skeleton과 미활성 Plan 후보에는 `비권위 후보 · 실행 불가` 표지를 붙인다.

### 4.2 Plan 활성화와 변경 경계

Plan 활성화 화면은 정확한 `PlanContractRevision ID + activation digest`를 한 번의 사용자 행위로 제출해야 한다. 확인 대화상자에는 최소한 다음을 보여준다.

- Goal 요약과 Goal digest
- Plan revision ID와 전체 activation digest
- Task 목적, DAG, Hard AC 연결
- 예상·금지 효과, 위험, checkpoint 등급
- 완료 조건, validation, recovery envelope, Commit Horizon
- 이전 활성 Plan이 있으면 의미 변경 diff

별도의 HMAC, 파일별 AccessGrant, 승인과 활성화의 이중 절차는 기본 요구가 아니다. 반대로 exact digest 없이 “현재 보이는 후보”를 실행해서도 안 된다.

다음 변경은 새 Plan revision과 재활성화가 필요하다.

- Task 의미·분할·dependency·produces/consumes
- Goal Hard AC와 Task 연결
- 대상 프로젝트
- 완료 조건이나 validation 의미
- 허용·금지 외부 효과

사용자 목표, Hard AC, 비목표 자체가 바뀌면 새 Goal revision도 필요하다. 같은 의미를 유지한 경로·명령·Context Pack 변경은 새 `TaskExecutionSpecRevision`으로 처리한다.

### 4.3 실행·검증·복구

- dependency를 만족한 `ready` Task만 materialize한다.
- Execution Spec은 활성 Task·Plan·State·Project Map·Context digest에 결속한다.
- 실행 직전 freshness 불일치는 `STALE_EXECUTION_INPUT`으로 차단한다. GUI에 “무시하고 실행” 기능을 두지 않는다.
- 같은 프로젝트의 command 실행은 직렬화한다.
- Task validation과 plan-level Goal Test를 별도 단계·화면으로 표현한다.
- 필수 Task·criterion·integration evidence가 모두 확인되어야 Goal을 `satisfied`로 표시한다.
- 미확인 receipt나 완료 관측 없는 외부 효과는 `external_unknown`으로 유지하고 기존 intent·binding·provider 상태를 먼저 관측한다.
- 복구 시 새 Task나 thread를 추측 생성하지 않는다. 실제 후속 turn이 필요할 때만 마지막 검증 checkpoint에서 resume한다.
- 실패 분류와 권장 repair는 표시하되 Core 계약 변경을 자동 적용하지 않는다.

### 4.4 모델·권한·비용

- 모델·effort를 GUI 코드에 하드코딩하지 않는다.
- 실제 호출 직전 `model/list`와 Plan의 model lock/fallback envelope를 대조한다.
- 지원되지 않는 model/effort를 조용히 바꾸지 않는다.
- `danger-full-access`는 OS 실행 정책이지 Plan 활성화나 Core 상태 변경 권한이 아님을 구분한다.
- usage 미제공 값은 `0`이 아니라 `알 수 없음`과 사유로 표시한다.

## 5. 권장 아키텍처

### 5.1 계층 구조

```text
┌──────────────────────────────────────────────────────────────┐
│ Presentation                                                 │
│ Tauri shell + React/TypeScript UI                            │
│ Router · screens · view state · accessibility · localization │
└───────────────────────────┬──────────────────────────────────┘
                            │ typed client
┌───────────────────────────▼──────────────────────────────────┐
│ Transport Adapter                                             │
│ Local HTTP/JSON commands · queries · SSE events               │
│ auth token · API version handshake · request correlation      │
└───────────────────────────┬──────────────────────────────────┘
                            │ Command/Query DTO
┌───────────────────────────▼──────────────────────────────────┐
│ EngineApplication                                             │
│ CommandFacade · QueryFacade · RuntimeSessionManager           │
│ command serialization · error mapping · view-model projection │
└───────────────┬───────────────────────────────┬───────────────┘
                │                               │
┌───────────────▼──────────────┐   ┌────────────▼───────────────┐
│ EngineService / Domain Core  │   │ EngineDispatcher           │
│ authoritative transitions   │   │ one deterministic step     │
└───────────────┬──────────────┘   └────────────┬───────────────┘
                │                               │
┌───────────────▼──────────────┐   ┌────────────▼───────────────┐
│ SQLiteEngineLedger           │   │ CodexRuntimePort            │
│ authority + hash History     │   │ App Server adapter          │
└──────────────────────────────┘   └────────────────────────────┘
```

### 5.2 `EngineApplication`

현재 `EngineService`는 mutation 권위 경계로 유지한다. 새 `EngineApplication`은 UI 전용 business logic을 만드는 계층이 아니라 다음 책임만 가진다.

1. CLI/API 입력을 typed command로 변환한다.
2. `EngineService`, planning/goal pipeline, `EngineDispatcher`를 올바른 순서로 조립한다.
3. 프로젝트별 command를 직렬화한다.
4. raw SQL을 캡슐화한 typed query 결과를 제공한다.
5. Core 예외를 안정된 application error로 변환한다.
6. 장수 Codex runtime의 생성·재연결·종료를 관리한다.
7. 응답에 History cursor와 관련 entity ref를 포함한다.

권장 공개 표면은 다음과 같다.

```python
class EngineApplication(Protocol):
    def execute(self, command: EngineCommand) -> CommandResult: ...
    def project_overview(self, project_id: str) -> ProjectOverview: ...
    def plan_comparison(self, project_id: str) -> PlanComparison: ...
    def task_detail(self, task_id: str) -> TaskDetail: ...
    def attempt_detail(self, attempt_id: str) -> AttemptDetail: ...
    def events(self, query: EventQuery) -> EventPage: ...
    def run_once(self, command: RunOnceCommand) -> RunOnceOutcome: ...
```

도메인 모델과 API DTO를 동일 타입으로 고정하지 않는다. Domain schema는 권위 계약이고, API DTO는 표시·호환성·redaction을 위한 외부 계약이다. API DTO가 domain payload를 포함할 때는 `schema_version`, `revision_id`, `digest`를 손실 없이 보존한다.

### 5.3 현재 CLI에서 먼저 분리할 부분

현재 다음 CLI handler는 직접 SQL 또는 CLI 내부 조립에 의존한다.

- `plan compare`
- `task show`
- `attempt show`
- `report final`
- live Goal preparation과 Plan search의 runtime/role 조립
- `run once`의 runtime 생명주기와 turn 대기

이 로직을 GUI에서 복제하지 않는다. 먼저 typed query/application service로 이동하고, 기존 CLI handler는 argument parsing과 JSON 출력만 맡게 한다.

```text
Before: argparse handler → SQL / runtime 조립 / service
After : argparse handler → EngineApplication command/query → JSON renderer
```

CLI subprocess 호출은 초기 진단용 bridge로만 허용한다. GUI의 최종 공개 계약으로 삼지 않는다. subprocess 방식은 프로세스마다 runtime을 새로 만들고, exit code/stdout과 SQLite schema에 결합되며, 실시간 상태·취소·재접속을 다루기 어렵다.

### 5.4 Runtime session manager

GUI backend는 CLI보다 오래 살아 있으므로 `RuntimeSessionManager`가 필요하다.

- Codex App Server runtime을 한 곳에서 소유한다.
- 현재 executable, model inventory digest, permission policy evidence를 노출한다.
- `EngineDispatcher`에 `CodexRuntimePort`를 주입한다.
- 연결 종료 후 저장된 thread binding을 사용해 `read`를 먼저 수행한다.
- in-memory future를 권위 상태로 취급하지 않는다.
- 앱 종료 시 진행 중 외부 효과를 임의 실패/성공으로 바꾸지 않는다.
- 재시작 시 원장의 unreceipted intent와 running/unknown Attempt를 recovery center에 먼저 노출한다.

## 6. 프로세스·배포 구조

### 6.1 권장 데스크톱 구성

```text
flowmarshal-desktop.exe        Tauri shell, window, updater, capability policy
└─ flowmarshal-engine-api.exe  Python sidecar, EngineApplication, local API
   └─ codex.exe                Codex App Server/runtime
```

Tauri는 Python CLI 또는 API server를 external binary(sidecar)로 묶을 수 있으므로 현재 Python Engine을 재작성하지 않고 데스크톱으로 패키징하기 적합하다. Tauri의 capability 설정으로 프런트엔드가 실행할 sidecar와 인자를 제한한다. 참고: [Tauri external binaries](https://v2.tauri.app/develop/sidecar/), [Tauri capabilities](https://v2.tauri.app/security/capabilities/).

초기 지원 OS는 현재 runtime requirement에 맞춰 Windows로 제한한다. macOS/Linux는 Codex runtime, path semantics, packaging, signing, update와 E2E를 별도 qualification한 뒤 연다.

### 6.2 프로세스 소유권

- Tauri shell이 Engine sidecar를 시작하고 종료한다.
- sidecar는 단일 Engine DB의 유일한 application writer다.
- GUI window를 닫을 때 진행 중 turn을 자동 interrupt하지 않는다. 종료 정책을 사용자에게 명시한다.
- tray/background 실행을 지원하지 않는 초기 버전에서는 진행 중 작업이 있으면 종료 전에 상태와 영향을 알린다.
- 동일 DB를 대상으로 두 sidecar가 동시에 writer가 되지 않도록 lock file 또는 OS mutex를 사용한다.
- CLI와 GUI 동시 mutation은 초기에 지원하지 않는다. 다른 writer 감지 시 읽기 전용 상태와 충돌 해결 안내를 제공한다.

### 6.3 버전 handshake

프런트엔드는 시작 시 다음을 확인한다.

```json
{
  "api_version": "1",
  "engine_package": "flowmarshal-engine",
  "engine_version": "0.2.0a1",
  "engine_schema_version": 2,
  "sqlite_application_id": "FME1",
  "minimum_ui_api_version": "1",
  "product_status": "NO-GO"
}
```

- 호환되지 않는 API/DB schema에는 mutation UI를 열지 않는다.
- 미래 schema를 구버전 GUI가 자동 변환하지 않는다.
- prototype DB를 감지하면 현재 Engine DB로 자동 migration하지 않는다.
- GUI alpha와 Engine 1.0 cutover 표기를 분리한다.

## 7. Local API 계약

### 7.1 기본 규칙

- prefix: `/api/v1`
- command: `POST`
- query: `GET`
- 긴 작업: `202 Accepted` + durable entity/operation ref
- 동시성 충돌: `409 Conflict`
- stale digest: `409` + `STALE_EXECUTION_INPUT`
- 추가 입력 필요: `422` + structured `CONTEXT_REQUIRED`
- 외부 효과 상태 불명: `409` + `EXTERNAL_UNKNOWN`
- request body에는 클라이언트가 계산한 권위 상태를 받지 않는다.
- mutation에는 `Idempotency-Key`와 expected digest/cursor를 요구한다.
- OpenAPI 문서는 server/client 계약 검증과 TypeScript client 생성에 사용한다. OpenAPI는 언어 독립적인 HTTP API 계약을 제공한다. 참고: [OpenAPI Specification](https://spec.openapis.org/oas/latest.html).

### 7.2 주요 query

| Method | Path | 용도 |
|---|---|---|
| `GET` | `/health` | API·Engine·DB 호환성 및 runtime 상태 |
| `GET` | `/projects` | 프로젝트 목록·요약 |
| `GET` | `/projects/{project_id}` | 프로젝트 overview와 최신 cursor |
| `GET` | `/projects/{project_id}/goals` | Goal revision 계보·활성 상태 |
| `GET` | `/projects/{project_id}/plans` | 후보/활성 Plan과 Core decision |
| `GET` | `/plans/{plan_revision_id}` | Plan 계약·DAG·AC/validation 연결 |
| `GET` | `/tasks/{task_id}` | Task 계약·spec·validation·evidence |
| `GET` | `/attempts/{attempt_id}` | Attempt·intent·receipt·binding·failure |
| `GET` | `/projects/{project_id}/history` | `after_sequence` 기반 History page |
| `GET` | `/projects/{project_id}/budget` | 단계별 usage·latency·결측 사유 |
| `GET` | `/projects/{project_id}/recovery` | unknown intent와 가능한 복구 동작 |
| `GET` | `/projects/{project_id}/report` | progress/final view model |

조회 응답에는 가능한 경우 다음 공통 필드를 포함한다.

```json
{
  "api_version": "1",
  "project_id": "project_...",
  "history_cursor": {"sequence": 42, "event_hash": "sha256:..."},
  "generated_at": "2026-09-05T00:00:00Z",
  "data": {}
}
```

### 7.3 주요 command

| Method | Path | Core 동작 |
|---|---|---|
| `POST` | `/projects` | project와 profile 생성 |
| `POST` | `/projects/{id}/sources` | context source 등록 |
| `POST` | `/projects/{id}/goals` | Goal preparation/등록 |
| `POST` | `/projects/{id}/goals/{rev}/revise` | 새 Goal revision |
| `POST` | `/projects/{id}/plan-searches` | Skeleton-first plan search |
| `POST` | `/plans/{plan_revision_id}/activate` | exact ID+digest 활성화 |
| `POST` | `/projects/{id}/steps` | `run_once()` 한 단계 실행 |
| `POST` | `/attempts/{id}/observe` | 기존 provider 상태 우선 관측 |
| `POST` | `/attempts/{id}/resume` | 관측 후 허용된 resume |
| `POST` | `/attempts/{id}/interrupt` | 현재 turn interrupt |
| `POST` | `/tasks/{id}/retry` | 새 evidence를 결속한 retry |
| `POST` | `/recovery/intents/{id}/reconcile` | receipt/binding 대조 |
| `POST` | `/recovery/intents/{id}/abandon` | 사용자 rationale을 남긴 abandon |

Plan 활성화 예시는 다음과 같다.

```json
{
  "plan_revision_id": "plan_revision_...",
  "activation_digest": "sha256:...",
  "expected_goal_digest": "sha256:...",
  "expected_history_sequence": 41,
  "source": "desktop-gui:user-confirmation"
}
```

서버는 화면에 보였던 snapshot이 여전히 최신인지 확인하고, 불일치하면 현재 cursor와 변경 요약을 반환한다. 클라이언트는 자동 재전송하지 않고 새 계약을 다시 보여준다.

### 7.4 연속 실행 제어

Core의 권위 실행 단위는 계속 `run_once()`다. GUI의 “계속 실행”은 별도 workflow 판단기가 아니라 다음 정책을 가진 application controller다.

```text
run_once
→ 새 History/snapshot 표시
→ checkpoint·context·stale·external_unknown 여부 확인
→ 차단이 없고 사용자가 허용한 범위면 다음 run_once 예약
→ Goal terminal 또는 blocker에서 정지
```

- 다음 Task는 Core의 ready 상태가 결정한다.
- checkpoint가 필요한 외부 효과에서는 반드시 멈춘다.
- 오류·재접속 시 자동 command 재전송보다 idempotency 결과 조회를 먼저 수행한다.
- 초기 GUI는 프로젝트 하나당 연속 실행 controller 하나만 허용한다.

### 7.5 이벤트 stream

서버→클라이언트 단방향 진행 알림에는 SSE를 사용한다. command는 기존 HTTP POST로 유지하므로 WebSocket의 양방향 복잡성이 필요하지 않다. FastAPI는 `text/event-stream` 기반 SSE 응답을 제공한다. 참고: [FastAPI SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/).

```text
GET /api/v1/projects/{project_id}/events?after_sequence=42
Accept: text/event-stream
```

```json
{
  "sequence": 43,
  "event_hash": "sha256:...",
  "event_type": "attempt.observed",
  "entity_type": "attempt",
  "entity_id": "attempt_...",
  "occurred_at": "2026-09-05T00:00:01Z",
  "summary": {},
  "requires_snapshot_refresh": true
}
```

History event는 UI 캐시 갱신 신호이지 별도 권위 상태가 아니다. 연결 복구 시 다음 순서를 사용한다.

1. 마지막 `sequence + event_hash`로 event 재요청
2. hash gap, retention gap, chain 검증 실패가 있으면 전체 snapshot 재조회
3. snapshot의 새 cursor 이후 stream 재연결
4. optimistic UI 상태 제거

provider token stream이나 로그는 History와 분리된 진단 stream으로 다룬다. 모델 출력 조각을 Task 상태 이벤트로 변환하지 않는다.

### 7.6 오류 형식

모든 오류는 안정된 code와 사용자 조치 정보를 제공한다.

```json
{
  "error": {
    "code": "STALE_EXECUTION_INPUT",
    "message": "Execution Spec 입력이 현재 상태와 다릅니다.",
    "retryable": false,
    "suggested_action": "execution_spec_revision",
    "checkpoint_required": false,
    "related_entities": ["task_...", "execution_spec_revision_..."],
    "history_cursor": {"sequence": 52, "event_hash": "sha256:..."},
    "details": {}
  }
}
```

최소 표준 code는 다음을 포함한다.

- `PERMISSION_POLICY_MISMATCH`
- `CONTEXT_REQUIRED`
- `STALE_EXECUTION_INPUT`
- `REVISION_CONFLICT`
- `DIGEST_MISMATCH`
- `CHECKPOINT_REQUIRED`
- `EXTERNAL_UNKNOWN`
- `MODEL_BINDING_UNAVAILABLE`
- `VALIDATION_FAILED`
- `RECOVERY_LIMIT_REACHED`
- `ENGINE_BUSY`
- `API_VERSION_MISMATCH`

## 8. Read model과 상태 동기화

### 8.1 Typed read model

현재 `project_snapshot()`의 raw `dict`만을 GUI 계약으로 사용하지 않는다. 다음 read model을 정의한다.

| Read model | 핵심 내용 |
|---|---|
| `ProjectSummary` | root, active Goal/Plan, 전체 상태, blocker, 최근 활동 |
| `GoalRevisionView` | 원문, outcome, Hard AC, non-goal, effect policy, preparation binding, 계보 |
| `PlanComparisonView` | 후보 decision, score, finding, Task/DAG, 의미 diff, activation 가능 여부 |
| `TaskDetailView` | 계약, 상태, dependency, current spec, evidence, validations, possible actions |
| `AttemptDetailView` | 상태, intent, receipt, thread/turn, observation, failure/recovery |
| `HistoryEventView` | sequence/hash, event/entity, redacted payload, timestamp |
| `BudgetSummaryView` | stage/model/effort, token, cache, latency, retry, availability reason |
| `RecoveryCaseView` | 불명확한 효과, 확인된 근거, 허용 가능한 다음 command |

`possible_actions`는 프런트엔드가 enum 조합으로 추론하지 않고 application query가 계산한다. 단, 이 계산은 새 상태 전이가 아니라 “현재 Core 계약상 호출 가능한 command”의 투영이다. 서버는 실제 command 시점에 전조건을 다시 검증한다.

### 8.2 경쟁 상태

- 프로젝트별 mutation queue를 둔다.
- SQLite transaction과 History sequence를 최종 concurrency 경계로 사용한다.
- command는 `expected_history_sequence` 또는 관련 active digest를 받는다.
- 충돌 시 `409`와 최신 cursor를 반환한다.
- GUI는 서버 확인 전 Task/Goal 상태를 완료로 낙관 반영하지 않는다.
- 버튼 중복 클릭은 같은 `Idempotency-Key`로 같은 결과를 조회한다.

## 9. 화면 정보구조

### 9.1 전역 레이아웃

```text
┌ 프로젝트 목록 ┬──────────────── 작업 영역 ────────────────┬ 상태/감사 ┐
│                │                                           │          │
│ 프로젝트 A     │ Goal / Plan / Run / Recovery / Report     │ runtime  │
│ 프로젝트 B     │                                           │ model    │
│                │ 선택 화면의 주요 콘텐츠                   │ policy   │
│ + 새 프로젝트  │                                           │ history  │
└────────────────┴───────────────────────────────────────────┴──────────┘
```

우측 상태 영역에는 최소한 Engine/API 호환성, runtime 연결, permission evidence, model inventory lock, History chain 검증, active blocker를 보여준다.

### 9.2 주요 화면

#### 프로젝트 홈

- active Goal과 Plan
- Task 상태 집계와 DAG 진행률
- 현재 blocker/checkpoint
- 최근 History
- token/latency 요약
- `한 단계 실행`, `계속 실행`, `복구 센터` 진입

#### Goal 작성 마법사

- 사용자 원문
- observable outcome
- Hard AC와 validation intent
- soft preference, constraint, non-goal의 분리
- mutation/behavior/effect policy
- source trace와 context 요청
- 정규화 후보와 독립 review 결과

모델이 정규화한 결과를 바로 활성 Goal로 보이지 않는다. review와 Core compilation 상태를 구분한다.

#### Plan 비교·활성화

- 최대 2개 상세 Plan 비교
- Task DAG 시각화
- AC coverage와 validation 연결
- 위험·외부 효과·checkpoint
- model/effort와 fallback envelope
- finding/rating과 Core decision의 구분
- exact digest 확인 후 활성화

#### 실행 모니터

- Task별 `pending → ready → materialized → reserved → running → validating → completed/failed/blocked`
- 현재 Attempt와 thread/turn binding
- 실행/검사 역할 구분
- 수집 evidence와 validation
- `run_once` action 이력
- interrupt 가능 여부

#### 복구 센터

- `external_unknown`, stale input, context 부족, permission mismatch를 별도 유형으로 표시
- intent, receipt, binding, 마지막 validated checkpoint
- 먼저 관측해야 하는 이유와 관측 결과
- 허용된 `observe`, `resume`, `abandon`, revision/replan 동작
- abandon/replan의 rationale 입력과 영향

#### 감사·보고

- append-only History와 hash chain 상태
- revision 계보와 digest 복사
- evidence→validation→criterion trace
- token/latency/retry/폐기 비용
- 최종 GoalVerdict와 미충족·불명확 criterion

### 9.3 위험한 동작 UX

- Plan 활성화와 외부 효과 checkpoint는 일반 실행 버튼과 시각적으로 분리한다.
- 삭제·배포·공개·외부 메시지·권한 확대 등 비가역/제3자 효과의 실제 대상과 effect를 확인창에 표시한다.
- digest는 축약 표시할 수 있지만 확인창과 상세 화면에서 전체 값을 볼 수 있어야 한다.
- stale 또는 revision conflict가 발생하면 기존 확인창을 닫고 최신 diff를 다시 보여준다.
- `external_unknown`에는 “재시도” 단일 버튼을 두지 않는다. `먼저 관측`을 기본 동작으로 둔다.

## 10. 로컬 보안

Core의 파일·네트워크 hardening 범위와 GUI transport 보안을 구분한다. GUI의 local API는 다음을 지킨다.

- `127.0.0.1`의 OS가 배정한 임시 port에만 bind한다.
- sidecar 시작 시 256-bit 이상 session token을 생성해 stdin 또는 상속 handle로 전달한다.
- token을 command line, URL, 로그, History에 기록하지 않는다.
- 모든 API와 SSE에 bearer token을 요구한다.
- 허용 Origin을 Tauri 앱 origin으로 제한하고 임의 browser origin을 거부한다.
- remote content/navigation을 비활성화하고 엄격한 CSP를 사용한다.
- Tauri capability는 sidecar spawn과 필요한 최소 명령만 허용한다.
- 파일 선택은 사용자가 명시적으로 고른 경로를 application command로 전달한다.
- PromptBundle, raw model response, 환경 변수, credential은 기본 화면과 일반 event payload에서 redaction한다.
- 진단 export는 포함 범위를 미리 보여주고 credential·운영 DB 원본을 기본 제외한다.

API token은 사용자 인증이나 다중 사용자 권한 모델을 대체하지 않는다. 원격 접속을 추가할 때는 이 local-only 설계를 확장하지 말고 별도 threat model과 인증·권한·TLS·감사를 설계한다.

## 11. 기술 선택

### 11.1 권장안

| 영역 | 권장 기술 | 이유 |
|---|---|---|
| Engine | 기존 Python + Pydantic | 현재 권위 구현과 strict schema 재사용 |
| Application API | Python typed facade | CLI/GUI 공통 계약, transport 독립성 |
| Local HTTP | FastAPI 계열 adapter | Pydantic 연동, OpenAPI, SSE 지원 |
| Desktop shell | Tauri 2 | Python sidecar 패키징, capability 기반 권한 제한 |
| Frontend | React + TypeScript + Vite | 복잡한 DAG/상태 UI와 typed client 생태계 |
| Server event | SSE | 진행 알림이 주로 단방향이고 재연결 cursor가 단순함 |
| 권위 저장소 | 기존 SQLite Engine ledger | 단일 권위와 현재 복구 계약 유지 |
| UI cache | 메모리 query cache | 삭제 가능한 비권위 projection만 허용 |

FastAPI, React, Tauri는 adapter/프런트엔드 선택이다. `flowmarshal.engine.domain`, `EngineService`, `EngineDispatcher`가 이 프레임워크를 import해서는 안 된다.

### 11.2 대안 비교

| 대안 | 장점 | 단점 | 판단 |
|---|---|---|---|
| PySide/PyQt가 Python을 직접 호출 | 단일 언어, 초기 연결이 빠름 | UI와 Engine process 격리가 약하고 웹/원격 UI 재사용이 어려움 | 소규모 내부 도구라면 가능하나 기본안 아님 |
| Electron + web UI | 성숙한 웹 생태계와 디버깅 | 런타임·패키지 크기와 Node 프로세스 관리 증가 | Tauri 제약이 확인되면 대안 |
| 브라우저 전용 localhost UI | 구현·개발이 가장 단순 | 설치·단일 인스턴스·OS 통합·origin 제어 UX가 약함 | 읽기 전용 GUI prototype에 적합 |
| CLI subprocess만 호출 | Core 변경이 적음 | 실시간 진행, 취소, 오류 타입, runtime 생명주기, 출력 호환성에 취약 | 임시 bridge만 허용 |

최종 Tauri 채택은 작은 spike로 다음을 확인한 뒤 확정한다.

- Python sidecar 시작/종료와 crash 감지
- Codex child process와 Windows console 숨김
- SSE 재연결과 대용량 History 성능
- 설치본에서 DB/artifact 경로 및 권한
- 코드 서명·업데이트 시 sidecar 무결성
- 백신 오탐과 패키지 크기

## 12. 패키지 구조 제안

```text
src/flowmarshal/engine/
├─ domain.py                       # 기존 권위 schema
├─ service.py                      # 기존 권위 mutation
├─ ledger.py                       # 기존 권위 persistence
├─ runtime.py                      # 기존 dispatcher/runtime port
├─ application/
│  ├─ __init__.py
│  ├─ app.py                       # EngineApplication
│  ├─ commands.py                  # command DTO와 handler
│  ├─ queries.py                   # typed read model/query
│  ├─ events.py                    # History cursor/event page
│  ├─ errors.py                    # 안정된 application error
│  └─ runtime_sessions.py          # 장수 runtime 생명주기
└─ transports/
   ├─ cli_adapter.py               # argparse handler가 호출
   └─ http_api.py                  # 나중에 추가

apps/desktop/
├─ src/                            # React/TypeScript
├─ src-tauri/                      # Tauri shell/capabilities
└─ generated/engine-client/        # OpenAPI 생성 client
```

현재 `flowmarshal.application`은 legacy 계열이므로 새 GUI가 import하지 않는다. 새 계층은 반드시 `flowmarshal.engine.application` 아래에 둔다.

## 13. 구현 단계와 종료 기준

### G0. CLI/Application 경계 정리

산출물:

- `EngineApplication` command/query facade
- typed `ProjectOverview`, `TaskDetail`, `AttemptDetail`, `EventPage`
- CLI 직접 SQL 제거
- CLI 내부 live role/runtime 조립 이동
- 표준 application error와 idempotency 입력
- History `after_sequence` query

종료 기준:

- 기존 CLI JSON 의미와 exit code 회귀가 통과한다.
- CLI와 application facade가 동일 Core 결과를 낸다.
- GUI framework dependency를 Core에 추가하지 않는다.

### G1. 읽기 전용 로컬 API와 대시보드

산출물:

- `/api/v1` query와 SSE
- read-only browser prototype
- 프로젝트 홈, DAG, Attempt, History, 비용 화면

종료 기준:

- UI가 DB 파일을 직접 열지 않는다.
- event gap/reconnect 후 snapshot과 일치한다.
- History chain 실패를 정상 상태로 숨기지 않는다.

### G2. 사용자 command와 복구 UX

산출물:

- Goal 작성, Plan 검색/비교/활성화
- `run_once`, observe, interrupt, resume
- checkpoint와 recovery center
- exact digest/expected cursor/idempotency 계약

종료 기준:

- stale activation과 stale Execution Spec을 차단한다.
- crash/restart 후 unknown intent를 중복 실행하지 않는다.
- Task validation과 Goal Test가 UI와 E2E에서 구분된다.

### G3. Windows 데스크톱 패키징

산출물:

- Tauri shell과 Python sidecar
- single-instance writer lock
- local token/origin/CSP/capability
- 설치·제거·업데이트·DB 호환성 검사

종료 기준:

- 설치본에서 정상 실행·중단·재시작·복구 E2E가 통과한다.
- sidecar/runtime crash에서 원장과 intent/receipt 무결성이 유지된다.
- GUI alpha가 Engine 1.0으로 오표기되지 않는다.

### G4. 공개 GUI 준비

산출물:

- 접근성, 키보드 내비게이션, 한국어/영어 문자열 분리
- 진단 export/redaction
- 성능·장기 실행·업데이트 rollback 검증
- GUI-specific qualification report

종료 기준:

- Engine cutover Gate와 GUI Gate를 각각 통과한다.
- 현재 제품 상태와 실제 qualification 결과가 UI/About/보고서에 일치한다.

## 14. 검증 전략

### 14.1 계약 테스트

- command DTO → Core 호출 → History/응답 mapping
- query DTO와 SQLite schema 간 typed mapping
- OpenAPI snapshot/golden test
- TypeScript 생성 client compile test
- enum 추가 시 UI unknown-value fallback
- error code와 HTTP status mapping

### 14.2 핵심 불변조건 E2E

1. exact digest가 다른 Plan 활성화 거부
2. materialize 뒤 입력 변경 시 `STALE_EXECUTION_INPUT`
3. incomplete dependency Task materialize 거부
4. permission policy mismatch에서 provider call 0회
5. unknown receipt 상황에서 새 thread/Attempt 중복 없음
6. 저장 thread를 read한 뒤에만 필요한 resume 수행
7. Worker 완료 문구만으로 Task 완료되지 않음
8. Task validation과 Goal Test가 각각 evidence에 결속됨
9. 새 evidence 없는 재계획 한도 초과 차단
10. usage 미제공이 0으로 집계되지 않음
11. GUI/CLI가 같은 command에서 같은 권위 결과 생성
12. event stream reconnect 후 snapshot/history cursor 일치

### 14.3 데스크톱 테스트

- sidecar 비정상 종료·Codex child 종료·UI 강제 종료
- suspend/resume와 네트워크 일시 단절
- 중복 클릭·뒤로 가기·여러 창 command 경쟁
- 대형 DAG와 긴 History 렌더링
- keyboard-only 사용과 screen reader label
- CSP/origin/token 거부와 remote navigation 차단
- 오래된 UI↔새 Engine, 새 UI↔오래된 Engine 호환 거부
- 설치 경로에 한글·공백이 있는 Windows 환경

## 15. 비기능 요구사항

| 항목 | 초기 목표 |
|---|---|
| 권위 일관성 | command 응답 전에 Core transaction과 History 기록 완료 |
| 복구 | UI/sidecar 재시작 후 원장 기반 snapshot과 unknown intent 복원 |
| 조회 성능 | 로컬 프로젝트 overview p95 200ms 이하(대표 fixture 기준) |
| event 지연 | History commit 후 UI 반영 p95 500ms 이하 |
| 확장성 | 1,000 Task·100,000 History event에서 cursor pagination 유지 |
| 접근성 | WCAG 2.2 AA를 목표로 색상 외 상태 표현·키보드 조작 제공 |
| 관측성 | request ID, Core operation/entity ref, History cursor를 로그에 결속 |
| 개인정보/비밀 | token·credential·raw 환경 값은 기본 로그/화면/export에서 제외 |

성능 수치는 구현 전 목표이며 실제 대표 fixture와 측정 환경을 qualification 문서에 고정해야 한다. 측정하지 않은 목표를 PASS로 기록하지 않는다.

## 16. 수용 기준

GUI 인터페이스 기반 구현은 다음을 모두 만족해야 완료로 본다.

- [ ] GUI와 CLI가 `flowmarshal.engine.application`을 공유한다.
- [ ] GUI가 SQLite를 직접 쓰거나 status/완료를 계산하지 않는다.
- [ ] CLI handler의 직접 SQL 조회가 typed query facade로 이동했다.
- [ ] Plan 활성화는 exact revision ID·digest·expected state를 검증한다.
- [ ] 모든 mutation은 idempotency와 프로젝트별 직렬화를 가진다.
- [ ] History cursor 기반 reconnect와 gap 복구가 검증됐다.
- [ ] runtime 재시작은 저장 binding을 먼저 관측하고 중복 실행하지 않는다.
- [ ] permission, stale, context, checkpoint, external unknown이 서로 다른 UX로 표현된다.
- [ ] Task validation과 Goal Test, Worker 관측과 Core 판정이 화면에서 구분된다.
- [ ] model inventory·permission·usage 결측을 사실대로 표시한다.
- [ ] local API token, origin, CSP, capability와 secret redaction이 적용됐다.
- [ ] API/Engine/DB/UI version handshake가 호환되지 않는 mutation을 차단한다.
- [ ] legacy/prototype DB를 자동 migration하거나 현재 Engine 상태로 표시하지 않는다.
- [ ] CLI 회귀, API 계약, 핵심 복구, 데스크톱 E2E가 실제로 통과했다.

## 17. 금지할 구현 패턴

1. React/Tauri에서 SQLite 파일을 직접 열기
2. CLI stdout 문자열을 파싱해 영구 GUI 계약으로 사용하기
3. 프런트엔드 enum만으로 `possible_actions`나 완료 상태를 결정하기
4. 화면에 보인 오래된 digest를 서버 재검증 없이 활성화하기
5. SSE event만 재생해 권위 snapshot을 재구성하기
6. 네트워크 오류 뒤 mutation을 무조건 자동 재전송하기
7. unknown receipt를 retry 버튼 하나로 처리하기
8. 앱 재시작 시 새 thread를 만들고 이전 binding을 버리기
9. model/effort fallback과 permission mismatch를 사용자에게 숨기기
10. GUI 편의를 위해 immutable revision/evidence/History를 수정하기
11. Task validation을 Goal Test로 대신하거나 반대로 합치기
12. legacy `flowmarshal.application.FlowMarshalService`를 새 Engine GUI facade로 재사용하기

## 18. 미결정 사항

다음은 architecture spike 또는 제품 결정 후 확정한다.

- Tauri 최종 채택 여부와 Electron/PySide 대비 실제 패키징 결과
- Python sidecar 번들러(PyInstaller/Nuitka 등)
- GUI 종료 시 background 실행/tray 지원 여부
- named pipe transport 추가 여부
- 진단용 provider progress stream의 보존 기간과 redaction 수준
- Engine schema upgrade의 backup·rollback 정책
- 원격 UI 또는 모바일 companion의 별도 제품 범위

이 항목들은 `EngineApplication`과 Core 권위 경계를 바꾸지 않는 범위에서 결정한다. 원격 다중 사용자 지원처럼 권위·보안·동시성 모델을 바꾸는 요구는 이 명세의 단순 확장이 아니라 별도 설계 revision으로 다룬다.

## 19. 최종 권고

지금 GUI 코드를 시작할 필요는 없지만, **CLI를 제품 로직의 최종 경계로 굳히지 않는 것**이 중요하다. 우선 G0에서 `EngineApplication`, typed query, History cursor, runtime session 경계를 만들고 CLI를 첫 번째 adapter로 전환한다. 그러면 향후 GUI는 Core를 다시 만들지 않고 두 번째 adapter로 추가할 수 있다.

GUI 구현 시에는 읽기 전용 대시보드부터 시작해 상태·event·재접속 모델을 검증한 뒤, exact-digest 활성화와 복구처럼 위험한 command를 단계적으로 연다. 데스크톱 기술은 Tauri 2를 우선 검증하되, 화면 프레임워크 선택이 FlowMarshal의 권위 계약을 바꾸지 않게 유지한다.
