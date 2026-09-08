# Engine 사용자 workflow

`flowmarshal-engine`의 사용자 경계는 raw request에서 시작해 Goal 정규화·독립 review·Planning을 실제 역할 호출로 수행한다. 사용자는 내부 Plan ID나 digest를 승인 입력으로 복사하지 않는다.

```powershell
flowmarshal-engine --db .flowmarshal-engine/engine.sqlite3 prepare `
  --project-id <project_id> `
  --request "두 모듈을 수정하고 회귀 테스트까지 실행해 주세요." `
  --role-config config/roles.json

flowmarshal-engine --db .flowmarshal-engine/engine.sqlite3 authorize `
  --project-id <project_id> `
  --source cli-user

flowmarshal-engine --db .flowmarshal-engine/engine.sqlite3 run-once `
  --project-id <project_id> `
  --role-config config/roles.json
```

`run-once`는 한 번의 Core 상태 전이 또는 RuntimeJob 예약·관측만 수행하고 신속히 반환한다. 같은 명령을 반복하면 checkpoint와 원장 상태에 따라 이미 예약된 작업을 관측·소비하며 새 효과를 중복 생성하지 않는다. `observe`는 active provider job만 관측하고 Task·Goal 완료를 직접 판정하지 않는다.

```powershell
flowmarshal-engine observe --project-id <project_id>
flowmarshal-engine status --project-id <project_id>
flowmarshal-engine pause --project-id <project_id> --reason "사용자 검토"
flowmarshal-engine run-once --project-id <project_id> --role-config config/roles.json --resume
flowmarshal-engine cancel --project-id <project_id> --reason "요청 철회"
flowmarshal-engine final-report --project-id <project_id> --format markdown
```

`pause`는 현재 Goal revision에 결속된 제어 상태를 기록하고 active job에 bounded interrupt를 요청한다. 명시적인 `run-once --resume` 전에는 후속 tick이 차단된다. `cancel`은 현재 Goal 흐름을 재개할 수 없게 만들지만 interrupt 응답을 provider terminal이나 Task 완료로 바꾸지 않는다.

`read_only` Goal의 `final-report`는 다음을 각각 표시한다.

- 원래 Hard AC 전체와 각 Core 판정·evidence ID
- evidence ID의 실제 원장 존재 여부
- Planning에 결속된 Project Map과 보고 시점 Project Map의 semantic digest 비교

이 검사는 채팅/표준 출력 응답의 완전성과 프로젝트 source 무변경을 구분한다. 사용자가 파일 산출물을 요청하지 않았다면 별도 보고 파일을 만드는 것으로 응답을 대체하지 않는다.
