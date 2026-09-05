# R-S06-30 Sol/xhigh 제한 실제 검증 인계

R-S06-30 결과는 **FAIL**이다. 고정 13사례 중 PASS 1, FAIL 1, NOT_RUN 11이다. 첫 실패 `bad`에서 중단했다.
이번 결과는 제한된 후보 검증이다. S06 전체·Functional Alpha·planning pipeline·실제 프로젝트 E2E·token/latency qualification의 PASS 근거가 아니며 **1.0 NO-GO**를 유지한다.

## 실제 정책·시작 상태·범위

첫 파일 조회·명령 실행 전에 현재 turn의 개발자 `<permissions instructions>`에서 `sandbox_mode=danger-full-access`, `approval_policy=never`를 직접 확인했다. 환경 컨텍스트도 파일 시스템 `unrestricted`였다. 승인 질문 없이 진행했다. 전역 및 프로젝트 지침·상위 경로·작업 경로의 override 여부와 config의 대체 지침 설정을 확인했다.

시작은 `main`, `HEAD=origin/main=원격 main=85b7ea34df6b3c30e54c32250279445dc58537a7`, clean이었다. `gh repo view --json isPrivate,nameWithOwner`로 `jaeseongs95/flowmarshal`, `isPrivate=true`를 확인했다.

[Finding evidence 보정 인계](r-s06-finding-evidence-binding-diagnostics-handoff.md), [R29 실제 검증](r-s06-29-sol-xhigh-actual-validation-handoff.md), [후보 설정 인계](r-s06-29-sol-xhigh-limited-validation-handoff.md), [권위 설계](orchestration-redesign.md), [cutover ADR](engine-cutover-adr.md)을 확인했다.

새 root는 `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r30-20260905-v1`다. 생성 직전 부재를 확인했고 `assignment.json`에 R-S06-30을 고정했다. CLI 경로 prefix와 harness 내부 `R-S06-19`는 기존 호환 식별자다. R29 run·receipt·응답을 resume하거나 성공 evidence로 재사용하지 않았다. 기존 R29 로컬 실행·감사 스크립트의 로직만 새 경로에 맞춰 재사용했고, 기존 builder의 과거 입력은 고정 fixture 출처로만 사용했다.

제품 source·기본 역할·prompt·strict schema·자동 주입 지침·Goal/Plan·oracle·expectation·threshold·taxonomy를 수정하지 않았다. 새 역할 작업·다른 작업 메시지·callback·예약·S07·원인분석·복구는 수행하지 않았다. 검증 범위의 실제 provider 역할 호출만 실행했다.

| 역할 | 설정 | fallback |
|---|---|---|
| normalizer | gpt-5.6-luna/high | [] |
| skeleton_generator | gpt-5.6-luna/high | [] |
| plan_expander | gpt-5.6-luna/high | [] |
| general_reviewer | gpt-5.6-sol/xhigh | [] |
| critical_reviewer | gpt-5.6-sol/xhigh | [] |
| executor | gpt-5.6-terra/high | [] |
| validator | gpt-5.6-sol/xhigh | [] |

역할 설정은 절대 경로 `D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-xhigh-roles.json`을 prepare에 주입했다. 원문 snapshot, bytes·canonical JSON·typed configuration digest와 입력 경로·선택 사유를 planning/preflight 및 각 request의 v2 lock에 결속하고 호출 전에 재대조했다.

## 새 deterministic Gate와 immutable 계약

변경이 반영된 시작 source에서 미사용 `deterministic` 하위 root로 Gate를 **정확히 한 번 실행하여 5/5 PASS**했다. `-X utf8 -B`, `PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`을 사용했다. Python·설치 package·runtime 파일 bytes, interpreter·venv·환경을 실행 전후 대조했으며 prepare/run에서도 동일했다.

