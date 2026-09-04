# R-S06-19-CLOSE provider 검사 계약 확정과 인계

기준일: 2026-09-05 KST. 대상은 `D:\codex\flowmarshal`이다.

## 세션 판정

**R-S06-19-CLOSE 완료.** 중단된 R19의 기존 3파일 보정이 자유 설명을 허용하면서 phase·근거·scope·AC link의 구조와 제출물 내부 일관성을 보존함을 확인했다. 추가 제품 구현·테스트 수정 없이 기존 보정을 채택했다. 새 provider·실모델 호출은 **0건**이다.

| 판정 대상 | 결과와 한계 |
|---|---|
| CLOSE 계약 보정·오프라인 회귀 | PASS. AC-CLOSE-01~03 충족 |
| 마지막 실제 R19 v2 | **FAIL**. 첫 clean 호출 `schema_failed`, recovery 0, 나머지 12개 미실행 |
| 원본 raw의 현재 source 오프라인 대조 | 구조 PASS, 고정 AC boolean 의미 비교 **FAIL 2건** |
| 현재 source의 실제 역할 의미 검증 | **NOT_RUN**. 새 clean/bad 실모델 PASS 없음 |
| 기존 실제 S06 v4 | **FAIL** 유지. Plan의 task phase 검사 능력 과장 |
| Functional Alpha / FlowMarshal 1.0 | **미완료 / NO-GO** |

첫 파일 조회·명령 전에 이 turn의 개발자 권한 설정에서 `sandbox_mode=danger-full-access`, `approval_policy=never`를 직접 확인하고 첫 응답에 기록했다. 부모 기대값을 관측으로 대체하지 않았다. 전역·제품 `AGENTS.md`와 권위 설계 §6.1, cutover ADR을 적용했고 대상 `src`·`tests`·`docs`에는 추가 적용 지침이 없었다. AGENTS.md와 장기 설계는 변경하지 않았다.

감사 `current-development-status.json`에서 원래 개발 작업 `01a06e4b-c912-7042-9a82-32dfa64488ae`의 turn `01a06e4b-caf4-77a0-8625-3bae594c5208`이 `interrupted`이며 최종 Handoff가 없었던 상태를 확인했다. 이번 문서는 그 개발 마감을 담당하며 원래 제품 평가 결과를 바꾸지 않는다.

## 기준 source와 Git 상태

증거 디렉터리는 `.flowmarshal-engine-eval/runs/r-s06-19-close-20260905/`다. 기존 R19 run은 `.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2/`이며 이하 각각 **CLOSE**, **R19**로 표기한다. 이 로컬 증거는 Git에 stage하지 않았다.

| 시점 | 직접 확인한 상태 |
|---|---|
| 시작·검증 HEAD | `e886f3ea43266cd1576cc1e7e22f74a3e66e44a1` |
| branch / 원격 추적 | `main`, `origin/main`보다 기존 R19 커밋 1개 앞섬 |
| 시작 dirty | `src/flowmarshal/engine/plan_inspection.py`, `src/flowmarshal/engine/planner_roles.py`, `tests/test_engine_plan_inspection.py`만 수정 |
| R19 실행 snapshot source | `sha256:739d1b672dc94fd26b4bf6690571e353b1d01d554736ad4e490f25a538267c00` |
| CLOSE source before / after | 모두 `sha256:febb5decb9b4c57957f786de8221844beed96427ce9716da42d257635b448a29` |
| 최종 변경 범위 | 기존 3파일 보정 + 본 Handoff + README·구현 현황의 현재 단계·다음 단계·검증 상태 |

시작 snapshot은 [source-before.json](../.flowmarshal-engine-eval/runs/r-s06-19-close-20260905/source-before.json)에 있다. 감사 전후의 추적 파일 3,401개와 현재 파일의 SHA-256이 모두 같았고 Engine 입력 221개 및 결정론 평가 계약도 일치했다. 감사보고서 SHA-256 `0300ab0e0dd8e56d5f9217340056ed5999645798b79f3cbe38d6e6a49e4ebaf0`도 직접 확인했다. 이 값들은 동일성 확인에만 사용했으며 reset 기준으로 사용하지 않았다.

