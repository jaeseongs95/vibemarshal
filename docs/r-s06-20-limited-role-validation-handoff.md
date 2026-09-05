# R-S06-20 CLOSE 이후 제한 실제 역할 검증 인계

기준일: 2026-09-05 KST. 제품 대상은 `D:\codex\flowmarshal`이다.

## 판정

**BLOCKED — 새 preflight 준비 중 `MODEL_OR_EXECUTABLE_LOCK_CHANGED`로 중단했다.** 실제 역할 검증은 시작하지 않았으며 logical role call·provider turn·새 역할 thread는 모두 **0건**이다. 첫 실패 후 재호출·모델 교체·계약 보정은 하지 않았다.

현재 visible model inventory에는 기존 고정 목록의 `gpt-5.4`가 없다. 이번에 배정된 Luna·Terra·Sol의 model/effort는 모두 지원되며 `roles.json`과 Codex 실행 파일도 원래 값과 같다. 그러나 기존 실행기는 전체 inventory digest와 거기에 결속된 model lock까지 과거 고정값과 일치해야 진행하므로 차단됐다. 모델이 사라진 이유는 이번 관측으로 확인할 수 없다.

| 구분 | 이번 결과 |
|---|---|
| 완료된 R-S06-19-CLOSE | 완료 유지. 보정·회귀를 재수행하지 않음 |
| R-S06-20 준비 | FAIL, 새 `preflight.json` 미생성 |
| 제한 실제 역할 평가 | **NOT_RUN, 0/13**. 모든 사례 미실행 |
| 마지막 실제 R19 v2 | 기존 `schema_failed`·FAIL 유지 |
| R19 원본 raw의 오프라인 의미 | CLOSE에서 확인한 AC boolean 불일치 2건 유지 |
| 현재 source의 실제 역할 의미 검증 | **NOT_RUN** 유지 |
| 기존 실제 S06 / Functional Alpha / 1.0 | **FAIL / 미완료 / NO-GO** 유지 |
| 다음 경계 가능 여부 | **불가**. 이번 제한 검증이 충족되지 않았으며 후속 작업을 선택·생성하지 않음 |

## 실제 권한과 source

첫 파일 조회·명령 전에 이 새 turn의 개발자 권한 설정에서 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인해 첫 응답에 기록했다. 부모 기대값이나 전역 config를 이 turn의 권한으로 간주하지 않았다. 이번 차단은 개발 turn의 `PERMISSION_POLICY_MISMATCH`가 아니다.

제품 runtime도 새 workspace에 대해 `config/read`·`permissionProfile/list`로 실제 `:danger-full-access/never`를 확인했다. 그 관측의 config digest는 `sha256:02e2cf6d9234892e79549d86d72c6b7bdad9b98801280e48d5723a9853beec3a`, profile catalog digest는 `sha256:69b84ba576775830ce29c176d134b160fbd8f5a96b3b8a63d23161f66d138af2`다. 전역·제품 `AGENTS.md`, 권위 설계 §6.1, cutover ADR, R3.1 동결 기준과 [CLOSE Handoff](r-s06-19-close-handoff.md)를 확인했다.

- 시작 HEAD: `9e0328b51aca4621c3ec15c17d3955cd25179c3d`.
- 시작 branch·상태: `main`, clean, `origin/main`과 일치.
- Engine source before/after: 모두 `sha256:febb5decb9b4c57957f786de8221844beed96427ce9716da42d257635b448a29`.
- 제품 구현·Prompt·Schema·테스트·fixture·oracle·기대값·threshold·model/effort 변경: 없음.
- 저장소 변경: 본 인계 문서 1파일만. README·구현 현황·AGENTS.md·장기 설계는 변경하지 않았다.

## Fresh run과 기존 계약의 관계

새 run root:

```text
D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-close-r20-20260905-v1
```

