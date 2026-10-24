"""Private saved references over existing Files and native bootstrap owners.

No credential store, package interpreter, or Fast Queen/attempt-context tool is added.
Ordinary knowledge uses the existing immutable upload and binding protocol.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import nullcontext, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .workspace_files import FileAdmissionError, pin_file_manifest


class LibraryError(FileAdmissionError):
    def __init__(self, message, status_code=409, *, code="library_conflict"):
        super().__init__(message, status_code, code=code)


class LibraryRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _ref(value):
    try:
        return LibraryRef.model_validate(value).model_dump()
    except Exception as exc:
        raise LibraryError("Select an exact saved item version", 422, code="library_reference_invalid") from exc


def _description_excerpt(text):
    """A bounded excerpt of declared text, never a generated claim about readiness."""
    clean = " ".join(str(text).split())
    sentence = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0]
    return sentence if len(sentence) <= 280 else sentence[:279].rsplit(" ", 1)[0] + "…"


def _skill_description(files):
    # Read only the bounded, declarative description field. This is not a YAML
    # evaluator or an interpretation of a Skill's body/instructions.
    for entry in sorted(files, key=lambda value: str(value.get("path", ""))):
        if Path(entry.get("path", "")).name != "SKILL.md":
            continue
        lines = str(entry.get("content", ""))[:16384].splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        try:
            end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        except StopIteration:
            continue
        for index in range(1, end):
            line = lines[index]
            if not line.startswith("description:"):
                continue
            value = line.partition(":")[2].strip()
            if value in {">", ">-", ">+", "|", "|-", "|+"}:
                parts = []
                for continuation in lines[index + 1:end]:
                    if continuation and not continuation[0].isspace():
                        break
                    parts.append(continuation.strip())
                value = " ".join(parts)
            elif value.startswith('"'):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    value = ""
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            else:
                value = value.partition(" #")[0]
            if isinstance(value, str) and value:
                return _description_excerpt(value)
    return ""


def _version_metadata(payload):
    keys={"name","kind","uploads","refs","profiles","source_uri","profile","library_id","catalog_version","content_hash","capture_source","description"}
    result={key:value for key,value in payload.items() if key in keys}
    if "files" in payload:
        result["files_count"]=len(payload["files"])
        result["paths"]=[entry["path"] for entry in payload["files"]]
    if "description" not in result and payload.get("kind") == "skill":
        result["description"] = _skill_description(payload.get("files", []))
    return result


_PUBLIC_RECEIPT_KEYS={"name","kind","uploads","loaded","binding_id","manifest","context_delivery",
    "reason","paths","projection","library_id","source_uri","grant_id","native_cleanup"}

_APPLICATION_SELECT = """SELECT a.*,v.version FROM personal_library_applications a
    LEFT JOIN personal_library_versions v ON v.version_id=a.version_id AND v.item_id=a.item_id
    JOIN personal_library_items i ON i.item_id=a.item_id
        AND i.tenant_id=a.tenant_id AND i.owner_id=a.owner_id"""


def _public_receipt(receipt):
    return {key:value for key,value in receipt.items() if key in _PUBLIC_RECEIPT_KEYS}


def ensure_personal_library_schema(conn):
    for statement in (
        """CREATE TABLE IF NOT EXISTS personal_library_items (
            item_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            name TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
            current_version_id TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS personal_library_versions (
            version_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, version INTEGER NOT NULL,
            payload_json TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(item_id,version), FOREIGN KEY(item_id) REFERENCES personal_library_items(item_id))""",
        """CREATE TABLE IF NOT EXISTS personal_library_requests (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, request_key TEXT NOT NULL,
            request_hash TEXT NOT NULL, item_id TEXT NOT NULL, version_id TEXT NOT NULL,
            PRIMARY KEY(tenant_id,owner_id,request_key))""",
        """CREATE TABLE IF NOT EXISTS personal_library_holders (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, holder_kind TEXT NOT NULL,
            holder_id TEXT NOT NULL, version_id TEXT NOT NULL,
            PRIMARY KEY(tenant_id,owner_id,holder_kind,holder_id,version_id))""",
        """CREATE TABLE IF NOT EXISTS personal_library_applications (
            application_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            worker_id TEXT NOT NULL, item_id TEXT NOT NULL, version_id TEXT NOT NULL,
            state TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at REAL NOT NULL,
            removed_at REAL, request_key TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS personal_library_apply_requests (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, worker_id TEXT NOT NULL,
            request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
            PRIMARY KEY(tenant_id,owner_id,worker_id,request_key))""",
        "CREATE INDEX IF NOT EXISTS personal_library_owner ON personal_library_items(tenant_id,owner_id,state,updated_at)",
    ):
        conn.execute(statement)
    if "result_json" not in {r[1] for r in conn.execute("PRAGMA table_info(personal_library_apply_requests)")}:
        conn.execute("ALTER TABLE personal_library_apply_requests ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'")
    if "undo_expires_at" not in {r[1] for r in conn.execute("PRAGMA table_info(personal_library_applications)")}:
        conn.execute("ALTER TABLE personal_library_applications ADD COLUMN undo_expires_at REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS personal_library_undo_expiry ON personal_library_applications(tenant_id,owner_id,undo_expires_at)")
    if "metadata_json" not in {r[1] for r in conn.execute("PRAGMA table_info(personal_library_versions)")}:
        conn.execute("ALTER TABLE personal_library_versions ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
    for row in conn.execute("SELECT version_id,payload_json FROM personal_library_versions WHERE metadata_json='{}'").fetchall():
        conn.execute("UPDATE personal_library_versions SET metadata_json=? WHERE version_id=?",(_json(_version_metadata(json.loads(row["payload_json"]))),row["version_id"]))
    columns={row[1] for row in conn.execute("PRAGMA table_info(personal_library_applications)")}
    for column in ("cleanup_state","public_receipt_json"):
        if column not in columns:conn.execute(f"ALTER TABLE personal_library_applications ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    for row in conn.execute("SELECT application_id,receipt_json FROM personal_library_applications WHERE public_receipt_json=''").fetchall():
        receipt=json.loads(row["receipt_json"])
        cleanup=receipt.get("removal_result",{}).get("native_cleanup")
        if cleanup:receipt["native_cleanup"]=cleanup
        conn.execute("UPDATE personal_library_applications SET public_receipt_json=?,cleanup_state=? WHERE application_id=?",(_json(_public_receipt(receipt)),"unconfirmed" if cleanup and cleanup.get("state")=="unconfirmed" else "",row["application_id"]))
    conn.execute("CREATE INDEX IF NOT EXISTS personal_library_application_listing ON personal_library_applications(tenant_id,owner_id,worker_id,removed_at,cleanup_state)")


def _skill_policy_snapshot(conn,tenant,owner,worker_id):
    """Read current launch fields and compact labels without parsing saved file bodies."""
    row=conn.execute("SELECT json_extract(bootstrap_bundle_json,'$.access_mode'),json_type(bootstrap_bundle_json,'$.provider_capabilities.native_tools'),json_extract(bootstrap_bundle_json,'$.library_selection'),execution_mode,profile,json_extract(bootstrap_bundle_json,'$.execution_policy'),json_type(bootstrap_bundle_json,'$.launch_authority'),json_type(bootstrap_bundle_json,'$.viventium_launch_authority') FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",(worker_id,tenant,owner)).fetchone()
    if row is None:return None
    policy={"access_mode":row[0],"provider_capabilities":{"native_tools":row[1]!="false"},"execution_mode":row[3],"profile":row[4],"execution_policy":row[5]}
    # Clean-room exclusion needs only the envelope type, never its private body.
    for field,kind in (("launch_authority",row[6]),("viventium_launch_authority",row[7])):
        if kind=="object":policy[field]={}
    return policy,json.loads(row[2] or "{}")


def sync_library_applications(conn,tenant,owner,worker_id,*,only_if_approved_changed=False):
    """Project private selections from their existing native grant authority."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_library_applications'").fetchone():
        return
    if "metadata_json" not in {row[1] for row in conn.execute("PRAGMA table_info(personal_library_versions)")} or "public_receipt_json" not in {row[1] for row in conn.execute("PRAGMA table_info(personal_library_applications)")}:
        ensure_personal_library_schema(conn)
    rows=conn.execute("SELECT a.application_id,a.item_id,a.version_id,a.state,a.public_receipt_json,v.version,v.metadata_json FROM personal_library_applications a JOIN personal_library_versions v ON v.version_id=a.version_id WHERE a.tenant_id=? AND a.owner_id=? AND a.worker_id=? AND a.removed_at IS NULL ORDER BY a.created_at,a.application_id",(tenant,owner,worker_id)).fetchall()
    snapshot=_skill_policy_snapshot(conn,tenant,owner,worker_id)
    if snapshot is None:return
    policy,previous_selection=snapshot
    has_grants=bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_capability_grants'").fetchone())
    refs,items,pending,uploads=[],[],[],[]
    changed=False
    for row in rows:
        payload=json.loads(row["metadata_json"])
        state=row["state"]
        if payload["kind"]=="skill" and state=="ready_for_next_start":
            from .profile_runtime import host_claude_skill_policy_reason
            if host_claude_skill_policy_reason(policy,execution_mode=policy["execution_mode"]):state="not_available"
            prior=next((item.get("state") for item in previous_selection.get("items",[]) if item.get("version_id")==row["version_id"]),None)
            changed=changed or prior!=state
        elif payload["kind"]=="skill" and state in {"needs_review","not_available"}:
            from .profile_runtime import host_claude_skill_policy_reason
            from .store import is_parallel_clean_room_bootstrap
            supported=(policy["profile"]=="claude-code" and "claude-code" in payload.get("profiles",[])
                       and all(Path(path).is_relative_to(".claude/skills") for path in payload.get("paths",[]))
                       and not is_parallel_clean_room_bootstrap(None,policy))
            reason=(host_claude_skill_policy_reason(policy,execution_mode=policy["execution_mode"])
                    if supported else "Not available for this worker type")
            state="not_available" if reason else "needs_review"
            receipt=json.loads(row["public_receipt_json"])
            receipt.update(reason=reason or "Review this exact worker and skill files before adding",loaded=False)
            encoded=_json(receipt)
            if row["state"]!=state or row["public_receipt_json"]!=encoded:
                private=json.loads(conn.execute("SELECT receipt_json FROM personal_library_applications WHERE application_id=?",(row["application_id"],)).fetchone()[0])
                private.update(receipt)
                conn.execute("UPDATE personal_library_applications SET state=?,receipt_json=?,public_receipt_json=? WHERE application_id=?",(state,_json(private),encoded,row["application_id"]))
                changed=True
        if payload["kind"]=="approved":
            grant=conn.execute("SELECT grant_id FROM workspace_capability_grants WHERE tenant_id=? AND owner_id=? AND worker_id=? AND library_id=? AND revoked_at IS NULL",(tenant,owner,worker_id,payload["library_id"])).fetchone() if has_grants else None
            state="enabled" if grant else "needs_review"
            receipt=json.loads(row["public_receipt_json"])
            if grant:receipt["grant_id"]=grant["grant_id"]
            else:receipt.pop("grant_id",None)
            receipt["reason"]="Enabled by this worker’s approved grant; session load is not confirmed" if grant else "Use the existing approved-item review"
            encoded=_json(receipt)
            if row["state"]!=state or row["public_receipt_json"]!=encoded:
                private=json.loads(conn.execute("SELECT receipt_json FROM personal_library_applications WHERE application_id=?",(row["application_id"],)).fetchone()[0])
                private.update(receipt)
                if not grant:private.pop("grant_id",None)
                conn.execute("UPDATE personal_library_applications SET state=?,receipt_json=?,public_receipt_json=? WHERE application_id=?",(state,_json(private),encoded,row["application_id"]))
                changed=True
        identity={"item_id":row["item_id"],"version_id":row["version_id"]}
        item={**identity,"name":payload["name"],"kind":payload["kind"],"version":row["version"],"state":state}
        if payload.get("source_uri"):item["source_uri"]=payload["source_uri"]
        refs.append(identity);items.append(item)
        uploads.extend(entry["upload_id"] for entry in payload.get("uploads",[]))
        if state in {"needs_setup","needs_review","not_available"}:
            pending.append({key:value for key,value in item.items() if key not in {"version","state"}}|{"status":state})
    if only_if_approved_changed and not changed:return
    from .library_context_metadata import canonical_library_selection
    current=conn.execute("SELECT bootstrap_bundle_json FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",(worker_id,tenant,owner)).fetchone()
    if current is None:return
    bundle=json.loads(current[0] or "{}")
    selected=canonical_library_selection({"refs":refs,"items":items,"pending":pending,"upload_ids":list(dict.fromkeys(uploads))})
    if bundle.get("library_selection")!=selected:
        bundle["library_selection"]=selected
        conn.execute("UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?",(_json(bundle),worker_id))


class PersonalLibrary:
    def __init__(self, service):
        self.service, self.files, self.store = service, service.files, service.store
        with self.store._connect() as conn:
            ensure_personal_library_schema(conn)

    def _item(self, conn, tenant, owner, item_id):
        row = conn.execute("SELECT * FROM personal_library_items WHERE item_id=? AND tenant_id=? AND owner_id=?",
                           (item_id, tenant, owner)).fetchone()
        if row is None:
            raise LibraryError("Saved item not found", 404, code="library_not_found")
        return dict(row)

    def _version(self, conn, tenant, owner, item_id, version_id=None, *, allow_deleted=False, metadata_only=False):
        item = self._item(conn, tenant, owner, item_id)
        version_id = version_id or item["current_version_id"]
        if item["state"] == "deleted" and not allow_deleted:
            raise LibraryError("Saved item was deleted", 404, code="library_not_found")
        body="metadata_json" if metadata_only else "payload_json"
        row = conn.execute(f"SELECT version_id,version,{body} AS body_json FROM personal_library_versions WHERE item_id=? AND version_id=?", (item_id, version_id)).fetchone()
        if row is None:
            raise LibraryError("Saved item version is unavailable", 404, code="library_version_missing")
        payload = json.loads(row["body_json"])
        return {**item, "name": payload.get("name", item["name"]), "version_id": row["version_id"],
                "version": row["version"], "payload": payload,
                "uploads": payload.get("uploads", []), "refs": payload.get("refs", [])}

    def _catalog_description(self, tenant, owner, item, metadata):
        """Fill a missing display projection once; exact saved material stays immutable.

        This runs only on an owner's Library browse, never on native context reads.
        Existing Skills use their header; notes use a bounded first-sentence excerpt.
        """
        if "description" in metadata or item["kind"] not in {"skill", "note"}:
            return metadata
        if item["kind"] == "skill":
            with self.store._connect() as conn:
                exact = self._version(conn, tenant, owner, item["item_id"], item["version_id"])
            description = _skill_description(exact["payload"].get("files", []))
        else:
            from .workspace_file_drafts import DraftDownload
            uploads = metadata.get("uploads", [])
            description = ""
            if uploads:
                entry = uploads[0]
                try:
                    reader = DraftDownload(self.files, tenant, owner, entry["upload_id"], revision=entry["revision"])
                    try:
                        raw = b"".join(reader.stream(end=min(reader.size, 4096) - 1)) if reader.size else b""
                        description = _description_excerpt(raw.decode("utf-8", errors="ignore"))
                    finally:
                        reader.close()
                except (FileAdmissionError, OSError):
                    # An unavailable source is not an empty note or new material.
                    return metadata
        projected = {**metadata, "description": description}
        with self.store._connect() as conn:
            conn.execute("UPDATE personal_library_versions SET metadata_json=? WHERE version_id=? AND item_id=? AND EXISTS (SELECT 1 FROM personal_library_items i WHERE i.item_id=personal_library_versions.item_id AND i.tenant_id=? AND i.owner_id=?)", (_json(projected), item["version_id"], item["item_id"], tenant, owner))
        return projected

    def list_items(self, tenant_id, owner_id, include_archived=False):
        self._expire_undo(tenant_id, owner_id)
        with self.store._connect() as conn:
            rows = conn.execute("SELECT i.*,v.version_id,v.version,v.metadata_json FROM personal_library_items i JOIN personal_library_versions v ON v.version_id=i.current_version_id WHERE i.tenant_id=? AND i.owner_id=? AND i.state!='deleted' "
                                + ("" if include_archived else "AND i.state='active' ") + "ORDER BY i.updated_at DESC,i.item_id", (tenant_id, owner_id)).fetchall()
        items=[]
        for row in rows:
            item=dict(row);payload=json.loads(item.pop("metadata_json"))
            payload=self._catalog_description(tenant_id,owner_id,item,payload)
            items.append({**item,"payload":payload,"uploads":payload.get("uploads",[]),"refs":payload.get("refs",[])})
        return {"items": items}

    def get_item(self, tenant_id, owner_id, item_id, version_id=None):
        self._expire_undo(tenant_id, owner_id)
        with self.store._connect() as conn:
            value = self._version(conn, tenant_id, owner_id, item_id, version_id)
            value["versions"] = [dict(row) for row in conn.execute(
                "SELECT version_id,version,created_at FROM personal_library_versions WHERE item_id=? ORDER BY version DESC", (item_id,))]
            value["holders"]=self._holders(conn,tenant_id,owner_id,item_id)
        if value["kind"] == "note":
            context = self.knowledge_context(tenant_id, owner_id, resolved={"items": [value], "upload_refs": value["uploads"]}, max_chars=1024 * 1024)
            value["payload"]["text"] = "".join(source.get("text", "") for source in context["sources"])
        return value

    def _holders(self,conn,tenant,owner,item_id):
        holders=[dict(row) for row in conn.execute("SELECT holder_kind,holder_id,version_id FROM personal_library_holders WHERE tenant_id=? AND owner_id=? AND version_id IN (SELECT version_id FROM personal_library_versions WHERE item_id=?) ORDER BY holder_kind,holder_id,version_id",(tenant,owner,item_id))]
        for holder in holders:
            kind,key=holder["holder_kind"],holder["holder_id"]
            label=None
            if kind=="bundle":
                row=conn.execute("SELECT i.name FROM personal_library_versions v JOIN personal_library_items i ON i.item_id=v.item_id WHERE v.version_id=? AND i.tenant_id=? AND i.owner_id=?",(key,tenant,owner)).fetchone()
                label=("Bundle: "+row[0]) if row else None
            elif kind=="application":
                row=conn.execute("SELECT w.name FROM personal_library_applications a JOIN workers w ON w.worker_id=a.worker_id WHERE a.application_id=? AND a.tenant_id=? AND a.owner_id=?",(key,tenant,owner)).fetchone()
                label=("Worker: "+row[0]) if row else None
            elif kind=="template" and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_templates'").fetchone():
                row=conn.execute("SELECT name FROM workspace_templates WHERE template_id=? AND tenant_id=? AND owner_id=?",(key,tenant,owner)).fetchone()
                label=("Template: "+row[0]) if row else None
            holder["label"]=label or {"coordinator_turn":"Accepted conversation turn","removal_undo":"Library Undo (24 hours)"}.get(kind,kind.replace("_"," ").capitalize())
        return holders

    @contextmanager
    def _generated_save(self,tenant,owner):
        # Compose note/capture with the existing Files lock and cancellation
        # owner, rather than leaving failed saves as charged ready drafts.
        from .workspace_file_mutations import _locked
        with _locked(self.files._owner_root(tenant,owner)):
            generated=[]
            try:
                yield generated
            except BaseException:
                for upload_id in generated:
                    try:
                        self.files.cancel(tenant,owner,upload_id)
                    except FileAdmissionError:
                        # A concurrent accepted binding's pin takes precedence.
                        pass
                raise

    def _upload_bytes(self, tenant, owner, name, size, chunks, request_key, generated=None):
        # This synchronous owner is called by sync endpoints/MCP worker threads.
        row = self.files.create_upload(tenant, owner, draft_id="library", name=name,
                                       size_bytes=size, idempotency_key=request_key)
        if row["state"] in {"cancelled","expired"}:
            row=self.files.create_upload(tenant,owner,draft_id="library",name=name,size_bytes=size,
                idempotency_key=request_key[:90]+"-retry-"+uuid.uuid4().hex[:24])
        if generated is not None:
            generated.append(row["upload_id"])
        async def stream():
            for chunk in chunks:
                yield chunk
        return asyncio.run(self.files.receive(tenant, owner, row["upload_id"], stream()))

    def _normalize(self, tenant, owner, payload, request_key, *, retained_from=None, generated=None):
        if not isinstance(payload, dict):
            raise LibraryError("Saved item must be an object", 422)
        try:
            value = json.loads(_json(payload))
        except (TypeError,ValueError) as exc:
            raise LibraryError("Saved material must be valid JSON",422) from exc
        if not isinstance(value.get("name"),str):
            raise LibraryError("Choose a name for this item",422)
        name = value["name"].strip()
        kind = str(value.get("kind") or "knowledge")
        if not name or len(name) > 120 or kind not in {"knowledge", "note", "bundle", "skill", "native", "approved"}:
            raise LibraryError("Choose a name and supported item type", 422, code="library_item_invalid")
        value["name"], value["kind"] = name, kind
        if "description" in value:
            description = value["description"]
            if not isinstance(description, str) or len(description) > 300:
                raise LibraryError("Use a short description up to 300 characters", 422)
            value["description"] = " ".join(description.split())
        if kind == "note":
            text = value.pop("text", value.pop("content", ""))
            if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
                raise LibraryError("Notes must be UTF-8 text up to 1 MiB", 413)
            safe_name = "note.txt"
            row = self._upload_bytes(tenant, owner, safe_name, len(text.encode()), [text.encode()], "lib-note-" + _hash([request_key, text])[:64],generated)
            value["uploads"] = self.files.manifest(tenant, owner, [row["upload_id"]], [row["revision"]])
        elif kind == "knowledge":
            ids = value.pop("upload_ids", None)
            revisions = value.pop("upload_revisions", value.pop("revisions", None))
            if ids is None:
                uploads = value.get("uploads") or []
                if not isinstance(uploads,list) or any(not isinstance(entry,dict) for entry in uploads):
                    raise LibraryError("Select ready file versions",422)
                ids, revisions = [u.get("upload_id") for u in uploads], [u.get("revision") for u in uploads]
            if not isinstance(ids,list) or not ids or len(ids)>128 or not isinstance(revisions,list) or len(revisions)!=len(ids) or any(not isinstance(entry,str) or not entry or len(entry)>256 for entry in ids+revisions):
                raise LibraryError("Select ready file versions", 422)
            value["uploads"] = self.files.manifest(tenant, owner, ids, revisions)
        elif kind == "bundle":
            refs = value.get("refs")
            if not isinstance(refs, list) or not refs or len(refs) > 128:
                raise LibraryError("Choose at least one item for this bundle", 422)
            value["refs"] = list({_json(_ref(r)): _ref(r) for r in refs}.values())
            # Existing Bundle versions retain their exact children, including an
            # item later removed from My Library. A label edit keeps that right.
            prior_refs = {_json(ref) for ref in (retained_from or {}).get("refs", [])}
            for ref in value["refs"]:
                if _json(ref) in prior_refs:
                    resolved = self.resolve_retained_refs(tenant, owner, [ref], "bundle", retained_from["version_id"])
                else:
                    resolved = self.resolve_refs(tenant, owner, [ref])
                if any(item["kind"] == "bundle" for item in resolved["selected_items"]):
                    raise LibraryError("Bundles contain items, not other bundles", 422)
            if not value["refs"]:
                raise LibraryError("Bundles contain items, not other bundles", 422)
        elif kind == "skill":
            from .library_registry import _validate_bundle, LibraryManifestError
            profiles = value.get("profiles") or ["claude-code"]
            if not isinstance(profiles, list) or not profiles or any(not isinstance(profile,str) for profile in profiles) or set(profiles) - {"claude-code", "codex-cli", "grok-build"}:
                raise LibraryError("Select supported native skill profiles", 422)
            files = value.get("files")
            try:
                _validate_bundle({"files": files}, profile="claude-code")
            except (LibraryManifestError, TypeError) as exc:
                raise LibraryError(str(exc), 422, code="library_skill_invalid") from exc
            if not files:
                raise LibraryError("Save the full skill package, including its instructions", 422)
            value["profiles"] = profiles
        elif kind == "native":
            source=value.get("source_uri")
            if not isinstance(source,str):
                raise LibraryError("Use a credential-free HTTPS or native package reference",422)
            source=source.strip()
            try:
                parsed=urlsplit(source)
                host=parsed.hostname
            except ValueError as exc:
                raise LibraryError("Use a valid package reference",422) from exc
            if not source or len(source)>2048 or any(char.isspace() or ord(char)<32 for char in source) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.scheme not in {"https","native"} or (parsed.scheme=="https" and not host):
                raise LibraryError("Use a credential-free HTTPS or native package reference", 422)
            value["source_uri"] = source
        elif kind == "approved":
            cp = self.service.control_plane_store
            if cp is None:
                raise LibraryError("Approved catalog is unavailable", 503)
            entries = cp.list_library()
            library_id = str(value.get("library_id") or "")
            found = next((item for item in entries if item.get("library_id") == library_id), None)
            if found is None:
                raise LibraryError("Approved item is unavailable", 404)
            value["library_id"] = library_id
            value["catalog_version"] = found.get("version")
            value["content_hash"] = found.get("content_hash")
        if len(_json(value).encode()) > 5 * 1024 * 1024:
            raise LibraryError("Saved item exceeds the size limit", 413)
        return value

    def _pin_version(self, conn, tenant, owner, version_id, payload, *, retained_from=None):
        if payload.get("uploads"):
            from .workspace_file_drafts import draft_revision
            for entry in payload["uploads"]:
                row = self.files._upload(conn, tenant, owner, entry["upload_id"])
                if draft_revision(row) != entry["revision"]:
                    raise LibraryError("The selected file changed before it was saved", code="library_version_conflict")
            pin_file_manifest(conn, kind="library", reference_id=version_id,
                              tenant_id=tenant, owner_id=owner, manifest=payload["uploads"])
        for ref in payload.get("refs", []):
            child = self._version(conn, tenant, owner, ref["item_id"], ref["version_id"], allow_deleted=True,metadata_only=True)
            if child["state"] == "deleted" and not (retained_from and conn.execute(
                "SELECT 1 FROM personal_library_holders WHERE tenant_id=? AND owner_id=? AND holder_kind='bundle' AND holder_id=? AND version_id=?",
                (tenant, owner, retained_from, child["version_id"])).fetchone()):
                raise LibraryError("Bundle item was deleted before it was saved", 404)
            conn.execute("INSERT OR IGNORE INTO personal_library_holders VALUES (?,?,?,?,?)",
                         (tenant, owner, "bundle", version_id, child["version_id"]))
            if child["uploads"]:
                pin_file_manifest(conn, kind="library_bundle", reference_id=version_id,
                                  tenant_id=tenant, owner_id=owner, manifest=child["uploads"])

    def _save_capacity(self, conn, tenant, owner, payload):
        limit = self.files._policy(conn, tenant, owner)["storage_limit_bytes"]
        if limit is not None:
            used, reserved = self.files._logical_usage(conn, tenant, owner)
            if used + reserved + len(_json(payload).encode()) > limit:
                raise LibraryError("Storage limit reached; remove unused material or increase your limit", 413, code="file_storage_limit")

    def create_item(self, tenant_id, owner_id, payload, idempotency_key):
        with self._generated_save(tenant_id,owner_id) as generated:
            return self._create_item(tenant_id,owner_id,payload,idempotency_key,generated)

    def _create_item(self,tenant_id,owner_id,payload,idempotency_key,generated):
        if not idempotency_key or len(idempotency_key) > 128:
            raise LibraryError("A save request key is required", 422)
        request_hash = _hash(payload)
        with self.store._connect() as conn:
            old = conn.execute("SELECT * FROM personal_library_requests WHERE tenant_id=? AND owner_id=? AND request_key=?", (tenant_id, owner_id, idempotency_key)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise LibraryError("This save request already used different material")
                return self._version(conn, tenant_id, owner_id, old["item_id"], old["version_id"])
        value = self._normalize(tenant_id, owner_id, payload, idempotency_key,generated=generated)
        now, item_id, version_id = time.time(), "pli_" + uuid.uuid4().hex, "plv_" + uuid.uuid4().hex
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM personal_library_requests WHERE tenant_id=? AND owner_id=? AND request_key=?", (tenant_id, owner_id, idempotency_key)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise LibraryError("This save request already used different material")
                return self._version(conn, tenant_id, owner_id, old["item_id"], old["version_id"])
            self._save_capacity(conn, tenant_id, owner_id, value)
            conn.execute("INSERT INTO personal_library_items VALUES (?,?,?,?,?,?,?,?,?)", (item_id, tenant_id, owner_id, value["name"], value["kind"], "active", version_id, now, now))
            conn.execute("INSERT INTO personal_library_versions (version_id,item_id,version,payload_json,created_at,metadata_json) VALUES (?,?,?,?,?,?)", (version_id, item_id, 1, _json(value), now,_json(_version_metadata(value))))
            self._pin_version(conn, tenant_id, owner_id, version_id, value)
            conn.execute("INSERT INTO personal_library_requests VALUES (?,?,?,?,?,?)", (tenant_id, owner_id, idempotency_key, request_hash, item_id, version_id))
            return self._version(conn, tenant_id, owner_id, item_id)

    def revise_item(self, tenant_id, owner_id, item_id, payload, expected_version_id):
        with self._generated_save(tenant_id,owner_id) as generated:
            return self._revise_item(tenant_id,owner_id,item_id,payload,expected_version_id,generated)

    def _revise_item(self,tenant_id,owner_id,item_id,payload,expected_version_id,generated):
        old = self.get_item(tenant_id, owner_id, item_id)
        if old["version_id"]!=expected_version_id:
            raise LibraryError("The saved item changed; reload before editing",code="library_version_conflict")
        if old["kind"] != payload.get("kind", old["kind"]):
            raise LibraryError("Make a new item to change its type", 422)
        merged = {**old["payload"], **payload}
        if "content" in payload and "text" not in payload:
            merged.pop("text", None)
        # Unchanged knowledge keeps canonical ready refs, not another upload.
        if old["kind"] == "note" and "text" not in payload and "content" not in payload:
            merged["kind"] = "knowledge"
            merged.pop("text", None)
        value = self._normalize(tenant_id, owner_id, merged, "revision-" + expected_version_id + _hash(payload)[:32], retained_from=old,generated=generated)
        value["kind"] = old["kind"]
        version_id, now = "plv_" + uuid.uuid4().hex, time.time()
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._item(conn, tenant_id, owner_id, item_id)
            if current["current_version_id"] != expected_version_id or current["state"] == "deleted":
                raise LibraryError("The saved item changed; reload before editing", code="library_version_conflict")
            self._save_capacity(conn, tenant_id, owner_id, value)
            number = conn.execute("SELECT MAX(version) FROM personal_library_versions WHERE item_id=?", (item_id,)).fetchone()[0] + 1
            conn.execute("INSERT INTO personal_library_versions (version_id,item_id,version,payload_json,created_at,metadata_json) VALUES (?,?,?,?,?,?)", (version_id, item_id, number, _json(value), now,_json(_version_metadata(value))))
            self._pin_version(conn, tenant_id, owner_id, version_id, value, retained_from=expected_version_id)
            conn.execute("UPDATE personal_library_items SET name=?,current_version_id=?,updated_at=? WHERE item_id=?", (value["name"], version_id, now, item_id))
            return self._version(conn, tenant_id, owner_id, item_id)

    def archive_item(self, tenant_id, owner_id, item_id, archived=True, expected_version_id=None):
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = self._item(conn, tenant_id, owner_id, item_id)
            if item["state"] == "deleted":
                raise LibraryError("Saved item was deleted", 404)
            if expected_version_id and item["current_version_id"] != expected_version_id:
                raise LibraryError("The saved item changed; reload before archiving")
            conn.execute("UPDATE personal_library_items SET state=?,updated_at=? WHERE item_id=?", ("archived" if archived else "active", time.time(), item_id))
            return self._version(conn, tenant_id, owner_id, item_id)

    def delete_item(self, tenant_id, owner_id, item_id, confirm=False):
        self._expire_undo(tenant_id, owner_id)
        if not confirm:
            raise LibraryError("Confirm deletion from My Library", 422, code="library_confirmation_required")
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._item(conn, tenant_id, owner_id, item_id)
            ids = [r[0] for r in conn.execute("SELECT version_id FROM personal_library_versions WHERE item_id=?", (item_id,))]
            holders = [dict(r) for r in conn.execute("SELECT holder_kind,holder_id,version_id FROM personal_library_holders WHERE tenant_id=? AND owner_id=? AND version_id IN (SELECT version_id FROM personal_library_versions WHERE item_id=?)", (tenant_id, owner_id, item_id))]
            conn.execute("UPDATE personal_library_items SET state='deleted',updated_at=? WHERE item_id=?", (time.time(), item_id))
            for vid in ids:
                conn.execute("DELETE FROM workspace_file_pins WHERE kind='library' AND reference_id=?", (vid,))
                conn.execute("DELETE FROM workspace_file_pins WHERE kind='library_bundle' AND reference_id=?", (vid,))
                conn.execute("DELETE FROM personal_library_holders WHERE holder_kind='bundle' AND holder_id=?", (vid,))
            self._prune_deleted_versions(conn, tenant_id, owner_id)
            return {"item_id": item_id, "state": "deleted", "retained_by": holders}

    def _prune_deleted_versions(self, conn, tenant_id, owner_id):
        # Keep tombstone identity and audit rows, but no unreferenced body forever.
        conn.execute("DELETE FROM personal_library_versions WHERE item_id IN (SELECT item_id FROM personal_library_items WHERE tenant_id=? AND owner_id=? AND state='deleted') AND NOT EXISTS (SELECT 1 FROM personal_library_holders h WHERE h.version_id=personal_library_versions.version_id)", (tenant_id, owner_id))

    def resolve_refs(self, tenant_id, owner_id, refs, *, holder_kind=None, holder_id=None):
        if not isinstance(refs, list) or len(refs) > 128:
            raise LibraryError("Choose a bounded list of item versions", 422)
        selected, expanded = [], {}
        with self.store._connect() as conn:
            for raw in refs:
                ref = _ref(raw)
                item = self._version(conn, tenant_id, owner_id, ref["item_id"], ref["version_id"], allow_deleted=True,metadata_only=True)
                if item["state"] == "deleted" and (not holder_kind or not holder_id or not conn.execute(
                    "SELECT 1 FROM personal_library_holders WHERE tenant_id=? AND owner_id=? AND version_id=? AND holder_kind=? AND holder_id=?",
                    (tenant_id, owner_id, item["version_id"], holder_kind, holder_id)).fetchone()):
                    raise LibraryError("Saved item was deleted", 404)
                selected.append(item)
                values = item["refs"] if item["kind"] == "bundle" else [ref]
                for value in values:
                    child = self._version(conn, tenant_id, owner_id, value["item_id"], value["version_id"], allow_deleted=True,metadata_only=True)
                    if child["state"] == "deleted":
                        allowed = conn.execute("SELECT 1 FROM personal_library_holders WHERE tenant_id=? AND owner_id=? AND version_id=? AND ((holder_kind='bundle' AND holder_id=?) OR (holder_kind=? AND holder_id=?))",
                            (tenant_id, owner_id, child["version_id"], item["version_id"], holder_kind or "", holder_id or "")).fetchone()
                        if not allowed:
                            raise LibraryError("A saved bundle item was deleted", 404)
                    if child["kind"] == "bundle":
                        raise LibraryError("Nested bundles are not supported", 422)
                    old = next((v for v in expanded.values() if v["item_id"] == child["item_id"]), None)
                    if old and old["version_id"] != child["version_id"]:
                        raise LibraryError("Choose one version of each saved item", code="library_version_conflict")
                    expanded[child["version_id"]] = child
        items, uploads, pending = list(expanded.values()), {}, []
        for item in items:
            if item["uploads"]:
                for entry in self.files.manifest(tenant_id, owner_id, [u["upload_id"] for u in item["uploads"]], [u["revision"] for u in item["uploads"]]):
                    uploads[entry["upload_id"]] = entry
            else:
                pending.append({"item_id": item["item_id"], "version_id": item["version_id"], "name": item["name"], "kind": item["kind"], "state": "needs_review" if item["kind"] in {"skill", "approved"} else "needs_setup", "payload": item["payload"]})
        refs_out = [{"item_id": i["item_id"], "version_id": i["version_id"]} for i in selected]
        return {"refs": refs_out, "items": items, "selected_items": selected, "upload_ids": list(uploads),
                "upload_revisions": [i["revision"] for i in uploads.values()],
                "upload_refs": [{"upload_id": i["upload_id"], "revision": i["revision"]} for i in uploads.values()],
                "pending": pending, "requirements": pending}

    def resolve_retained_refs(self, tenant_id, owner_id, refs, holder_kind, holder_id):
        return self.resolve_refs(tenant_id, owner_id, refs, holder_kind=holder_kind, holder_id=holder_id)

    @staticmethod
    def compact_selection(resolved):
        """Durable conversation selection carries identities, never saved bodies."""
        def compact(item):
            value={key:item[key] for key in ("item_id","version_id","name","kind","version","state","refs") if key in item}
            value["uploads"]=[{key:entry[key] for key in ("upload_id","revision")} for entry in item.get("uploads",[])]
            source=item.get("source_uri") or item.get("payload",{}).get("source_uri")
            if source:value["source_uri"]=source
            return value
        pending=[]
        for item in resolved.get("pending",[]):
            value={key:item[key] for key in ("item_id","version_id","name","kind","state","status","reason","code","profile","source_uri") if key in item}
            source=item.get("source_uri") or item.get("payload",{}).get("source_uri")
            if source:value["source_uri"]=source
            pending.append(value)
        return {"refs":resolved.get("refs",[]),"items":[compact(item) for item in resolved.get("items",[])],
            "selected_items":[compact(item) for item in resolved.get("selected_items",[])],
            "upload_ids":resolved.get("upload_ids",[]),"upload_revisions":resolved.get("upload_revisions",[]),
            "upload_refs":resolved.get("upload_refs",[]),"pending":pending,"requirements":pending}

    def retain_refs(self, tenant_id, owner_id, refs, holder_kind, holder_id, connection=None, *, resolved=None):
        if not holder_kind or not holder_id or len(holder_id) > 256:
            raise LibraryError("A retention identity is required", 422)
        resolved = resolved if resolved is not None else self.resolve_refs(tenant_id, owner_id, refs)
        with (nullcontext(connection) if connection is not None else self.store._connect()) as conn:
            for item in resolved["items"] + resolved.get("selected_items", []):
                retained = self._version(conn, tenant_id, owner_id, item["item_id"], item["version_id"], allow_deleted=True,metadata_only=True)
                if retained["state"] == "deleted" and not conn.execute("SELECT 1 FROM personal_library_holders WHERE tenant_id=? AND owner_id=? AND version_id=?", (tenant_id, owner_id, item["version_id"])).fetchone():
                    raise LibraryError("Saved item was deleted before admission", 404)
                conn.execute("INSERT OR IGNORE INTO personal_library_holders VALUES (?,?,?,?,?)", (tenant_id, owner_id, holder_kind, holder_id, item["version_id"]))
                if retained["uploads"]:
                    pin_file_manifest(conn, kind="library_" + holder_kind, reference_id=holder_id,
                                      tenant_id=tenant_id, owner_id=owner_id, manifest=retained["uploads"])
        return resolved

    def release_refs(self, tenant_id, owner_id, holder_kind, holder_id, connection=None):
        with (nullcontext(connection) if connection is not None else self.store._connect()) as conn:
            conn.execute("DELETE FROM personal_library_holders WHERE tenant_id=? AND owner_id=? AND holder_kind=? AND holder_id=?", (tenant_id, owner_id, holder_kind, holder_id))
            conn.execute("DELETE FROM workspace_file_pins WHERE kind=? AND reference_id=? AND upload_id IN (SELECT upload_id FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=?)", ("library_" + holder_kind, holder_id, tenant_id, owner_id))
            self._prune_deleted_versions(conn, tenant_id, owner_id)

    def capture_file(self, tenant_id, owner_id, worker_id, file_id, revision, name=None, idempotency_key=None):
        with self._generated_save(tenant_id,owner_id) as generated:
            return self._capture_file(tenant_id,owner_id,worker_id,file_id,revision,name,idempotency_key,generated)

    def _capture_file(self,tenant_id,owner_id,worker_id,file_id,revision,name,idempotency_key,generated):
        if not revision:
            raise LibraryError("Refresh Files and select an exact revision", 422)
        request_key = idempotency_key or "capture-" + _hash([worker_id, file_id, revision])[:64]
        source_ref = {"worker_id": worker_id, "file_id": file_id, "revision": revision}
        with self.store._connect() as conn:
            old = conn.execute("SELECT item_id,version_id FROM personal_library_requests WHERE tenant_id=? AND owner_id=? AND request_key=?", (tenant_id, owner_id, request_key)).fetchone()
            if old:
                saved = self._version(conn, tenant_id, owner_id, old["item_id"], old["version_id"])
                if saved["payload"].get("capture_source") != source_ref or (name is not None and saved["name"] != name):
                    raise LibraryError("This capture request already used another file version")
                return saved
        worker = self.files._worker(worker_id, tenant_id, owner_id,read_only=True)
        info = self.files.get_file(worker_id, tenant_id, owner_id, file_id, revision=revision)
        if info["is_dir"]:
            raise LibraryError("Choose a file to keep for reuse", 422)
        with self.store._connect() as conn:
            entry = self.files._entry(conn, worker, file_id)
            accepted = conn.execute("SELECT * FROM workspace_file_uploads WHERE upload_id=? AND tenant_id=? AND owner_id=? AND state='ready'", (entry.get("version_id"), tenant_id, owner_id)).fetchone()
        descriptor, _, size = self.files.open_download(worker_id, tenant_id, owner_id, file_id, revision=revision)
        try:
            before = os.fstat(descriptor)
            digest = hashlib.sha256()
            def chunks():
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                    yield chunk
                after = os.fstat(descriptor)
                if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise LibraryError("File changed while it was saved; retry", code="library_source_changed")
            # Reuse canonical bytes only after verifying this copy is still exact.
            if accepted:
                for _ in chunks():
                    pass
                if digest.hexdigest() == accepted["sha256"]:
                    upload = self.files._public(dict(accepted))
                else:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    upload = self._upload_bytes(tenant_id, owner_id, info["name"], size, chunks(), "lib-capture-" + _hash(request_key)[:64],generated)
            else:
                upload = self._upload_bytes(tenant_id, owner_id, info["name"], size, chunks(), "lib-capture-" + _hash(request_key)[:64],generated)
        finally:
            os.close(descriptor)
        return self._create_item(tenant_id, owner_id, {"name": name or info["name"], "kind": "knowledge", "upload_ids": [upload["upload_id"]], "upload_revisions": [upload["revision"]], "capture_source": source_ref}, request_key,generated)

    def knowledge_context(self, tenant_id, owner_id, refs=None, max_chars=24000, *, resolved=None):
        from .workspace_file_drafts import DraftDownload
        resolved = resolved if resolved is not None else self.resolve_refs(tenant_id, owner_id, refs or [])
        pairs = resolved.get("upload_refs") or [{"upload_id": u, "revision": r} for u,r in zip(resolved.get("upload_ids", []),resolved.get("upload_revisions", []))]
        manifest = self.files.manifest(tenant_id, owner_id, [u["upload_id"] for u in pairs], [u["revision"] for u in pairs])
        remaining, sources = max(0, min(int(max_chars), 1024 * 1024)), []
        names = {u["upload_id"]: item for item in resolved.get("items", []) for u in item.get("uploads", [])}
        for entry in manifest:
            source = {"upload_id": entry["upload_id"], "item_id": names.get(entry["upload_id"], {}).get("item_id"), "version_id": names.get(entry["upload_id"], {}).get("version_id"), "name": names.get(entry["upload_id"], {}).get("name", entry["name"]), "sha256": entry["sha256"], "loaded": False, "reason": "over_limit"}
            if entry["size_bytes"] <= min(4 * remaining, 4 * 1024 * 1024):
                reader = DraftDownload(self.files, tenant_id, owner_id, entry["upload_id"], revision=entry["revision"])
                try:
                    data = b""
                    for chunk in reader.stream():
                        data += chunk
                    try:
                        text = data.decode("utf-8")
                        if "\x00" in text:
                            raise UnicodeDecodeError("utf8", data, 0, 1, "binary")
                        if len(text) <= remaining:
                            source.update(loaded=True, reason="", text=text)
                            remaining -= len(text)
                    except UnicodeDecodeError:
                        source["reason"] = "binary"
                finally:
                    reader.close()
            sources.append(source)
        return {"sources": sources, "bootstrap_files": self.files.bootstrap_entries(tenant_id, owner_id, manifest), "upload_refs": pairs}

    @staticmethod
    def _public_application(row, *, bundle=None):
        receipt = json.loads(row.get("public_receipt_json") or row["receipt_json"])
        receipt = _public_receipt(receipt)
        result = {key: row[key] for key in ("application_id", "worker_id", "item_id", "version_id", "state", "created_at")} | {
            "name": receipt.get("name", "Saved item"), "kind": receipt.get("kind"), "version": row.get("version"), "uploads": receipt.get("uploads", []), "receipt": receipt,
            "active":row.get("removed_at") is None,"undo_available":bool(row.get("undo_expires_at") and row["undo_expires_at"]>time.time()),
            "undo_expires_at":row.get("undo_expires_at")}
        if result["kind"]=="skill" and result["state"]=="ready_for_next_start" and bundle is not None:
            from .profile_runtime import host_claude_skill_policy_reason
            reason=host_claude_skill_policy_reason(bundle,execution_mode=bundle.get("execution_mode","host"))
            if reason:
                result["state"]="not_available"
                receipt.update(reason=reason,loaded=False)
        return result

    def list_worker_items(self, tenant_id, owner_id, worker_id):
        self._expire_undo(tenant_id, owner_id)
        self.files._worker(worker_id, tenant_id, owner_id, read_only=True)
        with self.store._connect() as conn:
            sync_library_applications(conn,tenant_id,owner_id,worker_id,only_if_approved_changed=True)
            policy,_=_skill_policy_snapshot(conn,tenant_id,owner_id,worker_id)
            result=[]
            for row in conn.execute(_APPLICATION_SELECT + " WHERE a.tenant_id=? AND a.owner_id=? AND a.worker_id=? AND (a.removed_at IS NULL OR a.cleanup_state='unconfirmed' OR a.undo_expires_at>?) ORDER BY (a.removed_at IS NOT NULL),a.created_at,a.application_id",(tenant_id,owner_id,worker_id,time.time())):
                value=dict(row)
                if row["removed_at"] is not None:
                    value["state"]="cleanup_unconfirmed" if row["cleanup_state"]=="unconfirmed" else "removed"
                result.append(self._public_application(value,bundle=policy))
            return {"items":result}

    def _sync_worker_selection(self,conn,tenant,owner,worker_id):
        sync_library_applications(conn,tenant,owner,worker_id)

    def apply_to_worker(self, tenant_id, owner_id, worker_id, refs, request_key, confirm_native=False):
        return self._apply_resolved(tenant_id, owner_id, worker_id,
            self.resolve_refs(tenant_id, owner_id, refs), request_key, confirm_native)

    def apply_retained_to_worker(self, tenant_id, owner_id, worker_id, refs, request_key,
                                 holder_kind, holder_id, confirm_native=False):
        resolved = self.resolve_retained_refs(tenant_id, owner_id, refs, holder_kind, holder_id)
        return self._apply_resolved(tenant_id, owner_id, worker_id, resolved, request_key, confirm_native)

    def _apply_resolved(self, tenant_id, owner_id, worker_id, resolved, request_key, confirm_native=False):
        worker = self.files._worker(worker_id, tenant_id, owner_id)
        with self.files.native_start_guard(worker):
            return self._apply_locked(tenant_id, owner_id, worker_id, resolved, request_key, confirm_native)

    def _apply_locked(self, tenant_id, owner_id, worker_id, resolved, request_key, confirm_native=False):
        from .service import merge_bootstrap_bundle
        from .store import utc_now
        worker = self.files._worker(worker_id, tenant_id, owner_id)
        if not request_key or len(request_key) > 128:
            raise LibraryError("An add request key is required", 422)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._sync_worker_selection(conn,tenant_id,owner_id,worker_id)
            old = conn.execute("SELECT request_hash,result_json FROM personal_library_apply_requests WHERE tenant_id=? AND owner_id=? AND worker_id=? AND request_key=?", (tenant_id, owner_id, worker_id, request_key)).fetchone()
            request_hash = _hash(resolved["refs"])
            if old and old["request_hash"] != request_hash:
                raise LibraryError("This add request already selected different material")
            if old and json.loads(old["result_json"]).get("completed"):
                self._sync_worker_selection(conn,tenant_id,owner_id,worker_id)
                policy,_=_skill_policy_snapshot(conn,tenant_id,owner_id,worker_id)
                rows = [self._public_application(dict(row),bundle=policy) for row in conn.execute(
                    _APPLICATION_SELECT + " WHERE a.tenant_id=? AND a.owner_id=? AND a.worker_id=? AND a.removed_at IS NULL ORDER BY a.created_at,a.application_id", (tenant_id, owner_id, worker_id))]
                return {"items": rows, "pending": [row for row in rows if row["state"] in {"needs_review", "needs_setup", "not_available"}], "state": "replayed", "replayed": True}
            if not old:
                conn.execute("INSERT INTO personal_library_apply_requests (tenant_id,owner_id,worker_id,request_key,request_hash) VALUES (?,?,?,?,?)", (tenant_id, owner_id, worker_id, request_key, request_hash))
        pending = []
        for item in resolved["items"]:
            with self.store._connect() as conn:
                existing = conn.execute(_APPLICATION_SELECT + " WHERE a.tenant_id=? AND a.owner_id=? AND a.worker_id=? AND a.version_id=? AND a.removed_at IS NULL", (tenant_id, owner_id, worker_id, item["version_id"])).fetchone()
            if existing and not (confirm_native and existing["state"] in {"needs_review", "not_available"}):
                if existing["state"] in {"needs_review", "needs_setup", "not_available"}:
                    pending.append(self._public_application(dict(existing)))
                continue
            app_id = existing["application_id"] if existing else "pla_" + uuid.uuid4().hex
            receipt = {"name": item["name"], "kind": item["kind"], "uploads": item["uploads"], "loaded": False}
            state = "needs_setup"
            if item["uploads"]:
                bound_key = "library-" + _hash([worker_id, item["version_id"]])[:64]
                bound = self.files._bind(worker_id, tenant_id, owner_id, [u["upload_id"] for u in item["uploads"]], bound_key,
                                        revisions=[u["revision"] for u in item["uploads"]])
                with self.store._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    binding = conn.execute("SELECT binding_id FROM workspace_file_bindings WHERE tenant_id=? AND owner_id=? AND worker_id=? AND request_key=?", (tenant_id, owner_id, worker_id, bound_key)).fetchone()
                    current = conn.execute("SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
                    bundle = json.loads(current[0] or "{}")
                    ids = {u["upload_id"] for u in item["uploads"]}
                    for entry in bundle.get("files", []):
                        if entry.get("managed_upload_id") in ids:
                            entry["context_delivery"] = "lookup"
                    conn.execute("UPDATE workers SET bootstrap_bundle_json=?,updated_at=? WHERE worker_id=?", (_json(bundle), utc_now(), worker_id))
                receipt.update(binding_id=binding[0], manifest=bound["items"], context_delivery="lookup")
                state = "in_files"
            elif item["kind"] == "skill":
                payload = item["payload"]
                if confirm_native:
                    with self.store._connect() as conn:
                        payload=self._version(conn,tenant_id,owner_id,item["item_id"],item["version_id"],allow_deleted=True)["payload"]
                source_proved = worker.get("profile") == "claude-code" and "claude-code" in payload.get("profiles", [])
                paths = [f["path"] for f in payload["files"]] if "files" in payload else payload.get("paths",[])
                source_proved = source_proved and all(Path(path).is_relative_to(".claude/skills") for path in paths)
                bundle = json.loads(worker.get("bootstrap_bundle_json") or "{}")
                from .service import is_parallel_clean_room_bootstrap
                from .profile_runtime import host_claude_skill_policy_reason
                policy_reason=host_claude_skill_policy_reason(bundle,execution_mode=worker["execution_mode"]) if source_proved else None
                if not source_proved or is_parallel_clean_room_bootstrap(None, bundle):
                    state = "not_available"
                    receipt["reason"] = "Not available for this worker type"
                elif policy_reason:
                    state = "not_available"
                    receipt["reason"] = policy_reason
                elif not confirm_native:
                    state = "needs_review"
                    receipt["reason"] = "Review this exact worker and skill files before adding"
                else:
                    if self.files.scope_has_active_run(worker):
                        raise LibraryError("Available when the worker is idle", code="library_worker_busy")
                    with self.store._connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        current = conn.execute("SELECT bootstrap_bundle_json,state FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?", (worker_id, tenant_id, owner_id)).fetchone()
                        if current is None or current["state"] in {"terminated", "terminating", "termination_failed"}:
                            raise LibraryError("Workspace is closed")
                        prior = json.loads(current["bootstrap_bundle_json"] or "{}")
                        incoming = [{"scope": "workspace", **f} for f in payload["files"]]
                        by_path = {e.get("path"): e for e in prior.get("files", [])}
                        if any(e["path"] in by_path and by_path[e["path"]] != e for e in incoming):
                            raise LibraryError("A skill path already contains different material; remove or review it first", code="library_path_conflict")
                        applied = merge_bootstrap_bundle(prior, {"files": incoming})
                        conn.execute("UPDATE workers SET bootstrap_bundle_json=?,updated_at=? WHERE worker_id=?", (_json(applied), utc_now(), worker_id))
                    receipt.update(prior=prior, applied=applied, paths=[{"path": f["path"], "sha256": hashlib.sha256(f["content"].encode()).hexdigest()} for f in incoming], projection="next_native_start", reason="Ready for the next native start; session load is not yet confirmed")
                    state = "ready_for_next_start"
            elif item["kind"] == "approved":
                state = "needs_review"
                receipt.update(library_id=item["payload"]["library_id"], reason="Use the existing approved-item review")
            else:
                receipt.update(source_uri=item["payload"].get("source_uri"), reason="Use the worker’s native setup and verify actual availability")
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.retain_refs(tenant_id, owner_id, [{"item_id": item["item_id"], "version_id": item["version_id"]}], "application", app_id, connection=conn, resolved={"items": [item]})
                raced = conn.execute("SELECT application_id FROM personal_library_applications WHERE tenant_id=? AND owner_id=? AND worker_id=? AND version_id=? AND removed_at IS NULL", (tenant_id, owner_id, worker_id, item["version_id"])).fetchone()
                if raced and not existing:
                    self.release_refs(tenant_id, owner_id, "application", app_id, connection=conn)
                    app_id = raced["application_id"]
                elif existing:
                    conn.execute("UPDATE personal_library_applications SET state=?,receipt_json=?,public_receipt_json=? WHERE application_id=?", (state, _json(receipt),_json(_public_receipt(receipt)),app_id))
                else:
                    conn.execute("INSERT INTO personal_library_applications (application_id,tenant_id,owner_id,worker_id,item_id,version_id,state,receipt_json,created_at,removed_at,request_key,public_receipt_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (app_id, tenant_id, owner_id, worker_id, item["item_id"], item["version_id"], state, _json(receipt), time.time(), None, request_key,_json(_public_receipt(receipt))))
                self._sync_worker_selection(conn,tenant_id,owner_id,worker_id)
            if state in {"needs_setup", "needs_review", "not_available"}:
                pending.append({"item_id": item["item_id"], "version_id": item["version_id"], "name": item["name"], "state": state, "receipt": receipt})
        with self.store._connect() as conn:
            conn.execute("UPDATE personal_library_apply_requests SET result_json=? WHERE tenant_id=? AND owner_id=? AND worker_id=? AND request_key=?", (_json({"completed": True}), tenant_id, owner_id, worker_id, request_key))
        listing=self.list_worker_items(tenant_id,owner_id,worker_id)
        pending=[item for item in listing["items"] if item["state"] in {"needs_setup","needs_review","not_available"}]
        return {**listing, "upload_ids": resolved["upload_ids"], "upload_revisions": resolved["upload_revisions"], "pending": pending,
                "state": "action_required" if pending else "available"}

    def remove_from_worker(self, tenant_id, owner_id, worker_id, application_id):
        worker = self.files._worker(worker_id, tenant_id, owner_id)
        with self.files.native_start_guard(worker):
            return self._remove_locked(tenant_id, owner_id, worker_id, application_id)

    def _other_binding_paths(self, conn, worker, binding_id):
        scope=self.files._workspace_key(self.files._scope_worker(worker,connection=conn))
        used=set()
        for binding in conn.execute("SELECT worker_id,manifest_json FROM workspace_file_bindings WHERE tenant_id=? AND owner_id=? AND binding_id!=?", (worker["tenant_id"],worker["owner_id"],binding_id or "")):
            member_scope=None
            for entry in json.loads(binding["manifest_json"]):
                entry_scope=entry.get("file_scope_id")
                if entry_scope is None:
                    if member_scope is None:
                        member=conn.execute("SELECT * FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",(binding["worker_id"],worker["tenant_id"],worker["owner_id"])).fetchone()
                        member_scope=self.files._workspace_key(self.files._scope_worker(dict(member),connection=conn)) if member else ""
                    entry_scope=member_scope
                if entry_scope==scope:
                    used.add((entry.get("upload_id"),entry.get("path")))
        return used

    def _remove_locked(self, tenant_id, owner_id, worker_id, application_id):
        from .bootstrap_contributions import (subtract_contribution, preserve_required_contribution,
            rebase_active_contributions, active_native_contributions)
        from .store import utc_now
        worker = self.files._worker(worker_id, tenant_id, owner_id)
        if self.files.scope_has_active_run(worker):
            raise LibraryError("Available when the worker is idle", code="library_worker_busy")
        with self.store._connect() as conn:
            self._sync_worker_selection(conn,tenant_id,owner_id,worker_id)
            app = conn.execute("SELECT * FROM personal_library_applications WHERE application_id=? AND tenant_id=? AND owner_id=? AND worker_id=?", (application_id, tenant_id, owner_id, worker_id)).fetchone()
            if app is None:
                raise LibraryError("Added item not found", 404)
            receipt = json.loads(app["receipt_json"])
            other_binding_paths=self._other_binding_paths(conn,worker,receipt.get("binding_id")) if receipt.get("manifest") and app["removed_at"] is None else set()
        if app["removed_at"] is not None:
            if receipt.get("removal_undo",{}).get("restored_at"):
                return {"application_id":application_id,"state":"already_restored","replayed":True,"undo_available":False,"history_unchanged":True}
            previous=receipt.get("removal_result")
            if previous is None:
                raise LibraryError("This item was already removed; refresh the Library",409)
            return self._cleanup_removed(worker,{**previous,"replayed":True})
        approved_removal={}
        grant_id=receipt.get("removal_grant_id") or receipt.get("grant_id")
        if receipt.get("kind")=="approved" and grant_id:
            if self.service.control_plane_store is None:
                raise LibraryError("Approved capability management is unavailable",503)
            receipt["removal_grant_id"]=grant_id
            with self.store._connect() as conn:
                shared=conn.execute("SELECT COUNT(*) FROM personal_library_applications a JOIN personal_library_versions v ON v.version_id=a.version_id WHERE a.tenant_id=? AND a.owner_id=? AND a.worker_id=? AND a.removed_at IS NULL AND a.application_id!=? AND json_extract(v.metadata_json,'$.library_id')=?",(tenant_id,owner_id,worker_id,application_id,receipt.get("library_id"))).fetchone()[0]
                conn.execute("UPDATE personal_library_applications SET receipt_json=? WHERE application_id=?",(_json(receipt),application_id))
            if not shared:
                approved_removal=self.service.control_plane_store.revoke_workspace_grant(
                    grant_id=grant_id,tenant_id=tenant_id,owner_id=owner_id,worker_id=worker_id)
            else:
                approved_removal["retained_capabilities"]=[{"library_id":receipt["library_id"],"reason":"Still enabled by another selected Library item","references":shared}]
        retained, trash = [], []
        # Ordinary unchanged copies use the existing recoverable mutation owner.
        for entry in receipt.get("manifest", []):
            from .workspace_file_mutations import FileMutations
            operation_id="fop_"+_hash([application_id,entry["upload_id"],entry["path"]])[:32]
            with self.store._connect() as conn:
                completed=conn.execute("SELECT file_id FROM workspace_file_operations WHERE operation_id=? AND tenant_id=? AND owner_id=? AND workspace_key=?",(operation_id,tenant_id,owner_id,self.files._workspace_key(self.files._scope_worker(worker)))).fetchone()
                row = conn.execute("SELECT * FROM workspace_file_entries WHERE tenant_id=? AND owner_id=? AND workspace_key=? AND path=? AND deleted=0", (tenant_id, owner_id, self.files._workspace_key(self.files._scope_worker(worker)), entry["path"])).fetchone()
                other=(entry["upload_id"],entry["path"]) in other_binding_paths
            if completed:
                trash.append(FileMutations(self.files,worker)._mutate_locked(completed["file_id"],revision="",path=None,delete=True,operation_id=operation_id))
                continue
            if row is None:
                continue
            if other:
                retained.append({"path": entry["path"], "reason": "Used by another accepted binding"})
                continue
            try:
                from .workspace_files import _directory, _relative, _snapshot_fd
                relative = _relative(row["path"])
                with _directory(self.files._root(worker), relative.parent) as parent:
                    descriptor, revision, _ = _snapshot_fd(parent, relative.name)
                digest = hashlib.sha256()
                try:
                    while chunk := os.read(descriptor, 1024 * 1024):
                        digest.update(chunk)
                finally:
                    os.close(descriptor)
                if digest.hexdigest() != entry["sha256"]:
                    retained.append({"path": entry["path"], "reason": "Kept your edited copy"})
                    continue
                trash.append(FileMutations(self.files, worker)._mutate_locked(row["file_id"], revision=revision, path=None, delete=True,operation_id=operation_id))
            except FileAdmissionError as exc:
                if exc.status_code == 404:
                    continue
                raise
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT bootstrap_bundle_json FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?", (worker_id, tenant_id, owner_id)).fetchone()
            bundle = json.loads(current[0] or "{}")
            native_before = bundle if receipt.get("prior") is not None else {}
            if receipt.get("prior") is not None:
                before_remove = bundle
                bundle, changed = subtract_contribution(bundle, receipt["prior"], receipt["applied"],restore_prior=receipt.get("restore_prior",receipt["prior"]))
                try:
                    for native in active_native_contributions(conn,tenant_id,owner_id,worker_id,worker["profile"],application_id=application_id):
                        bundle = preserve_required_contribution(before_remove,bundle,native)
                except ValueError as exc:
                    raise LibraryError(str(exc)) from exc
                rebase_active_contributions(conn,tenant_id,owner_id,worker_id,receipt["prior"],receipt["applied"],application_id=application_id)
                retained.extend({"path": p, "reason": "Kept later configuration changes"} for p in changed)
            ids = {u["upload_id"] for u in receipt.get("uploads", [])}
            if ids:
                # Remove only this binding's recorded input contribution.
                contribution={(entry["upload_id"],entry["path"]) for entry in receipt.get("manifest",[])}
                bundle["files"] = [e for e in bundle.get("files", []) if (e.get("managed_upload_id"),e.get("path")) not in contribution-other_binding_paths]
                conn.execute("DELETE FROM workspace_file_pins WHERE kind='workspace' AND reference_id=?", (receipt.get("binding_id", ""),))
                conn.execute("DELETE FROM workspace_file_bindings WHERE binding_id=? AND tenant_id=? AND owner_id=?", (receipt.get("binding_id", ""), tenant_id, owner_id))
            conn.execute("UPDATE workers SET bootstrap_bundle_json=?,updated_at=? WHERE worker_id=?", (_json(bundle), utc_now(), worker_id))
            undo_expires = time.time() + 86400
            version = self._version(conn, tenant_id, owner_id, app["item_id"], app["version_id"], allow_deleted=True,metadata_only=True)
            self.retain_refs(tenant_id, owner_id, [], "removal_undo", application_id, connection=conn,
                             resolved={"items": [version]})
            receipt["removal_undo"] = {"expires_at": undo_expires, "trash": trash, "previous_state": app["state"]}
            removed_native_files = [{"path": entry["path"], "sha256": hashlib.sha256(entry["content"].encode()).hexdigest()}
                for entry in native_before.get("files", []) if isinstance(entry,dict) and isinstance(entry.get("content"),str)
                and entry.get("path") not in {e.get("path") for e in bundle.get("files", [])}]
            removed_native_files.extend(approved_removal.get("removed_native_files",[]))
            remaining=[{"path":e["path"]} for e in bundle.get("files",[]) if isinstance(e,dict) and e.get("path")]
            result={"application_id":application_id,"state":"removed","trash":trash,"retained":retained,
                "removed_native_files":removed_native_files,"remaining_native_files":remaining,"undo_available":True,
                "undo_expires_at":undo_expires,"history_unchanged":True}
            if approved_removal.get("retained_capabilities"):
                result["retained_capabilities"]=approved_removal["retained_capabilities"]
            if removed_native_files:
                result["native_cleanup"]={"state":"unconfirmed","code":"cleanup_pending"}
            receipt["removal_result"]=result
            public=_public_receipt(receipt)
            if result.get("native_cleanup"):public["native_cleanup"]=result["native_cleanup"]
            conn.execute("UPDATE personal_library_applications SET removed_at=?,state='removed',receipt_json=?,public_receipt_json=?,undo_expires_at=?,cleanup_state=? WHERE application_id=?", (time.time(), _json(receipt),_json(public),undo_expires,"unconfirmed" if removed_native_files else "",application_id))
            self._sync_worker_selection(conn,tenant_id,owner_id,worker_id)
            self.release_refs(tenant_id, owner_id, "application", application_id, connection=conn)
        return self._cleanup_removed(worker,result)

    def _cleanup_removed(self,worker,result):
        from .bootstrap import remove_projected_library_files
        from .openclaw_runtime import RuntimeErrorBase
        cleanup={"removed":[],"retained":[]}
        if result["removed_native_files"]:
            remaining=self.files.scope_bootstrap_paths(worker)
            try:
                cleanup=remove_projected_library_files(self.files._root(worker),result["removed_native_files"],remaining)
            except (ValueError,RuntimeErrorBase,OSError) as exc:
                cleanup={"state":"unconfirmed","code":getattr(exc,"code","native_cleanup_unavailable")}
        result={**result,"native_cleanup":cleanup,"undo_available":result.get("undo_expires_at",0)>time.time()}
        with self.store._connect() as conn:
            row=conn.execute("SELECT receipt_json FROM personal_library_applications WHERE application_id=? AND tenant_id=? AND owner_id=?",(result["application_id"],worker["tenant_id"],worker["owner_id"])).fetchone()
            if row:
                receipt=json.loads(row[0]);receipt["removal_result"]=result
                receipt["native_cleanup"]=cleanup
                conn.execute("UPDATE personal_library_applications SET receipt_json=?,public_receipt_json=?,cleanup_state=? WHERE application_id=?",(_json(receipt),_json(_public_receipt(receipt)),"unconfirmed" if cleanup.get("state")=="unconfirmed" else "",result["application_id"]))
        return result

    def _expire_undo(self, tenant_id, owner_id):
        """Lazy expiry of Library-only Undo retention; ordinary Files Trash is unchanged."""
        now = time.time()
        with self.store._connect() as conn:
            for row in conn.execute("SELECT application_id,receipt_json FROM personal_library_applications WHERE tenant_id=? AND owner_id=? AND undo_expires_at<=?", (tenant_id, owner_id, now)).fetchall():
                receipt = json.loads(row["receipt_json"])
                undo = receipt.get("removal_undo") or {}
                if undo.get("expires_at", 0) <= now and not undo.get("released"):
                    self.release_refs(tenant_id, owner_id, "removal_undo", row["application_id"], connection=conn)
                    if undo:
                        undo["released"] = True
                        receipt.pop("prior", None)
                        receipt.pop("applied", None)
                        receipt.pop("restore_prior", None)
                        conn.execute("UPDATE personal_library_applications SET receipt_json=?,undo_expires_at=NULL WHERE application_id=?", (_json(receipt), row["application_id"]))

    def restore_to_worker(self, tenant_id, owner_id, worker_id, application_id):
        self._expire_undo(tenant_id, owner_id)
        worker = self.files._worker(worker_id, tenant_id, owner_id)
        if self.files.scope_has_active_run(worker):
            raise LibraryError("Available when the worker is idle", code="library_worker_busy")
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM personal_library_applications WHERE application_id=? AND tenant_id=? AND owner_id=? AND worker_id=?", (application_id, tenant_id, owner_id, worker_id)).fetchone()
            if row is None:
                raise LibraryError("Removed item not found", 404)
            receipt = json.loads(row["receipt_json"])
            undo = receipt.get("removal_undo") or {}
            if undo.get("restored_at"):
                return {**self.list_worker_items(tenant_id, owner_id, worker_id), "state": "already_restored"}
            if row["removed_at"] is None or undo.get("released") or undo.get("expires_at", 0) <= time.time():
                raise LibraryError("Library Undo expired; files may still be restored from Files Trash", code="library_undo_expired")
        for entry in undo.get("trash", []):
            self.files.undo(worker_id, tenant_id, owner_id, entry["file_id"], entry["undo_id"])
        refs = [{"item_id": row["item_id"], "version_id": row["version_id"]}]
        result = self.apply_retained_to_worker(tenant_id, owner_id, worker_id, refs,
            "undo-" + _hash(application_id)[:64], "removal_undo", application_id,
            confirm_native=undo.get("previous_state") == "ready_for_next_start")
        with self.store._connect() as conn:
            undo["restored_at"] = time.time()
            conn.execute("UPDATE personal_library_applications SET receipt_json=?,undo_expires_at=NULL,cleanup_state='' WHERE application_id=?", (_json(receipt), application_id))
            self.release_refs(tenant_id, owner_id, "removal_undo", application_id, connection=conn)
        return {**result,**self.list_worker_items(tenant_id,owner_id,worker_id), "state": "restored", "restored_application_id": application_id}
