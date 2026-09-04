# 기능 Alpha A2 구현·검증 인계

- 완료 단위: A2 — 필수 Context 보장과 최소 Project Map 보완
- 기준일: 2026-09-04 KST
- 판정: A2 결정적 Gate 통과. 기능 Alpha 전체는 미완료이며 1.0은 `NO-GO`다.
- 실행 artifact: `.flowmarshal-engine-eval/runs/alpha-a2-20260903T235411Z`

## 기준선과 변경 범위

이전 세션의 [A1 인계 기록](alpha-a1-handoff.md)과 승인된 Alpha 계획을 이어받았다. 시작 HEAD는 `0639cc6ecdd00680310dc6297442ea2c284a18ff`이며 Git 작업 폴더에 기존 변경은 없었다. 시작 source digest는 다음과 같다.

```text
sha256:2a7014272df88f87746f46f491fec6a27481d0fce288cbb6acad661cd9950476
```

- `src/flowmarshal/engine/context.py`: 예산 적용 후 필수 need·정책의 누락 검사, 실제 AST 행 범위 선택·병합, 본문 기준 token 추정, 동일 본문 복원과 파일 freshness 검사, 기본·사용자 지정 운영 경로의 탐색 제외를 구현했다.
- `src/flowmarshal/engine/service.py`: 선택한 source·selector·본문을 최종 PromptBundle의 binding에 반영한다. 선택 이후 파일이 바뀌면 컴파일을 차단하고, Context 부족은 기존 서비스 오류와 호환되는 `ContextRequiredError`에 구조화 요청을 보존한다.
- `src/flowmarshal/engine/runtime.py`: 컴파일 중 Context가 부족하면 `BLOCKED / CONTEXT_REQUIRED`와 요청 JSON을 반환한다. Execution Spec·Attempt·Worker는 생성하지 않고 Task를 ready 상태로 유지한다.
- `src/flowmarshal/engine/execution.py`, `service.py`, `smoke.py`: 설정된 artifact root를 같은 방식으로 제외한다. Goal 관찰, State 재관측, 실행 준비와 합성 실행에서 제외 경로가 일치한다.
- `tests/test_engine_context.py`, `tests/test_engine_execution_automation.py`, `tests/test_engine_runtime_e2e.py`: A2의 누락·범위·freshness·실제 컴파일 연결·운영 artifact 회귀를 추가했다.
- `AGENTS.md`, `docs/orchestration-redesign.md`, `docs/engine-implementation-status.md`: 장기 불변조건과 현재 구현 범위를 반영했다. cutover ADR와 동결 기준은 검토했으며 변경하지 않았다.

권위 schema의 필드나 DB revision은 변경하지 않았다. 이전 평가 원장·artifact와 R1~R3.1 source도 변경하지 않았다.

## 구현된 동작

정책과 required need를 먼저 배정한다. 예산 때문에 필수 본문이 하나라도 제외되거나 실제 source·symbol을 찾지 못하면 누락 need의 원래 selector hint와 이유를 반환한다. 한 need가 여러 필수 파일에 매칭되는 경우도 모든 파일이 포함되어야 한다. 선택적 전체 파일 요청 때문에 이미 선택한 필수 symbol 범위의 비용이 커지면 그 선택 항목을 제외한다. 정책 파일은 예산 초과나 일반 파일 크기 제한을 이유로 조용히 누락하지 않는다.

Python 함수·async 함수·클래스·한정된 메서드 이름은 AST에서 decorator를 포함한 실제 행 범위를 추출한다. `python-lines:1-2,7-8`처럼 1기반 양끝 포함 범위로 표시하고 중복·중첩 범위를 병합한다. 경로만 요청하거나 지원하지 않는 형식·파싱 불가 파일은 `whole-file`로 표시한다. 범위 표현 한도를 넘으면 전체 파일로 확장한 뒤 예산을 다시 검사한다. 유효한 Python에서 요청한 symbol이 없으면 경로가 일치해도 해당 need를 충족했다고 처리하지 않는다.

파일 전체 byte digest는 freshness 기준으로 유지한다. 선택 비용과 Prompt 본문 조립은 같은 범위 복원 규칙을 사용하며 CRLF와 문자열 안의 Unicode separator도 보존한다. 선택 밖의 코드만 바뀌어도 실행 예약을 차단한다. 추정치는 실제 선택 문자열의 UTF-8 byte 수를 4로 나눈 올림값이고, 기존 4,000-token 상한은 제거했다.

기본 `.flowmarshal-engine`, `.flowmarshal-engine-eval`과 설정된 artifact root의 탐색을 가지치기한다. 운영 결과 추가로 Map·State를 갱신하지 않지만 실제 source 변경은 감지한다. 제외 경로의 자료라도 사용자가 명시적으로 등록한 참고자료·지침은 정상 입력이다.

## 검증과 evidence

최종 검증 source digest:

```text
sha256:9dec438e8afac8b692fac69075aa2974f3e0206d45a087c84ce59e554f88da26
```

최종 결정적 보고서 digest:

```text
sha256:c52c6f3b89c657d914f8a204902048fb2259ce9ec431223e18f41b7b59f7d974
```

