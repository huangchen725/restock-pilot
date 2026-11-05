"""M5-2 backend scheduling and global-budget contracts; synthetic source and adapter."""
from datetime import timedelta
import uuid
import pytest
from app import main,db
from tests.test_step3_api import client
from tests.test_collection import clock,source,batch,observe,publish,auth
from tests.test_purchase_context import context


@pytest.fixture(autouse=True)
def manual_scheduler(monkeypatch):
    from app.replenishment import Worker
    monkeypatch.setattr(Worker,'start',lambda self:None)


def prepared(client,clock,stock='0'):
    sid=source(client)
    assert observe(client,sid,{**batch(clock,stock=stock),'purchase_context':context()}).status_code==200
    clock[0]+=timedelta(seconds=60);assert publish(client,sid).status_code==200
    return sid


def config(sid,**changes):
    return {'source_id':sid,'anchor':'2026-10-29T00:00:00+00:00','period_seconds':86400,
            'policy':{'budget_cents':10000,'cover_days':7,'safety_days':2,'priority_categories':[],'excluded_skus':[]},
            'relationship':'separate','mode':'observe','enabled':True,
            'legacy_task_ids':[],'legacy_tasks_reviewed':True,**changes}


def setup(client,sid):
    r=client.post('/replenishment-policies',json=config(sid),headers=auth())
    assert r.status_code==200,r.text
    return r.json()['group_id']


def tick(gid):
    from app import replenishment
    replenishment.check_group(main,gid,force=True)


def group(client,gid):return client.get('/replenishment-policies/'+gid).json()


def approve(client,tid):
    row=client.get('/tasks/'+tid).json()
    return client.post('/tasks/'+tid+'/approve',json={'version':row['latest_plan']['version'],'plan_hash':row['latest_plan_hash']})


def review(client,gid):
    c=group(client,gid)['config'];c['mode']='review'
    r=client.put('/replenishment-policies/'+gid,json=c,headers=auth());assert r.status_code==200,r.text
    tick(gid)


def test_observe_then_review_and_replays_keep_one_task(client,clock):
    sid=prepared(client,clock);gid=setup(client,sid)
    tick(gid);tid=group(client,gid)['state']['task_id']
    assert len(client.get('/tasks').json()['tasks'])==1
    assert approve(client,tid).status_code==409
    for _ in range(3):tick(gid)
    assert db.list_plan_versions(main.get_conn(),tid)==[1]
    review(client,gid)
    row=client.get('/tasks/'+tid).json()
    assert row['latest_plan']['schema_version']==3
    assert row['latest_plan']['automation_context']['budget_cents']==10000
    assert approve(client,tid).status_code==200
    assert not db.list_execution_records(main.get_conn(),tid)


def test_pause_period_and_unauthorized_settings_block_new_execution(client,clock):
    sid=prepared(client,clock)
    assert client.post('/replenishment-policies',json=config(sid),headers={'X-Restock-Approval':''}).status_code==403
    gid=setup(client,sid);tick(gid);review(client,gid);tid=group(client,gid)['state']['task_id']
    assert approve(client,tid).status_code==200
    c=group(client,gid)['config'];c['enabled']=False
    assert client.put('/replenishment-policies/'+gid,json=c,headers=auth()).status_code==200
    assert client.post('/tasks/'+tid+'/execute').status_code==409
    assert not db.list_execution_records(main.get_conn(),tid)


def test_no_demand_checks_do_not_create_empty_tasks(client,clock):
    sid=prepared(client,clock,'1000');gid=setup(client,sid)
    for _ in range(3):tick(gid)
    assert client.get('/tasks').json()['tasks']==[]
    assert group(client,gid)['state']['status']=='no_demand'


def test_source_updates_same_unexecuted_task_and_invalidates_approval(client,clock):
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid);review(client,gid)
    tid=group(client,gid)['state']['task_id'];assert approve(client,tid).status_code==200
    clock[0]+=timedelta(seconds=60)
    assert observe(client,sid,{**batch(clock,'b2','10'),'purchase_context':context()}).status_code==200
    assert publish(client,sid).status_code==200
    tick(gid)
    assert group(client,gid)['state']['task_id']==tid
    row=client.get('/tasks/'+tid).json()
    assert row['state']=='NEEDS_REVIEW' and row['approved_version'] is None
    assert row['latest_plan']['items'][0]['order_qty']==42
    assert len(client.get('/tasks').json()['tasks'])==1


