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
    'denied': [{'host': '169.254.169.254', 'probe_port': 80}],
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
    guard.remove_rules(NETWORK, POLICY, execute=firewall)
    assert json.dumps(firewall.chains['UNRELATED']) == before


def test_denial_covers_all_protocols_and_only_explicit_narrow_exceptions_precede_it():
    policy = {**POLICY, 'denied': [{'host': '169.254.169.254', 'probe_port': 80, 'exceptions': [
        {'protocol': 'udp', 'port': 53}, {'protocol': 'tcp', 'port': 53}]}]}
    _, _, rules = guard.rules_for(NETWORK, policy)
    assert rules == [
        ['-d', '169.254.169.254/32', '-p', 'tcp', '-m', 'tcp', '--dport', '53', '-j', 'RETURN'],
        ['-d', '169.254.169.254/32', '-p', 'udp', '-m', 'udp', '--dport', '53', '-j', 'RETURN'],
        ['-d', '169.254.169.254/32', '-j', 'REJECT', '--reject-with', 'icmp-admin-prohibited'],
        ['-j', 'RETURN']]


def test_an_exception_never_accepts_forwarding_or_adopts_a_rule_that_bypasses_icc():
    policy = {**POLICY, 'denied': [{'host': '10.44.0.3', 'probe_port': 18081,
                                  'exceptions': [{'protocol': 'tcp', 'port': 18080}]}]}
    firewall = Firewall()
    guard.apply_rules(NETWORK, policy, execute=firewall)
    chain, _, rules = guard.rules_for(NETWORK, policy)
    assert rules[0][-2:] == ['-j', 'RETURN']
    assert rules[1][-3:] == ['REJECT', '--reject-with', 'icmp-admin-prohibited']
    assert all('ACCEPT' not in rule for rule in rules)
    # An earlier ACCEPT-shaped exception would bypass Docker's ICC=false rules.
    # It is foreign state now and must not be adopted or silently replaced.
    firewall.chains[chain][0][-1] = 'ACCEPT'
    before = json.dumps(firewall.chains, sort_keys=True)
    for operation in (guard.verify_rules, guard.apply_rules, guard.remove_rules):
        with pytest.raises(RuntimeError, match='unrecognized'):
            operation(NETWORK, policy, execute=firewall)
    assert json.dumps(firewall.chains, sort_keys=True) == before


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


def test_earlier_accept_cannot_be_blessed_by_refresh_or_reapply():
    firewall = Firewall()
    guard.apply_rules(NETWORK, POLICY, execute=firewall)
    firewall.chains['DOCKER-USER'].insert(0, ['-j', 'ACCEPT'])
    before = json.dumps(firewall.chains, sort_keys=True)
    for operation in (guard.verify_rules, guard.apply_rules):
        with pytest.raises(RuntimeError, match='can be bypassed'):
            operation(NETWORK, POLICY, execute=firewall)
    assert json.dumps(firewall.chains, sort_keys=True) == before


def test_another_bridge_rule_is_safe_but_wildcard_or_negation_is_not():
    firewall = Firewall()
    guard.apply_rules(NETWORK, POLICY, execute=firewall)
    firewall.chains['DOCKER-USER'].insert(0, ['-i', 'br-other', '-j', 'ACCEPT'])
    guard.verify_rules(NETWORK, POLICY, execute=firewall)
    for rule in (['-i', 'br-aaa+', '-j', 'ACCEPT'], ['!', '-i', 'br-other', '-j', 'ACCEPT']):
        firewall.chains['DOCKER-USER'][0] = rule
        with pytest.raises(RuntimeError, match='can be bypassed'):
            guard.verify_rules(NETWORK, POLICY, execute=firewall)


