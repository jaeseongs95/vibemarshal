from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .domain import (
    ApprovalClass,
    AttemptKind,
    AttemptRecord,
    AttemptStatus,
    BudgetUsageRecord,
    CandidateDecision,
    CandidateStatus,
    ContextManifest,
    ContextSourceRegistration,
    ContextSourceRegistrationKind,
    ExecutionSpecProposal,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    GoalContractRevision,
    GoalVerdict,
    GoalVerdictStatus,
    ManualValidationObservation,
    ExternalValidationObservation,
    PlanContractRevision,
    PlanSkeletonCandidate,
    ProjectMapRevision,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RecoveryAssessment,
    RepairAction,
    RevisionStatus,
    RuntimeIntentKind,
    RuntimeIntentRecord,
    RuntimeIntentStatus,
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
    plan_gate,
    plan_review_evidence_catalog,
    skeleton_gate,
    skeleton_review_evidence_catalog,
)


class EngineServiceError(RuntimeError):
    pass


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _same_assignment(left: Any, right: Any) -> bool:
    return left.model_dump(mode="json") == right.model_dump(mode="json")


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

    def create_project(self, *, name: str, root: Path | str) -> str:
        resolved_root = Path(root).resolve()
        if not resolved_root.is_dir():
            raise EngineServiceError("프로젝트 root가 존재하는 디렉터리가 아닙니다.")
        project_id = new_id("project")
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

    def register_plan_evaluation(self, evaluation: ExpandedPlanEvaluation) -> None:
        plan = evaluation.plan
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
        for submission in evaluation.semantic_submissions:
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
            for submission in evaluation.semantic_submissions
            for finding in submission.findings
        )
        ratings = (
            evaluation.semantic_submissions[0].ratings
            if evaluation.semantic_submissions
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
            evaluation.plan,
            decision=evaluation.decision,
            reviews=evaluation.semantic_submissions,
        )

    def _register_plan(
        self,
        plan: PlanContractRevision,
        *,
        decision: CandidateDecision,
        reviews: Iterable[Any] = (),
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
            tx.history(
                definition.project_id,
                "plan.registered",
                "plan_revision",
                plan.plan_revision_id,
                {"activation_digest": plan.activation_digest, "decision": decision.status.value},
            )

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
            active_plan_id = project["active_plan_revision_id"]
            if active_plan_id is not None:
                active_plan = tx.one(
                    "SELECT id, plan_id, status FROM plan_revisions WHERE id = ?",
                    (active_plan_id,),
                )
                if (
                    payload.supersedes_plan_revision_id != active_plan_id
                    or payload.plan_id != active_plan["plan_id"]
                ):
                    raise EngineServiceError(
                        "active Plan을 교체하려면 같은 plan_id의 새 revision이 현재 revision을 supersede해야 합니다."
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
                "(id, project_id, plan_revision_id, activation_digest, source, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (activation_id, plan["project_id"], plan_revision_id, activation_digest, source, now),
            )
            self._refresh_ready(tx, plan_revision_id)
            tx.history(
                plan["project_id"],
                "plan.activated",
                "plan_revision",
                plan_revision_id,
                {"activation_digest": activation_digest, "source": source},
            )
            return activation_id

    def retry_task(self, *, task_id: str, new_evidence_ids: tuple[str, ...] = ()) -> None:
        """동일 TaskContract 범위의 재시도만 다시 materialized 상태로 연다."""

        with self.ledger.transaction() as tx:
            task = tx.one("SELECT * FROM task_contracts WHERE id = ?", (task_id,))
            if task["status"] not in {"failed", "blocked"}:
                raise EngineServiceError("failed/blocked Task만 동일 계약으로 재시도할 수 있습니다.")
            attempt = tx.one(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no DESC, rowid DESC LIMIT 1",
                (task_id,),
            )
            failure = FailureClass(attempt["failure_class"])
            if failure in {
                FailureClass.TASK_CONTRACT,
                FailureClass.DEPENDENCY,
                FailureClass.REQUIREMENT_CHANGE,
                FailureClass.EXTERNAL_UNKNOWN,
            }:
                raise EngineServiceError(
                    f"{failure.value} 실패는 같은 Task 재시도가 아니라 Plan/Goal revision이 필요합니다."
                )
            unknown = tx.connection.execute(
                "SELECT COUNT(*) FROM runtime_intents WHERE attempt_id = ? AND status = 'unknown'",
                (attempt["id"],),
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
                {"previous_attempt_id": attempt["id"], "new_evidence_ids": new_evidence_ids},
            )

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
        selection = ContextSelector().select(
            project_map=project_map,
            task=task,
            prompt_binding=preliminary_prompt.binding,
            needs=proposal.context_needs,
            token_budget=proposal.context_token_budget,
        )
        if selection.manifest is None:
            request = selection.additional_context_request
            raise EngineServiceError(
                "CONTEXT_REQUIRED: "
                + canonical_json(request)  # type: ignore[arg-type]
            )
        reference_blocks: list[tuple[str, str]] = []
        for fragment in selection.manifest.fragments:
            source_path = Path(fragment.source_ref)
            if not source_path.is_absolute():
                source_path = root / source_path
            try:
                reference_blocks.append(
                    (fragment.source_ref, source_path.read_text(encoding="utf-8"))
                )
            except (OSError, UnicodeError) as error:
                raise EngineServiceError(
                    f"Context fragment를 읽을 수 없습니다: {fragment.source_ref}"
                ) from error
        final_prompt = PromptAssembler().assemble(
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
            reference_blocks=reference_blocks,
        )
        manifest = ContextManifest(
            context_pack_id=selection.manifest.context_pack_id,
            fragments=selection.manifest.fragments,
            prompt_binding=final_prompt.binding,
            total_token_estimate=selection.manifest.total_token_estimate,
            selection_rationale=selection.manifest.selection_rationale,
            missing_context=selection.manifest.missing_context,
        )
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
            plan = tx.one(
                "SELECT status FROM plan_revisions WHERE id = ?", (task["plan_revision_id"],)
            )
            if plan["status"] != "active":
                raise EngineServiceError("active Plan의 Task만 실행할 수 있습니다.")
            project = tx.one("SELECT run_state FROM projects WHERE id = ?", (task["project_id"],))
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
    def _verify_execution_inputs(tx: Any, task: Any, spec_row: Any) -> None:
        """Attempt 예약 직전에 target/context digest와 current map을 재확인한다."""

        spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
        project = tx.one("SELECT root FROM projects WHERE id = ?", (task["project_id"],))
        current_map = tx.one(
            "SELECT revision_digest FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
            (task["project_id"],),
        )
        if current_map["revision_digest"] != spec.definition.project_map_digest:
            raise EngineServiceError("ExecutionSpec 이후 Project Map이 변경됐습니다.")
        root = Path(project["root"])
        checks: dict[str, str] = {}
        for target in spec.definition.resolved_targets:
            if target.expected_content_digest is not None:
                checks[target.path] = target.expected_content_digest
        for fragment in spec.definition.context_manifest.fragments:
            if fragment.source_kind.value in {"code", "test", "reference", "policy", "project"}:
                checks[fragment.source_ref] = fragment.content_digest
        for raw_path, expected_digest in checks.items():
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
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

    def record_validation(self, *, project_id: str, plan_revision_id: str, result: ValidationResult) -> None:
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
            tx.history(
                project_id,
                "validation.recorded",
                "validation_result",
                result.validation_result_id,
                {"validation_id": result.validation_id, "status": result.status.value, "task_id": result.task_id},
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
            for row in tx.all(
                "SELECT validation_id, status FROM validation_results WHERE task_id = ? "
                "ORDER BY evaluated_at, rowid",
                (task_id,),
            ):
                latest[row["validation_id"]] = row["status"]
            if set(latest) != expected or any(latest[item] != "pass" for item in expected):
                raise EngineServiceError("모든 Task validation의 최신 결과가 PASS여야 합니다.")
            now = tx.now
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'completed', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            ready = self._refresh_ready(tx, task["plan_revision_id"])
            tx.history(
                task["project_id"],
                "task.completed",
                "task_contract",
                task_id,
                {"new_ready_task_ids": ready},
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

    def record_budget_usage(self, usage: BudgetUsageRecord) -> None:
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (usage.project_id,))
            tx.connection.execute(
                "INSERT INTO budget_usage "
                "(id, project_id, goal_contract_digest, stage, logical_call_ref, payload_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    usage.usage_id,
                    usage.project_id,
                    usage.goal_contract_digest,
                    usage.stage.value,
                    usage.logical_call_ref,
                    canonical_json(usage),
                    usage.recorded_at.isoformat(),
                ),
            )
            tx.history(
                usage.project_id,
                "budget.recorded",
                "budget_usage",
                usage.usage_id,
                {"stage": usage.stage.value, "optimization_tokens": usage.optimization_tokens},
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
        expected_action = self.repair_action_for(assessment.failure_class)
        if assessment.action is not expected_action:
            raise EngineServiceError(
                "failure_class에 대한 repair action이 Core 매핑과 다릅니다: "
                f"expected={expected_action.value}, actual={assessment.action.value}"
            )
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT project_id FROM attempts WHERE id = ?", (assessment.attempt_id,))
            if attempt["project_id"] != project_id:
                raise EngineServiceError("RecoveryAssessment가 다른 프로젝트 Attempt를 참조합니다.")
            same_existing = tx.connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments WHERE project_id = ? "
                "AND failure_class = ? AND action = 'subgraph_replan'",
                (project_id, assessment.failure_class.value),
            ).fetchone()[0]
            goal_existing = tx.connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments WHERE project_id = ? "
                "AND action = 'goal_revision'",
                (project_id,),
            ).fetchone()[0]
            expected_same = int(same_existing) + (
                1 if assessment.action is RepairAction.SUBGRAPH_REPLAN else 0
            )
            expected_goal = int(goal_existing) + (
                1 if assessment.action is RepairAction.GOAL_REVISION else 0
            )
            if assessment.same_failure_replan_count != expected_same:
                raise EngineServiceError(
                    f"same_failure_replan_count는 원장에서 계산한 {expected_same}여야 합니다."
                )
            if assessment.goal_replan_count != expected_goal:
                raise EngineServiceError(
                    f"goal_replan_count는 원장에서 계산한 {expected_goal}여야 합니다."
                )
            if assessment.same_failure_replan_count > 2:
                raise EngineServiceError("동일 실패 재계획 한도 2회를 넘었습니다.")
            if assessment.goal_replan_count > 5:
                raise EngineServiceError("Goal 전체 재계획 한도 5회를 넘었습니다.")
            existing_assessments = tx.all(
                "SELECT payload_json, created_at FROM recovery_assessments WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            )
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
