"""Activity page: the two forwards, the module's own rules, and how a person's words are drawn."""
from pathlib import Path
import re
import subprocess

from fastapi.testclient import TestClient

from glass_drive_ui.server import create_app

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'
SOURCE = (STATIC / 'activity-page.js').read_text()


class Runtime:
    def __init__(self):
        self.calls = []

    def _request(self, method, path, json_body=None):
        self.calls.append((method, path, json_body))
        return {'items': [], 'next_cursor': ''}


def run_node(code):
    result = subprocess.run(['node', '--input-type=module'], input=code, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout


def exported(name):
    match = re.search(r'^export function ' + re.escape(name) + r'\(.*?^}', SOURCE, re.M | re.S)
    assert match, name
    return match.group(0).replace('export ', '')


def test_the_filters_travel_in_the_body_and_a_line_id_is_checked_before_the_runtime():
    runtime = Runtime()
    client = TestClient(create_app(runtime_client=runtime))
    body = {'experts': ['lineage-a'], 'statuses': ['failed'], 'q': 'synthetic phrase', 'limit': 30}
    assert client.post('/api/activity/lines', json=body).json() == {'items': [], 'next_cursor': ''}
    assert client.get('/api/activity/lines/dB9jb252LWE').status_code == 200
    assert client.get('/api/activity/lines/not%2Fa%2Fline').status_code == 404
    assert runtime.calls == [('POST', '/v1/activity/lines', body), ('GET', '/v1/activity/lines/dB9jb252LWE', None)]


def test_day_headings_time_ranges_people_and_event_words():
    events = re.search(r'^const EVENTS = \{.*?^\};', SOURCE, re.M | re.S).group(0)
    run_node("import assert from 'node:assert/strict';\n" + events + "\n"
             + "\n".join(exported(name) for name in ('eventLabel', 'dayLabel', 'rangeStart', 'personWords', 'groupRuns')) + "\n"
             "const now = new Date(2026, 10, 25, 15, 0, 0);\n"
             "assert.equal(dayLabel(new Date(2026, 10, 25, 9, 0).toISOString(), now), 'Today');\n"
             "assert.equal(dayLabel(new Date(2026, 10, 24, 23, 59).toISOString(), now), 'Yesterday');\n"
             "assert.match(dayLabel(new Date(2026, 10, 20, 12, 0).toISOString(), now), /20/);\n"
             "assert.match(dayLabel(new Date(2025, 10, 20, 12, 0).toISOString(), now), /2025/);\n"
             "assert.equal(rangeStart('all', now), '');\n"
             "assert.equal(rangeStart('today', now), new Date(2026, 10, 25).toISOString());\n"
             "assert.equal(Date.parse(rangeStart('hour', now)), now.getTime() - 3600000);\n"
             "assert.equal(Date.parse(rangeStart('week', now)), now.getTime() - 7 * 86400000);\n"
             "assert.equal(personWords({ person: { name: '', you: false } }), 'Someone');\n"
             "assert.equal(personWords({ person: { name: 'Synthetic Person', you: false } }), 'Synthetic Person');\n"
             "assert.equal(personWords({ person: { name: '', you: true } }), 'You');\n"
             "assert.equal(eventLabel('run.queued'), 'Work queued');\n"
             "assert.equal(eventLabel('worker.created'), 'Workspace created');\n"
             "assert.equal(eventLabel('provider.session_started'), 'Provider session started');\n"
             "const rows = groupRuns([{at:'1',step:'received'},{at:'2',step:'event',event_type:'run.queued',ai:'codex-cli',run:'a'},"
             "{at:'3',step:'job',state:'started'},{at:'4',step:'event',event_type:'provider.child_updated',ai:'codex-cli',run:'a'},"
             "{at:'5',step:'event',event_type:'provider.child_updated',ai:'codex-cli',run:'a'},{at:'6',step:'event',event_type:'run.queued',ai:'claude-code',run:'b'}]);\n"
             "assert.deepEqual(rows.map(r => r.step), ['received', 'run', 'job', 'run']);\n"
             "assert.deepEqual(rows[1].labels, [{label: 'Work queued', count: 1}, {label: 'Provider child updated', count: 2}]);\n")


def test_a_persons_words_are_only_ever_text_and_the_phrase_never_enters_an_address():
    # innerHTML only ever receives this file's own constant marks, never data.
    for assigned in re.findall(r'\.innerHTML = (.+?);\n', SOURCE):
        assert assigned.startswith('`<svg') and '${paths}' in assigned, assigned
    assert "`“${item.preview}”`" in SOURCE and "'(no text)'" in SOURCE
    assert "request('/api/activity/lines', { method: 'POST', body: body(cursor) })" in SOURCE
    assert 'state.q' not in SOURCE.split('function setAddress')[1].split('function openFromAddress')[0]
    # The address keeps only the opened line's id.
    assert "'#activity/' + lineId" in SOURCE


def test_open_chat_only_from_a_proven_conversation_and_times_in_the_viewers_own_zone():
    # The rules of the renderer this page replaces: a chat link only from the conversation the runtime proved,
    # otherwise the workspace; times drawn in the viewer's own zone.
    assert "if (item.open?.conversation_id) { const a = node('a', 'quiet-button', 'Open chat'); a.href = '/conversation?id=' + encodeURIComponent(item.open.conversation_id);" in SOURCE
    assert "else if (item.open?.worker_id) { const a = node('a', 'quiet-button', 'Open workspace'); a.href = '/watch/' + encodeURIComponent(item.open.worker_id);" in SOURCE
    assert 'toLocaleTimeString(undefined' in SOURCE and 'toLocaleDateString(undefined' in SOURCE
    assert "'Activity is temporarily unavailable—refresh to retry'" in SOURCE and "'No activity yet'" in SOURCE
    # The page's regular refresh never redraws old rows over the page: it only mounts it or looks for newer lines.
    app = (STATIC / 'app.js').read_text()
    assert "import { renderActivity } from './activity-page.js?v=" in app and 'renderActivity();' in app
    assert 'export function renderActivity' not in (STATIC / 'control-plane.js').read_text()
