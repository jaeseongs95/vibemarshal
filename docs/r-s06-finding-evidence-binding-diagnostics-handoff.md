# Finding evidence 참조 결속 설명·진단·원본 회귀 인계

R-S06-29 bad의 직접 원인은 모델 제출 오류다. `VALIDATION_SCOPE_TASK_ORACLE_OVERCLAIM`의 link는 Goal 인용을 사용했지만 finding evidence에서 `source:goal`을 누락했다. 이번 변경은 현재 수용 불변식을 유지하는 필드 설명·제출 전 대조·오류 진단·회귀 보강이다. **Sol/xhigh는 보류**이며 실제 역할 적격성·S06 PASS·1.0 GO로 확대하지 않는다.

## 기준과 근거

첫 조회 전에 실제 개발자 `<permissions instructions>`의 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인했다. 적용 지침과 대체 지침 설정을 확인했으며 대상 경로에 추가 override는 없었다. 시작은 `main`, `HEAD=origin/main=원격 main=1478e9854f6d5fd9395ef7f60bee5006efa5ba94`, clean이었다. 권위 원격 `jaeseongs95/flowmarshal`의 private 상태도 확인했다.

[R29 실제 검증 인계](r-s06-29-sol-xhigh-actual-validation-handoff.md)와 분석 작업 `01a0713d-90bd-7461-9a97-3d976a8eca9a`의 완료 원문을 대조했다. 앱 조회가 완료 turn의 본문을 반환하지 않아 해당 작업의 로컬 원본 이력에서 최종 근거를 읽었다.

- 해당 link의 basis_refs 10개가 요구하는 집합: `artifact:plan_contract`, `source:goal`, `source:project_map`.
- 실제 finding evidence: `artifact:plan_contract`, `source:project_map`.
- 누락 ref: `source:goal`. 대응 citation: `c_ac001_intent`, `c_ac001_statement`, `c_ac002_intent`, `c_ac002_statement`.
- bad expectation의 `required_evidence_refs`는 결함 탐지 최소 집합이므로 수정하지 않았다. bad 의미 assessment는 여전히 미평가이며 모델이 누락한 내부 이유는 추정하지 않는다.

## 변경 경계

`InspectionFindingLink.basis_refs`, `PlanReviewDraft.findings`, 공유 지침과 권위 문서에 다음 관계를 명시했다.

```text
해당 finding link가 실제 인용한 원본 evidence ref 환산 집합
    ⊆ 같은 finding_code의 finding.evidence_refs
    ⊆ 실제 evidence_catalog key 집합
```

`source:goal`·Reviewer의 `artifact:plan_contract`는 그대로, 검증된 `project:<entry_id>` citation은 `source:project_map`으로 환산한다. Goal 인용이 없는 link에는 Goal ref를 강제하지 않고 유효한 추가 catalog ref를 허용한다. citation ID·project ref·`source:plan`은 직접 evidence ref로 허용하지 않는다.

기존 `cited_sources <= set(finding.evidence_refs)` 비교식과 catalog membership 검사를 유지했다. 오류 접두사 `대조표 finding evidence ref 불일치` 뒤에 finding_code, 정렬된 required/actual/missing evidence refs와 missing citation IDs를 기록한다. adapter의 자동 evidence 추가·인용 삭제·finding 생성·의미 판정·silent fallback은 없다. strict schema의 설명 외 구조·properties/required 순서와 finding/rating 배타 조건도 유지한다.

신규 [R29 원본 fixture](../tests/fixtures/engine/r-s06-29-bad-finding-evidence-ref-failure.json)는 전체 final_response 문자열·digest·출처·기대 진단을 고정했다. 기존 bad Plan fixture의 네 배열을 원본 문맥으로 투영하고 기존 reference/test_app fixture의 동일 bytes를 재사용한다. provider thread/turn/run ID·credential·운영 receipt를 fixture에 넣지 않았다. `source:goal` 추가 사본은 테스트 메모리에만 존재하며 모든 인용·finding·scope·AC boolean은 원본과 같음을 확인한다.

