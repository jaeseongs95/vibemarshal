# R-021 VM Cloud C3 final-r03 POST 독립 판정

- 감사관: claude-code/970c8d0e-0c55-4171-a496-1ca74d080036
- 요청: VM 리드 codex/01a10240의 직접 peer e1858d5c-16da-46ee-9a61-f1ab68694f5e (2026-10-04T15:14:10Z). 앞선 판정 R-019(SOURCE STATIC PASS), R-020(PRE PASS 자료 기준)에 이어진다.
- 대상: `C:/Users/sjs95/Documents/Codex/2026-10-04/task-8/vm-cloud-platform-24cfe63e/lead-post-c3-r03-r02-01/` 22파일.
- 방법: 읽기 전용. 해시·크기 대조, 반송 묶음(carrier → ZIP → raw)의 바이트 일치 확인, `raw/run/`의 8파일과 `raw/support/`의 실행 지시·직전 점검·바깥 실행 기록·실행 명령 원문 정독, `preflight.json`·`postflight.json`의 모듈 목록 집계. 후보·스크립트·Cloud 명령은 실행하지 않았다. Cloud 환경에는 접근하지 않았다. ZIP은 메모리에서만 읽었다.
- 실행 근거: dot의 범위 확정 지시(courier peer 64b0a0af, 14:52:31Z, 전달 폴더 `C3-FINITE-EXECUTION-SCOPE-NOTICE.txt`)를 원문으로 읽었다. 그 지시가 인용한 사용자 요청 d717·24cf의 원문은 이 세션에서 확인하지 못했다.
- 한계 공개: R-015·R-019·R-020의 내 조언이 후보와 점검 항목에 반영돼 있어 완전한 독립 판단이 아니다. 모든 Cloud 사실은 리드가 옮겨 온 사본에 의존한다. 사본끼리의 일치는 내가 확인했지만 사본이 Cloud 원본과 같다는 것은 리드의 App 기록 판독(`ROOT-APP-RECORDS.actual.json`)에 의존하며, 그 파일은 핀만 대조하고 내용을 한 줄씩 읽지 않았다.

## 1. 고정 대조 (일치)

- 요청의 세 핀(`POST-EVIDENCE.SHA256.json` 2979B 86f171ad…, `ROOT-READBACK.ko.txt` 3857B 69d61ceb…, `ROOT-APP-RECORDS.actual.json` 13798B 8e6f8057…)과 실제 파일 일치.
- `POST-EVIDENCE.SHA256.json`의 21개 핀이 실제 파일과 모두 일치.
- carrier 72609B 917b87dd…를 base64로 풀면 ZIP 54454B a0622a7d…와 바이트가 같다. ZIP 17개 항목이 `raw/` 17파일과 바이트가 같다. `POST.INVENTORY.json`의 16개 핀이 `raw/` 파일과 모두 일치.
- 실행 전후 입력 5개(runner 457fd655…, manifest ae0260a8…, 테스트 0053b2f6…, helper 70f5c5f0…, legacy 8f32c23d…)가 `invocation.json`, `preflight.json`, `postflight.json`, `execution.json` 네 곳에서 같고 R-019·R-020에서 본 값과 같다.

## 2. 판정

| 구분 | 판정 |
|---|---|
| POST 종합 | **수용(POST PASS)**. 고정된 C3 선택 3건이 helper 70f5(10-03 후보)에 대해 Linux Python 3.12.14, `-I -S -B -X utf8` 조건에서 1회 실행돼 통과했다는 결과와 근거를 수용한다. |
| 범위 | 이 3건, 이 helper, 이 환경에 한정한다. original76·Windows·F6·R2·ledger4·현재 helper(10a69265…)·qualification·제품 목표 달성으로 넓히지 않는다. |
| 실행 절차 | PRE와 dot의 범위 지시에 맞는다. |

## 3. 근거

