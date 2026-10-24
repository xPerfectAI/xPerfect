"""Typed targets for the existing recurrence store, clock and callback outbox.

This module has no application imports. It does not own another clock, queue,
work ledger or retry thread. The service calls these operations from its normal
scheduler and uses the existing callback claim/generation fence for delivery.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
import uuid

from .recurrence import normalize_recurrence_spec, first_occurrence_at
from .state_permissions import ensure_state_directory
from .control_plane import ControlPlaneConflict as ScheduleCreateConflict


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DestinationCreateConflict(ScheduleCreateConflict):
    def __init__(self, destination_id, changed_fields):
        super().__init__("Destination create identity conflicts with another request")
        self.destination_id = destination_id
        self.changed_fields = sorted(changed_fields)


def due_payload(*, definition_id, version, occurrence_id, scheduled_at, expires_at, tenant_id, owner_id, instruction, request_data):
    """The exact envelope used for admission sizing and immutable due snapshots."""
    return {"type": "schedule.due", "schedule_id": definition_id, "schedule_version": version,
        "occurrence_id": occurrence_id, "scheduled_at": scheduled_at, "expires_at": expires_at,
        "principal": {"tenant_id": tenant_id, "owner_id": owner_id}, "instruction": instruction,
        "request_data": request_data}


def validate_target_body(service, tenant, owner, target, instruction):
    if target.get("kind") != "webhook":
        return
    destination = service.webhook_destinations.get(tenant, owner, target["destination_id"])
    if not destination:
        raise ValueError("Webhook destination is unavailable")
    # IDs have fixed ASCII lengths. Reserve the largest SQLite positive version
    # and ISO UTC timestamps, using the same envelope builder as actual firing.
    payload = (target.get("request_data", {}) if destination["payload_mode"] == "raw" else
        due_payload(definition_id="rsd_" + "0" * 32, version=9223372036854775807,
            occurrence_id="occ_" + "0" * 32, scheduled_at="9999-12-31T23:59:59.999999+00:00",
            expires_at="9999-12-31T23:59:59.999999+00:00", tenant_id=tenant, owner_id=owner,
            instruction=instruction, request_data=target.get("request_data", {})))
    maximum = min(destination.get("max_body_bytes", 262144), service.schedule_webhook_transport.limits.request_bytes)
    if len(encoded(payload).encode("utf-8")) > maximum:
        raise ValueError("schedule_payload_too_large")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parsed(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return result.astimezone(timezone.utc)


def needs_nullable_migration(conn):
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(recurring_schedule_definitions)")}
    return bool(columns and columns["worker_id"]["notnull"])


def migrate_nullable_targets(conn):
    """SQLite's existing transactional table-rebuild pattern, preserving all rows.

    Called only on the schema migration connection with foreign keys disabled
    before BEGIN. Restore every index/trigger and check foreign keys before the
    owning initialization transaction commits.
    """
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        for table, nullable in (
            ("recurring_schedule_definitions", ("project_id", "worker_id")),
            ("recurring_schedule_occurrences", ("scheduled_run_id",)),
            ("callback_outbox", ("project_id", "worker_id")),
        ):
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row is None:
                continue
            sql = row["sql"]
            for column in nullable:
                sql = sql.replace(f"{column} TEXT NOT NULL", f"{column} TEXT")
            temporary = table + "_target_migration"
            import re
            sql = re.sub(r"(?i)^CREATE TABLE (?:IF NOT EXISTS )?[\"`\[]?" + table + r"[\"`\]]?", "CREATE TABLE " + temporary, sql, count=1)
            objects = conn.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,)).fetchall()
            conn.execute(sql)
            columns = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            names = ",".join('"'+c+'"' for c in columns)
            conn.execute(f"INSERT INTO {temporary} ({names}) SELECT {names} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
            for item in objects:
                if item["name"] not in {"callback_outbox_trace_insert", "callback_outbox_trace_update"}:
                    conn.execute(item["sql"])
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")


def ensure_target_columns(conn):
    additions = {
        "scheduled_runs": {"bootstrap_json": "TEXT NOT NULL DEFAULT '{}'", "definition_version": "INTEGER NOT NULL DEFAULT 1"},
        "recurring_schedule_definitions": {
            "target_kind": "TEXT NOT NULL DEFAULT 'worker'", "target_json": "TEXT NOT NULL DEFAULT '{}'",
            "version": "INTEGER NOT NULL DEFAULT 1", "create_key": "TEXT NOT NULL DEFAULT ''",
            "create_digest": "TEXT NOT NULL DEFAULT ''", "bootstrap_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "recurring_schedule_occurrences": {
            "target_kind": "TEXT NOT NULL DEFAULT 'worker'", "target_json": "TEXT NOT NULL DEFAULT '{}'",
            "definition_version": "INTEGER NOT NULL DEFAULT 1", "callback_id": "TEXT NOT NULL DEFAULT ''",
            "snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            "http_status": "INTEGER", "timings_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "callback_outbox": {
            "owner_id": "TEXT NOT NULL DEFAULT ''", "destination_id": "TEXT NOT NULL DEFAULT ''",
            "definition_id": "TEXT NOT NULL DEFAULT ''", "occurrence_id": "TEXT NOT NULL DEFAULT ''",
            "schedule_version": "INTEGER NOT NULL DEFAULT 0", "next_attempt_at": "TEXT",
            "expires_at": "TEXT", "cancel_requested_at": "TEXT", "timings_json": "TEXT NOT NULL DEFAULT '{}'",
            "http_status": "INTEGER",
        },
    }
    for table, fields in additions.items():
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in fields.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS recurring_create_identity ON recurring_schedule_definitions(tenant_id,owner_id,create_key) WHERE create_key<>''")
    conn.execute("CREATE INDEX IF NOT EXISTS callback_schedule_due ON callback_outbox(event_type,status,next_attempt_at)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS callback_schedule_snapshot_immutable
      BEFORE UPDATE ON callback_outbox WHEN OLD.event_type='schedule.due' AND (
        OLD.owner_id IS NOT NEW.owner_id OR OLD.destination_id IS NOT NEW.destination_id OR
        OLD.definition_id IS NOT NEW.definition_id OR OLD.occurrence_id IS NOT NEW.occurrence_id OR
        OLD.schedule_version IS NOT NEW.schedule_version OR OLD.payload_json IS NOT NEW.payload_json OR
        OLD.expires_at IS NOT NEW.expires_at)
      BEGIN SELECT RAISE(ABORT,'schedule delivery snapshot is immutable'); END""")


