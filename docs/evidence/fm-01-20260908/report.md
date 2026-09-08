# FM-01 계약 정합성 재검증 보고서

검토 기준은 현재 main `c484243cfa871b04b9858a71f74c4736ac505a00`과 승인 계획 원문이다. 이전 FM-01 보고서나 완료 주장을 검증 근거로 사용하지 않았다. 시작 시 main checkout은 `D:/codex/flowmarshal`이었고 tracked/untracked 변경은 없었다. 이번 변경은 문서·지침과 이 증거 묶음에 한정한다. 제품 실행, qualification, 1.0 전환은 수행하지 않았다.

첫 파일 조회·명령 전에 turn의 실제 `danger-full-access`, `approval_policy=never`를 확인했다. 읽은 source·main·template 지침의 요구와 일치했다. 지정 source인 `D:/codex/fm-performance-floor`는 `codex/performance-release-floor`의 worktree여서 필수 입력으로만 읽었다. 최신 main-only 지시에 따라 그 위치나 template에 쓰지 않았다. 원격 push·원장 변경·다른 작업 상태 변경·다음 앱 작업 생성도 하지 않았다.

## 변경과 근거

승인 계획의 SHA-256은 `a24eb860c8b603f8edc43a71370c6d8638cc53d3c5c49b8a568c44fc9f5b1742`다. 필수 source 문서 6개는 작업 시작 시 main 대응 파일과 SHA-256이 모두 같았다. 승인 12항과 검증 임계값은 이미 권위 문서에 반영되어 있었으므로 원문과 직접 대조해 유지했다. 현재 지시와 충돌한 작업 경로·통합 시점·detached preflight·자동 push 조항을 수정했다.

| 변경 파일 | 변경 내용 |
|---|---|
| `AGENTS.md` | 최신 사용자 승인과 과거 source/template 지침의 우선순위, 승인 계약 링크, 승인된 checkout의 preflight, 명시 승인에 따른 push 원칙 |
| `docs/redesign-1.0-contract.md` | 현재 main 쓰기 대상, 전환 전 main 개발·커밋과 감사 후 1.0 전환 구분, planned 의미와 다른 태스크 상태 비판정 |
| `docs/orchestration-redesign.md` | 과거 detached 실험 조건과 현재 승인된 checkout의 구분, 기존 harness 정합화·재검증 책임 |
| `docs/engine-cutover-adr.md` | main 개발과 최종 제품 승격 Gate 구분 |
| `docs/pre-1.0-roadmap.md` | main 한시 규칙, FM-16 전환 책임, planned 상태 해석 |
| `docs/pre-1.0-handoff.md` | 현재 작업 대상·과거 입력 출처·지침 충돌 처리·보존 범위·한시 규칙 만료 조건 |
| `docs/README.md` | GUI를 1.0 후속 설계로 분리하고 당시 exact-digest UX의 비권위성 명시, Alpha 실행 기록의 역사 범위 명시 |

한시 main 규칙과 작업 경로는 인계·로드맵에 두고 AGENTS에는 반복 적용할 장기 원칙만 반영했다. template의 main-only 규칙과 usage/activation/릴리스 원칙은 현재 승인과 일치한다. template의 source_root 보존 문장은 사용자 명시 대상을 따르는 원칙이므로 최신 main 지정을 막지 않는다. 외부 source의 detached·자동 push 조항은 최신 지시와 충돌하는 과거 조항으로 처리했다. 외부 파일을 물리적으로 동기화했다고 주장하지 않는다.

## 요구사항별 반영 위치와 태스크 연결

아래 D/V/M 식별자는 [권위 계약](../../redesign-1.0-contract.md)의 절이다. 현재 행 번호는 [requirement-locations.txt](requirement-locations.txt), 전체 문서 해시는 [documents.json](documents.json)에 있다. 이 표는 책임 연결이며 원장의 완료 판정이 아니다.

| 승인 항목 | 반영 위치 | 구현 책임 | 검증 책임 |
|---|---|---|---|
| 1 목표 승인·내부 Plan | D01, 제품 설계 §2·4 | FM-03, FM-08 | FM-12, FM-14 |
| 2 authorization 결속·Attempt 보호 | D02, 제품 설계 §4·9 | FM-03, FM-05 | FM-12, FM-14 |
| 3 명령 경계·짧은 tick·supervisor | D03, 제품 설계 §10 | FM-04, FM-08 | FM-12, FM-14 |
| 4 활성화 후 모든 역할 job화 | D04 | FM-04, FM-06, FM-08 | FM-12, FM-14 |
| 5 실행·usage 분리 | D05, 제품 설계 §13 | FM-02, FM-04 | FM-12, FM-14 |
| 6 실측·운영 한도 의미 | D06 | FM-02, FM-08 | FM-12, FM-15 |
| 7 기존 운영 제한 | D07 | FM-02, FM-04, FM-06, FM-07 | FM-12, FM-13, FM-14 |
| 8 효과 직전·재시작 안전 | D08, 제품 설계 §9 | FM-05 | FM-12, FM-14 |
| 9 실패 분류·로컬 ContextRequest | D09 | FM-06 | FM-12, FM-14 |
| 10 dedupe·CommitHorizon·ProjectMap | D10, 제품 설계 §5·6 | FM-07 | FM-12, FM-13 |
| 11 schema 4·역사 reader | D11, ADR | FM-02, FM-10 | FM-12, FM-15 |
| 12 v1·배포물 경계 | D12, ADR | FM-07, FM-10 | FM-12, FM-13, FM-15 |
| 결정적·역할48·Planning18·실제 요청 E2E·설치·감사 | V01·V02 | FM-11 harness 및 각 구현 태스크 | FM-12~FM-15; 감사 후 FM-16 전환 |
| 비교 성능·GUI·Localizer·광범위 graph 후속 | V03, 제품 설계 §11.2, 로드맵 | 이번 구현 범위 밖 | 1.0 비차단, 원본 보존 |

