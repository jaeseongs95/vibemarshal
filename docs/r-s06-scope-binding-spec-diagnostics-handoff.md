# R-S06 참조 결속 규격 설명·진단 보정 인계

R27에서 드러난 Reviewer 제출 규격의 설명 부족과 누락 위치 진단을 보정했다. 기존 참조 결속 조건과 Goal/Plan 의미는 유지한다. 관련 테스트 63개와 변경된 source에 대한 fresh 결정론 Gate 5/5가 통과했다. 실제 provider 역할 검증은 수행하지 않았으며 R27 FAIL·후속 12사례 NOT_RUN·FlowMarshal 1.0 NO-GO는 유지한다.

## 기준과 원인

시작 시 `main=origin/main=a0624fdeeb69a6f3b6632031e6ae8332be6e6ee7`과 clean을 직접 확인했다. 현재 turn의 개발자 정책은 `sandbox_mode=danger-full-access`, `approval_policy=never`였으며 첫 파일 조회 전에 확인했다.

[R27 원본 인계](r-s06-27-sol-high-limited-validation-handoff.md)와 원인분석 최종 근거를 대조했다. 첫 clean은 `val_goal_independent_behavior_contract`의 goal mechanism 근거 `[p_goal,p_contract]`를 `scope_v5_phase`의 `[v5_phase,p_goal]`에 완전히 반복하지 않아 거부됐다. `p_contract`는 다른 scope에는 존재하므로 공개 계약의 응답 전체 누락이 아니다. 모델의 제출 오류와 provider 규격 설명 부족이 결합한 문제다.

같은 validation·같은 phase의 mechanism 하나에 대한 `mechanism.basis_refs ⊆ scope.basis_refs`는 [R19에서 유지한 계약](r-s06-19-close-handoff.md)이다. 교집합 검사로 완화하거나 여러 mechanism 전체의 합집합으로 강화하지 않았다.

## 변경 파일과 동작

| 파일 | 변경 |
|---|---|
| [plan_inspection.py](../src/flowmarshal/engine/plan_inspection.py) | Field description·공유 참조 규칙, 식별자·실제 누락 ref를 포함하는 결정론적 진단 |
| [planner_roles.py](../src/flowmarshal/engine/planner_roles.py) | Reviewer/Expander 공통 추적 지침 정합화, Goal에 명시된 독립 검사 의무 보존 |
| [test_engine_plan_inspection.py](../tests/test_engine_plan_inspection.py) | 다중 mechanism·scope/AC 참조 거부, 두 역할 지침 전달, strict schema 저장 왕복 회귀 |
| [test_engine_inspection_raw_regressions.py](../tests/test_engine_inspection_raw_regressions.py) | R27 원본 거부와 참조만 완성한 메모리 사본의 구조·boolean 회귀 |
| [R27 원본 실패 fixture](../tests/fixtures/engine/r-s06-27-clean-scope-binding-failure.json) | 원본 응답 문자열·출처 digest·고정 boolean 28행, 기존 입력 재사용에 필요한 네 Plan 배열 |
| [권위 설계](orchestration-redesign.md), [AGENTS.md](../AGENTS.md) | 장기 provider 추적 규칙과 부분 주장 의미 판단의 구분 |
| 본 문서, [문서 인덱스](README.md) | 구현 근거·실제 검증·다음 제한 검증 조건 |

공유 규칙은 다음과 같다.

1. 각 scope는 같은 validation·같은 phase의 mechanism 하나의 전체 `basis_refs`와 자신의 `claim_ref`를 포함한다.
2. 모든 AC 행은 true/false와 무관하게 해당 validation의 mechanism 및 모든 scope에서 실제 범위 판단에 사용한 project citation을 포함한다.
3. true 행은 선택한 supported scope 각각의 claim·전체 근거도 포함한다. false 행은 `scope_ids=[]`를 유지한다.

근거 반복은 추적 결속이며 다른 부분의 검사 능력이나 판정을 scope에 부여하지 않는다. supported·contradicted 부분 scope의 공존과 AC 연결의 독립 판단을 보존한다. sibling 비전염 규칙은 Goal에 명시된 task/goal 독립 검사 의무를 없애지 않는다. adapter에는 의미 정답 계산·참조 채우기·인용 교정·boolean 보정을 추가하지 않았다. 공통 참조 규칙은 실제 두 역할의 요청에 각각 한 번 전달된다.