class Destinations:
    """Owner-scoped protected configuration, following native credential homes.

    URL and auth never enter schedule/outbox/history rows or public responses.
    URLs are immutable; credentials can rotate or be revoked for queued events.
    """
    def __init__(self, state_directory):
        self.root = Path(state_directory) / "webhook-destinations"
        ensure_state_directory(self.root)

    def _directory(self, tenant, owner):
        if not str(owner).strip():
            raise ValueError("An authenticated owner is required")
        result = self.root / hashlib.sha256(encoded([tenant, owner]).encode()).hexdigest()
        ensure_state_directory(result)
        return result

    def _path(self, tenant, owner, destination_id):
        if not destination_id.startswith("whd_") or len(destination_id)!=36 or not all(c in "0123456789abcdef" for c in destination_id[4:]):
            raise ValueError("Invalid destination identity")
        return self._directory(tenant, owner) / (destination_id+".json")

    @contextmanager
    def _locked(self, tenant, owner):
        import fcntl
        directory = self._directory(tenant, owner)
        descriptor = os.open(directory/".lock", os.O_CREAT|os.O_RDWR|getattr(os,"O_NOFOLLOW",0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def public(value):
        result = {k:value[k] for k in ("destination_id","name","version","enabled","payload_mode","created_at","updated_at","max_body_bytes")}
        result["url_sha256"] = hashlib.sha256(value["url"].encode("utf-8")).hexdigest()
        return result

    def get(self, tenant, owner, destination_id):
        path = self._path(tenant, owner, destination_id)
        try:
            fd = os.open(path, os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd,"r") as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
                raise PermissionError("Destination credential storage is not private")
            value=json.load(f)
        if value.get("tenant_id")!=tenant or value.get("owner_id")!=owner:
            raise PermissionError("Destination scope mismatch")
        value.setdefault("max_body_bytes", 262144)
        return value

    def _write(self, tenant, owner, value):
        path=self._path(tenant,owner,value["destination_id"])
        temporary=path.with_suffix("."+uuid.uuid4().hex+".tmp")
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,"w") as f:
                f.write(encoded(value));f.flush();os.fsync(f.fileno())
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, tenant, owner, *, name, url, secret=None, headers=None, payload_mode="envelope", idempotency_key=None, max_body_bytes=262144):
        from .schedule_webhooks import validate_destination_config
        # DNS and egress authority are checked again against operator policy at send.
        validate_destination_config(url, headers=headers or {}, secret=secret)
        if idempotency_key is not None and not 8 <= len(idempotency_key) <= 128:
            raise ValueError("Invalid destination create identity")
        if type(max_body_bytes) is not int or not 1024 <= max_body_bytes <= 262144:
            raise ValueError("Invalid destination body limit")
        generated=not secret
        requested_secret = secret
        digest=hashlib.sha256(encoded([name,url,secret,headers or {},payload_mode,max_body_bytes]).encode()).hexdigest()
        destination_id="whd_"+(hashlib.sha256(encoded([tenant,owner,idempotency_key]).encode()).hexdigest()[:32] if idempotency_key else uuid.uuid4().hex)
        secret=secret or "whsec_"+base64.b64encode(secrets.token_bytes(32)).decode()
        from standardwebhooks.webhooks import Webhook
        try: Webhook(secret)
        except Exception: raise ValueError("Invalid signing credential") from None
        if payload_mode not in {"envelope","raw"} or not str(name).strip():
            raise ValueError("Invalid destination configuration")
        value={"destination_id":destination_id,"create_digest":digest,"tenant_id":tenant,"owner_id":owner,
               "name":str(name).strip()[:120],"url":url,"secret":secret,"headers":headers or {},
               "payload_mode":payload_mode,"max_body_bytes":max_body_bytes,"version":1,"enabled":True,"created_at":now_iso(),"updated_at":now_iso()}
        with self._locked(tenant,owner):
            existing=self.get(tenant,owner,destination_id)
            if existing:
                requested = {"name": str(name).strip()[:120], "url": url, "headers": headers or {},
                    "payload_mode": payload_mode, "max_body_bytes": max_body_bytes}
                if requested_secret is not None:
                    requested["secret"] = requested_secret
                changed = [key for key, item in requested.items() if existing.get(key) != item]
                if changed:
                    raise DestinationCreateConflict(destination_id, changed)
                result=self.public(existing)
                if generated:result["secret_once"]=existing["secret"]
                return result
            if len(list(self._directory(tenant,owner).glob("whd_*.json")))>=128:
                raise ValueError("Owner destination capacity is full")
            self._write(tenant,owner,value)
        result=self.public(value)
        if generated: result["secret_once"]=secret
        return result

    def list(self,tenant,owner):
        result=[]
        for path in sorted(self._directory(tenant,owner).glob("whd_*.json")):
            value=self.get(tenant,owner,path.stem)
            if value is not None:result.append(self.public(value))
        return result

    def update(self,tenant,owner,destination_id,**updates):
        with self._locked(tenant,owner):
            value=self.get(tenant,owner,destination_id)
            if value is None:return None
            for key,item in updates.items():
                if item is None:continue
                if key not in {"secret","headers","enabled","name","max_body_bytes"}:raise ValueError("Destination URL is immutable")
                if key == "max_body_bytes" and (type(item) is not int or not 1024 <= item <= 262144):
                    raise ValueError("Invalid destination body limit")
                if key=="secret":
                    from .schedule_webhooks import validate_destination_config
                    validate_destination_config(value["url"], secret=item)
                if key=="headers":
                    from .schedule_webhooks import validate_destination_config
                    validate_destination_config(value["url"], headers=item)
                value[key]=item
            value.update(version=value["version"]+1,updated_at=now_iso())
            self._write(tenant,owner,value)
        return self.public(value)


