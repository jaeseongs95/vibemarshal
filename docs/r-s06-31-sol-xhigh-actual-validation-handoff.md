# R-S06-31 Sol/xhigh 제한 실제 재검증 인계

이번 경계는 **FAIL — 호출 전 기준 HEAD 불일치**다. deterministic Gate는 새 하위 root에서 1회 실행해 **5/5 PASS**했지만, 준비 과정에서 기준 HEAD가 다른 작업의 문서 commit으로 변경됐다. 사전 검증의 첫 AssertionError에서 중단했다. 실제 Reviewer 품질 결과는 없으며 13사례 모두 **NOT_RUN**, logical/provider/recovery는 **0/0/0**이다.

이는 모델 구조·참조·의미 실패가 아니라 실행 환경의 기준 변경이다. Sol/xhigh 후보는 보류하며 S06 전체·Functional Alpha는 NOT_RUN, **1.0 NO-GO**를 유지한다. S07·모델 비교·원인분석·새 작업·callback·예약을 시작하지 않았다.

## 실제 정책과 시작·변경 관측

첫 조회 전에 현재 turn 개발자 `<permissions instructions>`의 `sandbox_mode=danger-full-access`, `approval_policy=never`를 직접 확인했다. 사용자 환경의 파일 시스템도 unrestricted였다. 부모 기대값으로 추정하지 않았으며 승인 질문·권한 상승 요청 없이 실행했다. 적용되는 전역·프로젝트 AGENTS.md, 상위·docs·evaluation 경로의 override 여부와 대체 지침 설정을 확인했다.

| 시점 | 실제 관측 |
|---|---|
| 시작 HEAD / origin/main / 원격 main | 46fd9169906869994849ce190c69ddf6b1b3e021 |
| 시작 worktree | clean 아님: ?? docs/gui-interface-design.md |
| prepare 계약 base_head | 840a37ea5258062fff84571fde6a4c8adbae890b |
| 동시 변경 | 840a37e GUI 인터페이스 설계명세 추가 — docs/gui-interface-design.md 799행 추가 |
| provider 전 재조회 | HEAD=origin/main=원격 main=840a37e, clean |
| 권위 origin | https://github.com/jaeseongs95/vibemarshal.git; gh repo view에서 isPrivate=true |
| GUI 문서 bytes SHA-256 | sha256:4f185662c89707d2841c65634744c3e3b5b89ecda48a123772dfd952eac91037 |

시작의 미추적 GUI 문서는 이 세션이 수정·삭제·커밋하지 않았다. 이후 해당 파일만 추가한 별도 commit이 관측됐으며 최초 bytes와 일치한다. 시작 당시 요청의 clean 조건은 충족되지 않았고, source가 아닌 독립 문서로 보고 보존한 채 Gate를 진행했다. 이어 prepare가 잠근 HEAD까지 달라졌으므로 기준 검사를 완화하거나 새 root·계약·재호출로 우회하지 않았다.

[R30 구현 인계](r-s06-30-scope-finding-binding-handoff.md), [이전 실제 결과](r-s06-30-sol-xhigh-actual-validation-handoff.md), [복구 설계](C:/Users/sjs95/Documents/ChatGPT/자동화%20문제%20원인분석/R-S06-30-참조결속-복구설계.md), 권위 설계와 cutover ADR을 확인했다.

## 새 root와 계약

