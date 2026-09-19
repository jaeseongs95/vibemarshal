# FlowMarshal ↔ agent-governance-suite 연동 프로토타입

FlowMarshal Engine이 Task마다 agent-governance-suite의 orchestrated workflow를 진행하는 방식이 실제로 동작하는지 확인하는 spike다. 제품 wheel에는 들어가지 않는다. 결과를 보고 연동 형태(선택형 연동, 방법론만 채택, 필수 연동)를 정한다.

## 구성

| 위치 | 역할 |
|---|---|
| `src/flowmarshal/engine/runtime.py` | `EngineDispatcher(task_gate=...)` 선택 인자. gate가 차단 사유를 돌려주면 Worker dispatch 또는 Task 완료를 막는다. 지정하지 않으면 기존 동작과 같다. |
| `bridge.py` | `GovernanceTaskGate`. 플러그인 MCP 서버를 `flowmarshal-engine` 호스트로 띄우고 Task별 workflow를 진행한다. |
| `test_bridge.py` | 가짜 provider 결정적 검사(모델 호출 없음). |
| `run_real.py` | 실제 Claude Code provider로 합성 fixture 한 Task를 끝까지 진행하는 1회 측정 스크립트. |

플러그인 쪽은 agent-governance-suite 브랜치 `claude/flowmarshal-engine-host`(커밋 `98db130`)의 `flowmarshal-engine` host attestation과 서명 CLI(`mcp-server/dist/engine-attestation.mjs`)를 쓴다. `bridge.DEFAULT_PIN`이 commit과 dist 서버·서명 CLI의 sha256을 고정하며, 하나라도 다르면 gate를 만들지 않는다.

## 실행

저장소 루트에서 실행한다. 플러그인 worktree 경로는 `AGS_PLUGIN_ROOT`로 바꿀 수 있다(기본 `D:/claude/mcp변경/ags-engine-host`).

```bash
# 결정적 검사(모델 호출 없음)
FLOWMARSHAL_ENGINE_SOURCE_ROOT=. .venv/Scripts/python.exe -m unittest discover -s spikes/governance_bridge -p "test_*.py"

# 실제 1회 측정(Claude Code CLI 호출: steward 4회 + Worker 1회)
FLOWMARSHAL_ENGINE_SOURCE_ROOT=. .venv/Scripts/python.exe spikes/governance_bridge/run_real.py <빈 출력 디렉터리>
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

### 가짜 provider 결정적 검사 (`test_bridge.py`, 7개 통과)

| 사례 | 결과 |
|---|---|
| 정상 Task | 플러그인 workflow가 plan→root→claim→start→stage 4개→finalize `passed`로 끝난 뒤에만 Core가 Task를 완료한다. plan·stage 호출 5회에 token이 붙고, 수용 근거 stage는 deep 하한에 맞춰 Opus급 관측으로 기록된다. |
| 선언 밖 파일 변경(`notes.md` 생성) | 범위 확인이 `NEEDS_APPROVAL`(`unplanned:notes.md`)이 되어 Task가 `blocked`로 남는다. |
| 같은 변경, gate 없음(대조군) | Task가 `completed`된다. 완료 뒤 State 재관측이 선언 밖 파일을 새 기준으로 흡수하는 현재 빈틈이다. |
| stage 하한 미달 관측(Haiku급 steward) | `plan_workflow`가 `BINDING_INVALID`를 반환한다. Worker Attempt를 예약하지 않고 `GOVERNANCE_GATE_BLOCKED`가 된다. |
| 기준선 stage 실패(steward 거절) | 첫 tick과 다음 tick 모두 `GOVERNANCE_GATE_BLOCKED`이고 Worker Attempt는 0건이다. 이미 시작한 플러그인 run은 abort한다. |
| Worker 관측 불가 | token을 만들지 않고 `WORKER_OBSERVATION_UNAVAILABLE`로 Task를 막는다. 기록된 stage는 기준선 하나뿐이다. |
| 플러그인 고정값 불일치 | gate를 만들지 않는다(`PLUGIN_PIN_MISMATCH`). |

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

## 발견한 제약과 권고

1. **순서 대응은 hook 두 곳으로 충분하다.** 플러그인 stage 순서(기준선→구현→범위→근거)가 FlowMarshal의 dispatch 전·완료 전 두 지점에 자연스럽게 맞는다. 판정 권위는 Core에 남기고 플러그인은 추가 차단 조건(AND)으로 쓸 수 있다.
2. **실제로 메우는 빈틈은 선언 밖 파일이다.** FlowMarshal은 Execution Spec에 선언한 읽기 target이 바뀌면 digest로 이미 잡는다(가짜 검사에서 `RECOVERED`로 관측). 선언하지 않은 파일의 추가·수정은 완료 뒤 재관측이 흡수한다. 플러그인 범위 확인이 이것을 막는다.
3. **결정적 stage에도 모델 관측이 필요하다.** 플러그인은 한국어 산문 최종화 외 모든 stage를 semantic으로 보고, 기준선·범위 확인처럼 스크립트가 판정하는 stage에도 모델 관측(general 이상)을 요구한다. Engine 호스트에서는 그 관측을 만들기 위해 steward 모델 호출을 추가해야 했고, 이것이 governance 비용의 대부분이다.
   - **권고:** 플러그인 쪽에서 호스트가 "스크립트 실행 결과를 그대로 기록했다"는 결정적 실행을 증명하는 경로를 두거나, 해당 capability를 deterministic으로 분류할지 검토한다.
4. **effort는 관측할 수 있었다.** Claude CLI session transcript의 assistant 줄에 `effort`가 있어, 새 "관측 불가" 값 없이 플러그인 훅과 같은 원천으로 증명했다. 단 FlowMarshal 원장의 `observed_effort`는 계속 null이다(provider 응답 echo 기준). transcript의 `effort`가 CLI에 요청한 값을 적은 것인지 실제 적용한 값인지는 확인하지 않았다. transcript를 `provider_observed`로 인정할지는 FlowMarshal 계약 결정이 필요하다.
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
