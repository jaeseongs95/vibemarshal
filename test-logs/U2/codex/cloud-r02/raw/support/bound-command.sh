/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,os,sys,json,hashlib,datetime,time
root=pathlib.Path('/workspace/vm-u2-source-24cf-r02');packet=root/'packet';run=pathlib.Path('/workspace/vm-u2-run-24cf-r02');cwd=root/'pre-cwd';exe=pathlib.Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12')
sha=lambda b:hashlib.sha256(b).hexdigest()
deadline=datetime.datetime.fromisoformat('2026-10-04T18:05:03.000Z');started=datetime.datetime.now(datetime.timezone.utc);checks={};errors=[];pins=[];modulepins=[]
try:
 checks['beforeDeadlineAtGuardStart']=started<=deadline
 sealb=(packet/'SOURCE.SHA256.json').read_bytes()
 checks['sealExternalPin']=len(sealb)==1995 and sha(sealb)=='0c1c54b6643cfb514df94cf1160a41eca46c086dce0e042254658801afcc22d2'
 assert checks['sealExternalPin']
 seal=json.loads(sealb);actual=[p for p in packet.rglob('*') if p.is_file()]
 checks['exactSource15']=len(actual)==15 and {p.relative_to(packet).as_posix() for p in actual}==set(seal)|{'SOURCE.SHA256.json'}
 checks['sourceTotalBytes']=sum(p.stat().st_size for p in actual)==724947
 for p in sorted(actual):
  name=p.relative_to(packet).as_posix();b=p.read_bytes();pin=seal.get(name,{'bytes':1995,'sha256':'0c1c54b6643cfb514df94cf1160a41eca46c086dce0e042254658801afcc22d2'})
  ok=not p.is_symlink() and len(b)==pin['bytes'] and sha(b)==pin['sha256'];pins.append({'path':name,'bytes':len(b),'sha256':sha(b),'match':ok})
 checks['allSourcePins']=all(p['match'] for p in pins)
 checks['runnerExternalPin']=seal['runner.py']=={'bytes':20603,'sha256':'cfc6e0f438f42ba453d503475c54a3dd8959c06ae5bdef83b48fa760166f2cee'}
 checks['manifestExternalPin']=seal['manifest.json']=={'bytes':5916,'sha256':'90a8ac8ee16171b735a105ba27e34b3d60e6ed0e520133e15cf16513ab2a11af'}
 b=exe.read_bytes();checks['executablePin']=len(b)==30894944 and sha(b)=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7' and exe.resolve()==exe
 raw=(root/'evidence/linux-pre.stdout.json').read_bytes();checks['preExternalPin']=len(raw)==98011 and sha(raw)=='bad4ed5ef98f679338d065308a0c51cf0e3547d5586dd8af83f099ad90cd5abb'
 assert checks['preExternalPin'];pre=json.loads(raw)
 for row in pre['selectedStdlibAndNativePins']:
  if row['file'] and row['bytes'] is not None:
   p=pathlib.Path(row['file']);b=p.read_bytes();modulepins.append({'name':row['name'],'path':str(p),'bytes':len(b),'sha256':sha(b),'match':len(b)==row['bytes'] and sha(b)==row['sha256']})
 checks['eightStdlibFilePins']=len(modulepins)==8 and {x['name'] for x in modulepins}=={'sqlite3','sqlite3.dbapi2','ssl','unittest','unittest.mock','subprocess','contextlib','inspect'} and all(x['match'] for x in modulepins)
 checks['dummyResultEvidencePin']=sha((root/'evidence/linux-dummy/result.json').read_bytes())=='55b72eeb8c452bbc84dd7f9d9eb799c4c33b4678fc816dd1682da78e889cde8d'
 checks['dummyExecutionEvidencePin']=sha((root/'evidence/linux-dummy/execution.json').read_bytes())=='658bf6c6595b8238b2e37263e8678a96aaef1848e7fb176c2b24a62faab896e5'
 checks['linuxPython312']=sys.platform==pre['python']['platform']=='linux' and sys.version==pre['python']['version']=='3.12.14 (main, Aug 25 2026, 14:00:49) [Clang 22.1.3 ]'
 checks['actualFlags']=bool(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and sys.flags.utf8_mode)
 checks['actualExecutable']=pathlib.Path(sys.executable)==exe
 checks['sourceCanonical']=root.resolve()==root and packet.resolve()==packet and all(not p.is_symlink() for p in [root,packet,*root.parents])
 resolved=run.resolve(strict=False);ancestors=[pathlib.Path('/'),*reversed(run.parents[:-1]),run]
 checks['runrootAbsent']=not os.path.lexists(run);checks['runrootResolveExact']=resolved==run
 checks['ancestorsNoSymlink']=all(not p.is_symlink() for p in ancestors)
 checks['canonicalUpperFMZero']=not any(x.startswith('FM-') for x in resolved.parts)
 checks['canonicalURIUnambiguous']=not any(x in str(resolved) for x in ('%','#','?','\x00'))
 owned=[p for p in pathlib.Path('/workspace').iterdir() if p.name.startswith(('ags-','vm-private-','vm-')) or p.name=='agent-governance-suite']
 checks['nonOverlap']=all(not resolved.is_relative_to(p.resolve()) and not p.resolve().is_relative_to(resolved) for p in owned)
 checks['actualParentCwd']=pathlib.Path.cwd()==cwd
 checks['parentCwdExactEmpty']=cwd.resolve()==cwd and cwd.is_dir() and not cwd.is_symlink() and not any(cwd.iterdir())
