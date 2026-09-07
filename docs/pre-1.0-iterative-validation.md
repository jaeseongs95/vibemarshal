# 1.0 선행 보완 후 반복 검증

기준일: 2026-09-07. 현재 판정은 **NO-GO**다. v18은 결정적 Gate **5/5·887개 테스트**, 역할 **48/48 PASS** 뒤 Planning **18/18 완료·6 PASS·12 FAIL**을 기록했다. 정상 계획 선택은 0건이지만, 전체 75회 호출 **2,228,076 token**의 Goal 계약·공개 사용량·정산·프로젝트 연결 감사는 통과했다. 원본 실패와 비용을 보존하며 보완한 v19 source에서 전수 재검증한다. 최초 v9의 실패·비용은 [원본 결과](role-fixture-48-qualification.md)에 별도 보존한다.

## 승인 범위

사용자는 실패 원인 분석·수정·재검증을 계속하고 token 사용량 증가 허용 요청일 때만 승인받도록 지시했다. qualification은 기존 Goal별 상한 1,000,000·호출 예약 100,000·reserve 25%를 유지한다. token 상한 또는 예약량 증가는 추가 승인이 필요하다. 원문 결정은 `D:\codex\fm-inspection-runtime\planning-continuation-20260906\iterative-validation-user-decision-20260906.json`에 있다. 과거 진단 Goal의 2m/200k override는 승계하지 않는다.

## 구현과 검증 경계

- Reviewer는 같은 검토 객체의 요청·기대 결과·AC·관측·Profile 전체를 대조한다. 짧은 label을 문맥과 분리해 누락으로 판단하지 않고, 부분 발췌와 명시적 빈 값, AC 검증 가능성과 source binding을 구분한다. fixture·oracle·합격선은 변경하지 않았다.
- Full Planning과 Benchmark는 완료 checkpoint 없는 기존 호출을 새 attempt·예산으로 우회하지 않는다. 부분 호출이 있는 재시작은 runtime 진입 전에 차단하고 원장·provider 관측을 다음 조치로 안내한다. 자동 재개 경로가 없는 상태를 `PAUSED`로 보고하지 않는다.
- E2E는 전체 inventory와 model/effort 요청을 기록한다. `start_turn` receipt가 요청 인자를 되돌려 준 값은 실제 provider 관측으로 표시하지 않고, 원시 응답에서 확인하지 못한 실제 값은 `null`과 이유로 남긴다. exact Plan과 qualification driver 활성화 근거도 저장한다.
- E2E wrapper는 실제 runtime의 완료 observer 등록도 전달한다. terminal 관측을 불변 journal에 먼저 기록하고 Core에 전달하므로 SDK 연결 종료 때 받은 usage도 정산에 연결된다. 요청 echo와 provider 관측은 계속 구분하고 누적 usage를 임의로 단일 turn에 배분하지 않는다.
- 생성 receipt가 유실된 빈 thread는 원장 intent·receipt·binding과 직접 `thread/read`의 `turn_count=0`을 대조한다. 예산 해제와 Attempt·Task 실패 종료를 한 트랜잭션으로 기록해 처리 사이 crash의 자동 재실행을 막는다. 실측 token은 `null`이며 usage를 만들지 않는다. 같은 증거의 해제는 멱등이고 다른 증거·start/resume/unknown intent는 차단한다.

통합 회귀와 완료 observer 관련 44개 회귀를 통과한 뒤 고정 source에서 전체 Gate를 실행했다. 임시 DB 연결을 닫지 않은 신규 테스트의 Windows cleanup 오류는 테스트 연결 해제로 수정했고, 이 실패를 실제 모델 실패로 집계하지 않았다.

## 고정 source와 원본

| 항목 | 결속 |
|---|---|
| 작업본 | `D:\codex\fm-pre10-validation-v13` |
| HEAD | `b4cc3b9f0ebe0ee01c874a98c8aa77b904a56b55` |
| source manifest | `sha256:e697bc9d6f51e8ab09495b9ae6bed3a284478ecc1a241e772b9218de9ec76d56` |
| 결정적 Gate 원본 | `.flowmarshal-engine-eval\runs\pre10-completion-callback-devgate-20260906` |
| Gate contract | `sha256:85ba8e923d8eb8f5966ef8a66b618290db44107530e9fd3e55b09420bca85525` |
| Gate report | `sha256:dd14528f6697549de5e42e4163490b2cbd637ec4460b141254ab8c10d8a1525f` |
| 실제 역할 실행 | `D:\codex\fm-inspection-runtime\qualification-b4cc3b9-20260906\10-role-fixture` |
| 실행 지시·receipt | 같은 campaign의 `10-role-fixture-launch` |
| source 복사·설치 증명 | 운영 폴더의 `workspace-v13-source-freeze-proof.json` |

중간 v11 source(`f7fc541`, manifest `sha256:094b1a821b7098097cb2a2a57309f598130e39d74ba262a3866c8a4c9148d9b2`)도 Gate 5/5·826개를 통과했다. 후속 harness 공백을 실제 호출 전에 발견해 09:37:52 UTC에 실행을 중단했다. 당시 run root 미생성·provider 호출 0·예산 원장 0이므로 모델 실패가 아니다. `qualification-f7fc541-20260906\10-role-fixture-launch\precall-stop.intent.json`과 종료 receipt를 보존한다.

## v12 역할 48건 완료 기록

v12 `1fe759c20ba2cf3d3c1a78eaa44f443003ec6853`, manifest `sha256:ad8d6ab5ff27681db22166d0656f2b5336f6cd151fd4026d2ddd9c2107f5f3ca`에서 2026-09-06 10:20:33 UTC에 **48/48·PASS**를 확인했다. recall·precision은 각각 100%, 정상 요청 오차단·critical false admission·schema failure·critical seed 불일치는 모두 0이다. v12 Gate는 5/5·841개였다.

provider·receipt·usage는 각각 48개, History chain은 48/48 유효하며 중복·unknown·미정산 예약은 0이다. 실측 **1,350,702 token**은 입력 1,338,795 + 출력 11,907이다. 캐시 입력 523,904와 reasoning 7,608은 각각 입력·출력에 포함된다. 요청과 receipt에 결속한 설정은 `gpt-5.6-sol/xhigh`이며 독립적인 backend 실제 설정 관측으로 표현하지 않는다.

원본은 `D:\codex\fm-inspection-runtime\qualification-1fe759c-20260906\10-role-fixture`, contract `sha256:8dc4e7dac60c9b8f8d79f069e1a5e1edf0fe7e4720d3d5ae9a76a8d1a881fb82`, scope report `sha256:65c1cefa34af2a61fa947b78ca3a657683544da3b741dcbc91ece11e7b5b658b`다. 운영 폴더의 `role48-audit-v12-final.json`은 COMPLETE/PASS·errors 0이다. E2E observer 전달 보완으로 source가 달라져 이 PASS를 v13의 선행 Gate로 재사용하지 않는다.

## v13 역할 48건 완료 기록

2026-09-06 10:37:39 UTC에 **48/48·PASS**를 확인했다. required finding recall **100%**, finding precision **95.45%**이며 정상 요청 오차단·critical false admission·schema failure·critical seed 불일치는 모두 **0**이다. G06/seed-43의 `GOAL_AMBIGUITY`, G05/seed-89의 `GOAL_INVENTED_REQUIREMENT`가 추가 finding으로 남았다. 필수 finding과 차단 여부는 맞았으며 기존 recall 90%·precision 85% 등 동결 합격선을 통과했다. 이를 무결함 100% 판정으로 표현하지 않는다.

provider·receipt·usage **48/48/48**, History chain **48/48 유효**, 중복·unknown·미정산 예약 **0**을 확인했다. 실측 **1,351,318 token**은 입력 **1,338,795** + 출력 **12,523**이다. 캐시 입력 **610,176**과 reasoning **8,153**은 각각 입력·출력에 포함된다. 관측 latency 합계는 567,316ms이며 성능 36-cell의 측정 결과로 대체하지 않는다. 1m/100k/25는 Goal별 정책이고 이 합계는 서로 다른 48개 Goal의 총량이다.

contract는 `sha256:85bd326b9812269eaf80e9f02256927677eb9930c1ae5170b4d196090b1a2925`, scope report는 `sha256:f994157c299ddac143e6a3c20e05066ea97ce1b509fdab43638c36a55ba42fe6`, role report는 `sha256:656e36925b403e45b414680b2ec6d7eb664807c752221a117a472b251dd0d65e`다. `role48-audit-v13.json`은 COMPLETE/PASS·errors 0이며 이전 v12와 별개로 실제 48회를 감사했다.

## v13 Planning 18건 완료와 후속 보완

2026-09-06 10:59:37 UTC에 **18/18 완료·FAIL**을 기록했다. S06/seed-17의 필수 정보 차단 1건이 PASS이고 17건은 Goal 준비의 schema/validator 단계에서 실패했다. 정상 12건의 Plan 선택은 0이며 Skeleton·상세 Plan 단계에는 도달하지 못했다. 실패 분포는 빈 finding과 `ratings=null` **10건**, 제공되지 않은 하위 evidence ref **6건**, `analysis_audit`와 변경 정책의 불일치 **1건**이다.

