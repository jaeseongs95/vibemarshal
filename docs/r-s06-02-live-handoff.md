# R-S06-02 실제 진단 인계 — 검증 계약은 보완됐지만 연결 누락과 경로 오판이 남음

## 판정과 다음 작업

- 2026-09-04, [이전 R-S06-02](r-s06-02-handoff.md)에서 정책 불일치로 미실행이던 제한된 실제 역할 진단을 수행했다. 이번 시작 HEAD는 `89c61f8`이다.
- **정책 차단 해소**: 새 App Server의 실제 정책과 각 역할 thread receipt가 `:danger-full-access / never`였다. 이번 세션에서 사용자 설정·권한 정책·검사기를 변경하지 않았다.
- **실제 진단 FAIL**: 각 Task 자체의 필수 검증은 생성됐지만, 변경 Task의 검증 ID가 해당 Goal AC에 연결되지 않았다. 새 Plan reviewer는 별도로 직접 근거가 없는 프로젝트 경로 finding을 제출했다. 기존 불량 Plan은 유효한 검증 누락 근거로 계속 거부했다.
- **결정적 검증 PASS**: 이번 세션에 전체 486개 테스트와 Gate 5/5를 다시 실행했고, legacy freeze 40개 파일도 통과했다.
- 이번 결과는 과거 Goal·Skeleton을 이용한 진단이다. 새 Goal 정규화·Skeleton 생성부터 시작하는 S06, 실제 Plan 선택·활성화·Worker 실행을 수행하지 않았다. 자연어 → Goal 완료 Trace는 `INCOMPLETE`, 1.0은 `NO-GO`로 유지한다.
- **다음 단위는 R-S06-03 — Task 검증 ID 연결과 Reviewer의 경로 근거 구분 보완**이다. 이 제한 진단을 통과한 뒤 고정 원문부터 새 S06을 수행한다. 현재 진단 Plan이나 과거 draft를 수동 수정·활성화하지 않는다.

## 실제 관측

