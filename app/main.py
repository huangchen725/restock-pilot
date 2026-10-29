# -*- coding: utf-8 -*-
"""FastAPI 入口：真实三阶段 Agent + 审批 gate + 固定浏览器执行适配器。"""

import hashlib
import json
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import db, runner
from app.execution import PlaywrightProcurementAdapter, build_supplier_orders
from app.models import Plan, Policy, TaskState, canonical_json
from app.prompts import build_stage_prompt
from app.rules import build_plan
from app.state_machine import TaskRecord, TransitionError
from app.validation import load_dataset

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = ROOT / "restock.db"

app = FastAPI(title="RestockPilot", version="0.3.0-step3")
_conn = None
_tasks: dict = {}


class AgentStageError(RuntimeError):
    pass


def get_conn():
    global _conn
    if _conn is None:
        _conn = db.init_db(DB_PATH)
    return _conn


def get_record(task_id: str) -> TaskRecord:
    if task_id not in _tasks:
        row = db.get_task(get_conn(), task_id)
        if row is None:
            raise HTTPException(404, f"task {task_id} not found")
        rec = TaskRecord(task_id)
        rec.state = TaskState(row["state"])
        rec.approved_plan_hash = row["approved_plan_hash"]
        rec.approved_version = row["approved_version"]
        _tasks[task_id] = rec
    return _tasks[task_id]


class AnalyzeRequest(BaseModel):
    policy: Policy


class ReviseRequest(BaseModel):
    comment: str


