"""Combined M5 actual-page -> durable source -> scheduler -> UI approval -> target orders.

The merchant fixture is an independent oracle. Only the test process can access
it directly; the API/collector process must read the visible external webpage.
"""
import csv,io,json,os,time,secrets,hashlib,sys,socket,traceback
from datetime import datetime,timezone,date,timedelta
from fractions import Fraction
from pathlib import Path
import httpx
from tests.ui_actions import auto_entry
from playwright.sync_api import sync_playwright,expect
from app.models import DatasetSnapshot
from app.redaction import redact_value
from app.merchant_test_portal import build_portal_data
from start import Service,Supervisor,services
from tests.test_real_m2 import free_port
from tests.test_real_collection import SCOPE
from tests.test_browser_collection import config,assert_snapshot_matches_portal
from tests.summary_assertions import assert_planner_summaries
from scripts.business_evidence import check_exports


def independent_plan(plan,merchant):
    """Closed-form oracle from merchant values, with sufficient explicit budget."""
    policy=plan['policy'];expected={};d=merchant.data
    for sku in SCOPE:
        if sku in policy['excluded_skus']:continue
        sales=sum(r['qty']-r['refund_qty'] for r in d.sales if r['sku']==sku and r['store_id']=='S01'
                  and d.window_end-timedelta(days=27)<=r['date']<=d.window_end)
        available=(d.inventory[('W01',sku)]['on_hand']-d.inventory[('W01',sku)]['reserved'])*d.products[sku].unit_factor
        terms=d.supplier_terms[sku];horizon=terms['lead_days']+policy['cover_days']+policy['safety_days']
        # Fraction.__ceil__ is independent of production target/rounding helpers.
        target=Fraction(sales*horizon,28).__ceil__()
        inbound=sum(r['qty'] for r in d.inbound if r['warehouse_id']=='W01' and r['sku']==sku
                    and r['status'] in ('已订货','在途') and 0<=(date.fromisoformat(r['eta'])-d.window_end).days<=horizon)
        requested=sum(r['qty'] for r in d.inbound if r['warehouse_id']=='W01' and r['sku']==sku and r['status']=='已申请')
        gap=max(0,target-available-inbound-requested)
        quantity=0 if gap==0 else -(-max(gap,terms['min_order_qty'])//terms['pack_size'])*terms['pack_size']
        expected[sku]=(target,gap,quantity,quantity*terms['unit_cost_cents'],terms['supplier_id'],terms['unit_cost_cents'])
    assert set(expected)=={i['sku'] for i in plan['items']} and len(expected)==30
    total=sum(row[3] for row in expected.values());assert 0<total<policy['budget_cents']
    for item in plan['items']:
        assert (item['target_qty'],item['gap'],item['order_qty'],item['line_total_cents'],item['supplier_id'],item['unit_cost_cents'])==expected[item['sku']]
    assert plan['total_cost_cents']==total and plan['budget_remaining_cents']==policy['budget_cents']-total
    return {'total_cents':total,'quantity':sum(row[2] for row in expected.values()),'rows':expected}


def run_flow(tmp_path,portal,required,output):
    merchant,url=portal;out=Path(output);out.mkdir(parents=True,exist_ok=True)
    if required:assert os.getenv('INTERN_API_KEY'),'Real model key required'
    ports={n:free_port() for n in (('proxy','portal','api','ui') if required else ('portal','api','ui'))}
    urls={n:f'http://127.0.0.1:{p}' for n,p in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':'required' if required else 'off','RESTOCK_API_URL':urls['api'],
        'PROCUREMENT_PORTAL_URL':urls['portal'],'RESTOCK_LITELLM_URL':urls.get('proxy','http://127.0.0.1:1'),
        'RESTOCK_DB_PATH':str(tmp_path/'business.db'),'PROCUREMENT_DB_PATH':str(tmp_path/'procurement.db'),
        'RESTOCK_COLLECTION_DIR':str(tmp_path/'collections'),'RESTOCK_APPROVAL_TOKEN':secrets.token_urlsafe(32),
        'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','RESTOCK_AGENT_TIMEOUT_S':'600'}
    specs=[]
    for spec in services(not required):
        cmd=list(spec.command);flag='--server.port' if spec.name=='ui' else '--port';cmd[cmd.index(flag)+1]=str(ports[spec.name])
        if spec.name=='api' and not required:cmd=[sys.executable,'-m','tests.m5_flow_server','--port',str(ports['api'])]
        specs.append(Service(spec.name,ports[spec.name],cmd,spec.health_path))
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=60)
    evidence={'passed':False,'scope':'30 authorized synthetic merchant SKUs; actual visible-page reads; local demonstration target',
        'model':'intern-s2' if required else None,'mode':'required' if required else 'scripted collector + off business stages',
        'generate_clicks':0,'upload_actions':0,'collect_clicks':0,'execute_clicks':0,'final_approval_clicks':0,'phase':'starting','stages':{}}
    sid=gid=tid=None;page=None
    def save():
        (out/'business.json').write_text(json.dumps(redact_value(evidence,env),ensure_ascii=False,indent=2),encoding='utf-8')
    def wait(read,predicate,phase,seconds=660):
        evidence['phase']=phase;save();started=time.monotonic();value=None
        while time.monotonic()-started<seconds:
            value=read()
            if predicate(value):
                evidence['stages'][phase]={'elapsed_s':round(time.monotonic()-started,2)};save();return value
            time.sleep(.5)
        raise AssertionError('Timed out in '+phase+': '+str(value))
    try:
        supervisor.start()
        with httpx.Client(base_url=urls['api'],trust_env=False,timeout=30,headers={'X-Restock-Approval':env['RESTOCK_APPROVAL_TOKEN']}) as client,sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            try:
                page=browser.new_page(viewport={'width':1440,'height':1200},accept_downloads=True);page.set_default_timeout(30000)
                # Human source configuration. No observation rows or formal plans are injected.
                c=config(url,SCOPE).model_dump();c['schedule'].update(collect_every_seconds=600,save_every_seconds=1,check_every_seconds=2)
                created=client.post('/data-sources',json=c);assert created.status_code==200,created.text
                sid=created.json()['source_id'];evidence['source_id']=sid
                def publication(after=None):
                    def read():
                        record=client.get('/data-sources/'+sid).json();job=record['state'].get('collection_job',{})
                        assert job.get('status') not in ('failed','interrupted'),record
                        return record
                    return wait(read,lambda r:r['state']['latest_dataset_id'] and r['state']['latest_dataset_id']!=after and
                        r['state']['latest_dataset_id']==r['state']['latest_observed_dataset_id'] and
                        r['state'].get('collection_job',{}).get('status')=='succeeded','collect-first' if after is None else 'collect-second')
                def verify_source(record,label):
                    did=record['state']['latest_dataset_id'];prefix=f'/data-sources/{sid}/publications/{did}/'
                    manifest=client.get(prefix+'manifest.json').json();tables={}
                    for name in ('sales','inventory','suppliers'):
                        content=client.get(prefix+name+'.csv').content
                        assert hashlib.sha256(content).hexdigest()==manifest['files'][name+'.csv']['sha256']
                        (out/(label+'-'+name+'.csv')).write_bytes(content)
                        tables[name]=list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
                    snapshot=DatasetSnapshot.model_validate({**tables,**{k:manifest[k] for k in ('contract','window_start','window_end','purchase_context')}})
                    assert_snapshot_matches_portal(snapshot,merchant,set(SCOPE))
                    assert len(snapshot.sales)==840 and len(snapshot.inventory)==len(snapshot.suppliers)==30
                    evidence[label]={'dataset_id':did,'manifest':manifest,'rows':{k:len(v) for k,v in tables.items()},'independent_source_match':True}
                    save();return did
                first_source=publication();did1=verify_source(first_source,'first')
                page.goto(urls['ui']);auto_entry(page)
                page.get_by_role('spinbutton',name='每周期额度（元）',exact=True).fill('25000')
                page.get_by_role('spinbutton',name='自动覆盖天数',exact=True).fill('7')
                page.get_by_role('spinbutton',name='自动安全天数',exact=True).fill('2')
                page.get_by_text('我确认按以上周期和预算检查补货；采购前仍由我批准',exact=True).click()
                page.get_by_role('button',name='保存并开始观察',exact=True).click()
                groups=wait(lambda:client.get('/replenishment-policies').json()['policies'],lambda rows:len(rows)==1,'configure',30)
                gid=groups[0]['group_id'];evidence['group_id']=gid
                def group_ready(expected):
                    def read():
                        g=client.get('/replenishment-policies/'+gid).json()
                        assert g['state']['status'] not in ('failed','blocked'),g['state']
                        return g
                    return wait(read,lambda g:g['state']['status']==expected,'scheduler-'+expected)
                observed=group_ready('observed');tid=observed['state']['task_id'];evidence['task_id']=tid
                first=client.get('/tasks/'+tid).json();evidence['planner']=first
                evidence['first_oracle']=independent_plan(first['latest_plan'],merchant)
                assert first['latest_plan']['dataset_id']==did1 and first['latest_plan']['version']==1
                assert client.post('/tasks/'+tid+'/approve',json={'version':1,'plan_hash':first['latest_plan_hash']}).status_code==409
                assert client.get(urls['portal']+'/api/purchase-orders').json()==[]
                combo=page.get_by_role('combobox',name='运行方式',exact=True);expect(combo).to_be_visible();combo.click();combo.press('ArrowDown');combo.press('Enter')
                expect(combo).to_match_aria_snapshot('- combobox "运行方式": 生成待审方案')
                page.get_by_role('button',name='更新自动补货设置',exact=True).click()
                group_ready('needs_review')
                second=client.get('/tasks/'+tid).json();assert second['latest_plan']['version']==2
                with page.expect_popup() as pending:page.get_by_role('link',name='打开自动补货任务',exact=True).click()
                page=pending.value;page.set_default_timeout(30000)
                expect(page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True)).to_be_visible()
                # Change the test merchant independently. A timer, never /collect or /tasks POST,
                # retrieves the new page batch and updates the existing task.
                merchant.data=build_portal_data(batch=2)
                schedule={**c['schedule'],'collect_every_seconds':15}
                assert client.put('/data-sources/'+sid+'/settings',json=schedule).status_code==200
                second_source=publication(did1);did2=verify_source(second_source,'second')
                assert client.put('/data-sources/'+sid+'/collection-enabled',json={'enabled':False}).status_code==200
                final_plan=wait(lambda:client.get('/tasks/'+tid).json(),lambda t:t['latest_plan']['version']==3 and not t['operation']['busy'],'automatic-revision',60)
                assert final_plan['latest_plan']['dataset_id']==did2 and did1!=did2
                assert len(client.get('/tasks').json()['tasks'])==1 and final_plan['approved_version'] is None
                evidence['revision']=final_plan;evidence['second_oracle']=independent_plan(final_plan['latest_plan'],merchant)
                assert evidence['first_oracle']!=evidence['second_oracle']
                assert client.post('/tasks/'+tid+'/approve',json={'version':2,'plan_hash':second['latest_plan_hash']}).status_code==409
                assert client.post('/tasks/'+tid+'/execute').status_code==409
                assert client.get(urls['portal']+'/api/purchase-orders').json()==[]
                expect(page.get_by_text('我已核对 v3 的数量、金额和风险提示',exact=True)).to_be_visible()
                expect(page.get_by_text('预算周期与已有采购',exact=True)).to_be_visible()
                page.get_by_text('我已核对 v3 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准并执行',exact=True).click();evidence['final_approval_clicks']=1
                final=wait(lambda:client.get('/tasks/'+tid).json(),lambda t:t['state']=='COMPLETED' and not t['operation']['busy'],'execution')
                evidence['final']=final
                g=client.get('/replenishment-policies/'+gid).json();g['config']['enabled']=False
                assert client.put('/replenishment-policies/'+gid,json=g['config']).status_code==200
                plan=final_plan['latest_plan'];orders=client.get(urls['portal']+'/api/purchase-orders').json();evidence['orders']=orders
                expected={}
                for item in plan['items']:
                    if item['order_qty']:expected.setdefault(item['supplier_id'],[]).append((item['sku'],item['order_qty'],item['unit_cost_cents']))
                assert len(orders)==len(expected)>0 and len({o['order_id'] for o in orders})==len(orders)
                for order in orders:
                    assert order['status']=='SUBMITTED' and order['external_ref']==tid+'-v3-'+order['supplier_id']
                    assert sorted((r['sku'],r['order_qty'],r['unit_cost_cents']) for r in order['lines'])==sorted(expected[order['supplier_id']])
                    assert order['total_cost_cents']==sum(q*p for _,q,p in expected[order['supplier_id']])
                assert sum(o['total_cost_cents'] for o in orders)==evidence['second_oracle']['total_cents']
                assert final['approved_plan_hash']==final_plan['latest_plan_hash'] and final['execution_intent']['status']=='succeeded'
                activity=client.get('/tasks/'+tid+'/activity').json()['events'];evidence['activity']=activity
                approved=[e for e in activity if e['kind']=='approved'];assert len(approved)==1 and approved[0]['payload']['version']==3
                audit=client.get('/tasks/'+tid+'/audit-trail').json();evidence['audit']=audit
                assert audit['replay_complete'],audit
                assert client.post('/tasks/'+tid+'/execute').status_code==409 and client.get(urls['portal']+'/api/purchase-orders').json()==orders
                runs=client.get('/data-sources/'+sid+'/collection-runs').json()['runs'];evidence['collection_runs']=runs
                assert len(runs)==2 and all(r['status']=='succeeded' for r in runs)
                assert merchant.write_log==[];evidence['merchant_writes']=merchant.write_log
                if required:
                    assert first['operation']['result']['agent']['status']==final['operation']['result']['agent']['status']=='ok'
                    decision=json.loads(first['operation']['result']['agent']['model_advice'])
                    assert set(decision)=={'advice_codes'} and 1<=len(decision['advice_codes'])<=3
                    assert set(decision['advice_codes'])<={'review_parameters','review_future_budget','review_low_confidence','review_arrival_risk'}
                    for run in runs:
                        models=[e['result'] for e in run['events'] if e['tool']=='model_response']
                        assert len(models)==1 and models[0]['model']=='intern-s2' and models[0]['exit_code']==0
                    calls={e['payload']['tool'] for e in activity if e['kind']=='business_tool' and 'result' in e['payload']}
                    assert {'inspect_dataset','build_plan','revise_plan','get_execution_result'}<=calls
                    assert_planner_summaries(activity,{1:first['latest_plan'],3:plan})
                    summary=final['operation']['result']['agent']['summary'];evidence['executor_summary']=summary
                    assert all(o['order_id'] in summary for o in orders) and '美元' not in summary and '美分' not in summary
                exports={}
                for kind,label in [('csv','下载补货清单 CSV'),('html','下载完整报告 HTML')]:
                    expect(page.get_by_role('button',name=label,exact=True)).to_be_visible()
                    with page.expect_download() as download:page.get_by_role('button',name=label,exact=True).click()
                    path=out/('report.'+kind);download.value.save_as(path)
                    exports[kind]={'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                check_exports(out,exports,plan,tid,final_plan['latest_plan_hash'],orders);evidence['exports']=exports
                assert '预算周期与已有采购' in (out/'report.html').read_text(encoding='utf-8')
                assert page.locator('[data-testid="stException"]').count()==0
                page.screenshot(path=str(out/'completed.png'),full_page=True);evidence.update(passed=True,phase='complete');save()
            except BaseException:
                evidence['failure']=traceback.format_exc()
                if page:
                    page.screenshot(path=str(out/'failure.png'),full_page=True)
                    (out/'failure-page.txt').write_text(page.locator('body').inner_text(),encoding='utf-8')
                raise
            finally:
                for name,path in [('collection_runs_at_exit','/data-sources/'+str(sid)+'/collection-runs'),('task_at_exit','/tasks/'+str(tid))]:
                    try:evidence[name]=client.get(path).json()
                    except httpx.HTTPError:pass
                browser.close()
    finally:
        import psutil
        owned={}
        for _,proc,owner,_ in supervisor.children:
            try:
                for process in [owner]+owner.children(recursive=True):owned[process.pid]=process.create_time()
            except psutil.Error:pass
        supervisor.stop();remaining=[];open_ports=[]
        for pid,created in owned.items():
            try:
                process=psutil.Process(pid)
                if process.is_running() and abs(process.create_time()-created)<.01:remaining.append(pid)
            except psutil.Error:pass
        for number in ports.values():
            with socket.socket() as probe:
                probe.settimeout(.3)
                if probe.connect_ex(('127.0.0.1',number))==0:open_ports.append(number)
        evidence['shutdown']={'recorded_processes':len(owned),'remaining_owned':remaining,'open_ports':open_ports}
        if remaining or open_ports:evidence['passed']=False
        save();assert not remaining and not open_ports
