"""Recorded-shape Teams activities against a fake xPerfect runtime."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from xperfect_teams.bridge import Bridge, RuntimeClient, inbound_message


def activity(activity_id="act-1", text="Hello", sender="00000000-0000-0000-0000-00000000000a"):
    return SimpleNamespace(id=activity_id, text=text, timestamp=None, service_url="https://smba.example.invalid/",
                           from_=SimpleNamespace(id="29:synthetic", aad_object_id=sender, name="Synthetic Person"),
                           conversation=SimpleNamespace(id="a:synthetic-chat", tenant_id="tenant-synthetic",
                                                        conversation_type="personal"),
                           channel_data=None)


class FakeRuntime:
    def __init__(self):
        self.inbound, self.results, self.queue = [], [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/inbound"):
            body = json.loads(request.content)
            seen = any(item["activity_id"] == body["activity_id"] for item in self.inbound)
            self.inbound.append(body)
            return httpx.Response(200, json={"accepted": True, "duplicate": seen})
        if path.endswith("/deliveries/claim"):
            items, self.queue = self.queue, []
            return httpx.Response(200, json={"items": items})
        if "/deliveries/" in path:
            self.results.append((path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={})
        return httpx.Response(404)


def make():
    runtime = FakeRuntime()
    client = RuntimeClient("http://runtime.invalid", "synthetic-token", "chb_synthetic",
                           transport=httpx.MockTransport(runtime.handler))
    return runtime, Bridge(client, bot_id="synthetic-bot")


def test_text_message_maps_to_typed_inbound_message():
    message = inbound_message(activity())
    assert message["person"] == {"id": "00000000-0000-0000-0000-00000000000a", "name": "Synthetic Person",
                                 "tenant": "tenant-synthetic"}
    assert message["thread_key"] == "00000000-0000-0000-0000-00000000000a:a:synthetic-chat"
    assert message["thread_ref"]["conversation_id"] == "a:synthetic-chat"
    assert message["text"] == "Hello"


def test_empty_and_own_messages_are_ignored():
    assert inbound_message(activity(text="  ")) is None
    own = activity(); own.from_.id = "28:synthetic-bot"
    assert inbound_message(own, "synthetic-bot") is None


def test_redelivered_activity_shows_typing_only_once():
    runtime, bridge = make()
    typing = []

    async def show():
        typing.append(1)

    async def scenario():
        await bridge.on_message(activity(), show)
        await bridge.on_message(activity(), show)

    asyncio.run(scenario())
    assert len(runtime.inbound) == 2 and len(typing) == 1


def test_outbox_reports_sent_refused_and_leaves_unknown_outcomes_unconfirmed():
    runtime, bridge = make()
    runtime.queue = [{"delivery_id": "dlv_sent", "text": "One", "thread_ref": {"conversation_id": "c"}},
                     {"delivery_id": "dlv_refused", "text": "Two", "thread_ref": {"conversation_id": "c"}},
                     {"delivery_id": "dlv_unknown", "text": "Three", "thread_ref": {"conversation_id": "c"}}]
    calls = []

    async def send(thread_ref, text):
        calls.append(text)
        if text == "Two":
            raise httpx.HTTPStatusError("refused", request=httpx.Request("POST", "https://x.invalid"),
                                        response=httpx.Response(403))
        if text == "Three":
            raise httpx.ReadTimeout("no answer")
        return "platform-1"

    assert asyncio.run(bridge.deliver_once(send)) == 1
    assert calls == ["One", "Two", "Three"]
    assert runtime.results == [("dlv_sent", {"state": "sent", "platform_message_id": "platform-1", "error": ""}),
                               ("dlv_refused", {"state": "failed", "platform_message_id": "", "error": "Teams refused the message (403)"})]
    # Nothing was reported for the unknown outcome; the core marks it not confirmed.


def test_only_personal_chats_are_answered():
    personal = activity()
    group = activity(activity_id="act-2")
    group.conversation = SimpleNamespace(id="19:group", tenant_id="tenant-synthetic", conversation_type="groupChat",
                                         is_group=True)
    team_channel = activity(activity_id="act-3")
    team_channel.conversation = SimpleNamespace(id="19:channel", tenant_id="tenant-synthetic", conversation_type="channel")
    untyped = activity(activity_id="act-4")
    untyped.conversation = SimpleNamespace(id="a:untyped", tenant_id="tenant-synthetic")
    assert inbound_message(personal) is not None
    # A reply in a shared chat would be read by people the owner never listed.
    assert inbound_message(group) is None and inbound_message(team_channel) is None and inbound_message(untyped) is None
