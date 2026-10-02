import ast, hashlib, json, re, shutil, subprocess
from pathlib import Path
repo=Path('/workspace/vm-e-r2')
source=Path('/workspace/vm-e-r2-evidence')
publication=Path('/workspace/vm-e-r2-publication')
dest=repo/'docs/evidence/vm-e-goal-verdict-proposal-r2-20261002'
assert not dest.exists()
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip() == 'f862dc4f948dde8b6f429bcbaa073f03ce41a70f'
def sha(b): return hashlib.sha256(b).hexdigest()
def scan(path,data):
    assert not path.is_symlink(), str(path)
    text=data.decode('utf-8')
    assert '\x00' not in text
    patterns=(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',r'gh[pousr]_[A-Za-z0-9]{20,}',r'github_pat_[A-Za-z0-9_]{20,}',r'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}',r'AKIA[A-Z0-9]{16}',r'https?://[^\s/@]+:[^\s/@]+@')
    for pattern in patterns:
        assert not re.search(pattern,text), f'credential pattern at {path.name}'
    return text
files=[]
for path in sorted(source.iterdir()):
    if path.is_file() and (path.suffix in ('.py','.json','.jsonl','.txt','.patch')):
        data=path.read_bytes(); scan(path,data); files.append((path, path.name, data))
for name in ('remote-preflight.raw.txt','remote-fetch-before.raw.txt','ancestry-and-product-scope.raw.txt','commands.jsonl','run.py','verify_scope.py','prepare_artifacts.py'):
    path=publication/name
    data=path.read_bytes(); scan(path,data); files.append((path,'publication-preflight/'+name,data))
dest.mkdir(parents=True)
inventory=[]
for path,name,data in files:
    target=dest/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
    assert target.read_bytes()==data
    inventory.append({'path':name,'source':str(path),'bytes':len(data),'sha256':sha(data),'sanitization':'utf8 text, no NUL/symlink/binary/credential pattern; synthetic fixture reports only; preserved original bytes'})
for name,args in (
    ('cumulative-product.diff',('32bb0f9dd9f024045d24487312b50f5b703573a3','f862dc4f948dde8b6f429bcbaa073f03ce41a70f','--','src/flowmarshal/engine/service.py','tests/test_engine_goal_verdict_authority.py')),
    ('compatibility-boundary.diff',('d6956481a964f1b84fb88baa03218a3034f8978d','f862dc4f948dde8b6f429bcbaa073f03ce41a70f','--','src/flowmarshal/engine/service.py','tests/test_engine_goal_verdict_authority.py'))):
    data=subprocess.check_output(['git','diff','--binary',*args],cwd=repo)
    scan(dest/name,data); (dest/name).write_bytes(data)
    inventory.append({'path':name,'bytes':len(data),'sha256':sha(data),'origin':'exact git diff scoped to product files'})
