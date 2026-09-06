# Goal과 상세 Plan 실패 피드백 구현과 검증

2026-09-06의 [탐색 방향 검토](inspection-search-direction-review.md)를 이어 상세 Plan의 거절 뒤 검색이 종료되던 경로를 보완했다. 기존 검토 결과의 정답·finding·합격 조건과 inspection provider 기본값은 바꾸지 않는다.

## 변경 동작

수정 가능한 상세 실패에는 원본 계약·finding·직접 근거를 전달한다. `plan_refiner`는 상세 계약을 수정하거나, Task 의미·DAG 수정이 필요하면 새 Skeleton을 제안하거나, 원문과 지적이 충돌하면 `disputed`, 판단할 자료가 부족하면 `unresolved`를 반환한다. 수정의 이유와 근거는 최소 의미 출력이며, 기존 inspection의 반복 장부를 이 역할에 추가하지 않는다. 수정 Plan은 기존 compiler의 Skeleton 보존 검사와 Core Gate·독립 Reviewer를 거친다. refiner의 자체 inspection 장부가 없다는 점을 독립 검토 생략으로 해석하지 않는다.

원본 평가를 보존하고 같은 Plan의 새 revision을 만든다. SQLite의 Task ID는 새로 만들며 `task_ref`로 의미 관계를 비교한다. 반박은 원래 거절을 자동으로 뒤집지 않는다. 같은 후보의 Skeleton·상세 수정은 최대 한 번이고, 추가 상세 후보도 5-version 예산에 포함한다. 상세 수정에는 잔여 2회, Skeleton 경로에는 4회가 필요하다. ID·배열 순서·비용만 바뀐 제안은 기록하고 재검토하지 않는다. 이후 실패를 같은 검색에서 반복 수정하지 않는다.

실제 adapter는 검증된 request·output·receipt와 원본 평가·컴파일된 제안의 digest를 연결한다. CLI의 live와 outcome-file 입력 모두 검색 전체를 원장 후보·Reviewer 제출물·판정에 대조하고 History에 남긴다. History에 기록되기 전 외부 제출물은 비권위 관측이며 암호학적 서명으로 취급하지 않는다.

## 검증 범위

집중 회귀는 수정 성공, 원래 실패 보존, Reviewer 반박, 호출·version·계보 한도, ID만 바뀐 무진전, 비수정 가능 finding, Skeleton 경로, 요청·응답 결속과 실제 SQLite의 정확한 digest 활성화 경계를 검사한다. 기존 의미 경계 회귀 일부는 최초 실패를 검사하도록 refinement 한도를 0으로 명시했고, 나머지는 명시적 미해결 응답으로 원본 거절이 유지되는지 확인한다.

전체 결정적 Gate 및 실제 단일 planning 실행의 결과는 아래 실행 기록에 추가한다. 실제 모델이 생성한 계획의 성공, 실패 뒤 개선, 활성화 이후 Goal 완료는 각각 별도 근거가 필요하다. 합성 회귀 성공을 S06·전체 qualification·실제 GoalVerdict로 승격하지 않는다.

## 실제 계획 준비

`scripts/diagnostics/planning_feedback_live.py`는 고정 worktree와 명시적 역할 설정·Codex executable을 받아 기존 단일 자연어 시나리오를 Goal 정규화부터 Plan 원장 등록까지 진행한다. 실행별 새 프로젝트 복사본과 DB를 사용하며 요청·원시 관측·receipt·검색 결과를 보존한다. 선택된 Plan이 있으면 사용자 검토용 파일과 정확한 revision·활성화 digest를 내보낸다. 이 단계에서 Plan 활성화와 Worker 실행을 자동으로 수행하지 않는다.

## 실행 기록

- 개발 결정적 Gate는 696개 단위·통합 테스트를 포함해 5/5 통과했다. contract는 `sha256:c64561c936f2befa868a305923e35dd913dc91dad48d1ac6e6385d1493efa05b`, source manifest는 `sha256:3c844fcfa5c21c3d661baaa19634329572d6a3d28f900f23f5edbd8793c4ec16`이다. artifact는 `D:\codex\fm-recovery\.flowmarshal-engine-eval\runs\planning-feedback-devgate-20260906`에 있다.
- 상세 수정 성공 합성 경로는 원래 4회 호출에서 멈추던 실패를 6회 호출·2개 후보 version 안에서 새 Plan revision 선택까지 연결했다. Reviewer 반박은 5회 호출 뒤 원래 거절을 유지했다. Skeleton 수정은 8회 호출이며, 두 root가 같은 graph로 수렴하는 회귀는 12회 호출·4개 version에서 중복 상세화를 생략했다. 이 호출 수는 결정적 scripted 역할의 논리 호출이며 실제 모델 성과가 아니다.
- 관측 도구는 READY Goal의 실제 검색·SQLite 등록·History·검토 문서 저장과 BLOCKED Goal을 모두 검증했다. 별도 테스트에서 request의 실제 model binding을 확인하고 계획 중 프로젝트 파일 변경을 탐지했다. 원본 fixture 불변과 활성화 0건도 확인했다.

