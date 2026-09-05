# R-S06-22 capture 보정 이후 제한 실제 역할 검증 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 결과와 다음 경계

**FAIL — prepare는 성공했고 첫 clean의 실제 provider 응답까지 확보했으나, 완료 receipt 재검증에서 `ACTUAL_COMPLETED_CALL_BINDING_MISMATCH`로 즉시 중단했다.** logical role call **1/13**, provider turn **1/13**, schema recovery **0**이다. clean의 사례별 의미 평가와 이후 12사례는 NOT_RUN이다. CLI 종료 코드 0은 FAIL summary를 기록한 정상 종료이며 검증 PASS를 뜻하지 않는다.

[R21 capture 보정](r-s06-21-runtime-preflight-capture-fix-handoff.md)의 prepare/run 파일 충돌은 이번 실제 실행에서 발생하지 않았다. 새 실패는 저장 request로 strict schema를 재구성할 때 `required` 배열 순서가 바뀌는 진단 receipt 검증 경계에 있다. 다음 경계는 이 재구성·digest 불변조건의 구현 보정과 결정적 회귀다. 코드·fixture·oracle·threshold·role 설정은 이번에 변경하지 않았다.

기존 **S06 FAIL, Functional Alpha 미완료, 1.0 NO-GO**를 유지한다. Plan activation, Worker, ledger write, 전체 qualification, S07 이후 단계와 cutover 진입 조건은 충족하지 못했다. 새 실제 검증·다음 작업 생성·예약·다른 작업 통지는 수행하지 않았다.

## 권한·기준·provenance

첫 파일 조회 전에 현재 turn의 개발자 `permissions instructions`에서 실제 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인해 자기 세션 응답에 남겼다. 부모의 기대값으로 추정하지 않았다. 제품 prepare 및 실제 역할 turn receipt도 `:danger-full-access/never`를 확인했다.

- 시작 main·origin/main·실제 원격 main: `506d73eb3c81b1b22f3e79738fa5862116991904`. 작업 트리 clean.
- origin: 프로젝트 지침의 private 원격 `https://github.com/jaeseongs95/flowmarshal.git`. 공개 설정 변경 없음.
- 고정 계약 root: `.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2`.
- 새 run root: `.flowmarshal-engine-eval/runs/r-s06-19-post-capture-fix-r22-20260905-v1`. 첫 확인 당시 부재했으며 미확정 intent도 없었다.
- R20 `r-s06-19-post-close-r20-20260905-v1` 및 R21 `r-s06-19-post-lock-v2-r21-20260905-v1`을 읽기 전용 provenance로 보존했다. 각각 logical/provider **0/0**, 미확정 intent 없음이다. 특히 [R21 실패](r-s06-21-model-lock-v2-limited-validation-handoff.md)는 0회 실패 이력으로 누적 기록했고 재개하지 않았다.

이하 artifact 상대 경로는 새 run root 기준이다. 진단기의 기존 `session=R-S06-19` 식별 형식은 변경하지 않고 `assignment.json`에 이번 세션을 R-S06-22로 기록했다. `source-before.json`과 `source-after.json`에서 고정 원본·과거 run·보정 Gate의 전체 파일 bytes digest를 대조한다.

## Gate 재사용과 실행 전 검증

보정 Gate 원본 `.flowmarshal-engine-eval/runs/r-s06-21-runtime-preflight-capture-gate-20260905-v2`를 직접 읽고 다음을 확인했다.

| 항목 | 관측 |
|---|---|
| 현재 source manifest | 225파일, `sha256:b714ef5e353f17442a929b5e353735aacacd14fe694b3743c0f352b098bdf23c` |
| 결정적 계약 | 현재 `_deterministic_contract()` 전체 객체와 정확히 일치, `sha256:37dc482be8584166709d90981f5f24ecd6718a0217fede257d23bfa78ce38e3e` |
| report canonical digest | `sha256:742eb27ce1a562c63c7046f7c4031875d24de176bca6e535a703ab4afe3766ef` |
| 완료 상태와 cell | COMPLETED, 5/5 완료·PASS, 각 cell 계약 일치 |
| legacy freeze | 원본 cell과 현재 보고서 전체 일치, 40파일 PASS |

원본 9파일을 `deterministic/`에 바이트 그대로 복사했다. `deterministic-provenance.json`, `gate-legacy-exact-verification.json`에 재사용 근거·원본 경로·파일별 digest를 보존했다. 재사용 Gate는 576 tests, compileall, pip check, synthetic lifecycle, legacy freeze를 포함한다. 이번 세션에서 전체 Gate를 새로 실행한 것으로 표현하지 않는다. 과거 Python 실행 환경 전체의 동일성을 새로 입증한 것도 아니다.

