# VM W4 r02 CPython 3.11 이상 독립 시험

결과는 **PASS**다. 실제 CPython **3.12.14**, pydantic **2.13.5**를 격리 venv에서 확인하고 원 manifest의 `nativeSupplementExactArgv`를 한 번 실행했다. 확인된 interpreter와 source root placeholder 두 개만 치환했다. exit **0**, `Ran 1 test in 0.264s`, `OK`가 원시 stderr에 기록됐다. 메서드 전체가 최소 독립단위였고 subTests를 변경하거나 분리하지 않았다.

수신: 입력 고정 commit `8fb519e85844c74f60be176e0242667e8673d32a`의 ZIP/patch/manifest를 이 소비 환경의 입력 디렉터리에 `git show`로 받아 세 파일의 지정 bytes/SHA가 모두 일치했다. 이전 Drive 전달 실패와 NOT_RUN 기록은 상위 디렉터리에 그대로 남겼다.

소스 결속: 고정 base `32bb0f9dd9f024045d24487312b50f5b703573a3`, tree `27e18c0c7853c1d35a98d5437cebf0ba703ed42d`의 detached candidate worktree에서 검증된 exact patch를 적용했다. source4는 시험 전·후 모두 지정 bytes/SHA와 일치했고 시험 후 원본 ZIP bytes와도 일치했다. 후보 제품 commit을 따로 만들지 않았으며 입력 manifest의 candidateCommit null을 유지한다.

시험: `EnginePythonCompatibilityTests.test_string_enums_preserve_public_contract_without_stdlib_strenum` 하나가 통과했다. 원문 시험이 runtime/fallback의 public enum map과 대표 8종의 str/format/JSON/constructor 및 pydantic 계약을 검증한다. 16개 subTest 행은 원문 loop와 메서드 OK에서 도출한 결과이며 개별 stdout을 관측한 것으로 쓰지 않았다. 원시 argv/cwd/start/end/exit/stdout/stderr와 실제 interpreter·패키지 정보는 command·runtime 파일에 보존했다.

효과·범위: 새 입력·격리 venv/cache/evidence 생성과 candidate worktree의 exact patch 적용을 관측했다. 시험 동안의 network/syscall 효과는 별도 추적하지 않아 UNKNOWN이다. agent의 Claude/추론 API 호출은 0이다. main/F07과 다른 환경 경로는 변경하지 않았다. 릴리스 freeze/F07을 선행조건으로 삼지 않았고 제품·테스트 소스를 기대값에 맞춰 수정하지 않았다. 실제 Python 3.10 검증(A2-AC1)은 이 cell에서 NOT_RUN이며 다른 cell이 담당한다. 이 native supplement PASS는 전체 릴리스 판정이 아니다.

게시: 최신 evidence HEAD에 이 실행 디렉터리와 상위 LATEST.md만 추가하고 non-force push한다. 고정 원격 result commit의 raw git blob bytes/SHA 재관측은 게시 후 영수증과 최종 보고에 기록한다. `FILES.json`은 이 실행 디렉터리의 파일 목록이며 자기 자신은 제외한다.
