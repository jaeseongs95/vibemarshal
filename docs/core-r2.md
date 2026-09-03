# FlowMarshal R2 Core 원장 축소·정리

- 상태: **GO**
- 완료일: 2026-09-02
- 실행 증거: [R2 Core 보고서](../spikes/orchestration/r2/artifacts/runs/r2-20260902T085441Z-e3dad94a/core-report.md)
- 구조화 증거: [R2 Core receipt](../spikes/orchestration/r2/artifacts/runs/r2-20260902T085441Z-e3dad94a/core-receipt.json)

## 결과

Gate 0B의 원장 자산 가운데 실제 오케스트레이션에 필요한 부분을 새
`flowmarshal.core` 제품 경로로 분리했다. 새 Core는 다음을 권위적으로
기록하고 전이한다.

- Project와 불변 PlanRevision
- WorkItem DAG와 dependency에서 계산되는 `ready` 상태
- 실행·검사 model/effort가 복사된 Attempt
- 외부 호출 전에 저장되는 RuntimeActionIntent
- Codex thread·turn·cwd·instructionSources binding
- append-only validation evidence와 hash-chained History
- receipt 없는 실행 중 외부 효과의 `unknown` 격리

새 경로는 `HumanControlAuthority`, HMAC proof, `AccessGrant`를 import하거나
요구하지 않는다. 사용자가 검토한 정확한 PlanRevision digest를 CLI·UI·API
중 한 경로에서 활성화하는 행위 자체가 계획 승인이다.

```text
PlanDraft + 확정 Assignment
  → activate_plan(revision_id, expected_digest, source)
  → active PlanRevision
  → dependency 충족 WorkItem을 ready로 계산
  → Attempt + CREATE_THREAD intent 원자적 예약
  → thread/turn binding
  → validation evidence
  → 완료 또는 원인별 종료·복구 대기
```

## 코드 경계

| 경로 | 책임 |
|---|---|
| `flowmarshal.core.domain` | 새 제품의 Project·PlanRevision·WorkItem·Assignment·Attempt 계약 |
| `flowmarshal.core.ledger` | 별도 SQLite schema, 불변·append-only trigger, History 검증 |
| `flowmarshal.core.service` | 활성화, ready 계산, Attempt/intent/binding/validation 상태 전이 |
| `flowmarshal.core.smoke` | R2 합성 lifecycle과 과거 artifact 불변성 재검증 |
| `flowmarshal.compat.gate0b` | HMAC authority와 AccessGrant가 포함된 과거 Gate 0B API의 명시적 호환 진입점 |

`import flowmarshal.core`는 Gate 0B application, authority 또는 SQLite adapter를
로드하지 않는다. 과거 구현 파일과 API는 역사적 테스트 재현을 위해 유지하되,
새 제품 코드의 dependency graph에서는 제외했다.

## 새 SQLite schema

| 범주 | 테이블 |
|---|---|
| 계획 | `projects`, `plan_revisions`, `work_items`, `work_item_dependencies` |
| 실행 | `attempts`, `runtime_action_intents`, `runtime_bindings` |
| 판정·감사 | `evidence_records`, `validation_results`, `history_events` |
| 식별 | `schema_meta` |

다음 Gate 0B 테이블은 새 DB에 생성되지 않는다.

- `authority_uses`
- `access_requests`
- `access_request_decisions`
- `access_grants`
- `protected_paths`

프로젝트 파일, `AGENTS.md`와 사용자가 고른 외부 참고자료는
`context_sources`로 정상 등록한다. 이는 파일별 보안 허가증이 아니라 Planner와
Worker에 전달할 입력 출처 기록이다.

## 강제되는 불변조건

- revision 내용, WorkItem 정의와 dependency는 생성 후 직접 수정·삭제할 수 없다.
- 새 계획은 기존 revision을 고치는 대신 parent를 가진 새 revision으로 만든다.
- 한 프로젝트에는 active revision 하나와 active Attempt 하나만 존재할 수 있다.
- 외부 효과 전에 intent를 원장에 먼저 저장한다.
- 예약된 intent는 Attempt 종료 시 함께 취소하며, 실행 중 intent는 일반 실패 처리로
  덮지 못한다.
- 실행 중 intent가 receipt 없이 남으면 자동 재실행하지 않고 `unknown`, Attempt는
  `external_unknown`, Project는 `recovery_required`로 전이한다.
- History, evidence와 validation 결과는 UPDATE·DELETE할 수 없다.
- DB에는 `schema_id`, `schema_revision`, SQLite `application_id`를 함께 기록한다.
  기존 Gate DB나 외부 DB를 새 Core 경로로 열면 쓰기 연결 전에 거부한다.

## 검증 결과

R2 스모크는 합성 Project와 두 WorkItem을 사용했다. 첫 작업 완료 전에는
`foundation`만 ready였고, 검증 완료 후 `integration`이 ready가 됐다. 모델·추론
수준, thread/turn ID, cwd, instructionSources와 evidence가 Attempt에 연결됐으며,
두 번째 작업의 실행 중 intent는 crash 상황에서 `unknown`으로 격리됐다.

- 새 Core 단위·통합 검사: 10개 통과
- 저장소 전체 회귀 검사: 154개 통과
- SQLite `integrity_check`: `ok`
- History hash chain: 유효
- 과거 Gate 0B/0C artifact: 510개 파일, 실행 전후 집계 digest 동일
- 과거 artifact 집계 digest: `sha256:ff35f779cf37fc878b558b5457bf959e56ec98514f29bfdf28efb3aa7d69359f`

R2의 model ID는 Assignment 보존을 확인하기 위한 합성값이다. 실제
`model/list` 기반 선택은 R4 범위이며, 실제 Codex Runner lifecycle은 R1에서 이미
검증했다.

## 남은 범위

R2는 여전히 제품 Core 프로토타입이다. Planner, 실제 model inventory 기반
Assigner, 사용자용 activation/dispatch CLI, 실제 진행 동기화와 recovery 해소
명령은 아직 없다. 다음 단계 R3에서 RequestSpec을 WorkItem DAG 후보로 만드는
Planner와 deterministic coverage 검사를 구현한다.
