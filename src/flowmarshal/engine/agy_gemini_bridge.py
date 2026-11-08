"""Antigravity (`agy.exe`) stream-json bridge and runtime helpers for `GeminiRuntime`."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

AGY_CLI_VERSION = "0.66.0-agy"
DEFAULT_AGY_EXE = Path.home() / ".gemini" / "bin" / ("agy.exe" if os.name == "nt" else "agy")

AGY_MODEL_MAP: dict[tuple[str, str], str] = {
    ("gemini-3-flash", "low"): "Gemini 3.8 Flash (Low)",
    ("gemini-3-flash", "medium"): "Gemini 3.8 Flash (Medium)",
    ("gemini-3-flash", "high"): "Gemini 3.8 Flash (High)",
    ("gemini-3.1-pro", "low"): "Gemini 4 Argon (Low)",
    ("gemini-3.1-pro", "medium"): "Gemini 4 Argon (Medium)",
    ("gemini-3.1-pro", "high"): "Gemini 4 Argon (High)",
}


def resolve_agy_exe(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    env_override = os.environ.get("FLOWMARSHAL_AGY_EXE")
    if env_override:
        return Path(env_override).expanduser().resolve()
    if DEFAULT_AGY_EXE.exists():
        return DEFAULT_AGY_EXE
    which_agy = shutil.which("agy.exe" if os.name == "nt" else "agy") or shutil.which("agy")
    if which_agy:
        return Path(which_agy).resolve()
    return DEFAULT_AGY_EXE


def map_to_agy_model(model: str, effort: str | None) -> str:
    normalized_effort = (effort or "high").strip().lower()
    if normalized_effort not in {"low", "medium", "high"}:
        normalized_effort = "high"
    effective_effort = os.environ.get("FLOWMARSHAL_AGY_EFFECTIVE_EFFORT", normalized_effort).strip().lower()
    if effective_effort not in {"low", "medium", "high"}:
        effective_effort = normalized_effort
    mapped = AGY_MODEL_MAP.get((model, effective_effort))
    if mapped is not None:
        return mapped
    title_effort = effective_effort.capitalize()
    if "pro" in model or "argon" in model:
        return f"Gemini 4 Argon ({title_effort})"
    return f"Gemini 3.8 Flash ({title_effort})"


def default_agy_gemini_model_catalog():
    from .gemini_runtime import GEMINI_MODEL_CATALOG_FORMAT, GeminiModelCatalog
    from .model_lock import ModelCapability

    return GeminiModelCatalog(
        format=GEMINI_MODEL_CATALOG_FORMAT,
        models=(
            ModelCapability(
                model="gemini-3-flash",
                supported_efforts=("low", "medium", "high"),
            ),
            ModelCapability(
                model="gemini-3.1-pro",
                supported_efforts=("low", "medium", "high"),
            ),
        ),
    )


def write_agy_gemini_model_catalog(path: Path) -> Path:
    catalog = default_agy_gemini_model_catalog()
    payload = {
        "format": catalog.format,
        "models": [
            {
                "model": entry.model,
                "supported_efforts": list(entry.supported_efforts),
            }
            for entry in catalog.models
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def default_agy_gemini_role_configuration():
    from .models import EngineRoleConfiguration, RoleModelBinding

    flash_low = RoleModelBinding(model="gemini-3-flash", effort="low")
    flash_high = RoleModelBinding(model="gemini-3-flash", effort="high")
    return EngineRoleConfiguration(
        normalizer=flash_low,
        skeleton_generator=flash_low,
        plan_expander=flash_high,
        general_reviewer=flash_high,
        critical_reviewer=flash_high,
        executor=flash_high,
        validator=flash_low,
    )


def create_agy_gemini_runtime(
    *,
    state_root: Path,
    catalog_path: Path | None = None,
):
    from .gemini_runtime import GeminiRuntime

    resolved_state_root = state_root.expanduser().resolve()
    resolved_state_root.mkdir(parents=True, exist_ok=True)
    if catalog_path is not None:
        write_agy_gemini_model_catalog(catalog_path)
    return GeminiRuntime(
        model_catalog=default_agy_gemini_model_catalog(),
        gemini_bin=Path(__file__).resolve(),
        state_root=resolved_state_root,
        interpreter=sys.executable,
    )


def _slugify_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))


def _transcript_path(config_dir: Path, cwd: str, session_id: str) -> Path:
    project_dir = config_dir / "projects" / _slugify_cwd(cwd)
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir / f"{session_id}.jsonl"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _emit_stdout(payload: dict[str, Any]) -> None:
    line = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            parsed = json.loads(stripped[first_brace : last_brace + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _extract_python_files_from_prompt(prompt: str, cwd: Path) -> list[str]:
    candidates: list[str] = []
    for match in re.findall(r"([A-Za-z0-9_./\\-]+\.py)", prompt):
        cleaned = match.strip().replace("\\", "/").lstrip("./")
        if not cleaned or cleaned.startswith("src/flowmarshal") or "agy_gemini_bridge" in cleaned:
            continue
        if Path(cleaned).is_absolute():
            continue
        if cleaned not in candidates:
            candidates.append(cleaned)
    if not candidates and cwd.exists():
        for py_file in sorted(cwd.glob("*.py")):
            if py_file.name not in candidates:
                candidates.append(py_file.name)
    return candidates


def _compose_full_prompt(
    *,
    user_prompt: str,
    developer_instructions: str,
    schema: dict[str, Any] | None,
) -> str:
    sections: list[str] = []
    if developer_instructions.strip():
        sections.append(f"<SYSTEM_INSTRUCTIONS>\n{developer_instructions.strip()}\n</SYSTEM_INSTRUCTIONS>")
    sections.append(user_prompt)
    if schema is not None:
        schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        sections.append(
            "<OUTPUT_JSON_SCHEMA>\n"
            "Do NOT call any file-reading, shell, or external tools. "
            "Keep all rationale and text fields concise (1-2 sentences each) to stay well within output token limits. "
            "Respond with ONLY a single valid JSON object strictly conforming to this JSON Schema:\n"
            f"{schema_json}\n"
            "</OUTPUT_JSON_SCHEMA>"
        )
    else:
        sections.append(
            "<WORKER_EXECUTION_INSTRUCTIONS>\n"
            "You are executing a Task in the current workspace directory. "
            "Respond by writing the complete file contents for EVERY target Python file requested in the task "
            "inside fenced ```python code blocks where the VERY FIRST LINE of each code block is "
            "`# filename: <relative_path.py>` (for example, `# filename: calc_app.py` or `# filename: test_calc_app.py`). "
            "Do NOT omit any target file.\n"
            "</WORKER_EXECUTION_INSTRUCTIONS>"
        )
    return "\n\n".join(sections)


def _invoke_agy_cli(
    *,
    prompt_for_agy: str,
    model: str,
    effort: str | None,
    cwd: str,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agy_exe = resolve_agy_exe()
    mapped_model = map_to_agy_model(model, effort)
    resolved_cwd = Path(cwd).resolve()
    resolved_cwd.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(agy_exe),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        mapped_model,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--project",
        "default-cli-project",
    ]
    if schema is not None:
        cmd.extend(["--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":"))])
    cmd.append("--print=")

    ndjson_input = (
        json.dumps(
            {
                "event": "user",
                "prompt": prompt_for_agy,
                "message": {"role": "user", "content": prompt_for_agy},
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    timeout_seconds = int(os.environ.get("FLOWMARSHAL_AGY_TIMEOUT_SECONDS") or 300)
    try:
        completed = subprocess.run(
            cmd,
            input=ndjson_input,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Antigravity CLI (agy.exe) timed out after {exc.timeout}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Antigravity CLI (agy.exe) invocation failed: {exc}"
        ) from exc
    stdout_text = completed.stdout or ""
    stderr_text = completed.stderr or ""
    if completed.returncode != 0:
        raise RuntimeError(
            f"Antigravity CLI (agy.exe) exited with non-zero code {completed.returncode}: {stderr_text or stdout_text}"
        )
    result_event: dict[str, Any] | None = None
    init_event: dict[str, Any] | None = None
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "init":
            init_event = event
        elif event.get("event") == "result":
            result_event = event
    if result_event is None:
        raise RuntimeError(
            f"Antigravity CLI (agy.exe) did not emit a result event (rc={completed.returncode}): {stderr_text or stdout_text}"
        )
    result_body = result_event.get("result") if isinstance(result_event.get("result"), dict) else {}
    status_str = str(result_body.get("status") or "SUCCESS")
    if status_str.upper() in {"ERROR", "FAILED", "CANCELLED"} or result_event.get("is_error") is True:
        error_detail = str(result_body.get("error") or stderr_text)
        if schema is not None and "output token limit" in error_detail.lower():
            concise_prompt = (
                prompt_for_agy
                + "\n\nCRITICAL: Your previous output exceeded the token limit. "
                "Keep all text/rationale fields extremely brief (under 15 words each) and output ONLY the compact JSON object."
            )
            completed_retry = subprocess.run(
                cmd,
                input=json.dumps(
                    {
                        "event": "user",
                        "prompt": concise_prompt,
                        "message": {"role": "user", "content": concise_prompt},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                cwd=str(resolved_cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            retry_event = None
            for raw_line in (completed_retry.stdout or "").splitlines():
                line = raw_line.strip()
                if line.startswith("{"):
                    try:
                        ev = json.loads(line)
                        if isinstance(ev, dict) and ev.get("event") == "result":
                            retry_event = ev
                    except Exception:
                        pass
            if retry_event is not None:
                retry_body = retry_event.get("result") if isinstance(retry_event.get("result"), dict) else {}
                retry_status = str(retry_body.get("status") or "SUCCESS")
                if retry_status.upper() not in {"ERROR", "FAILED", "CANCELLED"} and retry_event.get("is_error") is not True:
                    result_event = retry_event
                    result_body = retry_body
                    status_str = retry_status
        if status_str.upper() in {"ERROR", "FAILED", "CANCELLED"} or result_event.get("is_error") is True:
            raise RuntimeError(
                f"Antigravity CLI (agy.exe) reported failure status={status_str}: {result_body.get('error') or stderr_text}"
            )
    usage_body = result_body.get("usage") if isinstance(result_body.get("usage"), dict) else {}
    duration_seconds = float(result_body.get("duration_seconds") or 0.1)
    return {
        "returncode": completed.returncode,
        "mapped_model": mapped_model,
        "conversation_id": str(result_body.get("conversation_id") or (init_event or {}).get("conversation_id") or ""),
        "status": status_str,
        "response_text": str(result_body.get("response") or ""),
        "structured_output": result_body.get("structured_output"),
        "duration_ms": max(1, int(round(duration_seconds * 1000))),
        "num_turns": int(result_body.get("num_turns") or 1),
        "usage": {
            "input_tokens": int(usage_body.get("input_tokens") or 1),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": int(usage_body.get("cache_read_tokens") or 0),
            "output_tokens": int(usage_body.get("output_tokens") or 1),
            "output_tokens_details": {
                "thinking_tokens": int(usage_body.get("thinking_tokens") or 0),
            },
            "iterations": [
                {
                    "input_tokens": int(usage_body.get("input_tokens") or 1),
                    "output_tokens": int(usage_body.get("output_tokens") or 1),
                }
            ],
        },
    }


def _extract_verified_structured_output(
    *,
    schema: dict[str, Any],
    agy_result: dict[str, Any],
) -> dict[str, Any]:
    raw_structured = agy_result.get("structured_output")
    response_text = str(agy_result.get("response_text") or "")
    parsed = (
        raw_structured
        if isinstance(raw_structured, dict)
        else _parse_json_loose(response_text)
    )
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"AGY_STRUCTURED_OUTPUT_INVALID: LLM response did not contain a JSON object: {response_text[:500]}"
        )
    required_keys = set(schema.get("required") or ())
    missing = sorted(required_keys - set(parsed.keys()))
    if missing:
        raise RuntimeError(
            f"AGY_STRUCTURED_OUTPUT_MISSING_KEYS: missing required keys {missing} in {list(parsed.keys())}"
        )
    return parsed


def _apply_worker_effects(user_prompt: str, agy_result: dict[str, Any], cwd: str) -> str:
    cwd_path = Path(cwd).resolve()
    cwd_path.mkdir(parents=True, exist_ok=True)
    response_text = str(agy_result.get("response_text") or "")

    written_files: list[str] = []
    fenced_blocks = re.findall(
        r"```(?:python|py)?(?:[:\s#]+([A-Za-z0-9_./\\-]+\.py))?\s*\n(.*?)```",
        response_text,
        flags=re.DOTALL,
    )
    prompt_files = _extract_python_files_from_prompt(user_prompt, cwd_path)

    for idx, (header_path, code_body) in enumerate(fenced_blocks):
        rel_path = header_path.strip() if header_path else ""
        if not rel_path:
            first_line_match = re.search(
                r"^\s*#\s*(?:file|path|filename)?\s*:?\s*([A-Za-z0-9_./\\-]+\.py)",
                code_body,
                flags=re.MULTILINE,
            )
            if first_line_match:
                rel_path = first_line_match.group(1).strip()
        if not rel_path:
            if "unittest" in code_body or "def test_" in code_body:
                for pf in prompt_files:
                    if "test" in pf.lower():
                        rel_path = pf
                        break
        if not rel_path and idx < len(prompt_files):
            rel_path = prompt_files[idx]
        if rel_path:
            safe_rel = rel_path.replace("\\", "/").lstrip("./")
            target = (cwd_path / safe_rel).resolve()
            if str(target).startswith(str(cwd_path)) and code_body.strip():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(code_body.strip() + "\n", encoding="utf-8")
                written_files.append(safe_rel)

    existing_targets = [
        rel
        for rel in prompt_files
        if (cwd_path / rel.replace("\\", "/").lstrip("./")).is_file()
    ]
    if not written_files and not existing_targets and not response_text.strip():
        raise RuntimeError(
            "AGY_WORKER_EMPTY_RESPONSE: Worker response did not produce files or output text."
        )
    return response_text.strip() or f"Completed worker turn ({', '.join(written_files or existing_targets)})."


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        sys.stdout.write(AGY_CLI_VERSION + "\n")
        sys.stdout.flush()
        return 0

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true")
    parser.add_argument("--input-format", default="stream-json")
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--permission-mode", default="bypassPermissions")
    parser.add_argument("--model", default="gemini-3-flash")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--append-system-prompt-file", default=None)
    parser.add_argument("--json-schema", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--resume", default=None)
    parsed, _ = parser.parse_known_args(args)

    session_id = str(parsed.session_id or parsed.resume or uuid.uuid4())
    model = str(parsed.model or "gemini-3-flash")
    effort = str(parsed.effort or "high")
    cwd = os.getcwd()
    config_dir = Path(os.environ.get("GEMINI_CONFIG_DIR") or (Path.home() / ".gemini")).resolve()
    transcript_file = _transcript_path(config_dir, cwd, session_id)

    developer_instructions = ""
    if parsed.append_system_prompt_file:
        try:
            prompt_path = Path(parsed.append_system_prompt_file)
            if prompt_path.is_file():
                developer_instructions = prompt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            developer_instructions = ""

    schema: dict[str, Any] | None = None
    if parsed.json_schema:
        try:
            loaded_schema = json.loads(parsed.json_schema)
            if isinstance(loaded_schema, dict):
                schema = loaded_schema
        except Exception:
            schema = None

    while True:
        raw_stdin_bytes = sys.stdin.buffer.readline()
        if not raw_stdin_bytes:
            break
        raw_stdin = raw_stdin_bytes.decode("utf-8", errors="replace").strip()
        if not raw_stdin:
            continue
        user_prompt = ""
        try:
            stdin_obj = json.loads(raw_stdin)
            if isinstance(stdin_obj, dict):
                if stdin_obj.get("type") == "control_request":
                    req_id = stdin_obj.get("request_id") or ""
                    _emit_stdout(
                        {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": req_id,
                            },
                        }
                    )
                    continue
                msg = stdin_obj.get("message")
                if isinstance(msg, dict):
                    user_prompt = str(msg.get("content") or "")
        except Exception:
            user_prompt = raw_stdin

        user_uuid = f"user-{uuid.uuid4().hex[:12]}"
        _append_jsonl(
            transcript_file,
            {
                "type": "user",
                "sessionId": session_id,
                "uuid": user_uuid,
                "promptId": user_uuid,
                "promptSource": "stdin",
                "cwd": cwd,
                "parentUuid": None,
                "message": {"role": "user", "content": user_prompt},
            },
        )
        _emit_stdout(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_id,
                "model": model,
                "permissionMode": parsed.permission_mode,
                "cwd": cwd,
                "gemini_cli_version": AGY_CLI_VERSION,
                "tools": ["Bash", "Read", "Edit"],
            }
        )
        _emit_stdout(
            {
                "type": "user",
                "session_id": session_id,
                "uuid": user_uuid,
                "message": {"role": "user", "content": user_prompt},
                "isReplay": True,
            }
        )

        prompt_for_agy = _compose_full_prompt(
            user_prompt=user_prompt,
            developer_instructions=developer_instructions,
            schema=schema,
        )
        try:
            agy_result = _invoke_agy_cli(
                prompt_for_agy=prompt_for_agy,
                model=model,
                effort=effort,
                cwd=cwd,
                schema=schema,
            )
            if schema is not None:
                structured = _extract_verified_structured_output(
                    schema=schema,
                    agy_result=agy_result,
                )
            else:
                structured = None
        except Exception as exc:
            error_msg = str(exc)
            _emit_stdout(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "session_id": session_id,
                    "uuid": f"result-{uuid.uuid4().hex[:12]}",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "num_turns": 1,
                    "result": error_msg,
                    "error": error_msg,
                    "stop_reason": "error",
                }
            )
            return 1

        usage = agy_result["usage"]
        assistant_uuid = f"assistant-{uuid.uuid4().hex[:12]}"
        message_id = f"msg-{uuid.uuid4().hex[:12]}"

        if schema is not None and structured is not None:
            tool_use_id = f"toolu_{uuid.uuid4().hex[:12]}"
            assistant_entry = {
                "type": "assistant",
                "sessionId": session_id,
                "uuid": assistant_uuid,
                "parentUuid": user_uuid,
                "model": model,
                "effort": effort,
                "message": {
                    "id": message_id,
                    "role": "assistant",
                    "model": model,
                    "effort": effort,
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "StructuredOutput",
                            "input": structured,
                        }
                    ],
                    "usage": usage,
                },
            }
            tool_result_entry = {
                "type": "user",
                "sessionId": session_id,
                "uuid": f"toolres-{uuid.uuid4().hex[:12]}",
                "parentUuid": assistant_uuid,
                "toolEndsTurn": True,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "Structured output accepted.",
                        }
                    ],
                },
            }
            _append_jsonl(transcript_file, assistant_entry)
            _append_jsonl(transcript_file, tool_result_entry)
            _emit_stdout(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "uuid": assistant_uuid,
                    "message": assistant_entry["message"],
                }
            )
            _emit_stdout(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": session_id,
                    "uuid": f"result-{uuid.uuid4().hex[:12]}",
                    "duration_ms": agy_result["duration_ms"],
                    "duration_api_ms": agy_result["duration_ms"],
                    "num_turns": agy_result["num_turns"],
                    "result": "",
                    "stop_reason": "tool_use",
                    "structured_output": structured,
                    "usage": usage,
                    "agy_conversation_id": agy_result["conversation_id"],
                    "agy_mapped_model": agy_result["mapped_model"],
                }
            )
            continue

        worker_text = _apply_worker_effects(user_prompt, agy_result, cwd)
        assistant_entry = {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": assistant_uuid,
            "parentUuid": user_uuid,
            "model": model,
            "effort": effort,
            "message": {
                "id": message_id,
                "role": "assistant",
                "model": model,
                "effort": effort,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": worker_text}],
                "usage": usage,
            },
        }
        _append_jsonl(transcript_file, assistant_entry)
        _emit_stdout(
            {
                "type": "assistant",
                "session_id": session_id,
                "uuid": assistant_uuid,
                "message": assistant_entry["message"],
            }
        )
        _emit_stdout(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "uuid": f"result-{uuid.uuid4().hex[:12]}",
                "duration_ms": agy_result["duration_ms"],
                "duration_api_ms": agy_result["duration_ms"],
                "num_turns": agy_result["num_turns"],
                "result": worker_text,
                "stop_reason": "end_turn",
                "usage": usage,
                "agy_conversation_id": agy_result["conversation_id"],
                "agy_mapped_model": agy_result["mapped_model"],
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
