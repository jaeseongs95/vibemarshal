# FlowMarshal R3.1 목적 기반 다중 후보 Planner

- 단계: **프로토타입 구현 기준선**
- 선행 계약: [R3 Planner 계약·검증](planner-r3.md)
- 후속 단계: R4 Assigner
- 판정: 정적 계약·fixture 검증과 실제 모델 forward qualification을 모두 통과하기 전에는 `GO`로 선언하지 않는다.

## 1. 목표와 경계

R3.1은 R3의 단일 `PlanDraft` 구조 검증 위에 목적 기반 다중 후보 검색 계층을 추가한다.

```text
ProjectProfileRevision
+ PlanningMission
+ 현재 요청의 명시적 제약
+ 사용자 원문·프로젝트 상태
  → 동일 Mission의 접근 후보 생성
  → Hard Gate
  → 수정 가능한 초기 후보의 제한 정제
  → Soft Score
  → Diversity Top-K
  → 남은 예산의 제한 정제
  → 추천 후보와 대안
  → 사용자 검토
  → 선택된 PlanDraft 하나만 후속 Core로 export
```

R3.1의 고정 경계는 다음과 같다.

- 선택 단위는 WorkItem 조각이 아니라 완전한 계획 그래프다. 서로 다른 후보의 고득점 WorkItem을 임의로 합치지 않는다.
- 목적 선택과 추천안 선택은 실행 승인이 아니다. 정확한 `PlanRevision`을 사용자가 활성화하는 기존 승인 경계를 유지한다.
- 기존 `RequestSpec`, `PlanDraft`, Core DB schema를 변경하지 않는다.
- 후보와 평가는 비권위 planning artifact다. 선택된 `PlanDraft` 하나만 R4 이후 흐름에 전달한다.
- 평가는 성공확률이 아니라 `fitness_score`다. 실행 데이터로 calibration하기 전에는 백분율 성공확률로 표현하지 않는다.
- 검색 서비스와 Planner 스킬에는 Core 활성화·dispatch capability를 제공하지 않는다.
- R3의 단일 후보 `PlannerService.propose()`와 회귀 검사는 그대로 유지한다.

### 1.1 프로토타입 구현 지도

현재 프로토타입은 다음 모듈로 경계를 분리한다.

- `r31_domain.py`: frozen typed sidecar, digest와 교차참조 불변조건
- `r31_intent.py`: 목적 catalog, profile 검사, Mission 해석, 정책 precedence와 요구 추적
- `r31_store.py`: 설정 주입 artifact root, atomic write, OS file lock, profile CAS와 run idempotency
- `r31_search.py`: Hard-before-soft, 고정 가중치, diversity와 deterministic tie-break
- `r31_pipeline.py`: 구조 검사, 독립 review, Top-2 walkthrough, 1회 정제와 상태 전이
- `r31_models.py`, `r31_role_adapters.py`: 호출 직전 runtime inventory 검증과 strict structured model pass
- `r31_runtime.py`: 외부에서 주입된 역할 설정을 inventory와 대조하고 Mission·요구·검색 adapter를 Core 없이 조합
- `r31_session.py`: reuse·isolate·handoff 판단
- `r31_live_smoke.py`: 실제 역할 모델로 Mission부터 단일 PlanDraft export까지 관통하는 부분 smoke
- `r31_evaluation.py`, `r31_eval_runner.py`, `r31_eval_campaign.py`, `r31_eval_cli.py`: 숨은 oracle fixture, 실제 역할 실행, 순서 변형, cell checkpoint와 반복 평가
- `r31_smoke.py`: 모델을 호출하지 않는 결정적 end-to-end 기준선

결정적 smoke가 통과해도 실제 모델 forward qualification을 대신하지 않는다.

## 2. 목적 기반 입력

장기 프로젝트 성격과 요청별 목적을 분리한다.

```text
ProjectProfileRevision       프로젝트의 장기 기본값
PlanningMission              이번 요청의 관찰 가능한 목적
ExplicitRequestConstraint    현재 사용자의 명시적 요구
EffectivePlanningPolicy      이번 PlanningRun에 실제 적용할 정책
```

적용 우선순위는 다음과 같다.

```text
현재 요청의 명시적 제약
> 사용자가 직접 선택한 PlanningMission
> ProjectProfile 기본값
> 모델이 추론한 추천
```

하위 입력이 상위 입력과 다르면 상위 입력을 적용하고 override receipt를 남긴다. 현재 요청과 사용자가 직접 선택한 Mission이 서로 모순되면 임의로 보정하지 않고 `blocked`로 판정한다.

`explicit_request_constraints`는 검토된 요구 추출 중 `kind=constraint`인 항목만 담는다. 사용자가 명시한 제외 요구는 `out_of_scope`와 `forbidden_scopes`에 보존하며 constraint 집합에 중복시키지 않는다. 양쪽을 같은 목록으로 강제하면 승인된 제외 범위를 오히려 정책 불일치로 잘못 차단하게 된다.

### 2.1 ProjectProfileRevision

`ProjectProfileRevision`은 다음 장기 기본값을 immutable snapshot으로 보존한다.

- `product_goal`
- `lifecycle_stage`: `prototype | growth | mature | legacy`
- `criticality`: `low | standard | high`
- `compatibility_policy`: `flexible | preserve | strict`
- `default_risk_tolerance`: `conservative | balanced | exploratory`
- `architecture`, `validation`, `runtime`, `risk`, `compatibility`별 `source_refs[]`, `source_digest?`, `freshness`, `unknown`, `unknown_reasons[]`
- `profile_revision_id`, `project_id`, `definition`, `definition_digest`, `status`, 선택적 `supersedes_profile_revision_id`, `created_at`

