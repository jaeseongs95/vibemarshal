"""실제 Claude Code provider로 합성 fixture 한 Task를 governance gate와 함께 끝까지 진행한다(1회 측정).

실행(저장소 루트):
  FLOWMARSHAL_ENGINE_SOURCE_ROOT=. .venv/Scripts/python.exe spikes/governance_bridge/run_real.py <빈 출력 디렉터리>

모델 호출: steward 4회(stage 하한 general→Sonnet급 3회, deep→Opus급 1회)와 Worker 1회.
출력 디렉터리에 governance-log.jsonl, steward-calls.jsonl, run-summary.json을 남긴다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge import DEFAULT_PIN, GovernanceTaskGate, Observation  # noqa: E402
from flowmarshal.engine.domain import RunOnceAction  # noqa: E402
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare  # noqa: E402
from flowmarshal.engine.models import EngineRoleConfiguration  # noqa: E402
from flowmarshal.engine.providers import RuntimeProviderSelection, open_runtime  # noqa: E402
from flowmarshal.engine.roles import CodexStructuredRoleRunner, make_role_request  # noqa: E402
from flowmarshal.engine.runtime import EngineDispatcher  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
# spike 실행 설정: 플러그인 stage 하한(model class)마다 steward가 쓸 Claude 모델·effort.
STEWARD_MODELS = {"general": ("claude-sonnet-5", "high"), "deep": ("claude-opus-5", "high")}
DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["accept", "rationale"],
    "properties": {"accept": {"type": "boolean"}, "rationale": {"type": "string"}},
}
INSTRUCTIONS = (
    "당신은 FlowMarshal governance steward다. payload.question에 payload에 담긴 근거만으로 답한다. "
    "파일을 만들거나 고치지 않고 명령도 실행하지 않는다. 근거가 질문을 충족할 때만 accept를 true로 하고 "
    "rationale에 한국어 한두 문장으로 판단 근거를 쓴다."
)


def transcript_observation(runtime, thread_id: str, actor_id: str) -> Observation | None:
    """Claude CLI가 저장한 session 기록에서 마지막 assistant 메시지의 model·effort를 읽는다."""
    # ponytail: 제품 runtime의 저장 기록 조회(private)를 재사용한다. 제품화 때 공개 관측 API로 옮긴다.
    path = runtime._session_transcript(thread_id)
    for record in reversed(runtime._parse_transcript(path.read_bytes())):
        message = record.get("message")
        if (record.get("type") == "assistant" and record.get("isSidechain") is not True
                and isinstance(message, dict) and message.get("model") not in (None, "<synthetic>")
                and isinstance(record.get("effort"), str)):
            return Observation(message["model"], record["effort"], actor_id, f"claude-transcript:{path}")
    return None


class ClaudeSteward:
    def __init__(self, runtime, cwd: Path, log_path: Path) -> None:
        cwd.mkdir(parents=True, exist_ok=True)
        self.runtime, self.cwd, self.log_path = runtime, cwd, log_path
        self.runner = CodexStructuredRoleRunner(runtime, ephemeral_threads=False, max_schema_recovery_attempts=0)
        self.inventory = runtime.list_models()

    def review(self, stage, requirement, brief):
        model, effort = STEWARD_MODELS[(requirement or {}).get("minimumModelClass") or "general"]
        # inventory를 넘겨 model-lock v2 binding을 만든다(역할 runner가 요구한다).
        request = make_role_request(role=f"governance_steward_{stage}", instructions=INSTRUCTIONS,
                                    payload=json.loads(json.dumps(brief, default=str)), output_schema=DECISION_SCHEMA,
                                    model=model, effort=effort, inventory=self.inventory,
                                    inventory_digest=self.inventory.inventory_digest, cwd=str(self.cwd))
        started = time.monotonic()
        result = self.runner.run(request)
        receipt = result.receipt
        observation = (None if receipt.thread_id is None else transcript_observation(
            self.runtime, receipt.thread_id, f"flowmarshal-engine:steward:{stage}:{receipt.thread_id}"))
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "stage": stage, "requestedModel": model, "requestedEffort": effort,
                "seconds": round(time.monotonic() - started, 1), "status": receipt.status,
                "threadId": receipt.thread_id, "inputTokens": receipt.input_tokens,
                "cachedInputTokens": receipt.cached_input_tokens, "outputTokens": receipt.output_tokens,
                "observation": None if observation is None else observation.__dict__, "decision": result.payload,
            }, ensure_ascii=False) + "\n")
        return result.payload, observation


def main(out: Path) -> int:
    out = out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"빈 출력 디렉터리가 필요하다: {out}")
    out.mkdir(parents=True, exist_ok=True)
    workspace, fixture_digest = _copy_fixture(ROOT, out)
    (workspace / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=fm", "-c", "user.email=fm@local", "commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(workspace), *args], check=True)
    runtime = open_runtime(RuntimeProviderSelection(
        provider="claude", claude_model_catalog=str(ROOT / "config" / "claude-model-catalog.json"),
        claude_state_root=str(out / "claude-threads")))
    roles = EngineRoleConfiguration.model_validate_json(
        (ROOT / "config" / "qualification-roles.claude.json").read_text(encoding="utf-8"))
    prepared = _prepare(workspace=workspace, state_root=out / "state", inventory=runtime.list_models(), roles=roles)
    gate = GovernanceTaskGate(
        prepared.service, pin=DEFAULT_PIN, state_dir=out / "governance",
        steward=ClaudeSteward(runtime, out / "steward-cwd", out / "steward-calls.jsonl"),
        observe_worker=lambda attempt, thread: transcript_observation(runtime, thread, f"flowmarshal-engine:worker:{attempt}"))
    dispatcher = EngineDispatcher(prepared.service, runtime, task_gate=gate)
    outcomes, proposal, started = [], prepared.proposal, time.monotonic()
    try:
        while time.monotonic() - started < 1800:
            tick = time.monotonic()
            outcome = dispatcher.run_once(prepared.project_id, proposal=proposal)
            proposal = None
            outcomes.append({"action": outcome.action.value, "blocker": outcome.blocker_code,
                             "detail": (outcome.detail or "")[:600], "seconds": round(time.monotonic() - tick, 1)})
            print(json.dumps(outcomes[-1], ensure_ascii=False), flush=True)
            if outcome.action in (RunOnceAction.COMPLETED, RunOnceAction.BLOCKED):
                break
            time.sleep(3)
    finally:
        gate.close()
        runtime.close()
    with prepared.service.ledger.read() as connection:
        status = connection.execute("SELECT status FROM task_contracts WHERE id = ?", (prepared.task_id,)).fetchone()[0]
    summary = {"fixtureDigest": fixture_digest, "plugin": gate.plugin, "taskStatus": status,
               "seconds": round(time.monotonic() - started, 1), "outcomes": outcomes,
               "diff": subprocess.run(["git", "-C", str(workspace), "status", "--porcelain"], capture_output=True,
                                      text=True).stdout}
    (out / "run-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"taskStatus": status, "seconds": summary["seconds"]}, ensure_ascii=False))
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
