# R-S06-21 모델 잠금 v2 이후 제한 실제 역할 검증 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 최종 판정

**FAIL — fresh v2 prepare는 성공했으나 첫 clean의 inventory 기록 단계에서 `FileExistsError`로 중단했다.** 과거 R-S06-20의 비관련 `gpt-5.4` 삭제 차단은 실제 prepare 경로에서 제거됐다. 실제 역할 thread 생성·logical role call·provider turn은 각각 **0건**이며, 현재 source의 실제 역할 의미 검증은 **NOT_RUN**이다.

첫 실패 이후 prepare·run 재실행, 모델 교체, fallback, schema recovery, 다른 root 생성, 입력·코드 수정은 하지 않았다. 기존 S06 FAIL, Functional Alpha 미완료, 1.0 NO-GO는 유지한다. 다음 제품 단계 진입 조건은 충족하지 못했다.

## 권한·시작 상태와 고정 계약

첫 파일 접근 전에 현재 turn의 개발자 `permissions instructions`에서 실제 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인해 응답에 남겼다. 부모가 전달한 기대값으로 대체하지 않았다. 제품 runtime의 fresh prepare도 `:danger-full-access/never`를 관측했다.

- 시작 HEAD·로컬 main·원격 main: `86f39014b0f68a8198131c5cc60a3bad05d0669b`. 작업 트리 clean.
- 권위 origin: `https://github.com/jaeseongs95/flowmarshal.git`. 프로젝트 지침상 private 원격이며 저장소 공개 설정은 변경하지 않았다.
- 새 root는 최초 조회에서 존재하지 않았다. 이후 이 세션의 감사 파일부터 생성했다.
- 고정 계약 root: `.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2`.
- 이전 R20 root는 provenance로만 읽었으며 재개·수정하지 않았다.
- 새 root: `.flowmarshal-engine-eval/runs/r-s06-19-post-lock-v2-r21-20260905-v1`.

이하 상대 artifact 경로는 새 root 기준이다. 기존 `r-s06-19-` 접두사와 preflight의 `session=R-S06-19`는 수정하지 않은 공통 진단기의 식별 형식이다. 이번 작업의 식별자 R-S06-21은 `assignment.json`에 별도로 기록했다.

새 prepare가 만든 Goal·State·Plan 입력, `roles.json`, `expectations.json`, `independent-fixture-review.json` **26파일을 R19 대응 원본과 바이트 단위로 대조해 모두 일치**했다. static 사례 11개의 expectation input binding도 직접 검사했다. oracle·threshold·taxonomy·사례 의미와 순서는 변경하지 않았다. Prompt·Schema·instruction 본문은 현재 source와 fresh preflight에 결속했으며 과거 v1 checkpoint를 재해석하거나 migration하지 않았다.

제품 역할은 기존 설정 그대로다. 일반 Reviewer는 `gpt-5.6-terra/high`, expander는 `gpt-5.6-luna/high`, critical Reviewer는 `gpt-5.6-sol/xhigh`다. 바깥 개발 세션의 모델을 제품 역할 배정에 사용하지 않았다. 실제 역할 thread 생성에 도달하지 않아 빈 새 thread·실제 instruction receipt 검증은 NOT_RUN이다.

## 결정적 Gate 재사용 근거

구현 인계의 `model-lock-v2-20260905-deterministic-v2` 원본을 직접 읽었다. 현재 `source_manifest_files()`의 **224파일**을 다시 hash하고, 현재 `_deterministic_contract()` 전체 객체와 원본 계약, report의 계약·digest, 완료 상태, 원본 5개 cell의 계약·완료·PASS를 대조했다. 모두 일치했다.

| 결속 | 직접 확인한 값 |
|---|---|
| 현재 source manifest | `sha256:f2543cdcd6d457620e6ea07baa6a0b6c2fd2f32e9ace8c22ecc18982a4d3ed4d` |
| 결정적 evaluation contract | `sha256:5f27761faba2c01b6badcda559901fbeefd8dc71ec7b0801972b970cf3f27b6b` |
| 결정적 report canonical digest | `sha256:ac5794d25e1baf9e443f624f0ed6c1049125925b41743a6325bdad59846eed25` |

