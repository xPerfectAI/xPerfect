import json

import pytest

from workers_projects_runtime.failure_classification import classify_runtime_error
from workers_projects_runtime.native_route import (
    NativeRouteContractError, NativeTurnIncompleteError, codex_route_contract,
    require_completed_codex_turn,
)
from workers_projects_runtime.profile_runtime import CodexCliRuntime, HostCodexCliRuntime


CONTRACT = {
    "version": 1, "execution_mode": "docker", "model": "synthetic-model", "effort": "high",
    "provider_id": "synthetic_provider", "base_url": "https://provider.example.invalid/v1",
    "env_key": "OPENAI_API_KEY", "wire_api": "responses", "session_mode": "stateless",
    "max_capacity_retries": 0, "require_completed_turn": True,
}


@pytest.fixture(params=["host", "docker"])
def launch(tmp_path, monkeypatch, request):
    settings = {
        "WPR_CODEX_CLI_ROUTE_CONTRACT": json.dumps({**CONTRACT, "execution_mode": request.param}),
        "WPR_MODEL_CODEX_CLI": CONTRACT["model"], "WPR_CODEX_CLI_REASONING_EFFORT": "high",
        "WPR_CODEX_CLI_MODEL_PROVIDER": CONTRACT["provider_id"],
        "WPR_CODEX_CLI_BASE_URL": CONTRACT["base_url"], "WPR_CODEX_CLI_ENV_KEY": "OPENAI_API_KEY",
        "WPR_CODEX_CLI_WIRE_API": "responses", "GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS": "0",
        "OPENAI_API_KEY": "synthetic-selected-key", "PORTKEY_API_KEY": "synthetic-unrelated-key",
        "GLASSHIVE_PROJECT_PROVIDER_ENV": "false",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    runtime = (HostCodexCliRuntime if request.param == "host" else CodexCliRuntime)(str(tmp_path / "runtime"))
    worker = {"worker_id": "wrk_synthetic_contract", "profile": "codex-cli",
              "execution_mode": request.param, "model": CONTRACT["model"],
              "workspace_root": str(tmp_path / "workspaces"),
              "bootstrap_bundle_json": json.dumps({"env": {"GLASSHIVE_PROVIDER_SESSION_MODE": "stateless"}})}
    runtime._ensure_dirs(worker["worker_id"])
    def build(candidate=None):
        selected = candidate or worker
        info = runtime._host_runtime_info(selected) if request.param == "host" else runtime._runtime_info(selected)
        command, environment = runtime._build_command(selected, "Synthetic task.", info)
        runtime._check_native_route_contract(selected, command, environment)
        return command, environment
    return runtime, worker, build


def test_existing_builder_keeps_only_the_selected_provider_authority(launch):
    _, _, build = launch
    command, environment = build()
    assert command[1:3] == ["exec", "--json"]
    assert environment["OPENAI_API_KEY"] == "synthetic-selected-key"
    assert "PORTKEY_API_KEY" not in environment


def test_enterprise_route_uses_explicit_projection_policy_without_re_sourcing_keys(launch, monkeypatch):
    from workers_projects_runtime.bootstrap import bootstrap_env_for
    _, worker, build = launch
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _, environment = build()
    assert environment["OPENAI_API_KEY"] == "synthetic-selected-key"
    assert "OPENAI_API_KEY" not in bootstrap_env_for(worker, "codex-cli")
    assert "PORTKEY_API_KEY" not in bootstrap_env_for(worker, "codex-cli")
    monkeypatch.delenv("GLASSHIVE_PROJECT_PROVIDER_ENV")
    with pytest.raises(NativeRouteContractError, match="GLASSHIVE_PROJECT_PROVIDER_ENV=false"):
        codex_route_contract()


def test_factory_rejects_unsafe_projection_before_creating_state(tmp_path, monkeypatch):
    from workers_projects_runtime import api
    monkeypatch.setattr(api, "load_viventium_runtime_env", lambda: None)
    monkeypatch.setenv("WPR_CODEX_CLI_ROUTE_CONTRACT", json.dumps(CONTRACT))
    monkeypatch.setenv("GLASSHIVE_PROJECT_PROVIDER_ENV", "true")
    database = tmp_path / "must-stay-absent" / "runtime.sqlite3"
    with pytest.raises(NativeRouteContractError, match="GLASSHIVE_PROJECT_PROVIDER_ENV=false"):
        api.create_app(db_path=str(database))
    assert not database.parent.exists()


@pytest.mark.parametrize("flag", ["-p", "--profile=alternate", "--oss", "--local-provider=local"])
def test_service_route_refuses_profile_or_local_provider_overrides(launch, flag):
    from workers_projects_runtime.native_route import codex_command_route
    _, _, build = launch
    command, _ = build()
    with pytest.raises(NativeRouteContractError, match="profile or local-provider"):
        codex_command_route(command[:2] + [flag] + command[2:])


@pytest.mark.parametrize("field,value", [
    ("model", "different-model"), ("effort", "low"), ("base_url", "https://other.example.invalid"),
    ("provider_id", "different_provider"), ("env_key", "PORTKEY_API_KEY"),
    ("wire_api", "different_api"), ("execution_mode", "host"), ("max_capacity_retries", 1),
])
def test_every_declared_route_field_must_match_generated_launch(launch, monkeypatch, field, value):
    current = codex_route_contract()
    if field == "execution_mode":
        value = "docker" if current[field] == "host" else "host"
    monkeypatch.setenv("WPR_CODEX_CLI_ROUTE_CONTRACT", json.dumps({**current, field: value}))
    with pytest.raises(NativeRouteContractError):
        launch[2]()


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "OPENAI_BASE_URL", "PORTKEY_API_KEY", "http_proxy", "NODE_EXTRA_CA_CERTS"])
def test_bootstrap_cannot_change_authority_after_exec_env_checks(launch, key):
    _, worker, build = launch
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["env"][key] = "synthetic-conflicting-authority"
    with pytest.raises(NativeRouteContractError):
        build({**worker, "bootstrap_bundle_json": json.dumps(bundle)})


