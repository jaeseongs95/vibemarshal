# 역할 설정 주입 경계와 R26 회귀 인계

기준일: 2026-09-05 KST. 프로젝트: `D:\codex\flowmarshal`.

## 범위와 권한

시작 HEAD·main·origin/main은 `a28eedfc224a12d4192c63aff63d7ecf186a20c1`이며, 승인된 7개 미커밋 경로만 존재했다. 기존 변경을 보존하고 전체 tracked diff와 새 파일을 감사했다. 첫 조회 전에 이 turn의 개발자 설정에서 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인했다. 승인 질문이나 권한 상승 요청은 없었다. private origin은 `jaeseongs95/flowmarshal`이다.

전역·프로젝트 지침, 권위 설계 §6.1·§7.1, cutover ADR, R3.1 동결 기준, README와 R26 인계를 대조했다. 적용되는 하위 override와 별도 fallback 지침 파일 설정은 없었다. 장기 입력 결속 설명은 루트 AGENTS와 권위 설계에 추가하고 세션 결과는 이 문서에 둔다.

## 전체 변경 감사와 보정

| 경로 | 감사·수정 내용 |
|---|---|
| `scripts/diagnostics/r_s06_10.py` | 외부 역할 설정의 strict schema·중복 key, 절대 입력 경로, 원문 snapshot과 세 digest, planning/preflight 결속, 호출 직전 source·copy·inventory·request·v2 검사. 실행 모드의 `--role-config` 무시를 거부로 고치고 요청의 inventory digest·필수 capability·fallback 순서 검사도 보완했다. |
| `tests/test_engine_execution_automation.py` | 실제 `_prepare` 계약은 `model_review/file/test`다. `model_review`만 기대하던 assertion을 고쳤다. 실제 Validator request에 file/test가 전달되며 catalog의 required 인자를 `model_review`만으로 줄여도 같은 직접 evidence가 남음을 검사한다. |
| `tests/test_engine_inspection_fixture_revision.py` | 선택 연결과 필수성, 비배타적 file/diff 언급과 명시적 test 제외, task/goal oracle 양성 연결을 고정 기대표와 대조했다. |
| `tests/fixtures/engine/plan-inspection-general-reviewer-sol-high-roles.json` | R26 역할 설정 대비 general Reviewer만 `gpt-5.6-sol/high`로 다르다. expander `gpt-5.6-luna/high`, critical Reviewer `gpt-5.6-sol/xhigh`와 다른 역할은 그대로다. |
| `tests/fixtures/engine/r-s06-26-clean-semantic-failure.json` | 원본 payload·응답·최종 문자열·기대표·assessment·전체 ProjectMapRevision·등록 본문·workspace 파일·요청 inventory와 provenance를 대조했다. 과거 파일을 수정하거나 재개하지 않았다. |
| `tests/test_engine_r_s06_26_regression.py` | strict schema와 원본 digest를 확인하고 portable 경로로만 재배치한다. project-map catalog의 tuple/list 차이는 canonical JSON으로 비교한다. 역할 configuration digest와 portable 본문별 digest도 확인한다. |
| `tests/test_engine_r_s06_role_configuration.py` | 기본 입력 보존, cwd 독립성·상대 경로 거부, schema·빈/null 값·중복, bytes/canonical 차이, snapshot 변조, 미지원 model/effort, 요청·lock·fallback 순서·CLI mode를 검증한다. |

원본 clean Plan 파일과 전송 payload는 `supersedes_plan_revision_id=null`의 기본값 직렬화 유무만 달랐다. `PlanContractRevision`으로 읽은 canonical 값은 같고 portable fixture의 Plan은 실제 전송 payload와 동일하다. Project Map의 전체 revision과 catalog projection도 구분한다. 표현 차이를 해결하기 위해 제품 계약이나 기대표를 바꾸지 않았다.

제품 `runtime.py`, Goal/Plan 의미, clean fixture 생성 의미, 기존 기대표·evaluator·validator·oracle·threshold·taxonomy와 기본 `config/qualification-roles.json`은 변경하지 않았다. 문서 4개를 포함한 최종 변경 범위는 총 11개 파일이다.

## 역할 입력 운영 계약

`--role-config`는 `prepare`에서만 받으며 명시 입력은 절대 경로여야 한다. 생략하면 기존 S05 `roles.json`을 사용한다. 후보를 기본값으로 선택하지 않는다. 경로를 cwd에 따라 해석하거나 run/review-generated에서 다른 설정으로 덮지 않는다.

세 digest는 각각 원문 bytes, 입력 JSON의 canonical 표현, default fallback을 포함한 typed `EngineRoleConfiguration`을 식별한다. 입력 경로·선택 이유와 함께 preflight·planning binding에 남기고 `roles.json`에는 원문 bytes를 그대로 복사한다. 공백만 바꿔도 bytes 결속이 달라져 prepare 후 변경으로 차단된다. source와 snapshot은 호출 직전 다시 확인한다.

