"""Small carrier checks shared by the public runtime boundaries.

Identity and capability policy stay with their existing authenticated owners. This
module only prevents two transport names from asserting different values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class BoundaryConflict(ValueError):
    """Canonical and compatibility carriers disagree."""


def compatible_value(
    values: Mapping[str, Any], canonical: str, legacy: str, *, label: str,
) -> Any:
    current = values.get(canonical)
    previous = values.get(legacy)
    if current is not None and previous is not None and current != previous:
        # Never echo a supplied credential, context or principal into the error.
        raise BoundaryConflict(f"Conflicting {label} carriers")
    return current if current is not None else previous


def launch_authority_for(bundle: Mapping[str, Any] | None) -> Any:
    """Read the neutral launch envelope without weakening legacy validation."""
    return compatible_value(
        bundle or {}, "launch_authority", "viventium_launch_authority",
        label="launch authority",
    )


def opaque_application_context(bundle: Mapping[str, Any] | None) -> bool:
    """A client may declare already-prepared instruction data as opaque."""
    value = (bundle or {}).get("application_context_format")
    if value is not None and value != "opaque_v1":
        raise ValueError("Unsupported application context format")
    return value == "opaque_v1"


def canonical_schedule_access(grant: Any) -> dict[str, Any]:
    """Validate and copy supplied native scheduling targets, never credentials."""
    def invalid() -> None:
        raise ValueError("Invalid schedule access grant")

    if (not isinstance(grant, dict) or set(grant) - {"destinations", "allow_worker_target", "manage_destination_ids", "defaults"}
            or type(grant.get("allow_worker_target", False)) is not bool
            or not isinstance(grant.get("destinations", []), list)
            or len(grant.get("destinations", [])) > 64
            or not isinstance(grant.get("manage_destination_ids", []), list)
            or len(grant.get("manage_destination_ids", [])) > 64
            or any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in grant.get("manage_destination_ids", []))):
        invalid()
    defaults = grant.get("defaults", {})
    if (not isinstance(defaults, dict)
            or set(defaults) - {"once", "daily", "interval", "cron", "rfc5545"}):
        invalid()
    from .models import UpdateRecurringScheduleRequest
    copied_defaults = {}
    for family, values in defaults.items():
        if (not isinstance(values, dict)
                or set(values) - {"misfire_grace_seconds", "catch_up_policy", "max_catch_up_occurrences"}
                or any(value is None for value in values.values())
                or any(type(values[key]) is not int for key in ("misfire_grace_seconds", "max_catch_up_occurrences") if key in values)):
            invalid()
        try:
            copied_defaults[family] = UpdateRecurringScheduleRequest.model_validate(values).model_dump(exclude_unset=True)
        except ValueError:
            invalid()
    destinations = []
    for item in grant.get("destinations", []):
        if (not isinstance(item, dict) or not {"destination_id", "request_data"} <= set(item)
                or set(item) - {"destination_id", "request_data", "label"}
                or ("label" in item and (not isinstance(item["label"], str) or len(item["label"]) > 240))
                or not isinstance(item["destination_id"], str) or not 1 <= len(item["destination_id"]) <= 200
                or not isinstance(item["request_data"], dict) or set(item["request_data"]) != {"request_ref"}
                or not isinstance(item["request_data"]["request_ref"], str)
                or not 1 <= len(item["request_data"]["request_ref"]) <= 200):
            invalid()
        destinations.append({"destination_id": item["destination_id"],
            "request_data": {"request_ref": item["request_data"]["request_ref"]},
            **({"label": item["label"]} if "label" in item else {})})
    return {"destinations": destinations, "allow_worker_target": grant.get("allow_worker_target", False),
            "manage_destination_ids": list(dict.fromkeys(grant.get("manage_destination_ids", []))),
            "defaults": copied_defaults}
