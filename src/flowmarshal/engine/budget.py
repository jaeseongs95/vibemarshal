from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field

from ..canonical import canonical_json, sha256_digest
from .domain import BudgetStage, BudgetUsageRecord, EngineModel, new_id, utc_now
from .service import EngineService, EngineServiceError


class GoalBudgetPolicy(EngineModel):
    """사용자가 명시하는 관측 token 예산. 청구 금액이나 구독 차감량이 아니다."""

    schema_version: Literal["1.0"] = "1.0"
    total_tokens: int = Field(gt=0, strict=True)
    call_reservation_tokens: int = Field(gt=0, strict=True)
    replan_reserve_percent: int = Field(default=25, ge=0, le=90, strict=True)


class BudgetBlocked(EngineServiceError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class BudgetStatus(EngineModel):
    project_id: str
    goal_id: str | None
    policy: GoalBudgetPolicy | None
    measured_token_subtotal: int = Field(ge=0)
    explicit_adjustment_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    unresolved_call_ids: tuple[str, ...]
    remaining_normal_tokens: int | None
    remaining_total_tokens: int | None
    error_code: str | None
    next_action: str | None


ROLE_STAGES = {
    "goal_normalizer": BudgetStage.GOAL_NORMALIZATION,
    "goal_reviewer": BudgetStage.GOAL_REVIEW,
    "goal_refiner": BudgetStage.REPLAN,
    "skeleton_generator": BudgetStage.SKELETON_GENERATION,
    "skeleton_reviewer": BudgetStage.SKELETON_REVIEW,
    "skeleton_refiner": BudgetStage.REPLAN,
    "plan_expander": BudgetStage.PLAN_EXPANSION,
    "plan_refiner": BudgetStage.REPLAN,
    "compact_plan_reviewer": BudgetStage.PLAN_REVIEW,
    "plan_reviewer": BudgetStage.PLAN_REVIEW,
    "execution_preparation": BudgetStage.EXECUTION_PREPARATION,
    "goal_validation_preparation": BudgetStage.VALIDATION,
    "goal_test_preparation": BudgetStage.VALIDATION,
    "goal_validator": BudgetStage.VALIDATION,
    "critical_plan_reviewer": BudgetStage.PLAN_REVIEW,
    "semantic_validator": BudgetStage.VALIDATION,
    "worker": BudgetStage.EXECUTION,
}


def receipt_usage(receipt: Any, *, project_id: str, goal_digest: str,
                  stage: BudgetStage | None = None) -> BudgetUsageRecord:
    available = receipt.usage_available and receipt.schema_recovery_attempts == 0
    return BudgetUsageRecord(
        usage_id=new_id("usage"), project_id=project_id, goal_contract_digest=goal_digest,
        stage=stage or ROLE_STAGES.get(receipt.role, BudgetStage.VALIDATION),
        logical_call_ref=receipt.call_id, role=receipt.role, call_status=receipt.status,
        model=receipt.model, effort=receipt.effort, permission_profile=receipt.permission_profile,
        approval_policy=receipt.approval_policy, thread_id=receipt.thread_id,
        turn_ids=receipt.turn_ids, input_digest=receipt.input_digest,
        output_digest=receipt.output_digest, output_schema_digest=receipt.output_schema_digest,
        runner_receipt_digest=sha256_digest(receipt),
        input_tokens=receipt.input_tokens if available else None,
        cached_input_tokens=receipt.cached_input_tokens if available else None,
        output_tokens=receipt.output_tokens if available else None,
        reasoning_tokens=receipt.reasoning_tokens if available else None,
        latency_ms=receipt.latency_ms, usage_available=available,
        unavailable_reason=None if available else "PROVIDER_USAGE_UNAVAILABLE_OR_RECOVERY_UNATTRIBUTABLE",
        retry_count=receipt.schema_recovery_attempts, recorded_at=receipt.recorded_at,
    )


def _terminal_receipt(receipt: Any | None) -> bool:
    return receipt is not None and (receipt.status in {"succeeded", "failed", "schema_failed"} or
        (getattr(receipt, "terminal_observation_digest", None) is not None and
         getattr(receipt, "terminal_status_after_interrupt", None) in {"completed", "success", "succeeded", "failed", "interrupted", "cancelled"}))


class BudgetManager:
    """Core 원장의 호출 전 예약·종료 정산. 불명확한 예약은 자동 해제하지 않는다."""

    def __init__(self, service: EngineService):
        self.service = service

    def configure(self, project_id: str, policy: GoalBudgetPolicy, *, goal_id: str = "") -> str:
        with self.service.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (project_id,))
            if goal_id:
                tx.one("SELECT goal_id FROM goal_revisions WHERE project_id = ? AND goal_id = ?",
                       (project_id, goal_id))
            revision = tx.one("SELECT COALESCE(MAX(revision_no), 0) + 1 AS n FROM budget_policy_revisions "
                              "WHERE project_id = ? AND scope_key = ?", (project_id, goal_id))["n"]
            identifier = new_id("budget_policy")
            tx.connection.execute("INSERT INTO budget_policy_revisions VALUES (?,?,?,?,?,?,?)",
                (identifier, project_id, goal_id, revision, sha256_digest(policy), canonical_json(policy), tx.now))
            tx.history(project_id, "budget.policy_configured", "budget_policy", identifier,
                       {"goal_id": goal_id or None, "policy": policy.model_dump(mode="json")})
            return identifier

    @staticmethod
    def _policy(tx: Any, project_id: str, goal_id: str) -> GoalBudgetPolicy | None:
        row = tx.maybe_one("SELECT payload_json FROM budget_policy_revisions WHERE project_id = ? "
            "AND scope_key IN ('', ?) ORDER BY (scope_key = ?) DESC, revision_no DESC LIMIT 1",
            (project_id, goal_id, goal_id))
        return None if row is None else GoalBudgetPolicy.model_validate_json(row["payload_json"])

    def require_policy(self, project_id: str, goal_id: str) -> None:
        with self.service.ledger.transaction() as tx:
            if self._policy(tx, project_id, goal_id) is None:
                raise BudgetBlocked("BUDGET_POLICY_REQUIRED", "project budget set으로 총량과 호출 예약량을 지정하세요.")

    def status(self, project_id: str, *, goal_id: str | None = None) -> BudgetStatus:
        with self.service.ledger.read() as connection:
            project = connection.execute("SELECT active_goal_revision_id FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise EngineServiceError("PROJECT_NOT_FOUND")
            if goal_id is None:
                row = connection.execute("SELECT goal_id FROM goal_revisions WHERE id=?", (project["active_goal_revision_id"],)).fetchone()
                if row is None:
                    row = connection.execute("SELECT goal_id FROM provider_calls WHERE project_id=? ORDER BY rowid DESC LIMIT 1", (project_id,)).fetchone()
                goal_id = None if row is None else row["goal_id"]
            policy_row = connection.execute("SELECT payload_json FROM budget_policy_revisions WHERE project_id=? AND scope_key IN ('',?) "
                "ORDER BY (scope_key=?) DESC,revision_no DESC LIMIT 1", (project_id, goal_id or "", goal_id or "")).fetchone()
            policy = None if policy_row is None else GoalBudgetPolicy.model_validate_json(policy_row["payload_json"])
            rows = connection.execute("SELECT c.*,a.charge_tokens FROM provider_calls c LEFT JOIN budget_adjustments a ON a.call_id=c.id "
                "WHERE c.project_id=? AND c.goal_id=? AND c.status<>'released'", (project_id, goal_id)).fetchall()
            unresolved = tuple(row["id"] for row in connection.execute("SELECT c.id FROM provider_calls c LEFT JOIN budget_adjustments a ON a.call_id=c.id "
                "WHERE c.project_id=? AND c.status IN ('reserved','usage_unknown') AND a.call_id IS NULL ORDER BY c.rowid", (project_id,)))
        measured = sum(row["actual_tokens"] or 0 for row in rows)
        adjusted = sum(row["charge_tokens"] or 0 for row in rows if row["actual_tokens"] is None)
        reserved = sum(row["estimated_tokens"] for row in rows if row["status"] == "reserved")
        blocked = policy is None or bool(unresolved)
        return BudgetStatus(project_id=project_id, goal_id=goal_id, policy=policy,
            measured_token_subtotal=measured, explicit_adjustment_tokens=adjusted, reserved_tokens=reserved,
            unresolved_call_ids=unresolved,
            remaining_normal_tokens=None if blocked else max(0, policy.total_tokens * (100-policy.replan_reserve_percent)//100 - measured-adjusted),
            remaining_total_tokens=None if blocked else max(0, policy.total_tokens-measured-adjusted),
            error_code="BUDGET_POLICY_REQUIRED" if policy is None else "BUDGET_USAGE_UNKNOWN" if unresolved else None,
            next_action="project budget set으로 정책을 등록하십시오." if policy is None else
                "기존 호출을 관측하고 usage 미제공이 확정되면 이유와 잠정 차감량을 명시하십시오." if unresolved else None)

    def reserve(self, *, project_id: str, goal_id: str, goal_digest: str | None,
                call_key: str, role: str, request: dict[str, Any],
                stage: BudgetStage | None = None, attempt_id: str | None = None) -> str:
        stage = stage or ROLE_STAGES.get(role, BudgetStage.VALIDATION)
        with self.service.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (project_id,))
            if goal_digest is not None and tx.maybe_one(
                    "SELECT id FROM goal_revisions WHERE project_id=? AND goal_id=? AND definition_digest=?",
                    (project_id, goal_id, goal_digest)) is None:
                raise BudgetBlocked("BUDGET_GOAL_BINDING_MISMATCH", "등록된 Goal 계보와 revision digest가 필요합니다.")
            if tx.maybe_one("SELECT id FROM provider_calls WHERE project_id = ? AND call_key = ?",
                            (project_id, call_key)):
                raise BudgetBlocked("PROVIDER_CALL_ALREADY_RESERVED", "기존 호출을 먼저 관측해야 합니다.")
            policy = self._policy(tx, project_id, goal_id)
            calls = tx.all("SELECT c.*, a.charge_tokens FROM provider_calls c LEFT JOIN budget_adjustments a "
                           "ON a.call_id = c.id WHERE c.project_id = ? AND c.goal_id = ?", (project_id, goal_id))
            if policy:
                unresolved = [c["id"] for c in tx.all(
                    "SELECT c.id FROM provider_calls c LEFT JOIN budget_adjustments a ON a.call_id=c.id "
                    "WHERE c.project_id=? AND c.status IN ('reserved','usage_unknown') AND a.call_id IS NULL",
                    (project_id,))]
                if unresolved:
                    raise BudgetBlocked("BUDGET_USAGE_UNKNOWN", "사용량 재관측 또는 명시적 정산이 필요합니다: " + ", ".join(unresolved))
                charged = sum(c["actual_tokens"] if c["actual_tokens"] is not None else
                              c["charge_tokens"] if c["charge_tokens"] is not None else
                              c["estimated_tokens"] if c["status"] == "reserved" else 0 for c in calls)
                reserve = policy.call_reservation_tokens
                cap = (policy.total_tokens if stage is BudgetStage.REPLAN else
                       policy.total_tokens * (100 - policy.replan_reserve_percent) // 100)
                if charged + reserve > cap:
                    raise BudgetBlocked("BUDGET_BLOCKED", f"누적·예약 {charged} + 다음 예약 {reserve} > 사용 가능 {cap}")
            identifier = new_id("provider_call")
            tx.connection.execute("INSERT INTO provider_calls (id,project_id,goal_id,goal_contract_digest,call_key,role,stage,"
                "request_digest,request_json,estimated_tokens,policy_digest,status,attempt_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)", (identifier, project_id, goal_id, goal_digest,
                call_key, role, stage.value, sha256_digest(request), canonical_json(request),
                0 if policy is None else policy.call_reservation_tokens,
                None if policy is None else sha256_digest(policy), attempt_id, tx.now))
            tx.history(project_id, "budget.call_reserved", "provider_call", identifier,
                       {"goal_id": goal_id, "call_key": call_key, "role": role,
                        "policy_configured": policy is not None})
            return identifier

    def settle(self, call_id: str, receipt: Any | None) -> None:
        with self.service.ledger.transaction() as tx:
            call = tx.one("SELECT * FROM provider_calls WHERE id = ?", (call_id,))
            value = None if receipt is None else canonical_json(receipt)
            if call["status"] != "reserved":
                if call["receipt_json"] != value:
                    raise BudgetBlocked("BUDGET_RECEIPT_CONFLICT", "기존 관측을 다른 receipt로 덮어쓸 수 없습니다.")
                return
            request = json.loads(call["request_json"])
            if receipt is not None and "output_schema" in request and "instructions" in request:
                from .roles import RoleCallRequest, strict_json_output_schema
                bound_request = RoleCallRequest.model_validate(request)
                if (sha256_digest(request) != call["request_digest"]
                        or receipt.input_digest != bound_request.request_digest or receipt.role != call["role"]
                        or receipt.model != request["model"] or receipt.effort != request["effort"]
                        or receipt.output_schema_digest != sha256_digest(strict_json_output_schema(request["output_schema"]))):
                    raise BudgetBlocked("BUDGET_RECEIPT_BINDING_MISMATCH", "예약한 역할 요청과 다른 receipt는 정산하지 않습니다.")
            terminal = _terminal_receipt(receipt)
            known = terminal and receipt.usage_available and receipt.schema_recovery_attempts == 0
            actual = receipt.input_tokens + receipt.output_tokens if known else None
            usage_id = None
            if receipt is not None and call["goal_contract_digest"] is not None:
                usage = receipt_usage(receipt, project_id=call["project_id"],
                                      goal_digest=call["goal_contract_digest"], stage=BudgetStage(call["stage"]))
                usage_id = self.service._insert_budget_usage(tx, usage)
            tx.connection.execute("UPDATE provider_calls SET status=?, actual_tokens=?, receipt_json=?, usage_id=?, completed_at=? "
                                  "WHERE id=?", ("settled" if known else "usage_unknown" if terminal else "reserved",
                                                actual, value, usage_id, tx.now if terminal else None, call_id))
            tx.history(call["project_id"], "budget.call_settled", "provider_call", call_id,
                       {"actual_tokens": actual, "usage_available": known, "receipt": None if receipt is None else receipt.model_dump(mode="json")})

    def attach_goal(self, project_id: str, goal_id: str, goal_digest: str) -> None:
        """정규화 이전 호출을 실제 생성된 Goal에 연결하고 원시 receipt는 보존한다."""
        from .roles import RoleCallReceipt
        with self.service.ledger.transaction() as tx:
            tx.one("SELECT id FROM goal_revisions WHERE project_id=? AND goal_id=? AND definition_digest=?",
                   (project_id, goal_id, goal_digest))
            for call in tx.all("SELECT * FROM provider_calls WHERE project_id=? AND goal_id=? AND goal_contract_digest IS NULL",
                               (project_id, goal_id)):
                usage_id = None
                if call["receipt_json"]:
                    receipt = RoleCallReceipt.model_validate_json(call["receipt_json"])
                    usage = receipt_usage(receipt, project_id=project_id, goal_digest=goal_digest,
                                          stage=BudgetStage(call["stage"]))
                    usage_id = self.service._insert_budget_usage(tx, usage)
                tx.connection.execute("UPDATE provider_calls SET goal_contract_digest=?, usage_id=? WHERE id=?",
                                      (goal_digest, usage_id, call["id"]))
                tx.history(project_id, "budget.goal_bound", "provider_call", call["id"], {"goal_contract_digest": goal_digest})

    def adjust_unknown(self, *, call_id: str, charge_tokens: int, reason: str) -> None:
        if isinstance(charge_tokens, bool) or not isinstance(charge_tokens, int) or charge_tokens < 0 or not reason.strip():
            raise ValueError("명시적 비음수 잠정 차감량과 이유가 필요합니다.")
        with self.service.ledger.transaction() as tx:
            call = tx.one("SELECT * FROM provider_calls WHERE id=?", (call_id,))
            if call["status"] != "usage_unknown":
                raise BudgetBlocked("BUDGET_ADJUSTMENT_NOT_ALLOWED", "종료 처리된 미확인 사용량만 명시적으로 정산할 수 있습니다.")
            identifier = new_id("budget_adjustment")
            tx.connection.execute("INSERT INTO budget_adjustments VALUES (?,?,?,?,?,?,?)",
                (identifier, call["project_id"], call["goal_id"], call_id, charge_tokens, reason, tx.now))
            tx.history(call["project_id"], "budget.unknown_adjusted", "budget_adjustment", identifier,
                       {"call_id": call_id, "charge_tokens": charge_tokens, "reason": reason, "actual_usage_known": False})

    def import_role_checkpoint(self, *, project_id: str, goal_id: str, goal_digest: str,
                               entries: tuple[dict[str, Any], ...], source_ref: str,
                               source_digest: str) -> tuple[str, ...]:
        """새 원장에 검증된 과거 호출 계보를 명시 등록한다. 호출·승인·성공을 재생하지 않는다."""
        import re
        from .roles import RoleCallReceipt, RoleCallRequest, strict_json_output_schema
        if not source_ref.strip() or re.fullmatch(r"sha256:[0-9a-f]{64}", source_digest) is None or not entries:
            raise ValueError("BUDGET_CHECKPOINT_PROVENANCE_REQUIRED")
        checked = []
        for item in entries:
            request = RoleCallRequest.model_validate(item["request"])
            receipt = RoleCallReceipt.model_validate(item["receipt"])
            if (receipt.input_digest != request.request_digest
                    or receipt.output_schema_digest != sha256_digest(strict_json_output_schema(request.output_schema))
                    or (receipt.role, receipt.model, receipt.effort, receipt.inventory_digest)
                    != (request.role, request.model, request.effort, request.inventory_digest)):
                raise BudgetBlocked("BUDGET_CHECKPOINT_BINDING_MISMATCH", "과거 요청·receipt 결속이 다릅니다.")
            checked.append((item, request, receipt))
        if len({r.call_id for _, _, r in checked}) != len(checked):
            raise BudgetBlocked("BUDGET_CHECKPOINT_DUPLICATE", "과거 호출 ID가 중복됐습니다.")
        identifiers = []
        with self.service.ledger.transaction() as tx:
            tx.one("SELECT id FROM goal_revisions WHERE project_id=? AND goal_id=? AND definition_digest=?",
                   (project_id, goal_id, goal_digest))
            if tx.maybe_one("SELECT id FROM provider_calls WHERE project_id=?", (project_id,)):
                raise BudgetBlocked("BUDGET_CHECKPOINT_TARGET_NOT_EMPTY", "호출 계보가 없는 새 원장에만 가져올 수 있습니다.")
            for item, request, receipt in checked:
                digest = item.get("goal_contract_digest", goal_digest)
                tx.one("SELECT id FROM goal_revisions WHERE project_id=? AND goal_id=? AND definition_digest=?",
                       (project_id, goal_id, digest))
                usage = receipt_usage(receipt, project_id=project_id, goal_digest=digest)
                usage_id = self.service._insert_budget_usage(tx, usage)
                terminal = _terminal_receipt(receipt)
                known = terminal and usage.usage_available
                identifier = new_id("provider_call")
                tx.connection.execute("INSERT INTO provider_calls (id,project_id,goal_id,goal_contract_digest,call_key,role,stage,"
                    "request_digest,request_json,estimated_tokens,status,actual_tokens,receipt_json,usage_id,created_at,completed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)", (identifier, project_id, goal_id, digest, receipt.call_id,
                    receipt.role, usage.stage.value, sha256_digest(item["request"]), canonical_json(item["request"]),
                    "settled" if known else "usage_unknown" if terminal else "reserved",
                    usage.input_tokens + usage.output_tokens if known else None,
                    canonical_json(item["receipt"]), usage_id, receipt.recorded_at.isoformat(),
                    receipt.recorded_at.isoformat() if terminal else None))
                identifiers.append(identifier)
                tx.history(project_id, "budget.checkpoint_call_imported", "provider_call", identifier,
                           {"source_ref": source_ref, "source_digest": source_digest,
                            "original_call_id": receipt.call_id, "original_receipt_digest": sha256_digest(item["receipt"]),
                            "actual_usage_known": known})
        return tuple(identifiers)

    def observe_role_terminal(self, call_id: str, observation: Any) -> None:
        """기존 역할 호출의 terminal usage를 append-only 재관측으로 결속한다."""
        from .role_usage_reconciliation import reconcile_role_usage_terminal
        return reconcile_role_usage_terminal(self.service, call_id, observation)

    def release_before_effect(self, call_id: str, *, reason: str) -> None:
        """역할 runner가 provider 효과 전 실패를 증명한 예약만 닫는다."""
        with self.service.ledger.transaction() as tx:
            call = tx.one("SELECT * FROM provider_calls WHERE id=?", (call_id,))
            if call["status"] != "reserved" or call["receipt_json"] is not None or call["attempt_id"] is not None:
                raise BudgetBlocked("BUDGET_RELEASE_NOT_ALLOWED", "역할 효과가 시작되지 않은 예약만 해제할 수 있습니다.")
            tx.connection.execute("UPDATE provider_calls SET status='released',completed_at=? WHERE id=?", (tx.now, call_id))
            tx.history(call["project_id"], "budget.released_before_effect", "provider_call", call_id,
                       {"request_digest": call["request_digest"], "reason": reason})


class BudgetedRoleRunner:
    """모든 역할의 provider 호출을 공통 예약·관측 경로로 보낸다."""

    def __init__(self, runner: Any, service: EngineService, *, project_id: str, goal_id: str,
                 goal_digest: str | None = None):
        self.runner, self.service = runner, service
        self.project_id, self.goal_id, self.goal_digest = project_id, goal_id, goal_digest
        if getattr(runner, "max_schema_recovery_attempts", 0):
            with service.ledger.transaction() as tx:
                if BudgetManager._policy(tx, project_id, goal_id) is not None:
                    raise ValueError("BUDGET_SCHEMA_RECOVERY_UNSUPPORTED: 예산 집행에는 recovery=0을 명시하세요.")

    @property
    def receipts(self):
        return self.runner.receipts

    def run(self, request: Any, *, validator: Any = None):
        from .roles import StructuredRoleError
        from .role_budget import current_role_budget_stage
        manager = BudgetManager(self.service)
        if getattr(getattr(self.runner, "runtime", None), "requires_budget_policy", False):
            manager.require_policy(self.project_id, self.goal_id)
        call_id = manager.reserve(project_id=self.project_id, goal_id=self.goal_id,
            goal_digest=self.goal_digest, call_key=new_id("role_invocation"), role=request.role,
            request=request.model_dump(mode="json"), stage=current_role_budget_stage())
        try:
            result = self.runner.run(request, validator=validator)
        except BaseException as error:
            if isinstance(error, StructuredRoleError) and getattr(error, "effects_started", True) is False:
                manager.release_before_effect(call_id, reason=str(error))
                raise
            receipt = error.receipt if isinstance(error, StructuredRoleError) else None
            manager.settle(call_id, receipt)
            raise
        manager.settle(call_id, result.receipt)
        return result


def reserve_attempt_call(service: EngineService, attempt: Any, *, call_key: str, request: dict[str, Any],
                         require_policy: bool = False) -> str:
    with service.ledger.read() as connection:
        goal = connection.execute("SELECT g.goal_id,g.definition_digest FROM attempts a JOIN plan_revisions p "
            "ON p.id=a.plan_revision_id JOIN goal_revisions g ON g.project_id=a.project_id "
            "AND g.definition_digest=json_extract(p.payload_json,'$.definition.goal_contract_digest') WHERE a.id=?",
            (attempt["id"],)).fetchone()
    if goal is None:
        raise EngineServiceError("BUDGET_GOAL_BINDING_MISSING: Attempt의 Goal을 찾을 수 없습니다.")
    if require_policy:
        BudgetManager(service).require_policy(attempt["project_id"], goal["goal_id"])
    return BudgetManager(service).reserve(project_id=attempt["project_id"], goal_id=goal["goal_id"],
        goal_digest=goal["definition_digest"], call_key=call_key,
        role="worker" if attempt["kind"] == "execution" else "semantic_validator",
        request=request, attempt_id=attempt["id"])


def record_validator_usage(service: EngineService, attempt_id: str, observation: Any) -> BudgetUsageRecord | None:
    """Task Validator의 직접 provider turn 관측을 Worker와 별도로 결속한다."""
    from .domain import RuntimeReceipt, TaskExecutionSpecRevision
    with service.ledger.transaction() as tx:
        attempt = tx.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if attempt["kind"] != "validation":
            return None
        rows = tx.all("SELECT i.*,r.payload_json AS receipt_json FROM runtime_intents i JOIN runtime_receipts r "
                      "ON r.intent_id=i.id WHERE i.attempt_id=? AND i.kind='start_turn' AND i.status='received'", (attempt_id,))
        matching = [(r, RuntimeReceipt.model_validate_json(r["receipt_json"])) for r in rows]
        matching = [(r, receipt) for r, receipt in matching if receipt.binding and
                    (receipt.binding.thread_id, receipt.binding.turn_id) == (observation.thread_id, observation.turn_id)]
        if len(matching) != 1:
            raise EngineServiceError("VALIDATOR_USAGE_BINDING_MISMATCH: 유일한 turn receipt가 필요합니다.")
        intent, receipt = matching[0]
        request = json.loads(intent["request_json"])
        if request.get("role_usage_contract") != 1:
            return None
        key = f"validator:{observation.thread_id}:{observation.turn_id}"
        prior = tx.maybe_one("SELECT payload_json FROM budget_usage WHERE project_id=? AND logical_call_ref=?",
                             (attempt["project_id"], key))
        raw = observation.payload.get("usage")
        scope = observation.payload.get("usage_scope")
        start = receipt.response_payload or {}
        # 과거 transport가 정책·usage 관측 계약을 제공하지 않았다면 소급 작성하지 않는다.
        if "permission_profile" not in start or "approval_policy" not in start:
            return None
        if observation.active or observation.terminal_status not in {"completed", "success", "succeeded", "failed", "interrupted", "cancelled"}:
            raise EngineServiceError("VALIDATOR_USAGE_TERMINAL_REQUIRED: 종료 관측이 필요합니다.")
        if (start.get("permission_profile") != ":danger-full-access" or start.get("approval_policy") != "never"
                or start.get("prompt_digest") != request["prompt_digest"]
                or any(observation.payload.get(key, expected) != expected for key, expected in (
                    ("thread_id", observation.thread_id), ("turn_id", observation.turn_id),
                    ("prompt_digest", request["prompt_digest"]),
                ))):
            raise EngineServiceError("VALIDATOR_USAGE_BINDING_MISMATCH: 전송 정책 또는 Prompt 관측이 다릅니다.")
        first_empty = start.get("first_empty_thread") is True
        if scope == "thread" and first_empty:
            creations = [RuntimeReceipt.model_validate_json(r["payload_json"]) for r in tx.all(
                "SELECT r.payload_json FROM runtime_receipts r JOIN runtime_intents i ON i.id=r.intent_id "
                "WHERE i.attempt_id=? AND i.kind='create_thread'", (attempt_id,))]
            first_empty = any(
                created.provider_operation_id == observation.thread_id and created.binding
                and created.binding.thread_id == observation.thread_id
                and isinstance((created.response_payload or {}).get("thread"), dict)
                and created.response_payload["thread"].get("id") == observation.thread_id
                and created.response_payload["thread"].get("turns") == []
                for created in creations)
            first_empty = first_empty and len(matching) == 1 and len(rows) == 1
        values_source = raw if scope == "turn" else raw.get("total") if isinstance(raw, dict) and scope == "thread" and first_empty else None
        keys = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
        values = tuple(values_source.get(k) for k in keys) if isinstance(values_source, dict) else (None,) * 4
        known = all(type(v) is int and v >= 0 for v in values)
        known = known and values[1] <= values[0] and values[3] <= values[2]
        if known and "totalTokens" in values_source:
            known = type(values_source["totalTokens"]) is int and values_source["totalTokens"] == values[0] + values[2]
        if not known:
            values = (None,) * 4
        if prior:
            existing = BudgetUsageRecord.model_validate_json(prior["payload_json"])
            if known and (not existing.usage_available or values != (
                    existing.input_tokens, existing.cached_input_tokens, existing.output_tokens, existing.reasoning_tokens)):
                raise EngineServiceError("VALIDATOR_USAGE_CONFLICT: 같은 turn의 실측은 명시적 재관측 없이 변경하지 않습니다.")
            return existing
        call = tx.one("SELECT * FROM provider_calls WHERE attempt_id=? AND status='reserved' ORDER BY created_at DESC LIMIT 1", (attempt_id,))
        spec = TaskExecutionSpecRevision.model_validate_json(tx.one("SELECT payload_json FROM execution_spec_revisions WHERE definition_digest=?",
                                                                   (attempt["execution_spec_digest"],))["payload_json"])
        payload = {"thread_id": observation.thread_id, "turn_id": observation.turn_id,
                   "terminal_status": observation.terminal_status, "payload": observation.payload}
        duration = observation.payload.get("duration_ms")
        usage = BudgetUsageRecord(usage_id=new_id("usage"), project_id=attempt["project_id"],
            goal_contract_digest=call["goal_contract_digest"], stage=BudgetStage.VALIDATION,
            logical_call_ref=key, role="semantic_validator", call_status=observation.terminal_status,
            model=request["model"], effort=request["effort"], permission_profile=receipt.response_payload["permission_profile"],
            approval_policy=receipt.response_payload["approval_policy"],
            thread_id=observation.thread_id, turn_ids=(observation.turn_id,), input_digest=request["prompt_digest"],
            output_digest=None if observation.final_response is None else sha256_digest(observation.final_response),
            output_schema_digest=request["output_schema_digest"], runner_receipt_digest=sha256_digest(payload),
            input_tokens=values[0], cached_input_tokens=values[1], output_tokens=values[2], reasoning_tokens=values[3],
            latency_ms=duration if type(duration) is int and duration >= 0 else None, usage_available=known,
            usage_scope=scope if scope in {"turn", "thread"} else "unavailable",
            usage_source=observation.payload.get("usage_source", "runtime.read"),
            attribution_basis=("provider_turn" if scope == "turn" else "first_empty_thread") if known else "unavailable",
            unavailable_reason=None if known else "PROVIDER_USAGE_UNAVAILABLE_OR_UNATTRIBUTABLE",
            runtime_intent_id=intent["id"], runtime_receipt_id=receipt.receipt_id,
            execution_spec_digest=spec.definition_digest, provider_observation=payload, recorded_at=utc_now())
        service._insert_budget_usage(tx, usage)
        tx.connection.execute("UPDATE provider_calls SET status=?,actual_tokens=?,receipt_json=?,usage_id=?,completed_at=? WHERE id=?",
            ("settled" if known else "usage_unknown", values[0] + values[2] if known else None,
             canonical_json(payload), usage.usage_id, tx.now, call["id"]))
        tx.history(attempt["project_id"], "budget.call_settled", "provider_call", call["id"],
                   {"actual_tokens": values[0] + values[2] if known else None, "usage_available": known})
        return usage


def settle_worker_in_transaction(tx: Any, usage: BudgetUsageRecord) -> None:
    if usage.attempt_id is None:
        return
    call = tx.maybe_one("SELECT * FROM provider_calls WHERE attempt_id=? AND status='reserved' ORDER BY created_at DESC LIMIT 1",
                        (usage.attempt_id,))
    if call is None:
        return
    actual = usage.input_tokens + usage.output_tokens if usage.usage_available else None
    tx.connection.execute("UPDATE provider_calls SET status=?,actual_tokens=?,receipt_json=?,usage_id=?,completed_at=? WHERE id=?",
        ("settled" if usage.usage_available else "usage_unknown", actual, canonical_json(usage.provider_observation),
         usage.usage_id, tx.now, call["id"]))
    tx.history(usage.project_id, "budget.call_settled", "provider_call", call["id"],
               {"actual_tokens": actual, "usage_available": usage.usage_available})
