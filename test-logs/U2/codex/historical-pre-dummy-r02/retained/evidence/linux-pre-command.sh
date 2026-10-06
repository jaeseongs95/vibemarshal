umask 077
set -C
/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - > /workspace/vm-u2-source-24cf-r02/evidence/linux-pre.stdout.json 2> /workspace/vm-u2-source-24cf-r02/evidence/linux-pre.stderr.txt <<'PY'
import sys
startup=[{'name':n,'origin':getattr(getattr(m,'__spec__',None),'origin',None),'file':getattr(m,'__file__',None)} for n,m in sorted(sys.modules.items())]
startup_path=list(sys.path)
import pathlib,hashlib,json,datetime,os,stat,sqlite3,ssl,unittest.mock,subprocess,signal,resource,contextlib,inspect
root=pathlib.Path('/workspace/vm-u2-source-24cf-r02');exe=pathlib.Path(sys.executable);b=exe.read_bytes()
def pin(name):
 m=sys.modules.get(name);spec=getattr(m,'__spec__',None);p=getattr(m,'__file__',None)
 row={'name':name,'loaded':m is not None,'origin':getattr(spec,'origin',None),'file':p,'builtin':getattr(spec,'origin',None)=='built-in'}
 if p and pathlib.Path(p).is_file():
  data=pathlib.Path(p).read_bytes();row.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
 else:row.update(bytes=None,sha256=None)
 return row
stdlibbase=pathlib.Path(sys.base_prefix)/'lib/python3.12'
def is_external(row):
 p=row.get('file') or row.get('origin')
 if p in (None,'built-in','frozen','<stdin>'):return False
 return 'site-packages' in p or 'dist-packages' in p or not pathlib.Path(p).resolve().is_relative_to(stdlibbase.resolve())
loaded=[{'name':n,'origin':getattr(getattr(m,'__spec__',None),'origin',None),'file':getattr(m,'__file__',None)} for n,m in sorted(sys.modules.items())]
cwd=pathlib.Path.cwd()
assert str(cwd)==str(root/'pre-cwd') and not list(cwd.iterdir())
assert sys.version_info[:3]==(3,12,14) and sys.platform=='linux'
assert sys.flags.isolated==1 and sys.flags.no_site==1 and sys.flags.dont_write_bytecode==1 and sys.flags.utf8_mode==1
out={'scope':'SELECTED_STDLIB_U2_PRE_METADATA_ONLY','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'actualArgv':[str(exe),'-I','-S','-B','-X','utf8','-'],'cwd':str(cwd),'preCwdEmpty':True,'python':{'executable':str(exe),'resolved':str(exe.resolve()),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'version':sys.version,'platform':sys.platform,'osName':os.name,'prefix':sys.prefix,'basePrefix':sys.base_prefix,'flags':repr(sys.flags),'xoptions':sys._xoptions},'startupSysPath':startup_path,'startupModuleCount':len(startup),'startupModules':startup,'startupNonStdlibOrigins':[r for r in startup if is_external(r)],'startupSiteImported':any(r['name']=='site' for r in startup),'siteOrPkgPathPresent':any('site-packages' in p or 'dist-packages' in p for p in startup_path),'selectedStdlibAndNativePins':[pin(n) for n in ['sqlite3','sqlite3.dbapi2','_sqlite3','ssl','_ssl','_socket','unittest','unittest.mock','subprocess','_posixsubprocess','select','resource','_hashlib','contextlib','inspect']],'sqliteVersion':sqlite3.sqlite_version,'opensslVersion':ssl.OPENSSL_VERSION,'postProbeNonStdlibOrigins':[r for r in loaded if is_external(r)],'selectedSyscallSymbols':{n:callable(getattr(os,n,None)) for n in ['fork','posix_spawn','waitpid','kill','pipe','open','mkdir','read','write']},'subprocessPopenSymbol':callable(subprocess.Popen),'selectedSyscallsActuallyExercised':'Private root mkdir/CreateNew/write/read/stat already observed; fork/Popen/wait/kill/socket NOT exercised','rootMode':oct(stat.S_IMODE(root.stat().st_mode)),'rootOsWritable':os.access(root,os.W_OK),'runrootExists':os.path.lexists('/workspace/vm-u2-run-24cf-r02'),'runrootCreated':False,'permissionProfile':'managed workspace-write; writable /workspace and /tmp; network restricted; this task writes only accepted source root','containmentLimit':'Workspace write permission is broader than this task root; runner audit hooks are not OS syscall containment. Future child lifecycle/SQLite target closure unobserved','providerActorNativeHostRecord':None,'productRunnerImportsLoadersTestsSqliteConnectsClaims':0,'pydanticFallback':False,'originalQ76StartupEquivalent':False,'independentSourcePreGo':'PENDING; this is preparation evidence only'}

out['metadataProbeMainFrame']={'name':'__main__','origin':None,'file':'<stdin>'}
out['comparisonWithPinnedC3Executable']={'expectedBytes':30894944,'expectedSha256':'fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7','actualBytes':len(b),'actualSha256':hashlib.sha256(b).hexdigest(),'matches':len(b)==30894944 and hashlib.sha256(b).hexdigest()=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7'}
out['sqliteSurfaceObservation']={'sqliteVersion':sqlite3.sqlite_version,'sqliteVersionInfo':list(sqlite3.sqlite_version_info),'threadsafety':sqlite3.threadsafety,'connectionBackupCallable':callable(getattr(sqlite3.Connection,'backup',None)),'connectionCreateFunctionCallable':callable(getattr(sqlite3.Connection,'create_function',None)),'connectionClassModule':sqlite3.Connection.__module__,'backupDescriptorType':type(getattr(sqlite3.Connection,'backup',None)).__name__,'createFunctionDescriptorType':type(getattr(sqlite3.Connection,'create_function',None)).__name__,'sqliteConnectCalls':0,'sqlFixturesDummyQueries':0,'compileOptions':'UNKNOWN_NOT_QUERIED; SQLite PRAGMA compile_options requires a connection forbidden in this preparation scope','compileApiSurface':'backup/create_function descriptors exposed by the supplied built-in _sqlite3; implementation behavior untested','backupUriUdfTriggerPass':False,'uriUdfTriggerBehavior':'UNKNOWN_NOT_EXECUTED'}
out['loadedModuleFilePins']=[pin(n) for n,m in sorted(sys.modules.items()) if getattr(m,'__file__',None) and getattr(m,'__file__',None)!='<stdin>']
out['scopeLimit']='U2 source/PRE preparation only; C3/R021 not rerun; fixed helper70f5 exact2 runner NOT_RUN'
assert out['comparisonWithPinnedC3Executable']['matches']
out['scope']='SELECTED_STDLIB_U2_R02_PRE_METADATA_ONLY'
out['scopeLimit']='U2 r02 staging/PRE only; product runner/import/loader/tests/fixture0; permitted stdlib dummy is a separately recorded single call'
out['loadedModuleOriginsAfterMetadata']=[{'name':n,'origin':getattr(getattr(m,'__spec__',None),'origin',None),'file':getattr(m,'__file__',None)} for n,m in sorted(sys.modules.items())]
out['sqliteBehaviorActual']='UNKNOWN_NOT_RUN'
out['externalJobs']='UNKNOWN'
out['priorUnreadableProcesses']=7
out['OSContainment']='UNKNOWN'
out['providerNativeIds']=None
print(json.dumps(out,indent=2))

PY