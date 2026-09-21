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

`status`의 `current_stage`, 한 줄 `reason`, `next_action`을 먼저 읽는다. `drill_down`은 Task·파일·검사·evidence의 원장 기록 보존 여부와 현재 입력에 대한 `validity`를 나눠 보여 준다. `record_status=preserved`이면서 `validity=invalidated`일 수 있으며, 이는 과거 근거를 삭제하지 않았지만 현재 Plan 입력으로 재사용할 수 없다는 뜻이다. `active_runtime_job`이 있으면 `observe`로 기존 job을 먼저 확인하고, active job이 없을 때만 다음 `run-once`를 호출한다. `observe`는 provider 상태만 관측하며 Task나 Goal을 직접 완료하지 않는다. 각 명령이 0이 아닌 exit code를 반환하면 다음 tick을 자동 실행하지 말고 출력된 `error_code`, `status`와 원장 상태를 확인한다.

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

Engine에 전역 daemon은 없다. Windows Task Scheduler, cron, CI 같은 외부 scheduler는 DB·artifact·역할 설정·프로젝트 경로를 절대 경로로 고정하고, 한 번에 인스턴스 하나만 실행해야 한다. 각 실행은 먼저 `status`를 읽은 뒤 활성 RuntimeJob이 있으면 `observe`, 없으면 `run-once`를 한 번만 호출한다. 명령이 실패하면 그 tick을 종료하고 새 `run-once`로 즉시 우회하지 않는다.

`run-once`가 반환됐다는 사실은 provider job 완료를 뜻하지 않는다. 다음 schedule은 다시 `status`부터 읽으며, scheduler나 PC가 재시작돼도 아래 observe-first 복구 순서를 따른다. 사용자의 Codex 예약 상태나 특정 Windows Task Scheduler wrapper는 제품 Gate가 아니다.

## 자동 복구 읽기

Task 실행이 실패하면 Core는 원장에 남은 직접 근거로만 원인을 분류하고, 승인 경계 안에서 두 가지 최소 복구만 자동으로 수행한다. 복구는 항상 `run-once`의 한 tick 안에서 일어나며 별도 명령이 필요하지 않다.

| 자동 경로 | 시작 근거 | Core가 하는 일 |
|---|---|---|
| bounded repair | provider/local이 명시한 구현 실패 code 또는 직접 test·diff·file 실패 evidence | 같은 Task·같은 Execution Spec으로 새 Attempt 하나를 허용하고 Task validation을 다시 실행 |
| Context 복구 | `CONTEXT_REQUIRED` 계열 명시 code | 같은 Task 의미로 새 Execution Spec 준비를 활성화 |
| subgraph replan | `TASK_CONTRACT_INVALID`·`DEPENDENCY_*` 계열 명시 code | 실패 Task와 그 후행 Task만 교체한 새 Plan revision을 만들고 복구 전용 독립 Reviewer 검토와 결정적 Gate를 통과한 경우에만 자동 활성화 |

자동 복구는 `run-once`에 역할 설정(`--role-config`)이 있을 때만 연결된다. 설정이 없으면 subgraph replan은 `REPLAN_PROVIDER_REQUIRED`로 명시적으로 멈춘다.

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
- `state`가 `user_decision_required`면 `next_action.blocker_code`가 필요한 판단을 가리킨다.

| `blocker_code` | 뜻과 다음 동작 |
|---|---|
| `RECOVERY_DIAGNOSIS_REQUIRED` | 직접 근거로 원인을 분류하지 못했다. provider code·직접 evidence를 확인하고 원인을 결정한 뒤 진행한다 |
| `ENVIRONMENT_RECOVERY_REQUIRED` | 다시 시도로 풀리지 않는 환경 실패다. 환경 복구를 직접 관측한 뒤 진행한다 |
| `AUTHORIZATION_EXPANSION_REQUIRED` | 목표·범위·효과·정책 확장이 필요하다. 새 Goal revision과 승인이 필요하다 |
| `SAME_FAILURE_RECOVERY_LIMIT` / `SAME_FAILURE_REPLAN_LIMIT` / `GOAL_REPLAN_LIMIT` | 원장에서 센 복구 한도에 도달했다. `limits`의 실제 횟수와 상한을 확인한다 |
| `NEW_RECOVERY_EVIDENCE_REQUIRED` | 첫 복구 이후 새 근거 없이 같은 복구를 반복할 수 없다 |
| `TASK_VALIDATION_RECOVERY_REQUIRED` | 성공한 Worker 뒤 Task validation이 FAIL이다. 실패 결과와 직접 evidence로 원인을 분류한 `RecoveryAssessment`를 명시해 재시도한다 |
| `REPLAN_PROVIDER_REQUIRED` | 현재 호출에 역할 설정이 없어 재계획 후보와 독립 검토를 만들 수 없다. `--role-config`를 지정한다 |

`codes`의 `provenance`는 값의 출처를 구분한다. `provider_observed`와 `local_derived`만 `authoritative=true`이며 자동 복구를 시작할 수 있다. 모델 응답 본문이 `IMPLEMENTATION_ERROR: ...`처럼 스스로 코드를 보고해도 provider payload에 error code가 없으면 `model_reported`로만 남고 복구는 시작되지 않는다. Worker가 완료를 자칭해도 Task는 검사 결과로만 완료된다.

## 재시작과 receipt 복구

| 관측 상태 | 조치 | 금지 사항 |
|---|---|---|
| `active_runtime_job`이 있음 | `observe --project-id ...`로 같은 job을 먼저 관측 | 새 `run-once`로 중복 실행하지 않음 |
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
