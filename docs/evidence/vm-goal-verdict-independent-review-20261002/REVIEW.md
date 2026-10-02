# Goal verdict proposal 독립 기술 검토

검토 결과는 **정책 분리 또는 명시적 정책 결정 필요**다. 활성 Plan 확인, 최신 유효 Task 검사 확인, 현재 integration FAIL/INCONCLUSIVE 차단은 근거가 있고 재현된 결함을 막는다. 그러나 candidate의 `service.py:6754–6756`은 동일 입력·동일 근거를 가진 연속 PASS에서도 이전 PASS ID를 거부한다. 이 변경은 문서의 명시 규칙이 아닌 새 정책이다. 해당 규칙을 결함 수정과 함께 승인된 계약처럼 통합하는 것을 권고하지 않는다. 이 문서는 구현·merge·source GO·release 승인이 아니다.

검토자는 author의 결과·참여자·판정을 증거로 사용하지 않았다. author README와 normative-sources.json은 검토 입력으로 읽었고, 실제 base 문서·source·전체 diff를 직접 읽고 원문 18개 항목의 line/text/hash를 재계산했다. 아래 실행 결과는 이 reviewer가 새로 만든 disposable fixture와 새 실행에서 얻었다.

## 고정 입력과 reviewer provenance

| 항목 | 값 / 출처 |
|---|---|
| private repository | `jaeseongs95/vibemarshal` |
| 구현 commit | `98bb122970f82bb5fcef122003ee10c7eb787cf4` |
| 구현 tree | `1bcc8378c86f30aca46fd757545f07d458d65dd3` |
| direct base | `32bb0f9dd9f024045d24487312b50f5b703573a3` |
| 검토 입력 artifact commit | `d6956481a964f1b84fb88baa03218a3034f8978d` |
| branch | `dot/vm-goal-verdict-proposal-20261002` |
| 실제 reviewer thread/task identity | `01a0f9e2-ed1d-7296-815a-14270ae95034` (`CODEX_THREAD_ID` 환경 관측) |
| canonical agent | `/root` (현재 turn developer metadata) |
| parent source thread | `01a0f995-7341-7179-91a4-63a814e66020` (delegation 입력) |
| author task: 증거/참여자로 재사용 안 함 | `01a0f9c6-8438-7130-850b-132cda135032` |
| requested model / effort | `gpt-6.1-sol` / `high` (사용자 요청) |
| provider-observed model / effort | `null` / `null`: turn별 provider receipt가 노출되지 않음 |
| 환경 model / effort setting | `CODEX_MODEL=null`, `CODEX_REASONING_EFFORT=null` |
| 요청과 실제 설정 일치 | `UNKNOWN`; 요청값을 observed 값으로 승격하지 않음 |
| 실행 환경 | POSIX, Python 3.12.14; `/workspace/cloud-setup/venv/bin/python` |
| 의존성 | pydantic 2.13.5, cryptography 50.0.1, openai-codex 0.147.0 |

candidate diff는 `record_goal_verdict`와 새 permanent regression 파일만 바꾼다. commit parent/tree를 직접 확인했다. 독립 실행에는 `git archive`로 `/workspace/vm-goal-independent-review-20261002/base`와 `candidate`를 만들었으며 원본 checkout과 frozen candidate는 수정하지 않았다. AGENTS의 한국어 보고·원장 권위·evidence 보존 규칙을 적용했다. 현재 사용자가 지정한 managed environment·local-only review가 과거 AGENTS의 danger-full-access/never 및 commit/push 기본 조항보다 우선한다. 권한 상승·commit·merge·push·Library 작업은 하지 않았다.

## 계약과 reachability 판단

