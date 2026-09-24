"""MCP Files operations use the authenticated runtime HTTP authority.

Large payloads travel through authenticated streaming routes, never MCP text,
base64, server-local paths, or returned bearer credentials.
"""
from __future__ import annotations

from urllib.parse import urlencode

from mcp.types import ToolAnnotations
from pydantic import Field
from typing import Annotated, Any, Literal


FileUploadIdsParam = Annotated[list[str] | None, Field(description="Exact ready upload_id values from the authenticated stored draft. Do not mix with uploaded_files or bootstrap files.")]
FileBindingKeyParam = Annotated[str | None, Field(min_length=1, max_length=128, description="Stable retry key for binding stored files before an existing or legacy workspace run. Required with file_upload_ids on these paths; atomic new delegation uses its existing operation key.")]


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


class WorkspaceFilesMcpClient:
    def __init__(self, client):
        self.client = client

    def worker_path(self, worker_id: str) -> str:
        return "/v1/workers/" + self.client._path_id(worker_id, "worker_id")

    def upload_path(self, upload_id: str) -> str:
        return "/v1/file-uploads/" + self.client._path_id(upload_id, "upload_id")

    def file_path(self, worker_id: str, file_id: str) -> str:
        return self.worker_path(worker_id) + "/files/" + self.client._path_id(file_id, "file_id")

    def request(self, method, path, payload=None):
        return self.client._request(method, path, json_body=payload)

    def prepare_export(self, path, payload):
        return self.client._request("POST", path, json_body=payload, file_export_prepare=True)

    def bind(self, worker_id, upload_ids, idempotency_key, directory=None):
        accepted = self.request("POST", self.worker_path(worker_id) + "/files", {
            "upload_ids": upload_ids, "idempotency_key": idempotency_key, "directory": directory,
        })
        if [item.get("upload_id") for item in accepted.get("items", [])] != upload_ids or accepted.get("state") not in {"accepted", "available"}:
            raise ValueError("Runtime did not acknowledge the exact stored file binding")
        return accepted


def validate_file_transport(file_upload_ids, uploaded_files=None, bundle=None, binding_key=None, *, bind=False):
    if not file_upload_ids:
        return
    if uploaded_files or (bundle and bundle.get("files")):
        raise ValueError("Choose exact file_upload_ids or uploaded/bootstrap files for this request")
    if len(set(file_upload_ids)) != len(file_upload_ids):
        raise ValueError("file_upload_ids must contain each accepted upload once")
    if bind and not binding_key:
        raise ValueError("file_binding_key is required with file_upload_ids")


