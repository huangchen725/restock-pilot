"""Independent merchant fixtures are test oracles, never passed to the collector."""
import json
import socket
import threading
import time
from datetime import timedelta, date
from pathlib import Path

import httpx
import pytest
import uvicorn

from app import collection, collector, db
from app.browser_collection import BrowserConfig, MerchantBrowser, PageError
from app.merchant_test_portal import create_merchant_test_app, build_portal_data
from tests.test_data_contract import contract


@pytest.fixture
def portal():
    application = create_merchant_test_app()
    sock = socket.socket(); sock.bind(('127.0.0.1',0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(application,log_level='error'))
    thread = threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.02)
    assert server.started
    try: yield application.state.portal, url
    finally:
        server.should_exit=True; thread.join(10); sock.close()
        assert not thread.is_alive()


def config(url, scope=None):
    return collection.SourceConfig(name='页面验收',merchant_id='S01',warehouse_id='W01',environment='test',
        contract=contract().model_copy(update={'window_end':None,'missing_sales':'reject'}),
        schedule=collection.Schedule(collect_every_seconds=1800,save_every_seconds=1,
            check_every_seconds=86400,max_age_seconds=2678400),
        browser=BrowserConfig(base_url=url,sku_scope=scope if scope is not None else ['SKU-1001','SKU-1003','SKU-1004']))


def scripted_model(session):
    sid=session.sid
    session.call('inspect_source',{'source_id':sid})
    for section in ('inventory','sales','suppliers','inbound'):
        session.call('read_source_section',{'source_id':sid,'section':section})
    detail=session.call('read_source_product',{'source_id':sid,'sku':'SKU-1014'})
    assert '999' in detail['remark']
    session.call('finish_source_collection',{'source_id':sid})
    return {'mode':'test-controller','model':None}


def source_db(tmp_path,url,scope=None):
    conn=db.init_db(tmp_path/'collection.db')
    sid=collection.create_source(conn,config(url,scope))
    return conn,sid


def due(conn,sid):
    row=db.get_source(conn,sid)
    row['state']['next_save_at']=collection.stamp(collection.utcnow()-timedelta(seconds=1))
    db.update_source(conn,sid,row['state'])


def assert_snapshot_matches_portal(snapshot, portal, scope):
    assert len(snapshot.inventory)==len(scope)
    context=snapshot.purchase_context
    assert context and context.complete
    assert context.row_count==len(context.orders)
    expected=[r for r in portal.data.inbound if r['warehouse_id']=='W01' and r['sku'] in scope]
    states={'已申请':'requested','已订货':'ordered','在途':'in_transit','已到货':'received'}
    assert sorted((r.order_id,r.sku,r.quantity,r.status,r.created_date,r.expected_date,r.amount_cents) for r in context.orders)==sorted(
        (r['po_no'],r['sku'],r['qty'],states[r['status']],r['created'],r['eta'],None) for r in expected)

    end=portal.data.window_end
    expected_sales={(r['date'].isoformat(),r['sku']):r['qty']-r['refund_qty'] for r in portal.data.sales
                    if r['store_id']=='S01' and r['sku'] in scope and end-timedelta(days=27)<=r['date']<=end}
    assert {(r['date'],r['sku']):int(r['sold_qty']) for r in snapshot.sales}==expected_sales
    for row in snapshot.inventory:
        sku=row['sku']; inv=portal.data.inventory[('W01',sku)]
        assert int(row['available_qty'])==(inv['on_hand']-inv['reserved'])*portal.data.products[sku].unit_factor
        incoming=[r for r in portal.data.inbound if r['sku']==sku and r['warehouse_id']=='W01' and r['status'] in ('在途','已订货')]
        assert int(row['inbound_qty'])==sum(r['qty'] for r in incoming)
        assert row['inbound_eta']==(str((date.fromisoformat(incoming[0]['eta'])-end).days) if incoming else '')
    for row in snapshot.suppliers:
        actual=portal.data.supplier_terms[row['sku']]
        for field in ('unit_cost_cents','lead_days','pack_size','min_order_qty'):
            assert int(row[field])==actual[field]


