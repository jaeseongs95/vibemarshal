# FlowMarshal 1.0 재설계 인계

현재 인계는 FM-01의 **문서 계약 정합화**다. 실제 제품 구현·검증·1.0 전환은 미완료이며 [승인 계약](redesign-1.0-contract.md)과 [구현 로드맵](pre-1.0-roadmap.md)을 따른다. 기존 Core 자산을 유지하고 새 계약은 planned로 표시했다. 원장의 태스크 상태는 이 파일에서 판정하거나 갱신하지 않는다.

## 실행 대상과 권위

- 현재 변경 대상: `D:/codex/flowmarshal`의 `main` 브랜치 main checkout. 1.0 릴리스 완료까지 모든 파일 변경·커밋을 이 위치에서만 수행한다. 다른 브랜치·worktree·detached HEAD에서는 작업하거나 커밋하지 않는다. 릴리스 완료가 확인되면 이 한시 규칙은 만료되고 당시 Git 작업 지침을 따른다.
- 필수 입력의 과거 source: `D:/codex/fm-performance-floor`, 승인 당시 기준 `a5bf2bf7a21a0dfe7ffe2e4bb34e8ae67808da08` / `codex/performance-release-floor`. 현재 쓰기 대상이 아니며 최신 명시 지시에 따라 main 상태와 직접 대조한다.
- 승인 원문: `D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/approved-plan.md`, SHA-256 `a24eb860c8b603f8edc43a71370c6d8638cc53d3c5c49b8a568c44fc9f5b1742`.
- 구현 조율 원장: `D:/codex/fm-inspection-runtime/performance-release-floor-20260907/orchestration-status.sqlite3`. 제품 Engine 원장과 별개다.
- 조회 helper: `D:/codex/자동화템플릿/scripts/flowmarshal_implementation_workflow.py --db <조율 원장> task --task-id <ID>`는 read-only다. worker는 완료 상태를 직접 변경하거나 다음 앱 task를 생성하지 않는다.
- 역할 cwd나 자료 저장 위치만으로 source를 추정하지 않는다. 이번 main 지정은 사용자의 최신 명시 승인에 근거한다. main의 무관한 상태, `D:/codex/fm-recovery`, 동결 source·실제 DB·receipt·History는 보존한다.

source·template·실제 작업 프로젝트의 AGENTS는 최신 명시 승인과 함께 적용한다. `D:/codex/자동화템플릿/AGENTS.md`의 승인 source_root 보존 원칙도 과거 경로를 고정하는 근거로 사용하지 않는다. template의 main 한시 규칙은 현재 지시와 일치한다. 과거 source 지침의 detached preflight·자동 push 조항은 현재 main-only·push 금지를 대체하지 못한다. main 밖 source/template 파일은 입력으로 보존하고, 제품의 장기 원칙은 main AGENTS와 권위 문서에 반영한다. 조율 스크립트·원장·다른 작업 상태의 수정은 FM-01에 포함하지 않는다.

최신 명시 승인에 따라 목표·범위·효과·운영 정책을 GoalAuthorization으로 승인하고 내부 Plan revision은 Core가 자동 활성화한다. 정확한 Plan ID·digest의 사용자 수동 입력은 필수가 아니다. usage 누락은 null/unknown으로 보존하고 유효 terminal 결과의 후속 진행을 막는 전역 조건으로 사용하지 않는다. 외부 효과가 미확정이면 기존 intent/binding을 먼저 관측한다. 비교 성능은 비차단 보고다.

## 후속 구현에 전달할 책임

[M01 설계별 연결표](redesign-1.0-contract.md)에 승인 1~12 전 항목, [V01·V02](redesign-1.0-contract.md)에 필수 검증 임계값·E2E 책임과 FM 태스크를 연결했다. 다음 배정은 조율 원장의 정확한 immutable 명세를 읽어 수행한다. FM-01은 문서 계약을 검증하므로 schema 4·GoalAuthorization·supervisor·새 CLI·패키지·qualification의 구현/검증 완료 여부를 판정하지 않는다. 미구현·미검증 책임은 planned로 유지한다.

실제 동작과 새 계약의 차이는 FM-02~FM-10 구현, FM-11 harness, FM-12~FM-14 독립 검증, FM-15 감사에서 해소한다. 최초 실제 역할48/Planning18은 공통 계약 변경에 맞춰 새로 실행한다. 과거 51개와 1,056개 검사를 새 실행으로 합치지 않는다. 깨끗한 non-editable install과 E2E의 read_only·local context·usage missing·재시작·응답 유실·timeout·stale·partial resume·cancel·실행 중 replan을 모두 확인한다.

로컬 task는 첫 파일/명령 전에 실제 danger-full-access·approval_policy=never를 확인한다. Fast·증거 없는 재시도·임의 fallback·원격 push·공개 배포·외부 메시지·삭제·인증/권한 변경은 하지 않는다. 종료 시 실제 evidence와 report를 남기고 assignment/spec/dispatch digest에 결속한다.

## 보존된 이전 실행

[승인 이전 인계 원문](pre-1.0-handoff-before-redesign-1.0.md)은 원본 bytes 그대로 보존했다. 그 안의 실제 Plan/Goal/Attempt/validation ID, 실패·사용량·운영 보정과 명령은 당시 실행의 provenance다. old usage 회수→static11→qualification13→performance36→exact Plan12 절차는 현재 재개 명령이 아니다. v2 채택 평가를 기본 v1의 필수 선행조건으로 만들지 않는다.

이전 평가·R3.1 보고서·fixture·원장·receipt는 수정하지 않는다. 미측정량은 null, 과거 FAIL은 FAIL로 유지한다. [과거 구현 현황](engine-implementation-status-before-redesign-1.0.md), [반복 검증](pre-1.0-iterative-validation.md)은 새 1.0 PASS 근거가 아니다.
