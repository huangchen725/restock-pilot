import hashlib
import json

import pytest
from pydantic import ValidationError

from app import db, main
from app.models import Plan, Policy, canonical_json, plan_hash
from tests.test_data_contract import write_dataset, tables, contract
from tests.test_step3_api import client  # Isolated DB, simulated model and adapter.


def create(client):
    response = client.post("/tasks", json={"policy":{"budget_cents":1800,"cover_days":7,"safety_days":2}})
    assert response.status_code == 200
    return response.json()


def test_api_exposes_snapshot_and_calculation_contract(client, tmp_path, monkeypatch):
    write_dataset(tmp_path); monkeypatch.setattr(main,"DATA_DIR",tmp_path)
    created = create(client); p = created["plan"]
    assert p["schema_version"] == 2 and p["rules_version"] == "replenishment-v2"
    assert p["dataset_id"] == "ds-"+p["dataset_hash"]
    assert p["dataset_snapshot"]["window_end"] == "2026-10-28"
    assert p["items"][0]["order_qty"] == 18 and p["items"][0]["unmet_qty"] == 30
    assert p["budget_remaining_cents"] == 0
    loaded, h = db.get_plan(main.get_conn(),created["task_id"],1)
    assert loaded.model_dump() == p and h == created["plan_hash"]


@pytest.mark.parametrize("broken",["missing_file","bad_stock","missing_day","unconfirmed"])
def test_data_errors_stop_before_model_or_plan(client,tmp_path,monkeypatch,broken):
    data = tables()
    if broken == "bad_stock": data[1][0]["available_qty"] = "unknown"
    if broken == "missing_day": data[0].pop(5)
    write_dataset(tmp_path,data,confirmed=broken!="unconfirmed")
    if broken == "missing_file": (tmp_path/"sales.csv").unlink()
    monkeypatch.setattr(main,"DATA_DIR",tmp_path)
    result = create(client)
    assert result["state"] == "NEEDS_INPUT" and result["data_problems"]
    assert client.agent_calls == []
    assert db.get_plan(main.get_conn(),result["task_id"],1) == (None,None)
    assert client.post(f"/tasks/{result['task_id']}/approve").status_code == 404


def test_request_can_explicitly_confirm_contract(client,tmp_path,monkeypatch):
    write_dataset(tmp_path,confirmed=False); monkeypatch.setattr(main,"DATA_DIR",tmp_path)
    result = client.post('/tasks',json={'policy':{'budget_cents':10000},'data_contract':contract().model_dump()})
    assert result.json()['state']=='NEEDS_REVIEW'


def test_revision_uses_persisted_snapshot_after_file_changes_and_reload(client,tmp_path,monkeypatch):
    write_dataset(tmp_path); monkeypatch.setattr(main,"DATA_DIR",tmp_path)
    # cover_days=30 so the existing simulated Reviser can reduce it by one.
    created=client.post('/tasks',json={'policy':{'budget_cents':10000}}).json()
    task=created['task_id']; client.post(f'/tasks/{task}/approve')
    (tmp_path/'sales.csv').write_text('invalid data',encoding='utf-8')
    main._tasks = {}  # Simulate state reload from the persisted task and plan.
    revised=client.post(f'/tasks/{task}/revise',json={'comment':'覆盖天数减少一天'})
    assert revised.status_code==200,revised.text
    p=revised.json()['plan']
    assert p['version']==2 and p['policy']['cover_days']==29
    assert p['dataset_snapshot']==created['plan']['dataset_snapshot']
    assert p['dataset_hash']==created['plan']['dataset_hash']
    assert client.post(f'/tasks/{task}/execute').status_code==409


@pytest.mark.parametrize('decision',[
    {'policy':None,'explanation':'首版不支持','unsupported_requests':['商品A至少采购20件']},
    {'policy':{'budget_cents':3000},'explanation':'遗漏其他参数','unsupported_requests':[]},
    {'policy':{'budget_cents':3000,'cover_days':30,'safety_days':7,'priority_categories':[], 'excluded_skus':[]},'explanation':'legacy response'},
])
def test_unsupported_or_incomplete_decision_cannot_create_revision(client,monkeypatch,decision):
    created=client.post('/tasks',json={'policy':{'budget_cents':500000}}).json()
    task=created['task_id']; client.post(f'/tasks/{task}/approve')
    monkeypatch.setattr(main,'_run_agent_stage',lambda *a:{'summary':json.dumps(decision)})
    result=client.post(f'/tasks/{task}/revise',json={'comment':'商品A至少采购20件'})
    assert result.status_code==422
    state=client.get(f'/tasks/{task}').json()
    assert state['latest_plan']['version']==1 and state['approved_plan_hash'] is None
    assert client.post(f'/tasks/{task}/execute').status_code==409


