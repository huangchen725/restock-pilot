"""Windows project environment installer. Runs on standalone Python, stdlib only.
No model calls, global installs, configuration rewrites or destructive directory cleanup.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid
import zipfile

class InstallError(Exception):
    pass


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def save_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.partial-'+uuid.uuid4().hex)
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,path)


def local_path(root,path):
    root=Path(root).resolve();path=Path(path)
    if not path.is_absolute():path=root/path
    resolved=path.resolve()
    if not resolved.is_relative_to(root) or resolved==root:
        raise InstallError('Installer target must remain inside the project.')
    for candidate in [path,*path.parents]:
        if candidate==root:break
        if candidate.is_symlink() or (hasattr(candidate,'is_junction') and candidate.is_junction()):
            raise InstallError('Installer targets cannot contain links or junctions.')
    return resolved


def safe_text(text,values):
    # Share the established secret-field contract without importing app.__init__
    # (dotenv is not installed yet during bootstrap).
    import importlib.util
    spec=importlib.util.spec_from_file_location('installer_redaction',Path(__file__).resolve().parents[1]/'app/redaction.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    text=re.sub(r'(?i)(https?://)[^/\s:@]+:[^/\s@]+@',r'\1[REDACTED]@',text)
    return module.redact(text,values)


@contextmanager
def install_guard(path):
    import msvcrt
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open('a+b')
    try:
        if not path.stat().st_size:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise InstallError('Another installation is running; wait and retry.') from None
        try:yield
        finally:stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
    finally:stream.close()


def download(item,cache):
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    if Path(item['filename']).name!=item['filename'] or not re.fullmatch('[0-9a-f]{64}',item['sha256']):
        raise InstallError('Invalid distribution manifest.')
    if not item['url'].startswith('https://'):
        raise InstallError('Distribution downloads require HTTPS.')
    target=cache/item['filename']
    if target.exists():
        if digest(target)!=item['sha256']:
            raise InstallError('Cached distribution checksum mismatch; remove this cached archive and retry: '+item['filename'])
        return target
    partial=cache/(item['filename']+'.partial-'+uuid.uuid4().hex)
    try:
        request=urllib.request.Request(item['url'],headers={'User-Agent':'RestockPilot-setup'})
        with urllib.request.urlopen(request,timeout=90) as response,partial.open('xb') as output:
            if not response.geturl().startswith('https://'):raise InstallError('Insecure distribution redirect rejected.')
            while chunk:=response.read(1024*1024):output.write(chunk)
        if digest(partial)!=item['sha256']:raise InstallError('Downloaded distribution checksum mismatch.')
        os.replace(partial,target)
        return target
    except (OSError,ValueError):
        raise InstallError('Distribution download failed; check network/proxy and retry.') from None
    finally:
        if partial.exists():partial.unlink()  # Only this attempt's exact partial file.


def extract_node(archive,stage,archive_root):
    stage=Path(stage);stage.mkdir(parents=True,exist_ok=False)
    with zipfile.ZipFile(archive) as package:
        seen=set()
        for item in package.infolist():
            name=item.filename.replace('\\','/')
            parts=name.split('/')
            if parts[0]!=archive_root or any(p=='..' or ':' in p for p in parts) or name.startswith('/'):
                raise InstallError('Unsafe Node archive member.')
            key=name.casefold().rstrip('/')
            if key in seen or (item.external_attr>>16)&0o170000==0o120000:
                raise InstallError('Duplicate or symbolic Node archive member.')
            seen.add(key)
            target=local_path(stage,stage/name)
            if item.is_dir():target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with package.open(item) as src,target.open('xb') as dst:
                    while data:=src.read(1024*1024):dst.write(data)
    return stage/archive_root


def preserve_env(root):
    """Exclusive creation: no race can replace an existing .env, including empty files."""
    target=local_path(root,Path(root)/'.env')
    if target.exists():
        if not target.is_file():raise InstallError('Existing .env is not a regular file; preserved.')
        return 'preserved'
    template=(Path(root)/'.env.example').read_bytes()
    try:
        with target.open('xb') as stream:stream.write(template)
        return 'created'
    except FileExistsError:return 'preserved'


class Installer:
    def __init__(self,root,cache,run):
        self.root=Path(root).resolve();self.cache=Path(cache).resolve()
        self.managed=local_path(self.root,'.restock');self.run=local_path(self.root,run)
        self.run.mkdir(parents=True,exist_ok=True)
        self.manifest=json.loads((self.root/'bootstrap-lock.json').read_text(encoding='utf-8-sig'))
        self.runtime=json.loads((self.root/'runtime-lock.json').read_text(encoding='utf-8-sig'))
        self.venv=local_path(self.root,'.venv');self.python=self.venv/'Scripts/python.exe'
        self.node=local_path(self.root,'.restock/node')
        self.secrets=dict(os.environ)
        allowed={'PATH','PATHEXT','SYSTEMROOT','SYSTEMDRIVE','WINDIR','COMSPEC','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA',
                 'PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS',
                 'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE'}
        self.env={k:v for k,v in os.environ.items() if k.upper() in allowed}
        self.env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PIP_CONFIG_FILE=os.devnull,
                        PIP_DISABLE_PIP_VERSION_CHECK='1',PIP_NO_INPUT='1',PIP_CACHE_DIR=str(self.cache/'pip'),npm_config_userconfig=os.devnull,
                        npm_config_cache=str(self.cache/'npm'))
        self.steps=[];self.stage='manifest';self.defaults=None
        self.before_env=digest(self.root/'.env') if (self.root/'.env').is_file() else None
        self.result={'schema':1,'status':'running','steps':self.steps,'base_python':sys.version.split()[0],
                     'requirements_sha256':digest(self.root/'requirements.lock'),'runtime_sha256':digest(self.root/'runtime-lock.json'),
                     'bootstrap_sha256':digest(self.root/'bootstrap-lock.json'),
                     'installer_sha256':{name:digest(self.root/name) for name in ('setup.cmd','scripts/setup.ps1','scripts/install_environment.py')},
                     'existing_env_sha256_before':self.before_env}

    def probe(self,cmd,env=None):
        try:
            result=subprocess.run([str(x) for x in cmd],cwd=self.root,env=env or self.env,capture_output=True,text=True,
                encoding='utf-8',errors='replace',timeout=40,creationflags=subprocess.CREATE_NO_WINDOW)
            return result.returncode,result.stdout.strip()
        except (OSError,subprocess.TimeoutExpired):return 125,''

    def command(self,label,cmd,env=None,timeout=900):
        # The owned tree is available before pip via its source module, not app.__init__.
        import importlib.util
        spec=importlib.util.spec_from_file_location('installer_processes',self.root/'app/processes.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        # Bind before executing any installer code. NtResumeProcess resumes the
        # process created suspended, without depending on an installed psutil.
        job=module.WindowsJob();proc=None
        try:
            proc=subprocess.Popen([str(x) for x in cmd],cwd=self.root,env=env or self.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW | 4)
            try:
                job.assign(proc)
                import ctypes
                resume=ctypes.WinDLL('ntdll').NtResumeProcess
                resume.argtypes=[ctypes.c_void_p];resume.restype=ctypes.c_long
                if resume(int(proc._handle))!=0:raise OSError('Cannot resume owned installer process')
            except BaseException:
                proc.kill();proc.wait();raise
            try:log=proc.communicate(timeout=timeout)[0];code=proc.returncode
            except subprocess.TimeoutExpired:
                job.stop();log=proc.communicate(timeout=10)[0];code=124
        finally:
            try:job.stop()
            finally:job.close()
        (self.run/(label+'.log')).write_text(safe_text(log,{**self.secrets,**(env or self.env)}),encoding='utf-8')
        if code:raise InstallError(label+' failed (exit '+str(code)+'); see its redacted log and retry setup.')

    def step(self,name,action):
        self.stage=name;print('Preparing '+name+'...',flush=True);started=time.monotonic()
        status=action()
        self.steps.append({'step':name,'status':status or 'verified','seconds':round(time.monotonic()-started,2)})
        save_json(self.run/'installation.json',self.result)
        print(name+': '+(status or 'verified'),flush=True)

    def validate_manifest(self):
        if self.manifest['schema']!=1 or self.manifest['python']['version']!=self.runtime['python'] or sys.version.split()[0]!=self.runtime['python']:
            raise InstallError('Bootstrap Python does not match runtime-lock.json.')
        item=self.manifest['node']
        if not item['url'].startswith('https://nodejs.org/dist/') or not re.fullmatch(r'\d+\.\d+\.\d+',item['version']):
            raise InstallError('Invalid pinned Node distribution.')

    def install_node(self):
        item=self.manifest['node'];receipt=self.node/'.restock-receipt.json'
        if self.node.exists():
            try:data=json.loads(receipt.read_text(encoding='utf-8'))
            except (OSError,ValueError):raise InstallError('Existing .restock/node has no verified receipt; preserved.') from None
            if data.get('archive_sha256')!=item['sha256'] or digest(self.node/'node.exe')!=data.get('executable_sha256'):
                raise InstallError('Existing Node integrity mismatch; preserved.')
            status='reused'
        else:
            archive=download(item,self.cache)
            stage=local_path(self.root,self.managed/'stage'/('node-'+uuid.uuid4().hex))
            candidate=extract_node(archive,stage,item['archive_root'])
            # Publish verified archive bytes before starting node.exe. Windows
            # may retain executable image handles briefly after --version exits,
            # preventing its parent directory from being renamed. Version is
            # still verified below, before PATH use or any npm execution.
            save_json(candidate/'.restock-receipt.json',{'archive_sha256':item['sha256'],'executable_sha256':digest(candidate/'node.exe')})
            candidate.rename(self.node);status='installed'
        code,version=self.probe([self.node/'node.exe','--version'])
        if code or version!='v'+item['version']:raise InstallError('Node cannot start with its locked version.')
        self.env['PATH']=str(self.node)+os.pathsep+self.env.get('PATH','')
        return status

    def install_venv(self):
        marker=self.managed/'venv-owner.json'
        if not self.venv.exists():save_json(marker,{'path':'.venv','created_by':'RestockPilot setup'})
        elif not self.python.exists() and not marker.exists():
            raise InstallError('Existing .venv is incomplete and not owned by setup; preserved. Use a new project directory.')
        if self.python.exists():
            code,version=self.probe([self.python,'-I','-c','import platform;print(platform.python_version())'])
            if code or version!=self.runtime['python']:
                raise InstallError('Existing .venv Python is incompatible; preserved. Use a new project directory.')
            return 'reused'
        self.command('venv',[sys.executable,'-I','-m','venv',self.venv])
        if self.probe([self.python,'-I','-c','import platform;print(platform.python_version())'])!=(0,self.runtime['python']):
            raise InstallError('Virtual environment creation did not produce the locked Python.')
        return 'installed'

    def packages_match(self):
        code,text=self.probe([self.python,'-I','-c',"import importlib.metadata,json;print(json.dumps({d.metadata['Name'].lower():d.version for d in importlib.metadata.distributions()}))"])
        if code:return False
        actual={re.sub(r'[-_.]+','-',key.lower()):value for key,value in json.loads(text).items()}
        for line in (self.root/'requirements.lock').read_text(encoding='utf-8').splitlines():
            if not line or line.startswith('#'):continue
            name,version=line.split('==')
            if actual.get(re.sub(r'[-_.]+','-',name.lower()))!=version:return False
        return self.probe([self.python,'-I','-m','pip','check'])[0]==0

    def install_packages(self):
        if self.packages_match():return 'reused'
        self.command('pip',[self.python,'-I','-m','pip','install','--index-url','https://pypi.org/simple','-r',self.root/'requirements.txt'])
        self.command('pip-check',[self.python,'-I','-m','pip','check'])
        if not self.packages_match():raise InstallError('Installed dependencies do not match requirements.lock.')
        return 'installed'

    def configure(self):
        state=preserve_env(self.root)
        code,text=self.probe([self.python,'-I','-c',"from dotenv import dotenv_values;import json,sys;print(json.dumps(dotenv_values(sys.argv[1],interpolate=False,encoding='utf-8-sig')))",self.root/'.env'])
        if code:raise InstallError('Cannot read existing .env; preserved.')
        saved=json.loads(text);self.secrets.update(saved)
        # Same precedence as app.config: process > .env > generated installation defaults.
        values={**{k:v for k,v in saved.items() if v is not None},**os.environ}
        self.effective={key:values.get(key) for key in ('RESTOCK_CODEX_BIN','PLAYWRIGHT_BROWSERS_PATH')}
        self.defaults={'schema':1,'RESTOCK_CODEX_BIN':'.venv/codex/node_modules/.bin/codex.cmd',
                       'PLAYWRIGHT_BROWSERS_PATH':'.restock/browsers','node_directory':'.restock/node'}
        return state

    def install_cli(self):
        # Explicit custom configuration is verified later, never silently replaced.
        shim=local_path(self.root,'.venv/codex/node_modules/.bin/codex.cmd')
        if self.probe([shim,'--version'])==(0,'codex-cli '+self.runtime['codex']):return 'reused'
        prefix=local_path(self.root,'.venv/codex')
        self.command('cli',[self.node/'npm.cmd','install','--prefix',prefix,'@openai/codex@'+self.runtime['codex'],
                            '--no-audit','--no-fund','--registry=https://registry.npmjs.org'])
        if self.probe([shim,'--version'])!=(0,'codex-cli '+self.runtime['codex']):raise InstallError('Installed CLI version mismatch.')
        return 'installed'

    def browser_ready(self,env):
        expected=self.runtime['chromium_version']
        code,text=self.probe([self.python,'-I','-c',"from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n b=p.chromium.launch(headless=True,args=['--no-proxy-server']);print(b.version);b.close()"],env)
        return code==0 and text==expected

    def install_browser(self):
        target=self.effective['PLAYWRIGHT_BROWSERS_PATH'] or self.defaults['PLAYWRIGHT_BROWSERS_PATH']
        path=Path(target)
        if not path.is_absolute():path=self.root/path
        path=path.resolve()
        self.env['PLAYWRIGHT_BROWSERS_PATH']=str(path)
        if self.browser_ready(self.env):return 'reused'
        # Do not populate another user's/global cache because of inherited config.
        local_path(self.root,path)
        self.command('browser',[self.python,'-I','-m','playwright','install','chromium'],self.env)
        if not self.browser_ready(self.env):raise InstallError('Installed browser cannot start with the locked version.')
        return 'installed'

    def verify(self):
        env={**self.env,'RESTOCK_CODEX_BIN':self.effective['RESTOCK_CODEX_BIN'] or self.defaults['RESTOCK_CODEX_BIN']}
        # Install completion does not require or validate a real API key; all checks
        # are local. Explicit invalid CLI configuration remains a visible failure.
        script="import sys;sys.path.insert(0,sys.argv[1]);from app.runtime_versions import check_runtime;check_runtime({},include_cli=True);print('Runtime verified')"
        self.command('runtime-check',[self.python,'-I','-c',script,self.root],env,timeout=120)
        if self.before_env is not None and digest(self.root/'.env')!=self.before_env:
            raise InstallError('Existing .env changed during installation; refusing to report completion.')
        # Atomic publication occurs only after every actual component has passed.
        save_json(local_path(self.root,'.restock/environment.json'),self.defaults)
        return 'verified'

    def execute(self):
        with install_guard(self.managed/'install.lock'):
            try:
                for name,action in [('manifest',self.validate_manifest),('node',self.install_node),('venv',self.install_venv),
                                    ('dependencies',self.install_packages),('configuration',self.configure),('cli',self.install_cli),
                                    ('browser',self.install_browser),('runtime',self.verify)]:self.step(name,action)
                self.result['status']='complete'
                self.result['env_preserved']=None if self.before_env is None else digest(self.root/'.env')==self.before_env
                save_json(self.run/'installation.json',self.result)
                print('Environment ready. Fill INTERN_API_KEY in the local .env, then open start.cmd.\nNo model request was made.',flush=True)
                return 0
            except Exception as exc:
                self.result.update(status='failed',failed_step=self.stage,
                                   error=str(exc) if isinstance(exc,InstallError) else type(exc).__name__,
                                   trace=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)])
                save_json(self.run/'installation.json',self.result)
                print('Installation stopped at '+self.stage+': '+self.result['error'],flush=True)
                return 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--run',type=Path,required=True)
    args=parser.parse_args(argv)
    try:return Installer(args.root,args.cache,args.run).execute()
    except Exception as exc:
        print(str(exc) if isinstance(exc,InstallError) else 'Installer initialization failed: '+type(exc).__name__)
        return 1

if __name__=='__main__':raise SystemExit(main())
