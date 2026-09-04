# R-S06-10 v3 제한 실제 진단 결과

## 판정

**FAIL — 첫 정상 `clean` 사례의 strict structured output schema 실패.** 실제 논리 호출과 provider turn은 각각 1회이며, schema recovery는 고정값 0회로 재시도하지 않았다. 남은 12개 구성과 생성 Plan의 독립 정상성 대조·Reviewer 호출은 미실행이다. 전체 qualification과 1.0 cutover는 계속 **NO-GO**다.

이번 v3는 이전 v1의 `prepare.log` 잠금 실패를 재개한 것이 아니다. 새 run root, 새 결정적 Gate, 새 fixture·기대값·지침·model inventory·source lock으로 준비했다. v2의 직접 파일 실행 import 오류와 사전 독립 대조 artifact 누락은 preflight 및 provider 효과 전에 끝난 launcher 준비 실패로 보존했으며, v3 판정에는 포함하지 않는다.

## 사전 결속과 잠금 검증

- 실행 source: Git `32a1c6c9dc9137456acd391255ae2c1696c31273`, manifest `sha256:ece76db8d2b3c56a4a5467b0b8f6649ac228956cb02f7c7c21015b00f99efc55`.
- 새 결정적 계약은 5/5 PASS다. compileall, 전체 `unittest` 528개, `pip check`, synthetic lifecycle, legacy freeze manifest를 통과했다. 계약 digest는 `sha256:251c7311eff8232d40a49130bd7373f4742757fc6c558c7f0bac24cddb505b1a`, report bytes digest는 `sha256:bf4f1be35a512568faf5579b446a4dc8f8ef02261b5181f55500ef8e6a708b6d`다.
- independent fixture review와 fixture assessment를 호출 전에 작성하고 lock에 포함했다. bytes digest는 각각 `sha256:ce54c9d7bf4b1a60070bf7c3300851869f6efe12528edd0bb82daa24562c4dc0`, `sha256:3df84ee51abe6a7e8fdf016c4315411b07240df1cea0e5f840e8dd94459fbe65`다.
- preflight lock digest는 `sha256:d7257c52e132a278ee14ebff73c9654b26cff262148d371d6aa86f4d896b8b25`이고 파일 bytes digest는 `sha256:0325b09bc67554828c20abae8f8ac83e636010f326c90efd0b8f832218b9690c`다. 179개 입력을 잠갔으며 실제 `verify_lock()`이 통과했다.
- `prepare.log`는 최상위 출력 로그로 잠금 목록에 없었다. workspace 내부 입력 로그와 JSON·schema·source·지침 snapshot은 계속 잠근다. 따라서 v1의 출력 로그 자체 변경 오류는 v3에서 재발하지 않았다.
- 실제 thread/start receipt의 `instructionSources`는 전역 `C:\Users\sjs95\.codex\AGENTS.md`, 제품 `D:\codex\flowmarshal\AGENTS.md`, 새 workspace `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-10-20260905-v3\workspace\AGENTS.md`의 잠긴 순서·내용 digest와 일치했다. 해당 digest는 순서대로 `sha256:6b1b3dbf5dc74c9b077f4c73099d4c54c1dc31b85376b19eafac7a5d5ead77ba`, `sha256:096d161fb23fc8e917d6a983ff6e3483552709dd5f5cde63ddf7ef894896c366`, `sha256:57fb50121f05548961e1e725e5537526b4e9cbc754383f8d42b0162096944b2a`다.

## 실제 첫 호출과 실패 근거

`clean`은 `compact_plan_reviewer`, `gpt-5.6-terra/high`로 새 빈 thread에서 실행됐다. 실제 thread receipt는 `:danger-full-access`와 `approval_policy=never`를 보고했다. request·prompt·strict schema·thread/turn·receipt binding 검증은 모두 통과했다.

그러나 모델이 반환한 reviewer payload는 `{"findings": [], "ratings": null}`이었다. `PlanReviewEnvelope`는 finding이 없을 때 fitness rating을 요구하므로 Pydantic validator가 이를 거부했다. receipt는 `schema_failed`이며 오류는 다음과 같다.

