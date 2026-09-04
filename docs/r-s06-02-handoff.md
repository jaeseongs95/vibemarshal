# R-S06-02 인계 — Task별 필수 검증 보완과 실제 진단 차단

## 판정과 다음 작업

- 2026-09-04, [S06 재평가](s06-planning-retry-handoff.md)의 다음 Repair 단위를 수행했다.
- **구현·결정적 검증 PASS**: 최종 source에서 전체 486개 테스트와 결정적 Gate 5/5를 통과했다.
- **실제 역할 진단 BLOCKED**: 새 Codex App Server의 `config/read`가 `:danger-full-access / on-request`를 반환했다. 프로젝트의 `:danger-full-access / never` 요구와 달라 `PERMISSION_POLICY_MISMATCH`로 역할 호출 전에 종료했다. 실제 역할 요청·thread/turn intent·provider turn은 모두 0건이다.
- 따라서 Task별 검증 누락이 실제 모델에서 해소됐다는 판정은 아직 없다. 기존 S06은 선택 Plan 없는 FAIL로 보존하며 자연어 → Goal 완료 Trace는 `INCOMPLETE`, 1.0은 `NO-GO`다.
- **다음 작업은 R-S06-02의 제한된 실제 역할 진단 재개**다. 요구 정책이 실제 적용되는 새 실행 환경에서 진단을 마친 뒤 새 S06을 수행한다. 실제 선택 Plan 전에는 S07로 진행하지 않는다.

## 반영한 변경

[planner_roles.py](../src/flowmarshal/engine/planner_roles.py)의 공통 Planning 안내와 provider 필드 설명을 보완했다.

- AC의 `contributes_to`는 산출물·근거의 기여 관계이고, Goal이 각 Task 또는 특정 Task의 완료 전에 요구한 검증은 해당 Task 자체의 책임이다.
- 상세화는 적용 대상 `Task.validations`에 검사 목적·method·필수 evidence 종류를 보존한다. 후속 검증 Task, 완료 조건 문장, `independence_required` 모델 배정 또는 독립 Goal Test만으로 자체 검증을 대신하지 않는다.
- `detail_requirements`와 Task의 AC 연결에 같은 요구를 반복하지 않아도 Goal의 명시적 적용 범위는 유지한다. 필요한 Task의 검사 ID를 Goal coverage에 연결한다.
- 모든 Task 뒤의 독립 Goal Test는 `integration_validations`에 둔다. 정상적인 후속 검증 Task를 허용하며, 특정 Task에만 요구한 검사 종류를 모든 Task의 규칙으로 확대하지 않는다.

Generator·Skeleton reviewer·refiner·expander·Plan reviewer가 같은 책임 경계를 공유한다. provider schema의 필드 설명만 바꿨으며 권위 schema·Core 상태 전이·DB·모델 배정·fallback·재시도 한도·공식 fixture·oracle·합격선은 변경하지 않았다. 장기 원칙은 [AGENTS.md](../AGENTS.md), [권위 설계](orchestration-redesign.md)와 작업 시작 프로젝트의 `D:\codex\자동화템플릿\AGENTS.md`에 반영했다.

## 회귀와 결정적 검증

[역할 adapter 테스트](../tests/test_engine_role_adapters.py)에 두 회귀를 추가했다.

1. 실제 실패처럼 두 Skeleton Task의 `detail_requirements`가 비어 있고 변경 Task의 AC 기여에는 Task별 검증 AC가 없어도 Goal의 요구가 상세화 입력으로 전달된다. 자체 `command/test/file/diff`·semantic `model_review`와 coverage 검사 ID를 보존한 Plan은 정상 처리하고, `file/diff`만 있는 변경 Task의 유효한 Reviewer finding은 `needs_revision`으로 유지한다. 후속 검사·독립 모델 배정·Goal Test가 있어도 같은 경계를 검사한다.
2. Goal이 변경 Task에만 요구한 semantic 검증은 무관한 후속 Task에 일괄 강제되지 않는다.

이 테스트는 scripted 역할 결과의 전달·계약 결합·Core 판정을 검사한다. 실제 모델의 생성 품질이나 누락 검출률을 검증한 결과는 아니다.

| 최종 검사 | 결과 |
|---|---|
| 전체 unittest | 486개 PASS |
| compileall | PASS |
| pip check | PASS |
| synthetic lifecycle | PASS |
| legacy freeze | 40개 파일 PASS |

독립 구현 검토에서 expander 문장의 적용 대상이 모호한 부분을 분리하고, paired 회귀의 변경 Task 검사 ID 연결 assertion을 보강했다. 그때마다 source가 달라진 결정적 보고서를 별도로 보존했으며 최종 증거는 `deterministic-final`이다. 이전 source의 PASS를 최종 source의 증거로 재사용하지 않는다.