def test_browser_config_rejects_external_and_secret_urls():
    for url in ('https://evil.example','http://user:pass@127.0.0.1:8030','http://127.0.0.1:8030/api'):
        with pytest.raises(ValueError): BrowserConfig(base_url=url)


def test_browser_two_batches_dedup_publish_and_independent_values(tmp_path,portal):
    target,url=portal; conn,sid=source_db(tmp_path,url)
    try:
        first=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),scripted_model)
        assert first['status']=='succeeded', first
        assert not db.list_publications(conn,sid)
        state=db.get_source(conn,sid)['state']
        snapshot=collection.DatasetSnapshot.model_validate(state['pending_snapshot'])
        assert_snapshot_matches_portal(snapshot,target,{'SKU-1001','SKU-1003','SKU-1004'})
        assert len(snapshot.sales)==84
        due(conn,sid); published=collection.publish(conn,tmp_path/'csv',sid)
        assert published['state']=='PUBLISHED'
        old=published['dataset_id']
        package=tmp_path/'csv'/sid/old
        original={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in package.iterdir()}
        again=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),scripted_model)
        assert again['status']=='succeeded',again
        due(conn,sid); collection.publish(conn,tmp_path/'csv',sid)
        assert original=={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in package.iterdir()}
        target.data=build_portal_data(batch=2)
        changed=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),scripted_model)
        assert changed['status']=='succeeded', changed
        newstate=db.get_source(conn,sid)['state']
        assert newstate['latest_observed_dataset_id']!=old
        with pytest.raises(collection.CollectionError): collection.assert_ready(conn,tmp_path/'csv',sid,old)
        snapshot=collection.DatasetSnapshot.model_validate(newstate['pending_snapshot'])
        assert_snapshot_matches_portal(snapshot,target,{'SKU-1001','SKU-1003','SKU-1004'})
        due(conn,sid); collection.publish(conn,tmp_path/'csv',sid)
        assert len(db.list_publications(conn,sid))==2
        assert target.write_log==[]
        pages=json.loads(next((tmp_path/'csv'/sid/'runs').glob('*/pages.json')).read_text(encoding='utf-8'))
        assert len([p for p in pages if p['section']=='sales'])>20
    finally: conn.close()


@pytest.mark.parametrize('scope,expected',[
    (['SKU-1011'],'成本缺失'),(['SKU-1014'],'含税含运'),(['SKU-1023'],'missing days'),
    (['SKU-1019'],'不存在或已停用')])
def test_bad_business_rows_never_publish(tmp_path,portal,scope,expected):
    target,url=portal
    if scope==['SKU-1023']:  # isolate missing sales from an independent overdue-inbound fixture fault
        target.data.inbound=[r for r in target.data.inbound if r['sku']!='SKU-1023']
    conn,sid=source_db(tmp_path,url,scope)
    try:
        result=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),scripted_model)
        assert result['status']=='failed' and expected in result['error'],result
        assert not db.list_publications(conn,sid)
        assert db.get_source(conn,sid)['state']['latest_observed_dataset_id'] is None
        assert target.write_log==[]
    finally: conn.close()


def test_reader_network_guard_and_session_expiry(tmp_path,portal):
    target,url=portal; cfg=config(url)
    reader=MerchantBrowser(cfg.browser,cfg.model_dump(),tmp_path/'evidence')
    try:
        reader.start(); reader.read_section('inventory'); reader.detail('SKU-1014')
        assert not reader.permitted(url+'/products/SKU-1014/price','POST')
        assert not reader.permitted(url+'/login','POST')
        assert not reader.permitted(url+'/api/sales','GET')
        assert not reader.permitted('http://127.0.0.1:1/dashboard','GET')
        # Actually submit the dangerous form; request interception must prevent mutation.
        reader.page.locator('#new-price').fill('0.01')
        try: reader.page.locator('#btn-adjust-price').click()
        except Exception: pass
        assert any(b['method']=='POST' for b in reader.blocked)
        assert target.write_log==[]
        reader.page.close()
        reader.page=reader.context.new_page()
        reader.page.goto(url+'/dashboard')
        target.sessions.clear()
        with pytest.raises(PageError,match='登录'): reader.read_section('sales')
    finally: reader.close()


