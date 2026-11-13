import json
import subprocess

import pytest

from workers_projects_runtime import provider_network as policy
from workers_projects_runtime.docker_sandbox import DockerSandboxManager


@pytest.mark.parametrize("value", ["[]", "{}", '{"container":"proxy","hostname":"model","extra":1}',
    '{"container":"proxy","container":"other","hostname":"model"}',
    '{"container":"--privileged","hostname":"model"}',
    '{"container":"proxy","hostname":"host.docker.internal"}',
    '{"container":"proxy","hostname":"127.0.0.1"}',
    '{"container":"proxy","hostname":"model:80"}',
    '{"container":"proxy","hostname":"A"}',
    '{"container":"proxy","hostname":"a..b"}'])
def test_invalid_service_proxy_fails_closed(monkeypatch, value):
    monkeypatch.setenv(policy.ENV_KEY, value)
    with pytest.raises(ValueError):
        policy.configured_proxy()


def test_absent_proxy_keeps_legacy_network_mode(monkeypatch):
    monkeypatch.delenv(policy.ENV_KEY, raising=False)
    assert policy.configured_proxy() is None


class Docker:
    def __init__(self):
        self.networks = {}
        self.proxy_id = "a" * 64
        self.aliases = {}
        self.workers = {}
        self.calls = []
        self.version = "29.1.3"

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        code, payload, error = 0, None, ""
        if args[0] == "version":
            return subprocess.CompletedProcess(args, 0, self.version + "\n", "")
        if args[:2] == ["network", "inspect"]:
            payload = [self.networks[args[2]]] if args[2] in self.networks else None
        elif args[:2] == ["network", "create"]:
            def fields(flag):
                return dict(args[index + 1].split("=", 1) for index, value in enumerate(args[:-1]) if value == flag)
            self.networks[args[-1]] = {"Name": args[-1], "Driver": "bridge", "Internal": True,
                "EnableIPv6": False, "Labels": fields("--label"), "Options": fields("--opt"), "Containers": {}}
            return subprocess.CompletedProcess(args, 0, args[-1], "")
        elif args[:2] == ["network", "connect"]:
            name, proxy = args[-2:]
            assert proxy == "proxy"
            self.networks[name]["Containers"][self.proxy_id] = {"Name": proxy}
            self.aliases[name] = [args[args.index("--alias") + 1]]
        elif args[:2] == ["network", "ls"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(self.networks), "")
        elif args[:2] == ["network", "disconnect"]:
            self.networks[args[2]]["Containers"].pop(args[3])
            self.aliases.pop(args[2], None)
        elif args[:2] == ["network", "rm"]:
            assert not self.networks[args[2]]["Containers"]
            self.networks.pop(args[2])
        elif args[0] == "inspect":
            if args[1] == "proxy":
                payload = [{"Name": "/proxy", "Id": self.proxy_id, "State": {"Running": True},
                    "NetworkSettings": {"Networks": {name: {"Aliases": aliases} for name, aliases in self.aliases.items()}}}]
            elif args[1] in self.workers:
                payload = [self.workers[args[1]]]
        if payload is None and (args[0] == "inspect" or args[:2] == ["network", "inspect"]):
            code, error = 1, "No such object"
        return subprocess.CompletedProcess(args, code, json.dumps(payload) if payload is not None else "", error)


@pytest.fixture
def network(tmp_path, monkeypatch):
    monkeypatch.setenv(policy.ENV_KEY, json.dumps({"container": "proxy", "hostname": "model-proxy"}))
    manager = DockerSandboxManager(str(tmp_path / "runtime"))
    docker = Docker()
    monkeypatch.setattr(manager, "_docker", docker)
    return manager, docker


def test_one_owned_internal_isolated_bridge_per_worker(network):
    manager, docker = network
    first = manager._ensure_provider_only_network("worker-one")
    second = manager._ensure_provider_only_network("worker-two")
    assert first != second
    for name in (first, second):
        entry = docker.networks[name]
        assert entry["Internal"] and entry["EnableIPv6"] is False
        assert entry["Options"] == policy.OPTIONS
        assert set(entry["Containers"]) == {docker.proxy_id}
        assert docker.aliases[name] == ["model-proxy"]
    assert manager._ensure_provider_only_network("worker-one") == first


