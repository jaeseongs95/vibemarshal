# VibeMarshal

VibeMarshal은 큰 개발 요청을 검증 가능한 목표와 Task DAG로 정리하고, Codex 작업의 실행·관측·검사·복구를 로컬 원장 하나에서 추적하는 Workflow Orchestrator다. 제품명은 **VibeMarshal**이며 Python 패키지와 CLI의 기술 식별자는 `FlowMarshal`, `flowmarshal-engine`이다.

> **개발 상태:** 현재 버전은 `0.2.0a1` 프리릴리스다. 사용자 façade, schema 4 원장, RuntimeJob은 구현돼 있다. 다만 1.0 필수 qualification과 독립 최종 감사가 끝나지 않았으므로 1.0 판정은 **NO-GO**다. `apps/desktop` GUI는 아직 실제 Engine에 연결되지 않은 UX 프로토타입이다.

## 무엇을 해결하나

- 사용자 요청을 Goal Contract로 정규화하고 독립 검토한다.
- 실행 가능한 Plan과 의존성이 있는 Task DAG를 만든다.
- 사용자는 목표·범위·허용 효과·운영 정책을 승인하고, Core는 그 경계 안에서 Plan revision을 활성화한다.
- ready Task만 실행 명세로 구체화하고 실제 효과 직전에 입력 freshness와 대상을 다시 확인한다.
- Codex 실행의 intent, receipt, thread/turn binding과 evidence를 SQLite 원장에 남긴다.
- 모델의 완료 선언이 아니라 validation과 GoalVerdict로 완료 여부를 결정한다.
- 중단·재개·실패 분류·제한된 재계획을 원장 상태에 따라 처리한다.

```text
요청 → prepare → authorize → run-once / observe → validation → final-report
```

현재 권위 구현은 `flowmarshal.engine`이다. R1~R3.1 코드는 감사 가능한 `legacy/prototype` 기준선으로 보존하며 새 Engine의 도메인 구현으로 가져오지 않는다.

## 설치

Python 3.10 이상이 필요하다. 프리릴리스 wheel은 source checkout과 분리된 가상환경에 절대 경로로 설치한다.

```powershell
python -m venv C:\absolute\flowmarshal-venv
C:\absolute\flowmarshal-venv\Scripts\python.exe -m pip install `
  C:\absolute\dist\flowmarshal_engine-0.2.0a1-py3-none-any.whl
C:\absolute\flowmarshal-venv\Scripts\flowmarshal-engine.exe --help
```

이 예시는 1.0 qualification 완료를 뜻하지 않는다. wheel 경계와 고정 의존성은 [Engine 설치 계약](docs/engine-package-install.md)에서 확인할 수 있다.

## 기본 워크플로

wheel만 설치한 환경에서도 저장소의 `config/` 파일 없이 시작할 수 있다. 상태 경로와 관리할 프로젝트 경로를 절대 경로로 정한 뒤, 현재 Codex의 `model/list`에 있는 model/effort로 역할 설정을 만든다.

```powershell
$ProjectRoot = (Resolve-Path "C:\path\to\my-project").Path
$StateRoot = (New-Item -ItemType Directory -Force (Join-Path $ProjectRoot ".flowmarshal-engine")).FullName
$Database = Join-Path $StateRoot "flowmarshal-engine.sqlite3"
$Artifacts = Join-Path $StateRoot "artifacts"
$RoleConfig = Join-Path $StateRoot "roles.json"
$FlowMarshal = "C:\absolute\flowmarshal-venv\Scripts\flowmarshal-engine.exe"

& $FlowMarshal config init `
  --output $RoleConfig `
  --model "<model-id>" `
  --effort high

& $FlowMarshal --db $Database --artifacts $Artifacts project init `
  --name "my-project" `
  --root $ProjectRoot
```

`config init`은 지정한 값을 일곱 역할에 적용하고, 파일을 쓰기 전에 현재 model inventory와 대조한다. 역할 하나만 바꾸려면 `--role "validator=<model-id>:<effort>"`를 덧붙인다. 기존 파일은 덮어쓰지 않는다. 생성된 JSON은 사용자 소유 설정이며, 패키지는 특정 모델을 범용 기본값으로 정하지 않는다.

