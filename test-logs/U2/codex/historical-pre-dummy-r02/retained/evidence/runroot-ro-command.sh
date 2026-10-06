/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,json,datetime,os
source=pathlib.Path('/workspace/vm-u2-source-24cf-r02');run=pathlib.Path('/workspace/vm-u2-run-24cf-r02')
def components(p):
 return [{'path':str(a),'lexists':os.path.lexists(a),'symlink':a.is_symlink(),'resolve':str(a.resolve(strict=False))} for a in [pathlib.Path('/'),*reversed(p.parents[:-1]),p]]
def overlap(a,b):return a.is_relative_to(b) or b.is_relative_to(a)
owned=[p for p in pathlib.Path('/workspace').iterdir() if p.name.startswith(('ags-','vm-')) or p.name=='agent-governance-suite']
cwd=source/'pre-cwd';packet=source/'packet'
checks={'futureRunAbsent':not os.path.lexists(run),'futureRunResolveExact':str(run.resolve())==str(run),'futureAncestorsNoSymlink':not any(x['symlink'] for x in components(run)),'uppercaseFMAbsent':not any(x.startswith('FM-') for x in run.resolve().parts),'uriCharactersAbsent':not any(c in str(run.resolve()) for c in ['%','#','?','\x00']),'runOwnedNonOverlap':not any(overlap(run.resolve(),p.resolve()) for p in owned),'sourceOwnedNonOverlap':not any(overlap(source.resolve(),p.resolve()) for p in owned if p!=source),'sourceRunNonOverlap':not overlap(source.resolve(),run.resolve()),'sourceResolveExact':str(source.resolve())==str(source),'sourceAncestorsNoSymlink':not any(x['symlink'] for x in components(source)),'packetResolveExact':str(packet.resolve())==str(packet),'preCwdResolveExact':str(cwd.resolve())==str(cwd),'preCwdNoSymlink':not cwd.is_symlink(),'preCwdEmpty':not list(cwd.iterdir()),'parentWritable':os.access(run.parent,os.W_OK),'parentSearchable':os.access(run.parent,os.X_OK)}
out={'scope':'U2_R02_PRE_RUNROOT_RO_ONLY','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sourceRoot':str(source),'sourceResolve':str(source.resolve()),'sourceIsEmpty':not list(source.iterdir()),'sourceFreshEmptyBeforeStaging':'Created as empty root, then supplied pinned packet15; source root is now populated intentionally','packetRoot':str(packet),'preCwd':str(cwd),'preCwdResolve':str(cwd.resolve()),'futureRunRoot':str(run),'futureRunResolve':str(run.resolve()),'futureRunAncestors':components(run),'sourceAncestors':components(source),'ownedRootsCompared':[str(p) for p in owned],'checks':checks,'runRootCreated':False,'externalJobs':'UNKNOWN','priorUnreadableProcesses':7,'OSContainment':'UNKNOWN','nativeToolId':None,'productExecution':0,'futureProductDispatch':'NOT_RUN; requires separate new fixed template/PRE/instruction'}
p=source/'evidence/runroot-ro.json'
with p.open('xb') as f:f.write((json.dumps(out,indent=2)+'\n').encode())
os.chmod(p,0o600)
print(json.dumps(out,indent=2))
raise SystemExit(0 if all(checks.values()) else 2)
PY