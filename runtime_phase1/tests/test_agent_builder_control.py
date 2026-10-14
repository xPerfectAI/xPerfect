from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from workers_projects_runtime.agent_builder_control import (
    conversation_output_schema,
    graph_transfer_control,
    graph_transfer_output_schema,
    messaging_delivery_control,
    parse_conversation_output,
    parse_graph_transfer_output,
    NativeConversationParts,
)


@pytest.mark.parametrize('terminal', [
    {'type': 'assistant_response', 'tool_name': None, 'voice': 'skip', 'content': 'Silent result.'},
    {'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist', 'voice': 'eligible', 'content': 'Consulting.'},
    {'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist', 'voice': 'skip', 'content': 'Silent handoff.'},
])
def test_native_generation_terminal_controls_match_existing_tool_parts(terminal):
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    delivery = messaging_delivery_control(audio_eligible=True)
    first = json.dumps({'type': 'assistant_response', 'tool_name': None,
                        'voice': 'eligible', 'content': 'I will check.'})
    outputs = []
    for boundary in ['tool', 'generation']:
        parts = NativeConversationParts(graph, delivery)
        def chunk(raw, stream):
            parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': raw}},
                         {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream})
        chunk(first, 100)
        if boundary == 'tool':
            parts.update({'sessionUpdate': 'tool_call'})
        else:
            parts.update({'sessionUpdate': 'response_completed'})
        raw = json.dumps(terminal)
        stream = 200 if boundary == 'generation' else 100
        chunk(raw[:-2], stream)
        if boundary == 'generation':
            if terminal['type'] == 'tool_call' and terminal['voice'] == 'skip':
                assert parts.prefix() is None
            else:
                assert parts.prefix()['delivery_disposition']['audio'] == terminal['voice']
        chunk(raw[-2:], stream)
        parts.update({'sessionUpdate': 'response_completed'})
        prefix = parts.prefix()
        assert bool(prefix) is (terminal['type'] == 'assistant_response' or terminal['voice'] == 'eligible')
        if prefix:
            assert prefix['text'] == terminal['content']
        outputs.append(json.loads(parts.finish()))
        if boundary == 'generation':
            chunk(first, 300)
            if terminal['type'] == 'tool_call':
                assert json.loads(parts.finish()) == outputs[-1]
            else:
                assert json.loads(parts.finish())['content'] == 'I will check.\n\n' + terminal['content'] + '\n\nI will check.'
    assert outputs == [{**terminal, 'content': 'I will check.\n\n' + terminal['content']}] * 2


@pytest.mark.parametrize('voice', ['eligible', 'skip'])
@pytest.mark.parametrize('earlier_public', [False, True])
def test_native_silent_predecessor_keeps_later_part_controls_without_erasing_public(voice, earlier_public):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    def put(text, voice, stream):
        raw = json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': voice, 'content': text})
        parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': raw}},
                     {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream})
        parts.update({'sessionUpdate': 'response_completed'})
    if earlier_public:
        put('Accepted narration.', 'eligible', 75)
    put('Silent body.', 'skip', 100)
    put('Silent body.' if voice == 'skip' else 'Useful addition.', voice, 200)
    payload = json.loads(parts.finish())
    assert payload['voice'] == 'skip'  # The preserved text-only predecessor stays text-only.
    assert payload['content'] == ('Accepted narration.\n\n' if earlier_public else '') + 'Silent body.\n\n' + (
        'Silent body.' if voice == 'skip' else 'Useful addition.')
    assert parts.parts == (['Accepted narration.'] if earlier_public else []) + ['Silent body.']
    assert parts.prefix()['delivery_disposition']['audio'] == voice


def test_graph_only_output_declines_optional_interject_without_changing_voice_or_freeform():
    from workers_projects_runtime.agent_builder_control import conversation_interjection_supported
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    assert conversation_interjection_supported(None) is True
    assert conversation_interjection_supported({'type': 'string'}) is True
    assert conversation_interjection_supported(conversation_output_schema(graph, messaging_delivery_control(audio_eligible=True))) is True
    assert conversation_interjection_supported(conversation_output_schema(graph, None)) is False


def test_distinct_native_generations_preserve_model_authored_repetition_and_stream_prefix():
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    def put(text, stream, complete=True):
        raw = json.dumps({'type': 'assistant_response', 'tool_name': None,
                          'voice': 'eligible', 'content': text})
        meta = {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}
        parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {
            'type': 'text', 'text': raw if complete else raw[:-2]}}, meta)
        if complete:
            parts.update({'sessionUpdate': 'response_completed'})
        return meta, raw
    put('First answer.', 100)
    meta, raw = put('First answer.', 200, False)
    assert parts.prefix()['text'] == 'First answer.'
    assert parts.prefix()['complete'] is False
    parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': raw[-2:]}}, meta)
    assert parts.prefix()['text'] == 'First answer.'
    assert not parts.prefix().get('duplicate')
    parts.update({'sessionUpdate': 'response_completed'})
    put('Useful addition.', 300)
    assert json.loads(parts.finish())['content'] == 'First answer.\n\nFirst answer.\n\nUseful addition.'
    assert parts.part_index == 3