비밀정보와 특정 요청에서만 유효한 판단은 profile에 저장하지 않는다. 변경은 기존 revision 수정이 아니라 새 revision 생성과 compare-and-swap 활성화로 처리한다.

### 2.2 PlanningMission

`PlanningMission`은 PlanningRun마다 고정되는 불변 snapshot이다.

- `primary`: 아래 여섯 목적 중 하나
- `secondary`: 선택 사항, 최대 하나
- `observable_outcome`
- `mutation_policy`: `read_only | minimal_change | scoped_change | structural_change | migration_change`
- `behavior_preservation`: `preserve_observed_behavior | preserve_public_contracts | allow_explicit_breaking_changes | not_applicable`
- `allowed_external_effects[]`, `forbidden_scopes[]`, `risk_tags[]`
- `selected_by`: `user | inferred`
- `confidence`: `high | medium | low`
- mission policy ID·version

| primary | 기본 계획 전략 |
|---|---|
| `new_build` | 사용 가능한 최소 vertical slice, 안정된 boundary, 외부 연동 단계화 |
| `feature_extension` | additive 변경, adapter·extension point, 기존 동작 회귀 검증 |
| `legacy_refactor` | characterization test, seam·adapter, 점진적 교체 |
| `bugfix_stabilization` | 재현과 원인 규명, causal minimal fix, 재발 방지 |
| `migration_modernization` | expand–migrate–contract, dual-run, checkpoint·reconciliation |
| `analysis_audit` | 근거 수집, 재현 가능한 분석, 불확실성 명시, 제품 상태 변경 금지 |

명확한 요청은 목적을 추천해 기본 선택한다. 결과·범위·호환성·외부 효과가 달라지는 해석이 둘 이상일 때만 2~3개 선택지를 요청한다. 구현 방식만 모호하면 안전한 가정을 receipt에 기록하고 후보 생성을 막지 않는다.

목적을 바꾸면 기존 후보를 재채점하지 않고 새 `PlanningRun`을 만든다. 서로 다른 Mission의 후보는 같은 순위표에서 비교하지 않는다.

### 2.3 목적과 위험의 분리

Mission은 달성할 결과를 나타내며, 별도 `risk_tags`가 적용할 Gate와 reviewer 강도를 정한다.

```text
existing_behavior
public_contract_change
persistent_state_change
destructive_effect
external_effect
shared_concurrency
security_sensitive
scale_or_slo
human_checkpoint
```

예를 들어 DB를 변경하는 신규 기능은 `feature_extension + persistent_state_change`이며 자동으로 migration Mission이 되지 않는다.

`analysis_audit`는 반드시 `read_only`다. 분석이 독립 산출물이거나 분석 결과가 구현 범위를 바꾸는 경우에는 분석과 구현을 서로 다른 PlanningRun으로 분리한다.

```text
Analysis PlanningRun
  → 검증된 AnalysisArtifact
  → 새 Implementation PlanningRun
```

관계는 `source_run_ids`와 `input_artifact_refs`로 표현한다. `PlanRevision.parent_revision_id`나 후보 lineage는 이 용도로 재사용하지 않는다.

## 3. Planning sidecar 계약

R3.1은 기존 R3 schema를 감싸는 typed sidecar를 둔다.

- `ProjectProfileDefinition`, `ProjectProfileRevision`
- `PlanningMissionDefinition`, `MissionSelectionReceipt`, `MissionReviewEvidence`
- `ExplicitRequestConstraint`, `EffectivePlanningPolicy`
- `RequirementAnalysisContext`, `RequirementExtractionDraft`, `RequirementIntentReview`
- `RequirementExtractionReceipt`, `RequirementReviewEvidence`, `ProjectStateSnapshot`
- `PlanningRunInput`, `PlanningRunReceipt`
- `ApproachBrief`, `CandidateEnvelope`, `CandidateReviewResult`
- `PlanContractSidecar`
  - plan-level outcome와 integration validation
  - criterion-validation binding
  - `produces/consumes`와 dependency 계약
  - 실패·재실행·rollback 계약
- `GateFinding`, `PlanQualityReport`
- `SelectionReceipt`, `SessionHint`, `ModelCallReceipt`

외부 경계는 다음 API로 고정한다.

```text
ProjectProfileStore
  get_active(project_id)
  create_revision(project_id, expected_active_digest, definition)
  activate_revision(profile_revision_id, expected_digest)

PlanningMissionResolver
  resolve(request_spec, profile_revision, user_selection?)
    -> resolved | needs_user_input | blocked

MissionPlanningService
  resolve(request_spec, profile_revision, user_selection?)
    -> ReviewedMissionResolution

RequirementAnalyzer
  context(raw_request, mission_selection, profile_revision, profile_sections, project_snapshot)
  analyze(context, extraction_draft)
  prepare_assembly(context, extraction_draft, assembly_context)
  assemble(context, extraction_draft, assembly_context, intent_review)

RequirementPlanningService
  analyze_and_assemble(context, assembly_context)
    -> ReviewedRequirementAnalysis

PlanningRunService
  freeze(input, idempotency_key)
  supersede(run_id, reason)

PlanningSearchService
  search(frozen_run_input) -> PlanningSearchOutcome

SelectedPlanExporter
  export(outcome, expected_selection_receipt_digest)
    -> PlanDraft + receipt reference
```

Resolver와 검색 서비스는 Core 원장, 계획 활성화 또는 실행 인터페이스에 의존하지 않는다.

`MissionPlanningService`는 `Mission 제안 → 독립 intent review → 명시 요청 우선의 결정적 resolve` 순서를 강제한다. 생성기와 reviewer의 성공 receipt, typed output digest 및 서로 다른 thread를 검증하지 못하면 fail-closed한다. 그 결과는 proposal·reviewed proposal·source `MissionSelectionReceipt`와 두 model call을 묶은 `MissionReviewEvidence`를 포함한다. 요구 조립으로 최종 `RequestSpec` digest만 바뀐 경우에는 raw request·profile·Mission·option·warning·override가 모두 같은지 확인한 뒤 source selection을 새 RequestSpec에 재결속한다.

