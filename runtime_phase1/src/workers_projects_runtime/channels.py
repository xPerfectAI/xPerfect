"""Channels that bring people to an Expert, such as Microsoft Teams.

The core keeps only generic parts: which Expert a channel serves, who may write, each
incoming message once, and an outbox whose every message is saved before it is sent. A
channel package beside the core (for example ``integrations/teams``) speaks the channel's
protocol and talks to the core only through these routes. Nothing here interprets words.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import Field

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
    record_schema_version(conn, component="channels", version=CHANNELS_SCHEMA_VERSION)


class ChannelService:
    def __init__(self, store: Any, experts: ExpertService) -> None:
        self.store, self.experts = store, experts
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
        seen = binding["package_seen_at"]
        online = bool(seen) and datetime.fromisoformat(seen) > datetime.now(UTC) - timedelta(seconds=45)
        return {"binding_id": binding["binding_id"], "lineage_id": binding["lineage_id"], "kind": binding["kind"],
                "label": CHANNEL_LABELS.get(binding["kind"], binding["kind"]), "state": binding["state"],
                "online": online, "people": json.loads(binding["people_json"] or "[]"),
                "app": {key: settings[key] for key in ("app_id", "app_name", "tenant_id", "mode", "install_link")
                        if key in settings},
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
                conn.execute(f"UPDATE channel_bindings SET {assignments} WHERE binding_id=?", (*updates.values(), binding_id))
            if "people_json" in updates:
                changed = {**binding, "people_json": updates["people_json"]}
                with self.store._connect() as conn:
                    removed = [row["conversation_id"] for row in conn.execute(
                        "SELECT conversation_id,sender_json FROM expert_conversations WHERE binding_id=? AND role='speaker'",
                        (binding_id,)) if not _listed(changed, json.loads(row["sender_json"] or "{}"))]
                # Nothing more goes to a removed person: their requests, jobs and unsent messages close.
                self.experts.close_channel_work(removed, "The person was removed from this channel.")
                for conversation_id in removed:
                    self.drop_unsent(conversation_id, "The person is no longer allowed")
            if updates.get("state") == "off":
                self.setup.stop(binding_id)
                # Off means off: nothing more is sent, its jobs stop and its waiting requests are
                # withdrawn. Accepted work stays visible to the owner.
                with self.store._connect() as conn:
                    conn.execute("UPDATE channel_deliveries SET state='dropped',error='The channel was turned off',"
                                 "finished_at=? WHERE binding_id=? AND state IN ('pending','held')", (_now(), binding_id))
                    speakers = [row[0] for row in conn.execute(
                        "SELECT conversation_id FROM expert_conversations WHERE binding_id=?", (binding_id,))]
                self.experts.close_channel_work(speakers, "The channel was turned off.")
        return self.view(self._binding(binding_id))

    def record_setup(self, tenant: str, owner: str, binding_id: str, settings: dict) -> dict:
        """The setup helper stores public app facts only; credentials stay in the private state folder."""
        self._binding(binding_id, tenant, owner)
        allowed = {key: str(value)[:2000] for key, value in settings.items()
                   if key in {"app_id", "app_name", "tenant_id", "mode", "install_link", "teams_app_id"}}
        with self.store._connect() as conn:
            conn.execute("UPDATE channel_bindings SET settings_json=?,updated_at=? WHERE binding_id=?",
                         (canonical(allowed), _now(), binding_id))
        return self.view(self._binding(binding_id))

    # ---------------------------------------------------------------- package routes
    def package_config(self, binding_id: str) -> dict:
        binding = self._binding(binding_id)
        expert = self.experts.latest(binding["tenant_id"], binding["owner_id"], binding["lineage_id"])
        return {**self.view(binding), "expert_name": expert["name"],
                "settings": json.loads(binding["settings_json"] or "{}")}

    def heartbeat(self, binding_id: str) -> dict:
        self._binding(binding_id)
        with self.store._connect() as conn:
            conn.execute("UPDATE channel_bindings SET package_seen_at=? WHERE binding_id=?", (_now(), binding_id))
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
        with self.store._connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT i.* FROM channel_inbox i WHERE i.state='accepted' AND i.created_at<? AND i.message_json<>'{}' "
                "AND NOT EXISTS (SELECT 1 FROM coordinator_turns t WHERE t.conversation_id=i.conversation_id "
                "AND t.turn_id=i.turn_id) LIMIT ?", (settled, limit))]
        repaired = 0
        for row in rows:
            try:
                message = InboundMessage.model_validate_json(row["message_json"])
                self._accept_turn(self._binding(row["binding_id"]), message.person.model_dump(), message, row["turn_id"])
                repaired += 1
            except Exception:  # retried on the next pass
                continue
        return repaired

    def claim(self, binding_id: str, limit: int = 10) -> list[dict]:
        binding = self._binding(binding_id)
        if binding["state"] != "connected":
            return []
        claimed = []
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT d.*,e.sender_json,c.stop_scope AS stop_scope,c.restore_hold AS conversation_hold "
                                "FROM channel_deliveries d LEFT JOIN expert_conversations e "
                                "ON e.conversation_id=d.conversation_id LEFT JOIN coordinator_conversations c "
                                "ON c.conversation_id=d.conversation_id WHERE d.binding_id=? AND d.state='pending' "
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

    def _insert_delivery(self, conn, speaker: dict, turn_id: str, kind: str, text: str, state: str, review_id: str = "") -> None:
        conn.execute(
            "INSERT OR IGNORE INTO channel_deliveries(delivery_id,binding_id,conversation_id,turn_id,kind,text,thread_ref_json,"
            "state,review_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("dlv_" + digest([speaker["conversation_id"], turn_id, kind])[:32], speaker["binding_id"],
             speaker["conversation_id"], turn_id, kind, text, speaker["thread_ref_json"] or "{}", state, review_id, _now()))

    def release_approved_reply(self, review: dict) -> None:
        """Approve sends exactly the reply that was shown, word for word."""
        speaker = self._speaker_for(review["speaker_conversation_id"])
        if not speaker or not speaker["binding_id"]:
            return
        text = json.loads(review["payload_json"]).get("text", "")
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fence = conn.execute("SELECT stop_scope,restore_hold FROM coordinator_conversations WHERE conversation_id=?",
                                 (review["speaker_conversation_id"],)).fetchone()
            if fence is None or fence["stop_scope"] or fence["restore_hold"]:
                # A stopped conversation sends nothing; a restored copy waits for the owner's resume.
                return
            existing = conn.execute("SELECT delivery_id,state FROM channel_deliveries WHERE review_id=? "
                                    "ORDER BY created_at LIMIT 1", (review["review_id"],)).fetchone()
            if existing and existing["state"] == "held":
                conn.execute("UPDATE channel_deliveries SET state='pending' WHERE delivery_id=?", (existing["delivery_id"],))
            elif not existing:
                # A reply the Expert asked about (Auto) has no held copy yet.
                self._insert_delivery(conn, speaker, "approved-" + review["review_id"], "approved", text, "pending",
                                      review["review_id"])
            # Any other state (dropped, sent, not confirmed) is final: approval never revives it.

    def drop_unsent(self, conversation_id: str, reason: str) -> None:
        """Nothing waiting in a stopped conversation leaves; what was already sent keeps its receipt."""
        with self.store._connect() as conn:
            conn.execute("UPDATE channel_deliveries SET state='dropped',error=?,finished_at=? "
                         "WHERE conversation_id=? AND state IN ('held','pending')", (reason, _now(), conversation_id))

    def discard_held_reply(self, review: dict) -> None:
        with self.store._connect() as conn:
            conn.execute("UPDATE channel_deliveries SET state='dropped',error='Your owner said no',finished_at=? "
                         "WHERE review_id=? AND state='held'", (_now(), review["review_id"]))

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
                "SELECT t.conversation_id,t.turn_id,t.origin,t.response_json,e.binding_id,e.lineage_id,e.tenant_id,e.owner_id "
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
            mode = self.experts.spec(self.experts.latest(row["tenant_id"], row["owner_id"], row["lineage_id"])).ask_me_first
            with self.store._connect() as conn:
                if not text:
                    self._insert_delivery(conn, speaker, row["turn_id"], "reply", "", "dropped")
                    continue
                if origin in {"owner_decision", "owner_checking"}:
                    kind = "decision" if origin == "owner_decision" else "checking"
                    self._insert_delivery(conn, speaker, row["turn_id"], kind, text, "pending")
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
            if mode != "always":
                with self.store._connect() as conn:
                    self._insert_delivery(conn, speaker, row["turn_id"], "reply", text, "pending")
                outcomes.append({"turn_id": row["turn_id"], "delivery": "pending"})
                continue
            # Always ask: a reply to another person waits for the owner.
            review = self.experts.hold_reply(speaker, row["turn_id"], text, "Ask me first is set to Always ask.")
            with self.store._connect() as conn:
                self._insert_delivery(conn, speaker, row["turn_id"], "reply", text, "held", review["review_id"])
            self.experts.tell_checking(speaker, review)
            outcomes.append({"turn_id": row["turn_id"], "delivery": "held", "review_id": review["review_id"]})
        outcomes.extend(self.setup.reconcile_once())
        return outcomes
