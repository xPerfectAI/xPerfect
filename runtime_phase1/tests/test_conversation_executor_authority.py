"""A shared queue does not share a process's invocation authority."""
from __future__ import annotations

import json
import copy

import pytest

from workers_projects_runtime.conversation_provider import ChatCompletionRequest, ConversationProvider
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from test_host_run_leases import _GrantCaptureRuntime, _normal_host_broker_turn
from test_conversation_provider import (
    AUTH, _client, _payload, _signed_bundle_headers, lifecycle_owned_test_clients,
)
from test_stateless_restart_preservation import BrokerBundleCaptureRuntime, _truthfully_invoke_conversation_run


def _consume(service, worker_id, consumer):
    with service._processors_lock:
        generation = service._processor_generations.get(worker_id, 0) + 1
        service._processor_generations[worker_id] = generation
        service._active_processors.add(worker_id)
    getattr(service, consumer)(worker_id, generation)


def _catalog_bundle():
    catalog = {'version':1,'server_name':'synthetic-capabilities','broker_url':'https://broker.invalid/mcp',
               'grant_id':'synthetic-old-id','expires_at':2000000000,'tools':[
                   {'name':'active_work','description':'Manage accepted work.',
                    'inputSchema':{'type':'object','properties':{'action':{'type':'string'}}}}]}
    return {'env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'synthetic-old-grant'},
            'glasshive_capability_broker':{'name':catalog['server_name'],'url':catalog['broker_url'],
                'allowed_host_tools':['active_work'],'scopes':{'content_read':True},
                'grant_id':catalog['grant_id'],'grant_expires_at':catalog['expires_at'],'tool_catalog':catalog},
            'claude_project_mcp':{'synthetic-capabilities':{'type':'http','url':catalog['broker_url'],
                'headers':{'Authorization':'Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}'}}}}


def _renew_catalog(bundle):
    broker = bundle['glasshive_capability_broker']
    broker.update(grant_id='synthetic-new-id',grant_expires_at=2000000600)
    broker['tool_catalog'].update(grant_id='synthetic-new-id',expires_at=2000000600)


@pytest.mark.parametrize("consumer", ["_process_worker_queue_parallel", "_process_worker_queue_legacy"])
@pytest.mark.parametrize("native_tools", [True, False])
def test_foreign_executor_leaves_broker_turn_for_its_invocation_owner(tmp_path, monkeypatch, consumer, native_tools):
    store, owner, runtime, worker_id, run_id, bundle = _normal_host_broker_turn(
        tmp_path, "two-executors", grant="synthetic-current-grant"
    )
    bundle["provider_capabilities"]["native_tools"] = native_tools
    store.update_worker(worker_id, bootstrap_bundle_json=json.dumps(bundle))
    foreign_store = Store(store.db_path)
    foreign_runtime = _GrantCaptureRuntime()
    foreign = WorkersProjectsService(foreign_store, foreign_runtime,
                                    reconcile_on_startup=False, start_background_consumers=False)
    foreign_runtime.service = foreign
    foreign._emit_callback = lambda *_args, **_kwargs: None
    claims = []
    original_claim = foreign_store.claim_next_queued_run
    def claim(*args, **kwargs):
        claims.append(True)
        return original_claim(*args, **kwargs)
    monkeypatch.setattr(foreign_store, "claim_next_queued_run", claim)
    try:
        assert not foreign.has_run_local_bundle(run_id)
        _consume(foreign, worker_id, consumer)
        assert claims == []
        assert foreign_runtime.bearers == []
        assert store.get_run(run_id)["state"] == "queued"
        assert store.get_active_host_run_lease_for_run(run_id) is None
        _consume(owner, worker_id, consumer)
        assert store.get_run(run_id)["state"] == "completed"
        assert runtime.bearers == ["synthetic-current-grant"]
        assert runtime.late_grants == [False]
        _consume(owner, worker_id, consumer)
        assert runtime.bearers == ["synthetic-current-grant"]
        assert "synthetic-current-grant" not in store.get_worker(worker_id)["bootstrap_bundle_json"]
    finally:
        foreign.shutdown()
        owner.shutdown()
        foreign_store.close()
        store.close()


@pytest.mark.parametrize("consumer", ["_process_worker_queue_parallel", "_process_worker_queue_legacy"])
def test_conversation_without_broker_keeps_shared_queue_behavior(tmp_path, consumer):
    store, owner, runtime, worker_id, run_id, bundle = _normal_host_broker_turn(tmp_path, "no-broker")
    bundle.pop("glasshive_capability_broker")
    bundle.pop("claude_project_mcp")
    store.update_worker(worker_id, bootstrap_bundle_json=json.dumps(bundle))
    try:
        assert not owner.has_run_local_bundle(run_id)
        _consume(owner, worker_id, consumer)
        assert store.get_run(run_id)["state"] == "completed"
        assert runtime.bearers == [""]
    finally:
        owner.shutdown()
        store.close()


