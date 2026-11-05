# -*- coding: utf-8 -*-
"""FastAPI 入口：真实三阶段 Agent + 审批 gate + 固定浏览器执行适配器。"""

import os
import json
import secrets
import re
import threading
from contextlib import asynccontextmanager
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app import db, runner, jobs, datasets, reports, ledger, collection, approval_queue, replenishment
from app.execution import PlaywrightProcurementAdapter, build_supplier_orders, summarize_execution
from app.models import Policy, TaskState, canonical_json, DataContract, RevisionDecision
from app.prompts import build_stage_prompt
from app.state_machine import TaskRecord, TransitionError
from app.validation import load_snapshot, snapshot_hash, validate_policy
from app.business_tools import ToolSession, active_session, planner_review, revision_review
from app.agent_runtime import AgentRuntime

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = ROOT / os.getenv("RESTOCK_DB_PATH", "restock.db")
COLLECTION_DIR = ROOT / os.getenv("RESTOCK_COLLECTION_DIR", "outputs/collections")

@asynccontextmanager
async def lifespan(app):
    from app.demo_site import DemoSite
    app.state.demo_site=DemoSite()
    get_conn()
    publisher = collection.PublicationWorker(get_conn, lambda: COLLECTION_DIR)
    from app.collector import CollectorWorker
    collector_worker = CollectorWorker(get_conn, lambda: COLLECTION_DIR)
    collector_worker.start()
    publisher.start()
    import sys
    approved_worker = approval_queue.ApprovalWorker(sys.modules[__name__])
    approved_worker.start()
    replenishment_worker=replenishment.Worker(sys.modules[__name__])
    replenishment_worker.start()
    try:
        yield
    finally:
        try:
            replenishment_worker.stop()
            approved_worker.stop()
            collector_worker.stop()
            publisher.stop()
            jobs.shutdown()
        finally:
            app.state.demo_site.stop()


app = FastAPI(title="RestockPilot", version="0.7.0-m3", lifespan=lifespan)
_conn = None
_tasks: dict = {}
_conn_lock = threading.Lock()
APPROVAL_TOKEN = os.getenv("RESTOCK_APPROVAL_TOKEN") or secrets.token_urlsafe(32)
from app.collection_api import router as collection_router
app.include_router(collection_router(lambda: get_conn(), lambda: COLLECTION_DIR, lambda: APPROVAL_TOKEN))


def _source_ready(source_id, dataset_id):
    try:
        collection.assert_ready(get_conn(), COLLECTION_DIR, source_id, dataset_id)
    except collection.CollectionError as exc:
        raise HTTPException(exc.status, str(exc)) from None


def _task_source_ready(task_id, dataset_id):
    source_id = db.task_source(get_conn(), task_id)
    if source_id:
        _source_ready(source_id, dataset_id)




@app.get("/health")
def health():
    return {"status": "ok", "service": "restock-pilot", "version":"0.7.0-m3",
            "agent_mode":os.getenv("RESTOCK_AGENT_MODE","required")}


def _human(token):
    if not secrets.compare_digest(token,APPROVAL_TOKEN):
        raise HTTPException(403,'此操作只能从人工工作台提交。')


@app.get('/demo-site')
def demo_site_status():
    return app.state.demo_site.status()


