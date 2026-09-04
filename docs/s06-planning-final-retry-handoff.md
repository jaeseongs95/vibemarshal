# S06 재평가 — Goal 검토의 과잉 finding과 금지 효과 분리 경고

2026-09-04, `S06-RETRY-02`. [R-S06-03](r-s06-03-handoff.md)의 최종 진단 PASS 이후 고정 원문부터 새 원장에서 Goal 정규화·독립 검토를 수행했다. **Goal은 `conflict`, S06은 선택 Plan 없이 FAIL**이다. Goal 활성화·Skeleton·Plan·Task 실행은 없다. 전체 Trace는 `INCOMPLETE`, 1.0은 `NO-GO`를 유지한다.

Reviewer는 키워드 호출 검증 누락 error와 로컬·외부 금지 효과 혼합 warning을 제출했다. 전체 Goal과 등록 검사 도구를 별도 대조한 결과 **첫 finding은 과잉 검토, 두 번째는 유효한 표현 분리 경고**로 판단했다. 실제 finding과 Core의 `conflict`는 수정하지 않았다.

**다음 단위는 R-S06-04 — Goal의 등록 검사 도구 참조를 전체 계약으로 검토하고 금지 효과를 분리하는 보완**이다. 현재 후보에서 finding을 삭제하거나 Plan 단계로 강제 진행하지 않는다.

## 고정 입력

- source: `sha256:72922b8c3f475bcf59d3a6a7da4e10dc6726cf812bd5feff7a07ffc4f3f3b4e9`
- 로컬 실행 root: `.flowmarshal-engine-eval/runs/s06-bugfix-trace-20260904-v3`
- preflight: `sha256:915174be750c828c6941ede93f4dd3f9be4bd9f30a6636efc0e4bdba453f4b77`
- 과거 파일 567개 보존, 새 SQLite 원장과 별도 artifact root 사용.
- 원문·profile·등록 참고자료·fixture·oracle·threshold·역할별 model/effort를 보존한다. 참고자료는 복사 전 과거 preflight의 input digest를 대조한다.
- 현재 source와 같은 결정적 계약·report의 489개 테스트·Gate 5/5 PASS를 결속한다. source가 같으므로 테스트를 중복 실행하지 않는다.
- 활성 Goal과 Plan이 없으며 Plan 단계는 시작하지 않았다.

수집기는 기존 CLI의 실제 요청·strict schema·turn intent/receipt·terminal·usage를 저장한다. Core 원장 상태와 후보 판정에 개입하지 않으며, 과거 draft나 진단 Plan을 수동 수정해 활성화하지 않는다.

## 실제 finding과 독립 대조

`GOAL_TEST_KEYWORD_INVOCATION_COVERAGE_OMITTED`는 `artifact:goal_proposal`, `source:observation_005`를 참조했다. 다음 세 AC를 함께 읽어야 한다.

| 실제 Goal 항목 | 검증 의미 |
|---|---|
| `ac_001` | 7개 정수 쌍을 위치 인자로 호출하고 기대 합과 비교 |
| `ac_002` | 위치·키워드 호출 계약 보존과 시그니처·annotation 확인 |
| `ac_005` | 모든 Task 검증 뒤 기존 validation 도구의 **goal phase를 새로 실행** |

등록 `validation-reference.md`는 해당 goal phase를 **7개 정수 쌍 각각의 위치·키워드 호출**, 공개 계약과 파일 보존을 검사하는 기존 도구 실행으로 정의한다. `ac_005`의 명시 참조가 키워드 호출 검사를 포함하므로 특정 AC에 같은 문장을 반복하지 않았다는 이유만으로 전체 Goal의 누락을 단정할 수 없다. 단순히 원본 자료에 요구가 존재해서 상속을 추정한 판단이 아니라, 실제 Goal이 그 검사 도구의 정확한 phase 실행을 명시한 데 근거한다.

메인 에이전트도 처음에는 정규화 누락으로 잠정 설명했으나 전체 계약을 대조한 뒤 정정했다. 독립 보조 검토도 같은 결론이었다. 실제 응답·finding은 보존하고 `semantic-assessment.json`에 별도 판단으로 남겼다.

