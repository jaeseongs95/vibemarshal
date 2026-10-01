# VM Linux portable 검증 보고서

## 판정

- **명시 선택한 Linux portable 검사만 PASS**: 최종 118 tests, 실패 0, 오류 0, skip 0. 전체 VM qualification 또는 1.0 release PASS가 아니다.
- 설치 wheel의 공개 CLI 9회, source 밖 cwd의 공백·한글 경로, 별도 프로세스 재조회가 통과했다. 이는 로컬 합성 SQLite의 재개방이며 Windows RuntimeJob owner 재시작 검증이 아니다.
- 설치 candidate launcher `probe`와 `project-e2e --pre-provider-dry-run`이 exit 0. dry-run의 `release_pass=false`, `provenance=fake`, 미실행 단계 5개를 그대로 보존했다.
- 제품 source 변경 없음. 원본 409파일의 SHA-256을 실행 뒤 재검사해 모두 일치했다. FM03 endpoint, 실제 provider turn, 운영 DB, native 승인 경로는 호출하지 않았다.

## 대상과 환경

- Repository: `jaeseongs95/vibemarshal`
- Commit: `32bb0f9dd9f024045d24487312b50f5b703573a3`
- 원격 tree: `27e18c0c7853c1d35a98d5437cebf0ba703ed42d`
- Sanitized source archive SHA-256: `31024d4a14fe7a5d447eba8e909df67b85c6311dfa67c23a90ccfc0411851639`
- GitHub connector tree의 Git blob과 archive manifest의 SHA-256을 409파일 모두 대조했다. Git commit의 서명 payload로 재구성한 commit hash도 정확히 일치했다.
- 이 작업 경로는 **full Git checkout이 아닌 정확 commit의 source subset**이다. `.git`, 운영 DB/WAL, 인증정보, 과거 raw provider fixture, spikes와 대부분 문서는 제외됐다. 부족한 입력이 있는 검사를 PASS로 계산하지 않는다.
- Linux/POSIX, Python 3.12.14, Node v24.19.0
- Locked dependencies: cryptography 50.0.1, openai-codex 0.147.0, pydantic 2.13.5. 새 venv 설치와 `pip check` exit 0
- 새로 만든 로컬 진단 wheel: `flowmarshal_engine-0.2.0a1-py3-none-any.whl`
- Wheel SHA-256: `b63a1422f7ac76ce6fabd839a5d9cd84c221db93b84be5fec058660483210629`
- Wheel/설치 package digest: `sha256:ba615e05569f5a172d8b4b2fbdae49f459c57c8ae46c33cd7709ff638eab2e1f`
- 공개 entrypoint 1개, wheel 총 64파일, `flowmarshal/` 57파일. Source tree에서 import하지 않는 non-editable 설치임을 확인했다. 기존 release candidate wheel과 동일하다고 주장하지 않는다.
- 실제 model/provider/effort: **NOT_OBSERVED**. 테스트 fixture의 model 문자열은 실측이 아니다.

## 발견과 분류

### 1. Windows-only D1 계약과 Linux 검사 범위를 구분해야 한다

첫 broad 실행: 122 tests, failure 기록 20개, error 기록 18개, exit 1. Subtest 오류가 포함되므로 이를 독립 실패 38개로 해석하지 않는다.

`src/flowmarshal/engine/runtime.py:130`의 `owner_lock_platform_supported()`는 `os.name == "nt"`만 지원한다. POSIX의 `RuntimeJob` 실행은 `RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix`로 명시적으로 fail-closed한다. 이 지원범위 때문에 RuntimeJob supervisor/restart와 이를 fixture로 쓰는 일부 facade 검사가 실패했다. **POSIX lock 구현을 추가하거나 차단을 mock으로 우회해 release PASS로 만들지 않았다.**

초기 broad 실행의 나머지 2 error는 실행 명령의 `FLOWMARSHAL_ENGINE_SOURCE_ROOT` 누락이었다. 명시된 source root로 바로잡은 후 두 CLI parser 검사는 통과했다. 테스트 harness의 missing-project 예상 exit 1도 실제 CLI 계약인 exit 2로 바로잡았다. 이 두 사항은 제품 source 결함으로 분류하지 않는다.

Portable selection에 추가했던 `application_revise` 2개도 완료 Goal fixture에서 Windows owner를 요구함을 확인했다. 첫 portable120 실행의 2FAIL은 원본 로그에 보존하고 명시 제외 목록에 추가했다.

### 2. 지금 증명한 신규 표면