| Gate | 결과 | 직접 근거 |
|---|---|---|
| legacy-freeze-manifest | PASS | {"changed_paths": [], "checked_file_count": 40, "manifest_digest": "sha256:25f21e8d09fb20f1aa0b3d28f5e1946dc4423aff0c5edf7c23e77c7f62bd1f5a", "missing_paths": [], "passed": true, "unexpected_paths": []} |
| compileall | PASS | 완료 receipt·PASS |
| pip-check | PASS | No broken requirements found. |
| synthetic-lifecycle | PASS | 완료 receipt·PASS |
| full-test-suite | PASS | Ran 611 tests in 66.583s, OK |

source 235파일의 원문 snapshot·manifest, Gate contract/report·완료 cell 5개·stdout/stderr digest, 환경 binding, prompt·strict schema·자동 주입 지침, 역할 설정·fresh inventory·입력·고정 기대표·독립 review를 새 `preflight.json`과 `planning-binding.json`에 결속했다. `locked_files`와 `lock_digest`를 매 호출 전에 재검증한다. `runtime-preflight/session-audit/pre-verification.json`에는 12개 사전 request의 bytes/typed digest·prompt bytes·schema canonical digest 및 11개 고정 기대표 결속을 기록했다. clean의 28행과 `ac_004 × val_goal_independent_unittest=true`를 호출 전에 확인했다.

fresh 실제 App Server inventory를 prepare와 각 역할 호출 직전에 수집했다. 선택·빈 fallback 조합의 지원, executable digest, 실제 정책, 요청·관측·receipt의 `flowmarshal-model-lock-v2`를 검증했다. inventory의 과거 digest와 같다는 사실을 fresh 관측 대신 사용하지 않았다.

| 자동 주입 지침 경로 | bytes SHA-256 |
|---|---|
| C:\Users\sjs95\.codex\AGENTS.md | sha256:2c113a26bd82cd1964a444ae53c99c3b4d45a2d42faec1262d0ade358c9751dc |
| D:\codex\flowmarshal\AGENTS.md | sha256:0c83a8fa36bade1dfb29f5f2efffdbdf1272542a233da1280a154a92ba6bfb0d |
| D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r30-20260905-v1\workspace\AGENTS.md | sha256:57fb50121f05548961e1e725e5537526b4e9cbc754383f8d42b0162096944b2a |

사례별 Goal·Plan 전체·validation 소유 단계/method/mode·등록 근거 digest·AC 표를 기존 사전 독립 review의 입력과 대조했다. 고정 평가 범위는 AC×validation 연결 필수성과 등록된 결함의 직접 근거다. constraint×Task 및 모든 수단·phase 의미의 전체 적격성까지 통과한 것으로 확대하지 않는다.

## 실행과 결과

| 단계 | 횟수 | 시작 UTC | 종료 UTC | 초 | exit |
|---|---|---|---|---|---|
| gate | 1 | 2026-09-05T12:07:04.300173+00:00 | 2026-09-05T12:08:15.833999+00:00 | 71.531 | 0 |
| prepare | 1 | 2026-09-05T12:08:46.598501+00:00 | 2026-09-05T12:08:54.175610+00:00 | 7.578 | 0 |
| run | 1 | 2026-09-05T12:09:21.378100+00:00 | 2026-09-05T12:20:05.795415+00:00 | 644.407 | 0 |

실제 실행 argv:

```text
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\codex\flowmarshal --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r30-20260905-v1\deterministic
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 prepare --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r30-20260905-v1 --role-config D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-xhigh-roles.json
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 run --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r30-20260905-v1
```

프로세스 exit 0은 사례 의미 PASS를 뜻하지 않는다. 각 사례의 request·receipt·완료 terminal·thread/turn·공통/output·inventory/config 결속·구조/참조·고정 의미 assessment를 통과한 뒤에만 다음 사례에 진입했다. 첫 실패 이후 호출·resume·recovery·fallback·새 root 우회를 수행하지 않았다.

