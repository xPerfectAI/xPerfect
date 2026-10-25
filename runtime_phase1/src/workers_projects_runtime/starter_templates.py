"""Public starter material composes ordinary private Library items and worker templates.

No worker starts, grant, package installation or new expert identity is created here.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations

from .personal_library import LibraryError
from .runtime_requirements import WORKER_EFFORT_ENV_VALUES

PACK_ROOT = Path(__file__).with_name("starter_packs")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _asset(root, source):
    path = PurePosixPath(str(source))
    if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
        raise LibraryError("Starter material contains an invalid file path", 503)
    target = root.joinpath(*path.parts)
    if any(p.is_symlink() for p in [target, *target.parents] if p == root or root in p.parents):
        raise LibraryError("Starter material must use regular packaged files", 503)
    if not target.resolve().is_relative_to(root.resolve()) or not target.is_file():
        raise LibraryError("A required starter file is unavailable", 503)
    raw = target.read_bytes()
    if len(raw) > 1024 * 1024:
        raise LibraryError("Starter material exceeds the supported file size", 503)
    return raw.decode("utf-8")


def _load(root):
    spec = json.loads(_asset(root, "template.json"))
    if spec.get("schema_version") != 1 or spec.get("id") != root.name:
        raise LibraryError("Starter template metadata is invalid", 503)
    items = spec.get("library_items")
    if not isinstance(items, list) or not items or len(items) > 32:
        raise LibraryError("Starter template requires bounded Library material", 503)
    hydrated, keys = [], set()
    for entry in items:
        key = str(entry.get("key") or "")
        if not key or key in keys:
            raise LibraryError("Starter Library item keys must be distinct", 503)
        keys.add(key)
        item = dict(entry.get("item") or {})
        if item.get("kind") not in {"note", "skill", "native"}:
            raise LibraryError("Starter material must use supported Library kinds", 503)
        if entry.get("file_sources"):
            if item.get("kind") != "skill":
                raise LibraryError("Packaged skill files require a skill item", 503)
            item["files"] = [{"scope": "workspace", "path": e["path"], "content": _asset(root, e["source"])}
                             for e in entry["file_sources"]]
        hydrated.append({"key": key, "selected": entry.get("selected", True) is True, "item": item})
    spec["library_items"] = hydrated
    digest = hashlib.sha256(_json(spec).encode()).hexdigest()
    return spec, "sha256:" + digest


def _summary(spec, digest):
    return {key: spec.get(key) for key in ("id", "name", "role", "description", "outcome", "tags", "profile", "execution_mode", "version", "provenance")} | {
        "content_hash": digest,
        "abilities": [{"key": e["key"], "name": e["item"]["name"], "kind": e["item"]["kind"],
                       "included": e["selected"], "profiles": e["item"].get("profiles", []),
                       **({"source_uri": e["item"]["source_uri"]} if e["item"].get("source_uri") else {})}
                      for e in spec["library_items"]],
    }


def catalog():
    if not PACK_ROOT.is_dir():
        return {"items": []}
    return {"items": [_summary(*_load(p.parent)) for p in sorted(PACK_ROOT.glob("*/template.json"))]}


class StarterImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)
    content_hash: str = Field(min_length=1, max_length=100)
    provider_account: dict[str, str] | None = None
    effort: str | None = Field(default=None, max_length=32)


def _active_import(conn, cp, tenant, owner, fingerprint, spec, bundle, account):
    """Reuse the receipt's exact setup only while its lineage head is active."""
    rows = conn.execute("""SELECT t.template_id FROM workspace_template_imports i
        JOIN workspace_templates t ON t.template_id=i.template_id AND t.tenant_id=i.tenant_id AND t.owner_id=i.owner_id
        JOIN workspace_templates h ON h.lineage_id=t.lineage_id AND h.tenant_id=t.tenant_id AND h.owner_id=t.owner_id
        WHERE i.tenant_id=? AND i.owner_id=? AND i.request_hash=? AND h.catalog_state='active'
          AND NOT EXISTS (SELECT 1 FROM workspace_templates n WHERE n.tenant_id=h.tenant_id
            AND n.owner_id=h.owner_id AND n.lineage_id=h.lineage_id AND n.version>h.version)
        ORDER BY i.created_at DESC,h.version DESC""", (tenant, owner, fingerprint)).fetchall()
    for row in rows:
        result = cp.get_workspace_template(template_id=row['template_id'], tenant_id=tenant, owner_id=owner)
        worker = result['content'].get('worker', {})
        if (worker.get('profile') == spec['profile'] and worker.get('execution_mode') == spec['execution_mode']
                and worker.get('bootstrap_bundle') == bundle and worker.get('provider_account_ref') == account):
            return {k: v for k, v in result.items() if k != 'content'}
    return None