```text
finding이 없으면 fitness rating이 필요합니다.
```

이 오류는 정상 Plan의 의미상 0 finding을 승인하지 않는다. strict schema recovery가 0회로 잠겨 있으므로 같은 thread, 다른 모델, 수정된 prompt, 완화된 기대값으로 재호출하지 않았다.

## 호출·비용

| 항목 | 실제 값 |
|---|---:|
| 논리 호출 / 최대 | 1 / 13 |
| provider turn / 최대 | 1 / 13 |
| schema recovery | 0 |
| input tokens | 41,792 |
| cached input tokens | 0 |
| output tokens | 6,215 |
| reasoning output tokens | 782 (output에 포함) |
| total tokens | 48,007 |
| role latency | 116,750ms |
| provider duration | 116,152ms |
| 청구 금액 | provider receipt 미제공 |

이 수치는 새 빈 thread의 `thread/tokenUsage/updated.total`에서 얻었다. 이전 R-S06-08/R-S06-09의 호출은 input·projection·schema·지침 계약이 달라 비용 또는 정확성 개선 비교 대상이 아니다.

## 미실행 범위와 다음 작업

`bad`, `wrong-goal`, `combined`, `boundary-clean`, `missing-link`, `future-result`, `stored-expanded`, `semantic-explicit`, `stored-multi-defect`, `semantic-missing-link`, `expansion`, `expanded-review`는 실행하지 않았다. 생성 결과의 독립 정상성 대조, model의 정식 인용 일관성·필수 결함 검출, 전체 S06, Plan 활성화, Worker 실행, 운영 SQLite/파일 원장, 전체 qualification과 1.0 cutover도 이번 범위 밖이거나 미실행이다.

다음 작업은 이 `schema_failed` 출력이 발생한 실제 prompt·strict schema·shared instruction을 분석해 수정 필요성을 판단해야 한다. 수정한다면 새 source digest, 새 결정적 Gate, 새 run root와 새 preflight lock을 사용해야 하며 v3의 남은 12개 호출을 이어서 실행하면 안 된다.

## 증거

진단 root는 `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-10-20260905-v3`다.

- 결정적 Gate: `deterministic/qualification-report.json` (`sha256:bf4f1be35a512568faf5579b446a4dc8f8ef02261b5181f55500ef8e6a708b6d`)
- preflight와 지침 결속: `preflight.json` (`sha256:0325b09bc67554828c20abae8f8ac83e636010f326c90efd0b8f832218b9690c`), `instruction-binding.json` (`sha256:c3b4a6eefe78200aaa6bde3c5a971ecd9f201d04750f1ee0f1b4ba32cd258fe0`)
- 첫 호출: `calls/01-compact_plan_reviewer/thread.receipt.json` (`sha256:8a514063ba399072b0d319f846f50ba761c902f67595ed517620a1ab5bf7e785`), `turn.receipt.json` (`sha256:b10a6b5700471532d650565ee3421cdaa61c5839c595a2cdd195e04c0256082c`), `terminal.json` (`sha256:aa430693964cc7073adf5914be5d512b089cda9c99499372b240c5b3b8f8ee24`), `failed.json` (`sha256:96670168055d043cbb988380a27fa8fd5beab3c0ac1fe10b83819e52498b3777`)
- 최종 제한 진단 summary: `summary.json` (`sha256:e34728d5affab8626b914fb4385dfe97011b3f00711412353d486f4ddef47438`)

## R-S06-11 — 평점 조건 schema·관계표 실제 대조 보완

### 구현 범위

v3 원문은 수정하지 않고 `tests/fixtures/engine/plan-inspection-raw-v3-rejected.json`으로 옮겼다. fixture는 원래 terminal·failed·request·schema·developer instruction digest와 원시 response digest를 함께 보존한다. 빈 `findings`와 `ratings: null`은 과거 실패 응답 그대로이며, test가 이를 rating으로 보정하거나 PASS로 바꾸지 않는다. 이 원문은 사후 Pydantic 거부, `constraint_001`·`constraint_002`의 자기 constraint 인용 누락, 고정 clean 28행과의 relation 분류 차이 8건을 각각 검증한다.

