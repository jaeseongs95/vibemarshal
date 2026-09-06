# S06 재진입 기록

9차 새 계약의 독립 static 11사례가 전부 통과해 S06 제한 qualification 13단계에 진입했다. [9차 완료 결과](inspection-v2r9-source-requirements.md)는 선결 조건의 근거이며, 이 문서의 S06 실행 결과나 실제 Goal 완료를 대신하지 않는다.

## 계약과 준비

| 항목 | 결속 |
|---|---|
| 고정 worktree | `D:\codex\fm-inspection-v2r9` |
| detached HEAD | `92b11e1a5948c2721b958e03db28d5c82dbff7b9` |
| source manifest | `sha256:498937a7dbe24485ab917d714a9222eb975dcb5caf57ef639db43a21f484d33a` |
| 실행 root | `D:\codex\fm-inspection-v2r9\.flowmarshal-engine-eval\runs\inspection-v2r9-qualification13-20260906` |
| 실행 모드·provider | `qualification` · `plan-inspection-v2` |
| workspace preflight | `sha256:83c5924ec14ef5681ce52b22457c750e0b535a5c88053bcc88a4cf3d1a08b031` |
| prepare lock | `sha256:b2416a8cf3c6f6e73fcaaf7db65103ca4a0fcc97fab0692c5ac8a4bc673587c6` |
| fixture package | `D:\codex\fm-inspection-inputs\r32-v1`, manifest `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866` |
| Codex executable | `D:\codex\fm-inspection-runtime\codex-935a1911.exe`, SHA-256 `935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |
| 역할 설정 | 고정본의 `plan-inspection-general-reviewer-sol-xhigh-roles.json` · Reviewer Sol/xhigh, Expander Luna/high · fallback 없음 |
| 최대 호출·recovery | logical/provider 각각 13회 · schema recovery 0회 |
| 역할 thread | 저장형, `ephemeral=false` |

같은 고정 worktree·전용 Python·source에서 완료된 677개 포함 결정적 Gate 5/5를 재사용했다. contract `sha256:1ae196248be58c339549d1c18d65825889c6c6f81ed0e85bae523d955b1db577`와 현재 source 일치를 확인하고 Gate artifact 9개를 byte 그대로 보존했다. 이미 끝난 전체 테스트를 반복하지 않았으며 static 모델 응답·checkpoint를 S06의 PASS로 복사하지 않았다. 새 preflight·prepare는 fresh inventory·실제 정책·지침·새 request/expectation binding을 확인했다.

## 실행과 다음 확인 지점

13단계는 `static 11 → expansion → 독립 생성 검토 → expanded-review` 순서다. 독립 생성 검토는 13회 provider 호출과 별도의 의미 검토 경계다. 이 모드에서는 첫 실패에서 중단한다. 첫 프로세스는 최대 12회까지만 호출하며, 생성 Plan을 직접 검토해 고정 criteria·전체 AC 관계·원문 입력 digest를 확인한 뒤에만 마지막 Reviewer를 호출할 수 있다.

운영 기록은 `D:\codex\fm-inspection-observations\s06-reentry-20260906`에 있다. `deterministic-evidence-reuse.json`은 Gate 재사용의 provenance, `pre-launch-verification.json`은 21개 사전 확인을 보존한다. launcher SHA-256은 `335cf4f9a1c0db2f740b6a4750de542ec720c05a6b5f0f74b2796b40bada112e`다. 첫 구간을 `2026-09-06T02:23:14.2109950Z`에 숨김 Python 프로세스 PID 36100으로 한 번 시작했다.

## 첫 구간의 완료 결과

실행은 `2026-09-06T02:26:59.327882Z`에 첫 `clean` 사례의 **semantic FAIL**로 종료됐다. 나머지 12단계는 NOT_RUN이며 logical/provider 호출은 1/1, schema recovery는 0회다. summary SHA-256은 `dea5930fdf841f10f95bff0ee2f77cf153a05724a34074b02aaf27bfefc869c4`다. 종료된 PID 36100이나 같은 실패 사례를 재시작하지 않았다.

28개 AC×validation 기대 관계는 전부 일치했다. 실패 원인은 예상에 없던 `TASK_PRODUCES_OWN_FUTURE_VALIDATOR_EVIDENCE` finding 하나다. 모델은 `result_order`와 `val_task_validator_review`를 선택하고 Plan의 `/definition/tasks/0/produces/3`에 있는 `evidence:task_validator_review`를 직접 근거로 제출했다. transport·schema·compiler·요청/응답 결속은 통과했으며 external_unknown·incomplete·provider terminal failure는 0이다. 이 실패는 9차의 AC 원문 선택이나 adapter 참조 전개 오류로 분류하지 않는다.

종료 뒤 새 App Server에서 같은 저장 thread `01a07486-f5cb-7033-ad93-a44c31afabc6`, turn `01a07486-f9d0-7551-b680-203d72e365c2`의 completed 상태와 전체 최종 응답 일치를 확인했다. `terminal-result-verification.json`의 19개 확인이 통과했고 SHA-256은 `71dd193937c3961e16f3cf446dd0300666241b8eebdacc710145dd3e7e5ffe54`다. 두 실행의 lock·summary 입력 digest, S06 파일 451개와 static 파일 638개의 보존도 확인했다. 사후 확인은 모델 호출·resume 없이 수행했다.

provider 보고 input/output은 52,814/12,034 token, total은 64,848, cached input은 0, output에 포함된 reasoning은 10,358이다. receipt latency는 221,641ms, provider duration은 220,497ms다. 청구 금액은 미제공이며 단일 실패 응답을 성능 비교나 구독 차감량으로 환산하지 않는다.

## Task 산출물과 Worker 제출의 원인 구분

`clean`의 `produces`는 Task의 논리적 산출물 목록이다. 실제 [TaskContract](../src/flowmarshal/engine/domain.py), [DAG 검사](../src/flowmarshal/engine/planning.py)와 [Worker Prompt](../src/flowmarshal/engine/worker_prompt.py)를 대조하면 이 목록을 Worker 응답의 필수 항목으로 직접 검사하거나 Validator 결과의 작성 주체를 Worker로 지정하는 처리는 없다. clean의 완료 조건과 semantic validation은 독립 Validator가 직접 file·diff evidence를 검토하고 별도 결과를 제출한다고 명시한다. precondition에도 미래 Validator 결과 요구가 없다.

반면 고정 `future-result` 사례는 같은 `produces` 목록을 유지하면서 완료 조건에 “실행 역할과 분리된 Validator의 직접 검토 결과가 Worker 응답 본문으로 제출된다”를 명시한다. 해당 Validator는 Worker 응답을 입력으로 사용하므로 두 문장의 결합이 실제 순서 충돌 근거다. static 11의 Reviewer는 이 직접 완료 조건을 인용해 `FUTURE_VALIDATOR_RESULT_REQUIRED_IN_WORKER_RESPONSE`를 올바르게 검출했다.

따라서 이번 추가 finding은 Task 전체의 산출물을 Worker의 직접 제출 책임으로 읽은 의미 오판으로 판단한다. 기존 공유 지침도 Worker 제출과 이후 Task 검증을 구분하지만 `produces` 필드 자체의 책임 범위는 schema에 설명되어 있지 않다. 다음 보완은 이 일반적인 필드 의미를 생성·검토 입력에서 명확히 하는 범위로 제한하며, 특정 evidence 이름의 finding을 삭제하거나 과거 FAIL·고정 기대표를 바꾸지 않는다. 새 계약에서는 정상적인 Task 검증 산출물과 실제 미래 결과 선제 제출 요구를 함께 검증해야 한다.

## 전체 목표의 남은 완료 근거

S06의 13단계 전체 PASS 뒤에는 자연어 요청에서 생성한 Goal과 선택한 Plan을 실제 실행·독립 검증·GoalVerdict까지 같은 계보로 연결해야 한다. CLI의 `goal create --live`, `plan search --live --inspection-contract plan-inspection-v2`, 정확한 revision/digest 활성화, `run once`, `report final` 경로를 확인했다. 아직 이 실제 경로는 실행하지 않았다.

기존 `project-e2e` harness는 고정 Goal·Plan과 검토 제출물을 구성해 실행 경계를 검사한다. 그 결과만으로 자연어 Goal 생성부터 실제 선택 Plan의 완료까지 이어졌다고 판정하지 않는다. 준비·검토 receipt, 원장의 활성 Goal/Plan, execution/validation evidence와 독립 GoalVerdict가 함께 필요하다. 사용자에 의한 정확한 Plan revision 활성화 경계와 Core만 완료를 판정하는 권위도 유지한다.

제품 기본 provider는 v1이며 Functional Alpha·1.0 cutover와 정식 token/latency Gate는 미완료다. S06 일부 PASS나 9차 static PASS로 이 범위를 완료 처리하지 않는다.
