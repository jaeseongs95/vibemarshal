# R-S06-26 근거 우선 schema 적용 후 제한 역할 검증 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 결과

**FAIL — 첫 clean의 실제 성공 result·receipt·완료 terminal 결속은 PASS였으나, 고정 AC 연결 2개 불일치와 예상 밖 `VAL_SCOPE_001` finding 1개로 의미 assessment가 실패했다.** logical/provider/recovery는 **1/1/0**, 이후 12사례는 **NOT_RUN**이다. `prepare`와 `run`을 각각 정확히 한 번 실행했고 첫 실패에서 중단했다. `run`의 exit 0을 PASS로 해석하지 않았다.

새 결정론 Gate는 **5/5 PASS, 584 tests OK**다. 최종 `summary.json`은 첫 실패 뒤 한 번 게시됐고 `status=FAIL`, `diagnostic_errors=[]`, 공통·output 결속 및 보존 checks는 모두 true다. `outcomes.success=1`은 구조화 역할 결과 한 건의 수용이며 전체 의미 성공이 아니다.

이번 경계의 실행·검증·실패 인계는 완료했다. 기존 **S06 FAIL, Functional Alpha 미완료, FlowMarshal 1.0 NO-GO**를 유지한다. 생성 Plan 검토·전체 pipeline·실제 프로젝트 E2E를 통과했다고 주장하지 않는다.

## 실제 권한·기준·적용 지침

첫 파일 조회와 명령 실행 전에 이 turn의 개발자 `<permissions instructions>`에 제공된 `sandbox_mode=danger-full-access`, `approval_policy=never`를 직접 확인하고 첫 응답에 기록했다. 부모가 쓴 기대값을 관측으로 복제하지 않았으며 승인 질문·권한 상승 요청은 없었다. prepare의 실제 policy와 clean receipt도 `:danger-full-access/never`다.

- 시작 `HEAD=main=origin/main=6bf3f760697fee8f1db3e3a99bc06447210840ac`, 작업 트리 clean. `git ls-remote origin refs/heads/main`도 일치했다.
- origin은 `https://github.com/jaeseongs95/flowmarshal.git`이며 `gh repo view`의 `isPrivate=true`를 확인했다.
- 전역 `C:\Users\sjs95\.codex\AGENTS.md`와 프로젝트 루트 `AGENTS.md`를 적용했다. 상위 경로·docs·실행 스크립트 경로와 새 run 상위에는 추가 지침·override가 없었으며 config에 별도 fallback filename 지정은 없었다.
- [근거 우선 순서 보정](r-s06-evidence-first-schema-order-fix-handoff.md), [R25 인계](r-s06-25-post-diagnostics-fix-limited-validation-handoff.md), [실패 summary 보정](r-s06-diagnostics-failure-summary-fix-handoff.md), [R19 CLOSE](r-s06-19-close-handoff.md)를 기존 진단기와 대조했다. 권위 설계 §6.1·§7.1, cutover ADR과 legacy 동결 기준도 적용했다.
- 개발 세션 thread: `01a0703d-bdd3-74f3-80b5-2c1b79ca739a`. 개발 turn ID는 환경에 제공되지 않아 null로 남겼다. 제품 역할 thread·turn은 아래 실제 receipt 값이다.

## 새 root와 불변 계약

새 미사용 root는 `.flowmarshal-engine-eval/runs/r-s06-19-evidence-order-r26-20260905-v1`다. 생성 전에 부재를 확인했다. CLI의 기존 허용 prefix `r-s06-19-*`를 사용하고 `assignment.json`에 **R-S06-26**을 명시했다. 진단기 내부 `session=R-S06-19`는 변경하지 않았으며 과거 실행을 재개한 것이 아니다.

prepare는 기준 commit, source manifest 228파일과 source snapshot, 새 prompt·strict schema·실제 지침, 고정 fixture·기대표·독립 review·역할 설정·호출 순서·13/13/0 상한을 새 `preflight.json`과 `planning-binding.json`에 결속했다. 정적 11사례의 입력별 expectation, 독립 review의 Goal·Plan·등록 근거 digest, 준비 요청 12개의 strict schema 재구성·순서를 직접 검증했다. declared Reviewer schema digest도 현재 source에서 별도 기록했다. planning binding은 계약 결속 자료이며 전체 planning campaign을 실행한 결과가 아니다.

