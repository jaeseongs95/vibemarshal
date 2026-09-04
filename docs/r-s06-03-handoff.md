# R-S06-03 — Task 검증 연결과 프로젝트 경로 근거 보완

## 현재 상태

2026-09-04, 시작 HEAD `7428027`. [R-S06-02 실제 진단](r-s06-02-live-handoff.md)의 다음 Repair를 수행했다. **최종 source의 제한된 실제 진단은 PASS**다. 1차 진단의 기존 unittest 실행 목적 누락을 보존하고 한 차례 보완한 뒤, 2차 진단에서 모든 기준을 통과했다.

- 전체 unittest **489개 PASS**, 결정적 Gate **5/5 PASS**, legacy freeze **40개 파일 PASS**.
- 실제 진단은 기존 Goal·Skeleton·State·Project Map과 과거 불량 Plan을 사용했다. 이전과 같은 네 역할 호출·기준을 새 source에서 검사했다.
- 자연어부터 시작하는 새 S06·Plan 활성화·Worker 실행과 구분한다. 전체 Trace는 `INCOMPLETE`, 1.0은 `NO-GO`를 유지한다.

## 변경과 검증 범위

[Planning 역할 안내](../src/flowmarshal/engine/planner_roles.py)와 provider의 `PlanGoalCoverageDraft` 필드 설명을 보완했다.

1. `task_refs`는 Skeleton의 AC 기여 Task 집합이며 `validation_ids` 소유 Task의 허용 목록이 아니다. Goal의 검증 요구가 적용되는 Task는 기여 목록 밖에 있어도 필수 검사 ID를 연결한다. Task 의미와 기여 집합을 바꾸지 않는다. Reviewer는 자체 검사 존재와 Goal 검사 ID 연결을 각각 확인한다.
2. Project Map root와 Goal의 명시 대상을 대조한다. 등록 참고자료의 저장 경로·부모 디렉터리와 역할 cwd만으로 대상 변경·stale을 추론하지 않는다. 실제 대상 충돌, State·Map binding 불일치와 freshness 위반은 직접 근거로 계속 검토한다.
3. Plan Reviewer의 `affected_task_refs`는 semantic `task_ref`를 사용하고 Core의 `task_id`와 구분한다.
4. Goal이 명시한 검사 대상·종류·실행 목적은 적용 대상 Task의 `validation.statement`에 보존한다. 기존 unittest 실행을 일반 동작 검사나 `test` evidence 종류만으로 대체하지 않는다. 실제 실행 명령은 ready-time 명세에 둔다.

권위 schema, Plan 컴파일러, Core 상태 전이·판정, DB, 실제 모델 배정, fallback·재시도 한도와 공식 fixture·oracle·합격선은 변경하지 않았다. 장기 경계는 [프로젝트 지침](../AGENTS.md), [권위 설계](orchestration-redesign.md)와 작업 시작 프로젝트의 `D:\codex\자동화템플릿\AGENTS.md`에 함께 반영했다.

새 회귀는 다음을 확인한다.

- 각 Task의 필수 검사는 모두 있으나 Goal의 변경 Task 검사 ID만 누락된 후보를 그대로 전달하고 유효한 Reviewer finding으로 거부한다. 정상 후보는 후속 Task만 있는 기여 목록을 보존하면서 변경 Task의 검사 ID도 연결한다.
- 기존 paired 회귀에 검사 종류 집합은 같지만 unittest 실행 문장을 일반 동작 확인으로 바꾼 하위 사례를 추가했다. 후속 검사·Goal Test와 무관하게 유효한 자체 검사 누락 finding이 유지된다.
- 대상 밖의 실제 등록 참고자료와 별도 reviewer cwd를 정상 Project Map과 구분해 요청에 전달한다.
- 실제 파일 변경 후 새 State·Project Map에서 과거 binding의 Plan을 반환하면 결정적 `PLAN_MAP_BINDING_MISMATCH`, `PLAN_STATE_BINDING_MISMATCH`로 거부하고 Plan Reviewer를 호출하지 않는다. 정상 기준 후보는 선택된다.

Scripted 역할 테스트는 계약 전달·결합과 Core 판정을 검사한다. 실제 모델의 생성·검토 품질은 별도 진단으로 판정한다. 독립 구현 검토에서 현재 변경 범위의 즉시 수정 사항은 발견되지 않았다.

## 실행 근거

1차 진단은 `.flowmarshal-engine-eval/runs/r-s06-03-20260904`, 최종 source의 2차 진단은 `r-s06-03-20260904-v2`에 저장한다. 1차는 과거 371개 파일을 고정했고 2차는 1차 결과·당시 source snapshot까지 총 473개 파일을 보존했다. 기존 기준의 digest는 `sha256:3ab86b61a45696676fb514f7c3b03b499c4f4120edf450edb94226fa0465d1fd`이며 두 진단 모두 이전 preflight의 기준과 동등함을 호출 전에 검사했다.

