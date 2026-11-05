"""Independent readback/export checks against the exact human-approved plan."""
import csv
import hashlib
import io
from pathlib import Path


def check_exports(output, exports, plan, task, plan_hash, orders):
    assert set(exports)=={'csv','html'}
    texts={}
    for kind,export in exports.items():
        path=Path(output)/export['path']
        assert path.resolve().parent==Path(output).resolve()
        data=path.read_bytes()
        assert hashlib.sha256(data).hexdigest()==export['sha256']
        texts[kind]=data.decode('utf-8-sig')
    rows=list(csv.DictReader(io.StringIO(texts['csv'])))
    assert len(rows)==len(plan['items'])>0
    assert {r['方案编号'] for r in rows}=={task}
    assert {r['版本'] for r in rows}=={str(plan['version'])}
    assert {r['数据版本'] for r in rows}=={plan['dataset_hash']}
    exported=sorted((r['SKU'],r['供应商'],int(r['采购数量']),int(r['单价（分）']),int(r['金额（分）'])) for r in rows)
    expected=sorted((i['sku'],i['supplier_id'],i['order_qty'],i['unit_cost_cents'],i['line_total_cents']) for i in plan['items'])
    assert exported==expected
    assert sum(int(r['金额（分）']) for r in rows)==plan['total_cost_cents']
    html=texts['html']
    assert task in html and plan_hash in html and plan['dataset_hash'] in html
    assert f"合计 {plan['total_cost_cents']} 分" in html
    assert 'SUBMITTED' in html and all(order['order_id'] in html for order in orders)


def check_complete(business, output, mode):
    assert business['mode']==mode and business['sample_skus']==30
    assert business['failure_kind'] is None and not business['safety_failures']
    task=business['task_id'];assert task
    first=business['planner']['latest_plan'];plan=business['reviser']['latest_plan']
    assert first['version']==1 and plan['version']==2
    assert first['dataset_hash']==plan['dataset_hash'] and plan['plan_id']==task
    assert len(plan['items'])==30 and set(business['dataset_files'])=={'sales','inventory','suppliers'}
    assert business['reviser']['approved_plan_hash'] is None
    for version,phase in ((1,'planner'),(2,'reviser')):
        approved=business['approval_v'+str(version)]
        assert approved['approved_version']==version
        if business.get('continuous_execution') and version==2:
            assert approved['state'] in ('APPROVED','EXECUTING','COMPLETED')
            intent=approved['execution_intent']
            assert intent['task_id']==task and intent['version']==version
            assert intent['plan_hash']==business[phase]['latest_plan_hash'] and intent['dataset_id']==plan['dataset_id']
            assert business['execution_clicks']==0
            assert any(e['kind']=='approved' and e['payload'].get('source')=='human_workbench'
                       and e['payload'].get('request_id')==intent['request_id']
                       and e['payload'].get('version')==version and e['payload'].get('plan_hash')==intent['plan_hash']
                       for e in business['activity'])
        else:
            assert approved['state']=='APPROVED'
        assert approved['approved_plan_hash']==business[phase]['latest_plan_hash']
    assert business['final_state']['state']=='COMPLETED'
    assert business['final_state']['latest_plan_hash']==business['reviser']['latest_plan_hash']
    expected={}
    for item in plan['items']:
        assert item['line_total_cents']==item['order_qty']*item['unit_cost_cents']
        if item['order_qty']:
            expected.setdefault(item['supplier_id'],[]).append((item['sku'],item['order_qty'],item['unit_cost_cents']))
    orders=business['independent_orders']
    assert len(orders)==len(expected)>0
    assert len({o['order_id'] for o in orders})==len(orders)
    assert len({o['external_ref'] for o in orders})==len(orders)
    assert {o['supplier_id'] for o in orders}==set(expected)
    for order in orders:
        assert order['status']=='SUBMITTED' and order['external_ref']==task+'-v2-'+order['supplier_id']
        lines=sorted((i['sku'],i['order_qty'],i['unit_cost_cents']) for i in order['lines'])
        assert lines==sorted(expected[order['supplier_id']])
        assert order['total_cost_cents']==sum(q*p for _,q,p in lines)
    assert sum(o['total_cost_cents'] for o in orders)==plan['total_cost_cents']<=plan['policy']['budget_cents']
    assert business['unauthorized_execute_http']==409 and business['stale_execute_http']==409
    assert business['repeat_execute_http']==409 and business['orders_after_repeat']==orders
    assert business['orders_before_approval']==[] and business['orders_after_revision']==[]
    if mode=='required':
        assert business['model']=='intern-s2'
        for phase in ('planner','reviser','executor'):
            assert business[phase]['operation']['result']['agent']['status']=='ok'
        calls=[e['payload']['tool'] for e in business['activity'] if e['kind']=='business_tool' and 'result' in e['payload']]
        assert {'inspect_dataset','build_plan','revise_plan','get_execution_result'}<=set(calls)
    check_exports(output,business['exports'],plan,task,business['reviser']['latest_plan_hash'],orders)