def definition_row(store,row):
    value=store._recurring_definition_row(row)
    if value:
        value["target"]=value.get("target") or json.loads(value.pop("target_json","{}"))
        if not value["target"]:value["target"]={"kind":"worker","worker_id":value.get("worker_id")}
        for key in ("bootstrap_json","create_digest","create_key"):value.pop(key,None)
    return value


def _authority(conn,tenant,owner):
    row=conn.execute("SELECT enabled FROM schedule_principal_authority WHERE tenant_id=? AND owner_id=?",(tenant,owner)).fetchone()
    if row is None or not row["enabled"]:raise ValueError("Scheduled principal authority is disabled or missing")


def _target(service,tenant,owner,target):
    if target.get("kind")=="webhook":
        dest=service.webhook_destinations.get(tenant,owner,str(target.get("destination_id") or ""))
        if not dest or not dest["enabled"]:raise ValueError("Webhook destination is unavailable")
        if not isinstance(target.get("request_data",{}),dict) or len(encoded(target).encode())>65536:
            raise ValueError("Webhook request data must be a bounded object")
        return None
    if target.get("kind")!="worker":raise ValueError("Unsupported schedule target")
    worker=service.require_worker(str(target.get("worker_id") or ""))
    if (worker.get("tenant_id") or "local",worker.get("owner_id"))!=(tenant,owner):raise PermissionError("Schedule target scope mismatch")
    service._ensure_execution_allowed(worker)
    return worker


