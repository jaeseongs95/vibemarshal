# S06 재평가 인계 — Task별 필수 검증 누락으로 선택 Plan 없음

## 판정과 다음 작업

- 세션: `S06-RETRY-01`, 2026-09-04. [R-S06-01](r-s06-01-handoff.md) 이후의 실제 자연어 → Goal 생성·독립 검토 → Skeleton 생성·검토 → Plan 상세화·검토를 새 원장에서 실행했다.
- **S06 재평가 FAIL**: Core 최종 판정은 `needs_revision`, finding은 `VERIFICATION_TASK_VALIDATOR_GAP`, `selected_activation_digest=null`이다.
- 이전 Goal Test의 일반 Task 재귀 배치는 이번 후보에서 나타나지 않았다. 그러나 수정 Task에 필수 unittest와 독립 Validator 검증이 빠져 선택 Plan을 확보하지 못했다.
- 현재 source의 **전체 484개 테스트·결정적 Gate 5/5 PASS**와 이번 실제 Planning 실패를 구분한다. 자연어 → Goal 완료 전체 Trace는 `INCOMPLETE`, 제품은 `flowmarshal-engine 0.2.0a1`, 1.0은 `NO-GO`다.
- 마지막 checkpoint는 활성 Goal 1개, admissible Skeleton 1개, 미선택 `draft` Plan 1개와 `pending` Task 2개다. Plan 활성화·Execution Spec·Attempt·Worker·Task/Goal validation·GoalVerdict는 0건이다.
- **다음 단일 작업은 R-S06-02 — Goal의 Task별 필수 검증 요구를 Planning 상세화에 보존하는 Repair**다. 실패 Plan을 수동 수정하거나 활성화하지 않는다. 실제 선택 Plan이 생기기 전에는 S07로 진입하지 않는다.

## 입력과 호출 계약

[S05](s05-bugfix-trace-handoff.md)의 원문·fixture·oracle·역할 설정과 [첫 S06](s06-planning-handoff.md)의 Profile·등록 참고자료 본문을 그대로 사용했다. 실행 원장과 artifact root는 새 디렉터리이며 workspace는 S05의 초기 작업 복사본이다. 과거 Goal·refined Skeleton·진단 Plan이나 기대 proposal을 새 모델 출력으로 주입하지 않았다.

| 항목 | 값 |
|---|---|
| 시작 HEAD | `6986dca1143ec55242cc251777a7f626aa82492b` |
| 현재 source digest | `sha256:0e5fbef54b55c153729daa025e41064c59a363ccde280123785aa44d9cbb70b0` |
| S05 입력 lock | `sha256:367e331f40f1ce38ffdbfaf813d11edb20859396677d34aaef2c2308b177ac42` |
| 새 preflight digest | `sha256:1d9af226e9303d2d721708958b0eb86b24a131962a7f63f39035f536e4853cf7` |
| 원문 digest | `sha256:7a2e53cd7c7f0aebbda43ca6048972e122e9e08cab6c9679d26cab5d09e7e1a4` |
| 역할 설정 digest | `sha256:be726ac5b76c4b6e12172a5e6e4060cfc8d1ed832d2e5e31167333ad2d52564e` |
| 실제 inventory digest | `sha256:82e6bbcba85b38800c736c9f9809a493fbc9b3270cb53b514721a80839a49f14` |
| model lock digest | `sha256:5cca3c82ad7d7eb4e61aea0dba9e2101a465860b8abe1ec771538bc7d13762ec` |
| 등록 참고자료 digest | `sha256:c0c58dc3ed0ff48b9f6161d1013d8fb8972549d20378322843f7279033b01ed8` |
| project ID | `project_63dc1a480b8f4796a52fa16a5affd57a` |
| Goal revision | `goal_revision_081f70b6edd149319263acb296fda9c0` |
| Goal digest | `sha256:7090024da135c09bb189dd2dfa91c44f6cabcb94a1a354cc2414ff62ffc65383` |
| 미선택 Plan revision | `plan_revision_ec0ebdf5c9414f0685660c3b11b4c20b` |
| 미선택 Plan activation digest | `sha256:993436f028a14ae17d3f4d7ff749815a74993cbc982a242312f0f1b2aa30023d` |
| State snapshot digest | `sha256:94eb2ec16f18d5cafe0a87d4c7a4ce29c52b92e289ffb92584ecb8b52c879475` |
| Project Map digest | `sha256:b0f373bd67d893c6a930525eb2233b3ab7e1e3f91b4ab535cfcc724be38838d7` |

