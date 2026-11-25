"""Channels that bring people to an Expert, such as Microsoft Teams.

The core keeps only generic parts: which Expert a channel serves, who may write, each
incoming message once, and an outbox whose every message is saved before it is sent. A
channel package beside the core (for example ``integrations/teams``) speaks the channel's
protocol and talks to the core only through these routes. Nothing here interprets words.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import Field

from . import teams_admin
from .coordinator import CoordinatorScopeError, StrictModel, canonical, digest
from .experts import ExpertError, ExpertService, reply_text

CHANNELS_SCHEMA_VERSION = 1
CHANNEL_KINDS = ("teams", "slack")
CHANNEL_LABELS = {"teams": "Teams", "slack": "Slack"}
SENDING_TIMEOUT = timedelta(minutes=2)


class ChannelPerson(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=200)
    tenant: str = Field(default="", max_length=256)
    email: str = Field(default="", max_length=320)


class InboundMessage(StrictModel):
    activity_id: str = Field(min_length=1, max_length=512)
    person: ChannelPerson
    thread_ref: dict[str, Any] = Field(default_factory=dict)
    thread_key: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=100000)
    sent_at: str = Field(default="", max_length=64)


class DeliveryResult(StrictModel):
    state: Literal["sent", "failed"]
    platform_message_id: str = Field(default="", max_length=512)
    error: str = Field(default="", max_length=2000)


class PackageHeartbeat(StrictModel):
    # "connecting" until Microsoft's socket has held once, then "ready"; "refused" with the refusal when the package
    # gives up on Microsoft's final refusal; empty from a package that predates it.
    socket: Literal["", "connecting", "ready", "refused"] = ""
    error: str = Field(default="", max_length=400)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _listed(binding: dict, person: dict) -> bool:
    """The one allow-list rule: a person by platform id, in the listed tenant when one is given."""
    allowed = {(p["id"], p.get("tenant", "")) for p in json.loads(binding["people_json"] or "[]")}
    return (str(person.get("id") or ""), str(person.get("tenant") or "")) in allowed or (str(person.get("id") or ""), "") in allowed


_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_bindings (
 binding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, lineage_id TEXT NOT NULL,
 kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'setup' CHECK(state IN ('setup','connected','off')),
 people_json TEXT NOT NULL DEFAULT '[]', settings_json TEXT NOT NULL DEFAULT '{}',
 package_seen_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 package_socket TEXT NOT NULL DEFAULT '', package_refusal TEXT NOT NULL DEFAULT '',
 UNIQUE(tenant_id, owner_id, lineage_id, kind)
);
CREATE TABLE IF NOT EXISTS channel_inbox (
 binding_id TEXT NOT NULL, activity_id TEXT NOT NULL, state TEXT NOT NULL,
 conversation_id TEXT NOT NULL DEFAULT '', turn_id TEXT NOT NULL DEFAULT '', person_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, message_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(binding_id, activity_id)
);
CREATE TABLE IF NOT EXISTS channel_deliveries (
 delivery_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, thread_ref_json TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL CHECK(state IN ('held','pending','sending','sent','not_confirmed','failed','dropped')),
 review_id TEXT NOT NULL DEFAULT '', platform_message_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 claimed_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(conversation_id, turn_id, kind)
);
CREATE TABLE IF NOT EXISTS channel_unknown_people (
 binding_id TEXT NOT NULL, person_id TEXT NOT NULL, person_json TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 PRIMARY KEY(binding_id, person_id)
);
"""


CHANNEL_TABLES = frozenset({"channel_bindings", "channel_inbox", "channel_deliveries", "channel_unknown_people"})


