"""supervisor 경로의 ContextRequest 자동 해소와 후속 준비 job 하나를 고정한다."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.domain import (
    ExecutionContextNeed,
    RunOnceAction,
    RuntimeJobKind,
    TaskExecutionSpecRevision,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import FakeCodexRuntime, RuntimeJobSupervisor
from flowmarshal.engine.worker_prompt import PromptArtifactStore
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]
HIDDEN = "def hidden_selector():\n    return 42\n"


class ContextFollowupJobTests(unittest.TestCase):
    """초기 Project Map 밖 파일에만 있는 symbol을 준비 역할이 요청하는 상황."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        workspace, _ = _copy_fixture(ROOT, base)
        (workspace / "z_hidden.py").write_text(HIDDEN, encoding="utf-8")
        self.inventory = qualification_inventory()
        self.roles = default_role_configuration(ROOT)
        self.prepared = _prepare(
            workspace=workspace, state_root=base / "state",
            inventory=self.inventory, roles=self.roles,
        )
        self.runtime = FakeCodexRuntime(self.inventory)
        self.hidden = workspace / "z_hidden.py"

    def context_request(self, reason: str = "초기 관측 Project Map에 본문이 없습니다.") -> dict:
        return {
            "proposal": None,
            "context_request": {
                "task_id": self.prepared.task_id,
                "missing_needs": [{
                    "need_id": "hidden",
                    "description": "hidden_selector 구현 본문",
                    "path_hints": [],
                    "symbol_hints": ["hidden_selector"],
                    "tag_hints": [],
                    "required": True,
                }],
                "reason": reason,
            },
        }

    def proposal(self, *unmapped_paths: str) -> dict:
        # 후속 proposal은 z_hidden.py를 언급하지 않는다. 결속은 Core가 맡는다.
        # unmapped_paths는 ContextRequest 대신 Map 밖 경로를 context_needs에 바로 적은 경우다.
        proposal = self.prepared.proposal.model_copy(update={
            "context_needs": self.prepared.proposal.context_needs + tuple(
                ExecutionContextNeed(
                    need_id=Path(path).stem, description=f"{path} 본문", path_hints=(path,),
                )
                for path in unmapped_paths
            ),
        }).model_dump(mode="json")
        for step in proposal["validation_steps"]:
            step.pop("method")
            step.pop("required_evidence_kinds")
        return {"proposal": proposal, "context_request": None}

    def application(self, runner, supervisor=None) -> EngineApplication:
        application = EngineApplication(
            self.prepared.service,
            runtime=self.runtime,
            role_configuration=self.roles,
            structured_runner=runner,
            governance=ALLOW_ALL,
            supervisor=supervisor,
        )
        self.addCleanup(application.supervisor.close, timeout_seconds=0.2)
        return application

    def drive(self, application, *, until=(RunOnceAction.MATERIALIZED, RunOnceAction.BLOCKED)):
        outcomes = []
        for _ in range(80):
            outcome = application.run_once(self.prepared.project_id)
            outcomes.append(outcome)
            if outcome.action in until:
                return outcome, outcomes
            time.sleep(0.01)
        self.fail(f"run_once가 80회 안에 끝나지 않았다: {[item.action for item in outcomes]}")

    def jobs(self) -> list[dict]:
        with self.prepared.service.ledger.read() as connection:
            return [
                {**dict(row), "request": json.loads(row["request_json"])}
                for row in connection.execute(
                    "SELECT * FROM runtime_jobs WHERE project_id=? AND kind=? ORDER BY rowid",
                    (self.prepared.project_id, RuntimeJobKind.EXECUTION_SPEC_PREPARE.value),
                )
            ]

    def current_spec(self) -> TaskExecutionSpecRevision | None:
        with self.prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM execution_spec_revisions WHERE task_id=? AND is_current=1",
                (self.prepared.task_id,),
            ).fetchone()
        return None if row is None else TaskExecutionSpecRevision.model_validate_json(row[0])

    def test_supervisor_resolves_request_with_one_followup_job_and_binds_body(self) -> None:
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.context_request(), self.proposal()],
        })
        outcome, _ = self.drive(self.application(runner))

        self.assertEqual(RunOnceAction.MATERIALIZED, outcome.action, outcome.detail)
        # job 하나 = turn 하나: 첫 job과 후속 job이 각각 역할을 한 번씩 부른다.
        self.assertEqual(2, len(runner.calls))
        additional = runner.calls[1].payload["additional_context"]
        self.assertEqual(["z_hidden.py"], [item["source_ref"] for item in additional])
        self.assertIn("return 42", additional[0]["content"])
        first, followup = self.jobs()
        self.assertEqual(["consumed", "consumed"], [first["status"], followup["status"]])
        self.assertIn(":context:", followup["checkpoint_key"])
        digest = sha256_bytes(self.hidden.read_bytes())
        self.assertEqual(
            [{"source_ref": "z_hidden.py", "selector": "python-lines:1-2", "content_digest": digest}],
            followup["request"]["context_resolution"],
        )
        self.assertEqual("hidden", followup["request"]["context_request"]["missing_needs"][0]["need_id"])
        # 해소한 파일·symbol은 lazy Project Map entry로 남는다.
        project_map = self.prepared.service.load_current_project_map(self.prepared.project_id)
        entry = next(item for item in project_map.entries if item.path == "z_hidden.py")
        self.assertEqual(("hidden_selector",), entry.symbols)
        self.assertEqual(digest, entry.content_digest)
        # 실제 fragment 본문과 전체 파일 digest가 Execution Spec·Worker PromptBundle에 결속된다.
        spec = self.current_spec()
        fragment = next(
            item for item in spec.definition.context_manifest.fragments
            if item.source_ref == "z_hidden.py"
        )
        self.assertEqual("python-lines:1-2", fragment.selector)
        self.assertEqual(digest, fragment.content_digest)
        self.assertEqual(project_map.revision_digest, spec.definition.project_map_digest)
        bundle = PromptArtifactStore(self.prepared.service.ledger.artifact_root).load(
            spec.definition.context_manifest.prompt_binding
        )
        self.assertIn('source="z_hidden.py#python-lines:1-2"', bundle.dynamic_suffix)
        self.assertIn("return 42", bundle.dynamic_suffix)

    def test_followup_job_resumes_from_checkpoint_after_restart(self) -> None:
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.context_request(), self.proposal()],
        })
        first = self.application(runner)
        for _ in range(80):
            first.run_once(self.prepared.project_id)
            if len(self.jobs()) == 2:
                break
            time.sleep(0.01)
        followup = self.jobs()[1]
        first.supervisor._workers[followup["id"]].join(1)
        self.assertEqual(2, len(runner.calls))

        restarted = self.application(runner, supervisor=RuntimeJobSupervisor(
            self.prepared.service, self.runtime,
        ))
        outcome, _ = self.drive(restarted)

        self.assertEqual(RunOnceAction.MATERIALIZED, outcome.action, outcome.detail)
        self.assertEqual(2, len(runner.calls))
        self.assertEqual([followup["id"]], [job["id"] for job in self.jobs()][1:])
        self.assertIn("z_hidden.py", [
            item.source_ref for item in self.current_spec().definition.context_manifest.fragments
        ])

    def test_second_context_request_from_followup_stops_with_question(self) -> None:
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.context_request(), self.context_request()],
        })
        application = self.application(runner)
        outcome, _ = self.drive(application)

        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("CONTEXT_REQUIRED", outcome.blocker_code)
        again = application.run_once(self.prepared.project_id)
        self.assertEqual("CONTEXT_REQUIRED", again.blocker_code)
        # 후속은 한 번뿐이다. 같은 해소를 반복하는 세 번째 job·turn은 없다.
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(2, len(self.jobs()))
        self.assertIsNone(self.current_spec())

    def test_unmapped_context_need_in_proposal_gets_one_followup_job(self) -> None:
        """ContextRequest 없이 Map 밖 source를 context_needs에 적어도 같은 후속 job 한 번으로 해소한다."""

        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.proposal("z_hidden.py"), self.proposal()],
        })
        outcome, _ = self.drive(self.application(runner))

        self.assertEqual(RunOnceAction.MATERIALIZED, outcome.action, outcome.detail)
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(
            ["z_hidden.py"],
            [item["source_ref"] for item in runner.calls[1].payload["additional_context"]],
        )
        first, followup = self.jobs()
        self.assertNotIn(":context:", first["checkpoint_key"])
        self.assertIn(":context:", followup["checkpoint_key"])
        self.assertEqual(
            ["z_hidden.py"],
            [item["source_ref"] for item in followup["request"]["context_resolution"]],
        )
        fragment = next(
            item for item in self.current_spec().definition.context_manifest.fragments
            if item.source_ref == "z_hidden.py"
        )
        self.assertEqual(sha256_bytes(self.hidden.read_bytes()), fragment.content_digest)

    def test_unmapped_context_need_from_followup_stops_with_question(self) -> None:
        (self.hidden.parent / "z_other.py").write_text("OTHER = 1\n", encoding="utf-8")
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.context_request(), self.proposal("z_other.py")],
        })
        application = self.application(runner)
        outcome, _ = self.drive(application)

        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("CONTEXT_REQUIRED", outcome.blocker_code)
        self.assertIn("z_other.py", outcome.detail)
        # 후속 job 결과의 Map 밖 need는 다시 해소하지 않는다.
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(2, len(self.jobs()))
        self.assertIsNone(self.current_spec())

    def test_preference_request_is_still_a_question_without_followup(self) -> None:
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.context_request("사용자 선호를 확인해야 합니다.")],
        })
        outcome, _ = self.drive(self.application(runner))

        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("CONTEXT_REQUIRED", outcome.blocker_code)
        self.assertEqual(1, len(runner.calls))
        self.assertEqual(1, len(self.jobs()))
        project_map = self.prepared.service.load_current_project_map(self.prepared.project_id)
        self.assertNotIn("z_hidden.py", [item.path for item in project_map.entries])

    def test_resolved_source_change_during_followup_job_is_stale(self) -> None:
        # Map 확장이 후속 job 시작 전에 끝나므로 adapter의 호출 전후 비교가 해소 파일을 본다.
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.context_request(), self.proposal()],
        })
        run = runner.run

        def change_hidden_during_followup(*args, **kwargs):
            result = run(*args, **kwargs)
            if len(runner.calls) == 2:
                self.hidden.write_text(HIDDEN.replace("42", "43"), encoding="utf-8")
            return result

        runner.run = change_hidden_during_followup
        application = self.application(runner)
        for _ in range(30):
            outcome = application.run_once(self.prepared.project_id)
            self.assertNotEqual(RunOnceAction.MATERIALIZED, outcome.action)
            time.sleep(0.01)

        followup = self.jobs()[1]
        with self.prepared.service.ledger.read() as connection:
            observations = [row[0] for row in connection.execute(
                "SELECT payload_json FROM runtime_job_observations WHERE job_id=?",
                (followup["id"],),
            )]
        self.assertTrue(any("STALE_EXECUTION_INPUT" in item for item in observations))
        self.assertIsNone(self.current_spec())
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(2, len(self.jobs()))

    def test_resolved_source_change_after_materialize_blocks_dispatch_as_stale(self) -> None:
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [self.context_request(), self.proposal()],
        })
        application = self.application(runner)
        outcome, _ = self.drive(application)
        self.assertEqual(RunOnceAction.MATERIALIZED, outcome.action, outcome.detail)

        self.hidden.write_text(HIDDEN.replace("42", "43"), encoding="utf-8")
        blocked = application.run_once(self.prepared.project_id)

        self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
        self.assertEqual("STALE_EXECUTION_INPUT", blocked.blocker_code)
        self.assertIn("z_hidden.py", blocked.detail)
        self.assertEqual(0, self.runtime.create_calls)


if __name__ == "__main__":
    unittest.main()