def test_partial_version_change_and_schema_change_fail_closed(tmp_path,portal):
    target,url=portal; cfg=config(url)
    reader=MerchantBrowser(cfg.browser,cfg.model_dump(),tmp_path/'evidence')
    try:
        reader.start(); reader.read_section('inventory')
        with pytest.raises(PageError,match='完整'):reader.observation('incomplete')
        target.data=build_portal_data(batch=2)
        with pytest.raises(PageError,match='版本'):reader.read_section('sales')
    finally: reader.close()


def test_claim_recovery_and_model_refusal_leave_no_observation(tmp_path,portal):
    target,url=portal; conn,sid=source_db(tmp_path,url)
    try:
        run=collector.claim(conn,sid)
        with pytest.raises(collection.CollectionError):collector.claim(conn,sid)
        collector.recover(conn)
        assert db.collection_runs(conn,sid)[0]['status']=='interrupted'
        run=collector.claim(conn,sid)
        result=collector.execute(conn,tmp_path/'csv',sid,run,lambda _: {'pretended':'success'})
        assert result['status']=='failed'
        assert not db.get_source(conn,sid)['state']['latest_dataset_id']
        assert target.write_log==[]
    finally: conn.close()


def test_visible_header_drift_and_cross_store_identity_rejected(tmp_path,portal,monkeypatch):
    import app.merchant_test_portal as site
    from fastapi.responses import HTMLResponse
    target,url=portal; cfg=config(url)
    cfg.warehouse_id='W02'
    wrong=MerchantBrowser(cfg.browser,cfg.model_dump(),tmp_path/'wrong')
    try:
        with pytest.raises(PageError,match='身份'):wrong.start()
    finally:wrong.close()
    cfg.warehouse_id='W01'
    original=site._inventory_page
    def changed(*args):
        response=original(*args)
        return HTMLResponse(response.body.decode('utf-8').replace('<th>可用</th>','<th>总库存</th>'))
    monkeypatch.setattr(site,'_inventory_page',changed)
    reader=MerchantBrowser(cfg.browser,cfg.model_dump(),tmp_path/'changed')
    try:
        reader.start()
        with pytest.raises(PageError,match='表头'):reader.read_section('inventory')
    finally:reader.close()
    assert not target.write_log


def test_missing_sales_cannot_be_relabelled_zero(tmp_path,portal):
    _,url=portal; cfg=config(url)
    cfg.contract.missing_sales='zero'
    conn=db.init_db(tmp_path/'config.db')
    try:
        with pytest.raises(collection.CollectionError,match='不等于零'):
            collection.create_source(conn,cfg)
    finally:conn.close()


def test_periodic_collect_and_publish_restart_pause_and_stale_session(tmp_path,portal):
    target,url=portal;conn,sid=source_db(tmp_path,url)
    worker=collector.CollectorWorker(lambda:conn,lambda:tmp_path/'csv',model=scripted_model)
    try:
        worker.cycle()
        assert db.collection_runs(conn,sid)[0]['status']=='succeeded'
        count=len(db.collection_runs(conn,sid))
        worker.cycle()
        assert len(db.collection_runs(conn,sid))==count
        assert not db.list_publications(conn,sid)
        due(conn,sid);collection.publish_due(conn,tmp_path/'csv')
        assert len(db.list_publications(conn,sid))==1
        collector.set_enabled(conn,sid,False)
        with pytest.raises(collection.CollectionError):collector.claim(conn,sid)
        worker.cycle()
        assert len(db.collection_runs(conn,sid))==count
        collector.set_enabled(conn,sid,True)
        run=collector.claim(conn,sid)
        state=db.get_source(conn,sid)['state'];state['collection_job']['status']='running';db.update_source(conn,sid,state)
        session=collector.CollectorSession(conn,sid,run,tmp_path/'csv')
        try:
            for tool,args in [('inspect_source',{'source_id':'foreign'}),
                              ('build_plan',{'source_id':sid}),
                              ('finish_source_collection',{'source_id':sid,'sales':[]})]:
                with pytest.raises(ValueError):session.call(tool,args)
            collector.set_enabled(conn,sid,False)
            with pytest.raises(ValueError,match='关闭'):session.call('inspect_source',{'source_id':sid})
        finally:session.close()
        conn.close();conn=db.init_db(tmp_path/'collection.db')
        assert db.collection_runs(conn,sid)[0]['status']=='interrupted'
        assert len(db.list_publications(conn,sid))==1
        assert target.write_log==[]
    finally:conn.close()


