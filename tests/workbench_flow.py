"""Actual 30-SKU UI flow shared by acceptance and stability; isolated services."""
import os,json,time,socket,secrets,hashlib,traceback
from app.redaction import redact, redact_value
from pathlib import Path
import httpx,pytest
from playwright.sync_api import sync_playwright,expect
from start import Service,Supervisor,services
from tests.summary_assertions import assert_planner_summaries
ROOT=Path(__file__).resolve().parents[1]
class SafetyFailure(AssertionError):
    pass

class FlowTimeout(TimeoutError):
    pass

def require(condition, code):
    if not condition:
        raise SafetyFailure(code)


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def run_workbench_flow(tmp_path, mode, output, continuous=False):
    required = mode == 'required'
    OUT=Path(output);OUT.mkdir(parents=True,exist_ok=True)
    if required:
        assert os.environ.get('INTERN_API_KEY'), 'Real model key is required'
    ports={k:port() for k in (('proxy','portal','api','ui') if required else ('portal','api','ui'))}
    urls={k:f'http://127.0.0.1:{v}' for k,v in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':mode,'RESTOCK_API_URL':urls['api'],
        'RESTOCK_LITELLM_URL':urls.get('proxy','http://127.0.0.1:1'),'PROCUREMENT_PORTAL_URL':urls['portal'],
        'RESTOCK_DB_PATH':str(tmp_path/'main.db'),'PROCUREMENT_DB_PATH':str(tmp_path/'portal.db'),
        'RESTOCK_APPROVAL_TOKEN':secrets.token_urlsafe(32),'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    specs=[]
    for spec in services(not required):
        cmd=list(spec.command);flag='--server.port' if spec.name=='ui' else '--port'
        cmd[cmd.index(flag)+1]=str(ports[spec.name])
        specs.append(Service(spec.name,ports[spec.name],cmd,spec.health_path))
    evidence={'continuous_execution':continuous,'mode':mode,'model':'intern-s2' if required else None,'sample_skus':30,'passed':False,'phase':'start','phase_seconds':{},'task_id':None,'failure_kind':'blocking','safety_failures':[],
              'dataset_files':{n:hashlib.sha256((ROOT/'data'/(n+'.csv')).read_bytes()).hexdigest() for n in ('sales','inventory','suppliers')}}
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
        raise FlowTimeout('Stage timeout: '+phase)
    try:
        supervisor.start()
        with sync_playwright() as pw,httpx.Client(base_url=urls['api'],trust_env=False,timeout=20) as client:
            browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            page=browser.new_page(viewport={'width':1440,'height':1100},accept_downloads=True)
            page.set_default_timeout(20000)
            try:
                page.goto(urls['ui'])
                expect(page.get_by_text('离线演示模式：',exact=False)).to_have_count(0 if required else 1)
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
                evidence['unauthorized_execute_http']=client.post('/tasks/'+evidence['task_id']+'/execute').status_code
                evidence['orders_before_approval']=httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()
                require(evidence['unauthorized_execute_http']==409 and evidence['orders_before_approval']==[], 'unauthorized_write')
                page.get_by_text('我已核对 v1 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准当前方案').click()
                expect(page.get_by_role('button',name='创建采购申请')).to_be_visible()
                evidence['approval_v1']=client.get('/tasks/'+evidence['task_id']).json()
                require(evidence['approval_v1']['approved_plan_hash']==first['latest_plan_hash'], 'approval_binding')
                page.get_by_text('调整参数',exact=True).click()
                if required:
                    page.get_by_role('textbox',name='用一句话说明修改意见').fill('把预算改为3000元，其他参数保持不变。')
                    page.get_by_role('button',name='提交修改意见').click()
                else:
                    page.get_by_role('spinbutton',name='采购预算（元）',exact=True).fill('3000')
                    page.get_by_role('button',name='按参数重新计算').click()
                evidence['phase']='reviser'
                # Wait until the asynchronous request has started or the version changes.
                for _ in range(60):
                    state=client.get('/tasks/'+evidence['task_id']).json()
                    if state['operation']['busy'] or state['latest_plan']['version']==2:break
                    time.sleep(.2)
                revised=wait_operation(client,'reviser')
                revision_result=revised['operation']['result']['agent']
                explanation=json.loads(revision_result['summary'])['explanation']
                amount=revised['latest_plan']['total_cost_cents']
                exact=f'{amount//100}.{amount%100:02d} 元'
                assert '程序生成' in explanation and exact in explanation and '3000.00 元' in explanation
                assert revised['latest_plan']['version']==2
                assert revised['latest_plan']['policy']=={**first['latest_plan']['policy'],'budget_cents':300000}
                assert revised['approved_plan_hash'] is None
                assert revised['latest_plan']['dataset_hash']==first['latest_plan']['dataset_hash']
                expect(page.get_by_role('heading',name='与上一版本比较')).to_be_visible()
                expect(page.get_by_role('button',name='创建采购申请')).to_have_count(0)
                evidence['stale_execute_http']=client.post('/tasks/'+evidence['task_id']+'/execute').status_code
                evidence['orders_after_revision']=httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()
                require(evidence['stale_execute_http']==409 and evidence['orders_after_revision']==[], 'stale_approval')
                page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True).click()
                if continuous:
                    page.get_by_role('button',name='批准并执行',exact=True).click()
                    for _ in range(60):
                        approved=client.get('/tasks/'+evidence['task_id']).json()
                        if approved.get('execution_intent'):break
                        time.sleep(.2)
                    evidence['approval_v2']=approved
                    require(approved.get('execution_intent',{}).get('plan_hash')==revised['latest_plan_hash'],'durable_approval_binding')
                    evidence['execution_clicks']=0
                else:
                    page.get_by_role('button',name='批准当前方案').click()
                    expect(page.get_by_role('button',name='创建采购申请')).to_be_visible()
                    evidence['approval_v2']=client.get('/tasks/'+evidence['task_id']).json()
                    page.get_by_role('button',name='创建采购申请').click()
                require(evidence['approval_v2']['approved_plan_hash']==revised['latest_plan_hash'], 'approval_binding')
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
                    target=OUT/('delivered.'+suffix);download.value.save_as(target)
                    content=target.read_text(encoding='utf-8-sig')
                    assert evidence['task_id'] in content
                    if suffix=='html':assert 'SUBMITTED' in content and 'PO-' in content
                    evidence.setdefault('exports',{})[suffix]={'path':target.name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
                orders=httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()
                evidence['independent_orders']=orders
                plan=revised['latest_plan'];expected={}
                for item in plan['items']:
                    if item['order_qty']:
                        expected.setdefault(item['supplier_id'],[]).append((item['sku'],item['order_qty'],item['unit_cost_cents']))
                require(len(orders)==len(expected)>0, 'duplicate_or_missing_order')
                for order in orders:
                    lines=sorted((i['sku'],i['order_qty'],i['unit_cost_cents']) for i in order['lines'])
                    assert order['status']=='SUBMITTED' and order['external_ref']==evidence['task_id']+'-v2-'+order['supplier_id']
                    require(lines==sorted(expected[order['supplier_id']]), 'amount_or_quantity_mismatch')
                    require(order['total_cost_cents']==sum(q*p for _,q,p in lines), 'amount_mismatch')
                require(sum(o['total_cost_cents'] for o in orders)==plan['total_cost_cents']<=300000, 'amount_mismatch')
                evidence['repeat_execute_http']=client.post('/tasks/'+evidence['task_id']+'/execute').status_code
                evidence['orders_after_repeat']=httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()
                require(evidence['repeat_execute_http']==409 and evidence['orders_after_repeat']==orders, 'duplicate_order')
                activity=client.get('/tasks/'+evidence['task_id']+'/activity').json()['events']
                evidence['activity']=activity
                calls=[e['payload'] for e in activity if e['kind']=='business_tool' and 'result' in e['payload']]
                if required:
                    assert all(any(c['tool']==name for c in calls) for name in ('inspect_dataset','build_plan','revise_plan','get_execution_result'))
                    assert_planner_summaries(activity,{1:first['latest_plan'],2:revised['latest_plan']})
                    assert all(evidence[s]['operation']['result']['agent']['status']=='ok' for s in ('planner','reviser','executor'))
                assert page.locator('[data-testid="stException"]').count()==0
                page.screenshot(path=str(OUT/'completed.png'),full_page=True)
                evidence['final_state']=client.get('/tasks/'+evidence['task_id']).json()
                evidence['failure_kind']=None
                from scripts.business_evidence import check_complete
                check_complete(evidence,OUT,mode)
                evidence['passed']=True;evidence['phase']='complete'
            except BaseException as exc:
                evidence["failure_trace"]=redact(traceback.format_exc(),env)
                if isinstance(exc,SafetyFailure): evidence["safety_failures"].append(str(exc))
                if page:
                    try:page.screenshot(path=str(OUT/'failure.png'),full_page=True)
                    except Exception:pass
                raise
            finally:
                if browser:browser.close()
    except BaseException as exc:
        evidence.setdefault('failure_trace',redact(traceback.format_exc(),env))
        if isinstance(exc, FlowTimeout) and evidence['phase'] in ('planner','reviser'):
            try:
                orders=httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False,timeout=5).json()
                if orders==[]:
                    evidence['failure_kind']='ordinary_timeout'
                    evidence['no_orders_at_timeout']=True
            except httpx.HTTPError:
                pass
        raise
    finally:
        if evidence['task_id']:
            try:
                evidence['final_state']=httpx.get(urls['api']+'/tasks/'+evidence['task_id'],trust_env=False,timeout=5).json()
                evidence['activity']=httpx.get(urls['api']+'/tasks/'+evidence['task_id']+'/activity',trust_env=False,timeout=5).json()['events']
            except httpx.HTTPError:pass
        import psutil
        owned={}
        for _,proc,owner,_ in supervisor.children:
            try:
                for process in [owner]+owner.children(recursive=True):owned[process.pid]=process.create_time()
            except psutil.Error:pass
        supervisor.stop()
        remaining=[]
        for pid,created in owned.items():
            try:
                process=psutil.Process(pid)
                if process.is_running() and abs(process.create_time()-created)<.01:remaining.append(pid)
            except psutil.Error:pass
        open_ports=[]
        for number in ports.values():
            with socket.socket() as probe:
                probe.settimeout(.3)
                if probe.connect_ex(('127.0.0.1',number))==0:open_ports.append(number)
        evidence['shutdown']={'recorded_processes':len(owned),'remaining_owned':remaining,'open_service_ports':open_ports}
        if remaining or open_ports:
            evidence['passed']=False;evidence['safety_failures'].append('shutdown_residue')
        (OUT/'business.json').write_text(json.dumps(redact_value(evidence,env),ensure_ascii=False,indent=2),encoding='utf-8')

        assert not remaining and not open_ports, 'Owned service residue after shutdown'
