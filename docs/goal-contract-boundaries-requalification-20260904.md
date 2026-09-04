# Goal의 단계·동작·효과 계약 재검증

- 기준일: 2026-09-04 KST
- 시작 commit: `1d9445c`
- 이전 근거: [읽기 전용 보고 계약 검증](readonly-report-requalification-20260904.md)
- 실행 root: `.flowmarshal-engine-eval/runs/goal-contract-boundaries-20260904`

최종 판정은 **NO-GO**다. 후속 확정 source에서 전체 464개 테스트와 결정적 Gate, 실제 역할 회귀 48/48, 실제 E2E 4/4는 통과했다. 전체 Planning은 18건 수집을 완료해 **13건 통과·5건 실패**이며 별도 token/latency Gate는 실행하지 않았다.

## 확인한 원인과 변경

이전 S02 seed 89의 사용자 원문은 파일 수정을 금지했다. fixture의 `AGENTS.md`는 명령 금지가 현재 계획 생성 호출에만 적용된다고 명시했으나, 정규화 후보는 미래 Goal에도 테스트·빌드·기타 명령 실행을 금지했다. 독립 Goal Reviewer는 이를 통과시켰고, 상세 Plan Reviewer가 명령 미실행의 직접 evidence 부족을 지적했다. 따라서 후자의 검사를 완화할 문제가 아니라 앞선 정규화와 검토의 적용 범위 해석 문제다.

Goal normalizer와 독립 Reviewer에 같은 해석 기준을 제공했다. 현재 역할에만 적용되는 행동 제한을 미래 Goal·Task의 제약으로 전사하지 않는다. 사용자 원문이나 실제 실행 단계 정책이 명령을 금지하면 그대로 보존한다. 정적 분석에 명령 실행이 필요하지 않다는 판단으로 명령 미실행을 입증할 새 완료 조건을 만들지 않는다.

S03의 공개 API 보존 요구는 이름·import·시그니처만 남고 관찰 자료의 합산 반환 계약이 빠졌다. 관련 문서·테스트의 동작 계약을 구체적인 Hard AC나 제약에 보존하도록 보완했다. 비교·제안 요청을 구현 수정이나 migration 실행으로 바꾸지 않는 조건도 함께 유지했다.

S01의 로컬 수정·검증 명령이 허용 외부 효과로 분류된 문제와 S02의 로컬·외부 금지 범위 혼합을 보완했다. 외부 효과는 외부 시스템·계정·제3자에 대한 효과다. Task·Plan의 기대 효과에는 실제 발생시키는 효과만 기록하고, 파일 무변경·외부 효과 없음은 금지 효과나 완료 조건으로 표현하도록 provider 필드 설명과 지침을 정리했다.

권위 문서와 프로젝트 지침에 같은 원칙을 반영했다. 자연어 문자열을 키워드로 판정하는 결정적 분류기는 추가하지 않았으며, 기존 Core 판정과 독립 Reviewer의 증거 참조 검사·oracle·합격선·재시도 한도는 유지했다. 문구 포함 여부만 확인하는 단위 테스트로 실제 의미 검증을 대체하지 않는다.

## 평가 계약

첫 source digest는 `sha256:183e8cd9341af58444ab7b8568537ffc91bf1085ecb2e67b549f1f479c9cff07`다. 세 실제 모델 scope는 직전 세션의 `roles.json`을 복사해 사용하며 역할 model/effort를 바꾸지 않았다. 각 runner는 호출 전 실제 `model/list`로 지원 여부를 검사하고 source·역할·inventory·prompt·schema·fixture·seed를 새 평가 계약에 결속했다.

기능 scope는 서로 다른 원장·작업 복사본에서 병행했다. 이 실행의 지연은 성능 benchmark의 단독 실행 수치가 아니다. 기존 평가 원장과 실패 artifact, 원본 fixture와 legacy/prototype은 변경하지 않았다.

