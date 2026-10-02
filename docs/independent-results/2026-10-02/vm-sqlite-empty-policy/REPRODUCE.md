# 재현과 publication 경계

이 폴더는 `dot/vm-sqlite-empty-policy-20261002`의 독립 정책 결정 근거다. 부모 commit은 정확히 `32bb0f9dd9f024045d24487312b50f5b703573a3`이며 제품 source/spec·구현 acceptance·GO와 분리한다. 과거 FAIL과 첫 preflight stop은 덮어쓰지 않았다.

## 기록 검증

`python validate_decision_record.py`는 canonical schema, 기록한 제약·claim·cross-exam·fresh Judge·requested model/effort·artifact hash 일관성만 검사한다. Python의 `jsonschema`가 필요하다. 실제 effective model/effort 또는 외부 source의 참을 기계적으로 증명하지 않는다.

`initial-preflight/`는 수정 전 stop 기록이다. 부모가 제공한 실제 `gpt-6.1-sol/high` Coordinator dispatch 요청 evidence로 self-selection이라는 추가 prerequisite를 제거했다. 현재 결과는 `decision-record.json`이며 이전 `provisional` 기록을 현재 verdict로 해석하지 않는다.

`judge-dossier.json`과 `identity-lifecycle.json`은 실제 Judge에 전달한 정제된 dossier와 그 관측 metadata를 byte-for-byte 보존한다. 이 dossier는 private transcript나 raw reviewer output이 아니다. `judge-dossier-portable.json`은 locator만 정리한 게시용 사본이다. `prepublication-artifact-manifest.json`은 local handoff 시점의 역사 snapshot이며 현재 게시 파일 목록은 `artifact-manifest.json`이다.

## 작은 synthetic counterexample 재현

기록된 실행은 **Python 3.12.14 / SQLite 3.53.1 / Linux**다. 재현 도구는 tar의 data extraction filter를 사용하므로 Python 3.12 이상과 base 프로젝트의 import dependency(특히 pydantic2)가 필요하다. 프로젝트 전체 suite 또는 benchmark를 실행하는 도구가 아니다.

private repository의 필요한 두 commit 객체를 확보하고 임의 checkout root를 첫 인자로 넘긴다. source의 현재 HEAD를 바꾸지 않고 pinned base src를 temporary directory에 추출해 정확한 미적용 v3 patch를 적용한다.

```sh
git fetch origin 8a159ed4b79058aba8e6302358733696b33d11ff
PYTHONDONTWRITEBYTECODE=1 python policy_counterexamples.py /path/to/vibemarshal
PYTHONDONTWRITEBYTECODE=1 python edge_counterexamples.py /path/to/vibemarshal
PYTHONDONTWRITEBYTECODE=1 python auxiliary_counterexamples.py
PYTHONDONTWRITEBYTECODE=1 python header_counterexamples.py /path/to/vibemarshal
```

실제 VM 원장 경로나 사용자 DB를 인자로 받지 않는다. DB·WAL·journal·candidate src는 TemporaryDirectory에서 생성하고 제거한다. artifact_directory도 synthetic temp root에만 생성한다.

- `policy_counterexamples.py`: 12 scope cases. normalized 원 결과 `counterexample-results.json`.
- `edge_counterexamples.py`: cache-spill/hot-journal, committed foreign WAL 2 cases. normalized 원 결과 `edge-results.json`.
- `auxiliary_counterexamples.py`: 과거 실행된 full-byte collision·secure_delete=OFF DROP SQL recipe의 게시용 script. 이 script 파일 자체는 publication 단계에 재실행하지 않았다. 관측값은 `provenance-collision.json`, `dropped-history.json`.
- `header_counterexamples.py`: 원래 reviewer의 source-derived experiment와 Coordinator readonly header 조회를 실제 package import 방식으로 재현하도록 준비한 script. **이 게시용 script는 NOT_RUN**이며 새 PASS로 합산하지 않는다. 원 normalized 관측은 `header-observations.json`, 원 방식과 한계는 `auxiliary-evidence.md`.

게시 단계에서는 source/hash/record/whitespace 검사와 원격 검증만 수행했다. counterexamples, broad suite 또는 20-run race benchmark를 반복하지 않았다.

## 결과 읽기

v3는 all logical-empty container allocation을 선택한 경우 coherent하다. 유일하거나 smallest acceptance라고 주장하지 않는다. fresh DELETE retry, hot-journal recovery, pristine WAL residue, arbitrary external logically-empty container의 범위를 VM root가 기존 권한 안에서 정의한다. 새 인간 approval gate나 marker/framework를 요구하지 않는다.

`schema_version=0`·`freelist_count=0` 추가 restriction과 recovered-size0 restriction은 coherence가 있는 **미구현·미qualification 후보**다. 현재 논리적 빈 상태는 never-used, ownership, forensic-byte preservation을 증명하지 않는다. 기존 partial schema·intact legacy/operating DB 자동 변환은 계속 금지한다.

## 원본 identity와 제외 자료

`source-inventory.json`의 commit/path/SHA-256이 정확한 source와 계약을 지정한다. AGS는 공식 commit `ba85fdcf245b9910674d88fffdd125f6f0454027`의 skill/references/canonical schema다. 실제 requested routing·admission과 advertised availability는 metadata로 남기며 effective model/effort는 null/NOT_OBSERVABLE이다.

이 폴더에는 operational DB/WAL/journal, binary fixture DB, credentials, private profiles, private transcripts 또는 raw role outputs를 넣지 않았다. 이는 포함 파일을 제한·직접 확인한 사실이지 keyword scan만으로 모든 외부 시스템의 privacy를 증명한다는 뜻은 아니다.
