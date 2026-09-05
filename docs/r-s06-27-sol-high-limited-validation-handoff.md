# R-S06-27 Sol/high 후보 제한 실제 역할 검증 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 결과

**FAIL — 첫 clean에서 scope와 mechanism의 근거 집합 결속이 깨져 `schema_failed`로 중단했다.** logical/provider/recovery는 **1/1/0**다. 성공 result와 의미 assessment는 생성되지 않았고 후속 12사례는 NOT_RUN이다.

`prepare --role-config`와 `run`을 각각 정확히 한 번 실행했다. 각 호출의 성공 receipt·완료 terminal·result 및 고정 의미 assessment가 통과해야 다음 사례로 진행하는 기존 진단기를 사용했다. 첫 실패에서 중단하고 재호출·resume·자동 fallback·모델 승격·새 root 우회를 하지 않았다. 종료 코드 0은 의미 평가 PASS로 해석하지 않았다.

최종 summary는 첫 실패 뒤 한 번 게시했다. `status=FAIL`, `diagnostic_errors` 0개, summary checks는 모두 true다. outcome 집계는 `{"external_unknown": 0, "failure": 1, "incomplete": 0, "success": 0}`다. 성공 result 없는 확정 실패이며 의미 평가를 통과한 결과는 없다.

이번 제한 역할 검증의 실행·provenance 인계를 완료했다. **기존 S06 FAIL, Functional Alpha 미완료, FlowMarshal 1.0 NO-GO**를 유지한다. 전체 pipeline·실제 프로젝트 E2E·비용 Gate 완료로 확대하지 않는다.

## 실제 권한·시작 기준·지침

첫 파일 조회와 명령 실행 전에 이 turn의 개발자 `<permissions instructions>`에서 `sandbox_mode=danger-full-access`, `approval_policy=never`를 직접 확인해 첫 응답에 기록했다. 부모의 기대값을 관측값으로 대신하지 않았다. prepare와 실제 역할의 권한도 `:danger-full-access/never`로 결속됐다. 승인 질문·권한 상승 요청은 없었다.

- 시작 `HEAD=main=origin/main=a9155c55a1777eea47d66e3513129ceb2a644787`, 작업 트리 clean. 실제 `git ls-remote origin refs/heads/main`도 일치했다.
- origin은 `https://github.com/jaeseongs95/flowmarshal.git`, `gh repo view`의 `isPrivate=true`를 확인했다.
- 전역 `C:\Users\sjs95\.codex\AGENTS.md`, 프로젝트 루트 `AGENTS.md`를 적용했다. 상위 경로·docs·scripts/diagnostics·fixture 경로·run 상위에는 추가 AGENTS/override가 없었고 config의 fallback filename 지정도 없었다.
- [역할 설정 주입 경계](r-s06-role-configuration-boundary-handoff.md), [R26 인계](r-s06-26-evidence-order-limited-validation-handoff.md), [근거 우선 schema 보정](r-s06-evidence-first-schema-order-fix-handoff.md), [R19 CLOSE](r-s06-19-close-handoff.md), 권위 설계 §6.1·§7.1, cutover ADR·legacy 동결 기준을 대조했다.
- 과거 인계의 별도 사용자 판단 문구에는 이번 명시적 실행 승인과 1.0까지의 자율 진행 지시를 우선 적용했다. 이번 실행·인계·승인된 private main push에 추가 사용자 판단은 필요하지 않았다.
- 다른 작업으로 메시지·callback·예약을 보내지 않았고 새 Codex 작업·서브에이전트를 생성하지 않았다. 제품 진단기의 승인된 역할 thread만 생성했다.

## 새 root·역할·사전 계약

새 root: `.flowmarshal-engine-eval/runs/r-s06-19-sol-high-r27-20260905-v1`. 생성 전 부재를 확인했다. 기존 CLI의 허용 prefix `r-s06-19-*`를 쓰되 새 `assignment.json`에 **R-S06-27**을 명시했다. 내부 `session=R-S06-19` 식별자는 source 수정 없이 유지했다. 과거 실행을 재개한 것이 아니다.

