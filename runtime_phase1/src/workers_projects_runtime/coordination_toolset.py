"""Two names over existing owner-authorized work tools; no scheduler or execution loop."""
from dataclasses import dataclass, field, replace
from pathlib import Path
import hashlib
import json

from pydantic import TypeAdapter
from pydantic_ai.toolsets.wrapper import WrapperToolset

START = 'worker_delegate_once_mcp_glasshive-workers-projects'
CONTROL = 'active_work_action'
ROSTER = 'active_work_list'
ACTIONS = {'start', 'queue', 'message', 'steer', 'pause', 'resume', 'stop', 'retry', 'dismiss'}


def coordination_tool_manifest():
    path = Path(__file__).with_name("prompts") / "coordination-tools.json"
    raw = path.read_bytes()
    return json.loads(raw) | {
        "source": "workers_projects_runtime/prompts/coordination-tools.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@dataclass
class CoordinationToolset(WrapperToolset):
    _original: dict = field(default_factory=dict, init=False)

    async def get_tools(self, ctx):
        available = await self.wrapped.get_tools(ctx)
        self._original = {name: available[name] for name in (START, CONTROL, ROSTER) if name in available}
        result = {}
        if ROSTER in self._original:
            original = self._original[ROSTER]
            result['list_workers'] = replace(original, toolset=self,
                tool_def=replace(original.tool_def, name='list_workers'))
        start = self._original.get(START)
        control = self._original.get(CONTROL)
        if start is None and control is None:
            return result
        # An old broker cannot honor exact-source handoff; retain no invented brief fallback.
        if any(original and 'inheritContext' not in original.tool_def.parameters_json_schema.get('properties', {})
               for original in (start, control)):
            raise ValueError('coordination_source_binding_unavailable')
        template = start or control
        start_schema = start.tool_def.parameters_json_schema if start else {}
        control_schema = control.tool_def.parameters_json_schema if control else {}
        start_properties = start_schema.get('properties', {})
        control_properties = control_schema.get('properties', {})
        permitted = {'workRef', 'sourceOrdinals', 'fileIds', 'nativeInput', 'instruction'}
        properties = {key: value for key, value in control_properties.items() if key in permitted}
        # Current input bounds are common; historical-file choices are start-only.
        if start:
            if 'sourceOrdinals' in start_properties:
                properties['sourceOrdinals'] = {**start_properties['sourceOrdinals'], 'description':
                    'Select the exact current input segments for start, queue, message, steer or retry. Required when there is more than one current input.'}
            if 'fileIds' in start_properties:
                properties['fileIds'] = {**(control_properties.get('fileIds') or start_properties['fileIds']),
                    'description': ('Required for start. ' if 'fileIds' in start_schema.get('required', []) else '') +
                        'For start, select the listed historical files or [] for none; current uploads remain included. For queue/message/steer, select authorized files for that exact work.'}
        manifest = coordination_tool_manifest()
        instruction_schema = (start_properties.get('instruction') or control_properties.get('instruction'))
        if instruction_schema:
            properties['instruction'] = {**instruction_schema, 'description': manifest['instruction_selection']}
        actions = sorted(({'start'} if start else set()) | (ACTIONS - {'start'} if control else set()))
        properties['action'] = {'type': 'string', 'enum': actions}
        schema = {'type': 'object', 'properties': properties,
                  'required': ['action'], 'additionalProperties': False}
        conditions = []
        if start:
            required = [name for name in start_schema.get('required', []) if name in permitted]
            start_fields = {key: start_properties[key] for key in ('fileIds', 'sourceOrdinals') if key in start_properties}
            conditions.append({'if': {'properties': {'action': {'const': 'start'}}},
                               'then': {'properties': start_fields, 'required': required}})
        if control:
            conditions.append({'if': {'properties': {'action': {'enum': [action for action in actions if action != 'start']}}},
                               'then': {'required': ['workRef']}})
        if conditions:
            schema['allOf'] = conditions
        description = manifest['tools']['hand_off_to']
        result['hand_off_to'] = replace(template, toolset=self,
            tool_def=replace(template.tool_def, name='hand_off_to', description=description,
                             parameters_json_schema=schema),
            args_validator=TypeAdapter(dict[str, object]).validator)
        return result

    async def call_tool(self, name, tool_args, ctx, tool):
        if name == 'list_workers':
            target, args = ROSTER, tool_args
        elif name == 'hand_off_to':
            if set(tool_args) - {'action', 'workRef', 'sourceOrdinals', 'fileIds', 'nativeInput', 'instruction'}:
                raise ValueError('coordination_arguments_invalid')
            action = tool_args.get('action')
            if action not in ACTIONS:
                raise ValueError('coordination_action_invalid')
            target = START if action == 'start' else CONTROL
            declared = self._original.get(target)
            if 'instruction' in tool_args and (
                declared is None or action not in {'start', 'queue', 'message', 'steer', 'retry'} or
                'instruction' not in declared.tool_def.parameters_json_schema.get('properties', {})
            ):
                raise ValueError('coordination_instruction_unavailable')
            args = {key: value for key, value in tool_args.items() if key != 'action'}
            if target == START:
                if 'workRef' in args or 'nativeInput' in args:
                    raise ValueError('coordination_start_arguments_invalid')
                args['inheritContext'] = True
            else:
                if not args.get('workRef'):
                    raise ValueError('coordination_work_identity_required')
                args['action'] = action
                if action in {'queue', 'message', 'steer', 'retry'}:
                    args['inheritContext'] = True
        else:
            raise ValueError('coordination_tool_unavailable')
        original = self._original.get(target)
        if original is None:
            raise ValueError('coordination_action_unavailable')
        # The existing broker owns validation, current source authority, idempotency and receipt.
        return await self.wrapped.call_tool(target, args, replace(ctx, tool_name=target), original)
