# VibeMarshal (FlowMarshal) 저장소 결함 분석 및 병렬 무충돌 태스크 분해(Parallel Non-Colliding Task Decomposition) 보완 보고서

## 1. 분석 목적 및 핵심 관점

VibeMarshal(FlowMarshal Engine)의 가장 핵심적인 목적은 **하나의 큰 요청(Goal)을 검증 가능한 여러 태스크(Task DAG)로 쪼개고, 그 태스크들을 병렬로 개발하더라도 파일·산출물·리소스 충돌이 발생하지 않도록(disjoint write scope / single-writer ownership) 분해·검증·실행하는 것**이다.

본 문서는 Agent Governance Suite(AGS) 워크플로우(`plan_workflow` → `start_workflow` → `record_stage_result` → `finalize_workflow`) 절차에 따라 저장소를 전수 점검하여 확인한 **병렬 태스크 분해 및 무충돌 보장 경계의 구조적 결함**과 **AGS 거버넌스 연동 결함**, 그리고 이에 대한 코드 보완 및 검증 결과를 정리한다.

---

## 2. 핵심 결함 분석: 병렬 태스크 분해 및 충돌 방지 경계

### 2.1 [결함 P-1] `skeleton_gate` 및 `plan_gate`의 병렬(비의존) Task 산출물 충돌 미검출
- **위치**: [`src/flowmarshal/engine/planning.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/planning.py) (`skeleton_gate`, `plan_gate`), [`src/flowmarshal/engine/domain.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/domain.py) (`PlanSkeletonCandidate`, `PlanContractDefinition`)
- **현상 및 원인**:
  - 기존 `PlanSkeletonCandidate.graph_is_consistent`와 `PlanContractDefinition.plan_graph_is_consistent`는 DAG의 순환 여부(`_assert_acyclic`)와 `data` dependency의 producer/consumer 집합 포함 관계만 검사했다.
  - `skeleton_gate` 또한 `DISCONNECTED_CONSUME`(소비 산출물이 dependency edge로 연결되지 않음)만 검출할 뿐, **선후 dependency 경로가 전혀 없는 두 병렬(concurrent) Task($T_A \not\prec^* T_B \land T_B \not\prec^* T_A$)가 동일한 `produces` 항목을 동시에 생성·수정하는 충돌**을 차단하지 않았다.
- **영향**:
  - Planner가 전체 작업을 여러 Task로 쪼갤 때 선후 의존성 없이 같은 산출물 키나 모듈 결과물을 두 Task의 `produces`에 중복 배정하더라도 결정적 Gate(`skeleton_gate`, `plan_gate`)를 그대로 통과하여 병렬 개발 시 덮어쓰기(write-write race) 또는 책임 모호성을 유발한다.
- **해결 내역**:
  - [`src/flowmarshal/engine/planning.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/planning.py)에 DAG 유향 도달 가능성(`_reachable_pairs`) 기반의 `parallel_task_product_collisions` 함수를 구현했다.
  - `skeleton_gate`와 `plan_gate` 모두에서 선후 의존 경로가 없는 병렬 Task 쌍이 동일한 `produces` 산출물을 공유하면 즉시 `PARALLEL_TASK_PRODUCT_COLLISION` (`GateName.PLAN`, `remediable=True`) finding을 발생시켜 후보를 차단하고 정제(refinement) 시 산출물 범위를 분리하거나 명시적 dependency로 직렬화하도록 강제했다.

### 2.2 [결함 P-2] Planner 역할 지침(`planner_roles.py`) 내 병렬 무충돌 분해 규칙 부재
- **위치**: [`src/flowmarshal/engine/planner_roles.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/planner_roles.py) (`SkeletonGeneratorAdapter`, `SkeletonReviewerAdapter`)
- **현상 및 원인**:
  - Skeleton 생성·정제·검토 프롬프트 지침에 AC 기여도(`contributes_to`)와 검증 경계는 상세히 기술되어 있었으나, 정작 **"전체 작업을 여러 Task로 분해할 때 병렬 실행 가능한 Task들은 산출물(`produces`)과 쓰기 범위(write scope)가 겹치지 않도록(disjoint write targets, single-writer ownership) 쪼개야 하며, 동일 대상을 수정하는 Task 쌍은 반드시 dependency로 직렬화해야 한다"**는 핵심 분해 원칙이 명시되어 있지 않았다.
- **해결 내역**:
  - [`src/flowmarshal/engine/planner_roles.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/planner_roles.py)에 `PARALLEL_TASK_DECOMPOSITION_INSTRUCTIONS` 상수를 정의하고 `SkeletonGeneratorAdapter.generate`, `SkeletonGeneratorAdapter.refine`, `SkeletonReviewerAdapter.review` 지침에 결합하여 LLM 계획 수립 및 검토 단계부터 병렬 무충돌 분해 원칙을 준수하도록 보강했다.

### 2.3 [결함 P-3] `materialize_execution_spec`의 병렬 Task 간 실제 파일 쓰기 대상(`resolved_targets`) 충돌 미검증
- **위치**: [`src/flowmarshal/engine/service.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/service.py) (`EngineService.materialize_execution_spec`)
- **현상 및 원인**:
  - Skeleton/Plan 단계에서는 논리적 산출물(`produces`) 단위로 태스크를 나누더라도, Ready 시점에 구체화되는 `TaskExecutionSpecRevision`의 `resolved_targets`에서 두 병렬 Task가 우연히 동일한 실제 파일 경로(`path`)에 대해 변경 권한(`access in {"write", "create", "delete"}`)을 선언할 수 있다.
  - 기존 `materialize_execution_spec`은 단일 Task 내부의 target 존재 여부와 해시만 확인했을 뿐, 동일 활성 Plan 내의 다른 비의존(병렬) Task가 이미 고정한 `ExecutionSpec`의 쓰기 경로와 겹치는지 대조하지 않았다.