호출 직전 실제 정책 `:danger-full-access / never`, 실행 파일 digest와 inventory/model lock을 다시 확인했다. Normalizer·Skeleton generator·Plan expander는 Luna/high, Goal reviewer는 Sol/xhigh, Skeleton/Plan reviewer는 Terra/high다. Executor Terra/high와 Validator Sol/xhigh는 Plan 배정만 있으며 실제 실행하지 않았다. fallback이나 모델·추론 수준 변경은 없다.

기존 `goal create --live --request <고정 원문>`과 `plan search --live --candidate-count 1`을 각각 한 번 실행했다. 로컬 수집기가 기존 CLI의 runner/runtime을 상속해 요청·응답을 추가 저장했으며 입력, validator, schema 변환, 후보 선택 및 재시도 로직은 원래 구현에 그대로 전달했다. 두 CLI의 종료 코드 0은 처리 완료이고 Plan 선택 성공을 뜻하지 않는다.

## 실제 Goal과 Plan의 의미 검토

실제 Goal은 다섯 AC로 정규화됐다. S05의 여섯 의미를 통합한 것이며 AC 개수·ID·문장의 동일성은 요구하지 않았다. Goal reviewer는 finding 없이 rating을 제출했다. 별도 읽기 전용 검토와 메인 에이전트의 원문 대조에서도 의미 누락은 발견하지 못했다.

| S05의 고정 의미 | 실제 Goal | 상세 Plan 관측 |
|---|---|---|
| 양수·음수·0 덧셈 | `ac_001` | 수정·검증 Task와 독립 동작 검사 연결 |
| 공개 이름·annotation·위치/키워드 호출 보존 | `ac_001`, `constraint_003` | Task 완료 조건과 독립 Goal 검사에 유지 |
| add 구현만 최소 변경·파일 보존 | `ac_002`, `constraint_001~002` | `file/diff`, 파일 집합·byte hash·본문 밖 AST 요구 유지 |
| 기존 unittest 실제 통과 | `ac_003` | 후속 검증 Task와 독립 Goal 검사에 유지 |
| Task의 직접 evidence와 분리 Validator | `ac_004` | **수정 Task 자체의 command/test/model_review 누락** |
| 모든 Task 뒤 독립 Goal Test | `ac_005`, `constraint_004` | `iv_goal_independent`, 새 command/test/file/diff 및 `independent` 유지 |

실제 DAG는 `task_change_add → task_validate_change`다. 후속 검증 Task는 독립 Goal Test를 수행하지 않는다고 명시하며, `iv_goal_independent`는 별도 `integration_validations`에 있다. 일반 Task가 자신의 검증을 포함한 전체 Task 완료를 기다리는 이전 구조는 없다. 이 단일 관측으로 일반적인 Planning 안정성을 선언하지 않는다.

최종 Reviewer는 `task_change_add`를 직접 지목했다. 이 Task의 유일한 `val_change_output`은 deterministic `file/diff`만 요구하며 unittest의 `command/test`나 semantic `model_review`를 요구하지 않는다. `independence_required=true`인 모델 배정만으로 실제 Validator 호출을 보장할 수 없다.

후속 `task_validate_change`에는 해당 검사들이 있지만 [Core의 Task 완료 처리](../src/flowmarshal/engine/service.py)는 각 Task 계약의 validation 집합이 모두 PASS인지 확인한 다음 dependency를 해제한다. 따라서 후속 Task의 검사 계획은 선행 Task 자체에 명시된 검증 요구를 대체하지 않는다. Reviewer의 `artifact:plan_contract`, `source:goal`과 `affected_task_refs=[task_change_add]`는 직접 근거에 맞는다. **검토가 유효하게 거부한 사례이며 실제 잘못된 Task 완료가 발생한 사례는 아니다.**

