"""Canonical shape와 기록한 protocol invariant만 검증한다. live capability/fact proof가 아니다."""
import hashlib, json
from pathlib import Path
from jsonschema import Draft202012Validator

root = Path(__file__).resolve().parent
record = json.loads((root / "decision-record.json").read_text())
schema = json.loads((root / "decision-record.v1.schema.json").read_text())
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema).validate(record)
run = record["run"]
workers = run["workers"]
assert workers == record["panel_manifest"]
assert len(workers) == record["observability"]["worker_count"] == 4
assert len({w["id"] for w in workers}) == len(workers)
assert all(w["status"] == "completed" and w["instantiated"] for w in workers)
assert set(run["completed_worker_ids"]) == {w["id"] for w in workers}
reviewers = [w for w in workers if w["classification"] == "reviewer"]
judges = [w for w in workers if w["classification"] == "judge"]
assert len(reviewers) == 3 and len(judges) == 1
assert all(w["blind_round1"] and w["context_isolated"] for w in reviewers)
judge = judges[0]
assert judge["id"] == run["fresh_judge_id"]
assert judge["context_isolated"] and judge["participated_stages"] == ["final_judge"]
assert all(w["requested_model"] == "gpt-6.1-sol" and w["requested_reasoning"] == "high"
           and w["actual_model"] is None and w["actual_reasoning"] is None
           and w["fallback_reason"] is None for w in workers)
assert run["strict"] and not run["capability_shortfall"]
assert not record["preflight"]["missing_capabilities"]
assert run["assurance"] == "independent"
assert not run["specialist_additions"] and not run["redeliberations"]
assert record["constraints"] == record["case_brief"]["constraints"]
assert set(record["required_constraints"]) <= set(record["constraints"])
proposal = record["consensus_proposal"]
assert proposal["status"] == "conditional_consensus"
assert proposal["satisfied_constraints"] == record["required_constraints"]
verified = {c["id"] for c in record["material_claims"]
            if any(p["verification_status"] == "verified" for p in c["provenance"])}
assert set(proposal["supported_by_verified_claims"]) <= verified
for axis in record["axis_decisions"]:
    assert axis["evidence_claim_ids"] and set(axis["evidence_claim_ids"]) <= verified
cross = record["cross_examination"]
assert cross["decision"] == "run"
triggers = {t["id"]: t["origin_reviewer"] for t in cross["trigger_items"]}
assert set(triggers) == set(cross["selected_item_ids"])
assert len({f["reviewer_id"] for f in cross["followups"]}) == len(cross["followups"])
for item in cross["coverage"]:
    assert item["item_id"] in triggers
    assert triggers[item["item_id"]] not in item["reviewer_ids"]
    for wid in item["reviewer_ids"]:
        assert any(f["reviewer_id"] == wid and item["item_id"] in f["item_ids"]
                   for f in cross["followups"])
for follow in cross["followups"]:
    assert 1 <= len(follow["item_ids"]) <= 2
    for item in follow["item_ids"]:
        assert any(c["item_id"] == item and follow["reviewer_id"] in c["reviewer_ids"]
                   for c in cross["coverage"])
life = json.loads((root / "identity-lifecycle.json").read_text())
spawn = next(e for e in life["events"] if e.get("role") == "fresh_judge")
assert all(e["seq"] < spawn["seq"] for e in life["events"]
           if e.get("phase") in ("round1", "cross_examination")
           and e.get("completed_id"))
assert spawn["dossier_sha256"] == hashlib.sha256((root / "judge-dossier.json").read_bytes()).hexdigest()
assert spawn["id"] == judge["id"] and not spawn["prior_participation"]
manifest_path = root / "artifact-manifest.json"
if manifest_path.exists():
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["artifacts"]:
        path = root / item["path"]
        assert path.is_file() and path.resolve().is_relative_to(root.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
print("PASS: canonical schema, stored protocol invariants, frozen Judge dossier, artifact hashes")
print("Limit: this validates recorded consistency, not hidden/effective model settings or external facts.")
