import json

import pytest

from workers_projects_runtime.conversation_provider import _native_usage
from workers_projects_runtime.profile_runtime import CodexCliRuntime


def native_output(usage):
    return json.dumps({'type': 'turn.completed', 'usage': usage})


def test_codex_usage_persists_disjoint_token_buckets_and_preserves_native_total(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    stdout = native_output({
        'input_tokens': 157395, 'cached_input_tokens': 119424,
        'cache_write_input_tokens': 0, 'output_tokens': 437,
        'reasoning_output_tokens': 118,
    })
    usage = runtime._record_run_usage('synthetic-worker', 'synthetic-run', stdout)
    assert usage == {
        'input_tokens': 37971, 'cache_read_input_tokens': 119424,
        'cache_creation_input_tokens': 0, 'output_tokens': 437,
    }
    assert runtime.run_usage({'worker_id': 'synthetic-worker'}, 'synthetic-run') == usage
    assert sum(usage.values()) == 157832
    assert _native_usage('codex-cli', stdout) == {
        'prompt_tokens': 157395, 'completion_tokens': 437, 'total_tokens': 157832,
    }


@pytest.mark.parametrize('stdout', [
    '', 'not-json', json.dumps({'type': 'item.completed', 'usage': {'input_tokens': 123}}),
    native_output({'input_tokens': True, 'output_tokens': 2}),
    native_output({'input_tokens': -1, 'output_tokens': 2}),
    native_output({'input_tokens': 10, 'output_tokens': 2, 'cached_input_tokens': 11}),
])
def test_codex_missing_or_invalid_usage_is_unknown_not_invented(tmp_path, stdout):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    assert runtime._usage_from_output(stdout) == {}
    assert _native_usage('codex-cli', stdout) is None


def test_codex_only_terminal_usage_owns_counts(tmp_path):
    stdout = '\n'.join([
        native_output({'input_tokens': 4, 'output_tokens': 2}),
        json.dumps({'type': 'item.completed', 'usage': {'input_tokens': 900, 'output_tokens': 900}}),
    ])
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    assert runtime._usage_from_output(stdout)['input_tokens'] == 4
    assert _native_usage('codex-cli', stdout)['total_tokens'] == 6


def test_codex_explicit_zero_usage_is_retained(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    assert runtime._usage_from_output(native_output({'input_tokens': 0, 'output_tokens': 0})) == {
        'input_tokens': 0, 'output_tokens': 0,
        'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
    }
    assert _native_usage('codex-cli', native_output({'input_tokens': 0, 'output_tokens': 0})) == {
        'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
    }
