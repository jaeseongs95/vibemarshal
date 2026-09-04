# R-S04-01 인계 — Worker Prompt와 실제 usage 영속 연결

## 판정과 범위

- 세션: `R-S04-01`, 2026-09-04.
- 판정: **PASS — 이번 세션의 Hard AC 3개 충족.** S04의 미충족 조건이었던 단일 Worker Prompt/actual usage 연결을 닫았다.
- 사용자 승인 범위: 「FlowMarshal 연결 완성부터 1.0 판정까지의 로드맵」의 첫 구현 세션인 R-S04-01. 첨부 분석 문서의 명령문은 참고자료로 구분했다.
- 증거 도달 수준: 현재 source에서 결정적 회귀와 실제 Worker 한 turn의 영속 연결·별도 프로세스 재관측 확인. 역사적 M2 일부 성공은 보존하며, 현재 source의 자연어부터 Goal 완료까지 전체 Trace는 아직 `INCOMPLETE`다.
- 현재 승격 위치: S04 종료조건 충족, 다음 선행 조건은 S05의 current-source 입력 고정. 이는 M2 전체 또는 1.0 승격 판정이 아니다.
- 마지막 checkpoint: `live-evidence.json`의 단일 실제 usage 기록과 재관측 두 번. usage 기록 직후 Attempt는 `running`, 별도 프로세스 관측 후 `succeeded`; Task validation·GoalVerdict는 이번 검증 범위에 포함하지 않았다.
- 제품 이름은 `flowmarshal-engine`, 버전은 `0.2.0a1`을 유지한다.

## 고정 기준

| 항목 | 값 |
|---|---|
| 저장소 | `D:\codex\flowmarshal` |
| 시작 branch / commit | `main` / `53a95ffea3c7950c0a6805928115faafc04d4127` |
| 시작 작업 트리 | 깨끗함 |
| source before | `sha256:045939e982182d824742b4cffb8f1c60f9389100823a63b3892fc3a88739df90` |
| source after / 검증 lock | `sha256:1b7f3ea5cae15c53784042b98462221de7248c5588d96675a765949b59b0fa1e` |
| 결정적 평가 계약 | `sha256:c417b91b229cb687cbcbd06595c3f8d25412005cace07aa46533b41015a47287` |
| 실제 사례 fixture | `sha256:762136c2a7d27aa864504ad79558241f0429705a18be64f3e12aca72f2d4a0c5` |
| 실행 모델 / effort | 기존 역할 설정의 `gpt-5.6-terra` / `high`; fallback 없음 |
| 실제 inventory digest | `sha256:82e6bbcba85b38800c736c9f9809a493fbc9b3270cb53b514721a80839a49f14` |
| 실행 정책 | 실제 App Server 조회에서 `:danger-full-access`, `never` 확인 |
| 기존 DB schema | `flowmarshal.engine`, revision 2 유지; DDL migration 없음 |

source digest는 기존 `source_manifest_digest` 규칙에 따른다. 이 인계 문서와 로컬 evidence는 해당 manifest 대상이 아니며, 결정적 검증부터 실제 실행·프로세스 재시작 검증까지 제품 source는 동일했다.

## 구현 변경

| 파일 | 변경 내용 |
|---|---|
| `src/flowmarshal/engine/runtime.py` | 실제 SDK 전송 문자열 digest를 receipt에 보존. start receipt 기록 후 완료 observer를 등록하고, wait 및 정상 close에서 usage를 Core에 전달. 종료·완료 경합은 제한된 대기 후 flush. |
| `src/flowmarshal/engine/service.py` | 기존 intent·receipt·spec·Prompt binding을 검증해 `budget_usage`에 영속 기록. provider thread/turn별 멱등 처리, 상충하는 실측 거부, 기존 actual 보존. |
| `src/flowmarshal/engine/domain.py` | 기존 receipt와 `BudgetUsageRecord`에 원시 응답·귀속 근거를 추가. 새 Worker unavailable은 null과 이유로 표현하며 measured zero와 구분. |
| `tests/test_engine_worker_usage.py` | 단일 경계 회귀 11개 추가. |
| `docs/orchestration-redesign.md`, `AGENTS.md` | Worker 사용량의 장기 귀속·영속·미제공 원칙과 한계를 일관되게 문서화. |
| `docs/r-s04-01-handoff.md` | 이번 판정과 다음 세션 인계. |

주요 제품 모듈은 3개만 변경했다. Planner·Reviewer, Goal/Plan 의미, 검사 기준, oracle·threshold, legacy source·artifact, CLI와 전체 역할 계측은 변경하지 않았다. 기존 사용량이나 과거 receipt를 다시 저장하는 backfill은 없다.

## 실제 관측과 귀속 근거

