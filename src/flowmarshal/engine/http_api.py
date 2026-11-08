"""VibeMarshal Desktop GUI를 위한 로컬 HTTP/JSON + SSE API 어댑터.

GUI는 SQLite를 직접 읽거나 쓰지 않고, 이 로컬 HTTP 어댑터를 통해 EngineService 및
EngineApplication의 권위 상태와 명령을 호출한다.
"""
from __future__ import annotations

from copy import deepcopy
import hmac
import importlib
import importlib.util
import json
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..canonical import sha256_bytes, sha256_digest
from .application import ApplicationAuthority, EngineApplication, EngineApplicationError
from .capabilities import CoreCapabilityError
from .domain import (
    ApprovalClass,
    BehaviorPolicy,
    Criticality,
    DependencyType,
    GoalContractRevision,
    GoalVerdict,
    GoalVerdictStatus,
    LifecycleStage,
    MissionClass,
    MutationPolicy,
    PlanContractRevision,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RevisionStatus,
    RiskLevel,
    RuntimeIntentKind,
    TaskKind,
    ThreadBinding,
    new_id,
    utc_now,
)
from .governance_gate import GovernanceSettings
from .ledger import EngineLedgerError, SQLiteEngineLedger
from .model_lock import RUNTIME_CAPABILITIES
from .models import EngineRoleConfiguration, ModelCapability, ModelInventory, RoleModelBinding
from .roles import (
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    RoleCallReceipt,
    RoleCallRequest,
    RoleCallResult,
    strict_json_output_schema,
)
from .runtime import FakeCodexRuntime, RuntimeOperationReceipt
from .service import EngineService, EngineServiceError


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _LocalImmediateRuntime(FakeCodexRuntime):
    """로컬 결정적 실행 모드에서 turn 시작 직후 provider terminal을 완료로 전환하는 runtime."""

    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        prompt: str,
        model: str,
        effort: str,
        output_schema: dict[str, Any] | None = None,
    ) -> RuntimeOperationReceipt:
        receipt = super().start_turn(
            thread_id=thread_id,
            cwd=cwd,
            prompt=prompt,
            model=model,
            effort=effort,
            output_schema=output_schema,
        )
        self.complete(thread_id, response="worker turn completed")
        return receipt


class _LocalTaskGate:
    """로컬 결정적 모드에서 EngineDispatcher가 실행 Task를 통과시키는 task gate."""

    def before_execution(self, task: Any) -> None:
        return None

    def before_completion(self, task: Any) -> None:
        return None

    def close(self) -> None:
        return None


class _LocalGovernance:
    """로컬 결정적 모드용 governance provider."""

    def check_conformance(self) -> dict[str, Any]:
        return {"verdict": "PASS", "checks": [], "provenance": "local_derived"}

    def open_gate(self, service: Any, *, runtime: Any, roles: Any, runner: Any) -> _LocalTaskGate:
        return _LocalTaskGate()


def _build_plan_inspection_dict(
    plan: dict[str, Any],
    goal: dict[str, Any],
    *,
    revision: bool = False,
) -> dict[str, Any]:
    """실제 plan draft/revision과 goal 원문에서 대조표(PlanInspection)를 동적으로 구성한다."""
    definition = plan["definition"] if revision else plan
    prefix = "/definition" if revision else ""
    plan_ref = "artifact:plan_contract" if revision else "artifact:plan_draft"
    citations: list[dict[str, Any]] = []

    def cite(source: str, selector: str, text: str) -> str:
        for existing in citations:
            if (existing["source_ref"], existing["selector"], existing["quote"]) == (source, selector, text):
                return str(existing["citation_id"])
        ref = f"c{len(citations)}"
        citations.append(
            {
                "citation_id": ref,
                "source_ref": source,
                "selector": selector,
                "quote": text,
            }
        )
        return ref

    ac_refs = {
        ac["criterion_id"]: (
            cite("source:goal", f"/hard_acceptance/{i}/statement", ac["statement"]),
            cite("source:goal", f"/hard_acceptance/{i}/validation_intent", ac["validation_intent"]),
        )
        for i, ac in enumerate(goal["hard_acceptance"])
    }
    validations = [
        (task["task_ref"], validation, f"{prefix}/tasks/{ti}/validations/{vi}")
        for ti, task in enumerate(definition["tasks"])
        for vi, validation in enumerate(task["validations"])
    ]
    validations += [
        (None, validation, f"{prefix}/integration_validations/{vi}")
        for vi, validation in enumerate(definition["integration_validations"])
    ]
    val_refs = {
        validation["validation_id"]: cite(plan_ref, path + "/statement", validation["statement"])
        for _, validation, path in validations
    }
    constraint_refs = {
        constraint["constraint_id"]: cite("source:goal", f"/constraints/{i}/statement", constraint["statement"])
        for i, constraint in enumerate(goal.get("constraints", []))
    }
    return {
        "citations": citations,
        "ac_validation_rows": [
            {
                "criterion_id": ac,
                "validation_id": vid,
                "ac_link_required": False,
                "scope_ids": [],
                "basis_refs": [*arefs, vref],
                "finding_codes": [],
            }
            for ac, arefs in ac_refs.items()
            for vid, vref in val_refs.items()
        ],
        "constraint_task_rows": [
            {
                "constraint_id": cid,
                "task_ref": task["task_ref"],
                "applicability": "not_applicable",
                "validation_ids": [],
                "basis_refs": [ref],
                "finding_codes": [],
            }
            for cid, ref in constraint_refs.items()
            for task in definition["tasks"]
        ],
        "validation_rows": [
            {
                "validation_id": vid,
                "claim_ref": ref,
                "mechanisms": [{"tool": "deterministic_check", "phase": None, "basis_refs": [ref]}],
                "separate_check_refs": [],
            }
            for vid, ref in val_refs.items()
        ],
        "validation_scope_rows": [
            {
                "scope_id": f"scope_{index}",
                "validation_id": vid,
                "claim_ref": ref,
                "procedure": "deterministic_check",
                "phase": None,
                "basis_refs": [ref],
                "assessment": "supported",
                "finding_codes": [],
            }
            for index, (vid, ref) in enumerate(val_refs.items())
        ],
        "finding_links": [],
    }


