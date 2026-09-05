# Plan inspection v2 6차 부분 실행 기준선

## 실행 결속

- source commit: `de2aae353149c97e5ea728d501b78a5f0834a19b`
- 고정 checkout: `D:\codex\fm-inspection-v2r6`
- 실행 root: `D:\codex\fm-inspection-v2r6\.flowmarshal-engine-eval\runs\inspection-v2r6-static11-20260906`
- 독립 입력 package manifest: `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866`
- Codex executable: `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d`
- preflight binding: `sha256:a0e3c056b0cb19300277ae567090698529a85e92f961df8c8f258a34fc368341`
- input lock: `sha256:1522abf56f4dec5085661604bf890dbb0952ca2ea56bd4aeaa853cb175963716`
- 역할: `gpt-5.6-sol` / `xhigh`, fallback 없음
- 실제 정책: `:danger-full-access`, `approval_policy=never`

고정 checkout의 결정적 Gate는 전체 671개 테스트와 함께 5/5를 통과했다. evaluation contract는 `sha256:801757cf8fdf8d0f45889204b8839828365e5676464df86b160ab5a5fca3ad1a`, 결정적 report digest는 `sha256:52d00a1bdf738c62af20d9538c355fc74460c4f3ff1dda1927f2390d4fe139d5`다.

## 관측 결과

11개 고정 사례 중 네 번째 호출에서 중단됐다.

| 순서 | 사례 | 결과 | 직접 관측 |
|---:|---|---|---|
| 1 | `clean` | PASS | schema·compiler·binding·고정 의미 기대 통과 |
| 2 | `bad` | semantic FAIL | 의도한 `TASK_ORACLE_BEHAVIOR_SCOPE_OVERCLAIM` finding은 검출했으나 AC 관계 2개 과잉 |
| 3 | `wrong-goal` | PASS | 의도한 goal phase 결함과 28개 AC 관계 기대 통과 |
| 4 | `combined` | `external_unknown` | 900초 role turn timeout, terminal/result/usage 미확보 |
| 5~11 | 나머지 7개 | NOT_RUN | 불명확한 provider intent 뒤 전체 중단 |

logical call과 provider turn은 각각 4/11, schema recovery는 0이다. 완료된 세 호출은 모두 strict schema와 compiler를 통과했다. `clean`, `bad`, `wrong-goal`의 provider duration은 각각 178,758ms, 256,880ms, 329,840ms이고 total token은 61,713, 65,400, 68,218이다. `combined`는 900,187ms 뒤 usage를 확보하지 못했으므로 전체 token·provider duration 합계는 unavailable이다.

source·workspace·지침·격리 입력·보존 원본은 모두 unchanged로 확인됐다. 새 원장 기록, Plan 활성화, Worker 실행은 없었다. `summary.json` SHA-256은 `a8eb83bb5411f389a7d46c55dcd9f7acf2d81e01255545d9f0b4ee8b5209dcf0`이다.

## 의미 실패 원인

`ac_004`는 Task 단계에서 `oracle.py task phase`를 명시하고, Goal 단계에서 동작·공개 계약·파일 보존과 새 command·test·file·diff evidence를 요구한다. Reviewer는 올바른 Task oracle과 세 Goal validation 외에 다음 두 Task validation까지 양의 관계로 선택했다.

- `ac_004 × val_task_unittest`
- `ac_004 × val_task_scope_preservation`

두 검사는 전역 Task 검증으로는 필요하지만 `ac_004`가 Task 단계에서 명시한 절차는 아니다. 6차에 추가한 “validation 단계의 열거 책임” 규칙이 Goal Test의 세 책임은 올바르게 복원했으나, `Task 검증과 별도로`라는 경계 표현을 Task validation 전체의 묶음 의무로 확대했다. 이는 finding target catalog 보정의 실패가 아니라 AC 의미 선택의 과잉이다.

## timeout intent 관측

`combined`의 thread는 `01a073bd-2157-7003-9a9b-9ada13a541e1`, turn은 `01a073bd-24d0-7800-9c52-045c1833adc3`, call ID는 `model_call_ed31202592c84385a2fa4786c806dafe`다. 기존 runner는 deadline에서 interrupt를 요청했지만 terminal receipt를 보존하지 못했다. 고정 executable을 새로 열어 `thread/read`만 수행한 후속 관측은 `thread not loaded`로 실패했다. 관측 artifact는 `D:\codex\fm-inspection-observations\v2r6-combined-20260906\thread-read-observation.json`, SHA-256은 `76dcc2d8e5ca86dc8b06b89b5f5fc6a7cd90818cc46d7e368e4c0decb01aeb07`이다.

완료·실패 terminal과 usage를 확인할 수 없으므로 이 intent는 계속 `external_unknown`이다. 같은 intent를 resume하거나 재호출하지 않는다. 후속 검증은 변경된 source·schema·prompt에 결속한 새 실행으로만 수행한다.

## 판정

6차는 복합 finding target 조립 실패를 제거하고 `wrong-goal`을 회복했다. 11사례 전수 결과는 확보하지 못했고 `bad` 의미 오차와 provider timeout이 남았으므로 S06·전체 qualification·Functional Alpha·1.0 cutover는 `NO-GO`다.
