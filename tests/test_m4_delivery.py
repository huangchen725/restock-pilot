"""M4-06..10 regression controls. Credentials are synthetic, never real keys."""
from pathlib import Path
import os, subprocess, sys, zipfile, json
import pytest
ROOT=Path(__file__).resolve().parents[1]
CHECK=ROOT/'scripts/check_delivery.py'
TOKEN='m4-'+'SYNTHETIC-CREDENTIAL-0123456789abcdef'

def make_candidate(tmp_path,files,git=False):
    root=tmp_path/'candidate';root.mkdir()
    if git:subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
    for name,text in files.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
    if git:subprocess.run(['git','add','-f','--all'],cwd=root,check=True,capture_output=True)
    return root


def check(root,encoding='utf-8',git_discovery=True):
    env={**os.environ,'PYTHONIOENCODING':encoding,'PYTHONUTF8':'1' if encoding=='utf-8' else '0'}
    if not git_discovery:env['GIT_CEILING_DIRECTORIES']=str(root.parent)
    return subprocess.run([sys.executable,str(CHECK),'--path',str(root),'--strict'],capture_output=True,env=env)


def test_known_secret_detection_positive_control(tmp_path):
    root=make_candidate(tmp_path,{'settings.py':f'INTERN_API_KEY = "{TOKEN}"'})
    r=check(root);assert r.returncode!=0 and b'UnicodeEncodeError' not in r.stderr


def test_safe_package_positive_control(tmp_path):
    root=make_candidate(tmp_path,{'README.md':'Demo instructions only.'})
    assert check(root).returncode==0


@pytest.mark.parametrize('name',['outputs/run.log','data/customer.db','agent-integration/session.sqlite','agent-integration/session.jsonl','.git-broken/objects/private','.venv/cache.bin','nested/.env'])
def test_sensitive_candidate_paths_cannot_pass(tmp_path,name):
    root=make_candidate(tmp_path,{name:'SYNTHETIC PRIVATE RUNTIME DATA'})
    assert check(root).returncode!=0,name


def test_unicode_filename_secret_cannot_be_silently_skipped(tmp_path):
    root=make_candidate(tmp_path,{'资料/配置.py':f'INTERN_API_KEY = "{TOKEN}"'})
    assert check(root).returncode!=0


def test_secret_with_example_comment_still_blocks_delivery(tmp_path):
    root=make_candidate(tmp_path,{'settings.py':f'INTERN_API_KEY = "{TOKEN}" # 示例'})
    assert check(root).returncode!=0


def test_nongit_extracted_package_is_actually_scanned(tmp_path):
    root=make_candidate(tmp_path,{'settings.py':f'INTERN_API_KEY = "{TOKEN}"'},git=False)
    assert check(root,git_discovery=False).returncode!=0


def test_missing_input_path_is_not_a_clean_delivery(tmp_path):
    assert check(tmp_path/'missing').returncode!=0


def test_zip_input_must_be_scanned_or_rejected(tmp_path):
    archive=tmp_path/'delivery.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr('settings.py',f'INTERN_API_KEY = "{TOKEN}"')
    assert check(archive).returncode!=0


def test_delivery_diagnostics_do_not_echo_credentials(tmp_path):
    root=make_candidate(tmp_path,{'settings.py':f'INTERN_API_KEY = "{TOKEN}"'})
    r=check(root)
    assert TOKEN.encode() not in r.stdout+r.stderr,'Synthetic credential appeared in diagnostics'


def test_gbk_console_does_not_crash_on_clean_result(tmp_path):
    root=make_candidate(tmp_path,{'README.md':'safe'})
    r=check(root,encoding='cp936')
    assert r.returncode==0 and b'UnicodeEncodeError' not in r.stderr

@pytest.mark.parametrize('encoding',['utf-8','cp936'])
@pytest.mark.parametrize('case,code',[('clean',0),('secret',1),('missing',2)])
def test_encoding_preserves_exit_meaning_and_no_secret(tmp_path,encoding,case,code):
    root=tmp_path/'missing' if case=='missing' else make_candidate(tmp_path,{'配置.py':f'INTERN_API_KEY = "{TOKEN}" # 示例' if case=='secret' else 'safe'})
    r=check(root,encoding=encoding)
    assert r.returncode==code and b'UnicodeEncodeError' not in r.stderr
    assert TOKEN.encode() not in r.stdout+r.stderr
    data=json.loads(r.stdout)
    assert data['exit_code']==code


@pytest.mark.parametrize('text',[f'INTERN_API_KEY={TOKEN}',f'INTERN_API_KEY="{TOKEN}" # 密钥示例',
    f'# INTERN_API_KEY="{TOKEN}" 示例',f'{{"INTERN_API_KEY":"{TOKEN}"}}',f'password = "{TOKEN}"'])
