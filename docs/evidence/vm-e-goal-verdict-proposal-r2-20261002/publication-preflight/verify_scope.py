import ast, json, subprocess

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()
base='32bb0f9dd9f024045d24487312b50f5b703573a3'
chain=[base,'98bb122970f82bb5fcef122003ee10c7eb787cf4','d6956481a964f1b84fb88baa03218a3034f8978d','927f9d2936635d8c987c2a3a4092e6e4dc2c742b','f862dc4f948dde8b6f429bcbaa073f03ce41a70f']
assert git('rev-parse','FETCH_HEAD') == chain[2]
for previous, current in zip(chain,chain[1:]):
    assert git('rev-parse',current+'^') == previous
    subprocess.run(['git','merge-base','--is-ancestor',previous,current],check=True)
source='src/flowmarshal/engine/service.py'
test='tests/test_engine_goal_verdict_authority.py'
files=git('diff','--name-only',base,chain[-1]).splitlines()
product=[p for p in files if not p.startswith('docs/evidence/vm-e-goal-verdict-proposal-20261002/')]
assert product == [source,test],product
old=subprocess.check_output(['git','show',base+':'+source],text=True)
new=subprocess.check_output(['git','show',chain[-1]+':'+source],text=True)
def omit_method(text):
    tree=ast.parse(text)
    matches=[node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='record_goal_verdict']
    assert len(matches)==1
    method=matches[0]
    lines=text.splitlines(keepends=True)
    return ''.join(lines[:method.lineno-1]+lines[method.end_lineno:])
assert omit_method(old)==omit_method(new)
assert git('diff','--name-only',chain[1],chain[2]).splitlines() and all(p.startswith('docs/evidence/vm-e-goal-verdict-proposal-20261002/') for p in git('diff','--name-only',chain[1],chain[2]).splitlines())
assert git('diff','--name-only',chain[3],chain[4])==test
assert not git('status','--porcelain')
print(json.dumps({'chain':[dict(zip(('commit','tree','parents'),git('show','-s','--format=%H%n%T%n%P',c).splitlines())) for c in chain], 'remote_before':chain[2], 'candidate_descends_from_remote':True,'publication_mode':'FAST_FORWARD_ONLY_SAME_PROPOSAL_BRANCH','changed_product_files':product,'source_change_only_record_goal_verdict':True,'prior_artifact_files_unchanged_since_d695':not git('diff','--name-only',chain[2],chain[4],'--','docs/evidence/'),'working_tree_clean':True,'no_tests_run':True},ensure_ascii=False,indent=2))