except Exception as e:errors.append({'type':type(e).__name__,'message':str(e)})
ended=datetime.datetime.now(datetime.timezone.utc);checks['beforeDeadlineAtGuardEnd']=ended<=deadline
passed=all(checks.values()) and not errors
argv=[str(exe),'-I','-S','-B','-X','utf8',str(packet/'runner.py'),'--run-root',str(run),'--runner-sha256','cfc6e0f438f42ba453d503475c54a3dd8959c06ae5bdef83b48fa760166f2cee','--manifest-sha256','90a8ac8ee16171b735a105ba27e34b3d60e6ed0e520133e15cf16513ab2a11af','--timeout','300']
out={'scope':'SINGLE_LAUNCH_FRESHNESS_ONLY','peerId':'08151dd4-2745-48ee-a451-aca9a8af0268','issuedUtc':'2026-10-04T18:03:03.000Z','launchNotAfterUtc':'2026-10-04 18:05:03 UTC','guardStartUtc':started.isoformat(),'guardEndUtc':ended.isoformat(),'guardArgv':[sys.executable,'-I','-S','-B','-X','utf8','-'],'guardCwd':str(pathlib.Path.cwd()),'checks':checks,'source15':pins,'stdlib8':modulepins,'errors':errors,'passed':passed,'status':'FRESHNESS_PASS_PENDING_NATIVE_DISPATCH' if passed else 'NOT_RUN','runnerInvocationsSoFar':0,'externalJobs':'UNKNOWN','priorUnreadableProcesses':'7','nativeCommandId':None}

print(json.dumps(out,sort_keys=True),flush=True)
if not passed:raise SystemExit(2)
dispatch=datetime.datetime.now(datetime.timezone.utc)
if dispatch>deadline:
 print(json.dumps({'scope':'U2_FINAL_DEADLINE_GATE','utc':dispatch.isoformat(),'status':'NOT_RUN_DEADLINE_EXPIRED','runnerInvocations':0}),flush=True)
 raise SystemExit(124)
print(json.dumps({'scope':'U2_OS_EXECV_DISPATCH','utc':dispatch.isoformat(),'argv':argv,'cwd':str(cwd),'method':'os.execv same process replacement; no additional child','runnerInvocationRequested':1,'retries':0}),flush=True)
try:os.execv(str(exe),argv)
except OSError as error:
 print(json.dumps({'scope':'U2_EXECV_FAILURE','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'UNKNOWN_EXEC_FAILED','errorType':type(error).__name__,'error':str(error),'retry':0}),flush=True)
 raise

PY