**결과**
- `result.json`: 테스트 ID 3개가 manifest와 같고 `testsRun` 3, `wasSuccessful` true, failures·errors·skipped·expectedFailures·unexpectedSuccesses가 모두 빈 배열, `suite_exit` 0.
- `loader.json`: ID 3개, count 3, errors 빈 배열. R-015에서 지적한 loader 문제(C1)가 실제 실행에서도 나타나지 않았다.
- `execution.json`: status PASS, `actual_exit` 0, `terminal_observed` true, `timed_out` false, `observation_unknown` false. run-root에 남은 파일은 기록 7개뿐이고 `child-error.json`은 없다.
- `stderr.txt`: unittest 원문에 세 테스트가 각각 ok, `Ran 3 tests in 0.065s`, `OK`. `stdout.txt`는 0바이트다.

**절차**
- 실행 지시: 14:56:17Z 발급, 기한 14:58:17Z. 실제 dispatch 14:56:48.09Z, runner 시작 14:56:48.17Z, 종료 관측 14:56:48.48Z. 120초 창과 300초 예산 안이다.
- 직전 점검 18항목이 모두 true, errors 빈 배열(`launch-freshness-r02.json`, `outer-native-output-r02.txt`). 점검 내용은 `r02-native-command.sh` 원문에서 읽었다: source 9파일·seal·실행 파일·PRE 핀·표준 모듈 6개·run-root 부재·symlink 0·`FM-` 마디 0·비중첩·빈 cwd.
- 점검이 끝난 같은 프로세스가 `os.execv`로 runner를 불렀다. argv는 PRE-REQUEST와 dot 지시의 값과 같다. runner 호출 1, child 1, 재시도 0.
- child의 cwd·HOME·TMP가 모두 run-root 안이다(`invocation.json`).
- 앞선 지시 03433096의 미실행(NOT_RUN)은 별도 폴더에 그대로 보존돼 있다.

**R-020에서 남긴 항목**
- Linux에서 hook이 걸린 상태의 import: child가 오류 없이 끝났으므로 금지 이벤트가 나지 않았다. `preflight.json`의 모듈 137개 중 135개가 표준 라이브러리 경로이고 나머지 둘은 runner와 테스트 원본이다. `asyncio`·`socket`·`ssl`·`subprocess`·`uuid`가 실렸지만 `ctypes`는 없다. 실행 전후 모듈 목록이 같다.
- 조언 1(준비 명령 기록): `command-audit.json` 41792B 12731f1a… 전체 사본이 포함됐다. 리드는 이 사본이 준비 명령 61개의 본문 전부를 담지는 않는다고 스스로 적었다. 나는 이 파일을 핀만 대조했다.
- 조언 2(표준 모듈 해시): `module-file-pins-post-r02.json`에 274행, 대응 오류 0, PRE의 6개 핀과 불일치 0이라고 적혀 있다. 실행 뒤에 읽은 값이라 실행 시점의 메모리 안 코드까지 증명하지는 않는다(리드가 스스로 적음).

**확인하지 못한 것과 남는 한계**
- OS 수준 격리는 없다. 읽지 못한 프로세스 7개, 외부 작업 UNKNOWN. audit hook은 선택된 Python 지점만 본다.
- built-in 확장(`_sqlite3`, `_ssl` 등)은 파일 해시가 없고 실행 파일 핀(fa674435…)에만 묶인다.
- 직전 점검 기록의 `guardArgv`에 `-X utf8`가 빠져 있다. 실제 명령 원문에는 들어 있고, 기록 생성 코드가 그 문자열을 고정값으로 쓴 탓이다(`r02-native-command.sh` 원문에서 확인). 결과에 영향 없다.
- `ROOT-APP-RECORDS.actual.json`과 `command-audit.json`의 내용, `module-file-pins-post-r02.json`의 274행 각각은 읽지 않았다.

## 4. 조언 (지시 아님)

1. 이 결과를 장부나 문서에 옮길 때 "C3 3건, helper 70f5, Linux no-site 조건"이라는 범위를 문장 그대로 붙여 두는 것을 권한다. 현재 저장소의 helper는 다른 파일이다.
2. 이번에 쓴 "직전 점검과 실행을 한 명령으로 묶는 방식"은 기한 초과로 인한 미실행을 줄였다. 다음 후보에서도 같은 방식을 쓰면 PRE에서 그 명령 원문을 미리 볼 수 있게 해 주면 좋다.
