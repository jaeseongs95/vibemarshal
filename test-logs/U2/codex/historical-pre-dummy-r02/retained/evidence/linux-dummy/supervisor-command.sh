/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,sys,os,json,hashlib,subprocess,time,datetime
root=pathlib.Path('/workspace/vm-u2-source-24cf-r02')
packet=root/'packet';e=root/'evidence/linux-dummy'
exe='/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12'
sha=lambda b:hashlib.sha256(b).hexdigest()
utc=lambda:datetime.datetime.now(datetime.timezone.utc).isoformat()
pins={
 'runner.py':{'bytes':20603,'sha256':'cfc6e0f438f42ba453d503475c54a3dd8959c06ae5bdef83b48fa760166f2cee'},
 'regression/check_origin.py':{'bytes':8497,'sha256':'8321f2343c74a829b4e6509f0a0b2942a7808ffd81f84b45592d5fa6d0616098'},
 'regression/dummy_helper.py':{'bytes':300,'sha256':'b42b7cf911d2f08cd5cf023c0b914f7d910411509c5b8d515304bce60ca9133f'}
}
def inputs():
 return {name:{'bytes':len(b),'sha256':sha(b)} for name in pins for b in [(packet/name).read_bytes()]}
before=inputs();assert before==pins
exebytes=pathlib.Path(exe).read_bytes()
assert len(exebytes)==30894944 and sha(exebytes)=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7'
assert str(pathlib.Path.cwd())==str(root/'pre-cwd') and not list(pathlib.Path.cwd().iterdir())
assert not os.path.lexists('/workspace/vm-u2-run-24cf-r02')
e.mkdir(mode=0o700)
os.umask(0o077)
argv=[exe,'-I','-S','-B','-X','utf8',str(packet/'regression/check_origin.py'),'--runner-sha256',pins['runner.py']['sha256'],'--self-sha256',pins['regression/check_origin.py']['sha256'],'--dummy-sha256',pins['regression/dummy_helper.py']['sha256'],'--result',str(e/'result.json')]
env={'HOME':str(root),'TMPDIR':str(e),'TMP':str(e),'TEMP':str(e),'PATH':'/bin:/usr/bin'}
startedUtc=utc();start=time.monotonic();deadline=start+30.0
timedOut=False;killError=None;reapError=None
with (e/'stdout.txt').open('xb') as stdout,(e/'stderr.txt').open('xb') as stderr:
 proc=subprocess.Popen(argv,cwd=str(root/'pre-cwd'),env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr)
 pid=proc.pid
 try:proc.wait(timeout=max(0,deadline-time.monotonic()))
 except subprocess.TimeoutExpired:
  timedOut=True
  try:proc.kill()
  except OSError as error:killError=repr(error)
  try:proc.wait(timeout=max(0,deadline-time.monotonic()))
  except subprocess.TimeoutExpired as error:reapError=repr(error)
observedUtc=utc();elapsed=time.monotonic()-start
after=inputs()
files=[]
for name in ['result.json','stdout.txt','stderr.txt']:
 p=e/name;row={'path':str(p),'exists':p.is_file(),'bytes':None,'sha256':None}
 if p.is_file():
  b=p.read_bytes();row.update(bytes=len(b),sha256=sha(b));os.chmod(p,0o600)
 files.append(row)
result=None;resultError=None
if (e/'result.json').is_file():
 try:result=json.loads((e/'result.json').read_bytes())
 except (ValueError,OSError) as error:resultError=repr(error)
receipt={'scope':'U2_R02_STDLIB_DUMMY_ONLY_NOT_PRODUCT','supervisorPid':os.getpid(),'childPid':pid,'actualChildArgv':argv,'actualChildCwd':str(root/'pre-cwd'),'actualChildEnv':env,'supervisorProfile':[exe,'-I','-S','-B','-X','utf8','-'],'startedUtc':startedUtc,'observedUtc':observedUtc,'startedMonotonic':start,'deadlineMonotonic':deadline,'elapsedMonotonic':elapsed,'budgetSeconds':30,'actualExit':proc.returncode,'terminalObserved':proc.returncode is not None,'timedOut':timedOut,'ownChildKillError':killError,'ownChildReapError':reapError,'ownChildInvocations':1,'retries':0,'inputsBefore':before,'inputsAfter':after,'inputsUnchanged':before==after,'rawFiles':files,'resultParseError':resultError,'resultStatusRaw':result.get('status') if result else None,'productImportsLoadersTestsFixturesWholeRunnerStarts':0,'futureRunRootCreated':False,'nativeToolId':None,'providerNativeId':None,'externalJobs':'UNKNOWN','priorUnreadableProcesses':7,'OSContainment':'UNKNOWN'}
with (e/'execution.json').open('xb') as f:f.write((json.dumps(receipt,indent=2)+'\n').encode())
os.chmod(e/'execution.json',0o600)
cases=result.get('cases',[]) if result else []
journal=result.get('provenance_journal',[]) if result else []
summary={'scope':receipt['scope'],'childPid':pid,'startedUtc':startedUtc,'observedUtc':observedUtc,'elapsedMonotonic':elapsed,'actualExit':proc.returncode,'terminalObserved':receipt['terminalObserved'],'timedOut':timedOut,'resultStatusRaw':receipt['resultStatusRaw'],'inputsUnchanged':before==after,'caseCount':result.get('case_count') if result else None,'acceptedCount':sum(row['observed_accept'] for row in cases),'rejectedCount':sum(not row['observed_accept'] for row in cases),'journalRows':len(journal),'cases':[{'case':x['case'],'expectedAccept':x['expected_accept'],'observedAccept':x['observed_accept']} for x in cases],'cycleError':result.get('general_unwrap_cycle_error') if result else None,'nestedUnwrapsToOriginal':result.get('normal_nested_chain_unwraps_to_original') if result else None,'oldGuardAcceptsValidDecorated':result.get('old_guard_accepts_valid_decorated') if result else None,'dummyBodyCalls':result.get('dummy_body_calls') if result else None,'nullObjectObservation':next((x['observation'] for x in cases if x['case']=='code_less_object'),None),'rawFiles':files,'productExecution':0}
print(json.dumps(summary,indent=2))
raise SystemExit(0 if proc.returncode==0 and not timedOut else 2)
PY