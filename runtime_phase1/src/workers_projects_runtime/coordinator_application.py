"""Application-prepared coordinator turns over the existing goals and callback outbox."""
from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field

from .coordinator import CoordinatorConfig, CoordinatorConflict, CoordinatorScopeError, StrictModel, canonical, digest, now


EVENTS = ("coordinator.turn_due", "coordinator.dispatch_due", "coordinator.result_ready", "coordinator.action_due",
          "coordinator.turn_result_ready")


class ResultPublicationData(StrictModel):
    conversation_id: str = Field(min_length=1)
    request_ref: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    result_revision: int = Field(ge=1, strict=True)
    result_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ResultPublication(StrictModel):
    event_id: str = Field(min_length=1)
    type: Literal["coordinator.result_ready"]
    created_at: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    source_turn_id: str = Field(min_length=1)
    data: ResultPublicationData


class TurnResultPublicationData(StrictModel):
    conversation_id: str = Field(min_length=1)
    request_ref: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    result_revision: int = Field(ge=1, strict=True)
    result_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class TurnResultPublication(StrictModel):
    event_id: str = Field(min_length=1)
    type: Literal["coordinator.turn_result_ready"]
    created_at: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    source_turn_id: str = Field(min_length=1)
    data: TurnResultPublicationData


def application(conversation):
    return CoordinatorConfig.model_validate_json(conversation["config_json"]).application


def publications(core, tenant, owner, conversation_id, cursor="", limit=100):
    """Read retained result identity from the existing outbox, without admitting delivery."""
    conversation = core._conversation(tenant, owner, conversation_id)
    binding = application(conversation)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise CoordinatorConflict("Invalid publication page")
    if not isinstance(cursor, str):
        raise CoordinatorConflict("Invalid publication cursor")
    if binding is None:
        if cursor:
            raise CoordinatorConflict("Publication cursor unavailable")
        return {"conversation_id": conversation_id, "publications": [], "next_cursor": None}

    def projection(record, connection):
        try:
            payload = json.loads(record["payload_json"])
            is_turn = record["event_type"] == EVENTS[4]
            model = TurnResultPublication if is_turn else ResultPublication
            source_turn_id = (original_source_turn(connection, conversation_id, record["result_turn_id"])
                              if is_turn else record["source_turn_id"])
            value = model.model_validate({
                **{key: payload[key] for key in ("event_id", "type", "created_at", "expires_at", "data")},
                "source_turn_id": source_turn_id,
            })
            if (record["callback_id"] != value.event_id or record["event_type"] != value.type
                    or payload.get("principal") != {"tenant_id": tenant, "owner_id": owner}
                    or value.data.conversation_id != conversation_id
                    or value.data.request_ref != binding.request_ref):
                raise ValueError("Publication identity mismatch")
            datetime.fromisoformat(value.created_at.replace("Z", "+00:00"))
            datetime.fromisoformat(value.expires_at.replace("Z", "+00:00"))
            return value.model_dump()
        except (KeyError, TypeError, ValueError) as exc:
            raise CoordinatorConflict("Retained publication identity is invalid") from exc

    # The existing insertion order, not mutable delivery state, supplies the page order.
    # A cursor is an existing row identity in this exact authenticated scope.
    source = ("SELECT cb.rowid AS publication_sequence,cb.callback_id,cb.event_type,cb.payload_json,"
              "g.source_turn_id,t.turn_id AS result_turn_id "
              "FROM callback_outbox cb LEFT JOIN coordinator_goals g "
              "ON g.conversation_id=json_extract(cb.payload_json,'$.data.conversation_id') "
              "AND g.goal_id=json_extract(cb.payload_json,'$.data.goal_id') "
              "LEFT JOIN coordinator_turns t ON t.conversation_id=json_extract(cb.payload_json,'$.data.conversation_id') "
              "AND t.turn_id=json_extract(cb.payload_json,'$.data.turn_id') "
              "WHERE cb.tenant_id=? AND cb.owner_id=? AND cb.event_type IN (?,?) "
              "AND json_valid(cb.payload_json) "
              "AND json_extract(cb.payload_json,'$.data.conversation_id')=?")
    parameters = (tenant, owner, EVENTS[2], EVENTS[4], conversation_id)
    with core.store._connect() as conn:
        after = None
        if cursor:
            row = conn.execute(source + " AND cb.callback_id=?", (*parameters, cursor)).fetchone()
            if row is None:
                raise CoordinatorConflict("Publication cursor unavailable")
            projection(row, conn)
            after = row["publication_sequence"]
        page = " AND cb.rowid>?" if after is not None else ""
        rows = conn.execute(source + page + " ORDER BY cb.rowid LIMIT ?",
                            (*parameters, *((after,) if after is not None else ()), limit + 1)).fetchall()
        items = [projection(row, conn) for row in rows[:limit]]
    return {"conversation_id": conversation_id, "publications": items,
            "next_cursor": items[-1]["event_id"] if len(rows) > limit else None}


def validate_destination(core, tenant, owner, binding):
    destinations = getattr(core.service, "webhook_destinations", None)
    target = destinations.get(tenant, owner, binding.destination_id) if destinations else None
    if not target or not target.get("enabled") or target.get("payload_mode") != "envelope":
        raise CoordinatorScopeError("Coordinator application destination is unavailable")


def notify(core, conversation, event_type, identity, data, *, connection=None):
    binding = application(conversation)
    if binding is None:
        raise CoordinatorConflict("Application preparation is not configured")
    callback_id = "cb_coordinator_" + digest([conversation["conversation_id"], event_type, identity])
    stamp = now()
    expires = (datetime.now(UTC) + timedelta(seconds=binding.delivery_timeout_seconds)).isoformat()
    payload = {"type": event_type, "event_id": callback_id,
               "principal": {"tenant_id": conversation["tenant_id"], "owner_id": conversation["owner_id"]},
               "created_at": stamp, "expires_at": expires,
               "data": {"conversation_id": conversation["conversation_id"],
                        "request_ref": binding.request_ref, **data}}
    def write(conn):
        conn.execute("INSERT OR IGNORE INTO callback_outbox"
                     "(callback_id,tenant_id,owner_id,event_type,url,payload_json,status,created_at,updated_at,"
                     "destination_id,next_attempt_at,expires_at) VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?)",
                     (callback_id, conversation["tenant_id"], conversation["owner_id"], event_type,
                      "destination://" + binding.destination_id, canonical(payload), stamp, stamp,
                      binding.destination_id, stamp, expires))
    if connection is not None:
        write(connection)
    else:
        with core.store._connect() as conn:
            write(conn)
    wake = getattr(core.service, "_scheduler_wake_event", None)
    if wake is not None:
        wake.set()
    return callback_id


def interactive_retry_available(core, conversation, turn, *, connection=None):
    if (conversation.get("restore_hold") or turn["restore_hold"]
            or turn["origin"] != "interactive" or turn["blocker"] not in {"failed", "native_input_expired"}
            or turn["response_json"] or not turn["request_id"]):
        return False
    binding = application(conversation)
    if binding is not None:
        try:
            validate_destination(core, conversation["tenant_id"], conversation["owner_id"], binding)
        except CoordinatorScopeError:
            return False
    def check(conn):
        request = conn.execute("SELECT state FROM provider_requests WHERE request_id=? AND tenant_id=? AND owner_id=?",
                               (turn["request_id"], conversation["tenant_id"], conversation["owner_id"])).fetchone()
        if not request or request["state"] not in {"failed", "cancelled"}:
            return False
        root = original_source_turn(conn, conversation["conversation_id"], turn["turn_id"])
        if conn.execute("SELECT 1 FROM coordinator_goals WHERE conversation_id=? AND source_turn_id=? LIMIT 1",
                        (conversation["conversation_id"], root)).fetchone():
            return False
        return not conn.execute("SELECT 1 FROM coordinator_turns WHERE conversation_id=? AND retry_of_turn_id=? LIMIT 1",
                                (conversation["conversation_id"], turn["turn_id"])).fetchone()
    if connection is not None:
        return check(connection)
    with core.store._connect() as conn:
        return check(conn)