@pytest.mark.parametrize('metadata', [
    {}, {'promptId': 'other', 'turnStartMs': 50, 'streamStartMs': 200},
    {'promptId': 'prompt-exact', 'turnStartMs': 51, 'streamStartMs': 200},
    {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': 99},
    {'promptId': 'prompt-exact', 'turnStartMs': True, 'streamStartMs': 200},
])
def test_native_generation_parts_keep_exact_identity(metadata):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    update = {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text':
        '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Answer."}'}}
    parts.update(update, {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': 100})
    parts.update({'sessionUpdate': 'response_completed'})
    with pytest.raises(ValueError, match='identity|prompt or turn'):
        parts.update(update, metadata)


def test_native_generation_parts_do_not_erase_intervening_tool_narration():
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    def chunk(text, stream):
        parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text':
            json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': text})}},
            {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream})
    chunk('First answer.', 100)
    parts.update({'sessionUpdate': 'response_completed'})
    chunk('I will check.', 200)
    parts.update({'sessionUpdate': 'tool_call'})
    chunk('First answer.', 200)
    assert json.loads(parts.finish())['content'] == 'First answer.\n\nI will check.\n\nFirst answer.'


def test_native_generation_completion_cannot_rebind_another_prompt():
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text':
        '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Answer."}'}},
        {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': 100})
    with pytest.raises(ValueError, match='completion.*identity'):
        parts.update({'sessionUpdate': 'response_completed'}, {'promptId': 'foreign'})


@pytest.mark.parametrize('first_voice', ['eligible', 'skip'])
@pytest.mark.parametrize('final_voice', ['eligible', 'skip'])
@pytest.mark.parametrize('buffered', [False, True])
def test_native_tools_retire_public_buffer_before_later_completed_generation(first_voice, final_voice, buffered):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    def chunk(text, voice, stream):
        parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text':
            json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': voice, 'content': text})}},
            {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream})
    chunk('Same public text.', first_voice, 100)
    parts.update({'sessionUpdate': 'response_completed'})
    if buffered:
        chunk('Intervening narration.', first_voice, 200)
        parts.update({'sessionUpdate': 'response_completed'})
    parts.update({'sessionUpdate': 'tool_call'})
    parts.update({'sessionUpdate': 'response_completed'})
    parts.update({'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'Private reasoning.'}})
    parts.update({'sessionUpdate': 'interjected', 'content': {'type': 'text', 'text': 'Background evidence.'}})
    parts.update({'sessionUpdate': 'tool_call'})
    parts.update({'sessionUpdate': 'response_completed'})
    chunk('Same public text.', final_voice, 300)
    parts.update({'sessionUpdate': 'response_completed'})
    payload = json.loads(parts.finish())
    earlier = ['Same public text.'] + (['Intervening narration.'] if buffered else [])
    assert payload == {'type': 'assistant_response', 'tool_name': None, 'voice': 'skip' if 'skip' in (first_voice, final_voice) else 'eligible',
                       'content': '\n\n'.join([*earlier, 'Same public text.'])}


@pytest.mark.parametrize('change', ['prompt', 'turn', 'stream', 'unfinished'])
def test_retired_native_buffer_keeps_generation_identity_fences(change):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    raw = '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Accepted narration."}'
    parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': raw}},
                 {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': 100})
    if change != 'unfinished':
        parts.update({'sessionUpdate': 'response_completed'})
    parts.update({'sessionUpdate': 'tool_call'})
    with pytest.raises(ValueError, match='prompt or turn|before response completion'):
        parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': raw}},
                     {'promptId': 'foreign' if change == 'prompt' else 'prompt-exact',
                      'turnStartMs': 51 if change == 'turn' else 50,
                      'streamStartMs': 99 if change == 'stream' else 200})


@pytest.mark.parametrize('voice', ['eligible', 'skip'])
def test_native_tool_event_cannot_erase_declared_terminal_graph_envelope(voice):
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    parts = NativeConversationParts(graph, messaging_delivery_control(audio_eligible=True))
    terminal = {'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist', 'voice': voice, 'content': 'Consulting.'}
    parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps(terminal)}},
                 {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': 100})
    parts.update({'sessionUpdate': 'response_completed'})
    parts.update({'sessionUpdate': 'tool_call'})
    parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text':
        '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Later optional text."}'}},
        {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': 200})
    assert json.loads(parts.finish()) == terminal
from workers_projects_runtime.conversation_provider import (
    GLASSHIVE_MODELS,
    ChatCompletionRequest,
    ConversationProvider,
)


def _transfer(name: str, *, with_input: bool = False) -> dict:
    properties = {"query": {"type": "string"}} if with_input else {}
    required = ["query"] if with_input else []
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Transfer through the current graph.",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _payload(*, stream: bool = False) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Choose the best next graph step."}],
            "stream": stream,
            "metadata": {
                "owner_id": "owner-example",
                "conversation_id": "conversation-example",
                "agent_id": "agent-example",
                "idempotency_key": "message-example",
                "glasshive_options": {
                    "workspace": {"mode": "default"},
                    "access": "workspace",
                },
            },
            "tools": [
                _transfer("lc_transfer_to_specialist"),
                _transfer("lc_transfer_to_requires_input", with_input=True),
                {
                    "type": "function",
                    "function": {
                        "name": "external_side_effect",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ],
        }
    )


