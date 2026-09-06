# Reviewer 참조·scope 축소와 저장형 복구 경계 인계

이번 제한 검증은 **FAIL**이다. 현재 source의 고정 static 11사례를 한 번 관측하여 **9 PASS·2 의미 FAIL·NOT_RUN 0**을 얻었다. 모든 11개 응답은 schema·compiler·request/receipt binding을 통과했지만, `stored-expanded`와 `stored-multi-defect`에서 AC-004에 필요한 Task oracle 연결과 해당 누락 finding을 놓쳤다. **S06 qualification·Functional Alpha·1.0은 NO-GO**다.

이 인계는 참조 표현과 운영 복구 경계의 검증 결과다. 전체 Goal 완료, S06의 13단계 qualification, 새로운 planning pipeline 또는 실제 프로젝트 E2E 완료를 뜻하지 않는다. 이번에는 실패를 보존하고 추가 source 보정·재호출 없이 경계를 종료한다.

## 재사용과 실행 결속

현재 turn에 제공된 실제 정책은 `danger-full-access`, `approval_policy=never`였다. 개발 checkout `D:\codex\fm-recovery`의 시작 HEAD는 `95b544d43f8c5ccee9fed5e8e408d4b62eadf3ce`, fetch한 `origin/main`은 `11a27c7d76a9c794fb0e3aee4368da3cfc58ff38`이었다. 두 checkout 모두 clean이며 개발 branch는 origin/main보다 13 commit 앞서 있었다.

이미 완료된 **673개 테스트와 결정적 Gate 5/5**, 저장형 단일 thread의 종료 후 조회 probe를 재실행하지 않았다. 현재 source와 입력 lock에 다시 대조하여 유효함을 확인했다. v2r5~v2r7 raw는 새 checkpoint로 이어 붙이지 않고 독립 기준선으로 보존했다. 준비만 되어 있던 v2r8의 실제 호출은 이번 요청에서 처음 수행했다.

| 항목 | 결속 |
|---|---|
| 고정 worktree | `D:\codex\fm-inspection-v2r8` |
| 고정 HEAD | `a3d801f3556ffbf13030abeb872644453f1c2e46` |
| run root | `D:\codex\fm-inspection-v2r8\.flowmarshal-engine-eval\runs\inspection-v2r8-static11-20260906` |
| source manifest | `sha256:18fa14077f1cae4164be6ceac25d340b795f0a632e885086a9b6fa066058037c` |
| fixture package | `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866` |
| workspace preflight | `sha256:91e376790f32601667a08ec46cdd5db269a8b89642c2a7e987a700e331164211` |
| prepare lock | `sha256:e2d78fe754f67361d1457c285d1a88b40355783aa03d40921c64cc5d21db8fef` |
| 결정적 Gate contract | `sha256:77233a6c0da4290c9ed3470529770b771bb0302ac326b3de6e897ea74e3b7fe3` |
| 고정 Gate report SHA-256 | `df036d962c35e574500df1dc8680fb6f8809260880a36dd7425fa5eb81016f04` |
| summary SHA-256 | `c29668823bcd0733f3869a69ea021ef5296459899c0c21abf82ddcd6c6dffaf1` |
| 모델 / effort / fallback | `gpt-5.6-sol` / `xhigh` / 없음 |
| 실제 정책 / thread | full-access·never / `ephemeral=false` |
| logical / provider / schema recovery | 11 / 11 / 0 |

실행 직전과 각 호출에서 고정 executable, 실제 정책, fresh model inventory와 선택 조합, 원본 역할 설정, 입력·지침·schema·기대표·source 결속을 검사했다. Windows 숨김 프로세스 PID `16364`는 2026-09-06 00:30:45 UTC에 시작했고 01:05:35 UTC에 종료된 것으로 관측했다. 시작 intent·PID·시각·로그는 `D:\codex\fm-inspection-observations\v2r8-launch`에 있다.

8차는 저장형 운영 revision이다. v2r7 대비 모든 11개 요청의 `instructions`, `output_schema`, `model`, `effort`, `timeout_seconds`는 같으며, 의미 schema·compiler·evaluator source도 그대로다. 자동 주입 운영 지침·물리 경로·digest는 새 실행에 결속했다.

## 사례별 결과

