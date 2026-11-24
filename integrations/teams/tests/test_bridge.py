"""Recorded-shape Teams activities against a fake xPerfect runtime."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from xperfect_teams.bridge import (FIRST_START_WAITS, Bridge, Readiness, RuntimeClient, inbound_message,
                                   start_with_retries)


def activity(activity_id="act-1", text="Hello", sender="00000000-0000-0000-0000-00000000000a"):
    return SimpleNamespace(id=activity_id, text=text, timestamp=None, service_url="https://smba.example.invalid/",
                           from_=SimpleNamespace(id="29:synthetic", aad_object_id=sender, name="Synthetic Person"),
                           conversation=SimpleNamespace(id="a:synthetic-chat", tenant_id="tenant-synthetic",
                                                        conversation_type="personal"),
                           channel_data=None)


class FakeRuntime:
    def __init__(self):
        self.inbound, self.results, self.queue, self.claims = [], [], [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/inbound"):
            body = json.loads(request.content)
            seen = any(item["activity_id"] == body["activity_id"] for item in self.inbound)
            self.inbound.append(body)
            return httpx.Response(200, json={"accepted": True, "duplicate": seen})
        if path.endswith("/deliveries/claim"):
            self.claims.append(dict(request.url.params))
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

    assert asyncio.run(bridge.deliver_once(send)) == 3  # how many were claimed; one was sent
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


class Refusal(RuntimeError):
    """The SDK's socket negotiation error, by its fields."""

    def __init__(self, status_code, terminal=True):
        super().__init__(f"Socket Mode negotiate failed: HTTP {status_code}")
        self.status_code, self.terminal = status_code, terminal


def starter(outcomes):
    calls = []

    async def start():
        calls.append(1)
        outcome = outcomes.pop(0) if outcomes else outcomes_default
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    outcomes_default = Refusal(401)
    return start, calls


def test_a_new_bots_first_refusals_are_tried_again_with_growing_waits():
    # The first real test: one region answered 403, then 401, and the third start held.
    start, _ = starter([Refusal(403), Refusal(401), "serving"])
    slept, told = [], []

    async def sleep(seconds):
        slept.append(seconds)

    async def gave_up(error):
        told.append(error)
    assert asyncio.run(start_with_retries(start, lambda: False, sleep=sleep, gave_up=gave_up)) == "serving"
    assert slept == list(FIRST_START_WAITS[:2]) and told == []


def test_other_errors_and_a_refusal_after_the_socket_held_are_not_tried_again():
    for error, held in ((Refusal(500, terminal=False), False), (Refusal(429, terminal=False), False),
                        (Refusal(401, terminal=False), False), (Refusal(401), True), (ValueError("bad"), False)):
        start, calls = starter([error])
        slept = []

        async def sleep(seconds):
            slept.append(seconds)
        with pytest.raises(type(error)):
            asyncio.run(start_with_retries(start, lambda: held, sleep=sleep))
        assert calls == [1] and slept == []


def test_a_lasting_refusal_ends_after_a_short_budget_and_is_reported_once():
    start, calls = starter([])  # refused every time
    slept, told = [], []

    async def sleep(seconds):
        slept.append(seconds)

    async def gave_up(error):
        told.append(str(error))
    with pytest.raises(Refusal):
        asyncio.run(start_with_retries(start, lambda: False, sleep=sleep, gave_up=gave_up))
    assert len(calls) == len(FIRST_START_WAITS) + 1 and slept == list(FIRST_START_WAITS)
    assert told == ["Socket Mode negotiate failed: HTTP 401"]
    assert list(FIRST_START_WAITS) == sorted(FIRST_START_WAITS) and sum(FIRST_START_WAITS) <= 150


def test_a_failed_report_never_hides_the_refusal():
    start, _ = starter([])

    async def sleep(seconds):
        pass

    async def gave_up(error):
        raise httpx.ConnectError("runtime unreachable")
    with pytest.raises(Refusal):
        asyncio.run(start_with_retries(start, lambda: False, waits=(), sleep=sleep, gave_up=gave_up))


def test_replies_and_online_wait_until_the_socket_has_held_once():
    statuses = iter(["idle", "connecting", "ready", "disconnected", "connecting"])
    readiness = Readiness(lambda: next(statuses))
    # Held stays held: the SDK's overall status drops for a moment whenever one region reconnects.
    assert [readiness.check() for _ in range(5)] == [False, False, True, True, True]
    assert Readiness(None).check()  # served over HTTP: nothing to wait for


def test_the_heartbeat_says_connecting_ready_or_the_refusal():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})
    client = RuntimeClient("http://runtime.invalid", "synthetic-token", "chb_synthetic",
                           transport=httpx.MockTransport(handler))

    async def beats():
        await client.heartbeat()
        await client.heartbeat("connecting")
        await client.heartbeat("refused", "Socket Mode negotiate failed: HTTP 401 " + "x" * 500)
    asyncio.run(beats())
    assert bodies[0] == {} and bodies[1] == {"socket": "connecting"}
    assert bodies[2]["socket"] == "refused" and len(bodies[2]["error"]) == 400


def test_a_claim_asks_the_core_to_answer_as_soon_as_a_reply_is_ready():
    # The wait before a ready reply left was the package's one-second poll (0.1 to 0.8 s on real Teams). The claim
    # now waits at the core, which answers as soon as a reply is ready; what a claim means is unchanged.
    runtime, bridge = make()
    runtime.queue = [{"delivery_id": "dlv_1", "text": "One", "thread_ref": {"conversation_id": "c"}}]

    async def send(thread_ref, text):
        return "platform-1"

    assert asyncio.run(bridge.deliver_once(send, wait=5.0)) == 1
    assert asyncio.run(bridge.deliver_once(send)) == 0
    assert runtime.claims == [{"limit": "10", "wait": "5"}, {"limit": "10"}]