def test_graph_control_admits_only_zero_input_structural_transfers():
    payload = _payload()
    control = graph_transfer_control(payload.tools, payload.tool_choice)

    assert control == {
        "version": 1,
        "tools": [
            {
                "name": "lc_transfer_to_specialist",
                "description": "Transfer through the current graph.",
            }
        ],
    }
    with pytest.raises(ValueError, match="not available"):
        graph_transfer_control(
            payload.tools,
            {
                "type": "function",
                "function": {"name": "lc_transfer_to_requires_input"},
            },
        )


def test_graph_control_schema_and_native_choice_fail_closed():
    control = graph_transfer_control(_payload().tools)
    schema = graph_transfer_output_schema(control)

    assert schema["additionalProperties"] is False
    assert schema["properties"]["tool_name"]["enum"] == [
        None,
        "lc_transfer_to_specialist",
    ]
    assert parse_graph_transfer_output(
        json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
            }
        ),
        control,
    )["type"] == "tool_call"
    with pytest.raises(ValueError, match="unavailable"):
        parse_graph_transfer_output(
            json.dumps(
                {
                    "type": "tool_call",
                    "content": "",
                    "tool_name": "lc_transfer_to_unknown",
                }
            ),
            control,
        )


@pytest.mark.parametrize(
    "with_graph,with_delivery",
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_conversation_schema_declares_final_work_and_async_receipt_boundary(
    with_graph, with_delivery
):
    graph = graph_transfer_control(_payload().tools) if with_graph else None
    delivery = messaging_delivery_control(audio_eligible=True) if with_delivery else None
    schema = conversation_output_schema(graph, delivery)
    if not with_graph and not with_delivery:
        assert schema is None
        return

    description = schema["description"]
    assert "Returning type=assistant_response ends your work for this turn." in description
    assert "complete authorized work you can do now, or report its actual blocker" in description
    assert "a promise is not a result" in description
    assert (
        "A real accepted asynchronous work receipt may be acknowledged without waiting for completion."
        in description
    )
    assert schema["additionalProperties"] is False
    assert schema["properties"]["type"]["enum"] == (
        ["assistant_response", "tool_call"] if with_graph else ["assistant_response"]
    )
    accepted = {
        "type": "assistant_response",
        "content": "The work was accepted. Its result will follow.",
        "tool_name": None,
    }
    if with_delivery:
        accepted["voice"] = "eligible"
    assert parse_conversation_output(json.dumps(accepted), graph, delivery)["content"] == accepted["content"]
    if with_graph:
        transfer = {
            "type": "tool_call",
            "content": "",
            "tool_name": "lc_transfer_to_specialist",
        }
        if with_delivery:
            transfer["voice"] = "eligible"
        expected = {"type": "tool_call", "content": "", "tool_name": "lc_transfer_to_specialist"}
        if with_delivery:
            expected["delivery_disposition"] = {"version": 1, "audio": "eligible",
                "required": True, "valid": True, "source": "model"}
        assert parse_conversation_output(json.dumps(transfer), graph, delivery) == expected


def test_audio_eligible_messaging_schema_requires_model_owned_voice_disposition():
    delivery = messaging_delivery_control(audio_eligible=True)
    schema = conversation_output_schema(None, delivery)

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["type", "content", "tool_name", "voice"]
    assert schema["properties"]["type"]["enum"] == ["assistant_response"]
    assert schema["properties"]["tool_name"]["enum"] == [None]
    assert schema["properties"]["voice"]["enum"] == ["eligible", "skip"]
    assert parse_conversation_output(
        json.dumps(
            {
                "type": "assistant_response",
                "content": "Copy-ready answer.",
                "tool_name": None,
                "voice": "skip",
            }
        ),
        None,
        delivery,
    ) == {
        "type": "assistant_response",
        "content": "Copy-ready answer.",
        "delivery_disposition": {
            "version": 1,
            "audio": "skip",
            "required": True,
            "valid": True,
            "source": "model",
        },
    }
    assert parse_conversation_output(
        json.dumps(
            {
                "type": "assistant_response",
                "content": "Missing disposition.",
                "tool_name": None,
            }
        ),
        None,
        delivery,
    ) == {
        "type": "assistant_response",
        "content": "Missing disposition.",
        "delivery_disposition": {
            "version": 1,
            "audio": "skip",
            "required": True,
            "valid": False,
            "source": "required_missing",
        },
    }


def test_audio_eligible_messaging_schema_composes_with_graph_transfer_control():
    graph = graph_transfer_control(_payload().tools)
    delivery = messaging_delivery_control(audio_eligible=True)
    schema = conversation_output_schema(graph, delivery)

    assert schema["properties"]["tool_name"]["enum"] == [
        None,
        "lc_transfer_to_specialist",
    ]
    assert schema["properties"]["voice"]["enum"] == ["eligible", "skip"]
    assert parse_conversation_output(
        json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
                "voice": "eligible",
            }
        ),
        graph,
        delivery,
    ) == {
        "type": "tool_call",
        "content": "",
        "tool_name": "lc_transfer_to_specialist",
        "delivery_disposition": {"version": 1, "audio": "eligible",
                                 "required": True, "valid": True, "source": "model"},
    }
    assert parse_conversation_output(
        json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
                "voice": "skip",
            }
        ),
        graph,
        delivery,
    ) == {
        "type": "tool_call",
        "content": "",
        "tool_name": "lc_transfer_to_specialist",
        "delivery_disposition": {"version": 1, "audio": "skip",
                                 "required": True, "valid": True, "source": "model"},
    }


