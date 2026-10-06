/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,os,json,datetime,hashlib
source=pathlib.Path('/workspace/vm-u2-source-24cf-r02');run=pathlib.Path('/workspace/vm-u2-run-24cf-r02')
def components(p):
 return [{'path':str(a),'lexists':os.path.lexists(a),'symlink':a.is_symlink(),'resolve':str(a.resolve(strict=False))} for a in [pathlib.Path('/'),*reversed(p.parents[:-1]),p]]
def overlap(a,b):return a.is_relative_to(b) or b.is_relative_to(a)
owned=[p for p in pathlib.Path('/workspace').iterdir() if p.name.startswith(('ags-','vm-')) or p.name=='agent-governance-suite']
paths={label:{'path':str(p),'lexists':os.path.lexists(p),'resolve':str(p.resolve(strict=False)),'components':components(p),'canonicalUpperFM':any(x.startswith('FM-') for x in p.resolve().parts),'uriAmbiguousCharactersPresent':any(c in str(p.resolve()) for c in ['%','#','?','\x00']),'overlapOwned':[str(x) for x in owned if overlap(p.resolve(),x.resolve())]} for label,p in [('source',source),('futureRun',run)]}
exe=pathlib.Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12');b=exe.read_bytes()
checks={'bothAbsent':all(not x['lexists'] for x in paths.values()),'bothCanonical':all(x['path']==x['resolve'] for x in paths.values()),'ancestorsNoSymlinks':all(not r['symlink'] for x in paths.values() for r in x['components']),'noFM':all(not x['canonicalUpperFM'] for x in paths.values()),'noURIAmbiguity':all(not x['uriAmbiguousCharactersPresent'] for x in paths.values()),'noOwnedOverlap':all(not x['overlapOwned'] for x in paths.values()),'sourceRunNonOverlap':not overlap(source.resolve(),run.resolve()),'parentWritable':os.access(source.parent,os.W_OK),'exePinMatch':len(b)==30894944 and hashlib.sha256(b).hexdigest()=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7'}
instructions=[]
for p in [pathlib.Path('/AGENTS.md'),pathlib.Path('/workspace/AGENTS.md')]:
 row={'path':str(p),'exists':p.is_file()}
 if p.is_file():row['text']=p.read_text()
 instructions.append(row)
ag=pathlib.Path('/workspace/.agents');skillFiles=[str(p) for p in ag.rglob('SKILL.md')] if ag.is_dir() else []
out={'scope':'U2_R02_STAGING_PREFLIGHT_RO','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'paths':paths,'checks':checks,'executable':{'path':str(exe),'resolve':str(exe.resolve()),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()},'workspaceInstructions':instructions,'agentsSkillFiles':skillFiles,'externalJobs':'UNKNOWN','priorUnreadableProcesses':7,'OSContainment':'UNKNOWN','productExecution':0,'writes':0}
print(json.dumps(out,indent=2))
raise SystemExit(0 if all(checks.values()) else 2)
PY