# 유효한 historical PASS ID 호환성: 최소 반례와 required assertions

parent는 기존 검토의 exact-latest-ID 정책과 최소 결함 수정의 구분을 수용했다. 새 frozen candidate를 기다리는 상태이며 아래 내용은 제품 수정·새 candidate 승인·release 승인이나 새 실행 결과가 아니다.

## 최소 상태와 호출

원본 candidate `98bb122970f82bb5fcef122003ee10c7eb787cf4`의 `tests/test_engine_goal_verdict_authority.py::GoalVerdictAuthorityTests.setUp`을 그대로 사용한다. 이 fixture는 active Plan, completed Task, 그 Task의 유효 PASS와 한 integration PASS를 이미 정상 Core API로 준비한다. integration은 deterministic `task_aggregate`이며 새로운 provider/독립 semantic 관측이 필요한 사례가 아니다.

```python
old_pass = self.integration
new_pass = self.record_result(task=False)
self.record_verdict(self.verdict(result_ids=(old_pass.validation_result_id,)))
```

`new_pass`는 `old_pass`와 validation ID·task_id=None·evidence_ids·status=PASS가 같다. result ID와 evaluated_at만 새 값이다. Plan/Goal/State/Map/Worker/evidence가 바뀌지 않으며 기존 evidence를 무효화하는 변경도 없다. verdict는 새 PASS 이후 만들어 제출한다. 최신-ID 정책이 제거된 revision은 이를 수락해야 한다. 동일 검사의 모든 PASS ID를 tuple에 넣거나 제출 ID를 최신 ID로 바꾸어 이 반례를 지우지 않는다.

기존 independent probe의 `successive_pass_old_id`가 정확히 이 호출이며 baseline 수락 / frozen candidate 거부를 이미 관측했다. original raw를 재사용하는 역사 근거이고 테스트를 다시 실행하지 않았다.

## 최소 permanent regression 변경 제안

기존 `test_old_pass_id_cannot_replace_latest_integration_pass_id`의 거부 oracle을 다음 수락 oracle로 교체한다. 아래 코드는 author가 자신의 새 revision에서 구현할 참고 코드이며 reviewer는 제품/test source를 수정하지 않는다.

```python
def test_valid_prior_integration_pass_can_complete_after_successive_pass(self):
    old_pass = self.integration
    new_pass = self.record_result(task=False)
    self.assertNotEqual(old_pass.validation_result_id, new_pass.validation_result_id)
    self.assertEqual(old_pass.validation_id, new_pass.validation_id)
    self.assertEqual(old_pass.evidence_ids, new_pass.evidence_ids)
    self.assertEqual(ValidationStatus.PASS, old_pass.status)
    self.assertEqual(ValidationStatus.PASS, new_pass.status)
    submitted = self.verdict(result_ids=(old_pass.validation_result_id,))
    self.record_verdict(submitted)
    # 아래 required assertions를 검사한다.
```

필수 assertions:

1. SATISFIED 제출이 `EngineServiceError` 없이 수락되고 Plan status=completed, project.active_plan_revision_id=NULL, project.run_state=completed가 된다.
2. 새 goal_verdicts payload의 `integration_validation_result_ids`는 **정확히 제출한 `(old_pass.validation_result_id,)`**다. Core가 이를 new_pass ID로 조용히 치환하지 않는다.
3. 두 validation_results의 기존 row/payload/status=pass가 그대로 남고 evidence_records, attempts, runtime_intents, runtime_receipts는 전/후 전체 행이 같다. Goal 완료로 필요한 Plan/project/goal_verdict/History 변경만 발생한다.
4. 원장 History hash chain이 유효하다.

기존 관련 protection assertions는 유지한다:

- integration PASS→FAIL 또는 PASS→INCONCLUSIVE에서는 old PASS ID를 인용한 SATISFIED를 거부한다. goal_verdicts/history_events뿐 아니라 project active pointer와 Plan status도 전/후 같다. append된 실패/미확정 result와 evidence는 보존한다.
- completed Task 뒤 새 FAIL/NOT_RUN에서도 SATISFIED를 거부하고 active Plan을 보존한다.
- quiescent한 새 Plan 활성화로 이전 Plan이 superseded이면 이전 Plan SATISFIED를 거부하고 replacement active pointer/run_state와 History를 보존한다.
- 기존 current PASS 및 FAIL→새 PASS positive는 계속 성공한다.

이 변경 때문에 더 강한 full latest-ID set equality/모든 integration ID 완전 집합 규칙을 새로 도입하지 않는다. base에서 허용된 historical PASS ID는 여전히 같은 Plan의 실제 PASS row여야 한다. 없는 ID, FAIL ID 또는 다른 Plan의 PASS ID를 허용하라는 제안이 아니다.

## 독립 재검토 범위

새 author frozen commit/tree/direct-base와 이전 frozen candidate 대비 전체 diff를 받은 뒤, policy 경계 변경과 해당 regression만 읽는다. 새 실행은 historical PASS ID positive 및 기존 latest failure/superseded protection의 관련 class에 한정하고 broad campaign·provider·Windows 반복은 하지 않는다. 원본 REVIEW.md/commands/raw hashes는 역사 evidence로 보존한다. 이 문서 생성 시 테스트·provider 추가 호출은 0회다.

## 기존 exact evidence

- baseline probe raw SHA-256: `b5a671101a963219821cc18afb014da9f1b2036a4444c4f86d1c4f8bd557bb96`
- candidate probe raw SHA-256: `f811fc4b064cf639476beb0eac571107f3df6783c254cf3549d2ce21599435a4`
- historical REVIEW.md SHA-256: `35b85037bf961d343000e30fa4c2fbfa1c03a66c53a75175ce5919f2ea566c60`
- original sanitized bundle SHA-256: `3a124c754fe96f10a55cde4495bc1f3f3e4d69b060152cfcfcd09535d41e289e`
- actual independent reviewer: `01a0f9e2-ed1d-7296-815a-14270ae95034`; requested `gpt-6.1-sol/high`; observed model/effort null/null.
