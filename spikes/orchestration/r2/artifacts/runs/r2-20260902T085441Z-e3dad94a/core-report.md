# FlowMarshal R2 Core 원장 축소·정리

- 판정: **GO**
- 실행 ID: `r2-20260902T085441Z-e3dad94a`
- 시작: `2026-09-02T08:54:41.404004Z`
- 완료: `2026-09-02T08:54:41.812200Z`

## 검사 결과

| 검사 | 상태 | 설명 |
|---|---|---|
| `direct_plan_activation` | pass | HMAC proof 없이 정확한 revision digest를 CLI에서 활성화함 |
| `derived_ready_state` | pass | ready를 저장값이 아니라 dependency 완료 상태에서 계산함 |
| `attempt_intent_binding_evidence` | pass | Attempt·intent·thread/turn binding·validation evidence와 crash 격리를 보존함 |
| `reduced_core_schema` | pass | 새 DB는 오케스트레이션 table만 가지며 authority/access table을 만들지 않음 |
| `historical_artifacts_unchanged` | pass | Gate 0B/0C artifact를 마이그레이션하거나 덮어쓰지 않음 |

## Core 원장

- DB: `D:\codex\flowmarshal\spikes\orchestration\r2\artifacts\runs\r2-20260902T085441Z-e3dad94a\core.sqlite3`
- Project: `project_25197ff073e84a9599d5a86f67a09d09`
- PlanRevision: `revision_bea0b6316df24273bb985e2380ff2577`
- Plan digest: `sha256:3b696e19e9c41d7ff56db869196e36335635ae8216aba9400ecac4415aa4d86c`

Assignment의 모델 ID는 원장 복사·보존을 시험하기 위한 합성값이다. 실제 모델 catalog 기반 선택은 R4 범위다.

## 범위

R2는 새 SQLite Core의 도메인 전이와 보존 성질을 합성 receipt로 검사한다. 실제 Codex Runner lifecycle은 R1에서 이미 검증했으며 이 실행은 새 task를 만들지 않는다.
