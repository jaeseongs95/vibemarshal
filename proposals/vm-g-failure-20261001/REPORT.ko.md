# VM G failure/concurrency 격리 제안 결과

기준 `jaeseongs95/vibemarshal@32bb0f9dd9f024045d24487312b50f5b703573a3`에서 동일 bound job의 interrupt가 중복 전달되는 새 결함 하나를 검증했다. 별도 프로세스·SQLite connection·파일 barrier로 (1) 응답 대기 중 경쟁 caller와 (2) 효과 후 sender 강제 종료·restart를 고정했고, 두 경우 모두 기준 합성 효과 2회, 후보 1회였다. authoritative/shared source, branch, 운영 DB/Profile/key, live provider/native effect를 수정하거나 호출하지 않았다. 공식 GO/merge/release 판정이 아니다.

## 소스·실행 identity

- 기준 cwd: `/workspace/vm-g-failure-shard`; HEAD `32bb0f9dd9f024045d24487312b50f5b703573a3`; tracked status 빈 문자열.
- 기준 `src` git tree: `c23da6ac4c4ba43ca9a8009b89a83d7c660520fe`.
- 기준 SHA-256 source manifest: `a1eea39a9823f9b740d624c1ea361277448cc614ef96ed888bac7a39af0139cb`.
- 후보 commit: `null` (미commit). `/workspace/vm-g-artifacts/candidate/src` disposable shadow에만 patch 적용.
- 후보 SHA-256 source manifest: `4a605ef675521762146df6a9e483b4207fc6664230f87a2a8f80149df510a02b`.
- source diff는 `runtime.py`의 전달 직전 claim 연결과 `service.py`의 transactional History claim만. schema·Windows owner lock·model policy 불변.
- diff SHA-256: `616647002d4fcc3641dd0fcd6a9a0e11d8f40fa32cd4e403aa2f234903086741`. harness SHA-256: `df14fcfda7a5b5a462230ace0b8459cbf40a6708044f2768ab60c02ef19ce975`.
- 파일별 기준/후보 hash 전체는 `source-identity.json`.
- Python `3.12.14` (`/workspace/cloud-setup/venv/bin/python`), Node `v24.19.0`, OS Linux/POSIX.
- worker 요청 `gpt-6.1-sol/high`는 `client_requested`. actual model/effort provider 관측은 `null/unknown`; 모델 fallback 없음. 합성 fixture model 이름은 실제 호출·적용 관측이 아니다.
- 실제 `AGENTS.md`를 읽었다. repo `.agents` 없음, workspace `.agents`/`.codex` 비어 있음. 본 shard에 필요한 별도 skill 없음. 한국어 artifact, 권위 분리·unknown 유지·source 분리 계약 준수.

## 검증된 결함과 최소 제안

`begin_runtime_job_interrupt`는 interrupt 요청을 한 번 예약하지만, `_request_bounded_interrupt`는 이미 INTERRUPTING인 경쟁 caller도 receipt가 없으면 `_deliver_bounded_interrupt`로 들어간다. request·receipt 사이의 외부 호출이 SQLite transaction 밖이라 둘 다 호출한다. restart의 `reattach`도 effect 후 receipt 유실에 같은 interrupt를 재전송한다. 기준 case05는 `interrupt_requested=1`, `interrupt_receipt=1`, **provider 합성 journal=2**다. 동일 response digest로 receipt가 dedupe돼 원장 count만 보면 중복이 숨겨진다.

제안은 기존 History에 `runtime_job.interrupt_dispatching` marker를 효과 전에 transaction으로 한 번 기록한다. exact `(thread_id, turn_id)`, INTERRUPTING/CANCELLED 상태, 기존 receipt·marker를 함께 대조한 caller만 보낸다. unbound 요청은 marker 없이 late binding을 기다린다. marker 이전 중단은 이후 caller가 한 번 전달할 수 있다. marker 이후 receipt 유실·crash는 sent 여부 unknown으로 보존하고 재송신하지 않으며 원래 exact turn을 직접 재관측한다. provider terminal·Task 완료를 marker나 interrupt 응답으로 만들어내지 않는다. marker 직후 RPC 이전 중단도 effects=0/receipt=0의 unknown이며 자동 재전송하지 않는다. 따라서 완전 exactly-once나 eventual interrupt delivery 보장이 아니라 at-most-once delivery claim의 격리 제안이다.

## 신규 19 cases

