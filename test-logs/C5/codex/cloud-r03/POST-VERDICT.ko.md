# R-034 VM 다음 공통 5건 r03 POST 독립 판정

- 감사관: claude-code/970c8d0e-0c55-4171-a496-1ca74d080036
- 요청: VM 리드 codex/01a10240의 직접 peer b3d01c3d-2ca2-44fd-8172-6d5801d842fe (2026-10-04T21:19:09Z). 앞선 문서 R-031(SOURCE), R-032(더미 fixture SOURCE), R-033(PRE)에 이어진다.
- 대상: `C:/Users/sjs95/Documents/Codex/2026-10-04/task-8/vm-cloud-platform-24cfe63e/lead-post-c5-r03-01/` (봉인 31파일과 봉인 파일 자신).
- 방법: 읽기 전용. 봉인 31개 전체 재해시, 반송 ZIP 23항목과 `raw/` 파일의 바이트 일치, 실행 명령과 R-033 템플릿의 바이트 비교(네 값 대입), `raw/raw/`의 10파일 정독(`preflight.json`·`postflight.json`의 모듈 목록과 `helper-origin.jsonl`의 각 행은 집계), `raw/support/outer-native-output.txt` 전문(직전 점검 결과·dispatch·종료 출력), `C5-POST-SCOPE.ko.txt` 전문. 첫 발급의 미실행에 대해서는 15분 점검에서 `lead-launch-c5-r03-01/NOT-RUN-raw/`의 오류 출력과 준비 명령의 해당 줄, dot의 복구 지시(`C5-PREPARATION-RECOVERY-01a108af-NOTICE.txt`) 원문을 읽었다. 후보·스크립트·Cloud 명령은 실행하지 않았다. Cloud 환경에는 접근하지 않았다. ZIP은 메모리에서만 읽었다.
- 해시만 대조한 것: `ROOT-C5-POST-VERIFIED.actual.json`, `ROOT-C5-ACTUAL-APP-COMMANDS.json`(구조와 첫 항목 앞부분만 판독), `ROOT-C5-VERIFY.py`·`ROOT-C5-VERIFY-r02.py`, `C5-NATIVE-RETURN-MANIFEST.actual.json`, `raw/support/`의 command-audit·module-file-pins-post·native-template-response·post-readback, `raw/prior-dummy/`·`raw/prior-pre/`·`raw/candidate-source/`.
- 실행 근거: dot의 20:45Z 실행 연결 지시와 20:58Z 복구 지시를 원문으로 읽었다. 그 지시들이 인용한 사용자 요청 d717·24cf의 원문은 이 세션에서 확인하지 못했다.
- 한계 공개: C1 지적, 더미 회귀 제안, runner의 기본 방식이 모두 내 조언에서 나왔고 나는 U2 r01을 틀리게 통과시킨 전례가 있다. 완전한 독립 판단이 아니다. 모든 Cloud 사실은 리드가 옮겨 온 사본에 의존한다. 실제 App이 실행한 명령 문자열이 `bound-command.sh`와 같다는 것은 리드의 대조에 의존한다. 나는 `bound-command.sh`가 템플릿에 네 값을 넣은 것과 같다는 것과, 직전 점검 출력의 발급 값들이 그 네 값과 같다는 것을 확인했다. 76건 현황 수치("10건 통과, 66건 미완료")는 이 판정의 대상이 아니다.

## 1. 고정 대조 (일치)

- 요청이 적은 핀(봉인 4371B 13fadb32…, POST-VERIFIED ac7795a5…, ZIP 104575B 5d2108ba…, INVENTORY 3499c243…, result 1073B 6b02e828…, execution 2554B 9f2c9caa…, 실행 명령 9844B aa331819…)이 실제 파일과 일치.
- `ROOT-POST-EVIDENCE.SHA256.json`의 31개 핀이 실제 파일과 모두 일치. ZIP 23항목이 `raw/` 파일과 바이트가 같다.
- `raw/support/bound-command.sh`가 리드의 재발급 명령(`lead-launch-c5-r03-recovery-01/C5-LAUNCH.ISSUED.command.txt`)과 바이트가 같고, R-033에서 본 템플릿(f51c32e3…)의 네 토큰을 발급 값으로 바꾼 결과와 바이트가 같다. R-033의 유효 조건이 지켜졌다.
- 입력 5개(runner f7b4206a…, manifest e7fe12b6…, 테스트 0053b2f6…, helper 70f5c5f0…, legacy 8f32c23d…)가 `invocation.json`·`preflight.json`·`postflight.json`·`execution.json`에서 같고 R-031의 값과 같다.

## 2. 판정

| 구분 | 판정 |
|---|---|
| POST 종합 | **수용(POST PASS)**. 선택 5건이 helper 70f5(10-03 후보)에 대해 Linux Python 3.12.14, `-I -S -B -X utf8` 조건에서 1회 실행돼 통과했다는 결과와 근거를 수용한다. |
| 범위 | 이 5건, 이 helper, 이 환경에 한정한다. original76 전체·Windows·F6·R2·ledger4·현재 helper·qualification·제품 목표 달성으로 넓히지 않는다. |
| 실행 절차 | R-033의 범위와 dot의 지시에 맞는다. 실제 제품 실행은 1회다. |
| 첫 발급(90b87660) | 미실행(NOT_RUN)으로 그대로 둔다. 제품 결과가 아니다. |

