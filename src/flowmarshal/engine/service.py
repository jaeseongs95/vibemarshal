from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .context import AdditionalContextRequest
from .domain import (
    ApprovalClass,
    AttemptKind,
    AttemptRecord,
    AttemptStatus,
    BudgetUsageRecord,
    BudgetStage,
    CandidateDecision,
    CandidateStatus,
    ContextManifest,
    ContextSourceRegistration,
    ContextSourceRegistrationKind,
    DeterministicValidationObservation,
    ExecutionSpecProposal,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    GoalContractRevision,
    GoalAuthorization,
    GoalOperatingPolicy,
    GoalVerdict,
    GoalVerdictStatus,
    ManualValidationObservation,
    ExternalValidationObservation,
    PlanContractRevision,
    PlanSkeletonCandidate,
    PROVIDER_TERMINAL_STATUSES,
    ProjectMapRevision,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RecoveryAssessment,
    RepairAction,
    RevisionStatus,
    RuntimeIntentKind,
    RuntimeIntentRecord,
    RuntimeIntentStatus,
    RuntimeJob,
    RuntimeJobKind,
    RuntimeJobObservation,
    RuntimeJobObservationKind,
    RuntimeJobStatus,
    RuntimeReceipt,
    StateFact,
    StateSnapshot,
    TaskExecutionSpecRevision,
    TaskExecutionSpecDefinition,
    TaskRuntimeStatus,
    ThreadBinding,
    ValidationResult,
    ValidationStatus,
    ValidationExecutionStep,
    ResolvedTarget,
    derive_candidate_decision,
    new_id,
    utc_now,
    validate_reviewer_submission_evidence,
)
from .ledger import EngineLedgerError, SQLiteEngineLedger
from .models import AssignmentResolver, ModelInventory
from .planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    PlanningSearchOutcome,
    plan_gate,
    plan_review_evidence_catalog,
    skeleton_gate,
    skeleton_review_evidence_catalog,
)
from .planning_feedback import skeleton_semantic_digest

if TYPE_CHECKING:
    from .runtime import RuntimeObservation


class EngineServiceError(RuntimeError):
    pass


class GoalAuthorizationRequired(EngineServiceError):
    code = "GOAL_AUTHORIZATION_REQUIRED"
    effects_started = False

    def __init__(self, changes: tuple[dict[str, Any], ...]) -> None:
        self.changes = changes
        super().__init__("GOAL_AUTHORIZATION_REQUIRED: 사용자 목표·대상·효과·정책 판단이 필요합니다: "
                         + canonical_json(changes))


class ContextRequiredError(EngineServiceError):
    def __init__(self, request: AdditionalContextRequest) -> None:
        self.request = request
        super().__init__("CONTEXT_REQUIRED: " + canonical_json(request))


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _same_assignment(left: Any, right: Any) -> bool:
    from .model_lock import verify_binding
    try:
        if right is None or left.operational_binding is None or right.operational_binding is None:
            return False
        if right.inventory_digest != right.operational_binding.inventory_digest:
            return False
        verify_binding(right.operational_binding, left.operational_binding.inventory,
                       role=right.role, model=right.model, effort=right.effort)
        return (left.model_dump(exclude={"inventory_digest", "operational_binding"})
                == right.model_dump(exclude={"inventory_digest", "operational_binding"})
                and left.operational_binding.lock_digest == right.operational_binding.lock_digest)
    except ValueError:
        return False


