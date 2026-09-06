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

## 고정 작업본 준비 결과

운영 보완 커밋 `a3d801f3556ffbf13030abeb872644453f1c2e46`을 `codex/inspection-recovery`에 push하고 새 detached 작업본 `D:\codex\fm-inspection-v2r8`을 만들었다. 8차는 운영 실행 revision이며 의미 계약은 7차 그대로다.

| 항목 | 실제 결과 |
|---|---|
| 실행 경로 | `D:\codex\fm-inspection-v2r8\.flowmarshal-engine-eval\runs\inspection-v2r8-static11-20260906` |
| workspace preflight digest | `sha256:91e376790f32601667a08ec46cdd5db269a8b89642c2a7e987a700e331164211` |
| 고정 Gate | 5/5 PASS, 전체 673개 테스트 88.471초 |
| 고정 Gate report SHA-256 | `df036d962c35e574500df1dc8680fb6f8809260880a36dd7425fa5eb81016f04` |
| prepare lock digest | `sha256:e2d78fe754f67361d1457c285d1a88b40355783aa03d40921c64cc5d21db8fef` |
| 역할 thread 저장 설정 | `role_threads_ephemeral=false` |
| 7차 대비 11개 요청 비교 | `instructions`, `output_schema`, `model`, `effort`, `timeout_seconds` 모두 동일 |
| 평가용 실제 모델 호출 | 0회, 실행 전 lock 검사 PASS |

source와 payload의 실제 경로·digest는 새 작업본에 재결속됐으며 이전 checkpoint를 사용하지 않았다. 다음 실행은 최대 11회, schema recovery 0회, 기존 900초 timeout과 같은 역할 설정을 유지한다.

## 실제 저장 조회 probe

평가 사례와 분리한 운영 probe를 위 작업본의 전용 Python으로 `Start-Process -WindowStyle Hidden` 실행했다. 시작 명령이 반환된 뒤에도 probe가 실행되어 모델 응답을 받았다. 첫 App Server를 닫고 새 App Server에서 `thread/read`만 호출해 같은 thread·turn·완료 상태·JSON 응답, turn 수 1을 확인했다. resume와 schema recovery는 없었다. 이는 종료 후 저장 조회의 직접 증거이며 모든 강제 종료 방식에 대한 생존 보장은 아니다.

| 항목 | 값 |
|---|---|
| probe 경로 | `D:\codex\fm-inspection-observations\durable-runtime-probe-20260906` |
| verification SHA-256 | `36f837a23712d0bb1bf7588db64ce0a78728a28122e5d075279ea5d48c6dc988` |
| thread | `01a073fe-ddf8-7422-8487-51e22c5687a4` |
| turn | `01a073fe-deaf-7f82-a69f-6081bbcc5472` |
| 확인한 모델·effort | `gpt-5.6-sol` / `xhigh` |
| input / output / reasoning token | 18,308 / 15 / 0 |
| latency / schema recovery | 5,797ms / 0회 |
| 결과 | 7개 관측 조건 모두 PASS |

probe의 `request`, 정책·inventory, thread/turn intent·receipt, terminal, 역할 result, 첫 App Server 종료 기록과 새 연결 관측을 별도 경로에 보존했다. 이 1회 호출은 static 11의 성공·실패·비용에 합치지 않는다. 과거 combined unknown도 그대로다.

## 검토 가능한 시작 명령

준비된 스크립트는 `D:\codex\fm-inspection-observations\v2r8-launch\start-diagnostic.ps1`이며 SHA-256은 `076b84e5a9aaf8c12ec793dcbd212c74f76150de5f767c7342bf020c3866785a`다. PowerShell 구문 검사를 통과했다. 스크립트는 정확한 위 lock·저장 설정·전체 입력을 실행 직전에 검사하고, 기존 실행·launch·로그가 있으면 중단하며, 새 launch intent를 독점 기록한 뒤에만 숨김 프로세스를 시작한다. 실제 11사례 시작은 아직 수행하지 않았다.

프로젝트 `AGENTS.md`의 “완료 관측이 없는 효과는 `external_unknown`으로 보존하고 입력 변경·새 Task·모델 변경으로 우회해 자동 재실행하지 않는다”는 경계 때문에, 이전 unknown을 보존한 별도 새 전수 실행에 대한 사용자 결정을 남긴 뒤 시작한다. 이는 정확한 Plan 활성화와 별개인 운영 재실행 결정이며, 아직 생성하지 않은 실제 Goal Plan을 승인받는 단계가 아니다.

위 내용은 준비 당시의 기록이다. 이후 현재 경계 검증에 대한 명시 요청으로 잠긴 8차 11사례를 한 번 수행했다. 9 PASS·2 의미 FAIL이며 전체 결과와 실제 Reviewer thread의 종료 후 조회·사후 감사는 [8차 경계 인계](inspection-v2r8-boundary-handoff.md)에 기록했다. 과거 unknown은 그대로 보존했고 같은 intent의 재개·재호출은 없다.