def import_starter(service, starter_id, tenant, owner, request):
    cp, store = service.control_plane_store, service.store
    if cp is None or Path(cp.db_path).resolve() != Path(store.db_path).resolve():
        raise LibraryError("Starter templates require the canonical owner datastore", 503)
    # A completed receipt owns the original exact template, including after a
    # packaged catalog update. Replaying it is a read, never an implicit upgrade.
    requested_fingerprint = hashlib.sha256(_json([starter_id, request.content_hash, request.provider_account, request.effort]).encode()).hexdigest()
    with store._connect() as conn:
        prior = conn.execute("SELECT request_hash,template_id FROM workspace_template_imports WHERE tenant_id=? AND owner_id=? AND idempotency_key=?", (tenant, owner, request.idempotency_key)).fetchone()
    if prior and prior["request_hash"] != requested_fingerprint:
        raise LibraryError("This import request already used different choices", 409)
    if prior and prior["template_id"]:
        result = cp.get_workspace_template(tenant_id=tenant, owner_id=owner, template_id=prior["template_id"])
        if result is None:
            raise LibraryError("The saved template receipt is unavailable; no new import was created", 409)
        return {"template": {k: v for k, v in result.items() if k != "content"}, "idempotent_replay": True}
    matches = [p.parent for p in PACK_ROOT.glob("*/template.json") if p.parent.name == starter_id]
    if len(matches) != 1:
        raise LibraryError("This starter template is unavailable", 404)
    spec, digest = _load(matches[0])
    if request.content_hash != digest:
        raise LibraryError("This starter changed. Refresh its details before using it", 409)
    profile = str(spec.get("profile") or "")
    placement = str(spec.get("execution_mode") or "")
    service._ensure_profile_allowed(profile)
    service._ensure_execution_allowed(placement)
    bundle = {"project_definition": str(spec.get("project_definition") or "")}
    account = request.provider_account
    if account is not None:
        if set(account) - {"policy", "account_id"} or account.get("policy") not in {"legacy", "personal_required", "personal_preferred"}:
            raise LibraryError("Choose a supported account selection", 422)
        account_id = str(account.get("account_id") or "")
        if account.get("policy") == "legacy" and account_id:
            raise LibraryError("Deployment account selection cannot contain a personal account", 422)
        if account.get("policy") == "personal_required" and not account_id:
            raise LibraryError("Choose an account for this worker", 422)
        if account_id and service._retained_provider_account_problem(account_id=account_id, tenant_id=tenant, owner_id=owner, profile=profile):
            raise LibraryError("Reconnect this worker’s account or choose another ready account", 409)
    if request.effort:
        effort_key = {"claude-code": "WPR_CLAUDE_CODE_EFFORT", "codex-cli": "WPR_CODEX_CLI_REASONING_EFFORT", "grok-build": "WPR_GROK_REASONING_EFFORT"}.get(profile)
        if not effort_key or request.effort not in WORKER_EFFORT_ENV_VALUES[effort_key]:
            raise LibraryError("This worker does not support the selected effort", 422)
        bundle["env"] = {effort_key: request.effort}
    fingerprint = hashlib.sha256(_json([starter_id, digest, account, request.effort]).encode()).hexdigest()
    cp, store = service.control_plane_store, service.store
    if cp is None or Path(cp.db_path).resolve() != Path(store.db_path).resolve():
        raise LibraryError("Starter templates require the canonical owner datastore", 503)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT request_hash,template_id FROM workspace_template_imports WHERE tenant_id=? AND owner_id=? AND idempotency_key=?", (tenant, owner, request.idempotency_key)).fetchone()
        if prior and prior["request_hash"] != fingerprint:
            raise LibraryError("This import request already used different choices", 409)
        if prior and prior["template_id"]:
            result = cp.get_workspace_template(tenant_id=tenant, owner_id=owner, template_id=prior["template_id"])
            return {"template": {k: v for k, v in result.items() if k != "content"}, "idempotent_replay": True}
        match = _active_import(conn, cp, tenant, owner, fingerprint, spec, bundle, account)
        if match:
            conn.execute("INSERT INTO workspace_template_imports (tenant_id,owner_id,idempotency_key,request_hash,template_id,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,owner_id,idempotency_key) DO UPDATE SET template_id=excluded.template_id",
                         (tenant, owner, request.idempotency_key, fingerprint, match['template_id'], time.time()))
            return {"template": match, "idempotent_replay": False, "reused_saved_expert": True}
        conn.execute("INSERT OR IGNORE INTO workspace_template_imports (tenant_id,owner_id,idempotency_key,request_hash,template_id,created_at) VALUES (?,?,?,?,NULL,?)", (tenant, owner, request.idempotency_key, fingerprint, time.time()))
        staging_key = conn.execute("SELECT idempotency_key FROM workspace_template_imports WHERE tenant_id=? AND owner_id=? AND request_hash=? AND template_id IS NULL ORDER BY created_at,idempotency_key LIMIT 1",
                                   (tenant, owner, fingerprint)).fetchone()[0]
    retained, selected = [], []
    for entry in spec["library_items"]:
        key = "starter-" + hashlib.sha256(_json([staging_key, fingerprint, entry["key"]]).encode()).hexdigest()
        saved = service.personal_library.create_item(tenant, owner, entry["item"], key)
        ref = {"item_id": saved["item_id"], "version_id": saved["version_id"]}
        retained.append(ref)
        if entry["selected"]:
            selected.append(ref)
    resolved = service.personal_library.resolve_refs(tenant, owner, selected)
    content = {"schema_version": 1, "project": {"title": spec["name"], "goal": spec.get("description", "")},
               "worker": {"name": spec["name"], "role": spec["role"], "profile": profile, "execution_mode": placement,
                          "bootstrap_bundle": bundle, "tags": spec.get("tags", []),
                          **({"provider_account_ref": account} if account is not None else {})},
               "library_refs": [], "personal_library_refs": selected,
               "source": {"starter_id": starter_id, "content_hash": digest, "version": spec.get("version")}}
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT template_id FROM workspace_template_imports WHERE tenant_id=? AND owner_id=? AND idempotency_key=?", (tenant, owner, request.idempotency_key)).fetchone()
        if prior["template_id"]:
            result = cp.get_workspace_template(tenant_id=tenant, owner_id=owner, template_id=prior["template_id"])
            return {"template": {k: v for k, v in result.items() if k != "content"}, "idempotent_replay": True}
        result = _active_import(conn, cp, tenant, owner, fingerprint, spec, bundle, account)
        if result is None:
            result = cp.create_workspace_template(tenant_id=tenant, owner_id=owner, name=spec["name"], description=spec.get("description", ""), content=content, connection=conn)
            service.personal_library.retain_refs(tenant, owner, selected, "workspace_template", result["template_id"], connection=conn, resolved=resolved)
        conn.execute("UPDATE workspace_template_imports SET template_id=? WHERE tenant_id=? AND owner_id=? AND idempotency_key=?", (result["template_id"], tenant, owner, request.idempotency_key))
    return {"template": result, "idempotent_replay": False}


