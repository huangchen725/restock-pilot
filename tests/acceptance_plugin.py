"""Explicit collection partition: each node belongs to exactly one acceptance layer."""
import json
from pathlib import Path
import pytest


def layer_for(node):
    file, _, name = node.partition('::')
    if file in ('tests/test_real_m2.py',) or name.startswith('test_required_model_workbench'):
        return 5
    if file in ('tests/test_agent_runtime.py','tests/test_startup.py'):
        return 4
    if file == 'tests/test_m3_workbench.py':
        return 3 if name.startswith('test_workbench_') else 2
    if file in ('tests/test_workbench_e2e.py','tests/test_m4_workbench.py'):
        return 3
    if file in ('tests/test_playwright_e2e.py','tests/test_fault_injection.py','tests/test_m3_live.py'):
        return 2
    return 1


def pytest_addoption(parser):
    parser.addoption('--restock-layer', type=int, choices=range(1,6))
    parser.addoption('--restock-scenarios', action='store_true')
    parser.addoption('--restock-collection')


def pytest_collection_modifyitems(config, items):
    layer = config.getoption('--restock-layer')
    scenarios = config.getoption('--restock-scenarios')
    selected = None
    if scenarios:
        from tests.scenario_catalog import SCENARIOS
        selected = {node for entry in SCENARIOS.values() for node in entry['tests']}
        missing = {node for node in selected if not any(i.nodeid==node or i.nodeid.startswith(node+'[') for i in items)}
        if missing:
            raise pytest.UsageError('Scenario tests missing: '+str(sorted(missing)))
    kept, removed = [], []
    for item in items:
        item.user_properties.append(('nodeid', item.nodeid))
        wanted = (not layer or layer_for(item.nodeid)==layer) and (
            selected is None or any(item.nodeid==n or item.nodeid.startswith(n+'[') for n in selected))
        (kept if wanted else removed).append(item)
    items[:] = kept
    config.hook.pytest_deselected(items=removed)
    target = config.getoption('--restock-collection')
    if target:
        Path(target).write_text(json.dumps([{'id':i.nodeid,'layer':layer_for(i.nodeid)} for i in kept],indent=2),encoding='utf-8')
