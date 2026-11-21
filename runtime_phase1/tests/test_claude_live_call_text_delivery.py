"""A retained Claude live call speaks authored text as it streams; text-only stays typed."""
import json
import os
import sys

import pytest

from workers_projects_runtime import claude_conversation as transport
from workers_projects_runtime import conversation_provider as provider
from workers_projects_runtime.agent_builder_control import (
    NATIVE_TEXT_DELIVERY_SERVER,
    NATIVE_TEXT_DELIVERY_TOOL_RESULT,
    messaging_delivery_control,
    native_text_delivery,
    native_text_delivery_tool_name,
    native_text_envelope,
    parse_conversation_output,
)
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime

DELIVERY = messaging_delivery_control(audio_eligible=True)
GRAPH = {"version": 1, "tools": [{"name": "lc_transfer_to_helper", "description": "Transfer."}]}
TOOL = native_text_delivery_tool_name()


def _stream(event):
    return json.dumps({"type": "stream_event", "event": event})


def _init(*tools):
    return json.dumps({"type": "system", "subtype": "init", "tools": list(tools)})


def _text(index, *deltas):
    return [
        _stream({"type": "content_block_start", "index": index, "content_block": {"type": "text"}}),
        *[_stream({"type": "content_block_delta", "index": index,
                   "delta": {"type": "text_delta", "text": delta}}) for delta in deltas],
        _stream({"type": "content_block_stop", "index": index}),
    ]


def _tool(index, name, *fragments):
    return [
        _stream({"type": "content_block_start", "index": index,
                 "content_block": {"type": "tool_use", "name": name}}),
        *[_stream({"type": "content_block_delta", "index": index,
                   "delta": {"type": "input_json_delta", "partial_json": fragment}})
          for fragment in fragments],
        _stream({"type": "content_block_stop", "index": index}),
    ]


RESULT = json.dumps({"type": "result", "result": "ignored native summary", "is_error": False})


def _spoken(lines):
    return provider._native_spoken_content("claude-code", "\n".join(lines), None, DELIVERY)


def _terminal(lines, graph=None):
    raw = provider._native_terminal_text("claude-code", "\n".join(lines), graph, DELIVERY)
    return raw, (parse_conversation_output(raw, graph, DELIVERY) if raw else None)


def test_only_a_live_call_without_graph_transfers_speaks_native_text():
    assert native_text_delivery(None, DELIVERY, "voice") is True
    assert native_text_delivery(None, DELIVERY, "telegram") is False
    assert native_text_delivery(None, None, "voice") is False
    assert native_text_delivery(GRAPH, DELIVERY, "voice") is False


def test_envelope_round_trips_quotes_newlines_and_non_ascii():
    content = 'She said "hi"\n\\ café 😀'
    decision = parse_conversation_output(native_text_envelope(content, "eligible"), None, DELIVERY)
    assert decision["content"] == content
    assert decision["delivery_disposition"] == {
        "version": 1, "audio": "eligible", "required": True, "valid": True, "source": "model"}
    with pytest.raises(ValueError):
        native_text_envelope(content, "loud")


def test_authored_text_is_spoken_from_its_first_delta():
    lines = [_init(TOOL), *_text(0, "Loud ", "and clear.")]
    first = _spoken(lines[:3])
    assert first["text"] == "Loud" and first["part_complete"] is False
    assert first["delivery_disposition"]["audio"] == "eligible"
    assert first["completed_parts"] == []
    done = _spoken([*lines, RESULT])
    assert done["text"] == "Loud and clear." and done["part_complete"] is True
    assert done["completed_parts"] == [{"part_index": 1, "text_end": len("Loud and clear.")}]
    raw, decision = _terminal([*lines, RESULT])
    assert decision["content"] == "Loud and clear."
    assert decision["delivery_disposition"]["audio"] == "eligible"
    assert decision["delivery_disposition"]["valid"] is True


def test_terminal_waits_for_the_native_result():
    lines = [_init(TOOL), *_text(0, "Almost")]
    assert _terminal(lines) == (None, None)


def test_text_only_tool_is_never_spoken_and_is_the_complete_reply():
    lines = [_init(TOOL),
             *_tool(0, TOOL, '{"content":"Par', 'is."}'),
             *_text(0, "Paris. Sent as text."), RESULT]
    spoken = _spoken(lines)
    assert spoken["text"] == "Paris." and spoken["completed_parts"] == []
    assert [part["delivery_disposition"]["audio"] for part in spoken["presentation_parts"]] == ["skip"]
    raw, decision = _terminal(lines)
    assert decision["content"] == "Paris."
    assert decision["delivery_disposition"]["audio"] == "skip"
    assert decision["delivery_disposition"]["valid"] is True