def test_malformed_delivery_envelope_preserves_plain_text_without_leaking_json():
    delivery = messaging_delivery_control(audio_eligible=True)

    plain = parse_conversation_output("Plain fallback answer.", None, delivery)
    malformed_json = parse_conversation_output(
        '{"type":"assistant_response","content":"private envelope"',
        None,
        delivery,
    )
    fenced_json = parse_conversation_output(
        '```json\n{"type":"assistant_response","content":"private envelope",'
        '"tool_name":null}\n```',
        None,
        delivery,
    )
    truncated_fenced_json = parse_conversation_output(
        '```json\n{"type":"assistant_response","content":"private envelope"\n```',
        None,
        delivery,
    )
    fenced_code = parse_conversation_output(
        "```python\nprint('hello')\n```",
        None,
        delivery,
    )

    assert plain["content"] == "Plain fallback answer."
    assert plain["delivery_disposition"]["audio"] == "skip"
    assert plain["delivery_disposition"]["valid"] is False
    assert malformed_json["content"] == (
        "The response completed, but its delivery metadata was malformed."
    )
    assert "private envelope" not in malformed_json["content"]
    assert fenced_json["content"] == malformed_json["content"]
    assert truncated_fenced_json["content"] == malformed_json["content"]
    assert "private envelope" not in truncated_fenced_json["content"]
    assert fenced_code["content"] == "```python\nprint('hello')\n```"


def test_conversation_bundle_projects_graph_control_without_broad_tool_authority():
    provider = ConversationProvider.__new__(ConversationProvider)
    payload = _payload()
    model = GLASSHIVE_MODELS[payload.model]

    bundle = provider._native_bundle(payload, model, model.recommended_effort)

    assert bundle["agent_builder_control"]["tools"] == [
        {
            "name": "lc_transfer_to_specialist",
            "description": "Transfer through the current graph.",
        }
    ]
    assert bundle["provider_capabilities"]["graph_control_transport"] == "openai_tool_call"
    assert bundle["provider_capabilities"]["graph_control_tools"] == [
        "lc_transfer_to_specialist"
    ]
    assert "external_side_effect" not in json.dumps(bundle)


def test_conversation_bundle_projects_audio_eligibility_as_structural_output_control():
    provider = ConversationProvider.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Reply for this messaging turn."}],
            "metadata": {
                "owner_id": "owner-example",
                "conversation_id": "conversation-example",
                "agent_id": "agent-example",
                "surface": "telegram",
                "audio_eligible": True,
            },
        }
    )
    model = GLASSHIVE_MODELS[payload.model]

    bundle = provider._native_bundle(payload, model, model.recommended_effort)

    assert bundle["messaging_delivery_control"] == {
        "version": 1,
        "audio_eligible": True,
    }


