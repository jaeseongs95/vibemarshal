# 합성 add 프로젝트

현재 구현은 `app.py`의 `add(left: int, right: int) -> int` 하나다. 공개 import 경로는 `from app import add`이며 두 정수의 합을 반환해야 한다. `test_app.py`의 `test_add_returns_sum`은 `add(2, 3) == 5`를 검사한다.

검증에는 Python 표준 실행기만 필요하며 프로젝트 디렉터리에서 다음 명령을 사용한다. 현재 구현의 뺄셈 때문에 이 검사는 실패한다.

```text
python -c "from test_app import test_add_returns_sum; test_add_returns_sum()"
```

모듈 구조 개편 전략을 제안·비교하는 요청은 이 단일 모듈을 출발점으로 삼는다. 새 모듈 배치는 기존 외부 시스템의 사실이 아니라 제안할 설계 선택이며, 공개 import 경로와 호출 계약의 보존 방법을 함께 설명한다. 사내 API 계약·운영 계정·삭제 대상에 관한 자료는 이 프로젝트에 없다.