def test_speech_before_a_text_only_reply_keeps_its_own_voice_choice():
    lines = [_init(TOOL), *_text(0, "Sending it now."),
             *_tool(1, TOOL, '{"content":"The code is 42."}'), RESULT]
    spoken = _spoken(lines)
    assert spoken["completed_parts"] == [{"part_index": 1, "text_end": len("Sending it now.")}]
    assert [part["delivery_disposition"]["audio"] for part in spoken["presentation_parts"]] == ["eligible", "skip"]
    raw, decision = _terminal(lines)
    assert decision["content"] == "Sending it now.\n\nThe code is 42."


def test_spoken_acknowledgement_before_a_work_tool_seals_its_own_part():
    lines = [_init(TOOL, "mcp__broker__hand_off_to"),
             *_text(0, "On it."), *_tool(1, "mcp__broker__hand_off_to", "{}")]
    mid = _spoken(lines)
    assert mid["text"] == "On it."
    assert mid["completed_parts"] == [{"part_index": 1, "text_end": 6}]
    raw, decision = _terminal([*lines, RESULT])
    assert decision["content"] == "On it."
    assert decision["delivery_disposition"]["audio"] == "eligible"
    after = [*lines, *_text(0, "Started."), RESULT]
    raw, decision = _terminal(after)
    assert decision["content"] == "On it.\n\nStarted."
    assert decision["delivery_disposition"]["audio"] == "eligible"


def test_a_turn_with_no_public_text_is_a_valid_silent_result():
    lines = [_init(TOOL, "mcp__broker__hand_off_to"),
             *_tool(0, "mcp__broker__hand_off_to", "{}"), RESULT]
    raw, decision = _terminal(lines)
    assert decision["content"] == ""
    assert decision["delivery_disposition"] == {
        "version": 1, "audio": "skip", "required": True, "valid": True, "source": "model"}


def test_structured_transport_is_untouched_when_the_tool_is_not_declared():
    envelope = native_text_envelope("Typed.", "skip")
    lines = [_init("StructuredOutput"), *_text(0, "Prose first."),
             json.dumps({"type": "result", "result": envelope, "is_error": False})]
    assert _spoken(lines) is None
    assert provider._native_terminal_text("claude-code", "\n".join(lines), None, DELIVERY) == envelope


def test_subagent_stream_events_are_not_public_speech():
    nested = json.dumps({"type": "stream_event", "parent_tool_use_id": "toolu_1", "event": {
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "private"}}})
    assert _spoken([_init(TOOL), nested]) is None


@pytest.mark.parametrize("text,held", [
    ("[laugh]\n\nRain on the window", None),
    ("Hello [chuckle] there", None),
    ("Listen [laugh]", len("Listen ")),
    ("See [the file](/tmp/a", len("See ")),
])
def test_only_the_unfinished_last_line_can_still_become_a_link(text, held):
    assert provider._mutable_native_file_link_start(text, incomplete=True) == held


def test_voice_tag_on_its_own_line_does_not_hold_back_the_reply():
    lines = [_init(TOOL), *_text(0, "[laugh]\n\n", "Rain on ", "the window")]
    assert _spoken(lines)["text"] == "[laugh]\n\nRain on the window"


def test_delivery_tool_server_answers_the_native_protocol():
    listed = transport.delivery_tool_response({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["reply_without_voice"]
    called = transport.delivery_tool_response({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "reply_without_voice", "arguments": {"content": "x"}}})
    assert called == {"jsonrpc": "2.0", "id": 3, "result": {
        "content": [{"type": "text", "text": NATIVE_TEXT_DELIVERY_TOOL_RESULT}]}}
    started = transport.delivery_tool_response({"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"}})
    assert started["result"]["protocolVersion"] == "2025-03-26"
    assert started["result"]["serverInfo"]["name"] == NATIVE_TEXT_DELIVERY_SERVER
    assert transport.delivery_tool_response({"jsonrpc": "2.0", "method": "notifications/initialized"}) == {
        "jsonrpc": "2.0", "result": {}}


