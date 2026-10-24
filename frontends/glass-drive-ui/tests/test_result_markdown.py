"""Actual Markdown renderer: native identities never become broken browser URLs."""
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def test_native_file_identities_are_plain_labels_and_safe_links_stay_clickable():
    module = (STATIC / 'result-markdown.js').as_uri()
    code = f"import {{createResultMarkdown}} from {json.dumps(module)};\n" + r'''
import assert from 'node:assert/strict';
const markdown = createResultMarkdown();
const ref = 'artifact_sha256:' + 'a'.repeat(64);
for (const render of [value => markdown.render(value), value => markdown.renderInline(value)]) {
  const text = render(`[report.txt](${ref}) and [other.txt](artifact_sha256:unknown)`);
  assert(text.includes('report.txt') && text.includes('other.txt'));
  assert(!text.includes('<a ') && !text.includes('artifact_sha256:'));
  assert.match(render(`[**report.txt**](${ref})`), /<strong>report.txt<\/strong>/);
  assert(!render(`![preview](${ref})`).includes('<img'));
  assert(render(`![<script>bad</script>](${ref})`).includes('&lt;script&gt;'));
  const safe = render(`[local](/v1/link-refs/synthetic) [web](https://example.com) [native](${ref})`);
  assert(safe.includes('href="/v1/link-refs/synthetic"') && safe.includes('href="https://example.com"'));
  assert(safe.includes('target="_blank"') && safe.includes('noopener noreferrer'));
  assert(!safe.includes('artifact_sha256:'));
  assert(!render('[bad](javascript:alert(1))').includes('<a '));
  assert(!render('<script>bad</script>').includes('<script>'));
}
assert(markdown.render('`[literal](' + ref + ')`').includes('artifact_sha256:'), 'Code examples are literal, not navigation');
assert(markdown.render('[safe](https://example.com) after [native](' + ref + ') then [safe](/ok)').includes('href="/ok"'), 'A native label does not swallow neighboring links');
'''
    result = subprocess.run(['node', '--input-type=module'], input=code, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout
