"""M5-1: organize observations and publish immutable CSV batches; never buy or plan."""
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.browser_collection import BrowserConfig
from app import db
from app.models import DataContract, DatasetSnapshot, PurchaseContext, canonical_json
from app.validation import prepare_snapshot, snapshot_hash, normalize_snapshot_numbers

COLUMNS = {
    'sales': ['date', 'sku', 'sold_qty'],
    'inventory': ['sku', 'name', 'category', 'available_qty', 'inbound_qty', 'inbound_eta'],
    'suppliers': ['sku', 'supplier_id', 'unit_cost_cents', 'lead_days', 'pack_size', 'min_order_qty'],
}

FILES = {*[name + '.csv' for name in COLUMNS], 'manifest.json'}
MAX_BATCH_BYTES = 15 * 1024 * 1024


class Schedule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    collect_every_seconds: int = Field(gt=0, le=2678400)
    save_every_seconds: int = Field(gt=0, le=2678400)
    check_every_seconds: int = Field(gt=0, le=2678400)
    max_age_seconds: int = Field(gt=0, le=2678400)


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=100)
    merchant_id: str = Field(min_length=1, max_length=100)
    warehouse_id: str = Field(min_length=1, max_length=100)
    environment: Literal['test', 'real']
    contract: DataContract
    schedule: Schedule
    browser: BrowserConfig | None = None
    purchase_system_id: str | None = Field(default=None,min_length=1,max_length=160)


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    batch_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    merchant_id: str = Field(min_length=1, max_length=100)
    warehouse_id: str = Field(min_length=1, max_length=100)
    observed_at: str = Field(max_length=40)
    source_as_of: str = Field(max_length=40)
    complete: bool
    row_counts: dict[str, int]
    sales: list[dict[str, str]] = Field(max_length=100000)
    inventory: list[dict[str, str]] = Field(max_length=10000)
    suppliers: list[dict[str, str]] = Field(max_length=10000)
    purchase_context: PurchaseContext | None = None


