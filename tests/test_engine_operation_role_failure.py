from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetedRoleRunner, BudgetManager
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.operations import CoreOperations, KnownOperationFailed
from flowmarshal.engine.roles import (
    RoleCallReceipt,
    StructuredRoleError,
    make_role_request,
    strict_json_output_schema,
)
from flowmarshal.engine.runtime import RuntimeObservation
from flowmarshal.engine.service import EngineService, EngineServiceError
from flowmarshal.engine.domain import utc_now
from tests.engine_helpers import goal, inventory, profile


class _SchemaFailureRunner:
    def __init__(self, *, status: str = "schema_failed") -> None:
        self.status = status
        self.calls = 0
        self.receipt = None

    @property
    def receipts(self):
        return () if self.receipt is None else (self.receipt,)

    def run(self, request, *, validator=None):
        self.calls += 1
        self.receipt = RoleCallReceipt(
            call_id="model_call_" + "7" * 32,
            role=request.role,
            status=self.status,
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id="thread-schema",
            turn_ids=("turn-schema",),
            input_digest=request.request_digest,
            output_schema_digest=sha256_digest(
                strict_json_output_schema(request.output_schema)
            ),
            input_tokens=20 if self.status == "schema_failed" else None,
            cached_input_tokens=0 if self.status == "schema_failed" else None,
            output_tokens=10 if self.status == "schema_failed" else None,
            reasoning_tokens=2 if self.status == "schema_failed" else None,
            usage_available=self.status == "schema_failed",
            latency_ms=11,
            error_summary=(
                "semantic_instruction은 deterministic step에 허용되지 않습니다."
                if self.status == "schema_failed" else "role turn timeout"
            ),
            recorded_at=utc_now(),
            observed_binding=request.operational_binding,
        )
        raise StructuredRoleError(
            "structured output이 유효하지 않습니다."
            if self.status == "schema_failed" else "role turn timeout",
            receipt=self.receipt,
            receipts=(self.receipt,),
        )


class EngineOperationRoleFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        ledger = SQLiteEngineLedger(
            self.root / "ledger.sqlite3", artifact_root=self.root / "artifacts"
        )
        self.service = EngineService(ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="operation failure", root=self.root)
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.service.register_profile(self.profile)
        self.service.register_goal(self.goal)
        self.inventory = inventory()

    def request(self, *, marker: str = "original"):
        return make_role_request(
            inventory=self.inventory,
            role="goal_test_preparation",
            instructions="Goal Test 실행 상세를 JSON으로 반환한다.",
            payload={"marker": marker},
            output_schema={
                "type": "object",
                "properties": {"step": {"type": "string"}},
                "required": ["step"],
                "additionalProperties": False,
            },
            model="worker",
            effort="medium",
            inventory_digest=self.inventory.inventory_digest,
            cwd=str(self.root),
        )

    def budgeted(self, runner):
        return BudgetedRoleRunner(
            runner,
            self.service,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest,
        )

    def test_schema_failed_receipt_closes_operation_and_same_request_is_not_recalled(self):
        request = self.request()
        runner = _SchemaFailureRunner()
        wrapped = self.budgeted(runner)
        operation_request = {
            "role_request": request.model_dump(mode="json"),
            "authority_context_digest": "sha256:" + "a" * 64,
        }

        with self.assertRaises(StructuredRoleError):
            CoreOperations(self.service).invoke(
                project_id=self.project_id,
                kind="goal_test_preparation",
                request=operation_request,
                execute=lambda: wrapped.run(request),
            )

        self.assertEqual((), CoreOperations(self.service).recover_unfinished(self.project_id))
        with self.assertRaisesRegex(KnownOperationFailed, "ROLE_SCHEMA_FAILED"):
            CoreOperations(self.service).invoke(
                project_id=self.project_id,
                kind="goal_test_preparation",
                request=operation_request,
                execute=lambda: wrapped.run(request),
            )
        self.assertEqual(1, runner.calls)

        result = CoreOperations(self.service).invoke(
            project_id=self.project_id,
            kind="goal_test_preparation",
            request={**operation_request, "authority_context_digest": "sha256:" + "b" * 64},
            execute=lambda: {"manual_step": "separately validated"},
        )
        self.assertEqual({"manual_step": "separately validated"}, result)
        with self.service.ledger.read() as connection:
            events = connection.execute(
                "SELECT event_type,payload_json FROM history_events "
                "WHERE project_id=? AND entity_type='core_operation' ORDER BY sequence",
                (self.project_id,),
            ).fetchall()
            provider = connection.execute(
                "SELECT status,actual_tokens,receipt_json FROM provider_calls"
            ).fetchone()
        self.assertEqual(
            ["operation.prepared", "operation.failed", "operation.prepared", "operation.completed"],
            [row["event_type"] for row in events],
        )
        failed = json.loads(events[1]["payload_json"])
        self.assertEqual("ROLE_SCHEMA_FAILED", failed["failure_code"])
        self.assertEqual(sha256_digest(runner.receipt), failed["receipt_digest"])
        self.assertEqual(("settled", 30), (provider["status"], provider["actual_tokens"]))
        self.assertEqual(
            runner.receipt,
            RoleCallReceipt.model_validate_json(provider["receipt_json"]),
        )

    def test_existing_prepared_operation_accepts_only_bound_completed_schema_failure(self):
        request = self.request()
        operation_request = {
            "role_request": request.model_dump(mode="json"),
            "authority_context_digest": "sha256:" + "c" * 64,
        }

        class _Crash(BaseException):
            pass

        def fault(point: str):
            if point == "after_goal_test_preparation_intent":
                raise _Crash()

        with self.assertRaises(_Crash):
            CoreOperations(self.service, fault).invoke(
                project_id=self.project_id,
                kind="goal_test_preparation",
                request=operation_request,
                execute=lambda: {},
            )
        request_digest = sha256_digest({
            "kind": "goal_test_preparation", "request": operation_request,
        })
        operation_id = "operation_" + sha256_digest({
            "project_id": self.project_id, "request_digest": request_digest,
        })[7:39]

        runner = _SchemaFailureRunner()
        try:
            self.budgeted(runner).run(request)
        except StructuredRoleError:
            pass
        observation = RuntimeObservation(
            thread_id=runner.receipt.thread_id,
            turn_id=runner.receipt.turn_ids[-1],
            active=False,
            terminal_status="completed",
            final_response=json.dumps({
                "step": {"method": "deterministic", "semantic_instruction": "invalid"}
            }),
            payload={"source": "runtime.read"},
        )
        self.assertEqual(
            (operation_id,),
            CoreOperations(self.service).recover_unfinished(self.project_id),
        )
        with self.assertRaisesRegex(
            EngineServiceError, "TERMINAL_MISMATCH"
        ):
            CoreOperations(self.service).observe_terminal_role_failure(
                project_id=self.project_id,
                operation_id=operation_id,
                observation=observation.model_copy(update={"turn_id": "wrong-turn"}),
            )
        with self.service.ledger.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE entity_id=? "
                    "AND event_type='operation.failed'",
                    (operation_id,),
                ).fetchone()[0],
            )
        self.assertEqual(
            "ROLE_SCHEMA_FAILED",
            CoreOperations(self.service).observe_terminal_role_failure(
                project_id=self.project_id,
                operation_id=operation_id,
                observation=observation,
            ),
        )
        self.assertEqual((), CoreOperations(self.service).recover_unfinished(self.project_id))
        with self.service.ledger.read() as connection:
            failed = connection.execute(
                "SELECT payload_json FROM history_events WHERE entity_id=? "
                "AND event_type='operation.failed'",
                (operation_id,),
            ).fetchone()
            project = connection.execute(
                "SELECT run_state,recovery_reason FROM projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
        payload = json.loads(failed["payload_json"])
        self.assertEqual(sha256_digest(observation), payload["terminal_observation_digest"])
        self.assertEqual(observation.model_dump(mode="json"), payload["terminal_observation"])
        self.assertEqual(("idle", None), (project["run_state"], project["recovery_reason"]))

    def test_timeout_and_terminal_mismatch_remain_unknown(self):
        request = self.request()
        runner = _SchemaFailureRunner(status="timed_out")
        operation_request = {"role_request": request.model_dump(mode="json")}
        with self.assertRaises(StructuredRoleError):
            CoreOperations(self.service).invoke(
                project_id=self.project_id,
                kind="goal_test_preparation",
                request=operation_request,
                execute=lambda: self.budgeted(runner).run(request),
            )
        unknown = CoreOperations(self.service).recover_unfinished(self.project_id)
        self.assertEqual(1, len(unknown))
        with self.service.ledger.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type='operation.failed'"
                ).fetchone()[0],
            )

        with self.assertRaises(EngineServiceError):
            CoreOperations(self.service).observe_terminal_role_failure(
                project_id=self.project_id,
                operation_id=unknown[0],
                observation=RuntimeObservation(
                    thread_id="wrong-thread", turn_id="wrong-turn", active=False,
                    terminal_status="completed", final_response="{}", payload={},
                ),
            )


if __name__ == "__main__":
    unittest.main()
