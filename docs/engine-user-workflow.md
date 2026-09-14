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