fresh App Server `model/list` 원본을 prepare와 실제 호출 inventory artifact에 보존했다. `flowmarshal-model-lock-v2`로 general Reviewer **gpt-5.6-terra/high**, expander **gpt-5.6-luna/high**, critical Reviewer **gpt-5.6-sol/xhigh**의 정확한 지원을 확인했다. 실제 호출은 general Reviewer 한 건뿐이다. 개발 세션의 Astra 배정을 제품 binding에 적용하지 않았으며 fallback·자동 모델 승격은 없었다.

자동 주입된 전역·프로젝트·새 workspace `AGENTS.md` 3개의 경로·본문 snapshot과 digest를 잠갔다. 실제 thread/start의 `instructionSources` 및 provider turn 직전 본문과 대조했다. 저장된 schema를 재구성해 `citations → validation_rows → validation_scope_rows → ac_validation_rows`와 AC 행의 `criterion_id → validation_id → basis_refs → scope_ids → ac_link_required → finding_codes` 순서를 확인했다. 이 순서의 통과와 의미 결과는 별도다.

과거 raw·thread·turn·receipt·summary·checkpoint는 실패 provenance로만 보존했고 실행·재개·추가 turn에 재사용하지 않았다. 기존 prepare가 읽는 고정 fixture 원본·역할 설정 및 과거 주입 경로는 provenance 입력이며, 새 현재 본문·fresh inventory·실제 receipt로 다시 결속했다. 고정 기대표·evaluator·oracle·threshold·taxonomy를 변경하지 않았다.

## 지정 Gate 대조와 새 Gate

지정 Gate `.flowmarshal-engine-eval/runs/r-s06-evidence-order-fix-final-20260905-v3`의 전체 evaluation contract와 현재 `_deterministic_contract()`가 동일했다. source digest, report, run-state와 5개 완료 cell의 fixture·contract·order seed·local receipt·실제 PASS를 직접 확인했다. 지정 Gate contract는 `sha256:6084f5f3d17a84a7ffb52079c8211e0806abee3deb857684ee5b9b07cdea9d33`, report는 `sha256:00f27cd51e8ee2a55222c328804cf4e746e34ef7514bd5115eb4d7982fa6dfd1`다.

그러나 지정 Gate에는 당시 Python 실행 파일 bytes·설치 package·환경 변수 snapshot이 없었다. 현재 환경까지 동일함을 입증할 수 없어 **그 Gate를 실행 성공 evidence로 재사용하지 않고 새 Gate를 한 번 실행했다.** 원본은 변경하지 않았다. 새 Gate 전후 Python 3.12.14, Windows 환경, interpreter·base interpreter bytes, 설치 package와 Python 환경 변수를 보존하고 동일성을 확인했다. 전체 contract·source·5개 cell을 재검증했다.

| 새 Gate 항목 | 결과 |
|---|---|
| compileall | PASS |
| 전체 unittest | 584 tests OK, 65.039초 |
| pip check | PASS |
| synthetic lifecycle | PASS, 합성 검사 |
| legacy freeze | 40파일 PASS, 변경·누락·예상 밖 경로 0 |

| 단계 | 실행 횟수 | 시각 KST | 경과 | exit |
|---|---|---|---|---|
| gate | 1회 | 15:29:31~15:30:39 | 68.250초 | 0 |
| prepare | 1회 | 15:31:36~15:31:41 | 5.125초 | 0 |
| run | 1회 | 15:31:56~15:34:31 | 155.343초 | 0 |

실제 실행은 `.venv\Scripts\python.exe -X utf8 -B`로 다음 모듈 명령을 호출했다. `runtime-preflight/session-audit/`의 단계별 `*.started.json`, `*.completed.json`, stdout/stderr log에 전체 argv·시각·exit·환경·log digest를 보존했다.

```text
-m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\codex\flowmarshal --run-root <R26 root>\deterministic
-m scripts.diagnostics.r_s06_10 prepare --run-root <R26 root>
-m scripts.diagnostics.r_s06_10 run --run-root <R26 root>
```

## 첫 clean의 직접 결과

- call: `model_call_c87d2346518444719b668dae66556490`.
- 역할·status: `compact_plan_reviewer`, `gpt-5.6-terra/high`, `succeeded`.
- thread: `01a07044-5163-73b2-8a37-386c25d83980`.
- turn: `01a07044-57a6-7433-bbd6-159488c9d0b3`.
- request·strict schema·prompt/instructions·권한·model/effort·inventory observation·thread/turn·완료 terminal·usage·result/output 결속은 모두 true다. `binding-verification.json`의 11개 checks와 현재 재계산 값이 완전히 일치했다.

