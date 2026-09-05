# R-S06-25 diagnostics 보정 이후 제한 역할 검증 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 결과

**FAIL — 첫 clean 호출의 성공 result·receipt·terminal 결속은 통과했지만 고정 AC 연결 기대값 3개가 불일치했다.** logical/provider/recovery는 **1/1/0**, 상한은 13/13/0이다. 첫 assessment FAIL에서 중단했고 이후 12사례는 NOT_RUN이다. 기존 `run`은 정확히 한 번 실행했으며 exit 0을 의미 검증 PASS로 해석하지 않았다.

새 결정론 Gate **5/5 PASS**, fresh inventory와 v2 prepare PASS, clean의 구조·호출 결속 PASS와 의미 assessment FAIL을 구분한다. 최종 `summary.json`은 첫 실패 뒤 한 번 게시되었고 `status=FAIL`, `diagnostic_errors=[]`다. `outcomes.success=1`은 구조화 역할 결과의 성공이며 제한 검증 전체의 성공이 아니다.

기존 **S06 FAIL, Functional Alpha 미완료, 1.0 NO-GO**를 유지한다. Plan activation·Worker·제품 원장 쓰기는 0이며 전체 qualification과 S07 이후는 NOT_RUN이다.

## 실제 권한과 기준

첫 파일 조회·명령 실행 전에 현재 turn의 개발자 `<permissions instructions>`에 명시된 `sandbox_mode=danger-full-access`, `approval_policy=never`를 직접 확인하고 첫 응답에 남겼다. 승인 질문·권한 상승 요청은 없었다. prepare의 실제 정책과 역할 receipt도 `:danger-full-access/never`다.

- 시작 `HEAD/main/origin/main`: 모두 `0d6ea1abbb3c1e576519944d4a2f502517513d6a`, 작업 트리 clean.
- `git ls-remote origin refs/heads/main`도 같은 커밋이었다. origin은 `https://github.com/jaeseongs95/flowmarshal.git`, `gh repo view`로 `isPrivate=true`를 확인했다.
- 적용 프로젝트 지침은 루트 `AGENTS.md`다. 상위 경로와 docs에는 추가 적용 지침·override가 없고, 조회한 설정에 별도 fallback filename 지정은 없었다.
- [R24 인계](r-s06-24-post-diagnostics-fix-limited-validation-handoff.md), [실패 안전 summary 보정](r-s06-diagnostics-failure-summary-fix-handoff.md), [R19 CLOSE](r-s06-19-close-handoff.md), [R17 규칙·실행 경계](r-s06-17-ac-link-phase-rule-handoff.md)를 현재 진단기와 대조했다. 권위 설계의 AC 관계·v2 lock 규칙과 cutover ADR도 적용했다.
- 개발 세션 thread는 환경에서 확인한 `01a06ff5-0f36-7923-9662-8d90065b55fa`다. 현재 개발 turn ID는 환경에 제공되지 않아 null이며 추정하지 않았다. 실제 제품 역할 thread/turn은 아래 receipt로 확인했다.

사용자가 이번 요청에서 확정한 경계는 **각 사례의 실제 성공 binding-verification과 assessment PASS 뒤에만 다음 사례로 진입하고, 최종 summary는 전체 완료 또는 첫 실패 뒤 게시하는 것**이다. 이는 현재 `RecordedRunner.run()`과 `execute()`의 순서와 일치한다. R24의 과거 진입 실패와 artifact는 provenance로 보존하며 checkpoint로 사용하지 않았다. 이 경계 적용에 source·Goal·Plan revision이나 추가 사용자 판단은 필요하지 않았다.

## 새 root·결정론 Gate·prepare

이 문서의 로컬 증거 root는 `.flowmarshal-engine-eval/runs/r-s06-19-post-diagnostics-fix-r25-20260905-v1`이다. 처음에 부재함을 확인하고 생성했다. 기존 CLI가 허용하는 `r-s06-19-*` prefix를 사용하며 R25 식별은 `assignment.json`에 기록했다. 진단기 내부 `session=R-S06-19` 필드는 기존 source 그대로이고, 과거 run을 재개했다는 의미가 아니다.

과거 Gate의 실행 환경 전체 동일성을 입증할 증거가 부족하여 재사용하지 않았다. 새 root의 `deterministic/`에서 기존 Gate를 한 번 실행했다. Python 3.12.14, Windows 11, interpreter bytes digest와 설치 package·선택된 Python 환경 변수는 `source-before.json`, `gate.started.json`, `gate.completed.json`에 보존했다. 실행 전후 환경, 현재 `_deterministic_contract()` 전체 객체, source, report와 5개 완료 cell을 직접 대조했다.

