# R-029 VM U2 next-u64-r02 POST 독립 판정

- 감사관: claude-code/970c8d0e-0c55-4171-a496-1ca74d080036
- 요청: VM 리드 codex/01a10240의 직접 peer 2f13f984-1d80-4afd-adbb-fe9d8822625c (2026-10-04T18:17:20Z). 앞선 문서 R-026(r02 SOURCE STATIC PASS), R-028(r02 PRE PASS)에 이어진다. r01에 관한 R-024·R-025는 그대로 보존한다.
- 대상: `C:/Users/sjs95/Documents/Codex/2026-10-04/task-8/vm-cloud-platform-24cfe63e/lead-post-u2-r02-01/`.
- 방법: 읽기 전용. 해시·크기 대조, 반송 묶음(carrier → ZIP → raw)의 바이트 일치, 실행 명령 틀에 네 값을 채운 결과와 실제 명령의 바이트 비교, `raw/run-raw/` 9파일 정독(`preflight.json`·`postflight.json`의 모듈 목록은 집계), `raw/support/`의 `guard.json`·`dispatch.json`·`native-terminal.json`·바깥 출력 끝부분 판독. 후보·스크립트·Cloud 명령은 실행하지 않았다. Cloud 환경에는 접근하지 않았다. ZIP은 메모리에서만 읽었다.
- 해시만 대조한 것: `ROOT-APP-RECORDS.actual.json`, `ROOT-POST-READBACK.actual.json`, `raw/support/`의 outer-execution-receipt·binding-metadata·post-collector.receipt·post-observation, `raw/retained-pre/` 6파일, `raw/packet-pins/` 2파일, `raw/POST.INVENTORY.json`의 payload 목록.
- 실행 근거: dot의 17:16Z 범위 지시와 17:58Z 진행 지시(전달 폴더 `R026-U2-R02-SCOPE-VM-NOTICE.txt`, `R028-U2-R02-PRE-VM-NOTICE.txt`)를 원문으로 읽었다. 그 지시가 인용한 사용자 요청 d717·24cf의 원문은 이 세션에서 확인하지 못했다.
- 한계 공개: 나는 r01의 SOURCE·PRE를 틀리게 통과시킨 당사자다(R-024). r02의 보정 방향과 점검 항목은 내 조언에서 나왔다. 이 판정은 완전한 독립 판단이 아니다. 모든 Cloud 사실은 리드가 옮겨 온 사본에 의존한다. 사본끼리의 일치는 내가 확인했지만 사본이 Cloud 원본과 같다는 것은 리드의 App 기록 판독에 의존한다. helper의 `command_verify` 전체 등 R-022에서 읽지 않은 구간은 끝내 읽지 않았다.

## 1. 고정 대조 (일치)

- 요청이 적은 핀(result 582B 06a539d8…, preflight 7e5d8da7…, postflight 897d9eda…, helper-origin 39b6295d…, 템플릿 a78788b8…, 실행 명령 6399B beb4f505…, carrier de663bcc…, ZIP 870dcf98…, ROOT-POST-READBACK e4fe8e8d…, ROOT-APP-RECORDS afa1a02a…)이 `POST-EVIDENCE.SHA256.json`(4740B e988dd09…)의 33개 핀에 들어 있고, 33개 모두 실제 파일과 일치한다. `POST-REQUEST.ko.txt` 4037B의 해시 28b419aa…는 peer 본문의 해시와 같다.
- carrier 128373B를 풀면 ZIP 96279B와 같고, ZIP 28항목이 `raw/` 파일과 바이트가 같다.
- `execution-template.sh`가 R-028에서 본 `LAUNCH-PRE-TEMPLATE.sh`와 바이트가 같다. 그 틀의 네 토큰을 발급 값으로 바꾼 결과가 `bound-command.sh`와 바이트가 같고, 리드가 발급 때 남긴 `LAUNCH-BOUND.expected.sh`와도 같다. R-028의 유효 조건(네 토큰 외 변경 없음)이 지켜졌다.
- 입력 5개(runner cfc6e0f4…, manifest 90a8ac8e…, 테스트 0053b2f6…, helper 70f5c5f0…, legacy 8f32c23d…)가 `invocation.json`·`preflight.json`·`postflight.json`·`execution.json`에서 같고 R-026의 값과 같다.

## 2. 판정

