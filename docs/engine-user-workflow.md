# Engine 사용자 workflow

`flowmarshal-engine`은 raw request를 Goal로 정규화하고 독립 review와 Planning을 거쳐 실행한다. 사용자는 내부 Plan ID나 digest를 승인 입력으로 복사하지 않는다.

## 처음 시작하기

DB, artifact와 역할 설정은 source checkout 밖에서도 만들 수 있다. 아래 경로는 모두 절대 경로로 고정한다.

```powershell
$ProjectRoot = (Resolve-Path "C:\path\to\my-project").Path
$StateRoot = (New-Item -ItemType Directory -Force (Join-Path $ProjectRoot ".flowmarshal-engine")).FullName
$Database = Join-Path $StateRoot "flowmarshal-engine.sqlite3"
$Artifacts = Join-Path $StateRoot "artifacts"
$RoleConfig = Join-Path $StateRoot "roles.json"

flowmarshal-engine config init `
  --output $RoleConfig `
  --model "<model-id>" `
  --effort high
```

`config init`은 model/effort를 자동 선택하지 않는다. 사용자가 지정한 값을 현재 Codex `model/list`에 대조한 뒤 일곱 역할의 JSON을 만들며, 지원되지 않는 binding은 거부한다. 역할별 override는 `--role "validator=<model-id>:<effort>"`처럼 지정한다. 기존 파일은 덮어쓰지 않는다.

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts project init `
  --name "my-project" `
  --root $ProjectRoot

flowmarshal-engine --db $Database --artifacts $Artifacts prepare `
  --project-id <project-id> `
  --request "두 모듈을 수정하고 회귀 테스트까지 실행해 주세요." `
  --role-config $RoleConfig

flowmarshal-engine --db $Database --artifacts $Artifacts authorize `
  --project-id <project-id> `
  --source cli-user
```

요구를 바꾸거나 늘릴 때는 `revise`로 같은 Goal의 다음 revision을 준비한다. `revise`는 실제 Goal 역할과 Planning을 수행하지만 Plan을 활성화하지 않는다. 새 revision의 Plan은 기존 승인으로 실행되지 않으므로 `authorize`로 새 승인 target을 다시 확인해야 실행된다.

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts revise `
  --project-id <project-id> `
  --request "수정한 요구를 적어 주세요." `
  --role-config $RoleConfig
```

관측 token 정책은 선택 사항이다. 설정하지 않아도 승인된 provider 호출 수와 절대 deadline은 적용된다. `project budget set`은 사용자가 별도로 선택한 best-effort token 중단 정책에만 사용한다.

## 수동 실행

`run-once`는 Core 상태 전이 또는 RuntimeJob 예약·관측 한 번만 수행하고 반환한다. GoalVerdict가 나올 때까지 같은 절대 DB·artifact·역할 설정 경로로 반복한다.

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts status `
  --project-id <project-id>

flowmarshal-engine --db $Database --artifacts $Artifacts run-once `
  --project-id <project-id> `
  --role-config $RoleConfig

flowmarshal-engine --db $Database --artifacts $Artifacts observe `
  --project-id <project-id>
```

`status`의 `current_stage`, 한 줄 `reason`, `next_action`을 먼저 읽는다. `drill_down`은 Task·파일·검사·evidence의 원장 기록 보존 여부와 현재 입력에 대한 `validity`를 나눠 보여 준다. `record_status=preserved`이면서 `validity=invalidated`일 수 있으며, 이는 과거 근거를 삭제하지 않았지만 현재 Plan 입력으로 재사용할 수 없다는 뜻이다. `active_runtime_job`이 있어도 다음 tick은 `run-once` 한 번이다. `run-once`는 활성 job 관측도 수행한다. `status`·`observe`는 사람이 진단할 때 쓴다. provider thread가 결속된 활성 job에서 `status`의 `next_action`이 `observe`를 안내해도 사람용 안내이며, 다음 명령을 고르는 규칙이 아니다. `observe`는 provider 상태만 관측하며 Task나 Goal을 직접 완료하지 않는다. 각 명령이 0이 아닌 exit code를 반환하면 다음 tick을 자동 실행하지 말고 출력된 `error_code`, `status`와 원장 상태를 확인한다.

```json
{
  "current_stage": "task_ready",
  "reason": "dependency를 만족한 Task 하나가 실행 준비 상태입니다.",
  "next_action": "run-once로 Task 실행 명세 준비를 시작하십시오.",
  "authorization_state": "authorized",
  "drill_down": {
    "tasks": [],
    "files": [],
    "checks": [],
    "evidence": []
  },
  "execution_summary": {
    "model_observations": [
      {
        "requested_model": "<requested-model>",
        "requested_effort": "high",
        "observed_model": null,
        "observed_effort": null,
        "observed_source": null
      }
    ],
    "usage_status": "missing",
    "usage_missing_components": ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"],
    "external_effect_status": "none",
    "external_effect_unknown": false
  }
}
```

`requested_*`는 client 요청값이고 `observed_*`는 provider 원문 응답이 turn별 값을 명시한 경우에만 채워진다. `model/list` 지원 확인이나 요청 echo를 observed 값으로 복사하지 않는다. `usage_status=missing`은 token을 0이나 예약량으로 보충하지 않으며, `external_effect_status=unknown`은 기존 intent·receipt·대상 관측을 먼저 대조하고 자동 재실행하지 않는다는 뜻이다.

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts pause `
  --project-id <project-id> `
  --reason "사용자 검토"

