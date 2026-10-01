import json,tempfile
from pathlib import Path
from flowmarshal.engine.e2e_qualification import _copy_fixture,_prepare
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher,FakeCodexRuntime,RUNTIME_OWNER_PLATFORM_UNSUPPORTED
from tests.test_engine_qualification import qualification_inventory
root=Path('/workspace/vm-g-failure-shard')
with tempfile.TemporaryDirectory(prefix='vm-g-platform-only-') as temp:
 base=Path(temp);workspace,_=_copy_fixture(root,base);inventory=qualification_inventory()
 prepared=_prepare(workspace=workspace,state_root=base/'state',inventory=inventory,roles=default_role_configuration(root))
 runtime=FakeCodexRuntime(inventory);result=EngineDispatcher(prepared.service,runtime).run_once(prepared.project_id,proposal=prepared.proposal)
 observed={'action':result.action.value,'detail':result.detail,'create_calls':runtime.create_calls,'turn_calls':runtime.turn_calls}
 assert observed['action']=='blocked',observed
 assert observed['detail']==RUNTIME_OWNER_PLATFORM_UNSUPPORTED,observed
 assert (runtime.create_calls,runtime.turn_calls)==(0,0),observed
 print(json.dumps(observed,ensure_ascii=False))
