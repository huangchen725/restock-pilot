"""Derive current procurement facts from the one persistent ledger.

Observations are append-only. An empty lookup never clears an outstanding
submission intent; only a verified real receipt can resolve that uncertainty.
"""
from copy import deepcopy
from app import db
from app.execution import build_supplier_orders, reconcile_receipt, summarize_execution

SUCCESS = {'created', 'recovered_after_error', 'already_exists'}


def has_execution(conn, task_id, version=None):
    records = db.list_execution_records(conn, task_id)
    events = db.list_execution_events(conn, task_id)
    return any(version is None or r['version'] == version for r in records) or any(
        e['event_type'] == 'execution_started' and (version is None or e['payload'].get('version') == version)
        for e in events)


def _plans(conn, task_id):
    return {v: db.get_plan(conn, task_id, v)[0] for v in db.list_plan_versions(conn, task_id)}


def effective_orders(conn, task_id):
    plans = _plans(conn, task_id)
    expected = {o['external_ref']: (p, o) for p in plans.values() for o in build_supplier_orders(p)}
    records = db.list_execution_records(conn, task_id)
    events = db.list_execution_events(conn, task_id)
    latest = {r['external_ref']: r for r in records}
    past_submission = {r['external_ref'] for r in records if (r['receipt'].get('receipt') is not None
        or r['receipt'].get('submission_state') == 'unknown' or r['status'] == 'unknown')}
    started = {}
    known_initial = set()
    intents = {}
    for event in events:
        if event['event_type'] == 'execution_started':
            for order in event['payload'].get('orders', []):
                started[order['external_ref']] = event
                if event['payload'].get('protocol_version') == 2 and event['payload'].get('mode') == 'execute':
                    known_initial.add(order['external_ref'])
        elif event['event_type'] == 'submit_intent':
            intents[event['external_ref']] = event['seq']
    states = {}
    for ref in set(latest) | set(started) | set(intents):
        record = latest.get(ref)
        result = deepcopy(record['receipt']) if record else {}
        plan, order = expected.get(ref, (None, None))
        state, problems = 'unknown', []
        if order is None or (record and record['version'] != plan.version):
            problems = ['记录不属于可核对的正式方案']
        elif intents.get(ref, 0) > result.get('ledger_seq', 0):
            problems = ['提交意图之后没有可靠结果，须按原参考号核对']
        elif result.get('receipt') is not None:
            problems = list(result.get('problems') or []) + reconcile_receipt(order, result['receipt'])
            state = 'created' if not problems and record['status'] in SUCCESS else 'mismatch'
            if state == 'mismatch' and not problems:
                problems = ['回执状态不是已验证的有效申请']
        elif result.get('submission_state') == 'not_attempted' and record['status'] == 'failed':
            if ref in past_submission or ref in intents:
                problems = ['后续未尝试记录不能抹掉历史提交意图、未知结果或已有申请']
            else:
                state = 'not_submitted'
        elif result.get('submission_state') == 'unknown' or (record and record['status'] == 'unknown'):
            problems = list(result.get('problems') or ['提交结果未知'])
        elif not record and ref in known_initial and ref not in intents:
            state = 'not_submitted'  # New protocol always persists intent before any submit.
        else:
            problems = ['缺少可证明未提交的记录，须先核对']
        states[ref] = {'external_ref': ref, 'version': plan.version if plan else (record or {}).get('version'),
                       'dataset_hash': plan.dataset_hash if plan else None,
                       'supplier_id': order['supplier_id'] if order else result.get('supplier_id'),
                       'state': state, 'problems': list(dict.fromkeys(problems)), 'result': result}
    return states


def reconciliation_results(conn, task_id, plan, observations):
    expected = {o['external_ref']: o for o in build_supplier_orders(plan)}
    actual = {r['external_ref']: r for r in observations}
    if len(actual) != len(observations) or set(actual) != set(expected):
        raise ValueError('核对结果没有准确覆盖本方案的全部订单')
    history = effective_orders(conn, task_id)
    results = []
    for ref, order in expected.items():
        raw = actual[ref]
        result = {'external_ref': ref, 'supplier_id': order['supplier_id'],
                  'order_id': raw.get('order_id'), 'receipt': raw.get('receipt'),
                  'attempt_log': raw.get('attempt_log', []), 'reconciled': True, 'error': None}
        if raw.get('state') == 'created' and isinstance(raw.get('receipt'), dict):
            problems = list(raw.get('problems') or []) + reconcile_receipt(order, raw['receipt'])
            result.update(status='already_exists', problems=list(dict.fromkeys(problems)))
        elif raw.get('state') == 'not_submitted' and history.get(ref, {}).get('state') == 'not_submitted':
            result.update(status='failed', submission_state='not_attempted', problems=[])
        else:
            result.update(status='unknown', submission_state='unknown', receipt=None, order_id=None,
                          problems=list(raw.get('problems') or ['空回查不能解除历史未知或证明已发出的申请未提交']))
        results.append(result)
    return results


