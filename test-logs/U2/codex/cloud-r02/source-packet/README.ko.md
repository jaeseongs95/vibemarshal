# U2 next-u64-r02 — decorator 출처 검증 최소 보정

상태: SOURCE 후보 작성. 제품 runner/import/loader/시험/fixture 재실행0, SOURCE/PRE/GO 없음. 기존 r01 UNKNOWN 원자료·후보, C3 r03와 probe는 그대로 보존한다. 원본3파일/원2 ID/SQL/UDF/oracle/setUp/tearDown·private SQLite·child1/retry0/300 wait-reap·UNKNOWN/TIMEOUT·URI 거절 경계는 변경하지 않는다.

## 실제 관측과 원인 판별

r01 POST.INVENTORY.json4147B/d11a5d1f0b4c8abc31b8a396add28257af1d421122d0e58e294e309ef89c8466 및 raw22파일 핀을 직접 확인했다. loader는 원2 IDs/count2/errors[]였고 child2949는 terminal exit2/UNKNOWN이다. child-error와 stderr는 runner168의 helper code origin mismatch만 기록한다. preflight/result/postflight는 없으며 TextTestRunner.run 전에 멈췄다. 당시 실패한 함수명/실제 co_filename은 기록되지 않았으므로 실제값은 UNKNOWN이다. 제품2 실패/PASS로 기록하지 않는다.

원 pinned helper70f5와 runner를 AST/본문으로 대조했다. 검사 순서의 command_migrate,command_begin,command_verify는 decorator가 없고 네 번째 open_readonly만 helper741의 @contextmanager를 사용한다. open_write/backup/fence도 decorator가 없다. Python3.12 contextlib.contextmanager는 functools.wraps를 적용한 내부 helper를 반환하며, 노출 함수의 code는 contextlib.py, __wrapped__의 code는 원본 helper파일이다. r01은 둘을 구분하지 않고 노출 code filename을 원본 경로와 비교한다. 정상 표준 wrapper도 거절하는 검증기 결함이 소스상 확인됐다. 당시 실제 객체가 이 형태였다는 개별 관측과는 구분한다.

## 최소 보정과 provenance

새 checked_helper_origin은 전체7개 출처 검사를 유지한다. open_readonly에만 helper.contextmanager의 실제 표준 함수 identity를 확인하고, 단일 __wrapped__ generator에 같은 runtime의 contextmanager를 적용해 reference wrapper를 만든다. generator 본문을 호출하지 않는다. 실제 wrapper의 code object/globals/defaults/closure cells가 reference와 같고 closure가 바로 그 원함수에 결속돼야 한다. inspect.unwrap의 반환도 같은 원함수여야 한다. wrapper chain·임의 decorator·filename 허용 목록은 받지 않는다.

최종 원함수의 globals는 실제 helper.__dict__, name/code.co_name은 요구한 함수명, co_filename은 고정 helper 경로여야 한다. 다른6개 함수는 직접 provenance로 검사한다. 잘못된 원파일·원 globals·wrapper code·closure·decorator는 계속 거절한다. 검사 순서와 동일 객체를 유지하여 실제 type/code/name/globals 결속·wrapped 출처와 기대 source를 helper-origin.jsonl에 guard 전에 쓰고 flush한다. 수용/거절 행도 기록하며 거절 후 logging 실패가 원 guard 예외를 덮지 않는다. 성공한7개 provenance는 preflight.helper_code_origins에도 저장한다. 이전 r01 raw는 수정하지 않는다.

입력3원본의 hash/origin 검증을 줄이지 않았고 stdlib wrapper를 전역 허용하지 않는다. 새 PRE는 실제 Linux contextlib.py/inspect.py·기타 stdlib/native·실행 파일/원문 명령을 고정해야 한다. Python 반사 검사는 OS sandbox나 악의적 native process 전체 격리 증거가 아니다.

## 판별 회귀와 한계

regression/check_origin.py는 고정 runner에서 checked_helper_origin 및 provenance 기록/utc의 AST 함수4개만 추출하여 실행한다. 전체 runner나 제품/원시험 module을 import·실행하지 않는다. 별도 dummy_helper.py의 표준 decorator만 정의하며 dummy 함수/generator 본문도 호출하지 않는다.