@pytest.fixture
def unique_receipts(monkeypatch):
    from tests.test_step3_api import _FakeAdapter
    original=_FakeAdapter.execute_plan
    posted=[]
    def execute(self,plan,on_order_result=None,**kwargs):
        def write(result):
            result['order_id']='PO-'+plan.plan_id+'-'+result['supplier_id']
            result['receipt']['order_id']=result['order_id']
            from copy import deepcopy
            posted.append(deepcopy(result['receipt']))
            if on_order_result:on_order_result(result)
        return original(self,plan,on_order_result=write,**kwargs)
    monkeypatch.setattr(_FakeAdapter,'execute_plan',execute)
    def reconcile(self,plan):
        from app.execution import build_supplier_orders
        stored={r['external_ref']:r for r in posted}
        return [{'external_ref':o['external_ref'],'supplier_id':o['supplier_id'],
                 'state':'created' if o['external_ref'] in stored else 'not_submitted',
                 'receipt':stored.get(o['external_ref']),'order_id':stored.get(o['external_ref'],{}).get('order_id'),
                 'problems':[],'attempt_log':[]} for o in build_supplier_orders(plan)]
    monkeypatch.setattr(_FakeAdapter,'reconcile_orders',reconcile)
    return posted


def changed(client,clock,sid,name='b2',sales='8'):
    clock[0]+=timedelta(seconds=60)
    b={**batch(clock,name),'purchase_context':context()}
    for row in b['sales']:row['sold_qty']=sales
    assert observe(client,sid,b).status_code==200
    assert publish(client,sid).status_code==200


def test_all_tasks_share_period_budget_and_pending_order_coverage(client,clock,unique_receipts):
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid);review(client,gid)
    first=group(client,gid)['state']['task_id'];assert approve(client,first).status_code==200
    assert client.post('/tasks/'+first+'/execute').json()['state']=='COMPLETED'
    tick(gid)
    assert len(client.get('/tasks').json()['tasks'])==1
    changed(client,clock,sid);tick(gid)
    second=group(client,gid)['state']['task_id'];assert second!=first
    plan=client.get('/tasks/'+second).json()['latest_plan']
    assert plan['items'][0]['order_qty']==48 and plan['total_cost_cents']==4800
    assert plan['budget_remaining_cents']==10000-4800-4800
    assert plan['automation_context']['coverage']['occupied_cents']==4800
    # A separately requested task also consumes the same cycle, not fresh money.
    did=client.get('/data-sources/'+sid).json()['state']['latest_dataset_id']
    manual=client.post('/tasks',json={'source_id':sid,'dataset_id':did,'policy':config(sid)['policy']})
    assert manual.status_code==200,manual.text
    third=manual.json()['task_id'];assert approve(client,second).status_code==200 and approve(client,third).status_code==200
    assert client.post('/tasks/'+second+'/execute').json()['state']=='COMPLETED'
    assert client.post('/tasks/'+third+'/execute').status_code==409
    records=[r for t in [first,second,third] for r in db.list_execution_records(main.get_conn(),t)]
    assert len(unique_receipts)==2 and sum(r['total_cost_cents'] for r in unique_receipts)==9600
    assert len(records)>2 # repeated readbacks are audit evidence, not new purchases
    assert sum(e['event_type']=='submit_intent' for t in [first,second,third] for e in db.list_execution_events(main.get_conn(),t))==2


def test_alias_source_and_unbound_new_task_cannot_reissue_budget(client,clock):
    sid=prepared(client,clock);gid=setup(client,sid)
    alias=source(client)
    r=client.post('/replenishment-policies',json=config(alias),headers=auth());assert r.status_code==409
    r=client.post('/tasks',json={'policy':{'budget_cents':10000}});assert r.status_code==422
    assert len(client.get('/replenishment-policies').json()['policies'])==1