`RequirementAnalyzer`는 원문·확정 `MissionSelectionReceipt`·필요한 profile section·그 Mission resolution에 결속된 프로젝트 snapshot을 불변 `RequirementAnalysisContext`로 만든다. Mission 선택 당시 option·warning·override receipt도 이 context에 보존한다. 모델이 낸 `RequirementExtractionDraft`의 span·source·digest를 결정적으로 검증한 뒤 기존 R3 `RequestSpecAssembler`가 소비할 `RequestSpecAssemblyInput`을 만든다. 제품 경계인 `RequirementPlanningService`는 `요구 추출 → 결정적 검사 → 독립 intent review → RequestSpec 조립` 순서와 생성기·reviewer의 독립 thread를 강제한다. 조립 직후에는 다시 읽힌 context 파일의 digest를 review 당시 `ProjectStateSnapshot`과 비교하므로 변경이 있으면 새 snapshot과 review를 요구한다. 최종 `RequirementExtractionReceipt`의 내용은 원래 draft digest로 재구성 가능해야 한다. `PlanningRunInput`은 별도의 `RequirementReviewEvidence`에서 PASS review, extraction/review typed output digest, 성공 receipt와 서로 다른 thread를 receipt 본문에 다시 대조하므로 호출자가 만든 PASS 문자열이나 임의 digest만으로 freeze할 수 없다.

`EffectivePlanningPolicy`의 lifecycle·criticality는 R3.1에서 profile 값과 같아야 한다. compatibility·risk tolerance가 달라지면 profile 값부터 최종 값까지 끊김 없는 override receipt와 `user_mission → explicit_request` 권위 순서를 검증한다. 검토된 extraction에서 raw trace를 가진 모든 `CONSTRAINT`는 policy의 `explicit_request_constraints`와 `(statement, source_ref)` 집합이 양방향으로 정확히 같아야 한다. `EXCLUSION`은 이 비교에 넣지 않고 `RequestSpec.out_of_scope`와 Mission·policy의 `forbidden_scopes` 결속으로 검증한다.

### 3.1 주요 후보 artifact의 실제 shape

```text
ApproachBrief
  approach_id, mission_primary, strategy_family, change_shape,
  compatibility, rollout_recovery, rationale, tradeoffs[], risk_tags[]

CandidateEnvelope
  candidate_id, parent_candidate_id?, version, refinement_round, status,
  observation_status, planning_input_digest, mission_resolution_digest,
  mission_primary, approach, plan, contract, quality_report?,
  policy_id, policy_version

PlanContractSidecar
  plan_digest, plan_outcome, integration_validations[], criterion_bindings[],
  dependency_contracts[], failure_recovery_contracts[]

CriterionValidationBinding
  work_item_ref, criterion_id, check_type, capability_id?,
  specification_digest, required_evidence[]

ModelCallReceipt
  call_id, role, model_id, reasoning_effort, inventory_digest,
  input_digest, output_schema_digest, output_digest?, status,
  schema_recovery_attempts, thread_id?, turn_ids[], token_count?,
  latency_ms?, usage[], error_summary?

MissionReviewEvidence
  proposal, reviewed_proposal, resolved_selection,
  proposer_receipt, reviewer_receipt

RequirementReviewEvidence
  intent_review, extractor_receipt, reviewer_receipt
```

`DependencyContract`는 `producer_work_item_ref`, `consumer_work_item_ref`, `produces[]`, `consumes[]`, `compatibility_contract`를 가진다. sidecar의 dependency edge 집합은 `PlanDraft` DAG와 정확히 같아야 하며 모든 `consumes`는 해당 계약의 `produces`에 포함되어야 한다.

`GateFinding`은 `gate`, `plan_verdict`, `runtime_status`, `summary`, `diagnostics[]`, 선택적 `not_applicable_rationale`를 가진다. 세부 진단은 `GateDiagnostic.finding_code`, `severity`, `message`, `evidence_refs[]`, `work_item_refs[]`, `remediable`로 표현한다.

`SessionHint`는 `role`, `strategy: reuse | isolate | handoff`, 선택적 `candidate_id`·`reusable_prefix_digest`, `artifact_refs[]`, `independent_review_session`, 항상 false인 `hidden_context_required`, `rationale`를 가진다. 병렬 가능성은 `isolate` 근거일 수 있지만 별도 session strategy 값은 아니다.

### 3.2 저장·동시성·digest

- R3.1 artifact는 설정으로 주입된 artifact root 아래 canonical JSON으로 저장한다. 개인 경로나 인증정보를 코드에 하드코딩하지 않는다.
- revision과 receipt는 immutable이다. 저장은 atomic write를 사용한다.
- profile 활성화는 `expected_active_digest` 기반 compare-and-swap으로 동시 갱신 충돌을 탐지한다.
- 동일 idempotency key와 동일 입력의 `freeze`는 기존 PlanningRun을 반환한다. 같은 key에 다른 semantic 입력이 오면 충돌로 거부한다.
- semantic 입력이 달라지면 후보와 평가를 재사용하지 않고 새 run을 만든다.
- model call receipt는 후속 파싱·검색 완료를 기다리지 않고 input digest 기반 journal에 즉시 저장한다. freeze 전 Mission·요구 분석 호출은 frozen review evidence에도 상호 결속하고, freeze 후 검색 호출은 즉시 journal한 뒤 해당 run에도 결속한다. 모델 resolution 실패 receipt는 별도 preflight 영역에 보존한다. structured adapter는 반환 receipt의 role·model·effort·inventory·input·output schema digest가 실제 요청과 정확히 같은지 확인한 뒤에만 typed output digest를 기록한다.

