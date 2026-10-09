# VM W4 r02 codexcloud-py310: PASS

고정 입력 commit 8fb519e85844c74f60be176e0242667e8673d32a에서 ZIP·patch·manifest를 수신해 각 bytes/SHA를 대조했다. 지정 base/tree 별도 worktree에 exact patch를 적용했고 source4는 시험 전후 모두 일치했다.

uv 공식 python-build-standalone 배포로 설치한 실제 CPython 3.10.21과 격리 venv의 pydantic 2.13.5에서 원 manifest primaryExactArgv를 interpreter/source root placeholder만 바꾸어 실행했다. exit 0, 1 test OK. subTests는 수정하거나 분리하지 않았다. 명령·cwd·start/end·stdout/stderr는 primary-exact.json 및 원시 stream 파일에 있다.

A2-AC1/AC2는 해당 원 unit 기준 PASS다. Python 3.11+ native 보충시험은 이 셀에서 NOT_RUN이며, 이 결과는 릴리스 freeze/F07/전체 qualification 판정이 아니다. 개별 subTest 성공 로그는 unittest가 출력하지 않아 원 method OK에서 통과를 추론한다. runtime 출처와 interpreter bytes/SHA, lock, source4와 효과 UNKNOWN은 result.json에 기록했다.

상위 경로의 과거 NOT_RUN 증거는 보존한다. 원격 게시·고정 commit 재조회 결과는 최종 보고의 commit 및 검증 기록을 참조한다.
