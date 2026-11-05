"""Durable human approval outbox. Synthetic adapter; real portal tested separately."""
import uuid
from datetime import timedelta
import pytest
from app import main, db, collection
from tests.test_step3_api import client
from tests.test_collection import clock, source, ready, batch, observe


@pytest.fixture(autouse=True)
def pause_background_worker(monkeypatch):
    from app.approval_queue import ApprovalWorker
    monkeypatch.setattr(ApprovalWorker,'start',lambda self:None)


def make_plan(client, dataset=None, sid=None):
    payload={'policy':{'budget_cents':300000,'cover_days':7,'safety_days':2}}
    if dataset:payload.update(dataset_id=dataset,source_id=sid)
    r=client.post('/tasks',json=payload);assert r.status_code==200,r.text
    return r.json()


def request(plan, **changes):
    return {'version':plan['plan']['version'],'plan_hash':plan['plan_hash'],
            'request_id':uuid.uuid4().hex,'expires_in_seconds':600,**changes}


def enqueue(client, plan, payload=None, **kwargs):
    return client.post('/tasks/'+plan['task_id']+'/approve-and-execute',json=payload or request(plan),**kwargs)


def test_approval_and_intent_are_atomic_and_idempotent(client):
    plan=make_plan(client);payload=request(plan)
    first=enqueue(client,plan,payload);assert first.status_code==202,first.text
    again=enqueue(client,plan,payload);assert again.status_code==202 and again.json()==first.json()
    status=client.get('/tasks/'+plan['task_id']).json()
    assert status['state']=='APPROVED' and status['execution_intent']['status']=='queued'
    bad=enqueue(client,plan,{**payload,'plan_hash':'0'*64});assert bad.status_code==409
    assert len(client.get('/tasks/'+plan['task_id']+'/execution-intents').json()['intents'])==1


def test_unauthorized_and_stale_approval_create_no_intent(client):
    plan=make_plan(client)
    assert enqueue(client,plan,headers={'X-Restock-Approval':''}).status_code==403
    assert enqueue(client,plan,request(plan,version=2)).status_code==409
    assert client.get('/tasks/'+plan['task_id']).json()['approved_version'] is None


def test_queue_commit_failure_rolls_back_approval(client,monkeypatch):
    plan=make_plan(client)
    def fail(*args):raise OSError('injected intent persistence failure')
    monkeypatch.setattr(db,'save_execution_intent',fail)
    with pytest.raises(OSError):enqueue(client,plan)
    state=client.get('/tasks/'+plan['task_id']).json()
    assert state['state']=='NEEDS_REVIEW' and state['approved_version'] is None


def test_dispatch_replay_executes_once_and_preserves_report(client):
    from app.approval_queue import dispatch_one
    plan=make_plan(client);r=enqueue(client,plan);assert r.status_code==202,r.text
    dispatch_one(main);dispatch_one(main);dispatch_one(main)
    state=client.get('/tasks/'+plan['task_id']).json()
    assert state['state']=='COMPLETED' and state['execution_intent']['status']=='succeeded'
    events=client.get('/tasks/'+plan['task_id']+'/audit-trail').json()
    assert events['replay_complete'],events
    assert client.get('/tasks/'+plan['task_id']+'/artifacts/1/html').status_code==200
    assert sum(e['event_type']=='execution_started' for e in db.list_execution_events(main.get_conn(),plan['task_id']))==1


def test_expired_and_revoked_intents_never_execute(client,clock):
    from app.approval_queue import dispatch_one
    plan=make_plan(client);assert enqueue(client,plan,request(plan,expires_in_seconds=1)).status_code==202
    clock[0]+=timedelta(seconds=2);dispatch_one(main)
    state=client.get('/tasks/'+plan['task_id']).json()
    assert state['state']=='NEEDS_REVIEW' and state['execution_intent']['status']=='expired'
    assert not db.list_execution_records(main.get_conn(),plan['task_id'])
    assert enqueue(client,plan).status_code==202
    assert client.post('/tasks/'+plan['task_id']+'/revoke-approval',headers={'X-Restock-Approval':''}).status_code==403
    assert client.post('/tasks/'+plan['task_id']+'/revoke-approval').status_code==200
    dispatch_one(main)
    assert not db.list_execution_events(main.get_conn(),plan['task_id'])


def test_changed_source_invalidates_queued_execution(client,clock):
    from app.approval_queue import dispatch_one
    sid=source(client);did=ready(client,clock,sid);plan=make_plan(client,did,sid)
    assert enqueue(client,plan).status_code==202
    clock[0]+=timedelta(seconds=1)
    assert observe(client,sid,batch(clock,'new-stock','3')).status_code==200
    dispatch_one(main)
    state=client.get('/tasks/'+plan['task_id']).json()
    assert state['execution_intent']['status']=='cancelled'
    assert not db.list_execution_events(main.get_conn(),plan['task_id'])


def test_restart_claim_without_attempt_requeues_but_attempt_requires_check(client):
    from app import approval_queue
    plan=make_plan(client);r=enqueue(client,plan);assert r.status_code==202
    conn=main.get_conn();intent=db.execution_intents(conn,plan['task_id'])[0]
    db.save_execution_intent(conn,{**intent,'status':'running'})
    approval_queue.recover(conn)
    assert db.execution_intents(conn,plan['task_id'])[0]['status']=='queued'
    intent=db.execution_intents(conn,plan['task_id'])[0]
    db.save_execution_intent(conn,{**intent,'status':'running'})
    db.append_execution_event(conn,plan['task_id'],'execution_started',None,{'version':1,'attempt_id':'interrupted'})
    approval_queue.recover(conn)
    assert db.execution_intents(conn,plan['task_id'])[0]['status']=='needs_reconciliation'
    assert not db.list_execution_records(conn,plan['task_id'])