def test_owned_dispatch_accepts_iptables_canonical_order_without_accepting_extra_fields():
    firewall = Firewall()
    def canonical(args, **kwargs):
        result = firewall(args, **kwargs)
        if args[3:] == ['-S', 'DOCKER-USER'] and result.returncode == 0:
            chain, jump, _ = guard.rules_for(NETWORK, POLICY)
            printed = ['-s', jump[3], '-i', jump[1], '-j', chain]
            result.stdout = result.stdout.replace(shlex.join(jump), shlex.join(printed))
        return result
    guard.apply_rules(NETWORK, POLICY, execute=canonical)
    guard.verify_rules(NETWORK, POLICY, execute=canonical)
    chain, jump, _ = guard.rules_for(NETWORK, POLICY)
    for altered in (['!', *jump], [*jump, '-m', 'comment', '--comment', 'extra'],
                    ['-i', jump[1], '-i', jump[1], '-j', chain]):
        firewall.chains['DOCKER-USER'] = [altered]
        with pytest.raises(RuntimeError):
            guard.verify_rules(NETWORK, POLICY, execute=canonical)
    firewall.chains['DOCKER-USER'] = [jump]
    guard.remove_rules(NETWORK, POLICY, execute=canonical)


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


@pytest.mark.parametrize('runtime', [None, 'foreign', 'wrong-generation'])
def test_public_apply_requires_exact_existing_runtime_before_host_operations(tmp_path, monkeypatch, runtime):
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(POLICY))
    argv = ['network_policy.py', 'apply', '--network', 'fixture-workers', '--policy', str(policy),
            '--image', 'sha256:' + 'b' * 64, '--receipt', str(tmp_path / 'proof.json')]
    if runtime:
        argv += ['--runtime-container', runtime]
    monkeypatch.setattr(guard.sys, 'argv', argv)
    monkeypatch.setattr(guard, 'require_operator', lambda: None)
    def execute(args, **kwargs):
        assert args[:1] == ['docker'], 'No host firewall operation may precede exact runtime validation'
        record = NETWORK if args[3:5] == ['network', 'inspect'] else {
            'Config': {'Labels': {'xperfect.package': 'xperfect-fixture' if runtime == 'wrong-generation' else 'foreign',
                                  'xperfect.role': 'runtime'}},
            'NetworkSettings': {'Networks': {'fixture-workers': {'NetworkID': 'c' * 64}}}}
        return subprocess.CompletedProcess(args, 0, json.dumps([record]), '')
    monkeypatch.setattr(guard, 'run', execute)
    monkeypatch.setattr(guard, 'attest', lambda **kwargs: pytest.fail('Unvalidated runtime'))
    with pytest.raises(ValueError, match='existing package runtime|exact package bridge'):
        guard.main()
    assert not (tmp_path / 'proof.json').exists()


def test_public_remove_accepts_only_empty_exact_managed_bridge_after_runtime_removal(tmp_path, monkeypatch):
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(POLICY))
    monkeypatch.setattr(guard.sys, 'argv', ['network_policy.py', 'remove', '--network', 'fixture-workers',
        '--policy', str(policy), '--image', 'sha256:' + 'b' * 64, '--receipt', str(tmp_path / 'proof.json')])
    monkeypatch.setattr(guard, 'require_operator', lambda: None)
    record = {**NETWORK, 'Containers': {'active': {}}}
    monkeypatch.setattr(guard, 'run', lambda args, **kw: subprocess.CompletedProcess(args, 0, json.dumps([record]), ''))
    removed = []
    fail = [True]
    def remove(row, policy):
        assert json.loads((tmp_path / 'proof.json').read_text())['enforcement'] == 'removing'
        if fail[0]:
            raise RuntimeError('Synthetic removal refusal')
        removed.append(row['Id'])
    monkeypatch.setattr(guard, 'remove_rules', remove)
    with pytest.raises(ValueError, match='worker containers'):
        guard.main()
    assert not removed and not (tmp_path / 'proof.json').exists()
    record['Containers'] = {}
    with pytest.raises(RuntimeError, match='removal refusal'):
        guard.main()
    assert json.loads((tmp_path / 'proof.json').read_text())['enforcement'] == 'removing'
    fail[0] = False
    guard.main()
    assert removed == [NETWORK['Id']]
    assert json.loads((tmp_path / 'proof.json').read_text())['enforcement'] == 'removed'