추가로 생성된 Worker 응답의 `external_observation` 검사는 별도 실행 준비 경계에서 확인할 항목이다. 현재의 직접 실패나 이전 Goal Test 재귀 문제와 합쳐 추가 실패로 집계하지 않았다.

## 실제 사용량과 structured recovery

| 단계 | logical calls | provider turns | input tokens | output tokens | recovery |
|---|---:|---:|---:|---:|---:|
| Goal 정규화 | 1 | 1 | 25,149 | 1,279 | 0 |
| Goal 검토 | 1 | 2 | 55,967 | 1,088 | 1 |
| Skeleton 생성 | 1 | 2 | 63,131 | 2,758 | 1 |
| Skeleton 검토 | 1 | 2 | 62,913 | 735 | 1 |
| Plan 상세화 | 1 | 2 | 70,425 | 9,097 | 1 |
| Plan 검토 | 1 | 2 | 70,296 | 1,449 | 1 |
| 합계 | 6 | 11 | 347,881 | 16,406 | 5 |

총 **364,287 tokens**는 이번 Goal 준비·Planning 역할 receipt 합계다. 6개 고유 call의 실제 receipt와 원장 사용량이 일치하고 usage unavailable은 없다. 메인·보조 검토 에이전트 비용, 미실행 Worker·Validator·Goal Test 비용은 포함하지 않는다. 누적 receipt를 provider turn별 비용으로 임의 분할하지 않았으며 M3 전체 정산 완료나 이전 실행 대비 비용 개선을 주장하지 않는다.

이번에는 복구 전 원문과 실제 recovery 입력의 오류 문자열도 보존했다.

- Goal reviewer와 Skeleton reviewer: `findings=[]`, `ratings=null`로 반환해 무결함 rating 필수 검증 실패.
- Skeleton generator: dependency의 product가 consumer Task의 consumes에 없어 Core 후보 검증 실패.
- Plan expander: `task_change_add`의 목적 문장을 변경해 기존 Skeleton 의미 보존 검사 실패.
- Plan reviewer: `affected_task_refs`에 semantic task_ref 대신 Core task ID를 사용해 참조 검증 실패.

각 logical call은 기존 허용 범위의 recovery 1회 후 structured 출력을 반환했다. 최종 schema failure는 0건이지만 semantic Plan 판정은 FAIL이다. Skeleton refinement는 0회이며 structured recovery와 후보 보정을 혼동하지 않는다. 복구를 위해 합격선·oracle·재시도 한도·schema·모델을 변경하지 않았다.

## 검증과 수집 한계

`verify_trace.py`를 별도 프로세스에서 실행해 다음을 대조했다.

- 현재 source·canonical preflight, S05 원문·fixture·oracle·역할 설정, 단계별 argv와 started/completed marker 및 stdout/stderr byte digest.
- 실제 request·전송 strict schema·turn intent/receipt·terminal 원문·역할 receipt·각 원장 usage 행과 모델/effort/policy binding.
- Goal 원문과 현재 adapter의 재구성 요청, 원장의 Goal/Profile/State/Project Map/Skeleton/Plan, 실제 raw review와 Core decision.
- 저장 Goal·State·Project Map으로 Skeleton/Plan의 결정적 Gate 재계산. 선택 digest null, Plan `draft`, Task 2개 `pending`, 모든 실행·검사 효과 0건.
- SQLite application ID·integrity·foreign key, History 15건의 hash chain. 과거 S05/S06/Repair artifact 130개와 workspace의 byte 보존.
- 결정적 계약·report·5개 완료 cell의 digest·scope·개수·PASS 일치. 전체 484개 unittest, compileall, pip check, synthetic lifecycle, legacy freeze 40개 검사 PASS.