def test_task_claim_collision_requeues_without_losing_approval(client,monkeypatch):
    from app.approval_queue import dispatch_one
    plan=make_plan(client);assert enqueue(client,plan).status_code==202
    conn=main.get_conn();tid=plan['task_id']
    assert db.claim_task(conn,tid,'other-step','busy-owner')
    dispatch_one(main)
    assert db.execution_intents(conn,tid)[0]['status']=='queued'
    assert not db.list_execution_events(conn,tid)
    db.release_task(conn,tid,'busy-owner');dispatch_one(main)
    assert client.get('/tasks/'+tid).json()['state']=='COMPLETED'


def test_repeated_approval_different_id_and_post_start_revoke_refused(client):
    from app.approval_queue import dispatch_one
    plan=make_plan(client);assert enqueue(client,plan).status_code==202
    assert enqueue(client,plan).status_code==409
    dispatch_one(main)
    assert client.post('/tasks/'+plan['task_id']+'/revoke-approval').status_code==409
    assert enqueue(client,plan).status_code==409


def test_partial_failure_is_not_automatically_retried(client,monkeypatch):
    from app.approval_queue import dispatch_one
    from tests.test_step3_api import _FakeAdapter
    plan=make_plan(client);assert enqueue(client,plan).status_code==202
    def failed(self,plan,**kwargs):
        orders=__import__('app.execution',fromlist=['build_supplier_orders']).build_supplier_orders(plan)
        kwargs['on_submit_intent'](orders[0])
        raise OSError('injected unknown response')
    monkeypatch.setattr(_FakeAdapter,'execute_plan',failed)
    dispatch_one(main);count=len(db.list_execution_events(main.get_conn(),plan['task_id']))
    dispatch_one(main);dispatch_one(main)
    state=client.get('/tasks/'+plan['task_id']).json()
    assert state['state']=='NEEDS_RECONCILIATION'
    assert state['execution_intent']['status']=='needs_reconciliation'
    assert len(db.list_execution_events(main.get_conn(),plan['task_id']))==count


def test_orphan_submit_intent_is_not_treated_as_unstarted(client):
    from app import approval_queue
    plan=make_plan(client);assert enqueue(client,plan).status_code==202
    conn=main.get_conn();tid=plan['task_id']
    db.append_execution_event(conn,tid,'submit_intent','orphan',{'version':1,'attempt_id':'missing-start'})
    assert client.post('/tasks/'+tid+'/execute').status_code==409
    approval_queue.recover(conn);approval_queue.dispatch_one(main)
    assert db.execution_intents(conn,tid)[0]['status']=='needs_reconciliation'
    assert not db.list_execution_records(conn,tid)


def test_pending_intent_survives_database_reopen(client):
    from app import approval_queue
    plan=make_plan(client);assert enqueue(client,plan).status_code==202
    conn=main.get_conn();dbfile=conn.execute('PRAGMA database_list').fetchone()[2]
    record=db.execution_intents(conn,plan['task_id'])[0]
    db.save_execution_intent(conn,{**record,'status':'running'})
    assert db.claim_task(conn,plan['task_id'],'批准后连续执行','pre-execution-crash')
    conn.close();main._conn=db.init_db(dbfile);db.recover_interrupted(main._conn)
    approval_queue.recover(main._conn);approval_queue.dispatch_one(main)
    state=client.get('/tasks/'+plan['task_id']).json()
    assert state['state']=='COMPLETED' and state['execution_intent']['status']=='succeeded'


@pytest.mark.parametrize('fault',[None,'missing_approval','wrong_intent_hash','wrong_intent_version'])
def test_continuous_evidence_uses_persisted_approval_not_poll_timing(tmp_path,fault):
    from tests.test_m4_acceptance_tools import valid_business
    from scripts.business_evidence import check_complete
    data=valid_business(tmp_path)
    data['continuous_execution']=True;data['execution_clicks']=0
    data['approval_v2'].update(state='EXECUTING',execution_intent={'task_id':'task-demo','version':2,
        'plan_hash':'plan2','dataset_id':'ds-data','request_id':'human-intent','status':'running'})
    data['reviser']['latest_plan']['dataset_id']='ds-data'
    data['activity'].append({'kind':'approved','payload':{'version':2,'plan_hash':'plan2',
                           'request_id':'human-intent','source':'human_workbench'}})
    if fault=='missing_approval':data['activity']=data['activity'][:-1]
    if fault=='wrong_intent_hash':data['approval_v2']['execution_intent']['plan_hash']='old'
    if fault=='wrong_intent_version':data['approval_v2']['execution_intent']['version']=1
    if fault:
        with pytest.raises(AssertionError):check_complete(data,tmp_path,'off')
    else:check_complete(data,tmp_path,'off')


def test_concurrent_consumers_claim_one_execution(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.approval_queue import dispatch_one
    from tests.test_step3_api import _FakeAdapter
    plan=make_plan(client);assert enqueue(client,plan).status_code==202
    entered=threading.Event();release=threading.Event();original=_FakeAdapter.execute_plan
    def held(self,plan,**kwargs):
        entered.set();assert release.wait(10)
        return original(self,plan,**kwargs)
    monkeypatch.setattr(_FakeAdapter,'execute_plan',held)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(dispatch_one,main)
        try:
            assert entered.wait(5)
            second=pool.submit(dispatch_one,main);third=pool.submit(dispatch_one,main)
            assert second.result(5) is False and third.result(5) is False
        finally:release.set()
        assert first.result(5) is True
    events=db.list_execution_events(main.get_conn(),plan['task_id'])
    assert sum(e['event_type']=='execution_started' for e in events)==1
    assert client.get('/tasks/'+plan['task_id']).json()['state']=='COMPLETED'
