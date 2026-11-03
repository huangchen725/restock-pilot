"""Hand-written controls for the real-model acceptance oracle (P2-9)."""
from copy import deepcopy
import pytest
from tests.summary_assertions import assert_planner_summaries


def sample():
    plan = {'version':1,'policy':{'budget_cents':1000},'total_cost_cents':0,'budget_remaining_cents':1000,
        'items':[{'sku':'A','stockout_before_arrival':True,'confidence':'low','order_qty':0}], 'skipped':['A']}
    text = '全局预算 10.00 元，方案金额 0.00 元，余额 10.00 元。采购 0 种商品，预算跳过 1 种；低置信商品 1 种。预计到货前缺货风险 1 种，其中被预算跳过 1 种。金额为人民币。模型建议：建议人工复核。'
    return plan, {'kind':'planner','payload':{'version':1,'summary':text}}


def test_correct_v1_is_compared_to_v1_even_when_v2_exists():
    v1,event=sample();v2=deepcopy(v1);v2.update(version=2,skipped=[])
    assert_planner_summaries([event],{1:v1,2:v2})
    e2=deepcopy(event);e2['payload']['version']=2
    e2['payload']['summary']=e2['payload']['summary'].replace('预算跳过 1','预算跳过 0')
    assert_planner_summaries([event,e2],{1:v1,2:v2})


def test_version_mismatch_is_rejected_even_if_numbers_equal():
    plan,event=sample();plan['version']=2
    with pytest.raises(AssertionError):assert_planner_summaries([event],{1:plan})


@pytest.mark.parametrize('old,new',[('10.00','100.00'),('人民币','美元'),('风险 1','风险 0'),('被预算跳过 1','被预算跳过 0'),('低置信商品 1','低置信商品 0'),('采购 0','采购 1'),('模型建议：','建议：')])
def test_incorrect_summary_fails_independent_oracle(old,new):
    plan,event=sample();event['payload']['summary']=event['payload']['summary'].replace(old,new)
    with pytest.raises(AssertionError):assert_planner_summaries([event],{1:plan})