@pytest.mark.parametrize('policy',[{'budget_cents':True},{'budget_cents':100.5},
    {'budget_cents':100,'min_purchase':20},{'budget_cents':100,'excluded_skus':[123]}])
def test_policy_rejects_unsupported_fields_and_coercion(policy):
    with pytest.raises(ValidationError): Policy.model_validate(policy)


def test_unknown_sku_or_category_is_not_silently_ignored(client):
    for field,values in [('excluded_skus',['NO-SUCH-SKU']),('priority_categories',['NO-SUCH-CATEGORY'])]:
        result=client.post('/tasks',json={'policy':{'budget_cents':500000,field:values}}).json()
        assert result['state']=='NEEDS_INPUT'


def test_legacy_json_hash_and_approved_execution_remain_compatible(client):
    created=client.post('/tasks',json={'policy':{'budget_cents':500000}}).json()
    task=created['task_id']
    # Materialize the precise pre-v2 JSON format independently of current defaults.
    old_item_fields=['sku','name','category','avg_daily_sales','lead_days','available_qty','inbound_qty',
        'inbound_eta_days','target_qty','gap','order_qty','unit_cost_cents','supplier_id','pack_size',
        'min_order_qty','line_total_cents','confidence','reason']
    p=created['plan']
    raw={k:p[k] for k in ['plan_id','version','dataset_hash','policy','total_cost_cents','skipped']}
    raw['items']=[{k:item[k] for k in old_item_fields} for item in p['items']]
    payload={k:raw[k] for k in ['version','dataset_hash','policy','items','total_cost_cents']}
    old_hash=hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()
    with main.get_conn():
        main.get_conn().execute('UPDATE plans SET plan_json=?,plan_hash=? WHERE plan_id=?',
                               (json.dumps(raw),old_hash,task))
    loaded,h=db.get_plan(main.get_conn(),task,1)
    assert h==old_hash and loaded.schema_version==1
    approved=client.post(f'/tasks/{task}/approve').json()
    assert approved['approved_plan_hash']==old_hash
    main._tasks={}
    assert client.post(f'/tasks/{task}/execute').json()['state']=='COMPLETED'


def test_v2_hash_covers_snapshot_and_evidence(client):
    created=client.post('/tasks',json={'policy':{'budget_cents':500000}}).json()
    p=Plan.model_validate(created['plan']); original=plan_hash(p)
    p.dataset_snapshot.inventory[0]['available_qty']='999'
    assert plan_hash(p)!=original
    p=Plan.model_validate(created['plan']); p.items[0].warnings.append('changed')
    assert plan_hash(p)!=original


def test_legacy_revision_requires_new_task_without_calling_model(client,monkeypatch):
    created=client.post('/tasks',json={'policy':{'budget_cents':500000}}).json()
    task=created['task_id']
    plan,_=db.get_plan(main.get_conn(),task,1)
    plan.schema_version=1; plan.dataset_snapshot=None
    db.save_plan(main.get_conn(),plan)
    client.post(f'/tasks/{task}/approve')
    monkeypatch.setattr(main,'_run_agent_stage',lambda *a: pytest.fail('legacy revision called model'))
    response=client.post(f'/tasks/{task}/revise',json={'comment':'减少预算'})
    assert response.status_code==409 and 'no data snapshot' in response.json()['detail']
    assert client.get(f'/tasks/{task}').json()['approved_plan_hash'] is None


def test_snapshot_corruption_is_rejected_before_revision_model(client,monkeypatch):
    created=client.post('/tasks',json={'policy':{'budget_cents':500000}}).json()
    task=created['task_id']; p=created['plan']
    p['dataset_snapshot']['inventory'][0]['available_qty']='999'
    with main.get_conn():
        main.get_conn().execute('UPDATE plans SET plan_json=? WHERE plan_id=?',(json.dumps(p),task))
    monkeypatch.setattr(main,'_run_agent_stage',lambda *a: pytest.fail('corrupt snapshot called model'))
    response=client.post(f'/tasks/{task}/revise',json={'comment':'减少预算'})
    assert response.status_code==409 and 'snapshot hash mismatch' in response.json()['detail']
