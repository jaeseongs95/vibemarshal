# AC 원문별 검사 의무 분리

## 근거와 변경 범위

8차 독립 11사례는 9 PASS·2 의미 FAIL이었다. 두 실패 모두 같은 Goal의 AC-004 `validation_intent`가 명시한 Task oracle 절차를 빠뜨렸다. `stored-expanded`와 `stored-multi-defect`의 응답에는 해당 Task phase를 실제 수행하는 supported scope가 있었으며, adapter는 모델이 선택하지 않은 관계를 그대로 false로 전개했다. 직접 원문·응답·기대표 근거는 [8차 경계 인계](inspection-v2r8-boundary-handoff.md)에 있다.

현재 입력 누락이나 compiler join 오류는 확인되지 않았다. 같은 Goal을 받은 `clean`에서는 Task phase를 선택했으므로, 모든 Plan 형태에서 Goal 의무를 일관되게 보존하지 못한 것이 관측된 실패다. AC statement의 독립 Goal Test 설명과 validation_intent의 task/goal 절차를 한 목록으로 바로 연결하는 표현이 누락에 기여했는지는 아직 검증할 가설이다. 이번 변경을 의미 정확도 개선이 증명된 결과로 부르지 않는다.

## 새 직접 제출물과 결정적 전개

`ac_scope_requirements`는 모든 AC를 정확히 한 행씩 제출하며 다음 필드를 사용한다.

| 필드 | 책임 |
|---|---|
| `criterion_id` | 원문 AC 식별 |
| `statement_scope_ids` | statement가 명시적으로 요구한 실제 절차의 supported scope를 모델이 선택 |
| `validation_intent_scope_ids` | validation_intent가 명시적으로 요구한 실제 절차의 supported scope를 모델이 선택 |

두 원문은 전체 AC 문맥으로 해석하는 상호 보완 자료다. 한 원문의 단계·독립성 설명으로 다른 원문의 명시적 절차를 없애지 않는다. 특정 원문에 검사 절차 요구가 없으면 해당 목록을 비운다. 같은 절차를 두 원문이 각각 명시하면 같은 scope가 양쪽 목록에 있을 수 있다. 이는 두 의미 관계이며, 반복된 전체 AC×validation 장부는 아니다.

Adapter는 두 목록을 순서를 보존하는 합집합으로 만들고 scope의 소유 validation을 join한다. 이 결과로 기존의 전체 bool 행렬·scope ID 집합·근거 closure·coverage witness를 계산한다. 모델이 합집합이나 validation ID를 다시 제출하지 않는다. 원문별 선택은 raw 응답에 그대로 남으며 adapter가 옮기거나 채우지 않는다.

단계의 경계·순서와 해당 단계에 명시한 검사 책임을 구분하는 기존 의미 규칙을 유지한다. 특정 사례 ID·정답 scope·oracle 기대값을 prompt에 넣지 않는다. AC 설명은 전용 envelope 지침에 모으고 역할 trace 지침에서 같은 설명을 반복하던 부분을 줄였다. Goal·Plan 권위 schema, v1, finding taxonomy, 고정 기대표, 역할 선택과 최대 11회·recovery 0회·실패 중단 경계는 변경하지 않는다.

이는 한 Reviewer 응답 안에서 원문별 판단을 구분하는 변경이다. Goal 의미를 별도 모델 단계에서 한 번 정규화해 여러 Plan에 재사용하는 기능을 구현한 것은 아니다. 그 확장은 새 의미 artifact의 독립 검토·유효성·실제 pipeline 결속과 호출 예산까지 함께 설계해야 하므로 이번 실패 두 건만으로 채택하지 않는다.

## 검증 기준

변경 관련 26개 테스트가 통과했다. 새 회귀는 서로 다른 원문 목록의 선택을 모두 보존하는 합집합, 양쪽에서 선택한 같은 scope의 중복 방지, 빈 선택의 임의 보충 금지, 모든 AC 행의 존재, 두 원문 목록의 존재하지 않는 scope·non-supported scope 거부를 검사한다. 기존 회귀는 직접 의미 필드 보존, finding과 closure 결속, 잘못된 관계의 고정 기대표 FAIL을 유지한다. Scripted fixture의 필드 배치는 adapter 검증 입력이며 실제 모델의 원문 해석이 옳다는 증거가 아니다.

