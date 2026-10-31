import io
import json
import sqlite3
import threading
import time

import pytest

from app import db, main
from tests.test_data_contract import tables, contract, write_dataset
from tests.test_step3_api import client


@pytest.fixture
def dataset(client,tmp_path,monkeypatch):
    write_dataset(tmp_path); monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    return client.post('/datasets/example').json()


def create(client,dataset,**extra):
    r=client.post('/tasks',json={'dataset_id':dataset['dataset_id'],'policy':{'budget_cents':10000},**extra})
    assert r.status_code==200,r.text
    return r.json()


def test_upload_validation_repair_and_immutable_storage(client,tmp_path):
    write_dataset(tmp_path)
    files={n:("../../untrusted.csv",(tmp_path/(n+'.csv')).read_bytes(),'text/csv') for n in ('sales','inventory','suppliers')}
    r=client.post('/datasets',files=files,data={'contract':json.dumps({})}).json()
    assert r['state']=='NEEDS_INPUT' and r['dataset_id'] is None
    r=client.post('/datasets',files=files,data={'contract':contract().model_dump_json()}).json()
    assert r['state']=='READY' and r['sku_count']==1
    original=client.get('/datasets/'+r['dataset_id']).json()
    files['inventory']=('inventory.csv',files['inventory'][1].replace(b',0,0,',b',12,0,'),'text/csv')
    new=client.post('/datasets',files=files,data={'contract':contract().model_dump_json()}).json()
    assert new['dataset_id']!=r['dataset_id']
    assert client.get('/datasets/'+r['dataset_id']).json()==original


def test_upload_size_is_bounded(client,monkeypatch):
    monkeypatch.setattr(main.datasets,'MAX_FILE_BYTES',10)
    files={n:(n+'.csv',b'x'*20,'text/csv') for n in ('sales','inventory','suppliers')}
    result=client.post('/datasets',files=files,data={'contract':contract().model_dump_json()}).json()
    assert result['state']=='NEEDS_INPUT' and len(result['data_problems'])==3


def test_full_workbench_api_flow_diff_approval_execution_exports(client,dataset,monkeypatch):
    first=create(client,dataset)
    task=first['task_id']
    assert client.post(f'/tasks/{task}/approve',json={'version':1,'plan_hash':first['plan_hash']},headers={'X-Restock-Approval':''}).status_code==403
    assert client.post(f'/tasks/{task}/approve',json={}).status_code==422
    assert client.post(f'/tasks/{task}/approve').status_code==200
    policy={**first['plan']['policy'],'budget_cents':1800}
    revised=client.post(f'/tasks/{task}/revise',json={'policy':policy}).json()
    assert revised['plan']['version']==2 and revised['plan']['total_cost_cents']==1800
    diff=client.get(f'/tasks/{task}/diff?from_version=1&to_version=2').json()
    assert diff['policy_changes']['budget_cents']=={'before':10000,'after':1800}
    # At 100 cents each and packs of six, the first budget buys 96 units.
    assert diff['total_delta_cents']==-7800
    assert diff['items'][0]['after_qty']==18
    assert client.post(f'/tasks/{task}/approve',json={'version':1,'plan_hash':first['plan_hash']}).status_code==409
    assert client.post(f'/tasks/{task}/execute').status_code==409
    assert client.post(f'/tasks/{task}/approve').status_code==200
    assert client.post(f'/tasks/{task}/execute').json()['state']=='COMPLETED'
    assert client.post(f'/tasks/{task}/execute').status_code==409
    assert client.get(f'/tasks/{task}/plans/1').json()['plan']==first['plan']
    assert len(client.get(f'/tasks/{task}/plans').json()['versions'])==2
    # Close and reopen the actual database; no in-memory task/session cache.
    path=main.get_conn().execute('PRAGMA database_list').fetchone()[2]
    monkeypatch.setattr(main,'DB_PATH',path)
    main._conn.close(); main._conn=None; main._tasks={}
    status=client.get(f'/tasks/{task}').json()
    assert status['operation']['result']['execution']['total']==1
    assert client.get('/datasets/'+dataset['dataset_id']).json()['sku_count']==1
    events=client.get(f'/tasks/{task}/activity').json()['events']
    assert any(e['kind']=='approved' and e['payload']['version']==2 for e in events)
    csv=client.get(f'/tasks/{task}/artifacts/2/csv')
    assert csv.status_code==200 and '18,100,1800' in csv.content.decode('utf-8-sig')
    html=client.get(f'/tasks/{task}/artifacts/2/html')
    assert 'PO-S' in html.text and 'SUBMITTED' in html.text and '1800' in html.text
    old=client.get(f'/tasks/{task}/artifacts/1/html').text
    assert 'PO-S' not in old


