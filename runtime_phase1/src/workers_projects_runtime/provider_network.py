"""Opt-in, service-owned provider-only Docker networking.

A distinct internal isolated-gateway bridge belongs to each worker. Its only
other endpoint is an explicitly named trusted provider proxy. The proxy must
not expose administrative authority to worker credentials on any listener.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

ENV_KEY = "WPR_SANDBOX_PROVIDER_PROXY"
POLICY_LABEL = "com.glasshive.provider-only-policy"
WORKER_LABEL = "com.glasshive.provider-only-worker"
DEPLOYMENT_LABEL = "com.glasshive.provider-only-deployment"
POLICY = "isolated-gateway-v1"
OPTIONS = {
    "com.docker.network.bridge.enable_icc": "true",
    "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
}
# Non-root worker execs have no effective/permitted/inheritable/ambient caps.
# Only trusted control-plane root execs use this bounding set to repair owned
# bind mounts. no-new-privileges prevents the worker acquiring it through sudo.
MAINTENANCE_CAPABILITIES = frozenset({"CHOWN", "DAC_OVERRIDE", "FOWNER"})
ENTRYPOINT = ("/bin/bash",)
COMMAND = ("-c", "exec sleep infinity")


class ProviderOnlySetupChangedError(RuntimeError):
    def __init__(self):
        from .failure_classification import FailureClassification

        message = "Provider-only saved setup or inline files changed; create a fresh worker."
        super().__init__(message)
        self.failure_classification = FailureClassification(
            failure_class="provider_only_setup_changed", retryable=False,
            user_message=message,
            recommended_recovery=(
                "Create a fresh worker with the current saved setup. Use managed uploads "
                "for files that change between turns."
            ),
            diagnostic_summary="The immutable provider-only worker setup changed.",
            structured=True,
        )


@dataclass(frozen=True)
class ProviderProxy:
    container: str
    hostname: str


def configured_proxy() -> ProviderProxy | None:
    raw = os.environ.get(ENV_KEY, "").strip()
    if not raw:
        return None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Provider proxy configuration contains a duplicate key")
            result[key] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=unique)
    except json.JSONDecodeError as exc:
        raise ValueError("Provider proxy configuration must be JSON") from exc
    if not isinstance(data, dict) or set(data) != {"container", "hostname"}:
        raise ValueError("Provider proxy configuration requires only container and hostname")
    container, hostname = data["container"], data["hostname"]
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise ValueError("Provider proxy container name is invalid")
    if (not isinstance(hostname, str) or len(hostname) > 253
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in hostname.split("."))
            or hostname in {"localhost", "host.docker.internal", "gateway.docker.internal"}
            or re.fullmatch(r"[0-9.]+", hostname)):
        raise ValueError("Provider proxy hostname is invalid")
    return ProviderProxy(container, hostname)


def deployment_id(runtime_root: Path) -> str:
    return hashlib.sha256(str(runtime_root.resolve()).encode()).hexdigest()[:12]


def network_name(runtime_root: Path, container: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise ValueError("Provider-only worker container name is invalid")
    return f"wpr-provider-{deployment_id(runtime_root)}-{hashlib.sha256(container.encode()).hexdigest()[:16]}"


def labels(runtime_root: Path, container: str) -> dict[str, str]:
    return {POLICY_LABEL: POLICY, WORKER_LABEL: container, DEPLOYMENT_LABEL: deployment_id(runtime_root)}


def validate_worker(worker: dict) -> None:
    if (worker.get("bootstrap_profile") not in {"clean-room", "none"}
            or worker.get("_glasshive_provider_account_bound")
            or worker.get("_glasshive_inference_broker_bound")):
        raise ValueError("Provider-only workers require an explicit clean-room or none bootstrap without account or broker authority")
    if "_sandbox_setup" in worker:
        from .bootstrap import bootstrap_bundle_for, sandbox_setup_for

        def inline_files(bundle):
            return [entry for entry in bundle.get("files") or []
                    if not isinstance(entry, dict) or not entry.get("managed_upload_id")]

        saved = sandbox_setup_for(worker)["bootstrap_bundle_json"]
        if inline_files(saved) != inline_files(bootstrap_bundle_for(worker)):
            # Initial seeding is immutable. Only the existing managed-upload
            # owner can safely refresh per-turn file content in this generation.
            raise ProviderOnlySetupChangedError()
