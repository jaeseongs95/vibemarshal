# R-S06-14 Gate0C 이벤트 식별·qualification source 결속 인계

## 판정

**Gate0C 재시험 이벤트 식별과 결정적 qualification source 결속 수정은 완료했고, 최종 결정적 Gate는 5/5 PASS다. 제한 실제 진단은 첫 `clean` 사례의 의미 관계표 불일치로 FAIL했으며, 전체 qualification과 `flowmarshal` 1.0 cutover는 계속 NO-GO다.**

실제 모델 호출은 첫 실패에서 중단한 **논리 호출 1회 / provider turn 1회**, schema recovery 0회다. Plan 활성화, Worker 실행, 운영 SQLite·파일 원장 생성, 전체 S06와 나머지 실제 qualification은 수행하지 않았다.

## 권한과 동결 경계

작업 시작 전 실제 세션 정책이 전체 액세스(`danger-full-access`와 동등한 unrestricted filesystem), `approval_policy=never`임을 확인했다. 진단 preflight와 실제 thread receipt도 각각 `:danger-full-access`, `never`를 기록했다.

`config/legacy-freeze-manifest.json`의 동결 roots는 `src/flowmarshal/core`, `src/flowmarshal/orchestration`, `src/flowmarshal/planning`, prototype Planner skill, R3.1 campaign 10 artifact다. `src/flowmarshal/gate0c/**`와 `tests/test_gate0c_e2e.py`는 동결 40개에 포함되지 않고 R3.1 campaign 이후 별도 Gate 0C 경로로 추가됐다. 따라서 이번 Gate0C 직접 수정은 R1~R3.1 legacy freeze 위반이 아니다. 최종 freeze 검사는 manifest digest `sha256:25f21e8d09fb20f1aa0b3d28f5e1946dc4423aff0c5edf7c23e77c7f62bd1f5a`, 40개 모두 PASS다.

## 구현

### Gate0C correction 이벤트

`src/flowmarshal/gate0c/e2e.py`의 `append_retest_correction()`을 다음과 같이 보완했다.

- 안정적인 `attempt_fm0c1_<label>_20260902`를 correction의 READY·ACTIVE·FAILED `detail.retest_attempt_id`에 동일하게 결속했다.
- Attempt를 상태 전이보다 먼저 원장에 생성해 상태 event가 실제 의미 식별자를 참조하도록 했다.
- 같은 label의 Attempt 또는 evidence가 이미 있으면 첫 상태 event 전에 `RETEST_ATTEMPT_ALREADY_EXISTS`로 중단한다. 중복 correction이 READY·ACTIVE만 부분 append한 뒤 PK 충돌하는 경로를 닫았다.
- `task_events.row_digest` 계산, `UNIQUE(row_digest)`, append-only trigger, history hash chain과 verifier는 약화하거나 변경하지 않았다.

`tests/test_gate0c_e2e.py`는 모든 `_now()`가 같은 고정 시각인 상태에서 동일 Task의 합법적인 두 correction이 각각 `FAILED → READY → ACTIVE → FAILED`로 실행되는지 검증한다. 두 correction의 READY·ACTIVE·FAILED digest가 각각 다르고 canonical 재계산과 history chain이 일치하며, 동일 correction 중복은 행 수 변화 없이 거부되고 task event 직접 변조는 append-only trigger가 거부하는 것도 확인한다.

DB schema revision과 authoritative contract는 바뀌지 않았다. 기존 ledger를 다시 쓰거나 migration을 추가하지 않았고, `INSERT OR IGNORE`, 무작위 salt, 합성 시각, 예외 무시는 사용하지 않았다.

### qualification source manifest

`src/flowmarshal/engine/qualification.py`의 manifest를 다음 현재 입력 전체로 확장했다.

- 모든 `src/**/*.py`
- 모든 `tests/**/*.py`와 `tests/fixtures/**/*`
- `scripts/diagnostics/*.py`
- qualification config 3개, 권위 문서 3개, `AGENTS.md`, `pyproject.toml`

정렬된 상대 POSIX 경로와 raw bytes SHA-256 map을 기존 canonical digest에 넣는다. symlink, `.venv`, cache, `.flowmarshal-engine*`, DB 및 sidecar, `.env`, 일반 auth/credential/secret/token 파일과 개인 key 형식은 제외한다.

`tests/test_engine_qualification.py`는 88개 `src/**/*.py`와 모든 `test_*.py`, Gate0C fixture/config 및 다음 네 파일 포함을 검사한다.

- `src/flowmarshal/gate0c/ledger.py`
- `src/flowmarshal/gate0c/e2e.py`
- `src/flowmarshal/canonical.py`
- `tests/test_gate0c_e2e.py`

