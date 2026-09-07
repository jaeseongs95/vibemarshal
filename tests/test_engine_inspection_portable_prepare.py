"""Portable R-S06 prepare가 실제 계약·입력 materialization까지 도달하는 회귀."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.model_lock import ModelCapability, ModelInventory, RUNTIME_CAPABILITIES
from flowmarshal.engine.qualification import EvaluationRunStatus, EvaluationScope
from flowmarshal.engine.runtime import ExecutionPolicyEvidence, RuntimeOperationReceipt
from scripts.diagnostics import r_s06_10
from tests.test_engine_inspection_materialization import _detached_package


class _PrepareRuntime:
    def __init__(self, *, run: Path, phase: str, codex_bin: Path, project_binding=None, **_kwargs) -> None:
        self.run = run
        self.phase = phase
        self.capture = r_s06_10.claim_runtime_phase(run, phase)
        self.project_binding = project_binding
        self.executable_digest = sha256_bytes(codex_bin.read_bytes())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def verify_execution_policy(self, cwd):
        return ExecutionPolicyEvidence(
            permission_profile=":danger-full-access", approval_policy="never",
            config_digest="sha256:" + "1" * 64, profile_catalog_digest="sha256:" + "2" * 64,
            cwd=str(Path(cwd).resolve()),
        )

    def list_models(self):
        return ModelInventory(
            source="portable-prepare-fake", executable_digest=self.executable_digest,
            runtime_capabilities=RUNTIME_CAPABILITIES,
            models=(
                ModelCapability(model="gpt-5.6-luna", supported_efforts=("medium", "high")),
                ModelCapability(model="gpt-5.6-terra", supported_efforts=("high",)),
                ModelCapability(model="gpt-5.6-sol", supported_efforts=("xhigh", "high")),
            ),
        )


class PortablePrepareIntegrationTests(unittest.TestCase):
    def _write_policy_inputs(self, root: Path, run: Path) -> tuple[Path, Path, Path]:
        budget = root / "budget.json"
        timeout = root / "timeouts.json"
        project = root / "project.json"
        budget.write_text(json.dumps({"schema_version": "1.0", "total_tokens": 1_000_000,
                                      "call_reservation_tokens": 100_000,
                                      "replan_reserve_percent": 25}), encoding="utf-8")
        timeout.write_text(json.dumps({"format": "flowmarshal-role-timeouts-v1",
                                       "default_timeout_seconds": 900}), encoding="utf-8")
        project.write_text(json.dumps({"project_id": "project-portable",
                                       "expected_root": str((run / "workspace").resolve()),
                                       "expected_name": "자동화테스트"}), encoding="utf-8")
        return budget, timeout, project

    def _write_deterministic_gate(self, run: Path) -> None:
        source_digest = r_s06_10.source_manifest_digest(r_s06_10.ROOT)
        report = {
            "scope": EvaluationScope.DETERMINISTIC.value,
            "contract_digest": "sha256:" + "3" * 64,
            "status": EvaluationRunStatus.COMPLETED.value,
            "passed": True,
            "metrics": {"check_count": 5},
            "failures": [],
            "generated_at": "2026-09-07T00:00:00Z",
        }
        r_s06_10.write_new(run / "deterministic/qualification-report.json", report)
        r_s06_10.write_new(run / "deterministic/evaluation-contract.json", {
            "source_manifest_digest": source_digest,
        })

    def test_policy_bound_portable_prepare_materializes_external_reference_and_binds_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for execution_mode, expected_request_count, expected_maximum_calls in (
                ("development-diagnostic", 11, 11), ("qualification", 12, 13),
            ):
                with self.subTest(execution_mode=execution_mode):
                    case_root = root / execution_mode
                    case_root.mkdir()
                    run = case_root / "run"
                    package = _detached_package(case_root)
                    executable = case_root / "codex.exe"
                    executable.write_bytes(b"fake pinned executable")
                    roles_path = r_s06_10.ROOT / "config/qualification-roles.json"
                    roles = r_s06_10.load_role_configuration_input(roles_path)
                    budget, timeout, project = self._write_policy_inputs(case_root, run)
                    workspace_binding = {"format": "portable-test-workspace", "root": str(r_s06_10.ROOT)}
                    with patch("scripts.diagnostics.inspection_workspace.capture_workspace_binding", return_value=workspace_binding), patch(
                        "scripts.diagnostics.inspection_workspace.verify_workspace_binding"
                    ):
                        r_s06_10.portable_preflight(
                            run, fixture_package=package, codex_bin=executable, role_configuration=roles,
                            execution_mode=execution_mode, budget_policy_path=budget,
                            role_timeout_policy_path=timeout, codex_project_binding_path=project,
                            inspection_provider_contract=r_s06_10.PLAN_INSPECTION_PROVIDER_V2,
                        )
                    self._write_deterministic_gate(run)

                    def fake_probe(runtime, **kwargs):
                        self.assertFalse(kwargs["ephemeral"])
                        return RuntimeOperationReceipt(
                            operation_id="instruction-probe",
                            payload={"thread": {"id": "thread-probe", "turns": [], "ephemeral": False,
                                                "projectId": "project-portable"},
                                     "instructionSources": [str(r_s06_10.ROOT / "AGENTS.md")]},
                        )

                    with patch("scripts.diagnostics.inspection_workspace.verify_workspace_binding"), patch.object(
                        r_s06_10, "CapturingRuntime", _PrepareRuntime
                    ), patch.object(r_s06_10.CodexAppServerRuntime, "create_thread", side_effect=fake_probe
                    ), patch.object(r_s06_10, "_planning_contract", wraps=r_s06_10._planning_contract) as contract:
                        r_s06_10.prepare(
                            run, roles, execution_mode=execution_mode, fixture_package=package,
                            codex_bin=executable, budget_policy_path=budget, role_timeout_policy_path=timeout,
                            codex_project_binding_path=project,
                            inspection_provider_contract=r_s06_10.PLAN_INSPECTION_PROVIDER_V2,
                        )
                        r_s06_10.verify_lock(run)

                    lock = r_s06_10.read(run / "preflight.json")
                    self.assertEqual("plan-inspection-v6-scope-boundary",
                                     r_s06_10.read(run / "expectations.json")["fixture_revision"])
                    self.assertTrue((run / "requests/clean.json").is_file())
                    self.assertEqual(expected_request_count, len(list((run / "requests").glob("*.json"))))
                    self.assertEqual(expected_maximum_calls, lock["maximum_provider_turns"])
                    self.assertTrue((run / "workspace/app.py").is_file())
                    self.assertTrue((run / "project-references/entry_ad363844d3392d9ef718d2d6/content.md").is_file())
                    self.assertEqual("project-portable", lock["diagnostic_policy_input"]
                                     ["codex_project_binding"]["project_id"])
                    self.assertIsNotNone(contract.call_args.kwargs["policies"])
                    self.assertEqual(r_s06_10.PLAN_INSPECTION_PROVIDER_V2,
                                     contract.call_args.kwargs["inspection_provider_contract"])

    def test_new_prepare_without_policy_stops_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            with patch.object(r_s06_10, "CapturingRuntime") as runtime, self.assertRaisesRegex(
                RuntimeError, "DIAGNOSTIC_POLICY_REQUIRED_FOR_NEW_PREPARE"
            ):
                r_s06_10.prepare(run, r_s06_10.load_role_configuration_input(r_s06_10.ROOT / "config/qualification-roles.json"))
            runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