`PROHIBITED_EFFECT_MIXES_LOCAL_AND_EXTERNAL_EFFECTS`는 외부 서비스 변경·배포와 로컬 프로젝트의 새 의존성 추가 금지를 한 문자열에 묶은 점을 지적했다. Goal의 `prohibited_effects`에는 typed `external` flag가 없지만 [현재 Goal 지침과 필드 설명](../src/flowmarshal/engine/goal.py)은 로컬·외부 효과를 별도 항목으로 작성하도록 요구한다. 따라서 이 warning은 표현 분리 대상으로 타당하다. 범위 확대나 실제 외부 효과 발생을 뜻하지 않는다. 현 Core는 finding이 있으면 Goal을 `conflict`로 기록하므로 첫 error를 분석상 과잉 검토로 판단해도 이번 Goal을 임의로 활성화하지 않는다.

## 원장·호출 검증과 사용량

새 원장의 byte 복사본을 `mode=ro&immutable=1`로 읽어 integrity·foreign key·Engine application ID, Profile revision 1→2와 등록 참고자료, Goal 원문·preparation binding, history hash chain 7건을 확인했다.

- Goal revision 1개: `goal_revision_bda7d627bc8045a396fd19bd57828a28`, 상태 `conflict`.
- Goal definition digest: `sha256:53deb0aa5b1d9369e76d359378d57e8e0bc4b93b5f7eba36976e39fdac31a33b`.
- 활성 Goal·Plan은 null. Skeleton·Plan·Task·활성화·Execution Spec·Attempt·Worker runtime intent/receipt·validation·evidence·GoalVerdict는 모두 0건.
- 준비 역할 BudgetUsageRecord는 2건이다. Worker runtime 영역의 0건을 실제 Goal 모델 호출 0건으로 해석하지 않는다.
- `plan.started.json`과 Plan 호출 디렉터리는 없다. Goal CLI 종료 코드 0은 structured 결과 수집 성공이며 Goal 준비 PASS가 아니다.

현재 adapter로 두 요청을 재구성해 저장 request digest를 대조했다. 원래 필드 선언 순서의 strict schema, 실제 turn intent/receipt/terminal, 최종 payload, model/effort·inventory·정책과 원장 usage receipt도 일치했다. 추가 모델 호출과 원장 쓰기는 없다.

| 역할 | logical calls | provider turns | input tokens | output tokens |
|---|---:|---:|---:|---:|
| Goal normalizer | 1 | 1 | 25,452 | 1,112 |
| Goal reviewer | 1 | 1 | 26,367 | 3,170 |
| 합계 | 2 | 2 | 51,819 | 4,282 |

총 **56,101 tokens**, schema recovery 0회, usage unavailable 0건이다. 메인·보조 에이전트와 이전 R-S06-03 비용은 제외한다. Plan·Worker·Validator·Goal Test 비용은 발생하지 않았다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/s06-bugfix-trace-20260904-v3/verify_goal_conflict.py
```

`goal-conflict-verification.json.verification_passed=true`, `pipeline_passed=false`를 함께 보존한다. 초기 수집기 대조의 Python tuple/JSON list와 strict schema 필드 선언 순서 문제를 수정했으며, 원장·provider 결과·제품 source·판정에는 개입하지 않았다. 준비된 `verify_trace.py`는 Goal ready와 Plan 결과를 요구하므로 이번 중단 상태에 사용하지 않았다. 실제 응답·원장·실행 이력은 Git에서 제외한다.

## 다음 Repair의 완료 조건

1. Reviewer가 전체 AC·constraint와 명시된 등록 검사 도구·phase의 의미를 함께 읽도록 한다. 단순 원본 존재와 계약의 명시 참조를 구분하며 실제 키워드 호출 누락은 계속 거부한다.
2. Normalizer가 로컬 의존성 추가 금지와 외부 변경·배포 금지를 별도 항목으로 보존하게 한다. 허용 효과나 의미를 추가하지 않는다.
3. 정상 참조·실제 검사 누락·금지 효과 혼합의 paired 회귀와 제한된 실제 Goal 진단을 수행한다. 기존 raw finding·평가 기준은 바꾸지 않는다.
4. 통과 후 새 source lock의 S06에서 실제 선택 Plan을 확보한다. S07 전 정확한 Plan revision·digest 활성화 계약은 유지한다.