@pytest.mark.parametrize("fault", ["foreign", "alias", "options", "labels", "ipv6", "internal"])
def test_network_drift_fails_closed(network, fault):
    manager, docker = network
    name = manager._ensure_provider_only_network("worker-one")
    entry = docker.networks[name]
    if fault == "foreign": entry["Containers"]["b" * 64] = {"Name": "foreign"}
    elif fault == "alias": docker.aliases[name] = ["wrong"]
    elif fault == "options": entry["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] = "nat"
    elif fault == "labels": entry["Labels"][policy.WORKER_LABEL] = "other-worker"
    elif fault == "ipv6": entry["EnableIPv6"] = True
    else: entry["Internal"] = False
    with pytest.raises(RuntimeError): manager._ensure_provider_only_network("worker-one")
    assert not any(call[:2] in (["network", "disconnect"], ["network", "rm"]) for call in docker.calls)


def test_proxy_recreation_reattaches_exact_new_generation(network):
    manager, docker = network
    name = manager._ensure_provider_only_network("worker-one")
    docker.networks[name]["Containers"].clear()
    docker.aliases.clear()
    docker.proxy_id = "c" * 64
    manager._ensure_provider_only_network("worker-one")
    assert set(docker.networks[name]["Containers"]) == {"c" * 64}


def test_proxy_replacement_mid_attestation_is_refused(network, monkeypatch):
    manager, docker = network
    manager._ensure_provider_only_network("worker-one")
    real = manager._provider_only_proxy_id
    def replaced():
        result = real()
        docker.proxy_id = "c" * 64
        return result
    monkeypatch.setattr(manager, "_provider_only_proxy_id", replaced)
    with pytest.raises(RuntimeError, match="endpoint"):
        manager._ensure_provider_only_network("worker-one")


@pytest.mark.parametrize("version", ["27.5.1", "", "not-a-version"])
def test_engine_without_isolated_gateway_is_refused(network, version):
    manager, docker = network
    docker.version = version
    with pytest.raises(RuntimeError, match="Engine 28"):
        manager._ensure_provider_only_network("worker-one")
    assert docker.networks == {}


def test_only_exact_owned_orphan_is_reclaimed(network):
    manager, docker = network
    name = manager._ensure_provider_only_network("worker-one")
    assert manager.repair_provider_only_networks() == ()
    assert name not in docker.networks


def test_foreign_endpoint_prevents_orphan_cleanup(network):
    manager, docker = network
    name = manager._ensure_provider_only_network("worker-one")
    docker.networks[name]["Containers"]["b" * 64] = {"Name": "foreign"}
    with pytest.raises(RuntimeError): manager.repair_provider_only_networks()
    assert name in docker.networks


def test_provider_create_is_inert_non_root_without_desktop_ports(network, monkeypatch):
    manager, docker = network
    worker = {"worker_id": "wrk_synthetic", "bootstrap_profile": "clean-room", "bootstrap_bundle_json": {}}
    paths = manager.paths(worker["worker_id"])
    manager._ensure_host_dirs(paths)
    calls = []
    monkeypatch.setattr(manager, "_ensure_provider_only_network", lambda _: "isolated-worker")
    monkeypatch.setattr(manager, "_desktop_credentials", lambda *_a, **_k: pytest.fail("headless worker requested desktop credentials"))
    monkeypatch.setattr(manager, "_docker", lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "a" * 64, ""))
    manager._create_container(manager._container_name(worker["worker_id"]), paths, worker=worker)
    args = calls[-1]
    assert args[0] == "create" and "-p" not in args and "--add-host" not in args
    assert args[args.index("--user") + 1] == "1200:1201"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert {args[index + 1] for index, item in enumerate(args) if item == "--cap-add"} == policy.MAINTENANCE_CAPABILITIES
    assert args[-3:] == [manager.image, *policy.COMMAND]