@pytest.mark.parametrize("voice", [None, "eligible", "skip"])
def test_non_streaming_graph_choice_returns_one_openai_tool_call(monkeypatch, voice):
    class RecordingStore:
        response_json = ""

        def get_provider_request(self, request_id: str):
            assert request_id == "request-example"
            return None

        def commit_provider_request_terminal(self, request_id: str, **fields):
            assert request_id == "request-example"
            self.response_json = fields["response_json"]
            return {"state": "completed", "response_json": self.response_json}

        def get_provider_session_by_id(self, _session_id: str):
            return None

        def get_worker(self, _worker_id: str):
            return None

    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = RecordingStore()
    provider.service = SimpleNamespace(runtime=SimpleNamespace())
    payload = _payload()
    payload.metadata.audio_eligible = voice is not None
    monkeypatch.setattr(
        provider,
        "_conversation_output",
        lambda request_record, run, **_output_contract: json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
                **({"voice": voice} if voice else {}),
            }
        ),
    )
    monkeypatch.setattr(
        provider,
        "_completion_usage",
        lambda request_record, run, completion, output: (
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "estimated",
        ),
    )

    response = provider.response_payload(
        {"request_id": "request-example", "state": "completed", "response_json": "", "session_id": "session-example"},
        {"state": "completed", "run_id": "run-example"},
        payload,
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": choice["message"]["tool_calls"][0]["id"],
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "arguments": "{}",
            },
        }
    ]
    if voice:
        assert choice["message"]["provider_specific_fields"]["viventium"]["delivery_disposition"] == {
            "version": 1, "audio": voice, "required": True, "valid": True, "source": "model"}
    else:
        assert "provider_specific_fields" not in choice["message"]
    assert json.loads(provider.store.response_json) == response


def test_public_prefix_requires_controls_first_and_decodes_split_json_strings():
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix
    delivery = messaging_delivery_control(audio_eligible=True)
    text = 'A "quoted" word, newline\n and café 😀 with \\ slash.'
    envelope = json.dumps({"type": "assistant_response", "tool_name": None,
                           "voice": "eligible", "content": text}, ensure_ascii=True)
    content_start = envelope.index('"content"')
    prefixes = [public_conversation_prefix(envelope[:end], None, delivery)
                for end in range(len(envelope) + 1)]
    assert all(prefix is None for prefix in prefixes[:content_start])
    assert any(prefix and prefix["text"] and not prefix["complete"] for prefix in prefixes)
    for prefix in prefixes:
        if prefix:
            assert text.startswith(prefix["text"])
            assert not any(0xD800 <= ord(char) <= 0xDFFF for char in prefix["text"])
    assert prefixes[-1]["text"] == text
    assert prefixes[-1]["complete"] is True
    assert prefixes[-1]["delivery_disposition"]["audio"] == "eligible"
    assert list(conversation_output_schema(None, delivery)["properties"]) == [
        "type", "tool_name", "voice", "content"]


@pytest.mark.parametrize("envelope", [
    '{"type":"assistant_response", "tool_name":null, "voice":"skip", "content":"Text only"}',
    '{"type":"tool_call", "tool_name":"lc_transfer_to_specialist", "voice":"eligible", "content":"Internal"}',
    '{"type":"assistant_response", "tool_name":null, "voice":"eligible", "extra":true, "content":"Unknown field"}',
    '{"type":"assistant_response", "tool_name":null, "voice":"skip", "voice":"eligible", "content":"Duplicate"}',
    '{"type":"assistant_response", "tool_name":null, "voice":"eligible", "content":"Malformed \\q"}',
    '{"type":"assistant_response", "tool_name":null, "voice":"eligible", "content":"Duplicate tail", "voice":"skip"}',
])
def test_public_prefix_withholds_invalid_skip_and_graph_envelopes(envelope):
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix
    assert public_conversation_prefix(envelope, None, messaging_delivery_control(audio_eligible=True)) is None


def test_duplicate_terminal_control_is_rejected_instead_of_last_field_winning():
    with pytest.raises(ValueError, match="Duplicate"):
        parse_conversation_output('{"type":"assistant_response","tool_name":null,"voice":"skip",'
                                  '"voice":"eligible","content":"Do not publish"}', None,
                                  messaging_delivery_control(audio_eligible=True))


@pytest.mark.parametrize("voice", ["eligible", "skip"])
def test_declared_transfer_public_content_has_model_owned_delivery(voice):
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix
    graph = graph_transfer_control(_payload().tools)
    delivery = messaging_delivery_control(audio_eligible=True)
    text = 'I will check the current source before recommending a change.'
    envelope = json.dumps({"type":"tool_call", "tool_name":"lc_transfer_to_specialist",
                           "voice":voice, "content":text})
    result = parse_conversation_output(envelope, graph, delivery)
    assert result["content"] == text and result["tool_name"] == "lc_transfer_to_specialist"
    assert result["delivery_disposition"]["audio"] == voice
    prefixes = [public_conversation_prefix(envelope[:end], graph, delivery)
                for end in range(len(envelope)+1)]
    if voice == "skip":
        assert all(prefix is None for prefix in prefixes)
    else:
        assert any(prefix and prefix["text"] and not prefix["complete"] for prefix in prefixes)
        assert prefixes[-1]["text"] == text
        assert prefixes[-1]["delivery_disposition"] == result["delivery_disposition"]
        assert all(text.startswith(prefix["text"]) for prefix in prefixes if prefix)


