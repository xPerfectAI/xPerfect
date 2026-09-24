"""Preserve native tool selection on the existing MCP child's stdio transport."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time


def native_turn_metadata() -> dict | None:
    """Read only context issued by the parent native relay for this invocation."""
    raw_path = os.environ.get("GLASSHIVE_NATIVE_INPUT_CONTEXT")
    token = os.environ.get("GLASSHIVE_NATIVE_INPUT_TOKEN")
    if not raw_path and not token:
        return None
    if not raw_path or not token or not Path(raw_path).is_absolute():
        raise ValueError("Native invocation context is unavailable")
    try:
        from .native_input import read_object
    except ImportError:  # The selected native MCP child runs this file directly.
        from native_input import read_object
    context = read_object(Path(raw_path))
    if context.get("contextToken") != token or context.get("nativeSessionActive") is not True:
        raise ValueError("Native invocation context is stale")
    session_id = context.get("nativeSessionId")
    turn_id = context.get("nativeTurnId")
    if not all(isinstance(value, str) and value for value in (session_id, turn_id)):
        raise ValueError("Native invocation identity is unavailable")
    return {"session_id": session_id, "turn_id": turn_id}


def native_permission_presentation(packet: dict) -> dict:
    """Retain native metadata while projecting Claude's supported display fields."""
    if packet.get("method") != "elicitation/create":
        return packet
    params = packet.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or "anthropic/permissionDisplay" in meta:
        return packet
    presentation = {}
    for source, target in (("tool_title", "title"), ("tool_name", "title"),
                           ("connector_name", "displayName"), ("tool_description", "description")):
        value = meta.get(source)
        if isinstance(value, str) and value and target not in presentation:
            presentation[target] = value
    if not presentation:
        return packet
    return {**packet, "params": {**params, "_meta": {**meta, "anthropic/permissionDisplay": presentation}}}


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError("Native MCP tool names must be a list of nonempty strings")
    return sorted(set(value))


def selected_filter(server: dict) -> dict | None:
    enabled = _names(server['enabled_tools']) if 'enabled_tools' in server else None
    disabled = _names(server.get('disabled_tools', []))
    tools = server.get('tools', {})
    if not isinstance(tools, dict):
        raise ValueError("Native MCP per-tool settings must be an object")
    disabled = sorted(set(disabled) | {
        name for name, config in tools.items()
        if isinstance(config, dict) and config.get('enabled') is False
    })
    if enabled is None and not disabled:
        return None
    return {'enabled_tools': enabled, 'disabled_tools': disabled}


def read_policy(path: Path, expected_sha256: str) -> dict:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("Native MCP tool policy changed after materialization")
    policy = json.loads(data)
    if not isinstance(policy['command'], str) or not policy['command']:
        raise ValueError("Native MCP child command is missing")
    if not isinstance(policy['args'], list) or any(not isinstance(x, str) for x in policy['args']):
        raise ValueError("Native MCP child arguments are invalid")
    rule = policy['filter']
    if rule['enabled_tools'] is not None:
        _names(rule['enabled_tools'])
    _names(rule['disabled_tools'])
    return policy


def permitted(name: object, rule: dict) -> bool:
    return isinstance(name, str) and bool(name) and (
        rule['enabled_tools'] is None or name in rule['enabled_tools']
    ) and name not in rule['disabled_tools']


