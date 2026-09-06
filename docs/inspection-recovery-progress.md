# 검사 계약 구조 개선 진행 기록

이 작업의 완료 범위는 고정 검증 환경, 독립 11사례 실패 분포, 의미 판단을 보존하는 provider 참조 계약 개선, S06과 실제 Goal 완료 경로의 연결이다. 단계 A나 단위 테스트 통과만으로 전체 목표를 완료하지 않는다.

설계 출발점은 [개발 정체 개선안](D:/codex/자동화템플릿/참고자료/방향검토/VibeMarshal_개발정체_개선안_2026-09-05.md)이다. 과거 R25~R32 원시 응답·기대표·FAIL은 보존한다. 계획과 Core의 판정 권한, 정확한 Plan revision 활성화 경계도 유지한다.

| 단계 | 완료를 증명할 근거 | 현재 상태 |
|---|---|---|
| A. 실행 기반 격리 | 고정 worktree·공통 preflight·독립 fixture package·결정적 Gate | 완료 · `22d68c0` |
| B. 기존 계약 기준선 | 같은 역할로 static 11사례, 사례별 형식·참조·의미·운영 결과 | 완료 · PASS 7 / FAIL 4 |
| C. provider v2 | 직접 판단·인용 보존, adapter의 참조 전개, v1 회귀 보존 | 9차 AC 원문별 합집합 계약 검증 완료. Task 필드 설명 후보는 집중 28개·Gate 5/5 통과, 실제 모델 호출 없이 방향 재검토 |
| D. 새 계약 비교 | 새 실행의 static 11사례와 고정 기대표·실측 usage | 9차 11/11 PASS·252개 관계 일치·참조/운영 실패 0. v1 대비 보고 total token −12.47%, latency −40.80%의 단일 관측 |
| E. S06 재진입 검사 | static·expansion·독립 생성 검토·expanded-review 13단계 | 첫 clean semantic FAIL, 나머지 12 NOT_RUN. AC 관계 28개 일치, Task 산출물을 Worker 제출로 해석한 추가 finding 1개 |
| F. 실제 Goal 경로 | 고정 자연어에서 실제 Goal·Plan 선택·활성화·실행·독립 검증·GoalVerdict | NOT_RUN |

## 실행 기반

개발 checkout은 `D:\codex\fm-recovery`, v1 검증 checkout은 `D:\codex\fm-inspection-v1`이다. 첫 v2 고정본은 `D:\codex\fm-inspection-v2`의 detached `880874d5b5ae7e14183ecc2e2e17c10f4cc76a26`이며, 첫 provider 호출에서 발견한 schema 호환 실패 원본을 보존한다. 첫 완전 v2는 `D:\codex\fm-inspection-v2r1`의 `7d3b2013`, 두 번째 구조 보정본은 `D:\codex\fm-inspection-v2r2`의 `19055d71`, 세 번째 구조 보정본은 `D:\codex\fm-inspection-v2r3`의 `2fd29ad8`, 네 번째 scope 연결본은 `D:\codex\fm-inspection-v2r4`의 `12e496ad`에 각각 고정돼 있다. 각 검증본은 detached HEAD와 전용 `.venv`를 사용한다. 원격 추적 브랜치와 다른 checkout의 HEAD는 시작 provenance이며 실행 중에는 자신의 HEAD·source·Python·import origin·입력만 대조한다.

독립 입력 package는 `D:\codex\fm-inspection-inputs\r32-v1`이다. whitelist 입력 17개, 프로젝트 파일 3개, 등록 참고자료 1개를 byte 보존했다. manifest digest는 `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866`이다. 새 실행으로 옮길 때 ProjectMap의 물리 경로와 해당 digest 결속만 바꾸고, 허용 필드 밖 변경을 거부하는 relocation proof를 남긴다.

Codex executable은 `D:\codex\fm-inspection-runtime\codex-935a1911.exe`에 복제하고 `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d`로 결속했다. 역할 설정은 기존 `plan-inspection-general-reviewer-sol-xhigh-roles.json`을 명시적으로 전달한다. 실행 직전에 fresh model inventory로 지원 조합을 다시 확인한다.

