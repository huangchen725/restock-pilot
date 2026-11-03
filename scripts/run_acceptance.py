# -*- coding: utf-8 -*-
"""M4 分层验收测试运行脚本——正式验收必须显式启用浏览器测试，不能以 skipped 计为通过。

分层：
1. 普通测试（不依赖浏览器/真实模型）
2. 浏览器测试（Playwright E2E + 故障注入，需 RUN_PLAYWRIGHT_E2E=1）
3. 工作台 E2E（Streamlit UI，需 RUN_WORKBENCH_E2E=1）
4. Agent 边界（本地假模型，需 RUN_AGENT_BOUNDARY_E2E=1）
5. 真实模型（需 RUN_REAL_MODEL_E2E=1 + INTERN_API_KEY，单独运行，不消耗额度时不启用）

用法：
    python scripts/run_acceptance.py [--layers 1,2,3,4] [--strict]
    --strict：任何 skipped 都视为失败（正式验收模式，不能以 skipped 计为通过）
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else str(ROOT / ".venv" / "bin" / "python")

LAYERS = {
    1: ("普通测试", "tests/", {}),
    2: ("浏览器测试", "tests/test_playwright_e2e.py tests/test_fault_injection.py", {"RUN_PLAYWRIGHT_E2E": "1"}),
    3: ("工作台 E2E", "tests/test_workbench_e2e.py", {"RUN_WORKBENCH_E2E": "1"}),
    4: ("Agent 边界", "tests/test_agent_runtime.py", {"RUN_AGENT_BOUNDARY_E2E": "1"}),
    5: ("真实模型", "tests/test_real_m2.py", {"RUN_REAL_MODEL_E2E": "1"}),
}


def run_layer(num, name, targets, env_extra, strict):
    """运行一层测试，返回 (passed, failed, skipped)。"""
    env = os.environ.copy()
    env.update(env_extra)
    cmd = [PYTHON, "-m", "pytest"] + targets.split() + ["-q", "--tb=short"]
    print(f"\n{'='*60}")
    print(f"Layer {num}: {name}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True)
    print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr[-500:], file=sys.stderr)
    # 解析测试结果（pytest 输出格式："267 passed, 25 skipped in 40.31s"）
    import re
    passed = failed = skipped = 0
    for line in result.stdout.splitlines():
        m = re.search(r'(\d+) passed', line)
        if m:
            passed = int(m.group(1))
        m = re.search(r'(\d+) failed', line)
        if m:
            failed = int(m.group(1))
        m = re.search(r'(\d+) skipped', line)
        if m:
            skipped = int(m.group(1))
    if strict and skipped > 0:
        print(f"⚠️  Layer {num} 有 {skipped} 个 skipped（严格模式：skipped 不算通过）")
        failed += 1  # 严格模式下 skipped 视为失败
    return passed, failed, skipped, result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="M4 分层验收测试运行")
    parser.add_argument("--layers", default="1,2,3,4", help="运行的层（逗号分隔，默认 1,2,3,4）")
    parser.add_argument("--strict", action="store_true", help="严格模式：任何 skipped 都视为失败")
    args = parser.parse_args()

    layers = [int(x) for x in args.layers.split(",")]
    total_passed = total_failed = total_skipped = 0
    all_ok = True

    for num in layers:
        if num not in LAYERS:
            print(f"警告：Layer {num} 不存在，跳过")
            continue
        name, targets, env_extra = LAYERS[num]
        passed, failed, skipped, ok = run_layer(num, name, targets, env_extra, args.strict)
        total_passed += passed
        total_failed += failed
        total_skipped += skipped
        if not ok or (args.strict and skipped > 0):
            all_ok = False

    print(f"\n{'='*60}")
    print(f"总计：{total_passed} passed, {total_failed} failed, {total_skipped} skipped")
    print(f"{'='*60}")
    if args.strict and total_skipped > 0:
        print(f"⚠️  严格模式：{total_skipped} 个 skipped 不算通过")
        sys.exit(1)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
