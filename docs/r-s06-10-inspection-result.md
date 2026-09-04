# R-S06-10 정식 인용 출처·검사 평가 정합성 결과

## 판정과 범위

구현과 최종 결정적 검증은 완료했다. **제한 실제 진단은 첫 호출 전 잠금 검증에서 FAIL이며, 실제 논리 호출 0회·provider turn 0회·schema recovery 0회다.** 실제 Reviewer·Expander의 품질 개선은 미검증이다. 전체 qualification과 1.0 cutover는 **NO-GO**를 유지한다.

실패 원인은 새 진단 하네스가 출력 중인 최상위 `prepare.log`까지 입력 잠금 대상으로 포함한 것이다. 비어 있을 때 잠긴 로그에 준비 완료 출력이 기록되면서 `INPUT_LOCK_CHANGED: prepare.log`가 발생했다. 모델이나 인용 제출물의 실패는 아니다. 로그 잠금 결함을 수정하고 최종 결정적 Gate를 통과했지만, 첫 실패 중단 규칙에 따라 이 진단의 잠금·로그·FAIL을 보존하고 실제 호출을 재개하지 않았다.

진단 root: [r-s06-10-20260905-v1](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1)

## 권한·기준선

- 첫 파일 조회·명령 전에 이 작업에 실제 제공된 개발자 권한 지침에서 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인했다. 부모 설정을 실제 관측으로 대체하지 않았다.
- 전역 `C:/Users/sjs95/.codex/AGENTS.md`, 제품 `D:/codex/flowmarshal/AGENTS.md`, 권위 재설계·cutover ADR·R3.1 동결 문서를 적용했다. 역할 cwd를 제품 소스로 사용하지 않았다.
- 시작 HEAD와 로컬 `origin/main`, 실제 원격 main은 모두 `7eee130437f5fa81a97c3ddb737fba9988b9addb`였고 작업 트리는 clean이었다.
- 원본 분석 작업 `01a06cda-b791-70e0-8653-935f8fd7cb66`의 최종 결과를 읽고 원문 근거를 재사용했다. 최종 HEAD에 이미 있던 전역 검사 부분 누락 표현과 평가 gate 분류 제한 보완은 재구현하지 않았다.
- 진단 preflight에서 실제 App Server 권한·model inventory·기존 역할 설정·실행 파일 digest를 확인했다. 실제 역할 thread는 만들지 않았으므로 이번 진단의 thread/turn receipt는 없다.

## 구현

### 등록 자료의 공통 입력과 정식 주소

[planner_roles.py](D:/codex/flowmarshal/src/flowmarshal/engine/planner_roles.py)의 `inspection_source_catalog`를 Expander와 Reviewer에 공통 적용했다. 등록 참고자료와 지침 entry는 실제 파일 bytes의 digest를 검사한 UTF-8 `content`를 `source_ref`, `selector`, `content_digest`, 원본 `evidence_ref`와 함께 제공한다. 다른 Project Map entry에는 정확한 경로와 인용 metadata를 제공한다.

검사 자료의 정식 인용은 `project:entry_ad363844d3392d9ef718d2d6` / `/content`다. Goal trace의 복제 본문이나 배열 번호를 파일 주소로 재구성하도록 요구하지 않는다. 입력 projection과 응답 인용 검증이 같은 파일 원문 검사를 사용하고, 응답 수용 시에도 digest·selector·연속 quote를 검사한다. 잘못된 주소의 자동 교정이나 사후 alias는 추가하지 않았다.

### provider 설명과 실제 검증 조건

[plan_inspection.py](D:/codex/flowmarshal/src/flowmarshal/engine/plan_inspection.py)의 strict schema 설명과 공유 지침을 실제 adapter 조건에 맞췄다.

