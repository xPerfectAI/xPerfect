"""Remove only a recorded managed bootstrap contribution, retaining other edits."""
from __future__ import annotations

from copy import deepcopy
import json

_MISSING = object()


def subtract_contribution(current: dict, prior: dict, applied: dict, *, restore_prior: dict | None = None):
    """Undo changed leaves that still match; do not restore a whole old snapshot.

    Files have an existing (scope,path) identity. Later native/user edits are kept.
    `restore_prior` can exclude already-retired earlier contributions without
    changing immutable audit snapshots.
    """
    retained = []
    restore = prior if restore_prior is None else restore_prior

    def walk(now, before, after, replacement, path):
        if before == after and before == replacement:
            return deepcopy(now) if now is not _MISSING else _MISSING
        if path and path[-1] == "files" and isinstance(after, list):
            key = lambda item: (item.get("scope", "workspace"), item.get("path")) if isinstance(item, dict) else None
            b = {key(i): i for i in before} if isinstance(before, list) else {}
            a = {key(i): i for i in after}
            r = {key(i): i for i in replacement} if isinstance(replacement, list) else {}
            n = list(now) if isinstance(now, list) else []
            for identity in set(b) | set(a):
                if b.get(identity, _MISSING) == a.get(identity, _MISSING) == r.get(identity, _MISSING):
                    continue
                index = next((i for i, value in enumerate(n) if key(value) == identity), None)
                actual = n[index] if index is not None else _MISSING
                expected = a.get(identity, _MISSING)
                if actual == expected:
                    value = r.get(identity, _MISSING)
                    if value is _MISSING:
                        if index is not None:
                            n.pop(index)
                    elif index is None:
                        n.append(deepcopy(value))
                    else:
                        n[index] = deepcopy(value)
                else:
                    retained.append("/".join((*path, str(identity[1]))))
            return _MISSING if not n and replacement is _MISSING else n
        if isinstance(after, dict) and (isinstance(before, dict) or before is _MISSING):
            b = before if isinstance(before, dict) else {}
            n = deepcopy(now) if isinstance(now, dict) else {}
            r = replacement if isinstance(replacement, dict) else {}
            for key in set(b) | set(after):
                value = walk(n.get(key, _MISSING), b.get(key, _MISSING), after.get(key, _MISSING), r.get(key, _MISSING), (*path, key))
                if value is _MISSING:
                    n.pop(key, None)
                else:
                    n[key] = value
            return n
        if now == after:
            return deepcopy(replacement) if replacement is not _MISSING else _MISSING
        retained.append("/".join(path))
        return deepcopy(now) if now is not _MISSING else _MISSING

    return walk(current, prior, applied, restore, ()), sorted(set(retained))


def preserve_required_contribution(current: dict, candidate: dict, required: dict):
    """Keep exact current leaves still required by another active contribution.

    `required` is the other item's declared activation, never its full historic
    applied snapshot. Edited current values are not overwritten by this pass.
    """
    result = deepcopy(candidate)
    for key, expected in required.items():
        actual = current.get(key, _MISSING)
        if key == "files" and isinstance(expected, list):
            files = list(result.get(key, []))
            for entry in expected:
                identity = (entry.get("scope", "workspace"), entry.get("path"))
                exact = next((value for value in current.get(key, [])
                              if (value.get("scope", "workspace"), value.get("path")) == identity
                              and value == entry), None)
                if exact is not None:
                    files = [value for value in files if (value.get("scope", "workspace"), value.get("path")) != identity]
                    files.append(deepcopy(exact))
            if files:
                result[key] = files
        elif isinstance(expected, dict) and isinstance(actual, dict):
            result[key] = preserve_required_contribution(actual, result.get(key, {}), expected)
        elif actual == expected:
            result[key] = deepcopy(actual)
    return result


def rebase_active_contributions(conn, tenant, owner, worker_id, prior, applied, *, grant_id=None, application_id=None):
    """Maintain derived restoration baselines without changing original audits."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_capability_grants'").fetchone():
        for row in conn.execute("SELECT grant_id,prior_bootstrap_bundle_json,restore_bootstrap_bundle_json FROM workspace_capability_grants WHERE tenant_id=? AND owner_id=? AND worker_id=? AND revoked_at IS NULL AND grant_id!=?", (tenant,owner,worker_id,grant_id or "")).fetchall():
            baseline=json.loads(row["restore_bootstrap_bundle_json"] or row["prior_bootstrap_bundle_json"] or "{}")
            baseline,_=subtract_contribution(baseline,prior,applied)
            conn.execute("UPDATE workspace_capability_grants SET restore_bootstrap_bundle_json=? WHERE grant_id=?",(json.dumps(baseline),row["grant_id"]))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_library_applications'").fetchone():
        for row in conn.execute("SELECT application_id,receipt_json FROM personal_library_applications WHERE tenant_id=? AND owner_id=? AND worker_id=? AND removed_at IS NULL AND application_id!=?", (tenant,owner,worker_id,application_id or "")).fetchall():
            receipt=json.loads(row["receipt_json"])
            if "prior" not in receipt:
                continue
            baseline,_=subtract_contribution(receipt.get("restore_prior",receipt["prior"]),prior,applied)
            receipt["restore_prior"]=baseline
            conn.execute("UPDATE personal_library_applications SET receipt_json=? WHERE application_id=?",(json.dumps(receipt),row["application_id"]))


def active_native_contributions(conn, tenant, owner, worker_id, profile, *, grant_id=None, application_id=None):
    """Read other exact active declarations; never replay a full old bootstrap."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_capability_grants'").fetchone():
        from .library_registry import validate_library_manifest, activation_bundle_for_profile
        for row in conn.execute("SELECT installation_plan_json FROM workspace_capability_grants WHERE tenant_id=? AND owner_id=? AND worker_id=? AND revoked_at IS NULL AND grant_id!=?", (tenant,owner,worker_id,grant_id or "")):
            for item in json.loads(row["installation_plan_json"]):
                saved=conn.execute("SELECT manifest_json FROM control_plane_library WHERE library_id=? AND content_hash=?",(item["library_id"],item["content_hash"])).fetchone()
                if saved is None:
                    raise ValueError("An active capability's pinned package is unavailable")
                yield activation_bundle_for_profile(validate_library_manifest(json.loads(saved["manifest_json"])),profile)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_library_applications'").fetchone():
        for row in conn.execute("SELECT v.payload_json FROM personal_library_applications a JOIN personal_library_versions v ON v.version_id=a.version_id WHERE a.tenant_id=? AND a.owner_id=? AND a.worker_id=? AND a.removed_at IS NULL AND a.application_id!=? AND a.state='ready_for_next_start'", (tenant,owner,worker_id,application_id or "")):
            yield {"files":[{"scope":"workspace", **entry} for entry in json.loads(row["payload_json"]).get("files",[])]}
