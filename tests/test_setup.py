"""New-machine installer contracts; all credentials in controls are synthetic."""
from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]

def test_setup_entrypoints_exist_without_python_prerequisite():
    assert (ROOT/'setup.cmd').is_file(), 'ENV-01: missing no-Python bootstrap entry'
    assert (ROOT/'scripts/setup.ps1').is_file()
    assert (ROOT/'scripts/install_environment.py').is_file()

def test_bootstrap_artifacts_are_locked_and_match_runtime():
    path=ROOT/'bootstrap-lock.json'
    assert path.is_file(), 'ENV-02: no independent pinned Python distribution'
    data=json.loads(path.read_text(encoding='utf-8'))
    runtime=json.loads((ROOT/'runtime-lock.json').read_text(encoding='utf-8'))
    assert data['python']['version']==runtime['python']
    for item in (data['python'],data['node']):
        assert item['url'].startswith('https://') and len(item['sha256'])==64

import copy,hashlib,importlib.util,io,os,subprocess,sys,urllib.error,zipfile
import pytest

def module():
    spec=importlib.util.spec_from_file_location('restock_installer',ROOT/'scripts/install_environment.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

@pytest.fixture
def project(tmp_path):
    root=tmp_path/'项目 with spaces';root.mkdir()
    for file in ('bootstrap-lock.json','runtime-lock.json','requirements.lock','requirements.txt','.env.example','setup.cmd','start.cmd','scripts/setup.ps1','scripts/install_environment.py','app/processes.py'):
        target=root/file;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/file).read_bytes())
    return root

@pytest.mark.parametrize('original',[b'',b'INTERN_API_KEY=synthetic-secret\r\nCUSTOM=retain\r\n',b'\xef\xbb\xbfCUSTOM=value\n'])
def test_existing_env_preserved_byte_for_byte(project,original):
    m=module();(project/'.env').write_bytes(original)
    assert m.preserve_env(project)=='preserved'
    assert (project/'.env').read_bytes()==original
    assert m.preserve_env(project)=='preserved'
    assert (project/'.env').read_bytes()==original


def test_missing_env_created_once_from_example(project):
    m=module();assert m.preserve_env(project)=='created'
    original=(project/'.env').read_bytes();assert original==(project/'.env.example').read_bytes()
    assert m.preserve_env(project)=='preserved' and (project/'.env').read_bytes()==original


def test_bad_archive_hash_never_extracts_or_executes(tmp_path,monkeypatch):
    m=module();data=b'good pinned content'
    item={'filename':'node.zip','url':'https://nodejs.org/artifact','sha256':hashlib.sha256(data).hexdigest()}
    (tmp_path/'node.zip').write_bytes(b'corrupt')
    monkeypatch.setattr(m.urllib.request,'urlopen',lambda *a,**k:pytest.fail('must reject the bad existing cache'))
    with pytest.raises(m.InstallError,match='checksum'):m.download(item,tmp_path)


def test_download_positive_cache_and_network_failure(tmp_path,monkeypatch):
    m=module();data=b'known synthetic bytes';calls=[]
    item={'filename':'artifact.zip','url':'https://nodejs.org/artifact','sha256':hashlib.sha256(data).hexdigest()}
    class Response(io.BytesIO):
        def geturl(self):return item['url']
    def fetch(*a,**k):calls.append(1);return Response(data)
    monkeypatch.setattr(m.urllib.request,'urlopen',fetch)
    assert m.download(item,tmp_path).read_bytes()==data
    assert m.download(item,tmp_path).read_bytes()==data and len(calls)==1
    item={**item,'filename':'another.zip'}
    def fail(*a,**k):raise urllib.error.URLError('synthetic-private-value')
    monkeypatch.setattr(m.urllib.request,'urlopen',fail)
    with pytest.raises(m.InstallError,match='download failed') as exc:m.download(item,tmp_path)
    assert 'synthetic-private-value' not in str(exc.value)
    assert not list(tmp_path.glob('*.partial-*')) and not (tmp_path/'another.zip').exists()


@pytest.mark.parametrize('name',['../escape','/absolute','node/root/../../escape','C:/escape'])
def test_zip_escape_rejected(tmp_path,name):
    m=module();archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr(name,'malicious')
    with pytest.raises(m.InstallError,match='Unsafe'):m.extract_node(archive,tmp_path/'stage','node')
    assert not (tmp_path/'escape').exists()


def test_node_zip_positive_and_duplicate_rejected(tmp_path):
    m=module();archive=tmp_path/'node.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr('node/node.exe',b'fixture')
    assert (m.extract_node(archive,tmp_path/'stage','node')/'node.exe').read_bytes()==b'fixture'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('node/A','a');z.writestr('node/a','b')
    with pytest.raises(m.InstallError,match='Duplicate'):m.extract_node(archive,tmp_path/'stage2','node')


