import asyncio
import json

import httpx
import pytest
from livekit.agents.llm import ChatContext
from livekit.agents.types import APIConnectOptions
from openai import AsyncOpenAI

from xperfect_livekit import SpeechDispositionError, SpeechProjection, create_llm


def decision(audio="eligible", **kwargs):
    return {"version": 1, "audio": audio, "required": True, "valid": True,
            "source": "model", **kwargs}


def fields(audio="eligible"):
    return {"delivery_disposition": decision(audio)}


@pytest.mark.parametrize("value", [None, {}, decision("skip"), decision(valid=False),
    decision(required=False), decision(version=True), decision(source="required_missing")])
def test_only_valid_explicit_audio_is_spoken(value):
    projection = SpeechProjection()
    assert projection.text("Visible but quiet.", {"delivery_disposition": value}) == ""
    assert projection.text("Speak now.", fields()) == "Speak now."


def test_mixed_ranges_use_utf16_and_include_skipped_prefix():
    text = "Quiet 🟢. Spoken."
    split = len("Quiet 🟢.".encode("utf-16-le")) // 2
    ranges = [{"start": 0, "end": split, "disposition": decision("skip")},
              {"start": split, "end": len(text.encode("utf-16-le")) // 2,
               "disposition": decision()}]
    metadata = {**fields(), "deliveryDispositionRanges": ranges, "delivery_range_text": text}
    assert SpeechProjection().text(text, metadata) == " Spoken."
    projection = SpeechProjection()
    assert projection.text("Quiet 🟢.", fields("skip")) == ""
    assert projection.text(" Spoken.", metadata) == " Spoken."


@pytest.mark.parametrize("change", ["gap", "overlap", "missing_source", "changed_text", "incomplete"])
def test_invalid_ranges_never_release_text(change):
    metadata = {**fields(), "delivery_range_text": "Quiet.Spoken.",
        "deliveryDispositionRanges": [{"start": 0, "end": 6, "disposition": decision("skip")},
                                      {"start": 6, "end": 13, "disposition": decision()}]}
    if change == "gap": metadata["deliveryDispositionRanges"][1]["start"] = 7
    if change == "overlap": metadata["deliveryDispositionRanges"][1]["start"] = 5
    if change == "missing_source": del metadata["delivery_range_text"]
    if change == "changed_text": metadata["delivery_range_text"] = "Changed text."
    if change == "incomplete": metadata["deliveryDispositionRanges"].pop()
    with pytest.raises(SpeechDispositionError):
        SpeechProjection().text("Quiet.Spoken.", metadata)