@pytest.mark.parametrize("native_tools", [True, False])
@pytest.mark.parametrize("authority_kind", [None, "conversation_orchestrator"])
def test_service_and_fallback_recovery_require_normal_conversation_grants(tmp_path, native_tools, authority_kind):
    store, service, _runtime, worker_id, _run_id, bundle = _normal_host_broker_turn(tmp_path, "predicates")
    bundle["provider_capabilities"]["native_tools"] = native_tools
    if authority_kind:
        bundle["glasshive_capability_broker"]["authority_kind"] = authority_kind
    worker = store.update_worker(worker_id, bootstrap_bundle_json=json.dumps(bundle))
    try:
        assert service._requires_conversation_invocation_bearer(worker)
        assert ConversationProvider._worker_requires_invocation_bearer(worker)
        bundle.pop("glasshive_capability_broker")
        worker["bootstrap_bundle_json"] = json.dumps(bundle)
        assert not service._requires_conversation_invocation_bearer(worker)
        assert not ConversationProvider._worker_requires_invocation_bearer(worker)
    finally:
        service.shutdown()
        store.close()


@pytest.mark.parametrize("consumer", ["_process_worker_queue_parallel", "_process_worker_queue_legacy"])
def test_local_invocation_without_grant_fails_closed_before_runtime(tmp_path, consumer):
    store, service, runtime, worker_id, run_id, _bundle = _normal_host_broker_turn(tmp_path, "empty-local-grant")
    assert service.attach_run_local_bundle(run_id, {"env": {}})
    try:
        _consume(service, worker_id, consumer)
        result = store.get_run(run_id)
        assert result["state"] == "needs_input"
        assert result["failure_class"] == "conversation_capability_grant_required"
        assert runtime.bearers == []
    finally:
        service.shutdown()
        store.close()


def test_exact_claim_does_not_advance_to_foreign_next_turn(tmp_path):
    store, service, runtime, worker_id, first_id, _bundle = _normal_host_broker_turn(
        tmp_path, "peek-claim-race", grant="synthetic-owned-first"
    )
    worker = store.get_worker(worker_id)
    second = store.create_run(worker_id, worker["project_id"], "Another synthetic turn.")
    try:
        peeked = store.peek_next_queued_run(worker_id)
        assert peeked["run_id"] == first_id
        store.update_run(first_id, state="completed", output_text="Completed elsewhere.")
        claimed = store.claim_next_queued_run(worker_id, executor_id="synthetic-old-consumer",
                                             expected_run_id=peeked["run_id"])
        assert claimed is None
        retained = store.get_run(second["run_id"])
        assert retained["state"] == "queued"
        assert not retained["active_attempt_id"]
        assert runtime.bearers == []
    finally:
        service.shutdown()
        store.close()


@pytest.mark.parametrize("consumer", ["_process_worker_queue_parallel", "_process_worker_queue_legacy"])
def test_consumer_cannot_claim_next_foreign_turn_after_its_owned_peek(tmp_path, monkeypatch, consumer):
    store, service, runtime, worker_id, first_id, _bundle = _normal_host_broker_turn(
        tmp_path, "consumer-peek-claim-race", grant="synthetic-owned-first"
    )
    worker = store.get_worker(worker_id)
    second = store.create_run(worker_id, worker["project_id"], "Another synthetic turn.")
    original_claim = store.claim_next_queued_run
    claims = []
    def claim(*args, **kwargs):
        claims.append(kwargs.get("expected_run_id"))
        # A different consumer finished the inspected turn before this DB transaction.
        store.update_run(first_id, state="completed", output_text="Completed elsewhere.")
        return original_claim(*args, **kwargs)
    monkeypatch.setattr(store, "claim_next_queued_run", claim)
    try:
        _consume(service, worker_id, consumer)
        assert claims == [first_id]
        retained = store.get_run(second["run_id"])
        assert retained["state"] == "queued"
        assert not retained["active_attempt_id"]
        assert store.get_active_host_run_lease_for_run(second["run_id"]) is None
        assert runtime.bearers == []
    finally:
        service.shutdown()
        store.close()