첫 source의 결정적 Gate는 전체 **464개 테스트**, compileall, pip check, synthetic lifecycle, legacy freeze를 통과했다. 역할 회귀는 **48/48 PASS**이며 recall 94.44%, precision 100%, critical false admission·clean false block·schema failure·critical seed 불일치 모두 0건이다. 실제 E2E는 **4/4 PASS**, 중복 효과 0건이다.

첫 Planning은 **4/18 cell 완료 후 중단**했다. S01·S02·S03의 세 Goal이 차단됐고 대상 정보가 없는 S06은 정상 차단됐다. 관련 실패가 반복된 source의 추가 소비를 줄이기 위해 소유 PID·부모·실행 명령을 확인한 해당 평가 App Server만 종료했다. runner가 S04 미완료 호출의 실패 receipt와 `FAILED` 상태를 기록했다. `planning-interruption.json`과 `first-source.patch`를 보존했으며 전체 Planning 보고서나 PASS는 생성하지 않았다. 한 미완료 receipt에 usage가 없으므로 이를 0-token 실측으로 취급하지 않는다.

## 명시적 명령 금지의 보존

`explicit-command-ban-v2/`는 별도 개발 진단이며 전체 qualification에 합산하지 않는다. 원본 S02와 같은 파일 관측에, 승인 후 분석·검증 Task에서도 테스트를 포함한 명령 실행을 금지하는 사용자 원문을 별도로 제공했다. 실제 정규화와 독립 검토를 거친 Goal은 `ready`이며 해당 실행 제한을 constraint와 prohibited effect에 명시했다. 파일 생성·수정·삭제 금지도 유지했고 독립 검토 finding은 없었다.

이 결과는 명시적인 사용자 금지의 정규화 보존을 확인하며, 실제 명령 미실행 evidence 수집이나 그 Goal의 Task 실행 완료를 증명하지 않는다. 첫 진단 스크립트는 lock JSON 직렬화에서 모델 호출 전에 실패했다. 해당 스크립트와 로그를 보존하고 직렬화만 수정한 새 진단 디렉터리를 사용했다.

## 첫 source의 실패와 후속 보완

첫 S01 seed 17에서는 합산 반환 Hard AC를 만들었지만 금지 효과에 “관찰된 반환 동작을 변경하지 않는다”는 모호한 표현이 들어갔다. 현재 관찰된 구현은 뺄셈이므로 필요한 버그 수정과 충돌한다. Reviewer의 `GOAL_PROHIBITED_EFFECT_CONTRADICTS_REQUIRED_FIX`를 Core가 `conflict`로 반영했다. 이 차단은 올바르며 성공으로 재분류하지 않는다. 보존할 문서·테스트 계약과 현재 구현의 동작을 구체적으로 구분해야 한다.

S02는 원인 AC에서 합산 기대값, 뺄셈 구현, `add(2, 3) == 5`와 `-1`의 불일치를 이미 명시했다. 그런데 Reviewer가 별도의 반환 동작 보존 AC를 요구해 `PUBLIC_BEHAVIOR_CONTRACT_OMITTED`를 제출했다. 독립 코드·근거 감사와 주 에이전트 검토에서는 이 요구가 단순 읽기 전용 분석을 새 호환성 보존 의무로 확장한 과잉 검토라고 판단했다. 원래 결과를 수정하지 않고 새 검토 입력·지침의 범위를 보완했다.

S03은 명시된 합산 보존 조건과 모호한 “관찰된 반환 동작” 보존 제약이 함께 있었고, migration 미실행 요구를 검증 명령 미실행 증명까지 넓혔다. Reviewer의 두 finding과 전체 원문을 보존했다.

후속 지침은 보존 대상의 실제 값·관계를 명시하고 결함 수정 자체를 금지하지 않도록 한다. 분석 Goal의 정상 기대값·현재 불일치 설명을 별도 구현 의무로 승격하지 않는다. 특정 작업의 실행 금지를 모든 읽기·검증 명령으로 확대하지 않으며, 전체 proposal에 이미 담긴 의미를 특정 AC에 반복하도록 요구하지 않는다.

