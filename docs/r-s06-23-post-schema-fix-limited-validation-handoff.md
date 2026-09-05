# R-S06-23 strict schema 보정 이후 제한 실제 역할 검증 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 결과와 진입 경계

**FAIL — prepare와 strict schema 저장 왕복 결속은 통과했으나 첫 clean 응답의 scope 근거 결속이 adapter에서 거부되었다.** logical role call **1/13**, provider turn **1/13**, schema recovery **0**이다. clean의 사례별 의미 평가는 NOT_RUN이며 이후 12사례도 NOT_RUN이다. 재호출·fallback·다른 root 우회는 수행하지 않았다.

최초 오류는 `StructuredRoleError`이며 failed receipt의 구체적 원인은 `대조표 검사 scope 절차·phase·근거 결속 오류`다. provider terminal은 `completed`지만 receipt는 `schema_failed`이고 성공 `result.json`은 없다. 이어 진단기의 `summarize → completed_call_verification`이 없는 `result.json`을 읽어 `FileNotFoundError`로 종료했다. **run 종료 코드는 1이고 진단기 `summary.json`도 생성되지 않았다.** 별도 `limited-validation-outcome.json`은 직접 증거를 정리한 세션 감사 결과이며 진단기의 성공 summary를 대신 만들거나 원본을 보정한 것이 아니다.

[strict schema 저장 왕복 보정](r-s06-strict-schema-roundtrip-fix-handoff.md)의 효과는 이번 실제 전송·failed receipt에서도 확인했다. 새 제한 검증 통과나 전체 역할 성공으로 확대하지 않는다. 다음 경계의 진입 조건은 충족하지 못했다. 직접 증거상 미해결 경계는 응답의 scope 근거 일관성과 성공 result가 없는 terminal의 요약 처리다. 이번 범위에서 코드·fixture·oracle·threshold·역할 설정은 수정하지 않았다.

기존 **S06 FAIL, Functional Alpha 미완료, 1.0 NO-GO**를 유지한다. Plan activation, Worker, ledger write, 전체 qualification, S07 이후와 cutover는 NOT_RUN이다. 다음 작업 생성·자체 예약·다른 작업 메시지나 callback 없이 이 세션의 결과만 남긴다.

## 권한·기준과 provenance

첫 파일 조회 전에 현재 turn의 개발자 `<permissions instructions>`에서 실제 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인해 자기 세션 첫 응답에 남겼다. 현재 환경의 파일 시스템도 `unrestricted`로 제공되었다. 부모가 제시한 기대값을 관측값으로 복사하지 않았다. prepare와 실제 역할 receipt에서도 `:danger-full-access/never`를 확인했다.

- 시작 `main`, `origin/main`, `git ls-remote`의 원격 main: `ea1f003e33bd008654a48616035b55e5fd764132`. 작업 트리 clean.
- origin: `https://github.com/jaeseongs95/flowmarshal.git`. GitHub 조회의 `isPrivate=true`를 확인했다.
- 고정 R19/CLOSE 계약 root: `.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2`.
- 새 R23 root: `.flowmarshal-engine-eval/runs/r-s06-19-post-schema-fix-r23-20260905-v1`. 최초 확인 때 부재했고 미확정 intent가 없었다. 한 번만 생성했다.
- R20 `r-s06-19-post-close-r20-20260905-v1`, R21 `r-s06-19-post-lock-v2-r21-20260905-v1`, R22 `r-s06-19-post-capture-fix-r22-20260905-v1`은 읽기 전용 provenance로만 사용했다. 모두 미확정 intent와 terminal 없는 시작 turn이 없었다.

이하 artifact 경로는 새 R23 root 기준이다. `assignment.json`에 세션 R-S06-23과 호출 상한·순서를 기록했고 진단기의 기존 `session=R-S06-19` 형식은 변경하지 않았다. 과거 v1 checkpoint를 새 v2 또는 새 schema 의미로 재해석하지 않았다. R22의 완료 clean receipt는 과거 실패의 증거로만 남겼으며 재사용·재개·중복 완료 판정하지 않았다.

## 결정적 Gate와 prepare