| 순번 | 사례 | 결과 | AC 일치 |
|---|---|---|---|
| 1 | clean | PASS | 28/28 |
| 2 | bad | FAIL | 미평가 |
| 3 | wrong-goal | NOT_RUN | 미평가 |
| 4 | combined | NOT_RUN | 미평가 |
| 5 | boundary-clean | NOT_RUN | 미평가 |
| 6 | missing-link | NOT_RUN | 미평가 |
| 7 | future-result | NOT_RUN | 미평가 |
| 8 | stored-expanded | NOT_RUN | 미평가 |
| 9 | semantic-explicit | NOT_RUN | 미평가 |
| 10 | stored-multi-defect | NOT_RUN | 미평가 |
| 11 | semantic-missing-link | NOT_RUN | 미평가 |
| 12 | expansion | NOT_RUN | 미평가 |
| 13 | expanded-review | NOT_RUN | 미평가 |

expansion에 도달하지 않았으므로 새 생성 Plan 독립 검토·전용 기대표·expanded-review는 NOT_RUN이다.

| 사례 | call ID | thread | turn | receipt 상태 |
|---|---|---|---|---|
| clean | model_call_fdb00aede1d44c3ab8a610b73b323bde | 01a07179-3dd3-7d43-a65c-df713b845f6d | 01a07179-4703-7371-9a16-7a929f048542 | succeeded |
| bad | model_call_a1e9b4061ae944ec8b5b609e046c52d7 | 01a0717e-28a5-7e23-bf36-f6e4ad35f43a | 01a0717e-3022-7480-9e0e-1b19a0a5433d | schema_failed |

로컬 run 식별자는 위 R30 root와 assignment·preflight digest다. provider receipt는 별도 run ID를 제공하지 않아 call ID·thread ID·turn ID로 결속했다. terminal의 실제 최상위 thread_id/turn_id를 turn receipt binding과 대조했다. 추가 읽기 조회에서 terminal에 binding 키가 있다고 가정한 단발 KeyError가 있었고 실제 키를 확인했다. provider 호출·Gate·검증을 재실행한 것은 아니다.

| 사례 | request typed digest | receipt canonical digest | terminal bytes digest | raw final_response UTF-8 digest |
|---|---|---|---|---|
| clean | sha256:49d3ea42e0864a1d710c2c68f9c8dfa003e052b908bdaa9e46ab928644025804 | sha256:472f642131575313b4ed9b8b46a02d2cd7ee052d113658abd4870560d8144328 | sha256:86b0ab6558058e2619f21eb0df3d838e15a1ff6dbc1f61160991b7a68d98c074 | sha256:123200c65756a13eda42a474d43fc6c4bd30cae9cbdc068d55b550c87c968073 |
| bad | sha256:a756b3f61b73f927107b0b646ca7d7a942b83e65a02f62ff60ca156afdf753e9 | sha256:3f8bef023e6b21fc737bbe61c78765ecb0903dbbd5ea2e7fbf16da0d00212024 | sha256:74e01f5eef182fd9f31a77795690261570706b61bed531119dce07a9734b595e | sha256:71a94ec76ceda686e7aca10e4d8f1374a3d896cf714e482be8f3fd572e1414dc |

### 정확한 첫 실패

`bad` receipt는 `schema_failed`이며 오류는 다음과 같다. terminal은 completed·active=false, 공통 결속 PASS다. 구조/참조 실패 뒤 의미 assessment는 미평가이며 성공 전용 output 결속은 NOT_APPLICABLE이다.

```text
대조표 검사 scope finding의 직접 근거 누락
```

```json
{
  "error": "StructuredRoleError: structured output이 유효하지 않습니다. schema recovery 0회",
  "differences": [],
  "assessment": null
}
```

위 기록은 실제 원문·기대표·assessment의 직접 대조다. 모델 내부 원인 추정이나 제품 수정·복구는 수행하지 않았다.

## 효과·실측 사용량

| 사례 | input | cached input | output | reasoning(출력 포함) | role latency ms |
|---|---|---|---|---|---|
| clean | 47755 | 0 | 16752 | 9920 | 320312 |
| bad | 47773 | 0 | 17079 | 9840 | 313640 |

logical/provider/recovery는 **2/2/0**, 상한은 **13/13/0**이다.