@pytest.mark.parametrize("session_mode", ["persistent", "stateless"])
@pytest.mark.parametrize("catalog", [False, True])
def test_graceful_shutdown_preserves_exact_authenticated_authorization_retry(tmp_path, monkeypatch, session_mode, catalog):
    first = _client(tmp_path, monkeypatch, runtime=BrokerBundleCaptureRuntime())
    service = first.app.state.service
    store = first.app.state.store
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    payload = _payload(tmp_path)
    payload["metadata"]["provider_session_mode"] = session_mode
    payload["metadata"]["bootstrap_bundle"] = {
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-old-grant"},
        "glasshive_capability_broker": {"allowed_host_tools": ["active_work"]},
    }
    if catalog:
        payload["metadata"]["bootstrap_bundle"] = _catalog_bundle()
    accepted = first.app.state.conversation_provider.start(ChatCompletionRequest.model_validate(payload))
    run_id = accepted["run_id"]
    assert not store.get_run(run_id)["active_attempt_id"]
    service.shutdown()
    assert store.get_run(run_id)["state"] == "needs_input"
    assert store.get_run(run_id)["failure_class"] == "conversation_capability_grant_required"
    assert store.get_provider_request(accepted["request_id"])["state"] == "failed"
    replay_runtime = BrokerBundleCaptureRuntime()
    restarted = _client(tmp_path, monkeypatch, runtime=replay_runtime)
    replay_store = restarted.app.state.store
    refreshed = copy.deepcopy(payload)
    refreshed["metadata"]["bootstrap_bundle"]["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] = "synthetic-new-grant"
    if catalog:
        _renew_catalog(refreshed["metadata"]["bootstrap_bundle"])
    changed = copy.deepcopy(refreshed)
    changed["messages"][-1]["content"] = "A changed synthetic original must not reuse this receipt."
    headers = {**AUTH, **_signed_bundle_headers(refreshed["metadata"]["bootstrap_bundle"]),
               "X-GlassHive-Provider-Session-Mode": session_mode}
    rejected = restarted.post("/v1/chat/completions", headers=headers, json=changed)
    assert rejected.status_code == 409
    assert replay_runtime.run_bundles == []
    reply = restarted.post("/v1/chat/completions", headers=headers, json=refreshed)
    assert reply.status_code == 200, reply.text
    completed = replay_store.get_provider_request(accepted["request_id"])
    assert (completed["run_id"], completed["state"]) == (run_id, "completed")
    assert len(replay_runtime.run_bundles) == 1
    assert replay_runtime.run_bundles[0]["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == "synthetic-new-grant"
    duplicate = restarted.post("/v1/chat/completions", headers=headers, json=refreshed)
    assert duplicate.status_code == 200
    assert len(replay_runtime.run_bundles) == 1
    activities = replay_store.list_provider_activity(accepted["request_id"])
    assert [entry["event_type"] for entry in activities].count("failed") == 1
    assert [entry["event_type"] for entry in activities].count("completed") == 1
    persisted = replay_store.get_worker(replay_store.get_run(run_id)["worker_id"])["bootstrap_bundle_json"]
    assert "synthetic-new-grant" not in persisted


@pytest.mark.parametrize("foreign_state", ["queued", "claimed", "running"])
def test_shutdown_leaves_foreign_live_generation_and_unowned_work_untouched(tmp_path, foreign_state):
    store, service, _runtime, worker_id, first_id, _bundle = _normal_host_broker_turn(
        tmp_path, "shutdown-ownership", grant="synthetic-queued-grant"
    )
    worker = store.get_worker(worker_id)
    first = store.get_run(first_id)
    foreign_store = Store(store.db_path)
    foreign_runtime = _GrantCaptureRuntime()
    foreign = WorkersProjectsService(foreign_store, foreign_runtime,
                                    reconcile_on_startup=False, start_background_consumers=False)
    foreign_runtime.service = foreign
    assert foreign.attach_run_local_bundle(first_id, {"env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-transferred-grant"}})
    first = store.get_run(first_id)
    if foreign_state == "running":
        _truthfully_invoke_conversation_run(store, worker, first)
    else:
        if foreign_state == "claimed":
            assert store.claim_next_queued_run(worker_id, executor_id=foreign._executor_id)
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=first_id, executor_id=foreign._executor_id,
            conversation_limit=2, mission_limit=64, account_mission_limit=64, tenant_mission_limit=64,
            lease_ttl_s=300,
        )
    owned = service.assign_run(worker_id, "An owned pending turn.", start_processor=False,
                              run_local_bundle={"env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-owned-pending"}})
    unowned = store.create_run(worker_id, worker["project_id"], "A foreign pending turn.")
    worker_before = store.get_worker(worker_id)["state"]
    try:
        service.shutdown()
        assert store.get_run(first_id)["state"] == foreign_state
        assert store.get_active_host_run_lease_for_run(first_id) is not None
        assert store.get_run(owned["run_id"])["state"] == "needs_input"
        assert store.get_run(unowned["run_id"])["state"] == "queued"
        assert store.get_worker(worker_id)["state"] == worker_before
    finally:
        service.shutdown()
        foreign.shutdown()
        foreign_store.close()
        store.close()