이번 세션에서 직접 실행한 관련 회귀는 다음 **32 tests PASS**, exit 0이다. 전용 capture 회귀는 phase 분리, 성공·예외 뒤 capture 복귀, append-only 충돌, 빈·절단 raw 보존, 동시 claim, 미완료 intent·중복 실행 차단을 검사한다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_engine_inspection_runtime_capture tests.test_engine_model_lock tests.test_engine_model_lock_consumers tests.test_engine_inspection_case_binding -q
```

## 실제 prepare 및 capture 보정 확인

`session_audit.py`가 아래 명령을 각각 정확히 한 번 실행하고 started/completed 기록, stdout/stderr, 종료 코드, 로컬 경과 시간을 별도 파일로 보존했다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 prepare --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-capture-fix-r22-20260905-v1
.\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 run --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-capture-fix-r22-20260905-v1
```

| 단계 | KST | 관측 |
|---|---|---|
| prepare | 11:25:10.368~11:25:15.805 | prepared=true, exit 0, 로컬 5.437초 |
| run | 11:25:34.223~11:29:38.761 | summary FAIL, exit 0, 로컬 244.531초 |
| review-generated | NOT_RUN | phase claim·raw·호출 없음 |

prepare가 만든 Goal·State·Plan 입력, roles, expectations, independent fixture review **26파일은 R19 고정 원본과 바이트 단위로 모두 일치**했다. static 11사례의 expectation input binding도 검증했다. `pre-run-verification.json`에 결과와 prepare raw 초기 digest를 남겼다. oracle·threshold·taxonomy·사례 의미·순서와 Prompt/Schema/instruction binding을 수정하지 않았으며 과거 v1 checkpoint를 v2로 재해석하지 않았다.

`runtime-preflight/prepare/`에는 `phase-claim.json`, `inventory-01.json`, `policy-01.json`이 있고, `runtime-preflight/run/`에는 별도 `phase-claim.json`, `inventory-01.json`이 있다. prepare raw는 run 이후에도 bytes가 동일하다. call 디렉터리에는 `inventory-01.json`, `inventory-02.json`, `policy-01.json`~`policy-03.json`이 별도로 보존돼 있다. run phase 정책 파일이 없는 것은 역할 진입 후 정책 관측이 call capture에 기록됐기 때문이며 누락된 관측을 추정해서 채우지 않았다.

call capture 복귀는 source의 `finally` 경로와 이번에 통과한 전용 예외 회귀로 확인했다. **이번 실제 run은 첫 call에서 중단돼 다음 phase inventory가 없다. 따라서 실제 호출 뒤 복귀를 후속 raw로 직접 관측한 것으로 주장하지 않는다.** `limited-validation-outcome.json`의 해당 상태는 `NOT_DIRECTLY_OBSERVED_NO_FOLLOWING_PREFLIGHT`다. review-generated의 실제 capture도 NOT_RUN이다.

| fresh 결속 | 값 |
|---|---|
| format | `flowmarshal-model-lock-v2` |
| preflight canonical lock | `sha256:f86f0b8ded606a961e79b6eaa06d934001719bf29fb76e894b2013b50269cf43` |
| operational lock | `sha256:ef7d5b53bc35fc500f0ce0eb046bc8d9984bf8b042aa416a3be5f221db8cee83` |
| 전체 raw inventory 감사 digest | `sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2` |
| executable | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |
| 역할 설정 | `sha256:ba683966a19b9cc249ef6117af7df5430979ad1907a631f5b8cd54d85a97eeb6` |
| Prompt | `sha256:79d3a4ce1160489c3b937a150fcadca7309526d1df9bb4204ed5c8d587f366cf` |
| planning Schema | `sha256:a4b850d73a3e040f7f769208206fffbca530ef65de3487999c2f4625dc6bca7c` |

## 첫 실패의 직접 증거

첫 clean은 `calls/01-compact_plan_reviewer/`에 request, strict schema, thread/turn intent·receipt, terminal, result, binding verification을 남겼다. `thread.receipt.json`의 `turns=[]`, 실제 instructionSources 세 경로·본문 digest와 `turn.receipt.json`의 `first_empty_thread=true`를 확인했다. 공급자 terminal은 completed이며 result receipt는 succeeded지만 최종 검증 판정은 FAIL이다.

- thread: `01a06f62-c0b1-7ed0-9386-a518565a7a24`
- turn: `01a06f62-c65a-7783-91c3-f4b9ae5e2eee`
- call: `model_call_2fe82f58eb6e4abd929ab4cc26628006`
- request digest: `sha256:d2a337dd8a798a157378f75912debafbdf1c221732fb0a8f8ba732a6bdc290c8`
- receipt canonical digest: `sha256:1b949aa20337da70b7c38f2890f32f77c2b5a23aa428d0acdf4753748dcaba4b`
- 실제 output digest: `sha256:523b2a7778401e36a18865dc96aea89f92a1478dc9a14fb844c983fc445a54e7`

`binding-verification.json`에서 `payload`, `prompt`, `instructions`, `schema`, `model_effort`, `thread_turn`, `receipt`는 true이고 **`model_observation`만 false**다. 이 필드명만으로 실제 model 변경을 뜻하지 않는다. 내부 `verify_role_receipt()`는 모델 관측 전에 output schema digest도 검사한다.

저장 원문만 읽은 로컬 원인 분리 감사에서 다음을 확인했다. provider·phase·역할 재호출은 없었다.