PEER = r'''
import sys,json
from pathlib import Path
def emit(v): print(json.dumps(v),flush=True)
pending=None
for line in sys.stdin:
 e=json.loads(line)
 if e['type']=='control_request':
  r=e['request'];sub=r['subtype'];response={}
  if sub=='initialize':Path('initialize.json').write_text(json.dumps(r))
  if sub=='apply_flag_settings':Path('settings.json').write_text(json.dumps(r['settings']))
  if sub=='reload_output_styles':response={'available_output_styles':['xperfect-application']}
  if sub=='mcp_set_servers':
   if r['servers']:Path('turn-connections.json').write_text(json.dumps(r['servers']))
   response={'errors':{}}
  emit({'type':'control_response','response':{'subtype':'success','request_id':e['request_id'],'response':response}})
 elif e['type']=='control_response':
  Path('tool-reply.json').write_text(json.dumps(e['response']))
  emit({'type':'result','session_id':'synthetic-session','result':'done','is_error':False})
 elif e['type']=='user':
  emit({'type':'control_request','request_id':'peer-1','request':{'subtype':'mcp_message',
        'server_name':'xperfect-delivery','message':{'jsonrpc':'2.0','id':7,'method':'tools/call',
        'params':{'name':'reply_without_voice','arguments':{'content':'x'}}}}})
'''


def _prepared(tmp_path, monkeypatch, surface, *, reuse=True):
    monkeypatch.delenv('VIVENTIUM_NATIVE_FIRST_ADMIN_STATE', raising=False)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / 'runtime'))
    runtime.set_run_start_observer(lambda _payload: None)
    monkeypatch.setattr(runtime, '_native_context_hooks_supported', lambda: True)
    monkeypatch.setattr(runtime, '_assert_host_claude_mcp_config', lambda *_: None)
    peer = tmp_path / 'peer.py'
    peer.write_text(PEER)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    capabilities = {'native_tools': False}
    if reuse:
        capabilities['conversation_process_reuse'] = True
    worker = {'worker_id': 'synthetic-call', 'profile': 'claude-code', 'execution_mode': 'host',
              'model': 'claude-opus-5', 'workspace_root': str(workspace),
              '_active_run_id': 'run-1', '_run_attempt_id': 'attempt-1',
              'trusted_run_lane': 'conversation', 'bootstrap_bundle_json': json.dumps({
                  'run_mode': 'conversation', 'developer_instructions': 'Current context',
                  'surface': surface, 'messaging_delivery_control': DELIVERY,
                  'provider_capabilities': capabilities})}
    state = runtime._state_dir(worker['worker_id'])
    state.mkdir(parents=True)
    (state / 'conversation-mcp.json').write_text('{"mcpServers":{}}')
    command = [sys.executable, str(peer), '--settings', '{"permissions":{"allow":["mcp__broker__hand_off_to"]}}',
               '--setting-sources', '', '--no-session-persistence',
               '--mcp-config', str(state / 'conversation-mcp.json')]
    return runtime, worker, workspace, command


def test_live_call_turn_declares_serves_and_releases_the_text_only_tool(tmp_path, monkeypatch):
    runtime, worker, workspace, command = _prepared(tmp_path, monkeypatch, 'voice')
    root = runtime._run_root(worker['worker_id'], 'run-1')
    root.mkdir(parents=True, exist_ok=True)
    try:
        assert transport.run_turn(runtime, worker, command, dict(os.environ), workspace, root, 'hello', 5, {}) == 0
    finally:
        runtime.close_persistent_conversations()
    assert json.loads((workspace / 'initialize.json').read_text())['sdkMcpServers'] == ['xperfect-delivery']
    assert json.loads((workspace / 'turn-connections.json').read_text()) == {
        'xperfect-delivery': {'type': 'sdk', 'name': 'xperfect-delivery'}}
    assert json.loads((workspace / 'settings.json').read_text())['permissions']['allow'] == [
        'mcp__broker__hand_off_to', TOOL]
    reply = json.loads((workspace / 'tool-reply.json').read_text())
    assert reply['subtype'] == 'success' and reply['request_id'] == 'peer-1'
    assert reply['response']['mcp_response']['result']['content'][0]['text'] == NATIVE_TEXT_DELIVERY_TOOL_RESULT
    # The host answers on the control channel; the exchange is not public native output.
    assert 'mcp_message' not in (root / 'stdout.log').read_text()


@pytest.mark.parametrize("surface,reuse,expects_schema", [
    ("voice", True, False),
    ("voice", False, True),
    ("telegram", True, True),
])
def test_only_the_retained_live_call_drops_the_forced_structured_result(
        tmp_path, monkeypatch, surface, reuse, expects_schema):
    runtime, worker, _workspace, _command = _prepared(tmp_path, monkeypatch, surface, reuse=reuse)
    try:
        schema = runtime._conversation_output_schema(worker)
    finally:
        runtime.close_persistent_conversations()
    assert (schema is not None) is expects_schema
    if schema is not None:
        assert set(schema["properties"]) == {"type", "tool_name", "voice", "content"}
