"""실제 qualification 역할 호출의 사용자 주입 예산·timeout 결속."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import model_validator

from ..canonical import sha256_digest
from .budget import BudgetManager, BudgetedRoleRunner, GoalBudgetPolicy
from .domain import EngineModel, GoalContractRevision, ProjectProfileRevision
from .ledger import SQLiteEngineLedger
from .role_execution import RoleTimeoutPolicy
from .roles import CodexStructuredRoleRunner
from .runtime import CodexProjectBinding
from .service import EngineService


class EvaluationPolicies(EngineModel):
    """qualification 계약에 그대로 포함되는 사용자 제공 운영 정책."""

    budget: GoalBudgetPolicy
    role_timeouts: RoleTimeoutPolicy
    codex_project: CodexProjectBinding | None = None

    @model_validator(mode="after")
    def schema_recovery_is_never_budgeted(self) -> "EvaluationPolicies":
        # recovery 횟수는 runner 생성 시 0으로 고정하며 정책에 묵시적 token을 더하지 않는다.
        return self

    @property
    def policy_digest(self) -> str:
        return sha256_digest(self)


def load_evaluation_policies(
    *,
    budget_policy_path: Path | str,
    role_timeout_policy_path: Path | str,
    codex_project_binding_path: Path | str | None = None,
) -> EvaluationPolicies:
    return EvaluationPolicies(
        budget=GoalBudgetPolicy.model_validate_json(
            Path(budget_policy_path).read_text(encoding="utf-8")
        ),
        role_timeouts=RoleTimeoutPolicy.model_validate_json(
            Path(role_timeout_policy_path).read_text(encoding="utf-8")
        ),
        codex_project=(
            None
            if codex_project_binding_path is None
            else CodexProjectBinding.model_validate_json(
                Path(codex_project_binding_path).read_text(encoding="utf-8")
            )
        ),
    )


def policies_from_metadata(metadata: dict[str, Any]) -> EvaluationPolicies:
    policies = EvaluationPolicies.model_validate(metadata.get("evaluation_policies"))
    if metadata.get("evaluation_policy_digest") != policies.policy_digest:
        raise ValueError("EVALUATION_POLICY_METADATA_DIGEST_MISMATCH")
    return policies


def policy_contract_fragment(policies: EvaluationPolicies) -> dict[str, Any]:
    fragment = {
        "evaluation_policy_digest": policies.policy_digest,
        "budget_policy_digest": sha256_digest(policies.budget),
        "role_timeout_policy_digest": policies.role_timeouts.policy_digest,
        "max_schema_recovery_attempts": 0,
        "ephemeral_threads": False,
    }
    if policies.codex_project is not None:
        fragment["codex_project"] = policies.codex_project.model_dump(
            mode="json", exclude_none=True
        )
    if policies.role_timeouts.observation_policy is not None:
        fragment["role_observation_policy"] = policies.role_timeouts.observation_policy.model_dump(mode="json")
        fragment["role_observation_policy_digest"] = policies.role_timeouts.observation_policy.policy_digest
    return fragment


def metadata_with_policies(values: dict[str, Any], policies: EvaluationPolicies) -> dict[str, Any]:
    document = dict(values)
    document.update(
        evaluation_policies=policies.model_dump(mode="json", exclude_none=True),
        evaluation_policy_digest=policies.policy_digest,
    )
    document["metadata_digest"] = sha256_digest(document)
    return document


def write_immutable_run_metadata(
    path: Path | str, values: dict[str, Any], policies: EvaluationPolicies
) -> None:
    destination = Path(path)
    document = metadata_with_policies(values, policies)
    if destination.is_file():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if existing != document:
            raise ValueError("EVALUATION_RUN_METADATA_BINDING_MISMATCH")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def verify_metadata_digest(metadata: dict[str, Any]) -> None:
    document = dict(metadata)
    observed = document.pop("metadata_digest", None)
    if observed != sha256_digest(document):
        raise ValueError("EVALUATION_RUN_METADATA_DIGEST_MISMATCH")


def initialize_cell_budget(
    *,
    state_root: Path,
    workspace: Path,
    project_id: str,
    profile: ProjectProfileRevision,
    policies: EvaluationPolicies,
) -> tuple[EngineService, BudgetManager]:
    """새 cell 원장을 만들거나 같은 정책에 결속된 미완료 원장을 복원한다."""

    database = state_root / "flowmarshal-engine.sqlite3"
    existed = database.is_file()
    ledger = SQLiteEngineLedger(database, artifact_root=state_root / "artifacts")
    service = EngineService(ledger)
    service.initialize()
    manager = BudgetManager(service)
    if not existed:
        service.create_project(
            name="FlowMarshal qualification role cell",
            root=workspace,
            project_id=project_id,
        )
        service.register_profile(profile)
        manager.configure(project_id, policies.budget)
    else:
        with ledger.read() as connection:
            project = connection.execute(
                "SELECT root, active_profile_revision_id FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            policy = connection.execute(
                "SELECT policy_digest FROM budget_policy_revisions WHERE project_id = ? "
                "AND scope_key = '' ORDER BY revision_no DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            prior_calls = connection.execute(
                "SELECT id, status FROM provider_calls WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        if (
            project is None
            or Path(project["root"]).resolve() != workspace.resolve()
            or project["active_profile_revision_id"] != profile.profile_revision_id
            or policy is None
            or policy["policy_digest"] != sha256_digest(policies.budget)
        ):
            raise ValueError("EVALUATION_CELL_BUDGET_BINDING_MISMATCH")
        if prior_calls:
            raise ValueError(
                "EVALUATION_CELL_PARTIAL_CALLS_REQUIRE_NEW_RUN: 중간 역할 산출물 checkpoint가 "
                "없어 기존 provider 호출을 재실행할 수 없습니다: "
                + ", ".join(f"{row['id']}={row['status']}" for row in prior_calls)
            )
    return service, manager


def evaluation_cell_provider_calls(state_root: Path | str) -> tuple[tuple[str, str], ...]:
    """완료 checkpoint가 없는 evaluation cell의 provider 호출 상태를 읽는다."""

    root = Path(state_root)
    database = root / "flowmarshal-engine.sqlite3"
    if not database.is_file():
        return ()
    ledger = SQLiteEngineLedger(database, artifact_root=root / "artifacts")
    with ledger.read() as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_calls'"
        ).fetchone()
        if table is None:
            raise ValueError("EVALUATION_CELL_PROVIDER_CALL_LEDGER_INVALID")
        rows = connection.execute(
            "SELECT id, status FROM provider_calls ORDER BY rowid"
        ).fetchall()
    return tuple((str(row["id"]), str(row["status"])) for row in rows)


def register_and_attach_goal(
    service: EngineService,
    manager: BudgetManager,
    goal: GoalContractRevision,
) -> None:
    with service.ledger.read() as connection:
        existing = connection.execute(
            "SELECT definition_digest FROM goal_revisions WHERE id = ?",
            (goal.goal_revision_id,),
        ).fetchone()
    if existing is None:
        service.register_goal(goal, activate=goal.status.value == "ready")
    elif existing["definition_digest"] != goal.definition_digest:
        raise ValueError("EVALUATION_GOAL_RESUME_BINDING_MISMATCH")
    manager.attach_goal(
        goal.definition.project_id,
        goal.goal_id,
        goal.definition_digest,
    )


def verify_service_budget_policy(
    service: EngineService, project_id: str, policies: EvaluationPolicies
) -> None:
    with service.ledger.read() as connection:
        row = connection.execute(
            "SELECT policy_digest FROM budget_policy_revisions WHERE project_id = ? "
            "ORDER BY (scope_key <> '') DESC, revision_no DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    if row is None or row["policy_digest"] != sha256_digest(policies.budget):
        raise ValueError("EVALUATION_CELL_BUDGET_BINDING_MISMATCH")


def budgeted_role_runner(
    runtime: Any,
    service: EngineService,
    *,
    project_id: str,
    goal_id: str,
    goal_digest: str | None,
    progress_sink: Any = None,
    operational_binding: Any = None,
) -> BudgetedRoleRunner:
    runner = CodexStructuredRoleRunner(
        runtime,
        progress_sink=progress_sink,
        operational_binding=operational_binding,
        max_schema_recovery_attempts=0,
        ephemeral_threads=False,
    )
    return BudgetedRoleRunner(
        runner,
        service,
        project_id=project_id,
        goal_id=goal_id,
        goal_digest=goal_digest,
    )
