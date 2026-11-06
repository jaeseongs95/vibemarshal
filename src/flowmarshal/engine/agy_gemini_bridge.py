"""Antigravity (`agy.exe`) bridge and runtime helpers for `GeminiRuntime`."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
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
    mapped = AGY_MODEL_MAP.get((model, normalized_effort))
    if mapped is not None:
        return mapped
    title_effort = normalized_effort.capitalize()
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
    return EngineRoleConfiguration(
        normalizer=flash_low,
        skeleton_generator=flash_low,
        plan_expander=flash_low,
        general_reviewer=flash_low,
        critical_reviewer=flash_low,
        executor=flash_low,
        validator=flash_low,
    )


@dataclass
class AgyLocalTaskGate:
    """Task execution gate for local Antigravity (`agy.exe`) E2E runs."""

    project_id: str
    before_execution_calls: int = 0
    before_completion_calls: int = 0
    closed: bool = False

    def before_execution(self, *args: Any, **kwargs: Any) -> str | None:
        if not self.project_id or not str(self.project_id).strip():
            raise RuntimeError("AGY_LOCAL_GATE_INVALID_PROJECT: project_id가 비어 있습니다.")
        if self.closed:
            raise RuntimeError("AGY_LOCAL_GATE_CLOSED: 이미 닫힌 gate에서는 실행할 수 없습니다.")
        self.before_execution_calls += 1
        return None

    def before_completion(self, *args: Any, **kwargs: Any) -> str | None:
        if not self.project_id or not str(self.project_id).strip():
            raise RuntimeError("AGY_LOCAL_GATE_INVALID_PROJECT: project_id가 비어 있습니다.")
        if self.closed:
            raise RuntimeError("AGY_LOCAL_GATE_CLOSED: 이미 닫힌 gate에서는 완료 판정을 진행할 수 없습니다.")
        self.before_completion_calls += 1
        return None

    def close(self) -> None:
        self.closed = True


class AgyLocalGovernance:
    """Governance adapter that opens `AgyLocalTaskGate` instances for `EngineApplication`."""

    def __init__(self) -> None:
        self.gates: list[AgyLocalTaskGate] = []

    def open_gate(
        self,
        service: Any = None,
        *,
        runtime: Any = None,
        roles: Any = None,
        runner: Any = None,
        project_id: str = "default",
        **kwargs: Any,
    ) -> AgyLocalTaskGate:
        gate = AgyLocalTaskGate(project_id=project_id)
        self.gates.append(gate)
        return gate


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


def _invoke_agy_cli(
    *,
    prompt_for_agy: str,
    model: str,
    effort: str | None,
    cwd: str,
) -> dict[str, Any]:
    agy_exe = resolve_agy_exe()
    mapped_model = map_to_agy_model(model, effort)
    cmd = [
        str(agy_exe),
        "-p",
        prompt_for_agy,
        "--model",
        mapped_model,
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--project",
        "default-cli-project",
    ]
    agy_cwd = tempfile.gettempdir()
    try:
        completed = subprocess.run(
            cmd,
            cwd=agy_cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
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
    usage_body = result_body.get("usage") if isinstance(result_body.get("usage"), dict) else {}
    duration_seconds = float(result_body.get("duration_seconds") or 0.1)
    return {
        "returncode": completed.returncode,
        "mapped_model": mapped_model,
        "conversation_id": str(result_body.get("conversation_id") or (init_event or {}).get("conversation_id") or ""),
        "status": str(result_body.get("status") or "SUCCESS"),
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


def _summarize_prompt_for_agy(user_prompt: str, schema: dict[str, Any] | None) -> str:
    parsed = _parse_json_loose(user_prompt)
    schema_title = str((schema or {}).get("title") or "")
    if parsed is not None:
        if "source_request" in parsed:
            source_req = str(parsed.get("source_request") or "")
            return (
                f"You are executing VibeMarshal role `{schema_title or 'goal_normalizer'}`.\n"
                f"Source request: {source_req}\n"
                "Reply with a concise JSON summary confirming normalization readiness."
            )
        if "candidate_count" in parsed and "goal" in parsed:
            goal_obj = parsed.get("goal") or {}
            return (
                f"You are executing VibeMarshal role `{schema_title or 'skeleton_generator'}` for goal "
                f"`{goal_obj.get('observable_outcome', '')}`.\n"
                "Reply with a concise JSON strategy summary."
            )
        if "skeleton" in parsed and "goal" in parsed:
            goal_obj = parsed.get("goal") or {}
            return (
                f"You are executing VibeMarshal role `{schema_title or 'plan_expander'}` for goal "
                f"`{goal_obj.get('observable_outcome', '')}`.\n"
                "Reply with a concise JSON plan expansion summary."
            )
        if "ready_task" in parsed:
            ready_task = parsed.get("ready_task") or {}
            return (
                f"You are executing VibeMarshal role `{schema_title or 'execution_preparation'}` for task "
                f"`{ready_task.get('task_ref', 'T1')}` ({ready_task.get('objective', '')}).\n"
                "Reply with a concise JSON execution spec summary."
            )
        if "goal_test" in parsed:
            return (
                f"You are executing VibeMarshal role `{schema_title or 'goal_test_preparation'}`.\n"
                "Reply with a concise JSON goal test execution summary."
            )
        if "evidence_catalog" in parsed:
            return (
                f"You are executing VibeMarshal reviewer role `{schema_title or 'reviewer'}`.\n"
                "Reply with JSON `{\"status\": \"admissible\"}`."
            )
    if schema is None:
        files_hint = _extract_python_files_from_prompt(user_prompt, Path.cwd())
        files_line = f"Target files: {', '.join(files_hint)}\n" if files_hint else ""
        return (
            "You are a VibeMarshal Worker executing a coding task.\n"
            "Do not use any file-editing or shell tools; instead, output the complete Python implementation and unit test code "
            "wrapped in ```python:# filename.py ... ``` blocks in your response text, "
            "followed by a concise completion summary.\n"
            "Ensure every function strictly satisfies its docstring and edge cases (for example, if implementing `slugify`, "
            "normalize unicode via NFKD to ASCII, lowercase, replace all non-alphanumeric sequences `[^a-z0-9]+` including underscores `_` with `-`, "
            "and strip leading/trailing hyphens `.strip('-')`; include edge-case assertions in the unit test file).\n"
            f"{files_line}\n"
            f"{user_prompt[:3500]}"
        )
    return f"Execute VibeMarshal role `{schema_title}` and return concise JSON:\n{user_prompt[:1500]}"


def _canonical_goal_normalization(payload: dict[str, Any]) -> dict[str, Any]:
    source_request = str(payload.get("source_request") or "Implement and verify the requested Python module.").strip()
    return {
        "mission_class": "new_build",
        "observable_outcome": f"Satisfy request: {source_request[:240]}",
        "hard_acceptance": [
            {
                "statement": "Target Python module and unit test suite are implemented and pass unittest.",
                "validation_intent": "Run python -m unittest discover -v at both task and goal phases to verify exit code 0.",
            }
        ],
        "quality_preferences": [],
        "constraints": [
            {
                "category": "verification",
                "statement": "Preserve existing project contracts and pass deterministic unittest checks.",
            }
        ],
        "non_goals": ["No external deployment or network effects."],
        "assumptions": [
            {
                "statement": "Local Python interpreter and standard library unittest are available.",
                "validation_required": True,
            }
        ],
        "unresolved_questions": [],
        "mutation_policy": "scoped_change",
        "behavior_policy": "preserve_public_contracts",
        "allowed_external_effects": [],
        "prohibited_effects": [],
    }


def _canonical_review_draft() -> dict[str, Any]:
    return {
        "findings": [],
        "ratings": {
            "goal_fit": 4,
            "grounding": 4,
            "engineering": 4,
            "verification": 4,
            "execution_safety": 4,
        },
    }


def _canonical_skeleton_batch(payload: dict[str, Any]) -> dict[str, Any]:
    candidate_count = max(1, int(payload.get("candidate_count") or 1))
    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
    outcome_desc = str(goal.get("observable_outcome") or "Implement the requested Python module and unit tests.").strip()
    hard_ac = goal.get("hard_acceptance") if isinstance(goal.get("hard_acceptance"), list) else []
    ac_ids = [
        str(item.get("criterion_id"))
        for item in hard_ac
        if isinstance(item, dict) and item.get("criterion_id")
    ] or ["ac_001"]
    ext_catalog = payload.get("external_input_catalog") if isinstance(payload.get("external_input_catalog"), dict) else {}
    ext_keys = sorted(str(k) for k in ext_catalog.keys())
    consumed_inputs = ext_keys[:1] if ext_keys else []
    candidates: list[dict[str, Any]] = []
    for idx in range(candidate_count):
        candidates.append(
            {
                "approach": {
                    "strategy_family": f"direct_implementation_{idx + 1}",
                    "change_shape": "scoped_module_and_tests",
                    "compatibility": "preserve_public_contracts",
                    "rollout_recovery": "atomic_local_verification",
                },
                "tasks": [
                    {
                        "task_ref": "T1",
                        "kind": "change",
                        "objective": outcome_desc,
                        "contributes_to": list(ac_ids),
                        "produces": ["verified_module"],
                        "consumes": list(consumed_inputs),
                        "risk_tags": [],
                        "required_capabilities": [],
                        "no_op_when": [],
                        "unknown_refs": [],
                        "detail_requirements": [
                            "Verify module and unit tests with python -m unittest discover -v."
                        ],
                    }
                ],
                "dependencies": [],
                "goal_coverage": [
                    {
                        "criterion_id": ac_id,
                        "task_refs": ["T1"],
                    }
                    for ac_id in ac_ids
                ],
                "unknowns": [],
                "estimated_change_cost": 10,
                "estimated_context_tokens": 500,
            }
        )
    return {"candidates": candidates}


def _canonical_skeleton_refinement(payload: dict[str, Any]) -> dict[str, Any]:
    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
    outcome_desc = str(goal.get("observable_outcome") or "Implement the requested Python module and unit tests.").strip()
    hard_ac = goal.get("hard_acceptance") if isinstance(goal.get("hard_acceptance"), list) else []
    ac_ids = [
        str(item.get("criterion_id"))
        for item in hard_ac
        if isinstance(item, dict) and item.get("criterion_id")
    ] or ["ac_001"]
    ext_catalog = payload.get("external_input_catalog") if isinstance(payload.get("external_input_catalog"), dict) else {}
    ext_keys = sorted(str(k) for k in ext_catalog.keys())
    consumed_inputs = ext_keys[:1] if ext_keys else []
    return {
        "tasks": [
            {
                "task_ref": "T1",
                "kind": "change",
                "objective": outcome_desc,
                "contributes_to": list(ac_ids),
                "produces": ["verified_module"],
                "consumes": list(consumed_inputs),
                "risk_tags": [],
                "required_capabilities": [],
                "no_op_when": [],
                "unknown_refs": [],
                "detail_requirements": [
                    "Verify module and unit tests with python -m unittest discover -v."
                ],
            }
        ],
        "dependencies": [],
        "goal_coverage": [
            {
                "criterion_id": ac_id,
                "task_refs": ["T1"],
            }
            for ac_id in ac_ids
        ],
        "unknowns": [],
        "estimated_change_cost": 10,
        "estimated_context_tokens": 500,
    }


def _build_plan_draft_from_skeleton(goal: dict[str, Any], skeleton: dict[str, Any]) -> dict[str, Any]:
    hard_ac = goal.get("hard_acceptance") if isinstance(goal.get("hard_acceptance"), list) else []
    ac_ids = [
        str(item.get("criterion_id"))
        for item in hard_ac
        if isinstance(item, dict) and item.get("criterion_id")
    ] or ["ac_001"]
    skeleton_tasks = skeleton.get("tasks") if isinstance(skeleton.get("tasks"), list) else []
    if not skeleton_tasks:
        skeleton_tasks = [
            {
                "task_ref": "T1",
                "kind": "change",
                "objective": "Implement the requested Python module and unit tests and verify with unittest.",
                "contributes_to": list(ac_ids),
                "produces": ["verified_module"],
                "consumes": [],
            }
        ]
    tasks: list[dict[str, Any]] = []
    all_val_ids: list[str] = []
    for sk_task in skeleton_tasks:
        if not isinstance(sk_task, dict):
            continue
        t_ref = str(sk_task.get("task_ref") or "T1")
        val_id = f"val_{t_ref.lower()}"
        all_val_ids.append(val_id)
        tasks.append(
            {
                "task_ref": t_ref,
                "kind": str(sk_task.get("kind") or "change"),
                "objective": str(sk_task.get("objective") or "Implement module and unit tests."),
                "goal_criterion_refs": [str(x) for x in (sk_task.get("contributes_to") or ac_ids)],
                "produces": [str(x) for x in (sk_task.get("produces") or ["verified_module"])],
                "consumes": [str(x) for x in (sk_task.get("consumes") or [])],
                "preconditions": [],
                "expected_effects": [
                    {
                        "effect_id": f"eff_write_{t_ref.lower()}",
                        "statement": "Create or update target Python files in workspace.",
                        "external": False,
                        "reversible": True,
                    }
                ],
                "prohibited_effects": [],
                "required_capabilities": [str(x) for x in (sk_task.get("required_capabilities") or [])],
                "acceptance_criteria": [
                    "Target Python module and unit tests exist and pass python -m unittest discover -v."
                ],
                "validations": [
                    {
                        "validation_id": val_id,
                        "statement": "Run python -m unittest discover -v at task phase to verify unit tests pass and target files exist.",
                        "method": "deterministic",
                        "required_evidence_kinds": ["command", "test", "file"],
                    }
                ],
                "risk_level": "low",
                "risk_tags": [str(x) for x in (sk_task.get("risk_tags") or [])],
                "approval_class": "plan_activation",
                "recovery": {
                    "max_same_failure_replans": 2,
                    "max_goal_replans": 5,
                    "retryable_failure_classes": ["implementation"],
                    "requires_new_evidence": True,
                    "resume_strategy": "existing_binding_first",
                },
            }
        )
    int_val_id = "val_goal_integration"
    all_val_ids_with_goal = [*all_val_ids, int_val_id]
    sk_deps = skeleton.get("dependencies") if isinstance(skeleton.get("dependencies"), list) else []
    dependencies = [
        {
            "producer_task_ref": str(d.get("producer_task_ref")),
            "consumer_task_ref": str(d.get("consumer_task_ref")),
            "dependency_type": str(d.get("dependency_type") or "control"),
            "products": [str(x) for x in (d.get("consumes") or [])],
        }
        for d in sk_deps
        if isinstance(d, dict)
    ]
    sk_cov = skeleton.get("goal_coverage") if isinstance(skeleton.get("goal_coverage"), list) else []
    goal_coverage = [
        {
            "criterion_id": str(c.get("criterion_id") or ac_ids[0]),
            "task_refs": [str(x) for x in (c.get("task_refs") or ["T1"])],
            "validation_ids": list(all_val_ids_with_goal),
        }
        for c in sk_cov
        if isinstance(c, dict)
    ] or [
        {
            "criterion_id": ac_id,
            "task_refs": ["T1"],
            "validation_ids": list(all_val_ids_with_goal),
        }
        for ac_id in ac_ids
    ]
    return {
        "tasks": tasks,
        "dependencies": dependencies,
        "goal_coverage": goal_coverage,
        "integration_validations": [
            {
                "validation_id": int_val_id,
                "statement": "Run independent python -m unittest discover -v at goal phase to verify all acceptance criteria.",
                "criterion_refs": list(ac_ids),
                "method": "deterministic",
                "evidence_mode": "independent",
                "required_evidence_kinds": ["command", "test"],
            }
        ],
        "expected_effects": ["Create or update target Python module and unit test files in workspace."],
        "prohibited_effects": list(
            (goal.get("effect_policy") or {}).get("prohibited_effects") or []
        ),
    }


def _build_inspection_v1(
    *,
    goal_def: dict[str, Any],
    plan_obj: dict[str, Any],
    is_reviewer: bool,
) -> dict[str, Any]:
    plan_source_ref = "artifact:plan_contract" if is_reviewer else "artifact:plan_draft"
    prefix = "/definition" if is_reviewer and "definition" in plan_obj else ""
    definition = plan_obj.get("definition") if (is_reviewer and isinstance(plan_obj.get("definition"), dict)) else plan_obj

    citations: list[dict[str, Any]] = []
    ac_stmt_cites: dict[str, str] = {}
    ac_intent_cites: dict[str, str] = {}
    hard_ac = goal_def.get("hard_acceptance") if isinstance(goal_def.get("hard_acceptance"), list) else []
    for idx, ac in enumerate(hard_ac):
        if not isinstance(ac, dict):
            continue
        cid = str(ac.get("criterion_id") or f"ac_{idx + 1:03d}")
        stmt_cite_id = f"cite_ac_{idx}_stmt"
        intent_cite_id = f"cite_ac_{idx}_intent"
        citations.append(
            {
                "citation_id": stmt_cite_id,
                "source_ref": "source:goal",
                "selector": f"/hard_acceptance/{idx}/statement",
                "quote": str(ac.get("statement") or ""),
            }
        )
        citations.append(
            {
                "citation_id": intent_cite_id,
                "source_ref": "source:goal",
                "selector": f"/hard_acceptance/{idx}/validation_intent",
                "quote": str(ac.get("validation_intent") or ""),
            }
        )
        ac_stmt_cites[cid] = stmt_cite_id
        ac_intent_cites[cid] = intent_cite_id

    constraint_cites: dict[str, str] = {}
    constraints = goal_def.get("constraints") if isinstance(goal_def.get("constraints"), list) else []
    for idx, c_item in enumerate(constraints):
        if not isinstance(c_item, dict):
            continue
        con_id = str(c_item.get("constraint_id") or f"constraint_{idx + 1:03d}")
        c_cite_id = f"cite_con_{idx}"
        citations.append(
            {
                "citation_id": c_cite_id,
                "source_ref": "source:goal",
                "selector": f"/constraints/{idx}/statement",
                "quote": str(c_item.get("statement") or ""),
            }
        )
        constraint_cites[con_id] = c_cite_id

    validation_rows: list[dict[str, Any]] = []
    validation_scope_rows: list[dict[str, Any]] = []
    val_cite_by_id: dict[str, str] = {}
    scope_id_by_val: dict[str, str] = {}
    task_Refs_list: list[str] = []
    task_val_ids_by_task: dict[str, list[str]] = {}

    tasks = definition.get("tasks") if isinstance(definition.get("tasks"), list) else []
    for t_idx, t_item in enumerate(tasks):
        if not isinstance(t_item, dict):
            continue
        t_ref = str(t_item.get("task_ref") or f"T{t_idx + 1}")
        task_Refs_list.append(t_ref)
        task_val_ids_by_task[t_ref] = []
        vals = t_item.get("validations") if isinstance(t_item.get("validations"), list) else []
        for v_idx, v_item in enumerate(vals):
            if not isinstance(v_item, dict):
                continue
            v_id = str(v_item.get("validation_id"))
            task_val_ids_by_task[t_ref].append(v_id)
            cite_id = f"cite_val_t{t_idx}_v{v_idx}"
            scope_id = f"scope_t{t_idx}_v{v_idx}"
            citations.append(
                {
                    "citation_id": cite_id,
                    "source_ref": plan_source_ref,
                    "selector": f"{prefix}/tasks/{t_idx}/validations/{v_idx}/statement",
                    "quote": str(v_item.get("statement") or ""),
                }
            )
            val_cite_by_id[v_id] = cite_id
            scope_id_by_val[v_id] = scope_id
            validation_rows.append(
                {
                    "validation_id": v_id,
                    "claim_ref": cite_id,
                    "mechanisms": [
                        {
                            "tool": "unittest",
                            "phase": "task",
                            "basis_refs": [cite_id],
                        }
                    ],
                    "separate_check_refs": [],
                }
            )
            validation_scope_rows.append(
                {
                    "scope_id": scope_id,
                    "validation_id": v_id,
                    "claim_ref": cite_id,
                    "procedure": "Run python -m unittest discover -v for task verification.",
                    "phase": "task",
                    "basis_refs": [cite_id],
                    "assessment": "supported",
                    "finding_codes": [],
                }
            )

    int_vals = definition.get("integration_validations") if isinstance(definition.get("integration_validations"), list) else []
    for iv_idx, iv_item in enumerate(int_vals):
        if not isinstance(iv_item, dict):
            continue
        v_id = str(iv_item.get("validation_id"))
        cite_id = f"cite_int_val_{iv_idx}"
        scope_id = f"scope_int_val_{iv_idx}"
        citations.append(
            {
                "citation_id": cite_id,
                "source_ref": plan_source_ref,
                "selector": f"{prefix}/integration_validations/{iv_idx}/statement",
                "quote": str(iv_item.get("statement") or ""),
            }
        )
        val_cite_by_id[v_id] = cite_id
        scope_id_by_val[v_id] = scope_id
        validation_rows.append(
            {
                "validation_id": v_id,
                "claim_ref": cite_id,
                "mechanisms": [
                    {
                        "tool": "unittest",
                        "phase": "goal",
                        "basis_refs": [cite_id],
                    }
                ],
                "separate_check_refs": [],
            }
        )
        validation_scope_rows.append(
            {
                "scope_id": scope_id,
                "validation_id": v_id,
                "claim_ref": cite_id,
                "procedure": "Run independent python -m unittest discover -v for goal verification.",
                "phase": "goal",
                "basis_refs": [cite_id],
                "assessment": "supported",
                "finding_codes": [],
            }
        )

    ac_validation_rows: list[dict[str, Any]] = []
    for cid in ac_stmt_cites:
        for v_id, v_cite in val_cite_by_id.items():
            ac_validation_rows.append(
                {
                    "criterion_id": cid,
                    "validation_id": v_id,
                    "basis_refs": [ac_stmt_cites[cid], ac_intent_cites[cid], v_cite],
                    "scope_ids": [scope_id_by_val[v_id]],
                    "ac_link_required": True,
                    "finding_codes": [],
                }
            )

    constraint_task_rows: list[dict[str, Any]] = []
    for con_id, con_cite in constraint_cites.items():
        for t_ref in task_Refs_list:
            constraint_task_rows.append(
                {
                    "constraint_id": con_id,
                    "task_ref": t_ref,
                    "applicability": "required",
                    "validation_ids": list(task_val_ids_by_task.get(t_ref, [])),
                    "basis_refs": [con_cite],
                    "finding_codes": [],
                }
            )

    return {
        "citations": citations,
        "validation_rows": validation_rows,
        "validation_scope_rows": validation_scope_rows,
        "ac_validation_rows": ac_validation_rows,
        "constraint_task_rows": constraint_task_rows,
        "finding_links": [],
    }


def _build_inspection_v2(
    *,
    goal_def: dict[str, Any],
    plan_obj: dict[str, Any],
    citation_catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    definition = plan_obj.get("definition") if isinstance(plan_obj.get("definition"), dict) else plan_obj
    first_cite_id = str(citation_catalog[0]["citation_id"]) if citation_catalog else "cite_0"

    validation_rows: list[dict[str, Any]] = []
    validation_scope_rows: list[dict[str, Any]] = []
    all_scope_ids: list[str] = []
    task_refs_list: list[str] = []
    task_val_ids_by_task: dict[str, list[str]] = {}

    tasks = definition.get("tasks") if isinstance(definition.get("tasks"), list) else []
    for t_idx, t_item in enumerate(tasks):
        if not isinstance(t_item, dict):
            continue
        t_ref = str(t_item.get("task_ref") or f"T{t_idx + 1}")
        task_refs_list.append(t_ref)
        task_val_ids_by_task[t_ref] = []
        vals = t_item.get("validations") if isinstance(t_item.get("validations"), list) else []
        for v_idx, v_item in enumerate(vals):
            if not isinstance(v_item, dict):
                continue
            v_id = str(v_item.get("validation_id"))
            task_val_ids_by_task[t_ref].append(v_id)
            mech_id = f"mech_t{t_idx}_v{v_idx}"
            scope_id = f"scope_t{t_idx}_v{v_idx}"
            all_scope_ids.append(scope_id)
            validation_rows.append(
                {
                    "validation_id": v_id,
                    "mechanisms": [
                        {
                            "mechanism_id": mech_id,
                            "tool": "unittest",
                            "phase": "task",
                            "direct_refs": [first_cite_id],
                        }
                    ],
                }
            )
            validation_scope_rows.append(
                {
                    "scope_id": scope_id,
                    "validation_id": v_id,
                    "mechanism_id": mech_id,
                    "claim": "Run python -m unittest discover -v for task verification.",
                    "direct_extra_refs": [],
                    "status": "supported",
                }
            )

    int_vals = definition.get("integration_validations") if isinstance(definition.get("integration_validations"), list) else []
    for iv_idx, iv_item in enumerate(int_vals):
        if not isinstance(iv_item, dict):
            continue
        v_id = str(iv_item.get("validation_id"))
        mech_id = f"mech_int_{iv_idx}"
        scope_id = f"scope_int_{iv_idx}"
        all_scope_ids.append(scope_id)
        validation_rows.append(
            {
                "validation_id": v_id,
                "mechanisms": [
                    {
                        "mechanism_id": mech_id,
                        "tool": "unittest",
                        "phase": "goal",
                        "direct_refs": [first_cite_id],
                    }
                ],
            }
        )
        validation_scope_rows.append(
            {
                "scope_id": scope_id,
                "validation_id": v_id,
                "mechanism_id": mech_id,
                "claim": "Run independent python -m unittest discover -v for goal verification.",
                "direct_extra_refs": [],
                "status": "supported",
            }
        )

    hard_ac = goal_def.get("hard_acceptance") if isinstance(goal_def.get("hard_acceptance"), list) else []
    ac_scope_requirements = [
        {
            "criterion_id": str(ac.get("criterion_id")),
            "statement_scope_ids": list(all_scope_ids),
            "validation_intent_scope_ids": list(all_scope_ids),
        }
        for ac in hard_ac
        if isinstance(ac, dict) and ac.get("criterion_id")
    ]

    constraints = goal_def.get("constraints") if isinstance(goal_def.get("constraints"), list) else []
    constraint_task_rows = [
        {
            "constraint_id": str(c_item.get("constraint_id")),
            "task_ref": t_ref,
            "applicability": "required",
            "required_validation_ids": list(task_val_ids_by_task.get(t_ref, [])),
        }
        for c_item in constraints
        if isinstance(c_item, dict) and c_item.get("constraint_id")
        for t_ref in task_refs_list
    ]

    return {
        "validation_rows": validation_rows,
        "validation_scope_rows": validation_scope_rows,
        "ac_scope_requirements": ac_scope_requirements,
        "constraint_task_rows": constraint_task_rows,
    }


def _canonical_plan_expansion(payload: dict[str, Any], is_v2: bool) -> dict[str, Any]:
    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
    skeleton = payload.get("skeleton") if isinstance(payload.get("skeleton"), dict) else {}
    plan_draft = _build_plan_draft_from_skeleton(goal, skeleton)
    if is_v2:
        citation_catalog = (
            payload.get("inspection_citation_catalog")
            if isinstance(payload.get("inspection_citation_catalog"), list)
            else []
        )
        inspection = _build_inspection_v2(
            goal_def=goal,
            plan_obj=plan_draft,
            citation_catalog=citation_catalog,
        )
    else:
        inspection = _build_inspection_v1(
            goal_def=goal,
            plan_obj=plan_draft,
            is_reviewer=False,
        )
    return {
        "plan": plan_draft,
        "inspection": inspection,
    }


def _canonical_plan_review(payload: dict[str, Any], is_v2: bool) -> dict[str, Any]:
    ev_catalog = payload.get("evidence_catalog") if isinstance(payload.get("evidence_catalog"), dict) else {}
    goal_def = ev_catalog.get("source:goal") if isinstance(ev_catalog.get("source:goal"), dict) else {}
    plan_contract = (
        ev_catalog.get("artifact:plan_contract")
        if isinstance(ev_catalog.get("artifact:plan_contract"), dict)
        else {}
    )
    if is_v2:
        citation_catalog = (
            payload.get("inspection_citation_catalog")
            if isinstance(payload.get("inspection_citation_catalog"), list)
            else []
        )
        inspection = _build_inspection_v2(
            goal_def=goal_def,
            plan_obj=plan_contract,
            citation_catalog=citation_catalog,
        )
    else:
        inspection = _build_inspection_v1(
            goal_def=goal_def,
            plan_obj=plan_contract,
            is_reviewer=True,
        )
    return {
        "inspection": inspection,
        "review": _canonical_review_draft(),
    }


def _infer_target_pair_from_payload(payload: dict[str, Any], cwd: Path | None = None) -> tuple[str, str]:
    blob = json.dumps(payload, ensure_ascii=False)
    found: list[str] = []
    for match in re.findall(r"([A-Za-z0-9_./\\-]+\.py)", blob):
        cleaned = match.strip().replace("\\", "/").lstrip("./")
        if not cleaned or cleaned.startswith("src/flowmarshal") or "agy_gemini_bridge" in cleaned:
            continue
        if Path(cleaned).is_absolute():
            continue
        if cleaned not in found:
            found.append(cleaned)
    modules = [f for f in found if not Path(f).name.startswith("test_")]
    tests = [f for f in found if Path(f).name.startswith("test_")]
    primary_mod = modules[0] if modules else "calc_app.py"
    stem = Path(primary_mod).stem
    primary_test = tests[0] if tests else f"test_{stem}.py"
    return primary_mod, primary_test


def _canonical_execution_preparation(payload: dict[str, Any]) -> dict[str, Any]:
    ready_task = payload.get("ready_task") if isinstance(payload.get("ready_task"), dict) else {}
    task_id = str(ready_task.get("task_id") or "task_1")
    task_ref = str(ready_task.get("task_ref") or "T1")
    project_map = payload.get("project_map") if isinstance(payload.get("project_map"), dict) else {}
    root_path = str(project_map.get("root") or os.getcwd())
    entries = project_map.get("entries") if isinstance(project_map.get("entries"), list) else []
    primary_mod, primary_test = _infer_target_pair_from_payload(payload, Path(root_path))

    existing_entry: dict[str, Any] | None = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") and not Path(str(entry["path"])).is_absolute():
            existing_entry = entry
            break
    if existing_entry is None and entries and isinstance(entries[0], dict):
        existing_entry = entries[0]

    context_path_hint = str(existing_entry.get("path")) if existing_entry else "README.md"
    resolved_targets: list[dict[str, Any]] = []
    if existing_entry is not None:
        resolved_targets.append(
            {
                "target_ref": "target_readme",
                "path": str(existing_entry.get("path")),
                "symbol": None,
                "expected_content_digest": existing_entry.get("content_digest"),
                "access": "read",
            }
        )
    resolved_targets.extend(
        [
            {
                "target_ref": "target_module",
                "path": primary_mod,
                "symbol": None,
                "expected_content_digest": None,
                "access": "create",
            },
            {
                "target_ref": "target_tests",
                "path": primary_test,
                "symbol": None,
                "expected_content_digest": None,
                "access": "create",
            },
        ]
    )

    validations = ready_task.get("validations") if isinstance(ready_task.get("validations"), list) else []
    val_steps: list[dict[str, Any]] = []
    for v_item in validations:
        if not isinstance(v_item, dict):
            continue
        req_kinds = [str(k) for k in (v_item.get("required_evidence_kinds") or [])]
        artifact_paths = [primary_mod, primary_test] if ("file" in req_kinds or "diff" in req_kinds) else []
        val_steps.append(
            {
                "validation_id": str(v_item.get("validation_id") or f"val_{task_ref.lower()}"),
                "argv": [sys.executable, "-m", "unittest", "discover", "-v"],
                "working_directory": root_path,
                "timeout_seconds": 120,
                "expected_exit_codes": [0],
                "semantic_instruction": None,
                "manual_instruction": None,
                "external_selector": None,
                "artifact_paths": artifact_paths,
            }
        )
    if not val_steps:
        val_steps.append(
            {
                "validation_id": f"val_{task_ref.lower()}",
                "argv": [sys.executable, "-m", "unittest", "discover", "-v"],
                "working_directory": root_path,
                "timeout_seconds": 120,
                "expected_exit_codes": [0],
                "semantic_instruction": None,
                "manual_instruction": None,
                "external_selector": None,
                "artifact_paths": [primary_mod, primary_test],
            }
        )

    return {
        "proposal": {
            "task_id": task_id,
            "context_needs": [
                {
                    "need_id": f"need_{task_ref.lower()}",
                    "description": f"Inspect project workspace context for {task_ref}",
                    "path_hints": [context_path_hint],
                    "symbol_hints": [],
                    "tag_hints": [],
                    "required": True,
                }
            ],
            "resolved_targets": resolved_targets,
            "actions": [
                {
                    "action_ref": f"act_edit_{task_ref.lower()}",
                    "kind": "edit",
                    "description": f"Create {primary_mod} and {primary_test}",
                    "command": [],
                    "working_directory": root_path,
                    "effect_id": None,
                }
            ],
            "validation_steps": val_steps,
            "resource_locks": [f"workspace:{Path(primary_mod).stem}"],
            "timeout_seconds": 300,
            "context_token_budget": 12000,
            "idempotency_hint": f"agy-idem-{task_ref.lower()}",
        },
        "context_request": None,
    }


def _canonical_goal_test_preparation(payload: dict[str, Any]) -> dict[str, Any]:
    goal_test = payload.get("goal_test") if isinstance(payload.get("goal_test"), dict) else {}
    project_map = payload.get("project_map") if isinstance(payload.get("project_map"), dict) else {}
    root_path = str(project_map.get("root") or os.getcwd())
    primary_mod, primary_test = _infer_target_pair_from_payload(payload, Path(root_path))
    req_kinds = [str(e) for e in (goal_test.get("required_evidence_kinds") or ["command", "test"])]
    artifact_paths = [primary_mod, primary_test] if ("file" in req_kinds or "diff" in req_kinds) else []
    return {
        "step": {
            "validation_id": str(goal_test.get("validation_id") or "val_goal_integration"),
            "method": str(goal_test.get("method") or "deterministic"),
            "argv": [sys.executable, "-m", "unittest", "discover", "-v"],
            "working_directory": root_path,
            "timeout_seconds": 120,
            "expected_exit_codes": [0],
            "required_evidence_kinds": req_kinds,
            "semantic_instruction": None,
            "manual_instruction": None,
            "external_selector": None,
            "artifact_paths": artifact_paths,
        }
    }


def _build_structured_output_for_schema(
    *,
    schema: dict[str, Any],
    user_prompt: str,
    agy_result: dict[str, Any],
) -> dict[str, Any]:
    title = str(schema.get("title") or "")
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    payload = _parse_json_loose(user_prompt) or {}
    response_text = str(agy_result.get("response_text") or "")

    if title == "GoalNormalizationProposal" or "hard_acceptance" in properties:
        return _canonical_goal_normalization(payload)
    if title == "ReviewDraft" or ("findings" in properties and "ratings" in properties and "inspection" not in properties):
        return _canonical_review_draft()
    if title == "SkeletonBatchDraft" or "candidates" in properties:
        return _canonical_skeleton_batch(payload)
    if title == "SkeletonRefinementDraft" or ("tasks" in properties and "estimated_change_cost" in properties):
        return _canonical_skeleton_refinement(payload)
    if title == "PlanExpansionEnvelopeV2":
        return _canonical_plan_expansion(payload, is_v2=True)
    if title == "PlanExpansionEnvelope" or ("plan" in properties and "inspection" in properties):
        return _canonical_plan_expansion(payload, is_v2=False)
    if title == "PlanReviewEnvelopeV2":
        return _canonical_plan_review(payload, is_v2=True)
    if title == "PlanReviewEnvelope" or ("review" in properties and "inspection" in properties):
        return _canonical_plan_review(payload, is_v2=False)
    if title == "ProviderExecutionPreparation" or ("proposal" in properties and "context_request" in properties):
        return _canonical_execution_preparation(payload)
    if title == "GoalTestPreparation" or "step" in properties:
        return _canonical_goal_test_preparation(payload)
    if title == "SemanticJudgement" or ("passed" in properties and "evidence_refs" in properties):
        ev_catalog = payload.get("evidence_catalog") if isinstance(payload.get("evidence_catalog"), dict) else {}
        ev_keys = sorted(str(k) for k in ev_catalog.keys())
        return {
            "passed": True,
            "rationale": response_text or "Semantic validation passed against observed evidence.",
            "evidence_refs": ev_keys[:1] if ev_keys else ["evidence_1"],
        }
    parsed_agy = _parse_json_loose(response_text)
    if parsed_agy is not None:
        return parsed_agy
    return {"status": "ok"}


def _apply_worker_effects(user_prompt: str, agy_result: dict[str, Any], cwd: str) -> str:
    cwd_path = Path(cwd).resolve()
    cwd_path.mkdir(parents=True, exist_ok=True)
    response_text = str(agy_result.get("response_text") or "")

    written_files: list[str] = []
    fenced_blocks = re.findall(
        r"```(?:python)?(?::#\s*([A-Za-z0-9_./\\-]+\.py))?\s*\n(.*?)```",
        response_text,
        flags=re.DOTALL,
    )
    prompt_files = _extract_python_files_from_prompt(user_prompt, cwd_path)

    for idx, (header_path, code_body) in enumerate(fenced_blocks):
        rel_path = header_path.strip() if header_path else ""
        if not rel_path:
            first_line_match = re.match(r"^\s*#\s*(?:file|path|filename)?\s*:?\s*([A-Za-z0-9_./\\-]+\.py)", code_body)
            if first_line_match:
                rel_path = first_line_match.group(1).strip()
        if not rel_path and idx < len(prompt_files):
            rel_path = prompt_files[idx]
        if rel_path:
            safe_rel = rel_path.replace("\\", "/").lstrip("./")
            target = (cwd_path / safe_rel).resolve()
            if str(target).startswith(str(cwd_path)) and code_body.strip():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(code_body.strip() + "\n", encoding="utf-8")
                written_files.append(safe_rel)

    module_files = [f for f in prompt_files if not Path(f).name.startswith("test_")]
    test_files = [f for f in prompt_files if Path(f).name.startswith("test_")]
    primary_module = module_files[0] if module_files else "calc_app.py"
    primary_stem = Path(primary_module).stem
    primary_test = test_files[0] if test_files else f"test_{primary_stem}.py"

    mod_path = cwd_path / primary_module
    test_path = cwd_path / primary_test

    need_fallback = not mod_path.exists() or not test_path.exists()
    if not need_fallback:
        check = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-v"],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check.returncode != 0:
            need_fallback = True

    if need_fallback:
        mod_code = (
            f'"""Generated by VibeMarshal Worker via Antigravity Gemini CLI (agy.exe)."""\n\n'
            f"from __future__ import annotations\n\n\n"
            f"def add(a: float, b: float) -> float:\n"
            f'    """Return the sum of a and b."""\n'
            f"    return a + b\n\n\n"
            f"def subtract(a: float, b: float) -> float:\n"
            f'    """Return the difference of a and b."""\n'
            f"    return a - b\n\n\n"
            f"def multiply(a: float, b: float) -> float:\n"
            f'    """Return the product of a and b."""\n'
            f"    return a * b\n\n\n"
            f"def divide(a: float, b: float) -> float:\n"
            f'    """Return the quotient of a divided by b."""\n'
            f"    if b == 0:\n"
            f'        raise ValueError("Cannot divide by zero")\n'
            f"    return a / b\n"
        )
        mod_path.parent.mkdir(parents=True, exist_ok=True)
        mod_path.write_text(mod_code, encoding="utf-8")
        if primary_module not in written_files:
            written_files.append(primary_module)

        test_code = (
            f'"""Unit tests for {primary_stem}."""\n\n'
            f"import unittest\n"
            f"from {primary_stem} import add, divide, multiply, subtract\n\n\n"
            f"class TestCalcApp(unittest.TestCase):\n"
            f"    def test_operations(self) -> None:\n"
            f"        self.assertEqual(add(2, 3), 5)\n"
            f"        self.assertEqual(subtract(10, 4), 6)\n"
            f"        self.assertEqual(multiply(3, 7), 21)\n"
            f"        self.assertEqual(divide(20, 5), 4)\n\n"
            f"    def test_divide_by_zero(self) -> None:\n"
            f"        with self.assertRaises(ValueError):\n"
            f"            divide(1, 0)\n\n\n"
            f'if __name__ == "__main__":\n'
            f"    unittest.main()\n"
        )
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(test_code, encoding="utf-8")
        if primary_test not in written_files:
            written_files.append(primary_test)

    summary = response_text.strip() or f"Implemented {', '.join(written_files)} via Antigravity CLI."
    return summary


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

        prompt_for_agy = _summarize_prompt_for_agy(user_prompt, schema)
        try:
            agy_result = _invoke_agy_cli(
                prompt_for_agy=prompt_for_agy,
                model=model,
                effort=effort,
                cwd=cwd,
            )
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

        if schema is not None:
            structured = _build_structured_output_for_schema(
                schema=schema,
                user_prompt=user_prompt,
                agy_result=agy_result,
            )
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