| case | 기준/후보 effect count | 기준/후보 runtime intents | 기준/후보 runtime receipts | 후보 interrupt claim |
|---|---:|---:|---:|---:|
| test_01_two_process_attempt_reservation_has_one_winner | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| test_02_two_process_budget_admission_has_one_winner | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| test_03_two_process_exact_binding_is_idempotent | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| test_04_two_process_conflicting_binding_has_one_winner | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| test_05_interrupt_inflight_response_window_has_one_effect | 2 / 1 | 0 / 0 | 0 / 0 | 1 |
| test_06_killed_interrupt_sender_is_observed_without_resend | 2 / 1 | 0 / 0 | 0 / 0 | 1 |
| test_07_interrupt_response_loss_is_unknown_not_terminal_or_retry | 1 / 1 | 0 / 0 | 0 / 0 | 1 |
| test_08_reservation_without_delivery_recovers_once | 1 / 1 | 0 / 0 | 0 / 0 | 1 |
| test_09_same_intent_key_is_idempotent_across_processes | 0 / 0 | 1 / 1 | 0 / 0 | 0 |
| test_10_same_receipt_is_idempotent_across_processes | 0 / 0 | 1 / 1 | 1 / 1 | 0 |
| test_11_stale_file_after_intent_refuses_effect_marker | 0 / 0 | 1 / 1 | 0 / 0 | 0 |
| test_12_expired_job_after_intent_refuses_effect_marker | 0 / 0 | 1 / 1 | 0 / 0 | 0 |
| test_13_collector_loss_late_binding_preserves_exact_turn_without_terminal | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| test_14_unknown_receipt_reconciliation_never_repeats_effect | 1 / 1 | 1 / 1 | 1 / 1 | 0 |
| test_15_unbound_interrupt_is_delivered_once_after_late_binding | 1 / 1 | 0 / 0 | 0 / 0 | 1 |
| test_16_crash_after_claim_before_send_does_not_invent_delivery_or_retry | NOT_RUN/SKIP / 0 | NOT_RUN/SKIP / 0 | NOT_RUN/SKIP / 0 | 1 |
| test_17_workflow_revocation_after_intent_refuses_effect_marker | 0 / 0 | 1 / 1 | 0 / 0 | 0 |
| test_18_cancelled_late_binding_still_delivers_one_interrupt | 1 / 1 | 0 / 0 | 0 / 0 | 1 |
| test_19_provider_terminal_before_interrupt_starts_no_effect | 0 / 0 | 0 / 0 | 0 / 0 | 0 |

- 기준: 19개 실행, 16 PASS / 2 FAIL / 1 candidate-only SKIP, errors=0, exit `1`. 실제 두 FAIL은 case05·06의 effects=2 assertion이다.
- 후보: 19 PASS / 0 FAIL / 0 ERROR / 0 SKIP, exit `0`.
- 모든 case의 History chain 유효. case별 counts, nonterminal/terminal observations, child exit, barrier 시각은 `base-evidence.json`과 `candidate-evidence.json`.
- case01·02: concurrent Attempt reserve·Budget admission 승자 1, 새 provider effect 0. case03·04: identical binding 멱등/다른 turn binding 단일 승자. case09·10: identical intent·receipt 별도 프로세스 멱등.
- case11·12·17: ready barrier 이후 파일 stale·job deadline·workflow cancellation을 적용해 효과 직전 검사를 거부; effect marker 0, provider effect 0. workflow cancellation을 authorization 철회 API나 실제 AGS key revocation 증명으로 확대하지 않는다.
- case13: collector loss 뒤 late exact binding을 보존하되 terminal은 만들지 않는다. Windows owner loss 증명이 아니다.
- case14: 합성 provider child가 create trace를 durable 파일로 남긴 뒤 `os._exit(23)` (response loss); Core recover_inspect로 unknown을 기록한다. 다른 child는 원래 trace의 response·binding을 직접 읽어 reconcile=True로 receipt 등록한다. 자동 retry/DB repair 없고 효과 1·intent 1·receipt 1. **하위 service reconciliation 진단**이며 provider adapter의 public end-to-end recovery나 Goal 완료 증명이 아니다.
- case15·18: unbound pause·cancelled job의 late binding은 한 번 interrupt를 전달하고 provider terminal을 만들지 않는다. case16: claim 직후 `os._exit(24)`, 다음 caller effects=0/terminal=null, exact stored turn 관측 뒤에만 terminal=completed. case19: 이미 provider terminal인 job은 경쟁 interrupt 호출에도 effects=0.

## commands / cwd / exit / UTC / raw hash

공통 cwd `/workspace/vm-g-failure-shard`. shell invocation은 아래와 같으며 정확한 내부 argv·환경은 각 `*-run.json`에 고정했다.

```bash
/workspace/cloud-setup/venv/bin/python /workspace/vm-g-artifacts/build_candidate.py
/workspace/cloud-setup/venv/bin/python /workspace/vm-g-artifacts/run_suite.py base
/workspace/cloud-setup/venv/bin/python /workspace/vm-g-artifacts/run_suite.py candidate
/workspace/cloud-setup/venv/bin/python /workspace/vm-g-artifacts/run_related.py
/workspace/cloud-setup/venv/bin/python /workspace/vm-g-artifacts/run_followups.py baseline-deadline
/workspace/cloud-setup/venv/bin/python /workspace/vm-g-artifacts/run_followups.py supported-related
```

