from __future__ import annotations

import asyncio

from fastmcp import Client
import pytest

from workers_projects_runtime.mcp_server import WorkersProjectsApiClient, create_mcp_server


class RecurrenceApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_recurring_schedule(self, worker_id: str, payload: dict) -> dict:
        self.calls.append(("create", {"worker_id": worker_id, **payload}))
        return {
            "definition_id": "rsd_public_safe",
            "worker_id": worker_id,
            "active": True,
            "schedule_owner": "viventium_cortex",
            "owner_action": "dispatch_via_viventium_cortex",
            **payload,
        }

    def recurring_schedules(self, worker_id: str | None = None, include_inactive: bool = False) -> list[dict]:
        self.calls.append(("list", {"worker_id": worker_id, "include_inactive": include_inactive}))
        return [{"definition_id": "rsd_public_safe", "worker_id": worker_id, "active": True}]

    def recurring_schedule_occurrences(self, definition_id: str, limit: int = 50) -> list[dict]:
        self.calls.append(("occurrences", {"definition_id": definition_id, "limit": limit}))
        return [{"occurrence_id": "occ_public_safe", "definition_id": definition_id, "state": "completed"}]

    def deactivate_recurring_schedule(self, definition_id: str) -> dict:
        self.calls.append(("deactivate", definition_id))
        return {"definition_id": definition_id, "active": False}

    def update_recurring_schedule(self, definition_id: str, payload: dict) -> dict:
        self.calls.append(("update", {"definition_id": definition_id, **payload}))
        return {"definition_id": definition_id, **payload}

    def run_recurring_schedule_now(self, definition_id: str, idempotency_key: str) -> dict:
        self.calls.append(("run_now", {"definition_id": definition_id, "idempotency_key": idempotency_key}))
        return {"definition_id": definition_id, "status": "scheduled", "schedule_id": "sch_public_safe"}

    def retire_recurring_schedule(self, definition_id: str) -> dict:
        self.calls.append(("retire", definition_id))
        return {"definition_id": definition_id, "active": False, "retired_at": "2027-01-01T00:00:00+00:00"}


def _json(result):
    payload = result.structured_content or {}
    return payload.get("result", payload)


def test_additive_recurring_schedule_mcp_tools_preserve_direct_structured_control():
    api = RecurrenceApi()
    server = create_mcp_server(api_client=api)

    async def scenario() -> None:
        async with Client(server) as client:
            tool_names = {tool.name for tool in await client.list_tools()}
            assert {
                "worker_recurring_schedule_create",
                "worker_recurring_schedules",
                "worker_recurring_schedule_occurrences",
                "worker_recurring_schedule_deactivate",
                "worker_recurring_schedule_run_now",
                "worker_recurring_schedule_retire",
            } <= tool_names

            created = await client.call_tool(
                "worker_recurring_schedule_create",
                {
                    "worker_id": "wrk_public_safe",
                    "instruction": "Run the synthetic check.",
                    "recurrence_type": "interval",
                    "interval_seconds": 3600,
                    "timezone_name": "UTC",
                },
            )
            listed = await client.call_tool(
                "worker_recurring_schedules",
                {"worker_id": "wrk_public_safe", "include_inactive": True},
            )
            occurrences = await client.call_tool(
                "worker_recurring_schedule_occurrences",
                {"definition_id": "rsd_public_safe", "limit": 10},
            )
            deactivated = await client.call_tool(
                "worker_recurring_schedule_deactivate",
                {"definition_id": "rsd_public_safe"},
            )
            run_now = await client.call_tool(
                "worker_recurring_schedule_run_now",
                {"definition_id": "rsd_public_safe", "idempotency_key": "manual-public-safe-1"},
            )
            retired = await client.call_tool(
                "worker_recurring_schedule_retire",
                {"definition_id": "rsd_public_safe"},
            )

            assert _json(created)["definition_id"] == "rsd_public_safe"
            assert _json(created)["schedule_owner"] == "viventium_cortex"
            assert _json(listed)[0]["definition_id"] == "rsd_public_safe"
            assert _json(occurrences)[0]["occurrence_id"] == "occ_public_safe"
            assert _json(deactivated)["active"] is False
            assert _json(run_now)["status"] == "scheduled"
            assert _json(retired)["retired_at"]

    asyncio.run(scenario())
    assert api.calls[0][0] == "create"
    assert api.calls[-1] == ("retire", "rsd_public_safe")


def test_recurring_schedule_mcp_client_validates_ids_before_building_paths():
    class RecordingClient(WorkersProjectsApiClient):
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def _request(self, method: str, path: str, *, json_body=None):
            self.requests.append({"method": method, "path": path, "json_body": json_body})
            return {"items": []} if method == "GET" else {"definition_id": "rsd_public_safe"}

    client = RecordingClient()

    with pytest.raises(ValueError, match="definition_id must be a simple id"):
        client.deactivate_recurring_schedule("../rsd_bad")
    with pytest.raises(ValueError, match="worker_id must be a simple id"):
        client.create_recurring_schedule("wrk_bad/schedules", {"instruction": "No"})
    with pytest.raises(ValueError, match="definition_id must be a simple id"):
        client.run_recurring_schedule_now("../rsd_bad", "manual-public-safe-1")

    client.recurring_schedule_occurrences("rsd_public_safe", limit=500)
    assert client.requests == [
        {
            "method": "GET",
            "path": "/v1/recurring-schedules/rsd_public_safe/occurrences?limit=100",
            "json_body": None,
        }
    ]


def test_recurring_schedule_mcp_create_exposes_the_same_structured_contract_as_http():
    api = RecurrenceApi()
    server = create_mcp_server(api_client=api)

    async def scenario() -> None:
        async with Client(server) as client:
            created = await client.call_tool(
                "worker_recurring_schedule_create",
                {
                    "worker_id": "wrk_public_safe",
                    "instruction": "Run the structured synthetic schedule.",
                    "recurrence_type": "rfc5545",
                    "rrule": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0",
                    "timezone_name": "America/Toronto",
                    "starts_at": "2027-01-01T00:00:00-05:00",
                    "ends_at": "2027-06-30T23:59:59-04:00",
                    "enabled": True,
                    "overlap_policy": "skip",
                    "misfire_grace_seconds": 600,
                    "catch_up_policy": "bounded",
                    "max_catch_up_occurrences": 2,
                    "jitter_seconds": 120,
                },
            )
            payload = _json(created)
            assert payload["recurrence_type"] == "rfc5545"
            assert payload["rrule"].startswith("FREQ=WEEKLY")
            updated = await client.call_tool(
                "worker_recurring_schedule_update",
                {"definition_id": "rsd_public_safe", "enabled": False},
            )
            assert _json(updated)["enabled"] is False

    asyncio.run(scenario())
    request = api.calls[0][1]
    assert request["worker_id"] == "wrk_public_safe"
    assert request["overlap_policy"] == "skip"
    assert request["catch_up_policy"] == "bounded"
    assert request["max_catch_up_occurrences"] == 2
    assert request["jitter_seconds"] == 120
    assert api.calls[-1] == (
        "update",
        {"definition_id": "rsd_public_safe", "enabled": False},
    )