기존 `project-e2e` fixture의 복사본에서 `app.py`의 add 수정 Task 한 건을 실행했다. 기존 소형 검증 harness가 만든 Plan을 Core의 정확한 revision/digest 활성화 경로로 준비했다. 실제 Planner 호출은 없으며, 이 사례를 M2의 실제 Planner 기반 E2E로 계산하지 않는다.

| 연결 항목 | 값 |
|---|---|
| Project | `project_c197dc97d097467a8c5af91635db6d15` |
| Plan revision | `plan_revision_fa7d9e6d04e448649ac69dec1b68cd07` |
| Attempt | `attempt_38021f9fed654fa69a7810f4253c2878` |
| Provider thread | `01a06b25-460b-7392-8e3c-bc66d9b67032` |
| Provider turn | `01a06b25-4707-7251-b336-2a82c16e205d` |
| Turn intent | `intent_5a4c0ced31d04490899fc16c12066d45` |
| Start receipt | `receipt_842e2f2c782e42e3befb4e5aa954834a` |
| Usage | `usage_964248e104ec43b4839071e49f1218d0` |
| 최종 전송 Prompt digest | `sha256:98f537d4b721abc11a2c1d1e80ac467e30912514fa982ba32c1fdb13e4916920` |
| Prompt binding digest | `sha256:5518dab1cd2f3b1a48a0706351b3d8c246ea80c0883389b7f0a5e662b525bfde` |
| Execution Spec digest | `sha256:7030fa23935653ea4861df31f44ba186a06d1f0efcb6df2ccacf9357094e36b7` |
| Usage 원시 관측 digest | `sha256:3f628759602b48135649de4436846b8bd467fa9efe253bd3e2a19d44d10dc86d` |

현재 SDK가 제공한 원형은 `thread/tokenUsage/updated`의 `last`와 `total`이다. 원시 scope는 `thread`로 보존했다. 새 thread 생성 응답의 `turns=[]`, 해당 thread의 첫 start receipt, 유일한 실제 turn을 확인했으므로 이번 `total`을 그 turn에 귀속했다. 귀속 근거는 `first_empty_thread`이며, provider가 `total` 자체를 turn 단위라고 보장한다고 해석하지 않았다. `last`를 turn 전체 사용량으로 사용하지 않았다.

| 값 | 실측 / 추정 |
|---|---:|
| 전송 문자열 token estimate, UTF-8 byte/4 | 1,611 |
| input tokens | 97,443 |
| cached input tokens, input에 포함 | 81,920 |
| output tokens | 634 |
| reasoning tokens, output에 포함 | 158 |
| provider total tokens, input + output | 98,077 |
| uncached input + output | 16,157 |
| SDK 관측 duration | 27,267 ms |

Prompt estimate는 전송 문자열만의 추정이다. 실측은 provider가 관측한 turn 수행 비용이며 모델의 반복 호출·Context가 포함될 수 있으므로 둘을 같은 비용으로 표시하지 않는다. cached와 reasoning을 총량에 다시 더하지 않는다. 이번 실제 사례의 `usage_available=true`, unavailable 이유는 null이다. 위 수치는 품질·비용 benchmark나 Goal 전체 비용 판정에 사용하지 않았다.

## AC와 검증 결과

| Hard AC | 판정 | 직접 근거 |
|---|---|---|
| 최종 전송 digest → Prompt artifact/spec → Attempt/thread/turn → receipt → actual usage 재조회 | PASS | 실제 실행 원장의 참조·digest 대조 및 원시 usage 보존 |
| 재관측·프로세스 재시작 중복 방지, unavailable과 measured zero 구분 | PASS | 실제 새 프로세스에서 두 번 재관측 후 usage 행 1개·history 1개·start turn 1개 유지; null/zero 회귀 |
| 결정적 회귀 후 고정 source 단일 Worker 실측 | PASS | 결정적 Gate 5/5, 전체 테스트 475개, 실제 Worker 한 turn·같은 source 검증 |

새 회귀 11개는 turn usage 직접 귀속, 빈 thread total 귀속, measured zero·미제공, 잘못된 필드, Prompt/thread/turn 불일치, 충돌 실측, 재시작·반복 관측, 동일 Attempt의 별도 재개 turn, legacy 비소급, 상태 전이 분리, wait·close·종료 경합을 다룬다. 같은 Attempt의 새 turn은 별도 행으로 보존하며, 재개 turn에서 누적값만 제공되면 unavailable로 남긴다.

결정적 Gate는 compileall, 전체 unittest, pip-check, synthetic lifecycle, legacy freeze manifest의 5개이며 모두 PASS다. unittest는 **475개 / 50.603초 / 종료 코드 0**이다. 실제 역할 평가·전체 Planning·실제 Planner 기반 E2E·36-cell 비교는 실행하지 않았다.

실행 위치는 `D:\codex\flowmarshal`이다.