`planning_input_digest`는 다음을 결속한다.

```text
RequestSpec digest
+ ProjectProfile definition digest
+ Mission resolution digest
+ EffectivePlanningPolicy digest
+ RequirementExtractionReceipt semantic digest
+ ProjectStateSnapshot semantic digest
+ 선행 AnalysisArtifact digest
```

run ID, artifact ID, timestamp, token·latency 같은 telemetry는 semantic digest에서 제외한다. 반대로 요구 문장·가정·미확정 항목, snapshot의 경로·내용 digest·unknown은 후보 prompt를 바꾸므로 반드시 포함한다. 후보 생성·Hard Gate prompt도 이 semantic view만 사용해, 제외한 provenance나 telemetry가 같은 digest의 모델 입력을 바꾸지 않게 한다.

### 3.3 상태

PlanningRun 상태는 다음으로 제한한다.

```text
draft → blocked | frozen → searching → ready_for_review | failed | superseded
```

후보 상태는 다음을 사용한다.

```text
generated | needs_revision | blocked | rejected | admissible | selected
```

R3의 `READY_FOR_ASSIGNMENT`는 R3.1 안에서 `structurally_valid`로만 해석한다. 의미 검토까지 끝난 검색 결과는 `ready_for_review`다. 실행 가능한 WorkItem의 `ready`는 사용자가 계획을 활성화한 뒤 Core만 계산한다.

## 4. 계획 품질 Gate

Gate는 점수보다 먼저 적용하며, 치명적 결함은 다른 장점으로 상쇄할 수 없다.

| Gate | 판정 질문 |
|---|---|
| Intent Gate | 사용자 원문, Mission, 요구사항과 최종 관찰 결과가 양방향 추적되는가 |
| Plan Gate | 모순·cycle·끊긴 dependency·후속 재작업·부적절한 분할이 없는가 |
| Engineering Gate | 보안·설정·회귀·데이터·동시성·외부 가정·성능을 처리했는가 |
| Verification Gate | 객관적 acceptance와 실행 가능한 validation·통합 검사가 있는가 |
| Execution Gate | 실패 복구·재실행·부분 실행·중복 dispatch·사람 checkpoint·세션 전략이 있는가 |

기존 22개 진단 항목은 다음과 같이 Gate에 결속한다.

| Gate | 진단 항목 |
|---|---|
| Intent | 전체 작업 후 사용자 의도 충족, 논리적 일관성, 독립 세션이 같은 구현 의도를 복원할 수 있는 명세 |
| Plan | 후속 작업의 대규모 재작업 방지, 선행조건·dependency·cycle, 기능 응집적 작업 크기, 범위 팽창 방지, 다음 작업이 소비할 계약 |
| Engineering | 유지보수성·확장성·가독성, 보안, 설정 비하드코딩, 회귀, 데이터 무결성, 동시성, 외부 가정, 성능·자원 |
| Verification | 추측 없는 구체성, 객관적 완료조건, 작업별 테스트·빌드·정적 분석·실동작·DB 검사 |
| Execution | 실패 시 안전 상태·복구, idempotency와 재실행 조건, 사람 승인·운영 checkpoint, 세션 reuse·isolate·handoff |

계획 판정과 실제 실행 상태는 분리한다.

```text
plan_verdict: pass | fail | blocked | not_applicable
runtime_status: not_run | pass | fail | error
```

계획 단계에서는 테스트가 성공했다고 주장하지 않고 실행 가능한 검증이 정의됐는지만 판단한다. `not_applicable`에는 근거가 필요하다.

- 수정 가능한 결함: `needs_revision`
- 사용자 결정·권한·외부 사실 필요: `blocked`
- 명시 요구 위반·실현 불가능·정제 예산 소진: `rejected`
- 모든 Hard Gate 통과: `admissible`

Hard Gate를 모두 통과하지 않은 후보에는 점수를 부여하지 않으며 Top-K에도 넣지 않는다.

## 5. 제한형 후보 검색

R3.1은 재귀 Beam Search 대신 최대 5개 candidate version 안에서 초기 수정과 Top-K 후 개선을 함께 다루는 제한형 검색으로 시작한다.

```text
ApproachBrief 생성
  → signature 중복 제거
  → 후보별 PlanDraft 확장
  → deterministic 구조·계약 검사
  → 독립 semantic Hard Gate
  → 수정 가능한 needs_revision 후보를 남은 예산에서 1회 정제
  → 정제 자식의 deterministic·semantic Gate 전체 재실행
  → admissible 후보만 점수화
  → Diversity Top-2
  → dependency·migration 의미 walkthrough
  → 아직 정제하지 않은 Top-K 후보를 남은 예산에서 최대 1회 정제
  → 전체 Gate 재실행
  → 추천 후보 선택
```

탐색 예산과 예외는 다음과 같다.

