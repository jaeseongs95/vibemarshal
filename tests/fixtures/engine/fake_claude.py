"""테스트용 가짜 Claude Code CLI (`claude -p` stream-json 입출력 흉내).

실제 CLI 2.1.277 smoke 원문(2026-09-19, haiku)에서 확인한 형식의 최소 부분만 재현한다.

- stdin의 `user` 한 줄마다(첫 turn 포함) `system/init` → `system/thinking_tokens` →
  replay user → assistant → `result`를 낸다. 같은 프로세스의 turn마다 init이 다시 나온다.
- 성공 result의 `usage`는 turn 값이고 `iterations`가 있다. `modelUsage`는 session 누적이다.
- interrupt된 turn의 result `usage`는 모든 값이 0이고 `iterations`가 비어 있다.
- 구조화 출력은 비 replay `user`(tool_result) 이벤트를 낸다.
- `--version`은 버전 한 줄을 출력한다. `--session-id`는 새 session, `--resume`은 기존
  저장 기록이 있어야 한다.
- `--no-session-persistence`가 없으면 `$CLAUDE_CONFIG_DIR/projects/<slug>/<id>.jsonl`에
  user(promptSource)·assistant·tool_result(toolEndsTurn) 기록을 쓴다. 실제 CLI처럼 assistant
  기록에는 `--effort` 값을 top-level `effort`로 남긴다(stream 이벤트에는 없다).

prompt 지시어:

- `HANG`: replay까지만 내고 interrupt control_request를 기다린다.
- `EXIT`: replay 뒤 result 없이 프로세스를 끝낸다.
- `FAIL`: `error_during_execution` result를 낸다.
- `DENY`: 도구 거부가 있는 `success` result(`permission_denials` 비어 있지 않음)를 낸다.
- `JSON:<문서>`: 구조화 출력 값으로 `<문서>`를 쓴다(기본 `{"ok": true}`).

환경 변수로 init 관측을 바꾼다: `FAKE_CLAUDE_PERMISSION_MODE`, `FAKE_CLAUDE_INIT_MODEL`,
`FAKE_CLAUDE_INIT_SESSION`, `FAKE_CLAUDE_INIT_CWD`, `FAKE_CLAUDE_INIT_VERSION`.
`FAKE_CLAUDE_NO_REPLAY`가 있으면 replay user를 내지 않는다. `FAKE_CLAUDE_LOG`가 있으면
argv를 한 줄씩 기록한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2.1.277-fake"
INTERRUPTED_MARKER = "[Request interrupted by user]"
DENIED_TEXT = (
    "Permission for this tool use was denied. It requires approval, and this session has no "
    "approval surface — nobody can answer a permission prompt here — so it was denied automatically."
)
ZERO_USAGE = {
    "input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    "output_tokens": 0, "output_tokens_details": {"thinking_tokens": 0}, "iterations": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _emit(event: dict) -> None:
    sys.stdout.buffer.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print(f"{VERSION} (Claude Code)")
        return 0
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"argv": argv, "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
    resume = _option(argv, "--resume")
    session_id = resume or _option(argv, "--session-id") or str(uuid.uuid4())
    model = _option(argv, "--model") or "fake-model"
    effort = _option(argv, "--effort")
    permission_mode = _option(argv, "--permission-mode") or "default"
    persist = "--no-session-persistence" not in argv
    schema = _option(argv, "--json-schema")
    cwd = os.getcwd()
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    transcript = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"
    if resume is not None and not transcript.is_file():
        sys.stderr.write(f"No conversation found with session ID: {session_id}\n")
        return 1
    if resume is None and persist and transcript.is_file():
        sys.stderr.write(f"Session ID {session_id} is already in use.\n")
        return 1

    def record(document: dict) -> None:
        if not persist:
            return
        transcript.parent.mkdir(parents=True, exist_ok=True)
        base = {"sessionId": session_id, "cwd": cwd, "isSidechain": False, "timestamp": _now()}
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**base, **document}, ensure_ascii=False) + "\n")

    pending: dict | None = None
    model_usage = {"inputTokens": 0, "outputTokens": 0}
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("type") == "control_request":
            _emit({
                "type": "control_response",
                "response": {"subtype": "success", "request_id": message.get("request_id")},
            })
            if pending is not None and message.get("request", {}).get("subtype") == "interrupt":
                record({
                    "type": "user", "uuid": str(uuid.uuid4()), "promptId": pending["prompt_id"],
                    "message": {"role": "user", "content": [{"type": "text", "text": INTERRUPTED_MARKER}]},
                })
                _emit({"type": "user", "message": {"role": "user", "content": [
                    {"type": "text", "text": INTERRUPTED_MARKER}]}, "session_id": session_id,
                    "uuid": str(uuid.uuid4())})
                _emit({
                    "type": "result", "subtype": "error_during_execution", "is_error": True,
                    "terminal_reason": "aborted_streaming", "session_id": session_id,
                    "duration_ms": 5, "num_turns": 1, "usage": ZERO_USAGE,
                    "modelUsage": {model: dict(model_usage)}, "permission_denials": [],
                })
                pending = None
            continue
        if message.get("type") != "user":
            continue
        prompt = message["message"]["content"]
        turn_uuid = str(uuid.uuid4())
        prompt_id = str(uuid.uuid4())
        _emit({
            "type": "system", "subtype": "init",
            "session_id": os.environ.get("FAKE_CLAUDE_INIT_SESSION", session_id),
            "model": os.environ.get("FAKE_CLAUDE_INIT_MODEL", model),
            "permissionMode": os.environ.get("FAKE_CLAUDE_PERMISSION_MODE", permission_mode),
            "cwd": os.environ.get("FAKE_CLAUDE_INIT_CWD", cwd),
            "claude_code_version": os.environ.get("FAKE_CLAUDE_INIT_VERSION", VERSION),
            "uuid": str(uuid.uuid4()), "apiKeySource": "none",
        })
        _emit({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50,
               "estimated_tokens_delta": 50, "session_id": session_id, "uuid": str(uuid.uuid4())})
        record({
            "type": "user", "uuid": turn_uuid, "promptId": prompt_id, "promptSource": "sdk",
            "message": {"role": "user", "content": prompt},
        })
        if not os.environ.get("FAKE_CLAUDE_NO_REPLAY"):
            _emit({
                "type": "user", "message": {"role": "user", "content": prompt},
                "session_id": session_id, "parent_tool_use_id": None, "uuid": turn_uuid,
                "timestamp": _now(), "isReplay": True,
            })
        if "HANG" in prompt:
            pending = {"prompt_id": prompt_id}
            continue
        if "EXIT" in prompt:
            return 3
        usage = {
            "input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300,
            "output_tokens": 40, "output_tokens_details": {"thinking_tokens": 15},
            "iterations": [{"input_tokens": 10, "output_tokens": 40, "cache_read_input_tokens": 300,
                            "cache_creation_input_tokens": 200, "type": "message"}],
        }
        model_usage["inputTokens"] += 10
        model_usage["outputTokens"] += 40
        if "FAIL" in prompt:
            _emit({
                "type": "result", "subtype": "error_during_execution", "is_error": True,
                "terminal_reason": "model_error", "session_id": session_id, "duration_ms": 7,
                "num_turns": 1, "usage": usage, "modelUsage": {model: dict(model_usage)},
                "permission_denials": [],
            })
            continue
        denials = []
        if "DENY" in prompt:
            tool_id = "toolu_" + uuid.uuid4().hex[:20]
            tool_input = {"file_path": os.path.join(cwd, "denied.txt"), "content": "x"}
            tool_use = {"model": model, "role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tool_id, "name": "Write", "input": tool_input}]}
            record({"type": "assistant", "uuid": str(uuid.uuid4()), "effort": effort, "message": tool_use})
            _emit({"type": "assistant", "message": tool_use, "session_id": session_id,
                   "parent_tool_use_id": None})
            denied = {"role": "user", "content": [
                {"type": "tool_result", "content": DENIED_TEXT, "is_error": True, "tool_use_id": tool_id}]}
            record({"type": "user", "uuid": str(uuid.uuid4()), "promptId": prompt_id, "message": denied})
            _emit({"type": "user", "message": denied, "session_id": session_id,
                   "parent_tool_use_id": None, "uuid": str(uuid.uuid4())})
            denials = [{"tool_name": "Write", "tool_use_id": tool_id, "tool_input": tool_input}]
        structured = None
        if schema is not None:
            structured = json.loads(prompt[5:]) if prompt.startswith("JSON:") else {"ok": True}
        tool_id = "toolu_" + uuid.uuid4().hex[:20]
        content = (
            [{"type": "tool_use", "id": tool_id, "name": "StructuredOutput", "input": structured}]
            if structured is not None
            else [{"type": "text", "text": "fake answer: " + prompt}]
        )
        assistant = {"model": model, "role": "assistant", "content": content,
                     "stop_reason": "tool_use" if structured is not None else "end_turn"}
        record({"type": "assistant", "uuid": str(uuid.uuid4()), "effort": effort, "message": assistant})
        _emit({"type": "assistant", "message": assistant, "session_id": session_id,
               "parent_tool_use_id": None})
        if structured is not None:
            tool_result = {"role": "user", "content": [
                {"tool_use_id": tool_id, "type": "tool_result",
                 "content": "Structured output provided successfully"},
            ]}
            record({
                "type": "user", "uuid": str(uuid.uuid4()), "promptId": prompt_id, "toolEndsTurn": True,
                "message": tool_result,
            })
            _emit({"type": "user", "message": tool_result, "session_id": session_id,
                   "parent_tool_use_id": None, "uuid": str(uuid.uuid4())})
        result = {
            "type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
            "session_id": session_id, "duration_ms": 12, "num_turns": 1, "usage": usage,
            "modelUsage": {model: dict(model_usage)}, "permission_denials": denials,
            "result": json.dumps(structured, separators=(",", ":")) if structured is not None
            else "fake answer: " + prompt,
        }
        if structured is not None:
            result["structured_output"] = structured
        _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
