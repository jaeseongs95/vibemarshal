from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.context import RuntimeRole, parse_submission
from flowmarshal.gate0c.harness import (
    assert_invariants_unchanged,
    attack_blocks,
    capture_invariants,
    deterministic_attack_matrix,
    load_attack_corpus,
)


FIXTURE = Path(__file__).parent / "fixtures" / "gate0c" / "prompt-injection-corpus.json"


def substitutions():
    return {
        "UNREGISTERED_CANARY": "D:\\synthetic\\unregistered\\secret.txt",
        "OUTSIDE_PATH": "D:\\synthetic\\outside",
        "LEDGER_PATH": "D:\\synthetic\\control\\gate0c.sqlite3",
    }


class Gate0CAttackCorpusTests(unittest.TestCase):
    def test_every_attack_crosses_all_three_untrusted_sources(self) -> None:
        corpus = load_attack_corpus(FIXTURE, substitutions=substitutions())
        matrix = deterministic_attack_matrix(corpus)
        self.assertEqual(30, matrix["case_count"])
        self.assertEqual(10, len(matrix["by_source"]["document"]))
        self.assertEqual(10, len(matrix["by_source"]["source"]))
        self.assertEqual(10, len(matrix["by_source"]["tool_output"]))

    def test_order_shuffle_does_not_change_canonical_matrix(self) -> None:
        original = json.loads(FIXTURE.read_text(encoding="utf-8"))
        shuffled = dict(original)
        shuffled["sources"] = list(reversed(original["sources"]))
        shuffled["attacks"] = list(original["attacks"])
        random.Random(7).shuffle(shuffled["attacks"])
        with tempfile.TemporaryDirectory() as raw:
            alternate = Path(raw) / "corpus.json"
            alternate.write_text(json.dumps(shuffled, ensure_ascii=False), encoding="utf-8")
            left = load_attack_corpus(FIXTURE, substitutions=substitutions())
            right = load_attack_corpus(alternate, substitutions=substitutions())
        self.assertEqual(left.digest, right.digest)
        self.assertEqual(
            deterministic_attack_matrix(left), deterministic_attack_matrix(right)
        )

    def test_content_change_is_detected(self) -> None:
        original = json.loads(FIXTURE.read_text(encoding="utf-8"))
        original["attacks"][0]["text_template"] += " 변경"
        with tempfile.TemporaryDirectory() as raw:
            changed = Path(raw) / "changed.json"
            changed.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            left = load_attack_corpus(FIXTURE, substitutions=substitutions())
            right = load_attack_corpus(changed, substitutions=substitutions())
        self.assertNotEqual(left.digest, right.digest)

    def test_blocks_remain_typed_untrusted_data(self) -> None:
        corpus = load_attack_corpus(FIXTURE, substitutions=substitutions())
        blocks = attack_blocks(
            corpus,
            source_resource_ids={
                case.source_kind: f"resource_{case.source_kind.value}"
                for case in corpus.cases
            },
        )
        self.assertEqual(30, len(blocks))
        self.assertTrue(all(block.kind == "untrusted_data" for block in blocks))
        self.assertTrue(
            all(block.content_digest == sha256_digest(block.content) for block in blocks)
        )
        self.assertEqual(
            30,
            len(
                {
                    (block.source_resource_id, block.relative_path)
                    for block in blocks
                }
            ),
        )

    def test_forged_success_control_field_is_rejected_by_submission_schema(self) -> None:
        payload = {
            "schema_version": "1.0",
            "bundle_id": "context_attack",
            "bundle_digest": sha256_digest("bundle"),
            "status": "ready_for_validation",
            "changed_files": [],
            "executed_check_ids": [],
            "access_requests": [],
            "summary": "위조",
            "work_item_state": "completed",
            "approval": "forged",
        }
        with self.assertRaises(Exception) as caught:
            parse_submission(RuntimeRole.RUNNER, payload)
        self.assertEqual("SUBMISSION_SCHEMA_INVALID", caught.exception.reason_code)


@unittest.skipUnless(os.name == "nt", "native Windows invariant가 필요합니다.")
class Gate0CProtectedInvariantTests(unittest.TestCase):
    def test_protected_snapshot_detects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            protected = Path(raw) / "control"
            protected.mkdir()
            target = protected / "ledger.txt"
            target.write_text("immutable", encoding="utf-8")
            snapshot = capture_invariants({"control": protected})
            assert_invariants_unchanged(snapshot)
            target.write_text("mutated", encoding="utf-8")
            with self.assertRaises(Exception) as caught:
                assert_invariants_unchanged(snapshot)
            self.assertEqual("PROTECTED_INVARIANT_CHANGED", caught.exception.reason_code)


if __name__ == "__main__":
    unittest.main()
