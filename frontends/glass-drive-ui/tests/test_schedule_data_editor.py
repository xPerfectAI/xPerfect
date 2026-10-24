"""Plain message editing keeps the app's other structured data intact."""
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src' / 'glass_drive_ui' / 'static'


def test_message_and_advanced_fields_preserve_app_request_contract():
    module = (STATIC / 'schedule-data.js').as_uri()
    script = f"""
      import {{ parseRequestData, withRequestMessage, requestDataRows, requestDataFromRows }} from {json.dumps(module)};
      const original = {{ message: 'Old message', request_ref: 'opaque', nested: {{ items: [1, 2], enabled: true }} }};
      const changed = withRequestMessage(JSON.stringify(original), 'New message', 'Old message');
      if (changed.message !== 'New message' || JSON.stringify(changed.nested) !== JSON.stringify(original.nested) || changed.request_ref !== 'opaque') throw Error('Message edit lost app context');
      if ('message' in withRequestMessage(JSON.stringify(original), '', 'Old message')) throw Error('Cleared message was retained');
      const unusual = {{ message: {{ custom: true }}, request_ref: 'opaque' }};
      if (JSON.stringify(withRequestMessage(JSON.stringify(unusual), '', '')) !== JSON.stringify(unusual)) throw Error('Untouched custom message was rewritten');
      if (requestDataRows(original) !== null) throw Error('Nested app data was flattened');
      const fields = requestDataFromRows([
        {{ key: 'text', type: 'string', value: 'hello' }}, {{ key: 'count', type: 'number', value: '2.5' }},
        {{ key: 'enabled', type: 'boolean', value: 'false' }}, {{ key: 'optional', type: 'null', value: '' }},
        {{ key: '__proto__', type: 'string', value: 'app-data' }},
      ]);
      if (fields.count !== 2.5 || fields.enabled !== false || fields.optional !== null || fields.__proto__ !== 'app-data') throw Error('Typed fields changed values');
      if (Object.getPrototypeOf(fields) !== Object.prototype) throw Error('Field name changed object authority');
      const restored = requestDataFromRows(requestDataRows(fields));
      if (JSON.stringify(restored) !== JSON.stringify(fields)) throw Error('Primitive field roundtrip changed data');
      for (const raw of ['[]', 'null', 'broken']) {{ let failed=false; try {{ parseRequestData(raw); }} catch (_) {{ failed=true; }} if (!failed) throw Error('Invalid app object accepted'); }}
      for (const rows of [
        [{{key:'a',type:'number',value:'n/a'}}], [{{key:'a',type:'boolean',value:'yes'}}],
        [{{key:'',type:'string',value:'orphan'}}], [{{key:'a',type:'string',value:'x'}},{{key:'a',type:'string',value:'y'}}],
      ]) {{ let failed=false; try {{ requestDataFromRows(rows); }} catch (_) {{ failed=true; }} if (!failed) throw Error('Invalid extra detail silently changed'); }}
    """
    result = subprocess.run(['node', '--input-type=module', '--eval', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_main_form_is_plain_and_connection_setup_is_a_native_dialog():
    from html.parser import HTMLParser

    class Structure(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.nodes = [], {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get('id'):
                self.nodes[attrs['id']] = (tag, attrs, list(self.stack))
            if tag not in {'input', 'meta', 'link', 'br', 'hr', 'img'}:
                self.stack.append((tag, attrs.get('id')))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    self.stack = self.stack[:index]
                    break

    page = Structure()
    page.feed((STATIC / 'index.html').read_text())
    nodes = page.nodes
    assert ('form', 'recurring-schedule-form') in nodes['recurring-schedule-message'][2]
    raw_parents = nodes['recurring-schedule-request-data'][2]
    assert ('details', 'schedule-data-json') in raw_parents
    assert ('details', 'schedule-request-advanced') in raw_parents
    assert nodes['webhook-destination-setup'][0] == 'dialog'
    assert nodes['webhook-destination-setup'][1]['aria-labelledby'] == 'webhook-destination-title'
    for field in ('webhook-destination-name', 'webhook-destination-url', 'webhook-destination-secret', 'webhook-destination-headers'):
        assert ('form', 'recurring-schedule-form') not in nodes[field][2]
        assert ('dialog', 'webhook-destination-setup') in nodes[field][2]
    for field in ('webhook-destination-secret', 'webhook-destination-headers', 'webhook-destination-mode'):
        assert ('details', 'webhook-destination-advanced') in nodes[field][2]
    assert nodes['schedule-preview'][1]['aria-live'] == 'polite'


def test_delivered_form_collects_plain_message_without_losing_complex_context():
    module = (STATIC / 'schedule-data.js').as_uri()
    source_path = str(STATIC / 'control-plane.js')
    script = f"""
      import fs from 'node:fs'; import vm from 'node:vm';
      import * as helpers from {json.dumps(module)};
      const original = {{ message: 'Old', request_ref: 'opaque', context: {{ items: ['a', 'b'], version: 3 }} }};
      const nodes = {{
        'recurring-schedule-request-data': {{ value: JSON.stringify(original) }},
        'recurring-schedule-message': {{ value: 'New', dataset: {{ originalValue: 'Old' }} }},
        'schedule-data-fields': {{ dataset: {{ mode: 'json' }} }},
        'schedule-data-status': {{ textContent: '' }},
        'webhook-destination-setup': {{ open: true, close() {{ this.open = false; }} }},
        'webhook-destination-secret': {{ value: 'synthetic-private' }},
        'webhook-destination-headers': {{ value: 'synthetic-private' }},
        'webhook-destination-url': {{ value: 'https://private.invalid/' }},
        'webhook-destination-secret-once': {{ textContent: 'synthetic-private', hidden: false }},
        'webhook-destination-secret-panel': {{ hidden: false }},
      }};
      const context = vm.createContext({{ ...helpers, Map, Set, console, document: {{ getElementById: (id) => nodes[id] || null }} }});
      let source = fs.readFileSync({json.dumps(source_path)}, 'utf8');
      source = source.replace(/^import[\\s\\S]*?from\\s+['"][^'"]+['"];\\n/gm, '').replace(/^export /gm, '');
      vm.runInContext(source, context);
      const collected = vm.runInContext('collectRequestData()', context);
      if (collected.message !== 'New' || collected.request_ref !== 'opaque' || JSON.stringify(collected.context) !== JSON.stringify(original.context)) throw Error('Delivered serializer lost full app context');
      vm.runInContext('syncRequestMessage()', context);
      if (JSON.parse(nodes['recurring-schedule-request-data'].value).message !== 'New') throw Error('Plain input did not update submitted data');
      nodes['recurring-schedule-request-data'].value = 'invalid';
      let failed = false; try {{ vm.runInContext('collectRequestData()', context); }} catch (_) {{ failed = true; }}
      if (!failed) throw Error('Invalid advanced data silently submitted');
      vm.runInContext('closeDestinationDialog()', context);
      for (const id of ['webhook-destination-secret', 'webhook-destination-headers', 'webhook-destination-url']) if (nodes[id].value) throw Error('Dialog close retained private data');
      if (nodes['webhook-destination-secret-once'].textContent || !nodes['webhook-destination-secret-panel'].hidden || nodes['webhook-destination-setup'].open) throw Error('Creation key remained visible after close');
    """
    result = subprocess.run(['node', '--input-type=module', '--eval', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_empty_workspace_guard_and_tomorrow_preview_use_the_visible_controls():
    policy_module = (STATIC / 'schedule-policy.js').as_uri()
    source_path = str(STATIC / 'control-plane.js')
    script = f"""
      import fs from 'node:fs'; import vm from 'node:vm'; import * as policy from {json.dumps(policy_module)};
      class Element {{
        constructor(value='') {{ this.value=value; this.hidden=false; this.children=[]; this.dataset={{}}; this.classList={{ toggle() {{}} }}; }}
        replaceChildren(...children) {{ this.children=children; }}
        setAttribute() {{}}
      }}
      const nodes = {{}};
      for (const id of ['recurring-schedule-workspace','recurring-schedule-instruction-field','recurring-schedule-fields','recurring-schedule-advanced','recurring-schedule-submit','schedule-when-heading','schedule-preview','recurring-schedule-workspace-empty','recurring-schedule-once-field','recurring-schedule-webhook-fields','recurring-schedule-workspace-field','recurring-schedule-cancel-edit','schedule-create-card']) nodes[id]=new Element();
      nodes['recurring-schedule-target']=new Element('worker');
      nodes['recurring-schedule-type']=new Element('once');
      nodes['recurring-schedule-timezone']=new Element('America/Toronto');
      nodes['recurring-schedule-starts-at']=new Element('');
      nodes['recurring-schedule-destination']=new Element('app');
      nodes['recurring-schedule-destination'].selectedOptions=[{{textContent:'My app'}}];
      const checked = {{ value: 'worker', focus() {{ this.focused=true; }} }};
      const context = vm.createContext({{...policy, console, Map, Set, Intl, Date,
        fetch: async () => ({{ok:true,json:async()=>({{items:[]}})}}),
        document: {{ getElementById:id=>nodes[id]||null, querySelector:(selector)=>selector.includes('schedule-target-choice')?checked:new Element(),
          querySelectorAll:()=>[checked], createElement:()=>new Element() }} }});
      let source=fs.readFileSync({json.dumps(source_path)},'utf8').replace(/^import[\\s\\S]*?from\\s+['"][^'"]+['"];\\n/gm,'').replace(/^export /gm,'');
      vm.runInContext(source,context);
      vm.runInContext('api={{withAuth:value=>value}}; workspaceCatalog={{items:[]}}; oneOffWorkspaceCatalog={{items:[]}}; syncRecurringScheduleFields();',context);
      for (const id of ['recurring-schedule-fields','recurring-schedule-advanced','recurring-schedule-submit','schedule-when-heading','schedule-preview']) if (!nodes[id].hidden) throw Error('Empty workspace exposed '+id);
      if (nodes['recurring-schedule-workspace-empty'].hidden || nodes['recurring-schedule-cancel-edit'].hidden) throw Error('Empty state hid its recovery action');
      nodes['recurring-schedule-target'].value='webhook';
      vm.runInContext('syncRecurringScheduleFields()',context);
      for (const id of ['recurring-schedule-fields','recurring-schedule-advanced','recurring-schedule-submit','schedule-when-heading','schedule-preview']) if (nodes[id].hidden) throw Error('App target incorrectly needs an AI workspace');
      vm.runInContext('useTomorrowScheduleTime()',context);
      if (!nodes['recurring-schedule-starts-at'].value || !nodes['schedule-preview'].textContent.includes('Toronto') || !nodes['schedule-preview'].textContent.includes('My app')) throw Error('Tomorrow did not update the visible summary');
      if (/T\\d{{2}}:/.test(nodes['schedule-preview'].textContent) || nodes['schedule-preview'].textContent.includes('America/')) throw Error('Preview contains raw protocol date/zone');
      await vm.runInContext('openScheduleEditor()',context);
      if (!checked.focused) throw Error('Initial focus did not reach the visible selected choice');
    """
    result = subprocess.run(['node', '--input-type=module', '--eval', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_large_json_numbers_uncertainty_and_app_instruction_are_not_rewritten():
    helpers_module = (STATIC / 'schedule-data.js').as_uri()
    source_path = str(STATIC / 'control-plane.js')
    script = f"""
      import fs from 'node:fs'; import vm from 'node:vm'; import * as helpers from {json.dumps(helpers_module)};
      const raw = '{{"message":"old","request_ref":"opaque","large":9007199254740993,"nested":{{"n":-9007199254740997}}}}';
      const exact = helpers.parseRequestData(raw);
      if (JSON.stringify(exact) !== raw || !helpers.hasPreciseNumbers(exact)) throw Error('Large integer digits changed');
      if (helpers.requestDataRows(exact) !== null) throw Error('Large number offered to a lossy field');
      let rejected = false; try {{ helpers.requestDataFromRows([{{key:'n',type:'number',value:'9007199254740993'}}]); }} catch (_) {{ rejected=true; }}
      if (!rejected) throw Error('Typed fields silently rounded a large number');
      if (helpers.effectiveRunOutcome({{state:'unacknowledged',outcome:'cancelled'}}) !== 'unacknowledged') throw Error('Cancellation concealed uncertain send');
      if (helpers.effectiveRunOutcome({{state:'accepted',outcome:'http_accepted'}}) !== 'accepted') throw Error('Known ACK was lost');
      if (helpers.effectiveRunOutcome({{state:'pending',outcome:'unacknowledged'}}) !== 'pending') throw Error('Retrying delivery shown as final unknown');
      if (helpers.effectiveRunOutcome({{state:'rejected',outcome:'http_rejected'}}) !== 'rejected') throw Error('Error code concealed rejected delivery');
      const nodes = {{
        'recurring-schedule-target': {{value:'webhook'}},
        'recurring-schedule-instruction': {{value:'stale hidden worker task'}},
        'recurring-schedule-message': {{value:'',dataset:{{}}}},
        'recurring-schedule-request-data': {{value:'{{}}'}},
        'schedule-message-label': {{textContent:''}}, 'schedule-message-note': {{textContent:'',hidden:true}},
        'recurring-schedule-status': {{textContent:'',dataset:{{}}}},
        'recurring-schedule-local-time': {{focus() {{ this.focused=true; }}}},
        'recurring-schedule-destination': {{value:'',focus() {{ this.focused=true; }}}},
      }};
      const context=vm.createContext({{...helpers,Map,Set,console,document:{{getElementById:id=>nodes[id]||null}}}});
      vm.runInContext(fs.readFileSync({json.dumps(source_path)},'utf8').replace(/^import[\\s\\S]*?from\\s+['"][^'"]+['"];\\n/gm,'').replace(/^export /gm,''),context);
      vm.runInContext('setRequestData({{request_ref:"opaque",message:{{custom:true}}}}, "Saved scheduled task", true)',context);
      if (nodes['recurring-schedule-message'].value !== 'Saved scheduled task') throw Error('Saved app instruction was hidden');
      nodes['recurring-schedule-message'].value='Edited scheduled task';
      if (vm.runInContext('effectiveScheduleInstruction()',context) !== 'Edited scheduled task') throw Error('App instruction edit was lost');
      if (vm.runInContext('collectRequestData().request_ref',context) !== 'opaque') throw Error('Task edit replaced opaque context');
      vm.runInContext('setRequestData({{message:{{custom:true}}}})',context);
      if (!nodes['recurring-schedule-message'].disabled) throw Error('Custom-format message could be overwritten');
      if (vm.runInContext('effectiveScheduleInstruction()',context)) throw Error('Hidden worker task leaked into app request');
      vm.runInContext('showScheduleFieldError({{message:"local_time invalid"}}, document.getElementById("recurring-schedule-status"))',context);
      if (!nodes['recurring-schedule-local-time'].focused || nodes['recurring-schedule-status'].dataset.tone !== 'error') throw Error('Field error did not guide keyboard focus');
      await vm.runInContext('submitRecurringSchedule({{preventDefault(){{}},currentTarget:{{querySelector(){{return {{}};}}}}}})',context);
      if (!nodes['recurring-schedule-destination'].focused || !nodes['recurring-schedule-status'].textContent.includes('connect one first')) throw Error('Missing app submit did not focus the app selector');
    """
    result = subprocess.run(['node', '--input-type=module', '--eval', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
