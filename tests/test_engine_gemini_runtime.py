"""Gemini CLI runtime adapter와 provider 선택 테스트.

실제 CLI 대신 `tests/fixtures/engine/fake_gemini.py`를 실행한다. Codex·Claude는 호출하지 않는다.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.gemini_runtime import (
    GEMINI_INVENTORY_SOURCE_PREFIX,
    GeminiModelCatalog,
    GeminiRuntime,
    gemini_turn_usage,
    project_gemini_usage,
)
from flowmarshal.engine.model_observation import (
    authoritative_model_observation,
    authoritative_receipt_model_observation,
)
from flowmarshal.engine.providers import (
    RuntimeProviderSelection,
    add_provider_arguments,
    open_harness_runtime,
    provider_run_metadata,
    selection_from_arguments,
    selection_from_run_metadata,
)
from flowmarshal.engine.runtime import (
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    CodexRuntimePort,
    RuntimePolicyError,
)

FAKE_GEMINI = Path(__file__).resolve().parent / "fixtures" / "engine" / "fake_gemini.py"
MODEL = "fake-gemini-model"
OTHER_MODEL = "fake-gemini-other"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _catalog() -> GeminiModelCatalog:
    return GeminiModelCatalog.model_validate(
        {
            "format": "flowmarshal-gemini-model-catalog-v1",
            "models": [
                {"model": MODEL, "supported_efforts": ["low", "high"]},
                {"model": OTHER_MODEL, "supported_efforts": ["medium"]},
            ],
        }
    )


class GeminiRuntimeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.config_dir = root / "gemini-config"
        self.state_root = root / "state"
        self.log = root / "argv.jsonl"
        self.env = {
            **os.environ,
            "GEMINI_CONFIG_DIR": str(self.config_dir),
            "FAKE_GEMINI_LOG": str(self.log),
        }
        self.runtimes: list[GeminiRuntime] = []

    def tearDown(self) -> None:
        for runtime in self.runtimes:
            runtime.close(timeout_seconds=5)
        self._tmp.cleanup()

    def runtime(self, **env: str) -> GeminiRuntime:
        runtime = GeminiRuntime(
            model_catalog=_catalog(),
            gemini_bin=FAKE_GEMINI,
            interpreter=sys.executable,
            state_root=self.state_root,
            config_dir=self.config_dir,
            turn_start_timeout_seconds=20,
            env={**self.env, **env},
        )
        self.runtimes.append(runtime)
        return runtime

    def invocations(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        return [json.loads(line)["argv"] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def thread(self, runtime: GeminiRuntime, *, ephemeral: bool = False) -> str:
        receipt = runtime.create_thread(
            cwd=self.workspace, title="t", model=MODEL,
            developer_instructions="역할 지침", ephemeral=ephemeral,
        )
        return receipt.operation_id

    def finish(self, runtime: GeminiRuntime) -> None:
        self.assertTrue(runtime.wait_for_active_turns(timeout_seconds=20))


class GeminiRuntimePortTests(GeminiRuntimeTestBase):
    def test_implements_every_runtime_port_method_with_same_keywords(self) -> None:
        for name, member in inspect.getmembers(CodexRuntimePort, inspect.isfunction):
            if name.startswith("_"):
                continue
            implementation = getattr(GeminiRuntime, name, None)
            self.assertIsNotNone(implementation, name)
            expected = list(inspect.signature(member).parameters)
            actual = list(inspect.signature(implementation).parameters)
            self.assertEqual(expected, actual, name)

    def test_inventory_is_configured_catalog_with_executable_digest(self) -> None:
        runtime = self.runtime()
        inventory = runtime.list_models()
        self.assertEqual("configured_catalog", inventory.inventory_provenance)
        self.assertTrue(inventory.source.startswith(GEMINI_INVENTORY_SOURCE_PREFIX))
        self.assertEqual(runtime.executable_digest, inventory.executable_digest)
        self.assertTrue(inventory.supports(MODEL, "high"))
        self.assertFalse(inventory.supports(MODEL, "medium"))
        self.assertEqual("configured_catalog", inventory.raw_response["inventorySource"])

    def test_cli_version_is_bound_into_inventory_capabilities(self) -> None:
        runtime = self.runtime()
        self.assertEqual("0.66.0-fake", runtime.cli_version)
        inventory = runtime.list_models()
        self.assertEqual("0.66.0-fake", inventory.raw_response["cliVersion"])
        contracts = {item.contract for item in inventory.runtime_capabilities}
        self.assertIn("gemini-cli-stream-json-v1@0.66.0-fake", contracts)

    def test_execution_policy_uses_engine_policy_identifiers(self) -> None:
        evidence = self.runtime().verify_execution_policy(self.workspace)
        self.assertEqual(REQUIRED_PERMISSION_PROFILE, evidence.permission_profile)
        self.assertEqual(REQUIRED_APPROVAL_POLICY, evidence.approval_policy)

    def test_policy_values_are_local_derived_not_provider_observed(self) -> None:
        runtime = self.runtime()
        document = runtime._policy_document()
        self.assertEqual("local_derived", document["engine_policy"]["provenance"])
        self.assertTrue(document["safe_mode"])
        thread_id = self.thread(runtime)
        payload = runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt="hi", model=MODEL, effort="low",
        ).payload
        self.assertEqual("local_derived", payload["permission_profile_provenance"])
        self.assertEqual("local_derived", payload["approval_policy_provenance"])
        self.assertEqual("provider_observed", payload["provider_permission_mode_provenance"])
        self.assertEqual("0.66.0-fake", payload["provider_cli_version"])


class GeminiRuntimeTurnTests(GeminiRuntimeTestBase):
    def test_structured_turn_completes_with_provider_observations(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        receipt = runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt='JSON:{"ok": true}',
            model=MODEL, effort="high", output_schema=SCHEMA,
        )
        payload = receipt.payload
        self.assertEqual(receipt.binding.turn_id, receipt.operation_id)
        self.assertEqual("provider_replayed_user_message_uuid", payload["turn_id_provenance"])
        self.assertEqual("bypassPermissions", payload["provider_permission_mode"])
        self.assertEqual(REQUIRED_PERMISSION_PROFILE, payload["permission_profile"])
        self.assertTrue(payload["first_empty_thread"])
        self.finish(runtime)
        observation = runtime.read(thread_id=thread_id)
        self.assertFalse(observation.active)
        self.assertEqual("completed", observation.terminal_status)
        self.assertEqual({"ok": True}, json.loads(observation.final_response or ""))
        self.assertEqual(receipt.operation_id, observation.turn_id)
        usage = observation.payload["usage"]
        self.assertEqual(
            {"inputTokens": 510, "cachedInputTokens": 300, "outputTokens": 40, "reasoningOutputTokens": 15},
            usage,
        )
        self.assertNotIn("totalTokens", usage)
        self.assertEqual("turn", observation.payload["usage_scope"])
        self.assertEqual("success_result_usage_with_iterations", observation.payload["usage_scope_basis"])
        self.assertEqual("session_cumulative", observation.payload["provider_model_usage_scope"])
        self.assertEqual([], observation.payload["provider_permission_denials"])
        self.assertEqual([MODEL], observation.payload["provider_reported_models"])
        self.assertEqual((MODEL, "high"), authoritative_model_observation(observation.payload))
        self.assertEqual("gemini_session_transcript", observation.payload["model_observation_source"])
        self.assertEqual((None, None), authoritative_model_observation(payload))
        argv = self.invocations()[0]
        self.assertIn("--safe-mode", argv)
        self.assertIn("--session-id", argv)
        self.assertEqual("bypassPermissions", argv[argv.index("--permission-mode") + 1])
        self.assertEqual("none", argv[argv.index("--permission-prompts") + 1])

    def test_completion_observer_receives_terminal_observation_once(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        receipt = runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt="hello", model=MODEL, effort="low",
        )
        seen = []
        runtime.register_completion_observer(
            thread_id=thread_id, turn_id=receipt.operation_id, observer=seen.append,
        )
        self.finish(runtime)
        self.assertEqual(1, len(seen))
        self.assertEqual("completed", seen[0].terminal_status)
        self.assertEqual("fake answer: hello", seen[0].final_response)

    def test_second_turn_reuses_process_and_schema_change_resumes(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        for prompt in ("one", "two"):
            runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt=prompt, model=MODEL, effort="low")
            self.finish(runtime)
        self.assertEqual(1, len(self.invocations()))
        runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt="three", model=MODEL, effort="low",
            output_schema=SCHEMA,
        )
        self.finish(runtime)
        calls = self.invocations()
        self.assertEqual(2, len(calls))
        self.assertIn("--resume", calls[1])
        self.assertEqual("completed", runtime.read(thread_id=thread_id).terminal_status)

    def test_failed_result_is_failed_terminal(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="FAIL", model=MODEL, effort="low")
        self.finish(runtime)
        observation = runtime.read(thread_id=thread_id)
        self.assertEqual("failed", observation.terminal_status)
        self.assertEqual("model_error", observation.payload["lifecycle"]["terminal_error"]["terminal_reason"])

    def test_interrupt_ends_turn_as_interrupted_and_is_stored(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        receipt = runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt="HANG", model=MODEL, effort="low",
        )
        self.assertTrue(runtime.read(thread_id=thread_id).active)
        interrupted = runtime.interrupt(thread_id=thread_id, turn_id=receipt.operation_id, timeout_seconds=10)
        self.assertTrue(interrupted.payload["interrupted"])
        self.finish(runtime)
        observation = runtime.read(thread_id=thread_id)
        self.assertEqual("interrupted", observation.terminal_status)
        self.assertIsNone(observation.payload["usage"])
        self.assertEqual("unavailable", observation.payload["usage_scope"])
        self.assertEqual("non_success_result_usage_unverified", observation.payload["usage_scope_basis"])
        self.assertEqual(0, observation.payload["provider_usage_raw"]["output_tokens"])
        with self.assertRaisesRegex(RuntimePolicyError, "RUNTIME_INTERRUPT_ALREADY_REQUESTED"):
            runtime.interrupt(thread_id=thread_id, turn_id=receipt.operation_id)
        stored = runtime.read_stored(thread_id=thread_id, turn_id=receipt.operation_id)
        self.assertEqual("interrupted", stored.terminal_status)

    def test_permission_denial_in_success_result_is_failed(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        receipt = runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt="DENY", model=MODEL, effort="low",
        )
        self.finish(runtime)
        observation = runtime.read(thread_id=thread_id)
        self.assertEqual("success", observation.payload["provider_result"]["subtype"])
        self.assertEqual("failed", observation.terminal_status)
        self.assertEqual("GEMINI_PERMISSION_DENIED", observation.payload["lifecycle"]["terminal_error"]["code"])
        self.assertEqual("Write", observation.payload["provider_permission_denials"][0]["tool_name"])
        stored = runtime.read_stored(thread_id=thread_id, turn_id=receipt.operation_id)
        self.assertEqual("failed", stored.terminal_status)

    def test_missing_replay_is_not_replaced_with_another_turn_id(self) -> None:
        runtime = self.runtime(FAKE_GEMINI_NO_REPLAY="1")
        thread_id = self.thread(runtime)
        with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_TURN_START_UNCONFIRMED"):
            runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="hi", model=MODEL, effort="low")
        self.assertIsNone(runtime._threads[thread_id].current.turn_id)

    def test_collector_end_without_result_is_not_terminal(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        receipt = runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt="EXIT", model=MODEL, effort="low",
        )
        self.finish(runtime)
        observation = runtime.read(thread_id=thread_id)
        self.assertFalse(observation.active)
        self.assertIsNone(observation.terminal_status)
        self.assertIn("GEMINI_COLLECTOR_ENDED_WITHOUT_RESULT", observation.payload["error"])
        self.assertEqual("unavailable", observation.payload["usage_scope"])
        stored = runtime.read_stored(thread_id=thread_id, turn_id=receipt.operation_id)
        self.assertIsNone(stored.terminal_status)
        self.assertEqual("terminal_unobserved", stored.payload["turn_status"])
        self.assertIsNone(stored.payload["usage"])

    def test_read_stored_selects_exact_turn(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        first = runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt='JSON:{"ok": false}',
            model=MODEL, effort="low", output_schema=SCHEMA,
        )
        self.finish(runtime)
        runtime.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt='JSON:{"ok": true}',
            model=MODEL, effort="low", output_schema=SCHEMA,
        )
        self.finish(runtime)
        stored = runtime.read_stored(thread_id=thread_id, turn_id=first.operation_id)
        self.assertEqual(first.operation_id, stored.turn_id)
        self.assertEqual("completed", stored.terminal_status)
        self.assertEqual({"ok": False}, json.loads(stored.final_response or ""))
        self.assertEqual(2, stored.payload["turn_count"])
        self.assertEqual((MODEL, "low"), authoritative_model_observation(stored.payload))
        with self.assertRaisesRegex(RuntimePolicyError, "RUNTIME_OBSERVATION_BINDING_MISMATCH"):
            runtime.read_stored(thread_id=thread_id, turn_id="missing-turn")

    def test_other_instance_resumes_persisted_thread(self) -> None:
        first = self.runtime()
        thread_id = self.thread(first)
        first.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="one", model=MODEL, effort="low")
        self.finish(first)
        first.close()
        second = self.runtime()
        with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_THREAD_NOT_OPEN"):
            second.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="x", model=MODEL, effort="low")
        resumed = second.resume(thread_id=thread_id, cwd=self.workspace)
        self.assertFalse(resumed.payload["live_connection"])
        receipt = second.start_turn(
            thread_id=thread_id, cwd=self.workspace, prompt="two", model=MODEL, effort="low",
        )
        self.assertFalse(receipt.payload["first_empty_thread"])
        self.finish(second)
        self.assertEqual("completed", second.read(thread_id=thread_id).terminal_status)
        self.assertIn("--resume", self.invocations()[-1])

    def test_ephemeral_thread_is_not_persisted_or_resumable(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime, ephemeral=True)
        runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="one", model=MODEL, effort="low")
        self.finish(runtime)
        self.assertIn("--no-session-persistence", self.invocations()[0])
        observation = runtime.read(thread_id=thread_id)
        self.assertEqual((None, None), authoritative_model_observation(observation.payload))
        self.assertEqual("GEMINI_SESSION_TRANSCRIPT_UNAVAILABLE", observation.payload["model_observation_reason"])
        with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_TURN_CONFIGURATION_CHANGE_REQUIRES_PERSISTED_THREAD"):
            runtime.start_turn(
                thread_id=thread_id, cwd=self.workspace, prompt="two", model=MODEL, effort="high",
            )
        runtime.close()
        with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_EPHEMERAL_THREAD_PROCESS_LOST"):
            self.runtime().resume(thread_id=thread_id, cwd=self.workspace)

    def test_transcript_read_failure_still_delivers_terminal_turn(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        with mock.patch.object(GeminiRuntime, "_stored_turns", side_effect=ValueError("broken")):
            runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="one", model=MODEL, effort="low")
            self.finish(runtime)
        observation = runtime.read(thread_id=thread_id)
        self.assertEqual("completed", observation.terminal_status)
        self.assertEqual((None, None), authoritative_model_observation(observation.payload))
        self.assertEqual("GEMINI_SESSION_TRANSCRIPT_UNAVAILABLE", observation.payload["model_observation_reason"])


class GeminiRuntimeProvenanceTests(GeminiRuntimeTestBase):
    def assert_rejected(self, code: str, **env: str) -> None:
        runtime = self.runtime(**env)
        thread_id = self.thread(runtime)
        with self.assertRaisesRegex(RuntimePolicyError, code):
            runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="x", model=MODEL, effort="low")
        process = runtime._threads[thread_id].process
        assert process is not None
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll())

    def test_permission_mode_mismatch_rejects_turn_and_kills_process(self) -> None:
        self.assert_rejected("PERMISSION_POLICY_MISMATCH", FAKE_GEMINI_PERMISSION_MODE="default")

    def test_session_mismatch_rejects_turn(self) -> None:
        self.assert_rejected("THREAD_PROVENANCE_MISMATCH", FAKE_GEMINI_INIT_SESSION="other-session")

    def test_cwd_mismatch_rejects_turn(self) -> None:
        self.assert_rejected("THREAD_PROVENANCE_MISMATCH", FAKE_GEMINI_INIT_CWD=str(self.config_dir))

    def test_model_mismatch_rejects_turn(self) -> None:
        self.assert_rejected("MODEL_PROVENANCE_MISMATCH", FAKE_GEMINI_INIT_MODEL="gemini-alias")

    def test_cli_version_mismatch_rejects_turn(self) -> None:
        self.assert_rejected("GEMINI_CLI_VERSION_MISMATCH", FAKE_GEMINI_INIT_VERSION="9.9.9")

    def test_executable_change_after_lock_is_rejected_before_process(self) -> None:
        copy = Path(self._tmp.name) / "gemini_copy.py"
        copy.write_bytes(FAKE_GEMINI.read_bytes())
        runtime = GeminiRuntime(
            model_catalog=_catalog(), gemini_bin=copy, interpreter=sys.executable,
            state_root=self.state_root, config_dir=self.config_dir, env=self.env,
        )
        self.runtimes.append(runtime)
        thread_id = self.thread(runtime)
        copy.write_bytes(copy.read_bytes() + b"\n# changed\n")
        with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_EXECUTABLE_CHANGED"):
            runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="x", model=MODEL, effort="low")
        with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_EXECUTABLE_CHANGED"):
            runtime.list_models()
        self.assertEqual([], self.invocations())

    def test_shell_shim_executable_is_rejected(self) -> None:
        for suffix in (".cmd", ".bat", ".ps1"):
            shim = Path(self._tmp.name) / f"gemini{suffix}"
            shim.write_text("@echo off\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_EXECUTABLE_SHIM_UNSUPPORTED"):
                GeminiRuntime(model_catalog=_catalog(), gemini_bin=shim, env=self.env)

    def test_uncatalogued_model_or_effort_is_rejected_before_process(self) -> None:
        runtime = self.runtime()
        with self.assertRaisesRegex(RuntimePolicyError, "MODEL_BINDING_UNAVAILABLE"):
            runtime.create_thread(cwd=self.workspace, title="t", model="unknown", developer_instructions="")
        thread_id = self.thread(runtime)
        with self.assertRaisesRegex(RuntimePolicyError, "MODEL_BINDING_UNAVAILABLE"):
            runtime.start_turn(thread_id=thread_id, cwd=self.workspace, prompt="x", model=MODEL, effort="medium")
        self.assertEqual([], self.invocations())

    def test_command_line_limit_is_checked_before_process(self) -> None:
        runtime = self.runtime()
        thread_id = self.thread(runtime)
        huge = {"type": "object", "description": "x" * 31_000}
        with self.assertRaisesRegex(RuntimePolicyError, "GEMINI_COMMAND_LINE_TOO_LONG"):
            runtime.start_turn(
                thread_id=thread_id, cwd=self.workspace, prompt="x", model=MODEL, effort="low",
                output_schema=huge,
            )
        self.assertEqual([], self.invocations())


class GeminiUsageAndTranscriptTests(unittest.TestCase):
    def test_turn_usage_requires_success_result_with_iterations(self) -> None:
        usage = {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                 "output_tokens": 2, "iterations": [{"type": "message"}]}
        success = {"subtype": "success", "is_error": False, "usage": usage}
        self.assertEqual(({"inputTokens": 1, "cachedInputTokens": 0, "outputTokens": 2}, "turn",
                          "success_result_usage_with_iterations"), gemini_turn_usage(success))
        self.assertEqual((None, "unavailable", "result_usage_without_iterations"),
                         gemini_turn_usage({**success, "usage": {**usage, "iterations": []}}))
        self.assertEqual((None, "unavailable", "non_success_result_usage_unverified"),
                         gemini_turn_usage({**success, "subtype": "error_during_execution", "is_error": True}))
        self.assertEqual((None, "unavailable", "result_unobserved"), gemini_turn_usage(None))

    def test_usage_projects_only_provided_components(self) -> None:
        self.assertEqual({"outputTokens": 5}, project_gemini_usage({"input_tokens": 3, "output_tokens": 5}))
        self.assertIsNone(project_gemini_usage(None))
        self.assertEqual(
            {"inputTokens": 6, "cachedInputTokens": 3, "outputTokens": 1},
            project_gemini_usage({
                "input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3,
                "output_tokens": 1, "output_tokens_details": {"thinking_tokens": 9},
            }),
        )

    def test_transcript_model_observation_requires_exactly_one_pair(self) -> None:
        def turn(*assistants: dict) -> dict:
            return {"records": [{"type": "assistant", **item} for item in assistants]}

        observe = GeminiRuntime._transcript_model_observation
        one = observe(turn({"effort": "high", "message": {"model": "m"}},
                           {"effort": "high", "message": {"model": "m"}},
                           {"message": {"model": "<synthetic>"}}))
        self.assertEqual(("m", "high"), authoritative_model_observation(one))
        for source, expected in (("gemini_session_transcript", ("m", "high")), ("request_echo", (None, None))):
            self.assertEqual(expected, authoritative_receipt_model_observation(
                observed_model="m", observed_effort="high", binding_provenance={"observed": source},
            ))
        cases = {
            "GEMINI_TRANSCRIPT_MODEL_EFFORT_NOT_REPORTED": (
                None, turn(), turn({"message": {"model": "m"}}),
                turn({"effort": ["high"], "message": {"model": "m"}}),
            ),
            "GEMINI_TRANSCRIPT_MODEL_EFFORT_AMBIGUOUS": (
                turn({"effort": "high", "message": {"model": "m"}}, {"effort": "low", "message": {"model": "m"}}),
                turn({"effort": "high", "message": {"model": "m"}}, {"message": {"model": "m"}}),
            ),
        }
        for reason, turns in cases.items():
            for item in turns:
                with self.subTest(reason=reason, turn=item):
                    observed = observe(item)
                    self.assertEqual((None, None), authoritative_model_observation(observed))
                    self.assertEqual(reason, observed["model_observation_reason"])


class GeminiModelCatalogTests(unittest.TestCase):
    def test_rejects_duplicate_models_and_unknown_effort(self) -> None:
        with self.assertRaises(ValidationError):
            GeminiModelCatalog.model_validate({
                "format": "flowmarshal-gemini-model-catalog-v1",
                "models": [
                    {"model": "a", "supported_efforts": ["low"]},
                    {"model": "a", "supported_efforts": ["high"]},
                ],
            })
        with self.assertRaises(ValidationError):
            GeminiModelCatalog.model_validate({
                "format": "flowmarshal-gemini-model-catalog-v1",
                "models": [{"model": "a", "supported_efforts": ["minimal"]}],
            })

    def test_raw_catalog_is_checked_before_typed_conversion(self) -> None:
        good = '{"format": "flowmarshal-gemini-model-catalog-v1", "models": [%s]}'
        rows = {
            "duplicate key": '{"model": "a", "model": "b", "supported_efforts": ["low"]}',
            "null model": '{"model": null, "supported_efforts": ["low"]}',
            "empty model": '{"model": "", "supported_efforts": ["low"]}',
            "null effort": '{"model": "a", "supported_efforts": [null]}',
            "empty efforts": '{"model": "a", "supported_efforts": []}',
            "duplicate effort": '{"model": "a", "supported_efforts": ["low", "low"]}',
            "extra field": '{"model": "a", "supported_efforts": ["low"], "hidden": true}',
            "duplicate model": ('{"model": "a", "supported_efforts": ["low"]}, '
                                '{"model": "a", "supported_efforts": ["high"]}'),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            for name, row in rows.items():
                path.write_text(good % row, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "GEMINI_MODEL_CATALOG_INVALID", msg=name):
                    GeminiModelCatalog.load(path)
            data = (good % ('{"model": "z", "supported_efforts": ["high", "low"]}, '
                            '{"model": "a", "supported_efforts": ["low"]}')).encode("utf-8")
            path.write_bytes(data)
            catalog = GeminiModelCatalog.load(path)
        self.assertEqual(["z", "a"], [item.model for item in catalog.models])
        self.assertEqual(("high", "low"), catalog.models[0].supported_efforts)
        self.assertEqual(sha256_bytes(data), catalog.source_sha256)
        self.assertEqual(sha256_bytes(data), catalog.raw_response()["catalogSourceSha256"])

    def test_repository_example_catalog_and_roles_load(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalog = GeminiModelCatalog.load(root / "config" / "gemini-model-catalog.json")
        roles = json.loads((root / "config" / "qualification-roles.gemini.json").read_text(encoding="utf-8"))
        for role, assignment in roles.items():
            self.assertTrue(
                any(
                    item.model == assignment["model"] and assignment["effort"] in item.supported_efforts
                    for item in catalog.models
                ),
                role,
            )


class GeminiProviderSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.catalog = Path(self._tmp.name) / "gemini-catalog.json"
        self.catalog.write_text(_catalog().model_dump_json(), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def parse(self, *argv: str) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        parser.add_argument("--codex-bin")
        add_provider_arguments(parser)
        return parser.parse_args(list(argv))

    def test_gemini_requires_catalog_and_rejects_other_provider_inputs(self) -> None:
        with self.assertRaisesRegex(ValidationError, "GEMINI_MODEL_CATALOG_REQUIRED"):
            selection_from_arguments(self.parse("--provider", "gemini"))
        with self.assertRaisesRegex(ValidationError, "RUNTIME_PROVIDER_INPUT_MISMATCH"):
            selection_from_arguments(self.parse(
                "--provider", "gemini", "--gemini-model-catalog", str(self.catalog), "--codex-bin", "codex",
            ))
        with self.assertRaisesRegex(ValidationError, "RUNTIME_PROVIDER_INPUT_MISMATCH"):
            selection_from_arguments(self.parse(
                "--provider", "gemini", "--gemini-model-catalog", str(self.catalog), "--claude-bin", "claude",
            ))
        with self.assertRaisesRegex(ValidationError, "RUNTIME_PROVIDER_INPUT_MISMATCH"):
            selection_from_arguments(self.parse("--gemini-model-catalog", str(self.catalog)))

    def test_gemini_run_metadata_binds_catalog_bytes(self) -> None:
        selection = selection_from_arguments(
            self.parse("--provider", "gemini", "--gemini-model-catalog", str(self.catalog)),
        )
        metadata = provider_run_metadata(selection)
        restored = selection_from_run_metadata(metadata)
        assert restored is not None
        self.assertEqual("gemini", restored.provider)
        self.catalog.write_text(self.catalog.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimePolicyError, "RUNTIME_PROVIDER_CATALOG_CHANGED"):
            selection_from_run_metadata(metadata)

    def test_harness_rejects_codex_bin_for_gemini(self) -> None:
        gemini = RuntimeProviderSelection(provider="gemini", gemini_model_catalog=str(self.catalog))
        with self.assertRaisesRegex(RuntimePolicyError, "RUNTIME_PROVIDER_INPUT_MISMATCH"):
            open_harness_runtime(
                gemini, codex_factory=lambda **_: "codex", codex_bin="codex.exe", project_binding=None,
                default_state_root=Path(self._tmp.name),
            )


if __name__ == "__main__":
    unittest.main()
