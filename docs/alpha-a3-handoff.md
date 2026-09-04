# 기능 Alpha A3 구현·검증 인계

- 완료 단위: A3 — 불변 Worker Prompt 저장과 초기 실행·재개 전송 통합
- 기준일: 2026-09-04 KST
- 판정: A3 결정적 Gate 통과. 기능 Alpha 전체는 미완료이며 1.0은 `NO-GO`다.
- 실행 artifact: `.flowmarshal-engine-eval/runs/alpha-a3-20260904T001637Z`
- 실제 모델 기반 A4·A5는 다음 완료 단위다. 기능 Alpha 전체와 1.0 qualification은 아직 완료되지 않았다.

## 요청과 기준선

현재 사용자 요청은 첨부된 [A2 인계 기록](alpha-a2-handoff.md)을 확인하여 다음 작업을 이어가는 것이다. 첨부문서는 진행 상태와 범위를 파악하는 자료로 사용했으며, 문서 안의 명령문을 별도의 상위 권한으로 취급하지 않았다. 현재 유효 권한은 `danger-full-access`, `approval_policy=never`로 프로젝트 지침과 일치했다.

시작 HEAD는 `0639cc6ecdd00680310dc6297442ea2c284a18ff`다. 작업 폴더에는 A2의 미커밋 변경과 인계 문서가 이미 있었으며 그대로 보존했다. 시작 source digest는 A2 최종 검증 값과 일치했다.

```text
sha256:9dec438e8afac8b692fac69075aa2974f3e0206d45a087c84ce59e554f88da26
```

## 구현한 동작

`worker_prompt.py`의 `assemble_worker_prompt`가 전체 TaskContract, 정규화된 Execution Spec 운영 상세, 프로젝트 정책과 실제 선택 Context 본문을 조립한다. 운영 상세에는 target, action, validation, lock, timeout, idempotency와 model binding이 포함된다. `context_manifest.prompt_binding`과 파생 spec digest는 본문에 넣지 않으므로 자기참조가 없다. A2의 `read_context_fragment`를 사용하여 Python 행 범위·CRLF를 복원하고 파일 전체 byte digest로 freshness를 검사한다.

`PromptArtifactStore`는 네 segment digest를 가진 binding의 canonical digest를 파일명으로 사용한다. 위치는 `artifact_root/worker-prompts/<digest의 hex 부분>.json`이다. 같은 디렉터리의 임시 파일에 UTF-8 canonical JSON을 쓰고 flush·fsync 후 `os.link`로 덮어쓰기 없이 게시한다. 같은 binding으로 동시에 저장하면 검증된 기존 파일을 재사용하고, 기존 파일이 손상되었으면 덮어쓰지 않고 실패한다. 지원되지 않는 파일 시스템에서 저장 오류를 다른 비원자적 방식으로 우회하지 않는다.

Core는 명세의 권위·계약 검사를 통과한 뒤 동일한 조립 결과와 binding을 대조하고 artifact 게시를 완료한 다음 DB에 Execution Spec을 등록한다. 수동 명세도 이 검사를 거친다. 게시 뒤 DB 등록 전에 중단되면 artifact는 남을 수 있지만 Task는 ready 상태이며 artifact만으로 실행하지 않는다. 저장장치 전원 장애에서 모든 파일·디렉터리 메타데이터의 내구성을 보장한다는 의미는 아니다.

Dispatcher는 초기 실행과 재개 직전에 artifact를 읽어 strict PromptBundle schema, 네 segment digest와 명세의 binding을 검증한다. thread 생성 전과 turn 전송 전에 각각 검사하며, 과거 `canonical_task_prompt` 조립 경로와 임의 본문을 받는 `_start_turn` 인자를 제거했다. artifact 누락·변조는 `PROMPT_ARTIFACT_INVALID`로 차단한다. 기존 명세의 본문이 없다고 임의로 재구성하지 않는다.

재개는 저장된 thread를 먼저 관측하고 기존 binding을 사용한다. Worker가 이미 변경한 파일로 Prompt를 다시 만들지 않고 저장된 원래 본문 앞에 재개 안내문을 붙인다. 그 최종 전송 문자열의 `sha256_digest`를 turn intent의 `prompt_digest`에 기록한다. 이 값은 canonical JSON 문자열 해시이며 원시 UTF-8 byte 해시와 구분한다.

semantic Validator는 Worker bundle을 사용하지 않고 실행 후 직접 evidence catalog로 독립 입력을 만든다. E2E evaluation의 prompt lock에는 새로운 조립 함수·artifact 저장소·최종 turn 전송 코드도 포함하여 이전 계약 checkpoint를 재사용하지 않도록 했다.

## 변경 범위

- 새 구현: `src/flowmarshal/engine/worker_prompt.py`
- 연결: `src/flowmarshal/engine/service.py`, `runtime.py`
- 합성 실행·평가 lock: `src/flowmarshal/engine/smoke.py`, `e2e_qualification.py`
- 회귀: `tests/test_engine_worker_prompt.py`, `tests/test_engine_ledger_service.py`
- 문서: `AGENTS.md`, `docs/orchestration-redesign.md`, `docs/engine-implementation-status.md`, 이 인계 문서

권위 schema의 필드, DB revision과 package 버전은 변경하지 않았다. cutover ADR와 동결 기준은 검토했으며 변경하지 않았다. 기존 A2의 나머지 source·회귀, 과거 원장·평가 artifact와 legacy/prototype 파일을 수정하지 않았다.