def create_definition(service,*,tenant_id,owner_id,target,idempotency_key,instruction="",bootstrap_bundle=None,**values):
    from .models import CreateTargetScheduleRequest
    request = CreateTargetScheduleRequest.model_validate({"target":target,"instruction":instruction,"idempotency_key":idempotency_key,"bootstrap_bundle":bootstrap_bundle,**values})
    target=request.target.model_dump();instruction=request.instruction;bootstrap_bundle=request.bootstrap_bundle
    values=request.model_dump(exclude={"target","instruction","idempotency_key","bootstrap_bundle"})
    tenant=tenant_id or "local";owner=str(owner_id)
    if not 8<=len(idempotency_key)<=128:raise ValueError("A stable create idempotency key is required")
    requested={"target":target,"instruction":instruction,"bootstrap_bundle":bootstrap_bundle,**values}
    digest=hashlib.sha256(encoded(requested).encode()).hexdigest()
    store=service.store
    with store._connect() as conn:
        current=conn.execute("SELECT * FROM recurring_schedule_definitions WHERE tenant_id=? AND owner_id=? AND create_key=?",(tenant,owner,idempotency_key)).fetchone()
        if current:
            if current["create_digest"]!=digest:raise ScheduleCreateConflict("Schedule create idempotency key conflicts with another request")
            return definition_row(store,current)
    worker=_target(service,tenant,owner,target)
    validate_target_body(service,tenant,owner,target,instruction)
    if worker is None and bootstrap_bundle:raise ValueError("A webhook target takes request data, not worker bootstrap")
    if worker is not None and not instruction.strip():raise ValueError("A worker instruction is required")
    if bootstrap_bundle:
        # Persist original context and owner references, never a usable run grant.
        # Such grants are re-minted by the current native admission path at firing.
        carriers=[bootstrap_bundle.get(k) for k in ("callbacks","capability_broker","launch_authority","viventium_launch_authority")]
        for carrier in carriers:
            if isinstance(carrier,dict) and any(carrier.get(k) for k in ("token","secret","hmac_secret","bearer_token","capability_token")):
                raise ValueError("Schedule context requires durable capability references, not live credentials")
    first=values.pop("first_run_at",None)
    schedule_text=values.pop("schedule_text","")
    if first and values.get("recurrence_type")=="once":values["starts_at"]=values.get("starts_at") or first
    spec=normalize_recurrence_spec(**values)
    now=datetime.now(timezone.utc)
    if not spec.get("starts_at") and spec["recurrence_type"] in {"cron","rfc5545"}:spec["starts_at"]=now.isoformat()
    due=first_occurrence_at(spec,now=now,first_run_at=first)
    store.ensure_schedule_principal_authority(tenant_id=tenant,owner_id=owner)
    data={"definition_id":"rsd_"+uuid.uuid4().hex,"project_id":worker["project_id"] if worker else None,
          "worker_id":worker["worker_id"] if worker else None,"tenant_id":tenant,"owner_id":owner,
          "scheduler_owner":"native","instruction":instruction,"schedule_text":schedule_text,
          **spec,"next_run_at":due.isoformat(),"active":int(spec["enabled"]),"last_occurrence_at":None,
          "target_kind":target["kind"],"target_json":encoded(target),"version":1,
          "create_key":idempotency_key,"create_digest":digest,"bootstrap_json":encoded(bootstrap_bundle or {}),
          "created_at":now.isoformat(),"updated_at":now.isoformat()}
    data["enabled"]=int(data["enabled"])
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE");_authority(conn,tenant,owner)
        current=conn.execute("SELECT * FROM recurring_schedule_definitions WHERE tenant_id=? AND owner_id=? AND create_key=?",(tenant,owner,idempotency_key)).fetchone()
        if current:
            if current["create_digest"]!=digest:raise ScheduleCreateConflict("Schedule create idempotency key conflicts with another request")
            return definition_row(store,current)
        if worker:
            live=conn.execute("SELECT state FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",(worker["worker_id"],tenant,owner)).fetchone()
            if not live or live["state"] in {"terminating","terminated","termination_failed"}:raise ValueError("Scheduled workspace is closed")
        capacity=max(1,min(int(os.environ.get("WPR_SCHEDULE_LIMIT_PER_OWNER","1000")),100000))
        if conn.execute("SELECT COUNT(*) FROM recurring_schedule_definitions WHERE tenant_id=? AND owner_id=? AND retired_at IS NULL",(tenant,owner)).fetchone()[0]>=capacity:
            raise ValueError("Owner schedule capacity is full")
        keys=list(data);conn.execute("INSERT INTO recurring_schedule_definitions ("+",".join(keys)+") VALUES ("+",".join("?" for _ in keys)+")",[data[k] for k in keys])
        row=conn.execute("SELECT * FROM recurring_schedule_definitions WHERE definition_id=?",(data["definition_id"],)).fetchone()
    service._scheduler_wake_event.set()
    return definition_row(store,row)


