import importlib.util
import json
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('network_policy', ROOT / 'deployment/linux/network_policy.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
POLICY = guard.contract.normalize_policy({
    'denied': [{'host': '169.254.169.254', 'port': 80}],
    'positive': [{'host': 'example.test', 'port': 443}]})
NETWORK = {'Id': 'a' * 64, 'Driver': 'bridge', 'EnableIPv6': False,
    'Labels': {'xperfect.package': 'xperfect-fixture'},
    'Options': {'com.docker.network.bridge.enable_icc': 'false'},
    'IPAM': {'Config': [{'Subnet': '10.44.0.0/24'}]}}


class Firewall:
    def __init__(self):
        self.chains = {'DOCKER-USER': [], 'UNRELATED': [['-j', 'RETURN']]}
        self.calls = []

    def __call__(self, argv, check=True):
        self.calls.append(argv)
        if argv[0] == 'ip':
            return subprocess.CompletedProcess(argv, 0, json.dumps([
                {'addr_info': [{'family': 'inet', 'local': '10.44.0.1'}]}]), '')
        action, name, *rule = argv[3:]
        result = 0
        output = ''
        if action == '-S':
            if name not in self.chains:
                result = 1
            else:
                output = '\n'.join([shlex.join(['-N', name]), *[shlex.join(['-A', name, *r]) for r in self.chains[name]]]) + '\n'
        elif action == '-N':
            if name in self.chains:
                result = 1
            else:
                self.chains[name] = []
        elif action == '-C':
            result = 0 if rule in self.chains.get(name, []) else 1
        elif action == '-A':
            self.chains[name].append(rule)
        elif action == '-I':
            assert rule.pop(0) == '1'
            self.chains[name].insert(0, rule)
        elif action == '-D':
            self.chains[name].remove(rule)
        elif action == '-X':
            if self.chains[name]:
                result = 1
            else:
                del self.chains[name]
        else:
            raise AssertionError(argv)
        if result and check:
            raise RuntimeError('Synthetic firewall refusal')
        return subprocess.CompletedProcess(argv, result, output, '')


def test_restrictive_rules_are_exact_network_scoped_and_repeat_is_idempotent():
    firewall = Firewall()
    before = json.dumps(firewall.chains['UNRELATED'])
    first = guard.apply_rules(NETWORK, POLICY, execute=firewall)
    assert guard.apply_rules(NETWORK, POLICY, execute=firewall) == first
    chain, jump, rules = guard.rules_for(NETWORK, POLICY)
    assert jump[:4] == ['-i', 'br-aaaaaaaaaaaa', '-s', '10.44.0.0/24']
    assert rules[0][-3:] == ['REJECT', '--reject-with', 'icmp-admin-prohibited']
    assert firewall.chains['DOCKER-USER'] == [jump]
    assert json.dumps(firewall.chains['UNRELATED']) == before
    assert not any('-F' in call or '--flush' in call for call in firewall.calls)
    guard.remove_rules(NETWORK, POLICY, execute=firewall)
    assert chain not in firewall.chains and firewall.chains['DOCKER-USER'] == []
    assert json.dumps(firewall.chains['UNRELATED']) == before


def test_unrecognized_rule_is_preserved_and_never_adopted():
    firewall = Firewall()
    guard.apply_rules(NETWORK, POLICY, execute=firewall)
    chain, _, _ = guard.rules_for(NETWORK, POLICY)
    firewall.chains[chain].insert(0, ['-d', '192.0.2.1/32', '-j', 'ACCEPT'])
    before = json.dumps(firewall.chains, sort_keys=True)
    with pytest.raises(RuntimeError, match='unrecognized'):
        guard.apply_rules(NETWORK, POLICY, execute=firewall)
    with pytest.raises(RuntimeError, match='unrecognized'):
        guard.remove_rules(NETWORK, POLICY, execute=firewall)
    assert json.dumps(firewall.chains, sort_keys=True) == before


def test_failed_positive_probe_rolls_back_only_new_owned_rules(monkeypatch):
    firewall = Firewall()
    monkeypatch.setattr(guard, 'require_operator', lambda: None)
    monkeypatch.setattr(guard.contract, 'probe_network', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('positive unavailable')))
    with pytest.raises(RuntimeError, match='positive unavailable'):
        guard.attest(docker=lambda _: json.dumps([NETWORK]), network='fixture-workers',
                     image='sha256:' + 'b' * 64, policy=POLICY, apply=True, execute=firewall)
    assert set(firewall.chains) == {'DOCKER-USER', 'UNRELATED'}
    assert not firewall.chains['DOCKER-USER']


def test_network_generation_drift_cannot_become_a_valid_proof(monkeypatch):
    firewall = Firewall()
    monkeypatch.setattr(guard, 'require_operator', lambda: None)
    monkeypatch.setattr(guard.contract, 'probe_network', lambda *a, **kw: {'network_id': 'c' * 64})
    with pytest.raises(RuntimeError, match='generation'):
        guard.attest(docker=lambda _: json.dumps([NETWORK]), network='fixture-workers',
                     image='sha256:' + 'b' * 64, policy=POLICY, apply=True, execute=firewall)
    assert not firewall.chains['DOCKER-USER']


def test_host_bridge_identity_is_required_before_any_firewall_write(monkeypatch):
    firewall = Firewall()
    monkeypatch.setattr(guard, 'require_operator', lambda: None)
    def execute(argv, check=True):
        if argv[0] == 'ip':
            return subprocess.CompletedProcess(argv, 0, '[{"addr_info":[]}]', '')
        return firewall(argv, check=check)
    with pytest.raises(ValueError, match='Docker host'):
        guard.attest(docker=lambda _: json.dumps([NETWORK]), network='fixture-workers',
                     image='sha256:' + 'b' * 64, policy=POLICY, apply=True, execute=execute)
    assert not firewall.calls


@pytest.mark.parametrize('label', [None, '', 'other-package', 'xperfect-../other'])
def test_foreign_package_refuses_before_any_host_operation(monkeypatch, label):
    firewall = Firewall()
    record = {**NETWORK, 'Labels': {'xperfect.package': label}}
    monkeypatch.setattr(guard, 'require_operator', lambda: None)
    with pytest.raises(ValueError, match='managed'):
        guard.attest(docker=lambda _: json.dumps([record]), network='fixture-workers',
                     image='sha256:' + 'b' * 64, policy=POLICY, apply=True, execute=firewall)
    assert not firewall.calls
