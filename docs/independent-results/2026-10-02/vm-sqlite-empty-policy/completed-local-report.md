# SQLite 초기화 정책 독립 결정 검토

## 1. Executive Verdict

**`conditional_consensus`: v3의 확대는 일관적인 논리 상태 정책이지만, failed partial bootstrap/crash retry에 항상 필요한 유일하거나 가장 작은 규칙은 아니다.**

두 identity가 0이고 전체 `sqlite_master`가 빈 **모든 SQLite container를 지정된 신규 Engine 대상에서 할당한다**는 정책이면 v3는 조건부로 타당하다. 이 정책은 external-empty와 과거 데이터가 DROP된 empty 파일도 받아들인다. 이것을 “우리 bootstrap이 만든 DB만 복구한다”거나 출처 인증으로 설명할 수 없다.

목표가 **새 DELETE-mode bootstrap의 실패 재시도만**이라면, SQLite의 writable recovery와 writer lock 이후 기존 세 조건에 `size=0`을 더하는 더 좁은 후보가 coherent하다. 과거 WAL-before-DDL이 남긴 nonzero residue까지 포함하려면 추가 acceptance가 필요하지만, 모든 logical-empty 상태까지 허용해야 한다는 결론은 나오지 않는다.

이것은 VM root의 정책 결정 입력이다. canonical source 적용, 제품/spec 변경, 새 GO 또는 production 효과를 승인하거나 실행하지 않았다.

## 2. Consensus Proposal

상태: **conditional_consensus**. VM root가 기존 권한 안에서 다음 지원 범위를 기록한다.

| 지원 범위 | 일관적인 정책 후보 | 받아들이는 비용 |
|---|---|---|
| 모든 logical-empty container 할당 | v3 삼중 predicate 유지 | external-empty 및 일부 과거 DROP-history-empty도 채택 |
| 새 DELETE-mode bootstrap retry만 | recovery/lock 이후 삼중 predicate + size0 | 기존 nonzero WAL-empty residue는 보존·거절 |
| 관측된 pristine WAL residue도 retry | 삼중 predicate + schema_version0/freelist0 후보 | 일부 과거 사용 상태 제외, provenance는 여전히 미인증 |

추천은 목표와 가장 좁게 맞는 범위다. fresh-only라면 size0 후보를 우선 검토하고, 논리적 빈 container의 일반 할당을 원하는 경우 v3를 그대로 선택할 수 있다. 추가 header 조건은 작은 중간 대안이며 출처 인증 수단이 아니다. 두 좁은 대안은 **미구현·미qualification 후보**다.

새 인간 승인 gate, marker 또는 framework를 필수로 추가할 이유는 없다.

## 3. Strong Consensus

세 독립 관점과 fresh Judge는 다음 경계를 공유했다.

- 동일한 관측 상태의 external-empty와 rollback-empty를 내용만으로 항상 구별할 수 없다.
- 판정은 recovery/WAL을 반영한 SQLite 상태를 같은 connection의 writer transaction 안에서 확인해야 한다.
- 전체 schema 공백 조건을 table-only 공백으로 완화하면 view 같은 외부 객체를 놓친다.
- zero identity만으로 기존 partial schema를 보충하거나 prototype/schema3/operating DB를 자동 변환하면 안 된다.
- “현재 논리적으로 비어 있음”과 “never-used·우리 소유·과거 bytes 없음”은 다른 주장이다.
- initialization 예외가 schema commit 취소를 뜻하지 않는다. postcommit WAL/artifact 오류에는 완성 Engine DB가 남을 수 있다.

## 4. Material Disagreements

Round1의 “자동 crash retry·추가 영속 상태 없음이면 가장 작은 실용적 정책” 표현은 교차검토에서 한정됐다. **가장 단순한 logical-empty 정책**과 **가장 작은 acceptance set**을 분리해야 한다.

full-byte provenance collision은 그 동일 상태를 다르게 분류할 수 없음을 입증한다. 다른 empty 상태 전체까지 받아야 한다는 의무는 만들지 않는다. 둘을 함께 거절하거나, observable header/history 흔적으로 일부 상태를 제외하는 정책도 coherent하다.