기존 Reviewer 안내는 finding만 제출하도록 읽힐 수 있지만 원래 검증 계약은 결함이 없을 때 다섯 축의 ratings를 요구한다. 첫 실패는 `model_call_346979cfd30a4b98a071db9c53b363ef`다. 개발 source의 Goal·Skeleton Reviewer에 두 허용 출력 조합과 비권위 ratings/최종 Core 판정의 차이, 정확한 catalog key와 Task ref 사용을 명시했다. Goal Normalizer에는 사용자 목표에 맞는 mission/mutation 조합과 필요한 누락 사실만 질문하는 원칙을 보완했다. 실제 `ReviewDraft`·Plan inspection v1 schema·fixture·oracle·합격선은 바꾸지 않았으며 실패 응답에 rating이나 올바른 ref를 사후 주입하지 않았다.

근거 ref 실패 중 정상 사례 3개의 저장 thread를 `thread/read`로 재관측했다. Task validation과 독립 Goal Test의 분리를 누락한 S04 두 사례, 무관한 자료 부재를 새 non-goal로 옮긴 S01 한 사례를 확인해 Profile 검증 정책 보존과 무관한 관측 필터링을 정규화 안내에 추가했다. 모델 호출·thread 재개·실제 원장 수정 없이 DB bytes가 같은 것을 확인했다. 원본 관측은 `planning18-v13-stored-review-observations.json`, SHA-256 `c221dad63b1f3d212b93e1de4b6ef5e261cf65ee80fbd45c8268337bf16378f5`다. 이 의미 분석은 원래 schema failure 판정을 변경하지 않는다.

전체 **35 provider/request/receipt**는 모두 `settled`이며 History **18/18**이 유효하고 중복·unknown·미정산은 **0**이다. 실측 **828,067 token**은 입력 **778,077** + 출력 **49,990**이며 캐시 입력 **193,024**와 reasoning **36,342**는 각 입력·출력에 포함된다. Goal이 생성된 2개 호출의 `BudgetUsageRecord`는 **51,444 token**이다. 나머지 33개 호출 **776,623 token**도 provider receipt·예약/정산 History에 저장돼 budget 소계에 포함된다. Goal revision 생성 전 실패했으므로 해당 digest와 Goal-bound usage projection이 없을 뿐 비용 유실은 아니다. 후속 공개 `usage_summary`와 `budget show`는 검증된 provider receipt를 읽기 전용으로 투영한다. 원본 DB를 변경하지 않고 18개 원장·35회 전체 **828,067 token**을 재현했다. 근거는 운영 폴더의 `pre-goal-public-usage-audit-v14.json`이다. schema recovery로 token 귀속이 불완전해도 관측된 latency를 보존하는 회귀도 포함한다.

원본은 `qualification-b4cc3b9-20260906\20-full-planning`, contract `sha256:b62d0aad88d3b84896c2589d71cdebc2c03a7cd5fb5ca484b5d240ddf8002663`, report `sha256:f229522eab31022f516fce7d35a766bb98bc71d4f19ca1d6a24bd88bdd39baa9`다. `planning18-audit-v13.json`은 COMPLETE/qualification FAIL·감사 errors 0이며 SHA-256은 `695f074733af6f4afa16ce3a9307f7b68891e7ecc1cc2a90ebcd1f195ea03dcc`다. 감사기는 exact receipt·정산·Goal 미생성 상태를 검증한 호출만 지연 projection으로 분류하고 다른 usage 누락은 오류로 남긴다.

## v14 프로젝트 결속과 새 검증

사용자가 지정한 Codex 프로젝트는 `자동화테스트`다. 실제 root는 `C:\Users\sjs95\Documents\ChatGPT\자동화테스트`, App Server project ID는 `01a074f8-b78e-7b21-9390-99e251da49bb`다. Desktop UI ID `a9b87a2d-7530-42a9-841c-4f23c99e8c22`와 구분한다. cwd만으로 소속을 추정하지 않고 `project/read`의 이름·단일 root와 `thread/start/read`의 project ID를 대조한다.

기존 pinned CLI 0.147에는 프로젝트 결속 API가 없어 명시적으로 고정한 CLI 0.151.0을 새 계약에 사용한다. 실행 파일 SHA-256은 `cf68265897197ac5f3bff6a10c168eec159842b353129726da5e3ed6b91ef0f4`이며 SDK는 `openai-codex 0.147.0`을 유지한다. 빈 새 thread의 프로젝트는 생성 응답에는 있지만 첫 turn 전 저장 조회에는 아직 없을 수 있다. 정확한 생성 응답 증명은 첫 turn에 한 번만 소비하고, 이후 조회·재개에는 저장된 정확한 project ID를 요구한다. 임의의 null 소속을 허용하지 않는다. 프로젝트 결속이 있는 legacy 비교도 동결 외부 bridge에서 저장형 thread로 실행하며 이 차이를 비교 계약에 명시한다.

| 항목 | 결속 |
|---|---|
| 고정 source | `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v14` |
| HEAD | `36e4c1ad25bb3dbb6b03da26f6b27baa7f6e3f47` |
| source manifest | `sha256:967db66cee468fa05b250cfc6a3b1add1ec7e4917cb08e193b416c1dad0a2be2` |
| 실제 모델 run parent | 테스트 프로젝트의 `v14`, 역할 `r`·Planning `p`·E2E `e`·benchmark `b` |
| 결정적 Gate | 테스트 프로젝트의 `.flowmarshal-engine-eval\qualification-36e4c1a-20260906\00-deterministic-r2`, 5/5·865개 테스트·legacy 40개 무변경 |
| Gate contract | `sha256:eb731a131efeafa0c1966aed1d0b945e25f608c6027094e23571df2031103d53` |
| Gate report | `sha256:3271244e12444cdc1bfe5b831844b22408cb974d4e9001c98d70c26d140f09ea` |
| 정책 digest | `sha256:d49f3aab7bbb439d2b283ad6991262d7593691a6e0aedfebaeed64bef0422938` |

첫 결정적 실행 `00-deterministic`은 이전 경로 밖의 동결 Planner fixture 7개 누락으로 전체 테스트 1건과 freeze Gate가 실패했다. 같은 원본 bytes를 manifest의 상대 경로에 배치하고 `legacy-fixture-relocation-v14.json`에 해시·source 무변경을 기록했다. 원래 실패는 보존했으며 실제 모델 호출은 0이었다.

진단 `.flowmarshal-engine-eval\runtime-v14`는 모델 호출 **1회·24,375 token**으로 저장 thread `01a076ad-fc6d-7240-9260-3d3ab50d8491`의 정확한 프로젝트 소속, provider receipt·공개 usage·정산·History·source 무변경을 확인했다. 이 진단은 qualification PASS에 포함하지 않는다.

첫 역할 실행 `qualification-36e4c1a-20260906\10-role-fixture`는 첫 호출 전 Windows 경로 길이 제한으로 종료했다. 생성된 SQLite 원장 1개에서 project·provider call·History·Attempt가 모두 0이며 cell·role progress도 없음을 읽기 전용으로 확인했다. `role48-v14-precall-path-failure.json`에 원본 FAIL과 호출 0 근거를 보존했다. 짧은 `v14\r` 경로의 재실행은 테스트 프로젝트에 추가 등록된 `D:\codex` 경로를 단일 root 검사에서 거부해 run root·provider 호출 생성 전에 종료했다. 이 실패도 `role48-v14-multi-root-precall-stop.json`과 실제 `project/read` 응답에 보존했다.

## v15 다중 경로 프로젝트 검증

`CodexProjectBinding.expected_root`는 지정 프로젝트에 등록돼 있어야 하는 절대 경로다. 응답의 모든 root가 절대 경로이고 정규화한 expected root가 정확히 한 번 존재하면 다른 등록 root도 허용한다. project ID·이름 불일치, 지정 root의 부재·중복, 상대 경로는 차단한다. 생성 receipt·첫 turn 증명의 단일 사용·저장 thread의 project ID·실제 권한과 cwd 검사는 유지한다. 실제 현재 프로젝트의 두 root를 새 runtime에서 호출 0회로 검증했으며 근거는 `project-binding-preflight-v15.json`이다.

| 항목 | 결속 |
|---|---|
| 고정 source | `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v15` |
| HEAD | `6eaa03eade1568f13195f88df26ceca6f1500264` |
| source manifest | `sha256:b1fb7a50a5b844844daf6106409b183c3006e0d3b25072bd750fa8ffbce621a4` |
| run parent | `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\v15` |
| 결정적 Gate | `d`, 5/5·869개 테스트 / 125.940초·legacy 40개 무변경 |
| Gate contract | `sha256:7754cb7d1878c242b1d9c846b35e9b58f87cdce252e75095022111e259291bbd` |
| Gate report | `sha256:a804fe5158d7f93910f720fd0a0039bd340e22c5b6c84429a7f975d5c03299a3` |
| 실제 역할 실행 | `r`, 기존 48개 사례·seed·oracle·합격선·역할 설정 유지 |

