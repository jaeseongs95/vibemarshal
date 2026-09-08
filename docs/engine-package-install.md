# FlowMarshal Engine 설치와 개발 평가 입력 계약

## 사용자 설치

`flowmarshal-engine` wheel은 사용자 CLI `flowmarshal-engine`과
`flowmarshal.engine` 및 필요한 canonical/time helper만 제공한다. Gate 0A~0C,
R1~R3.1 prototype, VM handoff, benchmark/qualification CLI는 wheel entrypoint가
아니며 설치된 사용자 환경에서 source checkout·형제 prototype을 탐색하지 않는다.

지원 Python은 `>=3.10`이다. 런타임 의존성은 `openai-codex==0.147.0` 및
`pydantic==2.13.5`로 고정한다. 다음은 non-editable 설치 예시다.

```powershell
python -m pip install flowmarshal_engine-0.2.0a1-py3-none-any.whl
flowmarshal-engine --help
```

기본 provider 계약은 qualification된 `v1`이다. `v2` 선택은 별도의 static 11 /
qualification 13 근거가 있을 때만 가능하며, 이 wheel 설치가 v2 채택을 뜻하지는
않는다.

Engine은 새 `.flowmarshal-engine/flowmarshal-engine.sqlite3`를 생성한다. 현재
Engine writer는 schema 4만 새로 만들며, schema 3 원장·raw receipt·history는
읽기 전용 adapter로만 열 수 있다. prototype 또는 운영 DB를 제자리 migration하지
않는다.

## 개발 평가와 재현 입력

qualification/benchmark/fixture 도구는 사용자 설치 표면이 아니다. 이를 실행할 때는
재현 bundle이 있는 checkout을 명시적으로 결속해야 한다. wheel 위치나
`Path(__file__)`의 상위 디렉터리, 형제 prototype 경로를 자동으로 사용하지 않는다.

```powershell
$env:FLOWMARSHAL_ENGINE_SOURCE_ROOT = (Resolve-Path <승인된-source-root>).Path
$env:PYTHONPATH = "$env:FLOWMARSHAL_ENGINE_SOURCE_ROOT/src"
python -m flowmarshal.engine.eval_cli run --scope deterministic `
  --project-root $env:FLOWMARSHAL_ENGINE_SOURCE_ROOT
```

재현 bundle은 해당 source root의 `config/qualification-roles.json`,
`config/qualification-finding-taxonomy.json`, `config/legacy-freeze-manifest.json`,
`tests/fixtures/`, 진단 script 및 source manifest 전체다. 실행 전에
`source_manifest_files()`/`source_manifest_digest()`가 이 입력들의 bytes digest를
기록한다. 누락된 fixture/config는 wheel에서 보완하지 않으며, 별도 새 bundle을
명시적으로 제공해야 한다.
