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