결정적 계약 digest는 `sha256:243671336ce5095fee2573bf1e39af0e6587b2a730b65a162b2e1a0c8ae46594`, report digest는 `sha256:c5361c8d191f386f93a0771240db3e44348c83e6086f7fe61e6694191983d3ed`다. source는 실행 전후 동일하다. 제품 구현·AGENTS.md·권위 설계·공식 fixture·oracle·합격선·기존 실행 기록은 수정하지 않았다.

최초 검증기의 Profile 비교는 `project source add`가 정상적으로 만든 revision 2의 등록 source ref를 고려하지 않아 assertion이 실패했다. 최초 Profile을 고정 파일과 비교하고 새 Profile의 supersedes·등록 ref 집합을 별도 대조하도록 검증기만 보정했다. 원인과 수정은 `verifier-correction.json`에 보존했다. 모델 재호출이나 제품 판정 변경은 없다.

수집기는 UTF-8과 저장·digest의 canonical 변환을 통일하고, 원래 선언 순서에서 strict schema를 만든 뒤 별도 보존했다. thread/create 원시 receipt와 모든 active polling 관측은 별도 저장하지 않았다. turn intent/receipt/terminal, runner의 thread 생성 event와 호출 직전 정책 관측은 남아 있다. 이번 결과는 정상 종료 수집 증거이며 timeout·강제 종료의 완전한 수집·복구 증거가 아니다. 기존 SQLite가 다시 열리면 WAL hash가 변할 수 있으므로 보존 대상 원장을 다른 실행에서 열거나 갱신하지 않는다.

## Evidence와 R-S06-02 인계

로컬 root는 `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\s06-bugfix-trace-20260904-v2`다. 실제 운영 원장·응답·usage는 Git에 포함하지 않는다.

- `preflight.json`, `run_stage.py`, `capture_cli.py`, `strict-schema-lock.json`: 고정 입력과 관측 수집기.
- `goal.stdout.json`, `plan.stdout.json`, `*.started/completed.json`, `*.progress.jsonl`: 실제 CLI 결과·효과 marker·역할 진행.
- `calls/goal`, `calls/plan`: 실제 요청·strict schema·원문 출력·복구 전 응답·turn intent/receipt와 완료 관측.
- `flowmarshal-engine.sqlite3`, `artifacts/`: 실제 새 Core 원장과 artifact.
- `verification-summary.json`, `recovery-observations.json`, `session-outcome.json`, `verifier-correction.json`, `deterministic/`: 검증·의미 검토·비용·수집 오류 근거.
- `evidence-manifest.json`: 완료 시점 92개 파일의 byte manifest. digest는 `sha256:5c7880ce76b455d1cfcbfd4f5f11fc0c39ce6f611d455e53f3bbeb6125f9c98c`다.

읽기 전용 재검증:

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/s06-bugfix-trace-20260904-v2/verify_trace.py
```

**R-S06-02의 범위는 Goal에서 Task별로 요구한 검증을 계획에 보존하는 경계 보완이다.** AC coverage의 산출물 기여 관계, 각 Task 완료 전에 필요한 자체 validation, 모든 Task 뒤의 독립 Goal Test를 구분한다. 이 Goal의 요구는 수정 Task에도 실제 unittest·파일 범위 검사와 독립 Validator evidence를 남기는 것이다. 이를 모든 제품 Task에 무조건 적용하는 새 정책으로 일반화하지 않는다.

현재 raw Goal·Skeleton·Plan·finding을 회귀 입력으로 사용해 상세화가 직접 검사·semantic 검증 요구를 빠뜨리지 않는지 검증한다. 정상 후속 검증 Task와 독립 Goal Test는 계속 허용하고, Task 이름·개수 고정, finding 삭제, `independence_required` 배정만으로의 검사 대체, Goal 요구 약화는 하지 않는다. 수정 후 작은 실제 역할 진단을 먼저 확인하고 새 source 계약에서 새 S06을 수행한다. 기존 draft를 수동 Plan이나 Execution Spec으로 보정해 전체 Trace를 대신하지 않는다.
