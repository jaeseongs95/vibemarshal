# FlowMarshal Gate 0B 판정 보고서

- 판정: **GO**
- 생성 시각(UTC): `2026-09-01T22:26:54.0785529Z`
- 선행 Gate: `Gate 0A = GO`
- 내부 원장 schema revision: `1`

## 결론

FlowMarshal가 승인된 작업 하나를 영속 원장에 등록하고, 중복 외부 효과 없이 Codex에 실행시키고, 독립된 evidence 검사를 통과한 경우에만 완료 처리하며, crash 뒤 불명확한 효과를 자동 재실행하지 않고 격리·복구할 수 있음을 확인했다.

따라서 Gate 0B의 범위인 원장·리소스·승인·실행 의도·evidence·crash recovery 최소 Core는 다음 단계로 진행할 수 있다. 다만 이 판정은 전체 제품 완성을 뜻하지 않는다. 자동 기능 분해, 모델 배정 정책, 역할별 최종 권한 격리와 공격 검증은 아직 남아 있다.

## 구현 결과

- SQLite WAL, foreign key, `BEGIN IMMEDIATE`를 사용하는 영속 작업 장부
- 불변 PlanRevision과 WorkItem dependency DAG
- Attempt, 프로젝트 실행 slot과 쓰기 lease의 중복 방지
- `CREATE_THREAD`, `START_TURN`, `RUN_CHECK`, recovery를 분리한 RuntimeActionIntent
- 실행 전 `executing`, 효과 뒤 receipt·binding 기록과 startup unknown 격리
- 프로젝트·workspace·reference·runtime dependency·보호 경로 등록
- read-only AccessRequest/AccessGrant, file identity·manifest snapshot과 drift 무효화
- 프로젝트 밖 32바이트 HumanControlAuthority capability와 HMAC proof
- nonce replay·만료·대상 불일치 차단
- 내용 주소 기반 evidence, append-only history hash chain과 state attestation
- Core 계산을 재사용하지 않는 독립 verifier
- 실제 Codex adapter, 결정적 fake adapter와 내부 기술 CLI

## 검증 결과

전체 회귀 검사는 다음 명령으로 실행했다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- 총 `63`개 통과, 실패 `0`
- Gate 0A 회귀 검사 포함
- 예약 직후, thread 외부 생성 뒤 receipt 전·후, turn 외부 시작 뒤 receipt 전·후 crash window 포함
- 동시 `run-once` 호출에서 Attempt와 외부 효과가 하나만 예약되는지 확인
- 원시 SQLite 행 변조와 evidence 파일 변조를 독립 verifier가 탐지하는지 확인
- trusted check 예외가 실행 intent를 미결로 남기지 않고 실패 evidence로 닫히는지 확인
- Desktop에 등록된 MCP 항목을 사용자 config 수정 없이 실행별로 비활성·inert 처리하는지 확인
- 원장 DB와 sidecar·evidence 저장소가 자동 보호되고 workspace 중첩이 거부되는지 확인

독립 verifier는 실제 Codex 원장과 최신 fake 원장에서 다음 10개 검사를 모두 통과했다.

1. SQLite 무결성·외래 키
2. schema 계약
3. PlanRevision digest·단일 활성화·DAG
4. Attempt·intent·slot·lease 실행 불변식
5. unknown 격리·recovery lineage
6. read-only grant·drift·nonce
7. evidence 존재·digest·통과 내용
8. history hash chain
9. state attestation
10. 상태 전이·append-only DB guard

## 실제 Codex smoke

- 실행: `20260901T222537Z`
- 결과: `completed`
- 원장 독립 검사: `10/10 GO`
- task: `01a05f13-f3ae-7110-966c-4edb0c5873ea`
- turn: `01a05f13-f6f1-76b0-9547-ae665cfe217b`
- 실행 모델: 논리 역할 `fast` → 실제 `gpt-5.6-luna`
- 실행 추론 수준: `low`
- 검사 프로필: `balanced / medium`
- 고정 응답 내용은 원장에 저장하지 않고 `sha256:80cdc57ec356d4c15ca63f2f074456bb9e7606a2de933790ea4f153a06ae9587`만 evidence에 저장
- SDK: `openai-codex 0.147.0`
- 실제 실행 파일: `codex-cli 0.151.0`