기존 미push 커밋 `e886f3e`의 20파일은 R19 scope/AC 분리, 관련 scripted 회귀, R17 bad 원본 회귀 fixture와 R19 진단 식별 추가 범위였다. 해당 fixture는 R17 원본 10파일과 바이트가 같고 v4/v5 고정 기대·독립 검토 4파일도 원래 digest와 같음을 확인했다. 새 CLOSE 커밋에는 검토한 3파일과 상태·인계 문서만 포함한다. 기존 R19 커밋도 정상 push에 함께 포함된다.

커밋 직전 전체 source·추적 파일·dirty·diff는 CLOSE의 `source-after.json`, `final.patch`에 기록한다. 커밋 이후 HEAD·branch·dirty·원격 HEAD와 push 종료 코드는 같은 디렉터리의 `delivery.json` 및 최종 응답에 기록한다. 커밋 해시를 그 커밋 내부 문서에 자기참조로 넣지 않는다.

## 실행 snapshot → 현재 보정의 실제 변화

실행 snapshot과 현재 파일의 차이는 위 3파일뿐이다. [executed-to-current.patch](../.flowmarshal-engine-eval/runs/r-s06-19-close-20260905/executed-to-current.patch)는 그 전체 차이이며, 시작 `git diff`는 [source-before.patch](../.flowmarshal-engine-eval/runs/r-s06-19-close-20260905/source-before.patch)에 보존했다. 기존 diff는 **31행 추가·6행 삭제**다.

- `plan_inspection.py`: `procedure`를 절차 설명으로 명확히 하고 `procedure == mechanism.tool` 조건 하나를 제거했다. 같은 validation 안의 mechanism과 **같은 phase**, **mechanism.basis_refs ⊆ scope.basis_refs**여야 하는 조건은 유지했다. AC가 특정 phase의 실제 실행 자체를 명시하면 세부 관측과 별도로 그 실행의 supported scope도 표현하도록 기존 provider 지침을 보강했다.
- `planner_roles.py`: 같은 phase 실행의 scope 표현과, AC가 semantic 절차를 명시하지 않았을 때 연관 문장만으로 semantic ID를 필수 연결하지 않는 기존 경계를 구체화했다. 새 의미 oracle이나 모델 배정은 추가하지 않았다.
- `test_engine_plan_inspection.py`: 기존 교차축 회귀에서 실제 unittest 책임을 설명하는 `procedure` 변형을 허용하고 잘못된 phase를 거부하도록 검증했다. 복합 phase 양성 규칙과 sibling 비전염·field 설명의 기존 회귀를 보완했다.

| 경계 | 현재 검사와 책임 |
|---|---|
| 자유 설명 / tool | `procedure`는 길이가 제한된 설명이며 `mechanism.tool`과 문자열이 같을 필요가 없다. tool은 제출된 수단 식별 문자열로 보존한다. adapter는 별칭 정규화나 실제 도구 능력의 의미 추론을 하지 않는다. |
| phase / basis | 같은 validation의 mechanism과 phase·근거 집합이 결속돼야 한다. claim 원문 인용과 claim의 basis 포함, 존재하는 citation ID·selector·quote, 등록 파일 digest를 검사한다. 실제 phase의 능력과 설명의 의미상 적합성은 독립 의미 검토 책임이다. |
| scope | 모든 validation에 scope가 있어야 하며 scope ID는 중복 불가다. contradicted/unresolved에는 직접 근거를 공유하는 해당 종류의 finding이 필요하고 supported에는 finding을 붙이지 못한다. |
| AC link | AC×validation 행 집합, AC statement·validation_intent·validation 전체 문장·등록 자료 인용을 검사한다. true 행은 같은 validation의 supported scope와 그 근거를 연결해야 하고 false 행에는 scope 참조를 둘 수 없다. true인데 실제 Plan link가 없으면 `missing_validation_link`와 직접 근거가 필요하다. false는 선택적 기존 link를 금지하지 않는다. |
| 의미 판정 / Core | adapter는 제출물·Plan·finding·AC boolean을 수정하지 않는다. `plan_inspection_eval.py`의 고정 exact 비교는 그대로다. Core에는 기존 Plan/ReviewerSubmission만 전달하며 admission·score 권위와 DB/runtime은 유지한다. |