@pytest.mark.parametrize("field,value", [
    ("container_id", "b" * 64), ("provider_only_generation", "different"),
    ("image_id", "sha256:" + "b" * 64), ("runtime_user", "root"), ("runtime_user", "seluser"),
    ("entrypoint", ("/bin/sh",)), ("command", ("-c", "unexpected")),
    ("network_mode", "host"), ("attached_networks", ("other",)),
    ("privileged", True), ("extra_hosts", ("host.docker.internal:host-gateway",)),
    ("port_bindings", ((8000, "127.0.0.1", 12345),)),
    ("cap_add", ("SYS_ADMIN",)), ("cap_drop", ()),
    ("security_options", ("seccomp=unconfined",)), ("pid_mode", "host"),
    ("ipc_mode", "host"), ("uts_mode", "host"), ("userns_mode", "host"),
    ("cgroupns_mode", "host"), ("tmpfs_targets", ("/workspace/input",)),
    ("mount_records", (("bind", "/unexpected", "/workspace"),)),
])
def test_provider_worker_generation_drift_is_refused(network, field, value):
    from dataclasses import replace
    from workers_projects_runtime.docker_sandbox import SandboxInfo
    manager, _ = network
    worker = {"worker_id": "wrk_synthetic", "bootstrap_profile": "clean-room", "bootstrap_bundle_json": {}}
    paths = manager.paths(worker["worker_id"])
    name = manager._container_name(worker["worker_id"])
    network_name = policy.network_name(manager.runtime_root, name)
    identity = "a" * 64
    sandbox = SandboxInfo(container_name=name, container_id=identity, state="created",
        workspace_dir=str(paths["workspace_dir"]), home_dir=str(paths["home_dir"]), pid=None, image=manager.image,
        image_id="sha256:" + identity, expected_image_id="sha256:" + identity,
        image_reference=manager.image, runtime_user="1200:1201", expected_runtime_user="1200:1201",
        entrypoint=policy.ENTRYPOINT, command=policy.COMMAND,
        provider_only_generation=manager._provider_only_generation(worker),
        network_mode=network_name, attached_networks=(network_name,), privileged=False,
        cap_drop=("ALL",), cap_add=tuple(policy.MAINTENANCE_CAPABILITIES),
        security_options=("no-new-privileges:true",), pid_mode="", uts_mode="", userns_mode="",
        ipc_mode="private", cgroupns_mode="private",
        mount_records=(("bind", str(paths["workspace_dir"]), manager.workspace_mount),
                       ("bind", str(paths["home_dir"]), manager.home_mount)))
    manager._require_provider_only_sandbox(worker, sandbox, expected_id=identity)
    with pytest.raises(RuntimeError, match="inline files changed" if field == "provider_only_generation" else "execution policy") as caught:
        manager._require_provider_only_sandbox(worker, replace(sandbox, **{field: value}), expected_id=identity)
    if field == "provider_only_generation":
        classification = caught.value.failure_classification
        assert classification.failure_class == "provider_only_setup_changed"
        assert classification.retryable is False


@pytest.mark.parametrize("worker", [{}, {"bootstrap_profile": "host-login"},
    {"bootstrap_profile": "clean-room", "_glasshive_provider_account_bound": True},
    {"bootstrap_profile": "none", "_glasshive_inference_broker_bound": True}])
def test_provider_only_cannot_copy_host_or_account_authority(worker):
    with pytest.raises(ValueError, match="explicit clean-room"):
        policy.validate_worker(worker)


