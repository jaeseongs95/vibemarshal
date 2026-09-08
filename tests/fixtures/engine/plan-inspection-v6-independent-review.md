# v6 검사 범위 개정의 독립 검토

`dependency_scope_review` 서브에이전트에 `gpt-5.6-sol/high`를 명시해 독립 검토를 요청했다. 아래 결과는 메인이 실제 검토 응답과 파일 해시를 대조해 기록했다. 요청 설정은 backend의 독립 설정 관측과 구분하며, 별도 작성자라는 운영상 기록은 암호학적 신원 증명이 아니다.

검토 결과는 **고정 입력·기대값의 독립 정적 검토 PASS**다. 실제 provider 호출·테스트 runner·qualification은 이 검토에서 실행하지 않았다.

## 변경 이유와 범위

기존 정상 계획은 파일 집합·diff로 의존성 상태까지 변경되지 않았음을 검증한다고 선언했다. Goal은 의존성 추가를 비목표·금지효과로 두지만, 별도 의존성 자원의 범위·전후 상태 검사는 요구하지 않는다. 등록 자료와 `bugfix-trace/oracle.py`가 식별한 관측 범위는 프로젝트 파일·보존 해시·AST·함수 계약·unittest다. 따라서 의존성 상태를 포함한 검증 주장은 지원 범위를 넘는다.

새 revision은 6개 base Plan과 그로부터 파생한 5개 Plan의 `/definition/tasks/0/validations/2/statement`에서 `val_task_scope_preservation`을 파일 보존 검사로 한정한다. 의존성 금지효과는 Goal·Plan에 보존한다. 같은 입력을 공유하는 case를 포함해 12개 scope 문장·직접 citation을 동기화하고 Plan digest를 다시 결속한다.

## 직접 확인한 사항

- 23개 입력 중 실제 변경은 11개 Plan의 해당 문장과 `definition_digest`뿐이다. 나머지 12개 입력은 동일하다.
- 17개 Plan의 strict 계약과 18개 case의 `model_dump(mode="json")` 기준 Plan digest가 일치한다.
- Goal·Plan 금지효과, AC 연결, integration `criterion_refs`, AC bool 표, required defects, 평가 범위·합격선과 provider 호출 13개의 순서를 보존했다.
- 변경된 직접 인용 12개는 같은 Plan selector에서 새 문장과 정확히 일치한다.
- 원본 source-input canonical digest 17개와 검토 입력 SHA 23개를 확인했다.
- `historical-r-s06-09-clean`의 과장 문장과 과거 표는 보존했다. 이 역사 입력을 새 정상 사례로 재인증하거나 v6의 실제 11사례에 포함하지 않는다.
- 기존 객체 배열에 설명 문자열을 추가했던 초안은 수정해 `normalization_assertions`를 v5와 동일하게 유지했다. 과거 실제 실행 검증 주장·경로는 `prior_review_metadata`로 옮겼다.

## 검토 결속

| 대상 | SHA-256 |
| --- | --- |
| v6 expectations 파일 | `cb40003b3c6486f178cfb617c7d472462b73c6c372cd96cbd1d7899b09cb60ae` |
| 23개 입력 manifest의 canonical digest | `0ec9246f9a6f3aaa44ebaf57353984de63cf5b6ec81a1ecff739c462f3533e2a` |
| runtime expectations | `bd01ecd71cfbc633fce5a903416a594df5a9036d31dfd5e2f56c49b37aa8280d` |
| 최종 완료 표기 전 검토 초안 | `f7b6c495b9561c0b0b3bee7ed59353a200ee11e766fc4189db9705f15fe3f5f6` |

기존 v5 파일·실제 실행·원시 응답·FAIL은 변경하지 않았다. 이 정적 검토는 새 source의 결정적 Gate와 실제 모델 qualification을 대체하지 않는다.
