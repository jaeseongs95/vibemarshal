# Task 산출물 책임의 입력 설명

## 원인과 변경 범위

[9차 static 11](inspection-v2r9-source-requirements.md)은 모두 통과했지만, 별도의 [S06 첫 구간](inspection-s06-reentry.md)은 clean의 `produces` 항목만으로 Worker의 미래 Validator 결과 제출을 추정해 중단됐다. AC 관계 28개와 참조 전개는 정확했다. 새 실패를 참조 adapter 결함이나 모델 호출 장애로 재분류하지 않는다.

현재 Task의 `produces`는 검증까지 포함한 Task 전체의 논리적 산출물 목록이다. 같은 product key가 있어도 완료 조건과 validation 문장이 Worker의 선제 제출을 요구하는지에 따라 순서 충돌 여부가 다르다. key 자체에서 작성 주체를 추정하는 것이 이번 오판의 원인이라는 가설을 적용한다.

v2 Expander·Reviewer 입력에 `task_result_field_semantics`를 추가했다. 세 필드의 공통 책임을 한 곳에서 정의해 각 요청에 복사한다. 반복 Task 행·새로운 관계 장부를 모델에게 요구하지 않으며 개별 product의 작성 주체를 프로그램이 추정하지 않는다. Reviewer는 필요한 시점·주체를 명시한 계약 원문과 이후 결과를 만드는 검사의 입력을 함께 읽고 직접 finding을 선택한다.

출력 schema, 기존 Goal·Plan 객체, v1 요청, citation·target catalog와 compiler·evaluator를 변경하지 않았다. 특정 evidence 이름의 finding을 삭제하거나 원본 Plan·완료 조건·고정 기대표를 보정하지 않는다. 입력 설명 자체는 finding evidence가 아니며 전체 request digest에 결속한다.

## 검증과 다음 완료 조건

집중 회귀 28개가 통과했다. 새 회귀는 `produces`만 인용한 잘못된 finding이 입력 설명 때문에 제거되지 않고 고정 clean 기대표에서 계속 FAIL인지 확인한다. 기존 adapter 회귀에서는 원본 Task·produces 보존과 필드 설명 변조 시 기존 receipt 재사용 거부도 확인한다. 이 검사는 모델의 의미 정확도 개선을 증명하지 않는다.

결정적 Gate도 `2026-09-06T02:47:25.340360Z`에 5/5로 통과했다. contract는 `sha256:d551ecb2f6270f9604ae83f047bc19157570f43b7864e3c87f611ccac355f261`, report SHA-256은 `5204a4d02c37011ef5d1f1d76c00b8c8d5c3e51eb17a2f8c282e242e7556d086`다. artifact는 `.flowmarshal-engine-eval/runs/inspection-v2r10-task-result-devgate/deterministic`에 있다.

사용자가 검증단 보정 반복을 지적하고 다른 대화와 탐색 알고리즘을 비교하도록 요청해 새 static·qualification 모델 호출은 시작하지 않았다. 이 입력 설명은 의미 개선을 아직 확인하지 않은 후보로 보존한다. [검색 방향 재검토](inspection-search-direction-review.md)의 코드 재현에서 상세 Plan 실패 피드백 경로의 빈틈을 확인했으며, 다음 우선순위는 이 탐색 루프와 실제 Goal 경로의 연결이다. 9차와 첫 S06의 raw·PASS·FAIL·미실행 기록과 승격 gate는 유지한다.