| 항목 | 실제 관측 |
|---|---|
| cached_input_tokens | 0 |
| input_tokens | 95528 |
| latency_ms | 633952 |
| output_tokens | 33831 |
| provider_duration_ms | 629267 |
| reasoning_included_in_output | True |
| reasoning_tokens | 19760 |
| total_tokens | 129359 |
| unavailable_reason | None |
| effect_counts | {"accepted_results": 1, "logical_calls": 2, "requests": 2, "terminal_observations": 2, "thread_intents": 2, "thread_start_receipts": 2, "turn_intents": 2, "turn_start_receipts": 2} |
| outcomes | {"external_unknown": 0, "failure": 1, "incomplete": 0, "success": 1} |
| checks | {"01-compact_plan_reviewer_common_binding": true, "01-compact_plan_reviewer_output_binding": true, "02-compact_plan_reviewer_common_binding": true, "instructions_unchanged": true, "preserved_originals": true, "source_unchanged": true, "workspace_unchanged": true} |
| diagnostic_errors | [] |

usage는 빈 새 thread의 첫 단일 turn과 terminal 원문으로 귀속했다. reasoning은 output에 포함되어 중복 합산하지 않는다. 청구 비용은 provider 미제공으로 null이며 세션 전체 비용이나 구독 차감량은 추정하지 않는다. Plan activation·Worker 실행·제품 원장 쓰기는 0이다.

## 직접 artifact와 digest

아래 경로는 위 새 run root 기준이다. 실행 raw evidence는 Git 제외 로컬 root에 원문 그대로 보존하며, 완료·실패 receipt나 terminal을 재호출하거나 보정하지 않았다.

| 결속 | 값 |
|---|---|
| source_manifest_digest | sha256:04761bd2da94f5f13e89349b9bdb78817aafafe2a5aa5ab1670b8dd39328e6aa |
| lock_digest | sha256:2b38c6ee90ce6e2b6629a7d53236c59c45c6e5fa455d1e4b8bcf75e77475e680 |
| inventory_digest | sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2 |
| model_lock_digest | sha256:9cb6e554b5ffbab137cbfe643f8e3d67c0b81f3963dd26fcf33f08d1cde5a005 |
| prompt_digest | sha256:f1b42365b4092f513dae0812ad64daac5021ff4d5f32e4334a1877e78ce57c19 |
| output_schema_digest | sha256:b73d9c57c159c62368ca1fc3e01097c4e1e0b22fad639c74cc9c5d6b5b8fe7e6 |
| harness_digest | sha256:18b4805bc74830412211486cabfae903be78dfe66cdd058f9e8a32789ac802cb |
| codex_bin_digest | sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d |
| deterministic_report_digest | sha256:05aae8386edbed699f407ce25b00ffe6203498be2eaa51deb2bd3f2fccda83e5 |
| configuration_digest | sha256:0ca70f5002e0254e36f2fe7709080e2a15977cfa75c355cdc48c08f8222a1418 |
| source_bytes_digest | sha256:1e6a41c85c50a3804eda4a45395c0ea6eb49c4f2ecd721acfe2cb6ebee5f6ba9 |
| source_canonical_digest | sha256:91d94b1f0cb40598ca3f5a5eb3f019c94e28a3c35d88252e2a31f3b6a3faad9c |
| deterministic_contract_digest | sha256:5c331d02938ee2b8fdedf2d99c89f6161cc24cadfaa45b8f5b7faeee5d3b81fa |
| environment_digest | sha256:5fbd0297e3be2ce47922ba5516ab07b807745b475ae4b0bd5a15b9c08ca6e54b |
| post_verification_bytes | sha256:a331ae42d5eabee0b344275daef7c7f3b593b76a6fdce5c2e13e5e01d98601ad |

