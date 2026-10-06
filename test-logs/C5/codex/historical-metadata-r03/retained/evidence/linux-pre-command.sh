umask 077
set -C
HOME=/workspace/vm-c5-source-24cf-r03/pre-home TMPDIR=/workspace/vm-c5-source-24cf-r03/pre-tmp TMP=/workspace/vm-c5-source-24cf-r03/pre-tmp TEMP=/workspace/vm-c5-source-24cf-r03/pre-tmp /opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - > /workspace/vm-c5-source-24cf-r03/evidence/linux-pre.stdout.json 2> /workspace/vm-c5-source-24cf-r03/evidence/linux-pre.stderr.txt <<'PY'
import sys
startup=[{'name':n,'origin':getattr(getattr(m,'__spec__',None),'origin',None),'file':getattr(m,'__file__',None)} for n,m in sorted(sys.modules.items())]
startupPath=list(sys.path)
import pathlib,os,json,hashlib,datetime,stat,sqlite3,ssl,unittest.mock,subprocess,resource,contextlib,inspect
root=pathlib.Path('/workspace/vm-c5-source-24cf-r03');packet=root/'packet';e=root/'evidence'
sha=lambda b:hashlib.sha256(b).hexdigest()
started=datetime.datetime.now(datetime.timezone.utc).isoformat()
sealbytes=(packet/'SOURCE.SHA256.json').read_bytes();seal=json.loads(sealbytes)
def sourcePins():
 rows=[]
 for name,pin in seal.items():
  b=(packet/name).read_bytes();rows.append({'path':name,'bytes':len(b),'sha256':sha(b),'matchesSeal':len(b)==pin['bytes'] and sha(b)==pin['sha256']})
 b=(packet/'SOURCE.SHA256.json').read_bytes();rows.append({'path':'SOURCE.SHA256.json','bytes':len(b),'sha256':sha(b),'matchesSeal':len(b)==1425 and sha(b)=='1e12d8ab6f3a4f9fee880355edf4fd0f1ca6c47d795c07ed800fc073aa40344d'})
 return sorted(rows,key=lambda x:x['path'])
before=sourcePins();assert len(before)==11 and all(x['matchesSeal'] for x in before)
exe=pathlib.Path(sys.executable);exeBytes=exe.read_bytes();stdlib=pathlib.Path(sys.base_prefix)/'lib/python3.12'
def pin(n):
 m=sys.modules.get(n);spec=getattr(m,'__spec__',None);p=getattr(m,'__file__',None)
 row={'name':n,'loaded':m is not None,'origin':getattr(spec,'origin',None),'file':p,'builtin':getattr(spec,'origin',None)=='built-in','bytes':None,'sha256':None}
 if p and p!='<stdin>':
  file=pathlib.Path(p)
  if file.is_file():
   b=file.read_bytes();row.update(bytes=len(b),sha256=sha(b))
 return row
def external(row):
 p=row.get('file') or row.get('origin')
 if p in (None,'built-in','frozen','<stdin>'):return False
 return 'site-packages' in p or 'dist-packages' in p or not pathlib.Path(p).resolve().is_relative_to(stdlib.resolve())
selected=['contextlib','inspect','sqlite3','sqlite3.dbapi2','ssl','unittest','unittest.mock','subprocess','_sqlite3','_ssl','_socket','_posixsubprocess','select','resource','_hashlib']
selectedPins=[pin(n) for n in selected]
hookDirs=[stdlib,stdlib/'site-packages',pathlib.Path(sys.base_prefix)/'lib/site-packages']
hooks=[]
for folder in hookDirs:
 record={'directory':str(folder),'exists':folder.is_dir(),'resolve':str(folder.resolve()),'symlink':folder.is_symlink(),'identities':[]}
 if folder.is_dir():
  for p in sorted(folder.glob('*.pth'))+[folder/'sitecustomize.py',folder/'usercustomize.py']:
   if p.is_file():
    b=p.read_bytes();record['identities'].append({'path':str(p),'resolve':str(p.resolve()),'symlink':p.is_symlink(),'bytes':len(b),'sha256':sha(b),'executedByThisProbe':False})
 hooks.append(record)
