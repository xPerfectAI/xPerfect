"""Typed ACP projection of the already-authorized bootstrap and native events."""
from __future__ import annotations
import copy
import re


def mcp_servers_for_bundle(bundle, environment):
    """Preserve the existing broker grant; never discover extra server authority."""
    from .bootstrap import claude_project_mcp_payload_for_bundle
    removed = {
        name
        for name in bundle.get('_configured_removed_mcp_servers', [])
        if isinstance(name, str)
    }
    result = []
    explicit_names = set()
    explicit = bundle.get('grok_mcp_servers')
    if explicit is not None:
        if not isinstance(explicit, list) or any(not isinstance(item, dict) for item in explicit):
            raise ValueError('grok_mcp_servers must contain native ACP MCP server objects')
        for item in explicit:
            name = item.get('name')
            if isinstance(name, str) and name in removed:
                continue
            if isinstance(name, str):
                explicit_names.add(name)
            if item.get('disabled') is True or item.get('enabled') is False:
                continue
            result.append(copy.deepcopy(item))
    config = bundle.get('claude_project_mcp')
    if not isinstance(config, dict):
        return result
    config = claude_project_mcp_payload_for_bundle(bundle, copy.deepcopy(config))
    def expand(value):
        if not isinstance(value, str):
            raise ValueError('MCP configuration values must be strings')
        def replacement(match):
            key = match.group(1)
            if key not in environment:
                raise ValueError('Grok MCP configuration references an unavailable environment variable')
            return str(environment[key])
        return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', replacement, value)
    servers=config.get('mcpServers', {})
    if not isinstance(servers, dict):
        raise ValueError('MCP server configuration must be an object')
    for name, server in servers.items():
        if name in removed or name in explicit_names:
            continue
        if not isinstance(server, dict):
            raise ValueError('MCP server configuration must be an object')
        if server.get('disabled') is True or server.get('enabled') is False:
            continue
        if server.get('url'):
            transport=server.get('type', 'http')
            if transport not in ('http','sse'):
                raise ValueError('Grok MCP transport is unsupported')
            result.append({'name':name,'type':transport,'url':expand(server['url']),
                'headers':[{'name':key,'value':expand(value)} for key,value in server.get('headers',{}).items()]})
        elif server.get('command'):
            result.append({'name':name,'command':expand(server['command']),
                'args':[expand(value) for value in server.get('args',[])],
                'env':[{'name':key,'value':expand(value)} for key,value in server.get('env',{}).items()]})
        else:
            raise ValueError('Grok MCP server has no transport')
    return result


def native_events(value, *, observed_at=None):
    from .native_team import _session_event, _clean_ref, _clean_role, _observed_at, _event
    event_type = value.get('type')
    if event_type in ('grok.permission.requested', 'grok.permission.response_submitted'):
        session_id = _clean_ref(value.get('session_id'))
        request_id = _clean_ref(value.get('request_id'))
        if not session_id or not request_id:
            return []
        if event_type == 'grok.permission.requested':
            method = _clean_ref(value.get('method'), max_length=128)
            if method not in {
                'session/request_permission',
                'x.ai/ask_user_question',
                'x.ai/exit_plan_mode',
                'x.ai/mcp/elicit',
            }:
                return []
            return [_event('provider.native.input.requested', {
                'sessionId': session_id,
                'requestId': request_id,
                'method': method,
                'observedAt': _observed_at(observed_at),
            })]
        outcome = _clean_ref(value.get('outcome'), max_length=32).lower()
        if outcome not in {'selected', 'cancelled'}:
            return []
        return [_event('provider.native.input.resolved', {
            'sessionId': session_id,
            'requestId': request_id,
            'outcome': outcome,
            'observedAt': _observed_at(observed_at),
        })]
    if value.get('type')=='grok.session.started':
        return _session_event(value.get('session_id'), observed_at=observed_at)
    if value.get('type')!='grok.session.update' or not isinstance(value.get('update'),dict):
        return []
    update=value['update']; kind=update.get('sessionUpdate')
    if kind not in ('subagent_spawned','subagent_progress','subagent_finished'):
        return []
    child=_clean_ref(update.get('child_session_id'))
    if not child or child!=_clean_ref(update.get('subagent_id')):
        return []
    if kind!='subagent_finished' and update.get('parent_session_id')!=value.get('session_id'):
        return []
    state='running'
    event_type='provider.child.started' if kind=='subagent_spawned' else 'provider.child.updated'
    if kind=='subagent_finished':
        state={'completed':'completed','failed':'failed','cancelled':'stopped'}.get(update.get('status'))
        if not state:
            return []
        event_type='provider.child.'+state
    payload={'childRef':child,'state':state,'observedAt':_observed_at(observed_at)}
    metadata=value.get('meta')
    event_ref=_clean_ref(metadata.get('eventId')) if isinstance(metadata,dict) else ''
    if event_ref:
        payload['providerEventRef']=event_ref
    if kind=='subagent_spawned':
        payload['role']=_clean_role(update.get('role') or update.get('subagent_type'))
    return [_event(event_type,payload)]
