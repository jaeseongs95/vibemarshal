# R-S06-04 — Goal의 검사 참조·금지 효과·대상 관측 경계

2026-09-04. [S06-RETRY-02](s06-planning-final-retry-handoff.md)의 후속 보완이다. 기존 원문·등록 자료·fixture·oracle·threshold·모델 배정을 유지하며, 과거 후보의 실제 결함과 역할의 과잉 판단을 구분한다. 실제 실행 원장과 원시 응답은 `.flowmarshal-engine-eval/runs/r-s06-04-20260904*`에 보존하고 Git에서 제외한다.

## 구현과 검증 범위

- [Goal 역할 공통 지침](../src/flowmarshal/engine/goal.py)은 전체 AC·제약·검증 목적과 명시된 검사 도구의 phase·절차를 함께 읽는다. 관측으로 확인한 참조의 검사 의미를 다른 AC에 반복하지 않은 것만으로 누락을 판정하지 않는다. 자료의 존재만으로 계약 채택을 추정하지 않으며 잘못된 phase·누락·불완전한 본문·명시적 제외나 충돌은 계속 검토한다.
- 정규화 필드 설명은 식별 가능한 검사 대상·범위·목적을 보존한다. 프로젝트 파일·의존성 변경 금지와 외부 서비스 변경·배포 금지는 새 의미 없이 분리한다.
- Goal 준비 시 사용자 명시 대상과 관측의 `project_root`를 대조한다. 역할용 복사 cwd나 참고자료 저장 위치만으로 다른 대상·stale·필수 selector 부재를 추정하지 않는다. 실제 대상 충돌과 불완전한 관측은 계속 근거에 따라 판단한다.
- [새 회귀](../tests/test_engine_goal_reference.py)는 명시 참조·참조 부재·잘못된 phase·효과 혼합·별도 역할 cwd의 입력 전달과 Compiler binding을 검사한다. Scripted 결과는 의미 탐지 품질의 증거가 아니다. 실제 모델의 판단은 아래 진단으로 별도 대조한다.

Core schema의 값 제약·권위 전이·finding 처리·모델 배정·재시도 한도를 바꾸지 않았다. 문자열 분류로 finding을 삭제하거나 특정 코드를 PASS로 치환하지 않는다. 장기 경계는 프로젝트 `AGENTS.md`, [권위 설계](orchestration-redesign.md), 시작 프로젝트의 `D:\codex\자동화템플릿\AGENTS.md`에 함께 반영했다.