이 최종 실행에는 원장 DB, WAL·SHM·journal sidecar, evidence 저장소와 승인 capability가 자동 보호 경로로 등록돼 있다.

증거는 [실제 smoke 결과](smoke/real/20260901T222537Z/smoke-result.json)와 [독립 검증 보고서](smoke/real/20260901T222537Z/gate0b-ledger-verification.md)에 있다.

최신 fake smoke `20260901T222530Z`도 완료 결과에 thread·turn binding을 포함하고 원장 검사 `10/10 GO`를 통과했다. 증거는 [fake smoke 결과](smoke/fake/20260901T222530Z/smoke-result.json)에 있다.

## 실패 시도에서 확인한 문제와 수정

실제 연동을 맞추는 과정의 실패 artifact는 삭제하지 않았다.

- 초기 세 번은 Desktop 전용 `cua_repl` MCP 설정을 고정 SDK 런타임 parser가 해석하지 못해 원장 생성 전에 실패했다. 사용자 설정이나 인증정보를 복사·수정하지 않고 실행별 override에서 등록 MCP를 비활성·inert 형태로 고정했다.
- 별도 `thread/resume`이나 별도 App Server reader로 상태를 확인하면 실행 중 turn 소유권에 영향을 줄 수 있었다. 같은 App Server 연결에서 순수 `thread/read`를 수행하도록 바꿨다.
- 파일 도구를 포함한 smoke 한 번은 끝나지 않아 timeout 처리했다. Gate 0B 실제 smoke는 원장·thread·turn·모델·evidence 경계만 검증하는 도구 없는 고정 응답으로 축소했고, 파일 실행과 sandbox primitive는 이미 통과한 Gate 0A 증거에 맡겼다.
- 초기화 전에 실패한 오래된 세 디렉터리는 당시 구현상 `smoke-result.json`이 없다. 현재 구현은 초기화 오류도 결과 파일로 남기며 이를 확인하는 단위 검사를 추가했다.

## 파일 접근 정책 판정

프로젝트 밖의 요구사항·로그·참고 자료는 FlowMarshal 목적상 정상 입력이다. 따라서 단순히 “프로젝트 밖”이라는 이유로 막지 않고 사용자가 프로젝트나 WorkItem 입력으로 등록했는지를 기준으로 한다.

Gate 0B는 등록 자료의 canonical path, 읽기 전용 범위, file identity·manifest digest와 AccessGrant를 원장에 기록하고 drift가 생기면 중단한다. 첨부문서와 저장소 파일 속 명령문은 데이터로 취급해 작업 계약을 바꾸지 못하게 한다.

Native Windows에서 미등록 외부 읽기를 OS가 완전히 막지 못하는 결함은 그대로다. 승인된 임시 예외는 이 한 항목에만 유지한다. 프로젝트 밖 쓰기, 보호 경로 읽기·쓰기, capability 노출, 외부 기능과 command network는 완화하지 않는다. 런타임이 바뀌거나 엄격 읽기 카나리가 통과하면 예외를 재검토한다.

HumanControlAuthority의 일반 Local AppData 경로와 Codex 패키지 `LocalCache` 경로는 Windows 앱 경로 가상화로 함께 보였으나, 파일 ID와 ACL을 비교해 동일한 32바이트 파일임을 확인했다. ACL은 현재 사용자와 `SYSTEM`만 Full Control이다. 비밀 내용은 읽거나 증거에 저장하지 않았다.

## 다음 Gate로 넘기는 범위

Gate 0C에서 먼저 검증할 항목은 다음과 같다.

- Planner·Runner·Validation Runner별 실제 permission profile과 최소 ContextBundle
- 보호 경로와 외부 참조에 대한 reparse point, junction, ADS, device path와 hardlink 우회
- 첨부문서·소스·도구 출력의 prompt injection이 계획·권한·원장을 바꾸지 못하는지
- 실제 파일 작업을 포함한 end-to-end 실행과 별도 Validator

그 뒤에 사용자의 큰 기능을 WorkItem DAG로 자동 분해하고, task별 실행·검사 모델 역할과 추론 수준을 자동 배정하는 Planner를 구현한다. scheduler, 원인별 자동 재시도, 병렬 실행, 공개 CLI/API, Skill·Starter Template과 배포 구조는 그 다음 단계다.

기계 판독 가능한 전체 판정과 정확한 artifact hash는 [gate0b-results.json](gate0b-results.json)에 있다.
