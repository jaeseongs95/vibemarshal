from pathlib import Path
import subprocess,hashlib,json,os,sys,importlib.metadata
repo=Path('/workspace/vibemarshal')
base='32bb0f9dd9f024045d24487312b50f5b703573a3';candidate='98bb122970f82bb5fcef122003ee10c7eb787cf4';packet='d6956481a964f1b84fb88baa03218a3034f8978d'
def read(sha,path):return subprocess.check_output(['git','show',sha+':'+path],cwd=repo)
manifest=json.loads(read(packet,'docs/evidence/vm-e-goal-verdict-proposal-20261002/normative-sources.json'))
audit=[]
for case in manifest['cases']:
 for section in ('normative_sources','supporting_sources'):
  for source in case[section]:
   raw=read(base,source['path']);actual='\n'.join(raw.decode().splitlines()[source['first_line']-1:source['last_line']])
   row={'case':case['id'],'kind':source['kind'],'path':source['path'],'first_line':source['first_line'],'last_line':source['last_line'],'actual_sha256':hashlib.sha256(raw).hexdigest(),'hash_matches':hashlib.sha256(raw).hexdigest()==source['file_sha256'],'exact_text_matches':actual==source['exact_text']}
   assert row['hash_matches'] and row['exact_text_matches'],row
   audit.append(row)
paths=['AGENTS.md','docs/orchestration-redesign.md','docs/redesign-1.0-contract.md','docs/engine-cutover-adr.md','docs/r31-frozen-baseline.md','src/flowmarshal/engine/service.py','src/flowmarshal/engine/runtime.py','src/flowmarshal/engine/domain.py','src/flowmarshal/engine/ledger.py','src/flowmarshal/engine/plan_reuse.py','src/flowmarshal/engine/cli.py','src/flowmarshal/engine/console_host.py','tests/test_engine_ledger_service.py','tests/test_engine_goal_authorization.py']
source_hashes={p:hashlib.sha256(read(base,p)).hexdigest() for p in paths}
source_hashes['candidate:src/flowmarshal/engine/service.py']=hashlib.sha256(read(candidate,'src/flowmarshal/engine/service.py')).hexdigest()
source_hashes['candidate:tests/test_engine_goal_verdict_authority.py']=hashlib.sha256(read(candidate,'tests/test_engine_goal_verdict_authority.py')).hexdigest()
print(json.dumps({'audited_source_entries':audit,'source_hashes':source_hashes,'reviewer_thread_id':os.environ.get('CODEX_THREAD_ID'),'canonical_agent':'/root','requested_model':'gpt-6.1-sol','requested_effort':'high','provider_observed_model':None,'provider_observed_effort':None,'model_environment_setting':os.environ.get('CODEX_MODEL'),'effort_environment_setting':os.environ.get('CODEX_REASONING_EFFORT'),'observation_reason':'No provider turn receipt or authoritative runtime model/effort exposed; requested values are user-provided only.','python':sys.version,'os_name':os.name,'dependencies':{p:importlib.metadata.version(p) for p in ('pydantic','cryptography','openai-codex')}},ensure_ascii=False,indent=2))
