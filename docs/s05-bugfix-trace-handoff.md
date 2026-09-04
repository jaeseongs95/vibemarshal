# S05 인계 — 단일 bugfix 입력과 검증 계약 고정

## 판정과 범위

- 세션: `S05`, 2026-09-04. 이전 [R-S04-01 인계](r-s04-01-handoff.md)와 승인된 로드맵의 다음 완료 단위를 수행했다.
- 증거 도달 수준: 단일 입력·Goal 후보·초기 snapshot·동작 oracle 고정, oracle 회귀와 결정적 Gate. **실제 자연어 → Planner → Goal 완료 Trace는 아직 INCOMPLETE**다.
- 현재 승격 위치: S05 입력 준비 완료, 다음 세션은 **S06 — 실제 자연어에서 Selected Plan까지**다. M2 전체와 1.0은 미완료이며 제품 이름·버전은 `flowmarshal-engine 0.2.0a1`이다.
- 마지막 checkpoint: `input-lock.json`과 변하지 않은 `workspace`·`initial-snapshot`. 실제 Goal·Plan·Attempt·Worker 실행 기록은 생성하지 않았다.
- Engine 구현, 기존 fixture, 과거 원장·receipt, oracle·합격선의 역사적 판정은 수정하지 않았다. 새 시나리오의 기대값을 실행 전에 정의했으며 기존 평가 기준을 완화하지 않았다.

## 고정 입력

| 항목 | 값 |
|---|---|
| fixture ID | `bugfix-add-trace-v1` |
| 원본 프로젝트 | `tests/fixtures/engine/project-e2e` |
| 재사용 기준 | 원본의 `app.py`, `test_app.py`, `AGENTS.md` 세 파일을 byte 단위로 보존 |
| 시나리오 계약 | [contract.json](../tests/fixtures/engine/bugfix-trace/contract.json) |
| 결정적 검사 | [oracle.py](../tests/fixtures/engine/bugfix-trace/oracle.py) |
| oracle 회귀 | [test_engine_bugfix_trace_fixture.py](../tests/test_engine_bugfix_trace_fixture.py) |
| 시작 HEAD | `b02cb91de23b28c57cb1309ef2b53bfdb905d533` |
| 시작 source digest | `sha256:1b7f3ea5cae15c53784042b98462221de7248c5588d96675a765949b59b0fa1e` |
| S05 source digest | `sha256:d697a1cdc06faea9c5123ed696ef1cbd39b9f619a41a24348712173f5cec7937` |
| 초기 파일 snapshot digest | `sha256:762136c2a7d27aa864504ad79558241f0429705a18be64f3e12aca72f2d4a0c5` |
| 시나리오 계약 digest | `sha256:59f0a6b8047f862ab432a0b8746e620cc0b9db9de48cc0891d59bd850f338b79` |
| 원문 UTF-8 digest | `sha256:7a2e53cd7c7f0aebbda43ca6048972e122e9e08cab6c9679d26cab5d09e7e1a4` |
| 입력 lock digest | `sha256:367e331f40f1ce38ffdbfaf813d11edb20859396677d34aaef2c2308b177ac42` |

source digest는 기존 `source_manifest_digest`를 사용한다. 새 평가 fixture와 회귀 테스트가 manifest에 포함되므로 S04와 digest가 달라졌다. Engine 구현 자체의 변경은 없다. S04의 단일 Worker 실측은 선행 경계의 provenance로 보존하며 새 source의 전체 실행 증거로 합산하지 않는다.

사용자 원문은 기존 bugfix 사례를 이번 평가 입력으로 구체화한 다음 문자열이다. 사용자가 이 문장을 직접 작성했다고 표시하지 않는다. `contract.json.source_request`와 로컬 `source-request.txt`에 동일한 UTF-8 문자열을 고정했으며 S06에서는 이를 그대로 전달한다.

