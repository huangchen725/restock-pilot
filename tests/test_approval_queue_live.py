"""Actual UI/Chromium and crashed API evidence for one human approval."""
import os,time,uuid,json,threading
from pathlib import Path
import httpx
import pytest
from tests.workbench_flow import run_workbench_flow
from tests.test_fault_injection import _start_portal,FaultInjector
from tests.test_m3_live import DeferredFirstPost
from tests.test_m3_workbench import launch,create_approved
from app.procurement_portal import create_portal_app


def out(tmp_path):
    path=Path(os.getenv('RESTOCK_M5_QUEUE_OUTPUT',str(tmp_path/'evidence')))
    path.mkdir(parents=True,exist_ok=True);return path


def wait_finished(client,api,task):
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        state=client.get(api+'/tasks/'+task).json()
        if state['execution_intent']['status'] not in ('queued','running') and not state['operation']['busy']:return state
        time.sleep(.2)
    raise AssertionError('approved task did not finish: '+str(state))


def queue(client,api,task,h):
    assert client.post(api+'/tasks/'+task+'/revoke-approval').status_code==200
    payload={'version':1,'plan_hash':h,'request_id':uuid.uuid4().hex,'expires_in_seconds':600}
    a=client.post(api+'/tasks/'+task+'/approve-and-execute',json=payload)
    b=client.post(api+'/tasks/'+task+'/approve-and-execute',json=payload)
    assert a.status_code==b.status_code==202 and a.json()==b.json()
    return payload


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='explicit actual offline workbench')
def test_workbench_one_approval_executes_and_exports_30_sku(tmp_path):
    run_workbench_flow(tmp_path,'off',out(tmp_path),continuous=True)


@pytest.mark.skipif(os.getenv('RUN_REAL_MODEL_E2E')!='1',reason='explicit real InternLM workbench')
def test_required_model_workbench_one_approval(tmp_path):
    run_workbench_flow(tmp_path,'required',out(tmp_path),continuous=True)


@pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT_E2E')!='1',reason='actual partial supplier failure')
def test_queue_partial_result_stops_and_controlled_recovery_does_not_rebuy(tmp_path):
    injector=FaultInjector(create_portal_app(tmp_path/'portal.db'));injector.connection_fail_next=1
    portal,app,server,thread=_start_portal(tmp_path,injector);supervisor=None
    try:
        supervisor,api,_,_=launch(tmp_path,portal)
        with httpx.Client(trust_env=False,timeout=40,headers={'Prefer':'wait','X-Restock-Approval':'m3-local-approval-token'}) as client:
            task,h=create_approved(client,api,tmp_path/'data',two=True);queue(client,api,task,h)
            state=wait_finished(client,api,task)
            assert state['state']=='PARTIAL' and state['execution_intent']['status']=='needs_reconciliation'
            assert injector.injected==['before_submit_form_failure'] and injector.submit_requests==1
            first=client.get(portal+'/api/purchase-orders').json();assert len(first)==1
            time.sleep(2.5);assert injector.submit_requests==1
            r=client.post(api+'/tasks/'+task+'/recover');assert r.status_code==200 and r.json()['state']=='COMPLETED',r.text
            orders=client.get(portal+'/api/purchase-orders').json()
            assert len(orders)==2 and injector.submit_requests==2
            assert sum(x['total_cost_cents'] for x in orders)==9600
            assert sum(l['order_qty'] for x in orders for l in x['lines'])==96
            assert client.get(api+'/tasks/'+task).json()['execution_intent']['status']=='succeeded'
            (out(tmp_path)/'partial.json').write_text(json.dumps({'first':first,'final':orders,'post_count':injector.submit_requests},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(5)


@pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT_E2E')!='1',reason='actual pending submission + API crash/restart')
def test_queued_execution_crash_keeps_unknown_and_never_reposts(tmp_path):
    injector=DeferredFirstPost(create_portal_app(tmp_path/'portal.db'));injector.response_gate=threading.Event()
    portal,app,server,thread=_start_portal(tmp_path,injector);supervisor=None
    try:
        supervisor,api,_,port=launch(tmp_path,portal)
        with httpx.Client(trust_env=False,timeout=40,headers={'Prefer':'wait','X-Restock-Approval':'m3-local-approval-token'}) as client:
            task,h=create_approved(client,api,tmp_path/'data');payload=queue(client,api,task,h)
            assert injector.accepted.wait(12)
            original=client.get(api+'/tasks/'+task).json();assert original['state']=='EXECUTING'
            supervisor.stop();supervisor,api,_,_=launch(tmp_path,portal,api_port=port)
            restored=client.get(api+'/tasks/'+task).json()
            assert restored['state']=='NEEDS_RECONCILIATION'
            assert restored['execution_intent']['status']=='needs_reconciliation'
            assert client.post(api+'/tasks/'+task+'/approve-and-execute',json=payload).status_code==202
            time.sleep(2.5);assert injector.posts==1 and app.state.store.list()==[]
            injector.release.set();assert injector.committed.wait(5) and injector.error is None
            injector.response_gate.set()
            recovered=client.post(api+'/tasks/'+task+'/recover')
            assert recovered.status_code==200 and recovered.json()['state']=='COMPLETED',recovered.text
            orders=client.get(portal+'/api/purchase-orders').json()
            assert len(orders)==1 and injector.posts==1
            assert orders[0]['lines'][0]['order_qty']==48 and orders[0]['total_cost_cents']==4800
            audit=client.get(api+'/tasks/'+task+'/audit-trail').json();assert not audit['replay_complete'] and audit['issues']
            (out(tmp_path)/'crash.json').write_text(json.dumps({'restored':restored['state'],'orders':orders,'post_count':injector.posts,'audit_issues':audit['issues']},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        injector.release.set();injector.response_gate.set();injector.committed.wait(5)
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(5)