| 구분 | 고정한 의미 |
|---|---|
| AC 관계 | `relation`은 연결 의무의 출처다. 전역 semantic 의무나 비필수 분류는 기존 선택적 연결을 금지하지 않는다. |
| AC 연결 누락 | 명시 절차를 수행하는 검사 계약은 존재하지만 ID 연결이 없으면 `missing_validation_link`다. 검사 실행 계약 누락으로 확대하지 않는다. |
| constraint 행 | 비적용 constraint도 해당 원문을 인용한다. 실제 의무의 일부가 빠지면 존재하는 검사 ID와 `missing_task_validation` finding을 함께 표현한다. |
| 검사 능력 행 | `contradicted`는 `validation_scope`, `unresolved`는 `insufficient_evidence`를 요구한다. 정상 행에 모순되는 finding을 붙이지 않는다. |
| evidence | file/diff의 단순 언급이나 `required_evidence_kinds`만으로 test 등 다른 직접 입력이 명시적으로 제외됐다고 추정하지 않는다. |

기존 지침의 “명시한 경우에만 연결”처럼 선택적 연결을 금지하는 것으로 읽힐 표현도 필수 연결의 판정으로 명확히 했다. 제품 AGENTS와 권위 설계 문서를 함께 정합화했다.

### 별도 fixture revision과 독립 대조

[fixture 생성기](D:/codex/flowmarshal/scripts/diagnostics/r_s06_10_fixtures.py), [새 기대 기준](D:/codex/flowmarshal/tests/fixtures/engine/plan-inspection-v2-expectations.json), [최소 source 보완](D:/codex/flowmarshal/tests/fixtures/engine/plan-inspection-v2-source-inputs.json)을 추가했다. 과거 fixture·기대값·oracle·threshold·FAIL은 수정하지 않았다.

원래 Goal·Plan·등록 문서와 oracle의 goal 전용 동작 분기 및 phase 공통 unittest 실행을 직접 대조했다. 새 clean/combined/semantic-explicit와 해당 오류 파생 사본에만 다음 두 연결을 추가했다.

- `ac_001` → `val_goal_independent_unittest`: AC가 기존 unittest 절차를 명시하며 이 integration 검사가 그 절차를 수행한다.
- `ac_003` → `val_goal_independent_behavior_contract`: AC의 새 프로세스 unittest는 Task 단계로 제한되지 않는다. 등록 goal phase는 Task 검사를 다시 수행하며 실제 구현도 unittest를 실행한다.

과거 clean은 두 누락을 가진 역사적 반례로 보존했다. F001/F002 각각의 단독 누락, 전역 semantic 의무와 선택적 연결, 명시 semantic 연결 제거, 독립 Validator 책임 자체의 누락, evidence의 명시적 제외와 단순 언급을 별도 결정적 사례로 고정했다. 부분 검사 누락 사례에서도 unittest·scope 책임은 남아 있으므로 기존 검사와 누락 finding의 동시 표현을 검사한다.

기존 `missing-link`, `stored-expanded`, 복수 독립 결함의 `stored-multi-defect`, 정상 combined, 잘못된 phase와 Worker의 미래 Validator 결과 의존은 보존했다. 정상 사본의 28개 AC×validation 관계와 constraint 책임도 기록했다. 수정·보존 원문인 constraint_001/002 자체는 검사 의무 행에서 비적용이며, 실제 Task 검사 의무의 직접 근거는 constraint_003이다.

호출 전 별도로 작성한 [독립 원문 대조](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/independent-fixture-review.json)와 [fixture assessment](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/fixture-assessment.json)를 잠금에 포함했다. 17개 원본 입력의 canonical digest를 파생 전에 확인하고 실제 복사 bytes digest도 기록한다. 회귀는 과거 실행 디렉터리 없이도 저장소의 fixture로 수행된다.

### 평가·실행 결속

[qualification.py](D:/codex/flowmarshal/src/flowmarshal/engine/qualification.py)의 planning 계약에 실제 공통 projection 및 원문 검사 구현을 추가로 결속했다. 실제 adapter strict schema·공유 지침 결속을 유지했고 source manifest의 파일별 digest를 감사용 snapshot으로 제공한다.

[새 진단 하네스](D:/codex/flowmarshal/scripts/diagnostics/r_s06_10.py)는 정상 사례부터 시작하는 기존 13회 순서, provider turn 최대 13회와 recovery 0회를 잠근다. 실제 요청·전송 schema·prompt·thread/turn·receipt를 대조하고, 생성 Plan은 별도 독립 평가와 기준 digest가 있어야 Reviewer에 전달한다. 전역·프로젝트·workspace 지침의 경로·본문 digest를 잠그며 실제 `thread/start.instructionSources`와 turn 전에 대조하도록 구현했다. 이 실제 thread 대조는 이번에는 도달하지 않았고 결정적 회귀만 통과했다.

