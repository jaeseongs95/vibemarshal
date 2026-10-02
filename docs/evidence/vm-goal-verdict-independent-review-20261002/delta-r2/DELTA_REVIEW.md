# R2 compatibility 경계 독립 재검토

**변경된 호환성 경계에서 기존 B1을 해소한 것으로 판단한다. 이 제한 범위에 새 blocker는 발견하지 않았다.** 유효한 이전 PASS ID는 수락되고 제출 ID 그대로 저장된다. 현재 FAIL/INCONCLUSIVE, 잘못된 Goal/Plan/result 결속과 superseded Plan의 satisfied 제출은 계속 원자적으로 거부된다. 이는 `f862dc4f948dde8b6f429bcbaa073f03ce41a70f`에 대한 좁은 기술 검토이며 source GO·merge·release 승인이 아니다. F07 first-four 순서를 변경하지 않는다.

## 고정 대상과 독립성

- 새 구현 commit: `f862dc4f948dde8b6f429bcbaa073f03ce41a70f`
- tree: `9388e2ade7f28341034b7d8c0271e59dcf4bc847`
- direct parent: `927f9d2936635d8c987c2a3a4092e6e4dc2c742b`
- 최초 base: `32bb0f9dd9f024045d24487312b50f5b703573a3`
- 역사 R1: `98bb122970f82bb5fcef122003ee10c7eb787cf4`
- author의 R2 입력 evidence commit: `59267e6584c49e6eab10cffca267848727d5350f`
- 실제 reviewer identity: `01a0f9e2-ed1d-7296-815a-14270ae95034` (`CODEX_THREAD_ID` 직접 환경 관측), canonical `/root`.
- requested model/effort: `gpt-6.1-sol/high`; provider-observed와 effective 환경 설정은 null/null, 실제 적용 일치 UNKNOWN. 요청값을 실제 모델 관측으로 채우지 않았다.

지정된 author `reviewer-boundary-report.json` 전체 JSON과 `compatibility-boundary.diff`를 읽고, 실제 Git diff와 boundary diff가 byte-identical인지 확인했다. author 실행 count/점수/결론은 독립 근거로 재사용하지 않았다. 이 reviewer가 새로 실행한 제한된 결과만 아래에 기록한다. author source는 동결된 Git archive로 읽었으며 수정하지 않았다. 원래 REVIEW.md/review.json/commands/raw files와 SHA256SUMS는 그대로 역사 evidence로 보존한다.

## source·scope 판단

R1과 R2의 EngineService AST를 method별 비교했으며 `record_goal_verdict`만 달라졌다. 최초 base와 R2의 누적 비-evidence 제품 변경은 `src/flowmarshal/engine/service.py`와 `tests/test_engine_goal_verdict_authority.py` 두 파일뿐이다. 그 밖의 누적 30개 경로는 모두 `docs/evidence/`다.

R2는 integration rows를 한 번 읽고 `(evaluated_at,rowid)` 정렬의 최신 status gate를 그대로 유지한다. 별도의 제출 result-ID 검사는 같은 Plan, task_id=NULL, status=pass인 실제 행의 ID 집합에 대한 subset으로 되돌린다. 최신 PASS와 정확히 같은 ID 집합이라는 미승인 정책은 제거됐다. 기존 active Plan/pointer check 및 effective Task 최신 PASS check, Goal/Plan digest·criterion coverage·evidence 존재·provider/external-effect blocker 검사는 변하지 않았다.

- service bytes SHA-256: `284805343517f29621381a9c1b045fe03492b83bbcc3c4ee2bed7d8781d02261`
- regression bytes SHA-256: `22411153cbb9bb4bdf6d36c2b337aae4a6b9aee9e11007d3f15ca1dc839f017e`

이는 parent가 요청한 base의 historical PASS 호환성을 복구한다. 기존 wrong Goal/Plan digest 또는 실제 PASS row가 아닌 ID를 허용하지 않는다. 모든 historical PASS가 현재 입력에서 유효함을 writer가 새로 일반 증명한다는 강화 주장도 하지 않는다.

## 새 독립 관측

한 번의 explicit unittest 실행에서 두 관련 class와 기존 tampered operation-binding 사례만 골라 **18/18 통과**, 내부 elapsed 1.244s, process wall 2.813009s, exit 0이었다. author의 non-null Goal Test binding positive는 새 independent 실행에서 수락과 이전 ID 보존을 확인했다. command subprocess는 합성 stub이며 live provider/Windows owner-lock 경로가 아니다. default TestCase discovery나 broad campaign을 실행하지 않았다.

이와 별도로 reviewer 작성 `delta_probe.py`로 **11/11 assertion 통과**, wall 1.768919s, exit 0을 얻었다. 각 사례는 새 disposable synthetic 원장을 사용한다. 의도/receipt도 synthetic CREATE_THREAD 응답으로 명시 등록해 보존 assertion이 빈 table 비교에 그치지 않도록 했다. 실제 provider receipt라고 표현하지 않는다.

