"""M4 acceptance controllers: original counterexamples plus adjacent controls."""
import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import pytest
from scripts import run_stability as stability, run_acceptance as acceptance
from scripts import acceptance_common as common
from tests import acceptance_plugin as partition
from tests import test_fixed_scenarios as fixed
from tests.scenario_catalog import SCENARIOS

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('oracle,good,bad',[
    (fixed.assert_moq,{'items':[{'order_qty':25,'min_order_qty':24}]},{'items':[{'order_qty':1,'min_order_qty':24}]}),
    (lambda p:fixed.assert_tight_budget(p,1000),{'skipped':['A'],'total_cost_cents':0},{'skipped':['A'],'total_cost_cents':2000}),
    (fixed.assert_priority,{'items':[{'sku':'A','category':'饮料','order_qty':48},{'sku':'B','category':'日用','order_qty':12}]},{'items':[]}),
])
def test_original_bad_oracles_rejected_with_valid_controls(oracle,good,bad):
    oracle(good)
    with pytest.raises(AssertionError):oracle(bad)

@pytest.mark.parametrize('broken',['empty_moq','missing_moq','no_shortfall','priority_reversed'])
def test_adjacent_oracle_faults(broken):
    with pytest.raises((AssertionError,KeyError)):
        if broken=='empty_moq':fixed.assert_moq({'items':[]})
        elif broken=='missing_moq':fixed.assert_moq({'items':[{'order_qty':1}]})
        elif broken=='no_shortfall':fixed.assert_tight_budget({'total_cost_cents':0,'skipped':[]},1000)
        else:fixed.assert_priority({'items':[{'sku':'B','order_qty':48},{'sku':'A','order_qty':12}]})


def test_fixed_scenario_catalog_has_twenty_real_asserting_targets():
    assert set(SCENARIOS)==set(range(1,21))
    nodes={n for scenario in SCENARIOS.values() for n in scenario['tests']}
    for node in nodes:
        file,fn=node.split('::')
        tree=ast.parse((ROOT/file).read_text(encoding='utf-8-sig'))
        target=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==fn)
        assert any(isinstance(n,ast.Assert) or (isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
                   and (n.func.id.startswith('assert_') or n.func.id=='run_workbench_flow')) for n in ast.walk(target)),node
        body=[n for n in target.body if not(isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant))]
        assert body and not all(isinstance(n,ast.Pass) for n in body),node
    assert 'tests/test_m3_workbench.py::test_workbench_partial_reconcile_recover_refresh_export' in nodes
    assert 'tests/test_m3_live.py::test_live_unknown_revise_cannot_create_duplicate_purchase' in nodes


def result(**changes):
    return dict({'passed':1,'failed':0,'errors':0,'skipped':0,'ids':['flow'],'invalid':[],'exit_code':0},**changes)


