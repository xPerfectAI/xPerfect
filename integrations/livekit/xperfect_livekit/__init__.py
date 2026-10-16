"""Use xPerfect's existing API with the official LiveKit OpenAI plugin."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4
from urllib.parse import quote

from livekit.plugins.openai import LLM
from openai import AsyncOpenAI
from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletionChunk


class SpeechDispositionError(ValueError):
    """The stream cannot safely identify which public text may be spoken."""


def _eligible(value: object) -> bool:
    return (isinstance(value, dict) and type(value.get("version")) is int
            and value["version"] == 1 and value.get("audio") == "eligible"
            and value.get("required") is True and value.get("valid") is True
            and value.get("source") == "model")


class SpeechProjection:
    """Project declared speech only; retain the original public text for the host."""
    def __init__(self) -> None:
        self._offset = 0

    def text(self, content: str, fields: dict[str, Any]) -> str:
        offset = self._offset
        units = len(content.encode("utf-16-le")) // 2
        self._offset += units
        if "deliveryDispositionRanges" not in fields:
            # Missing or malformed decisions fail audio closed. No keyword/status guessing.
            return content if _eligible(fields.get("delivery_disposition")) else ""
        ranges = fields["deliveryDispositionRanges"]
        source = fields.get("delivery_range_text")
        if not isinstance(source, str) or not isinstance(ranges, list):
            raise SpeechDispositionError("Invalid speech range metadata")
        encoded = source.encode("utf-16-le")
        end = offset + units
        if encoded[offset * 2:end * 2] != content.encode("utf-16-le"):
            raise SpeechDispositionError("Speech range text does not match the streamed prefix")
        cursor = 0
        spoken: list[str] = []
        for part in ranges:
            if (not isinstance(part, dict) or type(part.get("start")) is not int
                    or type(part.get("end")) is not int or part["start"] != cursor
                    or part["end"] <= cursor or part["end"] * 2 > len(encoded)):
                raise SpeechDispositionError("Invalid speech range coverage")
            left, right = max(offset, cursor), min(end, part["end"])
            if left < right and _eligible(part.get("disposition")):
                try:
                    spoken.append(encoded[left * 2:right * 2].decode("utf-16-le"))
                except UnicodeDecodeError as error:
                    raise SpeechDispositionError("Speech range splits a Unicode character") from error
            cursor = part["end"]
        if cursor * 2 != len(encoded):
            raise SpeechDispositionError("Incomplete speech range coverage")
        return "".join(spoken)


class _CancelTurn:
    """Stop only this SDK stream's native request through its existing scoped receipt."""
    def __init__(self, client: AsyncOpenAI, key: str, headers: dict[str, Any],
                 report: Callable[[dict[str, Any]], None] | None) -> None:
        self._client, self._key, self._headers, self._report = client, key, headers, report
        self._task: asyncio.Task[None] | None = None

    async def _request(self) -> None:
        try:
            receipt = await self._client.post(
                "requests/by-idempotency/" + quote(self._key, safe="") + "/cancel",
                cast_to=dict[str, Any], body={},
                options={"headers": self._headers, "max_retries": 0, "timeout": 5.0},
            )
            fields = {"native_cancel": receipt}
        except Exception as error:
            fields = {"native_cancel": {"status": "failed", "error_type": type(error).__name__,
                                        "capacityReleased": None}}
            logging.getLogger(__name__).warning("Native voice turn cancellation was not acknowledged")
        if self._report:
            try:
                self._report(fields)
            except Exception:
                logging.getLogger(__name__).warning("Native cancellation receipt callback failed")

    async def __call__(self) -> None:
        if not self._key:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._request())
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            # The SDK is cancelling its generation task; finish this bounded Stop receipt.
            await asyncio.shield(self._task)
            raise


class _SpeechStream:
    def __init__(self, stream: Any, on_text: Callable[[str], None] | None,
                 on_metadata: Callable[[dict[str, Any]], None] | None, cancel: _CancelTurn) -> None:
        self._stream = stream
        self._cancel = cancel
        self._terminal = False
        self._projections: dict[int, SpeechProjection] = {}
        self._on_text = on_text
        self._on_metadata = on_metadata

    @property
    def response(self) -> Any:
        return self._stream.response

    async def __aenter__(self) -> _SpeechStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        try:
            await self._stream.__aexit__(*args)
        finally:
            if not self._terminal:
                await self._cancel()

    async def close(self) -> None:
        try:
            await self._stream.close()
        finally:
            if not self._terminal:
                await self._cancel()

    async def __aiter__(self):
        async for chunk in self._stream:
            if not isinstance(chunk, ChatCompletionChunk):
                raise SpeechDispositionError("Expected a typed chat completion stream")
            # Native conversation API emits one choice. Observe its terminal receipt before yielding.
            if any(choice.finish_reason is not None for choice in chunk.choices):
                self._terminal = True
            choices = []
            for choice in chunk.choices:
                delta = choice.delta
                fields = getattr(delta, "provider_specific_fields", None)
                fields = fields.get("viventium", {}) if isinstance(fields, dict) else {}
                fields = fields if isinstance(fields, dict) else {}
                if fields and self._on_metadata:
                    self._on_metadata(fields)
                if delta.content:
                    speech = self._projections.setdefault(choice.index, SpeechProjection()).text(delta.content, fields)
                    if self._on_text:
                        self._on_text(delta.content)
                    delta = delta.model_copy(update={"content": speech or None})
                choices.append(choice.model_copy(update={"delta": delta}))
            yield chunk.model_copy(update={"choices": choices})