실제 전송용 strict schema를 저장한 뒤 그 원본과 비교한다. JSON 저장으로 object key 순서가 달라진 요청에서 schema를 다시 만들어 다른 `required` 배열 순서를 발생시키지 않는다. 이 경계는 모의 provider 호출 회귀로 확인했다.

실패 뒤 최상위 실행 출력 `.log`를 잠금에서 분리했다. workspace 안의 실제 입력 로그와 JSON·schema·source·지침 snapshot은 계속 잠근다. 이 수정은 최종 source의 변경이며 기존 preflight를 수정하지 않았다.

## 결정적 검증과 제한 진단

| 검증 | 결과 |
|---|---|
| 최초 결정적 Gate | 5/5 PASS, 전체 테스트 527개 PASS |
| 최종 결정적 Gate | 5/5 PASS, 전체 테스트 528개 PASS |
| Gate 구성 | compileall, 전체 테스트, pip check, synthetic lifecycle, legacy freeze manifest |
| 원시 인용 회귀 | 세 잘못된 trace 주소를 각각 거부, 정식 파일 주소·원래 quote 성공, trace 순서 변경 보존, 잘못된 selector/digest/quote 거부 |
| 원시 내부 모순 회귀 | 관계/finding 충돌, 비적용 constraint 두 행의 원문 누락, constraint 및 contradicted 행의 잘못된 finding 종류를 목표 오류별로 거부 |
| 의미 기준 회귀 | 새 정상/단독 누락/역사적 두 누락/semantic/부분 실행 누락/evidence 경계를 고정 oracle와 결속해 대조 |
| 실제 진단 | 첫 잠금 검증 FAIL, 논리 호출 0/13, provider turn 0/13, recovery 0 |

scripted 제출물의 통과는 구조·고정 oracle 결속의 증명이며 모델의 자연어 의미 탐지 개선 증명이 아니다. 과거 receipt의 누적 usage 집계도 결정적으로 재검증했으며 이를 새 실제 호출로 집계하지 않았다.

실패 로그의 잠금 기대 digest는 빈 파일의 `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`였고, 완료 출력 후 실제 digest는 `sha256:a0b843a479591646dbbd395cf1e007d6fc0cda7dd56c2c454a9941333d915470`였다. 원래 잠금의 artifact 181개 중 이 파일 하나만 달랐다.

91개 source 파일을 준비 당시 snapshot으로 남겼다. 실패 후 하네스와 회귀 파일 두 개만 추가 수정했고, 최종 source digest에 맞춘 결정적 Gate를 별도로 실행했다. 이전 관련 실행의 1,951개 파일은 hash 대조에서 그대로 보존됐다.

## 주요 증거와 digest

| 증거 | digest |
|---|---|
| 준비 당시 source | `sha256:4ebd789eeffe4e4a8d80b7034e6bb532ac6ba607e4cf3619c224114e163cd96b` |
| 최종 source | `sha256:ece76db8d2b3c56a4a5467b0b8f6649ac228956cb02f7c7c21015b00f99efc55` |
| preflight | `sha256:4c5d8da628d0aa74d42d8f80ea18b3b06af479bb8ab8c8ac4c405f8a21576db4` |
| 고정한 실행 기대값 파일 | `sha256:ab2d3ea8f7ebf9b0ed429270c01f5a6c2a626d02e4573e714dd36cec44b74272` |
| 공유 prompt 계약 | `sha256:2502f853d5410464f3d5f54ab05b12cef22bc5a2a5fadc124818de7abfcb1b24` |
| planning 출력 schema 계약 | `sha256:8042937449ad21ca028a54678269dc723652abf1f9ee3f669c33bed7cc7c176d` |
| 미전송 clean strict schema | `sha256:ca83c39aec4fda7c51eb540aa80864df510a6377146f48049d869671f52416c5` |
| 미전송 clean 요청 | `sha256:1ababf645417e7526218350e43f2880db43a4d8eedcbcfddbcde20b8ef20eb2f` |
| 주입 지침 source 결속 파일 | `sha256:45d03b5114a9db87ad33f18c8279ff41cf0e8982d5bd57196afb08f3ac61e3f4` |
| 최종 결정적 report | `sha256:0735238db8815dfe76aff548999df1149bdcdf9828e70ce63c1ffb096b284a3c` |