고정 기대표와 실제 result로 assessment를 재계산하여 저장 `clean-assessment.json`과 완전히 일치함을 확인했다. AC×validation 행 집합은 **28/28 일치**, boolean 값은 **26/28 일치**다.

| AC | validation | 고정 기대 | 실제 제출 |
|---|---|---|---|
| `ac_004` | `val_task_scope_preservation` | false | true |
| `ac_004` | `val_task_unittest` | false | true |

위 두 Task validation은 Plan에 실제 연결되어 있으나 고정 기준상 필수 연결은 아니다. 모델은 AC-004의 명시 task/goal oracle 실행 요구를 별도 Task scope·unittest ID의 필수성까지 확대했다. `ac_link_required=false`는 기존 선택적 연결을 금지하지 않으므로 Plan link가 있다는 이유로 기대값을 true로 바꾸지 않았다.

또한 clean의 고정 기대 finding은 비어 있는데 모델은 다음 finding을 제출했다.

> `VAL_SCOPE_001`: ac_002의 val_task_validator_review는 Validator가 file·diff evidence만 검토하도록 명시하지만, 등록 절차는 최소 변경 의미 검토에 직접 file·diff·test evidence를 모두 받도록 요구한다.

이 finding은 `task_change_add`에 결속되며 `artifact:plan_contract`, `source:project_map`, `source:goal`을 근거로 제출됐다. 고정 evaluator는 이를 예상 밖 finding으로 집계하여 `reviewer_precision_ok=false`를 기록했다. 이 문서는 모델의 주장을 원본 그대로 인계하며, 결과 뒤 fixture 결함으로 재분류하거나 기대표를 완화하지 않는다. finding 타당성의 추가 원인 분석은 별도 경계다.

최초 오류는 `RuntimeError: SEMANTIC_ASSESSMENT_FAILED: clean`이며 전체 원본 오류와 assessment JSON은 `summary.json.error` 및 run stdout에 보존했다. R25에서 불일치했던 세 관계는 이번 원본에서 true이지만, 단일 새 호출의 차이만으로 schema 순서의 효과나 회귀 성공을 단정하지 않는다. 이번 고정 평가의 결과는 FAIL이다.

| 순번 | 사례 | 실제 결과 |
|---|---|---|
| 1 | clean | 결속 PASS / assessment FAIL |
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

`generation-pending.json`과 12회 완료 증거, 생성 Plan 독립 검토·전용 기대표, 13번째 `review-generated`에는 도달하지 않았다. 재호출·resume·recovery·fallback·재생성·새 root 우회는 없었다.

## 효과·실측 사용량·최종 summary

| 항목 | 실제 관측 |
|---|---|
| logical/provider/recovery | 1/1/0; 13/13/0 상한 중 나머지 미사용 |
| request/thread intent/start/turn intent/start/terminal/accepted result | 각각 1 |
| outcome success/failure/external_unknown/incomplete | 1/0/0/0; 별도 의미 assessment FAIL |
| input/cached input/output/reasoning/total tokens | 45,030 / 0 / 8,062 / 3,624 / 53,092 |
| 역할 latency/provider duration | 149,594ms / 147,749ms |
| Plan activation/Worker/제품 원장 쓰기 | false / false / 0 |
| summary | FAIL, checks 모두 true, diagnostic_errors 0 |

usage 원본은 `thread/tokenUsage/updated`, scope는 `thread`다. 빈 새 thread의 첫 단일 turn을 확인했으므로 이 한 호출에 귀속했다. provider `totalTokens`를 그대로 사용하고 output에 포함된 reasoning을 다시 더하지 않았다. 개발 세션 전체 사용량·청구 금액·구독 한도 차감은 이 값으로 추정하지 않는다.

최종 summary를 다시 게시하지 않았다. summary의 입력 digest가 현재 artifact 집합과 같고, 저장 summary의 receipt·terminal usage·효과 수가 읽기 전용 collector와 같음을 검증했다. 실행 log·추가 감사 결과는 summary 입력에서 제외되는 `runtime-preflight/session-audit/`에 두어 게시 뒤 입력을 바꾸지 않았다.

이번에는 실제 성공 result가 존재하여 output binding은 APPLICABLE/PASS다. result 없는 확정 역할 실패·external_unknown·malformed/partial artifact 경로는 이번 실제 호출에서 발생하지 않았으며 해당 경로의 실제 qualification을 주장하지 않는다.

