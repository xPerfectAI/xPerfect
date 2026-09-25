"""An accepted launch always leaves a working link to its workspace."""
import base64
import subprocess
from pathlib import Path

from glass_drive_ui import server


def run_case(case):
    source = (Path(server.STATIC_DIR) / 'launch-result.js').read_bytes()
    uri = 'data:text/javascript;base64,' + base64.b64encode(source).decode()
    script = f"import {{ showLaunchedWorkspace, watchHref }} from '{uri}';\n" + r'''
import assert from 'node:assert/strict';
const base = 'http://127.0.0.1:19990/#project';
''' + case
    subprocess.run(['node', '--input-type=module', '--eval', script], check=True, capture_output=True, text=True)


def test_the_watch_address_resolves_only_to_a_web_page():
    run_case(r'''
assert.equal(watchHref('/watch/wrk_a?project_id=prj_a&surface=terminal', base),
             'http://127.0.0.1:19990/watch/wrk_a?project_id=prj_a&surface=terminal');
assert.equal(watchHref('https://xperfect.example/watch/wrk_a', base), 'https://xperfect.example/watch/wrk_a');
for (const value of ['javascript:alert(1)', 'data:text/html,x', '', '   ', undefined, null, 42]) {
  assert.equal(watchHref(value, base), '', String(value));
}
''')


def test_a_started_project_shows_an_ordinary_link_before_the_page_moves():
    run_case(r'''
const status = {children: null, replaceChildren(...children) { this.children = children; }};
const link = showLaunchedWorkspace(status, 'http://127.0.0.1:19990/watch/wrk_a', () => ({}));
assert.equal(link.href, 'http://127.0.0.1:19990/watch/wrk_a');
assert.equal(link.textContent, 'Open workspace');
assert.deepEqual(status.children, ['Project started. ', link]);
''')


def test_the_launch_form_shows_the_link_and_is_usable_before_navigating():
    source = (Path(server.STATIC_DIR) / 'app.js').read_text()
    success = source[source.index("      launchDraftState.clear();\n      if (data.status === 'scheduled')"):]
    success = success[:success.index('    } catch (error) {')]
    assert 'window.location.href = data.watch_url' not in source
    assert success.index('button.disabled = false;') < success.index('showLaunchedWorkspace(')
    assert success.index('showLaunchedWorkspace(') < success.index('window.location.assign(href)')