이하 **새 run**으로 표기한다. 기존 `scripts/diagnostics/r_s06_10.py`를 수정 없이 사용하기 위해 허용된 `r-s06-19-` prefix를 유지했다. `assignment.json`의 개발 검증 식별자는 R-S06-20이며, 완료된 CLOSE나 과거 R19 실행을 재개·덮어쓴 것이 아니다.

기존 계약 root는 `.flowmarshal-engine-eval/runs/r-s06-19-20260905-v2/`다. 기존 실행기가 준비한 새 Goal·State·Plan 입력, `roles.json`, `expectations.json`, `independent-fixture-review.json` **26파일**은 이 R19 root의 대응 파일과 byte-for-byte 같았다. 기존 fixture 작성·독립 검토 검사는 통과했으며 입력·정답을 이번 결과에 맞춰 수정하지 않았다.

CLOSE의 Engine 입력 221개, Python version·경로·binary hash·설치 package 목록·플랫폼이 현재와 같음을 확인했다. 따라서 CLOSE에서 실제 실행한 558 tests·결정론 Gate 5/5를 재사용했다. `deterministic/`에는 그 원본 9개 JSON을 바이트 그대로 복사했고 `reused-deterministic-evidence.json`에 출처·digest·재사용 범위를 기록했다. 전체 테스트나 전체 qualification을 새로 실행한 결과로 표기하지 않았다. legacy freeze는 이번에도 40파일 PASS를 직접 확인했다.

새 source manifest와 환경 관측은 `source-before.json`에 기록했다. 그러나 실제 inventory lock 검사에서 중단돼 **새 Prompt·Schema·사례별 expectation·instruction binding을 완성한 preflight는 없다.** `planning-binding.json`, `requests/`, `schemas/`, `case-expectations/`, `instruction-binding.json`, `executed-source-manifest.json`도 새 실행용으로 생성되기 전이었다. 과거 R19 lock을 현재 실행의 성공한 preflight로 재사용하지 않았다.

## 직접 관측한 차단 근거

기존 실행기의 `prepare()`는 역할 지원 검사를 통과한 뒤 다음 세 값의 정확한 동일성을 요구한다 (`scripts/diagnostics/r_s06_10.py:283`).

| 항목 | 기존 R19 고정값 | 새 관측 |
|---|---|---|
| inventory digest | `sha256:82e6bbcba85b38800c736c9f9809a493fbc9b3270cb53b514721a80839a49f14` | `sha256:7efa54159bb352b5bfffe2f2b9c2fb45de1b2b95ff2917cd48ae41f9edf197b2` |
| model lock | `sha256:5cca3c82ad7d7eb4e61aea0dba9e2101a465860b8abe1ec771538bc7d13762ec` | `sha256:accdedd74018f6767431736b1f07295d7f60077ede1ba74576d439cd88579e8e` |
| Codex executable digest | `sha256:935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d` | 동일 |

모델 목록의 실제 차이는 **`gpt-5.4` 한 항목 부재**다. 과거 supported efforts는 `high`, `low`, `medium`, `xhigh`였으며 현재 visible 목록에 그 model ID가 없다. 이번 역할 `roles.json`은 R19와 같고 일반 Reviewer `gpt-5.6-terra/high`를 포함한 모든 배정의 `validate_inventory()`는 통과했다. `qualification.py`의 `_model_lock()`은 전체 `inventory_digest`와 역할 설정을 함께 hash하므로, 배정 자체가 같아도 이번 inventory 변경으로 model lock이 달라졌다.

S05 input lock과 R19 preflight의 이 세 고정값도 일치함을 확인했다. 따라서 기존 실행기가 다른 과거 값을 잘못 읽었다고 해석하지 않는다. 이번 요청의 계약 불일치 중단 조건에 따라 값 덮어쓰기, lock 검사 제거, 실행 파일·역할 모델 교체 또는 새 준비 재시도를 하지 않았다.

## 실행 명령과 미실행 범위