Gate 원본 9파일을 `deterministic/`에 바이트 그대로 복사했고 `deterministic-provenance.json`에 원본 경로·파일별 digest·검사 결과·cell 원문을 보존했다. 재사용한 Gate는 전체 571 tests, compileall, pip check, synthetic lifecycle, legacy freeze를 포함한 5/5 PASS다. 이번 세션에서 전체 테스트와 Gate를 새로 실행한 것으로 표현하지 않는다. 재사용 판정은 요청된 source·contract·cell 대조에 근거하며 이전 Python 실행 환경 전체의 동일성까지 새로 입증한 것은 아니다.

## 실제 prepare와 첫 실패

cwd는 제품 root, 실행 interpreter는 `.venv\Scripts\python.exe`다. `session_audit.py`가 아래 두 명령을 각각 한 번 실행하고 명령·시각·stdout·stderr·종료 코드를 보존했다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 prepare --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-lock-v2-r21-20260905-v1
.\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 run --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-lock-v2-r21-20260905-v1
```

| 단계 | KST 관측 | 결과 |
|---|---|---|
| prepare | 10:36:47.266~10:36:52.392, 로컬 경과 5.125초 | exit 0, `prepared=true` |
| run | 10:37:44.580~10:37:49.272, 로컬 경과 약 4.687초 | exit 0, **summary status=FAIL** |

run CLI는 예외를 요약에 기록한 뒤 정상 종료하므로 **종료 코드 0을 검증 PASS로 해석하지 않는다.** 위 시간은 로컬 명령 경과이며 모델 역할 latency가 아니다.

fresh inventory에는 R19와 비교해 `gpt-5.4`만 빠졌으며 선택된 역할 model/effort와 Codex executable은 유효했다. prepare가 v2 operational binding을 생성하고 성공했으므로 R20의 `MODEL_OR_EXECUTABLE_LOCK_CHANGED` 차단은 제거됐다고 판정한다. 이후 실제 role call 직전의 operational 검증은 inventory 기록 오류 때문에 완료되지 않았다.

| fresh 결속 | 값 |
|---|---|
| format | `flowmarshal-model-lock-v2` |
| preflight canonical lock | `sha256:55a89e25933619d2fdfa172156a3da8ef89dc004816efa36a58dddfbdef65cc6` |
| operational lock | `sha256:ef7d5b53bc35fc500f0ce0eb046bc8d9984bf8b042aa416a3be5f221db8cee83` |
| 전체 inventory 감사 digest | `sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2` |
| Codex executable | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |
| 역할 설정 | `sha256:ba683966a19b9cc249ef6117af7df5430979ad1907a631f5b8cd54d85a97eeb6` |
| Prompt / Schema | `sha256:79d3a4ce1160489c3b937a150fcadca7309526d1df9bb4204ed5c8d587f366cf` / `sha256:a4b850d73a3e040f7f769208206fffbca530ef65de3487999c2f4625dc6bca7c` |

첫 실패는 `runtime-preflight/inventory-01.json`에 대한 `FileExistsError: [Errno 17] File exists`다. 직접 경로는 `scripts/diagnostics/r_s06_10.py`에서 확인했다.

1. `CapturingRuntime.__init__()`의 112~113행은 capture를 `run/runtime-preflight`로 지정하고 `indices={}`로 초기화한다.
2. prepare의 runtime(290행)이 첫 `model/list` 결과를 `inventory-01.json`에 기록한다.
3. 별도 run의 runtime(567행)도 같은 경로와 빈 indices로 시작한다. 첫 `clean`의 `RecordedRunner.run()` 374행이 다시 `list_models()`를 호출한다.
4. 126~129행의 `list_models()`는 실제 metadata 결과를 받은 뒤 `record()`를 호출한다. 117~119행에서 파일 번호를 다시 1로 정하고, 40행의 exclusive `open("x")`에서 충돌한다.
5. 오류는 호출 capture 생성(386행), `CodexStructuredRoleRunner.run()`, thread intent와 provider turn 이전에 발생했다.

최소 해결안은 **fresh prepare와 run 사이에도 충돌하지 않는 inventory 감사 기록 식별자**를 사용하는 것이다. 기존 파일을 덮어쓰거나 지워서는 안 되며 과거 raw·잠금은 그대로 보존해야 한다. 기존 기록을 고려한 append 전용 번호 배정 또는 단계별 별도 경로를 검토하고, 같은 root의 `prepare → run`에서 기록이 모두 보존되며 첫 역할 직전 검증에 도달하는 결정적 회귀가 필요하다. effect intent가 있으면 재실행을 막는 기존 규칙도 유지해야 한다. 이번 세션은 코드 수정 권한 범위가 아니므로 구현·fixture·역할 설정을 변경하지 않았다.

## 호출 상한·관측 한계

고정 순서 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`의 **13사례 모두 역할 실행 NOT_RUN**이다. clean은 로컬 사전 검사에만 진입했다.