flowmarshal-engine --db $Database --artifacts $Artifacts run-once `
  --project-id <project-id> `
  --role-config $RoleConfig `
  --resume

flowmarshal-engine --db $Database --artifacts $Artifacts cancel `
  --project-id <project-id> `
  --reason "요청 철회"
```

`pause`는 현재 Goal revision에 제어 상태를 남기고 active job에 bounded interrupt를 요청한다. 명시적인 `run-once --resume` 전에는 후속 tick을 막는다. `cancel`은 Goal 흐름을 재개할 수 없게 하지만 interrupt 응답을 provider terminal이나 Task 완료로 바꾸지 않는다.

## 외부 scheduler에서 실행하기

Engine에 전역 daemon은 없다. Windows Task Scheduler나 CI 같은 외부 scheduler는 DB·artifact·역할 설정·프로젝트 경로를 절대 경로로 고정하고, 한 번에 인스턴스 하나만 실행해야 한다. 1.0 RuntimeJob 실행은 Windows 전용이다(아래 「실행 환경과 업그레이드」). 외부 scheduler는 직렬로 매 tick `run-once`를 한 번만 호출한다. `run-once`는 활성 job 관측도 수행한다. `status`·`observe`는 사람이 진단할 때 쓴다. lock 파일이 없는 활성 행(`RUNTIME_OWNER_LOCK_UNAVAILABLE`, `owner proof missing`)에서 run-once는 원장을 바꾸지 않는다. 이 행의 기존 성공 결과 재부착과 deadline hard stop은 observe에서만 일어난다. scheduler는 `status`의 `next_action` 문자열을 해석해 다음 명령을 고르지 않는다. 명령이 실패하면 그 tick을 종료하고 새 `run-once`로 즉시 우회하지 않는다.

`run-once`가 반환됐다는 사실은 provider job 완료를 뜻하지 않는다. 다음 schedule도 `run-once` 한 번으로 시작하며, scheduler나 PC가 재시작돼도 아래 observe-first 복구 순서를 따른다. provider thread가 결속되기 전에 owner가 사라진 job은 다음 `run-once`가 owner lock으로 확인한 뒤 기존 복구 경로로 넘기거나, 같은 job을 한 번 재시작하거나, typed blocker로 멈춘다. 재시작은 job의 절대 deadline을 늘리지 않는다. 그래서 job deadline이 지난 뒤 발견한 소실은 이어받지 못하고 `RUNTIME_JOB_OWNER_LOST`(`effect_state=none_proven; reason=job_deadline_elapsed`)로 멈춘다. 이 정지를 공개 명령으로 풀어 가는 경로는 아직 없다(1.0 미해결). 사용자의 Codex 예약 상태나 특정 Windows Task Scheduler wrapper는 제품 Gate가 아니다. 개발 조율 도구 fm-tick(FM-09)은 선택형 도구이며, 이 단일 `run-once` 규칙과의 정합은 확인하지 않았다(범위 밖 미검증).

## 실행 환경과 업그레이드

### 실행 OS와 경로

- 1.0 RuntimeJob 실행은 Windows 전용이다. Linux·macOS·WSL의 POSIX 경로는 fail-closed이고, Linux 지원은 1.0 이후다.
  - 활성 Plan이 있는 프로젝트에서 POSIX의 `run-once`는 원장을 쓰거나 job 행을 만들기 전에 `action=blocked`, `blocker_code=RUNTIME_OWNER_LOCK_UNAVAILABLE`, `detail=RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix`를 게시하고 exit code 0으로 끝난다. owner lock 획득도 같은 이유로 거절한다.
  - `status`도 같은 blocker를 `recovery.next_action`에 보인다. `detail`은 `RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix 1.0 RuntimeJob 실행은 Windows 전용입니다(Linux·macOS·WSL 지원은 1.0 이후).`이다. workflow가 일시정지·취소 상태가 아니면 최상위 `next_action`도 같은 문구다.
  - `prepare`·`authorize`·`status`·`replan`·`pause`·`cancel`은 POSIX에서도 동작한다.
- 같은 원장(DB·artifact)은 한 Windows 환경에서 같은 절대 경로로만 연다. 같은 원장을 WSL과 Windows에서 섞어 열지 않는다.
  - owner lock 파일 경로는 `project init` 때 원장에 기록된 프로젝트 artifact root(`<artifacts>/<project-id>`)에서 계산한다. 그래서 lock 파일 위치는 명령의 `--artifacts` 표기가 아니라 원장 기록으로 정해진다.
  - 네트워크 드라이브 매핑이나 hard link처럼 같은 경로 문자열이 다른 파일을 가리키게 하지 않는다. 그러면 두 process가 서로의 lock을 보지 못할 수 있다.
- owner lock 파일은 `<artifacts>/<project-id>/runtime-owners/`에 job마다 하나씩 생긴다.
  - 새 job 행을 예약하는 `run-once` owner만 만든다. `status`·`observe`는 만들지 않는다.
  - 크기는 0~1바이트이고 자동으로 지우지 않으므로 job 수만큼 쌓인다.
  - 이 파일이 owner proof다. 활성 RuntimeJob이 있을 때 `runtime-owners/`를 지우지 않는다. 지우면 provider thread가 결속되지 않은 그 활성 job은 `owner proof missing: lock file absent`로 멈춘다.

### 업그레이드(cold upgrade)

Engine 업그레이드는 활성 RuntimeJob이 없을 때만 한다. 업그레이드 전에 다음을 확인한다.