정책 JSON·예산·역할 model/effort·pinned CLI는 v14와 같으며 source 변경을 새 계약에 결속했다. 최신 고정 source의 역할·Planning·E2E·성능은 서로 다른 source의 과거 PASS를 가져와 충족하지 않는다.

## v15 역할 48건 완료와 복구 진단

2026-09-06 12:58:23 UTC에 **48/48 완료·FAIL**을 기록했다. `P05-clean/seed-43`의 정상 요청 오차단 1건이 원인이다. required finding recall은 100%, precision은 93.33%이며 critical false admission·critical seed 불일치·schema failure는 0이다. provider·receipt·usage는 각각 48개, History는 48/48 유효하고 중복·unknown·미정산 예약은 0이다. 실측 **1,382,071 token**은 입력 1,368,087 + 출력 13,984이며 캐시 입력 386,944와 reasoning 9,532는 각각 입력·출력에 포함된다. `role48-audit-v15.json`은 감사 COMPLETE·errors 0과 qualification FAIL을 구분한다. 계약은 `sha256:8dc25ce6454216f73b8ce55b45035010342606efd00fd2d446ac4ad968b0ecd8`, scope report는 `sha256:4e7dc7eaa2d1ea793a61bed0043081e212cc4a1484205d8805acfdb082c78520`이다.

오차단 응답은 특정 목적의 검사 설명을 전체 검사 목록으로 해석하고, 계획에 선언된 선택에도 별도 실행 증빙이 없다고 지적했다. 후속 generic 검토 안내는 부분 발췌의 계획 정합성과 실행 후 증빙을 구분하고, 명시된 증명 의무·검사 완결성이나 직접 검증 불능은 계속 검사하도록 보완했다. fixture·oracle·taxonomy·합격선은 그대로이며 과거 실패를 재판정하지 않는다. 분석 원본은 `role-p05-failure-analysis-v16.json`이다.

별도의 모델 실행 없는 E2E 진단에서 생성 receipt의 project ID는 정확하지만, 첫 사용자 turn 전 조회의 project ID는 null이고 turn 목록 조회가 지원되지 않는 경계를 재현했다. `v15\u`와 후속 `v16\u`, `v16\u2`, `v16\u3`의 생성·조회 원본, 예약 및 실패 원장을 보존한다. 각 진단의 thread 생성은 1회이고 start/resume/interrupt는 0회다. 원장 사용량은 생성되지 않았고 예약도 임의 해제하지 않았다. 이 진단들은 조회 API와 생성 receipt의 근거를 구분하기 위한 과거 실패이며 실제 E2E 4건 PASS를 의미하지 않는다.

원인 구분에서 완료 thread의 raw·typed 전체 조회와 paginated turn 목록은 모두 같은 완료 turn을 반환했다. 새 빈 thread의 `thread/turns/list`는 최초 메시지 전 저장 준비가 안 됐다는 `-32600`을 반환했으며, `v16\u2`의 제한된 읽기 재시도만으로는 상태가 바뀌지 않았다. `v16\u3`에서는 호출별 파일 관측으로 typed 전체 조회가 `session_meta` 1건의 rollout을 만드는 것까지 확인했으나, 같은 연결의 목록 조회는 `-32601`을 반환했다. `v16\u`의 연결 종료 뒤 별도 읽기 연결에서는 실제 빈 목록을 확인했다. 각 결과와 API 오류를 성공 응답으로 대체하지 않고 보존하며, 이 근거로 같은 연결을 유지한 별도 읽기 경로를 추가 확인했고 결과는 다음 절에 보존했다.

## v16 직접 빈 목록 복구와 재검증

`v16\peer-probe`에서 원래 연결을 유지한 채 독립 App Server 연결의 빈 목록 조회가 성공했다. 후속 runtime은 같은 연결의 미소비 생성 증명과 owner 전체 조회 원본을 보존하고, 동일 실행 파일·프로젝트 binding의 독립 reader가 metadata 두 번과 완전한 빈 turn 목록을 직접 확인한다. 특정 API 오류나 rollout 파일 부재만으로 turn 0을 판정하지 않는다. Core는 이 출처와 생성 receipt·원장 model observation의 executable digest·thread·project·cwd·path를 대조하고, 해제와 실패 종료를 한 트랜잭션으로 기록한다.

실제 `v16\u4` 진단은 2026-09-06 13:46:19 UTC에 복구 PASS를 기록했다. create 1회, start/resume/interrupt 0회이며 실제 빈 목록으로 예약 1건을 해제했다. 해제 History는 1건, Attempt는 `failed/external_unknown`으로 종료됐으며 실측 token·usage ID·provider model receipt는 null, usage record는 0개다. 원본 사용량을 0으로 정산한 것이 아니다. 진단 전후 manifest는 아래 동결 source와 동일하며, 이것은 실제 E2E 4건을 대체하지 않는 제한 진단이다.

| 항목 | 결속 |
|---|---|
| 고정 source | `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v16` |
| HEAD | `6bcfe3f2e263270aeac3257b4a0b6ad829033383` |
| source manifest | `sha256:b2b4f206ebb2ccee85ebeaab02bde049e51a23383d5fa7db9dc0629a2dac628f` |
| run parent | `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\v16` |
| 결정적 Gate | `d`, 5/5·880개 테스트 / 117.087초·legacy 40개 무변경 |
| Gate contract | `sha256:d7245505d3b45c4ecf3d59debe25fccac7e7cd288672c6448cfa703dda6ff630` |
| Gate report | `sha256:4b988040ab542b7577cd3059bad20de451b1c4b2662755ea14e8fd1d20d078bc` |
| 실제 역할 실행 | `r`, 48/48·PASS, 원래 사례·seed·oracle·합격선·역할 설정 유지 |

CLI 0.151.0·SDK 0.147.0·예산 1m/100k/25와 테스트 프로젝트 binding은 유지했다. 새 source는 부분 발췌 검토 지침과 실제 빈 세션 복구를 포함하며, 실행·감사 artifact는 이전 버전을 덮어쓰지 않는 새 파일에 결속한다.

## v16 역할 48건 완료

2026-09-06 14:01:20 UTC에 **48/48·PASS**를 확인했다. required finding recall은 **100%**, finding precision은 **97.67%**이며 정상 사례 오차단·critical false admission·critical seed 불일치·schema failure는 모두 **0**이다. v15에서 실패한 `P05-clean/seed-43`도 기존 oracle 그대로 통과했다.

provider·receipt·usage **48/48/48**, History **48/48 유효**, 중복·unknown·미정산 예약 **0**을 확인했다. 실측 **1,386,981 token**은 입력 **1,375,227** + 출력 **11,754**이며 캐시 입력 **529,536**과 reasoning **7,483**은 각각 입력·출력에 포함된다. latency 합계는 **552,136ms**다. `role48-audit-v16.json`은 COMPLETE/PASS·errors 0이며 `role48-project-binding-audit-v16.json`도 48개 실제 저장 thread의 프로젝트·cwd·영속성·완료 turn을 읽기 전용으로 확인해 PASS를 기록했다. 두 감사는 입력 파일을 변경하지 않았다.

role contract는 `sha256:6e4cd0f48bd1a7e59a682435c81be54fd496783e04b30e3a95eddf139a16510e`, scope report는 `sha256:7c374c5d45236e115bfb0f052d03d562e0f61be5262b6ec790820d86e07f79a6`다. `v16\p`의 Planning 18건은 같은 source와 이 결정적·역할 보고서 두 개를 선행 조건으로 결속해 실행했다.

## v16 Planning 18건 완료와 개발 보완

2026-09-06 15:23:56 UTC에 **18/18 완료·FAIL**을 기록했다. 정보 부족 6건은 모두 기대대로 차단됐고 정상 12건 중 `S01-single-bugfix/seed-17` 1건만 Plan 선택까지 통과했다. 실패는 Goal 의미 검토 4건, Skeleton refinement의 접근 전략 변경 1건, 상세 Plan 인용 계약 위반 6건이다. 사례·seed·oracle·합격선은 그대로 유지했다.

실측 provider receipt **70개**는 모두 `settled`이며 남은 예약·중복 호출은 0, History **18/18 유효**다. 총 **2,086,595 token**은 입력 **1,865,433** + 출력 **221,162**다. 캐시 입력 **268,928**과 reasoning **127,324**는 각각 입력·출력에 포함되며 latency 합계는 **4,407,798ms**다. contract는 `sha256:cc6a67c1689ca279369f77fc2d937bffe494780a1f23d4daa0d87875d61ac938`, scope report는 `sha256:c6e31a0b62af8563c8c392946f73b03fad903a01d1580fc61c8dfc5d4428d321`이다.