## 원본 실패와 오프라인 결과의 분리

R19 [preflight](../.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2/preflight.json)의 lock은 `sha256:34c13f205c33b60f6a4384e4134ede74d69e129b1db98df9442421617e3b6ea0`다. 첫 clean 요청은 `gpt-5.6-terra/high`, 실제 thread `01a06e60-2409-70a2-8093-61448ac86cb8`, turn `01a06e60-2880-7690-a478-fb8e6c64c0c0`에 결속돼 있다.

원본 [failed.json](../.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2/calls/01-compact_plan_reviewer/failed.json)의 오류는 `대조표 검사 scope 절차·phase·근거 결속 오류`다. 예를 들어 raw의 `procedure="oracle.py task phase의 동작 검사"`와 `mechanism.tool="oracle.py"`가 달라 원본 adapter가 거부했다. 같은 raw를 동결 adapter로 재생해 동일 오류를 확인했고, 현재 adapter는 수정 없이 수용했다.

그러나 원본 `terminal.json.final_response`의 AC boolean은 [clean 기대 계약](../.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2/case-expectations/clean.json)과 다음처럼 다르다. 오프라인 evaluator는 원본 request payload·사례별 input binding·기대 digest를 검증한 뒤 비교했다.

| AC × validation | 고정 기대 | 원본 제출 | 의미 |
|---|---:|---:|---|
| `ac_002 × val_task_validator_review` | false | true | 공개 계약·oracle 요구를 semantic 검토 ID의 필수 연결로 확대 |
| `ac_004 × val_task_add_behavior_contract` | true | false | AC가 명시한 task/goal phase 중 task phase의 필수 연결 누락 |

행 집합은 **28/28 일치**, boolean 값은 **26/28 일치**다. clean의 기대 finding과 제출 finding은 모두 비어 있다. 이 결과는 **과거 raw의 오프라인 의미 FAIL**이며 당시 runner가 구조 거부 이후 새 의미 PASS를 얻었다는 뜻이 아니다. 원본 summary의 `FAIL`·`schema_failed`와 usage를 그대로 보존했다.

R17 bad의 finding 1건 누락·boolean 3건 불일치는 별개의 기존 원본이다. [R17 Handoff](r-s06-17-ac-link-phase-rule-handoff.md)와 바이트 보존 회귀에 그대로 남아 있다. S06 v4의 task phase는 실제 goal 전용 양·음·0 및 위치·키워드 호출 검사를 수행하지 않는데 Plan이 그 능력을 주장해 `PLAN_TASK_PHASE_CAPABILITY_MISMATCH`로 실패했다. [기존 S06 인계](s06-planning-goal-reference-handoff.md)의 FAIL을 유지하며 task를 goal phase로 바꾸거나 ready-time 명령·수동 Plan으로 우회하지 않았다.

## 이번에 실행한 검증

실행 cwd는 `D:\codex\flowmarshal`, Python은 `.venv\Scripts\python.exe`다. 모델 호출을 포함하는 진단 `prepare/execute`는 실행하지 않았다.

