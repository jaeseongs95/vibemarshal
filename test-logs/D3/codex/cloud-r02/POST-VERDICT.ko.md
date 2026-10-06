# R-040 VM D3 r02 선정 3건 단회 실행 POST 판정

- 감사관: claude-code/970c8d0e-0c55-4171-a496-1ca74d080036
- 요청: VM 리드 codex/01a10240 직접 peer 31763b2a-68b1-4ff6-880d-d0c7f3348a72 (2026-10-05T14:04:25Z). 보충 peer 1ee72caa-b77a-4a20-82ed-97f8b6c0d94b (14:11:54Z). 둘 다 ACK만 했다.
- 권한 근거(감사관이 직접 본 것): 01a10acd turn 01a10c27-178a userMessage 13:00:52Z "D3 선정3 실행 및 동일970 POST 고정 지시를 VM 리드에게 1회 발신하도록 허용한다". dot이 13:01Z에 VM 리드에게 보낸 고정 지시(1404자 sha256 3fe22396…)는 12:00Z에 거절된 첫 시도와 바이트가 같다.
- 기준: R-037(source), R-038(최종 PRE), R-039(helper-origin 정상 34행).
- B = `C:/Users/sjs95/Documents/Codex/2026-10-04/task-8/vm-cloud-platform-24cfe63e`

## 1. 판정

**선정 3건 POST: 필수 보완 없음.** 원자료 49개를 직접 읽었다. 실행은 사용자가 승인한 고정 범위와 조건 안에서 한 번 이뤄졌다. runner가 낸 PASS는 원자료와 runner 판정 코드(`runner.py:536-548`)에 들어맞는다.

이 판정이 뜻하지 않는 것은 다음과 같다. 공식 Task 수용·통합·설치, 76건 전체 분류, Windows, 현재 helper, F07S0, qualification이나 Goal 완료로 넓히지 않는다. 범위는 "D3 선정 3건, helper 70f5, Linux Python 3.12.14 -I -S -B, Cloud 1회 실행"이다.

## 2. 핀 (직접 계산)

| 대상 | bytes | SHA-256 |
|---|---|---|
| 반환 ZIP (49항목) | 245576 | 96bca3be143255776e51b33bb1e469c5fe25ab4bb4b237b93d5dec9f355937af |
| 반환 manifest | 59205 | b8cb0db4a0702d73c08badf53d00549d433731a33bc1acbdf9a3a245d9ec2c2c |
| W1 completion seal | 2254 | f35a58bec14e29365123154a105a70388ee420bf34624e9e0f65bdbb3adb88d5 |
| template | 71960 | 88892c55145ff1e17923f206bf788003a8dd468caede8fd610fab7e5c9079730 |
| issued-command.actual.txt | 71986 | a08585f99eb010f1d3a80d8123ab6fa9aecff41c166877e026a3d9ed89d830d8 |
| issued-tokens.actual.json | 233 | 7becf8523adb0dde8c3c8364cee0c40aec604784be404a4f4903ae41df436ef3 |
| runner.py (P19) | 31682 | 97958345d3ef7982ceb620854b56ea5ad2c91029b15b51b2076f26bf870f1cdb |
| manifest.json (P19) | 13558 | ff64e328bfc51fae2c96ebb605008d7ef2fb8fde15e4b36fd6de4d205bb6a3d0 |
| ROOT-ACTUAL-APP-RECORDS | 122648 | 4eb43a9321a3838055d840123985c4049763e54b7b1982d7b9107fd8e7c809f0 |

원자료 위치: `B/writer2/d3-post-execution-r02-01/raw-readback-r01/native/raw` (49개).

## 3. 대조 결과 (읽기·해시·데이터 처리만, 대상 코드 실행 0)

1. **운반**: ZIP 49항목은 폴더 49개와 바이트가 같다. 경로 탈출과 디렉터리 항목은 없다. manifest 49항목의 bytes·SHA가 모두 폴더 파일과 같다.
2. **후보 동일성**
   - `final17/` 17개가 W1 completion 폴더 17개와 바이트가 같다.
   - `P/` 4개(runner·manifest·prepare-command·SOURCE.SHA256)는 P19 원본과 같다.
   - `F/SOURCE.SHA256.json`은 F23 원본과 같다.
3. **발급**
   - 템플릿에 토큰 4개를 각 1회 치환하면 issued-command와 바이트가 같다. 남은 토큰은 0이다.
   - 창은 13:20:49.589Z부터 13:35:49.589Z까지 정확히 900초다.
   - peer ID 4724d72d…는 시스템 prepare messageId와 같다.
   - Cloud prepare가 실제로 만든 issued-command.actual.txt도 리드의 기대 렌더와 바이트가 같다.
