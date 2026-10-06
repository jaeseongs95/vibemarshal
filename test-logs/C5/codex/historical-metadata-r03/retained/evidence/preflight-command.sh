/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,os,json,datetime,hashlib
source=pathlib.Path('/workspace/vm-c5-source-24cf-r03');run=pathlib.Path('/workspace/vm-c5-run-24cf-r03')
def components(p):return [{'path':str(x),'lexists':os.path.lexists(x),'symlink':x.is_symlink(),'resolve':str(x.resolve(strict=False))} for x in [pathlib.Path('/'),*reversed(p.parents[:-1]),p]]
def overlap(a,b):return a.is_relative_to(b) or b.is_relative_to(a)
owned=list(pathlib.Path('/workspace').iterdir())
paths={name:{'path':str(p),'lexists':os.path.lexists(p),'resolve':str(p.resolve()),'components':components(p),'workspaceOverlaps':[str(x) for x in owned if overlap(p.resolve(),x.resolve())],'upperFM':any(x.startswith('FM-') for x in p.resolve().parts),'uriAmbiguous':any(c in str(p.resolve()) for c in ['%','#','?','\x00'])} for name,p in [('source',source),('futureRun',run)]}
exe=pathlib.Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12');b=exe.read_bytes()
checks={'bothAbsent':all(not x['lexists'] for x in paths.values()),'canonical':all(x['path']==x['resolve'] for x in paths.values()),'noSymlink':all(not a['symlink'] for x in paths.values() for a in x['components']),'nonOverlap':all(not x['workspaceOverlaps'] for x in paths.values()),'noFM':all(not x['upperFM'] for x in paths.values()),'noURI':all(not x['uriAmbiguous'] for x in paths.values()),'sourceRunNonOverlap':not overlap(source.resolve(),run.resolve()),'parentWritableSearchable':os.access(source.parent,os.W_OK|os.X_OK),'exeFixedPin':len(b)==30894944 and hashlib.sha256(b).hexdigest()=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7'}
out={'scope':'C5_R03_PRE_STAGING_RO','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'paths':paths,'checks':checks,'knownExe':{'path':str(exe),'resolve':str(exe.resolve()),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()},'externalJobs':'UNKNOWN','unreadableProcessObservation':'UNKNOWN; prior evidence 7 retained without current process enumeration','OSContainment':'UNKNOWN','writes':0,'productExecution':0}
print(json.dumps(out,indent=2))
raise SystemExit(0 if all(checks.values()) else 2)
PY