## 3. 근거

**결과**
- `result.json`: ID 5개가 manifest와 같고 `testsRun` 5, `wasSuccessful` true, 오류 배열 5개가 모두 빈 값, `unittest_exit` 0, 거절 횟수 0·기록 오류 0, `suite_exit` 0.
- `loader.json`: ID 5개, count 5, errors 빈 값.
- `execution.json`: status PASS, `actual_exit` 0, `terminal_observed` true, `timed_out` false, `observation_unknown` false. run-root의 파일은 기록 9개이고 `child-error.json`은 없다.
- `stderr.txt`: unittest 원문에 다섯 테스트가 각각 ok, `Ran 5 tests in 0.224s`, `OK`. `stdout.txt`는 0바이트다.

**거절 기록** (R-031에서 넣은 보정의 실제 관측)
- `effect-denials.jsonl`이 0바이트로 존재한다. child의 관측(`postflight.json`)과 감독의 관측(`execution.json`)이 같은 값이다: CLEAR, 횟수 0, 행 0, 기록 오류 0, 빈 파일의 해시.
- 따라서 hook이 거절한 호출은 없었다. R-030의 C1(정규식 없는 `assertRaises(RuntimeError)`가 hook 오류를 삼킬 가능성)은 이 실행에서 일어나지 않았다. `command_record_direct_attempt`의 git 호출에도 닿지 않았다.

**출처 검사**
- `helper-origin.jsonl` 28행: 14개 이름 각각 before 1행과 accepted 1행, rejected 0행. `preflight.json`의 `helper_code_origins` 14행: `open_readonly`만 `contextlib.contextmanager`로 감싸였고 나머지 13개는 helper 파일 그대로다.
- 모듈 137개 중 135개가 표준 라이브러리 경로이고 나머지 둘은 runner와 테스트 원본이다. `ctypes`는 실리지 않았다. 실행 전후 모듈 목록이 같다.

**절차**
- 재발급 a097931e: 발급 21:00:58Z, 기한 21:15:58Z. 직전 점검 21:04:11.62~.65Z, 35항목 모두 true, errors 빈 값. dispatch 21:04:11.65Z, runner 호출 21:04:11.72Z, child pid 5770, 종료 관측 21:04:12.18Z.
- `guardCwd`와 `invocation.json`의 `parent_cwd`가 모두 `/workspace/vm-c5-source-24cf-r03/pre-cwd`다. child의 cwd·HOME·TMP가 run-root 안이다. runner 1, child 1, 재시도 0.

**첫 발급의 미실행**
- 90b87660은 Cloud 쪽 준비 명령의 검사(`b'__' not in b`)가 발급 명령 안의 정상 문자열 `.__name__`(2곳)을 거절해 AssertionError로 끝났다. 직전 점검·runner·child는 호출되지 않았다. 제품 실행은 0이었다.
- dot이 준비 검사만 고친 새 발급 1회를 지시했고, 재발급 명령은 템플릿·argv·핀이 같다.

**새로 본 것 (정보)**
- **V1** 발급부터 기한까지의 창이 15분이다(C3·U2는 120초). R-033은 창의 길이를 조건으로 두지 않았다. 실제 실행은 발급 3분 13초 뒤였고 실행 시점에 직전 점검이 다시 통과했다.
- **V2** Cloud 쪽 준비 명령(템플릿과 발급 명령을 Cloud에 기록하고 검사하는 단계)은 PRE 자료에 없었고 내가 감사하지 않았다. 첫 발급의 실패가 그 단계의 결함이었다. 제품 실행 전 단계라 결과에 영향은 없다.
- **V3** 리드의 자체 대조 스크립트가 처음에 경로를 잘못 참조해 실패했고 두 번째 판이 성공했다고 적혀 있다. 두 스크립트는 핀만 대조했다. 내 판정은 그 스크립트에 의존하지 않는다.

**남는 한계**
- OS 수준 격리 없음, 외부 작업과 읽지 못하는 프로세스 수 UNKNOWN. built-in 확장은 실행 파일 핀에만 묶인다.
- 다섯 테스트가 통과했다는 것이 SQLite의 모든 기능이나 제품 전체를 확인한 것은 아니다.

## 4. 조언 (지시 아님)

1. 결과를 장부·문서에 옮길 때 "C5 5건, helper 70f5, Linux no-site 조건" 범위를 그대로 붙이고, 첫 발급의 NOT_RUN과 이 실행을 따로 적기를 권한다.
2. V2: 다음 묶음부터 Cloud 쪽 준비 명령도 PRE 자료에 넣어 주면 미리 본다. 준비 단계의 검사는 "네 토큰이 각 1회 치환됐는가"만 보는 지금의 형태를 유지하기를 권한다.
3. V1: 창의 길이를 PRE 범위에 숫자로 적어 두면 다음부터 판정 범위에 넣을 수 있다.
4. "원본 기준 10건 통과" 같은 누적 수치를 쓸 때는 감사관이 76건 현황표를 감사하지 않았다는 점을 함께 적어 달라.
