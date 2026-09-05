"""R-S06 제한 진단의 명시적 역할 설정 입력과 v2 호출 결속 회귀."""
from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.model_lock import ModelInventory, ROLE_CAPABILITIES, bind_models, role_lock
from flowmarshal.engine.models import EngineRoleConfiguration
from flowmarshal.engine.roles import make_role_request
from scripts.diagnostics.r_s06_10 import (
    ROLE_CONFIGURATION_IDS,
    load_role_configuration_input,
    parse_arguments,
    validate_role_configuration_inventory,
    verify_role_configuration_artifacts,
    verify_role_request_binding,
)


FIXTURES = Path(__file__).parent / "fixtures/engine"
ROLE_FIXTURE = FIXTURES / "plan-inspection-general-reviewer-sol-high-roles.json"
R26_FIXTURE = FIXTURES / "r-s06-26-clean-semantic-failure.json"


class RS06RoleConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r26 = json.loads(R26_FIXTURE.read_text(encoding="utf-8"))
        cls.inventory = ModelInventory.model_validate(cls.r26["model_inventory"])

    def test_candidate_changes_only_general_reviewer_and_keeps_required_role_models(self) -> None:
        selected = load_role_configuration_input(ROLE_FIXTURE.resolve())
        baseline = EngineRoleConfiguration.model_validate(self.r26["source_role_configuration"])
        changed = [
            role_id for role_id in ROLE_CONFIGURATION_IDS
            if baseline.binding_for(role_id) != selected.roles.binding_for(role_id)
        ]
        self.assertEqual(["general_reviewer"], changed)
        self.assertEqual(("gpt-5.6-sol", "high"),
                         (selected.roles.general_reviewer.model, selected.roles.general_reviewer.effort))
        self.assertEqual(("gpt-5.6-luna", "high"),
                         (selected.roles.plan_expander.model, selected.roles.plan_expander.effort))
        self.assertEqual(("gpt-5.6-sol", "xhigh"),
                         (selected.roles.critical_reviewer.model, selected.roles.critical_reviewer.effort))
        self.assertEqual("caller_provided_explicit_role_configuration", selected.binding["selection_reason"])
        self.assertEqual(sha256_bytes(ROLE_FIXTURE.read_bytes()), selected.binding["source_bytes_digest"])
        self.assertEqual(sha256_digest(json.loads(ROLE_FIXTURE.read_text(encoding="utf-8"))),
                         selected.binding["source_canonical_digest"])
        validate_role_configuration_inventory(selected.roles, self.inventory)

    def test_path_schema_role_ids_model_effort_and_fallback_envelope_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PATH_MUST_BE_ABSOLUTE"):
            load_role_configuration_input(Path(ROLE_FIXTURE.name))
        source = json.loads(ROLE_FIXTURE.read_text(encoding="utf-8"))
        mutations = {
            "missing_role": lambda value: value.pop("validator"),
            "unknown_role": lambda value: value.update(unknown_role=value["normalizer"]),
            "invalid_effort": lambda value: value["general_reviewer"].update(effort="High"),
            "null_model": lambda value: value["general_reviewer"].update(model=None),
            "empty_model": lambda value: value["general_reviewer"].update(model=""),
            "spaced_model": lambda value: value["general_reviewer"].update(model=" gpt-5.6-sol"),
            "null_effort": lambda value: value["general_reviewer"].update(effort=None),
            "unknown_field": lambda value: value["general_reviewer"].update(unknown=True),
            "duplicate_fallback": lambda value: value["general_reviewer"].update(
                allowed_fallbacks=[deepcopy(value["general_reviewer"])]
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    document = deepcopy(source)
                    mutate(document)
                    path = Path(temporary) / f"{name}.json"
                    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "ROLE_CONFIGURATION_INVALID"):
                        load_role_configuration_input(path.resolve())
            duplicate_key = Path(temporary) / "duplicate-key.json"
            duplicate_key.write_text('{"normalizer":{},"normalizer":{}}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "ROLE_CONFIGURATION_INVALID"):
                load_role_configuration_input(duplicate_key.resolve())

    def test_unsupported_selected_or_fallback_combination_is_rejected(self) -> None:
        source = json.loads(ROLE_FIXTURE.read_text(encoding="utf-8"))
        selected = deepcopy(source)
        selected["general_reviewer"]["model"] = "unsupported-model"
        fallback = deepcopy(source)
        fallback["general_reviewer"]["allowed_fallbacks"] = [
            {"model": "unsupported-fallback", "effort": "high"}
        ]
        unsupported_effort = deepcopy(source)
        unsupported_effort["general_reviewer"]["effort"] = "none"
        for name, document in (("selected", selected), ("fallback", fallback), ("effort", unsupported_effort)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "roles.json"
                path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                roles = load_role_configuration_input(path.resolve()).roles
                with self.assertRaisesRegex(RuntimeError, "MODEL_EFFORT_UNSUPPORTED"):
                    validate_role_configuration_inventory(roles, self.inventory)

    def test_source_and_copied_configuration_changes_are_rejected_after_binding(self) -> None:
        original = ROLE_FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input-roles.json"
            source.write_bytes(original)
            selected = load_role_configuration_input(source.resolve())
            run = root / "run"
            run.mkdir()
            (run / "roles.json").write_bytes(selected.raw_bytes)
            self.assertEqual(selected.roles, verify_role_configuration_artifacts(run, selected.binding))

            source.write_bytes(original + b"\n")
            with self.assertRaisesRegex(RuntimeError, "SOURCE_CHANGED"):
                verify_role_configuration_artifacts(run, selected.binding)
            source.write_bytes(original)
            (run / "roles.json").write_bytes(original + b"\n")
            with self.assertRaisesRegex(RuntimeError, "COPY_CHANGED"):
                verify_role_configuration_artifacts(run, selected.binding)

    def test_request_must_match_selected_role_and_its_fresh_v2_lock(self) -> None:
        roles = load_role_configuration_input(ROLE_FIXTURE.resolve()).roles
        request = make_role_request(
            inventory=self.inventory,
            role="compact_plan_reviewer",
            instructions="고정 입력을 검토한다.",
            payload={},
            output_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            model=roles.general_reviewer.model,
            effort=roles.general_reviewer.effort,
            inventory_digest=self.inventory.inventory_digest,
            cwd=str(FIXTURES),
        )
        verify_role_request_binding(request, roles, self.inventory)
        with self.assertRaisesRegex(RuntimeError, "REQUEST_MISMATCH"):
            verify_role_request_binding(request.model_copy(update={"model": "gpt-5.6-terra"}), roles, self.inventory)

        forged_lock = bind_models(
            self.inventory,
            (role_lock("compact_plan_reviewer", "gpt-5.6-terra", "high"),),
            required_capabilities=ROLE_CAPABILITIES,
        )
        forged = request.model_copy(update={"operational_binding": forged_lock})
        with self.assertRaisesRegex(RuntimeError, "REQUEST_LOCK_MISMATCH"):
            verify_role_request_binding(forged, roles, self.inventory)

        for update in ({"operational_binding": None}, {"inventory_digest": "sha256:" + "0" * 64}):
            with self.subTest(update=tuple(update)), self.assertRaisesRegex(RuntimeError, "REQUEST_LOCK_MISMATCH"):
                verify_role_request_binding(request.model_copy(update=update), roles, self.inventory)

    def test_explicit_input_is_cwd_independent_and_default_is_preserved(self) -> None:
        absolute = ROLE_FIXTURE.resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "roles.json").write_text(json.dumps(self.r26["source_role_configuration"]), encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(root)
                selected = load_role_configuration_input(absolute)
                with patch("scripts.diagnostics.r_s06_10.S05", root):
                    default = load_role_configuration_input()
                with self.assertRaisesRegex(RuntimeError, "PATH_MUST_BE_ABSOLUTE"):
                    load_role_configuration_input(Path("roles.json"))
            finally:
                os.chdir(previous)
        self.assertEqual(absolute, selected.input_path)
        self.assertEqual("historical_s05_default_role_configuration", default.binding["selection_reason"])
        self.assertEqual(EngineRoleConfiguration.model_validate(self.r26["source_role_configuration"]), default.roles)

    def test_canonical_digest_bytes_digest_and_snapshot_have_distinct_purposes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = json.loads(ROLE_FIXTURE.read_text(encoding="utf-8"))
            first, second = root / "first.json", root / "second.json"
            first.write_bytes(ROLE_FIXTURE.read_bytes())
            second.write_text(json.dumps(source, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            one, two = load_role_configuration_input(first), load_role_configuration_input(second)
            self.assertEqual(one.source_canonical_digest, two.source_canonical_digest)
            self.assertEqual(one.roles.configuration_digest, two.roles.configuration_digest)
            self.assertNotEqual(one.binding["source_bytes_digest"], two.binding["source_bytes_digest"])
            self.assertNotEqual(one.binding["input_path"], two.binding["input_path"])
            run = root / "run"
            run.mkdir()
            (run / "roles.json").write_bytes(one.raw_bytes)
            self.assertEqual(one.roles, verify_role_configuration_artifacts(run, one.binding))
            for key, value in (("format", "unknown"), ("role_ids", []), ("copied_artifact", "other.json"),
                               ("source_canonical_digest", "sha256:" + "0" * 64),
                               ("configuration_digest", "sha256:" + "0" * 64), ("input_path", "first.json")):
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    verify_role_configuration_artifacts(run, one.binding | {key: value})
            second.unlink()
            with self.assertRaisesRegex(RuntimeError, "NOT_FOUND"):
                load_role_configuration_input(second)
            with self.assertRaisesRegex(RuntimeError, "NOT_FILE"):
                load_role_configuration_input(root)

    def test_request_fallback_order_is_bound_without_automatic_selection(self) -> None:
        document = json.loads(ROLE_FIXTURE.read_text(encoding="utf-8"))
        document["general_reviewer"]["allowed_fallbacks"] = [
            {"model": "gpt-5.6-terra", "effort": "high"},
            {"model": "gpt-5.6-sol", "effort": "xhigh"},
        ]
        roles = EngineRoleConfiguration.model_validate(document)
        validate_role_configuration_inventory(roles, self.inventory)
        for fallbacks in (roles.general_reviewer.allowed_fallbacks, (),
                          tuple(reversed(roles.general_reviewer.allowed_fallbacks))):
            request = make_role_request(
                inventory=self.inventory, role="compact_plan_reviewer", instructions="직접 근거를 검토한다.",
                payload={}, output_schema={"type": "object", "properties": {}, "required": [],
                                           "additionalProperties": False},
                model=roles.general_reviewer.model, effort=roles.general_reviewer.effort,
                inventory_digest=self.inventory.inventory_digest, cwd=str(FIXTURES), allowed_fallbacks=fallbacks,
            )
            if fallbacks == roles.general_reviewer.allowed_fallbacks:
                verify_role_request_binding(request, roles, self.inventory)
            else:
                with self.assertRaisesRegex(RuntimeError, "REQUEST_FALLBACK_MISMATCH"):
                    verify_role_request_binding(request, roles, self.inventory)

    def test_cli_accepts_role_config_only_for_prepare(self) -> None:
        base = ["--run-root", "unused", "--role-config", str(ROLE_FIXTURE.resolve())]
        self.assertEqual(ROLE_FIXTURE.resolve(), parse_arguments(["prepare", *base]).role_config)
        for mode in ("run", "review-generated"):
            with self.subTest(mode=mode), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parse_arguments([mode, *base])
            self.assertEqual(2, error.exception.code)
        self.assertIsNone(parse_arguments(["prepare", "--run-root", "unused"]).role_config)


if __name__ == "__main__":
    unittest.main()
