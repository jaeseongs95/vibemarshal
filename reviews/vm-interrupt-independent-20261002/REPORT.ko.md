# VM interrupt 제안 독립 기술 검토

**부모 disposition: 현재 durable-claim 제안은 UNADOPTED/HOLD다.** duplicate RPC는 관측됐지만 전역 duplicate-effect acceptance 위반은 성립하지 않았고, 새 claim은 미전달 요청의 전달 소실을 만든다.

**독립 검토 판정: 게시 증거와 scoped diff의 identity는 확인했다. 전역 at-most-once interrupt 전달은 새 hardening 정책 제안이며, 현재 명시 계약 위반을 고쳤다는 주장은 입증되지 않았다. 채택 판단에는 전달 소실·무효과 거절의 복구 정책을 먼저 확정해야 한다.** 이 검토는 source GO·통합·merge·릴리스 승인이나 Windows qualification이 아니다. 부모는 현재 제안을 채택하지 않은 상태로 유지하며, 이 독립 검토 증거만 별도 비공개 브랜치에 게시하도록 승인했다.

- 검토 task ID: `vm-interrupt-independent-review-20261002` (이 산출물의 식별자; 별도 플랫폼 실행 ID는 미제공).
- 부모 source thread: `01a0f995-7341-7179-91a4-63a814e66020`.
- 요청 orchestration: `gpt-6.1-sol`, effort `high`. 실제 모델·effort의 권위 관측은 `null/unknown`; 도구에 해당 실행 metadata가 노출되지 않았다. fallback·실제 역할/provider 요청은 없다.
- 기준: `jaeseongs95/vibemarshal@32bb0f9dd9f024045d24487312b50f5b703573a3`.
- 검토한 제안/evidence commit: `6c28f9f271f4e99e1ee81c05db75dc342a022558`, [작성자 보고서](https://github.com/jaeseongs95/vibemarshal/blob/6c28f9f271f4e99e1ee81c05db75dc342a022558/proposals/vm-g-failure-20261001/REPORT.ko.md).
- 작성자 보고서의 이전 “independent review”는 가설·설계·harness 피드백에 참여한 내부 작성자/coordinator 교차 확인이라는 부모 설명을 반영했다. 외부 독립 감사 횟수에 포함하지 않았다. 본 검토는 별도 harness와 원시 결과로 판단했다.

## 실제 artifact와 source identity

원격 commit을 exact 기준과 비교하면 한 commit 앞이며, 30개 추가 파일은 모두 `proposals/vm-g-failure-20261001/` 아래다. 제품 source에 적용된 commit이 아니다. 30개 파일을 읽고 Git blob SHA-1을 bytes로 재계산했다. packet-index의 29개 항목과 delivery-manifest의 21개 항목은 bytes·SHA-256 모두 일치했다. manifests 자체를 순환 hash로 검증했다고 주장하지 않는다.

| 대상 | 실제 재계산 SHA-256 |
|---|---|
| 제안 patch | `616647002d4fcc3641dd0fcd6a9a0e11d8f40fa32cd4e403aa2f234903086741` |
| 기준 src manifest, 136 files | `a1eea39a9823f9b740d624c1ea361277448cc614ef96ed888bac7a39af0139cb` |
| patch를 적용한 disposable src manifest, 136 files | `4a605ef675521762146df6a9e483b4207fc6664230f87a2a8f80149df510a02b` |
| 작성자 fault harness | `df14fcfda7a5b5a462230ace0b8459cbf40a6708044f2768ab60c02ef19ce975` |
| 본 검토 main harness | `6f0f51be47c9bc22366770f9982ea54147a7ed032ec6acb1d978ef5f0643ef12` |
| 본 검토 RPC/effect 구별 harness | `166d461fdd54f014659427611d61fd805bf0938f35c8dc303320c8bd6de751c7` |
| 본 검토 별도 close 경계 harness | `71d267e16231a20f46b1a919f7e81bafa218b2ad1bcfaf39da6f479e12017f83` |

각 src file hash도 작성자 manifest와 전부 대조했다. patch apply/whitespace 검사와 검토 뒤 source 재대조 모두 exit 0. 차이는 `runtime.py` 5 additions/9 deletions, `service.py` 40 additions뿐이다. exact 기준에서 복제한 shadow에 기존 patch만 적용했으며 새 fix는 구현하지 않았다. 공유 checkout은 branch `work`, exact HEAD, status 빈 문자열이다. 자세한 bytes와 명령은 `evidence/input-verification.json`, `evidence/verification-command.json`에 있다.

실제 환경은 Linux `6.18.44`/POSIX, Python `3.12.14`, Node `v24.19.0`다. `AGENTS.md`를 읽었으며 workspace `.agents`·`.codex`는 비어 있고 repo `.agents`는 없다. 사용자 제한에 따라 Library·source publication·운영 DB/profile·인증 경로를 사용하지 않았다.

## 예약·RPC 전달·효과의 계약 구분 — R01

1. [service.py:1959](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/service.py#L1959)의 `begin_runtime_job_interrupt`는 상태와 interrupt 요청을 원자적으로 예약한다. 이 예약은 RPC 송신 또는 provider 상태 변화 receipt가 아니다. INTERRUPTING 경쟁 caller가 기존 요청을 배송하는 현재 supervisor 동작까지 금지한다는 명시 acceptance criterion은 찾지 못했다.
2. [runtime.py:469](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/runtime.py#L469)의 구체 구현 `CodexAppServerRuntime.close` 주석은 timeout 후 같은 adapter의 close/복구 중복 전송을 막는다. `interrupt`의 in-memory set도 같은 instance의 재송신을 막는다. 이 좁은 요구를 여러 연결·process·job 전체의 durable RPC-at-most-once 계약으로 확대할 근거는 부족하다.
3. [D08](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/redesign-1.0-contract.md#L74)은 제목부터 **planned**다. cancel/replan이 효과를 지우거나 중복 생성하지 말라는 방향과 관측 우선 복구, 완전 exactly-once 비보장은 확인된다. 이것만으로 interrupt RPC 두 번이 실제 효과 두 번이라는 결론은 나오지 않는다. 제품 지침의 중복 효과 방지도 전송 횟수와 별개다.

작성자 case05·06의 journal은 synthetic `interrupt()` 진입마다 한 줄을 쓴다. 동일 response digest가 원장에서 dedupe되는 것도 확인했지만, 이 journal은 실제 provider의 별도 side effect 횟수를 관측한 자료가 아니다. 독립적인 idempotent provider 의미 fixture에서는 실제 `CodexAppServerRuntime.interrupt` 메서드의 두 adapter 호출이 **기준 RPC 2회 / 상태 전이 1회**, 후보 RPC 1회 / 상태 전이 1회였다. provider는 합성 `_raw`만 주입했다. 실제 provider가 멱등이라는 증명도 아니며, “RPC 두 번이면 효과 두 번”이라는 추론이 성립하지 않는다는 구별 사례다.

**채택 전 정책 blocker:** 중복 제한의 단위를 request reservation, RPC attempt, exact turn의 provider transition 중 무엇으로 할지 선언해야 한다. 이 exact path의 재현은 교차 caller/restart 배송 중복을 보여 주며, 현행 전역 RPC acceptance 위반을 확정하지 않는다. scoped hardening의 동작으로 재분류할 수 있다.

후보 marker의 단위는 job ID이며 보장 범위도 이 supervisor 배송 경로다. 별도 `CodexAppServerRuntime` object에 같은 turn의 합성 handle을 주고 `close`를 호출하면 marker를 조회하지 않고 interrupt를 보낸다. 앞선 supervisor 송신과 합쳐 기준/후보 모두 RPC attempt 2회였다. 실제 두 native connection이나 provider 효과를 증명한 것은 아니지만, 이 patch가 모든 adapter/close/job에 걸친 전역 RPC-at-most-once를 집행하지 않는다는 코드 경계는 재현했다. caller/process/restart를 가로지르는 **동일 job supervisor 배송** 한정 보장으로 적어야 한다.

## durable claim 선형화와 crash/재관측 — R02

후보는 `service._claim_runtime_job_interrupt_delivery`에서 exact row binding, INTERRUPTING/CANCELLED 상태, 기존 receipt와 기존 History marker를 같은 transaction 안에서 읽고 `runtime_job.interrupt_dispatching`을 넣는다. [ledger.py:857](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/ledger.py#L857)의 `BEGIN IMMEDIATE`가 writer를 직렬화하며, commit이 영속 claim의 선형화 지점이다. private method는 transaction 종료·commit 이후에 request를 반환한다. 별도 schema/unique index 없이도 이 원장 transaction 경계 내 경쟁자는 하나다. unbound 요청은 supervisor에서 claim 이전에 반환한다.

claim과 RPC는 한 원자적 효과가 아니다. before-commit rollback은 transaction 구현으로 추론하며, transaction 도중 kill을 추가 실행한 것은 아니다. 독립 harness는 예약 후/claim 전 종료와 claim commit 후/RPC 전에 종료를 각각 만들었다. 후자의 `os._exit(72)`는 `bounded_observation_call`에 진입하자마자 실행하므로 fixture에서 provider method 미호출이 직접 확인된다. 이 crash injection은 검토 harness만 바꾸며 owner-lock 경계를 mock하지 않는다.

| 구별 사례 | 기준 / 후보 dispatch journal | 실제 관측 |
|---|---:|---|
| 경쟁 caller, 첫 응답 대기 중 | 2 / 1 | 후보 claim 1개; receipt count만으로 기준 dispatch 두 번은 안 드러남 |
| 예약 후 claim 전 process 종료 | 1 / 1 | 이후 caller가 한 번 전달 |
| claim 뒤 RPC 전 process 종료, 원 turn은 계속 active | 1 / 0 | 후보 claim 1, interrupt receipt 0, exact stored read 2; 재요청 뒤에도 effects 0 |
| effect 뒤 receipt 전 process 종료, 원 turn은 계속 active | 2 / 1 | 후보 재송신 없음; terminal 발명 없음 |
| effect 뒤 응답 유실, active → 나중 직접 terminal | 1 / 1 | error receipt는 terminal 아님; 직접 재관측 후에만 terminal 기록 |
| 비소유 Claude 연결의 no-effect 거절 뒤 정상 sender | 0 / 0 | 아래 R03; 기준에도 존재하는 문제 |
| replacement turn 반증 | 0 / 0 | conflicting binding 거절; Claude guard와 stored-read mismatch 거절 |
| dispatch 경계 뒤 Core job cancel | 1 / 1 | exact 원 turn interrupt는 계속 허용; 실행 재개·terminal·Goal 완료 없음 |
| D1 실제 public dispatcher POSIX 경계 | 0 / 0 | BLOCKED, create=0, turn=0 |

각 source의 9개 사례와 추가 idempotent 의미·close 경계 사례를 한 번씩 실행했다. 11개 구별 시나리오의 source별 대조 22실행이지 신규 qualification 22개가 아니다. main journal 총량은 기준 8/후보 5, 추가 의미 fixture RPC는 2/1, close 경계 RPC는 2/2이며 실제 live 호출은 전체 0이다. 원장 `attempts/runtime_intents/runtime_receipts/provider_calls/goal_verdicts`는 본 fixture의 interrupt 경로에서 모두 0, Task는 ready였다. interrupt receipt는 `runtime_job_observations`에 별도로 기록되므로 runtime_receipts=0을 receipt 부재로 혼동하면 안 된다. 모든 확인한 History chain은 유효했다.

**채택 전 복구 blocker:** 후보는 unsent인 synthetic crash에서도 다음 배송을 막는다. 저장 turn이 계속 active이면 `reattach`는 provider_progress만 기록하고 status는 INTERRUPTING으로 남는다. claim은 job 전체에 영구 소비되며, 동작 결과에는 별도 interrupt_delivery_unknown/no_effect 판정·해제·owner 전달 경로가 없다. 원문 History marker로 분석할 수는 있지만 일반 runtime job 반환만으로 pending과 uncertain delivery가 구별되지 않는다. 필요한 cancellation을 다시 전달할 수도 없다.

실제 crash 후 sender가 미송신이었다고 단정하는 것은 안전하지 않으므로 marker를 자동 삭제·재송신하는 해결책은 제안하지 않는다. 이 한계는 claim/send 간격의 의도적인 at-most-once 대가이고, 완전 exactly-once 위반으로 비판하지 않는다. 하지만 active turn이 영구 남아도 허용되는지, 직접 no-effect 증거가 있을 때 어떤 권위가 재시도를 허용하는지, 어떻게 사용자에게 정지를 드러낼지는 채택 전에 결정해야 한다. 작성자 case16은 재관측 fixture가 곧 completed를 돌려주어 이 active 상태의 전달 소실을 판별하지 못한다.

## owner·철회·정확한 target·reconciliation — R03

claim에는 owner lease, epoch, 현재 adapter 연결 검사가 없다. 요청 public facade도 owner 확인 없이 `begin_runtime_job_interrupt`와 배송으로 간다. **이것만으로 권한 위반이라고 판단하지 않는다.** deadline 감시와 pause/cancel은 원 turn을 중단하는 control path이며 non-owner deadline interrupt가 기존 설계에 있다. CANCELLED/만료된 job에 실행 효과 fence를 그대로 적용하면 필요한 정지까지 막을 수 있다.

독립 fixture는 `ClaudeCodeRuntime` 생성자를 건너뛰고 `_lock`, 빈 `_threads`, 빈 interrupt set만 주입한 뒤 **실제 interrupt 메서드의 pre-write guard**를 사용했다. 실행 파일·세션·인증에 접근하거나 process를 띄우지 않았다. 비소유 연결은 `CLAUDE_INTERRUPT_TARGET_NOT_LIVE`로 전송 전에 거절한다. supervisor는 이를 interrupt_receipt로 고정한다. 정상 연결을 모사한 후속 sender도 receipt 때문에 호출되지 않는다. 기준과 후보 모두 재현되므로 후보가 새로 만든 회귀라고 부르지 않는다. 후보에는 영속 claim까지 추가된다. 이 경우의 원인·no-effect는 fixture의 실제 adapter branch로 직접 확인됐으나, 현재 no-effect-aware routing/retry는 없다.

작성자 stale-file/job-deadline/workflow-cancel case11·12·17은 다른 `prepare_authorized_runtime_effect`의 fence를 검사하며 interrupt claim의 owner/epoch 검증이 아니다. AGS key/pin 철회, 실제 owner lease 만료·살아 있는 stale owner, producer revocation을 입증하지 않는다. 본 테스트의 Core cancel-after-boundary에서도 원 turn interrupt 1회는 발생했다. cancellation 자체가 stopping control을 철회했다는 증거가 아니므로 이것을 forbidden effect로 집계하지 않는다. 실제 stale/revoked OS owner 및 AGS 정책 qualification은 NOT_RUN이다.

확인된 exact target 경계는 다음과 같다.

- candidate claim에 replacement turn ID를 주면 None이며 marker를 만들지 않는다. `bind_runtime_job_provider`의 conflicting binding도 거절된다. 이 API의 불변성을 모든 내부 start API의 불변성으로 확대하지 않는다.
- actual Claude guard는 현재 연결의 turn이 다르면 `_write` 이전에 거절한다. 실제 Codex adapter는 `turn/interrupt` request에 threadId·turnId를 함께 넣으며, supplemental synthetic `_raw`에서 그 exact params를 확인했다.
- stored read가 replacement turn을 반환하면 `RUNTIME_OBSERVATION_BINDING_MISMATCH`; provider_terminal·Task·Goal 완료는 생기지 않는다. 이 사례에서는 replacement 대상 control write/effect 0이었다.
- claim commit 후 call 직전 다시 fencing하지 않지만 closure는 load한 원 thread/turn을 보낸다. 본 소스만으로 replacement turn을 공격하는 효과를 입증하지 않았다. Claude wire request의 `request` body는 `{subtype: interrupt}`이고 request_id의 turn 이름은 correlation 값이다. provider 내부 ordering/대상 보장은 이 로컬 guard 관측을 넘으므로 실제 qualification이 필요하며, 이 검토에서 replacement 효과가 발생했다고 주장하지 않는다.

reconciliation은 원 stored turn을 resume 없이 직접 읽고 exact binding을 검사한다. marker·ACK·collector loss는 terminal이 아니다. active 관측은 interrupt 전달 receipt를 복구하거나 claim을 해제하지 않는다. 직접 terminal이 나중 제공된 fixture에서만 provider_terminal이 기록됐고 GoalVerdict·Task 완료는 여전히 없다. 작성자 case14의 runtime intent receipt reconcile도 interrupt marker 복구를 제공하는 경로가 아니다.

## 다른 제안과의 충돌·경계

interrupt patch의 실제 hunks는 `RuntimeJobSupervisor._deliver_bounded_interrupt`와 `EngineService`의 새 claim method뿐이다. 부모의 최신 설명에 따라 F07 금지 first-four 경로와 직접 중복은 없다. 해당 네 경로의 전체 목록·F07 proposal bytes를 새로 제공받거나 검증한 것은 아니며, F07 구현이나 qualification은 수행하지 않았다.

SQLite revision3의 실제 [patch](https://github.com/jaeseongs95/vibemarshal/blob/8a159ed4b79058aba8e6302358733696b33d11ff/docs/independent-results/2026-10-02/vm-sqlite/revision3/atomic-initialize-v3.patch)를 읽었다. SHA-256은 `40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89`; `_connect/initialize/_assert_identity`의 ledger.py 한 파일이다. 직접 hunk 충돌은 없고 `transaction`의 BEGIN IMMEDIATE를 바꾸지 않는다. 다만 같은 SQLite lifecycle에 기대므로 두 제안의 결합 검증을 한 것으로 해석하면 안 된다. 이를 후보에 적용하지 않았고 이전 suite도 반복하지 않았다.

Planner/Goal-verdict 제안은 부모 설명에 따르면 같은 `service.py`의 `record_goal_verdict`를 바꾼다. 본 patch에는 그 함수나 GoalVerdict 생성 변경이 없다. **동일 파일이므로 shadow service.py 전체를 덮어쓰면 다른 제안이 유실된다.** 함수/hunk별 병합 책임은 소유 writer에게 있다. Planner proposal exact bytes·digest는 이 검토 입력에 없으므로 그 제안 자체의 정확성이나 결합 안전성을 인증하지 않는다. 병합·덮어쓰기·source 적용은 하지 않았다.

D1은 [1.0 Windows 경계](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/redesign-1.0-contract.md#L181)를 유지한다. `EngineDispatcher.run_once`는 두 source 모두 `RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix`로 BLOCKED, create=0/turn=0이었다. owner support 함수·OS lock을 patch/mock하지 않았다. 합성 row는 하위 service fixture에서만 만들었다. Windows qualification, 운영 DB/profile, consumed FM03, 실제 provider/AGS는 사용하지 않았다.

## 정확한 실행·원시 결과

공통 executable `<PYTHON_ENV>/bin/python`, source별 cwd `<REVIEW_ROOT>/base|candidate`. 아래 각 runner의 child argv·선택 환경·실제 UTC·exit·stdout/stderr hash는 해당 command JSON에 있다. alias는 원래 위치를 비밀값 없이 정규화한 것으로 `MANIFEST.json`에 대응을 설명했다.

```sh
<PYTHON_ENV>/bin/python <REVIEW_ROOT>/run_probe.py base
<PYTHON_ENV>/bin/python <REVIEW_ROOT>/run_probe.py candidate
<PYTHON_ENV>/bin/python <REVIEW_ROOT>/run_idempotent.py base
<PYTHON_ENV>/bin/python <REVIEW_ROOT>/run_idempotent.py candidate
<PYTHON_ENV>/bin/python <REVIEW_ROOT>/run_close.py base
<PYTHON_ENV>/bin/python <REVIEW_ROOT>/run_close.py candidate
<PYTHON_ENV>/bin/python <REVIEW_ROOT>/run_verification.py
```

| 실행 | UTC 시작 → 종료 | wall seconds | exit | 원시 stdout SHA-256 |
|---|---|---:|---:|---|
| main base | 2026-10-01 23:52:16.329079 → 23:52:24.533015 | 8.204076 | 0 | `7ebf8800d676bfc10e746327d0b833fe8999ed3cf96c5671d19fd0e2740d6805` |
| main candidate | 2026-10-01 23:52:16.329793 → 23:52:24.404155 | 8.074512 | 0 | `eafcfb3191edc6627113b22b666b75997ccdbe4f22c4fe3a20ecea08428fe118` |
| RPC/effect base | 2026-10-01 23:54:31.705139 → 23:54:32.984052 | 1.278925 | 0 | `4560e8d38be0aefd265b47573c7eec86bfae0a3ab8b1bd8b1edc7d235ef12e5c` |
| RPC/effect candidate | 2026-10-01 23:54:31.701143 → 23:54:32.953247 | 1.252111 | 0 | `594221470e3d1bf836863aa82f16abdb2f3fe4af63c55f3da71b4e5f58d211d8` |
| 별도 close base | 2026-10-02 00:00:57.461914 → 00:00:58.828587 | 1.366681 | 0 | `c654263d3880ffb2d4642baedba5f18c8f71d3c597583a031fc00235620b27d4` |
| 별도 close candidate | 2026-10-02 00:00:57.465548 → 00:00:58.848885 | 1.383346 | 0 | `d031c254434ae8b04a0ec3ae6b780654568e2556d07722dd6df32c6b4f33f365` |
| artifact/source 재대조 | 2026-10-01 23:55:59.812578 → 23:56:00.097854 | 0.285282 | 0 | `09a428d5821e7c7133c07a158f1128826a8ec84be5ce5a7eba1a51313ea6c464` |

모든 stderr는 빈 bytes, SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`. child crash exit 71/72/73은 각 지정 경계의 의도적 종료다. runner exit0은 기록한 차이가 observation oracle과 일치했다는 뜻이며 제품 acceptance PASS가 아니다. command/raw 결과의 원본 hash와 정규화한 게시 bytes hash는 manifest에서 구분했다. 준비 packet에는 DB/WAL, candidate source 전체, 운영 profile/key, 인증정보, provider session transcript가 없다.

남은 결정은 R01의 보장 단위와 R02/R03의 안전한 정지·복구 정책이다. 실제 owner/AGS/provider qualification은 별도 입력과 지원 환경이 필요하다. 현재 결과를 duplicate 외부 효과 결함 수정, eventual delivery, 완전 exactly-once, product GO로 승인하지 않는다.
