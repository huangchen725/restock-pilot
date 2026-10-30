# -*- coding: utf-8 -*-
"""M2 业务 MCP 工具层（STDIO server，供 Codex CLI 子进程使用）。

边界：
- 金额与数量只能由 app.rules 规则引擎产生；模型只传 policy，不能编造数值。
- build_plan / revise_plan 仅在许可的任务状态执行，且撤销旧批准。
- 同任务 + 同 policy + 同数据集的重复调用复用现有版本（幂等，不生成重复版本）。
- Agent 不直接读写业务数据库；所有持久化经由本工具层。
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from app import db
from app.models import Policy, TaskState
from app.rules import build_plan as rules_build_plan
from app.validation import load_snapshot, merged_rows, snapshot_hash

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("RESTOCK_DB_PATH", str(ROOT / "restock.db")))
DATA_DIR = Path(os.getenv("RESTOCK_DATA_DIR", str(ROOT / "data")))

server = MCPServer("restock-pilot")
_conn = None


def get_conn():
    global _conn
    if _conn is None:
        _conn = db.init_db(DB_PATH)
    return _conn


def _get_task(task_id: str):
    row = db.get_task(get_conn(), task_id)
    if row is None:
        raise ValueError(f"task {task_id} not found")
    return row


def _latest_version(task_id: str) -> int:
    row = get_conn().execute(
        "SELECT MAX(version) v FROM plans WHERE task_id = ?", (task_id,)
    ).fetchone()
    return (row["v"] or 0) if row else 0


def _generate(task_id: str, policy: Policy, stage: str) -> dict:
    """build_plan / revise_plan 共用：状态校验 + 幂等 + 生成 + 撤销旧批准。"""
    row = _get_task(task_id)
    state = row["state"]
    # 允许 ANALYZING / NEEDS_INPUT / NEEDS_REVIEW / APPROVED（修订即撤销旧批准）；
    # 执行中与已完成禁止改写，避免与已提交采购冲突。
    if state in (TaskState.EXECUTING.value, TaskState.COMPLETED.value):
        raise ValueError(
            f"任务状态 {state} 不允许{stage}；执行中/已完成的方案不可被 Agent 改写"
        )
    snapshot, problems = load_snapshot(DATA_DIR)
    if problems:
        return {"error": "dataset_problems", "problems": problems}

    ds_hash = snapshot_hash(snapshot)
    latest = _latest_version(task_id)
    if latest:
        existing, existing_hash = db.get_plan(get_conn(), task_id, latest)
        if (existing is not None and existing.policy == policy
                and existing.dataset_hash == ds_hash):
            return {"plan": existing.model_dump(exclude={"dataset_snapshot"}),
                    "plan_hash": existing_hash, "reused": True}

    plan = rules_build_plan(task_id, latest + 1, ds_hash, policy,
                            merged_rows(snapshot), snapshot)
    plan_hash = db.save_plan(get_conn(), plan)
    # 生成新版本即撤销旧批准（人工需重新复核批准）。
    db.upsert_task(get_conn(), task_id, TaskState.NEEDS_REVIEW)
    return {"plan": plan.model_dump(exclude={"dataset_snapshot"}),
            "plan_hash": plan_hash, "reused": False}


@server.tool(description="检查当前任务的数据集：SKU 数、数据问题与口径。")
def inspect_dataset(task_id: str) -> dict:
    _get_task(task_id)
    snapshot, problems = load_snapshot(DATA_DIR)
    return {
        "dataset_hash": snapshot_hash(snapshot),
        "sku_count": len(snapshot.inventory),
        "problems": problems,
        "contract": snapshot.contract.model_dump(),
    }


@server.tool(name="build_plan",
             description="用规则引擎生成补货方案；金额数量由规则计算。同 policy 重复调用复用现有版本。")
def build_plan(task_id: str, policy: dict | None = None) -> dict:
    pol = Policy.model_validate(policy) if policy else Policy(budget_cents=500000)
    return _generate(task_id, pol, "生成方案")


@server.tool(name="revise_plan",
             description="按新 policy 修订方案（生成 v+1 并撤销旧批准）；金额数量由规则计算。")
def revise_plan(task_id: str, policy: dict) -> dict:
    pol = Policy.model_validate(policy)
    return _generate(task_id, pol, "修订方案")


@server.tool(description="查询任务的执行回执与审计事件。")
def get_execution_result(task_id: str) -> dict:
    _get_task(task_id)
    return {
        "records": db.list_execution_records(get_conn(), task_id),
        "events": db.list_execution_events(get_conn(), task_id),
    }


if __name__ == "__main__":
    server.run()
