from __future__ import annotations

import asyncio

from fastmcp import Client

from workers_projects_runtime.mcp_server import create_mcp_server


class ControlPlaneApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def workspace_catalog(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"items": [{"worker_id": "wrk_public_safe", "name": "Research desk"}], "next_cursor": None}

    def update_workspace(self, worker_id, payload):
        self.calls.append(("rename", {"worker_id": worker_id, **payload}))
        return {"worker_id": worker_id, **payload}

    def duplicate_workspace(self, worker_id, *, idempotency_key, name=""):
        self.calls.append(
            (
                "duplicate",
                {"worker_id": worker_id, "idempotency_key": idempotency_key, "name": name},
            )
        )
        return {"workspace": {"worker_id": "wrk_copy", "name": name}}

    def workspace_templates(self):
        self.calls.append(("template_list", {}))
        return [{"template_id": "wst_public_safe", "name": "Research template", "version": 1}]

    def save_workspace_template(self, worker_id, *, name, description="", lineage_id=""):
        self.calls.append(
            (
                "template_save",
                {
                    "worker_id": worker_id,
                    "name": name,
                    "description": description,
                    "lineage_id": lineage_id,
                },
            )
        )
        return {"template_id": "wst_public_safe", "name": name, "version": 1}

    def instantiate_workspace_template(self, template_id, *, idempotency_key, name=""):
        self.calls.append(
            (
                "template_instantiate",
                {"template_id": template_id, "idempotency_key": idempotency_key, "name": name},
            )
        )
        return {
            "workspace": {"worker_id": "wrk_from_template", "name": name, "state": "paused"},
            "approvals_required": [{"library_id": "lib_public_safe", "approval_required": True}],
        }

    def provider_accounts(self):
        return [{"account_id": "acct_public_safe", "provider": "codex", "status": "ready"}]

    def create_provider_account(self, **payload):
        self.calls.append(("account_connect", payload))
        return {"account_id": "acct_connected", "status": "disconnected", **payload}

    def start_provider_account_setup(self, account_id):
        self.calls.append(("account_setup", account_id))
        return {"account_id": account_id, "status": "connecting", "instructions": "Use device login"}

    def verify_provider_account(self, account_id):
        self.calls.append(("account_test", account_id))
        return {"account_id": account_id, "status": "ready", "complete": True}

    def disconnect_provider_account(self, account_id):
        self.calls.append(("disconnect", account_id))
        return {"account_id": account_id, "status": "disconnected"}

    def forget_provider_account(self, account_id):
        self.calls.append(("account_forget", account_id))
        return {"account_id": account_id, "status": "forgotten"}

    def connections(self):
        return [{"connection_id": "conn_public_safe", "status": "ready", "scopes": ["documents:read"]}]

    def library(self):
        return [{"library_id": "lib_public_safe", "status": "available", "scopes": ["documents:read"]}]

    def propose_library_manifest(self, manifest):
        self.calls.append(("library_propose", manifest))
        return {"proposal_id": "lprop_public_safe", "status": "pending"}

    def create_pending_change(self, payload):
        self.calls.append(("prepare", payload))
        return {
            "change_id": "chg_public_safe",
            "confirmation_token": "opaque-synthetic-confirmation-token",
            "status": "pending",
        }

    def workspace_grants(self, worker_id):
        self.calls.append(("grants", worker_id))
        return [{"grant_id": "grant_public_safe", "worker_id": worker_id}]

    def revoke_workspace_grant(self, worker_id, grant_id):
        self.calls.append(("revoke", {"worker_id": worker_id, "grant_id": grant_id}))
        return {"grant_id": grant_id, "worker_id": worker_id, "revoked_at": 1}

    def activity(self, *, limit=50):
        return [{"event_id": "evt_public_safe", "event_type": "worker.created"}]


def _json(result):
    payload = result.structured_content or {}
    return payload.get("result", payload)


