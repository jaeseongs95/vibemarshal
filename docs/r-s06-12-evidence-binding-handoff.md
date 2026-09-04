# R-S06-12 검사 근거 연결과 사례별 평가 결속

## 결과와 적용 범위

**구현·결정적 검증 완료, 제한 실제 진단 FAIL. 전체 qualification·1.0 cutover는 NO-GO다.**

실제 진단은 `r-s06-12-20260905-v1`의 첫 `clean` Reviewer에서 구조 오류를 발견하고 즉시 종료했다. 논리 호출·provider turn은 각각 **1/13**, schema recovery는 **0회**다. 남은 12회와 생성 Plan 검토를 수행하지 않았다. 실패 후 새 run에서 추가 실제 호출하지 않았다.

진단 실행 source와 최종 제출 source는 구분해야 한다. 실행 중 별도로 발견한 인용 재사용 오류를 진단 종료 뒤 수정했으며, 최종 source에는 결정적 검증만 다시 수행했다. **최종 source에 대한 새 실제 모델 검증은 미실행**이다. 실행 snapshot·원문·기대값·FAIL은 보존했다.

| source | source manifest digest | 검증 |
|---|---|---|
| 실제 호출 전 잠금 source | `sha256:de7acb1fafe7d896db989dc1e269b63beb103a5b01cb849f9c346a04ee9e3c8f` | unittest 546개, 결정적 Gate 5/5 PASS; 실제 첫 호출 FAIL |
| 최종 제출 source | `sha256:41f198deb409ff66c6067f49ed596e3d63e9e11552c9e35b3cea4c870dd57900` | unittest 547개, 결정적 Gate 5/5 PASS; 추가 실제 호출 0회 |

시작 HEAD와 로컬 `origin/main`은 모두 `035b92e23335d6880e646fb2c19a2969dde4e125`였고 작업 트리는 clean이었다. 이후 읽기 전용 `git ls-remote origin refs/heads/main`에서도 같은 원격 HEAD를 확인했다. 작업 제품은 `D:\codex\flowmarshal`이며 역할 세션 cwd를 제품 소스로 사용하지 않았다. 첫 조회 전에 이 세션에 실제 제공된 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인하고 자기 세션에 기록했다. 조율 세션에 메시지·callback·예약을 보내지 않았다.

## 구현

### 원문과 판단의 연결

`planner_roles.py`와 `plan_inspection.py`에 다음 일반 규칙을 공유한다.

- 각 AC×validation 행은 비어 있지 않은 AC `statement`와 `validation_intent`를 각각 인용하고 validation 전체 문장을 연결한다.
- 등록 도구·phase가 실제 수행하는 절차를 전체 문장과 대조한다. phase의 실제 범위, 별도 실행·기대값 비교와 단순 목적 표현을 구분한다.
- validation mechanism에서 사용한 정식 `project:` citation ID를 해당 validation의 AC 관계 행에도 연결한다. 같은 인용을 여러 행과 여러 mechanism에서 재사용할 수 있으며, 한 `basis_refs` 배열 내부의 중복은 거부한다.
- 독립 Goal 검사와 별도 Task 검사, 전역 의무의 출처와 중복 검사 ID 각각의 필수성, 선택적 AC 연결을 구분한다. 특정 phase 실행을 명시했다고 같은 목적을 가진 모든 별도 검사가 그 AC의 명시 절차가 되지는 않는다.

adapter는 selector·연속 quote·참조 ID·행 집합과 제출물 내부 일관성만 검증한다. AC ID별 의미 정답을 provider 지침에 넣거나 coverage·인용·finding을 자동 보정하지 않는다. citation의 최대 길이는 전체 validation 문장을 인용할 수 있도록 240자에서 5,000자로 늘렸다. Task validation의 최대 3,000자와 integration validation의 최대 5,000자를 수용하기 위한 변경이다.

`GoalContractRevision`, `PlanContractRevision`, `ReviewerSubmission`, DB schema, Core admission/score는 변경하지 않았다. 실제 수용된 nested `anyOf`와 두 정상 응답 조합인 `findings=[] + ratings 객체`, `findings 비어 있지 않음 + ratings=null`을 유지했다. `AGENTS.md`와 `docs/orchestration-redesign.md`도 같은 일반 규칙으로 정합화했다.

### 사례별 사전 기대표 결속