기존 전체 테스트에는 Git에서 제외된 감사 입력 두 개가 필요하다. `spikes/gate0c/artifacts/control/gate0c.sqlite3`와 `spikes/gate0c/runs/profile-20260902-r7/unregistered/secret.txt`는 원본 그대로 검증 checkout에 복사하며 source 배포물에 넣지 않는다. 공통 preflight가 두 파일의 누락·변경을 검사한다. legacy의 checkout 이름 검사는 실제 프로젝트 root 일치 검사로 바꾸었으며 legacy 제품 source와 과거 판정은 변경하지 않았다.

## 실행 순서와 중단 경계

1. `python -m scripts.diagnostics.inspection_inputs export --source-run <원본 실행> --package <새 package>`로 입력을 한 번 보존한다. 이후 `verify --package <package>`로 검사한다.
2. 고정 검증 checkout에서 `python -m scripts.diagnostics.r_s06_10 preflight --run-root <새 inspection 실행> --fixture-package <package> --codex-bin <고정 executable> --role-config <절대 설정> --execution-mode development-diagnostic`을 실행한다.
3. 같은 checkout과 전용 Python으로 `run_deterministic(root=..., run_root=<새 실행>/deterministic)`을 수행한다.
4. 동일 입력 옵션으로 `prepare`를 실행한다. 모델 turn 없이 ephemeral thread의 실제 지침 경로를 관측하고 정책·inventory·요청·schema·기대표·source를 고정한다.
5. `python -m scripts.diagnostics.r_s06_10 run --run-root <새 inspection 실행>`으로 11사례를 직렬 관측한다. 새 실행은 저장형 역할 thread를 사용하고, Windows에서는 `Start-Process -WindowStyle Hidden`으로 대화의 포그라운드 세션 밖에서 시작하며 별도 운영 경로에 launch intent·PID·시작 시각·stdout/stderr를 남긴다. 실행 옵션은 잠금 뒤 교체할 수 없다.

완료된 응답의 schema·참조 오류 또는 의미 FAIL은 실패로 보존한 채 다음 독립 사례로 진행한다. provider가 명시적으로 실패 종료한 경우는 `provider_terminal_failed`, 완료·귀속이 불명확한 경우는 `external_unknown`으로 구분하며 둘 다 전체 중단한다. 실패한 사례 재호출과 schema recovery는 없다. 지침 probe를 포함한 미완료 intent는 먼저 관측해야 한다. 11사례 전수 관측은 qualification 통과와 별개의 결과다.

## 검증 기록