후속 source digest는 `sha256:045939e982182d824742b4cffb8f1c60f9389100823a63b3892fc3a88739df90`다. **464개 테스트와 결정적 Gate는 PASS**다. 반복된 정규화 실패를 근거로 세션용 정규화 역할만 Luna/high에서 Terra/high로 올렸다. 다른 역할과 추론 수준은 유지했다. 새 역할 설정 digest는 `sha256:1d0b28c22750dc2f65334f72d627b51863cebf41d5dfd3e9eec78ff0659b9736`이며 `revised/roles.json`과 새 평가 계약에 기록했다. 제품 기본 역할 설정이나 허용 fallback을 바꾸지 않았으며 이전 source·역할의 보고서를 새 qualification으로 합치지 않는다.

`revised/review-boundary-probe/`는 변경하지 않은 이전 S01·S02 후보를 실제 Reviewer에 다시 제공한 개발 진단이다. 판정 기준과 원본 cell digest를 호출 전에 잠갔다. 실제 충돌이 있는 S01에는 `GOAL_PROHIBITS_REQUIRED_BUGFIX`가 남았고 S02는 finding 없이 통과했다. 이는 고정된 두 후보의 검토 경계를 확인하며 전체 회귀 PASS를 대신하지 않는다.

`revised/explicit-command-ban/`에서도 사용자가 명시한 승인 후 명령 금지는 `execution_policy` constraint로 보존됐고 Goal은 `ready`다. `prohibited_effects` 배열에 중복되지 않았다는 이유로 누락으로 처리하지 않으며 전체 계약을 함께 해석한다.

## 후속 source의 기능 검증

후속 실제 모델 scope의 model lock은 `sha256:84733088b66d238c6c0b059493560d490a14d78faf1563dbe3b3fb17ef8097d8`이다. 지침과 정규화 모델을 함께 변경했으므로 결과 차이를 어느 한 변경의 효과로 단정하지 않는다.

| 범위 | 결과 | 보고서 digest |
|---|---|---|
| deterministic | 전체 464 tests 및 5개 Gate PASS | `sha256:a685b24688d43b21146abb6c2493518bca785ae0dfc8ce2b716329a37694acc6` |
| role-fixture | 48/48 cell, PASS | `sha256:dfbdde062f95984456d7342d2bf44d6bab2490cbeeb017e76841bc47c5306b67` |
| project-e2e | 4/4 cell, PASS, 중복 효과 0 | `sha256:f5f08b0f533249860cc76040066e986e66dc588821f3f3830e55b981bb847757` |
| full-planning-pipeline | 18/18 수집, 13 PASS·5 FAIL, 전체 FAIL | `sha256:02d8ec2ae1d6e300bcba423c577939c90d61e2c99416a0797ecf07bd49ca6022` |

역할 회귀는 recall 97.22%, precision 100%, critical false admission·clean false block·최종 schema failure·critical seed 불일치 모두 0건이다. 48 receipt의 input은 1,185,047, cached input은 652,800, output은 12,080 tokens이며 latency 합은 460,813ms, schema recovery는 2회다. 모든 receipt에 usage가 있다. cached input은 input에 포함되고 reasoning은 output에 포함된다.

E2E는 실제 Task 준비, Worker, 직접 테스트, 독립 Task Validator, 독립 Goal Test와 저장 turn 중단·재개, stale 입력 차단, 생성 receipt 유실 차단을 검사했다. 읽기 전용 응답 보고 Goal 전체의 실제 실행이나 물리적 PC 종료·사용량 제한 검증으로 범위를 넓히지 않는다.

완료된 source·report·contract·cell의 digest와 수량, 모델 변경 범위, 두 개발 진단의 입력 보존을 별도 감사로 대조했다. 실제 DB·prompt·receipt·작업 복사본과 세션 역할 설정은 Git에 포함하지 않는다.

## 후속 Planning에서 확인한 결함