준비된 `r-s06-02-20260904-v2/probe.py`와 `verify.py`의 digest 및 미실행 상태를 호출 전에 확인했다. 준비 시점과 같은 source·역할 설정·model inventory·Codex 실행 파일을 사용했다. `config/read`가 설정 계층을 적용한 유효 설정을 반환한다는 [OpenAI 공식 문서](https://learn.chatgpt.com/docs/app-server)에 맞춰 metadata를 확인하고, 별도로 생성된 각 thread의 실제 receipt까지 대조했다.

| 검사 | 결과와 근거 |
|---|---|
| 기존 정상 Skeleton 검토 | PASS. 빈 `detail_requirements`를 허용하고 finding 없이 rating 반환 |
| Task 자체의 필수 검증 계약 | PASS. 두 Task 모두 deterministic `command/test/file/diff`와 분리 Validator의 semantic `model_review` 계약을 가짐. 실제 Task 검증 실행은 아님 |
| Task 검증의 Goal 연결 | **FAIL. `ac_004.validation_ids`에서 변경 Task 검사 2개가 빠짐** |
| 독립 Goal Test 계약 | PASS. 모든 Task 검증 뒤 새 evidence를 요구하는 4개 independent 검사와 AC 연결 보존. 실제 Goal Test는 미실행 |
| 상세 Plan 결정적 Gate | PASS. 기존 Task 집합·목적·입출력·dependency·AC 기여 관계 보존 |
| 새 상세 Plan 의미 검토 | **FAIL. `PLAN_PROJECT_ROOT_STALE` 제출, Core 계산 결과 `needs_revision`** |
| 기존 불량 Plan 거부 | PASS. 변경 Task 자체의 unittest·독립 Validator 누락을 Goal/Plan 직접 evidence로 지적 |
| 원본과 source 보존 | PASS. 여러 보존 대상 root의 271개 파일·원장 byte와 작업 복사본 보존. 단일 S06 manifest의 파일 수가 아님 |

### 1. 검증은 있지만 Goal 검증 ID 연결이 빠짐

실제 Goal의 `ac_004`는 **각 Task**의 실제 unittest·파일 범위 직접 evidence와 실행 역할과 분리된 Validator를 요구한다. 새 `task_change_add`에는 다음 계약이 생겼다.

- `val_change_unittest_scope`: 새 unittest 프로세스와 파일 집합·보존 파일 hash·본문 밖 AST·공개 계약·diff 검사.
- `val_change_scope_review`: 원본 file과 Worker 응답 관측을 함께 참조하는 독립 semantic 검토. `model_review`, `external_observation`, `file`을 요구한다.

그러나 `ac_004.validation_ids`에는 후속 Task의 `val_task_unittest_scope`, `val_task_evidence_review`만 들어갔다. 이전 회귀가 이미 요구한 변경 Task 검사 ID 연결 조건과 호출 전 고정한 진단 기준을 충족하지 못했다. `task_checks.task_change_add.coverage=false`가 이를 검출했다.

Skeleton에서 AC에 기여하는 Task 목록과 상세 Plan에서 검증을 연결하는 ID 목록은 구분된다. [adapter](../src/flowmarshal/engine/planner_roles.py)는 Skeleton의 기여 Task 집합을 보존하면서 별도의 `validation_ids`를 상세화할 수 있고, [기존 회귀](../tests/test_engine_role_adapters.py)도 그 형태를 허용·검사한다. 필요한 것은 변경 Task의 검사 ID 연결이며, 이 진단을 통과시키려고 Skeleton의 Task·목적·기여 범위를 재정의할 필요는 없다.

이 누락으로 실제 Task나 Goal이 잘못 완료된 것은 아니다. 실행·완료 판정은 발생하지 않았다. 또한 새 Plan에 `external_observation`이 포함됐다는 사실은 실제 Worker 응답 수집·검증 경로의 실행 증거가 아니다.

### 2. Reviewer의 프로젝트 경로 finding에는 직접 근거가 없음

새 Plan reviewer는 S05 작업 복사본을 가리키는 Project Map이 낡았으며 실제 작업 복사본은 `s06-bugfix-trace-20260904-v2/workspace`라고 주장했다. 저장된 입력과 독립 읽기 전용 검토에서 확인한 역할은 다음과 같다.

| 경로 | 입력에서 확인한 역할 |
|---|---|
| `s05-bugfix-trace-20260904/workspace` | 과거 Goal과 Project Map의 의도된 대상 프로젝트 |
| `s06-bugfix-trace-20260904-v2/validation-reference.md` | 등록된 검증 참고자료. 본문도 대상 workspace를 S05로 명시 |
| `r-s06-02-20260904-v2/workspace` | 이번 진단 역할의 동일 byte cwd 복사본 |
| `s06-bugfix-trace-20260904-v2/workspace` | Reviewer가 주장한 경로. 이번 관측에서 존재하지 않음 |

참고자료가 저장된 디렉터리는 대상 프로젝트 변경의 증거가 아니다. 실제 모델 요청을 현재 adapter로 재구성한 결과도 저장 request digest와 같았으며, Plan·Goal·State·Project Map binding이 일치했다. 따라서 이 finding은 참고자료 위치를 프로젝트 root와 혼동한 것으로 판단한다. 역할 cwd 복사본은 OS 격리를 뜻하지 않는다.

해당 finding을 삭제하거나 Core 결과를 다시 합격으로 계산하지 않았다. 실제 후보에는 별도의 Goal 검증 연결 누락도 있으므로, 후보 전체를 정상이라고 판정하거나 정상 후보의 오거부율을 계산하지 않는다.

## 사용량과 복구

| 실제 역할 | logical calls | provider turns | input tokens | output tokens |
|---|---:|---:|---:|---:|
| 정상 Skeleton reviewer | 1 | 1 | 28,873 | 558 |
| Plan expander | 1 | 2 | 69,976 | 8,585 |
| 새 Plan reviewer | 1 | 2 | 72,974 | 3,257 |
| 기존 불량 Plan reviewer | 1 | 1 | 31,146 | 1,720 |
| 합계 | 4 | 6 | 202,969 | 14,120 |

총 **217,089 tokens**는 이번 제한 진단의 실제 역할 receipt 합계이며 usage unavailable은 없다. 메인·보조 검토 에이전트와 미실행 Worker·Validator·Goal Test 비용은 포함하지 않는다. provider turn별 누적값을 임의 분할하거나 M3의 Goal 전체 정산으로 표현하지 않는다.

기존 허용 schema recovery를 두 번 사용했다. 상세화의 첫 출력은 Skeleton dependency 의미를 바꿨고, 새 Plan reviewer의 첫 출력은 semantic task_ref 대신 Core Task ID를 참조했다. 각각 같은 모델·effort에서 한 번 복구했으며 복구 전 응답과 오류 문자열을 보존했다. 최종 structured 출력은 모두 반환됐으나 진단의 의미·연결 기준은 FAIL이다. fallback·추가 재시도·합격선 변경은 없었다.

## Evidence 검증과 보존

준비된 `verify.py`로 source·strict schema·inventory·정책·thread/turn·usage receipt·원본 보존과 실패 판정을 별도 프로세스에서 재검증했다. 수집기 검토에서 준비 manifest 및 입력 간 digest 연결의 명시적 assertion이 부족한 점을 찾아 `verify_bindings.py`로 보충했다.

- 준비 파일 digest와 source·실행 파일·역할·model lock을 재대조했다. 원래 `probe.py`와 `verify.py`는 변경하지 않았다.
- 과거 원본 Goal·Skeleton·불량 Plan과 진단 입력을 직접 비교하고, 원장 byte 복사본만 `mode=ro&immutable=1`로 조회해 State·Project Map을 대조했다.
- Goal·Skeleton·Plan의 상호 digest, Reviewer candidate/evidence catalog binding을 검증했다.
- 저장된 실제 역할 응답을 현재 adapter로 재생해 request와 결과를 대조했다. 재컴파일 시 새로 생기는 Task ID만 task_ref로 대응시켰으며 새 모델 호출·Core 기록은 없다.
- 최종 provider 응답 JSON과 역할 payload, 각 terminal의 thread/turn ID를 비교했다. 새 결정적 보고서와 5개 완료 cell도 대조했다.

검증 스크립트의 종료 코드 0은 **저장된 FAIL 결과가 근거와 일치한다**는 뜻이다. `verification.json.passed=false`와 `binding-verification.json.diagnostic_passed=false`를 유지했다. 수집기 보충 검증 성공을 실제 진단 성공으로 바꾸지 않는다.

| lock | 값 |
|---|---|
| source digest | `sha256:df2774e70648c40cef7bfd0ce3cbfbd31d6dea32f698d557f2b41251cf5e573a` |
| 진단 preflight | `sha256:e8a106a9ef67321034e0859928ab75f1e51baaf116760476e50c39424ef03a6c` |
| 진단 Plan activation digest | `sha256:514b1bffc31952e8dca9d41900a0fab5ab7b61a076e31c40fadd6181b384f18c` |
| 결정적 계약 | `sha256:463a26be5249b883e77e3bca4e63bf4c41605c099cfdc2023541c05298b2ee4c` |
| 이번 결정적 report | `sha256:a8e6c5127cd1f0658459e07f430a6c5c818f747a2812d03fc07fa111b2e707e5` |

로컬 root는 `.flowmarshal-engine-eval/runs/r-s06-02-20260904-v2`다. `preflight.json`, `input-*.json`, `calls/`, `summary.json`, `verification.json`, `recovery-observations.json`, `binding-verification.json`과 `deterministic-session/`에 실제 근거를 보존한다. 실제 응답·원장·실행 이력은 Git에서 제외한다. 과거 정책 차단 기록은 이전 디렉터리에 그대로 남아 있다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-02-20260904-v2/verify.py
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-02-20260904-v2/verify_bindings.py
```

## 다음 Repair의 완료 조건

1. Goal의 적용 범위에 해당하는 Task 검증 ID를 `goal_coverage.validation_ids`에 연결한다. Skeleton의 AC 기여 Task 목록과 Task 의미는 보존하고, 관련 없는 Task까지 검증 요구를 확대하지 않는다.
2. Reviewer가 대상 Project Map root·등록 참고자료 경로·역할 cwd를 구분하게 한다. 실제 target·snapshot·digest 불일치는 계속 거부하며 이 finding 이름만 예외 처리하지 않는다.
3. 정상·연결 누락·실제 stale 입력 회귀와 제한된 실제 역할 진단을 수행한다. 기존 불량 Plan 거부를 유지하고, 현재 실패 결과·oracle·기준을 수정하지 않는다.
4. 진단을 통과하면 새 source lock과 별도 원장에서 고정 원문부터 새 Goal 정규화·독립 검토·Skeleton·선택 Plan을 얻는 S06으로 진행한다. 실제 선택 Plan 전에는 S07로 진입하지 않는다.
