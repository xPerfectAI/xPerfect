"""Standalone Queen children use exact current goal ownership, never host privileges."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from workers_projects_runtime.coordinator import CoordinatorConfig, CoordinatorScope, CoordinatorService, Dispatch, Goal, Control
from workers_projects_runtime.openclaw_runtime import StubRuntime, HostCapacityError
from workers_projects_runtime.service import BackgroundWorkerConfigurationError, WorkersProjectsService, standalone_coordinator_host_enabled, native_parallel_policy_enabled
from workers_projects_runtime.store import Store, WorkAdmissionError
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.allowed_ai_policy import AllowedAiPolicyService


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    for name, value in {
        "GLASSHIVE_SECURITY_MODE":"local", "GLASSHIVE_AUTH_MODE":"local",
        "WPR_DEFAULT_EXECUTION_MODE":"host", "GLASSHIVE_HOST_WORKERS_ENABLED":"1",
        "GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS":"false",
        "WPR_MODEL_HOST_CODEX_CLI":"gpt-6.1-sol", "WPR_MODEL_CODEX_CLI":"gpt-6.1-sol",
        "GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED":"false",
    }.items(): monkeypatch.setenv(name, value)
    for name in ["XPERFECT_EXECUTION_PROFILE", "VIVENTIUM_PARALLEL_WORK_EXECUTION_MODE", "VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY"]:
        monkeypatch.delenv(name, raising=False)
    account = {"account_id":"owned-codex", "provider":"codex", "status":"ready", "is_default":True}
    class Accounts:
        def list_provider_accounts(self, *, tenant_id, owner_id):
            return [account] if (tenant_id,owner_id)==("local","owner") else []
        def get_provider_account(self, *, account_id, tenant_id, owner_id):
            return account if (account_id,tenant_id,owner_id)==("owned-codex","local","owner") else None
        def list_connections(self, **_kwargs): return []
    class Provider:
        def _model(self, _model, **_kwargs): return SimpleNamespace(effort_choices=("high",))
    store=Store(tmp_path/"standalone.db")
    service=WorkersProjectsService(store,StubRuntime(),control_plane_store=Accounts(),reconcile_on_startup=False,start_background_consumers=False)
    policy=AllowedAiPolicyService(store,ControlPlaneStore(store.db_path),service.runtime)
    service.bind_allowed_ai_policy(policy)
    service._ensure_worker_processor=lambda _worker_id: None
    service.start_assigned_run=lambda _worker_id: None
    core=CoordinatorService(store,service,Provider())
    project=store.create_project("owner","Synthetic coordinator","Four independent deliveries","codex-cli")
    config=CoordinatorConfig(model="exact-native-model",effort="high",
        scope=CoordinatorScope(project_id=project["project_id"],execution_mode="host"),
        routes=[{"id":"code", "profile":"codex-cli", "model":"codex-cli:gpt-6.1-sol", "effort":"high", "execution_mode":"host", "connection_id":"owned-codex"}])
    cid=core.create("local","owner",config)["conversation_id"]
    core.accept_turn("local","owner",cid,"turn","Create independent useful files",[Goal(id="first",text="First exact work"),Goal(id="second",text="Second exact work")])
    yield store,service,core,cid,project
    service.shutdown();store.close()


def dispatch_first(fixture):
    store,service,core,cid,_=fixture
    result=core.dispatch("local","owner",cid,Dispatch(goal_id="first",route_id="code",instruction="First exact work"))
    assert result["state"]=="queued",result
    return store.get_worker(result["worker_id"]),store.get_run(result["run_id"])


def test_standalone_owned_child_exempts_all_three_mutation_admissions(standalone, monkeypatch):
    store,service,core,cid,_=standalone
    assert standalone_coordinator_host_enabled() and not native_parallel_policy_enabled()
    scopes={"preflight":[],"preaccept":[]}
    original_preflight=store.acquire_preflight_capacity_reservation
    original_reserve=store.reserve_delegation
    def preflight(*args,**kwargs):
        scopes["preflight"].append(kwargs.get("mutation_scope"))
        return original_preflight(*args,**kwargs)
    def reserve(*args,**kwargs):
        scopes["preaccept"].append((kwargs.get("preaccept_host_lease") or {}).get("mutation_scope"))
        return original_reserve(*args,**kwargs)
    monkeypatch.setattr(store,"acquire_preflight_capacity_reservation",preflight)
    monkeypatch.setattr(store,"reserve_delegation",reserve)
    worker,run=dispatch_first(standalone)
    reserved=store.get_active_host_run_lease_for_run(run["run_id"])
    assert reserved is not None
    store.release_host_run_lease(reserved["lease_id"],executor_id=None,reason="synthetic restart before actual invocation")
    lease=service._acquire_host_run_lease(worker,run)
    assert scopes["preflight"] and set(scopes["preflight"])=={""}
    assert scopes["preaccept"]==[""]
    assert lease["mutation_scope"]==""
    bundle=json.loads(worker["bootstrap_bundle_json"])
    assert "launch_authority" not in bundle and "viventium_launch_authority" not in bundle
    assert not service._has_native_delegation_authority(worker)


@pytest.mark.parametrize("change", [
    "owner", "tenant", "model", "profile", "run", "unbound", "stopped", "goal_hold",
    "conversation_hold", "turn_hold", "route", "dispatch", "work_ref", "origin", "terminal",
    "effort", "account", "account_policy", "closed",
])
def test_current_run_exemption_rejects_missing_stale_or_foreign_proof(standalone, change):
    store,service,core,cid,_=standalone
    worker,run=dispatch_first(standalone)
    if change in {"owner","tenant","model","profile"}:
        worker={**worker,{"owner":"owner_id","tenant":"tenant_id"}.get(change,change):"foreign-value"}
    elif change=="run": run={**run,"run_id":"run_unrelated"}
    else:
        with store._connect() as conn:
            if change=="unbound": conn.execute("UPDATE coordinator_goals SET run_id='' WHERE conversation_id=?",(cid,))
            elif change=="stopped": conn.execute("UPDATE coordinator_goals SET intent_state='cancelled' WHERE conversation_id=?",(cid,))
            elif change=="goal_hold": conn.execute("UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?",(cid,))
            elif change=="conversation_hold": conn.execute("UPDATE coordinator_conversations SET restore_hold=1 WHERE conversation_id=?",(cid,))
            elif change=="turn_hold": conn.execute("UPDATE coordinator_turns SET restore_hold=1 WHERE conversation_id=?",(cid,))
            elif change=="route":
                config=json.loads(conn.execute("SELECT config_json FROM coordinator_conversations WHERE conversation_id=?",(cid,)).fetchone()[0]);config["routes"][0]["model"]="codex-cli:gpt-6-astra";conn.execute("UPDATE coordinator_conversations SET config_json=? WHERE conversation_id=?",(json.dumps(config),cid))
            elif change=="dispatch": conn.execute("UPDATE coordinator_goals SET dispatch_json=? WHERE conversation_id=?",(json.dumps({"goal_id":"first","route_id":"outside","instruction":"First exact work"}),cid))
            elif change=="work_ref": conn.execute("UPDATE coordinator_goals SET work_ref='work_unrelated' WHERE conversation_id=?",(cid,))
            elif change=="origin": conn.execute("UPDATE delegations SET origin_ref='unrelated' WHERE worker_id=?",(worker["worker_id"],))
            elif change=="terminal": conn.execute("UPDATE runs SET state='completed' WHERE run_id=?",(run["run_id"],))
            elif change=="closed": conn.execute("UPDATE workers SET state='terminated' WHERE worker_id=?",(worker["worker_id"],))
            elif change in {"effort","account","account_policy"}:
                bundle=json.loads(worker["bootstrap_bundle_json"])
                if change=="effort": bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"]="low"
                elif change=="account": bundle["provider_account"]["account_id"]="foreign-account"
                else: bundle["provider_account"]["policy"]="personal_preferred"
                store.update_worker(worker["worker_id"],bootstrap_bundle_json=json.dumps(bundle))
    assert service._host_mutation_scope(worker,run_id=run["run_id"])


def test_existing_worker_reuse_and_restart_have_only_exact_run_authority(standalone):
    store,service,core,cid,project=standalone
    worker=store.create_worker(project_id=project["project_id"],owner_id="owner",name="Existing specialist",role="Retain expertise",profile="codex-cli",backend="codex-cli",runtime="codex-cli",model="gpt-6.1-sol",execution_mode="host",workspace_kind="named",bootstrap_bundle={"provider_model":"gpt-6.1-sol"})
    store.update_worker_state(worker["worker_id"],"ready")
    result=core.dispatch("local","owner",cid,Dispatch(goal_id="first",worker_id=worker["worker_id"],instruction="Use this exact existing expert"))
    assert result["state"]=="queued",result
    current=store.get_worker(worker["worker_id"])
    assert not result["work_ref"]
    assert service._host_mutation_scope(current,run_id=result["run_id"])==""
    restarted=WorkersProjectsService(store,StubRuntime(),control_plane_store=service.control_plane_store,reconcile_on_startup=False,start_background_consumers=False)
    try:
        assert restarted._host_mutation_scope(current,run_id=result["run_id"])==""
        manual=store.create_run(worker["worker_id"],project["project_id"],"Later unrelated manual work")
        assert restarted._host_mutation_scope(store.get_worker(worker["worker_id"]),run_id=manual["run_id"])
    finally: restarted.shutdown()


def test_public_bootstrap_cannot_buy_standalone_mutation_authority(standalone):
    store,service,_core,cid,project=standalone
    worker=store.create_worker(project_id=project["project_id"],owner_id="owner",name="Manual worker",role="Manual work",profile="codex-cli",backend="codex-cli",runtime="codex-cli",model="gpt-6.1-sol",execution_mode="host",bootstrap_bundle={"launch_authority":{"version":1,"kind":"conversation_orchestrator","execution_mode":"host"},"coordinator_origin_ref":cid+":first","host_mutation_scope":"invented-independent-scope"})
    run=store.create_run(worker["worker_id"],project["project_id"],"Manual work")
    assert service._host_mutation_scope(store.get_worker(worker["worker_id"]),run_id=run["run_id"])
    assert not service._has_native_delegation_authority(worker)


@pytest.mark.parametrize("effect", ["queue","message","retry"])
def test_current_retry_and_accepted_queue_effect_keep_the_exact_binding(standalone, effect):
    store,service,core,cid,_=standalone
    worker,original=dispatch_first(standalone)
    replacement=store.create_run(worker["worker_id"],worker["project_id"],"Accepted exact follow-up")
    with store._connect() as conn:
        conn.execute("INSERT INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json,response_json) VALUES(?,?,?,?,?)",(cid,"first","synthetic-"+effect,json.dumps({"action":effect,"run_id":original["run_id"]}),json.dumps({"run_id":replacement["run_id"]})))
        if effect=="retry": conn.execute("UPDATE coordinator_goals SET run_id=? WHERE conversation_id=? AND goal_id='first'",(replacement["run_id"],cid))
    current=store.get_worker(worker["worker_id"])
    assert service._host_mutation_scope(current,run_id=replacement["run_id"])==""
    with store._connect() as conn: conn.execute("UPDATE coordinator_goals SET intent_state='cancelled' WHERE conversation_id=?",(cid,))
    assert service._host_mutation_scope(current,run_id=replacement["run_id"])


def test_accepted_standalone_replay_does_not_rescore_later_hold_or_capacity(standalone, monkeypatch):
    store,service,_core,cid,_=standalone
    worker,run=dispatch_first(standalone)
    record=store.get_delegation_for_worker(worker["worker_id"],tenant_id="local",owner_id="owner")
    with store._connect() as conn: conn.execute("UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?",(cid,))
    monkeypatch.setattr(service,"_queued_runtime_preflight",lambda *_a,**_k: pytest.fail("accepted replay must not rescore capacity"))
    replay=service.reserve_delegation(tenant_id="local",owner_id="owner",idempotency_key=record["idempotency_key"],request_digest=record["request_digest"],origin_ref=record["origin_ref"],title="Ignored replay metadata",goal="Ignored replay metadata",instruction="Ignored replay metadata",origin_surface="coordinator",worker_name="Ignored",worker_role="worker",profile="unavailable-profile",execution_mode="host",trusted_coordinator_model="unsupported-model")
    assert replay["idempotent_replay"] and replay["initial_run_id"]==run["run_id"]


@pytest.mark.parametrize("change", ["goal_hold","conversation_hold","turn_hold","stopped","instruction","goal","origin","idempotency","effort","account"])
def test_standalone_owner_route_validation_precedes_any_native_preflight(standalone, monkeypatch, change):
    store,service,core,cid,_=standalone
    command=Dispatch(goal_id="first",route_id="code",instruction="First exact work")
    with store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET dispatch_json=? WHERE conversation_id=? AND goal_id='first'",(command.model_dump_json(exclude_defaults=True),cid))
        if change=="goal_hold":conn.execute("UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?",(cid,))
        elif change=="conversation_hold":conn.execute("UPDATE coordinator_conversations SET restore_hold=1 WHERE conversation_id=?",(cid,))
        elif change=="turn_hold":conn.execute("UPDATE coordinator_turns SET restore_hold=1 WHERE conversation_id=?",(cid,))
        elif change=="stopped":conn.execute("UPDATE coordinator_goals SET intent_state='cancelled' WHERE conversation_id=?",(cid,))
    origin=cid+":first"
    kwargs={"tenant_id":"local","owner_id":"owner","idempotency_key":origin,"request_digest":"synthetic-unaccepted-exact-digest","origin_ref":origin,"title":"first","goal":"First exact work","instruction":"First exact work","origin_surface":"coordinator","worker_name":"first","worker_role":"worker","profile":"codex-cli","execution_mode":"host","trusted_coordinator_model":"gpt-6.1-sol","bootstrap_bundle":{"provider_model":"gpt-6.1-sol","env":{"WPR_CODEX_CLI_REASONING_EFFORT":"high"},"provider_account":{"policy":"personal_required","account_id":"owned-codex"}},"start_run":False}
    if change in {"instruction","goal"}:kwargs[change]="Changed exact source"
    elif change=="origin":kwargs["origin_ref"]="unrelated:first"
    elif change=="idempotency":kwargs["idempotency_key"]="different-admission-key"
    elif change=="effort":kwargs["bootstrap_bundle"]["env"]["WPR_CODEX_CLI_REASONING_EFFORT"]="low"
    elif change=="account":kwargs["bootstrap_bundle"]["provider_account"]["account_id"]="foreign-account"
    monkeypatch.setattr(service,"_queued_runtime_preflight",lambda *_a,**_k: pytest.fail("invalid origin must fail before any CLI/capacity preflight"))
    with pytest.raises(BackgroundWorkerConfigurationError):service.reserve_delegation(**kwargs)
    assert store.count_workers()==0


def _start_exact_in_process(store,service,worker,run):
    claim=store.claim_next_queued_run(worker["worker_id"],executor_id=service.executor_id)
    assert claim["run_id"]==run["run_id"]
    lease=service._acquire_host_run_lease(worker,claim)
    assert store.admit_claimed_run(run["run_id"],lease_id=lease["lease_id"],executor_id=service.executor_id)
    invoked=store.mark_run_runtime_invoked(run["run_id"],lease_id=lease["lease_id"],executor_id=service.executor_id)
    confirmed=store.confirm_host_run_start(worker_id=worker["worker_id"],run_id=run["run_id"],run_started_at=invoked["runtime_invoked_at"],lease_id=lease["lease_id"],startup_token=lease["startup_token"],executor_id=service.executor_id,identity_kind="in_process",pid=None,process_group=None,process_start_identity="",container_id="",session_id="in-process")
    assert confirmed
    store.update_worker_state(worker["worker_id"],"running")
    return lease


def test_stopping_one_owned_child_preserves_other_running_child(standalone):
    store,service,core,cid,_=standalone
    first,first_run=dispatch_first(standalone)
    other=core.dispatch("local","owner",cid,Dispatch(goal_id="second",route_id="code",instruction="Second exact work"))
    assert other["state"]=="queued",other
    second=store.get_worker(other["worker_id"]);second_run=store.get_run(other["run_id"])
    first_lease=_start_exact_in_process(store,service,first,first_run)
    second_lease=_start_exact_in_process(store,service,second,second_run)
    assert first_lease["mutation_scope"]==second_lease["mutation_scope"]==""
    core.control("local","owner",cid,"first",Control(action="stop",run_id=first_run["run_id"],idempotency_key="stop-only-first"))
    assert store.get_run(first_run["run_id"])["state"]=="cancelled"
    assert store.get_run(second_run["run_id"])["state"]=="running"
    assert store.get_active_host_run_lease_for_run(second_run["run_id"])["lease_id"]==second_lease["lease_id"]
    assert service._host_mutation_scope(store.get_worker(second["worker_id"]),run_id=second_run["run_id"])==""


def test_two_manual_host_jobs_still_share_the_same_mutation_fence(standalone):
    store,service,_core,_cid,project=standalone
    work=[]
    for name in ["Manual A","Manual B"]:
        worker=store.create_worker(project_id=project["project_id"],owner_id="owner",name=name,role="Manual work",profile="codex-cli",backend="codex-cli",runtime="codex-cli",model="gpt-6.1-sol",execution_mode="host")
        work.append((worker,store.create_run(worker["worker_id"],project["project_id"],"Independent-looking manual instruction")))
    first=service._acquire_host_run_lease(*work[0])
    assert first["mutation_scope"]
    with pytest.raises(HostCapacityError) as error:service._acquire_host_run_lease(*work[1])
    assert error.value.capacity_class=="mutation_scope"


def test_preaccept_lease_cannot_keep_an_obsolete_standalone_exemption(standalone):
    store,service,_core,cid,_=standalone
    worker,run=dispatch_first(standalone)
    assert store.get_active_host_run_lease_for_run(run["run_id"])["mutation_scope"]==""
    with store._connect() as conn:conn.execute("UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?",(cid,))
    with pytest.raises(WorkAdmissionError) as conflict:service._acquire_host_run_lease(worker,run)
    assert conflict.value.code=="coordinator_run_scope_changed"
    assert store.get_run(run["run_id"])["runtime_invoked_at"] is None