> app.py의 add(2, 3)이 -1을 반환하여 test_app.py의 기존 합산 테스트가 실패한다. add가 양수·음수·0을 포함한 두 정수의 합을 반환하도록 add 함수 구현만 최소 수정해줘. 공개 함수 add(left: int, right: int) -> int와 위치·키워드 인자 호출 계약을 유지한다. test_app.py와 AGENTS.md는 그대로 보존하고 프로젝트 파일을 추가하거나 삭제하지 않는다. 기존 unittest를 통과해야 한다. Task 검증은 실제 unittest·파일 변경 범위 확인과 실행 역할과 분리된 Validator의 직접 evidence 검토로 수행한다. 모든 Task가 검증된 뒤 같은 workspace에서 기존 unittest와 별도의 동작·공개 계약·파일 보존 검사를 Goal Test로 새로 실행하고, Task evidence와 구분된 근거로 최종 Goal을 판정한다. 외부 서비스 변경·배포·의존성 추가는 범위에 없다.

## Goal 후보와 완료 기준

`expected_goal_proposal`은 기존 `GoalNormalizationProposal` schema로 검증한 **비권위 기대 의미**다. 실제 `GoalContractRevision`·ID·reviewer rating을 만들거나 원장에 등록하지 않았다. S06 실제 Normalizer 결과를 이 후보로 대체하거나 byte 동일성을 요구하지 않는다. 실제 Goal의 의미·coverage를 다음 여섯 조건과 대조한다.

| 조건 | 고정 의미 | 검증 책임 |
|---|---|---|
| AC 1 | 두 정수의 합, 양수·음수·0 포함 | 기존 unittest + 독립 Goal 동작 검사 |
| AC 2 | 공개 이름·annotation·위치/키워드 인자 계약 보존 | 실행 모듈의 signature·호출 검사 |
| AC 3 | add 구현만 최소 변경, 테스트·지침·파일 집합 보존 | 초기 hash/AST·실제 diff + semantic Validator |
| AC 4 | 기존 unittest 실제 통과 | Task의 새 command/test evidence |
| AC 5 | 실행 역할과 분리된 직접 evidence 검토 | Core semantic Task validation |
| AC 6 | 모든 Task 뒤 독립 Goal Test와 최종 판정 | task 없는 새 Goal evidence·binding·State·GoalVerdict |

AC 번호는 이 입력 문서의 순번이며 Core criterion ID가 아니다. Task 개수·분할·DAG·Plan·Task ID는 실제 Planner의 결과에 맡긴다. 변경 범위는 작업 복사본의 `app.py` 안 `add` 구현이며 기능 확장·구조 재설계·UI·비용 benchmark·승격은 비목표다. 외부 효과는 없고 공개 정상 동작을 보존하면서 결함을 수정하는 `minimal_change / preserve_public_contracts`다.

## Task 검사와 독립 Goal 검사

두 명령은 해당 `workspace`의 파일을 직접 읽고 별도 프로세스에서 기존 unittest를 실행한다. `task`는 파일 보존·본문 밖 AST·시그니처·기존 unittest를 검사한다. `goal`은 동일 검사를 새로 실행하면서 고정된 7개 정수 쌍을 위치·키워드 인자로 각각 호출한다. 검증 중 소스가 변하면 실패한다.

```powershell
$s05Root = 'D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\s05-bugfix-trace-20260904'
$s05Oracle = 'D:\codex\flowmarshal\tests\fixtures\engine\bugfix-trace\oracle.py'
$s05Python = 'D:\codex\flowmarshal\.venv\Scripts\python.exe'
& $s05Python -B $s05Oracle --phase task --workspace "$s05Root\workspace"
& $s05Python -B $s05Oracle --phase goal --workspace "$s05Root\workspace"
```

**현재 초기 복사본에서 두 명령의 종료 코드 1은 예상된 결함 재현이다.** 이것을 해결 완료나 Task/Goal PASS로 기록하지 않는다. 정상 합산으로 수정된 뒤에만 각 명령은 0을 반환한다. 공개 계약이나 불변 파일을 바꿔 테스트만 통과시켜도 실패한다. 초기 파일은 Git 메타데이터와 Python bytecode를 제외한 세 파일이며 운영 DB·artifact는 workspace 밖에 둔다.

