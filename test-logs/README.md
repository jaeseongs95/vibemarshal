# VM Cloud 테스트 로그

로그 전용 브랜치: codex/vm-test-logs-20261006.

각 테스트 단위의 모든 반환 로그는 test-unit/vendor/run-id 아래에 보관합니다. 명령별 stdout/stderr, 실행·결과·환경·후보 metadata, 원시 지원 자료, 보고서와 파일 해시를 함께 저장합니다. 공동 실행 로그는 테스트 단위(C3·U2·C5·D3)의 exact test ID 목록으로 연결하고 중복 복사하지 않습니다.

기존 Codex Cloud C3 3건·U2 2건·C5 5건·D3 3건의 raw 반환 디렉터리와 POST 판정을 바이트 그대로 복사했습니다. 준비/dummy/issuance/support 자료는 원래 하위 경로를 유지합니다. 기존 실행의 기록이며 새 시험은 실행하지 않았습니다. 원본 76건 중 scoped 성공 결과 13건, 잔여 63건이며 공식 Task 수용·전체 76건·Goal·qualification PASS를 뜻하지 않습니다. I1은 SOURCE_REVIEW_REQUIRED / NOT_RUN입니다.

Claude 반환물은 같은 테스트 단위의 claude/run-id에 추가합니다. 이미 있는 실행 폴더는 덮어쓰지 않습니다. 실제 session/environment/host·runtime, HEAD/tree/dirty, argv/cwd/start/end/exit와 locator를 기록하고 미제공 값은 UNKNOWN/NOT_VERIFIABLE로 둡니다.

INDEX.json은 원자료 출처와 exact test IDs, SHA256SUMS는 이 저장본의 무결성을 제공합니다. raw 내부의 원래 절대 경로와 checksum은 역사적 provenance로 유지합니다. 복사 위치를 원래 실행 위치나 재실행 가능한 패키지로 간주하지 않습니다. 원자료와 가공본이 다르면 둘의 SHA와 변경 내역을 연결합니다.

비밀키·credential·운영 DB·사용자 profile은 로그 폴더에 넣지 않습니다. 이 브랜치에는 제품 코드 변경·main 병합이 없습니다. 현재 GitHub 저장소는 공개로 관측되었으며 이 로컬 정리 작업에서 원격 게시를 하지 않았습니다.

raw 하위 파일은 원시 기록의 바이트를 보존하는 archive로 취급하여 Git의 텍스트 diff·공백 정규화를 적용하지 않습니다. 원본과의 bytes·SHA256 대조로 검증합니다.