def valid_business(path,mode='off'):
    import csv,io
    items=[dict(sku=f'A{i}',supplier_id='S',order_qty=1,unit_cost_cents=100,line_total_cents=100) for i in range(30)]
    plan={'plan_id':'task-demo','version':2,'dataset_hash':'data','policy':{'budget_cents':3000},'total_cost_cents':3000,'items':items}
    order={'order_id':'PO-1','supplier_id':'S','external_ref':'task-demo-v2-S','status':'SUBMITTED','total_cost_cents':3000,'lines':items}
    out=io.StringIO();writer=csv.writer(out)
    writer.writerow(['方案编号','版本','数据版本','SKU','供应商','采购数量','单价（分）','金额（分）'])
    for item in items:writer.writerow(['task-demo',2,'data',item['sku'],'S',1,100,100])
    exports={}
    for suffix,text in [('csv',out.getvalue()),('html','task-demo plan2 data 合计 3000 分 SUBMITTED PO-1')]:
        p=path/('delivered.'+suffix);p.write_text(text,encoding='utf-8')
        exports[suffix]={'path':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    first={**plan,'version':1}
    agent={'operation':{'result':{'agent':{'status':'ok'}}}}
    return {'mode':mode,'model':'intern-s2' if mode=='required' else None,'sample_skus':30,'task_id':'task-demo','passed':True,'phase':'complete',
        'failure_kind':None,'safety_failures':[], 'dataset_files':dict.fromkeys(['sales','inventory','suppliers'],'hash'),
        'planner':{**agent,'latest_plan':first,'latest_plan_hash':'plan1'},
        'reviser':{**agent,'latest_plan':plan,'latest_plan_hash':'plan2','approved_plan_hash':None},'executor':agent,
        'approval_v1':{'approved_plan_hash':'plan1','approved_version':1,'state':'APPROVED'},
        'approval_v2':{'approved_plan_hash':'plan2','approved_version':2,'state':'APPROVED'},
        'final_state':{'state':'COMPLETED','latest_plan_hash':'plan2'},
        'activity':[{'kind':'business_tool','payload':{'tool':name,'result':{}}} for name in ['inspect_dataset','build_plan','revise_plan','get_execution_result']],
        'independent_orders':[order],'exports':exports,'unauthorized_execute_http':409,'stale_execute_http':409,'repeat_execute_http':409,
        'orders_before_approval':[],'orders_after_revision':[],'orders_after_repeat':[order]}


@pytest.mark.parametrize('code',['unauthorized_write','duplicate_order','amount_mismatch'])
def test_safety_failure_blocks_even_nine_successes(tmp_path,code):
    data=valid_business(tmp_path);data['safety_failures']=[code];data['passed']=False;data['failure_kind']='blocking'
    bad=stability.evaluate_run(result(passed=0,failed=1,exit_code=1),data,tmp_path,'off')
    assert bad['blocking'] and not bad['success']
    assert not stability.verdict([{'success':True,'blocking':False}]*9+[bad],9)


def test_ordinary_timeout_threshold_positive_control(tmp_path):
    data=valid_business(tmp_path);data.update(passed=False,phase='planner',failure_kind='ordinary_timeout',no_orders_at_timeout=True)
    failed=stability.evaluate_run(result(passed=0,failed=1,exit_code=1),data,tmp_path,'off')
    assert not failed['blocking'] and not failed['success']
    assert stability.verdict([{'success':True,'blocking':False}]*9+[failed],9)
    assert not stability.verdict([{'success':True,'blocking':False}]*8+[failed]*2,9)


@pytest.mark.parametrize('kind',['skip','empty','error','exit','missing','mode','export','approval','dataset','unsafe_timeout'])
def test_incomplete_or_inconsistent_evidence_is_blocking(tmp_path,kind):
    data=valid_business(tmp_path);r=result()
    if kind=='skip':r.update(passed=0,skipped=1)
    elif kind=='empty':r.update(passed=0,invalid=['empty'])
    elif kind=='error':r.update(passed=0,errors=1)
    elif kind=='exit':r['exit_code']=1
    elif kind=='missing':data={}
    elif kind=='mode':data['mode']='required'
    elif kind=='export':(tmp_path/'delivered.csv').write_text('changed')
    elif kind=='approval':data['approval_v2']['approved_plan_hash']='old'
    elif kind=='dataset':data['reviser']['latest_plan']['dataset_hash']='another'
    elif kind=='unsafe_timeout':
        data.update(passed=False,phase='executor',failure_kind='ordinary_timeout',no_orders_at_timeout=True)
        r.update(passed=0,failed=1,exit_code=1)
    checked=stability.evaluate_run(r,data,tmp_path,'off')
    assert checked['blocking'] and not checked['success']


@pytest.mark.parametrize('mode',['off','required'])
def test_complete_matching_evidence_passes(tmp_path,mode):
    assert stability.evaluate_run(result(),valid_business(tmp_path,mode),tmp_path,mode)['success']


@pytest.mark.parametrize('runs,target',[(0,0),(-1,1),(1,0),(1,2)])
def test_invalid_run_counts_rejected_before_launch(tmp_path,runs,target,monkeypatch):
    monkeypatch.setattr(stability,'run_single_flow',lambda *a:pytest.fail('must not launch'))
    with pytest.raises(SystemExit) as exc:
        stability.main(['--mode','off','--runs',str(runs),'--target-success',str(target),'--output',str(tmp_path/'run')])
    assert exc.value.code==2


@pytest.mark.parametrize('mode,node,flag',[('off','test_offline_workbench_full_flow','RUN_WORKBENCH_E2E'),('required','test_required_model_workbench_full_flow','RUN_REAL_MODEL_E2E')])
def test_stability_selects_actual_ui_and_explicit_mode(tmp_path,monkeypatch,mode,node,flag):
    observed={}
    monkeypatch.setenv('RUN_REAL_MODEL_E2E','1')
    def fake(targets,output,env,**kwargs):
        observed.update(targets=targets,env=env)
        folder=Path(env['RESTOCK_M4_OUTPUT']);folder.mkdir(parents=True)
        common.save_json(folder/'business.json',valid_business(folder,mode))
        return result()
    monkeypatch.setattr(stability,'run_pytest',fake)
    assert stability.run_single_flow(1,mode,tmp_path/'run')['success']
    assert observed['targets']==['tests/test_m4_workbench.py::'+node]
    assert observed['env'][flag]=='1' and observed['env']['RESTOCK_AGENT_MODE']==mode
    if mode=='off':assert observed['env']['RUN_REAL_MODEL_E2E']=='0'


@pytest.mark.parametrize('layers',['999','','1,1','1,6','x','1,'])
def test_invalid_layers_rejected_before_launch(layers,monkeypatch):
    monkeypatch.setattr(acceptance,'run_layer',lambda *a:pytest.fail('must not launch'))
    with pytest.raises(SystemExit) as exc:acceptance.main(['--layers',layers,'--strict'])
    assert exc.value.code==2


@pytest.mark.parametrize('layer',[1,2,3,4,5])
def test_layer_environment_is_explicit(tmp_path,monkeypatch,layer):
    for flag in common.FLAGS:monkeypatch.setenv(flag,'1')
    observed={}
    def fake(targets,output,env,options,**kwargs):
        observed.update(env=env,options=options);return result()
    monkeypatch.setattr(acceptance,'run_pytest',fake)
    assert acceptance.run_layer(layer,tmp_path/'layer',True)['ok']
    assert observed['options']==['--restock-layer='+str(layer)]
    for flag in common.FLAGS:assert observed['env'][flag]==('1' if flag in acceptance.LAYERS[layer][2] else '0')
    assert observed['env']['RUN_REAL_MODEL_E2E']==('1' if layer==5 else '0')


@pytest.mark.parametrize('change',[{'skipped':1},{'errors':1},{'failed':1},{'passed':0},{'invalid':['missing']},{'exit_code':1}])
def test_layer_strict_counts_failures_and_errors(tmp_path,monkeypatch,change):
    monkeypatch.setattr(acceptance,'run_pytest',lambda *a,**k:result(**change))
    assert not acceptance.run_layer(1,tmp_path/'layer',True)['ok']


def test_junit_counts_each_case_and_rejects_duplicates(tmp_path):
    file=tmp_path/'junit.xml'
    file.write_text('<testsuites><testsuite><testcase name="ok"/><testcase name="bad"><failure/></testcase><testcase name="setup"><error/></testcase><testcase name="skip"><skipped/></testcase></testsuite></testsuites>')
    counts=common.read_junit(file)
    assert (counts['passed'],counts['failed'],counts['errors'],counts['skipped'])==(1,1,1,1)
    file.write_text('<testsuite><testcase name="a"/><testcase name="a"/></testsuite>')
    assert common.read_junit(file)['invalid']
    file.write_text('<testsuite/>');assert common.read_junit(file)['invalid']
    assert common.read_junit(tmp_path/'missing')['invalid']


def test_stability_freeze_detects_mid_run_edit(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(stability,'fingerprint',lambda:{'source':len(calls)})
    def flow(*args):calls.append(1);return {'success':True,'blocking':False}
    monkeypatch.setattr(stability,'run_single_flow',flow)
    with pytest.raises(SystemExit) as exc:
        stability.main(['--mode','off','--runs','2','--target-success','1','--output',str(tmp_path/'freeze')])
    assert exc.value.code==1
    data=json.loads((tmp_path/'freeze/summary.json').read_text())
    assert data['executed_runs']==1 and not data['frozen']


def test_layer_duplicate_case_cannot_pass(tmp_path,monkeypatch):
    monkeypatch.setattr(acceptance,'fingerprint',lambda:{'source':'same'})
    monkeypatch.setattr(acceptance,'run_layer',lambda *a:dict(result(),ok=True))
    with pytest.raises(SystemExit) as exc:
        acceptance.main(['--layers','1,2','--strict','--output',str(tmp_path/'duplicate')])
    assert exc.value.code==1
    assert json.loads((tmp_path/'duplicate/summary.json').read_text())['duplicates']==['flow']

@pytest.mark.parametrize('fault',['amount','quantity','duplicate','order_status','wrong_version','old_approval','old_dataset','csv_amount','html_order','missing_model_tool'])
def test_business_proof_comparator_rejects_corruption(tmp_path,fault):
    data=valid_business(tmp_path,'required')
    if fault=='amount':data['independent_orders'][0]['total_cost_cents']+=1
    elif fault=='quantity':data['independent_orders'][0]=deepcopy(data['independent_orders'][0]);data['independent_orders'][0]['lines'][0]['order_qty']=2
    elif fault=='duplicate':data['independent_orders'].append(deepcopy(data['independent_orders'][0]))
    elif fault=='order_status':data['independent_orders'][0]['status']='DRAFT'
    elif fault=='wrong_version':data['reviser']['latest_plan']['version']=1
    elif fault=='old_approval':data['approval_v2']['approved_version']=1
    elif fault=='old_dataset':data['planner']['latest_plan']['dataset_hash']='old'
    elif fault in ('csv_amount','html_order'):
        kind='csv' if fault=='csv_amount' else 'html';path=tmp_path/data['exports'][kind]['path']
        raw=path.read_text(encoding='utf-8')
        path.write_text(raw.replace(',100,100',',100,200',1) if kind=='csv' else raw.replace('PO-1','PO-WRONG'),encoding='utf-8')
        data['exports'][kind]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    else:data['activity']=[]
    assert stability.evaluate_run(result(),data,tmp_path,'required')['blocking']


def test_different_valid_versions_are_not_compared_to_current_plan(tmp_path):
    data=valid_business(tmp_path)
    # v1 is independently different and valid; only v2 is approved for the real order.
    data['planner']['latest_plan']=deepcopy(data['planner']['latest_plan'])
    data['planner']['latest_plan']['policy']['budget_cents']=9000
    data['planner']['latest_plan']['items'][0]['order_qty']=2
    data['planner']['latest_plan']['items'][0]['line_total_cents']=200
    data['planner']['latest_plan']['total_cost_cents']=3100
    assert stability.evaluate_run(result(),data,tmp_path,'off')['success']


def test_run_pytest_real_subprocess_counts_skip_as_skip(tmp_path):
    test=tmp_path/'test_child.py'
    test.write_text('import pytest\ndef test_ok(): assert 2+2==4\n@pytest.mark.skip(reason="control")\ndef test_skip(): assert False\n')
    output=tmp_path/'child'
    r=common.run_pytest([str(test)],output,common.isolated_env(output),timeout=30)
    assert (r['passed'],r['skipped'],r['errors'],r['failed'],r['exit_code'])==(1,1,0,0,0)
    assert not r['invalid'] and len(set(r['ids']))==2


def test_layer_and_scenario_collection_uses_each_actual_node_once(tmp_path):
    manifest=tmp_path/'collection.json'
    r=subprocess.run([sys.executable,'-m','pytest','tests/','--collect-only','-q','-p','tests.acceptance_plugin',
                      '--restock-scenarios','--restock-collection='+str(manifest)],cwd=ROOT,capture_output=True,
                     text=True,encoding='utf-8',env={**os.environ,'PYTHONIOENCODING':'utf-8'},timeout=30)
    assert r.returncode==0,r.stdout+r.stderr
    nodes=json.loads(manifest.read_text())
    assert nodes and len(nodes)==len({n['id'] for n in nodes})
    assert {n['layer'] for n in nodes}=={1,2,3}
    for scenario in SCENARIOS.values():
        for expected in scenario['tests']:
            assert any(n['id']==expected or n['id'].startswith(expected+'[') for n in nodes)


def test_malformed_export_is_structured_blocking_result(tmp_path):
    data=valid_business(tmp_path)
    path=tmp_path/'delivered.csv'
    path.write_text(path.read_text(encoding='utf-8').replace(',100,100',',oops,100',1),encoding='utf-8')
    data['exports']['csv']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    assert stability.evaluate_run(result(),data,tmp_path,'off')['blocking']


@pytest.mark.parametrize('where',['launch','cleanup'])
def test_runner_infrastructure_failure_is_recorded_and_blocks(tmp_path,monkeypatch,where):
    import app.processes
    secret='synthetic-'+'diagnostic-secret-value'
    class Scope:
        def spawn(self,*a,**k):
            if where=='launch':raise OSError(secret)
            return self
        def communicate(self,**k):return '',''
        returncode=0
        def stop(self):
            if where=='cleanup':raise OSError(secret)
        def close(self):pass
    monkeypatch.setattr(app.processes,'ProcessScope',Scope)
    output=tmp_path/'failure'
    env=common.isolated_env(output);env['SYNTHETIC_API_KEY']=secret
    r=common.run_pytest(['nonexistent'],output,env)
    assert r['exit_code']!=0 and r['invalid']
    assert secret not in (output/'pytest.log').read_text(encoding='utf-8')
