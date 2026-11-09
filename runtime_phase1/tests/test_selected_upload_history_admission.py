"""A selected upload ledger is not an inference batch or lifetime file quota."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_account_api import account_client, account_headers, enable_native_orchestration
from workers_projects_runtime.bootstrap import _write_project_files
from workers_projects_runtime.coordinator import (
    ApplicationBinding, CoordinatorConfig, Dispatch, Goal, Route,
)
from workers_projects_runtime.coordinator_application import dispatch_input
from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS
from workers_projects_runtime.upload_projection import project_bootstrap_upload_files


MODEL_ID = next(key for key, model in GLASSHIVE_MODELS.items()
                if model.harness_profile == "codex-cli" and "high" in model.effort_choices)


@pytest.fixture
def owned_ledger(tmp_path, monkeypatch):
    enable_native_orchestration(monkeypatch)
    root = tmp_path / "owned-uploads"
    owner = root / "owner-a"
    owner.mkdir(parents=True)
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(root))
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-source-path-key")
    records, originals = [], {}
    for ordinal in range(134):
        file_id = f"owned-input-{ordinal:03d}"
        source = owner / f"{file_id}__original.txt"
        data = f"Original synthetic input {ordinal}.\n".encode()
        source.write_bytes(data)
        originals[file_id] = data
        records.append({
            "file_id": file_id, "filename": f"input-{ordinal:03d}.txt",
            "filepath": f"/uploads/owner-a/{source.name}", "source": "local",
            "user": "owner-a", "tenant_id": "tenant-a", "bytes": len(data),
        })
    return {
        "application_context_format": "opaque_v1",
        "glasshive_upload_context": {"tool_uploaded_files": records},
        "viventium_delegation_packet": {"selected_files": [
            {"ordinal": ordinal, "ref": record["file_id"]}
            for ordinal, record in enumerate(records)
        ]},
    }, originals


def prepared_goal(client, bundle):
    service, core = client.app.state.service, client.app.state.coordinator
    service.executor.submit = lambda *args, **kwargs: None
    core.provider._model = lambda *args, **kwargs: SimpleNamespace(effort_choices=("high",))
    destination = service.webhook_destinations.create(
        "tenant-a", "owner-a", name="Synthetic application",
        url="http://127.0.0.1:12345/adapter", payload_mode="envelope",
    )
    cid = core.create("tenant-a", "owner-a", CoordinatorConfig(
        model="synthetic-manager", effort="high",
        routes=[Route(id="primary", profile="codex-cli", model=MODEL_ID,
                      effort="high", execution_mode="host")],
        application=ApplicationBinding(destination_id=destination["destination_id"],
                                       request_ref="synthetic-original-context"),
    ))["conversation_id"]
    original = "Read the original selected inputs and prepare a report."
    core.accept_turn("tenant-a", "owner-a", cid, "original", original,
                     [Goal(id="goal", text=original)])
    core.dispatch("tenant-a", "owner-a", cid, Dispatch(goal_id="goal", route_id="primary"))
    dispatch = dispatch_input(core, "tenant-a", "owner-a", cid, "goal")
    return core, cid, {
        "dispatch_digest": dispatch["dispatch_digest"],
        "notification_ref": dispatch["notification_ref"],
        "delegation": {
            "title": "Read original inputs", "goal": original, "instruction": original,
            "profile": "codex-cli", "executionMode": "host",
            "bootstrapBundle": {
                **bundle,
                "launch_authority": {
                    "version": 1, "kind": "conversation_orchestrator", "execution_mode": "host",
                    "worker_model": MODEL_ID, "worker_reasoning_effort": "high",
                },
                "callbacks": {"origin_ref": "ghi_synthetic_selected_history"},
            },
        },
    }


def test_selected_history_keeps_all_originals_through_prepare_and_replay(account_client, owned_ledger, tmp_path):
    bundle, originals = owned_ledger
    core, cid, body = prepared_goal(account_client, bundle)
    url = f"/v1/coordinator/conversations/{cid}/goals/goal/prepare"
    response = account_client.post(url, headers=account_headers(idempotency_key="selected-history"), json=body)
    assert response.status_code == 200, response.text
    goal = response.json()["goal"]
    worker = core.store.get_worker(goal["worker_id"])
    projected = json.loads(worker["bootstrap_bundle_json"])
    files = [entry for entry in projected["files"] if entry.get("file_id") in originals]
    assert [entry["file_id"] for entry in files] == list(originals)
    original_projection = project_bootstrap_upload_files(bundle, tenant_id="tenant-a", owner_id="owner-a")
    assert all(entry["input_kind"] == "original" for entry in original_projection["files"])
    workspace = tmp_path / "materialized"
    _write_project_files(
        home_dir=tmp_path / "home", workspace_dir=workspace, bundle=projected,
        worker=worker, copy_file=lambda source, target: target.write_bytes(source.read_bytes()),
        copy_tree=lambda *_: None,
    )
    for entry in files:
        data = (workspace / entry["path"]).read_bytes()
        assert data == originals[entry["file_id"]]
        assert hashlib.sha256(data).digest() == hashlib.sha256(originals[entry["file_id"]]).digest()
    replay = account_client.post(url, headers=account_headers(idempotency_key="selected-history"), json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["goal"]["run_id"] == goal["run_id"]
    with core.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM delegations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_repeated_selected_refs_and_records_do_not_duplicate_originals(owned_ledger):
    bundle, originals = owned_ledger
    records = bundle["glasshive_upload_context"]["tool_uploaded_files"]
    selected = bundle["viventium_delegation_packet"]["selected_files"]
    records.extend(copy.deepcopy(records))
    first_repeat = len(selected)
    selected.extend({"ordinal": first_repeat + ordinal, "ref": file_id}
                    for ordinal, file_id in enumerate(originals))
    projected = project_bootstrap_upload_files(bundle, tenant_id="tenant-a", owner_id="owner-a")
    assert [entry["file_id"] for entry in projected["files"]] == list(originals)


@pytest.mark.parametrize("failure,reason", [
    ("missing", "does not match a trusted upload ref"),
    ("ambiguous", "identity is ambiguous"),
    ("owner", "authenticated owner"),
    ("tenant", "authenticated owner"),
    ("ordinal", "selected_files is invalid"),
    ("named", "requires a stable upload ref"),
])
def test_long_selected_history_keeps_validation_and_rejects_before_reservation(
    account_client, owned_ledger, failure, reason,
):
    bundle, _ = owned_ledger
    records = bundle["glasshive_upload_context"]["tool_uploaded_files"]
    selected = bundle["viventium_delegation_packet"]["selected_files"]
    if failure == "missing":
        records.pop()
    elif failure == "ambiguous":
        records.append({**records[100], "filepath": "/uploads/owner-a/other-original.txt"})
    elif failure == "owner":
        records[100]["user"] = "foreign-owner"
    elif failure == "tenant":
        records[100]["tenant_id"] = "foreign-tenant"
    elif failure == "ordinal":
        selected[100]["ordinal"] = selected[99]["ordinal"]
    elif failure == "named":
        selected[100] = {"ordinal": 100, "name": "input-100.txt"}
    with pytest.raises(ValueError, match=reason):
        project_bootstrap_upload_files(bundle, tenant_id="tenant-a", owner_id="owner-a")
    core, cid, body = prepared_goal(account_client, bundle)
    response = account_client.post(
        f"/v1/coordinator/conversations/{cid}/goals/goal/prepare",
        headers=account_headers(idempotency_key="invalid-selected-history"), json=body,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "delegation_upload_projection_invalid"
    assert not core._goal(cid, "goal")["run_id"]
    with core.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM delegations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