| 명령·검사 | 종료 / 결과 | 증거 |
|---|---|---|
| `python -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-19-close-20260905/offline_replay.py` | 0 / PASS | `offline-replay.json`, 원본 adapter 거부·현재 수용·고정 의미 FAIL을 함께 기록 |
| 오프라인 설명 변형 3개 | 모두 수용, 의미 비교 결과 불변 | 같은 JSON·Plan을 복사한 메모리 입력만 사용 |
| 오프라인 구조 변형 10개 | 모두 거부 | 다른/null phase, mechanism 근거 누락, 없는 citation, claim 근거 누락, 다른/없는 scope, false의 scope 참조, true의 scope 누락, AC 등록 근거 누락 |
| 기존 `completed_call_verification()`·원본 digest 대조 | 0 / PASS | `binding-and-provenance.json`, R19 payload·Prompt·지침·schema·model/effort·thread/turn·receipt 7항목 |
| `python -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-19-close-20260905/run_verification.py` | 0 / 결정론 **5/5 PASS** | `checks-summary.json`, `deterministic/cells/seed-0/*.json` |
| 그 Gate의 `unittest discover -s tests -p test_*.py` | 0 / **558 tests OK**, 60.792초 | 기존 inspection·case binding·fixture·raw 회귀도 포함. 별도로 중복 실행하지 않음 |
| 그 Gate의 compileall / pip check / synthetic lifecycle | 각각 0 / PASS | 로컬 합성 검사이며 제품 Worker 실행이 아님 |
| 그 Gate의 legacy freeze | **40파일 PASS** | 누락·변경·예상 밖 경로 0, manifest `sha256:25f21e8d09fb20f1aa0b3d28f5e1946dc4423aff0c5edf7c23e77c7f62bd1f5a` |
| 최종 `git diff --check` / source·원본 보존 | 0 / PASS | `source-after.json` |

Gate 실행은 **08:29:08~08:30:13 KST**, contract `sha256:eff459d99de596ddfdfa6031fb3c0286398b37c1f1341d5f57c9f4fe8570c68a`다. source는 전후 동일했다. Python 3.12.14, Windows 11, interpreter hash·설치 package 목록과 전후 동일성은 `checks-summary.json`에 기록했다. cache는 CLOSE 아래 `python-cache`로 분리했다.

오프라인 harness 준비 중 Project Map 전체 객체를 compact catalog와 비교한 assertion, 이어 tuple/list의 Python 표현을 직접 비교한 assertion이 각각 종료 1이었다. 기존 `compact_project_map()`의 canonical digest로 **동일 projection**을 비교하도록 로컬 harness만 수정한 뒤 최종 실행이 통과했다. 원본 request·Project Map·계약·제품 코드를 보정하지 않았고 이를 제품 실패나 새 실모델 시도로 세지 않았다.

## 기존 검증 재사용 범위와 Evidence 결속

감사의 `checks-summary.json`, `source-before.json`, `source-after.json`, deterministic contract·5개 cell을 확인했다. source·Prompt·Schema 구현·테스트·fixture·oracle·runner를 포함한 221개 Engine 입력과 추적 파일 3,401개가 현재와 같으며 평가 계약도 같았다. 그러나 감사 원본은 interpreter 경로·명령만 남겼고 당시 Python binary·설치 패키지 환경의 완전한 동일성을 입증할 기록이 없었다. 따라서 **과거 558 tests·5/5를 이번 최종 실행 결과로 재사용하지 않았고**, source 동일성·과거 PASS의 참고 증거로만 재사용했다. 이번 Gate를 최종 source에서 한 번 실행했다.

| CLOSE 증거 | bytes SHA-256 |
|---|---|
| `source-before.json` | `700dc0c16a1be601ab11bb227c3fcb6a9c64ef1670cd4c7a8f802a392a5e7679` |
| `source-before.patch` | `5e9570fe12e8260a43fdabf332edad86b4cabe6efc24ddc807aaf05e90d99ea3` |
| `executed-to-current.patch` | `0b9af8218d64dd14551d54eeb888322bc0a0886dc059fe82b94c1bb0b1f0b9c0` |
| `offline-replay.json` | `8763663d98ee948bc6566035a7c3d473a59a3d0c9b4c19a2b7bf36046d7d160a` |
| `binding-and-provenance.json` | `7709d579efe9d85401ff289e179ecf6f9300fb5acbdc0fd343fa37052f435743` |
| `checks-summary.json` | `d11c407733bd77729265f4b5231a93609badf9e329e4c2f87a20bbf6dc599ad1` |
| `deterministic/qualification-report.json` | `31f0635aba4316a3c959df593fab5c5b51b4aa19f734ea37d56750f81a9d2859` |

