"""Formal actual UI entries: mode gates are explicit and cannot be inherited accidentally."""
import os
from pathlib import Path
import pytest
from tests.workbench_flow import run_workbench_flow


def output_for(tmp_path):
    return Path(os.getenv('RESTOCK_M4_OUTPUT',str(tmp_path/'evidence')))


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='explicit offline real workbench')
def test_offline_workbench_full_flow(tmp_path):
    run_workbench_flow(tmp_path,'off',output_for(tmp_path))


@pytest.mark.skipif(os.getenv('RUN_REAL_MODEL_E2E')!='1',reason='explicit real InternLM workbench')
def test_required_model_workbench_full_flow(tmp_path):
    run_workbench_flow(tmp_path,'required',output_for(tmp_path))