1. 외부 scheduler를 멈추고, 옛 버전의 `run-once`(초기 결과를 게시한 뒤 job을 계속 따라가는 background owner process 포함)와 `observe` process가 모두 끝났는지 확인한다.
2. `status`의 `active_runtime_job`이 `null`인지 확인한다.
3. `status`의 `attempts`에 있는 Attempt를 `attempt show --attempt-id <attempt-id>`로 열어, intent의 `status`에 `prepared`가 없는지 확인한다.
4. 미완료 CoreOperation이 없는지 확인한다. `status`의 `current_stage`가 `external_effect_unknown`이면 완료가 확인되지 않은 효과(이미 발견된 미완료 CoreOperation 포함)가 있으므로 업그레이드하지 않는다. 다만 아직 `run-once`가 발견하지 않은 미완료 CoreOperation만 따로 보여 주는 읽기 전용 명령은 현재 공개 CLI에 없다.

활성 RuntimeJob이 있으면 업그레이드하지 않고, `runtime-owners/`도 지우지 않는다. 이 규칙을 어기면 새 버전은 lock 파일이 없는 binding 없는 활성 행을 FREE·crash·고아로 해석하지 않는다. crash 판정·재시작·라우팅 없이 `RUNTIME_OWNER_LOCK_UNAVAILABLE`(`owner proof missing: lock file absent`)로 멈춘다(아래 blocker 표).

## 자동 복구 읽기

Task 실행이 실패하면 Core는 원장에 남은 직접 근거로만 원인을 분류하고, 승인 경계 안에서 두 가지 최소 복구만 자동으로 수행한다. 자동 복구는 `run-once`의 한 tick 안에서 일어나며 별도 명령이 필요하지 않다. 재계획 후보가 정해진 차단으로 멈춘 경우에만 사용자가 `replan`으로 새 재계획 시도를 요청할 수 있다(아래 「차단된 재계획 후보 다시 시도」).

| 자동 경로 | 시작 근거 | Core가 하는 일 |
|---|---|---|
| bounded repair | provider/local이 명시한 구현 실패 code 또는 직접 test·diff·file 실패 evidence | 같은 Task·같은 Execution Spec으로 새 Attempt 하나를 허용하고 Task validation을 다시 실행 |
| Context 복구 | `CONTEXT_REQUIRED` 계열 명시 code | 같은 Task 의미로 새 Execution Spec 준비를 활성화 |
| subgraph replan | `TASK_CONTRACT_INVALID`·`DEPENDENCY_*` 계열 명시 code | 실패 Task와 그 후행 Task만 교체한 새 Plan revision을 만들고 복구 전용 독립 Reviewer 검토와 결정적 Gate를 통과한 경우에만 자동 활성화 |

자동 복구는 `run-once`에 역할 설정(`--role-config`)이 있을 때만 연결된다. 설정이 없으면 subgraph replan은 새 재계획 job을 예약해야 하는 시점에 `REPLAN_PROVIDER_REQUIRED`로 명시적으로 멈춘다. 이미 예약된 재계획 job이 있으면 설정 없는 `run-once`도 새 provider 호출이나 새 job 없이 그 job을 관측하고 결과를 소비한다. 다만 결과 없이 owner가 사라진 재계획 job을 다시 시작해야 하면 설정 없는 `run-once`는 원장을 바꾸지 않고 `REPLAN_PROVIDER_REQUIRED`로 멈춘다.

### 차단된 재계획 후보 다시 시도

자동 subgraph replan의 후보가 다음 두 가지로 멈추면 `replan`으로 같은 실패에 대한 새 재계획 시도를 요청한다.
- `REPLAN_CANDIDATE_NOT_ADMISSIBLE`이고 decision이 `needs_revision`인 경우
- 후보가 결속한 StateSnapshot이 더는 current가 아닌 경우(STALE)

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts replan `
  --project-id <project-id> `
  --rationale "<재시도 이유>"