def record_observation(conn, task_id, plan, result, event_type, metadata):
    """Receipt and its corresponding audit event commit or roll back together."""
    with db.transaction(conn):
        seq = db.append_execution_event(conn, task_id, event_type, result['external_ref'], {**result, **metadata})
        stored = {**result, 'ledger_seq': seq}
        db.save_execution_record(conn, task_id, plan.plan_id, plan.version,
                                 result['external_ref'], result['status'], stored)


def coverage(conn, task_id, dataset_hash):
    states = effective_orders(conn, task_id)
    quantities, amount, seen = {}, 0, set()
    for row in states.values():
        if row['state'] in ('unknown', 'mismatch'):
            raise ValueError('存在结果未知或回读不一致的申请，核对完成前不能修订方案')
        if row['dataset_hash'] != dataset_hash:
            raise ValueError('执行后的任务不能更换数据快照或忽略旧申请')
        if row['state'] != 'created':
            continue
        receipt = row['result']['receipt']
        for line in receipt['lines']:
            key = (receipt['order_id'], line['sku'])
            if key not in seen:
                seen.add(key)
                quantities[line['sku']] = quantities.get(line['sku'], 0) + line['order_qty']
                amount += line['order_qty'] * line['unit_cost_cents']
    return quantities, amount


def task_summary(conn, task_id):
    task = db.get_task(conn, task_id)
    versions = db.list_plan_versions(conn, task_id)
    current_version = task['approved_version'] or (versions[-1] if versions else None)
    states = effective_orders(conn, task_id)
    valid, pending, seen = [], [], set()
    for row in states.values():
        result = row['result']
        if row['state'] == 'created':
            receipt = result['receipt']
            if receipt['order_id'] not in seen:
                seen.add(receipt['order_id'])
                valid.append({'external_ref': row['external_ref'], 'supplier_id': row['supplier_id'],
                              'order_id': receipt['order_id'], 'version': row['version'],
                              'status': 'SUBMITTED', 'lines': receipt['lines'],
                              'total_cost_cents': receipt['total_cost_cents']})
        elif row['state'] in ('unknown', 'mismatch') or row['version'] == current_version:
            pending.append({'external_ref': row['external_ref'], 'supplier_id': row['supplier_id'],
                            'version': row['version'], 'status': row['state'], 'problems': row['problems'],
                            'error': result.get('error')})
    return {'task_id': task_id, 'state': task['state'], 'valid_orders': valid, 'pending_items': pending,
            'total_created_cents': sum(o['total_cost_cents'] for o in valid),
            'has_execution': has_execution(conn, task_id)}


