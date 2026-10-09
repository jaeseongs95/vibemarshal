import datetime,hashlib,json,os,pathlib,subprocess,sys
ROOT=pathlib.Path('/workspace/w4-r02-codexcloud-py310')
OUT=ROOT/'evidence/vm-w4-r02/2026-10-09/codexcloud-py310/run-01'
OUT.mkdir(exist_ok=True)
ENV=dict(os.environ,UV_CACHE_DIR=str(ROOT/'uv-cache'),UV_PYTHON_INSTALL_DIR=str(ROOT/'uv-python'),UV_PYTHON_BIN_DIR=str(ROOT/'uv-bin'),XDG_CACHE_HOME=str(ROOT/'xdg-cache'),TMPDIR=str(ROOT/'tmp'))
(ROOT/'tmp').mkdir(exist_ok=True)
def capture(name,argv,cwd=None):
 cwd=str(cwd or ROOT); start=datetime.datetime.now(datetime.timezone.utc).isoformat()
 p=subprocess.run(argv,cwd=cwd,env=ENV,capture_output=True); end=datetime.datetime.now(datetime.timezone.utc).isoformat()
 for stream in ('stdout','stderr'): (OUT/(name+'.'+stream)).write_bytes(getattr(p,stream))
 r={'argv':argv,'cwd':cwd,'start':start,'end':end,'exit':p.returncode,'environment_overrides':{k:ENV[k] for k in ('UV_CACHE_DIR','UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR','XDG_CACHE_HOME','TMPDIR')},'streams':{s:{'path':name+'.'+s,'bytes':len(getattr(p,s)),'sha256':hashlib.sha256(getattr(p,s)).hexdigest()} for s in ('stdout','stderr')}}
 (OUT/(name+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n'); print(json.dumps(r,ensure_ascii=False)); print(p.stdout.decode(errors='replace')); print(p.stderr.decode(errors='replace')); return p
if __name__=='__main__': sys.exit(capture(sys.argv[1],sys.argv[2:]).returncode)