def test_policy_edits_do_not_reset_current_period_and_next_period_rejects_old_approval(client,clock):
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid);review(client,gid)
    tid=group(client,gid)['state']['task_id'];assert approve(client,tid).status_code==200
    c=group(client,gid)['config'];c['policy']['budget_cents']=8000
    assert client.put('/replenishment-policies/'+gid,json=c,headers=auth()).status_code==200
    assert group(client,gid)['period']['policy']['budget_cents']==10000
    tick(gid);assert approve(client,tid).status_code==200
    clock[0]+=timedelta(days=1)
    changed(client,clock,sid,'next','4');tick(gid)
    assert group(client,gid)['period']['policy']['budget_cents']==8000
    assert client.post('/tasks/'+tid+'/execute').status_code==409
    new=group(client,gid)['state']['task_id'];assert new!=tid
    assert client.get('/tasks/'+new).json()['latest_plan']['policy']['budget_cents']==8000


def test_model_failure_retries_same_task_but_never_publishes_before_success(client,clock,monkeypatch):
    sid=prepared(client,clock);gid=setup(client,sid)
    real=main._run_agent_stage
    def failure(*args):raise main.AgentStageError('injected provider timeout')
    monkeypatch.setattr(main,'_run_agent_stage',failure)
    tick(gid);tid=group(client,gid)['state']['task_id']
    assert db.list_plan_versions(main.get_conn(),tid)==[]
    from app import replenishment
    conn=main.get_conn();path=conn.execute('PRAGMA database_list').fetchone()[2]
    conn.close();main._conn=db.init_db(path);db.recover_interrupted(main._conn);replenishment.recover(main._conn)
    monkeypatch.setattr(main,'_run_agent_stage',real);tick(gid)
    assert group(client,gid)['state']['task_id']==tid and db.list_plan_versions(main._conn,tid)==[1]


def test_pause_during_model_call_cannot_publish_candidate(client,clock,monkeypatch):
    sid=prepared(client,clock);gid=setup(client,sid);real=main._run_agent_stage
    def paused(stage,ctx,request):
        result=real(stage,ctx,request)
        c=group(client,gid)['config'];c['enabled']=False
        assert client.put('/replenishment-policies/'+gid,json=c,headers=auth()).status_code==200
        return result
    monkeypatch.setattr(main,'_run_agent_stage',paused);tick(gid)
    tid=group(client,gid)['state']['task_id']
    assert not db.list_plan_versions(main.get_conn(),tid)
    assert client.get('/tasks/'+tid).json()['approved_version'] is None


def test_same_failed_input_has_bounded_provider_attempts(client,clock,monkeypatch):
    sid=prepared(client,clock);gid=setup(client,sid);calls=[]
    def failure(*args):calls.append(1);raise main.AgentStageError('unavailable')
    monkeypatch.setattr(main,'_run_agent_stage',failure)
    for _ in range(7):tick(gid)
    assert len(calls)==3 and len(client.get('/tasks').json()['tasks'])==1


def test_changed_budget_facts_during_generation_cannot_publish_stale_plan(client,clock,monkeypatch):
    sid=prepared(client,clock);gid=setup(client,sid);real=main._run_agent_stage
    def changed_scope(stage,ctx,request):
        result=real(stage,ctx,request)
        c=group(client,gid)['config'];c['policy']['cover_days']=14
        assert client.put('/replenishment-policies/'+gid,json=c,headers=auth()).status_code==200
        return result
    monkeypatch.setattr(main,'_run_agent_stage',changed_scope);tick(gid)
    tid=group(client,gid)['state']['task_id'];assert not db.list_plan_versions(main.get_conn(),tid)


