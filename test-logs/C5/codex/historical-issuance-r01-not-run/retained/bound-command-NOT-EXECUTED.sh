/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,os,sys,json,hashlib,datetime,time
root=pathlib.Path('/workspace/vm-c5-source-24cf-r03');packet=root/'packet';run=pathlib.Path('/workspace/vm-c5-run-24cf-r03');cwd=root/'pre-cwd';exe=pathlib.Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12')
sha=lambda b:hashlib.sha256(b).hexdigest()
deadline=datetime.datetime.fromisoformat('2026-10-04T21:03:01+00:00');started=datetime.datetime.now(datetime.timezone.utc);checks={};errors=[];pins=[];modulepins=[]
try:
 checks['beforeDeadlineAtGuardStart']=started<=deadline
 sealb=(packet/'SOURCE.SHA256.json').read_bytes()
 checks['sealExternalPin']=len(sealb)==1425 and sha(sealb)=='1e12d8ab6f3a4f9fee880355edf4fd0f1ca6c47d795c07ed800fc073aa40344d'
 assert checks['sealExternalPin']
 seal=json.loads(sealb);actual=[p for p in packet.rglob('*') if p.is_file()]
 checks['exactSource11']=len(actual)==11 and {p.relative_to(packet).as_posix() for p in actual}==set(seal)|{'SOURCE.SHA256.json'}
 checks['sourceTotalBytes']=sum(p.stat().st_size for p in actual)==866064
 for p in sorted(actual):
  name=p.relative_to(packet).as_posix();b=p.read_bytes();pin=seal.get(name,{'bytes':1425,'sha256':'1e12d8ab6f3a4f9fee880355edf4fd0f1ca6c47d795c07ed800fc073aa40344d'})
  ok=not p.is_symlink() and len(b)==pin['bytes'] and sha(b)==pin['sha256'];pins.append({'path':name,'bytes':len(b),'sha256':sha(b),'match':ok})
 checks['allSourcePins']=all(p['match'] for p in pins)
 checks['exactFiveIds']=json.loads((packet/'manifest.json').read_bytes())['test_ids']==["test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_carry_rehashes_all_evidence_files_without_freshness_contract","test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_migration_backup_history_idempotence_tamper_cycle_and_readonly","test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_record_direct_attempt_rejects_task_that_is_not_ready","test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revalidate_success_rejects_ordinary_pending_task","test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_manifest_rejects_false_carry_and_incomplete_lineage"]
 checks['runnerExternalPin']=seal['runner.py']=={'bytes':29404,'sha256':'f7b4206ac2b96dcfc0d0f59560bcf83c5f0757684e1b8d17355518516d906343'}
 checks['manifestExternalPin']=seal['manifest.json']=={'bytes':12335,'sha256':'e7fe12b65611cd1dd72c7d670234962d20da6b1ea19baf952d52d3016d14d5c1'}
 b=exe.read_bytes();checks['executablePin']=len(b)==30894944 and sha(b)=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7' and exe.resolve()==exe
 raw=(root/'evidence/linux-pre.stdout.json').read_bytes();checks['preExternalPin']=len(raw)==114902 and sha(raw)=='3e303efd982ac83f7ef9ac68efb75bd7f3d010ce2f69fc1084ea64057ac2a9a6'
 assert checks['preExternalPin'];pre=json.loads(raw)
 for row in pre['selectedStdlibAndNativePins']:
  if row['file'] and row['bytes'] is not None:
   p=pathlib.Path(row['file']);b=p.read_bytes();modulepins.append({'name':row['name'],'path':str(p),'bytes':len(b),'sha256':sha(b),'match':len(b)==row['bytes'] and sha(b)==row['sha256']})
 checks['eightStdlibFilePins']=len(modulepins)==8 and {x['name'] for x in modulepins}=={'sqlite3','sqlite3.dbapi2','ssl','unittest','unittest.mock','subprocess','contextlib','inspect'} and all(x['match'] for x in modulepins)
 checks['dummyResultEvidencePin']=sha((root/'/workspace/vm-c5-c1-pre-24cf-r03-01/fixture-execution.json').read_bytes())=='0120fb7f7145e1c0b9b51845ef2bc609cefa95963217cfbdbb980ec43490fa85'
 checks['dummyExecutionEvidencePin']=sha((root/'/workspace/vm-c5-c1-pre-24cf-r03-01/fixture-child.json').read_bytes())=='f97aa0543f2eea551c16f5f82837ec0c6c7f5111fb0d0cbeda102cef13e653e4'
 checks['metadataChecks']=all(v is True for v in pre['checks'].values())
 fixture=root/'c1-fixture';fb=(fixture/'SOURCE.SHA256.json').read_bytes()
 checks['fixtureSealExternalPin']=len(fb)==769 and sha(fb)=='2345d41939c978a165693f4417db6944fe983fad42706466f6f986bf617d739e'
 assert checks['fixtureSealExternalPin'];fs=json.loads(fb);factual=[p for p in fixture.rglob('*') if p.is_file()]
 checks['exactFixture7']=len(factual)==7 and {p.relative_to(fixture).as_posix() for p in factual}==set(fs)|{'SOURCE.SHA256.json'}
 checks['fixtureCanonical']=fixture.resolve()==fixture and all(not p.is_symlink() for p in [fixture,*fixture.parents])
 checks['fixtureSixPins']=all(not (fixture/n).is_symlink() and len((fixture/n).read_bytes())==v['bytes'] and sha((fixture/n).read_bytes())==v['sha256'] for n,v in fs.items())
 checks['fixtureExternalPins']=fs['fixture.py']=={'bytes':20601,'sha256':'a3425095abc2dd6fc444cff848053396424202aa019636fc87685889e3ccb208'} and fs['manifest.json']=={'bytes':12565,'sha256':'15e10b1038f2ced4787e73cca5a19039132cf54b432191e3347aa394e3742b4a'} and fs['frozen/runner.py']==seal['runner.py']
 report=json.loads(pathlib.Path('/workspace/vm-c5-c1-pre-24cf-r03-01/fixture-execution.json').read_bytes());child=json.loads(pathlib.Path('/workspace/vm-c5-c1-pre-24cf-r03-01/fixture-child.json').read_bytes())
 expected=['normal_zero','caught_denial','uncaught_denial','init_io','write_io','flush_io','close_io','journal_missing','journal_partial','journal_corrupt','count_mismatch','pid_mismatch','post_counter_mismatch','bool_counter']
 checks['dummyExact14Expected']=len(report['cases'])==14 and [c['name'] for c in report['cases']]==expected and [c['dummy_acceptance'] for c in report['cases']]==['PASS','FAIL','FAIL']+['UNKNOWN']*11 and all(c['checks_match'] is True and c['check_errors']==[] and c['product_PASS'] is False for c in report['cases'])
 checks['dummyNativeTerminal']=report['scope']==child['scope']=='non_product_dummy' and report['fixture_status']=='MATCH' and type(report['actual_native_exit']) is int and report['actual_native_exit']==0 and report['terminal_observed'] is True and report['timed_out'] is False and report['owned_kill_error'] is None and type(report['pid']) is int and report['pid']==child['pid']==5265
 expectedSource={**fs,'SOURCE.SHA256.json':{'bytes':769,'sha256':'2345d41939c978a165693f4417db6944fe983fad42706466f6f986bf617d739e'}}
 checks['dummyFixedSource']=report['source_before']==report['source_after']==child['source_before']==child['source_after']==expectedSource and child['source_unchanged'] is True
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
argv=[str(exe),'-I','-S','-B','-X','utf8',str(packet/'runner.py'),'--run-root',str(run),'--runner-sha256','f7b4206ac2b96dcfc0d0f59560bcf83c5f0757684e1b8d17355518516d906343','--manifest-sha256','e7fe12b65611cd1dd72c7d670234962d20da6b1ea19baf952d52d3016d14d5c1','--timeout','300']
out={'scope':'UNISSUED_C5_LAUNCH_TEMPLATE_FRESHNESS_ONLY','peerId':'90b87660-0705-4bc2-bdbb-aeb4c9f4e5d4','issuedUtc':'2026-10-04T20:48:01Z','launchNotAfterUtc':'2026-10-04T21:03:01Z','guardStartUtc':started.isoformat(),'guardEndUtc':ended.isoformat(),'guardArgv':[sys.executable,'-I','-S','-B','-X','utf8','-'],'guardCwd':str(pathlib.Path.cwd()),'checks':checks,'source11':pins,'stdlib8':modulepins,'errors':errors,'passed':passed,'status':'FRESHNESS_PASS_PENDING_NATIVE_DISPATCH' if passed else 'NOT_RUN','runnerInvocationsSoFar':0,'externalJobs':'UNKNOWN','currentUnreadableProcesses':'UNKNOWN','historicalUnreadableProcesses':'7 (prior phase; not current)','nativeCommandId':None}

print(json.dumps(out,sort_keys=True),flush=True)
if not passed:raise SystemExit(2)
dispatch=datetime.datetime.now(datetime.timezone.utc)
if dispatch>deadline:
 print(json.dumps({'scope':'C5_FINAL_DEADLINE_GATE','utc':dispatch.isoformat(),'status':'NOT_RUN_DEADLINE_EXPIRED','runnerInvocations':0}),flush=True)
 raise SystemExit(124)
print(json.dumps({'scope':'C5_OS_EXECV_DISPATCH','utc':dispatch.isoformat(),'argv':argv,'cwd':str(cwd),'method':'os.execv same process replacement; no additional child','runnerInvocationRequested':1,'retries':0}),flush=True)
try:os.execv(str(exe),argv)
except OSError as error:
 print(json.dumps({'scope':'C5_EXECV_FAILURE','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'UNKNOWN_EXEC_FAILED','errorType':type(error).__name__,'error':str(error),'retry':0}),flush=True)
 raise

PY