| 구분 | 판정 |
|---|---|
| POST 종합 | **수용(POST PASS)**. 선택 2건(schema v3 additive migration, writer fence 거절)이 helper 70f5(10-03 후보)에 대해 Linux Python 3.12.14, `-I -S -B -X utf8` 조건에서 1회 실행돼 통과했다는 결과와 근거를 수용한다. |
| 범위 | 이 2건, 이 helper, 이 환경에 한정한다. C3·original76·Windows·F6·R2·ledger4·현재 helper(10a69265…)·qualification·제품 목표 달성으로 넓히지 않는다. |
| 실행 절차 | PRE와 dot의 범위 지시에 맞는다. |
| r01과의 관계 | r01의 UNKNOWN은 그대로 UNKNOWN이다. 이 통과가 r01 결과를 바꾸지 않는다. |

## 3. 근거

**결과**
- `result.json`: ID 2개가 manifest와 같고 `testsRun` 2, `wasSuccessful` true, failures·errors·skipped·expectedFailures·unexpectedSuccesses가 모두 빈 배열, `suite_exit` 0.
- `loader.json`: ID 2개, count 2, errors 빈 배열.
- `execution.json`: status PASS, `actual_exit` 0, `terminal_observed` true, `timed_out` false, `observation_unknown` false. run-root의 파일은 기록 8개뿐이고 `child-error.json`은 없다.
- `stderr.txt`: unittest 원문에 두 테스트가 각각 ok, `Ran 2 tests in 0.120s`, `OK`. `stdout.txt`는 0바이트다.

**절차**
- 발급 18:03:03Z, 기한 18:05:03Z. 직전 점검 18:03:36.44~.48Z, 25항목 모두 true, errors 빈 값(`guard.json`). dispatch 18:03:36.48Z, runner 호출 18:03:36.54Z, child pid 4113, 종료 관측 18:03:36.91Z.
- `guardCwd`와 `invocation.json`의 `parent_cwd`가 모두 `/workspace/vm-u2-source-24cf-r02/pre-cwd`다. child의 cwd·HOME·TMP가 run-root 안이다.
- runner 호출 1, child 1, 재시도 0. 고정 argv가 R-028에서 본 값과 같다.

**출처 검사의 실제 관측** (R-028에서 미관측으로 남긴 항목)
- `helper-origin.jsonl` 14행: 7개 이름 각각 before 1행과 accepted 1행, rejected 0행.
- `open_readonly`: 노출된 코드의 파일이 `…/lib/python3.12/contextlib.py`, 감싸인 원 함수의 파일이 helper, 원 함수의 전역 dict가 helper의 것, 데코레이터 판정 `contextlib.contextmanager`. 나머지 6개는 감싸임 없이 helper 파일이다.
- `preflight.json`의 `helper_code_origins` 7행이 위와 같다.
- 이로써 R-024의 원인 결론(r01이 `open_readonly`의 wrapper 출처 때문에 멈췄다)이 실제 helper에 대한 관측으로 뒷받침된다. r01 실행 자체에 그 이름이 기록돼 있지 않다는 사실은 그대로다.

**R-022·R-028에서 남긴 항목**
- U1(금지 호출 도달 여부): 두 테스트가 오류 없이 끝났으므로 hook이 막는 호출은 일어나지 않았다.
- SQLite의 backup·읽기 전용 URI·UDF·trigger와 runner의 URI 분기: 두 테스트가 이 기능들을 쓰고 통과했으므로 이 환경에서 동작했다. 각 기능을 따로 관측한 기록은 아니다.
- 모듈 137개 중 135개가 표준 라이브러리 경로이고 나머지 둘은 runner와 테스트 원본이다. `ctypes`는 실리지 않았다. 실행 전후 모듈 목록이 같다.

**남는 한계**
- OS 수준 격리는 없다. 읽지 못한 프로세스 7개, 외부 작업 UNKNOWN. audit hook은 선택된 Python 지점만 본다.
- built-in 확장은 파일 해시가 없고 실행 파일 핀에만 묶인다.
- 리드의 App 기록과 바깥 실행 영수증의 내용은 읽지 않았다.

## 4. 조언 (지시 아님)

1. 결과를 장부·문서에 옮길 때 "U2 2건, helper 70f5, Linux no-site 조건" 범위를 문장 그대로 붙여 두는 것을 권한다. 현재 저장소의 helper는 다른 파일이다.
2. r01(UNKNOWN)과 r02(PASS)를 한 줄로 합쳐 적지 말고 따로 적는 것을 권한다. r01은 검사 장치 결함으로 결과가 없었던 실행이다.
3. 다음 묶음의 runner가 이름으로 집는 객체가 늘면, 정적 점검에 데코레이터 유무 목록을 넣는 것을 권한다(R-024 조언 2).
