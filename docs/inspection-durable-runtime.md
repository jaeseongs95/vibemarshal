# 저장형 진단 실행 준비

7차 static 실행은 첫 세 사례에서 의미 계약을 통과했지만, 대화의 포그라운드 실행 세션 중단 뒤 네 번째 ephemeral thread의 상태를 읽을 수 없었다. 모델의 의미 규칙을 추가 보정할 근거로 삼지 않고 운영 복구 경계를 먼저 수정한다.

## 변경 범위

- `CodexStructuredRoleRunner`에 명시적 bool인 `ephemeral_threads`를 추가한다. 일반 호출의 기본값은 기존 `true`이며 요청 payload·schema·모델·effort·timeout을 바꾸지 않는다.
- 새 고정 diagnostics의 preflight와 prepare lock은 `role_threads_ephemeral=false`를 결속한다. 실제 역할 생성에서 intent·provider receipt도 같은 값을 요구한다. 요청 불일치는 thread 생성 전에, provider 불일치는 원래 receipt 저장 뒤 차단한다.
- 모델 turn 없는 지침 관측 probe는 ephemeral로 유지한다. 저장 여부와 실제 모델 turn 수를 혼동하지 않는다.
- 긴 실행은 Windows의 `Start-Process -WindowStyle Hidden`으로 시작한다. 실행 전 lock 검사와 중복 실행 방지 marker를 유지하고 launch intent, 실행 명령, PID·시작 시각, 로그 경로는 고정 평가 입력 밖의 별도 운영 경로에 남긴다.
- 프로세스 중단 뒤에는 기존 thread ID를 재개 없이 `thread/read`로 먼저 관측한다. 저장형 설정도 응답·usage 복구를 보장하지 않으며 자동 재실행·fallback·timeout 연장은 추가하지 않는다.

7차의 provider 의미 schema, AC scope 선택과 adapter join, 표준 finding target catalog, semantic prompt와 고정 기대표는 유지한다. 새 source·지침·운영 lock을 이전 실행에 적용하지 않는다.

## 검증과 다음 실행

집중 회귀는 일반/저장형 생성 값, 원래 request 불변, 단일 turn과 resume 미사용, 잘못된 bool 거부, preflight 결속, 생성 전/후 불일치와 receipt 보존을 확인한다. 실제 저장 조회는 별도의 운영 probe로 확인해야 하며 모의 테스트를 실제 복구 성공으로 보고하지 않는다.

관련 19개 테스트, 전체 673개 테스트(83.477초)를 포함한 개발 결정적 Gate 5/5가 통과했다. 개발 Gate의 artifact는 `D:\codex\fm-recovery\.flowmarshal-engine-eval\runs\inspection-durable-devgate\deterministic`이며 contract는 `sha256:77233a6c0da4290c9ed3470529770b771bb0302ac326b3de6e897ea74e3b7fe3`, source manifest는 `sha256:18fa14077f1cae4164be6ceac25d340b795f0a632e885086a9b6fa066058037c`, report SHA-256은 `669dee70c4a2b00877f3d75bd9a8edbe32d0e812da4303ccc3718290261eacd5`다. 의미 계약 구현·prompt·evaluator는 `ac314a8`과 파일 차이가 없음을 확인했다.

다음 고정 실행은 새 detached worktree·전용 Python·원본 fixture package·고정 executable·명시적 역할 설정으로 preflight, 결정적 Gate, prepare까지 먼저 완성한다. 실행은 새 전체 11사례이며 이전 PASS를 이어 붙이거나 미확인 combined를 성공 처리하지 않는다. 실제 호출의 결과는 별도 기준선에 기록한다.

7차 중단의 결속·부분 결과는 [7차 부분 실행 기준선](inspection-v2r7-partial-baseline.md)에 있다. 전체 static 11과 qualification 13, 실제 GoalVerdict가 미완료이므로 전체 목표와 cutover는 완료 상태가 아니다.
