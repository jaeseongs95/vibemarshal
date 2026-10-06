/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,os,sys,json,hashlib,datetime,time
root=pathlib.Path('/workspace/vm-common3-source-24cf-r03');packet=root/'packet';run=pathlib.Path('/workspace/vm-common3-run-24cf-r03');cwd=root/'pre-cwd';exe=pathlib.Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12')
sha=lambda b:hashlib.sha256(b).hexdigest()
deadline=datetime.datetime.fromisoformat('2026-10-04T14:58:17+00:00');started=datetime.datetime.now(datetime.timezone.utc);checks={};errors=[];pins=[];modulepins=[]
try:
 checks['beforeDeadlineAtGuardStart']=started<=deadline
 sealb=(packet/'SOURCE.SHA256.json').read_bytes()
 checks['sealExternalPin']=len(sealb)==1146 and sha(sealb)=='321ee6055812cdd7b2648bdc27f67eaac6d7fd2bcde309c8ad1d503bf988bc86'
 assert checks['sealExternalPin']
 seal=json.loads(sealb);actual=[p for p in packet.rglob('*') if p.is_file()]
 checks['exactSource9']=len(actual)==9 and {p.relative_to(packet).as_posix() for p in actual}==set(seal)|{'SOURCE.SHA256.json'}
 checks['sourceTotalBytes']=sum(p.stat().st_size for p in actual)==625583
 for p in sorted(actual):
  name=p.relative_to(packet).as_posix();b=p.read_bytes();pin=seal.get(name,{'bytes':1146,'sha256':'321ee6055812cdd7b2648bdc27f67eaac6d7fd2bcde309c8ad1d503bf988bc86'})
  ok=not p.is_symlink() and len(b)==pin['bytes'] and sha(b)==pin['sha256'];pins.append({'path':name,'bytes':len(b),'sha256':sha(b),'match':ok})
 checks['allSourcePins']=all(p['match'] for p in pins)
 checks['runnerExternalPin']=seal['runner.py']=={'bytes':15249,'sha256':'457fd65598bc5cb5a87dd46abb7036bac9768db20a932053fb9670b201862136'}
 checks['manifestExternalPin']=seal['manifest.json']=={'bytes':3381,'sha256':'ae0260a8fd9803cc716a8eab504e34b9bdfd919f39695e8bced5a896c116c3ad'}
 b=exe.read_bytes();checks['executablePin']=len(b)==30894944 and sha(b)=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7' and exe.resolve()==exe
 raw=(root/'evidence/linux-pre.stdout.json').read_bytes();checks['preExternalPin']=len(raw)==9799 and sha(raw)=='2de9b103aaba3a77d3f087b42d816896d9cbf7a233d7d7567e2e381236431e8a'
 assert checks['preExternalPin'];pre=json.loads(raw)
 for row in pre['selectedStdlibAndNativePins']:
  if row['file'] and row['bytes'] is not None:
   p=pathlib.Path(row['file']);b=p.read_bytes();modulepins.append({'name':row['name'],'path':str(p),'bytes':len(b),'sha256':sha(b),'match':len(b)==row['bytes'] and sha(b)==row['sha256']})
 checks['sixStdlibFilePins']=len(modulepins)==6 and all(x['match'] for x in modulepins)
 checks['linuxPython312']=pre['python']['platform']=='linux' and pre['python']['version'].startswith('3.12.')
 resolved=run.resolve(strict=False);ancestors=[pathlib.Path('/'),*reversed(run.parents[:-1]),run]
 checks['runrootAbsent']=not os.path.lexists(run);checks['runrootResolveExact']=resolved==run
 checks['ancestorsNoSymlink']=all(not p.is_symlink() for p in ancestors)
 checks['canonicalUpperFMZero']=not any(x.startswith('FM-') for x in resolved.parts)
 owned=[p for p in pathlib.Path('/workspace').iterdir() if p.name.startswith(('ags-','vm-private-','vm-common3-')) or p.name=='agent-governance-suite']
 checks['nonOverlap']=all(not resolved.is_relative_to(p.resolve()) and not p.resolve().is_relative_to(resolved) for p in owned)
 checks['parentCwdExactEmpty']=cwd.resolve()==cwd and cwd.is_dir() and not cwd.is_symlink() and not any(cwd.iterdir())
except Exception as e:errors.append({'type':type(e).__name__,'message':str(e)})
ended=datetime.datetime.now(datetime.timezone.utc);checks['beforeDeadlineAtGuardEnd']=ended<=deadline
passed=all(checks.values()) and not errors
argv=[str(exe),'-I','-S','-B','-X','utf8',str(packet/'runner.py'),'--run-root',str(run),'--runner-sha256','457fd65598bc5cb5a87dd46abb7036bac9768db20a932053fb9670b201862136','--manifest-sha256','ae0260a8fd9803cc716a8eab504e34b9bdfd919f39695e8bced5a896c116c3ad','--timeout','300']
out={'scope':'SINGLE_LAUNCH_FRESHNESS_ONLY','peerId':'df1ecb87-c6c8-4004-b658-2f8bc3322b34','issuedUtc':'2026-10-04T14:56:17Z','launchNotAfterUtc':'2026-10-04T14:58:17.000Z','guardStartUtc':started.isoformat(),'guardEndUtc':ended.isoformat(),'guardArgv':[sys.executable,'-I','-S','-B','-'],'guardCwd':str(pathlib.Path.cwd()),'checks':checks,'source9':pins,'stdlib6':modulepins,'errors':errors,'passed':passed,'status':'FRESHNESS_PASS_PENDING_NATIVE_DISPATCH' if passed else 'NOT_RUN','runnerInvocationsSoFar':0,'externalJobs':'UNKNOWN','priorUnreadableProcesses':7,'nativeCommandId':None}

print(json.dumps(out,sort_keys=True),flush=True)
if not passed:raise SystemExit(2)
dispatch=datetime.datetime.now(datetime.timezone.utc)
if dispatch>deadline:
 print(json.dumps({'scope':'R02_FINAL_DEADLINE_GATE','utc':dispatch.isoformat(),'status':'NOT_RUN_DEADLINE_EXPIRED','runnerInvocations':0}),flush=True)
 raise SystemExit(124)
print(json.dumps({'scope':'R02_OS_EXECV_DISPATCH','utc':dispatch.isoformat(),'argv':argv,'cwd':str(cwd),'method':'os.execv same process replacement; no additional child','runnerInvocationRequested':1,'retries':0}),flush=True)
try:os.execv(str(exe),argv)
except OSError as error:
 print(json.dumps({'scope':'R02_EXECV_FAILURE','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'UNKNOWN_EXEC_FAILED','errorType':type(error).__name__,'error':str(error),'retry':0}),flush=True)
 raise

PY
