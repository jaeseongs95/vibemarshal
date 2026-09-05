# R-S06-30 scope→finding link 참조 결속 보정 인계

이번 경계는 모델 제출에서 누락한 scope citation ID의 결속 설명·진단·raw 회귀를 보강한다. 기존 부분집합 수용 조건은 유지한다. R30 실제 결과는 FAIL로 보존하고 **Sol/xhigh 보류, S06·Functional Alpha·1.0 NO-GO**를 유지한다.

## 근거와 구현

첫 조회 전에 현재 turn의 개발자 `<permissions instructions>`에서 `sandbox_mode=danger-full-access`, `approval_policy=never`를 직접 확인했다. 적용되는 프로젝트 AGENTS.md를 확인했으며 대상 경로에 override나 설정된 대체 지침은 없었다. 시작 상태는 main, HEAD=origin/main=원격 main=`e213ee2b89ae4a50b9362701a430e55f29f9d951`, clean이다. 권위 원격 `jaeseongs95/vibemarshal`의 private 상태도 확인했다.

[복구 설계](C:/Users/sjs95/Documents/ChatGPT/자동화%20문제%20원인분석/R-S06-30-참조결속-복구설계.md), [R30 실제 검증](r-s06-30-sol-xhigh-actual-validation-handoff.md), [기존 finding evidence 보정](r-s06-finding-evidence-binding-diagnostics-handoff.md)을 근거로 구현했다.

`VAL_TASK_ORACLE_SCOPE_OVERCLAIM`의 link에서 `scope_task_behavior_values`의 `v_task_behavior_values`와 `scope_task_behavior_calls`의 `v_task_behavior_calls`가 누락됐다. validation은 `val_task_add_behavior_contract`, phase는 task다. R29의 link→finding evidence 누락과 구분되는 scope→link citation ID 결속 오류다.

ValidationScopeInspection.finding_codes·InspectionFindingLink.basis_refs·공유 지침에 같은 code의 각 non-supported scope 전체 basis_refs(자신의 claim_ref 포함)를 해당 link에 포함하도록 명시했다. 복수 scope의 합집합, 복수 code 각각의 결속, 동일 source/selector의 다른 citation ID 대체 금지와 무관한 finding·supported sibling 비강제를 함께 명시했다. contradicted/unresolved의 기존 종류와 supported 빈 finding 규칙을 유지했다.

validator의 기존 부분집합 비교식·adapter 순서·첫 실패 중단은 유지한다. 기존 오류 접두사 뒤에 finding_code, validation_id, scope_id, phase repr, claim_ref, 정렬된 required_citation_ids·actual_citation_ids·missing_citation_ids를 기록한다. 전체 오류 수집이나 참조 자동 보정은 없다. AGENTS.md·권위 설계에도 같은 의미를 동기화했다.

## 원본 회귀와 보존

[신규 R30 fixture](../tests/fixtures/engine/r-s06-30-bad-scope-finding-ref-failure.json)는 전체 raw final_response 문자열과 UTF-8 digest, 원본 terminal/request digest와 재현 문맥을 보존한다. 운영 receipt·credential·provider 식별자는 복제하지 않았다. 원본 root는 `.flowmarshal-engine-eval/runs/r-s06-19-sol-xhigh-r30-20260905-v1`이다.

R30 Goal과 Plan의 네 배열·Project Map entry의 경로 외 내용 및 등록 자료 digest를 대조했다. 원본 Plan에 생략된 supersedes_plan_revision_id는 typed 기본값 null로 복원되어 기존 R29 문맥과 전체 canonical digest가 같다. 기존 helper를 공통화해 재사용하며 R29 fixture bytes는 변경하지 않았다.

- raw final_response UTF-8: `sha256:71a94ec76ceda686e7aca10e4d8f1374a3d896cf714e482be8f3fd572e1414dc`
- 신규 fixture bytes: `sha256:c2be1b55de8ac1824eb6b167a04a2df1f1eb4e064dbd66038132e956d13d6fe6`
- 정규화 Plan canonical: `sha256:18d970e12968ac306a8e871d03a7a88f3034b39cd37710181141cc95cb202978`

테스트의 수정 사본은 메모리에만 존재하며 두 citation 외 finding·scope·AC boolean·evidence는 원문과 같다. 구조 수용을 실제 모델의 의미 평가 PASS로 소급하지 않는다. provider 호출·R30 resume·fresh 13사례·모델 비교는 수행하지 않는다. planner_roles.py·Core/domain/DB·transport 구조와 순서·역할 설정·기존 fixture·oracle/expectation/threshold/taxonomy는 변경하지 않는다.

## 실제 검증

앞 작업에서 실행한 관련 82개 테스트 PASS 결과는 source 추가 변경이 없어 재사용했다. R30 원문 values 누락·calls 누락·두 citation 추가 수용, claim 외 근거·동일 source/selector ID 대체·전체 문장 대체·복수 scope/code·contradicted/unresolved/supported·R29 evidence 경계·schema 왕복·입력 불변 회귀가 그 결과에 포함된다.

일회성 감사 `r-s06-30-scope-finding-audit-20260905-v2/path-normalization-audit.json`은 Windows `\`와 Git `/`를 POSIX 상대 경로 형식으로 정규화해 기존 tracked/evaluation 기준선 24,418개를 대조했다. 보고서 digest는 `sha256:209d5751d9e11dbfec92c36b328ca5a221ee6f23758d9231bfed05eb81a276a4`이며 누락·삭제·예상 밖 추가·예상 밖 변경은 모두 0이다. 허용된 기존 변경 6개와 신규 fixture·인계 문서 2개만 범위에 포함했다. R29 401개와 R30 402개 파일은 bytes가 보존됐다. strict schema의 properties/required 구조·순서는 동일했고, 차이는 새 결속 문구의 `description` 2곳뿐이다.

최종 source에서 미사용 deterministic root를 지정해 Gate를 정확히 1회 실행했다.

```text
D:\\codex\\flowmarshal\\.venv\\Scripts\\python.exe -X utf8 -B -m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\\codex\\flowmarshal --run-root D:\\codex\\flowmarshal\\.flowmarshal-engine-eval\\runs\\r-s06-30-scope-finding-deterministic-20260905-v2
```

Gate report digest는 `sha256:4fb94fa3c337de04fd6ed77fecbf3b582c99f5f9b4b953a88a7a44b5a6fe4f34`이고 `check_count=5`, `failure_count=0`, 종료 코드 0이다. compileall, 전체 unittest(615개), pip check, synthetic lifecycle, legacy freeze(40개)가 모두 PASS했다. `git diff --check`와 `git diff --cached --check`도 종료 코드 0이다.

원본 R30 raw final response UTF-8 digest `sha256:71a94ec76ceda686e7aca10e4d8f1374a3d896cf714e482be8f3fd572e1414dc`, 신규 fixture digest `sha256:c2be1b55de8ac1824eb6b167a04a2df1f1eb4e064dbd66038132e956d13d6fe6`, 정규화 Plan canonical digest `sha256:18d970e12968ac306a8e871d03a7a88f3034b39cd37710181141cc95cb202978`은 유지했다. provider 호출·R30 resume·fresh 13사례·모델 비교·oracle/expectation/threshold/taxonomy 변경은 수행하지 않았다.

이번 경계의 의도된 변경 전체는 하나의 한국어 commit으로 기록해 private `origin/main`에 push했고, 확인 결과 `HEAD=origin/main`이며 worktree는 clean이다. R30 FAIL, Sol/xhigh 보류, S06·Functional Alpha·1.0 **NO-GO** 판정은 그대로다.
