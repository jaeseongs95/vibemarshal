# R-S06-32 Sol/xhigh 제한 실제 재검증 인계

이번 경계는 **FAIL**이다. 새 deterministic Gate **5/5 PASS** 뒤 고정 순서로 실행했으며, `clean`과 `bad`는 PASS, 세 번째 `wrong-goal`은 고정 의미 기대값과 AC 관계 2개가 달라 FAIL했다. 첫 실패에서 중단했고 이후 10사례는 NOT_RUN이다. **PASS 2 / FAIL 1 / NOT_RUN 10**, logical/provider/recovery는 **3/3/0**이다.

구조·참조·receipt 검증을 통과한 응답은 3개지만, 이를 의미 검증 3 PASS로 계산하지 않는다. 모든 cutover Gate가 통과하지 않았으므로 **Functional Alpha·1.0 NO-GO**를 유지한다. 전체 S06·qualification은 이번 경계에서 NOT_RUN이다.

## 정책과 기준

첫 파일 조회 전에 이 turn 개발자 `<permissions instructions>`에 실제 제공된 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인했다. 승인 질문이나 권한 상승 요청 없이 실행했다. 전역 `C:\Users\sjs95\.codex\AGENTS.md`, 역할 cwd의 상위 적용 경로, `D:\codex\flowmarshal\AGENTS.md`, docs·scripts/diagnostics·evaluation 경로의 지침과 override 여부를 확인했다. 설정에 별도 대체 지침 파일 목록은 없었다. fixture workspace의 적용 지침도 읽었다.

권위 origin은 비공개 `https://github.com/jaeseongs95/vibemarshal.git`이며 `gh repo view`에서 `isPrivate=true`를 확인했다. 시작 직전 및 실행 전후 로컬 HEAD·origin/main·원격 main은 모두 아래 기준과 일치했고 main/clean이었다.

