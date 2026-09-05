# Plan inspection v2 7차 부분 실행 기준선

고정 커밋 `ac314a8703225f5c069905d1d26dc687e703d146`의 독립 11사례 중 첫 3사례가 모두 PASS였다. 네 번째 combined 실행 중 대화의 포그라운드 세션이 중단됐고 이후 해당 진단·Codex 프로세스가 없음을 확인했다. 완료 여부를 확인하지 못했으므로 실행 전체는 FAIL이며 `collection_complete=false`, qualification은 NOT_RUN이다.

| 사례 | 결과 | 직접 관측 |
|---|---|---|
| clean | PASS | AC별 4행, scope 선택 21개, 전개된 28개 관계 차이 0, finding 없음 |
| bad | PASS | scope 선택 17개, 관계 차이 0, Task phase overclaim finding 검출 |
| wrong-goal | PASS | scope 선택 14개, 관계 차이 0, Goal oracle wrong-phase finding 검출 |
| combined | external_unknown | thread·turn receipt는 있으나 terminal·응답·usage 없음 |
| 나머지 7사례 | NOT_RUN | provider 호출하지 않음 |

이 실행의 combined는 900초 timeout으로 분류하지 않는다. 6차의 timeout과 7차의 실행 프로세스 소실은 별개 사건이다. 7차에서 확인한 세 의미 PASS를 미완료 사례에 확장하지 않는다.

| 결속 항목 | 값 |
|---|---|
| 실행 경로 | `D:\codex\fm-inspection-v2r7\.flowmarshal-engine-eval\runs\inspection-v2r7-static11-20260906` |
| workspace preflight digest | `sha256:73d0e5431ec622abd6d368fd0d355d5f39931a362d74698a7efb517c0ea42db4` |
| prepare lock digest | `sha256:c1d0c03b64cf8346c1d2784bdb1a83d35d3941bd6f0af4be1a432ae11c21b0d7` |
| summary SHA-256 | `9f351936cfcefe4b66a7934ff2639e0b76b1a655a86f8d930f58aa9c200bede9` |
| combined thread | `01a073eb-af33-7383-9202-b3cfa0165138` |
| combined turn | `01a073eb-b2ae-7892-8e83-19970cd30cc4` |
| 저장 상태 관측 | `D:\codex\fm-inspection-observations\v2r7-combined-interrupted-20260906\thread-read-observation.json` |
| 관측 SHA-256 | `dafa4b803ef57284526c7d34b3b0b75809b24a199e722fa77a733f6d314fb486` |

고정 executable을 사용한 별도 App Server의 `thread/read`는 `InvalidRequestError -32600 thread not loaded`를 반환했다. 재개나 새 모델 turn은 호출하지 않았다. 원래 raw·receipt는 유지하고, 당시 고정 source의 `verify_lock`을 통과한 후 중단 관측·combined case result·첫 summary만 추가했다. summary의 source·workspace·instruction·격리 입력·원본 보존 검사는 모두 참이다.

| 완료 호출 | input token | output token | reasoning token | latency |
|---|---:|---:|---:|---:|
| clean | 52,223 | 10,902 | 9,184 | 212,813ms |
| bad | 52,257 | 9,951 | 8,286 | 192,453ms |
| wrong-goal | 52,233 | 10,688 | 9,236 | 213,188ms |

전체 logical/provider 호출은 4/4다. 네 번째 terminal usage가 없으므로 전체 usage는 null이며 완료된 3건의 합을 전체 비용으로 표시하지 않는다. schema recovery 설정은 0회지만 불완결 aggregate receipt로 실제 전체 횟수를 확정하지 않는다. 구조 축소가 실제 비용·지연 개선을 증명하지도 않는다.

후속 작업은 7차 의미 schema·compiler·prompt·기대표를 유지하고, 진단 역할 thread의 저장과 별도 프로세스 실행을 검증하는 운영 보완이다. 기존 unknown intent를 새 실행으로 닫거나 이전 PASS를 새 계약의 checkpoint로 재사용하지 않는다.
