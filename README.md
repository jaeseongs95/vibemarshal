# VibeMarshal

VibeMarshal은 사용자의 큰 요청을 검증 가능한 Task DAG로 만들고, Task마다 적절한 Codex 실행·검사 모델과 추론 수준을 배정한 뒤, 작업 생성·재개·진행·결과·실패·재시도를 추적하는 로컬 Workflow Orchestrator다. 코드와 CLI에서는 기존 식별자 `FlowMarshal`을 사용한다.

현재 권위 구현은 `flowmarshal.engine`이다. R1~R3.1 source와 artifact는 감사 가능한 `legacy/prototype` 기준선으로 동결하며 새 Engine에서 도메인 코드로 import하지 않는다.

자세한 계약은 [전면 재설계 권위 문서](docs/orchestration-redesign.md), 분리·전환 결정은 [Engine cutover ADR](docs/engine-cutover-adr.md)에 있다.

## 현재 판정

최신 상태의 단일 진입점은 [Engine 구현 현황](docs/engine-implementation-status.md)이다. [1.0 선행 로드맵](docs/pre-1.0-roadmap.md)에 승인된 구현 순서와 후속 기능을 구분했고, [현재 작업 인계](docs/pre-1.0-handoff.md)에 이번 변경과 다음 실행 조건을 기록한다.

현재 판정은 **NO-GO**다. [실제 역할 48건](docs/role-fixture-48-qualification.md)을 모두 실행했으며 정상 요청 오차단 1건·평가 계약의 형식 불일치 1건으로 Gate가 실패했다. 실측 1,332,724 token은 48회 모두 정산됐다. v9의 호출 전 결정적 Gate 5/5·824개 테스트는 통과했다. 후속 계약 보완·역할 재검증과 Planning 18·E2E 4·성능 36이 남아 있다. 이전 동일 Goal의 두 Task·독립 Goal Test·`satisfied` 판정은 여러 source를 거친 진단·복구 근거로 별도 보존한다.

평가 계약 불일치는 v10에서 수정했고 최종 결정적 Gate 5/5·826개 테스트를 통과했다. 수정 후 실제 역할 qualification은 재검증 전이며, v9 실패 판정·원시 응답·기존 합격선은 그대로 보존한다.

새 Engine schema revision은 **3**이다. Goal별 최종 보고, 전체 역할 usage 예약·정산, 명시적 모델 재결속과 공통 조회를 보완했다. 기존 revision 2 원장을 자동 변환하지 않으며, 사용량이 미확인이면 추가 모델 호출을 중단한다. 권장 검증 예산은 설정 파일로 주입하며 제품 상한으로 하드코딩하지 않는다.

R3.1은 동결된 prototype 감사 기준선이다. [최종 동결 판정](docs/r31-frozen-baseline.md)을 현재 Engine의 qualification으로 재사용하지 않는다.

## 새 권위 구조

```text
사용자 요청
→ Goal Contract 정규화·독립 검토
→ Project Map과 State Projection
→ Skeleton 1~3개 생성
→ 결정적 Gate·compact review·pruning
→ 최대 2개만 Plan Contract 후보로 상세화
→ 사용자 exact-digest 활성화
→ ready Task의 Execution Spec·Context Pack materialize
→ precondition·snapshot·effect checkpoint
→ Codex 실행·receipt·binding
→ Task validation과 State 재관측
→ plan-level Goal Test
→ Continue | Repair | Subgraph Replan | Goal Revision
```

사용자가 승인하는 단위는 목표·Task 의미·DAG·완료 조건·위험을 담은 `PlanContractRevision`이다. 실제 파일·symbol·명령·Context Pack은 Task가 ready가 될 때 계약 범위 안에서 결정한다. 운영 상세가 바뀔 때마다 반복 승인을 요구하지 않지만, 목표·Task 의미·dependency·완료 조건·외부 효과가 바뀌면 새 Plan Contract가 필요하다.

## 핵심 안전성과 사용성 원칙

- 프로젝트 파일, 전역·프로젝트 `AGENTS.md`, 등록 참고자료, 빌드·테스트와 필요한 localhost는 정상 입력이다.
- 파일마다 AccessGrant를 만들거나 네트워크를 일괄 차단하지 않는다.
- 참고자료 안의 명령문은 분석할 데이터이며 사용자 지시나 활성 계약보다 높은 권위를 갖지 않는다.
- Reviewer는 finding과 rating만 제출한다. Core가 admission·score·상태를 결정한다.
- 실제 모델은 코드에 하드코딩하지 않고 호출 직전 `model/list`와 대조한다.
- 로컬 Codex task는 실제 `:danger-full-access`, `approval_policy=never`일 때만 시작한다.
- 외부 효과는 intent를 먼저 기록하고 receipt·thread·turn binding으로 결속한다.
- 모델의 “완료” 선언이 아니라 evidence와 validation으로 Task와 Goal을 완료한다.
- 같은 프로젝트는 먼저 직렬 실행한다. 병렬화와 VM·WSL·permission hardening은 별도 qualification 뒤에 연다.

## 개발 CLI

