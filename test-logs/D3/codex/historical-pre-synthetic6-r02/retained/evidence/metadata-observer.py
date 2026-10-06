import sys
startup=[{'name':n,'file':getattr(m,'__file__',None),'origin':getattr(getattr(m,'__spec__',None),'origin',None)} for n,m in sorted(sys.modules.items())]
import os,json,hashlib,pathlib,datetime,sysconfig,types,typing
import contextlib,inspect,sqlite3,sqlite3.dbapi2,ssl,unittest,unittest.mock,subprocess
root=pathlib.Path('/workspace/vm-d3-source-24cf-r02');stdlib=pathlib.Path(sysconfig.get_path('stdlib'));observer=pathlib.Path(__file__)
sha=lambda b:hashlib.sha256(b).hexdigest()
def filepin(path):
 if path is None:return {'path':None,'bytes':None,'sha256':None,'state':'NO_FILE'}
 p=pathlib.Path(path)
 if not p.is_file():return {'path':str(p),'bytes':None,'sha256':None,'state':'MISSING'}
 b=p.read_bytes();return {'path':str(p),'resolve':str(p.resolve()),'bytes':len(b),'sha256':sha(b),'state':'FILE_READ','regular':p.is_file(),'symlink':p.is_symlink()}
def module(name,m):
 file=getattr(m,'__file__',None);origin=getattr(getattr(m,'__spec__',None),'origin',None)
 cls='builtin' if origin=='built-in' else 'frozen' if origin=='frozen' else 'stdlib' if file and pathlib.Path(file).is_relative_to(stdlib) and 'site-packages' not in pathlib.Path(file).parts else 'UNKNOWN_OR_NONSTDLIB'
 r={'name':name,'file':file,'origin':origin,'rawClassification':cls}
 r['filePin']=filepin(file) if file and cls in ['stdlib','frozen'] else {'path':file,'bytes':None,'sha256':None,'state':'UNOWNED_NOT_READ' if file else 'NO_FILE'}
 return r
loaded=[module(n,m) for n,m in sorted(sys.modules.items())]
unknown=[r for r in loaded if r['rawClassification']=='UNKNOWN_OR_NONSTDLIB']
selected=[module(n,sys.modules[n]) for n in ['contextlib','inspect','sqlite3','sqlite3.dbapi2','ssl','unittest','unittest.mock','subprocess']]
extensions=[module(n,sys.modules[n]) if n in sys.modules else {'name':n,'origin':None,'file':None,'state':'NOT_LOADED','filePin':{'path':None,'bytes':None,'sha256':None,'state':'NOT_LOADED'}} for n in ['_sqlite3','_ssl','_socket','_posixsubprocess','select','resource','_hashlib']]
objects=[('contextlib.contextmanager',contextlib.contextmanager),('inspect.unwrap',inspect.unwrap),('sqlite3.connect',sqlite3.connect),('sqlite3.dbapi2.connect',sqlite3.dbapi2.connect),('ssl.create_default_context',ssl.create_default_context),('unittest.TestLoader.loadTestsFromNames',unittest.TestLoader.loadTestsFromNames),('unittest.mock.patch',unittest.mock.patch),('subprocess.Popen.__init__',subprocess.Popen.__init__)]
codeRows=[]
for name,obj in objects:
 code=getattr(obj,'__code__',None)
 codeRows.append({'name':name,'objectType':type(obj).__name__,'co_filename':getattr(code,'co_filename',None),'co_name':getattr(code,'co_name',None),'filePin':filepin(code.co_filename) if code else {'path':None,'bytes':None,'sha256':None,'state':'BUILTIN_OR_NO_CODE'}})
aliasRows=[]
for name in ['typing.io','typing.re']:
 alias=sys.modules.get(name);attr=typing.__dict__.get(name.split('.')[1])
 aliasRows.append({'name':name,'present':name in sys.modules,'sameObjectAsTypingAttribute':alias is attr,'sysModulesObjectId':id(alias),'typingAttributeObjectId':id(attr),'objectType':type(alias).__name__,'objectTypeModule':type(alias).__module__,'declaredClassModule':getattr(alias,'__module__',None),'objectName':getattr(alias,'__name__',None),'isModuleType':isinstance(alias,types.ModuleType),'rawOrigin':getattr(getattr(alias,'__spec__',None),'origin',None),'rawFile':getattr(alias,'__file__',None)})
main=sys.modules['__main__']
extra={'observerSourcePin':filepin(str(observer)),'mainObservation':{'sameObjectAsCurrentGlobalsModule':main.__dict__ is globals(),'rawFile':getattr(main,'__file__',None),'resolvedFileEqualsObserver':pathlib.Path(main.__file__).resolve()==observer.resolve(),'objectType':type(main).__name__,'objectTypeModule':type(main).__module__,'rawOrigin':getattr(getattr(main,'__spec__',None),'origin',None),'actualOrigArgv':sys.orig_argv,'cwd':str(pathlib.Path.cwd())},'typingParent':{'moduleObservation':module('typing',typing),'actualFilePin':filepin(typing.__file__),'withinActualStdlibRoot':pathlib.Path(typing.__file__).resolve().is_relative_to(stdlib.resolve())},'aliasRows':aliasRows,'assessmentScope':'Additional object identity and ownership observations, separate from raw origin-null/UNKNOWN classifier. No arbitrary nonstdlib admission or PRE verdict.'}
sitePaths=list(dict.fromkeys([sysconfig.get_path('purelib'),sysconfig.get_path('platlib')]))
hooks=[]
for base in sitePaths:
 p=pathlib.Path(base)
 if p.is_dir():
  for f in sorted(p.glob('*.pth')):hooks.append({'kind':'.pth','executed':False,'basis':'actual -S profile; only bounded file metadata read',**filepin(str(f))})
