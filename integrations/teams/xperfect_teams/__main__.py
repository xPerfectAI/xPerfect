"""Run one Expert's Teams channel: python -m xperfect_teams --runtime URL --binding ID --state-dir DIR.

On this computer the bot uses Teams socket mode: it dials out to Microsoft, so nothing on this
computer is reachable from the internet. On a server, --http-port serves the Teams endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from .bridge import Bridge, RuntimeClient

logger = logging.getLogger("xperfect_teams")


def read_credentials(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"CLIENT_ID", "CLIENT_SECRET", "TENANT_ID"}:
            values[key.strip()] = value.strip().strip('"')
    missing = {"CLIENT_ID", "CLIENT_SECRET", "TENANT_ID"} - set(values)
    if missing:
        raise SystemExit(f"Teams credentials are incomplete: {', '.join(sorted(missing))}")
    return values


async def run(args: argparse.Namespace) -> None:
    from microsoft_teams.api import TypingActivityInput
    from microsoft_teams.apps import App

    state = Path(args.state_dir)
    token = json.loads((state / "secrets.json").read_text())["api_token"]
    credentials = read_credentials(Path(args.credentials))
    client = RuntimeClient(args.runtime, token, args.binding)
    options = dict(client_id=credentials["CLIENT_ID"], client_secret=credentials["CLIENT_SECRET"],
                   tenant_id=credentials["TENANT_ID"])
    app = App(**options) if args.http_port else App(**options, socket_mode=True)
    bridge = Bridge(client, bot_id=credentials["CLIENT_ID"])

    @app.on_message
    async def on_message(ctx):  # noqa: ANN001 - the SDK's context type
        await bridge.on_message(ctx.activity, lambda: ctx.send(TypingActivityInput()))

    async def send(thread_ref: dict, text: str) -> str:
        sent = await app.send(thread_ref["conversation_id"], text, service_url=thread_ref.get("service_url") or None)
        return str(getattr(sent, "id", "") or "")

    async def deliveries() -> None:
        while True:
            try:
                await bridge.deliver_once(send)
            except Exception:
                logger.warning("delivery round failed", exc_info=True)
            await asyncio.sleep(1)

    async def heartbeat() -> None:
        while True:
            try:
                await client.heartbeat()
            except Exception:
                logger.warning("heartbeat failed", exc_info=True)
            await asyncio.sleep(15)

    await app.initialize()
    await asyncio.gather(app.start(args.http_port or None), deliveries(), heartbeat())


def main() -> None:
    parser = argparse.ArgumentParser(prog="xperfect_teams")
    parser.add_argument("--runtime", required=True, help="xPerfect runtime API base URL")
    parser.add_argument("--binding", required=True, help="channel binding id")
    parser.add_argument("--state-dir", required=True, help="xPerfect private state folder")
    parser.add_argument("--credentials", required=True, help="private file with CLIENT_ID, CLIENT_SECRET, TENANT_ID")
    parser.add_argument("--http-port", type=int, default=0, help="serve the Teams endpoint over HTTP instead of socket mode")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("XPERFECT_TEAMS_LOG", "INFO"))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
