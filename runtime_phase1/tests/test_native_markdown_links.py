"""Selected-file Markdown scans do not block native reply or startup recovery."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from workers_projects_runtime import profile_runtime


def nested_json_output():
    return json.dumps({'type': 'assistant_response', 'tool_name': None,
                      'voice': 'eligible', 'content': json.dumps(
                          {'items': [{'name': 'synthetic', 'value': 'unchanged'}] * 16,
                           'empty': []})})


@pytest.mark.parametrize('shape', ['nested_json', 'escaped_label', 'unmatched_brackets',
                                   'escaped_brackets'])
def test_native_markdown_render_is_bounded_on_non_link_text(shape):
    payload = {
        'nested_json': nested_json_output(),
        'escaped_label': '[label' + '\\"' * 64,
        'unmatched_brackets': '[' * 100000,
        'escaped_brackets': r'\[' * 50000,
    }[shape]
    script = """import hashlib,sys
from workers_projects_runtime.profile_runtime import HostNativeCliMixin
value=sys.stdin.read()
rendered=HostNativeCliMixin._render_native_image_paths(value, [], '/workspace', file_transport=True)
assert rendered == value
print(hashlib.sha256(rendered.encode()).hexdigest())
"""
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'VIVENTIUM_PROMPT_BUNDLE_PATH', 'VIVENTIUM_INSTALL_MODE'}}
    environment['VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV'] = '1'
    environment['PYTHONPATH'] = str(Path(profile_runtime.__file__).resolve().parent.parent)
    result = subprocess.run([sys.executable, '-c', script], input=payload, text=True,
                            capture_output=True, timeout=3, check=False, env=environment)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hashlib.sha256(payload.encode()).hexdigest()


@pytest.mark.parametrize(('text', 'expected'), [
    ('[plain](file.txt)', [('[plain]', 'file.txt')]),
    ('![image](< /workspace/file name.png >)',
     [('![image]', '< /workspace/file name.png >')]),
    (r'[escaped\]label](file.txt)', [(r'[escaped\]label]', 'file.txt')]),
    (r'[escaped\[label](file.txt)', [(r'[escaped\[label]', 'file.txt')]),
    (r'\[literal](file.txt)', []),
    (r'\\[real](file.txt)', [('[real]', 'file.txt')]),
    ('[remote](https://example.org/path)', [('[remote]', 'https://example.org/path')]),
    ('[name](file%20name.txt)', [('[name]', 'file%20name.txt')]),
    ('[unfinished', []),
])
def test_shared_native_markdown_scan_preserves_declared_links_and_escapes(text, expected):
    from workers_projects_runtime.deliverables import NATIVE_MARKDOWN_LINK_OR_ESCAPE
    selected = [(m.group(1), m.group(2))
                for m in NATIVE_MARKDOWN_LINK_OR_ESCAPE.finditer(text) if m.group(2) is not None]
    assert selected == expected


def test_native_selected_link_keeps_escaped_label_and_exact_artifact_identity(tmp_path):
    data = b'synthetic file bytes'
    target = tmp_path / 'file name.txt'
    output = r'\[literal](ignored.txt) ' + r'[a\]b](< ' + str(target) + ' >)'
    actual = profile_runtime.HostNativeCliMixin._render_native_image_paths(
        output, [(target, 'text/plain', data)], str(tmp_path), file_transport=True)
    assert actual == (r'\[literal](ignored.txt) [a\]b](artifact_sha256:'
                      + hashlib.sha256(data).hexdigest() + ')')


def test_native_preview_link_scan_is_bounded_on_nested_json():
    payload = nested_json_output()
    script = """import sys
from workers_projects_runtime.conversation_provider import _mutable_native_file_link_start
assert _mutable_native_file_link_start(sys.stdin.read(), incomplete=False) is None
print('preview_ready')
"""
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'VIVENTIUM_PROMPT_BUNDLE_PATH', 'VIVENTIUM_INSTALL_MODE'}}
    environment['VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV'] = '1'
    environment['PYTHONPATH'] = str(Path(profile_runtime.__file__).resolve().parent.parent)
    result = subprocess.run([sys.executable, '-c', script], input=payload, text=True,
                            capture_output=True, timeout=3, check=False, env=environment)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'preview_ready'


@pytest.mark.parametrize(('text', 'expected'), [
    (r'\[literal](file.txt)', None),
    (r'\\[real](file.txt)', 2),
    (r'[escaped\]label](file.txt)', 0),
    ('[public](https://example.org/path)', None),
    (r'\[literal](hidden.txt) ' + '[Actual](result.txt)', len(r'\[literal](hidden.txt) ')),
])
def test_native_preview_keeps_only_real_local_links_after_escape_tokens(text, expected):
    from workers_projects_runtime.conversation_provider import _mutable_native_file_link_start
    assert _mutable_native_file_link_start(text, incomplete=False) == expected
