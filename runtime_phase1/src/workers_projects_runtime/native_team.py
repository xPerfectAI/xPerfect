from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping


NativeEventType = Literal[
    "provider.session.started",
    "provider.child.started",
    "provider.child.updated",
    "provider.child.completed",
    "provider.child.failed",
    "provider.child.stopped",
    "provider.child.snapshot",
    "provider.team.message",
    "provider.native.input.requested",
    "provider.native.input.resolved",
]

_LIVE_STATES = {"accepted", "pending", "queued", "starting", "running", "paused"}
_TERMINAL_STATES = {"completed", "failed", "stopped", "cancelled", "unknown"}
_CLAUDE_TERMINAL_STATE = {
    "completed": "completed",
    "failed": "failed",
    "stopped": "stopped",
    "killed": "stopped",
    "cancelled": "stopped",
}
_CODEX_TERMINAL_STATE = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "stopped",
    "stopped": "stopped",
    "shutdown": "stopped",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_ref(value: object, *, max_length: int = 256) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length or any(ord(char) < 32 for char in text):
        return ""
    return text


def _clean_role(value: object) -> str:
    return _clean_ref(value, max_length=128)


def _observed_at(value: datetime | None) -> str:
    instant = value or _utc_now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).isoformat()


def _event(
    event_type: NativeEventType,
    payload: Mapping[str, object],
) -> dict[str, object]:
    return {"event_type": event_type, "payload": dict(payload)}


def _session_event(session_id: object, *, observed_at: datetime | None) -> list[dict[str, object]]:
    clean_session_id = _clean_ref(session_id)
    if not clean_session_id:
        return []
    return [
        _event(
            "provider.session.started",
            {
                "sessionId": clean_session_id,
                "observedAt": _observed_at(observed_at),
            },
        )
    ]


def _codex_event(value: Mapping[str, object], *, observed_at: datetime | None) -> list[dict[str, object]]:
    event_type = _clean_ref(value.get("type"))
    if event_type == "thread.started":
        return _session_event(value.get("thread_id"), observed_at=observed_at)

    if event_type == "sub_agent_activity":
        child_ref = _clean_ref(value.get("agent_thread_id"))
        kind = _clean_ref(value.get("kind"), max_length=64).lower()
        if not child_ref or not kind:
            return []
        role = _clean_role(value.get("agent_path"))
        payload: dict[str, object] = {
            "childRef": child_ref,
            "role": role,
            "state": _CODEX_TERMINAL_STATE.get(kind, "running"),
        }
        event_ref = _clean_ref(value.get("event_id"))
        if event_ref:
            payload["providerEventRef"] = event_ref
        payload["observedAt"] = _observed_at(observed_at)
        if kind in {"started", "spawned", "created"}:
            normalized_type: NativeEventType = "provider.child.started"
        elif kind in _CODEX_TERMINAL_STATE:
            normalized_type = f"provider.child.{_CODEX_TERMINAL_STATE[kind]}"  # type: ignore[assignment]
        else:
            normalized_type = "provider.child.updated"
        events = [_event(normalized_type, payload)]
        if kind in {"interacted", "message", "steered"}:
            events.append(
                _event(
                    "provider.team.message",
                    {
                        "childRef": child_ref,
                        "direction": "observed",
                        "observedAt": _observed_at(observed_at),
                    },
                )
            )
        return events

    item = value.get("item")
    if not isinstance(item, Mapping) or _clean_ref(item.get("type")) not in {
        "collab_agent_tool_call",
        "collab_tool_call",
    }:
        return []
    events: list[dict[str, object]] = []
    tool = _clean_ref(item.get("tool"), max_length=128)
    receiver_refs = _clean_ref_list(item.get("receiver_thread_ids"))
    roles_by_ref: dict[str, str] = {}
    receiver_agents = item.get("receiver_agents")
    if isinstance(receiver_agents, list):
        for receiver in receiver_agents:
            if not isinstance(receiver, Mapping):
                continue
            receiver_ref = _clean_ref(
                receiver.get("thread_id")
                or receiver.get("threadId")
                or receiver.get("agent_thread_id")
            )
            if receiver_ref:
                roles_by_ref[receiver_ref] = _clean_role(
                    receiver.get("role")
                    or receiver.get("agent_role")
                    or receiver.get("name")
                )
    if tool in {"spawn_agent", "spawn_agents"}:
        for child_ref in receiver_refs:
            events.append(
                _event(
                    "provider.child.started",
                    {
                        "childRef": child_ref,
                        "role": roles_by_ref.get(child_ref, ""),
                        "state": "running",
                        "observedAt": _observed_at(observed_at),
                    },
                )
            )
    if tool in {"send_message", "send_input", "followup_task"}:
        for child_ref in receiver_refs:
            events.append(
                _event(
                    "provider.team.message",
                    {
                        "childRef": child_ref,
                        "direction": "sent",
                        "observedAt": _observed_at(observed_at),
                    },
                )
            )
    agent_states = item.get("agents_states")
    if isinstance(agent_states, Mapping):
        for child_ref_value, state_value in agent_states.items():
            child_ref = _clean_ref(child_ref_value)
            if isinstance(state_value, Mapping):
                state = _clean_ref(
                    state_value.get("status") or state_value.get("state"),
                    max_length=64,
                ).lower()
            else:
                state = _clean_ref(state_value, max_length=64).lower()
            terminal = _CODEX_TERMINAL_STATE.get(state)
            if not child_ref:
                continue
            if not terminal and state in _LIVE_STATES:
                events.append(
                    _event(
                        "provider.child.updated",
                        {
                            "childRef": child_ref,
                            "role": roles_by_ref.get(child_ref, ""),
                            "state": state,
                            "observedAt": _observed_at(observed_at),
                        },
                    )
                )
                continue
            if not terminal:
                continue
            events.append(
                _event(
                    f"provider.child.{terminal}",  # type: ignore[arg-type]
                    {
                        "childRef": child_ref,
                        "role": roles_by_ref.get(child_ref, ""),
                        "state": terminal,
                        "observedAt": _observed_at(observed_at),
                    },
                )
            )
    return events