def test_control_plane_mcp_tools_are_additive_and_require_browser_confirmation(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.com")
    api = ControlPlaneApi()
    server = create_mcp_server(api_client=api)

    async def scenario() -> None:
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            tool_names = set(tools)
            assert {
                "workspace_list",
                "workspace_rename",
                "workspace_duplicate",
                "workspace_template_list",
                "workspace_template_save",
                "workspace_template_instantiate",
                "worker_accounts_list",
                "worker_account_connect",
                "worker_account_setup_start",
                "worker_account_test",
                "worker_account_disconnect",
                "worker_account_forget",
                "connections_list",
                "library_list",
                "library_manifest_propose",
                "workspace_capability_prepare",
                "workspace_capability_upgrade_prepare",
                "workspace_provider_account_prepare",
                "workspace_capabilities_list",
                "workspace_capability_remove",
                "workspace_activity",
            } <= tool_names
            prepare_properties = tools["workspace_capability_prepare"].inputSchema["properties"]
            assert set(prepare_properties) == {"worker_id", "library_id", "scopes"}
            assert not any("publish" in name or "confirm" in name for name in tool_names if "library" in name)
            duplicate_schema = tools["workspace_duplicate"].inputSchema
            assert "idempotency_key" in duplicate_schema["required"]
            assert duplicate_schema["properties"]["idempotency_key"]["minLength"] == 8
            assert duplicate_schema["properties"]["idempotency_key"]["maxLength"] == 128

            workspace_list_schema = tools["workspace_list"].inputSchema["properties"]["limit"]
            assert "maximum" not in workspace_list_schema

            listed = await client.call_tool("workspace_list", {"search": "research", "tags": ["quarterly"]})
            listed_with_large_limit = await client.call_tool("workspace_list", {"limit": 200})
            renamed = await client.call_tool(
                "workspace_rename", {"worker_id": "wrk_public_safe", "name": "Quarterly research"}
            )
            duplicated = await client.call_tool(
                "workspace_duplicate",
                {
                    "worker_id": "wrk_public_safe",
                    "idempotency_key": "duplicate-public-safe-1",
                    "name": "Quarterly research copy",
                },
            )
            templates = await client.call_tool("workspace_template_list", {})
            saved_template = await client.call_tool(
                "workspace_template_save",
                {
                    "worker_id": "wrk_public_safe",
                    "name": "Quarterly research template",
                    "description": "Synthetic reusable intent",
                },
            )
            instantiated_template = await client.call_tool(
                "workspace_template_instantiate",
                {
                    "template_id": "wst_public_safe",
                    "idempotency_key": "template-public-safe-1",
                    "name": "Fresh quarterly research",
                },
            )
            proposal = await client.call_tool(
                "library_manifest_propose",
                {"manifest": {"schema_version": 1, "stable_id": "skill.synthetic.proposal"}},
            )
            prepared = await client.call_tool(
                "workspace_capability_prepare",
                {
                    "worker_id": "wrk_public_safe",
                    "library_id": "lib_public_safe",
                    "scopes": ["documents:read"],
                },
            )
            upgrade_prepared = await client.call_tool(
                "workspace_capability_upgrade_prepare",
                {
                    "worker_id": "wrk_public_safe",
                    "library_id": "lib_public_safe_v2",
                    "replaces_grant_id": "grant_public_safe",
                    "scopes": ["documents:read"],
                },
            )
            provider_prepared = await client.call_tool(
                "workspace_provider_account_prepare",
                {
                    "worker_id": "wrk_public_safe",
                    "policy": "personal_required",
                    "account_id": "acct_public_safe",
                },
            )
            grants = await client.call_tool(
                "workspace_capabilities_list", {"worker_id": "wrk_public_safe"}
            )
            removed = await client.call_tool(
                "workspace_capability_remove",
                {"worker_id": "wrk_public_safe", "grant_id": "grant_public_safe"},
            )
            disconnected = await client.call_tool(
                "worker_account_disconnect", {"account_id": "acct_public_safe"}
            )
            connected = await client.call_tool(
                "worker_account_connect",
                {
                    "provider": "codex",
                    "label": "Personal Codex",
                    "auth_method": "subscription",
                    "make_default": True,
                },
            )
            setup = await client.call_tool(
                "worker_account_setup_start", {"account_id": "acct_connected"}
            )
            tested = await client.call_tool(
                "worker_account_test", {"account_id": "acct_connected"}
            )
            forgotten = await client.call_tool(
                "worker_account_forget", {"account_id": "acct_public_safe"}
            )

            assert _json(listed)["items"][0]["name"] == "Research desk"
            assert _json(listed_with_large_limit)["items"][0]["name"] == "Research desk"
            assert _json(renamed)["name"] == "Quarterly research"
            assert _json(duplicated)["workspace"]["worker_id"] == "wrk_copy"
            assert _json(templates)[0]["template_id"] == "wst_public_safe"
            assert _json(saved_template)["version"] == 1
            assert _json(instantiated_template)["workspace"]["state"] == "paused"
            assert _json(instantiated_template)["approvals_required"][0]["approval_required"] is True
            assert _json(proposal) == {"proposal_id": "lprop_public_safe", "status": "pending"}
            prepared_payload = _json(prepared)
            assert prepared_payload["requires_human_confirmation"] is True
            assert prepared_payload["confirmation_url"].startswith(
                "https://glasshive.example.com/confirm-change#change_id=chg_public_safe&token="
            )
            assert "confirmation_token" not in prepared_payload
            upgrade_prepared_payload = _json(upgrade_prepared)
            assert upgrade_prepared_payload["requires_human_confirmation"] is True
            assert "confirmation_token" not in upgrade_prepared_payload
            provider_prepared_payload = _json(provider_prepared)
            assert provider_prepared_payload["requires_human_confirmation"] is True
            assert provider_prepared_payload["confirmation_url"].startswith(
                "https://glasshive.example.com/confirm-change#change_id=chg_public_safe&token="
            )
            assert _json(grants)[0]["grant_id"] == "grant_public_safe"
            assert _json(removed)["revoked_at"] == 1
            assert _json(disconnected)["status"] == "disconnected"
            assert _json(connected)["account_id"] == "acct_connected"
            assert _json(setup)["status"] == "connecting"
            assert _json(tested)["status"] == "ready"
            assert _json(forgotten)["status"] == "forgotten"

    asyncio.run(scenario())
    list_calls = [call for call in api.calls if call[0] == "list"]
    assert list_calls[1][1]["limit"] == 100
    prepare_calls = [call for call in api.calls if call[0] == "prepare"]
    assert prepare_calls[0][1]["payload"] == {
        "library_id": "lib_public_safe",
        "scopes": ["documents:read"],
    }
    assert prepare_calls[1][1] == {
        "change_type": "library_upgrade",
        "target_id": "wrk_public_safe",
        "payload": {
            "library_id": "lib_public_safe_v2",
            "replaces_grant_id": "grant_public_safe",
            "scopes": ["documents:read"],
        },
    }
    assert prepare_calls[2][1] == {
        "change_type": "workspace_provider_account",
        "target_id": "wrk_public_safe",
        "payload": {"policy": "personal_required", "account_id": "acct_public_safe"},
    }
    assert next(call for call in api.calls if call[0] == "duplicate")[1] == {
        "worker_id": "wrk_public_safe",
        "idempotency_key": "duplicate-public-safe-1",
        "name": "Quarterly research copy",
    }