Goal 준비의 두 호출은 각 원장의 Goal 등록 때 결속됐지만, 재사용한 runner에 새 Goal digest가 전달되지 않아 이후 **34개 호출**은 Goal이 존재하는데도 digest와 usage ID가 null로 남았다. 원본 비용과 예산 소계는 보존됐으며 공개 `usage_summary`는 이를 정상 준비 단계로 투영하지 않고 8개 cell에 `USAGE_INCOMPLETE`를 반환했다. 실제 Goal-bound usage는 **36개·886,964 token**, 결속이 누락된 호출은 **1,199,631 token**이다. `planning18-audit-v16-r2.json`은 저장 Goal·준비 출력·후속 request와 Goal 생성/예약 History 순서를 대조해 정확히 34개의 `POST_GOAL_CALL_PROJECTION_MISSING`을 기록했다. 최초 감사기의 축약 schema failure 오분류와 원래 출력도 보존했다. `planning18-public-usage-audit-v16.json`에 공개 조회의 무결성 실패를 보존했고 원본 원장을 사후 수정하지 않는다. `planning18-project-binding-audit-v16.json`은 별도로 실제 저장 thread **70개**의 프로젝트·cwd·영속성·완료 turn을 확인해 binding PASS를 기록했다. 프로젝트 연결 PASS를 전체 qualification 또는 사용량 결속 PASS로 표현하지 않는다.

후속 개발 변경과 원인은 다음과 같다. 기존 동결 v16과 실행 원장은 수정하지 않았으며 새 source의 전수 qualification이 필요하다.

- **Goal 해석:** 별도 원본 자료 보호, 최종 결과와 Hard AC의 완료 상태 일치, 파일 변경 금지와 명령 금지의 차이, Task validation과 독립 Goal Test의 분리를 실제 적용 범위에 따라 보존한다. 원본 분석은 `planning-s03-goal-failure-analysis-v16.json`, `planning-s02-goal-outcome-failure-analysis-v16.json`, `planning-s02-goal-execution-scope-failure-analysis-v16.json`, `planning-s03-validation-policy-failure-analysis-v16.json`에 있다.
- **Skeleton 생성·보정:** A/B 비교가 산출물 요구이면 각 후보가 두 대안을 모두 다루도록 하고, 초기 Skeleton 보정은 부모의 접근 전략 4필드를 Core에서 보존한다. 모델의 수정 schema에는 허용된 필드만 둔다. 상세 Plan feedback에서 같은 `strategy_family`의 새 Skeleton을 만드는 기존 경로는 별도 계약이며 다른 접근 특성의 변경·전체 재평가를 계속 허용한다. 근거는 `planning-s03-skeleton-refinement-analysis-v16.json`이다.
- **Plan 인용:** 실제 요청 catalog의 source ref만 schema enum으로 허용하고 writer의 `artifact:plan_draft`와 입력 `artifact:skeleton`을 구분한다. selector는 실제 문자열 필드까지 가리키며, 파싱된 quote에 원문에 없는 escape 문자를 추가하지 않는다. 모든 claim·basis ref는 실제 등록한 citation ID를 재사용하고 미사용 citation은 등록하지 않는다. source alias·자동 unescape·누락 ref 보정은 하지 않는다. 근거는 `planning-s02-schema-failure-analysis-v16.json`, `planning-s01-citation-leaf-failure-analysis-v16.json`, `planning-s04-citation-failure-analysis-v16.json`, `planning-s01-missing-citation-failure-analysis-v16.json`이다.
- **Planning 호출 예산:** Goal 등록과 준비 호출 attach 직후 후속 runner에 확정 Goal digest를 전달한다. 같은 Goal 예산 안에서 Skeleton·Plan의 성공과 schema failure 모두 해당 계약의 usage로 정산한다. 공개 조회의 누락 거부는 유지한다.

요청별 citation enum은 실제 `RoleCallRequest.output_schema`와 request/receipt digest에 결속된다. EvaluationContract는 동결 source와 schema 계열을 결속하며, 후속 감사기는 그 source의 생성 규칙과 실제 요청 catalog로 각 schema를 재구성한다. v2 schema에 v1 citation 규칙을 주입하지 않는다.

## v17 호출 전 검사 실패와 v18 재동결

v17 `97049fe96a782978ad291132d76595d0513c02d3`, manifest `sha256:be8bf105ca61f6e0d72d36012170a884ff0dc0c26337bac9a8e3e18f24a50dcb`는 실제 모델 호출 전에 결정적 Gate **4/5**를 기록했다. 전체 **887개 / 117.540초** 중 legacy proxy watchdog 회귀 1개가 실패했다. 고의로 막은 turn/interrupt까지 도달하기 전에 실제 thread 시작과 SQLite 예약 확인이 테스트의 합성 10ms deadline을 소진해 `LEGACY_THREAD_START_TIMEOUT`이 발생했다.

합성 watchdog timeout을 100ms로 조정했으며 고의 정지 RPC·turn, 기존 0.5초 종료 assert, 예약·interrupt·usage 검증은 유지했다. 관련 11개 회귀는 **2.061초·PASS**다. 실제 운영 역할 timeout·예산, qualification fixture·oracle·합격선은 변경하지 않았다. 원본 FAIL·source와 호출 0 근거는 `qualification-v17-pre-model-gate-failure.json`에 보존했다.

후속 v18은 같은 보완과 테스트 안정화를 포함한다. 2026-09-06 15:48:04 UTC에 결정적 Gate **5/5·887개 테스트 / 117.067초**, legacy **40개 무변경**을 확인했다. 같은 source의 역할 **48/48 PASS**를 새 run root에서 확인했으며 v17의 실패를 덮어쓰지 않는다.

| 항목 | 결속 |
|---|---|
| 고정 source | `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v18` |
| HEAD | `9f487f61dee45dbddcd05d26437368c573c7552d` |
| source manifest | `sha256:7ec73971bb93a0dc746646caf8b906cfcfc32ead20d376e028b34b198936b5f2` |
| run parent | `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\v18` |
| 결정적 Gate | `d`, contract `sha256:2ca50be1f6eaedecc0b63aa7778d398f72b433e53ad6fff78e16ed13a9622f55` |
| Gate report | `sha256:13bbeb6b1854f708cc0c48e677fec19ba9d39f723f011c30abcc18557bc101e3` |
| 동결 증명 | 운영 폴더의 `workspace-v18-source-freeze-proof.json` |

## v18 역할 48건 완료와 Planning 재검증

2026-09-06 15:59:04 UTC에 **48/48·PASS**를 확인했다. required finding recall **100%**, finding precision **95.45%**이며 정상 사례 오차단·critical false admission·critical seed 불일치·schema failure는 모두 **0**이다. provider·receipt·usage **48/48/48**, History **48/48 유효**, 중복·unknown·미정산 예약 **0**을 확인했다.

실측 **1,386,872 token**은 입력 **1,375,227** + 출력 **11,645**이며 캐시 입력 **473,088**과 reasoning **7,212**는 각각 입력·출력에 포함된다. latency 합계는 **493,447ms**다. `role48-audit-v18.json`은 COMPLETE/PASS·errors 0, `role48-project-binding-audit-v18.json`도 실제 저장 thread 48개의 프로젝트·cwd·영속성·완료 turn을 읽기 전용으로 확인해 PASS다. 요청·receipt의 `gpt-5.6-sol/xhigh` 결속값과 provider의 독립 실제 설정 관측은 계속 구분한다.

role contract는 `sha256:6f5e5cf42f49333d7661c5821b7c5117b31dbcece566aa8071630ebfbdfcebea`, scope report는 `sha256:ebb3bc0bf1af052797aa752206f2004d3aae712d1f32c4609a1b2f0548594768`다. 같은 source와 결정적·역할 보고서에 결속한 `v18\p`는 2026-09-06 17:20:13 UTC에 **18/18 완료·6 PASS·12 FAIL**을 기록했다. 정보 부족·외부 효과 차단 6건은 모두 PASS이며 정상 12건은 Goal 준비 의미 충돌 3건·Plan 작성 인용 계약 오류 9건으로 실패했다. 정상 Plan 선택은 0건이다. v16의 완료 FAIL과 후속 호출 결속 누락 기록은 그대로 보존한다.

전체 **75 provider/request/receipt/usage**는 모두 정산됐으며 History **18/18 유효**, 중복·unknown·미정산 예약·Goal 결속 누락은 **0**이다. 실측 **2,228,076 token**은 입력 **2,006,249** + 출력 **221,827**이며 캐시 입력 **358,016**과 reasoning **115,777**은 각 입력·출력에 포함된다. 관측 latency 합계는 **4,594,722ms**다. `planning18-audit-v18.json`은 COMPLETE/qualification FAIL·errors 0, `planning18-public-usage-audit-v18.json`은 공개 합계·역할/단계 소계와 원장·원시 receipt 일치 PASS다. `planning18-project-binding-audit-v18.json`은 실제 저장 thread·turn 75개의 프로젝트·cwd·영속성·종료 관측을 확인해 PASS다. 세 감사 모두 원본을 변경하지 않았다.

Planning contract는 `sha256:008c472e406bbb3e6c9a7bbd50a54587523d12a385feed6e7929df6f344cc955`, scope report는 `sha256:ef31d2e7d55a6fdd8bc32698f006a3d3d9ab820e001dd1b885c01d96af60108a`다. 정산 보완의 실제 검증 성공을 Planning 품질 합격과 구분한다.