class EngineService:
    """FlowMarshal Engine에서 권위 상태를 전이할 수 있는 유일한 서비스."""

    def __init__(
        self,
        ledger: SQLiteEngineLedger,
        *,
        assignment_resolver: AssignmentResolver | None = None,
    ) -> None:
        self.ledger = ledger
        self.assignment_resolver = assignment_resolver or AssignmentResolver()

    def initialize(self) -> None:
        self.ledger.initialize()

    def create_project(self, *, name: str, root: Path | str, project_id: str | None = None) -> str:
        resolved_root = Path(root).resolve()
        if not resolved_root.is_dir():
            raise EngineServiceError("프로젝트 root가 존재하는 디렉터리가 아닙니다.")
        if project_id is not None and re.fullmatch(r"project_[0-9a-f]{32}", project_id) is None:
            raise EngineServiceError("CHECKPOINT_PROJECT_ID_INVALID: 보존할 프로젝트 식별자가 유효하지 않습니다.")
        project_id = project_id or new_id("project")
        artifact_root = (self.ledger.artifact_root / project_id).resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        with self.ledger.transaction() as tx:
            now = tx.now
            try:
                tx.connection.execute(
                    "INSERT INTO projects "
                    "(id, name, root, artifact_root, run_state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'idle', ?, ?)",
                    (project_id, name, str(resolved_root), str(artifact_root), now, now),
                )
            except sqlite3.IntegrityError as error:  # type: ignore[name-defined]
                raise EngineServiceError("이미 등록된 프로젝트 root입니다.") from error
            tx.history(
                project_id,
                "project.created",
                "project",
                project_id,
                {"name": name, "root": str(resolved_root), "artifact_root": str(artifact_root)},
            )
        return project_id

    def register_context_source(self, registration: ContextSourceRegistration) -> None:
        source = Path(registration.path).resolve(strict=True)
        if not source.is_file():
            raise EngineServiceError("Context source는 존재하는 파일이어야 합니다.")
        if str(source) != registration.path:
            raise EngineServiceError("Context source path는 정규화된 절대 경로여야 합니다.")
        actual_digest = sha256_bytes(source.read_bytes())
        if actual_digest != registration.content_digest:
            raise EngineServiceError("Context source digest가 현재 파일과 다릅니다.")
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (registration.project_id,))
            try:
                tx.connection.execute(
                    "INSERT INTO context_source_registrations "
                    "(id, project_id, kind, path, content_digest, payload_json, registered_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        registration.context_source_id,
                        registration.project_id,
                        registration.kind.value,
                        registration.path,
                        registration.content_digest,
                        canonical_json(registration),
                        registration.registered_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:  # type: ignore[name-defined]
                raise EngineServiceError("같은 Context source path 또는 digest가 이미 등록됐습니다.") from error
            tx.history(
                registration.project_id,
                "context_source.registered",
                "context_source_registration",
                registration.context_source_id,
                {
                    "kind": registration.kind.value,
                    "path": registration.path,
                    "content_digest": registration.content_digest,
                },
            )

    def list_context_sources(self, project_id: str) -> tuple[ContextSourceRegistration, ...]:
        with self.ledger.read() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM context_source_registrations "
                "WHERE project_id = ? ORDER BY registered_at, rowid",
                (project_id,),
            ).fetchall()
        return tuple(ContextSourceRegistration.model_validate_json(row["payload_json"]) for row in rows)

    def validate_context_sources(
        self,
        project_id: str,
        *,
        required_refs: tuple[str, ...] | None = None,
    ) -> tuple[ContextSourceRegistration, ...]:
        sources = self.list_context_sources(project_id)
        by_id = {item.context_source_id: item for item in sources}
        required = set(by_id) if required_refs is None else set(required_refs)
        missing = required - set(by_id)
        if missing:
            raise EngineServiceError(
                f"ProjectProfile이 등록되지 않은 Context source를 참조합니다: {sorted(missing)}"
            )
        selected = tuple(by_id[item] for item in sorted(required))
        changed: list[str] = []
        for item in selected:
            path = Path(item.path)
            try:
                digest = sha256_bytes(path.read_bytes())
            except OSError:
                changed.append(item.context_source_id)
                continue
            if digest != item.content_digest:
                changed.append(item.context_source_id)
        if changed:
            raise EngineServiceError(
                "CONTEXT_SOURCE_CHANGED: 등록 source의 파일 또는 digest가 바뀌었습니다: "
                + ", ".join(changed)
            )
        return selected

    def register_profile(
        self,
        revision: ProjectProfileRevision,
        *,
        activate: bool = True,
    ) -> None:
        self.validate_context_sources(
            revision.project_id,
            required_refs=revision.definition.context_source_refs,
        )
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (revision.project_id,))
            expected_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM profile_revisions "
                    "WHERE project_id = ?",
                    (revision.project_id,),
                ).fetchone()[0]
            )
            if revision.revision_no != expected_no:
                raise EngineServiceError(f"ProjectProfile revision_no는 {expected_no}여야 합니다.")
            active = tx.maybe_one(
                "SELECT id FROM profile_revisions WHERE project_id = ? AND status = 'active'",
                (revision.project_id,),
            )
            if active is not None and revision.supersedes_profile_revision_id != active["id"]:
                raise EngineServiceError("새 ProjectProfile은 현재 active revision을 supersede해야 합니다.")
            status = "active" if activate else revision.status.value
            now = tx.now
            tx.connection.execute(
                "INSERT INTO profile_revisions "
                "(id, project_id, revision_no, definition_digest, payload_json, status, "
                "supersedes_id, created_at, activated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.profile_revision_id,
                    revision.project_id,
                    revision.revision_no,
                    revision.definition_digest,
                    canonical_json(revision),
                    status,
                    revision.supersedes_profile_revision_id,
                    revision.created_at.isoformat(),
                    now if activate else None,
                ),
            )
            if activate:
                if active is not None:
                    tx.connection.execute(
                        "UPDATE profile_revisions SET status = 'superseded' WHERE id = ?",
                        (active["id"],),
                    )
                tx.connection.execute(
                    "UPDATE projects SET active_profile_revision_id = ?, updated_at = ? WHERE id = ?",
                    (revision.profile_revision_id, now, revision.project_id),
                )
            tx.history(
                revision.project_id,
                "profile.registered" if not activate else "profile.activated",
                "profile_revision",
                revision.profile_revision_id,
                {"definition_digest": revision.definition_digest, "revision_no": revision.revision_no},
            )

    def register_goal(
        self,
        revision: GoalContractRevision,
        *,
        activate: bool = True,
    ) -> None:
        project_id = revision.definition.project_id
        with self.ledger.transaction() as tx:
            project = tx.one("SELECT * FROM projects WHERE id = ?", (project_id,))
            if project["active_profile_revision_id"] is None:
                raise EngineServiceError("GoalContract보다 먼저 ProjectProfile을 활성화해야 합니다.")
            profile = tx.one(
                "SELECT definition_digest FROM profile_revisions WHERE id = ?",
                (project["active_profile_revision_id"],),
            )
            if revision.definition.profile_definition_digest != profile["definition_digest"]:
                raise EngineServiceError("GoalContract가 active ProjectProfile에 결속되지 않았습니다.")
            expected_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM goal_revisions WHERE goal_id = ?",
                    (revision.goal_id,),
                ).fetchone()[0]
            )
            if revision.revision_no != expected_no:
                raise EngineServiceError(f"GoalContract revision_no는 {expected_no}여야 합니다.")
            latest = tx.maybe_one(
                "SELECT id FROM goal_revisions WHERE goal_id = ? "
                "ORDER BY revision_no DESC LIMIT 1",
                (revision.goal_id,),
            )
            existing_project_goal = tx.maybe_one(
                "SELECT id FROM goal_revisions WHERE project_id = ? LIMIT 1",
                (project_id,),
            )
            if latest is None and existing_project_goal is not None:
                raise EngineServiceError("프로젝트에 Goal이 이미 있으므로 새 Goal ID 대신 revise해야 합니다.")
            if latest is None and revision.supersedes_goal_revision_id is not None:
                raise EngineServiceError("첫 GoalContract revision은 supersedes 대상을 가질 수 없습니다.")
            if latest is not None and revision.supersedes_goal_revision_id != latest["id"]:
                raise EngineServiceError("새 GoalContract는 같은 Goal의 최신 revision을 supersede해야 합니다.")
            active = tx.maybe_one(
                "SELECT id, goal_id FROM goal_revisions WHERE project_id = ? AND status = 'active'",
                (project_id,),
            )
            if active is not None and revision.goal_id != active["goal_id"]:
                raise EngineServiceError("active Goal이 있으면 새 Goal create 대신 같은 Goal을 revise해야 합니다.")
            if activate and revision.status is not RevisionStatus.READY:
                raise EngineServiceError("ready GoalContract만 활성화할 수 있습니다.")
            if activate and project["active_plan_revision_id"] is not None:
                raise EngineServiceError("active Plan이 있는 동안 GoalContract를 교체할 수 없습니다.")
            status = "active" if activate else revision.status.value
            now = tx.now
            tx.connection.execute(
                "INSERT INTO goal_revisions "
                "(id, goal_id, project_id, revision_no, definition_digest, payload_json, status, "
                "supersedes_id, created_at, activated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.goal_revision_id,
                    revision.goal_id,
                    project_id,
                    revision.revision_no,
                    revision.definition_digest,
                    canonical_json(revision),
                    status,
                    revision.supersedes_goal_revision_id,
                    revision.created_at.isoformat(),
                    now if activate else None,
                ),
            )
            if activate:
                if active is not None:
                    tx.connection.execute(
                        "UPDATE goal_revisions SET status = 'superseded' WHERE id = ?",
                        (active["id"],),
                    )
                tx.connection.execute(
                    "UPDATE projects SET active_goal_revision_id = ?, run_state = 'idle', "
                    "updated_at = ? WHERE id = ?",
                    (revision.goal_revision_id, now, project_id),
                )
            tx.history(
                project_id,
                "goal.registered" if not activate else "goal.activated",
                "goal_revision",
                revision.goal_revision_id,
                {"definition_digest": revision.definition_digest, "revision_no": revision.revision_no},
            )

    def record_state_snapshot(self, snapshot: StateSnapshot) -> None:
        with self.ledger.transaction() as tx:
            project = tx.one("SELECT * FROM projects WHERE id = ?", (snapshot.project_id,))
            if project["active_goal_revision_id"] is None:
                raise EngineServiceError("StateSnapshot보다 먼저 GoalContract를 활성화해야 합니다.")
            goal = tx.one(
                "SELECT definition_digest FROM goal_revisions WHERE id = ?",
                (project["active_goal_revision_id"],),
            )
            if snapshot.goal_contract_digest != goal["definition_digest"]:
                raise EngineServiceError("StateSnapshot이 active GoalContract용 projection이 아닙니다.")
            tx.connection.execute(
                "UPDATE state_snapshots SET is_current = 0 WHERE project_id = ? "
                "AND goal_contract_digest = ? AND is_current = 1",
                (snapshot.project_id, snapshot.goal_contract_digest),
            )
            tx.connection.execute(
                "INSERT INTO state_snapshots "
                "(id, project_id, goal_contract_digest, version, snapshot_digest, semantic_digest, "
                "scope_fingerprint, payload_json, observed_at, is_current) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    snapshot.snapshot_id,
                    snapshot.project_id,
                    snapshot.goal_contract_digest,
                    snapshot.version,
                    snapshot.snapshot_digest,
                    snapshot.semantic_digest,
                    snapshot.scope_fingerprint,
                    canonical_json(snapshot),
                    snapshot.observed_at.isoformat(),
                ),
            )
            tx.history(
                snapshot.project_id,
                "state.observed",
                "state_snapshot",
                snapshot.snapshot_id,
                {"snapshot_digest": snapshot.snapshot_digest, "scope_fingerprint": snapshot.scope_fingerprint},
            )

    def record_project_map(self, project_map: ProjectMapRevision) -> None:
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (project_map.project_id,))
            expected_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM project_map_revisions "
                    "WHERE project_id = ?",
                    (project_map.project_id,),
                ).fetchone()[0]
            )
            if project_map.revision_no != expected_no:
                raise EngineServiceError(f"ProjectMap revision_no는 {expected_no}여야 합니다.")
            tx.connection.execute(
                "UPDATE project_map_revisions SET is_current = 0 WHERE project_id = ? AND is_current = 1",
                (project_map.project_id,),
            )
            tx.connection.execute(
                "INSERT INTO project_map_revisions "
                "(id, project_id, revision_no, revision_digest, semantic_digest, payload_json, created_at, is_current) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    project_map.project_map_revision_id,
                    project_map.project_id,
                    project_map.revision_no,
                    project_map.revision_digest,
                    project_map.semantic_digest,
                    canonical_json(project_map),
                    project_map.created_at.isoformat(),
                ),
            )
            tx.history(
                project_map.project_id,
                "project_map.recorded",
                "project_map_revision",
                project_map.project_map_revision_id,
                {"revision_digest": project_map.revision_digest, "entry_count": len(project_map.entries)},
            )

    def observe_goal_inputs(self, project_id: str, source_request: str) -> tuple[dict[str, Any], ...]:
        """Goal 활성화 전에도 등록 source를 확인하고 실제 입력만 읽어 반환한다."""

        from .context import ProjectMapper, goal_context_observations

        profile = self.load_active_profile(project_id)
        sources = self.validate_context_sources(
            project_id, required_refs=profile.definition.context_source_refs,
        )
        with self.ledger.read() as connection:
            project = connection.execute("SELECT root FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise EngineServiceError("프로젝트를 찾을 수 없습니다.")
        project_map = ProjectMapper().build(
            project_id=project_id,
            root=project["root"],
            revision_no=1,
            registered_references=(item.path for item in sources if item.kind is ContextSourceRegistrationKind.REFERENCE),
            instruction_sources=(item.path for item in sources if item.kind is ContextSourceRegistrationKind.INSTRUCTION),
            excluded_paths=(self.ledger.artifact_root.resolve(),),
        )
        return goal_context_observations(project_map, source_request)

    def reobserve_project(
        self,
        project_id: str,
        *,
        force_state_revision: bool = False,
    ) -> tuple[ProjectMapRevision, StateSnapshot]:
        """등록 source를 포함해 Project Map과 Goal 관련 파일 상태를 다시 관측한다."""

        from .context import ProjectMapper, state_scope_fingerprint

        goal = self.load_active_goal(project_id)
        profile = self.load_active_profile(project_id)
        with self.ledger.read() as connection:
            project = connection.execute(
                "SELECT root FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            map_row = connection.execute(
                "SELECT payload_json FROM project_map_revisions "
                "WHERE project_id = ? AND is_current = 1",
                (project_id,),
            ).fetchone()
        if project is None:
            raise EngineServiceError("프로젝트를 찾을 수 없습니다.")
        sources = self.validate_context_sources(
            project_id,
            required_refs=profile.definition.context_source_refs,
        )
        current_map = (
            None
            if map_row is None
            else ProjectMapRevision.model_validate_json(map_row["payload_json"])
        )
        observed_map = ProjectMapper().build(
            project_id=project_id,
            root=project["root"],
            revision_no=1 if current_map is None else current_map.revision_no + 1,
            registered_references=(
                item.path
                for item in sources
                if item.kind is ContextSourceRegistrationKind.REFERENCE
            ),
            instruction_sources=(
                item.path
                for item in sources
                if item.kind is ContextSourceRegistrationKind.INSTRUCTION
            ),
            excluded_paths=(self.ledger.artifact_root.resolve(),),
        )
        if current_map is None or observed_map.semantic_digest != current_map.semantic_digest:
            project_map = observed_map
            self.record_project_map(project_map)
        else:
            project_map = current_map

        try:
            current_state = self.load_current_state(project_id, goal.definition_digest)
        except EngineServiceError:
            current_state = None
        map_bound = None
        if current_state is not None:
            map_bound = next(
                (
                    item.value
                    for item in current_state.facts
                    if item.fact_id == "fact_project_map"
                ),
                None,
            )
        if (
            current_state is not None
            and not force_state_revision
            and map_bound == project_map.revision_digest
        ):
            return project_map, current_state

        relevant_paths: set[str] = set()
        with self.ledger.read() as connection:
            rows = connection.execute(
                "SELECT s.payload_json FROM execution_spec_revisions s "
                "JOIN task_contracts t ON t.id = s.task_id "
                "JOIN projects p ON p.active_plan_revision_id = t.plan_revision_id "
                "WHERE p.id = ? AND s.is_current = 1",
                (project_id,),
            ).fetchall()
        for row in rows:
            spec = TaskExecutionSpecRevision.model_validate_json(row["payload_json"])
            relevant_paths.update(item.path for item in spec.definition.resolved_targets)
        entries_by_path = {item.path: item for item in project_map.entries}
        facts = [
            StateFact(
                fact_id="fact_project_map",
                predicate="current project map",
                value=project_map.revision_digest,
                source_ref="project-map",
                evidence_digest=project_map.revision_digest,
                invalidates_on=("project file content changes",),
            )
        ]
        for path in sorted(relevant_paths):
            entry = entries_by_path.get(path)
            digest = None if entry is None else entry.content_digest
            facts.append(
                StateFact(
                    fact_id="fact_target_" + sha256_digest(path).split(":", 1)[1][:20],
                    predicate=f"Goal 관련 target의 현재 content digest: {path}",
                    value=digest,
                    source_ref=path,
                    evidence_digest=sha256_digest(
                        {"path": path, "content_digest": digest, "map": project_map.revision_digest}
                    ),
                    invalidates_on=(f"{path} content changes",),
                )
            )
        state = StateSnapshot(
            snapshot_id=new_id("snapshot"),
            project_id=project_id,
            goal_contract_digest=goal.definition_digest,
            version=1 if current_state is None else current_state.version + 1,
            scope_fingerprint=state_scope_fingerprint(
                goal_digest=goal.definition_digest,
                project_map_digest=project_map.revision_digest,
                refs=relevant_paths,
            ),
            facts=tuple(facts),
            observed_at=utc_now(),
        )
        self.record_state_snapshot(state)
        return project_map, state

    def record_skeleton_evaluation(self, evaluation: CandidateEvaluation) -> None:
        candidate = evaluation.candidate
        with self.ledger.transaction() as tx:
            goal = tx.one(
                "SELECT id, project_id, payload_json FROM goal_revisions "
                "WHERE definition_digest = ?",
                (candidate.goal_contract_digest,),
            )
            project_id = goal["project_id"]
            project = tx.one(
                "SELECT active_goal_revision_id FROM projects WHERE id = ?",
                (project_id,),
            )
            if project["active_goal_revision_id"] != goal["id"]:
                raise EngineServiceError("Skeleton은 active GoalContract에 결속돼야 합니다.")
            state_row = tx.one(
                "SELECT payload_json FROM state_snapshots "
                "WHERE project_id = ? AND goal_contract_digest = ? AND is_current = 1",
                (project_id, candidate.goal_contract_digest),
            )
            map_row = tx.one(
                "SELECT payload_json FROM project_map_revisions "
                "WHERE project_id = ? AND is_current = 1",
                (project_id,),
            )
            goal_contract = GoalContractRevision.model_validate_json(goal["payload_json"])
            state_snapshot = StateSnapshot.model_validate_json(state_row["payload_json"])
            project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
            actual_findings = skeleton_gate(
                candidate,
                goal=goal_contract,
                state=state_snapshot,
                project_map=project_map,
            )
            if evaluation.deterministic_findings != actual_findings:
                raise EngineServiceError(
                    "Skeleton deterministic findings가 Core 재계산 결과와 다릅니다."
                )
            submission = evaluation.semantic_submission
            if submission is not None:
                try:
                    validate_reviewer_submission_evidence(
                        submission,
                        evidence_catalog=skeleton_review_evidence_catalog(
                            candidate, goal_contract, state_snapshot, project_map
                        ),
                        known_task_refs={item.task_ref for item in candidate.tasks},
                    )
                except ValueError as error:
                    raise EngineServiceError(str(error)) from error
                findings = actual_findings + submission.findings
                expected_decision = derive_candidate_decision(
                    candidate_digest=sha256_digest(candidate),
                    findings=findings,
                    ratings=submission.ratings if not findings else None,
                )
            elif actual_findings:
                expected_decision = derive_candidate_decision(
                    candidate_digest=sha256_digest(candidate),
                    findings=actual_findings,
                    ratings=None,
                )
            else:
                expected_decision = CandidateDecision(
                    candidate_digest=sha256_digest(candidate),
                    status=CandidateStatus.REJECTED,
                    finding_codes=("MISSING_SEMANTIC_REVIEW",),
                )
            if evaluation.decision != expected_decision:
                raise EngineServiceError("Skeleton decision이 Core 재계산 결과와 다릅니다.")
            candidate_digest = sha256_digest(candidate)
            tx.connection.execute(
                "INSERT INTO skeleton_candidates "
                "(id, project_id, goal_contract_digest, candidate_digest, graph_signature, "
                "parent_candidate_id, version, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.candidate_id,
                    project_id,
                    candidate.goal_contract_digest,
                    candidate_digest,
                    candidate.graph_signature,
                    candidate.parent_candidate_id,
                    candidate.version,
                    canonical_json(candidate),
                    tx.now,
                ),
            )
            if evaluation.semantic_submission is not None:
                self._insert_review(tx, project_id, "skeleton", evaluation.semantic_submission)
            self._insert_decision(tx, project_id, "skeleton", evaluation.decision)
            tx.history(
                project_id,
                "skeleton.evaluated",
                "skeleton_candidate",
                candidate.candidate_id,
                {"candidate_digest": candidate_digest, "status": evaluation.decision.status.value},
            )

    def validate_initial_skeleton_call(
        self,
        tx: Any,
        *,
        project_id: str,
        goal_digest: str,
        role: str,
        request: dict[str, Any],
    ) -> None:
        """초기 Skeleton 역할 호출을 예약하기 전에 이전 실제 호출과 결속한다.

        이 경계는 새 후보를 아직 ``skeleton_candidates``에 기록하기 전에도 적용된다.
        따라서 후보 자체와 이전 역할 request를 함께 검증하며, 식별·selector를 재구성할 수
        없는 기존 동일 Goal의 Skeleton 호출은 재실행 권한으로 해석하지 않는다.
        """

        if role not in {"skeleton_reviewer", "skeleton_refiner"}:
            return
        payload = request.get("payload")
        if not isinstance(payload, dict):
            raise EngineServiceError("초기 Skeleton 역할 요청 payload가 객체가 아닙니다.")
        catalog = payload.get("evidence_catalog", {})
        if role == "skeleton_reviewer":
            if not isinstance(catalog, dict):
                raise EngineServiceError("초기 Skeleton 검토 요청의 evidence catalog가 객체가 아닙니다.")
            candidate_value = catalog.get("artifact:skeleton")
        else:
            candidate_value = payload.get("candidate")
        if not isinstance(candidate_value, dict):
            raise EngineServiceError("초기 Skeleton 역할 요청에 결속된 후보가 없습니다.")
        try:
            candidate = PlanSkeletonCandidate.model_validate(candidate_value)
        except ValueError as error:
            raise EngineServiceError("초기 Skeleton 역할 요청 후보가 유효하지 않습니다.") from error
        if candidate.goal_contract_digest != goal_digest:
            raise EngineServiceError("초기 Skeleton 역할 요청 후보가 다른 Goal revision에 결속됐습니다.")
        if role == "skeleton_refiner" and candidate.parent_candidate_id is not None:
            raise EngineServiceError("초기 Skeleton 역할에는 root Skeleton 후보만 사용할 수 있습니다.")

        existing_candidate = tx.maybe_one(
            "SELECT payload_json FROM skeleton_candidates WHERE candidate_digest = ?",
            (sha256_digest(candidate),),
        )
        if existing_candidate is not None and existing_candidate["payload_json"] != canonical_json(candidate):
            raise EngineServiceError("원장 Skeleton 후보와 초기 역할 요청 후보가 다릅니다.")

        def candidate_from_prior_call(row: Any) -> PlanSkeletonCandidate:
            try:
                prior_request = json.loads(row["request_json"])
                prior_payload = prior_request["payload"]
                if not isinstance(prior_payload, dict):
                    raise ValueError("payload")
                if row["role"] == "skeleton_reviewer":
                    prior_catalog = prior_payload["evidence_catalog"]
                    if not isinstance(prior_catalog, dict):
                        raise ValueError("evidence_catalog")
                    prior_candidate = prior_catalog["artifact:skeleton"]
                else:
                    prior_candidate = prior_payload["candidate"]
                return PlanSkeletonCandidate.model_validate(prior_candidate)
            except (KeyError, TypeError, ValueError) as error:
                raise EngineServiceError(
                    "기존 초기 Skeleton 역할 호출의 후보 결속을 확인할 수 없습니다."
                ) from error

        prior_calls = tx.all(
            "SELECT * FROM provider_calls WHERE project_id = ? AND goal_contract_digest = ? "
            "AND role IN ('skeleton_reviewer', 'skeleton_refiner') AND status <> 'released' "
            "ORDER BY rowid",
            (project_id, goal_digest),
        )
        matching_calls: list[Any] = []
        for prior in prior_calls:
            prior_candidate = candidate_from_prior_call(prior)
            if prior_candidate.goal_contract_digest != goal_digest:
                raise EngineServiceError("기존 초기 Skeleton 역할 호출의 Goal 결속이 다릅니다.")
            if (
                prior_candidate.candidate_id == candidate.candidate_id
                or sha256_digest(prior_candidate) == sha256_digest(candidate)
                or skeleton_semantic_digest(prior_candidate) == skeleton_semantic_digest(candidate)
            ):
                matching_calls.append(prior)

        for prior in matching_calls:
            if prior["receipt_json"] is None:
                continue
            try:
                receipt = json.loads(prior["receipt_json"])
            except (TypeError, ValueError) as error:
                raise EngineServiceError("기존 초기 Skeleton 역할 receipt를 확인할 수 없습니다.") from error
            if receipt.get("status") == "schema_failed":
                raise EngineServiceError(
                    "이 초기 Skeleton 후보의 schema 실패 역할 호출이 이미 정산되어 재검토·수정 호출을 시작할 수 없습니다."
                )

        if role == "skeleton_refiner" and any(
            prior["role"] == "skeleton_refiner" for prior in matching_calls
        ):
            raise EngineServiceError(
                "이 초기 Skeleton 후보 계보의 수정 호출은 성공·schema 실패를 합쳐 이미 한 번 사용됐습니다."
            )

    def record_planning_search(self, outcome: PlanningSearchOutcome) -> str:
        """등록된 후보와 대조한 검색·실패 피드백 전체를 변경 불가 History에 보존한다."""
        outcome = PlanningSearchOutcome.model_validate(outcome.model_dump(mode="json"))
        digest = sha256_digest(outcome)
        search_id = "planning_search_" + digest[7:]
        with self.ledger.transaction() as tx:
            goal = tx.one(
                "SELECT id, project_id FROM goal_revisions WHERE definition_digest = ?",
                (outcome.goal_contract_digest,),
            )
            project_id = goal["project_id"]
            project = tx.one("SELECT active_goal_revision_id FROM projects WHERE id = ?", (project_id,))
            if project["active_goal_revision_id"] != goal["id"]:
                raise EngineServiceError("Planning search가 active Goal과 다릅니다.")
            state = tx.one(
                "SELECT snapshot_digest FROM state_snapshots WHERE project_id = ? AND is_current = 1",
                (project_id,),
            )
            if state["snapshot_digest"] != outcome.state_snapshot_digest:
                raise EngineServiceError("Planning search가 current StateSnapshot과 다릅니다.")
            for kind, evaluations in (("skeleton", outcome.skeleton_evaluations), ("plan", outcome.plan_evaluations)):
                for evaluation in evaluations:
                    if kind == "skeleton":
                        artifact = evaluation.candidate
                        row = tx.one("SELECT payload_json FROM skeleton_candidates WHERE candidate_digest = ?", (sha256_digest(artifact),))
                        expected_reviews = (
                            (evaluation.semantic_submission,)
                            if evaluation.semantic_submission is not None
                            else ()
                        )
                    else:
                        artifact = evaluation.plan
                        row = tx.one("SELECT payload_json FROM plan_revisions WHERE activation_digest = ?", (artifact.activation_digest,))
                        expected_reviews = evaluation.semantic_submissions
                        if evaluation.adjudication is not None:
                            expected_reviews += (evaluation.adjudication.submission,)
                            adjudication_event = tx.one(
                                "SELECT payload_json FROM history_events WHERE project_id = ? "
                                "AND event_type = 'plan.review_adjudicated' AND entity_id = ?",
                                (project_id, artifact.plan_revision_id),
                            )
                            if json.loads(adjudication_event["payload_json"]) != {
                                "original_decision": evaluation.original_evaluation().decision.model_dump(mode="json"),
                                "adjudication": evaluation.adjudication.model_dump(mode="json"),
                                "decision": evaluation.decision.model_dump(mode="json"),
                            }:
                                raise EngineServiceError("Planning search 재심이 원장 원검토·판정과 다릅니다.")
                    decision = tx.one(
                        "SELECT payload_json FROM candidate_decisions WHERE artifact_kind = ? AND artifact_digest = ?",
                        (kind, evaluation.decision.candidate_digest),
                    )
                    registered_reviews = tx.connection.execute(
                        "SELECT payload_json FROM candidate_reviews "
                        "WHERE project_id = ? AND artifact_kind = ? AND artifact_digest = ?",
                        (project_id, kind, evaluation.decision.candidate_digest),
                    ).fetchall()
                    if (
                        row["payload_json"] != canonical_json(artifact)
                        or decision["payload_json"] != canonical_json(evaluation.decision)
                        or sorted(item["payload_json"] for item in registered_reviews)
                        != sorted(canonical_json(item) for item in expected_reviews)
                    ):
                        raise EngineServiceError("Planning search가 원장 후보·검토·판정과 다릅니다.")
            skeleton_evaluations_by_digest = {
                sha256_digest(item.candidate): item
                for item in outcome.skeleton_evaluations
            }
            for failure in outcome.candidate_schema_failures:
                call = tx.one("SELECT * FROM provider_calls WHERE id = ?", (failure.provider_call_id,))
                from .roles import RoleCallRequest
                try:
                    stored_request = RoleCallRequest.model_validate_json(call["request_json"])
                except ValueError as error:
                    raise EngineServiceError(
                        "격리한 후보 실패의 저장 역할 요청을 검증할 수 없습니다."
                    ) from error
                if (
                    call["project_id"] != project_id
                    or call["goal_contract_digest"] != outcome.goal_contract_digest
                    or call["status"] != "settled"
                    or call["receipt_json"] != canonical_json(failure.receipt)
                    or stored_request.request_digest != failure.receipt.input_digest
                ):
                    raise EngineServiceError("격리한 후보 실패가 실제 정산된 역할 호출과 다릅니다.")
                request_payload = json.loads(call["request_json"]).get("payload", {})
                if not isinstance(request_payload, dict):
                    raise EngineServiceError("격리한 후보 실패의 역할 요청 payload가 객체가 아닙니다.")
                catalog = request_payload.get("evidence_catalog", {})
                if not isinstance(catalog, dict):
                    raise EngineServiceError("격리한 후보 실패의 evidence catalog가 객체가 아닙니다.")
                if failure.operation in {
                    "expand", "refine", "skeleton_review", "initial_skeleton_review",
                    "skeleton_refine",
                }:
                    if failure.operation == "expand":
                        candidate_value = request_payload.get("skeleton")
                    elif failure.operation == "skeleton_refine":
                        candidate_value = request_payload.get("candidate")
                    else:
                        candidate_value = catalog.get("artifact:skeleton")
                    try:
                        request_candidate = PlanSkeletonCandidate.model_validate(candidate_value)
                    except ValueError as error:
                        raise EngineServiceError(
                            "격리한 실패의 역할 요청 Skeleton을 검증할 수 없습니다."
                        ) from error
                    if sha256_digest(request_candidate) != failure.source_skeleton_digest:
                        raise EngineServiceError("격리한 실패가 실제 요청의 Skeleton과 다릅니다.")
                if failure.operation in {"review", "refine", "adjudicate"}:
                    plan_value = catalog.get("artifact:plan_contract")
                    if (plan_value is None
                            or PlanContractRevision.model_validate(plan_value).activation_digest != failure.source_plan_digest):
                        raise EngineServiceError("격리한 실패가 실제 요청의 Plan과 다릅니다.")
                if failure.operation in {"initial_skeleton_review", "skeleton_refine"}:
                    if failure.source_plan_digest is not None:
                        raise EngineServiceError("초기 Skeleton 단계 실패에는 Plan 결속이 있으면 안 됩니다.")
                    source = skeleton_evaluations_by_digest.get(failure.source_skeleton_digest)
                    if source is None:
                        raise EngineServiceError("초기 Skeleton 단계 실패의 원본 후보가 검색에 없습니다.")
                    if failure.operation == "initial_skeleton_review":
                        if (
                            source.semantic_submission is not None
                            or source.decision.status is not CandidateStatus.REJECTED
                        ):
                            raise EngineServiceError(
                                "초기 Skeleton 검토 schema 실패와 성공 검토·admission을 함께 기록할 수 없습니다."
                            )
                    elif source.decision.status is not CandidateStatus.NEEDS_REVISION:
                        raise EngineServiceError(
                            "초기 Skeleton 수정 schema 실패에는 원본 NEEDS_REVISION 판정이 필요합니다."
                        )
            existing = tx.maybe_one(
                "SELECT id FROM history_events WHERE project_id = ? AND event_type = 'planning.search_recorded' AND entity_id = ?",
                (project_id, search_id),
            )
            if existing is None:
                def same_skeleton(left: PlanSkeletonCandidate, right: PlanSkeletonCandidate) -> bool:
                    return (
                        left.candidate_id == right.candidate_id
                        or sha256_digest(left) == sha256_digest(right)
                        or skeleton_semantic_digest(left) == skeleton_semantic_digest(right)
                    )

                def initial_refinement_roots(
                    search: PlanningSearchOutcome,
                ) -> tuple[PlanSkeletonCandidate, ...]:
                    candidates = {
                        item.candidate.candidate_id: item.candidate
                        for item in search.skeleton_evaluations
                    }
                    candidates_by_digest = {
                        sha256_digest(candidate): candidate
                        for candidate in candidates.values()
                    }
                    detailed_children = {
                        item.proposal.skeleton.candidate_id
                        for item in search.plan_refinements
                        if item.proposal.skeleton is not None
                    }
                    roots: dict[str, PlanSkeletonCandidate] = {}

                    def add_root(candidate: PlanSkeletonCandidate) -> None:
                        root = candidate
                        while root.parent_candidate_id is not None:
                            parent = candidates.get(root.parent_candidate_id)
                            if parent is None:
                                raise EngineServiceError(
                                    "Planning search의 초기 Skeleton 수정 계보를 확인할 수 없습니다."
                                )
                            root = parent
                        roots.setdefault(sha256_digest(root), root)

                    for candidate_id, candidate in candidates.items():
                        if candidate.parent_candidate_id is not None and candidate_id not in detailed_children:
                            add_root(candidate)
                    for failure in search.candidate_schema_failures:
                        if failure.operation != "skeleton_refine":
                            continue
                        source = candidates_by_digest.get(failure.source_skeleton_digest)
                        if source is None:
                            raise EngineServiceError(
                                "Planning search의 초기 Skeleton 수정 실패 원본을 확인할 수 없습니다."
                            )
                        add_root(source)
                    return tuple(roots.values())

                prior_searches = tx.all(
                    "SELECT payload_json FROM history_events WHERE project_id = ? "
                    "AND event_type = 'planning.search_recorded' ORDER BY sequence",
                    (project_id,),
                )
                current_candidates = tuple(
                    item.candidate for item in outcome.skeleton_evaluations
                )
                current_initial_refinement_roots = initial_refinement_roots(outcome)
                for row in prior_searches:
                    try:
                        stored = json.loads(row["payload_json"])
                        previous = PlanningSearchOutcome.model_validate(stored["outcome"])
                    except (KeyError, TypeError, ValueError) as error:
                        raise EngineServiceError(
                            "이전 planning search의 복구 계보를 검증할 수 없습니다."
                        ) from error
                    if previous.goal_contract_digest != outcome.goal_contract_digest:
                        continue
                    previous_candidates_by_digest = {
                        sha256_digest(item.candidate): item.candidate
                        for item in previous.skeleton_evaluations
                    }
                    for failure in previous.candidate_schema_failures:
                        if failure.operation not in {
                            "initial_skeleton_review", "skeleton_refine",
                        }:
                            continue
                        source = previous_candidates_by_digest.get(
                            failure.source_skeleton_digest
                        )
                        if source is None:
                            raise EngineServiceError(
                                "이전 초기 Skeleton schema 실패의 원본 후보를 확인할 수 없습니다."
                            )
                        affected = (
                            current_candidates
                            if failure.operation == "initial_skeleton_review"
                            else tuple(
                                candidate
                                for candidate in current_candidates
                                if candidate.parent_candidate_id is None
                            )
                        )
                        if any(same_skeleton(candidate, source) for candidate in affected):
                            raise EngineServiceError(
                                "이전 planning search의 초기 Skeleton schema 실패를 누락·재시도할 수 없습니다."
                            )
                    for previous_root in initial_refinement_roots(previous):
                        if any(
                            same_skeleton(current_root, previous_root)
                            for current_root in current_initial_refinement_roots
                        ):
                            raise EngineServiceError(
                                "이전 planning search에서 사용한 초기 Skeleton 수정 슬롯을 새 후보 ID로 복원할 수 없습니다."
                            )
                tx.history(project_id, "planning.search_recorded", "planning_search", search_id,
                           {"outcome_digest": digest, "outcome": outcome.model_dump(mode="json")})
                for failure in outcome.candidate_schema_failures:
                    tx.history(
                        project_id,
                        "planning.candidate_schema_failed",
                        "provider_call",
                        failure.provider_call_id,
                        {
                            "operation": failure.operation,
                            "source_skeleton_digest": failure.source_skeleton_digest,
                            "source_skeleton_semantic_digest": skeleton_semantic_digest(
                                skeleton_evaluations_by_digest[failure.source_skeleton_digest].candidate
                            ),
                            "source_plan_digest": failure.source_plan_digest,
                            "receipt_digest": sha256_digest(failure.receipt),
                            "search_id": search_id,
                        },
                    )
        return search_id

    def register_plan_evaluation(self, evaluation: ExpandedPlanEvaluation) -> None:
        # typed 입력도 model_copy로 검증을 우회할 수 있으므로 중첩 계약까지 다시 검사한다.
        try:
            evaluation = ExpandedPlanEvaluation.model_validate_json(evaluation.model_dump_json())
            plan = PlanContractRevision.model_validate_json(evaluation.plan.model_dump_json())
        except ValueError as error:
            raise EngineServiceError(f"PlanContract 입력 검증에 실패했습니다: {error}") from error
        definition = plan.definition
        with self.ledger.read() as connection:
            goal_row = connection.execute(
                "SELECT payload_json FROM goal_revisions "
                "WHERE project_id = ? AND definition_digest = ?",
                (definition.project_id, definition.goal_contract_digest),
            ).fetchone()
            state_row = connection.execute(
                "SELECT payload_json FROM state_snapshots "
                "WHERE project_id = ? AND snapshot_digest = ?",
                (definition.project_id, definition.base_state_snapshot_digest),
            ).fetchone()
            map_row = connection.execute(
                "SELECT payload_json FROM project_map_revisions "
                "WHERE project_id = ? AND revision_digest = ?",
                (definition.project_id, definition.project_map_digest),
            ).fetchone()
            skeleton_row = connection.execute(
                "SELECT payload_json FROM skeleton_candidates "
                "WHERE project_id = ? AND candidate_digest = ?",
                (definition.project_id, definition.source_skeleton_digest),
            ).fetchone()
        if any(row is None for row in (goal_row, state_row, map_row, skeleton_row)):
            raise EngineServiceError(
                "Plan evaluation의 Goal·State·Project Map·Skeleton 권위 입력을 찾을 수 없습니다."
            )
        source_skeleton = PlanSkeletonCandidate.model_validate_json(skeleton_row["payload_json"])
        goal_contract = GoalContractRevision.model_validate_json(goal_row["payload_json"])
        state_snapshot = StateSnapshot.model_validate_json(state_row["payload_json"])
        project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
        actual_findings = plan_gate(
            plan,
            source=source_skeleton,
            goal=goal_contract,
            state=state_snapshot,
            project_map=project_map,
        )
        if evaluation.deterministic_findings != actual_findings:
            raise EngineServiceError(
                "Plan deterministic findings가 Core 재계산 결과와 다릅니다."
            )
        all_reviews = evaluation.semantic_submissions
        if evaluation.adjudication is not None:
            try:
                evaluation.adjudication.validate_source(
                    source_evaluation_digest=sha256_digest(evaluation.original_evaluation()),
                    original_submission=evaluation.semantic_submissions[0],
                    evidence_catalog=plan_review_evidence_catalog(plan, goal_contract, state_snapshot, project_map),
                    dispute_evidence_catalog=plan_review_evidence_catalog(plan, goal_contract, state_snapshot, project_map)
                    | {"artifact:skeleton": source_skeleton.model_dump(mode="json")},
                    known_task_refs={item.task_ref for item in plan.definition.tasks},
                )
            except ValueError as error:
                raise EngineServiceError(str(error)) from error
            all_reviews += (evaluation.adjudication.submission,)
        for submission in all_reviews:
            try:
                validate_reviewer_submission_evidence(
                    submission,
                    evidence_catalog=plan_review_evidence_catalog(
                        plan, goal_contract, state_snapshot, project_map
                    ),
                    known_task_refs={item.task_ref for item in plan.definition.tasks},
                )
            except ValueError as error:
                raise EngineServiceError(str(error)) from error
        findings = actual_findings + tuple(
            finding
            for submission in evaluation.effective_semantic_submissions
            for finding in submission.findings
        )
        ratings = (
            evaluation.effective_semantic_submissions[0].ratings
            if evaluation.effective_semantic_submissions
            else None
        )
        expected_decision = derive_candidate_decision(
            candidate_digest=plan.activation_digest,
            findings=findings,
            ratings=ratings if not findings else None,
        )
        if evaluation.decision != expected_decision:
            raise EngineServiceError("Plan decision이 Core 재계산 결과와 다릅니다.")
        self._register_plan(
            plan,
            decision=evaluation.decision,
            reviews=all_reviews,
            adjudication=evaluation.adjudication,
            original_decision=evaluation.original_evaluation().decision if evaluation.adjudication else None,
        )

    def _register_plan(
        self,
        plan: PlanContractRevision,
        *,
        decision: CandidateDecision,
        reviews: Iterable[Any] = (),
        adjudication: Any = None,
        original_decision: CandidateDecision | None = None,
    ) -> None:
        definition = plan.definition
        if decision.candidate_digest != plan.activation_digest:
            raise EngineServiceError("Plan decision이 activation digest에 결속되지 않았습니다.")
        with self.ledger.transaction() as tx:
            project = tx.one("SELECT * FROM projects WHERE id = ?", (definition.project_id,))
            if project["active_goal_revision_id"] is None:
                raise EngineServiceError("active GoalContract가 없습니다.")
            goal = tx.one(
                "SELECT definition_digest FROM goal_revisions WHERE id = ?",
                (project["active_goal_revision_id"],),
            )
            if goal["definition_digest"] != definition.goal_contract_digest:
                raise EngineServiceError("PlanContract가 active GoalContract에 결속되지 않았습니다.")
            tx.one(
                "SELECT id FROM state_snapshots WHERE snapshot_digest = ? AND project_id = ?",
                (definition.base_state_snapshot_digest, definition.project_id),
            )
            tx.one(
                "SELECT id FROM project_map_revisions WHERE revision_digest = ? AND project_id = ?",
                (definition.project_map_digest, definition.project_id),
            )
            expected_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM plan_revisions WHERE plan_id = ?",
                    (plan.plan_id,),
                ).fetchone()[0]
            )
            if plan.revision_no != expected_no:
                raise EngineServiceError(f"PlanContract revision_no는 {expected_no}여야 합니다.")
            latest = tx.maybe_one(
                "SELECT id FROM plan_revisions WHERE plan_id = ? "
                "ORDER BY revision_no DESC LIMIT 1",
                (plan.plan_id,),
            )
            if latest is None and plan.supersedes_plan_revision_id is not None:
                raise EngineServiceError("첫 PlanContract revision은 supersedes 대상을 가질 수 없습니다.")
            if latest is not None and plan.supersedes_plan_revision_id != latest["id"]:
                raise EngineServiceError("새 PlanContract는 같은 Plan의 최신 revision을 supersede해야 합니다.")
            status = "ready" if decision.status is CandidateStatus.ADMISSIBLE else "draft"
            tx.connection.execute(
                "INSERT INTO plan_revisions "
                "(id, plan_id, project_id, revision_no, definition_digest, activation_digest, "
                "payload_json, status, supersedes_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan.plan_revision_id,
                    plan.plan_id,
                    definition.project_id,
                    plan.revision_no,
                    plan.definition_digest,
                    plan.activation_digest,
                    canonical_json(plan),
                    status,
                    plan.supersedes_plan_revision_id,
                    plan.created_at.isoformat(),
                ),
            )
            now = tx.now
            for position, task in enumerate(definition.tasks):
                tx.connection.execute(
                    "INSERT INTO task_contracts "
                    "(id, project_id, plan_revision_id, task_ref, position, contract_digest, "
                    "payload_json, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        task.task_id,
                        definition.project_id,
                        plan.plan_revision_id,
                        task.task_ref,
                        position,
                        task.contract_digest,
                        canonical_json(task),
                        now,
                        now,
                    ),
                )
            for dependency in definition.dependencies:
                tx.connection.execute(
                    "INSERT INTO task_dependencies "
                    "(plan_revision_id, producer_task_id, consumer_task_id, dependency_type, products_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        plan.plan_revision_id,
                        dependency.producer_task_id,
                        dependency.consumer_task_id,
                        dependency.dependency_type.value,
                        canonical_json(dependency.products),
                    ),
                )
            for review in reviews:
                self._insert_review(tx, definition.project_id, "plan", review)
            self._insert_decision(tx, definition.project_id, "plan", decision)
            if adjudication is not None:
                tx.history(
                    definition.project_id, "plan.review_adjudicated", "plan_revision", plan.plan_revision_id,
                    {"original_decision": original_decision.model_dump(mode="json"),
                     "adjudication": adjudication.model_dump(mode="json"),
                     "decision": decision.model_dump(mode="json")},
                )
            tx.history(
                definition.project_id,
                "plan.registered",
                "plan_revision",
                plan.plan_revision_id,
                {"activation_digest": plan.activation_digest, "decision": decision.status.value},
            )

    def schedule_runtime_job(
        self,
        *,
        project_id: str,
        kind: RuntimeJobKind,
        checkpoint_key: str,
        request: dict[str, Any],
        absolute_deadline_at: datetime,
        attempt_id: str | None = None,
        task_id: str | None = None,
    ) -> RuntimeJob:
        """활성 Plan의 역할 작업을 멱등 예약한다. Task/Goal 상태는 바꾸지 않는다."""

        if absolute_deadline_at.tzinfo is None or absolute_deadline_at.utcoffset() is None:
            raise EngineServiceError("runtime job deadline에는 timezone이 필요합니다.")
        request_digest = sha256_digest(request)
        with self.ledger.transaction() as tx:
            project = tx.maybe_one("SELECT active_plan_revision_id FROM projects WHERE id = ?", (project_id,))
            if project is None or project["active_plan_revision_id"] is None:
                raise EngineServiceError("활성 PlanContract 없이 runtime job을 예약할 수 없습니다.")
            existing = tx.maybe_one(
                "SELECT * FROM runtime_jobs WHERE project_id = ? AND checkpoint_key = ?",
                (project_id, checkpoint_key),
            )
            if existing is not None:
                if existing["kind"] != kind.value or existing["request_digest"] != request_digest:
                    raise EngineServiceError("runtime job checkpoint 입력이 기존 예약과 다릅니다.")
                return self._runtime_job_from_row(existing)
            job_id = new_id("runtime_job")
            tx.connection.execute(
                "INSERT INTO runtime_jobs "
                "(id,project_id,kind,status,checkpoint_key,request_digest,request_json,attempt_id,task_id,"
                "absolute_deadline_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, project_id, kind.value, RuntimeJobStatus.SCHEDULED.value,
                    checkpoint_key, request_digest, canonical_json(request), attempt_id, task_id,
                    absolute_deadline_at.isoformat(), tx.now, tx.now,
                ),
            )
            payload = {"kind": kind.value, "checkpoint_key": checkpoint_key, "request_digest": request_digest}
            self._append_runtime_job_observation(
                tx, job_id=job_id, project_id=project_id,
                kind=RuntimeJobObservationKind.SCHEDULED, payload=payload,
            )
            tx.history(project_id, "runtime_job.scheduled", "runtime_job", job_id, payload)
            return self._runtime_job_from_row(
                tx.one("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,))
            )

    @staticmethod
    def _runtime_job_from_row(row: Any) -> RuntimeJob:
        return RuntimeJob(
            job_id=row["id"], project_id=row["project_id"], kind=row["kind"], status=row["status"],
            checkpoint_key=row["checkpoint_key"], request_digest=row["request_digest"],
            request=json.loads(row["request_json"]), attempt_id=row["attempt_id"], task_id=row["task_id"],
            thread_id=row["thread_id"], turn_id=row["turn_id"],
            absolute_deadline_at=_dt(row["absolute_deadline_at"]),
            provider_terminal_status=row["provider_terminal_status"], result_digest=row["result_digest"],
            created_at=_dt(row["created_at"]), started_at=_dt(row["started_at"]),
            ended_at=_dt(row["ended_at"]), updated_at=_dt(row["updated_at"]),
        )

    def load_runtime_job(self, job_id: str) -> RuntimeJob:
        with self.ledger.read() as connection:
            row = connection.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise EngineServiceError("runtime job을 찾을 수 없습니다.")
        return self._runtime_job_from_row(row)

    def active_runtime_job(self, project_id: str) -> RuntimeJob | None:
        with self.ledger.read() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE project_id=? AND status IN "
                "('scheduled','running','interrupting','collector_lost') ORDER BY created_at,rowid LIMIT 1",
                (project_id,),
            ).fetchone()
        return None if row is None else self._runtime_job_from_row(row)

    def _append_runtime_job_observation(
        self,
        tx: Any,
        *,
        job_id: str,
        project_id: str,
        kind: RuntimeJobObservationKind,
        payload: dict[str, Any],
        provider_terminal: bool = False,
        terminal_status: str | None = None,
    ) -> RuntimeJobObservation:
        digest = sha256_digest(payload)
        existing = tx.maybe_one(
            "SELECT * FROM runtime_job_observations WHERE job_id=? AND payload_digest=?",
            (job_id, digest),
        )
        if existing is not None:
            return RuntimeJobObservation(
                observation_id=existing["id"], job_id=job_id, project_id=project_id,
                kind=existing["kind"], provider_terminal=bool(existing["provider_terminal"]),
                terminal_status=existing["terminal_status"], payload=json.loads(existing["payload_json"]),
                payload_digest=existing["payload_digest"], observed_at=_dt(existing["observed_at"]),
            )
        observation = RuntimeJobObservation(
            observation_id=new_id("job_observation"), job_id=job_id, project_id=project_id,
            kind=kind, provider_terminal=provider_terminal, terminal_status=terminal_status,
            payload=payload, payload_digest=digest, observed_at=_dt(tx.now),
        )
        tx.connection.execute(
            "INSERT INTO runtime_job_observations "
            "(id,job_id,project_id,kind,provider_terminal,terminal_status,payload_digest,payload_json,observed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                observation.observation_id, job_id, project_id, kind.value,
                int(provider_terminal), terminal_status, digest, canonical_json(payload), tx.now,
            ),
        )
        return observation

    def start_runtime_job(
        self, job_id: str, *, thread_id: str | None = None, turn_id: str | None = None,
    ) -> RuntimeJob:
        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,))
            if row["status"] not in {RuntimeJobStatus.SCHEDULED.value, RuntimeJobStatus.COLLECTOR_LOST.value}:
                return self._runtime_job_from_row(row)
            event = (
                RuntimeJobObservationKind.COLLECTOR_REATTACHED
                if row["status"] == RuntimeJobStatus.COLLECTOR_LOST.value
                else RuntimeJobObservationKind.STARTED
            )
            tx.connection.execute(
                "UPDATE runtime_jobs SET status='running',thread_id=COALESCE(?,thread_id),"
                "turn_id=COALESCE(?,turn_id),started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                (thread_id, turn_id, tx.now, tx.now, job_id),
            )
            payload = {
                "collector_event": event.value,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "absolute_deadline_at": row["absolute_deadline_at"],
            }
            self._append_runtime_job_observation(
                tx, job_id=job_id, project_id=row["project_id"], kind=event, payload=payload,
            )
            tx.history(row["project_id"], f"runtime_job.{event.value}", "runtime_job", job_id, payload)
            return self._runtime_job_from_row(tx.one("SELECT * FROM runtime_jobs WHERE id=?", (job_id,)))

    def bind_runtime_job_provider(
        self,
        job_id: str,
        *,
        thread_id: str,
        turn_id: str,
    ) -> RuntimeJob:
        """실행 중 job에 exact provider turn을 최초 한 번 결속한다.

        역할 runner의 provider thread/turn 생성은 job worker가 활성 상태가 된 뒤
        발생한다. deadline/collector 경합으로 상태가 먼저 바뀌어도 late start receipt의
        exact binding은 보존한다. 이 경계는 provider terminal을 판정하지 않고,
        재시작 시 같은 turn을 먼저 관측할 수 있는 binding만 영속화한다.
        """

        if (
            not isinstance(thread_id, str)
            or not thread_id.strip()
            or len(thread_id) > 300
            or not isinstance(turn_id, str)
            or not turn_id.strip()
            or len(turn_id) > 300
        ):
            raise EngineServiceError("runtime job provider binding에는 유효한 thread/turn ID가 필요합니다.")
        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,))
            existing = (row["thread_id"], row["turn_id"])
            candidate = (thread_id, turn_id)
            if existing == candidate:
                return self._runtime_job_from_row(row)
            if existing != (None, None):
                raise EngineServiceError(
                    "runtime job provider binding이 기존 exact thread/turn과 다릅니다."
                )
            if row["status"] not in {
                RuntimeJobStatus.RUNNING.value,
                RuntimeJobStatus.INTERRUPTING.value,
                RuntimeJobStatus.COLLECTOR_LOST.value,
            }:
                raise EngineServiceError(
                    "활성 runtime job에만 provider binding을 추가할 수 있습니다."
                )
            tx.connection.execute(
                "UPDATE runtime_jobs SET thread_id=?,turn_id=?,updated_at=? "
                "WHERE id=? AND status IN ('running','interrupting','collector_lost') "
                "AND thread_id IS NULL AND turn_id IS NULL",
                (thread_id, turn_id, tx.now, job_id),
            )
            payload = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "binding_source": "role_progress",
            }
            observation = self._append_runtime_job_observation(
                tx,
                job_id=job_id,
                project_id=row["project_id"],
                kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
                payload=payload,
            )
            tx.history(
                row["project_id"],
                "runtime_job.provider_progress",
                "runtime_job",
                job_id,
                {
                    "observation_id": observation.observation_id,
                    "payload_digest": observation.payload_digest,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                },
            )
            return self._runtime_job_from_row(
                tx.one("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,))
            )

    def record_runtime_job_observation(
        self,
        job_id: str,
        *,
        kind: RuntimeJobObservationKind,
        payload: dict[str, Any],
        provider_terminal: bool = False,
        terminal_status: str | None = None,
    ) -> RuntimeJobObservation:
        """관측을 저장하되 Attempt·Task·Goal의 완료는 판정하지 않는다."""

        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,))
            if provider_terminal and row["status"] in {
                RuntimeJobStatus.PROVIDER_TERMINAL.value,
                RuntimeJobStatus.CONSUMED.value,
            }:
                existing = tx.one(
                    "SELECT * FROM runtime_job_observations WHERE job_id=? AND provider_terminal=1 "
                    "ORDER BY rowid LIMIT 1", (job_id,),
                )
                candidate_result = payload.get("result")
                candidate_digest = None if candidate_result is None else sha256_digest(candidate_result)
                if (
                    existing["terminal_status"] != terminal_status
                    or row["result_digest"] != candidate_digest
                ):
                    raise EngineServiceError("runtime job provider terminal 관측이 기존 결과와 다릅니다.")
                return RuntimeJobObservation(
                    observation_id=existing["id"], job_id=job_id, project_id=row["project_id"],
                    kind=existing["kind"], provider_terminal=True,
                    terminal_status=existing["terminal_status"],
                    payload=json.loads(existing["payload_json"]),
                    payload_digest=existing["payload_digest"], observed_at=_dt(existing["observed_at"]),
                )
            payload_digest = sha256_digest(payload)
            duplicate = tx.maybe_one(
                "SELECT * FROM runtime_job_observations WHERE job_id=? AND payload_digest=?",
                (job_id, payload_digest),
            )
            if duplicate is not None:
                if kind is RuntimeJobObservationKind.COLLECTOR_LOST:
                    # reattach가 상태를 running으로 되돌린 뒤 동일한 수집 실패가
                    # 재발해도 durable 상태 전이는 다시 collector_lost여야 한다.
                    tx.connection.execute(
                        "UPDATE runtime_jobs SET status='collector_lost',updated_at=? WHERE id=? "
                        "AND status NOT IN ('provider_terminal','consumed','cancelled')",
                        (tx.now, job_id),
                    )
                return RuntimeJobObservation(
                    observation_id=duplicate["id"],
                    job_id=job_id,
                    project_id=row["project_id"],
                    kind=duplicate["kind"],
                    provider_terminal=bool(duplicate["provider_terminal"]),
                    terminal_status=duplicate["terminal_status"],
                    payload=json.loads(duplicate["payload_json"]),
                    payload_digest=duplicate["payload_digest"],
                    observed_at=_dt(duplicate["observed_at"]),
                )
            observation = self._append_runtime_job_observation(
                tx, job_id=job_id, project_id=row["project_id"], kind=kind, payload=payload,
                provider_terminal=provider_terminal, terminal_status=terminal_status,
            )
            if provider_terminal:
                result = payload.get("result")
                result_json = canonical_json(result) if result is not None else None
                result_digest = None if result is None else sha256_digest(result)
                tx.connection.execute(
                    "UPDATE runtime_jobs SET status='provider_terminal',provider_terminal_status=?,"
                    "result_digest=?,result_json=?,ended_at=COALESCE(ended_at,?),updated_at=? WHERE id=?",
                    (terminal_status, result_digest, result_json, tx.now, tx.now, job_id),
                )
            elif kind is RuntimeJobObservationKind.COLLECTOR_LOST:
                tx.connection.execute(
                    "UPDATE runtime_jobs SET status='collector_lost',updated_at=? WHERE id=? "
                    "AND status NOT IN ('provider_terminal','consumed','cancelled')", (tx.now, job_id),
                )
            tx.history(
                row["project_id"], f"runtime_job.{kind.value}", "runtime_job", job_id,
                {"observation_id": observation.observation_id, "provider_terminal": provider_terminal,
                 "terminal_status": terminal_status, "payload_digest": observation.payload_digest},
            )
            return observation

    def begin_runtime_job_interrupt(self, job_id: str, *, payload: dict[str, Any]) -> bool:
        """절대 deadline interrupt를 정확히 한 번 예약한다."""
        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE id=?", (job_id,))
            if row["status"] == RuntimeJobStatus.INTERRUPTING.value:
                return False
            if row["status"] not in {
                RuntimeJobStatus.RUNNING.value,
                RuntimeJobStatus.COLLECTOR_LOST.value,
            }:
                return False
            tx.connection.execute(
                "UPDATE runtime_jobs SET status='interrupting',updated_at=? WHERE id=?",
                (tx.now, job_id),
            )
            observation = self._append_runtime_job_observation(
                tx, job_id=job_id, project_id=row["project_id"],
                kind=RuntimeJobObservationKind.INTERRUPT_REQUESTED, payload=payload,
            )
            tx.history(
                row["project_id"], "runtime_job.interrupt_requested", "runtime_job", job_id,
                {"observation_id": observation.observation_id,
                 "payload_digest": observation.payload_digest},
            )
            return True

    def consume_runtime_job(self, job_id: str) -> dict[str, Any] | None:
        """Core 호출자가 terminal 역할 결과 하나를 소비한다. 상태 전이는 별도 Core 로직이 한다."""

        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,))
            if row["status"] == RuntimeJobStatus.CONSUMED.value:
                return None if row["result_json"] is None else json.loads(row["result_json"])
            if row["status"] != RuntimeJobStatus.PROVIDER_TERMINAL.value:
                raise EngineServiceError("provider terminal을 관측하지 않은 runtime job은 소비할 수 없습니다.")
            tx.connection.execute(
                "UPDATE runtime_jobs SET status='consumed',updated_at=? WHERE id=?", (tx.now, job_id),
            )
            payload = {"result_digest": row["result_digest"]}
            self._append_runtime_job_observation(
                tx, job_id=job_id, project_id=row["project_id"],
                kind=RuntimeJobObservationKind.CONSUMED, payload=payload,
            )
            tx.history(row["project_id"], "runtime_job.consumed", "runtime_job", job_id, payload)
            return None if row["result_json"] is None else json.loads(row["result_json"])

    def consume_runtime_job_required_result(self, job_id: str) -> dict[str, Any] | None:
        """재시작 뒤 provider terminal만 복원된 typed job 결과는 소비하지 않는다."""

        result = self.consume_runtime_job(job_id)
        if isinstance(result, dict) and (
            result.get("runtime_job_result_unavailable") is True
            or "job_error" in result
        ):
            from .operations import ExternalOperationUnknown

            raise ExternalOperationUnknown(
                "provider terminal은 관측했지만 local typed 결과를 확정할 수 없습니다."
            )
        return result

    def cancel_runtime_job(self, job_id: str, *, reason: str) -> RuntimeJob:
        """job을 취소하되 interrupt receipt를 provider terminal로 승격하지 않는다."""

        if not reason.strip():
            raise EngineServiceError("runtime job 취소 이유가 필요합니다.")
        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,))
            if row["status"] in {
                RuntimeJobStatus.PROVIDER_TERMINAL.value,
                RuntimeJobStatus.CONSUMED.value,
                RuntimeJobStatus.CANCELLED.value,
            }:
                return self._runtime_job_from_row(row)
            payload = {"reason": reason, "prior_status": row["status"]}
            tx.connection.execute(
                "UPDATE runtime_jobs SET status='cancelled',ended_at=?,updated_at=? WHERE id=?",
                (tx.now, tx.now, job_id),
            )
            self._append_runtime_job_observation(
                tx,
                job_id=job_id,
                project_id=row["project_id"],
                kind=RuntimeJobObservationKind.CANCELLED,
                payload=payload,
            )
            tx.history(
                row["project_id"],
                "runtime_job.cancelled",
                "runtime_job",
                job_id,
                payload,
            )
            return self._runtime_job_from_row(
                tx.one("SELECT * FROM runtime_jobs WHERE id=?", (job_id,))
            )

    def workflow_control_state(self, project_id: str) -> str:
        """현재 active Goal revision에 결속된 facade 제어 상태를 읽는다."""

        with self.ledger.read() as connection:
            project = connection.execute(
                "SELECT active_goal_revision_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise EngineServiceError("PROJECT_NOT_FOUND")
            goal_revision_id = project["active_goal_revision_id"]
            if goal_revision_id is None:
                return "running"
            rows = connection.execute(
                "SELECT event_type,payload_json FROM history_events "
                "WHERE project_id=? AND event_type IN "
                "('workflow.paused','workflow.resumed','workflow.cancelled') "
                "ORDER BY sequence DESC",
                (project_id,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("goal_revision_id") != goal_revision_id:
                continue
            return {
                "workflow.paused": "paused",
                "workflow.resumed": "running",
                "workflow.cancelled": "cancelled",
            }[row["event_type"]]
        return "running"

    def set_workflow_control(
        self, project_id: str, *, state: str, reason: str,
    ) -> str:
        """현재 Goal의 사용자 pause/resume/cancel 의도를 멱등 기록한다."""

        if state not in {"paused", "running", "cancelled"}:
            raise EngineServiceError("workflow control 상태가 유효하지 않습니다.")
        if not reason.strip():
            raise EngineServiceError("workflow control 이유가 필요합니다.")
        current = self.workflow_control_state(project_id)
        if current == "cancelled" and state != "cancelled":
            raise EngineServiceError("WORKFLOW_CANCELLED: 취소한 Goal은 재개할 수 없습니다.")
        if current == state:
            return current
        with self.ledger.transaction() as tx:
            project = tx.one(
                "SELECT active_goal_revision_id FROM projects WHERE id=?", (project_id,)
            )
            if project["active_goal_revision_id"] is None:
                raise EngineServiceError("active GoalContract가 없습니다.")
            event_type = {
                "paused": "workflow.paused",
                "running": "workflow.resumed",
                "cancelled": "workflow.cancelled",
            }[state]
            tx.history(
                project_id,
                event_type,
                "project",
                project_id,
                {
                    "goal_revision_id": project["active_goal_revision_id"],
                    "reason": reason,
                },
            )
        return state

    @staticmethod
    def _insert_review(tx: Any, project_id: str, artifact_kind: str, review: Any) -> None:
        tx.connection.execute(
            "INSERT INTO candidate_reviews "
            "(id, project_id, artifact_kind, artifact_digest, reviewer_role, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("review"),
                project_id,
                artifact_kind,
                review.candidate_digest,
                review.reviewer_role,
                canonical_json(review),
                tx.now,
            ),
        )

    @staticmethod
    def _insert_decision(
        tx: Any,
        project_id: str,
        artifact_kind: str,
        decision: CandidateDecision,
    ) -> None:
        tx.connection.execute(
            "INSERT INTO candidate_decisions "
            "(id, project_id, artifact_kind, artifact_digest, status, fitness_score, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("decision"),
                project_id,
                artifact_kind,
                decision.candidate_digest,
                decision.status.value,
                decision.fitness_score,
                canonical_json(decision),
                tx.now,
            ),
        )

    def authorize_goal(self, *, project_id: str, source: str,
                       operating_policy: GoalOperatingPolicy | None = None) -> GoalAuthorization:
        """호출자는 실제 사용자 승인을 받은 명령 경계여야 한다. 후보 제출 권한과 분리한다."""
        from .authorization import current_budget_policies

        with self.ledger.transaction() as tx:
            project = tx.one("SELECT * FROM projects WHERE id = ?", (project_id,))
            goal = GoalContractRevision.model_validate_json(tx.one(
                "SELECT payload_json FROM goal_revisions WHERE id = ? AND status = 'active'",
                (project["active_goal_revision_id"],),
            )["payload_json"])
            profile = tx.one("SELECT definition_digest FROM profile_revisions WHERE id = ?",
                             (project["active_profile_revision_id"],))
            if goal.definition.profile_definition_digest != profile["definition_digest"]:
                raise EngineServiceError("Goal의 Profile 결속이 최신 운영 정책과 다릅니다.")
            previous = tx.maybe_one(
                "SELECT * FROM goal_authorizations WHERE project_id = ? ORDER BY revision_no DESC LIMIT 1",
                (project_id,),
            )
            authorization = GoalAuthorization(
                authorization_id=new_id("goal_authorization"),
                revision_no=1 if previous is None else previous["revision_no"] + 1,
                supersedes_authorization_id=None if previous is None else previous["id"],
                project_id=project_id, project_root=str(Path(project["root"]).resolve()),
                goal_id=goal.goal_id, goal_revision_id=goal.goal_revision_id,
                goal_contract_digest=goal.definition_digest,
                profile_definition_digest=profile["definition_digest"], effect_policy=goal.definition.effect_policy,
                operating_policy=operating_policy or GoalOperatingPolicy(),
                budget_policies=current_budget_policies(tx.connection, project_id),
                source=source, approved_at=_dt(tx.now),
            )
            authorization = GoalAuthorization.model_validate_json(authorization.model_dump_json())
            tx.connection.execute(
                "INSERT INTO goal_authorizations (id,project_id,revision_no,authorization_digest,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?)", (authorization.authorization_id, project_id, authorization.revision_no,
                authorization.authorization_digest, canonical_json(authorization), tx.now),
            )
            tx.history(project_id, "goal.authorized", "goal_authorization", authorization.authorization_id,
                       {"authorization_digest": authorization.authorization_digest, "source": source})
            return authorization

    def activate_authorized_plan(self, *, plan_revision_id: str) -> str:
        """내부 후보 식별자를 사용하며 사용자에게 Plan ID/digest 입력을 요구하지 않는다."""
        with self.ledger.read() as connection:
            row = connection.execute("SELECT activation_digest FROM plan_revisions WHERE id = ?",
                                     (plan_revision_id,)).fetchone()
        if row is None:
            raise EngineServiceError("PlanContract를 찾을 수 없습니다.")
        return self.activate_plan(plan_revision_id=plan_revision_id, activation_digest=row["activation_digest"],
                                  source="goal_authorization")

    def register_authorized_plan_revision(self, evaluation: ExpandedPlanEvaluation) -> str:
        """승인 내 분할·repair 후보를 review/Gate 검증 후 자동 활성화한다."""
        self.register_plan_evaluation(evaluation)
        return self.activate_authorized_plan(plan_revision_id=evaluation.plan.plan_revision_id)

    def activate_selected_plan(self, *, project_id: str) -> str:
        """Core가 기록한 선택 후보를 활성화한다. 선택 이력이 없으면 단일 ready 후보만 허용한다."""
        with self.ledger.read() as connection:
            selection = connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id = ? "
                "AND event_type = 'planning.search_recorded' ORDER BY sequence DESC LIMIT 1", (project_id,),
            ).fetchone()
            selected_digest = None if selection is None else json.loads(selection["payload_json"])["outcome"].get("selected_activation_digest")
            selected_row = None if selected_digest is None else connection.execute(
                "SELECT id FROM plan_revisions WHERE project_id = ? AND activation_digest = ?",
                (project_id, selected_digest),
            ).fetchone()
            selected = None if selected_row is None else selected_row["id"]
            if selected is None:
                candidates = connection.execute("SELECT id FROM plan_revisions WHERE project_id = ? AND status = 'ready'",
                                                (project_id,)).fetchall()
                if len(candidates) != 1:
                    raise EngineServiceError("PLAN_SELECTION_REQUIRED: Core의 단일 선택 후보가 필요합니다.")
                selected = candidates[0]["id"]
        return self.activate_authorized_plan(plan_revision_id=selected)

    def _plan_authorization(self, tx: Any, project: Any, payload: PlanContractRevision) -> GoalAuthorization:
        from .authorization import authorization_changes, current_budget_policies

        row = tx.maybe_one("SELECT * FROM goal_authorizations WHERE project_id = ? ORDER BY revision_no DESC LIMIT 1",
                           (project["id"],))
        if row is None:
            raise GoalAuthorizationRequired(({"boundary": "goal", "field": "authorization", "approved": None,
                                              "requested": project["active_goal_revision_id"],
                                              "evidence_ref": payload.activation_digest},))
        authorization = GoalAuthorization.model_validate_json(row["payload_json"])
        if authorization.authorization_digest != row["authorization_digest"]:
            raise EngineServiceError("GoalAuthorization digest 결속이 다릅니다.")
        goal = GoalContractRevision.model_validate_json(tx.one("SELECT payload_json FROM goal_revisions WHERE id = ?",
                                                              (project["active_goal_revision_id"],))["payload_json"])
        profile = tx.one("SELECT definition_digest FROM profile_revisions WHERE id = ?", (project["active_profile_revision_id"],))
        changes = authorization_changes(authorization, project=project, goal=goal,
                                        profile_digest=profile["definition_digest"], plan=payload,
                                        budget_policies=current_budget_policies(tx.connection, project["id"]))
        project_map = ProjectMapRevision.model_validate_json(tx.one(
            "SELECT payload_json FROM project_map_revisions WHERE revision_digest = ?",
            (payload.definition.project_map_digest,),)["payload_json"])
        if Path(project_map.root).resolve() != Path(authorization.project_root).resolve():
            changes += ({"boundary": "project", "field": "project_map.root", "approved": authorization.project_root,
                         "requested": project_map.root, "evidence_ref": project_map.revision_digest},)
        if changes:
            raise GoalAuthorizationRequired(changes)
        if payload.definition.goal_contract_digest != goal.definition_digest:
            raise EngineServiceError("active GoalContract가 Plan의 승인 기준과 달라졌습니다.")
        return authorization

    @staticmethod
    def _assert_plan_replacement_quiescent(tx: Any, project_id: str) -> None:
        active = tx.all("SELECT id, status FROM attempts WHERE project_id = ? "
                        "AND (status IN ('reserved','starting','running','unknown') OR failure_class = 'external_unknown')",
                        (project_id,))
        intents = tx.all("SELECT i.id FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                         "WHERE a.project_id = ? AND i.status IN ('prepared','unknown')", (project_id,))
        validating = tx.all("SELECT id FROM task_contracts WHERE project_id = ? AND status = 'validating'", (project_id,))
        calls = tx.all("SELECT id FROM provider_calls WHERE project_id = ? "
                       "AND (execution_status IN ('reserved','started','unknown') OR effect_status IN ('pending','unknown'))",
                       (project_id,))
        # 준비 역할은 Attempt/provider call 생성 전에도 예약될 수 있다.
        # terminal 결과도 Core가 소비하기 전에는 이전 Plan에 결속된 입력이다.
        # cancelled는 provider terminal의 증거가 아니다. 시작하지 않은 취소만 제외한다.
        jobs = tx.all("SELECT id, status FROM runtime_jobs WHERE project_id = ? "
                      "AND (status NOT IN ('consumed','cancelled') OR "
                      "(status = 'cancelled' AND (started_at IS NOT NULL "
                      "OR thread_id IS NOT NULL OR turn_id IS NOT NULL)))", (project_id,))
        operations = tx.all("SELECT h.entity_id FROM history_events h WHERE h.project_id = ? "
                            "AND h.event_type = 'operation.prepared' AND NOT EXISTS (SELECT 1 FROM history_events c "
                            "WHERE c.project_id = h.project_id AND c.entity_id = h.entity_id AND c.sequence > h.sequence "
                            "AND c.event_type IN ('operation.completed','operation.failed','operation.no_effect'))",
                            (project_id,))
        if active or intents or validating or calls or jobs or operations:
            raise EngineServiceError("PLAN_REPLACEMENT_IN_FLIGHT: 기존 실행·검사·미확정 효과를 먼저 관측해야 합니다: "
                                     + canonical_json({"attempts": [dict(row) for row in active],
                                       "intents": [row["id"] for row in intents],
                                       "validating_tasks": [row["id"] for row in validating],
                                       "provider_calls": [row["id"] for row in calls],
                                       "runtime_jobs": [dict(row) for row in jobs],
                                       "operations": [row["entity_id"] for row in operations]}))

    def _reuse_completed_tasks(self, tx: Any, plan: PlanContractRevision, previous_id: str) -> tuple[str, ...]:
        from .plan_reuse import reuse_completed_tasks
        return reuse_completed_tasks(self, tx, plan, previous_id)

    @staticmethod
    def task_evidence_rows(connection: Any, task_id: str) -> tuple[Any, ...]:
        """재사용은 원래 근거 ID/Attempt를 보존하는 읽기 연결이다."""
        reuse = connection.execute("SELECT evidence_ids_json FROM task_completion_reuse WHERE task_id = ?", (task_id,)).fetchone()
        if reuse is None:
            return tuple(connection.execute("SELECT * FROM evidence_records WHERE task_id = ? ORDER BY id", (task_id,)).fetchall())
        ids = json.loads(reuse["evidence_ids_json"])
        return tuple(connection.execute("SELECT * FROM evidence_records WHERE id = ?", (item,)).fetchone() for item in ids)

    def activate_plan(
        self,
        *,
        plan_revision_id: str,
        activation_digest: str,
        source: str,
    ) -> str:
        with self.ledger.transaction() as tx:
            plan = tx.one("SELECT * FROM plan_revisions WHERE id = ?", (plan_revision_id,))
            if plan["activation_digest"] != activation_digest:
                raise EngineServiceError("지정한 activation digest가 PlanContract와 다릅니다.")
            if plan["status"] != "ready":
                raise EngineServiceError("admissible이며 ready인 PlanContract만 활성화할 수 있습니다.")
            decision = tx.one(
                "SELECT status FROM candidate_decisions WHERE artifact_kind = 'plan' "
                "AND artifact_digest = ?",
                (activation_digest,),
            )
            if decision["status"] != CandidateStatus.ADMISSIBLE.value:
                raise EngineServiceError("Core decision이 admissible인 Plan만 활성화할 수 있습니다.")
            project = tx.one("SELECT * FROM projects WHERE id = ?", (plan["project_id"],))
            payload = PlanContractRevision.model_validate_json(plan["payload_json"])
            if payload.activation_digest != activation_digest:
                raise EngineServiceError("저장된 Plan payload의 activation digest가 다릅니다.")
            authorization = self._plan_authorization(tx, project, payload)
            active_plan_id = project["active_plan_revision_id"]
            if active_plan_id is not None:
                self._assert_plan_replacement_quiescent(tx, project["id"])
                active_plan = tx.one(
                    "SELECT id, plan_id, status FROM plan_revisions WHERE id = ?",
                    (active_plan_id,),
                )
                ancestor = payload.supersedes_plan_revision_id
                while ancestor is not None and ancestor != active_plan_id:
                    prior = tx.one("SELECT plan_id, supersedes_id FROM plan_revisions WHERE id = ?", (ancestor,))
                    if prior["plan_id"] != payload.plan_id:
                        break
                    ancestor = prior["supersedes_id"]
                if ancestor != active_plan_id or payload.plan_id != active_plan["plan_id"]:
                    raise EngineServiceError(
                        "active Plan을 교체하려면 같은 plan_id의 immutable supersedes 계보가 현재 revision으로 이어져야 합니다."
                    )
            goal = tx.one(
                "SELECT definition_digest FROM goal_revisions WHERE id = ? AND status = 'active'",
                (project["active_goal_revision_id"],),
            )
            if goal["definition_digest"] != payload.definition.goal_contract_digest:
                raise EngineServiceError("active GoalContract가 Plan의 승인 기준과 달라졌습니다.")
            state = tx.one(
                "SELECT is_current FROM state_snapshots WHERE snapshot_digest = ?",
                (payload.definition.base_state_snapshot_digest,),
            )
            if state["is_current"] != 1:
                raise EngineServiceError("Plan의 기준 StateSnapshot이 stale입니다. 재검토가 필요합니다.")
            project_map = tx.one(
                "SELECT is_current FROM project_map_revisions WHERE revision_digest = ?",
                (payload.definition.project_map_digest,),
            )
            if project_map["is_current"] != 1:
                raise EngineServiceError("Plan의 Project Map이 최신 revision이 아닙니다.")
            now = tx.now
            reused = self._reuse_completed_tasks(tx, payload, active_plan_id) if active_plan_id else ()
            if active_plan_id is not None:
                tx.connection.execute(
                    "UPDATE plan_revisions SET status = 'superseded' WHERE id = ?",
                    (active_plan_id,),
                )
                tx.connection.execute(
                    "UPDATE task_contracts SET status = 'superseded', updated_at = ? "
                    "WHERE plan_revision_id = ? AND status <> 'completed'",
                    (now, active_plan_id),
                )
            tx.connection.execute(
                "UPDATE plan_revisions SET status = 'active', activated_at = ?, activation_source = ? "
                "WHERE id = ?",
                (now, source, plan_revision_id),
            )
            tx.connection.execute(
                "UPDATE projects SET active_plan_revision_id = ?, run_state = 'active', "
                "recovery_reason = NULL, updated_at = ? WHERE id = ?",
                (plan_revision_id, now, plan["project_id"]),
            )
            activation_id = new_id("activation")
            tx.connection.execute(
                "INSERT INTO plan_activations "
                "(id, project_id, plan_revision_id, activation_digest, authorization_id, source, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (activation_id, plan["project_id"], plan_revision_id, activation_digest,
                 authorization.authorization_id, source, now),
            )
            self._refresh_ready(tx, plan_revision_id)
            tx.history(
                plan["project_id"],
                "plan.activated",
                "plan_revision",
                plan_revision_id,
                {"activation_digest": activation_digest, "source": source,
                 "authorization_id": authorization.authorization_id, "reused_task_ids": reused},
            )
            return activation_id

    def retry_task(
        self,
        *,
        task_id: str,
        new_evidence_ids: tuple[str, ...] = (),
        recovery_assessment: RecoveryAssessment | None = None,
        failed_validation_result_id: str | None = None,
    ) -> None:
        """동일 TaskContract 범위의 재시도만 다시 materialized 상태로 연다."""

        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (task_id,))
            if task["status"] not in {"failed", "blocked"}:
                raise EngineServiceError("failed/blocked Task만 동일 계약으로 재시도할 수 있습니다.")
            contract = json.loads(task["payload_json"])
            recovery = contract["recovery"]
            retry_count = tx.connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE project_id = ? "
                "AND event_type IN ('task.retry_enabled','task.execution_spec_recovery_enabled') "
                "AND entity_id = ?",
                (task["project_id"], task_id),
            ).fetchone()[0]
            if retry_count >= int(recovery["max_same_failure_replans"]):
                raise EngineServiceError(
                    "동일 Task recovery 한도를 넘었습니다: "
                    f"{retry_count}/{recovery['max_same_failure_replans']}"
                )
            attempt = tx.one(
                "SELECT * FROM attempts WHERE task_id = ? AND kind = 'execution' "
                "ORDER BY attempt_no DESC, rowid DESC LIMIT 1",
                (task_id,),
            )
            failure = (
                None
                if attempt["failure_class"] is None
                else FailureClass(attempt["failure_class"])
            )
            if failure is None:
                new_evidence_ids = self._validate_task_validation_recovery(
                    tx,
                    task=task,
                    attempt=attempt,
                    recovery_assessment=recovery_assessment,
                    failed_validation_result_id=failed_validation_result_id,
                    supplied_evidence_ids=new_evidence_ids,
                )
                failure = recovery_assessment.failure_class  # type: ignore[union-attr]
            elif failed_validation_result_id is not None:
                raise EngineServiceError(
                    "Attempt 실패 재시도에는 Task validation result를 함께 사용할 수 없습니다."
                )
            elif recovery_assessment is not None:
                new_evidence_ids = self._validate_attempt_recovery(
                    tx,
                    task=task,
                    attempt=attempt,
                    failure=failure,
                    recovery_assessment=recovery_assessment,
                    supplied_evidence_ids=new_evidence_ids,
                )
            if failure in {
                FailureClass.TASK_CONTRACT,
                FailureClass.DEPENDENCY,
                FailureClass.REQUIREMENT_CHANGE,
                FailureClass.EXTERNAL_UNKNOWN,
                FailureClass.UNCLASSIFIED,
            }:
                raise EngineServiceError(
                    f"{failure.value} 실패는 같은 Task 재시도가 아니라 Plan/Goal revision이 필요합니다."
                )
            unknown = tx.connection.execute(
                "SELECT COUNT(*) FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                "WHERE a.project_id = ? AND i.status = 'unknown'",
                (task["project_id"],),
            ).fetchone()[0]
            if unknown:
                raise EngineServiceError("unknown 외부 효과를 reconcile하기 전에는 재시도할 수 없습니다.")
            if attempt["attempt_no"] > 1 and not new_evidence_ids:
                raise EngineServiceError("같은 실패를 반복 재시도하려면 새 evidence가 필요합니다.")
            if new_evidence_ids:
                placeholders = ",".join("?" for _ in new_evidence_ids)
                count = tx.connection.execute(
                    f"SELECT COUNT(*) FROM evidence_records WHERE project_id = ? AND id IN ({placeholders})",
                    (task["project_id"], *new_evidence_ids),
                ).fetchone()[0]
                if count != len(new_evidence_ids):
                    raise EngineServiceError("재시도 evidence 일부가 원장에 없습니다.")
            if failure.value not in set(recovery["retryable_failure_classes"]):
                raise EngineServiceError(
                    f"TaskContract가 {failure.value} 실패의 동일 Task 재시도를 허용하지 않습니다."
                )
            current_spec = tx.one(
                "SELECT definition_digest FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                (task_id,),
            )
            if failure is FailureClass.CONTEXT and current_spec["definition_digest"] == attempt["execution_spec_digest"]:
                raise EngineServiceError("context 실패는 새 ExecutionSpec revision을 먼저 만들어야 합니다.")
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'materialized', updated_at = ? WHERE id = ?",
                (tx.now, task_id),
            )
            tx.history(
                task["project_id"],
                "task.retry_enabled",
                "task_contract",
                task_id,
                {
                    "previous_attempt_id": attempt["id"],
                    "new_evidence_ids": new_evidence_ids,
                    "failure_class": failure.value,
                    "recovery_assessment_id": (
                        None if recovery_assessment is None else recovery_assessment.assessment_id
                    ),
                    "failed_validation_result_id": failed_validation_result_id,
                },
            )

    def enable_execution_spec_recovery(
        self,
        *,
        task_id: str,
        recovery_assessment: RecoveryAssessment,
    ) -> None:
        """context 실패를 같은 Task 의미의 새 ExecutionSpec 준비로 되돌린다."""

        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id=?", (task_id,))
            if task["status"] not in {"failed", "blocked"}:
                raise EngineServiceError("failed/blocked Task만 ExecutionSpec 복구할 수 있습니다.")
            attempt = tx.one(
                "SELECT * FROM attempts WHERE task_id=? AND kind='execution' "
                "ORDER BY attempt_no DESC,rowid DESC LIMIT 1",
                (task_id,),
            )
            if attempt["failure_class"] != FailureClass.CONTEXT.value:
                raise EngineServiceError("context 실패만 ExecutionSpec revision으로 복구할 수 있습니다.")
            contract = json.loads(task["payload_json"])
            if FailureClass.CONTEXT.value not in set(
                contract["recovery"]["retryable_failure_classes"]
            ):
                raise EngineServiceError("TaskContract가 context 복구를 허용하지 않습니다.")
            prior = tx.connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE project_id=? AND entity_id=? "
                "AND event_type IN ('task.retry_enabled','task.execution_spec_recovery_enabled')",
                (task["project_id"], task_id),
            ).fetchone()[0]
            if prior >= int(contract["recovery"]["max_same_failure_replans"]):
                raise EngineServiceError("동일 Task recovery 한도를 넘었습니다.")
            self._validate_attempt_recovery(
                tx,
                task=task,
                attempt=attempt,
                failure=FailureClass.CONTEXT,
                recovery_assessment=recovery_assessment,
                supplied_evidence_ids=(),
            )
            current = tx.one(
                "SELECT definition_digest FROM execution_spec_revisions "
                "WHERE task_id=? AND is_current=1",
                (task_id,),
            )
            if current["definition_digest"] != attempt["execution_spec_digest"]:
                raise EngineServiceError("이미 새 ExecutionSpec revision이 존재합니다.")
            tx.connection.execute(
                "UPDATE task_contracts SET status='ready',updated_at=? WHERE id=?",
                (tx.now, task_id),
            )
            tx.history(
                task["project_id"],
                "task.execution_spec_recovery_enabled",
                "task_contract",
                task_id,
                {
                    "previous_attempt_id": attempt["id"],
                    "recovery_assessment_id": recovery_assessment.assessment_id,
                    "new_evidence_ids": recovery_assessment.new_evidence_ids,
                },
            )

    def _validate_attempt_recovery(
        self,
        tx: Any,
        *,
        task: Any,
        attempt: Any,
        failure: FailureClass,
        recovery_assessment: RecoveryAssessment,
        supplied_evidence_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """자동 controller가 만든 assessment를 실패 Attempt에 직접 결속한다."""

        if recovery_assessment.attempt_id != attempt["id"]:
            raise EngineServiceError("RecoveryAssessment가 현재 실패 Attempt와 다릅니다.")
        if recovery_assessment.failure_class is not failure:
            raise EngineServiceError("RecoveryAssessment failure_class가 Attempt와 다릅니다.")
        expected_action = self.repair_action_for(failure)
        if recovery_assessment.action is not expected_action:
            raise EngineServiceError("RecoveryAssessment action이 Core 분류와 다릅니다.")
        evidence_ids = recovery_assessment.new_evidence_ids
        if supplied_evidence_ids and supplied_evidence_ids != evidence_ids:
            raise EngineServiceError("retry evidence와 RecoveryAssessment evidence가 다릅니다.")
        if not evidence_ids:
            raise EngineServiceError("Attempt recovery에는 직접 실패 evidence가 필요합니다.")
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = tx.all(
            f"SELECT id,project_id,task_id,attempt_id,kind FROM evidence_records "
            f"WHERE id IN ({placeholders})",
            tuple(evidence_ids),
        )
        direct_kinds = {"file", "diff", "command", "test", "build", "external_observation"}
        if len(rows) != len(evidence_ids) or any(
            row["project_id"] != task["project_id"]
            or row["task_id"] != task["id"]
            or row["attempt_id"] != attempt["id"]
            or row["kind"] not in direct_kinds
            for row in rows
        ):
            raise EngineServiceError(
                "Attempt recovery에는 해당 실패 Attempt에 결속된 직접 evidence만 사용할 수 있습니다."
            )
        existing = tx.connection.execute(
            "SELECT project_id,attempt_id,payload_json FROM recovery_assessments WHERE id=?",
            (recovery_assessment.assessment_id,),
        ).fetchone()
        if existing is None:
            self._record_recovery_assessment_in_transaction(
                tx, task["project_id"], recovery_assessment
            )
        elif (
            existing["project_id"] != task["project_id"]
            or existing["attempt_id"] != attempt["id"]
            or json.loads(existing["payload_json"])
            != recovery_assessment.model_dump(mode="json")
        ):
            raise EngineServiceError("기존 RecoveryAssessment ID의 결속 또는 내용이 다릅니다.")
        return evidence_ids

    @staticmethod
    def resolve_task_validation_worker(
        connection: Any, *, task_id: str, current_spec_digest: str
    ) -> Any | None:
        """validator-only selection 계보를 거슬러 실제 성공 Worker를 찾는다."""

        digest = current_spec_digest
        seen: set[str] = set()
        while digest not in seen:
            seen.add(digest)
            worker = connection.execute(
                "SELECT a.id, a.execution_spec_digest, h.sequence FROM attempts a "
                "JOIN history_events h ON h.project_id = a.project_id "
                "AND h.event_type = 'attempt.succeeded' AND h.entity_id = a.id "
                "WHERE a.task_id = ? AND a.kind = 'execution' AND a.status = 'succeeded' "
                "AND a.execution_spec_digest = ? "
                "ORDER BY a.attempt_no DESC, a.rowid DESC LIMIT 1",
                (task_id, digest),
            ).fetchone()
            if worker is not None:
                return worker
            selection = connection.execute(
                "SELECT s.previous_execution_spec_digest FROM model_rebinding_selections s "
                "JOIN attempts a ON a.id = s.attempt_id AND a.task_id = s.task_id "
                "AND a.kind = 'validation' "
                "AND a.execution_spec_digest = s.new_execution_spec_digest "
                "WHERE s.task_id = ? AND s.role = 'validator' "
                "AND s.new_execution_spec_digest = ? "
                "ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1",
                (task_id, digest),
            ).fetchone()
            if selection is None:
                return None
            digest = selection["previous_execution_spec_digest"]
        return None

    @staticmethod
    def effective_task_validation_results(connection: Any, task_id: str) -> tuple[Any, ...]:
        """현재 Worker 성공 및 retry epoch 뒤에 기록된 Task validation만 반환한다."""

        reuse = connection.execute("SELECT validation_ids_json FROM task_completion_reuse WHERE task_id = ?", (task_id,)).fetchone()
        if reuse is not None:
            return tuple(connection.execute("SELECT * FROM validation_results WHERE id = ?", (item,)).fetchone()
                         for item in json.loads(reuse["validation_ids_json"]))

        task = connection.execute(
            "SELECT project_id FROM task_contracts WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise EngineServiceError("TaskContract를 찾을 수 없습니다.")
        retry = connection.execute(
            "SELECT MAX(sequence) FROM history_events WHERE project_id = ? "
            "AND event_type IN ('task.retry_enabled','task.execution_spec_recovery_enabled') "
            "AND entity_id = ?",
            (task["project_id"], task_id),
        ).fetchone()[0]
        current_spec = connection.execute(
            "SELECT definition_digest FROM execution_spec_revisions "
            "WHERE task_id = ? AND is_current = 1",
            (task_id,),
        ).fetchone()
        worker = (
            None
            if current_spec is None
            else EngineService.resolve_task_validation_worker(
                connection,
                task_id=task_id,
                current_spec_digest=current_spec["definition_digest"],
            )
        )
        if worker is None:
            return ()
        worker_sequence = int(worker["sequence"])
        boundary = max(int(retry or 0), worker_sequence)
        candidates = connection.execute(
            "SELECT v.*, h.sequence AS history_sequence, "
            "h.payload_json AS history_payload_json FROM validation_results v "
            "JOIN history_events h ON h.project_id = v.project_id "
            "AND h.event_type = 'validation.recorded' AND h.entity_id = v.id "
            "WHERE v.task_id = ? AND h.sequence > ? ORDER BY h.sequence",
            (task_id, boundary),
        ).fetchall()
        fresh: list[Any] = []
        for row in candidates:
            result = ValidationResult.model_validate_json(row["payload_json"])
            if not result.evidence_ids:
                fresh.append(row)
                continue
            placeholders = ",".join("?" for _ in result.evidence_ids)
            evidence = connection.execute(
                "SELECT e.id, e.attempt_id, e.kind, e.observation, e.content_digest, "
                "h.sequence FROM evidence_records e "
                "JOIN history_events h ON h.project_id = e.project_id "
                "AND h.event_type = 'evidence.recorded' AND h.entity_id = e.id "
                f"WHERE e.id IN ({placeholders})",
                tuple(result.evidence_ids),
            ).fetchall()
            evidence_is_fresh = len(evidence) == len(result.evidence_ids) and all(
                item["attempt_id"] == worker["id"]
                or int(item["sequence"]) > worker_sequence
                for item in evidence
            )
            if evidence_is_fresh and EngineService._task_validation_operation_is_current(
                connection,
                row=row,
                result=result,
                evidence=evidence,
                worker=worker,
                retry_sequence=int(retry or 0),
                current_spec_digest=current_spec["definition_digest"],
            ):
                fresh.append(row)
        return tuple(fresh)

    @staticmethod
    def _task_validation_operation_is_current(
        connection: Any,
        *,
        row: Any,
        result: ValidationResult,
        evidence: Any,
        worker: Any,
        retry_sequence: int,
        current_spec_digest: str,
    ) -> bool:
        """명령 검증이면 완료 operation이 현재 Worker epoch에서 준비됐는지 입증한다."""

        history_payload = json.loads(row["history_payload_json"])
        binding = history_payload.get("operation_binding")
        worker_sequence = int(worker["sequence"])
        epoch_sequence = max(worker_sequence, retry_sequence)
        if binding is not None:
            if not isinstance(binding, dict) or any(
                (
                    binding.get("format") != "task-validation-operation-v1",
                    binding.get("worker_attempt_id") != worker["id"],
                    binding.get("worker_succeeded_sequence") != worker_sequence,
                    binding.get("validation_epoch_sequence") != epoch_sequence,
                    binding.get("source_worker_execution_spec_digest")
                    != worker["execution_spec_digest"],
                    binding.get("validation_execution_spec_digest") != current_spec_digest,
                )
            ):
                return False
            events = connection.execute(
                "SELECT sequence, event_type, payload_json FROM history_events "
                "WHERE project_id = ? AND entity_type = 'core_operation' AND entity_id = ? "
                "ORDER BY sequence",
                (row["project_id"], binding.get("operation_id")),
            ).fetchall()
            prepared_sequence = None
            completed_sequence = None
            for event in events:
                payload = json.loads(event["payload_json"])
                if (
                    event["event_type"] == "operation.prepared"
                    and payload.get("request_digest") == binding.get("request_digest")
                    and isinstance(payload.get("request"), dict)
                    and sha256_digest(
                        {"kind": "validation_command", "request": payload["request"]}
                    )
                    == binding.get("request_digest")
                    and payload["request"].get("worker_attempt_id") == worker["id"]
                    and payload["request"].get("worker_succeeded_sequence") == worker_sequence
                    and payload["request"].get("validation_epoch_sequence") == epoch_sequence
                    and payload["request"].get("source_worker_execution_spec_digest")
                    == worker["execution_spec_digest"]
                    and payload["request"].get("validation_execution_spec_digest")
                    == current_spec_digest
                ):
                    prepared_sequence = int(event["sequence"])
                if (
                    event["event_type"] == "operation.completed"
                    and payload.get("request_digest") == binding.get("request_digest")
                    and payload.get("result_digest") == binding.get("result_digest")
                    and sha256_digest(payload.get("result")) == payload.get("result_digest")
                ):
                    completed_sequence = int(event["sequence"])
            return bool(
                prepared_sequence is not None
                and completed_sequence is not None
                and worker_sequence < prepared_sequence < completed_sequence
                and all(item["attempt_id"] == worker["id"] for item in evidence)
            )

        # schema 변경 전 명령 검증도 operation의 직접 결과와 evidence digest가
        # 유일하게 대응하면 보존한다. 다만 operation 준비가 현재 Worker보다
        # 앞서면 이후에 복제된 validation/evidence여도 stale로 제외한다.
        operation_matches: list[tuple[int, dict[str, Any]]] = []
        operations = connection.execute(
            "SELECT p.sequence AS prepared_sequence, p.payload_json AS prepared_json, "
            "c.payload_json AS completed_json FROM history_events p "
            "JOIN history_events c ON c.project_id = p.project_id "
            "AND c.entity_type = 'core_operation' AND c.entity_id = p.entity_id "
            "AND c.event_type = 'operation.completed' AND c.sequence > p.sequence "
            "WHERE p.project_id = ? AND p.entity_type = 'core_operation' "
            "AND p.event_type = 'operation.prepared' ORDER BY p.sequence",
            (row["project_id"],),
        ).fetchall()
        for operation in operations:
            prepared = json.loads(operation["prepared_json"])
            completed = json.loads(operation["completed_json"])
            request = prepared.get("request")
            response = completed.get("result")
            if (
                prepared.get("kind") != "validation_command"
                or completed.get("kind") != "validation_command"
                or prepared.get("request_digest") != completed.get("request_digest")
                or not isinstance(request, dict)
                or not isinstance(response, dict)
                or sha256_digest({"kind": "validation_command", "request": request})
                != prepared.get("request_digest")
                or sha256_digest(response) != completed.get("result_digest")
                or request.get("plan_revision_id") != row["plan_revision_id"]
                or request.get("task_id") != result.task_id
                or request.get("execution_spec_digest") != worker["execution_spec_digest"]
                or not isinstance(request.get("step"), dict)
                or request["step"].get("validation_id") != result.validation_id
                or not EngineService._validation_evidence_matches_operation(
                    result=result, evidence=evidence, response=response
                )
            ):
                continue
            operation_matches.append((int(operation["prepared_sequence"]), request))
        if not operation_matches:
            return True
        return any(
            prepared_sequence > worker_sequence
            and (
                request.get("worker_attempt_id") is None
                or (
                    request.get("worker_attempt_id") == worker["id"]
                    and request.get("worker_succeeded_sequence") == worker_sequence
                    and request.get("validation_epoch_sequence") == epoch_sequence
                )
            )
            for prepared_sequence, request in operation_matches
        )

    @staticmethod
    def _validation_evidence_matches_operation(
        *, result: ValidationResult, evidence: Any, response: dict[str, Any]
    ) -> bool:
        try:
            observation = DeterministicValidationObservation.model_validate(
                response["observation"]
            )
            artifacts = response["artifacts"]
            if (
                observation.task_id != result.task_id
                or observation.validation_id != result.validation_id
                or observation.observed_at != result.evaluated_at
                or not isinstance(artifacts, list)
            ):
                return False
            for item in evidence:
                kind = item["kind"]
                if kind in {"file", "diff"}:
                    document = json.loads(item["observation"])
                    if document not in artifacts or item["content_digest"] != sha256_digest(
                        {"kind": kind, "direct_file_observation": document}
                    ):
                        return False
                elif kind in {"command", "test", "build"}:
                    if item["content_digest"] != sha256_digest(
                        {"kind": kind, "observation": observation}
                    ):
                        return False
                else:
                    return False
            return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def task_validation_recovery_blocker(
        self, project_id: str, *, plan_revision_id: str | None = None
    ) -> dict[str, Any] | None:
        """성공 Worker 뒤 현재 epoch의 최신 validation FAIL을 읽기 전용으로 찾는다."""

        with self.ledger.read() as connection:
            if plan_revision_id is None:
                project = connection.execute(
                    "SELECT active_plan_revision_id FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if project is None:
                    raise EngineServiceError("프로젝트를 찾을 수 없습니다.")
                plan_revision_id = project["active_plan_revision_id"]
            if plan_revision_id is None:
                return None
            tasks = connection.execute(
                "SELECT id FROM task_contracts WHERE project_id = ? AND plan_revision_id = ? "
                "AND status = 'blocked' ORDER BY position",
                (project_id, plan_revision_id),
            ).fetchall()
            for task in tasks:
                attempt = connection.execute(
                    "SELECT id, status, failure_class FROM attempts WHERE task_id = ? "
                    "AND kind = 'execution' ORDER BY attempt_no DESC, rowid DESC LIMIT 1",
                    (task["id"],),
                ).fetchone()
                if (
                    attempt is None
                    or attempt["status"] != AttemptStatus.SUCCEEDED.value
                    or attempt["failure_class"] is not None
                ):
                    continue
                latest = {
                    row["validation_id"]: row
                    for row in self.effective_task_validation_results(connection, task["id"])
                }
                failure = max(
                    (
                        row
                        for row in latest.values()
                        if row["status"] == ValidationStatus.FAIL.value
                    ),
                    key=lambda row: int(row["history_sequence"]),
                    default=None,
                )
                if failure is not None:
                    result = ValidationResult.model_validate_json(failure["payload_json"])
                    return {
                        "task_id": task["id"],
                        "attempt_id": attempt["id"],
                        "validation_id": result.validation_id,
                        "validation_result_id": result.validation_result_id,
                        "evidence_ids": result.evidence_ids,
                        "history_sequence": int(failure["history_sequence"]),
                    }
        return None

    def _validate_task_validation_recovery(
        self,
        tx: Any,
        *,
        task: Any,
        attempt: Any,
        recovery_assessment: RecoveryAssessment | None,
        failed_validation_result_id: str | None,
        supplied_evidence_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if attempt["status"] != AttemptStatus.SUCCEEDED.value:
            raise EngineServiceError(
                "failure_class 없는 Attempt는 성공 Worker와 실패 Task validation 조합이어야 합니다."
            )
        if recovery_assessment is None or failed_validation_result_id is None:
            raise EngineServiceError(
                "성공 Worker 뒤 validation 실패 재시도에는 실패 결과와 RecoveryAssessment가 필요합니다."
            )
        if recovery_assessment.attempt_id != attempt["id"]:
            raise EngineServiceError("RecoveryAssessment가 현재 성공 Worker Attempt와 다릅니다.")
        if recovery_assessment.failure_class not in {
            FailureClass.IMPLEMENTATION,
            FailureClass.ENVIRONMENT,
            FailureClass.CONTEXT,
        }:
            raise EngineServiceError(
                "Task validation 실패는 implementation/context/environment로만 동일 Task 재시도할 수 있습니다."
            )
        effective = self.effective_task_validation_results(tx.connection, task["id"])
        target = next((row for row in effective if row["id"] == failed_validation_result_id), None)
        if target is None:
            raise EngineServiceError("현재 validation epoch의 실패 결과가 아닙니다.")
        latest = {row["validation_id"]: row for row in effective}
        if latest.get(target["validation_id"])["id"] != failed_validation_result_id:
            raise EngineServiceError("Task 재시도 대상은 해당 검사의 최신 결과여야 합니다.")
        result = ValidationResult.model_validate_json(target["payload_json"])
        if result.task_id != task["id"] or result.status is not ValidationStatus.FAIL:
            raise EngineServiceError("Task 재시도 대상은 현재 Task에 직접 결속된 validation FAIL이어야 합니다.")
        assessment_evidence = recovery_assessment.new_evidence_ids
        if not assessment_evidence:
            raise EngineServiceError("Task validation recovery에는 실패에 직접 결속된 evidence가 필요합니다.")
        if supplied_evidence_ids and supplied_evidence_ids != assessment_evidence:
            raise EngineServiceError("retry evidence와 RecoveryAssessment evidence가 다릅니다.")
        placeholders = ",".join("?" for _ in assessment_evidence)
        evidence_rows = tx.all(
            f"SELECT id, project_id, task_id, attempt_id, kind FROM evidence_records "
            f"WHERE id IN ({placeholders})",
            tuple(assessment_evidence),
        )
        direct_kinds = {"file", "diff", "command", "test", "build", "external_observation"}
        if len(evidence_rows) != len(assessment_evidence) or any(
            row["project_id"] != task["project_id"]
            or row["task_id"] != task["id"]
            or row["kind"] not in direct_kinds
            or (
                row["id"] not in result.evidence_ids
                and row["attempt_id"] != attempt["id"]
            )
            for row in evidence_rows
        ):
            raise EngineServiceError(
                "Task validation recovery에는 FAIL 또는 현재 Worker Attempt에 결속된 직접 evidence만 사용할 수 있습니다."
            )
        if not any(row["id"] in result.evidence_ids for row in evidence_rows):
            raise EngineServiceError("RecoveryAssessment에는 실패 validation에 결속된 직접 evidence가 필요합니다.")
        if recovery_assessment.failure_class is FailureClass.ENVIRONMENT and not any(
            row["attempt_id"] == attempt["id"]
            and row["kind"] in {"command", "test", "build", "external_observation"}
            for row in evidence_rows
        ):
            raise EngineServiceError(
                "environment recovery에는 현재 Worker Attempt에 결속된 원인 evidence가 필요합니다."
            )
        existing = tx.connection.execute(
            "SELECT project_id, attempt_id, payload_json FROM recovery_assessments WHERE id = ?",
            (recovery_assessment.assessment_id,),
        ).fetchone()
        if existing is None:
            self._record_recovery_assessment_in_transaction(
                tx, task["project_id"], recovery_assessment
            )
        elif (
            existing["project_id"] != task["project_id"]
            or existing["attempt_id"] != attempt["id"]
            or json.loads(existing["payload_json"]) != recovery_assessment.model_dump(mode="json")
            or tx.connection.execute(
                "SELECT 1 FROM history_events WHERE project_id = ? "
                "AND event_type = 'recovery.assessed' AND entity_id = ?",
                (task["project_id"], recovery_assessment.assessment_id),
            ).fetchone() is None
        ):
            raise EngineServiceError("기존 RecoveryAssessment ID의 결속 또는 내용이 다릅니다.")
        return assessment_evidence

    @staticmethod
    def _refresh_ready(tx: Any, plan_revision_id: str) -> tuple[str, ...]:
        pending = tx.all(
            "SELECT id FROM task_contracts WHERE plan_revision_id = ? AND status = 'pending' "
            "ORDER BY position",
            (plan_revision_id,),
        )
        ready: list[str] = []
        for row in pending:
            incomplete = tx.connection.execute(
                "SELECT COUNT(*) FROM task_dependencies d "
                "JOIN task_contracts producer ON producer.id = d.producer_task_id "
                "WHERE d.plan_revision_id = ? AND d.consumer_task_id = ? "
                "AND producer.status <> 'completed'",
                (plan_revision_id, row["id"]),
            ).fetchone()[0]
            if incomplete == 0:
                tx.connection.execute(
                    "UPDATE task_contracts SET status = 'ready', updated_at = ? WHERE id = ?",
                    (tx.now, row["id"]),
                )
                ready.append(row["id"])
        return tuple(ready)

    def list_ready_tasks(self, project_id: str) -> tuple[str, ...]:
        with self.ledger.read() as connection:
            rows = connection.execute(
                "SELECT id FROM task_contracts WHERE project_id = ? AND status = 'ready' "
                "ORDER BY position",
                (project_id,),
            ).fetchall()
        return tuple(row["id"] for row in rows)

    def compile_execution_spec(
        self,
        proposal: ExecutionSpecProposal,
        *,
        inventory: ModelInventory,
    ) -> TaskExecutionSpecRevision:
        """비권위 운영 상세 후보를 현재 authority revision들에 결속한다."""

        from .context import ContextSelector, PromptAssembler
        from .worker_prompt import assemble_worker_prompt

        with self.ledger.read() as connection:
            task_row = connection.execute(
                "SELECT t.*, p.root, p.active_profile_revision_id "
                "FROM task_contracts t JOIN projects p ON p.id = t.project_id "
                "WHERE t.id = ?",
                (proposal.task_id,),
            ).fetchone()
            if task_row is None:
                raise EngineServiceError("ExecutionSpecProposal의 Task를 찾을 수 없습니다.")
            if task_row["status"] != "ready":
                raise EngineServiceError("ready Task에 대해서만 proposal을 컴파일할 수 있습니다.")
            plan_row = connection.execute(
                "SELECT * FROM plan_revisions WHERE id = ? AND status = 'active'",
                (task_row["plan_revision_id"],),
            ).fetchone()
            map_row = connection.execute(
                "SELECT payload_json FROM project_map_revisions "
                "WHERE project_id = ? AND is_current = 1",
                (task_row["project_id"],),
            ).fetchone()
            profile_row = connection.execute(
                "SELECT payload_json FROM profile_revisions WHERE id = ?",
                (task_row["active_profile_revision_id"],),
            ).fetchone()
            current_spec = connection.execute(
                "SELECT id, revision_no FROM execution_spec_revisions "
                "WHERE task_id = ? AND is_current = 1",
                (proposal.task_id,),
            ).fetchone()
        if plan_row is None or map_row is None or profile_row is None:
            raise EngineServiceError("ExecutionSpec compilation에 필요한 active authority가 없습니다.")
        plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
        project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
        profile = ProjectProfileRevision.model_validate_json(profile_row["payload_json"])
        state = self.load_current_state(task_row["project_id"], plan.definition.goal_contract_digest)
        sources = self.validate_context_sources(
            task_row["project_id"],
            required_refs=profile.definition.context_source_refs,
        )
        source_paths = {Path(item.path).resolve(): item for item in sources}
        mapped = {(Path(project_map.root) / item.path).resolve() if not Path(item.path).is_absolute() else Path(item.path).resolve(): item for item in project_map.entries}
        missing_mapped = [
            item.context_source_id
            for item in sources
            if Path(item.path).resolve() not in mapped
            or mapped[Path(item.path).resolve()].content_digest != item.content_digest
        ]
        if missing_mapped:
            raise EngineServiceError(
                "CONTEXT_SOURCE_NOT_MAPPED: current Project Map을 다시 관측해야 합니다: "
                + ", ".join(missing_mapped)
            )

        task = next((item for item in plan.definition.tasks if item.task_id == proposal.task_id), None)
        if task is None:
            raise EngineServiceError("active PlanContract에 proposal Task가 없습니다.")
        root = Path(task_row["root"]).resolve(strict=True)

        targets: list[ResolvedTarget] = []
        for target in proposal.resolved_targets:
            raw = Path(target.path)
            resolved = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
            internal = resolved == root or root in resolved.parents
            registered = source_paths.get(resolved)
            if not internal and (registered is None or target.access != "read"):
                raise EngineServiceError(
                    f"프로젝트 밖 target은 등록 Context source의 read만 허용합니다: {target.path}"
                )
            exists = resolved.is_file()
            if target.access == "create":
                if resolved.exists():
                    raise EngineServiceError(f"create target이 이미 존재합니다: {target.path}")
                actual_digest = None
            else:
                if not exists:
                    raise EngineServiceError(f"Execution target 파일이 없습니다: {target.path}")
                actual_digest = sha256_bytes(resolved.read_bytes())
            if (
                target.expected_content_digest is not None
                and target.expected_content_digest != actual_digest
            ):
                raise EngineServiceError(
                    f"proposal target digest가 현재 파일과 다릅니다: {target.path}"
                )
            normalized_path = (
                resolved.relative_to(root).as_posix() if internal and resolved != root else str(resolved)
            )
            targets.append(
                ResolvedTarget(
                    target_ref=target.target_ref,
                    path=normalized_path,
                    symbol=target.symbol,
                    expected_content_digest=actual_digest,
                    access=target.access,
                )
            )

        normalized_steps: list[ValidationExecutionStep] = []
        validation_by_id = {item.validation_id: item for item in task.validations}
        if set(validation_by_id) != {item.validation_id for item in proposal.validation_steps}:
            raise EngineServiceError("ExecutionSpec validation step이 TaskContract와 정확히 대응하지 않습니다.")
        known_evidence_kinds = {item.value for item in EvidenceKind}
        for step in proposal.validation_steps:
            contract = validation_by_id[step.validation_id]
            if step.method != contract.method:
                raise EngineServiceError(
                    f"validation method가 TaskContract와 다릅니다: {step.validation_id}"
                )
            if set(step.required_evidence_kinds) != set(contract.required_evidence_kinds):
                raise EngineServiceError(
                    f"validation evidence kind가 TaskContract와 다릅니다: {step.validation_id}"
                )
            unknown_kinds = set(step.required_evidence_kinds) - known_evidence_kinds
            if unknown_kinds:
                raise EngineServiceError(f"알 수 없는 evidence kind입니다: {sorted(unknown_kinds)}")
            if step.method == "semantic" and task.assignment.validator is None:
                raise EngineServiceError("semantic validation에는 별도 validator 배정이 필요합니다.")
            working_directory = step.working_directory
            if step.method == "deterministic":
                candidate_cwd = Path(working_directory or "")
                resolved_cwd = (
                    (root / candidate_cwd).resolve()
                    if not candidate_cwd.is_absolute()
                    else candidate_cwd.resolve()
                )
                if not resolved_cwd.is_dir() or not (resolved_cwd == root or root in resolved_cwd.parents):
                    raise EngineServiceError("deterministic validation cwd는 프로젝트 내부 디렉터리여야 합니다.")
                working_directory = str(resolved_cwd)
            artifact_paths = step.artifact_paths
            if step.method == "deterministic" and set(step.required_evidence_kinds) & {"file", "diff"}:
                from .validation_execution import normalize_artifact_paths
                artifact_paths = normalize_artifact_paths(
                    root, artifact_paths or tuple(item.path for item in targets)
                )
                target_paths = {(root / item.path).resolve() for item in targets}
                if any((root / path).resolve() not in target_paths for path in artifact_paths):
                    raise EngineServiceError("Task validation artifact는 resolved target에 결속해야 합니다.")
            normalized_steps.append(step.model_copy(update={
                "working_directory": working_directory, "artifact_paths": artifact_paths,
            }))

        preliminary_prompt = PromptAssembler().assemble(
            static_policy=(
                "활성 PlanContract가 지정한 Task 하나만 수행한다. Core 원장을 직접 변경하거나 "
                "다음 Task를 선택하지 않는다."
            ),
            project_policy=canonical_json(profile.definition),
            stage_schema=(
                "실제 변경과 실행 결과를 보고하되 완료 여부는 주장하지 말고, "
                "검증 가능한 파일·명령 evidence 위치를 제시한다."
            ),
            task_instruction=task.objective,
            reference_blocks=(),
        )
        try:
            selection = ContextSelector().select(
                project_map=project_map,
                task=task,
                prompt_binding=preliminary_prompt.binding,
                needs=proposal.context_needs,
                token_budget=proposal.context_token_budget,
            )
        except ValueError as error:
            raise EngineServiceError(str(error)) from error
        if selection.manifest is None:
            request = selection.additional_context_request
            assert request is not None
            raise ContextRequiredError(request)
        manifest = selection.manifest
        executor, validator = self.assignment_resolver.resolve_contract(task.assignment, inventory)
        revision_no = 1 if current_spec is None else int(current_spec["revision_no"]) + 1
        supersedes = None if current_spec is None else current_spec["id"]
        idempotency_key = "flowmarshal-" + sha256_digest(
            {
                "plan_activation_digest": plan.activation_digest,
                "task_contract_digest": task.contract_digest,
                "snapshot_digest": state.snapshot_digest,
                "project_map_digest": project_map.revision_digest,
                "proposal_digest": proposal.proposal_digest,
                "revision_no": revision_no,
                "hint": proposal.idempotency_hint,
            }
        ).split(":", 1)[1]
        definition = TaskExecutionSpecDefinition(
            plan_activation_digest=plan.activation_digest,
            task_contract_digest=task.contract_digest,
            task_id=task.task_id,
            snapshot_digest=state.snapshot_digest,
            project_map_digest=project_map.revision_digest,
            context_manifest=manifest,
            resolved_targets=tuple(targets),
            actions=proposal.actions,
            validation_steps=tuple(normalized_steps),
            executor=executor,
            validator=validator,
            resource_locks=tuple(sorted({f"project:{task.project_id}", *proposal.resource_locks})),
            timeout_seconds=proposal.timeout_seconds,
            idempotency_key=idempotency_key,
        )
        try:
            bundle = assemble_worker_prompt(
                task=task, definition=definition, profile=profile.definition, root=root,
            )
        except (OSError, ValueError) as error:
            raise EngineServiceError(f"Worker Context 조립 실패: {error}") from error
        definition = definition.model_copy(update={
            "context_manifest": manifest.model_copy(update={"prompt_binding": bundle.binding}),
        })
        spec = TaskExecutionSpecRevision(
            execution_spec_revision_id=new_id("execution_spec"),
            task_id=task.task_id,
            revision_no=revision_no,
            definition=definition,
            definition_digest=definition.definition_digest,
            supersedes_execution_spec_revision_id=supersedes,
            created_at=utc_now(),
        )
        self.materialize_execution_spec(spec, inventory=inventory)
        return spec

    def materialize_execution_spec(
        self,
        spec: TaskExecutionSpecRevision,
        *,
        inventory: ModelInventory,
    ) -> None:
        with self.ledger.read() as connection:
            profile_row = connection.execute(
                "SELECT r.payload_json FROM profile_revisions r JOIN projects p "
                "ON p.active_profile_revision_id = r.id JOIN task_contracts t ON t.project_id = p.id "
                "WHERE t.id = ?",
                (spec.task_id,),
            ).fetchone()
        if profile_row is None:
            raise EngineServiceError("active ProjectProfile이 없습니다.")
        profile = ProjectProfileRevision.model_validate_json(profile_row["payload_json"])
        self.validate_context_sources(
            profile.project_id,
            required_refs=profile.definition.context_source_refs,
        )
        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (spec.task_id,))
            if task["status"] not in {"ready", "materialized"}:
                raise EngineServiceError("ready/materialized Task만 ExecutionSpec을 만들 수 있습니다.")
            plan_row = tx.one(
                "SELECT * FROM plan_revisions WHERE id = ? AND status = 'active'",
                (task["plan_revision_id"],),
            )
            plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
            if spec.definition.plan_activation_digest != plan.activation_digest:
                raise EngineServiceError("ExecutionSpec이 active Plan activation digest와 다릅니다.")
            self._plan_authorization(tx, tx.one("SELECT * FROM projects WHERE id = ?", (task["project_id"],)), plan)
            if spec.definition.task_contract_digest != task["contract_digest"]:
                raise EngineServiceError("ExecutionSpec이 TaskContract 의미를 바꾸려 합니다.")
            if spec.definition.task_id != task["id"]:
                raise EngineServiceError("ExecutionSpec task binding이 다릅니다.")
            snapshot_row = tx.one(
                "SELECT * FROM state_snapshots WHERE snapshot_digest = ? AND project_id = ?",
                (spec.definition.snapshot_digest, task["project_id"]),
            )
            if snapshot_row["is_current"] != 1:
                raise EngineServiceError("stale StateSnapshot으로 ExecutionSpec을 만들 수 없습니다.")
            snapshot = StateSnapshot.model_validate_json(snapshot_row["payload_json"])
            if snapshot.goal_contract_digest != plan.definition.goal_contract_digest:
                raise EngineServiceError("ExecutionSpec의 StateSnapshot이 active Goal과 다릅니다.")
            if any(item.freshness.value != "current" for item in snapshot.facts):
                raise EngineServiceError("stale/unknown State fact가 있어 materialization을 중단합니다.")
            map_row = tx.one(
                "SELECT * FROM project_map_revisions WHERE revision_digest = ? AND project_id = ?",
                (spec.definition.project_map_digest, task["project_id"]),
            )
            if map_row["is_current"] != 1:
                raise EngineServiceError("최신 Project Map으로만 ExecutionSpec을 만들 수 있습니다.")
            project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
            known_fragments = {(entry.path, entry.content_digest) for entry in project_map.entries}
            missing_fragments = [
                item.source_ref
                for item in spec.definition.context_manifest.fragments
                if (item.source_ref, item.content_digest) not in known_fragments
                and item.source_kind.value not in {"goal", "state", "task", "tool_output", "policy"}
            ]
            if missing_fragments:
                raise EngineServiceError(
                    f"Context Pack fragment가 Project Map과 결속되지 않았습니다: {missing_fragments}"
                )
            task_contract = next(item for item in plan.definition.tasks if item.task_id == task["id"])
            contract_validations = {item.validation_id: item for item in task_contract.validations}
            spec_validations = {item.validation_id: item for item in spec.definition.validation_steps}
            if set(contract_validations) != set(spec_validations):
                raise EngineServiceError(
                    "ExecutionSpec validation step이 TaskContract와 정확히 대응하지 않습니다."
                )
            for validation_id, step in spec_validations.items():
                contract = contract_validations[validation_id]
                if step.method != contract.method or set(step.required_evidence_kinds) != set(
                    contract.required_evidence_kinds
                ):
                    raise EngineServiceError(
                        f"ExecutionSpec validation binding이 계약과 다릅니다: {validation_id}"
                    )
            expected_executor, expected_validator = self.assignment_resolver.resolve_contract(
                task_contract.assignment,
                inventory,
            )
            if not _same_assignment(expected_executor, spec.definition.executor):
                raise EngineServiceError("ExecutionSpec executor가 assignment envelope와 다릅니다.")
            if (expected_validator is None) != (spec.definition.validator is None):
                raise EngineServiceError("ExecutionSpec validator 유무가 assignment 계약과 다릅니다.")
            if expected_validator is not None and not _same_assignment(
                expected_validator, spec.definition.validator
            ):
                raise EngineServiceError("ExecutionSpec validator가 assignment envelope와 다릅니다.")
            expected_effect_ids = {item.effect_id for item in task_contract.expected_effects}
            action_effect_ids = {
                item.effect_id
                for item in spec.definition.actions
                if item.kind == "external_effect"
            }
            if not action_effect_ids.issubset(expected_effect_ids):
                raise EngineServiceError("PlanContract에 없는 외부 효과를 ExecutionSpec에 추가할 수 없습니다.")
            current = tx.maybe_one(
                "SELECT id, revision_no FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                (task["id"],),
            )
            expected_no = 1 if current is None else int(current["revision_no"]) + 1
            if spec.revision_no != expected_no:
                raise EngineServiceError(f"ExecutionSpec revision_no는 {expected_no}여야 합니다.")
            if current is not None and spec.supersedes_execution_spec_revision_id != current["id"]:
                raise EngineServiceError("새 ExecutionSpec은 현재 revision을 supersede해야 합니다.")
            # 수동 명세도 동일한 Core 조립 결과에 결속하며 본문 게시가 먼저 완료돼야 한다.
            from .worker_prompt import PromptArtifactStore, assemble_worker_prompt
            project = tx.one("SELECT root FROM projects WHERE id = ?", (task["project_id"],))
            try:
                bundle = assemble_worker_prompt(
                    task=task_contract, definition=spec.definition,
                    profile=profile.definition, root=Path(project["root"]),
                )
                if bundle.binding != spec.definition.context_manifest.prompt_binding:
                    raise ValueError("PROMPT_BINDING_MISMATCH: Core 조립 본문과 명세가 다릅니다.")
                PromptArtifactStore(self.ledger.artifact_root).put(bundle)
            except (OSError, ValueError) as error:
                raise EngineServiceError(f"Worker Prompt 저장 실패: {error}") from error
            if current is not None:
                tx.connection.execute(
                    "UPDATE execution_spec_revisions SET is_current = 0 WHERE id = ?",
                    (current["id"],),
                )
            tx.connection.execute(
                "INSERT INTO execution_spec_revisions "
                "(id, task_id, revision_no, definition_digest, payload_json, supersedes_id, "
                "created_at, is_current) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    spec.execution_spec_revision_id,
                    spec.task_id,
                    spec.revision_no,
                    spec.definition_digest,
                    canonical_json(spec),
                    spec.supersedes_execution_spec_revision_id,
                    spec.created_at.isoformat(),
                ),
            )
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'materialized', updated_at = ? WHERE id = ?",
                (tx.now, task["id"]),
            )
            tx.history(
                task["project_id"],
                "task.materialized",
                "execution_spec_revision",
                spec.execution_spec_revision_id,
                {"task_id": task["id"], "definition_digest": spec.definition_digest},
            )

    def record_effect_checkpoint(
        self,
        *,
        task_id: str,
        effect_id: str,
        execution_spec_digest: str,
        approved_by: str,
    ) -> str:
        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (task_id,))
            contract = json.loads(task["payload_json"])
            matching = [
                item
                for item in contract["expected_effects"]
                if item["effect_id"] == effect_id and item["external"] and not item["reversible"]
            ]
            if contract["approval_class"] != ApprovalClass.EXECUTION_CHECKPOINT.value or not matching:
                raise EngineServiceError("checkpoint 대상인 비가역 외부 효과가 아닙니다.")
            spec = tx.one(
                "SELECT definition_digest FROM execution_spec_revisions WHERE task_id = ? "
                "AND is_current = 1",
                (task_id,),
            )
            if spec["definition_digest"] != execution_spec_digest:
                raise EngineServiceError("checkpoint가 현재 ExecutionSpec과 다릅니다.")
            checkpoint_id = new_id("checkpoint")
            tx.connection.execute(
                "INSERT INTO effect_checkpoints "
                "(id, task_id, effect_id, execution_spec_digest, approved_by, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (checkpoint_id, task_id, effect_id, execution_spec_digest, approved_by, tx.now),
            )
            tx.history(
                task["project_id"],
                "effect.checkpointed",
                "effect_checkpoint",
                checkpoint_id,
                {"task_id": task_id, "effect_id": effect_id, "execution_spec_digest": execution_spec_digest},
            )
            return checkpoint_id

    def reserve_attempt(self, *, task_id: str, kind: AttemptKind = AttemptKind.EXECUTION) -> AttemptRecord:
        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (task_id,))
            expected_task_status = (
                TaskRuntimeStatus.MATERIALIZED.value
                if kind is AttemptKind.EXECUTION
                else TaskRuntimeStatus.VALIDATING.value
            )
            if task["status"] != expected_task_status:
                raise EngineServiceError(
                    "execution Attempt는 materialized Task, validation Attempt는 validating Task에서만 "
                    "예약할 수 있습니다."
                )
            plan = tx.one("SELECT * FROM plan_revisions WHERE id = ?", (task["plan_revision_id"],))
            if plan["status"] != "active":
                raise EngineServiceError("active Plan의 Task만 실행할 수 있습니다.")
            project = tx.one("SELECT * FROM projects WHERE id = ?", (task["project_id"],))
            self._plan_authorization(tx, project, PlanContractRevision.model_validate_json(plan["payload_json"]))
            if project["run_state"] == "recovery_required":
                raise EngineServiceError("crash recovery가 끝나기 전에는 새 Attempt를 만들 수 없습니다.")
            spec_row = tx.one(
                "SELECT * FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                (task_id,),
            )
            if kind is AttemptKind.EXECUTION:
                self._verify_execution_inputs(tx, task, spec_row)
            contract = json.loads(task["payload_json"])
            irreversible = {
                item["effect_id"]
                for item in contract["expected_effects"]
                if item["external"] and not item["reversible"]
            }
            if irreversible and kind is AttemptKind.EXECUTION:
                approved = {
                    row["effect_id"]
                    for row in tx.all(
                        "SELECT effect_id FROM effect_checkpoints WHERE task_id = ? "
                        "AND execution_spec_digest = ?",
                        (task_id, spec_row["definition_digest"]),
                    )
                }
                if approved != irreversible:
                    raise EngineServiceError("비가역 외부 효과의 실행 직전 checkpoint가 부족합니다.")
            attempt_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts WHERE task_id = ? AND kind = ?",
                    (task_id, kind.value),
                ).fetchone()[0]
            )
            attempt_id = new_id("attempt")
            now = tx.now
            try:
                tx.connection.execute(
                    "INSERT INTO attempts "
                    "(id, project_id, plan_revision_id, task_id, execution_spec_digest, attempt_no, "
                    "kind, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
                    (
                        attempt_id,
                        task["project_id"],
                        task["plan_revision_id"],
                        task_id,
                        spec_row["definition_digest"],
                        attempt_no,
                        kind.value,
                        now,
                        now,
                    ),
                )
            except Exception as error:
                if "uq_engine_one_active_attempt_per_project" in str(error) or "UNIQUE constraint" in str(error):
                    raise EngineServiceError("동일 프로젝트에서는 한 Attempt만 직렬 실행할 수 있습니다.") from error
                raise
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'reserved', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            tx.history(
                task["project_id"],
                "attempt.reserved",
                "attempt",
                attempt_id,
                {"task_id": task_id, "attempt_no": attempt_no, "kind": kind.value},
            )
            return AttemptRecord(
                attempt_id=attempt_id,
                task_id=task_id,
                execution_spec_digest=spec_row["definition_digest"],
                attempt_no=attempt_no,
                kind=kind,
                status=AttemptStatus.RESERVED,
            )

    @staticmethod
    def _verify_execution_inputs(
        tx: Any,
        task: Any,
        spec_row: Any,
        *,
        allow_mutable_targets: bool = False,
    ) -> None:
        """현재 immutable execution 입력을 재확인한다.

        최초 create/start에는 모든 target/context를 고정한다. 이미 시작된 Worker의
        허용된 resume에서는 그 Worker가 쓸 수 있었던 target만 달라질 수 있으며,
        State·Project Map·policy/reference/read-only context는 계속 고정한다.
        """

        spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
        project = tx.one("SELECT root FROM projects WHERE id = ?", (task["project_id"],))
        current_map = tx.one(
            "SELECT revision_digest FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
            (task["project_id"],),
        )
        if current_map["revision_digest"] != spec.definition.project_map_digest:
            raise EngineServiceError(
                "STALE_EXECUTION_INPUT: ExecutionSpec 이후 Project Map이 변경됐습니다."
            )
        snapshot = tx.maybe_one(
            "SELECT is_current FROM state_snapshots WHERE project_id = ? AND snapshot_digest = ?",
            (task["project_id"], spec.definition.snapshot_digest),
        )
        if snapshot is None or snapshot["is_current"] != 1:
            raise EngineServiceError(
                "STALE_EXECUTION_INPUT: ExecutionSpec 이후 StateSnapshot이 변경됐습니다."
            )
        root = Path(project["root"])
        checks: dict[str, str] = {}
        absent: set[str] = set()
        mutable_paths: set[Path] = set()
        for target in spec.definition.resolved_targets:
            target_path = Path(target.path)
            resolved_target = (
                target_path.resolve()
                if target_path.is_absolute()
                else (root / target_path).resolve()
            )
            if target.access in {"write", "create", "delete"}:
                mutable_paths.add(resolved_target)
            if target.expected_content_digest is not None:
                checks[target.path] = target.expected_content_digest
            elif target.access == "create":
                absent.add(target.path)
        for fragment in spec.definition.context_manifest.fragments:
            if fragment.source_kind.value in {"code", "test", "reference", "policy", "project"}:
                checks[fragment.source_ref] = fragment.content_digest
        for raw_path in absent:
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            if allow_mutable_targets and path.resolve() in mutable_paths:
                continue
            if path.exists():
                raise EngineServiceError(
                    f"STALE_EXECUTION_INPUT: create target이 실행 전에 생겼습니다: {raw_path}"
                )
        for raw_path, expected_digest in checks.items():
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            if allow_mutable_targets and path.resolve() in mutable_paths:
                continue
            try:
                actual_digest = sha256_bytes(path.read_bytes())
            except OSError as error:
                raise EngineServiceError(
                    f"실행 전 입력 파일을 재확인할 수 없습니다: {raw_path}"
                ) from error
            if actual_digest != expected_digest:
                raise EngineServiceError(
                    f"STALE_EXECUTION_INPUT: materialization 이후 파일이 바뀌었습니다: {raw_path}"
                )

    def _assert_attempt_authorized(self, tx: Any, attempt: Any) -> None:
        project = tx.one("SELECT * FROM projects WHERE id = ?", (attempt["project_id"],))
        if project["active_plan_revision_id"] != attempt["plan_revision_id"]:
            raise EngineServiceError("Attempt가 현재 active Plan에 결속되지 않았습니다.")
        plan = PlanContractRevision.model_validate_json(tx.one("SELECT payload_json FROM plan_revisions WHERE id = ?",
                                                              (attempt["plan_revision_id"],))["payload_json"])
        self._plan_authorization(tx, project, plan)

    def assert_attempt_authorized(self, attempt_id: str) -> None:
        """예약 후 정책 변경도 새 효과 전에 대조한다. 관측·취소는 승인 갱신 없이 허용한다."""
        with self.ledger.transaction() as tx:
            self._assert_attempt_authorized(tx, tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,)))

    def assert_project_authorized(self, project_id: str) -> None:
        """Attempt 없는 실행 준비·Task 검사·Goal Test에도 같은 승인 경계를 적용한다."""
        with self.ledger.transaction() as tx:
            project = tx.one("SELECT * FROM projects WHERE id = ?", (project_id,))
            plan = PlanContractRevision.model_validate_json(tx.one("SELECT payload_json FROM plan_revisions WHERE id = ?",
                                                                  (project["active_plan_revision_id"],))["payload_json"])
            self._plan_authorization(tx, project, plan)

    def prepare_authorized_runtime_effect(
        self,
        attempt_id: str,
        intent_id: str | None,
        *,
        request: Any | None = None,
        allow_mutable_targets: bool = False,
    ) -> None:
        """효과 호출 직전의 durable intent와 immutable 입력을 원자적으로 대조한다."""
        if intent_id is None:
            raise EngineServiceError("runtime 효과에는 먼저 저장된 intent가 필요합니다.")
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            intent = tx.one(
                "SELECT * FROM runtime_intents WHERE id = ? AND attempt_id = ? AND status = 'prepared'",
                (intent_id, attempt_id),
            )
            if request is None or intent["request_digest"] != sha256_digest(request):
                raise EngineServiceError(
                    "RUNTIME_INTENT_BINDING_MISMATCH: 저장된 intent와 실제 효과 요청이 다릅니다."
                )
            self._assert_attempt_authorized(tx, attempt)
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (attempt["task_id"],))
            spec_row = tx.one(
                "SELECT * FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                (attempt["task_id"],),
            )
            if attempt["execution_spec_digest"] != spec_row["definition_digest"]:
                raise EngineServiceError(
                    "STALE_EXECUTION_INPUT: Attempt와 current ExecutionSpec이 다릅니다."
                )
            self._verify_execution_inputs(
                tx,
                task,
                spec_row,
                allow_mutable_targets=allow_mutable_targets,
            )
            contract = json.loads(task["payload_json"])
            irreversible = {
                item["effect_id"]
                for item in contract["expected_effects"]
                if item["external"] and not item["reversible"]
            }
            if irreversible and attempt["kind"] == AttemptKind.EXECUTION.value:
                approved = {
                    row["effect_id"]
                    for row in tx.all(
                        "SELECT effect_id FROM effect_checkpoints WHERE task_id = ? "
                        "AND execution_spec_digest = ?",
                        (task["id"], spec_row["definition_digest"]),
                    )
                }
                if approved != irreversible:
                    raise EngineServiceError(
                        "EFFECT_CHECKPOINT_STALE: 비가역 외부 효과 승인이 효과 직전에 다릅니다."
                    )
            tx.history(
                attempt["project_id"],
                "runtime.effect_dispatching",
                "runtime_intent",
                intent_id,
                {
                    "attempt_id": attempt_id,
                    "request_digest": intent["request_digest"],
                    "execution_spec_digest": attempt["execution_spec_digest"],
                    "mutable_target_resume": allow_mutable_targets,
                },
            )

    def record_runtime_effect_not_started(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        code: str,
        detail: str,
        changes: tuple[dict[str, Any], ...] = (),
    ) -> None:
        """각 preflight 거부가 provider 호출 전이었다는 사실을 기록한다."""

        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT project_id FROM attempts WHERE id = ?", (attempt_id,))
            tx.one(
                "SELECT id FROM runtime_intents WHERE id = ? AND attempt_id = ? AND status = 'prepared'",
                (intent_id, attempt_id),
            )
            tx.history(
                attempt["project_id"],
                "runtime.effect_not_started",
                "runtime_intent",
                intent_id,
                {
                    "attempt_id": attempt_id,
                    "code": code,
                    "detail": detail,
                    "changes": changes,
                },
            )

    def prepare_runtime_intent(
        self,
        *,
        attempt_id: str,
        kind: RuntimeIntentKind,
        idempotency_key: str,
        request: Any,
    ) -> RuntimeIntentRecord:
        request_digest = sha256_digest(request)
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if kind is not RuntimeIntentKind.INTERRUPT_TURN:
                self._assert_attempt_authorized(tx, attempt)
            existing = tx.maybe_one(
                "SELECT * FROM runtime_intents WHERE idempotency_key = ?", (idempotency_key,)
            )
            if existing is not None:
                if existing["attempt_id"] != attempt_id or existing["request_digest"] != request_digest:
                    raise EngineServiceError("idempotency key가 다른 요청에 이미 사용됐습니다.")
                return RuntimeIntentRecord(
                    intent_id=existing["id"],
                    attempt_id=existing["attempt_id"],
                    kind=RuntimeIntentKind(existing["kind"]),
                    idempotency_key=existing["idempotency_key"],
                    request_digest=existing["request_digest"],
                    status=RuntimeIntentStatus(existing["status"]),
                    prepared_at=_dt(existing["prepared_at"]),  # type: ignore[arg-type]
                )
            if attempt["status"] not in {"reserved", "running"}:
                raise EngineServiceError("reserved/running Attempt에만 runtime intent를 준비할 수 있습니다.")
            pending = tx.maybe_one(
                "SELECT id FROM runtime_intents WHERE attempt_id = ? AND status = 'prepared'",
                (attempt_id,),
            )
            if pending is not None:
                raise EngineServiceError("Attempt에 receipt 없는 runtime intent가 이미 있습니다.")
            intent_id = new_id("intent")
            now = tx.now
            tx.connection.execute(
                "INSERT INTO runtime_intents "
                "(id, attempt_id, kind, idempotency_key, request_digest, request_json, status, "
                "prepared_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
                (
                    intent_id,
                    attempt_id,
                    kind.value,
                    idempotency_key,
                    request_digest,
                    canonical_json(request),
                    now,
                    now,
                ),
            )
            tx.connection.execute(
                "UPDATE attempts SET status = 'starting', updated_at = ? WHERE id = ?",
                (now, attempt_id),
            )
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'running', updated_at = ? WHERE id = ?",
                (now, attempt["task_id"]),
            )
            tx.history(
                attempt["project_id"],
                "runtime.intent_prepared",
                "runtime_intent",
                intent_id,
                {"attempt_id": attempt_id, "kind": kind.value, "request_digest": request_digest},
            )
            return RuntimeIntentRecord(
                intent_id=intent_id,
                attempt_id=attempt_id,
                kind=kind,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                status=RuntimeIntentStatus.PREPARED,
                prepared_at=_dt(now),  # type: ignore[arg-type]
            )

    def record_runtime_receipt(
        self,
        *,
        intent_id: str,
        provider_operation_id: str,
        response: Any,
        binding: ThreadBinding | None = None,
        allow_reconcile_unknown: bool = False,
    ) -> RuntimeReceipt:
        response_digest = sha256_digest(response)
        with self.ledger.transaction() as tx:
            intent = tx.one("SELECT * FROM runtime_intents WHERE id = ?", (intent_id,))
            if intent["status"] == "received":
                receipt = tx.one("SELECT * FROM runtime_receipts WHERE intent_id = ?", (intent_id,))
                if receipt["provider_operation_id"] != provider_operation_id or receipt["response_digest"] != response_digest:
                    raise EngineServiceError("같은 intent에 서로 다른 receipt를 기록할 수 없습니다.")
                return RuntimeReceipt.model_validate_json(receipt["payload_json"])
            if intent["status"] != "prepared" and not (
                allow_reconcile_unknown and intent["status"] == "unknown"
            ):
                raise EngineServiceError("prepared intent에만 receipt를 기록할 수 있습니다.")
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (intent["attempt_id"],))
            receipt = RuntimeReceipt(
                receipt_id=new_id("receipt"),
                intent_id=intent_id,
                provider_operation_id=provider_operation_id,
                response_digest=response_digest,
                binding=binding,
                response_payload=(response if isinstance(response, dict) and
                                  intent["kind"] in {"create_thread", "start_turn", "resume_turn"} else None),
                received_at=utc_now(),
            )
            tx.connection.execute(
                "INSERT INTO runtime_receipts "
                "(id, intent_id, provider_operation_id, response_digest, payload_json, binding_json, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    intent_id,
                    provider_operation_id,
                    response_digest,
                    canonical_json(receipt),
                    canonical_json(binding) if binding is not None else None,
                    receipt.received_at.isoformat(),
                ),
            )
            tx.connection.execute(
                "UPDATE runtime_intents SET status = 'received', updated_at = ? WHERE id = ?",
                (tx.now, intent_id),
            )
            tx.connection.execute(
                "UPDATE attempts SET status = 'running', binding_json = COALESCE(?, binding_json), "
                "started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                (
                    canonical_json(binding) if binding is not None else None,
                    tx.now,
                    tx.now,
                    attempt["id"],
                ),
            )
            if allow_reconcile_unknown:
                remaining = tx.connection.execute(
                    "SELECT COUNT(*) FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                    "WHERE a.project_id = ? AND i.status = 'unknown'",
                    (attempt["project_id"],),
                ).fetchone()[0]
                if remaining == 0:
                    tx.connection.execute(
                        "UPDATE projects SET run_state = 'active', recovery_reason = NULL, updated_at = ? "
                        "WHERE id = ?",
                        (tx.now, attempt["project_id"]),
                    )
            tx.history(
                attempt["project_id"],
                "runtime.receipt_recorded",
                "runtime_receipt",
                receipt.receipt_id,
                {"intent_id": intent_id, "provider_operation_id": provider_operation_id},
            )
            return receipt

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        succeeded: bool,
        failure_class: FailureClass | None = None,
        detail: str | None = None,
    ) -> None:
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if attempt["status"] not in {"reserved", "starting", "running"}:
                raise EngineServiceError("active Attempt만 종료할 수 있습니다.")
            unreceipted = tx.connection.execute(
                "SELECT COUNT(*) FROM runtime_intents WHERE attempt_id = ? AND status = 'prepared'",
                (attempt_id,),
            ).fetchone()[0]
            if unreceipted:
                raise EngineServiceError("receipt 없는 외부 효과가 있어 일반 종료할 수 없습니다.")
            if succeeded and failure_class is not None:
                raise EngineServiceError("성공 Attempt에는 failure_class를 기록할 수 없습니다.")
            if not succeeded and failure_class is None:
                raise EngineServiceError("실패 Attempt에는 failure_class가 필요합니다.")
            status = "succeeded" if succeeded else "failed"
            task_status = "validating" if succeeded else "failed"
            now = tx.now
            tx.connection.execute(
                "UPDATE attempts SET status = ?, failure_class = ?, failure_detail = ?, "
                "ended_at = ?, updated_at = ? WHERE id = ?",
                (status, failure_class.value if failure_class else None, detail, now, now, attempt_id),
            )
            tx.connection.execute(
                "UPDATE task_contracts SET status = ?, updated_at = ? WHERE id = ?",
                (task_status, now, attempt["task_id"]),
            )
            tx.history(
                attempt["project_id"],
                f"attempt.{status}",
                "attempt",
                attempt_id,
                {"task_id": attempt["task_id"], "failure_class": failure_class.value if failure_class else None},
            )

    def record_evidence(self, evidence: EvidenceRecord) -> None:
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (evidence.project_id,))
            if evidence.task_id is not None:
                task = tx.one("SELECT project_id FROM task_contracts WHERE id = ?", (evidence.task_id,))
                if task["project_id"] != evidence.project_id:
                    raise EngineServiceError("Evidence Task가 다른 프로젝트에 속합니다.")
            if evidence.attempt_id is not None:
                attempt = tx.one("SELECT project_id, task_id FROM attempts WHERE id = ?", (evidence.attempt_id,))
                if attempt["project_id"] != evidence.project_id or (
                    evidence.task_id is not None and attempt["task_id"] != evidence.task_id
                ):
                    raise EngineServiceError("Evidence Attempt binding이 다릅니다.")
            tx.connection.execute(
                "INSERT INTO evidence_records "
                "(id, project_id, task_id, attempt_id, kind, source_ref, observation, content_digest, "
                "payload_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.evidence_id,
                    evidence.project_id,
                    evidence.task_id,
                    evidence.attempt_id,
                    evidence.kind.value,
                    evidence.source_ref,
                    evidence.observation,
                    evidence.content_digest,
                    canonical_json(evidence),
                    evidence.observed_at.isoformat(),
                ),
            )
            tx.history(
                evidence.project_id,
                "evidence.recorded",
                "evidence_record",
                evidence.evidence_id,
                {"task_id": evidence.task_id, "kind": evidence.kind.value, "content_digest": evidence.content_digest},
            )

    @staticmethod
    def _verify_task_validation_operation_binding(
        tx: Any,
        *,
        project_id: str,
        plan_revision_id: str,
        result: ValidationResult,
        binding: dict[str, Any],
    ) -> None:
        expected_keys = {
            "format",
            "operation_id",
            "request_digest",
            "result_digest",
            "worker_attempt_id",
            "worker_succeeded_sequence",
            "validation_epoch_sequence",
            "validation_execution_spec_digest",
            "source_worker_execution_spec_digest",
        }
        if set(binding) != expected_keys or binding.get("format") != "task-validation-operation-v1":
            raise EngineServiceError("Task validation operation binding 형식이 정확하지 않습니다.")
        if result.task_id is None:
            raise EngineServiceError("Task validation operation binding은 Task 결과에만 사용할 수 있습니다.")
        attempt = tx.one(
            "SELECT id, status, failure_class, execution_spec_digest FROM attempts "
            "WHERE task_id = ? AND kind = 'execution' "
            "ORDER BY attempt_no DESC, rowid DESC LIMIT 1",
            (result.task_id,),
        )
        succeeded = tx.one(
            "SELECT sequence FROM history_events WHERE project_id = ? "
            "AND event_type = 'attempt.succeeded' AND entity_id = ?",
            (project_id, attempt["id"]),
        )
        retry_sequence = int(tx.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM history_events WHERE project_id = ? "
            "AND event_type = 'task.retry_enabled' AND entity_id = ?",
            (project_id, result.task_id),
        ).fetchone()[0])
        worker_sequence = int(succeeded["sequence"])
        current_spec = tx.one(
            "SELECT definition_digest FROM execution_spec_revisions "
            "WHERE task_id = ? AND is_current = 1",
            (result.task_id,),
        )
        resolved_worker = EngineService.resolve_task_validation_worker(
            tx.connection,
            task_id=result.task_id,
            current_spec_digest=current_spec["definition_digest"],
        )
        if (
            attempt["status"] != AttemptStatus.SUCCEEDED.value
            or attempt["failure_class"] is not None
            or resolved_worker is None
            or resolved_worker["id"] != attempt["id"]
            or binding.get("worker_attempt_id") != attempt["id"]
            or binding.get("worker_succeeded_sequence") != worker_sequence
            or binding.get("validation_epoch_sequence") != max(worker_sequence, retry_sequence)
            or binding.get("source_worker_execution_spec_digest")
            != attempt["execution_spec_digest"]
            or binding.get("validation_execution_spec_digest")
            != current_spec["definition_digest"]
        ):
            raise EngineServiceError("Task validation operation이 현재 성공 Worker epoch와 다릅니다.")
        operation_id = binding.get("operation_id")
        request_digest = binding.get("request_digest")
        expected_operation_id = "operation_" + sha256_digest(
            {"project_id": project_id, "request_digest": request_digest}
        )[7:39]
        if operation_id != expected_operation_id:
            raise EngineServiceError("Task validation operation ID와 request digest가 다릅니다.")
        rows = tx.all(
            "SELECT sequence, event_type, payload_json FROM history_events WHERE project_id = ? "
            "AND entity_type = 'core_operation' AND entity_id = ? ORDER BY sequence",
            (project_id, operation_id),
        )
        prepared = []
        completed = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if row["event_type"] == "operation.prepared":
                prepared.append((int(row["sequence"]), payload))
            elif row["event_type"] == "operation.completed":
                completed.append((int(row["sequence"]), payload))
        valid_prepared = [
            (sequence, payload)
            for sequence, payload in prepared
            if sequence > worker_sequence
            and payload.get("kind") == "validation_command"
            and payload.get("request_digest") == request_digest
            and isinstance(payload.get("request"), dict)
            and sha256_digest(
                {"kind": "validation_command", "request": payload["request"]}
            )
            == request_digest
            and payload["request"].get("project_id", project_id) == project_id
            and payload["request"].get("plan_revision_id") == plan_revision_id
            and payload["request"].get("task_id") == result.task_id
            and payload["request"].get("worker_attempt_id") == attempt["id"]
            and payload["request"].get("worker_succeeded_sequence") == worker_sequence
            and payload["request"].get("validation_epoch_sequence") == max(
                worker_sequence, retry_sequence
            )
            and payload["request"].get("source_worker_execution_spec_digest")
            == attempt["execution_spec_digest"]
            and payload["request"].get("validation_execution_spec_digest")
            == current_spec["definition_digest"]
            and payload["request"].get("execution_spec_digest")
            == current_spec["definition_digest"]
        ]
        valid_completed = [
            (sequence, payload)
            for sequence, payload in completed
            if payload.get("kind") == "validation_command"
            and payload.get("request_digest") == request_digest
            and payload.get("result_digest") == binding.get("result_digest")
            and sha256_digest(payload.get("result")) == payload.get("result_digest")
        ]
        if not any(
            prepared_sequence < completed_sequence
            for prepared_sequence, _ in valid_prepared
            for completed_sequence, _ in valid_completed
        ):
            raise EngineServiceError("Task validation operation의 준비·완료 receipt 결속이 없습니다.")
        if result.evidence_ids:
            placeholders = ",".join("?" for _ in result.evidence_ids)
            evidence = tx.all(
                f"SELECT attempt_id FROM evidence_records WHERE id IN ({placeholders})",
                tuple(result.evidence_ids),
            )
            if len(evidence) != len(result.evidence_ids) or any(
                row["attempt_id"] != attempt["id"] for row in evidence
            ):
                raise EngineServiceError("Task validation evidence가 현재 Worker Attempt에 결속되지 않았습니다.")

    def record_validation(
        self,
        *,
        project_id: str,
        plan_revision_id: str,
        result: ValidationResult,
        operation_binding: dict[str, Any] | None = None,
    ) -> None:
        with self.ledger.transaction() as tx:
            plan_row = tx.one(
                "SELECT payload_json FROM plan_revisions WHERE id = ? AND project_id = ?",
                (plan_revision_id, project_id),
            )
            plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
            task_validation_ids = {
                validation.validation_id: task.task_id
                for task in plan.definition.tasks
                for validation in task.validations
            }
            validation_contracts = {
                validation.validation_id: validation
                for task in plan.definition.tasks
                for validation in task.validations
            }
            integration_ids = {
                validation.validation_id for validation in plan.definition.integration_validations
            }
            validation_contracts.update(
                {
                    validation.validation_id: validation
                    for validation in plan.definition.integration_validations
                }
            )
            if result.validation_id not in task_validation_ids and result.validation_id not in integration_ids:
                raise EngineServiceError("PlanContract에 없는 validation 결과입니다.")
            expected_task = task_validation_ids.get(result.validation_id)
            if expected_task != result.task_id:
                raise EngineServiceError("validation의 Task binding이 계약과 다릅니다.")
            if result.evidence_ids:
                placeholders = ",".join("?" for _ in result.evidence_ids)
                rows = tx.all(
                    f"SELECT id, project_id, task_id, kind FROM evidence_records WHERE id IN ({placeholders})",
                    tuple(result.evidence_ids),
                )
                if len(rows) != len(result.evidence_ids):
                    raise EngineServiceError("validation evidence 일부가 원장에 없습니다.")
                if any(row["project_id"] != project_id for row in rows):
                    raise EngineServiceError("다른 프로젝트 evidence를 validation에 사용할 수 없습니다.")
                if result.task_id is not None and any(
                    row["task_id"] not in {None, result.task_id} for row in rows
                ):
                    raise EngineServiceError("다른 Task evidence를 validation에 사용할 수 없습니다.")
                if result.status in {ValidationStatus.PASS, ValidationStatus.FAIL}:
                    required_kinds = set(
                        validation_contracts[result.validation_id].required_evidence_kinds
                    )
                    actual_kinds = {row["kind"] for row in rows}
                    if not required_kinds.issubset(actual_kinds):
                        raise EngineServiceError(
                            "validation에 필요한 evidence kind가 부족합니다: "
                            + ", ".join(sorted(required_kinds - actual_kinds))
                        )
            if operation_binding is not None:
                self._verify_task_validation_operation_binding(
                    tx,
                    project_id=project_id,
                    plan_revision_id=plan_revision_id,
                    result=result,
                    binding=operation_binding,
                )
            tx.connection.execute(
                "INSERT INTO validation_results "
                "(id, project_id, plan_revision_id, task_id, validation_id, status, payload_json, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.validation_result_id,
                    project_id,
                    plan_revision_id,
                    result.task_id,
                    result.validation_id,
                    result.status.value,
                    canonical_json(result),
                    result.evaluated_at.isoformat(),
                ),
            )
            from .plan_reuse import observation_checkpoint
            reuse_checkpoint = None if result.task_id is None else observation_checkpoint(self, tx.connection, result.task_id)
            tx.history(
                project_id,
                "validation.recorded",
                "validation_result",
                result.validation_result_id,
                {
                    "validation_id": result.validation_id,
                    "status": result.status.value,
                    "task_id": result.task_id,
                    "operation_binding": operation_binding,
                    "reuse_checkpoint": reuse_checkpoint,
                },
            )

    def record_typed_validation_observation(
        self,
        *,
        project_id: str,
        plan_revision_id: str,
        observation: ManualValidationObservation | ExternalValidationObservation,
    ) -> ValidationResult:
        """수동·외부 관측을 계약과 대조해 evidence 및 validation 결과로 변환한다."""

        with self.ledger.read() as connection:
            plan_row = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ? AND project_id = ?",
                (plan_revision_id, project_id),
            ).fetchone()
            if plan_row is None:
                raise EngineServiceError("관측 대상 PlanContract를 찾을 수 없습니다.")
            plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
            contracts = {
                validation.validation_id: (task.task_id, validation)
                for task in plan.definition.tasks
                for validation in task.validations
            }
            contracts.update(
                {
                    validation.validation_id: (None, validation)
                    for validation in plan.definition.integration_validations
                }
            )
            bound = contracts.get(observation.validation_id)
            if bound is None:
                raise EngineServiceError("PlanContract에 없는 validation 관측입니다.")
            expected_task_id, contract = bound
            if observation.task_id != expected_task_id:
                raise EngineServiceError("typed observation의 Task binding이 계약과 다릅니다.")
            expected_method = (
                "manual"
                if isinstance(observation, ManualValidationObservation)
                else "external_observation"
            )
            if contract.method != expected_method:
                raise EngineServiceError("typed observation 종류가 validation 계약과 다릅니다.")
            expected_kind = "user_decision" if expected_method == "manual" else "external_observation"
            if set(contract.required_evidence_kinds) != {expected_kind}:
                raise EngineServiceError("typed observation을 파일·명령·모델 증거로 재표시할 수 없습니다.")
            if observation.task_id is not None:
                spec_row = connection.execute(
                    "SELECT payload_json FROM execution_spec_revisions "
                    "WHERE task_id = ? AND is_current = 1",
                    (observation.task_id,),
                ).fetchone()
                if spec_row is None:
                    raise EngineServiceError("typed observation 대상 ExecutionSpec이 없습니다.")
                spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
                step = next(
                    (
                        item
                        for item in spec.definition.validation_steps
                        if item.validation_id == observation.validation_id
                    ),
                    None,
                )
                if step is None or step.method != expected_method:
                    raise EngineServiceError("typed observation이 current ExecutionSpec과 다릅니다.")
                if (
                    isinstance(observation, ExternalValidationObservation)
                    and step.external_selector != observation.selector
                ):
                    raise EngineServiceError("외부 관측 selector가 ExecutionSpec과 다릅니다.")
            else:
                from .execution import execution_context
                from .validation_execution import GoalValidationBinding
                context = execution_context(self, project_id)
                rows = connection.execute(
                    "SELECT payload_json, created_at FROM history_events WHERE project_id = ? "
                    "AND event_type = 'goal_test.bound' ORDER BY sequence DESC", (project_id,),
                ).fetchall()
                matching = [(GoalValidationBinding.model_validate_json(row["payload_json"]), row)
                            for row in rows]
                bound_goal = next(((binding, row) for binding, row in matching
                                   if binding.plan_revision_id == plan_revision_id
                                   and binding.step.validation_id == observation.validation_id), None)
                if bound_goal is None:
                    raise EngineServiceError("typed Goal observation에 실행 binding이 없습니다.")
                binding, binding_row = bound_goal
                if (context["plan"]["plan_revision_id"] != plan_revision_id
                        or binding.plan_activation_digest != plan.activation_digest
                        or binding.context_digest != sha256_digest(context)):
                    raise EngineServiceError("STALE_EXECUTION_INPUT: typed Goal observation의 binding이 바뀌었습니다.")
                if binding.step.method != expected_method:
                    raise EngineServiceError("typed Goal observation의 method binding이 다릅니다.")
                if observation.observed_at < datetime.fromisoformat(binding_row["created_at"]):
                    raise EngineServiceError("Goal Test binding 이전 관측을 사용할 수 없습니다.")
                if (isinstance(observation, ExternalValidationObservation)
                        and binding.step.external_selector != observation.selector):
                    raise EngineServiceError("외부 관측 selector가 Goal Test binding과 다릅니다.")

        evidence_ids: list[str] = []
        observed_at = observation.observed_at
        for kind_name in contract.required_evidence_kinds:
            try:
                kind = EvidenceKind(kind_name)
            except ValueError as error:
                raise EngineServiceError(
                    f"지원하지 않는 validation evidence kind입니다: {kind_name}"
                ) from error
            evidence = EvidenceRecord(
                evidence_id=new_id("evidence"),
                project_id=project_id,
                task_id=observation.task_id,
                kind=kind,
                source_ref=(
                    observation.source_ref
                    if isinstance(observation, ManualValidationObservation)
                    else f"{observation.provider}:{observation.selector}"
                ),
                observation=observation.model_dump_json()[:10_000],
                content_digest=sha256_digest(
                    {"kind": kind.value, "typed_observation": observation}
                ),
                observed_at=observed_at,
            )
            self.record_evidence(evidence)
            evidence_ids.append(evidence.evidence_id)
        passed = observation.passed
        result = ValidationResult(
            validation_result_id=new_id("validation_result"),
            validation_id=observation.validation_id,
            task_id=observation.task_id,
            status=(
                ValidationStatus.INCONCLUSIVE
                if passed is None
                else (ValidationStatus.PASS if passed else ValidationStatus.FAIL)
            ),
            evidence_ids=tuple(evidence_ids),
            rationale=(
                observation.observation
                if isinstance(observation, ManualValidationObservation)
                else f"{observation.provider} 관측: {observation.observation}"
            ),
            evaluated_at=observed_at,
        )
        self.record_validation(
            project_id=project_id,
            plan_revision_id=plan_revision_id,
            result=result,
        )
        return result

    def block_task_from_validation(self, *, task_id: str, detail: str) -> None:
        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (task_id,))
            if task["status"] != TaskRuntimeStatus.VALIDATING.value:
                raise EngineServiceError("validating Task만 validation 실패로 차단할 수 있습니다.")
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'blocked', updated_at = ? WHERE id = ?",
                (tx.now, task_id),
            )
            tx.history(
                task["project_id"],
                "task.validation_blocked",
                "task_contract",
                task_id,
                {"detail": detail},
            )

    def complete_task(self, task_id: str) -> tuple[str, ...]:
        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (task_id,))
            if task["status"] != "validating":
                raise EngineServiceError("validating Task만 완료 판정할 수 있습니다.")
            contract = json.loads(task["payload_json"])
            expected = {item["validation_id"] for item in contract["validations"]}
            latest: dict[str, str] = {}
            for row in self.effective_task_validation_results(tx.connection, task_id):
                latest[row["validation_id"]] = row["status"]
            if set(latest) != expected or any(latest[item] != "pass" for item in expected):
                raise EngineServiceError("모든 Task validation의 최신 결과가 PASS여야 합니다.")
            now = tx.now
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'completed', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            ready = self._refresh_ready(tx, task["plan_revision_id"])
            from .plan_reuse import observation_checkpoint
            tx.history(
                task["project_id"],
                "task.completed",
                "task_contract",
                task_id,
                {"new_ready_task_ids": ready, "reuse_checkpoint": observation_checkpoint(self, tx.connection, task_id)},
            )
            return ready

    def record_goal_verdict(self, *, project_id: str, plan_revision_id: str, verdict: GoalVerdict) -> None:
        with self.ledger.transaction() as tx:
            plan_row = tx.one(
                "SELECT * FROM plan_revisions WHERE id = ? AND project_id = ?",
                (plan_revision_id, project_id),
            )
            plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
            if verdict.goal_contract_digest != plan.definition.goal_contract_digest:
                raise EngineServiceError("GoalVerdict가 Plan의 GoalContract와 다릅니다.")
            if verdict.plan_activation_digest != plan.activation_digest:
                raise EngineServiceError("GoalVerdict가 활성화된 Plan digest와 다릅니다.")
            expected_criteria = {item.criterion_id for item in plan.definition.goal_coverage}
            actual_criteria = {item.criterion_id for item in verdict.criteria}
            if expected_criteria != actual_criteria:
                raise EngineServiceError("GoalVerdict criterion coverage가 정확하지 않습니다.")
            if verdict.status is GoalVerdictStatus.SATISFIED:
                incomplete = tx.connection.execute(
                    "SELECT COUNT(*) FROM task_contracts WHERE plan_revision_id = ? AND status <> 'completed'",
                    (plan_revision_id,),
                ).fetchone()[0]
                if incomplete:
                    raise EngineServiceError("모든 Task가 완료되기 전에는 Goal satisfied를 선언할 수 없습니다.")
                expected_integrations = {
                    item.validation_id for item in plan.definition.integration_validations
                }
                passed: set[str] = set()
                result_ids: set[str] = set()
                for row in tx.all(
                    "SELECT id, validation_id, status FROM validation_results "
                    "WHERE plan_revision_id = ? AND task_id IS NULL ORDER BY evaluated_at, rowid",
                    (plan_revision_id,),
                ):
                    if row["status"] == "pass":
                        passed.add(row["validation_id"])
                        result_ids.add(row["id"])
                if not expected_integrations.issubset(passed):
                    raise EngineServiceError("plan-level integration/Goal Test PASS evidence가 부족합니다.")
                if not set(verdict.integration_validation_result_ids).issubset(result_ids):
                    raise EngineServiceError("GoalVerdict가 실제 PASS integration 결과에 결속되지 않았습니다.")
                evidence_ids = {item for criterion in verdict.criteria for item in criterion.evidence_ids}
                if evidence_ids:
                    placeholders = ",".join("?" for _ in evidence_ids)
                    found = tx.connection.execute(
                        f"SELECT COUNT(*) FROM evidence_records WHERE project_id = ? AND id IN ({placeholders})",
                        (project_id, *tuple(evidence_ids)),
                    ).fetchone()[0]
                    if found != len(evidence_ids):
                        raise EngineServiceError("GoalVerdict evidence 일부가 원장에 없습니다.")
            tx.connection.execute(
                "INSERT INTO goal_verdicts "
                "(id, project_id, plan_revision_id, goal_contract_digest, status, payload_json, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    verdict.goal_verdict_id,
                    project_id,
                    plan_revision_id,
                    verdict.goal_contract_digest,
                    verdict.status.value,
                    canonical_json(verdict),
                    verdict.evaluated_at.isoformat(),
                ),
            )
            if verdict.status is GoalVerdictStatus.SATISFIED:
                now = tx.now
                tx.connection.execute(
                    "UPDATE plan_revisions SET status = 'completed', completed_at = ? WHERE id = ?",
                    (now, plan_revision_id),
                )
                tx.connection.execute(
                    "UPDATE projects SET active_plan_revision_id = NULL, run_state = 'completed', "
                    "updated_at = ? WHERE id = ?",
                    (now, project_id),
                )
            tx.history(
                project_id,
                "goal.evaluated",
                "goal_verdict",
                verdict.goal_verdict_id,
                {"status": verdict.status.value, "plan_revision_id": plan_revision_id},
            )

    def record_worker_usage(
        self, *, attempt_id: str, thread_id: str, turn_id: str,
        terminal_status: str, provider_payload: dict[str, Any],
        output_digest: str | None = None,
    ) -> BudgetUsageRecord | None:
        """새 Worker turn의 관측만 기록하며 Task/Attempt 상태는 전이하지 않는다."""
        if terminal_status not in PROVIDER_TERMINAL_STATUSES:
            raise EngineServiceError("종료를 관측한 Worker turn만 usage를 기록합니다.")
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if attempt["kind"] != AttemptKind.EXECUTION.value:
                return None
            matches = []
            for item in tx.all(
                "SELECT i.*, r.payload_json AS receipt_json FROM runtime_intents i "
                "JOIN runtime_receipts r ON r.intent_id = i.id "
                "WHERE i.attempt_id = ? AND i.kind = 'start_turn' AND i.status = 'received'",
                (attempt_id,),
            ):
                receipt = RuntimeReceipt.model_validate_json(item["receipt_json"])
                if receipt.binding and (receipt.binding.thread_id, receipt.binding.turn_id) == (thread_id, turn_id):
                    matches.append((item, receipt))
            if len(matches) != 1:
                raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: provider turn의 receipt가 유일하지 않습니다.")
            intent, receipt = matches[0]
            request = json.loads(intent["request_json"])
            # 기존 실행은 소급 계측하거나 새 schema로 다시 저장하지 않는다.
            if request.get("worker_usage_contract") != 1:
                return None
            if sha256_digest(request) != intent["request_digest"]:
                raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: intent digest가 다릅니다.")
            start = receipt.response_payload
            if not isinstance(start, dict) or (
                start.get("thread_id") != thread_id or start.get("turn_id") != turn_id
                or start.get("prompt_digest") != request.get("prompt_digest")
                or receipt.provider_operation_id != turn_id
                or start.get("permission_profile") != ":danger-full-access"
                or start.get("approval_policy") != "never"
            ):
                raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: 실제 전송 Prompt/turn이 다릅니다.")
            if any(provider_payload.get(key, expected) != expected for key, expected in (
                ("thread_id", thread_id), ("turn_id", turn_id), ("prompt_digest", request["prompt_digest"]),
            )):
                raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: 완료 관측이 다른 전송을 가리킵니다.")
            spec_row = tx.one(
                "SELECT payload_json FROM execution_spec_revisions WHERE task_id = ? AND definition_digest = ?",
                (attempt["task_id"], attempt["execution_spec_digest"]),
            )
            spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
            if (request.get("execution_spec_digest") != spec.definition_digest or
                    request.get("prompt_binding_digest") != spec.definition.context_manifest.prompt_binding.binding_digest or
                    request.get("model") != spec.definition.executor.model or
                    request.get("effort") != spec.definition.executor.effort):
                raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: ExecutionSpec/Prompt binding이 다릅니다.")
            plan = PlanContractRevision.model_validate_json(tx.one(
                "SELECT payload_json FROM plan_revisions WHERE id = ?", (attempt["plan_revision_id"],),
            )["payload_json"])
            call_ref = "worker-turn:" + sha256_digest({"thread_id": thread_id, "turn_id": turn_id})
            existing_row = tx.maybe_one(
                "SELECT payload_json FROM budget_usage WHERE project_id = ? AND goal_contract_digest = ? AND logical_call_ref = ?",
                (attempt["project_id"], plan.definition.goal_contract_digest, call_ref),
            )
            existing = None if existing_row is None else BudgetUsageRecord.model_validate_json(existing_row["payload_json"])
            if existing is not None and (existing.attempt_id != attempt_id or existing.runtime_intent_id != intent["id"]):
                raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: 이미 다른 Attempt에 귀속된 turn입니다.")
            raw = provider_payload.get("usage")
            scope = provider_payload.get("usage_scope", "unavailable")
            first_empty_thread = False
            if start.get("first_empty_thread") is True:
                for created in tx.all(
                    "SELECT r.payload_json FROM runtime_receipts r JOIN runtime_intents i ON i.id = r.intent_id "
                    "WHERE i.attempt_id = ? AND i.kind = 'create_thread'", (attempt_id,),
                ):
                    creation = RuntimeReceipt.model_validate_json(created["payload_json"])
                    thread = (creation.response_payload or {}).get("thread", {})
                    if (creation.provider_operation_id == thread_id and creation.binding and creation.binding.thread_id == thread_id and
                            isinstance(thread, dict) and thread.get("id") == thread_id and thread.get("turns") == []):
                        first_empty_thread = True
                if not first_empty_thread:
                    raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: 빈 thread 생성 receipt가 없습니다.")
                prior_turn = tx.one(
                    "SELECT i.id FROM runtime_intents i JOIN runtime_receipts r ON r.intent_id = i.id "
                    "WHERE i.attempt_id = ? AND i.kind = 'start_turn' "
                    "AND json_extract(r.binding_json, '$.thread_id') = ? ORDER BY i.rowid LIMIT 1",
                    (attempt_id, thread_id),
                )
                if prior_turn["id"] != intent["id"]:
                    raise EngineServiceError("WORKER_USAGE_BINDING_MISMATCH: 첫 turn이 아닌 누적 usage입니다.")
            counts = None
            basis = "unavailable"
            reason = "PROVIDER_USAGE_UNAVAILABLE"
            if isinstance(raw, dict) and scope == "turn":
                counts, basis = raw, "provider_turn"
            elif isinstance(raw, dict) and scope == "thread":
                # raw total은 복수 turn에 배분하지 않는다. 시작 전 빈 thread임을
                # 실제 adapter receipt가 확인한 첫 turn만 전체 raw total에 귀속한다.
                if first_empty_thread:
                    counts, basis = raw.get("total"), "first_empty_thread"
                else:
                    reason = "CUMULATIVE_USAGE_NOT_ATTRIBUTABLE_TO_TURN"
            elif raw is not None:
                reason = "PROVIDER_USAGE_SCOPE_UNDETERMINED"
            keys = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
            values = tuple(counts.get(key) for key in keys) if isinstance(counts, dict) else (None,) * 4
            valid = all(type(value) is int and value >= 0 for value in values)
            if valid:
                valid = values[1] <= values[0] and values[3] <= values[2]
                if "totalTokens" in counts:
                    valid = valid and type(counts["totalTokens"]) is int and counts["totalTokens"] == values[0] + values[2]
            if basis != "unavailable" and not valid:
                reason = "PROVIDER_USAGE_FIELDS_INCOMPLETE_OR_INVALID"
            available = basis != "unavailable" and valid
            if not available:
                values, basis = (None,) * 4, "unavailable"
            if existing is not None:
                if raw is not None and available and (
                    not existing.usage_available or values != (
                        existing.input_tokens, existing.cached_input_tokens, existing.output_tokens, existing.reasoning_tokens,
                    )
                ):
                    raise EngineServiceError("WORKER_USAGE_CONFLICT: 같은 turn의 기존 usage를 변경하지 않습니다.")
                return existing
            duration = provider_payload.get("duration_ms")
            observation = {"thread_id": thread_id, "turn_id": turn_id, "terminal_status": terminal_status,
                           "output_digest": output_digest, "payload": provider_payload}
            usage = BudgetUsageRecord(
                usage_id=new_id("usage"), project_id=attempt["project_id"],
                goal_contract_digest=plan.definition.goal_contract_digest, stage=BudgetStage.EXECUTION,
                logical_call_ref=call_ref, role=spec.definition.executor.role, call_status=terminal_status,
                model=request["model"], effort=request["effort"],
                permission_profile=start["permission_profile"], approval_policy=start["approval_policy"],
                thread_id=thread_id, turn_ids=(turn_id,), input_digest=request["prompt_digest"],
                output_digest=output_digest, output_schema_digest=sha256_digest(None),
                runner_receipt_digest=sha256_digest(observation),
                input_tokens=values[0], cached_input_tokens=values[1], output_tokens=values[2], reasoning_tokens=values[3],
                latency_ms=duration if type(duration) is int and duration >= 0 else None,
                usage_available=available, attempt_id=attempt_id, runtime_intent_id=intent["id"],
                runtime_receipt_id=receipt.receipt_id, execution_spec_digest=spec.definition_digest,
                prompt_binding_digest=spec.definition.context_manifest.prompt_binding.binding_digest,
                prompt_token_estimate=request["prompt_token_estimate"],
                usage_scope=scope if scope in {"turn", "thread"} else "unavailable",
                usage_source=provider_payload.get("usage_source", "runtime.read"), attribution_basis=basis,
                unavailable_reason=None if available else reason, provider_observation=observation,
                recorded_at=utc_now(),
            )
            self._insert_budget_usage(tx, usage)
            return usage

    @staticmethod
    def _insert_budget_usage(tx: Any, usage: BudgetUsageRecord) -> str:
        existing = tx.maybe_one("SELECT payload_json FROM budget_usage WHERE project_id=? AND logical_call_ref=?",
                                (usage.project_id, usage.logical_call_ref))
        if existing is not None:
            prior = BudgetUsageRecord.model_validate_json(existing["payload_json"])
            if prior.model_dump(exclude={"usage_id", "recorded_at"}) != usage.model_dump(exclude={"usage_id", "recorded_at"}):
                raise EngineServiceError("BUDGET_RECEIPT_CONFLICT: 같은 호출에 다른 근거가 있습니다.")
            return prior.usage_id
        payload = usage.model_dump(mode="json") if usage.attempt_id is not None else usage
        tx.connection.execute(
            "INSERT INTO budget_usage (id, project_id, goal_contract_digest, stage, logical_call_ref, payload_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (usage.usage_id, usage.project_id, usage.goal_contract_digest, usage.stage.value,
             usage.logical_call_ref, canonical_json(payload), usage.recorded_at.isoformat()),
        )
        tx.history(usage.project_id, "budget.recorded", "budget_usage", usage.usage_id,
                   {"stage": usage.stage.value, "optimization_tokens": usage.optimization_tokens})
        if usage.attempt_id is not None:
            from .budget import settle_worker_in_transaction
            settle_worker_in_transaction(tx, usage)
        return usage.usage_id

    def rebind_model(self, *, request: Any, inventory: ModelInventory):
        """사용자의 명시적 envelope 내 선택을 새 Spec·Attempt에 결속한다."""
        from .model_rebinding import ModelRebindingService
        return ModelRebindingService(self.ledger, assignment_resolver=self.assignment_resolver).rebind_and_reserve(request, inventory)

    def record_budget_usage(self, usage: BudgetUsageRecord) -> None:
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (usage.project_id,))
            self._insert_budget_usage(tx, usage)

    def release_unstarted_attempt(self, attempt_id: str, reason: str) -> None:
        """외부 intent가 아직 없는 예산 거부만 해제하고 시도 이력을 보존한다."""
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
            if attempt["status"] != "reserved" or tx.maybe_one("SELECT id FROM runtime_intents WHERE attempt_id=?", (attempt_id,)):
                raise EngineServiceError("ATTEMPT_EFFECT_NOT_EMPTY: 외부 효과가 없는 예약만 해제할 수 있습니다.")
            tx.connection.execute("UPDATE attempts SET status='abandoned',ended_at=?,updated_at=? WHERE id=?",
                                  (tx.now, tx.now, attempt_id))
            tx.connection.execute("UPDATE task_contracts SET status=?,updated_at=? WHERE id=?",
                                  ("validating" if attempt["kind"] == "validation" else "materialized", tx.now, attempt["task_id"]))
            tx.history(attempt["project_id"], "attempt.released_before_effect", "attempt", attempt_id, {"reason": reason})

    def release_empty_created_thread_reservation(
        self,
        *,
        call_id: str,
        receipt: RuntimeReceipt,
        observation: "RuntimeObservation",
    ) -> None:
        """turn 0을 직접 관측한 복원 create receipt의 예약과 Attempt를 함께 닫는다."""

        from .model_lock import OperationalBinding, verify_binding
        from .runtime import RuntimeObservation

        receipt = RuntimeReceipt.model_validate(receipt)
        observation = RuntimeObservation.model_validate(observation)

        def reject(code: str, detail: str) -> None:
            raise EngineServiceError(f"{code}: {detail}")

        with self.ledger.transaction() as tx:
            call = tx.one("SELECT * FROM provider_calls WHERE id = ?", (call_id,))
            if (
                call["actual_tokens"] is not None
                or call["receipt_json"] is not None
                or call["usage_id"] is not None
            ):
                reject(
                    "EMPTY_THREAD_RESERVATION_HAS_USAGE",
                    "사용량 또는 역할 receipt가 기록된 provider 예약은 빈 thread로 해제할 수 없습니다.",
                )
            if call["attempt_id"] is None:
                reject(
                    "EMPTY_THREAD_ATTEMPT_BINDING_REQUIRED",
                    "Attempt에 결속된 provider 예약이 필요합니다.",
                )
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (call["attempt_id"],))
            calls = tx.all(
                "SELECT id FROM provider_calls WHERE attempt_id = ? ORDER BY rowid",
                (attempt["id"],),
            )
            if len(calls) != 1 or calls[0]["id"] != call_id:
                reject(
                    "EMPTY_THREAD_PROVIDER_CALL_CARDINALITY",
                    "빈 thread Attempt에는 정확히 하나의 provider 예약만 있어야 합니다.",
                )
            if call["project_id"] != attempt["project_id"]:
                reject(
                    "EMPTY_THREAD_PROJECT_BINDING_MISMATCH",
                    "provider 예약과 Attempt의 프로젝트가 다릅니다.",
                )

            intents = tx.all(
                "SELECT * FROM runtime_intents WHERE attempt_id = ? ORDER BY rowid",
                (attempt["id"],),
            )
            if (
                len(intents) != 1
                or intents[0]["kind"] != RuntimeIntentKind.CREATE_THREAD.value
                or intents[0]["status"] != RuntimeIntentStatus.RECEIVED.value
            ):
                reject(
                    "EMPTY_THREAD_RUNTIME_EFFECT_NOT_EMPTY",
                    "received create_thread 외의 start/resume/unknown runtime intent가 없어야 합니다.",
                )
            intent = intents[0]
            stored_receipts = tx.all(
                "SELECT * FROM runtime_receipts WHERE intent_id = ? ORDER BY rowid",
                (intent["id"],),
            )
            if len(stored_receipts) != 1:
                reject(
                    "EMPTY_THREAD_CREATE_RECEIPT_REQUIRED",
                    "복원된 create_thread receipt가 정확히 하나 필요합니다.",
                )
            stored_receipt = RuntimeReceipt.model_validate_json(
                stored_receipts[0]["payload_json"]
            )
            if receipt != stored_receipt or receipt.intent_id != intent["id"]:
                reject(
                    "EMPTY_THREAD_CREATE_RECEIPT_MISMATCH",
                    "입력 receipt가 원장에 복원된 create_thread receipt와 다릅니다.",
                )
            if receipt.binding is None or receipt.binding.turn_id is not None:
                reject(
                    "EMPTY_THREAD_CREATE_BINDING_MISMATCH",
                    "create_thread receipt에는 turn 없는 thread binding이 필요합니다.",
                )
            if (
                stored_receipts[0]["provider_operation_id"]
                != receipt.provider_operation_id
                or stored_receipts[0]["response_digest"] != receipt.response_digest
                or stored_receipts[0]["binding_json"] != canonical_json(receipt.binding)
                or attempt["binding_json"] != canonical_json(receipt.binding)
            ):
                reject(
                    "EMPTY_THREAD_CREATE_BINDING_MISMATCH",
                    "원장 receipt·Attempt binding이 입력 receipt와 다릅니다.",
                )

            request = json.loads(call["request_json"])
            intent_request = json.loads(intent["request_json"])
            if (
                request != intent_request
                or sha256_digest(request) != call["request_digest"]
                or sha256_digest(intent_request) != intent["request_digest"]
                or call["request_digest"] != intent["request_digest"]
                or request.get("task_id") != attempt["task_id"]
                or request.get("attempt_kind") != attempt["kind"]
            ):
                reject(
                    "EMPTY_THREAD_REQUEST_BINDING_MISMATCH",
                    "provider 예약과 create intent의 Task·Attempt 요청 결속이 다릅니다.",
                )
            project = tx.one(
                "SELECT root FROM projects WHERE id = ?", (attempt["project_id"],)
            )
            if Path(str(request.get("cwd", ""))).resolve() != Path(project["root"]).resolve():
                reject(
                    "EMPTY_THREAD_REQUEST_BINDING_MISMATCH",
                    "create 요청의 작업 경로가 프로젝트와 다릅니다.",
                )
            spec_row = tx.one(
                "SELECT payload_json FROM execution_spec_revisions "
                "WHERE definition_digest = ?",
                (attempt["execution_spec_digest"],),
            )
            spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
            expected_role = (
                spec.definition.executor
                if attempt["kind"] == AttemptKind.EXECUTION.value
                else spec.definition.validator
            )
            expected_call_role = (
                "worker"
                if attempt["kind"] == AttemptKind.EXECUTION.value
                else "semantic_validator"
            )
            expected_stage = (
                BudgetStage.EXECUTION.value
                if attempt["kind"] == AttemptKind.EXECUTION.value
                else BudgetStage.VALIDATION.value
            )
            if (
                expected_role is None
                or expected_role.operational_binding is None
                or call["role"] != expected_call_role
                or call["stage"] != expected_stage
                or request.get("model") != expected_role.model
            ):
                reject(
                    "EMPTY_THREAD_ROLE_BINDING_MISMATCH",
                    "Attempt 종류·Execution Spec·provider role/stage/model 결속이 다릅니다.",
                )
            try:
                observed_binding = OperationalBinding.model_validate(
                    request.get("model_observation")
                )
                verify_binding(
                    expected_role.operational_binding,
                    observed_binding.inventory,
                    role=expected_role.role,
                    model=expected_role.model,
                    effort=expected_role.effort,
                )
                if observed_binding.lock_digest != expected_role.operational_binding.lock_digest:
                    raise ValueError("operational lock digest mismatch")
            except ValueError as error:
                reject(
                    "EMPTY_THREAD_MODEL_BINDING_MISMATCH",
                    f"create 요청의 model observation이 Execution Spec과 다릅니다: {error}",
                )

            plan = tx.one(
                "SELECT payload_json FROM plan_revisions WHERE id = ?",
                (attempt["plan_revision_id"],),
            )
            goal_digest = json.loads(plan["payload_json"])["definition"][
                "goal_contract_digest"
            ]
            goal = tx.one(
                "SELECT goal_id FROM goal_revisions WHERE project_id = ? "
                "AND definition_digest = ?",
                (attempt["project_id"], goal_digest),
            )
            if (
                call["goal_id"] != goal["goal_id"]
                or call["goal_contract_digest"] != goal_digest
            ):
                reject(
                    "EMPTY_THREAD_GOAL_BINDING_MISMATCH",
                    "provider 예약이 Attempt의 Plan·Goal revision과 다릅니다.",
                )
            if call["policy_digest"] is None:
                if call["estimated_tokens"] != 0:
                    reject(
                        "EMPTY_THREAD_POLICY_BINDING_MISMATCH",
                        "정책 없는 provider 예약의 token 예약량이 0이 아닙니다.",
                    )
            else:
                policies = tx.all(
                    "SELECT payload_json FROM budget_policy_revisions "
                    "WHERE project_id = ? AND scope_key IN ('', ?)",
                    (attempt["project_id"], goal["goal_id"]),
                )
                matching_policy = next(
                    (
                        json.loads(item["payload_json"])
                        for item in policies
                        if sha256_digest(json.loads(item["payload_json"]))
                        == call["policy_digest"]
                    ),
                    None,
                )
                if (
                    matching_policy is None
                    or matching_policy.get("call_reservation_tokens")
                    != call["estimated_tokens"]
                ):
                    reject(
                        "EMPTY_THREAD_POLICY_BINDING_MISMATCH",
                        "provider 예약량과 명시 예산 정책의 결속이 다릅니다.",
                    )

            response = receipt.response_payload
            thread = None if not isinstance(response, dict) else response.get("thread")
            if (
                receipt.provider_operation_id != receipt.binding.thread_id
                or not isinstance(thread, dict)
                or thread.get("id") != receipt.binding.thread_id
                or thread.get("turns") != []
                or (
                    "thread_id" in response
                    and response.get("thread_id") != receipt.binding.thread_id
                )
            ):
                reject(
                    "EMPTY_THREAD_CREATE_RESPONSE_NOT_EMPTY",
                    "create receipt 원문이 turn 0 thread를 증명하지 않습니다.",
                )
            observation_payload = observation.payload
            evidence_type: str | None = None
            if (
                observation.thread_id != receipt.binding.thread_id
                or observation.turn_id is not None
                or observation.active
                or observation.terminal_status is not None
                or observation.final_response is not None
                or observation_payload.get("thread_id") != receipt.binding.thread_id
                or isinstance(observation_payload.get("turn_count"), bool)
                or not isinstance(observation_payload.get("turn_count"), int)
                or observation_payload["turn_count"] != 0
                or observation_payload.get("turn_status") is not None
                or observation_payload.get("usage") is not None
            ):
                reject(
                    "EMPTY_THREAD_TERMINAL_OBSERVATION_MISMATCH",
                    "저장 thread의 직접 read 관측이 turn 0·비활성·사용량 없음 상태가 아닙니다.",
                )
            if (
                observation_payload.get("turn_history_available") is True
                and observation_payload.get("turn_history_source")
                == "thread/read(includeTurns=true)"
            ):
                if (
                    observation_payload.get("usage_source") != "thread/read"
                    or observation_payload.get("turn_history_error") is not None
                ):
                    reject(
                        "EMPTY_THREAD_HISTORY_EVIDENCE_MISMATCH",
                        "typed thread/read turn history 증거가 다릅니다.",
                    )
                evidence_type = "typed_thread_read_include_turns"
            elif (
                observation_payload.get("turn_history_available") is True
                and observation_payload.get("turn_history_source") == "thread/turns/list"
            ):
                verification = observation_payload.get("project_binding_verification")
                retry_errors = observation_payload.get("read_retry_errors")
                expected_params = {
                    "threadId": receipt.binding.thread_id,
                    "limit": 1,
                    "sortDirection": "asc",
                    "itemsView": "full",
                }
                expected_response = {
                    "data": [],
                    "nextCursor": None,
                    "backwardsCursor": None,
                }
                expected_materialization_params = {
                    "threadId": receipt.binding.thread_id,
                    "includeTurns": True,
                }
                materialization = observation_payload.get("materialization_read")
                receipt_cwd = thread.get("cwd")
                receipt_path = thread.get("path")
                if (
                    observation_payload.get("usage_source") != "thread/turns/list"
                    or observation_payload.get("turn_history_error") is not None
                    or observation_payload.get("turn_history_params") != expected_params
                    or observation_payload.get("turn_history_response") != expected_response
                    or retry_errors != []
                    or observation_payload.get("turn_history_connection")
                    != "independent_app_server"
                    or observation_payload.get("independent_reader_executable_digest")
                    != observed_binding.inventory.executable_digest
                    or not isinstance(materialization, dict)
                    or materialization.get("method") != "thread/read"
                    or materialization.get("params") != expected_materialization_params
                    or (
                        materialization.get("response") is None
                        and materialization.get("error")
                        != {
                            "type": "MethodNotFoundError",
                            "code": -32601,
                            "message": "list_turns is not supported yet",
                        }
                    )
                    or (
                        materialization.get("response") is not None
                        and (
                            materialization.get("error") is not None
                            or not isinstance(materialization["response"], dict)
                            or not isinstance(
                                materialization["response"].get("thread"), dict
                            )
                            or materialization["response"].get("thread", {}).get("id")
                            != receipt.binding.thread_id
                            or materialization["response"].get("thread", {}).get("turns")
                            != []
                        )
                    )
                    or not isinstance(verification, dict)
                    or verification.get("source")
                    != "same_connection_thread_start_receipt_and_empty_turns_list"
                    or verification.get("creation_receipt_digest")
                    != sha256_digest(receipt.response_payload)
                    or verification.get("creation_project_id") != thread.get("projectId")
                    or verification.get("raw_metadata_source")
                    != "thread/read(includeTurns=false)"
                    or verification.get("raw_metadata_connection")
                    != "independent_app_server"
                    or verification.get("raw_metadata_confirmation_count") != 2
                    or not isinstance(receipt_cwd, str)
                    or not Path(receipt_cwd).is_absolute()
                    or Path(receipt_cwd).resolve() != Path(request["cwd"]).resolve()
                    or not isinstance(receipt_path, str)
                    or not Path(receipt_path).is_absolute()
                    or verification.get("rollout_path") != receipt_path
                    or thread.get("ephemeral") is not False
                    or not isinstance(thread.get("projectId"), str)
                    or not thread["projectId"]
                ):
                    reject(
                        "EMPTY_THREAD_TURNS_LIST_EVIDENCE_MISMATCH",
                        "same-connection 빈 thread turns/list 증거가 원본 create receipt와 다릅니다.",
                    )
                evidence_type = "same_connection_thread_turns_list_empty"
            else:
                reject(
                    "EMPTY_THREAD_HISTORY_EVIDENCE_MISMATCH",
                    "turn history 사용 가능 여부가 명시되어야 합니다.",
                )

            proof = {
                "attempt_id": attempt["id"],
                "intent_id": intent["id"],
                "request_digest": call["request_digest"],
                "runtime_receipt_digest": sha256_digest(receipt),
                "observation_digest": sha256_digest(observation),
                "observation": observation.model_dump(mode="json"),
                "thread_id": receipt.binding.thread_id,
                "observed_turn_count": 0,
                "actual_tokens": None,
                "turn_zero_evidence_type": evidence_type,
            }
            prior = tx.all(
                "SELECT payload_json FROM history_events WHERE project_id = ? "
                "AND event_type = 'budget.empty_thread_reservation_released' "
                "AND entity_type = 'provider_call' AND entity_id = ? ORDER BY sequence",
                (attempt["project_id"], call_id),
            )
            if call["status"] == "released":
                attempt_history = tx.all(
                    "SELECT payload_json FROM history_events WHERE project_id = ? "
                    "AND event_type = 'attempt.failed' AND entity_type = 'attempt' "
                    "AND entity_id = ? ORDER BY sequence",
                    (attempt["project_id"], attempt["id"]),
                )
                task = tx.one(
                    "SELECT status FROM task_contracts WHERE id = ?", (attempt["task_id"],)
                )
                if (
                    len(prior) == 1
                    and json.loads(prior[0]["payload_json"]) == proof
                    and attempt["status"] == AttemptStatus.FAILED.value
                    and attempt["failure_class"] == FailureClass.EXTERNAL_UNKNOWN.value
                    and task["status"] == TaskRuntimeStatus.FAILED.value
                    and len(attempt_history) == 1
                    and json.loads(attempt_history[0]["payload_json"])
                    == {
                        "task_id": attempt["task_id"],
                        "failure_class": FailureClass.EXTERNAL_UNKNOWN.value,
                    }
                ):
                    return
                reject(
                    "EMPTY_THREAD_RELEASE_PROOF_MISMATCH",
                    "기존 해제 이력과 입력 receipt·관측이 다릅니다.",
                )
            if call["status"] != "reserved" or prior:
                reject(
                    "EMPTY_THREAD_RESERVATION_RELEASE_NOT_ALLOWED",
                    "현재 reserved 예약만 검증된 빈 thread 증거로 해제할 수 있습니다.",
                )
            if attempt["status"] != AttemptStatus.RUNNING.value:
                reject(
                    "EMPTY_THREAD_ATTEMPT_NOT_RUNNING",
                    "복원된 create receipt로 running 상태가 된 Attempt만 해제할 수 있습니다.",
                )
            tx.connection.execute(
                "UPDATE provider_calls SET status='released',execution_status='released',"
                "effect_status='none',result_status='invalid',completed_at=? WHERE id=?",
                (tx.now, call_id),
            )
            tx.history(
                attempt["project_id"],
                "budget.empty_thread_reservation_released",
                "provider_call",
                call_id,
                proof,
            )
            detail = (
                "생성 receipt를 복구했고 turn 0 evidence를 기록했습니다: "
                f"{evidence_type}. 모델 turn은 시작되지 않았습니다."
            )
            tx.connection.execute(
                "UPDATE attempts SET status = 'failed', failure_class = 'external_unknown', "
                "failure_detail = ?, ended_at = ?, updated_at = ? WHERE id = ?",
                (detail, tx.now, tx.now, attempt["id"]),
            )
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'failed', updated_at = ? WHERE id = ?",
                (tx.now, attempt["task_id"]),
            )
            tx.history(
                attempt["project_id"],
                "attempt.failed",
                "attempt",
                attempt["id"],
                {
                    "task_id": attempt["task_id"],
                    "failure_class": FailureClass.EXTERNAL_UNKNOWN.value,
                },
            )

    def recover_inspect(self, project_id: str) -> tuple[str, ...]:
        unknown: list[str] = []
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (project_id,))
            rows = tx.all(
                "SELECT i.id, i.attempt_id, a.task_id FROM runtime_intents i "
                "JOIN attempts a ON a.id = i.attempt_id "
                "WHERE a.project_id = ? AND i.status = 'prepared' ORDER BY i.prepared_at",
                (project_id,),
            )
            for row in rows:
                tx.connection.execute(
                    "UPDATE runtime_intents SET status = 'unknown', updated_at = ? WHERE id = ?",
                    (tx.now, row["id"]),
                )
                tx.connection.execute(
                    "UPDATE attempts SET status = 'unknown', failure_class = 'external_unknown', "
                    "failure_detail = ?, ended_at = ?, updated_at = ? WHERE id = ?",
                    ("프로세스 중단 시점에 runtime receipt가 없습니다.", tx.now, tx.now, row["attempt_id"]),
                )
                tx.connection.execute(
                    "UPDATE task_contracts SET status = 'blocked', updated_at = ? WHERE id = ?",
                    (tx.now, row["task_id"]),
                )
                tx.history(
                    project_id,
                    "runtime.intent_unknown",
                    "runtime_intent",
                    row["id"],
                    {"attempt_id": row["attempt_id"], "reason": "MISSING_RECEIPT_AFTER_RESTART"},
                )
                unknown.append(row["id"])
            if unknown:
                tx.connection.execute(
                    "UPDATE projects SET run_state = 'recovery_required', recovery_reason = ?, "
                    "updated_at = ? WHERE id = ?",
                    ("EXTERNAL_EFFECT_UNKNOWN", tx.now, project_id),
                )
        return tuple(unknown)

    def abandon_unknown_intent(self, *, intent_id: str, rationale: str) -> None:
        with self.ledger.transaction() as tx:
            intent = tx.one("SELECT * FROM runtime_intents WHERE id = ?", (intent_id,))
            if intent["status"] != "unknown":
                raise EngineServiceError("unknown intent만 abandon할 수 있습니다.")
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (intent["attempt_id"],))
            tx.connection.execute(
                "UPDATE runtime_intents SET status = 'abandoned', updated_at = ? WHERE id = ?",
                (tx.now, intent_id),
            )
            tx.connection.execute(
                "UPDATE attempts SET status = 'abandoned', failure_detail = ?, ended_at = ?, updated_at = ? "
                "WHERE id = ?",
                (rationale, tx.now, tx.now, attempt["id"]),
            )
            remaining = tx.connection.execute(
                "SELECT COUNT(*) FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                "WHERE a.project_id = ? AND i.status = 'unknown'",
                (attempt["project_id"],),
            ).fetchone()[0]
            if remaining == 0:
                tx.connection.execute(
                    "UPDATE projects SET run_state = 'active', recovery_reason = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (tx.now, attempt["project_id"]),
                )
            tx.history(
                attempt["project_id"],
                "runtime.intent_abandoned",
                "runtime_intent",
                intent_id,
                {"rationale": rationale},
            )

    def record_recovery_assessment(self, project_id: str, assessment: RecoveryAssessment) -> None:
        with self.ledger.transaction() as tx:
            self._record_recovery_assessment_in_transaction(tx, project_id, assessment)

    def _record_recovery_assessment_in_transaction(
        self, tx: Any, project_id: str, assessment: RecoveryAssessment
    ) -> None:
        expected_action = self.repair_action_for(assessment.failure_class)
        if assessment.action is not expected_action:
            raise EngineServiceError(
                "failure_class에 대한 repair action이 Core 매핑과 다릅니다: "
                f"expected={expected_action.value}, actual={assessment.action.value}"
            )
        attempt = tx.one("SELECT project_id FROM attempts WHERE id = ?", (assessment.attempt_id,))
        if attempt["project_id"] != project_id:
            raise EngineServiceError("RecoveryAssessment가 다른 프로젝트 Attempt를 참조합니다.")
        existing_assessments = tx.all(
            "SELECT payload_json, created_at FROM recovery_assessments WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        )
        existing_payloads = [json.loads(row["payload_json"]) for row in existing_assessments]
        same_existing = sum(
            1
            for payload in existing_payloads
            if payload["action"] == RepairAction.SUBGRAPH_REPLAN.value
            and (
                payload.get("failure_fingerprint") == assessment.failure_fingerprint
                if assessment.failure_fingerprint is not None
                else payload["failure_class"] == assessment.failure_class.value
            )
        )
        goal_actions = (
            "('subgraph_replan','goal_revision')"
            if assessment.failure_fingerprint is not None
            else "('goal_revision')"
        )
        goal_existing = tx.connection.execute(
            "SELECT COUNT(*) FROM recovery_assessments WHERE project_id = ? "
            f"AND action IN {goal_actions}",
            (project_id,),
        ).fetchone()[0]
        expected_same = int(same_existing) + (
            1 if assessment.action is RepairAction.SUBGRAPH_REPLAN else 0
        )
        expected_goal = int(goal_existing) + (
            1
            if assessment.action is RepairAction.GOAL_REVISION
            or (
                assessment.failure_fingerprint is not None
                and assessment.action is RepairAction.SUBGRAPH_REPLAN
            )
            else 0
        )
        if assessment.same_failure_replan_count != expected_same:
            raise EngineServiceError(
                f"same_failure_replan_count는 원장에서 계산한 {expected_same}여야 합니다."
            )
        if assessment.goal_replan_count != expected_goal:
            raise EngineServiceError(
                f"goal_replan_count는 원장에서 계산한 {expected_goal}여야 합니다."
            )
        authorization = tx.connection.execute(
            "SELECT payload_json FROM goal_authorizations WHERE project_id=? "
            "ORDER BY revision_no DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        operating = (
            {"max_same_failure_replans": 2, "max_goal_replans": 5,
             "requires_new_evidence": True}
            if authorization is None
            else json.loads(authorization["payload_json"])["operating_policy"]
        )
        if assessment.same_failure_replan_count > int(operating["max_same_failure_replans"]):
            raise EngineServiceError(
                f"동일 실패 재계획 한도 {operating['max_same_failure_replans']}회를 넘었습니다."
            )
        if assessment.goal_replan_count > int(operating["max_goal_replans"]):
            raise EngineServiceError(
                f"Goal 전체 재계획 한도 {operating['max_goal_replans']}회를 넘었습니다."
            )
        if (
            operating.get("requires_new_evidence", True)
            and goal_existing > 0
            and assessment.action in {RepairAction.SUBGRAPH_REPLAN, RepairAction.GOAL_REVISION}
            and not assessment.new_evidence_ids
        ):
            raise EngineServiceError("첫 재계획 이후에는 새 evidence가 필요합니다.")
        prior_ids = {evidence_id for row in existing_assessments
                     for evidence_id in json.loads(row["payload_json"])["new_evidence_ids"]}
        for evidence_id in assessment.new_evidence_ids:
            evidence = tx.connection.execute(
                "SELECT project_id, observed_at FROM evidence_records WHERE id = ?", (evidence_id,),
            ).fetchone()
            if evidence is None or evidence["project_id"] != project_id:
                raise EngineServiceError("새 recovery evidence가 해당 프로젝트 원장에 없습니다.")
            if evidence_id in prior_ids:
                raise EngineServiceError("이전 recovery에서 사용한 evidence는 새 evidence가 아닙니다.")
            if existing_assessments:
                evidence_event = tx.connection.execute(
                    "SELECT sequence FROM history_events WHERE project_id = ? AND event_type = 'evidence.recorded' "
                    "AND entity_id = ?", (project_id, evidence_id),
                ).fetchone()
                previous_event = tx.connection.execute(
                    "SELECT MAX(sequence) FROM history_events WHERE project_id = ? AND event_type = 'recovery.assessed'",
                    (project_id,),
                ).fetchone()[0]
                if evidence_event is None or evidence_event["sequence"] <= previous_event:
                    raise EngineServiceError("이전 recovery 이후의 새 evidence 관측이 필요합니다.")
        tx.connection.execute(
            "INSERT INTO recovery_assessments "
            "(id, project_id, attempt_id, failure_class, action, same_failure_replan_count, "
            "goal_replan_count, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assessment.assessment_id,
                project_id,
                assessment.attempt_id,
                assessment.failure_class.value,
                assessment.action.value,
                assessment.same_failure_replan_count,
                assessment.goal_replan_count,
                canonical_json(assessment),
                tx.now,
            ),
        )
        tx.history(
            project_id,
            "recovery.assessed",
            "recovery_assessment",
            assessment.assessment_id,
            {"attempt_id": assessment.attempt_id, "action": assessment.action.value},
        )

    @staticmethod
    def repair_action_for(failure_class: FailureClass) -> RepairAction:
        return {
            FailureClass.UNCLASSIFIED: RepairAction.ABANDON,
            FailureClass.IMPLEMENTATION: RepairAction.TASK_REPAIR,
            FailureClass.CONTEXT: RepairAction.EXECUTION_SPEC_REVISION,
            FailureClass.TASK_CONTRACT: RepairAction.SUBGRAPH_REPLAN,
            FailureClass.DEPENDENCY: RepairAction.SUBGRAPH_REPLAN,
            FailureClass.ENVIRONMENT: RepairAction.CONTINUE,
            FailureClass.REQUIREMENT_CHANGE: RepairAction.GOAL_REVISION,
            FailureClass.EXTERNAL_UNKNOWN: RepairAction.WAIT_EXTERNAL,
        }[failure_class]

    def status(self, project_id: str) -> dict[str, Any]:
        return self.ledger.project_snapshot(project_id)

    def load_active_goal(self, project_id: str) -> GoalContractRevision:
        with self.ledger.read() as connection:
            row = connection.execute(
                "SELECT g.payload_json FROM goal_revisions g JOIN projects p "
                "ON p.active_goal_revision_id = g.id WHERE p.id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise EngineServiceError("active GoalContract가 없습니다.")
        return GoalContractRevision.model_validate_json(row["payload_json"])

    def load_active_profile(self, project_id: str) -> ProjectProfileRevision:
        with self.ledger.read() as connection:
            row = connection.execute(
                "SELECT r.payload_json FROM profile_revisions r JOIN projects p "
                "ON p.active_profile_revision_id = r.id WHERE p.id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise EngineServiceError("active ProjectProfile이 없습니다.")
        return ProjectProfileRevision.model_validate_json(row["payload_json"])

    def load_latest_goal(self, project_id: str) -> GoalContractRevision:
        with self.ledger.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM goal_revisions WHERE project_id = ? "
                "ORDER BY revision_no DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if row is None:
            raise EngineServiceError("GoalContract가 없습니다.")
        return GoalContractRevision.model_validate_json(row["payload_json"])

    def load_current_project_map(self, project_id: str) -> ProjectMapRevision:
        with self.ledger.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
                (project_id,),
            ).fetchone()
        if row is None:
            raise EngineServiceError("current Project Map이 없습니다.")
        return ProjectMapRevision.model_validate_json(row["payload_json"])

    def load_current_state(self, project_id: str, goal_digest: str) -> StateSnapshot:
        with self.ledger.read() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM state_snapshots WHERE project_id = ? "
                "AND goal_contract_digest = ? AND is_current = 1 ORDER BY observed_at DESC",
                (project_id, goal_digest),
            ).fetchall()
        if len(rows) != 1:
            raise EngineServiceError("Goal에 대한 current StateSnapshot이 정확히 하나여야 합니다.")
        return StateSnapshot.model_validate_json(rows[0]["payload_json"])


# 순환 import를 피하면서 sqlite 예외만 명시적으로 처리한다.
import sqlite3  # noqa: E402
