SQLite fresh DELETE recovery — review-only candidate transfer

source branch: codex/sqlite-fresh-delete-recovery
source commit: 4dae937be993693b78d1dbcd873d499633a08e97
source tree: 8c03f489783175f579c86b699a7b98d634217d19
source parent/baseline: 32bb0f9dd9f024045d24487312b50f5b703573a3
evidence branch: codex/sqlite-fresh-delete-recovery-evidence
evidence parent: 위 source commit. evidence commit은 text/log/manifest만 추가한다.

VM root가 fresh DELETE-mode bootstrap failure/crash retry 범위를 선택하고,
independent delta audit 전 검토용 source/evidence transfer를 명시 승인했다.
이 게시 자체는 technical adoption, source GO, main merge 또는 release가 아니다.
Reviewer01a0f9e7이 predicate/new cases를 별도 audit하며 아직 PENDING이다.
Windows native runner는 그 review 뒤 repin 예정이며 NOT_RUN이다.
Python3.10 runtime과 실제 power-loss/IOERR, support matrix도 NOT_RUN이다.
F07 first-four의 required first main no-ff merge 순서는 그대로다.

구현 소유권: ledger.py와 기존 permanent initialization test file 두 개뿐이다.
v3 대비 제품 delta는 recovered size=0 clause와 주석 두 줄이다. same writable
connection의 native recovery 뒤 BEGIN IMMEDIATE 안에서 app_id0/user_version0/
전체 sqlite_master 공백을 확인한 다음 size0을 요구한다. Engine unlink/truncate,
legacy repair, marker/provenance framework, schema_version/freelist policy gate는 없다.
기존 v3 commit211a5a0 및 evidencefdc5d03, 모든 과거 negative evidence는 보존한다.

비교 파일:
- cumulative-from-32bb.patch: exact baseline32bb 대비 frozen source 전체 두 파일 diff.
- predicate-and-tests-from-v3.patch: exact v3 commit211a5a0 대비 predicate/new regression delta.
- sealed-candidate-manifest.json: source/blob/patch SHA256와 author checks, NOT_RUN.
- sealed-author-report.txt: 2026-10-02T00:40 완료 시점의 historical author handoff.
  당시 pushed=false/no-publish 및 delta review 전 publication 비승인 문장은 당시 상태다.
  frozen source의 Publication trailer도 역사 기록이다. 이후 VM root가 review-only transfer를
  별도 명시 승인했으므로 이번 게시에는 충돌하지 않는다. 해당 원문을 재작성하지 않았다.
- commands.jsonl: original raw receipts 중 panel 전체 docs 목록과 report-print 두 기록만 제외.
  argv/cwd/UTC/elapsed/exit/stdout/stderr는 선택한 원 row 그대로다.

16 checks는 이 writer의 local authored unittest method 16개다:
기존11 + 새5, 단 한 번의 source suite 실행 PASS. independent audit/Windows qualification
수를 뜻하지 않는다. frozen v3 predicate에 새 거절 method 하나를 memory에서 적용한
5 subcase FAIL(expected exit1)도 그대로 보존하며 unique test 수에 더하지 않는다.
게시를 위해 source tests, panel probes나20-run benchmark를 반복하지 않았다.

Evidence sanitation:
original allowlisted file bytes를 sealed SHA256SUMS와 대조하고 credential/key/token
패턴·개인 home 경로·허용 suffix를 검사했다. source patches는 frozen exact bytes다.
게시물은 필요한 text/log/manifest/repro script만 포함한다. Git bundle/압축 packet,
DB/WAL/SHM/journal, runtime profile, credentials, local personal data, native transcripts,
전체 panel 폴더, hostname/boot ID status readback은 제외했다. 제품 코드의 필드 이름과
synthetic fixture marker를 실제 runtime profile/운영 데이터로 해석하지 않는다.
기본 runtime/Python/SQLite 버전과 sandbox cwd는 author check 재현 근거로 보존했다.
요청 모델/effort gpt-6.1-sol/high와 실제 provider 관측null은 분리돼 있다.
Library 사용이나 업로드는 없다.

Source/review/frozen predecessors:
https://github.com/jaeseongs95/vibemarshal/commit/4dae937be993693b78d1dbcd873d499633a08e97
https://github.com/jaeseongs95/vibemarshal/commit/211a5a0f5891a44f217aa7aae18bdb8825775878
https://github.com/jaeseongs95/vibemarshal/commit/fdc5d03bf58c9521ff06ab475aea01d1650a4269
https://github.com/jaeseongs95/vibemarshal/blob/d1b68f837d5e14698f090160502856ce67974502/docs/independent-results/2026-10-02/vm-sqlite-empty-policy/REPORT.md

SHA256SUMS는 이 evidence의 각 파일을 결속한다. remote push 뒤 fresh bare fetch로
source commit/parent/tree, evidence parent/tree, 모든 artifact 및 비교 patch bytes를
검증해 부모에게 별도 반환한다. source와 evidence를 독립 review 동안 동결한다.
