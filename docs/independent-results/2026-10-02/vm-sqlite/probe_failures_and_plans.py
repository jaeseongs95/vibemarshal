"""원본 DB 접근 없이 fault injection과 합성 query-plan 비교를 기록한다."""
from test_sqlite_boundaries import *
from collections import Counter

RESULT={'candidate':'32bb0f9dd9f024045d24487312b50f5b703573a3','scope':'synthetic-only','findings':[]}
def record(name,data):
    RESULT['findings'].append({'name':name,**data});print(name,json.dumps(data,ensure_ascii=False),flush=True)

def concurrent_init(path,barrier,q):
    class SyncedLedger(SQLiteEngineLedger):
        def _connect(self,*,readonly=False):
            if not readonly:barrier.wait(timeout=10)
            return super()._connect(readonly=readonly)
    try:SyncedLedger(path).initialize();q.put({'outcome':'initialized'})
    except BaseException as exc:q.put({'outcome':type(exc).__name__,'error':str(exc)})

with tempfile.TemporaryDirectory(prefix='sqlite-probes-',dir=HERE/'artifacts') as tmp:
    root=Path(tmp)
    ledger=SQLiteEngineLedger(root/'atomic-init.sqlite3')
    orig=ledger._connect
    class FaultConnection:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def executemany(self,*a,**k):raise RuntimeError('INJECTED: metadata write interrupted')
    with patch.object(ledger,'_connect',lambda **k:FaultConnection(orig(**k))):
        try:ledger.initialize()
        except RuntimeError:pass
    with sqlite3.connect(ledger.path) as c:
        state={'tables_after_failed_init':c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
               'meta_rows':c.execute('SELECT COUNT(*) FROM schema_meta').fetchone()[0],
               'application_id':c.execute('PRAGMA application_id').fetchone()[0],
               'user_version':c.execute('PRAGMA user_version').fetchone()[0]}
    try:ledger.initialize();state['retry']='success'
    except Exception as e:state['retry']=type(e).__name__+': '+str(e)
    record('init_metadata_failure_leaves_partial_db',state)

    ctx=mp.get_context('fork');barrier=ctx.Barrier(2);q=ctx.Queue();path=str(root/'race-init.sqlite3')
    ps=[ctx.Process(target=concurrent_init,args=(path,barrier,q)) for i in range(2)]
    for p in ps:p.start()
    out=[q.get(timeout=20) for p in ps]
    for p in ps:p.join(20)
    record('concurrent_first_initialize',{'outcomes':out,'exit_codes':[p.exitcode for p in ps]})

    ledger=SQLiteEngineLedger(root/'malformed-history.sqlite3');seed(ledger)
    with ledger.transaction() as tx:
        tx.connection.execute('DROP TRIGGER tr_engine_history_no_update')
        tx.connection.execute("UPDATE history_events SET payload_json='{bad json'")
    corruption={}
    for name,call in [('verify_history',lambda:ledger.verify_history(P)),('project_snapshot',lambda:ledger.project_snapshot(P))]:
        try:corruption[name]={'returned':call()}
        except Exception as e:corruption[name]={'exception':type(e).__name__,'message':str(e)}
    record('malformed_history_json',corruption)

    ledger=SQLiteEngineLedger(root/'history-reader.sqlite3');seed(ledger)
    aux=root/'reader-attached.sqlite3'
    with sqlite3.connect(aux) as c:c.execute('CREATE TABLE x(value)')
    reader=SQLiteEngineHistoryReader(ledger.path)
    with reader.read() as c:
        qonly=c.execute('PRAGMA query_only').fetchone()[0]
        c.execute('ATTACH DATABASE ? AS aux',(str(aux),));c.execute("INSERT INTO aux.x VALUES('synthetic')");c.commit()
    with role_execution_scope('synthetic-worker'):
        with reader.read() as c:role_read=c.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
    record('history_reader_confinement_gap',{'query_only':qonly,'attached_db_write':'succeeded on synthetic auxiliary','role_scope_read_rows':role_read,'scope_note':'same-process raw-code isolation is explicitly not a product guarantee; defense-in-depth only'})

    ledger=SQLiteEngineLedger(root/'plans.sqlite3');seed(ledger)
    with ledger.transaction() as tx:
        for project_no in range(60):
            project_id='project_'+f'{project_no+100:032x}'
            tx.connection.execute('INSERT INTO projects(id,name,root,artifact_root,run_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(project_id,'synthetic',f'/fixture/{project_no}',f'/fixture/{project_no}/artifacts','idle',STAMP,STAMP))
            for i in range(30):
                tx.connection.execute('INSERT INTO runtime_jobs(id,project_id,kind,status,checkpoint_key,request_digest,request_json,absolute_deadline_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(new_id('job'),project_id,'worker_turn','consumed',f'cp-{i}',sha256_digest({}),'{}','2027-01-01T00:00:00Z',STAMP,STAMP))
                tx.history(project_id,'synthetic.plan','project',project_id,{'i':i})
                plan_id=new_id('plan_revision');task_id=new_id('task')
                tx.connection.execute('INSERT INTO plan_revisions(id,plan_id,project_id,revision_no,definition_digest,activation_digest,payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(plan_id,new_id('plan'),project_id,1,new_id('digest'),new_id('activation'),'{}','completed',STAMP))
                tx.connection.execute('INSERT INTO task_contracts(id,project_id,plan_revision_id,task_ref,position,contract_digest,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(task_id,project_id,plan_id,'task',0,'synthetic','{}','completed',STAMP,STAMP))
                tx.connection.execute('INSERT INTO attempts(id,project_id,plan_revision_id,task_id,execution_spec_digest,attempt_no,kind,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(new_id('attempt'),project_id,plan_id,task_id,'synthetic',1,'execution','succeeded',STAMP,STAMP))
                tx.connection.execute('INSERT INTO context_source_registrations(id,project_id,kind,path,content_digest,payload_json,registered_at) VALUES(?,?,?,?,?,?,?)',(new_id('context'),project_id,'reference',f'/fixture/{project_no}/ref-{i}',f'digest-{i}','{}',STAMP))
        for i in range(20):
            tx.connection.execute('INSERT INTO runtime_jobs(id,project_id,kind,status,checkpoint_key,request_digest,request_json,absolute_deadline_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(new_id('job'),P,'worker_turn','consumed',f'cp-{i}',sha256_digest({}),'{}','2027-01-01T00:00:00Z',STAMP,STAMP))
            plan_id=new_id('plan_revision');task_id=new_id('task')
            tx.connection.execute('INSERT INTO plan_revisions(id,plan_id,project_id,revision_no,definition_digest,activation_digest,payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(plan_id,new_id('plan'),P,1,new_id('digest'),new_id('activation'),'{}','completed',STAMP))
            tx.connection.execute('INSERT INTO task_contracts(id,project_id,plan_revision_id,task_ref,position,contract_digest,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(task_id,P,plan_id,'task',0,'synthetic','{}','completed',STAMP,STAMP))
            tx.connection.execute('INSERT INTO attempts(id,project_id,plan_revision_id,task_id,execution_spec_digest,attempt_no,kind,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(new_id('attempt'),P,plan_id,task_id,'synthetic',1,'execution','succeeded',STAMP,STAMP))
            tx.connection.execute('INSERT INTO context_source_registrations(id,project_id,kind,path,content_digest,payload_json,registered_at) VALUES(?,?,?,?,?,?,?)',(new_id('context'),P,'reference',f'/fixture/target/ref-{i}',f'digest-{i}','{}',STAMP))
    queries={
      'history_tail':('SELECT sequence,event_hash FROM history_events WHERE project_id=? ORDER BY sequence DESC LIMIT 1',(P,)),
      'history_all':('SELECT * FROM history_events WHERE project_id=? ORDER BY sequence',(P,)),
      'snapshot_jobs':('SELECT id,kind,status,checkpoint_key FROM runtime_jobs WHERE project_id=? ORDER BY created_at,rowid',(P,)),
      'snapshot_plans':('SELECT id,plan_id,revision_no,activation_digest,status FROM plan_revisions WHERE project_id=? ORDER BY plan_id,revision_no',(P,)),
      'snapshot_tasks':('SELECT id,plan_revision_id,position,status FROM task_contracts WHERE project_id=? ORDER BY plan_revision_id,position',(P,)),
      'snapshot_attempts':('SELECT id,status FROM attempts WHERE project_id=? ORDER BY created_at,rowid',(P,)),
      'snapshot_context':('SELECT id FROM context_source_registrations WHERE project_id=? ORDER BY registered_at,rowid',(P,)),
    }
    def measure():
        data={}
        with ledger.read() as c:
            for name,(sql,params) in queries.items():
                c.execute(sql,params).fetchall()  # cold schema/statement setup 제외
                ticks=[0];c.set_progress_handler(lambda:ticks.__setitem__(0,ticks[0]+1) or 0,1)
                rows=[tuple(r) for r in c.execute(sql,params).fetchall()];c.set_progress_handler(None,0)
                data[name]={'plan':[r[3] for r in c.execute('EXPLAIN QUERY PLAN '+sql,params)],'vm_steps':ticks[0],'row_count':len(rows),'rows_sha256':sha256_digest(rows)}
        return data
    before=measure()
    with ledger.transaction() as tx:
        inventory=Counter(r[0] for r in tx.connection.execute("SELECT type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        all_indexes=tx.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'").fetchone()[0]
        tx.connection.execute('CREATE INDEX fixture_runtime_jobs_project_created ON runtime_jobs(project_id,created_at)')
        tx.connection.execute('CREATE INDEX fixture_plans_project_plan_revision ON plan_revisions(project_id,plan_id,revision_no)')
        tx.connection.execute('CREATE INDEX fixture_tasks_project_plan_position ON task_contracts(project_id,plan_revision_id,position)')
        tx.connection.execute('CREATE INDEX fixture_attempts_project_created ON attempts(project_id,created_at)')
        tx.connection.execute('CREATE INDEX fixture_context_project_registered ON context_source_registrations(project_id,registered_at)')
    after=measure()
    assert all(before[k]['rows_sha256']==after[k]['rows_sha256'] for k in before)
    record('isolated_query_plan_comparison',{'schema_inventory_excluding_sqlite_internal':dict(inventory),'all_indexes_including_autoindexes':all_indexes,'fixture_projects':61,'fixture_jobs':1820,'fixture_history_rows':1801,'before':before,'after_fixture_indexes':after,'result_identity':True,'production_indexes_changed':False})

(HERE/'artifacts/probe-results.json').write_text(json.dumps(RESULT,ensure_ascii=False,indent=2)+'\n')
