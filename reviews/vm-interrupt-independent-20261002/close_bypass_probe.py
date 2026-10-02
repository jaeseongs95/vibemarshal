"""supervisor claim의 범위와 별도 adapter.close 경로를 구별한다. SDK transport는 합성."""
import json,sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from independent_probe import fixture,snapshot,THREAD,TURN,HERE
from flowmarshal.engine.runtime import CodexAppServerRuntime,RuntimeJobSupervisor
source,label=Path(sys.argv[1]),sys.argv[2]
with fixture(source,label,'separate_adapter_close_bypasses_claim') as (root,p,j):
    calls=[]
    sender=object.__new__(CodexAppServerRuntime)
    sender._interrupted_turn_ids=set()
    def raw(method,params):
        calls.append({'method':method,'params':params,'path':'supervisor'})
        return {'synthetic':True}
    sender._raw=raw
    RuntimeJobSupervisor(p.service,sender).request_interrupt(j,reason='workflow_paused')
    closer=object.__new__(CodexAppServerRuntime)
    closer._interrupted_turn_ids=set();closer._completion_observers={}
    def handle_interrupt():
        calls.append({'method':'sdk.turn/interrupt','params':{'threadId':THREAD,'turnId':TURN},'path':'separate_adapter_close'})
        return SimpleNamespace()
    closer._turn_futures={THREAD:(SimpleNamespace(id=TURN,interrupt=handle_interrupt),Future())}
    closer._codex=SimpleNamespace(close=lambda:None)
    closer._actual_rpc=lambda method,params,operation,**kwargs:operation()
    closer.close(timeout_seconds=1)
    assert len(calls)==2,calls
    assert all(x['params']=={'threadId':THREAD,'turnId':TURN} for x in calls)
    after=snapshot(p.service,p.project_id,j,root)
    assert len(after['markers'])==(1 if label=='candidate' else 0)
    out={'task_id':'vm-interrupt-independent-review-20261002','source':label,'calls':calls,'rpc_attempt_count':len(calls),
         'live_provider_calls':0,'actual_external_effect_count':None,'after':after,
         'scope':'separate adapter object with a synthetic same-turn handle; no actual concurrent native connection proof'}
    (HERE/(label+'-close-results.json')).write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps({k:v for k,v in out.items() if k not in ['after','calls']},sort_keys=True))
