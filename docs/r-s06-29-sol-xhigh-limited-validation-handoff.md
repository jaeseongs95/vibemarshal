# R-S06-29 general Reviewer Sol/xhigh 제한 실제 검증 후보 고정

R-S06-28에서 general Reviewer `gpt-5.6-sol/high`는 strict 구조·참조·receipt·v2 결속을 통과했지만, 첫 clean의 `ac_004 × val_goal_independent_unittest`를 `expected=true` 대신 `actual=false`로 제출해 27/28 의미 행 일치로 실패했다. 따라서 Sol/high는 general Reviewer 후보에서 탈락한다.

이 문서는 제품 기본 역할과 Goal·Plan·adapter·evaluator, 기존 fixture·기대표·oracle·threshold·taxonomy를 변경하지 않는다. R28의 원본과 권위 Goal/Plan을 대조한 원인분석은 AC-004의 독립 Goal Test 의무, `val_goal_independent_unittest`의 전체 statement·method·`evidence_mode=independent`·통합 소유, 양방향 `goal_coverage` 및 `criterion_refs`에 따라 `expected=true`가 타당하다고 결론 냈다. prompt·schema에도 AC 직접 검증, 다른 ID 대체 금지, 독립 검사 의무를 sibling 비전염 규칙으로 없애지 않는 조건이 이미 들어 있었다.

새 후보는 [명시적 역할 설정 fixture](../tests/fixtures/engine/plan-inspection-general-reviewer-sol-xhigh-roles.json)다. 기존 Sol/high fixture와 비교하면 `general_reviewer`의 effort만 `high`에서 `xhigh`로 바뀌며, model은 계속 `gpt-5.6-sol`이다. 나머지 여섯 역할의 model·effort와 모든 빈 fallback envelope는 유지한다. 지원 여부는 실제 역할 호출 없이 제품의 typed `ModelInventory`와 `validate_role_configuration_inventory()`로 보존된 App Server `model/list` 관측을 대조한다.

| 결속 | SHA-256 |
|---|---|
| fixture 원문 bytes | `sha256:1e6a41c85c50a3804eda4a45395c0ea6eb49c4f2ecd721acfe2cb6ebee5f6ba9` |
| fixture canonical JSON | `sha256:91d94b1f0cb40598ca3f5a5eb3f019c94e28a3c35d88252e2a31f3b6a3faad9c` |
| typed configuration | `sha256:0ca70f5002e0254e36f2fe7709080e2a15977cfa75c355cdc48c08f8222a1418` |

새 fixture는 절대 `--role-config` 입력으로만 prepare에 주입한다. 원문 bytes·canonical·typed configuration digest, 원문 snapshot, planning/preflight 결속과 `flowmarshal-model-lock-v2`의 선택·지원·빈 fallback·request binding은 회귀로 고정한다. 후보 검증은 제품 기본 역할 변경이나 실제 의미 검증 성공을 뜻하지 않는다.

다음 경계는 새 미사용 root와 이 절대 Sol/xhigh role-config로 하는 **R-S06-29 제한 실제 검증**이다. 실행 전 fresh 결정론 Gate 5/5(compileall, 전체 unittest, pip check, synthetic lifecycle, legacy freeze), source manifest·interpreter/package/environment와 cell receipt·stdout/stderr digest 결속을 새로 남긴다. 이어 provider 호출은 규정된 한 번의 제한 검증만 허용하며, fallback·resume·재호출·기존 run/raw/frozen artifact 변경은 하지 않는다.

결정론 회귀의 통과는 Sol/xhigh의 역할 적격성, S06, planning pipeline, E2E 또는 qualification PASS가 아니다. R-S06-29의 실제 결과가 관측되기 전까지 FlowMarshal 1.0은 NO-GO이며 추가 사용자 판단은 필요 없다.
