from pathlib import Path
import ast,copy,difflib,hashlib,json,zipfile
from datetime import datetime,timezone
W=Path('C:/Users/sjs95/Documents/Codex/2026-10-04/task-8/vm-cloud-platform-24cfe63e/writer4')
OLD=W/'after-c5-next-source-r01';OUT=W/'after-c5-next-source-r02'
assert {p.relative_to(OUT).as_posix() for p in OUT.rglob('*') if p.is_file()}=={'BUILD-R02-SOURCE-METADATA.py'}
def pin(b):return {'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
def jb(x):return (json.dumps(x,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
def put(name,b):
 p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('xb') as f:f.write(b)
oldpins={str(p):pin(p.read_bytes()) for p in OLD.rglob('*') if p.is_file()}
assert len(oldpins)==19
runner=(OLD/'runner.py').read_bytes()
assert pin(runner)=={'bytes':31666,'sha256':'0aba0adde14a93ea88dbef8ad6a0130024f49f1058608c6cf0cf7c45124ae3f0'}
before=b'if name not in ("__main__", MODULE) and name.split(".")[0] not in sys.stdlib_module_names:'
after=b'if name not in ("__main__", MODULE, FIXTURE_MODULE) and name.split(".")[0] not in sys.stdlib_module_names:'
assert runner.count(before)==1
updated=runner.replace(before,after,1)
ot=ast.parse(runner);nt=ast.parse(updated)
closure=lambda tree:next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='closure')
def ah(node):return hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest()
normalized=copy.deepcopy(nt)
comp=next(n for n in ast.walk(closure(normalized)) if isinstance(n,ast.Compare) and isinstance(n.left,ast.Name) and n.left.id=='name')
assert [ast.unparse(x) for x in comp.comparators[0].elts]==["'__main__'",'MODULE','FIXTURE_MODULE']
comp.comparators[0].elts.pop()
assert ast.dump(normalized,include_attributes=False)==ast.dump(ot,include_attributes=False)
compile(updated,str(OUT/'runner.py'),'exec')
manifest=json.loads((OLD/'manifest.json').read_bytes())
manifest['runner']=dict(path='runner.py',**pin(updated))
manifest['candidate_revision']='after-c5-next-source-r02'
manifest['source_evidence']['prior_D3_r01']={'path':str(OLD),**pin(runner),'scope':'historical R035 SOURCE only; natural M closure rejection statically found afterwards; not executable acceptance'}
manifest['source_evidence']['closure_fix']={
 'old_line':95,'new_line':95,'old_AST_sha256':ah(closure(ot)),'new_AST_sha256':ah(closure(nt)),
 'exact_AST_equal':False,'change':'add only declared FIXTURE_MODULE to existing exact closure allow tuple',
 'full_runner_inverse_declared_tuple_AST_equal':True,
 'reason':'D imports natural M; checked binding requires M in sys.modules; preflight/postflight closure must admit that same declared M',
 'actual_import_loader_product_run':'NOT_RUN',
 'prior_R035':'historical/static unchanged source only; do not reuse for r02',
 'new_SOURCE_PRE_fixture_required':True}
manifest['source_evidence']['C5_baseline_runner']['scope']='historical reference; current SOURCE.DIFF is r01 to r02'
manifest['issuance']['future_rule']='Root must bind new r02 SOURCE and PRE/denial/closure fixture/runtime/paths into separate final template; actual preparation argv reviewed; only designated4 issued values substituted. SOURCE draft and issuance remain NOT_RUN.'
mb=jb(manifest)
skip={'BUILD-SOURCE-METADATA.py','runner.py','manifest.json','SOURCE.DIFF.patch','README.ko.md','STATIC-CHECKS.json',
 'LAUNCH.UNISSUED.SOURCE-DRAFT.template.txt','SOURCE.SHA256.json','transport.zip'}
for p in OLD.rglob('*'):
 if p.is_file() and p.relative_to(OLD).as_posix() not in skip:put(p.relative_to(OLD).as_posix(),p.read_bytes())
put('runner.py',updated);put('manifest.json',mb)
draft=(OLD/'LAUNCH.UNISSUED.SOURCE-DRAFT.template.txt').read_text('utf-8')
assert draft.count(oldpins[str(OLD/'runner.py')]['sha256'])==1
assert draft.count(oldpins[str(OLD/'manifest.json')]['sha256'])==1
draft=draft.replace(oldpins[str(OLD/'runner.py')]['sha256'],pin(updated)['sha256'])
draft=draft.replace('"bytes": 31666','"bytes": '+str(len(updated)),1)
draft=draft.replace(oldpins[str(OLD/'manifest.json')]['sha256'],pin(mb)['sha256'])
draft=draft.replace('"bytes": '+str(oldpins[str(OLD/'manifest.json')]['bytes']),'"bytes": '+str(len(mb)),1)
draft=draft.replace('/workspace/vm-d3-source-24cf-r01','/workspace/vm-d3-source-24cf-r02')
draft=draft.replace('/workspace/vm-d3-run-24cf-r01','/workspace/vm-d3-run-24cf-r02')
assert 'pre_evidence_pin=None' in draft
for token in manifest['issuance']['exact_token_keys']:assert draft.count(token)==1
compile('\n'.join(draft.splitlines()[1:-1])+'\n','r02-unissued-draft','exec')
put('LAUNCH.UNISSUED.SOURCE-DRAFT.template.txt',draft.encode('utf-8'))
diff=''
for name,b in [('runner.py',updated),('manifest.json',mb),('LAUNCH.UNISSUED.SOURCE-DRAFT.template.txt',draft.encode('utf-8'))]:
 diff+=''.join(difflib.unified_diff((OLD/name).read_text('utf-8').splitlines(keepends=True),
 b.decode('utf-8').splitlines(keepends=True),fromfile='r01/'+name,tofile='r02/'+name))
put('SOURCE.DIFF.patch',diff.encode('utf-8'))
readme=(OLD/'README.ko.md').read_text('utf-8')
prefix='''# D3 r02 — natural M closure SOURCE 수정
r01 closure95가 natural D→M의 M을 거절하는 정적 논리 결함을 확인했다. r02는 exact tuple에 FIXTURE_MODULE만 추가한다. 원 r01 19파일과 R035 판정은 historical로 보존한다. R035 PASS를 이 변경 후보의 수용/PRE/실행 증거로 인용하지 않는다.

runner 실제 수정은 한 줄뿐이다. stdlib 검사를 넓히거나 namespace/alias를 만들지 않는다. 원 pristine4/ID3/helper70f5/origin17/C1 거절 journal/prepare source6b0eec/900초/지정4token은 동일하다. manifest/pin/diff 및 PRE_UNBOUND draft의 core pin과 prospective r02 경로를 함께 갱신했다. schema 문자열은 같은 입력 계약이고 candidate_revision과 SHA가 후보를 구별한다. 새SOURCE·fixture·PRE는 미실행/미수용이다.

SOURCE.DIFF.patch는 r01→r02이다. 기존 아래 설명에서 r03 공통 C1 재사용은 closure를 제외한 C1 처리에 한정한다. 바뀐 closure actual AST와 synthetic natural-name 허용/미선언 거절은 별도 r02 fixture SOURCE 준비로 확인한다. 자연 module object/loader/helper/SQLite의 실제 동작은 아직 NOT_RUN이다.

'''
put('README.ko.md',(prefix+readme).encode('utf-8'))
checks=json.loads((OLD/'STATIC-CHECKS.json').read_bytes())
checks['observed_utc']=datetime.now(timezone.utc).isoformat()
checks['candidate_revision']='after-c5-next-source-r02'
checks['C1_top11_and_deny_AST_identical_to_r03']=False
checks['C1_top11_change_explanation']='closure exact allow tuple adds only natural declared M. Other10 common functions and deny unchanged.'
checks['full_runner_AST_equal_to_r01_after_only_declared_closure_tuple_inverse']=True
checks['closure_AST_equal_to_r01']=False
checks['closure_fix']=manifest['source_evidence']['closure_fix']
checks['prior_D3_19_files_preserved']=True
checks['new_SOURCE_PRE_ACTUAL_POST']='NOT_RUN'
checks['current_product_fixture_import_loader_discovery_cloud_runs']=0
checks['compiled_in_memory_files']=['runner.py','prepare-command.py','pristine4 unchanged','r02 draft embedded Python']
for p in OUT.rglob('*.py'):compile(p.read_bytes(),str(p),'exec')
for p,pin0 in oldpins.items():assert pin(Path(p).read_bytes())==pin0
for name in manifest['sources']:assert (OUT/name).read_bytes()==(OLD/name).read_bytes()
put('STATIC-CHECKS.json',jb(checks))
seal={p.relative_to(OUT).as_posix():pin(p.read_bytes()) for p in sorted(OUT.rglob('*')) if p.is_file()}
put('SOURCE.SHA256.json',jb(seal))
with zipfile.ZipFile(OUT/'transport.zip','x',compression=zipfile.ZIP_DEFLATED) as z:
 for p in sorted(OUT.rglob('*')):
  if p.is_file() and p.name!='transport.zip':z.write(p,p.relative_to(OUT).as_posix())
with zipfile.ZipFile(OUT/'transport.zip') as z:
 for name in z.namelist():assert z.read(name)==(OUT/name).read_bytes()
print(json.dumps({'SOURCE_only':True,'files':len(seal)+2,'bytes':sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file()),
 'seal_count':len(seal),'zip_count':len(seal)+1,'pins':{n:pin((OUT/n).read_bytes()) for n in ['runner.py','manifest.json','SOURCE.SHA256.json','transport.zip','SOURCE.DIFF.patch','LAUNCH.UNISSUED.SOURCE-DRAFT.template.txt']},'execution0':True}))

