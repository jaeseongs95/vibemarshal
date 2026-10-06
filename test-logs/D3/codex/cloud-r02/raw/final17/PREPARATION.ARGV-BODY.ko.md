Exact prepare argv와 body를 source-only로 고정했다. 아래 경로의 Cloud materialization·tokens 입력·output 파일은 계획이며 현재 생성/발급/실행을 뜻하지 않는다. 총괄의 유효한 발급과 최종 독립 PRE 후에만 사용한다.

Template pin: {"bytes": 71960, "sha256": "88892c55145ff1e17923f206bf788003a8dd468caede8fd610fab7e5c9079730"}
prepare-command.py pin: 3144B / 6b0eec027d07faaae75389fe11eae3c7b07bc1557b31315beaf22ec2655a7f04
cwd: /workspace/vm-d3-source-24cf-r02/pre-cwd

```sh
HOME=/workspace/vm-d3-source-24cf-r02/pre-home TMPDIR=/workspace/vm-d3-source-24cf-r02/pre-tmp TMP=/workspace/vm-d3-source-24cf-r02/pre-tmp TEMP=/workspace/vm-d3-source-24cf-r02/pre-tmp /opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 /workspace/vm-d3-source-24cf-r02/packet/prepare-command.py --template /workspace/vm-d3-source-24cf-r02/launch-preparation/D3-LAUNCH.UNISSUED.template.txt --template-sha256 88892c55145ff1e17923f206bf788003a8dd468caede8fd610fab7e5c9079730 --tokens-json /workspace/vm-d3-source-24cf-r02/issuance/issued-tokens.actual.json --output /workspace/vm-d3-source-24cf-r02/issuance/issued-command.actual.txt
```

argv와 환경은 PREPARATION.ARGV.actual.json에 각 요소로 보관했다. 전체 guard body는 GUARD-BODY.SOURCE.py이며 template here-doc의 body와 bytes가 같다. 해당 source는 미발급 token literal 상태로 AST/compile만 검사했다.

미발급 body의 정확한 4 key는 아래와 같다. null body는 실행 가능한 발급 입력이 아니며 prepare-command.py가 거절해야 한다. 각 token은 template에 정확히1회 있고 다른 정상적인 이중 밑줄 식별자를 일괄 금지하지 않는다.

```json
{
  "__DISPATCH_PEER_ID__": null,
  "__ISSUED_UTC__": null,
  "__LAUNCH_NOT_AFTER_ISO__": null,
  "__LAUNCH_NOT_AFTER_UTC__": null
}
```

__DISPATCH_PEER_ID__: 총괄이 유효하게 발급한 실제 canonical system UUID. self-made/old/callback/Cloud run ID를 대체 사용하지 않는다.
__ISSUED_UTC__: 실제 발급시각, timezone-aware UTC.
__LAUNCH_NOT_AFTER_ISO__, __LAUNCH_NOT_AFTER_UTC__: 동일한 timezone-aware UTC deadline이며 issued와의 차이는 정확히900초.

prepare는 위 frozen template SHA를 확인하고 canonical 절대 새 output에 xb로 렌더링할 뿐 권한을 발급하거나 명령을 실행하지 않는다. 정적 확인에서는 prepare 모듈 import/함수 호출/값 치환도 하지 않았다.

Guard는 같은 process에서 os.execv로 fixed runner에 연결한다. Runner argv는 fixed exe -I -S -B -X utf8, packet/runner.py, --run-root /workspace/vm-d3-run-24cf-r02, --runner-sha256 97958345d3ef7982ceb620854b56ea5ad2c91029b15b51b2076f26bf870f1cdb, --manifest-sha256 ff64e328bfc51fae2c96ebb605008d7ef2fb8fde15e4b36fd6de4d205bb6a3d0, --timeout 300이다. Runner가 child1/retry0를 소유한다. 900초 발급 창과 runner의300초 monotonic entry/wait/kill/reap budget은 별개이며 I/O hardwall을 보장하지 않는다.

발급 token JSON·prepared output·실제 dispatch 영수증·최종 PRE verdict는 이 패키지에 없다. Template와 argv/body는 완료됐으며 실행은 NOT_RUN이다.