정확11회귀는 정상 plain, 정상 contextmanager(r01 direct code 검사 거절 확인), 잘못된 plain origin, 진짜 wrapper+잘못된 원 origin, 기대 filename을 가진 가짜 wrapper code, __wrapped__ 위조와 다른 closure, 같은 filename/다른 원globals, 다른 decorator import binding, 순환 __wrapped__, 코드 없는 객체, 정상 functools.wraps 중첩 chain을 구분한다. 앞2는 수용, 뒤9는 거절해야 한다. 정상 중첩은 inspect.unwrap으로 원함수에 도달하는 것을 별도로 확인하되, pinned open_readonly에는 단일 contextmanager만 선언됐으므로 추가 wrapper는 이번 binding에서 거절한다. 일반 unwrap만 적용하면 허위 metadata/다른 closure를 통과시킬 수 있어 이 계약을 함께 검사한다. 원본에 중첩이 실제 생기는 새 source에는 별도 binding 검토가 필요하다. 실행은 로컬 Windows Python3.12.10의 stdlib dummy뿐이며 Cloud3.12.14의 당시 객체/제품2 결과가 아니다. raw 결과/argv/exit/stdout/stderr 및 파일 핀은 regression-capture에 따로 보존한다.

AST/compile·봉인·ZIP 검사는 구문/bytes 검증이다. dummy PASS도 제품 SOURCE/PRE/GO/POST를 대신하지 않는다. 원2 실제 시험은 여전히 NOT_RUN이며 r01 시도의 결과는 UNKNOWN을 유지한다.

## 유지하는 실행 범위

manifest의 원2 exact IDs와 원본3은 r01 pristine bytes 그대로다. U2 schema-v3 upgrade/writer-fence 검사만을 목표로 한다. 새 실행은 Root가 기존 인간 범위와 새 SOURCE/PRE·단일 정확 실행 지시를 연결한 뒤 결정한다. writer는 제품 재실행·추가 probe·Git/설치/운영 DB·원장·cleanup을 하지 않는다. old r01의 실제 UNKNOWN을 source/dummy 추론으로 덮어쓰지 않는다. 원76/Windows/F6/R2/ledger4/currenthelper10a/qualification/Goal 완료로 확대하지 않는다.

## 실제 더미 결과와 봉인

로컬 표준 더미를 2026-10-04T17:00:00.338016Z에 단1회 시작하여 17:00:00.461224Z에 terminal exit0을 관측했다(PID33016, Windows Python3.12.10, -I -S -B -X utf8, retry0/kill0). source3(runner/check/dummy) 핀은 전후 같고 stderr는0B다. 앞2 수용/뒤9 거절과 before·accepted/rejected22행이 모두 기대와 일치했다. contextlib wrapper code 파일과 __wrapped__ 원파일이 다름을 실제 더미에서 관측했다. dummy 함수본문0, 제품 import/loader/시험0, 전체 runner 실행0이다. 새 checker·관찰/기록·utc AST4함수만 stdlib dummy에 실행했다.

일반 inspect.unwrap은 순환에서 ValueError를 냈고 정상 중첩 wraps는 원함수에 도달했다. 같은 중첩 객체는 이번 단일-decorator source 계약에서 거절됐다. 이 결과는 당시 Cloud r01 실패 객체의 실제 co_filename을 복원하지 않는다. capture/result.json 및 stdout/stderr/execution.json이 원시 증거다. 실제 Linux 제품2/새 URI·SQLite·실제 helper-origin.jsonl은 NOT_RUN이다.

AST/compile6개, 원본3 byte equality, r01/r03/probe 봉인·r01 raw22핀 보존, unchanged supervise/main/SQLite audit AST와 ZIP byte equality를 확인했다. SOURCE.SHA256.json은14 payload를 봉인하고 transport.zip은 봉인까지15항목이다. 새 PRE에서 실제 runtime/contextlib/inspect·root·argv·기한·단회 scope를 다시 고정해야 하며, SOURCE/PRE/실행은 이 더미 PASS로 대체하지 않는다.