fresh Gate 원본은 `.flowmarshal-engine-eval/runs/schema-roundtrip-deterministic-gate-20260905-v3`다. 다음 직접 검증이 모두 통과해 원본 9파일을 `deterministic/`에 바이트 그대로 복사했다. Gate를 새로 실행한 것으로 보고하지 않는다.

| 대조 항목 | 실제 관측 |
|---|---|
| 현재 source manifest | 225파일, `sha256:bed985f9d13ab6eb769f530cda73b709eb070acad98311ee1ec520d640e36be2` |
| 현재 `_deterministic_contract()` 전체 객체 | 원본과 정확히 일치 |
| contract digest | `sha256:b375e72714681bac58b011fa7a8f239bc5a7a57f891bc11d7703797be8581185` |
| report canonical digest | `sha256:da42b01d771ef369baf9b0272bb357aea7c6b17c6539327e38e5d0624e87c57c` |
| 상태·cell | COMPLETED, 5/5 완료·PASS, 각 cell의 contract 일치 |
| legacy freeze | 현재 40파일 PASS, 원본 cell의 freeze report 전체와 정확히 일치 |

Gate의 5개 cell은 compileall, 전체 577 tests, pip check, synthetic lifecycle, legacy freeze다. 과거 실행 환경 전체의 동일성까지 이번에 입증한 것은 아니다. `deterministic-provenance.json`은 모든 대조 항목·cell·원본 파일 digest를 보존한다.

이번 세션에서 직접 실행한 관련 회귀는 **39 tests PASS, exit 0**이다. runtime capture, v2 model lock과 consumer, case binding, 역할 schema, 진단 회귀를 포함한다. strict object key/required 순서와 저장 요청 왕복, request·strict artifact·intent·receipt·terminal의 개별 변조 및 strict artifact+receipt 동시 위조 차단을 포함한다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_engine_inspection_runtime_capture tests.test_engine_model_lock tests.test_engine_model_lock_consumers tests.test_engine_inspection_case_binding tests.test_engine_roles tests.test_engine_inspection_diagnostic -q
```

`session_audit.py`가 아래 prepare와 run을 각각 정확히 한 번 실행했다. `*.started.json`, `*.completed.json`, stdout/stderr log에 명령·시각·종료 코드·경과 시간·출력 digest를 append-only로 남겼다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 prepare --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-schema-fix-r23-20260905-v1
.\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 run --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-schema-fix-r23-20260905-v1
```

| 단계 | 실제 KST | 결과 |
|---|---|---|
| prepare | 12:32:08.061~12:32:13.289 | prepared=true, exit 0, 로컬 5.235초 |
| run | 12:32:28.164~12:35:16.222 | 첫 clean 거부 및 요약 오류, exit 1, 로컬 168.047초 |
| review-generated | NOT_RUN | phase claim·raw·호출 없음 |

prepare에서 Goal·State·Plan 입력, roles, expectations, independent fixture review **26파일이 고정 R19 원본과 바이트 단위로 일치**했다. static 11사례의 expectation input binding, 12개 준비 요청의 저장 후 strict schema 재생성, phase claim/raw, 권한, v2 operational binding을 모두 확인했다. `pre-run-verification.json`에 직접 검증 결과를 보존한다. oracle·threshold·taxonomy·사례 의미·순서·Prompt·instruction binding을 바꾸지 않았다.

`runtime-preflight/prepare/`에는 phase claim·inventory·policy가, `runtime-preflight/run/`에는 별도 phase claim·inventory가 있다. prepare raw는 run 이후에도 동일하다. 실제 call에는 inventory 2파일과 policy 3파일이 보존되었다. 역할 진입 후 정책은 call capture에 기록되어 run phase의 별도 policy 파일은 없다. 첫 call에서 중단되므로 capture 복귀를 다음 실제 preflight로 직접 관측하지는 못했다. 해당 관측은 `NOT_DIRECTLY_OBSERVED_NO_FOLLOWING_PREFLIGHT`다.

