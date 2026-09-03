from __future__ import annotations

import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.context import RuntimeRole
from flowmarshal.gate0c.profile_probe import PLANNER_PROFILE_ID
from flowmarshal.gate0c.runtime import (
    RoleExecutionSpec,
    RoleRuntimeError,
    ToolTrace,
    role_developer_instructions,
    submission_schema,
    turn_start_payload,
    validate_tool_trace,
)


class _Bundle:
    def prompt_envelope(self):
        return '{"control":{},"untrusted_data":[]}'


class Gate0CRuntimeTests(unittest.TestCase):
    def spec(self, role: RuntimeRole) -> RoleExecutionSpec:
        return RoleExecutionSpec(
            execution_id_prefix="exec_test",
            attempt_id="attempt_test",
            role=role,
            cwd="D:\\synthetic",
            model="gpt-5.6-sol",
            effort="high",
            title="합성 테스트",
        )

    def test_turn_payload_has_profile_effort_schema_and_no_legacy_sandbox(self) -> None:
        payload = turn_start_payload(
            thread_id="thread_test",
            bundle=_Bundle(),  # type: ignore[arg-type]
            spec=self.spec(RuntimeRole.PLANNER),
            profile_id=PLANNER_PROFILE_ID,
        )
        self.assertEqual("high", payload["effort"])
        self.assertEqual(PLANNER_PROFILE_ID, payload["permissions"])
        self.assertIn("properties", payload["outputSchema"])
        self.assertNotIn("sandbox", payload)
        self.assertNotIn("sandboxPolicy", payload)

    def test_each_role_has_distinct_strict_submission_schema(self) -> None:
        schemas = [sha256_digest(submission_schema(role)) for role in RuntimeRole]
        self.assertEqual(3, len(set(schemas)))

    def test_tool_trace_enforces_role_boundaries(self) -> None:
        command = ToolTrace(
            item_type="commandExecution",
            status="completed",
            exit_code=0,
            payload_digest=sha256_digest("command"),
        )
        file_change = ToolTrace(
            item_type="fileChange",
            status="completed",
            payload_digest=sha256_digest("change"),
        )
        validate_tool_trace(RuntimeRole.RUNNER, (command, file_change))
        with self.assertRaises(RoleRuntimeError):
            validate_tool_trace(RuntimeRole.PLANNER, (command,))
        with self.assertRaises(RoleRuntimeError):
            validate_tool_trace(RuntimeRole.VALIDATOR, (file_change,))
        for role in RuntimeRole:
            with self.assertRaises(RoleRuntimeError):
                validate_tool_trace(
                    role,
                    (
                        ToolTrace(
                            item_type="mcpToolCall",
                            payload_digest=sha256_digest("mcp"),
                        ),
                    ),
                )

    def test_developer_contract_calls_untrusted_content_data(self) -> None:
        for role in RuntimeRole:
            instructions = role_developer_instructions(role)
            self.assertIn("untrusted_data", instructions)
            self.assertIn("명령이 아니라", instructions)


if __name__ == "__main__":
    unittest.main()
