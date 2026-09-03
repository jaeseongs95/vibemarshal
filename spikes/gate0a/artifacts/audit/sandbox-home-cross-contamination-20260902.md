# Windows 샌드박스 홈 교차 오염 사고 기록

- 상태: **해결 완료**
- 원인 코드: `SANDBOX_HOME_CROSS_CONTAMINATION`
- 비밀 파일 내용 열람: **없음**

## 원인

Gate0A가 프로젝트 artifacts 아래의 `permission-codex-home`을 임시 `CODEX_HOME`으로 지정하고 `windowsSandbox/setupStart` elevated를 호출했다. Windows 로컬 샌드박스 사용자와 방화벽 규칙은 컴퓨터 전체가 공유하지만 marker와 secrets는 각 `CODEX_HOME`에 따로 저장되므로, 컴퓨터 전역 사용자 상태와 실제 사용자 홈의 상태가 서로 맞지 않게 됐다.

실제 사용자 홈을 지정한 수동 setup에서 marker·secrets와 두 샌드박스 사용자의 `PasswordLastSet`이 같은 시각에 갱신된 뒤 복구됐다. 비밀 파일 내용은 확인하지 않았다.

## 사고 당시 판정 정정

- Gate0A-R 런타임 증거: 기존 `GO` 보존
- Gate0A-P 권한 증거: `NO-GO`
- Gate0A 전체: `NO-GO`
- Gate0B Core 단위검사: 기존 `GO` 보존
- Gate0B 전체: Gate0A 선행조건 때문에 `BLOCKED/NO-GO`

기존 결과 네 개는 각각의 `legacy/sandbox-incident-20260902-preimage-*` 파일로 보관했으며 해시는 JSON 감사 기록에 있다.

## 복구 원칙

실제 PC의 FlowMarshal은 더 이상 provisioning API를 호출하지 않는다. 실제 PC에서는 시스템 Codex와 실제 사용자 홈의 준비상태만 확인하고, provisioning 및 구버전 교차검사는 폐기 가능한 Windows VM에서만 실행한다.

과거 임시 홈의 `.sandbox-secrets`만 내용 확인 없이 Windows 휴지통으로 이동했다. 직접 영구 삭제는 실행 환경의 안전 정책이 차단해 복구 가능한 방법을 사용했다. `.sandbox` 로그와 marker는 사고 증거로 보존했고 실제 사용자 홈은 수정하지 않았다.

과거 임시 홈과 진단 폴더의 Codex 상태 DB 45개는 내용을 읽거나 해시하지 않고 `D:\codex\flowmarshal-forensic-quarantine\sandbox-home-cross-contamination-20260902`로 상대경로를 유지해 이동하고 읽기 전용으로 표시했다. 원위치의 setup marker와 sandbox 로그도 읽기 전용이며, 현재 Gate0A artifacts 안전 스캔 위반은 0개다.

## 호스트 재검증

- `sandbox-status`: `READY`, 조회 전후 상태 지문 동일
- 호스트 `setup-sandbox`: `HOST_PROVISIONING_FORBIDDEN`으로 setup API 호출 없이 종료
- 첫 권한 재검사: 과거 canary 경로의 오래된 SID ACL을 재사용해 workspace 쓰기 실패. 샌드박스 상태는 변하지 않음
- 고유 canary 경로로 수정한 두 번째 권한 재검사: 시스템 `codex-cli 0.151.0`, 전체 권한 검사와 상태 무변경 검사 통과
- Gate0B 실제 smoke `20260901T235045Z`: 준비상태 `READY`, sandbox command 성공 증거, 원장 독립 검사 `GO`

이 시점에는 폐기 가능한 Windows VM provisioning·버전 교차검사가 남아 있어 Gate0A-P와 전체 Gate0B를 `NO-GO`로 유지했다. 아래의 후속 재검증으로 이 조건은 해소됐다.

## VM 재검증 하네스 준비

- 세 개의 깨끗한 snapshot 시나리오(`current-first`, `cross-version`, `failure-retry`)를 분리해 기록
- 시나리오별 하나의 authoritative home과 단계별 최대 1회 재시도 강제
- 두 번째 홈 요청을 런타임과 setup API 시작 전에 차단하고 증거 생성
- 비밀 내용을 읽지 않는 시작 지문, 런타임 버전, readiness와 setup API 호출 여부 기록
- VM 초기화·폐기 증거까지 모두 있어야 `verify-vm-suite`가 `GO` 반환

## 해결 및 폐기 VM 재검증

- 호스트 Windows Sandbox·Hyper-V 기능은 바꾸지 않고 Oracle VirtualBox `7.2.16`을 설치
- Windows 11 IoT Enterprise LTSC Evaluation의 별도 VM에서 검증
- `current-first`, `cross-version`, `failure-retry` 세 시나리오 모두 통과
- 구버전 `codex-cli 0.147.0`에서 현재 `0.151.0`으로 같은 authoritative home 갱신 통과
- 두 번째 authoritative home 요청 차단과 제한된 실패 재시도 통과
- 반출 증거에서 인증정보·sandbox secrets·상태 DB 위반 `0`
- VM UUID `246a5d7a-af7f-49cc-8f30-5efa6351d917` 폐기와 임시 자격정보 삭제 완료
- suite `fm-sbx-vm-20260902-05`: `GO`
- 전체 회귀검사 `93/93` 통과, 실제 호스트 `sandbox-status = READY`

최종 증거는 `vm-harness/suites/fm-sbx-vm-20260902-05/suite-result.json`에 있다. 이에 따라 사고 상태를 `resolved`로 전환하고 Gate0A-P·전체 Gate0A·전체 Gate0B의 차단을 해제한다. 비밀 파일 내용은 전 과정에서 읽지 않았다.
