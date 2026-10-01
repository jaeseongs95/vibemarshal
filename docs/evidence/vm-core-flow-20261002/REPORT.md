# Core 공개 흐름 독립 증거 — 2026-10-02

**준비·업무 승인·Plan 활성화와 D1 차단의 bounded 검사는 PASS. 전체 수직 흐름의 6개 leg는 NOT_RUN입니다.** 이 보고서는 정확한 `32bb0f9dd9f024045d24487312b50f5b703573a3`에서 완료된 실행만 게시합니다. 게시 과정에서 추가 테스트는 실행하지 않았습니다.

| 항목 | 실제 결과 |
|---|---|
| 제품 base/candidate commit | `32bb0f9dd9f024045d24487312b50f5b703573a3` |
| 제품 base/candidate tree | `27e18c0c7853c1d35a98d5437cebf0ba703ed42d` |
| 제품 source diff | 0 bytes; source proposal patch 없음 |
| 최종 probe | child exit 0, wall time 0.787924초 |
| 최초 probe | 보고용 harness 필드 오류로 child exit 1, wall time 0.849965초 |
| 최종 probe의 local scripted 호출/receipt | 6 / 6 |
| 두 disposable 시도 전체 scripted 호출/receipt | 12 / 12 |
| 두 시도 전체 승인/activation | 2 / 2 |
| live provider 호출 | 0 |
| 실행 create/turn/resume/interrupt | 0 / 0 / 0 / 0 |
| runtime intent/receipt/job | 0 / 0 / 0 |
| unittest skip | 0; explicit probe이며 suite discovery 아님 |
| orchestration 요청 model/effort | `gpt-6.1-sol` / `high`, fallback 없음 |
| orchestration 실제 model/effort | provider 관측 없음 / null |

## 실제 공개 경계

기존 `InspectionScriptedRunner`와 `FakeCodexRuntime` adapter, 단일 Task fixture를 사용했습니다. production `EngineService` 및 공개 `EngineApplication.prepare`가 raw 요청을 Goal·Plan으로 정규화했습니다. 준비 역할 호출 순서는 normalizer → Goal reviewer → skeleton generator → skeleton reviewer → plan expander → compact Plan reviewer입니다.

승인 source 문자열만 준 `EngineApplication.authorize`는 `CORE_CAPABILITY_REQUIRED`로 거절됐고 모든 원장·호출 증분은 0입니다. 공개 `ApplicationAuthority.authorization_target/authorize`는 synthetic trusted-host 업무 확인으로 승인과 Plan 활성화를 함께 기록했습니다. Plan id·revision·본문·definition/activation digest가 유지됐고 Task가 ready가 됐습니다. private capability나 미리 만든 Goal·Plan 객체를 주입하지 않았습니다. 설치 CLI 및 실제 사용자 terminal 검사는 이 증거 범위에 없습니다.

활성 Plan의 `revise`는 `GOAL_REVISION_ACTIVE_PLAN`에서 거절됐으며 원장·호출 증분은 0입니다. 이후 두 `run_once`는 아래 D1 결과를 반환했습니다.

```json
{"action":"blocked","blocker_code":"RUNTIME_OWNER_LOCK_UNAVAILABLE","detail":"RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix"}
```

두 tick 모두 원장 표와 history hash가 보존됐습니다. 첫 tick의 fixture `open_gate` 호출은 1이고 다음 tick은 캐시를 재사용했습니다. `before_execution`과 `before_completion`은 모두 0이므로 AGS verdict를 얻지 않았습니다. 이 fixture gate는 실제 AGS 수용 증거가 아닙니다. owner-lock 함수를 patch하거나 Linux 지원을 구현하지 않았습니다.

## 명시적 NOT_RUN

1. Ready ExecutionSpec materialization
2. Task admission와 AGS AND gate
3. Task execution
4. Independent Task validation
5. Independent GoalTest
6. Core GoalVerdict

최소 다음 입력은 같은 candidate와 기존 test adapter를 실행할 **D1 지원 Windows executor**입니다. 실제 AGS AND 수용 증명에는 소유자가 제공하는 선언된 plugin closure와 explicit profile이 추가로 필요합니다. credential/key/운영 DB 전송은 요구하지 않습니다. consumed FM03 endpoint는 재실행하지 않았습니다.

## 두 harness 시도와 raw 보존

최초 harness는 `EngineAuthorizationResult`를 보고할 때 `authorized.authorization_id`를 잘못 읽었습니다. 실제 공개 반환 필드는 `authorized.authorization.authorization_id`입니다. [두 줄 correction](harness/correction.diff)은 이 필드와 새 disposable 디렉터리만 바꿉니다. 제품 소스 수정은 없습니다.

[최초 harness](harness/attempt1.py)는 최종 source에서 기록된 두 줄 수정을 역으로 적용해 재구성했습니다. 최초 launch 당시 별도 harness hash는 저장하지 않았으며 이 한계를 manifest에 표시했습니다. [최종 harness](harness/attempt2.py), [최초 normalized raw](evidence/attempt1-vertical-raw.txt), [최종 normalized raw](evidence/vertical-raw.txt)와 양쪽 command receipt를 보존합니다. 원본-byte raw SHA-256과 정규화 후 SHA-256은 별도로 기록합니다.

최초 실행도 준비 6회와 persisted receipt 6개, 승인1·활성화1을 남겼습니다. 그 DB를 재사용하지 않고 새 disposable DB에서 최종 probe를 실행했습니다. synthetic DB/WAL 자체는 게시하지 않습니다. scripted `provider_calls` 행과 receipt는 local adapter 기록이며 live provider 관측이 아닙니다. fixture 요청 model은 `worker/medium`, `validator/high`; 실제 observed model/effort는 모두 null입니다.

## 재검토 자료

- [MANIFEST.json](MANIFEST.json): 게시 파일별 SHA-256, 원본 hash, 정규화 및 재구성 provenance
- [전체 결과와 증분](evidence/vertical-results.json)
- [양쪽 시도 effect accounting](evidence/all-attempt-effect-accounting.json)
- [최종 command / exit / timing](evidence/vertical-command.json)
- [최초 command / exit / timing](evidence/attempt1-vertical-command.json)
- [source identity와 empty candidate diff](evidence/source-identity.json)
- [상세 준비 보고서](REPORT.txt)

로컬 경로는 `<TASK_ROOT>`, `<PYTHON_ENV>`, `<BASE_CHECKOUT>`, `<EXECUTION_WORKSPACE>`로 정규화했습니다. 실제 credential·runtime-profile payload·key·native 메시지·personal path·운영/합성 DB와 WAL·source checkout을 포함하지 않습니다. 이 evidence 전용 branch 게시에는 제품 source 변경, merge, release 또는 Library upload가 없습니다.