def _clean_ref_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        clean = _clean_ref(item)
        if clean and clean not in refs:
            refs.append(clean)
    return refs


def _claude_event(value: Mapping[str, object], *, observed_at: datetime | None) -> list[dict[str, object]]:
    event_type = _clean_ref(value.get("type"))
    subtype = _clean_ref(value.get("subtype"), max_length=128)
    if event_type == "system" and subtype == "init":
        return _session_event(value.get("session_id"), observed_at=observed_at)
    if event_type != "system":
        return []

    child_ref = _clean_ref(value.get("task_id"))
    role = _clean_role(value.get("subagent_type") or value.get("task_type"))
    common: dict[str, object] = {
        "childRef": child_ref,
        "role": role,
        "observedAt": _observed_at(observed_at),
    }
    if subtype == "task_started" and child_ref:
        return [_event("provider.child.started", {**common, "state": "running"})]
    if subtype in {"task_progress", "task_updated"} and child_ref:
        patch = value.get("patch") if isinstance(value.get("patch"), Mapping) else {}
        raw_state = _clean_ref(patch.get("status"), max_length=64).lower()
        state = _CLAUDE_TERMINAL_STATE.get(raw_state, raw_state or "running")
        normalized: NativeEventType = (
            f"provider.child.{state}"  # type: ignore[assignment]
            if state in {"completed", "failed", "stopped"}
            else "provider.child.updated"
        )
        return [_event(normalized, {**common, "state": state})]
    if subtype == "task_notification" and child_ref:
        state = _CLAUDE_TERMINAL_STATE.get(
            _clean_ref(value.get("status"), max_length=64).lower()
        )
        if not state:
            return []
        return [_event(f"provider.child.{state}", {**common, "state": state})]  # type: ignore[arg-type]
    if subtype == "background_tasks_changed":
        tasks = value.get("tasks")
        if not isinstance(tasks, list):
            return []
        children: list[dict[str, str]] = []
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            task_ref = _clean_ref(task.get("task_id"))
            if not task_ref:
                continue
            children.append(
                {
                    "childRef": task_ref,
                    "role": _clean_role(task.get("task_type")),
                    "state": "running",
                }
            )
        return [
            _event(
                "provider.child.snapshot",
                {
                    "children": children,
                    "replace": True,
                    "observedAt": _observed_at(observed_at),
                },
            )
        ]
    if subtype == "agent_progress":
        child_ref = _clean_ref(value.get("agent_id"))
        if child_ref:
            return [
                _event(
                    "provider.team.message",
                    {
                        "childRef": child_ref,
                        "direction": "observed",
                        "observedAt": _observed_at(observed_at),
                    },
                )
            ]
    return []