```text
flowmarshal-engine project init|show
flowmarshal-engine project source add|list
flowmarshal-engine project budget set|show|observe-role|adjust-unknown
flowmarshal-engine model status|rebind
flowmarshal-engine goal create|revise|show
flowmarshal-engine plan search|compare|activate|status
flowmarshal-engine task show|materialize
flowmarshal-engine run once|status
flowmarshal-engine attempt show|retry|observe|resume|interrupt
flowmarshal-engine validate task|goal|observe
flowmarshal-engine recover inspect|resume|abandon
flowmarshal-engine report progress|final
```

`goal create --live`, `plan search --live`, `run once` 전에 `project budget set --policy-file config/pre-1.0-validation-budget.json`으로 검증 예산을 등록한다. 이 파일은 Goal당 1,000,000 token, 호출당 100,000 token 예약, 재계획 reserve 25%의 시작값이다. 최적값이나 구독 한도 환산값이 아니다. 역할별 timeout은 전역 `--role-timeout-policy config/pre-1.0-role-timeouts.json`으로 결속한다.

`goal create --live`와 `plan search --live`는 역할별 model/effort를 호출자가 명시해야 한다. Engine은 이를 최신 App Server model inventory와 대조하고, 지원되지 않는 값을 임의 fallback으로 숨기지 않는다.

개발 qualification은 별도 CLI로 실행한다. 완료 cell만 immutable checkpoint가 되며 사용량 제한은 `PAUSED_RATE_LIMIT`으로 남겨 같은 run root에서 재개한다.

```powershell
flowmarshal-engine-eval run --scope deterministic
flowmarshal-engine-eval run --scope role-fixture
flowmarshal-engine-eval run --scope full-planning-pipeline
flowmarshal-engine-eval run --scope project-e2e
flowmarshal-engine-eval resume --run-root <run-root>
flowmarshal-engine-eval benchmark --cells-file <36-cell.json> --scope-report <report> ...
flowmarshal-engine-eval cutover --scope-report <report> ... --benchmark-report <report>
```

고정 역할 설정과 상세 합격 기준은 [Engine qualification 실행 지침](docs/engine-qualification.md)을 따른다.

실제 모델 평가에는 `--codex-bin <검증할 codex.exe의 절대 경로>`를 명시할 수 있다. 이 작업에서 SDK 동봉 runtime은 응답 API 404를 반환했고 설치된 앱 runtime을 명시한 실행은 정상 동작했다. 검증한 실행 파일과 실패·성공 범위는 [실행 현황 보고서](docs/engine-implementation-status.md)에 기록한다. 실행 파일을 바꾸면 새 evaluation 계약과 run root를 사용한다.

현재 `benchmark`는 외부 36-cell 입력 검증과 frozen R3.1·Engine의 중립 입력 live 수집 경로를 구현한다. S10~S12의 usage·결측·예산을 연결했고 실제 48회 정산도 감사했다. 측정되지 않은 lifecycle 폐기 비율은 null/NOT_OBSERVED로 남긴다. 수집기 구현·회귀와 실제 성능 Gate 통과는 별개이며, 36-cell 비용 비교는 아직 실행하지 않았다.

동결 검사는 이 저장소 외에 형제 디렉터리 `../자동화템플릿/prototypes/skills/flowmarshal-work-planner`의 원본 Planner 스킬 7개 파일도 요구한다. 새 clone에서 해당 감사 기준선이 없으면 freeze Gate는 실패하며 자동으로 생략하거나 재생성하지 않는다. 인증정보·로컬 실행 DB·Codex home 복제본·평가 작업 디렉터리는 Git에서 제외하고 기존 로컬 파일은 보존한다.

## 설치와 검증

```powershell
cd D:\codex\flowmarshal
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

새 Engine만 빠르게 검사하려면 다음을 사용한다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_engine*.py' -v
.\.venv\Scripts\python.exe -m flowmarshal.engine.smoke --project-root D:\codex\flowmarshal
.\.venv\Scripts\flowmarshal-engine.exe --help
```

기본 상태는 프로젝트 루트의 `.flowmarshal-engine/flowmarshal-engine.sqlite3`와 `.flowmarshal-engine/artifacts`에 저장한다. Engine DB schema revision은 2이고 SQLite application ID `0x464D4531`을 사용한다. revision 1, prototype 또는 alpha DB는 자동·제자리 migration하지 않는다.

## 코드 지도

| 영역 | 파일 |
|---|---|
| 권위 schema와 불변조건 | `src/flowmarshal/engine/domain.py` |
| SQLite 원장·History | `src/flowmarshal/engine/ledger.py` |
| Goal 정규화·검토 | `src/flowmarshal/engine/goal.py` |
| Project Map·Context Pack | `src/flowmarshal/engine/context.py` |
| Skeleton-first search | `src/flowmarshal/engine/planning.py` |
| 실제 Planner 역할 adapter | `src/flowmarshal/engine/planner_roles.py` |
| 모델 inventory·배정 | `src/flowmarshal/engine/models.py` |
| 서비스·상태 전이 | `src/flowmarshal/engine/service.py` |
| Codex Runtime·dispatcher | `src/flowmarshal/engine/runtime.py` |
| 평가·cutover Gate | `src/flowmarshal/engine/evaluation.py` |
| qualification 실행기 | `src/flowmarshal/engine/qualification.py`, `e2e_qualification.py`, `eval_cli.py` |
| CLI와 보고 | `src/flowmarshal/engine/cli.py`, `reporting.py` |

전체 문서와 역사적 증거는 [문서 지도](docs/README.md)에서 찾을 수 있다.
