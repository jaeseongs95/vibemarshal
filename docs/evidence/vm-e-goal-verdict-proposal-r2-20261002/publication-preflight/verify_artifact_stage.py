import hashlib,json,subprocess
from pathlib import Path
root=Path('docs/evidence/vm-e-goal-verdict-proposal-r2-20261002')
changes=subprocess.check_output(['git','diff','--cached','--name-status'],text=True).splitlines()
assert len(changes)==len(json.loads((root/'publication-inventory.json').read_text())['files'])+2,len(changes)
assert all(x.startswith('A\t'+str(root)+'/') for x in changes),changes
inventory=json.loads((root/'publication-inventory.json').read_text())
for item in inventory['files']:
    content=(root/item['path']).read_bytes()
    assert len(content)==item['bytes']
    assert hashlib.sha256(content).hexdigest()==item['sha256']
    if 'source' in item:
        assert (Path(item['source']).read_bytes().startswith(content) if item.get('source_snapshot_type') else content==Path(item['source']).read_bytes())
assert not subprocess.check_output(['git','diff','--name-only'],text=True).strip()
assert not subprocess.check_output(['git','diff','--cached','--name-only','--','src','tests'],text=True).strip()
print(json.dumps({'artifact_only':True,'added_files':len(changes),'product_changes_in_artifact_commit':0,'all_manifest_hashes_verified':True,'original_evidence_bytes_verified':True,'no_new_tests':True},indent=2))