def _audit(conn, task_id):
    """Strict per-attempt replay. Missing historical evidence stays visibly incomplete."""
    events = db.list_execution_events(conn, task_id)
    plans = _plans(conn, task_id)
    approvals = {(a['payload'].get('version'), a['payload'].get('plan_hash'))
                 for a in db.list_activity(conn, task_id) if a['kind'] == 'approved'}
    issues, attempts, current = [], set(), None
    checks = {}
    if not events:
        issues.append('没有执行事件')
    def problem(message):
        issues.append(message)
    from app.models import plan_hash
    for event in events:
        kind, data, ref = event['event_type'], event['payload'], event['external_ref']
        if kind == 'reconciliation_observed':
            p = plans.get(data.get('version'))
            check_id = data.get('check_id')
            expected_check = {o['external_ref']: o for o in build_supplier_orders(p)} if p else {}
            if current is not None or not check_id or not p or data.get('plan_hash') != plan_hash(p) or ref not in expected_check:
                problem('核对事件授权或顺序无效')
            else:
                check = checks.setdefault(check_id, {'version': p.version, 'hash': plan_hash(p), 'expected': set(expected_check), 'seen': set()})
                if ref in check['seen'] or check['version'] != p.version or check['hash'] != data.get('plan_hash'):
                    problem('核对事件重复或版本不一致')
                check['seen'].add(ref)
                if data.get('receipt') is not None and reconcile_receipt(expected_check[ref],data['receipt']) and not data.get('problems'):
                    problem('核对事件隐瞒回读不一致')
            continue
        if kind == 'execution_started':
            if current is not None: problem('前一次尝试尚未结束')
            aid, version, h = data.get('attempt_id'), data.get('version'), data.get('plan_hash')
            p = plans.get(version)
            expected = {o['external_ref']: o for o in build_supplier_orders(p)} if p else {}
            declared = data.get('orders', [])
            if not aid or aid in attempts or data.get('protocol_version') != 2:
                problem('尝试标识缺失、重复或旧协议证据不完整')
            attempts.add(aid)
            if not p or not h or h != plan_hash(p) or (version, h) not in approvals:
                problem('开始事件没有匹配正式方案与人工批准')
            if len(declared) != len(expected) or {o.get('external_ref') for o in declared} != set(expected):
                problem('开始订单集合不完整')
            for o in declared:
                expected_order = expected.get(o.get('external_ref'), {})
                if any(o.get(k) != expected_order.get(k) for k in ('supplier_id', 'total_cost_cents')) or o.get('lines') != len(expected_order.get('lines', [])):
                    problem('开始订单内容不符合批准方案')
            current = {'id': aid, 'version': version, 'hash': h, 'expected': expected, 'results': {}, 'intents': set(), 'phases': {r: {} for r in expected}}
            continue
        if current is None:
            problem('存在不属于任何执行尝试的事件'); continue
        if (data.get('attempt_id'), data.get('version'), data.get('plan_hash')) != (current['id'], current['version'], current['hash']):
            problem('尝试、版本或指纹不一致')
        if kind == 'execution_finished':
            if set(current['results']) != set(current['expected']): problem('缺少订单结果')
            summary = summarize_execution(list(current['results'].values()))
            if any(data.get(k) != summary[k] for k in ('state', 'succeeded', 'total')) or data.get('final_task_state') != summary['state']:
                problem('结束状态或计数不一致')
            current = None
        elif ref not in current['expected']:
            problem('事件订单不属于批准集合')
        elif kind == 'order_result':
            phase = current['phases'][ref]
            if not data.get('reconciled'):
                if data.get('status') == 'created' and not phase.get('readback'): problem('创建结果缺少回读事件')
                if data.get('status') == 'already_exists' and phase.get('found') is not True: problem('已存在结果缺少成功查重')
                if data.get('submission_state') == 'not_attempted' and ref in current['intents']: problem('已发出意图却标为未尝试提交')
            if ref in current['results']: problem('同一次尝试存在重复结果')
            if data.get('receipt') is not None:
                actual = reconcile_receipt(current['expected'][ref], data['receipt'])
                if actual and not data.get('problems'): problem('回执不一致被隐瞒')
            if data.get('status') in ('created', 'recovered_after_error') and not data.get('reconciled') and ref not in current['intents']:
                problem('提交没有持久化意图')
            if data.get('status') not in SUCCESS | {'failed', 'unknown'}: problem('订单结果状态无效')
            current['results'][ref] = data
        elif kind == 'submit_intent':
            if current['phases'][ref].get('found') is not False: problem('提交意图之前没有明确空查重')
            if ref in current['intents'] or ref in current['results']: problem('重复或晚到的提交意图')
            current['intents'].add(ref)
        elif kind not in ('lookup_started', 'lookup_finished', 'readback_completed', 'order_checked'):
            problem('无法解释的执行事件')
        else:
            phase = current['phases'][ref]
            if ref in current['results']: problem('订单结果之后仍有执行过程事件')
            if kind == 'lookup_started':
                if phase.get('lookup'): problem('重复开始查重')
                phase['lookup'] = True
            elif kind == 'lookup_finished':
                if not phase.get('lookup') or 'found' in phase or type(data.get('found')) is not bool: problem('查重结果顺序或内容无效')
                phase['found'] = data.get('found')
            elif kind == 'readback_completed':
                if ref not in current['intents'] or phase.get('readback'): problem('回读发生在提交前或重复')
                phase['readback'] = True
            elif kind == 'order_checked':
                if phase.get('checked') or phase.get('lookup'): problem('恢复核对顺序无效')
                phase['checked'] = True
    if current is not None: problem('执行尝试中断，缺少结束事件')
    if any(c['seen'] != c['expected'] for c in checks.values()): problem('核对订单集合不完整')
    return {'task_id': task_id, 'events': events, 'replay_complete': not issues,
            'event_count': len(events), 'issues': list(dict.fromkeys(issues))}


def audit(conn, task_id):
    try:
        return _audit(conn, task_id)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        events = db.list_execution_events(conn, task_id)
        return {'task_id': task_id, 'events': events, 'event_count': len(events),
                'replay_complete': False, 'issues': ['审计记录格式不完整: ' + str(exc)]}