각 파일을 독립적으로 정확히 1바이트 변경했을 때 file digest, source manifest digest와 결정적 contract digest가 모두 바뀌고 복원 후 원래 digest로 돌아오는지 확인한다. 제외된 credential·DB·sidecar·cache·평가 artifact 변경은 digest를 바꾸지 않는다.

### 진단 launcher 결속

`scripts/diagnostics/r_s06_10.py`가 새 `r-s06-14-*` root를 허용하고 preflight session을 `R-S06-14`로 기록하도록 했다. R-S06-13의 v4 fixture·기대표·provider 계약은 그대로 재사용하되 현재 source에 새로 결속한다.

## 결정적 검증

최종 source에서 다음이 통과했다.

| 검사 | 결과 |
|---|---|
| 고정-clock Gate0C + qualification 회귀 | 22 tests, PASS |
| Gate0C 전체 | 47 tests, PASS |
| 영향 회귀 | 43 tests, PASS |
| 전체 unittest | 552 tests, PASS |
| `compileall` | `src`, `scripts`, `tests` PASS; bytecode cache는 workspace 밖 TEMP 사용 |
| `pip check` | `No broken requirements found.` |
| legacy freeze | 40개, PASS |
| `git diff --check` | PASS |

최종 결정적 Gate root는 다음이다.

`D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-14-20260905-v1\deterministic`

- Gate: 5/5 PASS, failure 0
- full-suite cell: 552 tests, PASS
- source manifest: 207개
- source manifest digest: `sha256:482028c9b753b85e4fe77837fa608d61c1d99c37560d6c8398803fbe12b84db5`
- contract digest: `sha256:301fdfa99b1083805087c40b630b4b37a7ba038adff7354e930106a4bcacbc66`
- report canonical digest: `sha256:87de55bb6f5e5e640062a815d926e0822fc5dcdb1d69ca70f10d6d29013725bd`
- report file bytes SHA-256: `6ee29fdef4c2669207e4f4bcb1f9d0d1049408eb3ed6fa8ad8f35fb095fa1879`

manifest의 직접 관련 file digest는 다음과 같다.

| 상대 경로 | digest |
|---|---|
| `src/flowmarshal/gate0c/ledger.py` | `sha256:d613685694dbc9269eea02c50f8800b737c695edfe2a63c3a29304f58bca485d` |
| `src/flowmarshal/gate0c/e2e.py` | `sha256:24aa19ce5ade938c7cd9e698870b26385119d77a71bfe21e61bb9f8268333280` |
| `src/flowmarshal/canonical.py` | `sha256:64b74f58b66f513bdcd124d49c8cc9da64eb1df28182225d502cdede10f09282` |
| `tests/test_gate0c_e2e.py` | `sha256:a16446104369ca3410232e7e6977dcf7ca23d54df2121a6c407277e0f57a5263` |
| `scripts/diagnostics/r_s06_10.py` | `sha256:725bf84b0a410c96d64802bb42acea76b3f806cd4baf7369feabab7114595c49` |

`r-s06-13-20260905-v2`에서도 Gate 5/5가 한 번 통과했지만, 이후 발견한 진단 session 표기와 R-S06-14 root guard를 수정해 source digest가 바뀌었다. 이 중간 Gate는 보존하되 현재 진단의 근거로 재사용하지 않았다.

기존 실패 root `r-s06-13-20260905-v1`은 수정·resume·overwrite하지 않았다. 기존 `qualification-report.json` bytes SHA-256은 `52094082c1c15efe25bb9c9873631100d9b0d768b485de311a3a8f3f917a8f11`, 실제 실패 cell `cells/seed-0/7d4dcc7b1f24fd4291a0fa86.json`은 `1ddf5171464f39cf6ec29270f8fb6c87d0908d85d29e888b9d5ef0303727195e`로 작업 전후 동일하다. 기존 report canonical digest `sha256:b5e4afd12348f620c41d7a94941b2a7fb491139ba4edf335145c448307089f63`과 0/13 호출 실패 의미도 보존했다.

## 제한 실제 진단

진단 root는 다음이다.

`D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-14-20260905-v1`

처음 파일 경로 방식으로 launcher를 실행했을 때 provider 효과 전에 `ModuleNotFoundError: No module named 'scripts'`로 준비가 중단됐다. `preparation-failed.json`을 삭제하거나 덮어쓰지 않았으며 provider turn은 0회였다. 같은 source·root에서 정식 module 방식 `-m scripts.diagnostics.r_s06_10`으로 준비를 수행했고, 이 실패 기록도 preflight locked files에 포함했다.

preflight 결과:

- lock digest: `sha256:37233b781677f02f4eb1c1b070f1ada18a0cf03eebbe8db3e6227784129c1e8a`
- source snapshot: 207개, 현재 manifest와 완전 일치
- inventory digest: `sha256:82e6bbcba85b38800c736c9f9809a493fbc9b3270cb53b514721a80839a49f14`
- model lock digest: `sha256:5cca3c82ad7d7eb4e61aea0dba9e2101a465860b8abe1ec771538bc7d13762ec`
- general Reviewer: `gpt-5.6-terra/high`
- critical Reviewer: `gpt-5.6-sol/xhigh`
- Expander: `gpt-5.6-luna/high`
- 호출 순서 13개와 logical/provider 상한 각 13, schema recovery 0
- 실제 지침 source digest: 전역 `sha256:6b1b3dbf5dc74c9b077f4c73099d4c54c1dc31b85376b19eafac7a5d5ead77ba`, 제품 `sha256:851cf0f2882e32b438431a649d4f830b67fe010214c758dd182278f4a5e3dca7`, workspace `sha256:57fb50121f05548961e1e725e5537526b4e9cbc754383f8d42b0162096944b2a`

R-S06-13 v4 입력은 변경하지 않았다.

- 기대표 bytes: `f132337b8b237e2379c6315a3b12ff18bfd5d24f8d62be6e29608b8bf963b011`
- 독립 검토 bytes: `7b664981550bd50d53f19345052da0f92410e44f44458a9eb55fb3b291a3bd2d`
- 합성 정상 fixture bytes: `9a13d34bb921d20e87b624d56b8470ea741e371cd4a749090445b2008d751296`

### 첫 호출 receipt

- 사례/역할: `clean` / `compact_plan_reviewer`
- 실제 turn 배정: `gpt-5.6-terra/high`
- call: `model_call_37188cdb481f4aa4aaf24d05fdc88e4a`
- thread: `01a06dd9-a937-7402-bdc8-b5b8db91f683`
- turn: `01a06dd9-acb2-7472-9bd8-6f341b67b739`
- request canonical digest: `sha256:eb3687518c399afbae60c91993ff475084f9671d9935f71f251f70401cd7a848`
- prompt digest: `sha256:0f356e90b1c76532d608fc3f8a0c8a469b28a8be6047fce7e4d5b5662b98c4a0`
- actual schema digest: `sha256:f86335a9fea92c2c200ea22487bac74d61d221339e3947fc88e988cdef0de701`
- output digest: `sha256:e26a5b5157943022df4cf7278d098848648e4a0474cf492dfbcf74220e4e492c`
- receipt canonical digest: `sha256:de1537a319743f800ea1c5ab61c3f1ab572937d6c294a3735c43e31e984a1b49`
- binding verification: instructions, model/effort, payload, prompt, receipt, schema, thread/turn 전부 PASS

thread/start의 빈 thread 기본 관측에는 `reasoningEffort=max`가 표시됐지만, 실제 turn intent·turn receipt·model call receipt와 고정 역할 배정은 모두 `high`이며 binding 검증이 통과했다. 이를 effort 변경이나 fallback으로 처리하지 않는다.

### 의미 판정

provider 응답은 strict schema를 통과했고 finding 0개, 다섯 rating 모두 4였다. 28개 AC×validation pair 집합과 finding/rating 상호배타성도 맞았다. 그러나 사전 고정표와 다음 11개 relation이 달라 공식 의미 평가가 FAIL했다.

| AC | validation | 기대 | 실제 |
|---|---|---|---|
| `ac_001` | `val_task_scope_preservation` | `global_constraint_only` | `optional_or_unrelated` |
| `ac_001` | `val_task_validator_review` | `global_constraint_only` | `optional_or_unrelated` |
| `ac_002` | `val_task_add_behavior_contract` | `explicit_procedure` | `optional_or_unrelated` |
| `ac_002` | `val_task_scope_preservation` | `global_constraint_only` | `optional_or_unrelated` |
| `ac_002` | `val_task_unittest` | `global_constraint_only` | `optional_or_unrelated` |
| `ac_002` | `val_task_validator_review` | `global_constraint_only` | `optional_or_unrelated` |
| `ac_003` | `val_goal_independent_behavior_contract` | `explicit_procedure` | `optional_or_unrelated` |
| `ac_003` | `val_task_add_behavior_contract` | `explicit_procedure` | `optional_or_unrelated` |
| `ac_003` | `val_task_scope_preservation` | `global_constraint_only` | `optional_or_unrelated` |
| `ac_003` | `val_task_validator_review` | `global_constraint_only` | `optional_or_unrelated` |
| `ac_004` | `val_task_add_behavior_contract` | `explicit_procedure` | `optional_or_unrelated` |

