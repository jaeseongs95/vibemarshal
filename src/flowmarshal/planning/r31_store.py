from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .r31_domain import (
    ModelCallReceipt,
    PlanningRunInput,
    PlanningRunReceipt,
    PlanningRunStatus,
    PlanningSearchOutcome,
    ProfileRevisionStatus,
    ProjectProfileDefinition,
    ProjectProfileRevision,
)
from ..core.domain import PlanDraft


class PlanningArtifactStoreError(RuntimeError):
    code = "PLANNING_ARTIFACT_STORE_ERROR"


class PlanningArtifactNotFoundError(PlanningArtifactStoreError):
    code = "PLANNING_ARTIFACT_NOT_FOUND"


class PlanningArtifactConflictError(PlanningArtifactStoreError):
    code = "PLANNING_ARTIFACT_CONFLICT"


class PlanningArtifactBusyError(PlanningArtifactStoreError):
    code = "PLANNING_ARTIFACT_BUSY"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _storage_key(value: str, *, length: int = 40) -> str:
    return sha256_bytes(value.encode("utf-8")).removeprefix("sha256:")[:length]


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 대상 파일명을 반복하지 않아 Windows MAX_PATH 여유를 불필요하게 소모하지 않는다.
    temporary = path.with_name(f".tmp-{uuid4().hex[:16]}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(value))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as exc:
        raise PlanningArtifactNotFoundError(f"planning artifact가 없습니다: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningArtifactStoreError(f"planning artifact를 읽지 못했습니다: {path}") from exc


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """프로세스 종료 시 OS가 자동 해제하는 fail-fast 파일 lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            stream.close()
            raise PlanningArtifactBusyError(
                f"다른 planning 갱신이 진행 중입니다: {path}"
            ) from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            stream.close()
        return

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise PlanningArtifactBusyError(
            f"다른 planning 갱신이 진행 중입니다: {path}"
        ) from exc
    try:
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class ProjectProfileStore:
    """설정으로 주입된 root에 profile definition과 활성 포인터를 저장한다."""

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._root = Path(artifact_root).resolve()
        self._now = now or _utc_now
        self._revision_id_factory = revision_id_factory or (
            lambda: f"profile_revision_{uuid4().hex}"
        )

    def get_active(self, project_id: str) -> ProjectProfileRevision | None:
        pointer_path = self._active_path(project_id)
        if not pointer_path.exists():
            return None
        pointer = _read_json(pointer_path)
        if not isinstance(pointer, dict) or not isinstance(pointer.get("profile_revision_id"), str):
            raise PlanningArtifactStoreError("active profile pointer 형식이 올바르지 않습니다.")
        revision = self.get_revision(pointer["profile_revision_id"])
        if revision.project_id != project_id:
            raise PlanningArtifactStoreError("active profile pointer의 project_id가 다릅니다.")
        expected = pointer.get("definition_digest")
        if expected != revision.definition_digest:
            raise PlanningArtifactStoreError("active profile pointer digest가 revision과 다릅니다.")
        return revision.model_copy(update={"status": ProfileRevisionStatus.ACTIVE})

    def get_revision(self, profile_revision_id: str) -> ProjectProfileRevision:
        locator_path = (
            self._root
            / "profiles"
            / "by-id"
            / f"{_storage_key(profile_revision_id)}.json"
        )
        locator = _read_json(locator_path)
        if (
            not isinstance(locator, dict)
            or locator.get("profile_revision_id") != profile_revision_id
            or not isinstance(locator.get("project_id"), str)
        ):
            raise PlanningArtifactStoreError("profile revision locator 형식이 올바르지 않습니다.")
        project_id = locator["project_id"]
        raw = _read_json(self._revision_path(project_id, profile_revision_id))
        revision = ProjectProfileRevision.model_validate(raw)
        active_path = self._active_path(project_id)
        active_id: str | None = None
        if active_path.exists():
            pointer = _read_json(active_path)
            if isinstance(pointer, dict):
                active_id = pointer.get("profile_revision_id")  # type: ignore[assignment]
        status = (
            ProfileRevisionStatus.ACTIVE
            if active_id == profile_revision_id
            else ProfileRevisionStatus.SUPERSEDED
        )
        return revision.model_copy(update={"status": status})

    def create_revision(
        self,
        project_id: str,
        expected_active_digest: str | None,
        definition: ProjectProfileDefinition,
    ) -> ProjectProfileRevision:
        project_root = self._project_root(project_id)
        with _exclusive_lock(project_root / ".profile.lock"):
            active = self.get_active(project_id)
            observed = active.definition_digest if active is not None else None
            if observed != expected_active_digest:
                raise PlanningArtifactConflictError(
                    "활성 profile digest가 예상값과 달라 revision 생성을 중단했습니다."
                )
            if active is not None and active.definition_digest == definition.definition_digest:
                return active

            for metadata_path in (project_root / "metadata").glob("*.json"):
                metadata = _read_json(metadata_path)
                if not isinstance(metadata, dict):
                    continue
                if (
                    metadata.get("definition_digest") == definition.definition_digest
                    and metadata.get("base_active_digest") == expected_active_digest
                    and isinstance(metadata.get("profile_revision_id"), str)
                ):
                    return self.get_revision(metadata["profile_revision_id"])

            revision_id = self._revision_id_factory()
            revision = ProjectProfileRevision(
                profile_revision_id=revision_id,
                project_id=project_id,
                definition=definition,
                definition_digest=definition.definition_digest,
                status=ProfileRevisionStatus.SUPERSEDED,
                supersedes_profile_revision_id=(
                    active.profile_revision_id if active is not None else None
                ),
                created_at=self._now(),
            )
            metadata = {
                "profile_revision_id": revision_id,
                "project_id": project_id,
                "definition_digest": definition.definition_digest,
                "base_active_digest": expected_active_digest,
            }
            _atomic_write(self._revision_path(project_id, revision_id), revision)
            _atomic_write(
                project_root / "metadata" / f"{_storage_key(revision_id)}.json",
                metadata,
            )
            _atomic_write(
                self._root
                / "profiles"
                / "by-id"
                / f"{_storage_key(revision_id)}.json",
                {"profile_revision_id": revision_id, "project_id": project_id},
            )
            return revision

    def activate_revision(
        self,
        profile_revision_id: str,
        expected_digest: str,
    ) -> ProjectProfileRevision:
        target = self.get_revision(profile_revision_id)
        if target.definition_digest != expected_digest:
            raise PlanningArtifactConflictError(
                "활성화 대상 profile definition digest가 예상값과 다릅니다."
            )
        project_root = self._project_root(target.project_id)
        with _exclusive_lock(project_root / ".profile.lock"):
            current = self.get_active(target.project_id)
            if current is not None and current.profile_revision_id == profile_revision_id:
                return current
            metadata = _read_json(
                project_root
                / "metadata"
                / f"{_storage_key(profile_revision_id)}.json"
            )
            if not isinstance(metadata, dict):
                raise PlanningArtifactStoreError("profile revision metadata가 올바르지 않습니다.")
            base_digest = metadata.get("base_active_digest")
            current_digest = current.definition_digest if current is not None else None
            if base_digest != current_digest:
                raise PlanningArtifactConflictError(
                    "revision 생성 뒤 활성 profile이 변경되어 활성화를 중단했습니다."
                )
            pointer = {
                "project_id": target.project_id,
                "profile_revision_id": target.profile_revision_id,
                "definition_digest": target.definition_digest,
            }
            _atomic_write(self._active_path(target.project_id), pointer)
            return target.model_copy(update={"status": ProfileRevisionStatus.ACTIVE})

    def _project_root(self, project_id: str) -> Path:
        if not project_id.startswith("project_") or len(project_id) != 40:
            raise ValueError("project_id 형식이 올바르지 않습니다.")
        return self._root / "profiles" / project_id

    def _active_path(self, project_id: str) -> Path:
        return self._project_root(project_id) / "active.json"

    def _revision_path(self, project_id: str, revision_id: str) -> Path:
        return (
            self._project_root(project_id)
            / "revisions"
            / f"{_storage_key(revision_id)}.json"
        )


_ALLOWED_RUN_TRANSITIONS: dict[PlanningRunStatus, frozenset[PlanningRunStatus]] = {
    PlanningRunStatus.DRAFT: frozenset(
        {PlanningRunStatus.BLOCKED, PlanningRunStatus.FROZEN}
    ),
    PlanningRunStatus.FROZEN: frozenset(
        {
            PlanningRunStatus.SEARCHING,
            PlanningRunStatus.BLOCKED,
            PlanningRunStatus.FAILED,
            PlanningRunStatus.SUPERSEDED,
        }
    ),
    PlanningRunStatus.SEARCHING: frozenset(
        {
            PlanningRunStatus.READY_FOR_REVIEW,
            PlanningRunStatus.FAILED,
            PlanningRunStatus.BLOCKED,
            PlanningRunStatus.SUPERSEDED,
        }
    ),
    PlanningRunStatus.READY_FOR_REVIEW: frozenset({PlanningRunStatus.SUPERSEDED}),
    PlanningRunStatus.BLOCKED: frozenset({PlanningRunStatus.SUPERSEDED}),
    PlanningRunStatus.FAILED: frozenset({PlanningRunStatus.SUPERSEDED}),
    PlanningRunStatus.SUPERSEDED: frozenset(),
}


class PlanningRunService:
    def __init__(
        self,
        artifact_root: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._root = Path(artifact_root).resolve()
        self._now = now or _utc_now
        self._run_id_factory = run_id_factory or (lambda: f"planning_run_{uuid4().hex}")

    def freeze(
        self,
        input: PlanningRunInput,
        idempotency_key: str,
    ) -> PlanningRunReceipt:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key는 비어 있을 수 없습니다.")
        runs_root = self._root / "runs"
        index_key = sha256_bytes(idempotency_key.encode("utf-8")).removeprefix("sha256:")
        index_path = runs_root / "idempotency" / f"{index_key}.json"
        with _exclusive_lock(runs_root / ".runs.lock"):
            if index_path.exists():
                index = _read_json(index_path)
                if not isinstance(index, dict) or not isinstance(index.get("run_id"), str):
                    raise PlanningArtifactStoreError("PlanningRun idempotency index가 손상됐습니다.")
                existing = self.get(index["run_id"])
                if existing.idempotency_key != idempotency_key:
                    raise PlanningArtifactConflictError("idempotency key hash 충돌을 탐지했습니다.")
                if existing.planning_input_digest != input.planning_input_digest:
                    raise PlanningArtifactConflictError(
                        "같은 idempotency key에 다른 semantic planning input이 사용됐습니다."
                    )
                return existing

            now = self._now()
            receipt = PlanningRunReceipt(
                run_id=self._run_id_factory(),
                planning_input=input,
                planning_input_digest=input.planning_input_digest,
                idempotency_key=idempotency_key,
                status=PlanningRunStatus.FROZEN,
                created_at=now,
                updated_at=now,
            )
            _atomic_write(self._receipt_path(receipt.run_id), receipt)
            _atomic_write(
                index_path,
                {
                    "idempotency_key_digest": f"sha256:{index_key}",
                    "planning_input_digest": input.planning_input_digest,
                    "run_id": receipt.run_id,
                },
            )
            return receipt

    def get(self, run_id: str) -> PlanningRunReceipt:
        receipt = PlanningRunReceipt.model_validate(_read_json(self._receipt_path(run_id)))
        if receipt.run_id != run_id:
            raise PlanningArtifactStoreError("PlanningRun storage key 충돌을 탐지했습니다.")
        return receipt

    def transition(
        self,
        run_id: str,
        status: PlanningRunStatus,
        *,
        reason: str | None = None,
    ) -> PlanningRunReceipt:
        runs_root = self._root / "runs"
        with _exclusive_lock(runs_root / ".runs.lock"):
            current = self.get(run_id)
            if status is current.status:
                return current
            if status not in _ALLOWED_RUN_TRANSITIONS[current.status]:
                raise PlanningArtifactConflictError(
                    f"허용되지 않은 PlanningRun 전이입니다: {current.status.value} -> {status.value}"
                )
            updated = current.model_copy(
                update={"status": status, "updated_at": self._now(), "reason": reason}
            )
            updated = PlanningRunReceipt.model_validate(updated.model_dump(mode="python"))
            _atomic_write(self._receipt_path(run_id), updated)
            return updated

    def supersede(self, run_id: str, reason: str) -> PlanningRunReceipt:
        return self.transition(run_id, PlanningRunStatus.SUPERSEDED, reason=reason)

    def _receipt_path(self, run_id: str) -> Path:
        if not run_id:
            raise ValueError("run_id 형식이 올바르지 않습니다.")
        return self._root / "runs" / f"run-{_storage_key(run_id)}" / "receipt.json"


class PlanningArtifactRepository:
    """후보·평가·선택·모델 호출을 Core DB 밖 immutable JSON으로 보존한다."""

    def __init__(self, artifact_root: str | Path) -> None:
        self._root = Path(artifact_root).resolve()

    def save_search_outcome(self, outcome: PlanningSearchOutcome) -> Path:
        run_root = self._run_root(outcome.run_id)
        with _exclusive_lock(run_root / ".artifacts.lock"):
            for candidate in outcome.candidates:
                envelope_digest = sha256_digest(candidate)
                candidate_key = sha256_bytes(
                    candidate.candidate_id.encode("utf-8")
                ).removeprefix("sha256:")[:16]
                suffix = envelope_digest.removeprefix("sha256:")[:32]
                destination = (
                    run_root
                    / "candidates"
                    / f"{candidate_key}-{suffix}.json"
                )
                self._write_once(destination, candidate)
            for receipt in outcome.model_call_receipts:
                self._write_model_call_once(run_root, receipt)
            selection_suffix = outcome.selection_receipt.selection_digest.removeprefix(
                "sha256:"
            )[:32]
            self._write_once(
                run_root / "selections" / f"{selection_suffix}.json",
                outcome.selection_receipt,
            )
            outcome_suffix = outcome.outcome_digest.removeprefix("sha256:")[:32]
            destination = run_root / "outcomes" / f"{outcome_suffix}.json"
            self._write_once(destination, outcome)
            _atomic_write(
                run_root / "latest-search.json",
                {
                    "outcome_digest": outcome.outcome_digest,
                    "selection_digest": outcome.selection_receipt.selection_digest,
                },
            )
            return destination

    def load_search_outcome(
        self,
        run_id: str,
        outcome_digest: str | None = None,
    ) -> PlanningSearchOutcome:
        run_root = self._run_root(run_id)
        digest = outcome_digest
        if digest is None:
            latest = _read_json(run_root / "latest-search.json")
            if not isinstance(latest, dict) or not isinstance(latest.get("outcome_digest"), str):
                raise PlanningArtifactStoreError("latest search pointer 형식이 올바르지 않습니다.")
            digest = latest["outcome_digest"]
        suffix = digest.removeprefix("sha256:")[:32]
        outcome = PlanningSearchOutcome.model_validate(
            _read_json(run_root / "outcomes" / f"{suffix}.json")
        )
        if outcome.run_id != run_id:
            raise PlanningArtifactStoreError("search outcome storage key 충돌을 탐지했습니다.")
        if outcome.outcome_digest != digest:
            raise PlanningArtifactStoreError("저장된 search outcome digest가 다릅니다.")
        return outcome

    def save_model_call_receipt(self, run_id: str, receipt: ModelCallReceipt) -> Path:
        run_root = self._run_root(run_id)
        with _exclusive_lock(run_root / ".artifacts.lock"):
            return self._write_model_call_once(run_root, receipt)

    def save_preflight_model_call_receipt(
        self,
        scope_digest: str,
        receipt: ModelCallReceipt,
    ) -> Path:
        """PlanningRun freeze 전 Mission/요구 분석 호출을 즉시 보존한다."""

        if not (
            scope_digest.startswith("sha256:")
            and len(scope_digest) == len("sha256:") + 64
        ):
            raise ValueError("preflight scope에는 sha256 digest가 필요합니다.")
        scope_root = self._root / "preflight" / f"scope-{_storage_key(scope_digest)}"
        with _exclusive_lock(scope_root / ".artifacts.lock"):
            self._write_once(
                scope_root / "scope.json",
                {"scope_digest": scope_digest},
            )
            return self._write_model_call_once(scope_root, receipt)

    def save_model_call_journal_receipt(self, receipt: ModelCallReceipt) -> Path:
        """run 결속 전 crash에도 남도록 모든 실제 model call을 즉시 journal한다."""

        scope_root = (
            self._root
            / "model-call-journal"
            / f"input-{_storage_key(receipt.input_digest)}"
        )
        with _exclusive_lock(scope_root / ".artifacts.lock"):
            self._write_once(
                scope_root / "scope.json",
                {"input_digest": receipt.input_digest},
            )
            return self._write_model_call_once(scope_root, receipt)

    def save_selected_plan(
        self,
        run_id: str,
        selection_digest: str,
        plan: PlanDraft,
    ) -> Path:
        """Core 활성화 없이 선택된 PlanDraft snapshot 하나만 export한다."""

        run_root = self._run_root(run_id)
        suffix = selection_digest.removeprefix("sha256:")[:32]
        destination = run_root / "exports" / suffix / "plan-draft.json"
        with _exclusive_lock(run_root / ".artifacts.lock"):
            self._write_once(destination, plan)
            self._write_once(
                destination.with_name("selection-reference.json"),
                {
                    "plan_digest": plan.canonical_digest,
                    "selection_digest": selection_digest,
                },
            )
        return destination

    @staticmethod
    def _write_model_call_once(run_root: Path, receipt: ModelCallReceipt) -> Path:
        destination = (
            run_root / "model-calls" / f"{_storage_key(receipt.call_id)}.json"
        )
        PlanningArtifactRepository._write_once(destination, receipt)
        return destination

    @staticmethod
    def _write_once(path: Path, value: object) -> None:
        if path.exists():
            observed = _read_json(path)
            expected = json.loads(canonical_json(value))
            if observed != expected:
                raise PlanningArtifactConflictError(
                    f"immutable planning artifact를 덮어쓸 수 없습니다: {path}"
                )
            return
        _atomic_write(path, value)

    def _run_root(self, run_id: str) -> Path:
        if not run_id:
            raise ValueError("run_id 형식이 올바르지 않습니다.")
        return self._root / "runs" / f"run-{_storage_key(run_id)}"


__all__ = [
    "PlanningArtifactBusyError",
    "PlanningArtifactConflictError",
    "PlanningArtifactNotFoundError",
    "PlanningArtifactStoreError",
    "PlanningArtifactRepository",
    "PlanningRunService",
    "ProjectProfileStore",
]
