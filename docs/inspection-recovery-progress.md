# 검사 계약 구조 개선 진행 기록

이 작업의 완료 범위는 고정 검증 환경, 독립 11사례 실패 분포, 의미 판단을 보존하는 provider 참조 계약 개선, S06과 실제 Goal 완료 경로의 연결이다. 단계 A나 단위 테스트 통과만으로 전체 목표를 완료하지 않는다.

설계 출발점은 [개발 정체 개선안](D:/codex/자동화템플릿/참고자료/방향검토/VibeMarshal_개발정체_개선안_2026-09-05.md)이다. 과거 R25~R32 원시 응답·기대표·FAIL은 보존한다. 계획과 Core의 판정 권한, 정확한 Plan revision 활성화 경계도 유지한다.

| 단계 | 완료를 증명할 근거 | 현재 상태 |
|---|---|---|
| A. 실행 기반 격리 | 고정 worktree·공통 preflight·독립 fixture package·결정적 Gate | 완료 · `22d68c0` |
| B. 기존 계약 기준선 | 같은 역할로 static 11사례, 사례별 형식·참조·의미·운영 결과 | 완료 · PASS 7 / FAIL 4 |
| C. provider v2 | 직접 판단·인용 보존, adapter의 참조 전개, v1 회귀 보존 | 구현·결정적 검증 완료, provider schema 호환 보정 검증 중 |
| D. 새 계약 비교 | 새 실행의 static 11사례와 고정 기대표·실측 usage | 완료 · PASS 1 / FAIL 10, 후속 구조 보정 필요 |
| E. S06 재진입 검사 | static·expansion·독립 생성 검토·expanded-review 13단계 | NOT_RUN |
| F. 실제 Goal 경로 | 고정 자연어에서 실제 Goal·Plan 선택·활성화·실행·독립 검증·GoalVerdict | NOT_RUN |

## 실행 기반

개발 checkout은 `D:\codex\fm-recovery`, v1 검증 checkout은 `D:\codex\fm-inspection-v1`이다. 첫 v2 고정본은 `D:\codex\fm-inspection-v2`의 detached `880874d5b5ae7e14183ecc2e2e17c10f4cc76a26`이며, 첫 provider 호출에서 발견한 schema 호환 실패 원본을 보존한다. 각 검증본은 detached HEAD와 전용 `.venv`를 사용한다. 원격 추적 브랜치와 다른 checkout의 HEAD는 시작 provenance이며 실행 중에는 자신의 HEAD·source·Python·import origin·입력만 대조한다.

독립 입력 package는 `D:\codex\fm-inspection-inputs\r32-v1`이다. whitelist 입력 17개, 프로젝트 파일 3개, 등록 참고자료 1개를 byte 보존했다. manifest digest는 `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866`이다. 새 실행으로 옮길 때 ProjectMap의 물리 경로와 해당 digest 결속만 바꾸고, 허용 필드 밖 변경을 거부하는 relocation proof를 남긴다.

Codex executable은 `D:\codex\fm-inspection-runtime\codex-935a1911.exe`에 복제하고 `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d`로 결속했다. 역할 설정은 기존 `plan-inspection-general-reviewer-sol-xhigh-roles.json`을 명시적으로 전달한다. 실행 직전에 fresh model inventory로 지원 조합을 다시 확인한다.

기존 전체 테스트에는 Git에서 제외된 감사 입력 두 개가 필요하다. `spikes/gate0c/artifacts/control/gate0c.sqlite3`와 `spikes/gate0c/runs/profile-20260902-r7/unregistered/secret.txt`는 원본 그대로 검증 checkout에 복사하며 source 배포물에 넣지 않는다. 공통 preflight가 두 파일의 누락·변경을 검사한다. legacy의 checkout 이름 검사는 실제 프로젝트 root 일치 검사로 바꾸었으며 legacy 제품 source와 과거 판정은 변경하지 않았다.

## 실행 순서와 중단 경계

1. `python -m scripts.diagnostics.inspection_inputs export --source-run <원본 실행> --package <새 package>`로 입력을 한 번 보존한다. 이후 `verify --package <package>`로 검사한다.
2. 고정 검증 checkout에서 `python -m scripts.diagnostics.r_s06_10 preflight --run-root <새 inspection 실행> --fixture-package <package> --codex-bin <고정 executable> --role-config <절대 설정> --execution-mode development-diagnostic`을 실행한다.
3. 같은 checkout과 전용 Python으로 `run_deterministic(root=..., run_root=<새 실행>/deterministic)`을 수행한다.
4. 동일 입력 옵션으로 `prepare`를 실행한다. 모델 turn 없이 ephemeral thread의 실제 지침 경로를 관측하고 정책·inventory·요청·schema·기대표·source를 고정한다.
5. `python -m scripts.diagnostics.r_s06_10 run --run-root <새 inspection 실행>`으로 11사례를 직렬 관측한다. 실행 옵션은 잠금 뒤 교체할 수 없다.

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

전체 qualification, Functional Alpha와 1.0 cutover는 아직 NO-GO다. 실제 모델 응답의 개선이나 최종 Goal 완료는 아직 증명하지 않았다.