scope 결속 오류는 validation ID·scope ID·phase와 mechanism 후보별 원래 순번·phase·정렬된 누락 ref를 제공한다. AC 참조 오류는 validation ID·AC ID, 선택 scope ID와 정렬된 누락 ref를 제공한다. 여러 후보의 누락을 합쳐 모든 근거가 필요하다고 오인시키지 않는다. 기존 오류 문구 접두사와 거부 조건을 보존한다.

## 회귀 결과와 원본 보존

| 입력 | 실제 관측 |
|---|---|
| R27 원본 | `scope_v5_phase`, goal mechanism 후보 0에서 `p_contract` 누락으로 거부 |
| 위 scope에 `p_contract`만 추가한 메모리 사본 | `ac_001 × val_task_add_behavior_contract`, `scope_v1_phase`의 `v1_phase` 누락으로 거부 |
| 필요한 참조를 모두 완성한 메모리 사본 | 구조 수용, 원본 boolean 28개와 그 밖의 모든 필드 유지 |
| 위 사본과 원래 고정 기대표 비교 | 27/28 일치. `ac_004 × val_goal_independent_unittest`는 기대 true·원본 false 유지 |

원본의 selected scope claim/basis 누락은 두 behavior validation에 대한 AC 4개씩 총 8행이다. 사본은 테스트 메모리 안에서 참조만 추가한다. 원본 문자열은 그대로 저장하고 digest를 테스트에서 잠갔다. fixture에는 provider thread/turn·run 식별자, credential, 운영 receipt를 넣지 않았다. 입력은 기존 고정 회귀 자료를 재사용하며 R27 원본 Plan과 Goal definition의 canonical digest를 직접 대조한다.

R27 원래 run의 의미 assessment는 여전히 NOT_RUN이다. 위 boolean 대조는 오프라인 회귀이며 원래 성공 result나 qualification 결과를 만들지 않는다. 기존 raw/frozen fixture는 수정하지 않았고 R27 기존 파일 373개의 bytes가 보존됐다.

## 실제 검증

관련 테스트는 `-f`로 첫 실패에서 중단하며 실행했다.

```powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest -f tests.test_engine_plan_inspection tests.test_engine_inspection_raw_regressions tests.test_engine_roles tests.test_engine_evaluation_bindings tests.test_engine_inspection_source_contract tests.test_engine_inspection_case_binding
```

최종 결과는 **63 tests OK, 2.498초, exit 0**이다. 사전 명령에서 존재하지 않는 `test_engine_planner_roles`를 지정한 오류는 명령을 수정해 관련 32개 통과를 확인했다. 구현 후 첫 관련 실패 두 건은 새 테스트의 `observed`/`actual` 필드명 오기와 진단 문맥을 기존 `인용 참조 중복` 접두사 사이에 삽입한 호환성 문제였다. 각각 실제 evaluator 필드명과 기존 오류 접두사 보존으로 해결한 뒤 관련 테스트가 통과했다. 평가 기대 boolean이나 기존 거부 조건은 바꾸지 않았다.

fresh 결정론 Gate는 기존 root를 재사용하지 않고 아래 명령으로 한 번 실행했다. 자식 프로세스에도 `PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`을 적용했다.

```powershell
.venv/Scripts/python.exe -X utf8 -B -m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:/codex/flowmarshal --run-root D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-scope-binding-dev-20260905-v1
```

| Gate | 실제 결과 |
|---|---|
| compileall | PASS, exit 0 |
| 전체 unittest | 604 tests OK, 65.898초, exit 0 |
| pip check | No broken requirements found, exit 0 |
| synthetic lifecycle | PASS, completed·history_valid=true, exit 0 |
| legacy freeze | 40파일 PASS, changed/missing/unexpected 모두 빈 배열 |

