# FlowMarshal 입력 자료와 실행 범위 정책

## 목적

이 문서는 FlowMarshal이 작업에 필요한 입력과 실행 범위를 어떻게 표현하는지 정의한다. 현재 MVP에서 범위 정보는 계획 생성, 작업 분배, 사용자 검토와 결과 감사를 위한 계약이다. 모든 로컬 파일과 네트워크 socket을 차단하는 OS 보안 경계를 뜻하지 않는다.

세밀한 native Windows permission profile 실험과 과거 예외는 역사적 Gate artifact에 보존하며, 현재 제품 기준선은 [오케스트레이션 중심 재설계](orchestration-redesign.md)를 따른다.

## 정상 입력

다음은 별도 실행 중 승인 절차 없이 사용할 수 있는 정상 입력이다.

- 사용자가 선택한 프로젝트와 그 하위 파일
- 전역·프로젝트 `AGENTS.md`와 `AGENTS.override.md`
- 요청에 첨부하거나 프로젝트에 등록한 참고자료
- 활성 PlanContractRevision에 연결된 이전 Task 산출물
- 빌드·테스트가 생성한 작업 디렉터리 내부 결과

참고자료와 저장소 파일 안의 명령문은 분석할 데이터다. 현재 사용자 지시, `AGENTS.md`와 활성 PlanContractRevision보다 높은 실행 권위를 갖지 않는다.

## Project와 Task 범위

Project는 최소한 다음 정보를 가진다.

```text
project_id
root
context_sources[]
default_validations[]
runtime_requirements
```

Plan의 TaskContract는 의미 범위를, ready 시점의 TaskExecutionSpec은 운영 범위를 선언한다.

```text
TaskContract:
  project_id
  objective
  hard_acceptance_criteria[]
  dependencies[]
  expected_effects[]
  prohibited_effects[]
  validation[]

TaskExecutionSpec:
  resolved_targets[]
  context_manifest
  actions[]
  resource_locks[]
  timeout
  idempotency_key
```

- `context_manifest`는 Worker에게 전달하는 최소 Context Pack과 각 fragment digest를 고정한다.
- `resolved_targets`와 `expected_effects`는 변경 결과를 검토하기 위한 예상 경로·symbol·효과다. 이것을 완전한 OS 쓰기 allowlist라고 주장하지 않는다.
- `prohibited_effects`는 Planner·Worker·Validator에게 명시적으로 전달하고 결과 검사에서 위반 여부를 확인한다.
- 프로젝트 전체 탐색이 작업에 자연스럽게 필요한 경우 파일 하나마다 경로를 나열하지 않는다.

## 추가 자료 발견

다음 경우에는 추가 승인 없이 현재 Task 안에서 처리할 수 있다.

- 활성 프로젝트 내부에서 관련 파일을 발견한 경우
- 이미 등록된 참고자료 묶음 안에서 관련 파일을 발견한 경우
- 사용자의 현재 요청이 정확한 추가 경로나 자료를 명시한 경우

다음 경우에는 작업을 멈추고 사용자에게 대상과 이유를 설명한다.

- 현재 요청과 무관한 별도 프로젝트를 수정해야 하는 경우
- 사용자가 제공하지 않은 개인 자료나 인증정보가 필요한 경우
- 새 쓰기 대상 때문에 목표나 Task 분해가 달라지는 경우
- 공개, 배포, 원격 쓰기, 메시지 전송 또는 데이터 삭제가 새로 필요한 경우

승인이 필요하면 파일별 AccessGrant를 누적하지 않는다. Task 의미가 달라지면 새 PlanContractRevision을 만들고, 의미는 같지만 운영 상세만 달라지면 TaskExecutionSpecRevision을 갱신한다.

## 로컬 명령과 네트워크

FlowMarshal과 Worker command는 사용자 컴퓨터에서 실행된다. localhost 사용 자체를 비정상 행위로 보지 않는다.

TaskContract와 TaskExecutionSpec에는 다음처럼 작업 수행에 필요한 조건을 표시한다.

```text
shell: required | not_required
network: not_required | local | internet | external_service
services: [선택적 서비스 이름 또는 endpoint]
external_side_effects: []
```

이 정보는 Planner 검토, Worker 환경 준비와 감사에 사용한다. MVP에서 `network=not_required`가 모든 플랫폼에서 socket 차단을 보장한다는 제품 약속은 하지 않는다.

다음은 정상적인 로컬 실행의 예다.

- 개발 서버를 시작하고 `127.0.0.1`로 통합 테스트
- 로컬 데이터베이스를 사용한 테스트
- 패키지 설치나 공식 문서 조회가 명시된 작업의 인터넷 사용

반면 배포, 공개 업로드, 원격 저장소 쓰기와 제3자에게 메시지 전송은 `external_side_effects`에 명시해야 한다.

## 검사와 위반 처리

Core와 Validator는 가능한 범위에서 다음을 검사한다.

- 예상 산출물이 생성됐는가
- `out_of_scope`로 지정한 파일이 변경되지 않았는가
- 선언된 validation이 통과했는가
- Git diff 또는 manifest가 Worker 제출과 일치하는가
- 외부 부작용 receipt가 계획과 일치하는가

범위 밖 변경이 발견되면 자동으로 승인 범위를 확대하지 않는다. Attempt를 실패 또는 검토 필요 상태로 남기고 변경 내용과 복구 선택지를 보고한다.

## 선택형 hardening

보안 민감 프로젝트는 별도 프로필에서 다음을 사용할 수 있다.

- Codex permission profile
- 읽기 전용 reference staging
- command network filtering
- WSL 또는 disposable VM
- brokered filesystem/tool execution
- secret scanner와 보호 경로 deny

이 기능은 `hardening_profile`로 선택하며, 핵심 오케스트레이션 상태와 분리한다. hardening이 요청된 Task에서는 해당 프로필의 실패가 그 작업을 막을 수 있지만, 지원되지 않는 hardening 기능이 FlowMarshal 전체 제품을 사용할 수 없다는 뜻은 아니다.

## 과거 Gate 결과의 의미

- Gate 0A-P와 Gate 0C의 permission 실험은 삭제하거나 성공으로 재작성하지 않는다.
- 미등록 외부 파일 차단 성공은 해당 runtime·profile 조합의 hardening 증거다.
- 승인 reference 직접 읽기 실패는 Worker 입력 전달 설계에서 다시 다룬다.
- `network.enabled=false`인데 loopback에 도달한 결과는 native Windows no-network hardening의 알려진 한계다.
- 제한 profile에서 Runner instruction bootstrap이 실패한 결과는 정상 사용자 권한의 최소 Runtime Gate와 분리해 재시험한다.