Goal/Plan 의미·기존 fixture·evaluator·oracle·threshold·taxonomy·제품 역할 설정은 변경하지 않았다. 기존 run/raw/frozen artifact를 수정하거나 provider를 호출·resume·재호출하지 않았다. 새 역할 작업·fallback/recovery·callback·예약을 생성하지 않았다.

## 실제 검증

관련 테스트는 다음 명령에 첫 실패 중단 `-f`를 적용하여 **78개 PASS**였다.

```powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest -f tests.test_engine_plan_inspection tests.test_engine_inspection_raw_regressions tests.test_engine_inspection_source_contract tests.test_engine_inspection_case_binding tests.test_engine_role_adapters tests.test_engine_roles
```

Goal 미인용 수용·추가 인용 후 누락 거부·완전 제출 수용·Plan/Project Map 누락 거부·잘못된 직접 ref 3종 거부·정상 catalog 추가 ref 수용을 확인했다. 합성 runtime을 통한 `schema_failed` receipt의 정확한 진단, 단일 turn·recovery 0과 입력 보존을 검사했다. R29 bad 원문은 같은 불변식으로 계속 거부되고 정렬 순서를 역전한 사본도 동일 진단을 낸다. R29 clean 원문은 별도 읽기 검증으로 구조 통과했다. 기존 scope·AC 결속 및 strict schema 회귀도 통과했다.

추가 감사 스크립트의 첫 clean 재생 시 축약된 catalog Project Map을 전체 revision 타입으로 읽은 입력 구성 오류가 있었다. 제품·fixture를 변경하지 않고 원본 revision 문맥으로 재구성해 digest를 대조한 뒤 완료했다. 관련 테스트나 최종 Gate 실패를 재시도로 숨긴 것이 아니다.

미사용 `deterministic` root에서 fixture LF 정규화와 digest 갱신을 반영한 최종 source 상태의 Gate를 **정확히 한 번 실행하여 5/5 PASS**했다. CLI 종료 코드 0이며 qualification report의 `status=COMPLETED`, `failure_count=0`이다.

| Gate | 실제 결과 |
|---|---|
| compileall | PASS, exit 0 |
| 전체 unittest | PASS, Ran 611 tests in 66.888s / OK |
| pip check | PASS, No broken requirements found. |
| synthetic lifecycle | PASS, history_valid=true, run_state=completed |
| legacy freeze | PASS, 40파일, changed/missing/unexpected 모두 빈 배열 |

```powershell
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\codex\flowmarshal --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-30-finding-evidence-development-20260905-v2\deterministic
```

`PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`을 사용했다. qualification report bytes digest는 `sha256:9cf2eccbfd1fb96a4a2c5cf31636f1c0d45b4c1a18d60532bda01eaea1b3ff72`이고 report의 contract digest는 `sha256:5c331d02938ee2b8fdedf2d99c89f6161cc24cadfaa45b8f5b7faeee5d3b81fa`다. 5개 완료 cell과 stdout/stderr, source manifest와 계약 digest를 대조했다.

이전 전달에서 발생한 신규 fixture CRLF의 `git diff --cached --check` 실패는 포장 JSON만 LF로 정규화하고 bytes digest를 갱신해 해결했다. 내부 `raw_final_response` 문자열과 UTF-8 digest, 인용·판단·기존 raw/frozen bytes는 보존했으며, 최종 source 상태에서 새 run root의 Gate를 단일 실행했다.

LF 정규화 후 fixture bytes digest와 테스트 상수를 `sha256:d05c0acfb66d4ce0664e2d3847fdd7276419e19807013d8aadd418f3dd7d66b6`으로 고정했다. 좁은 관련 테스트 78개, 최종 결정적 Gate 5/5, staged diff 검사를 모두 통과한 뒤 이 9파일 변경을 하나의 한국어 commit으로 기록해 private `origin/main`에 push했다. R30 제한 실제 검증은 이번 경계에서 실행하지 않았다.

