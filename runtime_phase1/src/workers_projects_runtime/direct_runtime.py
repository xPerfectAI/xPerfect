"""Direct API execution on xPerfect's existing host run lifecycle.

The official single-agent SDK owns provider/tool continuation. xPerfect retains
admission, account authority, worker orchestration, Stop, history and output.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import copy
import base64
import tomllib
from urllib.parse import urlsplit

from .bootstrap import bootstrap_bundle_for, bootstrap_env_for, glasshive_project_agents_md, native_instruction_before_tail, worker_prompt_layer_producer
from .grok_runtime import GrokBuildRuntime
from .grok_projection import mcp_servers_for_bundle
from .profile_runtime import HostNativeCliMixin, _native_tools_disabled
from .mission_provider_accounts import apply_bound_provider_account_environment, mission_provider_account_selection
from .openclaw_runtime import RuntimeConfigurationError, RuntimeDependencyMissingError, RuntimeErrorBase

# Runtime IDs are typed configuration, not semantic routing based on prompt text.
DIRECT_PROVIDERS = {
    "direct-openai": {"provider": "openai", "key": "OPENAI_API_KEY", "model": "gpt-6.1-sol", "model_env": "WPR_MODEL_DIRECT_OPENAI", "effort_env": "WPR_CODEX_CLI_REASONING_EFFORT"},
    "direct-anthropic": {"provider": "anthropic", "key": "ANTHROPIC_API_KEY", "model": "claude-opus-5-5", "model_env": "WPR_MODEL_DIRECT_ANTHROPIC", "effort_env": "WPR_CLAUDE_CODE_EFFORT"},
    "direct-xai": {"provider": "xai", "key": "XAI_API_KEY", "model": "grok-4.7", "model_env": "WPR_MODEL_DIRECT_XAI", "effort_env": "WPR_GROK_REASONING_EFFORT"},
}


def direct_openai_base_url():
    """Use an explicit deployment endpoint; conversational input cannot set it."""
    value = os.environ.get("WPR_OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeConfigurationError("Direct OpenAI endpoint is invalid") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or port == 0):
        raise RuntimeConfigurationError("Direct OpenAI requires an HTTPS endpoint without embedded credentials or query parameters")
    return value


def direct_mcp_servers(bundle, environment):
    """Translate existing portable MCP declarations, retaining their tool filters."""
    projected = copy.deepcopy(bundle)
    portable = projected.get("claude_project_mcp") or {}
    portable = portable.get("mcpServers", portable)
    if not isinstance(portable, dict):
        raise RuntimeConfigurationError("Invalid portable MCP configuration")
    portable = dict(portable)
    codex = tomllib.loads(str(projected.get("codex_config_append") or "")).get("mcp_servers", {})
    for name, server in codex.items():
        if name in portable:
            continue
        if not isinstance(server, dict):
            raise RuntimeConfigurationError("Invalid MCP connection")
        if server.get("enabled") is False:
            continue
        item = {key: server[key] for key in ("command", "args", "env", "url") if key in server}
        if server.get("url"):
            item["type"] = "http"
            headers = dict(server.get("http_headers") or {})
            for key, env_name in (server.get("env_http_headers") or {}).items():
                if env_name not in environment:
                    raise RuntimeConfigurationError("MCP header refers to an unavailable environment variable")
                headers[key] = environment[env_name]
            bearer = server.get("bearer_token_env_var")
            if bearer:
                if bearer not in environment:
                    raise RuntimeConfigurationError("MCP bearer refers to an unavailable environment variable")
                headers["Authorization"] = "Bearer " + environment[bearer]
            item["headers"] = headers
        else:
            item["env"] = dict(item.get("env") or {})
            for env_name in server.get("env_vars") or []:
                if env_name not in environment:
                    raise RuntimeConfigurationError("MCP child refers to an unavailable environment variable")
                item["env"][env_name] = environment[env_name]
            if server.get("cwd"):
                item["cwd"] = server["cwd"]
        portable[name] = item
    projected["claude_project_mcp"] = {"mcpServers": portable}
    servers = mcp_servers_for_bundle(projected, environment)
    # The signed broker already exposes canonical authorized names. Adding an
    # SDK prefix would make its callable catalog disagree with that authority.
    broker = bundle.get("glasshive_capability_broker")
    # Use the existing declarations; runtime never guesses a tool's permission.
    for item in servers:
        if (isinstance(broker, dict) and broker.get("version") == 1
                and item.get("name") == broker.get("name")
                and item.get("url") == broker.get("url")):
            item["preserve_tool_names"] = True
        source = codex.get(item["name"], {})
        for key in ("enabled_tools", "disabled_tools"):
            if key in source:
                if not isinstance(source[key], list) or any(not isinstance(x, str) for x in source[key]):
                    raise RuntimeConfigurationError("Invalid MCP tool filter")
                item[key] = source[key]
        if isinstance(portable.get(item["name"]), dict) and portable[item["name"]].get("cwd"):
            item["cwd"] = portable[item["name"]]["cwd"]
    return servers


@worker_prompt_layer_producer("developer_instructions")
def direct_authority_instructions(worker, output_schema):
    bundle = bootstrap_bundle_for(worker)
    if bundle.get("run_mode") == "conversation":
        authority = str(bundle.get("developer_instructions") or glasshive_project_agents_md(bundle) or "")
    else:
        from .native_context_projection import native_context_authority
        authority = native_context_authority(bundle)
    if output_schema:
        from .agent_builder_control import conversation_output_contract_instructions
        try:
            return native_instruction_before_tail(bundle, authority,
                conversation_output_contract_instructions(output_schema), require_exact_tail=True)
        except ValueError as exc:
            raise RuntimeErrorBase(str(exc)) from exc
    return authority


from .direct_warm_runtime import DirectNativeWarmMixin


class DirectModelRuntime(DirectNativeWarmMixin, HostNativeCliMixin, GrokBuildRuntime):
    """Reuse exact host process lifecycle; do not invent an in-process job manager."""
    activity_protocol = "grok-build"
    preflight_uses_cli_subprocess = False

    def __init__(self, profile, *args, **kwargs):
        self.runtime_name = profile
        self.worker_root_name = profile.replace("-", "_") + "_runtime"
        super().__init__(*args, **kwargs)
        self.binary = sys.executable
        self.direct = DIRECT_PROVIDERS[profile]
        self._init_native_warm()

    def _agent_type(self):
        return "direct"

    @staticmethod
    def _has_bound_provider_account(worker):
        from .current_native_account import MARKER
        return (mission_provider_account_selection(worker) is not None or MARKER in worker
                or bool(worker.get("_glasshive_provider_account_bound"))
                or bool(worker.get("_glasshive_provider_api_key_file"))
                or bool(worker.get("_glasshive_inference_broker_bound")))

    def _openai_endpoint(self, worker):
        endpoint = direct_openai_base_url()
        if endpoint != "https://api.openai.com/v1" and self._has_bound_provider_account(worker):
            raise RuntimeConfigurationError("A deployment Direct OpenAI endpoint cannot use a selected personal provider account")
        return endpoint

    def _host_env(self, worker, run_id=None):
        env = super()._host_env(worker, run_id=run_id)
        if self.direct["provider"] == "openai":
            endpoint = self._openai_endpoint(worker)
            if not self._has_bound_provider_account(worker):
                key = os.environ.get("WPR_OPENAI_API_KEY", "").strip()
                if key and endpoint == "https://api.openai.com/v1":
                    raise RuntimeConfigurationError("A dedicated Direct deployment key requires an explicit custom WPR_OPENAI_BASE_URL")
                if endpoint != "https://api.openai.com/v1" and not key:
                    raise RuntimeConfigurationError("A custom Direct OpenAI endpoint requires its dedicated WPR_OPENAI_API_KEY")
                if key:
                    env["OPENAI_API_KEY"] = key
        return env

    def resolve_model(self, profile):
        if profile != self.runtime_name:
            raise RuntimeConfigurationError("Direct runtime/profile mismatch")
        return os.environ.get(self.direct["model_env"], self.direct["model"])

    def preflight_worker_profile(self, profile, execution_mode="host"):
        if profile != self.runtime_name or execution_mode != "host":
            raise RuntimeConfigurationError("Direct API execution requires its registered host profile")
        if importlib.util.find_spec("pydantic_ai") is None:
            raise RuntimeDependencyMissingError("Install the xPerfect Direct SDK dependency", binary=sys.executable,
                runtime_name=self.runtime_name, profile=profile, execution_mode="host")

    def _default_session_key(self, worker):
        return self._read_session_key(worker["worker_id"]) or worker.get("session_key") or "direct:" + worker["worker_id"]

    def _conversation_output_schema(self, worker):
        from .agent_builder_control import private_machine_state_output_schema
        bundle = bootstrap_bundle_for(worker)
        capabilities = bundle.get("provider_capabilities") or {}
        contract = capabilities.get("response_contract") if isinstance(capabilities, dict) else None
        schema = private_machine_state_output_schema(contract)
        if schema is not None:
            if (bundle.get("run_mode") != "conversation" or not _native_tools_disabled(bundle)
                    or bundle.get("access_mode") != "read_only"
                    or not self._provider_session_is_stateless(worker)):
                raise RuntimeConfigurationError("Private JSON output requires its isolated zero-tool authority")
            return schema
        return super()._conversation_output_schema(worker)

    def _select_native_agent_output(self, worker, info, output_parts):
        from .agent_builder_control import private_machine_state_output_schema
        capabilities = bootstrap_bundle_for(worker).get("provider_capabilities") or {}
        contract = capabilities.get("response_contract") if isinstance(capabilities, dict) else None
        if private_machine_state_output_schema(contract) is not None:
            if len(output_parts) != 1:
                raise RuntimeErrorBase("Private JSON output requires one exact native terminal")
            return output_parts[0]
        return super()._select_native_agent_output(worker, info, output_parts)

    def _request(self, worker, instruction, info):
        bundle = bootstrap_bundle_for(worker)
        env = self._host_env(worker)
        apply_bound_provider_account_environment(worker, env, runtime_name=self.runtime_name)
        servers = direct_mcp_servers(bundle, env)
        if bundle.get("run_mode") == "conversation" and bundle.get("conversation_tool_mode") == "coordination":
            servers = [server for server in servers if server.get("preserve_tool_names") is True]
        if _native_tools_disabled(bundle) and not bundle.get("glasshive_capability_broker"):
            servers = []
        authority = direct_authority_instructions(worker, self._conversation_output_schema(worker))
        settings = dict(bundle.get("direct_model_settings", {})) if isinstance(bundle.get("direct_model_settings", {}), dict) else bundle.get("direct_model_settings")
        if isinstance(settings, dict) and bundle.get("service_tier") is not None:
            settings.setdefault("service_tier", bundle["service_tier"])
        permitted = {"max_tokens", "parallel_tool_calls", "service_tier"}
        if self.direct["provider"] == "anthropic":
            permitted |= {"anthropic_thinking", "stop_sequences"}
        if not isinstance(settings, dict) or set(settings) - permitted:
            raise RuntimeConfigurationError("Unsupported Direct model settings")
        # Only settings the configured protocol preserves may cross this boundary.
        if self.direct["provider"] == "anthropic" and settings.get("service_tier", "default") not in {"default", "auto"}:
            raise RuntimeConfigurationError("This Direct Anthropic route supports default or auto service tier")
        if self.direct["provider"] != "anthropic" and "anthropic_thinking" in settings:
            raise RuntimeConfigurationError("Anthropic thinking settings apply only to its Direct provider")
        scope = self._history_scope(worker, info.model)
        scope_json = json.dumps(scope, sort_keys=True)
        scope_digest = hashlib.sha256(scope_json.encode()).hexdigest()
        from .agent_builder_control import private_machine_state_output_schema
        response_contract = (bundle.get("provider_capabilities") or {}).get("response_contract")
        cache_eligible = (self.direct["provider"] in {"openai", "xai"}
            and not self._provider_session_is_stateless(worker)
            and private_machine_state_output_schema(response_contract) is None)
        cache_key = hashlib.sha256(("xperfect-prompt-cache-v1\n" + scope_json).encode()).hexdigest() if cache_eligible else None
        history_name = "direct-history-" + scope_digest + ".json"
        return {"provider": self.direct["provider"], "model": info.model, "instruction": instruction,
                **({"base_url": self._openai_endpoint(worker)} if self.direct["provider"] == "openai" else {}),
                "run_id": str(worker.get("_active_run_id") or "direct"),
                "conversation_tool_mode": bundle.get("conversation_tool_mode", "full") if bundle.get("run_mode") == "conversation" else "full",
                "instructions": authority, "mcp_servers": servers, "session_id": info.session_key or self._default_session_key(worker),
                "input_images": [{"media_type": mime, "data_base64": base64.b64encode(data).decode()}
                                 for _path, mime, data in self._native_input_image_files(worker, info)],
                "history_enabled": not self._provider_session_is_stateless(worker),
                "core_archive": bundle.get("direct_core_archive") if bundle.get("run_mode") == "conversation" else None,
                "core_state": bundle.get("direct_core_state") if bundle.get("run_mode") == "conversation" else None,
                "prompt_cache_key": cache_key,
                "history_scope": scope, "history_path": str(self._state_dir(worker["worker_id"]) / history_name),
                "context_replay": self._context_replay_request(worker, scope, bundle.get("direct_context_replay")),
                "effort": (self._bootstrap_env_value(worker, self.direct["effort_env"]) or "default"),
                "model_settings": settings, "output_schema": self._conversation_output_schema(worker),
                "graph_control": bundle.get("agent_builder_control"),
                "delivery_control": bundle.get("messaging_delivery_control"),
                "response_contract": (bundle.get("provider_capabilities") or {}).get("response_contract")}

    def _history_scope(self, worker, model):
        selection = mission_provider_account_selection(worker)
        endpoint = self._openai_endpoint(worker) if self.direct["provider"] == "openai" else None
        return {"provider": self.direct["provider"], "model": model,
                "tenant": worker.get("tenant_id", "local"), "owner": worker.get("owner_id"),
                "worker": worker["worker_id"], "account": selection.account_id if selection else "configured-route",
                "epoch": bootstrap_env_for(worker).get("GLASSHIVE_PROVIDER_SESSION_EPOCH", ""),
                **({"endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest()}
                   if endpoint is not None and endpoint != "https://api.openai.com/v1" else {})}

    def _context_replay_request(self, worker, scope, descriptor):
        if not descriptor or self._provider_session_is_stateless(worker):
            return None
        if (not isinstance(descriptor, dict) or not isinstance(descriptor.get("source_epoch"), str)
                or descriptor.get("target_epoch") != scope["epoch"]):
            raise RuntimeConfigurationError("Invalid Direct context replay descriptor")
        digest = descriptor.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RuntimeConfigurationError("Invalid Direct context replay digest")
        source_scope = {**scope, "epoch": descriptor["source_epoch"]}
        if source_scope == scope:
            raise RuntimeConfigurationError("Direct context replay must cross a proven epoch boundary")
        return {"scope": source_scope, "sha256": descriptor["sha256"],
                "path": str(self._state_dir(worker["worker_id"]) / ("direct-context-seed-" + descriptor["sha256"] + ".json"))}

    def provider_context_replay(self, worker):
        from .direct_runner import complete_tool_history
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        if self._provider_session_is_stateless(worker):
            return None
        scope = self._history_scope(worker, str(worker.get("model") or self.direct["model"]))
        digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        path = self._state_dir(worker["worker_id"]) / ("direct-history-" + digest + ".json")
        raw = path.read_bytes()
        record = json.loads(raw)
        if record.get("scope") != scope:
            raise RuntimeConfigurationError("Direct context replay does not match its provider/account scope")
        retained, call_ids = complete_tool_history(ModelMessagesTypeAdapter.validate_json(record["messages"]))
        return {"source_epoch": scope["epoch"], "sha256": hashlib.sha256(raw).hexdigest(),
                "tool_call_ids": call_ids, "messages": len(retained),
                "bytes": len(ModelMessagesTypeAdapter.dump_json(retained)),
                "_scope": scope, "_source_bytes": raw}

    def freeze_provider_context_replay(self, worker, proof):
        from .direct_runner import private_write
        scope = self._history_scope(worker, str(worker.get("model") or self.direct["model"]))
        raw = proof.get("_source_bytes")
        if (proof.get("_scope") != scope or not isinstance(raw, bytes)
                or hashlib.sha256(raw).hexdigest() != proof.get("sha256")):
            raise RuntimeConfigurationError("Direct context replay proof does not match its authorized scope")
        path = self._state_dir(worker["worker_id"]) / ("direct-context-seed-" + proof["sha256"] + ".json")
        if path.exists():
            if path.read_bytes() != raw:
                raise RuntimeConfigurationError("Direct immutable replay seed changed")
        else:
            private_write(path, raw.decode("utf-8"))
        return {"source_epoch": scope["epoch"], "sha256": proof["sha256"]}

    def _build_command(self, worker, instruction, info):
        env = self._host_env(worker)
        apply_bound_provider_account_environment(worker, env, runtime_name=self.runtime_name)
        # A configured deployment API key is explicit local provider authority.
        # Never use it to replace an explicitly selected account's missing key.
        selection = mission_provider_account_selection(worker)
        if selection is not None and not worker.get("_glasshive_provider_api_key_file"):
            raise RuntimeConfigurationError("The selected Direct account must supply an API key; subscription authentication cannot be reused")
        if selection is None and not env.get(self.direct["key"]):
            key = os.environ.get(self.direct["key"], "")
            if key:
                env[self.direct["key"]] = key
        if not env.get(self.direct["key"]):
            raise RuntimeConfigurationError("Direct execution requires an API-key connection; CLI subscription sign-in does not supply API access")
        return [sys.executable, str(Path(__file__).with_name("direct_runner.py"))], env

    def _command_stdin_text(self, worker, instruction, info):
        if not self._conversation_mode_from_worker(worker):
            instruction = self._instruction_with_completion_contract(instruction)
        return json.dumps(self._request(worker, instruction, info), ensure_ascii=False)

    def _native_provider_authority_receipt(self, worker, *, command, run_id, model):
        bundle = bootstrap_bundle_for(worker)
        if bundle.get("run_mode") != "conversation" or not bundle.get("developer_instructions"):
            return None
        if command != [sys.executable, str(Path(__file__).with_name("direct_runner.py"))]:
            raise RuntimeErrorBase("Direct executable changed before authority projection")
        text = direct_authority_instructions(worker, self._conversation_output_schema(worker))
        return {"protocol": "glasshive.native_provider_authority_receipt.v1", "run_id": run_id,
                "runtime": self.runtime_name, "model": model, "authority_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "authority_chars": len(text), "feeling_capsule_count": text.count("<viventium_feeling_state"),
                "placement": "direct_sdk_instructions_stdin", "materialized": True}

    def _supplemental_provider_failure(self, *, worker, run_id, stdout, stderr, exit_code):
        from .failure_classification import FailureClassification
        errors = [event for event in self._events(stdout) if event.get("type") == "direct.error"]
        if len(errors) != 1:
            return None
        error = errors[0]
        status = error.get("status_code")
        stage = error.get("stage")
        recovery = "Check the selected API connection and model settings, then retry the same request."
        if status is None and (stage == "output_validation" or error.get("error_type") == "UnexpectedModelBehavior"):
            kind, retryable, message = "provider_response_invalid", False, "The selected model returned a response that did not meet its required delivery contract."
            recovery = "Review any accepted work before retrying this turn. The provider connection was not classified as unavailable."
        elif status is None and stage in {"tool_configuration", "tool_connection", "tool_catalog", "tool_execution"}:
            kind, retryable, message = "native_mcp_unavailable", False, "A configured tool connection or tool action could not finish."
            recovery = "Check the affected tool connection and its access. Review any accepted action before retrying."
        elif status is None and stage in {"configuration", "history_read", "history_commit", "history_checkpoint", "provider_configuration"}:
            kind, retryable, message = "unsupported_runtime_configuration", False, "Direct settings or retained conversation state could not be used safely."
            recovery = "Inspect the private Direct stage diagnostic and repair the named configuration or state prerequisite."
        elif status is None and stage == "sdk_dependency":
            kind, retryable, message = "runtime_dependency_missing", False, "The Direct SDK dependency could not load."
            recovery = "Install the locked xPerfect Direct dependencies, then retry."
        elif status == 401:
            kind, retryable, message = "provider_auth_missing", False, "The selected API connection was rejected. Reconnect it or check its model permissions."
        elif status == 403:
            kind, retryable, message = "provider_request_rejected", False, "The selected API declined this request or its model access."
            recovery = "Check the request restrictions and model access. Review any accepted work before retrying."
        elif status == 429:
            kind, retryable, message = "provider_rate_limited", True, "The selected API connection is temporarily rate limited."
        elif isinstance(status, int) and 400 <= status < 500:
            kind, retryable, message = "provider_request_rejected", False, "The selected API rejected this model or its settings."
        else:
            kind, retryable, message = "provider_unavailable", True, "The selected Direct provider request could not finish."
        # SDK retries are disabled. Existing request-family evidence decides if
        # retry/fallback is safe after any authored text or tool activity.
        evidence = None
        if kind == "provider_rate_limited":
            evidence = {"version": 1, "failure_class": kind, "failure_structured": True,
                "retry_at": "", "retry_after_s": None, "evidence_kind": "direct_sdk_http_status",
                "evidence_id": hashlib.sha256(json.dumps({"worker": worker.get("worker_id"),
                    "run": run_id, "runtime": self.runtime_name, "status": status}, sort_keys=True).encode()).hexdigest()}
        return FailureClassification(failure_class=kind, retryable=retryable, user_message=message,
            recommended_recovery=recovery,
            diagnostic_summary="Direct SDK " + str(error.get("error_type") or "failure")[:80] + "; stage " + str(stage) + "; HTTP " + str(status),
            structured=True), evidence

    def _record_direct_stop(self, worker, run_id):
        if not run_id:
            return
        transcript = self._attempt_transcript(worker["worker_id"], run_id, str(worker.get("_run_attempt_id") or ""))
        if transcript is None:
            return
        receipt = {"type": "direct.stop", "local_client_stopped": True,
                   "remote_compute_stopped": None, "remote_compute_status": "unknown",
                   "automatic_tool_replay": False}
        with transcript[0].open("a") as stream:
            stream.write(json.dumps(receipt) + "\n")

    def interrupt_worker(self, worker, run_id=None):
        active = self._read_active_session(worker["worker_id"]) or {}
        scoped = worker
        if active.get("run_id") == run_id and active.get("attempt_id"):
            scoped = {**worker, "_run_attempt_id": active["attempt_id"]}
        info = super().interrupt_worker(worker, run_id=run_id)
        self._record_direct_stop(scoped, run_id)
        return info

    def terminate_worker(self, worker):
        run_id = str(worker.get("_active_run_id") or "")
        info = super().terminate_worker(worker)
        self._record_direct_stop(worker, run_id)
        return info

    def run_telemetry(self, worker, run_id):
        telemetry = super().run_telemetry(worker, run_id)
        transcript = self._attempt_transcript(worker["worker_id"], run_id, str(worker.get("_run_attempt_id") or ""))
        if transcript is not None and transcript[0].exists():
            stops = [e for e in self._events(transcript[0].read_text()) if e.get("type") == "direct.stop"]
            if stops:
                telemetry = {**telemetry, "direct_stop": stops[-1]}
        return telemetry

    # The SDK has no native permission/control extensions. Existing xPerfect
    # queued Steer/Stop/callback contracts still apply to the supervised turn.
    def supports_native_control(self, worker, action, **kwargs):
        return False

    def pending_native_input(self, worker, **kwargs):
        return None
