# Cloud C3 final 소스 후보 r03

R015(8099B / 50624ab432fce89737b5c9262510105d7daf568d245963026dab8045953ece98)의 C1을 보정한 새 고정 후보다. SOURCE NOT PASS인 r02는 보존하고 실행하지 않았다. 원 M/helper70f5/legacy8f 3파일, exact C3 IDs/assertions/SQL/tearDown은 그대로다. helper는 10-03 후보70f5이며 현재 저장소10a가 아니다. 이 후보 runner/원 소스 import/loader/실제 C3는 NOT_RUN이며 독립 SOURCE/PRE와 Linux runtime 확인이 남아 있다.

SOURCE.DIFF.patch는 고정 r02 runner와의 실제 차이다. 고정 MODULE을 먼저 명시적으로 __import__한 뒤 full IDs가 원 exact3이며 MODULE+"." prefix인지 확인한다. prefix만 제거한 클래스·메서드 names를 TestLoader.loadTestsFromNames(names,module=module)에 넘긴다. 이후 case.id() full IDs/count3/error0 및 원 module/code origins·closure 검사는 그대로다. import hook의 name==MODULE 허용은 그대로이며 prefix allowlist를 넓히지 않았다. initializer/alias/fixture/oracle 수정0이다.

별도 writer4/probe-r03에서 Windows Python3.12.10의 더미 모듈 probe 1회를 수행했다. 기존 full-name 첫 import 인자는 zz_c3_probe_module.K.test_a이고 RuntimeError로 거절됐다. 고정 모듈 방식은 zz_c3_probe_module import와 full IDs3/count3/error0을 실제 반환했다. 범위 밖 import와 wrong_method/wrong_module/extra_id가 거절됐다. 더미 테스트 body는 실행0이며 제품·후보 import/C3 loader/테스트0이다. 이 결과는 C3/SOURCE/PRE/GO 또는 Linux PASS가 아니다. probe 근거 집합은 이 source packet과 분리했다.

독립 SOURCE/PRE 및 기존 정상 지원 Cloud의 고정 Linux Python3.12/native stdlib/private root 확인 후 사용할 명령:

    <고정 Linux Python3.12> -I -S -B -X utf8 <r03 packet>/runner.py --run-root <존재하지 않는 절대 private run-root> --runner-sha256 457fd65598bc5cb5a87dd46abb7036bac9768db20a932053fb9670b201862136 --manifest-sha256 ae0260a8fd9803cc716a8eab504e34b9bdfd919f39695e8bced5a896c116c3ad --timeout 300

-I/-S는 별도 stdlib C3 profile이며 Q/원76 startup을 대신하지 않는다. legacy는 원 M의 import seam만 사용한다. 직접 imports·선택 fixture/graph/path/string 경로에서 pydantic 의존은 발견되지 않았다. 실제 Linux SQLite/SSL native dependency 및 module closure는 NOT_BOUND/NOT_RUN이다. 설치/provider/Git0, ledger4/R2/원76 기준 완화0을 유지한다.

r02의 hook/timeout/UNKNOWN 경계는 변경하지 않았다. 새 root/home/tmp/빈 cwd와 자기 child1회, retry0이다. hook은 비stdlib import/socket/subprocess/ctypes/chdir/SQLite seam을 제한하고 open/dir_fd를 막지 않으며 OS sandbox/전체 syscall containment가 아니다. 고정 source의 fixture 쓰기와 실제 Cloud OS 권한은 PRE에서 따로 확인한다.

동일 최대300초 wait+reap 예산(min(5초,timeout/10)의 reap reserve)에서 timeout이면 자기 Popen child만 kill/reap한다. 기존 PID/프로세스 검색/그룹/global kill·기존 cleanup0이다. OS 생성/kill/capture의 hardwall 상한은 보장하지 않는다. reap 실패는 UNKNOWN/actual_exit=None, reaped timeout은 TIMEOUT으로 기록하며 PASS로 바꾸지 않는다. 원 raw unittest 결과를 추가 관측 전에 CreateNew 보존한다. partial raw/잔여 is_file-stat·열거 오류는 관측error/UNKNOWN이며 결과를 지우거나 PASS로 삼키지 않는다.

SOURCE.SHA256.json은 자신/transport.zip 외 payload8 파일을 봉인하고 carrier는 payload8+seal 9파일이다. 전후 original3/runner/manifest hash 검사는 유지된다. 성공 주장은 helper70f5의 C3에만 적용하고 현재helper10a·원76·Windows·F6·qualification·R2·ledger4 PASS로 확대하지 않는다. Root가 Claude970 직접 peer 재감사를 연결하며 이 writer는 감사관/courier로 직접 전송하지 않는다.
