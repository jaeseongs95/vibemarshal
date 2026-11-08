"""개발·테스트 전용 GUI 백엔드 결정적 픽스처 모듈.

이 모듈은 `setup.py`의 `ENGINE_DEVELOPER_MODULES`에 등록되어 사용자 배포용 Wheel에는
포함되지 않으며, 로컬 결정적 테스트 및 개발 시뮬레이션에서만 사용된다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    ApprovalClass,
    BehaviorPolicy,
    DependencyType,
    MissionClass,
    MutationPolicy,
    RiskLevel,
    TaskKind,
    new_id,
    utc_now,
)
from .roles import (
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    RoleCallReceipt,
    RoleCallRequest,
    RoleCallResult,
    strict_json_output_schema,
)
from .runtime import FakeCodexRuntime, RuntimeOperationReceipt


class LocalImmediateRuntime(FakeCodexRuntime):
    """로컬 결정적 테스트에서 turn 시작 직후 provider terminal을 완료로 전환하는 runtime."""

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


class LocalDevTaskGate:
    """로컬 결정적 테스트에서 EngineDispatcher가 실행 Task를 통과시키는 task gate."""

    def before_execution(self, task: Any) -> None:
        return None

    def before_completion(self, task: Any) -> None:
        return None

    def close(self) -> None:
        return None


class LocalDevGovernance:
    """로컬 결정적 테스트용 governance provider."""

    def check_conformance(self) -> dict[str, Any]:
        return {"verdict": "PASS", "checks": [], "provenance": "local_derived"}

    def open_gate(self, service: Any, *, runtime: Any, roles: Any, runner: Any) -> LocalDevTaskGate:
        return LocalDevTaskGate()


def build_plan_inspection_dict(
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


class LocalDeterministicRoleRunner:
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
                        "recovery": {"retryable_failure_classes": ["implementation"]},
                    }
                )
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
                "inspection": build_plan_inspection_dict(plan_draft, goal_def, revision=False),
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
                "inspection": build_plan_inspection_dict(plan_contract, goal_def, revision=True),
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

        raise ValueError(f"Unsupported role in LocalDeterministicRoleRunner: {role}")