- 의미 있는 설계 분기가 없으면 후보 1개와 반례 검토 1회만 수행한다.
- 일반 요청은 초기 후보 최대 3개, Top-K는 최대 2개다.
- `analysis_audit`는 기본 1개이며 분석 방법이 실질적으로 다를 때만 최대 2개다.
- 원인 미확정 `bugfix_stabilization`은 수정안 여러 개가 아니라 진단 WorkItem 계획 1개를 만든다.
- 후보별 정제는 최대 1회, 전체 candidate version은 최대 5개다.
- 초기 `needs_revision`은 결함이 수정 가능하고 `blocked`·`rejected`가 아닐 때만 Top-K 전에 정제할 수 있다. 정제 예산은 Top-K 후 walkthrough 정제와 같은 전체 version 예산을 공유한다.
- 자식 후보는 부모의 점수와 판정을 상속하지 않고 deterministic 구조 검사부터 semantic Hard Gate까지 다시 평가한다. 다른 Mission·score policy·planning input, 부모와 다른 approach ID/signature, 부모 risk tag 삭제 또는 중복 candidate ID를 반환한 정제 branch는 해당 branch에만 격리한다. 자식이 미통과해도 기존 admissible 부모 fallback은 제거하지 않으며, 둘 다 admissible이면 diversity dedupe가 재평가 근거로 더 나은 version을 남긴다.
- 한 후보의 실패는 다른 후보 검색을 중단시키지 않는다. admissible 후보가 하나도 없을 때만 run을 `failed` 또는 `blocked`로 전이한다.

후보 차이는 `strategy_family`, `change_shape`, `compatibility`, `rollout_recovery`로 구조화한다. 이름만 다르고 DAG·계약·산출물이 같으면 같은 signature로 간주한다.

## 6. Soft Score와 선택

R3.1은 모든 Mission에 `balanced-mvp-v0` 가중치 하나만 적용한다.

| 차원 | 가중치 |
|---|---:|
| 목표 적합성·변경 안전성 | 25 |
| 검증·근거 강도 | 25 |
| 실행 위험 통제 | 20 |
| 유지보수성·재현 가능성 | 20 |
| 시간·token·API·자원 효율 | 10 |

각 차원을 0~4로 평가하고 다음 식으로 0~100 정수 점수를 계산한다.

```text
numerator = Σ(weight × rating)
fitness_score = floor((numerator + 2) / 4)
```

평점과 가중치는 음수가 아니고 분모가 4이므로 이 식은 정확한 half-up 반올림이다. 언어별 `round()`의 ties-to-even 동작을 사용하지 않는다. 각 rating에는 artifact 기반 evidence가 있어야 한다. 보안·데이터 안전·명시 SLO는 점수 차원이 아니라 Hard Gate다.

WorkItem 품질은 `self_containment`, `functional_cohesion`, `acceptance_validation`, `interface_clarity`, `failure_retry` 다섯 축을 각각 0~2로 평가한다. 하나라도 0이면 해당 후보는 `needs_revision`이다. WorkItem 평균을 계획 점수에 합성하지 않고 `weakest_work_item_rating`과 동률일 때 정렬상 첫 `weakest_work_item_ref`를 `PlanQualityReport`에 보존한다.

선택 규칙은 다음과 같다.

- 1위가 2위보다 10점 이상 높고 confidence가 `medium` 이상이면 1위를 추천한다.
- 차이가 10점 미만이면 동점으로 취급한다.
- Mission별 우선 기준을 적용한 뒤 공통 tie-break를 적용한다.
- 공통 tie-break 순서는 최약 WorkItem, 가역성, public contract 변경 최소, 변경 표면, 비용이다.
- 미확인 가정이 Gate나 승자를 바꿀 수 있으면 confidence는 `low`이며 자동 기본 선택하지 않는다.
- 사용자가 대안을 고르면 새 immutable `SelectionReceipt`에 `selection_source=user_override`를 기록한다.

`selection_source`는 `recommended_default | user_override | none_low_confidence | none_no_admissible` 네 값이다. `none_low_confidence`와 `none_no_admissible`에는 선택 후보를 두지 않는다. 기존 receipt의 선택을 바꾸는 `user_override`는 `supersedes_selection_digest`로 이전 immutable receipt를 연결하고, 최초 검색 요청부터 사용자가 후보를 지정했다면 선행 digest는 없다.

Mission별 우선 기준은 다음과 같다.

| Mission | 우선 기준 |
|---|---|
| 신규 생성 | 사용 가능한 vertical slice, 불가역 기반 결정 최소화 |
| 기능 확장 | 기존 동작과 소비자 호환 |
| 리팩터링 | 동작 동등성, 점진성, blast radius |
| 버그 수정 | 원인 근거, 재현, causal change |
| migration | 데이터 보존, 복구, 중간 호환, 재실행 |
| 분석 | evidence coverage, 재현성, source 권위 |

## 7. 스킬·모델·세션 전략

Planner 지침은 `flowmarshal-work-planner` 단일 스킬 패키지로 유지한다. `SKILL.md`에는 router와 공통 불변조건만 두고 다음 참조를 역할별로 필요한 만큼 읽는다.

```text
intent-contract.md       ProjectProfile·Mission·요구 추출
plan-contract.md         PlanDraft·WorkItem 작성
quality-gates.md         독립 Hard Gate
candidate-selection.md   점수·다양성·Top-K
session-strategy.md      reuse·isolate·handoff
evaluation.md            fixture와 합격 기준
```

모델 pass는 역할별로 분리한다.

```text
Mission sub-pass:
  purpose_resolver 제안 → intent_reviewer 독립 검토 → Mission 확정
Requirement sub-pass:
  purpose_resolver 추출 → deterministic trace 검사
  → intent_reviewer 독립 검토 → RequestSpec 조립
Candidate sub-pass:
  candidate_generator → deterministic lint
  → hard_gate_reviewer | critical_reviewer
  → scorer_selector → session_advisor
Evaluation:
  eval_runner
```