`oracle.py`는 정답 diff나 `return left + right` 문자열을 요구하지 않는다. add 본문 밖 AST와 실제 반환 동작을 검사한다. Task 단계에서 `return 5`가 기존 unittest를 통과해도 Goal 단계는 거부한다. 최소 변경의 의미 검토와 AC 5·6의 원장·역할 독립성은 별도 검증 책임이다. oracle 출력의 `passed`만으로 Task나 Goal을 완료시키지 않는다.

Goal에서는 `evidence_mode=independent`, 새 command/test/file/diff evidence와 `task_id=null`을 요구한다. Task evidence의 복사·재표시로 통과시키지 않는다. 준비 역할이 명령을 materialize할 때 검증 의미·필수 evidence 종류를 유지하고, Core가 실제 Goal binding과 최신 State를 결속해야 한다.

## 실행 설정

실행별 `roles.json`은 기존 설정을 복사하고 현재 사용자의 Luna 최소 high 지시를 적용했다. 저장소 기본 설정은 변경하지 않았다. 실제 App Server의 정책과 `model/list`를 조회했고 모든 model/effort의 지원을 확인했다. 검증 대상 pipeline의 생성형 모델 호출과 thread 생성은 **0회**다. 입력·oracle의 보조 검토 에이전트는 이 pipeline과 별도로 사용했다. S06 호출 직전에도 정책·inventory freshness를 다시 확인한다.

| 역할 | model / effort |
|---|---|
| Normalizer·Skeleton generator·Plan expander | `gpt-5.6-luna / high` |
| General reviewer | `gpt-5.6-terra / high` |
| Critical reviewer | `gpt-5.6-sol / xhigh` |
| Executor | `gpt-5.6-terra / high` |
| Validator | `gpt-5.6-sol / xhigh` |

- 실제 정책: `:danger-full-access / never`.
- 역할 digest: `sha256:be726ac5b76c4b6e12172a5e6e4060cfc8d1ed832d2e5e31167333ad2d52564e`.
- inventory digest: `sha256:82e6bbcba85b38800c736c9f9809a493fbc9b3270cb53b514721a80839a49f14`.
- model lock: `sha256:5cca3c82ad7d7eb4e61aea0dba9e2101a465860b8abe1ec771538bc7d13762ec`.
- prompt와 output schema는 실제 adapter 코드·지침·schema를 `prompt-schema-lock.json`에 고정했다. 이는 완성된 Worker 전송 Prompt가 아니며 그 Prompt는 S07 ready 시점의 실제 Execution Spec에 결속한다.
- 호출 전 source·역할·inventory·Codex executable을 대조한다. 지원되지 않는 binding을 자동 대체하지 않는다. 실제 Task 배정 이유·fallback envelope는 S06의 Plan과 S07의 최신 binding에 남긴다.

## 검증과 S05 완료 조건

| S05 완료 조건 | 근거 |
|---|---|
| 단일 Goal 후보가 검토 가능하게 고정됨 | 실제 schema로 검증한 기대 proposal, 원문·6 AC·제약·비목표·효과 정책 |
| Task와 Goal 성공 기준을 각각 검사 가능 | 분리 명령·독립 동작 oracle·false-positive 방지 회귀 |
| 현재 제품 지원 범위 안의 작은 사례 | 기존 Python fixture·Core/CLI·실행/검사 역할·독립 Goal 경로 재사용 |

oracle 회귀 7개가 통과했다. 정상 구현 두 종류 허용, 원본 결함 거부, 상수 결과 거부, 공개 계약 변경, 테스트 변조, 파일 추가·삭제와 범위 밖 변경 거부를 확인했다. 검사 도구의 초기 작성 중 발견한 Python future annotation 상속 문제는 `compile(..., dont_inherit=True)`로 수정한 뒤 source를 고정했다. Engine·평가 oracle의 합격선을 변경한 것은 아니다.

