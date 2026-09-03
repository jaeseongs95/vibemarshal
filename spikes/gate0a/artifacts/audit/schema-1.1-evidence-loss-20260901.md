# Gate 0A schema 1.1 증거 손실 감사 기록

## 결론

`gate0a-readiness-v3`의 schema 1.1 결과 JSON과 보고서는 schema 1.2로 다시 분류되는 과정에서 별도 원본 보관 없이 덮어써졌다. 현재 workspace에서는 당시 원문 바이트와 SHA-256을 복구하지 못했다.

이 기록은 누락된 원본을 추정해 재작성하지 않는다. 확인 가능한 사실과 확인할 수 없는 부분을 분리해 남긴다.

## 확인된 사실

- 변환 전 판정은 전체 `SETUP_REQUIRED`, `0A-R = GO`, `0A-P = SETUP_REQUIRED`였다.
- 변환 후 판정은 전체 `SETUP_REQUIRED`, `0A-R = PENDING-INTEROP-CHECK`, `0A-P = SETUP_REQUIRED`다.
- 재분류 이유는 Desktop sidebar 표시만으로는 양방향 read/resume·project grouping·동시 접근을 입증하지 못했기 때문이다.
- 변환을 수행한 Codex task는 `01a05ce6-1d24-7422-b5f2-a26e0b049211`, turn은 `01a05cec-b9aa-7012-b7bf-87ef03cb5c15`다.
- `legacy` 폴더에는 schema 1.0 원본만 남아 있다.

## 확인할 수 없는 사실

- schema 1.1 결과 JSON의 정확한 원문 바이트와 SHA-256
- schema 1.1 보고서의 정확한 원문 바이트와 SHA-256

따라서 당시 `GO` 판정은 역사적 사실로만 기록하며, 현재 Gate 통과 증거로 사용하지 않는다.

## 재발 방지

앞으로 `write_outputs`가 현재 schema보다 오래된 결과를 발견하면 변환 전에 다음을 수행한다.

1. 결과 JSON과 보고서 원문을 content hash가 포함된 이름으로 그대로 보관한다.
2. 원본 SHA-256, byte 수, 이전 판정, 이전·새 schema와 보관 경로를 migration receipt에 기록한다.
3. 같은 원본을 다시 처리해도 충돌 없이 동일 보관본을 사용한다.
4. 현재 schema 결과에는 migration history와 별도 증거 감사 레코드 목록을 포함한다.
