# C1 r03 비제품 PRE 실행 자료 — SOURCE 준비
이번 산출물은 아직 실행하지 않은 비제품 dummy source다. 원 r03의 12개 파일은 그대로 보존했다. 필수 case의 PASS/FAIL/UNKNOWN 표기는 향후 관측과 대조할 기대값이며 실제 결과가 아니다. Root가 원본·pin을 다시 읽고 같은 Claude970이 fixture source를 검토한 뒤, 기존 Cloud actor의 새 PRE에서만 실행한다.

frozen/runner.py는 r03 runner 원 바이트다. 해당 모듈을 import하지 않는다. AST에서 utc/save/effect_denial_counts/observe_effect_denials/residual_files, child의 init·deny·audit·결과 저장/close/후검사, main의 child-error 처리, supervise의 raw 관측·거절·최종 판정 구간을 그대로 추출한다. EXTRACTIONS.json은 각 원 구간 행과 AST digest를 고정한다. 함수 wrapper·namespace binding만 dummy이며 부모의 PASS·FAIL·UNKNOWN 조건은 다시 작성하지 않았다. 원 main/child/supervise 전체, loader, 테스트 모듈, 제품 helper·legacy는 실행하지 않는다.

정상 0건에는 필수 빈 journal을 사용한다. 합성 이벤트를 audit가 거절한 뒤 payload가 RuntimeError를 잡아도 원 카운터·journal·부모 판정에 남아야 한다. uncaught_denial에서는 payload가 예외를 잡지 않고 바깥 fixture가 원 main의 오류 처리 AST로 child-error를 기록한다. 모든 case를 단일 child에서 다루므로 이 항목은 실제 OS process의 미처리 예외 종료를 재현하지 않는다. PID와 전체 child native exit는 실제 관측으로 기록하고, case 함수의 반환은 stage_return으로 구별한다.

init/write/flush/close 오류는 새 owned journal의 open/stream facade에서 OSError를 주입한다. write/flush 주입 시 count가 먼저 1이 되었는지와 RuntimeError 거절 유지도 관측한다. missing/partial/corrupt/count/PID/post counter/bool 불일치는 새 dummy 증거에만 주입한다. 바꾸기 전 파일은 .before-fault로 옮겨 원 바이트를 보존하고 새 fault 파일을 CreateNew로 쓴다. close 오류 뒤 열린 underlying handle을 닫는 fixture finalizer는 원 오류·카운터·raw bytes를 바꾸지 않는다.

합성 원시 결과는 c1_non_product.synthetic_shape.* ID 5개와 성공 형태를 가진 데이터이며 unittest를 실행한 결과가 아니다. 제품 oracle이나 제품 PASS로 쓰지 않는다. 부모 AST에는 실제 owned child PID/terminal/native exit/timeout과 실제 fixture source before-after를 넣고, dummy ID를 결속한다. 요구한 기대값, 카운터, 원 예외, 원 관측 필드를 대조한 fixture 상태만 MATCH/NOT_ACCEPTED로 보고한다.

향후 Linux Python3.12 -I -S -B -X utf8에서 새 absolute PRE root/cwd/home/tmp/cases, child 1개, 총 wait/kill/reap 예산 30초·retry0으로 수행한다. 새 부모가 자식 한 개를 시작하는 호출과 그 소유 child의 timeout kill/reap만 허용한다. os.system은 호출하지 않고 sys.audit('os.system', 고정 dummy 인자)만 호출한다. audit dispatcher는 child 하나에만 설치하고 case 사이에 현재 hook을 None으로 바꾸며 합성 sentinel로 누출 여부를 확인한다. 부모에는 hook을 설치하지 않는다. OS 호출과 파일 I/O의 hardwall 상한 보장은 하지 않는다.

실행 명령은 manifest의 argv_template과 새 manifest/seal pin을 결속한다. 모든 실제 argv/cwd/명시적 child env/PID/native exit/runtime module bytes와 source before-after를 원시 파일로 남긴다. 출력과 기존 파일을 덮거나 누락 증거를 복구·재시도하지 않는다. 전역 설정·설치·Git·provider·network·제품 SQLite·제품 import/discovery/runner 실행은 포함하지 않는다. 현재 수행한 작업은 파일 작성과 메모리 AST/compile·해시·ZIP 대조뿐이다.
