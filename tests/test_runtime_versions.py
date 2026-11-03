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
    original_read = Path.read_text
    def read_lock(path, *args, **kwargs):
        if path == root/'runtime-lock.json':
            path = tmp_path/'runtime-lock.json'
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_lock)
    with pytest.raises(ValueError):runtime_versions.check_runtime({},include_cli=True)


def test_declared_dependencies_use_the_verified_lock():
    root=runtime_versions.ROOT
    assert '-r requirements.lock' in (root/'requirements.txt').read_text()
    assert 'litellm==1.106.5' in (root/'requirements.lock').read_text()


@pytest.mark.parametrize('relative', [True, False])
@pytest.mark.parametrize('wrong', [False, True])
def test_real_cli_relative_and_absolute_version_contract(tmp_path, monkeypatch, relative, wrong):
    import os
    from app.runner import resolve_codex_bin
    root = runtime_versions.ROOT
    executable = Path(resolve_codex_bin(str(root)))
    monkeypatch.setenv('RESTOCK_CODEX_BIN', os.path.relpath(executable, root) if relative else str(executable))
    original_read = Path.read_text
    def read_lock(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if wrong and path == root/'runtime-lock.json':
            lock = json.loads(text); lock['codex'] = '0.0.0'; return json.dumps(lock)
        return text
    monkeypatch.setattr(Path, 'read_text', read_lock)
    if wrong:
        with pytest.raises(ValueError, match='Codex CLI'):
            runtime_versions.check_runtime({})
    else:
        assert runtime_versions.check_runtime({})['codex'] != '0.0.0'
