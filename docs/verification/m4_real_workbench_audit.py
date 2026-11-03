"""Audit-only real-model workbench integration; no production files modified."""
import os,json,time,socket,secrets,hashlib
from pathlib import Path
import httpx,pytest
from playwright.sync_api import sync_playwright,expect
from start import Service,Supervisor,services
from tests.summary_assertions import assert_planner_summaries
ROOT=Path(__file__).resolve().parents[2]
OUT=Path(os.getenv('RESTOCK_M4_AUDIT_OUTPUT',str(ROOT/'outputs/m4-recheck')))
OUT.mkdir(parents=True,exist_ok=True)

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

@pytest.mark.skipif(os.getenv('RUN_REAL_MODEL_E2E')!='1',reason='explicit opt-in calls real InternLM')
@pytest.mark.parametrize('round_id',[1,2])
def test_required_model_workbench_full_flow(tmp_path,round_id):
    assert os.environ.get('INTERN_API_KEY')
    ports={k:port() for k in ('proxy','portal','api','ui')}
    urls={k:f'http://127.0.0.1:{v}' for k,v in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':'required','RESTOCK_API_URL':urls['api'],
        'RESTOCK_LITELLM_URL':urls['proxy'],'PROCUREMENT_PORTAL_URL':urls['portal'],
        'RESTOCK_DB_PATH':str(tmp_path/'main.db'),'PROCUREMENT_DB_PATH':str(tmp_path/'portal.db'),
        'RESTOCK_APPROVAL_TOKEN':secrets.token_urlsafe(32),'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    specs=[]
    for spec in services(False):
        cmd=list(spec.command);flag='--server.port' if spec.name=='ui' else '--port'
        cmd[cmd.index(flag)+1]=str(ports[spec.name])
        specs.append(Service(spec.name,ports[spec.name],cmd,spec.health_path))
    evidence={'round':round_id,'mode':'required','model':'intern-s2','sample_skus':30,'passed':False,'phase':'start','phase_seconds':{},'task_id':None}
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=60)
    page=None;browser=None
    def wait_operation(client,phase):
        started=time.monotonic()
        while time.monotonic()-started<660:
            result=client.get('/tasks/'+evidence['task_id']).json()
            if not result['operation']['busy']:
                evidence['phase_seconds'][phase]=round(time.monotonic()-started,2)
                evidence[phase]=result
                assert not result['operation']['error'],result['operation']['error']
                return result
            time.sleep(1)
        raise AssertionError('Stage timeout: '+phase)
    try:
        supervisor.start()
        with sync_playwright() as pw,httpx.Client(base_url=urls['api'],trust_env=False,timeout=20) as client:
            browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            page=browser.new_page(viewport={'width':1440,'height':1100},accept_downloads=True)
            page.set_default_timeout(20000)
            try:
                page.goto(urls['ui'])
                expect(page.get_by_text('离线演示模式：',exact=False)).to_have_count(0)
                evidence['phase']='upload'
                for i,name in enumerate(('sales','inventory','suppliers')):
                    page.locator('input[type=file]').nth(i).set_input_files(str(ROOT/'data'/(name+'.csv')))
                for label in ('三份数据已统一为件','可用库存已扣除预占','单价为含税含运的整数分','在途到货时间填写的是距规划时点的天数'):
                    page.get_by_text(label,exact=True).click()
                page.get_by_role('button',name='检查并保存数据').click()
                expect(page.get_by_text('数据已就绪 · 30 个商品',exact=False)).to_be_visible()
                page.get_by_role('spinbutton',name='采购预算（元）',exact=True).fill('5000')
                page.get_by_role('button',name='生成补货方案').click()
                page.wait_for_url('**task=**',timeout=20000)
                evidence['task_id']=page.url.split('task=')[1].split('&')[0]
                evidence['phase']='planner'
                first=wait_operation(client,'planner')
                assert first['latest_plan']['version']==1 and len(first['latest_plan']['items'])==30
                assert first['latest_plan']['policy']['budget_cents']==500000
                expect(page.get_by_role('heading',name='人工确认',exact=True)).to_be_visible()
                page.get_by_text('我已核对 v1 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准当前方案').click()
                expect(page.get_by_role('button',name='创建采购申请')).to_be_visible()
                page.get_by_text('调整参数',exact=True).click()
                page.get_by_role('textbox',name='用一句话说明修改意见').fill('把预算改为3000元，其他参数保持不变。')
                page.get_by_role('button',name='提交修改意见').click()
                evidence['phase']='reviser'
                # Wait until the asynchronous request has started or the version changes.
                for _ in range(60):
                    state=client.get('/tasks/'+evidence['task_id']).json()
                    if state['operation']['busy'] or state['latest_plan']['version']==2:break
                    time.sleep(.2)
                revised=wait_operation(client,'reviser')
                assert revised['latest_plan']['version']==2
                assert revised['latest_plan']['policy']=={**first['latest_plan']['policy'],'budget_cents':300000}
                assert revised['approved_plan_hash'] is None
                assert revised['latest_plan']['dataset_hash']==first['latest_plan']['dataset_hash']
                expect(page.get_by_role('heading',name='与上一版本比较')).to_be_visible()
                expect(page.get_by_role('button',name='创建采购申请')).to_have_count(0)
                page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准当前方案').click()
                page.get_by_role('button',name='创建采购申请').click()
                evidence['phase']='executor'
                for _ in range(60):
                    state=client.get('/tasks/'+evidence['task_id']).json()
                    if state['operation']['busy'] or state['state']=='COMPLETED':break
                    time.sleep(.2)
                finished=wait_operation(client,'executor')
                assert finished['state']=='COMPLETED'
                expect(page.get_by_role('button',name='下载完整报告 HTML')).to_be_visible()
                page.reload()
                expect(page.get_by_text('申请已创建，回读核对一致。',exact=True).first).to_be_visible()
                for label,suffix in [('下载补货清单 CSV','csv'),('下载完整报告 HTML','html')]:
                    with page.expect_download() as download:page.get_by_role('button',name=label).click()
                    target=tmp_path/('delivered.'+suffix);download.value.save_as(target)
                    content=target.read_text(encoding='utf-8-sig')
                    assert evidence['task_id'] in content
                    if suffix=='html':assert 'SUBMITTED' in content and 'PO-' in content
                orders=httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()
                evidence['independent_orders']=orders
                plan=revised['latest_plan'];expected={}
                for item in plan['items']:
                    if item['order_qty']:
                        expected.setdefault(item['supplier_id'],[]).append((item['sku'],item['order_qty'],item['unit_cost_cents']))
                assert len(orders)==len(expected)>0
                for order in orders:
                    lines=sorted((i['sku'],i['order_qty'],i['unit_cost_cents']) for i in order['lines'])
                    assert order['status']=='SUBMITTED' and order['external_ref']==evidence['task_id']+'-v2-'+order['supplier_id']
                    assert lines==sorted(expected[order['supplier_id']])
                    assert order['total_cost_cents']==sum(q*p for _,q,p in lines)
                assert sum(o['total_cost_cents'] for o in orders)==plan['total_cost_cents']<=300000
                assert client.post('/tasks/'+evidence['task_id']+'/execute').status_code==409
                assert httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()==orders
                activity=client.get('/tasks/'+evidence['task_id']+'/activity').json()['events']
                evidence['activity']=activity
                calls=[e['payload'] for e in activity if e['kind']=='business_tool' and 'result' in e['payload']]
                assert all(any(c['tool']==name for c in calls) for name in ('inspect_dataset','build_plan','revise_plan','get_execution_result'))
                assert_planner_summaries(activity,{1:first['latest_plan'],2:revised['latest_plan']})
                assert all(evidence[s]['operation']['result']['agent']['status']=='ok' for s in ('planner','reviser','executor'))
                assert page.locator('[data-testid="stException"]').count()==0
                page.screenshot(path=str(OUT/f'real-ui-{round_id}.png'),full_page=True)
                evidence['passed']=True;evidence['phase']='complete'
            except BaseException:
                if page:page.screenshot(path=str(OUT/f'real-ui-{round_id}-failure.png'),full_page=True)
                raise
            finally:
                if browser:browser.close()
    finally:
        if evidence['task_id']:
            try:
                evidence['final_state']=httpx.get(urls['api']+'/tasks/'+evidence['task_id'],trust_env=False,timeout=5).json()
                evidence['activity']=httpx.get(urls['api']+'/tasks/'+evidence['task_id']+'/activity',trust_env=False,timeout=5).json()['events']
            except httpx.HTTPError:pass
        supervisor.stop()
        (OUT/f'real-ui-{round_id}.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