assert (dest/'compatibility-boundary.diff').read_bytes()==(dest/'reviewer-boundary.patch').read_bytes()
(dest/'publication-inventory.json').write_text(json.dumps({'format':'vm-e-r2-publication-v1','implementation_candidate':'f862dc4f948dde8b6f429bcbaa073f03ce41a70f','candidate_tree':'9388e2ade7f28341034b7d8c0271e59dcf4bc847','candidate_parent':'927f9d2936635d8c987c2a3a4092e6e4dc2c742b','remote_before':'d6956481a964f1b84fb88baa03218a3034f8978d','mode':'same proposal branch fast-forward only','product_files':['src/flowmarshal/engine/service.py','tests/test_engine_goal_verdict_authority.py'],'source_scope':'record_goal_verdict only; AST range excluded comparison confirms all outside bytes unchanged from original base','evidence_separate':True,'files':inventory,'excluded':['fixtures/ synthetic SQLite DB/temp state','*.bundle binary local transfer files','operational DB/profile/keys/native/provider transcripts'],'model':{'requested':'gpt-6.1-sol/high','provider_observed_model':None,'provider_observed_effort':None},'new_tests':0,'main_merge':False,'force':False,'source_GO':False,'F07_exception':False},ensure_ascii=False,indent=2)+'\n')
(dest/'README.md').write_text("""# VM E GoalVerdict 좁힌 proposal: revision 2

이 자료는 독립 리뷰어 `01a0f9e2-ed1d-7296-815a-14270ae95034`가 변경된 이전 PASS 호환성 경계를 재검토하기 위한 proposal이다. Parent가 게시를 명시 승인했다. 제품 적용·main merge·source GO·F07 예외를 뜻하지 않는다.

구현 candidate는 `f862dc4f948dde8b6f429bcbaa073f03ce41a70f`, tree는 `9388e2ade7f28341034b7d8c0271e59dcf4bc847`, 직접 parent는 `927f9d2936635d8c987c2a3a4092e6e4dc2c742b`다. 계보는 `32bb0f9dd9f024045d24487312b50f5b703573a3 → 98bb122970f82bb5fcef122003ee10c7eb787cf4 → d6956481a964f1b84fb88baa03218a3034f8978d → 927f9d2936635d8c987c2a3a4092e6e4dc2c742b → f862dc4f948dde8b6f429bcbaa073f03ce41a70f`다. 각 단계의 직접 parent, tree, 원격 preflight와 fast-forward 적합성은 `publication-preflight/ancestry-and-product-scope.raw.txt`에 있다. d695 원격 branch에서 fast-forward하며 기존 candidate와 evidence를 모두 계보에 보존한다.

독립 리뷰는 정확히 최신 PASS ID 집합을 요구하는 E03 강화가 명시 계약이 아니며, 같은 validation/evidence/binding의 여전히 유효한 이전 PASS까지 거부함을 확인했다. Parent 지시에 따라 그 강화만 제거했다. 같은 Plan에 기록된 실제 PASS subset이라는 원래 검사로 복구하고, 최신 필수 Task/integration PASS gate와 active Plan 보호를 유지했다. 기존 E01/E02/E04 규범 근거와 E03의 비규범 한계는 원래 `../vm-e-goal-verdict-proposal-20261002/normative-sources.json`에 보존돼 있다. 이 revision에서 새 계약이나 대체 강화 정책을 추가하지 않았다.

최소 호환성 경계는 `old_pass=self.integration`, `record_result(task=False)`로 동일 validation/evidence의 새 PASS를 기록하고 old_pass ID를 제출하는 경우다. 이 제출은 성공하며 저장된 verdict는 old ID를 유지한다. 회귀는 두 PASS와 모든 evidence/Attempt/runtime intent/합성 receipt 행이 변경 없이 남는지 원문 tuple로 비교하며 모든 보존 table이 비어 있지 않음도 확인한다. 동일 non-null Goal Test input binding 경로도 직접 API에서 검사한다. 최신 FAIL/INCONCLUSIVE 및 superseded Plan 제출은 GoalVerdict/history/project/Plan와 이 5개 기록 table의 모든 행을 원자적으로 보존하면서 거부된다.

최종 결과는 집중 검사 18/18 PASS다. 같은 최종 회귀를 원래 강화 구현에 적용하면 17개 중 15 PASS와 호환성 2 ERROR다. 이번 게시 단계에서 새 테스트는 실행하지 않았다. `reviewer-boundary-report.json`에 정확한 argv/cwd/UTC/wall/exit/raw SHA256, file ownership, candidate SHA/tree/parent, patch·bundle hash와 NOT_RUN이 있다. `reviewer-boundary-final.raw.txt`와 `reviewer-boundary-r1-before.raw.txt`는 원본 raw다. `compatibility-boundary.diff`는 d695→f862 제품 diff이며 `reviewer-boundary.patch`와 byte 동일하다. `cumulative-product.diff`는 원 base32bb→f862의 누적 제품 diff다.

제품 소유 범위는 `src/flowmarshal/engine/service.py::record_goal_verdict` 한 메서드와 `tests/test_engine_goal_verdict_authority.py`뿐이다. 증거는 별도 artifact commit과 이 directory에 둔다. source의 다른 바이트는 base와 동일함을 AST method range 제외 비교로 검증했다. d695의 기존 evidence는 변경하지 않았다. 927f 및 f862의 두 단계 commit을 squash/amend/rebase하지 않았다.

`report.json`, `report.txt`, `reviewer-boundary-report.*`의 LOCAL ONLY/미게시 문구는 게시 승인 전 동결 당시의 역사 상태다. 원문과 hash를 보존하므로 수정하지 않았다. 이 README와 publication preflight는 현재 parent의 후속 게시 승인을 기록한다. Push/readback은 이 artifact commit을 만든 뒤 발생하므로 후속 로컬 publication commands와 parent 전달 최종 응답에서 확인한다. 자기 참조 hash를 만들기 위해 원본 증거를 재작성하지 않는다.

합성 DB와 fixture만 사용했으며 DB·profile·key·binary bundle·운영/native/provider transcript는 이 packet에서 제외했다. 포함 파일은 UTF-8, NUL·symlink·credential 패턴 검사를 통과했고 원본 byte/hash를 inventory에 기록했다. validation command는 synthetic stub이며 direct Goal Test API의 기존 supervisor=None adapter를 사용했다. D1 Windows runtime guard는 변경하거나 우회하지 않았다. NOT_RUN은 실제 Windows runtime/semantic terminal, 다른 worker 소유 public vertical E2E·process faults/race, broad 118 portable tests·SQLite campaign이다. 요청 model/effort는 gpt-6.1-sol/high, provider observed 값은 turn receipt 미노출로 unknown이다. 추가 model/provider 호출과 fallback은 0이다.

F07 first-four source GO0, producer3 미배정, D7 after F07 merged 경계를 유지한다. Parent의 재검토 전 main 병합은 하지 않는다.
""",encoding='utf-8')
for path in dest.rglob('*'):
    if path.is_file(): scan(path,path.read_bytes())
print(json.dumps({'packet':str(dest),'copied_original_text_files':len(files),'generated_files':4,'all_scans_pass':True,'original_bytes_preserved':True,'no_new_tests':True},ensure_ascii=False))