@pytest.mark.parametrize("patch", [
    {"tool_name":"lc_transfer_to_foreign"}, {"tool_name":None}, {"voice":"maybe"},
    {"type":"unknown"}, {"extra":True},
])
def test_transfer_public_prefix_never_precedes_declared_control_validation(patch):
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix
    graph = graph_transfer_control(_payload().tools)
    delivery = messaging_delivery_control(audio_eligible=True)
    envelope = {"type":"tool_call", "tool_name":"lc_transfer_to_specialist",
                "voice":"eligible", "content":"Do not publish."}
    envelope.update(patch)
    raw = json.dumps(envelope)
    assert public_conversation_prefix(raw, graph, delivery) is None
    with pytest.raises(ValueError): parse_conversation_output(raw, graph, delivery)


def test_transfer_schema_describes_optional_truthful_public_content():
    schema = conversation_output_schema(graph_transfer_control(_payload().tools),
                                        messaging_delivery_control(audio_eligible=True))
    assert "leave it empty when no update is useful" in schema["description"]
    assert "use empty content" not in schema["description"]
    assert "For any public content" in schema["description"]
    assert "set voice=skip for its receiving-agent evidence" in schema["description"]
    assert "not as public speech" in schema["description"]
    assert list(schema["properties"]) == ["type", "tool_name", "voice", "content"]


@pytest.mark.parametrize("with_graph", [False, True])
def test_public_audio_schema_defers_to_surface_and_participant_role(with_graph):
    graph = graph_transfer_control(_payload().tools) if with_graph else None
    schema = conversation_output_schema(graph, messaging_delivery_control(audio_eligible=True))
    description = schema["description"]
    assert "follow the current surface and participant-role delivery instructions" in description
    assert "In a live call, ordinary public content from the current speaking agent is voice=eligible" in description
    assert "unless the user explicitly requests text-only or silent delivery" in description
    assert "Where messaging audio is optional" in description
    assert "set voice=skip when audio would reduce usefulness" in description
    assert "set voice=skip for its receiving-agent evidence" in description
    assert "For any public content, set voice=skip when optional audio" not in description
    if with_graph:
        assert description.endswith("Available transfers: lc_transfer_to_specialist: Transfer through the current graph.")
        assert description.index("For any public content") < description.index("Available transfers:")
    assert schema["properties"]["voice"] == {"type": "string", "enum": ["eligible", "skip"]}
    assert "voice" in schema["required"]
    # The model owns the choice; the schema/parser does not rewrite a valid skip to eligible.
    result = parse_conversation_output(json.dumps({"type": "assistant_response", "tool_name": None,
        "voice": "skip", "content": "Requested silent text."}), graph,
        messaging_delivery_control(audio_eligible=True))
    assert result["delivery_disposition"]["audio"] == "skip"


def test_intermediate_native_completion_retires_unstructured_tool_narration():
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    def chunk(text, stream):
        parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}},
                     {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream})
    chunk('I will inspect the folder.', 100)
    parts.update({'sessionUpdate': 'response_completed'})
    parts.update({'sessionUpdate': 'tool_call'})
    chunk('I will perform the authorized action.', 200)
    parts.update({'sessionUpdate': 'response_completed'})
    assert parts.prefix() is None
    parts.update({'sessionUpdate': 'tool_call'})
    raw = json.dumps({'type': 'assistant_response', 'tool_name': None,
                      'voice': 'eligible', 'content': 'The artifact is ready.'})
    chunk(raw, 300)
    parts.update({'sessionUpdate': 'response_completed'})
    assert json.loads(parts.finish()) == json.loads(raw)
    assert parts.parts == []


@pytest.mark.parametrize('terminal', ['Unstructured terminal result.',
    '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Incomplete'])
def test_deferred_native_completion_still_rejects_invalid_terminal_envelope(terminal):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    def chunk(text, stream):
        parts.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}},
                     {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream})
    chunk('Intermediate narration.', 100)
    parts.update({'sessionUpdate': 'tool_call'})
    parts.update({'sessionUpdate': 'response_completed'})
    chunk(terminal, 200)
    parts.update({'sessionUpdate': 'response_completed'})
    with pytest.raises(ValueError):
        parts.finish()