@pytest.mark.parametrize("replacement", [
    [],
    [{"scope": "workspace", "path": "uploads/source.txt", "content": "second turn"}],
    [{"scope": "workspace", "path": "uploads/new.txt", "content": "first turn"}],
])
def test_provider_only_refuses_changed_inline_files_before_bootstrap_seed(network, monkeypatch, replacement):
    from workers_projects_runtime.bootstrap import sandbox_setup_for

    manager, _ = network
    stored = {"worker_id": "wrk_synthetic", "bootstrap_profile": "clean-room", "bootstrap_bundle_json": {
        "files": [{"scope": "workspace", "path": "uploads/source.txt", "content": "first turn"}],
    }}
    projected = {**stored, "_sandbox_setup": sandbox_setup_for(stored),
                 "bootstrap_bundle_json": {"files": replacement}}
    monkeypatch.setattr(manager, "_require_docker", lambda: None)
    monkeypatch.setattr(manager, "_ensure_host_dirs", lambda *_: pytest.fail("Changed inline files reached seeding"))
    with pytest.raises(policy.ProviderOnlySetupChangedError) as caught:
        manager.ensure_ready(projected, "codex-cli")
    from workers_projects_runtime.failure_classification import classify_runtime_error
    failure = classify_runtime_error(caught.value, runtime_name="codex-cli")
    assert failure.failure_class == "provider_only_setup_changed"
    assert failure.structured and not failure.retryable


def test_provider_only_keeps_managed_upload_refresh_with_unchanged_inline_files():
    from workers_projects_runtime.bootstrap import sandbox_setup_for

    original = {"scope": "workspace", "path": "uploads/source.txt", "content": "saved inline"}
    stored = {"bootstrap_profile": "clean-room", "bootstrap_bundle_json": {
        "files": [original, {"managed_upload_id": "old-managed-input"}],
    }}
    projected = {**stored, "_sandbox_setup": sandbox_setup_for(stored),
                 "bootstrap_bundle_json": {"files": [original, {"managed_upload_id": "new-managed-input"}]}}
    policy.validate_worker(projected)


def test_generation_uses_stored_setup_and_retains_two_run_projection_authority(network):
    from workers_projects_runtime.bootstrap import bootstrap_bundle_for
    manager, _ = network
    stored = {"worker_id": "wrk_synthetic", "bootstrap_profile": "clean-room", "bootstrap_bundle_json": {}}
    runs = []
    for index in (1, 2):
        run = {**stored, "_active_run_id": f"run-{index}", "_coordinator_native_projection": {
            "worker_id": stored["worker_id"], "run_id": f"run-{index}",
            "token": f"synthetic-token-{index}", "url": "http://coordinator.invalid/mcp"}}
        assert f"synthetic-token-{index}" in json.dumps(bootstrap_bundle_for(run))
        runs.append(run)
    assert manager._provider_only_generation(stored) == manager._provider_only_generation(runs[0]) == manager._provider_only_generation(runs[1])
    changed = {**stored, "bootstrap_bundle_json": {"project_definition": "Changed stored setup"}}
    assert manager._provider_only_generation(stored) != manager._provider_only_generation(changed)


def test_service_without_configuration_preserves_generation_before_run_authority(network):
    from workers_projects_runtime.bootstrap import bootstrap_bundle_for
    from workers_projects_runtime.service import WorkersProjectsService

    manager, _ = network
    stored = {"worker_id": "wrk_synthetic", "bootstrap_profile": "clean-room", "bootstrap_bundle_json": "{}"}
    for index in (1, 2):
        projected = WorkersProjectsService._runtime_worker_for_run(stored, {
            "runtime_bundle_json": json.dumps({"env": {"EXAMPLE_ATTEMPT_TOKEN": f"synthetic-{index}"}}),
        })
        assert bootstrap_bundle_for(projected)["env"]["EXAMPLE_ATTEMPT_TOKEN"] == f"synthetic-{index}"
        assert manager._provider_only_generation(projected) == manager._provider_only_generation(stored)


def test_factory_rejects_unsafe_proxy_from_neutral_config_before_state(tmp_path, monkeypatch):
    from workers_projects_runtime.api import create_app
    selected = tmp_path / "runtime.env"
    selected.write_text('WPR_SANDBOX_PROVIDER_PROXY=\'{"container":"proxy","hostname":"host.docker.internal"}\'\n')
    monkeypatch.setenv("WPR_RUNTIME_ENV_FILE", str(selected))
    monkeypatch.setenv(policy.ENV_KEY, "")
    target = tmp_path / "not-opened" / "runtime.db"
    with pytest.raises(ValueError, match="hostname"):
        create_app(db_path=str(target), runtime_backend="stub")
    assert not target.parent.exists()