def test_install_lock_refuses_concurrent_owner_then_releases(tmp_path):
    m=module();path=tmp_path/'install.lock'
    with m.install_guard(path):
        with pytest.raises(m.InstallError,match='Another installation'):
            with m.install_guard(path):pytest.fail('second installation entered')
    with m.install_guard(path):assert path.exists()


def test_foreign_or_incompatible_venv_is_preserved(project):
    m=module();instance=m.Installer(project,project/'.restock/cache',project/'outputs/check')
    venv=project/'.venv';venv.mkdir();(venv/'keep.txt').write_text('user content')
    with pytest.raises(m.InstallError,match='preserved'):instance.install_venv()
    assert (venv/'keep.txt').read_text()=='user content'


def test_real_subprocess_failure_is_logged_and_secret_redacted(project):
    m=module();instance=m.Installer(project,project/'.restock/cache',project/'outputs/check')
    secret='synthetic-'+'setup-private-value'
    instance.env['SYNTHETIC_API_KEY']=secret
    with pytest.raises(m.InstallError,match='exit 7'):
        instance.command('failure',[sys.executable,'-c','import os;print(os.environ["SYNTHETIC_API_KEY"]);raise SystemExit(7)'])
    text=(instance.run/'failure.log').read_text(encoding='utf-8')
    assert secret not in text and '[REDACTED]' in text


def test_failure_does_not_publish_defaults_or_touch_existing_env(project,monkeypatch):
    m=module();original=b'CUSTOM=keep\nINTERN_API_KEY=synthetic\n';(project/'.env').write_bytes(original)
    instance=m.Installer(project,project/'.restock/cache',project/'outputs/failed')
    for name in ('validate_manifest','install_node','install_venv'):
        monkeypatch.setattr(instance,name,lambda:'reused')
    def fail():raise m.InstallError('synthetic dependency failure')
    monkeypatch.setattr(instance,'install_packages',fail)
    assert instance.execute()==1
    result=json.loads((instance.run/'installation.json').read_text(encoding='utf-8'))
    assert result['status']=='failed' and result['failed_step']=='dependencies'
    assert (project/'.env').read_bytes()==original and not (project/'.restock/environment.json').exists()


def test_managed_defaults_precedence_and_no_config_write(project,monkeypatch):
    from app.config import load_environment
    settings={'schema':1,'RESTOCK_CODEX_BIN':'.venv/codex/node_modules/.bin/codex.cmd','PLAYWRIGHT_BROWSERS_PATH':'.restock/browsers','node_directory':'.restock/node'}
    (project/'.restock/node').mkdir(parents=True);(project/'.restock/node/node.exe').write_bytes(b'fixture')
    (project/'.restock/environment.json').write_text(json.dumps(settings))
    original=b'RESTOCK_CODEX_BIN=custom-cli.cmd\r\nCUSTOM=keep\r\n';(project/'.env').write_bytes(original)
    monkeypatch.delenv('RESTOCK_CODEX_BIN',raising=False);monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH',raising=False)
    monkeypatch.setenv('PATH','prior-path');monkeypatch.setenv('CUSTOM','process-wins')
    load_environment(project)
    assert os.environ['RESTOCK_CODEX_BIN']=='custom-cli.cmd'
    assert os.environ['PLAYWRIGHT_BROWSERS_PATH']==str(project/'.restock/browsers')
    assert os.environ['CUSTOM']=='process-wins' and (project/'.env').read_bytes()==original
    monkeypatch.setenv('RESTOCK_CODEX_BIN','process-cli');load_environment(project)
    assert os.environ['RESTOCK_CODEX_BIN']=='process-cli'
    assert os.environ['PATH'].split(os.pathsep).count(str(project/'.restock/node'))==1


def test_invalid_managed_defaults_fail_without_secret_echo(project):
    from app.config import load_environment
    (project/'.restock').mkdir();(project/'.restock/environment.json').write_text('{"secret":"synthetic-sensitive"}')
    with pytest.raises(ValueError) as exc:load_environment(project)
    assert 'synthetic-sensitive' not in str(exc.value)


def test_new_generated_defaults_do_not_override_explicit_bad_cli(project,monkeypatch):
    m=module();instance=m.Installer(project,project/'.restock/cache',project/'outputs/check')
    instance.defaults={'schema':1};instance.effective={'RESTOCK_CODEX_BIN':'missing-explicit.cmd'}
    observed={}
    def command(label,args,env,**kwargs):
        observed.update(env);raise m.InstallError('CLI unavailable')
    monkeypatch.setattr(instance,'command',command)
    with pytest.raises(m.InstallError):instance.verify()
    assert observed['RESTOCK_CODEX_BIN']=='missing-explicit.cmd' and not (project/'.restock/environment.json').exists()


