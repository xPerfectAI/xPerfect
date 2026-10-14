"""Admission avoids unused logical accounting; public meters remain truthful."""
import asyncio
from pathlib import Path

import pytest

from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles, logical_bytes


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'local')
    monkeypatch.delenv('GLASSHIVE_OWNER_STORAGE_BYTES', raising=False)
    retained = tmp_path / 'retained'
    retained.mkdir()
    (retained / 'history.txt').write_bytes(b'older')
    store = Store(str(tmp_path / 'runtime.db'))
    files = WorkspaceFiles(store, owner_roots=lambda *_: [retained])
    yield files
    store.close()


def test_unlimited_admission_does_not_scan_retained_work_but_meter_does(local_files, monkeypatch):
    files = local_files
    original = files._logical_usage
    def forbidden(*_):
        pytest.fail('Unlimited admission must not walk retained owner work')
    monkeypatch.setattr(files, '_logical_usage', forbidden)
    draft = files.create_upload('tenant', 'owner', draft_id='draft', name='input.txt',
        size_bytes=3, idempotency_key='one')
    assert files.create_upload('tenant', 'owner', draft_id='draft', name='input.txt',
        size_bytes=3, idempotency_key='one')['upload_id'] == draft['upload_id']
    async def chunks():
        yield b'abc'
    ready = asyncio.run(files.receive('tenant', 'owner', draft['upload_id'], chunks()))
    assert ready['received_bytes'] == 3
    manifest = [{'size_bytes': 3}]
    with files.store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        files.reserve_projection_manifest(conn, 'tenant', 'owner', manifest)
    assert manifest[0]['projection_id']
    monkeypatch.setattr(files, '_logical_usage', original)
    meter = files.storage('tenant', 'owner')
    assert meter['limit_bytes'] is None
    assert meter['used_logical_bytes'] == 8
    assert meter['reserved_bytes'] == 0


def test_finite_admission_still_accounts_for_retained_bytes(local_files, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_OWNER_STORAGE_BYTES', '7')
    with pytest.raises(FileAdmissionError) as error:
        local_files.create_upload('tenant', 'owner', draft_id='draft', name='input.txt',
            size_bytes=3, idempotency_key='one')
    assert error.value.status_code == 413


def test_unlimited_admission_does_not_bypass_storage_snapshot_guard(local_files, monkeypatch):
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise FileAdmissionError('Storage unavailable', 503)
    monkeypatch.setattr(local_files.storage_adapter, 'snapshot', unavailable)
    with pytest.raises(FileAdmissionError) as error:
        local_files.create_upload('tenant', 'owner', draft_id='draft', name='input.txt',
            size_bytes=3, idempotency_key='one')
    assert calls
    assert error.value.status_code == 503


def test_invalid_unlimited_policy_is_not_bypassed(local_files, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_OWNER_STORAGE_BYTES', 'invalid')
    with pytest.raises(FileAdmissionError) as error:
        local_files.create_upload('tenant', 'owner', draft_id='draft', name='input.txt',
            size_bytes=3, idempotency_key='one')
    assert error.value.status_code == 503


def test_logical_meter_preserves_nested_missing_and_symlink_byte_truth(tmp_path):
    root = tmp_path / 'root'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    (root / 'one').write_bytes(b'ab')
    (nested / 'two').write_bytes(b'cde')
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'three').write_bytes(b'fghi')
    (root / 'linked-file').symlink_to(other / 'three')
    (root / 'linked-dir').symlink_to(other, target_is_directory=True)
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    assert logical_bytes([nested, root, root, alias, other, tmp_path / 'missing']) == 9


def test_large_disjoint_root_catalog_does_not_compare_every_prior_root(tmp_path, monkeypatch):
    roots = [tmp_path / str(index) for index in range(64)]
    for root in roots:
        root.mkdir()
    comparisons = []
    original = Path.is_relative_to
    def counted(path, other):
        comparisons.append((path, other))
        return original(path, other)
    monkeypatch.setattr(Path, 'is_relative_to', counted)
    assert logical_bytes(roots) == 0
    assert len(comparisons) <= len(roots), 'Root filtering must scale with path depth, not prior catalog size'