class _LocalDeterministicRoleRunner:
    """EngineApplication의 모든 역할을 입력 payload에 맞춰 결정적으로 수행하는 StructuredRolePort."""

    def __init__(self, *, project_root_provider: Callable[[], Path]) -> None:
        self._project_root_provider = project_root_provider

    def run(
        self,
        request: RoleCallRequest,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> RoleCallResult:
        payload = self._build_payload(request)
        if validator is not None:
            validator(payload)
        provenance: dict[str, Any] = {}
        if request.operational_binding is not None:
            provenance = {
                "binding_provenance_version": "2.0",
                "requested_model": request.model,
                "requested_effort": request.effort,
                "observed_model": None,
                "observed_effort": None,
                "provider_inventory_digest": request.operational_binding.inventory.provider_inventory_digest,
                "adapter_capability_digest": request.operational_binding.inventory.adapter_capability_digest,
                "binding_provenance": {
                    "requested": "role_request",
                    "observed": None,
                    "provider_inventory": request.operational_binding.inventory.inventory_provenance,
                    "adapter_capability": "local_operational_binding",
                },
            }
        call_id = new_id("model_call")
        receipt = RoleCallReceipt(
            call_id=call_id,
            role=request.role,
            status="succeeded",
            model=request.model,
            effort=request.effort,
            **provenance,
            inventory_digest=request.inventory_digest,
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            thread_id=f"local-thread-{call_id}",
            turn_ids=(f"local-turn-{call_id}",),
            input_digest=request.request_digest,
            output_digest=sha256_digest(payload),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            timeout_policy_digest=request.timeout_policy_digest,
            observation_policy_digest=request.observation_policy_digest,
            latency_ms=0,
            recorded_at=utc_now(),
            observed_binding=request.operational_binding,
        )
        return RoleCallResult(payload=payload, receipt=receipt)

    def _build_payload(self, request: RoleCallRequest) -> dict[str, Any]:
        role = request.role
        req_payload = request.payload

        if role == "goal_normalizer":
            source_req = str(req_payload.get("source_request") or "").strip()
            is_default_req = "사용량이 비어 있는 실행에서도" in source_req or "usage 일부가 null이어도" in source_req
            observable = (
                "usage 일부가 null이어도 Markdown과 JSON 보고가 생성되고 기존 정상 합계가 유지된다."
                if is_default_req
                else source_req
            )
            return {
                "mission_class": MissionClass.BUGFIX_STABILIZATION.value,
                "observable_outcome": observable,
                "hard_acceptance": [
                    {
                        "statement": (
                            "null usage 값이 있어도 최종 보고가 오류 없이 생성된다."
                            if is_default_req
                            else f"{source_req}의 핵심 동작이 오류 없이 수행된다."
                        ),
                        "validation_intent": "결정적 검증 절차로 정상 종료와 산출물 상태를 확인한다.",
                    },
                    {
                        "statement": (
                            "측정 가능한 기존 token·latency 합계는 바뀌지 않는다."
                            if is_default_req
                            else "기존 공개 계약과 파일 정합성이 보존된다."
                        ),
                        "validation_intent": "변경 전후 상태와 기존 검증 기준을 대조한다.",
                    },
                    {
                        "statement": (
                            "미제공 usage는 0이 아니라 알 수 없음과 사유로 표시된다."
                            if is_default_req
                            else "검증 결과가 원장 evidence로 기록된다."
                        ),
                        "validation_intent": "산출물 및 검증 결과를 검사한다.",
                    },
                ],
                "quality_preferences": [],
                "constraints": [
                    {
                        "category": "compatibility",
                        "statement": "기존 CLI JSON key와 exit code를 보존한다.",
                    },
                    {
                        "category": "scope",
                        "statement": "legacy/prototype source는 수정하지 않는다.",
                    },
                ],
                "non_goals": [
                    "실제 provider usage를 추정해 채우지 않는다.",
                    "Engine 1.0 cutover 판정을 변경하지 않는다.",
                ],
                "assumptions": [],
                "unresolved_questions": [],
                "mutation_policy": MutationPolicy.MINIMAL_CHANGE.value,
                "behavior_policy": BehaviorPolicy.PRESERVE_PUBLIC_CONTRACTS.value,
                "allowed_external_effects": [],
                "prohibited_effects": ["배포", "외부 메시지", "운영 DB 변경"],
            }

        if role in {"goal_reviewer", "skeleton_reviewer"}:
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

        if role == "skeleton_generator":
            goal_def = req_payload["goal"]
            candidate_count = int(req_payload.get("candidate_count") or 1)
            ac_ids = [c["criterion_id"] for c in goal_def["hard_acceptance"]]
            c1 = ac_ids[0]
            c2 = ac_ids[1] if len(ac_ids) > 1 else c1
            c3 = ac_ids[2] if len(ac_ids) > 2 else c1
            candidates = []
            for idx in range(candidate_count):
                strategy = "boundary-fix" if idx == 0 else f"alternative-strategy-{idx + 1}"
                t1_cov = sorted({c1})
                t2_cov = sorted({c1, c3})
                t3_cov = sorted(set(ac_ids))
                coverage_list = []
                for cid in ac_ids:
                    refs = []
                    if cid in t1_cov:
                        refs.append("T1")
                    if cid in t2_cov:
                        refs.append("T2")
                    if cid in t3_cov:
                        refs.append("T3")
                    coverage_list.append({"criterion_id": cid, "task_refs": refs})
                candidates.append(
                    {
                        "approach": {
                            "strategy_family": strategy,
                            "change_shape": "localized",
                            "compatibility": "preserve",
                            "rollout_recovery": "retry bounded",
                        },
                        "tasks": [
                            {
                                "task_ref": "T1",
                                "kind": TaskKind.INSPECT.value,
                                "objective": "현재 실패 재현",
                                "contributes_to": t1_cov,
                                "produces": ["artifact:repro"],
                                "consumes": ["input:request"],
                                "risk_tags": [],
                                "required_capabilities": [],
                                "no_op_when": [],
                                "unknown_refs": [],
                                "detail_requirements": [],
                            },
                            {
                                "task_ref": "T2",
                                "kind": TaskKind.CHANGE.value,
                                "objective": "usage 합산 경계 수정",
                                "contributes_to": t2_cov,
                                "produces": ["artifact:boundary"],
                                "consumes": ["artifact:repro"],
                                "risk_tags": [],
                                "required_capabilities": [],
                                "no_op_when": [],
                                "unknown_refs": [],
                                "detail_requirements": [],
                            },
                            {
                                "task_ref": "T3",
                                "kind": TaskKind.VALIDATE.value,
                                "objective": "보고 회귀 검증",
                                "contributes_to": t3_cov,
                                "produces": ["artifact:regression"],
                                "consumes": ["artifact:boundary"],
                                "risk_tags": [],
                                "required_capabilities": [],
                                "no_op_when": [],
                                "unknown_refs": [],
                                "detail_requirements": [],
                            },
                        ],
                        "dependencies": [
                            {
                                "producer_task_ref": "T1",
                                "consumer_task_ref": "T2",
                                "dependency_type": DependencyType.DATA.value,
                                "produces": ["artifact:repro"],
                                "consumes": ["artifact:repro"],
                            },
                            {
                                "producer_task_ref": "T2",
                                "consumer_task_ref": "T3",
                                "dependency_type": DependencyType.DATA.value,
                                "produces": ["artifact:boundary"],
                                "consumes": ["artifact:boundary"],
                            },
                        ],
                        "goal_coverage": coverage_list,
                        "unknowns": [],
                        "estimated_change_cost": 3 + idx,
                        "estimated_context_tokens": 300 + idx * 50,
                    }
                )
            return {"candidates": candidates}

        if role == "plan_expander":
            goal_def = req_payload["goal"]
            skeleton = req_payload["skeleton"]
            ac_ids = [c["criterion_id"] for c in goal_def["hard_acceptance"]]
            tasks_draft = []
            val_ids_by_task: dict[str, str] = {}
            for st in skeleton["tasks"]:
                t_ref = st["task_ref"]
                val_id = f"val_{t_ref.lower()}"
                val_ids_by_task[t_ref] = val_id
                tasks_draft.append(
                    {
                        "task_ref": t_ref,
                        "kind": st["kind"],
                        "objective": st["objective"],
                        "goal_criterion_refs": list(st["contributes_to"]),
                        "produces": list(st["produces"]),
                        "consumes": list(st["consumes"]),
                        "preconditions": [],
                        "expected_effects": [],
                        "prohibited_effects": [],
                        "required_capabilities": list(st.get("required_capabilities", [])),
                        "acceptance_criteria": [f"{t_ref} 검증 완료"],
                        "validations": [
                            {
                                "validation_id": val_id,
                                "statement": f"{t_ref} 결정적 파일 검증 수행",
                                "method": "deterministic",
                                "required_evidence_kinds": ["file"],
                            }
                        ],
                        "risk_level": RiskLevel.LOW.value,
                        "risk_tags": list(st.get("risk_tags", [])),
                        "approval_class": ApprovalClass.PLAN_ACTIVATION.value,
                        "recovery": {
                            "retryable_failure_classes": ["implementation"],
                            "max_attempts": 2,
                            "timeout_seconds": 300,
                            " escalation_policy": "stop",
                        },
                    }
                )
            for td in tasks_draft:
                td["recovery"] = {"retryable_failure_classes": ["implementation"]}
            deps_draft = [
                {
                    "producer_task_ref": dep["producer_task_ref"],
                    "consumer_task_ref": dep["consumer_task_ref"],
                    "dependency_type": dep["dependency_type"],
                    "products": list(dep["consumes"]),
                }
                for dep in skeleton.get("dependencies", [])
            ]
            cov_draft = []
            for cov in skeleton["goal_coverage"]:
                v_ids = [val_ids_by_task[r] for r in cov["task_refs"] if r in val_ids_by_task]
                v_ids.append("val_goal_integration")
                cov_draft.append(
                    {
                        "criterion_id": cov["criterion_id"],
                        "task_refs": list(cov["task_refs"]),
                        "validation_ids": v_ids,
                    }
                )
            plan_draft = {
                "tasks": tasks_draft,
                "dependencies": deps_draft,
                "goal_coverage": cov_draft,
                "integration_validations": [
                    {
                        "validation_id": "val_goal_integration",
                        "statement": "독립 Goal Test 결정적 파일 검증",
                        "evidence_mode": "task_aggregate",
                        "criterion_refs": ac_ids,
                        "method": "deterministic",
                        "required_evidence_kinds": ["file"],
                    }
                ],
                "expected_effects": [],
                "prohibited_effects": list(goal_def["effect_policy"].get("prohibited_effects", [])),
            }
            return {
                "plan": plan_draft,
                "inspection": _build_plan_inspection_dict(plan_draft, goal_def, revision=False),
            }

        if role in {
            "compact_plan_reviewer",
            "critical_effect_reviewer",
            "high_risk_reviewer",
            "external_effect_reviewer",
            "recovery_plan_reviewer",
        }:
            catalog = req_payload["evidence_catalog"]
            plan_contract = catalog["artifact:plan_contract"]
            goal_def = catalog["source:goal"]
            return {
                "review": {
                    "findings": [],
                    "ratings": {
                        "goal_fit": 4,
                        "grounding": 4,
                        "engineering": 4,
                        "verification": 4,
                        "execution_safety": 4,
                    },
                },
                "inspection": _build_plan_inspection_dict(plan_contract, goal_def, revision=True),
            }

        if role == "execution_preparation":
            ready_task = req_payload["ready_task"]
            project_map = req_payload["project_map"]
            entries = project_map.get("entries") or []
            entry_path = entries[0]["path"] if entries else "AGENTS.md"
            entry_digest = (
                entries[0]["content_digest"]
                if entries
                else sha256_bytes(b"# Project Instructions\nFollow standard Python unittest conventions.\n")
            )
            t_ref = ready_task["task_ref"]
            val_steps = [
                {
                    "validation_id": v["validation_id"],
                    "argv": [sys.executable, "-c", "print('ok')"],
                    "working_directory": str(project_map.get("root") or self._project_root_provider()),
                    "timeout_seconds": 30,
                    "expected_exit_codes": [0],
                    "semantic_instruction": None,
                    "manual_instruction": None,
                    "external_selector": None,
                    "artifact_paths": [entry_path],
                }
                for v in ready_task["validations"]
            ]
            return {
                "proposal": {
                    "task_id": ready_task["task_id"],
                    "resolved_targets": [
                        {
                            "target_ref": f"target_{t_ref.lower()}",
                            "path": entry_path,
                            "expected_content_digest": entry_digest,
                            "access": "read",
                        }
                    ],
                    "actions": [
                        {
                            "action_ref": f"act_{t_ref.lower()}",
                            "kind": "inspect",
                            "description": f"{t_ref} 검증 실행",
                            "command": [],
                        }
                    ],
                    "validation_steps": val_steps,
                    "context_needs": [
                        {
                            "need_id": f"need_{t_ref.lower()}",
                            "description": f"{t_ref} 문맥 확인",
                            "path_hints": [entry_path],
                            "symbol_hints": [],
                            "required": True,
                        }
                    ],
                    "resource_locks": [],
                    "timeout_seconds": 120,
                    "idempotency_hint": f"gui-{t_ref.lower()}",
                },
                "context_request": None,
            }

        if role == "goal_test_preparation":
            goal_test = req_payload["goal_test"]
            project_map = req_payload["project_map"]
            entries = project_map.get("entries") or []
            entry_path = entries[0]["path"] if entries else "AGENTS.md"
            return {
                "step": {
                    "validation_id": goal_test["validation_id"],
                    "method": goal_test["method"],
                    "argv": [sys.executable, "-c", "print('ok')"],
                    "working_directory": str(project_map.get("root") or self._project_root_provider()),
                    "timeout_seconds": 30,
                    "expected_exit_codes": [0],
                    "required_evidence_kinds": list(goal_test["required_evidence_kinds"]),
                    "artifact_paths": [entry_path],
                }
            }

        if role == "goal_validator":
            catalog = req_payload.get("evidence_catalog") or {}
            first_ref = next(iter(catalog.keys()), "evidence_placeholder")
            return {
                "passed": True,
                "rationale": "모든 검증 근거가 충족됐습니다.",
                "evidence_refs": [first_ref],
            }

        raise ValueError(f"Unsupported role in _LocalDeterministicRoleRunner: {role}")


class LocalGuiEngineBackend:
    """실제 SQLiteEngineLedger, EngineService, EngineApplication을 감싸는 GUI 백엔드 어댑터."""

    DEFAULT_SOURCE_REQUEST = (
        "사용량이 비어 있는 실행에서도 최종 보고가 중단되지 않도록 합산 경계를 수정하고 "
        "기존 동작을 검증해줘."
    )

    def __init__(
        self,
        *,
        db_path: str | Path,
        artifact_root: str | Path,
        project_root: str | Path | None = None,
        project_id: str | None = None,
        runtime: Any | None = None,
        role_configuration: EngineRoleConfiguration | None = None,
        structured_runner: Any | None = None,
        governance: Any | None = None,
        live_gemini: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self.db_path = Path(db_path).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.project_root = (Path(project_root) if project_root else Path.cwd()).resolve()
        self.ledger = SQLiteEngineLedger(self.db_path, artifact_root=self.artifact_root)
        self.service = EngineService(self.ledger)
        self.service.initialize()
        self.live_gemini = bool(live_gemini)

        if self.live_gemini and runtime is None:
            if importlib.util.find_spec("flowmarshal.engine.agy_gemini_bridge") is not None:
                bridge = importlib.import_module("flowmarshal.engine.agy_gemini_bridge")
                self.runtime = bridge.create_agy_gemini_runtime(
                    state_root=self.artifact_root / "gemini-runtime"
                )
                self.inventory = self.runtime.list_models()
                self.role_configuration = (
                    role_configuration or bridge.default_agy_gemini_role_configuration()
                )
            else:
                from .gemini_runtime import GeminiModelCatalog, GeminiRuntime

                catalog_path = (
                    Path(__file__).resolve().parents[3]
                    / "config"
                    / "gemini-3-flash-model-catalog.json"
                )
                self.runtime = GeminiRuntime(
                    model_catalog=GeminiModelCatalog.load(catalog_path),
                    state_root=self.artifact_root / "gemini-runtime",
                )
                self.inventory = self.runtime.list_models()
                binding = RoleModelBinding(model="gemini-3-flash", effort="high")
                self.role_configuration = role_configuration or EngineRoleConfiguration(
                    normalizer=binding,
                    skeleton_generator=binding,
                    plan_expander=binding,
                    general_reviewer=binding,
                    critical_reviewer=binding,
                    executor=binding,
                    validator=binding,
                )
            self.structured_runner = structured_runner
            self.governance = (
                governance
                or GovernanceSettings.from_environment(self.artifact_root / "governance")
                or _LocalGovernance()
            )
        else:
            self.inventory = (
                runtime.list_models()
                if runtime is not None and hasattr(runtime, "list_models")
                else ModelInventory(
                    executable_digest="sha256:" + "0" * 64,
                    runtime_capabilities=RUNTIME_CAPABILITIES,
                    source="gui-model-list",
                    models=(
                        ModelCapability(model="worker", supported_efforts=("low", "medium", "high")),
                        ModelCapability(model="validator", supported_efforts=("low", "medium", "high")),
                    ),
                )
            )
            self.runtime = runtime or _LocalImmediateRuntime(self.inventory)
            worker_binding = RoleModelBinding(model="worker", effort="medium")
            validator_binding = RoleModelBinding(model="validator", effort="high")
            self.role_configuration = role_configuration or EngineRoleConfiguration(
                normalizer=worker_binding,
                skeleton_generator=worker_binding,
                plan_expander=worker_binding,
                general_reviewer=worker_binding,
                critical_reviewer=worker_binding,
                executor=worker_binding,
                validator=validator_binding,
            )
            self.structured_runner = structured_runner or (
                _LocalDeterministicRoleRunner(project_root_provider=lambda: self.project_root)
                if runtime is None
                else None
            )
            self.governance = (
                governance
                or GovernanceSettings.from_environment(self.artifact_root / "governance")
                or _LocalGovernance()
            )

        self.application = self._make_application()
        self.scenario: str = "golden-path"
        self.run_log: list[dict[str, Any]] = []
        self._draft_goal_mode: bool = False
        self._ephemeral_workspaces: list[Path] = []
        self._event_subscribers: list[Any] = []
        self.project_id = project_id or self._ensure_default_project()

    def _make_application(self) -> EngineApplication:
        return EngineApplication(
            self.service,
            runtime=self.runtime,
            role_configuration=self.role_configuration,
            structured_runner=self.structured_runner,
            governance=self.governance,
        )

    def subscribe_events(self) -> Any:
        import queue

        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=64)
        with self._lock:
            self._event_subscribers.append(q)
        return q

    def unsubscribe_events(self, q: Any) -> None:
        with self._lock:
            if q in self._event_subscribers:
                self._event_subscribers.remove(q)

    def _notify_subscribers(self, snap: dict[str, Any] | None = None) -> None:
        with self._lock:
            if not self._event_subscribers:
                return
            payload = snap or self.snapshot()
            for q in list(self._event_subscribers):
                try:
                    q.put_nowait(payload)
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            for q in list(self._event_subscribers):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self._event_subscribers.clear()
            try:
                self.application.close_task_gate()
            except Exception:
                pass
            if getattr(self.application, "supervisor", None) is not None:
                try:
                    self.application.supervisor.close()
                except Exception:
                    pass
            elif getattr(self, "runtime", None) is not None and hasattr(self.runtime, "close"):
                try:
                    self.runtime.close()
                except Exception:
                    pass
            self._cleanup_ephemeral_workspaces()

    def _cleanup_ephemeral_workspaces(self) -> None:
        import shutil

        while self._ephemeral_workspaces:
            ws = self._ephemeral_workspaces.pop()
            if ws.exists() and ws != self.project_root:
                shutil.rmtree(ws, ignore_errors=True)

    def _record_recovery_ledger_event(
        self,
        event_type: str,
        entity_id: str,
        payload: dict[str, Any],
        *,
        run_state: str,
        recovery_reason: str | None = None,
    ) -> None:
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE projects SET run_state = ?, recovery_reason = ?, updated_at = ? WHERE id = ?",
                (run_state, recovery_reason, tx.now, self.project_id),
            )
            tx.history(
                self.project_id,
                event_type,
                "recovery",
                entity_id,
                payload,
            )

    def _ensure_default_project(self) -> str:
        with self.service.ledger.read() as conn:
            row = conn.execute("SELECT id FROM projects ORDER BY created_at LIMIT 1").fetchone()
        if row is not None:
            return str(row["id"])
        return self._create_and_prepare_project(source_request=self.DEFAULT_SOURCE_REQUEST)

    def _ensure_workspace_files(self, workspace_root: Path) -> None:
        workspace_root.mkdir(parents=True, exist_ok=True)
        readme = workspace_root / "README.md"
        if not readme.exists():
            readme.write_text("# VibeMarshal Workspace\n", encoding="utf-8")
        agents_md = workspace_root / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text(
                "# Project Instructions\nFollow standard Python unittest conventions.\n",
                encoding="utf-8",
            )

    def _register_default_profile(self, project_id: str) -> ProjectProfileRevision:
        profile_def = ProjectProfileDefinition(
            product_goal="검증 가능한 로컬 workflow orchestration",
            lifecycle_stage=LifecycleStage.DEVELOPMENT,
            criticality=Criticality.MEDIUM,
            compatibility_policy="기존 CLI JSON key와 exit code를 보존한다.",
            validation_policy=("프로젝트 결정적 테스트를 통과한다.",),
            runtime_requirements=("local Windows", "Python 3.10+"),
        )
        profile_rev = ProjectProfileRevision(
            profile_revision_id=new_id("profile_revision"),
            project_id=project_id,
            revision_no=1,
            definition=profile_def,
            definition_digest=profile_def.definition_digest,
            status=RevisionStatus.READY,
            created_at=utc_now(),
        )
        return self.service.register_profile(profile_rev)

    def _create_and_prepare_project(
        self,
        *,
        source_request: str,
        candidate_count: int = 1,
    ) -> str:
        scenario_root = self.project_root
        with self.service.ledger.read() as conn:
            existing_root = conn.execute(
                "SELECT id FROM projects WHERE root = ?",
                (str(self.project_root.resolve()),),
            ).fetchone()
        if existing_root is not None:
            scenario_root = self.project_root / new_id("workspace")
            self._ephemeral_workspaces.append(scenario_root)
        self._ensure_workspace_files(scenario_root)
        project_id = self.service.create_project(
            name=scenario_root.name if scenario_root != self.project_root else "VibeMarshal Engine",
            root=str(scenario_root),
        )
        self._register_default_profile(project_id)
        self.application.prepare(
            project_id,
            source_request=source_request.strip(),
            candidate_count=candidate_count,
        )
        return project_id

    def _derive_ledger_active_attempt_and_recovery(
        self,
        *,
        project_row: dict[str, Any],
        plans_view: list[dict[str, Any]],
        attempt_rows: list[Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """인메모리 시뮬레이터 없이 SQLite 원장의 실제 Attempt·Intent·ProjectMap 상태에서 복구/진행 정보를 도출한다."""
        with self.service.ledger.read() as conn:
            intent_rows = conn.execute(
                "SELECT i.*, a.task_id, a.status AS attempt_status, a.binding_json, a.failure_class "
                "FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                "WHERE a.project_id = ? ORDER BY i.prepared_at DESC, i.rowid DESC",
                (self.project_id,),
            ).fetchall()
            receipt_by_intent = {
                r["intent_id"]: r["id"]
                for r in conn.execute(
                    "SELECT r.id, r.intent_id FROM runtime_receipts r "
                    "JOIN runtime_intents i ON i.id = r.intent_id "
                    "JOIN attempts a ON a.id = i.attempt_id WHERE a.project_id = ?",
                    (self.project_id,),
                ).fetchall()
            }
            recovery_events = conn.execute(
                "SELECT event_type, entity_id, payload_json, created_at FROM history_events "
                "WHERE project_id = ? AND event_type LIKE 'recovery.%' ORDER BY sequence DESC LIMIT 10",
                (self.project_id,),
            ).fetchall()

        active_attempt_view: dict[str, Any] | None = None
        for a_row in attempt_rows:
            if a_row["status"] in {"reserved", "starting", "running"} or (
                a_row["failure_class"] == "external_unknown"
                and project_row.get("run_state") == "recovery_required"
            ):
                binding_doc = json.loads(a_row["binding_json"]) if a_row["binding_json"] else {}
                matching_intent = next(
                    (ir for ir in intent_rows if ir["attempt_id"] == a_row["id"]),
                    None,
                )
                intent_id = matching_intent["id"] if matching_intent is not None else None
                receipt_id = receipt_by_intent.get(intent_id) if intent_id is not None else None
                recovery_reason_now = str(project_row.get("recovery_reason") or "")
                derived_status = (
                    "interrupted"
                    if recovery_reason_now == "external_unknown:observed"
                    else (
                        "unknown"
                        if (matching_intent is not None and matching_intent["status"] in {"prepared", "unknown"})
                        or recovery_reason_now.startswith("external_unknown")
                        else a_row["status"]
                    )
                )
                active_attempt_view = {
                    "attemptId": a_row["id"],
                    "taskId": a_row["task_id"],
                    "status": derived_status,
                    "threadId": binding_doc.get("thread_id"),
                    "turnId": binding_doc.get("turn_id"),
                    "intentId": intent_id,
                    "receiptId": receipt_id,
                }
                break

        recovery_reason = str(project_row.get("recovery_reason") or "")
        run_state = str(project_row.get("run_state") or "")
        first_plan_digest = plans_view[0]["activationDigest"] if plans_view else None

        if run_state == "recovery_required" and recovery_reason.startswith("stale_input"):
            stale_payload: dict[str, Any] = {}
            for ev in recovery_events:
                if ev["event_type"] == "recovery.stale_input_detected":
                    stale_payload = json.loads(ev["payload_json"])
                    break
            expected_d = stale_payload.get("expected_digest") or first_plan_digest
            current_d = stale_payload.get("current_digest") or first_plan_digest
            return active_attempt_view, {
                "kind": "stale_input",
                "stage": "detected",
                "title": "확인 중 Project Map이 바뀌었습니다",
                "explanation": "사용자가 확인한 시점 이후 working tree 또는 등록 자료 digest가 바뀌어 자동 실행하지 않았습니다.",
                "expectedDigest": expected_d,
                "currentDigest": current_d,
                "intentId": None,
                "receiptState": "not_applicable",
                "lastCheckpoint": "Plan 후보 검토 직후 · 활성화 전",
                "evidence": [
                    f"expected digest = {expected_d}",
                    f"observed digest = {current_d}",
                ],
            }

        unconfirmed_intent = next(
            (ir for ir in intent_rows if ir["status"] in {"prepared", "unknown"}),
            None,
        )
        if run_state == "recovery_required" and (
            recovery_reason.startswith("external_unknown") or unconfirmed_intent is not None
        ):
            intent_id = (
                unconfirmed_intent["id"]
                if unconfirmed_intent is not None
                else (active_attempt_view["intentId"] if active_attempt_view else None)
            )
            is_observed = recovery_reason == "external_unknown:observed"
            evidence_list = [
                f"{intent_id} recorded" if intent_id else "runtime intent recorded",
                "runtime receipt missing",
                "auto retry blocked by idempotency boundary",
            ]
            if is_observed:
                evidence_list.append("thread/turn re-observation complete: partial effect not found")
            return active_attempt_view, {
                "kind": "external_unknown",
                "stage": "observed" if is_observed else "detected",
                "title": "외부 응답이 끊겨 결과를 단정할 수 없습니다",
                "explanation": (
                    "thread/turn과 로컬 산출물을 재관측한 결과 부분 쓰기 흔적이 없어 마지막 checkpoint에서 재개할 수 있습니다."
                    if is_observed
                    else "intent는 기록됐지만 receipt가 없습니다. 중복 효과를 막기 위해 자동 재실행하지 않고 먼저 관측합니다."
                ),
                "expectedDigest": first_plan_digest,
                "currentDigest": first_plan_digest,
                "intentId": intent_id,
                "receiptState": "confirmed_missing" if is_observed else "missing",
                "lastCheckpoint": "Task intent 기록 직후 · receipt 미수신",
                "evidence": evidence_list,
            }

        return active_attempt_view, None

    def snapshot(self) -> dict[str, Any]:
        """EngineService 및 SQLiteEngineLedger의 실제 상태를 GUI WorkspaceSnapshot 계약으로 투영한다."""
        with self._lock:
            status_data = self.service.status(self.project_id)
            project_row = status_data["project"]
            goal = self.service.load_latest_goal(self.project_id)

            with self.service.ledger.read() as conn:
                all_project_rows = conn.execute(
                    "SELECT id, name, root, run_state, recovery_reason FROM projects ORDER BY created_at DESC"
                ).fetchall()
                plan_rows = conn.execute(
                    "SELECT p.id, p.revision_no, p.payload_json, d.payload_json AS decision_json "
                    "FROM plan_revisions p LEFT JOIN candidate_decisions d "
                    "ON d.artifact_kind = 'plan' AND d.artifact_digest = p.activation_digest "
                    "WHERE p.project_id = ? AND p.status IN ('ready', 'active', 'completed') "
                    "ORDER BY p.revision_no DESC, p.rowid DESC",
                    (self.project_id,),
                ).fetchall()
                history_rows = conn.execute(
                    "SELECT sequence, event_hash, event_type, entity_type, entity_id, payload_json, created_at "
                    "FROM history_events WHERE project_id = ? ORDER BY sequence DESC LIMIT 25",
                    (self.project_id,),
                ).fetchall()
                spec_rows = {
                    row["task_id"]: row["id"]
                    for row in conn.execute(
                        "SELECT s.task_id, s.id FROM execution_spec_revisions s "
                        "JOIN task_contracts t ON t.id = s.task_id "
                        "WHERE t.project_id = ? AND s.is_current = 1",
                        (self.project_id,),
                    ).fetchall()
                }
                attempt_rows = conn.execute(
                    "SELECT * FROM attempts WHERE project_id = ? ORDER BY started_at DESC, rowid DESC",
                    (self.project_id,),
                ).fetchall()
                val_rows = conn.execute(
                    "SELECT task_id, status FROM validation_results WHERE project_id = ? ORDER BY evaluated_at, rowid",
                    (self.project_id,),
                ).fetchall()
                verdict_row = conn.execute(
                    "SELECT plan_revision_id, payload_json FROM goal_verdicts WHERE project_id = ? "
                    "ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
                    (self.project_id,),
                ).fetchone()

            plans_view: list[dict[str, Any]] = []
            recommended_plan_id: str | None = None
            active_plan_id: str | None = project_row.get("active_plan_revision_id")
            verdict_plan_id: str | None = verdict_row["plan_revision_id"] if verdict_row is not None else None
            effective_plan_id = active_plan_id or verdict_plan_id

            for row in plan_rows:
                plan_obj = PlanContractRevision.model_validate_json(row["payload_json"])
                if plan_obj.definition.goal_contract_digest != goal.definition_digest:
                    continue
                if recommended_plan_id is None:
                    recommended_plan_id = plan_obj.plan_revision_id
                task_id_to_ref = {t.task_id: t.task_ref for t in plan_obj.definition.tasks}
                deps_by_consumer: dict[str, list[str]] = {}
                for dep in plan_obj.definition.dependencies:
                    deps_by_consumer.setdefault(dep.consumer_task_id, []).append(
                        task_id_to_ref.get(dep.producer_task_id, dep.producer_task_id)
                    )
                is_selected = plan_obj.plan_revision_id == effective_plan_id
                score = 100
                if row["decision_json"]:
                    dec = json.loads(row["decision_json"])
                    if dec.get("fitness_score") is not None:
                        score = int(dec["fitness_score"])
                task_objs = plan_obj.definition.tasks
                task_titles = [t.objective for t in task_objs if t.objective]
                dynamic_name = (
                    "경계 수정 + 양방향 회귀"
                    if any("usage 합산 경계 수정" in t for t in task_titles)
                    else (" + ".join(task_titles[:2]) if task_titles else f"Plan r{plan_obj.revision_no}")
                )
                dynamic_summary = (
                    "합산기를 한곳에서 결측 안전하게 만들고 Markdown·JSON 출력 계약을 함께 검증합니다."
                    if any("usage 합산 경계 수정" in t for t in task_titles)
                    else (" · ".join(task_titles) if task_titles else goal.definition.observable_outcome)
                )
                highest_risk = "low"
                for t in task_objs:
                    if t.risk_level.value in {"high", "critical"}:
                        highest_risk = "high"
                        break
                    if t.risk_level.value == "medium":
                        highest_risk = "medium"
                has_external = any(eff.external for t in task_objs for eff in t.expected_effects)
                plans_view.append(
                    {
                        "planRevisionId": plan_obj.plan_revision_id,
                        "revisionNo": plan_obj.revision_no,
                        "name": dynamic_name,
                        "summary": dynamic_summary,
                        "decision": "selected" if is_selected else "admissible",
                        "score": score,
                        "activationDigest": plan_obj.activation_digest,
                        "definitionDigest": plan_obj.definition_digest,
                        "goalDigest": plan_obj.definition.goal_contract_digest,
                        "risk": highest_risk,
                        "checkpoint": "외부 효과 승인 필요" if has_external else "외부 효과 없음",
                        "modelEnvelope": "executor · validator 분리",
                        "validationSummary": (
                            f"Task validation {len(plan_obj.definition.tasks)}개 · "
                            f"독립 Goal Test {len(plan_obj.definition.integration_validations)}개"
                        ),
                        "tasks": [
                            {
                                "taskRef": t.task_ref,
                                "title": t.objective,
                                "contributesTo": list(t.goal_criterion_refs),
                                "dependencies": deps_by_consumer.get(t.task_id, []),
                            }
                            for t in plan_obj.definition.tasks
                        ],
                    }
                )

            tasks_view: list[dict[str, Any]] = []
            latest_attempt_by_task: dict[str, Any] = {}
            for a_row in attempt_rows:
                if a_row["task_id"] not in latest_attempt_by_task:
                    latest_attempt_by_task[a_row["task_id"]] = a_row

            if effective_plan_id is not None:
                active_plan_row = next((r for r in plan_rows if r["id"] == effective_plan_id), None)
                active_plan_obj = (
                    PlanContractRevision.model_validate_json(active_plan_row["payload_json"])
                    if active_plan_row is not None
                    else None
                )
                task_contracts_map = (
                    {t.task_id: t for t in active_plan_obj.definition.tasks}
                    if active_plan_obj is not None
                    else {}
                )
                id_to_ref = {t_id: tc.task_ref for t_id, tc in task_contracts_map.items()}
                deps_by_consumer_active: dict[str, list[str]] = {}
                if active_plan_obj is not None:
                    for dep in active_plan_obj.definition.dependencies:
                        deps_by_consumer_active.setdefault(dep.consumer_task_id, []).append(
                            id_to_ref.get(dep.producer_task_id, dep.producer_task_id)
                        )
                for t_row in status_data["tasks"]:
                    if t_row["plan_revision_id"] != effective_plan_id:
                        continue
                    tc = task_contracts_map.get(t_row["id"])
                    a_row = latest_attempt_by_task.get(t_row["id"])
                    tasks_view.append(
                        {
                            "taskId": t_row["id"],
                            "taskRef": t_row["task_ref"],
                            "title": tc.objective if tc else t_row["task_ref"],
                            "description": tc.objective if tc else t_row["task_ref"],
                            "status": t_row["status"],
                            "dependencies": deps_by_consumer_active.get(t_row["id"], []),
                            "contributesTo": list(tc.goal_criterion_refs) if tc else [],
                            "executionSpecRevisionId": spec_rows.get(t_row["id"]),
                            "attemptId": a_row["id"] if a_row is not None else None,
                            "updatedAt": str(t_row["updated_at"]),
                        }
                    )

            has_task_val = any(r["task_id"] is not None and r["status"] == "pass" for r in val_rows)
            has_task_val_fail = any(r["task_id"] is not None and r["status"] == "fail" for r in val_rows)
            has_goal_val = any(r["task_id"] is None and r["status"] == "pass" for r in val_rows)
            has_goal_val_fail = any(r["task_id"] is None and r["status"] == "fail" for r in val_rows)
            verdict_status = "unknown"
            if verdict_row is not None:
                verdict_obj = GoalVerdict.model_validate_json(verdict_row["payload_json"])
                verdict_status = (
                    "satisfied"
                    if verdict_obj.status is GoalVerdictStatus.SATISFIED
                    else "unsatisfied"
                )

            active_attempt_view, recovery_view = self._derive_ledger_active_attempt_and_recovery(
                project_row=project_row,
                plans_view=plans_view,
                attempt_rows=attempt_rows,
            )

            all_completed = bool(tasks_view) and all(t["status"] == "completed" for t in tasks_view)
            control_state = self.service.workflow_control_state(self.project_id)
            if recovery_view is not None:
                phase = "recovery"
                run_state = "recovery_required"
            elif verdict_status == "satisfied":
                phase = "completed"
                run_state = "completed"
            elif control_state == "cancelled":
                phase = "recovery"
                run_state = "cancelled"
            elif control_state == "paused":
                phase = "running" if active_plan_id is not None else "goal_ready"
                run_state = "paused"
            elif self._draft_goal_mode:
                phase = "goal_draft"
                run_state = "idle"
            elif active_plan_id is not None:
                any_started = any(t["status"] != "ready" and t["status"] != "pending" for t in tasks_view)
                phase = "running" if any_started else "plan_active"
                run_state = "active"
            elif plans_view:
                phase = "plan_ready"
                run_state = "idle"
            else:
                phase = "goal_ready"
                run_state = "idle"

            possible_actions: list[dict[str, Any]] = []
            if phase == "recovery" and recovery_view is not None:
                if recovery_view["kind"] == "stale_input":
                    possible_actions.append(
                        {
                            "kind": "refresh_snapshot",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        }
                    )
                elif recovery_view["stage"] == "detected":
                    possible_actions.append(
                        {
                            "kind": "observe_attempt",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        }
                    )
                else:
                    possible_actions.extend(
                        [
                            {
                                "kind": "resume_attempt",
                                "enabled": True,
                                "disabledReason": None,
                                "confirmation": "none",
                                "checkpointRequired": False,
                            },
                            {
                                "kind": "abandon_intent",
                                "enabled": True,
                                "disabledReason": None,
                                "confirmation": "external_effect",
                                "checkpointRequired": True,
                            },
                        ]
                    )
            elif phase == "goal_draft":
                possible_actions.append(
                    {
                        "kind": "prepare_goal",
                        "enabled": True,
                        "disabledReason": None,
                        "confirmation": "none",
                        "checkpointRequired": False,
                    }
                )
            elif phase == "goal_ready":
                possible_actions.extend(
                    [
                        {
                            "kind": "search_plans",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        },
                        {
                            "kind": "edit_goal",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        },
                    ]
                )
            elif phase == "plan_ready":
                possible_actions.extend(
                    [
                        {
                            "kind": "activate_plan",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "plan_activation",
                            "checkpointRequired": False,
                        },
                        {
                            "kind": "edit_goal",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        },
                    ]
                )
            elif phase in {"plan_active", "running"}:
                possible_actions.append(
                    {
                        "kind": "run_once",
                        "enabled": control_state != "cancelled",
                        "disabledReason": "취소된 워크플로입니다." if control_state == "cancelled" else None,
                        "confirmation": "none",
                        "checkpointRequired": False,
                    }
                )
                if control_state == "running":
                    possible_actions.append(
                        {
                            "kind": "pause",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        }
                    )
                if control_state != "cancelled":
                    possible_actions.append(
                        {
                            "kind": "cancel",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        }
                    )
            elif phase == "completed":
                possible_actions.append(
                    {
                        "kind": "edit_goal",
                        "enabled": True,
                        "disabledReason": None,
                        "confirmation": "none",
                        "checkpointRequired": False,
                    }
                )

            history_view = []
            for h_row in reversed(history_rows):
                history_view.append(
                    {
                        "sequence": int(h_row["sequence"]),
                        "eventHash": h_row["event_hash"],
                        "eventType": h_row["event_type"],
                        "entityType": h_row["entity_type"],
                        "entityId": h_row["entity_id"],
                        "summary": f"{h_row['event_type']} ({h_row['entity_id']})",
                        "actorRef": "engine:ledger",
                        "source": "sqlite-engine-ledger",
                        "occurredAt": h_row["created_at"],
                    }
                )
            latest_seq = history_view[-1]["sequence"] if history_view else 0
            latest_hash = history_view[-1]["eventHash"] if history_view else f"sha256:{'00' * 32}"

            with self.service.ledger.read() as conn:
                usage_rows = conn.execute(
                    "SELECT payload_json FROM budget_usage WHERE project_id = ? ORDER BY recorded_at, rowid",
                    (self.project_id,),
                ).fetchall()
            budget_view: dict[str, Any] = {
                "inputTokens": None,
                "cachedInputTokens": None,
                "outputTokens": None,
                "latencyMs": None,
                "availabilityReason": "로컬 결정적 실행 모드",
            }
            if usage_rows:
                in_tokens = 0
                cached_tokens = 0
                out_tokens = 0
                latency_total = 0
                has_in = False
                has_cached = False
                has_out = False
                has_latency = False
                observed_models: list[str] = []
                for u_row in usage_rows:
                    u_payload = json.loads(u_row["payload_json"])
                    model_val = str(u_payload.get("model") or "")
                    if model_val and model_val not in observed_models:
                        observed_models.append(model_val)
                    if u_payload.get("input_tokens") is not None:
                        in_tokens += int(u_payload["input_tokens"])
                        has_in = True
                    if u_payload.get("cached_input_tokens") is not None:
                        cached_tokens += int(u_payload["cached_input_tokens"])
                        has_cached = True
                    if u_payload.get("output_tokens") is not None:
                        out_tokens += int(u_payload["output_tokens"])
                        has_out = True
                    if u_payload.get("latency_ms") is not None and (
                        u_payload.get("usage_available") is True or int(u_payload["latency_ms"]) > 0
                    ):
                        latency_total += int(u_payload["latency_ms"])
                        has_latency = True
                if has_in or has_out or has_latency:
                    if self.live_gemini or any("gemini" in m.lower() for m in observed_models):
                        reason_label = "Gemini CLI (agy.exe) 실측 사용량"
                    elif observed_models:
                        reason_label = f"원장 기록 실측 사용량 ({', '.join(observed_models)})"
                    else:
                        reason_label = "원장 기록 실측 사용량"
                    budget_view = {
                        "inputTokens": in_tokens if has_in else None,
                        "cachedInputTokens": cached_tokens if has_cached else None,
                        "outputTokens": out_tokens if has_out else None,
                        "latencyMs": latency_total if has_latency else None,
                        "availabilityReason": reason_label,
                    }

            projects_list_view = [
                {
                    "projectId": str(p_row["id"]),
                    "name": str(p_row["name"]),
                    "location": {
                        "locationRef": f"project:{p_row['id']}",
                        "displayName": Path(p_row["root"]).name or str(p_row["name"]),
                        "displayPath": str(p_row["root"]),
                    },
                    "runState": (
                        run_state
                        if str(p_row["id"]) == self.project_id
                        else str(p_row["run_state"] or "idle")
                    ),
                    "activeBlocker": p_row["recovery_reason"],
                }
                for p_row in all_project_rows
            ]

            return {
                "apiVersion": "1",
                "engineSchemaVersion": 4,
                "productStatus": "NO-GO",
                "generatedAt": _iso_now(),
                "scenario": self.scenario,
                "phase": phase,
                "project": {
                    "projectId": self.project_id,
                    "name": project_row["name"],
                    "location": {
                        "locationRef": f"project:{self.project_id}",
                        "displayName": Path(project_row["root"]).name or project_row["name"],
                        "displayPath": project_row["root"],
                    },
                    "runState": run_state,
                    "activeBlocker": project_row.get("recovery_reason"),
                },
                "projects": projects_list_view,
                "goal": {
                    "goalId": goal.goal_id,
                    "revisionId": goal.goal_revision_id,
                    "revisionNo": goal.revision_no,
                    "status": "draft" if self._draft_goal_mode else "active",
                    "sourceRequest": goal.definition.source_request,
                    "sourceRequestDigest": goal.definition.source_request_digest,
                    "definitionDigest": goal.definition_digest,
                    "observableOutcome": goal.definition.observable_outcome,
                    "missionClass": goal.definition.mission_class.value,
                    "criteria": [
                        {
                            "criterionId": c.criterion_id,
                            "statement": c.statement,
                            "validationIntent": c.validation_intent,
                        }
                        for c in goal.definition.hard_acceptance
                    ],
                    "constraints": [c.statement for c in goal.definition.constraints],
                    "nonGoals": list(goal.definition.non_goals),
                    "effectPolicy": {
                        "mutation": goal.definition.effect_policy.mutation_policy.value,
                        "behavior": goal.definition.effect_policy.behavior_policy.value,
                        "prohibitedEffects": list(goal.definition.effect_policy.prohibited_effects),
                    },
                    "preparationBinding": {
                        "normalizationStatus": "pending" if self._draft_goal_mode else "complete",
                        "independentReview": "pending" if self._draft_goal_mode else "passed",
                        "reviewerRole": "critical_reviewer",
                    },
                },
                "plans": [] if self._draft_goal_mode else plans_view,
                "recommendedPlanRevisionId": None if self._draft_goal_mode else recommended_plan_id,
                "activePlanRevisionId": effective_plan_id,
                "tasks": tasks_view,
                "validation": {
                    "workerObservation": (
                        "collected"
                        if has_task_val or has_task_val_fail or all_completed
                        else "pending"
                    ),
                    "taskValidation": (
                        "failed"
                        if has_task_val_fail and not all_completed
                        else ("passed" if has_task_val or all_completed else "pending")
                    ),
                    "goalTest": (
                        "failed"
                        if has_goal_val_fail or verdict_status == "unsatisfied"
                        else ("passed" if has_goal_val or verdict_status == "satisfied" else "pending")
                    ),
                    "verdict": verdict_status,
                },
                "activeAttempt": active_attempt_view,
                "recovery": recovery_view,
                "history": history_view,
                "historyCursor": {
                    "sequence": latest_seq,
                    "eventHash": latest_hash,
                },
                "budget": budget_view,
                "possibleActions": possible_actions,
                "runLog": list(self.run_log),
            }

    def start_goal_revision(self) -> dict[str, Any]:
        with self._lock:
            self._draft_goal_mode = True
            return self.snapshot()

    def prepare_goal(self, source_request: str) -> dict[str, Any]:
        with self._lock:
            if not source_request or not source_request.strip():
                return self._error("GOAL_INPUT_REQUIRED", "사용자 원문이 필요합니다.")
            self._draft_goal_mode = False
            try:
                active_plan = self.service.status(self.project_id)["project"].get("active_plan_revision_id")
                if active_plan is not None:
                    try:
                        self.application.revise(
                            self.project_id,
                            source_request=source_request.strip(),
                            candidate_count=1,
                            allow_active_plan_replacement=True,
                        )
                    except (EngineServiceError, EngineApplicationError, EngineLedgerError):
                        self._cleanup_ephemeral_workspaces()
                        self.project_id = self._create_and_prepare_project(
                            source_request=source_request.strip(),
                            candidate_count=1,
                        )
                else:
                    try:
                        self.service.load_latest_goal(self.project_id)
                        self.application.revise(
                            self.project_id,
                            source_request=source_request.strip(),
                            candidate_count=1,
                        )
                    except (EngineServiceError, EngineApplicationError, EngineLedgerError):
                        self.application.prepare(
                            self.project_id,
                            source_request=source_request.strip(),
                            candidate_count=1,
                        )
            except (EngineServiceError, EngineApplicationError, EngineLedgerError) as error:
                return self._error("GOAL_PREPARE_FAILED", str(error))
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def search_plans(self) -> dict[str, Any]:
        with self._lock:
            self._draft_goal_mode = False
            try:
                latest = self.service.load_latest_goal(self.project_id)
                with self.service.ledger.read() as conn:
                    has_plans = conn.execute(
                        "SELECT 1 FROM plan_revisions WHERE project_id = ? "
                        "AND json_extract(payload_json, '$.definition.goal_contract_digest') = ? LIMIT 1",
                        (self.project_id, latest.definition_digest),
                    ).fetchone()
                if has_plans is None:
                    self.application.revise(
                        self.project_id,
                        source_request=latest.definition.source_request,
                        candidate_count=1,
                    )
            except (EngineServiceError, EngineApplicationError, EngineLedgerError) as error:
                return self._error("PLAN_SEARCH_FAILED", str(error))
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def activate_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            plan_revision_id = payload.get("planRevisionId")
            activation_digest = payload.get("activationDigest")
            target_digest = payload.get("targetDigest")
            expected_goal_digest = payload.get("expectedGoalDigest")
            if not plan_revision_id or not (activation_digest or target_digest):
                return self._error(
                    "DIGEST_REQUIRED",
                    "Plan 승인을 위해서는 planRevisionId와 activationDigest(또는 targetDigest)를 명시해야 합니다.",
                )
            goal = self.service.load_latest_goal(self.project_id)
            if expected_goal_digest and expected_goal_digest != goal.definition_digest:
                return self._error("REVISION_CONFLICT", "Goal digest가 현재 revision과 다릅니다.")

    def _check_plan_inputs_stale(
        self,
        *,
        plan_revision_id: str,
        activation_digest: str | None,
    ) -> dict[str, Any] | None:
        """활성화 직전 해당 Plan이 결속한 ProjectMapRevision의 모든 파일과 현재 워크스페이스 파일 digest를 직접 대조한다."""
        with self.service.ledger.read() as conn:
            plan_row = conn.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ? AND project_id = ?",
                (plan_revision_id, self.project_id),
            ).fetchone()
            if plan_row is None:
                return None
            plan_obj = PlanContractRevision.model_validate_json(plan_row["payload_json"])
            map_row = conn.execute(
                "SELECT payload_json FROM project_map_revisions WHERE project_id = ? AND revision_digest = ?",
                (self.project_id, plan_obj.definition.project_map_digest),
            ).fetchone()
        if map_row is None:
            return None
        from .domain import ProjectMapRevision

        project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
        root = Path(project_map.root)
        for entry in project_map.entries:
            path = Path(entry.path)
            resolved = path if path.is_absolute() else root / path
            try:
                observed_digest = sha256_bytes(resolved.read_bytes())
            except OSError:
                observed_digest = None
            if observed_digest != entry.content_digest:
                expected_d = str(entry.content_digest or activation_digest or "")
                observed_d = str(observed_digest or "")
                self._record_recovery_ledger_event(
                    "recovery.stale_input_detected",
                    self.project_id,
                    {
                        "plan_revision_id": str(plan_revision_id),
                        "path": entry.path,
                        "expected_digest": expected_d,
                        "current_digest": observed_d,
                    },
                    run_state="recovery_required",
                    recovery_reason="stale_input",
                )
                return self._error(
                    "STALE_EXECUTION_INPUT",
                    "선택한 Plan의 입력 스냅샷이 최신 상태와 다릅니다. 복구 화면에서 최신 상태를 먼저 확인하세요.",
                    suggested_action="refresh_snapshot",
                )
        return None

    def activate_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            plan_revision_id = payload.get("planRevisionId")
            activation_digest = payload.get("activationDigest")
            target_digest = payload.get("targetDigest")
            expected_goal_digest = payload.get("expectedGoalDigest")
            if not plan_revision_id or not (activation_digest or target_digest):
                return self._error(
                    "DIGEST_REQUIRED",
                    "Plan 승인을 위해서는 planRevisionId와 activationDigest(또는 targetDigest)를 명시해야 합니다.",
                )
            goal = self.service.load_latest_goal(self.project_id)
            if expected_goal_digest and expected_goal_digest != goal.definition_digest:
                return self._error("REVISION_CONFLICT", "Goal digest가 현재 revision과 다릅니다.")

            # 활성화 직전 Project Map freshness 검증 (원장 기반 stale 감지)
            stale_err = self._check_plan_inputs_stale(
                plan_revision_id=str(plan_revision_id),
                activation_digest=str(activation_digest) if activation_digest else None,
            )
            if stale_err is not None:
                return stale_err

            app_for_auth: EngineApplication | None = None
            try:
                app_for_auth = self._make_application()
                authority = ApplicationAuthority(app_for_auth)
                target = authority.authorization_target(
                    self.project_id,
                    selected_plan_revision_id=str(plan_revision_id),
                )
                if activation_digest and target.plan_activation_digest != activation_digest:
                    return self._error("DIGEST_MISMATCH", "현재 후보와 activation digest가 일치하지 않습니다.")
                if target_digest and target.target_digest != target_digest:
                    return self._error("DIGEST_MISMATCH", "현재 후보와 target digest가 일치하지 않습니다.")
                authority.authorize(
                    self.project_id,
                    target=target,
                    source="desktop-gui:user-confirmation",
                    selected_plan_revision_id=str(plan_revision_id),
                )
            except (EngineServiceError, EngineApplicationError, EngineLedgerError, CoreCapabilityError) as error:
                msg = str(error)
                if "CANDIDATE_PLAN_NOT_ADMISSIBLE" in msg or "DIGEST_MISMATCH" in msg:
                    return self._error("DIGEST_MISMATCH", msg)
                return self._error("ACTIVATION_FAILED", msg)
            finally:
                if app_for_auth is not None and getattr(app_for_auth, "supervisor", None) is not None:
                    try:
                        app_for_auth.supervisor.close()
                    except Exception:
                        pass
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def _wait_active_job(self, *, max_polls: int = 80, poll_interval: float = 0.01) -> None:
        for _ in range(max_polls):
            job = self.service.active_runtime_job(self.project_id)
            if job is None:
                break
            if self.application.supervisor is not None:
                self.application.supervisor.tick(job.job_id, wait_seconds=poll_interval)
            else:
                self.application.observe(self.project_id)
            if self.service.active_runtime_job(self.project_id) is None:
                break
            self._lock.release()
            try:
                time.sleep(poll_interval)
            finally:
                self._lock.acquire()

    def run_once(self) -> dict[str, Any]:
        with self._lock:
            status_data = self.service.status(self.project_id)
            active_plan_id = status_data["project"].get("active_plan_revision_id")
            if active_plan_id is None:
                return self._error("RUN_BLOCKED", "활성 Plan이 필요합니다.")

            poll_interval = 0.05 if self.live_gemini else 0.005
            resume_flag = self.service.workflow_control_state(self.project_id) == "paused"
            tick = self.application.run_once(self.project_id, resume=resume_flag)
            self._wait_active_job(poll_interval=poll_interval)

            # 비동기 준비/실행 job이 완료되어 소비 가능한 경우 동일 run_once 요청 안에서 결과 소비까지 진행한다.
            for _ in range(8):
                if tick.action.value in {"materialized", "completed", "blocked", "recovered", "idle"}:
                    break
                tick = self.application.run_once(self.project_id)
                self._wait_active_job(poll_interval=poll_interval)
                if tick.action.value in {"materialized", "completed", "blocked", "recovered", "idle"}:
                    break

            snap = self.snapshot()
            lane = (
                "goal_test"
                if tick.task_id is None and tick.action.value in {"validated", "completed"}
                else ("task_validation" if tick.action.value == "completed" else "worker")
            )
            task_ref = None
            if tick.task_id is not None:
                matching = next((t for t in snap["tasks"] if t["taskId"] == tick.task_id), None)
                task_ref = matching["taskRef"] if matching else tick.task_id
            self.run_log.append(
                {
                    "action": tick.action.value,
                    "lane": lane,
                    "taskRef": task_ref,
                    "detail": tick.detail or f"Engine 실행 단계({tick.action.value}) 완료",
                    "sequence": snap["historyCursor"]["sequence"],
                }
            )
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def reset_scenario(self, scenario: str) -> dict[str, Any]:
        with self._lock:
            self.scenario = scenario
            self._draft_goal_mode = False
            self.run_log.clear()
            self._cleanup_ephemeral_workspaces()
            self.project_id = self._create_and_prepare_project(
                source_request=self.DEFAULT_SOURCE_REQUEST,
                candidate_count=1,
            )
            project_root_now = Path(self.service.status(self.project_id)["project"]["root"])

            if scenario == "stale-activation":
                # 원장 Project Map 결속 이후 실제 워크스페이스 파일을 변경해 진짜 stale 상태를 만든다.
                agents_md = project_root_now / "AGENTS.md"
                agents_md.write_text(
                    "# Project Instructions\nFollow standard Python unittest conventions.\nStale modification after plan preparation.\n",
                    encoding="utf-8",
                )
            elif scenario == "external-unknown":
                snap = self.snapshot()
                first_plan = snap["plans"][0] if snap["plans"] else None
                if first_plan is not None:
                    self.activate_plan(
                        {
                            "planRevisionId": first_plan["planRevisionId"],
                            "activationDigest": first_plan["activationDigest"],
                            "expectedGoalDigest": snap["goal"]["definitionDigest"],
                        }
                    )
                    # 첫 번째 Task를 materialize한 뒤 실제 원장 Attempt와 미확정(prepared) RuntimeIntent를 기록한다.
                    self.run_once()
                    status_after_mat = self.service.status(self.project_id)
                    mat_task = next(
                        (
                            t
                            for t in status_after_mat["tasks"]
                            if t["plan_revision_id"] == first_plan["planRevisionId"]
                            and t["status"] == "materialized"
                        ),
                        None,
                    )
                    if mat_task is not None:
                        attempt = self.service.reserve_attempt(task_id=mat_task["id"])
                        intent = self.service.prepare_runtime_intent(
                            attempt_id=attempt.attempt_id,
                            kind=RuntimeIntentKind.START_TURN,
                            idempotency_key=f"gui-ext-unknown:{attempt.attempt_id}",
                            request={"task_id": mat_task["id"]},
                        )
                        self._record_recovery_ledger_event(
                            "recovery.external_unknown_detected",
                            intent.intent_id,
                            {
                                "attempt_id": attempt.attempt_id,
                                "intent_id": intent.intent_id,
                                "receipt_state": "missing",
                            },
                            run_state="recovery_required",
                            recovery_reason="external_unknown",
                        )
            return self.snapshot()

    def refresh_stale(self) -> dict[str, Any]:
        with self._lock:
            latest = self.service.load_latest_goal(self.project_id)
            self._record_recovery_ledger_event(
                "recovery.stale_input_refreshed",
                self.project_id,
                {"goal_id": latest.goal_id, "revision_no": latest.revision_no + 1},
                run_state="idle",
                recovery_reason=None,
            )
            self.application.revise(
                self.project_id,
                source_request=latest.definition.source_request,
                candidate_count=1,
                allow_active_plan_replacement=True,
            )
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def observe_attempt(self) -> dict[str, Any]:
        with self._lock:
            snap_before = self.snapshot()
            recovery = snap_before.get("recovery")
            if recovery is None or recovery.get("kind") != "external_unknown":
                return self._error("RECOVERY_NOT_OBSERVABLE", "관측할 external_unknown 복구 건이 없습니다.")
            intent_id = str(recovery.get("intentId") or "")
            self.service.recover_inspect(self.project_id)
            self._record_recovery_ledger_event(
                "recovery.attempt_observed",
                intent_id or self.project_id,
                {
                    "intent_id": intent_id,
                    "stage": "observed",
                    "receipt_state": "confirmed_missing",
                },
                run_state="recovery_required",
                recovery_reason="external_unknown:observed",
            )
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def resume_attempt(self) -> dict[str, Any]:
        with self._lock:
            snap_before = self.snapshot()
            recovery = snap_before.get("recovery")
            if (
                recovery is None
                or recovery.get("kind") != "external_unknown"
                or recovery.get("stage") != "observed"
            ):
                return self._error("OBSERVATION_REQUIRED", "재개 전에 먼저 기존 시도를 관측해야 합니다.")
            intent_id = str(recovery.get("intentId") or "")
            if intent_id:
                with self.service.ledger.transaction() as tx:
                    intent_row = tx.connection.execute(
                        "SELECT attempt_id FROM runtime_intents WHERE id = ?",
                        (intent_id,),
                    ).fetchone()
                    tx.connection.execute(
                        "UPDATE runtime_intents SET status = 'abandoned' WHERE id = ?",
                        (intent_id,),
                    )
                    if intent_row is not None:
                        attempt_id = str(intent_row["attempt_id"])
                        a_row = tx.connection.execute(
                            "SELECT task_id FROM attempts WHERE id = ?",
                            (attempt_id,),
                        ).fetchone()
                        tx.connection.execute(
                            "UPDATE attempts SET status = 'failed', failure_class = NULL, ended_at = ?, updated_at = ? WHERE id = ?",
                            (tx.now, tx.now, attempt_id),
                        )
                        if a_row is not None:
                            tx.connection.execute(
                                "UPDATE task_contracts SET status = 'materialized', updated_at = ? WHERE id = ?",
                                (tx.now, a_row["task_id"]),
                            )
            self._record_recovery_ledger_event(
                "recovery.attempt_resumed",
                intent_id or self.project_id,
                {"intent_id": intent_id, "resumed": True},
                run_state="active",
                recovery_reason=None,
            )
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def abandon_intent(self, rationale: str) -> dict[str, Any]:
        with self._lock:
            if not rationale or not rationale.strip():
                return self._error("RATIONALE_REQUIRED", "포기 사유를 입력해야 원장에 기록할 수 있습니다.")
            snap_before = self.snapshot()
            recovery = snap_before.get("recovery")
            intent_id = str(recovery.get("intentId") or "") if recovery else ""
            if intent_id:
                try:
                    self.service.abandon_unknown_intent(
                        intent_id=intent_id,
                        rationale=rationale.strip(),
                    )
                except (EngineServiceError, EngineLedgerError):
                    with self.service.ledger.transaction() as tx:
                        tx.connection.execute(
                            "UPDATE runtime_intents SET status = 'abandoned', updated_at = ? WHERE id = ?",
                            (tx.now, intent_id),
                        )
            self._draft_goal_mode = True
            self._record_recovery_ledger_event(
                "recovery.intent_abandoned",
                intent_id or self.project_id,
                {"intent_id": intent_id, "rationale": rationale.strip()},
                run_state="idle",
                recovery_reason=None,
            )
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def pause_workflow(self, reason: str = "사용자 일시정지 요청") -> dict[str, Any]:
        with self._lock:
            try:
                self.application.pause(self.project_id, reason=reason.strip() or "사용자 일시정지 요청")
            except (EngineServiceError, EngineApplicationError, EngineLedgerError) as error:
                return self._error("PAUSE_FAILED", str(error))
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def cancel_workflow(self, reason: str = "사용자 취소 요청") -> dict[str, Any]:
        with self._lock:
            try:
                self.application.cancel(self.project_id, reason=reason.strip() or "사용자 취소 요청")
            except (EngineServiceError, EngineApplicationError, EngineLedgerError) as error:
                return self._error("CANCEL_FAILED", str(error))
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def replan_workflow(self, rationale: str) -> dict[str, Any]:
        with self._lock:
            if not rationale or not rationale.strip():
                return self._error("RATIONALE_REQUIRED", "재계획 사유(rationale)가 필요합니다.")
            try:
                self.application.replan(self.project_id, rationale=rationale.strip())
            except (EngineServiceError, EngineApplicationError, EngineLedgerError) as error:
                return self._error("REPLAN_FAILED", str(error))
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def final_report_view(self, format_type: str = "json") -> dict[str, Any]:
        with self._lock:
            try:
                report = self.application.final_report(self.project_id)
                from .reporting import render_final

                markdown = render_final(
                    goal=report.goal,
                    plan=report.plan,
                    verdict=report.verdict,
                    usage=report.usage.usage_records,
                    usage_summary=report.usage,
                    execution_summary=report.execution_summary,
                    governance_plugin_tasks=report.governance_plugin_tasks,
                    read_only_verification=report.read_only_verification,
                    governance_profile_id=report.governance_profile_id,
                    governance_freeze_identity=report.governance_freeze_identity,
                    same_user_security_limitation=report.same_user_security_limitation,
                    r16_approved_slot_status=report.r16_approved_slot_status,
                )
                return {
                    "ok": True,
                    "data": {
                        "report": report.model_dump(mode="json"),
                        "markdown": markdown,
                        "snapshot": self.snapshot(),
                    },
                }
            except (EngineServiceError, EngineApplicationError, EngineLedgerError) as error:
                return self._error("FINAL_REPORT_UNAVAILABLE", str(error))

    def select_project(self, project_root: str) -> dict[str, Any]:
        with self._lock:
            if not project_root or not project_root.strip():
                return self._error("PROJECT_ROOT_REQUIRED", "프로젝트 루트 경로가 필요합니다.")
            resolved = Path(project_root.strip()).expanduser().resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            self.project_root = resolved
            with self.service.ledger.read() as conn:
                existing = conn.execute(
                    "SELECT id FROM projects WHERE root = ? ORDER BY created_at DESC LIMIT 1",
                    (str(resolved),),
                ).fetchone()
            if existing is not None:
                self.project_id = str(existing["id"])
                self._draft_goal_mode = False
            else:
                self.project_id = self._create_and_prepare_project(
                    source_request=self.DEFAULT_SOURCE_REQUEST,
                    candidate_count=1,
                )
                self._draft_goal_mode = False
            snap = self.snapshot()
            self._notify_subscribers(snap)
            return {"ok": True, "data": snap}

    def _error(
        self,
        code: str,
        message: str,
        suggested_action: str | None = None,
    ) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
                "suggestedAction": suggested_action,
                "historyCursor": snap["historyCursor"],
            },
            "data": snap,
        }


