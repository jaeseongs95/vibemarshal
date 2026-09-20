"""agent-governance-suite 플러그인 적합성 검사(flowmarshal.engine.governance_conformance)와 그 호출 지점 검사.

흉내 플러그인으로 검사 항목별 PASS·FAIL 분류와 격리·합성 attestation 불변조건을 확인한다. 실제 플러그인 검사는
FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT가 host-integration.json이 있는 plugin root를 가리킬 때만 실행한다.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.engine import governance_conformance, governance_gate
from flowmarshal.engine.e2e_qualification import QualificationRunError, _observe_frozen_plugin_identity
from flowmarshal.engine.governance_conformance import (
    CHECK_SET_DIGEST, CHECKS, PROBE_CLASS, PROBE_MODEL, first_failure, run_conformance,
)
from flowmarshal.engine.governance_gate import (
    GovernanceContractMismatch, GovernanceRejected, GovernanceTimeout, GovernanceUnavailable,
)
from tests.test_engine_governance_gate import PLUGIN_ROOT, FakePlugin, mcp_result, plugin_unavailable

RECORD_STAGES = {"record_baseline": "stage-1", "record_implementation": "stage-2", "record_scope": "stage-3",
                 "record_acceptance": "stage-4"}


class ConformanceFake(FakePlugin):
    """gate가 기대는 대로 동작하는 흉내 플러그인. broken에 검사 ID를 주면 그 표면만 기대와 다르게 답한다."""

    def __init__(self, root, state_dir, classes, broken=None) -> None:
        super().__init__()
        self.root, self.state_dir, self.classes_path, self.broken = root, state_dir, classes, broken
        self.classes = json.loads(Path(classes).read_text(encoding="utf-8"))
        self.closed = False

    def call(self, tool, arguments, observation):
        if tool == "plan_workflow" and observation is None and self.broken != "attestation_required":
            self.calls.append((tool, arguments, observation))
            return mcp_result({"ok": False, "data": None, "error": {"code": "BINDING_REQUIRED", "message": "token"}})
        if tool == "record_stage_result" and RECORD_STAGES.get(self.broken) == arguments["stageId"]:
            self.calls.append((tool, arguments, observation))
            return mcp_result({"ok": False, "data": None, "error": {"code": "INVALID_INPUT", "message": "digest format"}})
        if tool == "abort_workflow" and self.broken == "abort":
            self.calls.append((tool, arguments, observation))
            return mcp_result({"ok": True, "data": {"state": "running"}, "error": None})
        return super().call(tool, arguments, observation)

    def envelope(self, tool, arguments):
        reply = super().envelope(tool, arguments)
        broken = {"plan": ("plan_workflow", {"executionMode": "direct"}), "root": ("open_convergence_root", {"rootId": 7}),
                  "start": ("start_guarded_workflow", {"revision": None}), "finalize": ("finalize_workflow", {"state": "failed"})}
        if self.broken in broken and broken[self.broken][0] == tool:
            reply["data"].update(broken[self.broken][1])
        if self.broken == "claim" and tool == "claim_workflow_attempt":
            return {"ok": False, "data": None, "error": {"code": "LEASE_CONFLICT", "message": "busy"}}
        return reply

    def script(self, name, request_path):
        output = super().script(name, request_path)
        if self.broken == "baseline" and name == "scope-baseline":
            output["entries"] = "not-a-list"
        if name == "scope-compare":
            unplanned = bool(output["findings"])
            if (self.broken == "scope_out_of_scope" and unplanned) or (self.broken == "scope_in_scope" and not unplanned):
                output["verdict"] = "PASS" if unplanned else "NEEDS_APPROVAL"
        if name == "acceptance-cli":
            refuted = output["verdict"] != "PASS"
            if (self.broken == "acceptance_refuted" and refuted) or (self.broken == "acceptance" and not refuted):
                output["verdict"] = "PASS" if refuted else "FAIL"
        return output

    def close(self):
        self.closed = True


class ConformanceModuleTests(unittest.TestCase):
    def run_with(self, broken=None, **kwargs):
        made: list[ConformanceFake] = []

        def factory(root, state_dir, classes):
            made.append(ConformanceFake(root, state_dir, classes, broken))
            return made[-1]

        return run_conformance(Path("unused-plugin-root"), plugin_factory=factory, **kwargs), made[0]

    def test_conforming_plugin_passes_every_check_in_isolated_resources(self) -> None:
        result, fake = self.run_with()
        self.assertEqual("PASS", result["verdict"], first_failure(result))
        self.assertEqual(list(CHECKS), [item["id"] for item in result["checks"]])
        self.assertEqual({"PASS"}, {item["status"] for item in result["checks"]})
        self.assertEqual(("flowmarshal-governance-conformance-v1", "local_derived", CHECK_SET_DIGEST),
                         (result["format"], result["provenance"], result["check_set_digest"]))
        self.assertEqual(fake.identity["summary"], result["identity"])
        # 임시 자원만 쓰고 끝나면 지운다. 플러그인 state도 그 안이다.
        self.assertTrue(fake.closed)
        self.assertFalse(Path(fake.state_dir).parent.exists())
        self.assertEqual(Path(fake.state_dir).parent, Path(fake.classes_path).parent)
        self.assertTrue(Path(fake.state_dir).parent.name.startswith("flowmarshal-conformance-"))
        # 합성 attestation: probe 전용 model·class·actorId만 제출한다.
        self.assertEqual({PROBE_MODEL: PROBE_CLASS}, fake.classes["classes"])
        observations = [observation for _, _, observation in fake.calls if observation is not None]
        self.assertEqual({(PROBE_MODEL, "flowmarshal-engine:conformance-probe")},
                         {(item["model"], item["actorId"]) for item in observations})
        self.assertEqual(result["probe"], {"model": PROBE_MODEL, "modelClass": PROBE_CLASS,
                                           "actorId": "flowmarshal-engine:conformance-probe"})
        # 두 digest 형식: 범위 stage는 16진수, 수용 근거 stage는 sha256: 접두사.
        digests = {arguments["stageId"]: [item["digest"] for item in arguments["output"]["artifacts"]]
                   for tool, arguments, _ in fake.calls if tool == "record_stage_result"}
        self.assertTrue(all(not value.startswith("sha256:") for value in digests["stage-3"]))
        self.assertTrue(digests["stage-4"] and all(value.startswith("sha256:") for value in digests["stage-4"]))
        # 서로 다른 두 스냅샷 commit을 commit 모드로 비교한다.
        compares = [request for name, request in fake.scripts if name == "scope-compare"]
        self.assertEqual(2, len({request["commit"] for request in compares}))
        self.assertTrue(all(request["comparisonTarget"] == "commit" for request in compares))

    def test_each_unexpected_behavior_fails_its_own_check_without_raising(self) -> None:
        for check_id in CHECKS:
            with self.subTest(check=check_id):
                result, _ = self.run_with(check_id)
                self.assertEqual("FAIL", result["verdict"])
                failed = first_failure(result)
                self.assertEqual((check_id, "FAIL"), (failed["id"], failed["status"]), failed)
                self.assertTrue(failed["expected"] and failed["observed"])
                # 실패한 검사보다 앞의 항목은 모두 PASS다.
                before = result["checks"][:list(CHECKS).index(check_id)]
                self.assertEqual({"PASS"} if before else set(), {item["status"] for item in before})

    def test_environment_failures_are_retryable_not_results(self) -> None:
        def failing(error):
            def factory(root, state_dir, classes):
                fake = ConformanceFake(root, state_dir, classes)
                fake.failures["open_convergence_root"] = error
                return fake
            return factory

        cases = ((GovernanceTimeout("GOVERNANCE_TIMEOUT: MCP tools/call: 120초"), "GOVERNANCE_TIMEOUT: MCP"),
                 (GovernanceUnavailable("MCP_SERVER_START_FAILED: gone"), "MCP_SERVER_START_FAILED"),
                 (RuntimeError("bug"), "CONFORMANCE_ERROR"))
        for error, fragment in cases:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(GovernanceUnavailable) as raised:
                    run_conformance(Path("unused"), plugin_factory=failing(error))
                self.assertIn(fragment, str(raised.exception))
                # CoreOperations는 effects_started가 False인 예외만 no_effect로 남긴다.
                self.assertIs(False, raised.exception.effects_started)
        with self.assertRaises(GovernanceUnavailable) as raised:
            self.run_with(deadline_seconds=-1)
        self.assertIn("GOVERNANCE_TIMEOUT: conformance", str(raised.exception))

    def test_failures_outside_the_probe_are_also_retryable(self) -> None:
        """임시 디렉터리 생성·대응표 쓰기·플러그인 생성·정리의 예외도 결과 없는 효과가 아니라 다시 시도할 실패다."""
        def fail(*args, **kwargs):
            raise OSError("생성·정리 실패")

        def closing(root, state_dir, classes):
            fake = ConformanceFake(root, state_dir, classes)
            fake.failures["open_convergence_root"] = GovernanceUnavailable("MCP_SERVER_EXITED: probe 도중 종료")
            fake.close = fail
            return fake

        def only_close_fails(root, state_dir, classes):
            fake = ConformanceFake(root, state_dir, classes)
            fake.close = fail
            return fake

        # 정리 실패는 진행 중인 예외를 덮지 않는다.
        with self.assertRaises(GovernanceUnavailable) as raised:
            run_conformance(Path("unused"), plugin_factory=closing)
        self.assertIn("MCP_SERVER_EXITED", str(raised.exception))
        self.assertIs(False, raised.exception.effects_started)
        # 플러그인 생성(state_dir 만들기 포함) 실패.
        with self.assertRaises(GovernanceUnavailable) as raised:
            run_conformance(Path("unused"), plugin_factory=fail)
        self.assertIn("CONFORMANCE_ERROR", str(raised.exception))
        self.assertIs(False, raised.exception.effects_started)
        # 임시 디렉터리 생성 실패.
        with patch.object(governance_conformance.tempfile, "TemporaryDirectory", side_effect=OSError("임시 자원 없음")):
            with self.assertRaises(GovernanceUnavailable) as raised:
                self.run_with()
        self.assertIn("CONFORMANCE_ERROR", str(raised.exception))
        self.assertIs(False, raised.exception.effects_started)
        # 정리만 실패하면 이미 관측한 검사 항목으로 정해진 결과를 그대로 돌려준다.
        result = run_conformance(Path("unused"), plugin_factory=only_close_fails)
        self.assertEqual("PASS", result["verdict"])
        self.assertIn("생성·정리 실패", result["cleanup_error"])

    def test_probe_model_class_write_failure_is_retryable(self) -> None:
        original = governance_conformance._write

        def fail_probe_classes(path, value):
            if Path(path).name == "probe-model-classes.json":
                raise OSError("대응표 기록 실패")
            return original(path, value)

        with patch.object(governance_conformance, "_write", side_effect=fail_probe_classes):
            with self.assertRaises(GovernanceUnavailable) as raised:
                self.run_with()
        self.assertIn("대응표 기록 실패", str(raised.exception))
        self.assertIs(False, raised.exception.effects_started)

    def test_deterministic_rejection_is_not_reclassified_as_retryable(self) -> None:
        rejection = GovernanceRejected("CONFORMANCE_REJECTED")
        with self.assertRaises(GovernanceRejected) as raised:
            governance_gate.conformance_result(lambda: (_ for _ in ()).throw(rejection))
        self.assertIs(rejection, raised.exception)

    def test_preflight_mismatch_is_reported_as_a_contract_mismatch(self) -> None:
        def factory(root, state_dir, classes):
            fake = ConformanceFake(root, state_dir, classes)
            fake.preflight_failure = GovernanceContractMismatch("manifest_format", "v1", "v9")
            return fake

        with self.assertRaisesRegex(GovernanceContractMismatch, "manifest_format"):
            run_conformance(Path("unused"), plugin_factory=factory)

    def test_module_is_framework_free_and_names_no_real_model(self) -> None:
        source = Path(governance_conformance.__file__).read_text(encoding="utf-8")
        for name in ("import unittest", "import pytest", "from unittest", "from tests", "claude-", "gpt-", "sonnet", "opus", "haiku"):
            self.assertNotIn(name, source)
        self.assertNotIn("flowmarshal.core", source)
        self.assertNotIn("flowmarshal.planning", source)


class CallSiteTests(unittest.TestCase):
    def result(self, verdict):
        checks = [{"id": "plan", "status": verdict, "expected": "orchestrated", "observed": "direct"}]
        return {"format": "flowmarshal-governance-conformance-v1", "provenance": "local_derived", "verdict": verdict,
                "check_set_digest": CHECK_SET_DIGEST, "identity": {}, "checks": checks}

    def cli(self, argv, outcome):
        from flowmarshal.engine import cli

        emitted = []
        effect = outcome if isinstance(outcome, Exception) else None
        with patch.object(governance_conformance, "run_conformance", side_effect=effect,
                          return_value=None if effect else outcome) as run, \
                patch.object(cli, "_emit", side_effect=emitted.append), \
                patch.object(cli, "_service", side_effect=AssertionError("원장을 열지 않는다")), \
                patch.dict("os.environ", {}, clear=False) as environ:
            environ.pop(governance_gate.PLUGIN_ROOT_ENV, None)
            code = cli.main(argv)
        return code, emitted, run

    def test_adoption_command_reports_the_result_and_sets_the_exit_code(self) -> None:
        argv = ["governance", "check-plugin", "--plugin-root", "D:/plugin"]
        code, emitted, run = self.cli(argv, self.result("PASS"))
        self.assertEqual((0, "PASS"), (code, emitted[0]["verdict"]))
        run.assert_called_once_with(Path("D:/plugin"))
        code, emitted, _ = self.cli(argv, self.result("FAIL"))
        self.assertEqual((1, "FAIL", "plan"), (code, emitted[0]["verdict"], emitted[0]["checks"][0]["id"]))
        for error in (GovernanceContractMismatch("manifest", "JSON", "없음"), GovernanceUnavailable("GOVERNANCE_TIMEOUT: x")):
            code, emitted, _ = self.cli(argv, error)
            self.assertEqual(2, code)
            self.assertIn(str(error), emitted[0]["message"])
        code, emitted, run = self.cli(["governance", "check-plugin"], self.result("PASS"))
        self.assertEqual(2, code)
        self.assertIn("GOVERNANCE_PLUGIN_ROOT_REQUIRED", emitted[0]["message"])
        run.assert_not_called()

    def test_e2e_reuses_frozen_conformance_and_only_rebinds_exact_identity(self) -> None:
        fields = {
            "closure_tree_digest": "sha256:" + "1" * 64,
            "entrypoint_table_digest": "sha256:" + "2" * 64,
            "check_set_digest": CHECK_SET_DIGEST,
            "conformance_result_digest": "sha256:" + "3" * 64,
        }
        frozen = SimpleNamespace(
            **fields,
            e2e_identity_digest=governance_gate.sha256_digest(fields),
            manifest_sha256="sha256:" + "4" * 64,
            node_version="v22.13.0",
            plugin_version_label="2.1.2",
            server_info={"name": "frozen"},
            installation_root=str(Path("D:/other-plugin").resolve()),
        )
        identity = {
            "summary": {
                "closure_tree_digest": fields["closure_tree_digest"],
                "entrypoint_table_digest": fields["entrypoint_table_digest"],
                "manifest_sha256": "sha256:" + "9" * 64,
                "node_version": "v22.14.0",
            },
            "labels": [
                {"source": "plugin_manifest_file", "plugin": {"version": "2.1.3"}},
                {"source": "mcp_server_info", "serverInfo": {"name": "changed"}},
            ],
        }
        governance = SimpleNamespace(
            plugin_root=Path("D:/plugin"),
            inspect_identity=lambda: identity,
            check_conformance=lambda: (_ for _ in ()).throw(AssertionError("재실행 금지")),
        )
        observed = _observe_frozen_plugin_identity(governance, frozen)
        self.assertEqual(frozen.e2e_identity_digest, observed["governance_plugin_identity_digest"])
        self.assertTrue(all(observed["audit_only_changes"].values()))
        identity["summary"]["closure_tree_digest"] = "sha256:" + "8" * 64
        with self.assertRaisesRegex(QualificationRunError, "E2E_GOVERNANCE_PLUGIN_IDENTITY_MISMATCH"):
            _observe_frozen_plugin_identity(governance, frozen)
        # harness는 cell loop 전에 frozen identity를 대조하며 conformance를 다시 실행하지 않는다.
        from flowmarshal.engine import e2e_qualification

        source = Path(e2e_qualification.__file__).read_text(encoding="utf-8")
        body = source[source.index("def run_project_e2e"):]
        self.assertNotIn("governance.check_conformance", body)
        self.assertLess(body.index("_observe_frozen_plugin_identity(governance, governance_freeze)"),
                        body.index("for index, scenario in enumerate(E2E_SCENARIOS)"))


@unittest.skipIf(plugin_unavailable(), plugin_unavailable() or "")
class RealPluginConformanceTests(unittest.TestCase):
    def test_real_plugin_conforms(self) -> None:
        result = run_conformance(PLUGIN_ROOT)
        self.assertEqual("PASS", result["verdict"], first_failure(result))
        self.assertEqual("local_derived", result["identity"]["provenance"])
        print(f"\nreal plugin conformance: {result['duration_seconds']}s, tree {result['identity']['closure_tree_digest']}")


if __name__ == "__main__":
    unittest.main()
