"""Opt-in shared combined M5 flow. Never counts scripted selection as real model."""
import os
from pathlib import Path
import pytest
from tests.test_browser_collection import portal
from tests.m5_end_to_end import run_flow


def output(tmp_path,mode):return Path(os.getenv('RESTOCK_M5_COMBINED_OUTPUT',str(tmp_path/'evidence')))/mode


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='actual page and workbench opt-in')
def test_workbench_combined_page_collection_and_one_approval(tmp_path,portal):
    run_flow(tmp_path,portal,False,output(tmp_path,'scripted'))


@pytest.mark.skipif(os.getenv('RUN_REAL_MODEL_E2E')!='1',reason='real InternLM explicit opt-in')
def test_required_model_workbench_combined_page_collection_and_one_approval(tmp_path,portal):
    run_flow(tmp_path,portal,True,output(tmp_path,'required'))
