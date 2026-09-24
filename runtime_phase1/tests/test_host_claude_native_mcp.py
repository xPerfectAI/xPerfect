"""Native Computer tools are additive to the mission's owner-scoped broker."""
import json
import stat
from pathlib import Path

import pytest
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime


@pytest.fixture
def source(tmp_path, monkeypatch):
    owner = tmp_path / 'owner'
    home = owner / '.codex'
    home.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(owner))
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST', 'node_repl,cua_repl,computer-use')
    monkeypatch.setenv('GLASSHIVE_HOST_PLUGIN_DENYLIST', '')
    monkeypatch.setenv('WPR_HOST_PLUGIN_DENYLIST', '')
    monkeypatch.setenv('WPR_CODEX_CLI_PERSONALITY', 'inherit')
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / 'state'))
    monkeypatch.setattr(runtime, '_project_host_claude_capability_roots', lambda _path: None)
    return runtime, home, owner


def materialize(runtime, tmp_path, bundle, lane):
    worker = {'worker_id': 'wrk_native_tools', 'profile': 'claude-code', 'execution_mode': 'host'}
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    if lane == 'mission':
        runtime._write_host_project_mcp_files(worker, workspace, bundle)
        target = workspace / '.mcp.json'
    else:
        runtime._write_conversation_runtime_files(worker, bundle)
        target = runtime._state_dir(worker['worker_id']) / 'conversation-mcp.json'
        assert not (workspace / '.mcp.json').exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    return json.loads(target.read_text())['mcpServers']


@pytest.mark.parametrize('lane', ['mission', 'conversation'])
def test_materialized_claude_receives_selected_native_stdio_and_broker(source, tmp_path, lane):
    runtime, home, owner = source
    (home / 'config.toml').write_text('[mcp_servers.node_repl]\ncommand="/synthetic/native-tools"\nargs=["mcp"]\n')
    broker = {'type': 'http', 'url': 'http://127.0.0.1:9000/scoped'}
    result = materialize(runtime, tmp_path, {'claude_project_mcp': {'scoped-broker': broker}}, lane)
    assert result['scoped-broker'] == broker
    assert result['node_repl'] == {'type': 'stdio', 'command': '/synthetic/native-tools', 'args': ['mcp'], 'env': {'HOME': str(owner)}}


@pytest.mark.parametrize('restricted', [{'access_mode': 'workspace'}, {'provider_capabilities': {'native_tools': False}}])
def test_restricted_claude_does_not_read_or_add_host_capabilities(source, tmp_path, monkeypatch, restricted):
    runtime, _, _ = source
    monkeypatch.setattr(runtime, '_host_codex_worker_config', lambda *_: pytest.fail('restricted worker read host config'))
    broker = {'type': 'http', 'url': 'http://127.0.0.1:9000/scoped'}
    assert materialize(runtime, tmp_path, {**restricted, 'claude_project_mcp': {'broker': broker}}, 'conversation') == {'broker': broker}


def test_claude_keeps_explicit_server_home_and_selected_env_only(source, tmp_path, monkeypatch):
    runtime, home, _ = source
    monkeypatch.setenv('DECLARED_NATIVE_VALUE', 'synthetic-value')
    monkeypatch.setenv('UNRELATED_SECRET', 'must-not-project')
    (home / 'config.toml').write_text('[mcp_servers.node_repl]\ncommand="/synthetic/native"\nenv_vars=["DECLARED_NATIVE_VALUE"]\n[mcp_servers.node_repl.env]\nHOME="/synthetic/explicit-home"\n')
    result = materialize(runtime, tmp_path, {}, 'mission')
    assert result['node_repl']['env'] == {'HOME': '/synthetic/explicit-home', 'DECLARED_NATIVE_VALUE': 'synthetic-value'}


def test_claude_does_not_replace_broker_collision_or_enable_disabled_native(source, tmp_path):
    runtime, home, _ = source
    (home / 'config.toml').write_text('[mcp_servers.node_repl]\ncommand="/synthetic/native"\n[mcp_servers.computer-use]\ncommand="/synthetic/disabled"\nenabled=false\n[mcp_servers.private-mail]\ncommand="/synthetic/unselected"\n')
    broker = {'type': 'http', 'url': 'http://127.0.0.1:9000/scoped'}
    result = materialize(runtime, tmp_path, {'claude_project_mcp': {'node_repl': broker}}, 'mission')
    assert result == {'node_repl': broker}


