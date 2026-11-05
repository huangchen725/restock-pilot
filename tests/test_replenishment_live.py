"""Actual M5-2 scheduling/UI/procurement, with explicit synthetic observations."""
import os,time,json
from datetime import datetime,timezone
from pathlib import Path
import httpx,pytest
from playwright.sync_api import sync_playwright,expect
from app.procurement_portal import create_portal_app
from tests.test_fault_injection import FaultInjector,_start_portal
from tests.test_m3_workbench import launch
from tests.test_collection import config,batch
from tests.test_purchase_context import context


def until(read,predicate,seconds=40):
    deadline=time.monotonic()+seconds;value=None
    while time.monotonic()<deadline:
        value=read()
        if predicate(value):return value
        time.sleep(.3)
    raise AssertionError('Timed out: '+str(value))


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='actual scheduled workbench and browser')
@pytest.mark.parametrize('invalid_initial',[False,True])
def test_workbench_observe_auto_update_one_approval_and_actual_order(tmp_path,monkeypatch,invalid_initial):
    monkeypatch.setenv('RESTOCK_COLLECTION_DIR',str(tmp_path/'csv'))
    target=FaultInjector(create_portal_app(tmp_path/'portal.db'))
    portal,app,server,thread=_start_portal(tmp_path,target);supervisor=None
    evidence={'source':'synthetic backend observations (not page collection)','mode':'off','passed':False}
    out=Path(os.getenv('RESTOCK_M5_SCHEDULE_OUTPUT',str(tmp_path/'evidence')))/('corrected' if invalid_initial else 'normal');out.mkdir(parents=True,exist_ok=True)
    try:
        supervisor,api,ui,_=launch(tmp_path,portal,ui=True)
        with httpx.Client(base_url=api,trust_env=False,timeout=35,headers={'X-Restock-Approval':'m3-local-approval-token'}) as client,sync_playwright() as pw:
            c=config();c['schedule'].update(save_every_seconds=1,check_every_seconds=1)
            r=client.post('/data-sources',json=c);assert r.status_code==200,r.text
            sid=r.json()['source_id'];evidence['source_id']=sid
            data={**batch([datetime.now(timezone.utc)]),'purchase_context':context()}
            assert client.post('/data-sources/'+sid+'/observations',json=data).status_code==200
            until(lambda:client.get('/data-sources/'+sid).json(),lambda x:x['state']['latest_dataset_id'])
            browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            try:
                page=browser.new_page(viewport={'width':1440,'height':1200},accept_downloads=True);page.set_default_timeout(20000)
                page.goto(ui)
                page.get_by_text('自动补货与周期预算',exact=True).click()
                page.get_by_role('spinbutton',name='每周期额度（元）',exact=True).fill('100')
                page.get_by_role('spinbutton',name='自动覆盖天数',exact=True).fill('7')
                page.get_by_role('spinbutton',name='自动安全天数',exact=True).fill('2')
                if invalid_initial:page.get_by_role('textbox',name='自动优先类别（逗号分隔）',exact=True).fill('不存在的类别')
                page.get_by_role('button',name='保存并开始观察',exact=True).click()
                expect(page.get_by_text('请先核对旧任务归属并勾选确认。',exact=True)).to_be_visible()
                assert client.get('/replenishment-policies').json()['policies']==[]
                page.get_by_text('我已核对旧任务归属，并确认恢复原申请仍占用原补货周期额度',exact=True).click()
                page.get_by_role('button',name='保存并开始观察',exact=True).click()
                if invalid_initial:
                    blocked=until(lambda:client.get('/replenishment-policies').json()['policies'],lambda x:len(x)==1 and x[0]['state']['status']=='blocked')[0]
                    assert client.get('/tasks').json()['tasks']==[] and target.submit_requests==0
                    page.get_by_text('纠正尚未使用的本周期策略',exact=True).click()
                    page.get_by_role('textbox',name='纠正优先类别（逗号分隔）',exact=True).fill('')
                    page.get_by_role('button',name='保存当期纠正并重新观察',exact=True).click()
                g=until(lambda:client.get('/replenishment-policies').json()['policies'],lambda x:len(x)==1 and x[0]['state']['status']=='observed')[0]
                gid=g['group_id'];tid=g['state']['task_id'];evidence.update(group_id=gid,task_id=tid,initial_policy_corrected=invalid_initial)
                if invalid_initial:assert g['period']['start']==blocked['period']['start']
                first=client.get('/tasks/'+tid).json()
                assert first['latest_plan']['items'][0]['order_qty']==48
                assert client.post('/tasks/'+tid+'/approve',json={'version':1,'plan_hash':first['latest_plan_hash']}).status_code==409
                assert target.submit_requests==0
                # Change only mode; worker recalculates the same task without a Generate click.
                expect(page.get_by_role('combobox',name='运行方式',exact=True)).to_be_visible()
                page.get_by_role('combobox',name='运行方式',exact=True).click()
                page.get_by_role('combobox',name='运行方式',exact=True).press('ArrowDown')
                page.get_by_role('combobox',name='运行方式',exact=True).press('Enter')
                expect(page.get_by_text('生成待审方案',exact=True)).to_be_visible()
                page.get_by_role('button',name='更新自动补货设置',exact=True).click()
                reviewed=until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['latest_plan']['version']==2 and not x['operation']['busy'])
                with page.expect_popup() as pending:
                    page.get_by_role('link',name='打开自动补货任务',exact=True).click()
                page=pending.value
                page.set_default_timeout(20000)
                expect(page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True)).to_be_visible()
                page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准当前方案',exact=True).click()
                until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['state']=='APPROVED')
                data2={**batch([datetime.now(timezone.utc)],'changed','10'),'purchase_context':context()}
                assert client.post('/data-sources/'+sid+'/observations',json=data2).status_code==200
                changed=until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['latest_plan']['version']==3 and not x['operation']['busy'])
                assert changed['approved_version'] is None and changed['latest_plan']['items'][0]['order_qty']==42
                assert len(client.get('/tasks').json()['tasks'])==1 and target.submit_requests==0
                expect(page.get_by_text('我已核对 v3 的数量、金额和风险提示',exact=True)).to_be_visible()
                page.get_by_text('我已核对 v3 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准并执行',exact=True).click()
                final=until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['state']=='COMPLETED' and not x['operation']['busy'])
                orders=client.get(portal+'/api/purchase-orders').json()
                assert len(orders)==1 and target.submit_requests==1
                assert orders[0]['lines'][0]['order_qty']==42 and orders[0]['total_cost_cents']==4200 and orders[0]['status']=='SUBMITTED'
                # Pause the deliberately one-second test cadence after verifying the product flow.
                settings=client.get('/replenishment-policies/'+gid).json()['config'];settings['enabled']=False
                assert client.put('/replenishment-policies/'+gid,json=settings).status_code==200
                for label,kind in [('下载补货清单 CSV','csv'),('下载完整报告 HTML','html')]:
                    expect(page.get_by_role('button',name=label,exact=True)).to_be_visible()
                    with page.expect_download() as download:page.get_by_role('button',name=label,exact=True).click()
                    download.value.save_as(out/('report.'+kind))
                assert '预算周期与已有采购' in (out/'report.html').read_text(encoding='utf-8')
                assert client.post('/tasks/'+tid+'/execute').status_code==409
                assert client.get(portal+'/api/purchase-orders').json()==orders
                assert page.locator('[data-testid="stException"]').count()==0
                evidence.update(passed=True,orders=orders,submit_requests=target.submit_requests,final=final,
                                checks=client.get('/replenishment-policies/'+gid).json()['checks'],generate_button_clicks=0,execute_button_clicks=0)
                page.screenshot(path=str(out/'completed.png'),full_page=True)
            except Exception:
                page.screenshot(path=str(out/'failure.png'),full_page=True)
                (out/'failure-page.txt').write_text(page.locator('body').inner_text(),encoding='utf-8')
                raise
            finally:browser.close()
    finally:
        (out/'business.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(5)


@pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT_E2E')!='1',reason='actual API crash/restart with durable scheduler state')
@pytest.mark.parametrize('point',['before','after'])
def test_scheduler_process_crash_reuses_task_and_committed_version(tmp_path,monkeypatch,point):
    import sys
    from start import Service,Supervisor
    from tests.test_fault_injection import _free_port
    from tests.test_replenishment_schedule import config as policy_config
    monkeypatch.setenv('RESTOCK_COLLECTION_DIR',str(tmp_path/'csv'))
    target=FaultInjector(create_portal_app(tmp_path/'portal.db'))
    portal,app,server,thread=_start_portal(tmp_path,target);supervisor=None
    port=_free_port();api=f'http://127.0.0.1:{port}';marker=tmp_path/'probe-reached'
    env={**os.environ,'RESTOCK_AGENT_MODE':'off','RESTOCK_APPROVAL_TOKEN':'m3-local-approval-token',
         'RESTOCK_DB_PATH':str(tmp_path/'main.db'),'PROCUREMENT_PORTAL_URL':portal,
         'RESTOCK_API_URL':api,'RESTOCK_TEST_CRASH_POINT':point,'RESTOCK_TEST_REACHED':str(marker),
         'RESTOCK_TEST_API_PORT':str(port),'PYTHONIOENCODING':'utf-8'}
    try:
        supervisor=Supervisor([Service('api',port,[sys.executable,'-m','tests.scheduler_probe_server'],'/health')],env,tmp_path/'logs',timeout=40)
        supervisor.start()
        with httpx.Client(base_url=api,trust_env=False,timeout=35,headers={'X-Restock-Approval':'m3-local-approval-token'}) as client:
            cfg=config();cfg['schedule'].update(save_every_seconds=1,check_every_seconds=1)
            sid=client.post('/data-sources',json=cfg).json()['source_id']
            raw={**batch([datetime.now(timezone.utc)]),'purchase_context':context()}
            assert client.post('/data-sources/'+sid+'/observations',json=raw).status_code==200
            until(lambda:client.get('/data-sources/'+sid).json(),lambda x:x['state']['latest_dataset_id'])
            c=policy_config(sid,anchor=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat())
            r=client.post('/replenishment-policies',json=c);assert r.status_code==200,r.text
            gid=r.json()['group_id']
            until(marker.exists,lambda v:v)
            g=client.get('/replenishment-policies/'+gid).json();tid=g['state']['task_id']
            assert g['state']['claim'] and client.get('/tasks/'+tid).json()['operation']['busy']
            versions=client.get('/tasks/'+tid+'/plans').json()['versions']
            assert len(versions)==(1 if point=='after' else 0)
            original_hash=versions[0]['plan_hash'] if versions else None
            old_pid=supervisor.children[0][1].pid;supervisor.stop()
            supervisor,api,_,_=launch(tmp_path,portal,api_port=port)
            assert supervisor.children[0][1].pid!=old_pid
            restored=until(lambda:client.get('/replenishment-policies/'+gid).json(),lambda x:x['state']['status']=='observed')
            row=client.get('/tasks/'+tid).json()
            assert restored['state']['task_id']==tid and not restored['state']['claim']
            assert len(client.get('/tasks').json()['tasks'])==1
            assert len(client.get('/tasks/'+tid+'/plans').json()['versions'])==1
            assert row['latest_plan']['items'][0]['order_qty']==48 and row['approved_version'] is None
            if original_hash:assert row['latest_plan_hash']==original_hash
            assert any(c['status']=='interrupted' for c in restored['checks'])
            assert target.submit_requests==0 and client.get(portal+'/api/purchase-orders').json()==[]
            out=Path(os.getenv('RESTOCK_M5_SCHEDULE_OUTPUT',str(tmp_path/'evidence')));out.mkdir(parents=True,exist_ok=True)
            (out/('restart-'+point+'.json')).write_text(json.dumps({'task_id':tid,'version':row['latest_plan']['version'],
                'same_hash':not original_hash or row['latest_plan_hash']==original_hash,'checks':restored['checks'],'actual_orders':[]},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(5)


@pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT_E2E')!='1',reason='actual scheduled purchase failure and changed-source recovery')
def test_changed_source_and_paused_scheduler_allow_original_order_recovery(tmp_path,monkeypatch):
    from tests.test_replenishment_schedule import config as policy_config
    monkeypatch.setenv('RESTOCK_COLLECTION_DIR',str(tmp_path/'csv'))
    target=FaultInjector(create_portal_app(tmp_path/'portal.db'));target.connection_fail_next=1
    portal,app,server,thread=_start_portal(tmp_path,target);supervisor=None
    def two_sku_batch(name='b1',stock='0'):
        raw={**batch([datetime.now(timezone.utc)],name,stock),'purchase_context':context()}
        raw['sales'] += [{**r,'sku':'B'} for r in raw['sales'].copy()]
        raw['inventory'].append({**raw['inventory'][0],'sku':'B','name':'商品B'})
        raw['suppliers'].append({**raw['suppliers'][0],'sku':'B','supplier_id':'T'})
        raw['row_counts']={'sales':56,'inventory':2,'suppliers':2}
        return raw
    try:
        supervisor,api,_,_=launch(tmp_path,portal)
        with httpx.Client(base_url=api,trust_env=False,timeout=40,headers={'X-Restock-Approval':'m3-local-approval-token','Prefer':'wait'}) as client:
            cfg=config();cfg['schedule'].update(save_every_seconds=1,check_every_seconds=1)
            sid=client.post('/data-sources',json=cfg).json()['source_id']
            assert client.post('/data-sources/'+sid+'/observations',json=two_sku_batch()).status_code==200
            until(lambda:client.get('/data-sources/'+sid).json(),lambda x:x['state']['latest_dataset_id'])
            c=policy_config(sid,anchor=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat())
            r=client.post('/replenishment-policies',json=c);assert r.status_code==200,r.text
            gid=r.json()['group_id']
            g=until(lambda:client.get('/replenishment-policies/'+gid).json(),lambda x:x['state']['status']=='observed')
            c=g['config'];c['mode']='review';assert client.put('/replenishment-policies/'+gid,json=c).status_code==200
            tid=g['state']['task_id']
            row=until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['latest_plan']['version']==2 and not x['operation']['busy'])
            old_hash=row['latest_plan_hash'];old_dataset=row['latest_plan']['dataset_id']
            assert client.post('/tasks/'+tid+'/approve-and-execute',json={'version':2,'plan_hash':old_hash,'request_id':'source-recovery-test','expires_in_seconds':600}).status_code==202
            failed=until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['state']=='PARTIAL' and not x['operation']['busy'])
            assert target.injected==['before_submit_form_failure'] and target.submit_requests==1
            assert failed['execution_intent']['status']=='needs_reconciliation'
            assert client.post('/data-sources/'+sid+'/observations',json=two_sku_batch('changed','10')).status_code==200
            until(lambda:client.get('/data-sources/'+sid).json(),lambda x:x['state']['latest_dataset_id']!=old_dataset)
            c.update(enabled=False,mode='observe');assert client.put('/replenishment-policies/'+gid,json=c).status_code==200
            assert len(client.get('/tasks').json()['tasks'])==1 and target.submit_requests==1
            until(lambda:client.get('/tasks/'+tid).json(),lambda x:not x['operation']['busy'])
            r=client.post('/tasks/'+tid+'/recover');assert r.status_code==200 and r.json()['state']=='COMPLETED',r.text
            final=client.get('/tasks/'+tid).json();orders=client.get(portal+'/api/purchase-orders').json()
            assert len(orders)==2 and target.submit_requests==2
            assert all(o['lines'][0]['order_qty']==48 and o['total_cost_cents']==4800 for o in orders)
            assert {o['supplier_id'] for o in orders}=={'S','T'}
            assert final['latest_plan_hash']==old_hash and final['latest_plan']['dataset_id']==old_dataset
            assert final['execution_intent']['status']=='succeeded'
            assert client.post('/tasks/'+tid+'/recover').status_code==409
            assert client.get(portal+'/api/purchase-orders').json()==orders
            out=Path(os.getenv('RESTOCK_M5_SCHEDULE_OUTPUT',str(tmp_path/'evidence')));out.mkdir(parents=True,exist_ok=True)
            (out/'changed-source-recovery.json').write_text(json.dumps({'original_hash':old_hash,'original_dataset':old_dataset,
                'orders':orders,'submit_requests':target.submit_requests,'injected':target.injected},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        if supervisor:supervisor.stop()
        server.should_exit=True;thread.join(5)
