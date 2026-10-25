import json
import errno
import os
from pathlib import Path
import subprocess

import pytest

from workers_projects_runtime.worker_egress import (
    POLICY_ENV, WorkerEgressUnavailable, configured_policy, network_identity,
    normalize_policy, policy_digest, probe_network, require_current_receipt,
)


NETWORK_ID = 'a' * 64
POLICY = {'denied': [{'host': '169.254.169.254', 'probe_port': 80}],
          'positive': [{'host': 'example.test', 'port': 443}], 'max_age_seconds': 30}


def bridge():
    return {'Id': NETWORK_ID, 'Driver': 'bridge', 'EnableIPv6': False,
            'Options': {'com.docker.network.bridge.enable_icc': 'false'},
            'IPAM': {'Config': [{'Subnet': '10.44.0.0/24'}]}}


def proof():
    return {'version': 1, 'enforcement': 'iptables_readback', 'network_id': NETWORK_ID,
            'policy_sha256': policy_digest(POLICY), 'rules_sha256': 'b' * 64,
            'observed_at': 100.0}


def configure(tmp_path, monkeypatch, receipt=None):
    monkeypatch.setenv(POLICY_ENV, json.dumps(POLICY))
    monkeypatch.setenv('XPERFECT_CONTROL_ROOT', str(tmp_path))
    path = tmp_path / 'worker-egress.json'
    path.write_text(json.dumps(proof() if receipt is None else receipt))
    path.chmod(0o600)
    return path


def test_disabled_policy_performs_no_file_or_docker_operation(monkeypatch):
    monkeypatch.delenv(POLICY_ENV, raising=False)
    monkeypatch.delenv('XPERFECT_CONTROL_ROOT', raising=False)
    require_current_receipt(network='unused', docker=lambda _: pytest.fail('Unexpected Docker call'))
    assert configured_policy() is None


@pytest.mark.parametrize('value', [None, {}, {'denied': [], 'positive': []},
    {**POLICY, 'surprise': 'ignored?'}, {**POLICY, 'max_age_seconds': True},
    {**POLICY, 'max_age_seconds': 3601}, {**POLICY, 'timeout_seconds': float('nan')},
    {**POLICY, 'denied': [{'host': 'metadata.example.test', 'port': 80}]},
    {**POLICY, 'denied': [{'host': '::1', 'port': 80}]},
    {**POLICY, 'denied': [{'host': '127.0.0.1', 'port': 80}]},
    {**POLICY, 'denied': [{'host': '169.254.169.254', 'port': True}]},
    {**POLICY, 'positive': [{'host': 'https://example.test/path', 'port': 443}]}])
def test_invalid_configuration_refuses(value):
    with pytest.raises(ValueError):
        normalize_policy(value)


def test_policy_digest_uses_normalized_target_identity():
    expanded = normalize_policy(POLICY)
    assert policy_digest(POLICY) == policy_digest(expanded)
    assert policy_digest({**POLICY, 'denied': [{'host': '169.254.169.254', 'port': 443}]}) != policy_digest(POLICY)
    assert policy_digest({**POLICY, 'denied': [{'host': '169.254.169.254', 'port': 80}]}) == policy_digest(POLICY)


@pytest.mark.parametrize('denied', [
    [{'host': '169.254.169.254', 'probe_port': 80}, {'host': '169.254.169.254', 'probe_port': 443}],
    [{'host': '169.254.169.254', 'probe_port': 80, 'port': 443}],
    [{'host': '169.254.169.254', 'probe_port': 80, 'exceptions': [{'protocol': 'tcp', 'port': 80}]}],
    [{'host': '169.254.169.254', 'probe_port': 80, 'exceptions': [{'protocol': 'all', 'port': 53}]}],
    [{'host': '169.254.169.254', 'probe_port': 80, 'exceptions': [{'protocol': 'udp', 'port': True}]}],
    [{'host': '169.254.169.254', 'probe_port': 80, 'exceptions': [{'protocol': 'udp', 'port': 53}] * 2}],
])
def test_denied_address_and_narrow_exceptions_are_unambiguous(denied):
    with pytest.raises(ValueError):
        normalize_policy({**POLICY, 'denied': denied})