@pytest.mark.parametrize('unsupported', ['cwd="/synthetic/different-cwd"'])
def test_claude_never_discards_unrepresentable_native_restrictions(source, tmp_path, unsupported, caplog):
    runtime, home, _ = source
    (home / 'config.toml').write_text('[mcp_servers.node_repl]\ncommand="/synthetic/native"\n'+unsupported+'\n')
    assert materialize(runtime, tmp_path, {}, 'mission') == {}
    assert 'settings unsupported by Claude' in caplog.text


def test_selected_plugin_reuses_existing_native_selection(source, tmp_path):
    runtime, home, owner = source
    (home / 'config.toml').write_text('[plugins."unified-computer-use@openai-bundled"]\nenabled=true\n')
    manifest = home / 'plugins/cache/openai-bundled/unified-computer-use/1.0.0/.mcp.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'mcpServers': {'cua_repl': {'command': '/synthetic/selected-cua', 'args': []}}}))
    assert materialize(runtime, tmp_path, {}, 'mission')['cua_repl']['env']['HOME'] == str(owner)
    (home / 'config.toml').write_text('[plugins."unified-computer-use@openai-bundled"]\nenabled=false\n')
    assert materialize(runtime, tmp_path, {}, 'mission') == {}


@pytest.mark.parametrize('lane', ['mission', 'conversation'])
@pytest.mark.parametrize('restriction,allowed,blocked', [
    ('enabled_tools=["js","js_reset"]', 'js', 'new_tool'),
    ('enabled_tools=[]', None, 'js'),
    ('disabled_tools=["write"]', 'read', 'write'),
    ('[mcp_servers.cua_repl.tools.write]\nenabled=false', 'read', 'write'),
])
def test_native_filters_keep_server_and_deny_excluded_calls(source, tmp_path, lane, restriction, allowed, blocked):
    from workers_projects_runtime.native_mcp_tool_filter import read_policy, permitted
    runtime, home, owner = source
    (home / 'config.toml').write_text('[mcp_servers.cua_repl]\ncommand="/synthetic/cua"\nargs=["serve"]\n' + restriction + '\n')
    servers = materialize(runtime, tmp_path, {}, lane)
    projected = servers['cua_repl']
    assert projected['env'] == {'HOME': str(owner)}
    assert projected['args'][0].endswith('native_mcp_tool_filter.py')
    policy = Path(projected['args'][1])
    data = read_policy(policy, projected['args'][2])
    assert data['command'] == '/synthetic/cua' and data['args'] == ['serve']
    assert not permitted(blocked, data['filter'])
    if allowed:
        assert permitted(allowed, data['filter'])
    worker = {'worker_id': 'wrk_native_tools'}
    target = (tmp_path / 'workspace/.mcp.json' if lane == 'mission' else
              runtime._state_dir(worker['worker_id']) / 'conversation-mcp.json')
    runtime._assert_host_claude_mcp_config(worker, target)
    policy.write_text('{}')
    with pytest.raises(Exception, match='unavailable or stale'):
        runtime._assert_host_claude_mcp_config(worker, target)


def test_missing_native_filter_policy_requires_rematerialization(source, tmp_path):
    runtime, _, _ = source
    materialize(runtime, tmp_path, {}, 'mission')
    policy = runtime._state_dir('wrk_native_tools') / 'native-mcp-tool-policy.json'
    policy.unlink()
    with pytest.raises(Exception, match='policy is missing'):
        runtime._assert_host_claude_mcp_config({'worker_id':'wrk_native_tools'}, tmp_path / 'workspace/.mcp.json')


