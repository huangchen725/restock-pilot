"""Runtime lock mismatch controls: no upstream model calls."""
import json
from pathlib import Path
import pytest
from app import runtime_versions


@pytest.mark.parametrize('field,value',[('python','3.11.0'),('chromium_revision','0'),('codex','0.0.0')])
def test_runtime_mismatch_is_rejected(tmp_path,monkeypatch,field,value):
    root=runtime_versions.ROOT
    lock=json.loads((root/'runtime-lock.json').read_text());lock[field]=value
    (tmp_path/'runtime-lock.json').write_text(json.dumps(lock))
    (tmp_path/'requirements.lock').write_bytes((root/'requirements.lock').read_bytes())
    monkeypatch.setattr(runtime_versions,'ROOT',tmp_path)
    with pytest.raises(ValueError):runtime_versions.check_runtime({},include_cli=True)


def test_declared_dependencies_use_the_verified_lock():
    root=runtime_versions.ROOT
    assert '-r requirements.lock' in (root/'requirements.txt').read_text()
    assert 'litellm==1.106.5' in (root/'requirements.lock').read_text()
