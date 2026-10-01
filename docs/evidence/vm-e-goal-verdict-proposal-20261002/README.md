# VM E Goal verdict draft proposal — 독립 검토 패킷

이 branch는 draft proposal이다. 독립 승인·integration·F07 source GO·release GO를 뜻하지 않는다. parent가 별도 reviewer를 배정한다. 구현 commit은 검토 중 동결한다.

- Repository: private `jaeseongs95/vibemarshal`
- Proposal branch: `dot/vm-goal-verdict-proposal-20261002`
- 구현 commit: [98bb122970f82bb5fcef122003ee10c7eb787cf4](https://github.com/jaeseongs95/vibemarshal/commit/98bb122970f82bb5fcef122003ee10c7eb787cf4)
- Direct base: `32bb0f9dd9f024045d24487312b50f5b703573a3`
- 구현 tree: `1bcc8378c86f30aca46fd757545f07d458d65dd3`
- 구현 immutable diff: [32bb0f9dd9f024045d24487312b50f5b703573a3 → 98bb122970f82bb5fcef122003ee10c7eb787cf4](https://github.com/jaeseongs95/vibemarshal/compare/32bb0f9dd9f024045d24487312b50f5b703573a3...98bb122970f82bb5fcef122003ee10c7eb787cf4)
- 구현 범위: `src/flowmarshal/engine/service.py::record_goal_verdict`(+34/−13), `tests/test_engine_goal_verdict_authority.py`(+129). artifact commit은 `docs/evidence/vm-e-goal-verdict-proposal-20261002/` 아래 파일만 추가한다.

## 재현 근거

이전 완료 shard의 최종 기준 source 회귀 8개 중 제안 oracle의 negative 6개가 실패하고 positive 2개가 통과했다. candidate targeted 42개가 통과했다. 게시 작업에서는 broad tests를 반복하지 않았다. 같은 candidate를 다시 만들거나 rebase하지 않았다.

원본 sanitized [report.json](report.json), [report.txt](report.txt), [commands.jsonl](commands.jsonl), [negative baseline raw](reviewed-base-regressions.raw.txt), [candidate 42 raw](reviewed-candidate-42.raw.txt), [원 proposal diff](proposal.patch)를 포함한다. 원시 파일 bytes와 SHA-256을 보존한다. report.txt의 “push 없음”은 최초 완료 시점의 역사 기록이며 이 draft branch 게시 사실은 이 패킷이 따로 기록한다. 당시 command cwd·UTC·wall time·exit·raw SHA-256은 commands.jsonl에 있다.

중간 실패·harness 오류와 imported TestCase가 추가 발견된 실행도 보존하며 최종 결과로 합산하지 않는다. `adversarial-shard.raw.txt`의 Dispatcher 기반 9개 사례는 D1 POSIX guard에서 목적 assertion에 도달하지 못했다. 실패를 PASS로 보정하지 않는다.

## 규범 근거와 해석

원문은 모두 direct base `32bb0f9dd9f024045d24487312b50f5b703573a3`에 고정했다. 정확한 원문·문서 SHA-256·line range·immutable link·구현 지원 근거는 [normative-sources.json](normative-sources.json)에 있다. 문서 근거와 기존 구현의 일관성 근거를 구분한다.

| 사례 | 정확한 규범 출처 | 적용과 reviewer 판단 범위 |
|---|---|---|
| VM-E-01 integration 최신 FAIL/INCONCLUSIVE 무시 | [제품 설계 §8 line 370](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L370), [AGENTS line 72](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/AGENTS.md#L72) | 필수 integration evidence 확인·Core 원장 권위에서 현재 반증을 무시하지 않는다고 해석. 최신 행 선택 자체는 기존 dispatcher의 지원 근거다. |
| VM-E-02 completed Task 최신 FAIL/NOT_RUN 무시 | [1.0 계약 D02 line 21](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/redesign-1.0-contract.md#L21), [제품 설계 §8 line 370](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L370) | D02는 원본·중간 Task의 늦은 실패·미확정 결과를 재사용 계보의 현재 Goal에 반영하라고 명시한다. 새 직접 Core 회귀가 그 원칙을 원본 Task 자체에 적용하는 범위는 reviewer가 판단한다. |
| VM-E-03 이전 integration PASS ID 수락 | [제품 설계 §3 lines 62–65](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L62-L65), [§8 lines 370·378](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L370-L378) | **정확히 최신 PASS ID 집합과 같아야 한다는 문장은 없음.** 기존 [dispatcher lines 7141–7154](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/runtime.py#L7141-L7154)에 맞춘 결속 강화 제안이다. 유효한 과거 PASS 재사용을 차단하는지 독립 검토가 필요하다. |
| VM-E-04 superseded Plan 제출이 current Plan 종료 | [1.0 계약 D01 line 11](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/redesign-1.0-contract.md#L11), [제품 설계 §2 lines 29–36](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L29-L36), [§3 line 65](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L65), [§4.1 line 96](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L96) | 활성 Plan 권위와 supersede·원본 보존에서 역사 제출물이 현재 Plan을 종료하는 새 권위가 될 수 없다고 적용. |

## NOT_RUN과 경계

- TrustedTestEngineService/기존 EngineServiceFixture·Core service API의 합성 검사다. public vertical E2E 결과가 아니다.
- D1 Windows 전용 runtime을 보존했다. 새 POSIX runtime/owner-lock 우회 adapter를 구현하지 않았다. 막힌 9개 assertion의 전체 이름은 report.json의 NOT_RUN에 있다.
- 별도 Validator Attempt에 terminal job이 없거나 Worker에 다른 model명만 붙은 입력은 거부됨을 검사했다. 실제 Windows semantic terminal provider/thread/turn/status/digest/reuse 경로는 NOT_RUN이다.
- validation command는 synthetic stub/명시 거부다. synthetic DB/fixture만 사용하며 운영 DB·profile·keys·live provider·native transcript가 없다.
- conditional dependency `condition`은 선언 API에 없으므로 extra-field 거부만 확인했다. 조건 분기 실행 PASS를 주장하지 않는다.
- 실제 concurrent process race/process fault와 whole public vertical은 다른 worker 범위다. 기존 118 portable tests와 SQLite init/CAS/WAL campaign을 blanket 반복하지 않았다.
- requested model/effort `gpt-6.1-sol/high`; provider-observed model/effort는 `null/null`(노출된 turn별 receipt 없음). fallback 0, 추가 provider calls 0.
- F07 첫 4개 source GO 0, producer3 NOT_ASSIGNED, D7은 F07 merged 이후 대기 조건을 유지한다. main·tag·GO·Library는 이 게시 범위가 아니다.

## 게시 전 outgoing-file 검토

신규 구현 두 파일은 source·회귀 코드다. 이 artifact 묶음은 합성 fixture의 unittest 결과, 로컬 git identity/diff, 직접 만든 harness, command provenance와 원문 근거만 포함한다. 새 DB·fixture directory·운영 이력·profile·credential·실제 provider receipt/session/native transcript는 넣지 않았다. 모든 파일은 명시 allowlist에서 읽고 hash를 계산했으며 credential pattern·private-key·native transcript·운영 DB 형식과 binary/symlink 여부를 검사했다. inventory와 검토 범위는 [publication-inventory.json](publication-inventory.json)에 있다. 이 검토는 outgoing bytes 공개 범위 검토이며 구현의 독립 승인이 아니다.