| 새 결속 | 값 |
|---|---|
| preflight lock | `sha256:dc9f2b0445d84d81d4fd3f4ef51bfebab4b982d896383ecffb822304da0eebde` |
| v2 operational lock | `sha256:ef7d5b53bc35fc500f0ce0eb046bc8d9984bf8b042aa416a3be5f221db8cee83` |
| raw inventory digest | `sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2` |
| executable digest | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |
| 역할 설정 digest | `sha256:ba683966a19b9cc249ef6117af7df5430979ad1907a631f5b8cd54d85a97eeb6` |
| Prompt digest | `sha256:79d3a4ce1160489c3b937a150fcadca7309526d1df9bb4204ed5c8d587f366cf` |
| planning output schema digest | `sha256:e3b31a5f63458f6483ae25093a31399ac7ce22ec53378408ec57d96267863832` |

## 첫 실패와 schema 왕복의 실제 확인

`calls/01-compact_plan_reviewer/`에 request, strict schema, thread/turn intent·receipt, terminal, failed receipt와 원시 inventory/policy가 보존되어 있다.

- thread: `01a06fa0-003a-76b1-b693-9cbfab19b7b7`
- turn: `01a06fa0-0668-77d1-b618-dadc6d952e77`
- call: `model_call_9091835714bf4499aab5b48f229b3b7b`
- request digest: `sha256:eb0c848289bcdd14f28a5ae30dbd15cfc9e3dc3ce1b3a0b5cd9d08cd7a46b30f`
- failed receipt canonical digest: `sha256:8441267ac079109ac24ecd7e5571c6d76951ec3e0d14350e24b0eef3b58eac0a`
- terminal 파일 bytes digest: `sha256:63db4945392c113fa958277a561c84c81e194212c52d5e9a125103322050097e`
- 실제 final response bytes 및 파싱한 출력 canonical digest: `sha256:e740db0553026d0b797b4a8eaf3994d1419091d61c8689f6f36f223bfa4cae46`

**저장 request에서 재생성한 schema, strict artifact, 실제 turn intent의 전송 schema, failed receipt의 schema digest는 모두 `sha256:ea08cc472ab8a1465555aca04624fbc512fed99df2f0e80f650b4b2daced0d23`로 일치했다.** 이번 call에서 R22의 저장 왕복 불일치는 재발하지 않았다.

`failed-call-binding-verification.json`의 관측 가능한 11개 관계는 모두 true다. request/prompt/instruction, 권한, thread/turn, model/effort, 빈 새 thread와 실제 instructionSources를 확인했다. `model_observation=true`는 현재 `verify_binding()`을 직접 실행한 결과다. receipt에 `output_digest`가 없고 `result.json`도 없으므로 terminal→result/receipt output digest 검증은 **null/미확인**이다. 이를 성공 call 전체 binding PASS로 표시하지 않았다. 원본 call의 `binding-verification.json`도 생성되지 않았다.

terminal JSON 파싱과 `PlanReviewEnvelope.model_validate()`는 통과했다. 최초 거부의 직접 관계는 다음과 같다.

| 원시 행 | 제출된 값 |
|---|---|
| validation | `val_goal_independent_behavior_contract` |
| mechanism | `tool=oracle.py`, `phase=goal`, `basis_refs=[c_ref_goal, c_ref_task]` |
| scope | `scope_v5_goal_behavior`, `phase=goal`, `basis_refs=[c_v5, c_ref_goal]` |
| 빠진 mechanism 근거 | `c_ref_task` |

scope의 phase와 mechanism의 phase는 같지만 mechanism의 모든 근거가 scope에 포함되어야 한다는 검사가 실패했다. `c_ref_goal`은 goal phase가 Task 단계 검사를 다시 수행하는 등록 원문이고 `c_ref_task`는 그 Task 단계 절차 원문이다. 모델은 finding 없음과 rating 4들을 제출했지만 adapter는 이 내부 근거 불일치를 허용하지 않았다.

