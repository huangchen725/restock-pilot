"""Actual workbench recovery and API-process crash/restart; local demo systems only."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import httpx
import pytest
from playwright.sync_api import sync_playwright,expect
from start import Service,Supervisor
from tests.test_data_contract import tables,contract,write_dataset
from tests.test_fault_injection import FaultInjector,_start_portal,_free_port
from tests.test_m3_live import DeferredFirstPost
from app.procurement_portal import create_portal_app

ROOT=Path(__file__).resolve().parents[1]


def launch(tmp_path,portal,ui=False,api_port=None):
    port=api_port or _free_port();api=f'http://127.0.0.1:{port}'
    env={**os.environ,'RESTOCK_AGENT_MODE':'off','RESTOCK_APPROVAL_TOKEN':'m3-local-approval-token',
         'RESTOCK_DB_PATH':str(tmp_path/'main.db'),'PROCUREMENT_PORTAL_URL':portal,
         'RESTOCK_API_URL':api,'PROCUREMENT_TIMEOUT_MS':'1200','PYTHONIOENCODING':'utf-8'}
    specs=[Service('api',port,[sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],'/health')]
    ui_url=None
    if ui:
        ui_port=_free_port();ui_url=f'http://127.0.0.1:{ui_port}'
        specs.append(Service('ui',ui_port,[sys.executable,'-m','streamlit','run',str(ROOT/'ui/app.py'),
            '--server.address','127.0.0.1','--server.port',str(ui_port),'--server.headless','true','--browser.gatherUsageStats','false'],'/_stcore/health'))
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=40)
    supervisor.start()
    return supervisor,api,ui_url,port


def create_approved(client,api,path,two=False):
    data=tables()
    if two:
        data[0].extend([{**r,'sku':'B'} for r in data[0].copy()])
        data[1].append({**data[1][0],'sku':'B','name':'商品B'})
        data[2].append({**data[2][0],'sku':'B','supplier_id':'T'})
    write_dataset(path,data)
    with ExitStack() as stack:
        files={name:(name+'.csv',stack.enter_context((path/(name+'.csv')).open('rb')),'text/csv') for name in ('sales','inventory','suppliers')}
        ds=client.post(api+'/datasets',files=files,data={'contract':contract().model_dump_json()})
    assert ds.status_code==200,ds.text
    r=client.post(api+'/tasks',json={'dataset_id':ds.json()['dataset_id'],'policy':{'budget_cents':10000,'cover_days':7,'safety_days':2}})
    assert r.status_code==200,r.text
    task=r.json()['task_id'];plan=r.json()['plan']
    assert plan['items'][0]['order_qty']==48
    approved=client.post(api+f'/tasks/{task}/approve',json={'version':1,'plan_hash':r.json()['plan_hash']})
    assert approved.status_code==200,approved.text
    return task,r.json()['plan_hash']


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='opt-in real M3 workbench')
def test_workbench_partial_reconcile_recover_refresh_export(tmp_path):
    injector=FaultInjector(create_portal_app(tmp_path/'portal.db'));injector.connection_fail_next=1
    portal,app,server,thread=_start_portal(tmp_path,injector)
    supervisor=None
    try:
        supervisor,api,ui,_=launch(tmp_path,portal,ui=True)
        with httpx.Client(trust_env=False,timeout=40,headers={'Prefer':'wait','X-Restock-Approval':'m3-local-approval-token'}) as client:
            task,_=create_approved(client,api,tmp_path/'data',two=True)
            first=client.post(api+f'/tasks/{task}/execute')
            assert first.status_code==200 and first.json()['state']=='PARTIAL',first.text
            assert injector.injected==['before_submit_form_failure'] and injector.submit_requests==1
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
                try:
                    page=browser.new_page(accept_downloads=True,viewport={'width':1440,'height':1200});page.set_default_timeout(20000)
                    page.goto(ui+'/?task='+task)
                    expect(page.get_by_role('button',name='核对申请状态（受控核对）')).to_be_visible()
                    page.get_by_role('button',name='核对申请状态（受控核对）').click()
                    expect(page.get_by_role('heading',name='最新核对结果')).to_be_visible(timeout=20000)
                    expect(page.get_by_text('T：确定未提交',exact=True)).to_be_visible()
                    page.get_by_text('确认仅恢复已确定未提交的申请',exact=True).click()
                    page.get_by_role('button',name='恢复未完成的申请（受控恢复）').click()
                    expect(page.get_by_text('申请已创建，回读核对一致。',exact=True)).to_have_count(2,timeout=30000)
                    page.reload()
                    expect(page.get_by_text('申请已创建，回读核对一致。',exact=True)).to_have_count(2)
                    expect(page.get_by_text('未完成事项：',exact=True)).to_have_count(0)
                    with page.expect_download() as pending:page.get_by_role('button',name='下载完整报告 HTML').click()
                    target=tmp_path/'m3-report.html';pending.value.save_as(target)
                    orders=client.get(portal+'/api/purchase-orders').json()
                    assert len(orders)==2 and injector.submit_requests==2
                    assert sorted((o['supplier_id'],o['lines'][0]['sku'],o['lines'][0]['order_qty'],o['total_cost_cents'],o['status']) for o in orders)==[('S','A',48,4800,'SUBMITTED'),('T','B',48,4800,'SUBMITTED')]
                    text=target.read_text(encoding='utf-8');assert all(o['order_id'] in text for o in orders)
                    summary=client.get(api+f'/tasks/{task}/summary').json()
                    assert summary['state']=='COMPLETED' and summary['pending_items']==[] and summary['total_created_cents']==9600
                    assert client.get(api+f'/tasks/{task}/audit-trail').json()['replay_complete'] is True
                    assert client.post(api+f'/tasks/{task}/recover').status_code==409
                    assert len(client.get(portal+'/api/purchase-orders').json())==2
                    out=ROOT/'outputs'/'m3-remediation';out.mkdir(exist_ok=True,parents=True)
                    page.screenshot(path=str(out/'workbench-recovered.png'),full_page=True)
                    (out/'workbench.json').write_text(json.dumps({'summary':summary,'orders':orders},ensure_ascii=False,indent=2),encoding='utf-8')
                    assert page.locator('[data-testid="stException"]').count()==0
                finally:browser.close()
    finally:
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(timeout=5)


@pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT_E2E')!='1',reason='opt-in process crash/restart')
def test_real_api_process_restart_never_reissues_pending_submission(tmp_path):
    injector=DeferredFirstPost(create_portal_app(tmp_path/'portal.db'))
    injector.response_gate=threading.Event()
    portal,app,server,thread=_start_portal(tmp_path,injector)
    supervisor=None
    try:
        supervisor,api,_,port=launch(tmp_path,portal)
        with httpx.Client(trust_env=False,timeout=40,headers={'Prefer':'wait','X-Restock-Approval':'m3-local-approval-token'}) as client:
            task,h=create_approved(client,api,tmp_path/'data')
            assert client.post(api+f'/tasks/{task}/execute',headers={'Prefer':''}).status_code==202
            assert injector.accepted.wait(8)
            before=client.get(api+f'/tasks/{task}').json()
            assert before['state']=='EXECUTING' and before['operation']['busy']==1
            # Supervisor terminates only identities and descendants it started;
            # on Windows this is abrupt process termination, not application shutdown.
            old_pid=supervisor.children[0][1].pid
            supervisor.stop()
            assert supervisor.children[0][1].poll() is not None
            supervisor,api,_,_=launch(tmp_path,portal,api_port=port)
            restored=client.get(api+f'/tasks/{task}').json()
            assert restored['state']=='NEEDS_RECONCILIATION' and restored['approved_plan_hash']==h
            assert restored['operation']['busy']==0
            pending=client.post(api+f'/tasks/{task}/recover')
            assert pending.status_code==200 and pending.json()['state']=='NEEDS_RECONCILIATION'
            assert injector.posts==1 and app.state.store.list()==[]
            injector.release.set();assert injector.committed.wait(5) and injector.error is None
            injector.response_gate.set()
            recovered=client.post(api+f'/tasks/{task}/recover')
            assert recovered.status_code==200 and recovered.json()['state']=='COMPLETED',recovered.text
            orders=client.get(portal+'/api/purchase-orders').json()
            assert len(orders)==1 and orders[0]['lines'][0]['order_qty']==48 and orders[0]['total_cost_cents']==4800 and injector.posts==1
            assert client.get(api+f'/tasks/{task}/summary').json()['pending_items']==[]
            audit=client.get(api+f'/tasks/{task}/audit-trail').json()
            assert audit['replay_complete'] is False and audit['issues'] # Never invent lost pre-crash results.
            out=ROOT/'outputs'/'m3-remediation';out.mkdir(exist_ok=True,parents=True)
            (out/'restart.json').write_text(json.dumps({'old_pid':old_pid,'new_pid':supervisor.children[0][1].pid,'restored':restored['state'],'final':recovered.json()['state'],'orders':orders,'audit_issues':audit['issues'],'submission_count':injector.posts},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        injector.release.set();injector.response_gate.set();injector.committed.wait(5)
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(timeout=5)


@pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT_E2E')!='1',reason='opt-in cross-version actual orders')
def test_partial_revision_executes_only_remaining_demand_and_budget(tmp_path):
    injector=FaultInjector(create_portal_app(tmp_path/'portal.db'));injector.connection_fail_next=1
    portal,app,server,thread=_start_portal(tmp_path,injector);supervisor=None
    try:
        supervisor,api,_,_=launch(tmp_path,portal)
        with httpx.Client(trust_env=False,timeout=40,headers={'Prefer':'wait','X-Restock-Approval':'m3-local-approval-token'}) as client:
            task,h=create_approved(client,api,tmp_path/'data',two=True)
            first=client.post(api+f'/tasks/{task}/execute')
            assert first.status_code==200 and first.json()['state']=='PARTIAL'
            revised=client.post(api+f'/tasks/{task}/revise',json={'policy':{'budget_cents':8000,'cover_days':7,'safety_days':2,'priority_categories':[],'excluded_skus':[]}})
            assert revised.status_code==200,revised.text
            p=revised.json()['plan'];assert p['version']==2
            assert {r['sku']:r['order_qty'] for r in p['items']}=={'A':0,'B':30}
            assert p['total_cost_cents']==3000 and p['budget_remaining_cents']==200
            assert client.post(api+f'/tasks/{task}/execute').status_code==409
            assert client.post(api+f'/tasks/{task}/approve',json={'version':1,'plan_hash':h}).status_code==409
            assert client.post(api+f'/tasks/{task}/approve',json={'version':2,'plan_hash':revised.json()['plan_hash']}).status_code==200
            r=client.post(api+f'/tasks/{task}/execute');assert r.status_code==200 and r.json()['state']=='COMPLETED',r.text
            orders=client.get(portal+'/api/purchase-orders').json()
            assert len(orders)==2 and injector.submit_requests==2
            assert sorted((o['external_ref'],o['lines'][0]['order_qty'],o['total_cost_cents']) for o in orders)==[(f'{task}-v1-S',48,4800),(f'{task}-v2-T',30,3000)]
            summary=client.get(api+f'/tasks/{task}/summary').json()
            assert summary['total_created_cents']==7800 and summary['pending_items']==[]
            assert client.get(api+f'/tasks/{task}/audit-trail').json()['replay_complete'] is True
            report=client.get(api+f'/tasks/{task}/artifacts/2/html').text
            assert all(o['order_id'] in report for o in orders)
            assert client.post(api+f'/tasks/{task}/execute').status_code==409
            out=ROOT/'outputs'/'m3-remediation';out.mkdir(exist_ok=True,parents=True)
            (out/'cross-version.json').write_text(json.dumps({'orders':orders,'summary':summary},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(timeout=5)