class ToolFilter:
    """Preserve selected tools, native request context and elicitation presentation."""
    def __init__(self, rule: dict, *, native_context: bool = False):
        self.rule = rule
        self.native_context = native_context
        self.elicitations: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.lists: set[str] = set()
        self.batches: dict[str, dict] = {}
        self.lock = threading.Lock()

    @staticmethod
    def key(packet: dict) -> str:
        return json.dumps(packet.get('id'), sort_keys=True)

    def permission_request(self, packet: dict) -> dict:
        """Carry offered native scopes through Claude's supported form content."""
        if packet.get('method') != 'elicitation/create' or 'id' not in packet:
            return packet
        params = packet.get('params')
        if not isinstance(params, dict) or params.get('mode', 'form') != 'form':
            return packet
        meta = params.get('_meta')
        offered = meta.get('persist') if isinstance(meta, dict) else None
        schema = params.get('requestedSchema')
        if not isinstance(offered, list) or not isinstance(schema, dict) or schema.get('type') != 'object':
            return packet
        scopes = tuple(scope for scope in ('session', 'always') if scope in offered)
        properties = schema.get('properties', {})
        field = '_viventium_permission_scope'
        if not scopes or not isinstance(properties, dict) or field in properties:
            return packet
        key = self.key(packet)
        if key in self.elicitations:
            raise ValueError('Native elicitation request identity is already pending')
        self.elicitations[key] = (field, scopes)
        labels = {'session': 'For this task', 'always': 'Always'}
        scope_field = {'type': 'string', 'title': 'Remember approval',
                       'description': 'Optional. Leave empty for this request only.',
                       'oneOf': [{'const': scope, 'title': labels[scope]} for scope in scopes]}
        return {**packet, 'params': {**params, 'requestedSchema': {
            **schema, 'properties': {**properties, field: scope_field}}}}

    def permission_response(self, packet: dict) -> dict:
        """Restore only an explicit offered choice, after native result hooks ran."""
        if 'method' in packet or 'id' not in packet:
            return packet
        pending = self.elicitations.pop(self.key(packet), None)
        result = packet.get('result')
        if pending is None or not isinstance(result, dict):
            return packet
        field, scopes = pending
        content = result.get('content')
        if not isinstance(content, dict) or field not in content:
            return packet
        choice = content[field]
        if choice not in scopes:
            raise ValueError('Native permission scope was not offered')
        restored = {**result, 'content': {key: value for key, value in content.items() if key != field}}
        if result.get('action') == 'accept':
            meta = result.get('_meta', {})
            if not isinstance(meta, dict):
                raise ValueError('Invalid native elicitation result metadata')
            # An existing native post-hook metadata decision stays authoritative.
            restored['_meta'] = {'persist': choice, **meta}
        return {**packet, 'result': restored}

    def client(self, packet: object) -> tuple[object | None, object | None]:
        with self.lock:
            items = packet if isinstance(packet, list) else [packet]
            forwarded, errors = [], []
            ids = []
            for item in items:
                if not isinstance(item, dict):
                    forwarded.append(item)
                    continue
                if self.native_context:
                    item = self.permission_response(item)
                method = item.get('method')
                if method == 'tools/call' and not permitted((item['params'].get('name') if isinstance(item.get('params'), dict) else None), self.rule):
                    if 'id' in item:
                        errors.append({'jsonrpc': '2.0', 'id': item['id'], 'error': {
                            'code': -32602, 'message': 'Tool excluded by the selected native MCP configuration.'}})
                    continue
                if self.native_context and method == 'tools/call':
                    metadata = native_turn_metadata()
                    params = item.get('params')
                    if metadata is None or not isinstance(params, dict):
                        raise ValueError("Native invocation context is unavailable")
                    original_meta = params.get('_meta')
                    if original_meta is not None and not isinstance(original_meta, dict):
                        raise ValueError("Invalid native MCP request metadata")
                    item = {**item, 'params': {**params, '_meta': {**(original_meta or {}),
                        'x-codex-turn-metadata': metadata}}}
                forwarded.append(item)
                if method and 'id' in item:
                    key = self.key(item)
                    ids.append(key)
                    if method == 'tools/list':
                        self.lists.add(key)
            if isinstance(packet, list):
                # Old MCP protocol versions can use batches. Combine locally denied
                # responses with the child's responses, without splitting the batch.
                if ids:
                    batch = {'pending': set(ids), 'responses': errors}
                    for key in ids:
                        self.batches[key] = batch
                    errors = []
                return (forwarded if forwarded or not packet else None), (errors or None)
            return (forwarded[0] if forwarded else None), (errors[0] if errors else None)

    def child(self, packet: object) -> object | None:
        with self.lock:
            output = []
            for item in packet if isinstance(packet, list) else [packet]:
                if self.native_context and isinstance(item, dict):
                    item = self.permission_request(native_permission_presentation(item))
                if not isinstance(item, dict) or 'method' in item or 'id' not in item:
                    output.append(item)
                    continue
                key = self.key(item)
                if key in self.lists:
                    self.lists.remove(key)
                    result = item.get('result')
                    if isinstance(result, dict) and isinstance(result.get('tools'), list):
                        item = {**item, 'result': {**result, 'tools': [
                            tool for tool in result['tools']
                            if isinstance(tool, dict) and permitted(tool.get('name'), self.rule)]}}
                batch = self.batches.pop(key, None)
                if batch is None:
                    output.append(item)
                else:
                    batch['responses'].append(item)
                    batch['pending'].discard(key)
                    if not batch['pending']:
                        output.append(batch['responses'])
            if isinstance(packet, list):
                # A complete forwarded batch already has its own response array.
                flattened = []
                for item in output:
                    flattened.extend(item if isinstance(item, list) else [item])
                return flattened or None
            return output[0] if output else None