def local_definition(store,definition_id,tenant,owner):
    with store._connect() as conn:
        row=conn.execute("SELECT * FROM recurring_schedule_definitions WHERE definition_id=? AND tenant_id=? AND owner_id=? AND target_json<>'{}'",(definition_id,tenant or "local",owner)).fetchone()
    return definition_row(store,row)


def change_definition(service,definition_id,tenant,owner,updates,*,retire=False):
    store=service.store;current=local_definition(store,definition_id,tenant,owner)
    if not current:return None
    if current.get("retired_at"):raise ValueError("Retired schedule cannot be changed")
    expected_version = updates.get("expected_version")
    if expected_version is not None and (type(expected_version) is not int or expected_version != current["version"]):
        raise ScheduleCreateConflict("Schedule version changed; reload before editing")
    target=updates.get("target",current["target"])
    stopping = retire or (updates.get("enabled") is False and "target" not in updates)
    worker=_target(service,tenant,owner,target) if not stopping else None
    if not stopping:
        validate_target_body(service,tenant,owner,target,updates.get("instruction",current["instruction"]))
    fields={k:current[k] for k in ("recurrence_type","interval_seconds","local_time","timezone_name","dst_policy","cron_expression","rrule","starts_at","ends_at","enabled","overlap_policy","misfire_grace_seconds","catch_up_policy","max_catch_up_occurrences","jitter_seconds")}
    fields.update({k:v for k,v in updates.items() if k in fields})
    spec=normalize_recurrence_spec(**fields)
    now=datetime.now(timezone.utc)
    timing_changed=any(k in updates for k in fields if k!="enabled")
    next_at=first_occurrence_at(spec,now=now).isoformat() if timing_changed or (updates.get("enabled") is True and not current["active"]) else current["next_run_at"]
    changes={**spec,"target_kind":target["kind"],"target_json":encoded(target),"project_id":current["project_id"] if stopping else worker["project_id"] if worker else None,"worker_id":current["worker_id"] if stopping else worker["worker_id"] if worker else None,"instruction":updates.get("instruction",current["instruction"]),"schedule_text":updates.get("schedule_text",current["schedule_text"]),"next_run_at":next_at,"enabled":int(spec["enabled"] and not retire),"active":int(spec["enabled"] and not retire),"updated_at":now.isoformat(),"version":current["version"]+1}
    if retire:changes["retired_at"]=now.isoformat()
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not retire and changes["enabled"]:_authority(conn,tenant,owner)
        count=conn.execute("UPDATE recurring_schedule_definitions SET "+",".join(k+"=?" for k in changes)+" WHERE definition_id=? AND tenant_id=? AND owner_id=? AND version=?",[*changes.values(),definition_id,tenant,owner,current["version"]]).rowcount
        if count!=1:raise ValueError("Schedule changed concurrently; reload before editing")
        _cancel_pending(conn,definition_id,now.isoformat(),"cancelled" if retire or not changes["enabled"] else "superseded")
        row=conn.execute("SELECT * FROM recurring_schedule_definitions WHERE definition_id=?",(definition_id,)).fetchone()
    service._scheduler_wake_event.set()
    return definition_row(store,row)


def _cancel_pending(conn,definition_id,now,reason):
    conn.execute("UPDATE scheduled_runs SET state='cancelled',last_error=?,updated_at=? WHERE state='pending' AND schedule_id IN (SELECT scheduled_run_id FROM recurring_schedule_occurrences WHERE definition_id=?)",(reason,now,definition_id))
    conn.execute("UPDATE recurring_schedule_occurrences SET state=?,outcome=?,terminal_at=? WHERE definition_id=? AND terminal_at IS NULL AND target_kind='worker' AND scheduled_run_id IN (SELECT schedule_id FROM scheduled_runs WHERE state='cancelled' AND queued_run_id IS NULL)",(reason,reason,now,definition_id))
    conn.execute("UPDATE callback_outbox SET status=CASE WHEN attempts>0 OR occurrence_id IN (SELECT occurrence_id FROM recurring_schedule_occurrences WHERE outcome='unacknowledged') THEN 'unacknowledged' ELSE ? END,last_error=?,updated_at=?,next_attempt_at=NULL WHERE event_type='schedule.due' AND definition_id=? AND status='pending'",(reason,reason,now,definition_id))
    conn.execute("UPDATE callback_outbox SET cancel_requested_at=? WHERE event_type='schedule.due' AND definition_id=? AND status='delivering'",(now,definition_id))
    conn.execute("UPDATE recurring_schedule_occurrences SET state=CASE WHEN attempt_count>0 OR outcome='unacknowledged' THEN 'unacknowledged' ELSE ? END,outcome=?,terminal_at=? WHERE definition_id=? AND terminal_at IS NULL AND target_kind='webhook' AND callback_id IN (SELECT callback_id FROM callback_outbox WHERE status IN (?,'unacknowledged'))",(reason,reason,now,definition_id,reason))