`PlanReviewDraft`는 `findings`와 `ratings`를 모두 입력 필수 key로 둔다. `PlanReviewEnvelope`의 실제 provider schema는 review 속성 아래에 다음 두 object branch만 nested `anyOf`로 전송한다.

1. `findings: []`와 다섯 `ReviewRatings` (`goal_fit`, `grounding`, `engineering`, `verification`, `execution_safety`)의 0~4 정수 객체
2. 하나 이상의 `findings`와 `ratings: null`

이전 시도의 root `allOf`는 provider가 지원하지 않는다는 실제 400 응답을 받은 뒤 제거하지 않고, 같은 제약을 지원 표현인 nested `anyOf`로 옮겼다. local `$defs` reference도 branch 안에서 인라인 전개해 전송 schema가 독립적으로 두 분기를 강제한다. Pydantic 사후 validator는 동일 조합을 다시 검사한다. 누락 key, 빈 findings+null, findings/rating 동시 제출, range 밖 rating, `status`·`admissible`·`score`·`weakest_task` 같은 Core 권위 필드는 거부한다.

Reviewer 지침과 `PLAN_INSPECTION_INSTRUCTIONS` 앞부분에는 두 조합, `ratings:null`의 의미, 다섯 0~4 rating, Reviewer의 비권위 rating과 Core의 admission·0~100 종합 score 경계를 명시했다. 예시는 실제 schema와 validator 회귀에서 검증한다.

`assess_fixed_ac_validation_relations()`은 고정 clean AC×validation 표를 응답의 실제 `inspection.ac_validation_rows`와 대조한다. evaluator는 구조와 Goal이 같은 입력에만 이 표를 적용한다. validation pair 집합이 다른 결함 case나 생성 Plan에는 표를 강제하지 않으며, adapter·runtime·Core에 의미 정답, coverage 보정 또는 새 finding을 주입하지 않는다. `clean-assessment.json`에 실제 relation 차이를 남긴다.

또한 R-S06-10 fixture builder가 고정 독립 원문 검토를 새 run에 복사하고, runtime expectations bytes digest·정적 11개 Reviewer case 순서를 검증하도록 보완했다. 이로써 missing independent-review가 provider 호출 전 `prepare()`를 중단시키던 문제를 없앴다. `summary()`는 provider error의 `usage: null`도 보존하며 후속 요약 과정에서 실패하지 않게 했다.

`GoalContractRevision`, `PlanContractRevision`, `ReviewerSubmission`, DB schema 및 Core의 admission/score 계산은 변경하지 않았다.

### 결정적 검증

- 직접 추가한 transport/schema·원시 v3·fixture/결속 회귀 31건과 adapter 경계 회귀 24건을 통과했다.
- 최종 source의 결정적 Gate는 **5/5 PASS**다. `compileall`, 전체 unittest **530개**, `pip check`, synthetic lifecycle, legacy freeze manifest가 모두 통과했다.
- v6 결정적 계약 digest는 `sha256:ced6f696e0f88f2545f8aeaa24224d6dbf37391ab54f634b7b9d48b608055007`, report bytes digest는 `sha256:edd47dbd82fa236aae33ebc397fe85a34b65de6ca97c64f1752c21b3862941a4`다.

이 Gate는 구현·결속 검증일 뿐 실제 역할 품질, 전체 S06, 전체 qualification 또는 1.0 cutover의 PASS가 아니다.

### 제한 실제 진단

v5와 v6은 v3의 남은 호출을 재개하지 않은 새 run root다. 둘 다 실제 정책·모델 inventory·AGENTS source·원문 입력·기대값·projection·strict schema를 provider 효과 전에 잠갔다.

