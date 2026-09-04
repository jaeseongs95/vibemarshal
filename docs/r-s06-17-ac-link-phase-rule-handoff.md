# R-S06-17 복합 phase AC 연결 규칙 및 마지막 제한 진단

## 결론

AC statement 또는 `validation_intent`가 동일 절차의 task/goal phase를 각각 명시하면, 명시된 각 phase를 실제 수행하는 validation을 각각 `ac_link_required=true`로 판정하도록 provider 지침을 보강했다. 별도 실행은 실행·evidence 분리이며 task phase를 선택 사항으로 만들지 않고, 명시되지 않은 sibling unittest·scope·semantic validation에는 연결 의무를 전염시키지 않는다.

결정론 Gate는 최종 run root에서 5/5 PASS했다. 제한 실제 평가는 첫 `clean`에서 목표 pair를 포함한 28/28이 일치해 다음 사례로 진행했으나, 두 번째 `bad`에서 기대 결함 1건과 boolean 3건이 불일치해 계약대로 즉시 중단했다. 이 실행은 진단 캠페인상 허용된 두 번째이자 마지막 동일 실패 재계획이며, 같은 `ac_004 × val_task_add_behavior_contract` 오판이 다시 나타났으므로 추가 재호출은 금지한다. 1.0 cutover는 **NO-GO**다.

## 권한과 변경 경계

- 자식 turn에서 실제 관측한 정책은 unrestricted `danger-full-access`, `approval_policy=never`였다.
- 변경 범위는 provider 전용 field 설명·공유 작성/검토 지침, AC-004 회귀, 한 문장 권위 규칙 동기화와 새 run prefix/session launcher로 제한했다.
- Core, Goal/Plan 권위 schema, DB/runtime/ledger, evaluator의 exact boolean 비교는 변경하지 않았다.
- Goal/Plan 입력, 기대 `true`, oracle, threshold, model/effort binding도 변경하지 않았다.
- 기존 v4/v5 fixture와 R-S06-14/R-S06-15 artifact·raw·receipt·digest는 수정하지 않았다.
- Plan activation, Worker 실행, 새 orchestration ledger 쓰기는 없었다.

## 구현

- `src/flowmarshal/engine/plan_inspection.py`
  - `ac_link_required` field 설명 바로 옆에 복합 task/goal phase 양성 규칙과 sibling 비전염 규칙을 추가했다.
  - matrix 작성 지침에서 양성 규칙을 먼저 적용하고 음성 과잉연결 금지 규칙을 뒤에 판정하도록 순서를 고정했다.
- `src/flowmarshal/engine/planner_roles.py`
  - Plan 작성·검토 공통 규칙과 단계별 trace 지침에 동일한 우선순위를 반영했다.
- `tests/test_engine_inspection_fixture_revision.py`
  - AC-004형 복합 phase에서 task oracle과 goal oracle은 `true`, 별도 task unittest와 scope 검사는 `false`인 회귀를 고정했다.
- `tests/test_engine_plan_inspection.py`
  - provider schema 설명에 규칙이 포함되고, 양성 규칙이 sibling overlink 방지 규칙보다 먼저 배치되는지 검증한다.
- `AGENTS.md`, `docs/orchestration-redesign.md`
  - 같은 의미를 각각 한 문장으로 동기화했다.
- `scripts/diagnostics/r_s06_10.py`
  - 새 `R-S06-17` session과 `r-s06-17-*` fresh run root만 최소 추가했다.

## 결정적 검증

- 집중 inspection 회귀: **53 tests, PASS**
- 전체 테스트: **555 tests, PASS**, 60.204초
- `compileall src scripts tests`: PASS
- `pip check`: `No broken requirements found.`
- `git diff --check`: PASS
- legacy freeze: **40개 경로 PASS**, manifest `sha256:25f21e8d09fb20f1aa0b3d28f5e1946dc4423aff0c5edf7c23e77c7f62bd1f5a`

최종 결정론 Gate:

- run root: `.flowmarshal-engine-eval/runs/r-s06-17-20260905-v2/deterministic`
- status: `COMPLETED`, **5/5 PASS**, failure 0
- contract digest: `sha256:dc1b5a0e22c9e45ab5794d50b6e2236464544985be9390f4269491664815b4db`
- report digest: `sha256:9e5001dc90f5a407ad354046e1feabb6e715b5a720c5053ffa1b7e465cc50759`
- report bytes SHA-256: `8fe26989a19bef749c495b8c97c8dffa68887a41b5a225b40dfe06aca51d95bf`

첫 `r-s06-17-20260905-v1` 결정론 run은 사전 수동 `compileall src scripts tests`가 fixture 하위에 만든 `__pycache__` 디렉터리를 기존 테스트가 파일로 읽으면서 `PermissionError`가 나 4/5 FAIL했다. provider 호출은 없었다. 생성 캐시는 삭제 대신 `.flowmarshal-engine-eval/cache-quarantine-r-s06-17`로 이동해 복구 가능하게 보존했고, 새 v2 root에서 Gate 전체를 다시 실행해 통과했다. v1 artifact는 덮어쓰지 않았다.

## 실제 평가 사전 결속