## 계약·artifact digest

별도 bytes 표기를 제외하면 계약 필드 또는 canonical 객체 digest다.

| 항목 | SHA-256 |
|---|---|
| source manifest, 228파일 | `sha256:0f7b0ce4236d059e20e08a3a312cd6c3ab97656e51ec8ab4783a0eb4e7e2929f` |
| 결정론 evaluation contract | `sha256:6084f5f3d17a84a7ffb52079c8211e0806abee3deb857684ee5b9b07cdea9d33` |
| 새 결정론 report | `sha256:053005d1dd3c229993e1ad9a39e68e51562e72a6d290f88bef03aef28a890571` |
| 새 Gate 환경 snapshot | `sha256:0175b994ab6146ad6fe79024f4b2b969f990cd64caa9ce008c7e2d7a1217236c` |
| evaluation preflight lock | `sha256:a2ea4afc78291b9ab309e7c60c0e46c1204127053bbec8a2baca907faf204663` |
| planning binding contract | `sha256:693a396055e7e1fb6736bcc78ad5984fedd7c9f63888abf88fe2f598419f44da` |
| fresh inventory | `sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2` |
| 전체 역할 operational lock v2 | `sha256:ef7d5b53bc35fc500f0ce0eb046bc8d9984bf8b042aa416a3be5f221db8cee83` |
| clean 실제 operational lock | `sha256:57d0a693cff1e3efa1cba660ee8ac1921fa7c12089030d6965c505e417e63fe9` |
| 역할 설정 | `sha256:ba683966a19b9cc249ef6117af7df5430979ad1907a631f5b8cd54d85a97eeb6` |
| planning prompt | `sha256:c78fefead8921c5fa03f70cea9c0a2c1df38ce1734db2aa621c2097763f240b4` |
| planning output schema | `sha256:82f94a051b00cb36ab5620ce7320bbe456b30c064cd59c371efa77ee0544922c` |
| 현재 declared Reviewer schema | `sha256:5d827b169da092c04492cd516acdbd76ab62d3b6a116f5deb33eb6e8be120de9` |
| clean 실제 strict schema | `sha256:c6bfc9e5dbad732a6a7700f9bbed8a194a3a711acf1ce1eb51857c97efebbc9e` |
| clean 실제 instructions UTF-8 bytes | `sha256:19bfdc688722e660f2e6955cb9956902bca69803a3df044b60279953a95a9937` |
| clean 실제 전송 prompt UTF-8 bytes | `sha256:31c3f5f31b3ecc3695f5191afe9c36882fff2ff30401f46a522188541ed74ebe` |
| rules | `sha256:081e20a28f2be6a86dd8f4139f93474d1e751353f2808339e4e45d595f7f8871` |
| threshold | `sha256:624b50e26ba6df7f8639c5d898692c3ad170597d40285258e4dc098cffddcacf` |
| taxonomy | `sha256:2d51e24ec41ea93a87fa981c9642494ab2b1094dbb78d2ebcc3bdc8c96dc9f48` |
| 실행 codex.exe bytes | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |
| clean request canonical | `sha256:4e9ecb809790bf04f8971b9e64bc12cccacee0b2973bb43c8a6fddc05c61e2f6` |
| clean receipt canonical | `sha256:6a811f73413897d573e01422883d3f7e8b7c1b2065aaf74043298e9ea5886c20` |
| clean output canonical | `sha256:b6336dc8af7c16924988e1c34a5704feb7246e21aeb5be93dd9e2cf6a68f7fe7` |
| clean expectation | `sha256:47a415d771546cab537991420abc595d0b15ad2353fa51b619fd2fa267d13c14` |

다음은 실제 파일 bytes SHA-256이다. 전체 주요 artifact와 직접 검증 결과는 `runtime-preflight/session-audit/handoff-evidence.json`에도 결속했다.

