# VM W4 r02 — codexcloud-py311plus

결과는 **NOT_RUN**이다. 원시 입력 전달에 실패하여 manifest의 `nativeSupplementExactArgv`를 읽거나 실행하지 않았다. CPython 3.12.14와 pydantic 2.13.5의 실제 관측은 `runtime-probe.*`에 기록했다. 이 probe는 W4 시험이 아니다.

Drive 메타데이터 조회와 원시 fetch는 성공했고 제공된 ZIP/patch/manifest의 기대 크기와 같은 크기 정보를 반환했다. 그러나 소비 환경에서 반환된 인증 파일 URL의 다운로드가 모두 HTTP 403으로 실패했다. 응답 수집 시 `CONNECT tunnel failed, response 403`이 확인됐고, 승인 검토를 통과한 escalated 재시도도 HTTP 403이었다. proxy 우회는 해당 호스트 DNS 해석 실패(exit 6)였다. 다운로드 bytes/SHA, exact patch 적용, source4 검증은 모두 미실행이다. 서명 URL은 비밀값이므로 기록에서 제외했다. 초기 시도의 절대 start/end는 수집되지 않아 null로 남겼다.

고정 base `32bb0f9dd9f024045d24487312b50f5b703573a3`와 tree `27e18c0c7853c1d35a98d5437cebf0ba703ed42d`는 기존 소비 checkout에서 확인했다. 입력이 검증되지 않았으므로 별도 candidate source worktree나 후보 patch 적용으로 진행하지 않았다. 기존 checkout은 clean이다. 원문 시험과 subTests, 제품 소스, main, F07은 변경하지 않았다. Claude 및 추론 API 호출은 없었다. 릴리스 freeze/F07을 선행조건으로 삼지 않았다. 이 결과는 별도 Python 3.10 시험을 대체하지 않는다.

Git의 기존 evidence HEAD는 `003e6ed95b56b485ce8bca3f457659891ea80ec6`였다. 연결된 GitHub `get_repo`가 `visibility: public`을 반환했고, `gh api`로 재확인하는 경로는 Forbidden으로 실패했다. 사용자가 승인한 private 조건을 확인할 수 없어 당시 원격 게시를 보류했다. 이 visibility 관측의 충돌은 저장소가 실제 공개라는 확정 판정으로 확대하지 않는다. 로컬 evidence는 그 HEAD의 별도 detached worktree에서 고유 경로에만 작성했다. 당시 원격 result commit, URL, 원격 bytes/SHA 검증은 존재하지 않았다.

실제 `EnginePythonCompatibilityTests.test_string_enums_preserve_public_contract_without_stdlib_strenum`는 0회 실행됐으며 결과·효과 관측은 각각 NOT_RUN/UNKNOWN이다. 원시 입력을 이 소비 환경에 안전하게 전달하고 hash를 검증할 수 있는 지원 도구가 남은 차단 요인이다. 이전 실패 및 미실행은 그대로 유지했다.

`FILES.json`은 이 디렉터리의 결과 파일 bytes/SHA-256 목록이며 자기 자신은 제외한다.

후속 승인·관측: 사용자가 저장소를 의도적으로 public으로 바꿨음을 확인하고 정제된 evidence의 공개 게시를 승인하여 이전 private 게시 보류는 해제됐다. 부모가 안내한 `download_file` 지원 경로를 확인했으나 이 소비 환경의 활성 도구 목록에는 해당 도구 및 추가 도구 검색 기능이 없다. 반환된 파일 ID로 이 환경에 bytes를 받는 지원 기능이 없어 원문 hash 검증과 nativeSupplementExactArgv 시험은 여전히 NOT_RUN이다. 셸 URL 다운로드·proxy 변경·네트워크 상승을 추가로 시도하지 않았다. 이 디렉터리만 최신 evidence HEAD에 추가하고 non-force push한다. 고정 원격 commit의 bytes/SHA 재관측 결과와 URL은 게시 후 영수증·최종 보고에 기록한다.