| run | 실제 결과 | 호출 / 최대 | token·latency |
|---|---|---:|---:|
| v5 | 첫 `clean` turn에서 provider가 root `allOf`를 `invalid_json_schema`로 거부했다. retry 0회, 다음 case 미실행. | 1 / 13 | usage 미제공(모든 token 0), 3,578ms |
| v6 | nested `anyOf` schema는 실제 `gpt-5.6-terra/high` turn에서 수용됐다. 정상 다섯 rating 응답도 Pydantic·receipt 결속을 통과했지만, 고정 관계표와 2건 불일치하여 즉시 FAIL이다. | 1 / 13 | input 42,715, cached 0, output 7,031(그 안 reasoning 924), total 49,746, role latency 131,515ms, provider duration 131,055ms |

v5 provider 오류의 실제 메시지는 다음과 같다.

```text
Invalid schema for response_format 'codex_output_schema': In context=(), 'allOf' is not permitted.
```

v6의 실제 review는 `findings: []` 및 다섯 항목 모두 4인 ratings였다. 형식·권위 경계·실제 thread/turn receipt와 source/instruction binding은 통과했지만, `clean`의 relation이 다음 두 행에서 고정 표와 달랐다.

- `ac_003 × val_goal_independent_behavior_contract`: expected `explicit_procedure`, actual `optional_or_unrelated`
- `ac_004 × val_task_scope_preservation`: expected `optional_or_unrelated`, actual `explicit_procedure`

따라서 semantic assessment가 FAIL을 기록했고 `bad`, `wrong-goal`, `combined`, `boundary-clean`, `missing-link`, `future-result`, `stored-expanded`, `semantic-explicit`, `stored-multi-defect`, `semantic-missing-link`, `expansion`, `expanded-review`는 실행하지 않았다. 생성 Plan의 독립 정상성 대조 및 마지막 Reviewer도 미실행이다. 모델 대체·schema recovery·rating 기본값·인용 자동 교정·expectation 완화는 수행하지 않았다. provider receipt에는 청구 금액이 없으므로 금액은 `null`이다. v3/v5/v6은 schema·지침·source 계약이 달라 성능 비교 대상이 아니다.

### 증거와 다음 조건

- v5 root: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-10-20260905-v5`
  - preflight lock `sha256:509cf28a57279ec0bb69222b1d1fb9b1028fa148db7a659187f54534ff1a1488`, terminal `sha256:47d5cebee3336c92fffd1088b95c50cb4856d554c15016209e9fbab0676e9f06`, failed receipt `sha256:009bb4b2113a5e0eb1d963364d29818e27192ca7b584b9b3bcd2c7887a7281aa`
- v6 root: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-10-20260905-v6`
  - preflight lock `sha256:7ee6bebd8b301458694815516124e17b9603c4291439fbdb5e823d9ab974ebb6`, summary `sha256:23bd87633fdf0a1856bc63f922411d97c5985bdf7b0d5b3271c9c93045460501`
  - actual schema `calls/01-compact_plan_reviewer/strict-schema.json` (`sha256:bfeee24e88b215a2e22ddc378fdcff1be966f16945d5cf381d90f4e998492c6e`), request `sha256:c2b234bea109913402cd07758a65ddc79b0266ec7d56dbad2e37d6f8d55b20b6`, terminal `sha256:9f1aed1218e73d5d025f23f5896b9cd0f5e3957def931567fe7df240d19fbfdc`, result/receipt `sha256:836dd2e68e3112027aed473cc64d988232286789f021aa34fe371768b50d4b74`, assessment `sha256:69840cf1544c47068de08edfccfa2fcabd9e4a414cc474227854c88ecfca8af1`

다음 실제 진단은 Reviewer가 위 두 relation을 올바르게 구분하도록 별도 변경을 한 뒤, 새 source digest·결정적 Gate·새 run root·새 preflight lock으로 시작해야 한다. v6의 남은 12개 호출을 이어 실행해서는 안 된다. 이 제한 진단은 전체 qualification이나 1.0 판정이 아니며, 현재 cutover는 계속 **NO-GO**다.
