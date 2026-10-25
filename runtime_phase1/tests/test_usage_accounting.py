from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.failure_classification import FailureClassification
from workers_projects_runtime.profile_runtime import ClaudeCodeRuntime, CodexCliRuntime
from workers_projects_runtime.store import Store, utc_now
from workers_projects_runtime.usage_accounting import normalize_observation


def fixture_run(tmp_path, suffix="one", store=None):
    store = store or Store(str(tmp_path / "runtime.sqlite"))
    project = store.create_project("owner", "Synthetic project", "Synthetic goal", "codex-cli")
    worker = store.create_worker(project["project_id"], "owner", "Worker", "worker", "codex-cli", "codex", "codex-cli", "configured", execution_mode="host")
    store.update_worker_state(worker["worker_id"], "ready")
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic work")
    return store, project, worker, run


def invoked_attempt(store, worker, run):
    claimed = store.claim_next_queued_run(worker["worker_id"])
    lease = store.acquire_host_run_lease(
        runtime_family="codex", lane="mission", tenant_id="local", owner_id="owner",
        worker_id=worker["worker_id"], run_id=run["run_id"], executor_id="synthetic-executor",
        conversation_limit=2, mission_limit=64, account_mission_limit=64, tenant_mission_limit=64, lease_ttl_s=300)
    store.admit_claimed_run(run["run_id"], lease_id=lease["lease_id"], executor_id="synthetic-executor")
    invoked = store.mark_run_runtime_invoked(run["run_id"], lease_id=lease["lease_id"], executor_id="synthetic-executor")
    assert invoked["active_attempt_id"] == claimed["active_attempt_id"]
    return invoked, lease


def reconcile(store, run, usage=None, **kwargs):
    return store.reconcile_attempt_usage(run["run_id"], run["active_attempt_id"], usage,
                                         invocation_at=run["runtime_invoked_at"], **kwargs)


@pytest.mark.parametrize("value,status", [(None,"not_reported"),(0,"reported"),(True,"invalid"),(-1,"invalid"),(1.2,"invalid"),("3","invalid")])
def test_strict_observation_preserves_missing_invalid_and_zero(value, status):
    assert normalize_observation({"input_tokens": value})["tokens"]["input_tokens"]["status"] == status


def test_receipts_are_idempotent_late_and_do_not_erase_known_values(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    run, lease = invoked_attempt(store, worker, run)
    first = reconcile(store, run, {"input_tokens": 0, "output_tokens": 4}, total_cost_usd="0.000001")
    assert first["tokens"]["input_tokens"] == {"value": 0, "status": "reported"}
    assert first["coverage"] == "partial"
    assert reconcile(store, run)["money"] == first["money"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: reconcile(store, run, {"input_tokens": 0, "output_tokens": 4}), range(8)))
    assert len(store.get_run(run["run_id"])["usage"]["receipt_ids"]) == 1
    store.finalize_run(run["run_id"], "failed")
    after = reconcile(store, run, {"output_tokens": 6}, revision=1, total_cost_usd="0.000003")
    assert after["money"][0]["amount"] == "0.000003"
    assert store.get_run(run["run_id"])["state"] == "failed"
    assert store.get_run(run["run_id"])["output_tokens"] == 6
    with pytest.raises(ValueError, match="revision conflict"):
        reconcile(store, run, {"output_tokens": 7}, revision=1)
    assert reconcile(store, run, {"output_tokens": 1}, revision=0)["tokens"]["output_tokens"]["value"] == 6
    store.close()
    reloaded = Store(store.db_path).get_run(run["run_id"])
    assert reloaded["usage"]["tokens"]["input_tokens"]["value"] == 0
    assert reloaded["usage"]["money"][0]["amount"] == "0.000003"


