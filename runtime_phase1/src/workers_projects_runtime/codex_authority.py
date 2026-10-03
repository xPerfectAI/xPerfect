"""Verified developer-unit delivery before ordinary Codex resume.

This small stdlib-only helper runs in the supervised native child's process group,
then replaces itself with the unchanged exec command. It performs no inference.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import tempfile
import time

MAX_LEDGER_BYTES = 1024 * 1024
MAX_RPC_BYTES = 4 * 1024 * 1024


class AuthorityUnconfirmed(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=path.name + ".authority.", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def units(authority: str, declared_tail: str) -> dict[str, str]:
    """Split only the exact tail declared by the authoritative composer."""
    if declared_tail and not authority.endswith(declared_tail):
        raise AuthorityUnconfirmed('declared_tail_not_suffix')
    stable = authority[:-len(declared_tail)].rstrip() if declared_tail else authority
    return {'stable': stable, 'dynamic': declared_tail}


def digests(current: dict[str, str]) -> dict[str, str]:
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in current.items()}


def contained_rollout_path(home: Path, value: object) -> Path | None:
    if not value:
        return None
    try:
        candidate = Path(str(value)).resolve(strict=True)
        candidate.relative_to(home.resolve())
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def rollout_path(home: Path, session: str, known: object = None) -> Path | None:
    cached = contained_rollout_path(home, known)
    if cached is not None:
        return cached
    for database in sorted(home.glob('state_*.sqlite'), reverse=True):
        try:
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.1) as connection:
                row = connection.execute('SELECT rollout_path FROM threads WHERE id=?', (session,)).fetchone()
                if row:
                    candidate = contained_rollout_path(home, row[0])
                    if candidate is not None:
                        return candidate
        except sqlite3.Error:
            continue
    return None


def developer_parts(item: object) -> list[str]:
    if not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'developer':
        return []
    return [str(part.get('text') or '') for part in item.get('content', [])
            if isinstance(part, dict) and part.get('type') in {'input_text', 'output_text', 'text'}]


def reconcile(home: Path, session: str, current: dict[str, str], state: dict,
              *, full_authority: str | None = None) -> dict:
    """Read only new native ledger records; compaction invalidates prior delivery."""
    expected = digests(current)
    if full_authority is None:
        full_authority = '\n\n'.join(text for text in current.values() if text)
    path = rollout_path(home, session, state.get('path'))
    if path is None:
        return {}
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    offset = int(state.get('offset') or 0)
    valid_checkpoint = state.get('inode') == identity and 0 <= offset <= stat.st_size
    delivered = dict(state.get('delivered') or {}) if valid_checkpoint else {}
    last_unit = state.get('last_unit') if valid_checkpoint else None
    offset = offset if valid_checkpoint else 0
    bounded_recovery = stat.st_size - offset > MAX_LEDGER_BYTES
    if bounded_recovery:
        offset = stat.st_size - MAX_LEDGER_BYTES
        delivered = {}
        last_unit = None
    with path.open('rb') as handle:
        handle.seek(offset)
        if bounded_recovery:
            handle.readline()  # a bounded recovery scan never treats a partial record as proof
        complete_offset = handle.tell()
        for raw in handle:
            if not raw.endswith(b'\n'):
                break
            complete_offset = handle.tell()
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = event.get('payload') or {}
            if event.get('type') == 'session_meta' and payload.get('id') != session:
                raise AuthorityUnconfirmed('native_session_mismatch')
            if event.get('type') == 'compacted':
                delivered = {}
                last_unit = None
                items = payload.get('replacement_history') or []
            elif event.get('type') == 'response_item':
                items = [payload]
            else:
                continue
            for item in items:
                parts = developer_parts(item)
                if not parts:
                    continue
                metadata = item.get('internal_chat_message_metadata_passthrough') or {}
                kinds = metadata.get('content_item_kinds') if isinstance(metadata, dict) else None
                # Native inject_items labels application developer items "unknown".
                # Initial/compacted application authority is generic.developer_instructions.
                # Other typed native items (skills, permissions, collaboration, multi-agent)
                # do not replace application authority. Missing metadata is conservative.
                application_item = (
                    not isinstance(kinds, list) or not kinds
                    or 'unknown' in kinds or 'generic.developer_instructions' in kinds
                )
                if not application_item:
                    continue
                native_full_frame = (
                    len(parts) == 1
                    or (isinstance(kinds, list) and len(kinds) == len(parts)
                        and kinds[0] == 'generic.developer_instructions'
                        and all(kind not in {'unknown', 'generic.developer_instructions'}
                                for kind in kinds[1:]))
                )
                if full_authority and parts[0] == full_authority and native_full_frame:
                    delivered.update(expected)
                    last_unit = 'dynamic' if current['dynamic'] else 'stable'
                elif len(parts) == 1 and parts[0] in current.values():
                    for name, unit in current.items():
                        if unit and parts[0] == unit:
                            delivered[name] = expected[name]
                            last_unit = name
                else:
                    # Do not recover historical A as current after a later application AB.
                    # Without a unit owner for this item, invalidate both rather than infer
                    # which instructions changed from their text.
                    delivered = {}
                    last_unit = None
    # Empty units add no native authority. Removal is handled by existing rebind.
    for name, text in current.items():
        if not text:
            delivered[name] = expected[name]
    return {'path': str(path), 'inode': identity, 'offset': complete_offset,
            'delivered': delivered, 'last_unit': last_unit}


def missing_units(current: dict[str, str], state: dict) -> list[str]:
    expected = digests(current)
    missing = [name for name, text in current.items()
               if text and (state.get('delivered') or {}).get(name) != expected[name]]
    # The declared tail remains last after a stable-only update or interrupted inject.
    if current['dynamic'] and ('stable' in missing or state.get('last_unit') != 'dynamic'):
        if 'dynamic' not in missing:
            missing.append('dynamic')
    return missing


def session_state(manifest: dict, session: str, epoch: str) -> dict:
    if (manifest.get('session_key') != session or manifest.get('context_epoch', '') != epoch
            or manifest.get('runtime') != 'codex-cli'):
        raise AuthorityUnconfirmed('provider_session_changed')
    return manifest.get('codex_authority') or {}


def save_state(path: Path, session: str, epoch: str, state: dict) -> None:
    manifest = read_json(path)
    session_state(manifest, session, epoch)
    if manifest.get('codex_authority') != state:
        write_json(path, {**manifest, 'codex_authority': state})


class NativeControl:
    """Bounded stdio request handling, matching the existing provider-control reader."""
    def __init__(self, binary: str, deadline: float):
        self.deadline = deadline
        self.process = subprocess.Popen([binary, 'app-server', '--stdio'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.messages: queue.Queue = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                if len(line) > MAX_RPC_BYTES:
                    self.messages.put(None)
                    return
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.messages.put(value)
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, payload: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + '\n')
        self.process.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        request_id = 'authority-' + method
        self.send({'id': request_id, 'method': method, 'params': params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AuthorityUnconfirmed('native_control_timeout')
            try:
                value = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise AuthorityUnconfirmed('native_control_timeout') from exc
            if value is None:
                raise AuthorityUnconfirmed('native_control_closed')
            if value.get('id') == request_id:
                if 'error' in value or not isinstance(value.get('result'), dict):
                    raise AuthorityUnconfirmed('native_control_rejected')
                return value['result']

    def close(self, *, successful: bool = False) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            code = self.process.wait(timeout=max(0.01, min(1, self.deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.5)
            if successful:
                raise AuthorityUnconfirmed('native_control_exit_unconfirmed')
        else:
            if successful and code != 0:
                raise AuthorityUnconfirmed('native_control_exit_unconfirmed')
        finally:
            self.reader.join(timeout=0.1)


def synchronize(context: dict) -> dict:
    start = time.monotonic()
    current = units(context['authority'], context['declared_tail'])
    manifest_path = Path(context['manifest_path'])
    session, epoch = context['session'], context['epoch']
    state = session_state(read_json(manifest_path), session, epoch)
    state = reconcile(Path(context['codex_home']), session, current, state, full_authority=context['authority'])
    missing = missing_units(current, state)
    phases = {}
    if missing:
        deadline = min(start + 8, float(context['deadline_monotonic']))
        control = NativeControl(context['binary'], deadline)
        closed = False
        try:
            control.call('initialize', {'clientInfo': {'name': 'xperfect-authority', 'version': '1'},
                                      'capabilities': {'experimentalApi': False}})
            control.send({'method': 'initialized', 'params': {}})
            phases['initialize_ms'] = (time.monotonic() - start) * 1000
            before = time.monotonic()
            result = control.call('thread/resume', {'threadId': session, 'excludeTurns': True})
            if result.get('thread', {}).get('id') != session:
                raise AuthorityUnconfirmed('native_session_mismatch')
            native_path = result.get('thread', {}).get('path')
            if native_path:
                path = contained_rollout_path(Path(context['codex_home']), native_path)
                if path is None:
                    raise AuthorityUnconfirmed('native_rollout_path_unconfirmed')
                state = {**state, 'path': str(path)}
            phases['resume_ms'] = (time.monotonic() - before) * 1000
            before = time.monotonic()
            control.call('thread/inject_items', {'threadId': session, 'items': [
                {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': current[name]}]}
                for name in missing]})
            phases['inject_ms'] = (time.monotonic() - before) * 1000
            before = time.monotonic()
            control.close(successful=True)
            closed = True
            phases['close_ms'] = (time.monotonic() - before) * 1000
        finally:
            if not closed:
                control.close()
        state = reconcile(Path(context['codex_home']), session, current, state, full_authority=context['authority'])
        if missing_units(current, state):
            raise AuthorityUnconfirmed('native_persistence_unconfirmed')
    save_state(manifest_path, session, epoch, state)
    return {'preflight': 'injected' if missing else 'reconciled',
            'delivered_stable_sha256': state['delivered']['stable'],
            'delivered_dynamic_sha256': state['delivered']['dynamic'],
            'authority_preflight_ms': (time.monotonic() - start) * 1000,
            'authority_preflight_phases_ms': phases, 'injected_units': missing}


def main() -> None:
    context = read_json(Path(sys.argv[1]))
    try:
        receipt = synchronize(context)
        path = Path(context['receipt_path'])
        write_json(path, {**read_json(path), **receipt})
    except (AuthorityUnconfirmed, OSError, ValueError, KeyError) as exc:
        code = str(exc) if isinstance(exc, AuthorityUnconfirmed) else 'native_control_unavailable'
        write_json(Path(context['failure_path']), {'code': 'authority_update_unconfirmed', 'reason': code})
        print(json.dumps({'type': 'error', 'code': 'authority_update_unconfirmed'}), file=sys.stderr)
        raise SystemExit(78)
    os.execvpe(context['command'][0], context['command'], os.environ)


if __name__ == '__main__':
    main()
