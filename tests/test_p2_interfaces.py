"""P2 acceptance at backend publish boundaries; mocks prove wiring, not semantics."""
import json
import pytest
from app import main,db
from app.business_tools import active_session
from tests.test_step3_api import client
from tests.test_data_contract import write_dataset
REAL_STAGE = main._run_agent_stage


@pytest.mark.parametrize('advice,accepted',[
    ('建议人工复核缺货风险。',True),('建议增加两天安全库存。',False),
    ('建议提高库存至叁箱。',False),('采购金额壹佰美元',False)])
def test_planner_advice_guard_at_publish_boundary(client,tmp_path,monkeypatch,advice,accepted):
    write_dataset(tmp_path);monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    def model(stage,context,prompt):
        ctx=json.loads(context);session=active_session()
        assert 'error' not in session.call('inspect_dataset',{'task_id':ctx['task_id']})
        assert 'error' not in session.call('build_plan',{'task_id':ctx['task_id'],'dataset_id':ctx['dataset_id'],'policy':ctx['policy']})
        return {'status':'ok','summary':advice}
    monkeypatch.setattr(main,'_run_agent_stage',model)
    response=client.post('/tasks',json={'policy':{'budget_cents':1800,'cover_days':7,'safety_days':2}})
    assert response.status_code==(200 if accepted else 503)
    task=db.list_tasks(main.get_conn())[0]['task_id']
    assert db.list_plan_versions(main.get_conn(),task)==([1] if accepted else [])


@pytest.mark.parametrize('field,value,comment',[
    ('budget_cents',3000,'预算改为30元'),('cover_days',14,'覆盖改为14天'),
    ('safety_days',1,'安全天数改为1天'),('priority_categories',['cat'],'优先cat类别'),
    ('excluded_skus',['A'],'排除A')])
def test_supported_policy_fields_publish_through_revision_api(client,tmp_path,monkeypatch,field,value,comment):
    write_dataset(tmp_path);monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    first=client.post('/tasks',json={'policy':{'budget_cents':1800,'cover_days':7,'safety_days':2}}).json()
    task=first['task_id'];expected={**first['plan']['policy'],field:value}
    # The standard sample uses its actual category; avoid an unknown-category mock.
    if field=='priority_categories':
        expected[field]=[first['plan']['dataset_snapshot']['inventory'][0]['category']]
    def model(stage,context,prompt):
        ctx=json.loads(context);assert stage=='reviser'
        result=active_session().call('revise_plan',{'task_id':task,'dataset_id':ctx['dataset_id'],'policy':expected})
        assert 'error' not in result
        return {'status':'ok','summary':json.dumps({'policy':expected,'unsupported_requests':[],'explanation':'按要求修改'})}
    monkeypatch.setattr(main,'_run_agent_stage',model)
    response=client.post(f'/tasks/{task}/revise',json={'comment':comment})
    assert response.status_code==200,response.text
    assert response.json()['plan']['policy']==expected
    assert response.json()['plan']['version']==2


@pytest.mark.parametrize('comment',['预算改为30元并修改单价','指定供应商','至少采购20件','自动批准采购'])
def test_unsupported_mixed_requests_do_not_publish(client,tmp_path,monkeypatch,comment):
    write_dataset(tmp_path);monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    first=client.post('/tasks',json={'policy':{'budget_cents':1800,'cover_days':7,'safety_days':2}}).json()
    task=first['task_id']
    monkeypatch.setattr(main,'_run_agent_stage',lambda *a:{'status':'ok','summary':json.dumps({'policy':None,'unsupported_requests':[comment],'explanation':'不支持'})})
    response=client.post(f'/tasks/{task}/revise',json={'comment':comment})
    assert response.status_code==422
    assert db.list_plan_versions(main.get_conn(),task)==[1]


def test_cover_days_below_supported_minimum_is_rejected(client,tmp_path,monkeypatch):
    write_dataset(tmp_path);monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    response=client.post('/tasks',json={'policy':{'budget_cents':1800,'cover_days':5,'safety_days':2}})
    assert response.status_code==422
    assert db.list_tasks(main.get_conn())==[]


def test_rejected_real_runner_output_is_saved_before_plan_validation(client,tmp_path,monkeypatch):
    from app import runner
    write_dataset(tmp_path);monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    monkeypatch.setenv('RESTOCK_AGENT_MODE','required')
    def cli(**kwargs):
        session=active_session()
        session.call('inspect_dataset',{'task_id':session.task_id})
        session.call('build_plan',{'task_id':session.task_id,'dataset_id':session.dataset_id,'policy':session.fixed_policy.model_dump()})
        return runner.CodexRunResult(0,[runner.CodexEvent({'type':'item.completed','item':{'type':'agent_message','text':'建议补两天库存'}})],'',0.1)
    monkeypatch.setattr(main,'_run_agent_stage',REAL_STAGE)
    monkeypatch.setattr(runner,'run_codex_task',cli)
    response=client.post('/tasks',json={'policy':{'budget_cents':1800,'cover_days':7,'safety_days':2}})
    assert response.status_code==503
    task=db.list_tasks(main.get_conn())[0]['task_id']
    assert db.list_plan_versions(main.get_conn(),task)==[]
    trace=[e['payload'] for e in db.list_activity(main.get_conn(),task) if e['kind']=='agent_response']
    assert len(trace)==1 and trace[0]['input']=='建议补两天库存'
    assert trace[0]['stage']=='planner' and trace[0]['operation_id'] and trace[0]['dataset_id']
