"""M3 acceptance derived from the 2026-11-02 independent audit reproductions."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from app import db, main
from app.models import Policy, TaskState
from app.execution import build_supplier_orders, PlaywrightProcurementAdapter
from app.validation import prepare_snapshot
from tests.test_data_contract import tables, contract
from tests.support import SyncClient

@pytest.fixture
def env(tmp_path, monkeypatch):
    main._conn = db.init_db(tmp_path/'review.db')
    main._tasks = {}
    monkeypatch.setenv('RESTOCK_AGENT_MODE','off')
    snap, errors=prepare_snapshot(*tables(), contract()); assert not errors
    ds=db.save_dataset(main._conn,snap)
    policy=Policy(budget_cents=1800,cover_days=7,safety_days=2).model_dump()
    with SyncClient(main.app) as client:
        r=client.post('/tasks',json={'dataset_id':ds,'policy':policy}); assert r.status_code==200, r.text
        tid=r.json()['task_id']; assert r.json()['plan']['items'][0]['order_qty']==18
        assert client.post(f'/tasks/{tid}/approve').status_code==200
        plan,h=db.get_plan(main._conn,tid,1)
        yield SimpleNamespace(client=client,tid=tid,plan=plan,h=h,policy=policy,conn=main._conn,monkeypatch=monkeypatch)
    main._conn.close(); main._conn=None; main._tasks={}

def state(e,s):
    db.upsert_task(e.conn,e.tid,s,e.h,1)
    # Explicit fixture: the initial browser failed before any submission.
    if s in (TaskState.PARTIAL,TaskState.NEEDS_RECONCILIATION,TaskState.FAILED) and not db.list_execution_records(e.conn,e.tid):
        o=build_supplier_orders(e.plan)[0]
        save(e,{'external_ref':o['external_ref'],'supplier_id':o['supplier_id'],'status':'failed','submission_state':'not_attempted','receipt':None,'problems':[]})

def receipt(e,status='created', business='SUBMITTED'):
    order=build_supplier_orders(e.plan)[0]
    r={**order,'order_id':'PO-REVIEW','status':business}
    return {'supplier_id':order['supplier_id'],'external_ref':order['external_ref'],'status':status,
            'order_id':r['order_id'],'receipt':r,'problems':[],'error':None}

def save(e,r):
    db.save_execution_record(e.conn,e.tid,e.tid,1,r['external_ref'],r['status'],r)

def unknown(e):
    o=build_supplier_orders(e.plan)[0]
    r={'external_ref':o['external_ref'],'supplier_id':o['supplier_id'],'status':'unknown',
       'submission_state':'unknown','receipt':None,'problems':['response lost']}
    save(e,r); return r

def adapter(e,recon,executed_status='created'):
    calls=[]
    class Adapter:
        def __init__(self,**kwargs): pass
        def reconcile_orders(self,plan): return recon
        def execute_plan(self,plan,on_order_result=None,only_refs=None,**kwargs):
            order=build_supplier_orders(plan)[0]
            if kwargs.get('on_progress'):
                kwargs['on_progress'](order,'lookup_started',{})
                kwargs['on_progress'](order,'lookup_finished',{'found':executed_status=='already_exists'})
            if executed_status!='already_exists' and kwargs.get('on_submit_intent'): kwargs['on_submit_intent'](order)
            if executed_status=='created' and kwargs.get('on_progress'): kwargs['on_progress'](order,'readback_completed',{})
            r=receipt(e,executed_status); calls.append(r)
            if on_order_result:on_order_result(r)
            return {'state':'COMPLETED','results':[r],'succeeded':1,'total':1}
    e.monkeypatch.setattr(main,'PlaywrightProcurementAdapter',Adapter)
    return calls

def reconciliation(e,found=False,problems=None):
    r=receipt(e)
    return [{'external_ref':r['external_ref'],'supplier_id':r['supplier_id'],
             'state':'created' if found else 'not_submitted','order_id':r['order_id'] if found else None,
             'receipt':r['receipt'] if found else None,'problems':problems or [],'attempt_log':[]}]

def test_P0_1_historical_unknown_cannot_be_revised_after_one_empty_lookup(env):
    e=env; state(e,TaskState.NEEDS_RECONCILIATION); unknown(e); adapter(e,reconciliation(e))
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':4800}})
    assert r.status_code==409, {'http':r.status_code,'versions':db.list_plan_versions(e.conn,e.tid),'state':db.get_task(e.conn,e.tid)['state']}

def test_P0_3_history_unknown_is_not_resubmitted_by_recover(env):
    e=env; state(e,TaskState.NEEDS_RECONCILIATION); unknown(e); calls=adapter(e,reconciliation(e))
    r=e.client.post(f'/tasks/{e.tid}/recover')
    assert r.status_code==200 and not calls and r.json()['skipped_unknown']

def test_P1_3_resolved_unknown_can_complete(env):
    e=env; state(e,TaskState.NEEDS_RECONCILIATION); unknown(e); adapter(e,reconciliation(e,True))
    r=e.client.post(f'/tasks/{e.tid}/recover')
    assert r.status_code==200 and r.json()['state']=='COMPLETED', {'http':r.status_code,'body':r.json()}

@pytest.mark.parametrize('initial',[TaskState.PARTIAL,TaskState.NEEDS_RECONCILIATION,TaskState.FAILED])
def test_P1_3_4_successful_recover_reaches_completed(env,initial):
    e=env; state(e,initial); calls=adapter(e,reconciliation(e))
    r=e.client.post(f'/tasks/{e.tid}/recover')
    assert r.status_code==200 and r.json()['state']=='COMPLETED', {'http':r.status_code,'body':r.json(),'writes':len(calls),'records':len(db.list_execution_records(e.conn,e.tid))}

@pytest.mark.parametrize('kind',['recovered_after_error','already_exists'])
def test_P0_4_recovery_alternative_success_status_can_complete(env,kind):
    e=env; state(e,TaskState.PARTIAL); adapter(e,reconciliation(e),kind)
    r=e.client.post(f'/tasks/{e.tid}/recover')
    assert r.status_code==200 and r.json()['state']=='COMPLETED', {'http':r.status_code,'state':r.json().get('state')}

@pytest.mark.parametrize('kind',['created','recovered_after_error','already_exists'])
def test_P0_2_4_covered_and_dedup_and_cumulative_budget(env,kind):
    e=env; state(e,TaskState.PARTIAL)
    save(e,receipt(e,kind)); save(e,receipt(e,kind))
    from app import ledger
    assert ledger.coverage(e.conn,e.tid,e.plan.dataset_hash)==({'A':18},1800)
    adapter(e,reconciliation(e,True))
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':4800}})
    assert r.status_code==200, r.text
    p=r.json()['plan']; assert p['items'][0]['gap']==30 and p['items'][0]['order_qty']==30 and p['total_cost_cents']==3000

def test_P0_2_reconcile_persists_actual_order_for_revision(env):
    e=env; state(e,TaskState.NEEDS_RECONCILIATION); unknown(e); adapter(e,reconciliation(e,True))
    assert e.client.post(f'/tasks/{e.tid}/reconcile').status_code==200
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':4800}})
    assert r.status_code==200 and r.json()['plan']['items'][0]['order_qty']==30

def test_P1_5_cancelled_receipt_is_not_valid_coverage(env):
    e=env; state(e,TaskState.PARTIAL); save(e,receipt(e,business='CANCELLED'))
    observations=reconciliation(e,True);observations[0]['receipt']['status']='CANCELLED';adapter(e,observations)
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':4800}})
    assert r.status_code==409 or (r.status_code==200 and r.json()['plan']['items'][0]['order_qty']==48), {'http':r.status_code,'qty':r.json().get('plan',{}).get('items',[{}])[0].get('order_qty')}

def test_P1_10_dataset_change_after_purchase_rejected(env):
    e=env; state(e,TaskState.PARTIAL); save(e,receipt(e))
    s,i,p=tables(); i[0]['available_qty']='1'
    snap,errors=prepare_snapshot(s,i,p,contract()); assert not errors
    ds=db.save_dataset(e.conn,snap)
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'dataset_id':ds,'policy':{**e.policy,'budget_cents':4800}})
    assert r.status_code==409, {'http':r.status_code,'new_qty':r.json().get('plan',{}).get('items',[{}])[0].get('order_qty'),'already_purchased':18,'total_budget':4800}

def test_P1_11_failed_noop_revision_keeps_published_old_version(env):
    e=env
    def fail(*args,**kwargs): raise RuntimeError('injected activity write failure')
    e.monkeypatch.setattr(main,'_agent_event',fail)
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':e.policy})
    assert r.status_code==500
    assert db.list_plan_versions(e.conn,e.tid)==[1], 'old published version was deleted by rollback_candidate'

def test_P1_11_result_persistence_failure_cannot_leave_approvable_new_plan(env):
    e=env; original=db.update_context
    def fail(conn,tid,**fields):
        if 'result_json' in fields: raise RuntimeError('injected result write failure')
        return original(conn,tid,**fields)
    e.monkeypatch.setattr(db,'update_context',fail)
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':3000}})
    assert r.status_code==500
    # The original v1 can be reviewed again; the failed v2 must not exist or be approved.
    approval=e.client.post(f'/tasks/{e.tid}/approve',json={'version':2,'plan_hash':'0'*64})
    assert db.list_plan_versions(e.conn,e.tid)==[1] and approval.status_code==409
    assert e.client.post(f'/tasks/{e.tid}/approve').status_code==200

def event(e,kind,payload,ref=None): db.append_execution_event(e.conn,e.tid,kind,ref,payload)

def audit(e): return e.client.get(f'/tasks/{e.tid}/audit-trail').json()['replay_complete']

def test_P1_6_finish_only_is_incomplete(env):
    event(env,'execution_finished',{'plan_hash':env.h,'state':'COMPLETED'})
    assert audit(env) is False

@pytest.mark.parametrize('case',['missing_hash','wrong_finish_hash','extra_finish','orphan_result','forged_plan_hash'])
def test_P1_6_7_incomplete_event_chain_is_rejected(env,case):
    e=env;adapter(e,reconciliation(e))
    assert e.client.post(f'/tasks/{e.tid}/execute').status_code==200
    assert audit(e) is True
    events=db.list_execution_events(e.conn,e.tid)
    with db.transaction(e.conn):
        if case in ('missing_hash','wrong_finish_hash','forged_plan_hash'):
            targets=[events[0]] if case=='missing_hash' else ([events[-1]] if case=='wrong_finish_hash' else events)
            for item in targets:
                payload=item['payload'].copy()
                if case=='missing_hash':payload.pop('plan_hash')
                else:payload['plan_hash']='f'*64
                if case=='wrong_finish_hash':payload['version']=99
                e.conn.execute('UPDATE execution_events SET payload_json=? WHERE seq=?',(json.dumps(payload),item['seq']))
        else:
            if case=='orphan_result':event(e,'order_result',events[-2]['payload'],'foreign-ref')
            event(e,'execution_finished',events[-1]['payload'])
    assert audit(e) is False


def test_P1_2_recovery_restart_preserves_approval_and_requires_reconciliation(env):
    e=env; state(e,TaskState.FAILED); db.claim_task(e.conn,e.tid,'受控恢复')
    event(e,'execution_started',{'version':1,'plan_hash':e.h,'recovered':True,'orders':[]})
    db.recover_interrupted(e.conn)
    row=db.get_task(e.conn,e.tid)
    assert row['state']=='NEEDS_RECONCILIATION' and row['approved_plan_hash']==e.h, dict(row)

def test_P1_9_summary_drops_resolved_historical_pending(env):
    e=env; unknown(e); save(e,receipt(e))
    summary=e.client.get(f'/tasks/{e.tid}/summary').json()
    assert len(summary['valid_orders'])==1 and summary['pending_items']==[], summary

def test_P1_8_order_result_persistence_error_is_propagated(env):
    e=env
    import playwright.sync_api
    ctx=MagicMock(); e.monkeypatch.setattr(playwright.sync_api,'sync_playwright',lambda:ctx)
    a=PlaywrightProcurementAdapter()
    e.monkeypatch.setattr(a,'_execute_order',lambda *args,**kwargs:receipt(e))
    def fail(result): raise RuntimeError('injected order ledger failure')
    with pytest.raises(RuntimeError,match='injected order ledger failure'):
        a.execute_plan(e.plan,on_order_result=fail)