1. terminal JSON과 저장 result payload는 같고, request·output digest와 role/model/effort/inventory identity도 일치한다.
2. 실제 `strict-schema.json`과 `turn.intent.json.output_schema`, receipt의 schema digest는 모두 `sha256:53eef1cbc39b14a3ff470ce14c190dcd771c6be7cff54f1ce1f68e548d72cea3`로 일치한다.
3. 저장 `request.json`에서 strict schema를 다시 생성하면 `sha256:ea08cc472ab8a1465555aca04624fbc512fed99df2f0e80f650b4b2daced0d23`가 된다. 이 값 때문에 `ROLE_RECEIPT_BINDING_MISMATCH`가 재현됐다.
4. 깊은 경로 비교 결과 차이는 **required 배열 11곳의 순서만**이다. `write_new()`는 `sort_keys=True`로 properties key 순서를 정렬하고, `strict_json_output_schema()`는 `required=list(properties)`를 다시 만든다. 처음 메모리의 선언 순서와 저장 후 정렬 순서가 달라진다. 예를 들어 `ACValidationInspection.required`는 `criterion_id, validation_id, ac_link_required, ...`에서 `ac_link_required, basis_refs, criterion_id, ...` 순서로 바뀐다.
5. 별도 로컬 `verify_binding()`에서 request의 v2 operational binding과 receipt observed binding은 일치했다. 실패를 모델 교체나 권한 불일치로 해석할 직접 근거는 없다.

근거는 `binding-failure-analysis.json`, `binding-failure-field-comparison.json`, `schema-order-analysis.json`, `schema-order-deep-comparison.json`, `actual-operational-schema-verification.json`이다. 첫 schema 비교는 배열 전체를 한 차이로 묶어 분류가 거칠었고, 추가 deep comparison에서 내부 11개 required 경로로 좁혔다. 두 분석 파일 모두 보존했으며 원본 summary·request·schema·receipt는 수정하지 않았다.

후속 보정은 직렬화 왕복 후 strict schema·receipt digest 검증의 안정성을 확보하고 실제 전송 schema와 재검증 schema가 같은지 검사해야 한다. 저장 원본의 digest를 새 의미로 재해석하거나 현재 FAIL을 PASS로 바꾸어서는 안 된다. 이번 범위에서는 보정 코드를 작성하지 않았다.

## 사례·모델·실측 사용량

고정 순서 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`를 유지했다. clean 1회가 binding 단계에서 FAIL했고 `clean-assessment.json`은 없다. 나머지는 NOT_RUN이며 expansion에 도달하지 않아 generation-assessment나 13번째 호출을 만들지 않았다.

| 제품 역할 | 고정 설정 | 실제 관측 |
|---|---|---|
| 일반 Reviewer / compact_plan_reviewer | `gpt-5.6-terra/high` | 동일 설정으로 clean 1회 |
| expander | `gpt-5.6-luna/high` | NOT_RUN, 실제 model/effort는 null |
| critical Reviewer | `gpt-5.6-sol/xhigh` | NOT_RUN, 실제 model/effort는 null |

바깥 개발 세션 모델과 제품 역할 설정을 혼동하지 않았다. model fallback, 자동 재호출, schema recovery는 없다. 완료된 thread/turn receipt와 terminal이 있어 미확정 provider intent도 없다.

| 실측 항목 | 값 |
|---|---:|
| input tokens | 44,710 |
| cached input tokens | 0 |
| output tokens | 13,049 |
| reasoning tokens | 3,458 |
| total tokens | 57,759 |
| role receipt latency | 239,531 ms |
| provider duration | 237,854 ms |

usage source는 `thread/tokenUsage/updated`, scope는 `thread`다. 빈 새 thread의 첫 turn이라는 직접 receipt가 있어 이번 1회에 귀속했다. reasoning은 output에 포함되며 total에 다시 더하지 않는다. 청구 금액·구독 한도 차감은 제공되지 않아 null이며 추정하지 않았다. 미실행 사례의 receipt·usage·latency·model/effort도 null/NOT_RUN이다.

## 무결성 및 마감

`limited-validation-outcome.json`은 호출별 직접 관측, raw inventory·phase 목록, 첫 실패, 미실행 상태, R20/R21의 0회 실패 provenance를 보존한다. source·fixed input·instruction 잠금, prepare raw, 과거 root와 원본 Gate는 불변이다. 최종 `source-after.json`은 legacy freeze 40파일, 허용 문서 외 기존 추적 파일 불변, `git diff --check` 결과를 기록한다. `evidence-manifest.json`은 생성 시점 artifact의 파일별 bytes SHA-256이며 자신과 이후 `delivery.json`을 제외한다.

제품·fixture·role 설정과 프로젝트 지침의 변경은 없으므로 권위 문서나 AGENTS 갱신 대상은 없다. Git 변경은 이 한국어 인계 문서와 README 연결뿐이다. run artifact는 Git에 넣지 않는다. 세션 변경만 한국어 커밋으로 private origin main에 push하며 최종 commit·push·원격 HEAD·clean 확인은 `delivery.json`과 자기 세션 최종 응답에 남긴다.