def test_native_filter_keeps_protocol_schema_paging_notifications_and_batch_errors():
    from workers_projects_runtime.native_mcp_tool_filter import ToolFilter
    p = ToolFilter({'enabled_tools': ['js'], 'disabled_tools': []})
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'cursor': 'next'}}
    assert p.client(request) == (request, None)
    tool = {'name': 'js', 'inputSchema': {'type': 'object', 'properties': {'code': {'type': 'string'}}},
            'description': 'Original native description', 'annotations': {'readOnlyHint': False}}
    response = {'jsonrpc': '2.0', 'id': 1, 'result': {'tools': [tool, {'name': 'blocked'}], 'nextCursor': 'page3', '_meta': {'x': 1}}}
    filtered = p.child(response)
    assert filtered['result'] == {**response['result'], 'tools': [tool]}
    notification = {'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}
    assert p.child(notification) == notification
    reverse_request = {'jsonrpc': '2.0', 'id': 1, 'method': 'roots/list'}
    assert p.child(reverse_request) == reverse_request
    reverse_response = {'jsonrpc': '2.0', 'id': 1, 'result': {'roots': []}}
    assert p.client(reverse_response) == (reverse_response, None)
    batch = [{'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'blocked'}},
             {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}, notification]
    forwarded, local = p.client(batch)
    assert forwarded == batch[1:] and local is None
    error = {'jsonrpc': '2.0', 'id': 3, 'error': {'code': -32000, 'message': 'Original child error'}}
    result = p.child([error])
    assert result[0]['id'] == 2 and result[0]['error']['code'] == -32602
    assert result[1] == error
    cancel = {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 3}}
    assert p.client(cancel) == (cancel, None)
    assert p.client([]) == ([], None)


def test_stdio_filter_real_child_env_stderr_calls_and_lifecycle(tmp_path):
    import hashlib
    import os
    import select
    import subprocess
    import sys
    from workers_projects_runtime import native_mcp_tool_filter
    child = tmp_path / 'child.py'
    child.write_text('''import json,os,sys
print("synthetic-child-stderr", file=sys.stderr, flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if "id" not in r: continue
 if r.get("method")=="tools/list": result={"tools":[{"name":"js","inputSchema":{"type":"object"}},{"name":"blocked"}],"nextCursor":"retained"}
 else:
  with open(sys.argv[1],"a") as f:f.write(str(r.get("params",{}).get("name"))+"\\n")
  result={"content":[{"type":"text","text":os.environ["SYNTHETIC_NATIVE_ENV"]}]}
 print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":result}),flush=True)
''')
    calls = tmp_path / 'calls.txt'
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'command': sys.executable, 'args': [str(child), str(calls)],
        'filter': {'enabled_tools': ['js'], 'disabled_tools': []}}))
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
    process = subprocess.Popen([sys.executable, native_mcp_tool_filter.__file__, str(policy), digest],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, 'SYNTHETIC_NATIVE_ENV': 'unchanged-native-value'})
    def ask(request):
        process.stdin.write(json.dumps(request)+'\n'); process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0]
        return json.loads(process.stdout.readline())
    try:
        assert ask({'id':1,'method':'tools/list'})['result']['tools'] == [{'name':'js','inputSchema':{'type':'object'}}]
        assert ask({'id':2,'method':'tools/call','params':{'name':'blocked'}})['error']['code'] == -32602
        assert not calls.exists()
        assert ask({'id':3,'method':'tools/call','params':{'name':'js'}})['result']['content'][0]['text'] == 'unchanged-native-value'
        assert calls.read_text() == 'js\n'
        policy.write_text('{}')
        process.stdin.write(json.dumps({'id':4,'method':'tools/call','params':{'name':'js'}})+'\n'); process.stdin.flush()
        assert process.wait(timeout=5) == 1
        assert calls.read_text() == 'js\n'
        assert 'synthetic-child-stderr' in process.stderr.read()
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=8)

@pytest.mark.parametrize('termination', ['eof', 'signal', 'child_exit'])
def test_stdio_filter_releases_its_real_child_tree(tmp_path, termination):
    import hashlib
    import os
    import subprocess
    import sys
    import time
    from workers_projects_runtime import native_mcp_tool_filter
    pidfile = tmp_path / 'pids.json'
    child = tmp_path / 'child.py'
    child.write_text('''import json,os,subprocess,sys
p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])
with open(sys.argv[1],"w") as f: json.dump([os.getpid(),p.pid],f)
if len(sys.argv)>2: os._exit(0)
try:
 for line in sys.stdin: pass
finally:
 p.terminate(); p.wait(timeout=3)
''')
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'command': sys.executable, 'args': [str(child), str(pidfile), *(['exit'] if termination == 'child_exit' else [])],
        'filter': {'enabled_tools': ['js'], 'disabled_tools': []}}))
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
    process = subprocess.Popen([sys.executable, native_mcp_tool_filter.__file__, str(policy), digest],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic()+3
        while not pidfile.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        pids = json.loads(pidfile.read_text())
        if termination == 'eof': process.stdin.close()
        elif termination == 'signal': process.terminate()
        assert process.wait(timeout=8) in (0, 143), process.stderr.read()
        def exists(pid):
            try: os.kill(pid,0); return True
            except ProcessLookupError: return False
        deadline=time.monotonic()+3
        while any(exists(pid) for pid in pids) and time.monotonic()<deadline:
            time.sleep(.01)
        assert not any(exists(pid) for pid in pids)
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=8)