@pytest.mark.parametrize('installed', [False, True])
def test_apply_revokes_removal_ack_before_new_rules_but_preserves_current_proof_on_repeat(tmp_path, monkeypatch, installed):
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(POLICY))
    proof = tmp_path / 'proof.json'
    copied = []
    monkeypatch.setattr(guard.sys, 'argv', ['network_policy.py', 'apply', '--network', 'fixture-workers',
        '--policy', str(policy), '--image', 'sha256:' + 'b' * 64, '--receipt', str(proof),
        '--runtime-container', 'fixture-runtime'])
    monkeypatch.setattr(guard, 'require_operator', lambda: None)
    runtime = {'Id': 'd' * 64, 'Config': {'Labels': {'xperfect.package': 'xperfect-fixture', 'xperfect.role': 'runtime'}},
               'NetworkSettings': {'Networks': {'fixture-workers': {'NetworkID': NETWORK['Id']}}}}
    def execute(arguments, **kwargs):
        if arguments[0] == 'iptables':
            return subprocess.CompletedProcess(arguments, 0 if installed else 1, '', '')
        args = arguments[3:]
        if args[:1] == ['cp']:
            copied.append(json.loads(proof.read_text())['enforcement'])
            return subprocess.CompletedProcess(arguments, 0, '', '')
        return subprocess.CompletedProcess(arguments, 0, json.dumps([NETWORK if args[:2] == ['network', 'inspect'] else runtime]), '')
    def attest(**kwargs):
        assert copied == ([] if installed else ['applying'])
        return {'version': 1, 'enforcement': 'iptables_readback'}
    monkeypatch.setattr(guard, 'run', execute)
    monkeypatch.setattr(guard, 'attest', attest)
    guard.main()
    assert copied == (['iptables_readback'] if installed else ['applying', 'iptables_readback'])


@pytest.mark.parametrize('exception_bypasses_icc', [False, True])
def test_live_peer_check_refuses_an_exception_that_enables_a_real_listener(monkeypatch, exception_bypasses_icc):
    spec = importlib.util.spec_from_file_location('check_worker_network_live', ROOT / 'tests/check_worker_network_live.py')
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    applied, calls, removed = [False], [], []
    def docker(args):
        calls.append(args)
        if args[:2] == ['network', 'create']:
            return 'a' * 64
        if args[:2] == ['run', '-d']:
            return 'b' * 64
        if args[:1] == ['inspect']:
            return json.dumps([{'NetworkSettings': {'Networks': {'fixture-peer': {'IPAddress': '10.44.0.3'}}}}])
        if args[:2] == ['network', 'inspect']:
            return json.dumps([NETWORK])
        if args[:2] == ['run', '--rm']:
            return json.dumps({'results': [
                {'kind': 'positive', 'host': 'example.test', 'port': 443, 'status': 'reachable'},
                {'kind': 'positive', 'host': '10.44.0.3', 'port': 18080,
                 'status': 'reachable' if applied[0] and exception_bypasses_icc else 'unavailable'},
                {'kind': 'denied', 'host': '10.44.0.3', 'port': 18081, 'status': 'refused', 'errno': 113}]})
        return ''
    def attest(**kwargs):
        assert kwargs['policy']['denied'][0]['exceptions'] == [{'protocol': 'tcp', 'port': 18080}]
        applied[0] = True
        return {'results': [{'errno': 113}]}
    monkeypatch.setattr(live.guard, 'attest', attest)
    monkeypatch.setattr(live.guard, 'remove_rules', lambda *args: removed.append(True))
    action = lambda: live.check_peer_exception_isolation(docker, network='fixture-peer',
        image='sha256:' + 'b' * 64, positive=[{'host': 'example.test', 'port': 443}])
    if exception_bypasses_icc:
        with pytest.raises(AssertionError, match='bypassed Docker worker isolation'):
            action()
    else:
        action()
        assert len([call for call in calls if call[:1] == ['exec']]) == 2
    assert removed == [True]
    assert ['rm', '-f', 'b' * 64] in calls
    assert ['network', 'rm', 'a' * 64] in calls