4. **guard**(`issuance/dispatch.stdout.txt` 1행)
   - 검사 407개가 모두 true이고 `errors`는 빈 배열, `passed` true다.
   - context는 발급 토큰과 같다.
   - BOUND의 packet 18개·fixture packet 22개 핀은 guard가 관측한 같은 경로의 핀과 40/40 일치한다.
5. **단회 실행**
   - os.execv 시각은 13:30:28.217687Z로 기한보다 321초 이르다.
   - argv는 고정값 그대로다: runner, SHA 2개, `--timeout 300`.
   - App 기록 7개 중 발급 명령을 실행한 것은 exec-447ae0d6 하나다. 나머지는 사전 확인·수집·포장이고, subprocess·execv·os.system 호출이 없다. 포장 명령에 보이는 metadata-observer.py는 ZIP에 넣은 파일이다.
   - execution.json은 child PID 8338 하나, exit 0, timed_out false, retry 0을 기록했다.
6. **결과**
   - test_ids 3개가 P manifest·BOUND의 원래 3건과 같다.
   - testsRun 3, wasSuccessful true다. failures·errors·skipped는 0, unittest_exit·suite_exit는 0이다.
   - stderr의 3건 "ok"와 "Ran 3 tests … OK"가 일치한다.
7. **효과 차단**: `effect-denials.jsonl`은 0바이트(sha e3b0c442…)이고 상태는 CLEAR, count 0이다.
8. **입력 전후**: runner·manifest·테스트·fixture·helper 70f5c5f0…·legacy 8f32c23d…의 핀 6개가 실행 전후로 같다(matches_before true).
9. **D→M 바인딩 20**: 바인딩 20개(DirectSelectionTest 5, fixture 모듈 3, ImplementationWorkflowTest 12)가 `runner.py:233-249`의 기대 소유자·이름·파일·globals와 순서까지 같다. alias 4개(impl, ns, connect, digest)도 같은 객체다.
10. **helper-origin**
    - 34행이고 before 17·accepted 17, rejected 0이다.
    - BOUND `helperOriginNames` 순서대로 이름마다 before 다음에 accepted가 온다.
    - `open_readonly` 1개는 contextmanager 감싼 함수다(노출 contextlib, 원본 helper 파일).
    - R-039 정상 기준과 같다.
11. **모듈 목록**
    - sys.modules에서 표준 라이브러리 밖의 모듈은 `__main__`(runner)·테스트·fixture 3개다.
    - fixture는 helper·legacy를 `exec_module`로 sys.modules에 등록하지 않고 적재한다(`test_flowmarshal_implementation_workflow.py:26-35`). 따라서 이 목록은 `runner.py:92-96`의 허용 규칙과 맞는다.
    - 리드가 처음 세운 "5개" 기대는 리드 쪽 검사 기준의 오류였다. 제품이나 결과를 바꾼 것이 아니다.
    - module_files 경로 맵 138개는 실행 전후가 같다.

## 4. 한계

1. 원자료 값은 Cloud 실행자가 만든 것이다. native·provider ID는 null이고, App 기록 exec-447ae0d6은 출력 없이 exit 0이다. host 이름 742382ba19ca가 사전 확인 때와 같을 뿐, 물리 host가 같다는 증명은 아니다.
2. module_files 바이트 핀 276개는 실행 후에 수집했다. 실행 전 동일성은 경로 맵이 같다는 데까지만 입증된다. 실행 전 바이트 핀은 guard가 확인한 자기 import 모듈(currentGuardModuleFile 40)에만 있다.
3. OSContainment와 externalJobs는 UNKNOWN이고, I/O hardwall은 보장되지 않는다. guard 종료(13:30:28.217061Z)에서 execv까지 약 0.6ms 동안은 원자성이 없다.
4. guard 407개 검사의 논리는 R-038에서 정적으로 읽었다. 이번에는 결과값과 핀만 대조했다.
5. VM2 보고(CURRENT-REPORT 9547B 573a99c6…)와 리드 자체 대조 파일은 판정 근거로 쓰지 않았다. 자기 대조는 독립 판정이 아니다.
6. 독립성은 부분적이다. 이 감사관은 R-037·R-038을 판정했고, R-038 조언 3개(cwd 지정, 같은 Cloud 작업공간, 실행 세션 생존 확인)가 실행 절차에 반영됐다.

## 5. 조언 (지시가 아님)

1. 결과를 옮길 때 1절의 범위 문구를 그대로 붙이는 것이 좋다. "D3 통과"로 줄이면 76건 분류나 Windows 조건으로 읽힐 수 있다.
2. 다음 실행부터는 runner가 child 시작 전에 module_files 바이트 핀을 남기게 하면 4절 2의 한계가 사라진다(설계 변경이라 별도 결정 사안).
3. 리드의 "모듈 5개" 오판은 원기록과 정정을 함께 보존해 두었다. 이 판정에서는 정정이 코드와 맞는다는 것을 확인했다.