nodeModules=[]
for p in [root/'node_modules',packet/'node_modules',root/'pre-cwd/node_modules',root/'pre-home/node_modules',pathlib.Path('/workspace/node_modules')]:
 row={'path':str(p),'lexists':os.path.lexists(p),'resolve':str(p.resolve()),'symlink':p.is_symlink()}
 if os.path.lexists(p):
  st=p.lstat();row.update(mode=oct(stat.S_IMODE(st.st_mode)),device=st.st_dev,inode=st.st_ino,directory=p.is_dir())
 nodeModules.append(row)
def components(p):return [{'path':str(x),'lexists':os.path.lexists(x),'symlink':x.is_symlink(),'resolve':str(x.resolve())} for x in [pathlib.Path('/'),*reversed(p.parents[:-1]),p]]
def overlap(a,b):return a.is_relative_to(b) or b.is_relative_to(a)
run=pathlib.Path('/workspace/vm-c5-run-24cf-r03');cwd=pathlib.Path.cwd();owned=list(pathlib.Path('/workspace').iterdir())
future={'path':str(run),'lexists':os.path.lexists(run),'resolve':str(run.resolve()),'ancestors':components(run),'parentWritableSearchable':os.access(run.parent,os.W_OK|os.X_OK),'upperFMComponents':[x for x in run.resolve().parts if x.startswith('FM-')],'URICharactersPresent':[c for c in ['%','#','?','\x00'] if c in str(run.resolve())],'workspaceOverlaps':[str(x) for x in owned if overlap(run.resolve(),x.resolve())],'sourceOverlap':overlap(run.resolve(),root.resolve()),'created':False}
actualFlags={'isolated':sys.flags.isolated,'noSite':sys.flags.no_site,'dontWriteBytecode':sys.flags.dont_write_bytecode,'utf8Mode':sys.flags.utf8_mode,'ignoreEnvironment':sys.flags.ignore_environment,'noUserSite':sys.flags.no_user_site,'safePath':sys.flags.safe_path}
checks={'fixedExePath':str(exe)=='/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12' and str(exe.resolve())==str(exe),'fixedExePin':len(exeBytes)==30894944 and sha(exeBytes)=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7','Linux312':sys.platform=='linux' and sys.version_info[:2]==(3,12),'exactFlags':all(actualFlags.values()) and sys._xoptions.get('utf8') is True,'cwdExactEmpty':str(cwd)==str(root/'pre-cwd') and str(cwd.resolve())==str(cwd) and not list(cwd.iterdir()),'futureAbsentCanonical':not future['lexists'] and future['resolve']==future['path'],'futureNoSymlink':not any(x['symlink'] for x in future['ancestors']),'futureNoFMURI':not future['upperFMComponents'] and not future['URICharactersPresent'],'futureNonOverlap':not future['workspaceOverlaps'] and not future['sourceOverlap'],'source11PinsBefore':all(x['matchesSeal'] for x in before)}
loadedModulePins=[pin(n) for n in sorted(sys.modules)]
loadedModules=[{'name':n,'origin':getattr(getattr(m,'__spec__',None),'origin',None),'file':getattr(m,'__file__',None)} for n,m in sorted(sys.modules.items())]
after=sourcePins();checks['source11BeforeAfterUnchanged']=before==after and all(x['matchesSeal'] for x in after)
nonstdlib=[r for r in loadedModules if external(r)]
checks['noNonStdlibLoaded']=not nonstdlib
out={'scope':'C5_R03_FRESH_LINUX_RUNTIME_METADATA_ONLY','startedUtc':started,'observedUtc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'currentProcessPID':os.getpid(),'parentProcessPID':os.getppid(),'actualArgv':[str(exe),'-I','-S','-B','-X','utf8','-'],'actualSysArgv':sys.argv,'cwd':str(cwd),'cwdResolve':str(cwd.resolve()),'envNonSecretSubset':{k:os.environ.get(k) for k in ['HOME','TMPDIR','TMP','TEMP','PATH']},'environmentCapture':'Only explicit non-secret HOME/TMP/PATH subset; credential variables not read or enumerated. HOME/TMP set for this metadata process only.','python':{'executable':str(exe),'resolve':str(exe.resolve()),'bytes':len(exeBytes),'sha256':sha(exeBytes),'version':sys.version,'platform':sys.platform,'prefix':sys.prefix,'basePrefix':sys.base_prefix,'flags':actualFlags,'flagsRepr':repr(sys.flags),'xoptions':sys._xoptions},'startupSysPath':startupPath,'startupModules':startup,'startupModuleCount':len(startup),'startupNonStdlibOrigins':[r for r in startup if external(r)],'startupSiteHookImports':[r for r in startup if r['name'] in ['site','sitecustomize','usercustomize']],'loadedModuleOrigins':loadedModules,'loadedModuleOriginCount':len(loadedModules),'loadedModulePins':loadedModulePins,'loadedModuleFilePinCount':sum(x['sha256'] is not None for x in loadedModulePins),'nonStdlibLoaded':nonstdlib,'selectedStdlibAndNativePins':selectedPins,'nativeBuiltinFilePins':'No-file builtins remain bytes/SHA null; accompanying files for frozen modules do not attest frozen code bytes','startupHookFilesystemIdentities':hooks,'hookObservationScope':'Known supplied Python stdlib/site-packages only, nonrecursive *.pth/sitecustomize.py/usercustomize.py; no profile/credential/cache search. -S disables site processing; identified files are not executed.','nodeModulesFilesystemIdentities':nodeModules,'nodeModulesObservationScope':'Five explicit current-root/workspace paths only; no node process/module loading or global search. Other paths unobserved.','sqlite':{'version':sqlite3.sqlite_version,'versionInfo':list(sqlite3.sqlite_version_info),'threadsafety':sqlite3.threadsafety,'backupDescriptorPresent':callable(getattr(sqlite3.Connection,'backup',None)),'createFunctionDescriptorPresent':callable(getattr(sqlite3.Connection,'create_function',None)),'SQLiteConnects':0,'SQLProbes':0,'URISemanticsBackupUDFTriggerActual':'UNKNOWN_NOT_RUN','compileOptions':'UNKNOWN_NOT_QUERIED'},'opensslVersion':ssl.OPENSSL_VERSION,'source11PinsBefore':before,'source11PinsAfter':after,'futureRunRootRO':future,'privateMetadataPaths':{n:{'path':str(root/n),'empty':not list((root/n).iterdir()),'mode':oct(stat.S_IMODE((root/n).stat().st_mode))} for n in ['pre-cwd','pre-home','pre-tmp']},'checks':checks,'productRunnerTestHelperLegacyImportsLoadersTests':0,'dummyCalls':0,'installation':0,'networkProbes':0,'providerCalls':0,'futureRunRootCreated':False,'launchIDIssued':False,'nativeCommandId':None,'providerNativeId':None,'externalJobs':'UNKNOWN','unreadableProcessObservation':'UNKNOWN; no current global process enumeration; old unreadable7 evidence retained','priorUnreadableProcesses':7,'OSContainment':'UNKNOWN','productStatus':'NOT_RUN','sourceStaticReportedByRoot':{'R031Bytes':6444,'sha256':'c0bfb7b6026279d83e7f86bec106e95bb13641c5eb8add1b72ea3b6e0937c3e1','independentlyFetchedHere':False},'fixtureStatus':'No new C1 fixture supplied; dummy0','nativePeerCapability':'No peer tool exposed in this worker catalogue; parent notification uses normal delegated turn completion'}
print(json.dumps(out,indent=2))
raise SystemExit(0 if all(checks.values()) else 2)
PY