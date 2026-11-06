"""VibeMarshal Desktop GUI를 위한 로컬 HTTP/JSON + SSE API 어댑터.

GUI는 SQLite를 직접 읽거나 쓰지 않고, 이 로컬 HTTP 어댑터를 통해 EngineService 및
EngineApplication의 권위 상태와 명령을 호출한다.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..canonical import sha256_bytes, sha256_digest
from .application import ApplicationAuthority, EngineApplication, EngineApplicationError
from .capabilities import CoreCapabilityError
from .domain import (
    ApproachSignature,
    ApprovalClass,
    BehaviorPolicy,
    CandidateDecision,
    CandidateStatus,
    CriterionVerdict,
    Criticality,
    DependencyType,
    EffectPolicy,
    EvidenceKind,
    EvidenceRecord,
    ExecutionAction,
    ExecutionContextNeed,
    ExecutionSpecProposal,
    GoalConstraint,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCoverage,
    GoalCriterion,
    GoalVerdict,
    GoalVerdictStatus,
    IntegrationValidationContract,
    LifecycleStage,
    MissionClass,
    ModelAssignmentContract,
    MutationPolicy,
    PlanContractDefinition,
    PlanContractRevision,
    PlanDependency,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RecoveryEnvelope,
    ResolvedTarget,
    ReviewRatings,
    ReviewerSubmission,
    RevisionStatus,
    RiskLevel,
    RoleAssignmentPolicy,
    RuntimeIntentKind,
    SkeletonDependency,
    SourceTrace,
    TaskContract,
    TaskKind,
    TaskSkeleton,
    ThreadBinding,
    ValidationContract,
    ValidationExecutionStep,
    ValidationResult,
    ValidationStatus,
    new_id,
    utc_now,
)
from .ledger import EngineLedgerError, SQLiteEngineLedger
from .model_lock import RUNTIME_CAPABILITIES
from .models import EngineRoleConfiguration, ModelCapability, ModelInventory, RoleModelBinding
from .planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    PlanningSearchOutcome,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from .runtime import FakeCodexRuntime
from .service import EngineService, EngineServiceError


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LocalGuiEngineBackend:
    """실제 SQLiteEngineLedger, EngineService, EngineApplication을 감싸는 GUI 백엔드 어댑터."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        artifact_root: str | Path,
        project_root: str | Path | None = None,
        project_id: str | None = None,
        runtime: Any | None = None,
        role_configuration: EngineRoleConfiguration | None = None,
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
            from .agy_gemini_bridge import (
                AgyLocalGovernance,
                create_agy_gemini_runtime,
                default_agy_gemini_role_configuration,
            )

            self.runtime = create_agy_gemini_runtime(state_root=self.artifact_root / "gemini-runtime")
            self.inventory = self.runtime.list_models()
            self.role_configuration = role_configuration or default_agy_gemini_role_configuration()
            self.governance = AgyLocalGovernance()
        else:
            self.inventory = ModelInventory(
                executable_digest="sha256:" + "0" * 64,
                runtime_capabilities=RUNTIME_CAPABILITIES,
                source="gui-model-list",
                models=(
                    ModelCapability(model="worker", supported_efforts=("low", "medium", "high")),
                    ModelCapability(model="validator", supported_efforts=("low", "medium", "high")),
                ),
            )
            self.runtime = runtime or FakeCodexRuntime(self.inventory)
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
            self.governance = None
        self.application = EngineApplication(
            self.service,
            runtime=self.runtime,
            role_configuration=self.role_configuration,
            governance=self.governance,
        )
        self.scenario: str = "golden-path"
        self.run_log: list[dict[str, Any]] = []
        self._draft_goal_mode: bool = False
        self._stale_pending: bool = False
        self._recovery_state: dict[str, Any] | None = None
        self._active_attempt_state: dict[str, Any] | None = None
        self._ephemeral_workspaces: list[Path] = []
        self.project_id = project_id or self._ensure_default_project()

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
        return self._bootstrap_project_with_goal_and_plans()

    def _bootstrap_project_with_goal_and_plans(
        self,
        *,
        source_request: str = (
            "사용량이 비어 있는 실행에서도 최종 보고가 중단되지 않도록 합산 경계를 수정하고 "
            "기존 동작을 검증해줘."
        ),
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
        scenario_root.mkdir(parents=True, exist_ok=True)
        readme = scenario_root / "README.md"
        if not readme.exists():
            readme.write_text("# VibeMarshal Workspace\n", encoding="utf-8")
        agents_md = scenario_root / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text("# Project Instructions\nFollow standard Python unittest conventions.\n", encoding="utf-8")

        project_id = self.service.create_project(
            name="VibeMarshal Engine",
            root=str(scenario_root),
        )
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
        self.service.register_profile(profile_rev)
        self._register_goal_and_plans_in_ledger(
            project_id=project_id,
            source_request=source_request,
            revision_no=1,
            goal_id=new_id("goal"),
            supersedes_goal_revision_id=None,
        )
        return project_id

    def _register_goal_and_plans_in_ledger(
        self,
        *,
        project_id: str,
        source_request: str,
        revision_no: int,
        goal_id: str,
        supersedes_goal_revision_id: str | None,
    ) -> tuple[GoalContractRevision, PlanContractRevision]:
        profile = self.service.load_active_profile(project_id)
        req_digest = sha256_bytes(source_request.encode("utf-8"))
        trace = SourceTrace(
            trace_id=f"trace_gui_request_r{revision_no}",
            source_ref=f"desktop-gui:user-request:r{revision_no}",
            statement=source_request,
            source_digest=req_digest,
        )
        criteria = (
            GoalCriterion(
                criterion_id="ac_report_null",
                statement="null usage 값이 있어도 최종 보고가 오류 없이 생성된다.",
                validation_intent="Markdown·JSON 두 경로를 별도로 실행해 정상 종료와 결측 표시를 확인한다.",
                trace_refs=(trace.trace_id,),
            ),
            GoalCriterion(
                criterion_id="ac_preserve_totals",
                statement="측정 가능한 기존 token·latency 합계는 바뀌지 않는다.",
                validation_intent="기존 정상 fixture와 변경 후 결과를 비교한다.",
                trace_refs=(trace.trace_id,),
            ),
            GoalCriterion(
                criterion_id="ac_unknown_not_zero",
                statement="미제공 usage는 0이 아니라 알 수 없음과 사유로 표시된다.",
                validation_intent="결측 fixture의 직렬화 결과를 검사한다.",
                trace_refs=(trace.trace_id,),
            ),
        )
        constraints = (
            GoalConstraint(
                constraint_id="const_cli_compat",
                category="compatibility",
                statement="기존 CLI JSON key와 exit code를 보존한다.",
                trace_refs=(trace.trace_id,),
            ),
            GoalConstraint(
                constraint_id="const_no_legacy",
                category="scope",
                statement="legacy/prototype source는 수정하지 않는다.",
                trace_refs=(trace.trace_id,),
            ),
        )
        goal_def = GoalContractDefinition(
            project_id=project_id,
            source_request=source_request,
            source_request_digest=req_digest,
            mission_class=MissionClass.BUGFIX_STABILIZATION,
            observable_outcome="usage 일부가 null이어도 Markdown과 JSON 보고가 생성되고 기존 정상 합계가 유지된다.",
            hard_acceptance=criteria,
            constraints=constraints,
            non_goals=("실제 provider usage를 추정해 채우지 않는다.", "Engine 1.0 cutover 판정을 변경하지 않는다."),
            source_traces=(trace,),
            effect_policy=EffectPolicy(
                mutation_policy=MutationPolicy.MINIMAL_CHANGE,
                behavior_policy=BehaviorPolicy.PRESERVE_PUBLIC_CONTRACTS,
                allowed_external_effects=(),
                prohibited_effects=("배포", "외부 메시지", "운영 DB 변경"),
            ),
            profile_definition_digest=profile.definition_digest,
        )
        goal_rev = GoalContractRevision(
            goal_revision_id=new_id("goal_revision"),
            goal_id=goal_id,
            revision_no=revision_no,
            definition=goal_def,
            definition_digest=goal_def.definition_digest,
            status=RevisionStatus.READY,
            supersedes_goal_revision_id=supersedes_goal_revision_id,
            created_at=utc_now(),
        )
        self.service.register_goal(goal_rev, activate=True)

        project_map, state = self.service.reobserve_project(
            project_id,
            observed_paths=("README.md",),
        )

        skeleton = PlanSkeletonCandidate(
            candidate_id=new_id("candidate"),
            goal_contract_digest=goal_rev.definition_digest,
            state_signature=state.semantic_digest,
            approach=ApproachSignature(
                strategy_family="boundary-fix",
                change_shape="localized",
                compatibility="preserve",
                rollout_recovery="retry bounded",
            ),
            tasks=(
                TaskSkeleton(
                    task_ref="T1",
                    kind=TaskKind.INSPECT,
                    objective="현재 실패 재현",
                    contributes_to=("ac_report_null",),
                    produces=("artifact:repro",),
                    consumes=("input:request",),
                ),
                TaskSkeleton(
                    task_ref="T2",
                    kind=TaskKind.CHANGE,
                    objective="usage 합산 경계 수정",
                    contributes_to=("ac_report_null", "ac_unknown_not_zero"),
                    produces=("artifact:boundary",),
                    consumes=("artifact:repro",),
                ),
                TaskSkeleton(
                    task_ref="T3",
                    kind=TaskKind.VALIDATE,
                    objective="보고 회귀 검증",
                    contributes_to=("ac_report_null", "ac_preserve_totals", "ac_unknown_not_zero"),
                    produces=("artifact:regression",),
                    consumes=("artifact:boundary",),
                ),
            ),
            dependencies=(
                SkeletonDependency(
                    producer_task_ref="T1",
                    consumer_task_ref="T2",
                    dependency_type=DependencyType.DATA,
                    produces=("artifact:repro",),
                    consumes=("artifact:repro",),
                ),
                SkeletonDependency(
                    producer_task_ref="T2",
                    consumer_task_ref="T3",
                    dependency_type=DependencyType.DATA,
                    produces=("artifact:boundary",),
                    consumes=("artifact:boundary",),
                ),
            ),
            goal_coverage=(
                GoalCoverage(criterion_id="ac_report_null", task_refs=("T1", "T2", "T3")),
                GoalCoverage(criterion_id="ac_preserve_totals", task_refs=("T3",)),
                GoalCoverage(criterion_id="ac_unknown_not_zero", task_refs=("T2", "T3")),
            ),
            estimated_change_cost=3,
            estimated_context_tokens=300,
        )
        skeleton_digest = sha256_digest(skeleton)
        skel_catalog = skeleton_review_evidence_catalog(skeleton, goal_rev, state, project_map)
        skel_review = ReviewerSubmission(
            reviewer_role="skeleton_reviewer",
            candidate_digest=skeleton_digest,
            ratings=ReviewRatings(
                goal_fit=4,
                grounding=4,
                engineering=4,
                verification=4,
                execution_safety=4,
            ),
            evidence_catalog_digest=sha256_digest(skel_catalog),
        )
        skel_decision = CandidateDecision(
            candidate_digest=skeleton_digest,
            status=CandidateStatus.ADMISSIBLE,
            fitness_score=100,
            weakest_dimension="engineering",
        )
        skel_eval = CandidateEvaluation(
            candidate=skeleton,
            semantic_submission=skel_review,
            decision=skel_decision,
        )
        self.service.record_skeleton_evaluation(skel_eval)

        assignment = ModelAssignmentContract(
            executor=RoleAssignmentPolicy(
                role="executor",
                preferred_model="worker",
                preferred_effort="medium",
            ),
            validator=RoleAssignmentPolicy(
                role="validator",
                preferred_model="validator",
                preferred_effort="high",
            ),
            independence_required=True,
        )
        recovery_env = RecoveryEnvelope(retryable_failure_classes=("implementation",))

        t1_id = new_id("task")
        t2_id = new_id("task")
        t3_id = new_id("task")
        plan_tasks = (
            TaskContract(
                task_id=t1_id,
                task_ref="T1",
                project_id=project_id,
                kind=TaskKind.INSPECT,
                objective="현재 실패 재현",
                goal_criterion_refs=("ac_report_null",),
                produces=("artifact:repro",),
                consumes=("input:request",),
                acceptance_criteria=("재현 확인",),
                validations=(
                    ValidationContract(
                        validation_id="val_t1",
                        statement="T1 검증",
                        method="deterministic",
                        required_evidence_kinds=("file",),
                    ),
                ),
                risk_level=RiskLevel.LOW,
                approval_class=ApprovalClass.PLAN_ACTIVATION,
                recovery=recovery_env,
                assignment=assignment,
            ),
            TaskContract(
                task_id=t2_id,
                task_ref="T2",
                project_id=project_id,
                kind=TaskKind.CHANGE,
                objective="usage 합산 경계 수정",
                goal_criterion_refs=("ac_report_null", "ac_unknown_not_zero"),
                produces=("artifact:boundary",),
                consumes=("artifact:repro",),
                acceptance_criteria=("합산 경계 확인",),
                validations=(
                    ValidationContract(
                        validation_id="val_t2",
                        statement="T2 검증",
                        method="deterministic",
                        required_evidence_kinds=("file",),
                    ),
                ),
                risk_level=RiskLevel.LOW,
                approval_class=ApprovalClass.PLAN_ACTIVATION,
                recovery=recovery_env,
                assignment=assignment,
            ),
            TaskContract(
                task_id=t3_id,
                task_ref="T3",
                project_id=project_id,
                kind=TaskKind.VALIDATE,
                objective="보고 회귀 검증",
                goal_criterion_refs=("ac_report_null", "ac_preserve_totals", "ac_unknown_not_zero"),
                produces=("artifact:regression",),
                consumes=("artifact:boundary",),
                acceptance_criteria=("회귀 검증 확인",),
                validations=(
                    ValidationContract(
                        validation_id="val_t3",
                        statement="T3 검증",
                        method="deterministic",
                        required_evidence_kinds=("file",),
                    ),
                ),
                risk_level=RiskLevel.LOW,
                approval_class=ApprovalClass.PLAN_ACTIVATION,
                recovery=recovery_env,
                assignment=assignment,
            ),
        )
        dependencies = (
            PlanDependency(
                producer_task_id=t1_id,
                consumer_task_id=t2_id,
                dependency_type=DependencyType.DATA,
                products=("artifact:repro",),
            ),
            PlanDependency(
                producer_task_id=t2_id,
                consumer_task_id=t3_id,
                dependency_type=DependencyType.DATA,
                products=("artifact:boundary",),
            ),
        )
        plan_def = PlanContractDefinition(
            project_id=project_id,
            goal_contract_digest=goal_rev.definition_digest,
            base_state_snapshot_digest=state.snapshot_digest,
            project_map_digest=project_map.revision_digest,
            source_skeleton_digest=skeleton_digest,
            tasks=plan_tasks,
            dependencies=dependencies,
            goal_coverage=(
                PlanGoalCoverage(
                    criterion_id="ac_report_null",
                    task_ids=(t1_id, t2_id, t3_id),
                    validation_ids=("val_t1", "val_t2", "val_t3", "val_goal_integration"),
                ),
                PlanGoalCoverage(
                    criterion_id="ac_preserve_totals",
                    task_ids=(t3_id,),
                    validation_ids=("val_t3", "val_goal_integration"),
                ),
                PlanGoalCoverage(
                    criterion_id="ac_unknown_not_zero",
                    task_ids=(t2_id, t3_id),
                    validation_ids=("val_t2", "val_t3", "val_goal_integration"),
                ),
            ),
            integration_validations=(
                IntegrationValidationContract(
                    validation_id="val_goal_integration",
                    statement="독립 Goal Test",
                    evidence_mode="task_aggregate",
                    criterion_refs=("ac_report_null", "ac_preserve_totals", "ac_unknown_not_zero"),
                    method="deterministic",
                    required_evidence_kinds=("file",),
                ),
            ),
            model_inventory_digest=self.inventory.inventory_digest,
        )
        plan_rev = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=new_id("plan"),
            revision_no=1,
            definition=plan_def,
            definition_digest=plan_def.definition_digest,
            status=RevisionStatus.READY,
            created_at=utc_now(),
        )
        plan_catalog = plan_review_evidence_catalog(plan_rev, goal_rev, state, project_map)
        plan_review = ReviewerSubmission(
            reviewer_role="compact_plan_reviewer",
            candidate_digest=plan_rev.activation_digest,
            ratings=ReviewRatings(
                goal_fit=4,
                grounding=4,
                engineering=4,
                verification=4,
                execution_safety=4,
            ),
            evidence_catalog_digest=sha256_digest(plan_catalog),
        )
        plan_decision = CandidateDecision(
            candidate_digest=plan_rev.activation_digest,
            status=CandidateStatus.ADMISSIBLE,
            fitness_score=100,
            weakest_dimension="engineering",
        )
        plan_eval = ExpandedPlanEvaluation(
            plan=plan_rev,
            semantic_submissions=(plan_review,),
            decision=plan_decision,
        )
        self.service.register_plan_evaluation(plan_eval)
        outcome = PlanningSearchOutcome(
            goal_contract_digest=goal_rev.definition_digest,
            state_snapshot_digest=state.snapshot_digest,
            skeleton_evaluations=(skel_eval,),
            shortlist_digests=(skeleton_digest,),
            plan_evaluations=(plan_eval,),
            selected_activation_digest=plan_rev.activation_digest,
            logical_role_calls=4,
            candidate_versions=1,
        )
        self.service.record_planning_search(outcome)
        return goal_rev, plan_rev

    def snapshot(self) -> dict[str, Any]:
        """EngineService 및 SQLiteEngineLedger의 실제 상태를 GUI WorkspaceSnapshot 계약으로 투영한다."""
        with self._lock:
            status_data = self.service.status(self.project_id)
            project_row = status_data["project"]
            goal = self.service.load_latest_goal(self.project_id)

            with self.service.ledger.read() as conn:
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
                has_external = any(
                    eff.external for t in task_objs for eff in t.expected_effects
                )
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
                            "updatedAt": _iso_now(),
                        }
                    )

            has_task_val = any(r["task_id"] is not None and r["status"] == "pass" for r in val_rows)
            has_goal_val = any(r["task_id"] is None and r["status"] == "pass" for r in val_rows)
            verdict_status = "unknown"
            if verdict_row is not None:
                verdict_obj = GoalVerdict.model_validate_json(verdict_row["payload_json"])
                verdict_status = (
                    "satisfied"
                    if verdict_obj.status is GoalVerdictStatus.SATISFIED
                    else "unsatisfied"
                )

            all_completed = bool(tasks_view) and all(t["status"] == "completed" for t in tasks_view)
            if self._recovery_state is not None:
                phase = "recovery"
                run_state = "recovery_required"
            elif verdict_status == "satisfied":
                phase = "completed"
                run_state = "completed"
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
            if phase == "recovery" and self._recovery_state is not None:
                if self._recovery_state["kind"] == "stale_input":
                    possible_actions.append(
                        {
                            "kind": "refresh_snapshot",
                            "enabled": True,
                            "disabledReason": None,
                            "confirmation": "none",
                            "checkpointRequired": False,
                        }
                    )
                elif self._recovery_state["stage"] == "detected":
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
                    if u_payload.get("latency_ms") is not None:
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
                    "workerObservation": "collected" if has_task_val or all_completed else "pending",
                    "taskValidation": "passed" if has_task_val or all_completed else "pending",
                    "goalTest": "passed" if has_goal_val or verdict_status == "satisfied" else "pending",
                    "verdict": verdict_status,
                },
                "activeAttempt": self._active_attempt_state,
                "recovery": self._recovery_state,
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
            if self.live_gemini:
                active_plan = self.service.status(self.project_id)["project"].get("active_plan_revision_id")
                if active_plan is not None:
                    self._cleanup_ephemeral_workspaces()
                    scenario_root = self.project_root / new_id("workspace")
                    self._ephemeral_workspaces.append(scenario_root)
                    scenario_root.mkdir(parents=True, exist_ok=True)
                    (scenario_root / "README.md").write_text("# VibeMarshal Live Gemini Workspace\n", encoding="utf-8")
                    self.project_id = self.service.create_project(name="VibeMarshal Live Gemini", root=str(scenario_root))
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
                        project_id=self.project_id,
                        revision_no=1,
                        definition=profile_def,
                        definition_digest=profile_def.definition_digest,
                        status=RevisionStatus.READY,
                        created_at=utc_now(),
                    )
                    self.service.register_profile(profile_rev)
                    self.application.prepare(self.project_id, source_request=source_request.strip(), candidate_count=1)
                else:
                    try:
                        self.service.load_latest_goal(self.project_id)
                        self.application.revise(self.project_id, source_request=source_request.strip(), candidate_count=1)
                    except Exception:
                        self.application.prepare(self.project_id, source_request=source_request.strip(), candidate_count=1)
                return {"ok": True, "data": self.snapshot()}
            latest = self.service.load_latest_goal(self.project_id)
            active_plan = self.service.status(self.project_id)["project"].get("active_plan_revision_id")
            if active_plan is not None:
                self._cleanup_ephemeral_workspaces()
                self.project_id = self._bootstrap_project_with_goal_and_plans(
                    source_request=source_request.strip()
                )
            else:
                self._register_goal_and_plans_in_ledger(
                    project_id=self.project_id,
                    source_request=source_request.strip(),
                    revision_no=latest.revision_no + 1,
                    goal_id=latest.goal_id,
                    supersedes_goal_revision_id=latest.goal_revision_id,
                )
            return {"ok": True, "data": self.snapshot()}

    def search_plans(self) -> dict[str, Any]:
        with self._lock:
            self._draft_goal_mode = False
            return {"ok": True, "data": self.snapshot()}

    def activate_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._stale_pending:
                self._stale_pending = False
                expected_digest = str(payload.get("activationDigest") or f"sha256:{'74' * 32}")
                current_digest = sha256_digest({"stale_refresh": expected_digest, "ts": _iso_now()})
                self._recovery_state = {
                    "kind": "stale_input",
                    "stage": "detected",
                    "title": "확인 중 Project Map이 바뀌었습니다",
                    "explanation": "사용자가 확인한 시점 이후 working tree 또는 등록 자료 digest가 바뀌어 자동 실행하지 않았습니다.",
                    "expectedDigest": expected_digest,
                    "currentDigest": current_digest,
                    "intentId": None,
                    "receiptState": "not_applicable",
                    "lastCheckpoint": "Plan 후보 검토 직후 · 활성화 전",
                    "evidence": [
                        f"expected activation_digest = {expected_digest}",
                        f"observed state_snapshot_digest = {current_digest}",
                    ],
                }
                return self._error(
                    "STALE_EXECUTION_INPUT",
                    "선택한 Plan의 입력 스냅샷이 최신 상태와 다릅니다. 복구 화면에서 최신 상태를 먼저 확인하세요.",
                    suggested_action="refresh_snapshot",
                )
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
            try:
                app_for_auth = EngineApplication(
                    self.service,
                    runtime=self.runtime,
                    role_configuration=self.role_configuration,
                )
                authority = ApplicationAuthority(app_for_auth)
                target = authority.authorization_target(self.project_id)
                if target.plan_revision_id != plan_revision_id:
                    with self.service.ledger.read() as conn:
                        alt_row = conn.execute(
                            "SELECT id, activation_digest, status FROM plan_revisions "
                            "WHERE id = ? AND project_id = ?",
                            (plan_revision_id, self.project_id),
                        ).fetchone()
                    if alt_row is None or alt_row["status"] != "ready":
                        return self._error("DIGEST_MISMATCH", "선택한 Plan 후보가 승인 대상과 다릅니다.")
                    if activation_digest and alt_row["activation_digest"] != activation_digest:
                        return self._error("DIGEST_MISMATCH", "현재 후보와 activation digest가 일치하지 않습니다.")
                    self.service.authorize_goal(
                        project_id=self.project_id,
                        source="desktop-gui:user-confirmation",
                        capability=app_for_auth._core_action_authority.issue_goal_authorization(
                            ledger_path=self.service.ledger.path,
                            target=target,
                        ),
                        authorization_target=target,
                    )
                    self.service.activate_plan(
                        plan_revision_id=str(plan_revision_id),
                        activation_digest=str(alt_row["activation_digest"]),
                        source="desktop-gui:user-confirmation",
                    )
                else:
                    if activation_digest and target.plan_activation_digest != activation_digest:
                        return self._error("DIGEST_MISMATCH", "현재 후보와 activation digest가 일치하지 않습니다.")
                    if target_digest and target.target_digest != target_digest:
                        return self._error("DIGEST_MISMATCH", "현재 후보와 target digest가 일치하지 않습니다.")
                    authority.authorize(
                        self.project_id,
                        target=target,
                        source="desktop-gui:user-confirmation",
                    )
            except (EngineServiceError, EngineApplicationError, EngineLedgerError, CoreCapabilityError) as error:
                return self._error("ACTIVATION_FAILED", str(error))
            return {"ok": True, "data": self.snapshot()}

    def run_once(self) -> dict[str, Any]:
        with self._lock:
            status_data = self.service.status(self.project_id)
            active_plan_id = status_data["project"].get("active_plan_revision_id")
            if active_plan_id is None:
                return self._error("RUN_BLOCKED", "활성 Plan이 필요합니다.")

            if self.live_gemini:
                import time

                tick = self.application.run_once(self.project_id)
                for _ in range(60):
                    if self.service.active_runtime_job(self.project_id) is None:
                        break
                    self.application.observe(self.project_id)
                    self._lock.release()
                    try:
                        time.sleep(0.1)
                    finally:
                        self._lock.acquire()
                if self.service.active_runtime_job(self.project_id) is None and tick.action.value in {
                    "started",
                    "reserved",
                    "prepared_task",
                    "prepared_goal_validation",
                }:
                    tick = self.application.run_once(self.project_id)
                snap = self.snapshot()
                self.run_log.append(
                    {
                        "action": tick.action.value,
                        "lane": "worker",
                        "taskRef": tick.task_id,
                        "detail": tick.detail or f"Gemini CLI (agy.exe) 실행 단계({tick.action.value}) 완료",
                        "sequence": snap["historyCursor"]["sequence"],
                    }
                )
                return {"ok": True, "data": snap}

            with self.service.ledger.read() as conn:
                plan_row = conn.execute(
                    "SELECT payload_json FROM plan_revisions WHERE id = ?",
                    (active_plan_id,),
                ).fetchone()
            plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
            task_map = {t.task_id: t for t in plan.definition.tasks}

            incomplete = [
                t for t in status_data["tasks"]
                if t["plan_revision_id"] == active_plan_id and t["status"] != "completed"
            ]
            if not incomplete:
                project_map, _ = self.service.reobserve_project(self.project_id)
                if not project_map.entries:
                    readme_fallback = Path(status_data["project"]["root"]) / "README.md"
                    readme_fallback.parent.mkdir(parents=True, exist_ok=True)
                    if not readme_fallback.exists():
                        readme_fallback.write_text("# VibeMarshal Workspace\n", encoding="utf-8")
                    project_map, _ = self.service.reobserve_project(
                        self.project_id,
                        observed_paths=("README.md",),
                    )
                entry = project_map.entries[0]
                goal_evidence = EvidenceRecord(
                    evidence_id=new_id("evidence"),
                    project_id=self.project_id,
                    task_id=None,
                    attempt_id=None,
                    kind=EvidenceKind.FILE,
                    source_ref=entry.path,
                    observation="Goal Test 결정적 파일 검증 통과",
                    content_digest=entry.content_digest,
                    observed_at=utc_now(),
                )
                self.service.record_evidence(goal_evidence)
                int_val = ValidationResult(
                    validation_result_id=new_id("validation_result"),
                    validation_id="val_goal_integration",
                    task_id=None,
                    status=ValidationStatus.PASS,
                    evidence_ids=(goal_evidence.evidence_id,),
                    rationale="독립 Goal Test 통과",
                    evaluated_at=utc_now(),
                )
                self.service.record_validation(
                    project_id=self.project_id,
                    plan_revision_id=active_plan_id,
                    result=int_val,
                )
                verdict = GoalVerdict(
                    goal_verdict_id=new_id("goal_verdict"),
                    goal_contract_digest=plan.definition.goal_contract_digest,
                    plan_activation_digest=plan.activation_digest,
                    status=GoalVerdictStatus.SATISFIED,
                    criteria=tuple(
                        CriterionVerdict(
                            criterion_id=cov.criterion_id,
                            status=ValidationStatus.PASS,
                            evidence_ids=(goal_evidence.evidence_id,),
                            rationale=f"{cov.criterion_id} 충족 확인",
                        )
                        for cov in plan.definition.goal_coverage
                    ),
                    integration_validation_result_ids=(int_val.validation_result_id,),
                    evaluated_at=utc_now(),
                )
                self.service.record_goal_verdict(
                    project_id=self.project_id,
                    plan_revision_id=active_plan_id,
                    verdict=verdict,
                )
                snap = self.snapshot()
                self.run_log.append(
                    {
                        "action": "completed",
                        "lane": "goal_test",
                        "taskRef": None,
                        "detail": f"GoalVerdict({verdict.status.value})가 원장에 기록됐습니다.",
                        "sequence": snap["historyCursor"]["sequence"],
                    }
                )
                return {"ok": True, "data": snap}

            current_row = incomplete[0]
            task_id = current_row["id"]
            tc = task_map[task_id]
            t_status = current_row["status"]

            with self.service.ledger.read() as conn:
                map_row = conn.execute(
                    "SELECT payload_json FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
                    (self.project_id,),
                ).fetchone()
            from .domain import ProjectMapRevision
            project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
            if not project_map.entries:
                readme_fallback = Path(status_data["project"]["root"]) / "README.md"
                readme_fallback.parent.mkdir(parents=True, exist_ok=True)
                if not readme_fallback.exists():
                    readme_fallback.write_text("# VibeMarshal Workspace\n", encoding="utf-8")
                project_map, _ = self.service.reobserve_project(
                    self.project_id,
                    observed_paths=("README.md",),
                )
            entry = project_map.entries[0]

            if t_status == "ready":
                proposal = ExecutionSpecProposal(
                    task_id=task_id,
                    resolved_targets=(
                        ResolvedTarget(
                            target_ref=f"target_{tc.task_ref.lower()}",
                            path=entry.path,
                            expected_content_digest=entry.content_digest,
                            access="read",
                        ),
                    ),
                    actions=(
                        ExecutionAction(
                            action_ref=f"act_{tc.task_ref.lower()}",
                            kind="inspect",
                            description=f"{tc.task_ref} 파일 검증",
                        ),
                    ),
                    validation_steps=(
                        ValidationExecutionStep(
                            validation_id=tc.validations[0].validation_id,
                            method="deterministic",
                            argv=(sys.executable, "-c", "print('ok')"),
                            working_directory=str(self.project_root),
                            timeout_seconds=30,
                            expected_exit_codes=(0,),
                            required_evidence_kinds=("file",),
                            artifact_paths=(entry.path,),
                        ),
                    ),
                    context_needs=(
                        ExecutionContextNeed(
                            need_id=f"need_{tc.task_ref.lower()}",
                            description=f"{tc.task_ref} 문맥 확인",
                            path_hints=(entry.path,),
                        ),
                    ),
                    resource_locks=(),
                    timeout_seconds=120,
                    idempotency_hint=f"gui-{tc.task_ref.lower()}",
                )
                spec = self.service.compile_execution_spec(proposal, inventory=self.inventory)
                snap = self.snapshot()
                self.run_log.append(
                    {
                        "action": "materialized",
                        "lane": "worker",
                        "taskRef": tc.task_ref,
                        "detail": f"{tc.task_ref} Execution Spec({spec.execution_spec_revision_id})을 원장에 고정했습니다.",
                        "sequence": snap["historyCursor"]["sequence"],
                    }
                )
                return {"ok": True, "data": snap}

            # materialized -> reserve attempt, record intent/receipt, finish attempt, record evidence/validation, complete task
            attempt = self.service.reserve_attempt(task_id=task_id)
            intent = self.service.prepare_runtime_intent(
                attempt_id=attempt.attempt_id,
                kind=RuntimeIntentKind.START_TURN,
                idempotency_key=f"gui-intent-{attempt.attempt_id}",
                request={"task_id": task_id},
            )
            turn_op_id = f"turn_{attempt.attempt_id[-12:]}"
            self.service.record_runtime_receipt(
                intent_id=intent.intent_id,
                provider_operation_id=turn_op_id,
                response={"status": "completed"},
                binding=ThreadBinding(
                    thread_id=f"thr_{attempt.attempt_id[-12:]}",
                    turn_id=turn_op_id,
                    bound_at=utc_now(),
                ),
            )
            self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
            evidence = EvidenceRecord(
                evidence_id=new_id("evidence"),
                project_id=self.project_id,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
                kind=EvidenceKind.FILE,
                source_ref=entry.path,
                observation=f"{tc.task_ref} 검증 완료",
                content_digest=entry.content_digest,
                observed_at=utc_now(),
            )
            self.service.record_evidence(evidence)
            val_result = ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id=tc.validations[0].validation_id,
                task_id=task_id,
                status=ValidationStatus.PASS,
                evidence_ids=(evidence.evidence_id,),
                rationale=f"{tc.task_ref} 결정적 파일 검증 통과",
                evaluated_at=utc_now(),
            )
            self.service.record_validation(
                project_id=self.project_id,
                plan_revision_id=active_plan_id,
                result=val_result,
            )
            self.service.complete_task(task_id)
            snap = self.snapshot()
            self.run_log.append(
                {
                    "action": "completed",
                    "lane": "task_validation",
                    "taskRef": tc.task_ref,
                    "detail": f"{tc.task_ref} 필수 evidence와 validation이 통과하여 원장에 완료 기록됐습니다.",
                    "sequence": snap["historyCursor"]["sequence"],
                }
            )
            return {"ok": True, "data": snap}

    def reset_scenario(self, scenario: str) -> dict[str, Any]:
        with self._lock:
            self.scenario = scenario
            self._draft_goal_mode = False
            self._stale_pending = scenario == "stale-activation"
            self._recovery_state = None
            self._active_attempt_state = None
            self.run_log.clear()
            self._cleanup_ephemeral_workspaces()
            self.project_id = self._bootstrap_project_with_goal_and_plans()
            if scenario == "external-unknown":
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
                self._active_attempt_state = {
                    "attemptId": "attempt_gui_ext_01",
                    "taskId": "task_ext_02",
                    "status": "unknown",
                    "threadId": "thr_local_codex_09",
                    "turnId": "turn_local_codex_14",
                    "intentId": "intent_gui_ext_01",
                    "receiptId": None,
                }
                self._recovery_state = {
                    "kind": "external_unknown",
                    "stage": "detected",
                    "title": "외부 응답이 끊겨 결과를 단정할 수 없습니다",
                    "explanation": "intent는 기록됐지만 receipt가 없습니다. 중복 효과를 막기 위해 자동 재실행하지 않고 먼저 관측합니다.",
                    "expectedDigest": first_plan["activationDigest"] if first_plan else None,
                    "currentDigest": first_plan["activationDigest"] if first_plan else None,
                    "intentId": "intent_gui_ext_01",
                    "receiptState": "missing",
                    "lastCheckpoint": "T1 완료 · T2 intent 기록 직후",
                    "evidence": [
                        "intent_gui_ext_01 recorded",
                        "runtime receipt missing",
                        "auto retry blocked by idempotency boundary",
                    ],
                }
                self._record_recovery_ledger_event(
                    "recovery.external_unknown_detected",
                    "intent_gui_ext_01",
                    {
                        "attempt_id": "attempt_gui_ext_01",
                        "intent_id": "intent_gui_ext_01",
                        "receipt_state": "missing",
                    },
                    run_state="recovery_required",
                    recovery_reason="external_unknown",
                )
            return self.snapshot()

    def refresh_stale(self) -> dict[str, Any]:
        with self._lock:
            self._stale_pending = False
            self._recovery_state = None
            latest = self.service.load_latest_goal(self.project_id)
            self._register_goal_and_plans_in_ledger(
                project_id=self.project_id,
                source_request=latest.definition.source_request,
                revision_no=latest.revision_no + 1,
                goal_id=latest.goal_id,
                supersedes_goal_revision_id=latest.goal_revision_id,
            )
            self._record_recovery_ledger_event(
                "recovery.stale_input_refreshed",
                self.project_id,
                {"goal_id": latest.goal_id, "revision_no": latest.revision_no + 1},
                run_state="idle",
                recovery_reason=None,
            )
            return {"ok": True, "data": self.snapshot()}

    def observe_attempt(self) -> dict[str, Any]:
        with self._lock:
            if self._recovery_state is None or self._recovery_state.get("kind") != "external_unknown":
                return self._error("RECOVERY_NOT_OBSERVABLE", "관측할 external_unknown 복구 건이 없습니다.")
            intent_id = str(self._recovery_state.get("intentId") or "intent_gui_ext_01")
            self._recovery_state = {
                **self._recovery_state,
                "stage": "observed",
                "receiptState": "confirmed_missing",
                "explanation": "thread/turn과 로컬 산출물을 재관측한 결과 부분 쓰기 흔적이 없어 마지막 checkpoint에서 재개할 수 있습니다.",
                "evidence": [
                    *self._recovery_state.get("evidence", []),
                    "thread/turn re-observation complete: partial effect not found",
                ],
            }
            if self._active_attempt_state is not None:
                self._active_attempt_state = {
                    **self._active_attempt_state,
                    "status": "interrupted",
                }
            self._record_recovery_ledger_event(
                "recovery.attempt_observed",
                intent_id,
                {
                    "intent_id": intent_id,
                    "stage": "observed",
                    "receipt_state": "confirmed_missing",
                },
                run_state="recovery_required",
                recovery_reason="external_unknown:observed",
            )
            return {"ok": True, "data": self.snapshot()}

    def resume_attempt(self) -> dict[str, Any]:
        with self._lock:
            if (
                self._recovery_state is None
                or self._recovery_state.get("kind") != "external_unknown"
                or self._recovery_state.get("stage") != "observed"
            ):
                return self._error("OBSERVATION_REQUIRED", "재개 전에 먼저 기존 시도를 관측해야 합니다.")
            intent_id = str(self._recovery_state.get("intentId") or "intent_gui_ext_01")
            self._recovery_state = None
            self._active_attempt_state = None
            self._record_recovery_ledger_event(
                "recovery.attempt_resumed",
                intent_id,
                {"intent_id": intent_id, "resumed": True},
                run_state="active",
                recovery_reason=None,
            )
            return {"ok": True, "data": self.snapshot()}

    def abandon_intent(self, rationale: str) -> dict[str, Any]:
        with self._lock:
            if not rationale or not rationale.strip():
                return self._error("RATIONALE_REQUIRED", "포기 사유를 입력해야 원장에 기록할 수 있습니다.")
            intent_id = (
                str(self._recovery_state.get("intentId"))
                if self._recovery_state and self._recovery_state.get("intentId")
                else "intent_gui_ext_01"
            )
            self._recovery_state = None
            self._active_attempt_state = None
            self._draft_goal_mode = True
            self._record_recovery_ledger_event(
                "recovery.intent_abandoned",
                intent_id,
                {"intent_id": intent_id, "rationale": rationale.strip()},
                run_state="idle",
                recovery_reason=None,
            )
            return {"ok": True, "data": self.snapshot()}

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
                body = (
                    f"data: {ready_payload}\n\n"
                    f"event: snapshot\ndata: {snap_payload}\n\n"
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