첫 의미 실패에서 즉시 중단했고 `bad`부터 `expanded-review`까지 남은 12회, 생성 결과의 독립 정상성 대조·13번째 실제 Reviewer는 실행하지 않았다. rating·coverage·인용 자동 교정, validator 완화, 사후 oracle/threshold/기대값 수정, model 교체나 재호출은 하지 않았다.

### 사용량과 비용

| 항목 | 값 |
|---|---:|
| 논리 호출 / provider turn | 1 / 1 |
| input tokens | 44,016 |
| cached input tokens | 0 |
| output tokens | 6,898 |
| reasoning tokens, output에 포함 | 3,353 |
| total tokens | 50,914 |
| 역할 latency | 128,375ms |
| provider duration | 127,324ms |
| schema recovery | 0 |
| 청구 금액 | 미제공(null) |

usage는 새 빈 thread의 첫 turn에서 `thread/tokenUsage/updated`의 thread scope로 관측했다. reasoning token을 total에 다시 더하지 않았다.

## 주요 증거

| 파일 | bytes SHA-256 |
|---|---|
| `summary.json` | `8b15196685080a00c03ab7a33299597bac695bda52b2435220a421639bbb986d` |
| `preflight.json` | `db3e5e088edec5ce1087eb4b85b961894f3dd54da4116e91e99cd67fd86d9113` |
| `executed-source-manifest.json` | `1da8ef3be56a5fc64399bd3f79a3de92c21ec4694d1d11261b50c74b7f818803` |
| `deterministic/qualification-report.json` | `6ee29fdef4c2669207e4f4bcb1f9d0d1049408eb3ed6fa8ad8f35fb095fa1879` |
| `deterministic/evaluation-contract.json` | `b171352e36e78d196e495130cc03b40121cc68fb20c7b8d645065fc7b4fc6b64` |
| `clean-assessment.json` | `5472bab095eea155cfa0cddebf731b9c1ce2188161668884acd76bd50f4596c2` |
| `clean-review.json` | `49cbfa3b394ec6d7df5bc9442e3c309b34e3adabb81afa1c16ae8ed8aae27c2e` |
| `calls/01-compact_plan_reviewer/request.json` | `4bf21c0e24873d40c2594ffc1cb470026d5379ef1942a0d4ec9b7f29968d8188` |
| `calls/01-compact_plan_reviewer/strict-schema.json` | `21ad1039731a139ed2d0dfe5b09b0e953fb2df32fd294715fe25b22a470090ab` |
| `calls/01-compact_plan_reviewer/thread.receipt.json` | `6e3345c9d6e6e4e09c6d7b31c1a09cde713b10a36443203b16af32f5c5e989aa` |
| `calls/01-compact_plan_reviewer/turn.receipt.json` | `7f6d438838f5e06418a8347f8d9d92473b4111b8c86a8cc8968054073336f6fe` |
| `calls/01-compact_plan_reviewer/terminal.json` | `3cb766c0fc221104b5dbb7da1826377a591907365833b008a5d09c0abe3d7976` |
| `calls/01-compact_plan_reviewer/result.json` | `66a964cf1f0425ec6833c9c0aa9c5bd21af67d52ad65acd836fab3bc4582875d` |
| `calls/01-compact_plan_reviewer/binding-verification.json` | `daed304e230484c96ba099ba7a80e844f3ed4fcedd60ab4bc8bcbc3499914758` |

모든 경로는 진단 root 기준 상대 경로다. 실행 후 `verify_lock()`은 같은 preflight digest로 통과했고 source·workspace·기존 원본 보존, 첫 호출의 현재·이전 binding 검사가 모두 true였다.

## 잔여 문제와 다음 조건

Gate0C 이벤트 식별과 source manifest 결함은 닫혔다. 남은 직접 결함은 clean Plan의 AC별 명시 절차와 전역 Task 검사 의무를 Reviewer가 11개 행에서 `optional_or_unrelated`로 축소한 의미 분류 실패다.

다음 실제 진단은 이 관계 분류의 직접 원인을 별도 source 변경으로 해결한 뒤에만 가능하다. 새 source digest, 전체 결정적 Gate 5/5, 새 root, v4 입력·기대표·지침·schema·inventory의 새 preflight lock이 필요하다. 현재 run의 남은 호출을 resume하거나 같은 배정으로 재호출하지 않는다.

이 보고서는 R-S06-14의 구현과 제한 진단 인계다. 전체 실제 역할 Gate, 전체 Skeleton-to-selection pipeline, 실제 프로젝트 activation-to-recovery E2E, token/latency Gate와 `flowmarshal` 1.0 cutover 승인을 대신하지 않는다.
