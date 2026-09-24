"""Read authorized Library content and exact granted peer results through their owners."""

import json
from .library_registry import activation_bundle_for_profile, validate_library_manifest
from .peer_collaboration import PeerError


def authorized_sources(store, peers, worker):
    result = []
    # Library activation already owns executable tools/files in the bootstrap. This read path
    # exposes its reviewed text verbatim and checks the active grant on every request.
    with store._connect() as conn:
        rows = conn.execute(
            """SELECT g.grant_id,l.library_id,l.manifest_json,l.content_hash
        FROM workspace_capability_grants g JOIN control_plane_library l ON l.library_id=g.library_id
        WHERE g.worker_id=? AND g.tenant_id=? AND g.owner_id=? AND g.revoked_at IS NULL AND l.status='available'""",
            (worker["worker_id"], worker["tenant_id"], worker["owner_id"]),
        ).fetchall()
    for row in rows:
        manifest = validate_library_manifest(json.loads(row["manifest_json"]))
        if manifest["content_hash"] != row["content_hash"]:
            continue
        bundle = activation_bundle_for_profile(manifest, worker["profile"])
        for key in ("project_definition", "developer_instructions"):
            if isinstance(bundle.get(key), str) and bundle[key]:
                result.append(
                    {
                        "id": f"library:{row['grant_id']}:{key}",
                        "label": str(manifest["stable_id"]),
                        "kind": "library",
                        "text": bundle[key],
                    }
                )
        for index, entry in enumerate(bundle.get("files") or []):
            if isinstance(entry, dict) and isinstance(entry.get("content"), str):
                result.append(
                    {
                        "id": f"library:{row['grant_id']}:file:{index}",
                        "label": str(entry.get("path") or manifest["stable_id"]),
                        "kind": "library",
                        "text": entry["content"],
                    }
                )
    # Dispatch from the same owner explicitly binds a worker run to its Main conversation.
    # Share exact accepted user messages and visible assistant content, never raw native
    # request/configuration/token records. Configuration may select fewer of these sources.
    with store._connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_goals'"
        ).fetchone()
        turns = []
        if exists:
            turns = conn.execute(
                """SELECT DISTINCT c.conversation_id,t.turn_id,t.message,t.response_json
            FROM coordinator_goals g JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id
            JOIN runs r ON r.run_id=g.run_id JOIN coordinator_turns t ON t.conversation_id=c.conversation_id
            WHERE r.worker_id=? AND c.tenant_id=? AND c.owner_id=? ORDER BY t.created_at,t.turn_id""",
                (worker["worker_id"], worker["tenant_id"], worker["owner_id"]),
            ).fetchall()
    for turn in turns:
        prefix = f"conversation:{turn['conversation_id']}:{turn['turn_id']}"
        result.append(
            {
                "id": prefix + ":user",
                "label": "Conversation message",
                "kind": "conversation",
                "text": turn["message"],
            }
        )
        response = json.loads(turn["response_json"] or "{}")
        for index, choice in enumerate(response.get("choices") or []):
            content = (choice.get("message") or {}).get("content")
            if isinstance(content, str) and content:
                result.append(
                    {
                        "id": prefix + f":assistant:{index}",
                        "label": "Conversation reply",
                        "kind": "conversation",
                        "text": content,
                    }
                )
    if peers:
        for grant in peers.grants(
            worker["worker_id"],
            tenant_id=worker["tenant_id"],
            owner_id=worker["owner_id"],
        )["items"]:
            if (
                grant["source_worker_id"] != worker["worker_id"]
                or "context_read" not in grant["scopes"]
                or grant["revoked_at"]
            ):
                continue
            for run_id in grant["resource_ids"]:
                try:
                    row = peers.read_context(
                        worker["worker_id"],
                        grant["target_worker_id"],
                        grant["grant_id"],
                        run_id,
                        tenant_id=worker["tenant_id"],
                        owner_id=worker["owner_id"],
                    )
                except PeerError:
                    continue
                result.append(
                    {
                        "id": f"peer:{grant['grant_id']}:{run_id}",
                        "label": f"Result {run_id}",
                        "kind": "peer_result",
                        "text": str(row.get("output_text") or ""),
                    }
                )
    return result
