# 실제 역할 48-cell qualification 결과

2026-09-06 KST. **48/48건 실행 완료, 역할 Gate FAIL, 1.0 NO-GO**다. 사용자 지시 `응 그럼 48건 진행해`에 따라 Plan 8개·Goal 8개 fixture를 seed `17`, `43`, `89`로 각각 한 번씩 검증했다. 실제 모델 호출은 정확히 48회이며 재시도·schema recovery는 0회다. Planning 18·E2E 4·성능 36은 이번에 실행하지 않았다.

## 결과

| 지표 | 실측 결과 | 기존 합격선 | 판정 |
|---|---:|---:|---|
| 완료 cell | 48/48, seed별 16개 | 전체 matrix 완료 | 충족 |
| 필수 finding recall | 97.06% | 90% 이상 | PASS |
| finding precision | 90.70% | 85% 이상 | PASS |
| critical false admission | 0 | 0 | PASS |
| clean false block | 1 | 0 | **FAIL** |
| schema failure | 1 | 0 | **FAIL** |
| seed 간 critical verdict 불일치 | 0 | 0 | PASS |

원시 결과의 `P11-adversarial/seed-17`은 구조화 제출이 거부됐고, `G01-clean/seed-17`은 정상 요청을 차단했다. 이 두 건 때문에 역할 Gate가 실패했다. 별도 진단으로 `G07-adversarial/seed-43`의 필수 `GOAL_TRACE_GAP` 누락이 있으며 recall에 반영됐다. schema 실패는 의미 판단에 억지로 포함하지 않고 별도 0건 Gate에서 판정한다. 따라서 위 recall·precision만으로 모든 응답의 정확성을 주장하지 않는다.

`run-state.json`의 `COMPLETED`는 48건 수집 완료를 뜻한다. `qualification-report.json.passed=false`와 실행 프로세스의 종료 코드 `1`이 품질 실패를 나타낸다. receipt의 `succeeded` 47개도 provider 응답 수신·schema 통과이며, 47개 사례의 의미 정답을 뜻하지 않는다.

## 원인 분류와 후속 수정

| 사례 | 직접 대조한 원인 | 처리 |
|---|---|---|
| P11-adversarial / seed 17 | 원시 응답은 `DUPLICATE_STRATEGY`와 `DIVERSITY_FAILURE`를 찾았지만 `affected_task_refs`에 candidate ref를 넣었다. v9의 전달 `ReviewDraft`는 이를 허용하고 호출 후 generic validator만 금지했다. 입력에는 권위 Task catalog도 없었다. | 모델의 필드 의미 오용과 함께 **전달 schema/사후 validator 불일치라는 평가 계약 결함**으로 분류한다. 순수한 모델 JSON-schema 실패라고 단정하지 않는다. 기존 schema_failed receipt·Gate FAIL은 보존한다. |
| G01-clean / seed 17 | `hard_acceptance`의 짧은 label만 고립해 `GOAL_ACCEPTANCE_MISSING`을 냈다. 같은 입력에 `add(2, 3) == 5`, 실제 실패 구현·정확한 test·최소 변경 정책이 제공됐다. seed 43·89는 정상으로 허용했다. | 전체 제공 문맥을 놓친 **모델 의미 판단 실패**다. 기존 부분 발췌 해석 지침과 고정 정상 oracle을 유지하며 다음 의미 판단 보완 대상으로 남긴다. |
| G07-adversarial / seed 43 | 명시된 `source_traces: []`에 대한 `GOAL_TRACE_GAP` 없이 `GOAL_INVENTED_REQUIREMENT`만 냈다. | **모델 finding 누락**이다. recall 진단에 반영하며 별도의 critical admission 실패로 확대하지 않는다. |

P11의 근거는 고정 v9의 [공통 draft](D:/codex/fm-pre10-validation-v9/src/flowmarshal/engine/goal.py:142), [실제 요청과 사후 validator](D:/codex/fm-pre10-validation-v9/src/flowmarshal/engine/qualification.py:665)다. G01·G07은 [고정 Goal fixture](D:/codex/fm-pre10-validation-v9/tests/fixtures/engine/goal-reviewer-regressions.json:6)와 [개별 누락의 평가 규칙](D:/codex/fm-pre10-validation-v9/src/flowmarshal/engine/evaluation.py:376)을 대조했다. 원시 응답을 고쳐 다시 평가하지 않았다.

후속 개발 source에는 generic 전용 `GenericFixtureFindingDraft`·`GenericFixtureReviewDraft`를 추가했다. `affected_task_refs`의 `maxItems: 0`을 provider schema와 typed validator 양쪽에 적용하고 역할 지침에도 빈 배열 규칙을 명시했다. 실제 Goal·Plan의 `ReviewDraft`와 Task ref 기능은 유지한다. 금지된 non-empty 응답의 차단과 정상 empty 응답의 기존 Core 판정을 회귀로 확인했다. 이 수정은 prompt/schema·source digest를 바꾸므로 **새 실제 qualification이 필요**하다. 수정 후 모델 호출은 이번에 하지 않았다.

후속 수정은 `D:\codex\fm-pre10-validation-v10`의 HEAD `00f341af30f34f23362fbede65f42081ac0324e0`, source manifest `sha256:c543d2d1fa9cdad379cd9d8fd4e58c58faeff62a6beed41bb6d45db8b289cbad`로 다시 동결했다. **Gate 5/5·전체 테스트 826개 / 113.639초·legacy 40개 변경·누락 0**을 통과했다. [v10 결정적 보고서](D:/codex/fm-pre10-validation-v10/.flowmarshal-engine-eval/runs/pre10-role48-followup-devgate-20260906/qualification-report.json)의 contract는 `sha256:d7ad0188a6ac032999723ceb59b39e2ce9c8ad797c5d55d425110548fa7420e4`, report digest는 `sha256:d091be087c840eb08037d2013029bc07dcd9ef6f98cb826104c587d069fc69fe`다. 아래 실제 48건 수치와 계약은 계속 **v9**의 결과이며 v10 실제 PASS로 재사용하지 않는다.

## 사용량과 예산

| 항목 | 확인값 |
|---|---:|
| provider 호출 / receipt | 48 / 48 |
| 사용량 확인 / 미확인 | 48 / 0 |
| 입력 token | 1,320,603 |
| 출력 token | 12,121 |
| **실측 총 token** | **1,332,724** |
| 입력에 포함된 cached input token | 466,048 |
| 정산 완료 / 미정산 예약 | 48 / 0 |
| receipt latency 합계 | 569.921초 |
| 호출 latency 중앙값 / 최소 / 최대 | 10.539 / 7.765 / 44.828초 |

실제 48회는 모두 `gpt-5.6-sol/xhigh`이며 general/critical Reviewer 역할을 사용했다. 다른 역할의 기본 모델은 운영 설정에 결속했지만 이번 48-cell에서 호출하지 않았다. 모든 호출의 token·latency가 관측됐으므로 이 실행의 실측 총량은 `null`이 아니다. 과거 진단 Goal의 미확인 호출·100,000 token 잠정 차감은 이 실행과 합산하지 않는다. token 수는 Codex 구독 한도 차감량을 뜻하지 않는다.

사용자가 승인한 **Goal별 1,000,000 token·호출 예약 100,000·reserve 25%**를 각 cell의 새 Goal 원장에 적용했다. 과거 완료 Goal의 `2m/200k` override는 승계하지 않았다. 이번 호출은 모두 예약량 이내였고 unknown 조정·추가 증액이 없었다. campaign 합산 cap을 구현하거나 여러 Goal 상한의 합계를 실제 최대 사용량으로 보장한 것은 아니다. latency 수집도 별도 성능 36-cell Gate를 대신하지 않는다.

## 고정 source와 계약

| 항목 | 결속값 |
|---|---|
| 고정 작업본 | `D:\codex\fm-pre10-validation-v9` |
| detached HEAD | `2952c0431c817d9f4e1e1e10d66b7dc7f228b6f6` |
| source manifest | `sha256:452543ecaddb4942bdb62a4dce295b129c9d8c96bb2fe932fe2bc14aa922247e` |
| 역할 48 evaluation contract | `sha256:a98d0f0805f4abe8063928e5ae03aa540454fc8b6631d1738410bf00cddb61a5` |
| 역할 설정 | `sha256:0ca70f5002e0254e36f2fe7709080e2a15977cfa75c355cdc48c08f8222a1418` |
| model lock v2 | `sha256:9cb6e554b5ffbab137cbfe643f8e3d67c0b81f3963dd26fcf33f08d1cde5a005` |
| 예산·timeout 정책 묶음 | `sha256:5aa5f5bb60a4a989f41d5fb85c28edb4e1722922deee86703ca32a14a45f7036` |
| Codex executable | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |

source·역할·fixture·prompt/schema·taxonomy·threshold·정책을 호출 전에 고정했다. 실제 역할 실행 중 source 변경은 없었다. `model/list` 관측과 요청·receipt의 지원 model/effort·실행 권한·저장형 thread 결속을 남겼다. 실행 시작은 18:59:09 KST, 48건 완료는 19:08:44 KST다.

결정적 Gate는 v9에서 **5/5**, 전체 테스트 **824개 / 113.141초**, legacy 동결 **40개 / 변경·누락 0**으로 통과했다. 실제 역할 실행기의 자동 preflight도 통과한 뒤 모델 호출을 시작했다. Gate 원본은 [결정적 보고서](D:/codex/fm-pre10-validation-v9/.flowmarshal-engine-eval/runs/pre10-role48-preflight-devgate-20260906/qualification-report.json)이며 contract `sha256:1f1bfd2e69b9aef1d4431004c04dd087b4fdc5208a0e7bfc9bb3fb75fec62437`, report digest `sha256:1e2fb5bbaa5a65427908669244af979d4b0fead51b413ae4b7ec3b82dfaa3356`다.

## 호출 전에 수정한 실행기 문제

실제 48건 전의 두 실행 실패는 모두 provider 호출 **0회**였고 원본을 보존했다. 이 실패들을 모델 품질 실패나 실제 호출 48회에 포함하지 않는다.

| source | 실패와 수정 | 보존 위치 |
|---|---|---|
| v7 | inventory의 `OperationalBinding`이 `EngineModel` 하위가 아니어서 JSON 저장에 실패했다. `_write_json`이 Pydantic `BaseModel`을 처리하도록 고치고 실제 binding의 typed round-trip 회귀를 추가했다. | `D:\codex\fm-inspection-runtime\qualification-4dcd03d-e91658eb-20260906` |
| v8 | 자동 preflight의 중복 usage 회귀가 동일 시각·무작위 ID 순서 때문에 불안정했다. 선행/후행 기록의 시각을 명시하고 입력 순서 양방향을 검사했다. production 집계 규칙은 바꾸지 않았다. | `D:\codex\fm-inspection-runtime\qualification-a9b4848-role48-20260906` |

각 실패 뒤 새 source·새 run root로 동결하고 결정적 Gate를 다시 통과했다. 원래 48건 승인, 동일 정책·fixture·역할, 호출 전 실패 증명은 최종 실행의 [precall retry binding](D:/codex/fm-inspection-runtime/qualification-2952c04-role48-20260906/role48-launch/precall-retry-binding.json)에 연결했다. v7의 오래된 `RUNNING` 상태를 성공으로 고치지 않고 별도 프로세스 종료 receipt와 0-call 감사로 설명한다.

## 원본과 후속 경계

실행 root는 `D:\codex\fm-inspection-runtime\qualification-2952c04-role48-20260906\10-role-fixture`다. 불변 `cells`, 역할 요청·진행·receipt, 각 cell의 schema 3 예산 원장과 History가 이 경로에 있다. 원장과 실행 로그는 Git에 넣지 않는다.

- [역할 상세 보고서](D:/codex/fm-inspection-runtime/qualification-2952c04-role48-20260906/10-role-fixture/role-qualification-report.json): bytes SHA-256 `b2d6d83883a379e6590278f71595430d7341e3029fbd71f11c863f4a384237e6`
- [scope 보고서](D:/codex/fm-inspection-runtime/qualification-2952c04-role48-20260906/10-role-fixture/qualification-report.json): bytes SHA-256 `3c461e9f3b560249c59f37bb28368bc7591a53fa9264ace176b3419b300d23be`
- [프로세스 종료 receipt](D:/codex/fm-inspection-runtime/qualification-2952c04-role48-20260906/role48-launch/completion.receipt.json)
- [실측 요약](D:/codex/fm-inspection-runtime/planning-continuation-20260906/role48-result-summary-v9.json)
- [독립 읽기 전용 감사](D:/codex/fm-inspection-runtime/planning-continuation-20260906/role48-audit-v9.json): bytes SHA-256 `55068d1e6b92ad538df09c8668ca13791cdda93c840bf52249b6bc0f59c85ac6`

독립 감사는 16 fixture×3 seed의 완전성, source·계약·모델·정책 결속, provider/checkpoint/usage의 48/48/48 대응, 중복·unknown·예약 잔여 0, History chain 48/48을 확인했다. 고정 oracle·threshold로 보고서를 재계산한 결과도 원본 FAIL과 같았으며 감사 무결성 오류는 0개다. 감사 성공은 역할 품질 Gate의 PASS를 뜻하지 않는다.

다음 순서는 실패 응답과 입력 계약의 원인 보완 → 새 source·계약으로 역할 48 전수 재검증 → Planning 18 → 실제 E2E 4 → 성능 36이다. 이번 FAIL·원시 응답·oracle·합격선을 보존하며 실패 cell만 교체해 이전 실행을 PASS로 바꾸지 않는다. 이후 실제 호출은 이번 48회 승인에 포함하지 않는다. 기존 같은 Goal의 `satisfied` 결과도 여러 source의 진단·복구 근거로 유지하고 v9 fresh E2E로 승격하지 않는다.