- 개발 Gate 첫 실행: 전체 642개 테스트 중 legacy 환경 의존 4건 실패. 나머지 Gate 4/4 통과. 실패 artifact는 `inspection-stage-a-devgate`에 보존했다.
- 환경 의존 4건은 직접 재검증하여 모두 통과했다. 다음 전체 실행은 새 summary 분류 key에 대한 기존 기대값 2건을 찾아 보완했고, 해당 실패를 `inspection-stage-a-devgate-v2`에 보존했다.
- 세 번째 결정적 Gate는 5/5 통과했다. 전체 테스트는 646개였다. 관련 최종 경계 회귀 26개도 통과했다.
- v1 독립 기준선은 고정 worktree에서 11/11 사례를 호출했다. 결과는 PASS 7, 의미 FAIL 3, model output FAIL 1이며 logical/provider/recovery는 11/11/0이다. 상세 근거는 [v1 독립 11사례 기준선](inspection-v1-static11-baseline.md)에 있다.
- v2는 직접 의미 필드와 typed target을 보존하고 반복 row·finding closure, project evidence 환산, taxonomy 값을 adapter가 계산하도록 별도 schema·compiler·evaluator·request binding으로 구현했다. 동일 clean 표본의 반복 참조 항목은 101개에서 22개로, provider 호환 target 보정 뒤 Reviewer strict schema는 16,512 bytes에서 10,609 bytes로 줄었다. 이 수치는 결정적 표본이며 실제 모델 성공률 증거가 아니다.
- v2 및 기존 경계의 전체 단위·통합 테스트와 누적 error receipt 분류 회귀가 통과했고, 커밋 `880874d5`의 최종 결정적 Gate는 669개 테스트를 포함해 5/5 통과했다. 같은 커밋의 고정 v2 worktree에서도 Gate 5/5와 preflight/prepare가 통과했다.
- 첫 v2 static 실행의 `clean` 호출은 모델 추론 전에 provider가 `target_refs.items.oneOf is not permitted`로 400을 반환해 중단됐다. 이 실행은 `D:\codex\fm-inspection-v2\.flowmarshal-engine-eval\runs\inspection-v2-static11-20260906`에 보존했다. v1 artifact·oracle·재시도 경계를 바꾸지 않고, union target을 같은 직접 의미를 담는 단일 `{kind, primary_ref, secondary_ref}` 구조로 바꾸며 kind별 ref 개수를 결정적으로 검사한다.
- provider 호환 보정 뒤 새 고정본의 static 11은 11/11 호출을 완료했지만 PASS 1, model output FAIL 6, semantic FAIL 4였다. v1보다 total token은 17.61%, provider duration은 9.65% 줄었으나 PASS는 7건에서 1건으로 감소했다. model output FAIL 중 5건은 직접 citation 객체 장부, 1건은 target과 영향 Task의 중복 작성에서 발생했다. 상세 근거는 [v2 독립 11사례 기준선](inspection-v2-static11-baseline.md)에 있다.
- 위 전체 실패 분포를 한 원인군으로 묶어 2차 구조 보정을 구현했다. adapter가 결속 입력에서 immutable citation catalog를 만들고 모델은 직접 evidence ID만 선택한다. validation·scope의 claim ref는 원본 ID·selector join으로 붙이고, finding의 affected Task는 typed target의 소유 관계와 직접 `task` target에서 계산한다. 당시 계약의 모델 bool·scope status·finding·target·직접 evidence 선택은 그대로 보존했다. Reviewer strict schema는 첫 v2의 10,609 bytes에서 9,258 bytes로 줄었다. 집중 회귀 22개와 전체 671개 테스트가 통과했고, 새 artifact root의 결정적 Gate도 contract `sha256:673fae74fb4637dcfe0b05fb747b8d1820031615cf6c76ebbdcaf0f92b240f83`로 5/5 통과했다.
- 2차 구조 보정의 고정 static 실행은 완료된 첫 7건 모두 schema·compiler를 통과해 중복 장부 실패가 제거됐음을 확인했다. 결과는 PASS 1, semantic FAIL 6 뒤 8번째 `stored-expanded`가 900초 timeout으로 `external_unknown`, 나머지 3건은 NOT_RUN이다. 미완료 ephemeral thread는 종료 뒤 관측할 수 없어 재호출·재개하지 않았다. 상세 근거는 [v2 2차 구조 보정 부분 실행](inspection-v2r2-partial-baseline.md)에 있다.
- 2차 실행의 요청 artifact에서 v1 trace 작성 규칙과 v2 금지 규칙이 동시에 제공되고, Goal source trace와 State·ProjectMap의 기계 메타데이터까지 citation 후보로 복제된 사실을 확인했다. 3차 보정은 v2 전용 trace 지침을 분리하고 semantic citation projection을 추가했다. State·ProjectMap 결속은 Core·preflight의 결정적 책임으로 유지하며 모델의 bool·status·finding·target은 보정하지 않는다. 동일 clean 표본의 catalog는 334개·67,857자에서 86개·21,035자로, 전체 request는 129,360자에서 74,789자로 줄었다. v1 장부 필드 지침이 v2 요청에 없음을 회귀로 고정했다. 집중 47개와 전체 671개 테스트가 통과했고, 새 결정적 Gate는 contract `sha256:6d8614e1678873c4ad4c3a00500aa8cd893a867cf49c8365a8a686df9acb1af5`로 5/5 통과했다. 실제 모델 개선은 새 고정 worktree의 별도 static 11에서 판정한다.
- 3차 고정 static 11은 11/11 schema·compiler·binding을 통과했고 PASS 4, semantic FAIL 7, model output/provider/recovery FAIL 0이었다. State·ProjectMap 가짜 finding과 timeout은 사라졌고 input은 사례별 49,236~51,081 token으로 줄었다. 남은 모든 실패는 AC 관계 누락·과잉에 집중됐으며, 같은 응답에서 supported scope 의미와 28행 bool 장부가 서로 달랐다. 상세 근거는 [v2 3차 구조 보정 독립 11사례 기준선](inspection-v2r3-static11-baseline.md)에 있다.
- 4차 보정은 모델이 각 원자 scope의 `claim`·status와 실제 절차를 직접 요구하는 AC의 양의 `criterion_refs`만 제출하게 한다. compiler는 이 의미 선택을 바꾸지 않고 모든 AC×validation의 bool·scope ID·closure·coverage witness를 확장한다. v1 상세 필드 규칙은 권위 설계 문서에 남기고 자동 주입되는 저장소 지침에서는 provider version별 경계를 명확히 분리했다. 전체 671개 테스트와 개발 결정적 Gate 5/5가 통과했으며 contract는 `sha256:7e9f81c564f0ffd60baa2fad5a96e98664112ade86702ab8d97b1fedfcbd5a30`이다. 상세는 [scope 양의 연결 계약 보정](inspection-v2r4-scope-link-contract.md)에 기록했다.
- 4차 고정 static 실행은 clean semantic FAIL, bad·wrong-goal PASS 뒤 combined가 900,125ms timeout으로 `external_unknown`이 되어 나머지 7건을 실행하지 않았다. 완료된 응답은 모두 schema·compiler·binding을 통과했지만 clean은 scope 22개·양의 criterion ref 32개를 만들고 sibling validation 세 개를 과잉 연결했다. 완료·귀속을 확인할 수 없는 turn은 재시도하지 않았다. 상세는 [4차 부분 실행 기준선](inspection-v2r4-partial-baseline.md)에 있다.
- 5차 보정은 scope를 실제 절차·status의 최소 집합으로 되돌리고, AC가 validation의 supported 절차를 명시적으로 요구한다는 양의 의미 판단만 별도 `ac_validation_links`로 제출하게 한다. compiler는 양의 link의 ID·scope 소유권·supported 상태를 검사하고 생략된 나머지 조합을 false·빈 scope로 확장한다. 집중 회귀 22개와 전체 671개 테스트가 통과했다. 개발 결정적 Gate도 contract `sha256:f8016844b24a240b5880589834379423ed00b98ef52690cdfddec2ebd3f63295`, report SHA-256 `de59d597c273014e345259b306e48f4532739e1abd0380212071ab5a3990224c`로 5/5 통과했다. 상세는 [5차 희소 양의 링크 계약](inspection-v2r5-sparse-link-contract.md)에 있다.
- 5차 고정 static 11은 11/11 provider turn을 완료했고 PASS 9, model output FAIL 1, semantic FAIL 1, schema recovery·external unknown 0이었다. clean·semantic-explicit를 포함한 9건의 관계 차이는 0이며 scope는 응답별 6~11개로 줄었다. bad는 `validation_scope` target에 기계적으로 불필요한 secondary ref를 작성해 schema 후검증에서 거부됐다. wrong-goal은 의도한 phase overclaim finding을 찾았지만 AC-004가 명시한 독립 Goal Test의 behavior-contract와 unittest validation 연결을 두 개 누락했다. 상세는 [5차 독립 11사례 기준선](inspection-v2r5-static11-baseline.md)에 있다.
- 6차 보정은 고정 AC×validation과 constraint×Task 조합을 content hash `inspection_target_catalog`로 제공한다. Reviewer는 결함 종류에 맞는 복합 catalog ID, 자신이 만든 scope ID, validation ID 또는 citation ID만 선택하고, adapter가 내부 typed target과 closure·evidence·영향 Task를 계산한다. 출력 schema에서 `kind`·`primary_ref`·`secondary_ref` 조립 장부를 제거했다. 또한 AC가 독립 Task 또는 Goal validation 단계를 묶음으로 명시하고 검사 책임을 열거하면 그 단계에서 열거된 책임을 실제 수행하는 각 validation을 연결하되, 같은 phase의 열거되지 않은 sibling에는 전파하지 않는 일반 규칙을 추가했다. 집중 회귀 22개와 전체 671개 테스트가 통과했다. 개발 결정적 Gate도 contract `sha256:801757cf8fdf8d0f45889204b8839828365e5676464df86b160ab5a5fca3ad1a`, source manifest `sha256:86b50c2e1e67fc1f41eef0430b5d043d8877d83394e44f7e68ee255d82f6eaa5`로 5/5 통과했다. 상세는 [6차 finding target catalog 계약](inspection-v2r6-target-catalog-contract.md)에 있다.
- 6차 고정 static 실행은 clean PASS, bad semantic FAIL, wrong-goal PASS 뒤 combined가 900,187ms timeout으로 `external_unknown`이 되어 나머지 7건을 실행하지 않았다. bad는 의도한 scope overclaim finding을 검출했지만 `ac_004`를 Task unittest·파일 보존 sibling까지 두 건 과잉 연결했다. timeout intent의 저장 상태를 고정 executable로 다시 읽는 시도도 `thread not loaded`여서 재개·재호출하지 않았다. 상세는 [6차 부분 실행 기준선](inspection-v2r6-partial-baseline.md)에 있다.
- 7차 보정은 `ac_validation_links`의 중복 `validation_id`와 compiler가 사용하지 않던 `requirement_claim`을 제거하고, 모델이 AC별 `supported scope_ids`만 한 행으로 제출하는 `ac_scope_requirements`를 도입했다. adapter는 scope 소유 validation을 join해 전체 AC×validation 행렬을 만든다. 단계의 경계·순서 문구와 특정 단계에 결속된 검사 책임을 구분해 Goal Test의 책임 목록을 Task sibling에 전파하지 않는 일반 규칙도 고정했다. 집중 22개와 전체 671개 테스트가 통과했고, 결정적 Gate는 contract `sha256:65ff56d07589b5c4d9d35d7b3a91a7792f53f9ce4707e3a612452baa287f3d68`, source manifest `sha256:24023076cb7df9abaefc78473d68166bf85b58e4af26e659e7fb5de1d9e4b914`로 5/5를 통과했다. 상세는 [7차 AC scope 선택 계약](inspection-v2r7-ac-scope-contract.md)에 있다.

