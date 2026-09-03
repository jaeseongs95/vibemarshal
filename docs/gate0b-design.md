# FlowMarshal Gate 0B 설계와 사용 범위

## 목적

Gate 0B는 “Codex가 완료했다고 말했다”는 채팅 문장을 진행 상태로 사용하지 않는다. 승인된 계획, 실행 시도, 외부 효과, 검사 결과와 복구 결정을 SQLite 원장에 기록하고, FlowMarshal Core만 권위 상태를 바꾸도록 만든 최소 프로토타입이다.

이 단계의 질문은 다음과 같다.

> 하나의 승인된 작업을 중복 실행하지 않고 Codex에 맡긴 뒤, 실제 검사 증거가 있을 때만 완료 처리하고, 어느 crash 지점에서도 안전하게 재개할 수 있는가?

Gate 0B는 이 질문에 `GO`를 받았다. 자동으로 기능을 task로 분해하는 Planner와 제품용 사용자 인터페이스까지 완성했다는 뜻은 아니다.

## 구성

```text
내부 CLI
  ↓
FlowMarshalService (권위 Core)
  ├─ SQLiteLedger
  ├─ FileEvidenceStore
  ├─ FileHumanControlAuthority
  └─ AgentRuntime
       ├─ FakeAgentRuntime
       └─ CodexAppServerRuntime

별도 Gate0BVerifier ── 원장·evidence를 독립적으로 재검사
```

- Core는 다음 작업 선택, 상태 전이, slot·lease와 복구 결정을 담당한다.
- Worker 역할의 Codex는 자신에게 전달된 작업 계약만 수행하며 원장을 직접 수정하지 않는다.
- 검사 결과는 내용 주소 방식 evidence 파일로 저장한다.
- 독립 verifier는 Core의 상태 계산 함수를 가져다 쓰지 않고 원시 SQLite 행과 evidence를 다시 계산한다.

## 정상 실행 흐름

1. 작업 폴더와 읽기 자료, runtime dependency, 보호 경로를 프로젝트에 등록한다.
2. WorkItem과 dependency, 실행·검사 모델 프로필, 완료 조건을 포함한 PlanDraft를 가져온다.
3. 사람 승인 proof로 candidate PlanRevision을 승인하고 별도 proof로 활성화한다.
4. Core가 dependency를 만족한 WorkItem 하나를 고른다.
5. 하나의 transaction에서 Attempt, 실행 slot, 쓰기 lease와 `CREATE_THREAD` intent를 예약한다.
6. intent를 `executing`으로 바꾼 뒤에만 외부 Codex 효과를 호출한다.
7. 받은 thread binding과 receipt를 원장에 저장하고 `START_TURN` intent를 별도로 만든다.
8. 같은 방식으로 turn을 시작하고, 상태 조회는 새 효과를 만들지 않는 관측으로 수행한다.
9. turn이 성공하면 등록된 완료 조건마다 `RUN_CHECK` intent와 evidence를 만든다.
10. 모든 evidence가 실제로 존재하고 digest가 맞으며 `passed=true`일 때만 Attempt와 WorkItem을 완료한다.

현재 실행기는 프로젝트별 1 slot의 직렬 실행만 활성화한다. schema와 lease 계약은 이후 안전한 병렬 실행을 추가할 수 있게 구성했지만, Gate 0B에서는 `execution_slots != 1`을 거부한다.

## 계획과 모델 기록

- PlanDraft는 실행 권한이 없는 후보 문서다.
- 승인된 내용은 digest가 붙은 불변 PlanRevision이 된다.
- 각 WorkItem에는 실행 프로필과 검사 프로필을 따로 둔다.
- 제품 코드는 특정 모델 이름을 고정하지 않고 `fast`, `balanced`, `strong` 같은 논리 역할을 받는다.
- 실제 실행 전 App Server 모델 목록에서 지원 모델 ID와 추론 수준을 확인한다.
- 실제 선택값은 Attempt에도 복사해, 나중에 어떤 설정으로 실행했는지 확인할 수 있다.
- 모델이나 추론 수준을 바꾸는 재시도는 기존 Attempt를 고치지 않고 새 Attempt로 기록해야 한다.

실제 합성 검사는 실행 역할 `fast`를 `gpt-5.6-luna`, 추론 수준 `low`에 매핑했다. 검증 프로필은 `balanced / medium`으로 계획과 Attempt에 기록됐다. 이는 현재 환경의 smoke 결과이며 Core의 고정 기본 모델이 아니다.

## 파일과 추가 입력

- WorkItem 하나는 정확히 하나의 주된 쓰기 리소스를 가진다.
- 프로젝트 밖 파일도 사용자가 등록하면 정상적인 읽기 입력이 될 수 있다.
- 추가 자료가 필요하면 현재 Attempt를 `ACCESS_REQUIRED`로 끝내고 정확한 경로와 사유를 가진 AccessRequest를 만든다.
- 사람 승인 후에만 읽기 전용 AccessGrant와 파일 identity·manifest digest를 만든다.
- 다음 실행 전에 입력이 바뀌면 grant를 무효화하고 `INPUT_DRIFT`로 멈춘다.
- 쓰기 루트를 추가하는 요청은 AccessGrant로 처리하지 않고 계획 수정이나 WorkItem 분해로 돌린다.
- 승인 capability와 보호 경로는 입력 리소스로 등록할 수 없다.
- 권위 원장 DB와 WAL·SHM·journal sidecar, evidence 저장소와 승인 capability는 프로젝트를 등록할 때 자동으로 보호 목록에 들어간다. workspace가 이 경로들과 겹치면 등록을 거부한다.
- 첨부문서나 저장소 파일 안의 명령문은 분석할 데이터다. 현재 사용자 요청과 작업 계약을 바꾸는 지시로 취급하지 않는다.