def test_live_readback_disagreement_blocks_new_task_until_controlled_recovery(client,clock,unique_receipts):
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid);review(client,gid)
    tid=group(client,gid)['state']['task_id'];assert approve(client,tid).status_code==200
    assert client.post('/tasks/'+tid+'/execute').json()['state']=='COMPLETED'
    receipt=unique_receipts.pop();tick(gid)
    assert group(client,gid)['state']['status']=='blocked'
    assert client.get('/tasks/'+tid).json()['state']=='NEEDS_RECONCILIATION'
    assert len(client.get('/tasks').json()['tasks'])==1
    unique_receipts.append(receipt)
    r=client.post('/tasks/'+tid+'/recover');assert r.status_code==200 and r.json()['state']=='COMPLETED',r.text
    assert len(unique_receipts)==1


def test_review_requires_actual_observation_even_before_first_data(client,clock):
    sid=source(client);gid=setup(client,sid)
    c=group(client,gid)['config'];c['mode']='review'
    assert client.put('/replenishment-policies/'+gid,json=c,headers=auth()).status_code==409


def test_concurrent_checks_claim_one_durable_task(client,clock,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    sid=prepared(client,clock);gid=setup(client,sid);original=main._run_agent_stage
    entered=threading.Event();release=threading.Event()
    def held(*args):entered.set();assert release.wait(10);return original(*args)
    monkeypatch.setattr(main,'_run_agent_stage',held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(tick,gid)
        try:
            assert entered.wait(5);second=pool.submit(tick,gid);second.result(5)
        finally:release.set()
        first.result(5)
    assert len(client.get('/tasks').json()['tasks'])==1
    assert db.list_plan_versions(main.get_conn(),group(client,gid)['state']['task_id'])==[1]


def test_check_frequency_update_reschedules_without_resetting_period(client,clock):
    from app.collection import moment
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid)
    before=group(client,gid);clock[0]+=timedelta(seconds=10)
    schedule=client.get('/data-sources/'+sid).json()['config']['schedule'];schedule['check_every_seconds']=15
    r=client.put('/data-sources/'+sid+'/settings',json=schedule,headers=auth());assert r.status_code==200,r.text
    after=group(client,gid)
    assert moment(after['state']['next_check_at'])==clock[0]+timedelta(seconds=15)
    assert after['period']==before['period'] and after['state']['task_id']==before['state']['task_id']
    assert after['revision']==before['revision']


def test_cycle_report_explains_other_task_occupancy_and_escapes_source(client,clock,unique_receipts):
    from app.reports import export_html
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid);review(client,gid)
    tid=group(client,gid)['state']['task_id'];assert approve(client,tid).status_code==200
    assert client.post('/tasks/'+tid+'/execute').json()['state']=='COMPLETED'
    changed(client,clock,sid);tick(gid)
    tid=group(client,gid)['state']['task_id'];plan,h=db.get_plan(main.get_conn(),tid,max(db.list_plan_versions(main.get_conn(),tid)))
    html=export_html(plan,'NEEDS_REVIEW',[],[],h)
    assert '预算周期与已有采购' in html and '已有采购占用 4800 分' in html
    assert '本方案新增 4800 分' in html and '周期剩余 400 分' in html
    assert unique_receipts[0]['order_id'] in html
    altered=plan.model_copy(deep=True)
    altered.automation_context['coverage']['orders'][0]['identity'][1]='<script>bad</script>'
    safe=export_html(altered,'NEEDS_REVIEW',[],[],h)
    assert '<script>bad</script>' not in safe and '&lt;script&gt;bad&lt;/script&gt;' in safe


def test_source_status_and_model_context_describe_active_cycle(client,clock):
    from app.business_tools import compact_plan,revision_review
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid)
    assert client.get('/data-sources/'+sid).json()['automation']['replenishment'] is True
    tid=group(client,gid)['state']['task_id'];before,_=db.get_plan(main.get_conn(),tid,1)
    review(client,gid);after,_=db.get_plan(main.get_conn(),tid,2)
    context=compact_plan(after)['budget_cycle']
    assert context['occupied_cents']==0 and context['budget_cents']==10000
    assert context['period_start']==after.automation_context['period_start']
    text=revision_review(before,after)
    assert '复用当前方案' not in text and '补货周期或已核验采购记录已更新' in text
    assert '周期已有采购占用 0.00 元' in text
    c=group(client,gid)['config'];c['enabled']=False
    assert client.put('/replenishment-policies/'+gid,json=c,headers=auth()).status_code==200
    assert client.get('/data-sources/'+sid).json()['automation']['replenishment'] is False