| Gate | 실제 결과 |
|---|---|
| compileall | PASS |
| 전체 unittest | **583 tests OK**, 65.198초 |
| pip check | PASS |
| synthetic lifecycle | PASS, 실제 제품 Worker 호출 아님 |
| legacy freeze | **40파일 PASS**, 누락·변경·예상 밖 경로 0 |

Gate 실행 시각은 14:08:21~14:09:30 KST, 실제 하위 명령 exit 0, 경과 68.406초다. Gate 종료·완료 기록 저장 뒤 로컬 `session_driver.py`의 화면 출력에서 `datetime` JSON 직렬화 오류로 바깥 실행기는 exit 1이었다. 이 보조 보고 오류는 `local-reporting-error.json`에 기록했다. 원래 실행기와 Gate 원본은 보존하고, 화면 출력에 `default=str`만 지정한 `session_driver_v2.py`로 이후 명령을 실행했다. **Gate를 재실행하지 않았다.** 오류 당시 prepare·역할 provider 효과는 0이었다.

실제 명령은 프로젝트 `.venv\Scripts\python.exe -X utf8 -B`로 다음 모듈을 호출했다. 각 명령의 전체 argv·시각·exit·출력 digest는 `*.started.json`, `*.completed.json`, stdout/stderr log에 있다.

```text
-m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\codex\flowmarshal --run-root <R25 root>\deterministic
-m scripts.diagnostics.r_s06_10 prepare --run-root <R25 root>
-m scripts.diagnostics.r_s06_10 run --run-root <R25 root>
```

| 단계 | 실제 상태 |
|---|---|
| prepare | 정확히 1회, 14:10:51~14:10:57 KST, prepared=true, exit 0, 5.563초 |
| run | 정확히 1회, 14:11:39~14:14:07 KST, summary FAIL, exit 0, 147.453초 |
| review-generated | NOT_RUN, phase claim·호출 없음 |

prepare는 현재 source 226파일, 원본 Goal·State·Project Map·Skeleton·사례 Plan, 고정 기대표·독립 fixture review, validator/oracle·threshold·taxonomy, 역할 설정, 실제 지침 본문, strict schema와 사례별 expectation을 새 불변 preflight에 결속했다. 준비된 모든 `input-*.json`과 expectations·independent review·roles는 검토된 R19 원본과 바이트가 같다. 정적 11사례의 expectation input binding과 12개 준비 요청의 저장 후 strict schema 재구성도 직접 검증했다. `planning-binding.json`은 계약 결속 자료이며 해당 파일의 18-cell 전체 pipeline을 실행한 것은 아니다.

fresh App Server `model/list`의 원본은 prepare와 호출의 inventory artifact에 보존했다. general Reviewer `gpt-5.6-terra/high`, expander `gpt-5.6-luna/high`, critical Reviewer `gpt-5.6-sol/xhigh`를 포함한 기존 역할 설정의 지원과 v2 projection을 확인했다. 바깥 세션의 Astra 배정을 제품 역할에 적용하거나 silent fallback하지 않았다. 실제 호출은 Terra/high 한 건뿐이다.

자동 주입 지침은 전역 `C:\Users\sjs95\.codex\AGENTS.md`, 프로젝트 `AGENTS.md`, 새 `workspace/AGENTS.md`의 3개다. prepare가 각각의 본문 snapshot·digest를 잠갔고 실제 thread/start의 `instructionSources` 및 호출 전 본문과 대조했다.

## 핵심 계약 digest

다음은 별도 표기가 없으면 해당 계약 필드 또는 canonical 객체 digest다.

