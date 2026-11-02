"""Concurrency, transaction and upgrade acceptance for the M3 audit fixes."""
import json
import threading
import time
import pytest
from app import db,main,ledger
from app.models import TaskState
from tests.test_m3_safety import env,state,receipt,reconciliation,adapter,save,unknown


def test_recovery_is_async_exclusive_and_rechecks_approval(env):
    e=env;state(e,TaskState.FAILED)
    entered,release=threading.Event(),threading.Event()
    calls=adapter(e,reconciliation(e))
    kind=main.PlaywrightProcurementAdapter;original=kind.reconcile_orders
    def wait(self,plan):entered.set();assert release.wait(5);return original(self,plan)
    e.monkeypatch.setattr(kind,'reconcile_orders',wait)
    try:
        started=e.client.post(f'/tasks/{e.tid}/recover',headers={'Prefer':''})
        assert started.status_code==202 and entered.wait(3)
        status=e.client.get(f'/tasks/{e.tid}').json()
        assert status['state']=='EXECUTING' and status['operation']['busy']==1
        for action in ('recover','reconcile','execute','approve'):
            assert e.client.post(f'/tasks/{e.tid}/{action}').status_code==409
        assert e.client.post(f'/tasks/{e.tid}/revise',json={'policy':e.policy}).status_code==409
    finally:release.set()
    for _ in range(150):
        status=e.client.get(f'/tasks/{e.tid}').json()
        if not status['operation']['busy']:break
        time.sleep(.02)
    assert status['state']=='COMPLETED' and len(calls)==1
    assert e.client.get(f'/tasks/{e.tid}/audit-trail').json()['replay_complete'] is True


def test_reconciliation_is_async_and_excludes_revision(env):
    e=env;state(e,TaskState.PARTIAL);adapter(e,reconciliation(e))
    entered,release=threading.Event(),threading.Event()
    original=main.PlaywrightProcurementAdapter.reconcile_orders
    def wait(self,plan):entered.set();assert release.wait(5);return original(self,plan)
    e.monkeypatch.setattr(main.PlaywrightProcurementAdapter,'reconcile_orders',wait)
    try:
        assert e.client.post(f'/tasks/{e.tid}/reconcile',headers={'Prefer':''}).status_code==202
        assert entered.wait(3)
        assert e.client.post(f'/tasks/{e.tid}/recover').status_code==409
        assert e.client.post(f'/tasks/{e.tid}/reconcile').status_code==409
        assert e.client.post(f'/tasks/{e.tid}/revise',json={'policy':e.policy}).status_code==409
    finally:release.set()
    for _ in range(150):
        status=e.client.get(f'/tasks/{e.tid}').json()
        if not status['operation']['busy']:break
        time.sleep(.02)
    assert status['operation']['result']['reconciliation'][0]['state']=='not_submitted'


def test_receipt_event_transaction_rolls_back_both(env):
    e=env;before=db.list_execution_events(e.conn,e.tid)
    def fail(*a,**k):raise RuntimeError('receipt persistence failed')
    e.monkeypatch.setattr(db,'save_execution_record',fail)
    with pytest.raises(RuntimeError):ledger.record_observation(e.conn,e.tid,e.plan,receipt(e),'order_result',{'version':1,'plan_hash':e.h,'attempt_id':'review'})
    assert db.list_execution_events(e.conn,e.tid)==before
    assert db.list_execution_records(e.conn,e.tid)==[]


def test_submit_intent_without_result_is_never_retried(env):
    e=env;state(e,TaskState.NEEDS_RECONCILIATION)
    ref=receipt(e)['external_ref']
    db.append_execution_event(e.conn,e.tid,'submit_intent',ref,{'plan_hash':e.h,'version':1})
    calls=adapter(e,reconciliation(e))
    result=e.client.post(f'/tasks/{e.tid}/recover')
    assert result.status_code==200 and result.json()['state']=='NEEDS_RECONCILIATION'
    assert calls==[] and result.json()['skipped_unknown']==[ref]


def test_legacy_missing_receipts_are_unknown_not_confirmed_empty(env):
    e=env;db.upsert_task(e.conn,e.tid,TaskState.NEEDS_RECONCILIATION,e.h,1)
    calls=adapter(e,reconciliation(e))
    r=e.client.post(f'/tasks/{e.tid}/recover')
    assert r.status_code==200 and r.json()['state']=='NEEDS_RECONCILIATION' and calls==[]


def test_noop_after_partial_recomputes_remaining_plan(env):
    e=env;state(e,TaskState.PARTIAL);save(e,receipt(e));adapter(e,reconciliation(e,True))
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':e.policy})
    assert r.status_code==200 and r.json()['plan']['version']==2
    assert r.json()['plan']['total_cost_cents']==0
    assert db.get_plan(e.conn,e.tid,1)[1]==e.h


def test_valid_history_can_be_audited_after_a_new_approval(env):
    e=env;adapter(e,reconciliation(e))
    assert e.client.post(f'/tasks/{e.tid}/execute').status_code==200
    assert ledger.audit(e.conn,e.tid)['replay_complete'] is True
    # An explicit approved-version change must not reinterpret historic v1 hashes.
    v2=e.plan.model_copy(deep=True);v2.version=2
    h2=db.save_plan(e.conn,v2)
    db.upsert_task(e.conn,e.tid,TaskState.APPROVED,h2,2)
    db.activity(e.conn,e.tid,'approved',{'version':2,'plan_hash':h2})
    assert ledger.audit(e.conn,e.tid)['replay_complete'] is True