| 사례 | 직접 관측과 법적 상태 전이 | 계약 판단 |
|---|---|---|
| integration PASS 뒤 FAIL/INCONCLUSIVE | active Plan, completed Task, integration PASS 후 새 결과를 `record_validation`으로 기록 가능. baseline은 이전 PASS를 근거로 satisfied 수락, candidate는 원자적으로 거부 | [설계 §8 L370](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L370)의 필수 검사 근거 확인과 원장 권위에 부합하는 수정. 문서가 latest SQL 정렬 규칙까지 명시하지는 않는다. 현재 실패 반증 무시가 잘못이라는 적용은 계약 해석이며 dispatcher의 기존 최신 집계는 구현 지원 근거다. |
| completed Task 뒤 새 FAIL/NOT_RUN/INCONCLUSIVE | `record_validation`은 Task가 completed인 것을 금지하지 않는다. terminal PASS/FAIL에는 필요한 test evidence를 사용했고 NOT_RUN에는 빈 evidence를 사용했다. Task는 completed를 유지하지만 effective 검사 결과는 바뀐다. baseline satisfied 수락, candidate 거부 | [D02 L21](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/redesign-1.0-contract.md#L21)은 재사용 계보 원본·중간 Task의 늦은 실패/미확정을 현재 Goal에 반영하라고 명시한다. 새 독립 source/middle 재사용 사례에서도 위반과 수정 효과를 재현했다. 원본 Task 자체에 같은 원칙을 적용하는 것은 §8 및 `complete_task`의 기존 최신-PASS 규칙과 일관된다. |
| 연속 PASS의 이전 ID | Plan, Goal, State, Map, Worker, evidence ID/content를 그대로 두고 같은 validation에 새 PASS 하나만 append. 두 PASS 모두 합법적이며 이전 근거를 무효화하는 변화가 없다. baseline 수락, candidate 거부 | [설계 L62–65](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L62), [§8 L378](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L378)은 immutable binding·독립 검사·freshness를 요구하지만 latest result-ID set equality를 명시하지 않는다. **정책 강화/호환성 변경이며 기존 동작의 확정 규범 위반으로 분류하지 않는다.** |
| superseded Plan의 satisfied | 기존 Plan의 모든 Task를 완료하고 integration PASS 기록 후 같은 Goal/Plan 계보의 새 reviewed revision을 활성화. in-flight Attempt/job/operation이 없어 quiescence와 activation 검사를 통과한다. 이전 Plan의 완료 Task는 completed 상태를 보존하므로 늦은 제출이 baseline 검사를 통과한다. baseline이 replacement pointer를 NULL로 만들고 run_state=completed; candidate는 거부·replacement 보존 | [D01 L11](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/redesign-1.0-contract.md#L11)의 활성 Plan 권위와 [설계 L96](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L96)의 revision 보존에 대한 명백한 상태 전이 결함이다. candidate guard가 올바른 위치에 있다. |

`GoalVerdict` typed schema는 SATISFIED의 모든 criterion PASS, criterion evidence, unique criterion/result IDs, 하나 이상의 integration ID를 요구한다. probe는 이를 모두 만족한다. 다른 project/task에 evidence를 묶거나 raw SQL로 상태를 조작하지 않았다. 승인/activation은 기존 fixture의 명시 test authority와 reviewed eligible Plan을 사용했다. 재사용 probe는 실제 synthetic `print('ok')` command observation, 현재 파일 digest와 성공 Attempt를 준비한 뒤 Core의 정상 reuse 경로를 사용했다. provider receipt나 실제 Worker 실행을 만들어냈다고 주장하지 않는다.

`TrustedConsoleHost → cli.build_parser → validate task/goal → EngineService`의 source-tree entrypoint도 따로 실행했다. completed Task FAIL 기록은 baseline/candidate 모두 exit 0이었다. 이어진 satisfied 제출, valid historical PASS ID 제출, superseded satisfied 제출은 baseline exit 0 / candidate exit 2였다. Core가 이미 준비한 synthetic 상태에서 수동 validation 입력이 가능한 것을 입증한다. `validate task --complete`는 별도 governance 차단 대상이며 여기서는 사용하지 않았다. Worker/Validator에게 service/DB 권한이 있다는 뜻도 아니다.

정상 dispatcher는 active Plan에서 최신 결과를 직접 만들므로 수동 stale/역사 제출 사례와 동일하지 않다. 원본 state bootstrap에는 직접 Core API를 사용했다. 설치 wheel·provider public vertical E2E·Windows 실행의 reachability까지 이 결과에서 추론하지 않는다.

## 독립 실행 결과

같은 `probe.py`를 baseline/candidate에서 각각 한 번 실행했다. **16개 관측 사례씩**이며 전체 exit 0은 probe 수행과 provenance/원자성 assertion 성공을 뜻한다. baseline의 잘못된 satisfied 수락을 PASS 판정으로 포장하지 않는다.

| 관측 사례 | baseline | candidate |
|---|---|---|
| current PASS | 수락 | 수락 |
| latest integration FAIL / INCONCLUSIVE | 수락 / 수락 | 거부 / 거부 |
| completed Task FAIL / NOT_RUN / INCONCLUSIVE | 모두 수락 | 모두 거부 |
| successive PASS, old ID | 수락 | 거부: 정책 강화 |
| successive PASS, latest ID | 수락 | 수락 |
| integration FAIL 뒤 새 PASS | 수락 | 수락 |
| superseded SATISFIED | replacement 소거 | 거부, replacement 유지 |
| superseded INCONCLUSIVE | 역사 기록, replacement 유지 | 역사 기록, replacement 유지 |
| later append, earlier evaluated_at integration FAIL | 수락 | 수락 |
| later append, earlier evaluated_at Task FAIL | 수락 | 거부 |
| unchanged reused completion | 수락 | 수락 |
| reused Task 원본 / 중간 Plan의 늦은 FAIL | 모두 수락 | 모두 거부 |

permanent regression은 정확한 `GoalVerdictAuthorityTests` class만 선택해 **8/8 통과**, unittest 내부 elapsed 0.479s였다. imported TestCase의 추가 발견·broad campaign 반복은 없다. 독립 Core 32개 관측 + source-tree 수동 CLI 6개 사례 외에 추가 provider 호출은 0회다.

**정렬 해석 주의:** integration은 `(evaluated_at, rowid)`, Task는 append-only History sequence/effective Worker epoch를 사용한다. caller가 지정한 과거 timestamp의 FAIL은 더 늦게 기록돼도 integration에서는 기존 PASS 앞에 정렬된다. 양 버전에서 이를 재현했다. timestamp가 실제 더 이른 검사를 뜻하면 이후 PASS를 유지하는 것은 타당할 수 있다. integration의 최신 의미를 기록 순서로 바꾸라는 명시 계약을 발견하지 않았으므로 이것을 새 candidate 회귀나 확정 위반으로 올리지 않는다. 다만 “늦게 기록된 모든 FAIL을 막는다”는 일반적 주장에는 이 결과가 반례이며 정렬 의미를 명시해야 한다. candidate는 기존 dispatcher 정렬과 일치한다.

## DB·transaction·provenance 평가

`SQLiteEngineLedger.transaction`은 `BEGIN IMMEDIATE`로 시작하고 예외에는 rollback한다. candidate는 active status/pointer, 최신 결과 확인, verdict insert, Plan 완료, project pointer 제거와 History append를 이 한 transaction 안에서 수행한다. 따라서 정상 Core writer의 동시 activation은 그 사이에 끼어들 수 없다. 이 판단은 SQLite/source 분석이며 실제 multi-process race/fault 시험은 수행하지 않았다.

모든 거부 probe에서 `projects`, `plan_revisions`, `task_contracts`, `goal_verdicts`, `history_events`, `evidence_records`, `validation_results`, `attempts`, `runtime_intents`, `runtime_receipts`, `task_completion_reuse`의 전/후 전체 행 canonical digest가 같았다. 후보가 거절하기 전에 이미 append된 실패 evidence/result는 보존된다. 수락 사례에서도 evidence/validation/Attempt/intent/receipt/reuse 행을 변경하지 않았으며 각 synthetic History hash chain이 유효했다. superseded INCONCLUSIVE는 역사 기록만 추가하고 active Plan을 그대로 유지했다. unchanged reused completion은 원래 Task evidence/result/Attempt identity로 판정 가능했다.

기존 criterion evidence 검사는 project 존재 확인이며 최신 coverage 결과와 모든 criterion evidence를 재계산해 같게 만드는 검사는 아니다. 이번 patch가 그 검사를 강화하거나 전반적인 evidence provenance 검증을 완성했다고 주장하지 않는다. unrelated same-project criterion evidence, 외부 효과 receipt, 독립 semantic Validator provider 경로는 이 리뷰의 새 실행 대상으로 넓히지 않았다.

## parent에게 돌려보내는 blocker와 처리 제안

1. **B1 — exact latest-ID 정책 결정 필요 (candidate L6754–6756, permanent test의 old PASS ID oracle).** 무효화 없는 연속 PASS에서도 유효한 기존 verdict 제출을 거부한다. parent는 safety guard를 유지하면서 이 제약을 분리/완화하도록 author와 조율하거나, compatibility 변화·latest 정의·계약·tests를 별도 명시 정책으로 결정해야 한다. 기존 규범 위반 수정이라는 이유만으로 현재 equality를 승인할 근거는 부족하다. frozen candidate를 여기서 수정하지 않았다.
2. **검증 범위 잔여:** D1 Windows 경계는 유지됐다. `owner_lock_platform_supported=false`인 POSIX에서 owner lock을 우회하지 않았다. native Windows, actual provider/semantic terminal receipt, package installation, whole public vertical E2E, live operational DB/profile/keys, 실제 concurrent process/fault는 NOT_RUN이다. 이는 이 technical review의 제품 release 승인을 위한 근거가 없다는 경계이며 기존 D1 결함을 새로 주장하는 것이 아니다.
3. **observed model/effort 미제공:** reviewer identity는 환경에서 확인했지만 requested 설정의 실제 적용은 UNKNOWN이다. parent가 authoritative turn receipt를 보유하면 별도 provenance로 보완할 수 있다. 이 값들을 요청값으로 채우지 않았다.

상세 exact argv/cwd/PYTHONPATH/UTC 시작·종료/exit/wall time/raw SHA-256은 `commands.jsonl`에 있다. public raw에는 synthetic temporary paths·IDs와 각 nested CLI command/exit/stdout/hash도 보존했다. 임시 DB는 fixture 종료 시 삭제됐고 bundle에는 넣지 않았다. `input-audit.raw.txt`는 실제 문서/source hash와 18개 인용 원문 검증 결과를 담는다. 원본 checkout 최종 HEAD는 direct base이며 tracked/untracked status 출력은 빈 bytes다. 후보를 변경·merge·commit·publish하지 않았다. 결과는 parent 검토를 위한 로컬 artifact다.

## 실행 bytes 요약

| 실행 | cwd suffix | exit | wall seconds | raw SHA-256 |
|---|---|---:|---:|---|
| candidate-diff-check | `vibemarshal` | 0 | 0.008519 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| independent-base | `base` | 0 | 2.185268 | `b5a671101a963219821cc18afb014da9f1b2036a4444c4f86d1c4f8bd557bb96` |
| independent-candidate | `candidate` | 0 | 2.060047 | `f811fc4b064cf639476beb0eac571107f3df6783c254cf3549d2ce21599435a4` |
| permanent-candidate | `candidate` | 0 | 1.096415 | `24bfda7f0fa91da35f6efbb009a99656a7ee1dd9357e6ad3b0b27fd58f847da0` |
| public-base | `base` | 0 | 4.137744 | `c584bf9481e1c7ee39324d2f4b2746d17602a080edbf5d8a7823eac19e70d4f2` |
| public-candidate | `candidate` | 0 | 4.289257 | `30dd59a898e67e237094077f40eb4fe562ac91f4d4831e78c92659a14c779095` |
| input-audit | `vibemarshal` | 0 | 0.208525 | `0f4b776d9ce92549d687816582c536ca51d79a982a99f3df51e3ae65c34e08e2` |