- 실제 wheel build, Engine-only package/entrypoint 및 import graph 검사
- 설치 product bytes와 source-only developer allowlist의 분리·결속
- source 밖 cwd에서 설치 CLI help, config help, project init/show, goal create/show, plan search context, status와 존재하지 않는 project의 fail-closed
- 공백·한글 경로, 절대 DB/artifacts 경로, cwd fallback DB 미생성, 별도 프로세스 DB 재조회
- clean-install evidence의 tamper/삭제/경로 이탈/잘못된 candidate/reuse/실패 설치 및 fake dry-run 책임 경계
- portable preflight 입력 결속·변조 거부·미완료 intent 차단, fake-plugin conformance와 local timeout/cleanup
- D1 POSIX에서 ledger 변화 없이 차단하고 status 안내·pause/cancel 경계를 유지하는 실제 portable negative 검사 2개

이 결과는 과거 Cloud88 PASS를 현재 bytes의 acceptance로 재사용한 것이 아니다. 현재 exact source와 현재 wheel을 새로 검증했다. 앞선 campaign과 테스트 수를 합산하지 않는다.

## 실행 범위와 미실행

최종 runner의 `MODULES`, `EXCLUDED`와 결과의 `selected`가 정확한 선택 목록이다. Excluded 4개는 Windows owner가 필요한 facade/revise/recovery 검사다. 별도의 RuntimeJob supervisor native 실행군, Windows owner process/restart군은 portable 집계에 들어가지 않는다.

- Windows msvcrt owner lease·native scheduler·kill/restart: 이 Linux 환경에서 **NOT_RUN/지원범위 제한**. 초기 broad 실패가 이 항목의 성공 근거가 되지 않는다.
- real AGS plugin 및 실제 A2/native authority: **NOT_RUN**. fake-plugin/임시 local process 검증만 포함했다.
- 실제 provider inventory/turn, roles48/planning18, FM03 endpoint, full release campaign: **NOT_RUN**
- portable prepare/materialization의 누락된 historical JSON fixture 및 전체 source-manifest/freeze qualification: **NOT_RUN**. 추가로 명시된 sanitized 입력이 필요하다.
- dry-run 미실행5단계: deterministic_preflight, provider_model_list, evaluation_contract, checkpoint_store, cell_execution
- 원격 merge/push/release 및 production writer 파일 변경: 이 검사 작업에서 수행하지 않았다. 결과 브랜치의 게시만 별도 publisher가 담당한다.

## 재현 경로와 명령

게시용 파일에서는 cloud 절대 경로를 `${WORKSPACE}`로 치환했다. 다음 alias를 소비 환경의 절대 경로로 지정한다.

- `${SOURCE}` = `${WORKSPACE}/vm-portable`
- `${EVIDENCE}` = `${WORKSPACE}/vm-evidence`
- `${PYTHON}` = `${WORKSPACE}/vm-test-venv/bin/python`
- `${WHEEL}` = `${EVIDENCE}/dist/flowmarshal_engine-0.2.0a1-py3-none-any.whl`

명령·cwd·exit 및 확보된 elapsed는 `commands.json`에 있다. 개별 start time과 기록하지 않은 elapsed는 명시적으로 `not_recorded`다. Test runner가 출력한 elapsed는 원시 log를 따른다. 시스템 셸 날짜는 KST(+0900), 조율 시각은 2026-10-01 UTC였으며 혼용하지 않았다.

재실행 시 `VM_SOURCE_ROOT=${SOURCE}`, `VM_EVIDENCE_ROOT=${EVIDENCE}`, `FLOWMARSHAL_ENGINE_SOURCE_ROOT=${SOURCE}`를 설정한다. HOME/CODEX_HOME/TMPDIR는 disposable evidence 하위의 빈 디렉터리로 두고 source tests만 `PYTHONPATH=${SOURCE}/src`로 읽는다. public CLI smoke 및 launcher는 PYTHONPATH를 제거해 설치본을 읽는다. `public_path_smoke.py`를 다시 돌릴 때는 새 `VM_PUBLIC_RUN_NAME`을 지정한다.

## 제안

- 산출물의 `run_portable_subset.py`를 별도 Cloud-portable 진단 entrypoint 제안으로 검토할 수 있다. explicit selection/제외 사유와 skip=0을 검사하며 skip 발생 시 exit 1을 낸다.
- `public_path_smoke.py`는 기존 in-process CLI unit 검사를 보완하는 설치본 subprocess/public-path 제안이다. Provider 없는 표면만 다룬다.
- Windows 원격 환경의 현 writer가 native owner/restart 잔여 범위를 검증해야 한다. D1 지원 정책을 이번 검사에서 바꾸지 않는다.
- 이번 결과에는 재현 가능한 제품 결함을 확정하지 않았다. Linux portable 부분 검사 PASS를 qualification 전체 PASS로 확대하는 것이 방지해야 할 핵심 해석 오류다.