- **해결 내역**:
  - [`src/flowmarshal/engine/service.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/service.py)의 `materialize_execution_spec` 트랜잭션 내부에서 활성 Plan의 dependency DAG(`_reachable_pairs`)를 계산하여, 선후 의존 경로가 없는 다른 Task의 현재 `ExecutionSpec`과 `write`/`create`/`delete` 대상 경로가 하나라도 겹치면 즉시 `PARALLEL_EXECUTION_TARGET_COLLISION` 오류로 차단하도록 구현했다.

---

## 3. 거버넌스 및 런타임 연동 결함 분석

### 3.1 [결함 G-1] AGS 플러그인 루트 `host-integration.json` 누락 (해결 완료)
- **위치**: `C:\Users\Lenovo\.gemini\config\plugins\agent-governance-suite\host-integration.json`
- **현상**: FlowMarshal의 [`governance_conformance.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/governance_conformance.py)는 플러그인 루트의 `host-integration.json` 매니페스트(`agent-governance-suite.host-integration.v1`)와 4개 진입점(`mcp-server`, `scope-baseline`, `scope-compare`, `acceptance-cli`)의 `executionClosure`를 검증하나, 설치된 플러그인 루트에 해당 파일이 누락되어 있었다.
- **조치**: 플러그인 루트에 4개 진입점과 19개 실행 클로저 파일 목록을 정확히 결속한 `host-integration.json`을 생성하여 매니페스트 계약 검증을 충족했다.

### 3.2 [결함 G-2] A2 프로파일 전환기 서명(`signed=True`) 검증과 `GovernancePlugin.call` 간의 불일치
- **위치**: [`src/flowmarshal/engine/governance_conformance.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/governance_conformance.py) (`GovernancePlugin.call`, `_verify_signed_result`)
- **현상**: `governance_conformance.py`는 A2 전환 중(`_A2_IN_TRANSITION = True`)에 `signed=True` 호출 시 의도적으로 `a2_profile` 불일치를 발생시켜 Fail-Closed로 동작한다. 실 플러그인 E2E 자격 검증(qualification)을 통과하려면 AGS MCP 서버의 A2 Ed25519 서명 응답 형식과 `_verify_signed_result`의 검증 계약을 최종 정렬해야 한다.

### 3.3 [결함 G-3] 최종 보고서(`reporting.py`)의 거버넌스 감사 요약 출력 보강 필요
- **위치**: [`src/flowmarshal/engine/reporting.py`](file:///d:/gemini/바이브머셜/src/flowmarshal/engine/reporting.py) (`FinalReportBuilder.render_final`)
- **현상**: `FinalReport` 도메인 모델에는 `governance_executions` 및 `governance_audit` 필드가 존재하나, `render_final()` 마크다운 출력에서 해당 거버넌스 실행 요약이 생략되어 있어 향후 CLI 최종 보고서 출력 시 보강이 권장된다.

---

## 4. 단위 테스트 및 결정적 검증 결과

- **검증 명령**:
  ```powershell
  $env:PYTHONPATH="src"
  D:\codex\build\subagents-validation\python-package-smoke-env\Scripts\python.exe -m unittest `
    tests.test_engine_planning `
    tests.test_engine_ledger_service `
    tests.test_engine_role_adapters `
    tests.test_engine_plan_inspection `
    tests.test_engine_plan_inspection_v2
  ```
- **추가된 핵심 검증 케이스 ([`tests/test_engine_planning.py`](file:///d:/gemini/바이브머셜/tests/test_engine_planning.py))**:
  1. `test_parallel_tasks_with_disjoint_products_pass_skeleton_and_plan_gates`: 산출물(`produces`)이 서로 겹치지 않는 병렬 Task 분해(`task_one` → `result:module_a`, `task_two` → `result:module_b`)는 `skeleton_gate`와 `plan_gate`를 모두 정상 통과함을 검증.
  2. `test_parallel_tasks_with_overlapping_products_are_rejected_unless_serialized`: 선후 dependency가 없는 병렬 Task들이 동일 산출물(`result:shared_artifact`)을 생성하면 `skeleton_gate`와 `plan_gate`에서 모두 `PARALLEL_TASK_PRODUCT_COLLISION`으로 차단되며, 명시적 dependency(`task_one -> task_two`)로 직렬화하면 충돌이 해소됨을 검증.
  3. `test_materialize_execution_spec_rejects_parallel_write_target_collision`: 논리적 산출물이 다르더라도 Ready 시점 `ExecutionSpec`에서 두 병렬 Task가 동일 파일(`app.py`)에 `access="write"`를 요청하면 `PARALLEL_EXECUTION_TARGET_COLLISION`으로 거부되고, 서로 다른 파일(`app.py` vs `other.py`)을 수정할 때는 정상 materialize됨을 검증.
