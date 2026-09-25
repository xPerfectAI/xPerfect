"""A connected account's readiness carries what its provider said when it last stopped a run."""
import json

from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.store import Store

REFUSAL = "The model provider stopped the worker turn and said: “You’ve hit your usage limit.”"


def _setup(tmp_path):
    database = str(tmp_path / "runtime.db")
    store, plane = Store(database), ControlPlaneStore(database)
    accounts = {owner: plane.create_provider_account(
        tenant_id="local", owner_id=owner, provider="codex", label="Personal Codex", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://auto", status="ready") for owner in ("owner", "other")}
    project = store.create_project(owner_id="owner", tenant_id="local", title="Notice", goal="Notice",
                                   default_worker_profile="codex-cli")
    worker = store.create_worker(project_id=project["project_id"], tenant_id="local", owner_id="owner", name="Worker",
                                 role="main", profile="codex-cli", backend="codex-cli", runtime="codex-cli",
                                 model="exact-model")
    return store, plane, accounts, worker


def _run(store, worker, account_id, *, receipt_run=None):
    run = store.create_run(worker["worker_id"], worker["project_id"], "Synthetic task")
    receipt = {"connection_id": account_id, "run_id": receipt_run or run["run_id"]}
    with store._connect() as conn:
        conn.execute("UPDATE runs SET allowed_ai_connection_receipt_json = ? WHERE run_id = ?",
                     (json.dumps(receipt), run["run_id"]))
    return run


def _notice(plane, owner):
    [account] = plane.list_provider_accounts(tenant_id="local", owner_id=owner)
    return account["provider_notice"]


def test_a_provider_refusal_is_shown_on_its_account_until_a_run_completes(tmp_path):
    store, plane, accounts, worker = _setup(tmp_path)
    account_id = accounts["owner"]["account_id"]
    assert _notice(plane, "owner") is None
    refused = _run(store, worker, account_id)
    store.finalize_run_if_state(refused["run_id"], "queued", "failed", error_text="provider refused",
                                failure_class="provider_response_failed", failure_user_message=REFUSAL)
    notice = _notice(plane, "owner")
    assert notice["message"] == REFUSAL and notice["run_id"] == refused["run_id"] and notice["at"]
    # A failure that is not the provider's leaves the provider's last word in place.
    other_failure = _run(store, worker, account_id)
    store.finalize_run_if_state(other_failure["run_id"], "queued", "failed", error_text="terminated",
                                failure_class="runtime_terminated", failure_user_message="Stopped.")
    assert _notice(plane, "owner")["run_id"] == refused["run_id"]
    completed = _run(store, worker, account_id)
    store.finalize_run_if_state(completed["run_id"], "queued", "completed", output_text="Done")
    assert _notice(plane, "owner") is None


def test_only_the_runs_own_receipt_names_an_account_of_the_same_owner(tmp_path):
    store, plane, accounts, worker = _setup(tmp_path)
    for account_id, receipt_run in ((accounts["other"]["account_id"], None),
                                    (accounts["owner"]["account_id"], "run_someone_else")):
        run = _run(store, worker, account_id, receipt_run=receipt_run)
        store.finalize_run_if_state(run["run_id"], "queued", "failed", error_text="provider refused",
                                    failure_class="provider_quota_exhausted", failure_user_message=REFUSAL)
    assert _notice(plane, "other") is None
    assert _notice(plane, "owner") is None


def test_a_store_without_connected_accounts_still_finalizes_runs(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(owner_id="owner", tenant_id="local", title="Plain", goal="Plain",
                                   default_worker_profile="codex-cli")
    worker = store.create_worker(project_id=project["project_id"], tenant_id="local", owner_id="owner", name="Worker",
                                 role="main", profile="codex-cli", backend="codex-cli", runtime="codex-cli",
                                 model="exact-model")
    run = _run(store, worker, "acct_absent")
    finalized = store.finalize_run_if_state(run["run_id"], "queued", "failed", error_text="provider refused",
                                            failure_class="provider_response_failed", failure_user_message=REFUSAL)
    assert finalized["state"] == "failed"


def test_a_native_attempt_that_the_provider_refused_records_the_notice(tmp_path):
    # Real native runs finish through the exact-generation attempt path.
    from test_queue_truth_and_trace import _admit_test_run, _terminal_generation

    store, plane, accounts, worker = _setup(tmp_path)
    account_id = accounts["owner"]["account_id"]
    run = _run(store, worker, account_id)
    claimed = store.claim_next_queued_run(worker["worker_id"], executor_id="executor-notice")
    assert claimed["run_id"] == run["run_id"]
    admitted, lease = _admit_test_run(store, worker, claimed, executor_id="executor-notice")
    running = store.mark_run_runtime_invoked(admitted["run_id"], lease_id=lease["lease_id"],
                                             executor_id="executor-notice")
    assert running["active_attempt_id"]
    failed = store.finalize_run_if_state(run["run_id"], "running", "failed", error_text="provider refused",
                                         failure_class="provider_response_failed", failure_user_message=REFUSAL,
                                         **_terminal_generation(running, lease))
    assert failed["state"] == "failed"
    assert _notice(plane, "owner")["run_id"] == run["run_id"]
