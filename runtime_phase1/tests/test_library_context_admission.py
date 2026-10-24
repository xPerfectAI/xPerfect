"""Library context uses existing admission, exact files and source packing."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workers_projects_runtime.bootstrap import remove_projected_library_files
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.coordinator import (
    CoordinatorConfig, CoordinatorConflict, CoordinatorScopeError, CoordinatorService,
    Dispatch, Goal, Route,
)
from workers_projects_runtime.coordinator_api import install_coordinator_routes
from workers_projects_runtime.personal_library import PersonalLibrary
from workers_projects_runtime.store import Store, canonical_parallel_clean_room_bootstrap
from workers_projects_runtime.worker_configuration import WorkerConfiguration
from workers_projects_runtime.worker_context_sources import authorized_sources
from workers_projects_runtime.workspace_files import WorkspaceFiles


class Provider:
    def __init__(self, profile="direct-xai"):
        self.profile, self.packets = profile, []

    def _model(self, model, **scope):
        return SimpleNamespace(id=model, effort_choices=("high",), harness_profile=self.profile)

    def start(self, payload, **scope):
        self.packets.append(payload.model_dump())
        return {"request_id": f"request-{len(self.packets)}", "state": "accepted"}


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    store = Store(tmp_path / "state.db")
    ControlPlaneStore(tmp_path / "state.db")
    files = WorkspaceFiles(store)
    service = SimpleNamespace(store=store, files=files, control_plane_store=None)
    service.personal_library = PersonalLibrary(service)
    provider = Provider()
    coordinator = CoordinatorService(store, service, provider)
    yield coordinator, service, provider
    store.close()


def create(core, cap=24000):
    return core.create("local", "owner", CoordinatorConfig(
        model="exact-configured-model", effort="high", library_text_chars=cap,
        routes=[Route(id="native", profile="claude-code", model="exact-worker-model", effort="high", execution_mode="host")],
    ))["conversation_id"]


def note(service, text="The verified project code is ORCHID.", key="note"):
    return service.personal_library.create_item("local", "owner", {"name": key, "kind": "note", "text": text}, key)


def ref(item):
    return {"item_id": item["item_id"], "version_id": item["version_id"]}


def test_no_selection_does_not_construct_library(tmp_path):
    class Service:
        @property
        def personal_library(self):
            raise AssertionError("Ordinary turns must not initialize Library")
    store = Store(tmp_path / "state.db")
    provider = Provider()
    coordinator = CoordinatorService(store, Service(), provider)
    cid = create(coordinator)
    coordinator.accept_turn("local", "owner", cid, "turn", "Hello")
    coordinator.start_turn("local", "owner", cid, "turn")
    assert [m["content"] for m in provider.packets[0]["messages"]][1:] == ["Hello"]
    store.close()


def test_direct_text_is_actual_bounded_reference_after_unchanged_history(core):
    coordinator, service, provider = core
    small = note(service)
    large = note(service, "A" * 30000, "large")
    cid = create(coordinator, 100)
    coordinator.accept_turn("local", "owner", cid, "first", "Original words  é\nunchanged", library_refs=[ref(small), ref(large)])
    coordinator.start_turn("local", "owner", cid, "first")
    packet = provider.packets[-1]
    assert packet["messages"][1]["content"] == "Original words  é\nunchanged"
    assert packet["messages"][-1]["role"] == "tool"
    data = json.loads(packet["messages"][-1]["content"])
    assert data["sources"][0]["text"] == "The verified project code is ORCHID."
    assert data["sources"][1]["loaded"] is False
    assert data["sources"][1]["status"] == "Not loaded for this Queen; available to Helpers"
    assert "files" not in packet["metadata"]["bootstrap_bundle"]
    assert packet["tools"] is None
    state = coordinator.snapshot("local", "owner", cid)
    assert state["library"]["context"][0]["loaded"] is True
    assert "text" not in state["library"]["context"][0]
    assert "library_json" not in state["turns"][0]
    assert "source_path_token" not in json.dumps(state)


def test_appended_library_evidence_preserves_latest_actionable_user_turn(core):
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest, GLASSHIVE_MODELS, _admit_conversation_history, _protected_source_indices, _visible_message_keys
    coordinator, service, provider = core
    saved = note(service, "Reference evidence. " * 1200)
    cid = create(coordinator, 24000)
    coordinator.accept_turn("local", "owner", cid, "first", "Earlier question")
    coordinator.accept_turn("local", "owner", cid, "second", "Delegate the complete source verification to a Helper", library_refs=[ref(saved)])
    coordinator.start_turn("local", "owner", cid, "second")
    packet = ChatCompletionRequest.model_validate(provider.packets[-1])
    keys = _visible_message_keys(packet.messages, packet.metadata.visible_message_chain)
    protected = _protected_source_indices(packet, keys)
    result = _admit_conversation_history(packet.messages, start_at=0, turn_context="", model=GLASSHIVE_MODELS["direct-xai:grok-4.7"], protected_indices=protected)
    instruction = result[0] if isinstance(result, tuple) else result.instruction
    current = instruction.split("<viventium_current_accepted_turn_v1>",1)[1]
    assert "Delegate the complete source verification to a Helper" in current
    assert "Earlier question" not in current
    assert "Reference evidence. " * 1200 in current


@pytest.mark.parametrize("profile", ["direct-xai", "codex-cli"])
def test_transient_library_reference_does_not_advance_original_history_cursor(core, profile):
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest, GLASSHIVE_MODELS, _admit_conversation_history, _caller_history_count, _protected_source_indices, _visible_message_keys
    coordinator, service, provider = core
    provider.profile = profile
    saved = note(service)
    cid = create(coordinator)
    cursor = 0
    for number in range(1,4):
        turn = "turn-"+str(number)
        original = "Exact original user job "+str(number)
        coordinator.accept_turn("local", "owner", cid, turn, original, library_refs=[ref(saved)] if number==1 else None)
        coordinator.start_turn("local", "owner", cid, turn)
        packet = ChatCompletionRequest.model_validate(provider.packets[-1])
        protected = _protected_source_indices(packet, _visible_message_keys(packet.messages,packet.metadata.visible_message_chain))
        admitted = _admit_conversation_history(packet.messages, start_at=cursor, turn_context="", model=GLASSHIVE_MODELS["direct-xai:grok-4.7"], protected_indices=protected)[0]
        current = admitted.split("<viventium_current_accepted_turn_v1>",1)[1]
        assert original in current
        assert "selected_library_reference_material" in current
        cursor = _caller_history_count(packet)+1
        with coordinator.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET response_json=? WHERE conversation_id=? AND turn_id=?", (json.dumps({"choices":[{"message":{"content":"Exact assistant answer "+str(number)}}]}),cid,turn))
    with coordinator.store._connect() as conn:
        selected=conn.execute("SELECT library_json FROM coordinator_conversations WHERE conversation_id=?",(cid,)).fetchone()[0]
        conn.execute("INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,library_json,created_at) VALUES(?,?,?,?,?,?)",(cid,"result","Actual Helper evidence","worker_results",selected,"9999"))
    coordinator.start_turn("local", "owner", cid, "result")
    packet = ChatCompletionRequest.model_validate(provider.packets[-1])
    protected = _protected_source_indices(packet,_visible_message_keys(packet.messages,packet.metadata.visible_message_chain))
    admitted=_admit_conversation_history(packet.messages,start_at=cursor,turn_context="",model=GLASSHIVE_MODELS["direct-xai:grok-4.7"],protected_indices=protected)[0]
    assert "Actual Helper evidence" in admitted
    assert _caller_history_count(packet) == len(packet.messages)-1


def test_mismatched_transient_reference_cannot_shift_history_cursor(core):
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest, _caller_history_count
    from fastapi import HTTPException
    coordinator, service, provider = core
    saved=note(service);cid=create(coordinator)
    coordinator.accept_turn("local","owner",cid,"turn","Original job",library_refs=[ref(saved)])
    coordinator.start_turn("local","owner",cid,"turn")
    for changed in ("body","id","role"):
        packet=ChatCompletionRequest.model_validate(provider.packets[-1])
        if changed=="body":packet.messages[-1].content="Replaced source"
        elif changed=="id":packet.metadata.bootstrap_bundle["library_selection"]["reference_message"]["id"]="foreign"
        else:packet.messages[-1].role="user"
        with pytest.raises(HTTPException):_caller_history_count(packet)


def test_selection_inherits_clears_and_retry_survives_personal_delete(core):
    coordinator, service, provider = core
    saved = note(service)
    cid = create(coordinator)
    coordinator.accept_turn("local", "owner", cid, "first", "First", library_refs=[ref(saved)])
    service.personal_library.delete_item("local", "owner", saved["item_id"], True)
    # Admission, retries and the next turn use their retained exact version.
    coordinator.accept_turn("local", "owner", cid, "first", "First", library_refs=[ref(saved)])
    coordinator.start_turn("local", "owner", cid, "first")
    assert json.loads(provider.packets[-1]["messages"][-1]["content"])["sources"][0]["text"]
    coordinator.accept_turn("local", "owner", cid, "second", "Second")
    assert coordinator.snapshot("local", "owner", cid)["library"]["refs"] == [ref(saved)]
    coordinator.accept_turn("local", "owner", cid, "third", "Third", library_refs=[])
    assert coordinator.snapshot("local", "owner", cid)["library"]["refs"] == []
    with pytest.raises(CoordinatorConflict, match="idempotency"):
        coordinator.accept_turn("local", "owner", cid, "first", "First", library_refs=[])
    with pytest.raises(CoordinatorScopeError):
        coordinator.snapshot("local", "other", cid)


def test_native_queen_gets_supported_files_without_inlining_body(core):
    coordinator, service, provider = core
    provider.profile = "claude-code"
    saved = note(service, "Native exact source\n" * 2000)
    cid = create(coordinator, 1)
    coordinator.accept_turn("local", "owner", cid, "turn", "Read selected material", library_refs=[ref(saved)])
    coordinator.start_turn("local", "owner", cid, "turn")
    packet = provider.packets[-1]
    selection = packet["metadata"]["bootstrap_bundle"]["library_selection"]
    assert selection["upload_ids"] == [saved["uploads"][0]["upload_id"]]
    assert selection["upload_revisions"] == [saved["uploads"][0]["revision"]]
    assert "files" not in packet["metadata"]["bootstrap_bundle"]
    data = json.loads(packet["messages"][-1]["content"])
    assert data["sources"][0]["delivery"] == "native_file"
    assert "text" not in data["sources"][0]


def test_bundle_name_survives_snapshot_without_an_extra_catalog_read(core):
    coordinator, service, _ = core
    saved = note(service)
    bundle = service.personal_library.create_item("local", "owner", {"name": "Reporting bundle", "kind": "bundle", "refs": [ref(saved)]}, "bundle")
    cid = create(coordinator)
    coordinator.accept_turn("local", "owner", cid, "turn", "Use this bundle", library_refs=[ref(bundle)])
    summary = coordinator.snapshot("local", "owner", cid)["library"]
    assert summary["refs"] == [ref(bundle)]
    assert any(i["name"] == "Reporting bundle" for i in summary["items"])


def test_source_only_native_reference_stays_pending_and_visible(core):
    coordinator, service, provider = core
    saved = service.personal_library.create_item("local", "owner", {"name":"Native setup reference", "kind":"native", "source_uri":"https://example.org/native/package"}, "native-reference")
    cid = create(coordinator)
    coordinator.accept_turn("local", "owner", cid, "turn", "Use the available setup", library_refs=[ref(saved)])
    coordinator.start_turn("local", "owner", cid, "turn")
    data = json.loads(provider.packets[-1]["messages"][-1]["content"])
    assert data["sources"] == []
    assert data["requirements_not_applied"][0]["state"] == "needs_setup"
    assert data["requirements_not_applied"][0]["source_uri"] == "https://example.org/native/package"
    assert coordinator.snapshot("local", "owner", cid)["library"]["items"][0]["source_uri"] == "https://example.org/native/package"
    assert "claude_project_mcp" not in provider.packets[-1]["metadata"]["bootstrap_bundle"]


def test_conversation_selection_retains_versions_without_copying_skill_bodies(core, monkeypatch):
    coordinator, service, provider = core
    content = "Exact reusable skill evidence. " * 9000
    saved = service.personal_library.create_item("local", "owner", {"name":"Large saved skill", "kind":"skill", "profiles":["claude-code"], "files":[{"path":".claude/skills/example/SKILL.md", "content":content}]}, "large-skill")
    cid = create(coordinator)
    coordinator.accept_turn("local","owner",cid,"first","Original job",library_refs=[ref(saved)])
    with coordinator.store._connect() as conn:
        conversation = conn.execute("SELECT library_json FROM coordinator_conversations WHERE conversation_id=?",(cid,)).fetchone()[0]
        accepted = conn.execute("SELECT library_json FROM coordinator_turns WHERE conversation_id=?",(cid,)).fetchone()[0]
    assert len(conversation)<5000 and len(accepted)<5000
    assert "files" not in conversation and "payload" not in conversation
    assert service.personal_library.get_item("local","owner",saved["item_id"])["payload"]["files"][0]["content"] == content
    monkeypatch.setattr(service.personal_library,"resolve_refs",lambda *args,**kwargs:pytest.fail("Unchanged retained selection should not resolve catalog bodies again"))
    coordinator.accept_turn("local","owner",cid,"second","Next job",library_refs=[ref(saved)])
    coordinator.start_turn("local","owner",cid,"second")
    assert content not in json.dumps(provider.packets[-1])


def test_direct_conversation_uses_owned_protected_record_not_public_account(core, monkeypatch):
    coordinator, service, _ = core
    cp = ControlPlaneStore(coordinator.store.db_path)
    service.control_plane_store = cp
    account = cp.create_provider_account(tenant_id="local", owner_id="owner", provider="grok", label="Protected Direct account", auth_method="api_key", platform_support="supported", secret_locator="native-home://api-key", status="ready")
    assert "secret_locator" not in cp.get_provider_account(account_id=account["account_id"], tenant_id="local", owner_id="owner")
    config = CoordinatorConfig(model="exact-configured-model", effort="high", routes=[Route(id="native", profile="claude-code", model="exact-worker-model", effort="high", execution_mode="host")])
    monkeypatch.setenv("GLASSHIVE_COORDINATOR_CONFIG_JSON", config.model_dump_json())
    monkeypatch.setenv("GLASSHIVE_ENABLE_NATIVE_API_KEYS", "1")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    app = FastAPI()
    install_coordinator_routes(app, coordinator, lambda request:("local", "owner"), lambda *args:config)
    with TestClient(app) as client:
        response = client.post("/v1/coordinator/conversations",json={"account_id":account["account_id"]})
    assert response.status_code == 200, response.text
    assert response.json()["scope"]["connection_id"] == account["account_id"]
    assert response.json()["model"] == "exact-configured-model"


@pytest.mark.parametrize("mismatch", ["", "owner_id", "session_id", "worker_id"])
def test_native_goal_source_uses_actual_request_not_a_model_guessed_id(core, tmp_path, mismatch):
    coordinator, _, _ = core
    cid = create(coordinator)
    coordinator.accept_turn("local", "owner", cid, "earlier", "Earlier request")
    coordinator.accept_turn("local", "owner", cid, "current", "Current independent job")
    pid = coordinator.snapshot("local", "owner", cid)["scope"]["project_id"]
    worker = coordinator.store.create_worker(pid, "owner", "Queen", "conversation-agent", "codex-cli", "local", "codex", "exact-model", execution_mode="host")
    session = coordinator.store.upsert_provider_session(tenant_id="local", owner_id="owner", conversation_id=cid, agent_id="coordinator", model_id="exact-model", project_id=pid, worker_id=worker["worker_id"], workspace_dir=str(tmp_path), access_mode="full")
    run = coordinator.store.create_run(worker["worker_id"], pid, "Current independent job")
    request, _ = coordinator.store.create_provider_request(tenant_id="local", owner_id="owner", session_id=session["session_id"], idempotency_key="actual-request", message_id="current", stream_id="", requested_history_count=2)
    coordinator.store.update_provider_request(request["request_id"], run_id=run["run_id"])
    # The coordinator turn need not yet have attached the returned request receipt.
    # Source identity already exists at the actual provider's admitted request.
    principal = {"tenant_id":"local", "owner_id":"owner", "worker_id":worker["worker_id"], "run_id":run["run_id"]}
    if mismatch == "worker_id":
        other = coordinator.store.create_worker(pid, "owner", "Other", "worker", "codex-cli", "local", "codex", "exact-model")
        coordinator.store.update_run(run["run_id"], worker_id=other["worker_id"])
    elif mismatch == "session_id":
        other = coordinator.store.create_worker(pid, "owner", "Other", "worker", "codex-cli", "local", "codex", "exact-model")
        foreign = coordinator.store.upsert_provider_session(tenant_id="local", owner_id="owner", conversation_id=create(coordinator), agent_id="coordinator", model_id="exact-model", project_id=pid, worker_id=other["worker_id"], workspace_dir=str(tmp_path), access_mode="full")
        coordinator.store.update_provider_request(request["request_id"], session_id=foreign["session_id"])
    elif mismatch:
        coordinator.store.update_provider_request(request["request_id"], **{mismatch:"foreign"})
    if mismatch:
        with pytest.raises(CoordinatorScopeError):
            coordinator.accept_native_goals(principal, [Goal(id="job", text="Current independent job")])
        assert coordinator.snapshot("local", "owner", cid)["goals"] == []
    else:
        state = coordinator.accept_native_goals(principal, [Goal(id="job", text="Current independent job")])
        assert state["goals"][0]["source_turn_id"] == "current"
        # Explicit source selection is still available to the authenticated owner API.
        state = coordinator.accept_goals("local", "owner", cid, "earlier", [Goal(id="prior", text="Earlier request")])
        assert next(g for g in state["goals"] if g["goal_id"] == "prior")["source_turn_id"] == "earlier"


def test_helpers_bind_exact_accepted_versions_before_native_start(core):
    coordinator, service, _ = core
    saved = note(service, "The complete unchanged material." * 2000)
    cid = create(coordinator, 0)
    coordinator.accept_turn("local", "owner", cid, "turn", "Original job", [Goal(id="job", text="Original job")], [ref(saved)])
    # Removing the personal entry cannot lose an already accepted job's sources.
    service.personal_library.delete_item("local", "owner", saved["item_id"], True)
    project_id = coordinator.snapshot("local", "owner", cid)["scope"]["project_id"]
    reserved, started = [], []

    def reserve(**kwargs):
        if reserved:
            return reserved[0]
        worker = coordinator.store.create_worker(project_id, "owner", "Helper", "worker", "claude-code", "local", "claude", "exact-worker-model", execution_mode="host", bootstrap_bundle=kwargs["bootstrap_bundle"])
        run = coordinator.store.create_run(worker["worker_id"], project_id, kwargs["instruction"])
        reserved.append({"work_ref": "work-job", "worker_id": worker["worker_id"], "initial_run_id": run["run_id"]})
        return reserved[0]

    def start(worker_id):
        worker = coordinator.store.get_worker(worker_id)
        bundle = json.loads(worker["bootstrap_bundle_json"])
        assert bundle["library_selection"]["refs"] == [ref(saved)]
        entries = bundle["files"]
        assert [item["managed_upload_id"] for item in entries] == [saved["uploads"][0]["upload_id"]]
        with coordinator.store._connect() as conn:
            binding = json.loads(conn.execute("SELECT manifest_json FROM workspace_file_bindings WHERE worker_id=?", (worker_id,)).fetchone()[0])
        assert binding[0]["revision"] == saved["uploads"][0]["revision"]
        started.append(worker_id)

    service.reserve_delegation, service.start_assigned_run = reserve, start
    result = coordinator.dispatch("local", "owner", cid, Dispatch(goal_id="job", route_id="native", instruction="Original job"))
    assert result["state"] == "queued", result
    assert started == [reserved[0]["worker_id"]]
    # Reservation recovery uses the exact same idempotent binding, never a brief.
    assert coordinator.dispatch("local", "owner", cid, Dispatch(goal_id="job", route_id="native", instruction="Original job"))["run_id"] == result["run_id"]
    with coordinator.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_bindings WHERE worker_id=?", (result["worker_id"],)).fetchone()[0] == 1


def test_original_user_goal_precedes_large_lookup_only_library(core, tmp_path):
    coordinator, service, _ = core
    cid = create(coordinator)
    coordinator.accept_turn("local", "owner", cid, "origin", "Exact original objective, not the dispatch brief")
    with coordinator.store._connect() as conn:
        conn.execute("INSERT INTO coordinator_turns(conversation_id,turn_id,message,created_at) VALUES(?,?,?,?)", (cid,"earlier-steer","Keep this exact earlier correction", "0000"))
        conn.execute("INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,created_at) VALUES(?,?,?,?,?)", (cid,"result","Worker result", "worker_results", "9999"))
    coordinator.accept_goals("local", "owner", cid, "result", [Goal(id="goal", text="Objective")])
    project = coordinator.store.create_project("owner", "Example", "Goal", "claude-code")
    worker = coordinator.store.create_worker(project["project_id"], "owner", "Helper", "assistant", "claude-code", "local", "claude", "exact", bootstrap_bundle={"library_selection":{"items":[{"item_id":str(i),"name":"Large metadata label "*8} for i in range(500)]},"files": [{"path":"guide.txt", "content":"Large knowledge "*5000, "context_delivery":"lookup"}]})
    run = coordinator.store.create_run(worker["worker_id"], project["project_id"], "Additional task focus")
    with coordinator.store._connect() as conn:
        # This fixture represents a delegated Helper, whose retained context remains worker-scoped.
        conn.execute("UPDATE coordinator_goals SET run_id=?,work_ref='helper-work' WHERE conversation_id=?", (run["run_id"],cid))
    configuration = WorkerConfiguration(coordinator.store, None, source_resolver=lambda w: authorized_sources(coordinator.store, None, w))
    view = configuration.get("local", "owner", worker["worker_id"])
    sources = view["effective"]["context"]["sources"]
    assert sources[0]["id"].endswith(":origin:user")
    assert sources[0]["inline_chars"] == len("Exact original objective, not the dispatch brief")
    earlier = next(s for s in sources if s["id"].endswith(":earlier-steer:user"))
    assert earlier["inline_chars"] == len("Keep this exact earlier correction")
    assert sources.index(earlier) < next(i for i,s in enumerate(sources) if s["id"] == "library:selection")
    knowledge = next(s for s in sources if s["id"] == "bootstrap:file:0")
    assert knowledge["inline_chars"] == 0 and knowledge["delivery"] == "retrieval"
    assert knowledge["packing"] == "lookup"
    assert "Exact original objective" in next(s["text"] for s in configuration.sources(worker) if s["id"].endswith(":origin:user"))


def test_native_contribution_cleanup_preserves_edited_shared_and_links(tmp_path):
    root = tmp_path / "workspace"
    skill = root / ".claude/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Original")
    sha = hashlib.sha256(b"Original").hexdigest()
    entry = {"path": ".claude/skills/example/SKILL.md", "sha256": sha}
    assert remove_projected_library_files(root, [entry], [{"path":entry["path"]}])["retained"][0]["reason"] == "still_used"
    skill.write_text("Edited")
    assert remove_projected_library_files(root, [entry], [])["retained"][0]["reason"] == "edited"
    outside = tmp_path / "outside.md"; outside.write_text("Original")
    skill.unlink(); skill.symlink_to(outside)
    assert remove_projected_library_files(root, [entry], [])["retained"][0]["reason"] == "replaced_path"
    assert outside.read_text() == "Original"
    skill.unlink(); skill.write_text("Original")
    assert remove_projected_library_files(root, [entry], [])["removed"] == [entry["path"]]
    assert not skill.exists()
    assert remove_projected_library_files(root, [entry], [])["removed"] == []
    with pytest.raises(ValueError):
        remove_projected_library_files(root, [{"path":".codex/config.toml", "sha256":sha}], [])


@pytest.mark.parametrize("field,value", [
    ("source_path", "/private/source"),
    ("env", {"TOKEN": "synthetic"}),
    ("claude_project_mcp", {"extra": {"command": "runner"}}),
    ("text", "An invented hidden instruction"),
    ("refs", [{"item_id": "item", "version_id": "version", "scope": "all"}]),
    ("upload_ids", [False]),
])
def test_library_context_carrier_cannot_smuggle_authority(field, value):
    with pytest.raises(ValueError):
        canonical_parallel_clean_room_bootstrap({"library_selection": {field: value}})


def test_clean_room_files_keep_only_typed_context_hints_and_original_authority():
    entry = {"path": "uploads/note.txt", "source_path": "/authorized/source", "source_path_token": "existing-token", "managed_upload_id": "upload", "managed_projection_id": "projection", "sha256": "a"*64, "allow_empty": True, "context_delivery": "lookup", "extra_authority": {"scope": "all"}}
    rendered = canonical_parallel_clean_room_bootstrap({"files": [entry]})["files"][0]
    assert rendered == {k:v for k,v in entry.items() if k != "extra_authority"}
    for field, value in (("managed_upload_id", True), ("sha256", "broken"), ("allow_empty", "true"), ("context_delivery", "execute")):
        with pytest.raises(ValueError):
            canonical_parallel_clean_room_bootstrap({"files": [{**entry, field:value}]})


@pytest.mark.parametrize("uri", ["https://user:password@example.org/package", "https://example.org/package?token=synthetic", "file:///private/package", "https://example.org/package#secret"])
def test_source_only_metadata_refuses_private_or_credential_bearing_locations(uri):
    with pytest.raises(ValueError):
        canonical_parallel_clean_room_bootstrap({"library_selection":{"items":[{"item_id":"item", "version_id":"version", "name":"Native reference", "kind":"native", "source_uri":uri}]}})


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_only_live_coordinator_projection_gets_its_codex_tools_approved(transport):
    import tomllib
    from workers_projects_runtime.coordinator_mcp import project_coordinator_bootstrap
    projection={"worker_id":"queen", "run_id":"current", "token":"synthetic-bound-token", "url":"http://127.0.0.1:8766/v1/native/coordinator/", "transport":transport}
    worker={"worker_id":"queen", "_active_run_id":"current", "_coordinator_native_projection":projection}
    original={"codex_config_append":'[mcp_servers.existing]\nurl="https://example.org/mcp"\ndefault_tools_approval_mode="auto"\n'}
    result=project_coordinator_bootstrap(worker,original)
    servers=tomllib.loads(result["codex_config_append"])["mcp_servers"]
    assert servers["xperfect-coordinator"]["default_tools_approval_mode"]=="approve"
    assert servers["existing"]["default_tools_approval_mode"]=="auto"
    assert project_coordinator_bootstrap({"worker_id":"queen"},original)==original
    for bad in ({"worker_id":"foreign"},{"run_id":"stale"},{"token":""}):
        with pytest.raises(CoordinatorScopeError):
            project_coordinator_bootstrap({**worker,"_coordinator_native_projection":{**projection,**bad}},original)


@pytest.fixture
def standalone_host(tmp_path, monkeypatch):
    from workers_projects_runtime.service import WorkersProjectsService
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    for key,value in {"GLASSHIVE_SECURITY_MODE":"local", "GLASSHIVE_AUTH_MODE":"local", "WPR_DEFAULT_EXECUTION_MODE":"host", "GLASSHIVE_HOST_WORKERS_ENABLED":"true", "GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS":"false", "GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED":"false", "WPR_MODEL_CODEX_CLI":"gpt-6.1-sol", "WPR_MODEL_HOST_CODEX_CLI":"gpt-6.1-sol"}.items():
        monkeypatch.setenv(key,value)
    for key in ("XPERFECT_EXECUTION_PROFILE","VIVENTIUM_PARALLEL_WORK_EXECUTION_MODE","VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY"):
        monkeypatch.delenv(key,raising=False)
    store=Store(tmp_path/'state.db');cp=ControlPlaneStore(tmp_path/'state.db')
    account=cp.create_provider_account(tenant_id="local",owner_id="owner",provider="codex",label="Synthetic owner account",auth_method="subscription",platform_support="supported",secret_locator="native-home://synthetic",status="ready")
    service=WorkersProjectsService(store,StubRuntime(),control_plane_store=cp,reconcile_on_startup=False)
    service.start_assigned_run=lambda _:None
    coordinator=CoordinatorService(store,service,Provider())
    config=CoordinatorConfig(model="exact-configured-model",effort="high",routes=[Route(id="native",profile="codex-cli",model="codex-cli:gpt-6.1-sol",effort="high",execution_mode="host",connection_id=account["account_id"])])
    cid=coordinator.create("local","owner",config)["conversation_id"]
    coordinator.accept_turn("local","owner",cid,"turn","Exact original job",[Goal(id="job",text="Exact original job")])
    yield coordinator,service,cid,account
    service.shutdown();store.close()


def test_standalone_coordinator_reuses_ordinary_owner_host_admission(standalone_host):
    coordinator,service,cid,account=standalone_host
    result=coordinator.dispatch("local","owner",cid,Dispatch(goal_id="job",route_id="native",instruction="Exact original job"))
    assert result["state"]=="queued",result
    worker=coordinator.store.get_worker(result["worker_id"]);bundle=json.loads(worker["bootstrap_bundle_json"])
    assert worker["model"]=="gpt-6.1-sol" and worker["owner_id"]=="owner"
    assert bundle["provider_account"]=={"policy":"personal_required","account_id":account["account_id"]}
    assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"]=="high"
    assert "launch_authority" not in bundle and "execution_policy" not in bundle
    assert service._host_mutation_scope(worker), "Ordinary host mutation fence must remain"
    assert coordinator.dispatch("local","owner",cid,Dispatch(goal_id="job",route_id="native",instruction="Exact original job"))["run_id"]==result["run_id"]


def test_standalone_native_catalog_uses_unique_configured_model_identity(standalone_host, monkeypatch):
    coordinator,service,cid,_=standalone_host
    monkeypatch.setenv("WPR_MODEL_GROK_BUILD","grok-4.7")
    account=service.control_plane_store.create_provider_account(tenant_id="local",owner_id="owner",provider="grok",label="Synthetic Grok account",auth_method="subscription",platform_support="supported",secret_locator="native-home://synthetic-grok",status="ready")
    with coordinator.store._connect() as conn:
        config=json.loads(conn.execute("SELECT config_json FROM coordinator_conversations WHERE conversation_id=?",(cid,)).fetchone()[0])
        config["routes"][0].update(profile="grok-build",model="grok-build:grok-4.7",connection_id=account["account_id"])
        conn.execute("UPDATE coordinator_conversations SET config_json=? WHERE conversation_id=?",(json.dumps(config),cid))
    result=coordinator.dispatch("local","owner",cid,Dispatch(goal_id="job",route_id="native",instruction="Exact original job"))
    assert result["state"]=="queued",result
    assert coordinator.store.get_worker(result["worker_id"])["model"]=="grok-4.7"


@pytest.mark.parametrize("field,value",[("VIVENTIUM_PARALLEL_WORK_EXECUTION_MODE","host"),("VIVENTIUM_PARALLEL_WORK_EXECUTION_MODE","docker"),("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY","true"),("GLASSHIVE_HOST_WORKERS_ENABLED","false"),("GLASSHIVE_SECURITY_MODE","multi_user"),("XPERFECT_EXECUTION_PROFILE","local-linux")])
def test_standalone_host_does_not_override_declared_policy(standalone_host, monkeypatch,field,value):
    from workers_projects_runtime.service import standalone_coordinator_host_enabled
    monkeypatch.setenv(field,value)
    assert not standalone_coordinator_host_enabled()


@pytest.mark.parametrize("injected",[{"execution_policy":"parallel-clean-room-v1"},{"glasshive_capability_broker":{"name":"synthetic"}},{"env":{"OPENAI_API_KEY":"synthetic"}}])
def test_standalone_coordinator_rejects_caller_authority_in_route(standalone_host,injected):
    from workers_projects_runtime.service import ParallelExecutionIsolationError
    coordinator,_,cid,_=standalone_host
    with coordinator.store._connect() as conn:
        row=conn.execute("SELECT config_json FROM coordinator_conversations WHERE conversation_id=?",(cid,)).fetchone()
        config=json.loads(row[0]);config["routes"][0]["bootstrap_bundle"]=injected
        conn.execute("UPDATE coordinator_conversations SET config_json=? WHERE conversation_id=?",(json.dumps(config),cid))
    with pytest.raises(ParallelExecutionIsolationError):
        coordinator.dispatch("local","owner",cid,Dispatch(goal_id="job",route_id="native",instruction="Exact original job"))
    assert coordinator.store.list_all_workers()==[]


def test_private_host_model_override_requires_owned_durable_coordinator_route(standalone_host):
    from workers_projects_runtime.service import BackgroundWorkerConfigurationError
    coordinator,service,_,_=standalone_host
    with pytest.raises(BackgroundWorkerConfigurationError):
        service.reserve_delegation(tenant_id="local",owner_id="owner",idempotency_key="forged",request_digest="digest",origin_ref="foreign:job",title="Synthetic",goal="Synthetic",instruction="Synthetic",origin_surface="coordinator",worker_name="Helper",worker_role="worker",profile="codex-cli",execution_mode="host",bootstrap_bundle={"provider_model":"gpt-6.1-sol"},trusted_coordinator_model="gpt-6.1-sol",start_run=False,emit_callback=False)
    assert coordinator.store.list_all_workers()==[]


def test_new_chat_uses_the_same_declared_host_route_and_exact_owned_account(standalone_host, monkeypatch):
    from workers_projects_runtime.coordinator_config import configured_coordinator
    from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS
    coordinator, service, _, account = standalone_host
    monkeypatch.delenv("GLASSHIVE_COORDINATOR_CONFIG_JSON", raising=False)
    coordinator.provider._model=lambda model, **_: GLASSHIVE_MODELS[model]
    service.runtime.resolve_model=lambda profile,**_: "gpt-6.1-sol"
    service.runtime.host_cli_installed=lambda profile: profile == "codex-cli"
    service.shared_workspace_deployment_readiness=lambda _: (_ for _ in ()).throw(AssertionError("Host Chat must not require Docker"))
    app=FastAPI()
    install_coordinator_routes(app,coordinator,lambda _: ("local","owner"),lambda tenant,owner,profile="":configured_coordinator(service,coordinator.provider,tenant,owner,profile))
    client=TestClient(app)
    ready=client.get("/v1/coordinator/readiness")
    assert ready.status_code==200,ready.text
    assert ready.json()["connected_account"]=={"available":True,"code":"ready","supported_account_ids":[account["account_id"]],"supported_accounts":[{"account_id":account["account_id"],"profile":"codex-cli","auth_method":"subscription"}]}
    created=client.post("/v1/coordinator/conversations",json={"account_id":account["account_id"]})
    assert created.status_code==200,created.text
    with coordinator.store._connect() as conn:
        saved=json.loads(conn.execute("SELECT config_json FROM coordinator_conversations WHERE conversation_id=?",(created.json()["conversation_id"],)).fetchone()[0])
    assert saved["scope"]["execution_mode"]=="host" and saved["scope"]["connection_id"]==account["account_id"]
    assert saved["model"]=="codex-cli:gpt-6.1-sol" and saved["effort"]=="high"
    assert saved["routes"][0]["connection_id"]==account["account_id"]
    assert client.post("/v1/coordinator/conversations",json={"account_id":account["account_id"],"scope":{"execution_mode":"docker"}}).status_code==409
    assert client.post("/v1/coordinator/conversations",json={"account_id":"foreign"}).status_code==409
    # A connected account is not offered if this host cannot start its native profile.
    service.runtime.host_cli_installed=lambda _:False
    assert client.get("/v1/coordinator/readiness").json()["connected_account"]["supported_account_ids"]==[]


@pytest.mark.parametrize("environment", [
    {"GLASSHIVE_SECURITY_MODE":"multi_user"},
    {"WPR_DEFAULT_EXECUTION_MODE":"docker"},
    {"VIVENTIUM_PARALLEL_WORK_EXECUTION_MODE":"host"},
    {"XPERFECT_EXECUTION_PROFILE":"hosted-xfs"},
    {"GLASSHIVE_HOST_WORKERS_ENABLED":"false"},
    {"VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY":"true"},
])
def test_new_chat_does_not_offer_standalone_host_for_other_deployments(standalone_host, monkeypatch, environment):
    from workers_projects_runtime.coordinator_config import connected_account_execution_mode
    coordinator,service,_,_=standalone_host
    for key,value in environment.items():monkeypatch.setenv(key,value)
    assert connected_account_execution_mode("codex-cli")=="docker"
    calls=[]
    service.shared_workspace_deployment_readiness=lambda data:calls.append(data) or {"available":False,"code":"shared_linux_runtime_required"}
    service.runtime.host_cli_installed=lambda _:True
    app=FastAPI();install_coordinator_routes(app,coordinator,lambda _:("local","owner"),lambda *_:None)
    data=TestClient(app).get("/v1/coordinator/readiness").json()
    assert data["connected_account"]=={"available":False,"code":"shared_linux_runtime_required"}
    assert calls==[{"mode":"shared","execution_mode":"docker"}]


def test_new_chat_readiness_reports_real_direct_profile_and_api_key_without_native_cli(standalone_host, monkeypatch):
    from workers_projects_runtime.coordinator_config import configured_coordinator
    from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS
    from workers_projects_runtime.coordinator import CoordinatorScope
    coordinator,service,_,native_account=standalone_host
    account=service.control_plane_store.create_provider_account(tenant_id="local",owner_id="owner",provider="xai",label="Synthetic API connection",auth_method="api_key",platform_support="supported",secret_locator="native-home://api-key",status="ready")
    monkeypatch.setenv("GLASSHIVE_COORDINATOR_CONFIG_JSON",CoordinatorConfig(model="direct-xai:grok-4.7",effort="high",scope=CoordinatorScope(execution_mode="host")).model_dump_json())
    monkeypatch.setattr("workers_projects_runtime.native_api_keys.enabled",lambda:True)
    coordinator.provider._model=lambda model,**_:GLASSHIVE_MODELS[model]
    service.runtime.host_cli_installed=lambda _:False
    app=FastAPI();install_coordinator_routes(app,coordinator,lambda _:("local","owner"),lambda tenant,owner,profile="":configured_coordinator(service,coordinator.provider,tenant,owner,profile))
    ready=TestClient(app).get("/v1/coordinator/readiness").json()
    assert ready["default"]=={"available":True,"code":"ready"}
    assert ready["connected_account"]["supported_accounts"]==[{"account_id":account["account_id"],"profile":"direct-xai","auth_method":"api_key"}]
    assert native_account["account_id"] not in ready["connected_account"]["supported_account_ids"]
