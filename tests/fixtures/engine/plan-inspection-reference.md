# 단일 bugfix의 고정 검증 자료

이 자료는 S05에서 실행 전에 고정한 검증 계약의 운영 참고자료다. 실제 Goal 정규화 결과, Skeleton, Plan 후보나 선택 결과를 제공하지 않는다. Task 개수·분할·DAG·ID는 Planner가 제안한다. 아래 명령은 실행 준비 단계에서 사용할 기존 검사 도구이며 계획 생성 단계에서 실행할 의무가 아니다.

## 현재 프로젝트와 검사 범위

- 작업 복사본: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\s05-bugfix-trace-20260904\workspace`
- Python: `D:\codex\flowmarshal\.venv\Scripts\python.exe`
- 이미 구현·검증된 외부 검사 도구: `D:\codex\flowmarshal\tests\fixtures\engine\bugfix-trace\oracle.py`
- 초기 보존 파일은 `app.py`, `test_app.py`, `AGENTS.md` 세 개다. Git 메타데이터와 Python bytecode만 파일 집합 비교에서 제외한다. Engine DB·artifact는 workspace 밖에 둔다.
- 수정은 작업 복사본의 `app.py` 안 `add` 구현으로 한정한다. 원본 fixture와 검사 도구는 수정 대상이 아니다. 새 프로젝트 파일이나 의존성을 추가할 필요가 없다.

## Task 검사

기존 도구를 `--phase task --workspace <작업 복사본>`으로 실행하면 현재 프로젝트 파일 집합, 테스트·AGENTS.md 초기 byte 해시, add 본문 밖 AST, 실제 함수 annotation·시그니처와 기존 unittest를 직접 검사한다. Python 실행 시 `-B`를 사용한다. 실제 unittest는 workspace에서 `python -B -m unittest discover -s . -p test_app.py -v`로 새 프로세스에서 수행한다.

Task에는 직접 `file`, `diff`, `command`, `test` evidence와 실행 역할과 분리된 Validator의 `model_review`가 필요하다. 최소 변경 의미는 직접 file/diff/test evidence를 받은 독립 Validator가 검토하며, oracle 종료 코드만으로 이 의미 검사를 대체하지 않는다.

## 모든 Task 검증 뒤 독립 Goal Test

같은 기존 도구를 `--phase goal --workspace <동일 작업 복사본>`으로 새로 실행한다. Task 단계 검사를 다시 수행하면서 아래 정수 쌍 각각을 위치 인자와 키워드 인자로 호출하여 결과를 비교한다.

| left | right | 결과 |
|---:|---:|---:|
| 2 | 3 | 5 |
| 0 | 0 | 0 |
| -2 | -3 | -5 |
| -5 | 8 | 3 |
| 7 | 0 | 7 |
| 0 | -9 | -9 |
| 1000000000000 | -1 | 999999999999 |

공개 계약은 `add(left: int, right: int) -> int`이며 이름·annotation·위치 및 키워드 인자 호출을 유지한다. 검사 실행 도중 소스가 바뀌어도 실패한다. 정답 diff나 특정 구현 문자열은 강제하지 않는다.

Goal 검증은 `evidence_mode=independent`와 새 `command`, `test`, `file`, `diff` evidence를 요구한다. Goal evidence의 `task_id`는 null이며 Task evidence를 복사하거나 이름만 바꿔 쓰지 않는다. Core가 최신 State·Goal binding·최종 GoalVerdict를 별도로 결속한다. 고정된 합격 기준은 모든 검사 통과이며 초기 결함 상태에서 oracle 종료 코드 1은 예상되는 실패다.
