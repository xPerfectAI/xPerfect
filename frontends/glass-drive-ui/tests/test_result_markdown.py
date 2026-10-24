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


def test_unclosed_reference_definitions_finish_and_valid_references_still_render():
    """GHSA-gfr2-rv44-r23c applies even with HTML, linkify and typography off."""
    module = (STATIC / 'result-markdown.js').as_uri()
    vendor = (STATIC / 'vendor' / 'markdown-it-15.0.3.mjs').as_uri()
    code = (f"import {{createResultMarkdown}} from {json.dumps(module)};\n"
            f"import MarkdownIt from {json.dumps(vendor)};\n" + r'''
import assert from 'node:assert/strict';
const renderers = [createResultMarkdown(), new MarkdownIt({html:false,linkify:false,typographer:false})];
for (const markdown of renderers) {
  assert(markdown.block.ruler.__rules__.some(rule => rule.name === 'reference' && rule.enabled));
  const valid = markdown.render('[report]: /safe "Report title"\n\n[report]');
  assert(valid.includes('href="/safe"') && valid.includes('title="Report title"'));
  // One megabyte is small for an LLM result. The old parser repeatedly copied
  // its accumulated multiline title; the patched parser handles it in one pass.
  for (const input of ['[' + 'a\n'.repeat(50000), '[a]: /u "' + 'x\n'.repeat(500000)]) {
    const output = markdown.render(input);
    assert(output.startsWith('<p>') && !output.includes('<a '));
  }
  assert(markdown.render('| Name | Value |\n| --- | --- |\n| Safe | 1 |').includes('<table>'));
  assert(!markdown.render('<script>unsafe</script>').includes('<script>'));
}
''')
    result = subprocess.run(['node', '--input-type=module'], input=code, text=True,
                            capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr + result.stdout