새 root: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v1`. 생성 직전 부재를 확인했다. R29/R30 run·receipt·terminal·응답은 resume하거나 성공 근거로 사용하지 않았다. R30의 로컬 실행·감사 helper 로직만 새 root와 지정 HEAD에 맞춰 복사했다. 기존 fixture builder의 역사적 입력 출처는 유지했다. 제품 source·기존 raw/frozen artifact·prompt/schema 의미·oracle·expectation·threshold·taxonomy는 변경하지 않았다.

역할 설정은 절대 경로 `D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-xhigh-roles.json`으로 주입했다. general Reviewer는 `gpt-5.6-sol/xhigh`, fallback은 `[]`다. 다른 역할은 기존 설정 그대로이며 실제 provider 호출은 없었다.

| 결속 | digest |
|---|---|
| source_manifest_digest | sha256:34c275c7aa39dbfacc611d3d67f7c319232fe9546d0ac8bac8c7d7b8af300af6 |
| lock_digest | sha256:c1227210b9c0247850b983094cb019422950fd55b339a4f5fe1b095b3b4e9d5d |
| inventory_digest | sha256:76b6120a26acde3f173d1c03177645e08a3bbcb90bca8347f31743917b37d7c2 |
| model_lock_digest | sha256:9cb6e554b5ffbab137cbfe643f8e3d67c0b81f3963dd26fcf33f08d1cde5a005 |
| prompt_digest | sha256:d54e0f443a9d38f15e7499457c3b0647c8ab1e4cbfc9da71834f73edbac1db89 |
| output_schema_digest | sha256:6b1e34900c2ce8141b040eaf1366592d21abd5cdba16ca758de7cc7d2ea5055f |
| harness_digest | sha256:18b4805bc74830412211486cabfae903be78dfe66cdd058f9e8a32789ac802cb |
| codex_bin_digest | sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d |
| deterministic_report_digest | sha256:3051961de097e16bb6e572ef3f446237740a02beae8d8b6132d426f41163f0bf |
| configuration_digest | sha256:0ca70f5002e0254e36f2fe7709080e2a15977cfa75c355cdc48c08f8222a1418 |
| source_bytes_digest | sha256:1e6a41c85c50a3804eda4a45395c0ea6eb49c4f2ecd721acfe2cb6ebee5f6ba9 |
| source_canonical_digest | sha256:91d94b1f0cb40598ca3f5a5eb3f019c94e28a3c35d88252e2a31f3b6a3faad9c |

prepare는 fresh 실제 App Server inventory와 정책을 수집하고 선택·fallback 지원 및 model-lock-v2를 검증했다. source snapshot·prompt·strict schema·입력·고정 기대표·독립 review·역할 bytes/canonical/typed digest·자동 주입 지침을 immutable preflight와 planning binding에 결속했다. 사전 request 12개는 준비 artifact이며 provider request 전송 12회를 뜻하지 않는다. provider 전 독립 감사는 HEAD 검사에서 중단되어 완료되지 않았다. 실제 thread receipt의 지침 경로 대조·호출 직전 inventory 재관측·완료 terminal 결속은 모두 NOT_RUN이다.

| 자동 주입 지침 경로 | bytes digest |
|---|---|
| C:\Users\sjs95\.codex\AGENTS.md | sha256:2c113a26bd82cd1964a444ae53c99c3b4d45a2d42faec1262d0ade358c9751dc |
| D:\codex\flowmarshal\AGENTS.md | sha256:9d94703fc9240f47dcd562568a8c9218681e5c6af8bc535817ad62e3f4004f4e |
| D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v1\workspace\AGENTS.md | sha256:57fb50121f05548961e1e725e5537526b4e9cbc754383f8d42b0162096944b2a |

## 실행·첫 실패·사례별 결과

| Gate | 결과 | 직접 관측 |
|---|---|---|
| legacy-freeze-manifest | PASS | 완료 receipt 확인 |
| compileall | PASS | 완료 receipt 확인 |
| pip-check | PASS | 완료 receipt 확인 |
| synthetic-lifecycle | PASS | 완료 receipt 확인 |
| full-test-suite | PASS | Ran 615 tests in 74.266s, OK |

| 실행 | 횟수 | 초 | 종료 코드 |
|---|---|---|---|
| gate | 1 | 79.688 | 0 |
| prepare | 1 | 8.125 | 0 |
| pre_audit | 1 | 도구 wall time 2.487초 | 1 |
| run | 0 | 미실행 | 해당 없음 |

실제 명령:

```text
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\codex\flowmarshal --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v1\deterministic
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 prepare --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v1 --role-config D:\codex\flowmarshal\tests\fixtures\engine\plan-inspection-general-reviewer-sol-xhigh-roles.json
D:\codex\flowmarshal\.venv\Scripts\python.exe -X utf8 -B D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-sol-xhigh-r31-20260905-v1\runtime-preflight\session-audit\pre_audit.py
```

정확한 첫 실패는 `pre_audit.py:15`의 다음 검사이며 실제 예외는 `AssertionError`, 종료 코드 1이다. `BASE_HEAD_MISMATCH`는 이 원문을 설명하는 보고용 진단명이다.

```text
assert lock['base_head'] == '46fd9169906869994849ce190c69ddf6b1b3e021'
expected=46fd9169906869994849ce190c69ddf6b1b3e021
actual=840a37ea5258062fff84571fde6a4c8adbae890b
AssertionError
```

| 순서 | 사례 | 결과 | 구조·의미 assessment |
|---|---|---|---|
| 1 | clean | NOT_RUN | 미평가 |
| 2 | bad | NOT_RUN | 미평가 |
| 3 | wrong-goal | NOT_RUN | 미평가 |
| 4 | combined | NOT_RUN | 미평가 |
| 5 | boundary-clean | NOT_RUN | 미평가 |
| 6 | missing-link | NOT_RUN | 미평가 |
| 7 | future-result | NOT_RUN | 미평가 |
| 8 | stored-expanded | NOT_RUN | 미평가 |
| 9 | semantic-explicit | NOT_RUN | 미평가 |
| 10 | stored-multi-defect | NOT_RUN | 미평가 |
| 11 | semantic-missing-link | NOT_RUN | 미평가 |
| 12 | expansion | NOT_RUN | 미평가 |
| 13 | expanded-review | NOT_RUN | 미평가 |

expansion·생성 Plan 독립 검토·전용 기대표·expanded-review에도 진입하지 않았다. 실행 receipt·완료 terminal·thread/turn·output 결속은 생성되지 않았다. 모호한 provider 효과가 없어 external_unknown=0이다. 구조 실패 뒤 의미 결과를 추정한 사례도 없다.

## 실측 사용량과 raw evidence

logical/provider/recovery는 **0/0/0**, 계약 상한은 **13/13/0**이다. 모델 역할 호출이 없으므로 provider token·role latency 측정치는 없으며 `null`로 기록했다. null을 실측 token 0이나 비용 0으로 바꾸지 않는다. 청구 비용과 구독 한도 차감량도 추정하지 않는다. Gate/prepare 프로세스 경과 시간은 위 표의 별도 관측이다.

| artifact (새 root 기준) | bytes SHA-256 |
|---|---|
| assignment.json | sha256:92736673ef177325f194b0150b6ee52c7dbaefd59c41e6a5618eb4ab32bb4e19 |
| preflight.json | sha256:3b202c5e09596799687df941081cd073f6c2c37e0b7f0b5bcb77005722edd088 |
| planning-binding.json | sha256:317136bce250678e08f77a22be5c8850a5616ee8e1189f1b748e70c421282761 |
| roles.json | sha256:1e6a41c85c50a3804eda4a45395c0ea6eb49c4f2ecd721acfe2cb6ebee5f6ba9 |
| expectations.json | sha256:3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e |
| independent-fixture-review.json | sha256:9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27 |
| executed-source-manifest.json | sha256:123df1230177af9b3718985f4a8e394e2615d01df3715bf08713fdafb18ffdd2 |
| instruction-binding.json | sha256:9ef3d6f2ac2ec305716849ad6d85e81c5cf3f9e52cbe07e4becae2561aa67bfd |
| deterministic-environment-binding.json | sha256:10c8c1967721311fe87cb218f8db742836a72a4ed05fe39225a7fcb0a7b666c4 |
| deterministic/evaluation-contract.json | sha256:5615544b577f0b42f4c80d031e1b906a31fa1e5da10d19d4242ad6bcbd97c18c |
| deterministic/qualification-report.json | sha256:f5e92b976cc25df95fc15a4688862347679b2e104f324bcb34d04c603981e3fc |
| runtime-preflight/prepare/inventory-01.json | sha256:016c3acd7163f603422ef334fdeadc7a64666483c0b8da8556940fab79e5e51f |
| runtime-preflight/prepare/policy-01.json | sha256:9411a80fdeeed56f87ea55a845df6b890ff14d5bd423dda1830e9b4f808dce6c |
| runtime-preflight/session-audit/initial-state.json | sha256:d8ca34e656914edb3a42ed9ae22c19e3c02817ec3f0c30c6edcb9beab326497b |
| runtime-preflight/session-audit/preflight-failure.json | sha256:5cb6fe74afc75faae2cd7247e50b74c8896f52c060837c610319981507aeec30 |
| summary.json | sha256:17574f0b7c0f25dea34cb18fff6c91efbfbcf9e5c9c14f0813252d844ee14f23 |
| runtime-preflight/session-audit/boundary-evidence-manifest.json | sha256:cab92a4f5bf77d43d536551a733ca60cbf8dab07feb9627071636bd64fecbe2f |

모든 raw evidence는 위 Git 제외 로컬 root에 보존했다. 전체 파일 digest는 `runtime-preflight/session-audit/boundary-evidence-manifest.json`에 있으며, Gate/prepare 원본 stdout·stderr·시작/완료 기록과 실행 전후 환경 snapshot도 포함한다. 실패 traceback은 실제 도구 결과의 chunk ID와 명령·줄·종료 코드를 `preflight-failure.json`에 기록했다. 동일 검사를 재실행해 로그를 만들지 않았다.
보고서 helper의 최초 실행에서는 scripts import 경로 누락으로 artifact 생성 전 ModuleNotFoundError가 발생했다. 로컬 helper 경로만 수정했으며 Gate·prepare·pre_audit·provider를 재실행하지 않았다.

## 보존·전달

문서 작성 전 source 236파일, 시작 tracked 3438파일, 과거 보존 artifact 8764파일의 bytes 불변을 확인했다. Gate와 prepare 실행 전후 환경은 동일하다. 본 인계와 docs/README.md만 이번 commit에 포함한다. `git diff --check`·변경 범위 확인 후 승인된 private origin main에 한국어 commit으로 push하며, 실제 전달 commit·HEAD/origin/main/원격 main·clean은 자기참조를 피하도록 별도 `runtime-preflight/session-audit/delivery.json`과 최종 응답에 남긴다.

이 경계는 결과 기록 후 종료한다. 13사례의 실제 검증은 수행되지 않았고, 이 문서는 후속 실행이나 모델 변경을 승인하지 않는다.
