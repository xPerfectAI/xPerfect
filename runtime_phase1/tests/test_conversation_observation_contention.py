"""Transient local observation contention preserves the admitted native operation."""

import asyncio
import json
import sqlite3
import threading

import pytest

from workers_projects_runtime.conversation_provider import ConversationProvider
from test_conversation_provider import (NativeUsageRuntime, _client, _payload, lifecycle_owned_test_clients,
    _public_completion_stdout, _spoken_family_fixture)
from test_conversation_provider_recovery import _post_bound, _result


def contention(code):
    error = sqlite3.OperationalError("Synthetic local write contention")
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT])
def test_stream_retries_observation_of_same_admitted_request(tmp_path, monkeypatch, code):
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    provider = client.app.state.conversation_provider
    sync = provider._sync
    calls = []

    def observe(record):
        calls.append((record["request_id"], record["run_id"], record.get("response_deadline_at")))
        if len(calls) == 1:
            raise contention(code)
        return sync(record)

    monkeypatch.setattr(provider, "_sync", observe)
    payload = _payload(tmp_path, stream=True)
    # Test an actual requested deadline, independent of whether an instant native result
    # finishes before the next observation can anchor an optional default timeout.
    payload["metadata"]["response_timeout_s"] = 60
    response, digest = _post_bound(client, payload)
    assert response.status_code == 200
    assert response.text.endswith("data: [DONE]\n\n")
    assert len(calls) >= 2
    assert len({(request_id, run_id) for request_id, run_id, _ in calls}) == 1
    # First observation may persist the original anchored deadline; contention never extends it.
    deadlines = {deadline for _, _, deadline in calls if deadline}
    assert len(deadlines) == 1
    saved = _result(client, digest).json()
    assert saved["state"] == "completed"
    assert saved["run_id"] == calls[0][1]
    current = client.app.state.store.get_provider_request(saved["request_id"])
    assert current["response_deadline_at"] in deadlines
    assert saved["response"]["choices"][0]["message"]["content"] == "Native answer."


def test_stream_propagates_non_contention_database_failure(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    error = contention(sqlite3.SQLITE_CORRUPT)
    monkeypatch.setattr(client.app.state.conversation_provider, "_sync", lambda _: (_ for _ in ()).throw(error))
    with pytest.raises(sqlite3.OperationalError, match="Synthetic local write contention"):
        _post_bound(client, _payload(tmp_path, stream=True))


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_detached_observer_keeps_same_pending_request_on_contention(code):
    provider = object.__new__(ConversationProvider)
    provider._detached_reconciliation_lock = threading.Lock()
    provider._detached_reconciliation_stop = threading.Event()
    provider._detached_reconciliation_thread = None
    provider._detached_reconciliations = {"original-request"}
    calls = []

    def observe(request_id):
        calls.append(request_id)
        if len(calls) == 1:
            raise contention(code)
        return False

    provider._reconcile_detached_request_once = observe
    provider._detached_reconciliation_loop()
    assert calls == ["original-request", "original-request"]
    assert provider._detached_reconciliations == set()


def test_detached_observer_drops_fatal_record_and_continues_other_requests():
    provider = object.__new__(ConversationProvider)
    provider._detached_reconciliation_lock = threading.Lock()
    provider._detached_reconciliation_stop = threading.Event()
    provider._detached_reconciliation_thread = None
    provider._detached_reconciliations = {"a-corrupt", "b-valid"}
    calls = []

    def observe(request_id):
        calls.append(request_id)
        if request_id == "a-corrupt":
            raise contention(sqlite3.SQLITE_CORRUPT)
        return False

    provider._reconcile_detached_request_once = observe
    provider._detached_reconciliation_loop()
    assert calls == ["a-corrupt", "b-valid"]
    assert provider._detached_reconciliations == set()


def _public_marker_fixture(monkeypatch, code, *, disconnect=False):
    state, provider, payload = _spoken_family_fixture()
    state["stage"] = 0
    state["record"]["response_deadline_at"] = "2030-01-01T00:00:00+00:00"
    full = "First complete.\n\nSecond complete."
    bodies = [json.dumps({"type": "assistant_response", "tool_name": None, "voice": "eligible",
                          "content": text}) for text in ["First complete.", "Second complete."]]
    provider.service.runtime.provider_activity_log = lambda *_: _public_completion_stdout(bodies)
    final = {"model": payload.model, "choices": [{"message": {"content": full}, "finish_reason": "stop"}],
             "usage": {}, "glasshive": {"usage_source": "native"}}
    observed = []
    def sync(record):
        state["stage"] += 1
        observed.append((record["request_id"], record["run_id"], record["response_deadline_at"]))
        if state["stage"] == 3:
            record["state"] = "completed"
            record["response_json"] = json.dumps(final)
        return record
    writes, committed = [], []
    def marker(*args):
        writes.append(args)
        if len(writes) == 1:
            raise contention(code)
        committed.append(args)
    provider._sync = sync
    provider._graph_transfer_control = lambda *_: None
    provider.response_payload = lambda *_: final
    provider.store.add_provider_activity_once = marker
    class Connection:
        async def is_disconnected(self):
            return disconnect and len(writes) == 1
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    async def collect():
        rows = []
        async for row in provider._stream_chunks(state["record"], payload, Connection()):
            if row.startswith("data: {"):
                choice = json.loads(row[6:]).get("choices")
                if choice and choice[0]["delta"].get("content"):
                    assert len(committed) == 1, "Public bytes require the durable authoring receipt first"
            rows.append(row)
        return rows
    return collect, writes, committed, observed, full


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT])
def test_first_public_marker_contention_preserves_exact_prefix_and_parts(monkeypatch, code):
    collect, writes, committed, observed, full = _public_marker_fixture(monkeypatch, code)
    rows = asyncio.run(collect())
    deltas = [json.loads(row[6:])["choices"][0]["delta"] for row in rows
              if row.startswith("data: {") and json.loads(row[6:]).get("choices")]
    assert ": heartbeat\n\n" in rows
    assert len(writes) == 2 and len(committed) == 1
    assert writes[0] == writes[1]
    assert writes[0][1] == "authored-response"
    assert len(set(observed)) == 1
    assert "".join(delta.get("content", "") for delta in deltas) == full
    parts = [delta["provider_specific_fields"]["viventium"]["public_part_complete"] for delta in deltas
             if delta.get("provider_specific_fields", {}).get("viventium", {}).get("public_part_complete")]
    assert [part["part_id"] for part in parts] == ["run-a:1", "run-a:2"]
    assert all(part["request_id"] == "request-a" and part["run_id"] == "run-a" for part in parts)
    assert rows[-1] == "data: [DONE]\n\n"


def test_first_public_marker_corruption_still_propagates(monkeypatch):
    collect, writes, committed, _, _ = _public_marker_fixture(monkeypatch, sqlite3.SQLITE_CORRUPT)
    with pytest.raises(sqlite3.OperationalError, match="Synthetic local write contention"):
        asyncio.run(collect())
    assert len(writes) == 1 and not committed


def test_first_public_marker_contention_then_disconnect_emits_no_public_bytes(monkeypatch):
    collect, writes, committed, observed, _ = _public_marker_fixture(
        monkeypatch, sqlite3.SQLITE_BUSY, disconnect=True)
    rows = asyncio.run(collect())
    assert len(writes) == 1 and not committed and len(observed) == 1
    assert ": heartbeat\n\n" in rows
    assert all(not json.loads(row[6:])["choices"][0]["delta"].get("content")
               for row in rows if row.startswith("data: {"))