def turn_status(core, tenant, owner, conversation_id, turn_id):
    conversation = core._conversation(tenant, owner, conversation_id)
    with core.store._connect() as conn:
        row = conn.execute("SELECT rowid,* FROM coordinator_turns "
                           "WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone()
    if not row:
        raise CoordinatorConflict("Accepted turn unavailable")
    state = "restore_held" if conversation.get("restore_hold") or row["restore_hold"] else (
        "cancelled" if row["blocker"] == "cancelled" else "blocked" if row["blocker"] else "queued")
    request = None
    if row["request_id"] and state not in {"cancelled", "restore_held"}:
        request = core.store.get_provider_request(row["request_id"])
        state = str((request or {}).get("state") or "blocked")
    if row["response_json"]:
        state = "completed"
    with core.store._connect() as conn:
        source_id = original_source_turn(conn,conversation_id,turn_id)
        child = conn.execute("SELECT turn_id FROM coordinator_turns WHERE conversation_id=? AND retry_of_turn_id=?",
                             (conversation_id, turn_id)).fetchone()
        retryable = interactive_retry_available(core, conversation, row, connection=conn)
    binding = application(conversation)
    result = None
    if binding is not None and row["origin"] == "interactive" and row["response_json"]:
        try:
            result = completed_turn_result(core, tenant, owner, conversation_id, turn_id)
        except (CoordinatorConflict, CoordinatorScopeError):
            pass
    return {"conversation_id": conversation_id, "turn_id": turn_id, "origin": row["origin"],
            "created_at": row["created_at"],
            **({"message": row["message"]} if row["origin"] == "interactive" else {}),
            "source_turn_id": source_id,
            "retry_of_turn_id": row["retry_of_turn_id"], "retry_turn_id": child["turn_id"] if child else "",
            "request_id": row["request_id"], "state": state, "blocker": row["blocker"],
            "request_ref": binding.request_ref if binding else "",
            "run_id": result["run_id"] if result else str((request or {}).get("run_id") or ""),
            "result_revision": result["result_revision"] if result else None,
            "has_result": bool(result and result["presentation_eligible"]),
            "presentation_eligible": bool(result and result["presentation_eligible"]),
            **({"superseded": result["superseded"]} if result else {}),
            "intent_state": result["intent_state"] if result else "cancelled" if row["blocker"] == "cancelled" else "accepted",
            "restore_hold": result["restore_hold"] if result else bool(conversation.get("restore_hold") or row["restore_hold"]),
            "actions": ["retry"] if retryable else ["stop"] if state in {"queued", "pending", "running", "streaming"} else []}


def queue_turn(core, conversation, turn_id):
    tenant, owner, cid = conversation["tenant_id"], conversation["owner_id"], conversation["conversation_id"]
    state = turn_status(core, tenant, owner, cid, turn_id)
    if state["request_id"] or state["state"] in {"cancelled", "restore_held", "completed"}:
        return state
    notify(core, conversation, EVENTS[0], turn_id, {"turn_id": turn_id})
    return state


def dispatch_digest(conversation_id, dispatch):
    return digest({"conversation_id": conversation_id, "dispatch": dispatch.request_payload()})



def dispatch_notification_identity(goal):
    return json.loads(goal["preparation_json"] or "{}").get("dispatch_notification_identity", goal["goal_id"])


def dispatch_notification_ref(cid, goal):
    return "cb_coordinator_" + digest([cid, EVENTS[1], dispatch_notification_identity(goal)])


def assert_dispatch_notification(cid, goal, expected):
    explicit = json.loads(goal["preparation_json"] or "{}").get("dispatch_notification_identity")
    if (explicit and not expected) or (expected and expected != dispatch_notification_ref(cid, goal)):
        raise CoordinatorConflict("Application preparation targets a superseded dispatch notification")

def queue_dispatch(core, tenant, owner, conversation, dispatch, *, explicit=True, retry_action_key=""):
    cid = conversation["conversation_id"]
    config = CoordinatorConfig.model_validate_json(conversation["config_json"])
    if dispatch.route_id not in {route.id for route in config.routes}:
        raise CoordinatorScopeError("Route outside coordinator authority")
    if explicit:
        validate_destination(core, tenant, owner, config.application)
    serialized = canonical(dispatch.request_payload())
    with core.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        goal = conn.execute("SELECT g.*,c.restore_hold AS conversation_restore_hold FROM coordinator_goals g "
                            "JOIN coordinator_conversations c USING(conversation_id) "
                            "WHERE g.conversation_id=? AND g.goal_id=?", (cid, dispatch.goal_id)).fetchone()
        if not goal:
            raise CoordinatorConflict("Accepted goal unavailable")
        if retry_action_key:
            action = conn.execute("SELECT application_json FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                                  (cid, dispatch.goal_id, retry_action_key)).fetchone()
            if not action:
                raise CoordinatorConflict("Explicit dispatch retry receipt is unavailable")
            if json.loads(action["application_json"] or "{}").get("dispatch_retry"):
                return core.goal_snapshot(tenant, owner, cid, dispatch.goal_id)
        if goal["intent_state"] == "cancelled" or goal["restore_hold"] or goal["conversation_restore_hold"]:
            raise CoordinatorConflict("Goal is held or stopped before dispatch")
        if dispatch.instruction is not None and dispatch.instruction != goal["text"]:
            raise CoordinatorConflict("Application dispatch cannot replace the original goal; use separate guidance")
        if goal["dispatch_json"] and goal["dispatch_json"] != serialized:
            raise CoordinatorConflict("Goal already has a different dispatch; use exact run controls")
        first_dispatch = not goal["dispatch_json"]
        saved_digest = goal["application_dispatch_digest"] or dispatch_digest(cid, dispatch)
        ordinal = goal["application_dispatch_ordinal"] or (conn.execute(
            "SELECT COUNT(*)+1 FROM coordinator_goals WHERE conversation_id=? AND dispatch_json<>''", (cid,)).fetchone()[0])
        notification_identity = dispatch_notification_identity(goal)
        callback_id = dispatch_notification_ref(cid, goal)
        callback = conn.execute("SELECT * FROM callback_outbox WHERE callback_id=?", (callback_id,)).fetchone()
        if retry_action_key and (not callback or callback["status"] not in {"rejected", "expired"}):
            raise CoordinatorConflict("The exact dispatch is not awaiting an explicit retry")
        if callback and callback["status"] in {"rejected", "expired"}:
            if not explicit:
                return core.goal_snapshot(tenant, owner, cid, dispatch.goal_id)
            expired = (callback["status"] == "expired" or not callback["expires_at"]
                or datetime.fromisoformat(callback["expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC))
            if callback["cancel_requested_at"] or (expired and not retry_action_key):
                raise CoordinatorConflict("The original dispatch delivery needs a fresh explicit retry or was withdrawn")
            payload = json.loads(callback["payload_json"])
            if (callback["tenant_id"] != tenant or callback["owner_id"] != owner
                    or callback["event_type"] != EVENTS[1] or payload.get("type") != EVENTS[1]
                    or payload.get("event_id") != callback_id
                    or callback["destination_id"] != config.application.destination_id
                    or payload.get("principal") != {"tenant_id": tenant, "owner_id": owner}
                    or payload.get("data") != {"conversation_id": cid, "request_ref": config.application.request_ref,
                                              "goal_id": dispatch.goal_id, "dispatch_digest": saved_digest}):
                raise CoordinatorScopeError("Rejected dispatch no longer matches its application authority")
            saved_preparation = json.loads(goal["preparation_json"] or "{}")
            reservation = conn.execute("SELECT 1 FROM delegations WHERE tenant_id=? AND owner_id=? AND idempotency_key=?",
                (tenant, owner, saved_preparation.get("idempotency_key", ""))).fetchone()
            if goal["run_id"] or goal["worker_id"] or goal["work_ref"] or reservation:
                raise CoordinatorConflict("Accepted work must recover through its existing reservation")
            saved_preparation["explicit_redispatch_generation"] = int(saved_preparation.get("explicit_redispatch_generation", 0)) + 1
            if expired:
                notification_identity = canonical([dispatch.goal_id, "retry", retry_action_key])
                saved_preparation["dispatch_notification_identity"] = notification_identity
                # The new event and current reference commit with the explicit intent.
                # Recovery cannot manufacture a later expiry for this generation.
                notify(core, conversation, EVENTS[1], notification_identity,
                    {"goal_id": dispatch.goal_id, "dispatch_digest": saved_digest}, connection=conn)
            else:
                conn.execute("UPDATE callback_outbox SET status='pending',next_attempt_at=?,updated_at=?,"
                             "delivery_lease_token='',delivery_lease_expires_at=NULL WHERE callback_id=? AND status='rejected'",
                             (now(), now(), callback_id))
            conn.execute("UPDATE coordinator_goals SET preparation_json=? WHERE conversation_id=? AND goal_id=?",
                (canonical(saved_preparation), cid, dispatch.goal_id))
            if retry_action_key:
                conn.execute("UPDATE coordinator_actions SET application_json=? WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                             (canonical({"dispatch_retry": True, "dispatch_digest": saved_digest}), cid, dispatch.goal_id, retry_action_key))
        conn.execute("UPDATE coordinator_goals SET dispatch_json=?,application_dispatch_digest=?,application_dispatch_ordinal=?,"
                     "blocker='' WHERE conversation_id=? AND goal_id=?", (serialized, saved_digest, ordinal, cid, dispatch.goal_id))
    qa_admission = getattr(core.service, "local_qa_coordinator_admission", None)
    if first_dispatch and qa_admission is not None:
        qa_admission(tenant_id=tenant, owner_id=owner, conversation_id=cid, ordinal=ordinal)
    if not goal["run_id"]:
        notify(core, conversation, EVENTS[1], notification_identity,
               {"goal_id": dispatch.goal_id, "dispatch_digest": saved_digest})
    return core.goal_snapshot(tenant, owner, cid, dispatch.goal_id)



def dispatch_retry_available(core, conversation, goal):
    """Truthful native Retry mask for a rejected, still-unaccepted application dispatch."""
    binding = application(conversation)
    if (binding is None or conversation.get("restore_hold") or goal.get("restore_hold")
            or goal.get("intent_state") != "accepted" or goal.get("run_id") or goal.get("worker_id")
            or goal.get("work_ref") or not goal.get("dispatch_json") or goal.get("blocker") not in {"application_delivery_rejected", "application_delivery_expired"}):
        return False
    callback_id = dispatch_notification_ref(conversation["conversation_id"], goal)
    with core.store._connect() as conn:
        record = conn.execute("SELECT * FROM callback_outbox WHERE callback_id=?", (callback_id,)).fetchone()
        saved = json.loads(goal.get("preparation_json") or "{}")
        reserved = conn.execute("SELECT 1 FROM delegations WHERE tenant_id=? AND owner_id=? AND idempotency_key=?",
            (conversation["tenant_id"], conversation["owner_id"], saved.get("idempotency_key", ""))).fetchone()
    if (not record or record["status"] not in {"rejected", "expired"} or record["cancel_requested_at"] or reserved):
        return False
    try:
        validate_destination(core, conversation["tenant_id"], conversation["owner_id"], binding)
    except CoordinatorScopeError:
        return False
    return True


def retry_dispatch(core, tenant, owner, cid, gid, control):
    from .coordinator import Dispatch
    conversation = core._conversation(tenant, owner, cid)
    payload = canonical(control.model_dump())
    with core.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                                (cid, gid, control.idempotency_key)).fetchone()
        if previous and previous["payload_json"] != payload:
            raise CoordinatorConflict("Control idempotency key already has different input")
        if previous and previous["response_json"]:
            return json.loads(previous["response_json"])
        admitted = previous and json.loads(previous["application_json"] or "{}").get("dispatch_retry")
        goal = dict(conn.execute("SELECT * FROM coordinator_goals WHERE conversation_id=? AND goal_id=?", (cid, gid)).fetchone())
        if not admitted and (control.run_id or control.message or not dispatch_retry_available(core, conversation, goal)):
            raise CoordinatorConflict("This goal has no rejected dispatch eligible for retry")
        conn.execute("INSERT OR IGNORE INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json) VALUES(?,?,?,?)",
                     (cid, gid, control.idempotency_key, payload))
    result = queue_dispatch(core, tenant, owner, conversation, Dispatch.model_validate_json(goal["dispatch_json"]),
                            retry_action_key=control.idempotency_key)
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_actions SET response_json=? WHERE conversation_id=? AND goal_id=? AND idempotency_key=? AND response_json=''",
                     (canonical(result), cid, gid, control.idempotency_key))
    return result


def dispatch_retry_receipt(core, tenant, owner, cid, gid, payload):
    """Recover the exact no-run Retry receipt even after its real Worker has started."""
    from .coordinator import Control
    core._conversation(tenant, owner, cid)
    with core.store._connect() as conn:
        row = conn.execute("SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                           (cid, gid, payload.idempotency_key)).fetchone()
    if not row or not json.loads(row["application_json"] or "{}").get("dispatch_retry"):
        return None
    control = Control(action="retry", idempotency_key=payload.idempotency_key)
    semantic_input = (payload.native_input, payload.native_human_input, payload.file_inputs,
                      payload.capability_reauthorization, getattr(payload, "application_bootstrap_bundle_b64", None))
    automatic_context = (payload.runtime_bundle, payload.inherited_conversation, payload.source_context)
    if (payload.action != "retry" or payload.instruction or any(semantic_input)
            or (not row["response_json"] and any(automatic_context))
            or row["payload_json"] != canonical(control.model_dump())):
        raise CoordinatorConflict("Dispatch retry receipt input changed")
    # A completed control receipt never dispatches again. Later automatic run context
    # cannot change that stored empty-run operation after its real Worker has bound.
    snapshot = json.loads(row["response_json"]) if row["response_json"] else core.goal_snapshot(tenant, owner, cid, gid)
    return {"action": "retry", "status": "accepted", "state": snapshot["state"], "confirmationPending": False,
            "idempotentReplay": True, "goal": snapshot}

def dispatch_input(core, tenant, owner, conversation_id, goal_id):
    from .coordinator import Dispatch
    conversation = core._conversation(tenant, owner, conversation_id)
    config = CoordinatorConfig.model_validate_json(conversation["config_json"])
    if config.application is None:
        raise CoordinatorConflict("Application preparation is not configured")
    goal = core._goal(conversation_id, goal_id)
    if not goal["dispatch_json"]:
        raise CoordinatorConflict("Dispatch has not been accepted")
    dispatch = Dispatch.model_validate_json(goal["dispatch_json"])
    route = next((item for item in config.routes if item.id == dispatch.route_id), None)
    return {"conversation_id": conversation_id, "goal_id": goal_id, "source_turn_id": goal["source_turn_id"],
            "goal": goal["text"], "instruction": goal["text"] if dispatch.instruction is None else dispatch.instruction,
            "guidance": dispatch.guidance,
            "dispatch": dispatch.request_payload(), "dispatch_digest": goal["application_dispatch_digest"],
            "notification_ref": dispatch_notification_ref(conversation_id, goal),
            "redispatch_generation": int(json.loads(goal["preparation_json"] or "{}").get("explicit_redispatch_generation", 0)),
            "route": route.model_dump(exclude={"bootstrap_bundle"}) if route else None,
            "request_ref": config.application.request_ref, "state": core.goal_snapshot(tenant, owner, conversation_id, goal_id)["state"]}


def _existing_request(core, tenant, owner, cid, tid):
    with core.store._connect() as conn:
        rows = conn.execute("SELECT p.*,s.model_id AS coordinator_model FROM provider_requests p JOIN provider_sessions s ON s.session_id=p.session_id "
                            "WHERE p.tenant_id=? AND p.owner_id=? AND s.tenant_id=p.tenant_id AND s.owner_id=p.owner_id "
                            "AND s.conversation_id=? AND s.agent_id='coordinator' AND p.message_id=? AND p.idempotency_key=?",
                            (tenant, owner, cid, tid, f"{cid}:{tid}")).fetchall()
    if len(rows) > 1:
        raise CoordinatorConflict("Accepted turn has conflicting native requests")
    return dict(rows[0]) if rows else None


def prepare_turn(core, tenant, owner, cid, tid, payload):
    from .conversation_provider import ChatCompletionRequest
    conversation = core._conversation(tenant, owner, cid)
    core._assert_not_restore_held(conversation)
    config = CoordinatorConfig.model_validate_json(conversation["config_json"])
    if config.application is None:
        raise CoordinatorConflict("Application preparation is not configured")
    with core.store._connect() as conn:
        turn = conn.execute("SELECT * FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (cid, tid)).fetchone()
    if not turn or turn["blocker"] == "cancelled" or turn["restore_hold"]:
        raise CoordinatorConflict("Accepted turn is unavailable, held or stopped")
    existing = _existing_request(core, tenant, owner, cid, tid)
    if existing:
        contract_reader = getattr(core.provider, "_saved_completion_contract", None)
        contract = contract_reader(existing) if contract_reader else None
        if existing["coordinator_model"] != config.model or (contract and (
                contract.get("model") != config.model or contract.get("reasoning_effort") != config.effort)):
            raise CoordinatorConflict("Existing native request differs from the accepted coordinator configuration")
    if not existing:
        metadata = payload.metadata
        expected = {"tenant_id": tenant, "owner_id": owner, "conversation_id": cid, "agent_id": "coordinator",
                    "message_id": tid, "stream_id": tid, "idempotency_key": f"{cid}:{tid}", "surface": "coordinator"}
        if (metadata is None or any(getattr(metadata, key, None) != value for key, value in expected.items())
                or payload.model != config.model or payload.reasoning_effort != config.effort):
            raise CoordinatorScopeError("Prepared native request does not match the accepted coordinator turn")
        scope = core._conversation_scope(conversation)
        if metadata.allowed_ai_origin_scope and metadata.allowed_ai_origin_scope != scope:
            raise CoordinatorScopeError("Prepared native request changed its accepted origin scope")
        if metadata.origin not in {"system", "worker"} or metadata.actor_kind not in {"system", "worker"}:
            raise CoordinatorScopeError("Background planning requires system or worker authority")
        core._assert_foreground_allowed_ai(conversation, config)
        packet_data = payload.model_dump(mode="json")
        packet_data["messages"] = [*packet_data["messages"],
                                   {"role": "system", "content": json.loads(conversation["prompt_json"])["content"]},
                                   {"role": "user" if turn["origin"] == "interactive" else "tool", "content": turn["message"]}]
        packet = ChatCompletionRequest.model_validate(packet_data)
        packet.metadata.allowed_ai_origin_scope = scope
        # The provider owns the saved completion contract. This projection never retains caller grants.
        projection = ChatCompletionRequest(model=config.model, reasoning_effort=config.effort,
            messages=[{"role": "user", "content": turn["message"]}], metadata=expected)
        with core.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET payload_json=? WHERE conversation_id=? AND turn_id=? AND request_id=''",
                         (canonical(projection.model_dump(mode="json")), cid, tid))
        existing = core.provider.start(packet, tenant_id=tenant)
    with core.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute("SELECT request_id,blocker,restore_hold FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (cid, tid)).fetchone()
        if latest["request_id"] and latest["request_id"] != existing["request_id"]:
            raise CoordinatorConflict("Accepted turn has a different native request")
        if latest["blocker"] != "cancelled" and not latest["restore_hold"]:
            conn.execute("UPDATE coordinator_turns SET request_id=?,blocker='' WHERE conversation_id=? AND turn_id=?",
                         (existing["request_id"], cid, tid))
    return turn_status(core, tenant, owner, cid, tid)


def prepare_goal(core, tenant, owner, cid, gid, expected_dispatch, values, notification_ref=None):
    from .application_boundary import launch_authority_for
    details = dispatch_input(core, tenant, owner, cid, gid)
    conversation = core._conversation(tenant, owner, cid)
    core._assert_not_restore_held(conversation)
    assert_dispatch_notification(cid, core._goal(cid, gid), notification_ref)
    if details["dispatch_digest"] != expected_dispatch:
        raise CoordinatorConflict("Prepared delegation targets a different dispatch")
    route = details["route"]
    if (route is None or values["tenant_id"] != tenant or values["owner_id"] != owner
            or values["goal"] != details["goal"] or values["instruction"] != details["instruction"]
            or values["profile"] != route["profile"] or values["execution_mode"] != route["execution_mode"]
            or values["resource_class"] != route["resource_class"]):
        raise CoordinatorScopeError("Prepared delegation does not match the accepted goal and route")
    authority = launch_authority_for(values.get("bootstrap_bundle") or {}) or {}
    prefix = route["profile"] + ":"
    selected = str(authority.get("worker_model") or "").removeprefix(prefix)
    if (selected != str(route["model"]).removeprefix(prefix)
            or authority.get("worker_reasoning_effort") != route["effort"]):
        raise CoordinatorScopeError("Prepared delegation changed the configured model or effort")
    bundle = values.get("bootstrap_bundle") or {}
    account = bundle.get("provider_account") or {}
    if route.get("connection_id") and route["connection_id"] not in {
            bundle.get("connection_id"), account.get("account_id")}:
        raise CoordinatorScopeError("Prepared delegation changed its configured account")
    intent = {key: values[key] for key in ("idempotency_key", "origin_ref", "request_digest")}
    with core.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        goal = conn.execute("SELECT * FROM coordinator_goals WHERE conversation_id=? AND goal_id=?", (cid, gid)).fetchone()
        if goal["intent_state"] == "cancelled" or goal["restore_hold"]:
            raise CoordinatorConflict("Goal is held or stopped before preparation")
        if goal["application_dispatch_digest"] != expected_dispatch or digest({"conversation_id": cid, "dispatch": json.loads(goal["dispatch_json"])}) != expected_dispatch:
            raise CoordinatorConflict("Accepted dispatch changed before preparation")
        assert_dispatch_notification(cid, goal, notification_ref)
        saved = json.loads(goal["preparation_json"] or "{}")
        has_identity = bool(saved.get("idempotency_key"))
        if has_identity and any(saved.get(key) != intent[key] for key in ("idempotency_key", "origin_ref")):
            raise CoordinatorConflict("Prepared delegation changed its application identity")
        prior = conn.execute("SELECT * FROM delegations WHERE tenant_id=? AND owner_id=? AND idempotency_key=?",
                             (tenant, owner, intent["idempotency_key"])).fetchone()
        if not has_identity:
            if prior is not None:
                raise CoordinatorScopeError("Existing delegation was not reserved for this accepted goal")
            saved = {**saved, **intent, "preparation_generation": int(saved.get("explicit_redispatch_generation", 0))}
            conn.execute("UPDATE coordinator_goals SET preparation_json=? WHERE conversation_id=? AND goal_id=?",
                         (canonical(saved), cid, gid))
        elif prior is None and int(saved.get("explicit_redispatch_generation", 0)) > int(saved.get("preparation_generation", 0)):
            callback_id = dispatch_notification_ref(cid, goal)
            callback = conn.execute("SELECT * FROM callback_outbox WHERE callback_id=?", (callback_id,)).fetchone()
            if (not callback or callback["status"] not in {"pending", "delivering"} or callback["cancel_requested_at"]
                    or not callback["expires_at"] or datetime.fromisoformat(callback["expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC)):
                raise CoordinatorConflict("The explicit dispatch retry is no longer current")
            validate_destination(core, tenant, owner, CoordinatorConfig.model_validate_json(conversation["config_json"]).application)
            if goal["run_id"] or goal["worker_id"] or goal["work_ref"]:
                raise CoordinatorConflict("Bound work cannot replace its prepared request")
            # Only the explicit dispatch owner issues this one-preparation allowance.
            # HTTP attempts and maintenance leases do not grant fresh authority.
            history = list(saved.get("preparation_history", []))
            history.append({"request_digest": saved["request_digest"], "generation": int(saved.get("preparation_generation", 0))})
            saved = {**saved, "request_digest": intent["request_digest"], "preparation_history": history,
                     "preparation_generation": int(saved["explicit_redispatch_generation"])}
            conn.execute("UPDATE coordinator_goals SET preparation_json=? WHERE conversation_id=? AND goal_id=?",
                         (canonical(saved), cid, gid))
    reservation = core.store.get_delegation_by_idempotency_key(tenant_id=tenant, owner_id=owner, idempotency_key=intent["idempotency_key"])
    if reservation is None:
        if saved and saved["request_digest"] != values["request_digest"]:
            raise CoordinatorConflict("Application changed an unstarted prepared delegation")
        prepared = {**values, "origin_scope": core._conversation_scope(conversation)}
        if details.get("guidance"):
            inherited = (values.get("bootstrap_bundle") or {}).get("inherited_conversation") or {}
            if not any(isinstance(source, dict) and source.get("role") == "context"
                       and source.get("kind") == "coordinator_guidance" and source.get("text") == details["guidance"]
                       for source in inherited.get("sources", [])):
                raise CoordinatorScopeError("Prepared delegation omitted its separate specialist guidance")
        # A stopped/held intent wins even if context preparation completed late.
        latest = core._goal(cid, gid)
        current_conversation = core._conversation(tenant, owner, cid)
        if (latest["intent_state"] == "cancelled" or latest["restore_hold"] or current_conversation["restore_hold"]
                or latest["application_dispatch_digest"] != expected_dispatch
                or digest({"conversation_id": cid, "dispatch": json.loads(latest["dispatch_json"])}) != expected_dispatch):
            raise CoordinatorConflict("Accepted goal changed before reservation")
        assert_dispatch_notification(cid, latest, notification_ref)
        reservation = core.service.reserve_delegation(**prepared, start_run=False)
    elif reservation["request_digest"] != (saved or intent)["request_digest"]:
        raise CoordinatorConflict("Saved delegation differs from this goal's reservation")
    with core.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        goal = conn.execute("SELECT g.*,c.restore_hold AS conversation_restore_hold FROM coordinator_goals g "
                            "JOIN coordinator_conversations c USING(conversation_id) WHERE g.conversation_id=? AND g.goal_id=?", (cid, gid)).fetchone()
        if goal["application_dispatch_digest"] != expected_dispatch or digest({"conversation_id": cid, "dispatch": json.loads(goal["dispatch_json"])}) != expected_dispatch:
            raise CoordinatorConflict("Accepted dispatch changed before binding")
        assert_dispatch_notification(cid, goal, notification_ref)
        if goal["run_id"] and goal["run_id"] != reservation["initial_run_id"]:
            raise CoordinatorConflict("Goal already has different work")
        conn.execute("UPDATE coordinator_goals SET work_ref=?,worker_id=?,run_id=?,blocker='' WHERE conversation_id=? AND goal_id=?",
                     (reservation["work_ref"], reservation["worker_id"], reservation["initial_run_id"], cid, gid))
        stopped = goal["intent_state"] == "cancelled"
        held = goal["restore_hold"] or goal["conversation_restore_hold"]
    if stopped:
        core.service.cancel_run(reservation["worker_id"], reservation["initial_run_id"])
    elif not held:
        latest = core._goal(cid, gid)
        latest_conversation = core._conversation(tenant, owner, cid)
        if latest["intent_state"] == "cancelled" or latest["restore_hold"] or latest_conversation["restore_hold"]:
            return reservation
        core.service.start_assigned_run(reservation["worker_id"])
    return reservation


def queue_self_results(core, conversation):
    with core.store._connect() as conn:
        rows = conn.execute("SELECT g.goal_id,r.run_id,r.terminal_result_revision,r.output_text FROM coordinator_goals g "
                            "JOIN runs r ON r.run_id=g.run_id JOIN provider_requests p ON p.run_id=r.run_id "
                            "JOIN provider_sessions s ON s.session_id=p.session_id "
                            "WHERE g.conversation_id=? AND g.work_ref='' AND s.conversation_id=g.conversation_id "
                            "AND s.agent_id='coordinator' AND p.response_json<>'' AND p.state IN ('completed','failed','cancelled') "
                            "AND r.state IN ('completed','failed','cancelled','interrupted')",
                            (conversation["conversation_id"],)).fetchall()
    for row in rows:
        data = {"goal_id": row["goal_id"], "run_id": row["run_id"],
                "result_revision": int(row["terminal_result_revision"] or 0),
                "result_digest": "sha256:" + hashlib.sha256(str(row["output_text"] or "").encode()).hexdigest()}
        notify(core, conversation, EVENTS[2], canonical(data), data)


def completed_turn_result(core, tenant, owner, cid, tid):
    """Project one real canonical turn result; never infer completion from native stdout."""
    conversation = core._conversation(tenant, owner, cid)
    if application(conversation) is None:
        raise CoordinatorConflict("Application turn result unavailable")
    with core.store._connect() as conn:
        row = conn.execute(
            "SELECT t.turn_id,t.origin,t.request_id,t.blocker,t.restore_hold,t.response_json,"
            "p.run_id,p.state AS request_state,p.message_id,p.stream_id,p.response_json AS provider_response,"
            "p.restore_hold AS request_hold,r.state AS run_state,r.terminal_result_revision,r.active_attempt_id "
            "FROM coordinator_turns t JOIN provider_requests p ON p.request_id=t.request_id "
            "JOIN provider_sessions s ON s.session_id=p.session_id JOIN runs r ON r.run_id=p.run_id "
            "JOIN workers w ON w.worker_id=r.worker_id "
            "WHERE t.conversation_id=? AND t.turn_id=? AND p.tenant_id=? AND p.owner_id=? "
            "AND s.tenant_id=p.tenant_id AND s.owner_id=p.owner_id AND s.conversation_id=t.conversation_id "
            "AND s.agent_id='coordinator' AND r.tenant_id=p.tenant_id "
            "AND w.tenant_id=p.tenant_id AND w.owner_id=p.owner_id AND w.project_id=r.project_id "
            "AND p.message_id=t.turn_id AND p.stream_id=t.turn_id",
            (cid, tid, tenant, owner),
        ).fetchone()
        if (not row or row["origin"] != "interactive" or row["request_state"] != "completed"
                or row["run_state"] != "completed" or not row["response_json"] or not row["provider_response"]
                or int(row["terminal_result_revision"] or 0) < 1):
            raise CoordinatorConflict("Completed application turn result unavailable")
        source_id = original_source_turn(conn, cid, tid)
        source = conn.execute("SELECT blocker,restore_hold FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                              (cid, source_id)).fetchone()
        represented = conn.execute(
            "SELECT 1 FROM coordinator_goals WHERE conversation_id=? AND (run_id=? OR "
            "(source_turn_id=? AND (COALESCE(dispatch_json,'')<>'' OR COALESCE(work_ref,'')<>'' "
            "OR COALESCE(worker_id,'')<>'' OR COALESCE(run_id,'')<>''))) LIMIT 1",
            (cid, row["run_id"], source_id),
        ).fetchone() is not None
    try:
        response = json.loads(row["response_json"])
        saved = json.loads(row["provider_response"])
        choices = response["choices"]
        message = choices[0]["message"]
        text = message["content"]
        if (canonical(response) != canonical(saved) or len(choices) != 1
                or message["role"] != "assistant" or not isinstance(text, str)
                or response.get("id") != row["request_id"]
                or response.get("glasshive", {}).get("request_id") != row["request_id"]):
            raise ValueError("Canonical turn result mismatch")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise CoordinatorConflict("Canonical turn result unavailable") from exc
    publisher = {"owner_id": owner, "conversation_id": cid, "agent_id": "coordinator",
                 "request_id": row["request_id"], "message_id": row["message_id"],
                 "stream_id": row["stream_id"], "run_id": row["run_id"],
                 "attempt_id": str(row["active_attempt_id"] or "")}
    files = response.get("glasshive", {}).get("output_files")
    if files and (not isinstance(files, dict) or any(files.get(key) != value for key, value in publisher.items())):
        raise CoordinatorScopeError("Native turn result files target unrelated output")
    held = bool(conversation.get("restore_hold") or row["restore_hold"] or row["request_hold"] or source["restore_hold"])
    intent = "cancelled" if row["blocker"] == "cancelled" or source["blocker"] == "cancelled" else "accepted"
    return {"turn_id": tid, "source_turn_id": source_id, "request_id": row["request_id"],
            "run_id": row["run_id"], "state": "completed", "result_revision": int(row["terminal_result_revision"]),
            "intent_state": intent, "restore_hold": held, "superseded": represented,
            "presentation_eligible": intent == "accepted" and not held and not represented,
            "publisher": publisher, **({"output_files": files} if files else {}),
            "output_text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def turn_result(core, tenant, owner, cid, tid, request_id, run_id, offset=0, max_chars=12000):
    if offset < 0 or not 1 <= max_chars <= 64000:
        raise CoordinatorConflict("Invalid turn result page")
    result = completed_turn_result(core, tenant, owner, cid, tid)
    if not request_id or not run_id or result["request_id"] != request_id or result["run_id"] != run_id:
        raise CoordinatorConflict("Application turn result identity changed")
    text = result["output_text"]
    if offset > len(text):
        raise CoordinatorConflict("Turn result offset exceeds source")
    end = min(len(text), offset + max_chars)
    return {**result, "output_text": text[offset:end], "total_chars": len(text), "offset": offset,
            "next_offset": end if end < len(text) else None}


def queue_turn_results(core, conversation):
    """Re-observe retained accepted turns through the existing maintenance/outbox owners."""
    if application(conversation) is None:
        return
    with core.store._connect() as conn:
        turns = conn.execute("SELECT turn_id FROM coordinator_turns WHERE conversation_id=? "
                             "AND origin='interactive' AND response_json<>''", (conversation["conversation_id"],)).fetchall()
    for turn in turns:
        try:
            result = completed_turn_result(core, conversation["tenant_id"], conversation["owner_id"],
                                           conversation["conversation_id"], turn["turn_id"])
        except (CoordinatorConflict, CoordinatorScopeError):
            continue
        if not result["presentation_eligible"]:
            continue
        data = {"turn_id": result["turn_id"], "request_id": result["request_id"], "run_id": result["run_id"],
                "result_revision": result["result_revision"], "result_digest": "sha256:" + result["sha256"]}
        notify(core, conversation, EVENTS[4], canonical(data), data)


def notification_fence(service, record):
    core = getattr(service, "coordinator", None)
    try:
        payload = json.loads(record["payload_json"])
        data = payload["data"]
        conversation = core._conversation(record["tenant_id"], record["owner_id"], data["conversation_id"])
        binding = application(conversation)
        if (binding is None or binding.destination_id != record["destination_id"]
                or binding.request_ref != data["request_ref"] or payload["event_id"] != record["callback_id"]
                or payload["type"] != record["event_type"]):
            return "source_changed"
        if conversation.get("restore_hold"):
            return "continuity_restore_hold"
        if record["event_type"] == EVENTS[0]:
            state = turn_status(core, record["tenant_id"], record["owner_id"], data["conversation_id"], data["turn_id"])
            return ("continuity_restore_hold" if state["state"] == "restore_held" else
                    "cancelled" if state["state"] == "cancelled" else "source_prepared" if state["request_id"] else "")
        if record["event_type"] == EVENTS[4]:
            result = turn_result(core, record["tenant_id"], record["owner_id"], data["conversation_id"],
                                 data["turn_id"], data["request_id"], data["run_id"], 0, 1)
            if result["restore_hold"]:
                return "continuity_restore_hold"
            if result["intent_state"] == "cancelled":
                return "cancelled"
            if (not result["presentation_eligible"] or result["result_revision"] != data["result_revision"]
                    or "sha256:" + result["sha256"] != data["result_digest"]):
                return "source_changed"
            return ""
        goal = core._goal(data["conversation_id"], data["goal_id"])
        if record["event_type"] == EVENTS[1] and dispatch_notification_ref(data["conversation_id"], goal) != record["callback_id"]:
            return "source_changed"
        if record["event_type"] == EVENTS[3]:
            details = action_input(core, record["tenant_id"], record["owner_id"], data["conversation_id"], data["goal_id"], data["action_key"])
            if details["action_digest"] != data["action_digest"] or details["run_id"] != data["run_id"]:
                return "source_changed"
            if details["state"] == "completed":
                return "source_prepared"
            if goal["intent_state"] == "cancelled" or goal["restore_hold"]:
                return "cancelled" if goal["intent_state"] == "cancelled" else "continuity_restore_hold"
            current = core.goal_snapshot(record["tenant_id"],record["owner_id"],data["conversation_id"],data["goal_id"])
            if current.get("control_run_id",current["run_id"]) == data["run_id"]:
                return ""
            with core.store._connect() as conn:
                action_row=conn.execute("SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",(data['conversation_id'],data['goal_id'],data['action_key'])).fetchone()
            return "" if action_row and action_needs_context(core,action_row) else "source_changed"
        if record["event_type"] == EVENTS[2]:
            with core.store._connect() as conn:
                run = core._owned_goal_result_run(conn, record["tenant_id"], record["owner_id"],
                    data["conversation_id"], data["goal_id"], data["run_id"])
            if (not run or goal["run_id"] != data["run_id"]
                    or run["state"] not in {"completed", "failed", "cancelled", "interrupted"}
                    or int(run.get("terminal_result_revision") or 0) != data["result_revision"]
                    or "sha256:" + hashlib.sha256(str(run.get("output_text") or "").encode()).hexdigest() != data["result_digest"]):
                return "source_changed"
            return ""
        if goal["intent_state"] == "cancelled":
            return "cancelled"
        from .coordinator import Dispatch
        if goal["restore_hold"] or goal["application_dispatch_digest"] != data["dispatch_digest"] or dispatch_digest(data["conversation_id"], Dispatch.model_validate_json(goal["dispatch_json"])) != data["dispatch_digest"]:
            return "source_changed"
        return "source_prepared" if goal["run_id"] else ""
    except (KeyError, TypeError, ValueError, AttributeError, CoordinatorScopeError):
        return "source_unavailable"


def finish_notification(store, record, *, state, error="", timings=None, http_status=None,
                        retryable=False, attempt_started=True):
    stamp = datetime.now(UTC)
    attempts = int(record["attempts"]) + int(attempt_started)
    expires = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM callback_outbox WHERE callback_id=? AND status='delivering' "
                               "AND delivery_lease_token=? AND delivery_generation=?",
                               (record["callback_id"], record["delivery_lease_token"], record["delivery_generation"])).fetchone()
        if current is None:
            return False
        if record["event_type"] == EVENTS[1]:
            source = json.loads(record["payload_json"])["data"]
            goal = conn.execute("SELECT * FROM coordinator_goals WHERE conversation_id=? AND goal_id=?",
                (source["conversation_id"], source["goal_id"])).fetchone()
            if not goal or dispatch_notification_ref(source["conversation_id"], goal) != record["callback_id"]:
                state, error, retryable = "superseded", "source_changed", False
        uncertain = state == "unacknowledged" or current["last_error"] == "delivery_unacknowledged"
        pending = (retryable and stamp < expires and not current["cancel_requested_at"]
                   and not record.get("_destination_revoked") and not record.get("_source_withdrawn"))
        status = "pending" if pending else "http_accepted" if state == "accepted" else state
        if uncertain and state not in {"accepted", "superseded"} and not pending:
            status = "unacknowledged"
        next_at = min(stamp + timedelta(seconds=min(300, 2 ** min(attempts, 8))), expires).isoformat() if pending else None
        changed = conn.execute("UPDATE callback_outbox SET status=?,attempts=?,last_error=?,updated_at=?,"
                               "http_accepted_at=?,next_attempt_at=?,delivery_lease_token='',delivery_lease_expires_at=NULL,"
                               "timings_json=?,http_status=? WHERE callback_id=? AND status='delivering' "
                               "AND delivery_lease_token=? AND delivery_generation=?",
                               (status, attempts, "delivery_unacknowledged" if uncertain and state != "accepted" else error,
                                stamp.isoformat(), stamp.isoformat() if state == "accepted" else None, next_at,
                                canonical(timings or {}), http_status, record["callback_id"],
                                record["delivery_lease_token"], record["delivery_generation"])).rowcount
        if changed and not pending and status not in {"http_accepted", "superseded"}:
            data = json.loads(record["payload_json"])["data"]
            table, key = (("coordinator_turns", "turn_id") if record["event_type"] in {EVENTS[0], EVENTS[4]}
                          else ("coordinator_goals", "goal_id"))
            conn.execute(f"UPDATE {table} SET blocker=? WHERE conversation_id=? AND {key}=? "
                         "AND blocker<>'cancelled'",
                         ("application_delivery_" + status, data["conversation_id"], data[key]))
    return changed == 1


def goal_action_binding(core, tenant, owner, cid, gid, run_id, payload, payload_digest, reserve):
    """Fence an ordinary rich work action with the existing native goal intent."""
    from .coordinator import Control
    conversation = core._conversation(tenant, owner, cid)
    if application(conversation) is None:
        raise CoordinatorScopeError("Application work actions are not configured")
    core._assert_not_restore_held(conversation)
    goal = core.goal_snapshot(tenant, owner, cid, gid)
    if not goal.get("work_ref"):
        raise CoordinatorConflict("This goal has no delegated work action target")
    marker = {"work_ref": goal["work_ref"], "source_run_id": run_id, "payload_digest": payload_digest}
    control = Control(action=payload.action, run_id=run_id, message=payload.instruction or "",
                      idempotency_key=payload.idempotency_key)
    with core.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? "
                                "AND idempotency_key=?", (cid, gid, payload.idempotency_key)).fetchone()
        if previous:
            saved_marker = json.loads(previous["application_json"] or "{}")
            if saved_marker.get("control_digest") and not saved_marker.get("payload_digest"):
                retained = json.loads(previous["payload_json"])
                if (saved_marker.get("work_ref") != marker["work_ref"] or retained["action"] != payload.action
                        or retained["message"] != (payload.instruction or "")):
                    raise CoordinatorConflict("Prepared action changed its retained native control")
                if reserve:
                    saved_marker["payload_digest"] = payload_digest
                    saved_marker["action_request"] = payload.model_dump(mode="json",by_alias=True,exclude={"application_bootstrap_bundle_b64"},exclude_none=True)
                    conn.execute("UPDATE coordinator_actions SET application_json=? WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                                 (canonical(saved_marker),cid,gid,payload.idempotency_key))
                return saved_marker
            if any(saved_marker.get(key) != marker[key] for key in ("work_ref", "payload_digest")):
                raise CoordinatorConflict("Goal action idempotency key changed its exact target or request")
            # The exact durable operation owns its source run. Stable goal aliases may already
            # project a replacement when the same caller recovers the original receipt.
            return saved_marker
        if not reserve:
            raise CoordinatorConflict("Exact goal action receipt is unavailable")
        held = conn.execute("SELECT g.run_id,g.intent_state,g.restore_hold,c.restore_hold AS conversation_hold "
                            "FROM coordinator_goals g JOIN coordinator_conversations c USING(conversation_id) "
                            "WHERE g.conversation_id=? AND g.goal_id=?", (cid, gid)).fetchone()
        if held["restore_hold"] or held["conversation_hold"]:
            raise CoordinatorConflict("Goal is held for exact continuity resume")
        if not run_id or run_id != goal.get("control_run_id", held["run_id"]) or held["run_id"] != goal["run_id"]:
            raise CoordinatorConflict("Goal action targets a stale or unrelated run")
        if payload.action not in goal.get("actions", []):
            raise CoordinatorConflict("This action is unavailable for the exact work")
        if held["intent_state"] == "cancelled" and payload.action not in {"stop", "dismiss"}:
            raise CoordinatorConflict("Goal was already stopped")
        if payload.action == "stop":
            conn.execute("UPDATE coordinator_goals SET intent_state='cancelled' WHERE conversation_id=? AND goal_id=?",
                         (cid, gid))
        marker["action_request"] = payload.model_dump(mode="json",by_alias=True,exclude={"application_bootstrap_bundle_b64"},exclude_none=True)
        marker["control_digest"] = digest(control.model_dump())
        conn.execute("INSERT INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json,application_json) "
                     "VALUES(?,?,?,?,?)", (cid, gid, payload.idempotency_key, canonical(control.model_dump()), canonical(marker)))
    return marker


def finish_goal_action(core, tenant, owner, cid, gid, key, native_response):
    """Attach the existing ordinary action receipt before waking replacement work."""
    with core.store._connect() as conn:
        row = conn.execute("SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                           (cid, gid, key)).fetchone()
    if not row:
        raise CoordinatorConflict("Exact goal action reservation is unavailable")
    marker = json.loads(row["application_json"] or "{}")
    control = json.loads(row["payload_json"])
    if not marker:
        raise CoordinatorScopeError("Goal action is not application-bound")
    if row["response_json"]:
        return {**native_response, "goal": core.goal_snapshot(tenant, owner, cid, gid)}
    receipt = core.store.get_active_work_action_receipt(tenant_id=tenant, owner_id=owner,
        work_ref=marker["work_ref"], idempotency_key=key, action=control["action"], payload_digest=marker["payload_digest"])
    if receipt is None:
        raise CoordinatorConflict("Exact native action receipt is unavailable")
    if receipt["status"] != "completed" or native_response.get("confirmationPending"):
        return {**native_response, "goal": core.goal_snapshot(tenant, owner, cid, gid)}
    saved = json.loads(receipt["response_json"])
    effect_run_id = str(saved.get("runId") or marker["source_run_id"])
    run = core.store.get_run(effect_run_id)
    goal = core._goal(cid, gid)
    if not run or run["worker_id"] != goal["worker_id"] or run["tenant_id"] != tenant:
        raise CoordinatorScopeError("Native action receipt targets unrelated work")
    if marker.get("context_required") and not run.get("runtime_invoked_at") and run["state"] == "queued" and not core.service.has_run_local_bundle(effect_run_id):
        return {**native_response,"goal":core.goal_snapshot(tenant,owner,cid,gid)}
    if (effect_run_id != marker["source_run_id"] and control["action"] in {"queue", "message", "steer", "retry"}
            and saved.get("controlOutcome") != "terminal_won"):
        core._bind_goal_replacement(tenant, owner, cid, gid, goal, marker["source_run_id"], effect_run_id,
                                    preserve_newer_effect=True)
    result = {**native_response, "goal": core.goal_snapshot(tenant, owner, cid, gid)}
    with core.store._connect() as conn:
        committed = conn.execute("UPDATE coordinator_actions SET response_json=? WHERE conversation_id=? AND goal_id=? "
                     "AND idempotency_key=? AND response_json=''",
                     (canonical({**result, "run_id": effect_run_id}), cid, gid, key)).rowcount
    # Native lifecycle recovery may wake too, but its existing admission fence sees this binding first.
    if committed and control["action"] in {"queue", "message", "steer", "retry", "resume"}:
        current = core._goal(cid, gid)
        if current["intent_state"] == "accepted" and not current["restore_hold"]:
            core.service.start_assigned_run(goal["worker_id"])
    return result


def recover_goal_actions(core, tenant, owner, cid):
    with core.store._connect() as conn:
        # Transfer actionable receipts only. Completed history is not a lifetime budget;
        # an already-completed action remains relevant only while its queued effect may
        # need the invocation-local context restored after a process restart.
        rows = conn.execute("SELECT a.goal_id,a.idempotency_key,a.application_json FROM coordinator_actions a "
            "JOIN coordinator_goals g ON g.conversation_id=a.conversation_id AND g.goal_id=a.goal_id "
            "LEFT JOIN active_work_action_uses receipt ON receipt.tenant_id=? AND receipt.owner_id=? "
            "AND receipt.work_ref=json_extract(a.application_json,'$.work_ref') "
            "AND receipt.idempotency_key=a.idempotency_key "
            "AND receipt.payload_digest=json_extract(a.application_json,'$.payload_digest') AND receipt.status='completed' "
            "LEFT JOIN runs r ON r.run_id=COALESCE(NULLIF(json_extract(NULLIF(a.response_json,''),'$.run_id'),''),"
            "json_extract(NULLIF(receipt.response_json,''),'$.runId')) AND r.worker_id=g.worker_id AND r.tenant_id=? "
            "WHERE a.conversation_id=? AND a.application_json<>'{}' AND (a.response_json='' OR "
            "(json_extract(a.application_json,'$.context_required')=1 AND r.state='queued' "
            "AND COALESCE(r.runtime_invoked_at,'')='' AND g.intent_state='accepted' "
            "AND g.run_id IN (json_extract(a.application_json,'$.source_run_id'),r.run_id))) ORDER BY a.rowid",
            (tenant, owner, tenant, cid)).fetchall()
    for row in rows:
        marker = json.loads(row["application_json"])
        if marker.get("dispatch_retry"):
            from .coordinator import Control
            with core.store._connect() as conn:
                retry = conn.execute("SELECT payload_json,response_json FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                    (cid, row["goal_id"], row["idempotency_key"])).fetchone()
            if retry and not retry["response_json"]:
                retry_dispatch(core, tenant, owner, cid, row["goal_id"], Control.model_validate_json(retry["payload_json"]))
            continue
        with core.store._connect() as conn:
            action_row=conn.execute("SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",(cid,row['goal_id'],row['idempotency_key'])).fetchone()
        if action_needs_context(core,action_row):
            details=action_input(core,tenant,owner,cid,row['goal_id'],row['idempotency_key'])
            goal=core._goal(cid,row['goal_id'])
            if goal['intent_state']=='accepted' and not goal['restore_hold']:
                notify(core,core._conversation(tenant,owner,cid),EVENTS[3],
                       canonical([row['goal_id'],row['idempotency_key'],'context',getattr(core.service,'executor_id','recovery')]),
                       {key:details[key] for key in ('goal_id','action_key','action_digest','run_id','source_turn_id')})
            continue
        if not marker.get("payload_digest") and marker.get('control_digest'):
            details=action_input(core,tenant,owner,cid,row['goal_id'],row['idempotency_key'])
            notify(core,core._conversation(tenant,owner,cid),EVENTS[3],canonical([row['goal_id'],row['idempotency_key']]),
                   {key:details[key] for key in ('goal_id','action_key','action_digest','run_id','source_turn_id')})
            continue
        if action_row['response_json'] or not marker.get("payload_digest"):
            continue
        with core.store._connect() as conn:
            receipt = conn.execute("SELECT response_json FROM active_work_action_uses WHERE tenant_id=? AND owner_id=? "
                                   "AND work_ref=? AND idempotency_key=? AND payload_digest=? AND status='completed'",
                                   (tenant, owner, marker["work_ref"], row["idempotency_key"], marker["payload_digest"])).fetchone()
        if receipt:
            try:
                finish_goal_action(core, tenant, owner, cid, row["goal_id"], row["idempotency_key"], json.loads(receipt["response_json"]))
            except (CoordinatorConflict, CoordinatorScopeError, RuntimeError):
                continue


def self_goal_stop(core, tenant, owner, cid, gid, run_id, payload, *, receipt_only=False):
    """Reuse exact native controls for a pending or self-handled goal, without a fake mission."""
    from .coordinator import Control
    conversation = core._conversation(tenant, owner, cid)
    if application(conversation) is None:
        raise CoordinatorScopeError("Application work actions are not configured")
    goal = core.goal_snapshot(tenant, owner, cid, gid)
    rich_fields = (payload.native_input, payload.native_human_input, payload.file_inputs, payload.runtime_bundle,
                   payload.capability_reauthorization, payload.inherited_conversation, payload.source_context,
                   getattr(payload, "application_bootstrap_bundle_b64", None))
    replay = dispatch_retry_receipt(core, tenant, owner, cid, gid, payload)
    if replay:
        return replay
    if goal["work_ref"] or payload.action not in {"stop", "retry"} or payload.instruction or any(rich_fields):
        raise CoordinatorConflict("This goal supports only its exact native control")
    if payload.action == "retry" and run_id:
        raise CoordinatorConflict("A dispatch retry cannot target an existing run")
    control = Control(action=payload.action, run_id=run_id, idempotency_key=payload.idempotency_key)
    if receipt_only:
        with core.store._connect() as conn:
            row = conn.execute("SELECT payload_json,response_json FROM coordinator_actions WHERE conversation_id=? AND goal_id=? "
                               "AND idempotency_key=?", (cid, gid, payload.idempotency_key)).fetchone()
        if not row or row["payload_json"] != canonical(control.model_dump()):
            raise CoordinatorConflict("Exact goal Stop receipt is unavailable")
        snapshot = json.loads(row["response_json"]) if row["response_json"] else goal
        pending = not bool(row["response_json"])
    else:
        snapshot = core.control(tenant, owner, cid, gid, control)
        pending = bool(snapshot.get("confirmation_pending"))
    return {"action":payload.action, "status":"pending" if pending else "accepted", "state":snapshot["state"],
            "confirmationPending":pending, "idempotentReplay":receipt_only, "goal":snapshot}


def action_input(core,tenant,owner,cid,gid,key):
    conversation=core._conversation(tenant,owner,cid)
    binding=application(conversation)
    goal=core._goal(cid,gid)
    with core.store._connect() as conn:
        row=conn.execute('SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?',
                         (cid,gid,key)).fetchone()
    if binding and not row:
        with core.store._connect() as conn:
            row=conn.execute('SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?',
                (cid,gid,'coordinator-action-'+digest([cid,gid,key]))).fetchone()
    if not binding or not row:
        raise CoordinatorConflict('Accepted application action is unavailable')
    marker=json.loads(row['application_json'] or '{}')
    if not marker.get('control_digest'):
        raise CoordinatorScopeError('Action is not application-prepared')
    return {'conversation_id':cid,'request_ref':binding.request_ref,'goal_id':gid,'source_turn_id':goal['source_turn_id'],
            'run_id':marker['source_run_id'],'action_key':row['idempotency_key'],'action_digest':marker['control_digest'],
            'control':json.loads(row['payload_json']),
            **({'action_request':marker['action_request']} if marker.get('action_request') else {}),
            'state':'completed' if row['response_json'] and not action_needs_context(core,row) else 'preparing'}


def queue_action(core,tenant,owner,cid,gid,control):
    conversation=core._conversation(tenant,owner,cid)
    core._assert_not_restore_held(conversation)
    goal=core.goal_snapshot(tenant,owner,cid,gid)
    if control.action in {'queue','message','steer'} and not control.message.strip():
        raise CoordinatorConflict('Guidance is required for this action')
    serialized=canonical(control.model_dump())
    action_key='coordinator-action-'+digest([cid,gid,control.idempotency_key])
    with core.store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing=conn.execute('SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?',
                              (cid,gid,action_key)).fetchone()
        if existing and existing['payload_json']!=serialized:
            raise CoordinatorConflict('Control idempotency key changed content')
        latest=conn.execute('SELECT g.*,c.restore_hold AS conversation_hold FROM coordinator_goals g JOIN coordinator_conversations c USING(conversation_id) '
                            'WHERE g.conversation_id=? AND g.goal_id=?',(cid,gid)).fetchone()
        if not existing:
            if latest['restore_hold'] or latest['conversation_hold'] or latest['intent_state']=='cancelled':
                raise CoordinatorConflict('Goal is held or stopped')
            if control.run_id!=goal.get('control_run_id',latest['run_id']) or control.action not in goal.get('actions',[]):
                raise CoordinatorConflict('Control targets a stale or unavailable work action')
            marker={'work_ref':goal['work_ref'],'source_run_id':control.run_id,'control_digest':digest(control.model_dump())}
            conn.execute('INSERT INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json,application_json) VALUES(?,?,?,?,?)',
                         (cid,gid,action_key,serialized,canonical(marker)))
    details=action_input(core,tenant,owner,cid,gid,action_key)
    if details['state']!='completed':
        notify(core,conversation,EVENTS[3],canonical([gid,action_key]),
               {key:details[key] for key in ('goal_id','action_key','action_digest','run_id','source_turn_id')})
    return {**goal,'control':{'action':control.action,'idempotency_key':control.idempotency_key,'state':details['state']}}


def action_preflight(core,tenant,owner,cid,gid,run_id,payload,payload_digest):
    core._conversation(tenant,owner,cid)
    core._goal(cid,gid)
    with core.store._connect() as conn:
        row=conn.execute('SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?',
                         (cid,gid,payload.idempotency_key)).fetchone()
    if row is None:return {'state':'missing'}
    if not core._goal(cid,gid)['work_ref']:
        receipt=self_goal_stop(core,tenant,owner,cid,gid,run_id,payload,receipt_only=True)
        return {'state':'pending' if receipt['confirmationPending'] else 'completed'}
    marker=goal_action_binding(core,tenant,owner,cid,gid,run_id,payload,payload_digest,False)
    if not marker.get('payload_digest') or action_needs_context(core,row):return {'state':'unprepared'}
    receipt=core.store.get_active_work_action_receipt(tenant_id=tenant,owner_id=owner,work_ref=marker['work_ref'],
        idempotency_key=payload.idempotency_key,action=payload.action,payload_digest=payload_digest)
    if receipt and receipt['status']=='pending' and not core.service.active_work_action_claim_is_pending(receipt):
        return {'state':'unprepared'}
    return {'state':str(receipt['status']) if receipt else 'unprepared'}


def action_needs_context(core,row):
    marker=json.loads(row['application_json'] or '{}')
    if not marker.get('context_required'):return False
    response=json.loads(row['response_json'] or '{}')
    run_id=str(response.get('run_id') or '')
    if not run_id and marker.get('work_ref'):
        with core.store._connect() as conn:
            receipt=conn.execute("SELECT response_json FROM active_work_action_uses WHERE work_ref=? AND idempotency_key=? AND payload_digest=? AND status='completed'",
                (marker['work_ref'],row['idempotency_key'],marker.get('payload_digest',''))).fetchone()
        run_id=str(json.loads(receipt['response_json']).get('runId') or '') if receipt else ''
    run=core.store.get_run(run_id) if run_id else None
    if not run or run['state']!='queued' or run.get('runtime_invoked_at'):return False
    goal=core._goal(row['conversation_id'],row['goal_id'])
    if goal['intent_state']!='accepted' or goal['run_id'] not in {marker['source_run_id'],run_id}:return False
    return not core.service.has_run_local_bundle(run_id)


def validate_action_bundle(core,tenant,owner,cid,gid,bundle):
    """Validate fresh signed capability data against the unchanged native execution owner."""
    from .application_boundary import launch_authority_for
    from .service import derive_parallel_clean_room_bootstrap, is_parallel_clean_room_bootstrap
    goal=core._goal(cid,gid)
    worker=core.store.get_worker(goal['worker_id'])
    if not worker or (worker['tenant_id'],worker['owner_id'])!=(tenant,owner):
        raise CoordinatorScopeError('Action capability owner changed')
    if not bundle:
        raise CoordinatorScopeError('Application work action requires fresh signed invocation context')
    saved=core.service._bootstrap_bundle_for(worker) or {}
    from .service import application_invocation_bundle
    try:
        application_invocation_bundle(saved, bundle)
    except ValueError as exc:
        raise CoordinatorScopeError("Application action changed a Native execution binding") from exc
    # Existing host continuations keep their already-bound native placement and
    # authorization. Docker's new-launch clean-room validator is not a host gate.
    if worker.get("execution_mode") != "host" and is_parallel_clean_room_bootstrap(None,saved):
        derive_parallel_clean_room_bootstrap(None,bundle)
    authority=launch_authority_for(bundle)
    saved_authority=launch_authority_for(saved) or {}
    if authority:
        profile=worker['profile']+':'
        if (authority.get('execution_mode')!=worker['execution_mode'] or str(authority.get('worker_model') or '').removeprefix(profile)!=str(worker['model']).removeprefix(profile)):
            raise CoordinatorScopeError('Application action changed its native runtime identity')
        expected_effort=saved_authority.get('worker_reasoning_effort')
        if expected_effort is not None and authority.get('worker_reasoning_effort')!=expected_effort:
            raise CoordinatorScopeError('Application action changed its native reasoning effort')
    incoming_env=bundle.get('env') or {}
    saved_env=saved.get('env') or {}
    for key in ('WPR_CODEX_CLI_REASONING_EFFORT','WPR_CLAUDE_CODE_EFFORT','WPR_GROK_REASONING_EFFORT'):
        if key in incoming_env and key in saved_env and incoming_env[key]!=saved_env[key]:
            raise CoordinatorScopeError('Application action changed its native reasoning effort')
    for key in ('execution_policy','provider_model','capability_authorization','glasshive_capability_authorization'):
        if key in bundle and bundle[key]!=saved.get(key):
            raise CoordinatorScopeError('Application action changed its native runtime policy')
    return bundle


def stage_action_context(core,tenant,owner,cid,gid,key,bundle,response):
    """Keep the usable fresh grant in the existing invocation-local owner, never static config."""
    goal=core._goal(cid,gid)
    run_id=str(response.get('runId') or '')
    if not run_id or response.get('controlOutcome')=='terminal_won':return False
    run=core.store.get_run(run_id)
    if not run or run['worker_id']!=goal['worker_id'] or run['tenant_id']!=tenant:
        raise CoordinatorScopeError('Action context targets unrelated work')
    if run['state'] in {'completed','failed','cancelled','interrupted'} or run.get('runtime_invoked_at'):return False
    if not core.service.attach_run_local_bundle(run_id,bundle):
        raise CoordinatorConflict('Exact invocation context could not be attached')
    with core.store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current=conn.execute('SELECT application_json FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?',(cid,gid,key)).fetchone()
        marker=json.loads(current['application_json'])
        marker['context_required']=True
        conn.execute('UPDATE coordinator_actions SET application_json=? WHERE conversation_id=? AND goal_id=? AND idempotency_key=?',(canonical(marker),cid,gid,key))
        # This marker contains no credential or cognition. The existing runtime owner requires
        # a fresh in-memory bundle after a restart instead of falling back to old worker state.
        conn.execute("UPDATE runs SET runtime_bundle_json=json_set(COALESCE(NULLIF(runtime_bundle_json,''),'{}'), "
                     "'$.application_invocation_context_required',json('true')) WHERE run_id=? AND state='queued'",(run_id,))
    return True


def original_source_turn(conn,cid,tid):
    seen = set()
    while True:
        if tid in seen:raise CoordinatorConflict('Accepted retry source has a cycle')
        seen.add(tid)
        turn=conn.execute('SELECT origin,message,retry_of_turn_id FROM coordinator_turns WHERE conversation_id=? AND turn_id=?',(cid,tid)).fetchone()
        if not turn:raise CoordinatorConflict('Accepted source turn is unavailable')
        if not turn['retry_of_turn_id']:break
        tid=turn['retry_of_turn_id']
    if not turn:raise CoordinatorConflict('Accepted source turn is unavailable')
    if turn['origin']=='interactive':return tid
    try:items=json.loads(turn['message'])['items']
    except (KeyError,TypeError,ValueError) as exc:raise CoordinatorConflict('Result turn lacks retained goal-source evidence') from exc
    sources=set()
    for item in items:
        goal=conn.execute('SELECT source_turn_id FROM coordinator_goals WHERE conversation_id=? AND goal_id=?',(cid,item.get('goal_id'))).fetchone()
        if not goal:raise CoordinatorConflict('Result turn goal-source evidence is unavailable')
        sources.add(goal['source_turn_id'])
    if len(sources)!=1:raise CoordinatorConflict('Result turn must retain one exact original source group')
    return next(iter(sources))
