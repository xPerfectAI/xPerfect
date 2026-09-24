from dataclasses import replace
import json
import os

import pytest

from workers_projects_runtime.credential_projection import (
    CredentialProjection, CredentialProjectionError, ProjectionBinding,
)
from workers_projects_runtime.provider_credential_artifacts import credential_artifacts


@pytest.fixture
def projection(tmp_path):
    account, member, control = [tmp_path / name for name in ('account', 'member', 'control')]
    for path in (account, member, control):
        path.mkdir(mode=0o700)
    (account / 'codex').mkdir(mode=0o700)
    (account / 'codex/auth.json').write_text('{"refresh":"before"}')
    (account / 'codex/auth.json').chmod(0o600)
    binding = ProjectionBinding('tenant', 'owner', 'acct_one', 'lease_one', 'wrk_one', 'wsp_one',
                                'run_one', 'attempt_one', 'a' * 64, 20001)
    return CredentialProjection(account_root=account, member_home=member, receipt=control / 'receipt.json',
                                binding=binding, artifacts=credential_artifacts('codex', 'subscription'),
                                assert_lease=lambda: None, stop_member=lambda: None)


def test_native_refresh_is_saved_only_after_stop_and_projection_removed(projection):
    projection.prepare()
    target = projection.member_home / '.codex/auth.json'
    target.write_text('{"refresh":"after"}')
    stops = []
    projection.stop_member = lambda: stops.append('confirmed')
    projection.finish()
    assert stops == ['confirmed']
    assert (projection.account_root / 'codex/auth.json').read_text() == '{"refresh":"after"}'
    assert not target.exists()
    assert json.loads(projection.receipt.read_text())['phase'] == 'complete'
    projection.finish()


def test_uncertain_stop_keeps_canonical_and_projection_quarantined(projection):
    projection.prepare()
    target = projection.member_home / '.codex/auth.json'
    target.write_text('{"refresh":"after"}')
    def uncertain():
        raise RuntimeError('generation unconfirmed')
    projection.stop_member = uncertain
    with pytest.raises(RuntimeError, match='unconfirmed'):
        projection.finish()
    assert target.exists()
    assert (projection.account_root / 'codex/auth.json').read_text() == '{"refresh":"before"}'
    assert json.loads(projection.receipt.read_text())['phase'] == 'active'


def test_changed_canonical_revision_is_never_overwritten(projection):
    projection.prepare()
    canonical = projection.account_root / 'codex/auth.json'
    canonical.write_text('{"refresh":"reconnected"}')
    with pytest.raises(CredentialProjectionError, match='revision changed'):
        projection.finish()
    assert canonical.read_text() == '{"refresh":"reconnected"}'


@pytest.mark.parametrize('link', ['symlink', 'hardlink'])
def test_native_links_cannot_reach_canonical_credentials(projection, link):
    projection.prepare()
    target = projection.member_home / '.codex/auth.json'
    target.unlink()
    source = projection.account_root / 'codex/auth.json'
    if link == 'symlink':
        target.symlink_to(source)
    else:
        os.link(source, target)
    with pytest.raises(CredentialProjectionError):
        projection.finish()
    assert source.read_text() == '{"refresh":"before"}'


def test_crash_after_atomic_refresh_recovers_without_losing_native_token(projection, monkeypatch):
    projection.prepare()
    target = projection.member_home / '.codex/auth.json'
    target.write_text('{"refresh":"after"}')
    original = projection._atomic
    crash = [True]
    def atomic(path, content):
        original(path, content)
        if path == projection.account_root / 'codex/auth.json' and crash.pop():
            raise OSError('simulated crash after replace')
    monkeypatch.setattr(projection, '_atomic', atomic)
    with pytest.raises(OSError, match='simulated crash'):
        projection.finish()
    assert json.loads(projection.receipt.read_text())['phase'] == 'refreshing'
    monkeypatch.setattr(projection, '_atomic', original)
    projection.finish()
    assert (projection.account_root / 'codex/auth.json').read_text() == '{"refresh":"after"}'
    assert not target.exists()


def test_exact_member_and_lease_binding_is_required_on_recovery(projection):
    projection.prepare()
    projection.binding = replace(projection.binding, member_uid=20002)
    with pytest.raises(CredentialProjectionError, match='identity changed'):
        projection.finish()


