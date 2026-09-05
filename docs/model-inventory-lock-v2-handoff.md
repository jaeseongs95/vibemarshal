# 모델 inventory 실행 잠금 v2 구현 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`. 시작 HEAD와 원격 main은 `44c98cb796fde8da7cc8a97f1c497d3112d6ff19`이며 작업 트리는 clean이었다. 개발 turn에 실제 제공된 `sandbox_mode=danger-full-access`, `approval_policy=never`를 첫 파일 접근 전에 확인했다.

## 변경 결과

전체 inventory digest의 정확한 일치를 실행 조건으로 사용하던 경로를 `flowmarshal-model-lock-v2` 운영 계약으로 전환했다. 이전 제한 검증의 `MODEL_OR_EXECUTABLE_LOCK_CHANGED` 원인인 무관한 모델 삭제는 이제 감사 digest만 바꾸고 실행 projection을 바꾸지 않는다. 이 결과는 결정적 구현 검증이며 실제 R-S06 역할 평가 성공을 뜻하지 않는다.

- `model_lock.py`: 전체 raw JSON과 inventory 감사 digest, 역할별 선택·순서 있는 fallback·지원 상태, executable·필수 runtime capability projection을 분리했다. projection과 원래 observation을 서로 대조하고 두 digest를 검증한다.
- runtime parser: SDK typed coercion 전에 원본 `model/list`를 검사한다. 모든 수신 행의 duplicate model/effort, empty/null/잘못된 식별자·effort, 중복 field alias와 불완전 pagination을 거부한다. hidden 행도 검사하며 목록 순서를 감사 evidence에 보존한다.
- qualification·진단: v2 format을 evaluation contract·checkpoint·preflight에 명시했다. 새 prepare는 기존 입력·역할 설정·실행 파일 기준을 보존하며 v1 digest를 provenance로만 기록한다. 저장된 요청은 준비 당시 observation으로 재구성하고 실제 호출 직전의 전체 선택 역할 잠금을 다시 대조한다.
- 역할·실행: 요청·receipt, materialization, dispatch, 내부·공개 resume, 독립 Goal Test에서 같은 v2 검증기를 사용한다. Task intent와 역할 receipt에 실제 inventory observation을 남긴다. 요청·관측·receipt digest 위조를 검사한다.
- fallback: preferred가 없을 때 허용 목록을 자동 순회해 대체하던 resolver 동작을 제거했다. fallback envelope·순서·가용성은 잠그되, 현재 resolver는 명시적 새 binding이 필요한 경우 차단한다. 실패 후 즉시 모델을 바꾸거나 schema recovery로 재호출하지 않는다.

기존 `PlanContractDefinition.model_inventory_digest`, 과거 run·raw·artifact와 R1~R3.1 legacy/prototype은 수정하지 않았다. 모델 설정 파일·검사·threshold·oracle·taxonomy도 변경하지 않았다. 권위 설계 §7.1, cutover ADR와 제품 `AGENTS.md`를 운영 계약에 맞췄다. 적용한 템플릿 `AGENTS.md`의 감사 evidence·자동 fallback 금지 원칙도 대조했다.

## 실제 검증

| 항목 | 결과 |
|---|---|
| 최종 집중 회귀 | 28 tests PASS |
| 전체 관련 테스트 | 571 tests PASS, 64.370초 |
| 결정적 Gate | 5/5 PASS, 실패 0 |
| compileall·pip check·synthetic lifecycle | 각각 종료 코드 0 |
| legacy freeze | 40파일 PASS, 변경·누락·추가 0 |
| `git diff --check` | PASS |
| 기존 파일 EOL | HEAD와 형식 불일치 0, 일반/ignore-EOL numstat 동일 |
| 최종 Gate source 대조 | 현재 source digest와 일치 |
| 실제 provider 역할 호출 | 미실행 |

집중 검증 명령:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_engine_model_lock tests.test_engine_model_lock_consumers tests.test_engine_inspection_case_binding tests.test_engine_execution_automation.ExecutionAutomationTests.test_run_revalidates_replayed_execution_preparation_without_runtime_or_spec_and_deduplicates_usage -q
```

최종 Gate 명령:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\codex\flowmarshal --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\model-lock-v2-20260905-deterministic-v2
```

이 Gate는 실제로 compileall, 전체 unittest, pip check, 합성 lifecycle과 freeze를 실행했다. 각 명령·종료 코드·stdout/stderr 및 동결 결과는 위 run의 `cells/`에 있다. 최종 계약과 결과는 `evaluation-contract.json`, `qualification-report.json`에 보존했다. 이전 개발 중간 Gate 결과를 최종 source 검증으로 재사용하지 않았다.

- 최종 source manifest: `sha256:f2543cdcd6d457620e6ea07baa6a0b6c2fd2f32e9ace8c22ecc18982a4d3ed4d`
- 최종 evaluation contract: `sha256:5f27761faba2c01b6badcda559901fbeefd8dc71ec7b0801972b970cf3f27b6b`
- 최종 report digest: `sha256:ac5794d25e1baf9e443f624f0ed6c1049125925b41743a6325bdad59846eed25`

새 회귀는 무관한 model 추가·삭제와 목록 순서·미사용 effort 변화, fallback의 추가·삭제·effort·순서·가용성, 선택 역할·model·effort, executable·capability 변경을 구분한다. prepare 이후 역할 호출·dispatch·내부/공개 resume·독립 Goal Test의 허용과 차단, 현재 호출과 다른 선택 역할 삭제의 사전 차단, 요청·관측·receipt 위조와 v1 재사용 거부를 확인했다. 모의 runtime의 preflight 차단 사례에서 thread/turn 효과 0과 자동 recovery 없음도 확인했다.

## 잔여 경계

실제 provider를 사용하는 제한 R-S06 검증은 이번 세션에서 실행하지 않았다. 해당 검증은 별도 새 개발 세션의 fresh v2 preflight와 run이 필요하다. v1 checkpoint는 의도적으로 재사용할 수 없고 자동 migration은 없다. 허용 fallback 실행을 위한 별도 명시적 재결속 절차 없이 현재 resolver가 대체 모델을 실행하지 않는다.

기존 실제 S06 FAIL, Functional Alpha 미완료, 1.0 NO-GO 판정은 유지한다. 모델 호출·Plan 활성화·운영 예약·다른 세션 통지나 새 작업 생성은 수행하지 않았다. 커밋 hash와 실제 push 결과는 이 세션의 최종 응답에 기록한다.