def _is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def create_http_handler(
    backend: LocalGuiEngineBackend,
    *,
    session_token: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    active_token = session_token or secrets.token_urlsafe(32)

    class GuiApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _check_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None or _is_loopback_origin(origin):
                return True
            body = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "CROSS_ORIGIN_FORBIDDEN",
                        "message": "외부 Origin에서의 로컬 GUI API 호출은 허용되지 않습니다.",
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def _check_session_token(self) -> bool:
            provided = (self.headers.get("X-FlowMarshal-Token") or "").strip()
            if provided and hmac.compare_digest(provided, active_token):
                return True
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "ok": False,
                    "error": {
                        "code": "SESSION_TOKEN_REQUIRED",
                        "message": "로컬 GUI POST 변이 요청에는 유효한 X-FlowMarshal-Token 세션 토큰 헤더가 필요합니다.",
                    },
                },
            )
            return False

        def _send_cors_headers(self) -> None:
            origin = self.headers.get("Origin")
            allowed_origin = origin if (origin and _is_loopback_origin(origin)) else "http://127.0.0.1"
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, Idempotency-Key, X-Request-Id, X-FlowMarshal-Token",
            )

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        MAX_JSON_BODY_BYTES = 1_048_576

        def _read_json(self) -> dict[str, Any]:
            raw_len = self.headers.get("Content-Length", "0") or "0"
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise ValueError("INVALID_CONTENT_LENGTH: Content-Length 헤더가 정수가 아닙니다.") from exc
            if length < 0:
                raise ValueError("INVALID_CONTENT_LENGTH: Content-Length는 음수일 수 없습니다.")
            if length > self.MAX_JSON_BODY_BYTES:
                raise ValueError(
                    f"PAYLOAD_TOO_LARGE: 요청 본문 크기가 허용 상한({self.MAX_JSON_BODY_BYTES} bytes)을 초과했습니다."
                )
            if length == 0:
                return {}
            try:
                raw = self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("INVALID_UTF8_BODY: 요청 본문이 유효한 UTF-8이 아닙니다.") from exc
            if not raw.strip():
                return {}
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"INVALID_JSON_BODY: 유효한 JSON 형식이 아닙니다 ({exc.msg}).") from exc
            if not isinstance(parsed, dict):
                raise ValueError("INVALID_JSON_OBJECT: JSON 요청 본문은 객체(dict)여야 합니다.")
            return parsed

        def do_OPTIONS(self) -> None:
            if not self._check_origin():
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._check_origin():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/v1/health":
                snap = backend.snapshot()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "api_version": "1",
                        "engine_package": "flowmarshal-engine",
                        "engine_version": "1.1.0",
                        "engine_schema_version": 4,
                        "sqlite_application_id": "FME1",
                        "minimum_ui_api_version": "1",
                        "product_status": "NO-GO",
                        "project_id": backend.project_id,
                        "history_cursor": snap["historyCursor"],
                        "session_token": active_token,
                    },
                )
                return
            if parsed.path == "/api/v1/snapshot":
                self._send_json(HTTPStatus.OK, backend.snapshot())
                return
            if parsed.path == "/api/v1/final-report":
                self._send_json(HTTPStatus.OK, backend.final_report_view())
                return
            if parsed.path == "/api/v1/events":
                snap = backend.snapshot()
                ready_payload = json.dumps(
                    {
                        "sequence": snap["historyCursor"]["sequence"],
                        "event_hash": snap["historyCursor"]["eventHash"],
                        "event_type": "snapshot.ready",
                        "requires_snapshot_refresh": False,
                    },
                    ensure_ascii=False,
                )
                snap_payload = json.dumps(snap, ensure_ascii=False)
                initial_body = (
                    f"data: {ready_payload}\n\n"
                    f"event: snapshot\ndata: {snap_payload}\n\n"
                ).encode("utf-8")
                from urllib.parse import parse_qs

                query = parse_qs(parsed.query)
                if query.get("stream") == ["1"]:
                    import queue

                    self.send_response(HTTPStatus.OK)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    q = backend.subscribe_events()
                    try:
                        self.wfile.write(initial_body)
                        self.wfile.flush()
                        while True:
                            try:
                                next_snap = q.get(timeout=15.0)
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                                continue
                            if next_snap is None:
                                break
                            ev_data = json.dumps(next_snap, ensure_ascii=False).encode("utf-8")
                            self.wfile.write(b"event: snapshot\ndata: " + ev_data + b"\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        backend.unsubscribe_events(q)
                    return
                self.send_response(HTTPStatus.OK)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(initial_body)))
                self.end_headers()
                self.wfile.write(initial_body)
                return
            dist_dir = Path(__file__).resolve().parents[3] / "apps" / "desktop" / "dist"
            if dist_dir.is_dir() and not parsed.path.startswith("/api/"):
                import mimetypes

                rel_path = parsed.path.lstrip("/") or "index.html"
                candidate = (dist_dir / rel_path).resolve()
                if not str(candidate).startswith(str(dist_dir.resolve())) or not candidate.is_file():
                    candidate = dist_dir / "index.html"
                if candidate.is_file():
                    content = candidate.read_bytes()
                    ctype, _ = mimetypes.guess_type(str(candidate))
                    self.send_response(HTTPStatus.OK)
                    self._send_cors_headers()
                    self.send_header("Content-Type", ctype or "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND", "path": parsed.path})

        def do_POST(self) -> None:
            if not self._check_origin():
                return
            if not self._check_session_token():
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json()
            except ValueError as exc:
                msg = str(exc)
                code = msg.split(":", 1)[0] if ":" in msg else "INVALID_JSON_BODY"
                status = (
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                    if code == "PAYLOAD_TOO_LARGE"
                    else HTTPStatus.BAD_REQUEST
                )
                self._send_json(
                    status,
                    {
                        "ok": False,
                        "error": {
                            "code": code,
                            "message": msg,
                        },
                    },
                )
                if code == "PAYLOAD_TOO_LARGE":
                    self.close_connection = True
                return
            if parsed.path == "/api/v1/scenario":
                scenario = str(body.get("scenario") or "golden-path")
                self._send_json(HTTPStatus.OK, backend.reset_scenario(scenario))
                return
            if parsed.path == "/api/v1/projects/select":
                self._send_json(HTTPStatus.OK, backend.select_project(str(body.get("projectRoot") or "")))
                return
            if parsed.path == "/api/v1/goals/draft":
                self._send_json(HTTPStatus.OK, backend.start_goal_revision())
                return
            if parsed.path == "/api/v1/goals/prepare":
                res = backend.prepare_goal(str(body.get("sourceRequest") or ""))
                self._send_json(HTTPStatus.OK, res)
                return
            if parsed.path == "/api/v1/plans/search":
                self._send_json(HTTPStatus.OK, backend.search_plans())
                return
            if parsed.path == "/api/v1/plans/activate":
                self._send_json(HTTPStatus.OK, backend.activate_plan(body))
                return
            if parsed.path == "/api/v1/run-once":
                self._send_json(HTTPStatus.OK, backend.run_once())
                return
            if parsed.path == "/api/v1/pause":
                self._send_json(HTTPStatus.OK, backend.pause_workflow(str(body.get("reason") or "사용자 일시정지 요청")))
                return
            if parsed.path == "/api/v1/cancel":
                self._send_json(HTTPStatus.OK, backend.cancel_workflow(str(body.get("reason") or "사용자 취소 요청")))
                return
            if parsed.path == "/api/v1/replan":
                self._send_json(HTTPStatus.OK, backend.replan_workflow(str(body.get("rationale") or "")))
                return
            if parsed.path == "/api/v1/final-report":
                self._send_json(HTTPStatus.OK, backend.final_report_view(str(body.get("format") or "json")))
                return
            if parsed.path == "/api/v1/stale/refresh":
                self._send_json(HTTPStatus.OK, backend.refresh_stale())
                return
            if parsed.path == "/api/v1/attempts/observe":
                self._send_json(HTTPStatus.OK, backend.observe_attempt())
                return
            if parsed.path == "/api/v1/attempts/resume":
                self._send_json(HTTPStatus.OK, backend.resume_attempt())
                return
            if parsed.path == "/api/v1/intents/abandon":
                self._send_json(HTTPStatus.OK, backend.abandon_intent(str(body.get("rationale") or "")))
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND", "path": parsed.path})

    return GuiApiHandler