def relay(policy_path: Path, digest: str) -> int:
    policy = read_policy(policy_path, digest)
    projection = ToolFilter(policy['filter'], native_context=bool(
        os.environ.get("GLASSHIVE_NATIVE_INPUT_CONTEXT") or os.environ.get("GLASSHIVE_NATIVE_INPUT_TOKEN")))
    child = subprocess.Popen([policy['command'], *policy['args']], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=None, start_new_session=True)
    assert child.stdin is not None and child.stdout is not None
    output_lock = threading.Lock()
    input_closed = threading.Event()
    failure: list[Exception] = []

    def write(stream, packet, original: bytes | None = None):
        if packet is None:
            return
        data = original if original is not None else json.dumps(packet, separators=(',', ':')).encode() + b'\n'
        stream.write(data)
        stream.flush()

    def emit(packet, original=None):
        with output_lock:
            write(sys.stdout.buffer, packet, original)

    def stop(sig=signal.SIGTERM):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            if child.poll() is None:
                raise

    def from_client():
        pending = b''
        try:
            while True:
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    if pending:
                        raise ValueError('Incomplete client MCP frame')
                    break
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    read_policy(policy_path, digest)
                    try:
                        packet = json.loads(line)
                    except (ValueError, UnicodeError):
                        child.stdin.write(line + b'\n'); child.stdin.flush()
                        continue
                    forwarded, response = projection.client(packet)
                    emit(response)
                    write(child.stdin, forwarded, line + b'\n' if forwarded == packet else None)
        except Exception as exc:
            failure.append(exc)
            stop()
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass
            input_closed.set()

    def on_signal(sig, _frame):
        input_closed.set()
        stop(sig)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)
    reader = threading.Thread(target=from_client, daemon=True)
    reader.start()
    pending = b''
    deadline = None
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    try:
        while True:
            if input_closed.is_set() and deadline is None:
                deadline = time.monotonic() + 5
            if deadline is not None and time.monotonic() >= deadline:
                stop(signal.SIGKILL)
            if not selector.select(0.1):
                if child.poll() is not None:
                    break
                continue
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                read_policy(policy_path, digest)
                try:
                    packet = json.loads(line)
                except (ValueError, UnicodeError):
                    with output_lock:
                        sys.stdout.buffer.write(line + b'\n'); sys.stdout.buffer.flush()
                    continue
                response = projection.child(packet)
                emit(response, line + b'\n' if response == packet else None)
        if pending:
            # An incomplete JSON-RPC line is invalid transport, not executable data.
            raise ValueError('Native MCP child ended with an incomplete frame')
    finally:
        selector.close()
        stop()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            stop(signal.SIGKILL)
            child.wait(timeout=2)
    if failure:
        raise failure[0]
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


def main():
    try:
        return relay(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:
        print('Selected native MCP transport could not be verified or completed (' + type(exc).__name__ + ').', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