역할 설정은 prepare에 절대 경로 `D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-high-roles.json`를 명시 전달했다. 선택 이유는 승인된 general Reviewer Sol/high 후보의 새 제한 실제 검증이며, 원문 bytes·canonical·typed digest와 경로·선택 이유·snapshot을 preflight 및 planning binding에 고정했다. 입력 결속의 선택 사유 식별자는 `caller_provided_explicit_role_configuration`다.

| 역할 | 모델/effort | 순서 있는 fallback | logical/provider/recovery |
|---|---|---|---|
| normalizer | `gpt-5.6-luna/high` | `[]` | 0/0/0 |
| skeleton_generator | `gpt-5.6-luna/high` | `[]` | 0/0/0 |
| plan_expander | `gpt-5.6-luna/high` | `[]` | 0/0/0 |
| general_reviewer | `gpt-5.6-sol/high` | `[]` | 1/1/0 |
| critical_reviewer | `gpt-5.6-sol/xhigh` | `[]` | 0/0/0 |
| executor | `gpt-5.6-terra/high` | `[]` | 0/0/0 |
| validator | `gpt-5.6-sol/xhigh` | `[]` | 0/0/0 |

모든 선택·fallback 지원과 필요한 runtime capability는 prepare 및 실제 호출 직전 fresh App Server `model/list` 원본과 `flowmarshal-model-lock-v2`로 확인했다. 기본 제품 설정과 이 세션의 Astra 배정은 제품 역할 binding에 적용하지 않았다.

새 preflight는 기준 commit·source snapshot·실행 harness·prompt·strict schema·주입 지침·고정 fixture/expectation/독립 review·모든 역할·호출 순서·상한 13/13/0을 잠갔다. 준비 요청 12개의 request/schema/model 결속과 정적 11사례의 입력별 기대표를 직접 재검증했다. 새 planning binding은 평가 계약 자료이며 전체 planning campaign 실행 결과는 아니다.

자동 주입 지침 3개의 현재 본문과 경로를 snapshot으로 결속하고 실제 thread/start `instructionSources` 및 turn 직전 본문과 대조했다. transport schema는 `citations → validation_rows → validation_scope_rows → ac_validation_rows`, AC 행은 `criterion_id → validation_id → basis_refs → scope_ids → ac_link_required → finding_codes` 순서임을 직접 확인했다.

과거 입력은 기존 fixture builder의 provenance와 고정 입력 출처로만 읽었다. R25/R26 등 과거 실제 thread·turn·receipt·summary·raw·checkpoint는 새 실행 성공 evidence로 재사용하거나 재개하지 않았다. 기대값·oracle·threshold·taxonomy·제품 source는 변경하지 않았다.

## 지정 결정론 Gate의 직접 재검증과 재사용

지정 `.flowmarshal-engine-eval/runs/role-config-boundary-dev-20260905-v1`의 전체 evaluation contract가 현재 `_deterministic_contract()`와 같았다. source 232파일, report·run-state와 5개 완료 cell의 fixture·order seed·계약 digest·local 완료 receipt·PASS 및 실행 stdout/stderr digest를 직접 검증했다.

Gate 전후 snapshot과 현재 Python 3.12.14·Windows platform·venv 및 base interpreter bytes·설치 package 목록·기록된 Python 환경 변수 전체가 동일했다. 명령에는 `-X utf8 -B`, 환경에는 `PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`을 동일하게 적용했다. 동일성을 입증했으므로 기존 성공 Gate를 새 root에 바이트 그대로 복사해 잠그고 중복 실행하지 않았다. 이는 과거 실패 역할 checkpoint 재사용과 별개의 승인된 결정론 Gate 재사용이다.

Gate 결과: **5/5 PASS** — compileall, 전체 unittest **596 tests OK**(65.792초), pip check, synthetic lifecycle, legacy freeze **40파일 PASS**. 원본 Gate 경과는 69.063초이며 이번 역할 실행 시간에 더하지 않는다.

Gate contract `sha256:800b0e5a8b9f3f3bdbac8e7d7eeb7f26c84fb9fb46ecfda7b015b9aa70c2e85a`, report `sha256:e10f684323682d2ab5c6f921fd3f44e8cf60367b27f13f874e991a2eb9ea190f`, 현재 환경 canonical digest `sha256:2799e6c8e2ef136e4a589a63b4ce3978b938777fc97cd309299e1237c4370995`.