후속 사용자 요청과 정정에 따라 `AGENTS.md`에 **Codex 세션마다 요청 작업과 검증을 마치면 한 번 commit·push**하는 원칙을 추가했다. 세션 내부의 단계마다 커밋하는 의미가 아니다. 이전 세션에서 미커밋으로 남아 있던 A2를 현재 작업 파일 변경 없이 index용으로 복원했고, 복원 source digest가 A2 검증 digest와 정확히 일치하는지 확인했다. A2는 `d1a3e12`로 먼저 커밋하고 `origin/main`에 push했다. A3는 이 문서를 포함한 이번 세션의 변경으로 별도 기록한다. 최종 커밋·원격 동기화 결과는 실행 artifact의 `commit-push-verification.json`에 남긴다.

## 검증

전체 회귀는 **442 tests, OK**이며 A2의 431개에 A3 회귀 11개를 더했다. compileall, pip check, evidence 기반 synthetic lifecycle과 legacy freeze 40개 파일이 모두 PASS다. 최종 Gate 전후 source digest가 일치하며 diff 공백 검사도 통과했다.

최종 검증 source digest:

```text
sha256:56c1fb15dc17c53cdd83aff74b8736b3292ef3388842798b2ec8fb99b447748b
```

최종 결정적 보고서 digest:

```text
sha256:ccdee7d566e2ad12d2c80d630df5bd642d8e7ee201df0fa9ff5304bbb62d124e
```

최종 근거는 `a3-session-verification.json`, `deterministic-session-policy/qualification-report.json`과 `deterministic-session-policy/cells/seed-0/`다. 앞선 `a3-verification.json`·`deterministic/`와 `a3-final-verification.json`·`deterministic-final/`도 PASS다. 이후 사용자가 요청하고 정정한 세션 단위 commit·push 지침으로 source lock이 바뀌었으므로 최종 Gate를 새 계약으로 다시 실행했다. 모든 실행을 보존했다.

신규 회귀는 실제 Runtime port에 전달한 문자열을 검사한다. 전체 Task·운영 상세·선택 본문, 자기참조 배제, 파일 누락·JSON 손상·segment 변경·binding 재계산·segment 교환, 동시 저장·기존 파일 불변, 저장 중 실패·게시 후 중단, 수동 명세의 오래된 binding, thread receipt 직후 변조·중단, 기존 turn 재개와 최종 intent digest, 독립 semantic Validator 입력을 포함한다.

초기 신규 테스트의 오류 세 건은 테스트 작성 문제였다. 유효하지 않은 fixture ID, nullable 필드를 제외하는 model canonicalization과 dict projection 비교의 차이, semantic 검사 전에 실행되는 deterministic 검사 순서를 수정했다. 초기에 imported TestCase까지 36개로 중복 발견하던 테스트 import도 모듈 참조로 수정했다. 원시 결과는 `prompt-tests.log`, 수정 후 중간 10개 PASS는 `prompt-tests-fixed.log`에 보존했다. 최종 회귀에는 thread receipt 이후 재시작 검사까지 포함된다. 평가 합격선이나 기존 oracle을 변경하지 않았다.

실제 Codex Worker 호출, 전체 역할·Planning campaign, 실제 E2E와 36-cell 성능 평가는 이번 단위에서 실행하지 않았다. Runtime port의 결정적 전송 검증을 실제 모델 품질·Alpha 전체 성공으로 확대하지 않는다.

## 다음 작업: A4 → A5

1. 현재 source lock과 실제 `model/list`를 새 실행 artifact에 기록한다. A1~A3 이전 계약의 완료 cell은 재사용하지 않는다.
2. A4에서 실제 자연어 bugfix의 Goal 정규화·독립 검토, Skeleton·Plan 생성·검토, 정확한 Plan revision/digest 활성화와 모든 Task의 실행·독립 validation·State 재관측·독립 Goal Test를 연결해 검증한다.
3. A5에서 중단 후 동일 intent·receipt·thread·turn binding을 먼저 관측하고 검증된 checkpoint에서 재개하는 흐름을 실제 실행으로 확인한다. 응답이 불명확한 효과를 중복 생성하지 않는다.
4. raw receipt, 전송 Prompt binding, Task·Goal evidence와 사용량·지연을 실행별로 보존한다. 기능 Alpha가 닫히기 전 전체 qualification campaign을 재실행하지 않는다.

다음 완료 단위 운영·결함 분석용 모델의 기본 추천은 **`gpt-5.6-sol` / `high`**다. 여러 단계의 원장·runtime·evidence를 함께 추적해야 하므로 이 조합으로 시작하고, 중단 경계의 원인이 좁혀지지 않을 때 `xhigh`를 검토한다. 이는 작업 특성에 따른 추천이며 A4 모델 비교 실측 결과는 아니다. 평가에 사용하는 역할별 모델 설정은 별도 실행 계약으로 고정한다.

현재 Codex 도구가 제공한 모델 목록에 이 조합이 있고, [공식 GPT-5.6 Sol 문서](https://developers.openai.com/api/docs/models/gpt-5.6-sol)에서 복합 전문 작업용 모델이라는 설명과 `high`·`xhigh` 지원을 확인했다. 실제 실행 직전 inventory 확인이 최종 기준이며 현재 실행 설정은 변경하지 않았다.