역할48의 recall ≥90%·precision ≥85%와 critical false admission/clean false block/schema failure/critical admission seed instability 각 0, Planning18의 정상 4종 선택·정보 부족 2종 질문·호출 ≤14·version ≤5·선택 후보 deterministic finding 0을 원문과 대조했다. 모든 finding 100%를 새 기준으로 추가하지 않았다. V02의 다중 DAG·replan, read_only, sample 밖 Context, missing/late usage, 재시작·응답 유실·강제 종료·timeout, stale, partial resume, 금지 효과·cancel·실행 중 Attempt 보호, 설치 책임과 provider/stub/fault injection 구분도 확인했다.

## 실제 검증

| 검사 | 수행과 관측 결과 |
|---|---|
| FM-01-C1 | 메인과 별도 읽기 조사자가 승인 12항·필수 검증을 권위 본문에 직접 대조했다. 최종 변경의 별도 감사는 `independent-audit.md`에 기록한다. GoalAuthorization 경계·Core 자동 activation·비목표·안전/기능 Gate를 보존했다. |
| FM-01-C2 | `rg -n -C 1`로 usage 차단·exact/수동 승인·비교 성능 필수·경로/Git 문구를 저장하고 문맥을 검토했다. 활성 계약에는 usage 단독 차단·사용자의 exact Plan ID 입력 의무·비교 성능 필수 조건을 적용하지 않는다. 효과 미확정 차단과 내부 digest 결속은 유지한다. |
| FM-01-C3 | 계획 상태·변경 범위를 검토했고 `git diff --check c484243cfa871b04b9858a71f74c4736ac505a00`은 exit 0이었다. 제품 코드·원장·다른 작업 상태를 변경하지 않았다. 실제 역할/Planning/E2E·설치·제품 테스트는 NOT_RUN이다. |
| 원본 보존 | freeze manifest 40개 전부 SHA-256 일치, 누락 root·미등록 파일 0. R3.1 기준선·회귀 fixture·freeze manifest·역사본 5개를 포함한 8개 보호 파일은 시작 commit의 Git blob과 일치했다. |

검색의 역사적 일치는 삭제하지 않았다. `*-before-redesign-1.0.md`, 반복 검증·inspection·feedback·R-S06·Alpha 실행 인계는 문서 지도가 당시 source·시점의 provenance로 분류한다. `planner-r3.md`·`planner-r31.md`와 `spikes/orchestration`의 R1~R3.1 자료는 동결 계약이다. GUI의 당시 exact-digest UX는 후속 제안이며 실제 연결 전 정합화가 필요하다. 현재 적용 여부는 검색 단어 유무가 아니라 이 문맥과 최신 권위 계약으로 판단했다.

재현은 main checkout에서 `& './docs/evidence/fm-01-20260908/verify.ps1'`로 수행한다. 이 명령은 동일 증거 폴더의 관측 파일을 갱신한다. 검토 때 원래 관측을 보존하려면 먼저 commit의 증거를 읽고, 재실행 결과는 새 diff로 비교한다. [verification.json](verification.json)은 기계 검사 결과, [authority-documents.patch](authority-documents.patch)는 시작 commit 대비 권위 문서 diff, [contract-search.txt](contract-search.txt)와 [instruction-search.txt](instruction-search.txt)는 검색 원문이다. 검색·해시 검사는 의미 적합성이나 제품 qualification PASS를 자동 판정하지 않는다.

추가한 evidence까지 staging한 첫 검사에서는 JSON의 CRLF와 patch hunk 제목 끝 공백 때문에 whitespace 검사가 실패했다. 제품 문서 내용의 실패와 구분해 기록한다. 증거 생성기를 LF 출력으로 고치고 diff의 hunk 제목 끝 공백만 제거한 뒤 evidence를 재생성했다. 문서 내용·동결 입력·검색 원문 의미는 변경하지 않았다. 최종 staged diff 검사 결과와 독립 재감사는 [independent-audit.md](independent-audit.md)에 남긴다.

## 남은 검증 경계

FM-01의 완료 범위는 문서 계약 정합성이다. 후속 구현·실제 provider 평가·제품 최종 감사·1.0 전환의 완료를 주장하지 않으며 다른 작업 상태를 변경하지 않는다. 기존 harness의 detached 조건을 현재 main 정책에 맞추는 구현·실행 검증은 후속 검증 담당이 확인해야 한다. source/template 원본은 main 밖 쓰기 금지 때문에 보존했으며 적용 우선순위와 현재 실행 위치를 main 문서에서 명확히 했다. 토큰·계정 사용률·과거 PASS를 이번 작업의 요금이나 새 검증 결과로 환산하지 않았다.
