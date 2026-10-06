import pathlib,json,hashlib,datetime,os
source=pathlib.Path('/workspace/vm-u2-source-24cf-r01')
run=pathlib.Path('/workspace/vm-u2-run-24cf-r01')
e=source/'evidence'/'u2-post-e637-r01'
sha=lambda b:hashlib.sha256(b).hexdigest()
def save(name,data):
 b=(json.dumps(data,indent=2)+'\n').encode()
 with (e/name).open('xb') as f:f.write(b)
 os.chmod(e/name,0o600)
 return {'path':str(e/name),'bytes':len(b),'sha256':sha(b)}
started=datetime.datetime.now(datetime.timezone.utc).isoformat()
native=json.loads((e/'template-native-response.json').read_bytes())
template=native['structuredContent']['text'].encode('ascii')
assert len(template)==5853 and sha(template)=='876e85101ff682c2a5bf3d56436718ad8670b1333fbe07e8087241ca8158b46f'
receipt=json.loads((e/'outer-execution-receipt.json').read_bytes())
command=receipt['actualNativeCommand'].encode('ascii')
assert command.decode()==template.decode().replace('__ISSUED_UTC__','2026-10-04T16:22:56Z').replace('__LAUNCH_NOT_AFTER_ISO__','2026-10-04T16:24:56+00:00').replace('__LAUNCH_NOT_AFTER_UTC__','2026-10-04T16:24:56.000Z').replace('__DISPATCH_PEER_ID__','e63798e7-5076-446b-9a6e-4df59f5793db')
for name,b in [('execution-template.exact.sh',template),('execution-command.exact.sh',command),('native-execution.exact.output.txt',receipt['actualNativeToolResult']['output'].encode())]:
 with (e/name).open('xb') as f:f.write(b)
 os.chmod(e/name,0o600)
objects={};files=[]
for name in ['invocation.json','loader.json','preflight.json','result.json','postflight.json','execution.json','stdout.txt','stderr.txt','child-error.json']:
 p=run/name
 row={'path':name,'exists':p.is_file(),'bytes':None,'sha256':None}
 if p.is_file():
  b=p.read_bytes();row.update(bytes=len(b),sha256=sha(b))
  if name.endswith('.json'):objects[name]=json.loads(b)
 files.append(row)
inv=objects.get('invocation.json',{});ex=objects.get('execution.json',{})
sourcepins=[]
for name,before in inv.get('inputs_before',{}).items():
 b=(source/'packet'/name).read_bytes()
 sourcepins.append({'path':name,'bytes':len(b),'sha256':sha(b),'before':before,'executionAfter':ex.get('inputs_after_observation',{}).get(name),'allEqual':sha(b)==before==ex.get('inputs_after_observation',{}).get(name)})
pre=json.loads((source/'evidence'/'linux-pre.stdout.json').read_bytes())
baselinepins=[]
for pin in pre['selectedStdlibAndNativePins']:
 row={'module':pin.get('module'),'file':pin.get('file'),'builtin':pin.get('builtin'),'bytes':None,'sha256':None,'observationScope':'PRE-selected file readback only; child closure is absent'}
 if pin.get('file') and pin.get('bytes') is not None:
  try:
   b=pathlib.Path(pin['file']).read_bytes()
   row.update(bytes=len(b),sha256=sha(b),matchesPRE=len(b)==pin['bytes'] and sha(b)==pin['sha256'],status='FILE_READBACK')
  except OSError as error:row.update(status='UNKNOWN_FILE_UNREADABLE',errorType=type(error).__name__)
 else:row['status']='NULL_FROM_PRE_NO_CHILD_ATTESTATION'
 baselinepins.append(row)
module_rows=[];mapping_errors=[]
for filename,key in [('preflight.json','module_files'),('postflight.json','module_files_after')]:
 mapping=objects.get(filename,{}).get(key)
 if not isinstance(mapping,dict):
  mapping_errors.append({'file':filename,'field':key,'status':'UNKNOWN_MAPPING_ABSENT'})
  continue
 for name,path in mapping.items():
  row={'module':name,'file':path,'stage':filename,'bytes':None,'sha256':None}
  if path is None:row['status']='NULL_NO_FILE'
  else:
   resolved=pathlib.Path(path).resolve()
   if not (resolved.is_relative_to(source/'packet') or resolved.is_relative_to(pathlib.Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/lib/python3.12'))):row['status']='UNKNOWN_OUTSIDE_ALLOWED_ROOTS_NOT_READ'
   else:
    try:
     b=resolved.read_bytes();row.update(bytes=len(b),sha256=sha(b),status='FILE_READBACK')
    except OSError as error:row.update(status='UNKNOWN_FILE_UNREADABLE',errorType=type(error).__name__)
  module_rows.append(row)
loader=objects.get('loader.json',{})
manifest=json.loads((source/'packet'/'manifest.json').read_bytes())
rawsummary={'scope':'U2_POST_READ_ONLY_NOT_ORACLE','startedUtc':started,'observedUtc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'nativeToolId':None,'rawFiles':files,'actualExecution':ex,'actualInvocation':inv,'loaderRaw':loader,'loaderIdsMatchManifest':loader.get('ids')==manifest['test_ids'],'childErrorRaw':objects.get('child-error.json'),'resultRaw':objects.get('result.json'),'resultAllArrays':{k:objects.get('result.json',{}).get(k) for k in ['failures','errors','skipped','expectedFailures','unexpectedSuccesses']},'testsRun':objects.get('result.json',{}).get('testsRun'),'failureClassification':'Runner origin-admission failure before TextTestRunner.run; product assertion result unobserved','moduleOriginsObserved':objects.get('preflight.json',{}).get('origins'),'source5BeforeAfterReadback':sourcepins,'source5Unchanged':len(sourcepins)==5 and all(x['allEqual'] for x in sourcepins),'moduleFilePinsFromChildMappings':module_rows,'moduleMappingErrors':mapping_errors,'PRESelectedFileReadback':baselinepins,'externalJobs':'UNKNOWN','priorUnreadableProcesses':7,'runnerInvocations':1,'retries':0,'productTestsRerun':0,'originalRawUnmodified':True}
artifact=save('post-observation.json',rawsummary)
for p in e.iterdir():
 if p.is_file():os.chmod(p,0o600)
print(json.dumps({'artifact':artifact,'status':ex.get('status'),'exit':ex.get('actual_exit'),'loaderCount':loader.get('count'),'loaderErrors':loader.get('errors'),'testsRun':rawsummary['testsRun'],'resultArrays':rawsummary['resultAllArrays'],'source5Unchanged':rawsummary['source5Unchanged'],'missingRawFiles':[x['path'] for x in files if not x['exists']],'childError':objects.get('child-error.json'),'moduleMappingErrors':mapping_errors,'stdlibPREReadbackMismatches':[x for x in baselinepins if x.get('matchesPRE') is False]},indent=2))