실행 cwd는 `D:\codex\flowmarshal`이며, 다음 기존 명령을 로컬 `run_stage.py`로 한 번 실행해 stdout·stderr·종료 코드를 보존했다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m scripts.diagnostics.r_s06_10 prepare --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-19-post-close-r20-20260905-v1
```

- 준비 시작·종료: **2026-09-05 09:23:20.961~09:23:25.354 KST**.
- process exit code: **1**.
- `preparation-failed.json`: `status=FAIL`, `RuntimeError: MODEL_OR_EXECUTABLE_LOCK_CHANGED`, `provider_turns=0`.
- 실제 metadata 관측: 정책 확인 1회, `model/list` 1회. 역할 thread 생성·turn 시작·schema recovery는 0회.
- `run`과 `review-generated` 명령은 실행하지 않았다.

기존 순서인 `clean → bad → wrong-goal → combined → boundary-clean → missing-link → future-result → stored-expanded → semantic-explicit → stored-multi-defect → semantic-missing-link → expansion → expanded-review`는 전부 **NOT_RUN**이다. 13/13 상한과 recovery 0·첫 실패 중단 계약을 유지했고 첫 clean도 시작하지 않았다. 사례별 상태는 `limited-validation-outcome.json`에 있다.

실제 역할 usage·역할 latency·provider duration·청구 금액은 **null**이다. 실행하지 않은 turn의 측정값을 0으로 채우지 않았다. 위 준비 시간은 local preflight 명령 관측이며 모델 추론 latency가 아니다. Plan activation·Worker·새 S06/S07·DB/ledger 쓰기·전체 qualification·비용 비교는 수행하지 않았다.

## Evidence와 마감

모든 실행 증거는 새 run에 있으며 Git에 stage하지 않았다.

| 파일 | bytes SHA-256 |
|---|---|
| `source-before.json` | `981bb778f00962d5aae81b0fbbecfb6b32cda7909ecc61f3ea48cb0451de65a1` |
| `reused-deterministic-evidence.json` | `0b075fa9c93ae87127b7cd1a046f644599eb010f6e6209d20a65ae214c96c72c` |
| `prepare.completed.json` | `3c3b63bc3af74eb35a60d273f001f66282f3418187685e08b0ed13861788f78d` |
| `preparation-failed.json` | `8bc9a777a8802d94c583452db089085128348272bb9ee91c07213f5037023aa2` |
| `runtime-preflight/policy-01.json` | `92130b92d419c720684fc0f3413e2d99d619ddcf2cee05b2760c4d14186c49e9` |
| `runtime-preflight/inventory-01.json` | `302badd36c5036bd87f7df9d104f5ffc84930c625c67787689698a84c35f91c8` |
| `preflight-blocker-analysis.json` | `7d16ddcc5eb5e49ed09183a16face4ac9d3a43bb7ed52e95f926df4237c70af9` |
| `limited-validation-outcome.json` | `507293bdfbccc754275890fc90c889926b928bb64d7d19b24b75f3ee800deb6c` |

`prepare.stdout.log`·`prepare.stderr.log`와 `prepare.started.json`에는 원래 명령과 예외 위치를 보존했다. R19 원본 338파일·CLOSE 증거·기존 추적 파일의 before/after 보존과 최종 `git diff --check`는 새 run의 `source-after.json`에 기록한다. 본 인계 문서만 한국어 세션 커밋으로 기록하며, 커밋 이후 HEAD·branch·clean 상태·원격 HEAD·push 종료 코드는 `delivery.json` 및 최종 응답에 기록한다.

현재 실행 범위는 이 BLOCKED 인계에서 종료한다. 추가 역할 호출·후속 작업 생성·서브에이전트 위임·예약·다른 작업으로의 통지는 수행하지 않는다. 새로운 기능·Localizer 아이디어나 후속 구현 계획을 추가하지 않았다.
