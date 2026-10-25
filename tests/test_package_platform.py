import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_platform', ROOT / 'deployment/linux/launch.py')
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


@pytest.mark.parametrize('architecture,expected', [('aarch64', 'linux/arm64'), ('arm64', 'linux/arm64'),
                                                   ('x86_64', 'linux/amd64'), ('amd64', 'linux/amd64')])
def test_platform_comes_from_docker_not_client_machine(architecture, expected):
    assert launch.daemon_platform({'OSType': 'linux', 'Architecture': architecture}) == expected


@pytest.mark.parametrize('value', [{}, {'OSType': 'darwin', 'Architecture': 'arm64'},
                                   {'OSType': 'linux', 'Architecture': 's390x'}])
def test_unknown_platform_refuses(value):
    with pytest.raises(ValueError, match='platform'):
        launch.daemon_platform(value)


def test_explicit_wrong_architecture_is_never_emulated():
    identity = 'sha256:' + 'a' * 64
    with patch.object(launch, 'docker', return_value=json.dumps({'Id': identity, 'Os': 'linux', 'Architecture': 'arm64'})) as docker:
        with pytest.raises(ValueError, match='emulation'):
            launch.resolve_image('unix:///test.sock', identity, platform='linux/amd64')
        assert not any('pull' in call.args for call in docker.call_args_list)


def test_multi_platform_index_recovers_native_member_from_other_platform_cache():
    reference = 'ghcr.io/example/service@sha256:' + 'a' * 64
    arm, native = 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64
    with patch.object(launch, 'docker', side_effect=[arm, json.dumps({'Id': arm, 'Os': 'linux', 'Architecture': 'arm64'}),
             '', native, json.dumps({'Id': native, 'Os': 'linux', 'Architecture': 'amd64'})]) as docker:
        assert launch.resolve_image('unix:///test.sock', reference, platform='linux/amd64') == native
        assert docker.call_args_list[2].args == ('unix:///test.sock', 'pull', '--platform', 'linux/amd64', reference)


def test_unqualified_release_platform_is_not_called_corrupt_metadata():
    with pytest.raises(launch.PackagePlatformUnavailable, match='not been qualified'):
        launch.published_release(platform='linux/unsupported')


def test_per_cpu_release_selects_the_exact_pair_and_refuses_incomplete_pairs():
    manifest = {'registry': 'ghcr.io/example', 'platforms': ['linux/arm64', 'linux/amd64'], 'images': []}
    for index, platform in enumerate(manifest['platforms']):
        for offset, role in enumerate(('xperfect-service', 'xperfect-native')):
            manifest['images'].append({'platform': platform, 'repository': role, 'digest': 'sha256:' + str(index * 2 + offset + 1) * 64})
    with patch.object(Path, 'read_text', return_value=json.dumps(manifest)):
        images, _ = launch.published_release(platform='linux/amd64')
        assert images['xperfect-service'].endswith('sha256:' + '3' * 64)
        assert images['xperfect-native'].endswith('sha256:' + '4' * 64)
    manifest['images'].pop()
    with patch.object(Path, 'read_text', return_value=json.dumps(manifest)):
        with pytest.raises(ValueError, match='release metadata'):
            launch.published_release(platform='linux/arm64')