| 단계 | 횟수 | 시작~종료 UTC | 경과(초) | exit |
|---|---:|---|---:|---:|
| prepare | 1 | 2026-09-05T08:24:08.020553+00:00 ~ 2026-09-05T08:24:13.837344+00:00 | 5.688 | 0 |
| run | 1 | 2026-09-05T08:24:29.427231+00:00 ~ 2026-09-05T08:27:46.896139+00:00 | 197.344 | 0 |

실제 명령은 다음과 같으며 전체 argv·시각·exit·환경·stdout/stderr digest는 `runtime-preflight/session-audit/`에 보존했다.

```text
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 prepare --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-high-r27-20260905-v1 --role-config D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-high-roles.json
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 run --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-high-r27-20260905-v1
```

## 사례 결과·원본 결속

| 순번 | 사례 | 결과 |
|---:|---|---|
| 1 | clean | 공통 결속 PASS / schema_failed / 의미 assessment NOT_RUN |
| 2 | bad | NOT_RUN |
| 3 | wrong-goal | NOT_RUN |
| 4 | combined | NOT_RUN |
| 5 | boundary-clean | NOT_RUN |
| 6 | missing-link | NOT_RUN |
| 7 | future-result | NOT_RUN |
| 8 | stored-expanded | NOT_RUN |
| 9 | semantic-explicit | NOT_RUN |
| 10 | stored-multi-defect | NOT_RUN |
| 11 | semantic-missing-link | NOT_RUN |
| 12 | expansion | NOT_RUN |
| 13 | expanded-review | NOT_RUN |

실제 receipt 식별자는 다음과 같다.

| call / 역할 | 모델 | thread | turn |
|---|---|---|---|
| `model_call_8ac2b40e797a418783c300f15b22c450` / compact_plan_reviewer | gpt-5.6-sol/high | `01a070ab-5d27-79b1-8ef2-886521abec95` | `01a070ab-63bc-7263-93d6-b38dde69bc9f` |

원본 오류(요약 파일의 문자열을 보정 없이 보존):

```text
StructuredRoleError: structured output이 유효하지 않습니다. schema recovery 0회
```

실패 receipt의 `status=schema_failed`, 직접 오류는 **`대조표 검사 scope 절차·phase·근거 결속 오류`**다. 원본에서 `val_goal_independent_behavior_contract`의 mechanism은 `phase=goal`, `basis_refs=[p_goal,p_contract]`인데 `scope_v5_phase`는 같은 phase의 `basis_refs=[v5_phase,p_goal]`만 제출했다. 따라서 `mechanism.basis_refs ⊆ scope.basis_refs` 조건에서 `p_contract`가 빠진다. `plan_inspection.py`의 실제 거부 조건과 원본 행을 직접 대조했으며 응답을 보정하거나 의미 evaluator를 사후 실행하지 않았다.

완료 terminal은 `completed`, 실패 receipt와 request·schema·prompt/instructions·model/effort·권한·inventory 관측·thread/turn·terminal usage의 공통 결속 10개는 모두 PASS다. 성공 `result.json`과 `clean-assessment.json`은 존재하지 않는다. output binding은 `NOT_APPLICABLE`, passed는 null이며 이를 결속 실패나 의미 PASS로 바꾸지 않았다. 원본 failure·terminal 및 collector의 안전 분류를 보존했다. 직접 대조 결과는 `runtime-preflight/session-audit/failure-evidence.json`에 있다.

12번째 expansion의 generation-pending과 생성 Plan 독립 검토·전용 기대표 결속, 13번째 review-generated에는 도달하지 않았다.

## 효과·실측 사용량

| 항목 | 실제 관측 |
|---|---|
| logical/provider/recovery | 1/1/0 |
| input / cached input / output / reasoning / total tokens | 45144 / 0 / 10385 / 5372 / 55529 |
| 역할 latency / provider duration | 191469ms / 189682ms |
| effect counts | `{"accepted_results": 0, "logical_calls": 1, "requests": 1, "terminal_observations": 1, "thread_intents": 1, "thread_start_receipts": 1, "turn_intents": 1, "turn_start_receipts": 1}` |
| Plan activation / Worker / 제품 원장 쓰기 | False / False / 0 |