```

- `replan`은 Core 기록만 한다. provider·역할 호출이나 RuntimeJob 생성은 없다. 재시도 `RecoveryAssessment`와 History `recovery.replan_retry_requested`를 한 transaction에 남긴다.
- 역할 호출은 다음 `run-once`가 `replanning:<assessment-id>` RuntimeJob으로 한다. 그 `run-once`에도 `--role-config`가 필요하다.
- 새 근거는 차단 후보에 결속된 typed basis다. EvidenceRecord를 새로 만들지 않는다.
  - `needs_revision`: 그 decision의 finding 원문과 evidence_refs를 수정 역할 입력에 결속한다. 수정 역할이 수정안 없이 답하면 다른 역할로 다시 호출하지 않고 같은 차단을 보여 주며, 그 재시도는 소비된 것으로 남는다.
  - STALE: 후보가 결속한 StateSnapshot 뒤에 기록된 current 관측이 근거다. StateSnapshot은 Project Map revision을 함께 결속하고, 파일이 바뀌면 Map과 State가 함께 새로 기록된다. 그래서 State와 함께 current가 아니게 된 Map도 같은 STALE로 본다. 새 후보는 current State·Project Map에서 다시 상세화한다. State는 current인데 Map만 current가 아닌 후보는 STALE 재시도 대상이 아니어서 `REPLAN_RETRY_ACTIVATION_BLOCKED`로 거절한다. 이 상태는 재관측이 Map을 기록한 뒤 State를 기록하기 전에 멈춘 경우에만 생기고, 다음 재관측이 새 State를 기록하면 STALE 재시도 대상이 된다. 현재 공개 CLI에는 재관측만 수행하는 명령이 없다.
  - 두 조건이 함께 참이면(`needs_revision`이면서 STALE) STALE로 기록하고, finding은 역할 입력에 넣지 않는다.
  - `--rationale`은 감사 기록일 뿐 근거나 한도 검사에 쓰이지 않는다.
- `run-once`가 `GOAL_AUTHORIZATION_REQUIRED`를 먼저 보고한 후보도 적격일 수 있다. 조건은 두 가지다.
  - 후보의 StateSnapshot이 stale이다.
  - 승인과 State·Project Map 밖의 활성화 조건을 모두 통과한다. 그 조건은 active Goal이 current일 것, 진행 중인 Plan 교체가 없을 것, supersedes 계보가 active Plan에 닿을 것이다.
  - 이 경우 STALE 재시도로 기록한다.
- 한도는 원장에서 센다. 같은 실패 재계획 최대 2회에는 자동 재계획과 STALE 재시도도 들어간다. 기본 정책에서는 자동 1회 뒤 수동 1회다. 넘으면 `SAME_FAILURE_REPLAN_LIMIT`나 `GOAL_REPLAN_LIMIT`로 거절한다.
  - 수동 재시도로 만든 후보가 다시 STALE이나 다른 차단으로 멈추면, 기본 정책에서는 그 실패의 공개 재시도가 이미 끝났을 수 있다.
  - 문자열로만 허용된 외부 효과 후보는 STALE 재시도를 한 번 쓴 뒤에도 같은 승인 필요(boundary=effect)로 돌아올 수 있다.
- 같은 차단 후보로 다시 부르면 같은 assessment ID를 돌려주고 원장을 바꾸지 않는다(`recorded=false`).
- 새 후보는 active Plan의 Task 집합과 DAG를 유지하고, 같은 plan_id의 다음 revision으로 등록된다. 활성화 조건은 자동 재계획과 같다. 명령 뒤에 State가 다시 바뀌면 새 후보도 활성화 전에 `PLAN_STATE_SNAPSHOT_STALE`로 멈춘다.
- 다음 경우는 typed 오류로 거절하고 원장을 바꾸지 않는다. `NOT_REMEDIABLE`·`ACTIVATION_BLOCKED`·`BINDING_MISMATCH`·`OBSERVE_FIRST`는 StateSnapshot이 stale이어도 거절한다.

| 거절 code | 조건 |
|---|---|
| `REPLAN_RETRY_NOT_BLOCKED` | 사용자 재계획을 기다리는 차단 후보가 없음(원장에 등록되지 않은 후보 포함) |
| `REPLAN_RETRY_NOT_REMEDIABLE` | decision이 `blocked`·`rejected` |
| `REPLAN_RETRY_ACTIVATION_BLOCKED` | 그 밖의 활성화 조건 차단, 또는 승인 밖 조건을 통과하지 못한 승인 필요 후보. 후자가 stale이면 `run-once`는 그 후보를 활성화하지 않으므로, 진행 중인 Plan 교체처럼 끝나면 풀리는 조건은 해소된 뒤 `replan`을 다시 실행한다 |
| `REPLAN_RETRY_BINDING_MISMATCH` | 결속 불일치 |
| `REPLAN_RETRY_OBSERVE_FIRST` | 재계획 job 결과 불명 |
| `REPLAN_RETRY_REAUTHORIZATION_REQUIRED` | StateSnapshot이 current인 승인 필요 후보 |
| `REPLAN_RETRY_RATIONALE_REQUIRED` | 빈 rationale |
| `NEW_RECOVERY_EVIDENCE_REQUIRED` | 차단 후보의 typed basis가 직전 recovery 기록보다 앞선다(새 근거 없음) |
| `SAME_FAILURE_REPLAN_LIMIT` / `GOAL_REPLAN_LIMIT` | 원장에서 센 재계획 한도 초과 |
| `WORKFLOW_CANCELLED` | 취소된 workflow |
| `CORE_CAPABILITY_DENIED` | 역할 실행 scope 안에서 호출 |

- 어느 거절 안내도 cancel을 해결책으로 제시하지 않는다.

### 보존되는 것과 대체되는 것

복구는 기존 기록을 지우지 않는다.

- 보존: 원 Attempt와 그 failure_class, provider receipt와 intent, 실패 관측 evidence, 기록된 `RecoveryAssessment`. 새 Attempt는 다른 `attempt_no`와 다른 provider thread를 받는다.
- 대체: subgraph replan에서만 직전 Plan revision이 `superseded`가 되고, 그 revision의 미완료 Task가 `superseded`로 남는다. 완료된 Task의 검증된 로컬 근거는 같은 `task_ref` 기준으로 새 revision에 재사용되며 원본 Attempt·receipt는 이동하지 않는다.
- 실패 Task 밖의 Task 계약·Goal coverage·독립 Goal Test는 직전 revision의 의미를 그대로 유지한다.

### status 읽는 법

`status`의 `recovery`는 분류 근거, 보존·폐기 범위, 필요한 다음 동작을 나눠 보여 준다.

```json
{
  "current_stage": "recovery_required",
  "recovery": {
    "state": "automatic_pending",
    "classification": {
      "task_id": "<task-id>",
      "attempt_id": "<attempt-id>",
      "failure_class": "implementation",
      "basis": "direct_evidence",
      "transient": false,
      "evidence_ids": ["<evidence-id>"],
      "codes": [
        {
          "values": ["IMPLEMENTATION_ERROR"],
          "provenance": "provider_observed",
          "authoritative": true,
          "note": "provider payload가 직접 반환한 error code입니다."
        },
        {
          "values": ["TASK_CONTRACT_INVALID"],
          "provenance": "model_reported",
          "authoritative": false,
          "note": "모델 자기보고 code는 진단 가설이며 자동 복구를 시작하는 근거가 아닙니다."
        }
      ]
    },
    "limits": {
      "task_recovery_count": 0,
      "max_task_recovery": 2,
      "same_failure_replan_count": 0,
      "max_same_failure_replans": 2,
      "goal_replan_count": 0,
      "max_goal_replans": 5,
      "requires_new_evidence": true,
      "has_new_evidence": true,
      "limit_code": null
    },
    "scope": {
      "preserved_attempt_ids": ["<attempt-id>"],
      "preserved_evidence_ids": ["<evidence-id>"],
      "preserved_assessment_ids": [],
      "superseded_plan_revision_ids": [],
      "superseded_task_ids": [],
      "active_plan_revision_id": "<plan-revision-id>"
    },
    "next_action": {
      "mode": "automatic",
      "blocker_code": null,
      "suggested_repair_action": "task_repair",
      "checkpoint_required": false,
      "detail": "run-once가 원장 한도 안에서 task_repair 자동 복구를 이어서 수행합니다. ..."
    }
  }
}
```

- `state`가 `automatic_pending`이면 다음 `run-once`가 복구를 이어서 수행한다. 사용자가 할 일은 없다.
- `state`가 `recovered`면 미해결 실패가 없고 `scope`로 무엇이 보존됐고 무엇이 superseded인지 확인할 수 있다.
- `state`가 `observe_first_required`면 효과가 확정되지 않았다는 뜻이다. `recover inspect`로 기존 intent·receipt를 먼저 대조하고 자동 재실행하지 않는다.
  - 소비된 재계획 job 결과에 `job_error`가 있어 typed 결과를 확정하지 못한 경우도 여기에 해당한다. 이때 `next_action.blocker_code`는 `run-once`와 같은 `EXTERNAL_EFFECT_UNKNOWN`이고, `detail`에 job ID·오류 유형·terminal 관측 여부가 들어 있다.
  - 반복 `run-once`와 `status`는 원장을 바꾸지 않는다. `replan`은 `REPLAN_RETRY_OBSERVE_FIRST`로 거절한다.
  - 현재 공개 CLI에는 이 차단 상태를 같은 project 안에서 해소하는 recovery 명령이 없다. 원장을 직접 고치지 않는다.
  - provider thread가 결속되지 않은 활성 RuntimeJob에 효과가 시작됐을 수 있다는 근거(역할 요청 기록, intent, effect marker, provider 호출 기록, 미완료 CoreOperation)가 있어도 여기에 해당한다. 이때 `run-once`와 `status`는 같은 `EXTERNAL_EFFECT_UNKNOWN`을 보이고, `detail`에 job ID와 근거 종류(`evidence=…`), 마지막 역할 event가 들어 있다. `run-once`는 새 provider 호출을 만들지 않고 원장을 바꾸지 않는다. 현재 공개 CLI에는 이 차단 상태를 같은 project 안에서 해소하는 recovery 명령이 없다. 원장을 직접 고치지 않는다.
  - binding 없는 활성 RuntimeJob의 `RUNTIME_OWNER_LOCK_UNAVAILABLE`(owner proof 없음)과 `RUNTIME_JOB_OWNER_LOST`(`effect_state=unknown`)도 이 상태로 나온다. 아래 blocker 표의 해당 행을 따른다.
- `state`가 `user_decision_required`면 `next_action.blocker_code`가 필요한 판단을 가리킨다. 아래 표의 `RUNTIME_OWNER_LOCK_UNAVAILABLE`·`RUNTIME_JOB_OWNER_LOST` 행은 행 안에 적은 경우에 `observe_first_required`로 나온다.

| `blocker_code` | 뜻과 다음 동작 |
|---|---|
| `RECOVERY_DIAGNOSIS_REQUIRED` | 직접 근거로 원인을 분류하지 못했다. provider code·직접 evidence를 확인하고 원인을 결정한 뒤 진행한다 |
| `ENVIRONMENT_RECOVERY_REQUIRED` | 다시 시도로 풀리지 않는 환경 실패다. 환경 복구를 직접 관측한 뒤 진행한다 |
| `AUTHORIZATION_EXPANSION_REQUIRED` | 목표·범위·효과·정책 확장이 필요하다. 새 Goal revision과 승인이 필요하다 |
| `SAME_FAILURE_RECOVERY_LIMIT` / `SAME_FAILURE_REPLAN_LIMIT` / `GOAL_REPLAN_LIMIT` | 원장에서 센 복구 한도에 도달했다. `limits`의 실제 횟수와 상한을 확인한다 |
| `NEW_RECOVERY_EVIDENCE_REQUIRED` | 첫 복구 이후 새 근거 없이 같은 복구를 반복할 수 없다 |
| `TASK_VALIDATION_RECOVERY_REQUIRED` | 성공한 Worker 뒤 Task validation이 FAIL이다. 실패 결과와 직접 evidence로 원인을 분류한 `RecoveryAssessment`를 명시해 재시도한다 |
| `REPLAN_PROVIDER_REQUIRED` | 재계획 job을 새로 예약해야 하는데 역할 설정이 없어 재계획 후보와 독립 검토를 만들 수 없다. `run-once`에 `--role-config`를 지정한다. `status`는 `run-once`와 같은 판정을 쓴다. CLI `status`에는 역할 설정이 없으므로, 기록된 재계획 assessment에 job이 아직 없으면 이 code를 보여 준다. 이는 설정 없는 `run-once`의 결과와 같고, 다음 `run-once`에 `--role-config`가 필요하다는 뜻이다. provider thread가 결속되기 전에 owner가 사라진 재계획 job(`collector_lost`, 또는 시작되지 않은 `scheduled`)을 다시 시작해야 하는데 역할 설정이 없을 때도 같은 code다. 이때 `run-once`는 원장을 바꾸지 않고, `--role-config`를 지정한 다음 `run-once`가 같은 job을 한 번 재시작한다 |
| `EXECUTION_SPEC_PROPOSAL_REQUIRED` | provider thread가 결속되기 전에 owner가 사라진 Execution Spec 준비 job(역할이 제안을 만드는 job, Context 후속 준비 포함)을 다시 시작해야 하는데 준비 역할 provider가 없다. `run-once`는 원장을 바꾸지 않는다. `run-once`에 `--role-config`를 지정하면 다음 `run-once`가 같은 job을 한 번 재시작한다. CLI `status`에는 역할 설정이 없으므로 이런 job에서는 이 code를 보여 준다. 이는 설정 없는 `run-once`의 결과와 같다 |
| `REPLAN_CANDIDATE_NOT_ADMISSIBLE` | Core가 재계획 후보를 비적격으로 판정했다. 후보는 draft로 남고, 같은 후보를 활성화하거나 자동으로 다시 계획하지 않으며 `user_decision_required`로 멈춘다. 다음 `run-once`는 원장을 바꾸지 않고 같은 결과를 다시 보여 준다. `detail`에서 decision과 finding_codes를 확인한다. decision이 `needs_revision`이면 `replan`으로 finding을 결속한 재계획을 한 번 요청할 수 있다. `blocked`·`rejected`이면 `replan`이 `REPLAN_RETRY_NOT_REMEDIABLE`로 거절한다. 이때 목표·범위·효과를 바꾸려면 새 Goal revision과 승인이 필요하지만, active Plan이 있는 동안의 Goal revision은 아직 지원하지 않는다. 따라서 `blocked`·`rejected` 후보에 대해서는 현재 공개 CLI에 이 차단 상태를 같은 project 안에서 해소하는 recovery 명령이 없다. 원장을 직접 고치지 않는다 |
| `REPLAN_CANDIDATE_BINDING_MISMATCH` | 재계획 job 결과와 원장에 등록된 후보가 다르다. 자동으로 덮어쓰지 않는다. 원장 후보와 job 결과를 대조해 원인을 확인한다 |
| `PLAN_STATE_SNAPSHOT_STALE` | 적격 후보가 결속한 StateSnapshot이 더는 current가 아니다. StateSnapshot은 새 관측마다 새 revision으로 기록되고 이전 snapshot이 다시 current가 되지 않으므로, 같은 후보를 활성화하거나 자동으로 다시 계획하지 않으며 `user_decision_required`로 멈춘다. `replan`으로 current State에서 재계획을 요청한다(같은 실패의 재계획 한도 안에서). 원장을 직접 고치지 않는다 |
| `GOAL_AUTHORIZATION_REQUIRED` | 적격 후보를 활성화하려면 새 승인이 필요하다. `detail`의 changes를 확인한다. 재계획 후보가 있으면 `authorize`는 active Plan이 아니라 그 후보의 plan_id·plan_revision_id·plan_revision_no·definition·activation digest를 target에 표시하고, 표시된 target_digest를 입력하면 같은 transaction에서 승인과 그 후보의 활성화를 함께 기록한다. 다음 경우는 승인을 기록하지 않고 거절한다: 좁은 정책(`GOAL_AUTHORIZATION_REQUIRED`와 changes), 문자열로만 허용된 외부 효과 후보(boundary=effect, 같은 Goal 재승인으로 풀리지 않고 typed effect 계약을 가진 새 Goal revision이 필요하지만 active Plan이 있는 동안에는 아직 지원하지 않음), StateSnapshot이 current가 아닌 후보(Project Map이 함께 바뀌었는지와 무관하게 `PLAN_STATE_SNAPSHOT_STALE`, 이 뒤에는 한도 안에서 `replan`을 쓴다), 취소된 workflow(`WORKFLOW_CANCELLED`), target 표시 뒤 후보가 바뀜(`CORE_CAPABILITY_DENIED`). 두 console이 같은 후보를 동시에 승인하면 한쪽만 기록되고 다른 쪽은 typed 오류로 거절된다. paused 상태에서는 후보를 활성화하지만 pause는 유지되므로 다음 `run-once`에는 `--resume`이 필요하다. 승인 없이 풀리지 않는 차단이 아니어도 재계획 후보가 있는 동안에는 console 재승인이 그 후보를 표시하며, 활성화할 수 없는 후보면 무기록 거절된다 |
| `REPLAN_CANDIDATE_ACTIVATION_BLOCKED` | 그 밖의 활성화 사전 조건이 맞지 않는다. 진행 중인 Plan 교체처럼 끝나면 풀리는 조건은 해소된 뒤 다음 `run-once`가 같은 후보의 활성화를 한 번 시도한다. supersedes 계보 불일치나 현재가 아닌 Project Map·Goal은 같은 후보로는 풀리지 않는다. 풀리기 전까지 `run-once`는 원장을 바꾸지 않고 같은 결과를 보여 준다. `replan`은 StateSnapshot이 stale이어도 `REPLAN_RETRY_ACTIVATION_BLOCKED`로 거절한다 |
| `RUNTIME_OWNER_LOCK_UNAVAILABLE` | provider thread가 결속되지 않은 활성 RuntimeJob의 owner를 OS lock으로 확인할 수 없다. 두 경우가 있다. (1) lock 계층 오류나 POSIX 플랫폼: `state`는 `user_decision_required`이고 `detail`은 `run-once`와 같은 오류 원문이다. POSIX이면 활성 job이 없어도 활성 Plan이 있는 프로젝트에서 `platform unsupported: posix`와 Windows 전용 안내를 보인다(「실행 OS와 경로」). 이 판정은 원장에 남지 않으므로 lock 파일 접근 오류가 풀리면 다음 `run-once`가 다시 판정한다. (2) lock 파일이 없는 활성 행: `state`는 `observe_first_required`이고 `detail`은 `owner proof missing: lock file absent`로 시작한다. 구버전 owner, supervisor 없는 예약, 다른 lock 경로를 쓴 process가 만든 행일 수 있어 FREE·crash·고아로 해석하지 않는다. `run-once`는 crash 판정·재시작·라우팅 없이 원장을 바꾸지 않고 같은 결과를 반복한다. 이 행의 저장된 성공 결과 재부착과 절대 deadline hard stop은 `observe`에서만 일어난다. 옛 owner·scheduler process가 끝났는지 확인한다(cold upgrade 규칙). 옛 owner가 job을 끝내지 않았고 `observe`가 재부착할 저장된 성공 결과도 없으면, 현재 공개 CLI에는 이 차단 상태를 같은 project 안에서 해소하는 recovery 명령이 없다. 원장을 직접 고치지 않는다 |
| `RUNTIME_JOB_OWNER_LOST` | provider thread가 결속되기 전에 owner가 사라졌고 같은 job을 자동으로 이어 갈 수 없다. code만으로 효과가 없었다는 뜻이 아니므로 `detail`의 `effect_state`를 확인한다. `effect_state=none_proven`이면 `state`는 `user_decision_required`다. 효과 근거는 없지만 `reason`이 가리키는 이유로 다시 시작하지 않는다: `restart_limit`(같은 job 재시작은 한 번뿐), `job_deadline_elapsed`(job 절대 deadline 경과, 재시작은 deadline을 늘리지 않음), `not_reconstructable`(`--proposal-file`로 제안을 직접 제출한 Execution Spec 준비 job이라 저장된 요청으로 다시 만들 수 없음). `effect_state=unknown`이면 `state`는 `observe_first_required`이고 `suggested_repair_action=wait_external`, `checkpoint_required=true`다. Goal Test 준비·Goal semantic 검사 job(`kind=goal_test_prepare`·`goal_semantic_validate`)이며 효과 여부를 확정할 수 없어 자동으로 다시 호출하지 않는다. 두 경우 모두 `run-once`는 원장을 바꾸지 않고 같은 결과를 반복한다. 현재 공개 CLI에는 이 차단 상태를 같은 project 안에서 해소하는 recovery 명령이 없다. 원장을 직접 고치지 않는다 |

`codes`의 `provenance`는 값의 출처를 구분한다. `provider_observed`와 `local_derived`만 `authoritative=true`이며 자동 복구를 시작할 수 있다. 모델 응답 본문이 `IMPLEMENTATION_ERROR: ...`처럼 스스로 코드를 보고해도 provider payload에 error code가 없으면 `model_reported`로만 남고 복구는 시작되지 않는다. Worker가 완료를 자칭해도 Task는 검사 결과로만 완료된다.

### 활성 RuntimeJob과 status

`status`는 provider thread가 결속되지 않은 활성 RuntimeJob을 `run-once`와 같은 분류 함수로 판정한다. 입력은 원장, 읽기 전용 lock 확인(lock 파일을 만들지 않고 원장에 쓰지 않음), 역할 설정 유무다. 그래서 `recovery.state`·`recovery.next_action`은 같은 원장 상태에서 `run-once`가 내는 blocker code·state와 같고, 활성 job 단계의 최상위 `next_action`도 같은 판정 문구를 쓴다. lock·플랫폼 오류, owner proof 없음, owner 응답 없음처럼 원장에 남지 않는 판정도 같다. `status` 호출은 원장을 바꾸지 않는다.

- owner가 lock을 쥐고 있으면 `recovery.state`는 `none`, `next_action.mode`는 `none`이고 blocker는 없다. `detail`은 `owner 실행 중: 다음 run-once·observe는 관측만 합니다.`이다. job 절대 deadline에 관측 유예 시간을 더한 시각이 지나도 다른 process가 lock을 쥐고 있으면 `detail`은 `owner가 deadline 뒤에도 lock을 쥐고 있음(응답 없음)`이다. 이때 `run-once`는 `observed`를 반환한다. 실패 기록이 함께 있으면 `classification`·`limits`는 원장 사실대로 채워진다.
- `state`가 `automatic_pending`이고 `detail`이 `run-once가 한 단계를 진행합니다(…)`이면, 다음 `run-once`가 괄호 안의 한 단계(owner 소실 기록, 저장된 성공 결과 재부착, 같은 job 한 번 재시작)를 수행한다.
- 과도기 한 단계: 기존 prepared 복구 경로나 thread 재개 경로로 넘기는 두 판정에서는 `status`가 `automatic_pending`(`…(기존 prepared 복구 경로로 넘김)`, `…(기존 thread 재개 경로로 넘김)`)을 보인다. 그러나 그 `run-once`는 기존 경로의 결과를 낸다(예: prepared 복구에서 `EXTERNAL_EFFECT_UNKNOWN`). 그 `run-once` 한 번 뒤에는 `status`와 `run-once`가 다시 같은 code·state를 보인다.
- `detail`의 `effect_state`가 `unknown`이면 `state`는 `observe_first_required`다.
- 그 밖의 판정은 기존 code를 쓴다. 효과 근거 없이 worker 자기 오류로 끝난 job은 그 오류 code(목록 밖이면 `RUNTIME_EFFECT_PREFLIGHT_FAILED`)로 멈추고 같은 오류를 자동으로 반복하지 않는다. 그 code가 `GOAL_AUTHORIZATION_REQUIRED`이고 그 뒤 새 승인이 기록됐거나, `STALE_EXECUTION_INPUT`이고 그 뒤 새 State 관측이 기록됐으면 다음 `run-once`가 같은 job을 한 번 재시작한다. 승인된 절대 deadline이 지났으면 `GOAL_ABSOLUTE_DEADLINE_EXCEEDED`다.
- provider thread가 결속된 활성 job은 이 판정 대상이 아니다. 이때 최상위 `next_action`의 `observe로 같은 RuntimeJob을 먼저 관측하십시오.`는 사람용 안내이고, scheduler는 계속 `run-once`만 호출한다.
- 알려진 한계(1.0 미해결): owner 소실·효과 불명 정지(`RUNTIME_JOB_OWNER_LOST`, binding 없는 job의 `EXTERNAL_EFFECT_UNKNOWN`, 재부착할 성공 결과가 없는 `owner proof missing`)를 공개 명령으로 해소하는 경로가 없다. 여러 process가 동시에 `run-once`를 실행할 때 다른 process가 진행 중인 CoreOperation을 완료 관측 없는 작업으로 볼 수 있는 문제도 남아 있다.

## 재시작과 receipt 복구

| 관측 상태 | 조치 | 금지 사항 |
|---|---|---|
| `active_runtime_job`이 있음 | 다음 tick도 `run-once`를 한 번 호출한다. `run-once`가 같은 job을 관측하고, provider thread가 결속되기 전에 owner가 사라진 job은 owner lock으로 확인한 뒤 기존 복구 경로로 넘기거나 한 번 재시작하거나 typed blocker로 멈춘다. 사람이 진단할 때는 `observe --project-id ...`로 같은 job을 관측한다 | `run-once`를 동시에 여러 개 실행하지 않음. `status`·`observe` 결과 문자열로 다음 명령을 고르지 않음 |
| 이전 owner가 종료됐고 prepared intent에 receipt가 없음 | `recover inspect --project-id ...`로 intent를 `unknown`으로 전환하고 ID 확인 | owner가 살아 있는 동안 `recover inspect`를 조회 명령처럼 호출하지 않음 |
| 원래 provider operation의 정확한 응답·binding을 확보함 | receipt JSON을 만든 뒤 `recover resume`로 기존 intent에 기록 | 새 provider 작업을 만들거나 응답을 추정하지 않음 |
| provider와 대상 상태를 확인해도 효과 여부가 불명확함 | `unknown` 상태를 유지하고 운영자 판단을 요청 | 모델 변경, 새 Task 또는 자동 재시도로 우회하지 않음 |
| 대상 재관측으로 의도한 효과가 없음을 확인했고 해당 Attempt를 종료하기로 결정함 | 근거를 `--rationale`에 남기고 `recover abandon` 실행 | effect가 없다는 근거 없이 자동 abandon하지 않음 |

`recover inspect`는 읽기 전용 명령이 아니다. 프로세스 중단 뒤 receipt가 없는 `prepared` intent를 `unknown`으로 바꾸고 프로젝트를 `recovery_required`로 전환한다.

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts recover inspect `
  --project-id <project-id>