@pytest.mark.parametrize("with_delivery", [True, False])
def test_native_continuation_changes_guidance_without_changing_authority(with_delivery):
    graph = graph_transfer_control(_payload().tools)
    delivery = messaging_delivery_control(audio_eligible=True) if with_delivery else None
    original = conversation_output_schema(graph, delivery)
    continued = conversation_output_schema(graph, delivery, same_turn_continuation=True)
    assert {k: v for k, v in original.items() if k != "description"} == {
        k: v for k, v in continued.items() if k != "description"
    }
    assert "ends your work for this turn" not in continued["description"]
    assert "continue this same turn" in continued["description"]
    assert "full prior goal, context, tool results and work" in continued["description"]
    assert "unless the user requests repetition or a full replacement" in continued["description"]
    assert "not proof that text, preview or speech reached the user" in continued["description"]
    assert "not a substitute for answering an unanswered user request" in continued["description"]
    empty = {"type": "assistant_response", "content": "", "tool_name": None}
    if with_delivery:
        empty["voice"] = "skip"
    decision = parse_conversation_output(json.dumps(empty), graph, delivery)
    assert decision["content"] == ""
    if with_delivery:
        assert decision["delivery_disposition"]["valid"] is True
        assert decision["delivery_disposition"]["audio"] == "skip"


@pytest.mark.parametrize("terminal,prior,expected_voice", [
    ({"type": "assistant_response", "tool_name": None, "voice": "skip", "content": ""},
     ["Useful earlier answer."], "eligible"),
    ({"type": "assistant_response", "tool_name": None, "voice": "skip", "content": ""},
     [], "skip"),
    ({"type": "assistant_response", "tool_name": None, "voice": "skip", "content": ""},
     [""], "skip"),
    ({"type": "assistant_response", "tool_name": None, "voice": "skip", "content": "Read this silently."},
     ["Useful earlier answer."], "skip"),
    ({"type": "tool_call", "tool_name": "lc_transfer_to_specialist", "voice": "skip", "content": ""},
     ["Useful earlier answer."], "skip"),
])
def test_silent_continuation_preserves_eligible_parts_for_final_only_consumers(terminal, prior, expected_voice):
    from workers_projects_runtime.agent_builder_control import ordered_conversation_output
    graph = {"version": 1, "tools": [{"name": "lc_transfer_to_specialist", "description": "Consult."}]}
    delivery = messaging_delivery_control(audio_eligible=True)
    output = ordered_conversation_output(prior, json.dumps(terminal), graph, delivery)
    actual = json.loads(output)
    assert actual["voice"] == expected_voice
    assert actual["type"] == terminal["type"] and actual["tool_name"] == terminal["tool_name"]
    expected_content = "\n\n".join([*prior, terminal["content"]]) if prior else terminal["content"]
    assert actual["content"] == expected_content


@pytest.mark.parametrize("prior_voice,expected_content,expected_voice", [
    ("eligible", "Useful earlier answer.\n\n", "eligible"),
    ("skip", "Useful earlier answer.\n\n", "skip"),
])
def test_two_native_generations_preserve_public_answer_when_continuation_is_silent(prior_voice, expected_content, expected_voice):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    for text, voice, stream in [("Useful earlier answer.", prior_voice, 100), ("", "skip", 200)]:
        payload = {"type": "assistant_response", "tool_name": None, "voice": voice, "content": text}
        parts.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": json.dumps(payload)}},
                     {"promptId": "prompt-exact", "turnStartMs": 50, "streamStartMs": stream})
        parts.update({"sessionUpdate": "response_completed"})
    result = json.loads(parts.finish())
    assert result["content"] == expected_content
    assert result["voice"] == expected_voice
    assert result["tool_name"] is None


@pytest.mark.parametrize("first_voice,second_voice", [("skip", "eligible"), ("eligible", "skip")])
def test_native_text_and_audio_ranges_preserve_mixed_intent_and_utf16(first_voice, second_voice):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    for text, voice, stream in [("Text \U0001f642", first_voice, 100), ("Spoken addition.", second_voice, 200)]:
        raw = json.dumps({"type": "assistant_response", "tool_name": None, "voice": voice, "content": text})
        parts.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": raw}},
                     {"promptId": "prompt-exact", "turnStartMs": 50, "streamStartMs": stream})
        parts.update({"sessionUpdate": "response_completed"})
    final = json.loads(parts.finish())
    assert final["content"] == "Text \U0001f642\n\nSpoken addition."
    assert final["voice"] == "skip"  # A legacy one-flag consumer must not speak skipped text.
    ranges = parts.delivery_disposition_ranges()
    assert [(item["start"], item["end"], item["disposition"]["audio"]) for item in ranges] == [
        (0, 7, first_voice), (7, 25, second_voice)
    ]
    assert all(item["disposition"] == {"version": 1, "audio": audio, "required": True,
                "valid": True, "source": "model"} for item, audio in zip(ranges, [first_voice, second_voice]))
    assert parts.prefix()["delivery_disposition"]["audio"] == second_voice


