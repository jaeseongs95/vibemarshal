# FlowMarshal ↔ agent-governance-suite 연동 프로토타입

> 이 spike의 결론에 따라 필수 gate는 제품 코드 `src/flowmarshal/engine/governance_gate.py`로 옮겨졌다. 제품 gate는 작업 트리 스냅샷 commit 기준선(발견 9 해소), 원장 재진입, timeout, 역할 설정 기반 steward를 쓴다. 규칙의 권위는 [재설계 문서 9.1](../../docs/orchestration-redesign.md#91-agent-governance-suite-필수-gate)이다. 아래는 spike 당시 기록이다.

FlowMarshal Engine이 Task마다 agent-governance-suite의 orchestrated workflow를 진행하는 방식이 실제로 동작하는지 확인하는 spike다. 비교용으로 플러그인 없이 같은 빈틈을 막는 결정적 gate(방법론만 채택)도 둔다. 제품 wheel에는 들어가지 않는다. 결과를 보고 연동 형태(선택형 연동, 방법론만 채택, 필수 연동)를 정한다.

## 구성

| 위치 | 역할 |
|---|---|
| `src/flowmarshal/engine/runtime.py` | `EngineDispatcher(task_gate=...)` 선택 인자. gate가 차단 사유를 돌려주면 Worker dispatch 또는 Task 완료를 막는다. 지정하지 않으면 기존 동작과 같다. |
| `bridge.py` | `GovernanceTaskGate`. 플러그인 MCP 서버를 `flowmarshal-engine` 호스트로 띄우고 Task별 workflow를 진행한다. |
| `test_bridge.py` | 가짜 provider 결정적 검사(모델 호출 없음). |
| `scope_gate.py` | `WorkspaceScopeGate`. 플러그인 없이 Worker 전후 프로젝트 파일 내용 digest를 비교해 쓰기 target 밖 변경을 막는 결정적 gate(방법론만 채택 검증용). |
| `tests/fixtures/engine/governance/multitask.py`, `workspace/` | Task 3개 합성 Goal(제품 gate 테스트와 함께 쓰려고 옮겼다)(add 수정 → shout 수정 → add를 쓰는 total 추가). Task가 ready가 될 때 Execution Spec 후보를 만든다. |
| `test_scope_gate.py` | 합성 Goal에서 결정적 gate를 검사한다(모델 호출 없음). |
| `run_real.py` | 실제 Claude Code provider로 합성 fixture를 끝까지 진행하는 1회 측정 스크립트. 기본은 한 Task, `--multitask`는 Task 3개 Goal. |

플러그인 쪽은 agent-governance-suite 브랜치 `claude/flowmarshal-engine-host`(커밋 `98db130`)의 `flowmarshal-engine` host attestation과 서명 CLI(`mcp-server/dist/engine-attestation.mjs`)를 쓴다. `bridge.DEFAULT_PIN`이 commit과 dist 서버·서명 CLI의 sha256을 고정하며, 하나라도 다르면 gate를 만들지 않는다.

## 실행

저장소 루트에서 실행한다. 플러그인 worktree 경로는 `AGS_PLUGIN_ROOT`로 바꿀 수 있다(기본 `D:/claude/mcp변경/ags-engine-host`).

```bash
# 결정적 검사(모델 호출 없음)
FLOWMARSHAL_ENGINE_SOURCE_ROOT=. .venv/Scripts/python.exe -m unittest discover -s spikes/governance_bridge -p "test_*.py"

# 실제 1회 측정(Claude Code CLI 호출: Task마다 steward 최대 4회 + Worker 1회)
FLOWMARSHAL_ENGINE_SOURCE_ROOT=. .venv/Scripts/python.exe spikes/governance_bridge/run_real.py <빈 출력 디렉터리> [--multitask]
```

## 단계 대응

gate는 FlowMarshal 수명주기의 두 지점에 걸린다.

1. **Worker dispatch 직전** (`before_execution`)
   - Task 계약과 현재 Execution Spec으로 TaskEnvelope를 만든다. 쓰기 target이 `scope.included`, 읽기 target이 `scope.excluded`가 된다.
   - steward가 TaskEnvelope를 검토하면 `plan_workflow`, `open_convergence_root`, `claim_workflow_attempt`, `start_guarded_workflow`를 호출한다.
   - 이어서 변경 기준선 stage를 기록한다.
2. **Task 완료 직전** (`before_completion`)
   - Task validation이 모두 PASS인 뒤, `complete_task`와 State 재관측 전에 실행한다.
   - 구현·범위 확인·수용 근거 stage를 기록하고 `finalize_workflow`를 호출한다.

| 플러그인 stage | FlowMarshal 쪽 입력 | stage를 수행하고 관측되는 주체 |
|---|---|---|
| plan_workflow(bootstrap) | Task 계약·Execution Spec → TaskEnvelope | steward |
| 변경 기준선 | `capture-workspace-baseline.mjs`(working-tree) | steward(스크립트 결과 검토) |
| 구현 | Worker Attempt | Worker |
| 범위 확인 | `compare-change-scope.mjs` | steward(스크립트 결과 검토) |
| 수용 근거 | Task validation 결과·evidence → acceptance 요청 | steward(deep 하한) |
| finalize | 모든 stage passed | — |

- 플러그인 판정은 Core 판정에 더해지는 차단 조건(AND)일 뿐이다. Task 완료 판정은 여전히 FlowMarshal Core가 한다.
- plan과 stage 호출에는 그 stage를 실제로 수행한 호출의 관측(model·effort)으로 서명 CLI token을 받아 붙인다. Claude provider에서는 Claude CLI가 저장한 session transcript의 assistant `model`·`effort`를 관측원으로 쓴다.
- 관측이 없으면 token을 만들지 않고 차단한다.

## 측정 결과

### 가짜 provider 결정적 검사 (`test_bridge.py` 8개, `test_scope_gate.py` 4개 통과)

| 사례 | 결과 |
|---|---|
| 정상 Task | 플러그인 workflow가 plan→root→claim→start→stage 4개→finalize `passed`로 끝난 뒤에만 Core가 Task를 완료한다. plan·stage 호출 5회에 token이 붙고, 수용 근거 stage는 deep 하한에 맞춰 Opus급 관측으로 기록된다. |
| 선언 밖 파일 변경(`notes.md` 생성) | 범위 확인이 `NEEDS_APPROVAL`(`unplanned:notes.md`)이 되어 Task가 `blocked`로 남는다. |
| 같은 변경, gate 없음(대조군) | Task가 `completed`된다. 완료 뒤 State 재관측이 선언 밖 파일을 새 기준으로 흡수하는 현재 빈틈이다. |
| stage 하한 미달 관측(Haiku급 steward) | `plan_workflow`가 `BINDING_INVALID`를 반환한다. Worker Attempt를 예약하지 않고 `GOVERNANCE_GATE_BLOCKED`가 된다. |
| 기준선 stage 실패(steward 거절) | 첫 tick과 다음 tick 모두 `GOVERNANCE_GATE_BLOCKED`이고 Worker Attempt는 0건이다. 이미 시작한 플러그인 run은 abort한다. |
| Worker 관측 불가 | token을 만들지 않고 `WORKER_OBSERVATION_UNAVAILABLE`로 Task를 막는다. 기록된 stage는 기준선 하나뿐이다. |
| 플러그인 고정값 불일치 | gate를 만들지 않는다(`PLUGIN_PIN_MISMATCH`). |
| Task 3개 Goal, 플러그인 연동 gate | add·shout Task는 workflow `passed` 뒤 완료된다. 앞 Task가 커밋 없이 바꾼 `app.py`를 다시 고치는 total Task는 범위 확인이 `NEEDS_APPROVAL`(`preexisting-overlap:app.py`)이 되어 `blocked`로 남는다. |
| Task 3개 Goal, 결정적 범위 gate(`scope_gate.py`) | 세 Task가 모두 완료되고 Goal verdict까지 간다. 모델 호출은 없다. |
| 결정적 gate, 선언 밖 새 파일(`notes.md`) | `UNDECLARED_CHANGE: … notes.md`로 add Task가 `blocked`된다. |
| 결정적 gate, shout Task가 `app.py`(자기 쓰기 target이 아님)를 수정 | `UNDECLARED_CHANGE: 쓰기 target 밖 변경 app.py`로 `blocked`된다. |
| 같은 변경, gate 없음(대조군) | Goal verdict까지 완료된다. 다른 Task 파일 수정도 흡수된다. |

### 실제 Claude 1회 (2026-09-19, Claude Code CLI, 합성 fixture `project-e2e`)

**결과:** Task `completed`. 전체 53.1초, 변경은 `app.py`의 `left - right` → `left + right` 한 줄이었다. governance workflow는 5개 호출(plan·stage 4개)이 모두 통과했고 `finalize_workflow`가 `passed`였다.

| 호출 | 요청 model/effort | transcript 관측 | 시간 | 입력 tokens(캐시) | 출력 tokens |
|---|---|---|---|---|---|
| steward: bootstrap | claude-sonnet-5 / high | claude-sonnet-5 / high | 14.9초 | 40,413 (19,732) | 1,140 |
| steward: 기준선 | claude-sonnet-5 / high | claude-sonnet-5 / high | 4.1초 | 18,548 (14,804) | 161 |
| Worker | claude-sonnet-5 / high | claude-sonnet-5 / high | 약 10초 | 74,484 (63,020) | 490 |
| steward: 범위 확인 | claude-sonnet-5 / high | claude-sonnet-5 / high | 4.8초 | 18,696 (14,804) | 206 |
| steward: 수용 근거 | claude-opus-5 / high | claude-opus-5 / high | 5.4초 | 19,107 (0) | 207 |

- **governance 추가분:** steward 4회가 합계 29.2초, 입력 96,764 tokens(캐시 49,340), 출력 1,714 tokens였다. 작은 Task에서는 Worker 1회보다 크다. token 수는 provider가 준 관측값이며 요금이나 구독 한도 차감으로 환산하지 않는다.
- **호출당 고정 비용:** 판단 내용이 거의 없는 기준선·범위 호출도 입력이 약 18.5k tokens였다. 대부분 CLI가 붙이는 고정 입력으로 보인다(추론).
- **첫 실행 시도:** model-lock v2 binding 없이 steward 요청을 만들어 모델 호출 전에 `MODEL_LOCK_VERSION_UNSUPPORTED`로 멈췄다. 고친 뒤 위 실행을 했다.

### 실제 Claude 1회, Task 3개 Goal (2026-09-19 05:54Z, `run_real.py --multitask`)

**결과:** add·shout Task `completed`, total Task는 Worker 실행 전에 막혀 `materialized`로 남았다. 전체 113.4초. 플러그인은 같은 고정값(98db130)이었다.
- total Task의 기준선 stage에서 steward(Sonnet급)가 거절했다. 사유는 "쓰기 target `app.py`에 이미 미커밋 변경이 있어 깨끗한 기준선으로 쓸 수 없다"였다. 그래서 `GOVERNANCE_GATE_BLOCKED`가 됐고 플러그인 run은 abort됐다.
- 가짜 검사에서는 같은 원인이 뒤의 범위 확인 stage에서 드러났다. 실제 모델은 앞의 기준선 stage에서 잡았다. 차단은 같지만 걸리는 지점이 steward 판단에 따라 달라진다.

| Task | steward(호출 수·시간) | steward 입력 tokens(캐시)·출력 | Worker 입력 tokens(캐시)·출력 | 결과 |
|---|---|---|---|---|
| add 수정 | 4회·18.6초 | 76,086 (29,608)·775 | 74,384 (62,962)·503 | completed, finalize passed |
| shout 수정 | 4회·20.3초 | 76,269 (59,127)·919 | 49,590 (39,165)·477 | completed, finalize passed |
| total 추가 | 2회·16.6초 | 58,630 (49,347)·975 | 호출 안 함 | 기준선 거절로 blocked |
| 합계 | 10회·55.5초 | 210,985 (138,082)·2,669 | 123,974 (102,127)·980 | |

- Worker 두 번의 실행 시간은 session 기록의 첫 줄부터 끝 줄까지 6.1초, 7.5초였다.
- steward 요청 model·effort와 session 기록 관측은 10회 모두 같았다. 기준선·범위·bootstrap은 claude-sonnet-5/high, 수용 근거는 claude-opus-5/high였다.
- **원장의 Worker 관측값**(FlowMarshal `ea30491`·`9413246` 반영 뒤): 두 Worker usage 모두 `observed_model=claude-sonnet-5`, `observed_effort=high`, `binding_provenance.observed=claude_session_transcript`로 기록됐다. result 이벤트 시점에 session 기록이 이미 써져 있었다는 뜻이다(두 번 관측).
- token 수는 provider가 준 관측값이며 요금이나 구독 한도 차감으로 환산하지 않는다.

## 두 방식 비교

| 항목 | 결정적 범위 gate(방법론만 채택) | 플러그인 연동 gate |
|---|---|---|
| 막는 것 | Task 쓰기 target 밖 파일의 추가·수정·삭제(Task마다 내용 digest 비교) | 선언 밖 변경(`unplanned`), 제외 경로, 기존 변경과 겹침(`preexisting-overlap`), 수용 근거 부족, stage 하한 미달 관측 |
| 모델 호출 | 없음 | Task마다 steward 최대 4회. 실측은 Task당 약 19~20초, 입력 약 76k tokens로 Worker보다 컸다 |
| 같은 파일을 여러 Task가 차례로 고치는 Goal | 통과 | 뒤 Task가 막힌다. FlowMarshal은 Task마다 커밋하지 않고, 플러그인은 작업 트리를 기준선으로 잡는다 |
| 외부 의존 | 없음 | node, 플러그인 worktree와 MCP 서버, git 저장소. 플러그인이 갱신될 때마다 commit·dist sha256을 다시 고정해야 한다 |
| 판정 | Core가 결정적으로 판정한다 | Core와 플러그인 판정의 AND. steward 판단이 들어가 걸리는 지점이 달라질 수 있다 |
| 코드 | `scope_gate.py` 64줄 | `bridge.py` 476줄과 플러그인 쪽 host adapter·서명 CLI |
| 공통 한계 | 기준선이 메모리에만 있다. 원장 evidence로 옮기면 재시작 뒤에도 이어 갈 수 있다 | gate 상태가 메모리에만 있다 |

## 발견한 제약과 권고

1. **순서 대응은 hook 두 곳으로 충분하다.** 플러그인 stage 순서(기준선→구현→범위→근거)가 FlowMarshal의 dispatch 전·완료 전 두 지점에 자연스럽게 맞는다. 판정 권위는 Core에 남기고 플러그인은 추가 차단 조건(AND)으로 쓸 수 있다.
2. **실제로 메우는 빈틈은 선언 밖 파일이다.** FlowMarshal은 Execution Spec에 선언한 읽기 target이 바뀌면 digest로 이미 잡는다(가짜 검사에서 `RECOVERED`로 관측). 선언하지 않은 파일의 추가·수정은 완료 뒤 재관측이 흡수한다. 플러그인 범위 확인이 이것을 막는다.
3. **결정적 stage에도 모델 관측이 필요하다.** 플러그인은 한국어 산문 최종화 외 모든 stage를 semantic으로 보고, 기준선·범위 확인처럼 스크립트가 판정하는 stage에도 모델 관측(general 이상)을 요구한다. Engine 호스트에서는 그 관측을 만들기 위해 steward 모델 호출을 추가해야 했고, 이것이 governance 비용의 대부분이다.
   - **권고:** 플러그인 쪽에서 호스트가 "스크립트 실행 결과를 그대로 기록했다"는 결정적 실행을 증명하는 경로를 두거나, 해당 capability를 deterministic으로 분류할지 검토한다.
4. **effort는 관측할 수 있었다.** Claude CLI session transcript의 assistant 줄에 `effort`가 있어, 새 "관측 불가" 값 없이 플러그인 훅과 같은 원천으로 증명했다. 사용자 결정("출처 붙여 기록")에 따라 FlowMarshal 원장도 이 값을 `claude_session_transcript` 출처로 기록한다(`ea30491`, `9413246`). 실제 여러 Task 실행에서 두 Worker 모두 기록됐다. 기록값이 CLI에 요청한 값을 적은 것인지 실제 적용값인지는 여전히 구분하지 못하고, 출처 표식으로 그 한계를 드러낸다.
5. **플러그인 v1.20.2의 digest 형식이 stage마다 달랐다.** 범위 stage는 16진수만, 수용 근거 stage는 `sha256:` 접두사만 받아 브리지가 stage별로 맞춘다. 플러그인 v1.20.3에서 두 형식을 모두 받도록 고쳤다고 플러그인 작업 세션이 알려 왔다. 비통과 stage는 top-level `error`와 provider 결과 `error`가 같아야 한다.
6. **실행 조건:** 범위 확인은 git 저장소를 요구하고, validation이 만드는 `__pycache__` 같은 산출물은 `.gitignore`로 빼야 `unplanned`가 되지 않는다.
7. **프로토타입 한계(의도적 생략)**
   - 고위험 Task의 독립 감사 stage는 지원하지 않고 `UNSUPPORTED_PLAN`으로 막는다. 필요하면 별도 fresh steward thread로 붙인다.
   - Task 수용 기준(자유 문장)은 Task validation 전체 PASS에 대응시켰다.
   - gate 상태는 메모리에만 있어 Engine 재시작 뒤 이어 가지 못한다. 한 번 막힌 Task는 같은 gate 인스턴스에서 다시 열지 않는다.
   - gate 안의 예외는 `GATE_ERROR` 차단 사유로 바뀐다. dispatch 전 차단은 FlowMarshal History가 아니라 gate 로그(`governance-log.jsonl`)에만 남는다.
   - gate는 `EngineDispatcher.run_once` 경로에만 걸린다. CLI의 수동 Attempt 예약·Task 완료 명령(`cli.py`)은 gate를 거치지 않는다.
   - MCP 응답 대기에 timeout이 없다.
   - Claude transcript 조회는 제품 runtime의 private 메서드를 재사용한다.
8. **버전 고정:** 플러그인은 개발이 계속 진행 중이다(측정 중에도 v1.20.3 준비가 진행됐다). commit과 dist sha256을 함께 고정해야 한다. 플러그인을 갱신할 때마다 이 검사를 다시 돌려야 한다.
9. **여러 Task가 같은 파일을 차례로 고치면 플러그인 연동이 막힌다.** 플러그인 범위 확인은 Task 시작 때 작업 트리를 기준선으로 잡는다. 그래서 앞 Task가 남긴 미커밋 변경을 사용자 기존 변경과 구분하지 못한다. 결정적 검사에서는 범위 확인이, 실제 실행에서는 기준선 stage의 steward가 막았다.
   - **권고:** 플러그인 쪽에 "앞선 governed run이 소유한 변경"을 기준선에 넘기는 방법이 필요하다. 예를 들면 이전 run의 범위 보고서를 ownership 근거로 받는 방식이다. 결정적 gate는 Task마다 digest를 새로 잡으므로 이 문제가 없다.
10. **bare `EngineDispatcher`와 서로 독립인 Task.** dispatcher는 ready Task를 모두 먼저 materialize한다. 그래서 앞 Task가 완료돼 Project Map revision이 올라가면, 다음 Task의 dispatch가 `reserve_attempt`의 `STALE_EXECUTION_INPUT` 예외로 멈췄다.
    - 이 spike 경로에서만 관측했고, EngineApplication·supervisor 경로가 다시 materialize하는지는 확인하지 않았다.
    - 합성 Goal은 control 의존성으로 직렬화해 피했다.