## 실제 진단의 차단 근거

과거 S06의 Goal·State·Project Map·Skeleton·불량 Plan을 입력으로 보존하고, 정상 Skeleton 검토 → 상세화 → 새 Plan 검토 → 기존 불량 Plan 거부의 4회 진단을 준비했다. 새 역할 cwd에는 S05 작업 복사본과 같은 byte의 별도 복사본을 만들었다. 과거 입력의 경로는 바꾸지 않았으며 이 복사본을 OS 격리로 표현하지 않는다.

정책 검사에서 중단된 뒤 metadata 전용 runtime으로 새 복사본·기존 S05 workspace·프로젝트 루트를 확인했다. 세 경로 모두 `default_permissions=:danger-full-access`, `approval_policy=on-request`였고 전체 권한 profile 자체는 허용돼 있었다. 현재 대화의 정책과 별도 App Server가 읽은 실행 설정을 혼동하지 않았다. 역할 thread가 없으므로 실제 자식 turn 정책을 관측했다고 주장하지 않는다.

정책 설정이나 검사기를 바꾸지 않았고, 승인 요청·모델 변경·우회 호출은 하지 않았다. live preflight lock과 역할 receipt가 없으므로 사용량·지연·성공률을 산출하지 않는다. 이 진단의 모델 호출 token은 0이며 메인·서브에이전트 비용은 이 값에 포함되지 않는다.

기존 S06 evidence manifest의 92개 파일과 원장 byte를 대조했고 S05 workspace 및 진단 복사본은 동일한 byte를 유지한다. 과거 원장은 직접 열지 않고 WAL이 비어 있음을 확인한 뒤 byte 복사본만 `mode=ro&immutable=1`로 조회했다. 원장의 기존 활성화·Execution Spec·Attempt·runtime intent/receipt·validation·GoalVerdict는 0건이다.

## 최종 lock과 로컬 evidence

| 항목 | 값 |
|---|---|
| 시작 HEAD | `b9dd30c` |
| 최종 source digest | `sha256:df2774e70648c40cef7bfd0ce3cbfbd31d6dea32f698d557f2b41251cf5e573a` |
| 최종 결정적 계약 | `sha256:463a26be5249b883e77e3bca4e63bf4c41605c099cfdc2023541c05298b2ee4c` |
| 최종 결정적 report | `sha256:a20a9a90e87e176977c7b2b1ec75b82c68f3ad8959184785a0491261dcd550d5` |
| 실제 읽은 config digest | `sha256:6ea827de16e0a0e8bcc88b6a17d1037f692be7171d0cb716416b719293149a41` |

실제 시작 HEAD는 로컬 `session-verification.json`과 git 이력으로 대조한다. 실행 evidence는 `.flowmarshal-engine-eval/runs/r-s06-02-20260904`에 보존하며 Git에 포함하지 않는다.

- `probe.py`, `input-*.json`, `input-ledger-snapshot.sqlite3`, `workspace/`: 중단된 진단 코드와 과거 입력 복사본. 이 디렉터리를 재실행하거나 기존 실패 기록을 덮어쓰지 않는다.
- `collect_blocked.py`, `blocked-observation.json`: 최종 source의 metadata 전용 정책 재관측, 실제 역할 효과 0건, 기존 evidence 보존과 최종 Gate 확인.
- `deterministic/`, `deterministic-v2/`, `deterministic-final/`: 각 source의 독립 결정적 결과. 마지막 디렉터리만 현재 source에 해당한다.
- `verify_blocked.py`, `session-verification.json`: 모델 재호출 없는 저장 evidence 재검증.
- 별도 `.flowmarshal-engine-eval/runs/r-s06-02-20260904-v2`의 `probe.py`, `verify.py`, `preparation.json`: 다음 진단용 준비 파일이며 **미실행**이다. 현재 source가 같을 때만 위 최종 결정적 보고서를 참조한다.

저장 evidence 재검증:

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-02-20260904/verify_blocked.py
```

요구 정책이 실제 적용된 새 실행 환경에서, source digest를 재확인한 뒤 다음 진단을 수행한다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-02-20260904-v2/probe.py
```

정책 불일치가 남으면 그 새 실행도 역할 호출 전에 중단한다. 진단 통과 후에는 기존 draft를 수동 수정·활성화하지 않고 고정 원문부터 새 Goal과 독립 검토, Skeleton, 실제 선택 Plan을 얻는 새 S06으로 이어간다.