def test_cmd_bootstrap_rejects_corrupt_cached_python_under_restricted_policy(project):
    cache=project/'cache';cache.mkdir()
    manifest=json.loads((project/'bootstrap-lock.json').read_text(encoding='utf-8-sig'))
    (cache/manifest['python']['filename']).write_bytes(b'corrupt archive')
    original=b'CUSTOM=existing\r\n';(project/'.env').write_bytes(original)
    r=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Restricted','-Command',
                      '& $env:RESTOCK_TEST_SETUP -NoPause -CacheDirectory $env:RESTOCK_TEST_CACHE'],
                      env={**os.environ,'RESTOCK_TEST_SETUP':str(project/'setup.cmd'),'RESTOCK_TEST_CACHE':str(cache)},
                      capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=40)
    assert r.returncode!=0
    reports=list((project/'outputs/setup').glob('*/bootstrap-error.json'))
    assert len(reports)==1,r.stdout+r.stderr
    report=json.loads(reports[0].read_text())
    assert report['stage']=='python-distribution' and 'checksum' in report['hint']
    assert not (project/'.restock/python').exists() and (project/'.env').read_bytes()==original


def test_failed_step_can_resume_without_losing_previous_configuration(project,monkeypatch):
    m=module();original=b'INTERN_API_KEY=synthetic\nUSER_CHOICE=keep\n';(project/'.env').write_bytes(original)
    installed=set()
    def build(run,fail):
        instance=m.Installer(project,project/'.restock/cache',project/'outputs'/run)
        for name in ('validate_manifest','install_node','install_venv','configure','install_cli','install_browser','verify'):
            def action(n=name):
                state='reused' if n in installed else 'installed';installed.add(n);return state
            monkeypatch.setattr(instance,name,action)
        def deps():
            if fail:raise m.InstallError('download unavailable')
            installed.add('dependencies');return 'installed'
        monkeypatch.setattr(instance,'install_packages',deps)
        return instance
    failed=build('failed',True);assert failed.execute()==1
    recovered=build('recovered',False);assert recovered.execute()==0
    assert all(s['status']=='reused' for s in recovered.steps[:3])
    assert (project/'.env').read_bytes()==original
    assert json.loads((failed.run/'installation.json').read_text())['status']=='failed'


def test_existing_wrong_version_node_is_rejected_before_npm(project,monkeypatch):
    m=module();instance=m.Installer(project,project/'.restock/cache',project/'outputs/check')
    instance.node.mkdir(parents=True);(instance.node/'node.exe').write_bytes(b'fixture')
    m.save_json(instance.node/'.restock-receipt.json',{'archive_sha256':instance.manifest['node']['sha256'],'executable_sha256':m.digest(instance.node/'node.exe')})
    monkeypatch.setattr(instance,'probe',lambda *a,**k:(0,'v0.0.0'))
    with pytest.raises(m.InstallError,match='locked version'):instance.install_node()
    assert (instance.node/'node.exe').read_bytes()==b'fixture'


def test_runtime_artifacts_are_blocked_from_delivery(tmp_path):
    from scripts.check_delivery import scan
    (tmp_path/'.restock').mkdir();(tmp_path/'.restock/runtime.bin').write_bytes(b'local runtime')
    assert scan(tmp_path)['exit_code']==1


def test_missing_template_does_not_leave_partial_config(project):
    m=module();(project/'.env.example').unlink()
    with pytest.raises(FileNotFoundError):m.preserve_env(project)
    assert not (project/'.env').exists()


def test_dependency_names_use_standard_normalization(project,monkeypatch):
    m=module();(project/'requirements.lock').write_text('Some_Package==1.0\n',encoding='utf-8')
    instance=m.Installer(project,project/'.restock/cache',project/'outputs/check')
    def probe(cmd,*a,**k):
        return (0,'') if 'check' in cmd else (0,json.dumps({'some-package':'1.0'}))
    monkeypatch.setattr(instance,'probe',probe)
    assert instance.packages_match()


def test_nonfile_configuration_is_not_silently_accepted(project):
    m=module();(project/'.env').mkdir()
    with pytest.raises(m.InstallError,match='regular file'):m.preserve_env(project)
    assert (project/'.env').is_dir()


def test_paths_outside_project_are_rejected_without_mutation(project):
    m=module()
    with pytest.raises(m.InstallError,match='inside'):m.local_path(project,project/'../external')
    assert not (project.parent/'external').exists()


def test_installer_logging_keeps_known_boolean_switch_and_redacts_real_secret():
    m=module()
    assert m.safe_text('version 1.0',{'CODEBUDDY_API_KEY_HELPER_DISABLED':'1'})=='version 1.0'
    assert 'private' not in m.safe_text('key=private',{'INTERN_API_KEY':'private'})
    assert 'user:password' not in m.safe_text('https://user:password@example.invalid/a',{})
