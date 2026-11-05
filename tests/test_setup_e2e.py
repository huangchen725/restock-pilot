"""Opt-in real new-machine setup, failure/retry and retained user configuration.
Downloads fixed distributions; never loads the developer's .env or calls a model.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
import pytest
from app.processes import ProcessScope

ROOT=Path(__file__).resolve().parents[1]


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_owned(cmd,cwd,env,log,timeout=1200):
    scope=ProcessScope()
    try:
        proc=scope.spawn([str(x) for x in cmd],cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                         text=True,encoding='utf-8',errors='replace')
        try:text=proc.communicate(timeout=timeout)[0];code=proc.returncode
        except subprocess.TimeoutExpired:
            scope.stop();text=proc.communicate(timeout=10)[0];code=124
    finally:
        try:scope.stop()
        finally:scope.close()
    Path(log).write_text(text,encoding='utf-8')
    return code


@pytest.mark.skipif(os.getenv('RUN_SETUP_E2E')!='1',reason='explicit opt-in downloads and installs standalone runtimes')
@pytest.mark.parametrize('entry',['cmd','window'])
def test_blank_path_install_repeat_failure_recovery_and_product_flow(entry):
    out=Path(os.getenv('RESTOCK_SETUP_E2E_OUTPUT',str(ROOT/'outputs'/('setup-e2e-'+uuid.uuid4().hex[:8])))).resolve()
    out.mkdir(parents=True,exist_ok=False)
    project=out/'项目 A';project.mkdir()
    for name in ('app','ui','scripts','tests','data','.streamlit'):
        shutil.copytree(ROOT/name,project/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.pytest_cache'))
    (project/'agent-integration').mkdir()
    for name in ('config.toml','codex-config.toml','litellm-config.yaml'):
        shutil.copyfile(ROOT/'agent-integration'/name,project/'agent-integration'/name)
    for name in ('setup.cmd','start.cmd','start.py','desktop.py','launcher_control.py','RestockPilot.vbs','安装或修复 RestockPilot.vbs','关闭 RestockPilot.vbs','bootstrap-lock.json','runtime-lock.json','requirements.txt','requirements.lock','.env.example','.python-version','pytest.ini'):
        shutil.copyfile(ROOT/name,project/name)
    files={str(p.relative_to(project)):sha(p) for p in project.rglob('*') if p.is_file()}
    env={k:v for k,v in os.environ.items() if not k.startswith(('RESTOCK_','INTERN_','CODEX_','RUN_','OPENAI_','PYTHON'))}
    env.update(PATH=os.pathsep.join([str(Path(os.environ['SystemRoot'])/'System32'),os.environ['SystemRoot'],
                                    str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0')]),
               PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    assert shutil.which('python',path=env['PATH']) is None and shutil.which('node',path=env['PATH']) is None
    cache=Path(os.getenv('RESTOCK_SETUP_TEST_CACHE',str(project/'.restock/cache'))).resolve()
    command=[project/'setup.cmd','-NoPause','-CacheDirectory',cache]
    if entry=='window':command=[Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',project/'scripts/install_window.ps1','-Unattended','-CacheDirectory',cache]
    history=[]
    def setup(name,expected=0,offline=False):
        before=set((project/'outputs/setup').glob('*/installation.json')) if (project/'outputs/setup').exists() else set()
        current={**env}
        if offline:current.update(HTTP_PROXY='http://127.0.0.1:9',HTTPS_PROXY='http://127.0.0.1:9',ALL_PROXY='http://127.0.0.1:9',NO_PROXY='')
        code=run_owned(command,out,current,out/(name+'.log'))
        assert code==expected,(name,code,(out/(name+'.log')).read_text(encoding='utf-8')[-3000:])
        created=set((project/'outputs/setup').glob('*/installation.json'))-before
        assert len(created)==1
        record=json.loads(created.pop().read_text(encoding='utf-8'))
        history.append({'name':name,'exit':code,'record':record})
        (out/'runs.json').write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding='utf-8')
        return record
    first=setup('first')
    assert first['status']=='complete'
    assert (project/'.env').read_bytes()==(project/'.env.example').read_bytes()
    assert str(project/'.restock/python').lower() in (project/'.venv/pyvenv.cfg').read_text(encoding='utf-8').lower()
    assert 'codex-primary-runtime' not in (project/'.venv/pyvenv.cfg').read_text(encoding='utf-8')
    original='\ufeff# User-owned settings\r\nINTERN_API_KEY=synthetic-install-key\r\nRESTOCK_AGENT_MODE=off\r\nCUSTOM_NOTE=保留原配置\r\n'.encode('utf-8')
    (project/'.env').write_bytes(original)
    repeated=setup('repeat-offline',offline=True)
    assert repeated['status']=='complete' and repeated['env_preserved'] is True
    assert all(s['status']=='reused' for s in repeated['steps'] if s['step'] in ('node','venv','dependencies','cli','browser'))
    assert (project/'.env').read_bytes()==original
    defaults=sha(project/'.restock/environment.json')
    bad=original+b'RESTOCK_CODEX_BIN=missing-explicit-codex.cmd\r\n';(project/'.env').write_bytes(bad)
    rejected=setup('invalid-existing-config',expected=1,offline=True)
    assert rejected['status']=='failed' and rejected['failed_step']=='runtime'
    assert (project/'.env').read_bytes()==bad and sha(project/'.restock/environment.json')==defaults
    (project/'.env').write_bytes(original)  # Simulate the owner correcting the deliberate invalid field.
    recovered=setup('corrected-config',offline=True)
    assert recovered['status']=='complete' and (project/'.env').read_bytes()==original
    assert run_owned([project/'start.cmd','--check','--offline'],out,env,out/'start-check.log',120)==0
    test_env={**env,'RUN_STARTUP_E2E':'1','RUN_AGENT_BOUNDARY_E2E':'1','RUN_WORKBENCH_E2E':'1','RUN_REAL_MODEL_E2E':'0',
              'TEMP':str(out/'tmp'),'TMP':str(out/'tmp')}
    (out/'tmp').mkdir()
    targets=['tests/test_startup.py','tests/test_runtime_versions.py','tests/test_agent_runtime.py','tests/test_workbench_e2e.py','tests/test_runner.py']
    cmd=[project/'.venv/Scripts/python.exe','-m','pytest',*targets,'-q','--tb=short',
         '--basetemp='+str(out/'product-temp'),'--junitxml='+str(out/'product.xml')]
    assert run_owned(cmd,project,test_env,out/'product.log',600)==0,(out/'product.log').read_text(encoding='utf-8')[-4000:]
    cases=list(ET.parse(out/'product.xml').getroot().iter('testcase'))
    assert cases and not any(c.find('failure') is not None or c.find('error') is not None or c.find('skipped') is not None for c in cases)
    assert (project/'.env').read_bytes()==original
    assert all(sha(project/name)==value for name,value in files.items())
    report={'entry':entry,'passed':True,'scope':'same-machine, blank PATH and fresh project; not physical other-machine acceptance',
            'product_passed':len(cases),'product_failed':0,'product_skipped':0,'config_preserved':True,'source_sha256':files,
            'runs':[{'name':r['name'],'exit':r['exit'],'status':r['record']['status']} for r in history]}
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