원시 응답을 변경하지 않고 저장 request와 digest가 같은 입력으로 기존 adapter validator만 로컬 재생한 결과도 동일한 오류였다. provider 호출은 0회 추가되었다. `first-failure-analysis.json`에 해당 scope·mechanism·citation 전체와 재현 결과, 별도 요약 오류를 기록했다. 고정 oracle에 의한 `clean-assessment.json`은 생성되지 않았으므로 이 응답의 사례 의미를 합격·불합격으로 추가 판정하지 않았다.

## 호출 수·역할과 실측 usage

고정 순서 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`를 유지했다. clean 1회에서 중단했으며 12번째 expansion과 13번째 expanded-review에 도달하지 않았다. generation-assessment와 review-generated는 NOT_RUN이다.

| 시도 | logical / provider / recovery | 판정 |
|---|---|---|
| R20 | 0 / 0 / 0 | 과거 FAIL 보존 |
| R21 | 0 / 0 / 0 | 과거 FAIL 보존 |
| R22 | 1 / 1 / 0 | 과거 clean 완료 응답 후 binding FAIL 보존 |
| R23 이번 실행 | 1 / 1 / 0 | clean scope 근거 결속 거부, 요약 오류 |
| R20~R23 누적 | **2 / 2 / 0** | 시도 간 receipt 재사용 없음 |

이 누적은 지정된 R20~R23에 한정하며 R19 이전 전체 캠페인 호출 수를 뜻하지 않는다. 미확정 intent가 없고 이번 시작 turn에는 terminal과 failed receipt가 모두 있다.

| 제품 역할 | 고정 model/effort | 실제 관측 |
|---|---|---|
| general Reviewer / compact_plan_reviewer | `gpt-5.6-terra/high` | 동일 설정으로 clean 1회 |
| plan expander | `gpt-5.6-luna/high` | NOT_RUN, 실제 값 null |
| critical Reviewer | `gpt-5.6-sol/xhigh` | NOT_RUN, 실제 값 null |

바깥 세션 설정을 제품 역할에 적용하지 않았다. 기존 roles.json은 그대로이고 model 교체·silent fallback·schema recovery는 없다.

| 실측 항목 | R23 값 |
|---|---:|
| input tokens | 44,707 |
| cached input tokens | 0 |
| output tokens | 8,778 |
| reasoning tokens | 4,375 |
| total tokens | 53,485 |
| failed role receipt latency | 163,094 ms |
| provider duration | 161,211 ms |

usage source는 `thread/tokenUsage/updated`, scope는 `thread`다. `thread.receipt.json`의 `turns=[]`와 `turn.receipt.json`의 `first_empty_thread=true`가 있어 이번 첫 turn에 귀속했다. reasoning은 output에 포함되며 total에 중복 합산하지 않는다. 청구 금액·구독 한도 차감은 미제공으로 null이다. 미실행 사례의 receipt·usage·latency·실제 model/effort도 null/NOT_RUN이다.

## 무결성과 마감 증거

`source-before.json`, `limited-validation-outcome.json`, `source-after.json`으로 현재 source, 고정 R19 및 R20/R21/R22를 포함한 과거 원본, 원본 Gate, prepare raw의 불변을 확인한다. legacy freeze 40파일과 `git diff --check`도 확인한다. `evidence-manifest.json`은 생성 시점 artifact별 bytes digest이며 자기 파일과 이후 `delivery.json`은 제외한다.

`session_audit.py`, `failure_audit.py`는 로컬 증거 기록용이며 제품 코드를 변경하지 않는다. `final_audit.py`의 성공 result 전제 수집 경로는 실행하지 않았고 실패 전용 감사로 증거를 기록했다. 최종 무결성 경로만 마감에 사용한다. phase raw·intent·receipt·terminal·failed 원본을 덮어쓰거나 `result.json`·진단기 `summary.json`을 합성하지 않았다.

Git에는 이 한국어 인계 문서와 README 연결만 포함한다. run artifact·runtime DB·원장은 포함하지 않는다. 프로젝트 지침과 권위 계약 변경은 없어 AGENTS 수정 대상도 없다. 세션 변경만 하나의 한국어 commit으로 private origin main에 push하며 최종 commit·push 결과·원격 HEAD·clean 상태는 `delivery.json`과 자기 세션 최종 응답에 남긴다.
