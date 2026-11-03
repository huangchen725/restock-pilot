# -*- coding: utf-8 -*-
"""M4 交付包检查脚本——扫描交付包候选文件，确保没有明文密钥或个人运行数据。

检查项：
1. 明文密钥（INTERN_API_KEY、GitHub PAT、端砚 key 等）
2. 个人运行数据（数据库文件、日志、浏览器缓存）
3. 敏感路径（.env、个人配置）

用法：
    python scripts/check_delivery.py [--path .] [--strict]
    --strict：发现任何敏感内容都视为失败（交付前必须为零）
"""

import argparse
import os
import re
import sys
from pathlib import Path

# 敏感模式（明文密钥、个人数据）
SENSITIVE_PATTERNS = [
    (r'INTERN_API_KEY\s*=\s*[\'"][^\'"]{8,}[\'"]', "INTERN_API_KEY 明文"),
    (r'github_pat_[A-Za-z0-9_]{20,}', "GitHub PAT 明文"),
    (r'sk-[A-Za-z0-9]{20,}', "OpenAI/其他 API key 明文"),
    (r'Bearer\s+[A-Za-z0-9_\-\.]{20,}', "Bearer token 明文"),
    (r'password\s*=\s*[\'"][^\'"]{6,}[\'"]', "密码明文"),
]

# 敏感文件/目录（不应在交付包里）
SENSITIVE_PATHS = [
    '.env',  # 真实密钥文件（.env.example 是示例，不算敏感）
    '.env.local',
    'restock.db',
    'procurement.db',
    '.git-broken',
    'outputs/*.log',
    'agent-integration/*.sqlite*',
    'agent-integration/*.jsonl',
]

# 排除路径（正常代码/文档，不算敏感）
EXCLUDE_PATHS = [
    '.venv',
    '.git',
    '__pycache__',
    'node_modules',
    '.pytest_cache',
]


def is_excluded(path, root):
    """检查路径是否在排除列表里。"""
    rel = path.relative_to(root)
    for exclude in EXCLUDE_PATHS:
        if exclude in str(rel) or str(rel).startswith(exclude + os.sep):
            return True
    return False


def scan_file(path, root):
    """扫描单个文件，返回 [(pattern_name, line_num, line), ...]。"""
    findings = []
    try:
        content = path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return findings
    for pattern, name in SENSITIVE_PATTERNS:
        for match in re.finditer(pattern, content, re.IGNORECASE):
            line_num = content[:match.start()].count('\n') + 1
            line = content.split('\n')[line_num - 1].strip()
            # 排除注释和文档说明（# 密钥、// 密钥、"密钥"等）
            if re.search(r'#.*密钥|//.*密钥|示例|示例值|your.*key|填.*key', line, re.IGNORECASE):
                continue
            findings.append((name, line_num, line[:100]))
    return findings


def scan_directory(root):
    """扫描目录，返回所有发现。只检查 git 跟踪的文件（交付包候选文件）。"""
    import subprocess
    # 只检查 git 跟踪的文件（交付包候选文件，.gitignore 里的文件不会提交）
    try:
        result = subprocess.run(['git', 'ls-files'], cwd=root, capture_output=True, text=True)
        tracked_files = {root / line.strip() for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        # 非 git 仓库时扫描所有文件
        tracked_files = {p for p in root.rglob('*') if p.is_file()}
    all_findings = []
    sensitive_files = []
    for path in tracked_files:
        if not path.exists():
            continue
        if is_excluded(path, root):
            continue
        # 检查敏感路径（精确匹配，.env 不匹配 .env.example）
        rel = path.relative_to(root)
        rel_str = str(rel).replace('\\', '/')
        for sensitive in SENSITIVE_PATHS:
            sensitive_clean = sensitive.rstrip('*')
            if sensitive.endswith('*'):
                # 通配符匹配（如 outputs/*.log）
                if rel_str.startswith(sensitive_clean) and path.suffix == sensitive.split('.')[-1]:
                    sensitive_files.append(str(rel))
                    break
            else:
                # 精确匹配（.env 精确匹配，不匹配 .env.example）
                if rel_str == sensitive or rel_str == sensitive_clean:
                    sensitive_files.append(str(rel))
                    break
        # 扫描文件内容
        if path.suffix in ('.py', '.md', '.txt', '.json', '.toml', '.yaml', '.yml', '.csv'):
            findings = scan_file(path, root)
            if findings:
                all_findings.append((str(rel), findings))
    return all_findings, sensitive_files


def main():
    parser = argparse.ArgumentParser(description="M4 交付包检查")
    parser.add_argument("--path", default=".", help="扫描路径（默认当前目录）")
    parser.add_argument("--strict", action="store_true", help="严格模式：发现任何敏感内容都视为失败")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    print(f"M4 交付包检查：扫描 {root}")
    print("="*60)

    findings, sensitive_files = scan_directory(root)

    if findings:
        print(f"\n⚠️  发现 {len(findings)} 个文件包含敏感内容：")
        for file_path, file_findings in findings:
            print(f"\n  文件：{file_path}")
            for name, line_num, line in file_findings:
                print(f"    行 {line_num} [{name}]: {line}")
    else:
        print("\n✅ 未发现明文密钥")

    if sensitive_files:
        print(f"\n⚠️  发现 {len(sensitive_files)} 个敏感文件/路径：")
        for path in sensitive_files:
            print(f"  - {path}")
    else:
        print("\n✅ 未发现敏感文件/路径")

    print("\n" + "="*60)
    if findings or sensitive_files:
        print(f"❌ 发现敏感内容（{len(findings)} 个文件 + {len(sensitive_files)} 个路径）")
        if args.strict:
            sys.exit(1)
    else:
        print("✅ 交付包检查通过（未发现敏感内容）")
    sys.exit(0)


if __name__ == "__main__":
    main()
