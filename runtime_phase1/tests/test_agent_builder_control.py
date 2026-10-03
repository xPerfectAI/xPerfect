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
)
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
        assert parse_conversation_output(json.dumps(transfer), graph, delivery) == {
            "type": "tool_call",
            "content": "",
            "tool_name": "lc_transfer_to_specialist",
        }


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


def test_non_streaming_graph_choice_returns_one_openai_tool_call(monkeypatch):
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
    monkeypatch.setattr(
        provider,
        "_conversation_output",
        lambda request_record, run: json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
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
    '{"content":"Private until controls", "type":"assistant_response", "tool_name":null, "voice":"eligible"}',
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