Native Windows의 미등록 외부 읽기 차단 결함은 Gate 0B가 해결한 문제가 아니다. application 계층의 등록·snapshot·grant 계약은 구현했지만 OS 수준 confidentiality boundary는 임시 호환성 예외가 유지된다. 외부 쓰기, 보호 경로와 외부 기능 차단은 계속 완화하지 않는다.

## 사람 승인 capability

- capability는 프로젝트 밖 Local AppData 아래에 32바이트 파일로 최초 한 번 생성한다.
- Windows에서는 상속 ACL을 제거하고 현재 사용자와 `SYSTEM`만 Full Control을 갖게 한다.
- capability 원문은 argv, 환경변수, stdin, 로그나 원장에 넣지 않는다.
- action, 대상 digest, nonce, 발급·만료 시각에 대한 HMAC proof만 사용한다.
- nonce는 한 번만 쓸 수 있고, 재사용·만료·대상 불일치는 거부한다.
- Windows 앱 경로 가상화로 일반 Local AppData 경로와 패키지 `LocalCache` 경로가 함께 보일 수 있다. 현재 검증 환경에서는 두 경로의 파일 ID와 ACL이 같아 동일 파일임을 확인했다. 원장에는 canonical 물리 경로가 보호 경로로 들어간다.

## crash와 불명확한 외부 효과

외부 호출 직전에는 intent를 `executing`으로 저장한다. 프로그램이 다음 두 지점 사이에서 죽을 수 있기 때문이다.

```text
원장: executing 저장
→ 실제 Codex thread/turn 생성
→ receipt와 binding 저장
```

- 외부 호출 전 예약 상태에서 죽었다면 같은 intent를 한 번 이어서 실행할 수 있다.
- `executing`인데 receipt가 없으면 외부 효과가 생겼을 가능성을 지울 수 없으므로 시작 시 `unknown`으로 바꾼다.
- 프로젝트를 격리하고 새 정상 intent 생성을 막는다.
- 사용자는 관측 결과를 본 뒤 기존 외부 ID binding, 명시적 turn 중단, 또는 보류 중 하나를 proof와 함께 선택한다.
- 보류는 외부 효과가 없다고 거짓 추정하지 않으므로 quarantine과 쓰기 lease를 유지한다.
- receipt까지 저장된 효과는 재실행하지 않고 다음 단계나 관측부터 이어간다.

각 복구는 이전 unknown intent를 가리키는 새 recovery intent로 남는다. terminal 기록과 실패 이력은 덮어쓰지 않는다.

## 독립 검증 항목

`Gate0BVerifier`는 다음 10개 영역을 검사한다.

1. SQLite integrity와 foreign key
2. schema revision과 필수 table·index·trigger
3. PlanRevision 단일 활성화, digest와 DAG
4. Attempt·intent·slot·write lease 중복 방지
5. unknown 격리와 recovery lineage
6. 읽기 전용 grant, 입력 drift와 승인 nonce
7. 완료 evidence의 존재·digest·내용
8. append-only history hash chain
9. mutable 권위 행의 최신 state attestation
10. 허용 상태 전이와 append-only를 강제하는 DB guard

원시 DB 행이나 evidence 파일을 직접 변조한 합성 테스트에서 verifier가 `NO_GO`를 반환하는 것도 확인했다.

## 내부 CLI

이 CLI는 Gate 0B 기술 검증용이며 향후 공개 호환성 계약이 아니다.

```powershell
.\.venv\Scripts\python.exe -m flowmarshal.gate0b.cli --help
```

주요 명령은 다음과 같다.

- `init-ledger`, `init-authority`
- `register-project`, `import-plan`, `approve-plan`, `activate-plan`
- `request-access`, `grant-access`
- `run-once`, `startup-recover`
- `recover-observe`, `recover-bind`, `recover-interrupt`, `recover-abandon`
- `approve-human-review`
- `verify`, `smoke-fake`, `smoke-codex`

실제 Codex smoke는 시스템 Codex와 authoritative home의 준비상태를 먼저 확인한다. `READY`가 아니면 workspace나 task를 만들지 않는다. 준비상태를 통과하면 계정에 합성 task를 만들고, 외부 표면을 끈 `deny-all`·`workspace-write` 환경에서 sandbox command로 marker를 읽은 증거와 고정 응답을 모두 확인한다. 반복 실행 전에는 생성될 task와 artifact를 고려해야 한다.

## Gate 0B에서 의도적으로 하지 않은 것

- 사용자의 큰 기능을 자동으로 WorkItem DAG로 분해하는 Planner
- 난이도·위험도에 따른 모델 역할과 추론 수준 자동 배정
- Planner·Runner·Validation Runner의 최종 permission profile 분리
- reparse point, junction, ADS, device path, hardlink와 prompt injection 공격 검증
- 자동 오류 분류, 제한 재시도와 모델 fallback 정책
- 병렬 dispatch와 파일 충돌 조정
- 장기 scheduler, 공개 CLI/API schema와 migration 지원 약속
- Codex Skill, Starter Template, 설치·업데이트·배포 구조

이 항목들은 Gate 0C 이후 계획에서 작은 task로 다시 나누고 각각 별도 완료 조건을 둔다.