- 7차 고정 static 실행의 clean·bad·wrong-goal은 모두 PASS였고 AC 관계 차이도 0이었다. 네 번째 combined는 대화 실행 중단 후 프로세스가 사라져 terminal을 확보하지 못했다. 저장 상태 조회도 `thread not loaded`였으므로 `external_unknown`으로 보존했으며 나머지 7건은 NOT_RUN이다. 이는 이전 6차의 900초 timeout과 다른 운영 중단이다. 상세는 [7차 부분 실행 기준선](inspection-v2r7-partial-baseline.md)에 있다.

7차 종료 시점에는 전체 qualification, Functional Alpha와 1.0 cutover가 NO-GO였고, 의미 계약을 유지하며 운영 복구 보완을 [저장형 진단 실행 준비](inspection-durable-runtime.md)에 분리했다. 이후 static 11과 새 계약의 진행은 아래 기록을 따른다.

준비 시점에 저장형 운영 보완을 `a3d801f`에 커밋·push했고 새 고정 작업본 `D:\codex\fm-inspection-v2r8`에서 preflight·673개 포함 Gate 5/5·prepare를 완료했다. lock은 `sha256:e2d78fe754f67361d1457c285d1a88b40355783aa03d40921c64cc5d21db8fef`이며 당시 11사례 호출은 0회였다. 별도 1회 운영 probe는 App Server 종료 후 같은 thread·turn·최종 응답을 재개 없이 읽는 데 성공했다. 이후 실행 결과는 아래에 기록한다.