### 첫 실제 Goal 준비 관측

고정 source `7fce9fbecf207ca08161e54fee51fc4ebb8bc259`, 작업본 `D:\codex\fm-planning-feedback-v1`에서도 위 contract의 696개 포함 Gate 5/5를 통과했다. `S01-single-bugfix`의 기존 자연어 요청과 명시적 역할 설정을 사용했고, 첫 실행은 Goal 정규화·독립 검토 2회 뒤 `GOAL_BLOCKED`로 끝났다. Plan 탐색은 호출하지 않았으며 최초 feasible 시간은 null이다.

실제 지적은 observable outcome의 계획 단계 혼동, Profile의 원본 fixture 보호 조건 누락, 자료 부재에서 발명한 비목표였다. 원본 정규화·Reviewer 결과·`conflict` 계약과 call artifact는 `D:\codex\fm-planning-feedback-v1\.flowmarshal-engine-eval\runs\planning-feedback-s01-20260906`에 보존한다. 두 실제 역할의 보고 합계는 input 57,456, output 3,348, cached input 0, reasoning 2,719 token, receipt latency 50,235ms다. reasoning은 output의 별도 보고 내역이므로 총 token에 다시 더하지 않는다. 구독 한도 차감량으로 환산하지 않는다.

종료 후 저장형 thread 2개의 동일 turn·최종 응답과 원본 receipt·output digest를 재관측했다. 사후 감사 스크립트의 수동 `sys.path` 추가가 distribution을 중복 관측한 첫 실패 보고서는 보존했고, 실제 driver와 동일한 cwd·전용 Python import 형태로 workspace binding PASS를 확인했다. SQLite의 immutable 읽기 전후 원본 artifact manifest도 같았다. 앞선 일반 SQLite 읽기의 원본 전체 manifest 불일치는 변경 전 파일별 목록이 없어 소급 특정할 수 없으며, 전체 무변경을 과장하지 않는다. 근거는 별도 관측 디렉터리의 `postflight-original.json`과 `postflight-followup.json`이다.

### Goal 실패의 한 번 후속 처리

이 실제 원인을 근거로 일반 정규화·Reviewer prompt와 평가 기준은 유지하고 `GoalPreparationRefiner`를 추가했다. 원본 compiler 결과와 입력을 먼저 대조하며, 수정 가능한 충돌만 `revision`·`disputed`·`unresolved`로 응답한다. 변경된 proposal만 독립 재검토하고 같은 Goal ID의 새 revision을 만든다. 동일 후보·반박·정보 부족은 원래 거절을 유지한다.

진단 도구의 `--resume-goal-run`은 완료된 첫 실패의 원시 request/result·역할·실행 파일·프로젝트 입력을 검증하고 DB를 새 실행 경로에 복사한다. 같은 대상 프로젝트·자연어·Goal 계보를 유지하며 원본별 append-only claim으로 중복 후속 호출을 차단한다. 원본 2회를 포함한 총 상한은 14회로 유지하고 후속 Goal 수정·검토에 2회, 이후 Plan 탐색에 10회를 배정한다. 원본 호출을 숨기거나 새로운 Goal의 무관한 성공으로 교체하지 않는다.

후속 구현의 개발 Gate도 전체 706개 테스트를 포함해 5/5 통과했다. contract는 `sha256:9f86aeb29733d24d17677ea8aec68dc3b0d0fc9af2e7202f01e671a361e3b56c`, source manifest는 `sha256:01c0a15067a7f6b313d90ccfc1a9acd1a66244449b79557347fb3f5098d708b3`다. 새 Goal 피드백 8개 회귀와 기존 준비·역할 회귀가 원본 결속·새 revision·반박·무진전·재검토 후 충돌·출력 변조를 검사한다. 실제 원장 구조를 쓰는 후속 driver 회귀는 원본 불변·정확한 계보·Plan 선택·History·활성화 0건과 중복 claim·역할 변조 차단을 확인했다.

고정 source `14e37459f80abdc1f51e3a0d986556b9a8dec319`의 `D:\codex\fm-planning-feedback-v2`에서도 같은 Gate 5/5를 확인하고 실제 후속을 실행했다. `goal_refiner`는 지적된 결과 단계·보호 조건·근거 없는 비목표만 수정하고 Hard AC 두 개를 그대로 보존했다. 독립 Reviewer가 finding 없이 검토했으며 원장에 같은 `goal_c0872d8cdf3f4f7f994b44b9572f7d14`의 revision 1 conflict와 revision 2 active가 함께 남았다.