| 사례 | 의미 판정 | AC 관계 차이 | input token | output token | receipt latency ms |
|---|---|---:|---:|---:|---:|
| clean | PASS | 0/28 | 52,863 | 10,210 | 188,844 |
| bad | PASS | 0/28 | 52,889 | 10,738 | 198,266 |
| wrong-goal | PASS | 0/28 | 52,863 | 10,910 | 201,312 |
| combined | PASS | 0/28 | 53,020 | 8,723 | 162,328 |
| boundary-clean | PASS | 0/20 | 51,482 | 6,644 | 124,953 |
| missing-link | PASS | 0/20 | 51,477 | 9,377 | 176,734 |
| future-result | PASS | 0/20 | 51,444 | 7,392 | 139,688 |
| stored-expanded | FAIL | 1/12 | 50,718 | 9,251 | 171,282 |
| semantic-explicit | PASS | 0/28 | 58,144 | 7,577 | 259,922 |
| stored-multi-defect | FAIL | 1/12 | 50,480 | 10,479 | 205,828 |
| semantic-missing-link | PASS | 0/28 | 52,956 | 12,186 | 248,141 |

전체 252개 AC 관계 중 2개가 다르고 그에 대응하는 두 finding이 누락됐다. 관계 합계가 높다는 이유로 사례 FAIL을 상쇄하지 않는다. `summary.outcomes.success=11`은 provider·형식·결속의 정상 완료이며 **의미 PASS 11개가 아니다**. 이번 실행의 `external_unknown`, incomplete, provider terminal failure와 model output failure는 모두 0이다.

## 두 실패의 공통 원인군

실패 분류는 **`implementation`**, 세부 진단은 **Reviewer 의미 판정 결과의 누락(`semantic`)**이다. 모델 제출 결과가 고정 AC 요구를 충족하지 못했다는 분류이며, Python compiler 결함이나 Goal/Plan 계약 자체의 결함이 확정됐다는 뜻은 아니다. 입력 부족·stale·환경 오류·참조 결속 실패는 관측하지 않았다.

| 사례 | 누락한 필수 관계 | 놓친 고정 defect | 다른 직접 결과 |
|---|---|---|---|
| stored-expanded | `ac_004 → validation_task_oracle_and_unittest` | `ac004-combined-task-link` | finding 없이 rating 제출 |
| stored-multi-defect | `ac_004 → val_task_oracle` | `ac004-task-oracle-link` | `task-phase-overclaim`은 `TASK_ORACLE_BEHAVIOR_SCOPE_OVERCLAIM`으로 검출 |

두 사례의 Goal은 같은 `input-goal.json`이다. AC-004의 statement는 독립 Goal Test를 요구하고 validation_intent에는 “oracle.py를 task phase와 goal phase로 구분해 실행한다”가 명시돼 있다. 따라서 Goal Test 책임과 구분되는 명시적 Task oracle 절차도 해당 AC 관계에 보존해야 한다.

`stored-expanded` 응답은 `scope_task_oracle_phase`를 `supported`로 정의하고 “등록된 oracle.py를 task phase로 실제 실행한다”고 썼다. 하지만 AC-004의 `scope_ids`에는 Goal scope 6개만 선택했다. `stored-multi-defect`도 AC-004에 Goal scope만 선택했다. Adapter가 scope의 소유 validation을 잘못 join한 것이 아니라, 모델의 양의 의미 선택에 Task phase가 없어서 false로 전개됐다. 이를 adapter가 자동으로 true로 보정하지 않았다.

직접 근거는 각 사례의 `case-expectations/<case>.json`, `calls/08-compact_plan_reviewer/result.json`, `calls/10-compact_plan_reviewer/result.json`, `<case>-assessment.json`과 `binding-verification.json`이다. 사후에 원시 envelope로 assessment를 재계산해 저장 판정과 완전히 일치함을 확인했다. oracle·threshold·기대값·raw response는 변경하지 않았다.

v2r5에서는 이 두 사례가 PASS였고 `bad`·`wrong-goal`이 FAIL이었다. 이번에는 그 네 사례의 결과가 뒤바뀌어 전체 PASS 수는 여전히 9개다. **기계적 참조 실패는 제거됐지만 전체 의미 정확도의 개선은 입증되지 않았다.**

## 같은 입력의 token·latency 관측

v2r5와 이번 실행은 원본 fixture package, 11개 사례·순서·역할 설정, 모든 `ac_validation_rows`·`expected_defects`가 동일하다. 두 실행 모두 11개 terminal usage를 확보했다. 의도적으로 바꾼 prompt/schema와 운영 지침, 물리 경로 결속까지 완전 동일한 실험은 아니며, 단일 순서의 관측 비교다.

