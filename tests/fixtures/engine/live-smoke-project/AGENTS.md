# 합성 Planning Fixture

- 이 디렉터리는 FlowMarshal Engine의 읽기 전용 planning smoke 입력이다.
- 역할 모델은 파일을 수정하거나 명령을 실행하지 않고 구조화된 계획 후보만 반환한다.
- 위 읽기 전용 제약은 현재 계획 생성 호출에 적용한다. 향후 Plan 승인 뒤 Task의 변경 범위는 사용자의 Goal에 따른다.
- 공개 함수 `add`의 기존 두 정수 계약을 보존한다.