def manual_receipt(store, definition, key):
    """Recover an accepted owner-scoped command without admitting another send."""
    definition_id = definition["definition_id"]
    if definition["target"]["kind"] == "webhook":
        digest = hashlib.sha256(encoded([definition_id, "manual", key]).encode()).hexdigest()
        identity = "schedule:" + digest
    else:
        digest = hashlib.sha256(f"{definition_id}\0manual\0{key}".encode()).hexdigest()
        identity = f"recurrence-manual:{definition_id}:{digest}"
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM recurring_schedule_occurrences WHERE definition_id=? AND tenant_id=? AND owner_id=? AND idempotency_key=?", (definition_id, definition["tenant_id"], definition["owner_id"], identity)).fetchone()
        if row is None:
            return None
        if definition["target"]["kind"] == "webhook":
            return {**dict(row), "occurrence_created": False}
        scheduled = conn.execute("SELECT * FROM scheduled_runs WHERE schedule_id=?", (row["scheduled_run_id"],)).fetchone()
        return {**(dict(scheduled) if scheduled else {}), "occurrence_id": row["occurrence_id"], "idempotency_key": identity, "occurrence_created": False}


def materialize_webhook(store,definition,*,scheduled_for,detected_at,next_run_at,dispatch_at=None,state="pending",outcome="pending",deactivate=False,manual_key=None):
    tenant=definition["tenant_id"];owner=definition["owner_id"];definition_id=definition["definition_id"]
    identity=hashlib.sha256(encoded([definition_id,"manual" if manual_key else "due",manual_key or scheduled_for]).encode()).hexdigest()
    occurrence_id="occ_"+identity[:32];callback_id="cb_schedule_"+identity
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing=conn.execute("SELECT * FROM recurring_schedule_occurrences WHERE occurrence_id=?",(occurrence_id,)).fetchone()
        if existing:
            return {**dict(existing), "occurrence_created": False} if manual_key else dict(existing)
        row=conn.execute("SELECT * FROM recurring_schedule_definitions WHERE definition_id=? AND tenant_id=? AND owner_id=? AND retired_at IS NULL AND version=?",(definition_id,tenant,owner,definition["version"])).fetchone()
        if row is None or (not manual_key and (not row["active"] or row["next_run_at"]!=definition["next_run_at"])):return None
        _authority(conn,tenant,owner)
        target=json.loads(row["target_json"])
        expires=(parsed(scheduled_for)+timedelta(seconds=max(1,int(row["misfire_grace_seconds"])))).isoformat()
        payload=due_payload(definition_id=definition_id, version=row["version"], occurrence_id=occurrence_id, scheduled_at=scheduled_for, expires_at=expires, tenant_id=tenant, owner_id=owner, instruction=row["instruction"], request_data=target.get("request_data",{}))
        occurrence={"occurrence_id":occurrence_id,"definition_id":definition_id,"tenant_id":tenant,"owner_id":owner,"scheduled_for":scheduled_for,"detected_at":detected_at,"scheduled_run_id":None,"idempotency_key":"schedule:"+identity,"state":state,"outcome":outcome,"created_at":detected_at,"target_kind":"webhook","target_json":encoded(target),"definition_version":row["version"],"callback_id":callback_id,"snapshot_json":encoded(payload)}
        keys=list(occurrence);conn.execute("INSERT INTO recurring_schedule_occurrences ("+",".join(keys)+") VALUES ("+",".join("?" for _ in keys)+")",list(occurrence.values()))
        if state=="pending":
            conn.execute("""INSERT INTO callback_outbox(callback_id,project_id,worker_id,tenant_id,owner_id,run_id,event_type,url,payload_json,status,created_at,updated_at,destination_id,definition_id,occurrence_id,schedule_version,next_attempt_at,expires_at)
              VALUES(?,NULL,NULL,?,?,NULL,'schedule.due',?,?,'pending',?,?,?,?,?,?,?,?)""",(callback_id,tenant,owner,"destination://"+target["destination_id"],encoded(payload),detected_at,detected_at,target["destination_id"],definition_id,occurrence_id,row["version"],dispatch_at or detected_at,expires))
        if not manual_key:
            conn.execute("UPDATE recurring_schedule_definitions SET next_run_at=?,last_occurrence_at=?,active=CASE WHEN ? THEN 0 ELSE active END,enabled=CASE WHEN ? THEN 0 ELSE enabled END,updated_at=? WHERE definition_id=? AND next_run_at=? AND version=?",(next_run_at,scheduled_for,int(deactivate),int(deactivate),detected_at,definition_id,definition["next_run_at"],definition["version"]))
    return {**occurrence, "occurrence_created": True} if manual_key else occurrence