def install_workspace_file_tools(server, api_client):
    files = WorkspaceFilesMcpClient(api_client)

    @server.tool(name="files_storage", annotations=READ, structured_output=True)
    def storage() -> dict[str, Any]:
        """Read this authenticated owner's storage policy, meter and remaining reservations."""
        return files.request("GET", "/v1/storage")

    @server.tool(name="files_storage_policy", annotations=WRITE, structured_output=True)
    def storage_policy(values: dict[Literal["storage_limit_bytes", "max_file_bytes", "max_batch_files", "max_batch_bytes"], int | None], inherit: bool = False) -> dict[str, Any]:
        """Update owner storage policy through runtime authorization and quota transition rules. Omitted values stay unchanged; null requests unlimited where permitted."""
        return files.request("PATCH", "/v1/storage/policy", {**values, "inherit": inherit})

    @server.tool(name="workspace_files_list", annotations=READ, structured_output=True)
    def listing(worker_id: str, directory: str = "", cursor: Annotated[int, Field(ge=0)] = 0) -> dict[str, Any]:
        """List files and folders in one admitted workspace directory. Follow next_cursor; IDs and revisions are runtime-owned."""
        return files.request("GET", files.worker_path(worker_id) + "/files?" + urlencode({"directory": directory, "cursor": cursor}))

    @server.tool(name="workspace_files_mkdir", annotations=WRITE, structured_output=True)
    def mkdir(worker_id: str, path: str) -> dict[str, Any]:
        """Create a workspace-relative folder; existing names and private paths are rejected by runtime."""
        return files.request("POST", files.worker_path(worker_id) + "/directories", {"path": path})

    @server.tool(name="workspace_files_move", annotations=WRITE, structured_output=True)
    def move(worker_id: str, file_id: str, revision: str, path: str) -> dict[str, Any]:
        """Rename or move a file or folder to a workspace-relative path using its current revision. Conflicts never overwrite."""
        return files.request("PATCH", files.file_path(worker_id, file_id), {"revision": revision, "path": path})

    @server.tool(name="workspace_files_remove", annotations=WRITE, structured_output=True)
    def remove(worker_id: str, file_id: str, revision: str) -> dict[str, Any]:
        """Move a file or folder to recoverable Trash. Keep the returned undo_id; this does not purge retained bytes."""
        return files.request("DELETE", files.file_path(worker_id, file_id), {"revision": revision})

    @server.tool(name="workspace_files_trash", annotations=WRITE, structured_output=True)
    def trash(worker_id: str) -> dict[str, Any]:
        """Reload recoverable deleted files and folders, including their durable Undo identities."""
        return api_client._request("GET", files.worker_path(worker_id) + "/files/trash", require_write_scope=True)

    @server.tool(name="workspace_files_restore", annotations=WRITE, structured_output=True)
    def restore(worker_id: str, file_id: str, undo_id: str) -> dict[str, Any]:
        """Restore a removed file or folder with its original ID. A conflicting destination is rejected."""
        return files.request("POST", files.file_path(worker_id, file_id) + "/restore", {"undo_id": undo_id})

    @server.tool(name="workspace_files_bind", annotations=WRITE, structured_output=True)
    def bind(worker_id: str, upload_ids: list[str], idempotency_key: str, directory: str | None = None) -> dict[str, Any]:
        """Attach these exact ready stored upload versions to a workspace. Retry the same key and IDs; runtime owns pins, scope, quota and publication."""
        return files.bind(worker_id, upload_ids, idempotency_key, directory)

    @server.tool(name="file_uploads_list", annotations=READ, structured_output=True)
    def uploads(draft_id: str) -> dict[str, Any]:
        """Reload durable receipts for an owner draft. Use ready upload_id references for bind, launch or schedule."""
        return files.request("GET", "/v1/file-uploads?" + urlencode({"draft_id": draft_id}))

    @server.tool(name="file_upload_begin", annotations=WRITE, structured_output=True)
    def begin(draft_id: str, name: str, size_bytes: Annotated[int, Field(ge=0)], idempotency_key: str, relative_path: str = "") -> dict[str, Any]:
        """Reserve an exact-size upload through runtime quota admission. The caller's authenticated transport must stream raw bytes to upload_request; a receipt alone is not a completed upload. No model text, base64 or local server path is accepted."""
        receipt = files.request("POST", "/v1/file-uploads", {
            "draft_id": draft_id, "name": name, "size_bytes": size_bytes,
            "idempotency_key": idempotency_key, "relative_path": relative_path,
        })
        return {**receipt, "upload_request": {"method": "PUT", "path": files.upload_path(receipt["upload_id"]) + "/content",
            "media_type": "application/octet-stream", "authentication_required": True, "size_bytes": receipt["size_bytes"]}}

    @server.tool(name="file_upload_cancel", annotations=WRITE, structured_output=True)
    def cancel(upload_id: str) -> dict[str, Any]:
        """Cancel an unattached draft upload through its owner authority. Accepted pinned inputs cannot be cancelled."""
        return files.request("DELETE", files.upload_path(upload_id))

    @server.tool(name="workspace_files_download", annotations=READ, structured_output=True)
    def download(worker_id: str, file_id: str, revision: str | None = None) -> dict[str, Any]:
        """Authorize a file version and return its authenticated download route, without placing bytes or credentials in MCP output."""
        path = files.file_path(worker_id, file_id)
        if revision is not None:
            path += "?" + urlencode({"revision": revision})
        return {**files.request("GET", path), "authentication_required": True}

    @server.tool(name="workspace_files_export", annotations=READ, structured_output=True)
    def export(worker_id: str, file_ids: Annotated[list[str], Field(min_length=1)]) -> dict[str, Any]:
        """Validate selected files/folders and return an authenticated streaming ZIP descriptor. The host retrieves bytes separately; paths are revalidated on use."""
        return {**files.prepare_export(files.worker_path(worker_id) + "/files/export/prepare", {"file_ids": file_ids}), "authentication_required": True}

    @server.tool(name="file_upload_download", annotations=READ, structured_output=True)
    def upload_download(upload_id: str) -> dict[str, Any]:
        """Read the exact ready stored draft receipt and its authenticated retrieval route."""
        return {**files.request("GET", files.upload_path(upload_id)), "authentication_required": True}

    @server.tool(name="file_upload_move", annotations=WRITE, structured_output=True)
    def upload_move(upload_id: str, relative_path: str, revision: str) -> dict[str, Any]:
        """Rename or organize a stored draft file by relative path and current revision. Previously accepted input manifests keep their original metadata."""
        return files.request("PATCH", files.upload_path(upload_id), {"relative_path": relative_path, "revision": revision})

    @server.tool(name="file_uploads_export", annotations=READ, structured_output=True)
    def uploads_export(upload_ids: Annotated[list[str], Field(min_length=1)], revisions: list[str] | None = None) -> dict[str, Any]:
        """Validate exact stored upload selections and return an authenticated ZIP route. Optional revisions correspond to upload_ids in order."""
        payload = {"upload_ids": upload_ids}
        if revisions is not None:
            payload["revisions"] = revisions
        return {**files.prepare_export("/v1/file-uploads/export/prepare", payload), "authentication_required": True}