전체 **482개 테스트와 결정적 Gate 5/5가 PASS**다. compileall, 전체 unittest, pip check, synthetic lifecycle, legacy freeze 40개 파일 모두 통과했다. 결정적 계약의 source와 입력 lock의 source가 일치하며 별도 프로세스 재조회도 종료 코드 0이다. 원본·초기 snapshot·작업 복사본의 세 파일은 그대로이고 실제 Goal·Plan·Worker 생성은 0건이다.

- 결정적 계약 digest: `sha256:16b0ba6089c378cdf6b687c8b7f76b9bdb96253edbf94f3a0200d1c461ca6db8`.
- 결정적 report digest: `sha256:f04e47bd27e7809e612106d0c33f4f361915f21745153363f98a1eb82213d179`.
- S05 판정: **PASS — 입력 준비의 세 완료 조건 충족**. 실제 역할·전체 Planning·Planner 기반 E2E·비용 Gate 판정은 포함하지 않는다.

검증 명령은 다음과 같으며 tuple 반환을 정확히 분리한다.

```powershell
.venv\Scripts\python.exe -B -c "from pathlib import Path; from flowmarshal.engine.qualification import run_deterministic; destination, report = run_deterministic(root=Path.cwd(), run_root=Path('.flowmarshal-engine-eval/runs/s05-bugfix-trace-20260904/deterministic')); print(report.model_dump_json(indent=2)); raise SystemExit(0 if report.passed else 1)"
.venv\Scripts\python.exe -B .flowmarshal-engine-eval/runs/s05-bugfix-trace-20260904/prepare.py verify
```

## Evidence와 다음 세션 하나

로컬 root: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\s05-bugfix-trace-20260904`.

- `input-lock.json`: source·원문·fixture·oracle·prompt·schema·역할·inventory·실행 파일 결속.
- `initial-snapshot/`, `workspace/`: 초기 세 파일의 별도 복사본. 두 디렉터리와 원본의 byte digest가 일치한다.
- `source-request.txt`, `roles.json`: S06에 넘길 원문과 실제 역할 설정.
- `baseline-observations.json`: 두 명령의 초기 결함 관측·종료 코드·raw stdout/stderr.
- `metadata-observations.json`: 실제 정책·모델 목록의 비실행 조회.
- `prompt-schema-lock.json`, `prepare.py`: 동결 대상 원문과 재현·재조회 방식.
- `deterministic/`: 새 source의 전체 결정적 검사 계약·cell·결과.
- `verification-summary.json`, `verify_evidence.py`: 최종 입력·source·명령·Gate·원본 보존 대조 결과.

**다음 세션은 S06 하나다.** 먼저 `prepare.py verify`로 입력을 대조하고 동일 workspace·별도 Engine DB에서 프로젝트를 등록한다. 기존 CLI의 `goal create --live --request <고정 원문>` → `plan search --live --candidate-count 1`을 사용한다. 필요한 oracle 자료는 프로젝트 밖 reference로 등록하되 기대 Goal proposal을 정규화/Plan 출력으로 주입하지 않는다. 실제 역할 receipt·source/Goal/Plan digest·AC coverage를 같은 원장에 남기고 선택 Plan을 그대로 인계한다.

S06에는 Plan 활성화·Task 상세화·Worker 실행을 포함하지 않는다. 이후 S07에서 실제 선택된 Plan revision과 digest에 대한 활성화 경계를 따른다. 검증 과정에서 Engine 수정이 필요하면 실패 근거를 보존하고 별도 Repair와 새 source 계약으로 분리한다. 이전 `_prepare`가 만든 수동 Plan, `--outcome-file`, 수동 ExecutionSpec으로 실제 Planner 기반 Trace를 대체하지 않는다.

S09는 같은 원장에서 마지막 검증 checkpoint·binding을 먼저 관측하여 재시작·중복 방지를 확인한다. S04에서 인계된 재개 turn 누적 usage의 unavailable 한계는 유지하며 M3 전에 Goal 전체 비용이 완전하다고 주장하지 않는다.