| 검증 | 결과 |
|---|---|
| 전체 회귀 | 431 tests, OK. Engine 140개 포함 |
| compileall | PASS |
| pip check | PASS |
| synthetic lifecycle | evidence 기반 Goal 완료, PASS |
| legacy freeze | 40개 파일 PASS |
| 최종 Gate 전후 source digest | 일치 |
| diff 공백 검사 | PASS |

초기 16개 Context 회귀에서는 11 failures·3 errors로 기존 결함과 새 API 미구현을 재현했다. 원시 결과는 `context-before.log`에 보존했다. Windows 줄바꿈을 정규화해 비교하던 새 테스트의 기대값은 실제 파일 byte 기준으로 바로잡았고, CRLF와 Unicode separator 회귀로 별도 검증했다. 평가 합격선이나 기존 fixture oracle은 변경하지 않았다.

최종 권위 검증 근거는 아래에 있다. 경로는 저장소 root 기준이다.

- `.flowmarshal-engine-eval/runs/alpha-a2-20260903T235411Z/baseline.json`
- `.flowmarshal-engine-eval/runs/alpha-a2-20260903T235411Z/a2-verification.json`
- `.flowmarshal-engine-eval/runs/alpha-a2-20260903T235411Z/deterministic/qualification-report.json`
- `.flowmarshal-engine-eval/runs/alpha-a2-20260903T235411Z/deterministic/cells/seed-0/`
- `.flowmarshal-engine-eval/runs/alpha-a2-20260903T235411Z/all-tests.log`
- `.flowmarshal-engine-eval/runs/alpha-a2-20260903T235411Z/legacy-freeze-after.json`

최종 Gate는 기존 `run_deterministic(root=..., run_root=...)`로 새 평가 계약과 immutable cell을 생성했다. 실제 모델 호출, 전체 역할·Planning campaign, 실제 E2E와 36-cell 성능 평가는 실행하지 않았다. 이번 결정적 PASS를 실제 역할 품질이나 Alpha 전체 성공으로 확대하지 않는다.

## 남은 결함과 A3 착수 조건

다음 완료 단위는 A3의 실제 Worker Prompt 전달 경로 통합이다. 현재 컴파일 과정의 메모리 내 PromptBundle에는 선택 범위·본문이 반영되고 그 binding이 Execution Spec에 저장된다. **bundle 본문 자체의 불변 artifact 저장과 Dispatcher의 실제 Worker 전송 연결은 아직 남아 있다.** 실제 전송을 검증했다는 의미가 아니다.

A3에서는 다음을 이어서 구현한다.

1. Task 계약·운영상세 projection·선택 Context로 bundle을 만들고, binding digest로 식별되는 불변 artifact를 원자적으로 저장한 뒤 명세를 등록한다. 자기참조 binding·파생 spec digest는 본문에서 제외한다.
2. 초기 실행과 재개 모두 저장한 bundle의 binding·segment digest를 검증해 실제 본문을 전송한다. 임의 Prompt, artifact 누락·변조로 우회할 수 없게 한다.
3. 재개 안내문까지 포함한 최종 전송 문자열을 turn intent의 `prompt_digest`에 기록한다. semantic Validator는 실행 후 evidence로 독립 입력을 구성한다.
4. A2의 `read_context_fragment`와 파일 전체 freshness 검사를 유지하고 기존 정확한 Plan 활성화·직렬 실행 원칙을 보존한다.

모듈의 import·전역 값까지 자동으로 추적하는 dependency closure는 이번 범위에 포함하지 않는다. 필요하면 별도 Context need나 whole-file 요청을 사용한다. token 추정치는 Context 본문만 다루며 Prompt의 다른 영역이나 provider 실측 비용을 대신하지 않는다. Context 부족에 따른 Task 분할도 자동 적용하지 않는다.

A1~A3의 결정적 검증이 닫힌 뒤 A4·A5에서 실제 자연어 bugfix의 모든 Task·독립 Goal Test·중단 후 재개를 검증한다.

## 다음 작업의 모델·추론 수준 추천

사용자의 후속 요청에 따라 이후 완료 단위 인계에서도 다음 작업의 모델·추론 수준과 선택 이유를 함께 제시한다. 구체적인 모델명은 실행별 인계 문서에 기록한다.

**A3 구현의 기본 추천은 `gpt-5.6-sol` / `high`다.** 불변 artifact 저장, digest의 자기참조 방지, Core 명세 등록과 Dispatcher의 초기 실행·재개가 여러 모듈에 걸쳐 있으므로 복합 코드 작업에 맞는 모델과 높은 추론 수준을 권한다. 구현 범위와 종료 조건은 이미 확정되어 있으므로 `high`로 시작하고, 경쟁 조건이나 중단·재개 일관성의 원인이 좁혀지지 않을 때 `xhigh`를 검토한다. 이는 작업 특성에 따른 판단이며 A3의 모델 비교 실측 결과는 아니다.

이 세션의 Codex 도구에 표시된 모델 목록에 해당 조합이 있으며, 공식 문서에서도 Sol의 복합 작업 용도와 `high`·`xhigh` 지원을 확인했다. 실제 다음 실행 전에는 현재 inventory에서 지원 여부를 다시 확인한다. 추천만 기록했으며 실행 설정은 변경하지 않았다. [공식 GPT-5.6 Sol 문서](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
