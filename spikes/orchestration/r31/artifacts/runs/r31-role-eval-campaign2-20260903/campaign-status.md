# R3.1 전체 역할 평가 campaign 상태

## 현재 상태

- campaign: `r31-role-eval-campaign2-20260903`
- 상태: **PARTIAL**
- 전체 cell: 150개 (`50 fixtures × 3 order seeds`)
- 완료 cell: 1개
- 남은 cell: 149개
- 예상 전체 실제 모델 호출: 228회
- 현재 R3.1 판정: **GO 보류**

계정의 주간 Codex 사용량이 2026-09-03 확인 시점에 98%였고 추가 크레딧이 없어, 한도 소진으로 불완전한 호출을 양산하지 않도록 이번 실행은 새 cell 1개에서 멈췄다. 현재 한도 reset 시각은 2026-09-07 11:27:31 KST로 관측됐다.

## Campaign 결속

- manifest: [campaign-manifest.json](campaign-manifest.json)
- model lock: [model-lock.json](model-lock.json)
- 첫 실행 receipt: [invocation-4e5b62fa3df543ce83cd9bc2666ac78c.json](campaign-invocations/invocation-4e5b62fa3df543ce83cd9bc2666ac78c.json)
- manifest digest: `sha256:1dbba531380f896251d8eab13b62def64e32bcb03169c0bfd4858d10cd8c6bf7`
- fixture catalog digest: `sha256:68be20c5c11b66e0c0b4eea45a23e006f2e70dda5cb98f40856e6d85980dd717`
- prompt·output schema 평가 계약 digest: `sha256:0998b50b6a084fefd4c675e8b262f6abf536efa6193d6244849e2374781353ad`
- 역할 model lock digest: `sha256:e317891fa9155dd5f30d2c6661cdfd4d10ef71eab3b116aaa9ee147a615ec8ff`

manifest와 model lock이 다른 campaign 결과는 합치지 않는다. 각 `(fixture, seed)` 완료 결과에는 모델의 원시 structured assessment, 최종 observation, model-call receipt와 실제 권한 증거가 함께 저장된다.

## 완료된 첫 cell

- 내부 fixture: `P11-clean`, order seed `1`
- 모델 노출 ref: `case-20b60272c0095ebd`
- 결과 artifact: [result.json](cells/seed-1/case-20b60272c0095ebd/result.json)
- 역할: 일반 Hard Gate reviewer
- 판정: admissible
- requirement coverage: 통과
- 실질적으로 다른 후보 2개 보존: 통과
- 중복 signature Top-2 진입 없음: 통과
- 선택 후보 독립 복원: 통과
- validation 성공 허위 주장 없음: 통과
- schema: 1회 recovery 후 성공
- 사용량: 53,341 token
- model latency: 22,274 ms

이 한 cell만 떼어 계산하면 첫 출력 schema 준수율이 0%이므로 부분 보고는 실패다. 최종 합격선은 전체 150개 cell 중 첫 출력 준수율 90% 이상이며, 현재 한 건만으로 전체 통과·실패를 확정하지 않는다.

## 보존한 선행 시도

`r31-role-eval-campaign1-20260903`은 첫 실제 호출 뒤 긴 Windows artifact 경로의 최종 receipt 저장이 실패한 시도와, 경로 단축 후 저장에 성공했지만 복원 배열의 의미가 모호했던 v1 cell을 보존한다. 이 관측으로 다음을 수정했다.

- 실제 Runner journal을 짧은 campaign 전용 경로에 저장
- dependency edge와 failure/validation WorkItem ref 의미를 output schema에 명시
- 원시 structured assessment 저장
- fixture catalog뿐 아니라 prompt·output schema 평가 계약 digest 결속

계약이 달라진 campaign 1의 cell은 campaign 2에 재사용하지 않았다.

## 재개 명령

```powershell
flowmarshal-planner-r31-eval run-models `
  --artifact-root D:\codex\flowmarshal\spikes\orchestration\r31\artifacts\runs\r31-role-eval-campaign2-20260903 `
  --skill-root D:\codex\자동화템플릿\prototypes\skills\flowmarshal-work-planner `
  --role-config D:\codex\flowmarshal\spikes\orchestration\r31\artifacts\runs\r31-live-pilot-20260902\role-config.json `
  --codex-bin C:\Users\sjs95\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe `
  --full-catalog --order-seeds 1,2,3 --resume
```

재개 시 manifest와 model lock을 먼저 검증하고 완료된 첫 cell은 다시 호출하지 않는다. 한 번에 실행할 새 cell 수를 제한하려면 `--max-new-cells N`을 추가한다.
