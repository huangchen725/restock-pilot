"""Cycle-specific model advice is a closed, validated decision, not extra policy."""
import json
import pytest
from app import main,db
from app.business_tools import planner_review
from tests.test_step3_api import client
from tests.test_collection import clock
from tests.test_replenishment_schedule import prepared,setup,tick,group,manual_scheduler


def test_cycle_planner_rejects_unimplemented_budget_carryover(client,clock):
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid)
    tid=group(client,gid)['state']['task_id'];plan,_=db.get_plan(main.get_conn(),tid,1)
    with pytest.raises(ValueError):planner_review(plan,'剩余预算也可留至后续周期使用。')


@pytest.mark.parametrize('invalid',['free_text','extra_field','unknown_code','repeated','wrong_type','inapplicable','duplicate_key'])
def test_bad_cycle_advice_never_publishes_candidate_and_keeps_original(client,clock,monkeypatch,invalid):
    sid=prepared(client,clock);gid=setup(client,sid);original=main._run_agent_stage
    values={'free_text':'剩余预算也可留至后续周期使用。',
        'duplicate_key':'{"advice_codes":["review_parameters"],"advice_codes":["review_future_budget"]}',
        'extra_field':json.dumps({'advice_codes':['review_parameters'],'carryover':True}),
        'unknown_code':json.dumps({'advice_codes':['carry_over_budget']}),
        'repeated':json.dumps({'advice_codes':['review_parameters','review_parameters']}),
        'wrong_type':json.dumps({'advice_codes':'review_parameters'}),
        'inapplicable':json.dumps({'advice_codes':['review_low_confidence']})}
    def bad(stage,ctx,request):
        result=original(stage,ctx,request);result['summary']=values[invalid];return result
    monkeypatch.setattr(main,'_run_agent_stage',bad);tick(gid)
    tid=group(client,gid)['state']['task_id'];assert db.list_plan_versions(main.get_conn(),tid)==[]
    events=db.list_activity(main.get_conn(),tid)
    rejected=[e['payload'] for e in events if e['kind']=='planner_rejected']
    assert rejected and rejected[-1]['model_original']==values[invalid]
    assert client.get('/tasks/'+tid).json()['approved_version'] is None


def test_cycle_advice_renders_supported_choices_and_preserves_raw_decision(client,clock):
    sid=prepared(client,clock);gid=setup(client,sid);tick(gid)
    tid=group(client,gid)['state']['task_id'];task=client.get('/tasks/'+tid).json()
    assert task['latest_plan'] is not None
    agent=task['operation']['result']['agent']
    assert json.loads(agent['model_advice'])=={'advice_codes':['review_parameters']}
    assert '模型建议：' in agent['summary'] and '原周期额度' in agent['summary']
