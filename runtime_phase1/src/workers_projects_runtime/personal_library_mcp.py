"""Owner MCP tools over the same HTTP Library service; not the Queen toolset."""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlencode


def personal_library_tool_manifest():
    path = Path(__file__).with_name("prompts") / "personal-library-tools.json"
    raw = path.read_bytes()
    return json.loads(raw) | {"sha256": hashlib.sha256(raw).hexdigest(), "source": "workers_projects_runtime/prompts/personal-library-tools.json"}


def register_personal_library_tools(server, client):
    descriptions = personal_library_tool_manifest()["tools"]

    def call(method, path, payload=None):
        # Owner-private management never uses the attempt-scoped peer reader.
        return client._request(method, path, json_body=payload, require_write_scope=True)

    @server.tool(name="personal_library_list", description=descriptions["personal_library_list"])
    def listing(include_archived: bool = False) -> dict:
        return call("GET", "/v1/personal-library?" + urlencode({"include_archived": str(include_archived).lower()}))

    @server.tool(name="personal_library_read", description=descriptions["personal_library_read"])
    def read(item_id: str, version_id: str | None = None) -> dict:
        path = "/v1/personal-library/" + quote(item_id, safe="")
        if version_id is not None:
            path += "?" + urlencode({"version_id": version_id})
        return call("GET", path)

    @server.tool(name="personal_library_save", description=descriptions["personal_library_save"])
    def save(item: dict, idempotency_key: str) -> dict:
        return call("POST", "/v1/personal-library", {**item, "idempotency_key": idempotency_key})

    @server.tool(name="personal_library_revise", description=descriptions["personal_library_revise"])
    def revise(item_id: str, item: dict, expected_version_id: str) -> dict:
        return call("PATCH", "/v1/personal-library/" + quote(item_id, safe=""), {**item, "expected_version_id": expected_version_id})

    @server.tool(name="personal_library_archive", description=descriptions["personal_library_archive"])
    def archive(item_id: str, expected_version_id: str, archived: bool = True) -> dict:
        return call("POST", "/v1/personal-library/" + quote(item_id, safe="") + "/archive", {"archived": archived, "expected_version_id": expected_version_id})

    @server.tool(name="personal_library_delete", description=descriptions["personal_library_delete"])
    def delete(item_id: str, confirm: bool = False) -> dict:
        return call("DELETE", "/v1/personal-library/" + quote(item_id, safe="") + "?" + urlencode({"confirm": str(confirm).lower()}))

    @server.tool(name="personal_library_resolve", description=descriptions["personal_library_resolve"])
    def resolve(refs: list[dict[str, str]]) -> dict:
        return call("POST", "/v1/personal-library/resolve", {"refs": refs})

    @server.tool(name="personal_library_capture", description=descriptions["personal_library_capture"])
    def capture(worker_id: str, file_id: str, revision: str, name: str, idempotency_key: str) -> dict:
        return call("POST", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library/capture", {"file_id": file_id, "revision": revision, "name": name, "idempotency_key": idempotency_key})

    @server.tool(name="personal_library_workspace", description=descriptions["personal_library_workspace"])
    def workspace(worker_id: str) -> dict:
        return call("GET", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library")

    @server.tool(name="personal_library_apply", description=descriptions["personal_library_apply"])
    def apply(worker_id: str, refs: list[dict[str, str]], request_key: str, confirm_native: bool = False) -> dict:
        return call("POST", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library", {"refs": refs, "request_key": request_key, "confirm_native": confirm_native})

    @server.tool(name="personal_library_remove", description=descriptions["personal_library_remove"])
    def remove(worker_id: str, application_id: str) -> dict:
        return call("DELETE", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library/{quote(application_id, safe='')}")

    @server.tool(name="personal_library_undo", description=descriptions["personal_library_undo"])
    def undo(worker_id: str, application_id: str) -> dict:
        return call("POST", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library/{quote(application_id, safe='')}/undo", {})
