from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.budget import BudgetBlocked, BudgetManager, GoalBudgetPolicy, record_validator_usage
from flowmarshal.engine.cli import main
from flowmarshal.engine.domain import (
    BudgetStage,
    BudgetUsageRecord,
    CriterionVerdict,
    GoalContractRevision,
    GoalVerdict,
    GoalVerdictStatus,
    RevisionStatus,
    RunOnceAction,
    ValidationStatus,
    new_id,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.operations import CoreOperations
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeObservation
from flowmarshal.engine.service import EngineService, EngineServiceError
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.models import ModelInventory
from flowmarshal.engine.qualification import default_role_configuration
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class UsageFakeCodexRuntime(FakeCodexRuntime):
    """실제 provider 호출 없이 turn-scope usage 관측만 재현한다."""

    def __init__(self, inventory: ModelInventory) -> None:
        super().__init__(inventory)
        self._prompt_digests: dict[str, str] = {}

    def start_turn(self, **kwargs):
        receipt = super().start_turn(**kwargs)
        self._prompt_digests[kwargs["thread_id"]] = receipt.payload["prompt_digest"]
        return receipt

    def create_thread(self, **kwargs):
        receipt = super().create_thread(**kwargs)
        if getattr(self, "wrong_creation_thread", False):
            payload = {**receipt.payload, "thread": {**receipt.payload["thread"], "id": "unrelated-thread"}}
            return receipt.model_copy(update={"payload": payload})
        return receipt

    def read(self, *, thread_id: str) -> RuntimeObservation:
        observation = super().read(thread_id=thread_id)
        payload = dict(observation.payload)
        payload.update({
            "prompt_digest": self._prompt_digests[thread_id],
            "usage_scope": "turn",
            "usage_source": "test-fake",
            "usage": {
                "inputTokens": 7,
                "cachedInputTokens": 2,
                "outputTokens": 3,
                "reasoningOutputTokens": 1,
                "totalTokens": 10,
            },
            "duration_ms": 9,
        })
        return observation.model_copy(update={"payload": payload})


class EngineBudgetIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.inventory = qualification_inventory()
        self.roles = default_role_configuration(ROOT)

    def prepared(self, name: str, *, semantic_task_validation: bool = False):
        base = self.base / name
        base.mkdir()
        workspace, _ = _copy_fixture(ROOT, base)
        prepared = _prepare(
            workspace=workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=self.roles,
            semantic_task_validation=semantic_task_validation,
        )
        return prepared

    @staticmethod
    def configure_small_policy(prepared, *, total: int = 100, reserve: int = 10) -> BudgetManager:
        manager = BudgetManager(prepared.service)
        manager.configure(
            prepared.project_id,
            GoalBudgetPolicy(total_tokens=total, call_reservation_tokens=reserve),
        )
        return manager

    def test_core_no_effect_budget_block_can_retry_after_restart_without_external_unknown(self) -> None:
        prepared = self.prepared("core-no-effect")
        manager = self.configure_small_policy(prepared, total=1, reserve=2)
        request = {"operation": "small-budget"}
        attempts = 0

        def reserve_call():
            nonlocal attempts
            attempts += 1
            return {
                "call_id": manager.reserve(
                    project_id=prepared.project_id,
                    goal_id=self._goal_id(prepared),
                    goal_digest=self._goal_digest(prepared),
                    call_key=f"core-budget-{attempts}",
                    role="goal_reviewer",
                    request=request,
                )
            }

        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_BLOCKED"):
            CoreOperations(prepared.service).invoke(
                project_id=prepared.project_id, kind="budgeted_prepare", request=request, execute=reserve_call,
            )

        restarted = EngineService(prepared.service.ledger)
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_BLOCKED"):
            CoreOperations(restarted).invoke(
                project_id=prepared.project_id, kind="budgeted_prepare", request=request, execute=reserve_call,
            )

        with restarted.ledger.read() as connection:
            events = connection.execute(
                "SELECT event_type FROM history_events WHERE project_id = ? AND entity_type = 'core_operation' ORDER BY sequence",
                (prepared.project_id,),
            ).fetchall()
            project = connection.execute("SELECT run_state FROM projects WHERE id = ?", (prepared.project_id,)).fetchone()
        self.assertEqual(["operation.prepared", "operation.no_effect", "operation.prepared", "operation.no_effect"],
                         [row["event_type"] for row in events])
        self.assertEqual(2, attempts)
        self.assertEqual("active", project["run_state"])

    def test_budget_block_releases_unstarted_attempt_before_fake_provider_effect_and_keeps_typed_reads(self) -> None:
        prepared = self.prepared("dispatch-blocked")
        self.configure_small_policy(prepared, total=1, reserve=2)
        runtime = FakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(prepared.service, runtime)

        self.assertEqual(
            RunOnceAction.MATERIALIZED,
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal).action,
        )
        blocked = dispatcher.run_once(prepared.project_id)

        self.assertEqual((RunOnceAction.BLOCKED, "BUDGET_BLOCKED"), (blocked.action, blocked.blocker_code))
        self.assertEqual((0, 0), (runtime.create_calls, runtime.turn_calls))
        with prepared.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT id,status FROM attempts WHERE project_id = ? ORDER BY rowid DESC LIMIT 1",
                (prepared.project_id,),
            ).fetchone()
            task = connection.execute(
                "SELECT status FROM task_contracts WHERE id = ?", (prepared.task_id,),
            ).fetchone()
            calls = connection.execute("SELECT COUNT(*) FROM provider_calls WHERE project_id = ?",
                                       (prepared.project_id,)).fetchone()[0]
        assert attempt is not None and task is not None
        self.assertEqual(("abandoned", "materialized", 0), (attempt["status"], task["status"], calls))

        application = EngineApplication(prepared.service)
        detail = application.attempt_detail(attempt["id"])
        bindings = application.model_binding_status(prepared.project_id, inventory=self.inventory)
        self.assertEqual(attempt["id"], detail.attempt.attempt_id)
        self.assertEqual((), detail.intents)
        self.assertIsNone(detail.error_code)
        self.assertIsNone(bindings.error_code)
        self.assertTrue(bindings.bindings)
        self.assertTrue(all(item.supported is True for item in bindings.bindings))

    def test_worker_and_validator_usage_settle_their_reserved_calls_in_order(self) -> None:
        prepared = self.prepared("usage-settlement", semantic_task_validation=True)
        self.configure_small_policy(prepared, total=100, reserve=10)
        runtime = UsageFakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(prepared.service, runtime)

        self.assertEqual(RunOnceAction.MATERIALIZED,
                         dispatcher.run_once(prepared.project_id, proposal=prepared.proposal).action)
        dispatched_worker = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched_worker.action)
        worker_thread = self._attempt_thread(prepared, dispatched_worker.attempt_id)
        (prepared.workspace / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n",
            encoding="utf-8",
        )
        runtime.complete(worker_thread, response="worker completed")
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(prepared.project_id).action)

        dispatched_validator = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched_validator.action)
        row, spec = dispatcher._attempt_context(dispatched_validator.attempt_id)
        evidence_refs = tuple(dispatcher._semantic_evidence_catalog(row, spec, "validation_public_contract"))
        self.assertTrue(evidence_refs)
        validator_thread = self._attempt_thread(prepared, dispatched_validator.attempt_id)
        runtime.complete(
            validator_thread,
            response=json.dumps({"passed": True, "rationale": "직접 evidence 확인", "evidence_refs": evidence_refs}),
        )
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)

        with prepared.service.ledger.read() as connection:
            calls = connection.execute(
                "SELECT role,stage,status,usage_id,actual_tokens FROM provider_calls "
                "WHERE project_id = ? ORDER BY created_at,rowid",
                (prepared.project_id,),
            ).fetchall()
            usage = connection.execute(
                "SELECT COUNT(*) FROM budget_usage WHERE project_id = ?", (prepared.project_id,),
            ).fetchone()[0]
        self.assertEqual(
            [("worker", "execution"), ("semantic_validator", "validation")],
            [(row["role"], row["stage"]) for row in calls],
        )
        self.assertEqual([("settled", 10), ("settled", 10)],
                         [(row["status"], row["actual_tokens"]) for row in calls],
                         [dict(row) for row in calls])
        self.assertTrue(all(row["usage_id"] for row in calls))
        self.assertEqual(2, usage)
        self.assertEqual((2, 2), (runtime.create_calls, runtime.turn_calls))

    def test_cli_final_json_and_markdown_share_null_usage_for_historical_verdict(self) -> None:
        prepared = self.prepared("cli-historical")
        original = self._goal_revision(prepared)
        revised_definition = original.definition.model_copy(update={"observable_outcome": "새 활성 Goal"})
        revised = original.model_copy(update={
            "goal_revision_id": new_id("goal_revision"),
            "revision_no": 2,
            "status": RevisionStatus.READY,
            "supersedes_goal_revision_id": original.goal_revision_id,
            "definition": revised_definition,
            "definition_digest": revised_definition.definition_digest,
            "created_at": utc_now(),
        })
        # Final report는 활성 Goal이 아니라 verdict가 가리킨 정확한 revision을
        # 조회해야 한다. 실행 Plan은 역사적 revision에 그대로 결속해 둔다.
        with prepared.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO goal_revisions (id,goal_id,project_id,revision_no,definition_digest,payload_json,status,supersedes_id,created_at,activated_at) "
                "VALUES (?,?,?,?,?,?, 'active',?,?,?)",
                (revised.goal_revision_id, revised.goal_id, prepared.project_id, revised.revision_no,
                 revised.definition_digest, canonical_json(revised), original.goal_revision_id,
                 revised.created_at.isoformat(), utc_now().isoformat()),
            )
            tx.connection.execute("UPDATE goal_revisions SET status='superseded' WHERE id=?",
                                  (original.goal_revision_id,))
            tx.connection.execute("UPDATE projects SET active_goal_revision_id=? WHERE id=?",
                                  (revised.goal_revision_id, prepared.project_id))
        prepared.service.record_budget_usage(BudgetUsageRecord(
            usage_id=new_id("usage"),
            project_id=prepared.project_id,
            goal_contract_digest=original.definition_digest,
            stage=BudgetStage.EXECUTION,
            logical_call_ref="historical-unknown",
            role="worker",
            call_status="succeeded",
            model="worker",
            effort="medium",
            permission_profile=":danger-full-access",
            approval_policy="never",
            input_digest=DIGEST_A,
            output_schema_digest=DIGEST_B,
            runner_receipt_digest=sha256_digest("historical-unknown"),
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            latency_ms=None,
            usage_available=False,
            unavailable_reason="PROVIDER_USAGE_UNAVAILABLE",
            recorded_at=utc_now(),
        ))
        verdict = GoalVerdict(
            goal_verdict_id=new_id("goal_verdict"),
            goal_contract_digest=original.definition_digest,
            plan_activation_digest=prepared.activation_digest,
            status=GoalVerdictStatus.SATISFIED,
            criteria=(CriterionVerdict(
                criterion_id="ac_fix",
                status=ValidationStatus.PASS,
                evidence_ids=(new_id("evidence"),),
                rationale="역사적 verdict",
            ),),
            integration_validation_result_ids=(new_id("validation_result"),),
            evaluated_at=utc_now(),
        )
        with prepared.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO goal_verdicts (id,project_id,plan_revision_id,goal_contract_digest,status,payload_json,evaluated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (verdict.goal_verdict_id, prepared.project_id, prepared.plan_revision_id,
                 original.definition_digest, verdict.status.value, canonical_json(verdict),
                 verdict.evaluated_at.isoformat()),
            )

        common = [
            "--db", str(prepared.service.ledger.path),
            "--artifacts", str(prepared.service.ledger.artifact_root),
            "report", "final", "--project-id", prepared.project_id,
            "--goal-verdict-id", verdict.goal_verdict_id,
        ]
        json_output = io.StringIO()
        with redirect_stdout(json_output):
            self.assertEqual(0, main(common + ["--format", "json"]))
        document = json.loads(json_output.getvalue())
        markdown_output = io.StringIO()
        with redirect_stdout(markdown_output):
            self.assertEqual(0, main(common + ["--format", "markdown"]))

        self.assertEqual(original.goal_revision_id, document["goal"]["goal_revision_id"])
        self.assertNotEqual(revised.goal_revision_id, document["goal"]["goal_revision_id"])
        self.assertIsNone(document["usage"]["input_tokens"]["total"])
        self.assertIsNone(document["usage"]["latency_ms"]["total"])
        self.assertIn("미확인", markdown_output.getvalue())
        self.assertIn("## 미확인 사용량", markdown_output.getvalue())

    def validator_observation(self, name: str, *, wrong_creation_thread: bool = False):
        prepared = self.prepared(name, semantic_task_validation=True)
        self.configure_small_policy(prepared)
        runtime = UsageFakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        worker = dispatcher.run_once(prepared.project_id)
        (prepared.workspace / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n", encoding="utf-8")
        runtime.complete(self._attempt_thread(prepared, worker.attempt_id), response="worker completed")
        dispatcher.run_once(prepared.project_id)
        dispatcher.run_once(prepared.project_id)
        runtime.wrong_creation_thread = wrong_creation_thread
        validator = dispatcher.run_once(prepared.project_id)
        thread_id = self._attempt_thread(prepared, validator.attempt_id)
        runtime.complete(thread_id, response="validator completed")
        return prepared, validator.attempt_id, runtime.read(thread_id=thread_id)

    def test_validator_repeated_observation_rejects_conflicting_usage_and_keeps_real_zero(self) -> None:
        prepared, attempt_id, observation = self.validator_observation("validator-zero")
        payload = dict(observation.payload)
        payload["usage"] = {key: 0 for key in payload["usage"]}
        zero = observation.model_copy(update={"payload": payload})
        recorded = record_validator_usage(prepared.service, attempt_id, zero)
        self.assertTrue(recorded.usage_available)
        self.assertEqual((0, "turn", "provider_turn"),
                         (recorded.input_tokens, recorded.usage_scope, recorded.attribution_basis))
        self.assertEqual(recorded.usage_id, record_validator_usage(prepared.service, attempt_id, zero).usage_id)
        with self.assertRaisesRegex(EngineServiceError, "VALIDATOR_USAGE_CONFLICT"):
            record_validator_usage(prepared.service, attempt_id, observation)

    def test_validator_cumulative_usage_requires_the_exact_empty_thread_receipt(self) -> None:
        prepared, attempt_id, observation = self.validator_observation("validator-thread-binding", wrong_creation_thread=True)
        # receipt 안의 다른 thread는 빈 상태여도 해당 Validator의 첫 turn 근거가 아니다.
        payload = {**observation.payload, "usage_scope": "thread", "usage": {"total": observation.payload["usage"]}}
        recorded = record_validator_usage(prepared.service, attempt_id, observation.model_copy(update={"payload": payload}))
        self.assertFalse(recorded.usage_available)
        self.assertIsNone(recorded.input_tokens)
        status = BudgetManager(prepared.service).status(prepared.project_id, goal_id=self._goal_id(prepared))
        self.assertIsNone(status.error_code)
        self.assertIsNone(status.remaining_total_tokens)
        self.assertIn("실행을 차단하지 않습니다", status.next_action or "")

    def test_budget_rejects_unregistered_goal_lineage_before_reserving(self) -> None:
        prepared = self.prepared("wrong-goal-lineage")
        manager = self.configure_small_policy(prepared)
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_GOAL_BINDING_MISMATCH"):
            manager.reserve(project_id=prepared.project_id, goal_id=new_id("goal"),
                            goal_digest=self._goal_digest(prepared), call_key="wrong-goal",
                            role="goal_reviewer", request={})

    def test_interrupted_attempt_can_resume_after_explicit_budget_increase(self) -> None:
        prepared = self.prepared("resume-budget")
        manager = self.configure_small_policy(prepared, total=20, reserve=10)
        runtime = UsageFakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        thread_id = self._attempt_thread(prepared, dispatched.attempt_id)
        turn_id = runtime.read(thread_id=thread_id).turn_id
        runtime.interrupt(thread_id=thread_id, turn_id=turn_id)
        blocked = dispatcher.run_once(prepared.project_id)
        self.assertEqual((RunOnceAction.BLOCKED, "BUDGET_BLOCKED"), (blocked.action, blocked.blocker_code))
        with prepared.service.ledger.read() as db:
            attempt = db.execute("SELECT status FROM attempts WHERE id=?", (dispatched.attempt_id,)).fetchone()
        self.assertEqual("running", attempt["status"])
        self.assertEqual((1, 1, 0), (runtime.create_calls, runtime.turn_calls, runtime.resume_calls))
        manager.configure(prepared.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10))
        restarted = EngineDispatcher(EngineService(prepared.service.ledger), runtime)
        resumed = restarted.run_once(prepared.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, dispatched.attempt_id), (resumed.action, resumed.attempt_id))
        self.assertEqual((1, 2, 1), (runtime.create_calls, runtime.turn_calls, runtime.resume_calls))

    @staticmethod
    def _attempt_thread(prepared, attempt_id: str) -> str:
        with prepared.service.ledger.read() as connection:
            row = connection.execute("SELECT binding_json FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        assert row is not None and row["binding_json"] is not None
        return json.loads(row["binding_json"])["thread_id"]

    @staticmethod
    def _goal_revision(prepared) -> GoalContractRevision:
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM goal_revisions WHERE project_id = ? ORDER BY revision_no LIMIT 1",
                (prepared.project_id,),
            ).fetchone()
        assert row is not None
        return GoalContractRevision.model_validate_json(row["payload_json"])

    @classmethod
    def _goal_id(cls, prepared) -> str:
        return cls._goal_revision(prepared).goal_id

    @classmethod
    def _goal_digest(cls, prepared) -> str:
        return cls._goal_revision(prepared).definition_digest


if __name__ == "__main__":
    unittest.main()
