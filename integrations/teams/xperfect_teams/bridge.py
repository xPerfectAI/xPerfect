"""Map Teams activities to xPerfect's channel routes, and the outbox back to Teams."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger("xperfect_teams")


class RuntimeClient:
    """The channel routes of one xPerfect runtime, with the owner's runtime credential."""

    def __init__(self, base_url: str, token: str, binding_id: str, *, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.binding_id = binding_id
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                       headers={"Authorization": f"Bearer {token}"})

    async def _post(self, path: str, body: dict | None = None) -> dict:
        response = await self._http.post(path, json=body or {})
        response.raise_for_status()
        return response.json()

    async def package(self) -> dict:
        response = await self._http.get(f"/v1/channels/{self.binding_id}/package")
        response.raise_for_status()
        return response.json()

    async def heartbeat(self) -> dict:
        return await self._post(f"/v1/channels/{self.binding_id}/heartbeat")

    async def inbound(self, message: dict) -> dict:
        return await self._post(f"/v1/channels/{self.binding_id}/inbound", message)

    async def claim(self, limit: int = 10) -> list[dict]:
        return (await self._post(f"/v1/channels/{self.binding_id}/deliveries/claim?limit={limit}")).get("items", [])

    async def delivered(self, delivery_id: str, state: str, platform_message_id: str = "", error: str = "") -> dict:
        return await self._post(f"/v1/channels/{self.binding_id}/deliveries/{delivery_id}",
                                {"state": state, "platform_message_id": platform_message_id, "error": error[:2000]})

    async def close(self) -> None:
        await self._http.aclose()


def _value(obj: Any, *names: str) -> Any:
    for name in names:
        if obj is None:
            return None
        obj = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return obj


def inbound_message(activity: Any, bot_id: str = "") -> dict | None:
    """One Teams message as the core's typed inbound message; None for anything else."""
    text = str(_value(activity, "text") or "").strip()
    sender = _value(activity, "from_") or _value(activity, "from")
    sender_id = str(_value(sender, "aad_object_id") or _value(sender, "id") or "")
    if not text or not sender_id or (bot_id and str(_value(sender, "id") or "").endswith(bot_id)):
        return None
    conversation = _value(activity, "conversation")
    # Personal chats only for now: a reply in a group chat or a channel would be read by people
    # the owner never listed. An unknown chat type is treated as shared.
    kind = str(_value(conversation, "conversation_type") or _value(conversation, "conversationType") or "")
    if kind != "personal" or _value(conversation, "is_group") is True:
        return None
    conversation_id = str(_value(conversation, "id") or "")
    tenant = str(_value(conversation, "tenant_id") or _value(activity, "channel_data", "tenant", "id") or "")
    timestamp = _value(activity, "timestamp")
    return {
        "activity_id": str(_value(activity, "id") or ""),
        "person": {"id": sender_id, "name": str(_value(sender, "name") or "")[:200], "tenant": tenant},
        # One conversation per person and per Teams chat.
        "thread_key": f"{sender_id}:{conversation_id}",
        "thread_ref": {"conversation_id": conversation_id, "service_url": str(_value(activity, "service_url") or ""),
                       "tenant": tenant},
        "text": text,
        "sent_at": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp or ""),
    }


class Bridge:
    def __init__(self, client: RuntimeClient, *, bot_id: str = "") -> None:
        self.client, self.bot_id = client, bot_id

    async def on_message(self, activity: Any, show_typing: Callable[[], Awaitable[Any]] | None = None) -> dict:
        message = inbound_message(activity, self.bot_id)
        if message is None or not message["activity_id"]:
            return {"accepted": False, "reason": "not_a_text_message"}
        result = await self.client.inbound(message)
        # A person who is not listed gets no answer; the owner sees them in xPerfect.
        if result.get("accepted") and not result.get("duplicate") and show_typing is not None:
            try:
                await show_typing()
            except Exception:  # Typing is only a courtesy.
                logger.debug("typing indicator failed", exc_info=True)
        return result

    async def deliver_once(self, send: Callable[[dict, str], Awaitable[str]]) -> int:
        """Send each claimed message once. An unknown outcome is never resent by this package."""
        sent = 0
        for delivery in await self.client.claim():
            try:
                platform_id = await send(delivery["thread_ref"], delivery["text"])
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if isinstance(status, int) and 400 <= status < 500:
                    # Teams refused it outright, so it was not delivered.
                    await self.client.delivered(delivery["delivery_id"], "failed", error=f"Teams refused the message ({status})")
                else:
                    # It may or may not have arrived. The core marks it not confirmed and shows the owner.
                    logger.warning("send outcome unknown for %s", delivery["delivery_id"])
                continue
            await self.client.delivered(delivery["delivery_id"], "sent", platform_message_id=str(platform_id or ""))
            sent += 1
        return sent
