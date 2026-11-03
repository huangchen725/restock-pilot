# -*- coding: utf-8 -*-
"""M4 冻结版本连续稳定性测试——连续 10 次正常完整流程至少成功 9 次（内部目标）。

重复运行正常完整流程（上传 → 生成方案 → 批准 → 执行 → 完成），记录成功与失败，
不只保留最佳结果。测试中未经批准写入、重复申请和金额不一致均为阻断问题。

用法：
    python scripts/run_stability.py [--runs 10] [--target-success 9]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else str(ROOT / ".venv" / "bin" / "python")


def run_single_flow(run_num):
    """运行一次正常完整流程，返回 (success, error_msg)。"""
    env = os.environ.copy()
    env["RESTOCK_AGENT_MODE"] = "off"
    # 使用测试里的正常流程测试（场景 1：正常任务）
    cmd = [PYTHON, "-m", "pytest", "tests/test_fixed_scenarios.py::test_scenario_01_normal_task",
           "-q", "--tb=short", "-x"]
    result = subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True)
    success = result.returncode == 0
    error_msg = None
    if not success:
        # 提取错误信息（阻断问题：未经批准写入、重复申请、金额不一致）
        error_msg = result.stdout[-500:] if result.stdout else result.stderr[-500:]
        # 检查阻断问题
        if "未经批准" in error_msg or "重复申请" in error_msg or "金额不一致" in error_msg:
            error_msg = f"阻断问题：{error_msg}"
    return success, error_msg


def main():
    parser = argparse.ArgumentParser(description="M4 冻结版本连续稳定性测试")
    parser.add_argument("--runs", type=int, default=10, help="连续运行次数（默认 10）")
    parser.add_argument("--target-success", type=int, default=9, help="目标成功次数（默认 9）")
    args = parser.parse_args()

    print(f"M4 冻结版本连续稳定性测试：连续 {args.runs} 次正常完整流程，目标至少成功 {args.target_success} 次")
    print("="*60)

    successes = 0
    failures = []
    for i in range(1, args.runs + 1):
        print(f"\n运行 {i}/{args.runs}...", end=" ", flush=True)
        success, error_msg = run_single_flow(i)
        if success:
            successes += 1
            print("✅ 成功")
        else:
            failures.append((i, error_msg))
            print(f"❌ 失败：{error_msg[:200] if error_msg else '未知错误'}")

    print("\n" + "="*60)
    print(f"结果：{successes}/{args.runs} 成功，{len(failures)} 失败")
    print("="*60)

    if failures:
        print("\n失败详情：")
        for run_num, error_msg in failures:
            print(f"  运行 {run_num}: {error_msg[:300] if error_msg else '未知错误'}")

    if successes >= args.target_success:
        print(f"\n✅ 达标：{successes}/{args.runs} >= {args.target_success}（内部目标）")
        sys.exit(0)
    else:
        print(f"\n❌ 未达标：{successes}/{args.runs} < {args.target_success}（内部目标）")
        sys.exit(1)


if __name__ == "__main__":
    main()