결정론 Gate **5/5 PASS**, 실패 0, runner exit 0이다. 결과는 `.flowmarshal-engine-eval/runs/r-s06-scope-binding-dev-20260905-v1/`에, source/schema 대조와 R27 보존 검사는 형제 `r-s06-scope-binding-dev-20260905-v1-audit/`에 남겼다. 운영 root는 commit 대상이 아니다.

| 결속 | SHA-256 |
|---|---|
| source manifest, 233파일 | `sha256:2061d2578d735c819726aa719cfc9a6176a3b11f60b72210cc0f85f377e89655` |
| 결정론 Gate contract | `sha256:1c283ee18794c87d469c94df4d594052eefd5582f57d3a2f3f99080e39b12ecd` |
| 결정론 Gate report | `sha256:84c5bc6564e5cd4977aa13240b4284851ddb27f821a093dd167ad4e1f69ea0ca` |
| 신규 fixture bytes | `sha256:5179b61e2ed1765d21f84e85e4f5954941b1a714c42cb04a4a82a406a41b1c73` |
| R27 원본 응답 UTF-8 bytes | `sha256:fd2f7c9eac0292bdd50548f31a8315b4107a22b6698ccf535a560a02ad55426c` |
| PlanInspection strict schema | `sha256:ab2acbbb40bd9343adde9d12c3718ac70f2f92b939e0535ee3e57c4b3b3ab82c` |
| PLAN_INSPECTION_INSTRUCTIONS UTF-8 bytes | `sha256:3644454baf381f0d8f8dfb6fca5694109b84312e3a8c4c78cd89df82b720bf91` |
| PLAN_VALIDATION_TRACE_INSTRUCTIONS UTF-8 bytes | `sha256:f0190b8aae562ca5b68e35ef0ff39bccc5687488faffb989888d329aa870110f` |

기준 commit과 비교하여 strict schema는 description을 제외한 구조가 동일하고, schema·두 공유 지침의 digest는 실제로 변경됐다. Reviewer와 Expander의 실제 요청을 fake capture로 받아 strict schema를 canonical JSON 파일로 저장·재로드한 뒤 전체 property/required 순서와 request/schema digest 보존을 검사했다. 다중 mechanism 중 한 후보에 완전 결속하는 정상 사례, supported/contradicted 공존, 잘못된 phase·validation·citation, claim/AC 참조 누락, 중복·잘못된 quote/digest 거부 회귀가 통과했다.

## 전달과 다음 제한 검증 조건

관련 테스트와 결정론 Gate, source/schema/instruction·fixture digest, 문서 링크와 `git diff --check`를 확인한 후 이 작업의 9파일만 한국어 commit으로 승인된 private `origin/main`에 push한다. 최종 commit·push와 HEAD/origin/main 일치·clean은 자기참조를 피하여 최종 응답과 로컬 delivery 감사에 기록한다.

다음 fresh R-S06 제한 검증은 새 source·prompt·strict schema·자동 주입 지침·역할 설정·입력별 기대표를 사용되지 않은 immutable contract에 결속해야 한다. general Reviewer `gpt-5.6-sol/high` 후보, oracle·expectation·threshold·taxonomy·Goal/Plan 의미는 유지한다. 지침의 경로·본문 digest, 역할 설정과 실제 runtime 지원·receipt를 호출 전 다시 결속한다.

기존 사례 순서와 logical/provider/recovery 상한 **13/13/0**을 유지한다. 첫 clean은 성공 receipt·완료 terminal·참조 검사·고정 의미 평가와 AC **28/28 일치**를 모두 만족해야 다음 사례로 진행한다. 이후에도 매 사례의 구조·의미·결속 통과가 필요하며 생성 Plan은 독립 검토와 전용 기대표 결속 뒤 마지막 Reviewer에 전달한다. 첫 실패 또는 불명확한 provider 효과에서 즉시 중단하고 동일 계약 재호출·과거 R27 resume·자동 fallback은 하지 않는다.

이번 작업에서는 실제 provider 역할 호출이나 후속 작업 생성·예약을 수행하지 않았다. 결정론 Gate 통과를 실제 역할 적격성, 전체 S06·planning pipeline·실제 프로젝트 E2E·token/latency qualification PASS로 확대하지 않는다. 이번 기술 보정과 전달에는 추가 사용자 판단이 필요하지 않다.