def ensure_channels_schema(conn: Any) -> None:
    """The one owner of the channel tables (the service and continuity review both use it)."""
    from .schema_version import (begin_schema_migration, execute_schema_script,
                                 record_schema_version, require_compatible_schema)
    begin_schema_migration(conn)
    require_compatible_schema(conn, component="channels", target_version=CHANNELS_SCHEMA_VERSION)
    execute_schema_script(conn, _SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(channel_inbox)")}
    if "message_json" not in columns:  # added in place, as the other owners do
        conn.execute("ALTER TABLE channel_inbox ADD COLUMN message_json TEXT NOT NULL DEFAULT '{}'")
    binding_columns = {row[1] for row in conn.execute("PRAGMA table_info(channel_bindings)")}
    for column in ("package_socket", "package_refusal"):
        if column not in binding_columns:
            conn.execute(f"ALTER TABLE channel_bindings ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    record_schema_version(conn, component="channels", version=CHANNELS_SCHEMA_VERSION)


# How long a package's claim may wait for a reply to become ready: it returns as soon as one is, and a runtime
# stop waits at most this long for a claim in flight (measured: 4.7 s against 0.4 s with a 5 s wait, so 2 s).
CLAIM_WAIT_MAX_S = 2.0
# A waiting claim holds one of the server's worker threads, so only this many wait at once; any more answer at once,
# as before (their package then looks again a second later).
CLAIM_WAITERS_MAX = 8


class ChannelService:
    def __init__(self, store: Any, experts: ExpertService) -> None:
        self.store, self.experts = store, experts
        self._ready = threading.Condition()  # told when a message becomes ready to send
        self._readiness: dict[str, int] = {}  # per channel: how often a message became ready there
        self._waiters = threading.BoundedSemaphore(CLAIM_WAITERS_MAX)
        with store._connect() as conn:
            ensure_channels_schema(conn)
        experts.deliveries = self
        from .channels_setup import TeamsSetup
        self.setup = TeamsSetup(self)

    # ---------------------------------------------------------------- bindings (owner)
    def _binding(self, binding_id: str, tenant: str | None = None, owner: str | None = None) -> dict:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM channel_bindings WHERE binding_id=?", (binding_id,)).fetchone()
        if not row or (tenant is not None and (row["tenant_id"] != tenant or row["owner_id"] != owner)):
            raise ExpertError("Channel not found", code="channel_not_found", status=404)
        return dict(row)

    def view(self, binding: dict) -> dict:
        settings = json.loads(binding["settings_json"] or "{}")
        with self.store._connect() as conn:
            unknown = [json.loads(row["person_json"]) | {"last_seen_at": row["last_seen_at"]} for row in conn.execute(
                "SELECT person_json,last_seen_at FROM channel_unknown_people WHERE binding_id=? ORDER BY last_seen_at DESC LIMIT 20",
                (binding["binding_id"],))]
            unconfirmed = conn.execute("SELECT COUNT(*) FROM channel_deliveries WHERE binding_id=? AND state='not_confirmed'",
                                       (binding["binding_id"],)).fetchone()[0]
            # Online is not ready: a channel is ready once a first message from the account that connected it has
            # come through and been accepted (an unlisted sender's message is not that; an app made before this was
            # recorded counts any accepted message).
            connected_by = str(settings.get("connected_by") or "")
            heard = conn.execute("SELECT 1 FROM channel_inbox WHERE binding_id=? AND state='accepted' "
                                 "AND (?='' OR json_extract(person_json,'$.id')=?) LIMIT 1",
                                 (binding["binding_id"], connected_by, connected_by)).fetchone() is not None
        seen = binding["package_seen_at"]
        recent = bool(seen) and datetime.fromisoformat(seen) > datetime.now(UTC) - timedelta(seconds=45)
        # Online once Microsoft's socket has held. A package from before it reported the socket counts as before.
        socket = binding.get("package_socket", "")
        online = recent and socket in ("", "ready")
        return {"binding_id": binding["binding_id"], "lineage_id": binding["lineage_id"], "kind": binding["kind"],
                "label": CHANNEL_LABELS.get(binding["kind"], binding["kind"]), "state": binding["state"],
                "online": online, "connecting": recent and socket == "connecting", "first_message": heard,
                # Microsoft's last final refusal, kept until the socket holds again, so it is said once, not per try.
                "refusal": binding.get("package_refusal", ""), "people": json.loads(binding["people_json"] or "[]"),
                "app": {key: settings[key] for key in ("app_id", "app_name", "account", "tenant_id", "mode", "install_link",
                                                       "developer", "website_url", "privacy_url", "terms_url", "scopes",
                                                       "added_by_admin", "catalogue", "catalogue_id",
                                                       "picture_digest", "picture_error")
                        if key in settings},
                # Whether an administrator's one press (Microsoft's sign-in, then the organisation's catalogue) is
                # offered here: only once the owner has chosen the Microsoft sign-in it uses.
                "admin_route": bool(teams_admin.client_id()),
                "people_waiting": unknown, "unconfirmed_messages": unconfirmed,
                # While the disk guard holds runs, this Expert cannot answer; messages wait.
                "low_disk": self.experts.low_disk() is not None,
                "created_at": binding["created_at"], "updated_at": binding["updated_at"]}

    def list(self, tenant: str, owner: str, expert_id: str) -> list[dict]:
        template = self.experts.resolve(tenant, owner, expert_id)
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM channel_bindings WHERE tenant_id=? AND owner_id=? AND lineage_id=? "
                                "ORDER BY created_at", (tenant, owner, template["lineage_id"])).fetchall()
        return [self.view(dict(row)) for row in rows]

    def add(self, tenant: str, owner: str, expert_id: str, kind: str) -> dict:
        if kind not in CHANNEL_KINDS:
            raise ExpertError("Choose a supported channel", status=422)
        template = self.experts.resolve(tenant, owner, expert_id)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM channel_bindings WHERE tenant_id=? AND owner_id=? AND lineage_id=? AND kind=?",
                               (tenant, owner, template["lineage_id"], kind)).fetchone()
            if row is None:
                binding_id = f"chb_{uuid.uuid4().hex}"
                conn.execute("INSERT INTO channel_bindings(binding_id,tenant_id,owner_id,lineage_id,kind,created_at,updated_at) "
                             "VALUES(?,?,?,?,?,?,?)", (binding_id, tenant, owner, template["lineage_id"], kind, _now(), _now()))
                row = conn.execute("SELECT * FROM channel_bindings WHERE binding_id=?", (binding_id,)).fetchone()
        return self.view(dict(row))

    def update(self, tenant: str, owner: str, binding_id: str, *, people: list[dict] | None = None,
               state: str | None = None) -> dict:
        binding = self._binding(binding_id, tenant, owner)
        updates: dict[str, Any] = {}
        if people is not None:
            clean = [ChannelPerson.model_validate(person).model_dump() for person in people]
            if len({person["id"] for person in clean}) != len(clean):
                raise ExpertError("Each person may be listed once", status=422)
            updates["people_json"] = canonical(clean)
        if state is not None:
            if state not in {"connected", "off"}:
                raise ExpertError("Use connected or off", status=422)
            settings = json.loads(binding["settings_json"] or "{}")
            if state == "connected" and not settings.get("app_id"):
                raise ExpertError("Connect the channel first", code="channel_not_set_up")
            updates["state"] = state
        if updates:
            updates["updated_at"] = _now()
            assignments = ",".join(f"{key}=?" for key in updates)
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"UPDATE channel_bindings SET {assignments} WHERE binding_id=?", (*updates.values(), binding_id))
                changed = dict(conn.execute("SELECT * FROM channel_bindings WHERE binding_id=?", (binding_id,)).fetchone())
                speakers = [dict(row) for row in conn.execute(
                    "SELECT conversation_id,sender_json FROM expert_conversations WHERE binding_id=? AND role='speaker'",
                    (binding_id,))]
                # The change and everything it closes are one transaction, so no crash leaves part of it open.
                # Off means off: nothing more is sent, its jobs stop and its requests are withdrawn. Nothing
                # more goes to a removed person either. Accepted work stays visible to the owner.
                if updates.get("state") == "off":
                    closing = [row["conversation_id"] for row in speakers]
                    note, error = "The channel was turned off.", "The channel was turned off"
                else:
                    closing = [row["conversation_id"] for row in speakers
                               if not _listed(changed, json.loads(row["sender_json"] or "{}"))]
                    note, error = "The person was removed from this channel.", "The person is no longer allowed"
                stopped = self.experts._close_work_in(conn, closing, note, error)
            if updates.get("state") == "off":
                self.setup.stop(binding_id)
            for job in stopped:  # maintenance re-drives a fence that does not land here
                self.experts._fence_job(job)
        return self.view(self._binding(binding_id))

    def record_setup(self, tenant: str, owner: str, binding_id: str, settings: dict) -> dict:
        """The setup helper stores public app facts only; credentials stay in the private state folder."""
        self._binding(binding_id, tenant, owner)
        allowed = {key: str(value)[:2000] for key, value in settings.items()
                   if key in {"app_id", "app_name", "account", "connected_by", "tenant_id", "mode", "install_link",
                              "teams_app_id", "developer", "website_url", "privacy_url", "terms_url", "scopes",
                              "added_by_admin", "catalogue", "catalogue_id", "picture_digest", "picture_error"}}
        with self.store._connect() as conn:
            conn.execute("UPDATE channel_bindings SET settings_json=?,updated_at=? WHERE binding_id=?",
                         (canonical(allowed), _now(), binding_id))
        return self.view(self._binding(binding_id))

    def mark_added_by_admin(self, tenant: str, owner: str, binding_id: str, added: bool) -> dict:
        """The owner says an administrator added the app for them (published or deployed in the admin center): the
        card then stops offering the developer copy's install link, which stays blocked by the person's own policy
        (the tool reports no catalogue state, so this cannot be read)."""
        binding = self._binding(binding_id, tenant, owner)
        settings = {key: value for key, value in json.loads(binding["settings_json"] or "{}").items() if key != "added_by_admin"}
        return self.record_setup(tenant, owner, binding_id, {**settings, **({"added_by_admin": "yes"} if added else {})})

    # ---------------------------------------------------------------- package routes
    def package_config(self, binding_id: str) -> dict:
        binding = self._binding(binding_id)
        expert = self.experts.latest(binding["tenant_id"], binding["owner_id"], binding["lineage_id"])
        return {**self.view(binding), "expert_name": expert["name"],
                "settings": json.loads(binding["settings_json"] or "{}")}

    def heartbeat(self, binding_id: str, socket: str = "", error: str = "") -> dict:
        self._binding(binding_id)
        with self.store._connect() as conn:
            # A refusal stays until the socket holds; the tries in between ("connecting") leave it as it is.
            conn.execute("UPDATE channel_bindings SET package_seen_at=?, package_socket=?, package_refusal=CASE ? "
                         "WHEN 'ready' THEN '' WHEN 'refused' THEN ? ELSE package_refusal END WHERE binding_id=?",
                         (_now(), socket, socket, error[:400] or "Microsoft refused the connection.", binding_id))
        return {"ok": True}

    def inbound(self, binding_id: str, message: InboundMessage) -> dict:
        binding = self._binding(binding_id)
        tenant, owner = binding["tenant_id"], binding["owner_id"]
        person = message.person.model_dump()
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            seen = conn.execute("SELECT * FROM channel_inbox WHERE binding_id=? AND activity_id=?",
                                (binding_id, message.activity_id)).fetchone()
            if seen:
                # A message the channel delivers twice makes one turn. If xPerfect stopped after
                # accepting it but before its turn was saved, this delivery finishes that turn.
                saved = bool(seen["conversation_id"]) and conn.execute(
                    "SELECT 1 FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                    (seen["conversation_id"], seen["turn_id"])).fetchone()
                if seen["state"] != "accepted" or saved:
                    return {"accepted": seen["state"] == "accepted", "duplicate": True,
                            "conversation_id": seen["conversation_id"], "turn_id": seen["turn_id"]}
                turn_id, duplicate = seen["turn_id"], True
            else:
                if not (binding["state"] == "connected" and _listed(binding, person)):
                    conn.execute("INSERT INTO channel_inbox(binding_id,activity_id,state,person_json,created_at) VALUES(?,?,?,?,?)",
                                 (binding_id, message.activity_id, "refused", canonical(person), _now()))
                    conn.execute("INSERT INTO channel_unknown_people(binding_id,person_id,person_json,last_seen_at) VALUES(?,?,?,?) "
                                 "ON CONFLICT(binding_id,person_id) DO UPDATE SET person_json=excluded.person_json,"
                                 "last_seen_at=excluded.last_seen_at", (binding_id, person["id"], canonical(person), _now()))
                    return {"accepted": False, "reason": "not_listed" if binding["state"] == "connected" else "channel_off"}
                turn_id, duplicate = "ch-" + digest([binding_id, message.activity_id])[:40], False
                conn.execute("INSERT INTO channel_inbox(binding_id,activity_id,state,turn_id,person_json,created_at,message_json) "
                             "VALUES(?,?,?,?,?,?,?)", (binding_id, message.activity_id, "accepted", turn_id, canonical(person),
                                                       _now(), canonical(message.model_dump())))
        return self._accept_turn(binding, person, message, turn_id, duplicate=duplicate)

    def _accept_turn(self, binding: dict, person: dict, message: InboundMessage, turn_id: str, *,
                     duplicate: bool = False) -> dict:
        """The accepted message's turn in that person's conversation; safe to repeat."""
        binding_id, tenant, owner = binding["binding_id"], binding["tenant_id"], binding["owner_id"]
        speaker = self.experts.queen(tenant, owner, binding["lineage_id"], channel=CHANNEL_LABELS[binding["kind"]],
                                     thread_key=message.thread_key, sender=person, binding_id=binding_id,
                                     thread_ref=message.thread_ref)
        cid = speaker["conversation_id"]
        with self.store._connect() as conn:
            conn.execute("UPDATE channel_inbox SET conversation_id=? WHERE binding_id=? AND activity_id=?",
                         (cid, binding_id, message.activity_id))
        wrapped = self.experts.wrap(message.text, channel=CHANNEL_LABELS[binding["kind"]], sender=person,
                                    sent_at=message.sent_at)
        self.experts.coordinator.accept_turn(tenant, owner, cid, turn_id, wrapped)
        try:
            self.experts.coordinator.start_turn(tenant, owner, cid, turn_id)
        except Exception:  # The turn is saved; the coordinator's maintenance starts it.
            pass
        return {"accepted": True, "duplicate": duplicate, "conversation_id": cid, "turn_id": turn_id}

    def repair_inbox(self, limit: int = 50) -> int:
        """Finish messages accepted before an interruption whose turn was never saved."""
        settled = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        # Taken in turn, so messages that cannot be repaired for good never hide later ones.
        rows = self.experts._rotating(
            "inbox", "position", "SELECT i.rowid AS position,i.* FROM channel_inbox i WHERE i.state='accepted' "
            "AND i.created_at<? AND i.message_json<>'{}' AND NOT EXISTS (SELECT 1 FROM coordinator_turns t "
            "WHERE t.conversation_id=i.conversation_id AND t.turn_id=i.turn_id) AND i.rowid>? ORDER BY i.rowid LIMIT ?",
            (settled,), limit, start=0)
        repaired = 0
        for row in rows:
            try:
                message = InboundMessage.model_validate_json(row["message_json"])
                self._accept_turn(self._binding(row["binding_id"]), message.person.model_dump(), message, row["turn_id"])
                repaired += 1
            except Exception:  # retried on the next pass
                continue
        return repaired

    def _ready_now(self, binding_id: str) -> None:
        """A message became ready to send on this channel: a claim waiting there claims at once (its own transaction
        waits for the writer's to commit, so it reads the message)."""
        with self._ready:
            self._readiness[binding_id] = self._readiness.get(binding_id, 0) + 1
            self._ready.notify_all()

    def _pending_exists(self, binding_id: str) -> bool:
        """A read only, so a claim that waits takes no write lock while nothing is ready."""
        with self.store._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM channel_deliveries d LEFT JOIN coordinator_conversations c ON c.conversation_id=d.conversation_id "
                "WHERE d.binding_id=? AND d.state='pending' AND COALESCE(c.restore_hold,0)=0 LIMIT 1",
                (binding_id,)).fetchone() is not None

    def claim(self, binding_id: str, limit: int = 10, wait: float = 0.0) -> list[dict]:
        """The channel's ready messages, leased to the package to send. With wait, an empty claim waits up to that
        long (at most CLAIM_WAIT_MAX_S) and returns as soon as a message is ready; what a claim does is unchanged."""
        wait = max(0.0, min(float(wait or 0.0), CLAIM_WAIT_MAX_S))
        if not wait or not self._waiters.acquire(blocking=False):
            return self._claim_now(binding_id, limit)
        try:
            deadline = time.monotonic() + wait
            with self._ready:
                seen = self._readiness.get(binding_id, 0)
            claimed = self._claim_now(binding_id, limit)
            while not claimed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return claimed
                with self._ready:
                    if self._readiness.get(binding_id, 0) == seen:  # nothing ready here meanwhile: wait to be told
                        self._ready.wait(timeout=min(remaining, 0.25))
                    now = self._readiness.get(binding_id, 0)
                    told, seen = now != seen, now
                # Told: claim at once. Not told: claim only if a read finds a message ready (a path that did not tell).
                if told or self._pending_exists(binding_id):
                    claimed = self._claim_now(binding_id, limit)
            return claimed
        finally:
            self._waiters.release()

    def _claim_now(self, binding_id: str, limit: int) -> list[dict]:
        self._binding(binding_id)
        claimed = []
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Read in the transaction that claims: a channel turned off or a person removed a moment ago wins.
            binding = dict(conn.execute("SELECT * FROM channel_bindings WHERE binding_id=?", (binding_id,)).fetchone())
            if binding["state"] != "connected":
                return []
            rows = conn.execute("SELECT d.*,e.sender_json,c.stop_scope AS stop_scope,c.restore_hold AS conversation_hold "
                                "FROM channel_deliveries d LEFT JOIN expert_conversations e "
                                "ON e.conversation_id=d.conversation_id LEFT JOIN coordinator_conversations c "
                                "ON c.conversation_id=d.conversation_id WHERE d.binding_id=? AND d.state='pending' "
                                # A restored copy's messages wait for the owner's resume without using up the claim.
                                "AND COALESCE(c.restore_hold,0)=0 "
                                "ORDER BY d.created_at LIMIT ?", (binding_id, max(1, min(limit, 50)))).fetchall()
            for row in rows:
                if not _listed(binding, json.loads(row["sender_json"] or "{}")):
                    conn.execute("UPDATE channel_deliveries SET state='dropped',error='The person is no longer allowed',"
                                 "finished_at=? WHERE delivery_id=? AND state='pending'", (_now(), row["delivery_id"]))
                    continue
                if row["stop_scope"]:
                    # The last fence before a message leaves: nothing leaves a stopped conversation.
                    conn.execute("UPDATE channel_deliveries SET state='dropped',error='The conversation was stopped',"
                                 "finished_at=? WHERE delivery_id=? AND state='pending'", (_now(), row["delivery_id"]))
                    continue
                if row["conversation_hold"]:
                    continue  # a restored copy sends nothing before the owner resumes it
                if row["kind"] == "checking" and self._request_decided(conn, row["turn_id"]):
                    # A checking line leaves only while its request still waits: after the owner's decision
                    # the person gets the answer or the "no", never "I'm checking" after it.
                    conn.execute("UPDATE channel_deliveries SET state='dropped',error='Its request was already decided',"
                                 "finished_at=? WHERE delivery_id=? AND state='pending'", (_now(), row["delivery_id"]))
                    continue
                conn.execute("UPDATE channel_deliveries SET state='sending',claimed_at=? WHERE delivery_id=? AND state='pending'",
                             (_now(), row["delivery_id"]))
                claimed.append({"delivery_id": row["delivery_id"], "text": row["text"], "kind": row["kind"],
                                "thread_ref": json.loads(row["thread_ref_json"] or "{}")})
        return claimed

    def delivered(self, binding_id: str, delivery_id: str, result: DeliveryResult) -> dict:
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM channel_deliveries WHERE delivery_id=? AND binding_id=?",
                               (delivery_id, binding_id)).fetchone()
            if not row:
                raise ExpertError("Message not found", code="channel_delivery_not_found", status=404)
            if row["state"] in {"sending", "not_confirmed"}:
                conn.execute("UPDATE channel_deliveries SET state=?,platform_message_id=?,error=?,finished_at=? WHERE delivery_id=?",
                             (result.state, result.platform_message_id, result.error, _now(), delivery_id))
        return {"delivery_id": delivery_id, "state": result.state}

    # ---------------------------------------------------------------- Ask me first holds
    def _speaker_for(self, conversation_id: str) -> dict | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM expert_conversations WHERE conversation_id=? AND role='speaker'",
                               (conversation_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _request_decided(conn, checking_turn_id: str) -> bool:
        """Whether the request a checking line announces (its turn is "checking-<request id>") was decided."""
        row = conn.execute("SELECT state FROM expert_reviews WHERE review_id=?",
                           (checking_turn_id.removeprefix("checking-"),)).fetchone()
        return row is not None and row["state"] != "pending"

    def _closed_note(self, conn, speaker: dict) -> str:
        """Why nothing may leave for this person for good, read in the caller's transaction; "" when it may."""
        fence = conn.execute("SELECT stop_scope FROM coordinator_conversations WHERE conversation_id=?",
                             (speaker["conversation_id"],)).fetchone()
        if fence is None or fence["stop_scope"]:
            return "The conversation was stopped"
        binding = conn.execute("SELECT state,people_json FROM channel_bindings WHERE binding_id=?",
                               (speaker["binding_id"],)).fetchone()
        if binding is None or binding["state"] != "connected":
            return "The channel was turned off"
        if not _listed(dict(binding), json.loads(speaker.get("sender_json") or "{}")):
            return "The person is no longer allowed"
        return ""

    def _insert_delivery(self, conn, speaker: dict, turn_id: str, kind: str, text: str, state: str, review_id: str = "",
                         *, error: str = "") -> None:
        if state in {"pending", "held"}:
            # Checked where the message is written: none is created to leave a stopped conversation, a
            # channel turned off or a person removed.
            error = self._closed_note(conn, speaker) or error
            if error:
                state = "dropped"
        conn.execute(
            "INSERT OR IGNORE INTO channel_deliveries(delivery_id,binding_id,conversation_id,turn_id,kind,text,thread_ref_json,"
            "state,review_id,error,finished_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dlv_" + digest([speaker["conversation_id"], turn_id, kind])[:32], speaker["binding_id"],
             speaker["conversation_id"], turn_id, kind, text, speaker["thread_ref_json"] or "{}", state, review_id, error,
             _now() if state == "dropped" else "", _now()))
        if state == "pending":
            self._ready_now(speaker["binding_id"])

    def write_released_reply(self, conn, speaker: dict, review: dict) -> None:
        """Inside the one release's transaction, after its checks: exactly the reply that was shown, word for
        word, leaves its hold (Always), or is written to be sent (Auto, which held no copy)."""
        held = conn.execute("SELECT delivery_id FROM channel_deliveries WHERE review_id=? AND state='held' "
                            "ORDER BY created_at LIMIT 1", (review["review_id"],)).fetchone()
        if held:
            conn.execute("UPDATE channel_deliveries SET state='pending' WHERE delivery_id=?", (held["delivery_id"],))
            self._ready_now(speaker["binding_id"])
        else:
            self._insert_delivery(conn, speaker, "approved-" + review["review_id"], "approved",
                                  json.loads(review["payload_json"]).get("text", ""), "pending", review["review_id"])

    def discard_held(self, conn, review_id: str, reason: str) -> None:
        """Inside the caller's transaction: the reply held for this request never leaves its hold."""
        conn.execute("UPDATE channel_deliveries SET state='dropped',error=?,finished_at=? "
                     "WHERE review_id=? AND state='held'", (reason, _now(), review_id))

    def _turn_reviews(self, conversation_id: str, turn_id: str) -> list[dict]:
        """Every request a turn made of the owner, whatever its state: pending, decided or withdrawn."""
        with self.store._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM expert_reviews WHERE speaker_conversation_id=? AND source_turn_id=? ORDER BY created_at",
                (conversation_id, turn_id))]

    # ---------------------------------------------------------------- maintenance
    def reconcile_once(self, limit: int = 200) -> list[dict]:
        """Turn each finished Queen reply into exactly one outgoing message, or a hold."""
        outcomes: list[dict] = []
        self.repair_inbox()
        with self.store._connect() as conn:
            stale = (datetime.now(UTC) - SENDING_TIMEOUT).isoformat()
            # A send whose outcome never came back is not resent by itself; the owner sees it.
            conn.execute("UPDATE channel_deliveries SET state='not_confirmed',error='The channel did not confirm this message' "
                         "WHERE state='sending' AND claimed_at<>'' AND claimed_at<?", (stale,))
            rows = [dict(row) for row in conn.execute(
                "SELECT t.conversation_id,t.turn_id,t.origin,t.request_id,t.response_json,e.binding_id,e.lineage_id,e.tenant_id,e.owner_id "
                "FROM coordinator_turns t JOIN expert_conversations e ON e.conversation_id=t.conversation_id "
                "JOIN coordinator_conversations c ON c.conversation_id=t.conversation_id "
                # A restored turn waits for the owner's resume; it never becomes a message by itself.
                "WHERE e.role='speaker' AND e.binding_id<>'' AND t.response_json<>'' AND t.restore_hold=0 "
                # A stopped conversation creates nothing new to send.
                "AND c.restore_hold=0 AND c.stop_scope='' AND NOT EXISTS ("
                "SELECT 1 FROM channel_deliveries d WHERE d.conversation_id=t.conversation_id AND d.turn_id=t.turn_id) "
                "ORDER BY t.created_at LIMIT ?", (limit,))]
        for row in rows:
            speaker = self._speaker_for(row["conversation_id"])
            if speaker is None:
                continue
            text = reply_text(row["response_json"])
            origin = row["origin"]
            kind = {"owner_decision": "decision", "owner_checking": "checking"}.get(origin, "reply")
            with self.store._connect() as conn:
                closed = self._closed_note(conn, speaker)
                if closed or not text:
                    # Closed for good since its turn began (the channel off, the person removed): nothing of the
                    # turn leaves and nothing is asked for it.
                    self._insert_delivery(conn, speaker, row["turn_id"], kind, text, "dropped", error=closed)
                    if closed:
                        outcomes.append({"turn_id": row["turn_id"], "delivery": "dropped", "error": closed})
                    continue
                if kind != "reply":
                    decided = kind == "checking" and self._request_decided(conn, row["turn_id"])
                    self._insert_delivery(conn, speaker, row["turn_id"], kind, text, "dropped" if decided else "pending",
                                          error="Its request was already decided" if decided else "")
                    continue
            asked = self._turn_reviews(row["conversation_id"], row["turn_id"])
            if asked:
                # In any setting, a turn that asked the owner never sends its own reply, also when the owner
                # decided before this pass: an approval releases exactly what was shown, a no is told in a
                # turn of its own. While a request still waits, her checking line goes out instead, written
                # in a turn with every tool off.
                with self.store._connect() as conn:
                    self._insert_delivery(conn, speaker, row["turn_id"], "reply", text, "dropped")
                pending_reviews = [review for review in asked if review["state"] == "pending"]
                if pending_reviews:
                    self.experts.tell_checking(speaker, pending_reviews[0])
                    outcomes.append({"turn_id": row["turn_id"], "delivery": "held",
                                     "review_id": pending_reviews[0]["review_id"]})
                else:
                    outcomes.append({"turn_id": row["turn_id"], "delivery": "dropped"})
                continue
            # A turn whose hand-off already told the person (its words left when the job started) sends nothing
            # more: the words spoke for it. Read and written in one write transaction with the job's start.
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                spoke = conn.execute(
                    "SELECT 1 FROM expert_jobs j JOIN channel_deliveries d ON d.conversation_id=j.speaker_conversation_id "
                    "AND d.turn_id='handoff-' || j.job_id AND d.kind='handoff' "
                    "WHERE j.speaker_conversation_id=? AND j.source_turn_id=? LIMIT 1",
                    (row["conversation_id"], row["turn_id"])).fetchone() is not None
                if spoke:
                    self._insert_delivery(conn, speaker, row["turn_id"], "reply", text, "dropped",
                                          error="The hand-off's words were sent instead")
            if spoke:
                outcomes.append({"turn_id": row["turn_id"], "delivery": "dropped", "error": "hand-off words sent"})
                continue
            # A reply part of which the AI provider's own safety check stopped may have lost a request to the owner on
            # the way (the stopped call never ran). On Auto it waits for the owner as on Always; Off sends it as it is.
            # Decided from the typed stop in this turn's own native run, never from its words.
            stopped = bool(row["request_id"]) and bool(self.experts.coordinator.provider.safety_stop(row["request_id"]))
            # Auto or Off sends it; Always holds it for the owner, its request and its hold written together. The
            # setting is read inside the transaction that writes the message, so a setting saved meanwhile is never
            # missed: no save can land between the read and the write.
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                closed = self._closed_note(conn, speaker)
                mode = self.experts.spec(self.experts.latest(row["tenant_id"], row["owner_id"], row["lineage_id"])).ask_me_first
                review = None
                if closed:
                    self._insert_delivery(conn, speaker, row["turn_id"], "reply", text, "dropped", error=closed)
                elif mode == "off" or (mode == "auto" and not stopped):
                    self._insert_delivery(conn, speaker, row["turn_id"], "reply", text, "pending")
                else:
                    review = self.experts._insert_review(
                        conn, speaker["tenant_id"], speaker["owner_id"], speaker, kind="reply", target_id=row["turn_id"],
                        reason=("Ask me first is set to Always ask." if mode == "always" else
                                "The AI provider's safety check stopped part of this reply, so it waits for you."),
                        payload={"text": text}, source_turn_id=row["turn_id"], idempotency_key="reply:" + row["turn_id"])
                    self._insert_delivery(conn, speaker, row["turn_id"], "reply", text, "held", review["review_id"])
            if review is None and not closed:
                outcomes.append({"turn_id": row["turn_id"], "delivery": "pending"})
                continue
            if review is None:
                outcomes.append({"turn_id": row["turn_id"], "delivery": "dropped", "error": closed})
                continue
            self.experts.tell_checking(speaker, review)
            outcomes.append({"turn_id": row["turn_id"], "delivery": "held", "review_id": review["review_id"]})
        outcomes.extend(self.setup.reconcile_once())
        return outcomes
