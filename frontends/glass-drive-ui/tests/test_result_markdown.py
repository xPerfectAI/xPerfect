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


def test_native_file_links_use_only_exact_authorized_current_artifact_metadata():
    module = (STATIC / 'result-markdown.js').as_uri()
    code = f"import {{createResultMarkdown}} from {json.dumps(module)};\n" + r'''
import assert from 'node:assert/strict';
const markdown = createResultMarkdown();
const shared = { open_url:'/v1/link-refs/ghr_exact_common', native_workspace_root:'/workspace/common',
  native_paths:['/workspace/common/report one.txt','report one.txt','./report one.txt'] };
const member = { open_url:'/v1/link-refs/ghr_exact_member', native_workspace_root:'/workspace/data/members/20002/worktree',
  native_paths:['/workspace/data/members/20002/worktree/report.txt','report.txt','./report.txt'] };
const host = { open_url:'/v1/link-refs/ghr_exact_host', native_workspace_root:'/synthetic/project',
  native_paths:['/synthetic/project/report.txt','report.txt','./report.txt'] };
for (const render of [(text,env)=>markdown.render(text,env), (text,env)=>markdown.renderInline(text,env)]) {
  for (const [artifact,path] of [[shared,'/workspace/common/report%20one.txt'],[member,'/workspace/data/members/20002/worktree/report.txt'],[host,'/synthetic/project/report.txt']]) {
    const output=render(`[file](${path})`,{resultArtifacts:[artifact]});
    assert(output.includes(`href="${artifact.open_url}"`));
    assert(!output.includes(`href="${path}"`));
  }
  for (const path of ['/workspace/data/members/20001/worktree/report.txt','/workspace/common/missing.txt','/workspace/common/../private/report.txt','/workspace/common/%2e%2e/private/report.txt','/workspace/common/report%2520one.txt']) {
    const output=render(`[file](${path})`,{resultArtifacts:[shared,member]});
    assert(!output.includes('<a '),`Unknown/foreign/traversal path must not guess a file: ${path}`);
    assert(output.includes('file'));
  }
  const ref='artifact_sha256:'+'a'.repeat(64);
  assert(!render(`[report.txt](${ref})`,{resultArtifacts:[{...member,native_paths:[ref]}]}).includes('<a '),'Mission identities keep their existing verified-carrier fence');
  assert(!render('[file](/workspace/common/report.txt)').includes('<a '),'No metadata is no binding');
  assert(!render('[file](/workspace/common/report%20one.txt)',{resultArtifacts:[{...shared,open_url:'https://other.example/report'}]}).includes('<a '),'Native path aliases do not authorize an external open destination');
  for (const url of ['/watch/worker','/api/workspace/worker/files','/v1/link-refs/other','/ordinary/app/page','https://example.com/report']) {
    assert(render(`[app](${url})`,{resultArtifacts:[shared]}).includes(`href="${url}"`),'Normal app/web links remain unchanged');
  }
  assert(render('`[literal](/workspace/common/report.txt)`',{resultArtifacts:[shared]}).includes('/workspace/common/report.txt'),'Code examples remain literal');
}
'''
    result = subprocess.run(['node', '--input-type=module'], input=code, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout
