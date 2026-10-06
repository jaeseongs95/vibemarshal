# C5 r03 — 감사 거절 관측 보정 SOURCE 후보

원 M/helper70f5/legacy3 bytes, ID5/순서/원 assertion/oracle, 원76 inventory/status를 r01에서 그대로 재사용했다. R030은 r01 SOURCE STATIC PASS만이며 새 r03 SOURCE/PRE/실측은 NOT_RUN이다. R030의 현황표·Root proof 미독과 부분 독립성 한계를 유지한다.

audit 허용조건은 그대로다. 각 명시적 거절은 작업 전 count를 증가시키고 새 run-root의 CreateNew effect-denials.jsonl에 sequence/PID/UTC/event/고정 이유만 write+flush한다. 원 인자·target path·import name은 journal에 적지 않는다. 로깅 오류/재진입은 logging_errors로 남기며 RuntimeError 거절을 유지한다. 정상0건도 빈 journal이 필수다.

child result에는 원 unittest 필드와 unittest_exit, 독립 거절 count/logging_errors, 수용용 suite_exit를 함께 넣는다. 거절은 예외를 원 테스트가 잡아도 suite_exit1이며 I/O 오류는2다. result를 먼저 CreateNew 저장한 뒤 journal close/read와 postflight를 수행한다. 뒤늦은 I/O 오류/불일치는 postflight/부모 재독에서 UNKNOWN이며 raw result를 덮어쓰지 않는다.

부모는 journal의 존재/regular-file/UTF-8 완결행/정확필드/순번/PID·카운터를 다시 확인하고 child의 postflight 상태 및 native 결과를 대조한다. 일치한 CLEAR0만 PASS, 확인된 DENIED는 FAIL, 누락/손상/I/O/불일치는 UNKNOWN이다. 원 unittest 결과가 없는 사전 uncaught refusal은 기존 child-error.json의 PID/counters를 journal과 결속해 FAIL로 보고하되 unittest_observation_unknown과 원 missing-result 오류는 보존한다. 손상된 result가 있으면 child-error로 우회하지 않는다.

기존 origin14 journal/provenance, private SQLite URI/path, module/import/socket/process/ctypes/chdir 조건, source before/after, owned child1/retry0/동일≤300초 wait-kill-reap 예산은 유지한다. native timed_out/exit는 그대로 기록하며 denial evidence가 없으면 UNKNOWN이다. stderr의 C5 문자열 유무를 gate로 쓰지 않는다. OS sandbox/full native coverage 보장은 없다.

이번 실제 수행은 AST/compile 메모리·hash·정적 diff·봉인/ZIP 검사뿐이다. product/import/loader/candidate runner/dummy/Cloud 실행0이다. 새 SOURCE 검토 후 기존 Cloud actor의 PRE 자료에 정상0거절, RuntimeError를 잡아도 count가 남는 C1, journal I/O 오류 UNKNOWN 실측을 연결해야 한다. 이 문서는 그 dummy를 실행하거나 결과를 합성하지 않았다.

ORIGINAL76-INVENTORY.json과 ORIGINAL76-STATUS.ko.md는 r01 당시의 정적 선택/완료 근거 snapshot bytes다. 새 journal/판정 변경은 SOURCE.DIFF.patch·manifest.json·STATIC-CHECKS.json을 기준으로 읽는다. 원76/currenthelper10a/Windows/F6/qualification/Goal PASS로 확대하지 않는다. 기존 r01/이전후보/r01 UNKNOWN 및 운영물은 보존한다.

r03은 r02의 timeout_residual 설명 한 곳이 새 UNKNOWN 우선 판정과 충돌하던 부분을 맞췄다. 후보 schema 식별자만 함께 바꾸었으며 schema literal을 정규화한 runner 전체 AST는 r02와 같다. r02의 원12 파일과 봉인·ZIP을 보존했다. r02도 실행·SOURCE 승인 근거가 아니며 최종 검토 대상은 새 r03이다.
