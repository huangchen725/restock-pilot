"""A single checked runtime baseline; no silent patch-version fallback."""
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def check_runtime(env, include_cli=True):
    baseline = json.loads((ROOT/'runtime-lock.json').read_text(encoding='utf-8'))
    if (sys.platform != baseline['platform'] or platform.machine().upper() != baseline['machine'].upper()
            or platform.python_version() != baseline['python']):
        raise ValueError('运行环境不符合 runtime-lock.json 的 Windows x64 / Python '+baseline['python']+' 验证基线')
    for line in (ROOT/'requirements.lock').read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        name, expected = line.split('==')
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise ValueError(f'缺少锁定依赖 {name}；请安装 requirements.lock') from None
        if actual != expected:
            raise ValueError(f'{name} 版本 {actual} 不匹配锁定版本 {expected}；请安装 requirements.lock')
    import playwright
    package = Path(playwright.__file__).parent/'driver/package/browsers.json'
    browsers = json.loads(package.read_text(encoding='utf-8'))['browsers']
    chromium = next(b for b in browsers if b['name']=='chromium')
    if chromium['revision'] != baseline['chromium_revision'] or chromium['browserVersion'] != baseline['chromium_version']:
        raise ValueError('Chromium 版本与 runtime-lock.json 不一致')
    # Validate the executable actually selected for the product's headless mode;
    # Playwright's generic executable_path points to full Chrome even when only
    # the supported headless-shell distribution is installed.
    from playwright.sync_api import sync_playwright, Error
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=env.get('PROCUREMENT_HEADLESS','true').lower()!='false',
                proxy={'server':'per-context'},args=['--no-proxy-server'])
            try:
                if browser.version != baseline['chromium_version']:
                    raise ValueError('实际 Chromium 版本不匹配 runtime-lock.json')
            finally:
                browser.close()
    except Error:
        raise ValueError('缺少或无法启动锁定 Chromium；请用当前解释器执行 -m playwright install chromium，并检查 PLAYWRIGHT_BROWSERS_PATH') from None
    if include_cli:
        from app.runner import child_environment, resolve_codex_bin
        executable = resolve_codex_bin(str(ROOT))
        result = subprocess.run([executable,'--version'],capture_output=True,text=True,
            timeout=10,env=child_environment(), **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        if result.returncode or result.stdout.strip() != 'codex-cli '+baseline['codex']:
            raise ValueError('Codex CLI 版本与 runtime-lock.json 不一致；请配置匹配版本的 RESTOCK_CODEX_BIN')
    return baseline
