"""Business MCP acceptance: retain calculation/idempotency checks and enforce scope.

Fixtures use a manually checkable one-SKU upload, not the default 30-SKU file.
Real STDIO + CLI transport is covered by test_agent_runtime.py.
"""
import json
import pytest

from app import db, main
from app.business_tools import ToolSession, active_session
from app.models import Policy, TaskState
from app.validation import prepare_snapshot
from tests.test_data_contract import tables, contract
from tests.test_step3_api import client


@pytest.fixture
def tool_env(tmp_path):
    conn=db.init_db(tmp_path/'tools.db')
    snapshot,errors=prepare_snapshot(*tables(),contract()); assert not errors
    db.upsert_task(conn,'task-mcp',TaskState.ANALYZING)
    db.claim_task(conn,'task-mcp','计算')
    policy=Policy(budget_cents=1800,cover_days=7,safety_days=2)
    session=ToolSession(conn,'task-mcp','planner',snapshot,fixed_policy=policy)
    with session.activate():
        yield session,policy
    conn.close()


def calculate(session,policy):
    session.call('inspect_dataset',{'task_id':session.task_id})
    return session.call('build_plan',{'task_id':session.task_id,'policy':policy.model_dump(),
                                      'dataset_id':session.dataset_id})


def test_inspect_dataset_returns_task_contract_and_hash(tool_env):
    session,_=tool_env
    result=session.call('inspect_dataset',{'task_id':'task-mcp'})
    assert result['sku_count']==1 and result['problems']==[]
    assert result['dataset_id']==session.dataset_id
    assert result['window_start']=='2026-10-01' and result['contract']['quantity_unit']=='piece'


def test_build_plan_generates_amounts_only_from_rules(tool_env):
    session,policy=tool_env
    result=calculate(session,policy)
    assert result['candidate_only'] and not result['reused']
    assert result['plan']['total_cost_cents']==1800
    assert session.draft.items[0].order_qty==18 and session.draft.items[0].unmet_qty==30
    assert db.list_plan_versions(session.conn,'task-mcp')==[]
    session.commit(policy)
    assert db.list_plan_versions(session.conn,'task-mcp')==[1]


def test_repeated_build_and_commit_reuse_same_version(tool_env):
    session,policy=tool_env
    first=calculate(session,policy); second=calculate(session,policy)
    assert first['plan_hash']==second['plan_hash'] and second['plan']['version']==1
    session.commit(policy); session.commit(policy)
    assert db.list_plan_versions(session.conn,'task-mcp')==[1]


@pytest.mark.parametrize('state',[s for s in TaskState if s!=TaskState.ANALYZING])
def test_non_analyzing_task_cannot_be_rebuilt(tool_env,state):
    session,policy=tool_env
    db.upsert_task(session.conn,'task-mcp',state)
    with pytest.raises(ValueError,match='不允许'): calculate(session,policy)
    assert db.list_plan_versions(session.conn,'task-mcp')==[]


@pytest.mark.parametrize('change',[{'task_id':'another-task'}, {'dataset_id':'ds-foreign'},
                                  {'extra':'forbidden'}, {'policy':{'budget_cents':1800}}])
def test_wrong_task_data_or_incomplete_parameters_rejected(tool_env,change):
    session,policy=tool_env
    args={'task_id':'task-mcp','dataset_id':session.dataset_id,'policy':policy.model_dump(),**change}
    with pytest.raises(ValueError): session.call('build_plan',args)
    assert session.draft is None


def test_fixed_budget_cannot_be_changed_by_planner(tool_env):
    session,policy=tool_env
    with pytest.raises(ValueError,match='结构化'):
        calculate(session,policy.model_copy(update={'budget_cents':500000}))
    assert session.draft is None


