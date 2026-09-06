# 길 찾기 알고리즘과 현재 개발 방향의 비교

2026-09-06 사용자 요청에 따라 Codex 대화 [진행 상황 확인](codex://threads/01a07497-550a-7f70-a018-0c1e7877825c), 현재 코드와 새 실행 결과를 대조했다. 결론은 **Reviewer 계약 보정만 이어가는 우선순위를 바꾸고, 상세 Plan의 실패를 다음 후보에 반영하는 탐색 루프를 보완할 근거가 있다**는 것이다. 전체 계획 알고리즘이 잘못됐거나 특정 모델의 생성 능력이 부족하다고 단정할 근거는 아직 없다.

## 연결된 대화와 현재 상태의 차이

연결된 대화는 `D:\codex\자동화템플릿\audit-evidence\2026-09-05-direction\current-development-status.json`과 9월 5일 감사보고서를 읽었다. 근거는 R-S06-19, 커밋 `e886f3e`, 당시 558개 테스트였다. 최신 저장소나 이 작업의 9차 실행 결과를 직접 확인한 답변은 아니었다. 과거의 phase 과장 문제를 설명하는 데는 유효하지만 현재 진행 상태로 적용할 수 없다.

| 비교점 | 연결된 대화의 근거 | 현재 직접 확인 |
|---|---|---|
| 검증 상태 | R-S06-19 첫 clean 구조 실패 후 보완 | 9차 static 11/11 PASS, 고정 AC 관계 252개 일치 |
| 실패 성격 | Plan의 검사 능력 과장을 놓쳤던 과거 문제 | 새 S06 clean에서 정상 Task 산출물을 Worker의 미래 결과 요구로 오판 |
| 다음 전진 | 검사 계약의 실제 재검증 | 실제 Plan 생성·수정·선택·실행의 연결 증거가 필요 |

9차 static 진단은 이미 주어진 11개 Plan을 검토한다. 이 성공이나 실패는 그 실행에서 Planner가 좋은 경로를 생성했는지, 실패 후 더 나은 경로를 찾았는지를 측정하지 않는다. 첫 S06 재진입도 clean에서 끝나 새 expansion을 호출하지 않았다. 따라서 최근 검사 결과만으로 Planner의 생성 실패율을 단정해서는 안 된다.

## 검색 코드에서 확인한 빈틈

현재 [SkeletonFirstPlanner.search](../src/flowmarshal/engine/planning.py)는 다음 순서다.

1. Skeleton 후보 생성과 결정적·의미 검토.
2. 수정 가능한 초기 Skeleton 실패를 한 번 정제하고 재검토.
3. 통과한 Skeleton의 중복 제거·순위·shortlist 선정.
4. 각 shortlist를 상세 Plan으로 한 번 확장하고 검토.
5. 통과한 Plan 중 하나를 선택하거나 선택 없이 종료.

상세 Plan에서 새로 발견한 finding을 Expander 또는 Skeleton 정제로 돌려보내는 단계가 4와 5 사이에 없다. 다음 shortlist가 있으면 그 기존 후보를 검사할 수 있지만, 해당 실패 근거를 받아 수정된 후보를 생성하는 경로는 아니다. `PlanExpander` protocol에도 현재 `expand`만 있다. 이 검색 코드 파일은 최초 분석 기준 `fd09bf1` 이후 현재 recovery HEAD까지 변경되지 않았다.

원장의 [실행 이후 recovery](../src/flowmarshal/engine/service.py)는 별도로 존재하며 실패 분류·새 evidence·한도를 검사한다. 따라서 “제품에 재계획 기능이 전혀 없다”는 결론은 틀리다. 확인한 빈틈은 **활성화 전 상세 Plan 탐색에서의 실패 피드백**이다.

## 모델 호출 없는 제어 흐름 재현

기존 계획 테스트의 합성 입력을 사용하고, Plan Reviewer가 직접 Plan 근거를 가진 `remediable=true` finding을 제출하도록 했다. 실제 모델·Worker·활성화는 호출하지 않았다.

| 관측 | 결과 |
|---|---:|
| Skeleton 판정 | admissible |
| 상세 Plan 판정 | needs_revision |
| expansion / Plan review 호출 | 1 / 1 |
| 실패 뒤 Skeleton refinement | 0 |
| 사용 / 최대 논리 호출 | 4 / 14 |
| 남은 호출 예산 | 10 |
| budget_exhausted | false |
| 선택된 Plan | 없음 |

재현과 6개 확인은 `D:\codex\fm-inspection-observations\search-algorithm-review-20260906\run_planner_repair_probe.py` 및 `planner-repair-probe.json`에 있다. 검색 코드 SHA-256은 `eb2b9db2403590692d30cb32c77dd847f5a77629a2dd906246ec4af011cb5190`이다. 이 재현은 제어 흐름의 한계를 증명하며 실제 모델의 의미 정확도나 발생 빈도는 증명하지 않는다.

## 권장 변경과 수용 기준

**첫 우선순위는 상세 Plan에 대한 제한된 수정·재검토 루프다.** 원본 Goal·Plan·finding과 직접 evidence를 보존하고 수정 가능한 상세 계약 결함을 다음 Plan 작성 입력으로 돌려보낸다. Task 목적·DAG를 바꿔야 하면 Skeleton 단계로, validation 표현·연결을 고칠 문제면 상세화 단계로 분류한다. 새 Plan은 기존 Core 검토와 정확한 revision 활성화 경계를 통과해야 한다.

거절이 모두 올바른 수정 지시는 아니다. 최근 clean처럼 원문과 맞지 않는 finding에는 반증 근거를 제시할 수 있어야 한다. 동일 입력을 결과가 통과할 때까지 재호출하거나 adapter가 finding을 삭제하는 방식으로 해결하지 않는다. 원문 대조와 재검토를 제한된 예산 안에서 기록하고, 해소되지 않은 판단 충돌은 미해결 상태로 남긴다.

반복 한도는 기존 검색 예산에 포함한다. 상세 후보 revision과 review 호출까지 계산하고, 같은 결함·동일 후보가 새 근거 없이 되풀이되면 종료한다. 성공 기준은 “첫 Plan에 오류가 하나도 없음”뿐 아니라 “직접 근거가 있는 수정 가능한 실패 뒤, 제한 안에서 더 나은 Plan을 선택함”이어야 한다.

그다음에는 동일한 자연어 요청·Goal 계보에서 생성 → 검토 → 필요한 수정 → 선택 → 정확한 Plan 활성화 → Task 실행 → 독립 Goal Test → Core GoalVerdict를 연결한다. 첫 feasible Plan까지의 호출·시간, 수정 후 개선 여부, 동일 실패 반복, 실제 Goal 완료를 기록한다. 기존 static 정밀도·검출률·schema 통과는 함께 유지하되 이 실제 성과를 대체하지 않는다.

검사 능력·phase 혼동이 생성 단계에서 반복된다는 직접 근거가 쌓이면, 등록 수단의 phase·입출력·지원 검사·근거 digest를 가진 능력 catalog를 Planner의 선택 입력으로 제공하는 변경을 검토한다. 프로그램은 등록된 사실과 참조를 관리하고 AC와 검사 의미의 적합성은 모델이 판단한다. 이 catalog를 새 정답 장부나 또 하나의 과도한 출력 계약으로 만들지 않는다.

## 검증 gate와 현재 보존 상태

[activate_plan](../src/flowmarshal/engine/service.py)은 정확한 digest, Core admissible 판정, 활성 Goal·State·Project Map의 일치를 요구한다. CLI의 명시적 v2 계획 탐색이나 이 활성화 함수는 전체 qualification PASS를 전역 선행조건으로 조회하지 않는다. qualification과 cutover는 제품 승격 조건이다. 실제 Goal 경로를 개발·관측하는 일과 제품 기본 provider·1.0 승격을 구분할 수 있으며, 실패한 qualification을 PASS로 바꿀 필요는 없다. 이는 계획 승인이나 개별 실행 검증을 생략한다는 뜻이 아니다.

사용자 요청 직전에 작성한 [10차 Task 필드 설명](inspection-v2r10-task-result-context.md)은 집중 28개와 결정적 Gate 5/5를 통과한 미검증 후보로 보존한다. 새 static·qualification 모델 호출은 시작하지 않았다. 이 설명 보완의 완료를 실제 탐색 루프 개선으로 계산하지 않는다. 현재 S06은 FAIL, 실제 Goal 경로는 NOT_RUN, 제품 기본 provider는 v1·cutover는 NO-GO다.

이 문서는 방향 판단과 실제 코드 재현 결과다. 상세 Plan 재탐색 알고리즘 자체의 구현·실모델 검증을 완료한 기록은 아니다.