class LocalGuiServer:
    """백그라운드 스레드 또는 블로킹 모드로 실행할 수 있는 로컬 HTTP API 서버."""

    def __init__(
        self,
        backend: LocalGuiEngineBackend,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        session_token: str | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "LOCAL_LOOPBACK_ONLY: GUI HTTP 서버는 로컬 루프백 주소(127.0.0.1, localhost, ::1)에만 바인딩할 수 있습니다."
            )
        self.backend = backend
        self.session_token = session_token or secrets.token_urlsafe(32)
        self.httpd = ThreadingHTTPServer(
            (host, port),
            create_http_handler(backend, session_token=self.session_token),
        )
        self.host = str(self.httpd.server_address[0])
        self.port = int(self.httpd.server_address[1])
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start_background(self) -> str:
        if self._thread is None:
            self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self._thread.start()
        return self.base_url

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def close(self) -> None:
        self.backend.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="flowmarshal-gui",
        description="VibeMarshal Desktop GUI용 로컬 HTTP/JSON + SSE API 서버",
    )
    parser.add_argument("--db", default=str(Path.cwd() / ".flowmarshal-engine" / "engine_ledger.sqlite3"))
    parser.add_argument("--artifacts", default=str(Path.cwd() / ".flowmarshal-engine" / "artifacts"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root")
    parser.add_argument("--project-id")
    parser.add_argument("--live-gemini", action="store_true", help="실제 Antigravity Gemini CLI (agy.exe) 런타임 사용")
    parser.add_argument("--once", action="store_true", help="기동 검증 후 즉시 상태 출력 및 종료")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    backend = LocalGuiEngineBackend(
        db_path=arguments.db,
        artifact_root=arguments.artifacts,
        project_root=arguments.project_root,
        project_id=arguments.project_id,
        live_gemini=arguments.live_gemini,
    )
    server = LocalGuiServer(backend, host=arguments.host, port=arguments.port)
    if arguments.once:
        try:
            url = server.start_background()
            snap = backend.snapshot()
            sys.stdout.write(
                json.dumps(
                    {
                        "status": "ready",
                        "base_url": url,
                        "project_id": backend.project_id,
                        "phase": snap["phase"],
                        "history_cursor": snap["historyCursor"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        finally:
            server.close()
        return 0
    sys.stdout.write(
        json.dumps(
            {
                "status": "serving",
                "base_url": server.base_url,
                "project_id": backend.project_id,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