def _dataset_hash(sales, inv, sup) -> str:
    payload = json.dumps([sales, inv, sup], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _merged_rows(sales, inv, sup) -> List[dict]:
    inv_by_sku = {r["sku"]: r for r in inv}
    sup_by_sku = {r["sku"]: r for r in sup}
    sales_by_sku = defaultdict(list)
    for r in sales:
        sales_by_sku[r["sku"]].append(float(r["sold_qty"]))
    rows = []
    for sku, seq in sales_by_sku.items():
        inv_r = inv_by_sku[sku]
        sup_r = sup_by_sku[sku]
        seq = seq[-28:]
        if len(seq) < 28:
            seq = [0.0] * (28 - len(seq)) + seq
        rows.append({
            "sku": sku,
            "name": inv_r["name"], "category": inv_r["category"],
            "available_qty": int(inv_r["available_qty"]),
            "inbound_qty": int(inv_r["inbound_qty"]),
            "inbound_eta_days": (
                int(inv_r["inbound_eta"])
                if inv_r.get("inbound_eta") not in (None, "", "None") else None
            ),
            "supplier_id": sup_r["supplier_id"],
            "unit_cost_cents": int(sup_r["unit_cost_cents"]),
            "lead_days": int(sup_r["lead_days"]),
            "pack_size": int(sup_r["pack_size"]),
            "min_order_qty": int(sup_r["min_order_qty"]),
            "daily_sales": seq,
        })
    return rows


def _run_agent_stage(stage: str, context: str, user_input: str) -> dict:
    """真实调用 runner；off 只用于显式离线测试，绝不伪装成 Agent 成功。"""
    if os.getenv("RESTOCK_AGENT_MODE", "required").lower() == "off":
        return {"status": "disabled", "summary": "", "tool_calls": [], "duration_s": 0}
    prompt = build_stage_prompt(stage, context, user_input)
    result = runner.run_codex_task(
        prompt=prompt,
        workdir=str(ROOT),
        timeout_s=int(os.getenv("RESTOCK_AGENT_TIMEOUT_S", "600")),
    )
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
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
        policy_payload = payload["policy"] if isinstance(payload, dict) else None
        if not isinstance(policy_payload, dict):
            raise ValueError("missing policy object")
        required = {"budget_cents", "cover_days", "safety_days",
                    "priority_categories", "excluded_skus"}
        if set(policy_payload) != required:
            raise ValueError("policy must contain exactly the five supported fields")
        return Policy.model_validate(policy_payload)
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        raise AgentStageError(f"invalid Reviser policy output: {exc}") from exc


def _revision_error(rec: TaskRecord, message: str, status_code: int) -> HTTPException:
    # 修订失败不伪造 v+1；保留上一版供复核，但旧批准已按状态机失效。
    rec.transition(TaskState.NEEDS_REVIEW)
    db.upsert_task(get_conn(), rec.task_id, rec.state)
    return HTTPException(status_code, message)


@app.post("/tasks")
def create_task(req: AnalyzeRequest):
    task_id = str(uuid.uuid4())[:8]
    rec = TaskRecord(task_id)
    _tasks[task_id] = rec
    rec.transition(TaskState.ANALYZING)
    db.upsert_task(get_conn(), task_id, rec.state)

    sales, inv, sup, problems = load_dataset(DATA_DIR)
    if problems:
        rec.transition(TaskState.NEEDS_INPUT)
        db.upsert_task(get_conn(), task_id, rec.state)
        return {"task_id": task_id, "state": rec.state.value, "data_problems": problems}

    ds_hash = _dataset_hash(sales, inv, sup)
    plan = build_plan(task_id, 1, ds_hash, req.policy, _merged_rows(sales, inv, sup))
    h = db.save_plan(get_conn(), plan)
    try:
        agent = _run_agent_stage(
            "planner", canonical_json(plan.model_dump()),
            "请检查这份规则引擎方案并给出复核摘要。",
        )
    except AgentStageError as exc:
        rec.transition(TaskState.FAILED)
        db.upsert_task(get_conn(), task_id, rec.state)
        raise HTTPException(503, {"task_id": task_id, "error": str(exc)})
    rec.transition(TaskState.NEEDS_REVIEW)
    db.upsert_task(get_conn(), task_id, rec.state)
    return {"task_id": task_id, "state": rec.state.value,
            "plan": plan.model_dump(), "plan_hash": h, "agent": agent}


@app.post("/tasks/{task_id}/approve")
def approve(task_id: str):
    rec = get_record(task_id)
    plan, h = db.get_plan(get_conn(), task_id, _latest_version(task_id))
    if plan is None:
        raise HTTPException(404, "no plan to approve")
    try:
        rec.approve(h, plan.version)
    except TransitionError as exc:
        raise HTTPException(409, str(exc))
    db.upsert_task(get_conn(), task_id, rec.state, h, plan.version)
    return {"task_id": task_id, "state": rec.state.value,
            "approved_plan_hash": h, "approved_version": plan.version}


@app.post("/tasks/{task_id}/revise")
def revise(task_id: str, req: ReviseRequest):
    rec = get_record(task_id)
    current_plan, _ = db.get_plan(get_conn(), task_id, _latest_version(task_id))
    if current_plan is None:
        raise HTTPException(404, "no plan to revise")
    try:
        rec.transition(TaskState.ANALYZING)
    except TransitionError as exc:
        raise HTTPException(409, str(exc))
    db.upsert_task(get_conn(), task_id, rec.state)

    context = canonical_json({
        "current_policy": current_plan.policy.model_dump(),
        "current_version": current_plan.version,
        "current_total_cost_cents": current_plan.total_cost_cents,
        "skipped": current_plan.skipped,
    })
    try:
        agent = _run_agent_stage("reviser", context, req.comment)
        new_policy = _policy_from_agent(agent["summary"])
    except AgentStageError as exc:
        raise _revision_error(rec, str(exc), 422)

    sales, inv, sup, problems = load_dataset(DATA_DIR)
    if problems:
        raise _revision_error(rec, "dataset changed and is invalid: " + "; ".join(problems), 422)
    new_plan = build_plan(
        task_id, current_plan.version + 1, _dataset_hash(sales, inv, sup),
        new_policy, _merged_rows(sales, inv, sup),
    )
    h = db.save_plan(get_conn(), new_plan)
    rec.transition(TaskState.NEEDS_REVIEW)
    db.upsert_task(get_conn(), task_id, rec.state)
    return {"task_id": task_id, "state": rec.state.value,
            "plan": new_plan.model_dump(), "plan_hash": h, "agent": agent}


@app.post("/tasks/{task_id}/execute")
def execute(task_id: str):
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

    try:
        agent = _run_agent_stage(
            "executor", canonical_json(report),
            "请根据真实回执汇总执行结果；不要执行任何写操作。",
        )
    except AgentStageError as exc:
        # 采购结果已经持久化，解释 Agent 故障不能掩盖或改变实际执行状态。
        agent = {"status": "error", "summary": "", "error": str(exc)}
    return {"task_id": task_id, "state": rec.state.value,
            "executed_plan_hash": h, "execution": report, "agent": agent}


@app.get("/tasks/{task_id}")
def get_status(task_id: str):
    rec = get_record(task_id)
    plan, h = db.get_plan(get_conn(), task_id, _latest_version(task_id))
    return {"task_id": task_id, "state": rec.state.value,
            "approved_plan_hash": rec.approved_plan_hash,
            "approved_version": rec.approved_version,
            "latest_plan": plan.model_dump() if plan else None,
            "latest_plan_hash": h}


@app.get("/tasks/{task_id}/executions")
def get_executions(task_id: str):
    get_record(task_id)
    return {"task_id": task_id, "records": db.list_execution_records(get_conn(), task_id)}


@app.get("/tasks/{task_id}/audit-trail")
def get_audit_trail(task_id: str):
    """执行回放审计账本：append-only 事件链（seq 即回放顺序）。"""
    get_record(task_id)
    events = db.list_execution_events(get_conn(), task_id)
    replay_ok = all(
        event["event_type"] != "order_result" or event["payload"].get("plan_hash")
        for event in events
    )
    return {"task_id": task_id, "events": events,
            "replay_complete": replay_ok, "event_count": len(events)}


def _latest_version(task_id: str) -> int:
    row = get_conn().execute(
        "SELECT MAX(version) v FROM plans WHERE task_id = ?", (task_id,)
    ).fetchone()
    return (row["v"] or 1) if row else 1