| 항목 | SHA-256 |
|---|---|
| source-before.json | `sha256:aa66bae1b8009d48e8e392710e26c4f559415465baa1ea3aa0f809e1f258aeb7` |
| prior-gate-verification.json | `sha256:df6d63244e93823cabdc36117a2db581b2400f635e26cd42b7b9edaf8176be0e` |
| gate-verification.json | `sha256:75e601c510cf228b6840da28604b943e0937963a4242493fb07eac56ad7f6883` |
| preflight.json | `sha256:3435c56015af38bc07b85a178eb388875c2623c2dd2479d30d31f047b4c7c510` |
| executed-source-manifest.json | `sha256:7d45040b9b53c70377f8376c0a3ce7c4d317b017a060585cd29e9043411fde82` |
| planning-binding.json | `sha256:7e70479a09484a36aa39dd2890412ee98b303d6518e77cc9c2bcca92c906a4ab` |
| instruction-binding.json | `sha256:60ac3a76d1113554768652ac9ea747136dee716152f1d7584c4ad6f4b3c2a157` |
| expectations.json | `sha256:3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e` |
| independent-fixture-review.json | `sha256:9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27` |
| clean-assessment.json | `sha256:621df4fc0de2f7d73d64dab72ec48023685dacd0b72fa6a94df21cbb28cc1e1a` |
| clean-review.json | `sha256:29e7bc5dafe08410bcc2190739f970acbfdc2bbd369cb035017d94a6249650b5` |
| calls/01-compact_plan_reviewer/request.json | `sha256:2dafe032a376fb265a5a1d0f974771d879c6db2f90cb003b32f96ea2bb34e35d` |
| calls/01-compact_plan_reviewer/strict-schema.json | `sha256:70db549fe21ed1c3af1417aac3c0f0ebe2739c2198ceabe71bfcc475789c8cf1` |
| calls/01-compact_plan_reviewer/thread.receipt.json | `sha256:fe32699618f888ad7ff9deed6371770634ff4241cd7106fe7f2a371e77e69173` |
| calls/01-compact_plan_reviewer/turn.receipt.json | `sha256:d31502dc838aa6bf71b01e76a5bb814269ad7320df0a92146eb32c9eed8f0c43` |
| calls/01-compact_plan_reviewer/terminal.json | `sha256:0c47881491e22847e9ab575ef4ca1b58db280dbae5256dcb5d4802f686054bf9` |
| calls/01-compact_plan_reviewer/result.json | `sha256:91f7505f0b1b8f0e67604b381b2a92e87a7d0f241b3b7e4867302b0272f53775` |
| calls/01-compact_plan_reviewer/binding-verification.json | `sha256:3d5cb35fb7a2d2c6f6d269851f26d39c46f433bd2fbfa9ef6b1cc22b661a8ea6` |
| summary.json | `sha256:8d56c27604a638acc7c2e847d271a78bdc52c33575dfe49422c7a57524d08d47` |
| runtime-preflight/session-audit/pre-verification.json | `sha256:2ef7dddbaadd3caac88c5632824071ae4ed37a919ca8cbb2c24fa3e8a52de4a3` |
| runtime-preflight/session-audit/post-verification.json | `sha256:73a2ec7f9c9420e5f08b02926604f78b818867a095571365437631d53b5f19eb` |

## 보존·문서 검증·인계 경계

실행 후 source **228파일**, 과거 R-S06/S05/S06 원본 **6764파일**의 before/after digest가 같다. 인계 작성 전 tracked **3420파일**도 전부 같았다. legacy freeze 40파일 검증과 함께 source·frozen/legacy/raw artifact 보존을 확인했다. 변경·commit 범위는 본 인계 문서와 루트 README 인덱스뿐이다. run artifact·DB·cache·인증정보는 stage하지 않는다.

보조 읽기에서 Python one-liner 괄호 SyntaxError 1회와 `rg` 경로 wildcard 해석 오류 1회가 있었다. 모두 provider 실행 전에 발생한 조회 오류로 파일 변경·provider 효과가 없었고 정상 조회로 필요한 자료를 확인했다. 새 Gate·prepare·run과 사전·사후 계약 검증은 exit 0이다. 이 조회 오류를 제품 역할 실패나 schema recovery로 세지 않는다.

문서의 상대 링크·실제 digest·실행 횟수·변경 범위와 `git diff --check`를 검증한 뒤 이 세션의 두 문서만 한국어 commit으로 private origin main에 push한다. commit hash·push exit·원격 HEAD·최종 clean 상태는 자기참조를 피하여 로컬 `runtime-preflight/session-audit/delivery.json`과 세션 최종 응답에 기록한다.

현재 요청의 실행·provenance 전달에는 추가 사용자 판단이 필요하지 않았다. 이후 원인 분석·fixture 또는 source 보정·새 실제 검증의 범위를 정하려면 별도 사용자 판단이 필요하다. 동일 입력 재호출이나 자동 모델 승격으로 실패를 해소하지 않았으며 다른 작업으로 메시지·callback·예약을 보내지 않았다.
