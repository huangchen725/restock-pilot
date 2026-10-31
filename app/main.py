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

from app import db, runner, jobs, datasets, reports
from app.execution import PlaywrightProcurementAdapter, build_supplier_orders
from app.models import Policy, TaskState, canonical_json, DataContract, RevisionDecision
from app.prompts import build_stage_prompt
from app.state_machine import TaskRecord, TransitionError
from app.validation import load_snapshot, snapshot_hash, validate_policy
from app.business_tools import ToolSession, active_session, planner_review
from app.agent_runtime import AgentRuntime

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = ROOT / os.getenv("RESTOCK_DB_PATH", "restock.db")

@asynccontextmanager
async def lifespan(app):
    get_conn()
    yield
    jobs.shutdown()


app = FastAPI(title="RestockPilot", version="0.6.0-m2", lifespan=lifespan)
_conn = None
_tasks: dict = {}
_conn_lock = threading.Lock()
APPROVAL_TOKEN = os.getenv("RESTOCK_APPROVAL_TOKEN") or secrets.token_urlsafe(32)


@app.get("/health")
def health():
    return {"status": "ok", "service": "restock-pilot", "version":"0.6.0-m2",
            "agent_mode":os.getenv("RESTOCK_AGENT_MODE","required")}


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


class ReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field(default="", max_length=4000)
    policy: Policy | None = None
    dataset_id: str | None = None


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(gt=0)
    plan_hash: str = Field(min_length=64, max_length=64)


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
    # This catches a reproduced translation error, not every possible semantic
    # error in free-form model explanations. Tables/amounts remain authoritative.
    if re.search(r'\d[\d,.]*\s*(?:美分|美元|USD\b)', agent.get('summary',''), re.IGNORECASE):
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
        "tool_calls":_sanitized(agent.get('tool_calls', []))})


def _dispatch(task_id, phase, work, prefer):
    conn=get_conn()
    if not db.claim_task(conn,task_id,phase):
        raise HTTPException(409,"任务正在处理，请等待当前步骤完成。")
    def run():
        try:
            result=work()
            db.update_context(conn,task_id,result_json=json.dumps(_sanitized(result),ensure_ascii=False),
                              error=None,phase="本次操作完成")
            return result
        except Exception as exc:
            message=_public(exc.detail if isinstance(exc,HTTPException) else str(exc))
            db.update_context(conn,task_id,error=message,phase="需要处理")
            row=db.get_task(conn,task_id)
            if row and row['state'] in ('ANALYZING','EXECUTING'):
                state=TaskState.NEEDS_RECONCILIATION if row['state']=='EXECUTING' else (
                    TaskState.NEEDS_REVIEW if db.list_plan_versions(conn,task_id) else TaskState.FAILED)
                db.upsert_task(conn,task_id,state,
                    row['approved_plan_hash'] if state==TaskState.NEEDS_RECONCILIATION else None,
                    row['approved_version'] if state==TaskState.NEEDS_RECONCILIATION else None)
            db.activity(conn,task_id,"error",{"message":message})
            if prefer == "wait":
                raise HTTPException(exc.status_code if isinstance(exc,HTTPException) else 500,message)
        finally:
            db.update_context(conn,task_id,busy=0)
    if prefer == "wait":
        return run()
    try:
        jobs.submit(run)
    except Exception:
        db.update_context(conn,task_id,busy=0,error="无法启动任务，请重试。")
        raise HTTPException(503,"无法启动任务，请重试。")
    return JSONResponse(status_code=202,content={"task_id":task_id,"accepted":True})


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
    db.upsert_task(get_conn(),task_id,TaskState.NEW)
    db.update_context(get_conn(),task_id,request_json=req.model_dump_json())
    db.activity(get_conn(),task_id,"created",{"request":req.model_dump()})
    return _dispatch(task_id,"检查资料",lambda:_create_work(task_id,req,snap),prefer)


def _create_work(task_id,req,snapshot):
    rec=get_record(task_id)
    rec.transition(TaskState.ANALYZING)
    db.upsert_task(get_conn(),task_id,rec.state)
    problems=[]
    if snapshot is None:
        snapshot,problems=load_snapshot(DATA_DIR,req.data_contract)
    if problems:
        rec.transition(TaskState.NEEDS_INPUT)
        db.upsert_task(get_conn(),task_id,rec.state)
        return {"task_id":task_id,"state":rec.state.value,"data_problems":problems}
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
        return {"task_id":task_id,"state":rec.state.value,"data_problems":problems}
    plan=None
    # 统一数据源：将当前 snapshot 保存为 dataset，Planner 通过 MCP 以同一 dataset_id 生成方案。
    dataset_id=db.save_dataset(get_conn(),snapshot)
    _phase(task_id,"生成方案与复核摘要")
    planner_input=(
        f"为任务 {task_id} 生成补货方案："
        f"1) 用 inspect_dataset(task_id=\"{task_id}\") 检查数据集；"
        f"2) 用 build_plan(task_id=\"{task_id}\", dataset_id=\"{dataset_id}\", policy={policy.model_dump_json()}) 生成方案；"
        f"3) 根据工具返回的方案给出简短非数值复核建议。不要复述任何金额、数量统计或商品编号。"
    )
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
            plan,h = session.commit(policy)
    except (AgentStageError, ValueError) as exc:
        rec.transition(TaskState.FAILED)
        db.upsert_task(get_conn(),task_id,rec.state)
        raise HTTPException(503,str(exc))
    _agent_event(task_id,"planner",1,agent)
    rec.transition(TaskState.NEEDS_REVIEW)
    db.upsert_task(get_conn(),task_id,rec.state)
    return {"task_id":task_id,"state":rec.state.value,"plan":plan.model_dump(),"plan_hash":h,"agent":agent}