개발 결정적 Gate는 전체 677개 테스트(84.787초), compileall, pip-check, synthetic lifecycle, legacy freeze의 5/5를 통과했다. artifact는 `.flowmarshal-engine-eval/runs/inspection-v2r9-source-devgate/deterministic`이며 contract는 `sha256:1ae196248be58c339549d1c18d65825889c6c6f81ed0e85bae523d955b1db577`, report SHA-256은 `7cbdb346df010b1b11d1576f01bf46edc2281547e9f191873355257a25a16665`다. `git diff --check`도 통과했다. 이 Gate가 통과해도 8차의 FAIL이나 과거 unknown은 변하지 않는다. 새로운 source·schema·지침을 고정한 worktree와 독립 실행 root에서 실제 11사례를 관측해야 한다.

실제 판정은 기존 fixture package `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866`의 사례 순서·전체 기대 관계·기대 finding을 유지한다. 두 실패뿐 아니라 기존 성공 9건도 모두 통과해야 한다. 실제 receipt의 token·latency를 같은 전체 사례 집합과 비교하며 원문별 목록을 추가한 비용도 포함한다. 각 원문 목록의 의미 적합성은 형식 검증만으로 증명되지 않으며 기존 고정 기대표는 합집합으로 전개한 AC×validation 관계와 finding을 평가한다.

11사례 모두 통과한 새 결과가 있어야 S06 qualification 13과 실제 Goal 경로로 진행할 수 있다. 제품 기본 provider와 cutover 상태는 각각 v1·NO-GO를 유지한다.

## 고정 실행 준비와 시작

구현을 `92b11e1a5948c2721b958e03db28d5c82dbff7b9`로 commit·push했다. 고정 worktree `D:\codex\fm-inspection-v2r9`는 같은 detached HEAD와 전용 Python 3.12.14를 사용하며, source manifest는 `sha256:498937a7dbe24485ab917d714a9222eb975dcb5caf57ef639db43a21f484d33a`다. package와 두 legacy 감사 입력을 byte 보존하고 Python·import origin·설치 버전을 결속했다.

고정본의 Gate도 677개 테스트(85.243초)를 포함해 5/5로 완료됐다. report SHA-256은 `31598b6709ebf851ef730f1ec707a42b65bc7f7f62ffdab7b7543eb42a6e5fa9`이며 개발 Gate와 같은 contract다. 중단 뒤 기존 완료 artifact와 실행 프로세스의 부재를 확인했으며 Gate를 재실행하지 않았다.

실행 root는 `D:\codex\fm-inspection-v2r9\.flowmarshal-engine-eval\runs\inspection-v2r9-static11-20260906`이다. workspace preflight는 `sha256:2427c8f076203448a0c82a06399463b5ee078c1e840cf73264aa528c33e4f86e`, prepare lock은 `sha256:60a5f06880c904b454a9c0b064e01a10dd9e579a2abe361b923edcd32f7a6eaf`로 통과했다. fresh inventory·실제 지침·정책, 동일 Sol/xhigh 역할·호출 순서·11개 기대표·Goal 원문과 저장형 thread를 확인했다. 8차 원본 638개 파일도 모두 보존됐다.

운영 기록은 `D:\codex\fm-inspection-observations\v2r9-launch`에 있다. `pre-launch-verification.json`의 21개 검사를 통과한 뒤, `2026-09-06T01:40:58.9101965Z`에 숨김 프로세스 PID 35088로 진단을 한 번 시작했다. launcher SHA-256은 `b12783033563760152eaf8a136e57bda3fb81de5623805b9b0b2de3de72e382b`다. 이 시점의 상태는 **실제 11사례 검증 진행 중**이며 최종 PASS·의미 개선·S06 완료를 뜻하지 않는다. 중단 뒤에는 PID·시작 시각과 기존 artifact를 먼저 관측하고, PID가 사라져도 기존 provider thread를 재개 없이 확인한 뒤 상태를 분류한다. 같은 launcher나 미완료 사례를 자동 재호출하지 않는다.
