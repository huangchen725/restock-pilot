"""M4 deterministic oracles and the missing-price fixture.
Full 20-scenario coverage is selected via scenario_catalog, reusing real fault/UI tests
once each; the former pass statements and fake state transitions are replaced there.
"""
import pytest
from app.validation import load_snapshot
from tests.test_data_contract import write_dataset, tables
from tests.test_replenishment_v2 import plan, row


def assert_moq(result):
    assert result['items'], 'MOQ fixture must include items'
    for item in result['items']:
        assert item['order_qty'] == 0 or item['order_qty'] >= item['min_order_qty']


def assert_tight_budget(result, budget):
    assert 0 <= result['total_cost_cents'] <= budget
    assert result['skipped'], 'This fixture requires at least one unpurchased SKU'


def assert_priority(result):
    assert [(i['sku'],i['order_qty']) for i in result['items']] == [('A',48),('B',12)]
    assert any(i['category']=='饮料' and i['order_qty']>0 for i in result['items'])


def test_scenario_03_priority_categories():
    p=plan([row(sku='B',category='日用'),row(sku='A',category='饮料')],budget=6000,priority_categories=['饮料'])
    assert_priority(p.model_dump())


def test_scenario_07_moq_rounding():
    p=plan([row(available_qty=47,min_order_qty=24,pack_size=5)])
    assert p.items[0].gap==1 and p.items[0].order_qty==25  # MOQ is a floor, not a multiple.
    assert_moq(p.model_dump())


def test_scenario_11_budget_insufficient():
    p=plan(budget=1000)
    assert p.items[0].order_qty==0 and p.items[0].unmet_qty==48
    assert_tight_budget(p.model_dump(),1000)


@pytest.mark.parametrize('missing',['column','blank'])
def test_scenario_14_price_missing(tmp_path,missing):
    data=tables()
    if missing=='column':
        del data[2][0]['unit_cost_cents']
    else:
        data[2][0]['unit_cost_cents']=''
    write_dataset(tmp_path,data)
    snapshot,errors=load_snapshot(tmp_path)
    assert snapshot is None and any('unit_cost_cents' in e for e in errors)
    write_dataset(tmp_path,tables())
    assert load_snapshot(tmp_path)[1]==[]
