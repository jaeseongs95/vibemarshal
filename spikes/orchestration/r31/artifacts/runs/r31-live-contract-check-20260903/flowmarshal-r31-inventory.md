# FlowMarshal R3.1 구현 표면 inventory

- `src/flowmarshal/planning/r31_domain.py`: Mission, candidate, quality report, selection과 session hint 계약.
- `src/flowmarshal/planning/r31_role_adapters.py`: structured model 역할 adapter와 독립 review 경계.
- `src/flowmarshal/planning/r31_pipeline.py`: Generate → deterministic validation → Hard Gate → Top-K → refine 상태 흐름.
- `src/flowmarshal/planning/r31_search.py`: balanced-mvp-v0 점수 계산, diversity dedupe와 추천 선택.
- `src/flowmarshal/planning/r31_store.py`: immutable JSON artifact, idempotency key, 최신 outcome pointer 저장.
- `tests/test_planner_r31_*.py`: domain, pipeline, search, role adapter와 smoke 회귀 테스트.

구현 계약:
- R3 Core DB schema와 Core 활성화 경계는 변경하지 않는다.
- `PlanningSearchPipeline.search`는 candidate artifact와 model-call receipt를 저장하고, 추천은 하되 Core PlanRevision을 활성화하지 않는다.
- immutable artifact 기록은 동일 digest 재사용, 충돌 거부, 부분 기록 후 receipt/outcome 재대조가 가능해야 한다.
- WorkItem assignment는 후속 Assigner 책임이므로 Planner 후보에서는 `null`을 유지한다.
- 실행 가능한 검증 capability는 `python-unittest`; 명령은 `.venv\Scripts\python.exe -m unittest discover -s tests -q`다.
- 계획 단계 validation의 runtime 성공을 주장하지 않고 command, 입력, assertion, 기대 결과와 evidence 경로만 정의한다.