`plan_inspection_eval.py`의 새 사례 평가 경로는 사례 ID, 전체 Goal·Plan, Project Map, 등록 source 주소·본문·digest, validation projection과 전체 비교 대상 집합을 결속한다. Plan 전체에는 검사 statement·소유 Task/Goal·method·mode·evidence·현재 연결이 포함된다.

`r_s06_10_fixtures.py`는 독립 검토에 저장한 **전체 Goal·Plan의 사전 digest**와 실제 요청을 먼저 대조한다. 검사 문장·소유 단계·method·mode와 직접 인용도 확인한다. 원문 뒤에 새 명시 절차를 추가해 부분 인용은 여전히 유효한 경우도 전체 digest가 차단한다. 그 뒤에만 `r_s06_10.py`가 사례별 기대표를 실제 요청에 결속한다. 같은 ID의 `bad`, `wrong-goal`, `combined`에 clean 표를 재사용하던 경로는 제거했다.

모든 11개 고정 사례에 전용 관계표와 기존 결함별 직접 근거를 지정했다. 관계는 전수 평가하고, 독립 결함은 고정 evidence anchor로 대조한다. constraint×Task 전체 의미와 모든 mechanism 명칭·phase·별도 책임의 전체 의미를 자동 전수 검증했다고 주장하지 않는다. 필요한 표가 없거나 입력이 다르면 provider 효과 전에 중단한다.

생성물의 13번째 Reviewer는 12회 완료 및 정상 `GENERATION_REVIEW_REQUIRED` 상태, 모든 보존·결속 검사, 독립 정상성 대조, **생성 전용 관계표와 실제 생성 입력 digest**를 요구한다. clean 표를 적용하지 않는다. 실제 완료 receipt의 thread·turn·model·effort·inventory·권한·schema·request 결속도 다음 사례 전에 확인한다. 이 생성 경로는 결정적 회귀로 확인했으며 이번 실제 진단에서는 도달하지 않았다.

## 독립 기대표 revision

새 파일은 다음 두 개다. 기존 v2 기대표·독립 검토·원본·FAIL을 수정하지 않았다.

- `tests/fixtures/engine/plan-inspection-v3-expectations.json`
- `tests/fixtures/engine/plan-inspection-v3-independent-fixture-review.json`

독립 검토는 11개 실제 호출용 고정 case의 **252행**, 추가 7개 결정적 case를 포함한 **총 18개 case·444행**을 대조했다. 등록 자료와 case별 원문의 **직접 인용 277개**를 검증했다. 각 사례의 전체 Goal·Plan 사전 digest와 검사 scope 검토 기록을 남겼다. 메인이 모든 case의 인용·문장·집합·digest를 재검증했다.

| clean 행 | 원래 v2 | 독립 v3 | 직접 이유 |
|---|---|---|---|
| `ac_003 × val_goal_independent_behavior_contract` | explicit | explicit 유지 | AC의 새 프로세스 unittest와 실제 oracle goal phase 내부 unittest가 일치 |
| `ac_004 × val_task_scope_preservation` | optional | global | AC의 파일 검사는 독립 Goal 범위, 별도 Task 파일 검사 책임은 전역 제약에서 발생 |
| `ac_001/002/003 × val_task_scope_preservation` | optional | global | 전역 Task 파일 검사 의무의 출처를 보존하며 선택적 연결 허용 |
| `ac_002 × val_task_unittest` | optional | global | AC-002는 공개 호출 계약을 검사하는 oracle을 명시, 별도 Task unittest 책임은 전역 제약 |
| `ac_004 × val_task_unittest` | explicit | global | 같은 Task/Goal 경계의 일관성을 위해 추가 검토; 별도 Task unittest를 AC-004의 명시 절차로 확대하지 않음 |

지정된 여섯 행에 더해 같은 범위 문제의 일곱 번째 행을 검토했고, **기대값 변경은 총 여섯 행**이다. 새 모델 결과를 보기 전에 고정했다. v6 결과나 앞선 분석 결론을 자동 정답으로 채택하지 않았다.

직접 selector는 `source:goal`의 `/hard_acceptance/2/statement`, `/hard_acceptance/2/validation_intent`, `/hard_acceptance/3/statement`, `/hard_acceptance/3/validation_intent`, `/constraints/2/statement`와 `artifact:plan_contract`의 해당 `/definition/.../validations/.../statement`다. 등록 근거는 `project:entry_ad363844d3392d9ef718d2d6`, `/content`이며 본문 digest는 `sha256:c0c58dc3ed0ff48b9f6161d1013d8fb8972549d20378322843f7279033b01ed8`이다. 구현 `tests/fixtures/engine/bugfix-trace/oracle.py`의 phase 공통 `subprocess.run` unittest와 goal 전용 동작 분기도 직접 확인했다.