| 지표 | v2r5 | v2r8 | 변화 |
|---|---:|---:|---:|
| input token | 550,373 | 578,336 | +5.08% |
| output token | 104,651 | 103,487 | −1.11% |
| reasoning token | 83,158 | 85,029 | +2.25% |
| total token | 655,024 | 681,823 | +4.09% |
| receipt latency ms | 2,025,940 | 2,077,298 | +2.54% |
| provider duration ms | 2,013,906 | 2,063,975 | +2.49% |

이번 cached input은 98,560 token이다. cached input은 input의 부분집합이고 reasoning은 output에 포함되므로 중복 합산하지 않는다. 빈 새 thread의 첫 turn이라는 실제 receipt·terminal 결속으로 usage를 귀속했다. 청구 금액과 구독 한도 차감량은 제공되지 않아 추정하지 않았다. 이 비교는 비용·지연 개선이나 별도 formal benchmark Gate의 PASS 근거가 아니다.

## 복구 관측과 보존 감사

- source·workspace·instruction·격리 입력·request/receipt/terminal·usage 결속 검사가 모두 통과했다.
- v2r5의 635개, v2r6·v2r7의 각 501개 자료 파일을 사전 SHA-256 목록과 대조했다. 총 1,637개가 그대로이며 생성 cache는 목록에서 제외했다. 과거 unknown은 과거 실행에 남긴다.
- 원시 응답에서 11개 assessment를 다시 계산하고 call artifact·receipt·provider usage·summary input digest를 재대조했다. 모두 저장 결과와 일치했다. 이 감사에는 추가 모델 호출이 없다.
- 별도 App Server에서 `missing-link`의 저장 완료 상태를 읽었다. 진단 프로세스 종료 후에는 두 실패 사례와 마지막 사례의 같은 thread·turn·최종 JSON을 `thread/read`만으로 확인했다. resume·재호출은 없었다.
- 실제 모델을 쓴 기존 단일 storage probe와 이번 11사례는 서로 다른 검증이다. 기존 probe의 token을 이번 평가 비용에 합치지 않았다.
- 새 Engine 원장 쓰기 0, Plan 활성화 없음, Worker 실행 없음이다. frozen legacy source·v1 검사기·fixture/oracle/threshold는 이번에 바꾸지 않았다.

감사 root는 `D:\codex\fm-inspection-observations\reviewer-boundary-close-20260906`이다.

| artifact | SHA-256 |
|---|---|
| before.json | `dc4653b941b19731420dd4b0a1d2d5b169e4fe81d0547ea8c71ab878c89c5857` |
| boundary-verification.json | `079553ac9cb01ff68d1621955210bbd7995dc179cb4b2c6d9776acb321758212` |
| token-latency-comparison.json | `8337d28c40ee78d254efa6b89656bb0906d236c90669c4b5f1e51b3e5840d82c` |
| post-exit-thread-read.json | `76739571854196c70deb89dd94eb635df0e6a910a394746e8efad59876716e0c` |

## 다음 선결 경계와 배포 범위

다음 선결 조건은 **같은 Goal의 명시적 task/goal 절차를 Plan의 validation 구성과 무관하게 일관되게 보존하는 의미 판단**이다. 이번 두 실패를 같은 원인군으로 검토하고, 기존 성공 사례와 함께 수용 기준을 고정해야 한다. 특정 사례 ID를 prompt에 추가하거나, 근거 없이 adapter가 관계를 채우는 보정으로 닫지 않는다.

검토할 설계 가설은 Goal의 명시적 검사 의무를 의미 단계에서 한 번 확정해 Plan 표현이 바뀌어도 보존할 수 있는지다. 현재 결과만으로 새 schema나 Goal 계약 변경을 채택하지 않는다. 의미 계약·입력이 바뀌면 그 새 결속으로 검증해야 하며 이번 FAIL checkpoint를 PASS로 재사용하지 않는다.

새 계약의 static 11이 모두 통과한 뒤에만 기존 **qualification 13 → 전체 planning pipeline·실제 프로젝트 E2E → token/latency Gate**로 진행할 수 있다. 실제 Goal의 정확한 Plan revision 활성화와 독립 validation·GoalVerdict도 별도 선결 조건으로 남는다. 제품 기본 provider는 v1이며 v2를 기본값이나 1.0으로 승격하지 않았다.

이번 turn의 제품 source 추가 변경은 없다. 앞서 검증·push된 Reviewer/격리/복구 source를 보존하고, 이 인계·진행 기록·문서 지도를 commit한 뒤 clean 상태의 main에 fast-forward로 통합한다. 실제 commit·push·HEAD=origin/main=원격 main 및 clean 결과는 자기참조를 피하여 감사 root의 `delivery.json`과 최종 응답에 기록한다. callback·재위임·예약·다음 작업 생성은 수행하지 않는다.
