"""Activity lines: one line for each thing that happened to an Expert or a Worker, read for the signed-in owner.

A line is built from typed records only (ids, states, bindings): a message to an Expert, a message in Chat,
a Worker's run, or a change to one of the owner's Workers. The low-level events are its trail. The person's
words are shown to the owner only: they are cut short here and never logged. `GET /v1/activity` and the MCP
tools that read it are unchanged and carry no words.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any, Callable, Literal

from fastapi import HTTPException, Request
from pydantic import Field, field_validator

from .coordinator import _RESULT_RETRY_BLOCKERS, StrictModel
from .experts import ExpertService

Status = Literal["waiting_for_you", "working", "answered", "done", "stopped", "failed"]
PREVIEW_LIMIT = 140
NAME_LIMIT = 120
_SEP = "\x1f"
# A turn that is not settled and waits for one of these will be retried by the coordinator: it is still working.
_WAITING_BLOCKERS = ",".join(f"'{code}'" for code in sorted({"", *_RESULT_RETRY_BLOCKERS}))
# The owner's own Workers, by the rule the coordinator uses for the Workers it may reuse.
_REUSABLE = ("w.trusted_run_lane<>'conversation' AND w.workspace_kind<>'ephemeral' "
             "AND NOT EXISTS (SELECT 1 FROM delegations dl WHERE dl.worker_id=w.worker_id) "
             "AND NOT EXISTS (SELECT 1 FROM provider_sessions ps WHERE ps.worker_id=w.worker_id)")
_TERMINAL_RUN = "('completed','failed','cancelled')"


class LinesQuery(StrictModel):
    experts: list[str] = Field(default_factory=list, max_length=200)
    workers: list[str] = Field(default_factory=list, max_length=200)
    chat: bool = False
    since: str = Field(default="", max_length=64)
    until: str = Field(default="", max_length=64)
    statuses: list[Status] = Field(default_factory=list, max_length=6)
    q: str = Field(default="", max_length=200)
    cursor: str = Field(default="", max_length=2048)
    limit: int = Field(default=30, ge=1, le=100)

    @field_validator("experts", "workers")
    @classmethod
    def _ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 512 for value in values):
            raise ValueError("Each id must be 1 to 512 characters")
        return list(dict.fromkeys(values))


class LineNotFound(Exception):
    pass


def _instant(value: str) -> str:
    """A UTC ISO instant in the stored format (`datetime.isoformat`), so stored times compare as text."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, "Times must be ISO 8601 with a time zone") from exc
    if parsed.tzinfo is None:
        raise HTTPException(422, "Times must be ISO 8601 with a time zone")
    return parsed.astimezone(UTC).isoformat()


def _encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode(value: str) -> str:
    try:
        return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise LineNotFound from exc