`tests/fixtures/engine/plan-inspection-raw-v6-rejected.json`에는 v6의 정확한 final response 문자열·파싱 값·request·기존 관계표·기존 FAIL 평가와 원본 artifact digest를 보존했다. 새 adapter에서 AC validation_intent 근거가 없는 원시 v6를 거부하며, rating이나 citation을 추가해 통과시키지 않는다.

## 결정적 검증과 실행 후 수정

두 source 모두 `compileall`, 전체 unittest, `pip check`, synthetic lifecycle, legacy freeze manifest의 Gate **5/5 PASS**다. 최종 전체 테스트는 **547개**다.

회귀는 두 AC 원문·전체 검사 문장·등록 citation 누락, 동일 인용의 여러 행·수단 재사용, 잘못된 주소·selector·digest·quote, 정상/결함 두 응답 조합과 금지 조합, 독립 복수 결함, 같은 ID의 statement·phase·method·mode·소유자·evidence·등록 본문 변경, 불완전 등록 자료, Goal suffix 추가, 표 누락 시 무호출, FAIL 생성 상태와 다른 thread receipt의 차단을 포함한다. 실제 oracle의 task·goal 모두가 fresh unittest를 실행하고 고정 동작 비교는 goal에서만 실행하는 것도 관측했다. 원래 결함 fixture를 수정하거나 성공으로 바꾸지 않았다. scripted 의미 표 대조는 실제 모델의 의미 검출 성능 증명이 아니다.

실행 중 추가 확인한 오류는 `mechanisms[*].basis_refs`를 합친 뒤 중복 제한을 적용하여 **서로 다른 수단이 같은 인용을 재사용하는 정상 구조**를 거부하는 문제였다. 잠긴 source는 실행 종료까지 유지했다. 이후 `plan_inspection.py`에서 중복 검사 단위를 각 배열로 한정하고 `test_engine_inspection_case_binding.py`에 양방향 회귀 하나를 추가했다. 이 두 파일만 실제 호출 source와 다르다.

실행 후 수정은 실제 FAIL의 원인이 아니며 등록 근거의 AC 연결 요구를 완화하지 않았다. 최종 source에서 실제 원문을 다시 구조 검사해도 같은 `대조표 AC 관계 등록 자료 인용 누락`으로 거부된다. 기대값·threshold·원문은 변경하지 않았고 추가 provider 호출은 0회다. 실행 당시 snapshot은 `executed-source/`에, 차이와 최종 source digest는 `post-diagnostic-source-manifest.json`에 보존했다. 따라서 현재 source에 원래 `verify_lock()`를 적용하면 source 변경을 거부하는 것이 정상이며, 원래 잠금을 재사용해 호출해서는 안 된다.

## 사전 잠금과 실제 첫 호출

진단 root: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-12-20260905-v1`

- source snapshot **97개**, 현재 잠금 입력 **196개**, 과거 보존 원본 **2,853개**.
- `prepare.log`는 최상위 출력 로그로 잠금에서 제외했다. 입력·schema·projection·기대표·완료한 결정적 결과는 잠갔다.
- preflight canonical lock: `sha256:d733e3108822f525699ca42b48d7dec8ec7c1edd6fe6df35b0d1dcdfb13e8f90`.
- 실제 inventory: `sha256:82e6bbcba85b38800c736c9f9809a493fbc9b3270cb53b514721a80839a49f14`.
- runtime은 실제 `:danger-full-access`, `approval_policy=never`를 확인했다. thread/start receipt의 `instructionSources` 순서·경로·본문 digest가 사전 결속과 일치했다.

주입된 AGENTS의 본문 digest는 전역 `sha256:6b1b3dbf5dc74c9b077f4c73099d4c54c1dc31b85376b19eafac7a5d5ead77ba`, 제품 `sha256:40cd9489efcd9e065c7e7df0a38334b5bdf102102aceb59ed4e16db736580e53`, 새 workspace `sha256:57fb50121f05548961e1e725e5537526b4e9cbc754383f8d42b0162096944b2a`다. 경로는 `instruction-binding.json`과 실제 thread receipt에 남겼다.

기존 순서는 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`다. general Reviewer는 `gpt-5.6-terra/high`, critical Reviewer는 `gpt-5.6-sol/xhigh`, expander는 `gpt-5.6-luna/high`로 기존 역할 설정을 유지했다.