class CollectionError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def moment(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise CollectionError('时间必须包含时区且符合 ISO 8601。', 422) from None


def get_source(conn, sid):
    record = db.get_source(conn, sid)
    if record is None:
        raise CollectionError('找不到数据来源。', 404)
    return record


def create_source(conn, config):
    c = config.model_dump()
    if config.browser and not c["purchase_system_id"]:
        c["purchase_system_id"]="merchant-test-v1:"+config.merchant_id
    if any(not c[k].strip() for k in ('name', 'merchant_id', 'warehouse_id')):
        raise CollectionError('来源、商户和仓库名称不能为空。', 422)
    system=c.get('purchase_system_id')
    if system is not None and (not system.strip() or system!=system.strip()):
        raise CollectionError('采购系统身份不能空白或带首尾空格。',422)
    if config.browser and config.environment != "test":
        raise CollectionError("当前网页适配器只用于模拟测试页，真实平台需单独适配验收。", 422)
    if config.browser and config.contract.missing_sales != "reject":
        raise CollectionError("此模拟页面明确声明缺记录不等于零销量，不接受自动补零。", 422)
    contract = config.contract
    if (contract.quantity_unit != 'piece' or not contract.available_is_net
            or not contract.cost_includes_tax_shipping or not contract.inbound_eta_is_days):
        raise CollectionError('先确认数量、库存、采购成本和在途时间口径。', 422)
    sid = 'src-' + uuid.uuid4().hex
    now = utcnow()
    state = {'created_at': stamp(now), 'last_attempt_at': None, 'last_success_at': None,
             'observed_at': None, 'source_as_of': None, 'last_batch_id': None,
             'latest_observed_dataset_id': None, 'latest_dataset_id': None,
             'pending_snapshot': None, 'pending_batch_id': None,
             'next_collect_at': stamp(now), 'next_save_at': stamp(now + timedelta(seconds=config.schedule.save_every_seconds)),
             'last_saved_at': None, 'last_problem': None, 'last_publish_error': None}
    db.create_source(conn, sid, c, state)
    return sid


def update_schedule(conn, sid, schedule):
    with db.transaction(conn):
        record = get_source(conn, sid)
        c, state = record['config'], record['state']
        previous = c['schedule']
        now = utcnow()
        if schedule.save_every_seconds != previous['save_every_seconds']:
            state['next_save_at'] = stamp(now + timedelta(seconds=schedule.save_every_seconds))
        if schedule.collect_every_seconds != previous['collect_every_seconds']:
            state['next_collect_at'] = stamp(now + timedelta(seconds=schedule.collect_every_seconds))
        if schedule.check_every_seconds != previous['check_every_seconds']:
            for group in db.replenishment_groups(conn):
                if group['config']['source_id'] == sid:
                    group['state']['next_check_at'] = stamp(now + timedelta(seconds=schedule.check_every_seconds))
                    db.save_replenishment_group(conn, group)
        c['schedule'] = schedule.model_dump()
        db.update_source(conn, sid, state, c)


def _normalize(observation, contract):
    tables = []
    for name, fields in COLUMNS.items():
        rows = getattr(observation, name)
        if any(set(row) != set(fields) for row in rows):
            raise CollectionError(name + ' 字段缺失或含未知字段。', 422)
        tables.append([{k: value.strip() for k, value in row.items()} for row in rows])
    snapshot, errors = prepare_snapshot(*tables, contract)
    if errors:
        raise CollectionError('；'.join(errors[:20]), 422)
    return normalize_snapshot_numbers(snapshot)


def accept_observation(conn, sid, observation):
    body = canonical_json(observation.model_dump())
    if len(body.encode('utf-8')) > MAX_BATCH_BYTES:
        raise CollectionError('采集批次超过大小限制。', 413)
    payload_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    with db.transaction(conn):
        record = get_source(conn, sid)
        config, state = record['config'], record['state']
        prior = db.get_observation(conn, sid, observation.batch_id)
        if prior:
            if prior[0] != payload_hash:
                raise CollectionError('同一采集批次不能携带不同内容。')
            return {**prior[1], 'replayed': True}
        now = utcnow()
        result = None
        try:
            observed, as_of = moment(observation.observed_at), moment(observation.source_as_of)
            if as_of > observed or observed > now:
                raise CollectionError('采集时间或源数据时间在未来/顺序不正确。', 422)
            if state['observed_at'] and (observed < moment(state['observed_at']) or as_of < moment(state['source_as_of'])):
                raise CollectionError('拒绝乱序或旧批次。')
            if (now - as_of).total_seconds() > config['schedule']['max_age_seconds']:
                raise CollectionError('源数据已超过允许时效。')
            if observation.merchant_id != config['merchant_id'] or observation.warehouse_id != config['warehouse_id']:
                raise CollectionError('采集数据的商户或仓库不匹配。', 422)
            if not observation.complete or set(observation.row_counts) != set(COLUMNS):
                raise CollectionError('采集尚未完成或缺少完整性声明。', 422)
            if any(observation.row_counts[name] != len(getattr(observation, name)) for name in COLUMNS):
                raise CollectionError('采集行数与声明不一致，不能发布。', 422)
            snapshot = _normalize(observation, DataContract.model_validate(config['contract']))
            if observation.purchase_context is not None:
                from app.purchase_context import attach
                try:
                    snapshot=attach(snapshot,observation.purchase_context,observation.source_as_of)
                    if config.get('purchase_system_id') not in (None,observation.purchase_context.system_id):
                        raise ValueError('采购系统身份不能在后续批次中改变')
                except ValueError as exc:raise CollectionError(str(exc),422) from None
            elif config.get('purchase_system_id'):
                raise CollectionError('该来源已声明采购系统，后续批次不能省略采购上下文。',422)
            source_local = datetime.fromisoformat(observation.source_as_of.replace('Z', '+00:00'))
            if snapshot.window_end > source_local.date().isoformat():
                raise CollectionError('销售截止日不能晚于源数据时间。', 422)
            did = 'ds-' + snapshot_hash(snapshot)
            if state['observed_at'] == stamp(observed) and state['latest_observed_dataset_id'] != did:
                raise CollectionError('同一采集时点出现不同内容，请重新完整采集。', 422)
            same = did == state['latest_observed_dataset_id']
            if did != state['latest_dataset_id']:
                state['pending_snapshot'] = snapshot.model_dump()
                state['pending_batch_id'] = observation.batch_id
            else:
                state['pending_snapshot'] = None
                state['pending_batch_id'] = None
            if not same and state['latest_observed_dataset_id'] is not None:
                db.invalidate_source_approvals(conn, sid)
            if observation.purchase_context:config['purchase_system_id']=observation.purchase_context.system_id
            state.update(last_success_at=stamp(now), observed_at=stamp(observed), source_as_of=source_local.isoformat(),
                         last_batch_id=observation.batch_id, latest_observed_dataset_id=did,
                         next_collect_at=stamp(now + timedelta(seconds=config['schedule']['collect_every_seconds'])),
                         last_problem=None)
            result = {'state': 'UNCHANGED' if same else 'STAGED', 'dataset_id': did, 'http_status': 200}
        except CollectionError as exc:
            result = {'state': 'REJECTED', 'problems': [str(exc)], 'http_status': exc.status}
            # Delayed/outdated events cannot replace or poison a newer valid observation.
            if exc.status != 409:
                state['last_problem'] = str(exc)
        state['last_attempt_at'] = stamp(now)
        db.update_source(conn, sid, state, config=config)
        db.save_observation(conn, sid, observation.batch_id, payload_hash, result)
        return result


def _next_due(previous, interval, now):
    base = moment(previous)
    elapsed = max(0, (now - base).total_seconds())
    return stamp(base + timedelta(seconds=(int(elapsed // interval) + 1) * interval))


def _csv_bytes(snapshot, name):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=COLUMNS[name], lineterminator='\n')
    writer.writeheader()
    writer.writerows(getattr(snapshot, name))
    return stream.getvalue().encode('utf-8')


def _package_dir(root, sid, did):
    if not re.fullmatch(r'src-[0-9a-f]{32}', sid) or not re.fullmatch(r'ds-[0-9a-f]{64}', did):
        raise CollectionError('数据版本路径无效。', 404)
    root = Path(root).resolve()
    folder = root / sid / did
    if not folder.resolve().is_relative_to(root) or folder.is_symlink() or folder.parent.is_symlink():
        raise CollectionError('数据目录不允许指向其他位置。')
    return folder


def read_package(root, sid, did, expected=None):
    folder = _package_dir(root, sid, did)
    try:
        if any((folder / name).is_symlink() for name in FILES):
            raise ValueError()
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        if type(manifest.get('schema')) is not int or manifest['schema'] != (2 if manifest.get('purchase_context') is not None else 1):
            raise ValueError()
        if manifest['source_id'] != sid or manifest['dataset_id'] != did or set(manifest['files']) != FILES - {'manifest.json'}:
            raise ValueError()
        if expected is not None and manifest != expected:
            raise ValueError()
        data = {}
        for name, info in manifest['files'].items():
            data[name] = (folder / name).read_bytes()
            if hashlib.sha256(data[name]).hexdigest() != info['sha256'] or len(data[name]) != info['bytes']:
                raise ValueError()
        from app.datasets import ingest
        snapshot, problems = ingest({n: data[n + '.csv'] for n in COLUMNS}, DataContract.model_validate(manifest['contract']))
        if not problems and manifest.get('purchase_context') is not None:
            from app.purchase_context import attach
            snapshot=attach(snapshot,manifest['purchase_context'],manifest['source_as_of'])
        if problems or 'ds-' + snapshot_hash(snapshot) != did:
            raise ValueError()
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise CollectionError('正式 CSV 或清单缺失/指纹不符，已阻止使用。') from None


def write_package(root, sid, config, state, snapshot):
    did = 'ds-' + snapshot_hash(snapshot)
    target = _package_dir(root, sid, did)
    if target.exists():
        return read_package(root, sid, did)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / ('.pending-' + uuid.uuid4().hex)
    staging.mkdir()
    manifest = {'schema': 1, 'source_id': sid, 'merchant_id': config['merchant_id'],
                'warehouse_id': config['warehouse_id'], 'environment': config['environment'],
                'dataset_id': did, 'dataset_hash': snapshot_hash(snapshot),
                'batch_id': state['pending_batch_id'], 'observed_at': state['observed_at'],
                'source_as_of': state['source_as_of'], 'published_at': stamp(utcnow()),
                'window_start': snapshot.window_start, 'window_end': snapshot.window_end,
                'contract': snapshot.contract.model_dump(),
                'collection': {'adapter':config['browser']['adapter'], 'sku_scope':config['browser']['sku_scope'],
                               'run_id':state['pending_batch_id'], 'quantity_basis':'net sales and available pieces',
                               'eta_reference':'source_as_of local calendar date'} if config.get('browser') else None,
                'files': {}}
    if snapshot.purchase_context is not None:
        manifest.update(schema=2,purchase_context=snapshot.purchase_context.model_dump())
    for name in COLUMNS:
        data = _csv_bytes(snapshot, name)
        with (staging / (name + '.csv')).open('xb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        manifest['files'][name + '.csv'] = {'sha256': hashlib.sha256(data).hexdigest(),
                                          'bytes': len(data), 'rows': len(getattr(snapshot, name))}
    with (staging / 'manifest.json').open('x', encoding='utf-8') as output:
        output.write(canonical_json(manifest)); output.flush(); os.fsync(output.fileno())
    # Incomplete staging directories never appear in the DB publication index.
    # Same-filesystem directory rename promotes the complete four-file batch.
    try:
        staging.rename(target)
    except FileExistsError:
        return read_package(root, sid, did)
    return read_package(root, sid, did, manifest)


def _fresh(record):
    state, config = record['state'], record['config']
    if state['last_problem']:
        raise CollectionError('最近采集未通过：' + state['last_problem'])
    if not state['source_as_of']:
        raise CollectionError('还没有完整有效的数据。')
    if (utcnow() - moment(state['source_as_of'])).total_seconds() > config['schedule']['max_age_seconds']:
        raise CollectionError('来源数据已过期，请重新采集。')


def publish(conn, root, sid):
    try:
        with db.transaction(conn):
            record = get_source(conn, sid)
            state, config = record['state'], record['config']
            now = utcnow()
            if moment(state['next_save_at']) > now:
                raise CollectionError('尚未到保存周期。')
            _fresh(record)
            did = state['latest_dataset_id']
            if state['pending_snapshot'] is not None:
                snapshot = DatasetSnapshot.model_validate(state['pending_snapshot'])
                manifest = write_package(root, sid, config, state, snapshot)
                if any(manifest[k] != config[k] for k in ('merchant_id', 'warehouse_id', 'environment')):
                    raise CollectionError('已有文件的来源身份不匹配。')
                origin = db.get_observation(conn, sid, manifest['batch_id'])
                if origin is None or origin[1].get('dataset_id') != manifest['dataset_id']:
                    raise CollectionError('正式文件缺少对应的有效采集记录。')
                did = db.save_dataset(conn, snapshot)
                db.save_publication(conn, sid, did, manifest['batch_id'], manifest)
                state.update(latest_dataset_id=did, pending_snapshot=None, pending_batch_id=None,
                             last_saved_at=stamp(now), last_publish_error=None)
                result = 'PUBLISHED'
            else:
                if did:
                    read_package(root, sid, did, db.get_publication(conn, sid, did))
                result = 'UNCHANGED'
            state['last_publish_error'] = None
            state['next_save_at'] = _next_due(state['next_save_at'], config['schedule']['save_every_seconds'], now)
            db.update_source(conn, sid, state)
            return {'state': result, 'dataset_id': did}
    except CollectionError:
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        with db.transaction(conn):
            record = get_source(conn, sid)
            state = record['state']
            state['last_publish_error'] = '保存失败；暂存仍保留，将在下个周期重试。'
            state['next_save_at'] = _next_due(state['next_save_at'], record['config']['schedule']['save_every_seconds'], utcnow())
            db.update_source(conn, sid, state)
        raise CollectionError('正式数据保存失败，未发布不完整版本。', 503) from exc


def assert_ready(conn, root, sid, did):
    record = get_source(conn, sid)
    _fresh(record)
    state = record['state']
    if not did or did != state['latest_dataset_id'] or did != state['latest_observed_dataset_id']:
        raise CollectionError('已有更新的数据或数据仍待保存，请发布并复核最新版本。')
    manifest = db.get_publication(conn, sid, did)
    if manifest is None:
        raise CollectionError('来源尚未发布该数据版本。')
    read_package(root, sid, did, manifest)


def source_info(conn, sid, root=None):
    record = get_source(conn, sid)
    if root is not None:
        try:
            assert_ready(conn, root, sid, record['state']['latest_dataset_id'])
            record['data_status'] = {'ready': True, 'reason': None}
        except CollectionError as exc:
            record['data_status'] = {'ready': False, 'reason': str(exc)}
    record['automation'] = {'collection': bool(record['config'].get('browser', {}).get('enabled')) if record['config'].get('browser') else False, 'publication': True,
        'replenishment': any(g['config']['source_id']==sid and g['config']['enabled'] for g in db.replenishment_groups(conn))}
    return record


def task_data_status(conn, root, task_id, did):
    sid = db.task_source(conn, task_id)
    if sid is None:
        return None
    try:
        assert_ready(conn, root, sid, did)
        return {'source_id': sid, 'ready': True, 'reason': None}
    except CollectionError as exc:
        return {'source_id': sid, 'ready': False, 'reason': str(exc)}


def publication_bytes(conn, root, sid, did, name):
    if name not in FILES:
        raise CollectionError('不存在此数据文件。', 404)
    manifest = db.get_publication(conn, sid, did)
    if manifest is None:
        raise CollectionError('找不到已发布数据版本。', 404)
    read_package(root, sid, did, manifest)
    return (_package_dir(root, sid, did) / name).read_bytes()


def publish_due(conn, root):
    for record in db.list_sources(conn):
        state = record['state']
        if state['source_as_of'] and not state['last_problem'] and moment(state['next_save_at']) <= utcnow():
            try:
                publish(conn, root, record['source_id'])
            except CollectionError as exc:
                with db.transaction(conn):
                    current = get_source(conn, record['source_id'])
                    status = current['state']
                    if moment(status['next_save_at']) <= utcnow():
                        status['last_publish_error'] = '未发布：' + str(exc)
                        status['next_save_at'] = _next_due(status['next_save_at'], current['config']['schedule']['save_every_seconds'], utcnow())
                        db.update_source(conn, record['source_id'], status)


class PublicationWorker:
    """Only publishes staged data. Does not collect pages or start restock tasks."""
    def __init__(self, conn, root, interval=5):
        self.conn, self.root, self.interval = conn, root, interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='restock-csv-publisher', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(self.interval):
            try:
                conn = self.conn() if callable(self.conn) else self.conn
                root = self.root() if callable(self.root) else self.root
                publish_due(conn, root)
            except (sqlite3.Error, OSError):
                logging.getLogger(__name__).warning('CSV publication cycle failed; next cycle will retry.')

    def stop(self):
        self.stop_event.set()
        self.thread.join()