그다음 첫 Skeleton 생성은 terminal completed였지만 `TaskSkeleton.contributes_to`의 빈 배열 때문에 `schema_failed`로 끝났다. 후속 3회 호출의 실제 보고 합계는 input 91,693, output 3,718, cached input 0, reasoning 2,560 token, receipt latency 69,985ms다. Goal 수정 성공과 Plan pipeline FAIL을 구분한다. 원본 run은 `D:\codex\fm-planning-feedback-v2\.flowmarshal-engine-eval\runs\planning-feedback-s01-goal-repair-20260906`이며 별도 관측 경로 `D:\codex\fm-inspection-observations\planning-feedback-s01-goal-repair-20260906`에 후검증을 보존했다. 성공 2개와 실패 1개의 receipt·저장 thread/turn·terminal을 대조했고, 원본 v1 전체 artifact와 v2 입력·source가 유지됨을 확인했다. Plan·활성화·GoalVerdict는 모두 0건이다.

### READY Goal 이후 Skeleton 계약 정합화

실제 실패를 조사해 `SkeletonTaskDraft`는 빈 `contributes_to`·`produces`를 허용하는 반면 Core `TaskSkeleton`은 각각 최소 한 개를 요구하는 불일치를 확인했다. Draft가 Core TaskSkeleton을 직접 상속하도록 바꾸고 기존 의미 설명을 유지했다. task ref pattern, objective 문자열 길이, 두 배열의 `minItems`와 중복 검사가 같은 정의를 사용한다. 모델 출력 원본이나 AC 의미를 보충하지 않는다.

`--resume-planning-run`은 위 READY Goal과 첫 Skeleton schema 실패를 검증하고, 출력 계약이 실제로 바뀐 경우 한 번 planning부터 이어간다. 완료·귀속 불명, 같은 schema 재호출, 이미 후속 planning을 수행한 원본은 거부한다. 같은 Goal ID·revision·사용자 원문·대상 프로젝트를 유지하며 Goal 정규화와 정제는 다시 호출하지 않는다. 원본 전체 5회 호출을 포함해 남은 planning 예산은 9회다. 모델 없는 실제 artifact loader 검증에서 원본 보존과 동일 READY revision·target·5회/9회 계산을 확인했다.

Skeleton 정합화와 planning 후속 경로의 개발 Gate는 전체 709개 테스트를 포함해 5/5 통과했다. contract는 `sha256:4d5382d2a06df756add5496349fe700f014b5229e59a04cdc76c4ecd35042d12`, source manifest는 `sha256:a9b61118f68ea2bb7744327e2526d1b4caa6392d30e53a3c77c4abb868276619`다. READY Goal 유지·9회 예산·활성화 0건과 원본 무변경, active terminal 및 같은 schema 재호출 차단을 회귀로 확인했다. 원본 Goal 두 호출의 artifact·계보도 재계산한다.

고정 source `31d9e6cd79b0f3d02161d92f115d722e5fcaecb5`의 `D:\codex\fm-planning-feedback-v3`에서도 같은 709개 포함 Gate 5/5를 확인했다. Gate artifact는 `.flowmarshal-engine-eval/runs/planning-feedback-skeleton-frozen-gate-20260906`에 보존한다. 같은 Goal revision 2의 실제 planning은 2026-09-06 13:20:26 KST에 숨김 프로세스로 시작했다. 실행 경로는 `D:\codex\fm-planning-feedback-v3\.flowmarshal-engine-eval\runs\planning-feedback-s01-planning-20260906`, 별도 운영 관측 경로는 `D:\codex\fm-inspection-observations\planning-feedback-s01-planning-20260906`이다. 원본 Goal 2회·Goal 수정 후 3회를 포함한 전체 상한 14회 중 이 실행에는 최대 9회를 배정한다.

### 재개 입력의 추가 감사

v3 실행과 별도로 재사용 가능한 loader의 입력 검사를 보완했다. 성공 Goal 호출도 recovery 0·단일 turn·완료 terminal·원시 출력과 결과의 일치를 요구한다. refiner와 reviewer의 전체 payload는 원본 Goal·Profile·관측·수정 proposal에서 다시 구성해 대조한다. Skeleton 후속은 정확한 직전 provider schema와 원시 출력의 빈 `contributes_to`·`produces` 제약 불일치에 한정하며, 무관한 schema 변경·잘못된 JSON·다른 Core 계약 결함은 거절한다. 검사용 복사본만으로 다른 결함의 부재를 검사하고 원시 출력·후속 후보·평가 결과를 보충하지 않는다.