def test_assignment_syntax_and_comments_cannot_bypass(tmp_path,text):
    assert check(make_candidate(tmp_path,{'配置.txt':text})).returncode==1


@pytest.mark.parametrize('placeholder',['','your_api_key','your_key_here','replace_me','${INTERN_API_KEY}'])
def test_explicit_placeholder_values_are_safe(tmp_path,placeholder):
    root=make_candidate(tmp_path,{'.env.example':f'INTERN_API_KEY="{placeholder}"'})
    assert check(root).returncode==0


def test_tracked_source_scope_is_explicit_and_unicode_safe(tmp_path):
    root=make_candidate(tmp_path,{'资料/配置.py':f'INTERN_API_KEY="{TOKEN}"'},git=True)
    r=subprocess.run([sys.executable,str(CHECK),'--path',str(root),'--tracked'],capture_output=True)
    assert r.returncode==1
    data=json.loads(r.stdout)
    assert data['scope']=='tracked-source-candidate' and data['files_scanned']==1
    assert data['findings'][0]['path']=='资料/配置.py'
    # Actual package mode must not silently discard repository metadata.
    assert any(f['kind']=='private/runtime path' for f in json.loads(check(root).stdout)['findings'])


def test_tracked_git_error_is_not_empty_success(tmp_path):
    root=make_candidate(tmp_path,{'README.md':'safe'})
    env={**os.environ,'GIT_CEILING_DIRECTORIES':str(root.parent)}
    r=subprocess.run([sys.executable,str(CHECK),'--path',str(root),'--tracked'],env=env,capture_output=True)
    assert r.returncode==2


@pytest.mark.parametrize('case',['clean','secret','nested_env','traversal','duplicate','symlink','empty','corrupt'])
def test_actual_zip_contents_and_format_controls(tmp_path,case):
    archive=tmp_path/'package.zip'
    if case=='corrupt':archive.write_bytes(b'not a ZIP')
    else:
        with zipfile.ZipFile(archive,'w') as z:
            if case=='clean':z.writestr('资料/README.md','safe')
            elif case=='secret':z.writestr('资料/配置.txt',f'INTERN_API_KEY="{TOKEN}"'.encode('utf-16'))
            elif case=='nested_env':z.writestr('nested/.env','private')
            elif case=='traversal':z.writestr('../outside.txt','safe')
            elif case=='duplicate':
                z.writestr('a.txt','safe')
                with pytest.warns(UserWarning):z.writestr('a.txt','safe')
            elif case=='symlink':
                info=zipfile.ZipInfo('link');info.create_system=3;info.external_attr=0o120777<<16
                z.writestr(info,'outside')
    r=check(archive)
    assert r.returncode==(0 if case=='clean' else 1 if case in ('secret','nested_env','traversal') else 2)
    assert TOKEN.encode() not in r.stdout+r.stderr


def test_empty_directory_and_unreadable_member_cannot_pass(tmp_path,monkeypatch):
    from scripts import check_delivery
    assert check_delivery.scan(tmp_path)['exit_code']==2
    (tmp_path/'readme.txt').write_text('safe')
    original=Path.read_bytes
    def denied(path):
        if path.name=='readme.txt':raise PermissionError(TOKEN)
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',denied)
    report=check_delivery.scan(tmp_path)
    assert report['exit_code']==2 and TOKEN not in json.dumps(report)


def test_report_contains_counts_hashes_but_no_credential(tmp_path):
    root=make_candidate(tmp_path,{'config.txt':f'INTERN_API_KEY="{TOKEN}"'})
    report=tmp_path/'scan.json'
    r=subprocess.run([sys.executable,str(CHECK),'--path',str(root),'--report',str(report)],capture_output=True)
    assert r.returncode==1
    raw=report.read_text(encoding='utf-8')
    data=json.loads(raw)
    assert data['files_scanned']==1 and len(data['manifest'][0]['sha256'])==64
    assert TOKEN not in raw and TOKEN.encode() not in r.stdout+r.stderr


def test_unreadable_subdirectory_is_not_silently_omitted(tmp_path,monkeypatch):
    from scripts import check_delivery
    (tmp_path/'normal.txt').write_text('safe')
    hidden=tmp_path/'denied';hidden.mkdir();(hidden/'config.txt').write_text('safe')
    original=os.scandir
    def denied(path):
        if Path(path)==hidden:raise PermissionError('synthetic unreadable directory')
        return original(path)
    monkeypatch.setattr(os,'scandir',denied)
    assert check_delivery.scan(tmp_path)['exit_code']==2