이후 명시된 현재 경계 검증 요청에 따라 준비된 8차를 한 번 실행했다. 최종 결과는 9 PASS·2 의미 FAIL이며 11개 schema·compiler·binding, 사후 재계산과 종료 후 저장 조회는 통과했다. 두 실패는 같은 AC-004의 Task oracle 연결 누락이다. 5차 대비 총 token은 4.09%, receipt latency는 2.54% 증가했으며 PASS 수도 9개로 같다. 기존 자료 1,637개를 보존했다. 추가 보정·재호출 없이 [8차 경계 인계](inspection-v2r8-boundary-handoff.md)에 원인군과 다음 선결 조건을 남긴다. 전체 Goal과 S06·cutover는 미완료다.

8차의 새 실패 근거를 검토해 9차에서는 각 AC의 statement·validation_intent가 명시한 scope 선택을 나누고 adapter가 합집합을 계산하도록 바꿨다. 모든 AC의 두 원문 목록을 요구하며 빈 선택은 보충하지 않는다. 집중 26개와 전체 677개 포함 개발 Gate 5/5가 통과했다. 기존 source와 실제 응답을 통한 진단 가설, 변경 경계와 새 실행 수용 기준은 [AC 원문별 검사 의무 분리](inspection-v2r9-source-requirements.md)에 기록한다. 기존 실제 결과를 새 계약의 PASS로 재해석하지 않는다.

