from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.inspection_generation_review import (
    GenerationReviewBindingError,
    _verify_registered_sources,
    bind_generation_review_source,
    build_generation_assessment_provenance,
    generation_review_criteria_binding,
    verify_generation_assessment,
)
from flowmarshal.engine.domain import ProjectMapEntry, ProjectMapEntryKind
from flowmarshal.engine.plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V2
from flowmarshal.engine.plan_inspection_v2 import plan_inspection_citation_catalog_v2
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
)
from flowmarshal.engine.roles import RoleCallResult, ScriptedStructuredRoleRunner
from tests.engine_helpers import assignment, goal, inventory, profile, project_map, state
from tests.engine_inspection_helpers import inspection_fixture
from tests.test_engine_plan_inspection_v2 import v2_table_from_v1
from tests.test_engine_role_adapters import _plan_response, _skeleton_response


class GenerationReviewProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("검사 기준 본문", encoding="utf-8")
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        project_id = "project_" + "7" * 32
        current_profile = profile(project_id)
        self.goal = goal(project_id, current_profile.definition_digest)
        self.project_map = project_map(project_id, self.workspace)
        self.state = state(
            project_id, self.goal.definition_digest, self.project_map.revision_digest
        )
        current_inventory = inventory()
        draft = _plan_response()
        old = inspection_fixture(draft, self.goal.definition.model_dump(mode="json"))
        coverage = {row["criterion_id"]: row["validation_ids"] for row in draft["goal_coverage"]}
        citations = plan_inspection_citation_catalog_v2(
            {
                "source:goal": self.goal.definition.model_dump(mode="json"),
                "source:state": self.state.model_dump(mode="json"),
                "source:project_map": self.project_map.model_dump(mode="json"),
            },
            self.project_map,
        )
        envelope = {
            "inspection": v2_table_from_v1(old, coverage, citations),
            "plan": draft,
        }
        runner = ScriptedStructuredRoleRunner({
            "skeleton_generator": [_skeleton_response()],
            "plan_expander": [envelope],
        })
        options = {
            "model": "worker",
            "effort": "medium",
            "inventory_digest": current_inventory.inventory_digest,
            "cwd": self.workspace,
        }
        candidate = SkeletonGeneratorAdapter(runner, **options).generate(
            goal=self.goal, state=self.state, project_map=self.project_map, candidate_count=1
        )[0]
        adapter = PlanExpanderAdapter(
            runner,
            RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
            inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2,
            **options,
        )
        self.plan = adapter.expand(
            candidate=candidate,
            goal=self.goal,
            state=self.state,
            project_map=self.project_map,
        )
        self.request = runner.calls[-1]
        receipt = adapter.receipts[-1].model_copy(update={
            "thread_id": "thread-expander",
            "turn_ids": ("turn-expander",),
            "usage_available": True,
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "output_tokens": 5,
            "reasoning_tokens": 1,
        })
        self.result = RoleCallResult(payload=envelope, receipt=receipt)
        self._write_preflight(self.root)
        notes = self.root / "reviews" / "independent.md"
        notes.parent.mkdir()
        notes.write_text("Goal과 AC, phase 및 등록 본문을 원문과 대조했다.\n", encoding="utf-8")
        self.notes = notes

    def tearDown(self):
        self.temp.cleanup()

    def _write_preflight(self, root: Path, *, marker: str = "run-one") -> None:
        body = {
            "run_id": marker,
            "generation_review_criteria": generation_review_criteria_binding(),
            "inspection_provider_contract": "plan-inspection-v2",
            "diagnostic_policy_input": {"bound": True},
            "workspace_binding": {"portable": True},
            "role_threads_ephemeral": False,
        }
        (root / "preflight.json").write_text(
            json.dumps(body | {"lock_digest": sha256_digest(body)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _binding(self, *, root: Path | None = None, result=None):
        return bind_generation_review_source(
            run_root=root or self.root,
            expansion_request=self.request,
            expansion_result=result or self.result,
            goal=self.goal,
            state=self.state,
            project_map=self.project_map,
            expanded_plan=self.plan,
        )

    def _assessment(self, binding=None):
        binding = binding or self._binding()
        provenance = build_generation_assessment_provenance(
            run_root=self.root,
            source_binding=binding,
            author_ref="codex-thread:independent-reviewer",
            review_notes_path=self.notes,
        )
        return {
            "generation_acceptable": True,
            "ac_validation_rows": [{"existing": "oracle-output-is-untouched"}],
            "assessment_provenance": provenance.model_dump(mode="json"),
        }

    def _verify(self, assessment, **kwargs):
        return verify_generation_assessment(
            assessment,
            run_root=kwargs.pop("run_root", self.root),
            expansion_request=self.request,
            expansion_result=kwargs.pop("expansion_result", self.result),
            goal=self.goal,
            state=self.state,
            project_map=self.project_map,
            expanded_plan=self.plan,
            **kwargs,
        )

    def test_binds_exact_expansion_and_preserves_existing_assessment_oracle(self):
        binding = self._binding()
        assessment = self._assessment(binding)
        before = deepcopy(assessment)
        provenance = self._verify(assessment, expected_source_binding=binding)
        self.assertEqual(before, assessment)
        self.assertEqual(binding.source_binding_digest, provenance.source_binding_digest)
        self.assertEqual("thread-expander", binding.expansion_thread_id)
        self.assertTrue(binding.registered_source_content_digests)
        self.assertEqual(
            "manual_review_separate_from_harness_provider_receipts",
            provenance.provider_accounting_scope,
        )

    def test_review_notes_tamper_is_rejected(self):
        assessment = self._assessment()
        self.notes.write_text("사후 변조\n", encoding="utf-8")
        with self.assertRaisesRegex(GenerationReviewBindingError, "NOTES_DIGEST_MISMATCH"):
            self._verify(assessment)

    def test_registered_source_file_tamper_is_rejected(self):
        assessment = self._assessment()
        (self.workspace / "AGENTS.md").write_text("변조된 등록 본문", encoding="utf-8")
        with self.assertRaisesRegex(GenerationReviewBindingError, "REGISTERED_SOURCE_CHANGED"):
            self._verify(assessment)

    def test_portable_materialized_absolute_registered_reference_is_verified(self):
        """run/workspace 밖 project-references로 옮긴 등록 reference는 v2 catalog와 bytes로 확인한다."""
        portable_run = self.root / "portable-run"
        portable_workspace = portable_run / "workspace"
        references = portable_run / "project-references"
        portable_workspace.mkdir(parents=True)
        references.mkdir()
        for original_entry in self.project_map.entries:
            source = self.workspace / original_entry.path
            if source.is_file():
                destination = portable_workspace / original_entry.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
        reference = references / "registered-guide.md"
        reference.write_text("등록된 외부 reference 원문", encoding="utf-8")
        entry = ProjectMapEntry(
            entry_id="reference_registered", kind=ProjectMapEntryKind.REFERENCE,
            path=str(reference.resolve()), content_digest=sha256_bytes(reference.read_bytes()),
        )
        relocated_map = self.project_map.model_copy(update={
            "root": str(portable_workspace.resolve()),
            "entries": (*self.project_map.entries, entry),
        })
        payload = dict(self.request.payload)
        catalog = dict(payload["inspection_source_catalog"])
        catalog[f"project:{entry.entry_id}"] = {
            "source_ref": f"project:{entry.entry_id}", "selector": "/content",
            "path": entry.path, "content_digest": entry.content_digest,
            "evidence_ref": "source:project_map", "content": reference.read_text(encoding="utf-8"),
        }
        payload["inspection_source_catalog"] = catalog
        request = self.request.model_copy(update={"payload": payload})
        observed = _verify_registered_sources(request, relocated_map, run_root=portable_run)
        self.assertEqual(observed[f"project:{entry.entry_id}"], entry.content_digest)

    def test_cross_run_binding_and_wrong_receipt_are_rejected(self):
        assessment = self._assessment()
        other = self.root / "other-run"
        other.mkdir()
        self._write_preflight(other, marker="run-two")
        other_notes = other / "reviews" / "independent.md"
        other_notes.parent.mkdir()
        other_notes.write_bytes(self.notes.read_bytes())
        with self.assertRaisesRegex(GenerationReviewBindingError, "PROVENANCE_BINDING_MISMATCH"):
            self._verify(assessment, run_root=other)
        bad_receipt = self.result.receipt.model_copy(update={"input_digest": "sha256:" + "f" * 64})
        with self.assertRaisesRegex(GenerationReviewBindingError, "EXPANSION_RECEIPT_INVALID"):
            self._binding(result=RoleCallResult(payload=self.result.payload, receipt=bad_receipt))

    def test_wrong_prefixed_criteria_is_rejected(self):
        body = {
            "run_id": "run-one",
            "generation_review_criteria": {"criteria_digest": "sha256:" + "0" * 64},
            "inspection_provider_contract": "plan-inspection-v2",
            "diagnostic_policy_input": {"bound": True},
            "workspace_binding": {"portable": True},
            "role_threads_ephemeral": False,
        }
        (self.root / "preflight.json").write_text(
            json.dumps(body | {"lock_digest": sha256_digest(body)}), encoding="utf-8"
        )
        with self.assertRaisesRegex(GenerationReviewBindingError, "CRITERIA_NOT_PREFLIGHT_BOUND"):
            self._binding()

    def test_wrong_assessment_criteria_and_nonportable_preflight_are_rejected(self):
        assessment = self._assessment()
        assessment["assessment_provenance"]["criteria_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(GenerationReviewBindingError, "PROVENANCE_BINDING_MISMATCH"):
            self._verify(assessment)
        self._write_preflight(self.root)
        preflight = json.loads((self.root / "preflight.json").read_text(encoding="utf-8"))
        preflight.pop("lock_digest")
        preflight["role_threads_ephemeral"] = True
        (self.root / "preflight.json").write_text(
            json.dumps(preflight | {"lock_digest": sha256_digest(preflight)}), encoding="utf-8"
        )
        with self.assertRaisesRegex(GenerationReviewBindingError, "PORTABLE_V2_POLICY_REQUIRED"):
            self._binding()

    def test_expander_cannot_be_attested_as_independent_author(self):
        binding = self._binding()
        for author in ("thread-expander", "thread:thread-expander", "codex-thread:thread-expander"):
            with self.subTest(author=author), self.assertRaisesRegex(
                GenerationReviewBindingError, "EXPANDER_CANNOT_ATTEST"
            ):
                build_generation_assessment_provenance(
                    run_root=self.root,
                    source_binding=binding,
                    author_ref=author,
                    review_notes_path=self.notes,
                )

    def test_absolute_parent_and_symlink_note_escape_are_rejected(self):
        binding = self._binding()
        outside = self.root.parent / "outside-generation-review.md"
        outside.write_text("외부", encoding="utf-8")
        try:
            with self.assertRaisesRegex(GenerationReviewBindingError, "NOTES_PATH_ESCAPE"):
                build_generation_assessment_provenance(
                    run_root=self.root,
                    source_binding=binding,
                    author_ref="reviewer",
                    review_notes_path=outside,
                )
            link = self.root / "reviews" / "escape.md"
            try:
                link.symlink_to(outside)
            except OSError:
                return
            with self.assertRaisesRegex(GenerationReviewBindingError, "NOTES_PATH_ESCAPE"):
                build_generation_assessment_provenance(
                    run_root=self.root,
                    source_binding=binding,
                    author_ref="reviewer",
                    review_notes_path=link,
                )
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
