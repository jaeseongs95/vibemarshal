/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,base64,hashlib,zipfile,io,json,stat,datetime,os
root=pathlib.Path('/workspace/vm-c5-source-24cf-r03');sha=lambda b:hashlib.sha256(b).hexdigest()
response=json.loads((root/'evidence/native-page-response.json').read_bytes());b=response['structuredContent']['text'].encode('ascii')
assert len(b)==203517 and sha(b)=='0693bac5e36a7adc9794f69219ef25f31c1fdfee3ce377651ae2cea8ff285bda'
assert b.endswith(b'\n') and b.count(b'\n')==1 and b'\r' not in b
z=base64.b64decode(b[:-1],validate=True)
assert len(z)==152636 and sha(z)=='14a320ac9c1ebb8c6716c591b1d5f3eeed2523d0e4d6b7bcce3f39c810007850'
archive=zipfile.ZipFile(io.BytesIO(z));items=archive.infolist();names=[i.filename for i in items];sealname='SOURCE.SHA256.json'
assert len(items)==11 and len(set(names))==len({n.casefold() for n in names})==11 and sum(i.file_size for i in items)==866064
for i in items:
 p=pathlib.PurePosixPath(i.filename)
 assert not i.is_dir() and not p.is_absolute() and all(x not in ('','..','.') for x in i.filename.split('/')) and '\\' not in i.filename and ':' not in i.filename and '\x00' not in i.filename
 assert not stat.S_ISLNK(i.external_attr>>16) and not (i.external_attr&0x400) and not (i.flag_bits&1)
sealbytes=archive.read(sealname)
assert len(sealbytes)==1425 and sha(sealbytes)=='1e12d8ab6f3a4f9fee880355edf4fd0f1ca6c47d795c07ed800fc073aa40344d'
seal=json.loads(sealbytes);assert len(seal)==10 and set(seal)==set(names)-{sealname}
data={i.filename:archive.read(i) for i in items}
for name,pin in seal.items():assert len(data[name])==pin['bytes'] and sha(data[name])==pin['sha256']
externalPins={
 'runner.py':{'bytes':29404,'sha256':'f7b4206ac2b96dcfc0d0f59560bcf83c5f0757684e1b8d17355518516d906343'},
 'manifest.json':{'bytes':12335,'sha256':'e7fe12b65611cd1dd72c7d670234962d20da6b1ea19baf952d52d3016d14d5c1'},
 'pristine/tests/test_flowmarshal_implementation_workflow.py':{'bytes':173219,'sha256':'0053b2f64047d819fcb8da1fc40233ffe4bc635e2a64e4d4ceb9a28842f0f3c5'},
 'pristine/scripts/flowmarshal_implementation_workflow.py':{'bytes':384699,'sha256':'70f5c5f0da8885eda96becc07e5fcdb9b23a1d3a0ce02aad86fa6c684e432dea'},
 'pristine/scripts/flowmarshal_orchestrator_ledger.py':{'bytes':39264,'sha256':'8f32c23dd5c8bd8105be4d8338243fb67b59825397545a6681abc6c921a4bd72'}
}
for name,pin in externalPins.items():assert seal[name]==pin
manifest=json.loads(data['manifest.json']);assert len(manifest['test_ids'])==len(set(manifest['test_ids']))==5
packet=root/'packet';assert not list(packet.iterdir()) and not os.path.lexists('/workspace/vm-c5-run-24cf-r03')
def save(p,b):
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as f:f.write(b)
save(root/'carrier.base64.txt',b);save(root/'transport.zip',z)
for name,raw in data.items():
 current=packet
 for part in pathlib.PurePosixPath(name).parts[:-1]:
  current=current/part
  if not current.exists():current.mkdir(mode=0o700)
  assert not current.is_symlink() and stat.S_IMODE(current.stat().st_mode)==0o700
 save(packet/name,raw)
inventory=[]
for name in sorted(names):
 p=packet/name;raw=p.read_bytes();assert raw==data[name]
 inventory.append({'path':name,'bytes':len(raw),'sha256':sha(raw),'mode':oct(stat.S_IMODE(p.stat().st_mode))})
assert {p.relative_to(packet).as_posix() for p in packet.rglob('*') if p.is_file()}==set(names)
out={'scope':'C5_R03_SOURCE_MATERIALIZATION_ONLY','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'packetRoot':str(packet),'carrier':{'bytes':len(b),'sha256':sha(b)},'transportZIP':{'bytes':len(z),'sha256':sha(z),'storedOutsidePacket':True},'sourceFiles':11,'sourceBytes':866064,'seal':{'bytes':len(sealbytes),'sha256':sha(sealbytes)},'all10SealPinsVerified':True,'externalPins':externalPins,'inventory':inventory,'safeZIPChecksPassed':True,'zipEntryAttributes':[{'path':i.filename,'createSystem':i.create_system,'externalAttr':i.external_attr,'flagBits':i.flag_bits} for i in items],'testIDsFixedByManifest':manifest['test_ids'],'productImportsLoadersTestsClaims':0,'dummyCalls':0,'futureRunRootCreated':False,'nativeCommandId':None,'providerNativeId':None,'externalJobs':'UNKNOWN','OSContainment':'UNKNOWN','scopeLimit':'Source delivery/PRE metadata only; no C1 fixture supplied or run; no launch authority'}
save(root/'evidence/source-materialization.json',(json.dumps(out,indent=2)+'\n').encode())
print(json.dumps(out,indent=2))
PY