def test_stale_approved_v2_without_covered_deduction_cannot_execute(env):
    e=env;save(e,receipt(e))
    v2=e.plan.model_copy(deep=True);v2.version=2;h=db.save_plan(e.conn,v2)
    db.upsert_task(e.conn,e.tid,TaskState.APPROVED,h,2)
    calls=adapter(e,reconciliation(e))
    r=e.client.post(f'/tasks/{e.tid}/execute')
    assert r.status_code==409 and calls==[]


def test_recovery_with_mismatched_actual_receipt_remains_pending(env):
    e=env;state(e,TaskState.NEEDS_RECONCILIATION)
    recon=reconciliation(e,True);recon[0]['receipt']['total_cost_cents']+=1
    calls=adapter(e,recon)
    r=e.client.post(f'/tasks/{e.tid}/recover')
    assert r.status_code==200 and r.json()['state']=='NEEDS_RECONCILIATION' and calls==[]
    assert e.client.get(f'/tasks/{e.tid}/summary').json()['valid_orders']==[]


def test_lower_budget_than_existing_orders_is_rejected(env):
    e=env;state(e,TaskState.PARTIAL);save(e,receipt(e));adapter(e,reconciliation(e,True))
    r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':1200}})
    assert r.status_code==422 and db.list_plan_versions(e.conn,e.tid)==[1]
    preserved=db.get_task(e.conn,e.tid)
    assert preserved['state']=='PARTIAL' and preserved['approved_plan_hash']==e.h
    assert e.client.post(f'/tasks/{e.tid}/recover').json()['state']=='COMPLETED'


def test_nested_transactions_roll_back_without_deleting_prior_rows(env):
    e=env
    with pytest.raises(RuntimeError):
        with db.transaction(e.conn):
            db.activity(e.conn,e.tid,'must_rollback',{})
            with db.transaction(e.conn):db.update_context(e.conn,e.tid,error='must_rollback')
            raise RuntimeError('abort outer')
    assert not any(a['kind']=='must_rollback' for a in db.list_activity(e.conn,e.tid))
    assert db.get_context(e.conn,e.tid)['error'] is None
    assert db.list_plan_versions(e.conn,e.tid)==[1]


@pytest.mark.parametrize('mutation',['readback_before_submit','finish_before_lookup','missing_result','duplicate_result','missing_finish','cancelled_receipt'])
def test_audit_rejects_corruption_of_a_valid_chain(env,mutation):
    e=env;adapter(e,reconciliation(e));assert e.client.post(f'/tasks/{e.tid}/execute').status_code==200
    assert ledger.audit(e.conn,e.tid)['replay_complete'] is True
    events=db.list_execution_events(e.conn,e.tid)
    with db.transaction(e.conn):
        if mutation in ('readback_before_submit','finish_before_lookup'):
            kinds=('submit_intent','readback_completed') if mutation=='readback_before_submit' else ('lookup_started','lookup_finished')
            a,b=[next(x for x in events if x['event_type']==k) for k in kinds]
            for target,source in [(a,b),(b,a)]:
                e.conn.execute('UPDATE execution_events SET event_type=?,payload_json=? WHERE seq=?',(source['event_type'],json.dumps(source['payload']),target['seq']))
        elif mutation in ('missing_result','missing_finish'):
            kind='order_result' if mutation=='missing_result' else 'execution_finished'
            e.conn.execute('DELETE FROM execution_events WHERE task_id=? AND event_type=?',(e.tid,kind))
        elif mutation=='duplicate_result':
            result=next(x for x in events if x['event_type']=='order_result')
            db.append_execution_event(e.conn,e.tid,'order_result',result['external_ref'],result['payload'])
        else:
            result=next(x for x in events if x['event_type']=='order_result')
            result['payload']['receipt']['status']='CANCELLED'
            e.conn.execute('UPDATE execution_events SET payload_json=? WHERE seq=?',(json.dumps(result['payload']),result['seq']))
    assert ledger.audit(e.conn,e.tid)['replay_complete'] is False


@pytest.mark.parametrize('prior',['unknown','created'])
def test_legacy_later_failed_record_does_not_erase_past_submission(env,prior):
    e=env;state(e,TaskState.NEEDS_RECONCILIATION)
    if prior=='unknown':unknown(e)
    else:save(e,receipt(e))
    ref=receipt(e)['external_ref']
    save(e,{'external_ref':ref,'supplier_id':'S','status':'failed','submission_state':'not_attempted','receipt':None,'problems':[]})
    calls=adapter(e,reconciliation(e))
    r=e.client.post(f'/tasks/{e.tid}/recover')
    assert r.status_code==200 and r.json()['state']=='NEEDS_RECONCILIATION' and calls==[]


@pytest.mark.parametrize('code',[None,301,401,403,404,429,500])
def test_lookup_only_accepts_success_response(code):
    from types import SimpleNamespace
    from app.execution import PlaywrightProcurementAdapter
    page=SimpleNamespace(goto=lambda url:None if code is None else SimpleNamespace(status=code))
    with pytest.raises(RuntimeError):PlaywrightProcurementAdapter()._find_by_ref(page,'review-v1-S')


def test_lookup_requires_recognized_empty_page():
    from types import SimpleNamespace
    from app.execution import PlaywrightProcurementAdapter
    page=SimpleNamespace(goto=lambda url:SimpleNamespace(status=200),locator=lambda selector:SimpleNamespace(count=lambda:0))
    with pytest.raises(RuntimeError,match='页面结构'):PlaywrightProcurementAdapter()._find_by_ref(page,'review-v1-S')
