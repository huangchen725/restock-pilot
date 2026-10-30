# -*- coding: utf-8 -*-
"""任务状态机：严格单向流转 + 审批绑定 plan_hash。

合法迁移：
  NEW → ANALYZING
  ANALYZING → NEEDS_REVIEW | NEEDS_INPUT | FAILED
  NEEDS_REVIEW → APPROVED            （人工批准，绑定 plan_hash）
  NEEDS_REVIEW → ANALYZING           （用户提修改意见，重新生成 v+1，旧批准失效）
  APPROVED → EXECUTING
  EXECUTING → COMPLETED | PARTIAL | NEEDS_RECONCILIATION | FAILED
  PARTIAL / NEEDS_RECONCILIATION → NEEDS_REVIEW（人工重新决策）
"""

from typing import Optional, Set

from app.models import TaskState

TRANSITIONS: dict[TaskState, Set[TaskState]] = {
    TaskState.NEW: {TaskState.ANALYZING},
    TaskState.NEEDS_INPUT: {TaskState.ANALYZING},
    TaskState.ANALYZING: {TaskState.NEEDS_REVIEW, TaskState.NEEDS_INPUT, TaskState.FAILED},
    TaskState.NEEDS_REVIEW: {TaskState.APPROVED, TaskState.ANALYZING},
    # 批准后用户仍可提出修改：撤销批准 → 重新生成 v+1 → 回 NEEDS_REVIEW
    TaskState.APPROVED: {TaskState.EXECUTING, TaskState.ANALYZING},
    TaskState.EXECUTING: {
        TaskState.COMPLETED, TaskState.PARTIAL,
        TaskState.NEEDS_RECONCILIATION, TaskState.FAILED,
    },
    TaskState.PARTIAL: {TaskState.NEEDS_REVIEW},
    TaskState.NEEDS_RECONCILIATION: {TaskState.NEEDS_REVIEW},
    # 终态
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
}


class TransitionError(Exception):
    pass


def assert_transition(current: TaskState, target: TaskState) -> None:
    if target not in TRANSITIONS[current]:
        raise TransitionError(f"illegal transition: {current.value} -> {target.value}")


class TaskRecord:
    """内存态任务记录（DB 层负责持久化，这里只管状态与审批绑定逻辑）。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.state = TaskState.NEW
        self.approved_plan_hash: Optional[str] = None
        self.approved_version: Optional[int] = None

    def transition(self, target: TaskState) -> None:
        assert_transition(self.state, target)
        # 任何回到 ANALYZING 的修改路径都使旧批准立即失效
        if target is TaskState.ANALYZING and self.state in (
            TaskState.NEEDS_REVIEW, TaskState.APPROVED,
        ):
            self.approved_plan_hash = None
            self.approved_version = None
        self.state = target

    def approve(self, plan_hash: str, version: int) -> None:
        """批准只能发生在 NEEDS_REVIEW，且必须绑定具体方案指纹。"""
        if self.state is not TaskState.NEEDS_REVIEW:
            raise TransitionError(
                f"approve only allowed in NEEDS_REVIEW (current: {self.state.value})"
            )
        self.approved_plan_hash = plan_hash
        self.approved_version = version
        self.transition(TaskState.APPROVED)

    def check_execution_authority(self, plan_hash: str) -> None:
        """执行前校验：待执行方案必须与批准指纹逐字一致。"""
        if self.state not in (TaskState.APPROVED, TaskState.EXECUTING):
            raise TransitionError(
                f"execute only allowed in APPROVED/EXECUTING (current: {self.state.value})"
            )
        if self.approved_plan_hash is None or plan_hash != self.approved_plan_hash:
            raise TransitionError(
                "plan hash mismatch: refused to execute a plan that was never approved"
            )