def due_callback_records(store,now,limit=32):
    # Fair round-robin ordering by owner, then due time. No owner can fill every
    # bounded batch ahead of another owner with one ready destination.
    with store._connect() as conn:
        rows=conn.execute("""SELECT * FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY tenant_id,owner_id ORDER BY next_attempt_at,created_at) AS owner_order
          FROM callback_outbox WHERE event_type='schedule.due' AND status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?))
          ORDER BY owner_order,next_attempt_at,callback_id LIMIT ?""",(now,limit)).fetchall()
    return [dict(r) for r in rows]


def next_wake(store,now,*,include_legacy=True):
    with store._connect() as conn:
        row=conn.execute("""SELECT MIN(value) FROM (
          SELECT MIN(next_run_at) AS value FROM recurring_schedule_definitions WHERE scheduler_owner='native' AND active=1 AND next_run_at>? AND (? OR create_key<>'')
          UNION ALL SELECT MIN(next_attempt_at) FROM callback_outbox WHERE event_type='schedule.due' AND status='pending' AND next_attempt_at>?)""",(now,include_legacy,now)).fetchone()
    return row[0] if row else None


def fence_callback(service,record):
    store=service.store
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT * FROM callback_outbox WHERE callback_id=? AND status='delivering' AND delivery_lease_token=? AND delivery_generation=?",(record["callback_id"],record["delivery_lease_token"],record["delivery_generation"])).fetchone()
        if row is None:return "superseded"
        if row["cancel_requested_at"]:return "cancelled"
        definition=conn.execute("SELECT version,retired_at FROM recurring_schedule_definitions WHERE definition_id=?",(row["definition_id"],)).fetchone()
        if not definition or definition["version"]!=row["schedule_version"] or definition["retired_at"]:return "superseded"
        try:_authority(conn,row["tenant_id"],row["owner_id"])
        except ValueError:return "principal_disabled"
    return ""


def finish_callback(store,record,*,state,error="",timings=None,http_status=None,retryable=False,attempt_started=True):
    now=datetime.now(timezone.utc);attempts=int(record["attempts"])+int(attempt_started)
    expired=now>=parsed(record["expires_at"])
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM callback_outbox WHERE callback_id=? AND status='delivering' AND delivery_lease_token=? AND delivery_generation=?", (record["callback_id"], record["delivery_lease_token"], record["delivery_generation"])).fetchone()
        if current is None:
            return False
        prior = conn.execute("SELECT state,outcome FROM recurring_schedule_occurrences WHERE occurrence_id=?", (record["occurrence_id"],)).fetchone()
        # A retry's rejection cannot establish what happened to an earlier lost reply.
        # Preserve that disposition in the existing occurrence until a positive ACK.
        uncertain = state == "unacknowledged" or bool(prior and "unacknowledged" in (prior["state"], prior["outcome"]))
        definition = conn.execute("SELECT version,retired_at FROM recurring_schedule_definitions WHERE definition_id=?", (record["definition_id"],)).fetchone()
        withdrawn = bool(current["cancel_requested_at"] or not definition or definition["retired_at"] or definition["version"]!=record["schedule_version"] or record.get("_destination_revoked"))
        try:
            _authority(conn,record["tenant_id"],record["owner_id"])
        except ValueError:
            withdrawn = True
        pending=retryable and not expired and not withdrawn
        # A cancelled clock cannot undo a sent request or a known receiver ACK.
        status="pending" if pending else "http_accepted" if state=="accepted" else state
        occurrence_state="pending" if pending else "accepted" if state=="accepted" else "unacknowledged" if uncertain else state
        if not pending and state != "accepted" and uncertain:
            status = "unacknowledged"
        occurrence_outcome = "accepted" if state=="accepted" else "unacknowledged" if pending and uncertain else error or occurrence_state
        delay=min(300,2**min(attempts,8))
        next_at=min(now+timedelta(seconds=delay),parsed(record["expires_at"])).isoformat() if pending else None
        changed=conn.execute("UPDATE callback_outbox SET status=?,attempts=?,last_error=?,updated_at=?,http_accepted_at=?,next_attempt_at=?,delivery_lease_token='',delivery_lease_expires_at=NULL,timings_json=?,http_status=? WHERE callback_id=? AND status='delivering' AND delivery_lease_token=? AND delivery_generation=?",(status,attempts,error,now.isoformat(),now.isoformat() if state=="accepted" else None,next_at,encoded(timings or {}),http_status,record["callback_id"],record["delivery_lease_token"],record["delivery_generation"])).rowcount
        if changed:
            conn.execute("UPDATE recurring_schedule_occurrences SET state=?,outcome=?,terminal_at=?,attempt_count=?,http_status=?,timings_json=? WHERE occurrence_id=?",(occurrence_state,occurrence_outcome,None if pending else now.isoformat(),attempts,http_status,encoded(timings or {}),record["occurrence_id"]))
    return changed==1


