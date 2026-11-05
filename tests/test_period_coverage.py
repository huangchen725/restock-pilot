"""Independent small cases for store-period budget and purchase/inbound overlap."""
import copy
import pytest
from app import purchase_context
from app.models import PurchaseContext
from app.validation import prepare_snapshot
from tests.test_data_contract import tables,contract
from tests.test_purchase_context import context,order

START='2026-10-29T00:00:00+00:00'
END='2026-10-30T00:00:00+00:00'
ASOF='2026-10-29T10:00:00+00:00'


def local(**changes):
    return {'system_id':'demo','order_id':'PO-1','sku':'A','supplier_id':'S','quantity':48,
            'amount_cents':4800,'status':'SUBMITTED','created_at':'2026-10-29T09:00:00+00:00',**changes}


def snapshot(ctx,inbound=0):
    sales,inv,sup=tables();inv[0]['inbound_qty']=str(inbound);inv[0]['inbound_eta']='2' if inbound else ''
    snap,errors=prepare_snapshot(sales,inv,sup,contract());assert not errors
    return purchase_context.attach(snap,ctx,ASOF)


def calculate(snap,orders=(),relationship='separate',**kwargs):
    from app.period_coverage import calculate
    return calculate(snap,list(orders),kwargs.get('start',START),kwargs.get('end',END),
                     target_system_id='demo',relationship=relationship,source_as_of=ASOF)


def test_period_budget_is_cumulative_and_old_open_request_covers_demand():
    result=calculate(snapshot(context(order())),[local()])
    assert result['occupied_cents']==4800
    assert 10000-result['occupied_cents']==5200
    assert result['covered']=={'A':60} # external 12 + local 48, each once


def test_duplicate_local_readbacks_do_not_double_budget_or_quantity():
    row=local();result=calculate(snapshot(context()),[row,copy.deepcopy(row),copy.deepcopy(row)])
    assert result['occupied_cents']==4800 and result['covered']=={'A':48}
    with pytest.raises(ValueError):calculate(snapshot(context()),[row,local(quantity=49)])


def test_same_order_mapped_to_inbound_is_not_deducted_twice():
    source=order(order_id='ERP-99',quantity=48,status='in_transit',expected_date='2026-10-31',
                 origin_system_id='demo',origin_order_id='PO-1')
    result=calculate(snapshot(context(source),48),[local()],relationship='mapped')
    assert result['covered']=={} # CSV already carries exactly 48 inbound; no extra 48
    assert result['occupied_cents']==4800


def test_received_matched_order_is_already_in_stock():
    source=order(order_id='ERP-99',quantity=48,status='received',origin_system_id='demo',origin_order_id='PO-1')
    result=calculate(snapshot(context(source)),[local()],relationship='mapped')
    assert result['covered']=={} and result['occupied_cents']==4800


def test_external_current_period_requires_original_amount_not_current_price():
    source=order(created_date='2026-10-29')
    with pytest.raises(ValueError,match='原单金额'):calculate(snapshot(context(source)))
    source['amount_cents']=1800 # actual historical price differs from current 100 cents
    result=calculate(snapshot(context(source)),[local()])
    assert result['occupied_cents']==6600


def test_prior_period_local_request_still_covers_but_does_not_reissue_old_budget():
    result=calculate(snapshot(context()),[local(created_at='2026-10-28T10:00:00+00:00')])
    assert result['occupied_cents']==0 and result['covered']=={'A':48}


@pytest.mark.parametrize('fault',['unknown_relation','missing_mapping','quantity_mismatch','supplier_mismatch','amount_mismatch','unknown_receipt'])
def test_uncertain_overlap_or_inconsistent_receipts_blocks(fault):
    source=order(order_id='ERP-99',quantity=48,origin_system_id='demo',origin_order_id='PO-1')
    relation='mapped';item=local()
    if fault=='unknown_relation':relation='unknown'
    if fault=='missing_mapping':source.pop('origin_system_id');source.pop('origin_order_id')
    if fault=='quantity_mismatch':source['quantity']=47
    if fault=='supplier_mismatch':source['supplier_id']='different'
    if fault=='amount_mismatch':source['amount_cents']=4700
    if fault=='unknown_receipt':item['status']='unknown'
    with pytest.raises(ValueError):calculate(snapshot(context(source)),[item],relationship=relation)


def test_partial_day_budget_boundary_is_not_guessed():
    source=order(created_date='2026-10-29',amount_cents=1200)
    with pytest.raises(ValueError,match='日期'):
        calculate(snapshot(context(source)),start='2026-10-29T12:00:00+00:00',end='2026-10-30T12:00:00+00:00')


def test_missing_purchase_context_is_not_zero_external_orders():
    snap,_=prepare_snapshot(*tables(),contract())
    with pytest.raises(ValueError,match='采购上下文'):calculate(snap)