첫 호출은 `compact_plan_reviewer`, `gpt-5.6-terra/high`다.

- call: `model_call_41cf04b401954914a861a568b3991d9f`
- thread: `01a06d7e-dfa5-76a2-be89-765d35a4a08b`
- turn: `01a06d7e-e2c3-7c33-ad6d-c4bdb439c4f7`
- request canonical digest: `sha256:00a38ee2dd7ad3d242bb1b55fde14b96314706d72b95e8ecd737adb60e0ddb56`
- receipt canonical digest: `sha256:c1b6db8efde1e457ef26166b19c4d10aac3f34605a09fdecb6331af94ab475a1`
- actual schema canonical digest: `sha256:4545af7f82b77bb699652e58995ffc64435ddad0a0fe82d5c26eacefc04627a7`

provider는 nested `anyOf` schema를 수용했고 응답은 `findings: []`와 다섯 rating 모두 4였다. Pydantic envelope 형식은 유효했지만 adapter가 등록 근거 연결 누락을 발견해 runner receipt가 `schema_failed`를 기록했다. **provider의 invalid schema 오류나 rating 조합 실패와는 구별되는 사후 구조 검증 실패**다. request·thread·turn·지침·schema·receipt와 실행 종료 당시 source·과거 원본·workspace 보존 확인은 모두 통과했다.

## 원시 결과의 사후 설명

원시 응답은 citation 21개, AC 행 28개, constraint 행 3개, validation 행 7개였다. AC validation_intent 인용은 v6의 0개에서 4개로 늘었다. 그러나 `reference_goal_evidence`가 다음 6개 AC 행에서 누락됐다.

- `ac_001/002/003 × val_goal_independent_behavior_contract`
- `ac_001/002/003 × val_goal_independent_scope_preservation`

공식 의미 평가에는 진입하지 않았다. 원문을 보정하지 않은 **설명용 사후 대조**에서는 새 사전 기대표와 다음 세 행이 달랐다.

| 행 | 사전 기대 | 실제 |
|---|---|---|
| `ac_003 × val_goal_independent_behavior_contract` | explicit | optional |
| `ac_003 × val_goal_independent_unittest` | explicit | optional |
| `ac_004 × val_task_unittest` | global | explicit |

이 대조는 구조 거부를 뒤집거나 호출 재개를 허용하지 않는다. 근거 파일은 `raw-clean-analysis.json`이다. 일부 인용 구조 변화만으로 전체 의미 성능이 개선됐다고 판단할 수 없다.

## 호출·비용과 v6 비교

| 항목 | v6 | 이번 제한 진단 |
|---|---:|---:|
| 논리 호출 / provider turn | 1 / 1 | 1 / 1 |
| 최대 논리 호출 / provider turn | 13 / 13 | 13 / 13 |
| schema recovery | 0 | 0 |
| input tokens | 42,715 | 43,511 |
| cached input tokens | 0 | 0 |
| output tokens | 7,031 | 7,622 |
| reasoning tokens, output에 포함 | 924 | 4,061 |
| total tokens | 49,746 | 51,133 |
| 역할 latency | 131.515초 | 142.047초 |
| provider duration | 131.055초 | 140.962초 |
| 청구 금액 | 미제공 | 미제공(null) |

usage는 새 빈 thread의 첫 turn에서 관측한 `thread/tokenUsage/updated.total`이다. token 종류를 중복 합산하지 않았다. 이번 총 token은 1,387개, 역할 latency는 10.532초 늘었으나 **같은 평가 계약의 성능 비교가 아니다**. 첫 요청 payload와 model/effort는 동일했지만 공유 지침·제품 AGENTS·quote schema·인용 연결 검증·기대표 revision·사례 결속 계약이 달라졌다. v6는 원래 관계표와 2건 불일치로 FAIL, 이번은 구조 거부가 공식 첫 실패이며 설명용 관계 불일치는 3건이다. 이를 정확도 추세로 환산하지 않는다.

## 증거 digest

다음은 위 진단 root에 대한 상대 경로이며 digest는 **파일 bytes** 기준이다. canonical digest와 구분한다.

