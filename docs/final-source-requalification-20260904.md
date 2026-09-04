# 최종 source 재평가와 실행 준비 계약 복구

- 기준일: 2026-09-04 KST
- 시작 commit: `6031c9e`
- 이전 실행: [A4·A5 실행 기록](alpha-a4-a5-execution.md)
- 실행 root: `.flowmarshal-engine-eval/runs/final-source-20260904`

## 범위와 평가 계약

사용자의 다음 작업 계속 요청에 따라 이전 수정 완료 source에서 실제 역할 회귀, 전체 Planning과 실제 프로젝트 E2E 재평가를 시작했다. 평가 fixture는 별도 복사본을 사용하고 기존 운영 원장과 legacy/prototype은 변경하지 않았다.

수정 전 source는 `sha256:e2787c9c438d2273a4a717023b2385b8fa48a6524591869ec3e46bbe52b91e64`다. 세 실제 모델 scope는 같은 역할 설정과 model lock을 사용했다.

- 역할 설정 digest: `sha256:be726ac5b76c4b6e12172a5e6e4060cfc8d1ed832d2e5e31167333ad2d52564e`
- model lock: `sha256:87d0ec7abb30e101d94b2d96aa67da38f81ae3601506fd94c9a5ebd19e2148f5`
- 역할 설정은 직전 세션의 Luna/high, Terra/high, critical Reviewer·Validator의 Sol/xhigh를 유지했다. 프로젝트 기본 설정과 oracle·threshold는 변경하지 않았다.
- runtime은 기존 고정 Codex 실행 파일을 명시하고 `model/list`로 지원 여부를 확인했다.

기능 scope는 서로 다른 원장·작업 복사본에서 병행했다. 아래 receipt latency 합은 성능 benchmark의 단독 실행 수치가 아니다.

## 수정 전 결과

결정적 Gate는 전체 455개 테스트, compileall, pip check, synthetic lifecycle과 legacy freeze 40개를 통과했다. 보고서는 `deterministic/qualification-report.json`, digest는 `sha256:254fbfd1caf62b7e18dc4db9efcb23d40d2ae8979e5e69ab39b438b9c4d81122`다.

실제 역할 회귀는 **48/48 cell 완료, PASS**다. 보고서는 `role-fixture/qualification-report.json`, digest는 `sha256:8413ac9427621eaeaac568933a6d3799118c0d92e1b836b916914e8a75f7e73c`다.

| 지표 | 결과 |
|---|---:|
| 필수 finding recall | 97.22% |
| finding precision | 100% |
| critical false admission | 0 |
| clean false block | 0 |
| 최종 schema failure | 0 |
| critical seed 불일치 | 0 |

`G07-adversarial/seed-89`의 `GOAL_TRACE_GAP` 누락 1건을 diagnostics에 보존했다. 역할 receipt 48건의 input은 1,150,987, cached input은 436,352, output은 12,184 tokens이며 latency 합은 459,093ms다. cached input은 input에 포함되고 reasoning은 output에 포함된다. schema recovery는 1회이며 최종 schema 실패와 구분한다.

Planning은 seed 17의 **4/18 cell만 완료**했다.

| 시나리오 | 결과 | logical 역할 호출 | 후보 버전 |
|---|---|---:|---:|
| S01 단순 수정 | PASS | 6 | 1 |
| S06 대상 없는 비가역 효과 | 정상 차단, PASS | 2 | 0 |
| S02 읽기 전용 분석 | Plan 선택 실패 | 6 | 1 |
| S03 migration 전략 비교 | PASS | 12 | 4 |

S02의 Goal과 Skeleton은 통과했지만, 상세 Plan이 분석 보고서 생성을 요구하면서 분석 산출물 무변경도 요구했다. Reviewer의 `ENG_READ_ONLY_OUTPUT_CONTRADICTION`을 Core가 `needs_revision`으로 반영했다. 원시 cell은 `full-planning-pipeline/cells/seed-17/bd02b365e1b15e5a38ca84ce.json`이다. 현재 근거는 Plan 표현의 모순을 보여주며, 이를 oracle 결함이나 Core의 잘못된 차단으로 재분류하지 않았다.

E2E 구현 결함이 별도로 확인되어 수정에 앞서 진행 중인 S04 역할 호출을 중단했다. 평가 소유 App Server의 PID·부모·실행 명령을 확인하고 해당 프로세스만 종료했다. parent runner는 실패 receipt와 `FAILED` 상태를 기록했으며 완료 cell 네 개는 그대로 보존했다. 중단 근거는 `planning-interruption.json`, `full-planning-pipeline/last-error.json`에 있다. 미완료 cell과 사용량 없는 최종 호출을 성공이나 0-token 실측으로 집계하지 않는다. Planning 전체 보고서나 PASS는 생성하지 않았다.

## E2E 결함과 수정