완료된 첫 `S01-single-bugfix/seed-17` 셀은 Plan 생성의 미사용 인용으로 FAIL했지만, Goal 준비부터 후속 schema failure까지 실제 호출 **5개·150,692 token**이 같은 Goal digest와 usage ID에 결속됐다. 공개 `usage_summary`의 오류·누락은 없으며 raw receipt·BudgetManager·공개 합계가 일치하고 History도 유효하다. `planning-first-cell-goal-binding-audit-v18-r2.json`은 원본 원장 무변경을 확인했다. 최초 감사 파일의 source manifest 전사 누락은 r2에서 동결 proof 원문으로 교정하고 원본 감사 파일도 보존했다. 이 제한 감사 PASS는 전체 Planning PASS가 아니다.

같은 seed의 S01과 `S02-read-only-analysis`는 각각 Plan objective 인용 1개, Skeleton 필드 인용 8개를 등록한 뒤 검사 표에서 참조하지 않았다. 모든 source·selector·quote와 나머지 엄격 검사에는 오류가 없었으며, 원본을 수정하지 않은 메모리 진단에서 미사용 인용만 제외하면 기존 validator를 통과했다. 상세 근거와 원본 전후 hash는 `planning-s01-unused-citation-failure-analysis-v18.json`에 있다. 후속 개발 source의 writer 안내에 실제 참조 ID 합집합과 등록 citation ID 집합의 정확한 일치, catalog와 배경 필드의 불필요한 선등록 금지를 명시했다. validator·자동 후처리·fixture·합격선은 유지했고 관련 회귀 **30개 / 1.264초**를 통과했다. 이 안내 변경은 동결 v18에 소급 반영하지 않으며 별도 고정 source의 실제 재검증이 필요하다.

## v18 후속 실패 분석과 다음 계획 역할 후보

추가로 완료된 Plan 작성 실패는 Skeleton dependency의 `produces`와 Plan draft의 `products` 혼동, 문자열 대신 배열을 가리킨 selector, `scope_id`를 citation ID 자리에 복사한 오류, 선택 scope의 근거 누락·중복과 미사용 citation이었다. 개발 writer 지침에는 source별 정확한 문자열 경로, scope 관계 ID와 citation ID 구분, 선택 scope의 claim·basis 합집합과 등록·사용 citation 집합의 일치를 명시했다. 원본에 없는 JSON escape를 quote에 남긴 사례와 동일 phase mechanism의 근거 누락·중복은 실제 전달된 기존 schema·지침을 어긴 경우다. 추가 의미 규칙이나 validator 보정으로 통과시키지 않는다.

Goal 정규화에서는 생산·소비 산출물의 소유 관계 변경, 이미 명시된 원본 fixture 보호·Task validation과 독립 Goal Test 분리 누락, 완료 상태 충돌을 확인했다. 현재 계획 호출만의 파일 변경 금지를 미래 Goal 전체로 옮긴 사례도 있다. 원시 요청·저장 응답과 Reviewer의 직접 근거를 대조했으며 해당 차단을 유지한다. 개발 지침은 생산 주체·산출물·소비 주체와 의존 방향의 보존을 보완했다. 이미 주어진 규칙의 반복 불이행은 역할 배정 후보로 검증한다.

분석은 운영 폴더의 `planning-s03-scope-citation-failure-analysis-v18.json`, `planning-s04-selector-failure-analysis-v18.json`, `planning-s03-selector-failure-analysis-v18-seed43.json`, `planning-expander-quote-failure-analysis-v18-r2.json`, `planning-expander-final-cell-quote-diagnosis-v18.json`, `planning-s02-ac-scope-failure-analysis-v18.json`, `planning-s02-goal-failure-analysis-v18.json`, `planning-s04-goal-ownership-failure-analysis-v18.json`, `planning-s03-goal-stage-scope-failure-analysis-v18.json`에 원본 해시와 함께 보존했다. 원시 본문을 확인하기 전의 quote 분석은 r2로 보완했으며 최초 보고서도 남겼다. 메모리에서 인용 오류를 고친 결과는 원인 진단이며 원본 Plan의 합격 판정이 아니다.

새 후보는 `tests/fixtures/engine/plan-inspection-sol-planning-roles.json`으로 명시 주입한다. Goal normalizer와 Plan expander만 `gpt-5.6-luna/high`에서 `gpt-5.6-sol/high`로 바꾸고 Skeleton generator·Reviewer·Executor·Validator 배정은 유지한다. 파일 SHA-256은 `3939481870b746d158c6fd212b4f3ce7e2ed0351593025e578291a3032dcc37f`, typed configuration digest는 `sha256:d4289b5a79ab6f23bd88fbf29f7f40aee14844d48c6c9e48221459257a5a1dc1`다. 이 파일을 v19 source에 동결해 전수 검증에 사용하며 제품 기본값은 바꾸지 않았다. 두 역할 배정과 작성 안내를 함께 바꾼 후보이므로 이후 차이를 단일 모델 변경의 효과라고 단정하지 않는다. Plan expander만 바꾼 선행 준비안은 실행하지 않고 운영 폴더에 별도로 보존했다.

## v19 동결과 전수 재검증

고정 source는 `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v19`, HEAD는 `65b3716619a662f21a4021468ee973eb1557239c`, source manifest는 `sha256:443df3d49808d561690d65a781d1b399dba10985327c8d14d98b3e89a1b3ee5a`다. 전용 Python과 v18과 같은 의존성 버전을 결속했으며, 동결 증명은 `workspace-v19-source-freeze-proof.json`이다. 새 run parent `자동화테스트\v19`의 `d`는 2026-09-06 17:26:33 UTC에 **5/5·887개 테스트 / 119.993초·legacy 40개 무변경**을 통과했다. Gate contract는 `sha256:7807cacbd5c61b433d0d75a09476afbdb59f5e4414d9c91196923fc86dca57b2`, report는 `sha256:6816b19f5c4ccff1830340526cf9d1cddb0d598dd97198d9719bba985eab7c18`다. 같은 source의 역할 실행 `r`은 2026-09-06 17:37:45 UTC에 **48/48 PASS**를 기록했다. 필수 finding recall은 **100%**, precision은 **97.67%**, 정상 사례 오차단·critical false admission·critical seed 불일치·schema failure는 모두 **0**이다. 이후 역할·Planning·E2E·성능·lifecycle은 이 source와 명시한 Sol 계획 역할 파일·기존 예산·timeout·프로젝트 결속에 맞춘 새 계약으로 수행한다. 최초 운영 도구 결속은 `qualification-operations-registry-v19.json`에 기록했다. 읽기 전용 검토에서 감사기에 남은 이전 launcher hash와 실행 증거의 UI/App Server 프로젝트 ID 표기 혼동을 발견했다. 실제 역할 실행 argv의 canonical 프로젝트 결속은 정확했다. 실행 중인 역할 run과 원래 도구를 보존하고, 역할 감사기는 실제 원래 v19 launcher hash에, 이후 Planning·E2E·성능 실행기는 binding JSON의 App Server ID에 각각 결속한 별도 r2 도구를 만들었다. 최종 운영 결속은 `qualification-operations-registry-v19-r2.json`을 따르며 source·역할 설정·정책·기존 실행 기록을 바꾸거나 호출을 다시 시작하지 않았다.

v19 역할 호출·receipt·usage는 **48/48/48**, History **48/48 유효**, 중복·unknown·미정산 예약은 **0**이다. 실측 **1,387,386 token**은 입력 **1,375,227** + 출력 **12,159**, 캐시 입력 **480,000**과 reasoning **7,801**은 각 입력·출력에 포함된다. latency 합계는 **479,452ms**다. `role48-audit-v19.json`은 COMPLETE/PASS·errors 0, `role48-project-binding-audit-v19.json`은 실제 저장 세션 48개·turn 48개의 프로젝트 결속을 확인해 PASS다. 감사기는 r2이지만 실행 원본과 결과 파일은 최초 v19 역할 run을 가리킨다.

role contract는 `sha256:be82411ab094eee1c6b42b7a5c11ba2e462e95dba333eded269142207d08cd8f`, scope report는 `sha256:89018ab97da0a7e984cfbbd2b804abcfa05dfac3c4019784323b97cb852d1e2f`다. 결정적·역할 PASS를 같은 source에 결속한 `v19\p`는 2026-09-06 20:53:24 UTC에 **18/18 완료·15 PASS·3 FAIL**을 기록했다. 정보 부족·외부 효과 차단 6건과 정상 Plan 선택 9건은 PASS이며, 아래 분석한 3건의 실패를 유지한다. 시작 증거에서 r2 launcher hash, 명시한 Sol 역할 파일, canonical App Server 프로젝트 ID가 일치함을 확인했다.

전체 **110 provider/request/receipt/usage**가 정산됐고 History **18/18 유효**, 중복·unknown·미정산 예약·Goal 결속 누락은 **0**이다. 실측 **4,059,253 token**은 입력 **3,456,837** + 출력 **602,416**이며 캐시 입력 **432,384**과 reasoning **308,482**는 각각 입력·출력에 포함된다. latency 합계는 **11,525,141ms**다. `planning18-audit-v19.json`은 COMPLETE/qualification FAIL·errors 0, `planning18-public-usage-audit-v19.json`은 공개 합계·역할/단계 소계와 원장·원시 receipt 일치 PASS다. `planning18-project-binding-audit-v19.json`도 실제 저장 thread·terminal turn 110개의 프로젝트·cwd·영속성 결속을 확인해 PASS다. 원본 입력·원장·실패 판정은 변경하지 않았다.