남은 차이는 기술 사실이 아니라 지원 범위 선택이다. 기존 계약은 별도 새 DB와 역사 보존을 요구하지만 unmarked logically-empty 파일의 할당 의미까지 정하지 않는다. 이를 새 수동 승인 의무로 바꾸지 않는다.

## 5. Decision by Axis

| 축 | 결정 | verified claim |
|---|---|---|
| Recovery | transactional bootstrap과 postcommit WAL은 유지. fresh retry와 과거 nonzero residue retry를 구분 | C1, C3, C4, C9 |
| Provenance | external/rollback의 동일 bytes 반례 때문에 항상 구분 불가. state policy로 명시 | C2, C6 |
| Foreign/legacy 보존 | 현재 객체·nonzero identity·committed WAL·intact history/runtime 거절 경계 유지. forensic purity 보장은 없음 | C5, C6, C8 |
| 최소성 | v3는 coherent하나 unique/smallest acceptance 아님. size0 및 추가 header 조건 후보 존재 | C3, C4, C7 |
| 권위·보장 | VM root의 범위 결정 입력. 제품 변경·GO·qualification 판정과 분리 | C8, C9 |

## 6. Evidence

고정 identity:

- Base commit `32bb0f9dd9f024045d24487312b50f5b703573a3`, tree `27e18c0c7853c1d35a98d5437cebf0ba703ed42d`.
- Evidence commit `8a159ed4b79058aba8e6302358733696b33d11ff`.
- [미적용 v3 patch](https://github.com/jaeseongs95/vibemarshal/blob/8a159ed4b79058aba8e6302358733696b33d11ff/docs/independent-results/2026-10-02/vm-sqlite/revision3/atomic-initialize-v3.patch) SHA-256 `40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89`.
- [기존 독립 최종 기술 보고서](https://github.com/jaeseongs95/vibemarshal/blob/8a159ed4b79058aba8e6302358733696b33d11ff/docs/independent-results/2026-10-02/vm-sqlite-review/final/REVIEW-final.md)는 읽었으나 새 policy approval로 재사용하지 않았다.
- [실제 초기화·identity 소스](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/ledger.py#L794), [ADR](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/engine-cutover-adr.md#L14), [D11](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/redesign-1.0-contract.md#L98), [원장·legacy 계약](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md#L361), 적용 AGENTS 실제 본문을 읽었다.

Coordinator의 synthetic 반례는 base의 src를 TemporaryDirectory에 추출하고 정확한 v3 patch를 적용한 뒤 실제 package를 import했다. canonical checkout에는 적용하지 않았다. Python 3.12.14 / SQLite 3.53.1 / Linux에서 다음을 직접 확인했다.

| 반례 | 관측과 의미 |
|---|---|
| 외부 WAL-only와 WAL rollback-empty | 두 전체 main 파일이 4096 bytes로 동일. DB SHA-256 `0ab48b25cba617ed3a4acca0161813b314c095ef63544bb4af60769eb1012977`. provenance 항상 구분 가능 주장을 반증 |
| fresh DELETE vs precreated WAL rollback/exit | DELETE는 recovery 후 0-byte, WAL은 4096-byte; 둘 다 identity0/0·전체 schema empty. fresh-only에 broad acceptance가 필수라는 주장을 반증 |
| cache spill 후 process 종료 | recovery 전 main573440/journal1024 bytes. base readonly precheck는 OperationalError. writer-locked writable recovery 후 main0·identity/schema/header 흔적0 |
| 외부 plain table/row, view-only, sqlite_sequence, nonzero identity/version | v3 거절. 해당 synthetic DB bytes와 상태 보존 |
| committed foreign table/row가 WAL에만 존재 | main4096/WAL12392 bytes 상태에서 v3 거절, main/WAL bytes와 row7 보존 |
| CREATE/INSERT/DROP empty | v3 수용. 다른 secure_delete=OFF 반례에서는 삭제된 synthetic 문자열이 8192-byte 파일에 남음. schema_version2/freelist1 |
| pristine WAL rollback 추가 header | reviewer의 source-derived experiment와 결과를 읽고 Coordinator가 두 synthetic DB를 readonly 직접 조회: schema_version0/freelist0. external pristine WAL도 같음 |

`sqlite_master`는 table뿐 아니라 view/index/trigger의 schema 목록이다. 따라서 전체 공백을 확인하는 조건은 객체 종류를 빠뜨리지 않는 판단이다. [SQLite Schema Table](https://www.sqlite.org/schematab.html)

`BEGIN IMMEDIATE`는 write transaction을 즉시 시작한다. source의 lock 안 판정과 같은 connection 사용을 유지해야 하는 근거다. [SQLite Transaction](https://www.sqlite.org/lang_transaction.html)

`schema_version` 읽기는 안전하고 schema 변경 시 증가하며 VACUUM도 schema 변경으로 취급된다. 이는 좁은 restriction의 근거이지 소유권 증명이 아니다. secure_delete=OFF에서는 삭제 traces가 남을 수 있어 논리 emptiness가 physical-history absence를 보장하지 않는다. [SQLite PRAGMA](https://www.sqlite.org/pragma.html#pragma_schema_version), [secure_delete](https://www.sqlite.org/pragma.html#pragma_secure_delete)

재현 가능한 주요 script:

```sh
PYTHONDONTWRITEBYTECODE=1 python policy_counterexamples.py /path/to/private-vibemarshal
PYTHONDONTWRITEBYTECODE=1 python edge_counterexamples.py /path/to/private-vibemarshal
```

첫 script는 12개의 작은 scope case, 둘째는 hot-journal과 committed-WAL edge 2개다. 반복 회차·중복 suite를 unique test/qualification 수로 합산하지 않았다. 전체 bytes collision과 dropped-history의 SQL 절차는 별도 `auxiliary-evidence.md`에 기록했다. DB·WAL·journal·원자료 transcript는 이 게시용 bundle에 포함하지 않는다.

## 7. Required Actions

VM root의 결정 기록에서 **fresh-only retry**, **pristine nonzero residue recovery**, **모든 logical-empty container 할당** 중 지원 범위를 명시한다. 범위 미정인 채 v3를 “복구에 필수인 최소 변경”으로 기록하지 않는다.

어느 후보든 recovery/lock 뒤 동일 connection의 상태 판정, atomic bootstrap, 현재 foreign/legacy/runtime 거절 경계, postcommit 실패의 올바른 분류를 보존해야 한다. 기존 비원자 bootstrap이 이미 commit한 partial schema는 자동 repair 대상이 아니다.

v3 선택 시 external-empty와 DROP-history-empty 채택 및 provenance/forensic 보장 부재를 설명한다. 좁은 후보를 선택할 때 필요한 구현·해당 범위 검증은 별도 author task의 일이다. 이번 reviewer를 writer로 재사용하지 않았다.

이는 **새 사용자 승인 요구가 아니다**. 기존 정책 결정권과 별도 구현 작업 범위를 설명한 것이다.

## 8. Optional Optimizations

추가 marker/framework 없이 schema_version0/freelist0 restriction을 선택할 수 있다. observed pristine WAL residue를 포함하면서 일부 과거 사용 empty 상태를 제외하지만 그보다 더 넓은 recovery 이력을 지원하려면 false-negative를 검증해야 한다.

명시적으로 미식별 nonzero DB를 거절·보존하고 새 경로를 사용하는 대안도 coherent하다. 자동 retry 범위가 작아지는 비용이 있으며 별도 승인 gate를 요구하지 않는다.

process-local 생성 기억은 crash/restart provenance를 보존하지 못한다. unlink/truncate는 출처 없는 파일과 journal/WAL을 파괴할 수 있다. durable sidecar 또는 temp-publish는 별도의 crash/concurrency/durability 결속이 필요하다. 별도 allocate/recover API도 출처를 증명하지 못한다. 고정 page-size/file-size/journal-mode는 환경·상태별 false-negative를 늘린다. 현재 질문에 이 복잡성을 필수로 추가할 근거는 없다.

## 9. Unresolved

VM root의 지원 범위 선택과 좁은 candidate의 실제 구현/qualification이 남는다. coherent 정책이라는 분석을 모든 Windows/SQLite/Python 조합, 모든 crash 지점, 실제 IOERR·power-loss durability 또는 새 race qualification으로 승격하지 않는다.

기존 v3의 기술 회귀 evidence는 그대로 보존했으며 20-run race benchmark를 다시 돌리지 않았다. 운영 이력·한도 초기화를 피하기 위한 별도 DB 계약도 유지한다. 원시 파일을 같은 OS 사용자가 교체·위조하는 적대적 환경에서 소유권을 보장하지 않는다.

## 10. Method / Run Summary

- 공식 [AGS SKILL](https://github.com/jaeseongs95/agent-governance-suite/blob/ba85fdcf245b9910674d88fffdd125f6f0454027/skills/independent-deliberation-panel/SKILL.md)와 같은 commit의 entry-details/orchestration-policy/role-catalog/evidence-schema/canonical schema 적용.
- **MEDIUM, strict, bounded, cap5**. narrow fixed predicate의 세 failure function: recovery `/root/sqlite_recovery`, provenance/contract `/root/sqlite_contract`, minimality `/root/sqlite_minimal`. 모두 `fork_turns:none` blind Round1과 reviewer당 1회 cross-examination 완료.
- 별도 fresh Judge `/root/sqlite_judge`는 모든 reviewer 완료 뒤 `fork_turns:none`으로 생성됐고 완료했다. Judge에는 검증 claims·제약·issue ledger·cross summary·axes만 주고 raw reviewer output/다른 대화/검증 전 메모는 주지 않았다.
- 모든 실제 child 요청에 **model=gpt-6.1-sol, reasoning_effort=high**를 명시했고 host가 admit했다. fallback 없음. Coordinator의 동일 요청은 부모가 전달한 exposed cloud_threads create/admission evidence로 기록했다.
- advertised availability, request admission, effective 적용을 구분했다. 모든 provider effective model/effort는 **null / NOT_OBSERVABLE**. root의 child canonical names는 도구에서 관찰한 identity이며 provider UUID를 발명하지 않았다.
- `assurance: independent`는 skill 절차상 분리/lifecycle 수준이다. hidden provider internals, platform isolation 또는 실제 effective 설정의 외부 증명이 아니다.
- specialist reserve는 미사용. Stage6 adaptive/re-deliberation 없음. cross-exam reuse3은 새 독립 participant로 세지 않는다. actual distinct worker4.
- **초기 preflight 오류 정정:** Coordinator의 in-thread self-selection capability를 추가 요구해 처음 중단했으나, 부모의 실제 routing clarification으로 그 잘못된 prerequisite를 제거했다. 과거 preflight report/record/hash는 그대로 보존했고 현재 panel artifact와 구분했다. effective 관측을 새 필수 gate로 만들지 않았다.
- canonical schema와 claims/constraint/cross/Judge/model-request/lifecycle semantic invariant 검사 PASS. 이것은 artifact 내부 일관성 검증이며 사실·플랫폼 격리를 기계적으로 증명하지 않는다.
- canonical source/spec/운영DB 변경, 새 GO, GitHub 게시 또는 Library upload 없음. sanitized local private GitHub-ready artifact만 준비했다. hidden internals/credentials/restricted transcripts 접근 없음.

Deliverables: `decision-record.json`, `judge-verdict.json`, `judge-dossier.json`, `capability-preflight.json`, `identity-lifecycle.json`, `source-inventory.json`, probe scripts/결과, canonical schema, artifact manifest. 이 보고서는 raw reviewer/Judge output을 저장한 transcript가 아니라 verified claims와 fresh Judge 판정의 Coordinator 정리다.