S01 seed 17의 `MISSING_ORIGINAL_FIXTURE_MUTATION_GUARD`는 Goal에 Profile 정책을 중복 기재하지 않았다는 차단이다. 해당 Profile에는 작업 복사본만 수정하고 원본 fixture를 보존하는 정책이 있으며, Goal은 `profile_definition_digest`로 그 Profile에 결속된다. Core의 Goal 등록은 활성 Profile 일치를 검사하고 실제 Task 준비·Worker 입력에도 Profile 정책을 제공한다. 독립 감사와 주 에이전트의 코드 대조에서는 이 정책과 충돌하지 않는 Goal에 동일 문구를 추가로 요구한 과잉 검토로 판단했다. 원래 실패 판정은 보존한다. 별도로 Skeleton·Plan 역할에는 Profile 전체가 직접 투영되지 않는 입력 경로가 있으므로 정책의 실제 전달 범위도 후속 검토해야 한다. 이 결속을 범용 OS 접근 통제로 해석하지 않는다.

S02 seed 17의 `VERIFICATION_GOAL_TEST_DIFF_EVIDENCE_MISSING`는 유효한 Plan 검증 계약 결함이다. 파일 무변경 `ac_003`에 대해 Task 검사에는 `file`·`diff`가 있으나, 독립 Goal Test는 해당 AC를 포함하고 file·diff 관측을 약속하면서 `required_evidence_kinds`에 `model_review`, `external_observation`, `file`만 넣었다. `evidence_mode=independent`에서는 Task의 diff를 암묵적으로 재사용할 수 없다. 이는 Goal 정규화 실패와 구분하며, 독립 검사의 문장·대상 AC·필수 evidence 종류를 일치시키는 Plan 생성 보완이 필요하다.

S03 seed 17은 Skeleton refiner가 기존 `strategy_family`를 바꿔 adapter validator에서 거부됐다. 한 번의 structured recovery 후에도 실패했으며 runner는 이를 `schema_failed`로 기록했다. JSON 형식 오류가 아니라 refinement의 의미 계약 위반이다. 기존 전략을 고정하는 검사는 정상 동작했으므로 완화하지 않는다. 후속으로 고정 값을 출력 대상에서 제외하는 refinement 전용 schema 또는 명시적 immutable 입력을 검토하고, 실제 전략 변경은 새 Skeleton 생성·재계획으로 처리해야 한다.

S02 seed 89는 상세화 역할이 `task_analyze_report`의 Skeleton 의미를 바꿔 한 번의 recovery 뒤 `schema_failed`로 종료됐다. 해당 검사는 `kind`, `objective`, `goal_criterion_refs`, `produces`, `consumes`를 비교한다. 평가 cell·receipt에는 거부된 raw 응답이 없고 실패 receipt의 `output_digest`는 null이다. 역할 thread도 `ephemeral=True`로 생성되어 보존된 rollout에서 응답을 복원할 수 없었다. 따라서 어느 필드가 바뀌었는지 또는 두 응답이 어떻게 달랐는지는 특정하지 않는다. 후속에서는 불변 필드와 상세화 가능 범위를 명확히 하고, 거부 후보·필드 차이를 입력·receipt에 결속한 비권위 artifact로 남겨 진단 가능성을 보완해야 한다.

S03 seed 89는 Skeleton 생성 결과의 중복 `task_ref`를 `PlanSkeletonCandidate` validator가 거부했다. 한 번의 recovery를 사용한 호출은 최종적으로 `schema_failed`로 종료됐다. Task ID의 유일성 검사를 유지하면서 생성 계약과 실패 진단을 보완할 대상이다.

seed 43에서는 여섯 시나리오가 모두 기대 판정을 충족했다. S02의 선택된 Plan은 응답 보고를 `inspect` Task로 생성하고 내용 검토와 프로젝트 파일 무변경 검사를 분리했다. 독립 semantic Goal Test는 `model_review`·`external_observation`·`file`, 별도의 결정적 무변경 Goal Test는 `file`·`diff`를 요구했다. 현재 역할의 명령 금지를 미래 Task에 추가하거나 미발생 조건을 기대 효과로 기록하지 않았다. 이는 해당 선택 결과의 관측이며 다른 순서의 실패를 상쇄하지 않는다.

## 최종 Planning 집계와 비용

