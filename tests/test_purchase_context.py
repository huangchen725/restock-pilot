"""M5-2 purchase identities belong to immutable dataset identity, not mutable hints."""
import copy,json,hashlib
from datetime import timedelta
import pytest
from app import db,main
from app.models import canonical_json,plan_hash,Plan
from app.validation import snapshot_hash
from tests.test_step3_api import client
from tests.test_collection import clock,source,batch,observe,publish,artifact


def context(*orders):
    return {'system_id':'merchant-pos','timezone_offset':'+00:00','complete':True,'row_count':len(orders),'orders':list(orders)}


def order(**changes):
    return {'order_id':'EXT-1','sku':'A','supplier_id':'S','quantity':12,'status':'requested',
            'created_date':'2026-10-20','expected_date':None,'amount_cents':None,**changes}


def observed(client,clock,sid,ctx,name='b1',stock='0'):
    return observe(client,sid,{**batch(clock,name,stock),'purchase_context':ctx})


def test_purchase_identity_and_changes_are_part_of_snapshot(client,clock):
    sid=source(client)
    first=observed(client,clock,sid,context(order()));assert first.status_code==200,first.text
    clock[0]+=timedelta(seconds=60);r=publish(client,sid);assert r.status_code==200,r.text
    did=r.json()['dataset_id'];snap=db.get_dataset(main.get_conn(),did)
    assert snap.purchase_context.orders[0].amount_cents is None
    manifest=artifact(client,sid,did,'manifest.json').json()
    assert manifest['schema']==2 and manifest['purchase_context']['orders'][0]['order_id']=='EXT-1'
    clock[0]+=timedelta(seconds=1)
    changed=observed(client,clock,sid,context(order(order_id='EXT-2')),'b2')
    assert changed.status_code==200 and changed.json()['dataset_id']!=did
    assert artifact(client,sid,did,'manifest.json').json()==manifest


def test_purchase_rows_reordered_are_semantically_same(client,clock):
    sid=source(client);a=order();b=order(order_id='EXT-2',quantity=6)
    r=observed(client,clock,sid,context(a,b));assert r.status_code==200,r.text
    clock[0]+=timedelta(seconds=1)
    r2=observed(client,clock,sid,context(b,a),'b2')
    assert r2.status_code==200 and r2.json()['state']=='UNCHANGED' and r2.json()['dataset_id']==r.json()['dataset_id']


@pytest.mark.parametrize('fault',['duplicate','unknown_sku','unknown_status','wrong_total','incomplete','wrong_inbound','wrong_date','future_order'])
def test_invalid_purchase_context_never_publishes(client,clock,fault):
    sid=source(client);c=context(order())
    if fault=='duplicate':c=context(order(),order())
    if fault=='unknown_sku':c=context(order(sku='UNKNOWN'))
    if fault=='unknown_status':c['orders'][0]['status']='maybe'
    if fault=='wrong_total':c['row_count']=2
    if fault=='incomplete':c['complete']=False
    if fault=='wrong_inbound':c=context(order(status='in_transit',expected_date='2026-10-30'))
    if fault=='wrong_date':c['orders'][0]['created_date']='2026-02-31'
    if fault=='future_order':c['orders'][0]['created_date']='2027-01-01'
    r=observed(client,clock,sid,c)
    assert r.status_code==422,r.text
    assert client.get(f'/data-sources/{sid}/publications').json()['publications']==[]


def test_purchase_namespace_cannot_change_to_hide_existing_orders(client,clock):
    sid=source(client);r=observed(client,clock,sid,context(order()));assert r.status_code==200,r.text
    clock[0]+=timedelta(seconds=1)
    assert observed(client,clock,sid,{**context(order()),'system_id':'renamed'},'b2').status_code==422


def test_legacy_snapshot_and_schema2_plan_hash_unchanged(client,clock):
    sid=source(client);r=observe(client,sid,batch(clock));assert r.status_code==200
    clock[0]+=timedelta(seconds=60);did=publish(client,sid).json()['dataset_id']
    snap=db.get_dataset(main.get_conn(),did)
    explicit={k:snap.model_dump()[k] for k in ['contract','window_start','window_end','sales','inventory','suppliers']}
    assert snapshot_hash(snap)==hashlib.sha256(canonical_json(explicit).encode()).hexdigest()
    p=client.post('/tasks',json={'source_id':sid,'dataset_id':did,'policy':{'budget_cents':1800}}).json()['plan']
    assert 'purchase_context' not in p['dataset_snapshot']
    assert plan_hash(Plan.model_validate(p))==hashlib.sha256(canonical_json(p).encode()).hexdigest()


def test_purchase_namespace_and_mapping_identifiers_are_exact(client,clock):
    from app.models import PurchaseLine
    with pytest.raises(ValueError):PurchaseLine.model_validate(order(origin_system_id='demo',origin_order_id=' PO-1'))
    with pytest.raises(ValueError):PurchaseLine.model_validate(order(origin_system_id='demo'))
    sid=source(client);first=observed(client,clock,sid,context(order()));assert first.status_code==200
    clock[0]+=timedelta(seconds=1)
    assert observe(client,sid,batch(clock,'omitted')).status_code==422