def test_inbound_status_unit_and_distinct_eta_contract(tmp_path,portal):
    target,url=portal
    target.data.inbound += [
        {'po_no':'TEST-FUTURE','created':'2026-10-28','supplier_id':'SUP-X','sku':'SKU-1004',
         'warehouse_id':'W01','qty':24,'status':'在途','eta':'2026-10-30'},
        {'po_no':'TEST-APPLICATION','created':'2026-10-28','supplier_id':'SUP-X','sku':'SKU-1004',
         'warehouse_id':'W01','qty':999,'status':'已申请','eta':'2026-10-30'},
        {'po_no':'TEST-ARRIVED','created':'2026-10-28','supplier_id':'SUP-X','sku':'SKU-1004',
         'warehouse_id':'W01','qty':888,'status':'已到货','eta':'2026-10-28'}]
    conn,sid=source_db(tmp_path,url,['SKU-1004'])
    try:
        first=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),scripted_model)
        assert first['status']=='succeeded',first
        snapshot=collection.DatasetSnapshot.model_validate(db.get_source(conn,sid)['state']['pending_snapshot'])
        row=snapshot.inventory[0]
        assert row['inbound_qty']=='24' and row['inbound_eta']=='2'
        assert_snapshot_matches_portal(snapshot,target,{'SKU-1004'})
        target.data.inbound.append({'po_no':'TEST-SECOND-ETA','created':'2026-10-28','supplier_id':'SUP-X',
            'sku':'SKU-1004','warehouse_id':'W01','qty':12,'status':'已订货','eta':'2026-10-31'})
        second=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),scripted_model)
        assert second['status']=='failed' and '单一 ETA' in second['error']
        assert target.write_log==[]
    finally:conn.close()


def test_unknown_inbound_status_is_not_silently_ignored(tmp_path,portal):
    target,url=portal
    target.data.inbound.append({'po_no':'UNKNOWN-STATE','created':'2026-10-28','supplier_id':'SUP-X',
        'sku':'SKU-1001','warehouse_id':'W01','qty':20,'status':'待人工确认','eta':'2026-10-30'})
    conn,sid=source_db(tmp_path,url,['SKU-1001'])
    try:
        result=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),scripted_model)
        assert result['status']=='failed' and '未知采购状态' in result['error']
        assert db.get_source(conn,sid)['state']['latest_observed_dataset_id'] is None
    finally:conn.close()


def test_warehouse_identifier_requires_exact_match(tmp_path,portal,monkeypatch):
    import app.merchant_test_portal as site
    from fastapi.responses import HTMLResponse
    _,url=portal;cfg=config(url)
    original=site._inventory_page
    def changed(*args):
        response=original(*args)
        return HTMLResponse(response.body.decode('utf-8').replace('城南店仓（W01）','城南店仓（W010）'))
    monkeypatch.setattr(site,'_inventory_page',changed)
    reader=MerchantBrowser(cfg.browser,cfg.model_dump(),tmp_path/'warehouse')
    try:
        reader.start()
        with pytest.raises(PageError,match='仓库'):reader.read_section('inventory')
    finally:reader.close()
