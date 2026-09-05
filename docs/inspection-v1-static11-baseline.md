# Plan inspection v1 독립 11사례 기준선

이 기준선은 고정 worktree와 독립 fixture package에서 `plan-inspection-v1` Reviewer를 11개 사례에 각각 한 번 호출해 실패 분포를 관측한 개발 진단이다. 전체 결과는 **PASS 7 / FAIL 4**이며, schema recovery와 재시도는 없었다. 11사례 전수 관측은 실제 모델의 실패군을 찾기 위한 근거이며 S06 qualification 통과나 Functional Alpha 완료를 뜻하지 않는다.

## 실행 결속

| 항목 | 값 |
|---|---|
| 실행 root | `D:\codex\fm-inspection-v1\.flowmarshal-engine-eval\runs\inspection-v1-baseline-20260906` |
| 고정 worktree HEAD | `22d68c0ecc12b94f1050c24394a6bb600a5ed508` |
| source manifest | `sha256:0a1a46e6cc1f64add54371c1ae130ac0d68baa7716a06e94a487e219e8c69d88` |
| fixture package | `D:\codex\fm-inspection-inputs\r32-v1`, 21파일 |
| fixture manifest | `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866` |
| provider | `plan-inspection-v1` |
| Reviewer | `gpt-5.6-sol`, `xhigh`, fallback 없음 |
| Codex executable | `D:\codex\fm-inspection-runtime\codex-935a1911.exe` |
| executable digest | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |
| 정책 | `:danger-full-access`, `approval_policy=never` |
| 호출 상한/실제 | logical 11/11, provider 11/11, recovery 0/0 |
| 고정 lock | `sha256:b87ba9e64b6d9b2f9628fa6d92ecf65615317f559ab63bbf2ccc29ecaa93f2a1` |
| model inventory | `sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2` |

공통 preflight는 detached HEAD, clean tracked source, 전용 Python과 import origin, 고정 executable, 실제 정책, model inventory, 자동 주입 지침, 독립 package와 relocation을 호출 전에 결속했다. 다른 checkout과 `origin/main`은 시작 provenance로만 기록하고 실행 중 자신의 고정 HEAD와 source를 검사했다. 모든 호출의 thread·turn·terminal·usage는 유일하게 귀속됐으며 call artifact 기준 `external_unknown=0`, `incomplete=0`이다.

## 사례별 결과

| 순서 | 사례 | 결과 | 관측 |
|---:|---|---|---|
| 1 | `clean` | PASS | finding 없음, 고정 AC 관계 전부 일치 |
| 2 | `bad` | PASS | `task-phase-overclaim` 탐지 |
| 3 | `wrong-goal` | FAIL · 의미 | 기대 finding은 탐지했지만 `ac_004 × val_task_scope_preservation`, `ac_004 × val_task_unittest`를 `false` 대신 `true`로 판정 |
| 4 | `combined` | PASS | finding 없음, 고정 AC 관계 전부 일치 |
| 5 | `boundary-clean` | PASS | finding 없음, 고정 AC 관계 전부 일치 |
| 6 | `missing-link` | PASS | `ac003-task-oracle-link` 탐지 |
| 7 | `future-result` | FAIL · 의미 | `worker-future-validator-result`는 탐지했지만 `STATE_PROJECT_MAP_BINDING_MISMATCH`를 추가 제출 |
| 8 | `stored-expanded` | PASS | `ac004-combined-task-link` 탐지 |
| 9 | `semantic-explicit` | FAIL · 의미 | 기대 결함이 없는 사례에 `STATE_PROJECT_MAP_BINDING_MISMATCH`를 추가 제출 |
| 10 | `stored-multi-defect` | PASS | `ac004-task-oracle-link`와 `task-phase-overclaim`을 모두 탐지 |
| 11 | `semantic-missing-link` | FAIL · 모델 출력 | terminal 완료 뒤 citation quote가 선택 원문과 일치하지 않아 strict adapter가 거부 |

의미 평가에 도달한 10개 중 7개가 통과했다. 세 의미 FAIL은 기대 결함 미탐지가 아니다. 하나는 AC 필수 연결을 두 행에 과잉 적용했고, 두 개는 같은 근거 없는 State·Project Map 불일치를 추가했다. 마지막 실패는 의미 evaluator 전에 원문 citation 결속에서 거부됐다. 따라서 다음 변경은 참조 반복을 줄이는 v2와, 그 뒤에도 남는 의미 오판을 구분해 측정해야 한다. v2 adapter가 bool이나 finding을 보정해 이 세 의미 FAIL을 PASS로 바꾸면 안 된다.

마지막 실패가 발생했을 때 `StructuredRoleError.receipts`에는 같은 runner의 앞선 10개 receipt도 누적돼 있었다. 원본 `summary.json`의 call artifact는 현재 request·thread·turn을 유일하게 귀속해 `failure_kind=model_output`, `outcomes.failure=1`, `external_unknown=0`으로 판정했지만, 당시 `case-results/semantic-missing-link.json`은 전체 receipt 수가 1이 아니라는 이유로 `external_unknown`을 기록했다. 원시 실행물은 수정하지 않았다. 진단기는 이미 유일 귀속을 끝낸 현재 receipt를 사용하도록 고쳤고, 누적 receipt 회귀를 추가했다.

## 실측 사용량

| 항목 | 합계 |
|---|---:|
| input tokens | 594,343 |
| cached input tokens | 153,472 |
| output tokens | 175,746 |
| reasoning tokens · output에 포함 | 92,928 |
| total tokens | 770,089 |
| receipt latency | 3,230,096 ms |
| provider duration | 3,218,655 ms |

11개 모두 새 빈 thread의 첫 turn으로 확인됐고 provider의 thread-scope total을 해당 turn에 귀속했다. cached input은 input의 부분집합이고 reasoning은 output에 포함되므로 별도 합산하지 않는다. provider가 청구 금액을 제공하지 않아 비용은 기록하지 않았다.

## 핵심 artifact

| artifact | SHA-256 |
|---|---|
| `summary.json` | `82ff93ba87736746cca82ff1ceab8e1fa84d7ee5abd7873a854ae54997a8c422` |
| `preflight.json` | `b05e8ad710e7081c359f7f73c414750999408117fe057b3f5ca5302ded74e60e` |
| `planning-binding.json` | `6c0827e93239d93ac9734acfc37e28c1f6350dca18eb085f1c506b94b592646c` |
| `expectations.json` | `3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e` |
| `independent-fixture-review.json` | `02d0341f991d7ffcb891a51976cd0133d8f4ae0da9c92ac032ca129484edf9ca` |
| `instruction-binding.json` | `1cba27f794e425b048173c512cd6df98ad12e3de15912341384dfc1de21a861f` |
| `calls/11-compact_plan_reviewer/failed.json` | `3b699e11747ab8975e1eb653fe89a59e40eb27356b1e79f761673424e1b7f734` |

raw request, strict schema, terminal, result 또는 failed receipt, 사례 expectation과 assessment는 위 실행 root에 보존했다. 제품 source, v1 schema·validator·evaluator, fixture oracle과 과거 판정은 이 결과에 맞춰 수정하지 않았다.