제한 diagnostics는 선택과 fallback 모두 fresh inventory의 정확한 model/effort 지원을 요구한다. 일반 v2가 미지원 fallback을 `supported=false`로 기록하는 기능은 유지된다. request의 선택·inventory digest·필수 capability·순서 있는 fallback과 v2 lock을 재검증하며 자동 fallback이나 모델 승격은 없다. 테스트의 inventory는 R26의 저장 관측이며 현재 provider 지원 확인을 대신하지 않는다.

## R26 실패 보존

원본 사례는 `r-s06-19-evidence-order-r26-20260905-v1`의 clean이다. Portable 회귀는 구조 검증을 통과한 뒤 기존 evaluator의 원본 assessment 전체와 동일한 결과를 요구한다.

| AC | validation | 기대 `ac_link_required` | 원본 응답 |
|---|---|---:|---:|
| `ac_004` | `val_task_scope_preservation` | false | true |
| `ac_004` | `val_task_unittest` | false | true |

예상 밖 finding은 `VAL_SCOPE_001` 한 개이며 최종 의미 판정은 FAIL이다. 선택된 실제 연결이 존재해도 필수성은 false일 수 있다. AC가 명시한 task/goal oracle은 각각 양성 연결을 유지한다. R25의 세 누락과 R26의 두 과잉 필수화는 별도 실패로 보존한다.

## 검증 기록

- 알려진 첫 실패를 포함한 집중 회귀: **23 tests OK**.
- execution automation, R25/R26, 역할 설정, inspection, v2 lock/consumer, roles, qualification 관련 16개 모듈: **158 tests OK**, 40.663초.
- 전체 unittest: **596 tests OK**, 65.843초, 프로세스 종료 코드 0. 2026-09-05 17:06:52~17:07:59 KST에 실행했고 전체 프로세스 경과는 67.031초다.
- 최종 source의 fresh 결정론 Gate: **5/5 PASS**, failure 0, 종료 코드 0. 17:08:51~17:10:00 KST, 69.063초. compileall·전체 unittest(596개, 65.792초)·pip check·synthetic lifecycle·legacy freeze가 각각 PASS다. freeze는 40개 파일을 확인했고 변경·누락·예상 밖 경로가 모두 0이다. Gate 전후 source·Python 실행 파일 bytes·설치 package·기록한 Python 환경 변수는 동일했다.
- Gate contract digest: `sha256:800b0e5a8b9f3f3bdbac8e7d7eeb7f26c84fb9fb46ecfda7b015b9aa70c2e85a`. Report digest: `sha256:e10f684323682d2ab5c6f921fd3f44e8cf60367b27f13f874e991a2eb9ea190f`. Source digest: `sha256:7f7e96a88fc8bc0ffd3f0b4127a6c8b148a599c14bcf4a876dd4b436888c8e77`.
- 전체 테스트와 최종 fresh 결정론 Gate는 `.flowmarshal-engine-eval/runs/role-config-boundary-dev-20260905-v1/`에 stdout/stderr·실행 환경·종료 코드를 보존한다. `deterministic/qualification-report.json` 및 5개 cell의 실제 결과가 Gate 증거다. 문서에 적힌 경로만으로 PASS를 추정하지 않는다.
- 전체 테스트에는 첫 실패 중단 옵션을 사용한다. 실패 경로 회귀가 출력하는 모의 `status=FAIL`은 unittest 결과와 구분한다. 실제 provider 호출·diagnostics prepare/run·Plan 활성화·Worker 실행은 이 개발 경계에서 수행하지 않는다.

## fresh 실제 검증 조건과 판단

1. 이 개발 결과를 commit/push한 원격 HEAD와 clean을 확인하고, 별도 승인된 새 run root를 사용한다. 기존 R25/R26 thread·turn·raw·checkpoint는 실패 provenance로만 보존한다.
2. 현재 source의 새 결정론 Gate, Python 실행 환경, fixture·고정 기대표·독립 review·prompt·strict schema·주입 지침 snapshot을 호출 전에 결속한다. 이 개발 Gate를 재사용하려면 source뿐 아니라 전체 contract와 실행 환경의 동일성도 입증해야 한다.
3. 후보 파일의 절대 경로를 `prepare --role-config`로 전달한다. fresh App Server inventory에서 general Reviewer Sol/high, expander Luna/high, critical Reviewer Sol/xhigh 및 전체 설정의 선택·fallback 지원을 확인하고 새 v2 lock을 만든다. 과거 inventory를 fresh 관측으로 사용하지 않는다.
4. 실제 turn의 권한·지침·request·schema·receipt·terminal/result 결속과 clean 의미 assessment를 확인한 뒤에만 다음 사례로 진행한다. logical/provider/recovery 상한은 기존 13/13/0을 유지하고 첫 실패에서 중단한다.
5. 새 실제 제한 검증 실행에는 별도 사용자 판단이 필요하다. 이 개발·검증·승인된 commit/push 자체에는 추가 판단이 필요하지 않다. 기존 **S06 FAIL·Functional Alpha 미완료·1.0 NO-GO**는 유지한다.

다른 작업에 메시지·callback·예약을 보내거나 새 작업을 생성하지 않았다. 결과는 이 작업의 최종 응답과 저장소 인계 문서로 제공한다.
