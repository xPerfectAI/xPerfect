"""Atomic native publications stay private and current at the named-file check."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers_projects_runtime import native_input


def object_file(tmp_path, value=None):
    path = tmp_path / 'native-input-context.json'
    native_input.publish(path, {'version': 'current'} if value is None else value)
    return path


def test_opened_unlinked_old_inode_is_reread_not_returned(tmp_path, monkeypatch):
    path = object_file(tmp_path, {'version': 'old'})
    real_open = os.open
    opened = []
    def interleaved_open(target, flags, *args, **kwargs):
        descriptor = real_open(target, flags, *args, **kwargs)
        if Path(target) == path:
            opened.append(descriptor)
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            if len(opened) == 1:
                native_input.publish(path, {'version': 'new'})
                assert os.fstat(descriptor).st_nlink == 0
        return descriptor
    monkeypatch.setattr(os, 'open', interleaved_open)
    assert native_input.read_object(path) == {'version': 'new'}
    assert len(opened) == 2
    for descriptor in opened:
        with pytest.raises(OSError): os.fstat(descriptor)


def test_replacement_after_read_reopens_current_named_inode(tmp_path, monkeypatch):
    path = object_file(tmp_path, {'version': 'old'})
    real_fstat = os.fstat
    count = 0
    def replace_after_read(descriptor):
        nonlocal count
        result = real_fstat(descriptor)
        count += 1
        if count == 2:
            native_input.publish(path, {'version': 'new'})
        return result
    monkeypatch.setattr(os, 'fstat', replace_after_read)
    assert native_input.read_object(path) == {'version': 'new'}
    assert count == 4


def test_publisher_cannot_make_reader_retry_without_bound(tmp_path, monkeypatch):
    path = object_file(tmp_path)
    real_open = os.open
    attempts = 0
    def keep_replacing(target, flags, *args, **kwargs):
        nonlocal attempts
        descriptor = real_open(target, flags, *args, **kwargs)
        if Path(target) == path:
            attempts += 1
            native_input.publish(path, {'version': attempts})
        return descriptor
    monkeypatch.setattr(os, 'open', keep_replacing)
    with pytest.raises(native_input.NativeInputError, match='native_input_invalid'):
        native_input.read_object(path)
    assert attempts == 3


@pytest.mark.parametrize('replacement', ['symlink', 'hardlink', 'open_mode', 'fifo'])
def test_unsafe_current_replacement_is_never_read_or_accepted(tmp_path, monkeypatch, replacement):
    path = object_file(tmp_path)
    victim = tmp_path / 'unrelated-private.json'
    native_input.publish(victim, {'private': 'do not return'})
    real_fstat = os.fstat
    count = 0
    def replace_with_unsafe(descriptor):
        nonlocal count
        result = real_fstat(descriptor)
        count += 1
        if count == 1:
            path.unlink()
            if replacement == 'symlink': path.symlink_to(victim)
            elif replacement == 'hardlink': os.link(victim, path)
            elif replacement == 'fifo': os.mkfifo(path, 0o600)
            else:
                path.write_text('{"private":"do not return"}')
                path.chmod(0o644)
        return result
    monkeypatch.setattr(os, 'fstat', replace_with_unsafe)
    with pytest.raises(native_input.NativeInputError, match='native_input_invalid'):
        native_input.read_object(path)
    assert json.loads(victim.read_bytes()) == {'private': 'do not return'}


@pytest.mark.parametrize('unsafe', ['wrong_owner', 'open_mode', 'hardlink', 'oversize', 'directory', 'fifo'])
def test_current_unsafe_file_fails_without_publication_retry(tmp_path, monkeypatch, unsafe):
    path = object_file(tmp_path)
    if unsafe == 'open_mode': path.chmod(0o644)
    elif unsafe == 'hardlink': os.link(path, tmp_path / 'another-name')
    elif unsafe == 'oversize': path.write_bytes(b' ' * 1048577)
    elif unsafe in {'directory', 'fifo'}:
        path.unlink()
        if unsafe == 'directory': path.mkdir()
        else: os.mkfifo(path, 0o600)
    real_fstat = os.fstat
    count = 0
    def checked_descriptor(descriptor):
        nonlocal count
        count += 1
        info = real_fstat(descriptor)
        if unsafe == 'wrong_owner':
            info = SimpleNamespace(**{name: getattr(info, name) for name in dir(info) if name.startswith('st_')})
            info.st_uid += 1
        return info
    monkeypatch.setattr(os, 'fstat', checked_descriptor)
    with pytest.raises(native_input.NativeInputError, match='native_input_invalid'):
        native_input.read_object(path)
    assert count == 1


def test_same_inode_in_place_change_fails_closed(tmp_path, monkeypatch):
    path = object_file(tmp_path)
    real_fstat = os.fstat
    count = 0
    def changed_bytes(descriptor):
        nonlocal count
        result = real_fstat(descriptor)
        count += 1
        if count == 1:
            path.write_text('{"version":"in-place change"}')
        return result
    monkeypatch.setattr(os, 'fstat', changed_bytes)
    with pytest.raises(native_input.NativeInputError, match='native_input_invalid'):
        native_input.read_object(path)
    assert count == 2


@pytest.mark.parametrize('text', ['{', '[]'])
def test_stable_malformed_or_non_object_json_is_not_retried(tmp_path, monkeypatch, text):
    path = object_file(tmp_path)
    path.write_text(text)
    real_open = os.open
    count = 0
    def opened(target, flags, *args, **kwargs):
        nonlocal count
        count += 1
        return real_open(target, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', opened)
    with pytest.raises((native_input.NativeInputError, json.JSONDecodeError)):
        native_input.read_object(path)
    assert count == 1