- run root: `.flowmarshal-engine-eval/runs/r-s06-17-20260905-v2`
- source manifest: `sha256:368e92c7ea694b3899b2566173ed46c863992d533346e368f1af85a2171d30f2`
- prompt digest: `sha256:9fc2d9e69e92a9de4b9b8445b6234c12e2e24aed0bee02d0c82551f8149195fd`
- rules digest: `sha256:081e20a28f2be6a86dd8f4139f93474d1e751353f2808339e4e45d595f7f8871`
- output schema digest: `sha256:ca53c811d44020d80d684f54c7d501cd79603f0d707e5f2ccef133b834b08f1e`
- preflight lock: `sha256:d96c036d4e4e5e2cf7ea6dc35d09ee9e2f37e34dbf9a1b214cb5e4471bcd2284`
- model lock: `sha256:5cca3c82ad7d7eb4e61aea0dba9e2101a465860b8abe1ec771538bc7d13762ec`
- 일반 Reviewer: `gpt-5.6-terra/high`
- 최대 logical/provider turn: 13/13, schema recovery: 0, 첫 실패 즉시 중단

## 모든 실제 호출 결과

| # | 사례 | provider 상태 | 의미 평가 | receipt canonical digest | output digest | token(input/cached/output/reasoning/total) | latency / provider |
|---:|---|---|---|---|---|---:|---:|
| 1 | `clean` | `succeeded` | PASS, pair 28/28·목표 pair 일치 | `sha256:3df21691152dfdbbc9ccb00ab462dc33cace403981f39c332e301601afbfd028` | `sha256:8f3170dc57f84d4ad4b64de7bb7300b124bd98d48174db1028c65e001a7e7fe1` | 44,703 / 0 / 6,529 / 3,106 / 51,232 | 121,500ms / 120,385ms |
| 2 | `bad` | `succeeded` | FAIL | `sha256:8e5baa368d38bf337b40a4d6434a6b6e3faedc0e7c51cfcdaeb7fdc60725f23c` | `sha256:f71c1071374361443eae42f3b8810a908d72960ef3ea7d2aa4e13cca2d176d46` | 44,721 / 0 / 9,042 / 1,243 / 53,763 | 167,672ms / 165,301ms |

두 호출 모두 `gpt-5.6-terra/high`, permission `:danger-full-access`, approval `never`, 한 thread당 빈 새 thread의 단일 turn, schema recovery 0이며 request/schema/thread/turn/terminal receipt 결속 검사가 모두 통과했다.

합계:

- logical calls/provider turns: **2/2**
- input/cached/output/reasoning/total tokens: **89,424 / 0 / 15,571 / 4,349 / 104,995**
- 역할 latency/provider duration: **289,172ms / 285,686ms**
- reasoning token은 provider total에 포함되어 별도로 더하지 않았다.
- 청구 금액은 provider가 제공하지 않아 `null`이다.

## 잔여 실패와 중단 판정

`clean`은 missing/unexpected finding 없이 목표 `ac_004 × val_task_add_behavior_contract=true`를 포함해 전체 28개 행이 일치했다.

`bad`의 잔여 실패:

1. 고정 기대 결함 `task-phase-overclaim`을 제출하지 않았다.
2. `ac_003 × val_goal_independent_behavior_contract`: 기대 `true`, 실제 `false`.
3. `ac_003 × val_task_add_behavior_contract`: 기대 `true`, 실제 `false`.
4. `ac_004 × val_task_add_behavior_contract`: 기대 `true`, 실제 `false`.

두 번째 호출 직후 stop-on-first-failure를 적용했다. 따라서 `wrong-goal`, `combined`, `boundary-clean`, `missing-link`, `future-result`, `stored-expanded`, `semantic-explicit`, `stored-multi-defect`, `semantic-missing-link`, `expansion`, `expanded-review` 11개는 미실행이다. 새 근거 없는 동일 실패 재호출, 모델 교체, oracle·기대값·threshold 변경, 자동 보정은 하지 않는다.

주요 artifact bytes SHA-256:

- `summary.json`: `82dc9b610511b27d2790420c505c63fa1b0bd3db138e989600ffd543470104bc`
- `clean-assessment.json`: `dc0497ddd0a6d4c539e5920bf87b24436166de553036b445dc4e6f4bda7f1f28`
- `bad-assessment.json`: `88ede2071f82160c58c3543af0c6bb074e854656eabf1ce69d3c13f42609b189`
- clean `result.json`: `829ff406c7413d55e66baf1da84c4821e4e599bfb7300e758ca98d8e3b0c6786`
- bad `result.json`: `bf537ed3262fb509ed9ae7bc69bbdf3642cbeecfa1aaf531cf4541a6f4175b19`

## 보존 확인과 1.0 판정

- v4 expectations/review: `f132337b8b237e2379c6315a3b12ff18bfd5d24f8d62be6e29608b8bf963b011`, `7b664981550bd50d53f19345052da0f92410e44f44458a9eb55fb3b291a3bd2d`
- v5 expectations/review: `a826a9da5ef2faad40da1e8462206598f2a69bcc6dd52efa34a2b0791c871ba1`, `9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27`
- R-S06-14 summary: `8b15196685080a00c03ab7a33299597bac695bda52b2435220a421639bbb986d`
- R-S06-15 summary: `0f814c985c4042a1109eb28c34060047b559879e3b72df5b0625e0b696db2132`

최종 summary는 `status=FAIL`, `full_qualification=NOT_RUN`, `plan_activated=false`, `worker_executed=false`, `new_ledger_writes=0`, `cutover=NO-GO`다. 결정론 Gate 통과만으로 실제 역할 회귀·전체 planning pipeline·실제 프로젝트 E2E·token/latency Gate를 대체할 수 없고, 제한 실제 평가도 두 번째 사례에서 실패했으므로 FlowMarshal 1.0은 **NO-GO**다.