usage는 provider `thread/tokenUsage/updated` 원본을 사용했다. 빈 새 thread의 첫 단일 turn을 확인한 경우에만 해당 호출에 귀속하며 reasoning은 output에 포함되므로 다시 더하지 않는다. 청구 비용은 provider 미제공으로 null이다. 개발 세션 전체 사용량·청구액·구독 한도 차감량으로 확대 추정하지 않는다.

## 계약·artifact digest

별도 bytes 표기 외에는 계약 필드 또는 canonical digest다.

| 항목 | SHA-256 |
|---|---|
| source manifest | `sha256:7f7e96a88fc8bc0ffd3f0b4127a6c8b148a599c14bcf4a876dd4b436888c8e77` |
| evaluation preflight | `sha256:d595616a32194b82907f9d7b932edc2d1f8445ad77b3a7e492e09206e1797f79` |
| planning contract | `sha256:7f82ae70266822264381e775862a63cf49e2137ed3ad8cf74f0b59dbc37f6fcd` |
| inventory | `sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2` |
| 전체 역할 v2 lock | `sha256:cf580740558935b4fb759658e5a626068a54fba08e7cc72ab457247f42016689` |
| role config 원문 bytes | `sha256:ebd581e333daad1ebaf63c14ae72b3c622b52a67a1d9ca4f626cf51e5d004cbd` |
| role config canonical | `sha256:28dbf15ea710995e1464e2375b8c85a717995fb0fef3390466649c0411fdea92` |
| role config typed | `sha256:f6362ac6721c975121d96a97d8c067fd5a5073e9fac5d65ddc7f27ba4ea630c5` |
| planning prompt | `sha256:c78fefead8921c5fa03f70cea9c0a2c1df38ce1734db2aa621c2097763f240b4` |
| planning output schema | `sha256:82f94a051b00cb36ab5620ce7320bbe456b30c064cd59c371efa77ee0544922c` |
| rules | `sha256:081e20a28f2be6a86dd8f4139f93474d1e751353f2808339e4e45d595f7f8871` |
| threshold | `sha256:624b50e26ba6df7f8639c5d898692c3ad170597d40285258e4dc098cffddcacf` |
| taxonomy | `sha256:2d51e24ec41ea93a87fa981c9642494ab2b1094dbb78d2ebcc3bdc8c96dc9f48` |
| codex.exe bytes | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |
| summary input | `sha256:873340cbd15570f8cca5c366f0f0751c2b9ec8a379e732e7c0235c22ebac3ce1` |
| clean request_digest | `sha256:0129591446988eda28a2622e519ca670993a73a2f2aad9ac320613965640545a` |
| clean schema_digest | `sha256:c6bfc9e5dbad732a6a7700f9bbed8a194a3a711acf1ce1eb51857c97efebbc9e` |
| clean prompt_bytes_digest | `sha256:31c3f5f31b3ecc3695f5191afe9c36882fff2ff30401f46a522188541ed74ebe` |
| clean instructions_bytes_digest | `sha256:19bfdc688722e660f2e6955cb9956902bca69803a3df044b60279953a95a9937` |
| clean expectation_digest | `sha256:47a415d771546cab537991420abc595d0b15ad2353fa51b619fd2fa267d13c14` |

직접 파일 bytes digest:

| artifact | SHA-256 |
|---|---|
| assignment.json | `sha256:b8af230a537db30e1c95f7e48a801990d10769c09ed354eba89ec96d585e3525` |
| preflight.json | `sha256:7e19c5c37f445fa4b4e4e49a58e125058339443fc8066d67814961111ada5003` |
| planning-binding.json | `sha256:72e38d281ba262ef84e5fe3bcfe4b1c1cfb3f8644bc99484b1fef46b5cce1096` |
| roles.json | `sha256:ebd581e333daad1ebaf63c14ae72b3c622b52a67a1d9ca4f626cf51e5d004cbd` |
| executed-source-manifest.json | `sha256:46246df4dfa182f4f6a784ddc36433a9f1f0fc9b6810669318a4e267f963c80c` |
| instruction-binding.json | `sha256:bcde30a0b40b6bc6eb82b3082a3d7ea1f7e05a80e07266c3f2763ea08f933079` |
| expectations.json | `sha256:3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e` |
| independent-fixture-review.json | `sha256:9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27` |
| summary.json | `sha256:48dfc1361277b7cf79fb4579b2f875df413bec96c2ad9daec5dd1c9e747ba5f1` |
| runtime-preflight/session-audit/pre-verification.json | `sha256:3735841a9a9b7d6392c292c8578e0354753767cc6cb40cf45be4a0bed1122b3f` |
| runtime-preflight/session-audit/post-verification.json | `sha256:13b9d56af25d06286f6f46ffc48b7f2cba4645833ab1573f8dea9d63241117de` |
| runtime-preflight/session-audit/prior-gate-verification.json | `sha256:8813a8da9d5379f271813fe59e3786f2452f8001fcd7f17c05d3ad69219869db` |
| calls/01-compact_plan_reviewer/request.json | `sha256:f8debafb9b66ff9543d747a9fae410208ceb4b4d97d358358aa3d9cf432fea57` |
| calls/01-compact_plan_reviewer/strict-schema.json | `sha256:70db549fe21ed1c3af1417aac3c0f0ebe2739c2198ceabe71bfcc475789c8cf1` |
| calls/01-compact_plan_reviewer/thread.receipt.json | `sha256:6f569f831453f95cf0c79efe494d2133cafa83d86ed7b030002f13ce21344499` |
| calls/01-compact_plan_reviewer/turn.receipt.json | `sha256:b58233e12936eaa32e486ac951b6f86bdca409bb5d919206d23a63fad0ed733b` |
| calls/01-compact_plan_reviewer/terminal.json | `sha256:b92fbed717a62c07bd356ad110929a021a3c9959069145eba89eb62e98708103` |
| calls/01-compact_plan_reviewer/failed.json | `sha256:165a77078827731ee9a83f1306c405e0b3d7828934809774b21f675c3238e876` |
| clean receipt canonical | `sha256:ba947421c511428ec81335a57b5a8b2c3ca7c1077a00c7110967f64ae4592c62` |
| clean 원본 final_response UTF-8 bytes | `sha256:fd2f7c9eac0292bdd50548f31a8315b4107a22b6698ccf535a560a02ad55426c` |
| runtime-preflight/session-audit/failure-evidence.json | `sha256:06c12ac36099b080ecc95d95b90c983d810d5d93eedcd95c6b90d09d4168e28e` |

실제 존재하는 개별 request·receipt·terminal·failure 등 주요 artifact bytes digest는 `runtime-preflight/session-audit/post-verification.json`에 보존했다. source 232파일, tracked 3426파일, 과거 보존 artifact 7140파일과 지정 Gate 원본의 before/after bytes가 같았다. 요약을 다시 게시하지 않았으며 현재 summary 입력 digest·receipt·효과·usage·진단 상태를 collector와 대조했다.

## 문서 검증·전달·다음 경계

변경 범위는 본 한국어 인계 문서와 README 인덱스 두 파일이다. run artifact·DB·cache·인증정보·source·frozen/legacy/raw는 Git에 포함하지 않는다. 문서 상대 링크, 직접 digest, 실행 횟수·변경 범위 및 `git diff --check`를 검증하고 세션의 두 파일만 한국어 commit으로 private origin main에 push한다. commit hash·push 결과·원격 HEAD·최종 clean은 자기참조를 피하여 로컬 `runtime-preflight/session-audit/delivery.json`과 최종 응답에 기록한다.

문서 패치의 두 차례 일치 행 오류는 변경 없이 거부됐고 정확한 전체 행으로 적용했다. 로컬 감사 스크립트의 첫 실행은 저장소 import 경로가 없어 `ModuleNotFoundError: scripts`로 종료했다. 실행 전 helper의 import 경로만 명시한 뒤 감사가 통과했다. 그 오류에는 provider 효과가 없었고 Gate·prepare·run 재시도 또는 schema recovery로 세지 않는다.

다음 미충족 경계는 이번 최초 실패의 원본 근거에 대한 원인 분석과 필요한 별도 보정·새 계약 검증이다. 동일 계약 재호출·자동 모델 승격은 하지 않는다. 전체 S06·planning pipeline·실제 프로젝트 E2E·token/latency qualification도 미충족이다. 사용자의 1.0까지 자율 진행 승인이 유효하므로 현재 실패 인계만으로 추가 사용자 판단을 요구하지 않는다. 후속 구현·호출은 이번 단일 제한 경계에서 실행하거나 예약하지 않았다.