Planning contract는 `sha256:ed75d2373385ab882982c1816841adf273a88862bc36d634dbb55e4da04966cb`, scope report는 `sha256:e915fad9d2bbc9b690b11e8b80d1a1da3c0d5d72f6e588029892f9cd600d602a`다. 같은 source의 실제 E2E·성능·lifecycle은 Planning FAIL로 실행하지 않았다. S03/seed-89는 세 Skeleton에서 두 후보를 상세화한 뒤 둘 다 무결함·admissible로 검토돼 두 번째 Plan을 선택했다. 이 셀의 refinement 호출은 0이며 후속 호출을 refinement로 추정한 분석 요청은 실제 계보로 정정했다. `planning-s03-refinement-verification-v19-seed89-r2.json`에 근거를 보존했다.

## v19 중간 실패 분석과 v20 개발 보완

v19 Planning의 완료 셀에서 다음 원인을 확인했다. 원본 동결 source·입력·응답·원장·실패 판정은 유지하고, 보완은 개발 작업본에만 반영했다. 전체 18건의 완료·비용·무결성 감사와 구분한다.

- `S04/seed-17`은 Skeleton의 기여 관계 두 필드가 구조적으로 일치한 뒤에도 AC가 직접 요구한 생산 Task를 양쪽에서 누락했다. 첫 검토의 무결함 판정은 잘못됐고 상세 Plan의 최종 거절은 타당했다. 생성·검토 지침에 구조 일치와 의미상 완전성의 독립 검사를 추가했다. 단순 선행 관계나 validation 소유권만으로 모든 Task를 기여 집합에 넣지 않는다.
- `S01/seed-43`과 `S02/seed-89`의 첫 상세 검토는 결과 상태·보고서 내용의 관련성을 별도 검사 절차의 필수 연결로 잘못 확대했다. 명시된 절차와 phase는 계속 연결하되, 관련 검사라는 이유만으로 같은 AC의 필수 검사로 늘리지 않도록 v1 지침을 보완했다. S01은 추가 연결 후 선택됐으며, 최초 finding을 타당한 수정 요구로 표현하지 않는다.
- `S02/seed-89`의 최종 거절은 타당했다. 대상 프로젝트의 파일 목록·digest 비교가 관측 범위 밖의 별도 원본 fixture까지 무변경으로 증명한다는 Plan의 주장은 근거가 없다. 효과 금지와 실제 검사의 관측 범위를 구분하도록 공통 capability 지침을 보완했다. 단순 보호 금지를 모든 Task의 새 snapshot 의무로 확대하거나 등록 입력을 임의로 추가하지 않는다.
- `S04/seed-89`의 두 번째 Plan 작성은 원문 명령의 따옴표에 JSON escape를 중복 삽입해 엄격 인용 검사에서 실패했다. 실제 선택 문자열과 직렬화 표현을 구분하도록 v1 writer 지침을 보완했다. 원본 응답을 자동 보정하거나 validator를 완화하지 않았다.

근거는 운영 폴더의 `planning-s04-selection-failure-analysis-v19.json`, `planning-s01-refinement-verification-v19-seed43.json`, `planning-s02-original-link-finding-analysis-v19-seed89.json`, `planning-s02-selection-failure-analysis-v19-seed89.json`, `planning-s04-quote-failure-analysis-v19-seed89-r2.json`에 입력 hash와 함께 보존했다. 마지막 보고서의 최초본에 섞인 이전 실행의 정산 문구는 r2에서 해당 호출의 관측 범위로 바로잡았으며 최초본도 남겼다.

S04/seed-89에서는 첫 Plan이 실제 deterministic Gate와 Core admission을 통과해 feasible callback을 발생시켰지만, 다음 후보의 schema 오류로 전체 검색이 예외 종료되면서 첫 관측의 시각·Plan digest가 실패 checkpoint에서 유실됐다. 개발 작업본은 typed `PlanningPartialFeasibleObservation`을 별도로 수집해 실패 checkpoint에 보존한다. `passed=false`, `schema_valid=false`, `selected=false`와 원래 receipt는 유지하며 선택 결과·성공 성능 지표를 만들지 않는다. callback 이전에 실패하면 관측 여부는 false, 시각과 digest는 null이다. 과거 v19의 monotonic 시각은 재구성하지 않는다. 이 메타데이터 schema는 새 Planning 평가 계약에 결속한다.

실제 `run_full_planning_pipeline → _planning_cell → SkeletonFirstPlanner.search`와 Core Gate를 통과한 첫 후보 뒤 실패 및 첫 후보 이전 실패를 포함해 회귀 5개를 추가했다. 관측·qualification·Goal 예산 결속 관련 **32개**, 기존 역할 adapter·v1/v2 inspection·역할 관련 **77개**가 통과했다. 읽기 전용 지침 검토 `planning-guidance-review-v20.json`은 적용 경로와 양성·음성 경계에 차단할 충돌이 없음을 확인했다. 이는 실제 모델의 의미 판단 성공을 증명하지 않으며 새 동결 source의 전수 qualification이 필요하다. 현재 개발 후보는 v19의 모델·추론·예산·timeout·fixture·oracle·합격선을 유지한다.

## v20 동결과 전수 재검증

고정 source는 `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v20`, HEAD는 `b0b8c4d8a4ee4b7c0cb7fa11d389103b2235d3f2`, source manifest는 `sha256:47f59c125e153d3cf8f0dfdbdbadc7a9c7fb544c31b10825727b5ebc9c7421da`다. 동결 증명은 `workspace-v20-source-freeze-proof.json`, 운영 도구 결속은 `qualification-operations-registry-v20.json`이다. v19와 같은 Python 의존성·명시 역할 설정·예산·timeout·프로젝트 binding을 유지한다. 새 `자동화테스트\v20\d`는 892개 테스트 중 schema family 기대값 누락 2건으로 4/5 FAIL을 기록했다. 실제 모델 호출은 0회이며 원본 실패를 보존한다. 이후 역할 48 → Planning 18 → E2E 4 → 성능 36 및 실제 lifecycle 12건은 결정적 Gate를 통과한 같은 최종 source에서 수행한다. v19의 FAIL은 이 새 실행의 성공 선행 조건으로 재사용하지 않는다.

## 후속 판정

실제 역할 48 → Planning 18 → E2E 4 → 성능 36과 실행 lifecycle 관측을 같은 최종 source에서 수행한다. v13과 v15의 완료 결과·실패·사용량을 보존하고 보완한 새 source에서 전수 재검증한다. v19 Planning의 provider는 `plan-inspection-v1`이었다. 아래 근본 원인 보완 이후의 새 검증은 명시적 `plan-inspection-v2`로 결속하며, 제품 기본 provider는 승격 전까지 v1을 유지한다. E2E·성능은 아직 미실행이다. 부분 성공이나 단위 테스트만으로 1.0을 완료로 선언하지 않는다. 사용자는 작업·검증을 마친 뒤 `codex/inspection-recovery`를 `main`에 병합하도록 추가 지시했다. 기존 main의 GUI 프로토타입과 무관한 변경을 보존하고 병합 결과를 검증한다.


## v21 결정적 검사와 반복 실패의 근본 원인 보완

v21 고정 source는 `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v21`, HEAD `f27aa81633cf6c3d98b27691b367b062deec4cf8`, source manifest `sha256:568fe5d22d25da387d37b9bf2ad32178f2621338dae969d26e90fd8dab72ba48`다. 2026-09-06 21:04:25 UTC의 결정적 Gate는 **5/5·892개 테스트 / 129.013초·legacy 40개 무변경**으로 PASS다. 실제 역할·Planning 호출을 시작하기 전에 사용자의 요청으로 반복 실패를 재분석했다. v20의 production source와 실제 의미 판단 효과를 증명한 결과로 해석하지 않는다.

v19의 저장된 실제 요청을 다시 대조한 결과, `AC 일부를 직접 검증`하는 넓은 v1 설명과 `AC가 명시한 절차·도구·phase만 필수 연결`이라는 좁은 지침이 같은 요청 안에 공존했다. S01/43 및 S02/89의 관련 검사 연결 오탐을 모델의 단순 지침 불이행으로 단정할 수 없었다. 또 S03/89의 정상 Reviewer 최종 JSON 35,535자 중 inspection이 35,387자(99.58%)였다. 수동 인용·행렬·중복 참조 작성은 엄격한 quote/ref 실패의 표면을 늘렸다. 문자열 길이만으로 의미 오판의 인과를 증명하지는 않는다.

