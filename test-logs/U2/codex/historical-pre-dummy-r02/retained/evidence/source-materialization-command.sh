/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B -X utf8 - <<'PY'
import pathlib,base64,hashlib,zipfile,io,json,stat,datetime,os
root=pathlib.Path('/workspace/vm-u2-source-24cf-r02')
sha=lambda b:hashlib.sha256(b).hexdigest()
response=json.loads((root/'evidence/native-page-response.json').read_bytes())
text=response['structuredContent']['text'];b=text.encode('ascii')
assert len(b)==177681 and sha(b)=='11f88bc60623e74ad11855e9440411d83f8896527dc12bb956d526fb54bc3cd8'
assert b.endswith(b'\n') and b.count(b'\n')==1 and b'\r' not in b and not b.startswith(bytes([239,187,191]))
z=base64.b64decode(b[:-1],validate=True)
assert len(z)==133260 and sha(z)=='15e9898c20210576204681b2365b0cf488c2c19cd2e45205f13e5ffb98810e68'
archive=zipfile.ZipFile(io.BytesIO(z));items=archive.infolist()
names=[i.filename for i in items];sealname='SOURCE.SHA256.json'
assert len(items)==15 and len(set(names))==15 and len({n.casefold() for n in names})==15 and sum(i.file_size for i in items)==724947
for i in items:
 p=pathlib.PurePosixPath(i.filename)
 assert not i.is_dir() and not p.is_absolute() and all(x not in ('','..','.') for x in i.filename.split('/')) and '\\' not in i.filename and ':' not in i.filename and '\x00' not in i.filename
 assert not stat.S_ISLNK(i.external_attr>>16) and not (i.external_attr&0x400) and not (i.flag_bits&1)
sealbytes=archive.read(sealname)
assert len(sealbytes)==1995 and sha(sealbytes)=='0c1c54b6643cfb514df94cf1160a41eca46c086dce0e042254658801afcc22d2'
seal=json.loads(sealbytes)
assert len(seal)==14 and set(seal)==set(names)-{sealname}
data={i.filename:archive.read(i) for i in items}
for name,pin in seal.items():assert len(data[name])==pin['bytes'] and sha(data[name])==pin['sha256']
externalPins={
 'runner.py':{'bytes':20603,'sha256':'cfc6e0f438f42ba453d503475c54a3dd8959c06ae5bdef83b48fa760166f2cee'},
 'manifest.json':{'bytes':5916,'sha256':'90a8ac8ee16171b735a105ba27e34b3d60e6ed0e520133e15cf16513ab2a11af'},
 'pristine/tests/test_flowmarshal_implementation_workflow.py':{'bytes':173219,'sha256':'0053b2f64047d819fcb8da1fc40233ffe4bc635e2a64e4d4ceb9a28842f0f3c5'},
 'pristine/scripts/flowmarshal_implementation_workflow.py':{'bytes':384699,'sha256':'70f5c5f0da8885eda96becc07e5fcdb9b23a1d3a0ce02aad86fa6c684e432dea'},
 'pristine/scripts/flowmarshal_orchestrator_ledger.py':{'bytes':39264,'sha256':'8f32c23dd5c8bd8105be4d8338243fb67b59825397545a6681abc6c921a4bd72'},
 'regression/check_origin.py':{'bytes':8497,'sha256':'8321f2343c74a829b4e6509f0a0b2942a7808ffd81f84b45592d5fa6d0616098'},
 'regression/dummy_helper.py':{'bytes':300,'sha256':'b42b7cf911d2f08cd5cf023c0b914f7d910411509c5b8d515304bce60ca9133f'}
}
for name,pin in externalPins.items():assert seal[name]==pin
manifest=json.loads(data['manifest.json'])
r01=json.loads(pathlib.Path('/workspace/vm-u2-source-24cf-r01/packet/manifest.json').read_bytes())
assert manifest['test_ids']==r01['test_ids'] and len(manifest['test_ids'])==2
packet=root/'packet';assert not list(packet.iterdir())
assert not os.path.lexists('/workspace/vm-u2-run-24cf-r02')
for name,raw in [('carrier.base64.txt',b),('source.zip',z)]:
 with (root/name).open('xb') as f:f.write(raw)
 os.chmod(root/name,0o600)
for name,raw in data.items():
 p=packet/name
 current=packet
 for part in pathlib.PurePosixPath(name).parts[:-1]:
  current=current/part
  if not current.exists():current.mkdir(mode=0o700)
  assert not current.is_symlink() and stat.S_IMODE(current.stat().st_mode)==0o700
 with p.open('xb') as f:assert f.write(raw)==len(raw)
 os.chmod(p,0o600)
readback=[]
for name in sorted(names):
 p=packet/name;raw=p.read_bytes();assert raw==data[name]
 readback.append({'path':name,'bytes':len(raw),'sha256':sha(raw),'mode':oct(stat.S_IMODE(p.stat().st_mode))})
assert {p.relative_to(packet).as_posix() for p in packet.rglob('*') if p.is_file()}==set(names)
out={'scope':'U2_R02_SOURCE_MATERIALIZATION_ONLY','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'nativeToolId':None,'packetRoot':str(packet),'carrier':{'bytes':len(b),'sha256':sha(b)},'zip':{'bytes':len(z),'sha256':sha(z)},'sourceFiles':15,'sourceBytes':sum(len(x) for x in data.values()),'seal':{'path':sealname,'bytes':len(sealbytes),'sha256':sha(sealbytes)},'all14SealPinsVerified':True,'externalPins':externalPins,'inventory':readback,'safeZipPolicyPassed':True,'zipAttributes':[{'path':i.filename,'createSystem':i.create_system,'externalAttr':i.external_attr,'flagBits':i.flag_bits} for i in items],'original3AndExact2SameAsR01':True,'testIds':manifest['test_ids'],'regressionCapturePreservedUnmodified':True,'futureRunRootCreated':False,'productRunnerImportsLoadersTestsFixturesClaims':0,'sourceProvenance':'Root-pinned native carrier; helper70f5; Windows regression bytes preserved; no qualification claim'}
p=root/'evidence/source-materialization.json'
with p.open('xb') as f:f.write((json.dumps(out,indent=2)+'\n').encode())
os.chmod(p,0o600)
print(json.dumps(out,indent=2))
PY