"""Project-local configuration; explicit process environment takes precedence."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def load_environment(root: Path = ROOT) -> None:
    # Never search the caller's working directory or expand ${...} inside keys.
    load_dotenv(root / ".env", override=False, interpolate=False, encoding="utf-8-sig")

    # Installer-owned defaults are separate from user configuration. No global
    # environment/PATH is changed; values apply to this process and its children.
    generated = root / '.restock' / 'environment.json'
    if not generated.exists():
        return
    expected = {'schema': 1, 'RESTOCK_CODEX_BIN': '.venv/codex/node_modules/.bin/codex.cmd',
                'PLAYWRIGHT_BROWSERS_PATH': '.restock/browsers', 'node_directory': '.restock/node'}
    try:
        settings = json.loads(generated.read_text(encoding='utf-8'))
        if settings != expected:
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError('安装环境配置无效：请检查 .restock/environment.json 并重新运行 setup.cmd；原 .env 不需删除。') from None
    for key in ('RESTOCK_CODEX_BIN', 'PLAYWRIGHT_BROWSERS_PATH'):
        if not os.environ.get(key):
            os.environ[key] = str(root / settings[key])
    browser = Path(os.environ['PLAYWRIGHT_BROWSERS_PATH'])
    if not browser.is_absolute():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str((root / browser).resolve())
    node = str((root / settings['node_directory']).resolve())
    if (Path(node) / 'node.exe').is_file():
        entries = os.environ.get('PATH','').split(os.pathsep)
        if node.casefold() not in {entry.casefold() for entry in entries}:
            os.environ['PATH'] = node + os.pathsep + os.environ.get('PATH','')
