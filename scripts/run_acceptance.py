"""Disjoint M4 acceptance layers. Real models run only when layer 5 is selected."""
import argparse
from datetime import datetime
from pathlib import Path
import sys
try:
    from scripts.acceptance_common import ROOT, isolated_env, run_pytest, save_json, fingerprint
except ModuleNotFoundError:
    from acceptance_common import ROOT, isolated_env, run_pytest, save_json, fingerprint

LAYERS = {
    1: ('rules/API and acceptance controls', (), ()),
    2: ('real browser and faults', (), ('RUN_PLAYWRIGHT_E2E',)),
    3: ('real offline workbench', (), ('RUN_WORKBENCH_E2E',)),
    4: ('CLI boundary and startup', (), ('RUN_AGENT_BOUNDARY_E2E','RUN_STARTUP_E2E')),
    5: ('real InternLM API/workbench', (), ('RUN_REAL_MODEL_E2E',)),
    6: ('fresh Windows setup (network downloads)', (), ('RUN_SETUP_E2E',)),
}


def parse_layers(value):
    try:
        numbers = [int(v) for v in value.split(',')]
    except ValueError:
        raise argparse.ArgumentTypeError('Expected comma-separated layer numbers') from None
    if not numbers or len(numbers)!=len(set(numbers)) or any(n not in LAYERS for n in numbers):
        raise argparse.ArgumentTypeError('Layers must be unique numbers from 1 to 6')
    return numbers


def run_layer(num, output, strict=True):
    name, _, flags = LAYERS[num]
    env = isolated_env(output, flags, 'required' if num==5 else 'off')
    result = run_pytest(['tests/'], output, env, ['--restock-layer='+str(num)], timeout=3600)
    result['ok'] = (result['exit_code']==0 and not result['invalid'] and result['passed']>0
                    and not result['failed'] and not result['errors'] and (not strict or not result['skipped']))
    result.update(layer=num, name=name)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layers',type=parse_layers,default=parse_layers('1,2,3,4'))
    parser.add_argument('--strict',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'outputs'/('acceptance-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Output must be new; previous failures must be preserved')
    args.output.mkdir(parents=True)
    before = fingerprint()
    save_json(args.output/'before.json',before)
    results=[]; seen=set(); duplicates=[]
    for num in args.layers:
        result=run_layer(num,args.output/f'layer-{num}',args.strict)
        duplicates.extend(set(result['ids']) & seen);seen.update(result['ids'])
        results.append(result)
        print(f"Layer {num}: {result['passed']} passed, {result['failed']} failed, {result['errors']} errors, {result['skipped']} skipped",flush=True)
    after=fingerprint();save_json(args.output/'after.json',after)
    report={'layers':results,'unique_test_count':len(seen),'duplicates':sorted(duplicates),
            'frozen':before==after,'ok':all(r['ok'] for r in results) and not duplicates and before==after}
    save_json(args.output/'summary.json',report)
    raise SystemExit(0 if report['ok'] else 1)

if __name__=='__main__':
    main()
