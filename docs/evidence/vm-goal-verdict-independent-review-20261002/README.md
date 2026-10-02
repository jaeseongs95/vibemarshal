# Goal verdict 독립 검토 역사 evidence 패킷

parent 검토 후 private evidence branch용으로 준비한 reviewer 작성 자료다. 제품 파일은 바꾸지 않는다. parent는 original candidate의 exact-latest-ID 규칙이 최소 입증 결함 수정 밖이라는 구분을 수용했으며 author의 새 frozen revision을 기다린다.

- 원본 검토: [REVIEW.md](REVIEW.md), [review.json](review.json). 원본 bytes를 변경하지 않고 역사 판정으로 보존한다.
- author에게 전달할 최소 반례·required assertions: [COMPATIBILITY.md](COMPATIBILITY.md).
- 정확한 실행 argv/cwd/UTC/timing/exit/raw hash: [commands.jsonl](commands.jsonl).
- 고정 input identity·full diff: [candidate-identity.raw.txt](candidate-identity.raw.txt), [candidate-diff.raw.txt](candidate-diff.raw.txt).
- 실제 문서/source 원문 확인: [input-audit.raw.txt](input-audit.raw.txt).
- 원본 inventory/hash 목록: [inventory.json](inventory.json), [SHA256SUMS](SHA256SUMS). 이것은 original review bundle allowlist이며 새 README/COMPATIBILITY는 별도 publication-inventory.json에 포함한다.
- source provenance에 나온 `/workspace`·`/tmp` 경로와 opaque IDs는 reviewer의 disposable synthetic source/archive/fixture만 가리킨다. DB·fixture directory·운영 profile·keys·provider/native transcript는 이 packet에 없다.

검토 대상 구현 commit은 `98bb122970f82bb5fcef122003ee10c7eb787cf4`, tree `1bcc8378c86f30aca46fd757545f07d458d65dd3`, direct base `32bb0f9dd9f024045d24487312b50f5b703573a3`이다. 원본 author artifact input은 `d6956481a964f1b84fb88baa03218a3034f8978d`이다. reviewer task는 `01a0f9e2-ed1d-7296-815a-14270ae95034`이며 author 참여자·결론·실행 결과를 독립 증거로 사용하지 않았다. requested `gpt-6.1-sol/high`, provider-observed null/null을 그대로 보존한다.

원본 source-tree Core/수동 CLI 결과는 public provider E2E·실제 wheel 설치·native Windows·release qualification이 아니다. D1 Windows 경계를 변경/우회하지 않았다. 이 packet 준비 때 테스트를 추가 실행하지 않았다. branch 준비는 merge/source GO/release 승인이나 원격 publish 권한 확대를 뜻하지 않는다. 기존 로컬 bundle은 그대로 유지하며 parent의 명시 publish 지시 전에는 push하지 않는다.
