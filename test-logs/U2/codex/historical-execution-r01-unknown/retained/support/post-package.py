import pathlib,json,hashlib,zipfile,base64,os,datetime
source=pathlib.Path('/workspace/vm-u2-source-24cf-r01')
run=pathlib.Path('/workspace/vm-u2-run-24cf-r01')
e=source/'evidence'/'u2-post-e637-r01'
sha=lambda b:hashlib.sha256(b).hexdigest()
entries={}
for name in ['invocation.json','loader.json','preflight.json','result.json','postflight.json','execution.json','stdout.txt','stderr.txt','child-error.json']:
 p=run/name
 if p.is_file():entries['run-raw/'+name]=p.read_bytes()
missing=[name for name in ['preflight.json','result.json','postflight.json'] if not (run/name).is_file()]
for name in ['execution-template.exact.sh','execution-command.exact.sh','template-native-response.json','outer-execution-receipt.json','native-execution.exact.output.txt','launch-freshness.json','execution-dispatch.json','native-terminal.json','post-observation.json','post-collector.py','collection-audit.json','post-package.py']:
 entries['support/'+name]=(e/name).read_bytes()
for name in ['command-audit.json','linux-pre.stdout.json','source-materialization.json','runroot-ro.json']:
 entries['retained-pre/'+name]=(source/'evidence'/name).read_bytes()
inventory={'scope':'U2_SINGLE_EXECUTION_POST_RAW_RETURN','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'dispatchPeerId':'e63798e7-5076-446b-9a6e-4df59f5793db','sourceRoot':str(source),'runRoot':str(run),'statusRaw':'UNKNOWN','actualExitRaw':2,'missingRawFiles':missing,'runnerInvocations':1,'retries':0,'nativeToolId':None,'externalJobs':'UNKNOWN','priorUnreadableProcesses':7,'payloadFiles':[{'path':name,'bytes':len(b),'sha256':sha(b)} for name,b in sorted(entries.items())]}
ib=(json.dumps(inventory,indent=2)+'\n').encode()
with (e/'post-return.inventory.json').open('xb') as f:f.write(ib)
entries['POST.INVENTORY.json']=ib
with (e/'post-return.zip').open('xb') as f:
 with zipfile.ZipFile(f,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
  for name,b in sorted(entries.items()):z.writestr(name,b)
zb=(e/'post-return.zip').read_bytes()
with zipfile.ZipFile(e/'post-return.zip') as z:
 assert len(z.namelist())==len(set(z.namelist()))==len(entries)
 for name,b in entries.items():assert z.read(name)==b
cb=base64.b64encode(zb)+b'\n'
assert len(cb)<=256*1024
with (e/'post-return.carrier.txt').open('xb') as f:f.write(cb)
assert base64.b64decode(cb,validate=False)==zb
out={'scope':inventory['scope'],'utc':inventory['utc'],'zip':{'path':str(e/'post-return.zip'),'bytes':len(zb),'sha256':sha(zb)},'carrier':{'path':str(e/'post-return.carrier.txt'),'bytes':len(cb),'sha256':sha(cb),'format':'ASCII base64 with exactly one terminal LF'},'inventory':{'path':str(e/'post-return.inventory.json'),'bytes':len(ib),'sha256':sha(ib)},'zipEntries':sorted(entries),'missingRawFiles':missing,'status':'UNKNOWN','actualExit':2}
ob=(json.dumps(out,indent=2)+'\n').encode()
with (e/'post-return.transport.json').open('xb') as f:f.write(ob)
for p in e.iterdir():
 if p.is_file():os.chmod(p,0o600)
print(json.dumps(out,indent=2))