def test_dns_exceptions_are_explicit_and_positive_control_cannot_hide_in_denied_address():
    policy = {**POLICY, 'positive': [{'host': '169.254.169.254', 'port': 53}]}
    with pytest.raises(ValueError, match='explicit TCP exception'):
        normalize_policy(policy)
    policy['denied'] = [{**POLICY['denied'][0], 'exceptions': [
        {'protocol': 'udp', 'port': 53}, {'protocol': 'tcp', 'port': 53}]}]
    assert normalize_policy(policy)['denied'][0]['exceptions'] == [
        {'protocol': 'tcp', 'port': 53}, {'protocol': 'udp', 'port': 53}]


@pytest.mark.parametrize('change', [{'network_id': 'c' * 64}, {'policy_sha256': 'c' * 64},
    {'observed_at': 1}, {'observed_at': 101}, {'rules_sha256': ''}, {'enforcement': 'removed'}])
def test_changed_stale_or_future_proof_refuses(tmp_path, monkeypatch, change):
    configure(tmp_path, monkeypatch, {**proof(), **change})
    with pytest.raises(WorkerEgressUnavailable):
        require_current_receipt(network='fixture-workers', docker=lambda _: json.dumps([bridge()]), now=100)


def test_matching_current_private_proof_passes(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    require_current_receipt(network='fixture-workers', docker=lambda _: json.dumps([bridge()]), now=110)


@pytest.mark.parametrize('initial', ['missing', 'partial'])
def test_proof_publication_retries_once_then_validates_fresh_content(tmp_path, monkeypatch, initial):
    path = configure(tmp_path, monkeypatch)
    if initial == 'missing':
        path.unlink()
    else:
        path.write_text('{')
    waits = []
    def publish(delay):
        waits.append(delay)
        path.write_text(json.dumps(proof()))
        path.chmod(0o600)
    monkeypatch.setattr('workers_projects_runtime.worker_egress.time.sleep', publish)
    require_current_receipt(network='fixture-workers', docker=lambda _: json.dumps([bridge()]), now=110)
    assert waits == [0.05]


def test_missing_proof_retry_is_bounded_and_stale_refresh_never_passes(tmp_path, monkeypatch):
    path = configure(tmp_path, monkeypatch)
    path.unlink()
    waits = []
    monkeypatch.setattr('workers_projects_runtime.worker_egress.time.sleep', waits.append)
    with pytest.raises(WorkerEgressUnavailable, match='administrator.*apply'):
        require_current_receipt(network='fixture-workers', docker=lambda _: pytest.fail('No proof'), now=110)
    assert waits == [0.05]


def test_every_workspace_native_command_rechecks_proof_and_recovers_without_restart(tmp_path, monkeypatch):
    from workers_projects_runtime.workspace_box import WorkspaceBox, WorkspaceMemberBinding
    path = configure(tmp_path, monkeypatch, {**proof(), 'observed_at': 0})
    monkeypatch.setattr('workers_projects_runtime.worker_egress.time.time', lambda: 110)
    box = WorkspaceBox(volume_root=tmp_path / 'data', control_root=tmp_path / 'workspaces',
        volume_name='fixture-data', image='fixture-image', network='fixture-workers',
        binding=WorkspaceMemberBinding('wsp_one', 'wrk_one', 'tenant', 'owner', 20001), memory_bytes=1024, pids_limit=64)
    calls = []
    monkeypatch.setattr(box, 'ensure_box', lambda: calls.append('native') or 'c' * 64)
    box.docker = lambda args: subprocess.CompletedProcess(args, 0, json.dumps([bridge()]), '')
    with pytest.raises(WorkerEgressUnavailable, match='administrator.*apply'):
        box.command(['/usr/local/bin/claude'])
    assert not calls
    path.write_text(json.dumps(proof()))
    assert box.command(['/usr/local/bin/claude'])[-1] == '/usr/local/bin/claude'
    assert calls == ['native']
    monkeypatch.setattr('workers_projects_runtime.worker_egress.time.time', lambda: 131)
    with pytest.raises(WorkerEgressUnavailable):
        box.command(['/usr/local/bin/claude'])
    assert calls == ['native']


def test_public_or_linked_proof_is_not_authority(tmp_path, monkeypatch):
    path = configure(tmp_path, monkeypatch)
    path.chmod(0o644)
    with pytest.raises(WorkerEgressUnavailable):
        require_current_receipt(network='fixture-workers', docker=lambda _: json.dumps([bridge()]), now=110)
    path.chmod(0o600)
    os.link(path, tmp_path / 'second-link')
    with pytest.raises(WorkerEgressUnavailable):
        require_current_receipt(network='fixture-workers', docker=lambda _: json.dumps([bridge()]), now=110)


@pytest.mark.parametrize('change', [{'EnableIPv6': True}, {'Driver': 'overlay'}, {'Id': 'unknown'},
    {'Options': {'com.docker.network.bridge.enable_icc': 'true'}}])
def test_network_drift_never_reuses_proof(change):
    with pytest.raises(WorkerEgressUnavailable):
        network_identity({**bridge(), **change})


def test_probe_has_no_data_mount_or_privilege_and_requires_both_controls():
    calls = []
    def docker(args):
        calls.append(args)
        if args[:2] == ['network', 'inspect']:
            return json.dumps([bridge()])
        return json.dumps({'results': [
            {'kind': 'positive', 'host': 'example.test', 'port': 443, 'status': 'reachable', 'errno': None},
            {'kind': 'denied', 'host': '169.254.169.254', 'port': 80, 'status': 'refused', 'errno': 113}]})
    receipt = probe_network(docker, network='fixture-workers', image='sha256:' + 'a' * 64, policy=POLICY)
    assert receipt['network_id'] == NETWORK_ID
    command = calls[-1]
    assert '--mount' not in command and '--privileged' not in command and '--cap-add' not in command
    assert command[command.index('--network') + 1] == 'fixture-workers'
    assert command[command.index('--user') + 1] == '65534:65534'
    assert command[command.index('--entrypoint') + 1] == '/usr/bin/python3'


@pytest.mark.parametrize('status', ['reachable', 'unavailable'])
def test_failed_deny_or_unavailable_probe_is_not_passed(status):
    def docker(args):
        if args[:2] == ['network', 'inspect']:
            return json.dumps([bridge()])
        return json.dumps({'results': [
            {'kind': 'positive', 'host': 'example.test', 'port': 443, 'status': 'reachable'},
            {'kind': 'denied', 'host': '169.254.169.254', 'port': 80, 'status': status}]})
    with pytest.raises(WorkerEgressUnavailable):
        probe_network(docker, network='fixture-workers', image='sha256:' + 'a' * 64, policy=POLICY)


@pytest.mark.parametrize('code', [None, errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ENETUNREACH, 0, True])
def test_only_the_qualified_linux_reject_signature_proves_denial(code):
    def docker(args):
        if args[:2] == ['network', 'inspect']:
            return json.dumps([bridge()])
        return json.dumps({'results': [
            {'kind': 'positive', 'host': 'example.test', 'port': 443, 'status': 'reachable', 'errno': None},
            {'kind': 'denied', 'host': '169.254.169.254', 'port': 80, 'status': 'refused', 'errno': code}]})
    with pytest.raises(WorkerEgressUnavailable):
        probe_network(docker, network='fixture-workers', image='sha256:' + 'a' * 64, policy=POLICY)