보완된 driver의 집중 회귀 8개는 숨은 recovery, 요청과 출력의 결속 오류, 완료되지 않은 turn, 무관한 schema 재호출을 검사했다. 같은 새 코드로 실제 v2 원본을 모델 호출 없이 읽어 동일 Goal revision 2·대상 프로젝트·선행 5회/잔여 9회·원본 보존 PASS를 확인했다. 근거는 별도 운영 관측 경로의 `strict-resume-loader-probe/probe-result.json`이며 source manifest는 `sha256:06cd5e185be4a13a77b7103b63dedfa3032a6aebe9d9ccef29a14e4e0c319e3a`다. 이 loader 검증은 새로운 모델 성공이 아니며, 이미 진행한 실제 v3 source `31d9e6c`의 provenance를 바꾸지 않는다.

최종 개발 Gate는 전체 710개 테스트를 포함해 5/5 통과했다. contract는 `sha256:7dd4ce047f055e62a6f50a5f6f20802c28fd3bda7fd64d96c0aa5e7aef51fe91`이며 source manifest는 위 추가 감사와 같다. 전체 테스트는 96.335초였고 compileall·패키지 의존성·합성 lifecycle·legacy freeze도 통과했다. 근거는 `D:\codex\fm-recovery\.flowmarshal-engine-eval\runs\planning-feedback-resume-audit-devgate-20260906`에 있다.

### 실제 planning 후속 결과

v3는 Skeleton 생성·독립 검토·상세 Plan 생성의 세 역할 호출을 완료했다. Skeleton Reviewer finding은 없었고 상세 후보에는 최소 수정 Task와 검증 Task, 별도 독립 Goal Test가 있었다. 네 번째 `compact_plan_reviewer`는 900,078ms 뒤 `timed_out` receipt를 남겨 2026-09-06 13:37:42 KST에 driver가 exit 1·`FAIL`로 종료됐다. Reviewer의 의미 판정을 받지 못했으므로 이 결과를 상세 Plan의 의미 실패나 refinement의 무진전으로 분류하지 않는다.

이번 실행은 4회, 원본 두 실행의 5회를 포함하면 provider turn 9회를 시도했다. schema recovery와 새 Goal 호출은 없었다. Plan 선택·활성화·Worker·독립 Goal Test·GoalVerdict는 미실행이며 최초 feasible 시간은 확인되지 않는다. 실제 Goal 실패 피드백은 같은 revision 계보에서 성공했지만, 실제 상세 Plan 실패 피드백의 수정 성공은 아직 입증하지 못했다. 합성 회귀의 6회 수정 성공과 구분한다.

성공한 세 호출의 provider 보고 소계는 input 104,307, output 9,332, cached input 30,464, reasoning 6,184 token이다. receipt latency 소계는 128,907ms다. timeout receipt의 `usage_available=false`와 0 값은 실제 사용량 0의 근거가 아니므로 전체 실행·전체 계보의 token 총량은 산출하지 않는다. 네 receipt의 관측 latency 합계는 1,028,985ms이며, 이 값은 최초 feasible 시간이나 순수 모델 계산 시간이 아니다. 원본 timeout을 보존하고 저장 thread·원장·입력의 사후 대조를 별도 관측 경로에 남긴다.

종료 후 같은 저장 thread·turn을 재개 없이 읽었을 때 성공 세 역할은 원래 completed 응답과 일치했다. timeout Reviewer는 `interrupted`, 최종 응답 null이었다. 원본 실행에 누락된 terminal을 소급 작성하지 않고 별도 후관측으로 보존했다. `postflight-original.json`·`postflight-followup.json`·`postflight-v2-profile-binding.json`에 v3 workspace/source/target, v1→v2→v3 원본 manifest 연결, 정확한 Goal 요청 결속과 감사 전후 파일 불변을 기록했다. immutable SQLite 조회에서 같은 Goal의 conflict revision 1과 active revision 2, History 6개가 유지됐고 Plan revision·활성화·GoalVerdict는 모두 0개였다. `postflight-usage-worker-supplement.json`은 token 소계와 미확인 전체 사용량을 구분하고 Task 계약·Attempt도 모두 0개임을 직접 확인했다.

이 시점의 다음 선결 조건은 완료된 Plan Reviewer 관측을 확보하는 운영 경로다. 원본 timeout에는 의미 finding이 없으므로 이를 임의의 Plan 수정 입력으로 바꾸거나 Reviewer 판정을 대신 제출하지 않는다. 후속 실험은 원본 실행·예산·terminal 기록을 인계하는 별도 계약이 필요하며, 이 실행의 한 번 후속 claim을 지우거나 같은 source를 재호출하지 않았다. S06 FAIL·전체 qualification·실제 GoalVerdict·1.0 cutover의 미완료 상태는 유지한다.
