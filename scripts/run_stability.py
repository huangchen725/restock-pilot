"""Frozen-version stability using the actual 30-SKU workbench and browser portal.
Off and required modes have separate denominators. Any unsafe or unverified result
blocks the run regardless of the success threshold; only proven pre-purchase
ordinary timeouts can consume its failure allowance.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
try:
    from scripts.acceptance_common import ROOT, isolated_env, run_pytest, save_json, fingerprint
except ModuleNotFoundError:
    from acceptance_common import ROOT, isolated_env, run_pytest, save_json, fingerprint


def evaluate_run(result, business, output, mode):
    if not isinstance(business,dict):
        business={}
    blocking = bool(result['invalid'] or result['skipped'] or result['errors'])
    blocking |= business.get('mode')!=mode or bool(business.get('safety_failures'))
    success = (result['exit_code']==0 and result['passed']==1 and result['failed']==0
               and business.get('passed') is True and business.get('phase')=='complete')
    if success:
        try:
            from scripts.business_evidence import check_complete
            check_complete(business,output,mode)
        except (AssertionError, KeyError, OSError, TypeError, ValueError):
            blocking=True;success=False
    else:
        ordinary=(business.get('failure_kind')=='ordinary_timeout' and business.get('no_orders_at_timeout') is True
                  and result['failed']==1 and result['exit_code']==1 and business.get('phase') in ('planner','reviser'))
        if not ordinary:
            blocking=True
    return {'success':success and not blocking,'blocking':blocking,'test_result':result,
            'failure_kind':business.get('failure_kind','missing_evidence')}


def run_single_flow(run_num, mode, output):
    node='test_required_model_workbench_full_flow' if mode=='required' else 'test_offline_workbench_full_flow'
    flag='RUN_REAL_MODEL_E2E' if mode=='required' else 'RUN_WORKBENCH_E2E'
    env=isolated_env(output,(flag,),mode)
    env['RESTOCK_M4_OUTPUT']=str(Path(output).resolve()/'business')
    result=run_pytest(['tests/test_m4_workbench.py::'+node],output,env,timeout=2400)
    try:
        business=json.loads((Path(env['RESTOCK_M4_OUTPUT'])/'business.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        business={}
    record=evaluate_run(result,business,env['RESTOCK_M4_OUTPUT'],mode)
    record.update(round=run_num,mode=mode,evidence='business/business.json')
    save_json(Path(output)/'verdict.json',record)
    return record


def verdict(results, target):
    return bool(results) and sum(r['success'] for r in results)>=target and not any(r['blocking'] for r in results)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs',type=int,default=10)
    parser.add_argument('--target-success',type=int,default=9)
    parser.add_argument('--mode',choices=('off','required'),required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'outputs'/('stability-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    args=parser.parse_args(argv)
    if not (args.runs>0 and 1<=args.target_success<=args.runs):
        parser.error('Require runs > 0 and 1 <= target-success <= runs')
    if args.output.exists():
        parser.error('Output must be new; previous evidence is never overwritten')
    args.output.mkdir(parents=True)
    before=fingerprint();save_json(args.output/'before.json',before)
    results=[]; frozen=True
    for number in range(1,args.runs+1):
        if fingerprint()!=before:
            frozen=False;break
        record=run_single_flow(number,args.mode,args.output/f'run-{number:02}')
        results.append(record)
        save_json(args.output/'progress.json',results)
        print(f"Run {number}/{args.runs}: success={record['success']} blocking={record['blocking']}",flush=True)
        if record['blocking']:
            break
    after=fingerprint();save_json(args.output/'after.json',after)
    frozen=frozen and before==after
    report={'mode':args.mode,'requested_runs':args.runs,'executed_runs':len(results),
            'target_success':args.target_success,'successes':sum(r['success'] for r in results),
            'frozen':frozen,'results':results,
            'ok':frozen and len(results)==args.runs and verdict(results,args.target_success)}
    save_json(args.output/'summary.json',report)
    raise SystemExit(0 if report['ok'] else 1)

if __name__=='__main__':
    main()
