"""원격 artifact bytes·scoped source·검토 뒤 불변성을 다시 확인한다. 테스트 재실행 없음."""
import base64, hashlib, json, platform, subprocess, sys
from pathlib import Path
root=Path(__file__).resolve().parent;packet=root/'packet'
base='32bb0f9dd9f024045d24487312b50f5b703573a3'
sha=lambda b:hashlib.sha256(b).hexdigest()
commands=[]
def run(argv,cwd):
 r=subprocess.run(argv,cwd=cwd,capture_output=True,text=True)
 commands.append({'argv':argv,'cwd':str(cwd),'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
 assert r.returncode==0,(argv,r.stderr)
 return r.stdout.strip()
verified=[]
for row in json.loads((root/'remote-packet.json').read_text()):
 body=base64.b64decode(row['data']['content']);rel=row['path'].split('proposals/vm-g-failure-20261001/',1)[1]
 assert hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()==row['data']['sha']
 assert body==(packet/rel).read_bytes()
 verified.append({'path':row['path'],'bytes':len(body),'sha256':sha(body),'git_blob_sha':row['data']['sha']})
index_results=[]
for filename in ['packet-index.json','delivery-manifest.json']:
 manifest=json.loads((packet/filename).read_text())
 for name,meta in manifest['files'].items():
  body=(packet/name).read_bytes();assert sha(body)==meta['sha256'] and len(body)==meta['bytes'],name
 index_results.append({'file':filename,'matched_entries':len(manifest['files']),'mismatches':0})
claimed=json.loads((packet/'source-identity.json').read_text())
identities={}
for label in ['base','candidate']:
 source=root/label
 assert run(['git','rev-parse','HEAD'],source)==base
 hashes={str(p.relative_to(source)):sha(p.read_bytes()) for p in sorted((source/'src').rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
 assert hashes==claimed[label+'_source_hashes']
 manifest_sha=sha(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode())
 assert manifest_sha==claimed[label+'_src_sha256_manifest']
 identities[label]={'head':base,'source_manifest_sha256':manifest_sha,'source_files':len(hashes),
  'tracked_status':run(['git','status','--porcelain=v1','--untracked-files=no'],source)}
assert identities['base']['tracked_status']==''
diff_names=run(['git','diff','--name-only'],root/'candidate').splitlines()
assert diff_names==['src/flowmarshal/engine/runtime.py','src/flowmarshal/engine/service.py']
run(['git','diff','--check'],root/'candidate')
run(['git','apply','--check','--whitespace=error',str(packet/'interrupt-delivery-proposal.patch')],root/'base')
shared={'head':run(['git','rev-parse','HEAD'],Path('<BASE_CHECKOUT>')),
 'status':run(['git','status','--porcelain=v1'],Path('<BASE_CHECKOUT>')),
 'branch':run(['git','branch','--show-current'],Path('<BASE_CHECKOUT>'))}
assert shared['head']==base and shared['status']==''
out={'task_id':'vm-interrupt-independent-review-20261002','source_thread_id':'01a0f995-7341-7179-91a4-63a814e66020',
 'remote_evidence_commit':'6c28f9f271f4e99e1ee81c05db75dc342a022558','remote_verified_files':len(verified),
 'artifact_hashes':verified,'author_manifest_checks':index_results,'source_identities':identities,'shared_checkout':shared,
 'patch_sha256':sha((packet/'interrupt-delivery-proposal.patch').read_bytes()),'scoped_changed_paths':diff_names,
 'python_executable':sys.executable,'python_version':platform.python_version(),'os':platform.platform(),
 'node_version':run(['node','--version'],root),'commands':commands,
 'requested_model':'gpt-6.1-sol','requested_effort':'high','actual_observable_model':None,'actual_observable_effort':None,
 'actual_settings_note':'No authoritative orchestration model/effort metadata exposed; execution used local Python synthetic adapters only.',
 'provider_auth_consulted':False,'live_provider_calls':0,'operating_db_profile_accessed':False,'fallback':False}
(root/'input-verification.json').write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps({k:v for k,v in out.items() if k not in ['commands','artifact_hashes']},sort_keys=True))