`project init`이 출력한 `project_id`로 요청을 준비하고 승인한 뒤 첫 scheduler tick을 실행한다.

설치된 `flowmarshal-engine` 명령은 `TrustedConsoleHost`를 통해 승인 대상을 표시한다. `authorize`는 현재 Goal과 project root, Core가 실제 활성화할 선택 Plan의 identity·revision·definition/activation digest, 효과 정책, 운영 정책, budget 정책 및 전체 `target_digest`를 보여 주며, 대화형 console에서 그 digest 전체를 그대로 입력해야만 같은 host process의 `ApplicationAuthority`가 일회성 capability를 발급한다. Core는 쓰기 전에 선택 Plan을 포함한 최신 target을 다시 계산하므로 다른 입력·EOF·비대화형 실행·표시 후 Goal/root/policy/Plan 선택 변경은 승인 기록 없이 중단된다. `--source`는 감사 표식일 뿐 승인 권한이 아니다. 이 process-local 경계는 hostile same-process 코드를 격리하지 않으며, OS 또는 broker 격리가 없는 같은 사용자 subprocess의 raw SQLite 접근도 1.0 known limitation이다. 상세 경계는 [승인 계약 D02](docs/redesign-1.0-contract.md)를 따른다.

```powershell
& $FlowMarshal --db $Database --artifacts $Artifacts prepare `
  --project-id <project-id> `
  --request "두 모듈을 수정하고 회귀 테스트까지 실행해 주세요." `
  --role-config $RoleConfig

& $FlowMarshal --db $Database --artifacts $Artifacts authorize `
  --project-id <project-id>

& $FlowMarshal --db $Database --artifacts $Artifacts run-once `
  --project-id <project-id> `
  --role-config $RoleConfig
```

`run-once`는 한 번의 상태 전이 또는 RuntimeJob 예약·관측만 수행하고 반환한다. 다음 tick은 `status`부터 확인하고, 활성 provider job이 있으면 `observe`를 호출한다.

```powershell
& $FlowMarshal --db $Database --artifacts $Artifacts status --project-id <project-id>
& $FlowMarshal --db $Database --artifacts $Artifacts observe --project-id <project-id>
& $FlowMarshal --db $Database --artifacts $Artifacts run-once --project-id <project-id> --role-config $RoleConfig
& $FlowMarshal --db $Database --artifacts $Artifacts final-report --project-id <project-id> --format markdown
```