@app.post('/demo-site/start')
def start_demo_site(x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    from start import StartupError
    try:return app.state.demo_site.start()
    except (StartupError,OSError,ValueError) as exc:
        raise HTTPException(409,'测试页面未能启动：'+str(exc)) from None


@app.get('/system/status')
def system_status():
    conn=get_conn()
    with db._LOCK:
        busy=conn.execute('SELECT COUNT(*) FROM task_context WHERE busy=1').fetchone()[0]
    collecting=sum(r['state'].get('collection_job',{}).get('status') in ('queued','running') for r in db.list_sources(conn))
    return {'busy_tasks':busy,'collecting_sources':collecting,'can_stop':bool(os.getenv('RESTOCK_CONTROL_URL'))}


@app.post('/system/stop')
def stop_system(x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    import httpx
    from urllib.parse import urlsplit
    url=os.getenv('RESTOCK_CONTROL_URL','');parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise HTTPException(409,'此服务未通过统一入口启动，请使用原启动入口停止。')
    try:
        response=httpx.post(url+'/stop',headers={'X-Restock-Control':os.getenv('RESTOCK_CONTROL_TOKEN','')},trust_env=False,timeout=3)
        response.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503,'无法连接启动窗口，请使用“关闭 RestockPilot”入口。')
    return {'message':'正在停止本次服务。已保存资料保留；中断的采集下次完整重读，采购须先核对再恢复。'}


@app.post('/replenishment-policies')
def configure_replenishment(request:replenishment.Settings,x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    try:return replenishment.configure(get_conn(),request)
    except ValueError as exc:raise HTTPException(409,str(exc))


@app.put('/replenishment-policies/{group_id}')
def update_replenishment(group_id:str,request:replenishment.Settings,x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    try:return replenishment.configure(get_conn(),request,group_id)
    except ValueError as exc:raise HTTPException(409,str(exc))


@app.get('/replenishment-policies')
def list_replenishment():
    return {'policies':[replenishment.info(get_conn(),g['group_id']) for g in db.replenishment_groups(get_conn())]}


@app.get('/replenishment-policies/{group_id}')
def get_replenishment(group_id:str):
    try:return replenishment.info(get_conn(),group_id)
    except ValueError as exc:raise HTTPException(404,str(exc))


@app.post('/replenishment-policies/{group_id}/correct-current-policy')
def correct_replenishment_policy(group_id:str,request:replenishment.PolicyCorrection,x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    try:return replenishment.correct_current_policy(get_conn(),COLLECTION_DIR,group_id,request)
    except (ValueError,collection.CollectionError) as exc:raise HTTPException(409,str(exc))


@app.post('/replenishment-policies/{group_id}/check')
def check_replenishment(group_id:str,x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    with db.transaction(get_conn()):
        try:g=replenishment.get_group(get_conn(),group_id)
        except ValueError as exc:raise HTTPException(404,str(exc))
        g['state']['next_check_at']=collection.stamp(collection.utcnow())
        db.save_replenishment_group(get_conn(),g)
    return {'accepted':True,'group_id':group_id}


class AgentStageError(RuntimeError):
    pass


def get_conn():
    global _conn
    with _conn_lock:
        if _conn is None:
            _conn = db.init_db(DB_PATH)
            db.recover_interrupted(_conn)
    return _conn


def get_record(task_id: str) -> TaskRecord:
    row = db.get_task(get_conn(), task_id)
    if row is None:
        raise HTTPException(404, "找不到此任务。")
    rec = TaskRecord(task_id)
    rec.state = TaskState(row["state"])
    rec.approved_plan_hash = row["approved_plan_hash"]
    rec.approved_version = row["approved_version"]
    return rec


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Policy
    data_contract: DataContract | None = None
    dataset_id: str | None = None
    user_request: str = Field(default="", max_length=4000)
    source_id: str | None = None


class ReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field(default="", max_length=4000)
    policy: Policy | None = None
    dataset_id: str | None = None


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(gt=0)
    plan_hash: str = Field(min_length=64, max_length=64)


class ApproveAndExecuteRequest(ApprovalRequest):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,100}$')
    expires_in_seconds: int = Field(ge=1, le=86400)


@app.post('/tasks/{task_id}/approve-and-execute', status_code=202)
def approve_and_execute(task_id:str,req:ApproveAndExecuteRequest,x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    existing=db.get_execution_intent(get_conn(),req.request_id)
    if existing:
        if any(existing[k]!=v for k,v in {'task_id':task_id,'version':req.version,'plan_hash':req.plan_hash,
                                          'expires_in_seconds':req.expires_in_seconds}.items()):
            raise HTTPException(409,'同一批准请求标识不能对应不同任务、版本或有效期。')
        return {'task_id':task_id,'request_id':req.request_id,'accepted':True}
    import sys
    try:replenishment.refresh_task(sys.modules[__name__],task_id)
    except ValueError as exc:raise HTTPException(409,str(exc))
    from datetime import timedelta
    conn=get_conn()
    with db.transaction(conn):
        existing=db.get_execution_intent(conn,req.request_id)
        if existing:
            if any(existing[k]!=v for k,v in {'task_id':task_id,'version':req.version,
                    'plan_hash':req.plan_hash,'expires_in_seconds':req.expires_in_seconds}.items()):
                raise HTTPException(409,'同一批准请求标识不能对应不同任务、版本或有效期。')
            return {'task_id':task_id,'request_id':req.request_id,'accepted':True}
        if db.get_context(conn,task_id)['busy']:
            raise HTTPException(409,'任务正在处理，请等待完成后批准。')
        rec=get_record(task_id)
        plan,h=db.get_plan(conn,task_id,_latest_version(task_id))
        if plan is None or req.version!=plan.version or req.plan_hash!=h:
            raise HTTPException(409,'页面方案已过期，请刷新后重新确认。')
        if approval_queue.active(conn,task_id):
            raise HTTPException(409,'该任务已有待执行批准，请查看其进度。')
        _task_source_ready(task_id,plan.dataset_id)
        try:replenishment.assert_plan(conn,task_id,plan,approve=True)
        except ValueError as exc:raise HTTPException(409,str(exc))
        try:rec.approve(h,plan.version)
        except TransitionError as exc:raise HTTPException(409,str(exc))
        now=collection.utcnow()
        record={**req.model_dump(),'task_id':task_id,'dataset_id':plan.dataset_id,
                'created_at':collection.stamp(now),'updated_at':collection.stamp(now),
                'expires_at':collection.stamp(now+timedelta(seconds=req.expires_in_seconds)),
                'status':'queued','error':None}
        db.upsert_task(conn,task_id,rec.state,h,plan.version)
        db.save_execution_intent(conn,record)
        db.activity(conn,task_id,'approved',{'version':plan.version,'plan_hash':h,
                    'source':'human_workbench','request_id':req.request_id,'expires_at':record['expires_at']})
        db.update_context(conn,task_id,phase='已批准，排队连续执行',error=None)
    return {'task_id':task_id,'request_id':req.request_id,'accepted':True}


@app.get('/tasks/{task_id}/execution-intents')
def execution_intents(task_id:str):
    get_record(task_id)
    return {'intents':db.execution_intents(get_conn(),task_id)}


@app.post('/tasks/{task_id}/revoke-approval')
def revoke_approval(task_id:str,x_restock_approval:str=Header(default='')):
    _human(x_restock_approval);conn=get_conn()
    with db.transaction(conn):
        rec=get_record(task_id)
        if db.get_context(conn,task_id)['busy'] or rec.state!=TaskState.APPROVED:
            raise HTTPException(409,'只可撤销尚未开始执行且空闲的批准；已经提交的申请须按实际结果核对。')
        if ledger.has_execution(conn,task_id,rec.approved_version):
            raise HTTPException(409,'此版本已开始执行，请先核对申请。')
        intent=approval_queue.active(conn,task_id)
        if intent:approval_queue.cancel(conn,intent,reason='用户撤销尚未执行的批准。')
        else:db.upsert_task(conn,task_id,TaskState.NEEDS_REVIEW)
        db.activity(conn,task_id,'approval_revoked',{'version':rec.approved_version})
    return {'task_id':task_id,'state':'NEEDS_REVIEW'}


def _run_agent_stage(stage: str, context: str, user_input: str) -> dict:
    """真实调用 runner；off 只用于显式离线测试，绝不伪装成 Agent 成功。"""
    if os.getenv("RESTOCK_AGENT_MODE", "required").lower() == "off":
        return {"status": "disabled", "summary": "", "tool_calls": [], "duration_s": 0}
    prompt = build_stage_prompt(stage, context, user_input)
    try:
        # Resolve configured relative CLI paths at the same project root used by
        # startup, before entering a private per-run working directory.
        executable = runner.resolve_codex_bin(str(ROOT))
    except OSError as exc:
        raise AgentStageError(str(exc)) from exc
    try:
        with AgentRuntime(active_session()) as runtime:
            result = runner.run_codex_task(
                prompt=prompt, workdir=str(runtime.workdir), codex_home=str(runtime.home),
                executable=executable, child_env=runtime.env,
                timeout_s=int(os.getenv("RESTOCK_AGENT_TIMEOUT_S", "600")))
    except (OSError, ValueError) as exc:
        raise AgentStageError(f'Agent runtime failed: {exc}') from exc
    session = active_session()
    if session is not None:
        # Retain the model response before business validation can reject it.
        # This is diagnostic evidence, never a published plan or a success event.
        from app.redaction import audit_call
        db.activity(session.conn, session.task_id, 'agent_response', {
            'stage':stage,'operation_id':session.operation_id,'base_version':session.base_version,
            'dataset_id':session.dataset_id,'duration_s':result.duration_s,
            'exit_code':result.exit_code,'timed_out':result.timed_out,
            **audit_call({'input':result.final_text},dict(os.environ),limit=65536)})
    if result.timed_out or result.exit_code != 0 or not result.final_text.strip():
        detail = result.stderr.strip()[-800:] or "empty agent response"
        raise AgentStageError(
            f"{stage} agent failed (exit={result.exit_code}, timeout={result.timed_out}): {detail}"
        )
    return {
        "status": "ok",
        "summary": result.final_text,
        "tool_calls": result.tool_calls,
        "duration_s": result.duration_s,
    }


def _policy_from_agent(text: str) -> Policy:
    """Reviser 唯一可影响的通道：完整 Policy JSON，其他内容一律拒绝。"""
    try:
        decision = RevisionDecision.model_validate_json(text.strip())
        if decision.unsupported_requests:
            raise ValueError("unsupported requests: " + "; ".join(decision.unsupported_requests))
        if decision.policy is None:
            raise ValueError("missing policy")
        if decision.policy.model_fields_set != set(Policy.model_fields):
            raise ValueError("policy must explicitly contain all five supported fields")
        return decision.policy
    except ValueError as exc:
        raise AgentStageError(f"invalid Reviser policy output: {exc}") from exc


def _check_summary_currency(agent):
    import unicodedata
    summary = unicodedata.normalize('NFKC', agent.get('summary', ''))
    try:
        decision = json.loads(summary)
    except (ValueError, TypeError):
        decision = None
    if isinstance(decision, dict) and 'explanation' in decision:
        # Policy values (e.g. an SKU named USD-001) are not prose currency claims.
        summary = str(decision['explanation'])
    # This stage has CNY only. Permit an explicit negation of the two common
    # mistranslations, but no foreign currency amount in either prefix/suffix form.
    summary = re.sub(r'(?:不是|非|而非)(?:美元|美分)(?:或(?:美元|美分))?', '', summary)
    if re.search(r'美元|美分|美金|(?<![A-Za-z_])(?:USD|dollars?|cents?)(?![A-Za-z_])|\$', summary, re.IGNORECASE):
        raise AgentStageError('模型摘要金额口径错误：本任务使用人民币分，不能解释为美分或美元。')


def _revision_error(rec: TaskRecord, message: str, status_code: int) -> HTTPException:
    # 修订失败不伪造 v+1；保留上一版供复核，但旧批准已按状态机失效。
    rec.transition(TaskState.NEEDS_REVIEW)
    db.upsert_task(get_conn(), rec.task_id, rec.state)
    return HTTPException(status_code, message)


def _snapshot(identifier):
    try:
        snap = db.get_dataset(get_conn(), identifier)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if snap is None:
        raise HTTPException(404, "找不到数据版本，请重新上传。")
    return snap


def _public(value):
    from start import redact
    return redact(str(value), dict(os.environ))


def _sanitized(value):
    if isinstance(value, str):
        return _public(value)
    if isinstance(value, list):
        return [_sanitized(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitized(v) for k, v in value.items()}
    return value


def _phase(task_id, label):
    db.update_context(get_conn(),task_id,phase=label)


def _agent_event(task_id, stage, version, agent):
    db.activity(get_conn(),task_id,stage,{
        "version":version,"status":agent.get("status"),"summary":_public(agent.get("summary", "")),
        "duration_s":agent.get("duration_s"),"error":_public(agent.get("error", "")),
        "model_advice":_public(agent.get('model_advice','')),
        "model_original":_public(agent.get('raw_summary','')),
        "tool_calls":_sanitized(agent.get('tool_calls', []))})


def _save_operation_result(task_id, result):
    db.update_context(get_conn(), task_id, result_json=json.dumps(_sanitized(result), ensure_ascii=False),
                      error=None, phase="本次操作完成")
    return result


def _publish_candidate(session, policy, stage, agent):
    # No browser/model work inside this transaction. Nested helpers use savepoints.
    with db.transaction(get_conn()):
        _task_source_ready(session.task_id, session.dataset_id)
        plan, h = session.commit(policy)
        if stage=='reviser':
            if agent.get('status')=='ok':agent['raw_summary']=agent.get('summary','')
            agent['summary']=json.dumps({'policy':plan.policy.model_dump(),'explanation':revision_review(session.current,plan),
                                         'unsupported_requests':[]},ensure_ascii=False)
        _agent_event(session.task_id, stage, plan.version, agent)
        result = {"task_id": session.task_id, "state": TaskState.NEEDS_REVIEW.value,
                  "plan": plan.model_dump(), "plan_hash": h, "agent": agent}
        _save_operation_result(session.task_id, result)
        return result


def _dispatch(task_id, phase, work, prefer, publishes_plan=False):
    conn = get_conn()
    job_id = uuid.uuid4().hex
    if not db.claim_task(conn, task_id, phase, job_id):
        raise HTTPException(409, "任务正在处理，请等待当前步骤完成。")
    before = db.get_task(conn,task_id)
    before_versions = db.list_plan_versions(conn,task_id)
    def run():
        try:
            result = work()
            if not publishes_plan:
                _save_operation_result(task_id, result)
            return result
        except Exception as exc:
            message = _public(exc.detail if isinstance(exc, HTTPException) else str(exc))
            with db.transaction(conn):
                db.update_context(conn, task_id, error=message, phase="需要处理")
                row = db.get_task(conn, task_id)
                restore_execution = (publishes_plan and before['state'] in ('PARTIAL','NEEDS_RECONCILIATION')
                    and db.list_plan_versions(conn,task_id) == before_versions)
                if restore_execution:
                    unresolved = any(r['state'] in ('unknown','mismatch') for r in ledger.effective_orders(conn,task_id).values())
                    state = TaskState.NEEDS_RECONCILIATION if unresolved else TaskState(before['state'])
                    db.upsert_task(conn,task_id,state,before['approved_plan_hash'],before['approved_version'])
                elif row and row['state'] in ('ANALYZING', 'EXECUTING'):
                    state = TaskState.NEEDS_RECONCILIATION if row['state'] == 'EXECUTING' else (
                        TaskState.NEEDS_REVIEW if db.list_plan_versions(conn, task_id) else TaskState.FAILED)
                    db.upsert_task(conn, task_id, state,
                        row['approved_plan_hash'] if state == TaskState.NEEDS_RECONCILIATION else None,
                        row['approved_version'] if state == TaskState.NEEDS_RECONCILIATION else None)
                db.activity(conn, task_id, "error", {"message": message, "job_id": job_id})
            if prefer == "wait":
                raise HTTPException(exc.status_code if isinstance(exc, HTTPException) else 500, message)
        finally:
            db.release_task(conn, task_id, job_id)
    if prefer == "wait":
        return run()
    try:
        jobs.submit(run)
    except Exception:
        db.release_task(conn, task_id, job_id)
        raise HTTPException(503, "无法启动任务，请重试。")
    return JSONResponse(status_code=202, content={"task_id": task_id, "accepted": True})


@app.post("/datasets")
async def upload_dataset(sales:UploadFile=File(...),inventory:UploadFile=File(...),
                         suppliers:UploadFile=File(...),contract:str=Form(...)):
    try:
        data_contract=DataContract.model_validate_json(contract)
    except ValueError:
        raise HTTPException(422,"数据口径格式有误，请检查确认项。")
    files={}
    for name,upload in (("sales",sales),("inventory",inventory),("suppliers",suppliers)):
        files[name]=await upload.read(datasets.MAX_FILE_BYTES+1)
        await upload.close()
    snap,problems=datasets.ingest(files,data_contract)
    if problems:
        return {"state":"NEEDS_INPUT","data_problems":problems,"dataset_id":None}
    db.save_dataset(get_conn(),snap)
    return {"state":"READY",**datasets.summary(snap)}


@app.post("/datasets/example")
def example_dataset():
    snap,problems=load_snapshot(DATA_DIR)
    if problems:
        raise HTTPException(422,problems)
    db.save_dataset(get_conn(),snap)
    return {"state":"READY",**datasets.summary(snap)}


@app.get('/dataset-templates')
def dataset_templates():
    import io,zipfile
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as bundle:
        for name in ('sales','inventory','suppliers'):
            bundle.writestr(name+'.csv',(ROOT/'data'/(name+'.csv')).read_bytes())
        bundle.writestr('填写说明.txt', '''三份 CSV 是可用 Excel 打开的表格。这里有 30 种演示商品，请用本店资料替换示例行，保留英文列名。
1. sales.csv 销售记录：date 日期（年-月-日）、sku 商品编号、sold_qty 当天净销量。每种商品需连续 28 天。
2. inventory.csv 库存资料：sku 商品编号、name 名称、category 类别、available_qty 可用库存、inbound_qty 已订未到数量、inbound_eta 剩余到货天数。
3. suppliers.csv 供应商资料：sku 商品编号、supplier_id 供应商编号、unit_cost_cents 含税含运采购价（分）、lead_days 交货天数、pack_size 每包商品数、min_order_qty 最低采购数。
三份表格中的商品编号必须一一对应，每个商品一个默认供应商。
例如一箱 12 瓶，所有数量按瓶填写：2 箱填 24，pack_size 填 12。
仓库 100 瓶已有订单预留 20 瓶，可用库存填 80。采购价 12.50 元填 1250；交货/到货还需 3 天填 3。
销量统计截止日期留空时使用文件最新日期，再往前统计 28 天。没有记录可能是漏记，不能直接当作没卖出；只有确认缺日等于零销量时才勾选相应选项。
页面确认框不会自动换算、扣库存或补价格。网页自动采集已经负责整理资料，不需要下载再上传这些样例。
这些数据和采购后台均为演示用途，不代表真实门店。''')
    return Response(output.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="restock-templates.zip"'})


@app.get("/datasets/{dataset_id}")
def dataset_info(dataset_id:str):
    return datasets.summary(_snapshot(dataset_id))


@app.get("/tasks")
def list_tasks():
    return {"tasks":db.list_tasks(get_conn())}


@app.post("/tasks")
def create_task(req:AnalyzeRequest,prefer:str=Header(default="")):
    if req.dataset_id and req.data_contract is not None:
        raise HTTPException(422,"数据版本已绑定口径，不能在任务中覆盖。")
    snap=_snapshot(req.dataset_id) if req.dataset_id else None
    task_id=uuid.uuid4().hex[:12]
    with db.transaction(get_conn()):
        if req.source_id:
            _source_ready(req.source_id, req.dataset_id)
        elif req.dataset_id and db.sources_for_dataset(get_conn(), req.dataset_id):
            raise HTTPException(422, "来源发布的数据必须明确绑定 source_id。")
        db.upsert_task(get_conn(),task_id,TaskState.NEW)
        try:replenishment.bind_task(get_conn(),task_id,req.source_id)
        except ValueError as exc:raise HTTPException(422,str(exc))
        if req.source_id:
            db.bind_task_source(get_conn(), task_id, req.source_id)
        db.update_context(get_conn(),task_id,request_json=req.model_dump_json())
        db.activity(get_conn(),task_id,"created",{"request":req.model_dump()})
    return _dispatch(task_id,"检查资料",lambda:_create_work(task_id,req,snap),prefer,publishes_plan=True)


def _create_work(task_id,req,snapshot):
    rec=get_record(task_id)
    if rec.state==TaskState.FAILED:rec.retry_generation()
    else:rec.transition(TaskState.ANALYZING)
    db.upsert_task(get_conn(),task_id,rec.state)
    problems=[]
    if snapshot is None:
        snapshot,problems=load_snapshot(DATA_DIR,req.data_contract)
    if problems:
        rec.transition(TaskState.NEEDS_INPUT)
        db.upsert_task(get_conn(),task_id,rec.state)
        return _save_operation_result(task_id,{"task_id":task_id,"state":rec.state.value,"data_problems":problems})
    policy=req.policy
    if req.user_request.strip():
        _phase(task_id,"理解补货要求")
        agent=_run_agent_stage("reviser",canonical_json({"current_policy":policy.model_dump()}),req.user_request)
        if agent.get('status')=='disabled':
            raise HTTPException(422,"离线模式不能理解自然语言，请使用参数表单。")
        _agent_event(task_id,"requirements",1,agent)
        try:
            policy=_policy_from_agent(agent['summary'])
        except AgentStageError as exc:
            raise HTTPException(422,str(exc))
    problems=validate_policy(policy,snapshot)
    if problems:
        rec.transition(TaskState.NEEDS_INPUT)
        db.upsert_task(get_conn(),task_id,rec.state)
        return _save_operation_result(task_id,{"task_id":task_id,"state":rec.state.value,"data_problems":problems})
    plan=None
    # 统一数据源：将当前 snapshot 保存为 dataset，Planner 通过 MCP 以同一 dataset_id 生成方案。
    dataset_id=db.save_dataset(get_conn(),snapshot)
    if req.dataset_id is None and not db.task_source(get_conn(),task_id) and db.sources_for_dataset(get_conn(),dataset_id):
        raise HTTPException(422,'数据来自已发布来源，请选择保留完整采购上下文的正式版本并明确绑定 source_id。')
    db.update_context(get_conn(),task_id,request_json=req.model_copy(update={"dataset_id":dataset_id,"policy":policy,"data_contract":None}).model_dump_json())
    _phase(task_id,"生成方案与复核摘要")
    planner_input=(
        f"为任务 {task_id} 生成补货方案："
        f"1) 用 inspect_dataset(task_id=\"{task_id}\") 检查数据集；"
        f"2) 用 build_plan(task_id=\"{task_id}\", dataset_id=\"{dataset_id}\", policy={policy.model_dump_json()}) 生成方案；"
        f"3) 根据工具返回的方案给出简短非数值复核建议。不要复述任何金额、数量统计或商品编号。"
    )
    if db.replenishment_task(get_conn(),task_id):
        planner_input+='本任务采用周期预算：最后只输出 {"advice_codes":[建议代码]}；代码从 build_plan 返回的 budget_cycle.allowed_advice 中选择，不写自由建议正文，不提出预算结转。'
    agent=None
    try:
        with ToolSession(get_conn(), task_id, 'planner', snapshot, fixed_policy=policy).activate() as session:
            agent=_run_agent_stage("planner",canonical_json({"task_id":task_id,"dataset_id":dataset_id,"policy":policy.model_dump()}),planner_input)
            _check_summary_currency(agent)
            if agent.get('status') == 'disabled':
                session.call('inspect_dataset', {'task_id':task_id})
                session.call('build_plan', {'task_id':task_id,'dataset_id':dataset_id,'policy':policy.model_dump()})
            elif session.draft is not None:
                agent['model_advice'] = agent.get('summary','')
                agent['summary'] = planner_review(session.draft, agent['model_advice'])
            return _publish_candidate(session,policy,"planner",agent)
    except (AgentStageError, ValueError) as exc:
        if agent and agent.get('status')=='ok':
            _agent_event(task_id,'planner_rejected',1,{'status':'error','error':str(exc),
                'raw_summary':agent.get('model_advice',agent.get('summary','')),'duration_s':agent.get('duration_s')})
        rec.transition(TaskState.FAILED)
        db.upsert_task(get_conn(),task_id,rec.state)
        raise HTTPException(503,str(exc))


@app.post('/tasks/{task_id}/retry')
def retry_generation(task_id:str,req:AnalyzeRequest,prefer:str=Header(default=''),x_restock_approval:str=Header(default='')):
    _human(x_restock_approval)
    def checked():
        rec=get_record(task_id);conn=get_conn()
        if (rec.state!=TaskState.FAILED or db.list_plan_versions(conn,task_id) or ledger.has_execution(conn,task_id)
                or rec.approved_plan_hash or rec.approved_version):
            raise HTTPException(409,'只有尚未生成方案、从未采购的失败任务可以重新生成。')
        sid=db.task_source(conn,task_id)
        if req.source_id!=sid:
            raise HTTPException(409,'重试不能替换来源绑定，请按原来源选择数据。')
        if sid:_source_ready(sid,req.dataset_id)
        elif db.sources_for_dataset(conn,req.dataset_id):
            raise HTTPException(409,'来源发布的数据须新建来源绑定任务，不能绕过来源检查。')
    if not req.dataset_id or req.data_contract is not None:
        raise HTTPException(422,'请先选择完整有效的数据版本。')
    checked();snapshot=_snapshot(req.dataset_id)
    def work():
        checked()
        db.update_context(get_conn(),task_id,request_json=req.model_dump_json())
        return _create_work(task_id,req,snapshot)
    return _dispatch(task_id,'重新生成方案',work,prefer,publishes_plan=True)


@app.post("/tasks/{task_id}/approve")
def approve(task_id:str,req:ApprovalRequest,x_restock_approval:str=Header(default="")):
    if not secrets.compare_digest(x_restock_approval,APPROVAL_TOKEN):
        raise HTTPException(403,"批准只能从已配置的人工工作台提交。")
    import sys
    try:replenishment.refresh_task(sys.modules[__name__],task_id)
    except ValueError as exc:raise HTTPException(409,str(exc))
    rec=get_record(task_id)
    if not db.claim_task(get_conn(),task_id,"确认方案"):
        raise HTTPException(409,"任务正在处理，请稍后确认。")
    try:
        rec=get_record(task_id)
        plan,h=db.get_plan(get_conn(),task_id,_latest_version(task_id))
        if plan is None:
            raise HTTPException(404,"no plan to approve")
        if req.version!=plan.version or req.plan_hash!=h:
            raise HTTPException(409,"页面方案已过期，请刷新后重新确认。")
        _task_source_ready(task_id, plan.dataset_id)
        try:
            rec.approve(h,plan.version)
        except TransitionError as exc:
            raise HTTPException(409,str(exc))
        with db.transaction(get_conn()):
            _task_source_ready(task_id, plan.dataset_id)
            try:replenishment.assert_plan(get_conn(),task_id,plan,approve=True)
            except ValueError as exc:raise HTTPException(409,str(exc))
            db.upsert_task(get_conn(),task_id,rec.state,h,plan.version)
            db.activity(get_conn(),task_id,"approved",{"version":plan.version,"plan_hash":h,"source":"human_workbench"})
        return {"task_id":task_id,"state":rec.state.value,"approved_plan_hash":h,"approved_version":plan.version}
    finally:
        db.update_context(get_conn(),task_id,busy=0,phase="等待执行" if rec.state==TaskState.APPROVED else "等待复核")


@app.post("/tasks/{task_id}/revise")
def revise(task_id:str,req:ReviseRequest,prefer:str=Header(default="")):
    rec=get_record(task_id)
    # M3：PARTIAL / NEEDS_RECONCILIATION 允许人工重新决策并修订（新版本扣除已覆盖需求）。
    if rec.state not in (TaskState.NEEDS_REVIEW,TaskState.APPROVED,TaskState.NEEDS_INPUT,
                         TaskState.PARTIAL,TaskState.NEEDS_RECONCILIATION):
        raise HTTPException(409,"当前状态不能修改方案。")
    if req.comment.strip() and req.policy is not None:
        raise HTTPException(422,"请选择自然语言或参数修改中的一种。")
    if not req.comment.strip() and req.policy is None and not req.dataset_id:
        raise HTTPException(422,"请填写修改意见、参数或选择新数据。")
    if req.dataset_id:
        _snapshot(req.dataset_id)
        if not db.task_source(get_conn(), task_id) and db.sources_for_dataset(get_conn(), req.dataset_id):
            raise HTTPException(409, "请新建明确绑定来源的任务，不能隐式更改已有手动任务的来源。")
    return _dispatch(task_id,"重新计算方案",lambda:_revise_work(task_id,req),prefer,publishes_plan=True)


def _revise_work(task_id, req):
    rec = get_record(task_id)
    allowed = (TaskState.NEEDS_REVIEW, TaskState.APPROVED, TaskState.NEEDS_INPUT,
               TaskState.PARTIAL, TaskState.NEEDS_RECONCILIATION)
    if rec.state not in allowed:
        raise HTTPException(409, "当前状态不能修改方案。")
    current, _ = db.get_plan(get_conn(), task_id, _latest_version(task_id))
    snapshot = _snapshot(req.dataset_id) if req.dataset_id else (current.dataset_snapshot if current else None)
    if snapshot is None:
        if rec.state in (TaskState.NEEDS_REVIEW,TaskState.APPROVED):
            rec.transition(TaskState.ANALYZING)
            db.upsert_task(get_conn(),task_id,rec.state)
            raise _revision_error(rec,"legacy plan has no data snapshot; create a new task with confirmed data",409)
        raise HTTPException(409, "legacy plan has no data snapshot; create a new task with confirmed data")
    if not req.dataset_id and snapshot_hash(snapshot) != current.dataset_hash:
        raise HTTPException(409, "dataset snapshot hash mismatch")
    _task_source_ready(task_id, "ds-" + snapshot_hash(snapshot))
    executed = ledger.has_execution(get_conn(), task_id)
    if executed and current and snapshot_hash(snapshot) != current.dataset_hash:
        raise HTTPException(409, "已有执行尝试的任务不能更换数据快照，请先核对原任务。")
    if rec.state in (TaskState.PARTIAL, TaskState.NEEDS_RECONCILIATION):
        approved, _ = _approved_plan(rec)
        _observe_reconciliation(task_id, approved, _adapter().reconcile_orders(approved))
    if executed or rec.state in (TaskState.PARTIAL, TaskState.NEEDS_RECONCILIATION):
        try:
            ledger.coverage(get_conn(), task_id, snapshot_hash(snapshot))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if rec.state in (TaskState.PARTIAL, TaskState.NEEDS_RECONCILIATION):
        rec.transition(TaskState.NEEDS_REVIEW)
    rec.transition(TaskState.ANALYZING)
    db.upsert_task(get_conn(),task_id,rec.state)
    db.activity(get_conn(),task_id,"revision_requested",{"version":current.version+1 if current else 1,**req.model_dump()})
    original_req=db.get_context(get_conn(),task_id).get('request') or {}
    policy=req.policy or (current.policy if current else Policy.model_validate(original_req.get('policy',{})))
    problems=validate_policy(policy,snapshot)
    if problems:
        raise _revision_error(rec,"; ".join(problems),422)
    agent={"status":"not_requested","summary":"通过参数表单修改","duration_s":0}
    dataset_id=db.save_dataset(get_conn(),snapshot)
    try:
        with ToolSession(get_conn(), task_id, 'reviser', snapshot, current,
                         fixed_policy=None if req.comment.strip() else policy).activate() as session:
            if req.comment.strip():
                _phase(task_id,"理解修改意见并计算候选方案")
                reviser_input=(f"为任务 {task_id} 修订方案，数据版本 {dataset_id}。"
                    f"先理解修改意见，支持时调用 revise_plan 计算候选；最后返回完整决策 JSON。修改意见：{req.comment}")
                agent=_run_agent_stage("reviser",canonical_json({'task_id':task_id,'dataset_id':dataset_id,
                    'current_policy':policy.model_dump(),'current_version':current.version if current else 0}),reviser_input)
                if agent.get('status')=='disabled':
                    raise AgentStageError('离线模式不能理解自然语言，请使用参数表单。')
                # Validate before any authoritative plan write. Malformed output,
                # unsupported constraints and exceptions discard the candidate.
                policy=_policy_from_agent(agent.get('summary', ''))
                _check_summary_currency(agent)
            else:
                session.call('revise_plan', {'task_id':task_id,'dataset_id':dataset_id,'policy':policy.model_dump()})
            return _publish_candidate(session,policy,"reviser",agent)
    except (AgentStageError, ValueError) as exc:
        raise _revision_error(rec,str(exc),422)


def _adapter():
    return PlaywrightProcurementAdapter(
        base_url=os.getenv("PROCUREMENT_PORTAL_URL", "http://127.0.0.1:8200"),
        headless=os.getenv("PROCUREMENT_HEADLESS", "true").lower() != "false",
        timeout_ms=int(os.getenv("PROCUREMENT_TIMEOUT_MS", "15000")))


def _approved_plan(rec):
    if rec.approved_version is None or rec.approved_plan_hash is None:
        raise HTTPException(409, "缺少有效的人工批准，不能核对或恢复采购。")
    plan, h = db.get_plan(get_conn(), rec.task_id, rec.approved_version)
    if plan is None or plan.version != rec.approved_version or h != rec.approved_plan_hash:
        raise HTTPException(409, "plan hash mismatch: approved version/content changed")
    return plan, h


def _observe_reconciliation(task_id, plan, observations, metadata=None):
    from app.models import plan_hash
    results = ledger.reconciliation_results(get_conn(), task_id, plan, observations)
    check_meta = {"version": plan.version, "plan_hash": plan_hash(plan), "check_id": uuid.uuid4().hex}
    with db.transaction(get_conn()):
        for result in results:
            ledger.record_observation(get_conn(),task_id,plan,result,
                "order_checked" if metadata else "reconciliation_observed", metadata or check_meta)
        db.activity(get_conn(), task_id, "reconciled", {"version": plan.version, "results": results})
    return results


def _begin_attempt(task_id, recovering):
    with db.transaction(get_conn()):
        rec = get_record(task_id)
        allowed = (TaskState.PARTIAL, TaskState.NEEDS_RECONCILIATION, TaskState.FAILED) if recovering else (TaskState.APPROVED,)
        if rec.state not in allowed:
            raise HTTPException(409, "当前状态不允许此执行操作。")
        plan, h = _approved_plan(rec)
        if not (recovering and ledger.has_execution(get_conn(),task_id)):
            _task_source_ready(task_id, plan.dataset_id)
        try:replenishment.assert_plan(get_conn(),task_id,plan,approve=True,recovering=recovering)
        except ValueError as exc:raise HTTPException(409,str(exc))
        try:approval_queue.validate_start(get_conn(),task_id,plan)
        except ValueError as exc:raise HTTPException(409,str(exc))
        if not recovering and ledger.has_execution(get_conn(),task_id):
            if ledger.has_execution(get_conn(),task_id,plan.version):
                raise HTTPException(409,'此版本已有执行尝试，请使用受控核对与恢复。')
            try:
                covered, amount = ledger.coverage(get_conn(),task_id,plan.dataset_hash)
                if plan.dataset_snapshot is None:
                    raise ValueError('跨版本执行缺少固定数据快照')
                from app.rules import build_plan
                from app.validation import merged_rows
                if plan.automation_context:
                    covered=plan.automation_context['coverage']['covered'];amount=plan.automation_context['coverage']['occupied_cents']
                recomputed = build_plan(task_id,plan.version,plan.dataset_hash,plan.policy,
                    merged_rows(plan.dataset_snapshot),plan.dataset_snapshot,covered=covered,covered_amount=amount)
                if plan.automation_context:recomputed=recomputed.model_copy(update={'schema_version':3,'automation_context':plan.automation_context})
                from app.models import plan_hash
                if plan_hash(recomputed) != h:
                    raise ValueError('已批准方案未准确扣除当前有效申请，请重新复核修订')
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
        rec.transition(TaskState.EXECUTING)
        db.upsert_task(get_conn(),task_id,rec.state,h,plan.version)
        meta = {"attempt_id": uuid.uuid4().hex, "version": plan.version, "plan_hash": h}
        orders = build_supplier_orders(plan)
        db.append_execution_event(get_conn(),task_id,"execution_started",None, {
            **meta, "protocol_version": 2, "mode": "recover" if recovering else "execute",
            "recovered": recovering,
            "orders": [{"external_ref": o["external_ref"], "supplier_id": o["supplier_id"],
                        "lines": len(o["lines"]), "total_cost_cents": o["total_cost_cents"]} for o in orders]})
    return plan, meta


def _finish_attempt(task_id, plan, meta, results):
    summary = summarize_execution(results)
    with db.transaction(get_conn()):
        rec = get_record(task_id)
        rec.transition(TaskState(summary['state']))
        db.upsert_task(get_conn(),task_id,rec.state,meta['plan_hash'],plan.version)
        db.append_execution_event(get_conn(),task_id,"execution_finished",None,
            {**meta, **summary, "final_task_state": rec.state.value})
        intent=approval_queue.latest(get_conn(),task_id)
        if intent and intent['version']==plan.version and intent['plan_hash']==meta['plan_hash']:
            approval_queue.save(get_conn(),intent,'succeeded' if rec.state==TaskState.COMPLETED else 'needs_reconciliation')
    return {"results": results, **summary}


def _run_execution(task_id, recovering=False):
    if not recovering:
        import sys
        try:replenishment.refresh_task(sys.modules[__name__],task_id,owned=True)
        except ValueError as exc:raise HTTPException(409,str(exc))
    plan, meta = _begin_attempt(task_id, recovering)
    adapter = _adapter()
    def record(result):
        ledger.record_observation(get_conn(),task_id,plan,result,'order_result',meta)
    def intent(order):
        db.append_execution_event(get_conn(),task_id,'submit_intent',order['external_ref'],
            {**meta,'submitted_at':collection.stamp(collection.utcnow()),'supplier_id':order['supplier_id'],'lines':len(order['lines']),
             'total_cost_cents':order['total_cost_cents']})
    def progress(order, kind, payload):
        db.append_execution_event(get_conn(),task_id,kind,order['external_ref'],{**payload,**meta})
    if recovering:
        observed = _observe_reconciliation(task_id,plan,adapter.reconcile_orders(plan),meta)
        to_recover = {r['external_ref'] for r in observed if r.get('submission_state') == 'not_attempted'}
        results = []
        for result in observed:
            if result['external_ref'] not in to_recover:
                record(result)
                results.append(result)
        if to_recover:
            report = adapter.execute_plan(plan,on_order_result=record,only_refs=to_recover,
                                          on_submit_intent=intent,on_progress=progress)
            results.extend(report['results'])
    else:
        results = adapter.execute_plan(plan,on_order_result=record,on_submit_intent=intent,
                                       on_progress=progress)['results']
    report = _finish_attempt(task_id,plan,meta,results)
    if recovering:
        db.activity(get_conn(),task_id,'recovered',{'version':plan.version, 'attempt_id':meta['attempt_id'], 'results':results})
        return {"task_id":task_id,"state":report['state'],"version":plan.version,"execution":report,
                "recovered":[r for r in results if not r.get('reconciled')],
                "skipped_unknown":[r['external_ref'] for r in results if r.get('problems')],
                "message":"恢复及核对结果已保存；待核对事项不会重新提交。"}
    _phase(task_id,"汇总申请与核对结果")
    try:
        with ToolSession(get_conn(),task_id,'executor',current=plan).activate():
            agent=_run_agent_stage('executor',canonical_json({'task_id':task_id,'version':plan.version,'execution':report}),
                '请根据真实回执汇总执行结果；可以调用 get_execution_result 核对，不允许写入或重试采购。')
            _check_summary_currency(agent)
    except AgentStageError as exc:
        agent={'status':'error','summary':'','error':str(exc)}
    _agent_event(task_id,'executor',plan.version,agent)
    return {"task_id":task_id,"state":report['state'],"executed_plan_hash":meta['plan_hash'],"execution":report,"agent":agent}


@app.post("/tasks/{task_id}/execute")
def execute(task_id: str, prefer: str = Header(default="")):
    if get_record(task_id).state is not TaskState.APPROVED:
        raise HTTPException(409, "只有已批准且尚未执行的方案可以执行。")
    plan, _ = _approved_plan(get_record(task_id))
    _task_source_ready(task_id, plan.dataset_id)
    return _dispatch(task_id,"录入采购申请",lambda:_run_execution(task_id),prefer)


@app.post("/tasks/{task_id}/reconcile")
def reconcile(task_id: str, prefer: str = Header(default="")):
    if get_record(task_id).state not in (TaskState.NEEDS_RECONCILIATION,TaskState.PARTIAL,TaskState.FAILED):
        raise HTTPException(409,"只有执行异常的任务可以受控核对。")
    def work():
        rec=get_record(task_id)
        if rec.state not in (TaskState.NEEDS_RECONCILIATION,TaskState.PARTIAL,TaskState.FAILED):
            raise HTTPException(409,"当前状态不能核对。")
        plan,_=_approved_plan(rec)
        _observe_reconciliation(task_id,plan,_adapter().reconcile_orders(plan))
        facts=ledger.effective_orders(get_conn(),task_id)
        results=[{**r,'result':r['result']} for r in facts.values() if r['version']==plan.version]
        return {"task_id":task_id,"state":rec.state.value,"version":plan.version,"reconciliation":results}
    return _dispatch(task_id,"核对采购申请",work,prefer)


@app.post("/tasks/{task_id}/recover")
def recover(task_id: str, prefer: str = Header(default="")):
    if get_record(task_id).state not in (TaskState.NEEDS_RECONCILIATION,TaskState.PARTIAL,TaskState.FAILED):
        raise HTTPException(409,"只有执行异常的任务可以受控恢复。")
    return _dispatch(task_id,"受控恢复",lambda:_run_execution(task_id,True),prefer)


@app.get("/tasks/{task_id}")
def get_status(task_id: str):
    conn = get_conn()
    # Read state and busy flag together: a finishing worker must not produce
    # an ANALYZING / idle combination in a single polling response.
    with db._LOCK:
        rec = get_record(task_id)
        plan, h = db.get_plan(conn, task_id, _latest_version(task_id))
        return {"task_id": task_id, "state": rec.state.value,
                "approved_plan_hash": rec.approved_plan_hash,
                "approved_version": rec.approved_version,
                "latest_plan": plan.model_dump() if plan else None,
                "latest_plan_hash": h, "operation":db.get_context(conn,task_id),
                "data_status":collection.task_data_status(conn, COLLECTION_DIR, task_id, plan.dataset_id if plan else None),
                "execution_intent":approval_queue.latest(conn,task_id),
                "automation":replenishment.task_info(conn,task_id,plan)}


@app.get("/tasks/{task_id}/executions")
def get_executions(task_id: str):
    get_record(task_id)
    return {"task_id": task_id, "records": db.list_execution_records(get_conn(), task_id)}


@app.get("/tasks/{task_id}/summary")
def get_task_summary(task_id: str):
    get_record(task_id)
    with db._LOCK:
        return ledger.task_summary(get_conn(),task_id)


@app.get("/tasks/{task_id}/audit-trail")
def get_audit_trail(task_id: str):
    get_record(task_id)
    with db._LOCK:
        return ledger.audit(get_conn(),task_id)


def _latest_version(task_id: str) -> int:
    versions = db.list_plan_versions(get_conn(), task_id)
    return versions[-1] if versions else 1


@app.get("/tasks/{task_id}/activity")
def get_activity(task_id:str):
    get_record(task_id)
    return {"events":db.list_activity(get_conn(),task_id)}


@app.get("/tasks/{task_id}/plans")
def get_versions(task_id:str):
    get_record(task_id)
    result=[]
    for version in db.list_plan_versions(get_conn(),task_id):
        p,h=db.get_plan(get_conn(),task_id,version)
        result.append({"version":version,"plan_hash":h,"total_cost_cents":p.total_cost_cents,"dataset_id":p.dataset_id})
    return {"versions":result}


@app.get("/tasks/{task_id}/plans/{version}")
def get_version(task_id:str,version:int):
    get_record(task_id)
    p,h=db.get_plan(get_conn(),task_id,version)
    if p is None:
        raise HTTPException(404,"找不到方案版本。")
    return {"plan":p.model_dump(),"plan_hash":h}


@app.get("/tasks/{task_id}/diff")
def get_diff(task_id:str,from_version:int,to_version:int):
    get_record(task_id)
    a,_=db.get_plan(get_conn(),task_id,from_version)
    b,_=db.get_plan(get_conn(),task_id,to_version)
    if a is None or b is None:
        raise HTTPException(404,"找不到比较的方案版本。")
    return reports.plan_diff(a,b)


@app.get("/tasks/{task_id}/artifacts/{version}/{kind}")
def get_artifact(task_id:str,version:int,kind:str):
    rec=get_record(task_id)
    plan,h=db.get_plan(get_conn(),task_id,version)
    if plan is None or kind not in ('csv','html'):
        raise HTTPException(404,"找不到交付物。")
    content=reports.export_csv(plan) if kind=='csv' else reports.export_html(plan,rec.state.value,
        db.list_activity(get_conn(),task_id),db.list_execution_records(get_conn(),task_id),h,task_summary=ledger.task_summary(get_conn(),task_id),audit_summary=ledger.audit(get_conn(),task_id)).encode('utf-8')
    return Response(content,media_type='text/csv' if kind=='csv' else 'text/html',headers={
        'Content-Disposition':f'attachment; filename="restock-{version}.{kind}"',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'"})