def test_identical_fresh_observation_clears_stale_block_without_new_version(client,clock):
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid)
    tid=group(client,gid)['state']['task_id']
    clock[0]+=timedelta(seconds=700);tick(gid)
    assert group(client,gid)['state']['status']=='blocked'
    assert observe(client,sid,{**batch(clock,'fresh-same'),'purchase_context':context()}).status_code==200
    tick(gid)
    g=group(client,gid)
    assert g['state']['status']=='observed' and g['state']['error'] is None
    assert db.list_plan_versions(main.get_conn(),tid)==[1]


def test_controlled_recovery_keeps_original_cycle_after_new_data_and_pause(client,clock,monkeypatch,unique_receipts):
    from tests.test_step3_api import _FakeAdapter
    from app import replenishment
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid);review(client,gid)
    tid=group(client,gid)['state']['task_id'];row=client.get('/tasks/'+tid).json();old=row['latest_plan_hash']
    assert approve(client,tid).status_code==200
    original=_FakeAdapter.execute_plan
    def fail(*args,**kwargs):raise RuntimeError('injected before first submission')
    monkeypatch.setattr(_FakeAdapter,'execute_plan',fail)
    assert client.post('/tasks/'+tid+'/execute').status_code==500
    assert client.get('/tasks/'+tid).json()['state']=='NEEDS_RECONCILIATION'
    clock[0]+=timedelta(days=1);changed(client,clock,sid,'next-day')
    tick(gid);assert len(client.get('/tasks').json()['tasks'])==1
    c=group(client,gid)['config'];c.update(enabled=False,mode='observe')
    assert client.put('/replenishment-policies/'+gid,json=c,headers=auth()).status_code==200
    monkeypatch.setattr(_FakeAdapter,'execute_plan',original)
    r=client.post('/tasks/'+tid+'/recover');assert r.status_code==200 and r.json()['state']=='COMPLETED',r.text
    assert client.get('/tasks/'+tid).json()['latest_plan_hash']==old
    assert len(unique_receipts)==1 and unique_receipts[0]['lines'][0]['order_qty']==48
    # Actual submission happened next day; agreed accounting retains original cycle.
    g=replenishment.get_group(main.get_conn(),gid)
    lines=replenishment.local_lines(main.get_conn(),g)
    assert lines[0]['budget_at']=='2026-10-29T00:00:00+00:00'
    assert lines[0]['created_at'].startswith('2026-10-30')
    from app.period_coverage import calculate
    snap=db.get_dataset(main.get_conn(),client.get('/data-sources/'+sid).json()['state']['latest_dataset_id'])
    now=calculate(snap,lines,'2026-10-30T00:00:00+00:00','2026-10-31T00:00:00+00:00',
        target_system_id=replenishment.TARGET_SYSTEM,relationship='separate',source_as_of=clock[0].isoformat())
    assert now['occupied_cents']==0 and now['covered']=={'A':48}


def test_adopted_legacy_recovery_rejects_changed_target(client,clock,monkeypatch,unique_receipts):
    sid=prepared(client,clock);did=client.get('/data-sources/'+sid).json()['state']['latest_dataset_id']
    r=client.post('/tasks',json={'source_id':sid,'dataset_id':did,'policy':config(sid)['policy']});assert r.status_code==200
    tid=r.json()['task_id'];assert approve(client,tid).status_code==200
    assert client.post('/tasks/'+tid+'/execute').json()['state']=='COMPLETED'
    gid=setup(client,sid);unique_receipts.clear();tick(gid)
    assert client.get('/tasks/'+tid).json()['state']=='NEEDS_RECONCILIATION'
    monkeypatch.setenv('PROCUREMENT_PORTAL_URL','http://127.0.0.1:9999')
    assert client.post('/tasks/'+tid+'/recover').status_code==409
    assert not unique_receipts


