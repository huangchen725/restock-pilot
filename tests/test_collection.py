"""M5-1 local collection boundary; synthetic observations, no browser/model claim."""
import copy
import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db, main
from tests.test_step3_api import client
from tests.test_data_contract import contract, tables


def auth():
    return {"X-Restock-Approval": main.APPROVAL_TOKEN}


def settings(**changes):
    return {"collect_every_seconds": 30, "save_every_seconds": 60,
            "check_every_seconds": 3600, "max_age_seconds": 600, **changes}


def config():
    return {"name": "Synthetic shop", "merchant_id": "merchant-a", "warehouse_id": "warehouse-a",
            "environment": "test", "contract": contract().model_dump(), "schedule": settings()}


def source(client):
    r = client.post('/data-sources', json=config(), headers=auth())
    assert r.status_code == 200, r.text
    return r.json()['source_id']


def test_sources_accept_confirmed_config(client):
    sid = source(client)
    result = client.get('/data-sources/' + sid).json()
    assert result['config']['schedule'] == settings()
    assert result['state']['latest_dataset_id'] is None
    assert result['automation']['collection'] is False
    assert result['automation']['replenishment'] is False
    assert client.get('/tasks').json()['tasks'] == []


@pytest.fixture
def clock(client, tmp_path, monkeypatch):
    from app import collection
    value = [datetime(2026, 10, 29, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(collection, 'utcnow', lambda: value[0])
    monkeypatch.setattr(main, 'COLLECTION_DIR', tmp_path / 'csv-store')
    return value


def batch(clock, name='b1', stock='0', **changes):
    sales, inventory, suppliers = tables()
    inventory[0]['available_qty'] = stock
    moment = clock[0].isoformat()
    return {'batch_id': name, 'merchant_id': 'merchant-a', 'warehouse_id': 'warehouse-a',
            'observed_at': moment, 'source_as_of': moment, 'complete': True,
            'row_counts': {'sales': 28, 'inventory': 1, 'suppliers': 1},
            'sales': sales, 'inventory': inventory, 'suppliers': suppliers, **changes}


def observe(client, sid, payload):
    return client.post(f'/data-sources/{sid}/observations', json=payload, headers=auth())


def publish(client, sid):
    return client.post(f'/data-sources/{sid}/publish', headers=auth())


def ready(client, clock, sid):
    assert observe(client, sid, batch(clock)).status_code == 200
    clock[0] += timedelta(seconds=60)
    r = publish(client, sid)
    assert r.status_code == 200, r.text
    return r.json()['dataset_id']


def artifact(client, sid, did, name):
    return client.get(f'/data-sources/{sid}/publications/{did}/{name}')


@pytest.mark.parametrize('field,value', [('save_every_seconds', 0), ('collect_every_seconds', True),
                                         ('check_every_seconds', -1), ('max_age_seconds', 1.5)])
def test_bad_schedules_and_unauthorized_writes_are_rejected(client, field, value):
    c = config(); c['schedule'][field] = value
    assert client.post('/data-sources', json=c, headers=auth()).status_code == 422
    assert client.post('/data-sources', json=config(), headers={'X-Restock-Approval': ''}).status_code == 403
    assert client.get('/data-sources').json()['sources'] == []


def test_stage_then_publish_complete_csv_and_independent_values(client, clock):
    sid = source(client)
    r = observe(client, sid, batch(clock)); assert r.status_code == 200, r.text
    assert r.json()['state'] == 'STAGED'
    assert client.get(f'/data-sources/{sid}/publications').json()['publications'] == []
    assert publish(client, sid).status_code == 409
    assert not main.COLLECTION_DIR.exists()
    clock[0] += timedelta(seconds=60)
    r = publish(client, sid); assert r.status_code == 200, r.text
    did = r.json()['dataset_id']
    manifest = artifact(client, sid, did, 'manifest.json').json()
    assert manifest['merchant_id'] == 'merchant-a' and manifest['environment'] == 'test'
    assert manifest['dataset_id'] == did
    sales = list(csv.DictReader(io.StringIO(artifact(client, sid, did, 'sales.csv').content.decode('utf-8-sig'))))
    assert len(sales) == 28 and sum(int(row['sold_qty']) for row in sales) == 112
    inv = list(csv.DictReader(io.StringIO(artifact(client, sid, did, 'inventory.csv').text)))
    assert inv[0]['available_qty'] == '0'
    assert client.get('/datasets/' + did).status_code == 200
    assert client.get('/tasks').json()['tasks'] == []
    assert artifact(client, sid, did, '.env').status_code == 404


def test_changes_coalesce_and_equivalent_data_does_not_republish(client, clock):
    sid = source(client); first = ready(client, clock, sid)
    clock[0] += timedelta(seconds=5)
    p = batch(clock, 'b2', '5'); assert observe(client, sid, p).status_code == 200
    assert observe(client, sid, p).json()['replayed'] is True
    conflict = copy.deepcopy(p); conflict['inventory'][0]['available_qty'] = '7'
    assert observe(client, sid, conflict).status_code == 409
    clock[0] += timedelta(seconds=5)
    assert observe(client, sid, batch(clock, 'b3', '9')).status_code == 200
    clock[0] += timedelta(seconds=50)
    r = publish(client, sid); assert r.status_code == 200, r.text
    second = r.json()['dataset_id']; assert first != second
    assert ',9,0,' in artifact(client, sid, second, 'inventory.csv').text
    original_bytes = artifact(client, sid, second, 'manifest.json').content
    clock[0] += timedelta(seconds=1)
    p = batch(clock, 'b4', '009'); p['sales'].reverse()
    for row in p['sales']: row['sold_qty'] = '4.000'
    assert observe(client, sid, p).json()['state'] == 'UNCHANGED'
    clock[0] += timedelta(seconds=60)
    assert publish(client, sid).json()['state'] == 'UNCHANGED'
    assert artifact(client, sid, second, 'manifest.json').content == original_bytes
    assert len(client.get(f'/data-sources/{sid}/publications').json()['publications']) == 2
    assert artifact(client, sid, first, 'inventory.csv').status_code == 200


@pytest.mark.parametrize('fault', ['incomplete', 'row_count', 'foreign', 'missing_day', 'extra_field', 'future', 'old'])
def test_bad_observations_cannot_replace_formal_data(client, clock, fault):
    sid = source(client); did = ready(client, clock, sid)
    before = artifact(client, sid, did, 'inventory.csv').content
    clock[0] += timedelta(seconds=10)
    p = batch(clock, 'bad', '50')
    if fault == 'incomplete': p['complete'] = False
    if fault == 'row_count': p['row_counts']['sales'] = 29
    if fault == 'foreign': p['merchant_id'] = 'someone-else'
    if fault == 'missing_day': p['sales'].pop(); p['row_counts']['sales'] = 27
    if fault == 'extra_field': p['inventory'][0]['hidden'] = 'unknown'
    if fault == 'future': p['source_as_of'] = (clock[0] + timedelta(hours=1)).isoformat()
    if fault == 'old':
        p['observed_at'] = p['source_as_of'] = (clock[0] - timedelta(hours=2)).isoformat()
    r = observe(client, sid, p); assert r.status_code in (409, 422), r.text
    assert artifact(client, sid, did, 'inventory.csv').content == before
    assert client.get(f'/data-sources/{sid}').json()['state']['latest_dataset_id'] == did
    assert len(client.get(f'/data-sources/{sid}/publications').json()['publications']) == 1


def test_pending_state_survives_db_reopen_and_settings_are_independent(client, clock, monkeypatch):
    sid = source(client)
    assert observe(client, sid, batch(clock)).status_code == 200
    changed = settings(check_every_seconds=7200)
    assert client.put(f'/data-sources/{sid}/settings', json=changed, headers=auth()).status_code == 200
    path = main.get_conn().execute('PRAGMA database_list').fetchone()[2]
    main._conn.close(); main._conn = None; monkeypatch.setattr(main, 'DB_PATH', path)
    state = client.get(f'/data-sources/{sid}').json()
    assert state['config']['schedule'] == changed and state['state']['pending_snapshot'] is not None
    clock[0] += timedelta(seconds=60)
    assert publish(client, sid).status_code == 200


def test_published_files_recover_after_database_failure(client, clock, monkeypatch):
    sid = source(client)
    assert observe(client, sid, batch(clock)).status_code == 200
    clock[0] += timedelta(seconds=60)
    original = db.save_publication
    def fail(*args, **kwargs): raise OSError('injected after file promotion')
    monkeypatch.setattr(db, 'save_publication', fail)
    assert publish(client, sid).status_code == 503
    assert client.get(f'/data-sources/{sid}/publications').json()['publications'] == []
    assert main.get_conn().execute('SELECT count(*) FROM datasets').fetchone()[0] == 0
    assert list(main.COLLECTION_DIR.rglob('manifest.json'))
    monkeypatch.setattr(db, 'save_publication', original)
    clock[0] += timedelta(seconds=60)
    assert publish(client, sid).status_code == 200
    assert len(list(main.COLLECTION_DIR.rglob('manifest.json'))) == 1


def test_source_change_blocks_old_approval_before_csv_save(client, clock):
    sid = source(client); did = ready(client, clock, sid)
    r = client.post('/tasks', json={'source_id': sid, 'dataset_id': did, 'policy': {'budget_cents': 10000}})
    assert r.status_code == 200, r.text
    task = r.json()['task_id']; original = r.json()['plan']
    assert client.post(f'/tasks/{task}/approve').status_code == 200
    clock[0] += timedelta(seconds=1)
    assert observe(client, sid, batch(clock, 'changed', '80')).status_code == 200
    assert client.post(f'/tasks/{task}/execute').status_code == 409
    assert client.post(f'/tasks/{task}/approve').status_code == 409
    status = client.get(f'/tasks/{task}').json()
    assert status['approved_plan_hash'] is None
    assert status['latest_plan'] == original
    assert client.get(f'/tasks/{task}/executions').json()['records'] == []
    assert publish(client, sid).status_code == 409
    clock[0] += timedelta(seconds=59)
    new = publish(client, sid).json()['dataset_id']
    assert client.post(f'/tasks/{task}/revise', json={'dataset_id': new}).status_code == 200
    assert client.post(f'/tasks/{task}/approve').status_code == 200


def test_stale_or_corrupted_source_blocks_new_task_and_approval(client, clock):
    sid = source(client); did = ready(client, clock, sid)
    created = client.post('/tasks', json={'source_id': sid, 'dataset_id': did, 'policy': {'budget_cents': 10000}}).json()
    clock[0] += timedelta(seconds=601)
    assert client.post(f"/tasks/{created['task_id']}/approve").status_code == 409
    assert client.post('/tasks', json={'source_id': sid, 'dataset_id': did, 'policy': {'budget_cents': 10000}}).status_code == 409
    assert observe(client, sid, batch(clock, 'refresh')).status_code == 200
    file = main.COLLECTION_DIR / sid / did / 'inventory.csv'
    file.write_text('corrupt', encoding='utf-8')
    assert artifact(client, sid, did, 'inventory.csv').status_code == 409
    assert client.post(f"/tasks/{created['task_id']}/approve").status_code == 409


def test_omitting_source_cannot_bypass_freshness_guard(client, clock):
    sid = source(client); did = ready(client, clock, sid)
    assert client.post('/tasks', json={'dataset_id': did, 'policy': {'budget_cents': 10000}}).status_code == 422
    assert client.get('/tasks').json()['tasks'] == []


def test_failed_disk_write_keeps_old_version_and_pending_data(client, clock, monkeypatch):
    from app import collection
    sid = source(client); did = ready(client, clock, sid)
    clock[0] += timedelta(seconds=1)
    observe(client, sid, batch(clock, 'second', '9'))
    clock[0] += timedelta(seconds=59)
    def fail(*args): raise OSError('synthetic disk full')
    monkeypatch.setattr(collection, 'write_package', fail)
    assert publish(client, sid).status_code == 503
    state = client.get(f'/data-sources/{sid}').json()['state']
    assert state['pending_snapshot']['inventory'][0]['available_qty'] == '9'
    assert state['latest_dataset_id'] == did and state['last_publish_error']
    assert artifact(client, sid, did, 'inventory.csv').status_code == 200


def test_concurrent_publishing_has_one_formal_version(client, clock):
    from app import collection
    from concurrent.futures import ThreadPoolExecutor
    sid = source(client); observe(client, sid, batch(clock))
    clock[0] += timedelta(seconds=60)
    def attempt():
        try: return collection.publish(main.get_conn(), main.COLLECTION_DIR, sid)['state']
        except collection.CollectionError: return 'not_due'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ['PUBLISHED', 'not_due']
    assert len(list(main.COLLECTION_DIR.rglob('manifest.json'))) == 1
    assert len(db.list_publications(main.get_conn(), sid)) == 1


def test_background_worker_saves_without_creating_task(client, clock):
    import time
    from app import collection
    sid = source(client); observe(client, sid, batch(clock))
    clock[0] += timedelta(seconds=60)
    worker = collection.PublicationWorker(main.get_conn, lambda: main.COLLECTION_DIR, interval=.01)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not db.list_publications(main.get_conn(), sid):
            time.sleep(.01)
        assert len(db.list_publications(main.get_conn(), sid)) == 1
        assert db.list_tasks(main.get_conn()) == []
    finally: worker.stop()
    assert not worker.thread.is_alive()


def test_source_identity_and_settings_cannot_be_overwritten(client, clock):
    sid = source(client)
    changed = settings(); changed['merchant_id'] = 'different'
    assert client.put(f'/data-sources/{sid}/settings', json=changed, headers=auth()).status_code == 422
    assert client.put(f'/data-sources/{sid}/settings', json=settings(), headers={'X-Restock-Approval': ''}).status_code == 403
    assert observe(client, sid, batch(clock)).status_code == 200
    assert client.post(f'/data-sources/{sid}/observations', json=batch(clock, 'no-auth'), headers={'X-Restock-Approval': ''}).status_code == 403


def test_data_reversion_reuses_immutable_formal_files(client, clock):
    sid = source(client); first = ready(client, clock, sid)
    first_bytes = artifact(client, sid, first, 'manifest.json').content
    clock[0] += timedelta(seconds=1); observe(client, sid, batch(clock, 'second', '9'))
    clock[0] += timedelta(seconds=59); second = publish(client, sid).json()['dataset_id']
    clock[0] += timedelta(seconds=1); observe(client, sid, batch(clock, 'third', '0'))
    clock[0] += timedelta(seconds=59); assert publish(client, sid).json()['dataset_id'] == first
    assert first != second and len(db.list_publications(main.get_conn(), sid)) == 2
    assert artifact(client, sid, first, 'manifest.json').content == first_bytes


def test_normalizing_decimal_preserves_all_digits_without_context_rounding(client, clock):
    sid = source(client)
    value = '0.12345678901234567890123456789012345'
    p = batch(clock)
    p['sales'][0]['sold_qty'] = value
    assert observe(client, sid, p).status_code == 200
    stored = client.get(f'/data-sources/{sid}').json()['state']['pending_snapshot']
    assert stored['sales'][0]['sold_qty'] == value
    clock[0] += timedelta(seconds=60)
    did = publish(client, sid).json()['dataset_id']
    assert value in artifact(client, sid, did, 'sales.csv').text


def test_extreme_timezone_is_rejected_without_server_error(client, clock):
    sid = source(client)
    p = batch(clock, observed_at='0001-01-01T00:00:00+14:00')
    assert observe(client, sid, p).status_code == 422


def test_background_corruption_is_visible_and_retries_are_scheduled(client, clock):
    from app import collection
    sid = source(client); did = ready(client, clock, sid)
    (main.COLLECTION_DIR / sid / did / 'sales.csv').write_text('damaged', encoding='utf-8')
    clock[0] += timedelta(seconds=60)
    collection.publish_due(main.get_conn(), main.COLLECTION_DIR)
    state = db.get_source(main.get_conn(), sid)['state']
    assert '指纹' in state['last_publish_error']
    assert collection.moment(state['next_save_at']) > clock[0]
    assert len(db.list_publications(main.get_conn(), sid)) == 1


def test_preexisting_manual_task_is_not_retroactively_bound_by_identical_source_data(client, clock, tmp_path, monkeypatch):
    from tests.test_data_contract import write_dataset
    manual = tmp_path/'manual'; write_dataset(manual)
    monkeypatch.setattr(main, 'DATA_DIR', manual)
    did = client.post('/datasets/example').json()['dataset_id']
    result = client.post('/tasks', json={'dataset_id':did, 'policy':{'budget_cents':10000}})
    assert result.status_code == 200
    task = result.json()['task_id']
    sid = source(client)
    assert ready(client, clock, sid) == did
    assert db.task_source(main.get_conn(), task) is None
    # Matching values are not evidence that this older manual task belongs to that shop.
    assert client.post(f'/tasks/{task}/approve').status_code == 200
    assert client.post(f'/tasks/{task}/execute').status_code == 200


def test_source_calendar_day_uses_declared_offset_not_utc_date(client, clock):
    clock[0] = datetime(2026, 10, 28, 17, tzinfo=timezone.utc)
    sid = source(client)
    p = batch(clock, source_as_of='2026-10-29T01:00:00+08:00')
    for row in p['sales']:
        row['date'] = (datetime.fromisoformat(row['date']) + timedelta(days=1)).date().isoformat()
    assert observe(client, sid, p).status_code == 200
    state = client.get(f'/data-sources/{sid}').json()['state']
    assert state['pending_snapshot']['window_end'] == '2026-10-29'
    assert state['source_as_of'].endswith('+08:00')


def test_successful_unchanged_save_clears_previous_publish_error(client, clock):
    from app import collection
    sid = source(client); did = ready(client, clock, sid)
    target = main.COLLECTION_DIR / sid / did / 'sales.csv'
    original = target.read_bytes(); target.write_bytes(b'bad')
    clock[0] += timedelta(seconds=60)
    collection.publish_due(main.get_conn(), main.COLLECTION_DIR)
    assert db.get_source(main.get_conn(), sid)['state']['last_publish_error']
    target.write_bytes(original)
    clock[0] += timedelta(seconds=60)
    collection.publish_due(main.get_conn(), main.COLLECTION_DIR)
    assert db.get_source(main.get_conn(), sid)['state']['last_publish_error'] is None
    assert len(db.list_publications(main.get_conn(), sid)) == 1
