/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B - <<'PY'
import pathlib,json,hashlib,datetime,os
source=pathlib.Path('/workspace/vm-common3-source-24cf-r03');run=pathlib.Path('/workspace/vm-common3-run-24cf-r03');e=source/'evidence';stdlib=pathlib.Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/lib/python3.12');packet=source/'packet';sha=lambda b:hashlib.sha256(b).hexdigest()
pre=json.loads((run/'preflight.json').read_bytes());post=json.loads((run/'postflight.json').read_bytes());inv=json.loads((run/'invocation.json').read_bytes());execution=json.loads((run/'execution.json').read_bytes());result=json.loads((run/'result.json').read_bytes());loader=json.loads((run/'loader.json').read_bytes());pinnedPre=json.loads((e/'linux-pre.stdout.json').read_bytes())
guard6={x['file']:x for x in pinnedPre['selectedStdlibAndNativePins'] if x['file'] and x['bytes'] is not None}
rows=[];errors=[]
for stage,key,obj in [('preflight','module_files',pre),('postflight','module_files_after',post)]:
 values=obj.get(key)
 if not isinstance(values,dict):errors.append({'stage':stage,'status':'UNKNOWN_MODULE_FILE_MAPPING_MISSING'});continue
 for name,path in sorted(values.items()):
  row={'stage':stage,'module':name,'path':path,'bytes':None,'sha256':None}
  if path is None:row['status']='NULL_NO_FILE'
  else:
   p=pathlib.Path(path);resolved=p.resolve(strict=False);row['resolved']=str(resolved)
   if not (resolved.is_relative_to(stdlib) or resolved.is_relative_to(packet)):row['status']='UNKNOWN_OUTSIDE_SELECTED_ROOTS_NOT_READ'
   else:
    try:
     b=p.read_bytes();row.update(bytes=len(b),sha256=sha(b),status='FILE_PIN_OBSERVED')
     if path in guard6:pin=guard6[path];row['matchesPinnedPreStdlib']=len(b)==pin['bytes'] and sha(b)==pin['sha256']
    except OSError as error:row.update(status='UNKNOWN_FILE_UNREADABLE',errorType=type(error).__name__,error=str(error))
  rows.append(row)
sourcepins=[]
for name,before in inv['inputs_before'].items():
 p=packet/name;b=p.read_bytes();sourcepins.append({'path':name,'bytes':len(b),'sha256':sha(b),'before':before,'executionAfter':execution['inputs_after_observation'].get(name),'postflightAfter':post['inputs_after'].get(name),'allEqual':sha(b)==before==execution['inputs_after_observation'].get(name)==post['inputs_after'].get(name)})
out={'scope':'POST_FILE_IO_PINS_ONLY_NOT_TEST_OR_ORACLE','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'moduleMappings':{'preflightCount':len(pre.get('module_files',{})),'postflightCount':len(post.get('module_files_after',{}))},'filePins':rows,'mappingErrors':errors,'nativeBuiltinsFromPinnedPre':[x for x in pinnedPre['selectedStdlibAndNativePins'] if x['builtin']],'nativeBuiltinPostFileObservation':'module_files/after has no file for native builtins; null remains null. No module imports added to fill provenance. Frozen accompanying-file hashes do not attest frozen code bytes.','source5BeforeAfterReadback':sourcepins,'source5Unchanged':all(x['allEqual'] for x in sourcepins),'postflightMatchesBeforeRaw':post['matches_before'],'loaderIdsMatchManifest':loader['ids']==json.loads((packet/'manifest.json').read_bytes())['test_ids'],'loaderCount':loader['count'],'loaderErrors':loader['errors'],'resultIdsMatchLoader':result['test_ids']==loader['ids'],'testsRun':result['testsRun'],'allResultErrorArrays':{k:result[k] for k in ['failures','errors','skipped','expectedFailures','unexpectedSuccesses']},'executionStatusRaw':execution['status'],'executionExitRaw':execution['actual_exit'],'sourceScope':'selected helper70f5 C3 exact3 only; no wider qualification','priorUnreadableProcesses':7,'externalJobs':'UNKNOWN','nativeToolId':None,'productRerun':0}
p=e/'module-file-pins-post-r02.json'
with p.open('x',encoding='utf-8',newline='\n') as f:json.dump(out,f,indent=2);f.write('\n')
os.chmod(p,0o600);b=p.read_bytes()
print(json.dumps({'utc':out['utc'],'artifact':{'path':str(p),'bytes':len(b),'sha256':sha(b)},'moduleMappings':out['moduleMappings'],'pinRows':len(rows),'unknownRows':[x for x in rows if x['status']!='FILE_PIN_OBSERVED'],'pinnedPreStdlibMismatches':[x for x in rows if x.get('matchesPinnedPreStdlib') is False],'source5BeforeAfterReadback':sourcepins,'source5Unchanged':out['source5Unchanged'],'postflightMatchesBeforeRaw':post['matches_before'],'loaderIdsMatchManifest':out['loaderIdsMatchManifest'],'loaderCount':loader['count'],'loaderErrors':loader['errors'],'testsRun':result['testsRun'],'allResultErrorArrays':out['allResultErrorArrays'],'executionStatusRaw':execution['status'],'executionExitRaw':execution['actual_exit'],'rawOrigins':pre.get('origins')},indent=2))

PY