def test_two_approved_tasks_cannot_execute_concurrently_against_one_budget(client,clock,monkeypatch,unique_receipts):
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_step3_api import _FakeAdapter
    import threading
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid);review(client,gid)
    first=group(client,gid)['state']['task_id'];did=client.get('/data-sources/'+sid).json()['state']['latest_dataset_id']
    r=client.post('/tasks',json={'source_id':sid,'dataset_id':did,'policy':config(sid)['policy']});assert r.status_code==200
    second=r.json()['task_id'];assert approve(client,first).status_code==200 and approve(client,second).status_code==200
    original=_FakeAdapter.execute_plan;entered=threading.Event();release=threading.Event()
    def held(self,*args,**kwargs):entered.set();assert release.wait(10);return original(self,*args,**kwargs)
    monkeypatch.setattr(_FakeAdapter,'execute_plan',held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(client.post,'/tasks/'+first+'/execute')
        try:
            assert entered.wait(5)
            blocked=client.post('/tasks/'+second+'/execute');assert blocked.status_code==409,blocked.text
        finally:release.set()
        assert pending.result(10).json()['state']=='COMPLETED'
    assert len(unique_receipts)==1 and unique_receipts[0]['total_cost_cents']==4800


def correction(client,gid,policy,**extra):
    g=group(client,gid)
    return client.post('/replenishment-policies/'+gid+'/correct-current-policy',json={
        'expected_revision':g['revision'],'period_start':g['period']['start'],'policy':policy,**extra},headers=auth())


def test_invalid_initial_policy_can_be_corrected_without_resetting_cycle(client,clock):
    sid=prepared(client,clock);c=config(sid);c['policy']['priority_categories']=['不存在的类别']
    r=client.post('/replenishment-policies',json=c,headers=auth());assert r.status_code==200
    gid=r.json()['group_id'];tick(gid);before=group(client,gid)
    assert before['state']['status']=='blocked' and client.get('/tasks').json()['tasks']==[]
    r=correction(client,gid,config(sid)['policy']);assert r.status_code==200,r.text
    after=group(client,gid);assert after['period']['start']==before['period']['start']
    assert after['period']['policy']==config(sid)['policy'] and after['config']['policy']==config(sid)['policy']
    assert after['revision']==before['revision']+1 and after['config']['mode']=='observe'
    tick(gid);tid=group(client,gid)['state']['task_id']
    assert client.get('/tasks/'+tid).json()['latest_plan']['items'][0]['order_qty']==48
    assert approve(client,tid).status_code==409


@pytest.mark.parametrize('reason',['published','occupied','unknown','stale_revision','stale_period','invalid','busy','unauthorized'])
def test_current_policy_correction_rejects_unsafe_or_stale_input(client,clock,reason):
    from tests.test_purchase_context import order
    sid=prepared(client,clock);gid=setup(client,sid)
    if reason=='published':tick(gid)
    if reason in ('occupied','unknown'):
        clock[0]+=timedelta(seconds=60)
        raw={**batch(clock,'commitment'),'purchase_context':context(order(created_date='2026-10-29',amount_cents=1200 if reason=='occupied' else None))}
        assert observe(client,sid,raw).status_code==200 and publish(client,sid).status_code==200
    if reason=='busy':
        g=group(client,gid);g.pop('checks');g.pop('period');g['state']['claim']='active-model';db.save_replenishment_group(main.get_conn(),g)
    before=group(client,gid);policy=config(sid)['policy'];policy['budget_cents']=20000
    request={'expected_revision':before['revision'],'period_start':before['period']['start'],'policy':policy}
    if reason=='stale_revision':request['expected_revision']+=1
    if reason=='stale_period':request['period_start']='2026-10-28T00:00:00+00:00'
    if reason=='invalid':request['policy']['excluded_skus']=['missing-sku']
    r=client.post('/replenishment-policies/'+gid+'/correct-current-policy',json=request,headers=auth() if reason!='unauthorized' else {'X-Restock-Approval':''})
    assert r.status_code==(403 if reason=='unauthorized' else 409),r.text
    assert group(client,gid)==before