[공식 App Server 문서](https://learn.chatgpt.com/ko-KR/docs/app-server)의 `config/read`가 반환하는 유효 구성과 기존 정책 검사기를 사용한다. 각 실제 호출 전 `:danger-full-access / never`와 model inventory를 확인하고, 실제 thread·turn의 정책·receipt·원시 사용량을 보존한다.

## 1차 진단의 실패와 보완 근거

1차 source는 `sha256:ec202728eb482f185846b5240cd054e017c9ecb93c00bdafde4fba541caa2988`이다. 493개 unittest·결정적 Gate 5/5·legacy freeze 40개는 통과했으나 **실제 진단은 FAIL**이다. 과거 파일 608개를 보존했다.

1. 정상으로 분류한 후보는 과거 실제 Goal에서 금지 효과만 분리한 입력이었다. Reviewer의 `GOAL_TASK_UNITTEST_VALIDATION_OMITTED`는 유효했다. 일반 AC의 unittest 실행과 독립 `goal phase`만으로는 원문이 요구한 Task 완료 전 unittest 검증·evidence 귀속을 보존하지 못한다. 메인과 독립 검토가 이를 확인했다.
2. `goal phase`만 `task phase`로 바꾼 후보는 `GOAL_TEST_PHASE_CONFLICT`로 거부됐다. 실제 검사 범위가 다른 phase의 참조를 정상으로 허용하지 않았다.
3. 과거 혼합 효과 후보에서는 Task unittest 귀속 누락과 로컬 의존성·외부 효과 혼합을 각각 지적했다. 키워드 호출을 누락으로 잘못 판단한 finding은 없었다.
4. 새 Normalizer는 Task unittest 귀속과 금지 효과 분리를 보존했지만, 역할용 복사 cwd를 관측 대상과 혼동해 blocking 질문과 가정을 추가했다. 관측 `project_root`와 검사 자료의 대상은 같은 S05 workspace였으므로 직접 근거 없는 차단이다.

1차는 logical calls 4회·provider turns 4회·schema recovery 0회, input 106,654·output 9,942, 총 **116,596 tokens**다. 원장 쓰기와 Goal·Plan 활성화는 0건이다. `semantic-assessment.json.passed=false`와 `verification.json.passed=false`를 유지하며, 입력·현재 adapter 재생·strict schema·정책·receipt·사용량 결속 검증은 별도로 통과했다.

원본 응답·판정과 당시 source snapshot을 저장한 뒤 한 차례 보완했다. 새 진단은 모든 비교 proposal의 `ac_003.validation_intent`에 원문이 요구한 Task unittest 실행·command/test evidence 귀속만 추가한다. 정상/잘못된 phase/효과 혼합의 차이는 유지한다. 이 수정은 진단 입력의 잘못된 정상 분류를 바로잡는 것이며, 과거 Goal이나 공식 fixture·oracle·합격선을 수정하지 않는다. Goal 역할의 cwd 해석 지침 보완도 새 source로 별도 검증한다.

## 최종 검증

최종 source의 **494개 unittest·결정적 Gate 5/5·legacy freeze 40개와 제한된 실제 Goal 진단은 PASS**다. 새 진단 root는 `.flowmarshal-engine-eval/runs/r-s06-04-20260904-v2`이며, 1차 기록과 당시 source를 포함한 과거 파일 695개를 보존한다.

| 검사 | 실제 결과 |
|---|---|
| 정상 명시 참조·효과 분리 | finding 없이 rating 반환, Compiler `ready` |
| 잘못된 Goal 검사 phase | `GOAL_TEST_PHASE_CONFLICT` error, `conflict` |
| 혼합 금지 효과 | `GOAL_EFFECT_POLICY_MIXES_LOCAL_AND_EXTERNAL` warning, `conflict` |
| 새 정규화 | Task/Goal unittest, 분리 Validator, 새 독립 goal phase, 분리 효과, 관측 project_root 보존. 질문·가정 없음 |

메인과 독립 보조 검토가 네 결과를 원문·관측과 대조했다. Normalizer의 출력은 진단 산출물이며 여기서 원장에 등록하거나 독립 Goal Reviewer의 실행 결과로 대체하지 않았다. 자연어부터의 새 정규화·독립 검토는 후속 S06에서 별도로 수행한다.

| 결속 | digest |
|---|---|
| 최종 source | `sha256:e72a69e3ec7a1b1446632604bd152c7dd910093da1b45e3cdb47946f09d37eff` |
| 결정적 계약 | `sha256:1bd596a5fd02f2d0ed83000711bcf2d2acdc8c2940b5a8a639c87d6b816f69b2` |
| 결정적 report | `sha256:760491c8ca4b313fcf0da3ea6648bc78276f0f96a4daa9683e04ef24319ae5e2` |
| 실제 진단 preflight | `sha256:729a2340e146f0e3fafd50a75b758c8bdbd90874cbbc1c356b3c1115a9d1210d` |

최종 진단은 logical calls 4회·provider turns 6회·schema recovery 2회이며 input **169,660**, output **8,435**, 합계 **178,095 tokens**다. usage unavailable은 0건이다. 두 진단 합계는 **294,691 tokens**이며 메인·보조 에이전트나 후속 S06 비용을 포함하지 않는다. 복구 전 응답과 오류를 보존했다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-04-20260904-v2/verify.py
```

`verification.json`의 의미 진단 PASS, binding 검증 PASS와 무호출 재검증을 구분해 보존한다. 현재 adapter로 요청을 재구성하고 원본 순서의 strict schema·turn intent/receipt·terminal·usage와 Compiler binding을 대조했다. 진단의 원장 쓰기·Plan 선택·활성화·Task 실행은 0건이다. 전체 qualification이나 1.0 통과를 뜻하지 않는다.

후속 [S06 재실행](s06-planning-goal-reference-handoff.md)은 같은 source의 새 원장에서 고정 원문부터 진행한다. 기존 source와 같은 결정적 Gate는 digest로 결속해 재사용한다.