[공식 App Server 문서](https://learn.chatgpt.com/ko-KR/docs/app-server)의 `config/read`는 설정 계층을 적용한 유효 구성을 반환한다. 이번 실행은 기존 정책 검사기로 `:danger-full-access / never`를 확인하고 시작했으며 역할별 thread/turn receipt를 별도로 보존한다.

| 항목 | digest |
|---|---|
| 최종 source | `sha256:72922b8c3f475bcf59d3a6a7da4e10dc6726cf812bd5feff7a07ffc4f3f3b4e9` |
| 최종 결정적 계약 | `sha256:f7e6863b58818e663854f46a5d3a0b546f6abcf5cfe8e2fa1ece4d0ebbe56f81` |
| 최종 결정적 report | `sha256:5585ce268e8e8ef0727da1cd9dca7f18d83652008451d24ad19b4e24a6ac66a9` |

모델 재호출 없는 저장 evidence 검증:

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-03-20260904-v2/verify.py
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-03-20260904-v2/verify_bindings.py
```

검증기 종료 코드와 진단의 PASS/FAIL은 구분한다. 현재 저장 판정을 검증하는 성공을 모델 진단의 합격으로 바꾸지 않는다. 실제 응답·원장·실행 기록은 Git에서 제외한다.

## 1차 진단과 한 차례 보완 근거

1차 후보는 각 Task의 `command/test/file/diff`와 `model_review` 종류 및 AC 검사 ID 연결을 모두 가졌다. Project Map root에 대한 거짓 finding도 없었다. 그러나 변경 Task의 `task_change_deterministic_contract.statement`는 합산 동작·공개 API·파일 범위 검사만 명시했고, `ac_004`가 각 Task에 요구한 기존 unittest의 실제 실행은 후속 Task에만 남았다. Reviewer의 `TASK_VALIDATION_UNITTEST_MISSING`은 Goal과 변경 Task를 직접 참조한 유효한 finding이다.

보조 정적 검토는 최초에 evidence 종류 집합을 근거로 누락이 없다고 판단했으나, 메인 에이전트가 실제 검사 문장과 Goal을 대조해 그 결론을 채택하지 않았다. `semantic-assessment.json`에 이 판단 차이를 남겼다. 1차 summary의 일부 구조 검사가 true인 것을 전체 검사 의미의 충족으로 해석하지 않는다.

1차는 4 logical calls·6 provider turns·2 schema recoveries였고 실제 역할 receipt 합계는 input **200,046**, output **12,070**, 총 **212,116 tokens**다. Skeleton의 필수 rating 누락과 상세화의 `goal_criterion_refs` 확대를 각각 한 번 복구했다. 새 Goal·Worker·Validator·Goal Test는 호출하지 않았다. 메인·보조 에이전트 비용은 이 합계에서 제외한다.

1차 `verification.json.passed=false`, `binding-verification.json.diagnostic_passed=false`를 유지했다. source를 추가 수정하기 전에 두 검증기를 실행하고 변경 source 5개를 byte snapshot으로 보존했다. 당시 source는 `sha256:7d87ae5ea3f59078e1abeed8cdddf01f0003c16541e949ea5e4c2b71167c6244`이며 해당 검증을 최종 source의 결과로 재사용하지 않는다.

## 최종 실제 진단 결과

| 검사 | 결과 |
|---|---|
| 기존 정상 Skeleton | finding 없이 rating 반환 |
| Task 자체 검사 | 두 Task 모두 기존 `test_app.py` unittest discover의 새 프로세스 실제 실행·통과를 명시 |
| 파일 범위·Validator | 직접 file/diff와 실행 역할과 분리된 semantic `model_review` 보존 |
| `ac_004` 검사 ID | 두 Task의 검사 ID 전체 연결, 기여 Task 집합은 Skeleton 그대로 유지 |
| 독립 Goal Test | 모든 Task 검증 후 동일 workspace에서 새 `command/test/file/diff`, `task_id=null` 요구 |
| 새 Plan | 결정적 Gate 통과, 의미 Reviewer finding 없음 |
| 기존 불량 Plan | Goal·Plan 직접 근거로 계속 거부 |
| 입력·source·정책 | 원본 보존, current adapter 재생·strict schema·thread/turn·정책·usage receipt 결속 통과 |

최종 후보 activation digest는 `sha256:086c235600ea38665794eb072907d46476c4eb55f0c1afa876efe0e18e133db9`다. 이는 과거 입력 진단의 후보이며 원장 등록·선택·활성화하지 않았다.

최종 진단은 4 logical calls·6 provider turns·2 schema recoveries, input **203,309**, output **12,624**, 총 **215,933 tokens**다. usage unavailable은 없다. Skeleton의 rating 누락과 상세화의 AC 기여 집합 확대를 각각 한 번 복구했다. 복구 전 응답과 오류는 그대로 남겼다. 두 진단 합계 **428,049 tokens**는 역할 receipt의 합이며 메인·보조 에이전트 비용이나 이후 S06 비용을 포함하지 않는다.

최종 `verification.json.passed=true`와 `binding-verification.json.diagnostic_passed=true`를 별도 프로세스에서 대조했다. 이 PASS는 한 입력의 제한된 진단이며 전체 Planning·E2E·token/latency qualification을 대신하지 않는다. 다음 단계로 별도 원장의 [새 S06](s06-planning-final-retry-handoff.md)을 수행한다.