@pytest.mark.parametrize('field,value',[('priority_categories',['unknown']),('excluded_skus',['unknown'])])
def test_reviser_rejects_unknown_sku_and_category(tool_env,field,value):
    session,policy=tool_env
    reviser=ToolSession(session.conn,session.task_id,'reviser',session.snapshot)
    with reviser.activate(), pytest.raises(ValueError,match='unknown'):
        reviser.call('revise_plan',{'task_id':session.task_id,'policy':{**policy.model_dump(),field:value}})


def test_wrong_stage_and_expired_session_rejected(tool_env):
    session,policy=tool_env
    with pytest.raises(ValueError,match='阶段'): session.call('get_execution_result',{'task_id':session.task_id})
    session.closed=True
    with pytest.raises(ValueError,match='失效'): calculate(session,policy)


def test_changed_state_cannot_commit_an_existing_candidate(tool_env):
    session,policy=tool_env
    calculate(session,policy)
    db.upsert_task(session.conn,session.task_id,TaskState.NEEDS_RECONCILIATION)
    with pytest.raises(ValueError): session.commit(policy)
    assert db.list_plan_versions(session.conn,session.task_id)==[]


def test_tool_audit_records_parameters_results_failures_and_stage(tool_env):
    session,policy=tool_env
    calculate(session,policy)
    with pytest.raises(ValueError): session.call('approve',{'task_id':session.task_id})
    logs=db.list_activity(session.conn,session.task_id)
    assert logs[1]['payload']['arguments']['policy']['budget_cents']==1800
    assert logs[1]['payload']['result']['plan']['total_cost_cents']==1800
    assert logs[1]['payload']['stage']=='planner'
    assert logs[2]['payload']['error']


def test_execution_tools_only_read_bound_approved_version(tool_env):
    session,policy=tool_env; calculate(session,policy); plan,h=session.commit(policy)
    db.upsert_task(session.conn,session.task_id,TaskState.COMPLETED,h,1)
    db.append_execution_event(session.conn,session.task_id,'execution_started',None,{'version':2,'plan_hash':'other'})
    reader=ToolSession(session.conn,session.task_id,'executor',current=plan)
    with reader.activate():
        assert reader.call('get_execution_result',{'task_id':session.task_id})['records']==[]
        assert reader.call('get_execution_result',{'task_id':session.task_id})['events']==[]
        with pytest.raises(ValueError): reader.call('build_plan',{'task_id':session.task_id,'policy':policy.model_dump()})


def test_unchanged_policy_revision_returns_existing_version(client):
    first=client.post('/tasks',json={'policy':{'budget_cents':1800}}).json()
    task=first['task_id']
    response=client.post(f'/tasks/{task}/revise',json={'policy':first['plan']['policy']})
    assert response.status_code==200,response.text
    assert response.json()['plan']['version']==1
    assert db.list_plan_versions(main.get_conn(),task)==[1]


@pytest.mark.parametrize('outcome',['unsupported','malformed','mismatch','timeout'])
def test_model_failure_after_tool_call_never_publishes_candidate(client,monkeypatch,outcome):
    first=client.post('/tasks',json={'policy':{'budget_cents':10000}}).json()
    task=first['task_id']; client.post(f'/tasks/{task}/approve')
    def bad_model(stage,context,user_input):
        ctx=json.loads(context)
        policy={**ctx['current_policy'],'budget_cents':1800}
        active_session().call('revise_plan',{'task_id':task,'dataset_id':ctx['dataset_id'],'policy':policy})
        if outcome=='timeout': raise main.AgentStageError('test timeout after calculation')
        summary={'unsupported':{'policy':None,'explanation':'不支持','unsupported_requests':['指定供应商']},
                 'mismatch':{'policy':ctx['current_policy'],'explanation':'故障注入','unsupported_requests':[]}}
        return {'status':'ok','summary':'not json' if outcome=='malformed' else json.dumps(summary[outcome])}
    monkeypatch.setattr(main,'_run_agent_stage',bad_model)
    response=client.post(f'/tasks/{task}/revise',json={'comment':'预算改18元，并指定其他供应商'})
    assert response.status_code==422
    assert db.list_plan_versions(main.get_conn(),task)==[1]
    status=client.get(f'/tasks/{task}').json()
    assert status['approved_plan_hash'] is None and status['latest_plan']['version']==1