첫 E2E는 정상 시나리오의 Task 준비 단계에서 실패해 완료 cell이 0/4였다. 실제 첫 응답은 non-command action에 command를 넣어 schema recovery를 사용했고, 두 번째 응답은 semantic validation에 `artifact_paths`를 넣었다. provider의 축소 schema는 두 번째 응답을 허용했지만 Core 계약의 method를 결합한 최종 `ExecutionSpecProposal` 검증은 이를 거부했다. 이 결합 검사가 runner 밖에서 실행되어 제한된 교정 경로를 이용할 수 없었다.

수정은 `execution.py`와 회귀 테스트에 한정했다.

- Task 준비 응답은 provider schema 검사 뒤 `compile_task_preparation`으로 활성 Task 계약까지 검증한다. 이 검사를 기존 runner validator 안에 넣어 최초 계약 오류도 기존 한 번의 schema recovery 대상으로 처리한다.
- Goal Test 준비의 validation ID·method·필수 evidence 종류 일치도 같은 경로에서 검증한다.
- 저장된 operation 결과를 재사용할 때도 동일 validator를 적용한다. 유효 receipt의 실제 소비 비용은 먼저 보존하고 call ID로 중복 기록을 막는다.
- non-command action의 빈 command와 non-deterministic validation의 빈 artifact 경로를 역할 지침에 명시했다.

잘못된 artifact 경로를 삭제해 성공으로 바꾸지 않고 retry 한도를 늘리지 않았다. 실제 첫·두 번째 오류가 그대로 반복되면 최종 `schema_failed`가 맞다. 원래 실패의 payload·receipt·원장은 보존한다.

추가 회귀 네 개는 실제 `CodexStructuredRoleRunner`에 모의 runtime을 주입해 Task 계약 오류 뒤 정상 교정, 두 번 오류의 제한 실패, 잘못된 저장 응답의 무실행 차단과 비용 중복 방지, Goal 계약 오류의 교정을 검증한다. 이 모의 회귀는 실제 E2E 완료를 대신하지 않는다.

## 수정 후 검증

최종 source는 `sha256:7aabba62ea5d6faa0e3857b8739274a7b519b230fea693750839761756b977ff`다. 전체 **459 tests, OK**, compileall·pip check·synthetic lifecycle·legacy freeze 40개 모두 PASS다. 최종 보고서는 `final-deterministic/qualification-report.json`, report digest는 `sha256:ae03bd3eec5ab08f0939ded78938afaa33f349f60668094d021bf5a4968248c2`이며 현재 source lock과 일치한다.

파일 반영 중 줄바꿈을 정리하기 전 시작된 `fixed-deterministic/` 보고서는 중간 artifact로 보존하고 최종 source 증거로 사용하지 않는다. 실제 새 E2E는 `fixed-project-e2e/`에서 별도 원장과 최종 source 계약으로 수행한다.

**실제 E2E 4/4 cell, PASS**, 중복 효과 0건이다. 보고서는 `fixed-project-e2e/qualification-report.json`, report digest는 `sha256:1534ea39f3dd82e24724c89cc8ef5c01f1705aae428de046aeda728e11c4b620`다.

| 시나리오 | 직접 확인한 결과 |
|---|---|
| 정상 완료 | 자동 실행 준비, validation PASS 3개, 독립 Goal Test evidence 1개, Goal 완료와 유효 History |
| materialize 뒤 입력 변경 | `STALE_EXECUTION_INPUT`으로 차단, thread 생성 0회 |
| 저장 turn 중단·재개 | 재개 전 저장 관측, create 1회·resume 1회·turn start 2회, 독립 Goal Test와 Goal 완료 |
| 생성 receipt 유실 | `EXTERNAL_EFFECT_UNKNOWN`으로 차단, create 1회·turn start 0회 |

정상 시나리오는 실제 준비 역할을 사용하고 나머지 시나리오는 프로그램으로 준비한 계약에서 runtime 경계를 검사한다. 이를 자연어 Goal부터 Plan 선택까지의 전체 성공이나 물리적 PC 종료·실제 quota 소진 검증으로 확대하지 않는다.

수정 전 결정적·역할 PASS와 부분 Planning은 이전 source의 근거다. 수정 후 결과와 합쳐 같은 source의 전체 qualification으로 사용하지 않는다. 네 기능 scope와 별도 36-cell token/latency Gate가 모두 충족되지 않아 **1.0 cutover는 NO-GO**를 유지한다.

## 다음 작업

1. 읽기 전용 Planning에서 프로젝트 원본의 무변경과 새 보고 output의 의미를 구분하고, 원시 finding을 근거로 역할 지침의 필요한 범위만 보완한다. oracle와 합격선은 유지한다.
2. 확정한 최종 source에서 역할 48건과 Planning 18건을 새 계약으로 완주한다. 중단한 S04의 미완료 cell은 재사용하지 않으며 기존 부분 campaign은 provenance로 남긴다.
3. source가 다시 바뀌면 결정적·실제 E2E도 같은 최종 source로 확보한 뒤 네 기능 scope와 별도 36-cell 성능 Gate를 대조한다. 현재는 benchmark나 cutover를 수행하지 않는다.

실제 DB·prompt·receipt·작업 복사본과 세션용 역할 설정은 Git에서 제외한다. 코드·회귀·검증 보고서만 세션 커밋에 포함한다.