def chunk(content=None, metadata=None, *, finish=None, usage=None, tool_calls=None):
    delta = {}
    if content is not None: delta["content"] = content
    if metadata is not None: delta["provider_specific_fields"] = {"viventium": metadata}
    if tool_calls: delta["tool_calls"] = tool_calls
    event = {"id": "synthetic-response", "object": "chat.completion.chunk", "created": 1,
             "model": "grok-build:grok-4.7-build-fast", "choices": [
                 {"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None: event["usage"] = usage
    return ("data: " + json.dumps(event) + "\n\n").encode()


def test_real_livekit_sdk_streams_eligible_words_before_completion_and_keeps_full_text():
    async def check():
        release = asyncio.Event()
        public, metadata, requests = [], [], []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                first = chunk("Hello ", fields())
                yield first[:17]
                yield first[17:]
                yield chunk("Saved internally.", fields("skip"))
                yield chunk(metadata={"activity": {"status": "working"}})
                await release.wait()
                yield chunk("world.", fields())
                yield chunk(finish="stop", usage={"prompt_tokens": 3, "completion_tokens": 4,
                                                  "total_tokens": 7})
                yield b"data: [DONE]\n\n"

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

        client = AsyncOpenAI(api_key="synthetic-test-only", base_url="https://xperfect.example.test/v1",
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model = create_llm(base_url=str(client.base_url), api_key="synthetic-test-only",
            model="grok-build:grok-4.7-build-fast", conversation_id="synthetic-call",
            on_text=public.append, on_metadata=metadata.append, client=client,
            temperature=0.3, extra_body={"metadata": {"agent_id": "configured-queen"}})
        context = ChatContext.empty()
        context.add_message(role="user", content="Hello")
        stream = model.chat(chat_ctx=context, conn_options=APIConnectOptions(max_retry=0))
        try:
            first = await asyncio.wait_for(stream.__anext__(), 2)
            assert first.delta.content == "Hello "
            assert not release.is_set()  # A real SDK text chunk arrived before model completion.
            release.set()
            rest = [part async for part in stream]
            assert "".join(part.delta.content or "" for part in [first, *rest] if part.delta) == "Hello world."
            assert "".join(public) == "Hello Saved internally.world."
            assert any(item.get("activity") for item in metadata)
            assert rest[-1].usage.total_tokens == 7
            body = json.loads(requests[0].content)
            assert body["model"] == "grok-build:grok-4.7-build-fast"
            assert body["reasoning_effort"] == "high"
            assert body["temperature"] == 0.3
            assert body["metadata"]["agent_id"] == "configured-queen"
            assert body["metadata"]["audio_eligible"] is True
            assert body["metadata"]["surface"] == "voice"
            assert body["metadata"]["main_context_owner"] == "provider_legacy"
            assert body["metadata"]["conversation_id"] == "synthetic-call"
            assert body["metadata"]["idempotency_key"]
            assert body["metadata"]["message_id"]
            assert requests[0].headers["X-Viventium-Audio-Eligible"] == "true"
            assert requests[0].headers["X-Viventium-Conversation-Id"] == "synthetic-call"
        finally:
            release.set()
            await stream.aclose()
            await model.aclose()
            await client.close()
    asyncio.run(check())


def test_tools_usage_and_missing_disposition_are_not_accidentally_spoken():
    async def check():
        tool = {"index": 0, "id": "tool-one", "type": "function", "function": {
            "name": "example_tool", "arguments": '{"value":1}'}}
        body = chunk("Do not speak untyped text.") + chunk(tool_calls=[tool]) + chunk(finish="tool_calls") + b"data: [DONE]\n\n"
        client = AsyncOpenAI(api_key="synthetic-test-only", base_url="https://xperfect.example.test/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))))
        model = create_llm(base_url=str(client.base_url), api_key="synthetic-test-only", client=client,
                           model="configured-model", conversation_id="synthetic-call")
        context = ChatContext.empty(); context.add_message(role="user", content="Hello")
        stream = model.chat(chat_ctx=context, conn_options=APIConnectOptions(max_retry=0))
        try:
            output = [part async for part in stream]
            assert all(not part.delta.content for part in output if part.delta)
            assert output[-1].delta.tool_calls[0].name == "example_tool"
            assert output[-1].delta.tool_calls[0].arguments == '{"value":1}'
        finally:
            await stream.aclose(); await model.aclose(); await client.close()
    asyncio.run(check())


def test_factory_closes_owned_client_and_applies_transport_options():
    async def check():
        model = create_llm(base_url="https://xperfect.example.test/v1", api_key="synthetic-only",
                           model="configured-model", conversation_id="synthetic-call")
        client = model._xperfect_client
        assert client.max_retries == 0
        assert not client.is_closed()
        await model.aclose()
        assert client.is_closed()
    asyncio.run(check())


def test_supplied_client_and_extra_headers_keep_their_owner():
    async def check():
        client = AsyncOpenAI(api_key="synthetic-only", base_url="https://xperfect.example.test/v1")
        model = create_llm(base_url=str(client.base_url), api_key="synthetic-only", client=client,
            model="configured-model", conversation_id="synthetic-call", extra_headers={"X-Example": "value"})
        await model.aclose()
        assert not client.is_closed()
        assert model._opts.extra_headers["X-Example"] == "value"
        with pytest.raises(ValueError, match="identity"):
            create_llm(base_url=str(client.base_url), api_key="synthetic-only", client=client,
                model="configured-model", conversation_id="synthetic-call",
                extra_headers={"x-viventium-conversation-id": "a-different-call"})
        await client.close()
    asyncio.run(check())


def test_real_sdk_mixed_terminal_ranges_never_speak_the_quiet_part():
    async def check():
        text = "Quiet 🟢. Spoken."
        split = len("Quiet 🟢.".encode("utf-16-le")) // 2
        metadata = {**fields(), "delivery_range_text": text, "deliveryDispositionRanges": [
            {"start": 0, "end": split, "disposition": decision("skip")},
            {"start": split, "end": len(text.encode("utf-16-le")) // 2, "disposition": decision()}]}
        body = chunk(text, metadata) + chunk(finish="stop") + b"data: [DONE]\n\n"
        public=[]
        client = AsyncOpenAI(api_key="synthetic-only", base_url="https://xperfect.example.test/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={"content-type":"text/event-stream"},content=body))))
        model = create_llm(base_url=str(client.base_url), api_key="synthetic-only", client=client,
                           model="configured-model", conversation_id="synthetic-call", on_text=public.append)
        context=ChatContext.empty();context.add_message(role="user",content="Hello")
        stream=model.chat(chat_ctx=context,conn_options=APIConnectOptions(max_retry=0))
        try:
            result=[part async for part in stream]
            assert "".join(part.delta.content or "" for part in result if part.delta)==" Spoken."
            assert "".join(public)==text
        finally:
            await stream.aclose();await model.aclose();await client.close()
    asyncio.run(check())


def test_each_chat_has_one_retry_stable_identity_and_next_turn_gets_a_new_one():
    async def check():
        requests=[]
        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests)==1:
                return httpx.Response(503,json={"error":{"message":"Synthetic temporary failure","type":"server_error"}})
            return httpx.Response(200,headers={"content-type":"text/event-stream"},
                content=chunk("Hello.",fields())+chunk(finish="stop")+b"data: [DONE]\n\n")
        client=AsyncOpenAI(api_key="synthetic-only",base_url="https://xperfect.example.test/v1",max_retries=0,
                          http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model=create_llm(base_url=str(client.base_url),api_key="synthetic-only",client=client,
                         model="configured-model",conversation_id="synthetic-call")
        context=ChatContext.empty();context.add_message(role="user",content="Hello")
        for _ in range(2):
            stream=model.chat(chat_ctx=context,conn_options=APIConnectOptions(max_retry=1,retry_interval=.01))
            try:
                assert "".join([part.delta.content or "" async for part in stream if part.delta])=="Hello."
            finally:await stream.aclose()
        assert len(requests)==3
        assert requests[0]["metadata"]==requests[1]["metadata"]
        first,last=requests[0]["metadata"],requests[2]["metadata"]
        for name in ["message_id","stream_id","idempotency_key"]:
            assert first[name] and first[name]!=last[name]
        assert first["conversation_id"]==last["conversation_id"]=="synthetic-call"
        await model.aclose();await client.close()
    asyncio.run(check())


@pytest.mark.parametrize("phase", ["headers", "stream", "terminal", "cancel-failed"])
def test_sdk_interrupt_cancels_only_exact_native_turn_once_and_reports_receipt(phase):
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        requests, receipts = [], []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield chunk("Speaking now. ", fields())
                if phase == "terminal":
                    yield chunk(finish="stop")
                    yield b"data: [DONE]\n\n"
                else:
                    await release.wait()

        async def handler(request):
            requests.append(request)
            if request.url.path.endswith("/cancel"):
                if phase == "cancel-failed":
                    return httpx.Response(503, json={"error": "synthetic unavailable"})
                return httpx.Response(200, json={"id": "synthetic-response", "state": "cancelled",
                                                "capacityReleased": False})
            started.set()
            if phase == "headers":
                await release.wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

        client = AsyncOpenAI(api_key="synthetic-only", base_url="https://xperfect.example.test/v1",
            max_retries=3, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model = create_llm(base_url=str(client.base_url), api_key="synthetic-only", client=client,
            model="configured-model", conversation_id="synthetic-call", on_metadata=receipts.append,
            extra_headers={"X-Viventium-User-Id": "synthetic-owner"})
        context = ChatContext.empty(); context.add_message(role="user", content="Hello")
        stream = model.chat(chat_ctx=context, conn_options=APIConnectOptions(max_retry=0))
        try:
            if phase == "headers":
                await asyncio.wait_for(started.wait(), 2)
            elif phase == "terminal":
                assert [part async for part in stream]
            else:
                first = await asyncio.wait_for(stream.__anext__(), 2)
                assert first.delta.content == "Speaking now. "
            await asyncio.wait_for(stream.aclose(), 2)
            await stream.aclose()
            cancellations = [request for request in requests if request.url.path.endswith("/cancel")]
            if phase == "terminal":
                assert cancellations == []
                assert not any("native_cancel" in fields for fields in receipts)
            else:
                assert len(cancellations) == 1
                identity = json.loads(requests[0].content)["metadata"]["idempotency_key"]
                assert cancellations[0].url.path == f"/v1/requests/by-idempotency/{identity}/cancel"
                assert cancellations[0].headers["X-Viventium-User-Id"] == "synthetic-owner"
                assert cancellations[0].headers["authorization"] == requests[0].headers["authorization"]
                receipt = [fields["native_cancel"] for fields in receipts if "native_cancel" in fields]
                assert len(receipt) == 1
                if phase == "cancel-failed":
                    assert receipt[0]["status"] == "failed"
                    assert receipt[0]["capacityReleased"] is None
                else:
                    assert receipt[0].get("state") == "cancelled", receipt
                    assert receipt[0]["capacityReleased"] is False  # Never infer released from HTTP200.
        finally:
            release.set()
            await stream.aclose(); await model.aclose(); await client.close()
    asyncio.run(check())