@pytest.mark.parametrize("voice,include_skipped,visible", [
    ("eligible", False, True), ("skip", False, False), ("skip", True, True),
])
def test_complete_content_first_envelope_releases_without_waiting_for_later_generation(voice, include_skipped, visible):
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix
    raw = json.dumps({"content": "Earlier useful answer \U0001f642.", "type": "assistant_response", "tool_name": None, "voice": voice})
    delivery = messaging_delivery_control(audio_eligible=True)
    assert all(public_conversation_prefix(raw[:end], None, delivery, include_skipped=include_skipped) is None
               for end in range(len(raw)))
    prefix = public_conversation_prefix(raw, None, delivery, include_skipped=include_skipped)
    assert bool(prefix) is visible
    if visible:
        assert prefix["complete"] is True and prefix["text"] == "Earlier useful answer \U0001f642."
        assert prefix["delivery_disposition"]["audio"] == voice


@pytest.mark.parametrize("tail", [
    ',"voice":"eligible","voice":"skip"}',
    ',"voice":"invalid"}',
    ',"voice":"eligible","extra":true}',
    ',"voice":"eligible"',
    ',"voice":"eligible"} trailing',
])
def test_complete_content_first_invalid_controls_cannot_release(tail):
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix
    raw = '{"content":"Must stay withheld","type":"assistant_response","tool_name":null' + tail
    assert public_conversation_prefix(raw, None, messaging_delivery_control(audio_eligible=True), include_skipped=True) is None


@pytest.mark.parametrize("later", ["Useful new fact.", "Earlier useful answer."])
def test_content_first_native_generation_is_public_before_later_delta_or_repeat(later):
    parts = NativeConversationParts(None, messaging_delivery_control(audio_eligible=True))
    first = {"content": "Earlier useful answer.", "type": "assistant_response", "voice": "eligible", "tool_name": None}
    parts.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": json.dumps(first)}},
                 {"promptId": "prompt-exact", "turnStartMs": 50, "streamStartMs": 100})
    assert parts.prefix()["text"] == first["content"] and parts.prefix()["complete"] is True
    parts.update({"sessionUpdate": "response_completed"})
    assert parts.prefix()["text"] == first["content"]
    second = {**first, "content": later}
    parts.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": json.dumps(second)}},
                 {"promptId": "prompt-exact", "turnStartMs": 50, "streamStartMs": 200})
    parts.update({"sessionUpdate": "response_completed"})
    assert json.loads(parts.finish())["content"] == first["content"] + "\n\n" + later


def test_content_first_skipped_graph_evidence_remains_private():
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix
    graph = {"version": 1, "tools": [{"name": "lc_transfer_to_specialist", "description": "Consult."}]}
    raw = json.dumps({"content": "Receiving-agent evidence.", "voice": "skip", "type": "tool_call", "tool_name": "lc_transfer_to_specialist"})
    assert public_conversation_prefix(raw, graph, messaging_delivery_control(audio_eligible=True), include_skipped=True) is None


@pytest.mark.parametrize('graph_enabled', [False, True])
def test_minimal_wire_keeps_declared_controls_and_strict_pair_validation(graph_enabled):
    from jsonschema import Draft202012Validator
    from workers_projects_runtime.agent_builder_control import (
        minimal_conversation_output_schema, conversation_controls_from_schema,
    )
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]} if graph_enabled else None
    delivery = messaging_delivery_control(audio_eligible=True)
    declared = conversation_output_schema(graph, delivery, same_turn_continuation=True)
    before = json.dumps(declared, sort_keys=True)
    wire = minimal_conversation_output_schema(declared)
    Draft202012Validator.check_schema(wire)
    assert conversation_controls_from_schema(wire) == conversation_controls_from_schema(declared)
    assert json.dumps(declared, sort_keys=True) == before
    assert 'description' not in wire and 'anyOf' not in wire and '$schema' not in wire
    answer = {'type': 'assistant_response', 'tool_name': None, 'voice': 'skip', 'content': 'Verified answer.'}
    assert Draft202012Validator(wire).is_valid(answer)
    assert parse_conversation_output(json.dumps(answer), graph, delivery)['delivery_disposition']['valid'] is True
    if graph_enabled:
        malformed = {**answer, 'type': 'tool_call'}
        assert Draft202012Validator(wire).is_valid(malformed)
        with pytest.raises(ValueError):
            parse_conversation_output(json.dumps(malformed), graph, delivery)
    with pytest.raises(ValueError, match='messaging controls'):
        minimal_conversation_output_schema(conversation_output_schema(graph, None) or {'type': 'object'})