def test_deltas_have_unique_identity_and_cannot_mix_with_cumulative_total(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    run, _ = invoked_attempt(store, worker, run)
    for key, value in [("request-1",2),("request-2",3),("request-1",2)]:
        result = reconcile(store, run, {"output_tokens": value}, receipt_key=key, measurement_kind="delta")
    assert result["tokens"]["output_tokens"]["value"] == 5
    with pytest.raises(ValueError, match="Overlapping"):
        reconcile(store, run, {"output_tokens": 5})
    with pytest.raises(ValueError, match="Estimated"):
        reconcile(store, run, {"source":"estimated"}, receipt_key="request-3", measurement_kind="delta")


def test_receipt_requires_exact_invoked_attempt_and_preserves_legacy_unknown(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    assert store.get_run(run["run_id"])["usage"]["coverage"] == "unknown"
    claimed = store.claim_next_queued_run(worker["worker_id"])
    with pytest.raises(ValueError, match="invoked attempt"):
        store.reconcile_attempt_usage(run["run_id"], claimed["active_attempt_id"], {}, invocation_at=utc_now())


def test_terminal_missing_data_preserves_prior_invalid_status(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    run, _ = invoked_attempt(store, worker, run)
    reconcile(store, run, {"input_tokens":"invalid"})
    store.finalize_run(run["run_id"], "failed")
    assert store.get_run(run["run_id"])["usage"]["tokens"]["input_tokens"]["status"] == "invalid"


def test_retry_files_never_inherit_previous_or_legacy_usage(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    stdout = json.dumps({"type":"turn.completed", "usage":{"input_tokens":5,"output_tokens":2}})
    runtime._record_run_metrics("worker", "run", stdout)
    runtime._record_run_metrics("worker", "run", stdout, "attempt-1")
    assert runtime.run_usage({"worker_id":"worker", "_run_attempt_id":"attempt-1"}, "run")["input_tokens"] == 5
    assert runtime.run_usage({"worker_id":"worker", "_run_attempt_id":"attempt-2"}, "run") == {}
    assert runtime.run_telemetry({"worker_id":"worker", "_run_attempt_id":"attempt-2"}, "run") == {}
    assert ClaudeCodeRuntime(base_dir=str(tmp_path / "claude"))._usage_from_output("") == {}


def test_normalized_attempt_sidecar_keeps_invalid_status_without_source_text(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path))
    runtime._record_run_metrics("worker", "run", json.dumps({"type":"result","usage":{"input_tokens":"untrusted-value","output_tokens":0}}), "attempt")
    observation = runtime.run_usage_observation({"worker_id":"worker","_run_attempt_id":"attempt"}, "run")
    normalized = normalize_observation(observation)
    assert normalized["tokens"]["input_tokens"]["status"] == "invalid"
    assert normalized["tokens"]["output_tokens"]["value"] == 0
    sidecar = runtime._attempt_run_root("worker","run","attempt") / "usage-observation.json"
    assert "untrusted-value" not in sidecar.read_text()


def test_native_sidecar_declares_revisions_for_late_cumulative_usage_and_money(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    run, _ = invoked_attempt(store, worker, run)
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "native"))
    exact_worker = {**worker,"_run_attempt_id":run["active_attempt_id"]}
    def observe(tokens, cost):
        runtime._record_run_metrics(worker["worker_id"], run["run_id"], json.dumps({"type":"result","usage":{"output_tokens":tokens},"total_cost_usd":cost}), run["active_attempt_id"])
        raw = runtime.run_usage_observation(exact_worker,run["run_id"])
        result = reconcile(store,run,raw,revision=raw["_receipt_revision"],total_cost_usd=raw["_reported_cost_usd"])
        return raw,result
    old,_ = observe(2,"0.1")
    new,result = observe(3,"0.2")
    assert new["_receipt_revision"] == old["_receipt_revision"]+1
    assert result["tokens"]["output_tokens"]["value"] == 3
    assert result["money"][0]["amount"] == "0.2"
    runtime._record_run_metrics(worker["worker_id"],run["run_id"],"",run["active_attempt_id"])
    absent = runtime.run_usage_observation(exact_worker,run["run_id"])
    assert absent["_receipt_revision"] == new["_receipt_revision"]
    assert absent["output_tokens"] == 3


def test_native_money_does_not_round_json_decimal_through_binary_float(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path))
    observation = runtime._usage_observation_from_output('{"type":"result","usage":{"output_tokens":0},"total_cost_usd":0.123456789012345678901}')
    assert observation["money"][0]["amount"] == "0.123456789012345678901"


def test_grok_latest_response_and_direct_positive_totals_reach_attempt_ledger(tmp_path):
    from workers_projects_runtime.grok_runtime import GrokBuildRuntime
    from workers_projects_runtime.direct_runtime import DirectModelRuntime
    store, _, worker, run = fixture_run(tmp_path)
    run, _ = invoked_attempt(store, worker, run)
    for runtime, event, expected_input in (
        (GrokBuildRuntime(base_dir=str(tmp_path / "grok")), {"type":"grok.result","usage":{"prompt_tokens":12,"completion_tokens":0,"total_tokens":12}},12),
        (DirectModelRuntime("direct-openai",base_dir=str(tmp_path / "direct")), {"type":"grok.result","executor":"direct","usage":{"input_tokens":20,"output_tokens":3,"cached_input_tokens":7}},20),
    ):
        observation = runtime._usage_observation_from_output(json.dumps(event))
        assert observation["native_totals"]["input_tokens"]["value"] == expected_input
        assert observation["measurement_coverage"] == "partial"
        assert observation["tokens"]["input_tokens"]["status"] == "not_reported"
    runtime._record_run_metrics(worker["worker_id"],run["run_id"],json.dumps(event),run["active_attempt_id"])
    raw = runtime.run_usage_observation({**worker,"_run_attempt_id":run["active_attempt_id"]},run["run_id"])
    result = reconcile(store,run,raw,revision=raw["_receipt_revision"])
    assert result["native_totals"]["input_tokens"]["value"] == 20 and result["coverage"] == "partial"


def test_direct_zero_without_upstream_presence_is_unknown_but_grok_native_zero_is_retained(tmp_path):
    from workers_projects_runtime.direct_runtime import DirectModelRuntime
    from workers_projects_runtime.grok_runtime import GrokBuildRuntime
    from workers_projects_runtime.usage_accounting import summarize
    runtime = DirectModelRuntime("direct-openai",base_dir=str(tmp_path))
    observation = runtime._usage_observation_from_output(json.dumps({"type":"grok.result","executor":"direct","usage":{"input_tokens":0,"output_tokens":0,"cached_input_tokens":0}}))
    assert summarize([{"receipt_id":"r","usage":observation}],attempts=1,unknown_attempts=0)["coverage"] == "unknown"
    native = GrokBuildRuntime(base_dir=str(tmp_path / "grok"))._usage_observation_from_output(json.dumps({"type":"grok.result","usage":{"prompt_tokens":0,"completion_tokens":0}}))
    assert native["native_totals"]["input_tokens"]["value"] == 0 and native["tokens"]["output_tokens"]["value"] == 0


def test_account_projection_once_and_unknown_coverage_survive_reload(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite"))
    control = ControlPlaneStore(store.db_path)
    account = control.create_provider_account(tenant_id="local", owner_id="owner", provider="codex", label="Synthetic", auth_method="subscription", platform_support="supported", secret_locator="native-home://synthetic", status="ready")
    args = dict(account_id=account["account_id"], tenant_id="local", owner_id="owner", succeeded=False, duration_seconds=2, receipt_id="attempt-1")
    control.record_provider_account_usage(**args)
    control.record_provider_account_usage(**args)
    result = control.record_provider_account_usage(**args, input_tokens=0, output_tokens=4)
    assert result["observed_runs"] == 1 and result["observed_failures"] == 1
    assert result["observed_unknown_runs"] == 0 and result["observed_usage_coverage"] == "complete"
    result = control.record_provider_account_usage(**{**args,"receipt_id":"attempt-2"})
    assert result["observed_unknown_runs"] == 1 and result["observed_usage_coverage"] == "partial"
    assert result["observed_output_tokens"] == 4


def test_late_run_receipt_updates_account_in_same_transaction_once(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    control = ControlPlaneStore(store.db_path)
    account = control.create_provider_account(tenant_id="local", owner_id="owner", provider="codex", label="Synthetic", auth_method="subscription", platform_support="supported", secret_locator="native-home://synthetic", status="ready")
    run, _ = invoked_attempt(store, worker, run)
    with store._connect() as conn:
        conn.execute("UPDATE runs SET allowed_ai_connection_receipt_json=? WHERE run_id=?",
                     (json.dumps({"run_id":run["run_id"],"connection_id":account["account_id"]}), run["run_id"]))
    reconcile(store, run, {"input_tokens":0,"output_tokens":4})
    reconcile(store, run, {"input_tokens":0,"output_tokens":6}, revision=1)
    reconcile(store, run)
    result = control.get_provider_account(account_id=account["account_id"],tenant_id="local",owner_id="owner")
    assert result["observed_runs"] == 1 and result["observed_output_tokens"] == 6
    assert result["observed_unknown_runs"] == 0


@pytest.mark.parametrize("source", ["http_response", "harness_reported", "adapter_synthesized", "unavailable"])
def test_failure_provenance_is_typed_and_identifier_bounded(source):
    fields = FailureClassification("provider_access_denied",False,"Access refused","Check access","class=provider_access_denied",
        upstream_request_id="request-123", failure_status_source=source, failure_status_code=403).as_store_fields()
    assert fields["upstream_request_id"] == "request-123" and fields["failure_status_source"] == source
    assert FailureClassification("failed",False,"Failed","Retry","failed", upstream_request_id="https://secret.invalid/token").as_store_fields()["upstream_request_id"] == ""
