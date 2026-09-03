# FlowMarshal Gate 0B 검증 보고서

- 판정: **GO**
- 내부 schema revision: `1`

## 검사 결과

### sqlite_integrity: 통과

SQLite 무결성과 외래 키가 정상입니다.

### schema_contract: 통과

Gate 0B 내부 schema revision 1 계약이 갖춰졌습니다.

### plan_invariants: 통과

PlanRevision 단일 활성화·digest·dependency 불변식이 정상입니다.

### execution_invariants: 통과

Attempt·intent·slot·write lease 중복 방지 불변식이 정상입니다.

### recovery_invariants: 통과

unknown 효과가 격리되고 복구 lineage가 보존됩니다.

### access_invariants: 통과

추가 입력은 읽기 전용이며 승인 nonce와 입력 drift가 통제됩니다.

### evidence_invariants: 통과

완료 판정에 필요한 content-addressed evidence가 모두 유효합니다.

### history_chain: 통과

append-only history hash chain이 정상입니다.

### state_attestations: 통과

권위 상태의 최신 attestation이 원시 행과 일치합니다.

### database_guards: 통과

상태 전이·append-only·unknown 차단 DB guard가 설치됐습니다.