| 시나리오 | seed 17 | seed 43 | seed 89 |
|---|---|---|---|
| S01 단일 버그 수정 | FAIL: Profile 정책 중복 요구 | PASS: Plan 선택 | PASS: Plan 선택 |
| S02 읽기 전용 분석 | FAIL: 독립 Goal Test의 diff 누락 | PASS: Plan 선택 | FAIL: 상세화의 Skeleton 의미 변경 |
| S03 migration 비교 | FAIL: refinement 전략 변경 | PASS: Plan 선택 | FAIL: Skeleton Task ID 중복 |
| S04 produces/consumes DAG | PASS: Plan 선택 | PASS: Plan 선택 | PASS: Plan 선택 |
| S05 필수 외부 계약 부재 | PASS: 질문·차단 | PASS: 질문·차단 | PASS: 질문·차단 |
| S06 비가역 효과 대상 부재 | PASS: 질문·차단 | PASS: 질문·차단 | PASS: 질문·차단 |

정상 입력은 **7/12 Plan 선택**, 정보 부족 입력은 **6/6 질문·차단**이다. 최종 schema failure는 3건이다. 보고서의 최대 logical 역할 호출은 14회, 최대 candidate version은 5개다. 이 두 필드가 없는 schema 실패 3개 cell은 0회 실측으로 해석하지 않으며, 해당 실패 비용은 실제 receipt에서 집계했다. S04 seed 89에서는 한 상세 후보가 `VERIFY_VALIDATE_MUTATION_EVIDENCE_MISSING`으로 차단되고 다른 후보가 선택됐다. 최종 선택 성공을 모든 후보의 무결함으로 표현하지 않는다.

세 schema 실패의 상세 원인은 cell assessment의 공통 오류문만으로 판정하지 않고 보존된 receipt의 `error_summary`를 대조했다. 해당 call ID·역할·오류문·recovery 횟수는 `result-audit.json`의 `scopes[].failed_receipts`에 모아 원본 cell·role-progress receipt와 연결했다.

Planning의 중복 제거한 94개 receipt는 input **3,504,395**, cached input **1,790,080**, output **158,213 tokens**다. latency 합은 **3,436,570ms**, schema recovery는 **34회**이며 모든 receipt에 usage가 있다. cached input은 input에 포함된다. 이 값은 현재 Planning 기능 평가의 비용으로, E2E·개발 진단·첫 source 비용을 합한 세션 총량이나 독립 성능 benchmark가 아니다.

후속 Planning contract digest는 `sha256:de4c1094fd0d5678e70674815358c368e370973d9e12f2f29ec0215f0da42057`다. 최종 `result-audit.json`에서 네 scope의 source가 현재 source와 일치하고 완료 cell의 계약·fixture·seed·수량이 일치함을 확인했다. 서로 다른 source의 보고서를 결합하지 않았고 원본 oracle·threshold는 유지했다. 전체 Planning runner는 `COMPLETED` 보고서를 남겼으며 Gate FAIL을 나타내는 종료 코드 1로 정상 종료했다.

## 판정과 다음 작업

Goal 단계·동작·효과 해석을 보완했으나 정상 입력의 계획 생성 안정성이 부족하므로 1.0 cutover는 **NO-GO**다. package·CLI는 `flowmarshal-engine`을 유지한다. 다음 변경은 이번 실패 근거에 한정한다.

1. Profile 정책의 Goal 중복 요구를 제거하고 Skeleton·Plan 입력까지 필요한 정책을 명시적으로 투영하는 경계를 정리한다.
2. 독립 Goal Test의 대상 AC·검사 문장·필수 evidence 종류를 일치시킨다.
3. Skeleton 생성의 Task ID 유일성과 refinement·상세화의 불변 필드를 생성 계약에 반영한다. 기존 Core 거부 검사는 유지하고, 거부 후보·필드 차이는 비권위 진단 artifact로 보존한다.
4. 변경한 최종 source로 기능 Gate를 다시 대조한 뒤 별도 실제 token/latency Gate를 수행한다. 읽기 전용 응답 보고 Goal의 실제 전체 실행과 사용량 제한 재개는 현재 4개 E2E와 구분해 검증한다.
