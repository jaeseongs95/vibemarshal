import datetime
import hashlib
import json
from pathlib import Path
import subprocess

root = Path('/workspace/vm-e-r2-evidence')
repo = Path('/workspace/vm-e-r2')
old = Path('/workspace/vm-e-proposal')

def git(cwd, *args):
    return subprocess.check_output(['git', *args],cwd=cwd,text=True).strip()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

records = [json.loads(line) for line in (root/'commands.jsonl').read_text().splitlines()]
identity = {
    'implementation_commit':git(repo,'rev-parse','HEAD'),
    'candidate_tree':git(repo,'rev-parse','HEAD^{tree}'),
    'parent_commit':git(repo,'rev-parse','HEAD^'),
    'old_implementation_commit':'98bb122970f82bb5fcef122003ee10c7eb787cf4',
    'old_implementation_tree':git(old,'rev-parse','98bb122970f82bb5fcef122003ee10c7eb787cf4^{tree}'),
    'original_direct_base':'32bb0f9dd9f024045d24487312b50f5b703573a3',
    'local_branch':git(repo,'branch','--show-current'),
    'candidate_clean':not git(repo,'status','--porcelain'),
    'old_proposal_HEAD':git(old,'rev-parse','HEAD'),
    'old_proposal_clean':not git(old,'status','--porcelain'),
}
report = dict(
    format='vm-e-goal-verdict-r2-review-packet-v1',
    generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    identity=identity,
    status='FROZEN_LOCAL_REVISION_AWAITING_PARENT_CHECK_NOT_PUBLISHED',
    authority='현재 parent의 명시 결정: 최신-ID 동등성은 미승인 강화이므로 제거하고 확인된 최신 실패·state 보호만 유지',
    review_task='01a0f9e2-ed1d-7296-815a-14270ae95034',
    independent_report_read=False,
    review_detail_source='parent가 제공한 독립 재현 및 결정만 사용; 추가 상세 보고서는 필요하지 않았음',
    source_change='record_goal_verdict에서 필수 integration의 최신 PASS gate를 유지하고 result ID 검사를 원래 same-Plan 실제 PASS subset 규칙으로 복구',
    preserved=['최신 Task FAIL/INCONCLUSIVE/NOT_RUN 차단','최신 integration FAIL/INCONCLUSIVE 차단',
               'active Plan 확인과 superseded Plan state 전이 차단',
               '기존 Goal/Plan digest, 같은 Plan에 기록된 PASS result ID, evidence 존재 검사',
               '기존 Goal Test 입력 freshness·active revision·command operation binding 검증'],
    compatibility=['legacy 동일 binding/evidence의 이전 PASS ID 허용',
                   '동일 non-null Goal Test binding에서 실제 직접 API로 기록한 이전 PASS ID 허용'],
    before={'revision':'98bb122970f82bb5fcef122003ee10c7eb787cf4',
            'tests':17,'passed':15,'compatibility_errors':2,'label':'frozen-r1-before'},
    after={'tests':18,'passed':18,'failures':0,'errors':0,'skipped':0,'label':'r2-targeted-after',
           'breakdown':'R2 집중 회귀 17개 + 기존 변조 operation binding 검사 1개'},
    file_ownership=[{'owner':'VM E single writer', 'path':path,
                     'candidate_bytes_sha256':sha(repo/path)} for path in
                    ('src/flowmarshal/engine/service.py','tests/test_engine_goal_verdict_authority.py')],
    numstat=git(repo,'diff','--numstat',identity['parent_commit'],'HEAD'),
    diff={'path':str(root/'r2.patch'),'sha256':sha(root/'r2.patch')},
    local_bundle={'path':str(root/'r2-proposal.bundle'),'sha256':sha(root/'r2-proposal.bundle'),
                  'prerequisite':identity['parent_commit'],'verified':True},
    regression_bytes_sha256=sha(repo/'tests/test_engine_goal_verdict_authority.py'),
    commands=records,
    test_limits=['직접 Core service/Goal Test API와 기존 synthetic fixture의 bounded 테스트',
                 '직접 Goal Test API에서는 기존 supervisor=None 경로를 사용; production Dispatcher의 D1 guard를 패치하거나 해제하지 않음',
                 'validation command는 CompletedProcess synthetic stub; 실제 native command나 live provider 실행 없음',
                 '입력 변경 검사는 Goal Test API에서 command/result write 이전 BLOCKED를 확인하며 Core 완료 writer에 새 freshness 정책을 추가한 것이 아님',
                 '같은 입력/binding 이전 PASS 재사용을 허용하기 위해 기존 freshness 또는 binding 검증을 변경하지 않음',
                 'NOT_RUN: 실제 Windows runtime/semantic terminal 경로, public vertical E2E, process fault/race, broad 118 portable tests, SQLite campaign',
                 '동등하지 않은 과거 입력을 자동 재승인하거나 E03의 대체 강화 정책을 새로 도입하지 않음'],
    operations={'provider_calls':0,'fallback':0,'force':False,'rebase':False,'merge':False,
                'push_or_publication':False,'Library':False,'source_or_F07_GO':False,
                'F07_first_four_gate_preserved':True},
    model={'requested':'gpt-6.1-sol/high','provider_observed_model':None,
           'provider_observed_effort':None,'reason':'turn별 provider receipt 노출 없음'},
)
(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
by_label={item['label']:item for item in records}
lines=[
    'VM E R2: exact latest-ID 강화 제거. 최신 실패·active Plan 보호 유지. LOCAL ONLY, 부모 확인 대기.',
    f"commit: {identity['implementation_commit']}",
    f"tree: {identity['candidate_tree']}",
    f"parent: {identity['parent_commit']} (기존 artifact-only commit)",
    f"old candidate: {identity['old_implementation_commit']} / {identity['old_implementation_tree']}",
    '원 후보·원 evidence·기존 원격 branch는 수정하지 않음. 새 commit의 변경 파일은 service.py와 동일 회귀 파일뿐.',
    'service.py 변경은 record_goal_verdict 한 메서드(+9/-11), 회귀 파일 +128/-2.',
    '최신 필수 integration 상태를 PASS로 요구하는 gate는 유지하고, 같은 Plan에 기록된 실제 PASS ID subset 검사로 되돌렸다.',
    '동일 non-null Goal Test input binding 및 legacy binding의 이전 PASS 허용을 각각 검사했다.',
    '최신 FAIL/INCONCLUSIVE/NOT_RUN, 잘못된 Goal/Plan/revision/결과 ID, superseded Plan state 변경은 계속 거부된다.',
    'Goal Test 입력 변경은 실제 기존 직접 API에서 command/result write 전에 BLOCKED; 변조 operation binding도 거부된다.',
    'before: 동일 최종 R2 회귀를 기존 구현으로 실행, 17개 중 15 PASS + 호환성 2 ERROR.',
    'after: R2 회귀 17개 + 기존 binding 검사 1개, 18/18 PASS. broad 반복 없음.',
    'git diff --check, 적용 가능성 --check 및 local bundle 검증 PASS. 실제 apply/merge/push/rebase/force 없음.',
    f"patch SHA-256: {report['diff']['sha256']}",
    f"bundle SHA-256: {report['local_bundle']['sha256']}",
    '', '결정적인 명령 provenance:',
]
for label in ('frozen-r1-before','r2-targeted-after','r2-staged-diff-check','r1-patch-check'):
    item=by_label[label]
    lines.append(f"{label}: cwd={item['cwd']}; exit={item['exit_code']}; UTC={item['started_at']}; wall={item['duration_seconds']:.6f}s; rawSHA256={item['raw_sha256']}")
lines+=['', '범위 및 NOT_RUN:']+report['test_limits']+[
    '현재 parent 결정을 따른 수정이며 self-approval/GO가 아니다. reviewer 상세 원문은 추가로 읽지 않았고 parent의 요약과 결정으로 충분했다.',
    'requested gpt-6.1-sol/high, provider-observed null/null. 추가 provider calls/fallback 0.',
]
(root/'report.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps({'report':str(root/'report.json'),'report_sha256':sha(root/'report.json'),
                  'report_text':str(root/'report.txt'),'report_text_sha256':sha(root/'report.txt')},ensure_ascii=False))