def project_native_events(
    provider: str,
    value: Mapping[str, object] | str,
    *,
    observed_at: datetime | None = None,
) -> list[dict[str, object]]:
    """Project provider streams into a prompt-free, provider-neutral lifecycle.

    Unknown schemas return no events. That makes telemetry capability-gated: a
    version drift can reduce observability but cannot invent children or wedge a
    root mission in settling.
    """

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(decoded, Mapping):
            return []
        value = decoded
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider.startswith("codex"):
        return _codex_event(value, observed_at=observed_at)
    if normalized_provider.startswith("claude"):
        return _claude_event(value, observed_at=observed_at)
    if normalized_provider == "grok":
        from .grok_projection import native_events
        return native_events(value, observed_at=observed_at)
    return []


@dataclass(frozen=True)
class SettlementDecision:
    state: Literal["ready", "settling", "degraded"]
    active_child_refs: tuple[str, ...] = ()
    lost_child_refs: tuple[str, ...] = ()


@dataclass
class _Child:
    child_ref: str
    role: str
    state: str
    updated_at: datetime


class NativeTeamProjection:
    """In-memory reducer for normalized durable provider events."""

    def __init__(
        self,
        *,
        provider: str,
        observable: bool,
        reconcile_seconds: int = 120,
    ) -> None:
        self.provider = str(provider or "unknown")
        self.observable = bool(observable)
        self.reconcile_seconds = max(1, min(int(reconcile_seconds), 600))
        self.session_id = ""
        self._children: dict[str, _Child] = {}

    @classmethod
    def from_summary(
        cls,
        summary: Mapping[str, object] | None,
        *,
        reconcile_seconds: int | float = 120,
    ) -> NativeTeamProjection:
        if not isinstance(summary, Mapping):
            return cls(
                provider="unknown",
                observable=False,
                reconcile_seconds=max(1, int(reconcile_seconds)),
            )
        projection = cls(
            provider=str(summary.get("provider") or "unknown"),
            observable=summary.get("observable") is True,
            reconcile_seconds=max(1, int(reconcile_seconds)),
        )
        projection.session_id = _clean_ref(summary.get("sessionId"))
        children = summary.get("children")
        if not isinstance(children, list):
            return projection
        for child in children:
            if not isinstance(child, Mapping):
                continue
            raw_updated_at = str(child.get("updatedAt") or "").strip()
            try:
                updated_at = datetime.fromisoformat(raw_updated_at.replace("Z", "+00:00"))
            except ValueError:
                updated_at = _utc_now()
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            projection._upsert_child(child, now=updated_at)
        return projection

    def apply(self, event: Mapping[str, object], *, now: datetime | None = None) -> None:
        if not self.observable:
            return
        instant = now or _utc_now()
        event_type = _clean_ref(event.get("event_type"))
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        if event_type == "provider.session.started":
            self.session_id = _clean_ref(payload.get("sessionId"))
            return
        if event_type == "provider.child.snapshot":
            children = payload.get("children")
            if not isinstance(children, list):
                return
            snapshot_refs: set[str] = set()
            for item in children:
                if not isinstance(item, Mapping):
                    continue
                child_ref = _clean_ref(item.get("childRef"))
                if not child_ref:
                    continue
                snapshot_refs.add(child_ref)
                self._upsert_child(item, now=instant)
            if bool(payload.get("replace")):
                for child_ref, child in self._children.items():
                    if child_ref not in snapshot_refs and child.state in _LIVE_STATES:
                        child.state = "completed"
                        child.updated_at = instant
            return
        if event_type.startswith("provider.child."):
            self._upsert_child(payload, now=instant)

    def _upsert_child(self, payload: Mapping[str, object], *, now: datetime) -> None:
        child_ref = _clean_ref(payload.get("childRef"))
        if not child_ref:
            return
        state = _clean_ref(payload.get("state"), max_length=64).lower() or "running"
        if state not in _LIVE_STATES | _TERMINAL_STATES:
            state = "running"
        role = _clean_role(payload.get("role"))
        existing = self._children.get(child_ref)
        if existing and not role:
            role = existing.role
        self._children[child_ref] = _Child(child_ref, role, state, now)

    def settlement(
        self,
        *,
        root_exited_at: datetime,
        now: datetime | None = None,
    ) -> SettlementDecision:
        if not self.observable:
            return SettlementDecision("ready")
        instant = now or _utc_now()
        active = tuple(
            sorted(child.child_ref for child in self._children.values() if child.state in _LIVE_STATES)
        )
        if not active:
            return SettlementDecision("ready")
        root_exit = root_exited_at
        if root_exit.tzinfo is None:
            root_exit = root_exit.replace(tzinfo=timezone.utc)
        age_seconds = (instant.astimezone(timezone.utc) - root_exit.astimezone(timezone.utc)).total_seconds()
        if age_seconds < self.reconcile_seconds:
            return SettlementDecision("settling", active_child_refs=active)
        for child_ref in active:
            self._children[child_ref].state = "unknown"
            self._children[child_ref].updated_at = instant
        return SettlementDecision("degraded", lost_child_refs=active)

    def summary(self) -> dict[str, object] | None:
        if not self.observable:
            return None
        children = [
            {
                "childRef": child.child_ref,
                "role": child.role,
                "state": child.state,
                "updatedAt": child.updated_at.isoformat(),
            }
            for child in sorted(self._children.values(), key=lambda item: item.child_ref)
        ]
        return {
            "observable": True,
            "provider": self.provider,
            "sessionId": self.session_id or None,
            "activeCount": sum(child["state"] in _LIVE_STATES for child in children),
            "children": children,
        }