- risk router는 후보마다 일반 Hard Gate reviewer와 critical reviewer 중 하나를 배타적으로 선택한다. 둘을 순차 실행해 점수를 합산하지 않는다.
- 생성기의 숨은 reasoning, 자기 점수와 후보 이름은 reviewer에게 전달하지 않는다.
- Hard Gate reviewer는 후보 ID·approach ID·생성 rationale 없이 `anonymous_candidate`를 받고 `CandidateReviewResult`만 반환한다. 제품 코드가 그 결과를 원본 후보에 다시 결속한다.
- reviewer는 finding과 WorkItem·계획 차원 rating을 제출한다. runtime은 모든 rating과 evidence를 검사한 뒤 `status`, `quality_report.plan_verdict`, `fitness_score`, `weakest_work_item_ref/rating`을 결정적으로 파생하며, 모델이 임의로 보낸 집계값이나 서로 모순되는 조합을 사용하지 않는다.
- reviewer 입력에서 후보 순서를 무작위화해 위치 편향을 검사한다.
- 각 역할은 필요한 참조와 ProjectProfile section만 받는다.
- ProjectProfile과 확정 Mission은 안정된 공통 prompt prefix로 재사용한다.
- 후보 생성·정제는 같은 Mission 안에서 세션과 cache를 재사용할 수 있다.
- intent reviewer, 일반 Hard Gate reviewer와 critical reviewer는 생성기와 각각 독립된 세션을 사용한다.
- 한 후보의 reviewer timeout·schema 복구 실패는 그 후보를 `blocked`로 격리하고 다른 후보 검색은 계속한다. 필수 critical 역할 자체가 없으면 run 전체를 `blocked`로 한다.
- 모든 세션은 hidden conversation context 없이 artifact만으로 재개할 수 있어야 한다.

세션 배치는 `reuse | isolate | handoff` 중 하나로 결정하고 `SessionHint.rationale`에 이유를 기록한다.

- 앞 작업의 코드·설계 문맥이 다음 작업에서 크게 재사용되면 같은 세션을 유지한다.
- 독립 subsystem이거나 서로 병렬 수행할 수 있고 이전 reasoning이 거의 필요 없으면 `isolate`한다. 이 힌트 자체가 병렬 dispatch 승인은 아니다.
- 한 작업의 실패나 가정이 다른 판단을 오염시킬 가능성이 크면 reviewer 또는 작업 세션을 격리한다.
- 대규모 설계 변경으로 현재 reasoning 전제가 무효화되면 artifact를 고정해 `handoff`한다. Mission·semantic input 자체가 바뀌었다면 handoff만으로 기존 후보를 재사용하지 않고 새 PlanningRun으로 재계획한다.
- 긴 세션의 compaction·context loss 위험이 커지면 현재 artifact digest와 미결 결정을 포함한 handoff를 만든 뒤 분리한다.

공식 모델 안내를 초기 역할 가설로만 사용한다. Luna는 비용 민감·대량 후보 생성, Terra는 일반 semantic review, Sol은 보안·migration·데이터·동시성·외부 효과·public contract처럼 실패 비용이 큰 검토에 배정한다. 실제 모델 ID와 지원 reasoning effort는 runtime 조립 시 `model/list`로 해석하고, 각 model call 직전 inventory를 다시 읽어 같은 `inventory_digest`·model·effort인지 확인한다. SDK가 pagination cursor를 처리할 수 없는 상태에서 `nextCursor`가 오면 부분 목록을 전체 inventory로 오인하지 않고 차단한다. schema recovery turn 직전에도 inventory를 다시 확인한다. 변경되었거나 사용할 수 없으면 receipt를 남기고 fail-open하지 않는다. `migration_modernization` Mission이거나 Mission 또는 approach에 public contract·persistent state·destructive/external effect·shared concurrency·security risk가 있거나 Mission confidence가 `low`면 일반 reviewer 대신 `critical_reviewer`로 routing한다. 필수 critical reviewer를 사용할 수 없으면 조용히 일반 reviewer로 fallback하지 않고 run을 `blocked`로 만든다.

`build_planning_runtime`은 모든 필수 역할의 `ModelRolePreference`를 호출자가 주입하도록 강제한다. 제품 코드에는 실제 모델 ID 기본표를 두지 않으며, 중복 역할·누락 역할·예상하지 않은 역할 설정을 거부한다. 아래 조합은 forward qualification에서 비교할 **평가 가설**이지 Core 또는 runtime의 고정 기본값이 아니다.

- Luna `medium`: profile 정규화, Mission 추천 문구, raw request 요구 추출, ApproachBrief와 후보 확장
- deterministic code: schema·digest·DAG·참조·점수·tie-break
- Terra `high`: intent review, 일반 semantic Hard Gate, Top-2 walkthrough
- Sol `high/xhigh`: `critical_reviewer`의 고위험 Gate와 low-confidence 검토