def test_missing_native_credential_does_not_replace_canonical_with_empty(projection):
    projection.prepare()
    (projection.member_home / '.codex/auth.json').unlink()
    with pytest.raises(CredentialProjectionError, match='unavailable'):
        projection.finish()
    assert (projection.account_root / 'codex/auth.json').read_text() == '{"refresh":"before"}'


def test_copyback_ancestor_replacement_cannot_overwrite_another_account(projection, monkeypatch, tmp_path):
    projection.prepare()
    (projection.member_home / '.codex/auth.json').write_text('{"refresh":"after"}')
    outside=tmp_path/'unrelated';outside.mkdir(mode=0o700)
    unrelated=outside/'auth.json';unrelated.write_text('unrelated synthetic credential')
    original=projection._atomic
    def swap(path, content):
        if path == projection.account_root/'codex/auth.json':
            path.parent.rename(projection.account_root/'original-codex')
            path.parent.symlink_to(outside,target_is_directory=True)
        original(path,content)
    monkeypatch.setattr(projection,'_atomic',swap)
    with pytest.raises((CredentialProjectionError,OSError)):
        projection.finish()
    assert unrelated.read_text() == 'unrelated synthetic credential'
    assert json.loads(projection.receipt.read_text())['phase'] == 'refreshing'


def test_cleanup_parent_replacement_cannot_unlink_another_account(projection, monkeypatch, tmp_path):
    projection.prepare()
    outside=tmp_path/'unrelated';outside.mkdir(mode=0o700)
    unrelated=outside/'auth.json';unrelated.write_text('unrelated synthetic credential')
    original=projection._unlink
    def swap(path):
        path.parent.rename(projection.member_home/'original-codex')
        path.parent.symlink_to(outside,target_is_directory=True)
        original(path)
    monkeypatch.setattr(projection,'_unlink',swap)
    with pytest.raises((CredentialProjectionError,OSError)):
        projection.finish()
    assert unrelated.read_text() == 'unrelated synthetic credential'
    assert json.loads(projection.receipt.read_text())['phase'] == 'cleanup'


def test_prepare_parent_replacement_never_copies_secret_to_foreign_directory(projection, monkeypatch, tmp_path):
    outside=tmp_path/'unrelated';outside.mkdir(mode=0o700)
    original=projection._atomic
    def swap(path, content):
        if path == projection.member_home/'.codex/auth.json':
            path.parent.rename(projection.member_home/'original-codex')
            path.parent.symlink_to(outside,target_is_directory=True)
        original(path,content)
    monkeypatch.setattr(projection,'_atomic',swap)
    with pytest.raises((CredentialProjectionError,OSError)):
        projection.prepare()
    assert not (outside/'auth.json').exists()
    assert json.loads(projection.receipt.read_text())['phase'] == 'preparing'


def test_recovery_requires_original_directory_inodes_even_if_bytes_match(projection):
    projection.prepare()
    parent=projection.account_root/'codex'
    content=(parent/'auth.json').read_bytes()
    parent.rename(projection.account_root/'previous-codex')
    parent.mkdir(mode=0o700)
    (parent/'auth.json').write_bytes(content);(parent/'auth.json').chmod(0o600)
    with pytest.raises(CredentialProjectionError,match='directory binding changed'):
        projection.finish()


def test_pin_lifetimes_are_private_to_each_controller_thread(projection):
    from threading import Event, Thread
    first_open, second_open, first_closed = Event(), Event(), Event()
    observations=[]
    def first():
        with projection._pinned():
            first_open.set()
            assert second_open.wait(2)
        first_closed.set()
    def second():
        assert first_open.wait(2)
        with projection._pinned():
            fd=projection._directory_fds[projection.account_root]
            second_open.set()
            assert first_closed.wait(2)
            observations.append(os.fstat(fd).st_ino)
            observations.append(projection._read(projection.account_root/'codex/auth.json',native=False,limit=1024))
    a,b=Thread(target=first),Thread(target=second)
    a.start();b.start();a.join(3);b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert observations == [projection.account_root.stat().st_ino,b'{"refresh":"before"}']