주입 대상으로 잠근 source 내용 digest는 전역 AGENTS `6b1b3dbf…`, 제품 AGENTS `096d161f…`, 새 workspace AGENTS `57fb5012…`이며 전체 값과 절대 경로는 결속 파일에 있다. 이번 실제 역할 call/thread/turn ID와 model receipt는 **없다**. 미전송 준비 요청을 실제 요청 receipt로 표현하지 않는다.

- [호출 전 잠금](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/preflight.json)
- [잠금 실패 원본](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/lock-failure.json)
- [원본 FAIL summary](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/summary.json)
- [최종 검증·미실행 목록](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/final-verification.json)
- [준비 당시 source snapshot manifest](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/executed-source-manifest.json)
- [지침 경로·내용 결속](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/instruction-binding.json)
- [최종 결정적 Gate](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/deterministic-final/qualification-report.json)

## 호출·비용과 남은 검증

역할 설정은 기존 값을 유지했다. 일반 Reviewer는 `gpt-5.6-terra/high`, Expander는 `gpt-5.6-luna/high`, critical Reviewer는 `gpt-5.6-sol/xhigh`로 준비했다. 어느 역할도 이번에 실행되지 않았다.

| provider 실측 | R-S06-08 직전 clean | R-S06-09 clean | R-S06-10 |
|---|---:|---:|---|
| input tokens | 35,940 | 83,755 | 미발생 |
| cached input tokens | 0 | 37,632 | 미발생 |
| output tokens | 1,034 | 10,947 | 미발생 |
| reasoning tokens — output에 포함 | 991 | 6,972 | 미발생 |
| input + output | 36,974 | 94,702 | 미발생 |
| 역할 latency | 24.313초 | 205.062초 | 미발생 |
| provider turn duration | 23.861초 | 204.675초 | 미발생 |

새 하네스는 빈 새 thread의 첫 turn과 `thread/tokenUsage/updated.total`을 확인하고 모든 provider turn의 누적 input/cached/output/reasoning과 latency를 분리 집계한다. reasoning을 output에 다시 더하지 않는다. 이번 summary의 receipt latency 합계 0은 빈 배열 합계이며 실측 속도가 아니다. 청구 금액은 제공되지 않았다.

과거 두 값과 이번 입력·출력·지침 계약은 다르며, 새 실제 관측도 없으므로 정확성·비용·지연 개선을 주장할 수 없다. 정상 0 finding 역시 과거 숨은 연결 누락을 놓친 결과를 기준점으로 사용하지 않는다.

13개 실제 구성 전부, 생성 Plan의 독립 정상성 평가, 실제 주입 지침 receipt 대조, 정식 출처를 사용하는 실제 모델의 일관성과 의미 검출은 미실행이다. 다음 실제 진단에는 **최종 source와 새 디렉터리의 새 잠금**이 필요하다. 기존 v1 잠금을 고치거나 잔여 호출을 이어서 사용하면 안 된다. 동일 13회 상한과 recovery 0, 첫 실패 중단 기준을 유지해야 한다.

GoalContractRevision·PlanContractRevision·ReviewerSubmission·DB schema와 Core admission/score 권위는 유지했다. coverage 자동 보정·새 제품 역할 단계·역할 모델 교체는 없다. 전체 S06, Plan 활성화, Worker 실행, 전체 qualification·1.0 cutover는 수행하지 않았으며 운영 SQLite나 운영 파일 원장을 만들지 않았다.

## Git 인계

제품 지침에 따라 이 세션의 코드·회귀·AGENTS·권위 설명과 본 보고서를 하나의 한국어 커밋으로 기록하고 비공개 권위 origin에 push한다. 실제 결과·commit SHA·원격 확인·최종 작업 트리 상태는 [git-result.json](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-10-20260905-v1/git-result.json)에 남긴다. 로컬 진단 원문과 snapshot은 Git 제외 경로에 보존한다.