```

`recover resume`의 이름은 새 turn을 시작한다는 뜻이 아니다. 원래 intent에서 이미 발생한 provider operation의 receipt를 원장에 결속한다. receipt 파일은 다음 형식이다.

```json
{
  "provider_operation_id": "provider가 반환한 원래 operation 또는 turn ID",
  "response": {
    "provider의": "원래 JSON 응답"
  },
  "binding": {
    "thread_id": "원래 thread ID",
    "turn_id": null,
    "host_id": null,
    "bound_at": "2026-09-09T00:00:00+00:00"
  }
}
```

`binding`은 작업 종류에 따라 생략할 수 있지만 thread/turn이 있었으면 원래 값을 기록한다. `response`를 요약하거나 새로 만들지 않는다. 같은 intent에 같은 receipt를 다시 제출하면 멱등 처리되지만 provider operation ID나 response digest가 다르면 거부된다.

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts recover resume `
  --intent-id <intent-id> `
  --receipt-file "C:\absolute\recovery\receipt.json"

flowmarshal-engine --db $Database --artifacts $Artifacts recover abandon `
  --intent-id <intent-id> `
  --rationale "대상 재관측에서 의도한 외부 효과가 없음을 확인했고 이 Attempt를 종료한다."
```

복구 뒤에는 `status`를 다시 확인하고 최초 `recover inspect` 출력의 intent ID와 대조한다. `recover inspect`를 상태 조회나 unknown 해소 목적으로 반복 호출하지 않는다. 미확정 효과가 남아 있으면 후속 provider 호출을 시작하지 않는다.

## 최종 보고와 read-only 확인

```powershell
flowmarshal-engine --db $Database --artifacts $Artifacts final-report `
  --project-id <project-id> `
  --format markdown
```

`final-report`는 Hard AC별 Core 판정과 evidence ID에 더해 `execution_summary`를 포함한다. 이 요약은 requested/observed model·effort, usage 결측, 외부 effect 상태를 서로 다른 축으로 표시한다. `read_only` Goal에서는 evidence의 원장 존재 여부, Planning 시점과 보고 시점의 Project Map semantic digest, 채팅·표준 출력 응답의 완전성과 프로젝트 source 무변경도 별도로 확인한다.