def install_starter_routes(app, service, principal):
    @app.get("/v1/workspace-starters")
    def listing(request: Request):
        ctx, _, _ = principal(request)
        if ctx.auth_mode == "signed_link" or str(ctx.role or "").lower() == "viewer":
            raise HTTPException(403, detail="Sign in with workspace management access")
        return catalog()

    @app.post("/v1/workspace-starters/{starter_id}/import", status_code=201)
    def importing(starter_id: str, request: Request, payload: StarterImport):
        ctx, tenant, owner = principal(request)
        if ctx.auth_mode == "signed_link" or str(ctx.role or "").lower() == "viewer":
            raise HTTPException(403, detail="Sign in with workspace management access")
        return import_starter(service, starter_id, tenant, owner, payload)


def register_starter_tools(server, client):
    @server.tool(name="workspace_starter_list", description="Browse the packaged starting worker templates and their exact included knowledge and abilities. This reads public starting material; it does not create a worker, install tools or grant access.", annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def listing():
        return client._request("GET", "/v1/workspace-starters", require_write_scope=True)

    @server.tool(name="workspace_starter_import", description="Save one exact packaged starter as an ordinary private worker template and Library material. Pass the listed content hash and keep one idempotency key across retries. Choose the user's own account explicitly when supplied. Importing starts no worker and approves no native capability. Use the existing template instantiate and exact-target Library review to continue.")
    def importing(starter_id: str, content_hash: str, idempotency_key: str,
                  provider_account_id: str = "", provider_account_policy: str = "", effort: str = ""):
        from urllib.parse import quote
        payload = {"content_hash": content_hash, "idempotency_key": idempotency_key}
        if provider_account_id or provider_account_policy:
            payload["provider_account"] = {"policy": provider_account_policy or "personal_required",
                                           **({"account_id": provider_account_id} if provider_account_id else {})}
        if effort:
            payload["effort"] = effort
        return client._request("POST", "/v1/workspace-starters/" + quote(starter_id, safe="") + "/import",
                               json_body=payload, require_write_scope=True)