S04/17에서는 Skeleton의 구조 수정에 계보별 한 번의 기회를 사용한 뒤, 상세 Plan에서 실제 producer 기여 누락을 확인해도 고칠 수 없었다. S02/89에서는 관련 검사 연결 오탐을 고치며 기회를 사용한 다음, 별도 원본 fixture를 관측하지 않은 보호 검사라는 실제 결함을 고치지 못했다. S04/89에서는 앞선 admissible Plan이 있어도 두 번째 expander의 schema 실패가 전체 검색을 종료했다. 이전 partial feasible 메타 보존은 관측 손실만 줄였고 이 제어 흐름을 해결하지 않았다.

사용자가 이 분석에 따른 재작업을 승인하여 다음 개발 변경을 적용했다. v1의 strict inspection schema·validator·evaluator와 과거 FAIL은 보존한다.

- v2의 writer·reviewer·refiner·재심은 `validation_obligations.py`의 동일한 명시 검사 의무 정의를 사용한다. 기존 immutable citation/target catalog와 sparse scope 선택 compiler를 재사용해 수동 quote·전체 관계 장부를 요구하지 않는다.
- 명시적 `planning-recovery-v2`는 Skeleton 준비와 상세 Plan 수정에 각각 한 번의 기회를 두며 전체 호출·5개 version·Goal 토큰 예산은 공유한다. 상세 실패에서 돌아간 Skeleton 수정은 상세 슬롯을 소비한다.
- 직접 근거가 있는 `disputed`는 동일 Plan의 독립 재심을 계보별 한 번 허용한다. 원 지적의 유지/철회와 원 Core 판정·새 관측을 보존하며, 반박만으로 admission을 바꾸지 않는다. 실제 수정 기회와 재심을 구분한다.
- 종료·정책·단일 turn·사용량 정산이 확인된 후보의 schema 오류만 격리한다. 다른 정상 후보와 최초 feasible 관측을 보존하면서 qualification의 schema/전체 FAIL과 원시 비용은 유지한다. 결속 원문 파일의 무결성 실패는 별도 입력 오류로 전체 중단한다.
- 실제 CLI와 benchmark/resume에 같은 provider와 복구 정책을 전달한다. 다른 provider의 Planning PASS를 새 benchmark 선행증거로 사용하지 않는다. static11/S06 harness에도 명시적 예산·timeout·자동화테스트 프로젝트 결속을 추가한다.

새 경로의 결정적·통합 테스트와 운영 결속을 완료한 뒤 새 source를 동결한다. v2 승격에는 별도의 static11·S06 qualification13·실제 Goal evidence가 필요하고, 제품 완료에는 결정적 → 역할48 → Planning18 → E2E4 → benchmark36 → 실제 lifecycle12의 같은 source 결과가 필요하다. 아직 실행하지 않은 실제 모델 검증을 PASS로 보고하지 않는다.

## v22 결정적 통과와 진단 준비 경계 보완

v22는 HEAD `0b64fb8d97ce7df52f52d49d7adb0520f79d30a0`, source manifest `sha256:ee4de6687cc33a6c490e03c2ac0cd2dc11be88223a3effe9c6c8348241e317af`로 동결했다. 개발 Gate의 legacy pending 호환 오류 1건을 먼저 보완했으며, 개발·동결 환경 모두 결정적 **5/5·939개 테스트·legacy 40개 무변경**을 확인했다. 초기 개발 실패도 별도 원본 보고서에 보존한다.

실제 static11 준비에서는 `_planning_contract`의 필수 `policies` 인자를 진단 실행기가 전달하지 않아 중단됐다. `qualification-v22-static-prepare-failure.json`에 저장한 관측은 **모델 turn 0회·예산 원장 0개·지침 관측용 빈 thread 1개**다. 역할 호출이나 정적 11사례의 의미 검증 성공은 아니다. 부분 함수 테스트만으로는 진단 진입점의 API 연결 누락을 탐지하지 못했으므로, 정책 전달 수정과 전체 portable prepare를 실행하는 모의 런타임 회귀를 추가한 뒤 새 source에서 검증한다. v22 원본 source·실패·준비 artifact는 변경하지 않는다.

## v23 긴 경로 실패와 v24 재검증

v23은 HEAD `ecbdc29a132e54d86ce778bab95d03b901561688`, source manifest `sha256:c9c95fb8775652a2a3f17a31b656c951cd33434e47dc0572e775989a47231e21`로 동결했다. 개발·동결 환경의 결정적 Gate와 portable preflight·prepare가 모두 통과했으나, 첫 Goal 예산 기록에서 264자 경로의 파일 생성이 Windows 경로 제한으로 실패했다. 부모 폴더는 존재했고 **역할 호출·provider turn·미정산 예약은 모두 0**이었다. 프로젝트·예산 정책을 초기화한 원장 1개와 유효 History 2개는 남았다. 원본은 `qualification-v23-pre-model-windows-path-failure.json`과 해당 run의 FAIL summary에 보존한다.

수정은 Goal 원문을 `run/goal-bindings/전체-digest.json`에 보존해 중첩 경로를 줄이는 것으로 제한했다. 전체 digest·원문 일치 검사·Goal 계보별 예산 원장은 유지한다. 실제 SQLite 원장과 이전 위치가 260자 이상인 경로를 사용하는 회귀로 파일 생성, 재사용, 원문 변조 거부와 생성 검토 입력 manifest 포함을 확인했다.

v24 고정 source는 `C:\Users\sjs95\Documents\ChatGPT\자동화테스트\검증소스-v24`, HEAD `2f85a0b13e19f0b8dddd4b4ee43bb9f8407fef17`, source manifest `sha256:cea9e16d9dddded2cc0c176e471c2ce068eb223d2a9426ad9816aec90197842b`다. 개발·동결 환경에서 **결정적 Gate 5/5·942개 테스트**를 통과했고, 동결 Gate 완료 시점은 2026-09-06 23:04:24 UTC다. 계약은 `sha256:0b790e78f69c1206b73b883b3ebfe16e1412f1bc5dfcc1347403792e2d65fcf2`, 실제 static11 lock은 `sha256:38ab941a3e0d151b481d421119384902a09792dc34b55f800996dd9efa8c2042`다. 같은 예산·역할·timeout·프로젝트 결속으로 실제 진단을 시작했으며, 첫 `clean` 사례의 의미 검증 PASS는 전체 static11 또는 qualification PASS로 집계하지 않는다.


## v24 실제 static11 결과와 v25 연결 규칙 보완

v24 static11은 2026-09-06 23:38:02 UTC에 **11건 완료·1 PASS·10 FAIL**로 종료했다. 모든 호출의 schema·request/receipt 결속과 단일 turn 사용량은 확인됐고 schema recovery는 0회다. 완료된 의미 FAIL을 그대로 보존했으며 qualification13·역할48·Planning18 이후 단계는 이 source에서 실행하지 않았다.

원시 응답·고정 요청·원본 Goal·예산 정책·SQL 예약/정산·History·저장 thread 11개를 읽기 전용으로 대조한 `inspection-v24-static11-observation-audit.json`은 **무결성 PASS·의미 FAIL**이다. 실측 **705,460 token**은 입력 **603,439** + 출력 **102,021**이며 캐시 입력 **69,504**과 reasoning **84,555**는 각 입력·출력에 포함된다. 감사의 신규 모델 turn은 0회다. 감사 실행기의 Windows 출력 인코딩과 중복 import 경로 문제를 수정한 뒤 실행했으며 source·run·원장은 바꾸지 않았다.

실제 v2 요청에는 v1의 완전한 검사 기여 관계·다른 검사 ID로의 대체 금지·부분 scope 결함과 나머지 유효 연결의 독립성을 설명한 일부 규칙이 없었다. v1 source의 문구를 v2가 실제 전달받은 지침으로 인용한 초기 분석은 교정했다. v2의 공통 의미 정의와 필드 설명에 모든 일치 검사·등록 도구의 내부 실행·부분 결함의 독립성을 명시하고 실제 요청의 공통 정의 결속 및 compiler의 부분 결함 보존을 확인했다. v1 strict 계약·oracle·평가 합격선은 변경하지 않았다.

`semantic-explicit`와 `semantic-missing-link`에는 프로젝트 의존성의 무변경 검사가 file/diff 관측 범위를 넘는다는 별도 추가 finding이 있었다. 프로젝트 의존성과 실행 환경 패키지의 범위 해석은 미해결로 남긴다. 이를 통과시키려고 고정 fixture·oracle·결과를 바꾸지 않았다. 전체 사례별 평가와 원본 해시는 `static-v24-completed-root-analysis.json`에 보존했다.

v25는 HEAD `6da945d8beb43ccbe3423631bd391288574148cc`, source manifest `sha256:0257ee2b93cefbe48387ed1f46cd2459dce0c78f032d6c25005542fc86e9840b`다. 개발 결정적 Gate **5/5·943개 테스트 / 144.013초**를 통과했으며, 동결 환경 검증과 실제 static11의 새 결과는 별도로 기록한다. 앞선 v24 성공으로 이 변경본을 검증했다고 보고하지 않는다.

## 사용자 지시의 반복 중단 기준

