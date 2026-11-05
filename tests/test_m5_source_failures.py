"""Actual page failure -> no purchase -> source recovery -> one approved execution."""
import os,sys,time,json,secrets,uuid
from pathlib import Path
from datetime import datetime,timezone
import httpx,pytest
from app.merchant_test_portal import create_merchant_test_app
from tests.test_fault_injection import _start_portal
from tests.test_browser_collection import config
from tests.m5_end_to_end import independent_plan,SCOPE
from tests.test_replenishment_live import until
from tests.test_real_m2 import free_port
from start import Service,Supervisor,services


class Gate:
    def __init__(self,app):self.app=app;self.down=False;self.rejected=0
    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and self.down:
            self.rejected+=1
            await send({'type':'http.response.start','status':503,'headers':[(b'content-type',b'text/plain')]})
            await send({'type':'http.response.body','body':b'merchant unavailable - injected outage'})
            return
        return await self.app(scope,receive,send)


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='actual collector/API/browser fault chain')
@pytest.mark.parametrize('fault',['missing_cost','outage'])
def test_source_failure_blocks_purchase_and_recovery_keeps_same_plan(tmp_path,fault):
    gate=Gate(create_merchant_test_app());url,app,server,thread=_start_portal(tmp_path,gate);merchant=app.state.portal
    ports={n:free_port() for n in ('portal','api')};urls={n:f'http://127.0.0.1:{p}' for n,p in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':'off','RESTOCK_API_URL':urls['api'],'PROCUREMENT_PORTAL_URL':urls['portal'],
        'RESTOCK_DB_PATH':str(tmp_path/'main.db'),'PROCUREMENT_DB_PATH':str(tmp_path/'portal.db'),
        'RESTOCK_COLLECTION_DIR':str(tmp_path/'collections'),'RESTOCK_APPROVAL_TOKEN':secrets.token_urlsafe(32),
        'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    specs=[]
    for spec in services(True):
        if spec.name not in ports:continue
        command=list(spec.command);command[command.index('--port')+1]=str(ports[spec.name])
        if spec.name=='api':command=[sys.executable,'-m','tests.m5_flow_server','--port',str(ports['api'])]
        specs.append(Service(spec.name,ports[spec.name],command,spec.health_path))
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=40);evidence={'fault':fault,'mode':'test-controller + off','passed':False}
    try:
        supervisor.start()
        with httpx.Client(base_url=urls['api'],trust_env=False,timeout=30,headers={'X-Restock-Approval':env['RESTOCK_APPROVAL_TOKEN']}) as client:
            c=config(url,SCOPE).model_dump();c['schedule'].update(collect_every_seconds=600,check_every_seconds=2)
            r=client.post('/data-sources',json=c);assert r.status_code==200,r.text;sid=r.json()['source_id']
            first=until(lambda:client.get('/data-sources/'+sid).json(),lambda x:x['state']['latest_dataset_id'],seconds=80)
            did=first['state']['latest_dataset_id'];first_run=first['state']['collection_job']['run_id']
            policy={'budget_cents':2500000,'cover_days':7,'safety_days':2,'priority_categories':[],'excluded_skus':[]}
            cfg={'source_id':sid,'anchor':datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat(),
                 'period_seconds':86400,'policy':policy,'relationship':'separate','mode':'observe','enabled':True,
                 'legacy_task_ids':[],'legacy_tasks_reviewed':True}
            r=client.post('/replenishment-policies',json=cfg);assert r.status_code==200,r.text;gid=r.json()['group_id']
            g=until(lambda:client.get('/replenishment-policies/'+gid).json(),lambda x:x['state']['status']=='observed');tid=g['state']['task_id']
            cfg=g['config'];cfg['mode']='review';assert client.put('/replenishment-policies/'+gid,json=cfg).status_code==200
            plan=until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['latest_plan']['version']==2 and not x['operation']['busy'])
            expected=independent_plan(plan['latest_plan'],merchant)
            price=merchant.data.supplier_terms[SCOPE[0]]['unit_cost_cents']
            if fault=='missing_cost':merchant.data.supplier_terms[SCOPE[0]]['unit_cost_cents']=None
            else:gate.down=True
            schedule={**c['schedule'],'collect_every_seconds':2};assert client.put('/data-sources/'+sid+'/settings',json=schedule).status_code==200
            failed=until(lambda:client.get('/data-sources/'+sid).json(),lambda x:x['state']['collection_job']['run_id']!=first_run and x['state']['collection_job']['status']=='failed',seconds=80)
            assert client.put('/data-sources/'+sid+'/collection-enabled',json={'enabled':False}).status_code==200
            runs=client.get('/data-sources/'+sid+'/collection-runs').json()['runs'];evidence['failed_run']=runs[0]
            if fault=='missing_cost':assert '成本缺失' in runs[0]['error']
            else:assert gate.rejected>0
            until(lambda:client.get('/replenishment-policies/'+gid).json(),lambda x:x['state']['status']=='blocked')
            assert failed['state']['latest_dataset_id']==did and not failed['data_status']['ready']
            assert len(client.get('/data-sources/'+sid+'/publications').json()['publications'])==1
            assert client.post('/tasks/'+tid+'/approve',json={'version':2,'plan_hash':plan['latest_plan_hash']}).status_code==409
            assert client.post('/tasks/'+tid+'/execute').status_code==409
            assert client.get(urls['portal']+'/api/purchase-orders').json()==[]
            assert len(client.get('/tasks/'+tid+'/plans').json()['versions'])==2
            gate.down=False;merchant.data.supplier_terms[SCOPE[0]]['unit_cost_cents']=price
            assert client.put('/data-sources/'+sid+'/settings',json=c['schedule']).status_code==200
            assert client.put('/data-sources/'+sid+'/collection-enabled',json={'enabled':True}).status_code==200
            restored=until(lambda:client.get('/data-sources/'+sid).json(),lambda x:x['data_status']['ready'] and x['state']['collection_job']['status']=='succeeded',seconds=80)
            assert client.put('/data-sources/'+sid+'/collection-enabled',json={'enabled':False}).status_code==200
            ready=until(lambda:client.get('/replenishment-policies/'+gid).json(),lambda x:x['state']['status']=='needs_review')
            assert ready['state']['error'] is None and restored['state']['latest_dataset_id']==did
            assert len(client.get('/tasks').json()['tasks'])==1 and len(client.get('/tasks/'+tid+'/plans').json()['versions'])==2
            assert client.get('/tasks/'+tid).json()['latest_plan_hash']==plan['latest_plan_hash']
            payload={'version':2,'plan_hash':plan['latest_plan_hash'],'request_id':uuid.uuid4().hex,'expires_in_seconds':600}
            assert client.post('/tasks/'+tid+'/approve-and-execute',json=payload).status_code==202
            final=until(lambda:client.get('/tasks/'+tid).json(),lambda x:x['state']=='COMPLETED' and not x['operation']['busy'])
            cfg['enabled']=False;assert client.put('/replenishment-policies/'+gid,json=cfg).status_code==200
            orders=client.get(urls['portal']+'/api/purchase-orders').json()
            assert len(orders)==4 and sum(o['total_cost_cents'] for o in orders)==expected['total_cents']
            assert sum(l['order_qty'] for o in orders for l in o['lines'])==expected['quantity']
            assert client.post('/tasks/'+tid+'/approve-and-execute',json=payload).status_code==202
            assert client.get(urls['portal']+'/api/purchase-orders').json()==orders and merchant.write_log==[]
            activity=client.get('/tasks/'+tid+'/activity').json()['events'];assert sum(e['kind']=='approved' for e in activity)==1
            evidence.update(passed=True,task_id=tid,hash=plan['latest_plan_hash'],orders=orders,outage_responses=gate.rejected,
                checks=client.get('/replenishment-policies/'+gid).json()['checks'],no_orders_during_failure=True,original_version_retained=True)
    finally:
        supervisor.stop();server.should_exit=True;thread.join(5)
        out=Path(os.getenv('RESTOCK_M5_COMBINED_OUTPUT',str(tmp_path/'evidence')))/'faults';out.mkdir(parents=True,exist_ok=True)
        (out/(fault+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