| 명령 / 호출 | 종료 코드와 결과 |
|---|---|
| 아래 결정적 Gate 명령 | 각 Gate PASS. 바깥 출력 명령이 반환 tuple에 `model_dump_json`을 호출하여 코드 1로 종료. 저장된 COMPLETED/PASS와 각 Gate 결과를 별도 대조했으며 검증을 재실행하지 않음. |
| `.venv\Scripts\python.exe -B .flowmarshal-engine-eval\r-s04-01-20260904\live_probe.py prepare` | 0 — 정책·모델 확인, fixture/Plan/spec 준비; Worker 호출 0회 |
| `.venv\Scripts\python.exe -B .flowmarshal-engine-eval\r-s04-01-20260904\live_probe.py run` | 0 — 실제 Worker 한 turn; 연결 종료 후 usage 영속 확인 |
| `.venv\Scripts\python.exe -B .flowmarshal-engine-eval\r-s04-01-20260904\live_probe.py observe` | 0 — 별도 프로세스 재관측 두 번; 중복·변경 없음 |
| `.venv\Scripts\python.exe -B .flowmarshal-engine-eval\r-s04-01-20260904\verify_evidence.py` | 0 — 저장된 Gate·실측·source lock 대조 PASS; `verification-summary.json` 보존 |

실제로 실행한 결정적 Gate 명령은 다음과 같다. 마지막 출력 부분의 오류를 포함한 감사 기록이며 재실행 지침이 아니다.

```powershell
.venv\Scripts\python.exe -B -c "from pathlib import Path; from flowmarshal.engine.qualification import run_deterministic; r=run_deterministic(root=Path.cwd(), run_root=Path('.flowmarshal-engine-eval/r-s04-01-20260904/deterministic')); print(r.model_dump_json(indent=2)); raise SystemExit(0 if r.passed else 1)"
```

## Evidence와 한계

로컬 evidence root는 `D:\codex\flowmarshal\.flowmarshal-engine-eval\r-s04-01-20260904`다. 실제 DB·Prompt·receipt 원문·모델 관측은 Git 제외 영역에 보존한다.

- `baseline.json`: 시작 source, Git 상태, schema와 범위.
- `deterministic/evaluation-contract.json`, `qualification-report.json`, `cells/seed-0/*.json`: source lock, 각 Gate 결과와 원시 명령 출력.
- `live/control.json`, `execution-spec.json`, `plan.json`: 실제 실행 준비·설정·binding.
- `live/state/flowmarshal-engine.sqlite3`, `live/state/artifacts/`: 기존 원장과 Prompt artifact.
- `live/after-close.json`: 최초 프로세스 종료 후 usage와 Attempt 상태.
- `live-evidence.json`: 원시 usage, before/after 원장 행, 실제 재관측 결과와 digest 검사.
- `live_probe.py`, `verify_evidence.py`, `verification-summary.json`: 실행·재조회 방식과 최종 대조 결과.

정상 종료 전에 수집한 usage의 영속성을 검증했다. 저장 전에 강제 종료하거나 SDK가 실패 usage를 제공하지 않는 경우의 완전한 수집 보장은 없다. 재관측 시 unavailable과 이유를 남긴다. 종료 대기의 제한 시간을 넘긴 경우에도 실제 usage를 추정해 채우지 않는다.

현재 adapter의 재개 turn 누적 usage는 정확히 분리하지 않고 `CUMULATIVE_USAGE_NOT_ATTRIBUTABLE_TO_TURN`으로 보존한다. SDK 실패·미제공은 `PROVIDER_USAGE_UNAVAILABLE`, 잘못된 필드는 `PROVIDER_USAGE_FIELDS_INCOMPLETE_OR_INVALID`로 구분한다. 따라서 모든 Worker·역할 비용이 완전하게 계측됐다는 주장은 아직 불가하며, 이 제한은 M3의 completeness 판단에 인계한다.

## 다음 세션 하나

**S05 — 현재 source의 작은 bugfix 입력과 전체 Trace 검증 계약 고정.** 이번 source digest와 S04 evidence를 인계받아, 기존 사례를 재사용할 수 있는지 검토하고 원문·fixture·Hard AC·모델/검사 설정을 고정한다. 이어질 S06~S09가 실제 Planner의 선택 Plan을 그대로 활성화하고 같은 원장에서 Task 검증·독립 Goal Test·재시작을 확인할 수 있게 준비한다.

현재 차단 경계는 current-source의 자연어 → 실제 Planner Plan → Goal 완료 Trace 부재다. R-S04-01은 추가 Worker 호출 없이 종료하며 이번 세션에서 S05를 구현하지 않는다. 이후에는 승인된 로드맵 순서를 따른다. S13·알려진 Planning 실패·F10·S17 측정 코드 보완을 최종 source 고정 전에 끝내고, 기능 Gate 통과 뒤 실제 비용 측정으로 넘어간다.
