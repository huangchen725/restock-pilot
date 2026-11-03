"""Shared evidence primitives for M4 runners; no model requests."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.redaction import redact

FLAGS = ('RUN_REAL_MODEL_E2E', 'RUN_AGENT_BOUNDARY_E2E', 'RUN_PLAYWRIGHT_E2E',
         'RUN_WORKBENCH_E2E', 'RUN_STARTUP_E2E')


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def isolated_env(output, flags=(), mode='off'):
    env = os.environ.copy()
    env.pop('RESTOCK_M4_OUTPUT', None)
    for key in FLAGS:
        env[key] = '1' if key in flags else '0'
    env.update(RESTOCK_AGENT_MODE=mode, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    temp = Path(output).resolve()/'temp'
    temp.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(temp), TMP=str(temp))
    return env


def fingerprint(root=ROOT):
    proc = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, check=True)
    files = {}
    # Includes untracked source and tests: HEAD alone cannot freeze a dirty worktree.
    for folder in ('app', 'ui', 'tests', 'scripts', 'data', 'agent-integration'):
        for path in sorted((root/folder).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.csv','.toml','.yaml','.yml','.json') and not any(
                    p in ('__pycache__', 'node_modules', '.venv', '.codex', 'sessions', 'outputs') for p in path.parts):
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('start.py','runtime-lock.json','requirements.txt','requirements.lock','.python-version','pytest.ini'):
        path = root/name
        if path.exists():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    packages = sorted((d.metadata['Name'].lower(), d.version) for d in importlib.metadata.distributions())
    return {'head': proc.stdout.decode().strip(), 'files': files,
            'python': platform.python_version(), 'executable': sys.executable,
            'packages': packages, 'platform': platform.platform(),
            'runtime_config': {key: os.getenv(key) for key in ('RESTOCK_CODEX_BIN','PLAYWRIGHT_BROWSERS_PATH')}}


def read_junit(path):
    result = {'passed':0,'failed':0,'errors':0,'skipped':0,'ids':[], 'invalid':[]}
    try:
        root = ET.parse(path).getroot()
        for case in root.iter('testcase'):
            props = {p.get('name'):p.get('value') for p in case.findall('./properties/property')}
            node = props.get('nodeid', case.get('classname','')+'::'+case.get('name',''))
            result['ids'].append(node)
            state = ('errors' if case.find('error') is not None else 'failed' if case.find('failure') is not None
                     else 'skipped' if case.find('skipped') is not None else 'passed')
            result[state] += 1
        if not result['ids'] or len(set(result['ids'])) != len(result['ids']):
            result['invalid'].append('Empty or duplicated test identities')
    except (OSError, ET.ParseError):
        result['invalid'].append('Missing or invalid JUnit report')
    return result


def run_pytest(targets, output, env, options=(), timeout=900):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output/"result.xml").exists() or (output/"pytest.log").exists():
        raise ValueError("Refusing to overwrite previous run evidence")
    cmd = [sys.executable, '-m', 'pytest', *targets, '-q', '--tb=short',
           '-p','tests.acceptance_plugin', '--basetemp='+str(output/'pytest-temp'),
           '--junitxml='+str(output/'result.xml'), *options]
    from app.processes import ProcessScope
    scope = None
    code,log = 125,''
    try:
        scope = ProcessScope()
        proc = scope.spawn(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding='utf-8', errors='replace')
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            code, log = proc.returncode, stdout+'\n'+stderr
        except subprocess.TimeoutExpired:
            scope.stop()
            stdout, stderr = proc.communicate(timeout=10)
            code, log = 124, 'pytest process timeout\n'+stdout+'\n'+stderr
    except Exception:
        code,log = 125,log+'\nInfrastructure failure\n'+traceback.format_exc()
    finally:
        if scope is not None:
            try:
                scope.stop()
            except Exception:
                code,log = 125,log+'\nOwned process cleanup failed\n'+traceback.format_exc()
            finally:
                try:
                    scope.close()
                except Exception:
                    code,log = 125,log+'\nOwned process close failed\n'+traceback.format_exc()
    (output/'pytest.log').write_text(redact(log, env), encoding='utf-8')
    result = read_junit(output/'result.xml')
    result.update(exit_code=code, command=cmd)
    save_json(output/'result.json',result)
    return result