@dataclass(frozen=True)
class ClaudeAgentViewProbeResult:
    observable: bool
    reason: str
    events: tuple[dict[str, object], ...] = ()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _agent_view_state(value: object) -> str:
    state = _clean_ref(value, max_length=64).lower()
    if state in {"working", "busy"}:
        return "running"
    if state == "blocked":
        return "paused"
    if state == "done":
        return "completed"
    if state in _LIVE_STATES:
        return state
    if state in _TERMINAL_STATES:
        return state
    return ""


def probe_claude_agent_view(
    *,
    binary: str,
    workspace: Path,
    child_env: Mapping[str, str],
    isolated_root: Path,
    enabled: bool,
    timeout_seconds: float = 2.0,
) -> ClaudeAgentViewProbeResult:
    """Read Claude Agent View only when explicitly enabled and run-isolated.

    This is observation, not a second process owner. Unknown CLI schemas fail
    closed so the API reports child projection as unavailable.
    """

    if not enabled:
        return ClaudeAgentViewProbeResult(False, "disabled")
    config_text = str(child_env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if not config_text or not _is_within(Path(config_text), isolated_root):
        return ClaudeAgentViewProbeResult(False, "claude_config_not_isolated")
    command = [
        str(binary),
        "agents",
        "--json",
        "--all",
        "--cwd",
        str(workspace),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            env=dict(child_env),
            text=True,
            capture_output=True,
            timeout=max(0.1, min(float(timeout_seconds), 10.0)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ClaudeAgentViewProbeResult(False, "claude_agent_view_unavailable")
    if completed.returncode != 0:
        return ClaudeAgentViewProbeResult(False, "claude_agent_view_unavailable")
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ClaudeAgentViewProbeResult(False, "claude_agent_view_schema_unrecognized")
    if not isinstance(decoded, list):
        return ClaudeAgentViewProbeResult(False, "claude_agent_view_schema_unrecognized")
    children: list[dict[str, str]] = []
    for item in decoded:
        if not isinstance(item, Mapping):
            return ClaudeAgentViewProbeResult(False, "claude_agent_view_schema_unrecognized")
        kind = _clean_ref(item.get("kind"), max_length=64).lower()
        if kind not in {"background", "bg"}:
            # The installed command also returns the root as `interactive`.
            # Treating that root as a child would make a mission self-orphan.
            continue
        child_ref = _clean_ref(item.get("sessionId") or item.get("session_id") or item.get("id"))
        state = _agent_view_state(item.get("state") or item.get("status"))
        if not child_ref or not state:
            return ClaudeAgentViewProbeResult(False, "claude_agent_view_schema_unrecognized")
        children.append(
            {
                "childRef": child_ref,
                "role": _clean_role(item.get("name") or item.get("agent") or item.get("agent_type")),
                "state": state,
            }
        )
    if not children:
        return ClaudeAgentViewProbeResult(
            False,
            "claude_agent_view_no_background_children",
        )
    return ClaudeAgentViewProbeResult(
        True,
        "observable",
        events=(
            _event(
                "provider.child.snapshot",
                {"children": children, "replace": True},
            ),
        ),
    )


def reduce_native_events(
    provider: str,
    events: Iterable[Mapping[str, object]],
    *,
    observable: bool,
) -> NativeTeamProjection:
    projection = NativeTeamProjection(provider=provider, observable=observable)
    for event in events:
        projection.apply(event)
    return projection