def test_stateless_is_required_and_native_provider_settings_are_service_owned(launch):
    _, worker, build = launch
    with pytest.raises(NativeRouteContractError):
        build({**worker, "bootstrap_bundle_json": "{}"})
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["codex_config_append"] = 'model_provider="other"'
    with pytest.raises(NativeRouteContractError):
        build({**worker, "bootstrap_bundle_json": json.dumps(bundle)})


def test_native_stream_timeout_does_not_redefine_the_provider_authority(launch):
    _, worker, build = launch
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["codex_config_append"] = '[model_providers.synthetic_provider]\nstream_idle_timeout_ms=900000\n'
    build({**worker, "bootstrap_bundle_json": json.dumps(bundle)})
    bundle["codex_config_append"] += 'base_url="https://redirect.example.invalid"\n'
    with pytest.raises(NativeRouteContractError):
        build({**worker, "bootstrap_bundle_json": json.dumps(bundle)})


@pytest.mark.parametrize("raw", ["broken", "[]", "{}", '{"version":1,"version":2}',
                                 json.dumps({**CONTRACT, "require_completed_turn": "true"})])
def test_invalid_contract_schema_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("WPR_CODEX_CLI_ROUTE_CONTRACT", raw)
    with pytest.raises(NativeRouteContractError):
        codex_route_contract()


AGENT = {"type": "item.completed", "item": {"type": "agent_message", "text": "Synthetic result"}}
COMPLETE = {"type": "turn.completed"}


@pytest.mark.parametrize("events", [[], [AGENT], [COMPLETE], [AGENT, {"type": "turn.failed"}],
                                    [AGENT, COMPLETE, {"type": "turn.started"}], [AGENT, COMPLETE, COMPLETE]])
def test_incomplete_or_ambiguous_native_turns_cannot_succeed(events):
    with pytest.raises(NativeTurnIncompleteError):
        require_completed_codex_turn("\n".join(json.dumps(event) for event in events))


def test_actual_codex_parser_applies_opt_in_completion_contract(launch):
    runtime, worker, _ = launch
    info = runtime._runtime_info(worker)
    valid = "\n".join(json.dumps(event) for event in [AGENT, COMPLETE])
    assert "Synthetic result" in runtime._parse_output(worker, valid, "", info)[1]
    with pytest.raises(NativeTurnIncompleteError):
        runtime._parse_output(worker, json.dumps(AGENT), "", info)


def test_unconfigured_route_retains_existing_parser_behavior(launch, monkeypatch):
    runtime, worker, build = launch
    monkeypatch.delenv("WPR_CODEX_CLI_ROUTE_CONTRACT")
    build()
    assert "Synthetic result" in runtime._parse_output(worker, json.dumps(AGENT), "", runtime._runtime_info(worker))[1]


@pytest.mark.parametrize("error", [NativeRouteContractError("Synthetic mismatch"), NativeTurnIncompleteError("Synthetic incomplete turn")])
def test_typed_failures_retain_durable_classification(error):
    classified = classify_runtime_error(error, runtime_name="codex-cli")
    assert classified.failure_class == error.failure_class and classified.structured
    assert classified.retryable is False


@pytest.mark.parametrize("flag", ["-pother", "--profile=other"])
def test_attached_profile_selection_cannot_override_service_route(flag):
    from workers_projects_runtime.native_route import codex_command_route
    with pytest.raises(NativeRouteContractError, match="profile"):
        codex_command_route(["codex", "exec", flag, "-"])