def cut(text: str, limit: int = PREVIEW_LIMIT) -> str:
    """Spaces folded, at most `limit` characters ending on a whole word where one is near, then an ellipsis."""
    words = " ".join(str(text or "").split())
    if len(words) <= limit:
        return words
    short = words[:limit]
    space = short.rfind(" ")
    if space >= limit * 2 // 3:
        short = short[:space]
    return short.rstrip(" ,.;:") + "…"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ActivityLines:
    def __init__(self, store: Any) -> None:
        self.store = store

    # ------------------------------------------------------------------ the list
    def _sql(self, tenant: str, owner: str, query: LinesQuery, *, key: str = "") -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {"tenant": tenant, "owner": owner}

        def listed(prefix: str, values: list[str]) -> str:
            names = []
            for index, value in enumerate(values):
                params[f"{prefix}{index}"] = value
                names.append(f":{prefix}{index}")
            return ",".join(names)

        who = bool(query.experts or query.workers or query.chat)
        experts = listed("x", query.experts)
        workers = listed("w", query.workers)
        ctes = [
            # Each Expert's newest version: its name and the template that serves its picture.
            """names AS (SELECT t.lineage_id, t.template_id, t.name FROM workspace_templates t
              WHERE t.tenant_id=:tenant AND t.owner_id=:owner AND t.version=(SELECT MAX(v.version) FROM workspace_templates v
                WHERE v.tenant_id=t.tenant_id AND v.owner_id=t.owner_id AND v.lineage_id=t.lineage_id))""",
            """inbox AS (SELECT i.conversation_id, i.turn_id, i.person_json, i.message_json, b.kind AS channel_kind
              FROM channel_inbox i JOIN channel_bindings b ON b.binding_id=i.binding_id
              WHERE b.tenant_id=:tenant AND b.owner_id=:owner AND i.state='accepted' AND i.conversation_id<>'')""",
            """rev AS (SELECT speaker_conversation_id AS cid, source_turn_id AS tid, MAX(state='pending') AS pending,
              MAX(state IN ('rejected','withdrawn')) AS stopped, MIN(CASE WHEN state='pending' THEN review_id END) AS review_id
              FROM expert_reviews WHERE tenant_id=:tenant AND owner_id=:owner AND source_turn_id<>'' GROUP BY 1,2)""",
            """job AS (SELECT speaker_conversation_id AS cid, source_turn_id AS tid, MAX(state='failed') AS failed,
              MAX(state IN ('stopped','withdrawn')) AS stopped, MAX(state IN ('held','starting','running')) AS working,
              MAX(state='done') AS done
              FROM expert_jobs WHERE tenant_id=:tenant AND owner_id=:owner AND source_turn_id<>'' GROUP BY 1,2)""",
            """dlv AS (SELECT d.conversation_id AS cid, d.turn_id AS tid, MAX(d.state='failed') AS failed,
              MAX(d.state='dropped') AS dropped FROM channel_deliveries d JOIN channel_bindings b ON b.binding_id=d.binding_id
              WHERE b.tenant_id=:tenant AND b.owner_id=:owner GROUP BY 1,2)""",
        ]
        if workers:
            # A message "used" a Worker when its turn, or a job it started, gave that Worker a goal.
            ctes.append(f"""used AS (SELECT g.conversation_id AS cid, g.source_turn_id AS tid FROM coordinator_goals g
              WHERE g.worker_id IN ({workers})
              UNION SELECT j.speaker_conversation_id, j.source_turn_id FROM expert_jobs j
              JOIN coordinator_goals g ON g.conversation_id=j.job_conversation_id
              WHERE j.tenant_id=:tenant AND j.owner_id=:owner AND g.worker_id IN ({workers}))""")

        has_text = ("(i.message_json IS NOT NULL AND json_valid(i.message_json) "
                    "AND COALESCE(json_type(i.message_json,'$.text'),'')='text')")
        turn_who = []
        if experts:
            turn_who.append(f"e.lineage_id IN ({experts})")
        if query.chat:
            turn_who.append("e.conversation_id IS NULL")
        if workers:
            turn_who.append("EXISTS (SELECT 1 FROM used u WHERE u.cid=t.conversation_id AND u.tid=t.turn_id)")
        arms = []
        if not who or turn_who:
            arms.append(f"""SELECT CASE WHEN e.conversation_id IS NULL THEN 'chat' ELSE 'message' END AS kind,
                t.created_at AS at, 't' || char(31) || t.conversation_id || char(31) || t.turn_id AS line_key,
                CASE WHEN e.conversation_id IS NULL THEN 'chat' ELSE 'expert' END AS subject_type,
                COALESCE(e.lineage_id,'') AS subject_id, COALESCE(n.name,'') AS subject_name,
                COALESCE(n.template_id,'') AS template_id, '' AS ai,
                CASE WHEN COALESCE(e.binding_id,'')<>'' THEN COALESCE(i.channel_kind, b2.kind, '') ELSE 'chat' END AS channel_kind,
                CASE WHEN COALESCE(e.binding_id,'')<>'' THEN COALESCE(i.person_json, e.sender_json) ELSE '' END AS person_json,
                CASE WHEN COALESCE(e.binding_id,'')<>'' THEN 0 ELSE 1 END AS you,
                CASE WHEN {has_text} THEN json_extract(i.message_json,'$.text') ELSE t.message END AS words,
                CASE WHEN COALESCE(e.binding_id,'')<>'' AND NOT {has_text} THEN 1 ELSE 0 END AS framed,
                CASE
                  WHEN COALESCE(r.pending,0) THEN 'waiting_for_you'
                  WHEN (t.response_json='' AND t.restore_hold=0 AND t.blocker NOT IN ('cancelled',{_WAITING_BLOCKERS}))
                       OR COALESCE(j.failed,0) OR COALESCE(d.failed,0) THEN 'failed'
                  WHEN (t.response_json='' AND t.blocker='cancelled') OR COALESCE(r.stopped,0) OR COALESCE(j.stopped,0)
                       OR COALESCE(d.dropped,0) THEN 'stopped'
                  WHEN t.response_json='' OR COALESCE(j.working,0) THEN 'working'
                  WHEN COALESCE(j.done,0) THEN 'done'
                  ELSE 'answered' END AS status,
                COALESCE(r.review_id,'') AS review_id, t.conversation_id AS conversation_id, '' AS worker_id,
                '' AS event_type
              FROM coordinator_turns t
              JOIN coordinator_conversations c ON c.conversation_id=t.conversation_id
                AND c.tenant_id=:tenant AND c.owner_id=:owner
              LEFT JOIN expert_conversations e ON e.conversation_id=t.conversation_id
                AND e.tenant_id=:tenant AND e.owner_id=:owner
              LEFT JOIN names n ON n.lineage_id=e.lineage_id
              LEFT JOIN inbox i ON i.conversation_id=t.conversation_id AND i.turn_id=t.turn_id
              LEFT JOIN channel_bindings b2 ON b2.binding_id=e.binding_id AND b2.tenant_id=:tenant AND b2.owner_id=:owner
              LEFT JOIN rev r ON r.cid=t.conversation_id AND r.tid=t.turn_id
              LEFT JOIN job j ON j.cid=t.conversation_id AND j.tid=t.turn_id
              LEFT JOIN dlv d ON d.cid=t.conversation_id AND d.tid=t.turn_id
              WHERE t.origin='interactive' AND (e.conversation_id IS NULL OR e.role='speaker')
              {('AND (' + ' OR '.join(turn_who) + ')') if turn_who else ''}""")
        if not who or workers:
            arms.append(f"""SELECT 'run' AS kind, COALESCE(NULLIF(r.first_queued_at,''), r.queued_at) AS at,
                'r' || char(31) || r.run_id AS line_key, 'worker' AS subject_type, w.worker_id AS subject_id,
                w.name AS subject_name, '' AS template_id, w.profile AS ai,
                CASE WHEN r.run_id IN (SELECT queued_run_id FROM scheduled_runs WHERE tenant_id=:tenant AND owner_id=:owner
                  AND queued_run_id IS NOT NULL) THEN 'schedule' ELSE 'xperfect' END AS channel_kind,
                '' AS person_json, 1 AS you, r.instruction AS words, 0 AS framed,
                CASE WHEN r.state NOT IN {_TERMINAL_RUN} AND w.state='needs_input' THEN 'waiting_for_you'
                  WHEN r.state='failed' THEN 'failed' WHEN r.state IN ('cancelled','interrupted') THEN 'stopped'
                  WHEN r.state='completed' THEN 'done' ELSE 'working' END AS status,
                '' AS review_id, '' AS conversation_id, w.worker_id AS worker_id, '' AS event_type
              FROM runs r JOIN workers w ON w.worker_id=r.worker_id
              WHERE w.tenant_id=:tenant AND w.owner_id=:owner AND r.tenant_id=:tenant
                AND r.run_id NOT IN (SELECT run_id FROM coordinator_goals WHERE run_id<>'')
                AND r.run_id NOT IN (SELECT p.run_id FROM provider_requests p
                  JOIN provider_sessions s ON s.session_id=p.session_id
                  JOIN coordinator_conversations cc ON cc.conversation_id=s.conversation_id WHERE p.run_id IS NOT NULL)
                {f'AND w.worker_id IN ({workers})' if workers else ''}""")
            arms.append(f"""SELECT 'workspace' AS kind, ev.created_at AS at, 'e' || char(31) || ev.event_id AS line_key,
                'worker' AS subject_type, w.worker_id AS subject_id, w.name AS subject_name, '' AS template_id,
                w.profile AS ai, '' AS channel_kind, '' AS person_json, 0 AS you, '' AS words, 0 AS framed,
                '' AS status, '' AS review_id, '' AS conversation_id, w.worker_id AS worker_id, ev.event_type AS event_type
              FROM events ev JOIN workers w ON w.worker_id=ev.worker_id
              WHERE ev.tenant_id=:tenant AND w.tenant_id=:tenant AND w.owner_id=:owner
                AND COALESCE(ev.run_id,'')='' AND {_REUSABLE}
                {f'AND w.worker_id IN ({workers})' if workers else ''}""")

        where = []
        if key:
            params["key"] = key
            where.append("line_key=:key")
        since, until = _instant(query.since), _instant(query.until)
        if since:
            params["since"] = since
            where.append("at>=:since")
        if until:
            params["until"] = until
            where.append("at<:until")
        if query.statuses:
            where.append(f"status IN ({listed('s', list(query.statuses))})")
        if query.q.strip():
            params["q"] = "%" + _escape_like(query.q.strip()) + "%"
            where.append("(words LIKE :q ESCAPE '\\' OR subject_name LIKE :q ESCAPE '\\' OR "
                         "(json_valid(person_json) AND json_extract(person_json,'$.name') LIKE :q ESCAPE '\\'))")
        if query.cursor:
            try:
                at, line_key = json.loads(_decode(query.cursor))
                params["c_at"], params["c_key"] = str(at), str(line_key)
            except (LineNotFound, ValueError, TypeError) as exc:
                raise HTTPException(422, "This page of Activity is no longer available; start again") from exc
            where.append("(at<:c_at OR (at=:c_at AND line_key<:c_key))")
        params["limit"] = query.limit + 1
        if not arms:
            return "", params
        sql = ("WITH " + ",\n".join(ctes) + ",\nlines AS (" + "\nUNION ALL\n".join(arms) + ")\n"
               "SELECT * FROM lines" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY at DESC, line_key DESC LIMIT :limit")
        return sql, params

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        person: dict[str, Any] = {}
        if row["person_json"]:
            try:
                person = json.loads(row["person_json"]) or {}
            except ValueError:
                person = {}
        words = str(row["words"] or "")
        if row["framed"]:
            words = ExpertService._person_words(words)  # an old row saved before the channel's own words were kept
        item: dict[str, Any] = {
            "line_id": _encode(row["line_key"]), "kind": row["kind"], "at": row["at"], "status": row["status"],
            "subject": {"type": row["subject_type"], "id": row["subject_id"], "template_id": row["template_id"],
                        "name": row["subject_name"], "ai": row["ai"]},
            "channel": {"kind": row["channel_kind"]},
            "person": {"name": cut(str(person.get("name") or "") if isinstance(person, dict) else "", NAME_LIMIT),
                       "you": bool(row["you"])},
            "preview": cut(words) if row["kind"] != "workspace" else "",
            "open": ({"conversation_id": row["conversation_id"]} if row["conversation_id"]
                     else {"worker_id": row["worker_id"]}),
        }
        if row["review_id"]:
            item["review_id"] = row["review_id"]
        if row["event_type"]:
            item["event_type"] = row["event_type"]
        return item

    def lines(self, tenant: str, owner: str, query: LinesQuery) -> dict[str, Any]:
        sql, params = self._sql(tenant, owner, query)
        rows = []
        if sql:
            with self.store._connect() as conn:
                rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        more = len(rows) > query.limit
        rows = rows[:query.limit]
        result: dict[str, Any] = {
            "items": [self._view(row) for row in rows],
            "next_cursor": _encode(json.dumps([rows[-1]["at"], rows[-1]["line_key"]])) if more and rows else "",
        }
        if not query.cursor:
            result["subjects"] = self.subjects(tenant, owner)
        return result

    def subjects(self, tenant: str, owner: str) -> dict[str, Any]:
        """The buttons: the owner's Experts and Chat that have lines, and the owner's own Workers, newest first."""
        params = {"tenant": tenant, "owner": owner}
        with self.store._connect() as conn:
            experts = conn.execute("""
                SELECT e.lineage_id, COALESCE(n.template_id,'') AS template_id, COALESCE(n.name,'') AS name,
                       MAX(t.created_at) AS last
                FROM expert_conversations e
                JOIN coordinator_turns t ON t.conversation_id=e.conversation_id AND t.origin='interactive'
                LEFT JOIN (SELECT t1.lineage_id, t1.template_id, t1.name FROM workspace_templates t1
                  WHERE t1.tenant_id=:tenant AND t1.owner_id=:owner AND t1.version=(SELECT MAX(v.version)
                    FROM workspace_templates v WHERE v.tenant_id=t1.tenant_id AND v.owner_id=t1.owner_id
                    AND v.lineage_id=t1.lineage_id)) n ON n.lineage_id=e.lineage_id
                WHERE e.tenant_id=:tenant AND e.owner_id=:owner AND e.role='speaker'
                GROUP BY e.lineage_id ORDER BY last DESC LIMIT 200""", params).fetchall()
            workers = conn.execute(f"""
                SELECT w.worker_id, w.name, w.profile, MAX(ev.created_at) AS last
                FROM workers w JOIN events ev ON ev.worker_id=w.worker_id
                WHERE w.tenant_id=:tenant AND w.owner_id=:owner AND w.catalog_state='active'
                  AND w.state NOT IN ('terminating','termination_failed','terminated') AND {_REUSABLE}
                GROUP BY w.worker_id ORDER BY last DESC LIMIT 200""", params).fetchall()
            chat = conn.execute("""
                SELECT 1 FROM coordinator_turns t
                JOIN coordinator_conversations c ON c.conversation_id=t.conversation_id
                  AND c.tenant_id=:tenant AND c.owner_id=:owner
                WHERE t.origin='interactive' AND NOT EXISTS (SELECT 1 FROM expert_conversations e
                  WHERE e.conversation_id=t.conversation_id) LIMIT 1""", params).fetchone()
        return {
            "experts": [{"id": row["lineage_id"], "template_id": row["template_id"], "name": row["name"]} for row in experts],
            "workers": [{"id": row["worker_id"], "name": row["name"], "ai": row["profile"]} for row in workers],
            "chat": chat is not None,
        }

    # ------------------------------------------------------------------ one line and its trail
    def line(self, tenant: str, owner: str, line_id: str) -> dict[str, Any]:
        key = _decode(line_id)
        parts = key.split(_SEP)
        if parts[0] not in {"t", "r", "e"} or len(parts) != (3 if parts[0] == "t" else 2):
            raise LineNotFound
        sql, params = self._sql(tenant, owner, LinesQuery(), key=key)
        with self.store._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                raise LineNotFound
            row = dict(row)
            if parts[0] == "t":
                trail = self._turn_trail(conn, tenant, owner, parts[1], parts[2], row)
            elif parts[0] == "r":
                trail = self._run_trail(conn, parts[1])
            else:
                trail = [{"at": row["at"], "step": "event", "event_type": row["event_type"]}]
        trail.sort(key=lambda step: (step["at"] or "", step.get("order", 0)))
        for step in trail:
            step.pop("order", None)
        return {"line": self._view(row), "trail": trail}

    @staticmethod
    def _events(conn: Any, runs: list[dict], since: str) -> list[dict]:
        """The run events of these runs, and the changes to the Workers that ran them when those Workers are a
        Chat's, an Expert's or a job's own (no line of their own), from the line's start."""
        steps: list[dict] = []
        for run in runs:
            for event in conn.execute(
                    "SELECT event_type, created_at FROM events WHERE worker_id=? AND run_id=? ORDER BY created_at",
                    (run["worker_id"], run["run_id"])):
                steps.append({"at": event["created_at"], "step": "event", "event_type": event["event_type"],
                              "ai": run["profile"], "order": 1})
        seen = set()
        for run in runs:
            if run["worker_id"] in seen or run["reusable"]:
                continue
            seen.add(run["worker_id"])
            ended = max((step["at"] for step in steps), default=since)
            for event in conn.execute(
                    "SELECT event_type, created_at FROM events WHERE worker_id=? AND COALESCE(run_id,'')='' "
                    "AND created_at>=? AND created_at<=? ORDER BY created_at", (run["worker_id"], since, ended)):
                steps.append({"at": event["created_at"], "step": "event", "event_type": event["event_type"],
                              "ai": run["profile"], "order": 1})
        return steps

    @staticmethod
    def _runs(conn: Any, where: str, args: tuple) -> list[dict]:
        return [dict(row) for row in conn.execute(
            f"SELECT r.run_id, r.state, COALESCE(NULLIF(r.first_queued_at,''), r.queued_at) AS at, w.worker_id, "
            f"w.profile, w.model, (CASE WHEN {_REUSABLE} THEN 1 ELSE 0 END) AS reusable "
            f"FROM runs r JOIN workers w ON w.worker_id=r.worker_id WHERE {where}", args)]

    def _conversation_runs(self, conn: Any, tenant: str, owner: str, cid: str, turns: list[str]) -> list[dict]:
        """Every run that answered these turns of this conversation: retries and a backup included."""
        if not turns:
            return []
        return self._runs(conn, "r.run_id IN (SELECT p.run_id FROM provider_requests p JOIN provider_sessions s "
                                "ON s.session_id=p.session_id WHERE s.conversation_id=? AND s.tenant_id=? AND s.owner_id=? "
                                f"AND p.run_id IS NOT NULL AND p.message_id IN ({','.join('?' * len(turns))}))",
                          (cid, tenant, owner, *turns))

    def _turn_trail(self, conn: Any, tenant: str, owner: str, cid: str, tid: str, row: dict) -> list[dict]:
        steps: list[dict] = [{"at": row["at"], "step": "received", "order": 0}]
        # The runtime's own turns in this conversation (a reply being checked, a job's result told back, the turn
        # after a decision) belong to this message until the next message: their runs are part of its trail.
        following = conn.execute("SELECT MIN(created_at) FROM coordinator_turns WHERE conversation_id=? "
                                 "AND origin='interactive' AND created_at>?", (cid, row["at"])).fetchone()[0]
        turns = [tid] + [turn[0] for turn in conn.execute(
            "SELECT turn_id FROM coordinator_turns WHERE conversation_id=? AND origin<>'interactive' AND created_at>=? "
            "AND (? IS NULL OR created_at<?)", (cid, row["at"], following, following))]
        steps += self._events(conn, self._conversation_runs(conn, tenant, owner, cid, turns), row["at"])
        for review in conn.execute(
                "SELECT review_id, kind, state, created_at, decided_at FROM expert_reviews WHERE tenant_id=? AND owner_id=? "
                "AND speaker_conversation_id=? AND source_turn_id=? ORDER BY created_at", (tenant, owner, cid, tid)):
            steps.append({"at": review["created_at"], "step": "asked_you", "what": review["kind"],
                          "state": review["state"], "review_id": review["review_id"], "order": 2})
            if review["decided_at"]:
                steps.append({"at": review["decided_at"], "step": "decided", "what": review["kind"],
                              "state": review["state"], "order": 3})
        jobs = [dict(job) for job in conn.execute(
            "SELECT job_id, state, created_at, updated_at, job_conversation_id FROM expert_jobs WHERE tenant_id=? "
            "AND owner_id=? AND speaker_conversation_id=? AND source_turn_id=? ORDER BY created_at", (tenant, owner, cid, tid))]
        sent = turns + ["handoff-" + job["job_id"] for job in jobs]
        for delivery in conn.execute(
                f"SELECT kind, state, created_at, finished_at FROM channel_deliveries WHERE conversation_id=? "
                f"AND turn_id IN ({','.join('?' * len(sent))}) ORDER BY created_at", (cid, *sent)):
            steps.append({"at": delivery["finished_at"] or delivery["created_at"], "step": "delivery",
                          "what": delivery["kind"], "state": delivery["state"], "order": 4})
        goal_places = [(cid, tid)]
        for job in jobs:
            started = {"at": job["created_at"], "step": "job", "state": "started", "order": 5}
            if job["job_conversation_id"]:
                started["conversation_id"] = job["job_conversation_id"]
                goal_places.append((job["job_conversation_id"], ""))
                job_turns = [turn[0] for turn in conn.execute(
                    "SELECT turn_id FROM coordinator_turns WHERE conversation_id=?", (job["job_conversation_id"],))]
                steps += self._events(conn, self._conversation_runs(conn, tenant, owner, job["job_conversation_id"],
                                                                    job_turns), job["created_at"])
            steps.append(started)
            if job["state"] in {"done", "failed", "stopped", "withdrawn"}:
                steps.append({"at": job["updated_at"], "step": "job", "state": job["state"], "order": 6})
        for place, source in goal_places:
            for run in self._runs(conn, "r.run_id IN (SELECT run_id FROM coordinator_goals WHERE conversation_id=? "
                                        "AND run_id<>'' AND (?='' OR source_turn_id=?))", (place, source, source)):
                steps.append({"at": run["at"], "step": "worker", "ai": run["profile"], "model": run["model"],
                              "state": run["state"], "order": 7})
        return steps

    def _run_trail(self, conn: Any, run_id: str) -> list[dict]:
        runs = self._runs(conn, "r.run_id=?", (run_id,))
        steps = self._events(conn, runs, runs[0]["at"] if runs else "")
        if conn.execute("SELECT 1 FROM scheduled_runs WHERE queued_run_id=?", (run_id,)).fetchone():
            steps.append({"at": runs[0]["at"] if runs else "", "step": "scheduled", "order": 0})
        return steps


def install_activity_line_routes(app: Any, store: Any, principal: Callable[[Request], tuple[Any, str, str]]) -> None:
    reader = ActivityLines(store)
    app.state.activity_lines = reader

    def owner(request: Request) -> tuple[str, str]:
        # The Experts' owner rule, as Needs you's own read of the person's words: a signed-in owner only.
        ctx, tenant, user = principal(request)
        if ctx.auth_mode == "signed_link" or str(ctx.role or "").lower() == "viewer":
            raise HTTPException(403, "Sign in as the owner to see Activity")
        return tenant, user

    @app.post("/v1/activity/lines")
    def activity_lines(payload: LinesQuery, request: Request) -> dict[str, Any]:
        tenant, user = owner(request)
        return reader.lines(tenant, user, payload)

    @app.get("/v1/activity/lines/{line_id}")
    def activity_line(line_id: str, request: Request) -> dict[str, Any]:
        tenant, user = owner(request)
        if not line_id or len(line_id) > 2048:
            raise HTTPException(404, "Activity line not found")
        try:
            return reader.line(tenant, user, line_id)
        except LineNotFound as exc:
            raise HTTPException(404, "Activity line not found") from exc