@app.post("/tasks/{task_id}/approve")
def approve(task_id:str,req:ApprovalRequest,x_restock_approval:str=Header(default="")):
    if not secrets.compare_digest(x_restock_approval,APPROVAL_TOKEN):
        raise HTTPException(403,"批准只能从已配置的人工工作台提交。")
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
        try:
            rec.approve(h,plan.version)
        except TransitionError as exc:
            raise HTTPException(409,str(exc))
        db.upsert_task(get_conn(),task_id,rec.state,h,plan.version)
        db.activity(get_conn(),task_id,"approved",{"version":plan.version,"plan_hash":h,"source":"human_workbench"})
        return {"task_id":task_id,"state":rec.state.value,"approved_plan_hash":h,"approved_version":plan.version}
    finally:
        db.update_context(get_conn(),task_id,busy=0,phase="等待执行" if rec.state==TaskState.APPROVED else "等待复核")


@app.post("/tasks/{task_id}/revise")
def revise(task_id:str,req:ReviseRequest,prefer:str=Header(default="")):
    rec=get_record(task_id)
    if rec.state not in (TaskState.NEEDS_REVIEW,TaskState.APPROVED,TaskState.NEEDS_INPUT):
        raise HTTPException(409,"当前状态不能修改方案。")
    if req.comment.strip() and req.policy is not None:
        raise HTTPException(422,"请选择自然语言或参数修改中的一种。")
    if not req.comment.strip() and req.policy is None and not req.dataset_id:
        raise HTTPException(422,"请填写修改意见、参数或选择新数据。")
    if req.dataset_id:
        _snapshot(req.dataset_id)
    return _dispatch(task_id,"重新计算方案",lambda:_revise_work(task_id,req),prefer)


def _revise_work(task_id,req):
    rec=get_record(task_id)
    current,_=db.get_plan(get_conn(),task_id,_latest_version(task_id))
    rec.transition(TaskState.ANALYZING)
    db.upsert_task(get_conn(),task_id,rec.state)  # Revokes approval before changing data or policy.
    db.activity(get_conn(),task_id,"revision_requested",{"version":current.version+1 if current else 1,**req.model_dump()})
    snapshot=_snapshot(req.dataset_id) if req.dataset_id else (current.dataset_snapshot if current else None)
    if snapshot is None:
        raise _revision_error(rec,"legacy plan has no data snapshot; create a new task with confirmed data",409)
    if not req.dataset_id and snapshot_hash(snapshot)!=current.dataset_hash:
        raise _revision_error(rec,"dataset snapshot hash mismatch",409)
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
            plan,h=session.commit(policy)
    except (AgentStageError, ValueError) as exc:
        raise _revision_error(rec,str(exc),422)
    _agent_event(task_id,"reviser",plan.version,agent)
    rec.transition(TaskState.NEEDS_REVIEW)
    db.upsert_task(get_conn(),task_id,rec.state)
    return {"task_id":task_id,"state":rec.state.value,"plan":plan.model_dump(),"plan_hash":h,"agent":agent}


@app.post("/tasks/{task_id}/execute")
def execute(task_id: str,prefer:str=Header(default="")):
    rec=get_record(task_id)
    if rec.state is not TaskState.APPROVED:
        raise HTTPException(409,"只有已批准且尚未执行的方案可以执行。")
    return _dispatch(task_id,"录入采购申请",lambda:_execute_work(task_id),prefer)