9차 source `92b11e1`의 고정 worktree에서도 677개 포함 Gate 5/5·preflight·prepare가 통과했다. 이전 실행과 같은 11개 기대표·역할·Goal 원문을 확인한 뒤 저장형 thread를 쓰는 숨김 프로세스에서 실제 진단을 시작했다. 실행·관측 경로와 lock은 위 9차 문서에 기록하며, 완료 전에는 부분 관측을 전체 PASS로 올리지 않는다.

9차는 11/11 사례를 모두 PASS로 완료했다. 252개 기대 관계와 finding이 일치했고 재호출·recovery·unknown은 0이었다. 이전에 빠졌던 두 Task oracle 연결 finding을 새 응답이 직접 제출했다. v1 대비 total token 12.47%·receipt latency 40.80% 감소는 같은 전체 사례의 provider 보고값을 비교한 단일 관측이며 정식 성능 Gate는 아니다. 완료 뒤 원본 보존·29개 결과 확인과 저장 응답 재관측도 통과했다.

새 source와 결정적 Gate를 보존한 상태에서 별도의 S06 qualification 13단계를 실행했다. 첫 clean에서 28개 AC 관계는 일치했으나 Task의 논리적 `produces`를 Worker의 미래 Validator 결과 제출 요구로 읽은 추가 finding 하나 때문에 중단됐다. 호출은 1/1/0이며 뒤 12단계는 NOT_RUN이다. 종료 후 같은 저장 thread·turn·응답, 두 실행 lock과 artifact 보존을 확인한 19개 검사가 통과했다. 원인과 전체 Goal의 남은 근거는 [S06 재진입 기록](inspection-s06-reentry.md)에 이어간다. 자연어 Goal에서 실제 GoalVerdict까지의 동일 계보 검증은 아직 NOT_RUN이다.

이후 Task 필드 설명 후보를 작성했지만, 사용자가 검증단 수정의 반복을 지적하고 다른 대화와 길 찾기 알고리즘을 비교하도록 요청했다. 새 모델 호출 없이 [탐색 방향 비교](inspection-search-direction-review.md)를 수행했다. 상세 Plan의 수정 가능한 실패가 검색의 생성·정제 단계로 돌아가지 않아 예산 14회 중 4회 사용 후 선택 없이 종료되는 제어 흐름을 합성 입력으로 확인했다. 입력 설명 후보는 Gate 5/5·집중 28개까지 보존하며, 다음 검토의 중심은 상세 Plan 실패 피드백과 실제 Goal 경로다. 이 알고리즘 보완의 구현·실모델 검증은 아직 수행하지 않았다.
