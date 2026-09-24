"""The runtime, not the UI client, owns a new worker's bootstrap projection."""
import pytest

from glass_drive_ui.runtime_client import RuntimeClient


@pytest.mark.parametrize('profile', ['codex-cli', 'claude-code', 'grok-build', 'openclaw-general'])
def test_create_worker_leaves_bootstrap_projection_to_the_runtime(monkeypatch, profile):
    sent = []
    client = RuntimeClient(base_url='http://runtime.invalid')
    monkeypatch.setattr(client, '_request', lambda method, path, *, json_body=None: sent.append(json_body) or {})

    client.create_worker('prj_one', 'owner', profile)

    assert sent and sent[0]['profile'] == profile
    assert 'bootstrap_profile' not in sent[0]