- logical role calls **0/13**, provider turns **0/13**, schema recovery **0**.
- `calls/` 자체가 없으며 thread/turn intent·receipt, terminal, role result 모두 0건이다. 재조회할 미확정 역할 effect가 없다.
- prepare 정책 관측·전체 inventory 원문은 `runtime-preflight/policy-01.json`, `inventory-01.json`에 보존돼 있다.
- run의 두 번째 `model/list`는 반환됐지만 기록 충돌로 **그 원문은 보존되지 못했다**. 현재 prepare 원문으로 대신하거나 새 조회로 메우지 않았다. 따라서 모든 inventory raw 보존 요구는 이 구현 결함 때문에 완전히 충족되지 않았다.
- 실제 role receipt·binding verification·usage·provider duration·청구 금액은 **null/NOT_RUN**이다. 원본 `summary.json`의 `usage.latency_ms=0`은 빈 receipt 합계이며 실측이 아니다. 원본은 수정하지 않고 `limited-validation-outcome.json`에서 역할 latency를 null로 명시했다.
- expansion에 도달하지 않아 `generation-assessment.json`과 13번째 호출은 만들지 않았다.

Plan activation·Worker·ledger write·전체 qualification·S07 이후·cutover는 실행하지 않았다. 다른 작업에 메시지·callback·예약을 보내지 않았으며 새 보조 역할 작업이나 대기 루프를 만들지 않았다.

## 무결성·증거·세션 마감

이번에는 다음 관련 결정적 회귀를 직접 실행해 **27 tests PASS**, exit 0을 확인했다. 이 테스트의 통과가 발견된 prepare/run 충돌의 회귀 검증을 대신하지는 않는다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_engine_model_lock tests.test_engine_model_lock_consumers tests.test_engine_inspection_case_binding -q
```

최종 `source-after.json`은 source·고정 입력·instruction 잠금, 과거 artifact와 원본 Gate 보존, legacy freeze **40파일 PASS**, 허용된 문서 외 기존 추적 파일 불변, `git diff --check` 결과를 기록한다. `evidence-manifest.json`은 새 run의 파일별 bytes digest를 보존한다. 원본·run artifact는 Git에 넣지 않는다.

| 주요 artifact | bytes SHA-256 |
|---|---|
| `preflight.json` | `e8cce11c72c0ba1424d9cc0a9f61147571c1c3ffb53d3f24d46b5ef3013da925` |
| `deterministic-provenance.json` | `d2fb4f4da106b44a346b87970307a3c08a784eb8c44c52e22e6e8d6cfe0bb392` |
| `pre-run-verification.json` | `4a20a81dd31d691380cf06703b9ff0cf1dc3548cfd69550e6775d4bccbd97d5e` |
| `prepare.completed.json` | `c1af9bd9a419aa2888c9041aab5a8a76b6a15cd3fda2b9b3450cf66008384c8a` |
| `run.completed.json` | `3d4ab0abe54c98317821a2ed7cd712ea69061a692c2021ab68a83479313a2761` |
| `summary.json` | `b56cd08fe8a3b4a91bf4a2905e15dd8685b4c06844fa5edddccb174860f0d29d` |

로컬 감사 보조 스크립트 최초 실행은 `scripts` import 경로 누락으로 exit 1이었다. 새 run 내부 보조 스크립트의 import 경로만 수정한 뒤 감사를 실행했고 이를 `assignment.json`에 기록했다. 제품 prepare·실제 역할 재시도와 구분한다.

저장소 변경은 이 한국어 인계 문서와 현재 경계를 연결하는 README 문단이다. 기존 README가 v2 인계와 다음 제한 검증 상태를 안내하고 있어 R21 결과 링크를 반영했다. 세션 변경만 하나의 한국어 커밋으로 private origin main에 push하고, 실제 commit hash·push 결과·원격 HEAD·clean 상태는 `delivery.json`과 최종 응답에 남긴다.

**다음 경계는 진단 기록 충돌의 구현 보정과 결정적 회귀다.** 이번 FAIL을 지우거나 R21을 자동 재개하지 않는다. 그 경계 이후의 새 제한 검증·제품 다음 단계는 이번 세션에서 시작하거나 예약하지 않는다.
