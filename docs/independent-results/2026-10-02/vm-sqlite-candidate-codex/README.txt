SQLite 초기화 v3 — 동결된 draft review candidate

이 evidence branch는 source commit 211a5a0f5891a44f217aa7aae18bdb8825775878 바로 뒤에 evidence만 추가한다.
source branch: codex/sqlite-atomic-initialize-v3
source tree: e901b77ebf7551342ada996dd7ee4e8422b92f7a
source parent: 32bb0f9dd9f024045d24487312b50f5b703573a3
source 변경은 src/flowmarshal/engine/ledger.py와 tests/test_engine_ledger_initialize.py
두 파일이다. candidate.diff.patch는 위 parent 대비 source commit의 immutable 전체 diff다.

게시 권한은 이 draft review candidate와 검토·정제한 evidence에 한정한다.
빈 zero-identity/schema SQLite DB 허용 정책은 독립 decision panel 대기다.
source GO, Windows qualification, merge, deployment 또는 release 승인이 아니다.
Windows native process/owner-lock 및 Python 3.10 runtime은 NOT_RUN이다.
Python 3.10 AST 검사와 Linux spawn 관측을 위 qualification으로 승격하지 않는다.
requested model/effort는 gpt-6.1-sol/high이고 authoritative provider 관측은 null이다.

sealed-report.txt와 sealed-candidate-manifest.json은 2026-10-01T23:45 시점의
봉인 관측 원문이다. 그 안의 pushed=false, publication NOT_RUN, no-push 및
parent publication approval 대기 문장은 당시 상태다. 이번 게시 승인은 그 뒤에
받았고 remote publication 결과는 부모에게 반환하는 별도 검증 기록에 남긴다.
과거 실패/미실행/정책 대기 관측을 성공으로 다시 쓰지 않았다.

신규 회귀 최종 11 method와 기존 관련 6 method PASS. 초기 10 method 실행,
baseline sensitivity 첫 loader ERROR와 수정 후 baseline FAIL도 commands.jsonl에
그대로 남겼으며 반복·subcase를 qualification test 수로 합산하지 않았다.
commands.jsonl은 원본 기록 중 source preflight/apply/test/commit 검증 34개를
선별한 view다. 각 row의 argv/cwd/UTC/elapsed/exit/stdout/stderr는 원본과 같다.
원본의 관련 없는 branch 목록, 중복 문서 본문, bundle 생성/검증 및 report-print
기록은 이 view에서 제외했다. EARLY-OBSERVATIONS.txt의 exact UTC 미관측도 보존한다.

증거 선택·민감정보 검토:
- 보존 원문 파일은 기존 SHA256SUMS와 byte/digest를 대조했다.
- credentials/key/token 패턴과 개인 home/사용자 경로를 검사하고 selected text/log
  내용을 확인했다. 실제 credentials, runtime profile, 개인 데이터, native transcript는 없다.
- Git bundle, tar packet, cloned .git/history, DB/WAL/SHM, profile/config, credential,
  transcript 파일은 allowlist 밖이며 게시하지 않는다.
- 코드에 있는 schema/profile 필드 이름은 제품 source diff의 일부이며 runtime profile이 아니다.
- 사용자 제공 source thread ID와 합성 Codex author는 승인된 writer 추적 정보로 보존한다.
- Library를 사용하지 않는다. source branch와 source commit은 독립 review 동안 동결한다.

공식 AGS guidance 원문 위치(설치·수정 없음):
https://github.com/jaeseongs95/agent-governance-suite/blob/ba85fdcf245b9910674d88fffdd125f6f0454027/skills/ponytail/SKILL.md
선행 private evidence/review:
https://github.com/jaeseongs95/vibemarshal/blob/8a159ed4b79058aba8e6302358733696b33d11ff/docs/independent-results/2026-10-02/vm-sqlite-review/final/REVIEW-final.md

repro helper의 절대 /workspace 경로는 당시 sandbox 경로다. 재현 시 source root에서
PYTHONPATH=src 또는 baseline runner에는 PYTHONPATH=src:.를 사용한다. 모든 DB는
TemporaryDirectory의 synthetic fixture이며 provider calls는 0회다.

게시 전 검증 보충:
보존한 patch artifact의 빈 context 줄은 unified diff의 단일 공백 prefix다.
전체 outer diff --check는 이를 trailing whitespace로 보고 exit=2였다. raw patch
bytes/digest를 바꾸지 않고 .patch artifact만 제외한 outer diff --check 및 각 patch의
reverse apply --check --whitespace=error-all로 검사했으며 모두 exit=0이다.
SHA256SUMS 첫 실행은 repository root cwd에서 상대 파일명을 찾지 못해 exit=1;
evidence directory cwd에서 재실행한 결과 모든 항목 OK, exit=0이다.
publication-preflight.jsonl에는 이 실패 및 수정 관측을 포함한다. source tests는
바뀌지 않아 재실행하지 않았다. remote 결과는 push 뒤 별도 검증해 반환한다.
