"""Independent M4 acceptance controls. All credentials/data below are synthetic."""
from pathlib import Path
import ast,contextlib,importlib.util,io,json,os,subprocess,sys,zipfile
from types import SimpleNamespace
from unittest.mock import patch
import pytest
ROOT=Path(__file__).resolve().parents[2]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
accept=load('audit_accept','scripts/run_acceptance.py')
stability=load('audit_stability','scripts/run_stability.py')
fixed=load('audit_fixed','tests/test_fixed_scenarios.py')
CHECK=ROOT/'scripts/check_delivery.py'
TOKEN='m4-SYNTHETIC-CREDENTIAL-0123456789abcdef'

def invoke(module,args,changes=None):
    with patch.object(sys,'argv',[module.__file__,*args]),contextlib.redirect_stdout(io.StringIO()):
        try:module.main()
        except SystemExit as exc:return exc.code
    return 0


def test_fixed_scenarios_have_no_empty_pass():
    tree=ast.parse((ROOT/'tests/test_fixed_scenarios.py').read_text(encoding='utf-8'))
    empty=[]
    for n in tree.body:
        if isinstance(n,ast.FunctionDef) and n.name.startswith('test_'):
            body=[x for x in n.body if not(isinstance(x,ast.Expr) and isinstance(x.value,ast.Constant) and isinstance(x.value.value,str))]
            if len(body)==1 and isinstance(body[0],ast.Pass):empty.append(n.name)
    assert not empty,empty


def client_for(data):
    return SimpleNamespace(post=lambda *a,**k:SimpleNamespace(json=lambda:data))


def test_moq_oracle_rejects_qty_below_actual_min_order_qty():
    client=client_for({'plan':{'items':[{'order_qty':1,'min_order_qty':24}]}})
    with pytest.raises(AssertionError):fixed.test_scenario_07_moq_rounding(client)


def test_budget_oracle_rejects_overspend_even_if_some_skus_skipped():
    client=client_for({'plan':{'skipped':['A'],'total_cost_cents':2000}})
    with pytest.raises(AssertionError):fixed.test_scenario_11_budget_insufficient(client)


def test_priority_oracle_rejects_missing_priority_fixture():
    client=client_for({'task_id':'synthetic','plan':{'items':[]}})
    with pytest.raises(AssertionError):fixed.test_scenario_03_priority_categories(client)


@pytest.mark.parametrize('message',['未经批准写入','重复申请','金额不一致'])
def test_safety_failure_blocks_stability_despite_nine_successes(message):
    with patch.object(stability,'run_single_flow',side_effect=[(True,None)]*9+[(False,message)]):
        assert invoke(stability,['--runs','10','--target-success','9'])!=0


def test_ordinary_failure_threshold_positive_control():
    with patch.object(stability,'run_single_flow',side_effect=[(True,None)]*9+[(False,'model timeout')]):
        assert invoke(stability,['--runs','10','--target-success','9'])==0


def test_skipped_flow_is_not_stability_success():
    with patch.object(stability.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='1 skipped in 0.01s',stderr='')):
        assert stability.run_single_flow(1)[0] is False


def test_zero_runs_cannot_pass_stability():
    assert invoke(stability,['--runs','0','--target-success','0'])!=0


def test_unknown_only_acceptance_layer_cannot_pass():
    assert invoke(accept,['--layers','999','--strict'])!=0


def test_ordinary_layer_does_not_inherit_real_model_opt_in():
    observed={}
    def fake(cmd,**kwargs):
        observed.update(kwargs['env']);return SimpleNamespace(returncode=0,stdout='1 passed',stderr='')
    with patch.dict(os.environ,{'RUN_REAL_MODEL_E2E':'1'}),patch.object(accept.subprocess,'run',fake),contextlib.redirect_stdout(io.StringIO()):
        accept.run_layer(1,*accept.LAYERS[1],False)
    assert observed.get('RUN_REAL_MODEL_E2E')!='1'


def test_layers_do_not_double_count_ordinary_directory():
    assert accept.LAYERS[1][1]!='tests/', 'Whole test directory also contains browser/UI/agent/real layers'


def test_strict_layer_rejects_skip_positive_control():
    with patch.object(accept.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='1 skipped',stderr='')),contextlib.redirect_stdout(io.StringIO()):
        assert accept.run_layer(2,*accept.LAYERS[2],True)[1]>0


def make_candidate(tmp_path,files,git=True):
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
