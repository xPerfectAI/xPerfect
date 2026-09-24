from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Callable
from urllib.parse import quote, urlencode

import httpx


class RuntimeClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_sec: float = 120.0,
        headers: dict[str, str] | None = None,
        headers_factory: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("GLASSHIVE_RUNTIME_BASE_URL", "http://127.0.0.1:8766")).rstrip("/")
        self.timeout_sec = timeout_sec
        self.headers = dict(headers or {})
        self.headers_factory = headers_factory

    def _request_headers(self) -> dict[str, str] | None:
        resolved = dict(self.headers)
        if self.headers_factory is not None:
            resolved.update(
                {
                    key: value
                    for key, value in self.headers_factory().items()
                    if value
                }
            )
        return resolved or None

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=self._request_headers(),
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

    def file_request(self, method: str, path: str, payload: dict | None = None):
        return self._request(method,path,json_body=payload)

    async def upload_file_content(self, upload_id: str, chunks: Any) -> dict[str, Any]:
        path = f"/v1/file-uploads/{quote(upload_id, safe='')}/content"
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            response = await client.put(
                f"{self.base_url}{path}",
                headers={**(self._request_headers() or {}), "Content-Type": "application/octet-stream"},
                content=chunks,
            )
            response.raise_for_status()
            return response.json()

    @asynccontextmanager
    async def stream_file_download(self,path:str,headers:dict[str,str],*,method:str="GET",payload:dict|None=None):
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            async with client.stream(method,f"{self.base_url}{path}",headers={**(self._request_headers() or {}),**headers},json=payload) as response:
                if response.is_error:
                    await response.aread()
                    response.raise_for_status()
                yield response

    def with_headers(self, headers: dict[str, str]) -> "RuntimeClient":
        merged = dict(self.headers)
        merged.update({key: value for key, value in headers.items() if value})
        return RuntimeClient(
            self.base_url,
            self.timeout_sec,
            merged,
            headers_factory=self.headers_factory,
        )

    def with_headers_factory(
        self,
        headers_factory: Callable[[], dict[str, str]],
    ) -> "RuntimeClient":
        return RuntimeClient(
            self.base_url,
            self.timeout_sec,
            self.headers,
            headers_factory=headers_factory,
        )

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def list_projects(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/projects").get("items", [])

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/projects/{project_id}")

    def get_preferences(self) -> dict[str, Any]:
        return self._request("GET", "/v1/preferences")

    def grok_native_models(self) -> dict[str, Any]:
        return self._request("GET", "/v1/native-models/grok-build")

    def owner_storage(self) -> dict[str, Any]:
        return self._request("GET", "/v1/storage")

    def provider_readiness(self, profile: str) -> dict[str, str]:
        return self._request(
            "GET",
            f"/v1/provider-readiness/{quote(str(profile or '').strip(), safe='')}",
        )

    def update_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", "/v1/preferences", json_body=payload)

    def list_workers(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/workers").get("items", [])

    def list_workspace_catalog(
        self,
        *,
        kind: str = "named",
        search: str = "",
        tags: str = "",
        favorite: bool | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        query: dict[str, str] = {"kind": kind, "search": search, "tags": tags, "limit": str(limit)}
        if favorite is not None:
            query["favorite"] = "true" if favorite else "false"
        if cursor:
            query["cursor"] = cursor
        return self._request("GET", f"/v1/workspaces?{urlencode(query)}")

    def duplicate_workspace(
        self,
        worker_id: str,
        *,
        idempotency_key: str,
        name: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"idempotency_key": idempotency_key}
        if name:
            payload["name"] = name
        return self._request(
            "POST",
            f"/v1/workspaces/{quote(worker_id, safe='')}/duplicate",
            json_body=payload,
        )

    def list_workspace_templates(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/workspace-templates").get("items", [])

    def save_workspace_template(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/workspaces/{quote(worker_id, safe='')}/templates",
            json_body=payload,
        )

    def instantiate_workspace_template(
        self,
        template_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/workspace-templates/{quote(template_id, safe='')}/instantiate",
            json_body=payload,
        )

    def current_user(self) -> dict[str, Any]:
        return self._request("GET", "/v1/me")

    def set_schedule_principal_authority(
        self,
        principal_id: str,
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/v1/admin/principals/{quote(principal_id, safe='')}/schedule-authority",
            json_body={"enabled": enabled},
        )

    def current_native_claude_status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/provider-accounts/current-native/claude")

    def connect_current_native_claude(self) -> dict[str, Any]:
        return self._request("POST", "/v1/provider-accounts/current-native/claude")

    def list_provider_accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/provider-accounts").get("items", [])

    def provider_account_capabilities(self) -> dict[str, list[str]]:
        return self._request("GET", "/v1/provider-accounts/capabilities").get("profile_providers", {})

    def create_provider_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/provider-accounts", json_body=payload)

    def connect_provider_api_key(self, account_id: str, value: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/provider-accounts/{quote(account_id, safe='')}/credentials", json_body={"value": value})

    def start_provider_account_setup(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/provider-accounts/{quote(account_id, safe='')}/setup"
        )

    def provider_account_setup_status(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/provider-accounts/{quote(account_id, safe='')}/setup"
        )

    def submit_provider_account_setup_input(
        self, account_id: str, value: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/provider-accounts/{quote(account_id, safe='')}/setup/input",
            json_body={"value": value},
        )

    def cancel_provider_account_setup(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/provider-accounts/{quote(account_id, safe='')}/setup/cancel",
        )

    def verify_provider_account(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/provider-accounts/{quote(account_id, safe='')}/verify"
        )

    def disconnect_provider_account(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/provider-accounts/{quote(account_id, safe='')}/disconnect",
        )

    def forget_provider_account(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/v1/provider-accounts/{quote(account_id, safe='')}"
        )

    def list_connections(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/connections").get("items", [])

    def list_library(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/library").get("items", [])

    def create_pending_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/pending-changes", json_body=payload)

    def list_workspace_grants(self, worker_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/v1/workspaces/{quote(worker_id, safe='')}/capability-grants",
        ).get("items", [])

    def revoke_workspace_grant(self, worker_id: str, grant_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/workspaces/{quote(worker_id, safe='')}/capability-grants/{quote(grant_id, safe='')}",
        )

    def get_pending_change(self, change_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/pending-changes/{quote(change_id, safe='')}")

    def confirm_pending_change(self, change_id: str, confirmation_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/pending-changes/{change_id}/confirm",
            json_body={"confirmation_token": confirmation_token},
        )

    def list_activity(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        return self._request("GET", f"/v1/activity?limit={bounded_limit}").get("items", [])

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}")

    def worker_live(self, worker_id: str, *, compact: bool = False) -> dict[str, Any]:
        suffix = "?compact=1" if compact else ""
        return self._request("GET", f"/v1/workers/{worker_id}/live{suffix}")

    def native_control_state(self, worker_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/workers/{quote(worker_id, safe='')}/native-control",
        )

    def native_control(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/workers/{quote(worker_id, safe='')}/native-control",
            json_body=payload,
        )

    def record_worker_view_open(self, worker_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/view-opened")

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/projects",
            json_body={
                "owner_id": owner_id,
                "title": title,
                "goal": goal,
                "default_worker_profile": default_worker_profile,
            },
        )

    def create_execution_workspace(
        self,
        project_id: str,
        *,
        execution_mode: str = "docker",
        file_placement: str = "common",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{quote(project_id, safe='')}/execution-workspaces",
            json_body={
                "mode": "shared",
                "execution_mode": execution_mode,
                "file_placement": file_placement,
            },
        )

    def add_execution_workspace_member(
        self,
        workspace_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/workspaces/{quote(workspace_id, safe='')}/members",
            json_body=payload,
        )

    def execution_workspace_members(self, workspace_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/workspaces/{quote(workspace_id, safe='')}/members",
        )

    def create_worker(
        self,
        project_id: str,
        owner_id: str,
        profile: str,
        *,
        name: str = "Main Workspace",
        role: str = "main",
        bootstrap_bundle: dict[str, Any] | None = None,
        execution_mode: str = "docker",
        start_synchronously: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/workers",
            json_body={
                "owner_id": owner_id,
                "name": name,
                "role": role,
                "profile": profile,
                "execution_mode": execution_mode,
                # The runtime chooses the bootstrap projection for its own
                # execution substrate; a packaged shared member has no host login.
                "bootstrap_bundle": bootstrap_bundle,
                "start_synchronously": start_synchronously,
                "workspace_kind": "ephemeral",
            },
        )

    def duplicate_worker(
        self,
        project_id: str,
        source_worker_id: str,
        owner_id: str,
        *,
        name: str = "Main Workspace",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/workers/duplicate",
            json_body={
                "owner_id": owner_id,
                "source_worker_id": source_worker_id,
                "name": name,
                "role": "main",
            },
        )

    def assign_run(self, worker_id: str, instruction: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/assign", json_body={"instruction": instruction})

    def schedule_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        schedule_text: str | None = None,
        run_at: str | None = None,
        delay_seconds: int | None = None,
        file_upload_ids: list[str] | None = None,
        file_upload_revisions: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"instruction": instruction}
        if file_upload_ids:
            payload["file_upload_ids"] = file_upload_ids
        if file_upload_revisions:
            payload["file_upload_revisions"] = file_upload_revisions
        if schedule_text:
            payload["schedule_text"] = schedule_text
        if run_at:
            payload["run_at"] = run_at
        if delay_seconds is not None:
            payload["delay_seconds"] = delay_seconds
        return self._request("POST", f"/v1/workers/{worker_id}/schedule", json_body=payload)

    def create_recurring_schedule(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/workers/{quote(worker_id, safe='')}/recurring-schedules",
            json_body=payload,
        )

    def recurring_schedules(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        suffix = "?include_inactive=true" if include_inactive else ""
        return self._request("GET", f"/v1/recurring-schedules{suffix}").get("items", [])

    def recurring_schedule_occurrences(
        self,
        definition_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 100))
        return self._request(
            "GET",
            f"/v1/recurring-schedules/{quote(definition_id, safe='')}/occurrences?limit={bounded_limit}",
        ).get("items", [])

    def deactivate_recurring_schedule(self, definition_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/recurring-schedules/{quote(definition_id, safe='')}/deactivate",
        )

    def update_recurring_schedule(
        self,
        definition_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/v1/recurring-schedules/{quote(definition_id, safe='')}",
            json_body=payload,
        )

    def retire_recurring_schedule(self, definition_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/recurring-schedules/{quote(definition_id, safe='')}",
        )

    def run_recurring_schedule_now(
        self,
        definition_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/recurring-schedules/{quote(definition_id, safe='')}/run-now",
            json_body={"idempotency_key": idempotency_key},
        )

    def update_worker_metadata(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/v1/workspaces/{quote(worker_id, safe='')}",
            json_body=payload,
        )

    def launch_failed(self, worker_id: str, reason: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/launch-failed", json_body={"reason": reason})

    def message(self, worker_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/message", json_body={"message": message})

    def steer(self, worker_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/steer", json_body={"message": message})

    def lifecycle(self, worker_id: str, action: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/{action}")

    def desktop_action(
        self,
        worker_id: str,
        action: str,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if url:
            payload["url"] = url
        if run_id:
            payload["run_id"] = run_id
        return self._request("POST", f"/v1/workers/{worker_id}/desktop-action", json_body=payload)
