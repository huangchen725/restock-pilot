"""Ordinary-user managed demo page -> actual InternLM -> one approval -> real demo orders."""
import csv,hashlib,io,json,os,secrets,socket,time,traceback
from pathlib import Path
from types import SimpleNamespace
import httpx,psutil,pytest
from playwright.sync_api import sync_playwright,expect
from start import Service,Supervisor,services
from app.models import DatasetSnapshot
from app.redaction import redact_value
from app.merchant_test_portal import build_portal_data
from tests.test_browser_collection import assert_snapshot_matches_portal
from tests.m5_end_to_end import independent_plan
from tests.test_usability_ui import free_port
from tests.summary_assertions import assert_planner_summaries
from scripts.business_evidence import check_exports

@pytest.mark.skipif(os.getenv('RUN_REAL_MODEL_E2E')!='1',reason='explicit actual InternLM onboarding')
def test_required_model_first_use_managed_page_to_approved_orders(tmp_path):
    assert os.getenv('INTERN_API_KEY'),'Real model credential required'
    out=Path(os.getenv('RESTOCK_UX_OUTPUT',str(tmp_path/'evidence')));out.mkdir(parents=True,exist_ok=True)
    ports={k:free_port() for k in ('proxy','portal','api','ui','demo')}
    urls={k:f'http://127.0.0.1:{v}' for k,v in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':'required','RESTOCK_API_URL':urls['api'],
        'RESTOCK_LITELLM_URL':urls['proxy'],'PROCUREMENT_PORTAL_URL':urls['portal'],
        'RESTOCK_DB_PATH':str(tmp_path/'business.db'),'PROCUREMENT_DB_PATH':str(tmp_path/'orders.db'),
        'RESTOCK_COLLECTION_DIR':str(tmp_path/'collections'),'RESTOCK_DEMO_PORT':str(ports['demo']),
        'RESTOCK_DEMO_LOG_DIR':str(tmp_path/'demo-logs'),'RESTOCK_APPROVAL_TOKEN':secrets.token_urlsafe(32),
        'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','RESTOCK_AGENT_TIMEOUT_S':'600'}
    specs=[]
    for spec in services(False):
        cmd=list(spec.command);flag='--server.port' if spec.name=='ui' else '--port'
        cmd[cmd.index(flag)+1]=str(ports[spec.name]);specs.append(Service(spec.name,ports[spec.name],cmd,spec.health_path))
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=60)
    evidence={'passed':False,'model':'intern-s2','scope':'synthetic merchant page, real model, demonstration procurement only',
        'upload_actions':0,'manual_collect_clicks':0,'generate_clicks':0,'approval_clicks':0,'stages':{}}
    sid=tid=None;page=None
    def save():
        (out/'business.json').write_text(json.dumps(redact_value(evidence,env),ensure_ascii=False,indent=2),encoding='utf-8')
    def wait(read,ready,stage,limit=660):
        evidence['phase']=stage;save();started=time.monotonic()
        while time.monotonic()-started<limit:
            row=read()
            if ready(row):
                evidence['stages'][stage]=round(time.monotonic()-started,2);save();return row
            time.sleep(.5)
        raise AssertionError('Timeout: '+stage)
    try:
        supervisor.start()
        with httpx.Client(base_url=urls['api'],trust_env=False,timeout=20) as client,sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            try:
                page=browser.new_page(viewport={'width':1440,'height':1200},accept_downloads=True);page.set_default_timeout(30000)
                page.goto(urls['ui']);page.get_by_text('自动采集网页',exact=True).click()
                page.get_by_role('button',name='启动测试页面',exact=True).click()
                expect(page.get_by_role('link',name='打开测试商户后台',exact=True)).to_be_visible()
                evidence['demo']=client.get('/demo-site').json();assert evidence['demo']['running']
                page.get_by_role('button',name='连接测试页面并开始读取',exact=True).click()
                expect(page.get_by_text('请先确认本次读取的演示门店和商品范围。',exact=True)).to_be_visible()
                assert client.get('/data-sources').json()['sources']==[]
                page.get_by_text('我已确认使用这家演示门店和上述 30 种商品',exact=True).click()
                page.get_by_role('button',name='连接测试页面并开始读取',exact=True).click()
                rows=wait(lambda:client.get('/data-sources').json()['sources'],lambda rows:len(rows)==1,'connect',20)
                sid=rows[0]['source_id'];evidence['source_id']=sid
                assert rows[0]['config']['publish_first_immediately'] is True
                assert rows[0]['config']['schedule']['save_every_seconds']==3600
                def source():
                    row=client.get('/data-sources/'+sid).json()
                    assert row['state'].get('collection_job',{}).get('status') not in ('failed','interrupted'),row['state']
                    return row
                row=wait(source,lambda r:r['data_status']['ready'] and r['state'].get('collection_job',{}).get('status')=='succeeded','collection-and-first-publication')
                evidence['source']=row;did=row['state']['latest_dataset_id'];prefix=f'/data-sources/{sid}/publications/{did}/'
                manifest=client.get(prefix+'manifest.json').json();tables={}
                for name in ('sales','inventory','suppliers'):
                    body=client.get(prefix+name+'.csv').content
                    assert hashlib.sha256(body).hexdigest()==manifest['files'][name+'.csv']['sha256']
                    (out/(name+'.csv')).write_bytes(body);tables[name]=list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
                snapshot=DatasetSnapshot.model_validate({**tables,**{k:manifest[k] for k in ('contract','window_start','window_end','purchase_context')}})
                oracle=SimpleNamespace(data=build_portal_data(batch=1))
                assert_snapshot_matches_portal(snapshot,oracle,set(evidence['demo']['profile']['sku_scope']))
                assert {k:len(v) for k,v in tables.items()}=={'sales':840,'inventory':30,'suppliers':30}
                evidence['snapshot_matches_oracle']=True;evidence['manifest']=manifest
                expect(page.get_by_role('button',name='用这份资料继续',exact=True)).to_be_enabled()
                page.get_by_role('button',name='用这份资料继续',exact=True).click()
                expect(page.get_by_text('数据已就绪 · 30 个商品',exact=False)).to_be_visible()
                page.screenshot(path=str(out/'collected.png'),full_page=True)
                page.get_by_role('spinbutton',name='每周期额度（元）',exact=True).fill('25000')
                page.get_by_role('spinbutton',name='自动覆盖天数',exact=True).fill('7')
                page.get_by_role('spinbutton',name='自动安全天数',exact=True).fill('2')
                page.get_by_text('我确认按以上周期和预算检查补货；采购前仍由我批准',exact=True).click()
                page.get_by_role('button',name='保存并开始观察',exact=True).click()
                groups=wait(lambda:client.get('/replenishment-policies').json()['policies'],lambda rows:len(rows)==1,'configure',20)
                gid=groups[0]['group_id'];evidence['group_id']=gid
                def group():
                    g=client.get('/replenishment-policies/'+gid).json()
                    assert g['state']['status'] not in ('failed','blocked'),g['state'];return g
                observed=wait(group,lambda g:g['state']['status']=='observed','observation-plan')
                tid=observed['state']['task_id'];evidence['task_id']=tid
                first=client.get('/tasks/'+tid).json();evidence['first']=first
                evidence['independent_calculation']=independent_plan(first['latest_plan'],oracle)
                assert client.get(urls['portal']+'/api/purchase-orders').json()==[]
                combo=page.get_by_role('combobox',name='运行方式',exact=True);combo.click();combo.press('ArrowDown');combo.press('Enter')
                page.get_by_role('button',name='更新自动补货设置',exact=True).click()
                wait(group,lambda g:g['state']['status']=='needs_review','review-plan')
                approved=client.get('/tasks/'+tid).json();plan=approved['latest_plan'];evidence['review']=approved
                assert plan['version']==2 and plan['dataset_id']==did
                assert independent_plan(plan,oracle)==evidence['independent_calculation']
                assert client.post('/tasks/'+tid+'/execute').status_code==409
                with page.expect_popup() as popup:page.get_by_role('link',name='打开自动补货任务',exact=True).click()
                page=popup.value;page.set_default_timeout(30000)
                page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准并执行',exact=True).click();evidence['approval_clicks']=1
                def task():
                    t=client.get('/tasks/'+tid).json()
                    assert t['state'] not in ('FAILED','PARTIAL','NEEDS_RECONCILIATION'),t['operation'];return t
                final=wait(task,lambda t:t['state']=='COMPLETED' and not t['operation']['busy'],'execution')
                evidence['final']=final
                assert final['approved_plan_hash']==approved['latest_plan_hash']
                orders=client.get(urls['portal']+'/api/purchase-orders').json();evidence['orders']=orders
                expected={}
                for item in plan['items']:
                    if item['order_qty']:expected.setdefault(item['supplier_id'],[]).append((item['sku'],item['order_qty'],item['unit_cost_cents']))
                assert len(orders)==len(expected)==4
                for order in orders:
                    assert order['status']=='SUBMITTED' and order['external_ref']==tid+'-v2-'+order['supplier_id']
                    assert sorted((r['sku'],r['order_qty'],r['unit_cost_cents']) for r in order['lines'])==sorted(expected[order['supplier_id']])
                    assert order['total_cost_cents']==sum(q*p for _,q,p in expected[order['supplier_id']])
                assert sum(o['total_cost_cents'] for o in orders)==1646694
                assert sum(r['order_qty'] for o in orders for r in o['lines'])==594
                assert client.post('/tasks/'+tid+'/execute').status_code==409
                assert client.get(urls['portal']+'/api/purchase-orders').json()==orders
                activity=client.get('/tasks/'+tid+'/activity').json()['events'];evidence['activity']=activity
                assert len([e for e in activity if e['kind']=='approved'])==1
                calls={e['payload']['tool'] for e in activity if e['kind']=='business_tool' and 'result' in e['payload']}
                assert {'inspect_dataset','build_plan','get_execution_result'}<=calls
                assert_planner_summaries(activity,{1:first['latest_plan'],2:plan})
                assert final['operation']['result']['agent']['status']=='ok'
                summary=final['operation']['result']['agent']['summary']
                assert all(o['order_id'] in summary for o in orders) and '美元' not in summary and '美分' not in summary
                runs=client.get('/data-sources/'+sid+'/collection-runs').json()['runs'];evidence['collection_runs']=runs
                assert len(runs)==1 and runs[0]['status']=='succeeded'
                models=[e['result'] for e in runs[0]['events'] if e['tool']=='model_response']
                assert len(models)==1 and models[0]['model']=='intern-s2' and models[0]['exit_code']==0
                exports={}
                for kind,label in [('csv','下载补货清单 CSV'),('html','下载完整报告 HTML')]:
                    with page.expect_download() as download:page.get_by_role('button',name=label,exact=True).click()
                    path=out/('report.'+kind);download.value.save_as(path)
                    exports[kind]={'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                check_exports(out,exports,plan,tid,approved['latest_plan_hash'],orders);evidence['exports']=exports
                page.screenshot(path=str(out/'completed.png'),full_page=True)
                evidence['passed']=True;save()
            except BaseException:
                evidence['error']=traceback.format_exc()
                if page:page.screenshot(path=str(out/'failure.png'),full_page=True)
                raise
            finally:
                if sid:evidence['runs_at_exit']=client.get('/data-sources/'+sid+'/collection-runs').json()
                if tid:evidence['task_at_exit']=client.get('/tasks/'+tid).json()
                evidence['tasks_at_exit']=[client.get('/tasks/'+t['task_id']).json() for t in client.get('/tasks').json()['tasks']]
                evidence['policies_at_exit']=client.get('/replenishment-policies').json()
                evidence['orders_at_exit']=client.get(urls['portal']+'/api/purchase-orders').json()
                browser.close()
    finally:
        supervisor.stop()
        released=[]
        for name,port in ports.items():
            with socket.socket() as sock:
                sock.settimeout(.2)
                if sock.connect_ex(('127.0.0.1',port))==0:released.append(name)
        evidence['ports_still_open']=released
        demo_pid=evidence.get('demo',{}).get('pid')
        evidence['demo_process_remains']=bool(demo_pid and psutil.pid_exists(demo_pid))
        if released or evidence['demo_process_remains']:evidence['passed']=False
        save();assert not released and not evidence['demo_process_remains']