| 항목 | SHA-256 |
|---|---|
| source manifest, 226파일 | `fab8c58d5bd57dd10abb36aeb5a48e19ec97cd106dfb93e38a43874df00b820a` |
| 새 deterministic contract | `35f0cc64256e6904461c84f1415ae59d2e190a4f173f84ecaee622daa28b2186` |
| 새 deterministic report | `589d454cbc55a8310415ad4bd8cb5ddaff687f49ae88d393e566e8ed068df47e` |
| evaluation preflight lock | `a5b0f6a263e3e89af545f6d6de3881e91cd5483133143f350f861046f13fd708` |
| planning binding contract | `b347f5823a89ac1d81169913f348f66cf87070ea0326e86cb85a64bc933b81db` |
| fresh inventory | `76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2` |
| 전체 역할 operational lock v2 | `ef7d5b53bc35fc500f0ce0eb046bc8d9984bf8b042aa416a3be5f221db8cee83` |
| clean 실제 역할 operational lock | `57d0a693cff1e3efa1cba660ee8ac1921fa7c12089030d6965c505e417e63fe9` |
| 역할 설정 | `ba683966a19b9cc249ef6117af7df5430979ad1907a631f5b8cd54d85a97eeb6` |
| prompt | `79d3a4ce1160489c3b937a150fcadca7309526d1df9bb4204ed5c8d587f366cf` |
| planning output schema | `e3b31a5f63458f6483ae25093a31399ac7ce22ec53378408ec57d96267863832` |
| clean 실제 strict schema | `ea08cc472ab8a1465555aca04624fbc512fed99df2f0e80f650b4b2daced0d23` |
| rules | `081e20a28f2be6a86dd8f4139f93474d1e751353f2808339e4e45d595f7f8871` |
| threshold | `624b50e26ba6df7f8639c5d898692c3ad170597d40285258e4dc098cffddcacf` |
| taxonomy | `2d51e24ec41ea93a87fa981c9642494ab2b1094dbb78d2ebcc3bdc8c96dc9f48` |
| 실행 codex.exe bytes | `935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` |

## clean 실제 결속과 의미 실패

- 역할: `compact_plan_reviewer`, `gpt-5.6-terra/high`, receipt `status=succeeded`.
- call: `model_call_14959e9166524ad19294666c6352e488`.
- thread: `01a06ffa-d0df-72f2-9900-4540b211bb54`.
- turn: `01a06ffa-d6f4-7d62-9c25-5d32da1a067a`.
- request canonical digest: `sha256:13ff31115e92aa61def773e8b16baad02d33e649e6ffb66ebd4ac080287ea0da`.
- receipt canonical digest: `sha256:c3819273a903f55a5dea3d0e68610708bb35aa8ca4661ee40f46bb7466bc2132`.
- output canonical digest: `sha256:19ef3c6a84c804106b1760707914ebde253992a02fa26b073d5134afce5a65ac`.
- clean expectation digest: `sha256:47a415d771546cab537991420abc595d0b15ad2353fa51b619fd2fa267d13c14`.

성공 result와 terminal 원문, request·strict schema·Prompt·지침·권한·model/effort·thread/turn·usage의 공통 결속과 output 결속이 통과했다. 저장 `binding-verification.json`도 현재 artifact 재검사와 일치한다. 그 뒤 정적 기대표로 계산한 clean assessment는 FAIL이다. 저장된 assessment를 같은 입력과 고정 evaluator로 직접 재계산해 완전히 일치함을 확인했다.

| AC × validation | 고정 기대 | 실제 |
|---|---:|---:|
| `ac_003 × val_goal_independent_behavior_contract` | true | false |
| `ac_003 × val_task_add_behavior_contract` | true | false |
| `ac_004 × val_task_add_behavior_contract` | true | false |

관계 행 집합은 28/28 일치하고 boolean은 25/28 일치한다. 기대 finding과 제출 finding은 모두 비어 있으며 missing/unexpected finding은 없다. 최초 오류는 `RuntimeError: SEMANTIC_ASSESSMENT_FAILED: clean`이다. Plan의 기존 실제 link는 위 세 관계 모두 존재하지만 모델의 필수성 제출값이 false였다. 결과 뒤 기대표·oracle·threshold·taxonomy나 모델 설정을 보정하지 않았다.

