# 기능 Alpha A1 구현·검증 인계

- 완료 단위: A1 — 기준선 기록과 Task·Goal 준비 경계 수정
- 기준일: 2026-09-04 KST
- 판정: A1 결정적 회귀 통과. 기능 Alpha 전체는 미완료이며 1.0은 `NO-GO`다.
- 실행 artifact: `.flowmarshal-engine-eval/runs/alpha-a1-20260903T234342Z`

## 기준선

구현 시작 source digest:

```text
sha256:88f09a0d23bc496887cc93c41e92dee6750558e13b419a3f7d9c17dd96c43ca5
```

이 source의 역할 회귀는 48/48 PASS다. 감사 이후 Planning은 18/18을 완료했고 정상 입력 선택 8/12, 부정 입력 차단 6/6, schema 실패 2건으로 FAIL이다. 최신 E2E는 Task validation 두 개에 `validation_goal`이 섞여 첫 실행 전 실패했다. 원래 보고서·원장·응답은 수정하지 않았다.

원격 보관 전 저장소는 최초 commit이 없는 상태였으며 기존 staged baseline이 있었다. 기존 source 변경과 역사적 artifact는 보존했다. A1 밖의 `context.py`, `service.py`, `runtime.py`는 감사 당시 파일 SHA-256과 일치함을 검사했다.

## 구현 결과

- `execution.py`: Core의 전체 권위 Context와 provider payload를 분리했다. Task에는 현재 계약, 관련 Goal Hard AC·제약·비목표·효과 정책, 직접 선행 Task의 산출물 evidence와 프로젝트 관찰을 전달한다. 전체 Plan과 다른 Task 계약·integration validation을 전송하지 않는다.
- `ProviderExecutionPreparation`은 운영상세 proposal 또는 Context 요청만 허용한다. validation 목록의 ID 중복·누락·추가와 다른 Task ID를 거부하고, Core가 활성 Task의 method·필수 evidence 종류를 결합한다. 수동 `ExecutionSpecProposal` 및 `prepare_task()` 반환 계약은 유지했다.
- Goal Test는 독립 입력·지침·출력 경로를 사용한다. 활성 integration validation의 ID·method·필수 evidence 종류와 불일치하면 거부한다.
- Core operation에는 provider 요청과 별도로 전체 권위 Context digest를 결속했다. 같은 축약 payload라도 권위가 바뀌면 과거 완료 응답을 재사용하지 않는다. 호출 전후 권위·파일 관찰·선행 evidence 변경도 검사한다.
- E2E evaluation contract는 실제 provider 전용 schema와 Task·Goal의 분리된 지침을 digest에 포함한다. 이전 schema·source의 checkpoint는 재사용하지 않는다.

## 검증

검증 완료 source digest:

```text
sha256:2a7014272df88f87746f46f491fec6a27481d0fce288cbb6acad661cd9950476
```

- 관련 경계·qualification 회귀: 39 tests, OK.
- Engine 전체 회귀: 118 tests, OK.
- 최초 GitHub 보관 전 전체 회귀: 409 tests, OK.
- legacy freeze: 40개 파일 PASS.
- 테스트 전후 source digest와 Git index 불변 확인.
- 실제 모델 호출, 전체 역할·Planning campaign과 36-cell 성능 평가는 실행하지 않았다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_engine*.py' -v
```

원시 근거는 실행 artifact의 `engine-tests.log`, `all-tests-before-push.log`, `legacy-freeze.json`, `a1-verification.json`에 있다. 회귀는 감사의 Goal validation 혼입, ID 누락·중복, 권위 필드 주입, 다른 Task binding, 운영 필드와 검사 method 불일치, 준비 중 파일·비전송 권위 변경, 이전 응답 재사용 차단과 평가 schema lock을 포함한다. 합격선과 oracle은 변경하지 않았다.

## 다음 완료 단위

다음은 A2의 필수 Context 보장이다. 최종 선택 뒤 필수 need 충족을 검사하고, 예산 부족은 구조화 Context 요청으로 반환한다. Python symbol의 실제 범위와 whole-file 구분, 4,000-token 상한 제거, 운영 artifact 색인 제외를 함께 처리한다.

A2가 끝난 뒤 A3에서 불변 PromptBundle을 실제 Worker 전송에 연결한다. 현재 Worker Prompt 연결과 Context 예산 누락 문제는 아직 남아 있다. A4·A5의 실제 자연어 bugfix 전체 흐름과 중단·재개는 A1~A3 회귀를 통과한 뒤 수행한다. A1 결정적 성공을 실제 E2E 성공 또는 기능 Alpha 완료로 확대하지 않는다.
