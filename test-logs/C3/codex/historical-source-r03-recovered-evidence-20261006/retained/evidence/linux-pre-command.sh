/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import sys
startup=[{'name':n,'origin':getattr(getattr(m,'__spec__',None),'origin',None),'file':getattr(m,'__file__',None)} for n,m in sorted(sys.modules.items())]
startup_path=list(sys.path)
import pathlib,hashlib,json,datetime,os,stat,sqlite3,ssl,unittest.mock,subprocess,signal,resource
root=pathlib.Path('/workspace/vm-common3-source-24cf-r03');exe=pathlib.Path(sys.executable);b=exe.read_bytes()
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
 if p in (None,'built-in','frozen'):return False
 return 'site-packages' in p or 'dist-packages' in p or not pathlib.Path(p).resolve().is_relative_to(stdlibbase.resolve())
loaded=[{'name':n,'origin':getattr(getattr(m,'__spec__',None),'origin',None),'file':getattr(m,'__file__',None)} for n,m in sorted(sys.modules.items())]
cwd=pathlib.Path.cwd()
assert str(cwd)==str(root/'pre-cwd') and not list(cwd.iterdir())
assert sys.version_info[:2]==(3,12) and sys.platform=='linux'
assert sys.flags.isolated==1 and sys.flags.no_site==1 and sys.flags.dont_write_bytecode==1 and sys.flags.utf8_mode==1
out={'scope':'SELECTED_STDLIB_C3_PRE_METADATA_ONLY','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'actualArgv':[str(exe),'-I','-S','-B','-X','utf8','-'],'cwd':str(cwd),'preCwdEmpty':True,'python':{'executable':str(exe),'resolved':str(exe.resolve()),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'version':sys.version,'platform':sys.platform,'osName':os.name,'prefix':sys.prefix,'basePrefix':sys.base_prefix,'flags':repr(sys.flags),'xoptions':sys._xoptions},'startupSysPath':startup_path,'startupModuleCount':len(startup),'startupModules':startup,'startupNonStdlibOrigins':[r for r in startup if is_external(r)],'startupSiteImported':any(r['name']=='site' for r in startup),'siteOrPkgPathPresent':any('site-packages' in p or 'dist-packages' in p for p in startup_path),'selectedStdlibAndNativePins':[pin(n) for n in ['sqlite3','sqlite3.dbapi2','_sqlite3','ssl','_ssl','_socket','unittest','unittest.mock','subprocess','_posixsubprocess','select','resource','_hashlib']],'sqliteVersion':sqlite3.sqlite_version,'opensslVersion':ssl.OPENSSL_VERSION,'postProbeNonStdlibOrigins':[r for r in loaded if is_external(r)],'selectedSyscallSymbols':{n:callable(getattr(os,n,None)) for n in ['fork','posix_spawn','waitpid','kill','pipe','open','mkdir','read','write']},'subprocessPopenSymbol':callable(subprocess.Popen),'selectedSyscallsActuallyExercised':'Private root mkdir/CreateNew/write/read/stat already observed; fork/Popen/wait/kill/socket NOT exercised','rootMode':oct(stat.S_IMODE(root.stat().st_mode)),'rootOsWritable':os.access(root,os.W_OK),'runrootExists':os.path.lexists('/workspace/vm-common3-run-24cf-r03'),'runrootCreated':False,'permissionProfile':'managed workspace-write; writable /workspace and /tmp; network restricted; this task writes only accepted source root','containmentLimit':'Workspace write permission is broader than this task root; runner audit hooks are not OS syscall containment. Future child lifecycle/SQLite target closure unobserved','providerActorNativeHostRecord':None,'productRunnerImportsLoadersTestsSqliteConnectsClaims':0,'pydanticFallback':False,'originalQ76StartupEquivalent':False,'independentSourcePreGo':'PENDING; this is preparation evidence only'}
print(json.dumps(out,indent=2))

PY