| 파일 | SHA-256 |
|---|---|
| `summary.json` | `b0316724b531296263ef52a3f8ff19257acf999750424670332970263e20ab7f` |
| `preflight.json` | `e58c78c849b7e71e7dbeec01c6db09f36d5dfebbb8f16152ccd1452e94b6cdfd` |
| `executed-source-manifest.json` | `9459d939d639fa2ba52db2cb32a668f9390b446268aea646dbb0e6b17dd60afc` |
| `post-diagnostic-source-manifest.json` | `1d91f2c064fb9c63bea769801772099e73d34d0693dd379294bab0d8cb540adf` |
| `deterministic/qualification-report.json` | `6c05f0b7a787993daddef1319db2e09d301c730e5c969bb0da2ea1e67bd2733a` |
| `post-diagnostic-deterministic/qualification-report.json` | `3a4e272b772ba10df79d29ccec8c474718fc9d78d4fb356614c3debf5ad63213` |
| `expectations.json` | `f1cdbd5e5c3567e6044b9d0f28b2850703aa36c3e4a53dc7a12235effd1d9174` |
| `independent-fixture-review.json` | `1317be7c4bd053e448e1d2050525bcd96d26e226bb1d7a94eafabe99cc231d7e` |
| `raw-clean-analysis.json` | `af0fb15fe4ed470e087024e252a3b84a005b802606f9ac600a3446fd4b0f13e7` |
| `calls/01-compact_plan_reviewer/request.json` | `e073be43350a2b76023e1af3287164758325594cc10b9efa48dbdb6bb61fb14e` |
| `calls/01-compact_plan_reviewer/strict-schema.json` | `f11cec6fac6292d47978e7f152105f69afe326a6b6975d06a2d3078d6e2010ac` |
| `calls/01-compact_plan_reviewer/thread.receipt.json` | `e84990a75806a5abf79ba4449f1a23f798be2caa4a2f9a158c85e8b34c90e1df` |
| `calls/01-compact_plan_reviewer/turn.receipt.json` | `217189854aa9c83a4a78bca26607a74698b26c7364726c0a9ed17635866b8105` |
| `calls/01-compact_plan_reviewer/terminal.json` | `e491ff614e118d3aed415ed908c2ddb05b3bc832fd300ff9cde33d9ece34130a` |
| `calls/01-compact_plan_reviewer/failed.json` | `b985a2a8763b00f0241f5de5dc22a3b85f4ceb7c4585d09bf85d3aaf0988dc78` |

## 미실행과 다음 작업 조건

`bad`부터 `expanded-review`까지 남은 12회, 생성 결과의 독립 정상성 대조·실제 Reviewer, 전체 S06, Plan 활성화, Worker 실행, 전체 qualification·1.0 판정은 미실행이다. 운영 SQLite나 운영 파일 원장은 만들지 않았다. 기존 R1~R3.1 source·artifact와 v6 원문·기대값·FAIL은 보존했다.

다음 실제 진단에는 이번 원문에서 확인한 등록 근거 연결 누락과 세 관계 분류의 직접 원인을 검토한 변경이 필요하다. 근거 연결 지침만으로 의미 문제가 해소됐다고 가정해서는 안 된다. 새 source의 결정적 Gate, 새 독립 검토·사례표·지침/schema/inventory 결속과 **새 run root**가 필요하며 이번 FAIL의 남은 호출을 이어 실행하면 안 된다. 최종 source의 인용 재사용 수정도 새 실제 검증 범위에 포함해야 한다. 기대값·threshold를 이번 결과에 맞춰 조정하지 않는다.

이 보고서는 한 항목의 구현과 제한 진단 인계이며 전체 qualification 또는 `flowmarshal` 1.0 승인 자료가 아니다.

## R-S06-13 구현·제한 진단 후속 기록

### 구현 결과

R-S06-13은 검사 수단을 먼저 확정하도록 공유 지침과 provider schema 순서를 조정했다. 판단 순서는 **validation 전체 책임·실제 phase → 전역 Task 검사 의무 → AC statement·validation_intent 적용 범위 → 현재 연결·finding**이다. AC가 절차를 직접 요구한 경우와 특정 도구·phase 실행이 그 절차를 실제 포함하는 경우를 구분하며, 같은 목적의 별도 검사·다른 phase까지 확대하지 않는다.

`PlanInspection`과 Plan provider envelope는 기존 필드와 relation enum을 유지한다. 다만 `validation_rows` 및 `inspection`을 관계 행보다 먼저 내보내어 mechanism 근거를 먼저 작성하게 한다. 등록 `project:` citation을 mechanism 범위 판단에 썼으면, 제출 전 같은 validation의 모든 AC 행 `basis_refs`에 그 citation 집합이 포함됐는지 제출자가 확인한다. adapter는 selector·quote·참조 집합과 내부 일관성만 확인하며 관계·citation·coverage를 생성·보정하지 않는다.

