# xPerfect Teams channel

Lets the people an owner chooses message an xPerfect Expert in Microsoft Teams. The package speaks
Teams through Microsoft's Teams SDK for Python and talks to xPerfect only through its channel
routes. It never decides what a message means. Requirements and limits:
[Experts on channels](../../docs/10_Experts_and_Channels.md).

## How an owner connects it

In xPerfect: **My experts → Manage → Channels → Add Teams → Connect**. Complete Microsoft's sign-in
on Microsoft's page with the code shown. xPerfect then creates a Teams-managed bot with the pinned
Teams developer CLI (`@microsoft/teams.cli` 3.1.0), keeps the app secret in a private file in its
state folder, adds you as the first person who may write, and starts this package. Use **Install in
Teams**, then **Test in Teams**. No terminal step is needed.

## How it runs

- **On this computer:** Teams socket mode. The bot dials out to Microsoft, so nothing on this
  computer is reachable from the internet. Socket mode is marked experimental in the SDK.
- **On a server:** `--http-port N` serves Teams' HTTP endpoint instead; point the bot's messaging
  endpoint at it over HTTPS.

```sh
python -m xperfect_teams --runtime http://127.0.0.1:8766 --binding <channel id> \
  --state-dir <xPerfect state folder> --credentials <private file with CLIENT_ID, CLIENT_SECRET, TENANT_ID>
```

Every reply comes from xPerfect's outbox. A send that Teams refuses is reported as failed; a send
whose outcome is unknown is not reported, and xPerfect marks it not confirmed after two minutes.
It is never resent by itself.

## Tests

```sh
uv run --extra test pytest -q
```

The tests use recorded-shape activities and a fake xPerfect runtime; no Microsoft service is called.