def test_manifest_context_schema_cannot_be_falsified(client,clock):
    from app.collection import read_package,CollectionError
    sid=source(client);r=observed(client,clock,sid,context(order()));assert r.status_code==200
    clock[0]+=timedelta(seconds=60);did=publish(client,sid).json()['dataset_id']
    path=main.COLLECTION_DIR/sid/did/'manifest.json'
    manifest=json.loads(path.read_text(encoding='utf-8'));manifest['schema']=1
    path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(CollectionError):read_package(main.COLLECTION_DIR,sid,did)


@pytest.mark.parametrize('spelling',[None,'004','4.0'])
@pytest.mark.parametrize('missing_sales',['reject','zero'])
def test_reuploading_only_csv_cannot_strip_source_binding(client,clock,spelling,missing_sales):
    sid=source(client);assert observed(client,clock,sid,context(order())).status_code==200
    clock[0]+=timedelta(seconds=60);did=publish(client,sid).json()['dataset_id']
    files={name:(name+'.csv',artifact(client,sid,did,name+'.csv').content,'text/csv') for name in ('sales','inventory','suppliers')}
    if spelling is not None:
        filename,content,kind=files['sales'];files['sales']=(filename,content.replace(b',4\n',(','+spelling+'\n').encode()),kind)
    from tests.test_data_contract import contract
    clone=client.post('/datasets',files=files,data={'contract':contract(missing_sales=missing_sales).model_dump_json()}).json()['dataset_id']
    assert clone!=did # context is absent, but these are still the same published CSVs
    r=client.post('/tasks',json={'dataset_id':clone,'policy':{'budget_cents':1800}})
    assert r.status_code==422,r.text
    assert client.get('/tasks').json()['tasks']==[]


def test_implicit_local_dataset_cannot_strip_source_binding(client,clock,tmp_path,monkeypatch):
    sid=source(client);assert observed(client,clock,sid,context(order())).status_code==200
    clock[0]+=timedelta(seconds=60);did=publish(client,sid).json()['dataset_id']
    root=tmp_path/'implicit';root.mkdir()
    for name in ('sales','inventory','suppliers'):
        (root/(name+'.csv')).write_bytes(artifact(client,sid,did,name+'.csv').content)
    from tests.test_data_contract import contract
    (root/'contract.json').write_text(contract().model_dump_json(),encoding='utf-8')
    monkeypatch.setattr(main,'DATA_DIR',root)
    r=client.post('/tasks',json={'policy':{'budget_cents':1800}})
    assert r.status_code==422,r.text
    for task in client.get('/tasks').json()['tasks']:
        assert db.list_plan_versions(main.get_conn(),task['task_id'])==[]


def legacy_data_and_unpublished_source(client,clock):
    from tests.test_data_contract import tables,contract
    from app.validation import prepare_snapshot
    snap,errors=prepare_snapshot(*tables(),contract());assert not errors
    did=db.save_dataset(main.get_conn(),snap)
    sid=source(client);assert observed(client,clock,sid,context(order())).status_code==200
    return did,sid


@pytest.mark.parametrize('approved_first',[False,True])
def test_admitted_manual_task_keeps_provenance_when_source_appears(client,clock,approved_first):
    did,sid=legacy_data_and_unpublished_source(client,clock)
    r=client.post('/tasks',json={'dataset_id':did,'policy':{'budget_cents':1800}});assert r.status_code==200,r.text
    tid=r.json()['task_id']
    if approved_first:assert client.post('/tasks/'+tid+'/approve').status_code==200
    clock[0]+=timedelta(seconds=60);assert publish(client,sid).status_code==200
    action='/execute' if approved_first else '/approve'
    # This task was admitted before that source published; matching values do
    # not establish shop ownership retroactively (existing M5-1 contract).
    assert client.post('/tasks/'+tid+action).status_code==200
    state=client.get('/tasks/'+tid).json()
    assert state['data_status'] is None and db.task_source(main.get_conn(),tid) is None
    assert state['state']==('COMPLETED' if approved_first else 'APPROVED')


def test_publication_after_admission_does_not_retroactively_rebind_generation(client,clock,monkeypatch):
    did,sid=legacy_data_and_unpublished_source(client,clock)
    original=main._run_agent_stage
    def racing(stage,ctx,request):
        result=original(stage,ctx,request)
        if stage=='planner':
            clock[0]+=timedelta(seconds=60);assert publish(client,sid).status_code==200
        return result
    monkeypatch.setattr(main,'_run_agent_stage',racing)
    r=client.post('/tasks',json={'dataset_id':did,'policy':{'budget_cents':1800}})
    assert r.status_code==200,r.text
    tid=r.json()['task_id']
    assert db.list_plan_versions(main.get_conn(),tid)==[1]
    assert db.task_source(main.get_conn(),tid) is None