사용자는 진전 없이 테스트를 약 5회 반복하면 중단하도록 지시했다. source 버전 변경·단위 테스트 수 증가·감사 도구 수정만으로 의미 품질의 개선을 주장하거나 반복 횟수를 초기화하지 않는다. 기존 v20~v24의 실패 반복도 고려하여, 다음 v25 static11에서 v24 대비 의미 PASS 수 또는 같은 사례의 누락·오탐이 실질적으로 개선되지 않으면 추가 실제 모델 반복 없이 중단한다. v21 등 모델 미실행 준비를 같은 실제 의미 평가 5회로 집계하지 않는다. 비교 기준과 중단 시 원본 보존·main 병합 보류는 `user-no-progress-stop-policy-20260907.json`에 기록했다.

## v25 의미 개선과 예산 중단

v25 static11은 **10 PASS·1 NOT_RUN·전체 FAIL**로 종료했다. 실행된 동일 10사례의 v24 결과는 1 PASS·9 FAIL이었다. 첫 5사례의 AC 관계 오류는 각각 `0→0`, `4→0`, `6→0`, `1→0`, `2→0`으로 줄고 의도한 결함 검출을 유지했다. 이 변화는 실제 의미 품질의 진전이며 단순 source 번호나 단위 테스트 수 증가와 구분한다. 다만 미실행 사례와 최종 qualification의 성공을 뜻하지 않는다.

실측 누계 **656,226 token**은 입력 **562,700** + 출력 **93,526**이다. 캐시 입력 **79,232**와 reasoning **77,287**은 각 입력·출력에 포함된다. 11번째 `semantic-missing-link`는 `656,226 + 다음 호출 예약 100,000 > 일반 사용 가능분 750,000`으로 호출 전에 차단됐다. Goal 총 상한 1,000,000 중 25%는 replan reserve이므로 일반 호출에 모두 사용할 수 없다. 예약액은 다음 호출의 실사용량 추정이 아니다. 실제 provider turn은 10회이며 11번째의 모델 사용량은 발생하지 않았다.

기존 감사기를 완화하지 않고 실행한 `inspection-v25-static11-observation-audit.json`은 예상 11건 미완료에 따른 `EXPECTED_PHASE_CALL_COUNT_INCOMPLETE`와 `FIXED_CASE_REQUEST_EXACT_ONCE_MISMATCH`를 유지해 **전체 무결성 FAIL**이다. 완료 10건의 고정 요청·원문·원본 Goal·정산·History·사용량·저장 작업의 프로젝트 결속과 감사 전후 불변성에는 추가 오류가 보고되지 않았다. 감사는 저장 작업 10개를 읽기 전용으로 확인했으며 신규 모델 turn은 0회다.

## 관측 범위 입력 결함의 독립 검토와 v26

앞 절에서 미해결로 남긴 의존성 범위는 별도 독립 검토로 원인을 좁혔다. 기존 정상 Plan은 파일 집합·diff로 의존성 상태까지 검증한다고 선언했지만, Goal·등록 자료·oracle은 해당 의존성 자원과 전후 상태를 식별하지 않았다. 실제 관측은 프로젝트 파일·보존 해시·AST·함수 계약·unittest였다. 이 입력의 과장된 검증 주장을 모델 오탐으로 단정하지 않는다.

`plan-inspection-v6-scope-boundary`는 6개 base와 5개 파생 Plan의 `val_task_scope_preservation` 문장을 실제 파일 보존 범위로 한정했다. 의존성 금지효과, AC 연결·bool 기대표·required defects·oracle·합격선과 13단계 순서는 보존했다. 원본 외부 입력과 v5·과거 FAIL도 변경하지 않았다. v1은 기존 v5 입력을 사용하고, 명시적 v2 진단 준비만 v6을 선택한다. `historical-r-s06-09-clean`은 과거 입력 그대로 남으며 새 정상 사례로 재인증하지 않는다. [독립 검토 기록](../tests/fixtures/engine/plan-inspection-v6-independent-review.md)에 23개 입력·18개 case·12개 직접 인용과 해시 대조 결과가 있다. 서브에이전트에는 `gpt-5.6-sol/high`를 명시했으며 메인이 실제 변경 범위와 근거를 다시 확인했다.

v26은 HEAD `f776f491cfa7d3744353314c1801c38b9d2b378d`, source manifest `sha256:87b207f935469732c43a27de7f99a2d035abdda51268cf309a82d06dbcb0b322`로 동결했다. 관련 회귀 **32개 / 10.961초**, 개발 전체 **946개 / 130.862초**, 동결 환경 전체 **946개 / 130.736초**가 통과했다. 개발·동결 결정적 Gate는 모두 **5/5 PASS**이며 계약은 `sha256:bf5f89efeae8a1b7ac72bbce5faee80c812a7b393e729f26a2b3469204a72e48`다. 동결 Gate 완료 시각은 2026-09-07 00:33:33 UTC이며, **v26 실제 모델 호출은 0회**다. 관측·동결·중단 근거는 `v25-budget-stop-v26-readiness.json`에 결속했다.

## 현재 재개 조건

같은 v25 실행을 복사해 사용량만 초기화하지 않는다. 새 source와 불변 평가 계약의 별도 run은 이미 승인된 검증 격리이지만, 현재 1m/100k/25에서 실제 static11과 qualification13을 다시 실행하면 후반 예산 부족이 반복될 가능성이 높다. 현재는 새로운 실제 모델 호출을 중단하고 예산 증액의 사용자 결정을 기다린다. 기존 사용자 지시에서 token 상한·호출 예약량 증가만 별도 승인 대상으로 지정했다.

검토용 제안은 v26의 static11·S06 qualification13 진단 Goal 상한만 **1,000,000→1,500,000 token**으로 바꾸고 호출 예약 **100,000**과 reserve **25%**는 유지하는 것이다. 새 qualification13에서 앞선 12회의 사용량을 v25 평균 또는 관측 최대치로 가정한 13번째 호출 admission의 추정 필요 상한은 각각 **1,183,295**, **1,253,814 token**이다. 미래 생성 단계의 실제 사용량은 아직 관측하지 않았으므로 제안에는 변동 여유를 두며 성공이나 총비용을 보장하지 않는다. `inspection-v26-budget-increase-proposal.json`과 `inspection-v26-budget-1500k-proposed.json`은 **미승인·미적용 제안**으로만 저장했고 원래 예산 파일을 변경하지 않았다.

v25의 10건 PASS는 이전 입력의 관측 근거다. 입력이 달라진 v26의 실제 검증으로 재사용하지 않는다. 최신 source의 static11·S06 qualification13 및 실제 Goal evidence, 역할48·Planning18·E2E4·성능36과 Engine lifecycle12가 남아 있다. 필수 검증이 완료될 때까지 **NO-GO·main 병합 보류**를 유지한다. 진전 없이 약 5회 반복하면 중단한다는 사용자 기준도 계속 적용한다.

## 초기 Skeleton 실패 복구 보완

사용자가 승인한 후속 구현은 v26의 공통 검사 정의·독립 검토된 v6 fixture를 유지하고, 별도로 합성 재현한 초기 Skeleton 실패 격리 누락을 수정한다. `initial_skeleton_review`와 `skeleton_refine`을 추가하여 초기 검토·수정·재검토를 후보별 격리에 연결한다. 검토 실패 후보 보존, 실패 수정의 1회 소비·version 불증가, 실제 요청·정산 결속, 새 검색의 동일 실패 재호출·수정 슬롯 복원 거부를 결정적·SQLite 회귀로 검증한다. 이는 v24·v25의 의미 판단 오류가 개선됐다는 증거가 아니다.

후속 동결본과 개발·동결 결정적 보고서, 사용량·회차·실행 파일 결속은 기존 운영 폴더의 새 `v27-*` 기록에 남긴다. 소스 동결 뒤에는 static11 → qualification13(생성 뒤 독립 생성 검토 포함) → 실제 Goal의 Plan 선택 → 역할48 → Planning18 → E2E4 → 성능36 순으로 같은 소스를 검증한다. 정상 Engine 12개의 정확한 Plan 활성화 결정과 동일 Goal 예산 원장을 승계한 실행·독립 검증을 완료한 다음 provider 호출 없이 lifecycle을 관측한다. 미관측 lifecycle은 성능 Gate 미완료다.

기본 `1m / 호출 예약 100k / 예비분 25%`는 유지한다. static11·qualification13 각 진단 Goal의 1.5m 제안은 전체 캠페인 예산이나 승인된 증액이 아니며, 추가 사용자 승인 전 실제 증액·모델 호출은 하지 않는다. 이미 허용된 구현·모델 호출 없는 준비·결정적 검증은 진행한다. 현재 qualification 목표 전체의 원인 가설별 수정·검증을 한 회차로 기록하며, 비교 가능한 실제 오류 감소 또는 이전에 막힌 필수 단계 완료만 진전으로 인정한다. source·오류 코드·담당·단위 테스트 수 변화로 연속 무진전 횟수를 초기화하지 않는다. 연속 5회에서 신규 실제 모델 호출을 중단하고 새 근거 없는 동일 재시도는 그전에도 차단한다. 모든 필수 Gate가 통과하기 전에는 main 병합과 완료 선언을 보류하고 1.0 cutover도 별도로 판정한다.
