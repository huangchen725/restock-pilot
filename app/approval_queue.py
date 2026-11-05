"""Durable one-approval handoff. Never generates approvals or retries uncertain writes."""
import logging
import threading
from datetime import timedelta
from app import db, ledger, collection
from app.models import TaskState

ACTIVE={'queued','running'}


def latest(conn,task_id):
    rows=db.execution_intents(conn,task_id)
    return rows[-1] if rows else None


def active(conn,task_id):
    rows=[r for r in db.execution_intents(conn,task_id) if r['status'] in ACTIVE]
    return rows[-1] if rows else None


def attempted(conn,record):
    # Any execution evidence is a boundary, including an orphan intent without
    # execution_started. Missing ledger events must never authorize a replay.
    tid=record['task_id'];version=record['version']
    return ledger.has_execution(conn,tid,version) or any(
        e['payload'].get('version') in (None,version) for e in db.list_execution_events(conn,tid))


def needs_check(conn,record,error=None):
    row=db.get_task(conn,record['task_id'])
    if row and row['state']=='APPROVED':
        db.upsert_task(conn,record['task_id'],TaskState.NEEDS_RECONCILIATION,
                       row['approved_plan_hash'],row['approved_version'])
    return save(conn,record,'needs_reconciliation',error)


def expired(record):
    return collection.moment(record['expires_at']) <= collection.utcnow()


def save(conn,record,status,error=None):
    record={**record,'status':status,'updated_at':collection.stamp(collection.utcnow()),'error':error}
    db.save_execution_intent(conn,record)
    return record


def cancel(conn,record,status='cancelled',reason='批准已失效，请重新核对。'):
    with db.transaction(conn):
        row=db.get_task(conn,record['task_id'])
        if row and row['state']=='APPROVED' and row['approved_plan_hash']==record['plan_hash']:
            db.upsert_task(conn,record['task_id'],TaskState.NEEDS_REVIEW)
        save(conn,record,status,reason)
        db.activity(conn,record['task_id'],'execution_intent_closed',
                    {'request_id':record['request_id'],'status':status,'reason':reason})


def validate_start(conn,task_id,plan):
    record=active(conn,task_id)
    if not record:return
    if attempted(conn,record):raise ValueError('已有执行证据；请先核对原申请，不能重新执行。')
    from app.models import plan_hash
    if (record['version']!=plan.version or record['plan_hash']!=plan_hash(plan)
            or record['dataset_id']!=plan.dataset_id or expired(record)):
        raise ValueError('排队批准已过期或版本不匹配，请重新核对批准。')


def recover(conn):
    """Called only after the API has recovered interrupted task ownership on startup."""
    with db.transaction(conn):
        for record in db.execution_intents(conn):
            if record['status'] not in ACTIVE:continue
            row=db.get_task(conn,record['task_id'])
            if attempted(conn,record):
                if row and row['state']=='COMPLETED':save(conn,record,'succeeded')
                else:needs_check(conn,record,'执行证据已存在；先核对原单据，不自动重放采购。')
            elif record['status']=='running':
                save(conn,record,'queued')


def dispatch_one(api):
    conn=api.get_conn();chosen=None
    with db.transaction(conn):
        for record in db.execution_intents(conn):
            if record['status']!='queued':continue
            tid=record['task_id'];row=db.get_task(conn,tid)
            if db.get_context(conn,tid)['busy']:continue
            if attempted(conn,record):
                if row['state']=='COMPLETED':save(conn,record,'succeeded')
                else:needs_check(conn,record,'执行证据已存在；请先核对。')
                continue
            if expired(record):cancel(conn,record,'expired','排队批准已过期，请重新核对。');continue
            if not row or row['state']!='APPROVED' or row['approved_plan_hash']!=record['plan_hash'] or row['approved_version']!=record['version']:
                cancel(conn,record);continue
            chosen=save(conn,record,'running');break
    if not chosen:return False
    tid=chosen['task_id']
    try:
        result=api._dispatch(tid,'批准后连续执行',lambda:api._run_execution(tid),'wait')
    except Exception as exc:
        message=api._public(getattr(exc,'detail',str(exc)))
        with db.transaction(conn):
            current=db.get_execution_intent(conn,chosen['request_id'])
            if current['status'] not in ACTIVE:return True
            if attempted(conn,chosen):
                needs_check(conn,current,message)
            elif db.get_context(conn,tid)['busy']:
                save(conn,current,'queued')
            else:
                cancel(conn,current,'expired' if expired(current) else 'cancelled',message)
    else:
        with db.transaction(conn):
            save(conn,chosen,'succeeded' if result['state']=='COMPLETED' else 'needs_reconciliation')
    return True


class ApprovalWorker:
    def __init__(self,api,interval=1):
        self.api,self.interval=api,interval
        self.stop_event=threading.Event()
        self.thread=threading.Thread(target=self.run,name='restock-approved-queue',daemon=True)

    def start(self):
        recover(self.api.get_conn())
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(self.interval):
            try:dispatch_one(self.api)
            except Exception:
                logging.getLogger(__name__).warning('Approved queue check failed; persisted intents retained.')

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():self.thread.join()