def test_structured_request_cannot_accept_a_wrong_budget_from_model(client,monkeypatch):
    def bad_model(stage,context,user_input):
        ctx=json.loads(context)
        active_session().call('inspect_dataset',{'task_id':ctx['task_id']})
        active_session().call('build_plan',{'task_id':ctx['task_id'],'dataset_id':ctx['dataset_id'],
            'policy':{**ctx['policy'],'budget_cents':500000}})
    monkeypatch.setattr(main,'_run_agent_stage',bad_model)
    response=client.post('/tasks',json={'policy':{'budget_cents':1800}})
    assert response.status_code==503
    task=client.get('/tasks').json()['tasks'][0]['task_id']
    assert db.list_plan_versions(main.get_conn(),task)==[]


def test_explicit_offline_creation_uses_rules_without_model(client,monkeypatch):
    # Undo only the fixture's model stub, retaining its isolated database.
    monkeypatch.setattr(main,'_run_agent_stage',ORIGINAL_AGENT_STAGE)
    monkeypatch.setenv('RESTOCK_AGENT_MODE','off')
    monkeypatch.setattr(main.runner,'run_codex_task',lambda **kw: pytest.fail('offline called CLI'))
    response=client.post('/tasks',json={'policy':{'budget_cents':1800}})
    assert response.status_code==200,response.text
    assert response.json()['agent']['status']=='disabled'


def test_runtime_configuration_error_does_not_erase_executed_orders(client,monkeypatch):
    import sys
    first=client.post('/tasks',json={'policy':{'budget_cents':10000}}).json()
    task=first['task_id']; client.post(f'/tasks/{task}/approve')
    monkeypatch.setattr(main,'_run_agent_stage',ORIGINAL_AGENT_STAGE)
    monkeypatch.setenv('RESTOCK_AGENT_MODE','required')
    monkeypatch.setenv('RESTOCK_LITELLM_URL','https://not-a-local-gateway.invalid')
    monkeypatch.setattr(main.runner,'resolve_codex_bin',lambda _:sys.executable)
    response=client.post(f'/tasks/{task}/execute')
    assert response.status_code==200,response.text
    assert response.json()['state']=='COMPLETED' and response.json()['agent']['status']=='error'
    assert client.get(f'/tasks/{task}/executions').json()['records']


def test_wrong_currency_summary_does_not_publish_planner_candidate(client,monkeypatch):
    def bad_summary(stage,context,user_input):
        ctx=json.loads(context)
        active_session().call('inspect_dataset',{'task_id':ctx['task_id']})
        active_session().call('build_plan',{'task_id':ctx['task_id'],'dataset_id':ctx['dataset_id'],'policy':ctx['policy']})
        return {'status':'ok','summary':'全局预算 500,000 美分。'}
    monkeypatch.setattr(main,'_run_agent_stage',bad_summary)
    response=client.post('/tasks',json={'policy':{'budget_cents':500000}})
    assert response.status_code==503 and '人民币分' in response.text
    task=client.get('/tasks').json()['tasks'][0]['task_id']
    assert db.list_plan_versions(main.get_conn(),task)==[]


def test_planner_numeric_review_uses_authoritative_risk_intersection(tool_env):
    from app.business_tools import planner_review
    session,policy=tool_env; calculate(session,policy)
    plan=session.draft
    # A is at risk but receives a partial purchase, so it is not budget-skipped.
    text=planner_review(plan,'建议复核低置信商品及到货前缺货风险。')
    assert '方案金额 18.00 元' in text and '预算跳过 0 种' in text
    assert '缺货风险 1 种，其中被预算跳过 0 种' in text
    with pytest.raises(ValueError): planner_review(plan,'跳过了12个风险商品')


ORIGINAL_AGENT_STAGE=main._run_agent_stage