원시 v1은 다음의 새 fixture로 바이트 보존했다.

- `tests/fixtures/engine/r-s06-12-raw-rejected/manifest.json`
- request `sha256:e073be43350a2b76023e1af3287164758325594cc10b9efa48dbdb6bb61fb14e`
- terminal `sha256:e491ff614e118d3aed415ed908c2ddb05b3bc832fd300ff9cde33d9ece34130a`
- summary `sha256:b0316724b531296263ef52a3f8ff19257acf999750424670332970263e20ab7f`

원시 final response는 보정하지 않았고, `reference_goal_evidence`가 `ac_001/002/003 × val_goal_independent_behavior_contract/val_goal_independent_scope_preservation`의 6행에서 빠진 것을 회귀로 고정했다. 독립 정상 합성 fixture는 `tests/fixtures/engine/plan-inspection-r-s06-13-synthetic-normal.json`(bytes SHA-256 `9a13d34bb921d20e87b624d56b8470ea741e371cd4a749090445b2008d751296`)이다. 기존 사례별 기대값을 유지한 v4 기대표·독립 검토는 각각 `f132337b8b237e2379c6315a3b12ff18bfd5d24f8d62be6e29608b8bf963b011`, `7b664981550bd50d53f19345052da0f92410e44f44458a9eb55fb3b291a3bd2d`로 provenance와 직접 selector를 결속한다.

추가 회귀는 원시 보존·행별 citation 누락, 합성 정상 응답, mechanism 우선 schema 순서와 enum 보존, v4 provenance/direct selector, 다중 mechanism의 citation 재사용과 배열 내부 중복 거부를 확인한다. 기존 관계 전수·선택적 연결·AC-003 복합 fresh unittest·AC-004의 Goal/Task phase 경계·finding/receipt 결속 회귀는 유지한다.

### 검증 및 제한 실제 진단 판정

`compileall`, `pip check`, 전체 unittest는 각각 통과했으며 전체 unittest는 **551개 PASS**였다. `compileall`이 합성 fixture에 만든 `__pycache__`는 실제 소스가 아닌 cache 오염이어서 확인 후 workspace 밖 recoverable 임시 위치로 옮긴 뒤 다시 검증했다.

새 미사용 run root `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-13-20260905-v1`에서 source manifest `sha256:de6f4c86e940629befef71d4c1fe1f4e004672d3d14a108d5258145fbc296890`에 결속한 결정적 Gate 5개를 실행했다. 결과는 **4/5 PASS, FAIL**이다. `full-test-suite` cell에서 R-S06-13과 무관한 `test_gate0c_e2e.Gate0CE2ETests.test_retest_ledger_is_new_append_only_record`가 `sqlite3.IntegrityError: UNIQUE constraint failed: task_events.row_digest`로 실패했다. 실제 cell과 report는 다음에 보존한다.

- `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-13-20260905-v1\qualification-report.json` (report digest `sha256:b5e4afd12348f620c41d7a94941b2a7fb491139ba4edf335145c448307089f63`)
- `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-13-20260905-v1\cells\seed-0\7d4dcc7b1f24fd4291a0fa867a6dcb147c19f5c33ce41e8e73cef0f4b3930dff.json`

따라서 첫 결정적 Gate 실패에서 즉시 중단했다. `prepare`, 모델 호출, provider turn, receipt는 **새 run에서 모두 0회**이며 기존 v1의 1회 결과를 재사용하지 않았다. 이 구현이 모델의 관계 의미 정확도나 token/latency를 개선했다고 주장할 실제 결과는 없다. R-S06-12의 관측 비용(51,133 tokens, 142.047초)은 과거 FAIL 관측으로만 유지한다.

이 항목은 제한 진단 구현이며 전체 S06, Plan 활성화, Worker 실행, 운영 SQLite 원장, 전체 qualification 및 1.0 cutover는 모두 미실행/NO-GO다. 다음 실제 호출의 전제는 Gate0C의 append-only row digest 충돌을 별도 범위에서 해결·검증하고, 변경 source와 새 run root에서 5/5 Gate를 통과시키며 새 입력·기대표·지침·schema·inventory·역할 설정을 다시 잠그는 것이다. 실패한 `r-s06-13-20260905-v1` run은 재사용하거나 이어 실행하지 않는다.
