"""M5-2 durable store-cycle scheduler. No approval authority and no receipt copy."""
import hashlib,logging,threading,uuid,os
from datetime import timedelta
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from app import db,collection,ledger,period_coverage
from app.models import Policy,TaskState,canonical_json,plan_hash
from app.rules import build_plan
from app.validation import snapshot_hash,merged_rows,validate_policy

TARGET_SYSTEM='restock-demo-procurement'


class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    source_id:str
    anchor:str
    period_seconds:int=Field(ge=60,le=31622400)
    policy:Policy
    relationship:Literal['same','separate','mapped']
    mode:Literal['observe','review']='observe'
    enabled:bool=True
    legacy_task_ids:list[str]=Field(default_factory=list,max_length=1000)
    legacy_tasks_reviewed:bool
    budget_basis:Literal['approval_cycle']='approval_cycle'

    @model_validator(mode='after')
    def checked(self):
        collection.moment(self.anchor)
        if not self.legacy_tasks_reviewed:raise ValueError('请明确确认旧任务的店仓归属')
        if len(self.legacy_task_ids)!=len(set(self.legacy_task_ids)):raise ValueError('旧任务列表重复')
        return self


class PolicyCorrection(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_revision:int=Field(ge=1)
    period_start:str
    policy:Policy

    @model_validator(mode='after')
    def complete(self):
        collection.moment(self.period_start)
        if self.policy.model_fields_set!=set(Policy.model_fields):raise ValueError('请完整提交所有策略字段')
        return self


def correct_current_policy(conn,root,gid,request):
    """Human correction only before a cycle has formal plans or commitments."""
    with db.transaction(conn):
        g=get_group(conn,gid);p=period(conn,g)
        if g['revision']!=request.expected_revision or p['start']!=request.period_start:
            raise ValueError('配置或周期已变化，请刷新后重新核对')
        if target_url()!=g['target_url']:raise ValueError('执行目标已变化，请先核实原单据')
        current=[tid for tid in db.replenishment_task_ids(conn,gid)
                 if db.replenishment_task(conn,tid)['period_start']==p['start']]
        if g['state'].get('claim') or any(db.get_context(conn,tid)['busy'] for tid in current):
            raise ValueError('当前检查正在进行，请等待结束后纠正策略')
        if any(db.list_plan_versions(conn,tid) or ledger.has_execution(conn,tid) for tid in current):
            raise ValueError('本周期已有正式方案或执行记录，不能通过纠正策略重置额度')
        sid=g['config']['source_id'];source=collection.get_source(conn,sid);did=source['state']['latest_dataset_id']
        collection.assert_ready(conn,root,sid,did)
        snapshot=db.get_dataset(conn,did)
        errors=validate_policy(request.policy,snapshot)
        if errors:raise ValueError('；'.join(errors))
        facts=period_coverage.calculate(snapshot,local_lines(conn,g),p['start'],p['end'],
            target_system_id=TARGET_SYSTEM,relationship=g['config']['relationship'],source_as_of=source['state']['source_as_of'])
        if facts['occupied_cents']:raise ValueError('本周期已有采购占用，不能重新设置额度')
        old=p['policy'];p['policy']=request.policy.model_dump();g['config']['policy']=p['policy']
        g['config']['mode']='observe';g['revision']+=1
        g['state'].update(last_input=None,attempt_key=None,failures=0,observed_dataset=None,
                          status='waiting',error=None,next_check_at=collection.stamp(collection.utcnow()))
        db.save_replenishment_period(conn,p);db.save_replenishment_group(conn,g)
        db.record_replenishment_check(conn,gid,{'at':collection.stamp(collection.utcnow()),'status':'policy_corrected',
            'source':'human_workbench','period_start':p['start'],'before':old,'after':p['policy'],'revision':g['revision']})
    return g


def target_url():
    from urllib.parse import urlsplit
    value=os.getenv('PROCUREMENT_PORTAL_URL','http://127.0.0.1:8200').rstrip('/')
    parsed=urlsplit(value)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('自动补货当前仅接入本机演示采购后台，真实目标须另行授权适配')
    return value


def digest(value):return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def get_group(conn,gid):
    value=db.replenishment_group(conn,gid)
    if value is None:raise ValueError('找不到补货周期配置')
    return value


def group_for_source(conn,sid):
    source=db.get_source(conn,sid)
    if not source:return None
    return next((g for g in db.replenishment_groups(conn) if all(g[k]==source['config'][k] for k in ('merchant_id','warehouse_id'))),None)


def period(conn,group,*,start=None,create=True):
    config=group['config'];anchor=collection.moment(config['anchor']);now=collection.utcnow()
    if start is None:
        if now<anchor:raise ValueError('尚未到配置的补货周期起点')
        begin=anchor+timedelta(seconds=int((now-anchor).total_seconds()//config['period_seconds'])*config['period_seconds'])
        start=collection.stamp(begin)
    value=db.replenishment_period(conn,group['group_id'],start)
    if value:return value
    value={'group_id':group['group_id'],'start':start,
           'end':collection.stamp(collection.moment(start)+timedelta(seconds=config['period_seconds'])),
           'policy':config['policy'],'task_id':None}
    if create:db.save_replenishment_period(conn,value)
    return value


def related_tasks(conn,group):
    sids=[s['source_id'] for s in db.list_sources(conn) if all(s['config'][k]==group[k] for k in ('merchant_id','warehouse_id'))]
    return sorted(set(db.source_task_ids(conn,sids))|set(db.replenishment_task_ids(conn,group['group_id'])))


def configure(conn,settings,gid=None):
    c=settings.model_dump();source=collection.get_source(conn,c['source_id'])
    with db.transaction(conn):
        if gid:
            g=get_group(conn,gid)
            for k in ('source_id','anchor','period_seconds','relationship','legacy_task_ids','budget_basis'):
                if g['config'][k]!=c[k]:raise ValueError('已有周期身份/来源关联不可重置；政策金额和规则更新只用于未建立的新周期')
            if c['mode']=='review' and g['config']['mode']!='review' and (not g['state'].get('observed_dataset') or g['state']['observed_dataset']!=source['state']['latest_dataset_id']):
                raise ValueError('请先在观察模式完成当前数据的检查')
            g['revision']+=1;g['config']=c
            g['state'].update(next_check_at=collection.stamp(collection.utcnow()),last_input=None)
            for tid in related_tasks(conn,g):
                row=db.get_task(conn,tid)
                if row and row['state']=='APPROVED' and not ledger.has_execution(conn,tid):
                    db.upsert_task(conn,tid,TaskState.NEEDS_REVIEW)
                    db.activity(conn,tid,'automation_changed',{'group_id':gid,'approval_invalidated':True})
        else:
            if group_for_source(conn,c['source_id']):raise ValueError('该店仓已有补货周期，不能另建配置重复领取额度')
            if c['mode']!='observe':raise ValueError('首次配置必须先进入观察模式')
            g={'group_id':'rp-'+uuid.uuid4().hex,'merchant_id':source['config']['merchant_id'],
               'warehouse_id':source['config']['warehouse_id'],'target_url':target_url(),'config':c,'revision':1,
               'state':{'status':'waiting','next_check_at':collection.stamp(collection.utcnow()),'task_id':None,
                        'claim':None,'last_input':None,'attempt_key':None,'failures':0,'observed_dataset':None}}
            for tid in c['legacy_task_ids']:
                if not db.get_task(conn,tid):raise ValueError('旧任务不存在')
                if db.replenishment_task(conn,tid):raise ValueError('旧任务已属于其他周期，不可重复关联')
                sid=db.task_source(conn,tid)
                if sid and any(db.get_source(conn,sid)['config'][k]!=g[k] for k in ('merchant_id','warehouse_id')):
                    raise ValueError('不能把其他店仓任务纳入本周期')
            pending=set(c['legacy_task_ids'])|set(related_tasks(conn,g))
            if any(ledger.has_execution(conn,t) and db.get_task(conn,t)['state']!='COMPLETED' for t in pending):
                raise ValueError('已有未完成采购，请先按原批准核对/恢复，再配置自动补货')
            db.save_replenishment_group(conn,g)
            p=period(conn,g)
            for tid in sorted(set(c['legacy_task_ids'])|set(related_tasks(conn,g))):
                db.bind_replenishment_task(conn,tid,g['group_id'],p['start'])
                if not ledger.has_execution(conn,tid) and not db.task_source(conn,tid):db.bind_task_source(conn,tid,c['source_id'])
                row=db.get_task(conn,tid)
                if row['state']=='APPROVED' and not ledger.has_execution(conn,tid):db.upsert_task(conn,tid,TaskState.NEEDS_REVIEW)
        db.save_replenishment_group(conn,g)
    return g


def bind_task(conn,tid,sid):
    if not sid:
        if db.replenishment_groups(conn):raise ValueError('已配置店仓周期，新任务必须选择明确的数据来源；旧手工任务不会被追溯绑定')
        return
    g=group_for_source(conn,sid)
    if g:
        if sid!=g['config']['source_id']:raise ValueError('此店仓已指定供数来源，请使用该正式来源版本')
        p=period(conn,g);db.bind_replenishment_task(conn,tid,g['group_id'],p['start'])


def local_lines(conn,group,current_task=None,exclude_version=None):
    lines=[]
    for tid in related_tasks(conn,group):
        row=db.get_task(conn,tid);has=ledger.has_execution(conn,tid)
        if tid!=current_task and (row['state']=='EXECUTING' or (has and row['state'] in ('PARTIAL','NEEDS_RECONCILIATION','FAILED'))):
            raise ValueError('本店仓有执行中或待核对的采购，请先处理原任务')
        events=db.list_execution_events(conn,tid)
        for fact in ledger.effective_orders(conn,tid).values():
            if tid==current_task and fact['version']==exclude_version:continue
            if fact['state'] in ('unknown','mismatch'):raise ValueError('本店仓存在未知或回读不一致的申请')
            if fact['state']!='created':continue
            receipt=fact['result']['receipt']
            intents=[e for e in events if e['event_type']=='submit_intent' and e['external_ref']==fact['external_ref']]
            if not intents:raise ValueError('旧申请缺少可追溯的提交时间，须核实归属周期')
            event=intents[0];at=event['payload'].get('submitted_at') or event['created_at'].replace(' ','T')+'+00:00'
            plan,_=db.get_plan(conn,tid,fact['version']);ctx=plan.automation_context or {}
            for line in receipt['lines']:
                lines.append({'system_id':TARGET_SYSTEM,'order_id':receipt['order_id'],'sku':line['sku'],
                    'supplier_id':receipt['supplier_id'],'quantity':line['order_qty'],
                    'amount_cents':line['order_qty']*line['unit_cost_cents'],'status':receipt['status'],
                    'created_at':at,'budget_at':ctx.get('period_start')})
    return lines


def context_for(conn,tid,snapshot,policy,*,recovering=False,exclude_version=None):
    link=db.replenishment_task(conn,tid)
    if not link:return None
    g=get_group(conn,link['group_id']);p=period(conn,g,start=link['period_start'])
    if target_url()!=g['target_url']:raise ValueError('执行目标已变化，不能把原单据视为新后台的采购记录')
    if snapshot is None:raise ValueError('旧方案缺少不可变快照，请先选择完整来源数据')
    if not recovering:
        if not g['config']['enabled']:raise ValueError('自动补货已暂停，不能发布或执行新方案')
        if period(conn,g)['start']!=p['start']:raise ValueError('此任务属于已结束的补货周期，请查看本周期任务')
    if policy.budget_cents!=p['policy']['budget_cents']:raise ValueError('任务预算必须等于该店仓周期额度，不能单独重新发放或改变')
    source=collection.get_source(conn,g['config']['source_id'])
    asof=source['state']['source_as_of']
    if recovering:
        publication=db.get_publication(conn,g['config']['source_id'],'ds-'+snapshot_hash(snapshot))
        if not publication:raise ValueError('原批准来源的发布证据缺失，须先核实后恢复')
        asof=publication['source_as_of']
    facts=period_coverage.calculate(snapshot,local_lines(conn,g,tid,exclude_version),p['start'],p['end'],
        target_system_id=TARGET_SYSTEM,relationship=g['config']['relationship'],source_as_of=asof)
    if facts['occupied_cents']>policy.budget_cents:raise ValueError('已核验采购占用超过本周期额度，须人工处理')
    return {'group_id':g['group_id'],'group_revision':g['revision'],'mode':g['config']['mode'],
            'period_start':p['start'],'period_end':p['end'],'budget_cents':p['policy']['budget_cents'],
            'budget_basis':'approval_cycle','coverage':facts}


def assert_plan(conn,tid,plan,*,approve=False,recovering=False):
    link=db.replenishment_task(conn,tid)
    if not link:
        if plan.automation_context is not None:raise ValueError('方案周期关联缺失')
        return
    if recovering and plan.automation_context is None:
        # Explicitly adopted approvals keep their original schema, but not a movable target.
        if target_url()!=get_group(conn,link['group_id'])['target_url']:
            raise ValueError('执行目标已变化，不能在其他后台恢复原批准')
        return
    expected=context_for(conn,tid,plan.dataset_snapshot,plan.policy,recovering=recovering,
                         exclude_version=plan.version if recovering else None)
    actual=plan.automation_context
    if recovering and actual:
        # Recovery retains the original period authorization. Runtime settings
        # cannot rewrite already submitted facts or authorize a second task.
        expected.update(group_revision=actual['group_revision'],mode=actual['mode'])
    if approve and expected['mode']!='review':raise ValueError('当前为观察模式，草案不能批准采购')
    if expected!=actual:raise ValueError('周期、额度或已创建申请已变化，请重新计算并复核当前版本')


def task_info(conn,tid,plan):
    link=db.replenishment_task(conn,tid)
    if not link:return None
    g=get_group(conn,link['group_id']);result={'group_id':g['group_id'],'mode':g['config']['mode'],'ready':False,'reason':None}
    if db.get_task(conn,tid)['state']=='COMPLETED':return {**result,'historical':True}
    try:
        if plan is None:raise ValueError('等待本周期方案生成')
        assert_plan(conn,tid,plan,approve=True)
        result['ready']=True
    except ValueError as exc:result['reason']=str(exc)
    return result


def info(conn,gid):
    g=get_group(conn,gid)
    g['checks']=db.replenishment_checks(conn,gid)
    try:g['period']=period(conn,g,create=False)
    except ValueError:g['period']=None
    return g


def recover(conn):
    with db.transaction(conn):
        for g in db.replenishment_groups(conn):
            if g['state'].get('claim'):
                g['state'].update(claim=None,status='interrupted',next_check_at=collection.stamp(collection.utcnow()))
                db.record_replenishment_check(conn,g['group_id'],{'status':'interrupted','at':collection.stamp(collection.utcnow())})
                db.save_replenishment_group(conn,g)


def refresh_related(api,gid,owned_task=None):
    """Read-only live receipt verification before trusting cross-task occupancy."""
    conn=api.get_conn();g=get_group(conn,gid)
    if target_url()!=g['target_url']:raise ValueError('执行目标已变化，请先核实原单据')
    for tid in related_tasks(conn,g):
        if not ledger.has_execution(conn,tid):continue
        row=db.get_task(conn,tid)
        if row['state']=='EXECUTING':raise ValueError('本店仓正在执行采购，请等待本次写入结束')
        owner='period-readback-'+uuid.uuid4().hex
        if tid!=owned_task and not db.claim_task(conn,tid,'核对周期已有申请',owner):
            raise ValueError('已有任务正在处理，稍后重试核对')
        try:
            for version in db.list_plan_versions(conn,tid):
                if not ledger.has_execution(conn,tid,version):continue
                plan,_=db.get_plan(conn,tid,version)
                api._observe_reconciliation(tid,plan,api._adapter().reconcile_orders(plan))
            if any(f['state'] in ('unknown','mismatch') for f in ledger.effective_orders(conn,tid).values()):
                current=db.get_task(conn,tid)
                if current['state'] in ('COMPLETED','APPROVED') and current['approved_plan_hash']:
                    db.upsert_task(conn,tid,TaskState.NEEDS_RECONCILIATION,current['approved_plan_hash'],current['approved_version'])
                raise ValueError('已有申请回读不一致或结果未知，已停止新采购')
        finally:
            if tid!=owned_task:db.release_task(conn,tid,owner)


def refresh_task(api,tid,owned=False):
    link=db.replenishment_task(api.get_conn(),tid)
    if link:refresh_related(api,link['group_id'],tid if owned else None)


def check_group(api,gid,force=False):
    conn=api.get_conn();job=None;work=None
    initial=get_group(conn,gid);pre_error=None
    if not initial['config']['enabled'] or initial['state'].get('claim'):return False
    if not force and collection.moment(initial['state']['next_check_at'])>collection.utcnow():return False
    try:refresh_related(api,gid)
    except Exception as exc:pre_error=api._public(getattr(exc,'detail',str(exc)))
    with db.transaction(conn):
        g=get_group(conn,gid);state=g['state'];now=collection.utcnow()
        if not g['config']['enabled'] or state.get('claim'):return False
        if not force and collection.moment(state['next_check_at'])>now:return False
        source=collection.get_source(conn,g['config']['source_id']);sid=source['source_id']
        interval=source['config']['schedule']['check_every_seconds']
        state.update(next_check_at=collection._next_due(state['next_check_at'],interval,now),checked_at=collection.stamp(now))
        try:
            if pre_error:raise ValueError(pre_error)
            did=source['state']['latest_dataset_id'];collection.assert_ready(conn,api.COLLECTION_DIR,sid,did)
            snap=db.get_dataset(conn,did);p=period(conn,g);tid=p.get('task_id')
            if not tid:
                candidates=[t for t in db.replenishment_task_ids(conn,gid) if db.replenishment_task(conn,t)['period_start']==p['start'] and not ledger.has_execution(conn,t)]
                if len(candidates)>1:raise ValueError('同周期存在多个人工待审任务，请先明确要继续的任务')
                if candidates:tid=candidates[0]
            old=db.get_task(conn,tid) if tid else None
            if old and db.get_context(conn,tid)['busy']:raise ValueError('当前任务正在处理，稍后合并检查')
            if old and ledger.has_execution(conn,tid):
                if old['state']!='COMPLETED':raise ValueError('本周期原任务待核对，不能另建任务绕过')
                tid=None;old=None
            current=None
            if tid:
                versions=db.list_plan_versions(conn,tid)
                if versions:current,_=db.get_plan(conn,tid,versions[-1])
            policy=current.policy if current else Policy.model_validate(p['policy'])
            errors=validate_policy(policy,snap)
            if errors:raise ValueError('；'.join(errors))
            facts=period_coverage.calculate(snap,local_lines(conn,g,tid),p['start'],p['end'],
                target_system_id=TARGET_SYSTEM,relationship=g['config']['relationship'],source_as_of=source['state']['source_as_of'])
            if facts['occupied_cents']>policy.budget_cents:raise ValueError('已创建申请超出周期预算')
            key=digest({'dataset_id':did,'period':p['start'],'revision':g['revision'],'policy':policy.model_dump(),'facts':facts})
            if key==state.get('last_input'):
                state.update(status=state.get('last_success_status') or ('observed' if g['config']['mode']=='observe' else 'needs_review'),
                             error=None,occupied_cents=facts['occupied_cents'])
                db.save_replenishment_group(conn,g);return False
            if key==state.get('attempt_key') and state.get('failures',0)>=3:
                raise ValueError('相同输入已失败三次，请检查原因后更新配置或数据再试')
            preview=build_plan('preview',1,snapshot_hash(snap),policy,merged_rows(snap),snap,
                               covered=facts['covered'],covered_amount=facts['occupied_cents'])
            if not any(i.order_qty for i in preview.items):
                state.update(status='no_demand' if not any(i.gap for i in preview.items) else 'no_purchase',
                             last_input=key,observed_dataset=did,error=None,occupied_cents=facts['occupied_cents'])
                state['last_success_status']=state['status']
                db.record_replenishment_check(conn,gid,{'at':collection.stamp(now),'status':state['status'],'dataset_id':did,'period_start':p['start']})
                db.save_replenishment_group(conn,g);return True
            if not tid:
                tid=uuid.uuid4().hex[:12];db.upsert_task(conn,tid,TaskState.NEW);db.bind_task_source(conn,tid,sid)
                db.bind_replenishment_task(conn,tid,gid,p['start'])
                db.activity(conn,tid,'created',{'source':'replenishment_scheduler','group_id':gid,'period_start':p['start']})
            p['task_id']=tid;db.save_replenishment_period(conn,p)
            req=api.AnalyzeRequest(dataset_id=did,source_id=sid,policy=policy)
            if not current:db.update_context(conn,tid,request_json=req.model_dump_json())
            token=uuid.uuid4().hex
            state.update(status='running',task_id=tid,claim=token,error=None,occupied_cents=facts['occupied_cents'],
                         failures=state.get('failures',0) if state.get('attempt_key')==key else 0,attempt_key=key)
            job=(tid,token,key,did,g['revision'])
            work=(lambda:api._revise_work(tid,api.ReviseRequest(dataset_id=did,policy=policy))) if current else (lambda:api._create_work(tid,req,snap))
        except (ValueError,collection.CollectionError) as exc:
            state.update(status='blocked',error=str(exc))
            db.record_replenishment_check(conn,gid,{'at':collection.stamp(now),'status':'blocked','reason':str(exc)})
        db.save_replenishment_group(conn,g)
    if not job:return False
    tid,token,key,did,revision=job;error=None
    try:
        def guarded():
            latest=get_group(conn,gid)
            if not latest['config']['enabled'] or latest['revision']!=revision:raise ValueError('配置已变化或暂停')
            collection.assert_ready(conn,api.COLLECTION_DIR,latest['config']['source_id'],did)
            return work()
        api._dispatch(tid,'自动检查补货',guarded,'wait',publishes_plan=True)
    except Exception as exc:error=api._public(getattr(exc,'detail',str(exc)))
    with db.transaction(conn):
        g=get_group(conn,gid);state=g['state']
        if state.get('claim')==token:
            state.update(claim=None,status='failed' if error else ('observed' if g['config']['mode']=='observe' else 'needs_review'),error=error)
            if error:state['failures']=state.get('failures',0)+1
            else:state.update(last_input=key,observed_dataset=did,failures=0,last_success_status=state['status'])
            db.record_replenishment_check(conn,gid,{'at':collection.stamp(collection.utcnow()),'status':state['status'],'task_id':tid,'dataset_id':did,'error':error})
            db.save_replenishment_group(conn,g)
    return True


class Worker:
    def __init__(self,api,interval=2):
        self.api,self.interval=api,interval;self.stop_event=threading.Event()
        self.thread=threading.Thread(target=self.run,name='restock-replenishment',daemon=True)
    def start(self):recover(self.api.get_conn());self.thread.start()
    def run(self):
        while not self.stop_event.wait(self.interval):
            for g in db.replenishment_groups(self.api.get_conn()):
                if self.stop_event.is_set():break
                try:check_group(self.api,g['group_id'])
                except Exception:logging.getLogger(__name__).warning('Replenishment check failed; durable state retained.')
    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():self.thread.join()