| run | UTC start / end | elapsed | exit | raw SHA-256 |
|---|---|---:|---:|---|
| base | 2026-10-01T23:32:43.186944+00:00 / 2026-10-01T23:33:17.123747+00:00 | 33.936807s | 1 | `bfd1b4b6cb4c311f6a332563da34615a2c1c321e2c81edd77ad3487ef21a493b` |
| candidate | 2026-10-01T23:32:54.502617+00:00 / 2026-10-01T23:33:31.782697+00:00 | 37.280085s | 0 | `b1f2ba849efe2d573698f14383f7dffe665998703bcbdf8d7c56b6a3c8463c31` |

## 기존 회귀·미지원·실제 오류의 구분

기존 관련 10 tests에서 7 PASS, Windows owner가 필요한 2 tests의 총 8 subtest는 `RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix` ERROR, unbound deadline grace 1 test는 expected collector_lost vs observed running FAIL이었다. 후자는 exact base에서도 동일 FAIL을 확인했으므로 후보 회귀로 판정하지 않는다. owner proof 없이 public service로 만든 unbound row의 현재 fail-closed 계약과 옛 test expectation의 불일치 가능성을 보존한다. 이 shard에서 owner lock을 mock·POSIX 구현하거나 test oracle을 수정하지 않았다. 별도 지원되는 7 회귀 실행과 base deadline 대조의 command/time/raw hash는 `supported-related-run.json`, `baseline-deadline-run.json`에 있다. 10-test 원시 결과 `related-raw.log`는 버리지 않았다.

부모 독립 검토: `git apply --check`·`--whitespace=error` exit0, 기존 exact binding 회귀 1 test exit0. `/workspace/vm-g-review/independent-checks.json`. 부모 D1 실제 Linux preflight는 `blocked: RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix`, create=0/turn=0; `/workspace/vm-g-review/platform-boundary.json`. 이는 expected unsupported 관측이며 Windows proof가 아니다.

초기 harness 개발 실행에서 SQL fixture의 nonexistent is_current query와 result-file publication race, invalid constructor timeout0를 발견·고쳤다. product 결함으로 집계하지 않았다. 초기 개발 evidence와 두 번째 raw log 및 third-run 전체 기록은 `development-*`에 별도로 보존했다. 최초 개발 run의 full raw shell log는 미수집이며 도구 transcript에만 남았다; 이후 corrected final raw log를 근거로 사용한다.

## 남은 최소 입력·권한 경계

1. AGS의 실제 선택 source/revision·manifest·지원 RPC 계약이 필요하다. 이 FM checkout에 `beginEffect`/`admit` 서버 구현 없음; `fm|vm/reserve_dispatch` client만 있다. 실제 AGS stale epoch·key/pin revoked-after-reserve·lease-deadline·admit·beginEffect 판정은 NOT_RUN/unsupported source, FM synthetic 결과로 대체하지 않는다.
2. Native Windows 환경이 있어야 D1 owner kill/lease deadline/Windows process lock을 검증할 수 있다. 이 Linux VM에서는 intentional unsupported; POSIX owner lock을 구현하지 않았다.
3. F07 GO0의 금지 first four source paths 정확한 매핑이 필요하다. 그래서 원본 `runtime.py`/`service.py`에 쓰지 않고 patch artifact와 disposable shadow로만 제안했다. 승인 source 적용·official GO·merge·release 없음.

기존 SQLite init proposal3의 26 tests·20 repeats, consumed FM03, core public vertical workflow, deep Planner/Validator campaign을 수행하지 않았다. 추가 canary/Claude install/live model 요청 없음. Library upload 없음. sanitized harness·patch·report·JSON·raw logs만 review packet으로 전달하며 SQLite/state/generated key/temp project/candidate source 전체는 upload 제외한다.

지원 회귀 최종 결과: 7 PASS, exit0, 1.400735s, raw SHA-256 `7953be6ccd822503a5b588dac0de78637ee7945e355ef8ff815f82867a46f97a`. 기준 deadline 대조: 1 FAIL, exit1, 0.936590s, raw SHA-256 `b255c58298630b5f960fed172bfc7f0da199d3d3ac66d89221c00c70d901cdd8`. 부모 독립 최종 감사 `/workspace/vm-g-review/final-review.json` SHA-256 `cb27f8dee10f909ee22030afe5759378bf8d2e041f79d08aef7dd35dbed7d55c`.

최종 `git diff --check`, `git apply --check --whitespace=error`, tracked status 확인 모두 exit0. exact argv/cwd/UTC/elapsed/stdout/stderr는 `final-checks.json`. `rg` 비밀 키워드 scan은 JSON/log에서 no-match(exit1)이었고 file 내용에는 generated credentials 없음.

보고서 정정: 표는 case 이름을 key로 연결했다. 기준에서 SKIP한 case16은 NOT_RUN/SKIP으로 표시했으며 뒤의 case17·18·19 수치를 각 원본 case에 결속했다. 테스트·소스·harness·원시 evidence·raw logs는 수정하거나 재실행하지 않았다.