def test_background_request_returns_before_model_and_blocks_conflicts(client,dataset,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    def slow(stage, context, user_input):
        entered.set(); release.wait(5)
        if stage == "planner":
            # 慢速模拟也需产生 MCP build_plan 的副作用，否则 main 读不到方案会 FAILED
            from app.business_tools import active_session
            ctx = json.loads(context)
            active_session().call('inspect_dataset', {'task_id':ctx['task_id']})
            active_session().call('build_plan', {'task_id':ctx['task_id'], 'dataset_id':ctx['dataset_id'], 'policy':ctx['policy']})
        return {'status':'ok','summary':'持久化摘要','tool_calls':[],'duration_s':1}
    monkeypatch.setattr(main,'_run_agent_stage',slow)
    try:
        begin=time.monotonic()
        response=client.post('/tasks',headers={'Prefer':''},json={'dataset_id':dataset['dataset_id'],'policy':{'budget_cents':10000}})
        assert response.status_code==202 and time.monotonic()-begin<1
        task=response.json()['task_id']; assert entered.wait(3)
        status=client.get(f'/tasks/{task}').json()
        assert status['operation']['busy']==1 and status['operation']['phase']=='生成方案与复核摘要'
        assert client.post(f'/tasks/{task}/approve').status_code==409
        assert client.post(f'/tasks/{task}/revise',json={'policy':{'budget_cents':1000}}).status_code==409
    finally: release.set()
    for _ in range(100):
        status=client.get(f'/tasks/{task}').json()
        if not status['operation']['busy']: break
        time.sleep(.02)
    assert status['state']=='NEEDS_REVIEW'
    assert status['operation']['result']['agent']['model_advice']=='持久化摘要'
    assert status['operation']['result']['agent']['summary'].endswith('模型建议：持久化摘要')


def test_reupload_to_current_task_invalidates_approval(client,dataset,tmp_path):
    first=create(client,dataset); task=first['task_id']; client.post(f'/tasks/{task}/approve')
    data=tables(); data[1][0]['available_qty']='100'
    write_dataset(tmp_path,data)
    other=client.post('/datasets/example').json()
    updated=client.post(f'/tasks/{task}/revise',json={'dataset_id':other['dataset_id']}).json()
    assert updated['plan']['version']==2 and updated['plan']['dataset_hash']!=first['plan']['dataset_hash']
    assert client.get(f'/tasks/{task}').json()['approved_plan_hash'] is None
    assert client.get(f'/tasks/{task}/diff?from_version=1&to_version=2').json()['dataset_changed']


def test_background_execution_rejects_overlapping_writes(client,dataset,monkeypatch):
    first=create(client,dataset); task=first['task_id']
    client.post(f'/tasks/{task}/approve')
    entered,release=threading.Event(),threading.Event()
    original=main.PlaywrightProcurementAdapter.execute_plan
    calls=[]
    def slow(adapter,plan,on_order_result=None):
        calls.append(plan.version); entered.set(); release.wait(5)
        return original(adapter,plan,on_order_result=on_order_result)
    monkeypatch.setattr(main.PlaywrightProcurementAdapter,'execute_plan',slow)
    try:
        assert client.post(f'/tasks/{task}/execute',headers={'Prefer':''}).status_code==202
        assert entered.wait(3)
        assert client.post(f'/tasks/{task}/execute').status_code==409
        assert client.post(f'/tasks/{task}/revise',json={'policy':{'budget_cents':1000}}).status_code==409
        assert client.post(f'/tasks/{task}/approve').status_code==409
    finally: release.set()
    for _ in range(100):
        status=client.get(f'/tasks/{task}').json()
        if not status['operation']['busy']: break
        time.sleep(.02)
    assert status['state']=='COMPLETED' and calls==[1]


def test_natural_request_is_parsed_and_persisted(client,dataset):
    result=create(client,dataset,user_request='覆盖天数减少一天')
    assert client.agent_calls==['reviser','planner']
    assert result['plan']['policy']['cover_days']==29
    status=client.get('/tasks/'+result['task_id']).json()
    assert status['operation']['request']['user_request']=='覆盖天数减少一天'


def test_needs_input_can_continue_with_new_data(client,dataset,tmp_path):
    (tmp_path/'sales.csv').write_text('bad')
    broken=client.post('/tasks',json={'policy':{'budget_cents':10000}}).json()
    assert broken['state']=='NEEDS_INPUT'
    assert client.get('/tasks/'+broken['task_id']).json()['operation']['result']['data_problems']
    repaired=client.post('/tasks/'+broken['task_id']+'/revise',json={'dataset_id':dataset['dataset_id']}).json()
    assert repaired['state']=='NEEDS_REVIEW' and repaired['plan']['version']==1


def test_reports_escape_untrusted_text_and_csv_formulas(client,dataset):
    created=create(client,dataset); task=created['task_id']
    plan,_=db.get_plan(main.get_conn(),task,1)
    plan.items[0].name='=HYPERLINK("https://bad")<script>alert(1)</script>'
    db.save_plan(main.get_conn(),plan)
    text=client.get(f'/tasks/{task}/artifacts/1/html').text
    assert '<script>' not in text and '&lt;script&gt;' in text
    text=client.get(f'/tasks/{task}/artifacts/1/csv').content.decode('utf-8-sig')
    assert "'=HYPERLINK" in text


def test_migration_preserves_existing_rows_and_restart_marks_unknown(tmp_path):
    path=tmp_path/'old.db'
    conn=sqlite3.connect(path)
    conn.executescript("CREATE TABLE tasks(task_id TEXT PRIMARY KEY,state TEXT,approved_plan_hash TEXT,approved_version INTEGER,created_at TEXT,updated_at TEXT); INSERT INTO tasks VALUES ('old','EXECUTING','hash',1,'old-date','old-date');")
    conn.close(); conn=db.init_db(path)
    try:
        assert db.get_task(conn,'old')['approved_plan_hash']=='hash'
        # A legacy EXECUTING row has no M1 context at all; it is still unknown.
        db.upsert_task(conn,'queued',main.TaskState.NEW)
        assert db.claim_task(conn,'queued','检查资料')
        db.recover_interrupted(conn)
        assert db.get_task(conn,'old')['state']=='NEEDS_RECONCILIATION'
        assert db.get_context(conn,'old')['busy']==0
        assert db.get_context(conn,'old')['error']
        assert db.get_task(conn,'old')['created_at']=='old-date'
        assert db.get_task(conn,'queued')['state']=='FAILED'
        assert db.get_context(conn,'queued')['busy']==0
    finally: conn.close()
