"""RPC 횟수와 실제 효과 의미를 분리하는 합성 provider 반례. 실제 provider 증명 아님."""
import json
import sys
import threading
from pathlib import Path
from independent_probe import fixture, snapshot, append, THREAD, TURN, HERE
from flowmarshal.engine.runtime import CodexAppServerRuntime, RuntimeJobSupervisor

source,label=Path(sys.argv[1]),sys.argv[2]
with fixture(source,label,'idempotent_provider_semantics') as (root,p,j):
    first_inside=threading.Event();release=threading.Event();guard=threading.Lock()
    calls=[];provider_effects=[];seen=set();errors=[]
    def rpc(method,params):
        assert method=='turn/interrupt'
        assert params=={'threadId':THREAD,'turnId':TURN}
        with guard:
            calls.append({'method':method,'params':params})
            key=(params['threadId'],params['turnId'])
            if key not in seen:
                seen.add(key);provider_effects.append({'target':key,'transition':'active_to_interrupted'})
            first=len(calls)==1
        if first:
            first_inside.set()
            assert release.wait(5)
        return {'synthetic':True,'interruptAlreadyApplied':not first}
    def adapter():
        value=object.__new__(CodexAppServerRuntime)
        value._raw=rpc;value._interrupted_turn_ids=set()
        return value
    a=RuntimeJobSupervisor(p.service,adapter(),interrupt_timeout_seconds=3)
    b=RuntimeJobSupervisor(p.service,adapter(),interrupt_timeout_seconds=3)
    def invoke():
        try:a.request_interrupt(j,reason='workflow_paused')
        except BaseException as e:errors.append(type(e).__name__+': '+str(e))
    sender=threading.Thread(target=invoke);sender.start()
    try:
        assert first_inside.wait(5)
        b.request_interrupt(j,reason='workflow_paused')
    finally:
        release.set();sender.join(5)
    assert not errors and not sender.is_alive()
    assert len(calls)==(1 if label=='candidate' else 2)
    assert len(provider_effects)==1
    out={'task_id':'vm-interrupt-independent-review-20261002','source':label,
        'rpc_calls':calls,'rpc_count':len(calls),'synthetic_effect_count':len(provider_effects),
        'effect_model':'synthetic exact-target idempotent active-to-interrupted transition; not actual provider behavior',
        'live_provider_calls':0,'after':snapshot(p.service,p.project_id,j,root)}
    (HERE/(label+'-idempotent-results.json')).write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps({k:v for k,v in out.items() if k not in ['after','rpc_calls']},sort_keys=True))
