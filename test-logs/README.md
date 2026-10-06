# VM Cloud 테스트 로그

원격 증거 브랜치: evidence.

각 테스트 단위의 모든 반환 로그는 test-unit/vendor/run-id 아래에 보관합니다. 명령별 stdout/stderr, 실행·결과·환경·후보 metadata, 원시 지원 자료, 보고서와 파일 해시를 함께 저장합니다. 공동 실행 로그는 테스트 단위(C3·U2·C5·D3)의 exact test ID 목록으로 연결하고 중복 복사하지 않습니다.

기존 Codex Cloud C3 3건·U2 2건·C5 5건·D3 3건의 raw 반환 디렉터리와 POST 판정을 바이트 그대로 복사했습니다. 준비/dummy/issuance/support 자료는 원래 하위 경로를 유지합니다. 기존 실행의 기록이며 새 시험은 실행하지 않았습니다. 원본 76건 중 scoped 성공 결과 13건, 잔여 63건이며 공식 Task 수용·전체 76건·Goal·qualification PASS를 뜻하지 않습니다. I1은 SOURCE_REVIEW_REQUIRED / NOT_RUN입니다.

Claude 반환물은 같은 테스트 단위의 claude/run-id에 추가합니다. 이미 있는 실행 폴더는 덮어쓰지 않습니다. 실제 session/environment/host·runtime, HEAD/tree/dirty, argv/cwd/start/end/exit와 locator를 기록하고 미제공 값은 UNKNOWN/NOT_VERIFIABLE로 둡니다.

INDEX.json은 원자료 출처와 exact test IDs, SHA256SUMS는 이 저장본의 무결성을 제공합니다. raw 내부의 원래 절대 경로와 checksum은 역사적 provenance로 유지합니다. 복사 위치를 원래 실행 위치나 재실행 가능한 패키지로 간주하지 않습니다. 원자료와 가공본이 다르면 둘의 SHA와 변경 내역을 연결합니다.

비밀키·credential·운영 DB·사용자 profile은 로그 폴더에 넣지 않습니다. 이 브랜치에는 제품 코드 변경·main 병합이 없습니다. GitHub 저장소는 공개이며, 사용자가 테스트 단위별 Cloud 작업물을 evidence 브랜치에 게시하도록 승인했습니다.

raw 하위 파일은 원시 기록의 바이트를 보존하는 archive로 취급하여 Git의 텍스트 diff·공백 정규화를 적용하지 않습니다. 원본과의 bytes·SHA256 대조로 검증합니다.

각 codex 실행 폴더의 source-packet에는 원본 runner·manifest·테스트 소스·helper와 기존 증명 자료를 보관합니다. CLOUD-INPUT-PINS.json은 해당 실행 invocation의 inputs_before와 동일한 bytes/SHA임을 기록합니다. cloud-return/return.zip에는 원래 반환 ZIP을 보관합니다. runner·guard·소스는 역사적 증거이며 여기서 실행하지 않습니다.

테스트 목적: C3은 입력 계약·출력·경로, U2는 schema migration·writer fence, C5는 evidence·history·carry·revalidation, D3는 direct selection의 identity·binding·stale/tamper 거절을 확인한 기존 Cloud 실행입니다. exact test IDs는 각 TEST-UNIT.json과 INDEX.json에 연결됩니다.

2026-10-06 추가 보관: 알려진 과거 자료 305개 lineage를 대조하여 원본 바이트 사본 303개와 cursor 필드만 제외한 JSON 가공본 2개를 연결했습니다. 검토 전에 멤버를 제외했던 ZIP 3개는 가공본으로 유지하고, 해당 멤버의 구조 검토를 마친 원본 ZIP 3개를 별도로 보관했습니다. `/page/nextCursor` 2곳은 내부 바이너리 의미가 확인되지 않아 가공 사본에서만 삭제했으며, 원본은 로컬에 보존했습니다. 운영 credential을 발견하거나 삭제했다는 뜻은 아닙니다.

과거에 로컬 미회수로 기록됐던 Cloud 원본 11개(2,554,954 bytes)는 기존 Cloud 세션에서 실제 회수했습니다. 반환 chunk·ZIP·멤버의 SHA와 기존 pin 11개를 대조한 원본 사본과 반환 ZIP을 테스트별 `historical-source-*-recovered-evidence-20261006`에 보관합니다. 최종 native manifest reference는 관측하지 못했으며, 실제 관측한 업로드 영수증과 11개 reference의 파일 본문으로 복원했습니다.

각 `C3·U2·C5·D3/codex/historical-archive-20261006/PUBLICATION-CURRENT.json`은 원본·가공본·회수 자료의 현재 연결 정보를 제공합니다. 초기 제외·미회수 문서는 당시 상태를 담은 기록으로 남겼습니다. 같은 SHA의 서로 다른 lineage를 추가 테스트나 고유 파일 수로 계산하지 않습니다. 이번에 관측한 범위는 알려진 로컬 305개 lineage와 추가 회수 원본 11개이며, 관측하지 못한 Cloud 전체 자료의 완전성은 주장하지 않습니다.

논리적인 Codex Cloud 세션은 REUSED입니다. container·environment·boot freshness는 UNKNOWN이며, 개별 guard 작업 루트의 FRESH 관측과 구분합니다. PRE·synthetic·NOT_RUN·UNKNOWN 기록은 새 제품 PASS가 아닙니다. 신규 테스트 실행은 0건이고 I1 실제 후보 SOURCE/PRE, Windows·Git/CLI·qualification·Goal의 HOLD는 유지합니다.