| reviewer probe | 결과 |
|---|---|
| 같은 Plan/binding/evidence의 기존 PASS ID, 이후 새 PASS | 수락; saved verdict는 old ID 그대로; Plan/project 완료; active pointer NULL |
| 최신 integration FAIL / INCONCLUSIVE | 둘 다 거부; 모든 관련 원장 행 전/후 동일 |
| completed Task의 새 FAIL / INCONCLUSIVE | 둘 다 거부; active Plan 유지 |
| wrong Goal digest / wrong activation digest | 둘 다 원자적 거부 |
| unknown result ID / FAIL result ID 뒤 새 PASS | 둘 다 원자적 거부 |
| 다른 Plan의 실제 PASS ID | 원자적 거부; active Plan 유지 |
| superseded Plan의 satisfied 제출 | 원자적 거부; replacement pointer와 run_state=active 유지 |

거부 비교에는 projects/plan_revisions/task_contracts/goal_verdicts/history_events/evidence_records/validation_results/attempts/runtime_intents/runtime_receipts/task_completion_reuse의 전체 행 canonical hash를 사용했다. 수락에서는 기존 PASS 두 행과 evidence/Attempt/intent/receipt가 변하지 않았다. 모든 synthetic History hash chain이 유효했다. 이전 ID를 latest ID로 자동 치환하지 않았다는 assertion과 Plan/project 완료 assertion도 별도로 수행했다.

## 남는 경계

D1 Windows 경계를 변경·우회하지 않았다. public provider E2E, 실제 wheel 설치, native Windows, 실제 semantic Validator terminal receipt, live provider·operational DB/profile/keys, multi-process race/fault와 광범위 qualification은 NOT_RUN이다. source-tree Core API와 stub Goal Test 관측은 public vertical E2E가 아니다.

integration의 latest 정렬은 기존 dispatcher와 같은 `(evaluated_at,rowid)`다. Task는 effective Worker epoch/History sequence다. 이전 보고서의 정렬 의미 및 criterion evidence 검증 한계는 계속 적용된다. 이 좁은 re-review에서는 해당 범위를 넓히거나 broad baseline/probe를 다시 실행하지 않았다. source GO·merge·F07 순서·release 판단은 parent의 별도 책임이다.

## 정확한 실행과 게시 provenance

argv/cwd/PYTHONPATH/UTC 시작·종료/exit/wall seconds/raw SHA-256은 `delta-commands.jsonl`에 있다.

| 실행 | cwd | exit | wall seconds | raw SHA-256 |
|---|---|---:|---:|---|
| related regressions 18 | `/workspace/vm-goal-independent-r2-delta-20261002/candidate` | 0 | 2.813009 | `ed5effdc4f8f44512e225d031c083ba9a6bf2412d4b247b40e2d5d8dc2caad7b` |
| reviewer boundary probe 11 | 같은 candidate | 0 | 1.768919 | `4b06d6ba3a5dae380dc244d4181ddfe88b549e38b123cd2fcc406d334d413b5f` |

원격 branch 생성 전 connected GitHub app에서 repository visibility=private, push permission=true, review branch 검색 결과 없음, proposal branch 존재를 확인했다. Git CLI의 `ls-remote`는 username 인증 실패, gh API는 Forbidden이었다. 이 실패를 성공으로 기록하지 않았다. GitHub app Git API로 역사 파일 tree와 새 delta tree를 게시한다. API-generated remote commit은 metadata/parent가 로컬 prepared commit과 다를 수 있으므로 SHA를 같은 것으로 주장하지 않는다. historical bytes/tree와 final outgoing tree는 별도로 대조한다. 실제 branch/commit 및 postpublish 검증은 parent에게 전달하는 최종 결과와 publication receipt에 기록한다. main·tag·PR·merge를 생성하지 않는다.

Artifact whitespace 검사에서 raw diff의 blank context 줄(` `)이 trailing whitespace로 표시됐다. 원시 hash 보존을 위해 두 exact raw-diff 파일을 검사에서 제외하고, 역사 fixture의 EOF 빈 줄만 제외한 나머지 staged 문서 검사를 수행한다. candidate product diff의 whitespace 검사는 별도로 exit 0이었다. raw bytes를 정규화하지 않았다.

## 최종 게시 상태

**NOT_PUBLISHED — automatic approval review 거절.** private repository/계정 owner/push 권한/sanitized bytes를 확인한 뒤 동일 GitHub create_tree 작업을 재요청했지만, trusted user-authored approval로 내부 source 발췌·raw evidence의 정확한 disclosure를 승인한 근거가 없다는 이유로 다시 거절됐다. 추가 쓰기나 우회를 중단했다. readonly branch 재조회 결과 원격 review branch는 없다. `publication-receipt.json`에 사유·시점·checks·실제 reviewer identity를 기록했다. trusted 사용자 승인을 확인하기 전에는 이 packet을 업로드하지 않는다. 기술 검토의 B1 해소 판정은 그대로이며 게시만 차단됐다.


## 현재 게시 상태 — 2026-10-02

직접 사용자 승인 후 지정 private evidence branch 게시와 원격 readback이 완료됐다. [publication-success.json](publication-success.json)에 실제 로컬/원격 SHA, tree 일치, docs-only 범위, 원본·R2 raw hash 확인을 기록했다. 위 차단 기록은 당시 상태이며 현재 게시 blocker는 없다. 제품·main·tag·PR·merge 변경은 없다.