- 기준 HEAD: `fd09bf1f66e947f2db55ae4a706ca08f794f15d9`
- 새 root: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v2`
- 직전 인계: [R-S06-31](r-s06-31-sol-xhigh-actual-validation-handoff.md). 해당 실패 root나 R29/R30 결과를 resume하거나 성공 근거로 사용하지 않았다.
- 동일 fixture builder와 검증된 R31 실행·감사 helper를 사용했다. helper의 새 root·배정 식별자·기준 HEAD를 맞추고, 로컬 wrapper에 HEAD·origin·원격 main·clean·시작 tracked/source/기존 artifact·helper·지침 bytes 검사를 추가했다. 원본 harness의 입력·prompt/schema·assessment·첫 실패 중단 동작은 그대로다.
- `boundary_guard.py`와 `guarded_harness.py`는 Git 제외 새 root의 실행 helper다. 각 단계 및 원본 `verify_lock`의 호출 전후에 고정 기준을 재검사한다. 입력·HEAD 불일치에 대한 완화·재준비·추가 root 생성은 없었다.
- 제품 source, 기존 raw/frozen artifact, prompt/schema 의미, fixture/oracle/expectation/threshold/taxonomy는 변경하지 않았다. 과거 fixture builder의 역사적 입력 provenance만 유지했다.

## 새 계약과 호출 전 감사

역할 설정은 절대 경로 `D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-xhigh-roles.json`으로 prepare에 주입했다. 실제 호출한 general Reviewer는 `gpt-5.6-sol/xhigh`, fallback `[]`다. 다른 역할 설정은 유지했다. Plan expander의 기존 `gpt-5.6-luna/high`는 실행되지 않았다. 모델 비교·fallback·schema recovery·재시도는 없었다.

fresh App Server `model/list` 원문과 선택 조합 지원, executable 및 실제 정책을 새 model-lock-v2에 결속했다. 원문 bytes·canonical·typed 역할 설정, source snapshot, prompt·strict schema, 사례별 Goal·Plan·검사 phase/method/mode·등록 자료 digest·고정 기대표·독립 fixture review를 호출 전에 검증했다. 12개의 사전 request artifact는 준비용이며 실제 provider 호출 횟수가 아니다.

| 결속 | digest |
|---|---|
| source_manifest_digest | sha256:34c275c7aa39dbfacc611d3d67f7c319232fe9546d0ac8bac8c7d7b8af300af6 |
| lock_digest | sha256:2e2d08dc6e01cc2e6977ba40851b5b2e5b15714bb52706bbae0fa3a2222bdeed |
| inventory_digest | sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2 |
| model_lock_digest | sha256:9cb6e554b5ffbab137cbfe643f8e3d67c0b81f3963dd26fcf33f08d1cde5a005 |
| prompt_digest | sha256:d54e0f443a9d38f15e7499457c3b0647c8ab1e4cbfc9da71834f73edbac1db89 |
| output_schema_digest | sha256:6b1e34900c2ce8141b040eaf1366592d21abd5cdba16ca758de7cc7d2ea5055f |
| harness_digest | sha256:18b4805bc74830412211486cabfae903be78dfe66cdd058f9e8a32789ac802cb |
| codex_bin_digest | sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d |
| deterministic_report_digest | sha256:302b61ffa3f84a067d822482b14a36f397aa1a4b7fbb27dcd65d52326123d1dc |
| role_configuration_digest | sha256:0ca70f5002e0254e36f2fe7709080e2a15977cfa75c355cdc48c08f8222a1418 |
| role_source_bytes_digest | sha256:1e6a41c85c50a3804eda4a45395c0ea6eb49c4f2ecd721acfe2cb6ebee5f6ba9 |
| role_source_canonical_digest | sha256:91d94b1f0cb40598ca3f5a5eb3f019c94e28a3c35d88252e2a31f3b6a3faad9c |

자동 주입 지침은 다음 경로·본문을 잠갔고, 실제 3개 thread/start receipt의 `instructionSources`와 turn 시작 전에 대조했다.

| 경로 | bytes digest |
|---|---|
| `C:\Users\sjs95\.codex\AGENTS.md` | sha256:2c113a26bd82cd1964a444ae53c99c3b4d45a2d42faec1262d0ade358c9751dc |
| `D:\codex\flowmarshal\AGENTS.md` | sha256:9d94703fc9240f47dcd562568a8c9218681e5c6af8bc535817ad62e3f4004f4e |
| `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v2\workspace\AGENTS.md` | sha256:57fb50121f05548961e1e725e5537526b4e9cbc754383f8d42b0162096944b2a |

## 실행과 사례별 결과

| Gate | 결과 |
|---|---|
| legacy-freeze-manifest | PASS |
| compileall | PASS |
| pip-check | PASS |
| synthetic-lifecycle | PASS |
| full-test-suite | PASS |

full-test-suite 원문: `Ran 615 tests in 71.882s`, `OK`. 각 Gate의 계약·cell·완료 receipt·source/environment 결속을 독립 확인했다.

| 실행 단계 | 횟수 | 프로세스 경과 초 | 종료 코드 |
|---|---:|---:|---:|
| gate | 1 | 77.406 | 0 |
| prepare | 1 | 23.625 | 0 |
| run | 1 | 794.015 | 0 |
| pre_audit | 1 | 9.531 | 0 |
| post_audit | 1 | 별도 합산하지 않음 | 0 |

run 프로세스 종료 코드 0은 결과 기록의 정상 종료를 뜻한다. 평가 결과의 권위는 `summary.json`의 `status=FAIL`과 사례별 assessment다. 각 명령 argv·stdout/stderr·환경 전후 digest는 `runtime-preflight/session-audit/*.started.json`, `*.completed.json`, 로그에 보존했다.

| 순서 | 사례 | 결과 | 구조·참조·output 결속 | 고정 AC 관계 일치 |
|---:|---|---|---|---|
| 1 | clean | PASS | PASS | 28/28 |
| 2 | bad | PASS | PASS | 28/28 |
| 3 | wrong-goal | FAIL | PASS | 26/28 |
| 4 | combined | NOT_RUN | NOT_RUN | NOT_RUN |
| 5 | boundary-clean | NOT_RUN | NOT_RUN | NOT_RUN |
| 6 | missing-link | NOT_RUN | NOT_RUN | NOT_RUN |
| 7 | future-result | NOT_RUN | NOT_RUN | NOT_RUN |
| 8 | stored-expanded | NOT_RUN | NOT_RUN | NOT_RUN |
| 9 | semantic-explicit | NOT_RUN | NOT_RUN | NOT_RUN |
| 10 | stored-multi-defect | NOT_RUN | NOT_RUN | NOT_RUN |
| 11 | semantic-missing-link | NOT_RUN | NOT_RUN | NOT_RUN |
| 12 | expansion | NOT_RUN | NOT_RUN | NOT_RUN |
| 13 | expanded-review | NOT_RUN | NOT_RUN | NOT_RUN |

실행 3사례의 AC 관계 합계는 **82/84 일치**다. 합계를 합격률이나 전체 qualification PASS로 승격하지 않는다. 3사례 모두 고정 결함 탐지와 precision 검사는 통과했으며, `wrong-goal`의 AC 관계 판정이 실패했다.

각 실제 호출의 request·strict schema·prompt/developer instruction·receipt·terminal·thread/turn·inventory/config·output digest 및 token 귀속을 검증했다. 완료 후 저장 원문으로 binding과 고정 assessment를 다시 계산하여 보존 결과와 일치함을 확인했다. `external_unknown=0`, `incomplete=0`이다.

## 첫 실패의 직접 근거

첫 오류는 **`RuntimeError: SEMANTIC_ASSESSMENT_FAILED: wrong-goal`**이다. 환경 기준 불일치나 구조·참조 실패가 아니다.

| criterion | validation | 사전 기대값 | 실제 ac_link_required |
|---|---|---|---|
| ac_004 | val_task_scope_preservation | `false` | `true` |
| ac_004 | val_task_unittest | `false` | `true` |

AC-004의 statement는 Task 검증과 별도로 실행하는 Goal Test와 독립 evidence를 요구한다. validation_intent의 task/goal phase 구분은 oracle 절차를 식별한다. 별도 Task unittest·파일 scope 검사의 의무는 `constraint_003`에 보존되어 있으나, 고정 기대표는 이를 AC-004의 필수 연결로 확대하지 않는다. 실제 Plan에는 두 연결이 이미 존재한다. `ac_link_required=false`는 기존 연결의 금지를 뜻하지 않으므로 실제 연결 존재를 기대값 true로 바꾸지 않았다.

Reviewer는 요구된 `goal-uses-task-phase` 결함을 `GOAL_ORACLE_PHASE_SCOPE_MISMATCH` finding으로 탐지했고, missing/unexpected finding은 없었다. 그러나 위 두 행에 true를 제출하여 complete AC matrix 검사에서 탈락했다. 원본 finding·boolean·인용·기대값·threshold를 보정하지 않았다.

직접 주소는 `requests/wrong-goal.json`의 `/payload/evidence_catalog/source:goal/hard_acceptance/3`, `/payload/evidence_catalog/source:goal/constraints/2`, `case-expectations/wrong-goal.json`, `calls/03-compact_plan_reviewer/result.json`, `wrong-goal-assessment.json`이다. `post-verification.json`의 differences에 원본 행·기대 행·실제 Plan 연결과 bytes digest를 함께 보존했다.

expansion과 expanded-review는 NOT_RUN이다. 생성 검토 template의 독립 정상성 기준은 최초 prepare에서 선결속했지만, 생성 Plan·그 Plan의 전용 기대표·독립 review는 생성 단계 미도달로 수행하지 않았다. 기존 clean 표나 다른 사례의 표로 대체하지 않았다.

## 실측 사용량과 thread/turn

상한은 logical/provider/recovery **13/13/0**, 실제는 **3/3/0**이다. 미실행 사례의 token·latency는 `null`이며 이유는 첫 실패 뒤 NOT_RUN이다. 실행 3건은 빈 새 thread의 첫 turn이 확인됐고 `thread/tokenUsage/updated`의 thread scope 원문을 그대로 보존하여 단일 turn에 귀속했다.

| 사례 | input | cached input | output | reasoning (output에 포함) | total | receipt latency ms | provider duration ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| clean | 48863 | 0 | 16402 | 9765 | 65265 | 243406 | 234039 |
| bad | 48881 | 0 | 17851 | 11315 | 66732 | 254578 | 245499 |
| wrong-goal | 48863 | 28032 | 17089 | 11010 | 65952 | 249031 | 240320 |
| 합계 | 146607 | 28032 | 51342 | 32090 | 197949 | 747015 | 719858 |

cached input은 input의 부분집합이고 reasoning은 output에 포함되므로 중복 합산하지 않는다. receipt latency와 terminal의 provider duration은 서로 다른 관측값이다. 청구 금액은 provider receipt가 제공하지 않아 `null`이며, 구독 한도 차감량·추정 비용으로 대체하지 않았다.

| 사례 | thread | turn |
|---|---|---|
| clean | 01a071d0-9d6b-75e3-8e7b-03728046a27f | 01a071d0-c187-7860-97d7-bc6b62311196 |
| bad | 01a071d4-7755-7e90-8e17-dee7e963e7b6 | 01a071d4-99ee-7b52-ace9-3ee11675eb53 |
| wrong-goal | 01a071d8-7b20-7242-8ea4-a5172ace80d8 | 01a071d8-9caa-7d72-9653-c7786ef6808d |

## Artifact와 보존

모든 raw artifact는 Git 제외 로컬 root `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v2`에 보존한다. 전체 실행 artifact의 bytes digest 목록은 `runtime-preflight/session-audit/boundary-evidence-manifest.json`에 있다. 아래는 핵심 artifact의 bytes digest다.

| artifact (새 root 기준) | bytes digest |
|---|---|
| assignment.json | sha256:2b8f4b8726e023ad9e98af61990058e045244c532111a79d9f0188596cbb70b3 |
| preflight.json | sha256:2f31e82d9aeb483fdb1d5326d70271b656407a350933a88f90983441a5d2836d |
| planning-binding.json | sha256:317136bce250678e08f77a22be5c8850a5616ee8e1189f1b748e70c421282761 |
| expectations.json | sha256:3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e |
| independent-fixture-review.json | sha256:9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27 |
| instruction-binding.json | sha256:02b51239f2701e3d8ef51b0d33177ac9db333f7d6af0047e55074539db1d264c |
| deterministic-environment-binding.json | sha256:c952702abec5a6d3fae6cdff426cc151591393d938707890cc2dfcc792c81082 |
| summary.json | sha256:b61ba05b698f3deda004c45cb33b83b4d96f398e3e9b12b6dd4309d190038fe6 |
| clean-assessment.json | sha256:dc0497ddd0a6d4c539e5920bf87b24436166de553036b445dc4e6f4bda7f1f28 |
| bad-assessment.json | sha256:950bda7b66dae6d7c4fc6bdd4558e0f74d8777df7ce39728969edfbb43b04030 |
| wrong-goal-assessment.json | sha256:8707bc983467e5e92db2ce2b6733f50713a12934c85e5186ff635028e66cb763 |
| calls/03-compact_plan_reviewer/request.json | sha256:8603913ec5a6a6ac60faf1305dd36503513e65c7fa22d06d1fc5e776a6a636cd |
| calls/03-compact_plan_reviewer/result.json | sha256:447f82da521ec903ba2b8a41c2e71c8335cb524e2de2d8c9f173b9bee1ae3cb5 |
| calls/03-compact_plan_reviewer/terminal.json | sha256:65731ae6ac92950a70fc3fd82b87b05068a1c9bacf64ae822f3ebe2b8301e931 |
| runtime-preflight/session-audit/helper-input-binding.json | sha256:21f38b437e5ffe4cf8c97c06effb9def6a51a8d5f37e09b14861a5c0aa1b7c34 |
| runtime-preflight/session-audit/pre-verification.json | sha256:af2d0c8d756101768050a72fbbba3d9e8d32bba205383128ac149a0aa917f87b |
| runtime-preflight/session-audit/post-verification.json | sha256:62c17e0e85dc525c8ba5b18aa4d58f058f15ca839e90f82e70f956206df44eb5 |
| runtime-preflight/session-audit/execution-closure.json | sha256:10102aedf588b700a4c5bf9037c20e353ebaf027fe4728f9fe73c92e52ea1e22 |
| runtime-preflight/session-audit/boundary-evidence-manifest.json | sha256:db9a4e4d381a314d9ee4bf7abb5fd986c9678e645ff299855953c2c0abc870d9 |

문서 작성 전에 source 236파일, 시작 tracked 3440파일, 기존 보존 artifact 9130파일의 bytes 불변을 확인했다. fresh Gate의 legacy freeze manifest도 PASS다. 실행 중 HEAD·계약 입력의 변경은 관측되지 않았다.

이번 tracked 변경은 이 인계와 [문서 지도](README.md)뿐이며 **제품 source 변경은 없다**. 관련 Gate·실행 결과·문서 링크·digest·`git diff --check`·변경 범위를 검증한 뒤 한 개의 한국어 commit으로 비공개 origin main에 fast-forward push한다. 실제 commit/push와 HEAD=origin/main=원격 main·clean 확인은 자기참조를 피하여 별도 `runtime-preflight/session-audit/delivery.json` 및 최종 응답에 기록한다.

R-S06-32 한 경계에서 종료한다. 다음 작업·다른 Codex 작업에 대한 메시지·callback·예약·완료 후 대기 루프는 수행하지 않는다.