참고: [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [GPT-5.6 모델 선택 안내](https://developers.openai.com/api/docs/guides/latest-model)

이 프로젝트에서 역할별 로컬 Codex thread와 turn은 실제 유효 권한 `danger-full-access`, 승인 정책 `never`로 시작한다. `analysis_audit`의 `read_only`는 계획의 mutation 계약이지 Codex sandbox 프로필이 아니다. Planner가 계획을 활성화하거나 Core 상태를 바꾸지 못하는 경계는 축소 sandbox가 아니라 Core capability 미제공, 제한된 typed input, strict structured output과 receipt 검증으로 강제한다.

## 8. 구현 단계와 Definition of Done

1. **계약과 기준선 고정**
   - R3 단일 후보 API와 테스트를 회귀 기준으로 고정한다.
   - 목적 해석·계획 품질 suite의 숨은 oracle을 prompt 작성 전에 확정한다.
   - 완료조건: R3 계약 변경 없이 R3.1 artifact schema, 상태와 capability 경계가 문서·타입으로 검증된다.
2. **ProjectProfile·Mission sidecar**
   - immutable profile revision, Mission resolver, precedence, freshness와 digest를 구현한다.
   - 완료조건: profile 변경이 frozen run을 바꾸지 않고 Mission 변경은 새 planning input digest를 만든다. CAS, atomic write와 idempotency 충돌 검사가 통과한다.
3. **요구사항과 프로젝트 상태 근거화**
   - 요구·제약·제외·관찰 결과의 양방향 trace와 관련 프로젝트 snapshot을 만든다.
   - 완료조건: 누락하거나 발명한 요구가 단순 ID coverage만으로 통과할 수 없다.
4. **결정적 후보 검색 엔진**
   - typed candidate, Gate finding, scoring, diversity, dependency contract lint와 receipt를 구현한다.
   - 완료조건: Hard Gate 실패 후보의 점수화와 Top-K 진입이 타입 또는 validator 경계에서 거부된다.
5. **스킬과 모델 adapter**
   - 역할별 progressive disclosure와 structured output adapter를 연결한다.
   - `build_planning_runtime`이 Mission·요구·후보 생성, 일반/critical review, walkthrough와 session advisor를 실제 adapter로 조립하되 Core capability는 받지 않는다.
   - 완료조건: 각 pass가 선언된 입력만 사용하고 모든 model call receipt가 남는다. receipt는 호출 시점 inventory·입력·출력 schema digest를 결속하며 schema 오류는 최대 1회 복구하고 reviewer 오류·timeout은 fail-open하지 않는다.
6. **제한형 검색과 추천 UX 계약**
   - `1 또는 3개 → Top-2 → 최대 1회 정제 → 추천`을 연결한다.
   - 완료조건: 선택된 후보 하나만 export할 수 있고, 목적 변경·사용자 override·all-rejected·partial model failure가 명시 상태로 귀결된다.
7. **독립 forward test와 R3.1 판정**
   - 모델 역할 조합을 고정 fixture와 후보 순서 변형으로 반복 평가한다.
   - 완료조건: 독립 세션이 선택된 artifact만으로 목표·작업 순서·계약·실패 처리·완료 판정을 복원하고 아래 합격선을 모두 통과한다.

## 9. 평가 하네스와 합격선

평가 catalog와 모델 입력의 경계는 다음과 같다.

- fixture 내부의 숨은 oracle은 모델 호출 전에 고정하지만 모델 입력에는 넣지 않는다.
- 모델에는 내부 case ID, variant 이름과 `clean`·`adversarial`·`mutant` 같은 정답 암시 표시 대신 hash 기반 opaque `case_ref`만 전달한다.
- 목적 fixture에는 case별 실제 profile·요청·source clause·integrity binding을, 계획 fixture에는 구체적인 WorkItem·dependency·validation·recovery 후보 artifact를 전달한다. 결함 이름만 적은 추상 label은 평가 입력으로 쓰지 않는다.
- 순서 seed는 fixture 나열뿐 아니라 후보 배열의 순서도 바꾼다.
- oracle의 `required_major_defects`는 반드시 탐지할 핵심 결함으로 recall 분모가 된다. `allowed_major_defects`는 핵심 결함과 함께 보고해도 precision 오탐으로 세지 않을 상관 결함이며 recall을 부풀리지 않는다.
- schema 첫 출력 준수율, token, latency, 실제 role·model·effort, inventory와 권한은 모델 자기보고가 아니라 model-call·Runner receipt에서 계산한다.
- 모델이 oracle에 없는 clause ID나 defect code를 보고하면 결정적 evaluator가 발명 또는 오탐으로 계산한다.
- 계획 fixture의 독립 복원은 선택 후보의 모든 `work_item_ref`, `dependency_ref->consumer_work_item_ref` edge, failure policy와 validation을 가진 WorkItem ref를 각각 비교한다. validation 참조에 `criterion_id`를 대신 넣는 식의 표현 차이는 통과로 추정하지 않는다.

전체 catalog 실행은 50개 fixture × 3 seeds의 150개 cell로 나눠 저장한다. 각 cell은 observation, 원시 structured assessment, model-call receipt와 권한 증거를 immutable artifact로 남긴다. campaign manifest는 fixture catalog와 prompt·output schema의 평가 계약 digest를 고정하고 첫 cell의 역할별 model·effort·inventory를 model lock으로 고정한다. 계약이나 model lock이 달라지면 기존 cell과 합치지 않는다. 사용량 한도에 걸린 시도는 별도 이력으로 보존하되 완료 cell로 세지 않으며, `--resume`은 검증된 완료 cell만 건너뛴다.

### 9.1 목적 해석 suite

13개 family 각각에 clean/adversarial variant를 둔다.

- 명확한 목적과 불필요한 질문 방지
- 최종 결과가 달라지는 모호성
- 내부 구현법만 모호한 경우
- 현재 요청과 ProjectProfile 충돌
- stale 또는 누락 profile
- 같은 프로젝트의 연속된 다른 Mission
- prototype과 production profile 차이
- profile source prompt injection
- Mission digest 변조
- 같은 Mission의 최소 변경·구조 개선 후보
- 분석과 구현의 잘못된 혼합
- prototype 목적을 이용한 보안·데이터 Gate 약화

합격선은 명시 필수 요구·제외 범위 추출 100%, 발명한 필수 요구 0건, 결과를 바꾸는 모호성 질문률 100%, 명확한 요청의 불필요한 blocking 질문 0건이다. 미확정 Mission의 후보 생성·점수화, 서로 다른 Mission의 혼합 ranking, 이전 Mission 전용 문맥 유입은 모두 0건이어야 하며 precedence override와 stale/digest mismatch는 100% 기록·탐지해야 한다.

### 9.2 계획 품질 suite

기존 12개 family의 clean/mutant를 유지한다.

- 기능적 vertical slice
- producer/consumer dependency
- DB migration
- 중복 dispatch
- 외부 API·secret
- 대규모 scan·자원 사용
- 회귀 검증
- 사용자 판단이 필요한 모호성
- 저위험 작업의 과잉 Gate
- soft 고득점에 숨은 보안 결함
- 이름만 다른 후보
- revision·긴 세션·handoff

합격선은 다음과 같다.

- 기존 R3 회귀 검사 전부 통과
- 구조 결함 탐지 100%
- Hard Gate 실패 후보의 점수화·Top-K 진입 0건
- critical mutant 반복 평가의 false admission 0건
- 주요 의미 결함 recall 90% 이상, precision 85% 이상
- 실현 가능한 clean fixture의 잘못된 block 최대 1/12
- criterion-validation 및 내부 `consumes` 연결 100%, cycle 0건
- 다중 접근 fixture의 80% 이상에서 실질적으로 다른 후보 2개 이상
- 동일 signature cluster가 Top-2에 함께 남는 사례 0건
- 후보 순서 변경 뒤 critical verdict 일치율 100%
- 독립 세션의 목표·요구·제외·완료조건 복원율 100%
- 실행 evidence 없이 validation 성공을 주장한 사례 0건

전체 catalog는 목적 해석 26개와 계획 품질 24개, 합계 50개 fixture다. 순서 seed 1·2·3 입력은 같은 oracle에 결속하되 모델에는 oracle을 전달하지 않는다. 실제 역할 fixture probe에서 Mission 항목은 목적 제안과 독립 검토의 두 호출로, 계획 항목은 위험도에 따라 일반 또는 critical reviewer 한 호출로 평가한다.

Luna 생성 역할은 첫 출력 schema 준수율 90% 이상, 다중 접근 다양성 80% 이상, 전체 pipeline의 critical false admission 0건을 충족해야 한다. 미달하면 Luna를 기본 생성기로 확정하지 않고 Terra 생성 기준선과 품질·token·latency·비용을 비교 보고한다.

## 10. GO 판정과 유보 범위

schema·digest·DAG·점수·선택 같은 결정적 검사, 단일 실제 모델 smoke 또는 일부 fixture 역할 probe의 통과만으로 R3.1 `GO`를 선언하지 않는다. 실제 후보 생성과 독립 reviewer 조합을 전체 fixture·순서 변형·독립 forward test로 실행해 모든 합격선을 충족해야 한다. 일부 역할이나 일부 fixture만 검증됐다면 판정은 `GO 보류`이며 R4 시작 조건을 충족하지 않는다.

R3.1에서 유보하는 범위는 다음과 같다.

- 재귀 Beam Search와 동적 beam width
- Pareto pruning, MMR, embedding similarity
- Mission별 동적 가중치와 자동 reweight
- 성공확률 표기와 자동 calibration
- 후보 artifact의 Core DB 저장과 schema migration
- 사용자 승인 없는 PlanRevision 활성화
- 병렬 Worker dispatch
- 실제 제품 UI

R3.1은 UI가 소비할 목적 선택·추천·override 계약과 CLI/fixture 동작까지만 포함한다. 향후 검색 확장을 위해 candidate lineage, policy/model version, immutable receipt와 미선택 후보의 `not_observed` 상태는 처음부터 보존한다.

프로토타입 CLI는 다음 경계를 제공한다.

```text
flowmarshal-planner-r31-smoke --artifact-root <설정 경로>
flowmarshal-planner-r31-live-smoke --artifact-root <설정 경로> --skill-root <스킬 경로> --role-config <역할 설정> --codex-bin <Codex 실행 파일>
flowmarshal-planner-r31-eval export-inputs --output <모델 입력 JSON> --order-seeds 1,2,3
flowmarshal-planner-r31-eval run-models --artifact-root <설정 경로> --skill-root <스킬 경로> --role-config <역할 설정> --codex-bin <Codex 실행 파일> --case-ids <ID 목록> --order-seeds <seed 목록>
flowmarshal-planner-r31-eval run-models --artifact-root <설정 경로> --skill-root <스킬 경로> --role-config <역할 설정> --codex-bin <Codex 실행 파일> --full-catalog --order-seeds 1,2,3 [--resume] [--max-new-cells N]
flowmarshal-planner-r31-eval evaluate --observations <관측 JSON> --output <평가 보고서 JSON>
```

첫 명령은 Core 활성화 없이 선택된 `PlanDraft` 하나까지 결정적으로 export하고, 두 번째 명령은 같은 경계를 실제 역할 모델로 관통한다. 평가 입력 export는 숨은 oracle을 포함하지 않는다. `run-models`는 선택 fixture와 명시적 전체 catalog를 구분하며 fixture별 실패를 격리하고 receipt를 남긴다. 전체 50개 fixture × seed 3개 역할 probe는 150개 checkpoint cell과 현재 계약상 228회의 실제 모델 호출이 필요하므로 우발 실행을 막기 위해 `--full-catalog`를 명시해야 한다. 중단 후 같은 artifact root를 잇는 경우 `--resume`을 명시하며, `--max-new-cells`는 한 호출의 새 cell 수만 제한한다. observation 파일 평가는 고정 oracle로 반복 결과를 다시 계산한다.

이 문서는 당시 R3.1 계약의 감사 기준선이다. 중간 시점의 [qualification 상태 보고서](../spikes/orchestration/r31/artifacts/runs/r31-qualification-status-20260903.md)와 campaign 2의 `1/150` 표기는 역사적 artifact로 보존하며 현재 상태로 사용하지 않는다. 최종 campaign 10은 150/150 cell과 실제 모델 호출 228회를 완료했지만 역할 품질 Gate를 통과하지 못해 `FAIL`로 동결됐다. 최종 수치·실패 원인·회귀 fixture 이관 내역은 [R3.1 최종 동결 기준선](r31-frozen-baseline.md)을 권위 보고서로 사용한다. 새 제품 설계는 [전면 재설계 권위 문서](orchestration-redesign.md)를 따른다.
