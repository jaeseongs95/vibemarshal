/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -I -S -B - <<'PY'
import pathlib,base64,hashlib,zipfile,io,json,stat,datetime,ast,os
root=pathlib.Path('/workspace/vm-common3-source-24cf-r03');sha=lambda b:hashlib.sha256(b).hexdigest()
b=(root/'carrier.base64.txt').read_bytes()
assert len(b)==155777 and sha(b)=='534c71a90a36c81c69eb912f8782520bcd9e2ee8eb774d48f30e307c64fbccd4'
z=base64.b64decode(b[:-1],validate=True)
assert len(z)==116830 and sha(z)=='0763a488d0d780bfc243acc0d104c77f5128e78a8d9f04b422c496fcdfb63a7d'
archive=zipfile.ZipFile(io.BytesIO(z));items=archive.infolist()
names=[i.filename for i in items];sealname='SOURCE.SHA256.json'
expected={'runner.py','manifest.json','README.ko.md','SOURCE.DIFF.patch','STATIC-CHECKS.json',sealname,'pristine/tests/test_flowmarshal_implementation_workflow.py','pristine/scripts/flowmarshal_implementation_workflow.py','pristine/scripts/flowmarshal_orchestrator_ledger.py'}
assert len(items)==9 and len(set(names))==9 and set(names)==expected and sum(i.file_size for i in items)==625583
for i in items:
 p=pathlib.PurePosixPath(i.filename)
 assert not i.is_dir() and not p.is_absolute() and '..' not in p.parts and '\\' not in i.filename and not stat.S_ISLNK(i.external_attr>>16) and not i.flag_bits&1
sealbytes=archive.read(sealname)
assert len(sealbytes)==1146 and sha(sealbytes)=='321ee6055812cdd7b2648bdc27f67eaac6d7fd2bcde309c8ad1d503bf988bc86'
seal=json.loads(sealbytes)
assert set(seal)==expected-{sealname}
data={i.filename:archive.read(i) for i in items}
for name,pin in seal.items():assert len(data[name])==pin['bytes'] and sha(data[name])==pin['sha256']
assert seal['runner.py']=={'bytes':15249,'sha256':'457fd65598bc5cb5a87dd46abb7036bac9768db20a932053fb9670b201862136'}
assert seal['manifest.json']=={'bytes':3381,'sha256':'ae0260a8fd9803cc716a8eab504e34b9bdfd919f39695e8bced5a896c116c3ad'}
checks=[]
for name,raw in data.items():
 if name.endswith('.py'):
  tree=ast.parse(raw,filename=name);compile(tree,name,'exec',dont_inherit=True)
  checks.append({'path':name,'astParse':True,'compileOnly':True,'execImportPerformed':False})
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
out={'scope':'SOURCE_MATERIALIZATION_ONLY_NO_PRODUCT_EXECUTION','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'packetRoot':str(packet),'carrierBytes':len(b),'carrierSha256':sha(b),'zipBytes':len(z),'zipSha256':sha(z),'sourceFiles':9,'sourceBytes':sum(len(raw) for raw in data.values()),'externallyPinnedSeal':{'path':sealname,'bytes':len(sealbytes),'sha256':sha(sealbytes)},'inventory':readback,'allEightSealPinsVerified':True,'zipPolicyChecks':True,'astCompileChecks':checks,'runrootExists':os.path.lexists('/workspace/vm-common3-run-24cf-r03'),'runnerProductImportLoaderTestsClaims':0,'sourceDecision':'Independent SOURCE verdict pending'}
with (root/'evidence/source-materialization.json').open('x',encoding='utf-8',newline='\n') as f:json.dump(out,f,indent=2);f.write('\n')
print(json.dumps(out,indent=2))
print('PACKET_MANIFEST_BEGIN')
print((packet/'manifest.json').read_text())
print('PACKET_README_BEGIN')
print((packet/'README.ko.md').read_text())

PY
