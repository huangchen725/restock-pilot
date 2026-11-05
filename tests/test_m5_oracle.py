"""Positive and corrupted controls for the independent combined-flow comparator."""
from types import SimpleNamespace
from datetime import date,timedelta
from copy import deepcopy
import pytest
from tests.m5_end_to_end import independent_plan,SCOPE


def example():
    end=date(2026,10,28)
    data=SimpleNamespace(window_end=end,inbound=[],products={s:SimpleNamespace(unit_factor=1) for s in SCOPE},
        inventory={('W01',s):{'on_hand':0,'reserved':0} for s in SCOPE},
        supplier_terms={s:{'supplier_id':'S','unit_cost_cents':100,'lead_days':3,'pack_size':6,'min_order_qty':12} for s in SCOPE},
        sales=[{'date':end-timedelta(days=i),'sku':s,'store_id':'S01','qty':4,'refund_qty':0} for s in SCOPE for i in range(28)])
    plan={'policy':{'budget_cents':200000,'cover_days':7,'safety_days':2,'excluded_skus':[]},
        'items':[{'sku':s,'target_qty':48,'gap':48,'order_qty':48,'line_total_cents':4800,'supplier_id':'S','unit_cost_cents':100} for s in SCOPE],
        'total_cost_cents':144000,'budget_remaining_cents':56000}
    return plan,SimpleNamespace(data=data)


def test_independent_combined_oracle_accepts_hand_calculated_30_skus():
    plan,merchant=example();result=independent_plan(plan,merchant)
    assert result['total_cents']==144000 and result['quantity']==1440


@pytest.mark.parametrize('wrong',['quantity','money','source','snapshot','remaining'])
def test_independent_combined_oracle_rejects_wrong_plan_or_source(wrong):
    plan,merchant=example()
    if wrong=='quantity':plan['items'][0]['order_qty']+=6
    if wrong=='money':plan['total_cost_cents']+=1
    if wrong=='source':merchant.data.sales[0]['qty']+=28
    if wrong=='snapshot':plan['items'][0]['sku']='NOT-IN-SCOPE'
    if wrong=='remaining':plan['budget_remaining_cents']+=4800
    with pytest.raises(AssertionError):independent_plan(plan,merchant)
