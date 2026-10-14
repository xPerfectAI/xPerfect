"""ACP subprocess bridge executed by the existing native run supervisor.

Kept standard-library-only so the same reviewed bytes run in host and container
workstations. Its stdout is typed native evidence; instruction bytes arrive on
stdin and never enter argv. This module does not own scheduling or run leases.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from threading import Lock

try:
    from .grok_acp import AcpClient, AcpError, GrokSession
    from .grok_control import ControlMailbox
    from .agent_builder_control import conversation_interjection_supported, conversation_controls_from_schema, conversation_output_contract_instructions, minimal_conversation_output_schema
except ImportError:  # Private, materialized standalone runtime copy.
    from grok_acp import AcpClient, AcpError, GrokSession
    from grok_control import ControlMailbox
    from agent_builder_control import conversation_interjection_supported, conversation_controls_from_schema, conversation_output_contract_instructions, minimal_conversation_output_schema


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--session-id')
    parser.add_argument('--effort')
    parser.add_argument('--yolo-mode', choices=('true', 'false'))
    parser.add_argument('--native-tools-disabled', action='store_true')
    parser.add_argument('--restricted-workspace')
    parser.add_argument('--mcp-file')
    parser.add_argument('--required-mcp-server', action='append', default=[])
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument('--rules-file')
    authority.add_argument('--system-prompt-file')
    parser.add_argument('--output-schema-file')
    parser.add_argument('--output-schema-minimal-wire', action='store_true')
    parser.add_argument('--allow-mcp-tool', action='append', default=[])
    parser.add_argument('--control-dir')
    parser.add_argument('--run-id', default='')
    parser.add_argument('--attempt-id', default='')
    parser.add_argument('--permission-deadline-at', type=float)
    parser.add_argument('--reviewed-binary-sha256')
    parser.add_argument('--managed-home', action='store_true')
    args = parser.parse_args(argv)
    home = Path(os.environ['GROK_HOME'])
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if home.is_symlink() or not home.is_dir():
        raise ValueError("Grok home must be a real directory")
    if not args.managed_home:
        home.chmod(0o700)
    # A projected home is service-owned; its existing per-member ACL is the
    # authority. The native UID must not replace that ACL with chmod.
    # Serialize across service processes as well as threads; never use "latest".
    with (home / 'xperfect-session.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Grok native session is already in use', file=sys.stderr)
            return 2
        return run(args)


def _stopped_turn(exc, session, mailbox):
    # A cancelled turn is explained from typed state, never from Grok's text.
    if exc.stop_reason == 'mcp_unavailable':
        return 'native_mcp_unavailable', 'Configured Grok MCP tools did not become ready before prompting.'
    if exc.stop_reason != 'cancelled':
        return None, str(exc)
    if session.cancel_requested or (mailbox and mailbox.turn_cancelled()):
        return 'native_turn_cancelled', 'Grok stopped because its turn was cancelled.'
    outcome = mailbox.terminal_cause() if mailbox else None
    if outcome == 'expired':
        if getattr(mailbox, 'permission_deadline_at', None) is not None:
            return 'native_input_expired', 'Grok stopped because its request for your response reached the mission authorization deadline.'
        return 'native_input_expired', ('Grok stopped because its request for your response expired after '
                                        f'{mailbox.permission_timeout:g} seconds without an answer.')
    if outcome == 'dismissed':
        return 'native_input_cancelled', 'Grok stopped because you cancelled its request.'
    if outcome == 'declined':
        return 'native_input_declined', 'Grok stopped after you declined its request.'
    return None, str(exc)


def run(args):
    output_lock = Lock()
    started_at = time.monotonic()
    def emit(event):
        with output_lock:
            observation = {**event,
                           'observed_at': datetime.now(timezone.utc).isoformat(),
                           'elapsed_ms': round((time.monotonic() - started_at) * 1000, 3)}
            print(json.dumps(observation, ensure_ascii=False, separators=(',', ':')), flush=True)
    restricted = getattr(args, 'native_tools_disabled', False)
    child_cwd = os.getcwd()
    if restricted:
        workspace = Path(getattr(args, 'restricted_workspace', '') or '')
        if (args.session_id or not workspace.is_absolute() or workspace.is_symlink()
                or not workspace.is_dir() or workspace.resolve() != workspace
                or workspace.stat().st_mode & 0o777 != 0o700
                or any(workspace.iterdir())):
            raise ValueError('Restricted Grok requires a fresh empty native workspace and session')
        child_cwd = str(workspace)
    mcp_servers = [] if restricted else (json.loads(Path(args.mcp_file).read_text()) if args.mcp_file else [])
    instruction = sys.stdin.read()
    rules = Path(args.rules_file).read_text() if args.rules_file else None
    system_prompt_file = getattr(args, 'system_prompt_file', None)
    system_prompt = Path(system_prompt_file).read_text() if system_prompt_file else None
    schema_file = getattr(args, 'output_schema_file', None)
    output_schema = json.loads(Path(schema_file).read_text()) if schema_file else None
    if output_schema is not None and not isinstance(output_schema, dict):
        raise ValueError('Grok output schema must be an object')
    minimal_wire = getattr(args, 'output_schema_minimal_wire', False)
    if minimal_wire and (restricted or output_schema is None or not system_prompt):
        raise ValueError('Grok minimal-wire output contract requires native tools and composed conversation authority')
    if minimal_wire:
        if not any(conversation_controls_from_schema(output_schema)):
            raise ValueError('Grok minimal-wire output contract requires declared conversation controls')
        if conversation_output_contract_instructions(output_schema) not in system_prompt:
            raise ValueError('Grok minimal-wire output contract is missing from composed conversation authority')
    emit({'type': 'grok.output_contract', 'mode': 'minimal_wire_constraint' if minimal_wire else 'native_constraint' if output_schema is not None else 'none',
          **({'declared_schema_sha256': hashlib.sha256(json.dumps(output_schema, sort_keys=True,
              separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
              'wire_schema_sha256': hashlib.sha256(json.dumps(
                  minimal_conversation_output_schema(output_schema) if minimal_wire else output_schema,
                  sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()}
              if output_schema is not None else {})})
    reviewed_interject = False
    interject_reason = 'artifact_unreviewed'
    if args.reviewed_binary_sha256:
        binary_path = shutil.which(args.binary)
        try:
            with Path(binary_path).open('rb') if binary_path else open(os.devnull, 'rb') as artifact:
                reviewed_interject = bool(binary_path and hashlib.file_digest(artifact, 'sha256').hexdigest()
                                          == args.reviewed_binary_sha256)
        except OSError:
            reviewed_interject = False
        interject_reason = 'reviewed_artifact' if reviewed_interject else 'artifact_mismatch'
    emit({'type': 'grok.native_control.capability', 'action': 'interject',
          'enabled': reviewed_interject, 'reason': interject_reason})
    emit({'type':'grok.runtime.phase','phase':'runner_started'})
    try:
        process = subprocess.Popen([args.binary, 'agent', '--no-leader', '--model', args.model, 'stdio'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                                   **({'cwd': child_cwd} if restricted else {}))
        emit({'type':'grok.runtime.phase','phase':'process_started'})
    except OSError:
        emit({'type':'grok.error','failure_class':'runtime_dependency_missing',
              'message':'The configured Grok Build executable could not start'})
        print('The configured Grok Build executable could not start', file=sys.stderr)
        return 2
    with AcpClient(process, request_timing=lambda timing: emit({'type': 'grok.acp.rpc', **timing})) as client:
        session = GrokSession(client, event=emit, reviewed_interject=reviewed_interject)
        mailbox = None
        old_handlers = {}
        def cancel(signum, _frame):
            try:
                (mailbox.cancel_turn if mailbox else session.cancel)()
            except AcpError:
                pass
            # The enclosing exact-generation supervisor owns bounded escalation.
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, cancel)
        try:
            emit({'type':'grok.runtime.phase','phase':'session_open_started'})
            session.open(cwd=child_cwd, model=args.model, session_id=args.session_id,
                         effort=args.effort, mcp_servers=mcp_servers, required_mcp_servers=args.required_mcp_server, rules=rules,
                         system_prompt_override=system_prompt,
                         yolo_mode=({'true': True, 'false': False}.get(getattr(args, 'yolo_mode', None))),
                         auth_method='xai.api_key' if os.environ.get('XAI_API_KEY') else None,
                         native_tools_disabled=restricted)
            if args.control_dir:
                mailbox = ControlMailbox(args.control_dir, run_id=args.run_id, attempt_id=args.attempt_id,
                                         session=session, event=emit, managed_acl=args.managed_home,
                                         auto_allow_tools=frozenset() if restricted else frozenset(args.allow_mcp_tool),
                                         permission_deadline_at=getattr(args, 'permission_deadline_at', None))
                if not restricted:
                    client.permission = mailbox.permission
                    client.interaction = mailbox.interaction
                # The mailbox deadline decides; the client timeout is only a backstop.
                client.permission_timeout = mailbox.permission_timeout + 5
                mailbox.start()
            emit({'type':'grok.session.started','session_id':session.session_id,
                  'model':session.configured_model, 'requested_model':args.model, 'protocol_version':1,
                  'native_control_capabilities': {'interject': reviewed_interject and conversation_interjection_supported(output_schema)},
                  'agent_version':session.initialization.get('_meta', {}).get('agentVersion'),
                  'config_options':session.session_state.get('configOptions', []),
                  **({'input_authority_remaining_seconds':round(mailbox.permission_timeout, 3)}
                     if mailbox and mailbox.permission_deadline_at is not None else {})})
            if session.cancel_requested:
                raise AcpError('Grok turn was cancelled before prompting', stop_reason='cancelled')
            emit({'type':'grok.runtime.phase','phase':'prompt_started'})
            output = session.prompt(instruction, output_schema=output_schema, minimal_wire_output=minimal_wire)
            unanswered_question = mailbox and mailbox.finish_unanswered_question()
            if session.cancel_requested or (mailbox and mailbox.turn_cancelled()):
                raise AcpError('Grok turn was cancelled while prompting', stop_reason='cancelled')
            if unanswered_question:
                message = 'Grok ended before an owner response was recorded.'
                emit({'type':'grok.error','session_id':session.session_id,'code':None,
                      'failure_class':'native_input_unanswered','message':message})
                print(message, file=sys.stderr)
                session.close()
                return 2
            ranges = session.delivery_disposition_ranges()
            emit({'type':'grok.result','session_id':session.session_id,
                  'stop_reason':'end_turn','output':output,
                  **({'usage': session.native_usage} if session.native_usage is not None else {}),
                  **({'deliveryDispositionRanges': ranges} if ranges else {})})
            session.close()
            return 0
        except AcpError as exc:
            failure_class, message = _stopped_turn(exc, session, mailbox)
            emit({'type':'grok.error','session_id':session.session_id,'code':exc.code,
                  **({'failure_class':failure_class} if failure_class else {}), 'message':message})
            print(message, file=sys.stderr)
            return 2
        finally:
            if mailbox:
                mailbox.close()
            for signum, previous in old_handlers.items():
                signal.signal(signum, previous)


if __name__ == '__main__':
    raise SystemExit(main())
