/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B - <<'PY'
import pathlib,base64,hashlib,zipfile,io,json,stat,datetime,ast,os
root=pathlib.Path('/workspace/vm-u2-source-24cf-r01');sha=lambda b:hashlib.sha256(b).hexdigest()
b=(root/'carrier.base64.txt').read_bytes()
assert len(b)==164041 and sha(b)=='f44359ff00b782bf262675ee7140a1a35632a6cd0f1dc96d717ee0cdbbbbd554'
z=base64.b64decode(b[:-1],validate=True)
assert len(z)==123028 and sha(z)=='77702c59561cface04e08b23bcbb07282cf636c1d80296a1fcf81fe31fe5cff6'
archive=zipfile.ZipFile(io.BytesIO(z));items=archive.infolist()
names=[i.filename for i in items];sealname='SOURCE.SHA256.json'
expected={'runner.py','manifest.json','README.ko.md','SOURCE.DIFF.patch','STATIC-CHECKS.json',sealname,'pristine/tests/test_flowmarshal_implementation_workflow.py','pristine/scripts/flowmarshal_implementation_workflow.py','pristine/scripts/flowmarshal_orchestrator_ledger.py'}
assert len(items)==9 and len(set(names))==9 and set(names)==expected and sum(i.file_size for i in items)==642097
for i in items:
 p=pathlib.PurePosixPath(i.filename)
 assert not i.is_dir() and not p.is_absolute() and '..' not in p.parts and '\\' not in i.filename and not stat.S_ISLNK(i.external_attr>>16) and not i.flag_bits&1
sealbytes=archive.read(sealname)
assert len(sealbytes)==1146 and sha(sealbytes)=='7b8e95952e3e1f9b85d9a0e90dd75ad3cc2bf4272640a5d86b7612b82c9d4463'
seal=json.loads(sealbytes)
assert set(seal)==expected-{sealname}
data={i.filename:archive.read(i) for i in items}
for name,pin in seal.items():assert len(data[name])==pin['bytes'] and sha(data[name])==pin['sha256']
assert seal['runner.py']=={'bytes':15932,'sha256':'d00cf8b8d901915d9f7141bbbaae4e674535e9756dd4b0598d95063f79250af3'}
assert seal['manifest.json']=={'bytes':4525,'sha256':'07add40aeaea69d60bc88c64f477818a780a039e92e85b7bdcbe435fbea75162'}
checks=[]
assert b.endswith(b'\n') and b.count(b'\n')==1 and b'\r' not in b and not b.startswith(bytes([239,187,191]))
originalPins={
 'pristine/tests/test_flowmarshal_implementation_workflow.py':{'bytes':173219,'sha256':'0053b2f64047d819fcb8da1fc40233ffe4bc635e2a64e4d4ceb9a28842f0f3c5'},
 'pristine/scripts/flowmarshal_implementation_workflow.py':{'bytes':384699,'sha256':'70f5c5f0da8885eda96becc07e5fcdb9b23a1d3a0ce02aad86fa6c684e432dea'},
 'pristine/scripts/flowmarshal_orchestrator_ledger.py':{'bytes':39264,'sha256':'8f32c23dd5c8bd8105be4d8338243fb67b59825397545a6681abc6c921a4bd72'}
}
for name,pin in originalPins.items():assert seal[name]==pin
with (root/'source.zip').open('xb') as f:f.write(z)
os.chmod(root/'source.zip',0o600)
packet=root/'packet';packet.mkdir(mode=0o700)
for name,raw in data.items():
 p=packet/name;p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 with p.open('xb') as f:assert f.write(raw)==len(raw)
 os.chmod(p,0o600)
readback=[]
for name in sorted(expected):
 p=packet/name;raw=p.read_bytes()
 assert raw==data[name]
 readback.append({'path':name,'bytes':len(raw),'sha256':sha(raw),'mode':oct(stat.S_IMODE(p.stat().st_mode))})
actualfiles=[p.relative_to(packet).as_posix() for p in packet.rglob('*') if p.is_file()]
assert set(actualfiles)==expected and len(actualfiles)==9
out={'scope':'SOURCE_MATERIALIZATION_ONLY_NO_PRODUCT_EXECUTION','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'packetRoot':str(packet),'carrierBytes':len(b),'carrierSha256':sha(b),'zipBytes':len(z),'zipSha256':sha(z),'sourceFiles':9,'sourceBytes':sum(len(raw) for raw in data.values()),'externallyPinnedSeal':{'path':sealname,'bytes':len(sealbytes),'sha256':sha(sealbytes)},'inventory':readback,'allEightSealPinsVerified':True,'zipPolicyChecks':True,'astCompileChecks':checks,'runrootExists':os.path.lexists('/workspace/vm-u2-run-24cf-r01'),'runnerProductImportLoaderTestsClaims':0,'sourceDecision':'U2 SOURCE_ONLY actual NOT_RUN; independent SOURCE verdict pending','sourceProvenance':'Pinned original M0053/helper70f5/legacy8f32; helper is 10-03 candidate, not current10a'}
with (root/'evidence/source-materialization.json').open('x',encoding='utf-8',newline='\n') as f:json.dump(out,f,indent=2);f.write('\n')
print(json.dumps(out,indent=2))
print('PACKET_MANIFEST_BEGIN')
print((packet/'manifest.json').read_text())
print('PACKET_README_BEGIN')
print((packet/'README.ko.md').read_text())

PY
