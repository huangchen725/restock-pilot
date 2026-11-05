"""UR-01/02/03: port only user-facing features onto the current core."""
import io
import json
import zipfile
from pathlib import Path

import pytest
from app import db, main
from tests.test_step3_api import client
from tests.test_data_contract import write_dataset


def test_native_entries_use_existing_installation_core():
    root=Path(__file__).resolve().parents[1]
    assert (root/'RestockPilot.vbs').is_file()
    assert (root/'安装或修复 RestockPilot.vbs').is_file()
    assert (root/'关闭 RestockPilot.vbs').is_file()
    assert not (root/'runtime-manifest.json').exists()
    assert not (root/'app/process_owner.py').exists()


def test_csv_template_is_a_valid_explained_download(client):
    response=client.get('/dataset-templates')
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert set(z.namelist())=={'sales.csv','inventory.csv','suppliers.csv','填写说明.txt'}
        assert '演示' in z.read('填写说明.txt').decode('utf-8')
        files={n:(n+'.csv',z.read(n+'.csv'),'text/csv') for n in ('sales','inventory','suppliers')}
    from tests.test_data_contract import contract
    uploaded=client.post('/datasets',files=files,data={'contract':contract().model_dump_json()})
    assert uploaded.status_code==200 and uploaded.json()['sku_count']==30


def test_failed_generation_can_retry_but_purchase_cannot(client,monkeypatch):
    original=main._run_agent_stage
    def unavailable(*args,**kwargs):raise main.AgentStageError('planner unavailable')
    monkeypatch.setattr(main,'_run_agent_stage',unavailable)
    first=client.post('/tasks',json={'policy':{'budget_cents':500000}})
    assert first.status_code==503
    task=client.get('/tasks').json()['tasks'][0]['task_id']
    state=client.get('/tasks/'+task).json()
    assert state['state']=='FAILED' and state['latest_plan'] is None
    req=state['operation']['request']
    assert req.get('dataset_id'), 'retry must retain the exact resolved dataset'
    assert client.post('/tasks/'+task+'/retry',json=req,headers={'X-Restock-Approval':''}).status_code==403
    monkeypatch.setattr(main,'_run_agent_stage',original)
    retried=client.post('/tasks/'+task+'/retry',json=req)
    assert retried.status_code==200,retried.text
    assert retried.json()['plan']['version']==1
    assert client.post('/tasks/'+task+'/approve').status_code==200
    assert client.post('/tasks/'+task+'/execute').status_code==200
    assert client.post('/tasks/'+task+'/retry',json=req).status_code==409
    assert len(client.get('/tasks').json()['tasks'])==1


def test_revision_facts_override_misleading_prose_without_changing_policy(client,tmp_path,monkeypatch):
    write_dataset(tmp_path);monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    first=client.post('/tasks',json={'policy':{'budget_cents':10000,'cover_days':8,'safety_days':2}}).json()
    task=first['task_id'];original=main._run_agent_stage
    wrong='因供应或库存限制，预算还剩很多但无法采购'
    def model(stage,*args):
        r=original(stage,*args)
        if stage=='reviser':
            body=json.loads(r['summary']);body['explanation']=wrong
            r['summary']=json.dumps(body,ensure_ascii=False)
        return r
    monkeypatch.setattr(main,'_run_agent_stage',model)
    revised=client.post('/tasks/'+task+'/revise',json={'comment':'覆盖天数减一'}).json()
    assert first['plan']['total_cost_cents']==5400
    assert revised['plan']['total_cost_cents']==4800
    explanation=json.loads(revised['agent']['summary'])['explanation']
    assert '程序生成' in explanation and '48.00 元' in explanation and wrong not in explanation
    assert wrong in revised['agent']['raw_summary']
    event=[e for e in client.get('/tasks/'+task+'/activity').json()['events'] if e['kind']=='reviser'][-1]
    assert wrong in event['payload']['model_original']


def test_successful_later_attempt_cannot_hide_interrupted_history(client):
    first=client.post('/tasks',json={'policy':{'budget_cents':500000}}).json();task=first['task_id']
    client.post('/tasks/'+task+'/approve');client.post('/tasks/'+task+'/execute')
    original=client.get('/tasks/'+task+'/audit-trail').json()
    assert original['replay_complete']
    # Fault injection: the first process died before its final journal event.
    with db.transaction(main.get_conn()):
        main.get_conn().execute('DELETE FROM execution_events WHERE seq=?',(original['events'][-1]['seq'],))
    # A later otherwise complete recovery must not erase the missing first end.
    for event in original['events']:
        payload={**event['payload'],'attempt_id':'later-recovery'}
        db.append_execution_event(main.get_conn(),task,event['event_type'],event['external_ref'],payload)
    trail=client.get('/tasks/'+task+'/audit-trail').json()
    assert not trail['replay_complete']
    assert any('前一次尝试尚未结束' in s for s in trail['issues'])
    assert client.get('/tasks/'+task).json()['state']=='COMPLETED'


from tests.test_collection import clock

def test_retry_keeps_source_binding_and_rejects_pending_new_data(client,clock,monkeypatch):
    from tests.test_collection import source,ready,observe,batch,publish
    from datetime import timedelta
    sid=source(client);did=ready(client,clock,sid)
    original=main._run_agent_stage
    def fail(*args):raise main.AgentStageError('synthetic planning failure')
    monkeypatch.setattr(main,'_run_agent_stage',fail)
    assert client.post('/tasks',json={'source_id':sid,'dataset_id':did,'policy':{'budget_cents':10000}}).status_code==503
    task=client.get('/tasks').json()['tasks'][0]['task_id']
    request=client.get('/tasks/'+task).json()['operation']['request']
    assert client.post('/tasks/'+task+'/retry',json={**request,'source_id':None}).status_code==409
    clock[0]+=timedelta(seconds=1)
    assert observe(client,sid,batch(clock,'new',stock='12')).status_code==200
    assert client.post('/tasks/'+task+'/retry',json=request).status_code==409
    assert client.get('/tasks/'+task+'/plans').json()['versions']==[]
    clock[0]+=timedelta(seconds=60)
    newest=publish(client,sid).json()['dataset_id']
    monkeypatch.setattr(main,'_run_agent_stage',original)
    retried=client.post('/tasks/'+task+'/retry',json={**request,'dataset_id':newest})
    assert retried.status_code==200,retried.text
    assert retried.json()['plan']['dataset_id']==newest and retried.json()['plan']['version']==1
    assert db.task_source(main.get_conn(),task)==sid


def test_revision_explanation_failure_cannot_publish_partial_candidate(client,monkeypatch):
    first=client.post('/tasks',json={'policy':{'budget_cents':500000}}).json();task=first['task_id']
    def fail(*args):raise ValueError('synthetic rendering failure')
    monkeypatch.setattr(main,'revision_review',fail)
    rejected=client.post('/tasks/'+task+'/revise',json={'policy':{**first['plan']['policy'],'budget_cents':300000}})
    assert rejected.status_code==422
    state=client.get('/tasks/'+task).json()
    assert state['latest_plan_hash']==first['plan_hash'] and state['latest_plan']['version']==1
    assert state['approved_plan_hash'] is None
    assert not any(e['kind']=='reviser' and e['payload']['version']==2 for e in client.get('/tasks/'+task+'/activity').json()['events'])
