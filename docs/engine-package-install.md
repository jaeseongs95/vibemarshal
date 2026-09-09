# FlowMarshal Engine 설치와 개발 평가 입력 계약

## 사용자 설치

`flowmarshal-engine` wheel은 사용자 CLI `flowmarshal-engine`과 `flowmarshal.engine`, 필요한 canonical/time helper를 제공한다. Gate 0A~0C, R1~R3.1 prototype, VM handoff와 benchmark/qualification CLI는 사용자 entrypoint가 아니다. 설치된 사용자 환경은 source checkout이나 형제 prototype 경로를 찾아 설정을 보완하지 않는다.

지원 Python은 `>=3.10`이다. 런타임 의존성은 `openai-codex==0.147.0`과 `pydantic==2.13.5`로 고정한다. 현재 프리릴리스 wheel은 다음처럼 non-editable로 설치한다.

```powershell
python -m venv C:\absolute\flowmarshal-venv
C:\absolute\flowmarshal-venv\Scripts\python.exe -m pip install `
  C:\absolute\dist\flowmarshal_engine-0.2.0a1-py3-none-any.whl
C:\absolute\flowmarshal-venv\Scripts\flowmarshal-engine.exe --help
```

기본 provider 계약은 qualification된 `v1`이다. `v2`는 별도의 static 11과 qualification 13 근거가 있을 때만 선택할 수 있으며 wheel 설치만으로 채택되지 않는다.

## wheel에서 역할 설정 만들기

사용자 역할 설정은 wheel에 고정값으로 포함하지 않는다. `config init`에 model/effort를 명시하면 CLI가 현재 Codex `model/list`를 읽어 지원 여부를 검사하고 user-owned JSON을 만든다. source checkout의 `config/qualification-roles.json`은 개발 qualification 입력이며 사용자 bootstrap에 필요하지 않다.

```powershell
$Cli = "C:\absolute\flowmarshal-venv\Scripts\flowmarshal-engine.exe"
$ProjectRoot = (Resolve-Path "C:\absolute\my-project").Path
$StateRoot = (New-Item -ItemType Directory -Force (Join-Path $ProjectRoot ".flowmarshal-engine")).FullName
$Database = Join-Path $StateRoot "flowmarshal-engine.sqlite3"
$Artifacts = Join-Path $StateRoot "artifacts"
$RoleConfig = Join-Path $StateRoot "roles.json"

& $Cli config init `
  --output $RoleConfig `
  --model "<model-id>" `
  --effort high `
  --role "validator=<validator-model-id>:<effort>"

& $Cli --db $Database --artifacts $Artifacts project init `
  --name "my-project" `
  --root $ProjectRoot
```

`--role`은 선택 사항이며 지정하지 않은 역할에는 사용자가 `--model`과 `--effort`로 준 값이 들어간다. 같은 역할 override를 두 번 지정하거나 지원되지 않는 binding을 쓰면 파일을 만들지 않는다. 기존 출력 파일도 덮어쓰지 않는다. 생성된 파일은 이후 `prepare`와 `run-once`에 같은 절대 경로로 전달한다.

관측 token 정책도 사용자 bootstrap의 필수 파일이 아니다. 설정하지 않아도 GoalAuthorization에 기록된 provider 호출 수와 절대 deadline은 hard stop으로 적용된다. 사용자가 `project budget set`을 선택하면 그 JSON은 사용자 설정으로 따로 관리하며, 관측된 token만 사용하는 best-effort 중단 정책으로 해석한다. `call_reservation_tokens`는 선택적인 deprecated 호환 필드이며 admission·요금·구독 한도 계산에는 쓰지 않는다. 기존 기록의 숫자는 읽되 새 의미로 바꾸지 않는다.

Engine은 별도 `--db`가 없으면 현재 작업 디렉터리의 `.flowmarshal-engine/flowmarshal-engine.sqlite3`를 새로 만든다. 운영과 scheduler에서는 cwd에 따라 원장이 달라지지 않도록 `--db`와 `--artifacts`에 절대 경로를 준다. schema 4 writer는 schema 3 원장이나 prototype 운영 DB를 제자리 migration하지 않으며, 과거 schema 3 자료는 별도의 읽기 전용 inspector로만 확인한다.

## clean 설치 smoke

candidate wheel은 빈 venv와 빈 임시 프로젝트에서 한 번 완전히 검사한다. 다음 항목이 최소 smoke 범위다.

1. wheel을 non-editable로 설치하고 `pip check`를 통과한다.
2. `flowmarshal-engine --help`와 `flowmarshal-engine config init --help`가 source checkout 없이 동작한다.
3. 실제 계정의 명시적 model/effort로 `config init`을 실행해 inventory 검증된 역할 JSON을 만든다.
4. 절대 DB·artifact 경로로 `project init`과 `project show`를 실행한다.
5. 설치된 `flowmarshal` import 경로가 venv의 `site-packages`인지 확인한다.

qualification에서 사용한 candidate wheel bytes가 바뀌면 clean 설치 근거도 영향 범위에 맞게 다시 확인한다. 같은 wheel을 이름만 바꿔 재검증한 결과로 새로운 candidate를 증명하지 않는다.

## 개발 평가와 재현 입력

qualification, benchmark와 fixture 도구는 사용자 설치 표면이 아니다. 이를 실행할 때는 재현 bundle이 있는 checkout과 candidate wheel이 설치된 격리 Python을 각각 명시한다. launcher는 개발 전용 module만 checkout에서 읽고, `flowmarshal` 제품 module은 격리 Python에 설치된 wheel에서 import한다. wheel 위치, `Path(__file__)`의 상위 디렉터리나 형제 prototype 경로를 자동으로 사용하지 않는다.

```powershell
$SourceRoot = (Resolve-Path <승인된-source-root>).Path
$CandidatePython = "C:\absolute\candidate-venv\Scripts\python.exe"

& $CandidatePython "$SourceRoot\scripts\installed_candidate_qualification.py" `
  --source-root $SourceRoot `
  --candidate-wheel C:\absolute\dist\flowmarshal_engine-0.2.0a1-py3-none-any.whl `
  probe

& $CandidatePython "$SourceRoot\scripts\installed_candidate_qualification.py" `
  --source-root $SourceRoot `
  --candidate-wheel C:\absolute\dist\flowmarshal_engine-0.2.0a1-py3-none-any.whl `
  eval run --scope deterministic `
  --project-root $SourceRoot
```

`probe`는 실제 evaluation을 시작하지 않고 developer harness import와 현재 non-editable wheel의 distribution·version·import root·package bytes 결속만 검사한다. release `project-e2e`는 같은 launcher의 candidate wheel 경로를 내부 eval 명령에 결속해 evaluation contract·run metadata·evidence에 저장한다. source Python에서 직접 `flowmarshal.engine.eval_cli`를 실행한 결과는 진단에는 사용할 수 있지만 candidate wheel의 release PASS가 아니다.

재현 bundle은 해당 source root의 `config/qualification-roles.json`, `config/qualification-finding-taxonomy.json`, `config/legacy-freeze-manifest.json`, `tests/fixtures/`, 진단 script와 source manifest 전체다. 실행 전에 `source_manifest_files()`와 `source_manifest_digest()`가 입력 bytes digest를 기록한다. 누락된 fixture/config를 wheel에서 보완하지 않으며, 별도 bundle을 명시적으로 제공해야 한다.

사용자 bootstrap 설정과 qualification 설정은 역할이 다르다. 전자는 현재 사용자가 선택하고 inventory로 검증한 운영 입력이고, 후자는 동결된 평가를 재현하는 source-bound 입력이다.