## 보존과 digest

기존 tracked 파일과 기존 evaluation root 파일 총 23,295개의 bytes SHA-256을 수정 전에 기록했다. 허용된 기존 7파일을 제외한 **23,288파일이 bytes 동일**하고 누락은 없었다. R29 기존 run/raw 및 이전 frozen/legacy bytes가 보존됐으며 최종 Gate의 source manifest 235파일은 `sha256:04761bd2da94f5f13e89349b9bdb78817aafafe2a5aa5ab1670b8dd39328e6aa`로 기록됐다. 최종 대조 결과와 source/schema/instruction·fixture 결속은 로컬 감사 root `.flowmarshal-engine-eval/runs/r-s06-30-finding-evidence-development-20260905-v2`에 기록한다. 결정적 Gate는 그 아래 새 `deterministic` root에서 한 번 수행했으며 과거 Gate·완료 cell은 재사용하지 않았다.

| 항목 | SHA-256 |
|---|---|
| source_manifest_digest | `sha256:04761bd2da94f5f13e89349b9bdb78817aafafe2a5aa5ab1670b8dd39328e6aa` |
| schema_before_digest | `sha256:4c2ecf6f2fe1e7c7baba34aa47a875fc605e681f7edc69afccced52afdabef84` |
| schema_after_digest | `sha256:a6bb90547ea6732c2db8bce031c17122ea096156316f0c87a29b569fec1a9f1b` |
| shared_instructions_digest | `sha256:8029f7a4299b2dea4db4da30e7c7c162bb00b6076a27a72a8663339a4f1e8dfd` |
| inspection_instructions_digest | `sha256:98a0ec7445f7ebe69c443262ecdbf9f4e4a1dc327de2df5337dc69a63239fad2` |
| deterministic_contract_digest | `sha256:5c331d02938ee2b8fdedf2d99c89f6161cc24cadfaa45b8f5b7faeee5d3b81fa` |
| fixture_digest | `sha256:d05c0acfb66d4ce0664e2d3847fdd7276419e19807013d8aadd418f3dd7d66b6` |
| R29 raw final_response UTF-8 | `sha256:a5a3bef5d0dfb280498e435db2dd6c33b34332dadebe56fec4cac1da7b87f883` |
| R29 bad 원본 terminal bytes | `sha256:62f4f8d1fe4f1f2607b91722b08b3cef9d61108bf2dd3ae738fc4b21ce995620` |

## 다음 fresh 제한 검증 경계

다음 배정은 **R-S06-30 제한 실제 검증**이다. 이번 개발 작업에서는 실행하지 않았다. 같은 `gpt-5.6-sol/xhigh`, 빈 fallback과 절대 역할 설정 `D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-xhigh-roles.json`을 사용한다. fresh 실제 App Server inventory에서 지원 조합을 확인하고 새 immutable contract에 source·prompt·strict schema·자동 주입 지침·역할 bytes/canonical/typed digest·입력·고정 기대표·독립 review·환경을 결속한다. 기존 R29 run을 resume하거나 호출 결과를 성공 evidence로 재사용하지 않는다.

고정 순서는 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`다. logical/provider/recovery 상한 **13/13/0**, 사례당 한 번, 첫 실패 즉시 중단과 이후 NOT_RUN을 유지한다. receipt·완료 terminal·공통/output 결속·구조/참조·고정 의미 assessment를 모두 통과해야 다음 사례에 들어간다. expansion 뒤 새 생성 Plan의 독립 검토와 전용 기대표 결속이 통과해야 expanded-review로 진행한다.

이번 변경 9파일은 관련 검증과 staged `git diff --cached --check` 통과 후 하나의 한국어 commit으로 기록해 private `origin/main`에 push했다. push 뒤 `HEAD=origin/main`과 clean 작업 트리를 확인했다. R30 제한 실제 검증은 미실행이며 이 인계의 다음 경계로 남긴다. **추가 사용자 판단은 필요하지 않다.**