def _execute_work(task_id):
    rec = get_record(task_id)
    # 永远按批准的 version 取方案，不能因数据库出现更新版本而换单。
    version = rec.approved_version or _latest_version(task_id)
    plan, h = db.get_plan(get_conn(), task_id, version)
    try:
        rec.check_execution_authority(h)
        if rec.state is TaskState.APPROVED:
            rec.transition(TaskState.EXECUTING)
        # EXECUTING 可重入：进程崩溃后的恢复也走同一 external_ref 回查流程。
    except TransitionError as exc:
        raise HTTPException(409, str(exc))
    db.upsert_task(get_conn(), task_id, rec.state, h, plan.version)

    adapter = PlaywrightProcurementAdapter(
        base_url=os.getenv("PROCUREMENT_PORTAL_URL", "http://127.0.0.1:8200"),
        headless=os.getenv("PROCUREMENT_HEADLESS", "true").lower() != "false",
        timeout_ms=int(os.getenv("PROCUREMENT_TIMEOUT_MS", "15000")),
    )
    orders = build_supplier_orders(plan)
    db.append_execution_event(get_conn(), task_id, "execution_started", None, {
        "plan_id": plan.plan_id, "version": plan.version, "plan_hash": h,
        "orders": [{"external_ref": o["external_ref"],
                    "supplier_id": o["supplier_id"],
                    "lines": len(o["lines"]),
                    "total_cost_cents": o["total_cost_cents"]}
                   for o in orders],
    })
    report = adapter.execute_plan(plan)
    for result in report["results"]:
        db.save_execution_record(
            get_conn(), task_id, plan.plan_id, plan.version,
            result["external_ref"], result["status"], result,
        )
        db.append_execution_event(get_conn(), task_id, "order_result",
                                  result["external_ref"], {
            "status": result["status"],
            "order_id": result.get("order_id"),
            "problems": result.get("problems"),
            "error": result.get("error"),
            "attempt_log": result.get("attempt_log", []),
            "plan_hash": h,
        })
    rec.transition(TaskState(report["state"]))
    db.upsert_task(get_conn(), task_id, rec.state, h, plan.version)
    db.append_execution_event(get_conn(), task_id, "execution_finished", None, {
        "state": report["state"], "succeeded": report.get("succeeded"),
        "total": report.get("total"), "plan_hash": h,
        "final_task_state": rec.state.value,
    })

    _phase(task_id,"汇总申请与核对结果")
    try:
        with ToolSession(get_conn(), task_id, 'executor', current=plan).activate():
            agent = _run_agent_stage("executor", canonical_json({'task_id':task_id,'version':plan.version,'execution':report}),
                "请根据真实回执汇总执行结果；可以调用 get_execution_result 核对，不允许写入或重试采购。")
            _check_summary_currency(agent)
    except AgentStageError as exc:
        # 采购结果已经持久化，解释 Agent 故障不能掩盖或改变实际执行状态。
        agent = {"status": "error", "summary": "", "error": str(exc)}
    _agent_event(task_id,"executor",plan.version,agent)
    return {"task_id": task_id, "state": rec.state.value,
            "executed_plan_hash": h, "execution": report, "agent": agent}


@app.post("/tasks/{task_id}/reconcile")
def reconcile(task_id: str):
    """M3 受控核对：按原 external_ref 查询实际后台，返回确定状态，不创建新单。

    仅 NEEDS_RECONCILIATION / PARTIAL 状态可调用（执行结果待核对的任务）。
    返回每个供应商的确定状态（created / not_submitted / unknown），
    供人工复核与后续受控恢复决策；本端点只读，不提交、不修改任务状态。
    """
    rec = get_record(task_id)
    if rec.state not in (TaskState.NEEDS_RECONCILIATION, TaskState.PARTIAL):
        raise HTTPException(409, "只有执行结果待核对的任务可以受控核对。")
    version = rec.approved_version or _latest_version(task_id)
    plan, _ = db.get_plan(get_conn(), task_id, version)
    if plan is None:
        raise HTTPException(404, "找不到已批准的方案。")
    adapter = PlaywrightProcurementAdapter(
        base_url=os.getenv("PROCUREMENT_PORTAL_URL", "http://127.0.0.1:8200"),
        headless=True,
    )
    results = adapter.reconcile_orders(plan)
    db.activity(get_conn(), task_id, "reconciled", {
        "version": plan.version,
        "states": [{"external_ref": r["external_ref"], "state": r["state"]}
                   for r in results],
    })
    return {"task_id": task_id, "state": rec.state.value,
            "version": plan.version, "reconciliation": results}


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
                "latest_plan_hash": h, "operation":db.get_context(conn,task_id)}


@app.get("/tasks/{task_id}/executions")
def get_executions(task_id: str):
    get_record(task_id)
    return {"task_id": task_id, "records": db.list_execution_records(get_conn(), task_id)}


@app.get("/tasks/{task_id}/audit-trail")
def get_audit_trail(task_id: str):
    """执行回放审计账本：append-only 事件链（seq 即回放顺序）。

    完整性校验（M3）：空账本不代表完整执行；必须有执行结束事件；
    order_result 必须携带 plan_hash 且与当前批准版本一致。
    """
    rec = get_record(task_id)
    events = db.list_execution_events(get_conn(), task_id)
    if not events:
        replay_ok = False  # 空账本不代表完整执行
    else:
        has_finish = any(e["event_type"] == "execution_finished" for e in events)
        approved_hash = rec.approved_plan_hash
        replay_ok = has_finish and all(
            e["event_type"] != "order_result"
            or (e["payload"].get("plan_hash")
                and (approved_hash is None or e["payload"]["plan_hash"] == approved_hash))
            for e in events
        )
    return {"task_id": task_id, "events": events,
            "replay_complete": replay_ok, "event_count": len(events)}


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
        db.list_activity(get_conn(),task_id),db.list_execution_records(get_conn(),task_id),h).encode('utf-8')
    return Response(content,media_type='text/csv' if kind=='csv' else 'text/html',headers={
        'Content-Disposition':f'attachment; filename="restock-{version}.{kind}"',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'"})