class _SpeechCompletions(AsyncCompletions):
    def __init__(self, client: AsyncOpenAI, on_text: Callable[[str], None] | None,
                 on_metadata: Callable[[dict[str, Any]], None] | None) -> None:
        super().__init__(client)
        self._on_text, self._on_metadata = on_text, on_metadata

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        metadata = (kwargs.get("extra_body") or {}).get("metadata") or {}
        cancel = _CancelTurn(self._client, str(metadata.get("idempotency_key") or ""),
                             dict(kwargs.get("extra_headers") or {}), self._on_metadata)
        try:
            stream = await super().create(*args, **kwargs)
        except asyncio.CancelledError:
            if kwargs.get("stream"):
                await cancel()
            raise
        return _SpeechStream(stream, self._on_text, self._on_metadata, cancel) if kwargs.get("stream") else stream



class _ClientLLM(LLM):
    def __init__(self, *, client: AsyncOpenAI, close_client: bool, conversation_id: str,
                 **options: Any) -> None:
        self._base_body = dict(options.pop("extra_body", {}))
        self._conversation_id = conversation_id
        super().__init__(client=client, **options)
        self._xperfect_client, self._close_client = client, close_client

    def chat(self, **kwargs: Any):
        extra = dict(kwargs.pop("extra_kwargs", {}))
        body = {**self._base_body, **extra.get("extra_body", {})}
        metadata = dict(body.get("metadata", {}))
        identity = uuid4().hex
        metadata.setdefault("idempotency_key", identity)
        metadata.setdefault("message_id", identity)
        metadata.setdefault("stream_id", identity)
        required = {"conversation_id": self._conversation_id, "surface": "voice",
                    "input_mode": "voice", "audio_eligible": True,
                    "main_context_owner": "provider_legacy"}
        for name, value in required.items():
            if name in metadata and metadata[name] != value:
                raise ValueError("Voice metadata conflicts with the configured call")
        body["metadata"] = {**metadata, **required}
        extra["extra_body"] = body
        # One identity per SDK stream, retained by its internal retries.
        return super().chat(extra_kwargs=extra, **kwargs)

    async def aclose(self) -> None:
        await super().aclose()
        if self._close_client:
            await self._xperfect_client.close()


def create_llm(*, base_url: str, api_key: str, model: str, conversation_id: str,
               reasoning_effort: str = "high", on_text: Callable[[str], None] | None = None,
               on_metadata: Callable[[dict[str, Any]], None] | None = None,
               client: AsyncOpenAI | None = None, **options: Any) -> LLM:
    """One LLM/client per call; STT, TTS and the room remain normal LiveKit choices."""
    if not conversation_id.strip():
        raise ValueError("A stable conversation_id is required")
    headers = dict(options.pop("extra_headers", {}))
    required = {"X-Viventium-Conversation-Id": conversation_id,
                "X-Viventium-Surface": "voice", "X-Viventium-Input-Mode": "voice",
                "X-Viventium-Audio-Eligible": "true"}
    for name, value in headers.items():
        expected = next((item for key, item in required.items() if key.lower() == name.lower()), value)
        if value != expected:
            raise ValueError("Voice identity headers conflict with the configured call")
    headers.update(required)
    close_client = client is None
    client_options = {name: options.pop(name) for name in ("timeout", "max_retries") if name in options}
    if client is not None and client_options:
        raise ValueError("Configure transport timeout/retries on the supplied OpenAI client")
    if client is None:
        client_options.setdefault("max_retries", 0)
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, **client_options)
    client.chat.completions = _SpeechCompletions(client, on_text, on_metadata)
    return _ClientLLM(client=client, close_client=close_client, conversation_id=conversation_id, model=model, reasoning_effort=reasoning_effort,
               extra_headers=headers, **options)


__all__ = ["create_llm", "SpeechDispositionError", "SpeechProjection"]