def test_duplicate_transfers_queued_invocation_before_old_claim_and_shutdown(tmp_path):
    store, old, old_runtime, worker_id, run_id, _bundle = _normal_host_broker_turn(
        tmp_path, "queued-duplicate-transfer", grant="synthetic-earlier-grant"
    )
    current_store = Store(store.db_path)
    runtime = _GrantCaptureRuntime()
    current = WorkersProjectsService(current_store, runtime,
                                    reconcile_on_startup=False, start_background_consumers=False)
    runtime.service = current
    try:
        assert current.attach_run_local_bundle(run_id, {"env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-current-grant"}})
        assert store.invocation_executor_for_run(store.get_run(run_id)) == current._executor_id
        assert store.claim_next_queued_run(worker_id, executor_id=old._executor_id, expected_run_id=run_id) is None
        old.shutdown()
        assert store.get_run(run_id)["state"] == "queued"
        _consume(current, worker_id, "_process_worker_queue_parallel")
        assert store.get_run(run_id)["state"] == "completed"
        assert runtime.bearers == ["synthetic-current-grant"]
        assert old_runtime.bearers == []
        assert not json.loads(store.get_worker(worker_id)["bootstrap_bundle_json"]).get("invocation_executor_id")
    finally:
        old.shutdown()
        current.shutdown()
        current_store.close()
        store.close()


@pytest.mark.parametrize('changed_field',['tool','schema','description','server','url','scope'])
def test_catalog_renewal_does_not_hide_changed_authority(tmp_path,monkeypatch,changed_field):
    client=_client(tmp_path,monkeypatch,runtime=BrokerBundleCaptureRuntime())
    monkeypatch.setattr(client.app.state.service,'_ensure_worker_processor',lambda _worker_id:None)
    payload=_payload(tmp_path)
    payload['metadata']['bootstrap_bundle']=_catalog_bundle()
    accepted=client.app.state.conversation_provider.start(ChatCompletionRequest.model_validate(payload))
    changed=copy.deepcopy(payload)
    bundle=changed['metadata']['bootstrap_bundle'];_renew_catalog(bundle)
    broker=bundle['glasshive_capability_broker'];catalog=broker['tool_catalog']
    if changed_field=='tool':catalog['tools'][0]['name']='another_tool'
    elif changed_field=='schema':catalog['tools'][0]['inputSchema']['properties']['action']['type']='integer'
    elif changed_field=='description':catalog['tools'][0]['description']='Changed declared capability.'
    elif changed_field=='server':catalog['server_name']='another-server'
    elif changed_field=='url':catalog['broker_url']='https://another-broker.invalid/mcp'
    else:broker['scopes']['content_read']=False
    response=client.post('/v1/chat/completions',headers={**AUTH,**_signed_bundle_headers(bundle)},json=changed)
    assert response.status_code==409,response.text
    assert client.app.state.store.get_run(accepted['run_id'])['state']=='queued'
    assert client.app.state.service.runtime.run_bundles==[]


@pytest.mark.parametrize('invalid_grant',['expired','grant_mismatch','expiry_mismatch'])
def test_renewable_catalog_identity_still_requires_valid_launch_grant(invalid_grant):
    bundle=_catalog_bundle()
    bundle['provider_capabilities']={'native_tools':False}
    payload,_filters=HostClaudeCodeRuntime._host_claude_project_mcp_payload(None,bundle,bundle['claude_project_mcp'])
    assert len(payload['mcpServers'])==1
    original=ConversationProvider._request_authority_bundle_descriptor(bundle)
    broker=bundle['glasshive_capability_broker']
    if invalid_grant=='expired':
        broker['grant_expires_at']=broker['tool_catalog']['expires_at']=0
    elif invalid_grant=='grant_mismatch':broker['tool_catalog']['grant_id']='synthetic-unmatched-id'
    else:broker['tool_catalog']['expires_at']+=1
    assert ConversationProvider._request_authority_bundle_descriptor(bundle)==original
    with pytest.raises(RuntimeErrorBase,match='unavailable or stale'):
        HostClaudeCodeRuntime._host_claude_project_mcp_payload(None,bundle,bundle['claude_project_mcp'])
