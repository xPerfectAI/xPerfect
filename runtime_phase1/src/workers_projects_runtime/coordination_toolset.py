"""Two names over existing owner-authorized work tools; no scheduler or execution loop."""
from dataclasses import dataclass, field, replace

from pydantic import TypeAdapter
from pydantic_ai.toolsets.wrapper import WrapperToolset

START = 'worker_delegate_once_mcp_glasshive-workers-projects'
CONTROL = 'active_work_action'
ROSTER = 'active_work_list'
ACTIONS = {'start', 'queue', 'message', 'steer', 'pause', 'resume', 'stop', 'retry', 'dismiss'}


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
        permitted = {'workRef', 'sourceOrdinals', 'fileIds', 'nativeInput'}
        properties = {key: value for key, value in control_properties.items() if key in permitted}
        # Current input bounds are common; historical-file choices are start-only.
        if start:
            if 'sourceOrdinals' in start_properties:
                properties['sourceOrdinals'] = {**start_properties['sourceOrdinals'], 'description':
                    'Select the exact current input segments for start, queue, message, steer or retry. Required when there is more than one current input.'}
            if 'fileIds' in start_properties:
                properties['fileIds'] = {**(control_properties.get('fileIds') or start_properties['fileIds']),
                    'description': 'For start, select the listed historical files or [] for none; current uploads remain included. For queue/message/steer, select authorized files for that exact work.'}
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
        description = (
            'Perform action work through the configured background workers. Start accepts selected input as '
            'one background assignment; select sourceOrdinals for start and guidance actions when multiple current inputs exist. '
            'The runtime gives the worker the original selected input and inherited conversation; do not '
            'rewrite a brief. For existing work use its exact workRef and an available action: queue, message, '
            'steer, pause, resume, stop, retry or dismiss. Use list_workers only when the target or action '
            'is unclear. The original current input supplies guidance for queue, message, steer and retry. '
            'nativeInput answers an exact pending native question or permission using resume. '
            'Acknowledge work naturally only after the receipt confirms acceptance; do not wait for completion.'
        )
        result['hand_off_to'] = replace(template, toolset=self,
            tool_def=replace(template.tool_def, name='hand_off_to', description=description,
                             parameters_json_schema=schema),
            args_validator=TypeAdapter(dict[str, object]).validator)
        return result

    async def call_tool(self, name, tool_args, ctx, tool):
        if name == 'list_workers':
            target, args = ROSTER, tool_args
        elif name == 'hand_off_to':
            if set(tool_args) - {'action', 'workRef', 'sourceOrdinals', 'fileIds', 'nativeInput'}:
                raise ValueError('coordination_arguments_invalid')
            action = tool_args.get('action')
            if action not in ACTIONS:
                raise ValueError('coordination_action_invalid')
            target = START if action == 'start' else CONTROL
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