| artifact | bytes SHA-256 |
|---|---|
| assignment.json | sha256:3b7ac8fade3e804646e6574f1b54356468f818e96e79f6cccc45b6b8ffa1069f |
| calls\01-compact_plan_reviewer\binding-verification.json | sha256:2b449791986fd923a334ecf1db611143cd4afba7458c3ef35a5c5ad9ac356f7f |
| calls\01-compact_plan_reviewer\inventory-01.json | sha256:016c3acd7163f603422ef334fdeadc7a64666483c0b8da8556940fab79e5e51f |
| calls\01-compact_plan_reviewer\inventory-02.json | sha256:016c3acd7163f603422ef334fdeadc7a64666483c0b8da8556940fab79e5e51f |
| calls\01-compact_plan_reviewer\policy-01.json | sha256:82db286f00fd54f0cc8293124ac273bbaebbff233c727b9a5e80ac0bf3ff1454 |
| calls\01-compact_plan_reviewer\policy-02.json | sha256:82db286f00fd54f0cc8293124ac273bbaebbff233c727b9a5e80ac0bf3ff1454 |
| calls\01-compact_plan_reviewer\policy-03.json | sha256:82db286f00fd54f0cc8293124ac273bbaebbff233c727b9a5e80ac0bf3ff1454 |
| calls\01-compact_plan_reviewer\request.json | sha256:4e224c4997e50b518b8f1163271c7c208ffde86f17d50bcc02ba6c32456383b0 |
| calls\01-compact_plan_reviewer\result.json | sha256:629a76f29824837e731b8a4b8a15e5fc77aa169c5e696cd3853023178af35bd6 |
| calls\01-compact_plan_reviewer\strict-schema.json | sha256:367a250efd24bf239652f6acf91246f036084220897fbf02ec746f2d58a2b73d |
| calls\01-compact_plan_reviewer\terminal.json | sha256:86b0ab6558058e2619f21eb0df3d838e15a1ff6dbc1f61160991b7a68d98c074 |
| calls\01-compact_plan_reviewer\thread.intent.json | sha256:646bb4dc79a944b146a0272829ee86475ca78ee32521b60ac4023202340f01b6 |
| calls\01-compact_plan_reviewer\thread.receipt.json | sha256:72dd254ab4570cdc6428d1853c983ff3ecbd3e7b8892892ddbe8a67aba876864 |
| calls\01-compact_plan_reviewer\turn.intent.json | sha256:589068642e14a0d89e49e26d6fe70407d2cb186982026b1ae434c2c7b5a8aa44 |
| calls\01-compact_plan_reviewer\turn.receipt.json | sha256:aee044e02ea0e68c44aa465647c467c7f2673399e111d4c6d2dc0bd9fea72966 |
| calls\02-compact_plan_reviewer\failed.json | sha256:8ab563f2439a2081381007bb44d380a8a63f0da0f487f74a7811a98b762fe0fb |
| calls\02-compact_plan_reviewer\inventory-01.json | sha256:016c3acd7163f603422ef334fdeadc7a64666483c0b8da8556940fab79e5e51f |
| calls\02-compact_plan_reviewer\inventory-02.json | sha256:016c3acd7163f603422ef334fdeadc7a64666483c0b8da8556940fab79e5e51f |
| calls\02-compact_plan_reviewer\policy-01.json | sha256:82db286f00fd54f0cc8293124ac273bbaebbff233c727b9a5e80ac0bf3ff1454 |
| calls\02-compact_plan_reviewer\policy-02.json | sha256:82db286f00fd54f0cc8293124ac273bbaebbff233c727b9a5e80ac0bf3ff1454 |
| calls\02-compact_plan_reviewer\policy-03.json | sha256:82db286f00fd54f0cc8293124ac273bbaebbff233c727b9a5e80ac0bf3ff1454 |
| calls\02-compact_plan_reviewer\request.json | sha256:13f2c70d03175f39614a3a09c3c6238c2c3d51495dfe283e382085f3982a165d |
| calls\02-compact_plan_reviewer\strict-schema.json | sha256:367a250efd24bf239652f6acf91246f036084220897fbf02ec746f2d58a2b73d |
| calls\02-compact_plan_reviewer\terminal.json | sha256:74e01f5eef182fd9f31a77795690261570706b61bed531119dce07a9734b595e |
| calls\02-compact_plan_reviewer\thread.intent.json | sha256:646bb4dc79a944b146a0272829ee86475ca78ee32521b60ac4023202340f01b6 |
| calls\02-compact_plan_reviewer\thread.receipt.json | sha256:7e6866269997f09e33f288b8854d3c781200ce9faa0b2eae4d43f827aba4d6a8 |
| calls\02-compact_plan_reviewer\turn.intent.json | sha256:cff902ea04782bbd9993d682ae1b6dd792d2774bba3fffd6011ccf38be06f87b |
| calls\02-compact_plan_reviewer\turn.receipt.json | sha256:3764dcacbdda1cf1903b3eaa5a42b3125b34169ada431cc141e1385436e43688 |
| clean-assessment.json | sha256:dc0497ddd0a6d4c539e5920bf87b24436166de553036b445dc4e6f4bda7f1f28 |
| deterministic-environment-binding.json | sha256:2c765c48cf4b8098688951aa383789ccf3fadf7ae74065bfb4ff97103339cf69 |
| deterministic/evaluation-contract.json | sha256:4a2f176f70750a0d4e88c09e6ff4a58589907cc90e4a0dbcfdc840e625a0cdc2 |
| deterministic/qualification-report.json | sha256:6d05d384c0db8162acac2e1a0f651fc2104fc8ca68e96e743abcd0204485962f |
| executed-source-manifest.json | sha256:c590175bac8671bf2e0461401a5a55be25305060a48b9d691c62c83d1a00967c |
| expectations.json | sha256:3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e |
| fixture-assessment.json | sha256:6016ad1a49e79c9ca861eb2a90019db6a97dc3b20e92252e8a6237d74d5cbd89 |
| independent-fixture-review.json | sha256:9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27 |
| instruction-binding.json | sha256:bd3326cf9fc7d3d4206027865ab226c53da7a5c8ff6e25154c9a1e8f79619ee8 |
| planning-binding.json | sha256:608df9fc4e789ea3bfe1dd0732a93569d079cca47985b67bd1d146f66a00625a |
| preflight.json | sha256:504ec84751a1300d942d7ab9f70f0c951c0b1c8535d493cd239519d93a0e7b11 |
| roles.json | sha256:1e6a41c85c50a3804eda4a45395c0ea6eb49c4f2ecd721acfe2cb6ebee5f6ba9 |
| runtime-preflight\session-audit\gate.completed.json | sha256:75487d148c866e85b9cd3abf8975eb3429dccf1ff4cce8739c10afdafac8d4e0 |
| runtime-preflight\session-audit\prepare.completed.json | sha256:67d446356743652a62ed13a05eb9e8cb1316a67b6cb19d66de24ea06bb4f91dc |
| runtime-preflight\session-audit\run.completed.json | sha256:c0e5214d702cc6c0c1273eababb853f5322310ac03d1228da05f56601d264b1a |
| source-raw-clean-assessment.json | sha256:cff4b02471ca348d77925a48755c36fa3356ff6b16fc4512da7a223fd5161c89 |
| summary.json | sha256:2d6dc7c12edb68d30387c04cded3fbe18e8a8602093618e03858b49a90aec114 |

## 보존·전달

문서 작성 전에 source 235파일, 기존 tracked 3435파일, 과거 보존 artifact 8349파일의 bytes가 동일함을 확인했다. 새 결과를 저장된 원문에서 재검증했고 summary는 한 번만 게시했다. tracked 변경은 본 인계와 [문서 지도](README.md) 두 파일이다. 문서 링크·digest·변경 범위·`git diff --check`를 확인한 뒤 하나의 한국어 commit으로 승인된 private origin main에 push한다. 실제 commit/push와 HEAD=origin/main=원격 main·clean의 증거는 자기참조를 피하여 `runtime-preflight/session-audit/delivery.json` 및 최종 응답에 기록한다.

R-S06-30 결과 기록으로 이 경계를 종료한다. S07·원인분석·복구·후속 작업 선택은 수행하지 않았다. S06 전체와 Functional Alpha는 이번 경계에서 NOT_RUN이며 1.0 NO-GO다.
