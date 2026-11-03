"""Opt-in real InternLM + Codex/MCP + Chromium chain on isolated demo services."""
import json
import os
from pathlib import Path
import secrets
import socket
import time

import httpx
import pytest

from start import Service, Supervisor, services
from tests.test_data_contract import write_dataset, contract

ROOT=Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


@pytest.mark.skipif(os.getenv('RUN_REAL_MODEL_E2E')!='1',reason='explicit opt-in calls real InternLM')
@pytest.mark.parametrize('sample',[1,30])
def test_real_internlm_tool_generation_revision_and_procurement(tmp_path,sample):
    assert os.getenv('INTERN_API_KEY') and os.getenv('INTERN_API_KEY')!='在这里填端砚key', 'Real model key is not configured'
    ports={name:free_port() for name in ('proxy','portal','api')}
    urls={name:f'http://127.0.0.1:{port}' for name,port in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':'required','RESTOCK_LITELLM_URL':urls['proxy'],
         'PROCUREMENT_PORTAL_URL':urls['portal'],'RESTOCK_DB_PATH':str(tmp_path/'business.db'),
         'PROCUREMENT_DB_PATH':str(tmp_path/'portal.db'),'RESTOCK_APPROVAL_TOKEN':secrets.token_urlsafe(32),
         'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1','PYTHONUNBUFFERED':'1'}
    specs=[]
    for spec in services(False):
        if spec.name not in ports: continue
        command=list(spec.command); command[command.index('--port')+1]=str(ports[spec.name])
        specs.append(Service(spec.name,ports[spec.name],command,spec.health_path))
    evidence={'model':'intern-s2','mode':'required','sample_skus':sample,'stages':{},'passed':False}
    output=Path(os.getenv('RESTOCK_ACCEPTANCE_OUTPUT', str(ROOT/'outputs/m2-remediation'))); output.mkdir(parents=True,exist_ok=True)
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=60)
    def wait_task(client,task,phase):
        start=time.monotonic()
        while time.monotonic()-start<660:
            status=client.get('/tasks/'+task).json()
            if not status['operation']['busy']:
                evidence['stages'][phase]={'elapsed_s':round(time.monotonic()-start,2),'state':status['state'],
                    'error':status['operation']['error'],'result':status['operation']['result']}
                assert not status['operation']['error'],status['operation']['error']
                return status
            time.sleep(1)
        pytest.fail('Stage timed out: '+phase)
    try:
        supervisor.start()
        write_dataset(tmp_path)
        with httpx.Client(base_url=urls['api'],trust_env=False,timeout=20) as client:
            files={n:(n+'.csv',(tmp_path/(n+'.csv')).read_bytes(),'text/csv') for n in ('sales','inventory','suppliers')}
            dataset=client.post('/datasets',files=files,data={'contract':contract().model_dump_json()}).json()
            if sample==30: dataset=client.post('/datasets/example').json()
            assert dataset['state']=='READY'
            initial_budget,changed_budget=(1800,3000) if sample==1 else (500000,300000)
            request={'dataset_id':dataset['dataset_id'],'policy':{'budget_cents':initial_budget,
                'cover_days':7 if sample==1 else 30,'safety_days':2 if sample==1 else 7}}
            response=client.post('/tasks',json=request); assert response.status_code==202,response.text
            task=response.json()['task_id']; evidence['task_id']=task
            first=wait_task(client,task,'planner'); p=first['latest_plan']
            assert p['version']==1 and len(p['items'])==sample and 0<p['total_cost_cents']<=initial_budget
            if sample==1: assert p['total_cost_cents']==1800 and p['items'][0]['order_qty']==18
            auth={'X-Restock-Approval':env['RESTOCK_APPROVAL_TOKEN']}
            old={'version':1,'plan_hash':first['latest_plan_hash']}
            assert client.post(f'/tasks/{task}/approve',json=old,headers=auth).status_code==200
            expected_policy={**first['latest_plan']['policy'],'budget_cents':changed_budget}
            comment=f'把预算改为{changed_budget//100}元，其他参数保持不变。'
            if sample==30:
                # An unsupported mixed request must fail as a whole, preserving v1.
                before_response=client.get(f'/tasks/{task}/plans'); assert before_response.status_code==200
                before_versions=before_response.json()
                assert client.post(f'/tasks/{task}/revise',json={'comment':'把预算改为3000元，并把商品SKU-001单价改为1元。'}).status_code==202
                started=time.monotonic()
                while time.monotonic()-started<660:
                    rejected=client.get('/tasks/'+task).json()
                    if not rejected['operation']['busy']:break
                    time.sleep(1)
                assert not rejected['operation']['busy'] and rejected['operation']['error']
                evidence['unsupported_request']={'elapsed_s':round(time.monotonic()-started,2),'operation':rejected['operation']}
                assert client.get(f'/tasks/{task}/plans').json()==before_versions
                assert rejected['latest_plan']['version']==1
                expected_policy.update(cover_days=21,safety_days=5,priority_categories=['零食'],excluded_skus=['SKU-001'])
                comment='预算改为3000元，覆盖21天，安全5天，优先零食类别，排除SKU-001。'
            assert client.post(f'/tasks/{task}/revise',json={'comment':comment}).status_code==202
            revised=wait_task(client,task,'reviser'); p=revised['latest_plan']
            assert p['version']==2 and p['policy']==expected_policy
            assert p['dataset_hash']==first['latest_plan']['dataset_hash']
            assert 0<p['total_cost_cents']<=changed_budget
            if sample==1: assert p['total_cost_cents']==3000 and p['items'][0]['order_qty']==30 and p['items'][0]['unmet_qty']==18
            assert revised['approved_plan_hash'] is None
            assert client.post(f'/tasks/{task}/execute').status_code==409
            assert client.post(f'/tasks/{task}/approve',json=old,headers=auth).status_code==409
            approved={'version':2,'plan_hash':revised['latest_plan_hash']}
            assert client.post(f'/tasks/{task}/approve',json=approved,headers=auth).status_code==200
            assert client.post(f'/tasks/{task}/execute').status_code==202
            finished=wait_task(client,task,'executor'); assert finished['state']=='COMPLETED'
            assert all(s['result']['agent']['status']=='ok' for s in evidence['stages'].values())
            events=client.get(f'/tasks/{task}/activity').json()['events']; evidence['activity']=events
            calls=[e['payload'] for e in events if e['kind']=='business_tool' and 'result' in e['payload']]
            assert any(c['stage']=='planner' and c['tool']=='inspect_dataset' for c in calls)
            assert any(c['stage']=='planner' and c['tool']=='build_plan' for c in calls)
            assert any(c['stage']=='reviser' and c['tool']=='revise_plan' for c in calls)
            # P2-9：第四个工具 get_execution_result（executor 阶段）必须覆盖。
            assert any(c['stage']=='executor' and c['tool']=='get_execution_result' for c in calls)
            # The planner event belongs to the immutable v1, never latest v2.
            from tests.summary_assertions import assert_planner_summaries
            assert_planner_summaries(events, {1:first['latest_plan'],2:revised['latest_plan']})
            orders=httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()
            evidence['independent_orders']=orders
            expected={}
            for item in p['items']:
                if item['order_qty']: expected.setdefault(item['supplier_id'],[]).append((item['sku'],item['order_qty'],item['unit_cost_cents']))
            assert len(orders)==len(expected)
            for order in orders:
                assert order['status']=='SUBMITTED' and order['external_ref']==task+'-v2-'+order['supplier_id']
                actual=sorted((i['sku'],i['order_qty'],i['unit_cost_cents']) for i in order['lines'])
                assert actual==sorted(expected[order['supplier_id']])
                assert order['total_cost_cents']==sum(qty*price for _,qty,price in actual)
            if sample==1: assert len(orders)==1 and orders[0]['total_cost_cents']==3000
            assert client.post(f'/tasks/{task}/execute').status_code==409
            assert len(httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json())==len(orders)
            assert 'PO-' in client.get(f'/tasks/{task}/artifacts/2/html').text
            evidence['passed']=True
    finally:
        # Read the isolated task activity even if an assertion rejected a stage.
        if evidence.get('task_id'):
            try:
                response=httpx.get(urls['api']+'/tasks/'+evidence['task_id']+'/activity',trust_env=False,timeout=5)
                if response.status_code==200:evidence['activity']=response.json()['events']
            except httpx.HTTPError:
                pass
        supervisor.stop()
        path=output/('real-chain.json' if sample==1 else 'real-chain-30.json')
        if path.exists():
            previous=json.loads(path.read_text(encoding='utf-8'))
            (output/f"real-chain-{sample}-{previous.get('task_id','previous')}.json").write_bytes(path.read_bytes())
        content=json.dumps(evidence,ensure_ascii=False,indent=2)
        path.write_text(content,encoding='utf-8')
        (output/f"real-chain-{sample}-{evidence.get('task_id',time.time_ns())}.json").write_text(content,encoding='utf-8')
