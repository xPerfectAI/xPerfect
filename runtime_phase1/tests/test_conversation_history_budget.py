import pytest
from fastapi import HTTPException

from workers_projects_runtime.conversation_provider import (
    GLASSHIVE_MODELS, _admit_conversation_history, _conversation_replay_budget_bytes,
    _native_history_budget_receipt, ChatMessage,
)


@pytest.mark.parametrize('observed', [1.0, 2.054, 4.0])
def test_receipt_observes_the_same_budget_as_real_admission(monkeypatch, observed):
    monkeypatch.delenv('GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES', raising=False)
    model = GLASSHIVE_MODELS['codex-cli:gpt-6.1-sol']
    manifest = {'stable_authority_sha256': 'a' * 64, 'observed_chars_per_token': observed}
    receipt = _native_history_budget_receipt(model, manifest)
    _, admitted, _ = _admit_conversation_history(
        [ChatMessage(role='user', content='Exact current input.')],
        start_at=0, turn_context='', model=model, observed_chars_per_token=observed,
    )
    assert receipt['replay_budget_bytes'] == admitted['budget_bytes']
    assert receipt['model_id'] == model.id
    assert manifest == {'stable_authority_sha256': 'a' * 64, 'observed_chars_per_token': observed}


@pytest.mark.parametrize('cap', [1, 32, 65536])
def test_receipt_keeps_operator_cap_and_rejects_invalid_authority(monkeypatch, cap):
    monkeypatch.setenv('GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES', str(cap))
    model = GLASSHIVE_MODELS['codex-cli:gpt-6.1-sol']
    manifest = {'stable_authority_sha256': 'a' * 64, 'observed_chars_per_token': 4}
    assert _native_history_budget_receipt(model, manifest)['replay_budget_bytes'] == cap
    assert _native_history_budget_receipt(model, {**manifest, 'stable_authority_sha256': ''}) is None
    assert _native_history_budget_receipt(model, {**manifest, 'stable_authority_sha256': 'malformed'}) is None


def test_tiny_operator_maximum_rejects_protected_source_without_raising_the_cap(monkeypatch):
    monkeypatch.setenv('GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES', '1')
    model = GLASSHIVE_MODELS['codex-cli:gpt-6.1-sol']
    message = ChatMessage(role='user', content='Keep the complete original.')
    with pytest.raises(HTTPException) as error:
        _admit_conversation_history([message], start_at=0, turn_context='', model=model,
            observed_chars_per_token=4, protected_indices={0})
    assert error.value.status_code == 413
    assert 'No partial source was admitted' in error.value.detail
    assert message.content == 'Keep the complete original.'
    assert _conversation_replay_budget_bytes(model, observed_chars_per_token=4) == 1


def test_observed_small_native_window_remains_smaller_than_catalog(monkeypatch):
    monkeypatch.delenv('GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES', raising=False)
    model = GLASSHIVE_MODELS['codex-cli:gpt-6.1-sol']
    manifest = {'stable_authority_sha256': 'a' * 64, 'observed_chars_per_token': 2,
        'native_context_epoch': 'native', 'native_occupancy_native_context_epoch': 'native',
        'native_context_window_model_id': model.id, 'native_context_window_tokens': 64000}
    assert _native_history_budget_receipt(model, manifest)['replay_budget_bytes'] == 64000


def test_floor_is_not_the_actual_protected_source_ceiling(monkeypatch):
    monkeypatch.delenv('GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES', raising=False)
    model = GLASSHIVE_MODELS['codex-cli:gpt-6.1-sol']
    text = ('Full retained source. ' * 17000).strip()
    floor = _conversation_replay_budget_bytes(model, observed_chars_per_token=1)
    assert len(text.encode()) > floor
    instruction, admitted, _ = _admit_conversation_history(
        [ChatMessage(role='user', content=text)], start_at=0, turn_context='',
        model=model, observed_chars_per_token=2.054, protected_indices={0},
    )
    assert text in instruction
    assert admitted['omitted_message_count'] == 0
    assert admitted['instruction_bytes'] <= admitted['budget_bytes']


def test_smaller_actual_budget_rejects_a_stale_receipt_without_truncation(monkeypatch):
    monkeypatch.delenv('GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES', raising=False)
    model = GLASSHIVE_MODELS['codex-cli:gpt-6.1-sol']
    text = ('Full retained source. ' * 17000).strip()
    messages = [ChatMessage(role='user', content=text)]
    receipt = _native_history_budget_receipt(model, {
        'stable_authority_sha256': 'a' * 64, 'observed_chars_per_token': 2.054,
    })
    assert len(text.encode()) < receipt['replay_budget_bytes']
    with pytest.raises(HTTPException) as error:
        _admit_conversation_history(
            messages, start_at=0, turn_context='', model=model,
            observed_chars_per_token=1.0, protected_indices={0},
        )
    assert error.value.status_code == 413
    assert 'No partial source was admitted' in error.value.detail
    assert messages[0].content == text
