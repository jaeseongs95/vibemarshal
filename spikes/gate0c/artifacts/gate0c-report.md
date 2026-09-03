# FlowMarshal Gate 0C 검증 보고서

- 최종 판정: **NO-GO**
- 생성 시각(UTC): `2026-09-02T04:57:19.922243Z`
- 원장 schema revision: `3`
- 판정 원칙: 하나라도 누락·실패·변조·출처 불일치이면 통과로 추정하지 않음

## 핵심 상황

`FM-0C-1`의 엄격 Runner permission profile이 전역 `AGENTS.md`를 읽지 못해 thread 초기화에 실패했습니다. 보호 범위를 완화하거나 full access로 대체하지 않고 후속 task를 중단했으므로 현재 Gate는 `NO-GO`입니다.

## 독립 검증 결과

| 검사 | 결과 | 설명 |
|---|---:|---|
| `sqlite_integrity` | PASS | SQLite 무결성과 외래 키가 정상입니다. |
| `schema_contract` | PASS | Gate 0C schema revision 3 계약이 갖춰졌습니다. |
| `append_only_guards` | PASS | 모든 권위·evidence 표에 update/delete 차단 trigger가 있습니다. |
| `approved_plan_integrity` | PASS | 승인 PlanRevision·task 정의와 원문 digest가 일치합니다. |
| `authority_row_digests` | PASS | 권위 상태 행의 canonical digest가 모두 일치합니다. |
| `evidence_integrity` | PASS | content-addressed evidence가 원장과 일치합니다. |
| `history_chain` | PASS | append-only history hash chain이 연속적입니다. |
| `prerequisite_gates` | PASS | Gate 0A와 Gate 0B 권위 결과가 계속 GO입니다. |
| `profile_provenance` | FAIL | 필수 profile provenance가 실패·누락됐습니다. |
| `current_host_invariant` | PASS | 최종 엄격 probe는 사용자 config와 sandbox 상태를 변경하지 않았습니다. |
| `historical_host_invariant` | FAIL | 이전 진단 실행에서 사용자 config 변경이 발생했습니다. |
| `task_completion` | FAIL | 승인된 Gate 0C task가 모두 완료되지 않았습니다. |
| `actual_runtime_completion` | FAIL | 실제 세 역할 E2E·Core validation 증거가 없습니다. |

## 실패 근거

### `profile_provenance`

- profile probe status='NO-GO', reason='GLOBAL_INSTRUCTION_UNREADABLE'

### `historical_host_invariant`

- evidence_fm0c1_prior_trust_mutation: 합성 trust entry 5개가 사용자 config에 남아 있음

### `task_completion`

- FM-0C-1: 'failed'
- FM-0C-2: 'blocked'
- FM-0C-3: 'blocked'
- FM-0C-4: 'blocked'
- FM-0C-5: 'blocked'
- FM-0C-6: 'blocked'
- FM-0C-7: 'blocked'
- FM-0C-8: 'blocked'

### `actual_runtime_completion`

- 성공 runtime receipt 누락: planner
- 성공 runtime receipt 누락: runner
- 성공 runtime receipt 누락: validator
- 성공 Core validation receipt가 없습니다.

## Task 상태

| Task | 상태 |
|---|---|
| `FM-0C-1` | `failed` |
| `FM-0C-2` | `blocked` |
| `FM-0C-3` | `blocked` |
| `FM-0C-4` | `blocked` |
| `FM-0C-5` | `blocked` |
| `FM-0C-6` | `blocked` |
| `FM-0C-7` | `blocked` |
| `FM-0C-8` | `blocked` |

## 권장 후속 조치

새 PlanRevision에서 native Windows 직접 실행 대신 disposable VM, WSL 또는 brokered filesystem 중 하나를 선택해 재검증합니다. 사용자 config의 합성 trust entry 정리는 별도 명시적 승인 뒤 정확한 경로만 대상으로 수행해야 합니다.