for base in list(dict.fromkeys(sitePaths+[str(stdlib)])):
 for n in ['sitecustomize.py','usercustomize.py']:
  p=pathlib.Path(base)/n;hooks.append({'kind':n,'exists':p.exists(),'loaded':n[:-3] in sys.modules,'pin':filepin(str(p)) if p.exists() else {'path':str(p),'bytes':None,'sha256':None,'state':'ABSENT'}})
before=json.loads((root/'evidence/P-materialization.json').read_bytes())['sourceAfter'];after=[]
for r in before:
 p=pathlib.Path(r['absolutePath']);b=p.read_bytes()
 after.append({'path':r['path'],'absolutePath':str(p),'resolve':str(p.resolve()),'bytes':len(b),'sha256':sha(b),'regular':p.is_file(),'symlink':p.is_symlink(),'matches':len(b)==r['bytes'] and sha(b)==r['sha256']})
future=pathlib.Path('/workspace/vm-d3-run-24cf-r02');resolved=future.resolve(strict=False)
owned=[p for p in pathlib.Path('/workspace').iterdir() if p.name.startswith(('ags-','vm-')) or p.name=='agent-governance-suite']
frow={'path':str(future),'lexists':os.path.lexists(future),'resolve':str(resolved),'ancestors':[{'path':str(a),'symlink':a.is_symlink(),'resolve':str(a.resolve(strict=False))} for a in [*reversed(future.parents),future]],'nonOverlap':all(not resolved.is_relative_to(p.resolve()) and not p.resolve().is_relative_to(resolved) for p in owned),'created':False,'canonicalUpperFMZero':not any(s.startswith('FM-') for s in resolved.parts),'canonicalURIUnambiguous':not any(c in str(resolved) for c in ['%','#','?','\x00'])}
exe=filepin(sys.executable)
checks={'source18Unchanged':len(after)==18 and all(r['matches'] and r['regular'] and not r['symlink'] for r in after),'sourceBytes1081388':sum(r['bytes'] for r in after)==1081388,'actualFixedExePath':sys.executable=='/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12','Linux312':sys.platform=='linux' and sys.version_info[:2]==(3,12),'isolatedNoSiteNoBytecodeUTF8':bool(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and sys.flags.utf8_mode),'futureRunAbsentCanonical':not frow['lexists'] and resolved==future,'futureNoAncestorSymlink':all(not a['symlink'] for a in frow['ancestors']),'futureNonOverlap':frow['nonOverlap'],'preCwdHomeTmpEmpty':all(not any((root/n).iterdir()) for n in ['pre-cwd','pre-home','pre-tmp']),'actualCwd':pathlib.Path.cwd()==root/'pre-cwd','noLoadedNonstdlibObservedRaw':not unknown}
out={'scope':'D3 r02 fresh metadata only, source/product/fixture NOT_RUN in this process','peerId':'74a61344-e7c7-4c33-ad1b-9af03cfb5ba8','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'pid':os.getpid(),'argv':sys.argv,'orig_argv':sys.orig_argv,'cwd':str(pathlib.Path.cwd()),'envWhitelist':{k:os.environ.get(k) for k in ['HOME','TMPDIR','TMP','TEMP','PATH']},'python':{'version':sys.version,'platform':sys.platform,'executable':sys.executable,'flags':repr(sys.flags),'sys_path':sys.path,'actualStdlibRoot':str(stdlib),'exePin':exe,'matchesPriorPin':exe['bytes']==30894944 and exe['sha256']=='fa67443527ed9647f760d807e2a38f26340757123e643c4639cf273ed15d5ea7'},'startupModules':startup,'allLoadedModules':loaded,'rawUnknownOrNonstdlib':unknown,'additionalOriginNullEvidence':extra,'selectedStdlib8':selected,'extensions':extensions,'codeOrigins':codeRows,'startupHooks':hooks,'sqlite':{'version':sqlite3.sqlite_version,'backupDescriptorPresent':hasattr(sqlite3.Connection,'backup'),'createFunctionDescriptorPresent':hasattr(sqlite3.Connection,'create_function'),'actualCapabilities':'UNKNOWN_NOT_RUN','probeCount':0,'connectCount':0,'compileOptions':'UNKNOWN_NOT_QUERIED'},'source18Before':before,'source18After':after,'futureRunRootRO':frow,'checks':checks,'candidateImportExecute':0,'productChildCount':0,'fixtureCallsInThisMetadataProcess':0,'retry':0,'nativeToolCallId':None,'providerExecutionId':None,'externalJobs':'UNKNOWN','OSContainment':'UNKNOWN','r01RawFalseUnknown':'Preserved unchanged; additional evidence here does not relabel r01.'}
print(json.dumps(out,indent=2,ensure_ascii=True))