def deliver(service,record):
    started=time.perf_counter();timings={}
    attempt_started = False
    timings["executor_queue_ms"] = max(0, (started-record.get("_submitted_monotonic",started))*1000)
    timings["clock_lateness_ms"] = max(0, (parsed(record["created_at"])-parsed(json.loads(record["payload_json"])["scheduled_at"])).total_seconds()*1000)
    timings["outbox_wait_ms"] = max(0, (datetime.now(timezone.utc)-parsed(record["created_at"])).total_seconds()*1000)
    try:
        reason=fence_callback(service,record)
        if reason:
            finish_callback(service.store,record,state="unacknowledged" if record["attempts"] else "cancelled" if reason=="cancelled" else "rejected",error=reason,attempt_started=False);return
        destination=service.webhook_destinations.get(record["tenant_id"],record["owner_id"],record["destination_id"])
        if not destination or not destination["enabled"]:
            finish_callback(service.store,record,state="unacknowledged" if record["attempts"] else "rejected",error="destination_unavailable",attempt_started=False);return
        if datetime.now(timezone.utc)>=parsed(record["expires_at"]):
            finish_callback(service.store,record,state="unacknowledged" if record["attempts"] else "expired",error="delivery_expired",attempt_started=False);return
        payload=json.loads(record["payload_json"])
        body=encoded(payload["request_data"] if destination["payload_mode"]=="raw" else payload).encode()
        if len(body) > int(destination.get("max_body_bytes", 256*1024)):
            finish_callback(service.store,record,state="unacknowledged" if record["attempts"] else "rejected",error="request_too_large",attempt_started=False)
            return
        timings["executor_to_send_ms"]=(time.perf_counter()-started)*1000
        attempt_started = True
        outcome=service.schedule_webhook_transport.send(url=destination["url"],secret=destination["secret"],event_id=record["callback_id"],body=body,expires_at=parsed(record["expires_at"]),headers=destination.get("headers") or {})
        timings.update(outcome.timings_ms)
        current_destination=service.webhook_destinations.get(record["tenant_id"],record["owner_id"],record["destination_id"])
        record["_destination_revoked"] = not current_destination or not current_destination["enabled"]
        finish_callback(service.store,record,state=outcome.state,error=outcome.error_code or "",timings=timings,http_status=outcome.http_status,retryable=outcome.retryable)
    except Exception:
        # No exception text: HTTP/private configuration errors can contain URLs or credentials.
        finish_callback(service.store,record,state="unacknowledged",error="delivery_internal_error",retryable=True,timings=timings,attempt_started=attempt_started)
    finally:
        with service._schedule_transport_lock:
            service._schedule_transport_active.discard((record["tenant_id"],record["owner_id"],record["destination_id"],record["callback_id"]))
        service._scheduler_wake_event.set()


def dispatch_pending(service):
    now=now_iso()
    # Reclaim only expired sender leases; never infer a crashed owner from age alone.
    service.store.reclaim_stale_delivering_callbacks(stale_before=(datetime.now(timezone.utc)-timedelta(seconds=300)).isoformat(),limit=50)
    dispatched=[]
    for candidate in due_callback_records(service.store,now):
        key=(candidate["tenant_id"],candidate["owner_id"],candidate["destination_id"],candidate["callback_id"])
        with service._schedule_transport_lock:
            active=service._schedule_transport_active
            if len(active)>=service._schedule_transport_capacity:break
            if any(k[3]==key[3] for k in active) or sum(k[:2]==key[:2] for k in active)>=2 or sum(k[:3]==key[:3] for k in active)>=2:continue
            active.add(key)
        record=service.store.claim_pending_callback(candidate["callback_id"], lease_seconds=20)
        if not record:
            with service._schedule_transport_lock:service._schedule_transport_active.discard(key)
            continue
        record["_submitted_monotonic"] = time.perf_counter()
        try:service.schedule_transport_executor.submit(deliver,service,record)
        except Exception:
            with service._schedule_transport_lock:service._schedule_transport_active.discard(key)
            service.store.mark_callback_pending(record["callback_id"],lease_token=record["delivery_lease_token"],delivery_generation=record["delivery_generation"],attempts=0,payload_json=record["payload_json"],last_error="transport_executor_unavailable")
            raise
        dispatched.append(record["callback_id"])
    return dispatched