원본 R19의 핵심 결속:

- `summary.json` bytes: `622e5b98c840b71b23238448753cb89d56283a886aa4647e6df203b8416c38f3`.
- `terminal.json` bytes: `99a9d84a12598296ec6290e560e196c405aa7f91f8f964b799409f55c9408449`.
- `failed.json` bytes: `734b34de550f0952a0764e5f4fdb4ffb71a965b28bd2d6a92fab937e140f271f`.
- clean `expectation_digest` 필드: `sha256:47a415d771546cab537991420abc595d0b15ad2353fa51b619fd2fa267d13c14`; 파일 bytes SHA-256: `95d40a6a0ecd3b59c2d2d551cf6946f0ee154c4dd663b243c38546bb8ce9bef9`.
- request canonical digest: `sha256:88fe435842f507e894f6664e709c048d89c8fba2675c2e16d524933567cf8ca0`.
- receipt canonical digest: `sha256:e7b9dfb6469f07d0add3af8bb1ea0524b9c283b52d5ab699d59a0e6d6eeef3c4`.

R19의 338개 source·raw·계약·실패 파일과 감사 증거 14파일의 before/after hash를 대조한다. 실제 DB와 과거 raw를 수정·stage하지 않으며 원본 FAIL과 동결 source를 그대로 둔다. provider finding·coverage·AC boolean·usage를 자동 보정하지 않았다.

## 다음 하나의 경계와 종료 조건

**다음 경계는 별도 제한 역할 검증이다. 이번 세션에서는 실행하거나 예약·위임하지 않았다.** 시작 시 CLOSE의 source·diff·실제 권한과 기존 계약을 확인하고, 현재 source의 Prompt·Schema를 새 입력 결속에 고정해야 한다. R19의 과거 lock을 현재 source의 실행 증거로 재사용할 수 없다.

기존 계약 위치는 다음과 같다.

- source·Prompt·Schema·model lock·호출 순서·상한·recovery: R19 `preflight.json`, `planning-binding.json`, `roles.json`, `requests/`, `schemas/`, `instruction-binding.json`, `executed-source-manifest.json`.
- 사례별 입력·고정 정답·독립 검토: R19 `case-expectations/*.json`, `expectations.json`, `independent-fixture-review.json`과 원래 `input-*.json`. exact 비교는 `src/flowmarshal/engine/plan_inspection_eval.py`의 `verify_case_expectation()`·`assess_case_inspection_review()`.
- 기존 실행·중단 경계: `scripts/diagnostics/r_s06_10.py`의 `CALL_ORDER`, `MAXIMUM_CALLS`, `verify_lock()`, `completed_call_verification()`, `execute()` 및 [R17 인계](r-s06-17-ac-link-phase-rule-handoff.md). 관련 배정은 감사의 `scoped-user-decisions.json`에 보존돼 있다. R18 분석 전문을 읽었다고 주장하지 않는다.

기존 순서는 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`다. 일반 Reviewer는 `gpt-5.6-terra/high`, expander와 나머지 역할 binding은 기존 `roles.json`을 따른다. 기존 계약의 최대 logical/provider turn은 **13/13**, schema recovery는 **0**, **첫 실패 즉시 중단**이다. clean·bad가 모두 통과해야 뒤 사례로 진행하며 생성 Plan은 독립 정상성·사례별 기대표 결속 뒤에만 Reviewer로 보낸다. 이 기록은 새 13회 실행 승인이나 상한 소진 지시가 아니다.

원본·계약 증거 누락, 동시 source 변경, 다른 failure class, 범위 밖 의미 변경 또는 기대값·oracle·threshold·model binding 변경이 필요하면 INCOMPLETE/BLOCKED로 인계한다. 같은 AC 오판을 재명명하거나 성공할 때까지 재호출하지 않는다. Plan 활성화·Worker·새 S06/S07·전체 qualification·비용 비교는 이번 종료 범위 밖이다. F04·F09는 **S10~S12**, F06은 **S17 이후**의 기존 책임으로 남긴다.