고정 순서는 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`다. clean 뒤 12사례는 전부 NOT_RUN이다. `generation-pending.json`, 생성 Plan 독립 검토, 생성 전용 기대표와 13번째 Reviewer에 도달하지 않았으며 이 경계는 미검증으로 남긴다. 재호출·resume·fallback·재생성·새 root 우회는 없었다.

## 효과·usage·summary

| 항목 | 실제 관측 |
|---|---|
| logical/provider/recovery | 1/1/0, 남은 예산 12/12는 미사용 |
| request·thread intent/start·turn intent/start·terminal·accepted result | 각각 1 |
| outcome success/failure/external_unknown/incomplete | 1/0/0/0, 별도 의미 assessment는 FAIL |
| input/cached input/output/reasoning/total tokens | 44,710 / 0 / 7,568 / 2,687 / **52,278** |
| 역할 latency / provider duration | 142,047ms / 140,210ms |
| Plan activation / Worker / 제품 원장 쓰기 | false / false / 0 |
| summary | status=FAIL, checks 전체 true, diagnostic_errors 0 |

usage 원본은 `thread/tokenUsage/updated`, scope는 `thread`다. 빈 새 thread의 첫 단일 turn을 확인했으므로 이 한 호출에 귀속했다. provider total을 그대로 사용하며 output에 포함된 reasoning을 다시 더하지 않았다. 청구 금액과 구독 한도 차감은 미제공이며 추정하지 않는다. 이 사용량은 제품 역할 한 호출의 관측값이고 개발 세션 사용량 합계가 아니다.

이번에는 성공 result가 있어 output binding이 APPLICABLE/PASS다. result 없는 실패 receipt의 NOT_APPLICABLE 경로와 terminal 미관측 external_unknown 경로는 이번 실제 호출에서 발생하지 않았다. 그 경로의 결정적 회귀가 통과했다고 이번에 실제 provider 실패까지 검증한 것으로 확대하지 않는다.

## 보존과 직접 검증 증거

다음은 새 root 기준 파일 **bytes SHA-256**이다.

| artifact | digest |
|---|---|
| `preflight.json` | `25a53545b5a003656aacdbba71cff6c478e9ce1813ada707d4f8316b9ad31a0e` |
| `instruction-binding.json` | `24c85b01556069fa5cc8efcf88079c730b1b7311a608365a69af567489696d36` |
| `expectations.json` | `3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e` |
| `independent-fixture-review.json` | `9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27` |
| `clean-assessment.json` | `ca5b0be528879b7b6d54f8334c910e2e2c3469d1f17aecce8a5ec2d82e4986f1` |
| `calls/01-compact_plan_reviewer/result.json` | `26dd8c30e0d28578aa4ad8f088b15c446482855d50833ad615c7466634a6cd9d` |
| `calls/01-compact_plan_reviewer/terminal.json` | `159711994ae0106608be6d2fbbfb3fda1747749b4f13d13f0f043be531a17f59` |
| `calls/01-compact_plan_reviewer/binding-verification.json` | `5efdb074fcb923920d0956fdeeb025d2f8cb7bd5c2dbebd14080da6b9449eebb` |
| `summary.json` | `29d18771fbfb44b2c2f86045cbef615e8d71976c44bd301d9549af2a2490186a` |

`gate-verification.json`, `pre-verification.json`, `post-verification.json`과 `limited-validation-outcome.json`이 Gate 전체 계약, 사례별 기대표·strict schema·v2 binding, summary receipt·usage·결속의 실제 재대조를 보존한다. source 226파일과 과거 R-S06/S05/S06 원본 6,353파일의 before/after가 같다. tracked 파일 기준선 3,416개도 문서 작성 전 보존했다. 최종 문서 검증에서는 이 인계와 README만 변경됐는지 확인한다. legacy freeze의 40파일도 PASS다.

summary는 다시 호출하거나 덮어쓰지 않았다. 명령 완료·후속 감사 기록은 별도 artifact로 추가하고 `summary_input_digest`를 사후 갱신하지 않았다. `document-verification.json`과 `evidence-manifest.json`에 문서의 digest·링크·변경 범위 및 수집 시점 artifact 결속을 남기며, commit·push 실제 결과는 이후 `delivery.json`과 자기 세션 최종 응답에 기록한다. run artifact·raw·로컬 DB를 Git에 포함하지 않는다.

## 다음 경계와 사용자 판단

다음 필요한 경계는 **R25 clean의 원본 제출과 위 세 관계의 사전 기대·Goal·validation·등록 도구 phase 근거를 직접 대조하는 별도 원인분석**이다. 현재 구조 수용과 의미 FAIL을 분리하고, 특히 R17에서 이미 기록된 관계 오판과의 공통점·차이를 근거로 확인해야 한다. 같은 입력을 성공할 때까지 반복하거나 기대값을 완화할 근거가 되지 않는다.

이번 제한 검증과 실패 provenance 마감에는 추가 사용자 판단이나 Goal·Plan revision이 필요하지 않았다. 다음 분석·개발·새 실제 검증은 실행하지 않았으며 다른 작업 생성·메시지·callback·예약도 수행하지 않았다. S06 전체 PASS·Functional Alpha 완료·1.0 완료로 확대하지 않는다.