GoalVerdict가 확정될 때까지 필요한 tick 수는 Plan과 원장 상태에 따라 달라진다. 사용자는 내부 Plan ID나 digest를 승인 입력으로 복사할 필요가 없다. 반복 실행과 observe-first 복구는 [Engine 사용자 workflow](docs/engine-user-workflow.md)를 따르며, 실패 뒤 자동 복구 범위와 `status`의 `recovery` 읽는 법은 같은 문서의 [자동 복구 읽기](docs/engine-user-workflow.md#자동-복구-읽기)에 있다.

관측 token 정책은 선택 사항이다. `project budget set`에 넘기는 정책은 사용자가 별도 파일로 관리하며, 측정된 token을 기준으로 한 best-effort 중단 정책일 뿐 요금이나 구독 한도 상한이 아니다. `call_reservation_tokens`는 deprecated 호환 필드이며 admission 계산에 쓰지 않는다.

## 사용자 CLI

| 명령 | 역할 |
|---|---|
| `config init` | 명시한 model/effort를 inventory에 대조하고 user-owned 역할 설정 생성 |
| `prepare` | 요청 정규화, 독립 review와 Planning 준비 |
| `authorize` | 목표 범위와 운영 정책 승인, 적합한 Plan 활성화 |
| `run-once` | bounded scheduler tick 한 번 수행 |
| `observe` | 활성 provider job 관측 |
| `pause` | 현재 Goal workflow 일시정지 |
| `cancel` | 현재 Goal workflow 취소 |
| `status` | Core, control, job 상태 조회 |
| `final-report` | GoalVerdict에 근거한 최종 보고 출력 |

`project`, `model`, `goal`, `plan`, `task`, `run`, `attempt`, `validate`, `recover`, `report` 아래에는 진단과 세부 운영을 위한 중첩 명령이 있다. 전체 목록은 `flowmarshal-engine --help`와 각 명령의 `--help`에서 확인할 수 있다. 반복 실행과 재시작·receipt 복구 절차는 [Engine 사용자 workflow](docs/engine-user-workflow.md)에 정리되어 있다.

## 데이터와 안전 경계

별도 `--db`, `--artifacts` 옵션을 주지 않으면 CLI를 실행한 현재 작업 디렉터리 아래에 저장한다.

```text
.flowmarshal-engine/flowmarshal-engine.sqlite3
.flowmarshal-engine/artifacts/
```

- Engine writer는 schema 4의 새 DB만 만들며 schema 3 원장과 과거 receipt/history는 read-only adapter로 연다.
- prototype이나 운영 DB를 제자리 migration하지 않는다.
- materialize 이후 입력이나 대상이 바뀌면 `STALE_EXECUTION_INPUT`으로 실행을 중단한다.
- 결과가 불명확한 외부 intent는 자동으로 다시 실행하지 않고 기존 binding과 provider 상태를 먼저 관측한다.
- 같은 프로젝트의 Attempt는 qualification 전까지 직렬로 실행한다.
- 필수 evidence와 validation을 확인해야 Task와 Goal을 완료할 수 있다.

세부 권위와 불변조건은 [전면 재설계 문서](docs/orchestration-redesign.md)와 [1.0 계약](docs/redesign-1.0-contract.md)에 정의되어 있다.

## 개발과 검증

저장소의 개발·평가 모듈까지 검사하려면 사용자 설치와 분리된 개발 환경에 editable로 설치한다.

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_engine*.py' -v
.\.venv\Scripts\python.exe -m flowmarshal.engine.smoke --project-root <project-root>
.\.venv\Scripts\python.exe -m pip check
```

이 검사는 로컬 개발 확인용이다. 실제 역할 48회, Planning 18회, 실제 요청 E2E, 깨끗한 설치와 독립 최종 감사까지 모두 통과해야 1.0으로 승격한다. 상세 실행 계약은 [Engine qualification](docs/engine-qualification.md)을 따른다.

## GUI 프로토타입

`apps/desktop`에는 React/TypeScript 기반의 클릭형 UX 프로토타입이 있다. 패키지 관리자는 `pnpm@11.19.0`을 사용한다.

```powershell
cd apps\desktop
pnpm install
pnpm dev
```

이 앱은 `MockEngineClient`의 합성 snapshot을 사용한다. 실제 Engine, SQLite, Codex runtime을 읽거나 변경하지 않으며 현재 1.0 판정에도 영향을 주지 않는다. 화면과 Engine 연결 경계는 [GUI 인터페이스 설계](docs/gui-interface-design.md)에 정리되어 있다.

## 문서

| 문서 | 내용 |
|---|---|
| [전면 재설계](docs/orchestration-redesign.md) | 제품 목적, 권위 구조와 실행 설계 |
| [1.0 계약](docs/redesign-1.0-contract.md) | 1.0 수용 기준과 필수 검증 |
| [Engine cutover ADR](docs/engine-cutover-adr.md) | legacy 분리와 1.0 전환 결정 |
| [설치 계약](docs/engine-package-install.md) | wheel, 의존성, schema 경계 |
| [문서 지도](docs/README.md) | 전체 문서와 역사적 근거 색인 |

## 코드 지도

| 영역 | 경로 |
|---|---|
| 권위 schema와 불변조건 | `src/flowmarshal/engine/domain.py` |
| SQLite 원장과 History | `src/flowmarshal/engine/ledger.py` |
| Goal 정규화와 검토 | `src/flowmarshal/engine/goal.py` |
| Project Map과 Context Pack | `src/flowmarshal/engine/context.py` |
| Skeleton-first Planning | `src/flowmarshal/engine/planning.py` |
| 응용 façade | `src/flowmarshal/engine/application.py` |
| RuntimeJob과 Codex adapter | `src/flowmarshal/engine/runtime.py` |
| 서비스와 상태 전이 | `src/flowmarshal/engine/service.py` |
| CLI와 보고 | `src/flowmarshal/engine/cli.py`, `src/flowmarshal/engine/reporting.py` |
